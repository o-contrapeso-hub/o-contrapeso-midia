"""Testes de contrato do Instagram: imagem única (feed/story) e carrossel via cópia pública
temporária no repositório de estágio (T044; D1, A-004, A-005, RF-10, RF-12, RF-14, RF-20).

Só respostas FABRICADAS e tokens FALSOS: nenhuma chamada de rede real, nenhum sleep real.
Ponto central: a cópia temporária é SEMPRE apagada (sucesso, falha em cada etapa, INCERTO,
exceção interna e cancelamento do job), e falha ao apagar não muda o resultado.
"""

from __future__ import annotations

import hashlib
import json
import logging
import socket
import struct
import traceback
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import parse_qs

import pytest
from transporte_gravado import Resposta, TransporteGravado

from publicador.adaptadores.base import PedidoDePublicacao, TipoDeResultado
from publicador.adaptadores.instagram import AdaptadorInstagram
from publicador.artes.armazem import ArmazemGitHub, ArmazemLocal
from publicador.artes.estagio import EstagioGitHub
from publicador.core.modelos import Estagio, PapelEstagio, Post
from publicador.core.redacao import SegredoStr, limpar_segredos_registrados

FIXTURES = Path(__file__).parent / "fixtures" / "instagram"
TOKEN = "tok_ficticio_ZXCV1234abcd"
TOKEN_ESTAGIO = "tok_ficticio_ESTAGIO9876wxyz"
TOKEN_ARTES = "tok_ficticio_ARTES5555qwer"
ID_CONTA = "17841400000000001"
EXPIRA_EM = datetime.fromtimestamp(1767744000, UTC)
AGORA = EXPIRA_EM - timedelta(days=10, hours=3)
CHAVE = "post-ficticio-0001|instagram|2026-11-07T18:30:00-03:00"
TEXTO_SECRETO = "Legenda ficticia ULTRA-SECRETA"
CORPO_DE_ERRO_UNICO = "CORPO-DE-ERRO-QUE-NAO-PODE-IR-A-LOG"
ID_CONTAINER = "17889000000000077"
ID_MIDIA = "17895000000000042"
BASE = "https://graph.facebook.com/v21.0"
REPOSITORIO = "dono-ficticio/estagio-ficticio"
API_ESTAGIO = "https://api.github.com/repos/dono-ficticio/estagio-ficticio"
NOME_BASE = hashlib.sha256(CHAVE.encode()).hexdigest()[:16]
INSTANTE = AGORA.strftime("%Y%m%dT%H%M%SZ")


@pytest.fixture(autouse=True)
def _limpar_segredos() -> Any:
    yield
    limpar_segredos_registrados()


def _f(nome: str) -> bytes:
    return (FIXTURES / nome).read_bytes()


def _jpeg(largura: int, altura: int, extra: bytes = b"") -> bytes:
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, altura, largura, 1) + b"\x01\x11\x00"
    return b"\xff\xd8\xff\xe0\x00\x04JF" + sof + extra + b"\xff\xd9"


def _arte(i: int) -> str:
    return f"serie-a/2026/11/arte-{i}.jpg"


@pytest.fixture
def raiz(tmp_path: Path) -> Path:
    base = tmp_path / "artes"
    for i in range(1, 12):
        destino = base / _arte(i)
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(_jpeg(1080, 1080, bytes([i])))
    story = base / "serie-a/2026/11/story.jpg"
    story.write_bytes(_jpeg(1080, 1920))
    return base


def _post(**mudancas: Any) -> Post:
    base: dict[str, Any] = {
        "id": "post-ficticio-0001",
        "titulo": "Titulo ficticio",
        "corpo": TEXTO_SECRETO,
        "estagio": Estagio("confirmado", PapelEstagio.AUTORIZADO),
        "plataformas": ("instagram",),
        "formato": "Feed",
    }
    base.update(mudancas)
    return Post(**base)


def _pedido(
    simular: bool = False, formato: str = "Feed", artes: tuple[str, ...] = (_arte(1),)
) -> PedidoDePublicacao:
    return PedidoDePublicacao(_post(formato=formato), "instagram", artes, CHAVE, simular=simular)


def _carrossel(n: int, simular: bool = False) -> PedidoDePublicacao:
    return _pedido(simular, "Carrossel", tuple(_arte(i) for i in range(1, n + 1)))


class _Sufixos:
    def __init__(self) -> None:
        self.n = 0

    def __call__(self) -> str:
        self.n += 1
        return f"{self.n:016x}"


def _tag(i: int) -> str:
    return f"publicador-tmp-{INSTANTE}-{i:016x}"


def _url(i: int, item: int | None = None) -> str:
    return (
        f"https://github.com/dono-ficticio/estagio-ficticio/releases/download/{_tag(i)}/"
        f"{NOME_BASE}-{item if item is not None else i}.jpg"
    )


def _id_release(i: int) -> int:
    return 910000000 + i


def _release(i: int) -> tuple[int, bytes]:
    tag = _tag(i)
    return (201, json.dumps({"id": _id_release(i), "tag_name": tag, "name": tag}).encode())


def _asset(i: int, item: int | None = None) -> tuple[int, bytes]:
    dados = {"id": 920000000 + i, "state": "uploaded", "browser_download_url": _url(i, item)}
    return (201, json.dumps(dados).encode())


def _copia(i: int) -> list[Resposta]:
    return [_release(i), _asset(i)]


APAGADO: Resposta = (204, b"")
APAGAR = [APAGADO, APAGADO, APAGADO]  # asset, release, tag
PRE: list[Resposta] = [(200, _f("debug_token_ativa.json")), (200, _f("limite_restante.json"))]
CONTAINER: Resposta = (200, _f("container_imagem.json"))
FINALIZADO: Resposta = (200, _f("status_finalizado.json"))
PUBLICADO_OK: Resposta = (200, _f("publish_ok.json"))


def _filho(i: int) -> str:
    return f"178890000000001{i:02d}"


def _adaptador(
    transporte: Any,
    raiz: Path,
    *,
    estagio: Any = "padrao",
    transporte_estagio: Any = None,
    armazem: Any = None,
    **kw: Any,
) -> AdaptadorInstagram:
    if estagio == "padrao":
        estagio = EstagioGitHub(
            REPOSITORIO,
            SegredoStr(TOKEN_ESTAGIO),
            transporte_estagio if transporte_estagio is not None else transporte,
            lambda: AGORA,
            gerar_sufixo=_Sufixos(),
        )
    return AdaptadorInstagram(
        SegredoStr(TOKEN),
        ID_CONTA,
        transporte,
        armazem if armazem is not None else ArmazemLocal(raiz),
        lambda: AGORA,
        dormir=lambda s: None,
        estagio=estagio,
        **kw,
    )


def _feliz() -> list[Resposta]:
    return [*PRE, *_copia(1), CONTAINER, FINALIZADO, PUBLICADO_OK, *APAGAR]


def _publicacoes(transporte: TransporteGravado) -> int:
    return sum(1 for c in transporte.chamadas if c.url.endswith("/media_publish"))


def _criadas(transporte: TransporteGravado) -> set[int]:
    """Releases criadas com sucesso (o fluxo só sobe o arquivo depois de a release existir)."""
    prefixo = "https://uploads.github.com/repos/dono-ficticio/estagio-ficticio/releases/"
    return {
        int(c.url[len(prefixo) :].split("/", 1)[0])
        for c in transporte.chamadas
        if c.metodo == "POST" and c.url.startswith(prefixo)
    }


def _apagadas(transporte: TransporteGravado) -> set[int]:
    prefixo = f"{API_ESTAGIO}/releases/"
    return {
        int(c.url[len(prefixo) :])
        for c in transporte.chamadas
        if c.metodo == "DELETE" and c.url.startswith(prefixo) and "/assets/" not in c.url
    }


def _form(chamada: Any) -> dict[str, list[str]]:
    return parse_qs((chamada.corpo or b"").decode())


# --------------------------------------------------------------------------- caminho feliz


def test_feed_imagem_unica_sequencia_exata(raiz: Path) -> None:
    transporte = TransporteGravado(*_feliz())
    r = _adaptador(transporte, raiz).publicar(_pedido())

    assert r.tipo is TipoDeResultado.SUCESSO and r.id_externo == ID_MIDIA
    assert [(c.metodo, c.url) for c in transporte.chamadas] == [
        ("GET", f"{BASE}/debug_token?input_token={TOKEN}"),
        ("GET", f"{BASE}/{ID_CONTA}/content_publishing_limit?fields=quota_usage,config"),
        ("POST", f"{API_ESTAGIO}/releases"),
        (
            "POST",
            "https://uploads.github.com/repos/dono-ficticio/estagio-ficticio/releases/"
            f"{_id_release(1)}/assets?name={NOME_BASE}-1.jpg",
        ),
        ("POST", f"{BASE}/{ID_CONTA}/media"),
        ("GET", f"{BASE}/{ID_CONTAINER}?fields=status_code"),
        ("POST", f"{BASE}/{ID_CONTA}/media_publish"),
        ("DELETE", f"{API_ESTAGIO}/releases/assets/920000001"),
        ("DELETE", f"{API_ESTAGIO}/releases/{_id_release(1)}"),
        ("DELETE", f"{API_ESTAGIO}/git/refs/tags/{_tag(1)}"),
    ]
    container = _form(transporte.chamadas[4])
    assert container == {"image_url": [_url(1)], "caption": [TEXTO_SECRETO]}
    assert _form(transporte.chamadas[6]) == {"creation_id": [ID_CONTAINER]}
    assert transporte.chamadas[3].corpo == (raiz / _arte(1)).read_bytes()
    assert transporte.restantes == 0


def test_story_em_imagem_usa_media_type_stories(raiz: Path) -> None:
    transporte = TransporteGravado(*_feliz())
    r = _adaptador(transporte, raiz).publicar(
        _pedido(formato="Stories", artes=("serie-a/2026/11/story.jpg",))
    )
    assert r.tipo is TipoDeResultado.SUCESSO
    assert _form(transporte.chamadas[4]) == {
        "image_url": [_url(1)],
        "media_type": ["STORIES"],
        "caption": [TEXTO_SECRETO],
    }


def test_sem_legenda_nao_envia_caption(raiz: Path) -> None:
    transporte = TransporteGravado(*_feliz())
    pedido = PedidoDePublicacao(_post(corpo="   "), "instagram", (_arte(1),), CHAVE, simular=False)
    assert _adaptador(transporte, raiz).publicar(pedido).sucesso
    assert "caption" not in _form(transporte.chamadas[4])


@pytest.mark.parametrize("n", [2, 3, 10])
@pytest.mark.parametrize("formato", ["Carrossel", "Feed"])
def test_carrossel_sequencia_exata(raiz: Path, n: int, formato: str) -> None:
    respostas: list[Resposta] = [*PRE]
    for i in range(1, n + 1):
        respostas += _copia(i)
    respostas += [(200, json.dumps({"id": _filho(i)}).encode()) for i in range(1, n + 1)]
    respostas += [CONTAINER, FINALIZADO, PUBLICADO_OK]
    respostas += APAGAR * n
    transporte = TransporteGravado(*respostas)
    pedido = _pedido(False, formato, tuple(_arte(i) for i in range(1, n + 1)))

    r = _adaptador(transporte, raiz).publicar(pedido)

    assert r.tipo is TipoDeResultado.SUCESSO and r.id_externo == ID_MIDIA
    chamadas = transporte.chamadas
    assert transporte.metodos() == (
        ["GET", "GET"]
        + ["POST", "POST"] * n
        + ["POST"] * n
        + ["POST", "GET", "POST"]
        + ["DELETE"] * 3 * n
    )
    filhos = chamadas[2 + 2 * n : 2 + 3 * n]
    for i, chamada in enumerate(filhos, start=1):
        assert chamada.url == f"{BASE}/{ID_CONTA}/media"
        assert _form(chamada) == {"image_url": [_url(i)], "is_carousel_item": ["true"]}
    pai = chamadas[2 + 3 * n]
    assert _form(pai) == {
        "media_type": ["CAROUSEL"],
        "children": [",".join(_filho(i) for i in range(1, n + 1))],
        "caption": [TEXTO_SECRETO],
    }
    assert chamadas[3 + 3 * n].url == f"{BASE}/{ID_CONTAINER}?fields=status_code"
    assert chamadas[4 + 3 * n].url.endswith("/media_publish")
    assert _criadas(transporte) == _apagadas(transporte) and len(_criadas(transporte)) == n
    assert transporte.restantes == 0


@pytest.mark.parametrize("n", [1, 11])
def test_carrossel_fora_de_2_a_10_nao_envia_nada(raiz: Path, n: int) -> None:
    transporte = TransporteGravado()
    r = _adaptador(transporte, raiz).publicar(_carrossel(n))
    assert r.tipo is TipoDeResultado.FALHA_PERMANENTE and r.motivo == "midia_invalida"
    assert transporte.chamadas == []


def test_feed_com_11_imagens_nao_envia_nada(raiz: Path) -> None:
    transporte = TransporteGravado()
    pedido = _pedido(False, "Feed", tuple(_arte(i) for i in range(1, 12)))
    assert _adaptador(transporte, raiz).publicar(pedido).motivo == "midia_invalida"
    assert transporte.chamadas == []


def test_validacao_local_falha_antes_de_qualquer_envio(raiz: Path) -> None:
    (raiz / "ruim.jpg").write_bytes(b"\x89PNG nao e jpeg")
    transporte = TransporteGravado()
    adaptador = _adaptador(transporte, raiz)
    for artes in (("ruim.jpg",), ("nao/existe.jpg",), ("serie-a/2026/11/story.jpg",)):
        r = adaptador.publicar(_pedido(artes=artes))
        assert r.tipo is TipoDeResultado.FALHA_PERMANENTE and r.motivo == "midia_invalida"
    longa = PedidoDePublicacao(_post(corpo="x" * 2201), "instagram", (_arte(1),), CHAVE, False)
    assert adaptador.publicar(longa).motivo == "legenda_longa"
    assert transporte.chamadas == []


# ------------------------------------------------------------------- sem estágio / simulação


@pytest.mark.parametrize("simular", [True, False])
@pytest.mark.parametrize(
    "pedido",
    [_pedido(), _carrossel(2), _pedido(formato="Stories", artes=("serie-a/2026/11/story.jpg",))],
)
def test_sem_estagio_configurado_nada_e_enviado(
    raiz: Path, simular: bool, pedido: PedidoDePublicacao
) -> None:
    transporte = TransporteGravado()
    adaptador = _adaptador(transporte, raiz, estagio=None)
    pedido = PedidoDePublicacao(pedido.post, "instagram", pedido.artes, CHAVE, simular=simular)
    r = adaptador.publicar(pedido)
    assert r.tipo is TipoDeResultado.FALHA_PERMANENTE
    assert r.motivo == "estagio_nao_configurado"
    assert r.simulado is simular
    assert transporte.chamadas == []


def test_simulacao_nao_chama_estagio_nem_instagram(
    raiz: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def proibido(*a: Any, **k: Any) -> NoReturn:
        raise AssertionError("rede proibida")

    monkeypatch.setattr(socket, "socket", proibido)
    monkeypatch.setattr(socket, "create_connection", proibido)
    monkeypatch.setattr(urllib.request, "urlopen", proibido)
    graph, estagio = TransporteGravado(), TransporteGravado()
    adaptador = _adaptador(graph, raiz, transporte_estagio=estagio)
    for pedido in (_pedido(True), _carrossel(3, simular=True)):
        r = adaptador.publicar(pedido)
        assert r.tipo is TipoDeResultado.SUCESSO and r.simulado
    assert adaptador.publicar(_carrossel(11, simular=True)).motivo == "midia_invalida"
    assert graph.chamadas == [] and estagio.chamadas == []
    # simulação não deixa andamento que bloqueie o real
    real = TransporteGravado(*_feliz())
    adaptador._transporte = real  # noqa: SLF001 - troca de transporte só para o teste
    adaptador._estagio._transporte = real  # type: ignore[union-attr]  # noqa: SLF001
    assert adaptador.publicar(_pedido()).tipo is TipoDeResultado.SUCESSO


@pytest.mark.parametrize(
    "pre",
    [
        [(200, _f("debug_token_invalido.json"))],
        [(200, _f("debug_token_expirado.json"))],
        [OSError("queda")],
        [PRE[0], (200, _f("limite_esgotado.json"))],
        [PRE[0], (500, b"")],
    ],
)
def test_conexao_ou_limite_ruim_nada_e_copiado(raiz: Path, pre: list[Resposta]) -> None:
    transporte = TransporteGravado(*pre)
    r = _adaptador(transporte, raiz).publicar(_pedido())
    assert r.tipo in (TipoDeResultado.FALHA_PERMANENTE, TipoDeResultado.FALHA_TRANSITORIA)
    assert all("github" not in c.url for c in transporte.chamadas)
    assert transporte.restantes == 0


# --------------------------------------------------------------------------- cópia sempre apagada


ERRO_400 = (400, _f("erro_parametro_100.json"))


@pytest.mark.parametrize(
    ("respostas", "tipo", "motivo"),
    [
        # falha ao criar a release: nada a apagar
        ([*PRE, (500, b"{}")], TipoDeResultado.FALHA_TRANSITORIA, "estagio_"),
        ([*PRE, (403, b"{}")], TipoDeResultado.FALHA_PERMANENTE, "estagio_"),
        # falha no upload: o próprio estágio apaga release e tag
        ([*PRE, _release(1), (500, b"{}"), APAGADO, APAGADO],
         TipoDeResultado.FALHA_TRANSITORIA, "estagio_"),
        ([*PRE, _release(1), OSError("x"), APAGADO, APAGADO],
         TipoDeResultado.FALHA_TRANSITORIA, "estagio_"),
        # falhas no Instagram antes do publish
        ([*PRE, *_copia(1), ERRO_400, *APAGAR], TipoDeResultado.FALHA_PERMANENTE, "container_"),
        ([*PRE, *_copia(1), OSError("x"), *APAGAR], TipoDeResultado.FALHA_TRANSITORIA,
         "container_rede"),
        ([*PRE, *_copia(1), (200, b"{}"), *APAGAR], TipoDeResultado.FALHA_PERMANENTE,
         "container_fora_do_contrato"),
        ([*PRE, *_copia(1), CONTAINER, (200, _f("status_erro.json")), *APAGAR],
         TipoDeResultado.FALHA_PERMANENTE, "container_erro"),
        ([*PRE, *_copia(1), CONTAINER, (500, b""), *APAGAR],
         TipoDeResultado.FALHA_TRANSITORIA, "status_"),
        ([*PRE, *_copia(1), CONTAINER, (200, _f("status_publicado.json")), *APAGAR],
         TipoDeResultado.INCERTO, "container_ja_publicado"),
        # dúvida depois de enviar o media_publish: INCERTO
        ([*PRE, *_copia(1), CONTAINER, FINALIZADO, OSError("x"), *APAGAR],
         TipoDeResultado.INCERTO, "publish_sem_resposta"),
        ([*PRE, *_copia(1), CONTAINER, FINALIZADO, (500, b""), *APAGAR],
         TipoDeResultado.INCERTO, "publish_http_500"),
        ([*PRE, *_copia(1), CONTAINER, FINALIZADO, ERRO_400, *APAGAR],
         TipoDeResultado.INCERTO, "publish_http_400"),
        ([*PRE, *_copia(1), CONTAINER, FINALIZADO, (200, b"{}"), *APAGAR],
         TipoDeResultado.INCERTO, "publish_resposta_invalida"),
        # sucesso
        ([*PRE, *_copia(1), CONTAINER, FINALIZADO, PUBLICADO_OK, *APAGAR],
         TipoDeResultado.SUCESSO, None),
    ],
)  # fmt: skip
def test_copia_sempre_apagada_imagem_unica(
    raiz: Path, respostas: list[Resposta], tipo: TipoDeResultado, motivo: str | None
) -> None:
    transporte = TransporteGravado(*respostas)
    r = _adaptador(transporte, raiz).publicar(_pedido())
    assert r.tipo is tipo
    if motivo is None:
        assert r.motivo is None
    else:
        assert r.motivo is not None and r.motivo.startswith(motivo)
    assert _criadas(transporte) == _apagadas(transporte)
    assert transporte.restantes == 0
    if tipo is not TipoDeResultado.INCERTO and tipo is not TipoDeResultado.SUCESSO:
        assert _publicacoes(transporte) == 0


@pytest.mark.parametrize(
    ("respostas", "tipo"),
    [
        # segunda cópia falha ao criar: a primeira é apagada
        ([*PRE, *_copia(1), (500, b"{}"), *APAGAR], TipoDeResultado.FALHA_TRANSITORIA),
        # segundo filho falha: as duas cópias são apagadas
        ([*PRE, *_copia(1), *_copia(2), (200, json.dumps({"id": _filho(1)}).encode()),
          ERRO_400, *APAGAR, *APAGAR], TipoDeResultado.FALHA_PERMANENTE),
        # container pai falha
        ([*PRE, *_copia(1), *_copia(2), (200, json.dumps({"id": _filho(1)}).encode()),
          (200, json.dumps({"id": _filho(2)}).encode()), (503, b""), *APAGAR, *APAGAR],
         TipoDeResultado.FALHA_TRANSITORIA),
        # publish sem resposta
        ([*PRE, *_copia(1), *_copia(2), (200, json.dumps({"id": _filho(1)}).encode()),
          (200, json.dumps({"id": _filho(2)}).encode()), CONTAINER, FINALIZADO,
          TimeoutError("lento"), *APAGAR, *APAGAR], TipoDeResultado.INCERTO),
    ],
)  # fmt: skip
def test_copia_sempre_apagada_carrossel(
    raiz: Path, respostas: list[Resposta], tipo: TipoDeResultado
) -> None:
    transporte = TransporteGravado(*respostas)
    r = _adaptador(transporte, raiz).publicar(_carrossel(2))
    assert r.tipo is tipo
    assert _criadas(transporte) == _apagadas(transporte) and _criadas(transporte)
    assert transporte.restantes == 0


def test_filho_do_carrossel_com_id_invalido_e_permanente(raiz: Path) -> None:
    transporte = TransporteGravado(
        *PRE, *_copia(1), *_copia(2), (200, b'{"id":"abc"}'), *APAGAR, *APAGAR
    )
    r = _adaptador(transporte, raiz).publicar(_carrossel(2))
    assert r.tipo is TipoDeResultado.FALHA_PERMANENTE
    assert r.motivo == "container_fora_do_contrato"
    assert _criadas(transporte) == _apagadas(transporte)


def test_excecao_interna_apaga_a_copia(raiz: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def quebra(*a: Any, **k: Any) -> NoReturn:
        raise RuntimeError(f"defeito {TOKEN}")

    monkeypatch.setattr(AdaptadorInstagram, "_aguardar_container", quebra)
    transporte = TransporteGravado(*PRE, *_copia(1), CONTAINER, *APAGAR)
    r = _adaptador(transporte, raiz).publicar(_pedido())
    assert r.tipo is TipoDeResultado.FALHA_PERMANENTE and r.motivo == "erro_interno"
    assert _criadas(transporte) == _apagadas(transporte) == {_id_release(1)}


@pytest.mark.parametrize("onde", ["upload", "container", "status", "publish"])
def test_cancelamento_do_job_apaga_a_copia_e_propaga(raiz: Path, onde: str) -> None:
    """Simula o cancelamento do job (KeyboardInterrupt, uma BaseException) no meio do envio."""
    corte = KeyboardInterrupt()
    respostas: dict[str, list[Resposta]] = {
        "upload": [*PRE, _release(1), corte, APAGADO, APAGADO],
        "container": [*PRE, *_copia(1), corte, *APAGAR],
        "status": [*PRE, *_copia(1), CONTAINER, corte, *APAGAR],
        "publish": [*PRE, *_copia(1), CONTAINER, FINALIZADO, corte, *APAGAR],
    }
    transporte = TransporteGravado(*respostas[onde])
    adaptador = _adaptador(transporte, raiz)
    with pytest.raises(KeyboardInterrupt):
        adaptador.publicar(_pedido())
    assert _criadas(transporte) == _apagadas(transporte) == {_id_release(1)}
    assert transporte.restantes == 0
    if onde == "publish":  # o publish pode ter saído: repetir nunca reenvia
        r = adaptador.publicar(_pedido())
        assert r.tipo is TipoDeResultado.INCERTO
        assert len(transporte.chamadas) == len(respostas[onde])


@pytest.mark.parametrize(
    "apagar",
    [
        [(500, b"{}"), (500, b"{}"), (500, b"{}")],
        [OSError("queda"), OSError("queda"), OSError("queda")],
        [APAGADO, (403, b"{}"), APAGADO],
    ],
)
def test_falha_ao_apagar_nao_muda_o_resultado(
    raiz: Path, apagar: list[Resposta], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    transporte = TransporteGravado(*PRE, *_copia(1), CONTAINER, FINALIZADO, PUBLICADO_OK, *apagar)
    r = _adaptador(transporte, raiz).publicar(_pedido())
    assert r.tipo is TipoDeResultado.SUCESSO and r.id_externo == ID_MIDIA and r.motivo is None
    assert "copia_nao_apagada" in caplog.text
    assert _url(1) not in caplog.text and _tag(1) not in caplog.text


def test_estagio_que_levanta_ao_apagar_nao_muda_o_resultado(raiz: Path) -> None:
    class EstagioRuim(EstagioGitHub):
        def apagar_copia(self, copia: Any) -> bool:
            raise RuntimeError(f"quebrou {TOKEN_ESTAGIO}")

    transporte = TransporteGravado(*PRE, *_copia(1), CONTAINER, FINALIZADO, PUBLICADO_OK)
    estagio = EstagioRuim(
        REPOSITORIO, SegredoStr(TOKEN_ESTAGIO), transporte, lambda: AGORA, gerar_sufixo=_Sufixos()
    )
    r = _adaptador(transporte, raiz, estagio=estagio).publicar(_pedido())
    assert r.tipo is TipoDeResultado.SUCESSO


# --------------------------------------------------------------------------- idempotência


def test_repetir_apos_incerto_nao_faz_segundo_publish_nem_nova_copia(raiz: Path) -> None:
    transporte = TransporteGravado(
        *PRE, *_copia(1), CONTAINER, FINALIZADO, OSError("sem resposta"), *APAGAR
    )
    adaptador = _adaptador(transporte, raiz)
    assert adaptador.publicar(_pedido()).tipo is TipoDeResultado.INCERTO
    total = len(transporte.chamadas)
    r = adaptador.publicar(_pedido())
    assert r.tipo is TipoDeResultado.INCERTO and r.motivo == "publish_ja_enviado"
    assert len(transporte.chamadas) == total
    assert _publicacoes(transporte) == 1


def test_repetir_apos_sucesso_devolve_o_mesmo_sem_rede(raiz: Path) -> None:
    transporte = TransporteGravado(*_feliz())
    adaptador = _adaptador(transporte, raiz)
    primeiro = adaptador.publicar(_pedido())
    total = len(transporte.chamadas)
    assert adaptador.publicar(_pedido()) == primeiro
    assert len(transporte.chamadas) == total


def test_repetir_apos_falha_antes_do_publish_recomeca_com_nova_copia(raiz: Path) -> None:
    transporte = TransporteGravado(
        *PRE, *_copia(1), CONTAINER, (500, b""), *APAGAR,
        # a segunda tentativa usa cópia NOVA (release 2): a anterior já foi apagada
        *PRE, _release(2), _asset(2, item=1), CONTAINER, FINALIZADO, PUBLICADO_OK, *APAGAR,
    )  # fmt: skip
    adaptador = _adaptador(transporte, raiz)
    assert adaptador.publicar(_pedido()).tipo is TipoDeResultado.FALHA_TRANSITORIA
    r = adaptador.publicar(_pedido())
    assert r.tipo is TipoDeResultado.SUCESSO
    assert _criadas(transporte) == _apagadas(transporte) == {_id_release(1), _id_release(2)}
    assert _publicacoes(transporte) == 1


# --------------------------------------------------------------------------- A-004 e I-1


def test_token_de_escrita_so_nas_chamadas_do_estagio(raiz: Path) -> None:
    """A-004: token do estágio só no estágio; nem o Graph nem o armazém o recebem."""
    transporte_artes = TransporteGravado(
        *[(200, (raiz / _arte(i)).read_bytes()) for i in (1, 2)] * 2
    )
    armazem = ArmazemGitHub(
        "dono-ficticio", "artes-ficticias", "main", SegredoStr(TOKEN_ARTES), transporte_artes
    )
    respostas: list[Resposta] = [*PRE, *_copia(1), *_copia(2)]
    respostas += [(200, json.dumps({"id": _filho(i)}).encode()) for i in (1, 2)]
    respostas += [CONTAINER, FINALIZADO, PUBLICADO_OK, *APAGAR, *APAGAR]
    transporte = TransporteGravado(*respostas)
    r = _adaptador(transporte, raiz, armazem=armazem).publicar(_carrossel(2))
    assert r.tipo is TipoDeResultado.SUCESSO

    for c in transporte.chamadas:
        texto = repr(c.cabecalhos) + c.url + (c.corpo or b"").decode("latin-1")
        if "github.com" in c.url:
            assert c.cabecalhos["Authorization"] == f"Bearer {TOKEN_ESTAGIO}"
            assert TOKEN not in texto
        else:
            assert c.url.startswith(BASE)
            assert TOKEN_ESTAGIO not in texto
    for c in transporte_artes.chamadas:
        texto = repr(c.cabecalhos) + c.url
        assert TOKEN_ESTAGIO not in texto and TOKEN not in texto
        assert c.cabecalhos["Authorization"] == f"Bearer {TOKEN_ARTES}"


def _eco() -> bytes:
    return (
        f'{{"error":{{"message":"{TOKEN} {TOKEN_ESTAGIO} {TEXTO_SECRETO} '
        f'{CORPO_DE_ERRO_UNICO}","code":100}}}}'
    ).encode()


@pytest.mark.parametrize(
    "respostas",
    [
        [*PRE, (500, _eco())],
        [*PRE, _release(1), (422, _eco()), (500, _eco()), (500, _eco())],
        [*PRE, *_copia(1), (400, _eco()), (500, _eco()), (500, _eco()), (500, _eco())],
        [*PRE, *_copia(1), CONTAINER, FINALIZADO, (400, _eco()), *APAGAR],
        [*PRE, *_copia(1), CONTAINER, FINALIZADO,
         OSError(f"Authorization: Bearer {TOKEN} {TOKEN_ESTAGIO} {CORPO_DE_ERRO_UNICO}"),
         OSError(f"{TOKEN_ESTAGIO}"), OSError(f"{TOKEN_ESTAGIO}"), OSError(f"{TOKEN_ESTAGIO}")],
    ],
)  # fmt: skip
def test_tokens_corpo_e_url_da_copia_nunca_vazam(
    raiz: Path, respostas: list[Resposta], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    transporte = TransporteGravado(*respostas)
    adaptador = _adaptador(transporte, raiz)
    r = adaptador.publicar(_pedido())
    texto = "\n".join([repr(adaptador), str(adaptador), repr(r), str(r), caplog.text])
    for proibido in (TOKEN, TOKEN_ESTAGIO, TEXTO_SECRETO, CORPO_DE_ERRO_UNICO, _url(1), _arte(1)):
        assert proibido not in texto
    assert _criadas(transporte) == _apagadas(transporte)


def test_eco_dos_tokens_no_sucesso_nao_vaza(raiz: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    release = json.loads(_release(1)[1])
    release["body"] = TOKEN_ESTAGIO
    publish = f'{{"id":"{ID_MIDIA}","t":"{TOKEN}","u":"{TOKEN_ESTAGIO}"}}'.encode()
    transporte = TransporteGravado(
        *PRE, (201, json.dumps(release).encode()), _asset(1), CONTAINER, FINALIZADO,
        (200, publish), *APAGAR,
    )  # fmt: skip
    adaptador = _adaptador(transporte, raiz)
    r = adaptador.publicar(_pedido())
    assert r.tipo is TipoDeResultado.SUCESSO
    texto = repr(adaptador) + repr(r) + caplog.text
    assert TOKEN not in texto and TOKEN_ESTAGIO not in texto


def test_nome_da_copia_nao_tem_titulo_corpo_nem_caminho(raiz: Path) -> None:
    transporte = TransporteGravado(*_feliz())
    _adaptador(transporte, raiz).publicar(_pedido())
    for c in transporte.chamadas:
        if "github.com" in c.url:
            texto = c.url + (c.corpo or b"").decode("latin-1")
            for proibido in ("arte-1", "serie-a", "Titulo", TEXTO_SECRETO, "post-ficticio"):
                assert proibido not in texto


def test_excecao_do_construtor_nao_vaza(raiz: Path) -> None:
    with pytest.raises(TypeError) as info:
        _adaptador(TransporteGravado(), raiz, estagio=object())
    assert TOKEN not in "".join(traceback.format_exception(info.value))
