"""Testes do orquestrador do publicador (publicador.job.orquestrador) - T038.

RF-10 a RF-13, RF-17, RF-21, RF-37, RF-40, A-010, A-021, I-1, I-2. Sem rede: um transporte
falso com estado simula os bancos Posts e Execuções do Notion (consulta paginada com filtros,
GET/PATCH/POST de página e blocos). Artes em ``ArmazemLocal``; plataformas com
``AdaptadorFalso``. Relógio injetado. Todos os valores são fictícios (I-1).
"""

from __future__ import annotations

import copy
import json
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from publicador.adaptadores.base import PedidoDePublicacao, ResultadoPublicacao, TipoDeResultado
from publicador.adaptadores.falso import AdaptadorFalso
from publicador.artes.armazem import ArmazemLocal, ErroAcessoArmazem
from publicador.core.config import Parametros
from publicador.core.modelos import Estagio, PapelEstagio, Serie
from publicador.core.redacao import SegredoStr
from publicador.core.selo import calcular_selo
from publicador.job.orquestrador import (
    VARIAVEL_MODO_REAL,
    Desfecho,
    FalhaGeral,
    Registro,
    RelatorioExecucao,
    executar,
    modo_real_ligado,
)
from publicador.notion.bancos import Configuracao, IdsBancos
from publicador.notion.cliente import ClienteNotion, RespostaHttp
from publicador.notion.mapeamento import ler_post

TOKEN = "TOKEN_FALSO_orquestrador_000XYZ"
TOKEN_REDE = "TOKEN_FALSO_rede_social_999QQQ"
BANCO_POSTS = "d0" * 16
BANCO_EXEC = "e882e4987273430b8fe7002ce2811fa4"
ID_PAUTA = "a1" * 16
ID_PRONTO = "a2" * 16
ID_CONFIRMADO = "a3" * 16
ID_PUBLICADO = "a4" * 16
ID_FALHA = "a5" * 16
ID_CANCELADO = "a6" * 16
ID_FEED = "f1" * 16
ID_SERIE = "c1" * 16

EDITADO = "2026-11-07T11:00:00.000Z"
DATA = "2026-11-07T09:00:00-03:00"  # 12:00 UTC
AGORA = datetime(2026, 11, 7, 12, 10, tzinfo=UTC)
TITULO = "Titulo secreto do post XPTO"
TEXTO = "Corpo secreto do roteiro ZZZ."
ARTE = "serie-a/2026/11/arte_01.png"

ESTAGIOS = {
    ID_PAUTA: Estagio("pauta", PapelEstagio.PAUTA),
    ID_PRONTO: Estagio("pronto", PapelEstagio.PRONTO),
    ID_CONFIRMADO: Estagio("confirmado", PapelEstagio.AUTORIZADO),
    ID_PUBLICADO: Estagio("publicado", PapelEstagio.PUBLICADO),
    ID_FALHA: Estagio("falha na publicação", PapelEstagio.FALHA),
    ID_CANCELADO: Estagio("cancelado", PapelEstagio.CANCELADO),
}
IDS = IdsBancos(
    estagios="b1" * 16,
    formatos="b2" * 16,
    series="b3" * 16,
    temas="b4" * 16,
    posts=BANCO_POSTS,
    execucoes=BANCO_EXEC,
)
PARAMETROS = Parametros(
    janela_tolerancia_minutos=120,
    nova_tentativa_minutos=30,
    meta_pontualidade_minutos=30,
    fuso_padrao="America/Sao_Paulo",
    limite_silencio_vigia_minutos=60,
)
TIPOS = {"Feed": {"Instagram": "imagem", "TikTok": "fotos", "Substack": "artigo"}}


def _config(**mudancas: Any) -> Configuracao:
    base: dict[str, Any] = {
        "estagios": dict(ESTAGIOS),
        "formatos": {ID_FEED: "Feed"},
        "series": {ID_SERIE: Serie("Série teste", True)},
        "temas": {},
        "problemas": (),
    }
    base.update(mudancas)
    return Configuracao(**base)


def _pid(n: int) -> str:
    return f"{n:032x}"


# ------------------------------------------------------------------ Notion falso


def _rich(texto: str | None) -> dict[str, Any]:
    if not texto:
        return {"type": "rich_text", "rich_text": []}
    return {"type": "rich_text", "rich_text": [{"type": "text", "plain_text": texto}]}


def _props(
    estagio_id: str = ID_CONFIRMADO,
    data: str | None = DATA,
    plataformas: tuple[str, ...] = ("Instagram",),
    titulo: str = TITULO,
    serie: str | None = None,
    ordem: int | None = None,
) -> dict[str, Any]:
    return {
        "Título": {"type": "title", "title": [{"type": "text", "plain_text": titulo}]},
        "Estágio": {"type": "relation", "relation": [{"id": estagio_id}]},
        "Formato": {"type": "relation", "relation": [{"id": ID_FEED}]},
        "Plataformas": {
            "type": "multi_select",
            "multi_select": [{"name": p} for p in plataformas],
        },
        "Data e hora": {"type": "date", "date": None if data is None else {"start": data}},
        "Série": {"type": "relation", "relation": [] if serie is None else [{"id": serie}]},
        "Ordem na série": {"type": "number", "number": ordem},
        "Artes": _rich(ARTE),
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


def _resposta(dados: object, status: int = 200) -> RespostaHttp:
    return RespostaHttp(status, {}, json.dumps(dados).encode())


@dataclass
class PaginaFalsa:
    propriedades: dict[str, Any]
    editado: str = EDITADO


@dataclass
class NotionFalso:
    """Bancos Posts e Execuções falsos, com estado; ``eventos`` é compartilhado com os
    adaptadores espiões para conferir a ORDEM das chamadas."""

    posts: dict[str, PaginaFalsa] = field(default_factory=dict)
    execucoes: list[dict[str, Any]] = field(default_factory=list)
    # Visão desatualizada devolvida SÓ pela listagem (o post mudou depois de listado).
    lista_desatualizada: dict[str, dict[str, Any]] = field(default_factory=dict)
    falhar_leitura: set[str] = field(default_factory=set)
    falhas_na_criacao: int = 0
    falhar_patch_execucao: bool = False
    eventos: list[tuple[str, ...]] = field(default_factory=list)
    corpos: list[str] = field(default_factory=list)

    def enviar(
        self,
        metodo: str,
        url: str,
        cabecalhos: Mapping[str, str],
        corpo: bytes | None,
        timeout: float,
    ) -> RespostaHttp:
        caminho = re.sub(r"^https://api\.notion\.com/v1", "", url).split("?")[0]
        dados = json.loads(corpo) if corpo else {}
        if corpo:
            self.corpos.append(corpo.decode("utf-8"))
        self.eventos.append(("notion", metodo, caminho))
        if metodo == "POST" and caminho == f"/databases/{BANCO_POSTS}/query":
            return self._consultar_posts(dados)
        if metodo == "POST" and caminho == f"/databases/{BANCO_EXEC}/query":
            return self._consultar_execucoes(dados)
        if metodo == "POST" and caminho == "/pages":
            return self._criar(dados)
        m = re.fullmatch(r"/pages/([0-9a-f]{32})", caminho)
        if m:
            return self._pagina(metodo, m.group(1), dados)
        m = re.fullmatch(r"/blocks/([0-9a-f]{32})/children", caminho)
        if m and m.group(1) in self.posts and metodo == "GET":
            bloco = {
                "id": "b" * 32,
                "type": "paragraph",
                "paragraph": {"rich_text": [{"plain_text": TEXTO}]},
            }
            return _resposta({"results": [bloco], "has_more": False})
        raise AssertionError(f"chamada inesperada: {metodo} {caminho}")

    # -- posts
    def _visao(self, pid: str, *, da_lista: bool) -> dict[str, Any]:
        pagina = self.posts[pid]
        props = copy.deepcopy(pagina.propriedades)
        if da_lista and pid in self.lista_desatualizada:
            props.update(copy.deepcopy(self.lista_desatualizada[pid]))
        return {
            "object": "page",
            "id": pid,
            "last_edited_time": pagina.editado,
            "properties": props,
        }

    def _consultar_posts(self, dados: dict[str, Any]) -> RespostaHttp:
        visoes = [self._visao(i, da_lista=True) for i in self.posts]
        filtro = dados.get("filter")
        if filtro is not None:
            assert filtro["property"] == "Estágio"
            alvo = filtro["relation"]["contains"].replace("-", "")
            visoes = [
                v
                for v in visoes
                if any(r["id"] == alvo for r in v["properties"]["Estágio"]["relation"])
            ]
        return self._paginar(visoes, dados, tamanho=2)

    def _pagina(self, metodo: str, pid: str, dados: dict[str, Any]) -> RespostaHttp:
        if pid in self.posts:
            if pid in self.falhar_leitura:
                return _resposta({"object": "error", "code": "validation_error"}, 400)
            pagina = self.posts[pid]
            if metodo == "PATCH":
                for nome, valor in dados["properties"].items():
                    pagina.propriedades[nome] = _normalizar(copy.deepcopy(valor))
                pagina.editado = "2026-11-07T12:30:00.000Z"
            return _resposta(self._visao(pid, da_lista=False))
        for registro in self.execucoes:
            if registro["id"] == pid and metodo == "PATCH":
                if self.falhar_patch_execucao:
                    return _resposta({"object": "error", "code": "validation_error"}, 400)
                registro["properties"].update(copy.deepcopy(dados["properties"]))
                return _resposta(registro)
        raise AssertionError(f"página inesperada: {metodo} {pid}")

    # -- execuções
    def _criar(self, dados: dict[str, Any]) -> RespostaHttp:
        assert dados["parent"] == {"database_id": BANCO_EXEC}
        if self.falhas_na_criacao:
            self.falhas_na_criacao -= 1
            return _resposta({"object": "error", "code": "internal_server_error"}, 500)
        novo = {
            "id": f"{0xE0000 + len(self.execucoes):032x}",
            "properties": copy.deepcopy(dados["properties"]),
        }
        self.execucoes.append(novo)
        return _resposta(novo)

    def _consultar_execucoes(self, dados: dict[str, Any]) -> RespostaHttp:
        achadas = [r for r in self.execucoes if _casa(r, dados.get("filter"))]
        return self._paginar(achadas, dados, tamanho=100)

    @staticmethod
    def _paginar(itens: list[dict[str, Any]], dados: dict[str, Any], tamanho: int) -> RespostaHttp:
        inicio = int(dados.get("start_cursor") or 0)
        fim = inicio + min(int(dados["page_size"]), tamanho)
        mais = fim < len(itens)
        return _resposta(
            {
                "results": itens[inicio:fim],
                "has_more": mais,
                "next_cursor": str(fim) if mais else None,
            }
        )

    # -- consultas dos testes
    def registros(self, pid: str | None = None) -> list[dict[str, str | None]]:
        """Execuções (sem o sinal de vida) resumidas: plataforma, resultado e motivo."""
        saida: list[dict[str, str | None]] = []
        for r in self.execucoes:
            p = r["properties"]
            resultado = (p.get("Resultado") or {}).get("select")
            nome = resultado["name"] if resultado else None
            if nome == "sinal de vida":
                continue
            if pid is not None and p["Post"]["relation"][0]["id"] != pid:
                continue
            motivo = "".join(
                t["text"]["content"] for t in (p.get("Código do motivo") or {}).get("rich_text", [])
            )
            saida.append(
                {
                    "plataforma": p["Plataforma"]["select"]["name"],
                    "resultado": nome,
                    "motivo": motivo or None,
                }
            )
        return saida

    def sinais(self) -> list[dict[str, Any]]:
        return [
            r
            for r in self.execucoes
            if ((r["properties"].get("Resultado") or {}).get("select") or {}).get("name")
            == "sinal de vida"
        ]

    def estagio(self, pid: str) -> str:
        return str(self.posts[pid].propriedades["Estágio"]["relation"][0]["id"])


def _casa(pagina: dict[str, Any], filtro: dict[str, Any] | None) -> bool:
    if filtro is None:
        return True
    if "and" in filtro:
        return all(_casa(pagina, f) for f in filtro["and"])
    p = pagina["properties"].get(filtro["property"])
    if p is None:
        return False
    if "title" in filtro:
        return "".join(t["text"]["content"] for t in p["title"]) == filtro["title"]["equals"]
    if "select" in filtro:
        return bool((p["select"] or {}).get("name") == filtro["select"]["equals"])
    if "relation" in filtro:
        return any(r["id"] == filtro["relation"]["contains"] for r in p["relation"])
    raise AssertionError("filtro não suportado pelo Notion falso")


def _cliente(notion: NotionFalso) -> ClienteNotion:
    return ClienteNotion(SegredoStr(TOKEN), transporte=notion, dormir=lambda _: None)


def _selar(notion: NotionFalso, pid: str, armazem: ArmazemLocal) -> str:
    """Grava um selo válido (como se a tela tivesse confirmado), sem registrar eventos."""
    post = ler_post(_cliente(notion), pid, _config())
    selo = calcular_selo(post, armazem)
    notion.posts[pid].propriedades["Selo de confirmação"] = _rich(selo)
    notion.posts[pid].propriedades["Confirmado em"] = {
        "type": "date",
        "date": {"start": "2026-11-07T11:00:00+00:00"},
    }
    notion.eventos.clear()
    notion.corpos.clear()
    return selo


# ------------------------------------------------------------------ adaptadores


class AdaptadorEspiao(AdaptadorFalso):
    """Adaptador falso que anota as chamadas a ``publicar`` no log de eventos do Notion."""

    def __init__(self, plataforma: str, eventos: list[tuple[str, ...]], **opcoes: Any) -> None:
        opcoes.setdefault("dias_restantes_conexao", 30)
        opcoes.setdefault("credencial", SegredoStr(TOKEN_REDE))
        super().__init__(plataforma, **opcoes)
        self._eventos = eventos
        self.pedidos: list[PedidoDePublicacao] = []
        self.explodir = False

    def publicar(self, pedido: PedidoDePublicacao) -> ResultadoPublicacao:
        self._eventos.append(("publicar", self.plataforma, pedido.post.id))
        self.pedidos.append(pedido)
        if self.explodir:
            raise RuntimeError("falha inesperada do adaptador " + TOKEN_REDE)
        return super().publicar(pedido)

    @property
    def publicacoes(self) -> int:
        return len(self.pedidos)


@pytest.fixture
def armazem(tmp_path: Path) -> ArmazemLocal:
    destino = tmp_path / "artes" / ARTE
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_bytes(b"\x89PNG ficticio 1")
    return ArmazemLocal(tmp_path / "artes")


class ArmazemIndisponivel:
    def ler(self, caminho: str) -> bytes:
        raise ErroAcessoArmazem("Falha de comunicacao com o armazem de artes (detalhes omitidos).")

    def hash_sha256(self, caminho: str) -> str:
        raise ErroAcessoArmazem("Falha de comunicacao com o armazem de artes (detalhes omitidos).")

    def existe(self, caminho: str) -> bool:
        raise ErroAcessoArmazem("Falha de comunicacao com o armazem de artes (detalhes omitidos).")


P1, P2, P3 = _pid(1), _pid(2), _pid(3)


def _cenario(
    armazem: ArmazemLocal, *paginas: tuple[str, dict[str, Any]], selar: bool = True
) -> NotionFalso:
    notion = NotionFalso({pid: PaginaFalsa(props) for pid, props in paginas})
    if selar:
        for pid, props in paginas:
            if props["Estágio"]["relation"][0]["id"] == ID_CONFIRMADO:
                _selar(notion, pid, armazem)
    return notion


def _adaptadores(
    notion: NotionFalso, *plataformas: str, armazem: ArmazemLocal | None = None
) -> dict[str, AdaptadorEspiao]:
    return {p: AdaptadorEspiao(p, notion.eventos, armazem=armazem) for p in plataformas}


def _executar(
    notion: NotionFalso,
    armazem: Any,
    adaptadores: Mapping[str, Any],
    *,
    publicar_real: bool = False,
    agora: datetime = AGORA,
    config: Configuracao | None = None,
    **opcoes: Any,
) -> RelatorioExecucao:
    relogio: Callable[[], datetime] = lambda: agora  # noqa: E731
    opcoes.setdefault("tipos_por_formato", TIPOS)
    return executar(
        _cliente(notion),
        IDS,
        _config() if config is None else config,
        armazem,
        adaptadores,
        PARAMETROS,
        relogio,
        publicar_real=publicar_real,
        **opcoes,
    )


# ------------------------------------------------------------------ PUBLICAR_REAL (RF-40)


@pytest.mark.parametrize(
    "ambiente",
    [{}, {"PUBLICAR_REAL": "true"}, {"PUBLICAR_REAL": "0"}, {"PUBLICAR_REAL": ""},
     {"PUBLICAR_REAL": "yes"}, {"PUBLICAR_REAL": " 1"}, {"PUBLICAR_REAL": "1 "},
     {"PUBLICAR_REAL": "01"}, {"publicar_real": "1"}],
)  # fmt: skip
def test_modo_real_so_liga_com_valor_exato_1(ambiente: dict[str, str]) -> None:
    assert modo_real_ligado(ambiente) is False


def test_modo_real_liga_com_1() -> None:
    assert VARIAVEL_MODO_REAL == "PUBLICAR_REAL"
    assert modo_real_ligado({"PUBLICAR_REAL": "1"}) is True


def test_modo_real_le_o_ambiente_do_processo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PUBLICAR_REAL", raising=False)
    assert modo_real_ligado() is False
    monkeypatch.setenv("PUBLICAR_REAL", "true")
    assert modo_real_ligado() is False
    monkeypatch.setenv("PUBLICAR_REAL", "1")
    assert modo_real_ligado() is True


# ------------------------------------------------------------------ fluxo completo em simulação


def test_fluxo_completo_em_simulacao(armazem: ArmazemLocal) -> None:
    notion = _cenario(armazem, (P1, _props()))
    adaptadores = _adaptadores(notion, "Instagram", armazem=armazem)

    rel = _executar(notion, armazem, adaptadores)

    assert rel.modo_real is False and rel.falha is None
    oc = rel.ocorrencia(P1, "Instagram")
    assert oc.desfecho is Desfecho.SIMULADO
    assert oc.registro is Registro.GRAVADO
    assert oc.resultado is TipoDeResultado.SUCESSO
    insta = adaptadores["Instagram"]
    assert insta.publicacoes == 1
    assert all(p.simular is True for p in insta.pedidos)
    assert insta.publicacoes_efetivas == 1  # aceita pela simulação, nada enviado
    assert notion.registros(P1) == [
        {"plataforma": "Instagram", "resultado": "simulado", "motivo": "simulacao"}
    ]
    assert notion.estagio(P1) == ID_CONFIRMADO  # simulação não muda o estágio
    assert not [e for e in notion.eventos if e[:2] == ("notion", "PATCH") and P1 in e[2]]
    assert len(notion.sinais()) == 1 and rel.sinal_de_vida is not None
    assert rel.contagens()["simulado"] == 1


def test_iniciada_gravada_antes_de_publicar_e_resultado_depois(armazem: ArmazemLocal) -> None:
    notion = _cenario(armazem, (P1, _props()))
    adaptadores = _adaptadores(notion, "Instagram")

    _executar(notion, armazem, adaptadores)

    eventos = notion.eventos
    i_pub = eventos.index(("publicar", "Instagram", P1))
    criacoes = [i for i, e in enumerate(eventos) if e == ("notion", "POST", "/pages")]
    patches = [
        i
        for i, e in enumerate(eventos)
        if e[:2] == ("notion", "PATCH") and e[2].startswith("/pages/") and P1 not in e[2]
    ]
    assert criacoes and criacoes[0] < i_pub  # "iniciada" ANTES de enviar
    assert patches and patches[0] > i_pub  # resultado DEPOIS


def test_simulacao_repetida_nao_simula_nem_registra_de_novo(armazem: ArmazemLocal) -> None:
    notion = _cenario(armazem, (P1, _props()))
    adaptadores = _adaptadores(notion, "Instagram")

    _executar(notion, armazem, adaptadores)
    rel = _executar(notion, armazem, adaptadores)

    assert rel.ocorrencia(P1, "Instagram").desfecho is Desfecho.SIMULACAO_REPETIDA
    assert adaptadores["Instagram"].publicacoes == 1
    assert len(notion.registros(P1)) == 1


def test_publicar_real_falso_mesmo_com_variavel_no_ambiente(
    armazem: ArmazemLocal, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O orquestrador obedece ao parâmetro explícito; quem lê a variável é o ponto de entrada."""
    monkeypatch.setenv("PUBLICAR_REAL", "1")
    notion = _cenario(armazem, (P1, _props()))
    adaptadores = _adaptadores(notion, "Instagram")
    _executar(notion, armazem, adaptadores, publicar_real=False)
    assert all(p.simular for p in adaptadores["Instagram"].pedidos)


# ------------------------------------------------------------------ modo real


@pytest.mark.parametrize(
    ("tipo", "desfecho", "resultado", "motivo", "estagio"),
    [
        (TipoDeResultado.SUCESSO, Desfecho.SUCESSO, "sucesso", None, ID_PUBLICADO),
        (
            TipoDeResultado.SUCESSO_MODO_PRIVADO,
            Desfecho.SUCESSO_MODO_PRIVADO,
            "sucesso em modo privado",
            "modo_privado",
            ID_PUBLICADO,
        ),
        (
            TipoDeResultado.FALHA_TRANSITORIA,
            Desfecho.FALHA_TRANSITORIA,
            "falha",
            "erro_transitorio",
            ID_CONFIRMADO,
        ),
        (
            TipoDeResultado.FALHA_PERMANENTE,
            Desfecho.FALHA_PERMANENTE,
            "falha",
            "erro_permanente",
            ID_FALHA,
        ),
        (
            TipoDeResultado.INCERTO,
            Desfecho.INCERTO,
            "incerto",
            "plataforma_nao_confirmou",
            ID_FALHA,
        ),
    ],
)
def test_modo_real_registra_resultado_e_nao_publica_duas_vezes(
    armazem: ArmazemLocal,
    tipo: TipoDeResultado,
    desfecho: Desfecho,
    resultado: str,
    motivo: str | None,
    estagio: str,
) -> None:
    notion = _cenario(armazem, (P1, _props()))
    adaptadores = _adaptadores(notion, "Instagram")
    adaptadores["Instagram"].programar_publicacao(tipo)

    rel = _executar(notion, armazem, adaptadores, publicar_real=True)

    oc = rel.ocorrencia(P1, "Instagram")
    assert rel.modo_real is True
    assert oc.desfecho is desfecho and oc.resultado is tipo
    assert oc.registro is Registro.GRAVADO and oc.pagina_execucao is not None
    assert all(p.simular is False for p in adaptadores["Instagram"].pedidos)
    assert notion.registros(P1) == [
        {"plataforma": "Instagram", "resultado": resultado, "motivo": motivo}
    ]
    # Pós-publicação (T039): o estágio acompanha o resultado real; o transitório aguarda a T040.
    assert notion.estagio(P1) == estagio  # ajustado na T039 (antes: sempre "confirmado")
    assert oc.pos_publicacao is not None

    rel2 = _executar(notion, armazem, adaptadores, publicar_real=True)

    assert adaptadores["Instagram"].publicacoes == 1  # nunca duas vezes
    if estagio == ID_CONFIRMADO:  # falha transitória: segue autorizado, aguarda nova tentativa
        assert rel2.ocorrencia(P1, "Instagram").desfecho is Desfecho.AGUARDANDO_NOVA_TENTATIVA
    else:  # saiu de "confirmado": a rodada nem lista mais o post
        assert rel2.ocorrencias == ()
    assert len(notion.registros(P1)) == 1  # nenhuma recusa nova registrada


def test_recusa_repetida_nao_e_registrada_de_novo(armazem: ArmazemLocal) -> None:
    # Ajustado na T039: antes o cenário era "sucesso" seguido de recusas, mas após o sucesso o
    # post passa a "publicado" e sai da rodada. Agora a recusa vem de conexão sem validade.
    notion = _cenario(armazem, (P1, _props()))
    adaptadores = {
        "Instagram": AdaptadorEspiao("Instagram", notion.eventos, dias_restantes_conexao=None)
    }
    for _ in range(3):
        _executar(notion, armazem, adaptadores, publicar_real=True)
    assert [r["resultado"] for r in notion.registros(P1)] == ["recusado"]
    assert adaptadores["Instagram"].publicacoes == 0
    assert notion.estagio(P1) == ID_CONFIRMADO  # recusa não altera o estágio


def test_simulacao_anterior_nao_impede_publicacao_real(armazem: ArmazemLocal) -> None:
    notion = _cenario(armazem, (P1, _props()))
    simulacao = _adaptadores(notion, "Instagram")
    _executar(notion, armazem, simulacao)
    # Nova rodada = novo processo do job (o AdaptadorFalso guarda em memória até a simulação
    # pela chave; com o MESMO objeto ele devolveria o resultado simulado, e o orquestrador
    # registraria "simulado" sem enviar, lado seguro).
    real = _adaptadores(notion, "Instagram")
    rel = _executar(notion, armazem, real, publicar_real=True)
    assert rel.ocorrencia(P1, "Instagram").desfecho is Desfecho.SUCESSO
    assert [p.simular for p in real["Instagram"].pedidos] == [False]


def test_modo_real_com_adaptador_que_so_simulou_registra_simulado(armazem: ArmazemLocal) -> None:
    notion = _cenario(armazem, (P1, _props()))
    adaptadores = _adaptadores(notion, "Instagram")
    _executar(notion, armazem, adaptadores)
    rel = _executar(notion, armazem, adaptadores, publicar_real=True)
    assert rel.ocorrencia(P1, "Instagram").desfecho is Desfecho.SIMULADO
    assert [r["resultado"] for r in notion.registros(P1)] == ["simulado", "simulado"]


def test_falha_ao_registrar_resultado_apos_publicar_nao_republica(armazem: ArmazemLocal) -> None:
    notion = _cenario(armazem, (P1, _props()))
    notion.falhar_patch_execucao = True
    adaptadores = _adaptadores(notion, "Instagram")

    rel = _executar(notion, armazem, adaptadores, publicar_real=True)

    oc = rel.ocorrencia(P1, "Instagram")
    assert oc.desfecho is Desfecho.SUCESSO and oc.registro is Registro.FALHOU
    assert rel.erros_de_registro == (oc,)
    # A execução ficou "iniciada" no Notion (pode ter saído, não foi confirmada): a pós-publicação
    # NÃO marca "publicado" (T039; ajustado) e leva o post a "falha"; nada é publicado de novo.
    assert notion.registros(P1)[0]["resultado"] is None
    assert notion.estagio(P1) == ID_FALHA
    notion.falhar_patch_execucao = False
    rel2 = _executar(notion, armazem, adaptadores, publicar_real=True)
    assert rel2.ocorrencias == ()
    assert adaptadores["Instagram"].publicacoes == 1


def test_excecao_do_adaptador_em_modo_real_vira_incerto(armazem: ArmazemLocal) -> None:
    notion = _cenario(armazem, (P1, _props()))
    adaptadores = _adaptadores(notion, "Instagram")
    adaptadores["Instagram"].explodir = True

    rel = _executar(notion, armazem, adaptadores, publicar_real=True)

    assert rel.ocorrencia(P1, "Instagram").desfecho is Desfecho.INCERTO
    assert notion.registros(P1) == [
        {"plataforma": "Instagram", "resultado": "incerto", "motivo": "erro_desconhecido"}
    ]


# ------------------------------------------------------------------ falha ao gravar "iniciada"


def test_falha_ao_gravar_iniciada_nao_publica(armazem: ArmazemLocal) -> None:
    notion = _cenario(armazem, (P1, _props()))
    notion.falhas_na_criacao = 1  # 500 em criação: ErroNotionIncerto
    adaptadores = _adaptadores(notion, "Instagram")

    rel = _executar(notion, armazem, adaptadores, publicar_real=True)

    assert adaptadores["Instagram"].publicacoes == 0
    oc = rel.ocorrencia(P1, "Instagram")
    assert oc.desfecho is Desfecho.NAO_INICIADO and oc.registro is Registro.FALHOU


# ------------------------------------------------------------------ recusas (sem adaptador)


def _sem_publicar(
    armazem: ArmazemLocal, notion: NotionFalso, motivo: str, desfecho: Desfecho
) -> None:
    adaptadores = _adaptadores(notion, "Instagram")
    rel = _executar(notion, armazem, adaptadores, publicar_real=True)
    assert adaptadores["Instagram"].publicacoes == 0
    oc = rel.ocorrencia(P1, "Instagram")
    assert (oc.desfecho, oc.motivo, oc.registro) == (desfecho, motivo, Registro.GRAVADO)
    assert notion.registros(P1) == [
        {"plataforma": "Instagram", "resultado": "recusado", "motivo": motivo}
    ]


def test_post_nao_confirmado_na_releitura_e_recusado(armazem: ArmazemLocal) -> None:
    notion = _cenario(armazem, (P1, _props(estagio_id=ID_PRONTO)))
    notion.lista_desatualizada[P1] = {
        "Estágio": {"type": "relation", "relation": [{"id": ID_CONFIRMADO}]}
    }
    _sem_publicar(armazem, notion, "estagio_nao_autorizado", Desfecho.RECUSADO)


def test_post_futuro_na_releitura_e_recusado(armazem: ArmazemLocal) -> None:
    notion = _cenario(armazem, (P1, _props(data="2026-11-07T15:00:00-03:00")))
    notion.lista_desatualizada[P1] = {"Data e hora": {"type": "date", "date": {"start": DATA}}}
    _sem_publicar(armazem, notion, "data_futura", Desfecho.RECUSADO)


def test_post_sem_data_na_releitura_volta_a_pronto(armazem: ArmazemLocal) -> None:
    notion = _cenario(armazem, (P1, _props()))
    notion.lista_desatualizada[P1] = {"Data e hora": {"type": "date", "date": {"start": DATA}}}
    notion.posts[P1].propriedades["Data e hora"] = {"type": "date", "date": None}
    _sem_publicar(armazem, notion, "sem_data", Desfecho.DEVOLVIDO_A_PRONTO)
    assert notion.estagio(P1) == ID_PRONTO


def test_post_sem_hora_na_releitura_volta_a_pronto(armazem: ArmazemLocal) -> None:
    notion = _cenario(armazem, (P1, _props()))
    notion.lista_desatualizada[P1] = {"Data e hora": {"type": "date", "date": {"start": DATA}}}
    notion.posts[P1].propriedades["Data e hora"] = {"type": "date", "date": {"start": "2026-11-07"}}
    _sem_publicar(armazem, notion, "sem_hora", Desfecho.DEVOLVIDO_A_PRONTO)
    assert notion.estagio(P1) == ID_PRONTO


def test_post_sem_selo_nao_e_revertido_e_nao_publica(armazem: ArmazemLocal) -> None:
    # R2-12: selo ausente (confirmação direta no Notion) espera o vigia selar; o orquestrador
    # NÃO reverte o estágio, mas a guarda recusa (selo_ausente) e nada é publicado.
    notion = _cenario(armazem, (P1, _props()), selar=False)
    _sem_publicar(armazem, notion, "selo_ausente", Desfecho.RECUSADO)
    assert notion.estagio(P1) == ID_CONFIRMADO


def test_post_editado_depois_de_confirmado_volta_a_pronto(armazem: ArmazemLocal) -> None:
    notion = _cenario(armazem, (P1, _props()))
    notion.posts[P1].propriedades["Título"] = {
        "type": "title",
        "title": [{"type": "text", "plain_text": TITULO + " editado"}],
    }
    _sem_publicar(armazem, notion, "selo_divergente", Desfecho.DEVOLVIDO_A_PRONTO)
    assert notion.estagio(P1) == ID_PRONTO


def test_post_fora_da_janela_e_recusado(armazem: ArmazemLocal) -> None:
    notion = _cenario(armazem, (P1, _props()))
    adaptadores = _adaptadores(notion, "Instagram")
    rel = _executar(notion, armazem, adaptadores, agora=AGORA + timedelta(hours=3))
    assert adaptadores["Instagram"].publicacoes == 0
    assert rel.ocorrencia(P1, "Instagram").motivo == "fora_da_janela"
    assert notion.registros(P1)[0]["motivo"] == "fora_da_janela"


def test_post_futuro_nem_e_considerado(armazem: ArmazemLocal) -> None:
    notion = _cenario(armazem, (P1, _props(data="2026-11-07T15:00:00-03:00")))
    adaptadores = _adaptadores(notion, "Instagram")
    rel = _executar(notion, armazem, adaptadores)
    assert rel.ocorrencias == () and notion.registros() == []
    assert adaptadores["Instagram"].publicacoes == 0
    assert len(notion.sinais()) == 1


def test_leitura_do_post_falhou_recusa(armazem: ArmazemLocal) -> None:
    notion = _cenario(armazem, (P1, _props()))
    notion.falhar_leitura.add(P1)
    _sem_publicar(armazem, notion, "leitura_falhou", Desfecho.RECUSADO)


def test_post_posterior_ja_saiu_na_plataforma(armazem: ArmazemLocal) -> None:
    posterior = _props(estagio_id=ID_PUBLICADO, data="2026-11-07T09:05:00-03:00")
    notion = _cenario(armazem, (P1, _props()), (P2, posterior))
    notion.execucoes.append(
        {
            "id": "f" * 32,
            "properties": {
                "Chave de idempotência": {"title": [{"text": {"content": "v1-" + "0" * 32}}]},
                "Post": {"relation": [{"id": P2}]},
                "Plataforma": {"select": {"name": "Instagram"}},
                "Tentativa": {"select": {"name": "1"}},
                "Resultado": {"select": {"name": "sucesso"}},
                "Início": {"date": {"start": "2026-11-07T12:06:00+00:00"}},
            },
        }
    )
    adaptadores = _adaptadores(notion, "Instagram")
    rel = _executar(notion, armazem, adaptadores, publicar_real=True)
    assert rel.ocorrencia(P1, "Instagram").motivo == "post_posterior_ja_saiu"
    assert adaptadores["Instagram"].publicacoes == 0


def test_serie_com_anterior_nao_publicado_e_bloqueada(armazem: ArmazemLocal) -> None:
    anterior = _props(estagio_id=ID_PRONTO, serie=ID_SERIE, ordem=1)
    notion = _cenario(armazem, (P1, _props(serie=ID_SERIE, ordem=2)), (P2, anterior))
    adaptadores = _adaptadores(notion, "Instagram")
    rel = _executar(notion, armazem, adaptadores, publicar_real=True)
    assert rel.ocorrencia(P1, "Instagram").motivo == "serie_bloqueada"
    assert adaptadores["Instagram"].publicacoes == 0


# ------------------------------------------------------------------ RF-37: plataforma sem adaptador


def test_plataforma_sem_adaptador_e_recusada_sem_bloquear_as_demais(armazem: ArmazemLocal) -> None:
    notion = _cenario(armazem, (P1, _props(plataformas=("Instagram", "Substack"))))
    adaptadores = _adaptadores(notion, "Instagram")

    rel = _executar(notion, armazem, adaptadores, publicar_real=True)

    assert rel.ocorrencia(P1, "Substack").desfecho is Desfecho.RECUSADO
    assert rel.ocorrencia(P1, "Substack").motivo == "sem_adaptador"
    assert rel.ocorrencia(P1, "Instagram").desfecho is Desfecho.SUCESSO
    assert adaptadores["Instagram"].publicacoes == 1
    assert sorted((r["plataforma"], r["resultado"]) for r in notion.registros(P1)) == [
        ("Instagram", "sucesso"),
        ("Substack", "recusado"),
    ]


# ------------------------------------------------------------------ isolamento


def test_erro_em_um_post_nao_impede_os_outros(armazem: ArmazemLocal) -> None:
    notion = _cenario(
        armazem,
        (P1, _props()),
        (P2, _props(data="2026-11-07T09:01:00-03:00")),
        (P3, _props(data="2026-11-07T09:02:00-03:00")),
    )
    notion.falhar_leitura.add(P1)
    adaptadores = _adaptadores(notion, "Instagram")
    original = adaptadores["Instagram"].publicar

    def publicar(pedido: PedidoDePublicacao) -> ResultadoPublicacao:
        if pedido.post.id == P2:
            raise RuntimeError("falha inesperada " + TOKEN_REDE)
        return original(pedido)

    adaptadores["Instagram"].publicar = publicar  # type: ignore[method-assign]

    rel = _executar(notion, armazem, adaptadores)

    assert rel.ocorrencia(P1, "Instagram").motivo == "leitura_falhou"
    assert rel.ocorrencia(P2, "Instagram").desfecho is Desfecho.ERRO
    assert rel.ocorrencia(P3, "Instagram").desfecho is Desfecho.SIMULADO
    assert [r["resultado"] for r in notion.registros(P2)] == ["não executado"]
    assert [o.post_id for o in rel.ocorrencias] == [P1, P2, P3]  # ordem por horário


def test_ordem_deterministica_por_horario_e_id(armazem: ArmazemLocal) -> None:
    notion = _cenario(
        armazem,
        (P3, _props(data="2026-11-07T08:59:00-03:00")),
        (P2, _props()),
        (P1, _props()),
    )
    adaptadores = _adaptadores(notion, "Instagram")
    rel = _executar(notion, armazem, adaptadores)
    assert [o.post_id for o in rel.ocorrencias] == [P3, P1, P2]


# ------------------------------------------------------------------ configuração e armazém


def test_configuracao_de_estagios_invalida_nao_publica_nada(armazem: ArmazemLocal) -> None:
    notion = _cenario(armazem, (P1, _props()))
    adaptadores = _adaptadores(notion, "Instagram")
    config = _config(problemas=("estágios: estágio sem papel",))

    rel = _executar(notion, armazem, adaptadores, publicar_real=True, config=config)

    assert rel.falha is FalhaGeral.CONFIG_ESTAGIOS_INVALIDA
    assert rel.ocorrencias == ()
    assert adaptadores["Instagram"].publicacoes == 0
    assert notion.registros() == []
    assert len(notion.sinais()) == 1  # o job está vivo, mesmo sem publicar


def test_ids_sem_banco_de_posts_nao_publica(armazem: ArmazemLocal) -> None:
    notion = _cenario(armazem, (P1, _props()))
    adaptadores = _adaptadores(notion, "Instagram")
    ids = IdsBancos(estagios=IDS.estagios, formatos=IDS.formatos, series=IDS.series,
                    temas=IDS.temas, execucoes=BANCO_EXEC)  # fmt: skip
    rel = executar(
        _cliente(notion), ids, _config(), armazem, adaptadores, PARAMETROS, lambda: AGORA,
        publicar_real=True, tipos_por_formato=TIPOS,
    )  # fmt: skip
    assert rel.falha is FalhaGeral.IDS_INCOMPLETOS
    assert adaptadores["Instagram"].publicacoes == 0


def test_armazem_indisponivel_nao_publica_nem_desconfirma(armazem: ArmazemLocal) -> None:
    notion = _cenario(armazem, (P1, _props()))
    adaptadores = _adaptadores(notion, "Instagram")

    rel = _executar(notion, ArmazemIndisponivel(), adaptadores, publicar_real=True)

    assert rel.ocorrencia(P1, "Instagram").desfecho is Desfecho.INDETERMINADO
    assert adaptadores["Instagram"].publicacoes == 0
    assert notion.estagio(P1) == ID_CONFIRMADO
    assert notion.registros() == []
    assert not [e for e in notion.eventos if e[:2] == ("notion", "PATCH")]


def test_formato_sem_tipo_por_padrao_recusa(armazem: ArmazemLocal) -> None:
    notion = _cenario(armazem, (P1, _props()))
    adaptadores = _adaptadores(notion, "Instagram")
    rel = _executar(notion, armazem, adaptadores, tipos_por_formato={})
    assert rel.ocorrencia(P1, "Instagram").motivo == "formato_sem_tipo"
    assert adaptadores["Instagram"].publicacoes == 0


def test_conexao_sem_validade_conhecida_recusa(armazem: ArmazemLocal) -> None:
    notion = _cenario(armazem, (P1, _props()))
    adaptadores = {"Instagram": AdaptadorEspiao("Instagram", notion.eventos,
                                                dias_restantes_conexao=None)}  # fmt: skip
    rel = _executar(notion, armazem, adaptadores)
    assert rel.ocorrencia(P1, "Instagram").motivo == "conexao_invalida"
    assert adaptadores["Instagram"].publicacoes == 0


# ------------------------------------------------------------------ I-1


def test_nada_sensivel_em_registros_logs_e_relatorio(
    armazem: ArmazemLocal, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    notion = _cenario(
        armazem,
        (P1, _props(plataformas=("Instagram", "Substack"))),
        (P2, _props(data="2026-11-07T09:01:00-03:00")),
    )
    notion.posts[P2].propriedades["Título"] = {
        "type": "title",
        "title": [{"type": "text", "plain_text": TITULO + " editado"}],
    }
    adaptadores = _adaptadores(notion, "Instagram")
    adaptadores["Instagram"].explodir = True

    rel = _executar(notion, armazem, adaptadores, publicar_real=True)

    escrito = "\n".join(notion.corpos)
    textos = (escrito, caplog.text, repr(rel), str(rel.contagens()))
    for proibido in (TOKEN, TOKEN_REDE, TITULO, TEXTO, ARTE):
        for texto in textos:
            assert proibido not in texto
    for registro in notion.execucoes:
        assert set(registro["properties"]) <= {
            "Chave de idempotência", "Post", "Plataforma", "Tentativa", "Resultado",
            "Código do motivo", "Início", "Fim", "ID do post na plataforma",
        }  # fmt: skip


def test_sinal_de_vida_reaproveita_pagina_informada(armazem: ArmazemLocal) -> None:
    notion = _cenario(armazem, (P1, _props()))
    adaptadores = _adaptadores(notion, "Instagram")
    rel = _executar(notion, armazem, adaptadores)
    assert rel.sinal_de_vida is not None
    rel2 = _executar(notion, armazem, adaptadores, pagina_sinal_de_vida=rel.sinal_de_vida)
    assert rel2.sinal_de_vida == rel.sinal_de_vida
    assert len(notion.sinais()) == 1


# ------------------------------------------------------------------ T039: recusa em escrita única


def _criacoes(notion: NotionFalso) -> list[tuple[str, ...]]:
    return [e for e in notion.eventos if e == ("notion", "POST", "/pages")]


def test_recusa_e_gravada_com_uma_unica_criacao_e_nunca_fica_iniciada(
    armazem: ArmazemLocal,
) -> None:
    notion = _cenario(armazem, (P1, _props()))
    adaptadores = {
        "Instagram": AdaptadorEspiao("Instagram", notion.eventos, dias_restantes_conexao=None)
    }

    rel = _executar(notion, armazem, adaptadores, publicar_real=True)

    assert rel.ocorrencia(P1, "Instagram").registro is Registro.GRAVADO
    # 1 criação da recusa + 1 do sinal de vida; nenhum PATCH em página de execução.
    assert len(_criacoes(notion)) == 2
    assert not [
        e for e in notion.eventos if e[:2] == ("notion", "PATCH") and e[2].startswith("/pages/")
    ]
    assert [r["resultado"] for r in notion.registros(P1)] == ["recusado"]


def test_falha_ao_criar_a_recusa_nao_deixa_execucao_orfa_nem_retenta(
    armazem: ArmazemLocal,
) -> None:
    notion = _cenario(armazem, (P1, _props()))
    notion.falhas_na_criacao = 1  # 500 na criação da recusa: ErroNotionIncerto
    adaptadores = {
        "Instagram": AdaptadorEspiao("Instagram", notion.eventos, dias_restantes_conexao=None)
    }

    rel = _executar(notion, armazem, adaptadores, publicar_real=True)

    oc = rel.ocorrencia(P1, "Instagram")
    assert oc.desfecho is Desfecho.RECUSADO and oc.registro is Registro.FALHOU
    assert notion.registros(P1) == []  # nada "iniciada" ficou para bloquear o post
    assert notion.estagio(P1) == ID_CONFIRMADO
    # A criação da recusa não é retentada na rodada (só o sinal de vida cria outra página).
    assert len(_criacoes(notion)) == 2


def test_erro_inesperado_registra_nao_executado_em_escrita_unica(
    armazem: ArmazemLocal, monkeypatch: pytest.MonkeyPatch
) -> None:
    notion = _cenario(armazem, (P1, _props()))
    adaptadores = _adaptadores(notion, "Instagram")

    def explode(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("falha inesperada")

    monkeypatch.setattr("publicador.job.orquestrador.avaliar", explode)
    rel = _executar(notion, armazem, adaptadores, publicar_real=True)

    assert rel.ocorrencia(P1, "Instagram").desfecho is Desfecho.ERRO
    assert notion.registros(P1) == [
        {"plataforma": "Instagram", "resultado": "não executado", "motivo": "erro_desconhecido"}
    ]
    assert len(_criacoes(notion)) == 2  # o registro + o sinal de vida


# ------------------------------------------------------------------ T039: tipos por formato padrão


def test_tipos_por_formato_vem_da_configuracao_por_padrao(armazem: ArmazemLocal) -> None:
    notion = _cenario(armazem, (P1, _props()))
    adaptadores = _adaptadores(notion, "Instagram")
    config = _config(tipos_por_formato=TIPOS)
    rel = _executar(notion, armazem, adaptadores, config=config, tipos_por_formato=None)
    assert rel.ocorrencia(P1, "Instagram").desfecho is Desfecho.SIMULADO


def test_tipos_por_formato_explicito_tem_prioridade_sobre_a_configuracao(
    armazem: ArmazemLocal,
) -> None:
    notion = _cenario(armazem, (P1, _props()))
    adaptadores = _adaptadores(notion, "Instagram")
    config = _config(tipos_por_formato=TIPOS)
    rel = _executar(notion, armazem, adaptadores, config=config, tipos_por_formato={})
    assert rel.ocorrencia(P1, "Instagram").motivo == "formato_sem_tipo"
    assert adaptadores["Instagram"].publicacoes == 0


def test_sem_mapeamento_na_configuracao_tudo_e_recusado(armazem: ArmazemLocal) -> None:
    notion = _cenario(armazem, (P1, _props()))
    adaptadores = _adaptadores(notion, "Instagram")
    rel = _executar(notion, armazem, adaptadores, tipos_por_formato=None)
    assert rel.ocorrencia(P1, "Instagram").motivo == "formato_sem_tipo"


# ------------------------------------------------------------- limpeza do estágio (T044, A-005)


class EstagioEspiao:
    """Estágio falso: anota a limpeza no log de eventos; publicar/apagar são proibidos aqui."""

    def __init__(self, eventos: list[tuple[str, ...]], erro: BaseException | None = None) -> None:
        self.eventos = eventos
        self.erro = erro
        self.instantes: list[datetime] = []

    def publicar_copia(self, nome_unico: str, dados: bytes) -> Any:
        raise AssertionError("o orquestrador não copia nada")

    def apagar_copia(self, copia: Any) -> bool:
        raise AssertionError("o orquestrador não apaga cópia avulsa")

    def limpar_antigas(self, agora: datetime, idade_max: timedelta = timedelta(minutes=15)) -> Any:
        from publicador.artes.estagio import ResultadoLimpeza

        self.instantes.append(agora)
        self.eventos.append(("limpeza_estagio",))
        if self.erro is not None:
            raise self.erro
        return ResultadoLimpeza(apagadas=3, falhas=0, listagem_ok=True)


def test_limpeza_do_estagio_roda_no_inicio_de_cada_execucao(armazem: ArmazemLocal) -> None:
    notion = _cenario(armazem, (P1, _props()))
    estagio = EstagioEspiao(notion.eventos)
    adaptadores = _adaptadores(notion, "Instagram", armazem=armazem)

    _executar(notion, armazem, adaptadores, estagio=estagio)
    _executar(notion, armazem, adaptadores, estagio=estagio)

    assert estagio.instantes == [AGORA, AGORA]
    assert notion.eventos[0] == ("limpeza_estagio",)  # antes de qualquer leitura/publicação
    assert notion.eventos.count(("limpeza_estagio",)) == 2


def test_limpeza_do_estagio_roda_mesmo_com_configuracao_invalida(armazem: ArmazemLocal) -> None:
    notion = _cenario(armazem, (P1, _props()))
    estagio = EstagioEspiao(notion.eventos)
    ids_sem_posts = IdsBancos(**{**IDS.__dict__, "posts": ""})
    executar(
        _cliente(notion), ids_sem_posts, _config(), armazem, {}, PARAMETROS, lambda: AGORA,
        publicar_real=False,
    )  # fmt: skip
    assert estagio.instantes == []  # sem estágio passado: nada
    rel = executar(
        _cliente(notion), ids_sem_posts, _config(), armazem, {}, PARAMETROS, lambda: AGORA,
        publicar_real=False, estagio=estagio,
    )  # fmt: skip
    assert rel.falha is not None
    assert estagio.instantes == [AGORA]


def test_falha_na_limpeza_nao_derruba_a_rodada(
    armazem: ArmazemLocal, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    notion = _cenario(armazem, (P1, _props()))
    estagio = EstagioEspiao(notion.eventos, RuntimeError("quebrou " + TOKEN_REDE))
    adaptadores = _adaptadores(notion, "Instagram", armazem=armazem)
    rel = _executar(notion, armazem, adaptadores, estagio=estagio)
    assert rel.ocorrencia(P1, "Instagram").desfecho is Desfecho.SIMULADO
    assert TOKEN_REDE not in caplog.text


def test_cancelamento_durante_a_limpeza_propaga(armazem: ArmazemLocal) -> None:
    notion = _cenario(armazem, (P1, _props()))
    estagio = EstagioEspiao(notion.eventos, KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        _executar(notion, armazem, {}, estagio=estagio)


def test_sem_estagio_nada_muda(armazem: ArmazemLocal) -> None:
    notion = _cenario(armazem, (P1, _props()))
    adaptadores = _adaptadores(notion, "Instagram", armazem=armazem)
    rel = _executar(notion, armazem, adaptadores)
    assert rel.ocorrencia(P1, "Instagram").desfecho is Desfecho.SIMULADO
    assert ("limpeza_estagio",) not in notion.eventos


def test_limpeza_nao_roda_com_relogio_sem_fuso(armazem: ArmazemLocal) -> None:
    notion = _cenario(armazem, (P1, _props()))
    estagio = EstagioEspiao(notion.eventos)
    rel = _executar(notion, armazem, {}, estagio=estagio, agora=datetime(2026, 11, 7, 12, 10))
    assert rel.falha is FalhaGeral.AGORA_SEM_FUSO
    assert estagio.instantes == []
