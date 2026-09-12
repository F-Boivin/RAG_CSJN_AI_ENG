"""Empaqueta el índice para publicarlo, y dice qué variables poner en el despliegue.

    python -m scripts.empaquetar_indice

Deja `indice.tar.zst` junto al índice, con su sha256. Se publica como asset de un Release y
las dos variables que imprime van al servicio: el arranque baja el artefacto, comprueba la
huella y lo extrae en el volumen.
"""

import json
import sys
from pathlib import Path

from app.nucleo.config import cargar_entorno
from app.rag.ingesta.artefacto import empaquetar
from app.rag.ingesta.indice import rutas


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ajustes = cargar_entorno(exigir_clave=False)
    directorio = Path(ajustes.directorio_indice)
    _, _, ruta_manifiesto = rutas(directorio)
    if not ruta_manifiesto.exists():
        print(f"No hay índice en {directorio}. Corré scripts.construir_indice primero.")
        return 1

    manifiesto = json.loads(ruta_manifiesto.read_text(encoding="utf-8"))
    destino = directorio.parent / "indice.tar.zst"
    paquete, huella = empaquetar(directorio, destino)

    crudos = sum(f.stat().st_size for f in directorio.rglob("*") if f.is_file())
    print(f"índice:    {manifiesto['fragmentos']} fragmentos · {crudos / 1048576:.0f} MB")
    print(f"artefacto: {paquete} · {paquete.stat().st_size / 1048576:.0f} MB")
    print()
    print("Variables del servicio:")
    print(f"  INDICE_HUELLA=sha256:{huella}")
    print(f"  INDICE_URL=<url del asset del Release para {paquete.name}>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
