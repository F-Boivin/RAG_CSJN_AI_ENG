"""Agente redactor: escribe la respuesta final usando solo lo que el verificador aprobó.

Es la fase de síntesis del equipo. Su única herramienta, `link_oficial`, reparte los
links de esas citas —acotada por cierre a las de esta corrida—, así que amplía lo que puede
mostrar sin darle una fuente de hechos propia.

Lo que escribe se compara contra la lista aprobada antes de aceptarlo: es el último que toca el
texto, y sería el último lugar donde una cita inventada podría colarse.
"""

from langgraph.prebuilt import create_react_agent
from pydantic import ValidationError

import app.nucleo.constantes as cfg
import app.nucleo.mensajes as msj
import app.rag.herramientas as herramientas
from app.nucleo.modelos import crear_chat
from app.nucleo.errores import ErrorDeAgente
from app.grafo.estado import (
    EstadoOrquestador,
    Redaccion,
    huella_material,
    ultima_investigacion,
    ultima_redaccion,
    ultima_verificacion,
)

PROMPT = """Sos el redactor del equipo. Tu responsabilidad es transformar el material que \
produjeron los otros agentes en una respuesta clara y util para quien pregunto.

No investigues ni agregues doctrina: no tenes fuente propia. Todo lo que escribas tiene que \
salir del material que te pasan.

Tenes una sola herramienta, `link_oficial`: le pasas un fallo de la lista de citas \
verificadas y te devuelve su link oficial en el buscador de la Corte. Llamala una vez por \
cada fallo que vayas a citar, ANTES de escribir la respuesta, y pega el link junto a la cita. \
Si te contesta que una cita no esta aprobada, no la uses.

Reglas que no se negocian:
- Los unicos fallos que podes mencionar son los de la lista de citas verificadas. Si un \
numero de fallo no esta en esa lista, no existe: no lo escribas, ni siquiera de memoria.
- Escribi cada cita completa y en la forma exacta "Fallos: tomo:pagina", seguida de su link \
entre parentesis cuando la herramienta te de uno. Nunca abrevies una pagina apoyandote en el \
tomo de la cita anterior, y nunca cites un caso solo por su nombre.
- Fundamenta la respuesta: invoca al menos {minimas} de las citas verificadas.
- No inventes numeros de fallo para "redondear" una afirmacion. Si algo no tiene respaldo en \
el material, decilo sin cita o no lo digas.
- Escribi en prosa, para alguien que sabe derecho pero no leyo el cuadernillo. Sin titulos, \
sin vinetas y sin repetir la pregunta.

Tu mensaje final tiene que ser la respuesta y nada mas: ni comentarios sobre tu proceso, ni \
anuncios de lo que vas a hacer."""

PEDIDO = """Consulta original:
{consulta}

Sintesis del investigador:
{sintesis}

Citas verificadas (las unicas que podes invocar):
{citas}
{correccion}"""

CORRECCION = """
Tu redaccion anterior fue rechazada por lo siguiente:
- {motivos}
Reescribila corrigiendo exactamente eso."""


def crear_redactor(verificadas):
    """Arma el agente ReAct del redactor con su herramienta acotada.

    La tool se construye por corrida y cerrada sobre las citas aprobadas de esa corrida: es lo
    que impide que el redactor pida el link de un fallo real pero ajeno a esta consulta.
    """
    modelo = crear_chat("redactor")
    return create_react_agent(
        model=modelo,
        tools=[herramientas.crear_link_oficial(verificadas)],
        prompt=PROMPT.format(minimas=cfg.CITAS_MINIMAS),
    )


def auditar_citas(texto: str, verificadas) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Separa las citas del texto en las que el verificador aprobó y las que no.

    Compara en la forma normalizada "tomo:pagina", que es lo único que identifica una cita: el
    redactor escribe "Fallos: 338:623" o "los fallos 338:623 y 349:148" según le quede la
    oración, y comparar las cadenas crudas daría por distinto lo que es igual.

    El barrido lo hace `herramientas.citas_del_texto`, que **se ancla en la palabra "fallos"**
    en vez de buscar cualquier `numero:numero` suelto. Esa decisión se tomó midiendo las dos
    alternativas: sin ancla, "la audiencia de las 14:30" y "el voto 3:2" se marcaban como citas
    inventadas y disparaban reescrituras que nunca convergen; con ancla, además se caza la
    página abreviada ("Fallos: 315:356 y 3334"), que es como cita el propio cuadernillo y por
    lo tanto la forma que el modelo tiende a imitar. Los links oficiales tampoco disparan el
    ancla: ninguna de las 557 URLs del corpus contiene la palabra (medido).
    """
    aprobadas = {herramientas.normalizar_cita(c) for c in verificadas} - {""}
    del_texto = herramientas.citas_del_texto(texto)
    return tuple(sorted(del_texto & aprobadas)), tuple(sorted(del_texto - aprobadas))


def juzgar_texto(texto: str, verificadas, material: str, llamadas: int) -> Redaccion:
    """Convierte un texto en un artefacto `Redaccion` con su veredicto ya computado.

    Es la guarda entera en una sola función, así el nodo y los tests ejercitan exactamente la
    misma: un veredicto armado aparte estaría probando un control que no es el que corre.
    """
    usadas, intrusas = auditar_citas(texto, verificadas)
    motivos = []
    if intrusas:
        motivos.append(msj.MOTIVO_CITAS_INTRUSAS.format(citas=", ".join(intrusas)))
    if len(usadas) < cfg.CITAS_MINIMAS:
        # Estar fundado es citar. Sin este control, "limpia" solo querría decir "no mintió",
        # y la traza diría "todas sus citas verificadas" sobre un texto que no tiene ninguna.
        motivos.append(
            msj.MOTIVO_POCAS_CITAS.format(usadas=len(usadas), minimas=cfg.CITAS_MINIMAS)
        )
    return Redaccion(
        texto=texto,
        citas_usadas=usadas,
        citas_intrusas=intrusas,
        motivos=tuple(motivos),
        limpia=not motivos,
        sobre_material=material,
        llamadas=llamadas,
    )


def resumen_redaccion(redaccion: Redaccion) -> str:
    """La línea que el redactor deja en la traza.

    Sale del artefacto y de ningún otro lado. Si el mensaje se armara por su cuenta, la
    traza podría decir "todas verificadas" mientras el artefacto guarda lo contrario.
    """
    cabeza = f"{redaccion.llamadas} llamadas a link_oficial"
    if redaccion.limpia:
        return (
            f"{cabeza} · {len(redaccion.texto.split())} palabras sobre "
            f"{len(redaccion.citas_usadas)} citas verificadas, con sus links."
        )
    return f"{cabeza} · RECHAZADA: {' | '.join(redaccion.motivos)}"


def _texto_de(mensaje) -> str:
    """El contenido de un mensaje como string.

    `content` es str con ChatOpenAI, pero la firma admite una lista de bloques, y un bloque de
    un tipo inesperado reventaría con un AttributeError que no menciona al redactor.
    """
    crudo = getattr(mensaje, "content", "")
    if isinstance(crudo, list):
        crudo = "".join(
            b if isinstance(b, str) else (b.get("text", "") if isinstance(b, dict) else "")
            for b in crudo
        )
    return (crudo or "").strip()


async def redactor_node(state: EstadoOrquestador) -> dict:
    """Escribe la respuesta final y deja constancia de si se mantuvo dentro de lo verificado."""
    investigacion = ultima_investigacion(state)
    verificacion = ultima_verificacion(state)
    investigaciones = state.get("investigaciones") or ()
    verificaciones = state.get("verificaciones") or ()
    # Las dos tienen que ser de la misma tanda. Con una investigación nueva sin verificar, el
    # nodo mezclaría su síntesis con las citas aprobadas de la anterior y el control validaría
    # contra una lista que no le corresponde.
    if investigacion is None or verificacion is None or len(verificaciones) < len(investigaciones):
        raise ErrorDeAgente(msj.ERROR_REDACTOR_SIN_MATERIAL)

    material = huella_material(investigacion, verificacion)
    aprobadas = verificacion.verificadas
    citas = [
        f"- {c.fallo}: {c.afirmacion}"
        for c in investigacion.citas
        if c.fallo in aprobadas
    ] or [msj.MENSAJE_SIN_CITAS_APROBADAS]

    # El motivo del rechazo anterior entra en el prompt, igual que en el investigador: sin
    # decirle qué salió mal, "reescribí" es una instrucción vacía y el modelo repite el texto.
    # Solo se usa si la redacción previa se escribió sobre ESTE material: si el material
    # cambió, ese rechazo hablaba de otra cosa y reprocharlo sería mandarlo a corregir algo
    # que ya no está.
    previa = ultima_redaccion(state)
    correccion = ""
    if previa is not None and not previa.limpia and previa.sobre_material == material:
        correccion = CORRECCION.format(motivos="\n- ".join(previa.motivos))

    pedido = PEDIDO.format(
        consulta=state["consulta"],
        sintesis=investigacion.sintesis,
        citas="\n".join(citas),
        correccion=correccion,
    )

    try:
        salida = await crear_redactor(aprobadas).ainvoke(
            {"messages": [("user", pedido)]},
            {"recursion_limit": cfg.LIMITE_RECURSION_REDACTOR},
        )
    except Exception as exc:  # el SDK y LangGraph traen sus propias jerarquías
        raise ErrorDeAgente(msj.ERROR_REDACTOR.format(detalle=f"{type(exc).__name__}: {exc}")) from exc

    mensajes = salida.get("messages") or []
    texto = _texto_de(mensajes[-1]) if mensajes else ""
    if not texto:
        # Pasa si el agente se queda sin supersteps con una llamada a herramienta pendiente:
        # el último mensaje es la observación de la tool, no una respuesta.
        raise ErrorDeAgente(msj.ERROR_REDACTOR.format(detalle="no dejó una respuesta escrita"))

    try:
        redaccion = juzgar_texto(
            texto, aprobadas, material, herramientas.contar_llamadas(mensajes)
        )
    except ValidationError as exc:
        raise ErrorDeAgente(
            msj.ERROR_REDACTOR.format(detalle=f"produjo un artefacto inválido: {exc.error_count()} campo/s")
        ) from exc
    return {
        "redacciones": (redaccion,),
        "messages": [("assistant", f"[redactor] {resumen_redaccion(redaccion)}")],
    }
