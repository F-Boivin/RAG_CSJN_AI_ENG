"""Medir y cortar texto en tokens del modelo de embeddings.

Escribir el índice es trabajo de `indice.Constructor`; cortar un documento en fragmentos, de
`segmentacion`. Acá queda lo que los dos comparten con el servicio: cuántos tokens tiene un
texto y el splitter que corta con esa misma medida.
"""

from typing import Optional

import tiktoken
from langchain_text_splitters import RecursiveCharacterTextSplitter

import app.nucleo.constantes as cfg
from app.nucleo.config import obtener_ajustes


def contar_tokens(texto: str, modelo: Optional[str] = None) -> int:
    """Cuenta tokens con el tokenizador del modelo de embeddings."""
    modelo = modelo or obtener_ajustes().modelo_embeddings
    try:
        codificador = tiktoken.encoding_for_model(modelo)
    except KeyError:
        codificador = tiktoken.get_encoding("cl100k_base")
    return len(codificador.encode(texto))


def crear_splitter() -> RecursiveCharacterTextSplitter:
    """Splitter que mide en tokens y corta primero en el párrafo doble."""
    return RecursiveCharacterTextSplitter.from_tiktoken_encoder(
        encoding_name="cl100k_base",
        separators=cfg.SEPARADORES_PDF,
        chunk_size=cfg.TAMANO_CHUNK_TOKENS,
        chunk_overlap=cfg.SOLAPAMIENTO_CHUNK_TOKENS,
        keep_separator=True,
    )
