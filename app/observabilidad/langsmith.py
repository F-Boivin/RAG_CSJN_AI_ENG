"""Observabilidad con LangSmith: cada paso del orquestador queda como un run.

LangSmith se engancha al sistema de callbacks de LangChain, asi que con las variables de
entorno puestas quedan trazados los nodos de LangGraph, los agentes ReAct, las herramientas y
cada llamada al modelo, anidados entre si. Los accesos a Chroma pasan por fuera de esos
callbacks y los emite `trazas.py`.

Documentacion: https://docs.langchain.com/langsmith/trace-with-langchain
"""

import os
import sys
from typing import Optional

from langchain_core.tracers.langchain import wait_for_all_tracers
from langsmith import Client
from langsmith.utils import LangSmithError

import app.nucleo.constantes as cfg
import app.nucleo.mensajes as msj
from app.nucleo.config import obtener_ajustes


def configurada() -> bool:
    """Si esta la clave que LangSmith necesita para recibir trazas."""
    return bool(os.getenv("LANGSMITH_API_KEY"))


def alcanzable() -> bool:
    """Si LangSmith responde con las credenciales que hay."""
    try:
        Client().info
        return True
    except (LangSmithError, OSError):
        return False


def instrumentar(proyecto: Optional[str] = None) -> bool:
    """Enciende el trazado y devuelve si quedo activo.

    Nunca impide arrancar: sin clave o con LangSmith caido avisa por stderr y sigue. Un
    proceso que no levanta porque falta su telemetria es peor que uno sin telemetria.
    """
    if not configurada():
        print(msj.AVISO_LANGSMITH_APAGADO, file=sys.stderr)
        os.environ["LANGSMITH_TRACING"] = "false"
        return False

    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_PROJECT"] = proyecto or obtener_ajustes().proyecto_langsmith
    if not alcanzable():
        print(msj.AVISO_LANGSMITH_INALCANZABLE, file=sys.stderr)
    return True


def esperar_trazas() -> None:
    """Vacia lo pendiente antes de que el proceso termine.

    LangSmith postea en un hilo de fondo para no meterle latencia a la aplicacion, asi que un
    proceso que sale enseguida se lleva puestas las ultimas trazas. Son dos vaciados porque hay
    dos caminos: `wait_for_all_tracers` cubre lo que emite LangChain por callbacks, y
    `Client().flush()` lo que emite `trazas.py` con el SDK directo.

    Documentacion:
    https://docs.langchain.com/langsmith/trace-with-langchain#ensure-all-traces-are-submitted-before-exiting
    https://docs.langchain.com/langsmith/annotate-code#ensure-all-traces-are-submitted-before-exiting
    """
    if os.getenv("LANGSMITH_TRACING") != "true":
        return
    try:
        wait_for_all_tracers()
        Client().flush()
    except (LangSmithError, OSError) as exc:
        print(msj.AVISO_TRAZAS_SIN_VACIAR.format(detalle=exc), file=sys.stderr)
