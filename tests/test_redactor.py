"""La guarda del redactor: qué citas del texto final cuentan como verificadas.

Es la última comprobación del sistema. El verificador dictamina sobre las citas que el
investigador propuso, pero el redactor es quien escribe el texto que lee la persona, y una
cita agregada en ese último paso aparecería justo después de haberlas comprobado todas.

`auditar_citas` y `juzgar_texto` son puras y están separadas del nodo para poder probarlas
sin montar el grafo ni llamar a ningún modelo.
"""

import pytest

from app.agentes.redactor import (
    auditar_citas,
    juzgar_texto,
    resumen_redaccion,
    _texto_de,
)
from app.nucleo import constantes as cfg

VERIFICADAS = ("Fallos: 338:623", "Fallos: 349:148", "Fallos: 311:2437")
MATERIAL = "huella1234567890"


def redactar(texto: str, verificadas=VERIFICADAS, llamadas: int = 1):
    return juzgar_texto(texto, verificadas, MATERIAL, llamadas)


class TestAuditarCitas:
    """La separación entre lo aprobado y lo que se coló."""

    def test_una_cita_aprobada_cuenta_como_usada(self):
        usadas, intrusas = auditar_citas("Según Fallos: 338:623, la doctrina exige…", VERIFICADAS)
        assert usadas == ("338:623",)
        assert intrusas == ()

    def test_una_cita_que_el_verificador_no_aprobo_es_intrusa(self):
        usadas, intrusas = auditar_citas("Ver Fallos: 999:9999 sobre el punto.", VERIFICADAS)
        assert usadas == ()
        assert intrusas == ("999:9999",)

    def test_compara_normalizado_y_no_la_cadena_cruda(self):
        # El redactor escribe "Fallos: 338:623" o "los fallos 338:623 y 349:148" según le
        # quede la oración: comparar las cadenas daría por distinto lo que es igual.
        usadas, intrusas = auditar_citas("los fallos 338:623 y 349:148 lo sostienen", VERIFICADAS)
        assert set(usadas) == {"338:623", "349:148"}
        assert intrusas == ()

    def test_la_pagina_abreviada_hereda_el_tomo(self):
        # Es como cita el propio cuadernillo, así que es la forma que el modelo imita.
        usadas, intrusas = auditar_citas(
            "(Fallos: 338:623 y 700)", ("Fallos: 338:623", "Fallos: 338:700"))
        assert set(usadas) == {"338:623", "338:700"}
        assert intrusas == ()

    def test_un_horario_no_se_confunde_con_una_cita(self):
        # Sin el ancla en la palabra "fallos", esto dispararía una reescritura que no
        # converge: el redactor no puede sacar una cita que nunca escribió.
        usadas, intrusas = auditar_citas("La audiencia de las 14:30 fue suspendida.", VERIFICADAS)
        assert usadas == ()
        assert intrusas == ()

    def test_un_resultado_de_votacion_tampoco(self):
        usadas, intrusas = auditar_citas("El voto fue 3:2 en contra.", VERIFICADAS)
        assert intrusas == ()

    def test_un_texto_sin_citas_no_usa_ninguna(self):
        usadas, intrusas = auditar_citas("Un texto sin ninguna referencia.", VERIFICADAS)
        assert usadas == ()
        assert intrusas == ()

    def test_una_lista_de_verificadas_vacia_vuelve_intrusa_a_toda_cita(self):
        usadas, intrusas = auditar_citas("Ver Fallos: 338:623.", ())
        assert usadas == ()
        assert intrusas == ("338:623",)


class TestJuzgarTexto:
    """El veredicto completo sobre un texto."""

    def test_un_texto_bien_fundado_queda_limpio(self):
        redaccion = redactar("Se apoya en Fallos: 338:623 y en Fallos: 349:148.")
        assert redaccion.limpia is True
        assert redaccion.motivos == ()
        assert len(redaccion.citas_usadas) == 2

    def test_una_cita_intrusa_ensucia_la_redaccion(self):
        redaccion = redactar(
            "Se apoya en Fallos: 338:623, Fallos: 349:148 y Fallos: 999:9999.")
        assert redaccion.limpia is False
        assert redaccion.citas_intrusas == ("999:9999",)
        assert any("999:9999" in m for m in redaccion.motivos)

    def test_pocas_citas_tambien_ensucian(self):
        # Estar fundado es citar: sin este control, "limpia" solo querría decir "no mintió".
        redaccion = redactar("Se apoya solamente en Fallos: 338:623.")
        assert redaccion.limpia is False
        assert any(str(cfg.CITAS_MINIMAS) in m for m in redaccion.motivos)

    def test_un_texto_sin_ninguna_cita_no_pasa(self):
        redaccion = redactar("Una respuesta en prosa, sin ninguna referencia concreta.")
        assert redaccion.limpia is False
        assert redaccion.citas_usadas == ()

    def test_los_dos_motivos_se_acumulan(self):
        redaccion = redactar("Se apoya solo en Fallos: 999:9999.")
        assert redaccion.limpia is False
        assert len(redaccion.motivos) == 2      # intrusa y pocas citas

    def test_el_material_y_las_llamadas_viajan_en_el_artefacto(self):
        redaccion = redactar("Fallos: 338:623 y Fallos: 349:148 lo sostienen.", llamadas=3)
        assert redaccion.sobre_material == MATERIAL
        assert redaccion.llamadas == 3


class TestResumenRedaccion:
    """La línea de la traza sale del artefacto y de ningún otro lado."""

    def test_una_redaccion_limpia_informa_sus_citas(self):
        resumen = resumen_redaccion(redactar("Fallos: 338:623 y Fallos: 349:148 lo sostienen."))
        assert "2 citas verificadas" in resumen
        assert "RECHAZADA" not in resumen

    def test_una_rechazada_informa_sus_motivos(self):
        resumen = resumen_redaccion(redactar("Solo Fallos: 999:9999."))
        assert "RECHAZADA" in resumen
        assert "999:9999" in resumen

    def test_cuenta_las_llamadas_a_la_herramienta(self):
        resumen = resumen_redaccion(
            redactar("Fallos: 338:623 y Fallos: 349:148.", llamadas=4))
        assert "4 llamadas" in resumen


class TestTextoDeMensaje:
    """Lo que devuelve el modelo, normalizado a string."""

    class Mensaje:
        def __init__(self, content):
            self.content = content

    def test_un_string_se_devuelve_limpio(self):
        assert _texto_de(self.Mensaje("  un texto  ")) == "un texto"

    def test_una_lista_de_bloques_se_concatena(self):
        # La firma admite bloques, y un tipo inesperado reventaría con un AttributeError que
        # no menciona al redactor.
        assert _texto_de(self.Mensaje([{"text": "hola "}, {"text": "mundo"}])) == "hola mundo"

    def test_los_bloques_de_texto_plano_tambien(self):
        assert _texto_de(self.Mensaje(["hola ", "mundo"])) == "hola mundo"

    def test_un_bloque_de_tipo_inesperado_se_ignora(self):
        assert _texto_de(self.Mensaje([{"imagen": "x"}, {"text": "texto"}])) == "texto"

    def test_un_mensaje_sin_contenido_da_vacio(self):
        assert _texto_de(self.Mensaje(None)) == ""
        assert _texto_de(object()) == ""
