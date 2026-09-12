"""Mide la búsqueda contra un set de consultas etiquetadas.

    python -m scripts.medir_lexico                # el barrido léxico, sin red ni claves
    python -m scripts.medir_lexico --con-vectorial  # agrega el vectorial y el híbrido

Cada consulta declara qué documentos del corpus la responden. La medida es **precisión en el
top-4**: qué proporción de lo que devuelve la búsqueda pertenece a alguno de esos documentos.

El barrido del peso del título reconstruye el índice léxico desde la caché de texto de la
ingesta y no toca los embeddings: por eso puede probar siete valores sin pagar una
reindexación por cada uno. `--con-vectorial` mide las tres ramas contra el índice construido,
y para eso embebe cada consulta: pide la clave y sale a la red.
"""

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

import app.nucleo.constantes as cfg
from app.almacen.consulta import Lexico
from app.almacen.escritura import EscritorLexico
from app.nucleo.config import cargar_entorno
from app.rag.ingesta import pdf, segmentacion
from app.rag.ingesta.fuentes_csjn import leer_catalogo

# Cada consulta con los documentos que la responden. Las etiquetas salen del título de cada
# documento del catálogo, que es lo que declara de qué trata.
#
# Solo entran consultas cuya respuesta está en el índice. Las seis últimas se sumaron cuando
# entró la categoría «Archivo Histórico»: son temas que ningún otro documento del corpus
# cubre, y sirven para comprobar que esos suplementos quedaron alcanzables.
ETIQUETADAS = [
    ("recurso extraordinario por sentencia arbitraria",
     {"cuadernillo-6-1-concepto", "cuadernillo-6-2-causales-de-arbitrariedad",
      "cuadernillo-6-3-improcedencia-del-recurso", "cuadernillo-6-4-tramite-y-resolucion",
      "suplemento-75", "nota-211", "nota-154", "nota-57", "nota-164"}),
    ("cuando procede el recurso extraordinario por sentencia arbitraria",
     {"cuadernillo-6-1-concepto", "cuadernillo-6-2-causales-de-arbitrariedad",
      "cuadernillo-6-3-improcedencia-del-recurso", "cuadernillo-6-4-tramite-y-resolucion",
      "suplemento-75", "nota-211", "nota-154", "nota-57", "nota-164"}),
    ("interes superior del niño",
     {"suplemento-1", "suplemento-59", "suplemento-83", "nota-47", "nota-53"}),
    ("derecho a la salud y prestaciones", {"suplemento-2", "suplemento-52", "suplemento-82"}),
    ("libertad de expresion y real malicia", {"suplemento-61", "suplemento-68", "nota-189"}),
    ("delitos de lesa humanidad", {"suplemento-71", "suplemento-72"}),
    ("derechos de los consumidores y usuarios", {"suplemento-74"}),
    ("expulsion de migrantes y unidad familiar", {"suplemento-79"}),
    ("derechos de las personas con discapacidad", {"suplemento-82"}),
    ("principio precautorio ambiental",
     {"suplemento-42", "suplemento-43", "suplemento-44", "suplemento-45",
      "suplemento-46", "suplemento-47"}),
    ("exceso ritual manifiesto",
     {"nota-156", "nota-55", "cuadernillo-6-2-causales-de-arbitrariedad"}),
    ("doctrina de los actos propios", {"nota-184"}),
    ("caducidad de instancia", {"nota-186", "nota-48"}),
    ("responsabilidad de los buscadores de internet", {"nota-189", "nota-224"}),
    ("derecho a la imagen", {"nota-224", "nota-215"}),
    ("restitucion internacional de niños", {"suplemento-83"}),
    ("principio de reparacion plena del daño", {"suplemento-67"}),
    ("art. 19 de la Constitucion Nacional", {"suplemento-81", "nota-215"}),
    ("profesores universitarios", {"suplemento-73"}),
    ("plazo razonable en el proceso penal", {"nota-68", "nota-73"}),
    ("competencia originaria de la Corte", {"suplemento-48", "suplemento-60"}),
    ("decretos de necesidad y urgencia", {"suplemento-49"}),
    ("habeas corpus", {"suplemento-57"}),
    ("habeas data", {"suplemento-58"}),
    ("movilidad jubilatoria", {"suplemento-63"}),
    ("derecho electoral", {"suplemento-54"}),
]

# Las mismas preguntas escritas como las escribiría alguien que no leyó el corpus: sin su
# vocabulario. Miden lo que aporta el lado vectorial, que es justo lo que el léxico no puede
# encontrar. Van aparte porque un promedio único escondería que cada lado sirve para otra cosa.
PARAFRASEADAS = [
    ("puede la corte revisar una sentencia mal fundada",
     {"cuadernillo-6-1-concepto", "cuadernillo-6-2-causales-de-arbitrariedad",
      "cuadernillo-6-3-improcedencia-del-recurso", "cuadernillo-6-4-tramite-y-resolucion",
      "suplemento-75", "nota-211", "nota-154", "nota-57", "nota-164"}),
    ("que pasa si echan del pais a alguien que tiene hijos acá",
     {"suplemento-79", "suplemento-16"}),
    ("google tiene que borrar resultados de busqueda",
     {"nota-189", "nota-224", "suplemento-50"}),
    ("una fabrica contamina un rio, quien tiene que juzgarlo",
     {"suplemento-42", "suplemento-43", "suplemento-44", "suplemento-45", "suplemento-46",
      "suplemento-47", "suplemento-48", "suplemento-60"}),
    ("me negaron una silla de ruedas por la obra social",
     {"suplemento-2", "suplemento-52", "suplemento-82"}),
    ("cuanto tiempo puede durar un juicio penal sin condena",
     {"nota-68", "nota-73"}),
    ("el presidente puede dictar una norma sin el congreso",
     {"suplemento-49"}),
    ("quiero saber que datos mios tiene una empresa",
     {"suplemento-58"}),
]

TOPE = 4


def fragmentos_del_corpus(ajustes) -> list[dict]:
    """Todos los fragmentos, re-segmentados desde la caché de texto de la ingesta."""
    catalogo = [e for e in leer_catalogo(Path("catalogo.json")) if e.get("indexar")]
    cache = Path(ajustes.directorio_cache) / "txt"
    todos = []
    for ficha in catalogo:
        archivo = cache / f"{ficha['origen']}.json"
        if not archivo.exists():
            continue
        guardado = json.loads(archivo.read_text(encoding="utf-8"))
        documento = pdf.Documento(
            origen=ficha["origen"], titulo=guardado["titulo"],
            paginas=[pdf.Pagina(numero=p["numero"], texto=p["texto"],
                                citas_urls=p["citas_urls"], titulos=p["titulos"])
                     for p in guardado["paginas"]],
            outline=[tuple(x) for x in guardado["outline"]],
        )
        todos.extend(segmentacion.fragmentar(documento, ficha)[0])

    from scripts.construir_indice import fragmentos_del_cuadernillo
    todos.extend(fragmentos_del_cuadernillo())
    return todos


def precision(lexico: Lexico) -> tuple[float, list]:
    """Precisión en el top-4 sobre el set etiquetado."""
    filas, aciertos = [], []
    for consulta, esperados in ETIQUETADAS:
        origenes = [m["origen"] for _, _, m in lexico.buscar(consulta, TOPE)]
        buenos = sum(1 for o in origenes if o in esperados)
        p = buenos / TOPE
        aciertos.append(p)
        filas.append((p, consulta, origenes))
    return sum(aciertos) / len(aciertos), filas


async def precision_de(recuperador) -> float:
    """Precisión en el top-4 de un recuperador de LangChain.

    Los dos recuperadores son asincrónicos por diseño —el camino sincrónico bloquearía el
    event loop del servicio—, así que la medición también lo es.
    """
    aciertos = []
    for consulta, esperados in ETIQUETADAS:
        encontrados = await recuperador.ainvoke(consulta)
        origenes = [d.metadata.get("origen") for d in encontrados[:TOPE]]
        aciertos.append(sum(1 for o in origenes if o in esperados) / TOPE)
    return sum(aciertos) / len(aciertos)


async def medir_las_tres_ramas(ajustes) -> None:
    """Léxico, vectorial e híbrido contra el índice construido.

    Es la tabla que va al README: el barrido de arriba compara al léxico consigo mismo, y no
    dice cuánto aporta cada rama del ensamble.
    """
    from app.rag.hibrido import RecuperadorLexico, RecuperadorVectorial, crear_hibrido
    from app.rag.ingesta.indice import abrir

    indice = abrir(ajustes.directorio_indice)
    candidatos = max(cfg.CANDIDATOS_POR_RETRIEVER, TOPE)
    ramas = {
        "léxico": RecuperadorLexico(lexico=indice.lexico, k=candidatos),
        "vectorial": RecuperadorVectorial(vectorstore=indice.chroma, k=candidatos),
        "híbrido": crear_hibrido(indice.chroma, indice.lexico, TOPE),
    }
    print(f"\nlas tres ramas sobre el índice construido, peso {cfg.PESO_LEXICO}/"
          f"{cfg.PESO_VECTORIAL}:")
    for nombre, recuperador in ramas.items():
        print(f"  {nombre:<12} {await precision_de(recuperador):.0%}")
    indice.lexico.cerrar()


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--con-vectorial", action="store_true",
                        help="mide también el vectorial y el híbrido (usa la clave y la red)")
    args = parser.parse_args()
    ajustes = cargar_entorno(exigir_clave=args.con_vectorial)
    fragmentos = fragmentos_del_corpus(ajustes)
    print(f"corpus: {len(fragmentos)} fragmentos · {len(ETIQUETADAS)} consultas etiquetadas\n")

    temporal = Path(tempfile.mkdtemp(prefix="lexico-"))
    ruta = temporal / "lexico.sqlite3"
    with EscritorLexico(ruta) as escritor:
        escritor.escribir_fragmentos(fragmentos)

    print(f"{'peso del titulo':<18} precision@{TOPE}")
    mejor = (0.0, None)
    for peso in (0.0, 1.0, 2.0, 3.0, 5.0, 8.0, 12.0):
        pesos = (cfg.PESOS_BM25[0], cfg.PESOS_BM25[1], peso)
        original = cfg.PESOS_BM25
        cfg.PESOS_BM25 = pesos
        try:
            lexico = Lexico(ruta, solo_lectura=True)
            p, filas = precision(lexico)
            lexico.cerrar()
        finally:
            cfg.PESOS_BM25 = original
        marca = " <-- configurado" if peso == original[2] else ""
        print(f"  {peso:<16} {p:.0%}{marca}")
        if p > mejor[0]:
            mejor = (p, peso, filas)

    print(f"\nmejor: peso {mejor[1]} con {mejor[0]:.0%}")
    print("\nconsulta por consulta con ese peso:")
    for p, consulta, origenes in sorted(mejor[2]):
        print(f"  {p:>4.0%}  {consulta[:46]:<48} {', '.join(sorted(set(origenes)))[:56]}")

    if args.con_vectorial:
        asyncio.run(medir_las_tres_ramas(ajustes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
