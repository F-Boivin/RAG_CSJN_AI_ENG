"""Las piezas del servicio que se pueden probar sin montar nada: eventos, registro y costo.

`traducir` es pura, `anonimizar` es pura, y el contador de costo solo lee lo que el proveedor
devuelve. Las tres se ejercitan sin grafo, sin red y sin claves.
"""

import pytest

import app.nucleo.constantes as cfg
import app.nucleo.mensajes as msj
from app.grafo.estado import INVESTIGADOR, REDACTOR, SUPERVISOR, VERIFICADOR, Verificacion
from app.nucleo import precios
from app.servicio import registro
from app.servicio.costo import Contador
from app.servicio.eventos import fichas_de_citas, traducir
from tests.dobles import SUBSECCION, TEXTO_LEIDO


class TestTraducirEventos:
    """Del update de un nodo al evento que ve una persona."""

    def test_el_supervisor_anuncia_lo_que_viene(self):
        # El anuncio sale del supervisor y no del especialista: `updates` emite DESPUÉS de que
        # el nodo corre, así que "buscando doctrina" llegaría con la búsqueda ya terminada.
        evento = traducir(SUPERVISOR, {"siguiente": INVESTIGADOR, "intentos": 0})
        assert evento.nombre == "estado"
        assert evento.datos["fase"] == INVESTIGADOR

    def test_una_correccion_se_nombra_como_tal(self):
        evento = traducir(SUPERVISOR, {"siguiente": REDACTOR, "intentos": 1})
        assert evento.datos["fase"] == "corrigiendo"
        assert evento.datos["detalle"].startswith("Corrección 1 de 3")

    def test_la_ultima_correccion_no_pasa_del_tope(self):
        # El supervisor incrementa al delegar, así que en la última el contador ya está en el
        # tope: sumarle uno mostraba «Intento 4 de 3».
        evento = traducir(SUPERVISOR, {"siguiente": REDACTOR, "intentos": cfg.MAXIMO_INTENTOS})
        assert evento.datos["detalle"].startswith(
            f"Corrección {cfg.MAXIMO_INTENTOS} de {cfg.MAXIMO_INTENTOS}")

    def test_el_cierre_del_supervisor_no_produce_evento(self):
        assert traducir(SUPERVISOR, {"siguiente": "FINALIZAR", "completado": True}) is None

    def test_el_investigador_informa_cuantas_citas_propuso(self, investigacion):
        evento = traducir(INVESTIGADOR, {"investigaciones": (investigacion,)})
        assert "3" in evento.datos["detalle"]

    def test_el_verificador_informa_cuantas_existen(self):
        verificacion = Verificacion(verificadas=("Fallos: 311:2437", "Fallos: 315:1848"),
                                    inexistentes=(), aprobado=True, observaciones=())
        evento = traducir(VERIFICADOR, {"verificaciones": (verificacion,)})
        assert evento.datos["fase"] == "verificado"
        assert evento.datos["detalle"] == msj.FASE_VERIFICADO.format(verificadas=2, propuestas=2)

    def test_un_rechazo_dice_por_que(self):
        # El conteo por sí solo miente: una verificación con todas las citas ciertas puede
        # rechazar igual, y el reintento quedaría sin explicación en pantalla.
        verificacion = Verificacion(verificadas=("Fallos: 311:2437",),
                                    inexistentes=(), aprobado=False,
                                    observaciones=("la subseccion «X» no existe",))
        evento = traducir(VERIFICADOR, {"verificaciones": (verificacion,)})
        assert evento.datos["fase"] == "rechazado"
        assert "no existe" in evento.datos["detalle"]

    def test_un_nodo_desconocido_no_rompe(self):
        assert traducir("archivista", {"lo_que_sea": 1}) is None

    def test_una_actualizacion_vacia_no_rompe(self):
        assert traducir(INVESTIGADOR, {}) is None


class TestFichasDeCitas:
    """Lo que la interfaz muestra debajo del texto."""

    def test_cada_cita_usada_trae_su_link_del_padron(self, estado_terminado):
        padron = {"311:2437": "https://sjconsulta.csjn.gov.ar/uno",
                  "315:1848": "", "330:1228": "https://sjconsulta.csjn.gov.ar/tres"}
        fichas = fichas_de_citas(estado_terminado, padron)
        assert [f["fallo"] for f in fichas] == [
            "Fallos: 311:2437", "Fallos: 315:1848", "Fallos: 330:1228"]
        assert fichas[0]["url"].endswith("/uno")

    def test_una_cita_sin_link_igual_se_muestra(self, estado_terminado):
        fichas = fichas_de_citas(estado_terminado, {"311:2437": ""})
        assert fichas[0]["url"] == ""

    def test_la_afirmacion_viene_de_la_investigacion(self, estado_terminado):
        fichas = fichas_de_citas(estado_terminado, {})
        assert "sostiene la doctrina" in fichas[0]["afirmacion"]

    def test_la_ficha_trae_el_pasaje_que_respalda_la_cita(self, estado_terminado):
        # Es lo que el verificador ya comprobó contra el fragmento leído. Mostrarlo es lo que
        # le permite a quien lee juzgar el salto del pasaje a la afirmación, que es justo lo
        # que el sistema no comprueba.
        fichas = fichas_de_citas(estado_terminado, {})
        assert fichas[0]["respaldo"] == TEXTO_LEIDO

    def test_la_subseccion_sale_del_registro_y_no_del_modelo(self, estado_terminado):
        fichas = fichas_de_citas(estado_terminado, {})
        assert fichas[0]["subseccion"] == SUBSECCION

    def test_sin_redaccion_no_hay_fichas(self, estado_vacio):
        assert fichas_de_citas(estado_vacio, {}) == []


class TestAnonimizar:
    """Lo que se saca del texto de una consulta antes de que toque el disco."""

    @pytest.mark.parametrize("crudo, adentro", [
        ("mi DNI es 20.123.456 y quiero saber", "20.123.456"),
        ("mi cuit 20-20123456-3 figura ahi", "20-20123456-3"),
        ("escribime a felipe@ejemplo.com.ar", "felipe@ejemplo.com.ar"),
        ("llamame al +54 9 11 4444-5555", "4444-5555"),
    ])
    def test_saca_el_dato_personal(self, crudo, adentro):
        limpio = registro.anonimizar(crudo)
        assert adentro not in limpio
        assert msj.DATO_REMOVIDO in limpio

    def test_una_cita_de_fallo_sobrevive(self):
        # El patrón de DNI no puede comerse una cita: es lo que el buscador busca.
        texto = "que dijo la Corte en Fallos: 311:2437 sobre el exceso ritual"
        assert registro.anonimizar(texto) == texto

    def test_un_numero_de_ley_sobrevive(self):
        assert "25.871" in registro.anonimizar("la ley 25.871 de migraciones")

    def test_el_cuit_gana_sobre_el_dni(self):
        # Un CUIT contiene un DNI: el patrón más corto se lo comería a medias.
        assert registro.anonimizar("27-30123456-4").count(msj.DATO_REMOVIDO) == 1

    def test_un_texto_limpio_no_cambia(self):
        texto = "cuando procede el recurso extraordinario por sentencia arbitraria"
        assert registro.anonimizar(texto) == texto


class TestLinksOficialesEnLaRespuesta:
    """El filtro no puede romper el link oficial de una cita.

    `idDocumento=6952172` son siete dígitos, o sea la forma exacta de un DNI. Aplicado a
    ciegas sobre la respuesta, el filtro se los come y el link deja de abrir: medido, pasaba en
    3 de 15 respuestas ya guardadas, y `/consultas/{id}` las devuelve tal cual a quien reabre
    el enlace.
    """

    RESPUESTA = (
        "La Corte lo sostuvo (Fallos: 311:2437 - https://sjconsulta.csjn.gov.ar/sjconsulta/"
        "documentos/verDocumentoByIdLinksJSP.html?idDocumento=6952172&cache=1668424992130)."
    )

    def test_el_link_oficial_sobrevive(self):
        limpia = registro.anonimizar(self.RESPUESTA, conservar_links=True)
        assert "idDocumento=6952172" in limpia
        assert limpia == self.RESPUESTA

    def test_sin_la_excepcion_el_link_se_rompe(self):
        # Deja constancia de por qué existe el parámetro.
        assert "6952172" not in registro.anonimizar(self.RESPUESTA)

    def test_un_dato_personal_al_lado_del_link_igual_se_tapa(self):
        texto = f"Sobre Juan Perez DNI 12.345.678, {self.RESPUESTA}"
        limpia = registro.anonimizar(texto, conservar_links=True)
        assert "12.345.678" not in limpia
        assert "idDocumento=6952172" in limpia

    def test_en_texto_del_visitante_el_link_no_se_protege(self):
        # Un link con un DNI adentro, escrito por alguien de afuera, es justo lo que hay que
        # tapar: la excepción vale por la procedencia, no por la forma.
        assert "6952172" not in registro.anonimizar(self.RESPUESTA)


class TestFiltroAlCerrarElRegistro:
    """Ninguna columna de texto libre llega al disco sin pasar por el filtro.

    `abrir` anonimizaba la consulta y todo lo demás entraba crudo por `cerrar`. El caso
    concreto es `motivo_inadmision`: lo escribe el clasificador en una oración dirigida a quien
    preguntó, y su causal más frecuente es pedir asesoramiento sobre un caso propio.
    """

    def armar(self, tmp_path):
        from app.almacen.estado import Estado

        estado = Estado(tmp_path / "estado.sqlite3")
        fila = registro.abrir("una consulta de prueba sobre jurisprudencia",
                              registro.nuevo_id(), estado)
        return estado, fila

    def test_el_motivo_de_inadmision_se_filtra(self, tmp_path):
        estado, fila = self.armar(tmp_path)
        registro.cerrar(fila, estado, admitida=0, motivo_inadmision=(
            "Tu consulta sobre el despido de Juan Perez, DNI 12.345.678, pide asesoramiento "
            "sobre un caso propio."))
        assert "12.345.678" not in fila["motivo_inadmision"]
        assert msj.DATO_REMOVIDO in fila["motivo_inadmision"]
        estado.cerrar()

    def test_el_traceback_de_una_corrida_fallida_se_filtra(self, tmp_path):
        estado, fila = self.armar(tmp_path)
        registro.cerrar(fila, estado,
                        error="Traceback: consulta='mi CUIT 20-20123456-3' ...")
        assert "20-20123456-3" not in fila["error"]
        estado.cerrar()

    def test_la_respuesta_se_filtra_conservando_sus_links(self, tmp_path):
        estado, fila = self.armar(tmp_path)
        registro.cerrar(fila, estado, respuesta=(
            "Sobre Juan Perez DNI 12.345.678: la Corte lo sostuvo (Fallos: 311:2437 - "
            "https://sjconsulta.csjn.gov.ar/sjconsulta/documentos/"
            "verDocumentoByIdLinksJSP.html?idDocumento=6952172)."))
        assert "12.345.678" not in fila["respuesta"]
        assert "idDocumento=6952172" in fila["respuesta"]
        estado.cerrar()

    def test_lo_que_no_es_texto_libre_no_se_toca(self, tmp_path):
        estado, fila = self.armar(tmp_path)
        registro.cerrar(fila, estado, usd=0.0054, duracion_ms=21096,
                        citas=[{"fallo": "Fallos: 311:2437"}])
        assert fila["usd"] == 0.0054 and fila["duracion_ms"] == 21096
        assert "311:2437" in fila["citas"]
        estado.cerrar()


class TestHuellaDeVisitante:
    """La identificación que no guarda de qué salió."""

    def test_la_misma_ip_da_la_misma_huella(self):
        a = registro.huella_visitante("190.1.2.3", "sal")
        assert a == registro.huella_visitante("190.1.2.3", "sal")

    def test_otra_sal_da_otra_huella(self):
        # Rotar la sal olvida a todos, que es la forma más simple de un borrado.
        assert (registro.huella_visitante("190.1.2.3", "sal-a")
                != registro.huella_visitante("190.1.2.3", "sal-b"))

    def test_la_huella_no_contiene_la_ip(self):
        assert "190.1.2.3" not in registro.huella_visitante("190.1.2.3", "sal")

    def test_dos_ids_de_consulta_no_se_repiten(self):
        assert registro.nuevo_id() != registro.nuevo_id()


class RespuestaFalsa:
    """Lo que devuelve un proveedor, en las dos formas que el contador sabe leer."""

    def __init__(self, llm_output=None, generaciones=None):
        self.llm_output = llm_output
        self.generations = generaciones or []


class MensajeFalso:
    def __init__(self, uso, modelo):
        self.usage_metadata = uso
        self.response_metadata = {"model_name": modelo}


class GeneracionFalsa:
    def __init__(self, mensaje):
        self.message = mensaje


class TestContadorDeCosto:
    """El gasto medido, que es lo que sostiene el freno de presupuesto."""

    def test_suma_el_uso_que_informa_openai(self):
        contador = Contador()
        contador.on_llm_end(RespuestaFalsa(llm_output={
            "model_name": "gpt-4o-mini",
            "token_usage": {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000},
        }))
        assert contador.consumo.llamadas == 1
        assert contador.consumo.usd == pytest.approx(0.75)  # 0.15 + 0.60

    def test_lee_usage_metadata_cuando_no_hay_llm_output(self):
        # Es la forma común de LangChain: sin este camino, un proveedor que no complete
        # `llm_output` dejaría el contador en cero, y cero se lee como "no gastó nada".
        contador = Contador()
        contador.on_llm_end(RespuestaFalsa(generaciones=[[GeneracionFalsa(
            MensajeFalso({"input_tokens": 1_000_000, "output_tokens": 0}, "gpt-4o-mini"))]]))
        assert contador.consumo.usd == pytest.approx(0.15)

    def test_un_modelo_sin_precio_cuenta_tokens_y_suma_cero(self):
        contador = Contador()
        contador.on_llm_end(RespuestaFalsa(llm_output={
            "model_name": "modelo-que-no-existe",
            "token_usage": {"prompt_tokens": 500, "completion_tokens": 500},
        }))
        assert contador.consumo.tokens_entrada == 500
        assert contador.consumo.usd == 0.0
        assert "modelo-que-no-existe" in precios.sin_precio()

    def test_el_sufijo_de_fecha_no_esconde_el_precio(self):
        assert precios.costo("gpt-4o-mini-2024-07-18", 1_000_000, 0) == pytest.approx(0.15)

    def test_varias_llamadas_acumulan(self):
        contador = Contador()
        for _ in range(3):
            contador.on_llm_end(RespuestaFalsa(llm_output={
                "model_name": "gpt-4o-mini",
                "token_usage": {"prompt_tokens": 100, "completion_tokens": 10}}))
        assert contador.consumo.llamadas == 3
        assert contador.consumo.tokens_entrada == 300
        assert contador.consumo.modelo == "gpt-4o-mini"


class TestConstantesDelServicio:
    """Los cuatro desenlaces de una consulta, que la API y el registro comparten."""

    def test_los_desenlaces_son_los_cuatro(self):
        assert {cfg.EN_CURSO, cfg.PUBLICADA, cfg.SIN_BASE, cfg.FALLIDA} == {
            "en_curso", "publicada", "sin_base", "fallida"}
