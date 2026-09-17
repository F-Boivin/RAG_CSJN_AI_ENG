"""El lado léxico del ensamble, sobre el índice FTS5 de SQLite.

Reemplaza a `rank-bm25`, que reconstruía su índice en memoria en cada arranque leyendo el
corpus entero: con 7.400 fragmentos eso son ~115 MB de RAM y varios segundos antes de la
primera consulta. FTS5 vive en el volumen junto al resto del índice y consulta desde disco.

La función de ranking es `bm25()` de FTS5, la misma familia Okapi con otra normalización. Lo
que la fusión consume son rangos y no puntajes, así que la diferencia de escala entre las dos
implementaciones no la alcanza.

Verificado contra la documentación oficial de SQLite:
- `bm25()` devuelve valores negativos, mejor cuanto más negativo.
  https://sqlite.org/fts5.html#the_bm25_function
"""

import asyncio

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
from app.observabilidad.trazas import anotar_documentos, span_de_recuperacion


def tokenizar(texto: str) -> list[str]:
    """Separa en palabras y despega la puntuación que se pega a las citas.

    Sin esto, `316:2343;` y `316:2343` son términos distintos y la cita escrita al final de
    una enumeración deja de matchear.
    """
    return [token.strip(";,.()") for token in texto.split()]


class RecuperadorLexico(BaseRetriever):
    """Búsqueda por palabra sobre FTS5, hermano async del `RecuperadorVectorial`.

    La consulta corre en `asyncio.to_thread`: `sqlite3` es sincrónico y bloquearía el event
    loop del servicio, que atiende varias corridas a la vez.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    lexico: Lexico
    k: int = cfg.RESULTADOS_RECUPERADOS

    async def _aget_relevant_documents(
        self, query: str, *, run_manager: AsyncCallbackManagerForRetrieverRun
    ) -> list[Document]:
        """Los `k` fragmentos que mejor matchean la consulta por término."""
        with span_de_recuperacion("fts5_lexico", query, k=self.k) as span:
            filas = await asyncio.to_thread(self.lexico.buscar, query, self.k)
            documentos = [
                Document(page_content=texto, metadata={**metadata, "id": ident})
                for ident, texto, metadata in filas
            ]
            anotar_documentos(span, documentos)
        return documentos

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> list[Document]:
        """El camino sincrónico, que este retriever no ofrece."""
        # `BaseRetriever` lo declara abstracto, así que existe para fallar ruidoso: nadie del
        # sistema lo llama, y quien lo llamara bloquearía el event loop en silencio.
        raise NotImplementedError(msj.ERROR_RECUPERADOR_SINCRONICO)
