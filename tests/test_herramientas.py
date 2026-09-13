"""Las funciones que sostienen la promesa anti-alucinación, probadas sin la base.

El parser de citas y los normalizadores son código puro: se ejercen con cadenas. Los casos
borde salen de mediciones sobre el corpus real —el cuadernillo abrevia las páginas, y un
horario o un resultado de votación se parecen a una cita— y son los que decidieron la forma
de las expresiones regulares.
"""

import json

import pytest
from langchain_core.tools import ToolException

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


class TestBuscarDoctrina:
    """La única herramienta del investigador, y el registro de lo que le sirvió.

    Lo que se prueba acá es el invariante que faltaba: **un fallo llega al investigador solo
    si está en el texto de un fragmento que la búsqueda devolvió**. Antes existía una segunda
    herramienta que repartía las citas de una subsección entera —264 para un top-k que traía
    una—, y de ahí salieron 13 de las 17 citas que el buscador publicó en producción.
    """

    PADRON = {"311:2437": "https://ejemplo/311-2437", "315:1848": "",
              "330:1228": "https://ejemplo/330-1228", "112:384": ""}

    @pytest.fixture(autouse=True)
    def corpus_falso(self, monkeypatch, tmp_path):
        monkeypatch.setattr(herramientas, "_lexico", lexico_de_prueba(
            tmp_path / "lexico.sqlite3", padron=dict(self.PADRON)))

    def recuperador(self, monkeypatch, documentos):
        """Un doble del recuperador por cuota: acá se prueba la herramienta, no el ensamble."""
        class Falso:
            async def recuperar(self, _consulta, cantidad):
                return documentos[:cantidad]

        monkeypatch.setattr(herramientas, "_hibrido", Falso())

    def doc(self, texto, subseccion="6.1.1 Concepto", citas_urls=None):
        from langchain_core.documents import Document
        metadata = {"subseccion": subseccion, "origen": "cuadernillo-6-1", "fuente": "cuadernillo"}
        if citas_urls is not None:
            metadata["citas_urls"] = json.dumps(citas_urls)
        return Document(page_content=texto, metadata=metadata)

    async def test_cada_fragmento_llega_con_los_fallos_que_cita(self, monkeypatch):
        self.recuperador(monkeypatch, [
            self.doc("La doctrina no habilita una tercera instancia (Fallos: 311:2437).")])
        lectura = herramientas.Lectura()
        salida = await herramientas.crear_buscar_doctrina(lectura).ainvoke(
            {"consulta": "arbitrariedad"})
        assert "Fallos citados acá: Fallos: 311:2437" in salida

    async def test_el_registro_anota_de_que_subseccion_salio_cada_fallo(self, monkeypatch):
        self.recuperador(monkeypatch, [
            self.doc("Sostiene (Fallos: 311:2437).", subseccion="6.2.7 Exceso ritual")])
        lectura = herramientas.Lectura()
        await herramientas.crear_buscar_doctrina(lectura).ainvoke({"consulta": "arbitrariedad"})
        assert lectura.citas == {"311:2437": "6.2.7 Exceso ritual"}

    async def test_la_primera_procedencia_es_la_que_queda(self, monkeypatch):
        self.recuperador(monkeypatch, [
            self.doc("Primero (Fallos: 311:2437).", subseccion="6.1.1 Concepto"),
            self.doc("Despues (Fallos: 311:2437).", subseccion="6.9.9 Otra"),
        ])
        lectura = herramientas.Lectura()
        await herramientas.crear_buscar_doctrina(lectura).ainvoke({"consulta": "arbitrariedad"})
        assert lectura.citas["311:2437"] == "6.1.1 Concepto"

    async def test_las_anotaciones_del_pdf_suman_a_lo_que_dice_el_texto(self, monkeypatch):
        # El link viene anclado al texto exacto de la cita, y esas anotaciones cubren más
        # citas que el regex: 2.440 contra 1.720 en las notas.
        self.recuperador(monkeypatch, [
            self.doc("Un parrafo sin ninguna cita escrita en el cuerpo.",
                     citas_urls={"330:1228": "https://ejemplo/330-1228"})])
        lectura = herramientas.Lectura()
        salida = await herramientas.crear_buscar_doctrina(lectura).ainvoke(
            {"consulta": "arbitrariedad"})
        assert "330:1228" in lectura.citas and "Fallos: 330:1228" in salida

    async def test_una_cita_que_el_padron_no_tiene_no_se_ofrece(self, monkeypatch):
        # El verificador no la iba a reconocer: ofrecerla es mandar al investigador a un
        # rechazo seguro. El texto del fragmento va tal cual —es el corpus—, y lo que no
        # aparece es el ofrecimiento.
        self.recuperador(monkeypatch, [self.doc("Sostiene (Fallos: 999:9999).")])
        lectura = herramientas.Lectura()
        salida = await herramientas.crear_buscar_doctrina(lectura).ainvoke(
            {"consulta": "arbitrariedad"})
        assert lectura.citas == {}
        assert msj.ENCABEZADO_CITAS_DEL_FRAGMENTO not in salida
        assert msj.MENSAJE_SIN_CITABLES in salida

    async def test_el_cierre_lista_todo_lo_citable(self, monkeypatch):
        self.recuperador(monkeypatch, [
            self.doc("Uno (Fallos: 315:1848)."), self.doc("Dos (Fallos: 311:2437)."),
        ])
        salida = await herramientas.crear_buscar_doctrina(herramientas.Lectura()).ainvoke({"consulta": "arbitrariedad"})
        assert msj.ENCABEZADO_CITABLES in salida
        cierre = salida.split(msj.ENCABEZADO_CITABLES)[1]
        assert "311:2437" in cierre and "315:1848" in cierre

    async def test_sin_una_sola_cita_lo_dice_en_vez_de_callarse(self, monkeypatch):
        self.recuperador(monkeypatch, [self.doc("Un parrafo de doctrina sin citas.")])
        salida = await herramientas.crear_buscar_doctrina(herramientas.Lectura()).ainvoke({"consulta": "arbitrariedad"})
        assert msj.MENSAJE_SIN_CITABLES in salida

    async def test_sin_resultados_avisa(self, monkeypatch):
        self.recuperador(monkeypatch, [])
        assert await herramientas.crear_buscar_doctrina(herramientas.Lectura()).ainvoke(
            {"consulta": "arbitrariedad"}) == msj.MENSAJE_SIN_RESULTADOS

    async def test_anota_en_que_fragmento_aparece_cada_fallo(self, monkeypatch):
        # Es lo que ata el pasaje a su fallo: el respaldo se busca solo en esos fragmentos.
        self.recuperador(monkeypatch, [
            self.doc("Uno (Fallos: 311:2437)."),
            self.doc("Dos (Fallos: 315:1848)."),
            self.doc("Tres, otra vez (Fallos: 311:2437)."),
        ])
        lectura = herramientas.Lectura()
        await herramientas.crear_buscar_doctrina(lectura).ainvoke({"consulta": "arbitrariedad"})
        huellas = {texto: huella for huella, texto in lectura.textos.items()}
        assert lectura.fragmentos_por_cita["311:2437"] == [
            huellas["Uno (Fallos: 311:2437)."], huellas["Tres, otra vez (Fallos: 311:2437)."]]
        assert lectura.fragmentos_por_cita["315:1848"] == [huellas["Dos (Fallos: 315:1848)."]]

    async def test_un_fragmento_sin_citas_no_queda_asociado_a_ningun_fallo(self, monkeypatch):
        self.recuperador(monkeypatch, [
            self.doc("Un parrafo de doctrina sin citas."), self.doc("Uno (Fallos: 311:2437).")])
        lectura = herramientas.Lectura()
        await herramientas.crear_buscar_doctrina(lectura).ainvoke({"consulta": "arbitrariedad"})
        assert len(lectura.textos) == 2
        assert sum(len(v) for v in lectura.fragmentos_por_cita.values()) == 1

    async def test_dos_corridas_no_comparten_registro(self, monkeypatch):
        # Dos consultas simultáneas en el mismo proceso: mezclarlas haría que el chequeo de
        # pertinencia apruebe citas que este investigador nunca vio.
        self.recuperador(monkeypatch, [self.doc("Sostiene (Fallos: 311:2437).")])
        una, otra = herramientas.Lectura(), herramientas.Lectura()
        await herramientas.crear_buscar_doctrina(una).ainvoke({"consulta": "arbitrariedad"})
        assert otra.citas == {} and una.citas != {}

    async def test_metadata_rota_no_tumba_la_busqueda(self, monkeypatch):
        from langchain_core.documents import Document
        roto = Document(page_content="Sostiene (Fallos: 311:2437).",
                        metadata={"subseccion": "6.1.1 Concepto", "citas_urls": "{no es json"})
        self.recuperador(monkeypatch, [roto])
        lectura = herramientas.Lectura()
        salida = await herramientas.crear_buscar_doctrina(lectura).ainvoke(
            {"consulta": "arbitrariedad"})
        assert lectura.citas == {"311:2437": "6.1.1 Concepto"}
        assert "311:2437" in salida

    async def test_una_caida_del_indice_vuelve_como_observacion(self, monkeypatch):
        class Caido:
            async def recuperar(self, *_a, **_k):
                raise OSError("indice caido")

        monkeypatch.setattr(herramientas, "_hibrido", Caido())
        herramienta = herramientas.crear_buscar_doctrina(herramientas.Lectura())
        assert herramienta.handle_tool_error is True
        with pytest.raises(ToolException):
            await herramienta.coroutine(consulta="arbitrariedad")
