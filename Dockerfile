# Una sola imagen: la API con el grafo adentro. Sin worker y sin Redis.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Las tres rutas de datos apuntan al volumen, acá y no solo en el panel del proveedor. Los
# defaults del codigo son los de desarrollo y caen en RAIZ/datos, que adentro de la imagen es
# /srv/datos: existe, es escribible, y muere con el contenedor. Eso no falla —el servicio
# arranca igual— y cada despliegue se lleva los cupos, el registro y el gasto del mes, con lo
# que el techo de presupuesto pasa a ser por contenedor en vez de mensual. Una variable que
# falta y cuesta plata en silencio es el peor default posible, asi que la imagen la trae.
ENV DIRECTORIO_INDICE=/datos/indice \
    DIRECTORIO_CACHE=/datos/cache \
    ARCHIVO_ESTADO=/datos/estado.sqlite3

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/
COPY catalogo.json .

# El volumen se monta aca. El mkdir previo hace que herede el dueño correcto cuando el volumen
# es de Docker; cuando lo monta una plataforma, el dueño lo decide ella y por eso existe el
# entrypoint.
RUN mkdir -p /datos && \
    useradd --create-home --uid 10001 buscador && \
    chown -R buscador:buscador /srv /datos

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 8000

# El contenedor arranca como root y el entrypoint baja a `buscador` despues de dejar el volumen
# escribible. Sin eso, la plataforma monta /datos como root:root y el servicio muere antes de
# abrir el puerto: `unable to open database file`.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

# Forma shell porque ${PORT} no se expande en la forma JSON. --workers 1 es parte del diseño:
# los contadores de cupo y el registro de corridas viven en memoria del proceso.
CMD ["sh", "-c", "uvicorn app.api.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
