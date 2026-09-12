"""El ensamble: qué aporta cada lado y cómo se combinan.

El corpus es sintético, los embeddings son deterministas y falsos, y el índice léxico es un
SQLite en `tmp_path`, así que la prueba corre sin API, sin red y siempre da lo mismo. Eso
alcanza para lo que importa acá: que el lado léxico encuentre la cita escrita literal —que un
embedding diluye entre párrafos parecidos— y que la fusión la conserve.
"""

import pytest
from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_core.embeddings.fake import DeterministicFakeEmbedding

from app.nucleo import constantes as cfg
from app.rag.hibrido import RecuperadorVectorial, crear_hibrido
from app.rag.lexico import RecuperadorLexico, tokenizar
from tests.dobles import lexico_de_prueba

CITA = "Fallos: 316:2343"

CORPUS = [
    Document(page_content="La arbitrariedad exige un apartamiento inequivoco de la solucion "
                          "normativa prevista para el caso.",
             metadata={"subseccion": "6.1.1 Concepto", "origen": "6-1-concepto.md"}),
    Document(page_content="El exceso ritual manifiesto configura una causal autonoma cuando la "
                          "forma sacrifica la verdad juridica objetiva.",
             metadata={"subseccion": "6.2.7 Exceso ritual", "origen": "6-2-causales.md"}),
    Document(page_content=f"La doctrina de la arbitrariedad no habilita una tercera instancia "
                          f"ordinaria. Fuente: {CITA}",
             metadata={"subseccion": "6.3.1 Improcedencia", "origen": "6-3-improcedencia.md"}),
    Document(page_content="El a quo debe fundamentar la admisibilidad del recurso extraordinario "
                          "federal al concederlo.",
             metadata={"subseccion": "6.4.1 Obligacion del a quo", "origen": "6-4-tramite.md"}),
    Document(page_content="El per saltum procede ante cuestiones de notoria gravedad institucional "
                          "que requieran una decision inmediata.",
             metadata={"subseccion": "6.4.5 Per saltum", "origen": "6-4-tramite.md"}),
    Document(page_content="La sentencia debe ser una derivacion razonada del derecho vigente con "
                          "arreglo a las circunstancias comprobadas de la causa.",
             metadata={"subseccion": "6.1.2 Fundamento", "origen": "6-1-concepto.md"}),
]


@pytest.fixture
def indice() -> Chroma:
    """Un Chroma efímero con embeddings deterministas: sin API y sin disco."""
    return Chroma.from_documents(
        CORPUS, DeterministicFakeEmbedding(size=64), collection_name="prueba_hibrido")


@pytest.fixture
def lexico(tmp_path):
    """El mismo corpus en un índice FTS5 real."""
    return lexico_de_prueba(tmp_path / "lexico.sqlite3", fragmentos=[
        {"id": f"doc#{i:04d}", "origen": d.metadata["origen"], "seccion": "",
         "subseccion": d.metadata["subseccion"], "fuente": "cuadernillo", "pagina": i,
         "texto": d.page_content, "citas_urls": {}}
        for i, d in enumerate(CORPUS)
    ])


def rango(documentos, aguja: str = CITA):
    """La posición del fragmento buscado, o None si no está."""
    for posicion, doc in enumerate(documentos, start=1):
        if aguja in doc.page_content:
            return posicion
    return None


class TestTokenizar:
    """El tokenizador que despega la puntuación de las citas."""

    def test_despega_el_punto_y_coma_de_una_cita(self):
        assert tokenizar("Fallos: 316:2343;") == ["Fallos:", "316:2343"]

    def test_saca_los_parentesis(self):
        assert tokenizar("(Fallos: 316:2343)") == ["Fallos:", "316:2343"]

    def test_deja_intacto_lo_que_no_tiene_puntuacion_pegada(self):
        assert tokenizar("exceso ritual manifiesto") == ["exceso", "ritual", "manifiesto"]


class TestAporteLexico:
    """La cita escrita literal: lo que el ensamble gana sobre el vectorial solo."""

    async def test_el_lado_lexico_la_pone_primera(self, lexico):
        recuperador = RecuperadorLexico(lexico=lexico, k=4)
        assert rango(await recuperador.ainvoke(CITA)) == 1

    async def test_el_lado_vectorial_no_la_encuentra(self, indice):
        vectorial = RecuperadorVectorial(vectorstore=indice, k=4)
        assert rango(await vectorial.ainvoke(CITA)) is None

    async def test_el_hibrido_la_conserva(self, indice, lexico):
        hibrido = crear_hibrido(indice, lexico)
        assert rango(await hibrido.ainvoke(CITA)) is not None


class TestFusion:
    """Cómo se combinan los dos lados."""

    async def test_el_ensamble_usa_el_camino_async_del_lado_vectorial(self, indice, lexico, monkeypatch):
        llamadas = []
        original = RecuperadorVectorial._aget_relevant_documents

        async def espiar(self, query, *, run_manager):
            llamadas.append(query)
            return await original(self, query, run_manager=run_manager)

        monkeypatch.setattr(RecuperadorVectorial, "_aget_relevant_documents", espiar)
        await crear_hibrido(indice, lexico).ainvoke("arbitrariedad")
        assert llamadas == ["arbitrariedad"]

    async def test_el_recorte_se_aplica_despues_de_fusionar(self, indice, lexico):
        # Cada lado aporta más candidatos que los que se devuelven: la fusión elige entre
        # todos y recién ahí se recorta.
        fusionados = await crear_hibrido(indice, lexico).ainvoke("arbitrariedad")
        assert len(fusionados) > 2
        assert len(fusionados[:2]) == 2

    async def test_los_metadatos_sobreviven_a_la_fusion(self, indice, lexico):
        # `fallos_citados` y el padrón dependen de la subsección.
        fusionados = await crear_hibrido(indice, lexico).ainvoke("exceso ritual")
        assert all(d.metadata.get("subseccion") for d in fusionados)

    async def test_los_dos_lados_ven_el_mismo_corpus(self, indice, lexico):
        fusionados = await crear_hibrido(indice, lexico).ainvoke("arbitrariedad")
        textos = {d.page_content for d in CORPUS}
        assert all(d.page_content in textos for d in fusionados)

    def test_los_pesos_son_los_configurados(self, indice, lexico):
        hibrido = crear_hibrido(indice, lexico)
        assert hibrido.weights == [cfg.PESO_LEXICO, cfg.PESO_VECTORIAL]

    def test_cada_lado_aporta_mas_candidatos_que_el_resultado(self, indice, lexico):
        hibrido = crear_hibrido(indice, lexico, top_k=2)
        assert all(getattr(r, "k", 0) >= cfg.CANDIDATOS_POR_RETRIEVER for r in hibrido.retrievers)


class TestCaminoSincronico:
    """El retriever vectorial es async: el camino sync duele en vez de bloquear."""

    def test_invoke_sincronico_levanta_not_implemented(self, indice):
        with pytest.raises(NotImplementedError):
            RecuperadorVectorial(vectorstore=indice, k=2).invoke("lo que sea")

    async def test_sin_embeddings_falla_con_su_mensaje(self, monkeypatch, indice):
        monkeypatch.setattr(type(indice), "embeddings", property(lambda self: None))
        with pytest.raises(RuntimeError, match="embeddings"):
            await RecuperadorVectorial(vectorstore=indice, k=2).ainvoke("consulta")
