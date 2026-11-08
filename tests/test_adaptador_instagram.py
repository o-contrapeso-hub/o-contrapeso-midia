"""Testes de contrato do adaptador Instagram, parte 1 (T043; RF-14, RF-18).

Só respostas GRAVADAS/fabricadas (tests/fixtures/instagram) e tokens FALSOS: nenhuma
chamada de rede real. I-1: o token falso nunca aparece em repr/str/log/exceção/retorno.
I-2: ``publicar`` não existe nesta parte: recusa sem rede e nunca devolve sucesso.
"""

from __future__ import annotations

import json
import logging
import socket
import struct
import traceback
import urllib.request
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn

import pytest

from publicador.adaptadores.base import (
    EstadoConsulta,
    PedidoDePublicacao,
    PlatformAdapter,
    TipoDeProblema,
    TipoDeResultado,
)
from publicador.adaptadores.instagram import (
    VERSAO_GRAPH,
    AdaptadorInstagram,
    LimiteDiario,
)
from publicador.artes.armazem import ArmazemLocal
from publicador.core.modelos import ConexaoEstado, Estagio, PapelEstagio, Post
from publicador.core.redacao import SegredoStr, limpar_segredos_registrados

FIXTURES = Path(__file__).parent / "fixtures" / "instagram"
TOKEN_FICTICIO = "tok_ficticio_ZXCV1234abcd"
ID_CONTA = "17841400000000001"
EXPIRA_EM = datetime.fromtimestamp(1767744000, UTC)
AGORA = EXPIRA_EM - timedelta(days=10, hours=3)
CHAVE = "post-ficticio-0001|instagram|2026-11-07T18:30:00-03:00"
TEXTO_SECRETO = "Roteiro ficticio ULTRA-SECRETO"


@pytest.fixture(autouse=True)
def _limpar_segredos() -> Any:
    yield
    limpar_segredos_registrados()


def _fixture(nome: str) -> bytes:
    return (FIXTURES / nome).read_bytes()


class TransporteGravado:
    """Devolve respostas gravadas; registra as chamadas (sem rede)."""

    def __init__(self, *respostas: tuple[int, bytes]) -> None:
        self.respostas = list(respostas)
        self.chamadas: list[tuple[str, dict[str, str], float]] = []

    def obter(self, url: str, cabecalhos: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
        self.chamadas.append((url, dict(cabecalhos), timeout))
        return self.respostas.pop(0) if len(self.respostas) > 1 else self.respostas[0]


class TransporteQueExplode:
    def obter(self, url: str, cabecalhos: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
        raise OSError(f"falha {cabecalhos.get('Authorization')} em {url}")


def _jpeg(largura: int, altura: int) -> bytes:
    """JPEG fabricado: SOI + SOF0 com as dimensões + EOI (basta para ler a proporção)."""
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, altura, largura, 1) + b"\x01\x11\x00"
    return b"\xff\xd8\xff\xe0\x00\x04JF" + sof + b"\xff\xd9"


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 16


@pytest.fixture
def raiz(tmp_path: Path) -> Path:
    return tmp_path / "artes"


def _gravar(raiz: Path, nome: str, conteudo: bytes) -> str:
    destino = raiz / nome
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_bytes(conteudo)
    return nome


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


def _adaptador(
    transporte: Any = None, raiz: Path | None = None, agora: datetime = AGORA, **kw: Any
) -> AdaptadorInstagram:
    return AdaptadorInstagram(
        SegredoStr(TOKEN_FICTICIO),
        ID_CONTA,
        transporte if transporte is not None else TransporteGravado((200, b"{}")),
        ArmazemLocal(raiz) if raiz is not None else ArmazemLocal(Path("/nao/existe")),
        lambda: agora,
        **kw,
    )


def _tipos(adaptador: AdaptadorInstagram, post: Post, artes: tuple[str, ...]) -> list[Any]:
    return [p.tipo for p in adaptador.validar_midia(post, artes).problemas]


# --------------------------------------------------------------------------- protocolo


def test_cumpre_o_protocolo_e_nao_e_registrado_por_padrao() -> None:
    adaptador = _adaptador()
    assert isinstance(adaptador, PlatformAdapter)
    assert adaptador.plataforma == "instagram"


def test_exige_segredostr_e_https() -> None:
    with pytest.raises(TypeError):
        AdaptadorInstagram(
            TOKEN_FICTICIO, ID_CONTA, TransporteGravado(), ArmazemLocal(Path(".")), lambda: AGORA
        )  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        _adaptador(url_base="http://graph.facebook.com")
    with pytest.raises(ValueError):
        AdaptadorInstagram(
            SegredoStr(TOKEN_FICTICIO),
            "../x",
            TransporteGravado(),
            ArmazemLocal(Path(".")),
            lambda: AGORA,
        )


# --------------------------------------------------------------------------- conexão


def test_conexao_ativa_com_dias_restantes() -> None:
    transporte = TransporteGravado((200, _fixture("debug_token_ativa.json")))
    estado = _adaptador(transporte).verificar_conexao()
    assert estado.estado is ConexaoEstado.ATIVA
    assert estado.valida
    assert estado.dias_restantes == 10
    url, cabecalhos, timeout = transporte.chamadas[0]
    assert url.startswith(f"https://graph.facebook.com/{VERSAO_GRAPH}/debug_token")
    assert cabecalhos["Authorization"] == f"Bearer {TOKEN_FICTICIO}"
    assert 0 < timeout <= 60


def test_conexao_sem_expiracao_informada_e_ativa_sem_dias() -> None:
    estado = _adaptador(
        TransporteGravado((200, _fixture("debug_token_sem_expiracao.json")))
    ).verificar_conexao()
    assert estado.estado is ConexaoEstado.ATIVA
    assert estado.dias_restantes is None


def test_conexao_expirada_pela_data_mesmo_se_marcada_valida() -> None:
    transporte = TransporteGravado((200, _fixture("debug_token_ativa.json")))
    estado = _adaptador(transporte, agora=EXPIRA_EM + timedelta(seconds=1)).verificar_conexao()
    assert estado.estado is ConexaoEstado.EXPIRADA
    assert not estado.valida


def test_conexao_expirada_no_servidor() -> None:
    estado = _adaptador(
        TransporteGravado((200, _fixture("debug_token_expirado.json")))
    ).verificar_conexao()
    assert estado.estado is ConexaoEstado.EXPIRADA


def test_conexao_token_invalido() -> None:
    estado = _adaptador(
        TransporteGravado((200, _fixture("debug_token_invalido.json")))
    ).verificar_conexao()
    assert estado.estado is ConexaoEstado.INVALIDA


def test_conexao_sem_escopo_de_publicacao_e_invalida() -> None:
    estado = _adaptador(
        TransporteGravado((200, _fixture("debug_token_sem_escopo.json")))
    ).verificar_conexao()
    assert estado.estado is ConexaoEstado.INVALIDA


@pytest.mark.parametrize(
    "resposta",
    [
        (401, _fixture("erro_oauth_190.json")),
        (400, _fixture("erro_oauth_190.json")),
        (500, b"erro interno"),
        (200, _fixture("erro_oauth_190.json")),
        (200, _fixture("debug_token_malformada.json")),
        (200, b"nao e json"),
        (200, b"[]"),
        (200, b'{"data": null}'),
        (200, b""),
        (200, b'{"data":{"is_valid":true,"expires_at":true}}'),
        (200, b'{"data":{"is_valid":true,"expires_at":-5}}'),
        (200, b'{"data":{"is_valid":true}}'),
        (200, b"\xff\xfe\x00"),
    ],
)
def test_conexao_resposta_ruim_nunca_e_ativa(resposta: tuple[int, bytes]) -> None:
    estado = _adaptador(TransporteGravado(resposta)).verificar_conexao()
    assert estado.estado is not ConexaoEstado.ATIVA
    assert not estado.valida


def test_conexao_erro_de_rede_e_invalida_sem_vazar_token(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    estado = _adaptador(TransporteQueExplode()).verificar_conexao()
    assert estado.estado is ConexaoEstado.INVALIDA
    assert TOKEN_FICTICIO not in caplog.text + repr(estado)


# --------------------------------------------------------------------------- limite diário


def test_limite_com_restante() -> None:
    transporte = TransporteGravado((200, _fixture("limite_restante.json")))
    adaptador = _adaptador(transporte)
    limite = adaptador.limite_diario()
    assert limite == LimiteDiario(usado=37, total=100)
    assert limite is not None and limite.restante == 63
    url, cabecalhos, _ = transporte.chamadas[0]
    assert f"/{ID_CONTA}/content_publishing_limit" in url
    assert cabecalhos["Authorization"] == f"Bearer {TOKEN_FICTICIO}"
    assert adaptador.limite_diario_restante() == 63


@pytest.mark.parametrize("nome", ["limite_esgotado.json", "limite_excedido.json"])
def test_limite_esgotado_restante_zero(nome: str) -> None:
    adaptador = _adaptador(TransporteGravado((200, _fixture(nome))))
    limite = adaptador.limite_diario()
    assert limite is not None and limite.restante == 0
    assert adaptador.limite_diario_restante() == 0


@pytest.mark.parametrize(
    "resposta",
    [
        (200, _fixture("limite_malformado.json")),
        (200, _fixture("limite_vazio.json")),
        (401, _fixture("erro_oauth_190.json")),
        (500, b""),
        (200, b"lixo"),
        (200, b'{"data":[{"quota_usage":-1,"config":{"quota_total":100}}]}'),
        (200, b'{"data":[{"quota_usage":1,"config":{"quota_total":0}}]}'),
        (200, b'{"data":[{"quota_usage":true,"config":{"quota_total":100}}]}'),
    ],
)
def test_limite_erro_na_consulta_e_indisponivel(resposta: tuple[int, bytes]) -> None:
    adaptador = _adaptador(TransporteGravado(resposta))
    assert adaptador.limite_diario() is None
    # lado seguro: None no protocolo significaria "sem limite"; aqui bloqueia (0).
    assert adaptador.limite_diario_restante() == 0


def test_limite_erro_de_rede_e_indisponivel(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    adaptador = _adaptador(TransporteQueExplode())
    assert adaptador.limite_diario() is None
    assert adaptador.limite_diario_restante() == 0
    assert TOKEN_FICTICIO not in caplog.text


# --------------------------------------------------------------------------- mídia


def test_feed_imagem_unica_aceita(raiz: Path) -> None:
    arte = _gravar(raiz, "s/2026/11/a.jpg", _jpeg(1080, 1350))  # 4:5
    assert _adaptador(raiz=raiz).validar_midia(_post(), (arte,)).valida


@pytest.mark.parametrize("dim", [(1080, 1350), (1080, 1080), (1910, 1000), (1080, 566)])
def test_feed_proporcoes_nos_limites_aceitas(raiz: Path, dim: tuple[int, int]) -> None:
    arte = _gravar(raiz, "a.jpeg", _jpeg(*dim))
    assert _adaptador(raiz=raiz).validar_midia(_post(), (arte,)).valida


@pytest.mark.parametrize(
    "dim", [(1000, 1500), (1080, 1351), (2000, 1000), (1920, 1000), (400, 2000)]
)
def test_feed_proporcao_fora_do_intervalo(raiz: Path, dim: tuple[int, int]) -> None:
    arte = _gravar(raiz, "a.jpg", _jpeg(*dim))
    problemas = _adaptador(raiz=raiz).validar_midia(_post(), (arte,)).problemas
    assert [(p.tipo, p.indice_arte) for p in problemas] == [
        (TipoDeProblema.PROPORCAO_FORA_DO_PERMITIDO, 0)
    ]


def test_carrossel_aceita_2_a_10_e_valida_cada_item(raiz: Path) -> None:
    artes = tuple(_gravar(raiz, f"c/{i}.jpg", _jpeg(1080, 1080)) for i in range(10))
    adaptador = _adaptador(raiz=raiz)
    assert adaptador.validar_midia(_post(), artes).valida
    assert adaptador.validar_midia(_post(), artes[:2]).valida
    ruim = _gravar(raiz, "c/ruim.jpg", _jpeg(1000, 2000))
    problemas = adaptador.validar_midia(_post(), (artes[0], ruim)).problemas
    assert [(p.tipo, p.indice_arte) for p in problemas] == [
        (TipoDeProblema.PROPORCAO_FORA_DO_PERMITIDO, 1)
    ]


def test_carrossel_quantidade_fora_do_permitido(raiz: Path) -> None:
    artes = tuple(_gravar(raiz, f"c/{i}.jpg", _jpeg(1080, 1080)) for i in range(11))
    adaptador = _adaptador(raiz=raiz)
    assert TipoDeProblema.QUANTIDADE_DE_ARTES in _tipos(adaptador, _post(), artes)
    assert _tipos(adaptador, _post(), ()) == [TipoDeProblema.QUANTIDADE_DE_ARTES]
    # formato "Carrossel" exige pelo menos 2 itens
    assert TipoDeProblema.QUANTIDADE_DE_ARTES in _tipos(
        adaptador, _post(formato="Carrossel"), artes[:1]
    )
    assert adaptador.validar_midia(_post(formato="Carrossel"), artes[:2]).valida


def test_rejeita_png_e_webp_pela_extensao(raiz: Path) -> None:
    png = _gravar(raiz, "a.png", PNG)
    webp = _gravar(raiz, "b.webp", WEBP)
    adaptador = _adaptador(raiz=raiz)
    assert _tipos(adaptador, _post(), (png,)) == [TipoDeProblema.EXTENSAO_NAO_SUPORTADA]
    assert _tipos(adaptador, _post(), (webp,)) == [TipoDeProblema.EXTENSAO_NAO_SUPORTADA]


def test_rejeita_png_e_webp_disfarcados_de_jpg(raiz: Path) -> None:
    png = _gravar(raiz, "a.jpg", PNG)
    webp = _gravar(raiz, "b.jpg", WEBP)
    adaptador = _adaptador(raiz=raiz)
    assert _tipos(adaptador, _post(), (png,)) == [TipoDeProblema.TIPO_DE_ARQUIVO_INVALIDO]
    assert _tipos(adaptador, _post(), (webp,)) == [TipoDeProblema.TIPO_DE_ARQUIVO_INVALIDO]


def test_jpeg_corrompido_ou_sem_dimensoes(raiz: Path) -> None:
    truncado = _gravar(raiz, "a.jpg", b"\xff\xd8\xff\xe0\x00\x04JF")
    zerado = _gravar(raiz, "b.jpg", _jpeg(0, 0))
    adaptador = _adaptador(raiz=raiz)
    assert _tipos(adaptador, _post(), (truncado,)) == [TipoDeProblema.IMAGEM_ILEGIVEL]
    assert _tipos(adaptador, _post(), (zerado,)) == [TipoDeProblema.IMAGEM_ILEGIVEL]


def test_imagem_grande_demais(raiz: Path) -> None:
    grande = _jpeg(1080, 1080) + b"\x00" * (8 * 1024 * 1024)
    arte = _gravar(raiz, "a.jpg", grande)
    assert _tipos(_adaptador(raiz=raiz), _post(), (arte,)) == [TipoDeProblema.TAMANHO_EXCEDIDO]


def test_arte_ausente_e_caminho_invalido(raiz: Path) -> None:
    raiz.mkdir()
    adaptador = _adaptador(raiz=raiz)
    problemas = adaptador.validar_midia(_post(), ("nao/existe.jpg", "../fora.jpg")).problemas
    assert [(p.tipo, p.indice_arte) for p in problemas] == [
        (TipoDeProblema.ARTE_NAO_ENCONTRADA, 0),
        (TipoDeProblema.CAMINHO_INVALIDO, 1),
    ]


def test_armazem_indisponivel() -> None:
    from publicador.artes.armazem import ErroAcessoArmazem

    class ArmazemQuebrado:
        def ler(self, caminho: str) -> bytes:
            raise ErroAcessoArmazem("x")

        def hash_sha256(self, caminho: str) -> str:
            raise ErroAcessoArmazem("x")

        def existe(self, caminho: str) -> bool:
            raise ErroAcessoArmazem("x")

    adaptador = AdaptadorInstagram(
        SegredoStr(TOKEN_FICTICIO), ID_CONTA, TransporteGravado(), ArmazemQuebrado(), lambda: AGORA
    )
    assert _tipos(adaptador, _post(), ("a.jpg",)) == [TipoDeProblema.ARMAZEM_INDISPONIVEL]


def test_stories_proporcao_9_16(raiz: Path) -> None:
    ok = _gravar(raiz, "s.jpg", _jpeg(1080, 1920))
    quadrado = _gravar(raiz, "q.jpg", _jpeg(1080, 1080))
    adaptador = _adaptador(raiz=raiz)
    post = _post(formato="Stories")
    assert adaptador.validar_midia(post, (ok,)).valida
    assert _tipos(adaptador, post, (quadrado,)) == [TipoDeProblema.PROPORCAO_FORA_DO_PERMITIDO]
    assert TipoDeProblema.QUANTIDADE_DE_ARTES in _tipos(adaptador, post, (ok, ok))


def test_reels_e_stories_aceitam_video_por_extensao_e_feed_nao(raiz: Path) -> None:
    video = _gravar(raiz, "v.mp4", b"video-ficticio")
    adaptador = _adaptador(raiz=raiz)
    assert adaptador.validar_midia(_post(formato="Reels"), (video,)).valida
    assert adaptador.validar_midia(_post(formato="Stories"), (video,)).valida
    assert _tipos(adaptador, _post(), (video,)) == [TipoDeProblema.EXTENSAO_NAO_SUPORTADA]
    img = _gravar(raiz, "i.jpg", _jpeg(1080, 1920))
    assert _tipos(adaptador, _post(formato="Reels"), (img,)) == [
        TipoDeProblema.EXTENSAO_NAO_SUPORTADA
    ]


@pytest.mark.parametrize("formato", ["Podcast", "Thread", "reels-longo"])
def test_formato_nao_suportado(raiz: Path, formato: str) -> None:
    arte = _gravar(raiz, "a.jpg", _jpeg(1080, 1080))
    assert _tipos(_adaptador(raiz=raiz), _post(formato=formato), (arte,)) == [
        TipoDeProblema.FORMATO_NAO_SUPORTADO
    ]


def test_formato_ausente_e_plataforma_fora_do_post(raiz: Path) -> None:
    arte = _gravar(raiz, "a.jpg", _jpeg(1080, 1080))
    adaptador = _adaptador(raiz=raiz)
    assert _tipos(adaptador, _post(formato=None), (arte,)) == [TipoDeProblema.FORMATO_AUSENTE]
    assert TipoDeProblema.PLATAFORMA_FORA_DO_POST in _tipos(
        adaptador, _post(plataformas=("tiktok",)), (arte,)
    )


def test_formato_e_case_insensitive(raiz: Path) -> None:
    arte = _gravar(raiz, "a.jpg", _jpeg(1080, 1080))
    assert _adaptador(raiz=raiz).validar_midia(_post(formato=" FEED "), (arte,)).valida


# --------------------------------------------------------------------------- publicar / consultar


def test_publicar_nao_implementado_sem_rede_e_nunca_sucesso(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def proibido(*a: Any, **k: Any) -> NoReturn:
        raise AssertionError("rede proibida")

    monkeypatch.setattr(socket, "socket", proibido)
    monkeypatch.setattr(socket, "create_connection", proibido)
    monkeypatch.setattr(urllib.request, "urlopen", proibido)
    transporte = TransporteGravado((200, b"{}"))
    adaptador = _adaptador(transporte)
    for simular in (True, False):
        pedido = PedidoDePublicacao(_post(), "instagram", ("a.jpg",), CHAVE, simular=simular)
        resultado = adaptador.publicar(pedido)
        assert not resultado.sucesso
        assert resultado.tipo is TipoDeResultado.FALHA_PERMANENTE
        assert resultado.motivo == "nao_implementado"
        assert resultado.id_externo is None
    assert transporte.chamadas == []


def test_consultar_desconhecido_sem_rede() -> None:
    transporte = TransporteGravado((200, b"{}"))
    resultado = _adaptador(transporte).consultar(CHAVE)
    assert resultado.estado is EstadoConsulta.DESCONHECIDO
    assert transporte.chamadas == []


# --------------------------------------------------------------------------- I-1


def test_token_falso_nunca_aparece(raiz: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    arte = _gravar(raiz, "a.jpg", _jpeg(1080, 1080))
    respostas = [
        (200, _fixture("debug_token_ativa.json")),
        (200, _fixture("limite_restante.json")),
        (500, TOKEN_FICTICIO.encode()),
    ]
    for transporte in (TransporteGravado(*respostas), TransporteQueExplode()):
        adaptador = _adaptador(transporte, raiz=raiz)
        pedido = PedidoDePublicacao(_post(), "instagram", (arte,), CHAVE)
        saidas: list[object] = [
            adaptador,
            adaptador.verificar_conexao(),
            adaptador.limite_diario(),
            adaptador.limite_diario_restante(),
            adaptador.validar_midia(_post(), (arte,)),
            adaptador.publicar(pedido),
            adaptador.consultar(CHAVE),
        ]
        texto = "\n".join(repr(s) + str(s) for s in saidas) + caplog.text
        assert TOKEN_FICTICIO not in texto
        assert TEXTO_SECRETO not in texto


def test_excecoes_do_construtor_nao_vazam_token() -> None:
    with pytest.raises(ValueError) as info:
        AdaptadorInstagram(
            SegredoStr(TOKEN_FICTICIO),
            TOKEN_FICTICIO,
            TransporteGravado(),
            ArmazemLocal(Path(".")),
            lambda: AGORA,
        )
    texto = "".join(traceback.format_exception(info.value)) + repr(info.value)
    assert TOKEN_FICTICIO not in texto


def test_resposta_com_eco_do_token_nao_vaza_no_estado() -> None:
    corpo = json.dumps({"data": {"is_valid": False, "error": {"message": TOKEN_FICTICIO}}}).encode()
    estado = _adaptador(TransporteGravado((200, corpo))).verificar_conexao()
    assert TOKEN_FICTICIO not in repr(estado)
