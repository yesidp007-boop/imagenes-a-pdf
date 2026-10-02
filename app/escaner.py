"""
escaner.py — convierte una foto de un documento en una "hoja escaneada".

Pasos:
  1. Endereza la foto según la etiqueta EXIF (las fotos del celular vienen acostadas
     con una etiqueta que dice "gírame").
  2. Busca el contorno de la hoja (4 esquinas) en una copia pequeña de la foto.
  3. Si la encuentra, recorta y corrige la perspectiva sobre la foto a resolución completa.
  4. Mejora la imagen: quita sombras, deja el fondo blanco y el texto nítido.
  5. Guarda un JPEG con un tamaño razonable para el PDF.

Si no encuentra la hoja con suficiente confianza, NO toca la foto: solo la endereza
y la reduce. Así nunca se pierde parte del documento ni se estropea el fondo.
(Esos casos se resolverán en la app con ajuste manual de esquinas.)

Uso:
    from escaner import escanear
    info = escanear("foto.jpg", "foto_escaneada.jpg", modo="color")
    info["detectado"]  -> True si encontró y recortó la hoja
"""

import cv2
import numpy as np
from PIL import Image, ImageOps

# Lado más largo de la imagen final. 2480 px = hoja A4 a 300 ppp (calidad de escáner).
LADO_MAXIMO = 2480
# Tamaño de la copia pequeña donde se buscan los bordes (más rápido y menos ruido).
LADO_DETECCION = 800
# La hoja debe ocupar al menos este porcentaje de la foto para aceptarla.
AREA_MINIMA = 0.20
# Margen que se recorta hacia adentro tras enderezar (1% de cada lado).
MARGEN = 0.01
# Brillo mínimo (0-255) para considerar que algo es papel. Evita tomar como hoja
# una pantalla oscura, una carpeta negra, etc.
BRILLO_PAPEL = 120
# Para aceptar un contorno como hoja: al menos este % de su perímetro debe coincidir
# con un borde visible, y el interior debe ser algo más claro que el exterior.
SOPORTE_MINIMO = 0.75
CONTRASTE_MINIMO = 20
SOPORTE_MINIMO_LADO = 0.35   # minimo de soporte (evidencia de borde) por CADA lado, no solo el promedio
CONTRASTE_MINIMO_LADO = 8    # minimo de contraste por CADA lado

MODOS = ("color", "gris", "byn", "original")


# ─────────────────────────── carga ───────────────────────────
def abrir_enderezada(ruta):
    """Abre la imagen como PIL RGB ya enderezada según la etiqueta EXIF.

    Esta es la ÚNICA puerta de entrada a los píxeles: define el sistema de
    coordenadas en el que viven las esquinas (las detectadas y las que ajusta
    el usuario a mano). Cualquier parte de la app que muestre la foto para
    elegir esquinas debe abrirla con esta función, o las coordenadas quedarán
    en una orientación distinta a la que se recorta.
    """
    with Image.open(ruta) as im:
        im = ImageOps.exif_transpose(im)
        return im.convert("RGB")


def _cargar(ruta):
    """Abre la imagen, aplica la rotación EXIF y la devuelve como array BGR de OpenCV."""
    rgb = np.asarray(abrir_enderezada(ruta))
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)


# ─────────────────────── detección de la hoja ───────────────────────
def _ordenar_esquinas(pts):
    """Ordena 4 puntos como: arriba-izq, arriba-der, abajo-der, abajo-izq."""
    pts = pts.reshape(4, 2).astype("float32")
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()
    return np.array([
        pts[np.argmin(s)],  # arriba-izq: x+y más pequeño
        pts[np.argmin(d)],  # arriba-der: y-x más pequeño
        pts[np.argmax(s)],  # abajo-der: x+y más grande
        pts[np.argmax(d)],  # abajo-izq: y-x más grande
    ], dtype="float32")


def _cuadrilatero_valido(quad, ancho, alto):
    """Comprueba que el cuadrilátero parezca una hoja y no un objeto cualquiera."""
    if not cv2.isContourConvex(quad.astype("int32")):
        return False
    area = cv2.contourArea(quad)
    if area < AREA_MINIMA * ancho * alto:
        return False
    # ángulos internos entre 45° y 135° (una hoja fotografiada no se deforma más)
    q = _ordenar_esquinas(quad)
    for i in range(4):
        a, b, c = q[i - 1], q[i], q[(i + 1) % 4]
        v1, v2 = a - b, c - b
        cos = np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2) + 1e-6)
        ang = np.degrees(np.arccos(np.clip(cos, -1, 1)))
        if not 45 <= ang <= 135:
            return False
    return True


def _candidatos(gris):
    """Genera varias imágenes binarias; de cada una salen posibles contornos de la hoja.
    Se prueban muchas porque ningún método sirve para todos los fondos."""
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    # medianBlur borra el texto y la textura fina del fondo (mantel, madera...)
    suave = cv2.medianBlur(gris, 9)

    def limpiar(b):
        b = cv2.morphologyEx(b, cv2.MORPH_CLOSE, kernel, iterations=3)
        return cv2.morphologyEx(b, cv2.MORPH_OPEN, kernel, iterations=2)

    # 1) Otsu: separa hoja clara de fondo oscuro.
    _, otsu = cv2.threshold(suave, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    yield limpiar(otsu)

    # 2) Umbrales altos: con fondos claros, el papel es solo "lo más blanco" de la foto.
    for pct in (60, 70, 80, 88):
        t = np.percentile(suave, pct)
        yield limpiar(((suave > t) * 255).astype(np.uint8))

    # 3) Bordes de Canny con distintas sensibilidades.
    for bajo, alto in ((20, 60), (40, 120), (60, 180)):
        bordes = cv2.Canny(suave, bajo, alto)
        yield cv2.dilate(bordes, kernel, iterations=2)


def _brillo_dentro(gris, quad):
    """Mediana de brillo (0-255) de los píxeles dentro del cuadrilátero."""
    mascara = np.zeros(gris.shape, np.uint8)
    cv2.fillConvexPoly(mascara, quad.astype("int32"), 255)
    return float(np.median(gris[mascara > 0]))


def _puntuar_por_lado(quad, bordes, gris):
    """Qué tan probable es que el cuadrilátero sea el borde real de la hoja.

    Calcula soporte y contraste por CADA uno de los 4 lados por separado,
    no solo el promedio. Esto evita que un lado malo (borde roto del papel,
    o una grieta del fondo) se esconda detrás de 3 lados buenos.

    Devuelve:
        (promedio_soporte, promedio_contraste, lista_de_4_tuplas_lado)
    donde cada tupla_lado es (soporte_lado, contraste_lado, dentro_lado, fuera_lado)
    """
    h, w = gris.shape
    q = _ordenar_esquinas(quad)
    centro = q.mean(axis=0)
    lados = []  # lista de (soporte, contraste) por lado

    for i in range(4):
        a, b = q[i], q[(i + 1) % 4]
        n = max(2, int(np.linalg.norm(b - a) / 2))
        en_borde = 0
        total = 0
        dentro, fuera = [], []
        for t in np.linspace(0, 1, n, endpoint=False):
            p = a + (b - a) * t
            x, y = int(p[0]), int(p[1])
            if not (0 <= x < w and 0 <= y < h):
                continue
            total += 1
            if bordes[y, x]:
                en_borde += 1
            # un punto 6 px hacia el centro y otro 6 px hacia afuera
            v = centro - p
            v = v / (np.linalg.norm(v) + 1e-6)
            xi, yi = (p + v * 6).astype(int)
            xo, yo = (p - v * 6).astype(int)
            if 0 <= xi < w and 0 <= yi < h:
                dentro.append(gris[yi, xi])
            if 0 <= xo < w and 0 <= yo < h:
                fuera.append(gris[yo, xo])
        if total == 0:
            lados.append((0.0, 0.0, dentro, fuera))
            continue
        soporte = en_borde / total
        # Si casi no hay "afuera" no se puede comprobar que sea una hoja:
        # se rechaza (contraste = 0).
        if len(fuera) < total * 0.6:
            lados.append((soporte, 0.0, dentro, fuera))
            continue
        contraste = float(np.median(dentro)) - float(np.median(fuera))
        lados.append((soporte, contraste, dentro, fuera))

    if not lados:
        return 0.0, 0.0, []

    soportes = [l[0] for l in lados]
    contrastes = [l[1] for l in lados]
    promedio_soporte = float(np.mean(soportes))
    promedio_contraste = float(np.mean(contrastes))

    return promedio_soporte, promedio_contraste, lados


def _puntuar(quad, bordes, gris):
    """Qué tan probable es que el cuadrilátero sea el borde real de la hoja.

    - soporte: fracción del perímetro que coincide con un borde visible en la foto.
      El borde de una hoja es una línea continua; un recorte equivocado atraviesa
      el fondo y casi no coincide con bordes.
    - contraste: justo por dentro del borde debe ser más claro que justo por fuera.
      Esto evita elegir el recuadro impreso dentro de la hoja (blanco a ambos lados).

    NOTA: Esta función ahora delega en _puntuar_por_lado y suma los valores.
    Se mantiene para compatibilidad, pero se prefiere _puntuar_por_lado.
    """
    soporte, contraste, lados = _puntuar_por_lado(quad, bordes, gris)
    return soporte, contraste


def _lineas_rectas(gris):
    """Detecta segmentos rectos largos (posibles bordes de la hoja) y los devuelve
    como rectas infinitas (a, b, c) con a*x + b*y = c, separadas en 'horizontales'
    y 'verticales'."""
    h, w = gris.shape
    suave = cv2.medianBlur(gris, 7)
    # dos sensibilidades: la normal y una más fina para bordes de poco contraste
    # (hoja blanca sobre mantel claro)
    bordes = cv2.Canny(suave, 30, 90) | cv2.Canny(suave, 12, 36)
    segmentos = cv2.HoughLinesP(bordes, 1, np.pi / 180, threshold=50,
                                minLineLength=int(0.12 * min(h, w)), maxLineGap=40)
    horiz, vert = [], []
    if segmentos is not None:
        # según la versión de OpenCV el resultado viene como (N,1,4) o (N,4): se normaliza
        for x1, y1, x2, y2 in np.asarray(segmentos).reshape(-1, 4):
            x1, y1, x2, y2 = float(x1), float(y1), float(x2), float(y2)
            dx, dy = x2 - x1, y2 - y1
            largo = float(np.hypot(dx, dy))
            a, b = -dy / largo, dx / largo  # normal de la recta
            c = a * x1 + b * y1
            (horiz if abs(dx) >= abs(dy) else vert).append((largo, a, b, c))

    def unir(lineas):
        # rectas casi iguales (mismo ángulo y distancia) se quedan solo con la más larga
        lineas.sort(reverse=True)
        elegidas = []
        for l in lineas:
            _, a, b, c = l
            if all(abs(a * e[1] + b * e[2]) < 0.995 or abs(c - e[3] * np.sign(a * e[1] + b * e[2])) > 12
                   for e in elegidas):
                elegidas.append(l)
            if len(elegidas) >= 10:
                break
        return [(a, b, c) for _, a, b, c in elegidas]

    horiz, vert = unir(horiz), unir(vert)
    return horiz, vert


def _cruce(l1, l2):
    a1, b1, c1 = l1
    a2, b2, c2 = l2
    det = a1 * b2 - a2 * b1
    if abs(det) < 1e-6:
        return None
    return ((c1 * b2 - c2 * b1) / det, (a1 * c2 - a2 * c1) / det)


def _quads_por_lineas(gris):
    """Arma cuadriláteros con 2 rectas 'horizontales' y 2 'verticales'."""
    h, w = gris.shape
    horiz, vert = _lineas_rectas(gris)
    tol = 0.02 * max(h, w)
    for i in range(len(horiz)):
        for j in range(i + 1, len(horiz)):
            for k in range(len(vert)):
                for m in range(k + 1, len(vert)):
                    pts = [_cruce(horiz[i], vert[k]), _cruce(horiz[i], vert[m]),
                           _cruce(horiz[j], vert[m]), _cruce(horiz[j], vert[k])]
                    if any(p is None for p in pts):
                        continue
                    if any(not (-tol <= x <= w + tol and -tol <= y <= h + tol) for x, y in pts):
                        continue
                    q = np.array(pts, dtype="float32")
                    q[:, 0] = np.clip(q[:, 0], 0, w - 1)
                    q[:, 1] = np.clip(q[:, 1], 0, h - 1)
                    yield q


def _quads_por_contornos(gris):
    for binaria in _candidatos(gris):
        contornos, _ = cv2.findContours(binaria, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in sorted(contornos, key=cv2.contourArea, reverse=True)[:5]:
            casco = cv2.convexHull(c)  # tapa huecos (p. ej. un dedo sobre el borde)
            perimetro = cv2.arcLength(casco, True)
            for eps in (0.02, 0.03, 0.04, 0.05):
                a = cv2.approxPolyDP(casco, eps * perimetro, True)
                if len(a) == 4:
                    yield a.reshape(4, 2).astype("float32")
                    break


def _buscar_hoja(img):
    """Devuelve las 4 esquinas de la hoja (en coordenadas de img) o None.

    Se generan muchos cuadriláteros candidatos por dos caminos (contornos de zonas
    claras y combinaciones de bordes rectos) y se elige el que mejor coincide con
    bordes reales de la foto."""
    alto, ancho = img.shape[:2]
    escala = LADO_DETECCION / max(alto, ancho)
    peq = cv2.resize(img, (int(ancho * escala), int(alto * escala)), interpolation=cv2.INTER_AREA)
    gris = cv2.cvtColor(peq, cv2.COLOR_BGR2GRAY)
    ph, pw = gris.shape

    # mapa de bordes "de verdad" para puntuar (independiente de cómo se generó el candidato)
    suave = cv2.medianBlur(gris, 5)
    # dilatación amplia: tolera que el contorno candidato quede unos píxeles corrido
    bordes = cv2.dilate(cv2.Canny(suave, 30, 90), np.ones((15, 15), np.uint8))

    mejor, mejor_area = None, 0.0
    candidatos = list(_quads_por_contornos(gris)) + list(_quads_por_lineas(gris))
    for quad in candidatos:
        if not _cuadrilatero_valido(quad, pw, ph):
            continue
        area = cv2.contourArea(quad) / (pw * ph)
        if area > 0.97:
            continue  # es el marco de la foto, no una hoja
        if _brillo_dentro(gris, quad) < BRILLO_PAPEL:
            continue  # el papel es claro
        soporte, contraste, lados = _puntuar_por_lado(quad, bordes, gris)
        # chequeo global (promedio de los 4 lados)
        if soporte < SOPORTE_MINIMO or contraste < CONTRASTE_MINIMO:
            continue
        # chequeo por lado: ninguno puede estar por debajo del umbral mínimo
        soportes_lado = [s for s, c, d, f in lados]
        contrastes_lado = [c for s, c, d, f in lados]
        if any(s < SOPORTE_MINIMO_LADO for s in soportes_lado) or any(c < CONTRASTE_MINIMO_LADO for c in contrastes_lado):
            continue
        # preferir el más grande (la hoja completa), no un recuadro impreso dentro
        if area > mejor_area:
            mejor, mejor_area = quad, area

    if mejor is None:
        return None
    return _ordenar_esquinas(mejor / escala)


def _enderezar(img, esquinas):
    """Recorta y corrige la perspectiva para que la hoja quede rectangular."""
    tl, tr, br, bl = esquinas
    ancho = int(max(np.linalg.norm(br - bl), np.linalg.norm(tr - tl)))
    alto = int(max(np.linalg.norm(tr - br), np.linalg.norm(tl - bl)))
    destino = np.array([[0, 0], [ancho - 1, 0], [ancho - 1, alto - 1], [0, alto - 1]], dtype="float32")
    matriz = cv2.getPerspectiveTransform(esquinas, destino)
    hoja = cv2.warpPerspective(img, matriz, (ancho, alto), flags=cv2.INTER_CUBIC,
                               borderMode=cv2.BORDER_REPLICATE)
    # recortar un margen pequeño hacia adentro: elimina restos del fondo en los bordes
    m = int(min(ancho, alto) * MARGEN)
    return hoja[m:alto - m, m:ancho - m]


# ─────────────────────────── mejora ───────────────────────────
def _quitar_sombras(canal):
    """Divide el canal por una estimación del 'papel' para dejar el fondo blanco parejo."""
    k = max(15, (min(canal.shape[:2]) // 40) | 1)  # tamaño proporcional a la imagen, impar
    fondo = cv2.dilate(canal, np.ones((7, 7), np.uint8))  # borra el texto (oscuro)
    fondo = cv2.medianBlur(fondo, k)                      # suaviza: queda solo la luz
    normal = cv2.divide(canal, fondo, scale=255)
    return normal


def _mejorar(img, modo):
    if modo == "original":
        return img
    # La limpieza de fondo supone papel claro. Si la imagen es mayormente oscura
    # (foto de una pantalla, de noche...), se deja tal cual para no estropearla.
    if float(np.median(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))) < BRILLO_PAPEL:
        return img

    if modo == "color":
        canales = [_quitar_sombras(c) for c in cv2.split(img)]
        out = cv2.merge(canales)
        # contraste: estira los niveles dejando el 1% más oscuro en negro
        out = _niveles(out, negro_pct=1.0, blanco_pct=0.0)
        return out

    gris = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gris = _quitar_sombras(gris)
    if modo == "gris":
        return _niveles(gris, negro_pct=1.0, blanco_pct=0.0)

    # byn: blanco y negro puro, como una fotocopia.
    # Tras quitar sombras el papel ya es casi blanco parejo, así que basta un umbral
    # global (Otsu). Un suavizado leve antes evita que la textura del papel salga como ruido.
    suave = cv2.GaussianBlur(gris, (3, 3), 0)
    _, byn = cv2.threshold(suave, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return byn


def _niveles(img, negro_pct, blanco_pct):
    """Estira el contraste. El punto blanco se toma del nivel del papel (la mediana,
    porque en un documento la mayor parte es papel) para que el fondo quede blanco limpio."""
    bajo = np.percentile(img, negro_pct)
    if blanco_pct > 0:
        alto = np.percentile(img, 100 - blanco_pct)
    else:
        alto = max(200.0, float(np.median(img)) * 0.97)
    if alto - bajo < 10:
        return img
    lut = np.clip((np.arange(256) - bajo) * 255.0 / (alto - bajo), 0, 255).astype(np.uint8)
    return cv2.LUT(img, lut)


# ─────────────────────────── API ───────────────────────────
def escanear(ruta_entrada, ruta_salida, modo="color", recortar=True, esquinas=None):
    """
    Procesa una foto de documento y la guarda como JPEG en ruta_salida.

    modo: "color" (recomendado), "gris", "byn" (fotocopia) u "original" (sin mejora).
    recortar: si es False, no busca la hoja (solo endereza y mejora).
    esquinas: array de 4 puntos [x,y] para usar esquinas manuales en vez de detectar.
              Si es None, se usa detección automática.

    Devuelve un dict: {"detectado": bool, "ruta": ruta_salida, "tamano": (ancho, alto),
                       "esquinas": esquinas_usadas, "original_tamano": (ancho, alto)}
    """
    if modo not in MODOS:
        raise ValueError(f"modo debe ser uno de {MODOS}")

    img = _cargar(ruta_entrada)
    original_tamano = (img.shape[1], img.shape[0])  # guardar tamaño antes de cualquier modificación

    if esquinas is not None:
        # Usar esquinas manuales proporcionadas
        esquinas = _ordenar_esquinas(np.array(esquinas, dtype="float32"))
        img = _enderezar(img, esquinas)
        img = _mejorar(img, modo)
        detectado = True
    elif recortar:
        # Detección automática
        esquinas = _buscar_hoja(img)
        if esquinas is not None:
            img = _enderezar(img, esquinas)
            img = _mejorar(img, modo)
            detectado = True
        else:
            # No se encontró la hoja, solo mejorar sin recortar
            img = _mejorar(img, modo)
            detectado = False
    else:
        # recortar=False: solo mejorar sin recortar
        img = _mejorar(img, modo)
        detectado = False

    # reducir a calidad de escáner (300 ppp en A4) para que el PDF no pese demasiado
    alto, ancho = img.shape[:2]
    if max(alto, ancho) > LADO_MAXIMO:
        f = LADO_MAXIMO / max(alto, ancho)
        img = cv2.resize(img, (int(ancho * f), int(alto * f)), interpolation=cv2.INTER_AREA)

    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 88])
    if not ok:
        raise RuntimeError("No se pudo codificar la imagen")
    with open(ruta_salida, "wb") as f:  # evita problemas de cv2.imwrite con acentos en Windows
        f.write(buf.tobytes())

    return {"detectado": detectado, "ruta": ruta_salida,
            "tamano": (img.shape[1], img.shape[0]), "esquinas": esquinas,
            "original_tamano": original_tamano}


# Prueba rápida desde la terminal:
#   python escaner.py foto1.jpg foto2.jpg ...
# Crea foto1_escaneada.jpg, etc., en la misma carpeta.
if __name__ == "__main__":
    import os
    import sys
    import time

    if len(sys.argv) < 2:
        print("Uso: python escaner.py foto1.jpg [foto2.jpg ...] [--modo color|gris|byn]")
        sys.exit(1)
    args = sys.argv[1:]
    modo = "color"
    if "--modo" in args:
        i = args.index("--modo")
        modo = args[i + 1]
        del args[i:i + 2]
    for ruta in args:
        base, _ = os.path.splitext(ruta)
        salida = f"{base}_escaneada.jpg"
        t = time.time()
        info = escanear(ruta, salida, modo=modo)
        estado = "hoja detectada y recortada" if info["detectado"] else "NO se detectó la hoja (foto sin cambios)"
        print(f"{os.path.basename(ruta)}: {estado} -> {os.path.basename(salida)} "
              f"{info['tamano'][0]}x{info['tamano'][1]} ({time.time() - t:.1f}s)")
