"""Testes da redacao de credenciais (publicador.core.redacao) - regra I-1.

Cada teste injeta um valor FICTICIO e confere que ele NAO aparece na saida.
Nenhum valor aqui e credencial real.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import random
import string
import sys
import traceback
from collections.abc import Callable, Iterator
from urllib.parse import quote, quote_plus

import pytest

from publicador.core.redacao import (
    FiltroRedacao,
    FormatadorSeguro,
    SegredoStr,
    configurar_logging,
    instalar_excepthook_seguro,
    limpar_segredos_registrados,
    obter_logger,
    redigir,
    redigir_estrutura,
    registrar_segredo,
)

TOKEN = "TOKEN_FALSO_abc123XYZ789"
SEGREDO = "segredo-falso-9f8e7d"
MASCARA = "***"


@pytest.fixture(autouse=True)
def _registro_limpo() -> Iterator[None]:
    limpar_segredos_registrados()
    yield
    limpar_segredos_registrados()


CriarLogger = Callable[..., tuple[logging.Logger, io.StringIO]]


@pytest.fixture
def logger_com_buffer() -> Iterator[CriarLogger]:
    """Cria loggers com um handler 'cru' (Formatter comum, sem filtro) adicionado
    DEPOIS da configuracao - prova que a protecao nao depende do handler."""
    criados: list[tuple[logging.Logger, logging.Handler]] = []

    def criar(nome: str = "teste", fmt: str = "%(message)s") -> tuple[logging.Logger, io.StringIO]:
        logger = obter_logger(nome)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        fluxo = io.StringIO()
        handler = logging.StreamHandler(fluxo)
        handler.setFormatter(logging.Formatter(fmt))
        logger.addHandler(handler)
        criados.append((logger, handler))
        return logger, fluxo

    yield criar
    for logger, handler in criados:
        logger.removeHandler(handler)
        logger.propagate = True


# --------------------------------------------------------------------------- registro


def test_registrar_ignora_valores_curtos_e_vazios() -> None:
    registrar_segredo("")
    registrar_segredo("abc")
    registrar_segredo("     ")
    assert redigir("abc e espacos") == "abc e espacos"


def test_segredo_registrado_some_em_qualquer_posicao() -> None:
    registrar_segredo(TOKEN)
    for texto in (TOKEN, f"x{TOKEN}y", f"{TOKEN}{TOKEN}", f"inicio {TOKEN}", f"{TOKEN} fim"):
        saida = redigir(texto)
        assert TOKEN not in saida
        assert MASCARA in saida


def test_segredo_mais_longo_primeiro() -> None:
    registrar_segredo("FALSO_curto1")
    registrar_segredo("FALSO_curto1_e_mais_longo")
    saida = redigir("valor FALSO_curto1_e_mais_longo aqui")
    assert saida == "valor *** aqui"


# --------------------------------------------------------------------------- logging


def test_log_com_percent_s_e_fstring(logger_com_buffer: CriarLogger) -> None:
    registrar_segredo(TOKEN)
    logger, fluxo = logger_com_buffer()
    logger.info("token carregado: %s", TOKEN)
    logger.info(f"token carregado via f-string: {TOKEN}")
    logger.info("access_token=%s", "naoRegistrado_FALSO_123456")
    saida = fluxo.getvalue()
    assert TOKEN not in saida
    assert "naoRegistrado_FALSO_123456" not in saida
    assert saida.count(MASCARA) >= 3


def test_log_com_extra(logger_com_buffer: CriarLogger) -> None:
    registrar_segredo(TOKEN)
    logger, fluxo = logger_com_buffer(fmt="%(message)s | %(conta)s | %(cabecalhos)s | %(lista)s")
    logger.info(
        "chamada",
        extra={
            "conta": TOKEN,
            "cabecalhos": {"Authorization": "Bearer FALSO_bearer_777xyz", "Accept": "json"},
            "lista": ["ok", {"refresh_token": "FALSO_refresh_555"}],
        },
    )
    saida = fluxo.getvalue()
    for vazamento in (TOKEN, "FALSO_bearer_777xyz", "FALSO_refresh_555"):
        assert vazamento not in saida
    assert "Accept" in saida and "json" in saida


def test_log_de_dict_e_objeto_com_repr(logger_com_buffer: CriarLogger) -> None:
    registrar_segredo(TOKEN)

    class Credencial:
        def __repr__(self) -> str:
            return f"Credencial(interno={TOKEN!r}, access_token='FALSO_at_000999')"

    logger, fluxo = logger_com_buffer()
    logger.info("cfg: %s", {"client_secret": "FALSO_cs_000111222", "cliente": "publico"})
    logger.info("obj: %r", Credencial())
    logger.info("obj: %s", Credencial())
    logger.info({"token": "FALSO_tk_msg_dict", "outro": 1})
    logger.info("%(a)s e %(b)s", {"a": TOKEN, "b": "normal"})
    saida = fluxo.getvalue()
    for vazamento in (TOKEN, "FALSO_cs_000111222", "FALSO_at_000999", "FALSO_tk_msg_dict"):
        assert vazamento not in saida
    assert "publico" in saida and "normal" in saida


def test_excecao_simples(logger_com_buffer: CriarLogger) -> None:
    registrar_segredo(TOKEN)
    logger, fluxo = logger_com_buffer()
    try:
        raise ValueError(f"falha ao chamar API com {TOKEN}")
    except ValueError as erro:
        logger.exception("erro na chamada")
        assert TOKEN not in redigir(erro)
        assert TOKEN not in redigir(traceback.format_exc())
    saida = fluxo.getvalue()
    assert TOKEN not in saida
    assert "ValueError" in saida and "Traceback" in saida


def test_excecao_encadeada_from_e_context(logger_com_buffer: CriarLogger) -> None:
    registrar_segredo(TOKEN)
    logger, fluxo = logger_com_buffer()
    # raise ... from ... (__cause__)
    try:
        try:
            raise ConnectionError(f"https://api.invalid/x?access_token={TOKEN}")
        except ConnectionError as interno:
            raise RuntimeError("falha ao publicar") from interno
    except RuntimeError:
        logger.exception("com __cause__")
    # excecao durante tratamento de outra (__context__), segredo NAO registrado
    try:
        try:
            raise KeyError("Authorization: Bearer FALSO_ctx_nao_registrado")
        except KeyError:
            raise RuntimeError("segunda falha")  # noqa: B904 - queremos __context__
    except RuntimeError:
        logger.error("com __context__", exc_info=True)
    saida = fluxo.getvalue()
    assert TOKEN not in saida
    assert "FALSO_ctx_nao_registrado" not in saida
    assert "direct cause" in saida
    assert "During handling" in saida


def test_excecao_context_com_segredo_registrado(logger_com_buffer: CriarLogger) -> None:
    registrar_segredo(SEGREDO)
    logger, fluxo = logger_com_buffer()
    try:
        try:
            raise KeyError(f"senha={SEGREDO}")
        except KeyError:
            raise RuntimeError("segunda falha")  # noqa: B904 - queremos __context__
    except RuntimeError:
        logger.exception("com __context__")
    saida = fluxo.getvalue()
    assert SEGREDO not in saida
    assert "During handling" in saida and "KeyError" in saida


def test_logger_exception_e_exc_info_tupla(logger_com_buffer: CriarLogger) -> None:
    registrar_segredo(TOKEN)
    logger, fluxo = logger_com_buffer()
    try:
        raise PermissionError(TOKEN)
    except PermissionError:
        info = sys.exc_info()
    logger.error("tupla", exc_info=info)
    logger.warning("instancia", exc_info=info[1])
    logger.info("pilha", stack_info=True)
    saida = fluxo.getvalue()
    assert TOKEN not in saida
    assert saida.count("PermissionError") >= 2


def test_filtro_devolve_true_e_limpa_exc_info() -> None:
    registrar_segredo(TOKEN)
    try:
        raise ValueError(TOKEN)
    except ValueError:
        info = sys.exc_info()
    registro = logging.LogRecord("x", logging.ERROR, __file__, 1, "msg %s", (TOKEN,), info)
    registro.__dict__["extra_texto"] = f"extra {TOKEN}"
    assert FiltroRedacao().filter(registro) is True
    assert registro.exc_info is None
    assert registro.exc_text is not None and TOKEN not in registro.exc_text
    assert TOKEN not in registro.getMessage()
    assert TOKEN not in str(registro.__dict__["extra_texto"])


def test_handler_adicionado_depois_nao_vaza() -> None:
    registrar_segredo(TOKEN)
    configurar_logging()
    # 1) handler cru em logger do projeto criado depois da configuracao
    logger = obter_logger("tardio")
    assert logger.name == "publicador.tardio"
    fluxo = io.StringIO()
    handler = logging.StreamHandler(fluxo)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    # 2) handler cru no logger raiz adicionado depois
    fluxo_raiz = io.StringIO()
    handler_raiz = logging.StreamHandler(fluxo_raiz)
    logging.getLogger().addHandler(handler_raiz)
    try:
        logger.warning("vazou? %s", TOKEN)
        try:
            raise OSError(f"Authorization: Bearer {TOKEN}")
        except OSError:
            logger.exception("falha")
        # 3) chamada direta a handler.handle (ex.: QueueListener)
        registro = logging.LogRecord("x", logging.INFO, __file__, 1, "direto %s", (TOKEN,), None)
        handler.handle(registro)
    finally:
        logger.removeHandler(handler)
        logging.getLogger().removeHandler(handler_raiz)
    assert TOKEN not in fluxo.getvalue()
    assert TOKEN not in fluxo_raiz.getvalue()
    assert fluxo.getvalue().count(MASCARA) >= 3


def test_configurar_logging_protege_handlers_raiz() -> None:
    configurar_logging()
    raiz = logging.getLogger()
    assert raiz.handlers
    for handler in raiz.handlers:
        assert any(isinstance(f, FiltroRedacao) for f in handler.filters)
        assert isinstance(handler.formatter, FormatadorSeguro)
    assert any(isinstance(f, FiltroRedacao) for f in raiz.filters)


def test_obter_logger_namespace() -> None:
    assert obter_logger("publicador").name == "publicador"
    assert obter_logger("publicador.core.x").name == "publicador.core.x"
    assert obter_logger("job").name == "publicador.job"


def test_formatador_seguro_e_segunda_barreira() -> None:
    registrar_segredo(TOKEN)
    formatador = FormatadorSeguro("%(message)s")
    registro = logging.LogRecord("x", logging.INFO, __file__, 1, f"cru {TOKEN}", None, None)
    assert TOKEN not in formatador.format(registro)


# --------------------------------------------------------------------------- padroes


def test_url_com_access_token_em_query() -> None:
    url = "GET https://graph.facebook.invalid/v19.0/me?access_token=FALSO_qs_ABC123&x=1"
    saida = redigir(url)
    assert "FALSO_qs_ABC123" not in saida
    assert "x=1" in saida and "graph.facebook.invalid" in saida
    saida2 = redigir("https://a.invalid/cb#access_token=FALSO_frag_999&state=s1")
    assert "FALSO_frag_999" not in saida2 and "state=s1" in saida2
    codificada = "redirect=https%3A%2F%2Fa.invalid%2F%3Ftoken%3DFALSO_enc_123%26y%3D2"
    assert "FALSO_enc_123" not in redigir(codificada)


def test_cabecalho_authorization_e_bearer() -> None:
    casos = {
        "Authorization: Bearer FALSO_jwt.aaa.bbb": "FALSO_jwt.aaa.bbb",
        "authorization: Basic dXN1YXJpb0ZBTFNPOnNlbmhhRkFMU0E=": "dXN1YXJpb0ZBTFNPOnNlbmhhRkFMU0E=",
        "curl -H 'Authorization: Bearer FALSO_curl_123' https://x.invalid": "FALSO_curl_123",
        "{'Authorization': 'Bearer FALSO_repr_456'}": "FALSO_repr_456",
        "Bearer FALSO_solto_789": "FALSO_solto_789",
        "Cookie: sessao=FALSO_cookie_321; outro=2": "FALSO_cookie_321",
        "Authorization=Basic RkFMU09fYmFzaWNfNjU0": "RkFMU09fYmFzaWNfNjU0",
        "X-Api-Key: FALSO_xapikey_111": "FALSO_xapikey_111",
    }
    for texto, vazamento in casos.items():
        saida = redigir(texto)
        assert vazamento not in saida, texto
        assert MASCARA in saida


def test_json_com_client_secret() -> None:
    corpo = json.dumps({"client_id": "id-publico", "client_secret": "FALSO_cs_json_42"})
    saida = redigir(corpo)
    assert "FALSO_cs_json_42" not in saida
    assert "id-publico" in saida
    assert json.loads(saida)["client_secret"] == MASCARA
    aninhado = json.dumps({"resposta": corpo})  # JSON dentro de string JSON
    assert "FALSO_cs_json_42" not in redigir(aninhado)
    for texto in (
        "{'api_key': 'FALSO_ak_1', 'Password': b'FALSO_pw_2'}",
        'Config(senha="FALSO_senha_3", signature=FALSO_sig_4)',
        "client_key: FALSO_ck_5\nsecret: FALSO_sec_6",
        "apikey=FALSO_ak_7 x-api-key=FALSO_ak_8 meta_access_token=FALSO_ak_9",
        '{"accessToken": "FALSO_camel_10", "key": "FALSO_key_11"}',
    ):
        saida = redigir(texto)
        assert "FALSO_" not in saida, (texto, saida)


def test_url_com_usuario_e_senha() -> None:
    saida = redigir("clone https://usuario:senhaFALSA123@exemplo.invalid/caminho ok")
    assert "senhaFALSA123" not in saida and "usuario" not in saida
    assert "exemplo.invalid/caminho ok" in saida
    saida2 = redigir("https://x-access-token:ghp_FALSO000aaa@github.invalid/o/r.git")
    assert "ghp_FALSO000aaa" not in saida2
    saida3 = redigir("https://ghp_FALSOsemsenha@github.invalid/o/r")
    assert "ghp_FALSOsemsenha" not in saida3


def test_token_em_base64_e_url_encoded() -> None:
    especial = "segredo/falso+9f8e7d=?&"
    registrar_segredo(especial)
    variantes = [
        quote(especial, safe=""),
        quote(especial, safe="").lower(),
        quote_plus(especial),
        quote(especial),
        base64.b64encode(especial.encode()).decode(),
        base64.urlsafe_b64encode(especial.encode()).decode(),
        base64.b64encode(especial.encode()).decode().rstrip("="),
        json.dumps(especial)[1:-1],
    ]
    for variante in variantes:
        saida = redigir(f"antes {variante} depois")
        assert variante not in saida, variante
        assert especial not in saida


def test_segredo_multilinha() -> None:
    chave = (
        "-----BEGIN CHAVE FALSA-----\nlinhaFALSA111aaa\nlinhaFALSA222bbb\n-----END CHAVE FALSA-----"
    )
    registrar_segredo(chave)
    assert "linhaFALSA" not in redigir(chave)
    assert "linhaFALSA222bbb" not in redigir("so a segunda: linhaFALSA222bbb.")
    assert "linhaFALSA" not in redigir(json.dumps({"k": chave}))
    assert "linhaFALSA" not in redigir(repr(chave))
    assert "linhaFALSA" not in redigir(chave.replace("\n", "\r\n"))


def test_texto_sem_segredo_permanece_intacto() -> None:
    # Decisao: nomes sensiveis so disparam redacao quando seguidos de '=' ou ':'
    # (ou dentro de cabecalho/aspas como chave). A palavra solta em frase normal fica.
    registrar_segredo(TOKEN)
    frases = [
        "O token expirou; renove o token amanha.",
        "A senha esta errada e a chave de acesso foi revogada.",
        "Publicado no Instagram as 10:30 (post 42).",
        "monkey=banana turkey=assado max_tokens=100 token_type=bearer",
        "secretaria=central tokenizer=bpe keyboard=abnt2",
        "https://exemplo.invalid/caminho?pagina=2&ordem=asc",
        "Erro 404: recurso nao encontrado",
        'json {"id": 7, "titulo": "fatos", "chave_publica_ok": false}',
        "",
    ]
    for frase in frases:
        assert redigir(frase) == frase


# --------------------------------------------------------------------------- SegredoStr


def test_segredo_str(logger_com_buffer: CriarLogger) -> None:
    segredo = SegredoStr("TOKEN_FALSO_segredostr_123")
    assert repr(segredo) == MASCARA
    assert str(segredo) == MASCARA
    assert f"{segredo}" == MASCARA
    assert f"{segredo!r}" == MASCARA
    assert "{}".format(segredo) == MASCARA  # noqa: UP032 - testa format()
    assert "%s" % segredo == MASCARA  # noqa: UP031 - testa %s
    assert repr([segredo]) == "[***]"
    assert segredo.revelar() == "TOKEN_FALSO_segredostr_123"
    assert type(segredo.revelar()) is str
    # registrou-se sozinho: mesmo quem "fura" o __str__ (concatenacao) e pego
    assert "TOKEN_FALSO_segredostr_123" not in redigir("x" + segredo.revelar())
    assert "TOKEN_FALSO_segredostr_123" not in redigir(json.dumps({"v": segredo}))
    logger, fluxo = logger_com_buffer()
    logger.info("valor %s %r", segredo, segredo, extra={"s": segredo})
    assert "TOKEN_FALSO_segredostr_123" not in fluxo.getvalue()
    import copy

    assert copy.deepcopy(segredo).revelar() == segredo.revelar()


# --------------------------------------------------------------------------- excepthook


def test_excepthook_seguro(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    registrar_segredo(TOKEN)
    instalar_excepthook_seguro()
    try:
        try:
            raise ValueError(f"client_secret=FALSO_hook_1 {TOKEN}")
        except ValueError as interno:
            raise RuntimeError(f"embrulho {TOKEN}") from interno
    except RuntimeError as erro:
        sys.excepthook(type(erro), erro, erro.__traceback__)
    capturado = capsys.readouterr()
    saida = capturado.err + capturado.out
    assert TOKEN not in saida and "FALSO_hook_1" not in saida
    assert "RuntimeError" in saida and "ValueError" in saida


# --------------------------------------------------------------------------- robustez


def test_redigir_nunca_lanca() -> None:
    registrar_segredo(TOKEN)

    class StrQuebrado:
        def __str__(self) -> str:
            raise RuntimeError(TOKEN)

    class TudoQuebrado:
        def __str__(self) -> str:
            raise RuntimeError(TOKEN)

        def __repr__(self) -> str:
            raise RuntimeError(TOKEN)

    recursiva: list[object] = []
    recursiva.append(recursiva)
    entradas: list[object] = [
        None,
        b"bytes " + TOKEN.encode(),
        bytearray(b"\xff\xfe"),
        StrQuebrado(),
        TudoQuebrado(),
        recursiva,
        12345,
        3.5,
        object(),
        "\x00\udcff",
        "x" * 200_000,
    ]
    for entrada in entradas:
        saida = redigir(entrada)
        assert isinstance(saida, str)
        assert TOKEN not in saida
    assert redigir(None) == "None"


def test_redigir_estrutura() -> None:
    registrar_segredo(TOKEN)
    original = {
        "access_token": "FALSO_est_1",
        "Client-Secret": "FALSO_est_2",
        "nivel": {"lista": [TOKEN, {"password": 123456}], "tupla": (TOKEN, "ok")},
        "conjunto": {TOKEN, "publico"},
        "url": "https://a.invalid/?token=FALSO_est_3",
        "numero": 7,
        "nada": None,
    }
    copia = redigir_estrutura(original)
    assert isinstance(copia, dict)
    texto = repr(copia)
    for vazamento in (TOKEN, "FALSO_est_1", "FALSO_est_2", "FALSO_est_3", "123456"):
        assert vazamento not in texto
    assert copia["numero"] == 7 and copia["nada"] is None
    assert isinstance(copia["nivel"]["tupla"], tuple)
    assert isinstance(copia["conjunto"], set) and "publico" in copia["conjunto"]
    assert original["access_token"] == "FALSO_est_1"  # original intacto
    ciclo: dict[str, object] = {}
    ciclo["eu"] = ciclo
    assert isinstance(redigir_estrutura(ciclo), dict)


# --------------------------------------------------------------------------- propriedade

_ALFABETO = string.printable + "áçé€ñ*%=&\"'\\{}[]:/+?#" + "TOKEN_FALSO" + "\u2028"
_SEGREDOS_FIXOS = [
    TOKEN,
    SEGREDO,
    "segredo/falso+9f8e7d=?&",
    "******",
    "a***b***c",
    "aaaaaa",
    "FALSO%20com%espaco",
    "falso\nmulti\nlinha00",
]


def _texto_aleatorio(gerador: random.Random, maximo: int = 40) -> str:
    return "".join(gerador.choice(_ALFABETO) for _ in range(gerador.randint(0, maximo)))


def test_propriedade_segredo_registrado_nunca_sobrevive_fuzz() -> None:
    gerador = random.Random(20261107)
    simbolos = string.ascii_letters + string.digits + "-_./+=*%~"
    for i in range(1500):
        if i < len(_SEGREDOS_FIXOS) * 10:
            segredo = _SEGREDOS_FIXOS[i % len(_SEGREDOS_FIXOS)]
        else:
            segredo = "".join(gerador.choice(simbolos) for _ in range(gerador.randint(6, 24)))
        limpar_segredos_registrados()
        registrar_segredo(segredo)
        texto = _texto_aleatorio(gerador) + segredo + _texto_aleatorio(gerador)
        assert segredo not in redigir(texto), (segredo, texto)


try:
    from hypothesis import given, settings
    from hypothesis import strategies as st

    TEM_HYPOTHESIS = True
except ImportError:  # pragma: no cover - depende do ambiente
    TEM_HYPOTHESIS = False

if TEM_HYPOTHESIS:

    @settings(max_examples=300, deadline=None)
    @given(
        antes=st.text(),
        depois=st.text(),
        segredo=st.text(min_size=6, max_size=40).filter(lambda s: s.strip() == s),
    )
    def test_propriedade_hypothesis(antes: str, depois: str, segredo: str) -> None:
        limpar_segredos_registrados()
        registrar_segredo(segredo)
        assert segredo not in redigir(antes + segredo + depois)

else:

    @pytest.mark.skip(reason="hypothesis nao instalado; coberto pelo teste de fuzz deterministico")
    def test_propriedade_hypothesis() -> None:
        """Placeholder: hypothesis indisponivel neste ambiente."""
