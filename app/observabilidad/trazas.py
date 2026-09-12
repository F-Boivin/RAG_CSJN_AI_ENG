"""Trazas propias de los accesos a Chroma, que la instrumentacion automatica no ve.

LangSmith traza por el sistema de callbacks de LangChain, y una llamada directa al vectorstore
no pasa por ahi: sin estas trazas la recuperacion es un hueco de tiempo adentro de la
herramienta. Cada acceso queda con su `run_type`, asi que el dashboard lo clasifica igual que
a los que instrumenta la libreria.

`langsmith.trace` cuelga del run en curso por si solo. Sin `LANGSMITH_TRACING` activo no
registra nada y el codigo corre igual.

Documentacion: https://docs.langchain.com/langsmith/annotate-code
"""

from contextlib import contextmanager

from langsmith import trace


@contextmanager
def span_de_embedding(nombre: str, texto: str, modelo: str, tokens: int):
    """Envuelve la llamada que convierte texto en vector; es tiempo de red contra la API.

    Los tokens los cuenta el llamador con el tokenizador del modelo: la respuesta de
    embeddings no los informa, y sin ellos esta llamada no entra en el calculo de costo.
    """
    with trace(
        name=nombre,
        run_type="embedding",
        inputs={"texto": texto},
        metadata={"modelo": modelo, "tokens": tokens},
    ) as run:
        yield run


@contextmanager
def span_de_recuperacion(nombre: str, consulta: str, **detalles: str | int):
    """Envuelve una lectura de Chroma; es tiempo de proceso local, sin red de por medio.

    La traza queda abierta para que quien la use anote los documentos con `anotar_documentos`
    recien cuando los tiene.
    """
    with trace(
        name=nombre,
        run_type="retriever",
        inputs={"consulta": consulta},
        metadata=dict(detalles),
    ) as run:
        yield run


def anotar_documentos(run, documentos) -> None:
    """Escribe en la traza los documentos recuperados, con su subseccion."""
    run.add_outputs({
        "documentos": [
            {
                "page_content": getattr(doc, "page_content", str(doc)),
                "subseccion": getattr(doc, "metadata", {}).get("subseccion", ""),
            }
            for doc in documentos
        ]
    })
