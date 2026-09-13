"""Recuperación híbrida: léxico sobre FTS5 y búsqueda vectorial, fusionados por rango.

Los retrievers ven el mismo corpus —los fragmentos que la ingesta escribió en Chroma y en el
índice léxico— así que describen la misma base y la fusión dedupica por contenido. El léxico
encuentra la cita escrita literal, que el embedding diluye entre párrafos parecidos; el
vectorial encuentra la consulta parafraseada, que el léxico no matchea. `crear_hibrido` los
pesa por igual dentro de cada pool.

Los pools son dos —doctrina curada y compilaciones de sentencias— y cada uno tiene su cuota
de lugares en el resultado, porque el segundo es el 93% del corpus y le ganaba al primero por
volumen. Eso lo reparte `RecuperadorPorCuota`.

El lado vectorial emite sus dos spans: sin ellos, la recuperación sería un hueco de tiempo
adentro de la herramienta.

Verificado contra la documentación oficial:
- `EnsembleRetriever` vive en `langchain_classic` desde la v1 y fusiona por rango recíproco.
  https://reference.langchain.com/python/langchain-classic/retrievers/ensemble/EnsembleRetriever
"""

import asyncio
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
    # Con `fuentes`, este recuperador ve solo un pool del corpus. Vacío, ve el corpus entero.
    fuentes: tuple[str, ...] = ()

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
        pool = "+".join(self.fuentes) or "corpus"
        filtro = {"fuente": {"$in": list(self.fuentes)}} if self.fuentes else None
        with span_de_recuperacion("chroma_vecinos", query, k=self.k, pool=pool) as span:
            documentos = await self.vectorstore.asimilarity_search_by_vector(
                vector, k=self.k, filter=filtro)
            anotar_documentos(span, documentos)
        return documentos

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> list[Document]:
        """El camino sincrónico, que este retriever no ofrece."""
        # `BaseRetriever` lo declara abstracto, así que existe para fallar ruidoso: nadie del
        # sistema lo llama, y quien lo llamara bloquearía el event loop en silencio.
        raise NotImplementedError(msj.ERROR_RECUPERADOR_SINCRONICO)


def repartir(cantidad: int) -> tuple[int, int]:
    """Cuántos lugares le tocan a la doctrina y cuántos a las sentencias.

    Se calcula y no se lee de una constante porque `buscar_doctrina` acepta la cantidad como
    argumento: la cuota tiene que valer para cualquier `k`, no solo para el de por defecto.
    """
    de_sentencias = round(cantidad * cfg.PROPORCION_SENTENCIAS)
    return cantidad - de_sentencias, de_sentencias


class RecuperadorPorCuota(BaseRetriever):
    """Reparte los lugares del top-k entre los dos pools del corpus, en vez de sortearlos.

    **La competencia abierta entre pools la gana el que tiene más fragmentos.** Los
    suplementos son 24.145 contra 1.730 de doctrina, y con los dos compitiendo por los mismos
    lugares el investigador se quedaba sin la doctrina que responde la pregunta: «impuesto al
    valor agregado» le traía el impuesto al azúcar de 1871 y dos citas para toda la respuesta.

    Pesar la doctrina por encima arregla eso y rompe otra cosa, porque los suplementos son
    dueños de materias enteras —Habeas Corpus, Movilidad Jubilatoria, Marcas y Patentes— que
    ninguna nota cubre. Medido con 16 consultas etiquetadas, el peso pierde nueve de esas
    materias y la cuota no pierde ninguna. El detalle está en `constantes.py`.

    Cuando un pool viene corto, el otro completa: devolver menos de lo pedido le sacaría
    material al investigador sin que nadie gane nada.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    doctrina: BaseRetriever
    sentencias: BaseRetriever
    k: int = cfg.RESULTADOS_RECUPERADOS

    async def recuperar(self, consulta: str, cantidad: int) -> list[Document]:
        """Los `cantidad` fragmentos del corpus, repartidos entre los dos pools."""
        de_doctrina, de_sentencias = repartir(cantidad)
        with span_de_recuperacion("cuota_pools", consulta, k=cantidad,
                                  doctrina=de_doctrina, sentencias=de_sentencias) as span:
            # Los dos pools son consultas independientes contra la misma base: en paralelo,
            # la más lenta marca el tiempo en vez de sumarse a la otra.
            doctrina, sentencias = await asyncio.gather(
                self.doctrina.ainvoke(consulta), self.sentencias.ainvoke(consulta))
            elegidos = doctrina[:de_doctrina] + sentencias[:de_sentencias]
            if len(elegidos) < cantidad:
                sobrantes = doctrina[de_doctrina:] + sentencias[de_sentencias:]
                elegidos += sobrantes[: cantidad - len(elegidos)]
            anotar_documentos(span, elegidos)
        return elegidos

    async def _aget_relevant_documents(
        self, query: str, *, run_manager: AsyncCallbackManagerForRetrieverRun
    ) -> list[Document]:
        """El contrato de `BaseRetriever`, con la cantidad configurada."""
        return await self.recuperar(query, self.k)

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> list[Document]:
        """El camino sincrónico, que este retriever no ofrece."""
        raise NotImplementedError(msj.ERROR_RECUPERADOR_SINCRONICO)


def crear_hibrido(vectorstore: Chroma, lexico: Lexico,
                  top_k: Optional[int] = None) -> RecuperadorPorCuota:
    """El recuperador del servicio: dos ensambles híbridos, uno por pool, con su cuota.

    Cada ensamble fusiona su lado léxico y su lado vectorial por rango recíproco, con los dos
    lados pesados igual. Lo que ya no compite es un pool contra el otro: cada uno tiene sus
    lugares reservados en el resultado.

    Cada lado aporta más candidatos que los lugares que va a llenar, para que la fusión elija
    entre más de cada uno.
    """
    top_k = top_k or cfg.RESULTADOS_RECUPERADOS
    candidatos = max(cfg.CANDIDATOS_POR_RETRIEVER, top_k)

    def ensamble(fuentes: tuple[str, ...]) -> EnsembleRetriever:
        return EnsembleRetriever(
            retrievers=[
                RecuperadorLexico(lexico=lexico, k=candidatos, fuentes=fuentes),
                RecuperadorVectorial(vectorstore=vectorstore, k=candidatos, fuentes=fuentes),
            ],
            weights=[cfg.PESO_LEXICO, cfg.PESO_VECTORIAL],
        )

    return RecuperadorPorCuota(
        doctrina=ensamble(cfg.FUENTES_DOCTRINA),
        sentencias=ensamble(cfg.FUENTES_SENTENCIAS),
        k=top_k,
    )
