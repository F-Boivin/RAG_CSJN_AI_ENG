"""Ingesta del corpus: del markdown y de los PDF de la Secretaría al índice."""

from app.rag.ingesta.markdown import (
    contar_tokens,
    crear_splitter,
    fragmentar,
    leer_documentos,
)

__all__ = ["contar_tokens", "crear_splitter", "fragmentar", "leer_documentos"]
