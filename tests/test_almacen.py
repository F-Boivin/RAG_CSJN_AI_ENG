"""El almacén: el índice léxico y el estado del servicio, contra SQLite de verdad.

No hay dobles acá. Un SQLite en `tmp_path` cuesta milisegundos y ejercita exactamente las
consultas que corren en producción, incluida la sintaxis de FTS5, que es donde un doble
mentiría.
"""

import threading
import time
from datetime import timedelta

import pytest

import app.nucleo.constantes as cfg
from app.almacen.consulta import Lexico, _terminos
from app.almacen.escritura import EscritorLexico
from app.almacen.estado import Estado, ahora


class _CursorListo:
    """Un cursor con las filas ya traídas, para el doble de conexión de los tests."""

    def __init__(self, filas):
        self._filas = filas

    def fetchall(self):
        return self._filas

    def fetchone(self):
        return self._filas[0] if self._filas else None


CITA_CON_LINK = "343:2211"
CITA_SIN_LINK = "306:1892"


def fragmento(i, origen, subseccion, texto, citas_urls=None):
    return {"id": f"{origen}#{i:04d}", "origen": origen, "seccion": "Seccion",
            "subseccion": subseccion, "fuente": "nota", "pagina": i + 1,
            "texto": texto, "citas_urls": citas_urls or {}}


def lexico_ambiguo(tmp_path) -> Lexico:
    """Dos documentos con el mismo sufijo de subsección."""
    ruta = tmp_path / "ambiguo.sqlite3"
    with EscritorLexico(ruta) as escritor:
        escritor.escribir_fragmentos([
            fragmento(0, "nota-1", "Uno · págs. 1-10", "Texto de la nota uno."),
            fragmento(1, "nota-2", "Dos · págs. 1-10", "Texto de la nota dos."),
        ])
    return Lexico(ruta, solo_lectura=True)


@pytest.fixture
def lexico(tmp_path) -> Lexico:
    ruta = tmp_path / "lexico.sqlite3"
    with EscritorLexico(ruta) as escritor:
        escritor.escribir_fragmentos([
            fragmento(0, "nota-1", "Imagen · 1. Concepto",
                      "La Corte tutela el derecho a la imagen (Fallos: 343:2211).",
                      {CITA_CON_LINK: "https://sjconsulta.csjn.gov.ar/uno"}),
            fragmento(1, "nota-1", "Imagen · 2. Excepciones",
                      "El interes publico admite excepciones.", {}),
            fragmento(2, "nota-2", "Arbitrariedad · 1. Exceso ritual",
                      "El exceso ritual manifiesto sacrifica la verdad juridica objetiva.",
                      {CITA_SIN_LINK: ""}),
        ])
        escritor.registrar_documento({"origen": "nota-1", "tipo": "nota", "titulo": "Imagen",
                                      "paginas": 13, "fragmentos": 2,
                                      "metodo_subsecciones": "titulos"})
        escritor.completar_links_faltantes()
    return Lexico(ruta, solo_lectura=True)


class TestPadron:
    """El padrón, que es contra lo que el verificador compara."""

    def test_una_cita_del_corpus_existe(self, lexico):
        assert lexico.existe("Fallos: 343:2211")

    def test_una_cita_inventada_no_existe(self, lexico):
        assert not lexico.existe("Fallos: 999:9999")

    def test_la_forma_de_escribirla_no_importa(self, lexico):
        assert lexico.existe("343:2211") and lexico.existe("Fallos 343:2211")

    def test_una_cita_sin_link_en_el_pdf_igual_recibe_el_oficial(self, lexico):
        # Trece documentos del corpus no enlazan una sola cita: el link se arma por código con
        # la plantilla de la Corte, y nunca sale de un modelo.
        url = lexico.link("Fallos: 306:1892")
        assert url.startswith("https://sjconsulta.csjn.gov.ar/")
        assert "tomo=306" in url and "pagina=1892" in url


class TestUnaConexionUnLock:
    """Ninguna lectura toca la conexión sin exclusión.

    La conexión se abre con `check_same_thread=False` —las consultas corren en
    `asyncio.to_thread` y el hilo del pool cambia entre llamadas—, así que sqlite3 deja de
    controlar y la exclusión queda a cargo de esta clase. Cuatro caminos entran desde hilos
    distintos: `buscar` desde el recuperador léxico, el padrón desde el verificador y desde la
    herramienta de búsqueda, y las subsecciones desde la resolución de nombres.

    Lo que se prueba no es la carrera, que es intermitente por definición, sino el invariante:
    se envuelve la conexión y se falla en cuanto dos hilos se solapan adentro de una consulta.
    Sin el lock esto aparecía como `IndexError: tuple index out of range` desde adentro de
    `fetchall`, y se llevaba puesta la consulta entera después de cobrarle el cupo al visitante.
    """

    class ConexionCelosa:
        """Envuelve la conexión y anota si dos hilos están adentro a la vez."""

        def __init__(self, real):
            self._real = real
            self._adentro = 0
            self._testigo = threading.Lock()
            self.solapamientos = 0

        def execute(self, *args, **kwargs):
            with self._testigo:
                self._adentro += 1
                if self._adentro > 1:
                    self.solapamientos += 1
            try:
                # `fetchall` se llama afuera, así que el cursor tiene que traer todo ahora:
                # es lo que hace observable que la lectura entera quede bajo el lock.
                filas = self._real.execute(*args, **kwargs).fetchall()
                time.sleep(0.001)  # ensancha la ventana para que un solapamiento se vea
                return _CursorListo(filas)
            finally:
                with self._testigo:
                    self._adentro -= 1

        def close(self):
            self._real.close()

    def vigilar(self, lexico):
        celosa = self.ConexionCelosa(lexico._conexion)
        lexico._conexion = celosa
        return celosa

    def martillar(self, lexico, vueltas=12, hilos=6):
        errores = []
        arranque = threading.Event()

        def trabajo(i):
            arranque.wait()
            for k in range(vueltas):
                try:
                    lexico.buscar("imagen excepciones", 4)
                    # Sin caché, a diferencia del padrón: cada vuelta entra a la conexión.
                    lexico.cantidad_fragmentos()
                    lexico.subsecciones()
                except Exception as exc:  # cualquier fallo cuenta
                    errores.append(exc)

        equipo = [threading.Thread(target=trabajo, args=(i,)) for i in range(hilos)]
        for h in equipo:
            h.start()
        arranque.set()
        for h in equipo:
            h.join()
        return errores

    def test_dos_hilos_nunca_entran_juntos_a_la_conexion(self, lexico):
        celosa = self.vigilar(lexico)
        errores = self.martillar(lexico)
        assert celosa.solapamientos == 0
        assert errores == []

    def test_el_centinela_detecta_un_acceso_sin_lock(self, lexico):
        # Comprueba que la prueba de arriba puede fallar: sin pasar por el lock, el centinela
        # ve el solapamiento. Un test de concurrencia que no sabe fallar no prueba nada.
        celosa = self.vigilar(lexico)
        arranque = threading.Event()

        def sin_lock():
            arranque.wait()
            for _ in range(12):
                celosa.execute("SELECT count(*) AS n FROM fragmentos", ())

        equipo = [threading.Thread(target=sin_lock) for _ in range(6)]
        for h in equipo:
            h.start()
        arranque.set()
        for h in equipo:
            h.join()
        assert celosa.solapamientos > 0


class TestClaveDelPadron:
    """El padrón se guarda con la clave por la que después se lo lee.

    `existe`, `link` y el evento de cita pasan por `normalizar_cita`, así que una entrada
    guardada con la forma cruda queda inalcanzable. Medido sobre el índice real antes del
    arreglo: 557 claves sin normalizar y 330 sin gemela, o sea 330 citas del corpus que el
    verificador daba por inventadas.
    """

    def escribir(self, tmp_path, citas_urls):
        ruta = tmp_path / "padron.sqlite3"
        with EscritorLexico(ruta) as escritor:
            escritor.escribir_fragmentos([
                fragmento(0, "cuadernillo-6-1", "6.1.1 Origen", "Texto de prueba.", citas_urls)])
        return Lexico(ruta, solo_lectura=True)

    def test_una_cita_escrita_como_la_imprime_el_cuadernillo_se_encuentra(self, tmp_path):
        # Así la escribe el corpus markdown, y así entraba al padrón.
        lexico = self.escribir(tmp_path, {"Fallos: 112:384": "https://ejemplo/112-384"})
        assert lexico.existe("Fallos: 112:384")
        assert lexico.existe("112:384")
        assert lexico.link("112:384") == "https://ejemplo/112-384"

    def test_la_clave_guardada_es_la_normalizada(self, tmp_path):
        lexico = self.escribir(tmp_path, {"Fallos: 112:384": "https://ejemplo/112-384"})
        assert list(lexico.padron()) == ["112:384"]

    def test_lo_que_no_trae_tomo_y_pagina_queda_afuera(self, tmp_path):
        # Por esta puerta entraban 86 entradas que no son citas: expedientes, nombres de caso
        # y etiquetas de voto, todas texto de anclas que el corpus no puede respaldar.
        lexico = self.escribir(tmp_path, {
            "Fallos: 112:384": "https://ejemplo/112-384",
            "M. 358. XLII. REX": "https://ejemplo/expediente",
            "(Disidencia del juez Lorenzetti)": "https://ejemplo/voto",
            '"Gómez"': "https://ejemplo/caso",
        })
        assert list(lexico.padron()) == ["112:384"]

    def test_dos_formas_de_la_misma_cita_son_una(self, tmp_path):
        lexico = self.escribir(tmp_path, {
            "Fallos: 112:384": "https://ejemplo/112-384", "112:384": ""})
        assert list(lexico.padron()) == ["112:384"]
        assert lexico.link("112:384") == "https://ejemplo/112-384"

    def test_la_forma_sin_link_no_pisa_a_la_que_enlaza(self, tmp_path):
        # El orden del dict no puede decidir si una cita conserva su link oficial.
        lexico = self.escribir(tmp_path, {
            "112:384": "", "Fallos: 112:384": "https://ejemplo/112-384"})
        assert lexico.link("112:384") == "https://ejemplo/112-384"

    def test_la_tabla_por_subseccion_las_guarda_normalizadas(self, tmp_path):
        # La escribe la ingesta con las mismas claves que el padrón, y el fragmento las
        # muestra como "Fallos: {cita}": la clave cruda daba "Fallos: Fallos: 112:384".
        lexico = self.escribir(tmp_path, {
            "Fallos: 112:384": "https://ejemplo/112-384",
            "(Disidencia del juez Lorenzetti)": "https://ejemplo/voto",
        })
        filas = lexico._filas("SELECT cita FROM citas_por_subseccion")
        assert [f["cita"] for f in filas] == ["112:384"]


class TestSubsecciones:
    """La tolerancia de nombres, y el corte cuando hay ambigüedad."""

    def test_el_nombre_exacto_resuelve(self, lexico):
        assert lexico.resolver_subseccion("Imagen · 1. Concepto") == "Imagen · 1. Concepto"

    def test_sin_tildes_ni_caja_tambien_resuelve(self, lexico):
        assert lexico.resolver_subseccion("imagen · 1. concepto") == "Imagen · 1. Concepto"

    def test_una_inventada_no_resuelve(self, lexico):
        assert lexico.resolver_subseccion("Doctrina inventada") is None

    def test_resuelve_por_la_mitad_con_significado(self, lexico):
        # Los nombres son «Documento · Título» y el modelo escribe el título: copia la parte
        # que dice algo. Medido contra el corpus real: sin esta tolerancia el verificador
        # rechaza casi toda investigación por subsección inventada.
        assert lexico.resolver_subseccion("1. Concepto") == "Imagen · 1. Concepto"

    def test_un_sufijo_ambiguo_devuelve_none(self, tmp_path):
        # Los rangos de página del fallback se repiten entre documentos: elegir sería adivinar.
        lx = lexico_ambiguo(tmp_path)
        assert lx.resolver_subseccion("págs. 1-10") is None


class TestBusquedaLexica:
    """FTS5: lo que el lado léxico aporta sobre el vectorial."""

    def test_encuentra_por_palabra(self, lexico):
        ids = [i for i, _, _ in lexico.buscar("exceso ritual", 5)]
        assert "nota-2#0002" in ids

    def test_encuentra_una_cita_escrita_literal(self, lexico):
        # `tokenchars ':'` mantiene la cita como un token: sin eso se partiría en dos números.
        ids = [i for i, _, _ in lexico.buscar("Fallos: 343:2211", 5)]
        assert "nota-1#0000" in ids

    def test_el_titulo_hace_encontrable_al_documento_por_su_tema(self, tmp_path):
        """La columna de título es lo que hace que una consulta que nombra un tema encuentre
        al documento que trata de ese tema.

        Sin ella la consulta compite solo contra el cuerpo de los fragmentos, y gana el que
        repite esas palabras aunque sea de otra cosa. Medido sobre el corpus real con 21
        consultas etiquetadas: 73% de precisión en el top-4 sin la columna, 90% con ella.
        """
        ruta = tmp_path / "titulos.sqlite3"
        with EscritorLexico(ruta) as escritor:
            escritor.escribir_fragmentos([
                # El documento del tema, cuyo cuerpo casi no repite las palabras del título.
                {**fragmento(0, "suplemento-1", "Interés Superior del Niño · 1. Criterios",
                             "La adecuada apreciacion de las circunstancias facticas y la "
                             "evaluacion de los informes tecnicos condicionan la decision."),
                 "seccion": "Interés Superior del Niño"},
                # Un fallo ajeno que sí las repite.
                {**fragmento(1, "suplemento-9", "Ambiental · Texto del Fallo",
                             "El interes superior del niño fue invocado por el actor; el "
                             "interes superior del niño no altera la competencia. Interes."),
                 "seccion": "Suplemento Ambiental"},
            ])
        lx = Lexico(ruta, solo_lectura=True)
        assert [m["origen"] for _, _, m in lx.buscar("interés superior del niño", 2)][0]             == "suplemento-1"

    def test_la_puntuacion_de_una_consulta_no_rompe_la_sintaxis(self, lexico):
        # FTS5 lee `-`, `*` y `AND` como sintaxis: una consulta en lenguaje natural los trae.
        assert lexico.buscar("¿qué dijo la Corte -- sobre * imagen? AND", 5) is not None

    def test_las_palabras_vacias_no_entran_a_la_busqueda(self, lexico):
        # La búsqueda es un OR ordenado por bm25: con "por", "de" y "la" adentro, el
        # documento más largo del corpus gana por repetirlas y desplaza al que trata el tema.
        assert _terminos("¿que dijo la Corte sobre el exceso ritual?") == ["Corte", "exceso", "ritual"]

    def test_una_cita_sobrevive_al_filtro(self, lexico):
        assert "343:2211" in _terminos("Fallos: 343:2211")

    def test_una_consulta_toda_vacia_se_busca_igual(self, lexico):
        # Mejor un resultado flojo que ninguno.
        assert _terminos("que dijo sobre eso") != []

    def test_una_consulta_vacia_devuelve_nada(self, lexico):
        assert lexico.buscar("   ", 5) == []

    def test_los_metadatos_viajan_con_el_fragmento(self, lexico):
        _, _, metadata = lexico.buscar("exceso ritual", 1)[0]
        assert metadata["subseccion"] == "Arbitrariedad · 1. Exceso ritual"
        assert metadata["origen"] == "nota-2"


class TestIngestaIncremental:
    """Reindexar un documento no toca a los demás."""

    def test_borrar_y_reescribir_deja_solo_lo_nuevo(self, tmp_path):
        ruta = tmp_path / "lexico.sqlite3"
        with EscritorLexico(ruta) as escritor:
            escritor.escribir_fragmentos([
                fragmento(0, "nota-1", "A", "Texto viejo de la nota uno."),
                fragmento(1, "nota-2", "B", "Texto de la nota dos."),
            ])
            assert escritor.borrar_documento("nota-1") == 1
            escritor.escribir_fragmentos([
                fragmento(0, "nota-1", "A", "Texto nuevo de la nota uno."),
            ])
        lexico = Lexico(ruta, solo_lectura=True)
        assert lexico.cantidad_fragmentos() == 2
        assert lexico.buscar("viejo", 5) == []
        assert [i for i, _, _ in lexico.buscar("nuevo", 5)] == ["nota-1#0000"]
        # La consulta es un OR de términos, así que "nota" matchea los dos; lo que se
        # comprueba es que el documento intacto siga primero.
        assert [i for i, _, _ in lexico.buscar("nota dos", 5)][0] == "nota-2#0001"

    def test_la_cita_de_un_documento_borrado_sale_del_padron(self, tmp_path):
        ruta = tmp_path / "lexico.sqlite3"
        with EscritorLexico(ruta) as escritor:
            escritor.escribir_fragmentos([
                fragmento(0, "nota-1", "A", "Con cita.", {"311:2437": "https://u/1"})])
            escritor.borrar_documento("nota-1")
        assert Lexico(ruta, solo_lectura=True).padron() == {}


@pytest.fixture
def estado(tmp_path) -> Estado:
    return Estado(tmp_path / "estado.sqlite3")


class TestCuotas:
    """La ventana rodante de 24 h y el techo diario."""

    def test_cada_pulso_cuenta_uno(self, estado):
        for _ in range(3):
            estado.anotar("visitante-1", "consulta")
        assert estado.usos("visitante-1", "consulta") == 3

    def test_los_pulsos_de_otro_visitante_no_suman(self, estado):
        estado.anotar("visitante-1", "consulta")
        assert estado.usos("visitante-2", "consulta") == 0

    def test_los_tipos_se_cuentan_por_separado(self, estado):
        estado.anotar("visitante-1", "intento")
        assert estado.usos("visitante-1", "consulta") == 0

    def test_un_pulso_fuera_de_la_ventana_no_cuenta(self, estado):
        viejo = (ahora() - timedelta(hours=30)).isoformat()
        estado._escribir("INSERT INTO pulsos (clave, tipo, ts) VALUES (?, ?, ?)",
                         ("visitante-1", "consulta", viejo))
        assert estado.usos("visitante-1", "consulta") == 0

    def test_la_reposicion_sale_del_pulso_mas_viejo(self, estado):
        estado.anotar("visitante-1", "consulta")
        cuando = estado.se_repone("visitante-1", "consulta")
        assert timedelta(hours=23) < (cuando - ahora()) <= timedelta(hours=24)

    def test_el_techo_diario_acumula(self, estado):
        for _ in range(4):
            estado.anotar_consulta_global()
        assert estado.consultas_del_dia() == 4

    def test_la_poda_borra_lo_vencido_y_deja_lo_vigente(self, estado):
        estado.anotar("visitante-1", "consulta")
        viejo = (ahora() - timedelta(hours=cfg.HORAS_RETENCION_PULSOS + 1)).isoformat()
        estado._escribir("INSERT INTO pulsos (clave, tipo, ts) VALUES (?, ?, ?)",
                         ("visitante-9", "consulta", viejo))
        assert estado.podar() == 1
        assert estado.usos("visitante-1", "consulta") == 1

    def test_dos_hilos_no_regalan_cupo(self, estado):
        # Las corridas concurrentes cobran desde el pool de hilos: un cobro perdido es un cupo
        # regalado.
        import threading

        hilos = [threading.Thread(target=estado.anotar, args=("visitante-1", "consulta"))
                 for _ in range(20)]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join()
        assert estado.usos("visitante-1", "consulta") == 20


class TestGastoYRegistro:
    """Lo que sostiene el freno de presupuesto y el objetivo de recolectar consultas."""

    def test_el_gasto_del_mes_acumula(self, estado):
        estado.sumar_gasto(0.004, 3, 12000, 800)
        estado.sumar_gasto(0.006, 2, 9000, 500)
        assert estado.gasto_del_mes() == pytest.approx(0.010)

    def test_una_consulta_registrada_se_puede_leer(self, estado):
        estado.registrar({"id": "abc", "recibida_en": ahora().isoformat(),
                          "consulta": "una consulta", "admitida": 1, "desenlace": "publicada",
                          "respuesta": "texto", "citas": "[]", "senales": "[]"})
        assert estado.leer_consulta("abc")["respuesta"] == "texto"

    def test_las_metricas_son_agregados(self, estado):
        base = {"recibida_en": ahora().isoformat(), "consulta": "x", "admitida": 1,
                "citas": "[]", "senales": "[]", "duracion_ms": 1000}
        estado.registrar({**base, "id": "1", "desenlace": cfg.PUBLICADA})
        estado.registrar({**base, "id": "2", "desenlace": cfg.SIN_BASE})
        metricas = estado.metricas()
        assert metricas["consultas"] == 2
        assert metricas["publicadas"] == 1 and metricas["sin_base"] == 1
        # Ningún identificador de persona: es lo que hace publicable este endpoint.
        assert "visitante" not in metricas and "ip" not in metricas
