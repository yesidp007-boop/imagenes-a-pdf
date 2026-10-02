"""
conectar_omniroute.py — arranca OmniRoute + Claude Code para este proyecto,
sin tener que abrir las ventanas manualmente cada vez.

Uso:
    python conectar_omniroute.py
"""

import socket
import subprocess
import time
import urllib.request

PUERTO_OMNIROUTE = 20128
CARPETA_PROYECTO = r"C:\Users\YesidP\openroute\proyecto_imagenes_pdf"
ESPERA_MAXIMA_SEGUNDOS = 30


def omniroute_ya_esta_corriendo():
    """Hace una peticion HTTP real al dashboard. Si responde, el servidor ya esta listo de verdad."""
    try:
        with urllib.request.urlopen(f"http://localhost:{PUERTO_OMNIROUTE}", timeout=2) as resp:
            return resp.status < 500
    except Exception:
        return False


def abrir_ventana_powershell(comando, titulo):
    """Abre una nueva ventana de PowerShell que se queda abierta corriendo `comando`."""
    subprocess.Popen([
        "powershell", "-NoExit", "-Command",
        f"$host.UI.RawUI.WindowTitle = '{titulo}'; {comando}"
    ], creationflags=subprocess.CREATE_NEW_CONSOLE)


def main():
    if omniroute_ya_esta_corriendo():
        print("OmniRoute ya esta corriendo. Abriendo solo Claude Code...")
    else:
        print("Iniciando OmniRoute...")
        abrir_ventana_powershell("omniroute", "OmniRoute")

        print("Esperando a que el panel responda", end="", flush=True)
        esperado = 0
        while not omniroute_ya_esta_corriendo():
            if esperado >= ESPERA_MAXIMA_SEGUNDOS:
                print("\nOmniRoute no respondio a tiempo. Revisa la ventana que se abrio.")
                return
            print(".", end="", flush=True)
            time.sleep(1)
            esperado += 1
        print(" listo.")

    time.sleep(2)  # margen extra de seguridad antes de lanzar Claude Code
    print(f"Abriendo Claude Code en {CARPETA_PROYECTO}...")
    comando_claude = f'cd "{CARPETA_PROYECTO}"; omniroute launch'
    abrir_ventana_powershell(comando_claude, "Claude Code - Imagenes a PDF")

    print("Listo. Revisa la nueva ventana de Claude Code.")


if __name__ == "__main__":
    main()