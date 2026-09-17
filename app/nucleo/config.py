"""La configuración que llega por entorno, validada con Pydantic.

`Ajustes` recoge lo que cambia entre despliegues —proveedor, modelos, claves, rutas de
datos, cupos y presupuesto— y lo valida al construirse. Lo que no está acá
es invariante del sistema y vive en `constantes.py`.

Se lee una vez por proceso: `obtener_ajustes()` cachea la instancia, y nadie la construye al
importar, así la validación ocurre cuando el proceso arranca.

Verificado contra la documentación oficial (pydantic-settings 2.14.2):
- `BaseSettings` toma los valores del entorno y de `env_file`, con el nombre del campo en
  mayúsculas. https://docs.pydantic.dev/latest/concepts/pydantic_settings/
"""

import os
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Optional

from dotenv import find_dotenv, load_dotenv
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

import app.nucleo.constantes as cfg
import app.nucleo.mensajes as msj
from app.nucleo.errores import ErrorDeConfiguracion


class Proveedor(str, Enum):
    """Los proveedores de chat que el factory sabe construir."""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"


class Ajustes(BaseSettings):
    """Todo lo que se configura desde afuera del código.

    Los modelos por rol admiten `None`: sin valor, cada uno toma el modelo por defecto del
    proveedor activo. Con un default fijo, pedir `LLM_PROVIDER=anthropic` dejaría al sistema
    pidiéndole a Anthropic un modelo de OpenAI.
    """

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # --- Proveedor y claves ---
    llm_provider: Proveedor = Proveedor.OPENAI
    openai_api_key: Optional[SecretStr] = None
    anthropic_api_key: Optional[SecretStr] = None

    # --- Modelos por rol ---
    modelo_supervisor: Optional[str] = None
    modelo_investigador: Optional[str] = None
    modelo_redactor: Optional[str] = None
    modelo_embeddings: str = "text-embedding-3-small"
    modelo_admision: str = "gpt-4o-mini"
    temperatura: float = Field(default=0.0, ge=0.0, le=2.0)
    # `text-embedding-3-*` admite truncar el vector con degradación gradual. 512 deja el
    # índice en poco más de un tercio; el número se fija midiendo el solapamiento del top-4
    # contra 1536, y hasta entonces conviene el default del modelo.
    dimensiones_embeddings: Optional[int] = Field(default=None, ge=64, le=3072)

    # --- Dónde viven los datos ---
    nombre_coleccion: str = "csjn_jurisprudencia"
    directorio_indice: Path = cfg.RAIZ / "datos" / "indice"
    directorio_cache: Path = cfg.RAIZ / "datos" / "cache"
    archivo_estado: Path = cfg.RAIZ / "datos" / "estado.sqlite3"
    # De dónde baja el índice al arrancar, cuando el volumen está vacío. El índice se
    # construye en una máquina con las claves y se publica como artefacto; el servicio nunca
    # habla con csjn.gov.ar.
    indice_url: Optional[str] = None
    indice_huella: Optional[str] = None

    # --- Límites del servicio ---
    consultas_por_visitante: int = Field(default=5, ge=1)
    consultas_por_ip: int = Field(default=15, ge=1)
    consultas_por_dia: int = Field(default=50, ge=1)
    intentos_por_visitante: int = Field(default=20, ge=1)
    # El techo por IP guarda la misma proporción que el de consultas —tres veces el del
    # visitante—, para no castigar a un estudio entero detrás de un NAT. Es el que hace que el
    # tope de intentos exista: el de la cookie lo elude quien no la devuelve.
    intentos_por_ip: int = Field(default=60, ge=1)
    consultas_concurrentes: int = Field(default=cfg.CONSULTAS_CONCURRENTES, ge=1)
    presupuesto_usd_mes: float = Field(default=30.0, ge=0.0)
    # Calibrado contra el índice del cuadernillo con 30 consultas en tema y 30 fuera: las de
    # tema arrancan en 0,487 y las no jurídicas no pasan de 0,381. En 0,40 no rechaza ninguna
    # de tema, con 0,09 de margen, y frena todo lo no jurídico. Entre la arbitrariedad y los
    # otros temas jurídicos las distribuciones se pisan: esa separación es del clasificador.
    umbral_similitud: float = Field(default=0.40, ge=0.0, le=1.0)
    # Sin default a propósito: ver `sal_de_visitante`. Un default acá sería un secreto
    # publicado en un repositorio público.
    sal_visitante: Optional[str] = None
    origen_permitido: Optional[str] = None
    # Sin token, `/respaldo` contesta 404: lo que sirve es el registro entero, y un endpoint
    # así no puede existir por defecto.
    token_respaldo: Optional[str] = None

    # --- Observabilidad ---
    langsmith_api_key: Optional[SecretStr] = None
    proyecto_langsmith: str = "rag-csjn"

    @model_validator(mode="before")
    @classmethod
    def sin_valores_vacios(cls, valores):
        """Una variable declarada y vacía vale como no declarada.

        `.env.example` trae vacías las opcionales, que es la forma de mostrar que existen. Sin
        esto, copiarlo tal cual y completar solo la clave deja `TEMPERATURA=` como cadena vacía
        y el sistema no arranca; peor todavía, `DIRECTORIO_INDICE=` daría `Path('.')` y el
        servicio buscaría el índice en el directorio de trabajo.
        """
        if isinstance(valores, dict):
            return {k: v for k, v in valores.items()
                    if not (isinstance(v, str) and not v.strip())}
        return valores

    def clave_de(self, proveedor: Proveedor) -> SecretStr:
        """La clave del proveedor, o un error que dice cuál falta y dónde cargarla."""
        clave = getattr(self, f"{proveedor.value}_api_key", None)
        if clave is None or not clave.get_secret_value().strip():
            raise ErrorDeConfiguracion(
                msj.ERROR_CLAVE_DE_PROVEEDOR.format(variable=f"{proveedor.value.upper()}_API_KEY")
            )
        return clave

    def sal_de_visitante(self) -> str:
        """La sal con que se hashean la cookie y la IP, o un error que corta el arranque.

        **Es la única variable cuyo default sería un secreto publicado.** El registro guarda
        `sha256(sal | valor)` y la página de privacidad promete que de ahí no se vuelve a la IP;
        con la sal en el código de un repositorio público, recorrer las 4.300 millones de IPv4
        es cuestión de minutos, y `pulsos` pasa a ser un padrón de direcciones reales de quien
        usó un buscador de jurisprudencia. La promesa al titular del dato es lo que la ley
        25.326 exige que sea cierta, así que el servicio no arranca sin sal.

        El largo mínimo tapa el otro modo de falla, que hace exactamente el mismo daño: una sal
        corta escrita a mano en el panel del proveedor —«csjn», el nombre del proyecto— se
        adivina igual que un literal publicado. No prueba entropía; descarta lo obvio.

        Vive fuera de `Ajustes` porque es un requisito del **servicio público**, no de la
        configuración: la ingesta y los scripts no hashean a nadie y corren sin ella, igual que
        corren sin la clave del proveedor.
        """
        sal = (self.sal_visitante or "").strip()
        if len(sal) < cfg.LARGO_MINIMO_SAL:
            raise ErrorDeConfiguracion(
                msj.ERROR_SAL_VISITANTE.format(minimo=cfg.LARGO_MINIMO_SAL))
        return sal


@lru_cache(maxsize=1)
def obtener_ajustes() -> Ajustes:
    """Los ajustes del proceso, leídos una sola vez."""
    return Ajustes()


def reiniciar_ajustes() -> None:
    """Borra la caché para que la próxima lectura vuelva a mirar el entorno."""
    obtener_ajustes.cache_clear()


def cargar_entorno(exigir_clave: bool = True) -> Ajustes:
    """Carga el `.env` y devuelve los ajustes ya validados.

    `find_dotenv` busca hacia arriba desde el directorio de trabajo, y `ARCHIVO_ENV` permite
    apuntar a uno que viva en otra rama del disco: las claves se guardan fuera del árbol del
    repositorio, así que no siempre están por encima del cwd.

    `OPENAI_API_KEY` se exige cuando el proceso va a embeber o a consultar —los embeddings son
    de OpenAI incluso cuando el chat corre con otro proveedor—. Las etapas de la ingesta que
    solo descargan y extraen texto corren sin ninguna clave.
    """
    ruta = os.environ.get("ARCHIVO_ENV") or find_dotenv(usecwd=True)
    if ruta:
        load_dotenv(ruta, override=False)
    reiniciar_ajustes()
    ajustes = obtener_ajustes()
    if exigir_clave:
        ajustes.clave_de(Proveedor.OPENAI)
    return ajustes
