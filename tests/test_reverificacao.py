"""Testes da reverificação do selo (publicador.core.reverificacao) - RF-26, T023, I-2.

Toda edição de campo publicável DEPOIS da confirmação devolve o post a ``pronto`` (um único
PATCH que também limpa o selo e o "Confirmado em"). Falha segura: na dúvida o post não segue
autorizado; mas armazém indisponível NÃO desfaz confirmação (resultado indeterminado).

Sem rede: um transporte falso com estado simula o Notion. Valores fictícios (I-1).
"""

from __future__ import annotations

import copy
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from publicador.artes.armazem import ArmazemLocal, ErroAcessoArmazem
from publicador.core.confirmacao import confirmar_post
from publicador.core.modelos import Estagio, PapelEstagio, Post
from publicador.core.redacao import SegredoStr
from publicador.core.reverificacao import (
    ConfiguracaoSemProntoUnico,
    MotivoReverificacao,
    ResultadoReverificacao,
    SituacaoReverificacao,
    reverificar_post,
)
from publicador.notion.bancos import Configuracao
from publicador.notion.cliente import ClienteNotion, RespostaHttp
from publicador.notion.escrita import EdicaoConcorrente
from publicador.notion.mapeamento import ler_post

TOKEN = "TOKEN_FALSO_reverif_123XYZ"
PAGINA = "0123456789abcdef0123456789abcdef"
ID_PAUTA = "a1" * 16
ID_PRONTO = "a2" * 16
ID_CONFIRMADO = "a3" * 16
ID_PUBLICADO = "a4" * 16
ID_CANCELADO = "a6" * 16
ID_FEED = "c1" * 16
ID_REELS = "c2" * 16
EDITADO = "2026-11-07T12:00:00.000Z"
TITULO = "Quanto custa a energia?"
TEXTO_CORPO = "Texto do roteiro ficticio."
ARTE_1 = "serie-a/2026/11/arte_01.png"
ARTE_2 = "serie-a/2026/11/arte_02.png"
ARTE_3 = "serie-a/2026/11/arte_03.png"

AGORA = datetime(2026, 11, 7, 15, 0, tzinfo=UTC)

ESTAGIOS: dict[str, Estagio] = {
    ID_PAUTA: Estagio("pauta", PapelEstagio.PAUTA),
    ID_PRONTO: Estagio("pronto", PapelEstagio.PRONTO),
    ID_CONFIRMADO: Estagio("confirmado", PapelEstagio.AUTORIZADO),
    ID_PUBLICADO: Estagio("publicado", PapelEstagio.PUBLICADO),
    ID_CANCELADO: Estagio("cancelado", PapelEstagio.CANCELADO),
}


def _config(estagios: Mapping[str, Estagio] | None = None) -> Configuracao:
    return Configuracao(
        estagios=dict(ESTAGIOS if estagios is None else estagios),
        formatos={ID_FEED: "Feed", ID_REELS: "Reels"},
        series={},
        temas={},
        problemas=(),
    )


# ------------------------------------------------------------------ Notion falso


def _rich(texto: str | None) -> dict[str, Any]:
    if not texto:
        return {"type": "rich_text", "rich_text": []}
    return {"type": "rich_text", "rich_text": [{"type": "text", "plain_text": texto}]}


def _paragrafo(texto: str) -> dict[str, Any]:
    return {"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": texto}]}}


def _titulo_bloco(texto: str) -> dict[str, Any]:
    return {"type": "heading_2", "heading_2": {"rich_text": [{"plain_text": texto}]}}


def _propriedades_iniciais() -> dict[str, Any]:
    return {
        "Título": {"type": "title", "title": [{"type": "text", "plain_text": TITULO}]},
        "Estágio": {"type": "relation", "relation": [{"id": ID_PRONTO}]},
        "Plataformas": {
            "type": "multi_select",
            "multi_select": [{"name": "Instagram"}, {"name": "TikTok"}],
        },
        "Formato": {"type": "relation", "relation": [{"id": ID_FEED}]},
        "Data e hora": {"type": "date", "date": {"start": "2026-11-20T18:30:00-03:00"}},
        "Artes": _rich(f"{ARTE_1}\n{ARTE_2}"),
        "Selo de confirmação": _rich(None),
        "Confirmado em": {"type": "date", "date": None},
    }


def _normalizar(propriedade: Any) -> Any:
    """Faz o papel do Notion: devolve plain_text nos itens de texto gravados."""
    if isinstance(propriedade, dict):
        for chave in ("rich_text", "title"):
            if chave in propriedade:
                propriedade[chave] = [
                    {"type": "text", "plain_text": i["text"]["content"]} for i in propriedade[chave]
                ]
    return propriedade


@dataclass
class NotionFalso:
    """Transporte com estado: página, blocos e PATCH de propriedades."""

    propriedades: dict[str, Any]
    blocos: list[dict[str, Any]]
    editado: str = EDITADO
    minuto: int = 0
    editar_na_leitura: bool = False  # simula edição externa antes do GET de conferência
    chamadas: list[tuple[str, str, dict[str, Any] | None]] = field(default_factory=list)

    def _avancar(self) -> None:
        self.minuto += 1
        self.editado = f"2026-11-07T13:{self.minuto:02d}:00.000Z"

    def editar(self, nome: str, valor: dict[str, Any]) -> None:
        """Edição feita por uma pessoa direto no Notion (muda last_edited_time)."""
        self.propriedades[nome] = valor
        self._avancar()

    def editar_blocos(self, blocos: list[dict[str, Any]]) -> None:
        self.blocos = blocos
        self._avancar()

    def zerar_chamadas(self) -> None:
        self.chamadas.clear()

    @property
    def patches(self) -> list[dict[str, Any]]:
        return [c[2] or {} for c in self.chamadas if c[0] == "PATCH"]

    @property
    def escritas(self) -> list[tuple[str, str]]:
        return [(m, p) for m, p, _ in self.chamadas if m != "GET"]

    def enviar(
        self,
        metodo: str,
        url: str,
        cabecalhos: Mapping[str, str],
        corpo: bytes | None,
        timeout: float,
    ) -> RespostaHttp:
        caminho = re.sub(r"^https://api\.notion\.com/v1", "", url).split("?")[0]
        dados = json.loads(corpo) if corpo else None
        self.chamadas.append((metodo, caminho, dados))
        if caminho == f"/pages/{PAGINA}" and metodo == "GET":
            if self.editar_na_leitura:
                self.editar_na_leitura = False
                self._avancar()
            return self._resposta(self._pagina())
        if caminho == f"/pages/{PAGINA}" and metodo == "PATCH":
            assert dados is not None
            for nome, valor in dados["properties"].items():
                self.propriedades[nome] = _normalizar(copy.deepcopy(valor))
            self._avancar()
            return self._resposta(self._pagina())
        if caminho == f"/blocks/{PAGINA}/children" and metodo == "GET":
            return self._resposta({"results": copy.deepcopy(self.blocos), "has_more": False})
        raise AssertionError(f"chamada inesperada: {metodo} {caminho}")

    def _pagina(self) -> dict[str, Any]:
        return {
            "id": PAGINA,
            "last_edited_time": self.editado,
            "properties": copy.deepcopy(self.propriedades),
        }

    @staticmethod
    def _resposta(dados: object) -> RespostaHttp:
        return RespostaHttp(200, {}, json.dumps(dados).encode())


@pytest.fixture
def raiz(tmp_path: Path) -> Path:
    base = tmp_path / "artes"
    for caminho, conteudo in (
        (ARTE_1, b"\x89PNG ficticio 01"),
        (ARTE_2, b"\x89PNG ficticio 02"),
        (ARTE_3, b"\x89PNG ficticio 03"),
    ):
        destino = base / caminho
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(conteudo)
    return base


@pytest.fixture
def armazem(raiz: Path) -> ArmazemLocal:
    return ArmazemLocal(raiz)


def _ler(cliente: ClienteNotion, config: Configuracao | None = None) -> Post:
    return ler_post(cliente, PAGINA, _config() if config is None else config)


def _confirmado(armazem: ArmazemLocal) -> tuple[ClienteNotion, NotionFalso]:
    """Post confirmado de verdade (via confirmar_post), com selo válido gravado."""
    notion = NotionFalso(_propriedades_iniciais(), [_paragrafo(TEXTO_CORPO)])
    cliente = ClienteNotion(SegredoStr(TOKEN), transporte=notion, dormir=lambda _: None)
    novo = confirmar_post(cliente, _ler(cliente), _config(), armazem, AGORA)
    assert novo.estagio.papel is PapelEstagio.AUTORIZADO and novo.selo
    notion.zerar_chamadas()
    return cliente, notion


def _assert_devolvido(
    resultado: ResultadoReverificacao, notion: NotionFalso, cliente: ClienteNotion
) -> None:
    assert resultado.situacao is SituacaoReverificacao.DEVOLVIDO_A_PRONTO
    assert notion.escritas == [("PATCH", f"/pages/{PAGINA}")]  # exatamente UM PATCH
    (props,) = notion.patches
    assert props["properties"] == {
        "Estágio": {"relation": [{"id": ID_PRONTO}]},
        "Selo de confirmação": {"rich_text": []},
        "Confirmado em": {"date": None},
    }
    relido = _ler(cliente)
    assert relido.estagio.papel is PapelEstagio.PRONTO
    assert relido.selo is None and relido.confirmado_em is None
    assert resultado.post.estagio.papel is PapelEstagio.PRONTO


# ------------------------------------------------------------------ cada edição devolve a pronto


def _editar_corpo(notion: NotionFalso, raiz: Path) -> None:
    notion.editar_blocos([_paragrafo(TEXTO_CORPO + " Agora com outra frase.")])


def _editar_plataforma(notion: NotionFalso, raiz: Path) -> None:
    notion.editar("Plataformas", {"type": "multi_select", "multi_select": [{"name": "Instagram"}]})


def _editar_titulo(notion: NotionFalso, raiz: Path) -> None:
    notion.editar(
        "Título", {"type": "title", "title": [{"type": "text", "plain_text": "Outro título"}]}
    )


def _editar_formato(notion: NotionFalso, raiz: Path) -> None:
    notion.editar("Formato", {"type": "relation", "relation": [{"id": ID_REELS}]})


def _editar_data(notion: NotionFalso, raiz: Path) -> None:
    notion.editar("Data e hora", {"type": "date", "date": {"start": "2026-11-21T18:30:00-03:00"}})


def _editar_hora(notion: NotionFalso, raiz: Path) -> None:
    notion.editar("Data e hora", {"type": "date", "date": {"start": "2026-11-20T18:31:00-03:00"}})


def _editar_secao(notion: NotionFalso, raiz: Path) -> None:
    notion.editar_blocos(
        [_paragrafo(TEXTO_CORPO), _titulo_bloco("Instagram"), _paragrafo("Legenda só do IG.")]
    )


def _editar_caminho_arte(notion: NotionFalso, raiz: Path) -> None:
    notion.editar("Artes", _rich(f"{ARTE_1}\n{ARTE_3}"))


def _editar_conteudo_arte(notion: NotionFalso, raiz: Path) -> None:
    (raiz / ARTE_2).write_bytes(b"\x89PNG ficticio 02 trocado")  # nada muda no Notion


def _editar_ordem_artes(notion: NotionFalso, raiz: Path) -> None:
    notion.editar("Artes", _rich(f"{ARTE_2}\n{ARTE_1}"))


@pytest.mark.parametrize(
    ("edicao", "motivo"),
    [
        (_editar_corpo, MotivoReverificacao.SELO_DIVERGENTE),
        (_editar_plataforma, MotivoReverificacao.SELO_DIVERGENTE),
        (_editar_titulo, MotivoReverificacao.SELO_DIVERGENTE),
        (_editar_formato, MotivoReverificacao.SELO_DIVERGENTE),
        (_editar_data, MotivoReverificacao.SELO_DIVERGENTE),
        (_editar_hora, MotivoReverificacao.SELO_DIVERGENTE),
        (_editar_secao, MotivoReverificacao.SELO_DIVERGENTE),
        (_editar_caminho_arte, MotivoReverificacao.SELO_DIVERGENTE),
        (_editar_conteudo_arte, MotivoReverificacao.SELO_DIVERGENTE),
        (_editar_ordem_artes, MotivoReverificacao.SELO_DIVERGENTE),
    ],
)
def test_toda_edicao_depois_de_confirmar_devolve_a_pronto(
    edicao: Any, motivo: MotivoReverificacao, raiz: Path, armazem: ArmazemLocal
) -> None:
    cliente, notion = _confirmado(armazem)
    edicao(notion, raiz)
    post = _ler(cliente)
    notion.zerar_chamadas()

    resultado = reverificar_post(cliente, post, _config(), armazem)

    assert resultado.motivo is motivo
    _assert_devolvido(resultado, notion, cliente)


# ------------------------------------------------------------- sem mudança / fora de autorizado


def test_post_inalterado_e_mantido_sem_escrita(armazem: ArmazemLocal) -> None:
    cliente, notion = _confirmado(armazem)
    post = _ler(cliente)
    notion.zerar_chamadas()
    resultado = reverificar_post(cliente, post, _config(), armazem)
    assert resultado.situacao is SituacaoReverificacao.MANTIDO
    assert resultado.motivo is None
    assert resultado.post == post
    assert notion.escritas == []


@pytest.mark.parametrize("estagio_id", [ID_PAUTA, ID_PRONTO, ID_PUBLICADO, ID_CANCELADO])
def test_post_nao_autorizado_e_ignorado_sem_escrita(estagio_id: str, armazem: ArmazemLocal) -> None:
    notion = NotionFalso(_propriedades_iniciais(), [_paragrafo(TEXTO_CORPO)])
    notion.propriedades["Estágio"] = {"type": "relation", "relation": [{"id": estagio_id}]}
    notion.propriedades["Selo de confirmação"] = _rich("lixo")  # nem selo residual dispara
    cliente = ClienteNotion(SegredoStr(TOKEN), transporte=notion, dormir=lambda _: None)
    post = _ler(cliente)
    notion.zerar_chamadas()
    resultado = reverificar_post(cliente, post, _config(), armazem)
    assert resultado.situacao is SituacaoReverificacao.IGNORADO_NAO_AUTORIZADO
    assert notion.chamadas == []  # nem leitura


def test_post_nao_autorizado_e_ignorado_mesmo_com_configuracao_sem_pronto(
    armazem: ArmazemLocal,
) -> None:
    notion = NotionFalso(_propriedades_iniciais(), [_paragrafo(TEXTO_CORPO)])
    cliente = ClienteNotion(SegredoStr(TOKEN), transporte=notion, dormir=lambda _: None)
    post = _ler(cliente)
    notion.zerar_chamadas()
    config = _config({k: v for k, v in ESTAGIOS.items() if k != ID_PRONTO})
    resultado = reverificar_post(cliente, post, config, armazem)
    assert resultado.situacao is SituacaoReverificacao.IGNORADO_NAO_AUTORIZADO
    assert notion.escritas == []


# ------------------------------------------------------------------ selo ausente / inválido


def _sem_selo(notion: NotionFalso) -> None:
    notion.editar("Selo de confirmação", _rich(None))


def test_selo_ausente_com_devolver_vai_a_pronto(armazem: ArmazemLocal) -> None:
    cliente, notion = _confirmado(armazem)
    _sem_selo(notion)
    post = _ler(cliente)
    notion.zerar_chamadas()
    resultado = reverificar_post(cliente, post, _config(), armazem, tratar_selo_ausente="devolver")
    assert resultado.motivo is MotivoReverificacao.SELO_AUSENTE
    _assert_devolvido(resultado, notion, cliente)


def test_selo_ausente_e_devolvido_por_padrao(armazem: ArmazemLocal) -> None:
    cliente, notion = _confirmado(armazem)
    _sem_selo(notion)
    post = _ler(cliente)
    notion.zerar_chamadas()
    resultado = reverificar_post(cliente, post, _config(), armazem)
    assert resultado.motivo is MotivoReverificacao.SELO_AUSENTE
    _assert_devolvido(resultado, notion, cliente)


def test_selo_ausente_com_ignorar_e_mantido_sem_escrita(armazem: ArmazemLocal) -> None:
    cliente, notion = _confirmado(armazem)
    _sem_selo(notion)
    post = _ler(cliente)
    notion.zerar_chamadas()
    resultado = reverificar_post(cliente, post, _config(), armazem, tratar_selo_ausente="ignorar")
    assert resultado.situacao is SituacaoReverificacao.MANTIDO
    assert resultado.motivo is MotivoReverificacao.SELO_AUSENTE  # não é selo conferido
    assert notion.escritas == []


def test_tratar_selo_ausente_invalido_e_recusado_sem_escrita(armazem: ArmazemLocal) -> None:
    cliente, notion = _confirmado(armazem)
    post = _ler(cliente)
    notion.zerar_chamadas()
    with pytest.raises(ValueError, match="tratar_selo_ausente"):
        reverificar_post(cliente, post, _config(), armazem, tratar_selo_ausente="talvez")  # type: ignore[arg-type]
    assert notion.chamadas == []


@pytest.mark.parametrize(
    "selo",
    ["lixo", "v2:" + "a" * 64, "v1:" + "A" * 64, "v1:" + "a" * 63, "v1:" + "g" * 64],
)
def test_selo_de_versao_desconhecida_ou_malformado_devolve(
    selo: str, armazem: ArmazemLocal
) -> None:
    cliente, notion = _confirmado(armazem)
    notion.editar("Selo de confirmação", _rich(selo))
    post = _ler(cliente)
    notion.zerar_chamadas()
    resultado = reverificar_post(cliente, post, _config(), armazem, tratar_selo_ausente="ignorar")
    assert resultado.motivo is MotivoReverificacao.SELO_INVALIDO
    _assert_devolvido(resultado, notion, cliente)


def test_selo_v1_de_outro_conteudo_devolve_como_divergente(armazem: ArmazemLocal) -> None:
    cliente, notion = _confirmado(armazem)
    notion.editar("Selo de confirmação", _rich("v1:" + "0" * 64))
    post = _ler(cliente)
    notion.zerar_chamadas()
    resultado = reverificar_post(cliente, post, _config(), armazem)
    assert resultado.motivo is MotivoReverificacao.SELO_DIVERGENTE
    _assert_devolvido(resultado, notion, cliente)


# ------------------------------------------------------------------ recálculo impossível


@pytest.mark.parametrize(
    "data", [None, {"start": "2026-11-20"}], ids=["sem_data", "so_data_sem_hora"]
)
def test_sem_data_e_hora_devolve(data: Any, armazem: ArmazemLocal) -> None:
    cliente, notion = _confirmado(armazem)
    notion.editar("Data e hora", {"type": "date", "date": data})
    post = _ler(cliente)
    notion.zerar_chamadas()
    resultado = reverificar_post(cliente, post, _config(), armazem)
    assert resultado.motivo is MotivoReverificacao.SEM_DATA_HORA
    _assert_devolvido(resultado, notion, cliente)


def test_sem_plataformas_devolve_como_selo_impossivel(armazem: ArmazemLocal) -> None:
    cliente, notion = _confirmado(armazem)
    notion.editar("Plataformas", {"type": "multi_select", "multi_select": []})
    post = _ler(cliente)
    notion.zerar_chamadas()
    resultado = reverificar_post(cliente, post, _config(), armazem)
    assert resultado.motivo is MotivoReverificacao.SELO_IMPOSSIVEL
    _assert_devolvido(resultado, notion, cliente)


def test_arte_removida_do_armazem_devolve(raiz: Path, armazem: ArmazemLocal) -> None:
    cliente, notion = _confirmado(armazem)
    (raiz / ARTE_2).unlink()
    post = _ler(cliente)
    notion.zerar_chamadas()
    resultado = reverificar_post(cliente, post, _config(), armazem)
    assert resultado.motivo is MotivoReverificacao.ARTE_INVALIDA
    _assert_devolvido(resultado, notion, cliente)


@pytest.mark.parametrize(
    "artes", [f"../fora.png\n{ARTE_1}", f"youtube:{ARTE_1}"], ids=["caminho", "variacao"]
)
def test_referencia_de_arte_invalida_devolve(artes: str, armazem: ArmazemLocal) -> None:
    cliente, notion = _confirmado(armazem)
    notion.editar("Artes", _rich(artes))
    post = _ler(cliente)
    notion.zerar_chamadas()
    resultado = reverificar_post(cliente, post, _config(), armazem)
    assert resultado.motivo is MotivoReverificacao.ARTE_INVALIDA
    _assert_devolvido(resultado, notion, cliente)


# ------------------------------------------------------------------ armazém indisponível


class ArmazemForaDoAr:
    """Simula o armazém (GitHub) instável: toda leitura falha com ErroAcessoArmazem."""

    def ler(self, caminho: str) -> bytes:
        raise ErroAcessoArmazem("Falha de comunicacao com o armazem de artes (detalhes omitidos).")

    def hash_sha256(self, caminho: str) -> str:
        raise ErroAcessoArmazem("Falha de comunicacao com o armazem de artes (detalhes omitidos).")

    def existe(self, caminho: str) -> bool:
        raise ErroAcessoArmazem("Falha de comunicacao com o armazem de artes (detalhes omitidos).")


def test_armazem_indisponivel_nao_devolve_e_e_indeterminado(armazem: ArmazemLocal) -> None:
    cliente, notion = _confirmado(armazem)
    post = _ler(cliente)
    notion.zerar_chamadas()
    resultado = reverificar_post(cliente, post, _config(), ArmazemForaDoAr())
    assert resultado.situacao is SituacaoReverificacao.INDETERMINADO
    assert resultado.motivo is MotivoReverificacao.ARMAZEM_INDISPONIVEL
    assert notion.escritas == []
    assert _ler(cliente).estagio.papel is PapelEstagio.AUTORIZADO


# ------------------------------------------------------------------ configuração


def _sem_pronto() -> Configuracao:
    return _config({k: v for k, v in ESTAGIOS.items() if k != ID_PRONTO})


def _dois_prontos() -> Configuracao:
    estagios = dict(ESTAGIOS)
    estagios["a7" * 16] = Estagio("revisado", PapelEstagio.PRONTO)
    return _config(estagios)


@pytest.mark.parametrize("fabrica", [_sem_pronto, _dois_prontos])
def test_configuracao_sem_pronto_unico_levanta_e_nao_escreve(
    fabrica: Any, armazem: ArmazemLocal
) -> None:
    cliente, notion = _confirmado(armazem)
    notion.editar("Artes", _rich(f"{ARTE_2}\n{ARTE_1}"))  # selo diverge
    post = _ler(cliente)
    notion.zerar_chamadas()
    with pytest.raises(ConfiguracaoSemProntoUnico):
        reverificar_post(cliente, post, fabrica(), armazem)
    assert notion.escritas == []


# ------------------------------------------------------------------ concorrência


def test_edicao_concorrente_propaga_e_nao_grava(armazem: ArmazemLocal) -> None:
    cliente, notion = _confirmado(armazem)
    notion.editar("Artes", _rich(f"{ARTE_2}\n{ARTE_1}"))
    post = _ler(cliente)
    antes = copy.deepcopy(notion.propriedades)
    notion.zerar_chamadas()
    notion.editar_na_leitura = True  # alguém edita entre a leitura e a devolução
    with pytest.raises(EdicaoConcorrente):
        reverificar_post(cliente, post, _config(), armazem)
    assert notion.escritas == []
    assert notion.propriedades == antes


# ------------------------------------------------------------------ I-1: nada sensível


def test_mensagens_e_logs_sem_token_nem_conteudo_do_post(
    raiz: Path, armazem: ArmazemLocal, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    textos: list[str] = []

    cliente, notion = _confirmado(armazem)
    _editar_corpo(notion, raiz)
    r1 = reverificar_post(cliente, _ler(cliente), _config(), armazem)
    textos += [repr(r1), str(r1)]

    cliente2, _ = _confirmado(armazem)
    r2 = reverificar_post(cliente2, _ler(cliente2), _config(), ArmazemForaDoAr())
    textos += [repr(r2), str(r2)]

    cliente3, notion3 = _confirmado(armazem)
    notion3.editar("Artes", _rich(f"{ARTE_2}\n{ARTE_1}"))
    with pytest.raises(ConfiguracaoSemProntoUnico) as erro:
        reverificar_post(cliente3, _ler(cliente3), _sem_pronto(), armazem)
    textos += [str(erro.value), repr(erro.value)]

    textos += [r.getMessage() for r in caplog.records]
    assert textos
    for proibido in (TOKEN, TITULO, TEXTO_CORPO, ARTE_1, ARTE_2, "v1:"):
        assert all(proibido not in t for t in textos), proibido
