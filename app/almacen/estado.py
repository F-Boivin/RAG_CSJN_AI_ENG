"""El estado del servicio: cupos, gasto y registro de consultas.

Es lo único irreemplazable del despliegue, y por eso vive en un SQLite aparte del índice: el
índice se reconstruye con un script, esto no.

Un solo proceso escribe, así que la contención es nula; igual cada escritura va bajo un lock
de módulo y `BEGIN IMMEDIATE`, porque las corridas concurrentes comparten el event loop y una
cuota cobrada dos veces sería un cupo regalado. Las llamadas se hacen desde
`asyncio.to_thread`: `sqlite3` es sincrónico y bloquearía el loop.

Los cupos usan una ventana rodante de 24 h, más justa que el día calendario y sin el reinicio
de medianoche que se puede esperar. El techo global sí es calendario, porque es un techo de
presupuesto.
"""

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import app.nucleo.constantes as cfg
import app.nucleo.mensajes as msj
from app.almacen.esquema import DDL_ESTADO, conectar
from app.nucleo.errores import ErrorDeAlmacenamiento

_lock = threading.Lock()


def ahora() -> datetime:
    return datetime.now(timezone.utc)


class Estado:
    """Cupos, gasto y registro sobre un SQLite."""

    def __init__(self, ruta: Path | str):
        self.ruta = ruta
        self._conexion = conectar(ruta, ddl=DDL_ESTADO)

    def cerrar(self) -> None:
        self._conexion.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.cerrar()

    # --- Cupos por ventana rodante ---

    def usos(self, clave: str, tipo: str, horas: int = None) -> int:
        """Cuántos pulsos de ese tipo lleva la clave en la ventana."""
        desde = (ahora() - timedelta(hours=horas or cfg.HORAS_VENTANA_CUOTA)).isoformat()
        with _lock:
            fila = self._conexion.execute(
                "SELECT count(*) AS n FROM pulsos WHERE clave = ? AND tipo = ? AND ts > ?",
                (clave, tipo, desde),
            ).fetchone()
        return fila["n"]

    def se_repone(self, clave: str, tipo: str, horas: int = None) -> datetime | None:
        """Cuándo vuelve a haber cupo: 24 h después del pulso más viejo de la ventana."""
        horas = horas or cfg.HORAS_VENTANA_CUOTA
        desde = (ahora() - timedelta(hours=horas)).isoformat()
        with _lock:
            fila = self._conexion.execute(
                "SELECT min(ts) AS primero FROM pulsos "
                "WHERE clave = ? AND tipo = ? AND ts > ?",
                (clave, tipo, desde),
            ).fetchone()
        if not fila or not fila["primero"]:
            return None
        return datetime.fromisoformat(fila["primero"]) + timedelta(hours=horas)

    def anotar(self, clave: str, tipo: str) -> None:
        """Deja el pulso que consume cupo."""
        self._escribir("INSERT INTO pulsos (clave, tipo, ts) VALUES (?, ?, ?)",
                       (clave, tipo, ahora().isoformat()))

    def reservar(self, clave: str, tipo: str, tope: int, horas: int = None) -> bool:
        """Cuenta y anota **en la misma transacción**. Devuelve si había cupo.

        Contar con `usos` y anotar con `anotar` son dos operaciones, cada una con su lock, y
        entre las dos el servicio hace `await`: dos pedidos en paralelo contaban los dos el
        mismo número, los dos lo encontraban por debajo del tope, y los dos cobraban. Los topes
        se pasaban disparando pedidos a la vez, que es justamente lo que hace quien los quiere
        pasar. Acá la cuenta y el pulso van adentro del mismo `BEGIN IMMEDIATE`, así que el
        segundo ve al primero.
        """
        horas = horas or cfg.HORAS_VENTANA_CUOTA
        desde = (ahora() - timedelta(hours=horas)).isoformat()
        with _lock:
            try:
                self._conexion.execute("BEGIN IMMEDIATE")
                usados = self._conexion.execute(
                    "SELECT count(*) AS n FROM pulsos WHERE clave = ? AND tipo = ? AND ts > ?",
                    (clave, tipo, desde),
                ).fetchone()["n"]
                if usados >= tope:
                    self._conexion.rollback()
                    return False
                self._conexion.execute(
                    "INSERT INTO pulsos (clave, tipo, ts) VALUES (?, ?, ?)",
                    (clave, tipo, ahora().isoformat()))
                self._conexion.commit()
                return True
            except sqlite3.Error as exc:
                self._conexion.rollback()
                raise ErrorDeAlmacenamiento(msj.ERROR_ALMACEN.format(detalle=exc)) from exc

    def respaldar(self, destino: Path | str) -> int:
        """Copia consistente de la base, con el servicio andando. Devuelve los bytes escritos.

        Usa la API de respaldo en caliente de SQLite y no `copy`: la base va en WAL, así que
        el archivo `.sqlite3` solo no es la base —lo último escrito vive en el `-wal`— y
        copiarlo a mano da un respaldo que puede faltarle lo más reciente, justo lo que se
        quería guardar. `backup()` recorre las páginas tomando el bloqueo que hace falta, sin
        frenar al servicio.
        """
        destino = Path(destino)
        destino.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            try:
                copia = sqlite3.connect(destino)
                try:
                    self._conexion.backup(copia)
                finally:
                    copia.close()
            except sqlite3.Error as exc:
                raise ErrorDeAlmacenamiento(msj.ERROR_ALMACEN.format(detalle=exc)) from exc
        return destino.stat().st_size

    def reservar_dia(self, tope: int, dia: str = None) -> bool:
        """Lo mismo para el techo diario del sitio, que vive en otra tabla."""
        dia = dia or ahora().date().isoformat()
        with _lock:
            try:
                self._conexion.execute("BEGIN IMMEDIATE")
                fila = self._conexion.execute(
                    "SELECT consultas FROM cuota_global WHERE dia = ?", (dia,)).fetchone()
                if (fila["consultas"] if fila else 0) >= tope:
                    self._conexion.rollback()
                    return False
                self._conexion.execute(
                    "INSERT INTO cuota_global (dia, consultas) VALUES (?, 1) "
                    "ON CONFLICT(dia) DO UPDATE SET consultas = cuota_global.consultas + 1",
                    (dia,))
                self._conexion.commit()
                return True
            except sqlite3.Error as exc:
                self._conexion.rollback()
                raise ErrorDeAlmacenamiento(msj.ERROR_ALMACEN.format(detalle=exc)) from exc

    def consultas_del_dia(self, dia: str = None) -> int:
        dia = dia or ahora().date().isoformat()
        with _lock:
            fila = self._conexion.execute(
                "SELECT consultas FROM cuota_global WHERE dia = ?", (dia,)).fetchone()
        return fila["consultas"] if fila else 0

    def anotar_consulta_global(self, dia: str = None) -> int:
        """Suma una al techo diario y devuelve el total."""
        dia = dia or ahora().date().isoformat()
        self._escribir(
            "INSERT INTO cuota_global (dia, consultas) VALUES (?, 1) "
            "ON CONFLICT(dia) DO UPDATE SET consultas = cuota_global.consultas + 1", (dia,))
        return self.consultas_del_dia(dia)

    def podar(self) -> int:
        """Borra los pulsos vencidos y las consultas más viejas que la retención declarada."""
        limite_pulsos = (ahora() - timedelta(hours=cfg.HORAS_RETENCION_PULSOS)).isoformat()
        limite_consultas = (ahora() - timedelta(days=cfg.DIAS_RETENCION_CONSULTAS)).isoformat()
        with _lock:
            try:
                cursor = self._conexion.execute("DELETE FROM pulsos WHERE ts <= ?",
                                                (limite_pulsos,))
                borrados = cursor.rowcount
                cursor = self._conexion.execute("DELETE FROM consultas WHERE recibida_en <= ?",
                                                (limite_consultas,))
                borrados += cursor.rowcount
                self._conexion.commit()
            except sqlite3.Error as exc:
                raise ErrorDeAlmacenamiento(msj.ERROR_ALMACEN.format(detalle=exc)) from exc
        return borrados

    # --- Gasto ---

    def sumar_gasto(self, usd: float, llamadas: int, tokens_in: int, tokens_out: int,
                    dia: str = None) -> None:
        dia = dia or ahora().date().isoformat()
        self._escribir(
            "INSERT INTO gasto (dia, usd, llamadas, tokens_in, tokens_out) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(dia) DO UPDATE SET "
            "usd = gasto.usd + excluded.usd, llamadas = gasto.llamadas + excluded.llamadas, "
            "tokens_in = gasto.tokens_in + excluded.tokens_in, "
            "tokens_out = gasto.tokens_out + excluded.tokens_out",
            (dia, usd, llamadas, tokens_in, tokens_out))

    def gasto_del_mes(self, mes: str = None) -> float:
        """Los dólares gastados en el mes en curso."""
        mes = mes or ahora().strftime("%Y-%m")
        with _lock:
            fila = self._conexion.execute(
                "SELECT coalesce(sum(usd), 0.0) AS total FROM gasto WHERE dia LIKE ?",
                (f"{mes}-%",)).fetchone()
        return float(fila["total"])

    # --- Registro de consultas ---

    def registrar(self, fila: dict) -> None:
        """Guarda o actualiza el registro de una consulta.

        El texto llega ya anonimizado: quien registra no decide qué se guarda, eso lo resuelve
        `servicio.registro.anonimizar` antes.
        """
        columnas = (
            "id", "recibida_en", "consulta", "admitida", "motivo_inadmision", "desenlace",
            "respuesta", "citas", "intentos", "vueltas", "citas_propuestas",
            "citas_verificadas", "citas_inexistentes", "senales", "duracion_ms", "usd",
            "modelo", "version_indice", "error",
        )
        valores = [fila.get(c) for c in columnas]
        marcas = ", ".join("?" for _ in columnas)
        self._escribir(
            f"INSERT OR REPLACE INTO consultas ({', '.join(columnas)}) VALUES ({marcas})",
            tuple(valores))

    def leer_consulta(self, consulta_id: str) -> dict | None:
        with _lock:
            fila = self._conexion.execute(
                "SELECT * FROM consultas WHERE id = ?", (consulta_id,)).fetchone()
        return dict(fila) if fila else None

    def metricas(self) -> dict:
        """Los agregados públicos: volumen, tasa de publicación y latencia.

        Agregados y nunca el listado crudo. El registro no lleva ningún identificador de
        persona, y publicar promedios es lo que esa decisión hace seguro.
        """
        with _lock:
            fila = self._conexion.execute(
                "SELECT count(*) AS total, "
                "       sum(desenlace = ?) AS publicadas, "
                "       sum(desenlace = ?) AS sin_base, "
                "       sum(admitida = 0) AS fuera_de_alcance, "
                "       coalesce(avg(nullif(duracion_ms, 0)), 0) AS latencia_ms "
                "FROM consultas", (cfg.PUBLICADA, cfg.SIN_BASE)).fetchone()
            gasto = self._conexion.execute(
                "SELECT coalesce(sum(usd), 0.0) AS usd FROM gasto").fetchone()
        return {
            "consultas": fila["total"] or 0,
            "publicadas": fila["publicadas"] or 0,
            "sin_base": fila["sin_base"] or 0,
            "fuera_de_alcance": fila["fuera_de_alcance"] or 0,
            "latencia_ms": round(fila["latencia_ms"] or 0),
            "gasto_usd": round(float(gasto["usd"]), 4),
        }

    # --- Interno ---

    def _escribir(self, sql: str, parametros: tuple) -> None:
        with _lock:
            try:
                self._conexion.execute("BEGIN IMMEDIATE")
                self._conexion.execute(sql, parametros)
                self._conexion.commit()
            except sqlite3.Error as exc:
                self._conexion.rollback()
                raise ErrorDeAlmacenamiento(msj.ERROR_ALMACEN.format(detalle=exc)) from exc
