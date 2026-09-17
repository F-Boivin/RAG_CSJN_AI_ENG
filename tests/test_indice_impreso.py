"""El corte por índice impreso: cada fragmento con el título que de verdad lo precede.

El PDF se arma en memoria con PyMuPDF, como en `test_pdf.py`, así que corre la misma extracción
que contra el «Recurso Extraordinario». El documento de prueba trae los casos que ese libro
tiene: un título a mitad de página, uno que cae en otra página que la del índice, una entrada
del índice en dos renglones, otra cuya página va sin puntos guía, y títulos seguidos sin texto
entre ellos.
"""

import pytest

from app.nucleo.errores import ErrorRAG
from app.rag.ingesta import indice_impreso, pdf, segmentacion
from app.rag.ingesta.pdf import Documento, Pagina
from tests.test_pdf import armar_pdf

FICHA = {"origen": "suplemento-3", "tipo": "suplemento", "titulo": "Recurso Extraordinario",
         "url": "https://sj.csjn.gov.ar/homeSJ/suplementos/suplemento/3/documento"}

TITULO_LARGO = "1.3 Un titulo largo que no entra en un solo renglon del indice"
TITULO_SIN_PUNTOS = "1.4 Un titulo que llena el renglon y deja la pagina sin puntos"


def sumario(tema: str) -> list[str]:
    """Un sumario de tres renglones, con largo suficiente para pasar el filtro de calidad."""
    return [f"Es inadmisible el recurso sobre {tema} si no rebate los fundamentos",
            f"del pronunciamiento apelado, y la Corte lo reitera para {tema}",
            "(Fallos: 330:3248)."]


def paginas(sin_titulo: str = "", indice: list[str] | None = None) -> list[dict]:
    """Tapa, índice y tres páginas de cuerpo. `sin_titulo` saca ese título del cuerpo."""
    cuerpo = [
        ["1 Interposicion", "1.1 Quienes pueden interponerlo",
         *sumario("la legitimacion del letrado"),
         "1.2 Plazo para interponerlo", *sumario("el plazo de diez dias")],
        [*sumario("el computo del plazo"),
         TITULO_LARGO, "y sigue en el de abajo", *sumario("el titulo largo"),
         TITULO_SIN_PUNTOS, *sumario("la pagina sin puntos")],
        ["2 Trámite", "2.1 Sustanciacion", *sumario("la sustanciacion del recurso")],
    ]
    return [
        {"lineas": ["Recurso de prueba", "Actualizado al 10/09/2026"]},
        {"lineas": indice or [
            "Indice",
            "1 Interposicion ...................................................... 3",
            "1.1 Quienes pueden interponerlo ................................... 3",
            "1.2 Plazo para interponerlo ........................................ 3",
            TITULO_LARGO,
            "y sigue en el de abajo ................................................ 4",
            f"{TITULO_SIN_PUNTOS} 4",
            "2 Trámite ............................................................. 4",
            "2.1 Sustanciacion ................................................... 5",
        ]},
        *({"lineas": [renglon for renglon in pagina if renglon != sin_titulo]}
          for pagina in cuerpo),
    ]


def extraer(contenido: list[dict]) -> Documento:
    return pdf.extraer(armar_pdf(contenido), "suplemento-3", "Recurso Extraordinario")


@pytest.fixture(scope="module")
def documento() -> Documento:
    return extraer(paginas())


@pytest.fixture(scope="module")
def fragmentos(documento) -> list[dict]:
    return segmentacion.fragmentar(documento, FICHA)[0]


def el_que_dice(fragmentos: list[dict], texto: str) -> dict:
    return next(f for f in fragmentos if texto in f["texto"])


class TestLecturaDelIndice:
    """Los títulos y sus páginas, tal como el índice los escribe."""

    def test_lee_cada_titulo_con_la_pagina_que_declara(self, documento):
        entradas, ultima = indice_impreso.leer_indice(documento)
        assert [(e.nombre, e.pagina) for e in entradas] == [
            ("1 Interposicion", 3),
            ("1.1 Quienes pueden interponerlo", 3),
            ("1.2 Plazo para interponerlo", 3),
            (f"{TITULO_LARGO} y sigue en el de abajo", 4),
            (TITULO_SIN_PUNTOS, 4),
            ("2 Trámite", 4),
            ("2.1 Sustanciacion", 5),
        ]
        assert ultima == 2

    def test_un_documento_sin_indice_sigue_la_cascada(self):
        sin_indice = Documento(origen="nota-1", titulo="Nota", paginas=[
            Pagina(numero=n, texto=f"Contenido de la pagina {n}. " * 40) for n in range(1, 12)])
        assert indice_impreso.leer_indice(sin_indice) == ([], 0)
        assert segmentacion.subsecciones(sin_indice)[1] != segmentacion.INDICE_IMPRESO

    def test_una_numeracion_que_saltea_corta_con_error(self):
        # Un renglón mal leído rompe la secuencia; seguir cortando con él daría secciones
        # con el título equivocado.
        indice = paginas()[1]["lineas"]
        indice = [r.replace("1.2 Plazo", "1.5 Plazo") for r in indice]
        with pytest.raises(ErrorRAG, match="no sigue a «1.1 Quienes pueden interponerlo»"):
            indice_impreso.leer_indice(extraer(paginas(indice=indice)))

    def test_un_titulo_que_termina_sin_pagina_corta_con_error(self):
        indice = paginas()[1]["lineas"][:-1] + ["2.1 Sustanciacion"]
        with pytest.raises(ErrorRAG, match="termina sin la página"):
            indice_impreso.leer_indice(extraer(paginas(indice=indice)))


class TestCortePorTitulo:
    """El texto se corta en el renglón del título, no en el borde de la página."""

    def test_el_metodo_es_el_indice_impreso(self, documento):
        assert segmentacion.subsecciones(documento)[1] == segmentacion.INDICE_IMPRESO

    def test_un_titulo_a_mitad_de_pagina_corta_en_su_renglon(self, fragmentos):
        # Los dos sumarios están en la página 3; cortar por página los juntaría.
        legitimacion = el_que_dice(fragmentos, "la legitimacion del letrado")
        plazo = el_que_dice(fragmentos, "el plazo de diez dias")
        assert legitimacion["subseccion"] == "1.1 Quienes pueden interponerlo"
        assert plazo["subseccion"] == "1.2 Plazo para interponerlo"
        assert legitimacion["pagina"] == plazo["pagina"] == 3

    def test_la_seccion_sigue_en_la_pagina_siguiente_hasta_el_proximo_titulo(self, fragmentos):
        computo = el_que_dice(fragmentos, "el computo del plazo")
        assert computo["subseccion"] == "1.2 Plazo para interponerlo"
        assert not any("el titulo largo" in f["texto"]
                       for f in fragmentos if f["subseccion"].startswith("1.2"))

    def test_un_titulo_en_otra_pagina_que_la_del_indice_se_encuentra(self, fragmentos):
        # El índice pone «2 Trámite» en la página 4 y el cuerpo lo trae en la 5.
        sustanciacion = el_que_dice(fragmentos, "la sustanciacion del recurso")
        assert sustanciacion["subseccion"] == "2.1 Sustanciacion"
        assert sustanciacion["pagina"] == 5

    def test_un_titulo_de_dos_renglones_no_queda_en_el_texto(self, fragmentos):
        largo = el_que_dice(fragmentos, "el titulo largo si no")
        assert largo["subseccion"] == f"{TITULO_LARGO} y sigue en el de abajo"
        assert "y sigue en el de abajo" not in largo["texto"]

    def test_la_pagina_sin_puntos_guia_arma_su_seccion(self, fragmentos):
        assert el_que_dice(fragmentos, "la pagina sin puntos")["subseccion"] == TITULO_SIN_PUNTOS

    def test_la_tapa_y_el_indice_quedan_afuera(self, fragmentos):
        assert all(f["pagina"] >= 3 for f in fragmentos)
        assert not any("...." in f["texto"] or "Actualizado" in f["texto"] for f in fragmentos)

    def test_la_seccion_del_fragmento_es_su_capitulo(self, fragmentos):
        assert el_que_dice(fragmentos, "el plazo de diez dias")["seccion"] == "1 Interposicion"
        assert el_que_dice(fragmentos, "la sustanciacion")["seccion"] == "2 Trámite"

    def test_el_nombre_va_sin_prefijo_y_los_titulos_sin_texto_no_dan_fragmentos(self, fragmentos):
        # «1 Interposicion» y «2 Trámite» van seguidos de otro título: no tienen texto propio,
        # y no toman el de su página.
        assert {f["subseccion"] for f in fragmentos} == {
            "1.1 Quienes pueden interponerlo", "1.2 Plazo para interponerlo",
            f"{TITULO_LARGO} y sigue en el de abajo", TITULO_SIN_PUNTOS, "2.1 Sustanciacion"}

    def test_un_titulo_que_no_esta_en_el_cuerpo_corta_con_error(self):
        documento = extraer(paginas(sin_titulo="1.2 Plazo para interponerlo"))
        with pytest.raises(ErrorRAG, match="«1.2 Plazo para interponerlo» en la página 3"):
            segmentacion.fragmentar(documento, FICHA)

    def test_el_encabezado_es_la_cadena_de_titulos_sin_numeracion(self, fragmentos):
        # Es lo que se embebe delante del texto: un sumario casi nunca repite el título que
        # lo agrupa.
        plazo = el_que_dice(fragmentos, "el plazo de diez dias")
        sustanciacion = el_que_dice(fragmentos, "la sustanciacion")
        assert plazo["encabezado"] == "Interposicion › Plazo para interponerlo"
        assert sustanciacion["encabezado"] == "Trámite › Sustanciacion"
