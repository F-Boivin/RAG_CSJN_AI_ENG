"""El supervisor: elige quién sigue en cada vuelta y deja asentado por qué.

Decide con un resumen del estado —lo hecho y cómo salió— y devuelve un `Decision` tipado,
que es lo que lee la arista condicional del grafo. Los dos frenos se evalúan antes de
consultar al modelo, así una corrida ya decidida no gasta una llamada.

Verificado contra la documentación oficial (langchain-core 1.6.0):
- `with_structured_output` devuelve un Runnable que emite instancias del esquema pedido, y
  con `include_raw` entrega también la respuesta cruda y el error de parseo.
  https://reference.langchain.com/python/langchain-core/language_models/chat_models/BaseChatModel/with_structured_output
- `with_retry(retry_if_exception_type=..., wait_exponential_jitter=True, stop_after_attempt=n)`
  reintenta con backoff exponencial y jitter.
  https://reference.langchain.com/python/langchain-core/runnables/base/Runnable
"""

from typing import Optional

from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import Runnable
from openai import LengthFinishReasonError
from pydantic import BaseModel, Field

import app.nucleo.constantes as cfg
import app.nucleo.mensajes as msj
from app.nucleo.modelos import crear_chat
from app.grafo.estado import (
    FINALIZAR,
    INVESTIGADOR,
    REDACTOR,
    SUPERVISOR,
    VERIFICADOR,
    Destino,
    EstadoOrquestador,
    Situacion,
    leer_situacion,
)
from app.nucleo.errores import ErrorDeAgente


class Decision(BaseModel):
    """Lo que el supervisor devuelve en cada vuelta.

    Es Pydantic y no texto libre porque solo Pydantic valida: un destino fuera del conjunto
    tiene que fallar acá y no en el ruteo, donde el error sería un KeyError sin contexto.
    """

    siguiente: Destino = Field(
        description="Quién debe intervenir ahora, o FINALIZAR si la respuesta está lista."
    )
    # Sin `max_length`: este campo solo alimenta la línea de la traza, y un tope duro haría
    # que un motivo largo de más invalidara la decisión entera y con ella el trabajo.
    motivo: str = Field(description="Una frase: por qué ese paso y no otro.")


PROMPT_SUPERVISOR = f"""Sos el supervisor de un equipo de tres especialistas que responden consultas sobre jurisprudencia de la Corte Suprema de Justicia de la Nación.

- {INVESTIGADOR}: busca doctrina en el corpus de la CSJN y arma una sintesis con sus citas.
- {VERIFICADOR}: comprueba, contra los metadatos del corpus, que cada fallo citado exista.
- {REDACTOR}: escribe la respuesta final para el usuario, usando solo las citas verificadas.

Reglas de decision, en orden:
1. Si todavia no hay investigacion, delega en "{INVESTIGADOR}".
2. Si hay investigacion pero sus citas no fueron verificadas, delega en "{VERIFICADOR}".
3. Si el verificador RECHAZO y todavia quedan intentos, delega en "{INVESTIGADOR}" para que
   corrija.
4. Si el verificador APROBO y todavia no hay respuesta redactada, delega en "{REDACTOR}".
5. Si la respuesta redactada fue RECHAZADA y quedan intentos, delega en "{REDACTOR}" para que
   la reescriba. No mandes a verificar de nuevo: el veredicto sobre las citas ya esta dado.
6. Si la respuesta redactada esta limpia, respondes "{FINALIZAR}".
7. Si se agotaron los intentos, respondes "{FINALIZAR}" aunque no este todo aprobado.

No investigues, no verifiques y no redactes vos: tu unico trabajo es elegir quien sigue."""

MOTIVO_TOPE_ALCANZADO = (
    "se agotaron los intentos de correccion; se cierra con la ultima investigacion, "
    "dejando constancia de que sus citas no pudieron verificarse por completo"
)

MOTIVO_SOBRE_LO_VERIFICADO = (
    "se agotaron los intentos de correccion; se escribe la respuesta sobre las citas que si "
    "verificaron, que alcanzan el minimo para fundarla"
)

MOTIVO_TOPE_VUELTAS = (
    "se alcanzo el tope de vueltas del supervisor; se cierra para no quedar delegando en "
    "circulos"
)


class RespuestaIncompleta(ValueError):
    """El modelo cortó por límite de tokens: lo que devolvió puede estar a medias."""


class DecisionInvalida(ValueError):
    """La respuesta no se pudo convertir en una `Decision`."""


def _controlar_decision(respuesta: dict) -> Decision:
    """Comprueba la respuesta cruda antes de darla por buena.

    El orden importa: un objeto incompleto puede parsear igual y colarse como decisión
    válida, así que el corte por tokens se mira primero. Cada control levanta su propia
    excepción, y son las que la cadena reintenta.
    """
    crudo = respuesta.get("raw")
    metadatos = getattr(crudo, "response_metadata", {}) or {}
    # OpenAI lo llama `finish_reason`; Anthropic, `stop_reason`. Se leen los dos porque el
    # proveedor se elige por variable de entorno.
    if metadatos.get("finish_reason") == "length" or metadatos.get("stop_reason") == "max_tokens":
        raise RespuestaIncompleta(msj.ERROR_DECISION_INCOMPLETA)
    if respuesta.get("parsing_error") is not None:
        raise DecisionInvalida(str(respuesta["parsing_error"]))
    decision = respuesta.get("parsed")
    if decision is None:
        raise DecisionInvalida(msj.ERROR_DECISION_VACIA)
    return decision


def crear_cadena_supervisor(modelo: Optional[BaseChatModel] = None) -> Runnable:
    """El supervisor como cadena: decide, se controla y reintenta si hace falta.

    `include_raw` deja ver los metadatos de la respuesta además del objeto parseado, que es
    lo que permite distinguir un corte por tokens de un error de formato. Los reintentos
    cubren solo esos tres fallos: un destino inválido es un error de contenido y lo rechaza
    Pydantic, no la red.

    `modelo` se inyecta en las pruebas; en producción sale del factory.
    """
    modelo = modelo or crear_chat("supervisor")
    estructurado = modelo.with_structured_output(Decision, include_raw=True)
    return (estructurado | _controlar_decision).with_retry(
        retry_if_exception_type=(RespuestaIncompleta, DecisionInvalida, LengthFinishReasonError),
        wait_exponential_jitter=True,
        stop_after_attempt=cfg.INTENTOS_DECISION,
    )


def _estado_para_el_supervisor(state: EstadoOrquestador, situacion: Situacion) -> str:
    """Resume el estado en lo que el supervisor necesita para decidir.

    Recibe qué hay hecho y cómo salió, que es todo lo que hace falta para elegir el próximo
    paso. El historial completo queda afuera, para no arrastrar contexto que ya se resolvió.
    """
    lineas = [
        f"Consulta del usuario: {state['consulta']}",
        f"Investigaciones producidas: {situacion.n_investigaciones}  ·  "
        f"verificaciones hechas: {situacion.n_verificaciones}  ·  "
        f"redacciones escritas: {len(state.get('redacciones') or ())}",
        f"Intentos de corrección consumidos: {state.get('intentos', 0)} de {cfg.MAXIMO_INTENTOS}",
    ]
    if situacion.investigacion is None:
        lineas.append("ESTADO: todavía no hay ninguna investigación.")
        return "\n".join(lineas)

    if not situacion.vigente_verificada:
        lineas.append(
            f"ESTADO: la investigación vigente tiene {len(situacion.investigacion.citas)} "
            "citas y TODAVÍA NO FUE VERIFICADA."
        )
        return "\n".join(lineas)

    verificacion = situacion.verificacion
    estado = "APROBÓ" if verificacion.aprobado else "RECHAZÓ"
    lineas.append(f"ESTADO: la investigación vigente ya fue verificada y el verificador la {estado}.")
    if verificacion.observaciones:
        lineas.append("Observaciones del verificador: " + " | ".join(verificacion.observaciones))

    if not verificacion.aprobado:
        return "\n".join(lineas)

    if not situacion.redaccion_vigente:
        lineas.append("REDACCIÓN: todavía no hay respuesta final escrita sobre esta investigación.")
    elif situacion.reescritura_pendiente:
        lineas.append(
            "REDACCIÓN: la respuesta final fue RECHAZADA y HAY QUE REESCRIBIRLA. Motivos: "
            + " | ".join(situacion.redaccion.motivos)
        )
    else:
        lineas.append(
            "REDACCIÓN: la respuesta final está escrita, fundada en "
            f"{len(situacion.redaccion.citas_usadas)} citas verificadas y sin ninguna sin verificar."
        )
    return "\n".join(lineas)


async def supervisor_node(state: EstadoOrquestador) -> dict:
    """Decide quién sigue, y deja asentado por qué.

    Escribe tres cosas: `siguiente`, que es lo que lee la arista condicional; `completado`,
    el veredicto explícito de cierre; y un mensaje con el motivo, para que la decisión no
    sea una caja negra.

    **`intentos` se incrementa acá y en ningún otro lado**, y solo cuando esta vuelta manda
    a rehacer un artefacto que ya fue juzgado —la investigación que el verificador rechazó,
    o la redacción que invocó una cita sin verificar—. Sin esa precisión la guarda que lo
    compara no se puede leer.
    """
    situacion = leer_situacion(state)
    intentos = state.get("intentos", 0)
    vueltas = state.get("vueltas", 0) + 1

    # Los dos frenos se evalúan ANTES de consultar al modelo: si ya está decidido, pedirle
    # una decisión sería gastar una llamada en teatro.
    #
    # Freno 1, el general: un tope de vueltas del supervisor. Cubre todas las ramas,
    # incluida la de un trabajo ya aprobado sobre el que el supervisor siguiera delegando —
    # que `intentos` no toca, porque solo cuenta correcciones.
    if vueltas > cfg.MAXIMO_VUELTAS:
        return {
            "siguiente": "FINALIZAR", "completado": True, "vueltas": vueltas,
            "messages": [("assistant", f"[{SUPERVISOR}] {MOTIVO_TOPE_VUELTAS}")],
        }

    # Freno 2, el de los ciclos de corrección. Exige que lo pendiente sea una corrección
    # sobre un artefacto YA juzgado: si se agotaron los intentos pero la última corrección
    # nunca se revisó, cerrar acá la mostraría con el veredicto de la anterior.
    if intentos >= cfg.MAXIMO_INTENTOS and (
        situacion.rechazo_pendiente or situacion.reescritura_pendiente
    ):
        # Agotadas las correcciones, todavía se puede escribir sobre lo que sí verificó. El
        # verificador rechaza la tanda entera cuando una sola cita es inventada, y ese rechazo
        # es lo que hace corregir; una vez sin intentos, tirar dos citas ciertas por una
        # inventada pierde una respuesta buena sin proteger nada, porque el redactor solo
        # recibe las verificadas y el control del texto final las compara contra esa lista.
        if situacion.material_suficiente and not situacion.redaccion_vigente:
            return {
                "siguiente": REDACTOR, "completado": False, "vueltas": vueltas,
                "messages": [("assistant", f"[{SUPERVISOR}] {MOTIVO_SOBRE_LO_VERIFICADO}")],
            }
        return {
            "siguiente": "FINALIZAR", "completado": True, "vueltas": vueltas,
            "messages": [("assistant", f"[{SUPERVISOR}] {MOTIVO_TOPE_ALCANZADO}")],
        }

    try:
        decision: Decision = await crear_cadena_supervisor().ainvoke(
            [
                ("system", PROMPT_SUPERVISOR),
                ("user", _estado_para_el_supervisor(state, situacion)),
            ]
        )
    except ErrorDeAgente:
        raise
    except Exception as exc:
        # Agotados los reintentos, la excepción de la cadena sube y se traduce igual que
        # cualquier otro fallo del modelo.
        raise ErrorDeAgente(msj.ERROR_SUPERVISOR.format(detalle=f"{type(exc).__name__}: {exc}")) from exc

    reintenta = (
        (decision.siguiente == INVESTIGADOR and situacion.rechazo_pendiente)
        or (decision.siguiente == REDACTOR and situacion.reescritura_pendiente)
    )
    return {
        "siguiente": decision.siguiente,
        "completado": decision.siguiente == "FINALIZAR",
        "intentos": intentos + 1 if reintenta else intentos,
        "vueltas": vueltas,
        "messages": [("assistant", f"[{SUPERVISOR}] → {decision.siguiente}: {decision.motivo}")],
    }
