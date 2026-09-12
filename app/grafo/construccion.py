"""El cableado del grafo: quién puede seguir a quién, y con qué condición.

El supervisor decide y los tres especialistas devuelven el control. Una arista condicional
traduce esa decisión en un destino: el especialista elegido, o el final.

Verificado contra la documentación oficial (langgraph 1.2.9):
- Sin el mapeo explícito de `add_conditional_edges`, un destino inexistente se loguea y el
  grafo termina como si nada, sin levantar excepción.
  https://reference.langchain.com/python/langgraph/graph/state/StateGraph/add_conditional_edges
- `recursion_limit` va en el config de la invocación y no en `compile()`; cuenta supersteps.
  https://docs.langchain.com/oss/python/langgraph/graph-api#recursion-limit
"""

from typing import Literal

from langgraph.graph import END, START, StateGraph

import app.nucleo.mensajes as msj
from app.agentes.investigador import investigador_node
from app.agentes.redactor import redactor_node
from app.agentes.verificador import verificador_node
from app.grafo.estado import (
    DESTINOS,
    INVESTIGADOR,
    NODOS,
    REDACTOR,
    SUPERVISOR,
    VERIFICADOR,
    EstadoOrquestador,
)
from app.grafo.supervisor import supervisor_node
from app.nucleo.errores import ErrorDeAgente


def enrutar(
    state: EstadoOrquestador,
) -> Literal["investigador", "verificador", "redactor", "__end__"]:
    """Traduce la decisión del supervisor a un destino del grafo.

    En el Literal va el VALOR de END (`"__end__"`) porque el tipo describe lo que la función
    devuelve, y es lo que después dibuja bien el diagrama.

    El `.strip()` normaliza antes de decidir. Sin él, un destino con un espacio de más no
    coincide con ninguna clave del mapeo y LangGraph revienta con un KeyError crudo que no
    menciona el ruteo.
    """
    # `completado` es el veredicto explicito que escribe el supervisor: la arista lo lee y
    # no vuelve a deducir si la tarea termino. `siguiente` queda como el destino cuando no
    # termino, y los dos se escriben juntos en el mismo return del nodo.
    if state.get("completado") or str(state.get("siguiente", "")).strip() == "FINALIZAR":
        return END
    destino = str(state.get("siguiente", "")).strip()
    if destino not in DESTINOS:
        raise ErrorDeAgente(msj.ERROR_DESTINO_INVALIDO.format(destino=destino))
    return destino


def crear_grafo(
    investigador=investigador_node,
    verificador=verificador_node,
    redactor=redactor_node,
    *,
    supervisor=supervisor_node,
):
    """Arma el orquestador y lo compila.

    Los cuatro nodos entran por parámetro con su implementación real por defecto: es lo que
    permite que un test reemplace un nodo sin volver a escribir el cableado. Con el grafo
    armado dos veces, las aristas quedan declaradas dos veces y nada garantiza que sigan
    siendo las mismas.

    Se compila sin checkpointer: el grafo corre de punta a punta dentro de una corrida y no
    tiene ningún punto donde pausarse.
    """
    grafo = StateGraph(EstadoOrquestador)

    grafo.add_node(SUPERVISOR, supervisor)
    grafo.add_node(INVESTIGADOR, investigador)
    grafo.add_node(VERIFICADOR, verificador)
    grafo.add_node(REDACTOR, redactor)

    grafo.add_edge(START, SUPERVISOR)
    # Aristas fijas: los especialistas no deciden nada, siempre devuelven el control.
    for nodo in NODOS:
        grafo.add_edge(nodo, SUPERVISOR)

    # El mapeo va explícito aunque la API lo acepte omitido: sin él, un destino que no
    # existe se ignora en silencio y el grafo termina como si hubiera funcionado.
    grafo.add_conditional_edges(
        SUPERVISOR,
        enrutar,
        {**{nodo: nodo for nodo in NODOS}, END: END},
    )
    return grafo.compile()
