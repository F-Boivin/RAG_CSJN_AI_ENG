"""El límite de tema: qué consultas entran al grafo.

Una pregunta fuera de tema ya falla cerrada sin este control —cero citas, el verificador
rechaza, tres reintentos, no publica—. Lo que la admisión evita es pagar cuarenta segundos y
dos centavos para llegar ahí.

Dos capas que corren a la vez y tienen que coincidir:

1. **El piso de recuperación**: se embebe la consulta una vez y se mira la mejor similitud del
   corpus. Cuesta ~USD 0,000002. Frena lo más ajeno cuando el clasificador no responde, y nada
   más: una consulta en tema de una o dos palabras («ley 48», «per saltum») puntúa por debajo
   de preguntas que no son jurídicas.
2. **El clasificador**: una llamada a `gpt-4o-mini` con salida estructurada, ~USD 0,00006. Es
   el que separa el recurso extraordinario del resto de la jurisprudencia de la Corte y de lo
   que no es jurídico.

Medido contra el índice del «Recurso Extraordinario», con 30 consultas en tema repartidas entre
sus siete capítulos y 30 fuera —doce sobre el fondo de otros temas de la Corte, diez de otro
derecho o de un caso propio, ocho no jurídicas—: las dos capas juntas admiten 30 de 30 y
rechazan 30 de 30. El clasificador acierta además las 24 consultas de una o dos palabras con
que se eligió el umbral. La primera versión del prompt nombraba los capítulos en abstracto y
rechazaba cuatro consultas en tema —«qué es el exceso ritual manifiesto», «qué dice el artículo
14 de la ley 48», «doctrina de los fallos Strada y Di Mascio», «¿una medida cautelar es
sentencia definitiva?»—, y por eso el prompt nombra causales, normas y fallos.

Se admite cuando las dos pasan, y el motivo que ve la persona lo escribe el clasificador. Van
antes de crear la corrida, así una consulta fuera de tema no consume cupo de consultas —solo
de intentos, que es el contador que frena a quien martilla.
"""

import asyncio
from dataclasses import dataclass

from langchain_chroma import Chroma
from pydantic import BaseModel, Field

import app.nucleo.mensajes as msj
from app.nucleo.config import obtener_ajustes
from app.nucleo.modelos import crear_chat

PROMPT_ADMISION = """Sos el filtro de alcance de un buscador sobre la doctrina de la Corte Suprema de Justicia de la Nación Argentina acerca del recurso extraordinario federal.

El buscador responde con «Recurso Extraordinario», la obra de la Secretaría de Jurisprudencia que reúne esa doctrina en siete capítulos:
- interposición: quiénes pueden interponerlo, ante quién, el plazo, la fundamentación del escrito y los requisitos formales de la acordada 4/2007 (carátula, páginas, renglones);
- trámite: el traslado, la concesión o denegación por el tribunal de la causa, la vista al fiscal, las facultades de la Corte, el art. 280 del Código Procesal Civil y Comercial y la caducidad de instancia;
- cuestión federal: los supuestos del art. 14 de la ley 48, las cuestiones insustanciales, la gravedad institucional, la relación directa, la resolución contraria, y cuándo y cómo se introduce y se mantiene;
- sentencia definitiva: qué resoluciones lo son o se equiparan —medidas cautelares, amparos, nulidades, cuestiones de competencia, ejecución de sentencia, entre otras—, el gravamen de imposible reparación ulterior y las sentencias incompletas;
- superior tribunal de la causa: los tribunales superiores de provincia (los fallos «Strada» y «Di Mascio»), las cámaras, la casación penal («Giroldi», «Di Nunzio», «Casal») y el recurso por salto de instancia o per saltum;
- sentencias arbitrarias: la doctrina, sus causales —falta de fundamentación, afirmaciones dogmáticas, apartamiento de las constancias de la causa, valoración de la prueba, exceso ritual manifiesto, contradicción, entre otras— y la improcedencia del planteo;
- recurso de queja: contra qué procede, el plazo, el depósito previo y sus exenciones, y su trámite.

Admitís una consulta si pregunta por algo de eso, aunque esté mal redactada, sea de una o dos palabras, use términos vagos o no nombre el recurso extraordinario. Una pregunta por el texto o el alcance de las normas que regulan el recurso —el art. 14 de la ley 48, los artículos del Código Procesal Civil y Comercial sobre el recurso extraordinario y la queja, la acordada 4/2007— está en tema. Ante la duda con una pregunta sobre cómo se llega a la Corte Suprema o cómo revisa la Corte las sentencias de otros tribunales, admitila.

Rechazás una consulta sobre el fondo de otros temas que la Corte resolvió —derechos del niño, tributos, jubilaciones, salud, ambiente, lesa humanidad, libertad de expresión, entre otros— o sobre su competencia originaria: el buscador no tiene esos materiales, y admitirla es prometer una respuesta que no puede dar. Rechazás también si pide asesoramiento sobre un caso propio, si pregunta por hechos actuales, o si no es una pregunta jurídica.

Devolvé el motivo en una oración, dirigida a quien preguntó. Cuando rechazás por tema, decí que el buscador trata solo la doctrina de la Corte sobre el recurso extraordinario federal."""


class Admision(BaseModel):
    """El veredicto del clasificador."""

    admitida: bool = Field(
        description="Si la consulta busca la doctrina de la CSJN sobre el recurso extraordinario.")
    motivo: str = Field(min_length=3, max_length=300,
                        description="Por qué, en una oración para quien preguntó.")


@dataclass
class Veredicto:
    """Lo que la admisión resolvió, con los dos números que lo sostienen."""

    admitida: bool
    motivo: str
    similitud: float
    del_clasificador: bool


async def admitir(consulta: str, chroma: Chroma, contador=None) -> Veredicto:
    """Decide si la consulta entra al grafo.

    Las dos capas corren en paralelo: la lenta es la del modelo, y esperar a la barata para
    recién ahí empezarla sumaría su latencia a la del embedding sin necesidad.

    `contador` es el mismo `Contador` que mide una corrida. **La admisión corre en toda
    consulta, incluidas las que se rechazan, y su costo no entraba a ningún lado**: el freno
    por presupuesto no podía verlo, justo en el camino que alguien martillearía para gastar
    plata ajena. Va por parámetro y no adentro para que quien lo llama decida si suma ese
    gasto o no.
    """
    similitud, clasificacion = await asyncio.gather(
        _mejor_similitud(consulta, chroma),
        _clasificar(consulta, contador),
        return_exceptions=True,
    )
    # Un fallo de cualquiera de las dos capas deja pasar la consulta: el corpus decide igual
    # más adelante, y negarle una respuesta a alguien por un error de infraestructura es peor
    # que gastar una corrida. `del_clasificador` dice si el motivo lo escribió el modelo o si
    # es el texto de reserva, que es lo que hace legible un rechazo en el registro.
    respondio = not isinstance(clasificacion, Exception)
    if isinstance(similitud, Exception):
        similitud = 1.0
    if not respondio:
        clasificacion = Admision(admitida=True, motivo="el clasificador no respondió")

    umbral = obtener_ajustes().umbral_similitud
    if similitud < umbral:
        return Veredicto(False, msj.MENSAJE_FUERA_DE_ALCANCE, similitud, False)
    return Veredicto(clasificacion.admitida, clasificacion.motivo, similitud, respondio)


async def _mejor_similitud(consulta: str, chroma: Chroma) -> float:
    """La similitud del fragmento más cercano, entre 0 y 1.

    Chroma devuelve distancia coseno con la métrica configurada, así que la similitud es
    `1 - distancia`. Un corpus vacío devuelve 0.0, que es lo correcto: sin material no hay
    consulta dentro de alcance.
    """
    resultados = await chroma.asimilarity_search_with_score(consulta, k=1)
    if not resultados:
        return 0.0
    _, distancia = resultados[0]
    return max(0.0, 1.0 - float(distancia))


async def _clasificar(consulta: str, contador=None) -> Admision:
    """El veredicto del modelo, con salida estructurada.

    El rol es `admision`, que es el que `MODELO_ADMISION` configura: con el del supervisor, esa
    variable quedaba declarada y publicada en `.env.example` sin que nadie la leyera, y el
    clasificador corría con el modelo de otro rol.
    """
    modelo = crear_chat("admision").with_structured_output(Admision)
    config = {"callbacks": [contador]} if contador else {}
    return await modelo.ainvoke([("system", PROMPT_ADMISION), ("user", consulta)], config)
