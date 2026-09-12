"""Lo que se guarda de cada consulta, y lo que se saca antes de guardarlo.

El sitio registra lo que preguntan terceros, que es el tercer objetivo del proyecto. En
Argentina eso cae bajo la ley 25.326 en cuanto sea vinculable a una persona, y el riesgo
concreto es el texto libre que el propio usuario escribe: "mi hermano Juan Pérez, expediente
1234/2020".

`anonimizar` saca lo que se puede reconocer por forma —DNI, CUIT/CUIL, correo, teléfono— antes
de que el texto toque el disco. Es determinista y pura, así que se puede leer y probar. Lo que
queda son nombres propios en prosa, que ningún patrón distingue de la carátula de un fallo; la
defensa ahí es el aviso visible sobre el cuadro de búsqueda.

El registro no lleva IP, user agent, referer ni la cookie del visitante. Esa ausencia es lo
que hace seguro publicar los agregados de `/metricas`.
"""

import hashlib
import json
import re
import uuid

import app.nucleo.constantes as cfg
import app.nucleo.mensajes as msj
from app.almacen.estado import Estado, ahora

# CUIT/CUIL: 11 dígitos con o sin guiones. Va antes que el DNI, que es un prefijo suyo.
PATRON_CUIT = re.compile(r"\b\d{2}-?\d{8}-?\d\b")
# DNI: 7 u 8 dígitos sueltos, con o sin puntos de miles. El ancla de palabra evita comerse
# el año de un fallo o el número de una ley.
PATRON_DNI = re.compile(r"\b\d{1,2}\.?\d{3}\.?\d{3}\b")
PATRON_CORREO = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
# Teléfono argentino en sus formas usuales, con prefijo internacional o sin él.
PATRON_TELEFONO = re.compile(r"(?<!\d)(?:\+?54[\s-]?)?(?:9[\s-]?)?(?:11|\d{2,4})[\s-]?\d{3,4}[\s-]?\d{4}(?!\d)")
# Un link oficial de la Corte. Los ids de documento son de siete dígitos, o sea exactamente la
# forma de un DNI: sin esta excepción, anonimizar una respuesta le rompe los links.
PATRON_LINK_OFICIAL = re.compile(r"https?://[\w.-]*csjn\.gov\.ar/\S*", re.IGNORECASE)


def anonimizar(texto: str, conservar_links: bool = False) -> str:
    """Reemplaza por `[dato removido]` lo que se reconoce como dato personal.

    El orden importa: CUIT antes que DNI, porque un CUIT contiene un DNI y el patrón más
    corto se lo comería a medias.

    `conservar_links` deja intactos los links oficiales de la Corte, y existe por una colisión
    medida: `idDocumento=6952172` son siete dígitos, que es la forma de un DNI, así que el
    patrón se los come y el link deja de abrir. Sobre las respuestas ya guardadas pasaba en 3
    de 15. Va solo donde la procedencia lo justifica —el texto que escribe el redactor, cuyos
    links los arma este código desde el padrón—, y nunca sobre lo que escribe el visitante: en
    un texto de afuera, un link con un DNI adentro es justo lo que hay que tapar.
    """
    limpio = texto or ""
    if conservar_links:
        piezas = PATRON_LINK_OFICIAL.split(limpio)
        links = PATRON_LINK_OFICIAL.findall(limpio)
        limpias = [anonimizar(p) for p in piezas]
        return "".join(
            p + (links[i] if i < len(links) else "") for i, p in enumerate(limpias)
        )
    for patron in (PATRON_CORREO, PATRON_CUIT, PATRON_TELEFONO, PATRON_DNI):
        limpio = patron.sub(msj.DATO_REMOVIDO, limpio)
    return limpio


def nuevo_id() -> str:
    """El id de una consulta, que también es su URL para compartir.

    Es un uuid4 y no un correlativo: la página de un resultado es pública para quien tenga el
    link, así que el id no puede adivinarse.
    """
    return uuid.uuid4().hex


def huella_visitante(valor: str, sal: str) -> str:
    """El identificador de un visitante, sin guardar de qué salió.

    Se aplica tanto a la cookie como a la IP. La sal vive en el entorno: rotarla olvida a
    todos, que es la forma más simple de un borrado.
    """
    return hashlib.sha256(f"{sal}|{valor}".encode("utf-8")).hexdigest()[:32]


def abrir(consulta: str, consulta_id: str, estado: Estado) -> dict:
    """Anota una consulta apenas llega, antes de saber cómo va a salir.

    Se registra en dos tiempos —al llegar y al terminar— para que una corrida interrumpida por
    un reinicio deje rastro igual. Sin la primera escritura, esas consultas serían invisibles
    justo cuando más interesa mirarlas.
    """
    fila = {
        "id": consulta_id,
        "recibida_en": ahora().isoformat(),
        "consulta": anonimizar(consulta),
        "admitida": 1,
        "motivo_inadmision": "",
        "desenlace": cfg.EN_CURSO,
        "respuesta": "",
        "citas": "[]",
        "intentos": 0,
        "vueltas": 0,
        "citas_propuestas": 0,
        "citas_verificadas": 0,
        "citas_inexistentes": 0,
        "senales": "[]",
        "duracion_ms": 0,
        "usd": 0.0,
        "modelo": "",
        "version_indice": "",
        "error": "",
    }
    estado.registrar(fila)
    return fila


def cerrar(fila: dict, estado: Estado, **cambios) -> dict:
    """Completa el registro con el desenlace de la corrida.

    **El filtro vive acá y no en cada llamador**, que es lo que lo hace una garantía: `abrir`
    anonimizaba la consulta y todo lo demás entraba crudo por esta puerta. El caso concreto es
    `motivo_inadmision`, que lo escribe el clasificador «en una oración, dirigida a quien
    preguntó» y cuya causal más frecuente es pedir asesoramiento sobre un caso propio: o sea
    justo las consultas donde alguien escribió un nombre, un DNI o un expediente, devueltas en
    prosa y guardadas doce meses. El traceback de una corrida fallida arrastra lo mismo.

    Cada columna se filtra según de dónde viene su texto: lo que escribe el visitante o un
    modelo sobre él va con el filtro entero; la respuesta conserva sus links oficiales, porque
    los arma este código desde el padrón y el patrón de DNI se come los ids de documento.
    """
    for clave in ("citas", "senales"):
        if clave in cambios and not isinstance(cambios[clave], str):
            cambios[clave] = json.dumps(cambios[clave], ensure_ascii=False)
    for clave in ("consulta", "motivo_inadmision", "error"):
        if clave in cambios:
            cambios[clave] = anonimizar(cambios[clave])
    if "respuesta" in cambios:
        cambios["respuesta"] = anonimizar(cambios["respuesta"], conservar_links=True)
    fila.update(cambios)
    estado.registrar(fila)
    return fila
