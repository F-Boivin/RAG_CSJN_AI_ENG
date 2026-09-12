"""El motor: corre el grafo dentro del proceso de la API y publica su avance.

Una corrida vive en un `asyncio.Task` propio, y cada evento se escribe en dos lados: una cola,
que alimenta al cliente conectado, y una lista acotada, que permite reproducir el stream desde
el principio cuando alguien recarga la página. Que el navegador se vaya deja la tarea
corriendo: el visitante ya gastó una consulta de su cupo y puede volver a buscar el resultado
por su id.

El semáforo acota cuántas corridas hay en vuelo. Es un solo proceso con un solo event loop, y
el trabajo pesado es espera de red contra el proveedor; lo que el semáforo protege es la
memoria y la factura, no la CPU.
"""

import asyncio
import time
import traceback
from dataclasses import dataclass, field

import app.nucleo.constantes as cfg
import app.nucleo.mensajes as msj
from app.almacen.estado import Estado
from app.grafo.estado import REDACTOR, leer_situacion
from app.nucleo.config import obtener_ajustes
from app.nucleo.errores import ErrorDeAgente, ErrorRAG
from app.servicio import registro
from app.servicio.costo import Contador
from app.servicio.eventos import Evento, fichas_de_citas, traducir

FIN = object()


@dataclass
class Corrida:
    """Una consulta en vuelo o ya terminada, con su historial de eventos."""

    consulta_id: str
    consulta: str
    eventos: list[Evento] = field(default_factory=list)
    suscriptores: list[asyncio.Queue] = field(default_factory=list)
    terminada: bool = False
    terminada_en: float = 0.0
    resultado: dict = field(default_factory=dict)

    def publicar(self, evento: Evento) -> None:
        """Anota el evento y se lo pasa a quien esté escuchando."""
        if len(self.eventos) < cfg.EVENTOS_RETENIDOS:
            self.eventos.append(evento)
        for cola in self.suscriptores:
            cola.put_nowait(evento)

    def cerrar(self) -> None:
        self.terminada = True
        self.terminada_en = time.monotonic()
        for cola in self.suscriptores:
            cola.put_nowait(FIN)

    async def seguir(self, desde: int = 0):
        """Reproduce lo ya emitido y después sigue en vivo.

        `desde` es lo que el cliente ya vio, que llega por `Last-Event-ID`. Sin ese punto de
        partida, una reconexión repetiría el texto entero.
        """
        for evento in self.eventos[desde:]:
            yield evento
        if self.terminada:
            return
        cola: asyncio.Queue = asyncio.Queue()
        self.suscriptores.append(cola)
        try:
            while True:
                evento = await cola.get()
                if evento is FIN:
                    return
                yield evento
        finally:
            self.suscriptores.remove(cola)


class Motor:
    """Las corridas en vuelo y el grafo que las ejecuta."""

    def __init__(self, grafo, indice, estado: Estado, concurrentes: int = None):
        self.grafo = grafo
        self.indice = indice
        self.estado = estado
        self._semaforo = asyncio.Semaphore(
            concurrentes or obtener_ajustes().consultas_concurrentes)
        self._corridas: dict[str, Corrida] = {}
        self._tareas: dict[str, asyncio.Task] = {}
        self._por_visitante: dict[str, str] = {}

    # --- Ciclo de vida de una corrida ---

    def en_vuelo_de(self, visitante: str) -> str | None:
        """El id de la corrida que ese visitante tiene andando, si hay alguna."""
        consulta_id = self._por_visitante.get(visitante)
        corrida = self._corridas.get(consulta_id) if consulta_id else None
        return consulta_id if corrida and not corrida.terminada else None

    def corrida(self, consulta_id: str) -> Corrida | None:
        return self._corridas.get(consulta_id)

    def lanzar(self, consulta: str, consulta_id: str, visitante: str) -> Corrida:
        """Arranca la corrida y devuelve su handle sin esperarla."""
        self._podar()
        corrida = Corrida(consulta_id=consulta_id, consulta=consulta)
        self._corridas[consulta_id] = corrida
        self._por_visitante[visitante] = consulta_id
        self._tareas[consulta_id] = asyncio.create_task(self._correr(corrida))
        return corrida

    async def cerrar(self) -> None:
        """Cancela lo que quede en vuelo. Lo emitido ya está persistido."""
        for tarea in list(self._tareas.values()):
            tarea.cancel()
        if self._tareas:
            await asyncio.gather(*self._tareas.values(), return_exceptions=True)
        self._tareas.clear()

    def _podar(self) -> None:
        """Descarta de memoria las corridas terminadas hace rato. El registro queda en disco."""
        limite = time.monotonic() - cfg.MINUTOS_RETENCION_CORRIDA * 60
        vencidas = [i for i, c in self._corridas.items()
                    if c.terminada and c.terminada_en < limite]
        for consulta_id in vencidas:
            self._corridas.pop(consulta_id, None)
            self._tareas.pop(consulta_id, None)

    # --- La corrida ---

    async def _correr(self, corrida: Corrida) -> None:
        fila = registro.abrir(corrida.consulta, corrida.consulta_id, self.estado)
        comienzo = time.monotonic()
        contador = Contador()
        try:
            async with self._cola(corrida):
                # El tope va adentro del semáforo, sobre el trabajo y no sobre la espera: una
                # corrida encolada detrás de otras no se cae por haber esperado su turno. Lo
                # que corta es la que se colgó, y sin él tres colgadas tapan el semáforo con la
                # cola entera detrás y el cupo de cada uno ya cobrado.
                final = await asyncio.wait_for(self._ejecutar(corrida, contador),
                                               timeout=cfg.SEGUNDOS_MAXIMO_CORRIDA)
            self._resolver(corrida, final, fila, contador, comienzo)
        except asyncio.TimeoutError:
            self._fallar(corrida, fila, contador, comienzo,
                         msj.ERROR_CORRIDA_DEMORADA.format(
                             segundos=cfg.SEGUNDOS_MAXIMO_CORRIDA))
        except asyncio.CancelledError:
            self._fallar(corrida, fila, contador, comienzo, "corrida cancelada")
            raise
        except (ErrorDeAgente, ErrorRAG, Exception) as exc:
            # El traceback va al registro y nunca al cliente. Una corrida pública falla en
            # una máquina a la que nadie mira: sin la pila, el error queda como una línea que
            # no se puede seguir hasta su causa.
            self._fallar(corrida, fila, contador, comienzo,
                         f"{type(exc).__name__}: {exc}", traceback.format_exc())
        finally:
            corrida.cerrar()
            self._tareas.pop(corrida.consulta_id, None)

    def _cola(self, corrida: Corrida):
        """El semáforo, avisando la posición al que espera."""
        semaforo = self._semaforo

        class Espera:
            async def __aenter__(self):
                if semaforo.locked():
                    corrida.publicar(Evento("estado", {
                        "fase": "en_cola",
                        "detalle": msj.FASE_EN_COLA.format(posicion=len(semaforo._waiters or [])),
                    }))
                await semaforo.acquire()
                return self

            async def __aexit__(self, *_):
                semaforo.release()

        return Espera()

    async def _ejecutar(self, corrida: Corrida, contador: Contador) -> dict:
        """Corre el grafo y traduce su stream a eventos."""
        entrada = {
            "messages": [], "consulta": corrida.consulta, "siguiente": "investigador",
            "investigaciones": (), "verificaciones": (), "redacciones": (),
            "intentos": 0, "vueltas": 0, "completado": False,
        }
        config = {
            "recursion_limit": cfg.LIMITE_RECURSION_ORQUESTADOR,
            "callbacks": [contador],
        }
        final: dict = {}
        # `subgraphs=True` es lo que hace visible al redactor. Los tres especialistas son
        # agentes ReAct, o sea grafos compilados aparte que corren adentro de su nodo: sin
        # esta bandera el stream de mensajes solo trae al supervisor, que es el único que
        # llama al modelo desde el grafo de arriba. Medido: 355 chunks del redactor aparecen
        # con la bandera y cero sin ella.
        async for espacio, modo, dato in self.grafo.astream(
            entrada, config, stream_mode=["updates", "messages", "values"], subgraphs=True
        ):
            # El estado final y el avance son del grafo de arriba. Con `subgraphs=True` los
            # subgrafos también emiten sus `values` y sus `updates`, y tomarlos dejaría
            # `final` con el estado del agente ReAct en vez del orquestador.
            del_orquestador = not espacio
            if modo == "values" and del_orquestador:
                final = dato
            elif modo == "updates" and del_orquestador:
                for nodo, actualizacion in dato.items():
                    evento = traducir(nodo, actualizacion or {})
                    if evento:
                        corrida.publicar(evento)
            elif modo == "messages":
                self._texto(corrida, espacio, dato)
        return final

    def _texto(self, corrida: Corrida, espacio, dato) -> None:
        """Los tokens de la respuesta, mientras el redactor los escribe.

        El espacio de nombres dice de qué especialista viene el chunk y el nodo interno, de
        qué parte de su ciclo ReAct: `agent` es el modelo escribiendo, `tools` es la salida de
        `link_oficial`. Los turnos en que el redactor pide links llegan con `tool_call_chunks`
        y quedan afuera: son andamiaje, no la respuesta.

        El texto autoritativo viaja igual en el evento de cierre, así una reescritura tras un
        reintento reemplaza lo que el cliente venía acumulando.
        """
        raiz = espacio[0].split(":")[0] if espacio else ""
        chunk, metadata = dato
        if raiz != REDACTOR or metadata.get("langgraph_node") != "agent":
            return
        if getattr(chunk, "tool_call_chunks", None) or getattr(chunk, "tool_calls", None):
            return
        texto = getattr(chunk, "text", None)
        if isinstance(texto, str) and texto:
            corrida.publicar(Evento("texto", {"delta": texto}))

    # --- Desenlaces ---

    def _resolver(self, corrida: Corrida, final: dict, fila: dict,
                  contador: Contador, comienzo: float) -> None:
        """Publica o contesta que no hay base, según la barra del grafo."""
        situacion = leer_situacion(final)
        calidad = self._calidad(final)
        duracion = int((time.monotonic() - comienzo) * 1000)
        fichas = fichas_de_citas(final, self.indice.lexico.padron()) if situacion.listo else []
        texto = final["redacciones"][-1].texto if (final.get("redacciones") and situacion.listo) else ""

        if situacion.listo:
            for ficha in fichas:
                corrida.publicar(Evento("cita", ficha))
            corrida.publicar(Evento("final", {
                "publicado": True, "consulta_id": corrida.consulta_id,
                # El texto va entero además de por deltas: un reintento reescribe la
                # respuesta, y el cliente tiene que quedarse con la última.
                "respuesta": texto,
                "calidad": calidad, "duracion_ms": duracion,
            }))
        else:
            corrida.publicar(Evento("sin_base", {
                "motivo": msj.MENSAJE_SIN_BASE,
                "diagnostico": calidad,
                "consulta_id": corrida.consulta_id,
            }))

        corrida.resultado = {
            "publicado": situacion.listo, "respuesta": texto, "citas": fichas,
            "calidad": calidad, "duracion_ms": duracion,
        }
        self._anotar(fila, contador, duracion, calidad,
                     desenlace=cfg.PUBLICADA if situacion.listo else cfg.SIN_BASE,
                     respuesta=texto, citas=fichas)

    def _fallar(self, corrida: Corrida, fila: dict, contador: Contador,
                comienzo: float, detalle: str, pila: str = "") -> None:
        corrida.publicar(Evento("error", {"mensaje": msj.ERROR_CORRIDA.format(detalle=detalle)}))
        corrida.resultado = {"publicado": False, "error": detalle}
        self._anotar(fila, contador, int((time.monotonic() - comienzo) * 1000), {},
                     desenlace=cfg.FALLIDA, error=(pila or detalle)[-4000:])

    def _calidad(self, final: dict) -> dict:
        """La telemetría del grafo, para el registro y el evento de cierre."""
        from app.grafo.estado import evaluar_calidad

        if not final:
            return {}
        calidad = evaluar_calidad(final, cfg.CITAS_HOLGADAS, cfg.MAXIMO_INTENTOS)
        return {
            "citas_propuestas": calidad.citas_propuestas,
            "citas_verificadas": calidad.citas_verificadas,
            "citas_inexistentes": calidad.citas_inexistentes,
            "cobertura": round(calidad.cobertura, 3),
            "citas_en_el_texto": calidad.citas_en_el_texto,
            "intentos": calidad.intentos,
            "senales": list(calidad.motivos),
        }

    def _anotar(self, fila: dict, contador: Contador, duracion: int, calidad: dict,
                **cambios) -> None:
        consumo = contador.consumo
        self.estado.sumar_gasto(consumo.usd, consumo.llamadas,
                                consumo.tokens_entrada, consumo.tokens_salida)
        registro.cerrar(fila, self.estado, **{
            "duracion_ms": duracion,
            "usd": round(consumo.usd, 6),
            "modelo": consumo.modelo,
            "version_indice": self.indice.version,
            "intentos": calidad.get("intentos", 0),
            "citas_propuestas": calidad.get("citas_propuestas", 0),
            "citas_verificadas": calidad.get("citas_verificadas", 0),
            "citas_inexistentes": calidad.get("citas_inexistentes", 0),
            "senales": calidad.get("senales", []),
            **cambios,
        })
