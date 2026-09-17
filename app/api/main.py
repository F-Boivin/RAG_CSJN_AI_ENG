"""La API del buscador: un servicio, el grafo adentro, y el avance por SSE.

`POST /consultas` y `GET /consultas/{id}/stream` van separados por tres razones: `EventSource`
del navegador solo hace GET; el cupo se cobra en un único lugar; y una reconexión vuelve a leer
sin volver a cobrar ni a ejecutar.

El stream sale con `Cache-Control: no-cache` y `X-Accel-Buffering: no`, y `sse-starlette` manda
un latido cada 15 s. Los proxies de borde bufferean por defecto y cortan a los 5 minutos sin
transferencia; es la falla que solo aparece en producción.

Un proceso, un worker, una réplica: los contadores de cupo y el registro de corridas viven en
memoria del proceso.
"""

import asyncio
import json
import os
import secrets
import tempfile
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from sse_starlette.sse import EventSourceResponse

import app.nucleo.constantes as cfg
import app.nucleo.mensajes as msj
from app.almacen.estado import Estado
from app.api import limites
from app.grafo.construccion import crear_grafo
from app.nucleo.config import cargar_entorno, obtener_ajustes
from app.nucleo.errores import ErrorDeAlmacenamiento, ErrorRAG
from app.observabilidad import langsmith as observabilidad
from app.rag import herramientas
from app.rag.ingesta import artefacto
from app.rag.ingesta import indice as indice_rag
from app.servicio import admision, registro
from app.servicio.costo import Contador
from app.servicio.motor import Motor

WEB = Path(__file__).parent / "web"


class ConsultaEntrante(BaseModel):
    """Lo que manda quien pregunta."""

    model_config = ConfigDict(extra="forbid")

    consulta: str = Field(min_length=8, max_length=cfg.LARGO_MAXIMO_CONSULTA)


async def _preparar_indice(app: FastAPI, ajustes) -> None:
    """Deja el índice y el motor listos, en segundo plano.

    **Corre fuera del arranque a propósito.** Mientras el lifespan no termina, uvicorn no abre
    el socket: bajar acá los ~200 MB del artefacto dejaba el puerto cerrado hasta diez minutos
    —el timeout de la descarga—, y el healthcheck de la plataforma se rinde mucho antes. El
    despliegue se marcaba fallido, la plataforma reintentaba, y el contenedor nuevo volvía a
    bajar el artefacto desde cero.

    Atrapa `Exception` y no una lista de tipos. Es una tarea de fondo: lo que no se atrape acá
    no lo ve nadie, y la lista que había dejaba afuera todo lo que la descarga puede tirar
    —`HTTPError`, `HTTPStatusError`, `ConnectError`, `ZstdError`, `TarError`, ninguna es
    `OSError`—, con lo que un Release movido de lugar mataba el proceso en vez de dejarlo
    servir la interfaz con el aviso.
    """
    try:
        # Si el volumen está vacío o trae otra versión, el índice se baja del artefacto
        # publicado. Es lo único que el servicio descarga, y una vez por versión.
        await asyncio.to_thread(artefacto.asegurar, ajustes.directorio_indice,
                                ajustes.indice_url, ajustes.indice_huella)
        indice = await asyncio.to_thread(indice_rag.abrir, ajustes.directorio_indice)
        await asyncio.to_thread(herramientas.inicializar, indice.chroma, indice.lexico)
        app.state.indice = indice
        app.state.motor = Motor(crear_grafo(), indice, app.state.estado)
        app.state.indice_estado = "listo"
        print(msj.MENSAJE_INDICE_DESCARGADO.format(
            ruta=ajustes.directorio_indice, fragmentos=indice.fragmentos,
            huella=indice.version), flush=True)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        app.state.indice_estado = "ausente"
        app.state.error_indice = str(exc)
        print(f"AVISO: {exc}", flush=True)


@asynccontextmanager
async def ciclo_de_vida(app: FastAPI):
    """Deja el proceso atendiendo, y prepara el índice en paralelo.

    El índice se abre, no se construye: construirlo ataría el arranque a tener la clave del
    proveedor y a que la fuente esté disponible. Si falta, `/salud` lo dice y el servicio
    contesta la interfaz igual, en vez de morir en un bucle de reinicios.
    """
    ajustes = await asyncio.to_thread(cargar_entorno)
    # Antes que nada y sin red de contención: sin sal, los hashes de cupo se revierten a la IP
    # del visitante y la página de privacidad estaría mintiendo. Un servicio que no puede
    # cumplir lo que promete no arranca.
    ajustes.sal_de_visitante()
    await asyncio.to_thread(observabilidad.instrumentar, ajustes.proyecto_langsmith)

    # Dónde viven los datos, en el log del arranque. Un `ARCHIVO_ESTADO` que apunta adentro de
    # la imagen en vez del volumen montado no falla: el archivo se crea, el servicio anda, y
    # cada despliegue se lleva los cupos, el registro y el gasto acumulado del mes.
    print(f"índice en {ajustes.directorio_indice} · estado en {ajustes.archivo_estado}",
          flush=True)
    # Y con qué usuario corre. `entrypoint.sh` baja a `buscador` después de dejar el volumen
    # escribible, y un `startCommand` en la configuración de la plataforma lo saltea sin que
    # nada falle: Railway documenta que el start command reemplaza el ENTRYPOINT, el proceso
    # queda como root y todo anda igual. Esta línea es lo único que lo delata.
    print(f"proceso uid={getattr(os, 'getuid', lambda: None)()}", flush=True)
    app.state.estado = Estado(ajustes.archivo_estado)
    app.state.indice = None
    app.state.motor = None
    app.state.error_indice = ""
    app.state.indice_estado = "descargando"
    preparacion = asyncio.create_task(_preparar_indice(app, ajustes))

    await asyncio.to_thread(app.state.estado.podar)
    try:
        yield
    finally:
        # Una descarga a medias se cancela: el artefacto se vuelve a bajar entero en el
        # próximo arranque, que es lo mismo que hace hoy con el volumen vacío.
        preparacion.cancel()
        with suppress(asyncio.CancelledError):
            await preparacion
        if app.state.motor:
            await app.state.motor.cerrar()
        app.state.estado.cerrar()
        await asyncio.to_thread(observabilidad.esperar_trazas)


app = FastAPI(
    title=cfg.TITULO_API,
    description=msj.DESCRIPCION_API,
    version=cfg.VERSION_API,
    lifespan=ciclo_de_vida,
    # La API es de la interfaz propia: publicar su esquema solo agranda la superficie.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

if WEB.exists():
    app.mount("/estatico", StaticFiles(directory=WEB), name="estatico")


@app.exception_handler(ErrorDeAlmacenamiento)
async def error_de_almacenamiento(_request: Request, exc: ErrorDeAlmacenamiento):
    return JSONResponse(status_code=503, content={"detail": str(exc)})


# --- Páginas ---

@app.get("/")
async def inicio():
    return FileResponse(WEB / "index.html")


@app.get("/acerca-de")
async def acerca_de():
    return FileResponse(WEB / "acerca.html")


@app.get("/privacidad")
async def privacidad():
    return FileResponse(WEB / "privacidad.html")


# --- Respaldo ---

@app.get("/respaldo")
async def respaldo(request: Request):
    """Una copia consistente de `estado.sqlite3`, detrás de un token.

    Es lo único irreemplazable del despliegue: el índice se reconstruye con un script y esto
    no. Sin este endpoint, la única copia del registro de consultas, los cupos y el gasto vivía
    en un volumen y se perdía con él.

    **Sin `TOKEN_RESPALDO` el endpoint no existe.** Lo que sirve es el registro entero —doce
    meses de texto de consultas—, así que un default, un token corto o un descuido lo abriría a
    cualquiera: 404 y no 401, porque anunciar que hay algo detrás es información que no le
    sirve a nadie más que a quien busca. La comparación es en tiempo constante.
    """
    esperado = (obtener_ajustes().token_respaldo or "").strip()
    presentado = (request.headers.get("x-token-respaldo") or "").strip()
    if len(esperado) < cfg.LARGO_MINIMO_TOKEN or not secrets.compare_digest(
            presentado, esperado):
        return JSONResponse(status_code=404, content={"detail": msj.ERROR_NO_ENCONTRADO})

    estado: Estado = request.app.state.estado
    with tempfile.TemporaryDirectory() as temporal:
        copia = Path(temporal) / "estado.sqlite3"
        await asyncio.to_thread(estado.respaldar, copia)
        datos = copia.read_bytes()
    return Response(content=datos, media_type="application/vnd.sqlite3", headers={
        "Content-Disposition": 'attachment; filename="estado.sqlite3"'})


# --- Estado ---

@app.get("/vivo")
async def vivo():
    """El proceso atiende. Es lo que mira el healthcheck de la plataforma.

    **Separado de `/salud` a propósito.** `/salud` contesta 503 mientras no haya índice, que es
    lo correcto para la interfaz y lo contrario de lo que un healthcheck necesita: usarlo para
    eso hacía que un artefacto que no baja tumbara el despliegue entero, incluido el código que
    sí estaba bien, y dejaba al servicio en el bucle de reinicios que el arranque evita a
    propósito. Lo que decide si el deploy sirve es que el proceso conteste; lo que decide si el
    buscador puede responder es `/salud`, y eso lo mira una persona.
    """
    return {"vivo": True}


@app.get("/salud")
async def salud(request: Request):
    """Qué tiene el buscador y cuánto cupo queda.

    Sin datos de infraestructura: ni rutas, ni proyecto de trazas, ni nombres de modelo. Es un
    endpoint público y sin autenticar.
    """
    estado: Estado = request.app.state.estado
    indice = request.app.state.indice
    visitante = limites.identificar(request)
    ajustes = obtener_ajustes()
    del_dia = await asyncio.to_thread(estado.consultas_del_dia)
    cuerpo = {
        "indice": "listo" if indice else getattr(
            request.app.state, "indice_estado", "ausente"),
        "fragmentos": indice.fragmentos if indice else 0,
        "documentos": len(indice.lexico.documentos()) if indice else 0,
        "modo": "abierto" if del_dia < ajustes.consultas_por_dia else "lectura",
        "cupo_restante": await asyncio.to_thread(limites.restante, visitante, estado),
        "cupo_global_restante": max(0, ajustes.consultas_por_dia - del_dia),
    }
    if not indice:
        cuerpo["detalle"] = msj.MENSAJE_INDICE_NO_LISTO
        return JSONResponse(status_code=503, content=cuerpo)
    return cuerpo


@app.get("/metricas")
async def metricas(request: Request):
    """Los agregados públicos del uso. Nunca el listado de consultas."""
    estado: Estado = request.app.state.estado
    return await asyncio.to_thread(estado.metricas)


@app.get("/corpus")
async def corpus(request: Request):
    """Los documentos indexados, con su link oficial. Es lo que muestra la página del corpus."""
    indice = request.app.state.indice
    if not indice:
        return JSONResponse(status_code=503, content={"detail": msj.MENSAJE_INDICE_NO_LISTO})
    return {"documentos": indice.lexico.documentos(), "version": indice.version}


# --- Consultas ---

@app.post("/consultas", status_code=202)
async def crear_consulta(entrante: ConsultaEntrante, request: Request):
    """Cobra el cupo, decide si la consulta está en alcance, y lanza la corrida.

    El cupo se cobra acá y en ningún otro lado. El intento se cobra siempre; la consulta, solo
    si la admisión la deja pasar.

    Todas las salidas pasan por `_responder`, que siembra la cookie: un rechazo que no la
    dejara le daría al visitante una identidad nueva en cada intento, y el contador que frena
    a quien martillea consultas fuera de tema no acumularía nunca.
    """
    motor: Motor | None = request.app.state.motor
    estado: Estado = request.app.state.estado
    visitante = limites.identificar(request)
    if motor is None:
        return _responder({"detail": msj.MENSAJE_INDICE_NO_LISTO}, visitante, 503)

    en_vuelo = motor.en_vuelo_de(visitante.cookie)
    if en_vuelo:
        return _responder({"detail": msj.MENSAJE_CONSULTA_EN_CURSO, "consulta_id": en_vuelo},
                          visitante, 409)

    cupo = await asyncio.to_thread(limites.controlar, visitante, estado)
    if not cupo.permitido:
        return _responder({"detail": cupo.motivo, "reintentar_en": cupo.reintentar_en},
                          visitante, cupo.estado_http)

    # La admisión corre en toda consulta, la admita o no, y su costo entra al gasto del mes
    # como el de cualquier corrida. Sin esto, el camino más barato de martillear era también
    # el único invisible para el freno por presupuesto.
    contador = Contador()
    veredicto = await admision.admitir(
        entrante.consulta, request.app.state.indice.chroma, contador)
    consumo = contador.consumo
    if consumo.llamadas:
        await asyncio.to_thread(estado.sumar_gasto, consumo.usd, consumo.llamadas,
                                consumo.tokens_entrada, consumo.tokens_salida)
    consulta_id = registro.nuevo_id()
    if not veredicto.admitida:
        fila = await asyncio.to_thread(
            registro.abrir, entrante.consulta, consulta_id, estado)
        await asyncio.to_thread(registro.cerrar, fila, estado, admitida=0,
                                motivo_inadmision=veredicto.motivo, desenlace=cfg.SIN_BASE)
        return _responder({"detail": msj.MENSAJE_FUERA_DE_ALCANCE,
                           "motivo": veredicto.motivo, "consulta_id": consulta_id},
                          visitante, 422)

    # El cobro es el control que manda: entre el `controlar` de arriba y esta línea hubo una
    # llamada de red, y en esa ventana otros pedidos pasaron el mismo control con el mismo
    # número. Acá la cuenta y el cobro van en una transacción.
    reserva = await asyncio.to_thread(limites.reservar_consulta, visitante, estado)
    if not reserva.permitido:
        return _responder({"detail": reserva.motivo, "reintentar_en": reserva.reintentar_en},
                          visitante, reserva.estado_http)
    motor.lanzar(entrante.consulta, consulta_id, visitante.cookie)
    return _responder(
        {"consulta_id": consulta_id,
         "cupo_restante": await asyncio.to_thread(limites.restante, visitante, estado)},
        visitante, 202)


def _responder(cuerpo: dict, visitante: limites.Visitante, estado_http: int) -> JSONResponse:
    """La respuesta con la cookie del visitante puesta."""
    respuesta = JSONResponse(status_code=estado_http, content=cuerpo)
    limites.sembrar_cookie(respuesta, visitante)
    return respuesta


@app.get("/consultas/{consulta_id}/stream")
async def stream(consulta_id: str, request: Request):
    """El avance de una corrida, por Server-Sent Events.

    Reproduce lo ya emitido antes de seguir en vivo, así una recarga muestra la respuesta
    entera en vez de empezar por la mitad. `Last-Event-ID` dice desde dónde.
    """
    motor: Motor | None = request.app.state.motor
    corrida = motor.corrida(consulta_id) if motor else None
    if corrida is None:
        return JSONResponse(status_code=404, content={
            "detail": msj.ERROR_CONSULTA_INEXISTENTE.format(consulta_id=consulta_id)})

    # `Last-Event-ID` es el id del último evento que el cliente **recibió**, así que se sigue
    # desde el que viene después. Tomándolo como punto de partida, cada reconexión repetía ese
    # evento: un delta de texto duplicado, o una cita pintada dos veces.
    ultimo = request.headers.get("last-event-id")
    desde = int(ultimo) + 1 if (ultimo or "").strip().isdigit() else 0

    async def emitir():
        indice = desde
        async for evento in corrida.seguir(desde):
            yield {"id": str(indice), "event": evento.nombre,
                   "data": json.dumps(evento.datos, ensure_ascii=False)}
            indice += 1

    return EventSourceResponse(
        emitir(),
        ping=cfg.SEGUNDOS_LATIDO_SSE,
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/consultas/{consulta_id}")
async def leer_consulta(consulta_id: str, request: Request):
    """El resultado de una consulta terminada, para recargar o compartir."""
    estado: Estado = request.app.state.estado
    fila = await asyncio.to_thread(estado.leer_consulta, consulta_id)
    if fila is None:
        return JSONResponse(status_code=404, content={
            "detail": msj.ERROR_CONSULTA_INEXISTENTE.format(consulta_id=consulta_id)})
    return {
        "consulta_id": fila["id"],
        "consulta": fila["consulta"],
        "desenlace": fila["desenlace"],
        "respuesta": fila["respuesta"],
        "citas": json.loads(fila["citas"] or "[]"),
        "senales": json.loads(fila["senales"] or "[]"),
        "motivo_inadmision": fila["motivo_inadmision"],
        "duracion_ms": fila["duracion_ms"],
    }
