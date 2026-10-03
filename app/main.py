import datetime
import json
import math
import os
import random
import shutil
import time
import uuid
from pathlib import Path
from threading import Thread

from kivy.animation import Animation
from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.floatlayout import FloatLayout
from kivy.uix.scrollview import ScrollView
from kivy.uix.gridlayout import GridLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.image import Image as KivyImage
from kivy.uix.filechooser import FileChooserIconView
from kivy.uix.popup import Popup
from kivy.uix.textinput import TextInput
from kivy.uix.widget import Widget
from kivy.uix.behaviors import ButtonBehavior
from kivy.utils import platform
from kivy.logger import Logger
from kivy.graphics import Color, Rectangle, Ellipse, RoundedRectangle, Triangle
from kivy.graphics.texture import Texture

import numpy as np
import tempfile

import img2pdf
from PIL import Image, ImageOps

# El escáner (OpenCV) es opcional: si no está disponible, la app sigue funcionando
# y usa las fotos tal cual.
try:
    from escaner import escanear, abrir_enderezada
    Logger.info("main.py: escaner.py importado exitosamente")
except Exception as e:
    Logger.error(f"main.py: Escáner no disponible (esperado si OpenCV no está): {e}")
    escanear = None

    def abrir_enderezada(ruta):
        """Reserva si escaner.py no se pudo importar (no necesita OpenCV)."""
        with Image.open(ruta) as im:
            return ImageOps.exif_transpose(im).convert("RGB")

# Página A4 en puntos PDF; cada imagen se ajusta dentro sin deformarse
_A4 = (img2pdf.mm_to_pt(210), img2pdf.mm_to_pt(297))
_LAYOUT_A4 = img2pdf.get_layout_fun(_A4, fit=img2pdf.FitMode.into)


# Código propio para reconocer la respuesta del selector de fotos
REQUEST_PICK_IMAGES = 1001
# Activity.RESULT_OK de Android siempre vale -1 (el usuario eligió algo)
RESULT_OK = -1


def get_default_save_dir():
    """Directorio por defecto en escritorio (Descargas o Home)."""
    for cand in [Path.home() / "Downloads", Path.home() / "Descargas", Path.home()]:
        if cand.exists():
            return str(cand)
    return str(Path.home())


# ── utilidades Android ──
# android.activity (módulo de Kivy) SOLO sirve para escuchar resultados con bind().
# La pantalla real de la app (la Activity de Java) es PythonActivity.mActivity:
# con ella se abren otras apps, se obtiene el ContentResolver, la caché, etc.
def _android_activity():
    from jnius import autoclass  # type: ignore
    return autoclass("org.kivy.android.PythonActivity").mActivity


def _android_cache_dir():
    return _android_activity().getCacheDir().getAbsolutePath()


class WidgetAjusteEsquinas(Widget):
    """Widget para ajustar manualmente las 4 esquinas de la hoja.

    Muestra la imagen original con 4 puntos arrastrables en las esquinas,
    más 4 guías de punto medio (una por lado) que mueven las dos esquinas
    de ese lado juntas.

    IMPORTANTE: la foto se abre con escaner.abrir_enderezada(), igual que hace
    el escáner al recortar. Si se abriera con Image.open() a secas, las fotos
    del celular (que vienen acostadas con una etiqueta EXIF "gírame") se verían
    aquí en una orientación y se recortarían en otra, y el resultado saldría
    girado.
    """

    def __init__(self, imagen_ruta, esquinas_detectadas, on_confirmar, **kwargs):
        super().__init__(**kwargs)
        self.imagen_ruta = imagen_ruta
        self.on_confirmar_callback = on_confirmar

        # Una sola carga: de aquí salen las dimensiones y la texture, así no
        # pueden desincronizarse entre sí.
        self.imagen_pil = abrir_enderezada(imagen_ruta)
        self.imagen_ancho, self.imagen_alto = self.imagen_pil.size

        # Esquinas actuales, en coordenadas de la imagen enderezada: el mismo
        # espacio en el que las devuelve y las espera escaner.escanear().
        # Si no hay esquinas detectadas, usar las 4 esquinas de la imagen.
        if esquinas_detectadas is not None:
            # esquinas_detectadas ya está ordenado por _ordenar_esquinas
            self.esquinas = esquinas_detectadas.copy()
        else:
            self.esquinas = np.array([
                [0, 0],                                         # arriba-izq
                [self.imagen_ancho - 1, 0],                     # arriba-der
                [self.imagen_ancho - 1, self.imagen_alto - 1],  # abajo-der
                [0, self.imagen_alto - 1]                       # abajo-izq
            ], dtype="float32")

        # Cargar la imagen en una texture para el canvas
        self.texture = None
        self._cargar_texture()

        # Radio del punto arrastrable (+50% respecto al original de 12)
        self.punto_radio = 18

        # Para rastrear qué punto se está arrastrando
        self._punto_actual = None
        self._pos_inicial = None

        # Para rastrear qué lado (guía de punto medio) se está arrastrando
        self._lado_actual = None       # índice de lado (0..3) en arrastre, o None
        self._pos_inicial_lado = None  # posición de pantalla del frame anterior

        # Inicializar el canvas
        self._redibujar()

        # Ajustar tamaño inicial
        self.bind(size=self._redibujar, pos=self._redibujar)

    def _cargar_texture(self):
        """Cargar la imagen (ya enderezada por EXIF) en una texture de Kivy."""
        try:
            img = self.imagen_pil

            # Crear texture
            self.texture = Texture.create(
                size=(img.width, img.height),
                colorfmt='rgb'
            )
            self.texture.blit_buffer(
                img.tobytes(),
                colorfmt='rgb',
                bufferfmt='ubyte'
            )
            self.texture.flip_vertical()
        except Exception as e:
            Logger.error(f"main.py: Error cargando texture: {e}")
        finally:
            # Los píxeles ya están en la texture; soltar la copia de PIL
            # (una foto de 12 MP son ~36 MB de RAM, mucho para un celular).
            self.imagen_pil = None

    def _redibujar(self, *args):
        """Redibujar el canvas con la imagen, el contorno guía y los puntos."""
        self.canvas.clear()
        with self.canvas:
            # Limpiar fondo
            Color(0.1, 0.1, 0.1, 1)  # Fondo oscuro
            Rectangle(pos=self.pos, size=self.size)

            if self.texture is None:
                return

            # Calcular tamaño y posición para mantener aspect ratio
            img_ratio = self.imagen_ancho / self.imagen_alto
            box_ratio = self.size[0] / self.size[1]

            if img_ratio > box_ratio:
                # Imagen más ancha que caja: ancho fijo
                draw_width = self.size[0]
                draw_height = self.size[0] / img_ratio
                draw_x = self.pos[0]
                draw_y = self.pos[1] + (self.size[1] - draw_height) / 2
            else:
                # Imagen más alta que caja: alto fijo
                draw_height = self.size[1]
                draw_width = self.size[1] * img_ratio
                draw_x = self.pos[0] + (self.size[0] - draw_width) / 2
                draw_y = self.pos[1]

            # Dibujar imagen
            Color(1, 1, 1, 1)
            Rectangle(
                texture=self.texture,
                pos=(draw_x, draw_y),
                size=(draw_width, draw_height)
            )

            # Guardar mapeo para convertir coordenadas
            self._draw_x = draw_x
            self._draw_y = draw_y
            self._draw_width = draw_width
            self._draw_height = draw_height
            self._img_ancho = self.imagen_ancho
            self._img_alto = self.imagen_alto

            # Calcular coordenadas de pantalla de los 4 puntos de una vez.
            # Se reutiliza la misma lista para el contorno y para los círculos.
            puntos_pantalla = []
            for x, y in self.esquinas:
                px = float(draw_x + (x / self._img_ancho) * draw_width)
                py = float(draw_y + (1 - y / self._img_alto) * draw_height)  # Y invertido
                puntos_pantalla.append((px, py))

            # --- Contorno guía (cuadrilátero 1→2→3→4→1) ---
            #
            # kivy.graphics.Line NO renderiza en algunos drivers Android / OpenGL ES:
            # Kivy lo implementa con una malla de triángulos (smooth line) que el
            # driver envía a la GPU pero el draw call falla silenciosamente.
            # PushMatrix+Rotate+Rectangle tampoco funciona dentro del Popup porque el
            # test de stencil del Popup evalúa coordenadas pre-rotación en ciertos
            # drivers, haciendo invisibles los rectángulos rotados.
            #
            # Solución fiable: dibujar el contorno como una cadena de pequeñas Ellipse
            # (radio 2.5 px, paso 7 px → se solapan 2 px → línea casi sólida).
            # Ellipse usa triangle fan, primitiva GL que funciona en todos los drivers.
            Color(1, 1, 1, 0.85)  # blanco casi opaco, visible sobre cualquier imagen
            p0, p1, p2, p3 = puntos_pantalla
            lados = [(p0, p1), (p1, p2), (p2, p3), (p3, p0)]
            for (ax, ay), (bx, by) in lados:
                dx = bx - ax
                dy = by - ay
                largo = math.hypot(dx, dy)
                if largo < 1:
                    continue

                # Número de puntos para cubrir el lado con solapamiento mínimo
                radio_punto = 2.5   # px, da un diámetro de 5 px por punto
                paso = 7            # px entre centros; con ∅=5, solapamiento 2 px
                n_puntos = max(2, int(largo / paso) + 1)

                for k in range(n_puntos):
                    t = k / (n_puntos - 1)
                    cx = ax + t * dx
                    cy = ay + t * dy
                    Ellipse(
                        pos=(cx - radio_punto, cy - radio_punto),
                        size=(radio_punto * 2, radio_punto * 2)
                    )

            # --- Guías de punto medio de cada lado (debajo de los puntos de
            # esquina, encima del contorno): al arrastrarlas, mueven las dos
            # esquinas de ese lado juntas (ver on_touch_down/move). ---
            self._puntos_medios_pantalla = [
                ((p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2),  # medio del lado 0-1 (arriba)
                ((p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2),  # medio del lado 1-2 (derecha)
                ((p2[0] + p3[0]) / 2, (p2[1] + p3[1]) / 2),  # medio del lado 2-3 (abajo)
                ((p3[0] + p0[0]) / 2, (p3[1] + p0[1]) / 2),  # medio del lado 3-0 (izquierda)
            ]
            radio_medio = self.punto_radio  # mismo tamaño que las esquinas, más fácil de agarrar
            Color(0.85, 0.85, 0.85, 0.9)  # gris claro, neutro, distinto de las esquinas
            for mx, my in self._puntos_medios_pantalla:
                Ellipse(
                    pos=(mx - radio_medio, my - radio_medio),
                    size=(radio_medio * 2, radio_medio * 2)
                )

            # --- Puntos arrastrables (encima de todo) ---
            colores = [(1, 0, 0, 1), (0, 1, 0, 1), (0, 0, 1, 1), (1, 1, 0, 1)]
            for i, (px, py) in enumerate(puntos_pantalla):
                Color(*colores[i])
                Ellipse(
                    pos=(px - self.punto_radio, py - self.punto_radio),
                    size=(self.punto_radio * 2, self.punto_radio * 2)
                )
                # Nota: Kivy no tiene text rendering directo en canvas,
                # así que usamos Label para los números (ver _agregar_numeros)

    def _agregar_numeros(self):
        """Agregar Labels con los números de los puntos."""
        # Limpiar widgets anteriores
        self.clear_widgets()

        if not hasattr(self, '_draw_x'):
            return

        for i, (x, y) in enumerate(self.esquinas):
            px = float(self._draw_x + (x / self._img_ancho) * self._draw_width)
            py = float(self._draw_y + (1 - y / self._img_alto) * self._draw_height)

            lbl = Label(
                text=str(i + 1),
                font_size='14sp',
                bold=True,
                color=(1, 1, 1, 1),
                size_hint=(None, None),
                size=('36dp', '36dp'),  # +50% respecto a 24dp original, proporcional al radio
                center=(px, py)
            )
            self.add_widget(lbl)

    def coordenada_imagen_a_pantalla(self, x_img, y_img):
        """Convertir coordenada de imagen (0..ancho, 0..alto) a pantalla."""
        if not hasattr(self, '_draw_x'):
            return x_img, y_img
        px = self._draw_x + (x_img / self._img_ancho) * self._draw_width
        py = self._draw_y + (1 - y_img / self._img_alto) * self._draw_height
        return px, py

    def coordenada_pantalla_a_imagen(self, x_screen, y_screen):
        """Convertir coordenada de pantalla a imagen (0..ancho, 0..alto)."""
        if not hasattr(self, '_draw_x'):
            return x_screen, y_screen
        x_img = (x_screen - self._draw_x) / self._draw_width * self._img_ancho
        y_img = (1 - (y_screen - self._draw_y) / self._draw_height) * self._img_alto
        return x_img, y_img

    def _punto_en(self, x, y, punto_idx):
        """Verificar si una coordenada de pantalla está dentro de un punto."""
        if not hasattr(self, '_draw_x'):
            return False
        px, py = self.coordenada_imagen_a_pantalla(
            self.esquinas[punto_idx][0], self.esquinas[punto_idx][1]
        )
        return abs(x - px) <= self.punto_radio * 2 and abs(y - py) <= self.punto_radio * 2

    def _linea_en(self, x, y, lado_idx):
        """Verificar si una coordenada de pantalla está dentro de la guía
        de punto medio de un lado."""
        if not hasattr(self, '_puntos_medios_pantalla'):
            return False
        mx, my = self._puntos_medios_pantalla[lado_idx]
        radio = self.punto_radio * 2  # misma generosidad de toque que las esquinas (_punto_en)
        return abs(x - mx) <= radio and abs(y - my) <= radio

    def on_touch_down(self, touch):
        """Iniciar arrastre si se toca un punto de esquina o una guía de lado."""
        if not self.collide_point(*touch.pos):
            return super().on_touch_down(touch)

        for i in range(4):
            if self._punto_en(touch.x, touch.y, i):
                self._punto_actual = i
                self._pos_inicial = (touch.x, touch.y)
                return True

        for i in range(4):
            if self._linea_en(touch.x, touch.y, i):
                self._lado_actual = i
                self._pos_inicial_lado = (touch.x, touch.y)
                return True

        return super().on_touch_down(touch)

    def on_touch_move(self, touch):
        """Mover el punto o el lado si se está arrastrando."""
        if self._punto_actual is not None:
            # Convertir coordenada de pantalla a imagen
            x_img, y_img = self.coordenada_pantalla_a_imagen(touch.x, touch.y)

            # Clamp a los límites de la imagen
            x_img = float(max(0, min(x_img, self.imagen_ancho - 1)))
            y_img = float(max(0, min(y_img, self.imagen_alto - 1)))

            self.esquinas[self._punto_actual] = [x_img, y_img]
            self._redibujar()
            self._agregar_numeros()

            return True

        if self._lado_actual is not None:
            # Desplazamiento del dedo en coordenadas de PANTALLA desde el
            # frame anterior (incremental, no desde el punto de inicio del
            # arrastre completo), para que el lado no salte de golpe si el
            # usuario mueve el dedo lejos.
            dx_pantalla = touch.x - self._pos_inicial_lado[0]
            dy_pantalla = touch.y - self._pos_inicial_lado[1]
            self._pos_inicial_lado = (touch.x, touch.y)

            # Convertir el desplazamiento a coordenadas de IMAGEN (eje Y
            # invertido entre pantalla e imagen, igual que en
            # coordenada_pantalla_a_imagen).
            dx_img = dx_pantalla / self._draw_width * self._img_ancho
            dy_img = -dy_pantalla / self._draw_height * self._img_alto

            idx_a = self._lado_actual
            idx_b = (self._lado_actual + 1) % 4

            for idx in (idx_a, idx_b):
                nuevo_x = float(max(0, min(self.esquinas[idx][0] + dx_img, self.imagen_ancho - 1)))
                nuevo_y = float(max(0, min(self.esquinas[idx][1] + dy_img, self.imagen_alto - 1)))
                self.esquinas[idx] = [nuevo_x, nuevo_y]

            self._redibujar()
            self._agregar_numeros()
            return True

        return super().on_touch_move(touch)

    def on_touch_up(self, touch):
        """Finalizar arrastre."""
        if self._punto_actual is not None:
            self._punto_actual = None
            self._pos_inicial = None
            return True
        if self._lado_actual is not None:
            self._lado_actual = None
            self._pos_inicial_lado = None
            return True
        return super().on_touch_up(touch)


class FilaHistorial(ButtonBehavior, BoxLayout):
    """Fila tocable del historial de PDFs, con fondo oscuro dibujado
    con gráficos de Kivy (sin asset externo)."""
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        with self.canvas.before:
            Color(0.25, 0.25, 0.25, 1)
            self._bg_rect = Rectangle(pos=self.pos, size=self.size)
        self.bind(pos=self._actualizar_bg, size=self._actualizar_bg)

    def _actualizar_bg(self, *args):
        self._bg_rect.pos = self.pos
        self._bg_rect.size = self.size


class MiniaturaTocable(ButtonBehavior, BoxLayout):
    """Envoltorio tocable para la miniatura de una foto en la lista,
    sin alterar el layout de la fila (mismo tamaño que el KivyImage)."""
    pass


class PopupRevisarEscaneo(Popup):
    """Popup para revisar una imagen escaneada antes de aceptarla."""

    def __init__(self, imagen_escaneada_ruta, imagen_original_ruta, esquinas_detectadas, app, **kwargs):
        super().__init__(**kwargs)
        self.imagen_escaneada_ruta = imagen_escaneada_ruta
        self.imagen_original_ruta = imagen_original_ruta
        self.esquinas_detectadas = esquinas_detectadas
        self.app = app
        self.auto_dismiss = False  # El popup no se cierra solo
        self.on_cancelar = lambda: None  # valor por defecto, por si nadie lo asigna

        self.title = "Revisar imagen escaneada"
        self.size_hint = (0.9, 0.85)

        self._build_contenido()

    def _build_contenido(self):
        """Construir el contenido del popup."""
        layout = BoxLayout(orientation='vertical', spacing=10, padding=10)

        # Preview de la imagen escaneada
        preview_layout = BoxLayout(orientation='vertical', size_hint_y=0.7, spacing=5)

        lbl_preview = Label(
            text="Vista previa (ya escaneada)",
            font_size='12sp',
            color=(0.7, 0.7, 0.7, 1),
            size_hint_y=None,
            height='20dp'
        )
        preview_layout.add_widget(lbl_preview)

        self.imagen_preview = KivyImage(
            source=self.imagen_escaneada_ruta,
            allow_stretch=True,
            keep_ratio=True
        )
        preview_layout.add_widget(self.imagen_preview)
        layout.add_widget(preview_layout)

        # Botones de acción
        btn_layout = BoxLayout(spacing=8, size_hint_y=None, height='50dp')

        btn_ajustar = Button(text="Ajustar bordes", background_color=(0.8, 0.6, 0.2, 1))
        btn_ajustar.bind(on_press=self._abrir_ajuste)
        btn_layout.add_widget(btn_ajustar)

        btn_usar = Button(text="Usar esta", background_color=(0.18, 0.6, 0.95, 1))
        btn_usar.bind(on_press=self._usar_escaneo)
        btn_layout.add_widget(btn_usar)

        btn_cancelar = Button(text="Cancelar", background_color=(0.7, 0.3, 0.3, 1))
        btn_cancelar.bind(on_press=self._cancelar_revision)
        btn_layout.add_widget(btn_cancelar)

        layout.add_widget(btn_layout)
        self.content = layout

    def _cancelar_revision(self, instance):
        """Cancelar la revisión de esta foto y de todas las pendientes del lote."""
        self.dismiss()
        self.on_cancelar()

    def _abrir_ajuste(self, instance):
        """Abrir pantalla para ajustar bordes manualmente."""
        self.dismiss()

        # Crear el popup de ajuste
        popup_ajuste = Popup(
            title="Ajustar esquinas manualmente",
            size_hint=(0.95, 0.95),
            auto_dismiss=False
        )

        # Crear el widget de ajuste
        widget_ajuste = WidgetAjusteEsquinas(
            self.imagen_original_ruta,
            self.esquinas_detectadas,
            lambda esquinas: self._confirmar_ajuste(esquinas, popup_ajuste)
        )

        # Botones del popup de ajuste
        btn_layout = BoxLayout(size_hint_y=None, height='50dp', spacing=8)

        btn_cancelar = Button(text="Cancelar", background_color=(0.7, 0.3, 0.3, 1))
        btn_cancelar.bind(on_press=lambda _: self._cancelar_ajuste(popup_ajuste))
        btn_layout.add_widget(btn_cancelar)

        btn_guardar = Button(text="Guardar ajustes", background_color=(0.18, 0.6, 0.95, 1))
        btn_guardar.bind(on_press=lambda _: self._guardar_ajuste(widget_ajuste.esquinas, popup_ajuste))
        btn_layout.add_widget(btn_guardar)

        layout_principal = BoxLayout(orientation='vertical')
        layout_principal.add_widget(widget_ajuste)
        layout_principal.add_widget(btn_layout)

        popup_ajuste.content = layout_principal
        popup_ajuste.open()

    def _cancelar_ajuste(self, popup_ajuste):
        """Cancelar ajuste y volver al popup de revisión."""
        popup_ajuste.dismiss()
        # Reabrir el popup de revisión original
        self._build_contenido()
        self.open()

    def _guardar_ajuste(self, esquinas, popup_ajuste):
        """Guardar esquinas ajustadas y regenerar escaneo."""
        popup_ajuste.dismiss()

        # Regenerar el escaneo con las esquinas ajustadas
        Clock.schedule_once(
            lambda *_: self._regenerar_escaneo(esquinas),
            0.1
        )

    def _regenerar_escaneo(self, esquinas):
        """Regenerar el escaneo con esquinas manuales."""
        # Crear una nueva ruta de salida
        carpeta = os.path.dirname(self.imagen_escaneada_ruta)
        nueva_salida = os.path.join(carpeta, f"scan_manual_{uuid.uuid4().hex[:10]}.jpg")

        try:
            info = escanear(
                self.imagen_original_ruta,
                nueva_salida,
                modo="color",
                recortar=True,
                esquinas=esquinas
            )

            # Actualizar referencias
            self.imagen_escaneada_ruta = nueva_salida
            self.imagen_preview.source = nueva_salida
            self.imagen_preview.reload()

            # Guardar las esquinas ajustadas
            self.esquinas_ajustadas = esquinas

            # Continuar con el flujo: agregar la foto a la lista y procesar la siguiente
            # Esto es lo mismo que hace "Usar esta" pero sin el dismiss() porque ya se cerró el popup de ajuste
            Clock.schedule_once(lambda *_: self._usar_escaneo(None), 0.1)

        except Exception as e:
            self.app._popup("Error", f"Error al regenerar escaneo:\n{e}")

    def _confirmar_ajuste(self, esquinas, popup):
        """Confirmar ajuste (callback desde WidgetAjusteEsquinas)."""
        popup.dismiss()
        self._guardar_ajuste(esquinas)

    def _usar_escaneo(self, instance):
        """Aceptar el escaneo y continuar con la siguiente imagen."""
        self.dismiss()
        # Devolver las esquinas usadas (ajustadas o detectadas)
        if hasattr(self, 'esquinas_ajustadas'):
            self.on_confirmar(self.esquinas_ajustadas)
        else:
            self.on_confirmar(self.esquinas_detectadas)


class ColumnaLluvia(Label):
    """Una columna individual de caracteres 0/1 cayendo, estilo "Matrix"."""

    def __init__(self, pos_x, alto_pantalla, **kwargs):
        super().__init__(**kwargs)
        self.alto_pantalla = alto_pantalla
        self.n_caracteres = random.randint(10, 18)
        self.font_size = "16sp"
        self.color = (0.13, 0.77, 0.37, random.uniform(0.5, 1.0))
        self.halign = "center"
        self.valign = "top"
        self.size_hint = (None, None)
        self.width = 24
        self.text_size = (self.width, None)
        self.x = pos_x
        self.y = random.uniform(0, alto_pantalla)
        self._regenerar_texto()
        self.bind(texture_size=self._on_texture_size)

    def _regenerar_texto(self):
        self.text = "\n".join(random.choice("01") for _ in range(self.n_caracteres))

    def _on_texture_size(self, *_):
        self.height = self.texture_size[1]

    def caer(self, dt, velocidad):
        nueva_y = self.y - velocidad * dt
        if nueva_y + self.height < 0:
            # salió por abajo: reaparece arriba con texto nuevo
            nueva_y = self.alto_pantalla + random.uniform(0, 100)
            self.n_caracteres = random.randint(10, 18)
            self._regenerar_texto()
        self.y = nueva_y


class PantallaBienvenida(FloatLayout):
    """Pantalla de bienvenida animada tipo "Matrix": lluvia de 0/1 de fondo,
    logo con fade-in y texto con parpadeo. Se muestra DURACION_TOTAL
    segundos y luego llama a on_terminar() para que el contenedor la quite.

    IMPORTANTE: no usa kivy.graphics.Line en ningún punto (bug conocido de
    renderizado silencioso en algunos drivers Android), solo
    Rectangle/Image/Label, igual que el resto del proyecto.
    """

    DURACION_TOTAL = 5.0

    def __init__(self, on_terminar, **kwargs):
        super().__init__(**kwargs)
        self.on_terminar = on_terminar
        self._columnas = []
        self._evento_lluvia = None
        self._anim_texto = None
        self._contenido_creado = False

        with self.canvas.before:
            Color(0.02, 0.06, 0.03, 1)
            self._fondo_rect = Rectangle(pos=self.pos, size=self.size)
        self.bind(size=self._actualizar_fondo, pos=self._actualizar_fondo)

        # La lluvia y el logo necesitan el tamaño REAL del widget, que no
        # está garantizado en __init__ (puede valer (100, 100) por defecto
        # antes del primer layout). Se crea el contenido recién cuando el
        # tamaño ya es válido.
        self.bind(size=self._crear_contenido_si_hace_falta)
        Clock.schedule_once(self._crear_contenido_si_hace_falta, 0)

        Clock.schedule_once(self._cerrar, self.DURACION_TOTAL)

    def _actualizar_fondo(self, *_):
        self._fondo_rect.pos = self.pos
        self._fondo_rect.size = self.size

    def _crear_contenido_si_hace_falta(self, *_):
        if self._contenido_creado or self.width <= 1 or self.height <= 1:
            return
        self._contenido_creado = True

        # --- Lluvia Matrix ---
        n_columnas = 14
        paso = self.width / n_columnas
        for i in range(n_columnas):
            col = ColumnaLluvia(
                pos_x=i * paso + random.uniform(-10, 10),
                alto_pantalla=self.height,
            )
            self.add_widget(col)
            self._columnas.append(col)
        self._evento_lluvia = Clock.schedule_interval(self._animar_lluvia, 1 / 20)

        # --- Logo (icon.png, el mismo de la app; NO se redibuja a mano) ---
        self.logo = KivyImage(
            source="icon.png",
            size_hint=(None, None),
            size=(180, 180),
            pos_hint={"center_x": 0.5, "center_y": 0.58},
            opacity=0,
        )
        self.add_widget(self.logo)

        # --- Texto, debajo del logo ---
        self.texto_titulo = Label(
            text="YESID_TECH",
            font_size="22sp",
            bold=True,
            color=(0.13, 0.9, 0.4, 1),
            size_hint=(None, None),
            size=(300, 30),
            pos_hint={"center_x": 0.5, "center_y": 0.42},
            opacity=0,
        )
        self.texto_subtitulo = Label(
            text="SOLUTIONS",
            font_size="14sp",
            color=(0.13, 0.7, 0.35, 1),
            size_hint=(None, None),
            size=(300, 24),
            pos_hint={"center_x": 0.5, "center_y": 0.37},
            opacity=0,
        )
        self.add_widget(self.texto_titulo)
        self.add_widget(self.texto_subtitulo)

        Clock.schedule_once(self._iniciar_fade_logo, 0.3)

    def _animar_lluvia(self, dt):
        for col in self._columnas:
            col.caer(dt, velocidad=140)

    def _iniciar_fade_logo(self, *_):
        anim = Animation(opacity=1, duration=1.0)
        anim.bind(on_complete=lambda *_: self._iniciar_parpadeo_texto())
        anim.start(self.logo)

    def _iniciar_parpadeo_texto(self):
        self.texto_titulo.opacity = 1
        self.texto_subtitulo.opacity = 1
        parpadeo = Animation(opacity=0.2, duration=0.3) + Animation(opacity=1, duration=0.3)
        parpadeo.repeat = True
        self._anim_texto = parpadeo
        parpadeo.start(self.texto_titulo)
        parpadeo.start(self.texto_subtitulo)

    def _cerrar(self, *_):
        if self._evento_lluvia is not None:
            self._evento_lluvia.cancel()
        if self._anim_texto is not None:
            self._anim_texto.cancel(self.texto_titulo)
            self._anim_texto.cancel(self.texto_subtitulo)
        if self.on_terminar:
            self.on_terminar()


class ImageToPDFApp(App):
    title = "Imágenes a PDF"

    def build(self):
        # En Android, el teclado tapa el popup si la ventana no se reacomoda.
        # 'below_target' sube la vista justo lo necesario para que el campo
        # que estás editando (el TextInput enfocado) quede siempre visible.
        Window.softinput_mode = "below_target"

        self.ordered_images = []  # lista ordenada por el usuario
        self.last_pdf_uri = None  # Android: Uri de MediaStore; escritorio: ruta del PDF
        self.fotos_pendientes = []  # cola de fotos por escanear
        self.foto_actual_en_revision = None  # (ruta_original, ruta_escaneada, esquinas)
        # clave: ruta actual en self.ordered_images (la que se ve en la lista)
        # valor: {"original": ruta_original_sin_procesar, "esquinas": esquinas_o_None}
        self.fotos_necesitan_revision = {}
        # clave: ruta actual en self.ordered_images; valor: {"original":, "esquinas":}
        # para TODAS las fotos ya escaneadas (no solo las que necesitan revisión),
        # así cualquier miniatura se puede reabrir en el editor de ajuste.
        self.fotos_info = {}
        self.btn_generate_disabled_state = False
        self.historial_pdfs_path = os.path.join(self.user_data_dir, "historial_pdfs.json")
        self.historial_pdfs = self._cargar_historial_pdfs()

        root = BoxLayout(orientation="vertical", padding=10, spacing=8)

        # Header
        header = Label(
            text="Imágenes a PDF",
            font_size="20sp",
            bold=True,
            size_hint_y=None,
            height="36dp",
        )
        root.add_widget(header)

        subtitle = Label(
            text="Selecciona imágenes, ordena y genera el PDF",
            font_size="13sp",
            color=(0.7, 0.7, 0.7, 1),
            size_hint_y=None,
            height="22dp",
        )
        root.add_widget(subtitle)

        # FileChooser solo en escritorio; en Android se usa el selector de fotos del sistema
        if platform != "android":
            self.file_chooser = FileChooserIconView(
                filters=["*.png", "*.jpg", "*.jpeg", "*.bmp", "*.gif", "*.webp"],
                path=get_default_save_dir(),
                multiselect=True,
                size_hint_y=0.55,
            )
            self.file_chooser.bind(selection=self._on_file_selection)
            root.add_widget(self.file_chooser)
        else:
            self.file_chooser = None

        # Barra de acciones sobre la selección
        actions = BoxLayout(size_hint_y=None, height="40dp", spacing=6)
        btn_add = Button(text="Añadir", size_hint_x=0.25)
        btn_add.bind(on_press=self._add_selected)
        btn_up = Button(text="Subir", size_hint_x=0.25)
        btn_up.bind(on_press=self._move_up)
        btn_down = Button(text="Bajar", size_hint_x=0.25)
        btn_down.bind(on_press=self._move_down)
        btn_remove = Button(text="Quitar", size_hint_x=0.25)
        btn_remove.bind(on_press=self._remove_highlighted)
        for b in (btn_add, btn_up, btn_down, btn_remove):
            actions.add_widget(b)
        root.add_widget(actions)

        # Lista ordenada (scroll). En Android ocupa todo el espacio libre.
        self.list_container = GridLayout(cols=1, spacing=4, size_hint_y=None)
        self.list_container.bind(minimum_height=self.list_container.setter("height"))
        self.lista_scroll = ScrollView(size_hint_y=0.35 if platform != "android" else 1)
        self.lista_scroll.add_widget(self.list_container)
        root.add_widget(self.lista_scroll)

        # Botones generar y compartir
        btn_layout = BoxLayout(size_hint_y=None, height="48dp", spacing=8)
        self.btn_generate = Button(
            text="Generar PDF",
            size_hint_x=0.5,
            size_hint_y=None,
            height="48dp",
            background_color=(0.18, 0.6, 0.95, 1),
        )
        self.btn_generate.bind(on_press=self._on_btn_generate_press)
        btn_layout.add_widget(self.btn_generate)
        self._actualizar_boton_generar()  # arranca deshabilitado: aún no hay imágenes

        self.btn_share = Button(
            text="Compartir",
            size_hint_x=0.5,
            size_hint_y=None,
            height="48dp",
            background_color=(0.18, 0.6, 0.95, 1),
        )
        self.btn_share.bind(on_press=self._share_pdf)
        btn_layout.add_widget(self.btn_share)
        root.add_widget(btn_layout)
        self._actualizar_boton_compartir()  # arranca deshabilitado: aún no hay PDF

        # Estado
        self.status = Label(
            text="0 imágenes en cola",
            size_hint_y=None,
            height="22dp",
            font_size="12sp",
        )
        root.add_widget(self.status)

        # selección interna para mover/quitar (índice resaltado)
        self.highlighted_index = None

        # Escuchar la respuesta del selector de fotos de Android
        if platform == "android":
            from android.activity import bind  # type: ignore
            bind(on_activity_result=self._on_activity_result)

        Clock.schedule_once(lambda *_: self._refresh_list(), 0.1)

        # Pantalla de bienvenida animada (logo + texto + lluvia "Matrix")
        # por encima de la interfaz principal durante unos segundos.
        contenedor = FloatLayout()
        root.size_hint = (1, 1)
        root.pos_hint = {"x": 0, "y": 0}
        contenedor.add_widget(root)

        splash = PantallaBienvenida(on_terminar=lambda: contenedor.remove_widget(splash))
        splash.size_hint = (1, 1)
        splash.pos_hint = {"x": 0, "y": 0}
        contenedor.add_widget(splash)

        return contenedor

    # ── selección del FileChooser (solo en escritorio) ──
    def _on_file_selection(self, instance, selection):
        # solo informativo, el usuario debe pulsar Añadir para confirmar orden
        pass

    def _add_selected(self, *_):
        if platform == "android":
            self._open_system_picker()
            return
        nuevas = []
        for p in list(self.file_chooser.selection):
            if p not in self.ordered_images and os.path.isfile(p):
                # validar que sea imagen legible
                try:
                    Image.open(p).verify()
                except Exception:
                    continue
                nuevas.append(p)
        if nuevas:
            self.file_chooser.selection = []
            self._escanear_y_anadir(nuevas)

    # ── selector de fotos de Android ──
    def _open_system_picker(self):
        # El Photo Picker moderno de Android 13+ (ACTION_OPEN_DOCUMENT) NO requiere
        # permisos de almacenamiento. Se solicita directamente sin verificar permisos.
        self._open_picker_with_intent()

    def _open_picker_with_intent(self):
        """Abrir el selector de fotos usando intents de Android."""
        try:
            from jnius import autoclass  # type: ignore
            PythonActivity = autoclass('org.kivy.android.PythonActivity')
            Intent = autoclass('android.content.Intent')
            activity = PythonActivity.mActivity
            Logger.info("main.py: Abriendo selector de fotos")

            # NOTA: se probó MediaStore.ACTION_PICK_IMAGES (el Photo Picker moderno,
            # tipo "carrete") para ir directo a la galería, pero algo en esa llamada
            # bloqueaba la app antes de poder caer al método de respaldo. Se revirtió
            # para no arriesgar el flujo que ya funciona; se puede retomar más adelante
            # con más tiempo para probarlo con logcat a mano.

            # METHOD 1: ACTION_OPEN_DOCUMENT (explorador de archivos genérico, Android 4.4+)
            try:
                intent = Intent(Intent.ACTION_OPEN_DOCUMENT)
                intent.setType("image/*")
                intent.addCategory(Intent.CATEGORY_OPENABLE)
                intent.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, True)
                Logger.info("main.py: Intent ACTION_OPEN_DOCUMENT enviado")
                activity.startActivityForResult(intent, REQUEST_PICK_IMAGES)
                return
            except Exception as e1:
                Logger.error(f"main.py: METHOD 1 failed: {e1}")

            # METHOD 2: ACTION_GET_CONTENT (fallback)
            try:
                intent = Intent(Intent.ACTION_GET_CONTENT)
                intent.setType("image/*")
                intent.addCategory(Intent.CATEGORY_OPENABLE)
                intent.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, True)
                Logger.info("main.py: Intent ACTION_GET_CONTENT enviado")
                activity.startActivityForResult(intent, REQUEST_PICK_IMAGES)
                return
            except Exception as e2:
                Logger.error(f"main.py: METHOD 2 failed: {e2}")

            # Si todos fallan, mostrar error
            raise Exception("No se pudo abrir el selector de fotos")
        except Exception as e:
            Logger.error(f"ERROR in _open_system_picker: {e}")
            import traceback
            Logger.error(traceback.format_exc())
            self._popup("Error", f"No se pudo abrir el selector de imágenes:\n{e}")

    def _on_activity_result(self, requestCode, resultCode, intent):
        # OJO: esto lo llama Android desde SU hilo, no desde el de Kivy.
        # Nada de tocar la interfaz aquí: todo lo visual va por Clock.schedule_once.
        if requestCode != REQUEST_PICK_IMAGES:
            return
        if resultCode != RESULT_OK or intent is None:
            return  # el usuario canceló
        try:
            uris = []
            clip = intent.getClipData()  # varias fotos
            if clip is not None:
                for i in range(clip.getItemCount()):
                    uris.append(clip.getItemAt(i).getUri())
            else:
                uri = intent.getData()  # una sola foto
                if uri is not None:
                    uris.append(uri)

            paths = []
            for uri in uris:
                p = self._copy_uri_to_cache(uri)
                if p:
                    paths.append(p)
            total = len(uris)
        except Exception as e:
            msg = str(e)
            Clock.schedule_once(
                lambda *_: self._popup("Error", f"Error al procesar las imágenes:\n{msg}"), 0
            )
            return
        Clock.schedule_once(lambda *_: self._on_images_picked(paths, total), 0)

    def _on_images_picked(self, paths, total):
        if paths:
            self._escanear_y_anadir(paths)
        failed = total - len(paths)
        if failed:
            self._popup(
                "Aviso",
                f"{failed} imagen(es) no se pudieron leer.\n"
                "Si son fotos HEIC/HEIF, cámbialas a JPG.",
            )

    # ── escaneo automático con revisión manual ──
    def _escanear_y_anadir(self, rutas):
        """Escanea cada foto (recorte, perspectiva, limpieza) en segundo plano y la
        añade a la lista tras la revisión del usuario. Si el escáner no está o falla,
        se añade la foto original."""
        if escanear is None:
            self.ordered_images.extend(rutas)
            self._invalidar_pdf_generado()
            self._refresh_list()
            self.status.text = f"Se añadieron {len(rutas)} imágenes"
            return
        self.btn_generate_disabled_state = self.btn_generate.disabled
        self.btn_generate.disabled = True
        self.fotos_pendientes = list(rutas)
        self.tamano_lote_actual = len(rutas)
        self.aceptadas_lote_actual = 0
        self.status.text = f"Escaneando 1 de {len(rutas)}..."
        Clock.schedule_once(lambda *_: self._procesar_siguiente_foto(), 0.1)

    def _procesar_siguiente_foto(self, *_):
        """Procesar la siguiente foto de la cola."""
        if not self.fotos_pendientes:
            # Ya no hay más fotos por escanear
            Clock.schedule_once(lambda *_: self._on_escaneo_terminado_final(), 0)
            return

        ruta = self.fotos_pendientes[0]
        Clock.schedule_once(
            lambda *_, i=len(self.ordered_images)+1: setattr(self.status, "text", f"Escaneando..."), 0)

        # Escanear en un hilo
        Thread(target=self._escanear_foto_individual, args=(ruta,), daemon=True).start()

    def _escanear_foto_individual(self, ruta):
        """Escanea una sola foto y programa la revisión en el hilo principal."""
        try:
            if platform == "android":
                carpeta = _android_cache_dir()
            else:
                carpeta = os.path.join(tempfile.gettempdir(), "imagenes_pdf_escaneos")
                os.makedirs(carpeta, exist_ok=True)

            salida = os.path.join(carpeta, f"scan_{uuid.uuid4().hex[:10]}.jpg")
            info = escanear(ruta, salida, modo="color")

            # Escaneo automático por lotes: agregar directo, sin popup por foto
            # (se marca para revisión manual posterior si no se detectaron bordes).
            Clock.schedule_once(lambda *_: self._on_foto_escaneada(ruta, info), 0)

        except Exception as e:
            Logger.error(f"Error escaneando {ruta}: {e}")
            # En caso de error, usar la original tal cual y marcarla para revisión
            info_fallback = {"ruta": ruta, "detectado": False, "esquinas": None}
            Clock.schedule_once(lambda *_: self._on_foto_escaneada(ruta, info_fallback), 0)

    def _on_foto_escaneada(self, ruta_original, info):
        """Se llama en el hilo principal cuando terminó de escanearse una
        foto del lote. Agrega automáticamente el resultado a la cola, sin
        popup. Si no se detectaron bordes confiables, la marca para revisión
        manual posterior en vez de interrumpir el lote."""
        path = info["ruta"]
        self.ordered_images.append(path)

        # Se guarda SIEMPRE, se haya detectado bien o no, para que cualquier
        # foto pueda reabrirse en el editor de ajuste de esquinas desde su miniatura.
        self.fotos_info[path] = {
            "original": ruta_original,
            "esquinas": info.get("esquinas"),
        }

        if not info.get("detectado", False):
            self.fotos_necesitan_revision[path] = {
                "original": ruta_original,
                "esquinas": info.get("esquinas"),
            }

        self.aceptadas_lote_actual = getattr(self, "aceptadas_lote_actual", 0) + 1
        self._invalidar_pdf_generado()
        self._refresh_list()

        # Sacar la foto procesada de la cola y seguir con la siguiente,
        # sin esperar a que el usuario confirme nada.
        if self.fotos_pendientes:
            self.fotos_pendientes.pop(0)
        if self.fotos_pendientes:
            self._procesar_siguiente_foto()
        else:
            self._on_escaneo_terminado_final()

    def _mostrar_popup_revision(self):
        """Mostrar el popup de revisión con la foto escaneada."""
        if self.foto_actual_en_revision is None:
            return

        ruta_original, ruta_escaneada, info = self.foto_actual_en_revision

        # Crear el popup de revisión
        self.popup_revision = PopupRevisarEscaneo(
            imagen_escaneada_ruta=ruta_escaneada,
            imagen_original_ruta=ruta_original,
            esquinas_detectadas=info["esquinas"] if info else None,
            app=self
        )
        # Guardar referencia de callback
        self.popup_revision.on_confirmar = lambda esquinas: self._on_revision_terminada(
            ruta_original, ruta_escaneada, esquinas
        )
        self.popup_revision.on_cancelar = self._cancelar_lote_pendiente
        self.popup_revision.open()

    def _cancelar_lote_pendiente(self):
        """El usuario canceló la revisión: descartar la foto actual y todas
        las pendientes del lote, sin tocar las que ya se habían agregado."""
        n_agregadas = getattr(self, "aceptadas_lote_actual", 0)
        m_total = getattr(self, "tamano_lote_actual", n_agregadas)

        self.fotos_pendientes = []
        self.foto_actual_en_revision = None
        self.popup_revision = None
        self.btn_generate.disabled = self.btn_generate_disabled_state
        if n_agregadas > 0:
            self._invalidar_pdf_generado()
        self._refresh_list()
        self.status.text = f"Cancelado: se agregaron {n_agregadas} de {m_total} imágenes"

    def _on_revision_terminada(self, ruta_original, ruta_escaneada, esquinas_usadas=None):
        """Callback cuando el usuario acepta o ajusta la foto."""
        if self.foto_actual_en_revision is None:
            return

        ruta_original, ruta_escaneada, info = self.foto_actual_en_revision

        if esquinas_usadas is not None and ruta_escaneada is not None:
            # El usuario ajustó las esquinas - regenerar escaneo
            carpeta = os.path.dirname(ruta_escaneada)
            nueva_salida = os.path.join(carpeta, f"scan_manual_{uuid.uuid4().hex[:10]}.jpg")
            try:
                info = escanear(
                    ruta_original,
                    nueva_salida,
                    modo="color",
                    recortar=True,
                    esquinas=esquinas_usadas
                )
                ruta_escaneada = nueva_salida
            except Exception as e:
                Logger.error(f"Error regenerando escaneo: {e}")
                ruta_escaneada = ruta_original
        elif ruta_escaneada is None:
            # El escaneo falló o no se aplicó, usar la original
            ruta_escaneada = ruta_original

        # Agregar la foto a la lista
        self.ordered_images.append(ruta_escaneada)
        self.aceptadas_lote_actual = getattr(self, "aceptadas_lote_actual", 0) + 1
        self._invalidar_pdf_generado()
        self._refresh_list()

        # Sacar la foto procesada de la cola y seguir con la siguiente
        self.fotos_pendientes.pop(0)
        self.foto_actual_en_revision = None

        # Mostrar el popup de la siguiente foto después de un breve delay
        if self.fotos_pendientes:
            Clock.schedule_once(lambda *_: self._procesar_siguiente_foto(), 0.5)
        else:
            # Ya no hay más fotos
            Clock.schedule_once(lambda *_: self._on_escaneo_terminado_final(), 0.5)

    def _on_escaneo_terminado_final(self):
        """Finalizar el proceso de escaneo de todas las fotos."""
        self.btn_generate.disabled = self.btn_generate_disabled_state
        # fotos_pendientes ya está vacía aquí, así que esto deja el botón
        # reflejando el estado real (hay imágenes o no), sin arrastrar el
        # valor guardado antes de empezar el escaneo.
        self._actualizar_boton_generar()
        n_revisar = len(self.fotos_necesitan_revision)
        if n_revisar:
            msg = (f"Se añadieron {len(self.ordered_images)} imágenes "
                   f"({n_revisar} necesitan revisión manual — marcadas en naranja)")
        else:
            msg = f"Se añadieron {len(self.ordered_images)} imágenes"
        self.status.text = msg

    # ── copiar URI a caché (Android) ──
    def _copy_uri_to_cache(self, uri):
        """Copia una imagen content:// a la caché privada. Devuelve la ruta o None."""
        try:
            resolver = _android_activity().getContentResolver()
            mime = resolver.getType(uri) or ""
            ext = ".jpg"
            for key, e in (("png", ".png"), ("gif", ".gif"), ("webp", ".webp"), ("bmp", ".bmp")):
                if key in mime:
                    ext = e
                    break
            name = f"img_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}{ext}"
            dest = os.path.join(_android_cache_dir(), name)

            # openFileDescriptor + detachFd da un descriptor de archivo normal,
            # así Python copia los bytes directamente (sin arrays de Java).
            pfd = resolver.openFileDescriptor(uri, "r")
            fd = pfd.detachFd()
            with os.fdopen(fd, "rb") as src, open(dest, "wb") as dst:
                shutil.copyfileobj(src, dst)
            pfd.close()

            # comprobar que Pillow puede leerla
            try:
                with Image.open(dest) as im:
                    im.verify()
            except Exception:
                os.remove(dest)
                return None
            return dest
        except Exception as e:
            print(f"Error copiando imagen: {e}")
            return None

    # ── historial de PDFs generados (persistido en user_data_dir) ──
    def _cargar_historial_pdfs(self):
        """Carga el historial de PDFs generados desde el JSON local.
        Si no existe o está corrupto, devuelve lista vacía sin crashear."""
        try:
            with open(self.historial_pdfs_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        return []

    def _guardar_historial_pdfs(self):
        """Persiste el historial (máximo 20 registros) al JSON local."""
        try:
            with open(self.historial_pdfs_path, "w", encoding="utf-8") as f:
                json.dump(self.historial_pdfs[:20], f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _registrar_pdf_en_historial(self, nombre_archivo, uri_o_ruta):
        """Agrega un PDF recién generado al principio del historial y
        refresca la pantalla de inicio (si está vacía) para mostrarlo."""
        # En Android, uri_o_ruta es un objeto Java android.net.Uri; su propio
        # método .toString() da el texto real de la URI (content://...).
        # En escritorio, uri_o_ruta ya es un string de Python (una ruta), y
        # los strings de Python no tienen método .toString(), así que ahí se
        # usa directamente tal cual.
        if hasattr(uri_o_ruta, "toString"):
            uri_texto = uri_o_ruta.toString()
        else:
            uri_texto = str(uri_o_ruta)

        registro = {
            "nombre": nombre_archivo,
            "uri": uri_texto,
            "fecha": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        # Si ya había una entrada con este mismo nombre (p. ej. se reemplazó
        # un PDF existente en Descargas), quitarla primero para no dejar dos
        # filas apuntando al mismo archivo con fechas distintas y confusas.
        self.historial_pdfs = [r for r in self.historial_pdfs if r.get("nombre") != nombre_archivo]
        self.historial_pdfs.insert(0, registro)
        self.historial_pdfs = self.historial_pdfs[:20]
        self._guardar_historial_pdfs()
        if not self.ordered_images:
            self._refresh_list()

    def _mostrar_historial_pdfs(self):
        """Muestra la lista de PDFs generados anteriormente, dentro del
        mismo list_container con scroll que ya usa la lista de imágenes."""
        titulo = Label(
            text="PDFs generados anteriormente",
            font_size="13sp",
            bold=True,
            color=(0.7, 0.7, 0.7, 1),
            size_hint_y=None,
            height="30dp",
        )
        self.list_container.add_widget(titulo)

        for registro in self.historial_pdfs:
            fila = FilaHistorial(
                orientation="horizontal",
                size_hint_y=None,
                height="56dp",
                padding=("8dp", "6dp"),
                spacing="10dp",
            )

            icono_container = FloatLayout(size_hint=(None, None), size=("36dp", "44dp"))
            self._dibujar_icono_pdf(icono_container)
            icono_container.bind(
                pos=lambda inst, val, ic=icono_container: self._dibujar_icono_pdf(ic),
                size=lambda inst, val, ic=icono_container: self._dibujar_icono_pdf(ic),
            )
            fila.add_widget(icono_container)

            columna_texto = BoxLayout(orientation="vertical", spacing="2dp")
            lbl_nombre = Label(
                text=registro["nombre"],
                font_size="12sp",
                halign="left",
                valign="middle",
                size_hint_y=None,
                height="26dp",
            )
            lbl_nombre.bind(size=lambda inst, val: setattr(inst, "text_size", inst.size))
            lbl_fecha = Label(
                text=registro["fecha"],
                font_size="11sp",
                color=(0.7, 0.7, 0.7, 1),
                halign="left",
                valign="middle",
                size_hint_y=None,
                height="22dp",
            )
            lbl_fecha.bind(size=lambda inst, val: setattr(inst, "text_size", inst.size))
            columna_texto.add_widget(lbl_nombre)
            columna_texto.add_widget(lbl_fecha)
            fila.add_widget(columna_texto)

            fila.bind(on_press=lambda inst, r=registro: self._abrir_pdf_historial(r))
            self.list_container.add_widget(fila)

    def _dibujar_icono_pdf(self, icono_container):
        """Dibuja una insignia tipo documento PDF: cuadrado rojo con esquinas
        redondeadas, esquina superior derecha doblada, y texto 'PDF' centrado."""
        icono_container.canvas.before.clear()
        x, y = icono_container.pos
        w, h = icono_container.size
        doblez = min(w, h) * 0.28  # tamaño del triángulo de la esquina doblada

        with icono_container.canvas.before:
            # Cuerpo principal: rojo con esquinas redondeadas
            Color(0.80, 0.16, 0.16, 1)
            RoundedRectangle(pos=(x, y), size=(w, h), radius=[(6, 6), (6, 6), (6, 6), (6, 6)])

            # Esquina doblada (triángulo en la esquina superior derecha,
            # en un rojo más oscuro para simular la "hojita" doblada)
            Color(0.58, 0.10, 0.10, 1)
            Triangle(points=[
                x + w - doblez, y + h,      # punto superior, a la izquierda de la esquina
                x + w, y + h,                # esquina superior derecha
                x + w, y + h - doblez,       # punto a la derecha, hacia abajo
            ])

        # Reposicionar/crear el label "PDF" centrado sobre el cuerpo,
        # ligeramente desplazado hacia abajo para no chocar con el doblez.
        if not hasattr(icono_container, "_label_pdf"):
            label = Label(
                text="PDF",
                font_size="11sp",
                bold=True,
                color=(1, 1, 1, 1),
                size_hint=(None, None),
            )
            icono_container._label_pdf = label
            icono_container.add_widget(label)
        else:
            label = icono_container._label_pdf

        label.size = (w, h * 0.5)
        label.pos = (x, y)
        label.text_size = label.size
        label.halign = "center"
        label.valign = "middle"

    def _abrir_pdf_historial(self, registro):
        if platform == "android":
            self._abrir_pdf_android(registro)
        else:
            self._abrir_pdf_escritorio(registro)

    def _abrir_pdf_android(self, registro):
        try:
            from jnius import autoclass, cast  # type: ignore
            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            Intent = autoclass("android.content.Intent")
            Uri = autoclass("android.net.Uri")
            String = autoclass("java.lang.String")

            uri = Uri.parse(registro["uri"])
            intent = Intent(Intent.ACTION_VIEW)
            intent.setDataAndType(uri, "application/pdf")
            intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)

            # pyjnius no resuelve el overload de createChooser si se le pasa un
            # str de Python directo; hay que convertirlo a CharSequence de Java
            # (mismo patrón que ya usa _share_pdf más abajo).
            titulo = cast("java.lang.CharSequence", String("Abrir PDF con"))
            chooser = Intent.createChooser(intent, titulo)
            PythonActivity.mActivity.startActivity(chooser)
        except Exception as e:
            import traceback
            Logger.error(f"main.py: _abrir_pdf_android falló: {e}")
            Logger.error(traceback.format_exc())
            self._mostrar_popup_archivo_no_existe()

    def _abrir_pdf_escritorio(self, registro):
        ruta = registro["uri"]
        if not os.path.exists(ruta):
            self._mostrar_popup_archivo_no_existe()
            return
        self._popup("PDF generado", ruta)

    def _mostrar_popup_archivo_no_existe(self):
        self._popup("Aviso", "El archivo ya no existe")

    # ── botón Generar PDF: solo activo si hay imágenes y no hay un escaneo en curso ──
    def _actualizar_boton_generar(self):
        """Si ya hay un PDF generado y vigente para la lista actual, el botón
        se convierte en 'Regresar' (vuelve a la pantalla de inicio). Si no,
        se comporta como 'Generar PDF' tal como hasta ahora (habilitado solo
        si hay al menos una imagen y no hay un escaneo de lote en curso)."""
        escaneando = bool(getattr(self, "fotos_pendientes", None))

        if escaneando:
            self.btn_generate.text = "Generar PDF"
            self.btn_generate.disabled = True
            self.btn_generate.background_color = (0.4, 0.4, 0.4, 1)
            return

        if self.last_pdf_uri is not None:
            self.btn_generate.text = "Regresar"
            self.btn_generate.disabled = False
            self.btn_generate.background_color = (0.35, 0.35, 0.35, 1)
            return

        hay_imagenes = bool(self.ordered_images)
        self.btn_generate.text = "Generar PDF"
        self.btn_generate.disabled = not hay_imagenes
        self.btn_generate.background_color = (
            (0.18, 0.6, 0.95, 1) if hay_imagenes else (0.4, 0.4, 0.4, 1)
        )

    def _on_btn_generate_press(self, *_):
        if self.last_pdf_uri is not None:
            self._volver_al_inicio()
        else:
            self._ask_filename()

    def _volver_al_inicio(self, *_):
        """Limpia la lista de fotos actual (su PDF ya se generó) para volver
        a la pantalla de inicio, donde se ve el historial de PDFs generados
        (ya se muestra automáticamente cuando la lista está vacía)."""
        self.ordered_images = []
        self.highlighted_index = None
        self.fotos_necesitan_revision = {}
        self.fotos_info = {}
        self._invalidar_pdf_generado()  # pone last_pdf_uri en None, desactiva "Compartir"
        self._refresh_list()            # repinta: lista vacía + historial + botón vuelve a "Generar PDF"

    # ── botón Compartir: solo activo si hay un PDF ya generado y vigente ──
    def _actualizar_boton_compartir(self):
        """Habilita/colorea 'Compartir' solo cuando existe un PDF listo.
        Si no hay PDF (o quedó desactualizado por un cambio en las imágenes),
        se deja inhabilitado y en gris."""
        listo = self.last_pdf_uri is not None
        self.btn_share.disabled = not listo
        self.btn_share.background_color = (0.18, 0.6, 0.95, 1) if listo else (0.4, 0.4, 0.4, 1)

    def _invalidar_pdf_generado(self):
        """Llamar cada vez que cambie la lista de imágenes (añadir, quitar,
        reordenar): el PDF ya generado deja de corresponder a la lista actual."""
        self.last_pdf_uri = None
        self._actualizar_boton_compartir()

    # ── lista ──
    def _refresh_list(self):
        self.list_container.clear_widgets()
        if not self.ordered_images:
            self.list_container.add_widget(
                Label(text="Sin imágenes — pulsa Añadir", font_size="12sp", color=(0.6, 0.6, 0.6, 1), size_hint_y=None, height="30dp")
            )
            if self.historial_pdfs:
                self._mostrar_historial_pdfs()
            self.status.text = "0 imágenes en cola"
            self._actualizar_boton_generar()
            return

        for idx, path in enumerate(self.ordered_images):
            name = os.path.basename(path)
            is_hl = idx == self.highlighted_index
            row = BoxLayout(size_hint_y=None, height="64dp", spacing=8, padding=(4, 4))
            row.indice_actual = idx  # se recalcula en cada refresh, siempre consistente
            # resaltar la fila completa con un fondo si está seleccionada
            if is_hl:
                with row.canvas.before:
                    # Color y Rectangle ya están importados al inicio del
                    # archivo (línea 28); un import local aquí los convertía
                    # en variables locales de TODA la función _refresh_list,
                    # lo que rompía el otro bloque de resaltado (ámbar) de
                    # más abajo cuando esta fila no estaba seleccionada
                    # (UnboundLocalError: cannot access local variable 'Color').
                    Color(0.18, 0.6, 0.95, 0.18)
                    rect = Rectangle(pos=row.pos, size=row.size)
                row.bind(pos=lambda inst, val, r=rect: setattr(r, "pos", val))
                row.bind(size=lambda inst, val, r=rect: setattr(r, "size", val))

            # resaltar en ámbar las fotos que el escaneo automático no logró
            # detectar con confianza, para que el usuario note que conviene
            # revisarlas manualmente
            if path in self.fotos_necesitan_revision:
                with row.canvas.before:
                    Color(1, 0.6, 0, 0.25)
                    row._revision_rect = Rectangle(pos=row.pos, size=row.size)
                row.bind(pos=self._actualizar_rect_revision, size=self._actualizar_rect_revision)

            # miniatura de la imagen (ya escaneada / lista para el PDF);
            # tocable para reabrir el editor de ajuste de esquinas de esta foto
            thumb = KivyImage(
                source=path,
                size_hint=(None, None),
                size=("56dp", "56dp"),
                allow_stretch=True,
                keep_ratio=True,
            )
            thumb_tocable = MiniaturaTocable(size_hint=(None, None), size=("56dp", "56dp"))
            thumb_tocable.add_widget(thumb)
            thumb_tocable.bind(on_press=lambda inst, p=path: self._abrir_ajuste_desde_miniatura(p))
            row.add_widget(thumb_tocable)

            # columna con el número de orden y el nombre del archivo
            info = BoxLayout(orientation="vertical", spacing=2)
            top = Label(
                text=f"{idx+1}.",
                halign="left",
                valign="middle",
                font_size="13sp",
                bold=is_hl,
                size_hint_y=None,
                height="20dp",
                color=(0.18, 0.6, 0.95, 1) if is_hl else (1, 1, 1, 1),
            )
            top.bind(size=lambda inst, val: setattr(inst, "text_size", val))
            lbl = Label(text=name, halign="left", valign="middle", font_size="11sp", color=(0.6, 0.6, 0.6, 1))
            lbl.bind(size=lambda inst, val: setattr(inst, "text_size", val))
            info.add_widget(top)
            info.add_widget(lbl)
            row.add_widget(info)

            # botón para resaltar esta fila (y luego usar Subir/Bajar/Quitar)
            btn = Button(text="Sel." if is_hl else "-", size_hint_x=None, width="48dp", font_size="10sp")
            btn.bind(on_press=lambda _btn, i=idx: self._highlight(i))
            row.add_widget(btn)

            if path in self.fotos_necesitan_revision:
                boton_revisar = Button(
                    text="! Revisar",  # ASCII puro (el "⚠" no se ve en todas las fuentes)
                    font_size="11sp",
                    bold=True,
                    size_hint_x=None,
                    width="80dp",
                    background_color=(0.72, 0.56, 0.3, 1),  # ámbar suave (antes naranja puro)
                    background_normal="",
                )
                boton_revisar.bind(on_press=lambda inst, p=path: self._abrir_revision_manual(p))
                row.add_widget(boton_revisar)

            self.list_container.add_widget(row)

        self.status.text = f"{len(self.ordered_images)} imágenes en cola - toca '-' para seleccionar y usa Subir/Bajar"
        self._actualizar_boton_generar()

    def _actualizar_rect_revision(self, row, *args):
        if hasattr(row, "_revision_rect"):
            row._revision_rect.pos = row.pos
            row._revision_rect.size = row.size

    def _abrir_revision_manual(self, path):
        """Alias: el botón '! Revisar' usa el mismo editor que tocar
        cualquier miniatura, unificado en _abrir_ajuste_desde_miniatura."""
        self._abrir_ajuste_desde_miniatura(path)

    def _abrir_ajuste_desde_miniatura(self, path):
        """Abre el editor de ajuste de esquinas para CUALQUIER foto de la
        lista, esté marcada para revisión o no."""
        info = self.fotos_info.get(path)
        if info:
            original = info["original"]
            esquinas = info.get("esquinas")
        else:
            # No hay datos guardados (se agregó sin pasar por el escáner,
            # escanear is None): usar la propia ruta como "original" y sin
            # esquinas previas, el fallback ya esperado por
            # WidgetAjusteEsquinas cuando esquinas_detectadas es None.
            original = path
            esquinas = None

        def _on_guardado(nuevo_path, esquinas_usadas):
            if path in self.ordered_images:
                idx = self.ordered_images.index(path)
                self.ordered_images[idx] = nuevo_path
            self.fotos_necesitan_revision.pop(path, None)
            self.fotos_info.pop(path, None)
            # Guardar las esquinas que el usuario REALMENTE ajustó (no las
            # que tenía antes de abrir el editor), para que si vuelve a
            # tocar esta foto más adelante, el editor arranque donde quedó.
            self.fotos_info[nuevo_path] = {"original": original, "esquinas": esquinas_usadas}
            self._invalidar_pdf_generado()
            self._refresh_list()

        self._abrir_ajuste_manual(path, original, esquinas, _on_guardado)

    def _abrir_ajuste_manual(self, path_actual, ruta_original, esquinas_iniciales, on_guardado):
        """Reutiliza el mismo patrón de PopupRevisarEscaneo._abrir_ajuste:
        el botón 'Guardar ajustes' lee widget_ajuste.esquinas directamente
        (no hay getter ni evento on_confirmar real en WidgetAjusteEsquinas)."""
        popup_ajuste = Popup(
            title="Ajustar esquinas manualmente",
            size_hint=(0.85, 0.78),  # más pequeño que antes (era 0.95x0.95)
            auto_dismiss=False,
            background_color=(0.07, 0.07, 0.07, 1),  # fondo oscuro, acorde al resto de la app
            separator_color=(0.72, 0.56, 0.3, 1),
        )
        widget_ajuste = WidgetAjusteEsquinas(
            ruta_original,
            esquinas_iniciales,
            lambda esquinas: None  # on_confirmar real nunca se invoca internamente
        )

        def _cancelar(*_):
            popup_ajuste.dismiss()

        def _guardar(*_):
            esquinas = widget_ajuste.esquinas
            carpeta = os.path.dirname(path_actual)
            nueva_salida = os.path.join(carpeta, f"scan_manual_{uuid.uuid4().hex[:10]}.jpg")
            try:
                info_regen = escanear(
                    ruta_original,
                    nueva_salida,
                    modo="color",
                    recortar=True,
                    esquinas=esquinas
                )
                popup_ajuste.dismiss()
                on_guardado(info_regen["ruta"], esquinas)
            except Exception as e:
                self._popup("Error", f"Error al regenerar escaneo:\n{e}")

        btn_layout = BoxLayout(size_hint_y=None, height="50dp", spacing=8)
        btn_cancelar = Button(text="Cancelar", background_color=(0.7, 0.3, 0.3, 1))
        btn_cancelar.bind(on_press=_cancelar)
        btn_layout.add_widget(btn_cancelar)
        btn_guardar = Button(text="Guardar ajustes", background_color=(0.18, 0.6, 0.95, 1))
        btn_guardar.bind(on_press=_guardar)
        btn_layout.add_widget(btn_guardar)

        layout_principal = BoxLayout(orientation="vertical")
        layout_principal.add_widget(widget_ajuste)
        layout_principal.add_widget(btn_layout)
        popup_ajuste.content = layout_principal
        popup_ajuste.open()

    def _highlight(self, idx):
        self.highlighted_index = idx if self.highlighted_index != idx else None
        self._refresh_list()

    def _move_up(self, *_):
        i = self.highlighted_index
        if i is None or i == 0:
            return
        self.ordered_images[i - 1], self.ordered_images[i] = self.ordered_images[i], self.ordered_images[i - 1]
        self.highlighted_index = i - 1
        self._invalidar_pdf_generado()
        self._refresh_list()

    def _move_down(self, *_):
        i = self.highlighted_index
        if i is None or i >= len(self.ordered_images) - 1:
            return
        self.ordered_images[i + 1], self.ordered_images[i] = self.ordered_images[i], self.ordered_images[i + 1]
        self.highlighted_index = i + 1
        self._invalidar_pdf_generado()
        self._refresh_list()

    def _remove_highlighted(self, *_):
        i = self.highlighted_index
        if i is None:
            # si no hay resaltado, quitar la última
            if self.ordered_images:
                self.ordered_images.pop()
                self.highlighted_index = None
                self._invalidar_pdf_generado()
                self._refresh_list()
            return
        del self.ordered_images[i]
        self.highlighted_index = None
        self._invalidar_pdf_generado()
        self._refresh_list()

    # ── generación PDF ──
    def _ask_filename(self, *_, nombre_inicial=None):
        if nombre_inicial is None:
            nombre_inicial = "BOL_" + datetime.datetime.now().strftime("%d-%m-%y") + ".pdf"
        if not self.ordered_images:
            self._popup("Aviso", "Añade al menos una imagen antes de generar el PDF.")
            return

        box = BoxLayout(orientation="vertical", spacing=8, padding=10)
        box.add_widget(Label(text="Nombre del PDF:", size_hint_y=None, height="22dp"))
        ti = TextInput(text=nombre_inicial, multiline=False, size_hint_y=None, height="40dp")
        box.add_widget(ti)

        destino = "la carpeta Descargas del teléfono" if platform == "android" else get_default_save_dir()
        hint = Label(text=f"Se guardará en: {destino}", font_size="10sp", color=(0.6, 0.6, 0.6, 1), size_hint_y=None, height="18dp")
        box.add_widget(hint)

        btns = BoxLayout(size_hint_y=None, height="40dp", spacing=8)
        btn_cancel = Button(text="Cancelar")
        btn_ok = Button(text="Guardar", background_color=(0.18, 0.6, 0.95, 1))
        btns.add_widget(btn_cancel)
        btns.add_widget(btn_ok)
        box.add_widget(btns)

        popup = Popup(title="Guardar PDF", content=box, size_hint=(0.9, 0.45))
        btn_cancel.bind(on_press=popup.dismiss)
        btn_ok.bind(on_press=lambda *_: self._start_generation(ti.text.strip(), popup))
        popup.open()

    def _start_generation(self, filename, popup):
        if not filename:
            return
        if not filename.lower().endswith(".pdf"):
            filename += ".pdf"
        popup.dismiss()
        self.btn_generate.disabled = True
        self.btn_share.disabled = True

        if platform == "android":
            # En Android hay que comprobar primero si ya existe un archivo
            # con ese nombre en Descargas (MediaStore), antes de generar nada.
            self.status.text = "Comprobando nombre en Descargas..."
            Thread(target=self._verificar_y_generar_android, args=(filename,), daemon=True).start()
        else:
            self.status.text = "Generando PDF..."
            Thread(target=self._generate_in_background, args=(filename,), daemon=True).start()

    def _buscar_uri_en_descargas(self, filename):
        """Devuelve el Uri del archivo en Download/ con ese _display_name, o
        None si no existe. Se ejecuta en un hilo de fondo."""
        from jnius import autoclass  # type: ignore
        Downloads = autoclass("android.provider.MediaStore$Downloads")
        ContentUris = autoclass("android.content.ContentUris")

        resolver = _android_activity().getContentResolver()

        proyeccion = ["_id"]
        seleccion = "_display_name = ? AND relative_path = ?"
        args = [filename, "Download/"]

        cursor = resolver.query(
            Downloads.EXTERNAL_CONTENT_URI,
            proyeccion,
            seleccion,
            args,
            None,
        )
        if cursor is None:
            return None
        try:
            if cursor.getCount() == 0:
                return None
            cursor.moveToFirst()
            id_columna = cursor.getColumnIndexOrThrow("_id")
            id_archivo = cursor.getLong(id_columna)
            return ContentUris.withAppendedId(Downloads.EXTERNAL_CONTENT_URI, id_archivo)
        finally:
            cursor.close()

    def _verificar_y_generar_android(self, filename):
        """Corre en el hilo de fondo: comprueba el nombre en Descargas y,
        según el resultado, sigue con la generación o pide decidir al usuario."""
        try:
            uri_existente = self._buscar_uri_en_descargas(filename)
        except Exception as e:
            # Si la comprobación falla por algún motivo (p. ej. un problema
            # de conversión de tipos en pyjnius), no bloquear al usuario:
            # seguir con el flujo normal, tal como se comportaba antes de
            # este cambio.
            Logger.error(f"main.py: _buscar_uri_en_descargas falló, se ignora: {e}")
            uri_existente = None

        if uri_existente is not None:
            Clock.schedule_once(lambda *_: self._mostrar_aviso_duplicado(filename, uri_existente), 0)
        else:
            self._generate_in_background(filename)

    def _mostrar_aviso_duplicado(self, filename, uri_existente):
        # Se canceló la generación antes de empezar: reactivar los botones
        # tal como estaban antes de tocar "Generar PDF".
        self._actualizar_boton_generar()
        self._actualizar_boton_compartir()
        self.status.text = f"{len(self.ordered_images)} imágenes en cola - toca '-' para seleccionar y usa Subir/Bajar"

        contenido = BoxLayout(orientation="vertical", spacing=10, padding=10)
        contenido.add_widget(Label(
            text=f"Ya existe un PDF llamado '{filename}' en Descargas.",
            halign="center",
            valign="middle",
        ))
        botones = BoxLayout(size_hint_y=None, height="50dp", spacing=8)
        btn_reemplazar = Button(text="Reemplazar", font_size="12sp", background_color=(0.75, 0.35, 0.2, 1))
        btn_cambiar = Button(text="Cambiar nombre", font_size="12sp", background_color=(0.18, 0.6, 0.95, 1))
        btn_cancelar = Button(text="Cancelar", font_size="12sp", background_color=(0.7, 0.3, 0.3, 1))
        botones.add_widget(btn_reemplazar)
        botones.add_widget(btn_cambiar)
        botones.add_widget(btn_cancelar)
        contenido.add_widget(botones)

        popup_aviso = Popup(
            title="Nombre duplicado",
            content=contenido,
            size_hint=(0.85, 0.4),
            auto_dismiss=False,
        )

        def _reemplazar(*_):
            popup_aviso.dismiss()
            self.status.text = "Generando PDF..."
            Thread(
                target=self._generate_in_background,
                args=(filename,),
                kwargs={"uri_existente": uri_existente},
                daemon=True,
            ).start()

        def _cambiar(*_):
            popup_aviso.dismiss()
            self._ask_filename(nombre_inicial=filename)

        def _cancelar(*_):
            popup_aviso.dismiss()

        btn_reemplazar.bind(on_press=_reemplazar)
        btn_cambiar.bind(on_press=_cambiar)
        btn_cancelar.bind(on_press=_cancelar)
        popup_aviso.open()

    def _normalizar_para_pdf(self, ruta, carpeta_tmp):
        """Si la foto tiene una etiqueta EXIF de rotación, graba una copia con
        los píxeles ya enderezados y sin esa etiqueta; si no la tiene (es el
        caso normal de una foto ya escaneada), devuelve la ruta tal cual.

        Las fotos que pasaron por escanear() ya vienen derechas y sin EXIF.
        Las que NO se escanearon (el escaneo falló, o el usuario las añadió
        sin procesar) conservan el EXIF original de la foto del celular.
        img2pdf solo endereza automáticamente las rotaciones simples (90/180/
        270); con una orientación espejada o inválida lanza una excepción, y
        entonces _write_pdf cae al respaldo de Pillow, que NO aplica ninguna
        corrección EXIF, así que hasta una foto con rotación simple saldría
        girada. Normalizar aquí antes evita depender de ese comportamiento.
        """
        try:
            with Image.open(ruta) as im:
                orientacion = im.getexif().get(0x0112, 1)
                if orientacion in (1, None):
                    return ruta
                derecha = ImageOps.exif_transpose(im)
                if derecha.mode != "RGB":
                    derecha = derecha.convert("RGB")
                destino = os.path.join(carpeta_tmp, f"{uuid.uuid4().hex[:10]}.jpg")
                derecha.save(destino, "JPEG", quality=95)
                return destino
        except Exception as e:
            Logger.error(f"main.py: no se pudo normalizar EXIF de {ruta}: {e}")
            return ruta

    def _write_pdf(self, out_path):
        # img2pdf es lo más fiel (sin recompresión). Fallback a Pillow si falla.
        with tempfile.TemporaryDirectory(prefix="pdf_exif_") as carpeta_tmp:
            imagenes = [self._normalizar_para_pdf(p, carpeta_tmp) for p in self.ordered_images]
            try:
                pdf_bytes = img2pdf.convert(imagenes, layout_fun=_LAYOUT_A4)
                with open(out_path, "wb") as f:
                    f.write(pdf_bytes)
            except Exception as e_img2pdf:
                imgs = []
                for p in imagenes:
                    im = Image.open(p)
                    if im.mode != "RGB":
                        im = im.convert("RGB")
                    imgs.append(im)
                if not imgs:
                    raise e_img2pdf
                imgs[0].save(out_path, "PDF", resolution=100.0, save_all=True, append_images=imgs[1:])

    def _generate_in_background(self, filename, uri_existente=None):
        try:
            # En Android se genera primero en la caché y luego se copia a Descargas
            work_dir = _android_cache_dir() if platform == "android" else get_default_save_dir()
            out_path = os.path.join(work_dir, filename)

            if uri_existente is None:
                # evitar sobrescritura silenciosa: añadir sufijo
                base, ext = os.path.splitext(out_path)
                counter = 1
                while os.path.exists(out_path):
                    counter += 1
                    out_path = f"{base} ({counter}){ext}"
            # si uri_existente no es None, vamos a reemplazar ese archivo en
            # Descargas: no hace falta evitar colisiones en la caché temporal,
            # ese archivo intermedio es descartable

            self._write_pdf(out_path)

            if platform == "android":
                if uri_existente is not None:
                    self.last_pdf_uri = self._reemplazar_pdf_en_descargas(out_path, uri_existente)
                else:
                    self.last_pdf_uri = self._save_pdf_to_downloads(out_path, filename)
                try:
                    os.remove(out_path)  # ya está en Descargas, borrar la copia de caché
                except OSError:
                    pass
                shown = f"Descargas/{filename}"
            else:
                self.last_pdf_uri = out_path
                shown = out_path

            Clock.schedule_once(
                lambda *_: self._registrar_pdf_en_historial(filename, self.last_pdf_uri), 0
            )
            Clock.schedule_once(lambda *_: self._on_success(shown), 0)
        except Exception as e:
            # guardar el texto ANTES: Python borra 'e' al salir del except
            msg = str(e)
            Clock.schedule_once(lambda *_: self._on_error(msg), 0)
        finally:
            # Un hilo de Python que usó pyjnius debe desengancharse de Java al terminar;
            # si no, Android cierra la app.
            if platform == "android":
                try:
                    from jnius import detach  # type: ignore
                    detach()
                except Exception:
                    pass

    def _save_pdf_to_downloads(self, local_path, filename):
        """Copia el PDF a Descargas vía MediaStore (Android 10+). Devuelve su Uri."""
        from jnius import autoclass  # type: ignore
        ContentValues = autoclass("android.content.ContentValues")
        Downloads = autoclass("android.provider.MediaStore$Downloads")

        resolver = _android_activity().getContentResolver()
        values = ContentValues()
        values.put("_display_name", filename)       # MediaStore.MediaColumns.DISPLAY_NAME
        values.put("mime_type", "application/pdf")  # MediaStore.MediaColumns.MIME_TYPE
        values.put("relative_path", "Download/")    # MediaStore.MediaColumns.RELATIVE_PATH

        uri = resolver.insert(Downloads.EXTERNAL_CONTENT_URI, values)
        if uri is None:
            raise RuntimeError("Android no permitió crear el archivo en Descargas")

        pfd = resolver.openFileDescriptor(uri, "w")
        fd = pfd.detachFd()
        with open(local_path, "rb") as src, os.fdopen(fd, "wb") as dst:
            shutil.copyfileobj(src, dst)
        pfd.close()
        return uri

    def _reemplazar_pdf_en_descargas(self, local_path, uri_existente):
        """Sobrescribe el contenido de un archivo YA EXISTENTE en Descargas
        (dado su Uri), en vez de insertar una entrada nueva. El modo 'wt'
        trunca el archivo antes de escribir, para no dejar basura si el PDF
        nuevo es más corto que el anterior."""
        resolver = _android_activity().getContentResolver()
        pfd = resolver.openFileDescriptor(uri_existente, "wt")
        fd = pfd.detachFd()
        with open(local_path, "rb") as src, os.fdopen(fd, "wb") as dst:
            shutil.copyfileobj(src, dst)
        pfd.close()
        return uri_existente

    def _on_success(self, shown):
        self.status.text = f"PDF guardado: {os.path.basename(shown)}"
        self._actualizar_boton_generar()
        # Habilitar solo si last_pdf_uri fue asignado correctamente antes de llegar aquí
        self._actualizar_boton_compartir()
        self._popup("Éxito", f"PDF creado correctamente:\n{shown}")

    def _on_error(self, msg):
        self.status.text = "Error al generar PDF"
        self._actualizar_boton_generar()
        # El PDF no se generó: dejar/poner el botón deshabilitado
        self._actualizar_boton_compartir()
        self._popup("Error", f"No se pudo generar el PDF:\n{msg}")

    # ── compartir ──
    def _share_pdf(self, *_):
        if platform != "android":
            self._popup("Información", "La función de compartir solo está disponible en Android.")
            return
        if self.last_pdf_uri is None:
            self._popup("Aviso", "Primero genera un PDF para poder compartirlo.")
            return
        try:
            from jnius import autoclass, cast  # type: ignore
            Intent = autoclass("android.content.Intent")
            String = autoclass("java.lang.String")
            intent = Intent(Intent.ACTION_SEND)
            intent.setType("application/pdf")
            # putExtra necesita el Uri como Parcelable: por eso el cast
            intent.putExtra(Intent.EXTRA_STREAM, cast("android.os.Parcelable", self.last_pdf_uri))
            intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            title = cast("java.lang.CharSequence", String("Compartir PDF"))
            chooser = Intent.createChooser(intent, title)
            _android_activity().startActivity(chooser)
        except Exception as e:
            self._popup("Error", f"No se pudo compartir el PDF:\n{e}")

    def _popup(self, title, message):
        box = BoxLayout(orientation="vertical", spacing=8, padding=10)
        lbl = Label(text=message)
        lbl.bind(size=lambda inst, val: setattr(inst, "text_size", val))
        box.add_widget(lbl)
        btn = Button(text="OK", size_hint_y=None, height="40dp")
        box.add_widget(btn)
        popup = Popup(title=title, content=box, size_hint=(0.85, 0.4))
        btn.bind(on_press=popup.dismiss)
        popup.open()


if __name__ == "__main__":
    ImageToPDFApp().run()
