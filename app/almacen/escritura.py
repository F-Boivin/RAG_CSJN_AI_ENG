"""Escritura del índice léxico. La usa la ingesta, y los tests para armar un índice chico.

El padrón, las subsecciones y las citas por subsección se calculan acá, una vez, mientras se
indexa. En consulta solo se leen. Es lo que evita que el servicio tenga que recorrer el corpus
entero en cada arranque.

La unidad de trabajo es el documento: `borrar_documento` seguido de `escribir_fragmentos`
rehace uno solo sin tocar los demás, que es lo que hace la ingesta incremental.
"""

import sqlite3
from pathlib import Path

import app.nucleo.mensajes as msj
from app.almacen.esquema import DDL_LEXICO, conectar
from app.nucleo.errores import ErrorDeAlmacenamiento
from app.rag import citas as c


def claves_de_padron(citas_urls: dict) -> dict[str, str]:
    """Las citas de un fragmento con la clave que el padrón usa: "tomo:pagina".

    **El padrón se lee siempre por la forma normalizada** —`existe`, `link` y el evento de cita
    pasan por `normalizar_cita`—, así que guardarlo con la clave cruda deja la entrada
    inalcanzable. Cada fuente escribe la cita a su manera: los hipervínculos de los PDF ya
    vienen normalizados, pero el cuadernillo en markdown la escribía como la imprimió
    («Fallos: 112:384»).
    Medido sobre el índice construido antes de este arreglo: 557 claves sin normalizar, y 330
    de ellas sin gemela normalizada, o sea 330 citas reales del corpus que el verificador daba
    por inventadas y que, de pasar, se publicaban sin link.

    Lo que no trae tomo y página queda afuera, que es lo que el padrón declara ser. Por esa
    puerta entraban 86 entradas que no son citas: números de expediente
    («FALLO M. 655. XLIX. REX»), nombres de caso y etiquetas de voto
    («(Disidencia del juez Lorenzetti)»), texto de anclas que el corpus no puede respaldar.

    Ante dos formas de la misma cita gana la que trae URL: la vacía no pisa a la que enlaza.
    """
    normalizadas: dict[str, str] = {}
    for cita, url in (citas_urls or {}).items():
        clave = c.normalizar_cita(cita)
        if not clave:
            continue
        if url or clave not in normalizadas:
            normalizadas[clave] = url or normalizadas.get(clave, "")
    return normalizadas


class EscritorLexico:
    """El índice léxico abierto para escritura."""

    def __init__(self, ruta: Path | str):
        self.ruta = ruta
        self._conexion = conectar(ruta, ddl=DDL_LEXICO)

    def cerrar(self) -> None:
        self._conexion.commit()
        self._conexion.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.cerrar()

    def borrar_documento(self, origen: str) -> int:
        """Saca del índice todo lo que aportó un documento. Devuelve cuántos fragmentos eran.

        Una cita que este documento compartía con otro vuelve al padrón cuando ese otro se
        reindexe; mientras tanto queda registrada a nombre del que la conserva. El orden de los
        DELETE importa: `citas_por_subseccion` se resuelve contra `subsecciones`, así que se
        borra antes.
        """
        try:
            ids = [f["rowid"] for f in self._conexion.execute(
                "SELECT rowid FROM fragmentos WHERE origen = ?", (origen,)).fetchall()]
            self._conexion.executemany("DELETE FROM fts WHERE rowid = ?", [(i,) for i in ids])
            self._conexion.execute(
                "DELETE FROM citas_por_subseccion WHERE subseccion IN "
                "(SELECT nombre FROM subsecciones WHERE origen = ?)", (origen,))
            self._conexion.execute("DELETE FROM subsecciones WHERE origen = ?", (origen,))
            self._conexion.execute("DELETE FROM fragmentos WHERE origen = ?", (origen,))
            self._conexion.execute("DELETE FROM padron WHERE origen = ?", (origen,))
            self._conexion.commit()
        except sqlite3.Error as exc:
            raise ErrorDeAlmacenamiento(msj.ERROR_ALMACEN.format(detalle=exc)) from exc
        return len(ids)

    def escribir_fragmentos(self, fragmentos: list[dict]) -> None:
        """Escribe los fragmentos de un documento y actualiza padrón, FTS y subsecciones.

        Cada fragmento es un dict con `id`, `texto` y su metadata. `citas_urls` viaja como
        dict, y de ahí salen las tres tablas derivadas.
        """
        try:
            for f in fragmentos:
                citas_urls = claves_de_padron(f.get("citas_urls") or {})
                cursor = self._conexion.execute(
                    "INSERT OR REPLACE INTO fragmentos "
                    "(id, origen, seccion, subseccion, fuente, pagina, texto) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (f["id"], f["origen"], f.get("seccion", ""), f.get("subseccion", ""),
                     f.get("fuente", ""), f.get("pagina"), f["texto"]),
                )
                self._conexion.execute(
                    "INSERT INTO fts (rowid, texto, citas, titulo) VALUES (?, ?, ?, ?)",
                    (cursor.lastrowid, f["texto"], " ".join(sorted(citas_urls)),
                     f"{f.get('seccion', '')} {f.get('subseccion', '')}".strip()),
                )
                for cita, url in citas_urls.items():
                    self._conexion.execute(
                        "INSERT INTO padron (cita, url, origen) VALUES (?, ?, ?) "
                        "ON CONFLICT(cita) DO UPDATE SET "
                        "url = CASE WHEN padron.url = '' THEN excluded.url ELSE padron.url END",
                        (cita, url, f["origen"]),
                    )
                    if f.get("subseccion"):
                        self._conexion.execute(
                            "INSERT OR IGNORE INTO citas_por_subseccion (subseccion, cita) "
                            "VALUES (?, ?)", (f["subseccion"], cita),
                        )
                if f.get("subseccion"):
                    self._conexion.execute(
                        "INSERT INTO subsecciones (nombre, clave, origen, fragmentos) "
                        "VALUES (?, ?, ?, 1) ON CONFLICT(nombre) DO UPDATE SET "
                        "fragmentos = subsecciones.fragmentos + 1",
                        (f["subseccion"], c.clave_subseccion(f["subseccion"]), f["origen"]),
                    )
            self._conexion.commit()
        except sqlite3.Error as exc:
            raise ErrorDeAlmacenamiento(msj.ERROR_ALMACEN.format(detalle=exc)) from exc

    def registrar_documento(self, ficha: dict) -> None:
        """Deja la ficha del documento indexado: qué es, de dónde salió y cómo se segmentó."""
        try:
            self._conexion.execute(
                "INSERT OR REPLACE INTO documentos "
                "(origen, tipo, titulo, categoria, url, sha256, paginas, fragmentos, "
                " metodo_subsecciones, actualizado) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ficha["origen"], ficha.get("tipo", ""), ficha.get("titulo", ""),
                 ficha.get("categoria", ""), ficha.get("url", ""), ficha.get("sha256", ""),
                 ficha.get("paginas", 0), ficha.get("fragmentos", 0),
                 ficha.get("metodo_subsecciones", ""), ficha.get("actualizado", "")),
            )
            self._conexion.commit()
        except sqlite3.Error as exc:
            raise ErrorDeAlmacenamiento(msj.ERROR_ALMACEN.format(detalle=exc)) from exc

    def completar_links_faltantes(self) -> int:
        """Le pone link a las citas del padrón que ningún PDF enlazó.

        Trece documentos del corpus —la serie Ambiental, las ediciones viejas de Fallos
        Relevantes, «Citas de doctrina»— no traen un solo hipervínculo: sus citas viven solo
        en el texto. La URL se arma con la plantilla oficial a partir del tomo y la página que
        el corpus escribió, por código y sin que ningún modelo intervenga.
        """
        try:
            sin_link = [f["cita"] for f in self._conexion.execute(
                "SELECT cita FROM padron WHERE url = ''").fetchall()]
            self._conexion.executemany(
                "UPDATE padron SET url = ? WHERE cita = ?",
                [(c.link_de_cita(cita), cita) for cita in sin_link],
            )
            self._conexion.commit()
        except sqlite3.Error as exc:
            raise ErrorDeAlmacenamiento(msj.ERROR_ALMACEN.format(detalle=exc)) from exc
        return len(sin_link)

    def optimizar(self) -> None:
        """Compacta el índice FTS5 y recupera el espacio libre del archivo."""
        self._conexion.execute("INSERT INTO fts(fts) VALUES('optimize')")
        self._conexion.commit()
        self._conexion.execute("VACUUM")
