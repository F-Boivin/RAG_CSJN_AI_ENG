"""Construye el índice del corpus, en cinco etapas con caché.

    python -m scripts.construir_indice                        # todo
    python -m scripts.construir_indice --solo nota-224        # unos pocos documentos
    python -m scripts.construir_indice --etapa descargar      # hasta una etapa
    python -m scripts.construir_indice --forzar               # ignora la caché

Cada etapa deja su resultado en `datos/cache/` y la siguiente lo lee. Correr el script dos
veces sin cambios en la fuente no descarga, no extrae y no gasta un solo embedding: lo que
decide es el sha256 del PDF, que es la única señal de cambio que los endpoints exponen.

Esto corre en la máquina de quien construye el índice, con las claves a mano. El servicio
desplegado abre el índice ya armado y nunca habla con csjn.gov.ar.
"""

import argparse
import json
import sys
import time
from pathlib import Path

import app.nucleo.constantes as cfg
from app.nucleo.config import cargar_entorno, obtener_ajustes
from app.nucleo.errores import ErrorRAG
from app.rag.ingesta import fuentes_csjn as fuentes
from app.rag.ingesta import pdf, segmentacion
from app.rag.ingesta.indice import Constructor
from app.rag.ingesta.markdown import leer_documentos

ETAPAS = ("catalogar", "descargar", "extraer", "segmentar", "indexar")
RUTA_CATALOGO = cfg.RAIZ / "catalogo.json"


def _cache(ajustes, *partes: str) -> Path:
    ruta = Path(ajustes.directorio_cache).joinpath(*partes)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    return ruta


def catalogar(ajustes, forzar: bool) -> list[dict]:
    """Etapa 1: el catálogo de los 124 documentos, versionado en el repositorio."""
    if RUTA_CATALOGO.exists() and not forzar:
        entradas = fuentes.leer_catalogo(RUTA_CATALOGO)
        print(f"catálogo: {len(entradas)} documentos (de {RUTA_CATALOGO.name})")
        return entradas
    with fuentes.ClienteSJ() as cliente:
        entradas = cliente.catalogar()
    fuentes.guardar_catalogo(entradas, RUTA_CATALOGO)
    print(f"catálogo: {len(entradas)} documentos escritos en {RUTA_CATALOGO.name}")
    return entradas


def descargar(ajustes, entradas: list[dict], forzar: bool) -> dict[str, Path]:
    """Etapa 2: los PDF en `cache/pdf/`, con su sha256 anotado."""
    rutas: dict[str, Path] = {}
    bajados = 0
    with fuentes.ClienteSJ() as cliente:
        for entrada in entradas:
            destino = _cache(ajustes, "pdf", f"{entrada['origen']}.pdf")
            if destino.exists() and not forzar:
                entrada["sha256"] = fuentes.huella(destino.read_bytes())
                rutas[entrada["origen"]] = destino
                continue
            datos = cliente.descargar_pdf(entrada["url"])
            destino.write_bytes(datos)
            entrada["sha256"] = fuentes.huella(datos)
            rutas[entrada["origen"]] = destino
            bajados += 1
            print(f"  bajado {entrada['origen']} ({len(datos) // 1024} KB)")
    print(f"descarga: {len(rutas)} PDF disponibles, {bajados} bajados ahora")
    return rutas


def extraer(ajustes, entradas: list[dict], rutas: dict[str, Path], forzar: bool) -> dict[str, dict]:
    """Etapa 3: texto, links y citas de cada PDF, en `cache/txt/`."""
    extraidos: dict[str, dict] = {}
    for entrada in entradas:
        origen = entrada["origen"]
        if origen not in rutas:
            continue
        destino = _cache(ajustes, "txt", f"{origen}.json")
        if destino.exists() and not forzar:
            guardado = json.loads(destino.read_text(encoding="utf-8"))
            if guardado.get("sha256") == entrada.get("sha256"):
                extraidos[origen] = guardado
                continue
        documento = pdf.extraer(rutas[origen].read_bytes(), origen, entrada["titulo"])
        guardado = {
            "sha256": entrada.get("sha256", ""),
            "titulo": entrada["titulo"],
            "desacuerdos": documento.desacuerdos,
            "outline": documento.outline,
            "paginas": [
                {"numero": p.numero, "texto": p.texto, "citas_urls": p.citas_urls,
                 "titulos": p.titulos}
                for p in documento.paginas
            ],
        }
        destino.write_text(json.dumps(guardado, ensure_ascii=False), encoding="utf-8")
        extraidos[origen] = guardado
    paginas = sum(len(e["paginas"]) for e in extraidos.values())
    desacuerdos = sum(e["desacuerdos"] for e in extraidos.values())
    print(f"extracción: {len(extraidos)} documentos, {paginas} páginas, "
          f"{desacuerdos} desacuerdos ancla/URL")
    return extraidos


def _documento_de(guardado: dict, origen: str) -> pdf.Documento:
    """Rehidrata un documento extraído desde su caché."""
    paginas = [pdf.Pagina(numero=p["numero"], texto=p["texto"],
                          citas_urls=p["citas_urls"], titulos=p["titulos"])
               for p in guardado["paginas"]]
    return pdf.Documento(origen=origen, titulo=guardado["titulo"], paginas=paginas,
                         outline=[tuple(x) for x in guardado["outline"]],
                         desacuerdos=guardado["desacuerdos"])


def segmentar(entradas: list[dict], extraidos: dict[str, dict]) -> dict[str, tuple[list[dict], str]]:
    """Etapa 4: los fragmentos con su metadata, en memoria."""
    resultado: dict[str, tuple[list[dict], str]] = {}
    for entrada in entradas:
        origen = entrada["origen"]
        if origen not in extraidos:
            continue
        documento = _documento_de(extraidos[origen], origen)
        resultado[origen] = segmentacion.fragmentar(documento, entrada)
    total = sum(len(f) for f, _ in resultado.values())
    metodos = {}
    for _, metodo in resultado.values():
        metodos[metodo] = metodos.get(metodo, 0) + 1
    print(f"segmentación: {total} fragmentos · subsecciones por {metodos}")
    return resultado


def fragmentos_del_cuadernillo() -> list[dict]:
    """Los fragmentos del corpus markdown que ya estaba indexado.

    Entra por el mismo camino que los PDF: el índice no distingue de dónde viene un fragmento,
    solo su metadata `fuente`.
    """
    from app.rag.ingesta.markdown import fragmentar as fragmentar_markdown

    documentos = leer_documentos()
    fragmentos = []
    for doc in fragmentar_markdown(documentos):
        origen = f"cuadernillo-{Path(doc.metadata['origen']).stem}"
        fragmentos.append({
            "id": f"{origen}#{len([f for f in fragmentos if f['origen'] == origen]):04d}",
            "origen": origen,
            "seccion": doc.metadata.get("seccion", ""),
            "subseccion": doc.metadata.get("subseccion", ""),
            "fuente": "cuadernillo",
            "pagina": None,
            "url_documento": "",
            "texto": doc.page_content,
            "tokens": doc.metadata.get("tokens", 0),
            "citas_urls": json.loads(doc.metadata.get("citas_urls", "{}")),
        })
    return fragmentos


def indexar(ajustes, entradas: list[dict], segmentados: dict, extraidos: dict) -> dict:
    """Etapa 5: Chroma y el índice léxico, documento por documento."""
    comienzo = time.monotonic()
    por_origen = {e["origen"]: e for e in entradas}
    with Constructor(ajustes.directorio_indice) as constructor:
        # El cuadernillo entra primero: es el corpus que ya estaba y da el piso del padrón.
        del_cuadernillo = fragmentos_del_cuadernillo()
        for origen in sorted({f["origen"] for f in del_cuadernillo}):
            propios = [f for f in del_cuadernillo if f["origen"] == origen]
            constructor.indexar_documento(propios, {
                "origen": origen, "tipo": "cuadernillo",
                "titulo": propios[0]["seccion"], "categoria": "Cuadernillo de doctrina",
                "url": "", "sha256": "", "paginas": 0,
                "metodo_subsecciones": "markdown",
            })
            print(f"  indexado {origen}: {len(propios)} fragmentos")

        for origen, (fragmentos, metodo) in segmentados.items():
            ficha = por_origen[origen]
            constructor.indexar_documento(fragmentos, {
                **ficha, "paginas": len(extraidos[origen]["paginas"]),
                "metodo_subsecciones": metodo,
            })
            print(f"  indexado {origen}: {len(fragmentos)} fragmentos ({metodo})")

        total = sum(len(f) for f, _ in segmentados.values()) + len(del_cuadernillo)
        manifiesto = constructor.cerrar_indice({
            "documentos": len(segmentados) + len({f["origen"] for f in del_cuadernillo}),
            "fragmentos": total,
            "desacuerdos_ancla_url": sum(e["desacuerdos"] for e in extraidos.values()),
            "huella": fuentes.huella(
                json.dumps(sorted((o, e.get("sha256", ""))
                                  for o, e in por_origen.items()), ensure_ascii=False
                           ).encode("utf-8"))[:16],
        })
    print(f"índice: {manifiesto['fragmentos']} fragmentos, "
          f"{manifiesto['links_sintetizados']} links sintetizados, "
          f"huella {manifiesto['huella']} · {time.monotonic() - comienzo:.0f} s")
    return manifiesto


def main(argv=None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--solo", nargs="*", default=None,
                        help="orígenes a procesar, p. ej. nota-224 suplemento-79")
    parser.add_argument("--etapa", choices=ETAPAS, default="indexar",
                        help="hasta qué etapa correr (por defecto, todas)")
    parser.add_argument("--forzar", action="store_true", help="ignora la caché")
    parser.add_argument("--sin-cuadernillo", action="store_true",
                        help="deja fuera el corpus markdown")
    args = parser.parse_args(argv)

    hasta = ETAPAS.index(args.etapa)
    # Las tres primeras etapas son HTTP y PyMuPDF: sin clave. La que embebe la exige.
    ajustes = cargar_entorno(exigir_clave=hasta >= ETAPAS.index('indexar'))

    entradas = catalogar(ajustes, args.forzar)
    entradas = [e for e in entradas if e.get("indexar")]
    if args.solo:
        pedidos = set(args.solo)
        entradas = [e for e in entradas if e["origen"] in pedidos]
        faltan = pedidos - {e["origen"] for e in entradas}
        if faltan:
            raise ErrorRAG(f"no están en el catálogo o no son indexables: {sorted(faltan)}")
    if hasta == 0:
        return 0

    rutas = descargar(ajustes, entradas, args.forzar)
    if hasta == 1:
        return 0

    extraidos = extraer(ajustes, entradas, rutas, args.forzar)
    if hasta == 2:
        return 0

    segmentados = segmentar(entradas, extraidos)
    if hasta == 3:
        return 0

    indexar(ajustes, entradas, segmentados, extraidos)
    return 0


if __name__ == "__main__":
    sys.exit(main())
