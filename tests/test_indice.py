"""Abrir un índice ya construido: qué se comprueba y de dónde salen los parámetros.

Es el contrato de arranque del servicio. Lo que se prueba acá es que el manifiesto mande sobre
el entorno, porque la falla contraria no se ve: un índice abierto con el modelo o las
dimensiones equivocadas devuelve resultados sin sentido en vez de fallar, y el healthcheck lo
da por sano.

Chroma no se monta: se captura con qué ajustes se lo habría construido, que es exactamente lo
que decide si los vectores son comparables.
"""

import json
import uuid

import pytest
from langchain_chroma import Chroma
from langchain_core.embeddings.fake import DeterministicFakeEmbedding

import app.nucleo.constantes as cfg
from app.nucleo.errores import ErrorRAG
from app.rag.ingesta import indice as mod
from tests.dobles import lexico_de_prueba

MANIFIESTO = {
    "documentos": 1, "fragmentos": 1, "huella": "5f29cca82ab9f71d",
    "modelo_embeddings": "text-embedding-3-small", "dimensiones": 512,
    "coleccion": "csjn_jurisprudencia",
}


@pytest.fixture
def indice_en_disco(tmp_path):
    """Un índice con la forma mínima que `abrir` exige: manifiesto y léxico con fragmentos."""
    def armar(manifiesto=None):
        directorio = tmp_path / "indice"
        directorio.mkdir(exist_ok=True)
        dir_chroma, ruta_lexico, ruta_manifiesto = mod.rutas(directorio)
        lexico_de_prueba(ruta_lexico, subsecciones=["6.1.1 Origen"]).cerrar()
        ruta_manifiesto.write_text(
            json.dumps({**MANIFIESTO, **(manifiesto or {})}), encoding="utf-8")
        return directorio
    return armar


@pytest.fixture
def capturar(monkeypatch):
    """Reemplaza Chroma y los embeddings, y devuelve con qué los habrían construido."""
    visto = {}

    def falso_crear_embeddings(ajustes=None):
        visto["modelo"] = ajustes.modelo_embeddings
        visto["dimensiones"] = ajustes.dimensiones_embeddings
        return object()

    def falso_chroma(**kwargs):
        visto["coleccion"] = kwargs.get("collection_name")
        return object()

    monkeypatch.setattr(mod, "crear_embeddings", falso_crear_embeddings)
    monkeypatch.setattr(mod, "Chroma", falso_chroma)
    return visto


def con_entorno(monkeypatch, **campos):
    """Fija los ajustes que `abrir` va a leer."""
    from app.nucleo.config import obtener_ajustes

    monkeypatch.setattr(mod, "obtener_ajustes",
                        lambda: obtener_ajustes().model_copy(update=campos))


class TestElManifiestoManda:
    """Un índice construido sabe con qué se hizo; el entorno solo sabe lo que alguien escribió."""

    def test_sin_dimensiones_en_el_entorno_las_toma_del_indice(
            self, indice_en_disco, capturar, monkeypatch):
        # Es la falla que motivó esto: faltando DIMENSIONES_EMBEDDINGS, el proveedor devolvía
        # vectores de 1536 contra una colección de 512, el arranque no se caía, y cada consulta
        # moría en la recuperación después de cobrar el cupo.
        con_entorno(monkeypatch, dimensiones_embeddings=None)
        mod.abrir(indice_en_disco())
        assert capturar["dimensiones"] == 512

    def test_la_coleccion_tambien_sale_del_manifiesto(
            self, indice_en_disco, capturar, monkeypatch):
        # Un nombre distinto hace que se cree una colección vacía en silencio: cero resultados
        # y ningún error.
        con_entorno(monkeypatch, nombre_coleccion="otra")
        mod.abrir(indice_en_disco({"coleccion": "csjn_jurisprudencia"}))
        assert capturar["coleccion"] == "csjn_jurisprudencia"

    def test_el_modelo_tambien_sale_del_manifiesto(
            self, indice_en_disco, capturar, monkeypatch):
        con_entorno(monkeypatch, dimensiones_embeddings=None)
        mod.abrir(indice_en_disco())
        assert capturar["modelo"] == "text-embedding-3-small"


class TestDesacuerdoDeclarado:
    """Cuando el entorno declara otra cosa, eso es una contradicción y se corta ruidoso."""

    def test_otras_dimensiones_declaradas_cortan(self, indice_en_disco, monkeypatch):
        con_entorno(monkeypatch, dimensiones_embeddings=1536)
        with pytest.raises(ErrorRAG):
            mod.abrir(indice_en_disco())

    def test_otro_modelo_declarado_corta(self, indice_en_disco, monkeypatch):
        # Dos modelos distintos dan vectores incomparables, y eso no se nota en los resultados.
        con_entorno(monkeypatch, modelo_embeddings="text-embedding-3-large")
        with pytest.raises(ErrorRAG):
            mod.abrir(indice_en_disco())

    def test_las_mismas_dimensiones_declaradas_pasan(
            self, indice_en_disco, capturar, monkeypatch):
        con_entorno(monkeypatch, dimensiones_embeddings=512)
        mod.abrir(indice_en_disco())
        assert capturar["dimensiones"] == 512


class TestIndiceInservible:
    """Los dos controles que ya estaban: que exista y que tenga algo adentro."""

    def test_sin_manifiesto_falla(self, tmp_path):
        with pytest.raises(ErrorRAG):
            mod.abrir(tmp_path / "no-existe")

    def test_un_lexico_vacio_falla(self, tmp_path, capturar, monkeypatch):
        directorio = tmp_path / "vacio"
        directorio.mkdir()
        _, ruta_lexico, ruta_manifiesto = mod.rutas(directorio)
        lexico_de_prueba(ruta_lexico).cerrar()
        ruta_manifiesto.write_text(json.dumps(MANIFIESTO), encoding="utf-8")
        con_entorno(monkeypatch, dimensiones_embeddings=None)
        with pytest.raises(ErrorRAG):
            mod.abrir(directorio)


class TestConstruccion:
    """Construyendo, el manifiesto todavía no existe: mandan los ajustes."""

    def test_sin_manifiesto_abrir_chroma_usa_el_entorno(
            self, tmp_path, capturar, monkeypatch):
        con_entorno(monkeypatch, dimensiones_embeddings=1536, nombre_coleccion="en_construccion")
        mod.abrir_chroma(tmp_path / "chroma", escritura=True)
        assert capturar["dimensiones"] == 1536
        assert capturar["coleccion"] == "en_construccion"


class TestEncabezadoEnElVector:
    """El vector lleva la cadena de títulos; el texto guardado, no.

    El texto guardado es el que lee el investigador y contra el que el verificador busca cada
    pasaje, y el lado léxico lo devuelve igual: si el vectorial devolviera otro texto, el
    ensamble no reconocería que los dos encontraron el mismo fragmento.
    """

    ENCABEZADO = "Sentencias arbitrarias › Causales de arbitrariedad › Excesos"
    TEXTO = "Es arbitraria la sentencia que se pronuncia sobre lo que ninguna parte pidio."

    def construir(self, tmp_path, monkeypatch, encabezado):
        embebidos = []

        class Grabador(DeterministicFakeEmbedding):
            def embed_documents(self, textos):
                embebidos.extend(textos)
                return super().embed_documents(textos)

        chroma = Chroma(collection_name=f"constructor-{uuid.uuid4().hex}",
                        embedding_function=Grabador(size=16))
        monkeypatch.setattr(mod, "abrir_chroma", lambda *a, **k: chroma)
        fragmento = {"id": "suplemento-3#0000", "origen": "suplemento-3",
                     "seccion": "6 Sentencias arbitrarias", "subseccion": "6.2.6.2 Excesos",
                     "encabezado": encabezado, "fuente": "suplemento", "pagina": 540,
                     "texto": self.TEXTO, "tokens": 20, "citas_urls": {}}
        with mod.Constructor(tmp_path / "indice") as constructor:
            constructor.indexar_documento([fragmento], {"origen": "suplemento-3",
                                                        "tipo": "suplemento", "titulo": "RE"})
        guardado = chroma.get(ids=["suplemento-3#0000"], include=["documents", "metadatas"])
        return embebidos, guardado

    def test_el_vector_se_calcula_con_los_titulos_delante(self, tmp_path, monkeypatch):
        embebidos, _ = self.construir(tmp_path, monkeypatch, self.ENCABEZADO)
        assert embebidos == [f"{self.ENCABEZADO}\n\n{self.TEXTO}"]

    def test_lo_que_se_guarda_es_el_texto_solo(self, tmp_path, monkeypatch):
        _, guardado = self.construir(tmp_path, monkeypatch, self.ENCABEZADO)
        assert guardado["documents"] == [self.TEXTO]
        assert guardado["metadatas"][0]["subseccion"] == "6.2.6.2 Excesos"

    def test_sin_encabezado_se_embebe_el_texto_tal_cual(self, tmp_path, monkeypatch):
        embebidos, guardado = self.construir(tmp_path, monkeypatch, "")
        assert embebidos == [self.TEXTO]
        assert guardado["documents"] == [self.TEXTO]
