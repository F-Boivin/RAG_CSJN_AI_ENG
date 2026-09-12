"""Las dos mediciones que deciden cómo se construye el índice.

    python -m scripts.medir_recuperacion

1. **512 contra 1536 dimensiones**: se indexan los mismos documentos con las dos y se compara
   el solapamiento del top-4 sobre un set de consultas. Truncar el vector achica el índice a
   poco más de un tercio; lo que hay que saber es cuánto recall cuesta sobre jurisprudencia
   argentina, que es donde el número publicado por el proveedor no dice nada.

2. **FTS5 contra rank-bm25**: los dos lados léxicos sobre el mismo corpus, comparando el
   top-10. FTS5 vive en disco y consulta sin cargar nada en memoria; rank-bm25 reconstruye su
   índice en cada arranque leyendo el corpus entero.

Umbrales de aceptación: 80% de solapamiento para las dimensiones, 70% para el léxico.
"""

import shutil
import sys
import tempfile
from pathlib import Path

from app.almacen.consulta import Lexico
from app.almacen.escritura import EscritorLexico
from app.nucleo.config import cargar_entorno, obtener_ajustes, reiniciar_ajustes
from app.rag.ingesta import pdf, segmentacion
from app.rag.ingesta.fuentes_csjn import ClienteSJ, leer_catalogo
from app.rag.ingesta.indice import abrir_chroma

DOCUMENTOS = ["nota-224", "suplemento-79", "suplemento-42"]

CONSULTAS = [
    "exceso ritual manifiesto",
    "derecho a la imagen de personas publicas",
    "arbitrariedad por apartamiento de la solucion normativa",
    "expulsion de un migrante y unidad familiar",
    "competencia originaria en causas ambientales",
    "principio precautorio en materia ambiental",
    "consentimiento para la difusion de una fotografia",
    "beneficio de litigar sin gastos",
    "interes publico y libertad de informacion",
    "daño ambiental colectivo y recomposicion",
    "residencia precaria y arraigo",
    "verdad juridica objetiva",
    "sentencia definitiva a los fines del recurso extraordinario",
    "agotamiento de la via administrativa",
    "cuenca hidrica y jurisdiccion",
    "proteccion de datos personales e imagen",
    "medidas cautelares ambientales",
    "reunificacion familiar del migrante",
    "doctrina de la arbitrariedad y tercera instancia",
    "Fallos: 343:2211",
]


def solapamiento(a: list, b: list) -> float:
    """Qué proporción del primer resultado sobrevive en el segundo."""
    return len(set(a) & set(b)) / len(a) if a else 1.0


def fragmentos_de_prueba(ajustes) -> list[dict]:
    """Los fragmentos de los tres documentos, desde la caché de la ingesta."""
    catalogo = {e["origen"]: e for e in leer_catalogo(Path("catalogo.json"))}
    todos = []
    with ClienteSJ() as cliente:
        for origen in DOCUMENTOS:
            ficha = catalogo[origen]
            destino = Path(ajustes.directorio_cache) / "pdf" / f"{origen}.pdf"
            if not destino.exists():
                destino.parent.mkdir(parents=True, exist_ok=True)
                destino.write_bytes(cliente.descargar_pdf(ficha["url"]))
            documento = pdf.extraer(destino.read_bytes(), origen, ficha["titulo"])
            fragmentos, _ = segmentacion.fragmentar(documento, ficha)
            todos.extend(fragmentos)
    return todos


def medir_dimensiones(fragmentos: list[dict], temporal: Path) -> None:
    from langchain_core.documents import Document

    print("\n== 512 contra 1536 dimensiones ==")
    indices = {}
    for dims in (None, 512):
        etiqueta = dims or 1536
        import os

        os.environ["DIMENSIONES_EMBEDDINGS"] = str(dims) if dims else ""
        reiniciar_ajustes()
        obtener_ajustes()
        directorio = temporal / f"chroma-{etiqueta}"
        chroma = abrir_chroma(directorio)
        chroma.add_documents(
            [Document(page_content=f["texto"], metadata={"id": f["id"]}) for f in fragmentos],
            ids=[f["id"] for f in fragmentos],
        )
        indices[etiqueta] = chroma
        print(f"  indexados {len(fragmentos)} fragmentos con {etiqueta} dimensiones")

    solapes = []
    for consulta in CONSULTAS:
        tops = {
            etiqueta: [d.metadata["id"] for d in chroma.similarity_search(consulta, k=4)]
            for etiqueta, chroma in indices.items()
        }
        solapes.append(solapamiento(tops[1536], tops[512]))

    promedio = sum(solapes) / len(solapes)
    identicos = sum(1 for s in solapes if s == 1.0)
    print(f"  solapamiento del top-4: {promedio:.0%} promedio · "
          f"{identicos}/{len(solapes)} consultas idénticas · mínimo {min(solapes):.0%}")
    print(f"  umbral del plan: 80% → {'PASA' if promedio >= 0.80 else 'NO PASA'}")


def medir_lexico(fragmentos: list[dict], temporal: Path) -> None:
    print("\n== FTS5 contra rank-bm25 ==")
    ruta = temporal / "lexico.sqlite3"
    with EscritorLexico(ruta) as escritor:
        escritor.escribir_fragmentos(fragmentos)
    fts5 = Lexico(ruta, solo_lectura=True)

    try:
        from langchain_community.retrievers import BM25Retriever
        from langchain_core.documents import Document

        from app.rag.lexico import tokenizar
    except ImportError:
        print("  rank-bm25 no está instalado: la comparación no corre.")
        return

    bm25 = BM25Retriever.from_documents(
        [Document(page_content=f["texto"], metadata={"id": f["id"]}) for f in fragmentos],
        k=10, preprocess_func=tokenizar,
    )

    solapes = []
    for consulta in CONSULTAS:
        de_fts5 = [i for i, _, _ in fts5.buscar(consulta, 10)]
        de_bm25 = [d.metadata["id"] for d in bm25.invoke(consulta)]
        solapes.append(solapamiento(de_bm25, de_fts5))

    promedio = sum(solapes) / len(solapes)
    print(f"  solapamiento del top-10: {promedio:.0%} promedio · mínimo {min(solapes):.0%}")
    print(f"  umbral del plan: 70% → {'PASA' if promedio >= 0.70 else 'NO PASA'}")

    # La cita escrita literal es lo que el lado léxico aporta sobre el vectorial: si FTS5 la
    # pierde, la razón de tener dos retrievers desaparece.
    cita = "Fallos: 343:2211"
    posicion = next((n for n, (i, t, _) in enumerate(fts5.buscar(cita, 10), 1)
                     if "343:2211" in t), None)
    print(f"  la cita literal «{cita}» aparece en la posición {posicion or 'ninguna'} de FTS5")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ajustes = cargar_entorno()
    fragmentos = fragmentos_de_prueba(ajustes)
    print(f"corpus de prueba: {len(fragmentos)} fragmentos de {len(DOCUMENTOS)} documentos")
    temporal = Path(tempfile.mkdtemp(prefix="medicion-"))
    try:
        medir_dimensiones(fragmentos, temporal)
        medir_lexico(fragmentos, temporal)
    finally:
        shutil.rmtree(temporal, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
