"""La construcción del índice: chunking, subsecciones y el mapa de citas.

Todo se ejerce sobre cadenas y `Document` en memoria: sin disco, sin API de embeddings y sin
Chroma. Lo que se comprueba es que un extracto de doctrina llegue entero a su fragmento y con
la subsección correcta, porque la subsección es la clave con la que después se piden los
fallos de esa parte del cuadernillo.
"""

import json

from langchain_core.documents import Document

from app.nucleo import constantes as cfg
from app.rag.ingesta import markdown as ingesta

DOCUMENTO = """# 6 Sentencias arbitrarias — 6.2 Causales

### 6.2.7 Exceso ritual manifiesto

El exceso ritual manifiesto configura una causal autonoma de arbitrariedad cuando la forma
sacrifica la verdad juridica objetiva.

Fuente: [Fallos: 238:550](https://sj.csjn.gov.ar/fallo/238-550)

---

### 6.2.8 Fundamentacion aparente

La fundamentacion aparente descalifica la sentencia como acto jurisdiccional valido.

Fuente: [Fallos: 311:2437](https://sj.csjn.gov.ar/fallo/311-2437)
"""


class TestQuitarHipervinculos:
    """Las URLs salen del texto que se embebe y viajan en un mapa aparte."""

    def test_la_cita_queda_en_texto_plano(self):
        texto, urls = ingesta._quitar_hipervinculos(
            "Fuente: [Fallos: 238:550](https://sj.csjn.gov.ar/fallo/238-550)")
        assert texto == "Fuente: Fallos: 238:550"
        assert urls == {"Fallos: 238:550": "https://sj.csjn.gov.ar/fallo/238-550"}

    def test_la_cita_sigue_siendo_buscable_en_el_texto(self):
        # BM25 y el embedding ven la cita porque la línea `Fuente:` queda en el contenido:
        # es lo que permite recuperar por el número de fallo.
        texto, _ = ingesta._quitar_hipervinculos(DOCUMENTO)
        assert "Fallos: 238:550" in texto
        assert "https://" not in texto

    def test_un_texto_sin_links_no_cambia(self):
        texto, urls = ingesta._quitar_hipervinculos("Sin ninguna cita enlazada.")
        assert texto == "Sin ninguna cita enlazada."
        assert urls == {}

    def test_la_primera_url_de_una_cita_repetida_es_la_que_vale(self):
        _, urls = ingesta._quitar_hipervinculos(
            "[Fallos: 238:550](https://a) y despues [Fallos: 238:550](https://b)")
        assert urls == {"Fallos: 238:550": "https://a"}


class TestSubtituloVigente:
    """A qué subsección pertenece cada fragmento."""

    def test_toma_el_ultimo_subtitulo_que_lo_precede(self):
        posicion = DOCUMENTO.index("La fundamentacion aparente")
        assert ingesta._subtitulo_vigente(DOCUMENTO, posicion) == "6.2.8 Fundamentacion aparente"

    def test_antes_del_primer_subtitulo_vale_el_primero(self):
        # El contenido bajo el título principal pertenece a la primera subsección: dejarlo
        # sin valor deja la cita sin procedencia que mostrarle al lector.
        assert ingesta._subtitulo_vigente(DOCUMENTO, 0) == "6.2.7 Exceso ritual manifiesto"

    def test_un_documento_sin_subtitulos_devuelve_vacio(self):
        assert ingesta._subtitulo_vigente("# Solo un titulo\n\nTexto.", 20) == ""


class TestFragmentar:
    """El chunking y los metadatos que cada fragmento se lleva."""

    def documento(self) -> Document:
        texto, urls = ingesta._quitar_hipervinculos(DOCUMENTO)
        return Document(page_content=texto, metadata={
            "origen": "6-2-causales.md",
            "seccion": texto.splitlines()[0].lstrip("# ").strip(),
            ingesta.CLAVE_URLS: urls,
        })

    def test_cada_fragmento_lleva_su_subseccion_y_sus_tokens(self):
        fragmentos = ingesta.fragmentar([self.documento()])
        assert fragmentos
        for f in fragmentos:
            assert f.metadata["subseccion"]
            assert f.metadata["tokens"] > 0
            assert f.metadata["origen"] == "6-2-causales.md"

    def test_el_mapa_de_urls_viaja_serializado(self):
        # Chroma solo acepta metadatos escalares.
        fragmentos = ingesta.fragmentar([self.documento()])
        urls = json.loads(fragmentos[0].metadata["citas_urls"])
        assert isinstance(fragmentos[0].metadata["citas_urls"], str)
        assert all(u.startswith("https://") for u in urls.values())

    def test_cada_fragmento_solo_trae_las_urls_de_sus_propias_citas(self):
        fragmentos = ingesta.fragmentar([self.documento()])
        for f in fragmentos:
            for cita in json.loads(f.metadata["citas_urls"]):
                assert cita in f.page_content

    def test_un_extracto_corto_no_se_parte(self):
        # El separador de extractos va primero en la lista: ningún extracto se corta al medio.
        fragmentos = ingesta.fragmentar([self.documento()])
        completo = [f for f in fragmentos if "exceso ritual manifiesto configura" in f.page_content]
        assert len(completo) == 1
        assert "Fallos: 238:550" in completo[0].page_content

    def test_ningun_fragmento_supera_el_techo_de_tokens(self):
        fragmentos = ingesta.fragmentar([self.documento()])
        assert all(f.metadata["tokens"] <= cfg.TAMANO_CHUNK_TOKENS for f in fragmentos)


class TestContarTokens:
    """El tokenizador con el que se mide el chunking."""

    def test_un_texto_vacio_tiene_cero_tokens(self):
        assert ingesta.contar_tokens("") == 0

    def test_un_texto_mas_largo_tiene_mas_tokens(self):
        assert ingesta.contar_tokens("una frase corta") < ingesta.contar_tokens(DOCUMENTO)

    def test_un_modelo_desconocido_cae_al_codificador_por_defecto(self):
        assert ingesta.contar_tokens("texto de prueba", modelo="modelo-inexistente") > 0
