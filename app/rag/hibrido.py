"""Recuperación híbrida: léxico sobre FTS5 y búsqueda vectorial, fusionados por rango.

Los retrievers ven el mismo corpus —los fragmentos que la ingesta escribió en Chroma y en el
índice léxico— así que describen la misma base y la fusión dedupica por contenido. El léxico
encuentra la cita escrita literal, que el embedding diluye entre párrafos parecidos; el
vectorial encuentra la consulta parafraseada, que el léxico no matchea. `crear_hibrido` los
pesa por igual.

El lado vectorial emite sus dos spans: sin ellos, la recuperación sería un hueco de tiempo
adentro de la herramienta.

Verificado contra la documentación oficial:
- `EnsembleRetriever` vive en `langchain_classic` desde la v1 y fusiona por rango recíproco.
  https://reference.langchain.com/python/langchain-classic/retrievers/ensemble/EnsembleRetriever
"""

from typing import Optional

from langchain_chroma import Chroma
from langchain_classic.retrievers import EnsembleRetriever
from langchain_core.callbacks import (
    AsyncCallbackManagerForRetrieverRun,
    CallbackManagerForRetrieverRun,
)
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from pydantic import ConfigDict

import app.nucleo.constantes as cfg
import app.nucleo.mensajes as msj
from app.almacen.consulta import Lexico
from app.nucleo.config import obtener_ajustes
from app.observabilidad.trazas import (
    anotar_documentos,
    span_de_embedding,
    span_de_recuperacion,
)
from app.rag.lexico import RecuperadorLexico, tokenizar  # noqa: F401  (reexporta tokenizar)
from app.rag.ingesta.markdown import contar_tokens


class RecuperadorVectorial(BaseRetriever):
    """El lado vectorial del ensamble, con la búsqueda partida en sus dos mitades medibles.

    Embeber la consulta es una llamada de red al proveedor; buscar el vecino más cercano es
    trabajo local del índice. Medirlas juntas da un número que no se puede atribuir.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    vectorstore: Chroma
    k: int = cfg.RESULTADOS_RECUPERADOS

    async def _aget_relevant_documents(
        self, query: str, *, run_manager: AsyncCallbackManagerForRetrieverRun
    ) -> list[Document]:
        """Los `k` fragmentos más cercanos a la consulta."""
        if self.vectorstore.embeddings is None:
            raise RuntimeError(msj.ERROR_HERRAMIENTA_SIN_EMBEDDINGS)
        tokens = contar_tokens(query)
        with span_de_embedding("embeber_consulta", query,
                               obtener_ajustes().modelo_embeddings, tokens):
            vector = await self.vectorstore.embeddings.aembed_query(query)
        with span_de_recuperacion("chroma_vecinos", query, k=self.k) as span:
            documentos = await self.vectorstore.asimilarity_search_by_vector(vector, k=self.k)
            anotar_documentos(span, documentos)
        return documentos

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> list[Document]:
        """El camino sincrónico, que este retriever no ofrece."""
        # `BaseRetriever` lo declara abstracto, así que existe para fallar ruidoso: nadie del
        # sistema lo llama, y quien lo llamara bloquearía el event loop en silencio.
        raise NotImplementedError(msj.ERROR_RECUPERADOR_SINCRONICO)


def crear_hibrido(vectorstore: Chroma, lexico: Lexico,
                  top_k: Optional[int] = None) -> EnsembleRetriever:
    """El ensamble de los dos retrievers sobre el mismo corpus.

    Cada lado aporta más candidatos que los que se van a devolver: la fusión elige mejor
    viendo más de cada uno, y el recorte a `top_k` lo aplica quien consulta, después de
    fusionar.
    """
    top_k = top_k or cfg.RESULTADOS_RECUPERADOS
    candidatos = max(cfg.CANDIDATOS_POR_RETRIEVER, top_k)
    return EnsembleRetriever(
        retrievers=[
            RecuperadorLexico(lexico=lexico, k=candidatos),
            RecuperadorVectorial(vectorstore=vectorstore, k=candidatos),
        ],
        weights=[cfg.PESO_LEXICO, cfg.PESO_VECTORIAL],
    )
