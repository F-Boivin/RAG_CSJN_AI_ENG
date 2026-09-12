"""Herramientas del orquestador sobre el índice de la Secretaría de Jurisprudencia.

Diseño anti-alucinación: **ninguna URL oficial sale de un modelo**. Todas salen del índice, y
las que el corpus no enlaza las arma `citas.link_de_cita` con la plantilla oficial a partir
del tomo y la página. `buscar_doctrina` devuelve los fragmentos SIN links, así que las URLs
llegan por dos vías: `fallos_citados` se las da al investigador por subsección, y
`link_oficial` al redactor, acotada a las citas que el verificador ya aprobó.

Cuatro herramientas y tres consumidores distintos: dos las usa el ReAct del investigador, una
el ReAct del redactor, y a `verificar_citas` la invoca directamente el código del nodo
verificador, que es por qué es la única con `handle_tool_error = False`.

El padrón y las subsecciones se leen del índice léxico, que la ingesta dejó calculados. Antes
se armaban trayendo el corpus entero a memoria en el arranque; con el corpus completo eso son
cientos de MB antes de atender la primera consulta.
"""

import asyncio

from chromadb.errors import ChromaError
from langchain_chroma import Chroma
from langchain_classic.retrievers import EnsembleRetriever
from langchain_core.tools import ToolException, tool
from openai import APIConnectionError, APIError, RateLimitError
from pydantic import BaseModel, Field

import app.nucleo.constantes as cfg
import app.nucleo.mensajes as msj
from app.almacen.consulta import Lexico
from app.nucleo.errores import ErrorDeAlmacenamiento
from app.observabilidad.trazas import anotar_documentos, span_de_recuperacion
from app.rag import citas as c
from app.rag.hibrido import crear_hibrido
from app.rag.ingesta.markdown import contar_tokens

NL = chr(10)

# Reexportados: el verificador y el redactor los usan por este módulo, que es su única puerta
# a la capa RAG.
citas_del_texto = c.citas_del_texto
normalizar_cita = c.normalizar_cita
cuantas_citas = c.cuantas_citas
contar_llamadas = c.contar_llamadas

_vectorstore: Chroma | None = None
_lexico: Lexico | None = None
_hibrido: EnsembleRetriever | None = None


def inicializar(vectorstore: Chroma, lexico: Lexico) -> None:
    """Inyecta el índice y arma el retriever híbrido.

    Las dos mitades del índice entran juntas porque describen el mismo corpus: el vectorial
    busca por significado sobre los embeddings, el léxico por palabra sobre FTS5, y la fusión
    dedupica por contenido. Inyectarlas por separado abriría la puerta a que una describa un
    índice y la otra, otro.
    """
    global _vectorstore, _lexico, _hibrido
    _vectorstore = vectorstore
    _lexico = lexico
    _hibrido = crear_hibrido(vectorstore, lexico)
    # El primer `contar_tokens` construye el codificador de tiktoken, y puede bajarlo por
    # red. Que ocurra acá y no dentro de la primera búsqueda.
    contar_tokens("")


def _hibrido_actual() -> EnsembleRetriever:
    """El retriever híbrido, o el aviso de que faltó inicializar."""
    if _hibrido is None:
        raise RuntimeError(msj.ERROR_HERRAMIENTA_NO_INICIALIZADA)
    return _hibrido


def _indice() -> Lexico:
    """El índice léxico inyectado, o el aviso de que faltó inicializar."""
    if _lexico is None:
        raise RuntimeError(msj.ERROR_HERRAMIENTA_NO_INICIALIZADA)
    return _lexico


def padron_de_citas() -> dict[str, str]:
    """Todas las citas del corpus: "tomo:pagina" -> URL oficial (o "" sin link)."""
    return _indice().padron()


def subsecciones_del_corpus() -> list[str]:
    """Los nombres exactos de subsección del índice."""
    return _indice().subsecciones()


def subseccion_existe(nombre: str) -> bool:
    """True si esa subsección está en el corpus, tolerando la numeración y las tildes."""
    return _indice().subseccion_existe(nombre)


def resolver_subseccion(nombre: str) -> str | None:
    """El nombre tal cual figura en el índice, a partir del que escribió el modelo."""
    return _indice().resolver_subseccion(nombre)


class EntradaBusqueda(BaseModel):
    """Entrada de buscar_doctrina: Pydantic valida antes de tocar la base."""

    consulta: str = Field(
        min_length=3,
        max_length=cfg.LARGO_MAXIMO_CONSULTA,
        description="Tema o pregunta jurídica a buscar, en lenguaje natural.",
    )
    cantidad: int = Field(
        default=cfg.RESULTADOS_RECUPERADOS,
        ge=1,
        le=cfg.MAXIMO_RESULTADOS,  # tope defensivo: acota el contexto por llamada
        description="Cantidad máxima de fragmentos a devolver.",
    )


class EntradaFallos(BaseModel):
    """Entrada de fallos_citados."""

    subseccion: str = Field(
        min_length=3,
        max_length=200,
        description=(
            "Nombre de la subsección del corpus, de los que buscar_doctrina lista al final. "
            "Se acepta con su numeración o sin ella: «6.2.7 Exceso ritual manifiesto» y "
            "«Exceso ritual manifiesto» llegan al mismo lugar."
        ),
    )


@tool(args_schema=EntradaBusqueda)
async def buscar_doctrina(consulta: str, cantidad: int = cfg.RESULTADOS_RECUPERADOS) -> str:
    """Busca doctrina y jurisprudencia de la CSJN en el corpus indexado.

    Usá esta herramienta cuando el usuario pregunte por doctrina, precedentes o
    criterios de la Corte Suprema: el corpus reúne el cuadernillo sobre sentencias
    arbitrarias, las notas de jurisprudencia y los suplementos temáticos de la
    Secretaría de Jurisprudencia. Devuelve fragmentos con su subsección entre
    corchetes; los fragmentos citan números de fallo pero NO traen links: para los
    links usá fallos_citados. Busca por significado y por palabra exacta a la vez,
    así que sirve tanto para un tema como para un número de fallo escrito literal.
    No sirve para otras ramas del derecho ni para hechos actuales.

    Args:
        consulta: tema o pregunta a buscar (3 a 500 caracteres).
        cantidad: máximo de fragmentos a devolver (1 a 8; por defecto 4).

    Returns:
        Fragmentos ordenados por similitud, cada uno encabezado por su subsección.

    Raises:
        ToolException: si el índice o la API de embeddings fallan; el mensaje explica
        el problema para que el modelo pueda informarlo.
    """
    try:
        with span_de_recuperacion("hibrido_fusion", consulta, k=cantidad,
                                  candidatos=cfg.CANDIDATOS_POR_RETRIEVER) as span:
            fusionados = await _hibrido_actual().ainvoke(consulta)
            # El recorte se aplica sobre la lista ya fusionada: cada retriever aporta más
            # candidatos justamente para que la fusión tenga de dónde elegir.
            documentos = fusionados[:cantidad]
            anotar_documentos(span, documentos)
    except (RateLimitError, APIConnectionError, APIError, ChromaError,
            ErrorDeAlmacenamiento, OSError) as exc:
        raise ToolException(msj.ERROR_HERRAMIENTA_BASE.format(detalle=exc)) from exc

    if not documentos:
        return msj.MENSAJE_SIN_RESULTADOS

    partes = []
    subsecciones = []
    for doc in documentos:
        subseccion = doc.metadata.get("subseccion", "")
        if subseccion and subseccion not in subsecciones:
            subsecciones.append(subseccion)
        texto = doc.page_content.strip()
        if len(texto) > cfg.LARGO_MAXIMO_FRAGMENTO:
            texto = texto[: cfg.LARGO_MAXIMO_FRAGMENTO] + "…"
        partes.append(f"[{subseccion}]\n{texto}")

    # El separador entre fragmentos también aparece dentro de ellos: el corpus separa sus
    # extractos con `---` y el splitter lo conserva. Los encabezados `[subseccion]` son la
    # marca inequívoca de dónde empieza cada fragmento; partir esta salida por el separador
    # cuenta de más. El modelo se orienta por los encabezados, así que queda documentado.
    listado = "\n---\n".join(partes)
    return f"{listado}\n\n{msj.ENCABEZADO_SUBSECCIONES} {'; '.join(subsecciones)}"


@tool(args_schema=EntradaFallos)
async def fallos_citados(subseccion: str) -> str:
    """Lista los fallos citados en una subsección del corpus, con su link oficial.

    Usá esta herramienta DESPUÉS de buscar_doctrina, cuando el usuario pida links,
    fuentes verificables o el detalle de los fallos de una subsección. Las
    referencias salen del índice, nunca del modelo: si un fallo no aparece acá, no
    está citado en esa subsección.

    Args:
        subseccion: nombre exacto de la subsección, tomado de la lista final de
            buscar_doctrina (si viene con corchetes, se toleran).

    Returns:
        Lista "Fallos: tomo:página — URL" en orden cronológico, o, si la subsección
        no existe, un aviso con los nombres más parecidos para elegir uno y reintentar.

    Raises:
        ToolException: si el índice falla.
    """
    # El índice guarda los títulos con su numeración y el modelo los escribe sin ella, así que
    # la consulta se hace contra el nombre resuelto. Es la misma tolerancia que el verificador
    # aplica por su lado.
    try:
        exacta = await asyncio.to_thread(resolver_subseccion, subseccion)
        if exacta is None:
            pedido = subseccion.strip().strip("[]").strip()
            parecidas = await asyncio.to_thread(_indice().subsecciones_parecidas, pedido)
            return msj.MENSAJE_SIN_SUBSECCION.format(
                subseccion=pedido, validas="; ".join(parecidas)
            )
        with span_de_recuperacion("citas_por_subseccion", exacta):
            fallos = await asyncio.to_thread(_indice().citas_de_subseccion, exacta)
    except (ErrorDeAlmacenamiento, OSError, ValueError) as exc:
        raise ToolException(msj.ERROR_HERRAMIENTA_BASE.format(detalle=exc)) from exc

    if not fallos:
        parecidas = await asyncio.to_thread(_indice().subsecciones_parecidas, exacta)
        return msj.MENSAJE_SIN_SUBSECCION.format(
            subseccion=exacta, validas="; ".join(parecidas)
        )

    lineas = [
        f"- Fallos: {cita} — {url}" if url else f"- Fallos: {cita}"
        for cita, url in sorted(fallos.items(), key=lambda par: c.clave_fallo(par[0]))
    ]
    encabezado = msj.ENCABEZADO_FALLOS.format(subseccion=exacta)
    return f"{encabezado}\n" + "\n".join(lineas)


class EntradaVerificacion(BaseModel):
    """Entrada de verificar_citas."""

    citas: list[str] = Field(
        min_length=1,
        max_length=40,
        description='Citas a comprobar, como las escribió el investigador. Ej: "Fallos: 311:2437".',
    )


@tool(args_schema=EntradaVerificacion)
async def verificar_citas(citas: list[str]) -> str:
    """Comprueba, contra el padrón del corpus, cuáles de esas citas existen.

    Usala para auditar una investigación antes de darla por buena. El veredicto es un hecho:
    una cita está en el padrón del corpus o no está.

    Args:
        citas: entre 1 y 40 citas tal como fueron escritas.

    Returns:
        Una línea por cita: "<cita> | EXISTE | <url>", "<cita> | EXISTE" cuando la cita está
        en el corpus pero sin link registrado, o "<cita> | NO EXISTE".

    Raises:
        ToolException: si el índice falla.
    """
    try:
        padron = await asyncio.to_thread(padron_de_citas)
    except (ErrorDeAlmacenamiento, OSError, ValueError) as exc:
        raise ToolException(msj.ERROR_HERRAMIENTA_BASE.format(detalle=exc)) from exc

    lineas = []
    for cita in citas:
        clave = c.normalizar_cita(cita)
        if clave and clave in padron:
            url = padron[clave]
            lineas.append(f"{cita} | EXISTE | {url}" if url else f"{cita} | EXISTE")
        else:
            lineas.append(f"{cita} | NO EXISTE")
    return NL.join(lineas)


def leer_veredicto(texto: str) -> dict[str, bool]:
    """Convierte la salida de verificar_citas en {cita: existe}.

    Devuelve la existencia y no la URL. Una cita del cuerpo de la doctrina existe y puede no
    tener link registrado, así que leer la URL como si fuera el veredicto la daría por
    inexistente. Los links los reparte `link_oficial`, que consulta el padrón directo.
    """
    veredicto: dict[str, bool] = {}
    for linea in texto.splitlines():
        partes = [p.strip() for p in linea.split("|")]
        if len(partes) >= 2:
            veredicto[partes[0]] = partes[1] == "EXISTE"
    return veredicto


class EntradaLink(BaseModel):
    """Entrada de link_oficial."""

    cita: str = Field(
        min_length=3,
        max_length=120,
        description='Un fallo de la lista de citas verificadas. Ej: "Fallos: 311:2437".',
    )


def crear_link_oficial(verificadas):
    """Arma la herramienta de links, acotada a las citas aprobadas de ESTA corrida.

    La lista permitida se captura en el cierre en vez de leerse del padrón completo, y es la
    decisión que hace que esta herramienta no debilite nada. Si aceptara cualquiera de las
    miles de citas del corpus, el redactor podría pedir el link de un fallo real pero ajeno al
    tema, citarlo, y la guarda lo dejaría pasar porque existe. **Verificado no es lo mismo que
    pertinente**, y la herramienta que reparte links no puede ser la que borre esa diferencia.

    Devuelve una tool nueva por corrida. Es barato —no toca la base al construirse— y evita el
    estado global mutable que haría falta para inyectar la lista de otro modo.
    """
    permitidas = {c.normalizar_cita(x) for x in verificadas} - {""}

    @tool(args_schema=EntradaLink)
    async def link_oficial(cita: str) -> str:
        """Devuelve el link oficial de la CSJN para un fallo ya verificado.

        Usala para cada fallo que vayas a citar en la respuesta final, así el lector puede
        abrirlo. El link sale del índice, nunca de tu memoria: si esta herramienta no te lo
        da, ese fallo no tiene link verificable.

        No sirve para buscar doctrina ni para averiguar si un fallo existe: solo responde por
        las citas que el verificador ya aprobó en esta consulta.

        Args:
            cita: el fallo tal como figura en tu lista de citas verificadas.

        Returns:
            "<cita> - <url>", o un aviso claro si la cita no está entre las aprobadas.

        Raises:
            ToolException: si el índice falla.
        """
        clave = c.normalizar_cita(cita)
        if not clave:
            return msj.MENSAJE_CITA_ILEGIBLE.format(cita=cita)
        if clave not in permitidas:
            return msj.MENSAJE_CITA_NO_APROBADA.format(cita=cita)
        try:
            padron = await asyncio.to_thread(padron_de_citas)
        except (ErrorDeAlmacenamiento, OSError, ValueError) as exc:
            raise ToolException(msj.ERROR_HERRAMIENTA_BASE.format(detalle=exc)) from exc
        url = padron.get(clave)
        if not url:
            return msj.MENSAJE_SIN_LINK.format(cita=cita)
        return f"{cita} - {url}"

    # La consume un ReAct: el error vuelve como observación y el modelo reacciona al texto.
    link_oficial.handle_tool_error = True
    return link_oficial


# --- Configuración de las tools, toda junta ---

# Las del investigador las consume un ReAct: el error vuelve como observación y el modelo
# reacciona al texto, así que no corta el grafo.
buscar_doctrina.handle_tool_error = True
fallos_citados.handle_tool_error = True

# La del verificador la consume código, que espera el formato exacto
# "cita | EXISTE | url". Con handle_tool_error, una caída del índice volvería como string de
# error, leer_veredicto no lo parsearía y toda cita quedaría marcada como inexistente: el
# sistema acusaría al investigador de inventar. Explícito en False aunque sea el default:
# acá la ToolException tiene que llegar al except de verificador_node y salir como
# ErrorDeAgente.
verificar_citas.handle_tool_error = False

# La de `link_oficial` se asigna dentro de `crear_link_oficial`, porque esa tool se arma una
# por corrida y no existe todavía cuando corre este bloque.

# Las dos primeras son del investigador, que las recibe como lista porque las consume un
# ReAct. `verificar_citas` no va en ninguna lista: al verificador no lo maneja un modelo que
# elija herramientas, la invoca directo el código del nodo.
HERRAMIENTAS_INVESTIGACION = [buscar_doctrina, fallos_citados]
