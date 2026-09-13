"""El pedido que el investigador arma para el modelo.

El nodo decide qué contexto entra en el prompt: la consulta siempre, y el rechazo previo del
verificador cuando lo hubo. El modelo se reemplaza por un doble que devuelve una investigación
fija y guarda el pedido recibido, así lo que se comprueba es el prompt y no la respuesta.
"""

import pytest

from app.agentes import investigador as research_agent
from app.grafo.estado import Cita, Investigacion, Verificacion

CONSULTA = "¿Que es el exceso ritual manifiesto?"
TEXTO = ("El exceso ritual manifiesto configura una causal autonoma de arbitrariedad cuando "
         "la forma sacrifica la verdad juridica objetiva.")
CITAS = (Cita(fallo="Fallos: 311:2437",
              afirmacion="El fallo sostiene la doctrina aplicable al caso.",
              respaldo=TEXTO),)


@pytest.fixture
def pedidos(monkeypatch):
    """Registra el pedido que el nodo le manda al agente, sin llamar a ningún modelo."""
    recibidos = []

    class AgenteFalso:
        def __init__(self, lectura):
            self.lectura = lectura

        async def ainvoke(self, entrada, _config=None):
            recibidos.append(entrada["messages"][0][1])
            # Lo que haría `buscar_doctrina` mientras el agente busca.
            self.lectura.anotar(TEXTO, "6.2.7 Exceso ritual", ["311:2437"])
            return {"structured_response": Investigacion(
                sintesis="Una sintesis de prueba, con largo suficiente.",
                citas=CITAS, subsecciones=("6.2.7 Exceso ritual",)), "messages": []}

    monkeypatch.setattr(research_agent, "construir_investigador", AgenteFalso)
    return recibidos


def estado_base(**cambios) -> dict:
    base = {"messages": [], "consulta": CONSULTA, "siguiente": "investigador",
            "investigaciones": (), "verificaciones": (), "redacciones": (),
            "recuperado": {}, "textos_leidos": {},
            "intentos": 0, "vueltas": 0, "completado": False}
    base.update(cambios)
    return base


class TestPedidoDelInvestigador:
    """Qué contexto recibe el modelo, según lo que quedó pendiente."""

    async def test_un_trabajo_nuevo_solo_lleva_la_consulta(self, pedidos):
        await research_agent.investigador_node(estado_base())
        assert pedidos[0] == f"Consulta a investigar: {CONSULTA}"

    async def test_un_rechazo_del_verificador_llega_con_sus_observaciones(self, pedidos):
        rechazo = Verificacion(verificadas=(), inexistentes=("Fallos: 999:9999",),
                               aprobado=False, observaciones=("la cita no existe",))
        inv = Investigacion(sintesis="Una investigacion previa cualquiera.", citas=CITAS,
                            subsecciones=())
        await research_agent.investigador_node(
            estado_base(investigaciones=(inv,), verificaciones=(rechazo,)))
        assert "rechazada por el verificador" in pedidos[0]
        assert "999:9999" in pedidos[0]


class TestArtefacto:
    """Lo que el nodo deja en el estado."""

    async def test_devuelve_la_investigacion_y_su_linea_de_traza(self, pedidos):
        salida = await research_agent.investigador_node(estado_base())
        assert len(salida["investigaciones"]) == 1
        assert "[investigador]" in salida["messages"][0][1]

    async def test_devuelve_el_registro_de_lo_que_leyo(self, pedidos):
        # Es lo que el verificador usa para separar una cita pertinente de una cita cierta.
        salida = await research_agent.investigador_node(estado_base())
        assert salida["recuperado"] == {"311:2437": "6.2.7 Exceso ritual"}

    async def test_devuelve_el_texto_de_los_fragmentos_que_leyo(self, pedidos):
        # Es contra esto que se comprueba el pasaje de respaldo de cada cita.
        salida = await research_agent.investigador_node(estado_base())
        assert list(salida["textos_leidos"].values()) == [TEXTO]

    async def test_el_rechazo_por_impertinencia_llega_con_sus_fallos(self, pedidos):
        rechazo = Verificacion(verificadas=(), inexistentes=(),
                               impertinentes=("Fallos: 330:1228",),
                               aprobado=False, observaciones=("no salio de lo leido",))
        inv = Investigacion(sintesis="Una investigacion previa cualquiera.", citas=CITAS,
                            subsecciones=())
        await research_agent.investigador_node(
            estado_base(investigaciones=(inv,), verificaciones=(rechazo,)))
        assert "existen pero no salieron de" in pedidos[0]
        assert "330:1228" in pedidos[0]

    async def test_sin_artefacto_estructurado_falla_como_error_del_agente(self, monkeypatch):
        from app.nucleo.errores import ErrorDeAgente

        class SinArtefacto:
            async def ainvoke(self, *_a, **_k):
                return {"structured_response": None, "messages": []}

        monkeypatch.setattr(research_agent, "construir_investigador",
                            lambda _registro: SinArtefacto())
        with pytest.raises(ErrorDeAgente):
            await research_agent.investigador_node(estado_base())

    async def test_una_caida_del_modelo_se_traduce(self, monkeypatch):
        from app.nucleo.errores import ErrorDeAgente

        class Explota:
            async def ainvoke(self, *_a, **_k):
                raise ConnectionError("sin red")

        monkeypatch.setattr(research_agent, "construir_investigador",
                            lambda _registro: Explota())
        with pytest.raises(ErrorDeAgente):
            await research_agent.investigador_node(estado_base())
