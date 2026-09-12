"""La configuración por entorno y el factory de modelos.

Lo que se comprueba es que una variable de entorno cambie el comportamiento del sistema:
cambiar `LLM_PROVIDER` tiene que cambiar qué cliente construye el modelo, y pedir un rol sin
modelo configurado tiene que caer en el default del proveedor activo, no en uno ajeno.

Ningún test construye un modelo real: se monkeypatchea el constructor para observar con qué
lo llamaron y para afirmar que sin clave no llega a llamarse.
"""

import pytest
from pydantic import SecretStr

from app.nucleo import modelos
from app.nucleo.config import Ajustes, Proveedor, obtener_ajustes, reiniciar_ajustes
from app.nucleo.errores import ErrorDeConfiguracion

VARIABLES = ("LLM_PROVIDER", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "MODELO_SUPERVISOR",
             "MODELO_INVESTIGADOR", "MODELO_REDACTOR", "MODELO_EMBEDDINGS", "TEMPERATURA",
             "REDIS_URL", "NOMBRE_COLECCION", "PROYECTO_LANGSMITH", "LANGSMITH_API_KEY",
             "DIRECTORIO_VECTORSTORE")


@pytest.fixture(autouse=True)
def entorno_limpio(monkeypatch, tmp_path):
    """Sin variables heredadas ni `.env` del repositorio."""
    for variable in VARIABLES:
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.chdir(tmp_path)
    reiniciar_ajustes()
    yield
    reiniciar_ajustes()


def ajustes_con(**variables) -> Ajustes:
    """Los ajustes que salen de ese entorno."""
    return Ajustes(**variables)


class TestAjustes:
    """Los valores por defecto y lo que el entorno cambia."""

    def test_los_defaults_son_los_del_proyecto(self):
        ajustes = ajustes_con()
        assert ajustes.llm_provider is Proveedor.OPENAI
        assert ajustes.temperatura == 0.0
        assert ajustes.consultas_por_visitante == 5
        assert ajustes.nombre_coleccion == "csjn_jurisprudencia"

    def test_el_entorno_manda_sobre_los_defaults(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "anthropic")
        monkeypatch.setenv("CONSULTAS_POR_DIA", "120")
        monkeypatch.setenv("NOMBRE_COLECCION", "otra_coleccion")
        ajustes = Ajustes()
        assert ajustes.llm_provider is Proveedor.ANTHROPIC
        assert ajustes.consultas_por_dia == 120
        assert ajustes.nombre_coleccion == "otra_coleccion"

    def test_un_proveedor_inexistente_no_valida(self):
        with pytest.raises(Exception):
            ajustes_con(llm_provider="gemini")

    def test_la_temperatura_tiene_rango(self):
        with pytest.raises(Exception):
            ajustes_con(temperatura=3.0)

    def test_los_modelos_por_rol_arrancan_sin_valor(self):
        # Sin valor, cada rol toma el default del proveedor activo: un default fijo dejaría
        # a `LLM_PROVIDER=anthropic` pidiendo un modelo de OpenAI.
        ajustes = ajustes_con()
        assert ajustes.modelo_supervisor is None
        assert ajustes.modelo_investigador is None

    def test_las_claves_no_se_imprimen(self):
        ajustes = ajustes_con(openai_api_key="sk-secreta-de-verdad")
        assert "sk-secreta-de-verdad" not in repr(ajustes)
        assert ajustes.openai_api_key.get_secret_value() == "sk-secreta-de-verdad"

    def test_los_ajustes_se_leen_una_sola_vez(self):
        assert obtener_ajustes() is obtener_ajustes()

    def test_reiniciar_vuelve_a_mirar_el_entorno(self, monkeypatch):
        primera = obtener_ajustes()
        monkeypatch.setenv("CONSULTAS_POR_DIA", "77")
        reiniciar_ajustes()
        assert obtener_ajustes().consultas_por_dia == 77
        assert obtener_ajustes() is not primera


class TestVariablesVacias:
    """Una variable declarada y vacía vale como no declarada.

    `.env.example` trae vacías las opcionales, que es como se muestra que existen: copiarlo
    tal cual y completar solo la clave tiene que dejar un sistema que arranca.
    """

    def test_una_temperatura_vacia_cae_al_default(self):
        # Sin esto, `TEMPERATURA=` rompe la validación y el sistema no levanta.
        assert ajustes_con(temperatura="").temperatura == 0.0

    def test_un_directorio_vacio_cae_al_default_y_no_a_punto(self):
        # `Path("")` es el directorio actual: el índice se reconstruiría en cada arranque.
        assert ajustes_con(directorio_indice="").directorio_indice.name == "indice"

    def test_un_modelo_vacio_deja_elegir_al_proveedor(self):
        assert ajustes_con(modelo_supervisor="").modelo_supervisor is None

    def test_una_clave_vacia_es_como_no_tenerla(self):
        assert ajustes_con(anthropic_api_key="").anthropic_api_key is None

    def test_una_coleccion_vacia_cae_al_default(self):
        assert ajustes_con(nombre_coleccion="  ").nombre_coleccion == "csjn_jurisprudencia"

    def test_un_valor_real_no_se_descarta(self):
        assert ajustes_con(nombre_coleccion="otra").nombre_coleccion == "otra"


class TestClaveDelProveedor:
    """El guard que evita construir un cliente sin credencial."""

    def test_devuelve_la_clave_del_proveedor_pedido(self):
        ajustes = ajustes_con(openai_api_key="sk-openai", anthropic_api_key="sk-anthropic")
        assert ajustes.clave_de(Proveedor.ANTHROPIC).get_secret_value() == "sk-anthropic"

    def test_sin_clave_falla_nombrando_la_variable(self):
        with pytest.raises(ErrorDeConfiguracion, match="ANTHROPIC_API_KEY"):
            ajustes_con(openai_api_key="sk-openai").clave_de(Proveedor.ANTHROPIC)

    def test_una_clave_en_blanco_cuenta_como_ausente(self):
        with pytest.raises(ErrorDeConfiguracion):
            ajustes_con(openai_api_key="   ").clave_de(Proveedor.OPENAI)


class TestSalDeVisitante:
    """La única variable cuyo default sería un secreto publicado.

    El registro guarda `sha256(sal | valor)` para los cupos y la página de privacidad promete
    que de ahí no se vuelve a la IP. Con la sal en el código de un repositorio público, o con
    una escrita a mano en el panel del proveedor, recorrer las 4.300 millones de IPv4 alcanza
    para revertir el hash: `pulsos` pasa a ser un padrón de direcciones reales.
    """

    def test_la_sal_configurada_se_devuelve_sin_espacios(self):
        ajustes = ajustes_con(sal_visitante="  " + "a" * 40 + "  ")
        assert ajustes.sal_de_visitante() == "a" * 40

    def test_sin_sal_falla_nombrando_la_variable(self):
        with pytest.raises(ErrorDeConfiguracion, match="SAL_VISITANTE"):
            ajustes_con().sal_de_visitante()

    def test_no_hay_ninguna_sal_por_defecto(self):
        # Es el punto: un default acá viajaría en el repositorio.
        assert ajustes_con().sal_visitante is None

    def test_una_sal_corta_no_alcanza(self):
        # Una sal adivinable hace el mismo daño que una publicada.
        with pytest.raises(ErrorDeConfiguracion):
            ajustes_con(sal_visitante="csjn").sal_de_visitante()

    def test_el_error_dice_como_generar_una(self):
        with pytest.raises(ErrorDeConfiguracion, match="token_hex"):
            ajustes_con().sal_de_visitante()

    def test_la_sal_en_blanco_cuenta_como_ausente(self):
        with pytest.raises(ErrorDeConfiguracion):
            ajustes_con(sal_visitante="   ").sal_de_visitante()


class TestFactory:
    """Qué cliente construye el modelo, y con qué nombre."""

    @pytest.fixture
    def espia(self, monkeypatch):
        """Registra con qué se llamó a cada constructor, sin construir nada."""
        llamadas = []

        class ModeloFalso:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        def registrar(nombre):
            def constructor(**kwargs):
                llamadas.append((nombre, kwargs))
                return ModeloFalso(**kwargs)
            return constructor

        monkeypatch.setattr(modelos.ClienteOpenAI, "construir",
                            lambda self, modelo, temperatura: registrar("openai")(
                                model=modelo, temperature=temperatura, api_key=self.clave))
        monkeypatch.setattr(modelos.ClienteAnthropic, "construir",
                            lambda self, modelo, temperatura: registrar("anthropic")(
                                model=modelo, temperature=temperatura, api_key=self.clave))
        return llamadas

    def test_openai_es_el_proveedor_por_defecto(self, espia):
        modelos.crear_chat("supervisor", ajustes_con(openai_api_key="sk-openai"))
        assert espia[0][0] == "openai"
        assert espia[0][1]["model"] == "gpt-4o-mini"

    def test_el_proveedor_cambia_el_cliente_y_el_modelo(self, espia):
        modelos.crear_chat("supervisor", ajustes_con(
            llm_provider=Proveedor.ANTHROPIC, anthropic_api_key="sk-anthropic"))
        assert espia[0][0] == "anthropic"
        assert espia[0][1]["model"] == "claude-haiku-4-5"

    def test_el_modelo_configurado_gana_sobre_el_default(self, espia):
        modelos.crear_chat("redactor", ajustes_con(
            openai_api_key="sk-openai", modelo_redactor="gpt-4o"))
        assert espia[0][1]["model"] == "gpt-4o"

    def test_cada_rol_puede_tener_su_modelo(self, espia):
        ajustes = ajustes_con(openai_api_key="sk-openai", modelo_supervisor="gpt-4o",
                              modelo_investigador="gpt-4o-mini")
        modelos.crear_chat("supervisor", ajustes)
        modelos.crear_chat("investigador", ajustes)
        assert [ll[1]["model"] for ll in espia] == ["gpt-4o", "gpt-4o-mini"]

    def test_la_temperatura_llega_al_modelo(self, espia):
        modelos.crear_chat("supervisor", ajustes_con(openai_api_key="sk-openai", temperatura=0.7))
        assert espia[0][1]["temperature"] == 0.7

    def test_sin_clave_no_se_llega_a_construir_el_modelo(self, espia):
        # El SDK valida credenciales en el constructor: un guard posterior nunca correría.
        with pytest.raises(ErrorDeConfiguracion):
            modelos.crear_chat("supervisor", ajustes_con())
        assert espia == []


class TestEmbeddings:
    """El modelo del índice, siempre de OpenAI."""

    def test_exige_la_clave_de_openai_aunque_el_chat_sea_anthropic(self):
        # Anthropic no tiene API de embeddings: el índice se construye con OpenAI igual.
        with pytest.raises(ErrorDeConfiguracion, match="OPENAI_API_KEY"):
            modelos.crear_embeddings(ajustes_con(
                llm_provider=Proveedor.ANTHROPIC, anthropic_api_key="sk-anthropic"))

    def test_usa_el_modelo_de_embeddings_configurado(self, monkeypatch):
        capturado = {}

        def falso(**kwargs):
            capturado.update(kwargs)
            return object()

        monkeypatch.setattr(modelos, "OpenAIEmbeddings", falso)
        modelos.crear_embeddings(ajustes_con(
            openai_api_key="sk-openai", modelo_embeddings="text-embedding-3-large"))
        assert capturado["model"] == "text-embedding-3-large"
        assert isinstance(capturado["api_key"], SecretStr)


class TestSincronizacionDelFactory:
    """Los dos diccionarios cubren el enum entero.

    Es el mismo resguardo que `estado.py` usa para los destinos del grafo: sin él, agregar un
    proveedor y olvidarse de un diccionario da un KeyError en la primera llamada al modelo.
    """

    def test_cada_proveedor_tiene_su_cliente(self):
        assert set(modelos.CLIENTES) == set(Proveedor)

    def test_cada_proveedor_tiene_su_modelo_por_defecto(self):
        assert set(modelos.MODELOS_POR_DEFECTO) == set(Proveedor)

    def test_todo_proveedor_del_enum_construye_algo(self, monkeypatch):
        # Recorre el enum en vez de nombrar los proveedores: un proveedor nuevo entra solo.
        construidos = []
        for cliente in modelos.CLIENTES.values():
            monkeypatch.setattr(cliente, "construir",
                                lambda self, modelo, temperatura: construidos.append(modelo))
        for proveedor in Proveedor:
            modelos.crear_chat("supervisor", ajustes_con(
                llm_provider=proveedor,
                openai_api_key="sk-openai", anthropic_api_key="sk-anthropic"))
        assert len(construidos) == len(Proveedor)


class TestModeloDelRol:
    """La resolución del nombre de modelo, sin construir nada."""

    def test_sin_configurar_toma_el_default_del_proveedor(self):
        ajustes = ajustes_con(llm_provider=Proveedor.ANTHROPIC)
        assert modelos.modelo_del_rol("investigador", ajustes) == "claude-haiku-4-5"

    def test_configurado_gana(self):
        ajustes = ajustes_con(modelo_investigador="gpt-4o")
        assert modelos.modelo_del_rol("investigador", ajustes) == "gpt-4o"
