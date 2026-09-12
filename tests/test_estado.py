"""El contrato de datos: artefactos, reducer, huellas y la telemetría de calidad.

Los artefactos son lo que cruza cada frontera del grafo, así que sus invariantes son la
primera línea de validación del sistema. Acá se comprueba que un estado inconsistente no se
puede construir.
"""

import pytest
from pydantic import ValidationError

from app.grafo.estado import (
    DESTINOS,
    NODOS,
    Cita,
    Destino,
    Investigacion,
    Redaccion,
    Verificacion,
    acumular,
    evaluar_calidad,
    huella_material,
    ultima_investigacion,
    ultima_redaccion,
)


class TestConstantesDeRuteo:
    """Los nombres de los nodos y sus dos assert de sincronización."""

    def test_destino_cubre_exactamente_los_destinos(self):
        assert set(Destino.__args__) == set(DESTINOS)

    def test_los_nodos_son_los_destinos_menos_finalizar(self):
        assert set(NODOS) == set(DESTINOS) - {"FINALIZAR"}


class TestInvariantesDeLosArtefactos:
    """Lo que los modelos no dejan construir."""

    def test_una_cita_exige_afirmacion_sustantiva(self):
        with pytest.raises(ValidationError):
            Cita(fallo="Fallos: 311:2437", subseccion="6.2.7 Exceso ritual", afirmacion="corto")

    def test_una_sintesis_en_blanco_no_pasa_aunque_tenga_largo(self):
        with pytest.raises(ValidationError):
            Investigacion(sintesis=" " * 40, citas=(), subsecciones=())

    def test_un_rechazo_del_verificador_exige_observaciones(self):
        with pytest.raises(ValidationError):
            Verificacion(verificadas=(), inexistentes=("Fallos: 999:9999",),
                         aprobado=False, observaciones=())

    def test_una_redaccion_limpia_no_puede_traer_intrusas(self, investigacion, verificacion):
        with pytest.raises(ValidationError):
            Redaccion(texto="Texto", citas_usadas=(), citas_intrusas=("Fallos: 999:9999",),
                      motivos=(), limpia=True, llamadas=1,
                      sobre_material=huella_material(investigacion, verificacion))

    def test_una_redaccion_sucia_exige_motivos(self, investigacion, verificacion):
        with pytest.raises(ValidationError):
            Redaccion(texto="Texto", citas_usadas=(), citas_intrusas=("Fallos: 999:9999",),
                      motivos=(), limpia=False, llamadas=1,
                      sobre_material=huella_material(investigacion, verificacion))

    def test_un_campo_de_mas_se_rechaza(self):
        with pytest.raises(ValidationError):
            Cita(fallo="Fallos: 311:2437", subseccion="6.2.7 Exceso ritual",
                 afirmacion="Una afirmacion suficientemente larga.", prioridad="alta")

    def test_los_artefactos_son_inmutables(self, investigacion):
        with pytest.raises(ValidationError):
            investigacion.sintesis = "otra cosa"


class TestReducerAcumular:
    """El reducer que hace auditable el ciclo de refinamiento."""

    def test_concatena_en_vez_de_pisar(self):
        assert acumular(("a",), ("b",)) == ("a", "b")

    def test_normaliza_la_lista_que_devuelve_redis(self):
        # Al reanudar desde un checkpoint las tuplas vuelven como listas.
        assert acumular(["a"], ["b"]) == ("a", "b")

    def test_un_escalar_se_agrega_como_un_elemento(self):
        assert acumular((), "solo") == ("solo",)

    def test_ninguno_deja_lo_que_habia(self):
        assert acumular(("a",), None) == ("a",)

    def test_la_tupla_vacia_del_estado_inicial_no_borra(self):
        assert acumular(("a",), ()) == ("a",)


class TestHuellas:
    """Lo que decide si una redacción sigue vigente."""

    def test_el_mismo_material_da_la_misma_huella(self, investigacion, verificacion):
        assert huella_material(investigacion, verificacion) == huella_material(
            investigacion, verificacion)

    def test_material_distinto_da_huella_distinta(self, investigacion, verificacion):
        otra = Investigacion(sintesis="Otra sintesis de prueba, mas larga que el minimo.",
                             citas=investigacion.citas, subsecciones=())
        assert huella_material(otra, verificacion) != huella_material(investigacion, verificacion)

    def test_el_orden_de_las_citas_no_cambia_la_huella(self, investigacion):
        directa = Verificacion(verificadas=("Fallos: 311:2437", "Fallos: 315:1848"),
                               inexistentes=(), aprobado=True, observaciones=())
        invertida = Verificacion(verificadas=("Fallos: 315:1848", "Fallos: 311:2437"),
                                 inexistentes=(), aprobado=True, observaciones=())
        assert huella_material(investigacion, directa) == huella_material(investigacion, invertida)


class TestLecturasDelEstado:
    """Los helpers que leen el último artefacto de cada tipo."""

    def test_sin_artefactos_devuelven_none(self, estado_vacio):
        assert ultima_investigacion(estado_vacio) is None
        assert ultima_redaccion(estado_vacio) is None

    def test_devuelven_el_ultimo_de_la_tupla(self, estado_terminado, investigacion):
        otra = Investigacion(sintesis="La segunda investigacion, tras una ampliacion.",
                             citas=investigacion.citas, subsecciones=())
        estado = {**estado_terminado, "investigaciones": (investigacion, otra)}
        assert ultima_investigacion(estado) is otra


class TestEvaluarCalidad:
    """La telemetría de calidad que viaja al registro y al cierre del stream."""

    def test_un_trabajo_holgado_no_enciende_ninguna_senal(self, estado_terminado):
        calidad = evaluar_calidad(estado_terminado, holgadas=3, tope_intentos=3)
        assert calidad.senales is False
        assert calidad.motivos == ()
        assert calidad.citas_en_el_texto == 3

    def test_pocas_citas_en_el_texto_encienden_una_senal(self, estado_terminado):
        calidad = evaluar_calidad(estado_terminado, holgadas=4, tope_intentos=3)
        assert calidad.senales is True
        assert any("por debajo de las 4" in m for m in calidad.motivos)

    def test_los_intentos_agotados_encienden_una_senal(self, estado_terminado):
        estado = {**estado_terminado, "intentos": 3}
        calidad = evaluar_calidad(estado, holgadas=3, tope_intentos=3)
        assert calidad.senales is True
        assert any("intentos" in m for m in calidad.motivos)

    def test_una_cita_inventada_en_el_historial_enciende_una_senal(self, estado_terminado):
        # La verificación vigente aprueba, pero antes hubo un rechazo: eso es lo que
        # distingue a un trabajo difícil de uno fácil, y solo se ve en el historial.
        rechazo = Verificacion(verificadas=("Fallos: 311:2437",),
                               inexistentes=("Fallos: 999:9999",), aprobado=False,
                               observaciones=("una cita no existe",))
        estado = {**estado_terminado,
                  "verificaciones": (rechazo,) + estado_terminado["verificaciones"]}
        calidad = evaluar_calidad(estado, holgadas=3, tope_intentos=3)
        assert calidad.senales is True
        assert calidad.citas_inexistentes == 1
        assert calidad.cobertura < 1.0
        assert any("no existen en el corpus" in m for m in calidad.motivos)

    def test_las_propuestas_nunca_son_menos_que_las_verificadas(self, estado_terminado):
        # Las tres cuentas miden el historial completo. Leyendo las propuestas de la última
        # investigación quedaban en otra escala, y el registro llegó a decir "2 propuestas,
        # 3 verificadas" en una consulta que se corrigió una vez.
        rechazo = Verificacion(verificadas=("Fallos: 328:1146",), inexistentes=(),
                               aprobado=False, observaciones=("solo 1 cita verificada",))
        estado = {**estado_terminado,
                  "verificaciones": (rechazo,) + estado_terminado["verificaciones"]}
        calidad = evaluar_calidad(estado, holgadas=3, tope_intentos=3)
        assert calidad.citas_propuestas >= calidad.citas_verificadas
        assert calidad.citas_propuestas == calidad.citas_verificadas + calidad.citas_inexistentes

    def test_la_cobertura_sale_de_las_propuestas(self, estado_terminado):
        rechazo = Verificacion(verificadas=("Fallos: 311:2437",),
                               inexistentes=("Fallos: 999:9999", "Fallos: 998:8888"),
                               aprobado=False, observaciones=("dos citas no existen",))
        estado = {**estado_terminado,
                  "verificaciones": (rechazo,) + estado_terminado["verificaciones"]}
        calidad = evaluar_calidad(estado, holgadas=3, tope_intentos=3)
        assert calidad.cobertura == calidad.citas_verificadas / calidad.citas_propuestas

    def test_es_pura_dos_llamadas_dan_lo_mismo(self, estado_terminado):
        # La telemetría se lee del estado final y también en cada evento de reintento: dos
        # lecturas del mismo estado tienen que dar lo mismo.
        primera = evaluar_calidad(estado_terminado, holgadas=3, tope_intentos=3)
        segunda = evaluar_calidad(estado_terminado, holgadas=3, tope_intentos=3)
        assert primera == segunda

    def test_sin_verificaciones_la_cobertura_es_cero_y_no_divide_por_cero(self, estado_vacio):
        calidad = evaluar_calidad(estado_vacio, holgadas=3, tope_intentos=3)
        assert calidad.cobertura == 0.0
        assert calidad.citas_verificadas == 0
