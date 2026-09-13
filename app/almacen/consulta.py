"""Lectura del índice léxico: padrón, subsecciones y búsqueda por texto.

Reemplaza a la lectura completa del corpus que el sistema hacía en el arranque. Con 322
fragmentos traer todo a memoria costaba ~100 ms; con 7.400 son cientos de MB antes de atender
la primera consulta. Acá cada pregunta es una consulta a SQLite contra el disco.

Las funciones son las mismas que antes se calculaban en memoria, con la misma firma y la
misma semántica, así que el verificador determinista no distingue de dónde salen los datos.
"""

import sqlite3
import threading
import unicodedata
from difflib import get_close_matches
from pathlib import Path
from typing import Optional, Sequence

import app.nucleo.constantes as cfg
import app.nucleo.mensajes as msj
from app.nucleo.errores import ErrorDeAlmacenamiento
from app.rag import citas as c
from app.almacen.esquema import DDL_LEXICO, conectar


class Lexico:
    """El índice léxico abierto: padrón, subsecciones y búsqueda FTS5.

    Cachea el padrón y los nombres de subsección en memoria, que son los dos datos chicos y
    consultados en cada corrida. El texto de los fragmentos queda en disco.

    **Una conexión, un lock, y toda lectura pasa por `_filas`.** La conexión se abre con
    `check_same_thread=False` porque las consultas corren en `asyncio.to_thread` y el hilo del
    pool cambia entre llamadas; esa bandera apaga el control de sqlite3 y deja la exclusión a
    cargo de quien la usa. Sin el lock, cuatro caminos entran a la misma conexión desde hilos
    distintos —`buscar` desde el recuperador léxico y el padrón desde el verificador—
    y con tres consultas concurrentes se pisan. Reproducido sin modelos: ocho hilos sobre esta
    clase daban `IndexError: tuple index out of range` y `bad parameter or other API misuse`
    desde adentro de `fetchall`, que era el error intermitente que se llevaba puesta la
    consulta entera después de cobrarle el cupo al visitante.

    Serializar no cuesta: cada lectura es de microsegundos contra un índice en disco, y el
    trabajo caro de una corrida son las llamadas al modelo.
    """

    def __init__(self, ruta: Path | str, solo_lectura: bool = True):
        self.ruta = Path(ruta)
        self._conexion = conectar(
            self.ruta, ddl="" if solo_lectura else DDL_LEXICO, solo_lectura=solo_lectura
        )
        self._lock = threading.Lock()
        self._padron: dict[str, str] | None = None
        self._subsecciones: list[str] | None = None

    def _filas(self, sql: str, parametros: tuple = ()) -> list[sqlite3.Row]:
        """Una consulta, entera bajo el lock. Es el único camino a la conexión.

        `execute` devuelve un cursor perezoso, así que el `fetchall` tiene que quedar adentro:
        soltar el lock antes de leer las filas deja la lectura afuera de la exclusión, que es
        justamente lo que se quiere evitar.
        """
        with self._lock:
            try:
                return self._conexion.execute(sql, parametros).fetchall()
            except sqlite3.Error as exc:
                raise ErrorDeAlmacenamiento(msj.ERROR_ALMACEN.format(detalle=exc)) from exc

    def cerrar(self) -> None:
        with self._lock:
            self._conexion.close()

    # --- Padrón de citas ---

    def padron(self) -> dict[str, str]:
        """Todas las citas del corpus: "tomo:pagina" -> URL oficial (o "" sin link).

        La verificación de una cita no pasa por la subsección, a propósito: el investigador
        parafrasea el nombre de la subsección de memoria, así que buscar por nombre exacto
        daría por inexistente un fallo que sí está. Un fallo pertenece al corpus o no; de qué
        subsección salió es otra pregunta, y se responde aparte.
        """
        if self._padron is None:
            filas = self._filas("SELECT cita, url FROM padron")
            self._padron = {f["cita"]: f["url"] for f in filas}
        return self._padron

    def existe(self, cita: str) -> bool:
        clave = c.normalizar_cita(cita)
        return bool(clave) and clave in self.padron()

    def link(self, cita: str) -> str:
        """El link oficial de una cita del padrón, o "" si no está."""
        return self.padron().get(c.normalizar_cita(cita), "")

    # --- Subsecciones ---

    def subsecciones(self) -> list[str]:
        """Los nombres exactos de subsección, para señalar cuando el modelo los inventa."""
        if self._subsecciones is None:
            filas = self._filas("SELECT DISTINCT nombre FROM subsecciones ORDER BY nombre")
            self._subsecciones = [f["nombre"] for f in filas]
        return self._subsecciones

    def subseccion_existe(self, nombre: str) -> bool:
        return self.resolver_subseccion(nombre) is not None

    def resolver_subseccion(self, nombre: str) -> str | None:
        """El nombre tal cual figura en el índice, a partir del que escribió el modelo.

        Tres pasos, del más estricto al más tolerante:

        1. **Igualdad exacta.** Sin este paso, dos títulos que solo se distinguen por su
           numeración dejarían de ser alcanzables incluso escribiéndolos tal cual figuran.
        2. **Clave normalizada**, que perdona la numeración de cabeza y las tildes.
        3. **La mitad que lleva el significado.** Los nombres son «Documento · Título», y el
           modelo escribe el título: copia la parte que dice algo y descarta el prefijo, que
           está ahí para la unicidad y no para él. Medido sobre el corpus: 662 de los 722
           sufijos son únicos, y los que se repiten son los rangos de página del fallback,
           que son ambiguos de verdad.

        Devuelve None ante dos candidatos, porque elegir sería adivinar.
        """
        limpio = (nombre or "").strip().strip("[]").strip()
        nombres = self.subsecciones()
        if limpio in nombres:
            return limpio
        clave = c.clave_subseccion(limpio)
        candidatos = [s for s in nombres if c.clave_subseccion(s) == clave]
        if len(candidatos) == 1:
            return candidatos[0]
        if candidatos:
            return None
        por_sufijo = [
            s for s in nombres
            if s and cfg.SEPARADOR_SUBSECCION in s
            and c.clave_subseccion(s.split(cfg.SEPARADOR_SUBSECCION, 1)[1]) == clave
        ]
        return por_sufijo[0] if len(por_sufijo) == 1 else None

    # --- Búsqueda léxica ---

    def buscar(self, consulta: str, cantidad: int,
               fuentes: Optional[Sequence[str]] = None) -> list[tuple[str, str, dict]]:
        """Los fragmentos que mejor matchean la consulta, por `bm25()` de FTS5.

        `bm25()` devuelve valores negativos y mejor cuanto más negativo, así que el orden es
        ascendente. Las tres columnas pesan distinto: el título más que el cuerpo, porque una
        consulta suele nombrar el tema y el documento que trata de ese tema lo lleva en su
        nombre.

        La consulta se pasa como una lista de términos entre comillas: FTS5 trata los
        operadores (`AND`, `*`, `-`) como sintaxis, y una consulta en lenguaje natural que
        traiga uno de esos caracteres reventaría con un error de sintaxis.

        Con `fuentes`, la búsqueda queda acotada a esos tipos de documento. Es lo que permite
        que el ensamble consulte los dos pools del corpus por separado y los pese distinto:
        filtrar después de recuperar no serviría, porque los 24.145 fragmentos de sentencias
        copan el top-k antes de que haya nada que filtrar.
        """
        expresion = " OR ".join(f'"{t}"' for t in _terminos(consulta))
        if not expresion:
            return []
        fuentes = tuple(fuentes or ())
        filtro = f" AND f.fuente IN ({','.join('?' * len(fuentes))})" if fuentes else ""
        filas = self._filas(
            "SELECT f.id, f.texto, f.origen, f.seccion, f.subseccion, f.fuente, f.pagina "
            "FROM fts JOIN fragmentos f ON f.rowid = fts.rowid "
            f"WHERE fts MATCH ?{filtro} ORDER BY bm25(fts, ?, ?, ?) LIMIT ?",
            (expresion, *fuentes, *cfg.PESOS_BM25, cantidad),
        )
        return [
            (f["id"], f["texto"], {
                "origen": f["origen"], "seccion": f["seccion"],
                "subseccion": f["subseccion"], "fuente": f["fuente"],
                "pagina": f["pagina"],
            })
            for f in filas
        ]

    # --- Estado del índice ---

    def cantidad_fragmentos(self) -> int:
        return self._filas("SELECT count(*) AS n FROM fragmentos")[0]["n"]

    def documentos(self) -> list[dict]:
        return [dict(f) for f in self._filas("SELECT * FROM documentos ORDER BY tipo, origen")]


def _terminos(consulta: str) -> list[str]:
    """Los términos buscables de una consulta, sin la puntuación que FTS5 lee como sintaxis.

    Las citas se conservan enteras ("316:2343") porque el tokenizador tiene `:` entre sus
    `tokenchars`: es lo que hace que una consulta con un número de fallo matchee exacto.

    Las palabras vacías salen, y esa es la diferencia entre encontrar doctrina y encontrar el
    fallo más largo del corpus. La búsqueda es un OR de términos ordenado por `bm25()`: con
    "por", "de" y "la" adentro, una transcripción de trescientas páginas gana por repetirlas,
    y desplaza al fragmento que trata el tema. Medido sobre «recurso extraordinario por
    sentencia arbitraria»: sin este filtro los tres primeros resultados eran suplementos
    ambientales.
    """
    limpia = "".join(ch if ch.isalnum() or ch in ":-" else " " for ch in (consulta or ""))
    palabras = [t for t in limpia.split() if len(t) > 2 or ":" in t]
    utiles = [t for t in palabras if _pelar(t) not in VACIAS]
    # Una consulta hecha solo de palabras vacías se busca tal cual: mejor un resultado flojo
    # que ninguno.
    return utiles or palabras


def _pelar(palabra: str) -> str:
    """La palabra sin tildes ni mayúsculas, para comparar contra la lista de vacías."""
    descompuesta = unicodedata.normalize("NFD", palabra.lower())
    return "".join(ch for ch in descompuesta if unicodedata.category(ch) != "Mn")


# Palabras que aparecen en cualquier consulta jurídica y no la distinguen de ninguna otra.
# Incluye los verbos de pregunta ("dijo", "procede", "aplica"), que son los que más ruido
# meten en un corpus donde cada fallo los usa.
VACIAS = frozenset("""
    que qué cual cuál cuales cuando cuándo como cómo donde dónde quien quién porque por para
    con sin sobre entre desde hasta hacia segun según los las una unos unas del las les del
    este esta esto estos estas ese esa eso esos esas aquel aquella algun alguna algunos
    ser son era eran fue fueron sido siendo estar esta estan estaba haber hay habia
    tiene tienen tenia dice dijo decir dicho hace hacer hecho puede pueden podia
    procede aplica aplican usa usan usar dio dado deber debe deben mas menos muy tan
    caso casos tema temas cosa cosas vez veces
""".split())
