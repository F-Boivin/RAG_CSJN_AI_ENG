"""El grafo entero corriendo con dobles: ruteo, frenos y la barra de publicación.

Los cuatro nodos se reemplazan por dobles deterministas, así lo que queda bajo prueba es el
cableado. La barra de publicación es `leer_situacion(final).listo`: el servicio la lee del
estado final para decidir entre publicar y contestar que no hay base.
"""

import pytest

import app.nucleo.constantes as cfg
from app.grafo import construccion
from app.grafo.estado import leer_situacion
from app.nucleo.errores import ErrorDeAgente
from tests.dobles import (
    armar,
    estado_inicial,
    investigador_inventor,
    investigador_terco,
    redactor_sucio,
    supervisor_con_freno,
)

CONFIG = {"recursion_limit": cfg.LIMITE_RECURSION_ORQUESTADOR}


class TestCaminoFeliz:
    """Un trabajo que sale bien a la primera."""

    async def test_publica_sin_reintentos(self):
        final = await armar().ainvoke(estado_inicial(), CONFIG)
        assert leer_situacion(final).listo is True
        assert final["intentos"] == 0
        assert len(final["redacciones"]) == 1

    async def test_el_recorrido_queda_en_el_estado(self):
        final = await armar().ainvoke(estado_inicial(), CONFIG)
        assert len(final["investigaciones"]) == 1
        assert len(final["verificaciones"]) == 1
        assert final["completado"] is True


class TestReintento:
    """Una corrección que el sistema resuelve solo, sin que nadie apruebe nada."""

    async def test_una_cita_inventada_se_corrige_y_publica(self):
        final = await armar(investigador=investigador_inventor).ainvoke(
            estado_inicial(), CONFIG)
        assert leer_situacion(final).listo is True
        # Dos investigaciones: la que inventó y la corregida. El historial es lo que hace
        # auditable el ciclo.
        assert len(final["investigaciones"]) == 2
        assert final["verificaciones"][0].aprobado is False
        assert final["verificaciones"][-1].aprobado is True
        assert final["intentos"] == 1

    async def test_la_cita_inventada_queda_en_el_historial(self):
        final = await armar(investigador=investigador_inventor).ainvoke(
            estado_inicial(), CONFIG)
        inexistentes = [c for v in final["verificaciones"] for c in v.inexistentes]
        assert inexistentes == ["Fallos: 999:9999"]


class TestSobreLoVerificado:
    """Agotadas las correcciones, la respuesta se escribe sobre las citas que sí existen.

    El verificador rechaza la tanda entera cuando una sola cita es inventada, y ese rechazo es
    lo que hace corregir. Una vez sin intentos, tirar dos citas ciertas por una inventada
    pierde una respuesta buena sin proteger nada: el redactor solo recibe las verificadas.
    """

    async def test_una_cita_terca_no_impide_publicar(self):
        final = await armar(investigador=investigador_terco).ainvoke(
            estado_inicial(), CONFIG)
        assert leer_situacion(final).listo is True

    async def test_publica_solo_con_las_verificadas(self):
        final = await armar(investigador=investigador_terco).ainvoke(
            estado_inicial(), CONFIG)
        usadas = final["redacciones"][-1].citas_usadas
        assert "Fallos: 999:9999" not in usadas
        assert len(usadas) >= cfg.CITAS_MINIMAS

    async def test_la_verificacion_sigue_rechazada_en_el_historial(self):
        # La cita inventada no desaparece: queda en el estado, y de ahí sale la señal que el
        # registro guarda.
        final = await armar(investigador=investigador_terco).ainvoke(
            estado_inicial(), CONFIG)
        assert final["verificaciones"][-1].aprobado is False
        assert "Fallos: 999:9999" in final["verificaciones"][-1].inexistentes

    async def test_se_agotaron_los_intentos_antes_de_rescatar(self):
        final = await armar(investigador=investigador_terco).ainvoke(
            estado_inicial(), CONFIG)
        assert final["intentos"] >= cfg.MAXIMO_INTENTOS


class TestCorte:
    """Un trabajo que agota los intentos: el grafo cierra y nada se publica."""

    async def test_un_redactor_que_nunca_queda_limpio_corta_sin_publicar(self):
        final = await armar(redactor=redactor_sucio,
                            supervisor=supervisor_con_freno).ainvoke(estado_inicial(), CONFIG)
        assert final["completado"] is True
        assert leer_situacion(final).listo is False
        assert final["intentos"] >= cfg.MAXIMO_INTENTOS

    async def test_el_corte_deja_la_ultima_redaccion_a_la_vista(self):
        final = await armar(redactor=redactor_sucio,
                            supervisor=supervisor_con_freno).ainvoke(estado_inicial(), CONFIG)
        # El diagnóstico que el servicio muestra sale de acá.
        assert final["redacciones"][-1].limpia is False
        assert final["redacciones"][-1].motivos


class TestFrenos:
    """Los topes que impiden que una corrida quede dando vueltas."""

    async def test_el_tope_de_intentos_cierra_la_corrida(self):
        final = await armar(redactor=redactor_sucio,
                            supervisor=supervisor_con_freno).ainvoke(estado_inicial(), CONFIG)
        assert final["intentos"] <= cfg.MAXIMO_INTENTOS
        assert final["vueltas"] <= cfg.MAXIMO_VUELTAS + 1

    async def test_dos_corridas_no_comparten_estado(self):
        grafo = armar()
        a = await grafo.ainvoke(estado_inicial("primera consulta de prueba"), CONFIG)
        b = await grafo.ainvoke(estado_inicial("segunda consulta de prueba"), CONFIG)
        assert a["consulta"] != b["consulta"]
        assert len(a["investigaciones"]) == len(b["investigaciones"]) == 1


class TestSinCheckpointer:
    """El grafo se compila sin persistencia: cada corrida es completa o no es."""

    def test_crear_grafo_no_recibe_checkpointer(self):
        import inspect

        assert "checkpointer" not in inspect.signature(construccion.crear_grafo).parameters

    async def test_una_corrida_no_necesita_thread_id(self):
        # Sin checkpointer no hace falta `configurable.thread_id`: si el grafo lo pidiera,
        # esta llamada fallaría.
        final = await armar().ainvoke(estado_inicial(), CONFIG)
        assert final["completado"] is True


class TestNodosInyectables:
    """Los cuatro nodos entran por parámetro, y ninguno se reemplaza mutando el módulo."""

    def test_los_cuatro_nodos_son_parametros(self):
        import inspect

        from app.grafo.estado import NODOS

        firma = inspect.signature(construccion.crear_grafo)
        assert set(NODOS) | {"supervisor"} <= set(firma.parameters)

    def test_cada_parametro_trae_la_implementacion_real(self):
        import inspect

        from app.agentes import investigador, redactor, verificador
        from app.grafo import supervisor as sup

        reales = {
            "investigador": investigador.investigador_node,
            "verificador": verificador.verificador_node,
            "redactor": redactor.redactor_node,
            "supervisor": sup.supervisor_node,
        }
        firma = inspect.signature(construccion.crear_grafo)
        for nombre, real in reales.items():
            assert firma.parameters[nombre].default is real

    async def test_el_supervisor_inyectado_es_el_que_corre(self):
        llamadas = []

        def supervisor_espia(state):
            llamadas.append(state["consulta"])
            return {"siguiente": "FINALIZAR", "completado": True,
                    "vueltas": state.get("vueltas", 0) + 1, "messages": []}

        grafo = construccion.crear_grafo(supervisor=supervisor_espia)
        await grafo.ainvoke(estado_inicial("una consulta de prueba"), CONFIG)
        assert llamadas == ["una consulta de prueba"]


class TestRuteo:
    """La arista condicional que traduce la decisión del supervisor."""

    def test_un_destino_invalido_falla_en_vez_de_ignorarse(self):
        # Sin el mapeo explícito, LangGraph loguea y termina como si nada.
        with pytest.raises(ErrorDeAgente):
            construccion.enrutar({**estado_inicial(), "siguiente": "archivista"})

    def test_completado_cierra_la_corrida(self, estado_terminado):
        estado = {**estado_terminado, "siguiente": "FINALIZAR", "completado": True}
        assert construccion.enrutar(estado) == "__end__"

    def test_finalizar_sin_completado_tambien_cierra(self, estado_vacio):
        estado = {**estado_vacio, "siguiente": "FINALIZAR", "completado": False}
        assert construccion.enrutar(estado) == "__end__"

    def test_un_destino_valido_se_devuelve_tal_cual(self, estado_vacio):
        estado = {**estado_vacio, "siguiente": "verificador", "completado": False}
        assert construccion.enrutar(estado) == "verificador"

    def test_un_destino_con_espacios_se_normaliza(self, estado_vacio):
        estado = {**estado_vacio, "siguiente": " redactor ", "completado": False}
        assert construccion.enrutar(estado) == "redactor"


class TestBarraDePublicacion:
    """`listo` es lo único que decide si una respuesta sale."""

    def test_un_trabajo_terminado_esta_listo(self, estado_terminado):
        assert leer_situacion(estado_terminado).listo is True

    def test_un_trabajo_sin_empezar_no_esta_listo(self, estado_vacio):
        assert leer_situacion(estado_vacio).listo is False

    def test_una_verificacion_rechazada_con_citas_suficientes_alcanza(self, estado_terminado):
        # Es lo que hace publicable el subconjunto verificado: `listo` mira cuántas citas
        # verificaron, no si la tanda fue aprobada.
        from app.grafo.estado import Verificacion

        rechazada = Verificacion(
            verificadas=estado_terminado["verificaciones"][-1].verificadas,
            inexistentes=("Fallos: 999:9999",), aprobado=False,
            observaciones=("una cita no existe",))
        estado = {**estado_terminado, "verificaciones": (rechazada,)}
        situacion = leer_situacion(estado)
        assert situacion.material_suficiente is True
        assert situacion.listo is True

    def test_una_verificacion_rechazada_con_pocas_citas_no_alcanza(self, estado_terminado):
        from app.grafo.estado import Verificacion

        pobre = Verificacion(verificadas=("Fallos: 311:2437",),
                             inexistentes=("Fallos: 999:9999",), aprobado=False,
                             observaciones=("una cita no existe",))
        estado = {**estado_terminado, "verificaciones": (pobre,)}
        assert leer_situacion(estado).material_suficiente is False
        assert leer_situacion(estado).listo is False

    def test_una_redaccion_sucia_no_esta_lista(self, estado_terminado):
        sucia = estado_terminado["redacciones"][-1].model_copy(update={
            "limpia": False, "motivos": ("invoco una cita sin verificar",),
            "citas_intrusas": ("999:9999",),
        })
        estado = {**estado_terminado, "redacciones": (sucia,)}
        assert leer_situacion(estado).listo is False
