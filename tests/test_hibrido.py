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
from app.rag.hibrido import RecuperadorVectorial, crear_hibrido, repartir
from app.rag.lexico import RecuperadorLexico, tokenizar
from tests.dobles import lexico_de_prueba

CITA = "Fallos: 316:2343"

DOCTRINA = [
    Document(page_content="La arbitrariedad exige un apartamiento inequivoco de la solucion "
                          "normativa prevista para el caso.",
             metadata={"subseccion": "6.1.1 Concepto", "origen": "6-1-concepto.md",
                       "fuente": "cuadernillo"}),
    Document(page_content="El exceso ritual manifiesto configura una causal autonoma cuando la "
                          "forma sacrifica la verdad juridica objetiva.",
             metadata={"subseccion": "6.2.7 Exceso ritual", "origen": "6-2-causales.md",
                       "fuente": "cuadernillo"}),
    Document(page_content=f"La doctrina de la arbitrariedad no habilita una tercera instancia "
                          f"ordinaria. Fuente: {CITA}",
             metadata={"subseccion": "6.3.1 Improcedencia", "origen": "6-3-improcedencia.md",
                       "fuente": "cuadernillo"}),
    Document(page_content="El a quo debe fundamentar la admisibilidad del recurso extraordinario "
                          "federal al concederlo.",
             metadata={"subseccion": "6.4.1 Obligacion del a quo", "origen": "6-4-tramite.md",
                       "fuente": "cuadernillo"}),
    Document(page_content="El per saltum procede ante cuestiones de notoria gravedad institucional "
                          "que requieran una decision inmediata.",
             metadata={"subseccion": "6.4.5 Per saltum", "origen": "6-4-tramite.md",
                       "fuente": "cuadernillo"}),
    Document(page_content="La sentencia debe ser una derivacion razonada del derecho vigente con "
                          "arreglo a las circunstancias comprobadas de la causa.",
             metadata={"subseccion": "6.1.2 Fundamento", "origen": "6-1-concepto.md",
                       "fuente": "nota"}),
]

# El texto de las sentencias completas: la otra naturaleza del corpus. Dicen lo mismo que la
# doctrina y con las mismas palabras, que es exactamente el caso donde el volumen le ganaba.
SENTENCIAS = [
    Document(page_content="Que la arbitrariedad alegada por el recurrente exige un apartamiento "
                          "inequivoco de la solucion normativa, segun consta a fs. 412.",
             metadata={"subseccion": "Suplemento · pags. 401-410", "origen": "suplemento-1",
                       "fuente": "suplemento"}),
    Document(page_content="Que el exceso ritual invocado a fs. 88 no se configura en la especie, "
                          "sin que la forma haya sacrificado la verdad juridica objetiva.",
             metadata={"subseccion": "Suplemento · pags. 81-90", "origen": "suplemento-2",
                       "fuente": "suplemento"}),
]

CORPUS = DOCTRINA + SENTENCIAS


@pytest.fixture
def indice(request) -> Chroma:
    """Un Chroma efímero con embeddings deterministas: sin API y sin disco.

    La colección lleva el nombre del test y se borra al terminar. El cliente efímero de Chroma
    es uno solo por proceso, así que con un nombre fijo cada test agrega otra copia del corpus
    a la misma colección y los resultados pasan a depender del orden en que corrieron.
    """
    nombre = f"hibrido_{request.node.name}"[:62].rstrip("_")
    almacen = Chroma.from_documents(
        CORPUS, DeterministicFakeEmbedding(size=64), collection_name=nombre)
    yield almacen
    almacen.delete_collection()


@pytest.fixture
def lexico(tmp_path):
    """El mismo corpus en un índice FTS5 real."""
    return lexico_de_prueba(tmp_path / "lexico.sqlite3", fragmentos=[
        {"id": f"doc#{i:04d}", "origen": d.metadata["origen"], "seccion": "",
         "subseccion": d.metadata["subseccion"], "fuente": d.metadata["fuente"], "pagina": i,
         "texto": d.page_content, "citas_urls": {}}
        for i, d in enumerate(CORPUS)
    ])


def fuentes(documentos) -> list[str]:
    """El pool del que salió cada documento, en orden."""
    return [d.metadata.get("fuente", "") for d in documentos]


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
        # Dos, uno por pool, y los dos por el camino async.
        assert llamadas == ["arbitrariedad", "arbitrariedad"]

    async def test_el_recorte_se_aplica_despues_de_fusionar(self, indice, lexico):
        # Cada lado aporta más candidatos que los que se devuelven: la fusión elige entre
        # todos y recién ahí se recorta.
        fusionados = await crear_hibrido(indice, lexico).ainvoke("arbitrariedad")
        assert len(fusionados) > 2
        assert len(fusionados[:2]) == 2

    async def test_los_metadatos_sobreviven_a_la_fusion(self, indice, lexico):
        # La subsección es la procedencia que se le muestra al lector debajo de cada cita.
        fusionados = await crear_hibrido(indice, lexico).ainvoke("exceso ritual")
        assert all(d.metadata.get("subseccion") for d in fusionados)

    async def test_los_dos_lados_ven_el_mismo_corpus(self, indice, lexico):
        fusionados = await crear_hibrido(indice, lexico).ainvoke("arbitrariedad")
        textos = {d.page_content for d in CORPUS}
        assert all(d.page_content in textos for d in fusionados)

    def test_los_dos_lados_pesan_igual_dentro_de_cada_pool(self, indice, lexico):
        # La paridad léxico/vectorial está medida aparte, y separar pools no la toca.
        hibrido = crear_hibrido(indice, lexico)
        for ensamble in (hibrido.doctrina, hibrido.sentencias):
            assert ensamble.weights == [cfg.PESO_LEXICO, cfg.PESO_VECTORIAL]

    def test_cada_lado_aporta_mas_candidatos_que_el_resultado(self, indice, lexico):
        hibrido = crear_hibrido(indice, lexico, top_k=2)
        lados = hibrido.doctrina.retrievers + hibrido.sentencias.retrievers
        assert all(getattr(r, "k", 0) >= cfg.CANDIDATOS_POR_RETRIEVER for r in lados)


class TestReparto:
    """La cuota: cuántos lugares le tocan a cada pool."""

    def test_el_reparto_por_defecto_es_el_medido(self):
        assert repartir(cfg.RESULTADOS_RECUPERADOS) == (cfg.CUOTA_DOCTRINA,
                                                        cfg.CUOTA_SENTENCIAS)

    def test_reparte_cualquier_cantidad(self):
        # `buscar_doctrina` acepta la cantidad como argumento del modelo, así que la cuota
        # tiene que valer para todo el rango y no solo para el k de por defecto.
        for cantidad in range(1, cfg.MAXIMO_RESULTADOS + 1):
            doctrina, sentencias = repartir(cantidad)
            assert doctrina + sentencias == cantidad
            assert doctrina >= 0 and sentencias >= 0

    def test_la_doctrina_nunca_queda_en_minoria(self):
        for cantidad in range(1, cfg.MAXIMO_RESULTADOS + 1):
            doctrina, sentencias = repartir(cantidad)
            assert doctrina >= sentencias

    def test_con_un_solo_lugar_lo_toma_la_doctrina(self):
        assert repartir(1) == (1, 0)


class TestPools:
    """El corpus tiene dos naturalezas y cada una tiene su cuota de lugares.

    Los suplementos son el 93% de los fragmentos del corpus real, y mientras compitieron de
    igual a igual coparon el top-k de toda consulta temática: «impuesto al valor agregado»
    devolvía el impuesto al azúcar de 1871, y el sistema publicaba cuatro fallos anteriores a
    la creación del IVA. Pesarlos menos cambia la falla de lado y borra las materias que solo
    ellos cubren. Acá el corpus sintético reproduce la competencia en chico: cada sentencia
    dice lo mismo que un fragmento de doctrina y con las mismas palabras.
    """

    def test_cada_pool_ve_solo_lo_suyo(self, indice, lexico):
        hibrido = crear_hibrido(indice, lexico)
        assert [tuple(r.fuentes) for r in hibrido.doctrina.retrievers] == [
            cfg.FUENTES_DOCTRINA, cfg.FUENTES_DOCTRINA]
        assert [tuple(r.fuentes) for r in hibrido.sentencias.retrievers] == [
            cfg.FUENTES_SENTENCIAS, cfg.FUENTES_SENTENCIAS]

    async def test_los_dos_pools_entran_al_resultado(self, indice, lexico):
        # Que las sentencias estén es el punto de la cuota: son dueñas de materias enteras
        # —Habeas Corpus, Movilidad Jubilatoria— que ninguna nota cubre.
        recuperados = await crear_hibrido(indice, lexico).recuperar("exceso ritual", 4)
        assert set(fuentes(recuperados)) & set(cfg.FUENTES_DOCTRINA)
        assert "suplemento" in fuentes(recuperados)

    async def test_respeta_la_cuota_de_cada_pool(self, indice, lexico):
        recuperados = await crear_hibrido(indice, lexico).recuperar("exceso ritual", 4)
        de_doctrina, de_sentencias = repartir(4)
        conteo = fuentes(recuperados)
        assert sum(1 for f in conteo if f in cfg.FUENTES_DOCTRINA) == de_doctrina
        assert conteo.count("suplemento") == de_sentencias

    async def test_la_doctrina_va_primero(self, indice, lexico):
        recuperados = await crear_hibrido(indice, lexico).recuperar("exceso ritual", 4)
        orden = fuentes(recuperados)
        assert all(f in cfg.FUENTES_DOCTRINA for f in orden[: repartir(4)[0]])

    async def test_un_pool_corto_lo_completa_el_otro(self, indice, lexico):
        # El corpus sintético tiene dos sentencias: pedir ocho deja cuota de tres sin llenar,
        # y devolver siete cuando hay ocho fragmentos le sacaría material al investigador.
        recuperados = await crear_hibrido(indice, lexico).recuperar("arbitrariedad", 8)
        assert len(recuperados) == len(CORPUS)
        assert fuentes(recuperados).count("suplemento") == len(SENTENCIAS)

    async def test_no_devuelve_el_mismo_fragmento_dos_veces(self, indice, lexico):
        recuperados = await crear_hibrido(indice, lexico).recuperar("arbitrariedad", 8)
        textos = [d.page_content for d in recuperados]
        assert len(textos) == len(set(textos))


class TestFiltroPorFuente:
    """El filtro que hace posible consultar un pool solo."""

    async def test_el_lexico_devuelve_solo_su_pool(self, lexico):
        recuperador = RecuperadorLexico(lexico=lexico, k=10,
                                        fuentes=cfg.FUENTES_SENTENCIAS)
        documentos = await recuperador.ainvoke("arbitrariedad exceso ritual")
        assert documentos
        assert set(fuentes(documentos)) == {"suplemento"}

    async def test_el_vectorial_devuelve_solo_su_pool(self, indice):
        recuperador = RecuperadorVectorial(vectorstore=indice, k=10,
                                           fuentes=cfg.FUENTES_SENTENCIAS)
        documentos = await recuperador.ainvoke("arbitrariedad")
        assert documentos
        assert set(fuentes(documentos)) == {"suplemento"}

    async def test_sin_fuentes_ven_el_corpus_entero(self, indice, lexico):
        por_termino = await RecuperadorLexico(lexico=lexico, k=10).ainvoke("arbitrariedad")
        por_vector = await RecuperadorVectorial(vectorstore=indice, k=10).ainvoke("arbitrariedad")
        assert "suplemento" in fuentes(por_termino)
        assert set(fuentes(por_vector)) > {"suplemento"}


class TestCaminoSincronico:
    """El retriever vectorial es async: el camino sync duele en vez de bloquear."""

    def test_invoke_sincronico_levanta_not_implemented(self, indice):
        with pytest.raises(NotImplementedError):
            RecuperadorVectorial(vectorstore=indice, k=2).invoke("lo que sea")

    async def test_sin_embeddings_falla_con_su_mensaje(self, monkeypatch, indice):
        monkeypatch.setattr(type(indice), "embeddings", property(lambda self: None))
        with pytest.raises(RuntimeError, match="embeddings"):
            await RecuperadorVectorial(vectorstore=indice, k=2).ainvoke("consulta")
