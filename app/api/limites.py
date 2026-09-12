"""Quién es un visitante y cuánto le toca.

Dos claves por persona. La **cookie** es la principal: identifica a quien vuelve, y su cupo es
el que reparte con equidad. El **hash de la IP** es la secundaria, con un tope más alto, para
que borrar cookies no dé cupo infinito sin castigar a un estudio entero detrás de un NAT
compartido.

La IP cruda nunca se guarda: lo que va a disco es `sha256(SAL + ip)`, y solo mientras dure la
ventana de 48 h. Rotar `SAL_VISITANTE` olvida a todos.

Ninguna de las dos claves protege el presupuesto, y conviene decirlo: quien quiera eludir el
cupo puede. Lo que protege el presupuesto es el techo global diario y el corte por gasto.
"""

from dataclasses import dataclass
from datetime import datetime

from fastapi import Request, Response

import app.nucleo.constantes as cfg
import app.nucleo.mensajes as msj
from app.almacen.estado import Estado
from app.nucleo.config import obtener_ajustes
from app.servicio.registro import huella_visitante

COOKIE = "visitante"
TIPO_CONSULTA = "consulta"
TIPO_INTENTO = "intento"


@dataclass
class Visitante:
    """Las dos claves con que se cuenta a quien pregunta.

    `cruda` es el valor de la cookie sin hashear, y viaja solo hasta `sembrar_cookie`: si la
    cookie que se manda al navegador fuera otra que la que se hasheó, el cupo se cobraría
    contra una clave que nadie vuelve a presentar y cada visita empezaría de cero.
    """

    cookie: str
    ip: str
    cruda: str
    nueva: bool


@dataclass
class Cupo:
    """El resultado de mirar los contadores."""

    permitido: bool
    motivo: str = ""
    estado_http: int = 200
    reintentar_en: str = ""


def identificar(request: Request) -> Visitante:
    """Las claves del visitante, las dos hasheadas.

    Railway inyecta la IP del cliente en `X-Real-IP`; `X-Forwarded-For` queda como respaldo
    para otros proxies, donde la primera entrada es el cliente.
    """
    import uuid

    ajustes = obtener_ajustes()
    cruda = request.cookies.get(COOKIE)
    nueva = not cruda
    if nueva:
        cruda = uuid.uuid4().hex
    ip = (request.headers.get("x-real-ip")
          or (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
          or (request.client.host if request.client else "desconocida"))
    sal = ajustes.sal_de_visitante()
    return Visitante(
        cookie=huella_visitante(cruda, sal),
        ip=huella_visitante(ip, sal),
        cruda=cruda,
        nueva=nueva,
    )


def sembrar_cookie(response: Response, visitante: Visitante) -> None:
    """Deja en el navegador la misma cookie que se acaba de hashear.

    `HttpOnly` porque ningún script necesita leerla, y 400 días porque es el máximo que los
    navegadores conservan.
    """
    if not visitante.nueva:
        return
    response.set_cookie(
        COOKIE, visitante.cruda, max_age=400 * 24 * 3600,
        httponly=True, samesite="lax", secure=True,
    )


def controlar(visitante: Visitante, estado: Estado) -> Cupo:
    """Mira los cuatro contadores, del más específico al más general.

    El orden es el que le sirve a quien pregunta: primero lo que le toca a él, después el techo
    del sitio. Al revés, en un día ocupado todos leerían "el buscador llegó a su techo" aunque
    les quedara cupo propio.
    """
    ajustes = obtener_ajustes()

    # El gasto va primero y se lee sin cobrar nada: es un agregado que se mueve lento, y no
    # tiene sentido consumirle un intento a alguien cuando el sitio ya está en modo lectura.
    if estado.gasto_del_mes() >= ajustes.presupuesto_usd_mes:
        return Cupo(False, msj.MENSAJE_PRESUPUESTO_AGOTADO, 503)

    # **El intento se cuenta y se cobra en la misma transacción**, y también por IP. Colgado
    # solo de la cookie el contador no contaba nada: quien no la devuelve recibe una nueva en
    # cada request, así que sus intentos arrancan siempre en cero. Y el tope de consultas no lo
    # alcanza, porque la consulta se cobra recién cuando la admisión aprueba: una consulta
    # rechazada paga un embedding y una llamada al clasificador, y era gratis repetirla sin
    # límite. La cookie sigue primero porque es la que reparte con equidad; la IP es el piso
    # que hace que el tope exista.
    if not estado.reservar(visitante.cookie, TIPO_INTENTO, ajustes.intentos_por_visitante):
        return _agotado(estado, visitante.cookie, TIPO_INTENTO,
                        msj.MENSAJE_CUOTA_INTENTOS, ajustes.intentos_por_visitante)
    # La cookie ya quedó cobrada: si el piso por IP corta, ese intento se pierde. Es el lado
    # conservador del error, y el único que no regala cupo.
    if not estado.reservar(visitante.ip, TIPO_INTENTO, ajustes.intentos_por_ip):
        return _agotado(estado, visitante.ip, TIPO_INTENTO,
                        msj.MENSAJE_CUOTA_INTENTOS, ajustes.intentos_por_ip)

    # Las consultas se miran acá para poder contestar temprano y con el motivo correcto, y se
    # cobran recién en `reservar_consulta`, después de la admisión. Ese control es el que
    # manda: entre los dos hay una llamada de red, y lo que decide es el de la transacción.
    usos = estado.usos(visitante.cookie, TIPO_CONSULTA)
    if usos >= ajustes.consultas_por_visitante:
        return _agotado(estado, visitante.cookie, TIPO_CONSULTA,
                        msj.MENSAJE_CUOTA_VISITANTE, ajustes.consultas_por_visitante)

    por_ip = estado.usos(visitante.ip, TIPO_CONSULTA)
    if por_ip >= ajustes.consultas_por_ip:
        return _agotado(estado, visitante.ip, TIPO_CONSULTA,
                        msj.MENSAJE_CUOTA_VISITANTE, ajustes.consultas_por_ip)

    if estado.consultas_del_dia() >= ajustes.consultas_por_dia:
        return Cupo(False, msj.MENSAJE_MODO_LECTURA, 503)

    return Cupo(True)


def restante(visitante: Visitante, estado: Estado) -> int:
    """Cuántas consultas le quedan al visitante. La interfaz lo muestra desde el principio."""
    ajustes = obtener_ajustes()
    return max(0, ajustes.consultas_por_visitante
               - estado.usos(visitante.cookie, TIPO_CONSULTA))


def reservar_consulta(visitante: Visitante, estado: Estado) -> Cupo:
    """Cobra la consulta admitida en las tres cuentas que la miran, y dice si se pudo.

    **Es el control que manda.** `controlar` mira estas mismas tres cuentas antes de la
    admisión, para poder contestar temprano y con el motivo correcto; entre ese control y este
    cobro hay una llamada de red al clasificador, y en esa ventana otros pedidos pasan el mismo
    control con el mismo número. Contar y cobrar acá, en una transacción por cuenta, es lo que
    hace que el tope sea un tope: disparar pedidos a la vez deja de servir para pasarlo.
    """
    ajustes = obtener_ajustes()
    if not estado.reservar(visitante.cookie, TIPO_CONSULTA, ajustes.consultas_por_visitante):
        return _agotado(estado, visitante.cookie, TIPO_CONSULTA,
                        msj.MENSAJE_CUOTA_VISITANTE, ajustes.consultas_por_visitante)
    if not estado.reservar(visitante.ip, TIPO_CONSULTA, ajustes.consultas_por_ip):
        return _agotado(estado, visitante.ip, TIPO_CONSULTA,
                        msj.MENSAJE_CUOTA_VISITANTE, ajustes.consultas_por_ip)
    if not estado.reservar_dia(ajustes.consultas_por_dia):
        return Cupo(False, msj.MENSAJE_MODO_LECTURA, 503)
    return Cupo(True)


def _agotado(estado: Estado, clave: str, tipo: str, plantilla: str, tope: int) -> Cupo:
    cuando = estado.se_repone(clave, tipo)
    return Cupo(
        permitido=False,
        motivo=plantilla.format(tope=tope, horas=cfg.HORAS_VENTANA_CUOTA,
                                cuando=_legible(cuando)),
        estado_http=429,
        reintentar_en=cuando.isoformat() if cuando else "",
    )


def _legible(cuando: datetime | None) -> str:
    """La hora de reposición en horario de Buenos Aires, que es donde está quien pregunta."""
    if cuando is None:
        return "en unas horas"
    from datetime import timedelta, timezone

    local = cuando.astimezone(timezone(timedelta(hours=-3)))
    return local.strftime("%d/%m a las %H:%M")
