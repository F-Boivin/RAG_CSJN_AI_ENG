"""De los `updates` del grafo a los eventos que ve una persona.

`traducir` es una función pura: recibe el nombre del nodo y lo que ese nodo devolvió, y
devuelve un evento o nada. Al no tocar el grafo ni la red, se prueba sin montar nada.

Lee **los artefactos** (`Investigacion`, `Verificacion`, `Redaccion`) y no los `messages`. Esos
strings —"[verificador] 4 de 6 citas existen"— están escritos para un log de operador; el
público necesita otra cosa, y traducirlos sería reparsear lo que el artefacto ya dice bien.

El orden importa. `stream_mode="updates"` emite DESPUÉS de que un nodo corre, así que
"buscando doctrina" llegaría cuando la búsqueda ya terminó. El evento de inicio de cada
especialista sale del update del **supervisor**, que llega antes y trae `siguiente`; el de fin
sale del update del especialista.
"""

from dataclasses import dataclass, field

import app.nucleo.constantes as cfg
import app.nucleo.mensajes as msj
from app.grafo.estado import INVESTIGADOR, REDACTOR, SUPERVISOR, VERIFICADOR


@dataclass
class Evento:
    """Un evento del stream: su nombre y su cuerpo JSON."""

    nombre: str
    datos: dict = field(default_factory=dict)


ANUNCIOS = {
    INVESTIGADOR: msj.FASE_INVESTIGANDO,
    VERIFICADOR: msj.FASE_VERIFICANDO,
    REDACTOR: msj.FASE_REDACTANDO,
}


def traducir(nodo: str, actualizacion: dict) -> Evento | None:
    """El evento que corresponde a lo que ese nodo acaba de devolver, o None."""
    if nodo == SUPERVISOR:
        return _del_supervisor(actualizacion)
    if nodo == INVESTIGADOR:
        return _del_investigador(actualizacion)
    if nodo == VERIFICADOR:
        return _del_verificador(actualizacion)
    return None


def _del_supervisor(actualizacion: dict) -> Evento | None:
    """El anuncio de lo que viene, más el aviso de corrección cuando corresponde.

    `intentos` cuenta las correcciones, y la primera pasada no es una: el supervisor ya
    incrementó el contador al delegar, así que ese número es la corrección en curso. Sumarle
    uno mostraba «Intento 4 de 3» en la última.
    """
    siguiente = actualizacion.get("siguiente")
    if siguiente not in ANUNCIOS:
        return None
    intentos = actualizacion.get("intentos", 0)
    if intentos:
        return Evento("estado", {
            "fase": "corrigiendo",
            "detalle": msj.FASE_REINTENTANDO.format(
                intento=min(intentos, cfg.MAXIMO_INTENTOS), maximo=cfg.MAXIMO_INTENTOS,
                motivo=ANUNCIOS[siguiente].lower().rstrip(".")),
        })
    return Evento("estado", {"fase": siguiente, "detalle": ANUNCIOS[siguiente]})


def _del_investigador(actualizacion: dict) -> Evento | None:
    investigaciones = actualizacion.get("investigaciones") or ()
    if not investigaciones:
        return None
    investigacion = investigaciones[-1]
    return Evento("estado", {
        "fase": "investigado",
        "detalle": msj.FASE_INVESTIGADO.format(
            citas=len(investigacion.citas), subsecciones=len(investigacion.subsecciones)),
    })


def _del_verificador(actualizacion: dict) -> Evento | None:
    """El veredicto, con su motivo cuando rechaza.

    El conteo de citas por sí solo miente: una verificación con todas las citas ciertas puede
    rechazar igual —por una subsección mal atribuida, por ejemplo—, y quien mira la pantalla
    vería "5 de 5 citas existen" seguido de un reintento que no se explica.
    """
    verificaciones = actualizacion.get("verificaciones") or ()
    if not verificaciones:
        return None
    verificacion = verificaciones[-1]
    propuestas = len(verificacion.verificadas) + len(verificacion.inexistentes)
    detalle = msj.FASE_VERIFICADO.format(
        verificadas=len(verificacion.verificadas), propuestas=propuestas)
    if not verificacion.aprobado and verificacion.observaciones:
        detalle += f" {msj.FASE_RECHAZADO.format(motivo=verificacion.observaciones[0])}"
    return Evento("estado", {
        "fase": "verificado" if verificacion.aprobado else "rechazado",
        "detalle": detalle,
    })


def fichas_de_citas(estado: dict, padron: dict[str, str]) -> list[dict]:
    """Las citas que la respuesta usa, con su link, de dónde salieron y qué las respalda.

    Cuatro datos y cuatro procedencias, y solo una es del modelo. La URL sale del padrón del
    índice. **La subsección sale del registro de lo recuperado**, que sabe qué fragmento trajo
    cada fallo: antes la escribía el investigador y el verificador la rechazaba cuando no
    resolvía, lo que le costó a la consulta insignia sus tres correcciones. **El respaldo es el
    pasaje del corpus** que el verificador ya comprobó contra el texto leído, así que lo que el
    lector ve es la oración del documento y no una reescritura. La afirmación sí es del
    investigador, porque es lo único acá que nadie más puede escribir.

    Mostrar el respaldo es lo que le permite a quien lee juzgar el salto entre el pasaje y la
    afirmación, que es justo lo que el sistema no comprueba.

    **El pasaje se muestra solo si sale de un fragmento que cita ese mismo fallo.** Si no, la
    ficha va sin él. Mostrarlo igual era presentar como respaldo de un fallo una oración de
    otro documento: en producción pasaba con 4 de 14 citas. El pasaje lo elige el verificador
    y acá solo se lee, así que si falta no se muestra nada.

    **Y la afirmación es la del pasaje que se muestra.** Cuando un fallo sostiene dos
    afirmaciones, cada una trae su pasaje: la ficha las mantiene juntas, porque mostrar la
    afirmación de una con el pasaje de la otra es la misma mentira con otro orden.
    """
    from app.rag import citas as c

    redacciones = estado.get("redacciones") or ()
    investigaciones = estado.get("investigaciones") or ()
    recuperado = estado.get("recuperado") or {}
    verificaciones = estado.get("verificaciones") or ()
    if not redacciones:
        return []
    pasajes = dict(verificaciones[-1].pasajes_propios) if verificaciones else {}
    por_cita: dict[str, list] = {}
    for inv in investigaciones:
        for cita in inv.citas:
            por_cita.setdefault(c.normalizar_cita(cita.fallo), []).append(cita)
    fichas = []
    for usada in redacciones[-1].citas_usadas:
        clave = c.normalizar_cita(usada)
        pasaje = pasajes.get(clave, "")
        candidatas = por_cita.get(clave, [])
        # La más reciente que trae ese pasaje; sin pasaje propio, la más reciente a secas.
        origen = (next((ct for ct in reversed(candidatas) if pasaje and ct.respaldo == pasaje), None)
                  or (candidatas[-1] if candidatas else None))
        fichas.append({
            "fallo": f"Fallos: {clave}" if clave else usada,
            "url": padron.get(clave, ""),
            "subseccion": recuperado.get(clave, ""),
            "afirmacion": origen.afirmacion if origen else "",
            "respaldo": pasaje,
        })
    return sorted(fichas, key=lambda f: c.clave_fallo(f["fallo"]))
