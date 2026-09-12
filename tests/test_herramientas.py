"""Las funciones que sostienen la promesa anti-alucinación, probadas sin la base.

El parser de citas y los normalizadores son código puro: se ejercen con cadenas. Los casos
borde salen de mediciones sobre el corpus real —el cuadernillo abrevia las páginas, y un
horario o un resultado de votación se parecen a una cita— y son los que decidieron la forma
de las expresiones regulares.
"""

import pytest

import app.nucleo.mensajes as msj
from app.rag import herramientas
from tests.dobles import lexico_de_prueba


class TestCitasDelTexto:
    """El parser que decide qué es una cita en un texto libre."""

    def test_una_cita_simple(self):
        assert herramientas.citas_del_texto("Ver Fallos: 311:2437.") == {"311:2437"}

    def test_una_lista_de_citas(self):
        encontradas = herramientas.citas_del_texto("(Fallos: 315:356; 326:2759)")
        assert encontradas == {"315:356", "326:2759"}

    def test_la_pagina_abreviada_hereda_el_tomo(self):
        # Así cita el cuadernillo: la última es la página 3334 del tomo 326.
        encontradas = herramientas.citas_del_texto("(Fallos: 315:356; 326:2759 y 3334)")
        assert encontradas == {"315:356", "326:2759", "326:3334"}

    def test_un_horario_no_es_una_cita(self):
        assert herramientas.citas_del_texto("la audiencia de las 14:30") == set()

    def test_un_resultado_de_votacion_no_es_una_cita(self):
        assert herramientas.citas_del_texto("el voto fue 3:2") == set()

    def test_el_ancla_esta_en_la_palabra_fallos(self):
        # Sin el ancla, cualquier par de números con dos puntos entraría como cita.
        assert herramientas.citas_del_texto("el articulo 14:2 de la ley") == set()

    def test_una_url_no_dispara_el_ancla(self):
        texto = "https://sj.csjn.gov.ar/homeSJ/suplementos/3:1 no es una cita"
        assert herramientas.citas_del_texto(texto) == set()

    def test_un_texto_vacio_no_rompe(self):
        assert herramientas.citas_del_texto("") == set()
        assert herramientas.citas_del_texto(None) == set()

    def test_una_pagina_suelta_sin_tomo_previo_no_cuenta(self):
        assert herramientas.citas_del_texto("Fallos: 3334") == set()


class TestNormalizarYContar:
    """Lo que identifica a una cita y lo que exige que sea una sola."""

    @pytest.mark.parametrize("escrita", [
        "Fallos: 311:2437", "Fallos 311:2437", "311:2437", "  Fallos:  311:2437  ",
    ])
    def test_las_variantes_de_escritura_son_la_misma_cita(self, escrita):
        assert herramientas.normalizar_cita(escrita) == "311:2437"

    def test_una_cita_ilegible_normaliza_a_vacio(self):
        assert herramientas.normalizar_cita("la doctrina de Colalillo") == ""

    def test_cuantas_citas_distingue_una_de_varias(self):
        assert herramientas.cuantas_citas("Fallos: 311:2437") == 1
        assert herramientas.cuantas_citas("Fallos: 313:1045; 328:4597") == 2

    def test_un_campo_sin_numero_da_cero(self):
        # Cero y dos caen en ramas distintas del verificador: quien no citó nada recibe otro
        # mensaje que quien amontonó varias.
        assert herramientas.cuantas_citas("abc") == 0
        assert herramientas.cuantas_citas("") == 0


class TestLeerVeredicto:
    """La traducción de la salida de verificar_citas a {cita: existe}."""

    def test_lee_existe_y_no_existe(self):
        crudo = "Fallos: 311:2437 | EXISTE | http://x\nFallos: 999:9999 | NO EXISTE |"
        veredicto = herramientas.leer_veredicto(crudo)
        assert veredicto == {"Fallos: 311:2437": True, "Fallos: 999:9999": False}

    def test_una_cita_sin_link_igual_existe(self):
        # Las citas del cuerpo de la doctrina existen y no tienen URL registrada: leer la URL
        # como veredicto las daría por inventadas.
        veredicto = herramientas.leer_veredicto("Fallos: 211:958 | EXISTE |")
        assert veredicto == {"Fallos: 211:958": True}

    def test_una_linea_incompleta_se_ignora(self):
        assert herramientas.leer_veredicto("basura sin separador") == {}


class TestSubsecciones:
    """La tolerancia de nombres que evita rechazar subsecciones que existen."""

    @pytest.fixture(autouse=True)
    def corpus_falso(self, monkeypatch, tmp_path):
        monkeypatch.setattr(herramientas, "_lexico", lexico_de_prueba(
            tmp_path / "lexico.sqlite3", subsecciones=[
                "6.1.1 Concepto",
                "6.2.7 Exceso ritual manifiesto",
                "6.4.1 Obligación del a quo",
            ]))

    def test_el_nombre_exacto_existe(self):
        assert herramientas.subseccion_existe("6.2.7 Exceso ritual manifiesto")

    def test_sin_numeracion_tambien_existe(self):
        # El modelo escribe el título sin su número; exigir el exacto daría por inventada
        # una subsección que está en el corpus.
        assert herramientas.subseccion_existe("Exceso ritual manifiesto")

    def test_sin_tildes_tambien_existe(self):
        assert herramientas.subseccion_existe("Obligacion del a quo")

    def test_una_subseccion_inventada_no_existe(self):
        assert not herramientas.subseccion_existe("6.9.9 Doctrina inventada")

    def test_resolver_devuelve_el_nombre_del_indice(self):
        assert herramientas.resolver_subseccion("exceso ritual manifiesto") == \
            "6.2.7 Exceso ritual manifiesto"

    def test_resolver_prefiere_la_igualdad_exacta(self):
        assert herramientas.resolver_subseccion("6.1.1 Concepto") == "6.1.1 Concepto"

    def test_una_ambiguedad_devuelve_none_en_vez_de_adivinar(self, monkeypatch, tmp_path):
        monkeypatch.setattr(herramientas, "_lexico", lexico_de_prueba(
            tmp_path / "ambiguo.sqlite3", subsecciones=["6.1.1 Concepto", "6.3.1 Concepto"]))
        assert herramientas.resolver_subseccion("Concepto") is None


class TestLinkOficial:
    """La herramienta del redactor, acotada por cierre a lo verificado en esa corrida."""

    @pytest.fixture(autouse=True)
    def padron_falso(self, monkeypatch, tmp_path):
        monkeypatch.setattr(herramientas, "_lexico", lexico_de_prueba(
            tmp_path / "lexico.sqlite3", padron={
                "311:2437": "https://sj.csjn.gov.ar/fallo/311-2437",
                "315:1848": "https://sj.csjn.gov.ar/fallo/315-1848",
                "211:958": "",      # existe en el corpus, sin link registrado
                "330:1228": "https://sj.csjn.gov.ar/fallo/330-1228",
            }))

    async def test_devuelve_el_link_de_una_cita_aprobada(self):
        link_oficial = herramientas.crear_link_oficial(("Fallos: 311:2437",))
        salida = await link_oficial.ainvoke({"cita": "Fallos: 311:2437"})
        assert "sj.csjn.gov.ar/fallo/311-2437" in salida

    async def test_una_cita_real_pero_ajena_a_la_corrida_se_rechaza(self):
        # Verificado no es lo mismo que pertinente: 330:1228 está en el padrón, pero el
        # verificador no la aprobó en esta corrida.
        link_oficial = herramientas.crear_link_oficial(("Fallos: 311:2437",))
        salida = await link_oficial.ainvoke({"cita": "Fallos: 330:1228"})
        assert salida == msj.MENSAJE_CITA_NO_APROBADA.format(cita="Fallos: 330:1228")

    async def test_una_cita_ilegible_pide_el_formato(self):
        link_oficial = herramientas.crear_link_oficial(("Fallos: 311:2437",))
        salida = await link_oficial.ainvoke({"cita": "la doctrina de Colalillo"})
        assert salida == msj.MENSAJE_CITA_ILEGIBLE.format(cita="la doctrina de Colalillo")

    async def test_una_cita_verificada_sin_link_se_cita_igual(self):
        link_oficial = herramientas.crear_link_oficial(("Fallos: 211:958",))
        salida = await link_oficial.ainvoke({"cita": "Fallos: 211:958"})
        assert "Citala igual, sin" in salida


class TestContarLlamadas:
    """El conteo que hace visible el ciclo ReAct dentro de un nodo."""

    def test_cuenta_las_llamadas_pedidas_y_no_los_mensajes(self):
        class Mensaje:
            def __init__(self, tool_calls):
                self.tool_calls = tool_calls

        mensajes = [Mensaje([{"name": "a"}, {"name": "b"}]), Mensaje([]), Mensaje([{"name": "c"}])]
        assert herramientas.contar_llamadas(mensajes) == 3

    def test_un_historial_sin_llamadas_da_cero(self):
        assert herramientas.contar_llamadas([]) == 0
