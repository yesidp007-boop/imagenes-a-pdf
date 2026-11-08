
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

## Compilar e instalar en el celular (flujo real)

Usar siempre el script `build.sh` (alias **`bdap`**) desde **Ubuntu (WSL)**, no comandos sueltos ni PowerShell.

### Antes de ejecutar `bdap`

1. Activar el entorno: `source ~/buildozer-env/bin/activate` (la línea debe empezar con `(buildozer-env)`).
2. Revisar que no haya un `main.py` viejo en Descargas. El paso 0 de `bdap` lo usaría **en lugar** del `main.py` del proyecto:
   ```bash
   ls /mnt/c/Users/YesidP/Downloads/main*.py   # lo correcto: "No such file or directory"
   ```
3. **Si cambiaste `buildozer.spec`** (versión, permisos, requisitos), cópialo a mano, porque `bdap` solo copia `main.py` y `escaner.py`:
   ```bash
   cp /mnt/c/Users/YesidP/openroute/proyecto_imagenes_pdf/buildozer.spec ~/proyecto_imagenes_pdf/
   ```

### Regla de la versión

- Cada APK que vaya al celular debe tener una `version` **mayor** que la instalada (línea 7 de `buildozer.spec`).
- Android no instala una versión menor encima de una mayor, y con dos APK de versiones distintas es fácil instalar el viejo sin darse cuenta.
- Versión actual: **0.3** (8 nov 2026).

### Después de compilar

- Revisar que `bdap` termine con `APK copiado: .../imagenesapdf-<versión nueva>-arm64-v8a-debug.apk`.
- En el celular, instalar el APK cuyo nombre lleve la versión nueva y comprobar en la propia app que se ve el cambio (por ejemplo, un botón nuevo).
- Ojo: comprobar el APK con `unzip ... private.tar | grep` NO funciona (da 0 aunque el cambio esté). No usarlo.

### Lección del 8 nov 2026

Se compiló con rsync manual. Eso copió el `buildozer.spec` de Windows (versión 0.1) encima del de Linux (0.2). El APK nuevo salió como 0.1, quedaron dos APK en el Escritorio y el celular siguió con el código viejo. Solución: subir a 0.3 y usar `bdap`.
