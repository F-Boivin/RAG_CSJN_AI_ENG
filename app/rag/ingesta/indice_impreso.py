"""El índice impreso de un PDF: sus títulos numerados y dónde empieza cada uno en el texto.

Algunos documentos traen el índice escrito en sus primeras páginas y no como outline. El
«Recurso Extraordinario» de la Secretaría son 704 páginas, 7 capítulos y 500 títulos
numerados de hasta seis niveles, sin una sola entrada de outline, y ese índice es la mejor
fuente de subsecciones que el documento ofrece.

Cortar por página no alcanza: en ese documento, 136 de las 292 páginas con títulos tienen más
de uno. Cada título se busca renglón por renglón en el cuerpo, en el orden del índice, y el
texto se corta en el renglón donde aparece.

Un título del índice que no aparece en el cuerpo corta la ingesta con error. Si una edición
nueva cambia un título, caer en silencio a otro método dejaría el texto que le sigue con la
subsección equivocada, y esa subsección es la procedencia que acompaña a cada cita.
"""

import re
import unicodedata
from dataclasses import dataclass

import app.nucleo.constantes as cfg
import app.nucleo.mensajes as msj
from app.nucleo.errores import ErrorRAG
from app.rag.ingesta.pdf import Documento

# "Quienes pueden interponerlo ........ 17": los puntos guía marcan un renglón de índice.
PATRON_PUNTOS_GUIA = re.compile(r"\.{4,}\s*\d{1,4}\s*$")
# La numeración de cabeza: "1.5.2.1 Art. 4".
PATRON_NUMERACION = re.compile(r"^(\d+(?:\.\d+)*)\s+(\S.*)$")
# La página al final, después de puntos guía o de un espacio: cuando el título llena el
# renglón, el documento no deja lugar para los puntos.
PATRON_PAGINA_FINAL = re.compile(r"^(.*\S)(?:\s*\.{2,}\s*|\s+)(\d{1,4})$")


@dataclass(frozen=True)
class Entrada:
    """Un título del índice: su numeración, su texto y la página que declara."""

    numero: tuple[int, ...]
    titulo: str
    pagina: int

    @property
    def nombre(self) -> str:
        return f"{'.'.join(map(str, self.numero))} {self.titulo}"


@dataclass
class Seccion:
    """El texto bajo un título del índice, hasta el título siguiente."""

    nombre: str
    capitulo: str
    desde: int
    hasta: int
    texto: str
    # Dónde empieza cada página dentro de `texto`: (posición, número de página).
    marcas: tuple[tuple[int, int], ...]
    # Los títulos que la contienen, del capítulo a ella, sin numeración: «Sentencias
    # arbitrarias › Causales de arbitrariedad › Excesos u omisiones en el pronunciamiento ›
    # Excesos».
    encabezado: str = ""


def secciones(documento: Documento) -> list[Seccion]:
    """Las secciones del documento según su índice impreso, o ninguna si no lo trae.

    Lo que antecede al primer título —la tapa y el propio índice— queda afuera.
    """
    entradas, ultima_de_indice = leer_indice(documento)
    if not entradas:
        return []
    renglones = [(p.numero, renglon) for p in documento.paginas if p.numero > ultima_de_indice
                 for renglon in p.texto.splitlines()]
    planos = [_plano(renglon) for _, renglon in renglones]

    ubicaciones: list[tuple[int, int]] = []
    cursor = 0
    for entrada in entradas:
        hallado = _ubicar(entrada, renglones, planos, cursor)
        if hallado is None:
            raise ErrorRAG(msj.ERROR_TITULO_SIN_UBICAR.format(
                origen=documento.origen, titulo=entrada.nombre, pagina=entrada.pagina,
                margen=cfg.DESFASE_MAXIMO_DE_TITULO))
        ubicaciones.append(hallado)
        cursor = hallado[0] + hallado[1]

    por_numero = {entrada.numero: entrada for entrada in entradas}
    resultado = []
    capitulo = ""
    for i, (entrada, (renglon, largo)) in enumerate(zip(entradas, ubicaciones)):
        if len(entrada.numero) == 1:
            capitulo = entrada.nombre
        fin = ubicaciones[i + 1][0] if i + 1 < len(ubicaciones) else len(renglones)
        texto, marcas = _armar(renglones[renglon + largo:fin])
        desde = renglones[renglon][0]
        encabezado = " › ".join(por_numero[entrada.numero[:nivel]].titulo
                                for nivel in range(1, len(entrada.numero) + 1)
                                if entrada.numero[:nivel] in por_numero)
        resultado.append(Seccion(nombre=entrada.nombre, capitulo=capitulo, desde=desde,
                                 hasta=marcas[-1][1] if marcas else desde,
                                 texto=texto, marcas=marcas, encabezado=encabezado))
    return resultado


def leer_indice(documento: Documento) -> tuple[list[Entrada], int]:
    """Los títulos del índice impreso y la última página que ocupa, o ([], 0) si no hay.

    Es de índice la primera página que trae varios renglones con puntos guía, y el índice
    sigue mientras las páginas siguientes traigan alguno. Un título que no entra en un renglón
    sigue en el de abajo, y la entrada se completa recién cuando aparece su página.
    """
    primeras = documento.paginas[:cfg.PAGINAS_CON_INDICE_IMPRESO]
    inicio = next((i for i, p in enumerate(primeras)
                   if _renglones_con_puntos(p.texto) >= cfg.RENGLONES_MINIMOS_DE_INDICE), None)
    if inicio is None:
        return [], 0
    paginas = [documento.paginas[inicio]]
    for pagina in documento.paginas[inicio + 1:]:
        if not _renglones_con_puntos(pagina.texto):
            break
        paginas.append(pagina)

    entradas: list[Entrada] = []
    pendiente = ""
    for pagina in paginas:
        for renglon in pagina.texto.splitlines():
            renglon = " ".join(renglon.split())
            if not renglon or not (pendiente or PATRON_NUMERACION.match(renglon)):
                continue
            candidato = f"{pendiente} {renglon}" if pendiente else renglon
            anterior = entradas[-1] if entradas else None
            entrada = _entrada(candidato, anterior, len(documento.paginas))
            if entrada is None:
                pendiente = candidato
                continue
            if not _continua(anterior.numero if anterior else None, entrada.numero):
                raise ErrorRAG(msj.ERROR_INDICE_IMPRESO_ILEGIBLE.format(
                    origen=documento.origen, renglon=candidato,
                    motivo=msj.MOTIVO_NUMERACION_SALTEADA.format(
                        anterior=anterior.nombre if anterior else "")))
            entradas.append(entrada)
            pendiente = ""
    if pendiente:
        raise ErrorRAG(msj.ERROR_INDICE_IMPRESO_ILEGIBLE.format(
            origen=documento.origen, renglon=pendiente, motivo=msj.MOTIVO_RENGLON_SIN_PAGINA))
    return entradas, paginas[-1].numero


def _renglones_con_puntos(texto: str) -> int:
    return sum(1 for renglon in texto.splitlines() if PATRON_PUNTOS_GUIA.search(renglon))


def _entrada(renglon: str, anterior: Entrada | None, paginas: int) -> Entrada | None:
    """La entrada que el renglón completa, o None si todavía le falta la página."""
    numeracion = PATRON_NUMERACION.match(renglon)
    final = PATRON_PAGINA_FINAL.match(numeracion.group(2)) if numeracion else None
    if not final:
        return None
    pagina = int(final.group(2))
    # Un título que termina en número ("Art. 4") y sigue en el renglón de abajo todavía no
    # trae su página: la página no puede ir para atrás ni salirse del documento.
    if pagina > paginas or (anterior and pagina < anterior.pagina):
        return None
    numero = tuple(int(parte) for parte in numeracion.group(1).split("."))
    return Entrada(numero=numero, titulo=final.group(1).rstrip(" ."), pagina=pagina)


def _continua(anterior: tuple[int, ...] | None, numero: tuple[int, ...]) -> bool:
    """Si una numeración puede seguir a la anterior en un índice.

    Puede entrar un nivel más adentro o pasar al siguiente de cualquier nivel, y los niveles
    nuevos empiezan en 1. Un renglón mal leído rompe esa secuencia, y así se nota.
    """
    if anterior is None:
        return all(parte == 1 for parte in numero)
    if len(numero) > len(anterior) and numero[:len(anterior)] == anterior:
        return all(parte == 1 for parte in numero[len(anterior):])
    for nivel in range(len(anterior)):
        if (len(numero) > nivel and numero[:nivel] == anterior[:nivel]
                and numero[nivel] == anterior[nivel] + 1):
            return all(parte == 1 for parte in numero[nivel + 1:])
    return False


def _ubicar(entrada: Entrada, renglones: list[tuple[int, str]], planos: list[str],
            cursor: int) -> tuple[int, int] | None:
    """El renglón del cuerpo donde empieza el título y cuántos renglones ocupa.

    Se compara el título entero, sin tildes ni caja, y un título largo puede ocupar varios
    renglones. La búsqueda arranca donde terminó el título anterior y no se aleja más de
    `DESFASE_MAXIMO_DE_TITULO` páginas de la que declara el índice.
    """
    buscado = _plano(entrada.nombre)
    for k in range(cursor, len(renglones)):
        pagina = renglones[k][0]
        if pagina > entrada.pagina + cfg.DESFASE_MAXIMO_DE_TITULO:
            return None
        if pagina < entrada.pagina - cfg.DESFASE_MAXIMO_DE_TITULO:
            continue
        acumulado = ""
        for largo in range(1, cfg.RENGLONES_MAXIMOS_DE_TITULO + 1):
            if k + largo > len(planos) or not planos[k + largo - 1]:
                break
            acumulado = f"{acumulado} {planos[k + largo - 1]}".strip()
            if acumulado.rstrip(" .") == buscado:
                return k, largo
            if not buscado.startswith(acumulado):
                break
    return None


def _armar(renglones: list[tuple[int, str]]) -> tuple[str, tuple[tuple[int, int], ...]]:
    """El texto de una sección, con cada salto de página como párrafo, y dónde empieza cada una."""
    por_pagina: dict[int, list[str]] = {}
    for pagina, renglon in renglones:
        por_pagina.setdefault(pagina, []).append(renglon)
    texto, marcas = "", []
    for pagina, lineas in por_pagina.items():
        trozo = "\n".join(lineas).strip()
        if not trozo:
            continue
        if texto:
            texto += "\n\n"
        marcas.append((len(texto), pagina))
        texto += trozo
    return texto, tuple(marcas)


def _plano(texto: str) -> str:
    """Sin tildes, sin caja y con los espacios colapsados: lo comparable de un renglón."""
    descompuesto = unicodedata.normalize("NFKD", (texto or "").lower())
    sin_tildes = "".join(letra for letra in descompuesto if not unicodedata.combining(letra))
    return " ".join(sin_tildes.split())
