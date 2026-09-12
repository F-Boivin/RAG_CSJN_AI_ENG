"""Cuánto costó cada corrida, medido en vez de estimado.

Un `BaseCallbackHandler` enganchado en el `callbacks` del config de la corrida ve todas las
llamadas al modelo sin que ningún nodo lo sepa. Multiplicado por la tabla de `precios.py`, da
los dólares que van al registro y al freno de presupuesto.

La cuenta se hace por corrida y no por proceso: es lo que permite mirar qué consulta salió
cara, en vez de un total mensual que no se puede atribuir.
"""

from dataclasses import dataclass, field

from langchain_core.callbacks import BaseCallbackHandler

from app.nucleo import precios


@dataclass
class Consumo:
    """Lo que gastó una corrida."""

    llamadas: int = 0
    tokens_entrada: int = 0
    tokens_salida: int = 0
    usd: float = 0.0
    modelos: set[str] = field(default_factory=set)

    @property
    def modelo(self) -> str:
        return ", ".join(sorted(self.modelos))


class Contador(BaseCallbackHandler):
    """Suma el consumo de una corrida.

    El uso de tokens llega en `response.llm_output["token_usage"]` con OpenAI y en
    `generation.message.usage_metadata` con la interfaz común de LangChain. Se leen las dos:
    un proveedor que no complete la primera dejaría el contador en cero, y un contador en cero
    se lee igual que "no gastó nada".
    """

    def __init__(self):
        self.consumo = Consumo()

    def on_llm_end(self, response, **_kwargs) -> None:
        salida = getattr(response, "llm_output", None) or {}
        modelo = salida.get("model_name") or salida.get("model") or ""
        uso = salida.get("token_usage") or {}
        entrada = int(uso.get("prompt_tokens") or 0)
        respuesta = int(uso.get("completion_tokens") or 0)

        if not (entrada or respuesta):
            entrada, respuesta, modelo = _del_mensaje(response, modelo)

        self.consumo.llamadas += 1
        self.consumo.tokens_entrada += entrada
        self.consumo.tokens_salida += respuesta
        if modelo:
            self.consumo.modelos.add(modelo)
        self.consumo.usd += precios.costo(modelo, entrada, respuesta)


def _del_mensaje(response, modelo: str) -> tuple[int, int, str]:
    """El consumo leído de `usage_metadata`, que es la forma común a todos los proveedores."""
    for generaciones in getattr(response, "generations", []) or []:
        for generacion in generaciones:
            mensaje = getattr(generacion, "message", None)
            uso = getattr(mensaje, "usage_metadata", None) or {}
            if uso:
                metadata = getattr(mensaje, "response_metadata", None) or {}
                return (int(uso.get("input_tokens") or 0),
                        int(uso.get("output_tokens") or 0),
                        modelo or metadata.get("model_name") or "")
    return 0, 0, modelo
