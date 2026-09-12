"""Baja una copia de `estado.sqlite3` del servicio desplegado.

    python -m scripts.respaldar https://el-buscador.up.railway.app
    python -m scripts.respaldar https://... --destino C:\\respaldos

Es lo único irreemplazable del despliegue: el índice se reconstruye con `construir_indice` y
esto no. Adentro están el registro de consultas —el tercer objetivo del proyecto—, los cupos y
el gasto acumulado del mes.

El token va en `TOKEN_RESPALDO`, la misma variable que el servicio tiene cargada. Sin ella el
endpoint contesta 404, así que esto no sirve para hurgar en un servicio ajeno.

La copia la hace el servicio con la API de respaldo en caliente de SQLite, no copiando el
archivo: la base va en WAL, y el `.sqlite3` solo puede no tener lo último escrito.
"""

import argparse
import os
import sys
from pathlib import Path

import httpx

from app.nucleo.config import cargar_entorno


def main(argv=None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("url", help="la raíz del servicio, sin barra final")
    parser.add_argument("--destino", default=".",
                        help="dónde dejar el archivo (por defecto, el directorio actual)")
    parser.add_argument("--nombre", default="",
                        help="nombre del archivo; por defecto lleva la fecha del servidor")
    args = parser.parse_args(argv)

    cargar_entorno(exigir_clave=False)
    token = (os.environ.get("TOKEN_RESPALDO") or "").strip()
    if not token:
        print("Falta TOKEN_RESPALDO. Es el mismo valor que el servicio tiene cargado.")
        return 1

    destino = Path(args.destino)
    destino.mkdir(parents=True, exist_ok=True)
    try:
        with httpx.stream("GET", args.url.rstrip("/") + "/respaldo", timeout=300.0,
                          follow_redirects=True,
                          headers={"X-Token-Respaldo": token}) as respuesta:
            if respuesta.status_code == 404:
                print("El servicio contestó 404: el token no coincide, o no tiene "
                      "TOKEN_RESPALDO cargado.")
                return 1
            respuesta.raise_for_status()
            # La fecha la pone el servidor, así dos respaldos del mismo día no se pisan.
            fecha = (respuesta.headers.get("date") or "").replace(",", "").replace(" ", "-")
            archivo = destino / (args.nombre or f"estado-{fecha or 'respaldo'}.sqlite3")
            with archivo.open("wb") as salida:
                for trozo in respuesta.iter_bytes(1024 * 1024):
                    salida.write(trozo)
    except httpx.HTTPError as exc:
        print(f"No se pudo bajar el respaldo: {exc}")
        return 1

    print(f"respaldo: {archivo} · {archivo.stat().st_size / 1048576:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
