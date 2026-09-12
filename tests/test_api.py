"""Los endpoints: cupos, admisión, el stream SSE y lo que se expone sin autenticar.

La app se monta con `ASGITransport`, sin lifespan, y el estado se inyecta a mano: así la
suite prueba las rutas sin abrir un índice real ni llamar a ningún modelo. La admisión se
reemplaza por un doble, que es lo único que en producción habla con un proveedor.
"""

import json

import pytest
from httpx import ASGITransport, AsyncClient

import app.nucleo.constantes as cfg
from app.almacen.estado import Estado
from app.api import limites
from app.api.main import app, ciclo_de_vida
from app.nucleo.config import obtener_ajustes, reiniciar_ajustes
from app.servicio import admision
from app.servicio.motor import Motor
from tests.dobles import (
    FALLOS,
    armar,
    investigador_falso,
    lexico_de_prueba,
    redactor_sucio,
    supervisor_con_freno,
)

CONSULTA = "¿Que dijo la Corte sobre el exceso ritual manifiesto?"


class IndiceFalso:
    def __init__(self, lexico):
        self.lexico = lexico
        self.chroma = object()
        self.version = "prueba-0001"
        self.fragmentos = 3


@pytest.fixture
def entorno(tmp_path, monkeypatch):
    """La app con su estado inyectado y la admisión reemplazada."""
    # Del largo que el servicio exige: una sal corta se adivina igual que una publicada.
    monkeypatch.setenv("SAL_VISITANTE", "sal-de-prueba-de-treinta-y-dos-y-algo-mas")
    monkeypatch.setenv("ARCHIVO_ESTADO", str(tmp_path / "estado.sqlite3"))
    reiniciar_ajustes()

    indice = IndiceFalso(lexico_de_prueba(
        tmp_path / "lexico.sqlite3",
        padron={f.split(": ")[1]: "https://sjconsulta.csjn.gov.ar/x" for f in FALLOS},
    ))
    estado = Estado(tmp_path / "estado.sqlite3")
    app.state.estado = estado
    app.state.indice = indice
    app.state.motor = Motor(armar(), indice, estado, concurrentes=2)
    app.state.error_indice = ""

    async def admitir_todo(consulta, _chroma, _contador=None):
        return admision.Veredicto(True, "consulta sobre jurisprudencia", 0.9, True)

    monkeypatch.setattr(admision, "admitir", admitir_todo)
    monkeypatch.setattr("app.api.main.admision.admitir", admitir_todo)
    yield estado
    reiniciar_ajustes()


@pytest.fixture
async def cliente(entorno):
    # base_url https porque la cookie del visitante es `Secure`: sobre http el cliente la
    # recibe y no la guarda, y cada request parecería de alguien nuevo.
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="https://prueba") as c:
        yield c


async def leer_stream(cliente, consulta_id: str, desde: str | None = None) -> list[dict]:
    """Los eventos del stream, parseados, con su id.

    `desde` viaja como `Last-Event-ID`, que es lo que manda el navegador al reconectar.
    """
    eventos = []
    cabeceras = {"Last-Event-ID": desde} if desde is not None else {}
    async with cliente.stream("GET", f"/consultas/{consulta_id}/stream",
                              headers=cabeceras) as respuesta:
        nombre, ident = None, None
        async for linea in respuesta.aiter_lines():
            if linea.startswith("id:"):
                ident = linea.split(":", 1)[1].strip()
            elif linea.startswith("event:"):
                nombre = linea.split(":", 1)[1].strip()
            elif linea.startswith("data:") and nombre:
                eventos.append({"id": ident, "nombre": nombre,
                                "datos": json.loads(linea[5:].strip())})
    return eventos


class TestArranqueSinSal:
    """El servicio público no arranca sin `SAL_VISITANTE`.

    Es lo único que el arranque exige sin red de contención: un índice que falta se informa
    por `/salud` y el sitio sigue sirviendo la interfaz, pero sin sal los hashes de cupo se
    revierten a la IP del visitante y la página de privacidad estaría prometiendo algo falso.
    """

    async def test_el_ciclo_de_vida_corta_sin_sal(self, monkeypatch, tmp_path):
        from app.api.main import ciclo_de_vida
        from app.nucleo.errores import ErrorDeConfiguracion

        monkeypatch.delenv("SAL_VISITANTE", raising=False)
        monkeypatch.setenv("ARCHIVO_ENV", str(tmp_path / "sin.env"))
        monkeypatch.setenv("ARCHIVO_ESTADO", str(tmp_path / "estado.sqlite3"))
        monkeypatch.setenv("OPENAI_API_KEY", "sk-de-prueba")
        reiniciar_ajustes()
        try:
            with pytest.raises(ErrorDeConfiguracion, match="SAL_VISITANTE"):
                async with ciclo_de_vida(app):
                    pass
        finally:
            reiniciar_ajustes()

    async def test_una_sal_corta_tampoco_alcanza(self, monkeypatch, tmp_path):
        from app.api.main import ciclo_de_vida
        from app.nucleo.errores import ErrorDeConfiguracion

        monkeypatch.setenv("SAL_VISITANTE", "csjn")
        monkeypatch.setenv("ARCHIVO_ENV", str(tmp_path / "sin.env"))
        monkeypatch.setenv("ARCHIVO_ESTADO", str(tmp_path / "estado.sqlite3"))
        monkeypatch.setenv("OPENAI_API_KEY", "sk-de-prueba")
        reiniciar_ajustes()
        try:
            with pytest.raises(ErrorDeConfiguracion):
                async with ciclo_de_vida(app):
                    pass
        finally:
            reiniciar_ajustes()


class TestArranqueYSalud:
    """El arranque atiende primero y prepara el índice después.

    Mientras el lifespan no termina, uvicorn no abre el socket. Bajar ahí adentro los ~200 MB
    del artefacto dejaba el puerto cerrado hasta diez minutos —el timeout de la descarga— y el
    healthcheck de la plataforma se rinde mucho antes: el despliegue se marcaba fallido y el
    contenedor nuevo volvía a empezar la descarga desde cero.
    """

    def entorno_minimo(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SAL_VISITANTE", "sal-de-prueba-de-treinta-y-dos-y-algo-mas")
        monkeypatch.setenv("ARCHIVO_ENV", str(tmp_path / "sin.env"))
        monkeypatch.setenv("ARCHIVO_ESTADO", str(tmp_path / "estado.sqlite3"))
        monkeypatch.setenv("DIRECTORIO_INDICE", str(tmp_path / "indice"))
        monkeypatch.setenv("OPENAI_API_KEY", "sk-de-prueba")
        reiniciar_ajustes()

    async def test_el_arranque_no_espera_a_la_descarga(self, monkeypatch, tmp_path):
        import asyncio as aio

        from app.rag.ingesta import artefacto as art

        self.entorno_minimo(monkeypatch, tmp_path)
        empezo = aio.Event()
        soltar = aio.Event()

        def asegurar_lento(*_a, **_k):
            empezo.set()
            # Bloquea el hilo del pool, como lo haría una descarga de verdad.
            aio.run_coroutine_threadsafe(_esperar(soltar), bucle).result()
            return True

        async def _esperar(evento):
            await evento.wait()

        bucle = aio.get_running_loop()
        monkeypatch.setattr(art, "asegurar", asegurar_lento)
        monkeypatch.setattr("app.api.main.artefacto.asegurar", asegurar_lento)
        try:
            async with ciclo_de_vida(app):
                # El lifespan ya terminó con la descarga todavía corriendo: eso es lo que hace
                # que el puerto se abra y el healthcheck encuentre a alguien.
                await aio.wait_for(empezo.wait(), timeout=5)
                assert app.state.indice_estado == "descargando"
                assert app.state.motor is None
                soltar.set()
        finally:
            soltar.set()
            reiniciar_ajustes()

    async def test_una_descarga_que_falla_no_mata_el_proceso(self, monkeypatch, tmp_path):
        # Ninguna de las excepciones de la descarga es `OSError`: con la lista de tipos que
        # había, un Release movido de lugar tiraba el arranque en vez de dejar servir la
        # interfaz con el aviso.
        import asyncio as aio

        import httpx

        from app.rag.ingesta import artefacto as art

        self.entorno_minimo(monkeypatch, tmp_path)

        def explota(*_a, **_k):
            raise httpx.ConnectError("no se pudo conectar al Release")

        monkeypatch.setattr(art, "asegurar", explota)
        monkeypatch.setattr("app.api.main.artefacto.asegurar", explota)
        try:
            async with ciclo_de_vida(app):
                for _ in range(50):
                    if app.state.indice_estado != "descargando":
                        break
                    await aio.sleep(0.05)
                assert app.state.indice_estado == "ausente"
                assert "Release" in app.state.error_indice
        finally:
            reiniciar_ajustes()


class TestVivo:
    """El healthcheck mira que el proceso conteste, no que el índice esté."""

    async def test_responde_200_sin_indice(self, cliente):
        app.state.indice = None
        try:
            assert (await cliente.get("/vivo")).status_code == 200
            assert (await cliente.get("/salud")).status_code == 503
        finally:
            app.state.indice = app.state.motor.indice

    async def test_es_el_healthcheck_declarado(self):
        # Con `/salud` ahí, un artículo que no baja tumbaba el despliegue entero, incluido el
        # código que sí estaba bien.
        import json as _json
        from pathlib import Path as _Path

        railway = _json.loads(
            (_Path(__file__).resolve().parents[1] / "railway.json").read_text(encoding="utf-8"))
        assert railway["deploy"]["healthcheckPath"] == "/vivo"

    async def test_salud_dice_que_esta_descargando(self, cliente):
        app.state.indice = None
        app.state.indice_estado = "descargando"
        try:
            cuerpo = (await cliente.get("/salud")).json()
            assert cuerpo["indice"] == "descargando"
        finally:
            app.state.indice = app.state.motor.indice
            app.state.indice_estado = "listo"


class TestRespaldo:
    """Lo único irreemplazable del despliegue, detrás de un token.

    Adentro está el registro entero: doce meses de texto de consultas. Un endpoint así no
    puede existir por defecto, y cuando existe tiene que contestar lo mismo a quien no tiene
    el token que a quien busca una ruta que no está.
    """

    TOKEN = "un-token-de-respaldo-largo-y-propio-de-este-despliegue"

    async def test_sin_token_configurado_la_ruta_no_existe(self, cliente, monkeypatch):
        monkeypatch.delenv("TOKEN_RESPALDO", raising=False)
        reiniciar_ajustes()
        assert (await cliente.get("/respaldo")).status_code == 404

    async def test_un_token_corto_no_habilita_nada(self, cliente, monkeypatch):
        # Un token corto se adivina: vale lo mismo que no tenerlo.
        monkeypatch.setenv("TOKEN_RESPALDO", "csjn")
        reiniciar_ajustes()
        respuesta = await cliente.get("/respaldo", headers={"X-Token-Respaldo": "csjn"})
        assert respuesta.status_code == 404

    async def test_con_el_token_devuelve_una_base_abrible(self, cliente, entorno, monkeypatch,
                                                          tmp_path):
        import sqlite3

        monkeypatch.setenv("TOKEN_RESPALDO", self.TOKEN)
        reiniciar_ajustes()
        respuesta = await cliente.post("/consultas", json={"consulta": CONSULTA})
        await leer_stream(cliente, respuesta.json()["consulta_id"])

        bajado = await cliente.get("/respaldo", headers={"X-Token-Respaldo": self.TOKEN})
        assert bajado.status_code == 200
        copia = tmp_path / "bajado.sqlite3"
        copia.write_bytes(bajado.content)
        con = sqlite3.connect(copia)
        # La consulta recién corrida tiene que estar en la copia: es lo que prueba que el
        # respaldo trae lo último escrito y no una foto vieja del archivo en WAL.
        n = con.execute("SELECT count(*) FROM consultas").fetchone()[0]
        con.close()
        assert n >= 1

    async def test_el_token_equivocado_no_distingue_de_una_ruta_inexistente(self, cliente,
                                                                            monkeypatch):
        monkeypatch.setenv("TOKEN_RESPALDO", self.TOKEN)
        reiniciar_ajustes()
        con_token_malo = await cliente.get("/respaldo",
                                           headers={"X-Token-Respaldo": "otro-token-cualquiera"})
        sin_ruta = await cliente.get("/no-existe-esta-ruta")
        assert con_token_malo.status_code == sin_ruta.status_code == 404


class TestSalud:
    """Lo que el sitio dice de sí mismo, sin autenticar."""

    async def test_informa_el_estado_del_indice_y_el_cupo(self, cliente):
        cuerpo = (await cliente.get("/salud")).json()
        assert cuerpo["indice"] == "listo"
        assert cuerpo["modo"] == "abierto"
        assert cuerpo["cupo_restante"] == obtener_ajustes().consultas_por_visitante

    async def test_no_expone_infraestructura(self, cliente):
        # El endpoint es público: rutas, proyecto de trazas y nombres de modelo quedan afuera.
        cuerpo = (await cliente.get("/salud")).json()
        prohibidas = {"ruta", "directorio", "proyecto", "modelos", "modelo", "clave"}
        assert prohibidas.isdisjoint(cuerpo)

    async def test_sin_indice_responde_503(self, cliente):
        app.state.indice = None
        try:
            assert (await cliente.get("/salud")).status_code == 503
        finally:
            app.state.indice = app.state.motor.indice

    async def test_el_esquema_de_la_api_no_se_publica(self, cliente):
        assert (await cliente.get("/openapi.json")).status_code == 404


class TestCrearConsulta:
    """El único lugar donde se cobra cupo."""

    async def test_devuelve_202_con_su_id(self, cliente):
        respuesta = await cliente.post("/consultas", json={"consulta": CONSULTA})
        assert respuesta.status_code == 202
        assert respuesta.json()["consulta_id"]

    async def test_siembra_la_cookie_del_visitante(self, cliente):
        respuesta = await cliente.post("/consultas", json={"consulta": CONSULTA})
        assert limites.COOKIE in respuesta.cookies

    async def test_una_consulta_corta_se_rechaza(self, cliente):
        assert (await cliente.post("/consultas", json={"consulta": "hola"})).status_code == 422

    async def test_un_campo_de_mas_se_rechaza(self, cliente):
        respuesta = await cliente.post(
            "/consultas", json={"consulta": CONSULTA, "urgente": True})
        assert respuesta.status_code == 422

    async def test_una_segunda_consulta_en_vuelo_devuelve_409(self, cliente, entorno):
        # El grafo de prueba termina en microsegundos, así que la primera corrida se retiene
        # con un nodo que espera: sin eso, "en vuelo" no dura lo que tarda el segundo POST.
        import asyncio

        soltar = asyncio.Event()

        async def investigador_lento(state):
            await soltar.wait()
            return investigador_falso(state)

        app.state.motor = Motor(armar(investigador=investigador_lento),
                                app.state.indice, entorno, concurrentes=2)
        primera = await cliente.post("/consultas", json={"consulta": CONSULTA})
        segunda = await cliente.post("/consultas", json={"consulta": CONSULTA})
        soltar.set()
        assert segunda.status_code == 409
        assert segunda.json()["consulta_id"] == primera.json()["consulta_id"]

    async def test_un_rechazo_tambien_siembra_la_cookie(self, cliente, monkeypatch, entorno):
        # Sin cookie en el rechazo, cada intento sería un visitante nuevo y el contador de
        # intentos —el que frena a quien martillea— no acumularía nunca.
        async def rechazar(consulta, _chroma, _contador=None):
            return admision.Veredicto(False, "fuera de alcance", 0.05, True)

        monkeypatch.setattr("app.api.main.admision.admitir", rechazar)
        respuesta = await cliente.post("/consultas", json={"consulta": "receta de milanesas"})
        assert limites.COOKIE in respuesta.cookies


class TestCupos:
    """Lo que ve quien se queda sin consultas."""

    async def test_al_agotar_el_cupo_responde_429_con_la_reposicion(self, cliente, entorno):
        tope = obtener_ajustes().consultas_por_visitante
        for i in range(tope):
            respuesta = await cliente.post("/consultas", json={"consulta": CONSULTA})
            await leer_stream(cliente, respuesta.json()["consulta_id"])
        agotada = await cliente.post("/consultas", json={"consulta": CONSULTA})
        assert agotada.status_code == 429
        assert agotada.json()["reintentar_en"]
        assert str(tope) in agotada.json()["detail"]

    async def test_el_cupo_restante_baja(self, cliente):
        respuesta = await cliente.post("/consultas", json={"consulta": CONSULTA})
        assert respuesta.json()["cupo_restante"] == \
            obtener_ajustes().consultas_por_visitante - 1

    async def test_el_techo_global_pasa_a_modo_lectura(self, cliente, entorno, monkeypatch):
        monkeypatch.setenv("CONSULTAS_POR_DIA", "1")
        reiniciar_ajustes()
        entorno.anotar_consulta_global()
        respuesta = await cliente.post("/consultas", json={"consulta": CONSULTA})
        assert respuesta.status_code == 503
        assert (await cliente.get("/salud")).json()["modo"] == "lectura"

    async def test_el_presupuesto_agotado_frena(self, cliente, entorno, monkeypatch):
        monkeypatch.setenv("PRESUPUESTO_USD_MES", "0.001")
        reiniciar_ajustes()
        entorno.sumar_gasto(1.0, 1, 100, 100)
        assert (await cliente.post("/consultas",
                                   json={"consulta": CONSULTA})).status_code == 503


class TestElCupoNoSeEludeTirandoLaCookie:
    """Un cliente que no devuelve la cookie no puede gastar sin techo.

    La admisión cuesta un embedding y una llamada al clasificador, y corre **antes** de que la
    consulta se cobre: una consulta rechazada paga esas dos y no toca el contador de consultas.
    Con el intento anotado solo contra la cookie, quien la descarta recibía una nueva en cada
    request y su contador arrancaba siempre en cero: martillear era gratis para él y pago para
    el sitio.
    """

    async def sin_cookie(self, cliente, consulta=CONSULTA):
        """Un POST que no devuelve la cookie, como un cliente de línea de comandos."""
        cliente.cookies.clear()
        return await cliente.post("/consultas", json={"consulta": consulta})

    async def test_sin_cookie_el_tope_por_ip_termina_cortando(self, cliente, entorno,
                                                              monkeypatch):
        monkeypatch.setenv("INTENTOS_POR_IP", "3")
        reiniciar_ajustes()
        codigos = []
        for _ in range(5):
            respuesta = await self.sin_cookie(cliente)
            codigos.append(respuesta.status_code)
            if respuesta.status_code == 202:
                await leer_stream(cliente, respuesta.json()["consulta_id"])
        assert 429 in codigos, f"nunca cortó: {codigos}"

    async def test_el_intento_se_anota_contra_la_ip(self, cliente, entorno):
        antes = entorno.usos(limites.identificar_ip_de_prueba(), limites.TIPO_INTENTO)             if hasattr(limites, "identificar_ip_de_prueba") else None
        await self.sin_cookie(cliente)
        # La IP del cliente de prueba es la de `ASGITransport`; lo que importa es que alguna
        # clave distinta de la cookie haya quedado anotada.
        with entorno._conexion as _:
            filas = entorno._conexion.execute(
                "SELECT count(DISTINCT clave) AS n FROM pulsos WHERE tipo = ?",
                (limites.TIPO_INTENTO,)).fetchone()
        assert filas["n"] >= 2, "el intento tiene que anotarse contra la cookie y contra la IP"

    async def test_con_cookie_el_tope_propio_sigue_siendo_el_primero(self, cliente, entorno,
                                                                    monkeypatch):
        # La cookie reparte con equidad y sigue cortando antes que el piso por IP.
        monkeypatch.setenv("INTENTOS_POR_VISITANTE", "2")
        monkeypatch.setenv("INTENTOS_POR_IP", "99")
        reiniciar_ajustes()
        for _ in range(2):
            respuesta = await cliente.post("/consultas", json={"consulta": CONSULTA})
            if respuesta.status_code == 202:
                await leer_stream(cliente, respuesta.json()["consulta_id"])
        agotada = await cliente.post("/consultas", json={"consulta": CONSULTA})
        assert agotada.status_code == 429
        assert "2" in agotada.json()["detail"]


class TestElCupoNoSeEludeEnParalelo:
    """Disparar pedidos a la vez no pasa el tope.

    Entre mirar el contador y cobrarlo había una llamada de red —la admisión—, y con `await`
    de por medio varios pedidos leían el mismo número, lo encontraban por debajo del tope, y
    cobraban todos. El tope se pasaba justo haciendo lo que hace quien lo quiere pasar.
    """

    async def test_cinco_a_la_vez_no_pasan_un_tope_de_dos(self, cliente, entorno, monkeypatch):
        import asyncio as aio

        monkeypatch.setenv("CONSULTAS_POR_VISITANTE", "2")
        monkeypatch.setenv("CONSULTAS_POR_IP", "99")
        monkeypatch.setenv("CONSULTAS_POR_DIA", "99")
        monkeypatch.setenv("INTENTOS_POR_VISITANTE", "99")
        monkeypatch.setenv("INTENTOS_POR_IP", "99")
        reiniciar_ajustes()

        # La admisión tarda, como una llamada de red: es la ventana donde se cruzaban.
        async def admitir_lento(_consulta, _chroma, _contador=None):
            await aio.sleep(0.05)
            return admision.Veredicto(True, "sobre jurisprudencia", 0.9, True)

        monkeypatch.setattr("app.api.main.admision.admitir", admitir_lento)

        # La cookie se siembra con un pedido previo: cinco pedidos a la vez desde un navegador
        # que todavía no la tiene son cinco visitantes distintos, y el tope por visitante no
        # tendría nada que decir. Quien quiere pasar el suyo ya la tiene.
        primera = await cliente.post("/consultas", json={"consulta": CONSULTA})
        await leer_stream(cliente, primera.json()["consulta_id"])

        respuestas = await aio.gather(*[
            cliente.post("/consultas", json={"consulta": CONSULTA}) for _ in range(5)])
        aceptadas = [r for r in respuestas if r.status_code == 202]
        for r in aceptadas:
            await leer_stream(cliente, r.json()["consulta_id"])
        # Con la primera ya cobrada, del tope de dos queda una.
        assert len(aceptadas) == 1, [r.status_code for r in respuestas]
        assert all(r.status_code == 429 for r in respuestas if r.status_code != 202)

    async def test_el_techo_del_sitio_tampoco_se_pasa_en_paralelo(self, cliente, entorno,
                                                                 monkeypatch):
        import asyncio as aio

        monkeypatch.setenv("CONSULTAS_POR_DIA", "1")
        monkeypatch.setenv("CONSULTAS_POR_VISITANTE", "99")
        monkeypatch.setenv("CONSULTAS_POR_IP", "99")
        monkeypatch.setenv("INTENTOS_POR_VISITANTE", "99")
        monkeypatch.setenv("INTENTOS_POR_IP", "99")
        reiniciar_ajustes()

        async def admitir_lento(_consulta, _chroma, _contador=None):
            await aio.sleep(0.05)
            return admision.Veredicto(True, "sobre jurisprudencia", 0.9, True)

        monkeypatch.setattr("app.api.main.admision.admitir", admitir_lento)
        respuestas = await aio.gather(*[
            cliente.post("/consultas", json={"consulta": CONSULTA}) for _ in range(4)])
        aceptadas = [r for r in respuestas if r.status_code == 202]
        for r in aceptadas:
            await leer_stream(cliente, r.json()["consulta_id"])
        assert len(aceptadas) == 1, [r.status_code for r in respuestas]
        assert entorno.consultas_del_dia() == 1


class TestAdmision:
    """El límite de tema, antes de gastar una corrida."""

    async def test_una_consulta_fuera_de_alcance_no_corre(self, cliente, monkeypatch, entorno):
        async def rechazar(consulta, _chroma, _contador=None):
            return admision.Veredicto(False, "no es una consulta jurídica", 0.05, True)

        monkeypatch.setattr("app.api.main.admision.admitir", rechazar)
        respuesta = await cliente.post("/consultas", json={"consulta": "receta de milanesas"})
        assert respuesta.status_code == 422
        assert "no es una consulta jurídica" in respuesta.json()["motivo"]

    async def test_el_rechazo_gasta_intento_y_no_consulta(self, cliente, monkeypatch, entorno):
        async def rechazar(consulta, _chroma, _contador=None):
            return admision.Veredicto(False, "fuera de alcance", 0.05, True)

        monkeypatch.setattr("app.api.main.admision.admitir", rechazar)
        await cliente.post("/consultas", json={"consulta": "receta de milanesas"})
        assert (await cliente.get("/salud")).json()["cupo_restante"] == \
            obtener_ajustes().consultas_por_visitante

    async def test_el_rechazo_queda_registrado(self, cliente, monkeypatch, entorno):
        async def rechazar(consulta, _chroma, _contador=None):
            return admision.Veredicto(False, "fuera de alcance", 0.05, True)

        monkeypatch.setattr("app.api.main.admision.admitir", rechazar)
        respuesta = await cliente.post("/consultas", json={"consulta": "receta de milanesas"})
        fila = entorno.leer_consulta(respuesta.json()["consulta_id"])
        assert fila["admitida"] == 0


class TestStream:
    """El SSE: qué llega, en qué orden, y con qué cabeceras."""

    async def test_llegan_avance_texto_citas_y_cierre(self, cliente):
        respuesta = await cliente.post("/consultas", json={"consulta": CONSULTA})
        eventos = await leer_stream(cliente, respuesta.json()["consulta_id"])
        nombres = [e["nombre"] for e in eventos]
        assert "estado" in nombres and "cita" in nombres and "final" in nombres
        assert nombres.index("estado") < nombres.index("final")

    async def test_las_cabeceras_impiden_el_buffering(self, cliente):
        respuesta = await cliente.post("/consultas", json={"consulta": CONSULTA})
        consulta_id = respuesta.json()["consulta_id"]
        async with cliente.stream("GET", f"/consultas/{consulta_id}/stream") as stream:
            assert stream.headers["x-accel-buffering"] == "no"
            assert "no-cache" in stream.headers["cache-control"]
            assert stream.headers["content-type"].startswith("text/event-stream")

    async def test_un_id_inexistente_da_404(self, cliente):
        assert (await cliente.get("/consultas/no-existe/stream")).status_code == 404

    async def test_un_corte_llega_como_sin_base(self, cliente, entorno):
        app.state.motor = Motor(armar(redactor=redactor_sucio,
                                      supervisor=supervisor_con_freno),
                                app.state.indice, entorno, concurrentes=2)
        respuesta = await cliente.post("/consultas", json={"consulta": CONSULTA})
        eventos = await leer_stream(cliente, respuesta.json()["consulta_id"])
        assert "sin_base" in [e["nombre"] for e in eventos]


class TestReconexion:
    """`Last-Event-ID` dice qué vio el cliente, y el servidor sigue desde el siguiente.

    Es el camino que hace que una caída de conexión no cueste la corrida: el navegador
    reconecta solo y el servidor reproduce lo que falta. Tomándolo como punto de partida en vez
    de como último recibido, cada reconexión repetía un evento —un delta de texto duplicado, o
    una cita pintada dos veces—.
    """

    async def corrida_terminada(self, cliente):
        respuesta = await cliente.post("/consultas", json={"consulta": CONSULTA})
        consulta_id = respuesta.json()["consulta_id"]
        return consulta_id, await leer_stream(cliente, consulta_id)

    async def test_los_eventos_van_numerados_desde_cero(self, cliente):
        _, eventos = await self.corrida_terminada(cliente)
        assert [e["id"] for e in eventos] == [str(i) for i in range(len(eventos))]

    async def test_reconectar_no_repite_el_ultimo_visto(self, cliente):
        consulta_id, todos = await self.corrida_terminada(cliente)
        assert len(todos) > 2
        corte = 1
        resto = await leer_stream(cliente, consulta_id, desde=str(corte))
        assert [e["id"] for e in resto] == [str(i) for i in range(corte + 1, len(todos))]

    async def test_la_reproduccion_empalma_exacta_con_lo_ya_visto(self, cliente):
        consulta_id, todos = await self.corrida_terminada(cliente)
        corte = 1
        resto = await leer_stream(cliente, consulta_id, desde=str(corte))
        rearmado = todos[:corte + 1] + resto
        assert [e["datos"] for e in rearmado] == [e["datos"] for e in todos]

    async def test_sin_cabecera_se_reproduce_todo(self, cliente):
        consulta_id, todos = await self.corrida_terminada(cliente)
        assert len(await leer_stream(cliente, consulta_id)) == len(todos)

    async def test_un_last_event_id_no_numerico_no_rompe(self, cliente):
        # Un proxy o una extensión pueden mandar cualquier cosa; `int()` sobre eso era un 500.
        consulta_id, todos = await self.corrida_terminada(cliente)
        assert len(await leer_stream(cliente, consulta_id, desde="abc")) == len(todos)


class TestResultado:
    """Leer una consulta terminada por su id: recargar y compartir."""

    async def test_devuelve_la_respuesta_y_sus_citas(self, cliente):
        respuesta = await cliente.post("/consultas", json={"consulta": CONSULTA})
        consulta_id = respuesta.json()["consulta_id"]
        await leer_stream(cliente, consulta_id)
        cuerpo = (await cliente.get(f"/consultas/{consulta_id}")).json()
        assert cuerpo["desenlace"] == cfg.PUBLICADA
        assert cuerpo["respuesta"] and len(cuerpo["citas"]) == 3

    async def test_un_id_inexistente_da_404(self, cliente):
        assert (await cliente.get("/consultas/no-existe")).status_code == 404


class TestMetricasYCorpus:
    """Los dos endpoints que muestran el trabajo."""

    async def test_las_metricas_son_agregados(self, cliente):
        cuerpo = (await cliente.get("/metricas")).json()
        assert set(cuerpo) == {"consultas", "publicadas", "sin_base", "fuera_de_alcance",
                               "latencia_ms", "gasto_usd"}

    async def test_el_corpus_lista_sus_documentos(self, cliente):
        cuerpo = (await cliente.get("/corpus")).json()
        assert cuerpo["version"] == "prueba-0001"
        assert isinstance(cuerpo["documentos"], list)
