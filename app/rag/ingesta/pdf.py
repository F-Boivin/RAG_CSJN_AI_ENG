"""Extracción de los PDF de la Secretaría: texto, hipervínculos y citas.

Los PDF traen los links oficiales embebidos como anotaciones, anclados al texto exacto de la
cita. Esa anotación es la afirmación de la propia CSJN de que esa cita existe y esa es su URL,
así que es la fuente primaria del padrón; el regex sobre el texto la complementa. Medido sobre
las 82 notas: 2.440 citas únicas por anotación contra 1.720 por regex, de las cuales 1.600 ya
venían por anotación.

Las notas están a dos columnas y `page.get_text()` respeta el orden de lectura columna por
columna. `get_text(sort=True)` intercala las dos y arruina el texto, así que se usa el orden
por defecto.

PyMuPDF es AGPL. El repositorio y el servicio son públicos, así que la obligación de publicar
el fuente ya está cumplida; es una decisión consciente y no un descuido.
"""

import re
from collections import Counter
from dataclasses import dataclass, field

import fitz

import app.nucleo.constantes as cfg
import app.nucleo.mensajes as msj
from app.nucleo.errores import ErrorRAG
from app.rag import citas as c

# Palabra cortada al final de renglón: "arbitrarie-\ndad" vuelve a ser una sola.
PATRON_GUION = re.compile(r"(\w)-\n(\w)")
# Cita partida por el salto de línea del PDF: "Fallos: \n332:111".
PATRON_CITA_PARTIDA = re.compile(r"(\d)\s*\n\s*:\s*(\d)|(\d)\s*:\s*\n\s*(\d)")
# La fecha de corte que un documento escribe en su tapa: "Actualizado al 10/09/2026".
PATRON_ACTUALIZACION = re.compile(r"Actualizad[oa] al (\d{1,2}/\d{1,2}/\d{4})")
PAGINAS_DE_TAPA = 3


@dataclass
class Pagina:
    """Una página extraída: su texto y los links que lleva."""

    numero: int
    texto: str
    citas_urls: dict[str, str] = field(default_factory=dict)
    titulos: list[str] = field(default_factory=list)


@dataclass
class Documento:
    """Un PDF extraído, listo para segmentar."""

    origen: str
    titulo: str
    paginas: list[Pagina]
    outline: list[tuple[int, str, int]] = field(default_factory=list)
    desacuerdos: int = 0

    @property
    def texto(self) -> str:
        return "\n\n".join(p.texto for p in self.paginas)

    @property
    def citas_urls(self) -> dict[str, str]:
        acumulado: dict[str, str] = {}
        for pagina in self.paginas:
            for cita, url in pagina.citas_urls.items():
                if url or cita not in acumulado:
                    acumulado.setdefault(cita, url)
                    if url:
                        acumulado[cita] = url
        return acumulado


def abrir(datos: bytes, origen: str) -> fitz.Document:
    """El PDF abierto desde memoria, o el aviso de cuál no se pudo leer."""
    try:
        return fitz.open(stream=datos, filetype="pdf")
    except Exception as exc:  # PyMuPDF levanta su propia jerarquía
        raise ErrorRAG(msj.ERROR_PDF_ILEGIBLE.format(
            origen=origen, detalle=f"{type(exc).__name__}: {exc}")) from exc


def extraer(datos: bytes, origen: str, titulo: str = "") -> Documento:
    """Texto, links y candidatos a título de cada página."""
    documento = abrir(datos, origen)
    try:
        extraidas = [_extraer_pagina(p) for p in documento]
        outline = list(documento.get_toc() or [])
    finally:
        documento.close()

    paginas = [pagina for pagina, _ in extraidas]
    desacuerdos = sum(cuantos for _, cuantos in extraidas)
    _sacar_cromo(paginas)
    return Documento(origen=origen, titulo=titulo, paginas=paginas, outline=outline,
                     desacuerdos=desacuerdos)


def _extraer_pagina(pagina) -> tuple[Pagina, int]:
    """Una página y cuántas de sus anclas contradicen a su URL.

    Cuando la URL trae `tomo=` y `pagina=`, la cita sale de ahí y el texto anclado queda como
    control cruzado: los dos tienen que decir lo mismo. El desacuerdo se cuenta y viaja al
    manifiesto, porque una cita mal atribuida rompe la promesa central del sistema —el
    verificador aprobaría una cita que el corpus no respalda—.
    """
    citas_urls: dict[str, str] = {}
    desacuerdos = 0
    for enlace in pagina.get_links():
        uri = enlace.get("uri")
        if not uri:
            continue
        url = c.url_publica(uri)
        del_ancla = c.cita_de_ancla(pagina.get_textbox(enlace["from"]))
        de_la_url = c.cita_de_url(uri)
        if de_la_url and del_ancla and de_la_url != del_ancla:
            desacuerdos += 1
        cita = de_la_url or del_ancla
        if cita and (url or cita not in citas_urls):
            citas_urls[cita] = url or citas_urls.get(cita, "")
    return Pagina(numero=pagina.number + 1, texto=normalizar(pagina.get_text()),
                  citas_urls=citas_urls, titulos=_titulos_de(pagina)), desacuerdos


def fecha_de_actualizacion(textos: list[str]) -> str:
    """La fecha de corte que declaran las primeras páginas, o vacío si no declaran ninguna.

    Los endpoints no exponen fecha ni versión de un documento. El «Recurso Extraordinario» la
    escribe en su tapa, y es lo que le dice al lector hasta cuándo llega la doctrina reunida.
    """
    for texto in textos[:PAGINAS_DE_TAPA]:
        hallada = PATRON_ACTUALIZACION.search(texto or "")
        if hallada:
            return hallada.group(1)
    return ""


def normalizar(texto: str) -> str:
    """Deja el texto de una página listo para segmentar.

    Tres arreglos, todos por cómo el PDF corta las líneas: la cita partida al final de
    renglón, la palabra cortada con guión, y las líneas en blanco de más que dejan las dos
    columnas.
    """
    texto = PATRON_CITA_PARTIDA.sub(
        lambda m: f"{m.group(1) or m.group(3)}:{m.group(2) or m.group(4)}", texto or "")
    texto = PATRON_GUION.sub(r"\1\2", texto)
    lineas = [linea.rstrip() for linea in texto.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lineas)).strip()


def _sacar_cromo(paginas: list[Pagina]) -> None:
    """Quita los encabezados y pies que se repiten en casi todas las páginas.

    Una línea presente en más de `UMBRAL_CROMO` de las páginas del documento es cromo de
    imprenta ("Secretaría de Jurisprudencia – Corte Suprema..."), no contenido: repetirla en
    cada fragmento le roba lugar al texto y ensucia la búsqueda léxica.
    """
    if len(paginas) < 5:
        return
    conteo = Counter(
        linea.strip() for p in paginas for linea in set(p.texto.splitlines())
        if len(linea.strip()) > 12
    )
    tope = cfg.UMBRAL_CROMO * len(paginas)
    cromo = {linea for linea, veces in conteo.items() if veces >= tope}
    if not cromo:
        return
    for pagina in paginas:
        pagina.texto = "\n".join(
            linea for linea in pagina.texto.splitlines() if linea.strip() not in cromo
        ).strip()


def _titulos_de(pagina) -> list[str]:
    """Las líneas de la página que parecen títulos, por tamaño de fuente o negrita.

    Es la segunda fuente de subsecciones, para los documentos sin outline. El criterio es
    tipográfico: una línea corta, con fuente por encima de la mediana del cuerpo o en negrita.
    """
    try:
        bloques = pagina.get_text("dict")["blocks"]
    except Exception:
        return []
    lineas: list[tuple[float, bool, str]] = []
    for bloque in bloques:
        for linea in bloque.get("lines", []):
            partes = linea.get("spans", [])
            if not partes:
                continue
            texto = " ".join(p.get("text", "") for p in partes).strip()
            if not texto:
                continue
            tamano = max(p.get("size", 0) for p in partes)
            negrita = any(p.get("flags", 0) & 2 ** 4 for p in partes)
            lineas.append((tamano, negrita, texto))
    if not lineas:
        return []
    mediana = _tamano_del_cuerpo(lineas)
    return [
        texto for tamano, negrita, texto in lineas
        if _parece_titulo(texto)
        and (tamano > mediana * 1.15 or (negrita and tamano > mediana * 1.02))
    ]


def _tamano_del_cuerpo(lineas: list[tuple[float, bool, str]]) -> float:
    """El tamaño de fuente del cuerpo, como mediana ponderada por caracteres.

    Ponderar por caracteres y no por líneas es lo que hace que el cuerpo domine: una página
    con un título y un párrafo tiene dos líneas, y la mediana simple caería sobre el propio
    título, que es justo lo que hay que distinguir del cuerpo.
    """
    pesadas = sorted((tamano, len(texto)) for tamano, _, texto in lineas)
    mitad = sum(peso for _, peso in pesadas) / 2
    acumulado = 0.0
    for tamano, peso in pesadas:
        acumulado += peso
        if acumulado >= mitad:
            return tamano
    return pesadas[-1][0]


# Un título de la Secretaría empieza con su numeración ("13.1.2 Competencia originaria"), con
# un romano ("I - Termoeléctrica y ambiente") o directamente con la palabra.
PATRON_INICIO_TITULO = re.compile(r"^(?:[IVXLC]+\s*[-–.)]|\d+(?:\.\d+)*\s*[-–.)]?\s|\w)")


def _parece_titulo(texto: str) -> bool:
    """Descarta las líneas de cuerpo que la tipografía marca por otras razones.

    En las notas, que van a dos columnas, la negrita marca también la cita destacada y el
    renglón suelto. Un título no arrastra el final de una oración, no es una cita, y no
    termina en coma o paréntesis abierto.
    """
    texto = texto.strip()
    if not (6 <= len(texto) <= 90):
        return False
    if texto[-1] in ",;(" or texto.endswith(("y", "de", "el", "la", "que")):
        return False
    if c.PATRON_TOMO_PAGINA.search(texto) or "fallos" in texto.lower():
        return False
    letras = sum(ch.isalpha() for ch in texto)
    if letras < len(texto) * 0.5:
        return False
    return bool(PATRON_INICIO_TITULO.match(texto))
