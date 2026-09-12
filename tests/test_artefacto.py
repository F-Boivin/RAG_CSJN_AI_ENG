"""El índice como artefacto: empaquetado, descarga y la decisión de volver a bajarlo.

Es el único módulo que corre distinto en el despliegue que en la máquina de desarrollo, así
que lo que importa es la decisión: cuándo el servicio se baja el índice y cuándo se queda con
el que ya tiene. Un `MockTransport` reemplaza la red, y el índice de prueba es un directorio
con un manifiesto real.
"""

import json

import httpx
import pytest

from app.nucleo.errores import ErrorRAG
from app.rag.ingesta import artefacto
from app.rag.ingesta.indice import ARCHIVO_MANIFIESTO


@pytest.fixture
def indice(tmp_path):
    """Un directorio con la forma mínima que el extractor exige."""
    directorio = tmp_path / "indice"
    directorio.mkdir()
    (directorio / ARCHIVO_MANIFIESTO).write_text(
        json.dumps({"huella": "5f29cca82ab9f71d", "fragmentos": 3}), encoding="utf-8")
    (directorio / "lexico.sqlite3").write_bytes(b"contenido de prueba")
    return directorio


@pytest.fixture
def paquete(indice, tmp_path):
    """El índice empaquetado, con su sha256."""
    return artefacto.empaquetar(indice, tmp_path / "indice.tar.zst")


def servir(paquete_bytes: bytes):
    """Un transporte que devuelve esos bytes para cualquier GET."""
    return httpx.MockTransport(lambda pedido: httpx.Response(200, content=paquete_bytes))


@pytest.fixture
def red(monkeypatch):
    """Reemplaza la descarga por una lectura del artefacto local."""
    def instalar(ruta):
        def _bajar(url, destino):
            destino.write_bytes(ruta.read_bytes())
        monkeypatch.setattr(artefacto, "_bajar", _bajar)
    return instalar


class TestEmpaquetado:
    """Lo que se publica como asset del Release."""

    def test_el_paquete_se_escribe_con_su_sha256(self, paquete):
        ruta, huella = paquete
        assert ruta.exists()
        assert huella == artefacto.huella_de(ruta)
        assert len(huella) == 64

    def test_el_mismo_indice_da_el_mismo_paquete(self, indice, tmp_path):
        _, una = artefacto.empaquetar(indice, tmp_path / "a.tar.zst")
        _, otra = artefacto.empaquetar(indice, tmp_path / "b.tar.zst")
        assert una == otra


class TestDescarga:
    """El arranque baja el índice cuando falta, y lo comprueba antes de extraerlo."""

    def test_sin_indice_lo_baja_y_lo_extrae(self, paquete, tmp_path, red):
        ruta, huella = paquete
        red(ruta)
        destino = tmp_path / "volumen" / "indice"
        assert artefacto.asegurar(destino, "https://ejemplo/indice.tar.zst", huella) is True
        assert (destino / ARCHIVO_MANIFIESTO).exists()
        assert (destino / "lexico.sqlite3").read_bytes() == b"contenido de prueba"

    def test_un_artefacto_con_otra_huella_no_se_extrae(self, paquete, tmp_path, red):
        ruta, _ = paquete
        red(ruta)
        destino = tmp_path / "volumen" / "indice"
        with pytest.raises(ErrorRAG):
            artefacto.asegurar(destino, "https://ejemplo/indice.tar.zst", "sha256:" + "0" * 64)
        assert not destino.exists()

    def test_sin_url_no_hace_nada(self, tmp_path):
        # En desarrollo el índice se construye con el script y vive en disco.
        destino = tmp_path / "volumen" / "indice"
        assert artefacto.asegurar(destino, None, None) is False


class TestVolverABajarlo:
    """Con el índice ya en el volumen, cuándo se lo reemplaza y cuándo no.

    La huella del manifiesto resume los sha256 de los PDF de origen: no cambia cuando cambia
    el código de extracción, y nunca puede ser igual a un sha256 completo. Comparándola contra
    `INDICE_HUELLA`, el servicio se bajaba los cien megabytes en cada arranque.
    """

    def test_el_mismo_artefacto_no_se_vuelve_a_bajar(self, paquete, tmp_path, red):
        ruta, huella = paquete
        red(ruta)
        destino = tmp_path / "volumen" / "indice"
        assert artefacto.asegurar(destino, "https://ejemplo/indice.tar.zst", huella) is True
        assert artefacto.asegurar(destino, "https://ejemplo/indice.tar.zst", huella) is False

    def test_otra_version_reemplaza_lo_que_hay(self, indice, paquete, tmp_path, red):
        ruta, huella = paquete
        red(ruta)
        destino = tmp_path / "volumen" / "indice"
        artefacto.asegurar(destino, "https://ejemplo/indice.tar.zst", huella)

        # Un índice nuevo, con la misma huella de manifiesto y distinto contenido: es
        # exactamente lo que produce un arreglo en la extracción.
        (indice / "lexico.sqlite3").write_bytes(b"contenido corregido")
        otra_ruta, otra_huella = artefacto.empaquetar(indice, tmp_path / "nuevo.tar.zst")
        assert otra_huella != huella
        red(otra_ruta)
        assert artefacto.asegurar(destino, "https://ejemplo/indice.tar.zst", otra_huella) is True
        assert (destino / "lexico.sqlite3").read_bytes() == b"contenido corregido"

    def test_un_indice_sin_marca_se_reemplaza(self, indice, paquete, tmp_path, red):
        # El índice llegó de otro lado —una copia a mano, un artefacto viejo— y no dice cuál
        # es. Bajarlo de nuevo es la lectura segura.
        ruta, huella = paquete
        red(ruta)
        destino = tmp_path / "volumen" / "indice"
        artefacto.asegurar(destino, "https://ejemplo/indice.tar.zst", huella)
        (destino / artefacto.ARCHIVO_ARTEFACTO).unlink()
        assert artefacto.asegurar(destino, "https://ejemplo/indice.tar.zst", huella) is True

    def test_sin_huella_declarada_se_queda_con_lo_que_hay(self, paquete, tmp_path, red):
        ruta, huella = paquete
        red(ruta)
        destino = tmp_path / "volumen" / "indice"
        artefacto.asegurar(destino, "https://ejemplo/indice.tar.zst", huella)
        assert artefacto.asegurar(destino, "https://ejemplo/indice.tar.zst", None) is False
