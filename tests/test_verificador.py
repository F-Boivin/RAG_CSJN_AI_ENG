"""El verificador: qué aprueba, qué rechaza y con qué observación.

`verificar` está separada del nodo justamente para poder probarla sin montar el grafo. El
padrón y las subsecciones salen de un índice léxico chico, así la suite corre sin base
vectorial y sin red.

Cada rechazo se comprueba con su mensaje: un veredicto correcto que explica mal manda al
investigador a corregir el problema equivocado.
"""

import pytest

from app.agentes.verificador import verificar
from app.grafo.estado import Cita, Investigacion
from app.nucleo import constantes as cfg
from app.nucleo.errores import ErrorDeAgente
from app.rag import herramientas
from tests.dobles import lexico_de_prueba

PADRON = {
    "311:2437": "https://sj.csjn.gov.ar/fallo/311-2437",
    "315:1848": "https://sj.csjn.gov.ar/fallo/315-1848",
    "330:1228": "https://sj.csjn.gov.ar/fallo/330-1228",
    "211:958": "",
}
SUBSECCIONES = ["6.1.1 Concepto", "6.2.7 Exceso ritual manifiesto"]


@pytest.fixture(autouse=True)
def corpus_falso(monkeypatch, tmp_path):
    """Un índice léxico chico y real: nada de Chroma, nada de red."""
    monkeypatch.setattr(herramientas, "_lexico", lexico_de_prueba(
        tmp_path / "lexico.sqlite3", padron=dict(PADRON), subsecciones=list(SUBSECCIONES)))


def investigar(*citas: Cita, sintesis: str = "Sintesis de prueba sobre la doctrina invocada.") -> Investigacion:
    """Una investigación con las citas dadas."""
    return Investigacion(sintesis=sintesis, citas=citas, subsecciones=(SUBSECCIONES[1],))


def cita(fallo: str, subseccion: str = SUBSECCIONES[1]) -> Cita:
    return Cita(fallo=fallo, subseccion=subseccion,
                afirmacion=f"El fallo {fallo} sostiene la doctrina aplicable.")


class TestAprobacion:
    """Lo que el verificador deja pasar."""

    async def test_dos_citas_reales_se_aprueban(self):
        veredicto = await verificar(investigar(cita("Fallos: 311:2437"), cita("Fallos: 315:1848")))
        assert veredicto.aprobado is True
        assert set(veredicto.verificadas) == {"Fallos: 311:2437", "Fallos: 315:1848"}
        assert veredicto.inexistentes == ()

    async def test_una_cita_del_cuerpo_sin_link_tambien_existe(self):
        veredicto = await verificar(investigar(cita("Fallos: 211:958"), cita("Fallos: 311:2437")))
        assert veredicto.aprobado is True
        assert "Fallos: 211:958" in veredicto.verificadas


class TestRechazos:
    """Cada motivo de rechazo, con el mensaje que le corresponde."""

    async def test_una_cita_inventada_se_rechaza_nombrandola(self):
        veredicto = await verificar(investigar(
            cita("Fallos: 999:9999"), cita("Fallos: 311:2437"), cita("Fallos: 315:1848")))
        assert veredicto.aprobado is False
        assert veredicto.inexistentes == ("Fallos: 999:9999",)
        assert any("999:9999" in o and "no figura" in o for o in veredicto.observaciones)

    async def test_citas_amontonadas_piden_una_por_afirmacion(self):
        veredicto = await verificar(investigar(
            cita("Fallos: 311:2437; 315:1848"), cita("Fallos: 330:1228")))
        assert veredicto.aprobado is False
        assert any("junta varias citas" in o for o in veredicto.observaciones)

    async def test_un_campo_sin_numero_recibe_su_propio_mensaje(self):
        # Cero citas y varias citas son problemas distintos: decirle "separá las citas" a
        # quien no escribió ninguna lo manda a corregir algo que no hizo.
        veredicto = await verificar(investigar(cita("la doctrina de Colalillo"),
                                               cita("Fallos: 311:2437")))
        assert veredicto.aprobado is False
        assert any("no tiene ningun numero de fallo" in o for o in veredicto.observaciones)
        assert not any("junta varias citas" in o for o in veredicto.observaciones)

    async def test_una_cita_suelta_en_la_prosa_tambien_se_comprueba(self):
        # La síntesis es el material del que redacta el último agente: un número escrito al
        # costado de la lista tiene que pasar por el mismo control.
        veredicto = await verificar(investigar(
            cita("Fallos: 311:2437"), cita("Fallos: 315:1848"),
            sintesis="La doctrina se apoya ademas en Fallos: 999:9999, no verificado."))
        assert veredicto.aprobado is False
        assert any("la sintesis menciona el fallo" in o for o in veredicto.observaciones)

    async def test_una_subseccion_inventada_se_rechaza(self):
        # Atribuir una cita cierta a una subsección que no existe deja la cita sin
        # procedencia comprobable.
        veredicto = await verificar(investigar(
            cita("Fallos: 311:2437", subseccion="6.9.9 Doctrina inventada"),
            cita("Fallos: 315:1848")))
        assert veredicto.aprobado is False
        assert any("no existe en el corpus" in o for o in veredicto.observaciones)

    async def test_todas_ciertas_pero_pocas_es_un_rechazo_distinto(self):
        veredicto = await verificar(investigar(cita("Fallos: 311:2437")))
        assert veredicto.aprobado is False
        assert veredicto.verificadas == ("Fallos: 311:2437",)
        assert veredicto.inexistentes == ()
        assert any(f"al menos {cfg.CITAS_MINIMAS}" in o for o in veredicto.observaciones)

    async def test_una_sintesis_sin_citas_es_inverificable(self):
        veredicto = await verificar(Investigacion(
            sintesis="Una sintesis sin ninguna cita que comprobar.", citas=(), subsecciones=()))
        assert veredicto.aprobado is False
        assert any("no trae ninguna cita" in o for o in veredicto.observaciones)


class ToolFalsa:
    """Un doble de la tool: `verificar_citas` es un StructuredTool y no admite parches."""

    def __init__(self, respuesta=None, excepcion=None):
        self.respuesta, self.excepcion = respuesta, excepcion

    async def ainvoke(self, *_args, **_kwargs):
        if self.excepcion:
            raise self.excepcion
        return self.respuesta


class TestCaidaDeLaBase:
    """Una base caída sube como error del agente, no como 'todas las citas son falsas'."""

    async def test_la_caida_no_se_lee_como_citas_inventadas(self, monkeypatch):
        # Con handle_tool_error, la caída volvería como texto y el sistema acusaría al
        # investigador de haber inventado todas las citas.
        monkeypatch.setattr(herramientas, "verificar_citas",
                            ToolFalsa(excepcion=ConnectionError("chroma caido")))
        with pytest.raises(ErrorDeAgente):
            await verificar(investigar(cita("Fallos: 311:2437"), cita("Fallos: 315:1848")))

    async def test_un_veredicto_incompleto_no_se_completa_con_el_peor_caso(self, monkeypatch):
        monkeypatch.setattr(herramientas, "verificar_citas",
                            ToolFalsa(respuesta="Fallos: 311:2437 | EXISTE |"))
        with pytest.raises(ErrorDeAgente):
            await verificar(investigar(cita("Fallos: 311:2437"), cita("Fallos: 315:1848")))


class TestUnFalloEsUnaCita:
    """El mismo fallo invocado en varias afirmaciones se cuenta una vez.

    Medido con «habeas corpus colectivo»: el corpus responde con Verbitsky y nada más, el
    investigador lo propuso cuatro veces sosteniendo cuatro puntos, y el sistema informaba
    "4 de 4 citas existen en el corpus" sobre un solo fallo. Con eso daba por cumplido el
    mínimo de citas, mandaba a corregir tres veces al redactor —que escribía la única cita que
    tenía— y terminaba diciendo que no había base.
    """

    async def test_el_mismo_fallo_repetido_cuenta_una_vez(self):
        repetido = [cita("Fallos: 311:2437") for _ in range(4)]
        veredicto = await verificar(investigar(*repetido))
        assert veredicto.verificadas == ("Fallos: 311:2437",)

    async def test_repetido_no_alcanza_el_minimo_de_citas(self):
        # Es el punto: `CITAS_MINIMAS` pide fallos distintos, y con duplicados se cumplía solo.
        repetido = [cita("Fallos: 311:2437") for _ in range(cfg.CITAS_MINIMAS + 2)]
        veredicto = await verificar(investigar(*repetido))
        assert veredicto.aprobado is False
        assert any("al menos" in o for o in veredicto.observaciones)

    async def test_las_formas_distintas_del_mismo_fallo_son_una(self):
        # "Fallos: 311:2437" y "311:2437" son la misma cita; lo que la identifica es el par.
        veredicto = await verificar(investigar(
            cita("Fallos: 311:2437"), cita("311:2437"), cita("Fallos: 315:1848")))
        assert len(veredicto.verificadas) == 2
        assert veredicto.aprobado is True

    async def test_conserva_la_forma_en_que_se_escribio_la_primera(self):
        veredicto = await verificar(investigar(
            cita("Fallos: 311:2437"), cita("311:2437"), cita("Fallos: 315:1848")))
        assert "Fallos: 311:2437" in veredicto.verificadas

    async def test_un_fallo_inexistente_repetido_se_observa_una_vez(self):
        repetido = [cita("Fallos: 999:9999") for _ in range(3)]
        veredicto = await verificar(investigar(cita("Fallos: 311:2437"), *repetido))
        assert veredicto.inexistentes == ("Fallos: 999:9999",)
