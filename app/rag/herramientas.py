"""Herramientas del orquestador sobre el índice de la Secretaría de Jurisprudencia.

Diseño anti-alucinación: **ninguna URL oficial sale de un modelo**. Todas salen del índice, y
las que el corpus no enlaza las arma `citas.link_de_cita` con la plantilla oficial a partir
del tomo y la página. `buscar_doctrina` devuelve los fragmentos con los fallos que cada uno
cita y sin links; los links los reparte `link_oficial` al redactor, acotada a las citas que el
verificador ya aprobó.

Y ninguna cita sale de afuera del texto que el investigador leyó. `crear_buscar_doctrina` anota
en un registro qué fragmento trajo cada fallo, y el verificador rechaza lo que no esté ahí: una
cita puede ser cierta y ajena al tema, y el padrón solo sabe de lo primero.

Tres herramientas y tres consumidores distintos: una la usa el ReAct del investigador, otra el
ReAct del redactor, y a `verificar_citas` la invoca directamente el código del nodo verificador,
que es por qué es la única con `handle_tool_error = False`.

El padrón y las subsecciones se leen del índice léxico, que la ingesta dejó calculados. Antes
se armaban trayendo el corpus entero a memoria en el arranque; con el corpus completo eso son
cientos de MB antes de atender la primera consulta.
"""

import asyncio
import hashlib
import json

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
from app.observabilidad.trazas import span_de_recuperacion
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


def citas_del_fragmento(doc, padron: dict[str, str]) -> list[str]:
    """Los fallos que ese fragmento cita, normalizados y comprobados contra el padrón.

    Dos fuentes que se suman. La primera es el texto del fragmento, leído con el mismo patrón
    que usa el verificador. La segunda es `citas_urls`, que la ingesta sacó de las anotaciones
    del PDF: la Secretaría ancló el link oficial al texto exacto de la cita, y esas anotaciones
    cubren más citas que el regex —2.440 contra 1.720 en las notas—.

    El cruce contra el padrón es lo que hace que esta lista sea citable: una cita que el
    verificador no va a reconocer no tiene por qué llegarle al investigador.
    """
    claves = {c.normalizar_cita(x) for x in c.citas_del_texto(doc.page_content)}
    crudo = doc.metadata.get("citas_urls")
    if crudo:
        try:
            claves |= {c.normalizar_cita(x) for x in json.loads(crudo)}
        except (json.JSONDecodeError, TypeError):
            # Metadata rota de un fragmento no puede tumbar la búsqueda: el texto alcanza.
            pass
    return sorted((k for k in claves if k and k in padron), key=c.clave_fallo)


class Lectura:
    """Lo que la búsqueda le sirvió al investigador durante una corrida.

    Tres registros, porque el verificador hace tres preguntas distintas. `citas` mapea
    `"tomo:pagina"` a la subsección del primer fragmento que trajo ese fallo, y responde
    **de dónde salió esta cita**. `textos` guarda el texto de cada fragmento tal como el
    investigador lo vio —truncado igual—, y responde **qué decía**. `fragmentos_por_cita`
    mapea cada fallo a las huellas de los fragmentos donde apareció, y responde **dónde hay
    que buscar su respaldo**.

    El tercero es el que ata el pasaje a su fallo. Sin él, el respaldo se buscaba en todo lo
    leído, y una cita leída en un documento pasaba con una oración leída en otro: en
    producción, 243:190 —citado en una nota sobre honorarios— salió publicado con un pasaje
    del suplemento de Decretos de Necesidad y Urgencia.

    La huella del texto es su clave, así que un fragmento que vuelve en dos búsquedas se
    guarda una vez.
    """

    def __init__(self):
        self.citas: dict[str, str] = {}
        self.textos: dict[str, str] = {}
        self.fragmentos_por_cita: dict[str, list[str]] = {}

    def anotar(self, texto: str, subseccion: str, fallos: list[str]) -> None:
        huella = huella_de_texto(texto)
        self.textos.setdefault(huella, texto)
        for fallo in fallos:
            # La primera procedencia es la que vale: el mismo fallo puede volver a aparecer
            # más adelante sin que eso cambie de dónde lo leyó el investigador.
            self.citas.setdefault(fallo, subseccion)
            # Los fragmentos, en cambio, se suman todos: el pasaje puede estar en cualquiera.
            huellas = self.fragmentos_por_cita.setdefault(fallo, [])
            if huella not in huellas:
                huellas.append(huella)


def huella_de_texto(texto: str) -> str:
    """Identifica un fragmento por su contenido, que es lo único estable que tiene acá."""
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()[:16]


def crear_buscar_doctrina(lectura: "Lectura"):
    """Arma la herramienta de búsqueda, que anota en `lectura` lo que va sirviendo.

    Lo anotado es lo que después separa una cita pertinente de una cita meramente cierta. **El
    verificador comprueba que exista; esto comprueba que el investigador la haya leído.**

    Antes esa mitad no existía. Una herramienta hermana, `fallos_citados`, devolvía las citas
    de una subsección entera: para una consulta cuyos cuatro fragmentos traían **una** cita, le
    ofrecía 264 con su link oficial, y el prompt le decía que citara de ahí. De las 17 citas que
    el buscador publicó en producción, 13 no estaban en ningún texto que el investigador
    hubiera leído. El sistema contestó sobre el IVA con cuatro fallos anteriores a que el IVA
    existiera, todos reales.

    Una herramienta por corrida, como `crear_link_oficial`: el registro es de esta consulta y
    no un estado global que dos corridas concurrentes se pisarían.
    """

    @tool(args_schema=EntradaBusqueda)
    async def buscar_doctrina(consulta: str,
                              cantidad: int = cfg.RESULTADOS_RECUPERADOS) -> str:
        """Busca doctrina y jurisprudencia de la CSJN en el corpus indexado.

        Usá esta herramienta cuando el usuario pregunte por la doctrina de la Corte Suprema
        sobre sentencias arbitrarias: el corpus es el cuadernillo de la Secretaría de
        Jurisprudencia sobre la arbitrariedad —su concepto, sus causales, la improcedencia
        del recurso y su trámite—. Devuelve fragmentos con su subsección entre
        corchetes y, debajo de cada uno, los fallos que ese fragmento cita. Busca por
        significado y por palabra exacta a la vez, así que sirve tanto para un tema como
        para un número de fallo escrito literal. No sirve para otras ramas del derecho ni
        para hechos actuales.

        **Solo podés citar fallos que aparezcan en estos resultados.** Si el fallo que
        necesitás no está, buscá otra vez con otros términos: no hay ninguna otra
        herramienta que te dé fallos.

        Args:
            consulta: tema o pregunta a buscar (3 a 500 caracteres).
            cantidad: máximo de fragmentos a devolver (1 a 10; por defecto 6).

        Returns:
            Fragmentos ordenados por similitud, cada uno con su subsección y sus fallos, y
            al final la lista completa de los fallos citables.

        Raises:
            ToolException: si el índice o la API de embeddings fallan; el mensaje explica
            el problema para que el modelo pueda informarlo.
        """
        try:
            # El recorte se aplica sobre la lista ya fusionada: cada retriever aporta más
            # candidatos justamente para que la fusión tenga de dónde elegir.
            documentos = (await _hibrido_actual().ainvoke(consulta))[:cantidad]
            padron = await asyncio.to_thread(padron_de_citas)
        except (RateLimitError, APIConnectionError, APIError, ChromaError,
                ErrorDeAlmacenamiento, OSError) as exc:
            raise ToolException(msj.ERROR_HERRAMIENTA_BASE.format(detalle=exc)) from exc

        if not documentos:
            return msj.MENSAJE_SIN_RESULTADOS

        partes = []
        citables: list[str] = []
        for doc in documentos:
            subseccion = doc.metadata.get("subseccion", "")
            texto = doc.page_content.strip()
            if len(texto) > cfg.LARGO_MAXIMO_FRAGMENTO:
                texto = texto[: cfg.LARGO_MAXIMO_FRAGMENTO] + "…"
            fallos = citas_del_fragmento(doc, padron)
            # Se anota el texto ya recortado: es lo que el investigador va a poder citar, y
            # guardar el completo daría por respaldado un pasaje que nunca vio.
            lectura.anotar(texto, subseccion, fallos)
            for fallo in fallos:
                if fallo not in citables:
                    citables.append(fallo)
            pie = (f"{NL}{msj.ENCABEZADO_CITAS_DEL_FRAGMENTO} "
                   f"{'; '.join(f'Fallos: {f}' for f in fallos)}" if fallos else "")
            partes.append(f"[{subseccion}]{NL}{texto}{pie}")

        # El separador entre fragmentos también aparece dentro de ellos: el corpus separa sus
        # extractos con `---` y el splitter lo conserva. Los encabezados `[subseccion]` son la
        # marca inequívoca de dónde empieza cada fragmento; partir esta salida por el
        # separador cuenta de más. El modelo se orienta por los encabezados, así que queda
        # documentado.
        listado = f"{NL}---{NL}".join(partes)
        if not citables:
            return f"{listado}{NL}{NL}{msj.MENSAJE_SIN_CITABLES}"
        cierre = "; ".join(f"Fallos: {f}" for f in sorted(citables, key=c.clave_fallo))
        return f"{listado}{NL}{NL}{msj.ENCABEZADO_CITABLES} {cierre}"

    # La consume un ReAct: el error vuelve como observación y el modelo reacciona al texto,
    # así que no corta el grafo.
    buscar_doctrina.handle_tool_error = True
    return buscar_doctrina


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

    **El estado se busca desde la derecha**, y la cita es todo lo que queda a su izquierda. La
    cita la escribió el modelo y puede traer cualquier cosa, incluido un «|»; el estado y la
    URL, en cambio, los escribe `verificar_citas` y nunca lo traen. Partir desde la izquierda
    y tomar el primer campo como cita cortaba ahí cualquier cita con un «|» adentro.

    La clave sale recortada, igual que `Cita.fallo`, que es contra lo que se la busca.
    """
    veredicto: dict[str, bool] = {}
    for linea in texto.splitlines():
        crudas = linea.split("|")
        estados = [p.strip() for p in crudas]
        # «cita | EXISTE | url», «cita | EXISTE» o «cita | NO EXISTE |»: el estado es el último
        # campo o el anteúltimo, y nunca el primero.
        for posicion in (len(crudas) - 1, len(crudas) - 2):
            if posicion >= 1 and estados[posicion] in ("EXISTE", "NO EXISTE"):
                veredicto["|".join(crudas[:posicion]).strip()] = estados[posicion] == "EXISTE"
                break
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

# `buscar_doctrina` se arma una por corrida en `crear_buscar_doctrina`, que le pone ahí su
# `handle_tool_error`: el error vuelve como observación y el modelo reacciona al texto.

# La del verificador la consume código, que espera el formato exacto
# "cita | EXISTE | url". Con handle_tool_error, una caída del índice volvería como string de
# error, leer_veredicto no lo parsearía y toda cita quedaría marcada como inexistente: el
# sistema acusaría al investigador de inventar. Explícito en False aunque sea el default:
# acá la ToolException tiene que llegar al except de verificador_node y salir como
# ErrorDeAgente.
verificar_citas.handle_tool_error = False

# Lo mismo vale para `link_oficial`: las dos que se arman por corrida llevan su configuración
# adentro de la función que las construye, porque todavía no existen cuando corre este bloque.
#
# El investigador tiene una sola herramienta, y es a propósito. La segunda, `fallos_citados`,
# repartía las citas de una subsección entera —hasta 264 para un top-k que traía una— y era de
# donde salían las citas ciertas pero ajenas al tema. Verificado no es lo mismo que pertinente,
# y una herramienta que reparte fallos sueltos borra esa diferencia.
#
# `verificar_citas` no va en ninguna lista: al verificador no lo maneja un modelo que elija
# herramientas, la invoca directo el código del nodo.
