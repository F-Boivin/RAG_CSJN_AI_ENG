"""Los dos SQLite del sistema: el léxico del índice y el estado del servicio.

Están separados porque tienen ciclos de vida distintos. `lexico.sqlite3` se construye con el
índice y viaja con él, de solo lectura en producción; `estado.sqlite3` acumula cuotas, gasto y
el registro de consultas, y es lo único irreemplazable del despliegue.

Los dos abren en modo WAL, que deja convivir la lectura con la escritura.
"""

import sqlite3
from pathlib import Path

# --- Índice léxico: se escribe en la ingesta, se lee en cada consulta ---
#
# `fts5` contentless (`content=''`) guarda solo el índice invertido: el texto ya vive en la
# tabla `fragmentos`, y una tercera copia sumaría ~25 MB sin comprar nada. `contentless_delete`
# es lo que deja borrar filas por rowid, que es como la ingesta rehace un documento solo
# (SQLite 3.43+). `tokenchars ':'` mantiene "316:2343" como un token, así que una consulta que
# trae una cita literal matchea exacto en vez de partirse en dos números.
DDL_LEXICO = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS fragmentos (
    id          TEXT PRIMARY KEY,
    origen      TEXT NOT NULL,
    seccion     TEXT NOT NULL DEFAULT '',
    subseccion  TEXT NOT NULL DEFAULT '',
    fuente      TEXT NOT NULL DEFAULT '',
    pagina      INTEGER,
    texto       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_fragmentos_origen ON fragmentos(origen);
CREATE INDEX IF NOT EXISTS ix_fragmentos_subseccion ON fragmentos(subseccion);

-- `titulo` es el nombre del documento y su subsección. Sin esa columna, una consulta que
-- nombra un tema —«interés superior del niño»— compite solo contra el cuerpo de los
-- fragmentos, y gana el que repite esas palabras en vez del documento que trata de eso.
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
    texto, citas, titulo,
    content='', contentless_delete=1,
    tokenize="unicode61 tokenchars ':' remove_diacritics 2"
);

CREATE TABLE IF NOT EXISTS padron (
    cita    TEXT PRIMARY KEY,
    url     TEXT NOT NULL DEFAULT '',
    origen  TEXT NOT NULL DEFAULT ''
);

-- La clave es el nombre exacto y no su forma normalizada: dos títulos que normalizan igual
-- ("1. Introducción" y "5. Introducción") son dos subsecciones distintas, y colapsarlas
-- perdería una en silencio. La ambigüedad se resuelve al buscar, devolviendo None.
CREATE TABLE IF NOT EXISTS subsecciones (
    nombre      TEXT PRIMARY KEY,
    clave       TEXT NOT NULL DEFAULT '',
    origen      TEXT NOT NULL DEFAULT '',
    fragmentos  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_subsecciones_origen ON subsecciones(origen);

CREATE TABLE IF NOT EXISTS citas_por_subseccion (
    subseccion  TEXT NOT NULL,
    cita        TEXT NOT NULL,
    PRIMARY KEY (subseccion, cita)
);

CREATE TABLE IF NOT EXISTS documentos (
    origen      TEXT PRIMARY KEY,
    tipo        TEXT NOT NULL,
    titulo      TEXT NOT NULL,
    categoria   TEXT NOT NULL DEFAULT '',
    url         TEXT NOT NULL DEFAULT '',
    sha256      TEXT NOT NULL DEFAULT '',
    paginas     INTEGER NOT NULL DEFAULT 0,
    fragmentos  INTEGER NOT NULL DEFAULT 0,
    metodo_subsecciones TEXT NOT NULL DEFAULT ''
);
"""

# --- Estado del servicio: cuotas, gasto y registro de consultas ---
#
# `pulsos` va aparte del registro porque su retención es otra: 48 h contra 12 meses. Guardar
# los dos juntos obligaría a que la política más larga arrastre datos de cuota.
DDL_ESTADO = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS pulsos (
    clave  TEXT NOT NULL,
    tipo   TEXT NOT NULL,
    ts     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_pulsos ON pulsos(clave, tipo, ts);

CREATE TABLE IF NOT EXISTS cuota_global (
    dia        TEXT PRIMARY KEY,
    consultas  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS gasto (
    dia         TEXT PRIMARY KEY,
    usd         REAL NOT NULL DEFAULT 0.0,
    llamadas    INTEGER NOT NULL DEFAULT 0,
    tokens_in   INTEGER NOT NULL DEFAULT 0,
    tokens_out  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS consultas (
    id                 TEXT PRIMARY KEY,
    recibida_en        TEXT NOT NULL,
    consulta           TEXT NOT NULL,
    admitida           INTEGER NOT NULL DEFAULT 1,
    motivo_inadmision  TEXT NOT NULL DEFAULT '',
    desenlace          TEXT NOT NULL DEFAULT '',
    respuesta          TEXT NOT NULL DEFAULT '',
    citas              TEXT NOT NULL DEFAULT '[]',
    intentos           INTEGER NOT NULL DEFAULT 0,
    vueltas            INTEGER NOT NULL DEFAULT 0,
    citas_propuestas   INTEGER NOT NULL DEFAULT 0,
    citas_verificadas  INTEGER NOT NULL DEFAULT 0,
    citas_inexistentes INTEGER NOT NULL DEFAULT 0,
    senales            TEXT NOT NULL DEFAULT '[]',
    duracion_ms        INTEGER NOT NULL DEFAULT 0,
    usd                REAL NOT NULL DEFAULT 0.0,
    modelo             TEXT NOT NULL DEFAULT '',
    version_indice     TEXT NOT NULL DEFAULT '',
    error              TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_consultas_ts ON consultas(recibida_en);
"""


def conectar(ruta: Path | str, ddl: str = "", solo_lectura: bool = False) -> sqlite3.Connection:
    """Abre una conexión y aplica el DDL si se pasa.

    `check_same_thread=False` porque las escrituras corren en `asyncio.to_thread` y el hilo
    del pool cambia entre llamadas; la exclusión la da el lock del módulo que las envuelve.
    """
    if str(ruta) == ":memory:":
        conexion = sqlite3.connect(":memory:", check_same_thread=False)
        conexion.row_factory = sqlite3.Row
        if ddl:
            conexion.executescript(ddl)
            conexion.commit()
        return conexion
    ruta = Path(ruta)
    if solo_lectura:
        conexion = sqlite3.connect(f"file:{ruta.as_posix()}?mode=ro", uri=True,
                                   check_same_thread=False)
    else:
        ruta.parent.mkdir(parents=True, exist_ok=True)
        conexion = sqlite3.connect(ruta, check_same_thread=False)
    conexion.row_factory = sqlite3.Row
    if ddl:
        conexion.executescript(ddl)
        conexion.commit()
    return conexion
