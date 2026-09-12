"""El índice como artefacto: se empaqueta en una máquina y se descarga en el despliegue.

Construirlo en el arranque haría esperar veinte minutos a la primera consulta y pondría el
healthcheck en carrera contra la indexación; construirlo en el build exigiría la clave del
proveedor dentro de una capa de imagen. El índice se arma donde están las claves, se publica
como un archivo con su sha256, y el servicio lo baja una vez por versión.

Con esto el servicio desplegado nunca habla con csjn.gov.ar ni gasta un embedding al arrancar.
"""

import hashlib
import shutil
import tarfile
import tempfile
from pathlib import Path

import httpx
import zstandard

import app.nucleo.mensajes as msj
from app.nucleo.errores import ErrorRAG
from app.rag.ingesta.indice import ARCHIVO_MANIFIESTO, rutas

TROZO = 1024 * 1024
# Qué artefacto se extrajo en este directorio. Lo escribe la descarga, no el empaquetado: el
# sha256 de un `.tar.zst` no puede vivir adentro del archivo que resume.
ARCHIVO_ARTEFACTO = "ARTEFACTO.sha256"


def empaquetar(directorio: Path | str, destino: Path | str) -> tuple[Path, str]:
    """Comprime el índice en un `.tar.zst` y devuelve su ruta y su sha256."""
    directorio, destino = Path(directorio), Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    compresor = zstandard.ZstdCompressor(level=10)
    with destino.open("wb") as salida, compresor.stream_writer(salida) as flujo:
        with tarfile.open(fileobj=flujo, mode="w|") as tar:
            tar.add(directorio, arcname=".")
    return destino, huella_de(destino)


def huella_de(archivo: Path) -> str:
    """El sha256 de un archivo, leído por trozos."""
    resumen = hashlib.sha256()
    with Path(archivo).open("rb") as f:
        for trozo in iter(lambda: f.read(TROZO), b""):
            resumen.update(trozo)
    return resumen.hexdigest()


def asegurar(directorio: Path | str, url: str | None, huella: str | None) -> bool:
    """Baja y extrae el índice si falta o si su versión no es la que el despliegue espera.

    Devuelve True cuando descargó algo. Sin `url` no hace nada: en desarrollo el índice se
    construye con el script y vive en disco.
    """
    directorio = Path(directorio)
    _, _, ruta_manifiesto = rutas(directorio)
    if ruta_manifiesto.exists() and not _hay_que_actualizar(directorio, huella):
        return False
    if not url:
        return False

    print(msj.MENSAJE_DESCARGANDO_INDICE.format(url=url), flush=True)
    with tempfile.TemporaryDirectory() as temporal:
        paquete = Path(temporal) / "indice.tar.zst"
        _bajar(url, paquete)
        if huella and huella_de(paquete) != huella.removeprefix("sha256:"):
            raise ErrorRAG(
                f"El artefacto de {url} no tiene la huella esperada; no se extrae.")
        extraido = Path(temporal) / "indice"
        _extraer(paquete, extraido)
        if directorio.exists():
            shutil.rmtree(directorio)
        directorio.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(extraido), str(directorio))
    if huella:
        (directorio / ARCHIVO_ARTEFACTO).write_text(
            huella.removeprefix("sha256:"), encoding="utf-8")
    return True


def _hay_que_actualizar(directorio: Path, huella: str | None) -> bool:
    """True cuando el índice en disco no es el artefacto que el entorno pide.

    Lo que se compara es la marca que dejó la descarga anterior. La huella del manifiesto no
    sirve para esto: resume los sha256 de los PDF de origen, así que un arreglo en la
    extracción produce otro índice con la misma huella, y además nunca puede ser igual a un
    sha256 completo. Comparándola, el servicio se bajaba y reextraía el índice entero en cada
    arranque.
    """
    if not huella:
        return False
    marca = directorio / ARCHIVO_ARTEFACTO
    if not marca.exists():
        return True
    return marca.read_text(encoding="utf-8").strip() != huella.removeprefix("sha256:")


def _bajar(url: str, destino: Path) -> None:
    with httpx.stream("GET", url, follow_redirects=True, timeout=600.0) as respuesta:
        respuesta.raise_for_status()
        with destino.open("wb") as salida:
            for trozo in respuesta.iter_bytes(TROZO):
                salida.write(trozo)


def _extraer(paquete: Path, destino: Path) -> None:
    """Extrae el tar comprimido, sin dejar que una entrada escape del destino."""
    destino.mkdir(parents=True, exist_ok=True)
    descompresor = zstandard.ZstdDecompressor()
    with paquete.open("rb") as entrada, descompresor.stream_reader(entrada) as flujo:
        with tarfile.open(fileobj=flujo, mode="r|") as tar:
            # `filter="data"` rechaza rutas absolutas, `..` y enlaces: el artefacto es propio,
            # y extraer un tar sin ese control es cómo se escribe fuera del destino.
            tar.extractall(destino, filter="data")
    if not (destino / ARCHIVO_MANIFIESTO).exists():
        raise ErrorRAG(f"El artefacto no trae {ARCHIVO_MANIFIESTO}.")
