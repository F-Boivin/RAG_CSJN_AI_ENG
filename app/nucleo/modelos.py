"""El factory que construye el modelo de cada rol según el proveedor configurado.

Una clase concreta por proveedor detrás de una interfaz común, elegidas por un diccionario:
agregar uno nuevo es sumar una clase y una entrada. Cada cliente comprueba su clave antes de
instanciar el modelo, porque los SDK validan credenciales en el constructor.

Los clientes devuelven `BaseChatModel` de LangChain: los consumidores del sistema —el ciclo
ReAct de los agentes y la salida estructurada del supervisor— trabajan sobre esa interfaz.

Verificado contra la documentación oficial (langchain-core 1.6.0):
- `BaseChatModel` es la interfaz que `create_react_agent` y `with_structured_output` esperan.
  https://reference.langchain.com/python/langchain-core/language_models/chat_models/BaseChatModel
"""

from abc import ABC, abstractmethod
from typing import Literal, Optional

from langchain_core.language_models import BaseChatModel
from langchain_openai import OpenAIEmbeddings
from pydantic import SecretStr

import app.nucleo.constantes as cfg
from app.nucleo.config import Ajustes, Proveedor, obtener_ajustes

Rol = Literal["supervisor", "investigador", "redactor", "admision"]

MODELOS_POR_DEFECTO: dict[Proveedor, str] = {
    Proveedor.OPENAI: "gpt-4o-mini",
    Proveedor.ANTHROPIC: "claude-haiku-4-5",
}


class ClienteDeChat(ABC):
    """Construye el modelo de chat de un proveedor, con la clave ya comprobada."""

    def __init__(self, clave: SecretStr) -> None:
        self.clave = clave

    @abstractmethod
    def construir(self, modelo: str, temperatura: float) -> BaseChatModel:
        """El modelo listo para invocar, con su tope de espera."""


class ClienteOpenAI(ClienteDeChat):
    """Modelos de OpenAI."""

    def construir(self, modelo: str, temperatura: float) -> BaseChatModel:
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model=modelo, temperature=temperatura, api_key=self.clave,
                          timeout=cfg.SEGUNDOS_TIMEOUT_MODELO)


class ClienteAnthropic(ClienteDeChat):
    """Modelos de Anthropic."""

    def construir(self, modelo: str, temperatura: float) -> BaseChatModel:
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(model=modelo, temperature=temperatura, api_key=self.clave,
                             timeout=cfg.SEGUNDOS_TIMEOUT_MODELO)


CLIENTES: dict[Proveedor, type[ClienteDeChat]] = {
    Proveedor.OPENAI: ClienteOpenAI,
    Proveedor.ANTHROPIC: ClienteAnthropic,
}

# Los dos diccionarios tienen que cubrir el enum entero: agregar un proveedor y olvidarse de
# alguno daría un KeyError pelado en la primera llamada al modelo, lejos de la causa. Mismo
# resguardo que estado.py usa para los destinos del grafo.
assert set(CLIENTES) == set(Proveedor), "CLIENTES no cubre todos los proveedores"
assert set(MODELOS_POR_DEFECTO) == set(Proveedor), "MODELOS_POR_DEFECTO no cubre todos los proveedores"


def modelo_del_rol(rol: Rol, ajustes: Ajustes) -> str:
    """El modelo configurado para ese rol, o el que el proveedor trae por defecto."""
    configurado = getattr(ajustes, f"modelo_{rol}", None)
    return configurado or MODELOS_POR_DEFECTO[ajustes.llm_provider]


def crear_chat(rol: Rol, ajustes: Optional[Ajustes] = None) -> BaseChatModel:
    """El modelo de chat de ese rol, según el proveedor activo.

    La clave se comprueba antes de construir el cliente: los SDK validan credenciales al
    instanciarse, así que un control posterior nunca llegaría a ejecutarse.
    """
    ajustes = ajustes or obtener_ajustes()
    proveedor = ajustes.llm_provider
    clave = ajustes.clave_de(proveedor)
    cliente = CLIENTES[proveedor](clave)
    return cliente.construir(modelo_del_rol(rol, ajustes), ajustes.temperatura)


def crear_embeddings(ajustes: Optional[Ajustes] = None) -> OpenAIEmbeddings:
    """El modelo de embeddings del índice, siempre de OpenAI.

    Anthropic no ofrece una API de embeddings, así que el índice se construye y se consulta
    con OpenAI aunque el chat corra con otro proveedor. El README lo declara como alcance.

    `dimensiones_embeddings` trunca el vector: la familia `text-embedding-3` lo admite con
    degradación gradual, y es lo que permite achicar el índice a costa de recall.
    """
    ajustes = ajustes or obtener_ajustes()
    clave = ajustes.clave_de(Proveedor.OPENAI)
    # `dimensions` solo viaja cuando está configurado: pasarlo en None a un modelo que no lo
    # admite es un error del proveedor, y el default del modelo es lo correcto sin él.
    extra = ({"dimensions": ajustes.dimensiones_embeddings}
             if ajustes.dimensiones_embeddings else {})
    return OpenAIEmbeddings(model=ajustes.modelo_embeddings, api_key=clave, **extra)
