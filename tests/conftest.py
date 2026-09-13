"""Fixtures compartidas de la suite.

Todo lo que la suite necesita se construye en memoria: los dobles de `tests/dobles.py`, un
corpus sintético y SQLite en `:memory:`. `sin_red` corta cualquier conexión saliente que no
sea loopback, así que la suite corre igual en una máquina sin claves y sin internet.
"""

import socket

import pytest

from app.grafo.estado import (
    Investigacion,
    Redaccion,
    Verificacion,
    huella_material,
)
from app.rag.citas import normalizar_cita
from tests.dobles import (FALLOS, SUBSECCION, citas, estado_inicial, fragmentos_por_cita,
                          leido, textos_leidos)

_LOCALES = {"127.0.0.1", "::1", "localhost", "0.0.0.0"}
_conectar_real = socket.socket.connect


@pytest.fixture(autouse=True)
def sin_red(monkeypatch):
    """Convierte cualquier salida a la red en un fallo del test.

    La suite ya estaba escrita para no depender de la red; esto lo vuelve comprobable. Un
    test que empiece a llamar de verdad a un proveedor falla acá, y no seis meses después
    con una factura.
    """
    def bloquear(self, direccion):
        if isinstance(direccion, tuple) and str(direccion[0]) not in _LOCALES:
            raise RuntimeError(f"la suite no habla con la red: intento a {direccion}")
        return _conectar_real(self, direccion)

    monkeypatch.setattr(socket.socket, "connect", bloquear)


@pytest.fixture
def estado_vacio() -> dict:
    """El estado con el que arranca cualquier trabajo."""
    return estado_inicial()


@pytest.fixture
def investigacion() -> Investigacion:
    """Una investigación con tres citas del padrón de prueba."""
    return Investigacion(
        sintesis="Sintesis de prueba sobre el exceso ritual manifiesto.",
        citas=citas(3),
        subsecciones=(SUBSECCION,),
    )


@pytest.fixture
def verificacion(investigacion: Investigacion) -> Verificacion:
    """El veredicto que aprueba las tres citas de esa investigación, con su pasaje propio."""
    verificadas = tuple(c.fallo for c in investigacion.citas)
    return Verificacion(
        verificadas=verificadas,
        inexistentes=(),
        pasajes_propios=tuple((normalizar_cita(f), c.respaldo)
                              for f, c in zip(verificadas, investigacion.citas)),
        aprobado=True,
        observaciones=(),
    )


@pytest.fixture
def redaccion(investigacion: Investigacion, verificacion: Verificacion) -> Redaccion:
    """Una redacción limpia escrita sobre ese material."""
    usadas = verificacion.verificadas
    return Redaccion(
        texto=f"Respuesta apoyada en {', '.join(usadas)}.",
        citas_usadas=usadas,
        citas_intrusas=(),
        motivos=(),
        limpia=True,
        llamadas=1,
        sobre_material=huella_material(investigacion, verificacion),
    )


@pytest.fixture
def estado_terminado(estado_vacio, investigacion, verificacion, redaccion) -> dict:
    """El estado de un trabajo con las tres etapas hechas y nada pendiente.

    Lleva el registro de lo leído, porque un trabajo terminado lo tiene: de ahí salen la
    procedencia de cada cita y el texto contra el que se comprobó su pasaje.
    """
    return {
        **estado_vacio,
        "investigaciones": (investigacion,),
        "verificaciones": (verificacion,),
        "redacciones": (redaccion,),
        "recuperado": leido(3),
        "textos_leidos": textos_leidos(),
        "fragmentos_por_cita": fragmentos_por_cita(3),
    }


@pytest.fixture
def fallos() -> tuple:
    """Los fallos del padrón de prueba."""
    return FALLOS
