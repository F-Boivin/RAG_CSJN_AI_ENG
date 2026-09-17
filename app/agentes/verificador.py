"""Nodo de análisis: dictamina si cada cita existe comparándola contra el padrón del corpus.

Es código y no llama a ningún LLM: un fallo está entre las 574 del padrón o no está, y eso se
computa. Poner un modelo a juzgarlo mudaría la alucinación al que audita. Por eso es un nodo y
no un agente — la documentación reserva ese nombre para "a model calling tools in a loop":
https://docs.langchain.com/oss/python/langchain/agents

Es un nodo del grafo y no una herramienta del supervisor: verificar es un paso obligatorio del
recorrido, y una herramienta la invoca quien quiere.
"""

import unicodedata
from difflib import SequenceMatcher

import app.nucleo.constantes as cfg
import app.nucleo.mensajes as msj
import app.rag.herramientas as herramientas
from app.nucleo.errores import ErrorDeAgente
from app.grafo.estado import EstadoOrquestador, Investigacion, Verificacion, ultima_investigacion


async def verificador_node(state: EstadoOrquestador) -> dict:
    """Contrasta las citas de la última investigación contra el padrón del corpus."""
    investigacion = ultima_investigacion(state)
    if investigacion is None:
        raise ErrorDeAgente(msj.ERROR_VERIFICADOR_SIN_INVESTIGACION)

    veredicto = await verificar(investigacion, state.get("recuperado") or {},
                                state.get("textos_leidos") or {},
                                state.get("fragmentos_por_cita") or {})
    propuestas = (len(veredicto.verificadas) + len(veredicto.inexistentes)
                  + len(veredicto.impertinentes))
    return {
        "verificaciones": (veredicto,),
        "messages": [
            (
                "assistant",
                f"[verificador] {len(veredicto.verificadas)} de {propuestas} citas existen "
                f"en el corpus y salieron de lo que el investigador leyó. "
                f"{'APROBADO' if veredicto.aprobado else 'RECHAZADO'}.",
            )
        ],
    }


def _aplanar(texto: str) -> str:
    """Deja el texto en lo que sobrevive a que alguien lo vuelva a tipear.

    Espacios colapsados, tildes afuera y todo en minúscula. El PDF corta palabras con guión,
    parte las oraciones en dos renglones y mete el número de página en el medio; el modelo, al
    copiar el pasaje, arregla algunas de esas cosas y no otras. Comparar sobre esta forma es
    comparar lo que las dos versiones tienen en común de verdad.
    """
    plano = unicodedata.normalize("NFKD", (texto or "").lower())
    plano = "".join(letra for letra in plano if not unicodedata.combining(letra))
    return " ".join(plano.split())


def respaldado(respaldo: str, textos) -> bool:
    """Si ese pasaje aparece en alguno de los fragmentos que se le pasan.

    Quien llama decide qué fragmentos cuentan, y el verificador le pasa solo los que traen el
    fallo que el pasaje dice respaldar.

    Primero la contención literal, que es el caso normal. Cuando el retipeo la rompe, se
    comparan **listas de palabras** y no cadenas de caracteres: una palabra cambiada en el
    medio parte el bloque contiguo en dos mitades, y medir contra la más larga daría por no
    respaldado un pasaje que está casi entero. Por palabras, esa misma copia conserva 14 de 15.

    Dos condiciones, y las dos hacen falta. **La cobertura** —qué proporción de las palabras
    del pasaje aparece, en orden, en el fragmento— es la que mide el parecido. **El tramo
    contiguo más largo** es la que impide que un texto jurídico cualquiera respalde cualquier
    cosa: sin ella, «la», «de», «que» y «el» desparramadas suman cobertura sin que haya una
    sola frase en común.

    Se pide además un largo mínimo, porque un pasaje de tres palabras aparece en cualquier
    lado y darlo por respaldado sería firmar un cheque en blanco.
    """
    aguja = _aplanar(respaldo)
    if len(aguja) < cfg.LARGO_MINIMO_RESPALDO:
        return False
    palabras = aguja.split()
    for texto in textos:
        pajar = _aplanar(texto)
        if aguja in pajar:
            return True
        bloques = SequenceMatcher(
            None, palabras, pajar.split(), autojunk=False).get_matching_blocks()
        cobertura = sum(b.size for b in bloques) / len(palabras)
        seguido = max((b.size for b in bloques), default=0)
        if cobertura >= cfg.TOLERANCIA_RESPALDO and seguido >= cfg.PALABRAS_SEGUIDAS_RESPALDO:
            return True
    return False


def citas_distintas(investigacion: Investigacion) -> list[str]:
    """Los fallos que la investigación invoca, uno por fallo y en el orden en que aparecen.

    **Una cita es un fallo, no una afirmación.** El investigador propone el mismo fallo varias
    veces cuando sostiene varios puntos con él, y eso es correcto. Contarlo una vez por
    afirmación mentía en las tres puntas: le decía al usuario "4 de 4 citas existen en el
    corpus" sobre un solo fallo, daba por cumplido el mínimo de `CITAS_MINIMAS` con duplicados,
    y mandaba a corregir tres veces a un redactor que escribía la única cita que tenía. Medido
    con «habeas corpus colectivo», donde el corpus responde con Verbitsky y nada más.

    Se agrupa por tomo y página, que es lo que identifica una cita, conservando la forma en que
    se escribió la primera. Un `fallo` sin ningún número no tiene con qué agruparse y se
    compara entero: dos textos distintos son dos problemas distintos, y cada uno necesita su
    observación.
    """
    distintas: dict[str, str] = {}
    for cita in investigacion.citas:
        clave = herramientas.normalizar_cita(cita.fallo) or cita.fallo
        distintas.setdefault(clave, cita.fallo)
    return list(distintas.values())


async def verificar(investigacion: Investigacion,
                    recuperado: dict[str, str] | None = None,
                    textos: dict[str, str] | None = None,
                    fragmentos_por_cita: dict[str, list[str]] | None = None) -> Verificacion:
    """Devuelve el veredicto sobre una investigación.

    Tres comprobaciones y no una. **Que la cita exista** se contrasta contra el padrón, las
    citas del corpus con su link oficial. **Que venga al caso** se contrasta contra `recuperado`, el
    registro de qué fragmento trajo cada fallo durante esta corrida. **Que el pasaje la
    respalde** se contrasta contra el texto de los fragmentos donde aparece ese fallo, y de
    ningún otro.

    Las dos últimas faltaban. Sin la segunda se publicó una respuesta sobre el IVA sostenida en
    cuatro fallos anteriores a que el IVA existiera, todos en el padrón y ninguno leído. Y la
    tercera, mientras buscó el pasaje en todo lo leído, dejó publicar una cita de un documento
    con el respaldo de otro.

    Separada del nodo para poder probarla sola, sin montar el grafo.
    """
    recuperado = recuperado or {}
    textos = textos or {}
    fragmentos_por_cita = fragmentos_por_cita or {}

    def textos_de(fallo: str) -> list[str]:
        """El texto de los fragmentos donde aparece ese fallo, y de ningún otro."""
        clave = herramientas.normalizar_cita(fallo)
        return [textos[h] for h in fragmentos_por_cita.get(clave, ()) if h in textos]

    if not investigacion.citas:
        # Una síntesis sin citas es inverificable, que es distinto de tener las citas mal:
        # se rechaza diciendo exactamente eso.
        return Verificacion(
            aprobado=False,
            observaciones=("la sintesis no trae ninguna cita: no hay nada que verificar",),
        )

    # Las citas se recortan una vez, a la entrada, y todo lo que sigue compara contra esa forma.
    # `Cita.fallo` ya llega recortado cuando pasa por su validador; esto cubre al artefacto que se
    # arma sin validar. Sin eso la guarda de cobertura buscaba la cita con su espacio al final
    # contra un veredicto que la devuelve recortada, y mataba la corrida en vez de rechazarla.
    investigacion = investigacion.model_copy(update={"citas": tuple(
        c.model_copy(update={"fallo": c.fallo.strip()}) for c in investigacion.citas)})

    afirmadas = citas_distintas(investigacion)

    # Una cita por afirmacion. Si el `fallo` amontona varias -"Fallos: 313:1045; 328:4597",
    # que es como las escribe el cuadernillo-, normalizarlo se queda con la primera y las
    # demas quedarian por comprobadas sin que nadie las haya mirado.
    amontonadas = sorted({c.fallo for c in investigacion.citas
                          if herramientas.cuantas_citas(c.fallo) > 1})

    # Un campo `fallo` sin ningún número reconocible es otro problema, y necesita otro
    # mensaje: decirle "separá las citas" a quien no escribió ninguna lo manda a corregir algo
    # que no hizo.
    sin_numero = sorted({c.fallo for c in investigacion.citas
                         if herramientas.cuantas_citas(c.fallo) == 0})

    # Las citas escritas en la prosa de la sintesis se comprueban igual que las de la lista.
    # Un verificador que solo mira la lista deja pasar cualquier numero escrito al costado, y
    # esa sintesis es despues el material del que redacta el ultimo agente.
    de_la_sintesis = sorted(herramientas.citas_del_texto(investigacion.sintesis))

    a_comprobar = list(dict.fromkeys(afirmadas + de_la_sintesis))
    try:
        crudo = await herramientas.verificar_citas.ainvoke({"citas": a_comprobar})
    except Exception as exc:
        raise ErrorDeAgente(msj.ERROR_VERIFICADOR.format(detalle=f"{type(exc).__name__}: {exc}")) from exc

    veredicto = herramientas.leer_veredicto(crudo)
    faltantes = [c for c in a_comprobar if c not in veredicto]
    if faltantes:
        # Un veredicto que no cubre todas las citas es una respuesta rota, y leerlo como
        # "no existen" seria inventar el peor veredicto posible.
        raise ErrorDeAgente(
            msj.ERROR_VERIFICADOR.format(
                detalle=f"la respuesta no cubre {len(faltantes)} de {len(a_comprobar)} citas"
            )
        )
    # Existir y venir al caso se separan acá, y el orden importa: una cita que no existe es un
    # problema distinto de una que existe y el investigador no leyó, y cada una necesita su
    # observación para que la corrección sepa qué arreglar.
    def leyo(cita: str) -> bool:
        return herramientas.normalizar_cita(cita) in recuperado

    verificadas = tuple(c for c in afirmadas if veredicto.get(c) and leyo(c))
    inexistentes = tuple(c for c in afirmadas if not veredicto.get(c))
    impertinentes = tuple(c for c in afirmadas if veredicto.get(c) and not leyo(c))
    sueltas_inventadas = tuple(c for c in de_la_sintesis if not veredicto.get(c))
    sueltas_ajenas = tuple(c for c in de_la_sintesis if veredicto.get(c) and not leyo(c))

    observaciones = [
        f"'{cita.fallo}' no figura entre las citas del corpus; sostiene: {cita.afirmacion[:90]}"
        for cita in investigacion.citas
        if cita.fallo in inexistentes
    ]
    observaciones += [
        f"'{cita.fallo}' existe en el corpus pero no salio de ningun fragmento que hayas "
        f"leido, asi que no podes sostener con el: {cita.afirmacion[:90]}. Busca de nuevo, o "
        f"sacá la afirmacion"
        for cita in investigacion.citas
        if cita.fallo in impertinentes
    ]
    for cita in amontonadas:
        observaciones.append(
            f"'{cita}' junta varias citas en una: escribi una cita por afirmacion, con un "
            f"solo numero de fallo"
        )
    for cita in sin_numero:
        observaciones.append(
            f"'{cita}' no tiene ningun numero de fallo: escribilo en la forma "
            f"\"Fallos: tomo:pagina\""
        )
    for cita in sueltas_inventadas:
        observaciones.append(
            f"la sintesis menciona el fallo '{cita}', que no figura en el corpus: sacalo del "
            f"texto o reemplazalo por uno real"
        )
    for cita in sueltas_ajenas:
        observaciones.append(
            f"la sintesis menciona el fallo '{cita}', que existe en el corpus y no salio de "
            f"ningun fragmento que hayas leido: sacalo del texto"
        )

    # La tercera comprobacion: que el fragmento diga lo que la afirmacion dice que dice. Se
    # mira solo sobre las citas que ya pasaron las dos anteriores, porque acusar de mal
    # respaldada a una cita que ademas no existe es amontonar dos problemas en un mensaje.
    # Alcanza con que uno de sus pasajes resista: un fallo puede estar citado en dos lugares
    # del corpus, y exigir que todas sus afirmaciones salgan del mismo fragmento seria pedir
    # algo que el corpus no siempre permite.
    #
    # Dos varas, y cada una decide una cosa distinta. **La que veta busca el pasaje en todo
    # lo leido**: un pasaje que no esta en ningun texto servido es un pasaje inventado, y esa
    # cita no se publica. **La que decide si el lector ve el pasaje lo busca solo en los
    # fragmentos que citan ese fallo**, y va mas abajo.
    #
    # Se probo vetar con la vara estricta y se midio sobre 20 consultas: cero pasajes ajenos,
    # pero 31 correcciones contra 9, la latencia mediana de 18 a 40 segundos, y tres consultas
    # que el corpus si responde terminaron sin base. En los suplementos el fragmento que trata
    # el tema casi nunca cita el fallo —el 66% no cita ninguno—, asi que la vara estricta los
    # dejaba sin nada con que respaldar.
    leidos = list(textos.values())
    sin_respaldo = tuple(
        c for c in verificadas
        if not any(respaldado(r, leidos) for r in _respaldos_de(investigacion, c))
    )
    if cfg.RESPALDO_OBLIGATORIO:
        # **Sale de `verificadas`, y no alcanza con rechazar la tanda.** Agotadas las tres
        # correcciones, el sistema publica sobre el subconjunto verificado en vez de tirar
        # todo: una cita que solo baja `aprobado` vuelve igual al redactor en esa ultima
        # vuelta. Sacarla de la lista es lo que la deja afuera de la respuesta —y lo que hace
        # que una consulta sin nada respaldado termine en «no hay base suficiente»—, que es
        # el mismo camino que ya siguen las citas ajenas al tema.
        verificadas = tuple(c for c in verificadas if c not in sin_respaldo)

    # La vara estricta: el pasaje esta en un fragmento que cita ese mismo fallo. No veta —ver
    # arriba por que— y decide algo mas acotado: **que pasaje le muestra la ficha al lector**.
    # En produccion, 4 de 14 pasajes publicados venian de un documento distinto al que citaba
    # el fallo, y la ficha los presentaba como su respaldo. Una cita sin pasaje propio se
    # publica igual, con su afirmacion y su link oficial, y sin pasaje a la vista.
    #
    # **Se guarda el pasaje, no el fallo.** Un fallo puede sostener dos afirmaciones con dos
    # pasajes, uno propio y otro ajeno; marcar el fallo como bueno por el primero dejaba que la
    # ficha mostrara el segundo. Medido: Mazzeo, 330:3248, salio con un pasaje sobre la
    # Convencion de imprescriptibilidad que no estaba en ninguno de sus 26 fragmentos.
    pasajes_propios = []
    for fallo in verificadas:
        propios = [r for r in _respaldos_de(investigacion, fallo) if respaldado(r, textos_de(fallo))]
        if propios:
            pasajes_propios.append((herramientas.normalizar_cita(fallo), propios[0]))
    for cita in sin_respaldo:
        afirmacion = next((c.afirmacion for c in investigacion.citas if c.fallo == cita), "")
        observaciones.append(
            f"'{cita}' no viene con un pasaje que lo respalde: copia, del mismo fragmento "
            f"donde aparece ese fallo, la oracion que sostiene «{afirmacion[:70]}», tal como "
            f"esta escrita. Una oracion de otro fragmento no respalda a este fallo"
        )

    aprobado = not (inexistentes or impertinentes or amontonadas or sin_numero
                    or sueltas_inventadas or sueltas_ajenas
                    or (sin_respaldo and cfg.RESPALDO_OBLIGATORIO))
    if aprobado and len(verificadas) < cfg.CITAS_MINIMAS:
        # Todas ciertas pero pocas: la respuesta es correcta y floja. Es un rechazo
        # distinto del anterior, y el investigador tiene que poder distinguirlos.
        aprobado = False
        observaciones.append(
            f"solo {len(verificadas)} cita(s) verificada(s); se piden al menos "
            f"{cfg.CITAS_MINIMAS} para dar la respuesta por fundada"
        )

    return Verificacion(
        verificadas=verificadas,
        inexistentes=inexistentes,
        impertinentes=impertinentes,
        sin_respaldo=sin_respaldo,
        pasajes_propios=tuple(pasajes_propios),
        aprobado=aprobado,
        observaciones=tuple(observaciones),
    )


def _respaldos_de(investigacion: Investigacion, fallo: str) -> list[str]:
    """Los pasajes con los que la investigación sostiene ese fallo.

    Varios, porque el mismo fallo puede sostener varias afirmaciones y cada una trae el suyo.
    Se agrupan por tomo y página, igual que en `citas_distintas`: la lista de verificadas
    conserva la primera forma en que se escribió el fallo, y las demás pueden venir escritas
    distinto.
    """
    clave = herramientas.normalizar_cita(fallo) or fallo
    return [c.respaldo for c in investigacion.citas
            if (herramientas.normalizar_cita(c.fallo) or c.fallo) == clave]
