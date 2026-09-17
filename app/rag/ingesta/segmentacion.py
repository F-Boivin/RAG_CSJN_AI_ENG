"""De un PDF extraído a los fragmentos que entran al índice.

Dos decisiones viven acá. La primera es cómo se corta el texto: un splitter por tokens que
corta primero en el párrafo doble, con cada sumario pegado a sus citas para que el corte
caiga entre sumarios.

La segunda son las subsecciones. Son la procedencia que acompaña a cada cita en la respuesta,
y con una sola para un suplemento de 862 páginas esa procedencia no diría nada.
Se resuelven en cascada —índice impreso, outline del PDF, títulos por tipografía, bloques de
páginas— y el método elegido queda anotado en la ficha del documento, así se puede mirar cuál
cayó al fallback.

El nombre lleva el prefijo del documento. `resolver_subseccion` devuelve None ante dos
candidatos con la misma clave normalizada, y con ~800 nombres los "Introducción" de distintos
suplementos colisionarían sin prefijo. La excepción es el índice impreso: su numeración ya
distingue cada título dentro del documento («6.2.2 Apartamiento de las constancias de la
causa»), y el prefijo repetiría el título del documento en cada ficha. Con un segundo
documento de índice impreso en el corpus, el prefijo vuelve a hacer falta.
"""

import re
from dataclasses import dataclass

import app.nucleo.constantes as cfg
from app.rag import citas as c
from app.rag.ingesta import indice_impreso
from app.rag.ingesta.tokens import contar_tokens, crear_splitter
from app.rag.ingesta.pdf import Documento

INDICE_IMPRESO = "indice_impreso"
# Lo que sigue a un sumario y le pertenece: sus citas ("Fallos: 312:2151; ...", "FALLO A.
# 1430. XLIII. REX; ..."), la nota del dictamen, un voto o disidencia entre paréntesis, y el
# final de un párrafo partido por el salto de página.
PATRON_PARRAFO_PEGADO = re.compile(r"^(?:Fallos:|FALLO\b|[-–]\s*Del dictamen|\(|[a-záéíóúüñ])")


@dataclass
class Tramo:
    """Un pedazo del documento bajo un mismo título, con el rango de páginas que abarca.

    Cuando el corte cae a mitad de página, el tramo trae su propio `texto`, y `marcas` dice en
    qué posición de ese texto empieza cada página. Un texto propio vacío es un título seguido
    de otro título. Sin texto propio (`None`), el tramo son sus páginas enteras.
    """

    nombre: str
    desde: int
    hasta: int
    texto: str | None = None
    marcas: tuple[tuple[int, int], ...] = ()
    seccion: str = ""
    encabezado: str = ""


def subsecciones(documento: Documento) -> tuple[list[Tramo], str]:
    """Los tramos del documento y el método con que se detectaron.

    La cascada, en orden: el índice impreso en las primeras páginas; el outline del PDF si sus
    entradas son cortas; los títulos que la tipografía marca; y bloques de páginas. Los cuatro
    devuelven lo mismo, así que el resto de la ingesta no distingue de dónde salieron.
    """
    for metodo, detectar in ((INDICE_IMPRESO, _por_indice_impreso), ("outline", _por_outline),
                             ("titulos", _por_titulos)):
        tramos = detectar(documento)
        if tramos:
            return tramos, metodo
    return _por_bloques(documento), "bloques"


def _por_indice_impreso(documento: Documento) -> list[Tramo]:
    """Los tramos que marcan los títulos del índice escrito en las primeras páginas.

    Cada tramo corta en el renglón del título, así que trae su propio texto; la sección es el
    capítulo que lo contiene, y el encabezado, la cadena de títulos que `indice.Constructor`
    embebe junto al texto.
    """
    return [
        Tramo(nombre=s.nombre, desde=s.desde, hasta=s.hasta, texto=s.texto, marcas=s.marcas,
              seccion=s.capitulo, encabezado=s.encabezado)
        for s in indice_impreso.secciones(documento)
    ]


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
    splitter = crear_splitter()
    por_pagina = {p.numero: p for p in documento.paginas}

    fragmentos: list[dict] = []
    for tramo in tramos:
        texto, marcas = _pegar_al_parrafo_anterior(*_texto_del_tramo(tramo, por_pagina))
        if not texto:
            continue
        nombre = (tramo.nombre if metodo == INDICE_IMPRESO
                  else _nombre_completo(ficha.get("titulo", ""), tramo.nombre))
        pagina, desde = tramo.desde, 0
        for crudo in splitter.split_text(texto):
            # La posición se busca a partir del comienzo del pedazo anterior. El `start_index`
            # del splitter resta el solapamiento en tokens a una posición en caracteres, y en
            # el «Recurso Extraordinario» dejaba 4 de 1.268 fragmentos en otra página.
            posicion = texto.find(crudo, desde)
            if posicion >= 0:
                desde = posicion + 1
            pagina = _pagina_en(marcas, posicion, pagina)
            pedazo = crudo.strip()
            if not sirve(pedazo):
                continue
            del_texto = c.citas_del_texto(pedazo)
            citas_urls = {cita: enlazadas.get(cita, "") for cita in del_texto}
            # Las citas que el tramo enlaza y el fragmento no escribe quedan afuera: `Fallos:
            # 343:2211` tiene que estar en este texto para que este fragmento la ofrezca.
            fragmentos.append({
                "id": f"{documento.origen}#{len(fragmentos):04d}",
                "origen": documento.origen,
                "seccion": tramo.seccion or ficha.get("titulo", ""),
                "subseccion": nombre,
                "fuente": ficha.get("tipo", ""),
                "pagina": pagina,
                "url_documento": ficha.get("url", ""),
                "texto": pedazo,
                "encabezado": tramo.encabezado,
                "tokens": contar_tokens(pedazo),
                "citas_urls": citas_urls,
            })
    return fragmentos, metodo


def _texto_del_tramo(tramo: Tramo, por_pagina: dict) -> tuple[str, tuple[tuple[int, int], ...]]:
    """El texto del tramo y la posición donde empieza cada una de sus páginas."""
    if tramo.texto is not None:
        return tramo.texto, tramo.marcas
    texto, marcas = "", []
    for numero in range(tramo.desde, tramo.hasta + 1):
        pagina = por_pagina.get(numero)
        trozo = pagina.texto.strip() if pagina else ""
        if not trozo:
            continue
        if texto:
            texto += "\n\n"
        marcas.append((len(texto), numero))
        texto += trozo
    return texto, tuple(marcas)


def _pegar_al_parrafo_anterior(texto: str, marcas: tuple[tuple[int, int], ...]
                               ) -> tuple[str, tuple[tuple[int, int], ...]]:
    """Pega a su párrafo anterior lo que no se sostiene solo.

    Un sumario de la Secretaría es un párrafo seguido del renglón con sus citas, y entre los
    dos hay una línea en blanco: el splitter los toma como dos párrafos y corta entre uno y
    otro. Medido sobre el «Recurso Extraordinario», 750 de 1.257 fragmentos empezaban con las
    citas del sumario que había quedado en el fragmento anterior, y quien lee ese fragmento se
    las atribuye al sumario que sigue. Cuando esas citas caían solas al final de una sección,
    el filtro de calidad las descartaba por cortas, y 12 citas del documento no llegaban a
    ningún fragmento.

    Se pegan con un salto simple el renglón de citas, la nota del dictamen de la Procuración,
    los votos y disidencias entre paréntesis, y el final de un párrafo que un salto de página
    partió, que empieza en minúscula. Así el corte cae entre sumarios.
    """
    partes = texto.split("\n\n")
    pegado = partes[0]
    quitados: list[int] = []
    original = len(partes[0])
    for parte in partes[1:]:
        if PATRON_PARRAFO_PEGADO.match(parte):
            pegado += "\n" + parte
            quitados.append(original)
        else:
            pegado += "\n\n" + parte
        original += 2 + len(parte)
    corridas = tuple((inicio - sum(1 for q in quitados if q < inicio), pagina)
                     for inicio, pagina in marcas)
    return pegado, corridas


def _pagina_en(marcas: tuple[tuple[int, int], ...], posicion: int, anterior: int) -> int:
    """La página donde empieza un pedazo, por su posición en el texto del tramo.

    Un tramo de diez páginas corta en muchos fragmentos, y cada uno dice la página donde
    empieza, no la primera del tramo.
    """
    if posicion < 0:
        return anterior
    return max((pagina for inicio, pagina in marcas if inicio <= posicion), default=anterior)
