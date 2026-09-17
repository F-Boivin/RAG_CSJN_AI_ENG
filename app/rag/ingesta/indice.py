"""Construcción y apertura del índice: Chroma para el vectorial, SQLite para el léxico.

La construcción es un script que corre una persona; el servicio solo abre lo que ya existe y
falla ruidoso si no cuadra. Antes el arranque construía el índice si faltaba, lo que ataba el
despliegue a tener la clave de OpenAI y a que la fuente estuviera arriba.

La unidad de trabajo es el documento. Reindexar uno borra sus fragmentos por `origen` en las
dos mitades y los reescribe, así que sumar un documento nuevo cuesta lo que ese documento y
correr la ingesta sin cambios no gasta un solo embedding.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from chromadb.errors import ChromaError
from langchain_chroma import Chroma

import app.nucleo.constantes as cfg
import app.nucleo.mensajes as msj
from app.almacen.consulta import Lexico
from app.almacen.escritura import EscritorLexico
from app.nucleo.config import obtener_ajustes
from app.nucleo.errores import ErrorDeAlmacenamiento, ErrorRAG
from app.nucleo.modelos import crear_embeddings

ARCHIVO_MANIFIESTO = "MANIFIESTO.json"
ARCHIVO_LEXICO = "lexico.sqlite3"
DIRECTORIO_CHROMA = "chroma"

# Metadatos que Chroma acepta: escalares. `citas_urls` viaja serializado a JSON, y `tokens`
# y `pagina` como enteros.
CLAVES_METADATA = ("origen", "seccion", "subseccion", "fuente", "pagina", "url_documento",
                   "tokens")


@dataclass
class Indice:
    """Las dos mitades del índice abiertas, con su manifiesto."""

    chroma: Chroma
    lexico: Lexico
    manifiesto: dict

    @property
    def fragmentos(self) -> int:
        return self.lexico.cantidad_fragmentos()

    @property
    def version(self) -> str:
        return self.manifiesto.get("huella", "")


def rutas(directorio: Path | str) -> tuple[Path, Path, Path]:
    """Las tres rutas del índice: Chroma, el léxico y el manifiesto."""
    base = Path(directorio)
    return base / DIRECTORIO_CHROMA, base / ARCHIVO_LEXICO, base / ARCHIVO_MANIFIESTO


def abrir_chroma(directorio: Path, escritura: bool = False,
                 manifiesto: Optional[dict] = None) -> Chroma:
    """La colección persistente de Chroma, con la métrica y las dimensiones configuradas.

    Con `manifiesto`, el modelo, las dimensiones y el nombre de la colección salen de ahí: un
    índice ya construido sabe con qué se hizo, y el entorno no puede saberlo mejor. Sin él
    —cuando se está construyendo— mandan los ajustes.
    """
    ajustes = obtener_ajustes()
    if manifiesto:
        ajustes = ajustes.model_copy(update={
            k: v for k, v in (
                ("modelo_embeddings", manifiesto.get("modelo_embeddings")),
                ("dimensiones_embeddings", manifiesto.get("dimensiones")),
                ("nombre_coleccion", manifiesto.get("coleccion")),
            ) if v
        })
    directorio.mkdir(parents=True, exist_ok=True)
    try:
        return Chroma(
            collection_name=ajustes.nombre_coleccion,
            embedding_function=crear_embeddings(ajustes),
            persist_directory=str(directorio),
            collection_metadata=cfg.METRICA_DISTANCIA,
        )
    except (ChromaError, OSError) as exc:
        raise ErrorDeAlmacenamiento(msj.ERROR_VECTORSTORE.format(detalle=exc)) from exc


def abrir(directorio: Path | str) -> Indice:
    """Abre un índice ya construido y comprueba que sirva.

    **El manifiesto manda sobre el entorno.** Un índice construido sabe con qué modelo, con
    cuántas dimensiones y en qué colección se hizo; una variable de entorno solo sabe lo que
    alguien escribió. Cuando el entorno declara algo distinto, eso es una contradicción y se
    corta ruidoso; cuando no declara nada, se usa lo que dice el índice.

    Sin esto, faltar `DIMENSIONES_EMBEDDINGS` en el despliegue era la peor falla posible: la
    guarda quedaba desarmada por su propio `if`, el proveedor devolvía vectores de 1536 contra
    una colección de 512, y el arranque no se caía. `/salud` contestaba «listo», la plataforma
    marcaba el deploy sano, y cada consulta moría en la recuperación después de haber cobrado
    el cupo del visitante y pagado la llamada de admisión.
    """
    dir_chroma, ruta_lexico, ruta_manifiesto = rutas(directorio)
    if not ruta_manifiesto.exists() or not ruta_lexico.exists():
        raise ErrorRAG(msj.ERROR_INDICE_AUSENTE.format(ruta=directorio))
    manifiesto = json.loads(ruta_manifiesto.read_text(encoding="utf-8"))
    ajustes = obtener_ajustes()

    esperadas = ajustes.dimensiones_embeddings
    tiene = manifiesto.get("dimensiones")
    if esperadas and tiene and int(tiene) != int(esperadas):
        raise ErrorRAG(msj.ERROR_INDICE_DIMENSION.format(
            ruta=directorio, tiene=tiene, espera=esperadas))
    # El modelo no tiene cómo declararse ausente —su default es un nombre válido—, así que lo
    # que se compara es el desacuerdo declarado. Dos modelos distintos dan vectores que no se
    # pueden comparar, y eso no se nota mirando los resultados.
    modelo = manifiesto.get("modelo_embeddings")
    if modelo and modelo != ajustes.modelo_embeddings:
        raise ErrorRAG(msj.ERROR_INDICE_MODELO.format(
            ruta=directorio, tiene=modelo, espera=ajustes.modelo_embeddings))

    lexico = Lexico(ruta_lexico, solo_lectura=True)
    if lexico.cantidad_fragmentos() == 0:
        raise ErrorRAG(msj.ERROR_INDICE_VACIO.format(ruta=directorio))
    return Indice(chroma=abrir_chroma(dir_chroma, manifiesto=manifiesto),
                  lexico=lexico, manifiesto=manifiesto)


class Constructor:
    """El índice abierto para escritura, documento por documento."""

    def __init__(self, directorio: Path | str):
        self.directorio = Path(directorio)
        dir_chroma, ruta_lexico, self.ruta_manifiesto = rutas(directorio)
        self.chroma = abrir_chroma(dir_chroma, escritura=True)
        self.lexico = EscritorLexico(ruta_lexico)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.cerrar()

    def cerrar(self) -> None:
        self.lexico.cerrar()

    def borrar_documento(self, origen: str) -> int:
        """Saca un documento de las dos mitades del índice."""
        try:
            existentes = self.chroma.get(where={"origen": origen}, include=[])["ids"]
            if existentes:
                self.chroma.delete(ids=existentes)
        except (ChromaError, OSError) as exc:
            raise ErrorDeAlmacenamiento(msj.ERROR_VECTORSTORE.format(detalle=exc)) from exc
        return self.lexico.borrar_documento(origen)

    def indexar_documento(self, fragmentos: list[dict], ficha: dict) -> None:
        """Escribe los fragmentos de un documento en las dos mitades.

        Los ids son estables (`origen#0000`), así que reindexar es un upsert y no acumula
        copias. Chroma recibe la metadata escalar; el mapa de citas va en el léxico, que es
        quien responde por el padrón.

        **El vector se calcula con la cadena de títulos del fragmento delante de su texto, y lo
        que se guarda es el texto solo.** Un sumario casi nunca repite el título que lo agrupa:
        «¿qué hay que hacer si rechazan el planteo de arbitrariedad?» apunta a la sección
        «Omisión de interponer recurso de queja ante el rechazo del planteo de arbitrariedad»,
        y sin el título el vector la dejaba en el puesto 33. Medido con 48 consultas
        etiquetadas, la subsección que responde entra entre los seis primeros en 19 de las 20
        del capítulo de sentencias arbitrarias, contra 16 sin los títulos, y en 27 de 28
        repartidas entre los siete capítulos, contra 26. Con solo el título de la sección, sin
        la cadena, eran 19 y 26.

        El texto guardado es el que lee el investigador y contra el que el verificador busca
        cada pasaje, y el lado léxico lo tiene que devolver idéntico para que el ensamble
        fusione los dos resultados.
        """
        self.borrar_documento(ficha["origen"])
        if not fragmentos:
            self.lexico.registrar_documento({**ficha, "fragmentos": 0})
            return
        embebidos = [f"{f['encabezado']}\n\n{f['texto']}" if f.get("encabezado") else f["texto"]
                     for f in fragmentos]
        metadatas = [
            {**{k: f.get(k) for k in CLAVES_METADATA if f.get(k) is not None},
             "citas_urls": json.dumps(f.get("citas_urls") or {}, ensure_ascii=False)}
            for f in fragmentos
        ]
        try:
            vectores = self.chroma.embeddings.embed_documents(embebidos)
            # La API pública de LangChain embebe el mismo texto que guarda; la colección
            # acepta los dos por separado.
            self.chroma._collection.upsert(
                ids=[f["id"] for f in fragmentos], embeddings=vectores,
                documents=[f["texto"] for f in fragmentos], metadatas=metadatas)
        except (ChromaError, OSError) as exc:
            raise ErrorDeAlmacenamiento(msj.ERROR_VECTORSTORE.format(detalle=exc)) from exc
        self.lexico.escribir_fragmentos(fragmentos)
        self.lexico.registrar_documento({**ficha, "fragmentos": len(fragmentos)})

    def cerrar_indice(self, resumen: dict) -> dict:
        """Completa los links faltantes, compacta y escribe el manifiesto."""
        completados = self.lexico.completar_links_faltantes()
        self.lexico.optimizar()
        ajustes = obtener_ajustes()
        manifiesto = {
            **resumen,
            "modelo_embeddings": ajustes.modelo_embeddings,
            "dimensiones": ajustes.dimensiones_embeddings,
            "coleccion": ajustes.nombre_coleccion,
            "links_sintetizados": completados,
        }
        self.ruta_manifiesto.parent.mkdir(parents=True, exist_ok=True)
        self.ruta_manifiesto.write_text(
            json.dumps(manifiesto, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return manifiesto
