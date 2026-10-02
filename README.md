
# Imagen a PDF

Convertidor de imágenes a PDF desarrollado en Python con Kivy. La aplicación permite seleccionar varias imágenes, organizar su orden y unirlas en un único archivo PDF. Está lista para ejecutarse en escritorio y para exportar como APK con Buildozer hacia Android.

## Características

- Selección múltiple de imágenes (`.png`, `.jpg`, `.jpeg`, `.bmp`, `.gif`)
- Orden de imágenes igual al orden de selección visible en la interfaz
- Generación de PDF nombrada por el usuario
- Interfaz mínima y clara dirigida a teléfonos y escritorio
- Exportación a Android mediante Buildozer

## Requisitos

- Python 3.8 o superior
- Dependencias listadas en `requirements.txt`

## Instalación local

1. Activa el entorno virtual:
   - Windows: `.\venv\Scripts\activate`
   - Linux/macOS: `source venv/bin/activate`

2. Actualiza las dependencias:
   ```bash
   pip install -r requirements.txt
   ```

3. Ejecuta la aplicación:
   ```bash
   python app/main.py
   ```

## Estructura del proyecto

```text
proyecto_imagenes_pdf/
├── app/
│   └── main.py          # Lógica e interfaz de Kivy
├── venv/                # Entorno virtual
├── requirements.txt     # Dependencias Python
└── README.md
```

## Uso

1. Abre la aplicación y navega hasta la carpeta con las imágenes.
2. Selecciona las imágenes en el orden deseado.
3. Presiona **Generar PDF**, ingresa el nombre del archivo y guárdalo.
4. El PDF se creará en la ubicación indicada.

## Exportación a Android

1. Configura el entorno de Android (SDK, NDK, Java) según las instrucciones de Buildozer.
2. Genera el apk:
   ```bash
   buildozer init
   buildozer android debug
   ```
3. Ajusta `buildozer.spec` si es necesario para incluir las dependencias correctas.

## Notas de implementación

- La aplicación usa `img2pdf` para unir las imágenes sin recompresión.
- `Pillow` está disponible para cualquier transformación avanzada si se requiere en el futuro.
- El flujo de selección mantiene un listado interno de imágenes para facilitar la generación del PDF en dispositivos móviles.