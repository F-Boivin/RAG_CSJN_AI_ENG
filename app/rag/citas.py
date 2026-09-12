"""Lectura y normalización de citas de fallos. Funciones puras, sin índice ni red.

Una cita de la CSJN se identifica por tomo y página: "Fallos: 311:2437", "Fallos 311:2437" y
"311:2437" son la misma. Todo lo que compara citas —el verificador, el padrón, la auditoría
del texto final— pasa por `normalizar_cita`, así que la forma en que se escribieron deja de
importar en un solo lugar.

Los PDF de la Secretaría parten una cita al final de renglón ("Fallos: \\n332:111"), así que
los patrones toleran espacios en blanco alrededor de los dos puntos.
"""

import re
import unicodedata
from urllib.parse import parse_qs, urlsplit, urlunsplit

import app.nucleo.constantes as cfg

PATRON_TOMO_PAGINA = re.compile(r"(\d+)\s*:\s*(\d+)")

# Toda mención a fallos anclada en la palabra, con la lista de números que la sigue. El ancla
# es lo que separa una cita de un número cualquiera: sin ella, "la audiencia de las 14:30" se
# leería como el tomo 14 página 30.
PATRON_LISTA_FALLOS = re.compile(
    r"\bfallos?\b\s*:?\s*((?:\d+(?:\s*:\s*\d+)?)(?:\s*(?:[;,/]|y)\s*(?:\d+(?:\s*:\s*\d+)?))*)",
    re.IGNORECASE | re.DOTALL,
)
PATRON_UN_NUMERO = re.compile(r"\d+(?:\s*:\s*\d+)?")

# Tomos plausibles de la colección Fallos. Acota lo que se acepta como cita cuando el número
# llega suelto —desde un ancla de hipervínculo, donde no hay palabra "Fallos" que anclar—.
TOMO_MINIMO, TOMO_MAXIMO = 1, 400
PAGINA_MAXIMA = 9999


def normalizar_cita(cita: str) -> str:
    """Reduce una cita a "tomo:pagina", que es lo único que la identifica.

    Devuelve "" cuando el texto no trae ningún par de números reconocible.
    """
    coincidencia = PATRON_TOMO_PAGINA.search(cita or "")
    return f"{coincidencia.group(1)}:{coincidencia.group(2)}" if coincidencia else ""


def citas_del_texto(texto: str) -> set[str]:
    """Las citas que un texto invoca, normalizadas a "tomo:pagina".

    El corpus cita en lista y **abrevia**: `(Fallos: 315:356; 326:2759 y 3334)` son tres
    citas, y la última es la página 3334 del tomo 326 — el tomo se sobreentiende del anterior.
    Buscar solo `tomo:pagina` daría por no citada esa tercera, que es justo la forma en que el
    corpus le enseña al modelo a escribir.

    El alcance llega hasta las citas con números: una referencia como "la doctrina de
    Colalillo" queda afuera. El prompt del redactor la prohíbe, y conviene decir hasta dónde
    llega el control.
    """
    encontradas: set[str] = set()
    for mencion in PATRON_LISTA_FALLOS.finditer(texto or ""):
        tomo = None
        for pieza in PATRON_UN_NUMERO.findall(mencion.group(1)):
            if ":" in pieza:
                tomo, pagina = (p.strip() for p in pieza.split(":", 1))
            else:
                # Página suelta: hereda el tomo de la cita anterior de la misma lista. Sin
                # tomo previo es un número, y no una cita.
                if tomo is None:
                    continue
                pagina = pieza
            encontradas.add(f"{tomo}:{pagina}")
    return encontradas


def cita_de_ancla(texto: str) -> str:
    """La cita que lleva el texto de un hipervínculo del PDF, o "".

    Las anclas dicen "343:2255", "344:3095 «A.C.U.D.E.N.»" o "C. 623. XLV. «Compañía
    Financiera», 10/12/2013". Las dos primeras traen tomo y página; la tercera identifica el
    caso por expediente y queda fuera del padrón, que es de tomo y página.

    Sin la palabra "fallos" que anclar, el rango de tomos plausibles es lo que separa una cita
    de una fecha o de un número de expediente.
    """
    for coincidencia in PATRON_TOMO_PAGINA.finditer(" ".join((texto or "").split())):
        tomo, pagina = int(coincidencia.group(1)), int(coincidencia.group(2))
        if TOMO_MINIMO <= tomo <= TOMO_MAXIMO and 1 <= pagina <= PAGINA_MAXIMA:
            return f"{tomo}:{pagina}"
    return ""


def cita_de_url(url: str) -> str:
    """La cita que codifica una URL `buscarTomoPagina`, o "".

    Es la lectura más firme de las tres: el tomo y la página son parámetros de la URL que
    escribió la propia Secretaría, sin texto de por medio.
    """
    if "buscarTomoPagina" not in (url or ""):
        return ""
    parametros = parse_qs(urlsplit(url).query)
    tomo = (parametros.get("tomo") or [""])[0]
    pagina = (parametros.get("pagina") or [""])[0]
    return f"{tomo}:{pagina}" if tomo.isdigit() and pagina.isdigit() else ""


def url_publica(url: str) -> str:
    """La URL tal como puede abrirla cualquiera, o "" si no hay forma pública.

    Cuatro correcciones sobre lo que traen los PDF, todas encontradas en el corpus real:

    - `sjintranet.csjn.gov.ar` es el host interno de la Secretaría. Cuando el path es el mismo
      servicio público (`/sjconsulta/`), se reescribe el host; cualquier otro path interno
      queda fuera.
    - **Una URL con puerto explícito apunta a un servicio interno** —se encontraron seis a
      `csjn14.csjn.gov.ar:7003`— y desde afuera no abre.
    - **`tomosFallos.do` fue retirado.** Las 17 citas que lo enlazaban responden 200 y sirven la
      home del sitio: el status no alcanza para saberlo, hay que comparar el cuerpo.
    - **http pasa a https.** Los hosts de la Corte lo soportan, y un buscador público que
      enlaza en claro degrada la conexión de quien lo sigue.

    Una cita cuyo link se descarta acá no se queda sin link: entra al padrón con URL vacía y
    `completar_links_faltantes` le arma la oficial con la plantilla de tomo y página.
    """
    if not url:
        return ""
    partes = urlsplit(url)
    if partes.hostname == cfg.HOST_INTERNO_SJ:
        if not partes.path.startswith("/sjconsulta/"):
            return ""
        partes = partes._replace(netloc=cfg.HOST_PUBLICO_SJ)
    if partes.port is not None:
        return ""
    if partes.hostname == cfg.HOST_SJ and partes.path.startswith(cfg.PATH_SJ_RETIRADO):
        return ""
    if partes.scheme == "http":
        partes = partes._replace(scheme="https")
    return urlunsplit(partes)


def link_de_cita(cita: str) -> str:
    """El link oficial de una cita, armado con la plantilla de la Secretaría.

    Es lo que da link a las citas de los documentos que no traen hipervínculos —el Archivo
    Histórico y la serie Ambiental—. La URL la arma este código a partir del tomo y la página
    que el corpus escribió; ningún modelo interviene.
    """
    clave = normalizar_cita(cita)
    if not clave:
        return ""
    tomo, pagina = clave.split(":", 1)
    return cfg.PLANTILLA_LINK_FALLO.format(tomo=tomo, pagina=pagina)


def clave_fallo(cita: str) -> tuple[int, int]:
    """Orden cronológico aproximado: los Fallos se citan como tomo:página."""
    coincidencia = PATRON_TOMO_PAGINA.search(cita or "")
    if not coincidencia:
        return (10**9, 10**9)  # lo no parseable va al final
    return (int(coincidencia.group(1)), int(coincidencia.group(2)))


def cuantas_citas(texto: str) -> int:
    """Cuántas citas distintas invoca un texto. Sirve para exigir que una cita sea una sola."""
    return len(PATRON_TOMO_PAGINA.findall(texto or ""))


def contar_llamadas(mensajes) -> int:
    """Cuántas llamadas a herramienta hizo un agente en su ciclo interno.

    El ciclo ReAct queda adentro del nodo, así que sin este número el multi-paso solo se ve
    leyendo el código. Cuenta las llamadas pedidas y no los mensajes: un modelo puede pedir
    varias en un mismo turno, y eso es lo que muestra cómo razonó.
    """
    return sum(len(getattr(m, "tool_calls", None) or []) for m in mensajes)


def clave_subseccion(nombre: str) -> str:
    """Reduce un nombre de subsección a lo comparable: sin numeración, tildes ni caja.

    El modelo escribe "Caracterizacion" donde el índice dice "6.1.2 Caracterización". Exigir
    el nombre exacto daría por inventada una subsección que existe; ignorar el nombre por
    completo dejaría pasar una inventada de verdad.
    """
    limpio = re.sub(r"^[\d.]+\s*", "", (nombre or "").strip().strip("[]").strip())
    limpio = unicodedata.normalize("NFD", limpio)
    return "".join(c for c in limpio if unicodedata.category(c) != "Mn").lower()
