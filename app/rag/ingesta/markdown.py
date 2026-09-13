"""El corpus en markdown: lo lee de /data y lo fragmenta por extractos.

El chunking respeta la unidad semantica del cuadernillo: cada extracto de doctrina con su
cita es indivisible, y el splitter solo lo parte si por si solo supera el techo de tokens.
La seccion y el subtitulo viajan como metadatos de cada fragmento.

Escribir el indice es trabajo de `indice.Constructor`, que atiende igual a este corpus y a
los PDF de la Secretaria.
"""

import json
from typing import Optional
import re
from pathlib import Path

import tiktoken
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

import app.nucleo.constantes as cfg
import app.nucleo.mensajes as msj
from app.nucleo.config import obtener_ajustes
from app.nucleo.errores import ErrorRAG

PATRON_SUBTITULO = re.compile(r"^#{2,6}\s+(.*)$", re.MULTILINE)
PATRON_LINK_MD = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")
PATRON_CITAS = re.compile(rf"^{cfg.ETIQUETA_FUENTE}\s*(.+)$", re.MULTILINE)
CLAVE_URLS = "_urls"


def contar_tokens(texto: str, modelo: Optional[str] = None) -> int:
    """Cuenta tokens con el tokenizador del modelo de embeddings."""
    modelo = modelo or obtener_ajustes().modelo_embeddings
    try:
        codificador = tiktoken.encoding_for_model(modelo)
    except KeyError:
        codificador = tiktoken.get_encoding("cl100k_base")
    return len(codificador.encode(texto))


def _quitar_hipervinculos(texto: str) -> tuple[str, dict[str, str]]:
    """Deja las citas en texto plano y devuelve el mapa cita -> URL.

    Cada URL de la CSJN pesa ~120 caracteres. Dejarlas en el texto que se indexa
    ensucia el embedding y, en los extractos muy citados, hace que la sola linea de
    fuentes supere el techo de tokens: ahi el splitter termina separando la doctrina
    de su cita, que es justo lo que este diseño quiere evitar.
    """
    urls: dict[str, str] = {}

    def reemplazar(coincidencia: re.Match) -> str:
        cita, url = coincidencia.group(1), coincidencia.group(2)
        urls.setdefault(cita, url)
        return cita

    return PATRON_LINK_MD.sub(reemplazar, texto), urls


def leer_documentos(directorio: Path = cfg.DIRECTORIO_DATA) -> list[Document]:
    """Un Document por archivo del dataset, con su seccion y sus URLs como metadato."""
    archivos = sorted(directorio.glob(cfg.PATRON_DOCUMENTOS))
    if not archivos:
        # `ErrorRAG` y no `FileNotFoundError`: el worker atrapa los errores del proyecto, y
        # una excepción sin traducir lo mata con un stack trace en vez del mensaje preparado.
        raise ErrorRAG(
            msj.MENSAJE_SIN_DATASET.format(patron=cfg.PATRON_DOCUMENTOS, ruta=directorio)
        )
    documentos = []
    for archivo in archivos:
        try:
            contenido = archivo.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise ErrorRAG(
                msj.ERROR_LECTURA_DATASET.format(archivo=archivo.name, detalle=error)
            ) from error
        if not contenido.strip():
            raise ErrorRAG(msj.ERROR_DOCUMENTO_VACIO.format(archivo=archivo.name))
        texto, urls = _quitar_hipervinculos(contenido)
        titulo = texto.splitlines()[0].lstrip("# ").strip()
        documentos.append(
            Document(
                page_content=texto,
                metadata={"origen": archivo.name, "seccion": titulo, CLAVE_URLS: urls},
            )
        )
    return documentos


def crear_splitter(separadores=None) -> RecursiveCharacterTextSplitter:
    """Splitter que mide en tokens y corta primero en el limite entre extractos.

    `add_start_index` hace que cada fragmento sepa en que posicion del documento
    empieza: buscarlo despues por su texto no sirve, porque la doctrina repite
    parrafos casi identicos y la busqueda cae en la subseccion equivocada.

    Los separadores entran por parametro porque el markdown y el PDF cortan distinto: el
    corpus en markdown separa sus extractos con `---`, y un PDF no tiene ese limite.
    """
    return RecursiveCharacterTextSplitter.from_tiktoken_encoder(
        encoding_name="cl100k_base",
        separators=separadores or cfg.SEPARADORES,
        chunk_size=cfg.TAMANO_CHUNK_TOKENS,
        chunk_overlap=cfg.SOLAPAMIENTO_CHUNK_TOKENS,
        keep_separator=True,
        add_start_index=True,
    )


def _subtitulo_vigente(texto_completo: str, posicion: int) -> str:
    """Ultimo subtitulo Markdown que precede a esa posicion del documento.

    Si ningun subtitulo precede a la posicion (el primer fragmento arranca antes
    del primer `###`), vale el primer subtitulo del documento: en este dataset el
    contenido bajo el titulo principal pertenece siempre a esa primera
    subseccion, y `subseccion` es la procedencia que el sistema le muestra al lector
    debajo de cada cita — un valor vacio deja la cita sin decir de donde salio.
    """
    titulos = [(m.start(), m.group(1)) for m in PATRON_SUBTITULO.finditer(texto_completo)]
    vigente = ""
    for inicio, titulo in titulos:
        if inicio <= posicion:
            vigente = titulo
        else:
            break
    if not vigente and titulos:
        vigente = titulos[0][1]
    return vigente


def _posicion_en_documento(completo: str, fragmento: Document) -> int:
    """Donde empieza el fragmento dentro de su documento.

    `start_index` es la fuente principal, pero el splitter devuelve -1 cuando el
    fragmento no aparece literal en el original (pasa con los que arrancan en el
    separador de extractos): ahi se busca por el texto sin el separador.
    """
    posicion = fragmento.metadata.get("start_index", -1)
    if posicion >= 0:
        return posicion
    aguja = fragmento.page_content.lstrip("-# \n")[:120]
    return max(completo.find(aguja), 0)


def fragmentar(documentos: list[Document]) -> list[Document]:
    """Aplica el splitter y enriquece cada fragmento con su subtitulo y sus citas."""
    splitter = crear_splitter()
    fragmentos = splitter.split_documents(documentos)
    texto_por_origen = {doc.metadata["origen"]: doc.page_content for doc in documentos}

    for fragmento in fragmentos:
        completo = texto_por_origen[fragmento.metadata["origen"]]
        posicion = _posicion_en_documento(completo, fragmento)
        # Chroma solo acepta metadatos escalares: el mapa de URLs de las citas que
        # aparecen en ESTE fragmento viaja serializado.
        urls_documento = fragmento.metadata.pop(CLAVE_URLS, {})
        citas = {
            cita.strip()
            for linea in PATRON_CITAS.findall(fragmento.page_content)
            for cita in linea.split(";")
            if cita.strip()
        }
        fragmento.metadata["citas_urls"] = json.dumps(
            {cita: urls_documento[cita] for cita in citas if cita in urls_documento},
            ensure_ascii=False,
        )
        fragmento.metadata["start_index"] = posicion
        fragmento.metadata["subseccion"] = _subtitulo_vigente(completo, posicion)
        fragmento.metadata["tokens"] = contar_tokens(fragmento.page_content)
    return fragmentos
