"""Prueba de regresión: generación de PDF con fotos sin escanear que
conservan su etiqueta EXIF de orientación.

Las fotos que SÍ pasan por escaner.escanear() ya quedan derechas y sin EXIF
(escaner._cargar aplica ImageOps.exif_transpose y el archivo de salida se
graba con cv2.imencode, que no escribe EXIF). Pero una foto puede llegar a
ImageToPDFApp._write_pdf() sin pasar por el escáner: por ejemplo cuando
escanear() lanza una excepción y main.py usa la foto original como respaldo
(ver _on_revision_terminada). Esa foto sí conserva el EXIF del celular.

Antes de la corrección, _write_pdf() pasaba esas fotos directo a
img2pdf.convert(). img2pdf endereza automáticamente las rotaciones simples
(90/180/270) leyendo el EXIF, pero con una orientación ESPEJADA (valores
EXIF 2, 4, 5 o 7 — p. ej. algunas fotos de cámara frontal) o inválida lanza
ExifOrientationError. Esa excepción hacía caer TODO el lote al respaldo de
Pillow (Image.save(..., "PDF")), que no aplica NINGUNA corrección EXIF: el
resultado quedaba espejado o girado en el PDF final.

La corrección: _write_pdf() ahora normaliza cada foto con
_normalizar_para_pdf() antes de entregarla a img2pdf — graba una copia con
los píxeles ya enderezados y sin etiqueta EXIF cuando hace falta, y deja la
ruta tal cual cuando no (evita recomprimir fotos que no lo necesitan, como
las que ya vienen del escáner).
"""
import os
import sys

import numpy as np
import pikepdf
import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import img2pdf  # noqa: E402
from main import ImageToPDFApp  # noqa: E402

ANCHO, ALTO = 500, 700
MARCA = 40
# Marcas de color en las esquinas de la imagen "derecha" (como la vería una
# persona, sin ninguna rotación EXIF de por medio).
ESQUINAS = {
    "arriba-izq": ((0, 0), (255, 0, 0)),
    "arriba-der": ((ANCHO - MARCA, 0), (0, 200, 0)),
    "abajo-der": ((ANCHO - MARCA, ALTO - MARCA), (0, 0, 255)),
    "abajo-izq": ((0, ALTO - MARCA), (255, 255, 0)),
}


class _AppFalso:
    """Expone los dos métodos de ImageToPDFApp bajo prueba sin necesitar
    construir (ni mostrar) la app Kivy completa."""
    _normalizar_para_pdf = ImageToPDFApp._normalizar_para_pdf
    _write_pdf = ImageToPDFApp._write_pdf

    def __init__(self, ordered_images):
        self.ordered_images = ordered_images


def _crear_foto(ruta, orientacion_exif):
    """Genera una foto JPEG marcada con 4 colores en las esquinas, guardada
    como la guardaría un celular con la etiqueta EXIF dada:
      1 = sin rotación · 6 = girar 90° · 2 = espejada horizontal (sin rotar)
    """
    derecha = Image.new("RGB", (ANCHO, ALTO), (200, 200, 200))
    d = ImageDraw.Draw(derecha)
    for (mx, my), color in ESQUINAS.values():
        d.rectangle([mx, my, mx + MARCA, my + MARCA], fill=color)

    if orientacion_exif == 6:
        crudo = derecha.rotate(90, expand=True)
    elif orientacion_exif == 2:
        crudo = derecha.transpose(Image.FLIP_LEFT_RIGHT)
    else:
        crudo = derecha

    exif = Image.Exif()
    exif[0x0112] = orientacion_exif  # Orientation
    crudo.save(ruta, "JPEG", quality=95, exif=exif)


def _primera_imagen_incrustada(ruta_pdf):
    """Extrae la primera imagen embebida de la primera página de un PDF."""
    with pikepdf.open(ruta_pdf) as pdf:
        pagina = pdf.pages[0]
        obj = next(iter(pagina.images.values()))
        return pikepdf.PdfImage(obj).as_pil_image().convert("RGB")


def _comprobar_esquinas(img, contexto):
    arr = np.asarray(img)
    h, w = arr.shape[:2]
    s = 15
    muestras = {
        "arriba-izq": arr[s, s],
        "arriba-der": arr[s, w - 1 - s],
        "abajo-der": arr[h - 1 - s, w - 1 - s],
        "abajo-izq": arr[h - 1 - s, s],
    }
    for nombre, pixel in muestras.items():
        esperado = np.array(ESQUINAS[nombre][1], dtype=float)
        distancia = float(np.linalg.norm(pixel.astype(float) - esperado))
        assert distancia < 70, (
            f"[{contexto}] la esquina '{nombre}' salió con color "
            f"{tuple(int(v) for v in pixel)}, se esperaba algo cercano a "
            f"{tuple(ESQUINAS[nombre][1])}: la imagen está girada o espejada"
        )


def test_img2pdf_rechaza_la_orientacion_espejada_sin_normalizar(tmp_path):
    """Confirma la causa del bug: una foto con EXIF espejado (valor 2) hace
    que img2pdf.convert() lance ExifOrientationError si se le pasa tal cual."""
    foto = str(tmp_path / "espejada.jpg")
    _crear_foto(foto, orientacion_exif=2)

    layout = img2pdf.get_layout_fun((img2pdf.mm_to_pt(210), img2pdf.mm_to_pt(297)))
    with pytest.raises(img2pdf.ExifOrientationError):
        img2pdf.convert([foto], layout_fun=layout)


@pytest.mark.parametrize("orientacion_exif", [1, 6, 2])
def test_normalizar_para_pdf_deja_la_imagen_correcta(tmp_path, orientacion_exif):
    foto = str(tmp_path / f"foto_{orientacion_exif}.jpg")
    _crear_foto(foto, orientacion_exif)

    app = _AppFalso(ordered_images=[])
    resultado = app._normalizar_para_pdf(foto, str(tmp_path))

    if orientacion_exif == 1:
        # Sin rotación que corregir: no debe recomprimir, debe devolver la
        # misma ruta.
        assert resultado == foto
    else:
        assert resultado != foto, "debería haber grabado una copia enderezada"
        with Image.open(resultado) as normalizada:
            assert normalizada.getexif().get(0x0112, 1) in (1, None), (
                "la copia normalizada no debería conservar una etiqueta EXIF de rotación"
            )
            assert normalizada.size == (ANCHO, ALTO)
            _comprobar_esquinas(normalizada.convert("RGB"), f"normalizada orientacion={orientacion_exif}")

    # Y, sobre todo: ya no debe reventar a img2pdf.
    layout = img2pdf.get_layout_fun((img2pdf.mm_to_pt(210), img2pdf.mm_to_pt(297)))
    img2pdf.convert([resultado], layout_fun=layout)  # no debe lanzar


@pytest.mark.parametrize("orientacion_exif", [1, 6, 2])
def test_write_pdf_produce_pagina_con_orientacion_correcta(tmp_path, orientacion_exif):
    """Extremo a extremo: ImageToPDFApp._write_pdf() con una sola foto sin
    escanear que trae EXIF. La imagen incrustada en el PDF final debe quedar
    tal como la vería una persona, sin importar el valor de EXIF original."""
    foto = str(tmp_path / f"foto_{orientacion_exif}.jpg")
    _crear_foto(foto, orientacion_exif)

    app = _AppFalso(ordered_images=[foto])
    salida = str(tmp_path / "salida.pdf")
    app._write_pdf(salida)

    assert os.path.getsize(salida) > 0
    incrustada = _primera_imagen_incrustada(salida)
    assert incrustada.size == (ANCHO, ALTO), (
        f"la imagen incrustada en el PDF mide {incrustada.size}, "
        f"se esperaba {(ANCHO, ALTO)} (orientación EXIF {orientacion_exif})"
    )
    _comprobar_esquinas(incrustada, f"PDF final orientacion={orientacion_exif}")


def test_write_pdf_con_lote_mixto(tmp_path):
    """El caso real: una foto ya escaneada (sin EXIF) junto con una foto sin
    escanear con orientación espejada en el mismo lote. Antes del fix, la
    excepción de la segunda tiraba TODO el lote al respaldo de Pillow y
    arruinaba también la primera."""
    foto_normal = str(tmp_path / "ya_escaneada.jpg")
    _crear_foto(foto_normal, orientacion_exif=1)

    foto_espejada = str(tmp_path / "sin_escanear.jpg")
    _crear_foto(foto_espejada, orientacion_exif=2)

    app = _AppFalso(ordered_images=[foto_normal, foto_espejada])
    salida = str(tmp_path / "salida.pdf")
    app._write_pdf(salida)

    with pikepdf.open(salida) as pdf:
        assert len(pdf.pages) == 2
        for i, pagina in enumerate(pdf.pages):
            obj = next(iter(pagina.images.values()))
            img = pikepdf.PdfImage(obj).as_pil_image().convert("RGB")
            _comprobar_esquinas(img, f"página {i + 1} del lote mixto")
