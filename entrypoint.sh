#!/bin/sh
# Arranca como root, deja el volumen escribible, y baja privilegios antes de ejecutar.
#
# El volumen persistente lo monta la plataforma, y su dueño lo decide ella: Railway lo entrega
# como root:root con modo 755, así que un proceso no-root no puede escribir adentro. El
# servicio moría en el arranque con `sqlite3.OperationalError: unable to open database file`
# antes de abrir el puerto. Un volumen nombrado de Docker no reproduce el problema —hereda el
# dueño del directorio de la imagen—, así que esto solo se ve contra la plataforma de verdad.
#
# La alternativa era correr todo como root, que es lo que hace la mayoría de los ejemplos de
# Railway. No: el proceso que atiende internet no necesita ser root, y bajar privilegios acá
# cuesta dos líneas. Root vive lo que tarda un `chown`.
set -e

USUARIO=buscador
GRUPO=buscador

for ruta in "$DIRECTORIO_INDICE" "$DIRECTORIO_CACHE" "$(dirname "$ARCHIVO_ESTADO")"; do
    [ -n "$ruta" ] || continue
    mkdir -p "$ruta"
done

# Solo el punto de montaje: recorrer los 436 MB del índice en cada arranque costaría segundos
# y no hace falta, porque lo que se extrae adentro ya lo escribe el usuario correcto.
if [ -d /datos ]; then
    chown "$USUARIO:$GRUPO" /datos 2>/dev/null || true
    find /datos -maxdepth 1 -mindepth 1 ! -user "$USUARIO" -exec chown -R "$USUARIO:$GRUPO" {} + 2>/dev/null || true
fi

exec setpriv --reuid="$USUARIO" --regid="$GRUPO" --init-groups "$@"
