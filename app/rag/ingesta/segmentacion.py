"""De un PDF extraído a los fragmentos que entran al índice.

Dos decisiones viven acá. La primera es cómo se corta el texto: el mismo splitter por tokens
que el cuadernillo, con los separadores que un PDF sí tiene (los `---` del markdown no
existen en un PDF).

La segunda son las subsecciones. Son la procedencia que acompaña a cada cita en la respuesta,
y con una sola para un suplemento de 862 páginas esa procedencia no diría nada.
Se resuelven en cascada —outline del PDF, títulos por tipografía, bloques de páginas— y el
método elegido queda anotado en la ficha del documento, así se puede mirar cuál cayó al
fallback.

El nombre lleva siempre el prefijo del documento. `resolver_subseccion` devuelve None ante dos
candidatos con la misma clave normalizada, y con ~800 nombres los "Introducción" de distintos
suplementos colisionarían sin prefijo.
"""

from dataclasses import dataclass

import app.nucleo.constantes as cfg
from app.rag import citas as c
from app.rag.ingesta.markdown import contar_tokens, crear_splitter
from app.rag.ingesta.pdf import Documento


@dataclass
class Tramo:
    """Un pedazo del documento bajo un mismo título, con el rango de páginas que abarca."""

    nombre: str
    desde: int
    hasta: int


def subsecciones(documento: Documento) -> tuple[list[Tramo], str]:
    """Los tramos del documento y el método con que se detectaron.

    La cascada, en orden: el outline del PDF si sus entradas son cortas; los títulos que la
    tipografía marca; y bloques de páginas. Los tres devuelven lo mismo, así que el resto de
    la ingesta no distingue de dónde salieron.
    """
    for metodo, detectar in (("outline", _por_outline), ("titulos", _por_titulos)):
        tramos = detectar(documento)
        if tramos:
            return tramos, metodo
    return _por_bloques(documento), "bloques"


def _por_outline(documento: Documento) -> list[Tramo]:
    """Los tramos del índice que el propio PDF trae, si son de grano suficientemente fino."""
    entradas = [(nombre.strip(), pagina) for _, nombre, pagina in documento.outline
                if nombre and nombre.strip() and pagina > 0]
    if not entradas:
        return []
    ultima = len(documento.paginas)
    tramos = []
    for i, (nombre, desde) in enumerate(entradas):
        hasta = entradas[i + 1][1] - 1 if i + 1 < len(entradas) else ultima
        tramos.append(Tramo(nombre, desde, max(desde, hasta)))
    demasiado_gruesos = [t for t in tramos if t.hasta - t.desde + 1 > cfg.MAXIMO_PAGINAS_POR_SUBSECCION]
    return [] if demasiado_gruesos else tramos


def _por_titulos(documento: Documento) -> list[Tramo]:
    """Los tramos que marcan los títulos detectados por tipografía.

    Se descarta si produce tramos tan gruesos como el fallback por páginas: en ese caso el
    corte por bloques dice lo mismo y no se apoya en una heurística.
    """
    marcas: list[tuple[str, int]] = []
    for pagina in documento.paginas:
        for titulo in pagina.titulos[:1]:  # el primero de cada página alcanza para cortar
            marcas.append((titulo, pagina.numero))
    if len(marcas) < 3:
        return []
    ultima = len(documento.paginas)
    tramos = []
    for i, (nombre, desde) in enumerate(marcas):
        hasta = marcas[i + 1][1] - 1 if i + 1 < len(marcas) else ultima
        if hasta >= desde:
            tramos.append(Tramo(nombre, desde, hasta))
    if not tramos:
        return []
    grueso = max(t.hasta - t.desde + 1 for t in tramos)
    return tramos if grueso <= cfg.MAXIMO_PAGINAS_POR_SUBSECCION else []


def _por_bloques(documento: Documento) -> list[Tramo]:
    """El fallback: bloques de páginas de tamaño fijo."""
    total = len(documento.paginas) or 1
    paso = cfg.PAGINAS_POR_BLOQUE
    return [
        Tramo(f"págs. {desde}-{min(desde + paso - 1, total)}", desde,
              min(desde + paso - 1, total))
        for desde in range(1, total + 1, paso)
    ]


def sirve(texto: str) -> bool:
    """Si un fragmento puede sostener doctrina, o es ruido de extracción.

    Dos documentos del corpus salen mal del PDF: una nota cuya fuente no trae mapa a Unicode
    —su texto son bytes de control— y un suplemento de manuscritos escaneados, cuya capa de
    texto es ruido de OCR. A eso se suman los números de página sueltos y los renglones de
    índice con puntos suspensivos.

    El ruido no solo no aporta: distorsiona la búsqueda léxica. `bm25()` normaliza por
    longitud, así que un fragmento de tres caracteres que matchea un término se lleva un
    puntaje enorme y desplaza a la doctrina.

    Los dos umbrales salen de medir el corpus construido. La proporción de letras tiene
    mediana 80% y percentil 5 en 60%; entre 55% y 62% viven las listas de citas y las tablas
    de «Citas de doctrina», que son valiosas justamente por ser densas en números. El corte en
    40% deja todo eso adentro y saca 175 fragmentos de ruido, **al costo de una sola cita de
    las 5.950 del padrón**.
    """
    if len(texto) < cfg.LARGO_MINIMO_FRAGMENTO:
        return False
    letras = sum(1 for ch in texto if ch.isalpha())
    return letras / len(texto) >= cfg.PROPORCION_MINIMA_LETRAS


def _nombre_completo(titulo_documento: str, nombre: str) -> str:
    """El nombre de subsección con el prefijo de su documento, recortado a lo que entra."""
    corto = (titulo_documento or "").strip()[:70]
    completo = f"{corto}{cfg.SEPARADOR_SUBSECCION}{nombre.strip()}" if corto else nombre.strip()
    return completo[:200]


def fragmentar(documento: Documento, ficha: dict) -> tuple[list[dict], str]:
    """Los fragmentos del documento, con la metadata que consume el índice.

    Devuelve también el método de subsección elegido, para la ficha.

    `citas_urls` lleva solo las citas que aparecen en ESE fragmento, y ese recorte es lo que
    sostiene la regla de que el investigador solo cite lo que leyó. Se cruzan las dos
    fuentes —las citas que el texto del fragmento escribe y los links que el documento
    enlaza—, así una cita que el PDF no enlazó entra igual, con URL vacía, y
    `completar_links_faltantes` le arma la oficial.
    """
    tramos, metodo = subsecciones(documento)
    enlazadas = documento.citas_urls
    splitter = crear_splitter(separadores=cfg.SEPARADORES_PDF)
    por_pagina = {p.numero: p for p in documento.paginas}

    fragmentos: list[dict] = []
    for tramo in tramos:
        paginas = [por_pagina[n] for n in range(tramo.desde, tramo.hasta + 1)
                   if n in por_pagina]
        texto = "\n\n".join(p.texto for p in paginas if p.texto).strip()
        if not texto:
            continue
        nombre = _nombre_completo(ficha.get("titulo", ""), tramo.nombre)
        for pedazo in splitter.split_text(texto):
            pedazo = pedazo.strip()
            if not sirve(pedazo):
                continue
            del_texto = c.citas_del_texto(pedazo)
            citas_urls = {cita: enlazadas.get(cita, "") for cita in del_texto}
            # Las citas que el tramo enlaza y el fragmento no escribe quedan afuera: `Fallos:
            # 343:2211` tiene que estar en este texto para que este fragmento la ofrezca.
            fragmentos.append({
                "id": f"{documento.origen}#{len(fragmentos):04d}",
                "origen": documento.origen,
                "seccion": ficha.get("titulo", ""),
                "subseccion": nombre,
                "fuente": ficha.get("tipo", ""),
                "pagina": paginas[0].numero if paginas else None,
                "url_documento": ficha.get("url", ""),
                "texto": pedazo,
                "tokens": contar_tokens(pedazo),
                "citas_urls": citas_urls,
            })
    return fragmentos, metodo
