import os
import shutil
import time
import uuid
from pathlib import Path
from threading import Thread

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
from kivy.utils import platform
from kivy.logger import Logger
from kivy.graphics import Color, Rectangle, Ellipse
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

    Muestra la imagen original con 4 puntos arrastrables en las esquinas.
    Convierte coordenadas entre espacio de imagen y espacio de pantalla.

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
        # Si no hay esquinas detectadas, usar las 4 esquinas de la imagen
        if esquinas_detectadas is not None:
            # esquinas_detectadas ya está ordenado por _ordenar_esquinas
            self.esquinas = esquinas_detectadas.copy()
        else:
            self.esquinas = np.array([
                [0, 0],  # arriba-izq
                [self.imagen_ancho - 1, 0],  # arriba-der
                [self.imagen_ancho - 1, self.imagen_alto - 1],  # abajo-der
                [0, self.imagen_alto - 1]  # abajo-izq
            ], dtype="float32")

        # Cargar la imagen en una texture para el canvas
        self.texture = None
        self._cargar_texture()

        # Radio del punto arrastrable
        self.punto_radio = 12

        # Para rastrear qué punto se está arrastrando
        self._punto_actual = None
        self._pos_inicial = None

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
        """Redibujar el canvas con la imagen y los puntos."""
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

            # Dibujar puntos en las esquinas
            for i, (x, y) in enumerate(self.esquinas):
                # Convertir coordenadas de imagen a coordenadas de pantalla
                px = float(draw_x + (x / self._img_ancho) * draw_width)
                py = float(draw_y + (1 - y / self._img_alto) * draw_height)  # Y invertido

                # Color diferente para cada punto
                colores = [(1, 0, 0, 1), (0, 1, 0, 1), (0, 0, 1, 1), (1, 1, 0, 1)]
                Color(*colores[i])

                # Círculo
                Ellipse(pos=(px - self.punto_radio, py - self.punto_radio),
                        size=(self.punto_radio * 2, self.punto_radio * 2))

                # Número del punto
                Color(1, 1, 1, 1)
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
                size=('24dp', '24dp'),
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
        px, py = self.coordenada_imagen_a_pantalla(self.esquinas[punto_idx][0], self.esquinas[punto_idx][1])
        return abs(x - px) <= self.punto_radio * 2 and abs(y - py) <= self.punto_radio * 2

    def on_touch_down(self, touch):
        """Iniciar arrastre si se toca un punto."""
        if not self.collide_point(*touch.pos):
            return super().on_touch_down(touch)

        for i in range(4):
            if self._punto_en(touch.x, touch.y, i):
                self._punto_actual = i
                self._pos_inicial = (touch.x, touch.y)
                # Mover el punto al frente visualmente
                return True

        return super().on_touch_down(touch)

    def on_touch_move(self, touch):
        """Mover el punto si se está arrastrando."""
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
        return super().on_touch_move(touch)

    def on_touch_up(self, touch):
        """Finalizar arrastre."""
        if self._punto_actual is not None:
            self._punto_actual = None
            self._pos_inicial = None
            return True
        return super().on_touch_up(touch)


class PopupRevisarEscaneo(Popup):
    """Popup para revisar una imagen escaneada antes de aceptarla."""

    def __init__(self, imagen_escaneada_ruta, imagen_original_ruta, esquinas_detectadas, app, **kwargs):
        super().__init__(**kwargs)
        self.imagen_escaneada_ruta = imagen_escaneada_ruta
        self.imagen_original_ruta = imagen_original_ruta
        self.esquinas_detectadas = esquinas_detectadas
        self.app = app
        self.auto_dismiss = False  # El popup no se cierra solo

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

        layout.add_widget(btn_layout)
        self.content = layout

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
        self.btn_generate_disabled_state = False

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
        scroll = ScrollView(size_hint_y=0.35 if platform != "android" else 1)
        scroll.add_widget(self.list_container)
        root.add_widget(scroll)

        # Botones generar y compartir
        btn_layout = BoxLayout(size_hint_y=None, height="48dp", spacing=8)
        self.btn_generate = Button(
            text="Generar PDF",
            size_hint_x=0.5,
            size_hint_y=None,
            height="48dp",
            background_color=(0.18, 0.6, 0.95, 1),
        )
        self.btn_generate.bind(on_press=self._ask_filename)
        btn_layout.add_widget(self.btn_generate)

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
        return root

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
            self._refresh_list()
            self.status.text = f"Se añadieron {len(rutas)} imágenes"
            return
        self.btn_generate_disabled_state = self.btn_generate.disabled
        self.btn_generate.disabled = True
        self.fotos_pendientes = list(rutas)
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

            # Guardar información para la revisión
            self.foto_actual_en_revision = (ruta, salida, info)

            # Programar la revisión en el hilo principal
            Clock.schedule_once(lambda *_: self._mostrar_popup_revision(), 0)

        except Exception as e:
            Logger.error(f"Error escaneando {ruta}: {e}")
            # En caso de error, usar la original
            self.foto_actual_en_revision = (ruta, None, None)
            Clock.schedule_once(lambda *_: self._on_revision_terminada(None, None), 0)

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
        self.popup_revision.open()

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

    # ── lista ──
    def _refresh_list(self):
        self.list_container.clear_widgets()
        if not self.ordered_images:
            self.list_container.add_widget(
                Label(text="Sin imágenes — pulsa Añadir", font_size="12sp", color=(0.6, 0.6, 0.6, 1), size_hint_y=None, height="30dp")
            )
            self.status.text = "0 imágenes en cola"
            return

        for idx, path in enumerate(self.ordered_images):
            name = os.path.basename(path)
            is_hl = idx == self.highlighted_index
            row = BoxLayout(size_hint_y=None, height="64dp", spacing=8, padding=(4, 4))
            # resaltar la fila completa con un fondo si está seleccionada
            if is_hl:
                with row.canvas.before:
                    from kivy.graphics import Color, Rectangle
                    Color(0.18, 0.6, 0.95, 0.18)
                    rect = Rectangle(pos=row.pos, size=row.size)
                row.bind(pos=lambda inst, val, r=rect: setattr(r, "pos", val))
                row.bind(size=lambda inst, val, r=rect: setattr(r, "size", val))

            # miniatura de la imagen (ya escaneada / lista para el PDF)
            thumb = KivyImage(
                source=path,
                size_hint=(None, None),
                size=("56dp", "56dp"),
                allow_stretch=True,
                keep_ratio=True,
            )
            row.add_widget(thumb)

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
            self.list_container.add_widget(row)

        self.status.text = f"{len(self.ordered_images)} imágenes en cola - toca '-' para seleccionar y usa Subir/Bajar"
        self.btn_generate.disabled = False

    def _highlight(self, idx):
        self.highlighted_index = idx if self.highlighted_index != idx else None
        self._refresh_list()

    def _move_up(self, *_):
        i = self.highlighted_index
        if i is None or i == 0:
            return
        self.ordered_images[i - 1], self.ordered_images[i] = self.ordered_images[i], self.ordered_images[i - 1]
        self.highlighted_index = i - 1
        self._refresh_list()

    def _move_down(self, *_):
        i = self.highlighted_index
        if i is None or i >= len(self.ordered_images) - 1:
            return
        self.ordered_images[i + 1], self.ordered_images[i] = self.ordered_images[i], self.ordered_images[i + 1]
        self.highlighted_index = i + 1
        self._refresh_list()

    def _remove_highlighted(self, *_):
        i = self.highlighted_index
        if i is None:
            # si no hay resaltado, quitar la última
            if self.ordered_images:
                self.ordered_images.pop()
                self.highlighted_index = None
                self._refresh_list()
            return
        del self.ordered_images[i]
        self.highlighted_index = None
        self._refresh_list()

    # ── generación PDF ──
    def _ask_filename(self, *_):
        if not self.ordered_images:
            self._popup("Aviso", "Añade al menos una imagen antes de generar el PDF.")
            return

        box = BoxLayout(orientation="vertical", spacing=8, padding=10)
        box.add_widget(Label(text="Nombre del PDF:", size_hint_y=None, height="22dp"))
        ti = TextInput(text="documento.pdf", multiline=False, size_hint_y=None, height="40dp")
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
        self.status.text = "Generando PDF..."
        self.btn_generate.disabled = True
        self.btn_share.disabled = True
        Thread(target=self._generate_in_background, args=(filename,), daemon=True).start()

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

    def _generate_in_background(self, filename):
        try:
            # En Android se genera primero en la caché y luego se copia a Descargas
            work_dir = _android_cache_dir() if platform == "android" else get_default_save_dir()
            out_path = os.path.join(work_dir, filename)

            # evitar sobrescritura silenciosa: añadir sufijo
            base, ext = os.path.splitext(out_path)
            counter = 1
            while os.path.exists(out_path):
                counter += 1
                out_path = f"{base} ({counter}){ext}"

            self._write_pdf(out_path)

            if platform == "android":
                self.last_pdf_uri = self._save_pdf_to_downloads(out_path, filename)
                try:
                    os.remove(out_path)  # ya está en Descargas, borrar la copia de caché
                except OSError:
                    pass
                shown = f"Descargas/{filename}"
            else:
                self.last_pdf_uri = out_path
                shown = out_path

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

    def _on_success(self, shown):
        self.status.text = f"PDF guardado: {os.path.basename(shown)}"
        self.btn_generate.disabled = False
        self.btn_share.disabled = False
        self._popup("Éxito", f"PDF creado correctamente:\n{shown}")

    def _on_error(self, msg):
        self.status.text = "Error al generar PDF"
        self.btn_generate.disabled = False
        self.btn_share.disabled = False
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
