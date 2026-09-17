"""La extracción de un PDF: texto, hipervínculos y de dónde sale cada cita.

El PDF de prueba se construye en memoria con PyMuPDF, así que la suite no baja nada y no
depende de un binario versionado que nadie sabe reproducir. Lo que se ejercita es lo mismo que
corre contra los documentos reales.
"""

import fitz
import pytest

from app.rag import citas as c
from app.rag.ingesta import pdf

BASE_PUBLICA = "https://sjconsulta.csjn.gov.ar/sjconsulta"
URL_TOMO = BASE_PUBLICA + "/consultaSumarios/buscarTomoPagina.html?tomo={t}&pagina={p}"
URL_DOC = BASE_PUBLICA + "/documentos/verDocumentoByIdLinksJSP.html?idDocumento={i}"


def armar_pdf(paginas: list[dict]) -> bytes:
    """Un PDF en memoria. Cada página trae líneas de texto y links con su ancla."""
    documento = fitz.open()
    for contenido in paginas:
        pagina = documento.new_page(width=595, height=842)
        y = 80
        for linea in contenido.get("lineas", []):
            texto = linea["texto"] if isinstance(linea, dict) else linea
            tamano = linea.get("tamano", 11) if isinstance(linea, dict) else 11
            pagina.insert_text((60, y), texto, fontsize=tamano)
            if isinstance(linea, dict) and linea.get("url"):
                ancho = fitz.get_text_length(texto, fontsize=tamano)
                pagina.insert_link({
                    "kind": fitz.LINK_URI, "uri": linea["url"],
                    "from": fitz.Rect(60, y - tamano, 60 + ancho, y + 4),
                })
            y += tamano + 8
    datos = documento.tobytes()
    documento.close()
    return datos


@pytest.fixture
def documento() -> pdf.Documento:
    datos = armar_pdf([
        {"lineas": [
            {"texto": "1. Principios generales", "tamano": 15},
            {"texto": "La Corte sostuvo la tutela del derecho a la imagen."},
            {"texto": "348:821", "url": URL_TOMO.format(t=348, p=821)},
            {"texto": "Ver el precedente Campodonico", "url": URL_DOC.format(i=8130241)},
        ]},
        {"lineas": [
            {"texto": "2. Excepciones", "tamano": 15},
            {"texto": "El interes publico admite excepciones (Fallos: 306:1892)."},
        ]},
    ])
    return pdf.extraer(datos, "nota-9999", "Derecho a la imagen")


class TestExtraccion:
    """Lo que se saca de cada página."""

    def test_devuelve_una_pagina_por_pagina(self, documento):
        assert len(documento.paginas) == 2
        assert documento.paginas[0].numero == 1

    def test_el_texto_llega_legible(self, documento):
        assert "tutela del derecho a la imagen" in documento.paginas[0].texto

    def test_los_titulos_por_tipografia_se_detectan(self, documento):
        assert "1. Principios generales" in documento.paginas[0].titulos
        assert "2. Excepciones" in documento.paginas[1].titulos

    def test_una_linea_de_cuerpo_no_es_titulo(self, documento):
        titulos = documento.paginas[0].titulos
        assert not any("tutela del derecho" in t for t in titulos)


class TestCitasYLinks:
    """De dónde sale cada cita, que es la promesa central del sistema."""

    def test_la_cita_sale_de_los_parametros_de_la_url(self, documento):
        # `buscarTomoPagina?tomo=348&pagina=821` la escribió la propia Secretaría: es la
        # lectura más firme, sin texto de por medio.
        assert "348:821" in documento.paginas[0].citas_urls

    def test_con_id_opaco_la_cita_sale_del_ancla(self):
        datos = armar_pdf([{"lineas": [
            {"texto": "343:2255 caso citado", "url": URL_DOC.format(i=7630241)},
        ]}])
        documento = pdf.extraer(datos, "suplemento-9999", "Consumidores")
        assert "343:2255" in documento.citas_urls

    def test_un_ancla_sin_tomo_pagina_no_entra_al_padron(self):
        # Las referencias por expediente y fecha quedan fuera: el padrón es de tomo y página.
        datos = armar_pdf([{"lineas": [
            {"texto": "C. 623. XLV. Compania Financiera, 10/12/2013",
             "url": URL_DOC.format(i=7630241)},
        ]}])
        documento = pdf.extraer(datos, "suplemento-9998", "Consumidores")
        assert documento.citas_urls == {}

    def test_un_desacuerdo_entre_ancla_y_url_se_cuenta(self):
        datos = armar_pdf([{"lineas": [
            {"texto": "311:2437", "url": URL_TOMO.format(t=348, p=821)},
        ]}])
        documento = pdf.extraer(datos, "nota-9997", "Con desacuerdo")
        assert documento.desacuerdos == 1
        # Gana la URL: es el dato que la Secretaría escribió como parámetro.
        assert "348:821" in documento.citas_urls

    def test_el_host_interno_se_reescribe_al_publico(self):
        interno = URL_TOMO.format(t=340, p=1).replace(
            "sjconsulta.csjn.gov.ar", "sjintranet.csjn.gov.ar")
        datos = armar_pdf([{"lineas": [{"texto": "340:1", "url": interno}]}])
        documento = pdf.extraer(datos, "nota-9996", "Con intranet")
        assert documento.citas_urls["340:1"].startswith(
            "https://sjconsulta.csjn.gov.ar/sjconsulta/")

    def test_una_url_con_puerto_queda_sin_link(self):
        # Un puerto explícito es un servicio interno: se encontraron seis citas apuntando a
        # `csjn14.csjn.gov.ar:7003`, que desde afuera no abre.
        datos = armar_pdf([{"lineas": [
            {"texto": "338:1", "url": "http://csjn14.csjn.gov.ar:7003/sj/verTomo?t=338"},
        ]}])
        documento = pdf.extraer(datos, "suplemento-9992", "Con host interno")
        assert documento.citas_urls.get("338:1", "") == ""

    def test_una_url_al_aplicativo_retirado_queda_sin_link(self):
        # `tomosFallos.do` responde 200 y sirve la home del sitio. Las 17 citas del padrón que
        # lo enlazaban se comprobaron una por una comparando el cuerpo contra esa home.
        datos = armar_pdf([{"lineas": [
            {"texto": "323:1755",
             "url": "https://sj.csjn.gov.ar/sj/tomosFallos.do"
                    "?method=verTomoPagina&tomo=323&pagina=1755"},
        ]}])
        documento = pdf.extraer(datos, "suplemento-9990", "Con aplicativo retirado")
        assert documento.citas_urls.get("323:1755", "") == ""

    def test_una_url_en_claro_pasa_a_https(self):
        datos = armar_pdf([{"lineas": [
            {"texto": "337:1", "url": "http://sjservicios.csjn.gov.ar/sj/verTomoPagina?t=193"},
        ]}])
        documento = pdf.extraer(datos, "suplemento-9991", "Con http")
        assert documento.citas_urls["337:1"].startswith("https://")

    def test_un_path_interno_ajeno_queda_sin_link(self):
        datos = armar_pdf([{"lineas": [
            {"texto": "339:1", "url": "https://sjintranet.csjn.gov.ar/homeSJ/interno"},
        ]}])
        documento = pdf.extraer(datos, "nota-9995", "Con intranet ajena")
        # La cita del texto sigue existiendo para el padrón; el link se descarta.
        assert documento.citas_urls.get("339:1", "") == ""


class TestNormalizacion:
    """Los tres arreglos que hacen falta por cómo el PDF corta las líneas."""

    def test_una_cita_partida_por_el_salto_vuelve_a_ser_una(self):
        assert "332:111" in pdf.normalizar("Fallos: \n332:111")

    def test_una_palabra_cortada_con_guion_se_une(self):
        assert "arbitrariedad" in pdf.normalizar("arbitrarie-\ndad manifiesta")

    def test_las_lineas_en_blanco_de_mas_se_colapsan(self):
        assert pdf.normalizar("uno\n\n\n\n\ndos") == "uno\n\ndos"

    def test_la_cita_partida_se_encuentra_despues_de_normalizar(self):
        assert c.citas_del_texto(pdf.normalizar("Fallos: \n332:111")) == {"332:111"}


class TestFechaDeActualizacion:
    """La fecha de corte que el documento declara en su tapa."""

    def test_sale_de_la_tapa(self):
        datos = armar_pdf([{"lineas": ["Recurso Extraordinario", "Actualizado al 10/09/2026"]},
                           {"lineas": ["Indice"]}])
        documento = pdf.extraer(datos, "suplemento-3", "Recurso Extraordinario")
        assert pdf.fecha_de_actualizacion([p.texto for p in documento.paginas]) == "10/09/2026"

    def test_un_documento_que_no_la_declara_queda_sin_fecha(self):
        assert pdf.fecha_de_actualizacion(["Derecho a la Salud", "Contenido"]) == ""

    def test_una_fecha_en_el_cuerpo_no_es_la_de_la_edicion(self):
        # Una sentencia citada en la página 40 puede decir "actualizado al" sin ser la tapa.
        paginas = ["Tapa", "Indice", "Cuerpo"] + ["Actualizado al 01/01/2020"]
        assert pdf.fecha_de_actualizacion(paginas) == ""


class TestCromo:
    """El encabezado que se repite en todas las páginas es imprenta, no doctrina."""

    def test_una_linea_presente_en_todas_las_paginas_se_saca(self):
        cromo = "Secretaria de Jurisprudencia - Corte Suprema de Justicia"
        datos = armar_pdf([
            {"lineas": [cromo, f"Contenido propio de la pagina {n}."]} for n in range(1, 8)
        ])
        documento = pdf.extraer(datos, "suplemento-9994", "Con cromo")
        assert all(cromo not in p.texto for p in documento.paginas)
        assert "Contenido propio de la pagina 3" in documento.paginas[2].texto

    def test_un_documento_corto_conserva_todo(self):
        # Con pocas páginas, una línea repetida puede ser contenido: el umbral no aplica.
        datos = armar_pdf([{"lineas": ["Encabezado", "Cuerpo"]} for _ in range(3)])
        documento = pdf.extraer(datos, "nota-9993", "Corto")
        assert "Encabezado" in documento.paginas[0].texto
