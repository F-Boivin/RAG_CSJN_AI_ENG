"""Excepciones propias del proyecto.

Quien usa la ingesta o el agente captura estos tipos y no necesita conocer la API
interna de ChromaDB, LangChain ni OpenAI.
"""


class ErrorRAG(RuntimeError):
    """Falla de la ingesta o la base vectorial, con mensaje ya traducido."""


class ErrorDeAgente(RuntimeError):
    """Falla del agente al consultar el LLM; envuelve las excepciones del SDK."""


class ErrorDeAlmacenamiento(RuntimeError):
    """Falla al leer o escribir en Redis; envuelve las excepciones del cliente."""


class ErrorDeConfiguracion(RuntimeError):
    """Falta una variable de entorno o tiene un valor que el sistema no puede usar."""
