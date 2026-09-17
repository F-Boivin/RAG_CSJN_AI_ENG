"""Números y rutas invariantes del sistema. Los textos viven en mensajes.py.

Lo que cambia entre despliegues —modelos, claves, directorios de datos, cupos— viaja
por entorno en `Ajustes` (config.py). Acá queda lo que es propiedad del sistema y no
del despliegue.
"""

from pathlib import Path

# La raíz del repositorio: constantes.py vive en app/nucleo/.
RAIZ = Path(__file__).parent.parent.parent

# --- Corpus en markdown (el cuadernillo) ---
DIRECTORIO_DATA = RAIZ / "data"
PATRON_DOCUMENTOS = "*.md"

# --- Chunking: los extractos de doctrina son indivisibles ---
TAMANO_CHUNK_TOKENS = 500
SOLAPAMIENTO_CHUNK_TOKENS = 50
# El primer separador es el límite entre extractos de doctrina: el splitter parte ahí
# antes que en cualquier otro lado, así ningún extracto se corta al medio.
SEPARADOR_EXTRACTOS = "\n\n---\n\n"
SEPARADORES = [SEPARADOR_EXTRACTOS, "\n\n", "\n", " ", ""]
# Los PDF no traen el separador de extractos: el corte más grueso disponible es el
# párrafo doble.
SEPARADORES_PDF = ["\n\n\n", "\n\n", "\n", " ", ""]
ETIQUETA_FUENTE = "Fuente:"
METRICA_DISTANCIA = {"hnsw:space": "cosine"}

# --- Recuperación ---
# Seis fragmentos. Medido sobre el cuadernillo con 20 consultas etiquetadas por subsección: con
# cuatro, la subsección que responde entra al top-k en 16 de 20; con seis y con ocho, en 19. Seis
# da la misma cobertura que ocho con un cuarto menos de contexto, y la peor consulta del conjunto
# sigue leyendo diez citas propias.
#
# El corpus llegó a tener 128 documentos, con notas y suplementos, y ahí la recuperación repartía
# lugares entre dos pools para que el volumen de los suplementos no tapara la doctrina. Esa
# medición y ese código están en el historial de git, por si esas fuentes vuelven.
RESULTADOS_RECUPERADOS = 6
# Candidatos que aporta cada lado del ensamble antes de fusionar. Más altos que el
# resultado final a propósito: la fusión elige mejor viendo más de cada uno.
CANDIDATOS_POR_RETRIEVER = 10

# Los dos lados pesan igual, y eso está medido: cada uno gana en un tipo
# de consulta distinto. Con el vocabulario del corpus el léxico llega al 96% de precisión en
# el top-4 y el vectorial al 74%; con la misma pregunta escrita como la escribiría alguien que
# no leyó el corpus, se dan vuelta —50% contra 66%—.
#
# El ensamble en 0.5/0.5 queda en 88% y 59%: mejor que cada lado en el terreno del otro. Desde
# 0.6 hacia el léxico **colapsa al léxico puro** (96% y 50%, los mismos números que sin
# ensamble) y pierde nueve puntos en las parafraseadas, que es como pregunta quien entra a un
# buscador público. Se mide con `scripts.medir_lexico`.
PESO_LEXICO = 0.5
PESO_VECTORIAL = 0.5
MAXIMO_RESULTADOS = 10
LARGO_MAXIMO_FRAGMENTO = 900    # caracteres por fragmento en la observación
# Un fragmento por debajo de esto es un número de página o un renglón de índice.
LARGO_MINIMO_FRAGMENTO = 120
# Proporción de letras que separa la prosa del ruido de extracción. La mediana del corpus es
# 80% y las listas de citas rondan el 60%; el ruido de OCR y los bytes de control caen debajo
# de 40%. Medido: este corte saca 175 fragmentos y cuesta 1 cita de las 5.950 del padrón.
PROPORCION_MINIMA_LETRAS = 0.40
# Pesos por columna de `bm25()` en la búsqueda léxica: texto, citas, título. El título pesa
# más porque nombra el tema del documento, y una consulta suele nombrar el tema.
#
# Medido con 21 consultas etiquetadas (`scripts.medir_lexico`): sin la columna de título la
# precisión en el top-4 es 73%, y con ella 90%. El valor exacto del peso es indiferente entre
# 1 y 8 —los cinco dan 90%— y a partir de 12 empieza a caer. El 3 está en el medio de esa
# meseta, que es donde conviene pararse cuando el corpus todavía puede crecer.
PESOS_BM25 = (1.0, 0.5, 3.0)
LARGO_MAXIMO_CONSULTA = 500

# --- El orquestador: supervisor, especialistas y sus frenos ---
MAXIMO_INTENTOS = 3                 # veces que un especialista puede tener que rehacer su
                                    # trabajo, sumando las dos correcciones posibles (citas
                                    # rechazadas al investigador, cita sin verificar al
                                    # redactor), antes de cerrar con lo que haya
CITAS_MINIMAS = 2                   # por debajo, la respuesta se considera sin fundar
CITAS_HOLGADAS = 3                  # por debajo, la respuesta queda señalada en el registro

# --- El respaldo textual de cada cita ---
# Que la cita exista y que el investigador la haya leído son dos cosas comprobadas. Falta la
# tercera: **que el fragmento diga lo que la afirmación dice que dice**. El investigador copia
# el pasaje que la sostiene y esto lo busca en el texto que se le sirvió, sin modelo de por
# medio, igual que el resto del verificador.
#
# La comparación es tolerante porque el modelo retipea: se normalizan espacios, tildes y caja,
# y si la copia no es literal se mide qué proporción de las palabras del pasaje aparece en el
# fragmento, en orden. Una palabra cambiada sobre quince deja 0,93.
TOLERANCIA_RESPALDO = 0.85
# Y se pide además un tramo seguido, porque la cobertura sola se llena con «la», «de» y «que»
# desparramadas: cuatro palabras consecutivas ya son una frase y no una coincidencia.
PALABRAS_SEGUIDAS_RESPALDO = 4
# Un pasaje corto matchea cualquier cosa. Cuarenta caracteres son unas seis palabras: lo
# mínimo para que el match diga algo.
LARGO_MINIMO_RESPALDO = 40
# Si el respaldo veta o solo informa. Arrancó informando, porque un veto que rechaza de más
# deja al buscador mudo y eso es peor que el problema que arregla. **Medido sobre 20 consultas
# reales antes de encenderlo**: 100 citas verificadas, 1 sin respaldo. El veto toca 1 consulta
# de 20, muy por debajo del 20% que se había fijado como techo para dejarlo apagado.
RESPALDO_OBLIGATORIO = True
LIMITE_RECURSION_AGENTE = 12        # supersteps del ReAct interno del investigador
LIMITE_RECURSION_REDACTOR = 12      # supersteps del ReAct interno del redactor: una vuelta
                                    # por cita para pedir su link, más la redacción final
LIMITE_RECURSION_ORQUESTADOR = 30   # supersteps del grafo. La guarda del supervisor es
                                    # `vueltas > MAXIMO_VUELTAS`, así que la vuelta 13 se
                                    # ejecuta y recién ahí cierra. Cada vuelta arrastra a lo
                                    # sumo un superstep detrás, así que el peor caso es
                                    # 2*(MAXIMO_VUELTAS+1) = 26. Es una red, no la condición
                                    # de corte. Ojo al tocar MAXIMO_VUELTAS: desde 14 la
                                    # fórmula pasa de 30 y la corrida cortaría por
                                    # GraphRecursionError en vez de cerrar ordenada.
MAXIMO_VUELTAS = 12                 # freno general del supervisor. El camino feliz son 4
                                    # vueltas (investigador, verificador, redactor, cierre) y
                                    # cada corrección suma como mucho 2, así que con
                                    # MAXIMO_INTENTOS=3 el peor caso legítimo son 10.
INTENTOS_DECISION = 3               # llamadas al modelo por decisión del supervisor, con
                                    # backoff exponencial y jitter entre una y otra

# --- Fuentes de la Secretaría de Jurisprudencia ---
BASE_SJ = "https://sj.csjn.gov.ar/homeSJ"
URL_HOME_SJ = BASE_SJ + "/notas/inicia"
URL_NOTAS = BASE_SJ + "/notas/"
URL_NOTA_PDF = BASE_SJ + "/notas/nota/{id}/documento"
URL_SUPLEMENTOS_CATEGORIA = BASE_SJ + "/suplementos/categoria/{categoria}/suplementos"
URL_SUPLEMENTO_PDF = BASE_SJ + "/suplementos/suplemento/{id}/documento"
# Plantilla oficial para sintetizar el link de una cita que el PDF no enlaza.
PLANTILLA_LINK_FALLO = (
    "https://sjconsulta.csjn.gov.ar/sjconsulta/consultaSumarios/"
    "buscarTomoPagina.html?tomo={tomo}&pagina={pagina}"
)
HOST_PUBLICO_SJ = "sjconsulta.csjn.gov.ar"
HOST_INTERNO_SJ = "sjintranet.csjn.gov.ar"
# El aplicativo `tomosFallos.do` de `sj.csjn.gov.ar` fue retirado: las 17 citas del padrón que
# lo enlazaban redirigen a la home del sitio con status 200. Se descartan, y la cita recibe el
# link oficial armado con `PLANTILLA_LINK_FALLO`.
PATH_SJ_RETIRADO = "/sj/tomosFallos.do"
HOST_SJ = "sj.csjn.gov.ar"
# Las 17 categorías de suplementos, leídas del HTML de la home. La ingesta vuelve a
# scrapearlas y falla si discrepan: indexar de menos sería silencioso.
CATEGORIAS_SUPLEMENTOS = {
    1: "Interés Superior del Niño",
    2: "Derecho a la Salud",
    4: "Fallos Relevantes",
    5: "Ambiental",
    6: "Archivo Histórico",
    7: "Principio de la reparación plena del daño",
    8: "Libertad de Expresión",
    10: "Citas de Doctrina",
    11: "Delitos de lesa humanidad",
    12: "Profesores Universitarios",
    13: "Derechos de los Consumidores y Usuarios",
    14: "Origen de la doctrina de la arbitrariedad",
    15: "La vulnerabilidad en los precedentes de la Corte Suprema",
    16: "Migraciones - Ley 25.871",
    17: "Art. 19 Constitución Nacional",
    18: "Derechos de las Personas con Discapacidad",
    19: "Restitución internacional de Niños, Niñas y Adolescentes",
}
# Categorías que entran al catálogo y quedan fuera del índice. El «Archivo Histórico» estuvo
# acá mientras se lo creyó un conjunto de transcripciones; son 15 suplementos temáticos de
# ediciones 2009-2016 sobre temas que ningún otro documento cubre —Competencia Originaria,
# Decretos de Necesidad y Urgencia, Habeas Corpus, Derecho Electoral, entre otros—, sin
# hipervínculos pero con sus citas recuperables del texto, igual que la serie Ambiental.
CATEGORIAS_EXCLUIDAS: set[int] = set()

# --- Ingesta ---
SEGUNDOS_ENTRE_DESCARGAS = 1.0
TIMEOUT_DESCARGA = 240.0
REINTENTOS_DESCARGA = 3
AGENTE_HTTP = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
# Una línea presente en más de esta proporción de las páginas de un documento es
# encabezado o pie, y se saca antes de segmentar.
UMBRAL_CROMO = 0.7
# Un outline sirve como fuente de subsecciones mientras sus entradas sean cortas.
MAXIMO_PAGINAS_POR_SUBSECCION = 25
# Cuando no hay outline ni títulos detectables, se corta por bloques de páginas.
PAGINAS_POR_BLOQUE = 10
SEPARADOR_SUBSECCION = " · "

# --- Servicio ---
CONSULTAS_CONCURRENTES = 3      # corridas del grafo simultáneas en el único proceso
SEGUNDOS_LATIDO_SSE = 15        # el proxy de Railway corta a los 5 min sin transferencia
# Topes de espera. Sin ellos, el SDK del proveedor espera diez minutos por llamada y una
# corrida no tiene fin: tres colgadas tapan el semáforo de concurrencia media hora larga, con
# la cola detrás y el cupo de cada uno ya cobrado. Los dos números salen de lo medido —una
# consulta publicada tarda entre 18 y 33 s, y sus llamadas son de segundos— con holgura
# grande: lo que tienen que cortar es lo que se colgó, no lo que tarda.
SEGUNDOS_TIMEOUT_MODELO = 60    # por llamada al proveedor
SEGUNDOS_MAXIMO_CORRIDA = 180   # la corrida entera, de punta a punta
EVENTOS_RETENIDOS = 400         # por corrida, para poder reproducir el stream al reconectar
MINUTOS_RETENCION_CORRIDA = 10  # que la corrida terminada sigue en memoria
HORAS_VENTANA_CUOTA = 24
# Largo mínimo de SAL_VISITANTE. No prueba entropía: descarta la sal corta escrita a mano en
# el panel del proveedor, que se adivina igual que un literal publicado en el repositorio.
LARGO_MINIMO_SAL = 16
# Largo mínimo de TOKEN_RESPALDO, por lo mismo: detrás hay doce meses de consultas.
LARGO_MINIMO_TOKEN = 24
HORAS_RETENCION_PULSOS = 48
DIAS_RETENCION_CONSULTAS = 365

# Los cuatro desenlaces de una consulta.
EN_CURSO = "en_curso"
PUBLICADA = "publicada"
SIN_BASE = "sin_base"
FALLIDA = "fallida"

TITULO_API = "Buscador de jurisprudencia de la CSJN"
VERSION_API = "1.0"
