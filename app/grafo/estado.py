"""Esquema del estado compartido del orquestador.

Cada especialista agrega su artefacto a una tupla, así que las versiones sucesivas quedan
a la vista. Cuando el verificador rechaza y el investigador vuelve a trabajar, el estado
conserva las dos investigaciones, y eso es lo que hace auditable el ciclo de refinamiento.
"""

import hashlib
import json
from typing import Annotated, Literal, NamedTuple, Optional, Tuple

from langgraph.graph import MessagesState
from pydantic import BaseModel, ConfigDict, Field, field_validator

import app.nucleo.constantes as cfg

# Los cuatro destinos que puede elegir el supervisor. Se declara una sola vez: el prompt,
# el tipo de la decisión y el mapeo de las aristas condicionales salen todos de acá.
INVESTIGADOR = "investigador"
VERIFICADOR = "verificador"
REDACTOR = "redactor"
FINALIZAR = "FINALIZAR"
DESTINOS = (INVESTIGADOR, VERIFICADOR, REDACTOR, FINALIZAR)

# El tipo y la tupla se declaran juntos y se controla que no se separen: el Literal no
# puede construirse en runtime desde la tupla, asi que la unica garantia posible es que
# el desajuste falle al importar el modulo y no tres nodos mas adelante.
Destino = Literal["investigador", "verificador", "redactor", "FINALIZAR"]
assert set(Destino.__args__) == set(DESTINOS), "DESTINOS y Destino se desincronizaron"

# El router. Va aparte de DESTINOS porque nadie lo elige como destino: es el nodo al que
# vuelven todos, y el unico que decide.
SUPERVISOR = "supervisor"

# Los tres destinos que son nodos del grafo; FINALIZAR se traduce a END. Se
# controla contra DESTINOS por la misma razon que el Literal: el desajuste tiene que doler
# al importar y no cuando el ruteo no encuentre una clave.
NODOS = (INVESTIGADOR, VERIFICADOR, REDACTOR)
assert set(NODOS) == set(DESTINOS) - {FINALIZAR}, "NODOS y DESTINOS se desincronizaron"


class Cita(BaseModel):
    """Una afirmación del investigador respaldada por un fallo concreto.

    Es el objeto que el verificador contrasta contra los metadatos del corpus. Separar la
    cita de la síntesis es lo que permite comprobarlas una por una, en vez de leer un párrafo
    y confiar.

    **La subsección no está acá a propósito.** La escribía el modelo y el verificador la
    rechazaba cuando no resolvía contra el índice: la consulta insignia gastó sus tres
    correcciones y 40 segundos en eso, con las trece citas verificadas y ninguna inventada.
    Ahora sale del registro de lo recuperado, que sabe qué fragmento trajo cada fallo. Un dato
    que el sistema puede deducir es un dato que el modelo no tiene por qué transcribir.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    fallo: str = Field(min_length=3, max_length=120, description='Ej: "Fallos: 311:2437".')
    afirmacion: str = Field(min_length=10, max_length=400, description="Qué se sostiene con ese fallo.")
    # Vacío por defecto y no obligatorio en el esquema: un respaldo que falta tiene que volver
    # como rechazo corregible del verificador y no como una salida que no valida, que corta la
    # corrida entera sin decirle al investigador qué arreglar.
    respaldo: str = Field(
        default="", max_length=600,
        description=("El pasaje del fragmento que sostiene la afirmación, copiado tal cual. "
                     "Al menos una oración completa."))

    @field_validator("fallo", mode="before")
    @classmethod
    def sin_espacios_en_los_bordes(cls, valor):
        """El fallo llega recortado, porque es la clave con la que el verificador lo busca.

        Con un espacio al final, la corrida entera moría con error: el verificador buscaba la
        cita tal cual y el parser del veredicto la devolvía recortada, así que la guarda de
        cobertura la daba por no respondida. Medido sobre el cuadernillo: el modelo copia las
        listas de citas del corpus, el `max_length` del esquema se las corta y deja un
        «Fallos: » colgando. Pasó en 1 de 20 consultas, y en 1 de 4 repitiendo esa misma.
        """
        return valor.strip() if isinstance(valor, str) else valor


class Investigacion(BaseModel):
    """Lo que el agente de investigación deja en el estado."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sintesis: str = Field(min_length=20, description="Respuesta a la consulta, con la doctrina encontrada.")
    citas: Tuple[Cita, ...] = Field(default_factory=tuple, description="Fallos invocados, uno por afirmación.")
    subsecciones: Tuple[str, ...] = Field(default_factory=tuple, description="Subsecciones consultadas.")

    @field_validator("sintesis")
    @classmethod
    def sin_relleno(cls, valor: str) -> str:
        """Una síntesis en blanco pasa el largo mínimo si viene con espacios."""
        if not valor.strip():
            raise ValueError("la sintesis esta vacia")
        return valor


class Verificacion(BaseModel):
    """El veredicto del verificador sobre las citas de una investigación.

    `aprobado` sale de comparar cada cita contra los metadatos del corpus. Un fallo que no
    está en los metadatos no existe para el sistema, por convincente que suene.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    verificadas: Tuple[str, ...] = Field(default_factory=tuple, description="Fallos que existen en el corpus.")
    inexistentes: Tuple[str, ...] = Field(default_factory=tuple, description="Fallos que el investigador inventó o citó mal.")
    # Existir y venir al caso son dos cosas distintas, y hasta ahora solo se comprobaba la
    # primera. El corpus tiene 9.005 citas reales: una respuesta sobre el IVA se sostuvo en
    # cuatro fallos anteriores a que el IVA existiera, todos ciertos y ninguno leído.
    impertinentes: Tuple[str, ...] = Field(
        default_factory=tuple,
        description="Fallos que existen en el corpus y no salieron de ningún fragmento leído.")
    # La tercera comprobación: el pasaje está en algo que el investigador leyó. Veta cuando
    # `RESPALDO_OBLIGATORIO` está encendido, y con él apagado se calcula igual y viaja al
    # registro: para decidir si un control puede rechazar hace falta saber cuánto rechazaría.
    sin_respaldo: Tuple[str, ...] = Field(
        default_factory=tuple,
        description="Fallos cuyo pasaje de respaldo no aparece en ningún fragmento leído.")
    # Lo que se le muestra al lector: por cada fallo verificado, **el pasaje exacto** que salió
    # de un fragmento que cita ese mismo fallo, como pares `("tomo:pagina", pasaje)`. No
    # bloquea nada. Guarda el pasaje y no solo el fallo porque un fallo sostiene a veces dos
    # afirmaciones con dos pasajes, y marcar el fallo como bueno por uno dejaba a la ficha
    # mostrar el otro: medido, pasó con Mazzeo. Va en positivo para fallar cerrado: una
    # verificación que no lo calculó no le muestra ningún pasaje a nadie.
    pasajes_propios: Tuple[Tuple[str, str], ...] = Field(
        default_factory=tuple,
        description="Pares (fallo, pasaje) cuyo pasaje sale de un fragmento de ese fallo.")
    aprobado: bool
    observaciones: Tuple[str, ...] = Field(default_factory=tuple)

    def model_post_init(self, _context) -> None:
        if not self.aprobado and not self.observaciones:
            raise ValueError("un rechazo tiene que venir con al menos una observacion")


class Redaccion(BaseModel):
    """La respuesta final para el usuario, escrita por el redactor.

    Es la fase de síntesis del equipo: el investigador produce material con sus
    citas, el verificador dictamina cuáles resisten el contraste, y recién entonces alguien
    escribe la respuesta. Separar la síntesis de la búsqueda es lo que permite que el texto
    final se apoye **solo en lo verificado**, y no en todo lo que el investigador dijo.

    `limpia` sale de comparar las citas que aparecen en el texto contra las que el
    verificador aprobó. Un redactor que agrega un fallo nuevo en el último paso rompería la
    promesa del sistema justo después de haberla comprobado. Y estar fundado es citar, así
    que la cuenta de las citas que el texto sí usó también entra en el veredicto.

    `llamadas` viaja en el artefacto y no como argumento suelto porque la línea de la traza se
    arma desde acá: un conteo que llegara por otro lado podría contar otra cosa.

    `sobre_material` es la huella del material del que se escribió, y es lo que permite saber
    si sigue vigente. Es una huella porque lo que invalida una redacción es que **cambie el
    material**: volver a verificar la misma investigación da el mismo veredicto y la deja en
    pie, mientras que una investigación nueva la invalida aunque todavía no se haya
    verificado.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # El piso es 1 y no un número mayor a propósito: "el material verificado no permite
    # responder" es una respuesta legítima y corta, y rechazarla por longitud convertiría una
    # respuesta honesta en una caída del proceso.
    texto: str = Field(min_length=1, description="La respuesta final, en prosa.")
    citas_usadas: Tuple[str, ...] = Field(
        default_factory=tuple,
        description="Citas verificadas que el texto efectivamente invoca.",
    )
    citas_intrusas: Tuple[str, ...] = Field(
        default_factory=tuple,
        description="Citas que aparecen en el texto y el verificador no aprobó.",
    )
    motivos: Tuple[str, ...] = Field(
        default_factory=tuple, description="Por qué se rechazó, si se rechazó."
    )
    limpia: bool = Field(description="True si el texto está fundado solo en citas verificadas.")
    llamadas: int = Field(
        ge=0, description="Llamadas a herramienta que el redactor hizo para producirlo."
    )
    sobre_material: str = Field(
        min_length=8, description="Huella del material del que se redactó."
    )

    def model_post_init(self, _context) -> None:
        if self.limpia and (self.citas_intrusas or self.motivos):
            raise ValueError("una redaccion limpia no puede tener intrusas ni motivos de rechazo")
        if not self.limpia and not self.motivos:
            raise ValueError("un rechazo tiene que venir con al menos un motivo")


def huella_material(investigacion: "Investigacion", verificacion: "Verificacion") -> str:
    """Identifica el material del que se redacta: la síntesis y las citas aprobadas.

    Si los dos son los mismos, la redacción escrita a partir de ellos sigue valiendo. Si
    cambió cualquiera de los dos, no.
    """
    crudo = json.dumps(
        [investigacion.sintesis, sorted(verificacion.verificadas)], ensure_ascii=False
    )
    return hashlib.sha256(crudo.encode("utf-8")).hexdigest()[:16]


def acumular(actual, nuevo):
    """Reducer que acumula: cada aporte nuevo se agrega al final de los anteriores.

    Es lo que permite mostrar en la traza que hubo un rechazo y qué cambió después. Con el
    default de LangGraph, la segunda investigación borraría a la primera y el ciclo de
    refinamiento sería invisible: se vería el resultado, y no el recorrido.

    Todo se normaliza a tupla antes de concatenar, así una lista que llegue de una capa
    serializada entra igual que una tupla.
    """
    if nuevo is None:
        return tuple(actual or ())
    anteriores = tuple(actual or ())
    agregados = tuple(nuevo) if isinstance(nuevo, (tuple, list)) else (nuevo,)
    return anteriores + agregados


def fusionar(actual, nuevo):
    """Reducer que fusiona diccionarios conservando la primera procedencia de cada clave.

    El registro de lo recuperado crece a lo largo de la corrida: el investigador puede buscar
    varias veces, y una corrección lo hace buscar de nuevo. Lo que se acumula es qué fragmento
    trajo cada fallo, y la primera respuesta es la que vale: un fallo puede aparecer después en
    otro fragmento sin que eso cambie de dónde salió cuando el investigador lo leyó.
    """
    if not nuevo:
        return dict(actual or {})
    fusionado = dict(nuevo)
    fusionado.update(actual or {})
    return fusionado


def unir(actual, nuevo):
    """Reducer que une listas por clave, sin repetir y en el orden en que llegaron.

    Es el registro de en qué fragmentos aparece cada fallo, y ahí la primera respuesta no
    alcanza: un mismo fallo está citado en varios lugares del corpus, y el pasaje que lo
    respalda puede estar en cualquiera de los que el investigador leyó. Quedarse con el primero
    rechazaría un pasaje cierto por haber llegado en la segunda búsqueda.

    Copia en vez de mutar, por la misma razón que `fusionar`: la lista que devuelve el nodo
    sigue viva en la herramienta de la corrida.
    """
    unido = {clave: list(valores) for clave, valores in (actual or {}).items()}
    for clave, valores in (nuevo or {}).items():
        lista = unido.setdefault(clave, [])
        for valor in valores:
            if valor not in lista:
                lista.append(valor)
    return unido


class EstadoOrquestador(MessagesState):
    """Estado global del grafo.

    Hereda de `MessagesState`, así que trae `messages` con el reducer `add_messages` ya
    puesto. Los campos propios:

    - `consulta`: la pregunta del usuario. Entrada, nadie la reescribe.
    - `siguiente`: la decisión del supervisor. Va SIN reducer a propósito: se pisa en cada
      vuelta, que es exactamente lo que se quiere de un campo de ruteo.
    - `investigaciones` / `verificaciones` / `redacciones`: acumulan. Una entrada por
      intento. La respuesta vigente es `redacciones[-1].texto`; acumula por la misma razón
      que las investigaciones — con un campo plano, la segunda redacción borraría a la
      primera y el refinamiento sería invisible.
    - `intentos`: cuántas veces un especialista tuvo que rehacer su trabajo tras un
      rechazo, sumando los dos que pueden recibirlo (el investigador cuando el verificador
      le rechaza las citas, el redactor cuando invoca una cita sin verificar). **Se
      incrementa en un solo lugar: el nodo supervisor, y solo cuando esa vuelta manda a
      rehacer un artefacto que ya fue juzgado.** Sin esa precisión la guarda que lo compara
      no se puede leer.
    - `vueltas`: cuántas veces se ejecutó el supervisor. **Se incrementa en el nodo
      supervisor, en cada ejecución, sin condición.** Es el freno que cubre TODAS las
      ramas: `intentos` solo acota el ciclo de corrección, y un supervisor que delegara
      en círculos sin que nadie rechace nada no lo tocaría nunca.
    - `completado`: el veredicto explícito que lee la arista de cierre. Que el nodo escriba
      su conclusión y la arista solo la lea evita que la condición tenga que re-deducirla.
    - `recuperado`: qué fragmento trajo cada fallo, `"tomo:pagina" -> subsección`. Lo escribe
      la herramienta de búsqueda mientras el investigador trabaja, y acumula durante toda la
      corrida. **Es lo que separa una cita pertinente de una cita meramente cierta**: el
      verificador rechaza el fallo que existe en el corpus y no salió de ningún fragmento
      leído, y la subsección de cada cita se deduce de acá en vez de escribirla el modelo.
    - `textos_leidos`: el texto de cada fragmento que la búsqueda sirvió, `huella -> texto`,
      exactamente como el investigador lo vio. Va aparte de `recuperado` porque responde otra
      pregunta: uno dice qué fallos estuvieron disponibles, y este, qué decía el texto.
    - `fragmentos_por_cita`: en qué fragmentos apareció cada fallo, `"tomo:pagina" -> [huella]`.
      Decide **si el lector ve el pasaje de una cita**: solo cuando sale de uno de estos
      fragmentos. Buscarlo en todo lo leído dejaba pasar una cita leída en un documento con una
      oración leída en otro: medido en producción, 4 de 14 pasajes publicados venían de un
      documento distinto al que citaba el fallo, y la ficha los mostraba como su respaldo.
    """

    consulta: str
    siguiente: Destino
    investigaciones: Annotated[Tuple[Investigacion, ...], acumular]
    verificaciones: Annotated[Tuple[Verificacion, ...], acumular]
    redacciones: Annotated[Tuple[Redaccion, ...], acumular]
    recuperado: Annotated[dict, fusionar]
    textos_leidos: Annotated[dict, fusionar]
    fragmentos_por_cita: Annotated[dict, unir]
    intentos: int
    vueltas: int
    completado: bool


def ultima_investigacion(estado: EstadoOrquestador) -> Optional[Investigacion]:
    """La investigación vigente, o None si el investigador todavía no trabajó."""
    investigaciones = estado.get("investigaciones") or ()
    return investigaciones[-1] if investigaciones else None


def ultima_verificacion(estado: EstadoOrquestador) -> Optional[Verificacion]:
    """El último veredicto del verificador, o None si todavía no revisó."""
    verificaciones = estado.get("verificaciones") or ()
    return verificaciones[-1] if verificaciones else None


def ultima_redaccion(estado: EstadoOrquestador) -> Optional[Redaccion]:
    """La respuesta final vigente, o None si el redactor todavía no escribió."""
    redacciones = estado.get("redacciones") or ()
    return redacciones[-1] if redacciones else None


class Calidad(NamedTuple):
    """Qué tan bien le fue al sistema en este trabajo, en las señales que él mismo produce.

    Es telemetría: viaja al registro de consultas y al evento de cierre del stream. `senales`
    resume los umbrales que el trabajo tocó, y ninguno de ellos decide si se publica — eso lo
    decide `Situacion.listo`, que mira el texto final.
    """

    citas_propuestas: int
    citas_verificadas: int
    citas_inexistentes: int
    citas_impertinentes: int
    citas_sin_respaldo: int
    citas_con_pasaje_ajeno: int
    cobertura: float
    citas_en_el_texto: int
    intentos: int
    motivos: Tuple[str, ...]
    senales: bool


def evaluar_calidad(estado: EstadoOrquestador, holgadas: int,
                    tope_intentos: int) -> Calidad:
    """Mide el trabajo terminado y describe con qué holgura llegó.

    Se calcula solo del estado, sin reloj ni azar, así que dos lecturas del mismo estado dan
    lo mismo.

    **Mide el historial completo, no la última pasada.** Una verificación aprobada no tiene
    citas inexistentes: leer solo la última daría cobertura perfecta en todos los trabajos.
    Lo que distingue a un trabajo difícil de uno fácil es cuántas veces hubo que corregirlo,
    y eso vive en las versiones anteriores que el reducer `acumular` conserva.

    Las tres señales:

    - **El investigador citó fallos que no existen** en algún momento del recorrido. Las
      corrigió, y el material lo llevó a inventar.
    - **Citó fallos ciertos que no había leído.** El recorrido los rechazó, y saber cuántos
      hubo es saber cuánto empuja el material hacia la cita ajena.
    - **Alguna cita quedó sin un pasaje que la respalde.** Mientras `RESPALDO_OBLIGATORIO`
      esté apagado esta señal no bloquea nada, y es justamente para eso que se cuenta: la
      decisión de encenderla necesita saber cuánto rechazaría.
    - **Alguna cita se publicó con un pasaje de otro fragmento.** El pasaje está en lo leído
      pero no en un fragmento que cite ese fallo, así que el lector no lo ve. No bloquea: se
      probó vetar con esa vara y duplicó la latencia. Contarlo es lo que dice cuánto se pierde
      de pasaje a la vista, y si algún día conviene volver a intentarlo.
    - **La respuesta se apoya en pocas citas.** Cumple el mínimo, sin margen.
    - **Se agotaron los intentos de corrección.** El sistema cerró con lo que tenía.

    Un trabajo puede publicarse con señales encendidas: quedan anotadas en el registro, que
    es de donde sale saber qué consultas conviene mirar.
    """
    verificaciones = estado.get("verificaciones") or ()
    redaccion = ultima_redaccion(estado)

    verificadas = sum(len(v.verificadas) for v in verificaciones)
    inexistentes = sum(len(v.inexistentes) for v in verificaciones)
    impertinentes = sum(len(v.impertinentes) for v in verificaciones)
    # Solo la vigente: las anteriores fueron rechazadas y sus citas ya no están en la
    # respuesta, así que contarlas mediría un texto que nadie va a leer.
    sin_respaldo = len(verificaciones[-1].sin_respaldo) if verificaciones else 0
    pasaje_ajeno = (len(verificaciones[-1].verificadas) - len(verificaciones[-1].pasajes_propios)
                    if verificaciones else 0)
    # Las tres cuentan lo mismo: los fallos que el verificador juzgó en todo el recorrido.
    # Leer las propuestas de la última investigación las ponía en otra escala, y el registro
    # llegaba a decir "2 propuestas, 3 verificadas".
    propuestas = verificadas + inexistentes + impertinentes
    cobertura = verificadas / propuestas if propuestas else 0.0
    en_el_texto = len(redaccion.citas_usadas) if redaccion else 0
    intentos = estado.get("intentos", 0)

    motivos = []
    if inexistentes:
        motivos.append(
            f"el investigador citó {inexistentes} de {propuestas} fallos que no existen en el "
            f"corpus (cobertura {cobertura:.0%})"
        )
    if en_el_texto < holgadas:
        motivos.append(
            f"la respuesta se apoya en {en_el_texto} citas, por debajo de las {holgadas} "
            f"que dan margen"
        )
    if impertinentes:
        motivos.append(
            f"el investigador citó {impertinentes} fallos ciertos que no había leído"
        )
    if sin_respaldo:
        motivos.append(
            f"{sin_respaldo} cita(s) quedaron sin un pasaje del corpus que las respalde"
        )
    if pasaje_ajeno:
        motivos.append(
            f"{pasaje_ajeno} cita(s) con un pasaje de otro fragmento: se publican sin mostrarlo"
        )
    if intentos >= tope_intentos:
        motivos.append(f"se agotaron los {tope_intentos} intentos de corrección")

    return Calidad(
        citas_propuestas=propuestas,
        citas_verificadas=verificadas,
        citas_inexistentes=inexistentes,
        citas_impertinentes=impertinentes,
        citas_sin_respaldo=sin_respaldo,
        citas_con_pasaje_ajeno=pasaje_ajeno,
        cobertura=cobertura,
        citas_en_el_texto=en_el_texto,
        intentos=intentos,
        motivos=tuple(motivos),
        senales=bool(motivos),
    )


class Situacion(NamedTuple):
    """La lectura del estado que usan el resumen del prompt y los frenos.

    Se calcula una sola vez y la consumen los dos, así que el texto que ve el supervisor y
    la condición que lo frena dicen siempre lo mismo. Cuando cada uno deduce la situación por
    su cuenta, tarde o temprano dejan de coincidir y el sistema cuenta una cosa mientras hace
    otra.
    """

    investigacion: Optional[Investigacion]
    verificacion: Optional[Verificacion]
    redaccion: Optional[Redaccion]
    n_investigaciones: int
    n_verificaciones: int
    vigente_verificada: bool
    redaccion_vigente: bool
    rechazo_pendiente: bool
    material_suficiente: bool
    reescritura_pendiente: bool
    listo: bool


def leer_situacion(state: EstadoOrquestador,
                   minimas: int = cfg.CITAS_MINIMAS) -> Situacion:
    """Traduce el estado crudo a las preguntas que el supervisor necesita responder.

    `minimas` es el piso de citas verificadas para poder escribir una respuesta. Viene por
    parámetro con el valor configurado como default: quien mide con otro umbral lo pasa, y
    nadie tiene que repetir la constante.
    """
    investigaciones = state.get("investigaciones") or ()
    verificaciones = state.get("verificaciones") or ()
    investigacion = ultima_investigacion(state)
    verificacion = ultima_verificacion(state)
    redaccion = ultima_redaccion(state)

    # El dato que decide el próximo paso es si la investigación VIGENTE ya pasó por el
    # verificador, y no si hubo un rechazo. Sin esta comparación el supervisor no distingue
    # "rechazada y sin corregir" de "rechazada y ya corregida", y vuelve a mandar a
    # investigar sobre una corrección que nunca se revisó.
    vigente_verificada = len(verificaciones) >= len(investigaciones)

    # Y una redacción sigue valiendo mientras el material del que se escribió siga igual. Se
    # compara la huella del material: volver a verificar la misma investigación da el mismo
    # veredicto y la deja en pie, mientras que una investigación nueva la invalida aunque
    # todavía no se haya verificado — caso en que un contador de vueltas ni se habría movido.
    redaccion_vigente = (
        redaccion is not None
        and investigacion is not None
        and verificacion is not None
        and vigente_verificada
        and redaccion.sobre_material == huella_material(investigacion, verificacion)
    )

    # Escribir la respuesta necesita **citas verificadas suficientes**, y eso es más débil que
    # una investigación aprobada. El verificador rechaza la tanda entera cuando una sola cita
    # es inventada, aunque las otras existan; el redactor, en cambio, solo recibe
    # `verificacion.verificadas`, y el control del texto final lo compara contra esa misma
    # lista. Publicar sobre el subconjunto verificado conserva todas las garantías: lo que se
    # pierde con la regla estricta es una respuesta buena, no una salvaguarda.
    #
    # El rechazo sigue mandando a corregir mientras queden intentos. Lo que cambia es el
    # desenlace cuando se agotan: el corte pasa a ser «no hay nada verificable» en vez de
    # «alguna vez se inventó algo».
    suficiente = (
        verificacion is not None
        and vigente_verificada
        and len(verificacion.verificadas) >= minimas
    )
    return Situacion(
        investigacion=investigacion,
        verificacion=verificacion,
        redaccion=redaccion,
        n_investigaciones=len(investigaciones),
        n_verificaciones=len(verificaciones),
        vigente_verificada=vigente_verificada,
        redaccion_vigente=redaccion_vigente,
        rechazo_pendiente=(
            verificacion is not None and not verificacion.aprobado and vigente_verificada
        ),
        material_suficiente=suficiente,
        reescritura_pendiente=suficiente and redaccion_vigente and not redaccion.limpia,
        # La barra de publicación: hay citas verificadas suficientes, la redacción es vigente
        # sobre ese material, y el texto se apoya solo en ellas. El servicio la lee del estado
        # final para decidir entre publicar y contestar que no hay base.
        listo=suficiente and redaccion_vigente and redaccion.limpia,
    )
