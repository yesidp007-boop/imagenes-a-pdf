"""Prueba de regresión para el bug de orientación EXIF entre
WidgetAjusteEsquinas (main.py) y escaner.escanear().

Antes de la corrección, el widget abría la foto con Image.open() a secas
(sin aplicar la rotación EXIF), mientras que escaner._cargar() sí la
aplicaba. Esto hacía que las esquinas ajustadas a mano por el usuario
quedaran en un sistema de coordenadas distinto al que usa escanear() para
recortar, y el resultado salía girado/recortado mal.

La corrección: ambos (el widget y el escáner) abren la imagen con la misma
función, escaner.abrir_enderezada(), que es la única puerta de entrada a
los píxeles.

Este test reproduce una foto con etiqueta EXIF Orientation=6 (el caso
típico de una foto de celular "acostada"), simula que el usuario ajusta
las 4 esquinas sobre la imagen ya enderezada (igual que hace el widget) y
comprueba que el recorte final conserva esa orientación: cada esquina del
documento queda en su lugar, no rotada 90°.
"""
import os
import sys

import numpy as np
import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from escaner import abrir_enderezada, escanear  # noqa: E402

# Documento "lógico": hoja clara dentro de una foto vertical de 600x800.
ANCHO, ALTO = 600, 800
DOC = (100, 150, 500, 650)  # (x0, y0, x1, y1)
MARCA = 40  # tamaño del cuadrado de color que marca cada esquina del documento
ESQUINAS_DOC = {
    "arriba-izq": ((DOC[0], DOC[1]), (255, 0, 0)),
    "arriba-der": ((DOC[2] - MARCA, DOC[1]), (0, 200, 0)),
    "abajo-der": ((DOC[2] - MARCA, DOC[3] - MARCA), (0, 0, 255)),
    "abajo-izq": ((DOC[0], DOC[3] - MARCA), (255, 255, 0)),
}


def _crear_foto_celular(ruta):
    """Genera una foto con los píxeles acostados (90°) + EXIF Orientation=6,
    tal como la guarda un celular sostenido en vertical."""
    derecha = Image.new("RGB", (ANCHO, ALTO), (60, 60, 60))
    d = ImageDraw.Draw(derecha)
    d.rectangle(DOC, fill=(250, 250, 250))
    for (mx, my), color in ESQUINAS_DOC.values():
        d.rectangle([mx, my, mx + MARCA, my + MARCA], fill=color)

    acostada = derecha.rotate(90, expand=True)  # como queda el sensor
    exif = Image.Exif()
    exif[0x0112] = 6  # Orientation: "gírame 90° para verla bien"
    acostada.save(ruta, "JPEG", quality=95, exif=exif)
    return derecha.size, acostada.size


def test_abrir_enderezada_devuelve_orientacion_correcta(tmp_path):
    foto = str(tmp_path / "foto_celular.jpg")
    tamano_derecho, tamano_acostado = _crear_foto_celular(foto)

    with Image.open(foto) as crudo:
        assert crudo.size == tamano_acostado, (
            "la foto de prueba no quedó acostada como una foto de celular real"
        )

    pil = abrir_enderezada(foto)
    assert pil.size == tamano_derecho, (
        "abrir_enderezada() no corrigió la orientación EXIF: "
        f"devolvió {pil.size}, se esperaba {tamano_derecho}"
    )


def test_recorte_con_esquinas_manuales_conserva_orientacion(tmp_path):
    """Simula el flujo completo: el usuario ve la imagen enderezada (como
    ahora hace el widget) y arrastra las 4 esquinas sobre el documento tal
    cual lo ve. El recorte final debe coincidir con esas esquinas, no con
    una versión girada 90°."""
    foto = str(tmp_path / "foto_celular.jpg")
    _crear_foto_celular(foto)

    esquinas_usuario = np.array([
        [DOC[0], DOC[1]],  # arriba-izq
        [DOC[2], DOC[1]],  # arriba-der
        [DOC[2], DOC[3]],  # abajo-der
        [DOC[0], DOC[3]],  # abajo-izq
    ], dtype="float32")

    salida = str(tmp_path / "recortado.jpg")
    escanear(foto, salida, modo="original", recortar=True, esquinas=esquinas_usuario)
    recorte = Image.open(salida)

    ratio_esperado = (DOC[2] - DOC[0]) / (DOC[3] - DOC[1])
    ratio_real = recorte.width / recorte.height
    assert ratio_real == pytest.approx(ratio_esperado, abs=0.05), (
        f"el recorte salió con relación de aspecto {ratio_real:.3f}, "
        f"se esperaba {ratio_esperado:.3f} (un ratio invertido indica giro de 90°)"
    )

    # Cada marca de color debe aparecer en SU esquina del recorte, no en otra.
    arr = np.asarray(recorte)
    h, w = arr.shape[:2]
    s = 18  # muestrear un poco hacia adentro de cada esquina
    muestras = {
        "arriba-izq": arr[s, s],
        "arriba-der": arr[s, w - 1 - s],
        "abajo-der": arr[h - 1 - s, w - 1 - s],
        "abajo-izq": arr[h - 1 - s, s],
    }
    for nombre, pixel in muestras.items():
        esperado = np.array(ESQUINAS_DOC[nombre][1], dtype=float)
        distancia = float(np.linalg.norm(pixel.astype(float) - esperado))
        assert distancia < 70, (
            f"la esquina '{nombre}' del recorte tiene color {tuple(int(v) for v in pixel)}, "
            f"se esperaba algo cercano a {tuple(ESQUINAS_DOC[nombre][1])}: "
            "el recorte está girado o con las esquinas mal asignadas"
        )
