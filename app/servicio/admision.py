"""El límite de tema: qué consultas entran al grafo.

Una pregunta fuera de tema ya falla cerrada sin este control —cero citas, el verificador
rechaza, tres reintentos, no publica—. Lo que la admisión evita es pagar cuarenta segundos y
dos centavos para llegar ahí.

Dos capas que corren a la vez y tienen que coincidir:

1. **El piso de recuperación**: se embebe la consulta una vez y se mira la mejor similitud del
   corpus. Cuesta ~USD 0,000002. Frena lo que no es jurídico, y nada más: entre la
   arbitrariedad y los otros temas del derecho las similitudes se pisan.
2. **El clasificador**: una llamada a `gpt-4o-mini` con salida estructurada, ~USD 0,00006. Es
   el que separa la arbitrariedad del resto de la jurisprudencia de la Corte.

Medido contra el índice del cuadernillo, con 30 consultas en tema y 30 fuera —doce de ellas
sobre temas de la Corte que el corpus tuvo hasta que se quedó solo con el cuadernillo—: las dos
capas juntas admiten 30 de 30 y rechazan 30 de 30. La primera versión del prompt rechazaba dos
consultas del cuadernillo que no nombraban la palabra «arbitrariedad», y por eso el prompt lista
la estructura del cuadernillo y pide admitir ante la duda sobre el recurso extraordinario.

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

PROMPT_ADMISION = """Sos el filtro de alcance de un buscador sobre la doctrina de la Corte Suprema de Justicia de la Nación Argentina en materia de sentencias arbitrarias.

El buscador responde con el cuadernillo de la Secretaría de Jurisprudencia sobre la arbitrariedad, que trata:
- la doctrina: su origen, su carácter excepcional, que la Corte no es una tercera instancia, y la diferencia entre arbitrariedad y error;
- las causales: falta de fundamentación, afirmaciones dogmáticas, apartamiento de las constancias de la causa, valoración de hechos y prueba, omisión de extremos conducentes, interpretación errónea o apartamiento de la norma aplicable, excesos u omisiones en el pronunciamiento, exceso ritual manifiesto y contradicción;
- la improcedencia del recurso por arbitrariedad;
- el trámite: la fundamentación de la concesión por el tribunal a quo, el recurso de queja, y la relación y el orden entre la cuestión federal y la arbitrariedad.

Admitís una consulta si pregunta por algo de eso, aunque esté mal redactada, use términos vagos o no nombre la palabra «arbitrariedad». Ante la duda con una pregunta sobre el recurso extraordinario o sobre cómo revisa la Corte las sentencias de otros tribunales, admitila.

Rechazás una consulta sobre otros temas de la Corte —derechos del niño, tributos, salud, ambiente, lesa humanidad, competencia originaria, entre otros—: el buscador no tiene esos materiales, y admitirla es prometer una respuesta que no puede dar. Rechazás también si pide asesoramiento sobre un caso propio, si pregunta por hechos actuales, o si no es una pregunta jurídica.

Devolvé el motivo en una oración, dirigida a quien preguntó. Cuando rechazás por tema, decí que el buscador trata solo la doctrina de la Corte sobre sentencias arbitrarias."""


class Admision(BaseModel):
    """El veredicto del clasificador."""

    admitida: bool = Field(
        description="Si la consulta busca la doctrina de la CSJN sobre sentencias arbitrarias.")
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
