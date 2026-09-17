"""La medida con la que se corta el corpus: tokens del modelo de embeddings."""

import app.nucleo.constantes as cfg
from app.rag.ingesta import tokens

SUMARIO = ("Es arbitraria la sentencia que omitio considerar la prueba decisiva ofrecida por la "
           "actora (Fallos: 330:3248). ")


class TestContarTokens:
    """El tokenizador con el que se mide el chunking."""

    def test_un_texto_vacio_tiene_cero_tokens(self):
        assert tokens.contar_tokens("") == 0

    def test_un_texto_mas_largo_tiene_mas_tokens(self):
        assert tokens.contar_tokens("una frase corta") < tokens.contar_tokens(SUMARIO * 3)

    def test_un_modelo_desconocido_cae_al_codificador_por_defecto(self):
        assert tokens.contar_tokens("texto de prueba", modelo="modelo-inexistente") > 0


class TestSplitter:
    """El splitter corta con la misma medida con que se cuentan los fragmentos."""

    def test_ningun_pedazo_supera_el_techo(self):
        texto = "\n\n".join(SUMARIO * 4 for _ in range(30))
        pedazos = tokens.crear_splitter().split_text(texto)
        assert len(pedazos) > 1
        assert all(tokens.contar_tokens(p) <= cfg.TAMANO_CHUNK_TOKENS for p in pedazos)

    def test_corta_primero_en_el_parrafo_doble(self):
        parrafos = [f"Sumario {i}. " + SUMARIO * 6 for i in range(12)]
        pedazos = tokens.crear_splitter().split_text("\n\n".join(parrafos))
        assert all(p.startswith("Sumario") for p in pedazos)
