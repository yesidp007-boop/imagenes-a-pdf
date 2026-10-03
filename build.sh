#!/bin/bash
# Script para automatizar el ciclo completo:
#   0) tomar el main.py más reciente descargado (carpeta Descargas de Windows)
#      y copiarlo a la carpeta del proyecto (Windows)
#   1) copiar código desde Windows -> WSL
#   2) compilar con buildozer
#   3) copiar el APK resultante al Escritorio de Windows
#
# Uso: desde cualquier lugar, correr:
#   bdap
#
# Si algo falla, el script se detiene ahí mismo (gracias a "set -e") en vez de
# seguir como si nada y confundir sobre qué paso realmente funcionó.

set -e  # detener el script si cualquier comando falla

# --- Rutas (ajusta aquí si alguna vez cambian) ---
WIN_APP_DIR="/mnt/c/Users/YesidP/openroute/proyecto_imagenes_pdf/app"
WSL_PROJECT_DIR="$HOME/proyecto_imagenes_pdf"
WSL_APP_DIR="$WSL_PROJECT_DIR/app"
ESCRITORIO="/mnt/c/Users/YesidP/Desktop"
DESCARGAS="/mnt/c/Users/YesidP/Downloads"

echo "=============================================="
echo "0/4 - Buscando un main.py recién descargado"
echo "=============================================="
# Toma TODOS los archivos que empiecen con "main" dentro de Descargas
# (cubre main.py, main (1).py, main_actualizado.py, main_actualizado (1).py, etc.)
mapfile -t DESCARGADOS < <(ls -t "$DESCARGAS"/main*.py 2>/dev/null || true)

if [ "${#DESCARGADOS[@]}" -gt 0 ]; then
    ULTIMO_DESCARGADO="${DESCARGADOS[0]}"
    echo "Encontrado(s) en Descargas (${#DESCARGADOS[@]}):"
    printf '  %s\n' "${DESCARGADOS[@]}"
    echo "Usando el más reciente: $ULTIMO_DESCARGADO"
    cp -v "$ULTIMO_DESCARGADO" "$WIN_APP_DIR/main.py"

    # Borrar TODOS los main*.py de Descargas (incluido el que se acaba de usar).
    # Así nunca se acumulan copias viejas con "(1)", "(2)", etc. que puedan
    # confundirse después con "la más reciente".
    echo "Limpiando Descargas para evitar copias viejas..."
    rm -v -- "${DESCARGADOS[@]}"
else
    echo "No hay ningún main*.py nuevo en Descargas."
    echo "Se usará el main.py que ya está en la carpeta del proyecto (app/)."
fi
echo ""

echo "=============================================="
echo "1/4 - Copiando código fuente de Windows a WSL"
echo "=============================================="

# Copia main.py y escaner.py (los dos archivos que se editan normalmente).
# Si en algún momento hay más archivos .py que editar, agrégalos aquí también.
cp -v "$WIN_APP_DIR/main.py" "$WSL_APP_DIR/main.py"
if [ -f "$WIN_APP_DIR/escaner.py" ]; then
    cp -v "$WIN_APP_DIR/escaner.py" "$WSL_APP_DIR/escaner.py"
fi

echo ""
echo "Archivo main.py copiado. Fecha de modificación en WSL ahora:"
ls -la "$WSL_APP_DIR/main.py"
echo ""

cd "$WSL_PROJECT_DIR"

echo "=============================================="
echo "2/4 - Compilando con buildozer (puede tardar)"
echo "=============================================="
buildozer -v android debug

echo ""
echo "=============================================="
echo "3/4 - Copiando el APK al Escritorio"
echo "=============================================="

# Toma el .apk más reciente dentro de bin/, por si el nombre de versión cambia.
APK_PATH=$(ls -t bin/*.apk | head -n 1)

if [ -z "$APK_PATH" ]; then
    echo "ERROR: no se encontró ningún .apk en bin/. Revisa si buildozer falló arriba."
    exit 1
fi

cp -v "$APK_PATH" "$ESCRITORIO/"

echo ""
echo "=============================================="
echo "LISTO."
echo "APK copiado: $ESCRITORIO/$(basename "$APK_PATH")"
echo "Fecha/hora de compilación:"
date
echo "=============================================="
