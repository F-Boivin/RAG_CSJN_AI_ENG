"""Comprueba que un índice construido sirva, antes de publicarlo.

    python -m scripts.verificar_indice

Corre consultas conocidas contra el índice y controla lo que tiene que cumplirse siempre: que
la búsqueda devuelva fragmentos, que las citas del padrón resuelvan a una URL oficial, y que
las subsecciones se puedan resolver por el nombre que un modelo escribiría.
"""

import sys
from pathlib import Path

from app.nucleo.config import cargar_entorno
from app.rag.ingesta.indice import abrir

CONSULTAS = [
    "exceso ritual manifiesto",
    "arbitrariedad de sentencia por contradiccion",
    "interes superior del niño",
    "derecho a la salud y prestaciones",
    "competencia originaria de la Corte",
    "libertad de expresion y real malicia",
    "delitos de lesa humanidad imprescriptibilidad",
    "derechos de las personas con discapacidad",
    "sentencia definitiva recurso extraordinario",
    "Fallos: 311:2437",
]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ajustes = cargar_entorno(exigir_clave=False)
    indice = abrir(ajustes.directorio_indice)
    lexico = indice.lexico
    fallos = []

    padron = lexico.padron()
    sin_link = [c for c, u in padron.items() if not u]
    print(f"índice:      {lexico.cantidad_fragmentos()} fragmentos · "
          f"{len(lexico.documentos())} documentos · huella {indice.version}")
    print(f"padrón:      {len(padron)} citas · {len(sin_link)} sin link")
    print(f"subsecciones: {len(lexico.subsecciones())}")
    if sin_link:
        fallos.append(f"{len(sin_link)} citas del padrón quedaron sin link oficial")

    print("\nbúsqueda léxica:")
    for consulta in CONSULTAS:
        encontrados = lexico.buscar(consulta, 4)
        print(f"  {len(encontrados)}  {consulta}")
        if not encontrados:
            fallos.append(f"la consulta «{consulta}» no devolvió ningún fragmento")

    print("\nresolución de subsecciones:")
    for nombre in lexico.subsecciones()[:5]:
        sin_tildes = nombre.lower()
        resuelta = lexico.resolver_subseccion(sin_tildes)
        marca = "ok " if resuelta == nombre else "NO "
        print(f"  {marca} {nombre[:70]}")
        if resuelta != nombre:
            fallos.append(f"«{nombre}» no resuelve desde su forma normalizada")

    if fallos:
        print("\nPROBLEMAS:")
        for f in fallos:
            print(f"  - {f}")
        return 1
    print("\nEl índice está en condiciones de publicarse.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
