"""Comprueba que un índice construido sirva, antes de publicarlo.

    python -m scripts.verificar_indice

Corre consultas conocidas contra el índice y controla lo que tiene que cumplirse siempre: que
las citas del padrón resuelvan a una URL oficial, que una cita buscada por su número traiga el
fragmento que la escribe, y que cada capítulo del «Recurso Extraordinario» sea alcanzable: su
consulta tiene que traer, entre los cuatro primeros, un fragmento de ese capítulo.
"""

import sys

from app.nucleo.config import cargar_entorno
from app.rag.ingesta.indice import abrir

# Una consulta por capítulo, con el capítulo que la responde.
CONSULTAS = [
    ("escrito de interposicion firmado solo por el letrado patrocinante", "1 Interposición"),
    ("traslado del recurso extraordinario a la contraparte", "2 Trámite"),
    ("interpretacion de normas federales como cuestion federal simple", "3 Cuestión federal"),
    ("gravamen de imposible reparacion ulterior", "4 Sentencia definitiva"),
    ("superior tribunal de provincia y el art. 14 de la ley 48", "5 Superior Tribunal de la Causa"),
    ("exceso ritual manifiesto", "6 Sentencias arbitrarias"),
    ("deposito previo del recurso de queja", "7 Recurso de Queja"),
]
# El primer sumario del documento, en «1.1.1 Quienes pueden interponerlo».
CITA_CONOCIDA = "312:2151"
TOPE = 4


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
    for documento in lexico.documentos():
        print(f"             {documento['origen']} · {documento['titulo']} · "
              f"actualizado al {documento.get('actualizado') or '(sin fecha)'}")
    print(f"padrón:      {len(padron)} citas · {len(sin_link)} sin link")
    print(f"subsecciones: {len(lexico.subsecciones())}")
    if sin_link:
        fallos.append(f"{len(sin_link)} citas del padrón quedaron sin link oficial")

    print("\nun capítulo por consulta:")
    for consulta, capitulo in CONSULTAS:
        secciones = [m["seccion"] for _, _, m in lexico.buscar(consulta, TOPE)]
        marca = "ok " if capitulo in secciones else "NO "
        print(f"  {marca} {capitulo:32} ← {consulta}")
        if capitulo not in secciones:
            fallos.append(f"«{consulta}» no trae ningún fragmento de «{capitulo}» "
                          f"entre los {TOPE} primeros: {secciones}")

    con_la_cita = [t for _, t, _ in lexico.buscar(f"Fallos: {CITA_CONOCIDA}", TOPE)
                   if CITA_CONOCIDA in t]
    print(f"\nla cita {CITA_CONOCIDA}: {len(con_la_cita)} fragmento(s) que la escriben")
    if not con_la_cita:
        fallos.append(f"buscar «Fallos: {CITA_CONOCIDA}» no trae el fragmento que la escribe")

    if fallos:
        print("\nPROBLEMAS:")
        for f in fallos:
            print(f"  - {f}")
        return 1
    print("\nEl índice está en condiciones de publicarse.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
