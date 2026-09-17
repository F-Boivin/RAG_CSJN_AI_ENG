"""Ingesta del corpus: de los PDF de la Secretaría al índice."""

from app.rag.ingesta.tokens import contar_tokens, crear_splitter

__all__ = ["contar_tokens", "crear_splitter"]
