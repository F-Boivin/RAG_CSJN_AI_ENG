"""El motor: la corrida en proceso, sus eventos y el desenlace que publica.

El grafo es el real, con los nodos falsos; el índice es un SQLite chico; el estado, otro. No
hay modelo ni red, así que lo que se ejercita es el motor: cómo traduce el stream, cuándo
publica, cuándo contesta que no hay base, y qué pasa si el cliente se va.
"""

import asyncio

import pytest
from pydantic import SecretStr

import app.nucleo.constantes as cfg
from app.almacen.estado import Estado
from app.servicio.motor import Motor
from tests.dobles import (
    investigador_falso,
    FALLOS,
    armar,
    investigador_inventor,
    lexico_de_prueba,
    redactor_sucio,
    supervisor_con_freno,
)

CONSULTA = "¿Que es el exceso ritual manifiesto?"


class IndiceFalso:
    """Lo que el motor le pide al índice: el padrón y la versión."""

    def __init__(self, lexico):
        self.lexico = lexico
        self.version = "prueba-0001"


@pytest.fixture
def estado(tmp_path) -> Estado:
    return Estado(tmp_path / "estado.sqlite3")


@pytest.fixture
def indice(tmp_path) -> IndiceFalso:
    return IndiceFalso(lexico_de_prueba(
        tmp_path / "lexico.sqlite3",
        padron={f.split(": ")[1]: f"https://sjconsulta.csjn.gov.ar/{f[-6:]}" for f in FALLOS},
    ))


def motor(indice, estado, **grafo) -> Motor:
    return Motor(armar(**grafo), indice, estado, concurrentes=2)


async def correr(m: Motor, consulta: str = CONSULTA, visitante: str = "v1"):
    """Lanza una corrida y espera a que termine, devolviendo sus eventos."""
    corrida = m.lanzar(consulta, "c-1", visitante)
    eventos = [e async for e in corrida.seguir()]
    return corrida, eventos


def nombres(eventos) -> list[str]:
    return [e.nombre for e in eventos]


class TestCaminoFeliz:
    """Una consulta que se responde a la primera."""

    async def test_publica_con_sus_citas(self, indice, estado):
        _, eventos = await correr(motor(indice, estado))
        assert "final" in nombres(eventos)
        assert nombres(eventos).count("cita") == 3
        final = [e for e in eventos if e.nombre == "final"][0]
        assert final.datos["publicado"] is True

    async def test_el_avance_llega_antes_que_el_cierre(self, indice, estado):
        _, eventos = await correr(motor(indice, estado))
        assert nombres(eventos).index("estado") < nombres(eventos).index("final")

    async def test_cada_cita_trae_su_link_del_padron(self, indice, estado):
        _, eventos = await correr(motor(indice, estado))
        citas = [e.datos for e in eventos if e.nombre == "cita"]
        assert all(c["url"].startswith("https://sjconsulta.csjn.gov.ar/") for c in citas)

    async def test_queda_registrada_como_publicada(self, indice, estado):
        await correr(motor(indice, estado))
        fila = estado.leer_consulta("c-1")
        assert fila["desenlace"] == cfg.PUBLICADA
        assert fila["version_indice"] == "prueba-0001"


class TestReintentoYCorte:
    """Los dos desenlaces que reemplazan a la espera humana."""

    async def test_una_cita_inventada_se_corrige_y_publica(self, indice, estado):
        _, eventos = await correr(motor(indice, estado, investigador=investigador_inventor))
        assert "final" in nombres(eventos)
        assert any(e.datos.get("fase") == "corrigiendo" for e in eventos
                   if e.nombre == "estado")

    async def test_la_senal_de_cita_inventada_queda_en_el_registro(self, indice, estado):
        await correr(motor(indice, estado, investigador=investigador_inventor))
        fila = estado.leer_consulta("c-1")
        assert fila["citas_inexistentes"] == 1
        assert "no existen en el corpus" in fila["senales"]

    async def test_un_trabajo_que_no_se_sostiene_no_publica(self, indice, estado):
        _, eventos = await correr(motor(indice, estado, redactor=redactor_sucio,
                                        supervisor=supervisor_con_freno))
        assert "sin_base" in nombres(eventos)
        assert "final" not in nombres(eventos)

    async def test_el_corte_explica_por_que(self, indice, estado):
        _, eventos = await correr(motor(indice, estado, redactor=redactor_sucio,
                                        supervisor=supervisor_con_freno))
        sin_base = [e for e in eventos if e.nombre == "sin_base"][0]
        assert sin_base.datos["diagnostico"]["intentos"] >= cfg.MAXIMO_INTENTOS

    async def test_el_corte_queda_registrado(self, indice, estado):
        await correr(motor(indice, estado, redactor=redactor_sucio,
                           supervisor=supervisor_con_freno))
        assert estado.leer_consulta("c-1")["desenlace"] == cfg.SIN_BASE


class TestDesconexionYReconexion:
    """Que el navegador se vaya no cancela la corrida: el cupo ya se gastó."""

    async def test_la_corrida_sigue_sin_nadie_escuchando(self, indice, estado):
        m = motor(indice, estado)
        corrida = m.lanzar(CONSULTA, "c-1", "v1")
        while not corrida.terminada:
            await asyncio.sleep(0)
        assert corrida.resultado["publicado"] is True

    async def test_reconectar_reproduce_lo_ya_emitido(self, indice, estado):
        m = motor(indice, estado)
        corrida = m.lanzar(CONSULTA, "c-1", "v1")
        while not corrida.terminada:
            await asyncio.sleep(0)
        primera = [e async for e in corrida.seguir()]
        segunda = [e async for e in corrida.seguir()]
        assert nombres(primera) == nombres(segunda)

    async def test_last_event_id_evita_repetir_el_texto(self, indice, estado):
        m = motor(indice, estado)
        corrida = m.lanzar(CONSULTA, "c-1", "v1")
        while not corrida.terminada:
            await asyncio.sleep(0)
        completo = [e async for e in corrida.seguir()]
        desde_dos = [e async for e in corrida.seguir(desde=2)]
        assert len(desde_dos) == len(completo) - 2

    async def test_el_resultado_queda_leible_por_su_id(self, indice, estado):
        m = motor(indice, estado)
        await correr(m)
        assert estado.leer_consulta("c-1")["respuesta"]


class TestConcurrencia:
    """El semáforo acota cuántas corridas hay en vuelo."""

    async def test_una_corrida_por_visitante(self, indice, estado):
        m = motor(indice, estado)
        m.lanzar(CONSULTA, "c-1", "v1")
        assert m.en_vuelo_de("v1") == "c-1"
        assert m.en_vuelo_de("v2") is None

    async def test_terminada_deja_de_estar_en_vuelo(self, indice, estado):
        m = motor(indice, estado)
        corrida = m.lanzar(CONSULTA, "c-1", "v1")
        while not corrida.terminada:
            await asyncio.sleep(0)
        assert m.en_vuelo_de("v1") is None

    async def test_tres_corridas_con_dos_lugares_terminan_todas(self, indice, estado):
        m = motor(indice, estado)
        corridas = [m.lanzar(CONSULTA, f"c-{i}", f"v{i}") for i in range(3)]
        for corrida in corridas:
            [e async for e in corrida.seguir()]
        assert all(c.resultado["publicado"] for c in corridas)

    async def test_una_corrida_que_espera_avisa_su_lugar(self, indice, estado):
        m = Motor(armar(), indice, estado, concurrentes=1)
        primera = m.lanzar(CONSULTA, "c-1", "v1")
        segunda = m.lanzar(CONSULTA, "c-2", "v2")
        eventos = [e async for e in segunda.seguir()]
        [e async for e in primera.seguir()]
        assert any(e.datos.get("fase") == "en_cola" for e in eventos if e.nombre == "estado")


class TestFallo:
    """Un nodo que revienta se registra en vez de perderse."""

    async def test_un_error_del_grafo_llega_como_evento(self, indice, estado):
        def investigador_roto(_state):
            raise RuntimeError("la base se cayo")

        _, eventos = await correr(motor(indice, estado, investigador=investigador_roto))
        assert "error" in nombres(eventos)

    async def test_el_fallo_queda_registrado(self, indice, estado):
        def investigador_roto(_state):
            raise RuntimeError("la base se cayo")

        await correr(motor(indice, estado, investigador=investigador_roto))
        fila = estado.leer_consulta("c-1")
        assert fila["desenlace"] == cfg.FALLIDA
        assert "la base se cayo" in fila["error"]


class TestTopeDeDuracion:
    """Una corrida que se cuelga se corta, en vez de tapar el semáforo.

    Sin tope, el SDK del proveedor espera diez minutos por llamada y la corrida no tiene fin:
    con tres colgadas, el semáforo queda tomado media hora larga, la cola detrás no avanza, y
    cada uno de los que espera ya pagó su cupo.
    """

    async def test_una_corrida_colgada_se_corta_y_se_registra(self, indice, estado,
                                                              monkeypatch):
        monkeypatch.setattr(cfg, "SEGUNDOS_MAXIMO_CORRIDA", 0.2)

        async def investigador_colgado(_state):
            await asyncio.sleep(30)

        _, eventos = await correr(motor(indice, estado, investigador=investigador_colgado))
        assert "error" in nombres(eventos)
        fila = estado.leer_consulta("c-1")
        assert fila["desenlace"] == cfg.FALLIDA
        assert "segundos" in fila["error"]

    async def test_el_tope_libera_el_semaforo_para_la_que_sigue(self, indice, estado,
                                                               monkeypatch):
        # Es el punto: lo que se colgó no puede quedarse con el lugar de los demás.
        monkeypatch.setattr(cfg, "SEGUNDOS_MAXIMO_CORRIDA", 0.2)
        colgados = {"restantes": 2}

        async def investigador_mixto(state):
            if colgados["restantes"] > 0:
                colgados["restantes"] -= 1
                await asyncio.sleep(30)
            return investigador_falso(state)

        m = Motor(armar(investigador=investigador_mixto), indice, estado, concurrentes=2)
        primeras = [m.lanzar(CONSULTA, f"c-{i}", f"v{i}") for i in range(2)]
        for c in primeras:
            [e async for e in c.seguir()]
        tercera = m.lanzar(CONSULTA, "c-3", "v3")
        eventos = [e async for e in tercera.seguir()]
        assert "final" in [e.nombre for e in eventos]

    async def test_una_corrida_normal_no_se_corta(self, indice, estado):
        # El tope real es holgado contra lo medido: corta lo que se colgó, no lo que tarda.
        assert cfg.SEGUNDOS_MAXIMO_CORRIDA >= 120
        _, eventos = await correr(motor(indice, estado))
        assert "final" in nombres(eventos)


class TestTimeoutDelModelo:
    """Cada llamada al proveedor tiene su propio tope."""

    def test_el_cliente_se_construye_con_timeout(self, monkeypatch):
        from app.nucleo import modelos

        visto = {}

        class ChatFalso:
            def __init__(self, **kwargs):
                visto.update(kwargs)

        monkeypatch.setattr(
            "langchain_openai.ChatOpenAI", ChatFalso, raising=False)
        modelos.ClienteOpenAI(SecretStr("sk-x")).construir("gpt-4o-mini", 0.0)
        assert visto["timeout"] == cfg.SEGUNDOS_TIMEOUT_MODELO
