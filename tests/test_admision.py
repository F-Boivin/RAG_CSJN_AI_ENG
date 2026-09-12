"""El límite de tema: qué consultas entran al grafo y cuáles se cortan antes.

Las dos capas se reemplazan por dobles —una devuelve similitud, la otra un veredicto— así que
la suite prueba la decisión y no el modelo. Lo que importa acá es cómo se combinan y qué pasa
cuando una de las dos falla.
"""

import pytest

import app.nucleo.mensajes as msj
from app.nucleo.config import reiniciar_ajustes
from app.servicio import admision


class ChromaFalso:
    """Devuelve la distancia que el test pida. Chroma trabaja con distancia coseno."""

    def __init__(self, distancia=0.1, resultados=1):
        self.distancia = distancia
        self.resultados = resultados

    async def asimilarity_search_with_score(self, consulta, k=1):
        return [(object(), self.distancia)] * self.resultados


@pytest.fixture(autouse=True)
def umbral(monkeypatch):
    monkeypatch.setenv("UMBRAL_SIMILITUD", "0.30")
    reiniciar_ajustes()
    yield
    reiniciar_ajustes()


def clasificador(admitida: bool, motivo: str = "un motivo"):
    async def falso(_consulta, _contador=None):
        return admision.Admision(admitida=admitida, motivo=motivo)
    return falso


class TestPisoDeSimilitud:
    """La capa barata: un embedding y el mejor resultado del corpus."""

    async def test_una_consulta_lejana_del_corpus_se_rechaza(self, monkeypatch):
        monkeypatch.setattr(admision, "_clasificar", clasificador(True))
        # distancia 0.9 → similitud 0.1, por debajo del umbral de 0.30.
        veredicto = await admision.admitir("receta de milanesas", ChromaFalso(distancia=0.9))
        assert veredicto.admitida is False
        assert veredicto.motivo == msj.MENSAJE_FUERA_DE_ALCANCE

    async def test_una_consulta_cercana_pasa_la_primera_capa(self, monkeypatch):
        monkeypatch.setattr(admision, "_clasificar", clasificador(True))
        veredicto = await admision.admitir("exceso ritual", ChromaFalso(distancia=0.1))
        assert veredicto.admitida is True
        assert veredicto.similitud == pytest.approx(0.9)

    async def test_un_corpus_vacio_rechaza(self, monkeypatch):
        # Sin material no hay consulta dentro de alcance.
        monkeypatch.setattr(admision, "_clasificar", clasificador(True))
        veredicto = await admision.admitir("lo que sea", ChromaFalso(resultados=0))
        assert veredicto.admitida is False


class TestClasificador:
    """La capa del modelo: el motivo que lee una persona."""

    async def test_un_rechazo_del_clasificador_corta_aunque_haya_similitud(self, monkeypatch):
        monkeypatch.setattr(admision, "_clasificar",
                            clasificador(False, "pedís asesoramiento sobre tu caso"))
        veredicto = await admision.admitir("me despidieron, que hago", ChromaFalso(0.1))
        assert veredicto.admitida is False
        assert "asesoramiento" in veredicto.motivo

    async def test_se_admite_solo_si_pasan_las_dos(self, monkeypatch):
        monkeypatch.setattr(admision, "_clasificar", clasificador(True))
        assert (await admision.admitir("exceso ritual", ChromaFalso(0.1))).admitida is True


class TestFallos:
    """Un error de infraestructura deja pasar la consulta.

    Negarle una respuesta a alguien porque el clasificador no respondió es peor que gastar una
    corrida: el corpus decide igual más adelante.
    """

    async def test_si_el_clasificador_falla_la_consulta_pasa(self, monkeypatch):
        async def explota(_consulta, _contador=None):
            raise RuntimeError("el proveedor no responde")

        monkeypatch.setattr(admision, "_clasificar", explota)
        veredicto = await admision.admitir("exceso ritual", ChromaFalso(0.1))
        assert veredicto.admitida is True
        assert veredicto.del_clasificador is False

    async def test_si_el_embedding_falla_la_consulta_pasa(self, monkeypatch):
        class ChromaRoto:
            async def asimilarity_search_with_score(self, *_a, **_k):
                raise RuntimeError("la base no responde")

        monkeypatch.setattr(admision, "_clasificar", clasificador(True))
        assert (await admision.admitir("exceso ritual", ChromaRoto())).admitida is True

    async def test_si_fallan_las_dos_la_consulta_pasa(self, monkeypatch):
        class ChromaRoto:
            async def asimilarity_search_with_score(self, *_a, **_k):
                raise RuntimeError("la base no responde")

        async def explota(_consulta, _contador=None):
            raise RuntimeError("el proveedor no responde")

        monkeypatch.setattr(admision, "_clasificar", explota)
        assert (await admision.admitir("exceso ritual", ChromaRoto())).admitida is True
