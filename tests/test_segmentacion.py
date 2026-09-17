"""De un PDF extraído a los fragmentos que entran al índice.

Las subsecciones son la decisión de diseño que se prueba acá. Un suplemento de 862 páginas son
~1.500 fragmentos: con una sola subsección, la herramienta que lista los fallos de una
subsección devolvería miles de citas y dejaría de servir.
"""

import re

import pytest

import app.nucleo.constantes as cfg
from app.rag.ingesta import segmentacion
from app.rag.ingesta.pdf import Documento, Pagina

FICHA = {"origen": "suplemento-1", "tipo": "suplemento", "titulo": "Derecho a la Salud",
         "url": "https://sj.csjn.gov.ar/homeSJ/suplementos/suplemento/1/documento"}


def documento(paginas: int, titulos_por_pagina=None, outline=(), texto=None) -> Documento:
    """Un documento extraído, armado a mano."""
    return Documento(
        origen="suplemento-1",
        titulo="Derecho a la Salud",
        paginas=[
            Pagina(numero=n,
                   texto=(texto or f"Contenido de la pagina {n}. ") * 40,
                   citas_urls={},
                   titulos=(titulos_por_pagina or {}).get(n, []))
            for n in range(1, paginas + 1)
        ],
        outline=list(outline),
    )


class TestCascadaDeSubsecciones:
    """Outline, después tipografía, después bloques de páginas."""

    def test_el_outline_manda_cuando_es_de_grano_fino(self):
        doc = documento(20, outline=[(1, "1. Concepto", 1), (1, "2. Alcance", 11)])
        tramos, metodo = segmentacion.subsecciones(doc)
        assert metodo == "outline"
        assert [t.nombre for t in tramos] == ["1. Concepto", "2. Alcance"]
        assert (tramos[0].desde, tramos[0].hasta) == (1, 10)

    def test_un_outline_demasiado_grueso_se_descarta(self):
        # Una entrada que abarca 60 páginas no sirve para agrupar citas.
        doc = documento(60, outline=[(1, "Todo el suplemento", 1)])
        _, metodo = segmentacion.subsecciones(doc)
        assert metodo != "outline"

    def test_sin_outline_manda_la_tipografia(self):
        doc = documento(9, titulos_por_pagina={
            1: ["1. Concepto"], 4: ["2. Alcance"], 7: ["3. Excepciones"]})
        tramos, metodo = segmentacion.subsecciones(doc)
        assert metodo == "titulos"
        assert [t.nombre for t in tramos] == ["1. Concepto", "2. Alcance", "3. Excepciones"]

    def test_con_pocos_titulos_cae_a_bloques(self):
        doc = documento(30, titulos_por_pagina={1: ["Unico titulo"]})
        tramos, metodo = segmentacion.subsecciones(doc)
        assert metodo == "bloques"
        assert all(t.hasta - t.desde + 1 <= cfg.PAGINAS_POR_BLOQUE for t in tramos)

    def test_los_bloques_cubren_el_documento_entero(self):
        doc = documento(25)
        tramos, _ = segmentacion.subsecciones(doc)
        assert tramos[0].desde == 1 and tramos[-1].hasta == 25
        # Sin huecos ni solapamientos: cada página cae en un tramo y en uno solo.
        cubiertas = [n for t in tramos for n in range(t.desde, t.hasta + 1)]
        assert cubiertas == list(range(1, 26))


class TestNombreDeSubseccion:
    """El prefijo del documento es lo que mantiene los nombres únicos."""

    def test_lleva_el_titulo_del_documento_adelante(self):
        doc = documento(9, titulos_por_pagina={
            1: ["1. Concepto"], 4: ["2. Alcance"], 7: ["3. Cierre"]})
        fragmentos, _ = segmentacion.fragmentar(doc, FICHA)
        assert all(f["subseccion"].startswith("Derecho a la Salud" + cfg.SEPARADOR_SUBSECCION)
                   for f in fragmentos)

    def test_dos_documentos_con_el_mismo_titulo_interno_no_colisionan(self):
        # Con ~800 subsecciones, los "Introducción" de distintos suplementos colisionarían.
        doc = documento(9, titulos_por_pagina={1: ["Introduccion"], 4: ["A"], 7: ["B"]})
        uno, _ = segmentacion.fragmentar(doc, FICHA)
        otro, _ = segmentacion.fragmentar(doc, {**FICHA, "titulo": "Migraciones"})
        assert {f["subseccion"] for f in uno}.isdisjoint({f["subseccion"] for f in otro})

    def test_el_nombre_entra_en_el_campo_del_indice(self):
        largo = "T" * 300
        doc = documento(9, titulos_por_pagina={1: [largo], 4: ["A"], 7: ["B"]})
        fragmentos, _ = segmentacion.fragmentar(doc, {**FICHA, "titulo": largo})
        assert all(len(f["subseccion"]) <= 200 for f in fragmentos)


class TestFiltroDeCalidad:
    """Lo que no puede sostener doctrina queda fuera del índice.

    El ruido no solo no aporta: `bm25()` normaliza por longitud, así que un fragmento de tres
    caracteres que matchea un término se lleva un puntaje enorme y desplaza a la doctrina.
    """

    @pytest.mark.parametrize("texto", [
        "720",                                  # un número de página suelto
        "Índice",                               # un encabezado sin cuerpo
        "3) Buena fe",                          # un título de la tabla de contenidos
        # Bytes de una fuente sin mapa a Unicode: es como sale del PDF una nota entera.
        "".join(map(chr, (0x92, 0xAF, 0x4D3, 10, 9, 1))) * 30,
        "1 l . r , r S,*2 / y c S C C J , y ) <-> C ty & .y w j /y a y a , s t > >.;./",  # OCR
    ])
    def test_el_ruido_no_entra(self, texto):
        assert segmentacion.sirve(texto) is False

    @pytest.mark.parametrize("texto", [
        "Es arbitraria la sentencia que rechazo la accion de amparo interpuesta a fin de "
        "obtener la incorporacion de la actora como afiliada al Instituto de Obra Social.",
        # Las listas de citas son densas en números y son justamente lo que este sistema
        # quiere: rondan el 60% de letras, muy por encima del corte.
        "apelacion del art. 14 de la ley 48 (Fallos: 265:300; 273:289; 281:306; 304:154; "
        "338:1335; 342:1155), cabe recordar que la doctrina se aplica al caso.",
    ])
    def test_la_doctrina_y_las_listas_de_citas_entran(self, texto):
        assert segmentacion.sirve(texto) is True

    def test_los_umbrales_son_los_configurados(self):
        assert segmentacion.sirve("a" * (cfg.LARGO_MINIMO_FRAGMENTO - 1)) is False
        assert segmentacion.sirve("a" * cfg.LARGO_MINIMO_FRAGMENTO) is True


class TestFragmentos:
    """La metadata que el índice consume."""

    @pytest.fixture
    def fragmentos(self):
        doc = Documento(
            origen="suplemento-1", titulo="Derecho a la Salud",
            paginas=[
                Pagina(numero=1, texto="La Corte sostuvo la cobertura (Fallos: 343:2211). " * 20,
                       citas_urls={"343:2211": "https://sjconsulta.csjn.gov.ar/uno"},
                       titulos=["1. Cobertura"]),
                Pagina(numero=2, texto="Otro tema sin citas. " * 20, citas_urls={},
                       titulos=[]),
                Pagina(numero=3, texto="El amparo procede (Fallos: 306:1892). " * 20,
                       citas_urls={}, titulos=["2. Amparo"]),
                Pagina(numero=4, texto="Cierre del capitulo. " * 20, citas_urls={},
                       titulos=["3. Cierre"]),
            ],
        )
        return segmentacion.fragmentar(doc, FICHA)[0]

    def test_cada_fragmento_lleva_su_id_estable(self, fragmentos):
        # Ids estables: reindexar es un upsert y no acumula copias.
        assert fragmentos[0]["id"] == "suplemento-1#0000"
        assert len({f["id"] for f in fragmentos}) == len(fragmentos)

    def test_la_metadata_dice_de_donde_salio(self, fragmentos):
        f = fragmentos[0]
        assert f["origen"] == "suplemento-1" and f["fuente"] == "suplemento"
        assert f["seccion"] == "Derecho a la Salud" and f["pagina"] == 1
        assert f["url_documento"].endswith("/documento")

    def test_sin_indice_impreso_no_hay_encabezado(self, fragmentos):
        # La cadena de títulos sale de la numeración del índice impreso; los otros métodos
        # dan un título suelto o un rango de páginas, y el vector se calcula sobre el texto.
        assert all(f["encabezado"] == "" for f in fragmentos)

    def test_solo_lleva_las_citas_que_ese_fragmento_escribe(self, fragmentos):
        # Es lo que hace que la procedencia de una cita sea la subsección y no el documento.
        con_cita = [f for f in fragmentos if "343:2211" in f["citas_urls"]]
        assert all("343:2211" in f["texto"] for f in con_cita)

    def test_la_cita_enlazada_trae_su_url(self, fragmentos):
        f = next(f for f in fragmentos if "343:2211" in f["citas_urls"])
        assert f["citas_urls"]["343:2211"].endswith("/uno")

    def test_una_cita_sin_enlazar_entra_con_url_vacia(self, fragmentos):
        # `completar_links_faltantes` le arma después la oficial, por código.
        f = next(f for f in fragmentos if "306:1892" in f["citas_urls"])
        assert f["citas_urls"]["306:1892"] == ""

    def test_ningun_fragmento_supera_el_techo_de_tokens(self, fragmentos):
        assert all(f["tokens"] <= cfg.TAMANO_CHUNK_TOKENS for f in fragmentos)

    def test_una_pagina_vacia_no_produce_fragmentos(self):
        doc = Documento(origen="suplemento-1", titulo="X",
                        paginas=[Pagina(numero=n, texto="", citas_urls={}, titulos=[])
                                 for n in range(1, 12)])
        fragmentos, _ = segmentacion.fragmentar(doc, FICHA)
        assert fragmentos == []

    def test_cada_fragmento_dice_la_pagina_donde_empieza(self):
        # Un tramo de diez páginas corta en varios fragmentos, y todos decían la primera.
        fragmentos, _ = segmentacion.fragmentar(documento(10), FICHA)
        assert len({f["pagina"] for f in fragmentos}) > 1
        assert all(f["texto"].startswith(f"Contenido de la pagina {f['pagina']}.")
                   for f in fragmentos)

    def test_la_pagina_es_la_del_comienzo_aunque_el_fragmento_arrastre_solapamiento(self):
        # Con párrafos cortos, el splitter repite el final del fragmento anterior al comienzo
        # del siguiente. Su `start_index` resta ese solapamiento en tokens a una posición en
        # caracteres, y dejaba el fragmento en la página donde empezaba el anterior.
        doc = Documento(origen="suplemento-1", titulo="X", paginas=[
            Pagina(numero=n, texto="\n\n".join(
                f"Parrafo {i} de la pagina {n} con algo de texto." for i in range(20)))
            for n in range(1, 7)])
        fragmentos, _ = segmentacion.fragmentar(doc, FICHA)
        assert len(fragmentos) > 2
        assert all(f"de la pagina {f['pagina']} " in f["texto"].split("\n")[0]
                   for f in fragmentos)


def sumarios_con_citas(paginas: int = 4, por_pagina: int = 5) -> Documento:
    """Sumarios de largos variados, cada uno seguido de su renglón de citas.

    El último sumario de cada página deja sus citas en la página siguiente, como hace el salto
    de página en los PDF de la Secretaría.
    """
    largos = [2, 5, 9, 13, 3, 7, 11]
    parrafos: dict[int, list[str]] = {n: [] for n in range(1, paginas + 1)}
    k = 0
    for n in range(1, paginas + 1):
        for j in range(por_pagina):
            frase = f"el recurso {k} no rebate los fundamentos del fallo apelado. "
            parrafos[n].append(f"Sumario {k} de la pagina {n}: " + frase * largos[k % len(largos)])
            salta = j == por_pagina - 1 and n < paginas
            parrafos[n + 1 if salta else n].append(f"Fallos: 3{k:02d}:1{k:02d}")
            k += 1
    return Documento(origen="suplemento-1", titulo="Derecho a la Salud", paginas=[
        Pagina(numero=n, texto="\n\n".join(parrafos[n])) for n in range(1, paginas + 1)])


class TestSumariosYSusCitas:
    """Un sumario y el renglón de sus citas van en el mismo fragmento.

    Entre los dos hay una línea en blanco, y el splitter cortaba ahí: el fragmento siguiente
    empezaba con citas que no eran de su texto.
    """

    def test_cada_cita_queda_con_su_sumario(self):
        fragmentos, _ = segmentacion.fragmentar(sumarios_con_citas(), FICHA)
        assert len(fragmentos) > 1
        for k in range(20):
            con_cita = [f for f in fragmentos if f"3{k:02d}:1{k:02d}" in f["citas_urls"]]
            assert con_cita, f"la cita del sumario {k} no llegó a ningún fragmento"
            assert all(f"Sumario {k} " in f["texto"] for f in con_cita)

    def test_ningun_fragmento_empieza_con_citas(self):
        fragmentos, _ = segmentacion.fragmentar(sumarios_con_citas(), FICHA)
        assert all(f["texto"].startswith("Sumario") for f in fragmentos)

    def test_sin_pegarlas_el_splitter_las_separa(self, monkeypatch):
        # El control de los dos tests de arriba: este documento sí hace cortar entre un
        # sumario y sus citas cuando nada las pega.
        monkeypatch.setattr(segmentacion, "PATRON_PARRAFO_PEGADO", re.compile(r"(?!)"))
        fragmentos, _ = segmentacion.fragmentar(sumarios_con_citas(), FICHA)
        assert any(f["texto"].startswith("Fallos:") for f in fragmentos)

    def test_la_pagina_es_la_del_comienzo_aunque_se_haya_pegado(self):
        # Pegar quita un salto por párrafo, y las marcas de página se corren con él.
        fragmentos, _ = segmentacion.fragmentar(sumarios_con_citas(), FICHA)
        assert all(f"de la pagina {f['pagina']}:" in f["texto"].split("\n")[0]
                   for f in fragmentos)

    @pytest.mark.parametrize("propio", [
        "Fallos: 330:3248; Fallos: 316:1189",
        "FALLO A. 1430. XLIII. REX; Fallos: 312:2151",
        "-Del dictamen de la Procuración General al que la Corte remite-",
        "(Disidencia de los jueces Maqueda y Zaffaroni)",
        "de la Nación, conforme al art. 14 de la ley 48.",
    ])
    def test_lo_que_pertenece_al_sumario_se_pega(self, propio):
        texto = ("Es arbitraria la sentencia que omitio considerar la prueba decisiva ofrecida "
                 f"por la actora.\n\n{propio}\n\nOtro sumario sobre la misma cuestion federal.")
        doc = Documento(origen="suplemento-1", titulo="X", paginas=[Pagina(numero=1, texto=texto)])
        fragmento = segmentacion.fragmentar(doc, FICHA)[0][0]
        assert f"por la actora.\n{propio}\n\nOtro sumario" in fragmento["texto"]
