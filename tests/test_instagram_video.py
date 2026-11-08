"""Testes de contrato do Instagram, vídeo/Reels por upload resumable (T045; RF-10, RF-12,
RF-25, RF-38).

Só respostas FABRICADAS (tests/fixtures/instagram) e token FALSO: nenhuma chamada de rede
real, nenhum sleep real. I-1: o token falso nunca aparece em repr/str/log/exceção/resultado,
nem quando a resposta o ecoa. I-2: o adaptador só é chamado depois da guarda; nada aqui
escreve estágio.
"""

from __future__ import annotations

import io
import logging
import socket
import traceback
import urllib.error
import urllib.request
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import parse_qs

import pytest
from transporte_gravado import Resposta, TransporteGravado

from publicador.adaptadores.base import PedidoDePublicacao, TipoDeResultado
from publicador.adaptadores.instagram import AdaptadorInstagram
from publicador.artes.armazem import ArmazemLocal, TransporteHttpUrllib
from publicador.core.modelos import Estagio, PapelEstagio, Post
from publicador.core.redacao import SegredoStr, limpar_segredos_registrados

FIXTURES = Path(__file__).parent / "fixtures" / "instagram"
TOKEN = "tok_ficticio_ZXCV1234abcd"
ID_CONTA = "17841400000000001"
EXPIRA_EM = datetime.fromtimestamp(1767744000, UTC)
AGORA = EXPIRA_EM - timedelta(days=10, hours=3)
CHAVE = "post-ficticio-0001|instagram|2026-11-07T18:30:00-03:00"
TEXTO_SECRETO = "Legenda ficticia ULTRA-SECRETA"
CORPO_DE_ERRO_UNICO = "CORPO-DE-ERRO-QUE-NAO-PODE-IR-A-LOG"
VIDEO = b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 4
ARTE = "serie-a/2026/11/video.mp4"
ID_CONTAINER = "17889000000000001"
ID_MIDIA = "17895000000000042"
BASE = "https://graph.facebook.com/v21.0"


@pytest.fixture(autouse=True)
def _limpar_segredos() -> Any:
    yield
    limpar_segredos_registrados()


def _f(nome: str) -> bytes:
    return (FIXTURES / nome).read_bytes()


@pytest.fixture
def raiz(tmp_path: Path) -> Path:
    destino = tmp_path / "artes" / ARTE
    destino.parent.mkdir(parents=True)
    destino.write_bytes(VIDEO)
    return tmp_path / "artes"


def _post(**mudancas: Any) -> Post:
    base: dict[str, Any] = {
        "id": "post-ficticio-0001",
        "titulo": "Titulo ficticio",
        "corpo": TEXTO_SECRETO,
        "estagio": Estagio("confirmado", PapelEstagio.AUTORIZADO),
        "plataformas": ("instagram",),
        "formato": "Reels",
    }
    base.update(mudancas)
    return Post(**base)


def _pedido(
    simular: bool = False, post: Post | None = None, artes: tuple[str, ...] = (ARTE,)
) -> Any:
    return PedidoDePublicacao(post or _post(), "instagram", artes, CHAVE, simular=simular)


class Dorminhoco:
    """Relógio/sono injetado: registra os intervalos e nunca dorme."""

    def __init__(self) -> None:
        self.pausas: list[float] = []

    def __call__(self, segundos: float) -> None:
        self.pausas.append(segundos)


def _adaptador(
    transporte: Any, raiz: Path, dormir: Dorminhoco | None = None, **kw: Any
) -> AdaptadorInstagram:
    return AdaptadorInstagram(
        SegredoStr(TOKEN),
        ID_CONTA,
        transporte,
        ArmazemLocal(raiz),
        lambda: AGORA,
        dormir=dormir if dormir is not None else Dorminhoco(),
        **kw,
    )


# Respostas gravadas, na ordem em que o fluxo as consome.
PRE = [(200, _f("debug_token_ativa.json")), (200, _f("limite_restante.json"))]
CONTAINER = (200, _f("container_resumable.json"))
UPLOAD = (200, _f("upload_ok.json"))
EM_ANDAMENTO = (200, _f("status_em_andamento.json"))
FINALIZADO = (200, _f("status_finalizado.json"))
PUBLICADO_OK = (200, _f("publish_ok.json"))


def _feliz() -> list[Resposta]:
    return [*PRE, CONTAINER, UPLOAD, EM_ANDAMENTO, FINALIZADO, PUBLICADO_OK]


def _publicacoes(transporte: TransporteGravado) -> int:
    return sum(1 for c in transporte.chamadas if c.url.endswith("/media_publish"))


def _criacoes(transporte: TransporteGravado) -> int:
    return sum(1 for c in transporte.chamadas if c.url.endswith(f"/{ID_CONTA}/media"))


# --------------------------------------------------------------------------- caminho feliz


def test_caminho_feliz_sequencia_exata_de_chamadas(raiz: Path) -> None:
    transporte = TransporteGravado(*_feliz())
    sono = Dorminhoco()
    adaptador = _adaptador(transporte, raiz, sono, intervalo_status=7.0)
    resultado = adaptador.publicar(_pedido())

    assert resultado.tipo is TipoDeResultado.SUCESSO
    assert resultado.id_externo == ID_MIDIA
    assert resultado.simulado is False
    assert resultado.chave_idempotencia == CHAVE
    assert transporte.restantes == 0
    assert transporte.metodos() == ["GET", "GET", "POST", "POST", "GET", "GET", "POST"]
    urls = [c.url for c in transporte.chamadas]
    assert urls[0].startswith(f"{BASE}/debug_token")
    assert urls[1].startswith(f"{BASE}/{ID_CONTA}/content_publishing_limit")
    assert urls[2] == f"{BASE}/{ID_CONTA}/media"
    assert urls[3] == f"https://rupload.facebook.com/ig-api-upload/v21.0/{ID_CONTAINER}"
    assert urls[4] == urls[5] == f"{BASE}/{ID_CONTAINER}?fields=status_code"
    assert urls[6] == f"{BASE}/{ID_CONTA}/media_publish"
    assert sono.pausas == [7.0]  # uma espera entre IN_PROGRESS e FINISHED


def test_container_resumable_com_legenda_e_upload_com_bytes(raiz: Path) -> None:
    transporte = TransporteGravado(*_feliz())
    _adaptador(transporte, raiz).publicar(_pedido())
    criar, upload, publicar = (transporte.chamadas[i] for i in (2, 3, 6))

    campos = parse_qs((criar.corpo or b"").decode())
    assert campos == {
        "media_type": ["REELS"],
        "upload_type": ["resumable"],
        "caption": [TEXTO_SECRETO],
    }
    assert criar.cabecalhos["Authorization"] == f"Bearer {TOKEN}"
    assert TEXTO_SECRETO not in criar.url  # legenda no corpo, nunca na URL

    assert upload.corpo == VIDEO
    assert upload.cabecalhos["Authorization"] == f"OAuth {TOKEN}"
    assert upload.cabecalhos["offset"] == "0"
    assert upload.cabecalhos["file_size"] == str(len(VIDEO))
    assert upload.timeout > criar.timeout  # envio de vídeo tem prazo maior

    assert parse_qs((publicar.corpo or b"").decode()) == {"creation_id": [ID_CONTAINER]}


def test_story_em_video_usa_media_type_stories(raiz: Path) -> None:
    transporte = TransporteGravado(*_feliz())
    resultado = _adaptador(transporte, raiz).publicar(_pedido(post=_post(formato="Stories")))
    assert resultado.tipo is TipoDeResultado.SUCESSO
    assert parse_qs((transporte.chamadas[2].corpo or b"").decode())["media_type"] == ["STORIES"]


def test_imagem_e_carrossel_sem_estagio_nao_tocam_a_rede(raiz: Path) -> None:
    """T044 implementou imagem/carrossel; sem estágio configurado continuam sem rede."""
    transporte = TransporteGravado()
    adaptador = _adaptador(transporte, raiz)
    casos = [
        (_post(formato="Feed"), ("a.jpg",)),
        (_post(formato="Carrossel"), ("a.jpg", "b.jpg")),
        (_post(formato="Stories"), ("a.jpg",)),
    ]
    for post, artes in casos:
        for simular in (True, False):
            r = adaptador.publicar(_pedido(simular, post, artes))
            assert r.tipo is TipoDeResultado.FALHA_PERMANENTE
            assert r.motivo == "estagio_nao_configurado"
    assert transporte.chamadas == []


# --------------------------------------------------------------------------- simulação


def test_simulacao_nao_faz_chamada_de_rede(raiz: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def proibido(*a: Any, **k: Any) -> NoReturn:
        raise AssertionError("rede proibida")

    monkeypatch.setattr(socket, "socket", proibido)
    monkeypatch.setattr(socket, "create_connection", proibido)
    monkeypatch.setattr(urllib.request, "urlopen", proibido)
    transporte = TransporteGravado()
    adaptador = _adaptador(transporte, raiz)
    r1 = adaptador.publicar(_pedido(simular=True))
    r2 = adaptador.publicar(_pedido(simular=True))
    assert r1.tipo is TipoDeResultado.SUCESSO
    assert r1.simulado is True
    assert r1.id_externo is not None and r1.id_externo.startswith("simulado-")
    assert r1.url is not None and ".invalid/" in r1.url
    assert r1 == r2  # determinística
    assert transporte.chamadas == []


def test_simulacao_aplica_as_mesmas_validacoes_de_midia(raiz: Path) -> None:
    transporte = TransporteGravado()
    adaptador = _adaptador(transporte, raiz)
    ausente = adaptador.publicar(_pedido(True, artes=("nao/existe.mp4",)))
    assert ausente.tipo is TipoDeResultado.FALHA_PERMANENTE
    assert ausente.motivo == "midia_invalida"
    assert ausente.simulado is True
    longa = adaptador.publicar(_pedido(True, _post(corpo="x" * 2201)))
    assert longa.motivo == "legenda_longa"
    assert transporte.chamadas == []


def test_simulacao_nao_cria_andamento_que_bloqueie_o_real(raiz: Path) -> None:
    transporte = TransporteGravado(*_feliz())
    adaptador = _adaptador(transporte, raiz)
    adaptador.publicar(_pedido(simular=True))
    assert adaptador.publicar(_pedido()).tipo is TipoDeResultado.SUCESSO


# --------------------------------------------------------------------------- pré-condições


def test_conexao_invalida_ou_expirada_nada_e_enviado(raiz: Path) -> None:
    for nome in ("debug_token_invalido.json", "debug_token_expirado.json"):
        transporte = TransporteGravado((200, _f(nome)))
        r = _adaptador(transporte, raiz).publicar(_pedido())
        assert r.tipo is TipoDeResultado.FALHA_PERMANENTE
        assert r.motivo == "conexao_invalida"
        assert "POST" not in transporte.metodos()


def test_falha_na_verificacao_de_conexao_nada_e_enviado(raiz: Path) -> None:
    transporte = TransporteGravado(OSError("caiu"))
    r = _adaptador(transporte, raiz).publicar(_pedido())
    assert r.tipo is TipoDeResultado.FALHA_PERMANENTE  # lado seguro da T043 (INVALIDA)
    assert transporte.metodos() == ["GET"]


@pytest.mark.parametrize("fixture", ["limite_esgotado.json", "limite_malformado.json"])
def test_limite_esgotado_ou_indisponivel_nada_e_enviado(raiz: Path, fixture: str) -> None:
    transporte = TransporteGravado((200, _f("debug_token_ativa.json")), (200, _f(fixture)))
    r = _adaptador(transporte, raiz).publicar(_pedido())
    assert r.tipo is TipoDeResultado.FALHA_TRANSITORIA
    assert r.motivo == "limite_diario"
    assert "POST" not in transporte.metodos()


def test_transporte_sem_escrita_falha_sem_enviar_nada(raiz: Path) -> None:
    class SoLeitura:
        def __init__(self) -> None:
            self.chamadas = 0

        def obter(self, url: str, c: Mapping[str, str], t: float) -> tuple[int, bytes]:
            self.chamadas += 1
            return 200, b"{}"

    transporte = SoLeitura()
    r = _adaptador(transporte, raiz).publicar(_pedido())
    assert r.tipo is TipoDeResultado.FALHA_PERMANENTE
    assert r.motivo == "transporte_sem_escrita"
    assert transporte.chamadas == 0


def test_arte_ausente_ou_vazia_ou_legenda_longa_nada_e_enviado(raiz: Path) -> None:
    (raiz / "vazio.mp4").write_bytes(b"")
    transporte = TransporteGravado(*PRE)
    adaptador = _adaptador(transporte, raiz)
    assert adaptador.publicar(_pedido(artes=("nao/existe.mp4",))).motivo == "midia_invalida"
    assert adaptador.publicar(_pedido(post=_post(corpo="x" * 2201))).motivo == "legenda_longa"
    vazia = adaptador.publicar(_pedido(artes=("vazio.mp4",)))
    assert vazia.tipo is TipoDeResultado.FALHA_PERMANENTE
    assert vazia.motivo == "arte_vazia"
    assert "POST" not in transporte.metodos()


def test_armazem_indisponivel_e_transitorio(raiz: Path) -> None:
    from publicador.artes.armazem import ErroAcessoArmazem

    class ArmazemQuebrado:
        def ler(self, caminho: str) -> bytes:
            raise ErroAcessoArmazem("x")

        def hash_sha256(self, caminho: str) -> str:
            raise ErroAcessoArmazem("x")

        def existe(self, caminho: str) -> bool:
            raise ErroAcessoArmazem("x")

    transporte = TransporteGravado()
    adaptador = AdaptadorInstagram(
        SegredoStr(TOKEN), ID_CONTA, transporte, ArmazemQuebrado(), lambda: AGORA
    )
    r = adaptador.publicar(_pedido())
    assert r.tipo is TipoDeResultado.FALHA_TRANSITORIA
    assert transporte.chamadas == []


def test_pedido_de_outra_plataforma_e_recusado(raiz: Path) -> None:
    transporte = TransporteGravado()
    pedido = PedidoDePublicacao(_post(), "tiktok", (ARTE,), CHAVE, simular=False)
    r = _adaptador(transporte, raiz).publicar(pedido)
    assert r.tipo is TipoDeResultado.FALHA_PERMANENTE
    assert transporte.chamadas == []


# --------------------------------------------------------------------------- erros antes do publish

_ETAPAS: dict[str, list[Resposta]] = {
    "container": [*PRE],
    "upload": [*PRE, CONTAINER],
    "status": [*PRE, CONTAINER, UPLOAD],
}


@pytest.mark.parametrize("etapa", list(_ETAPAS))
@pytest.mark.parametrize(
    ("falha", "tipo"),
    [
        ((400, _f("erro_parametro_100.json")), TipoDeResultado.FALHA_PERMANENTE),
        ((401, _f("erro_oauth_190.json")), TipoDeResultado.FALHA_PERMANENTE),
        ((403, b"{}"), TipoDeResultado.FALHA_PERMANENTE),
        ((404, b""), TipoDeResultado.FALHA_PERMANENTE),
        ((302, b""), TipoDeResultado.FALHA_PERMANENTE),
        ((400, _f("erro_limite_taxa_4.json")), TipoDeResultado.FALHA_TRANSITORIA),
        ((400, _f("erro_servico_2.json")), TipoDeResultado.FALHA_TRANSITORIA),
        ((429, b""), TipoDeResultado.FALHA_TRANSITORIA),
        ((500, b"<html>"), TipoDeResultado.FALHA_TRANSITORIA),
        ((503, b""), TipoDeResultado.FALHA_TRANSITORIA),
        (OSError("queda"), TipoDeResultado.FALHA_TRANSITORIA),
        (TimeoutError("lento"), TipoDeResultado.FALHA_TRANSITORIA),
    ],
)
def test_erro_em_cada_etapa_antes_do_publish(
    raiz: Path, etapa: str, falha: Resposta, tipo: TipoDeResultado
) -> None:
    transporte = TransporteGravado(*_ETAPAS[etapa], falha)
    r = _adaptador(transporte, raiz).publicar(_pedido())
    assert r.tipo is tipo
    assert r.motivo is not None and r.motivo.startswith(etapa)
    assert _publicacoes(transporte) == 0
    assert transporte.restantes == 0


@pytest.mark.parametrize(
    ("etapa", "resposta", "motivo"),
    [
        ("container", (200, b"nao e json"), "container_fora_do_contrato"),
        ("container", (200, b"[]"), "container_fora_do_contrato"),
        ("container", (200, _f("container_sem_id.json")), "container_fora_do_contrato"),
        ("container", (200, _f("container_uri_estranha.json")), "container_uri_invalida"),
        ("container", (200, _f("erro_parametro_100.json")), "container_fora_do_contrato"),
        ("upload", (200, _f("upload_sem_sucesso.json")), "upload_fora_do_contrato"),
        ("upload", (200, b"{}"), "upload_fora_do_contrato"),
        ("upload", (200, b"lixo"), "upload_fora_do_contrato"),
        ("status", (200, _f("status_desconhecido.json")), "status_fora_do_contrato"),
        ("status", (200, b"{}"), "status_fora_do_contrato"),
        ("status", (200, b"lixo"), "status_fora_do_contrato"),
    ],
)
def test_resposta_fora_do_contrato_antes_do_publish_e_permanente(
    raiz: Path, etapa: str, resposta: Resposta, motivo: str
) -> None:
    transporte = TransporteGravado(*_ETAPAS[etapa], resposta)
    r = _adaptador(transporte, raiz).publicar(_pedido())
    assert r.tipo is TipoDeResultado.FALHA_PERMANENTE
    assert r.motivo is not None and r.motivo.endswith(motivo)
    assert _publicacoes(transporte) == 0


def test_uri_de_upload_fora_da_lista_nunca_recebe_o_token(raiz: Path) -> None:
    transporte = TransporteGravado(*PRE, (200, _f("container_uri_estranha.json")))
    _adaptador(transporte, raiz).publicar(_pedido())
    assert all("exemplo-malicioso" not in c.url for c in transporte.chamadas)
    assert transporte.metodos() == ["GET", "GET", "POST"]


@pytest.mark.parametrize(
    ("fixture", "motivo"),
    [("status_erro.json", "container_erro"), ("status_expirado.json", "container_expirado")],
)
def test_container_em_erro_ou_expirado_e_falha_sem_publicar(
    raiz: Path, fixture: str, motivo: str
) -> None:
    transporte = TransporteGravado(*PRE, CONTAINER, UPLOAD, (200, _f(fixture)))
    r = _adaptador(transporte, raiz).publicar(_pedido())
    assert r.tipo is TipoDeResultado.FALHA_PERMANENTE
    assert r.motivo == motivo
    assert _publicacoes(transporte) == 0


def test_container_ja_publicado_na_sondagem_e_incerto(raiz: Path) -> None:
    transporte = TransporteGravado(*PRE, CONTAINER, UPLOAD, (200, _f("status_publicado.json")))
    adaptador = _adaptador(transporte, raiz)
    r = adaptador.publicar(_pedido())
    assert r.tipo is TipoDeResultado.INCERTO
    assert _publicacoes(transporte) == 0
    assert adaptador.publicar(_pedido()).tipo is TipoDeResultado.INCERTO  # sem nova rede
    assert len(transporte.chamadas) == 5


def test_sondagem_que_estoura_o_limite_e_transitoria_sem_publicar(raiz: Path) -> None:
    transporte = TransporteGravado(*PRE, CONTAINER, UPLOAD, *[EM_ANDAMENTO] * 3)
    sono = Dorminhoco()
    r = _adaptador(transporte, raiz, sono, max_tentativas_status=3, intervalo_status=2.5).publicar(
        _pedido()
    )
    assert r.tipo is TipoDeResultado.FALHA_TRANSITORIA
    assert r.motivo == "sondagem_esgotada"
    assert sum(1 for c in transporte.chamadas if "fields=status_code" in c.url) == 3
    assert sono.pausas == [2.5, 2.5]  # não dorme depois da última tentativa
    assert _publicacoes(transporte) == 0


# --------------------------------------------------------------------------- media_publish


@pytest.mark.parametrize(
    "resposta",
    [
        OSError("queda"),
        TimeoutError("lento"),
        ConnectionResetError("reset"),
        (500, b""),
        (502, b"<html>"),
        (503, b"{}"),
        (429, _f("erro_limite_taxa_4.json")),
        (400, _f("erro_midia_nao_pronta_9007.json")),
        (401, _f("erro_oauth_190.json")),
        (302, b""),
        (200, b"nao e json"),
        (200, b""),
        (200, b"[]"),
        (200, b"{}"),
        (200, b'{"id":null}'),
        (200, b'{"id":""}'),
        (200, b'{"id":17895000000000042}'),
        (200, b'{"id":"abc/../x"}'),
        (200, b'{"id":"17895000000000042","error":{"code":1}}'),
        (200, _f("publish_sem_id.json")),
        (204, b""),
    ],
)
def test_duvida_apos_media_publish_e_sempre_incerto(raiz: Path, resposta: Resposta) -> None:
    transporte = TransporteGravado(*PRE, CONTAINER, UPLOAD, FINALIZADO, resposta)
    r = _adaptador(transporte, raiz).publicar(_pedido())
    assert r.tipo is TipoDeResultado.INCERTO
    assert r.id_externo is None
    assert not r.sucesso
    assert _publicacoes(transporte) == 1


# --------------------------------------------------------------------------- idempotência


def test_repetir_apos_sucesso_devolve_o_mesmo_sem_nova_rede(raiz: Path) -> None:
    transporte = TransporteGravado(*_feliz())
    adaptador = _adaptador(transporte, raiz)
    primeiro = adaptador.publicar(_pedido())
    n = len(transporte.chamadas)
    segundo = adaptador.publicar(_pedido())
    assert segundo == primeiro
    assert len(transporte.chamadas) == n
    assert _publicacoes(transporte) == 1


def test_repetir_apos_incerto_nunca_reenvia_publish(raiz: Path) -> None:
    transporte = TransporteGravado(*PRE, CONTAINER, UPLOAD, FINALIZADO, TimeoutError("lento"))
    adaptador = _adaptador(transporte, raiz)
    assert adaptador.publicar(_pedido()).tipo is TipoDeResultado.INCERTO
    n = len(transporte.chamadas)
    for _ in range(3):
        repetido = adaptador.publicar(_pedido())
        assert repetido.tipo is TipoDeResultado.INCERTO
    assert len(transporte.chamadas) == n
    assert _publicacoes(transporte) == 1


def test_repetir_apos_sondagem_esgotada_reaproveita_o_container(raiz: Path) -> None:
    transporte = TransporteGravado(
        *PRE,
        CONTAINER,
        UPLOAD,
        *[EM_ANDAMENTO] * 2,
        *PRE,
        FINALIZADO,
        PUBLICADO_OK,
    )
    adaptador = _adaptador(transporte, raiz, max_tentativas_status=2)
    assert adaptador.publicar(_pedido()).motivo == "sondagem_esgotada"
    segundo = adaptador.publicar(_pedido())
    assert segundo.tipo is TipoDeResultado.SUCESSO
    assert _criacoes(transporte) == 1  # um único container
    assert sum(1 for c in transporte.chamadas if c.corpo == VIDEO) == 1  # um único upload
    assert _publicacoes(transporte) == 1


def test_repetir_apos_falha_no_upload_reaproveita_o_container_e_reenvia_bytes(
    raiz: Path,
) -> None:
    transporte = TransporteGravado(
        *PRE, CONTAINER, (503, b""), *PRE, UPLOAD, FINALIZADO, PUBLICADO_OK
    )
    adaptador = _adaptador(transporte, raiz)
    assert adaptador.publicar(_pedido()).tipo is TipoDeResultado.FALHA_TRANSITORIA
    assert adaptador.publicar(_pedido()).tipo is TipoDeResultado.SUCESSO
    assert _criacoes(transporte) == 1
    assert _publicacoes(transporte) == 1


def test_apos_falha_permanente_o_proximo_pedido_recomeca(raiz: Path) -> None:
    transporte = TransporteGravado(
        *PRE,
        CONTAINER,
        UPLOAD,
        (200, _f("status_erro.json")),
        *PRE,
        CONTAINER,
        UPLOAD,
        FINALIZADO,
        PUBLICADO_OK,
    )
    adaptador = _adaptador(transporte, raiz)
    assert adaptador.publicar(_pedido()).tipo is TipoDeResultado.FALHA_PERMANENTE
    assert adaptador.publicar(_pedido()).tipo is TipoDeResultado.SUCESSO
    assert _criacoes(transporte) == 2
    assert _publicacoes(transporte) == 1


def test_chaves_diferentes_publicam_separadamente(raiz: Path) -> None:
    transporte = TransporteGravado(*_feliz(), *_feliz())
    adaptador = _adaptador(transporte, raiz)
    outro = PedidoDePublicacao(_post(), "instagram", (ARTE,), CHAVE + "|2", simular=False)
    assert adaptador.publicar(_pedido()).tipo is TipoDeResultado.SUCESSO
    assert adaptador.publicar(outro).tipo is TipoDeResultado.SUCESSO
    assert _publicacoes(transporte) == 2


def test_defeito_interno_apos_publish_e_incerto(
    raiz: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transporte = TransporteGravado(*_feliz())
    adaptador = _adaptador(transporte, raiz)

    def quebra(*a: Any, **k: Any) -> NoReturn:
        raise RuntimeError("defeito")

    monkeypatch.setattr(AdaptadorInstagram, "_publicar_container", quebra)
    r = adaptador.publicar(_pedido())
    assert r.tipo is TipoDeResultado.FALHA_PERMANENTE  # antes de enviar: falha, não incerto
    assert r.motivo == "erro_interno"


# --------------------------------------------------------------------------- I-1


def _eco() -> bytes:
    return (
        f'{{"error":{{"message":"{TOKEN} {TEXTO_SECRETO} {CORPO_DE_ERRO_UNICO}","code":100}}}}'
    ).encode()


def _tudo(adaptador: AdaptadorInstagram, r: object, caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join([repr(adaptador), str(adaptador), repr(r), str(r), caplog.text])


@pytest.mark.parametrize("etapa", [*_ETAPAS, "publish"])
@pytest.mark.parametrize("modo", ["eco_4xx", "eco_5xx", "excecao"])
def test_token_e_corpo_de_erro_nunca_aparecem(
    raiz: Path, caplog: pytest.LogCaptureFixture, etapa: str, modo: str
) -> None:
    caplog.set_level(logging.DEBUG)
    prefixo = [*_ETAPAS.get(etapa, [*PRE, CONTAINER, UPLOAD, FINALIZADO])]
    falha: Resposta = {
        "eco_4xx": (400, _eco()),
        "eco_5xx": (500, _eco()),
        "excecao": OSError(f"falha {TOKEN} {CORPO_DE_ERRO_UNICO} Authorization: OAuth {TOKEN}"),
    }[modo]
    transporte = TransporteGravado(*prefixo, falha)
    adaptador = _adaptador(transporte, raiz)
    r = adaptador.publicar(_pedido())
    texto = _tudo(adaptador, r, caplog)
    for proibido in (TOKEN, TEXTO_SECRETO, CORPO_DE_ERRO_UNICO):
        assert proibido not in texto
    if etapa == "publish":
        assert r.tipo is TipoDeResultado.INCERTO


def test_eco_do_token_em_resposta_de_sucesso_nao_vaza(
    raiz: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    ecoa = f'{{"id":"{ID_CONTAINER}","uri":"https://rupload.facebook.com/x","t":"{TOKEN}"}}'
    publish = f'{{"id":"{ID_MIDIA}","t":"{TOKEN}"}}'.encode()
    transporte = TransporteGravado(*PRE, (200, ecoa.encode()), UPLOAD, FINALIZADO, (200, publish))
    adaptador = _adaptador(transporte, raiz)
    r = adaptador.publicar(_pedido())
    assert r.tipo is TipoDeResultado.SUCESSO
    assert TOKEN not in _tudo(adaptador, r, caplog)


def test_excecao_do_construtor_e_motivo_nao_vazam(raiz: Path) -> None:
    with pytest.raises(ValueError) as info:
        _adaptador(TransporteGravado(), raiz, max_tentativas_status=0)
    assert TOKEN not in "".join(traceback.format_exception(info.value)) + repr(info.value)


# --------------------------------------------------------------------------- transporte urllib


class _AbridorFalso:
    def __init__(self, resposta: Any) -> None:
        self.resposta = resposta
        self.requisicoes: list[urllib.request.Request] = []

    def open(self, req: urllib.request.Request, timeout: float = 0) -> Any:  # noqa: A003
        self.requisicoes.append(req)
        if isinstance(self.resposta, BaseException):
            raise self.resposta
        return self.resposta


class _Resposta:
    def __init__(self, status: int, corpo: bytes) -> None:
        self.status = status
        self._corpo = corpo

    def read(self, n: int = -1) -> bytes:
        return self._corpo

    def __enter__(self) -> _Resposta:
        return self

    def __exit__(self, *a: object) -> None:
        return None


def _usar(monkeypatch: pytest.MonkeyPatch, resposta: Any) -> _AbridorFalso:
    abridor = _AbridorFalso(resposta)
    monkeypatch.setattr(urllib.request, "build_opener", lambda *h: abridor)
    return abridor


def _http_error(codigo: int, corpo: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://x.invalid", codigo, "m", {}, io.BytesIO(corpo))  # type: ignore[arg-type]


def test_urllib_post_devolve_status_e_corpo_em_4xx_e_5xx(monkeypatch: pytest.MonkeyPatch) -> None:
    transporte = TransporteHttpUrllib()
    for codigo in (400, 401, 429, 500):
        abridor = _usar(monkeypatch, _http_error(codigo, b'{"error":{"code":190}}'))
        status, corpo = transporte.postar("https://x.invalid/a", {"A": "b"}, b"dados", 5.0)
        assert (status, corpo) == (codigo, b'{"error":{"code":190}}')
        req = abridor.requisicoes[0]
        assert req.get_method() == "POST"
        assert req.data == b"dados"


def test_urllib_get_e_delete_tambem_devolvem_corpo_de_erro(monkeypatch: pytest.MonkeyPatch) -> None:
    transporte = TransporteHttpUrllib()
    metodos = []
    for metodo in ("obter", "apagar"):
        abridor = _usar(monkeypatch, _http_error(404, b"nada"))
        assert getattr(transporte, metodo)("https://x.invalid/a", {}, 5.0) == (404, b"nada")
        assert abridor.requisicoes[0].data is None
        metodos.append(abridor.requisicoes[0].get_method())
    assert metodos == ["GET", "DELETE"]


def test_urllib_sucesso_e_recusa_http_e_propaga_falha_de_rede(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transporte = TransporteHttpUrllib()
    _usar(monkeypatch, _Resposta(200, b"ok"))
    assert transporte.postar("https://x.invalid/a", {}, b"", 5.0) == (200, b"ok")
    with pytest.raises(ValueError):
        transporte.postar("http://x.invalid/a", {}, b"", 5.0)
    with pytest.raises(ValueError):
        transporte.apagar("http://x.invalid/a", {}, 5.0)
    _usar(monkeypatch, TimeoutError("lento"))
    with pytest.raises(TimeoutError):
        transporte.postar("https://x.invalid/a", {}, b"", 5.0)


def test_urllib_nao_segue_redirecionamento() -> None:
    from publicador.artes.armazem import _SemRedirecionamento

    manipulador = _SemRedirecionamento()
    assert manipulador.redirect_request(None, None, 302, "m", None, "https://outro.invalid") is None
