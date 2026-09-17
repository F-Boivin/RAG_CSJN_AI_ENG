"""El catálogo de la Secretaría: qué documentos existen y cuáles van al índice.

El sitio se reemplaza con un `MockTransport`, así que la suite no sale a la red. Lo que se
comprueba es lo que haría volver a catalogar con `--forzar`: que el «Recurso Extraordinario»,
que el sitio no lista en ninguna categoría, siga en el catálogo, y que sea lo único indexable.
"""

import json

import httpx
import pytest

import app.nucleo.constantes as cfg
from app.rag.ingesta import fuentes_csjn as fuentes

HOME = "".join(f"<a onclick=\"buscarSuplementos('{n}')\">x</a>" for n in cfg.CATEGORIAS_SUPLEMENTOS)
NOTAS = [{"id": 2, "titulo": " Conformación de las mayorías "}, {"id": 224, "titulo": "Otra"}]


def sitio(listados: dict[int, list[dict]]) -> httpx.Client:
    """Un cliente contra un sitio falso: la home, las notas y los suplementos por categoría."""
    def responder(pedido: httpx.Request) -> httpx.Response:
        url = str(pedido.url)
        if url == cfg.URL_HOME_SJ:
            return httpx.Response(200, text=HOME)
        if url == cfg.URL_NOTAS:
            return httpx.Response(200, json=NOTAS)
        for numero in cfg.CATEGORIAS_SUPLEMENTOS:
            if url == cfg.URL_SUPLEMENTOS_CATEGORIA.format(categoria=numero):
                return httpx.Response(200, json=listados.get(numero, []))
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(responder))


@pytest.fixture(autouse=True)
def sin_pausas(monkeypatch):
    # La cortesía con el sitio real es una petición por segundo; contra el falso, ninguna.
    monkeypatch.setattr(cfg, "SEGUNDOS_ENTRE_DESCARGAS", 0)


def entradas(listados: dict[int, list[dict]]) -> list[dict]:
    with fuentes.ClienteSJ(sitio(listados)) as cliente:
        return cliente.catalogar()


def catalogar(listados: dict[int, list[dict]]) -> dict[str, dict]:
    return {e["origen"]: e for e in entradas(listados)}


class TestCatalogo:

    def test_el_recurso_extraordinario_entra_aunque_ningun_listado_lo_traiga(self):
        catalogo = catalogar({2: [{"id": 1, "titulo": "Derecho a la Salud"}]})
        recurso = catalogo["suplemento-3"]
        assert recurso["titulo"] == "Recurso Extraordinario"
        assert recurso["url"] == cfg.URL_SUPLEMENTO_PDF.format(id=3)

    def test_solo_el_recurso_extraordinario_va_al_indice(self):
        catalogo = catalogar({2: [{"id": 1, "titulo": "Derecho a la Salud"}]})
        assert {o for o, e in catalogo.items() if e["indexar"]} == {"suplemento-3"}
        assert {"nota-2", "nota-224", "suplemento-1"} <= set(catalogo)

    def test_si_el_sitio_empieza_a_listarlo_no_se_duplica(self):
        listado = entradas({14: [{"id": 3, "titulo": "Recurso Extraordinario"}]})
        recursos = [e for e in listado if e["origen"] == "suplemento-3"]
        assert len(recursos) == 1 and recursos[0]["indexar"] is True

    def test_la_entrada_trae_su_titulo_limpio_y_su_url(self):
        nota = catalogar({})["nota-2"]
        assert nota == {"origen": "nota-2", "tipo": "nota", "id": 2,
                        "titulo": "Conformación de las mayorías",
                        "categoria": "Notas de jurisprudencia",
                        "url": cfg.URL_NOTA_PDF.format(id=2), "indexar": False}


class TestCatalogoDelRepositorio:
    """El `catalogo.json` versionado dice lo mismo que el código."""

    def test_indexa_solo_el_recurso_extraordinario(self):
        catalogo = fuentes.leer_catalogo(cfg.RAIZ / "catalogo.json")
        assert [e["origen"] for e in catalogo if e["indexar"]] == ["suplemento-3"]

    def test_esta_escrito_como_lo_escribe_guardar_catalogo(self, tmp_path):
        # Reescribirlo no mueve una coma: el diff en git muestra solo lo que cambió.
        ruta = cfg.RAIZ / "catalogo.json"
        copia = tmp_path / "catalogo.json"
        fuentes.guardar_catalogo(json.loads(ruta.read_text(encoding="utf-8")), copia)
        assert copia.read_text(encoding="utf-8") == ruta.read_text(encoding="utf-8")
