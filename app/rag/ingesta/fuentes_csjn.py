"""Catálogo y descarga de las dos fuentes de la Secretaría de Jurisprudencia.

El sitio es una SPA: `/homeSJ/notas/inicia` y `/homeSJ/suplementos/inicia` son rutas que el
JavaScript reescribe en la barra de direcciones, y los datos viajan por AJAX contra endpoints
JSON. Los documentos son PDF servidos directo, así que la ingesta es HTTP y nada más.

```
GET /homeSJ/notas/                                  → [{id, titulo, categoria, ...}]
GET /homeSJ/notas/nota/{id}/documento               → application/pdf
GET /homeSJ/suplementos/categoria/{cat}/suplementos → [{id, titulo, categoria, ...}]
GET /homeSJ/suplementos/suplemento/{id}/documento   → application/pdf
```

Esto corre en la máquina de quien construye el índice. El servicio desplegado nunca habla con
csjn.gov.ar: recibe el índice ya armado.
"""

import hashlib
import json
import re
import time
from pathlib import Path

import httpx

import app.nucleo.constantes as cfg
import app.nucleo.mensajes as msj
from app.nucleo.errores import ErrorRAG

PATRON_CATEGORIA = re.compile(r"buscarSuplementos\('(\d+)'\)")
MAGIA_PDF = b"%PDF-"


class ClienteSJ:
    """Cliente HTTP de la Secretaría, con la cortesía que corresponde a un sitio público.

    Un throttle de una petición por segundo, reintento con backoff, y User-Agent de navegador
    porque el sitio responde una interstitial a los clientes que no lo declaran. La sesión se
    abre pidiendo la home: los endpoints JSON esperan la cookie que instala esa visita.
    """

    def __init__(self, cliente: httpx.Client | None = None):
        self._cliente = cliente or httpx.Client(
            timeout=cfg.TIMEOUT_DESCARGA,
            follow_redirects=True,
            headers={"User-Agent": cfg.AGENTE_HTTP},
        )
        self._ultimo = 0.0
        self._sesion_abierta = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.cerrar()

    def cerrar(self) -> None:
        self._cliente.close()

    def _esperar(self) -> None:
        pausa = cfg.SEGUNDOS_ENTRE_DESCARGAS - (time.monotonic() - self._ultimo)
        if pausa > 0:
            time.sleep(pausa)
        self._ultimo = time.monotonic()

    def _abrir_sesion(self) -> None:
        if not self._sesion_abierta:
            self._pedir(cfg.URL_HOME_SJ)
            self._sesion_abierta = True

    def _pedir(self, url: str) -> httpx.Response:
        """Un GET con reintento. El último error sube como ErrorRAG con la URL adentro."""
        ultimo = None
        for intento in range(cfg.REINTENTOS_DESCARGA):
            self._esperar()
            try:
                respuesta = self._cliente.get(url)
                if respuesta.status_code < 500 and respuesta.status_code != 429:
                    return respuesta
                ultimo = f"HTTP {respuesta.status_code}"
            except httpx.HTTPError as exc:
                ultimo = f"{type(exc).__name__}: {exc}"
            time.sleep(2 ** intento)
        raise ErrorRAG(msj.ERROR_DESCARGA.format(url=url, detalle=ultimo))

    # --- Catálogo ---

    def categorias(self) -> dict[int, str]:
        """Las categorías de suplementos, leídas del HTML de la home.

        Se comparan contra `CATEGORIAS_SUPLEMENTOS` y la discrepancia corta la ingesta: una
        categoría nueva que nadie mire significa indexar de menos, y en silencio.
        """
        self._abrir_sesion()
        html = self._pedir(cfg.URL_HOME_SJ).text
        del_sitio = {int(x) for x in PATRON_CATEGORIA.findall(html)}
        declaradas = set(cfg.CATEGORIAS_SUPLEMENTOS)
        if del_sitio and del_sitio != declaradas:
            raise ErrorRAG(msj.ERROR_CATEGORIAS_DISCREPAN.format(
                sitio=sorted(del_sitio), declaradas=sorted(declaradas)))
        return dict(cfg.CATEGORIAS_SUPLEMENTOS)

    def catalogar(self) -> list[dict]:
        """Las 124 entradas del catálogo: las 82 notas y los 42 suplementos.

        Cada entrada dice si va al índice. Los 15 tomos del Archivo Histórico entran al
        catálogo con `indexar: False`: son transcripciones sin un solo hipervínculo, y
        sumarlos más adelante es cambiar esa bandera.
        """
        self._abrir_sesion()
        entradas = []
        for nota in self._pedir(cfg.URL_NOTAS).json():
            entradas.append({
                "origen": f"nota-{nota['id']}",
                "tipo": "nota",
                "id": nota["id"],
                "titulo": (nota.get("titulo") or "").strip(),
                "categoria": "Notas de jurisprudencia",
                "url": cfg.URL_NOTA_PDF.format(id=nota["id"]),
                "indexar": True,
            })
        for numero, nombre in self.categorias().items():
            url = cfg.URL_SUPLEMENTOS_CATEGORIA.format(categoria=numero)
            for suplemento in self._pedir(url).json():
                entradas.append({
                    "origen": f"suplemento-{suplemento['id']}",
                    "tipo": "suplemento",
                    "id": suplemento["id"],
                    "titulo": (suplemento.get("titulo") or "").strip(),
                    "categoria": nombre,
                    "url": cfg.URL_SUPLEMENTO_PDF.format(id=suplemento["id"]),
                    "indexar": numero not in cfg.CATEGORIAS_EXCLUIDAS,
                })
        return entradas

    # --- Descarga ---

    def descargar_pdf(self, url: str) -> bytes:
        """El PDF de un documento, comprobado.

        El sitio devuelve HTML de error con status 200, así que el content-type y los primeros
        bytes se controlan siempre: un HTML indexado como si fuera doctrina sería peor que una
        descarga fallida.
        """
        self._abrir_sesion()
        respuesta = self._pedir(url)
        tipo = respuesta.headers.get("content-type", "")
        cuerpo = respuesta.content
        if "pdf" not in tipo.lower() or not cuerpo.startswith(MAGIA_PDF):
            raise ErrorRAG(msj.ERROR_NO_ES_PDF.format(
                url=url, estado=respuesta.status_code, tipo=tipo or "(ninguno)"))
        return cuerpo


def guardar_catalogo(entradas: list[dict], ruta: Path) -> None:
    """Escribe el catálogo ordenado, para que su diff en git sea legible."""
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ordenadas = sorted(entradas, key=lambda e: (e["tipo"], e["id"]))
    ruta.write_text(json.dumps(ordenadas, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def leer_catalogo(ruta: Path) -> list[dict]:
    """El catálogo del repositorio, o el aviso de cómo armarlo."""
    if not ruta.exists():
        raise ErrorRAG(msj.ERROR_CATALOGO_AUSENTE.format(ruta=ruta))
    return json.loads(ruta.read_text(encoding="utf-8"))


def huella(datos: bytes) -> str:
    """El sha256 de un PDF. Los endpoints no exponen fecha ni versión, así que el contenido
    es la única señal de que un documento cambió."""
    return hashlib.sha256(datos).hexdigest()
