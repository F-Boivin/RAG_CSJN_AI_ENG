"""El verificador: qué aprueba, qué rechaza y con qué observación.

`verificar` está separada del nodo justamente para poder probarla sin montar el grafo. El
padrón y las subsecciones salen de un índice léxico chico, así la suite corre sin base
vectorial y sin red.

Cada rechazo se comprueba con su mensaje: un veredicto correcto que explica mal manda al
investigador a corregir el problema equivocado.
"""

import pytest

from app.agentes.verificador import respaldado, verificar
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
# El registro de lo que el investigador leyó: todo el padrón de prueba, salvo donde un test
# diga otra cosa. Una cita fuera de acá existe y no viene al caso, y eso es un rechazo.
LEIDO = {clave: SUBSECCIONES[1] for clave in PADRON}
# El texto del fragmento que se da por leido, y contra el que se comprueban los pasajes.
TEXTO = (
    "Para descalificar una sentencia por causa de arbitrariedad en el razonamiento legal se "
    "debe efectuar un analisis de los defectos logicos que justifican tan excepcionalisima "
    "conclusion, la cual no tiene por objeto corregir en tercera instancia pronunciamientos "
    "equivocados."
)
LEIDOS = {"huella-uno": TEXTO}
# En qué fragmento apareció cada fallo: todos en el único fragmento leído, salvo donde un test
# arme otra cosa. El pasaje de una cita solo se busca en los fragmentos de su fallo.
FRAGMENTOS = {clave: ["huella-uno"] for clave in PADRON}
# El pasaje que las citas de prueba copian: esta en TEXTO, asi que resiste la comprobacion.
RESPALDO = "se debe efectuar un analisis de los defectos logicos que justifican"


@pytest.fixture(autouse=True)
def corpus_falso(monkeypatch, tmp_path):
    """Un índice léxico chico y real: nada de Chroma, nada de red."""
    monkeypatch.setattr(herramientas, "_lexico", lexico_de_prueba(
        tmp_path / "lexico.sqlite3", padron=dict(PADRON), subsecciones=list(SUBSECCIONES)))


def investigar(*citas: Cita, sintesis: str = "Sintesis de prueba sobre la doctrina invocada.") -> Investigacion:
    """Una investigación con las citas dadas."""
    return Investigacion(sintesis=sintesis, citas=citas, subsecciones=(SUBSECCIONES[1],))


def cita(fallo: str, respaldo: str = RESPALDO) -> Cita:
    return Cita(fallo=fallo, respaldo=respaldo,
                afirmacion=f"El fallo {fallo} sostiene la doctrina aplicable.")


def con_respaldo(fallo: str, respaldo: str) -> Cita:
    return cita(fallo, respaldo)


class TestAprobacion:
    """Lo que el verificador deja pasar."""

    async def test_dos_citas_reales_se_aprueban(self):
        veredicto = await verificar(investigar(cita("Fallos: 311:2437"), cita("Fallos: 315:1848")), LEIDO, LEIDOS, FRAGMENTOS)
        assert veredicto.aprobado is True
        assert set(veredicto.verificadas) == {"Fallos: 311:2437", "Fallos: 315:1848"}
        assert veredicto.inexistentes == ()

    async def test_una_cita_del_cuerpo_sin_link_tambien_existe(self):
        veredicto = await verificar(investigar(cita("Fallos: 211:958"), cita("Fallos: 311:2437")), LEIDO, LEIDOS, FRAGMENTOS)
        assert veredicto.aprobado is True
        assert "Fallos: 211:958" in veredicto.verificadas


class TestRechazos:
    """Cada motivo de rechazo, con el mensaje que le corresponde."""

    async def test_una_cita_inventada_se_rechaza_nombrandola(self):
        veredicto = await verificar(investigar(
            cita("Fallos: 999:9999"), cita("Fallos: 311:2437"), cita("Fallos: 315:1848")), LEIDO, LEIDOS, FRAGMENTOS)
        assert veredicto.aprobado is False
        assert veredicto.inexistentes == ("Fallos: 999:9999",)
        assert any("999:9999" in o and "no figura" in o for o in veredicto.observaciones)

    async def test_citas_amontonadas_piden_una_por_afirmacion(self):
        veredicto = await verificar(investigar(
            cita("Fallos: 311:2437; 315:1848"), cita("Fallos: 330:1228")), LEIDO, LEIDOS, FRAGMENTOS)
        assert veredicto.aprobado is False
        assert any("junta varias citas" in o for o in veredicto.observaciones)

    async def test_un_campo_sin_numero_recibe_su_propio_mensaje(self):
        # Cero citas y varias citas son problemas distintos: decirle "separá las citas" a
        # quien no escribió ninguna lo manda a corregir algo que no hizo.
        veredicto = await verificar(investigar(cita("la doctrina de Colalillo"),
                                               cita("Fallos: 311:2437")), LEIDO, LEIDOS, FRAGMENTOS)
        assert veredicto.aprobado is False
        assert any("no tiene ningun numero de fallo" in o for o in veredicto.observaciones)
        assert not any("junta varias citas" in o for o in veredicto.observaciones)

    async def test_una_cita_suelta_en_la_prosa_tambien_se_comprueba(self):
        # La síntesis es el material del que redacta el último agente: un número escrito al
        # costado de la lista tiene que pasar por el mismo control.
        veredicto = await verificar(investigar(
            cita("Fallos: 311:2437"), cita("Fallos: 315:1848"),
            sintesis="La doctrina se apoya ademas en Fallos: 999:9999, no verificado."), LEIDO, LEIDOS, FRAGMENTOS)
        assert veredicto.aprobado is False
        assert any("la sintesis menciona el fallo" in o for o in veredicto.observaciones)

    async def test_todas_ciertas_pero_pocas_es_un_rechazo_distinto(self):
        veredicto = await verificar(investigar(cita("Fallos: 311:2437")), LEIDO, LEIDOS, FRAGMENTOS)
        assert veredicto.aprobado is False
        assert veredicto.verificadas == ("Fallos: 311:2437",)
        assert veredicto.inexistentes == ()
        assert any(f"al menos {cfg.CITAS_MINIMAS}" in o for o in veredicto.observaciones)

    async def test_una_sintesis_sin_citas_es_inverificable(self):
        veredicto = await verificar(Investigacion(
            sintesis="Una sintesis sin ninguna cita que comprobar.", citas=(), subsecciones=()),
            LEIDO)
        assert veredicto.aprobado is False
        assert any("no trae ninguna cita" in o for o in veredicto.observaciones)


class TestPertinencia:
    """Existir en el corpus y venir al caso son dos cosas distintas.

    El corpus tiene 9.005 citas reales, y durante meses alcanzó con estar entre ellas. Una
    respuesta sobre el impuesto al valor agregado se publicó sostenida en Fallos 2:88 (1865),
    11:405 (1871), 137:352 y 155:290, todos ciertos, todos anteriores a que el IVA existiera, y
    ninguno en un texto que el investigador hubiera leído: se los había dado una herramienta
    que repartía las citas de una subsección entera.
    """

    async def test_una_cita_que_no_salio_de_lo_leido_se_rechaza(self):
        leido = {"311:2437": SUBSECCIONES[1], "315:1848": SUBSECCIONES[1]}
        veredicto = await verificar(investigar(
            cita("Fallos: 311:2437"), cita("Fallos: 315:1848"),
            cita("Fallos: 330:1228")), leido, LEIDOS, FRAGMENTOS)
        assert veredicto.aprobado is False
        assert veredicto.impertinentes == ("Fallos: 330:1228",)
        assert veredicto.inexistentes == (), "existe: el problema es otro"

    async def test_el_mensaje_distingue_impertinente_de_inexistente(self):
        # Decirle «no existe» sobre un fallo que existe lo manda a corregir otra cosa.
        leido = {"311:2437": SUBSECCIONES[1]}
        veredicto = await verificar(investigar(
            cita("Fallos: 311:2437"), cita("Fallos: 330:1228")), leido, LEIDOS, FRAGMENTOS)
        observacion = next(o for o in veredicto.observaciones if "330:1228" in o)
        assert "existe en el corpus" in observacion
        assert "no salio de ningun fragmento" in observacion
        assert "no figura entre las citas del corpus" not in observacion

    async def test_las_dos_fallas_se_informan_por_separado(self):
        leido = {"311:2437": SUBSECCIONES[1], "315:1848": SUBSECCIONES[1]}
        veredicto = await verificar(investigar(
            cita("Fallos: 311:2437"), cita("Fallos: 315:1848"),
            cita("Fallos: 330:1228"), cita("Fallos: 999:9999")), leido, LEIDOS, FRAGMENTOS)
        assert veredicto.inexistentes == ("Fallos: 999:9999",)
        assert veredicto.impertinentes == ("Fallos: 330:1228",)
        assert set(veredicto.verificadas) == {"Fallos: 311:2437", "Fallos: 315:1848"}

    async def test_una_cita_ajena_en_la_prosa_tambien_se_rechaza(self):
        # La síntesis es el material del que redacta el último agente: un fallo cierto
        # escrito al costado pasa por el mismo control que los de la lista.
        leido = {"311:2437": SUBSECCIONES[1], "315:1848": SUBSECCIONES[1]}
        veredicto = await verificar(investigar(
            cita("Fallos: 311:2437"), cita("Fallos: 315:1848"),
            sintesis="La doctrina se apoya ademas en Fallos: 330:1228, que existe."), leido, LEIDOS, FRAGMENTOS)
        assert veredicto.aprobado is False
        assert any("no salio de ningun fragmento" in o and "330:1228" in o
                   for o in veredicto.observaciones)

    async def test_sin_registro_no_se_aprueba_nada(self):
        # Falla cerrada. Un registro vacío quiere decir que el investigador no leyó nada, y
        # aprobar ahí sería volver justo al agujero que esto tapa.
        veredicto = await verificar(investigar(
            cita("Fallos: 311:2437"), cita("Fallos: 315:1848")), {})
        assert veredicto.aprobado is False
        assert veredicto.verificadas == ()

    async def test_la_forma_en_que_se_escribio_la_cita_no_cambia_el_veredicto(self):
        # El registro guarda "tomo:pagina"; el modelo escribe "Fallos: tomo:pagina".
        veredicto = await verificar(investigar(
            cita("311:2437"), cita("Fallos: 315:1848")), LEIDO, LEIDOS, FRAGMENTOS)
        assert veredicto.aprobado is True
        assert veredicto.impertinentes == ()


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
            await verificar(investigar(cita("Fallos: 311:2437"), cita("Fallos: 315:1848")), LEIDO, LEIDOS, FRAGMENTOS)

    async def test_un_veredicto_incompleto_no_se_completa_con_el_peor_caso(self, monkeypatch):
        monkeypatch.setattr(herramientas, "verificar_citas",
                            ToolFalsa(respuesta="Fallos: 311:2437 | EXISTE |"))
        with pytest.raises(ErrorDeAgente):
            await verificar(investigar(cita("Fallos: 311:2437"), cita("Fallos: 315:1848")), LEIDO, LEIDOS, FRAGMENTOS)


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
        veredicto = await verificar(investigar(*repetido), LEIDO, LEIDOS, FRAGMENTOS)
        assert veredicto.verificadas == ("Fallos: 311:2437",)

    async def test_repetido_no_alcanza_el_minimo_de_citas(self):
        # Es el punto: `CITAS_MINIMAS` pide fallos distintos, y con duplicados se cumplía solo.
        repetido = [cita("Fallos: 311:2437") for _ in range(cfg.CITAS_MINIMAS + 2)]
        veredicto = await verificar(investigar(*repetido), LEIDO, LEIDOS, FRAGMENTOS)
        assert veredicto.aprobado is False
        assert any("al menos" in o for o in veredicto.observaciones)

    async def test_las_formas_distintas_del_mismo_fallo_son_una(self):
        # "Fallos: 311:2437" y "311:2437" son la misma cita; lo que la identifica es el par.
        veredicto = await verificar(investigar(
            cita("Fallos: 311:2437"), cita("311:2437"), cita("Fallos: 315:1848")), LEIDO, LEIDOS, FRAGMENTOS)
        assert len(veredicto.verificadas) == 2
        assert veredicto.aprobado is True

    async def test_conserva_la_forma_en_que_se_escribio_la_primera(self):
        veredicto = await verificar(investigar(
            cita("Fallos: 311:2437"), cita("311:2437"), cita("Fallos: 315:1848")), LEIDO, LEIDOS, FRAGMENTOS)
        assert "Fallos: 311:2437" in veredicto.verificadas

    async def test_un_fallo_inexistente_repetido_se_observa_una_vez(self):
        repetido = [cita("Fallos: 999:9999") for _ in range(3)]
        veredicto = await verificar(investigar(cita("Fallos: 311:2437"), *repetido), LEIDO, LEIDOS, FRAGMENTOS)
        assert veredicto.inexistentes == ("Fallos: 999:9999",)


class TestRespaldoTextual:
    """El pasaje con el que cada cita dice sostenerse, buscado en lo que se leyó.

    Es la tercera comprobación, y la que responde la pregunta que las otras dos no tocan: el
    fallo existe y el investigador lo leyó, pero **¿el fragmento dice lo que la afirmación dice
    que dice?** Sigue siendo código: se compara texto contra texto, y lo que decide no es un
    modelo juzgando a otro.
    """

    def test_un_pasaje_copiado_tal_cual_se_encuentra(self):
        assert respaldado("se debe efectuar un analisis de los defectos logicos", [TEXTO])

    def test_las_tildes_y_la_caja_no_lo_rompen(self):
        # El PDF trae tildes que el modelo copia o no, y a veces cambia la caja.
        assert respaldado("SE DEBE EFECTUAR UN ANÁLISIS DE LOS DEFECTOS LÓGICOS", [TEXTO])

    def test_los_saltos_de_linea_del_pdf_no_lo_rompen(self):
        assert respaldado("se debe efectuar\n  un analisis\nde los defectos logicos", [TEXTO])

    def test_una_palabra_cambiada_lo_sigue_respaldando(self):
        # El retipeo introduce diferencias chicas; el bloque común sigue siendo casi todo.
        assert respaldado("se debe efectuar un analisis de los defectos juridicos que "
                          "justifican tan excepcionalisima conclusion", [TEXTO])

    def test_un_pasaje_escrito_de_memoria_no_lo_respalda(self):
        assert not respaldado(
            "la Corte ha sostenido reiteradamente que el recurso extraordinario procede "
            "cuando media arbitrariedad manifiesta en la valoracion de la prueba", [TEXTO])

    def test_un_pasaje_demasiado_corto_no_alcanza(self):
        # Tres palabras aparecen en cualquier texto jurídico.
        assert not respaldado("la sentencia", [TEXTO])
        assert not respaldado("arbitrariedad", [TEXTO])

    def test_sin_pasaje_no_hay_respaldo(self):
        assert not respaldado("", [TEXTO])

    def test_sin_nada_leido_no_hay_respaldo(self):
        assert not respaldado("se debe efectuar un analisis de los defectos logicos", [])

    def test_alcanza_con_que_aparezca_en_uno_de_los_fragmentos(self):
        assert respaldado("se debe efectuar un analisis de los defectos logicos",
                          ["Un fragmento sobre otra cosa completamente distinta.", TEXTO])

    def test_las_palabras_sueltas_no_alcanzan_aunque_esten_todas(self):
        # Las trece palabras de acá están todas en el fragmento y en su orden —cobertura
        # 1,00—, salteadas de a una. Sin exigir un tramo seguido, un pasaje armado así daría
        # por respaldado cualquier invento; con la exigencia, el tramo más largo es de dos.
        salteado = ("para sentencia causa arbitrariedad el legal debe efectuar analisis "
                    "los logicos justifican excepcionalisima")
        assert not respaldado(salteado, [TEXTO])

    def test_el_tramo_seguido_solo_no_alcanza_si_falta_cobertura(self):
        # Cuatro palabras del corpus pegadas a una oración inventada: el tramo está, la
        # cobertura no.
        assert not respaldado(
            "se debe efectuar un analisis pericial contable sobre los libros del "
            "contribuyente durante todo el periodo fiscal cuestionado", [TEXTO])


class TestCompuertaDelRespaldo:
    """El respaldo se calcula siempre y veta solo cuando la medición lo habilita.

    Un control nuevo que rechaza de más deja al buscador mudo, que es peor que el problema que
    arregla. Se enciende con los números de 20 consultas reales a la vista, y hasta entonces
    viaja al registro como señal.
    """

    async def test_una_cita_sin_respaldo_queda_señalada(self, monkeypatch):
        monkeypatch.setattr(cfg, "RESPALDO_OBLIGATORIO", False)
        veredicto = await verificar(investigar(
            con_respaldo("Fallos: 311:2437", "se debe efectuar un analisis de los defectos "
                                             "logicos que justifican"),
            con_respaldo("Fallos: 315:1848", "un pasaje que ningun fragmento del corpus dice "
                                             "en ningun lado de ninguna manera")),
            LEIDO, LEIDOS, FRAGMENTOS)
        assert veredicto.sin_respaldo == ("Fallos: 315:1848",)

    async def test_con_la_compuerta_abierta_no_bloquea(self, monkeypatch):
        monkeypatch.setattr(cfg, "RESPALDO_OBLIGATORIO", False)
        veredicto = await verificar(investigar(
            con_respaldo("Fallos: 311:2437", "un pasaje inventado que no esta en el corpus"),
            con_respaldo("Fallos: 315:1848", "otro pasaje inventado que tampoco esta ahi")),
            LEIDO, LEIDOS, FRAGMENTOS)
        assert veredicto.sin_respaldo == ("Fallos: 311:2437", "Fallos: 315:1848")
        assert veredicto.aprobado is True

    async def test_con_la_compuerta_cerrada_rechaza(self, monkeypatch):
        monkeypatch.setattr(cfg, "RESPALDO_OBLIGATORIO", True)
        veredicto = await verificar(investigar(
            con_respaldo("Fallos: 311:2437", "un pasaje inventado que no esta en el corpus"),
            con_respaldo("Fallos: 315:1848", "otro pasaje inventado que tampoco esta ahi")),
            LEIDO, LEIDOS, FRAGMENTOS)
        assert veredicto.aprobado is False
        assert any("no viene con un pasaje que lo respalde" in o
                   for o in veredicto.observaciones)

    async def test_la_cita_sin_respaldo_sale_de_las_verificadas(self, monkeypatch):
        """Rechazar la tanda no alcanza: la cita tiene que salir de la lista.

        Agotadas las tres correcciones, el sistema publica sobre el subconjunto verificado en
        vez de tirar todo el trabajo —está escrito así en `leer_situacion`, y es correcto—.
        Una cita que solo baja `aprobado` vuelve igual al redactor en esa última vuelta y
        termina publicada. Medido: con el veto puesto solo sobre `aprobado`, dos de veinte
        consultas publicaron con una cita sin respaldo adentro.
        """
        monkeypatch.setattr(cfg, "RESPALDO_OBLIGATORIO", True)
        veredicto = await verificar(investigar(
            con_respaldo("Fallos: 311:2437", RESPALDO),
            con_respaldo("Fallos: 315:1848", "un pasaje que no esta en ningun fragmento")),
            LEIDO, LEIDOS, FRAGMENTOS)
        assert veredicto.verificadas == ("Fallos: 311:2437",)
        assert veredicto.sin_respaldo == ("Fallos: 315:1848",)

    async def test_sin_nada_respaldado_no_queda_material_para_responder(self, monkeypatch):
        # Y esto es lo que hace que el buscador sepa decir «no tengo esto»: sin citas
        # verificadas no se llega a `CITAS_MINIMAS`, y el desenlace es sin base suficiente.
        monkeypatch.setattr(cfg, "RESPALDO_OBLIGATORIO", True)
        veredicto = await verificar(investigar(
            con_respaldo("Fallos: 311:2437", "un pasaje que no esta en ningun fragmento"),
            con_respaldo("Fallos: 315:1848", "otro pasaje que tampoco esta en ninguno")),
            LEIDO, LEIDOS, FRAGMENTOS)
        assert veredicto.verificadas == ()
        assert len(veredicto.verificadas) < cfg.CITAS_MINIMAS

    async def test_con_la_compuerta_abierta_la_cita_no_se_toca(self, monkeypatch):
        # Apagada informa y no saca nada: es lo que permite medir cuánto rechazaría antes de
        # dejarla rechazar.
        monkeypatch.setattr(cfg, "RESPALDO_OBLIGATORIO", False)
        veredicto = await verificar(investigar(
            con_respaldo("Fallos: 311:2437", RESPALDO),
            con_respaldo("Fallos: 315:1848", "un pasaje que no esta en ningun fragmento")),
            LEIDO, LEIDOS, FRAGMENTOS)
        assert set(veredicto.verificadas) == {"Fallos: 311:2437", "Fallos: 315:1848"}
        assert veredicto.sin_respaldo == ("Fallos: 315:1848",)

    async def test_el_respaldo_no_se_le_reclama_a_una_cita_que_ni_existe(self, monkeypatch):
        # Dos problemas en un mensaje mandan a corregir lo que no es: un fallo inventado se
        # saca, no se respalda.
        monkeypatch.setattr(cfg, "RESPALDO_OBLIGATORIO", True)
        veredicto = await verificar(investigar(
            con_respaldo("Fallos: 999:9999", "un pasaje cualquiera bastante largo para pasar"),
            con_respaldo("Fallos: 311:2437", "se debe efectuar un analisis de los defectos "
                                             "logicos que justifican")),
            LEIDO, LEIDOS, FRAGMENTOS)
        assert veredicto.sin_respaldo == ()
        assert veredicto.inexistentes == ("Fallos: 999:9999",)

    async def test_alcanza_con_que_uno_de_sus_pasajes_resista(self, monkeypatch):
        # El mismo fallo puede sostener dos afirmaciones y estar citado en dos lugares del
        # corpus: exigir que las dos salgan del mismo fragmento pide algo que no siempre hay.
        monkeypatch.setattr(cfg, "RESPALDO_OBLIGATORIO", True)
        veredicto = await verificar(investigar(
            con_respaldo("Fallos: 311:2437", "un pasaje que no esta en ningun lado del corpus"),
            con_respaldo("311:2437", "se debe efectuar un analisis de los defectos logicos "
                                     "que justifican"),
            con_respaldo("Fallos: 315:1848", "la cual no tiene por objeto corregir en tercera "
                                             "instancia pronunciamientos equivocados")),
            LEIDO, LEIDOS, FRAGMENTOS)
        assert veredicto.sin_respaldo == ()
        assert veredicto.aprobado is True


class TestRespaldoDelMismoFallo:
    """El pasaje se muestra solo si sale de un fragmento que trae ese fallo.

    Buscarlo en todo lo leído dejaba pasar una cita leída en un documento con una oración
    leída en otro, y la ficha la mostraba como su respaldo. Medido en producción sobre 14
    citas publicadas, 4 venían así, las cuatro de documentos distintos: 243:190, citado en una
    nota sobre honorarios, salió con un pasaje del suplemento de Decretos de Necesidad y
    Urgencia.

    **Esta vara decide qué ve el lector, y no qué se publica.** Se probó vetar con ella y se
    midió sobre 20 consultas: 31 correcciones contra 9, la latencia de 18 a 40 segundos, y tres
    consultas que el corpus responde terminaron sin base.
    """

    HONORARIOS = ("La regulacion de honorarios devengados en las instancias ordinarias se rige "
                  "por la ley vigente al tiempo de los trabajos profesionales. (Fallos: 243:190)")
    DECRETOS = ("El hecho imponible, basado en la presuncion de capacidad economica, por la "
                "tenencia de activos financieros en un momento determinado.")
    TEXTOS = {"honorarios": HONORARIOS, "decretos": DECRETOS}
    PASAJE_AJENO = "hecho imponible, basado en la presuncion de capacidad economica, por la tenencia"
    PASAJE_PROPIO = "se rige por la ley vigente al tiempo de los trabajos profesionales"

    @pytest.fixture(autouse=True)
    def padron_con_el_fallo(self, monkeypatch, tmp_path):
        monkeypatch.setattr(herramientas, "_lexico", lexico_de_prueba(
            tmp_path / "padron.sqlite3",
            padron={**PADRON, "243:190": ""}, subsecciones=list(SUBSECCIONES)))
        monkeypatch.setattr(cfg, "RESPALDO_OBLIGATORIO", True)

    def leido(self):
        return {"243:190": "Honorarios profesionales", "311:2437": SUBSECCIONES[1]}

    def fragmentos(self):
        # 243:190 aparece solo en el fragmento de honorarios.
        return {"243:190": ["honorarios"], "311:2437": ["huella-uno"]}

    async def veredicto_con(self, pasaje, fragmentos=None):
        return await verificar(investigar(
            con_respaldo("Fallos: 243:190", pasaje), cita("Fallos: 311:2437")),
            self.leido(), {**LEIDOS, **self.TEXTOS}, fragmentos or self.fragmentos())

    async def test_un_pasaje_de_otro_documento_no_se_le_muestra_al_lector(self):
        veredicto = await self.veredicto_con(self.PASAJE_AJENO)
        assert "243:190" not in dict(veredicto.pasajes_propios)

    async def test_pero_la_cita_se_publica_igual(self):
        # El pasaje está en lo leído, así que no es inventado: la cita pasa, y lo único que
        # pierde es el pasaje a la vista.
        veredicto = await self.veredicto_con(self.PASAJE_AJENO)
        assert "Fallos: 243:190" in veredicto.verificadas
        assert veredicto.sin_respaldo == ()

    async def test_un_pasaje_del_fragmento_del_fallo_si_se_muestra(self):
        veredicto = await self.veredicto_con(self.PASAJE_PROPIO)
        assert dict(veredicto.pasajes_propios)["243:190"] == self.PASAJE_PROPIO

    async def test_con_la_busqueda_abierta_el_pasaje_ajeno_pasaba(self):
        # Deja constancia de la falla: el pasaje ajeno esta en lo leido, y eso alcanzaba.
        assert respaldado(self.PASAJE_AJENO, list(self.TEXTOS.values()))
        assert not respaldado(self.PASAJE_AJENO, [self.HONORARIOS])

    async def test_un_fallo_en_varios_fragmentos_se_muestra_con_cualquiera(self):
        fragmentos = {"243:190": ["decretos", "honorarios"], "311:2437": ["huella-uno"]}
        veredicto = await self.veredicto_con(self.PASAJE_AJENO, fragmentos)
        assert dict(veredicto.pasajes_propios)["243:190"] == self.PASAJE_AJENO

    async def test_sin_registro_de_fragmentos_no_se_muestra_ningun_pasaje(self):
        # Falla cerrada: sin saber donde aparecio el fallo no hay como afirmar que el pasaje
        # es suyo, y la ficha va sin el.
        veredicto = await verificar(investigar(
            cita("Fallos: 311:2437"), cita("Fallos: 315:1848")), LEIDO, LEIDOS, {})
        assert veredicto.pasajes_propios == ()
        assert len(veredicto.verificadas) == 2

    async def test_un_pasaje_que_no_esta_en_nada_leido_si_veta(self):
        veredicto = await self.veredicto_con("una oracion que no figura en ninguno de los textos leidos")
        assert veredicto.sin_respaldo == ("Fallos: 243:190",)
        assert "Fallos: 243:190" not in veredicto.verificadas

    async def test_de_dos_pasajes_del_mismo_fallo_se_guarda_el_que_es_propio(self):
        # Mazzeo: dos afirmaciones, un pasaje de su fragmento y otro de otro documento. Lo que
        # queda registrado es el propio, y no un visto bueno al fallo que la ficha usaba para
        # mostrar cualquiera de los dos.
        veredicto = await verificar(investigar(
            con_respaldo("Fallos: 243:190", self.PASAJE_PROPIO),
            con_respaldo("Fallos: 243:190", self.PASAJE_AJENO),
            cita("Fallos: 311:2437")),
            self.leido(), {**LEIDOS, **self.TEXTOS}, self.fragmentos())
        assert dict(veredicto.pasajes_propios)["243:190"] == self.PASAJE_PROPIO

    async def test_con_los_dos_pasajes_ajenos_no_se_guarda_ninguno(self):
        veredicto = await verificar(investigar(
            con_respaldo("Fallos: 243:190", self.PASAJE_AJENO),
            con_respaldo("Fallos: 243:190", "tenencia de activos financieros en un momento determinado"),
            cita("Fallos: 311:2437")),
            self.leido(), {**LEIDOS, **self.TEXTOS}, self.fragmentos())
        assert "243:190" not in dict(veredicto.pasajes_propios)


class TestUnaListaCortada:
    """La lista de citas que el esquema le corta al modelo se rechaza y no rompe la corrida.

    Medido con «arbitrariedad y error de derecho» sobre el cuadernillo: el investigador copió
    una lista del corpus, el `max_length` del campo se la cortó en «…; Fallos: » con un espacio
    al final, y el verificador moría con «la respuesta no cubre 1 de 3 citas». La corrida entera
    terminaba en error en 1 de cada 4 intentos, en vez de volver a corrección.
    """

    CORTADA = ("Fallos: 344:1070; FRO 011422/2013/1/RH001; Fallos: 343:919; Fallos: 339:499; "
               "Fallos: 326:3485; Fallos: 326:297; Fallos: ")

    async def test_no_levanta_error(self):
        veredicto = await verificar(investigar(
            cita(self.CORTADA), cita("Fallos: 311:2437"), cita("Fallos: 315:1848")),
            LEIDO, LEIDOS, FRAGMENTOS)
        assert veredicto is not None

    async def test_se_rechaza_como_citas_amontonadas(self):
        veredicto = await verificar(investigar(
            cita(self.CORTADA), cita("Fallos: 311:2437"), cita("Fallos: 315:1848")),
            LEIDO, LEIDOS, FRAGMENTOS)
        assert veredicto.aprobado is False
        assert any("junta varias citas" in o for o in veredicto.observaciones)

    async def test_el_parser_aguanta_la_cita_sin_recortar(self):
        # Sin pasar por el validador de `Cita`: el parser del veredicto tiene que resistir solo,
        # porque la cita que le llega la escribió el modelo.
        sin_validar = Cita.model_construct(fallo=self.CORTADA, respaldo=RESPALDO,
                                           afirmacion="Una afirmacion suficientemente larga.")
        veredicto = await verificar(investigar(
            sin_validar, cita("Fallos: 311:2437"), cita("Fallos: 315:1848")),
            LEIDO, LEIDOS, FRAGMENTOS)
        assert veredicto.aprobado is False
