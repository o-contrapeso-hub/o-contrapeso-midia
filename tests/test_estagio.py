"""Testes do repositório público de estágio (T044; D1, A-004, A-005, RF-20).

Só respostas FABRICADAS (tests/fixtures/github_estagio) e token FALSO: nenhuma chamada de
rede real. A cópia temporária é sempre apagada (inclusive quando o envio do arquivo falha ou
é cancelado no meio); a limpeza de cópias antigas só toca releases do publicador com mais
de 15 minutos e registra apenas a contagem.
"""

from __future__ import annotations

import json
import logging
import re
import traceback
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from transporte_gravado import TransporteGravado

from publicador.artes.estagio import (
    IDADE_MAXIMA,
    CopiaPublica,
    ErroEstagio,
    EstagioGitHub,
    EstagioPublico,
    ResultadoLimpeza,
    limpar_copias_antigas,
)
from publicador.core.redacao import SegredoStr, limpar_segredos_registrados

FIXTURES = Path(__file__).parent / "fixtures" / "github_estagio"
TOKEN_ESTAGIO = "tok_ficticio_ESTAGIO9876wxyz"
REPOSITORIO = "dono-ficticio/estagio-ficticio"
AGORA = datetime(2026, 11, 7, 21, 30, tzinfo=UTC)
SUFIXO = "0123456789abcdef"
TAG = f"publicador-tmp-20261107T213000Z-{SUFIXO}"
NOME = "0a1b2c3d4e5f6071-1"
ID_RELEASE = 910000001
ID_ASSET = 920000001
API = "https://api.github.com/repos/dono-ficticio/estagio-ficticio"
UPLOADS = "https://uploads.github.com/repos/dono-ficticio/estagio-ficticio"
URL_PUBLICA = (
    f"https://github.com/dono-ficticio/estagio-ficticio/releases/download/{TAG}/{NOME}.jpg"
)
IMAGEM = b"\xff\xd8\xff\xe0" + bytes(range(60))
CORPO_DE_ERRO_UNICO = "CORPO-DE-ERRO-QUE-NAO-PODE-IR-A-LOG"


@pytest.fixture(autouse=True)
def _limpar_segredos() -> Any:
    yield
    limpar_segredos_registrados()


def _f(nome: str) -> bytes:
    return (FIXTURES / nome).read_bytes()


RELEASE = (201, _f("release_criada.json"))
ASSET = (201, _f("asset_criado.json"))
APAGADO = (204, b"")


def _estagio(transporte: Any, **kw: Any) -> EstagioGitHub:
    kw.setdefault("gerar_sufixo", lambda: SUFIXO)
    return EstagioGitHub(REPOSITORIO, SegredoStr(TOKEN_ESTAGIO), transporte, lambda: AGORA, **kw)


def _copia() -> CopiaPublica:
    return CopiaPublica(url=URL_PUBLICA, tag=TAG, id_release=ID_RELEASE, id_asset=ID_ASSET)


def _apagou_release(transporte: TransporteGravado, id_release: int) -> bool:
    return any(
        c.metodo == "DELETE" and c.url == f"{API}/releases/{id_release}"
        for c in transporte.chamadas
    )


# --------------------------------------------------------------------------- construção


def test_implementa_o_protocolo_e_repr_sem_token() -> None:
    estagio = _estagio(TransporteGravado())
    assert isinstance(estagio, EstagioPublico)
    assert TOKEN_ESTAGIO not in repr(estagio) + str(estagio)
    assert "estagio-ficticio" in repr(estagio)


@pytest.mark.parametrize(
    "repositorio",
    ["", "sem-barra", "a/b/c", "../x", "dono/..", "dono/ repo", "/repo", "dono/", "dono/re po"],
)
def test_repositorio_invalido_e_recusado(repositorio: str) -> None:
    with pytest.raises(ValueError) as info:
        EstagioGitHub(repositorio, SegredoStr(TOKEN_ESTAGIO), TransporteGravado(), lambda: AGORA)
    assert TOKEN_ESTAGIO not in "".join(traceback.format_exception(info.value))


def test_token_precisa_ser_segredo() -> None:
    with pytest.raises(TypeError):
        EstagioGitHub(REPOSITORIO, TOKEN_ESTAGIO, TransporteGravado(), lambda: AGORA)  # type: ignore[arg-type]


def test_copia_nao_mostra_url_no_repr() -> None:
    copia = _copia()
    assert URL_PUBLICA not in repr(copia) + str(copia)
    assert copia.url == URL_PUBLICA


# --------------------------------------------------------------------------- publicar_copia


def test_publicar_copia_sequencia_exata() -> None:
    transporte = TransporteGravado(RELEASE, ASSET)
    copia = _estagio(transporte).publicar_copia(NOME, IMAGEM)

    assert copia == CopiaPublica(URL_PUBLICA, TAG, ID_RELEASE, ID_ASSET)
    criar, subir = transporte.chamadas
    assert (criar.metodo, criar.url) == ("POST", f"{API}/releases")
    corpo = json.loads(criar.corpo or b"")
    assert corpo["tag_name"] == TAG and corpo["name"] == TAG
    assert corpo["draft"] is False and corpo["prerelease"] is True
    assert corpo["make_latest"] == "false"
    assert set(corpo) == {"tag_name", "name", "body", "draft", "prerelease", "make_latest"}
    assert criar.cabecalhos["Authorization"] == f"Bearer {TOKEN_ESTAGIO}"
    assert (subir.metodo, subir.url) == (
        "POST",
        f"{UPLOADS}/releases/{ID_RELEASE}/assets?name={NOME}.jpg",
    )
    assert subir.corpo == IMAGEM
    assert subir.cabecalhos["Content-Type"] == "image/jpeg"
    assert subir.cabecalhos["Authorization"] == f"Bearer {TOKEN_ESTAGIO}"


def test_tag_tem_marcador_fechado_com_instante_e_sufixo_aleatorio() -> None:
    transporte = TransporteGravado()
    estagio = EstagioGitHub(REPOSITORIO, SegredoStr(TOKEN_ESTAGIO), transporte, lambda: AGORA)
    with pytest.raises(ErroEstagio):
        estagio.publicar_copia(NOME, IMAGEM)  # fila vazia: só queremos a tag enviada
    tag = json.loads(transporte.chamadas[0].corpo or b"")["tag_name"]
    assert re.fullmatch(r"publicador-tmp-20261107T213000Z-[0-9a-f]{16}", tag)


def test_corpo_da_release_nao_tem_conteudo_do_post() -> None:
    transporte = TransporteGravado(RELEASE, ASSET)
    _estagio(transporte).publicar_copia(NOME, IMAGEM)
    corpo = (transporte.chamadas[0].corpo or b"").decode()
    assert NOME not in corpo  # nem o nome do arquivo vai para a release


@pytest.mark.parametrize("nome", ["", "Maiuscula", "com espaco", "a/b", "../x", "x" * 65, "-a"])
def test_nome_unico_invalido_e_recusado_sem_rede(nome: str) -> None:
    transporte = TransporteGravado()
    with pytest.raises(ValueError):
        _estagio(transporte).publicar_copia(nome, IMAGEM)
    assert transporte.chamadas == []


def test_bytes_vazios_sao_recusados_sem_rede() -> None:
    transporte = TransporteGravado()
    with pytest.raises(ValueError):
        _estagio(transporte).publicar_copia(NOME, b"")
    assert transporte.chamadas == []


def test_relogio_sem_fuso_e_recusado_sem_rede() -> None:
    transporte = TransporteGravado()
    estagio = EstagioGitHub(
        REPOSITORIO, SegredoStr(TOKEN_ESTAGIO), transporte, lambda: datetime(2026, 11, 7)
    )
    with pytest.raises(ValueError):
        estagio.publicar_copia(NOME, IMAGEM)
    assert transporte.chamadas == []


@pytest.mark.parametrize(
    ("resposta", "transitorio"),
    [
        ((500, b"{}"), True),
        ((502, b""), True),
        ((429, b"{}"), True),
        (OSError("queda"), True),
        ((401, b"{}"), False),
        ((403, b"{}"), False),
        ((404, b"{}"), False),
        ((422, b"{}"), False),
        ((302, b""), False),
        ((201, b"nao-e-json"), False),
        ((201, b'{"id":"texto"}'), False),
        ((201, b'{"id":910000001,"tag_name":"outra-tag"}'), False),
    ],
)
def test_falha_ao_criar_release_nao_sobe_nada(resposta: Any, transitorio: bool) -> None:
    transporte = TransporteGravado(resposta)
    with pytest.raises(ErroEstagio) as info:
        _estagio(transporte).publicar_copia(NOME, IMAGEM)
    assert info.value.transitorio is transitorio
    assert transporte.metodos() == ["POST"]


def test_release_criada_mas_resposta_sem_id_e_permanente() -> None:
    transporte = TransporteGravado((201, b'{"tag_name":"%s"}' % TAG.encode()))
    with pytest.raises(ErroEstagio) as info:
        _estagio(transporte).publicar_copia(NOME, IMAGEM)
    assert info.value.transitorio is False


def _asset_com_url(url: str) -> tuple[int, bytes]:
    return (
        201,
        json.dumps({"id": ID_ASSET, "state": "uploaded", "browser_download_url": url}).encode(),
    )


@pytest.mark.parametrize(
    "falha",
    [
        (500, b"{}"),
        (422, b"{}"),
        OSError("queda no upload"),
        (201, b"{}"),
        (201, b"[]"),
        _asset_com_url(URL_PUBLICA.replace("https://", "http://")),
        _asset_com_url(URL_PUBLICA.replace("github.com", "evil.example")),
        _asset_com_url(URL_PUBLICA.replace(TAG, "outra-tag")),
        _asset_com_url(URL_PUBLICA + "?x=1"),
        _asset_com_url(URL_PUBLICA.replace("https://", "https://usuario@")),
        (
            201,
            json.dumps(
                {"id": ID_ASSET, "state": "starter", "browser_download_url": URL_PUBLICA}
            ).encode(),
        ),
    ],
)
def test_falha_no_upload_apaga_a_release_criada(falha: Any) -> None:
    transporte = TransporteGravado(RELEASE, falha, APAGADO, APAGADO)
    with pytest.raises(ErroEstagio):
        _estagio(transporte).publicar_copia(NOME, IMAGEM)
    assert _apagou_release(transporte, ID_RELEASE)
    assert transporte.chamadas[-1].url == f"{API}/git/refs/tags/{TAG}"
    assert transporte.restantes == 0


def test_url_devolvida_com_dono_em_outra_caixa_e_aceita() -> None:
    url = URL_PUBLICA.replace("dono-ficticio", "Dono-Ficticio")
    transporte = TransporteGravado(RELEASE, _asset_com_url(url))
    assert _estagio(transporte).publicar_copia(NOME, IMAGEM).url == url


def test_cancelamento_no_meio_do_upload_apaga_a_release_e_propaga() -> None:
    """Simula o cancelamento do job (KeyboardInterrupt/BaseException) durante o envio."""
    transporte = TransporteGravado(RELEASE, KeyboardInterrupt(), APAGADO, APAGADO)
    with pytest.raises(KeyboardInterrupt):
        _estagio(transporte).publicar_copia(NOME, IMAGEM)
    assert _apagou_release(transporte, ID_RELEASE)


def test_falha_ao_apagar_depois_de_upload_ruim_nao_mascara_o_erro() -> None:
    transporte = TransporteGravado(RELEASE, (500, b"{}"), (500, b"{}"), OSError("x"))
    with pytest.raises(ErroEstagio) as info:
        _estagio(transporte).publicar_copia(NOME, IMAGEM)
    assert info.value.transitorio is True


# --------------------------------------------------------------------------- apagar_copia


def test_apagar_copia_sequencia_exata() -> None:
    transporte = TransporteGravado(APAGADO, APAGADO, APAGADO)
    assert _estagio(transporte).apagar_copia(_copia()) is True
    assert [(c.metodo, c.url) for c in transporte.chamadas] == [
        ("DELETE", f"{API}/releases/assets/{ID_ASSET}"),
        ("DELETE", f"{API}/releases/{ID_RELEASE}"),
        ("DELETE", f"{API}/git/refs/tags/{TAG}"),
    ]
    assert all(
        c.cabecalhos["Authorization"] == f"Bearer {TOKEN_ESTAGIO}" for c in transporte.chamadas
    )


def test_apagar_copia_e_idempotente() -> None:
    transporte = TransporteGravado((404, b"{}"), (404, b"{}"), (422, b"{}"))
    assert _estagio(transporte).apagar_copia(_copia()) is True


def test_apagar_copia_sem_asset_so_apaga_release_e_tag() -> None:
    transporte = TransporteGravado(APAGADO, APAGADO)
    copia = CopiaPublica(url=URL_PUBLICA, tag=TAG, id_release=ID_RELEASE, id_asset=None)
    assert _estagio(transporte).apagar_copia(copia) is True
    assert transporte.metodos() == ["DELETE", "DELETE"]


@pytest.mark.parametrize(
    "respostas",
    [
        [(500, b"{}"), (500, b"{}"), APAGADO],
        [OSError("queda"), OSError("queda"), OSError("queda")],
        [APAGADO, (403, b"{}"), APAGADO],
    ],
)
def test_falha_ao_apagar_devolve_false_sem_levantar(respostas: list[Any]) -> None:
    transporte = TransporteGravado(*respostas)
    assert _estagio(transporte).apagar_copia(_copia()) is False
    assert transporte.restantes == 0  # tenta todas as etapas mesmo após uma falha


def test_tag_fora_do_formato_nunca_e_apagada() -> None:
    transporte = TransporteGravado(APAGADO, APAGADO)
    copia = CopiaPublica(url=URL_PUBLICA, tag="v1.0.0", id_release=ID_RELEASE, id_asset=ID_ASSET)
    assert _estagio(transporte).apagar_copia(copia) is False
    assert transporte.chamadas == []


# --------------------------------------------------------------------------- limpeza (A-005)


def _apagadas(transporte: TransporteGravado) -> list[str]:
    return [c.url for c in transporte.chamadas if c.metodo == "DELETE"]


def test_limpar_antigas_so_apaga_as_do_publicador_com_mais_de_15_minutos(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    transporte = TransporteGravado(
        (200, _f("releases_lista.json")), APAGADO, APAGADO, APAGADO, APAGADO, APAGADO
    )
    resultado = _estagio(transporte).limpar_antigas(AGORA)

    assert resultado == ResultadoLimpeza(apagadas=2, falhas=0, listagem_ok=True)
    listar = transporte.chamadas[0]
    assert (listar.metodo, listar.url) == ("GET", f"{API}/releases?per_page=100&page=1")
    assert _apagadas(transporte) == [
        f"{API}/releases/assets/940000001",
        f"{API}/releases/930000001",
        f"{API}/git/refs/tags/publicador-tmp-20261107T211000Z-aaaaaaaaaaaaaaaa",
        f"{API}/releases/930000007",
        f"{API}/git/refs/tags/publicador-tmp-20250101T000000Z-eeeeeeeeeeeeeeee",
    ]
    for intocada in ("930000002", "930000003", "930000004", "930000005", "930000006"):
        assert not any(intocada in url for url in _apagadas(transporte))
    # registro: só contagens, nunca nomes/tags/ids
    assert "2" in caplog.text
    for proibido in ("publicador-tmp-2026", "930000001", "x-1.jpg", "aaaaaaaa"):
        assert proibido not in caplog.text


def test_limpar_antigas_com_idade_configuravel() -> None:
    transporte = TransporteGravado((200, _f("releases_lista.json")), *([APAGADO] * 20))
    resultado = _estagio(transporte).limpar_antigas(AGORA, timedelta(minutes=5))
    assert resultado.apagadas == 4  # 20 min, 10 min, 15 min e a de 2025


def test_limpar_antigas_padrao_e_15_minutos() -> None:
    assert IDADE_MAXIMA == timedelta(minutes=15)


def test_limpar_antigas_pagina() -> None:
    alheias = [
        {"id": 1000 + i, "tag_name": f"v{i}", "name": f"v{i}", "assets": []} for i in range(100)
    ]
    antiga = [
        {
            "id": 930000009,
            "tag_name": "publicador-tmp-20261107T200000Z-9999999999999999",
            "name": "publicador-tmp-20261107T200000Z-9999999999999999",
            "assets": [],
        }
    ]
    transporte = TransporteGravado(
        (200, json.dumps(alheias).encode()), (200, json.dumps(antiga).encode()), APAGADO, APAGADO
    )
    resultado = _estagio(transporte).limpar_antigas(AGORA)
    assert resultado.apagadas == 1
    assert transporte.chamadas[1].url == f"{API}/releases?per_page=100&page=2"


def test_limpar_antigas_limita_paginas() -> None:
    cheia = json.dumps(
        [{"id": 1000 + i, "tag_name": f"v{i}", "name": f"v{i}", "assets": []} for i in range(100)]
    ).encode()
    transporte = TransporteGravado(*([(200, cheia)] * 3))
    resultado = _estagio(transporte, max_paginas=3).limpar_antigas(AGORA)
    assert resultado.apagadas == 0 and transporte.restantes == 0


def test_limpar_antigas_conta_falhas_sem_levantar() -> None:
    transporte = TransporteGravado(
        (200, _f("releases_lista.json")),
        APAGADO,
        (500, b"{}"),
        APAGADO,  # primeira: release não apagada
        APAGADO,
        APAGADO,
    )
    resultado = _estagio(transporte).limpar_antigas(AGORA)
    assert resultado == ResultadoLimpeza(apagadas=1, falhas=1, listagem_ok=True)


@pytest.mark.parametrize(
    "resposta", [(500, b"{}"), (401, b"{}"), OSError("queda"), (200, b"{}"), (200, b"nao-json")]
)
def test_limpar_antigas_com_listagem_falha_nao_apaga_nada(resposta: Any) -> None:
    transporte = TransporteGravado(resposta)
    resultado = _estagio(transporte).limpar_antigas(AGORA)
    assert resultado == ResultadoLimpeza(apagadas=0, falhas=0, listagem_ok=False)
    assert transporte.metodos() == ["GET"]


def test_limpar_antigas_exige_instante_com_fuso() -> None:
    transporte = TransporteGravado()
    with pytest.raises(ValueError):
        _estagio(transporte).limpar_antigas(datetime(2026, 11, 7, 21, 30))
    assert transporte.chamadas == []


def test_limpar_antigas_nunca_apaga_marcador_no_futuro() -> None:
    futura = [
        {
            "id": 930000010,
            "tag_name": "publicador-tmp-20261108T000000Z-1111111111111111",
            "name": "publicador-tmp-20261108T000000Z-1111111111111111",
            "assets": [],
        }
    ]
    transporte = TransporteGravado((200, json.dumps(futura).encode()))
    assert _estagio(transporte).limpar_antigas(AGORA).apagadas == 0


def test_limpar_antigas_ignora_marcador_com_data_invalida() -> None:
    invalida = [
        {
            "id": 930000011,
            "tag_name": "publicador-tmp-20261399T999999Z-1111111111111111",
            "name": "publicador-tmp-20261399T999999Z-1111111111111111",
            "assets": [],
        }
    ]
    transporte = TransporteGravado((200, json.dumps(invalida).encode()))
    assert _estagio(transporte).limpar_antigas(AGORA).apagadas == 0


class _EstagioQuebrado:
    def __init__(self, erro: BaseException) -> None:
        self.erro = erro

    def publicar_copia(self, nome_unico: str, dados: bytes) -> CopiaPublica:
        raise AssertionError("não deveria publicar")

    def apagar_copia(self, copia: CopiaPublica) -> bool:
        raise AssertionError("não deveria apagar")

    def limpar_antigas(
        self, agora: datetime, idade_max: timedelta = IDADE_MAXIMA
    ) -> ResultadoLimpeza:
        raise self.erro


def test_limpar_copias_antigas_registra_a_contagem(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    transporte = TransporteGravado((200, _f("releases_lista.json")), *([APAGADO] * 5))
    resultado = limpar_copias_antigas(_estagio(transporte), AGORA)
    assert resultado is not None and resultado.apagadas == 2
    assert re.search(r"2 cópia\(s\) temporária\(s\) antiga\(s\) apagada\(s\)", caplog.text)


def test_limpar_copias_antigas_nunca_levanta(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    erro = RuntimeError(f"falha {TOKEN_ESTAGIO} {CORPO_DE_ERRO_UNICO}")
    assert limpar_copias_antigas(_EstagioQuebrado(erro), AGORA) is None
    assert TOKEN_ESTAGIO not in caplog.text and CORPO_DE_ERRO_UNICO not in caplog.text


def test_limpar_copias_antigas_propaga_cancelamento() -> None:
    with pytest.raises(KeyboardInterrupt):
        limpar_copias_antigas(_EstagioQuebrado(KeyboardInterrupt()), AGORA)


# --------------------------------------------------------------------------- I-1 e A-004


def _eco() -> bytes:
    return f'{{"message":"{TOKEN_ESTAGIO} {CORPO_DE_ERRO_UNICO}"}}'.encode()


@pytest.mark.parametrize(
    "respostas",
    [
        [(401, _eco())],
        [(500, _eco())],
        [OSError(f"Authorization: Bearer {TOKEN_ESTAGIO} {CORPO_DE_ERRO_UNICO}")],
        [RELEASE, (422, _eco()), (500, _eco()), (500, _eco())],
        [RELEASE, OSError(f"x {TOKEN_ESTAGIO}"), OSError(f"y {TOKEN_ESTAGIO}"), APAGADO],
    ],
)
def test_token_e_corpo_nunca_vazam_em_erro(
    respostas: list[Any], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    estagio = _estagio(TransporteGravado(*respostas))
    with pytest.raises(ErroEstagio) as info:
        estagio.publicar_copia(NOME, IMAGEM)
    texto = "\n".join(
        [repr(estagio), str(info.value), repr(info.value), caplog.text]
        + traceback.format_exception(info.value)
    )
    assert TOKEN_ESTAGIO not in texto and CORPO_DE_ERRO_UNICO not in texto
    assert info.value.__cause__ is None


def test_eco_do_token_no_sucesso_nao_vaza(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    release = json.loads(_f("release_criada.json"))
    release["body"] = TOKEN_ESTAGIO
    asset = json.loads(_f("asset_criado.json"))
    asset["label"] = TOKEN_ESTAGIO
    transporte = TransporteGravado(
        (201, json.dumps(release).encode()), (201, json.dumps(asset).encode())
    )
    copia = _estagio(transporte).publicar_copia(NOME, IMAGEM)
    assert TOKEN_ESTAGIO not in repr(copia) + str(copia) + copia.url + caplog.text


def test_token_so_vai_a_hosts_do_github() -> None:
    transporte = TransporteGravado(
        RELEASE,
        ASSET,
        APAGADO,
        APAGADO,
        APAGADO,
        (200, _f("releases_lista.json")),
        *([APAGADO] * 5),
    )
    estagio = _estagio(transporte)
    copia = estagio.publicar_copia(NOME, IMAGEM)
    estagio.apagar_copia(copia)
    estagio.limpar_antigas(AGORA)
    for chamada in transporte.chamadas:
        assert chamada.url.startswith((API + "/", UPLOADS + "/"))
        assert chamada.cabecalhos["Authorization"] == f"Bearer {TOKEN_ESTAGIO}"
        assert TOKEN_ESTAGIO not in chamada.url
        assert TOKEN_ESTAGIO.encode() not in (chamada.corpo or b"")
