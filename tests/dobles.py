"""Dobles deterministas de los tres especialistas y del supervisor.

Los cuatro nodos falsos producen artefactos válidos sin llamar a ningún modelo, así el grafo
real —ruteo, frenos y arista de cierre— se ejerce en milisegundos. Lo que se prueba con ellos
es el cableado, que es lo único que no se reemplaza.
"""

from app.grafo.estado import (
    Cita,
    Investigacion,
    Redaccion,
    Verificacion,
    huella_material,
    leer_situacion,
)
from app.nucleo import constantes as cfg
from app.rag.citas import normalizar_cita

CONSULTA = "¿Que es el exceso ritual manifiesto?"
FALLOS = ("Fallos: 311:2437", "Fallos: 315:1848", "Fallos: 330:1228",
          "Fallos: 338:623", "Fallos: 349:306")
SUBSECCION = "6.2.7 Exceso ritual manifiesto"
# El texto del fragmento que los dobles dan por leido. Los respaldos de las citas salen de
# aca, asi el verificador los encuentra con la compuerta encendida o apagada.
TEXTO_LEIDO = (
    "El exceso ritual manifiesto configura una causal autonoma de arbitrariedad cuando la "
    "forma sacrifica la verdad juridica objetiva, y la sentencia deja de ser una derivacion "
    "razonada del derecho vigente con arreglo a las circunstancias comprobadas de la causa."
)


def estado_inicial(consulta: str = CONSULTA) -> dict:
    """El estado con el que arranca el grafo."""
    return {"messages": [], "consulta": consulta, "siguiente": "investigador",
            "investigaciones": (), "verificaciones": (), "redacciones": (),
            "recuperado": {}, "textos_leidos": {},
            "intentos": 0, "vueltas": 0, "completado": False}


def citas(cantidad: int) -> tuple:
    """`cantidad` citas del padrón de prueba."""
    return tuple(
        Cita(fallo=f, afirmacion=f"El fallo {f} sostiene la doctrina.", respaldo=TEXTO_LEIDO)
        for f in FALLOS[:cantidad]
    )


def leido(cantidad: int) -> dict:
    """El registro de lo recuperado que le corresponde a esas citas.

    Sin esto el verificador las da por impertinentes, que es exactamente lo que tiene que
    hacer: una cita que el investigador no leyó no se publica por más que exista.
    """
    return {normalizar_cita(f): SUBSECCION for f in FALLOS[:cantidad]}


def textos_leidos() -> dict:
    """El texto que los dobles dan por servido, contra el que se comprueban los respaldos."""
    return {"huella": TEXTO_LEIDO}


def investigador_falso(state):
    """Devuelve más citas en cada pasada: es lo que hace observable una corrección."""
    cantidad = 3 + len(state.get("investigaciones") or ())
    return {"investigaciones": (Investigacion(
        sintesis="Sintesis de prueba sobre el exceso ritual manifiesto.",
        citas=citas(cantidad), subsecciones=(SUBSECCION,)),),
        "recuperado": leido(cantidad), "textos_leidos": textos_leidos(),
        "messages": [("assistant", f"[investigador] doble · {cantidad} citas")]}


def investigador_inventor(state):
    """Cita un fallo que no existe en el padrón mientras haya una sola investigación.

    Con la segunda pasada corrige, así se ve el ciclo entero: rechazo, corrección y
    publicación con la señal de citas inexistentes encendida en la telemetría.
    """
    if len(state.get("investigaciones") or ()) >= 1:
        return investigador_falso(state)
    inventada = Cita(fallo="Fallos: 999:9999",
                     afirmacion="Un fallo que el corpus no registra.", respaldo=TEXTO_LEIDO)
    return {"investigaciones": (Investigacion(
        sintesis="Sintesis de prueba sobre el exceso ritual manifiesto.",
        citas=citas(2) + (inventada,), subsecciones=(SUBSECCION,)),),
        "recuperado": leido(2), "textos_leidos": textos_leidos(),
        "messages": [("assistant", "[investigador] doble · con una cita inventada")]}


def investigador_terco(state):
    """Repite siempre la misma cita inventada, junto a dos que sí existen.

    Es el caso que se midió contra el corpus real: el modelo cita un fallo de memoria y lo
    vuelve a proponer en cada corrección, aun leyendo que no existe.
    """
    inventada = Cita(fallo="Fallos: 999:9999", respaldo=TEXTO_LEIDO,
                     afirmacion="Un fallo que el corpus no registra, y que insiste en citar.")
    return {"investigaciones": (Investigacion(
        sintesis=f"Sintesis {len(state.get('investigaciones') or ()) + 1} con una cita terca.",
        citas=citas(2) + (inventada,), subsecciones=(SUBSECCION,)),),
        "recuperado": leido(2), "textos_leidos": textos_leidos(),
        "messages": [("assistant", "[investigador] doble · terco")]}


def verificador_falso(state):
    """Aprueba las citas que están en el padrón de prueba y rechaza las demás."""
    investigacion = state["investigaciones"][-1]
    verificadas = tuple(c.fallo for c in investigacion.citas if c.fallo in FALLOS)
    inexistentes = tuple(c.fallo for c in investigacion.citas if c.fallo not in FALLOS)
    return {"verificaciones": (Verificacion(
        verificadas=verificadas, inexistentes=inexistentes,
        aprobado=not inexistentes,
        observaciones=() if not inexistentes else (
            f"estos fallos no existen en el corpus: {', '.join(inexistentes)}",)),),
        "messages": [("assistant", "[verificador] doble")]}


def redactor_falso(state):
    """Escribe un texto que invoca todas las citas verificadas."""
    investigacion = state["investigaciones"][-1]
    verificacion = state["verificaciones"][-1]
    n = len(state.get("redacciones") or ()) + 1
    usadas = verificacion.verificadas
    return {"redacciones": (Redaccion(
        texto=f"Redaccion {n} sobre {len(usadas)} citas: {', '.join(usadas)}.",
        citas_usadas=usadas, citas_intrusas=(), motivos=(), limpia=True, llamadas=1,
        sobre_material=huella_material(investigacion, verificacion)),),
        "messages": [("assistant", f"[redactor] doble · {len(usadas)} citas")]}


def redactor_escaso(state):
    """Invoca dos citas mientras haya una sola investigación: por debajo de la holgura.

    Con material ampliado usa todo lo verificado. El texto lleva el número de versión porque
    lo que distingue una redacción de la anterior es su huella.
    """
    salida = redactor_falso(state)
    vieja = salida["redacciones"][0]
    if len(state.get("investigaciones") or ()) > 1:
        return salida
    n = len(state.get("redacciones") or ()) + 1
    usadas = vieja.citas_usadas[:2]
    return {"redacciones": (vieja.model_copy(update={
        "citas_usadas": usadas,
        "texto": f"Redaccion escasa {n} sobre {len(usadas)} citas: {', '.join(usadas)}.",
    }),), "messages": salida["messages"]}


def redactor_sucio(state):
    """Escribe una redacción que la guarda automática rechaza: nunca queda limpia."""
    investigacion = state["investigaciones"][-1]
    verificacion = state["verificaciones"][-1]
    return {"redacciones": (Redaccion(
        texto="Texto con una cita intrusa.", citas_usadas=(), citas_intrusas=("999:9999",),
        motivos=("invoco una cita sin verificar",), limpia=False, llamadas=1,
        sobre_material=huella_material(investigacion, verificacion)),),
        "messages": [("assistant", "[redactor] sucio")]}


def supervisor_falso(state):
    """Delega en orden y cierra cuando hay una redacción vigente y limpia.

    Espeja los dos frenos del supervisor real, incluido el rescate sobre lo verificado: mira
    los intentos con los que entra, así que con el tope alcanzado todavía puede mandar a
    escribir sobre las citas que sí existen.
    """
    situacion = leer_situacion(state)
    vueltas = state.get("vueltas", 0) + 1
    intentos = state.get("intentos", 0)
    if intentos >= cfg.MAXIMO_INTENTOS and (
        situacion.rechazo_pendiente or situacion.reescritura_pendiente
    ):
        # Mismo rescate que el supervisor real: con material verificado suficiente se escribe
        # sobre él en vez de tirar el trabajo.
        if situacion.material_suficiente and not situacion.redaccion_vigente:
            return {"siguiente": "redactor", "completado": False, "intentos": intentos,
                    "vueltas": vueltas,
                    "messages": [("assistant", "[supervisor] sobre lo verificado")]}
        return {"siguiente": "FINALIZAR", "completado": True, "intentos": intentos,
                "vueltas": vueltas,
                "messages": [("assistant", "[supervisor] tope de intentos")]}
    if situacion.investigacion is None or situacion.rechazo_pendiente:
        siguiente = "investigador"
        intentos += 1 if situacion.rechazo_pendiente else 0
    elif not situacion.vigente_verificada:
        siguiente = "verificador"
    elif (situacion.redaccion is None or not situacion.redaccion_vigente
          or situacion.reescritura_pendiente):
        siguiente = "redactor"
        intentos += 1 if situacion.reescritura_pendiente else 0
    else:
        siguiente = "FINALIZAR"
    return {"siguiente": siguiente, "completado": siguiente == "FINALIZAR",
            "intentos": intentos, "vueltas": vueltas,
            "messages": [("assistant", f"[supervisor] -> {siguiente}")]}


def supervisor_con_freno(state):
    """Cierra en cuanto el contador de intentos llega al tope, una vuelta antes que el real.

    Sirve para probar el corte sin recorrer el ciclo entero. El freno real mira los intentos
    *entrantes*, así que llega a evaluar el rescate sobre lo verificado; este cierra sobre los
    salientes y no llega. `supervisor_falso` es el que espeja al real.
    """
    salida = supervisor_falso(state)
    if salida["intentos"] >= cfg.MAXIMO_INTENTOS:
        salida.update({"siguiente": "FINALIZAR", "completado": True})
    return salida


def armar(redactor=redactor_falso, supervisor=supervisor_falso,
          investigador=investigador_falso):
    """Compila el grafo real con los dobles.

    Los cuatro nodos entran por parámetro; lo que queda bajo prueba es el cableado, el ruteo
    y los frenos.
    """
    from app.grafo.construccion import crear_grafo

    return crear_grafo(
        investigador=investigador,
        verificador=verificador_falso,
        redactor=redactor,
        supervisor=supervisor,
    )


def lexico_de_prueba(ruta, fragmentos=(), padron=None, subsecciones=()):
    """Un índice léxico chico y real, escrito en `ruta`.

    Es un SQLite de verdad y no un doble: las consultas que corren en producción son las que
    corren acá, y el costo de armarlo son milisegundos.

    `padron` y `subsecciones` son los atajos para los tests que solo necesitan esos dos datos;
    `fragmentos` es para los que además buscan texto.
    """
    from app.almacen.consulta import Lexico
    from app.almacen.escritura import EscritorLexico

    filas = list(fragmentos)
    for i, nombre in enumerate(subsecciones):
        filas.append({"id": f"sint-{i:04d}", "origen": "sintetico", "seccion": "",
                      "subseccion": nombre, "fuente": "sintetico", "pagina": i,
                      "texto": f"Fragmento sintetico de {nombre}.", "citas_urls": {}})
    with EscritorLexico(ruta) as escritor:
        if filas:
            escritor.escribir_fragmentos(filas)
        if padron:
            for cita, url in padron.items():
                escritor._conexion.execute(
                    "INSERT OR REPLACE INTO padron (cita, url, origen) VALUES (?, ?, 'prueba')",
                    (cita, url))
            escritor._conexion.commit()
    return Lexico(ruta, solo_lectura=True)
