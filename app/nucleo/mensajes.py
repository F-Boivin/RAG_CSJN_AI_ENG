"""Plantillas de texto del sistema: avisos, observaciones para los modelos y errores.

Separadas de constantes.py porque cambian por razones distintas: un número se toca
midiendo, un texto se toca leyéndolo. Las que llevan `{}` se formatean en el punto de
uso.
"""

# --- Ingesta ---
MENSAJE_INDICE_EXISTENTE = (
    "Índice existente en {ruta}: {cantidad} fragmentos ya indexados, no se reindexa."
)
MENSAJE_INDEXANDO = "No hay índice previo en {ruta}: indexando por primera vez."
MENSAJE_DOCUMENTOS_LEIDOS = "Documentos leídos: {cantidad} ({tokens} tokens en total)"
MENSAJE_CHUNKS = (
    "Fragmentos generados: {cantidad} · tokens por fragmento: "
    "mín {minimo}, promedio {promedio}, máx {maximo} (techo configurado: {techo})"
)
MENSAJE_INDICE_LISTO = "Índice listo: {cantidad} fragmentos en la colección '{coleccion}'."
MENSAJE_DOCUMENTO_INDEXADO = (
    "{origen}: {fragmentos} fragmentos, {citas} citas al padrón "
    "({subsecciones} subsecciones por {metodo})"
)
MENSAJE_DOCUMENTO_SIN_CAMBIOS = "{origen}: sin cambios desde la última ingesta."

ERROR_DESCARGA = "No se pudo descargar {url}: {detalle}"
ERROR_NO_ES_PDF = (
    "{url} respondió {estado} con content-type {tipo} y el cuerpo no empieza con %PDF-. "
    "El sitio devuelve HTML de error con status 200, así que esto se controla siempre."
)
ERROR_CATEGORIAS_DISCREPAN = (
    "Las categorías de suplementos del sitio ({sitio}) difieren de las declaradas en "
    "constantes ({declaradas}). Actualizá CATEGORIAS_SUPLEMENTOS antes de seguir."
)
ERROR_CATALOGO_AUSENTE = (
    "Falta {ruta}. Corré `python -m scripts.construir_indice catalogar` para armarlo."
)
ERROR_PDF_ILEGIBLE = "No se pudo abrir el PDF de {origen}: {detalle}"
ERROR_INDICE_IMPRESO_ILEGIBLE = (
    "No se pudo leer el índice impreso de {origen} en «{renglon}»: {motivo}. Cortar sin ese "
    "título dejaría el texto que le sigue con la subsección equivocada."
)
MOTIVO_NUMERACION_SALTEADA = "la numeración no sigue a «{anterior}»"
MOTIVO_RENGLON_SIN_PAGINA = "el índice termina sin la página de ese título"
ERROR_TITULO_SIN_UBICAR = (
    "El índice impreso de {origen} pone «{titulo}» en la página {pagina}, y el texto no lo trae "
    "a {margen} páginas o menos de ahí. Si el documento cambió de edición, revisá ese título: "
    "cortar sin él dejaría el texto que le sigue con la subsección equivocada."
)

# --- Índice ---
ERROR_INDICE_AUSENTE = (
    "No hay índice en {ruta}. Construilo con `python -m scripts.construir_indice` o "
    "configurá INDICE_URL para que el servicio lo descargue al arrancar."
)
ERROR_INDICE_VACIO = "El índice de {ruta} existe y no tiene ningún fragmento."
ERROR_INDICE_DIMENSION = (
    "El índice de {ruta} fue construido con {tiene} dimensiones y la configuración pide "
    "{espera}. Reconstruilo o ajustá DIMENSIONES_EMBEDDINGS."
)
ERROR_INDICE_MODELO = (
    "El índice de {ruta} fue construido con el modelo de embeddings «{tiene}» y la "
    "configuración pide «{espera}». Dos modelos distintos dan vectores incomparables: "
    "reconstruilo o ajustá MODELO_EMBEDDINGS."
)
MENSAJE_DESCARGANDO_INDICE = "Descargando el índice desde {url}…"
MENSAJE_INDICE_DESCARGADO = (
    "Índice listo en {ruta}: {fragmentos} fragmentos, huella {huella}."
)

# --- Herramientas ---
# La línea que acompaña a cada fragmento y la que cierra la búsqueda. Las dos dicen lo mismo
# desde dos lados, y es a propósito: la lista final es la que el modelo mira para elegir, y la
# de cada fragmento es la que le dice qué sostiene cada fallo.
ENCABEZADO_CITAS_DEL_FRAGMENTO = "Fallos citados acá:"
ENCABEZADO_CITABLES = (
    "Fallos que podés citar, porque salieron de estos fragmentos:"
)
MENSAJE_SIN_CITABLES = (
    "Ninguno de estos fragmentos cita un fallo. Buscá de nuevo con otros términos antes de "
    "escribir la síntesis."
)
MENSAJE_SIN_RESULTADOS = (
    "La búsqueda no devolvió ningún fragmento para esa consulta. "
    "Probá reformularla con otros términos."
)
MENSAJE_SIN_CITAS_APROBADAS = "(ninguna: el verificador no aprobó ninguna cita)"

# Respuestas de link_oficial. Las tres son observaciones para el modelo, no errores: el
# redactor tiene que poder seguir escribiendo sin el link en vez de cortar la corrida.
MENSAJE_CITA_NO_APROBADA = (
    "'{cita}' no está entre las citas verificadas de esta consulta, así que no tiene link "
    "ni puede aparecer en la respuesta. Usá solo las de la lista."
)
MENSAJE_CITA_ILEGIBLE = (
    "No pude leer un número de fallo en '{cita}'. Pedímelo como 'Fallos: tomo:pagina'."
)
MENSAJE_SIN_LINK = (
    "'{cita}' está verificada y el corpus no registra su link oficial. Citala igual, "
    "sin link."
)

# --- Errores de proveedor y almacenamiento ---
ERROR_CLAVE_DE_PROVEEDOR = (
    "Falta {variable}. Copiá .env.example a .env y cargá tu clave "
    "(el .env nunca se commitea)."
)
ERROR_SAL_VISITANTE = (
    "Falta SAL_VISITANTE, o tiene menos de {minimo} caracteres. El registro guarda "
    "sha256(sal | valor) para los cupos, y sin una sal larga y propia de este despliegue esos "
    "hashes se revierten a la IP del visitante. Generá una con "
    "`python -c \"import secrets; print(secrets.token_hex(32))\"` y cargála en el entorno."
)
ERROR_LIMITE = "Límite de uso de la API del proveedor alcanzado: {detalle}"
ERROR_CONEXION = "No se pudo conectar con la API del proveedor: {detalle}"
ERROR_API = "La API del proveedor devolvió un error: {detalle}"
ERROR_VECTORSTORE = "Error de la base vectorial: {detalle}"
ERROR_ALMACEN = "Error del almacén SQLite: {detalle}"
ERROR_HERRAMIENTA_BASE = (
    "La base de conocimiento no está disponible ({detalle}). "
    "Informale el problema al usuario o intentá de nuevo más tarde."
)
ERROR_HERRAMIENTA_NO_INICIALIZADA = (
    "Las herramientas no fueron inicializadas: llamá a herramientas.inicializar() "
    "con el vectorstore y el almacén antes de construir el grafo."
)
ERROR_RECUPERADOR_SINCRONICO = (
    "Este recuperador es asincrónico: usá ainvoke. El camino sincrónico bloquearía el "
    "event loop."
)
ERROR_HERRAMIENTA_SIN_EMBEDDINGS = (
    "La base vectorial no tiene función de embeddings: no se puede consultar por similitud."
)

# --- El orquestador ---
MOTIVO_CITAS_INTRUSAS = "invocó citas que el verificador no aprobó: {citas}"
MOTIVO_POCAS_CITAS = (
    "el texto se apoya en {usadas} cita(s) verificada(s); se piden al menos {minimas} "
    "para darlo por fundado"
)
ERROR_SUPERVISOR = "El supervisor no pudo decidir el próximo paso: {detalle}"
ERROR_DECISION_INCOMPLETA = (
    "la respuesta se cortó por límite de tokens y la decisión puede estar a medias"
)
ERROR_DECISION_VACIA = "el modelo no devolvió ninguna decisión"
ERROR_INVESTIGADOR = "El investigador falló: {detalle}"
ERROR_VERIFICADOR = "El verificador no pudo consultar el padrón de citas: {detalle}"
ERROR_VERIFICADOR_SIN_INVESTIGACION = (
    "El verificador se ejecutó sin investigación en el estado: el supervisor lo delegó "
    "fuera de orden."
)
ERROR_REDACTOR = "El redactor no pudo escribir la respuesta final: {detalle}"
ERROR_REDACTOR_SIN_MATERIAL = (
    "El redactor se ejecutó sin investigación o sin verificación en el estado: el "
    "supervisor lo delegó fuera de orden. Redactar antes de verificar sería escribir "
    "sobre citas que todavía nadie comprobó."
)
ERROR_DESTINO_INVALIDO = "El supervisor eligió un destino que no existe: {destino!r}"
ERROR_LIMITE_ORQUESTADOR = (
    "Se alcanzó el límite de supersteps del orquestador sin que el supervisor cerrara. "
    "El límite es una red, no la condición de corte: si salta, el problema está en el "
    "ruteo."
)

# --- Observabilidad ---
AVISO_MODELO_SIN_PRECIO = (
    "AVISO: «{modelo}» no está en la tabla de precios (consultada el {fecha}), así que su "
    "consumo suma cero dólares. El freno por presupuesto no puede verlo: agregalo a "
    "app/nucleo/precios.py."
)
AVISO_LANGSMITH_APAGADO = (
    "AVISO: sin LANGSMITH_API_KEY el sistema corre y no deja trazas."
)
AVISO_LANGSMITH_INALCANZABLE = (
    "AVISO: LangSmith no responde; el sistema sigue, las trazas se pierden."
)
AVISO_TRAZAS_SIN_VACIAR = "AVISO: quedaron trazas sin enviar a LangSmith ({detalle})."

# --- Servicio: lo que lee una persona ---
DESCRIPCION_API = (
    "Consultas en lenguaje natural sobre jurisprudencia de la Corte Suprema de Justicia "
    "de la Nación, respondidas con citas de fallos reales y sus links oficiales."
)
FASE_EN_COLA = "En cola: {posicion} consulta(s) adelante."
FASE_INVESTIGANDO = "Buscando doctrina en el corpus."
FASE_INVESTIGADO = "{citas} cita(s) propuestas sobre {subsecciones} subsección(es)."
FASE_VERIFICANDO = "Comprobando las citas contra el padrón del corpus."
FASE_VERIFICADO = "{verificadas} de {propuestas} citas existen en el corpus."
FASE_RECHAZADO = "Hay que corregir: {motivo}"
FASE_REDACTANDO = "Redactando la respuesta."
FASE_REINTENTANDO = "Corrección {intento} de {maximo}: {motivo}"

MENSAJE_SIN_BASE = (
    "El corpus indexado no tiene material suficiente para sostener una respuesta a esta "
    "consulta con citas verificables. La consulta quedó registrada."
)
MENSAJE_FUERA_DE_ALCANCE = (
    "Este buscador responde sobre la doctrina de la Corte Suprema de Justicia de la Nación "
    "acerca del recurso extraordinario federal. La consulta quedó fuera de ese alcance."
)
MENSAJE_CUOTA_VISITANTE = (
    "Llegaste a las {tope} consultas de las últimas {horas} horas. Volvés a tener "
    "consultas el {cuando}."
)
MENSAJE_CUOTA_INTENTOS = (
    "Llegaste a los {tope} intentos de las últimas {horas} horas. Volvés a tener "
    "intentos el {cuando}."
)
MENSAJE_MODO_LECTURA = (
    "El buscador alcanzó su techo de consultas de hoy. Las respuestas ya publicadas "
    "siguen disponibles; se reanuda a las 00:00."
)
MENSAJE_PRESUPUESTO_AGOTADO = (
    "El buscador alcanzó su presupuesto del mes. Las respuestas ya publicadas siguen "
    "disponibles."
)
MENSAJE_CONSULTA_EN_CURSO = (
    "Ya tenés una consulta corriendo. Esperá a que termine antes de mandar otra."
)
MENSAJE_INDICE_NO_LISTO = "El buscador está cargando el corpus. Probá en un minuto."
AVISO_REGISTRO = (
    "Las consultas se registran de forma anónima para mejorar el buscador. "
    "No escribas datos personales."
)
DATO_REMOVIDO = "[dato removido]"

ERROR_CONSULTA_INEXISTENTE = "No existe ninguna consulta con id {consulta_id}."
ERROR_NO_ENCONTRADO = "No existe esa ruta."
ERROR_CORRIDA_DEMORADA = "la consulta superó los {segundos} segundos y se cortó"
ERROR_CORRIDA = "La consulta falló: {detalle}"
