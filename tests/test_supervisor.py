"""La cadena del supervisor: el control de la respuesta y sus reintentos.

`_controlar_decision` se prueba como función pura sobre las respuestas que devuelve
`include_raw`, y la cadena con un modelo falso que falla las veces que se le pida. Nada de
esto llama a un proveedor.

Los frenos se comprueban aparte: importan porque son las dos ramas donde el supervisor
decide sin gastar una llamada.
"""

import pytest
from langchain_core.runnables import RunnableLambda

from app.grafo import supervisor
from app.grafo.supervisor import (
    Decision,
    DecisionInvalida,
    RespuestaIncompleta,
    _controlar_decision,
    crear_cadena_supervisor,
    supervisor_node,
)
from app.nucleo import constantes as cfg
from app.nucleo.errores import ErrorDeAgente


class RespuestaCruda:
    """Lo que `include_raw` pone en la clave `raw`."""

    def __init__(self, **metadatos):
        self.response_metadata = metadatos


def respuesta(parsed=None, parsing_error=None, **metadatos) -> dict:
    """Una respuesta de `with_structured_output(include_raw=True)`."""
    return {"raw": RespuestaCruda(**metadatos), "parsed": parsed, "parsing_error": parsing_error}


DECISION = Decision(siguiente="investigador", motivo="todavia no hay investigacion")


class TestControlarDecision:
    """Los tres controles, en su orden."""

    def test_una_decision_completa_pasa(self):
        assert _controlar_decision(respuesta(parsed=DECISION)) is DECISION

    def test_el_corte_por_tokens_se_mira_primero(self):
        # Un objeto incompleto puede parsear igual y colarse como decisión válida.
        with pytest.raises(RespuestaIncompleta):
            _controlar_decision(respuesta(parsed=DECISION, finish_reason="length"))

    def test_el_corte_de_anthropic_tambien_se_detecta(self):
        with pytest.raises(RespuestaIncompleta):
            _controlar_decision(respuesta(parsed=DECISION, stop_reason="max_tokens"))

    def test_un_error_de_parseo_no_pasa(self):
        with pytest.raises(DecisionInvalida):
            _controlar_decision(respuesta(parsing_error=ValueError("no es JSON")))

    def test_una_respuesta_sin_decision_no_pasa(self):
        with pytest.raises(DecisionInvalida):
            _controlar_decision(respuesta(parsed=None))

    def test_un_final_normal_no_dispara_nada(self):
        assert _controlar_decision(respuesta(parsed=DECISION, finish_reason="stop")) is DECISION


class TestCadenaConReintentos:
    """La cadena reintenta lo que puede salir bien en el siguiente intento."""

    def modelo_que_falla(self, veces: int):
        """Un modelo que devuelve respuestas cortadas las primeras `veces` llamadas."""
        estado = {"llamadas": 0}

        def responder(_entrada):
            estado["llamadas"] += 1
            if estado["llamadas"] <= veces:
                return respuesta(parsed=DECISION, finish_reason="length")
            return respuesta(parsed=DECISION, finish_reason="stop")

        falso = RunnableLambda(responder)
        falso.with_structured_output = lambda *_a, **_k: falso
        return falso, estado

    async def test_se_recupera_de_dos_respuestas_cortadas(self):
        modelo, estado = self.modelo_que_falla(veces=2)
        decision = await crear_cadena_supervisor(modelo).ainvoke("lo que sea")
        assert decision is DECISION
        assert estado["llamadas"] == 3          # dos fallas y el acierto

    async def test_agotados_los_intentos_la_excepcion_sube(self):
        modelo, estado = self.modelo_que_falla(veces=cfg.INTENTOS_DECISION + 1)
        with pytest.raises(RespuestaIncompleta):
            await crear_cadena_supervisor(modelo).ainvoke("lo que sea")
        assert estado["llamadas"] == cfg.INTENTOS_DECISION

    async def test_una_decision_buena_no_reintenta(self):
        modelo, estado = self.modelo_que_falla(veces=0)
        await crear_cadena_supervisor(modelo).ainvoke("lo que sea")
        assert estado["llamadas"] == 1


class TestFrenosDelNodo:
    """Los dos frenos cierran sin consultar al modelo."""

    @pytest.fixture(autouse=True)
    def sin_modelo(self, monkeypatch):
        """Cualquier llamada al modelo en estos casos es un error de diseño."""
        def prohibido(*_a, **_k):
            raise AssertionError("el freno tiene que decidir sin llamar al modelo")

        monkeypatch.setattr(supervisor, "crear_cadena_supervisor", prohibido)

    async def test_el_tope_de_vueltas_cierra(self, estado_vacio):
        estado = {**estado_vacio, "vueltas": cfg.MAXIMO_VUELTAS}
        salida = await supervisor_node(estado)
        assert salida["completado"] is True
        assert salida["siguiente"] == "FINALIZAR"

    async def test_el_tope_de_intentos_cierra_con_una_correccion_pendiente(
            self, estado_terminado, investigacion):
        from app.grafo.estado import Verificacion

        rechazo = Verificacion(verificadas=(), inexistentes=("Fallos: 999:9999",),
                               aprobado=False, observaciones=("una cita no existe",))
        estado = {**estado_terminado, "intentos": cfg.MAXIMO_INTENTOS,
                  "investigaciones": (investigacion,), "verificaciones": (rechazo,)}
        salida = await supervisor_node(estado)
        assert salida["completado"] is True

    async def test_con_material_verificado_el_tope_manda_a_escribir(
            self, estado_terminado, investigacion):
        """Agotadas las correcciones, la respuesta se escribe sobre lo que sí verificó.

        El verificador rechaza la tanda entera por una cita inventada, y ese rechazo es lo que
        hace corregir. Sin intentos, tirar las ciertas por la inventada pierde una respuesta
        buena sin proteger nada: el redactor solo recibe `verificadas`.
        """
        from app.grafo.estado import Verificacion

        rechazo = Verificacion(
            verificadas=("Fallos: 311:2437", "Fallos: 315:1848"),
            inexistentes=("Fallos: 999:9999",), aprobado=False,
            observaciones=("una cita no existe",))
        estado = {**estado_terminado, "intentos": cfg.MAXIMO_INTENTOS,
                  "investigaciones": (investigacion,), "verificaciones": (rechazo,),
                  "redacciones": ()}
        salida = await supervisor_node(estado)
        assert salida["siguiente"] == "redactor"
        assert salida["completado"] is False

    async def test_sin_material_suficiente_el_tope_cierra(
            self, estado_terminado, investigacion):
        from app.grafo.estado import Verificacion

        pobre = Verificacion(verificadas=("Fallos: 311:2437",),
                             inexistentes=("Fallos: 999:9999",), aprobado=False,
                             observaciones=("una cita no existe",))
        estado = {**estado_terminado, "intentos": cfg.MAXIMO_INTENTOS,
                  "investigaciones": (investigacion,), "verificaciones": (pobre,),
                  "redacciones": ()}
        salida = await supervisor_node(estado)
        assert salida["siguiente"] == "FINALIZAR"
        assert salida["completado"] is True

    async def test_con_la_respuesta_ya_escrita_el_tope_cierra(
            self, estado_terminado, investigacion):
        # El rescate escribe una vez: con redacción vigente, el freno vuelve a cerrar.
        from app.grafo.estado import Verificacion

        rechazo = Verificacion(
            verificadas=estado_terminado["verificaciones"][-1].verificadas,
            inexistentes=("Fallos: 999:9999",), aprobado=False,
            observaciones=("una cita no existe",))
        sucia = estado_terminado["redacciones"][-1].model_copy(update={
            "limpia": False, "motivos": ("invoco una cita sin verificar",),
            "citas_intrusas": ("999:9999",)})
        estado = {**estado_terminado, "intentos": cfg.MAXIMO_INTENTOS,
                  "verificaciones": (rechazo,), "redacciones": (sucia,)}
        salida = await supervisor_node(estado)
        assert salida["siguiente"] == "FINALIZAR"

    async def test_sin_correccion_pendiente_el_tope_de_intentos_no_cierra_solo(
            self, estado_terminado, monkeypatch):
        # El freno exige que lo pendiente sea una corrección sobre algo ya juzgado: si no,
        # cerraría un trabajo sano por un contador.
        modelo = RunnableLambda(lambda _: respuesta(parsed=DECISION, finish_reason="stop"))
        modelo.with_structured_output = lambda *_a, **_k: modelo
        monkeypatch.setattr(supervisor, "crear_cadena_supervisor",
                            lambda *_a, **_k: crear_cadena_supervisor(modelo))
        estado = {**estado_terminado, "intentos": cfg.MAXIMO_INTENTOS}
        salida = await supervisor_node(estado)
        assert salida["siguiente"] == "investigador"


class TestErroresDelNodo:
    """Cualquier fallo del modelo sale como error del agente."""

    async def test_una_caida_del_modelo_se_traduce(self, estado_vacio, monkeypatch):
        def explota(*_a, **_k):
            modelo = RunnableLambda(lambda _: (_ for _ in ()).throw(ConnectionError("sin red")))
            modelo.with_structured_output = lambda *_a, **_k: modelo
            return crear_cadena_supervisor(modelo)

        monkeypatch.setattr(supervisor, "crear_cadena_supervisor", explota)
        with pytest.raises(ErrorDeAgente):
            await supervisor_node(estado_vacio)


class TestDecision:
    """El contrato de la decisión."""

    def test_un_destino_fuera_del_conjunto_no_valida(self):
        with pytest.raises(Exception):
            Decision(siguiente="archivista", motivo="me parecio")

    def test_el_motivo_no_tiene_tope_de_largo(self):
        # Un tope acá haría que un motivo largo invalidara la decisión entera, y con ella el
        # trabajo, por un campo que solo alimenta la traza.
        decision = Decision(siguiente="redactor", motivo="x" * 400)
        assert len(decision.motivo) == 400
