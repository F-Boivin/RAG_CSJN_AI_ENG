"""Agente de investigación: arma una síntesis con las citas de lo que leyó.

Una sola herramienta, `buscar_doctrina`, que trae los fragmentos con los fallos que cada uno
cita. **Ahí se terminan los fallos disponibles**: no hay ninguna otra herramienta que reparta
citas, y la que había repartía las de una subsección entera —hasta 264 para un top-k que traía
una— de donde salían las citas ciertas y ajenas al tema.

La garantía no la sostiene el prompt. La herramienta anota qué fragmento trajo cada fallo y qué
decía ese fragmento, y el verificador rechaza lo que no esté ahí: pedirle al modelo que cite
bien es una instrucción, y comprobar de dónde salió cada cita es un hecho. Por eso además cada
cita viene con el pasaje que la sostiene, copiado del fragmento —lo que se comprueba es el
texto, no la intención—.

Devuelve un `Investigacion` de Pydantic, con la síntesis y las citas en campos separados para
que el verificador compruebe una por una.
"""

from langgraph.prebuilt import create_react_agent

import app.nucleo.constantes as cfg
import app.nucleo.mensajes as msj
import app.rag.herramientas as herramientas
from app.nucleo.modelos import crear_chat
from app.nucleo.errores import ErrorDeAgente
from app.grafo.estado import (
    EstadoOrquestador,
    Investigacion,
    ultima_verificacion,
)

PROMPT = """Sos el especialista en investigación jurídica del equipo. Tu única fuente de \
verdad es el corpus de la Secretaría de Jurisprudencia de la CSJN indexado en tus \
herramientas: el cuadernillo de doctrina sobre sentencias arbitrarias, con el concepto de \
arbitrariedad, sus causales, la improcedencia del recurso y su trámite.

Cómo trabajás:
1. Buscá la doctrina con `buscar_doctrina`. Si la consulta abarca más de un tema, hacé una \
búsqueda por tema.
2. Leé los fragmentos. Cada uno viene con los fallos que cita, y al final está la lista \
completa de los que podés usar.
3. Escribí una síntesis que responda la consulta, y separá cada afirmación con su fallo. \
**Para cada cita, copiá en `respaldo` una oración del mismo fragmento donde aparece ese \
fallo**, tal como está escrita. El fallo y su oración van juntos: si el fallo figura debajo \
de un fragmento, la oración sale de ese fragmento y de ningún otro. Copiala, no la \
parafrasees: lo que respalda es el texto, y una reescritura no se puede comprobar contra nada.

Reglas que no se negocian:
- **Solo podés citar fallos que aparezcan en los resultados de tus búsquedas.** No hay otra \
fuente: ni tu memoria, ni un número que deduzcas, ni un fallo que sepas que existe.
- Cada afirmación tiene que salir del fragmento que trae ese fallo. Un fallo que apareció en \
una búsqueda sobre otro tema no sirve para sostener esta afirmación.
- Si no podés copiar una oración que sostenga la afirmación, esa afirmación no está en el \
corpus: sacala.
- Si lo que encontraste no responde la consulta, buscá de nuevo con otros términos. Si \
después de buscar no hay material, decilo en la síntesis y no cites. **Decir que el corpus no \
trata el tema es una respuesta correcta**; forzar citas que no lo sostienen, no.
- Si te devuelven una investigación rechazada, corregí exactamente lo que se te señala: \
sacá las citas señaladas y buscá respaldo real para esas afirmaciones, o quitá la \
afirmación que no podés sostener."""


def construir_investigador(lectura: herramientas.Lectura):
    """Arma el agente ReAct de investigación, con su herramienta atada a `lectura`.

    La lectura se llena mientras el agente busca, y queda en el estado para que el verificador
    sepa de qué fragmento salió cada fallo y qué decía ese fragmento. Es por corrida y no
    global: dos consultas simultáneas en el mismo proceso mezclarían lo que leyó cada una, y
    los chequeos de pertinencia y de respaldo pasarían a aprobar sobre textos que este
    investigador nunca vio.

    `response_format` obliga a que la salida final sea un `Investigacion` válido: si el
    modelo devuelve algo que no cumple el esquema, falla acá y no tres nodos más adelante,
    cuando el dato ya se propagó.
    """
    modelo = crear_chat("investigador")
    return create_react_agent(
        model=modelo,
        tools=[herramientas.crear_buscar_doctrina(lectura)],
        prompt=PROMPT,
        response_format=Investigacion,
    )


async def investigador_node(state: EstadoOrquestador) -> dict:
    """Investiga y agrega su artefacto al estado.

    Recibe el contexto mínimo: la consulta y, si lo hubo, el motivo del rechazo anterior.
    Saber cómo el supervisor tomó sus decisiones le agregaría contexto que no usa.
    """
    pedido = f"Consulta a investigar: {state['consulta']}"
    rechazo = ultima_verificacion(state)
    if rechazo is not None and not rechazo.aprobado:
        pedido += (
            "\n\nTu investigación anterior fue rechazada por el verificador de citas.\n"
            f"Fallos que NO existen en el corpus: {', '.join(rechazo.inexistentes) or '(ninguno)'}\n"
            "Fallos que existen pero no salieron de ningún fragmento que leíste: "
            f"{', '.join(rechazo.impertinentes) or '(ninguno)'}\n"
            "Observaciones:\n- " + "\n- ".join(rechazo.observaciones)
        )

    # La lectura se crea acá y viaja al estado: lo que este investigador leyó es lo único que
    # sus citas pueden invocar y lo único contra lo que se comprueban sus pasajes.
    lectura = herramientas.Lectura()
    try:
        salida = await construir_investigador(lectura).ainvoke(
            {"messages": [("user", pedido)]},
            {"recursion_limit": cfg.LIMITE_RECURSION_AGENTE},
        )
    except Exception as exc:  # el SDK y LangGraph traen sus propias jerarquías
        raise ErrorDeAgente(msj.ERROR_INVESTIGADOR.format(detalle=f"{type(exc).__name__}: {exc}")) from exc

    investigacion = salida.get("structured_response")
    if investigacion is None:
        raise ErrorDeAgente(msj.ERROR_INVESTIGADOR.format(detalle="no devolvió un artefacto estructurado"))

    # El conteo se arma acá y no viaja en el artefacto: `Investigacion` lo produce el modelo
    # vía `response_format`, así que un campo más sería un campo que el modelo tendría que
    # llenar. Nadie más rearma este mensaje, así que no hay dos lugares que puedan discrepar.
    llamadas = herramientas.contar_llamadas(salida.get("messages") or [])
    return {
        "investigaciones": (investigacion,),
        "recuperado": lectura.citas,
        "textos_leidos": lectura.textos,
        "fragmentos_por_cita": lectura.fragmentos_por_cita,
        "messages": [
            (
                "assistant",
                f"[investigador] {llamadas} llamadas a herramientas · "
                f"{len(investigacion.citas)} citas sobre {len(lectura.citas)} fallos en "
                f"{len(lectura.textos)} fragmentos leídos.",
            )
        ],
    }
