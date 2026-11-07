"""Testes do vigia de selo e varredura (publicador.job.vigia) - T024, RF-26, A-001, A-009, R2-12.

Sem rede: um transporte falso com estado simula o banco Posts do Notion (consulta paginada com
filtro, GET/PATCH de página e blocos). Relógio falso injetado. Todos os valores são fictícios
(I-1).
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

from publicador.artes.armazem import ArmazemLocal, ErroAcessoArmazem
from publicador.core.modelos import Estagio, PapelEstagio, Post
from publicador.core.redacao import SegredoStr
from publicador.core.selo import calcular_selo, verificar_selo
from publicador.job.vigia import (
    INTERVALO_SEM_EDICAO_PADRAO,
    Desfecho,
    MotivoVigia,
    RelatorioVigia,
    varrer,
)
from publicador.notion.bancos import Configuracao
from publicador.notion.cliente import ClienteNotion, RespostaHttp
from publicador.notion.mapeamento import ler_post

TOKEN = "TOKEN_FALSO_vigia_000XYZ"
BANCO_POSTS = "d0" * 16
ID_PAUTA = "a1" * 16
ID_PRONTO = "a2" * 16
ID_CONFIRMADO = "a3" * 16
ID_PUBLICADO = "a4" * 16
ID_FALHA = "a5" * 16
ID_CANCELADO = "a6" * 16

EDITADO = "2026-11-07T12:00:00.000Z"
T_EDITADO = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
EDITADO_APOS_PATCH = "2026-11-07T12:30:00.000Z"
DATA = "2026-11-20T18:30:00-03:00"
TITULO = "Quanto custa a energia?"
TEXTO = "Texto secreto do roteiro."
ARTE = "serie-a/2026/11/arte_01.png"
ARTE_2 = "serie-a/2026/11/arte_02.png"

ESTAGIOS = {
    ID_PAUTA: Estagio("pauta", PapelEstagio.PAUTA),
    ID_PRONTO: Estagio("pronto", PapelEstagio.PRONTO),
    ID_CONFIRMADO: Estagio("confirmado", PapelEstagio.AUTORIZADO),
    ID_PUBLICADO: Estagio("publicado", PapelEstagio.PUBLICADO),
    ID_FALHA: Estagio("falha", PapelEstagio.FALHA),
    ID_CANCELADO: Estagio("cancelado", PapelEstagio.CANCELADO),
}


def _config(estagios: Mapping[str, Estagio] | None = None) -> Configuracao:
    return Configuracao(
        estagios=dict(ESTAGIOS if estagios is None else estagios),
        formatos={},
        series={},
        temas={},
        problemas=(),
    )


def _pid(n: int) -> str:
    return f"{n:032x}"


def _relogio(delta: timedelta) -> Callable[[], datetime]:
    return lambda: T_EDITADO + delta


# ------------------------------------------------------------------ Notion falso


def _rich(texto: str | None) -> dict[str, Any]:
    if not texto:
        return {"type": "rich_text", "rich_text": []}
    return {"type": "rich_text", "rich_text": [{"type": "text", "plain_text": texto}]}


def _props(
    estagio_id: str = ID_CONFIRMADO,
    data: str | None = DATA,
    selo: str | None = None,
    confirmado: str | None = None,
    plataformas: tuple[str, ...] = ("Instagram", "TikTok"),
    artes: str | None = ARTE,
) -> dict[str, Any]:
    return {
        "Título": {"type": "title", "title": [{"type": "text", "plain_text": TITULO}]},
        "Estágio": {"type": "relation", "relation": [{"id": estagio_id}]},
        "Plataformas": {
            "type": "multi_select",
            "multi_select": [{"name": p} for p in plataformas],
        },
        "Data e hora": {"type": "date", "date": None if data is None else {"start": data}},
        "Artes": _rich(artes),
        "Selo de confirmação": _rich(selo),
        "Confirmado em": {
            "type": "date",
            "date": None if confirmado is None else {"start": confirmado},
        },
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
class PaginaFalsa:
    propriedades: dict[str, Any]
    editado: str = EDITADO
    texto: str = TEXTO


def _resposta(dados: object, status: int = 200) -> RespostaHttp:
    return RespostaHttp(status, {}, json.dumps(dados).encode())


@dataclass
class NotionFalso:
    """Banco Posts falso: consulta paginada (filtro por relação), páginas e blocos."""

    paginas: dict[str, PaginaFalsa] = field(default_factory=dict)
    ignorar_filtro: bool = False
    editar_na_leitura: dict[str, str] = field(default_factory=dict)
    falhar_blocos: set[str] = field(default_factory=set)
    blocos_indisponiveis: set[str] = field(default_factory=set)
    apos_patch: dict[str, Callable[[PaginaFalsa], None]] = field(default_factory=dict)
    chamadas: list[tuple[str, str, dict[str, Any] | None]] = field(default_factory=list)

    @property
    def patches(self) -> list[tuple[str, dict[str, Any]]]:
        return [(c[1], c[2] or {}) for c in self.chamadas if c[0] == "PATCH"]

    @property
    def escritas(self) -> list[tuple[str, str]]:
        return [
            (m, p)
            for m, p, _ in self.chamadas
            if m != "GET" and not p.endswith("/query")  # consulta é POST mas só lê
        ]

    def escritas_em(self, pid: str) -> list[tuple[str, str]]:
        return [e for e in self.escritas if e[1] == f"/pages/{pid}"]

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
        if caminho == f"/databases/{BANCO_POSTS}/query" and metodo == "POST":
            return self._consulta(dados or {})
        m = re.fullmatch(r"/pages/([0-9a-f]{32})", caminho)
        if m and m.group(1) in self.paginas:
            pid = m.group(1)
            pagina = self.paginas[pid]
            if metodo == "GET":
                if pid in self.editar_na_leitura:
                    pagina.editado = self.editar_na_leitura.pop(pid)
                return _resposta(self._pagina(pid))
            if metodo == "PATCH":
                assert dados is not None
                for nome, valor in dados["properties"].items():
                    pagina.propriedades[nome] = _normalizar(copy.deepcopy(valor))
                pagina.editado = EDITADO_APOS_PATCH
                if pid in self.apos_patch:
                    self.apos_patch.pop(pid)(pagina)
                return _resposta(self._pagina(pid))
        m = re.fullmatch(r"/blocks/([0-9a-f]{32})/children", caminho)
        if m and m.group(1) in self.paginas and metodo == "GET":
            pid = m.group(1)
            if pid in self.falhar_blocos:
                return _resposta({"object": "error", "code": "validation_error"}, 400)
            if pid in self.blocos_indisponiveis:
                return _resposta({"object": "error", "code": "service_unavailable"}, 503)
            bloco = {
                "id": "b" * 32,
                "type": "paragraph",
                "paragraph": {"rich_text": [{"plain_text": self.paginas[pid].texto}]},
            }
            return _resposta({"results": [bloco], "has_more": False})
        raise AssertionError(f"chamada inesperada: {metodo} {caminho}")

    def _consulta(self, dados: dict[str, Any]) -> RespostaHttp:
        ids = list(self.paginas)
        filtro = dados.get("filter")
        if filtro is not None and not self.ignorar_filtro:
            alvo = filtro["relation"]["contains"].replace("-", "")
            assert filtro["property"] == "Estágio"
            ids = [
                i
                for i in ids
                if any(
                    r["id"].replace("-", "") == alvo
                    for r in self.paginas[i].propriedades["Estágio"]["relation"]
                )
            ]
        inicio = int(dados.get("start_cursor") or 0)
        tamanho = int(dados["page_size"])
        fatia = ids[inicio : inicio + tamanho]
        mais = inicio + tamanho < len(ids)
        return _resposta(
            {
                "results": [self._pagina(i) for i in fatia],
                "has_more": mais,
                "next_cursor": str(inicio + tamanho) if mais else None,
            }
        )

    def _pagina(self, pid: str) -> dict[str, Any]:
        pagina = self.paginas[pid]
        return {
            "object": "page",
            "id": pid,
            "last_edited_time": pagina.editado,
            "properties": copy.deepcopy(pagina.propriedades),
        }

    def selo(self, pid: str) -> str | None:
        itens = self.paginas[pid].propriedades["Selo de confirmação"]["rich_text"]
        return "".join(i["plain_text"] for i in itens) or None

    def estagio(self, pid: str) -> str:
        return str(self.paginas[pid].propriedades["Estágio"]["relation"][0]["id"])

    def confirmado_em(self, pid: str) -> Any:
        return self.paginas[pid].propriedades["Confirmado em"]["date"]


def _cliente(notion: NotionFalso) -> ClienteNotion:
    return ClienteNotion(SegredoStr(TOKEN), transporte=notion, dormir=lambda _: None)


def _ler(notion: NotionFalso, pid: str) -> Post:
    return ler_post(_cliente(notion), pid, _config())


def _selar_valido(notion: NotionFalso, pid: str, armazem: ArmazemLocal) -> str:
    """Grava no falso um selo válido (como se a tela tivesse confirmado), sem registrar chamadas."""
    post = _ler(notion, pid)
    selo = calcular_selo(post, armazem)
    notion.paginas[pid].propriedades["Selo de confirmação"] = _rich(selo)
    notion.paginas[pid].propriedades["Confirmado em"] = {
        "type": "date",
        "date": {"start": "2026-11-07T11:59:00+00:00"},
    }
    notion.chamadas.clear()
    return selo


def _varrer(
    notion: NotionFalso,
    armazem: Any,
    delta: timedelta = timedelta(minutes=10),
    config: Configuracao | None = None,
    **opcoes: Any,
) -> RelatorioVigia:
    return varrer(
        _cliente(notion),
        _config() if config is None else config,
        armazem,
        _relogio(delta),
        banco_posts_id=BANCO_POSTS,
        **opcoes,
    )


@pytest.fixture
def armazem(tmp_path: Path) -> ArmazemLocal:
    for nome, conteudo in ((ARTE, b"\x89PNG ficticio 1"), (ARTE_2, b"\x89PNG ficticio 2")):
        destino = tmp_path / "artes" / nome
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(conteudo)
    return ArmazemLocal(tmp_path / "artes")


class ArmazemIndisponivel:
    def ler(self, caminho: str) -> bytes:
        raise ErroAcessoArmazem("Falha de comunicacao com o armazem de artes (detalhes omitidos).")

    def hash_sha256(self, caminho: str) -> str:
        raise ErroAcessoArmazem("Falha de comunicacao com o armazem de artes (detalhes omitidos).")

    def existe(self, caminho: str) -> bool:
        raise ErroAcessoArmazem("Falha de comunicacao com o armazem de artes (detalhes omitidos).")


P1 = _pid(1)


def _um(**props: Any) -> NotionFalso:
    return NotionFalso({P1: PaginaFalsa(_props(**props))})


# ------------------------------------------------------------------ intervalo de 5 minutos


def test_intervalo_padrao_e_cinco_minutos() -> None:
    assert INTERVALO_SEM_EDICAO_PADRAO == timedelta(minutes=5)


def test_selo_ausente_antes_dos_5_minutos_aguarda_sem_escrever(armazem: ArmazemLocal) -> None:
    notion = _um()
    rel = _varrer(notion, armazem, timedelta(minutes=4, seconds=59))
    assert rel.aguardando == (P1,)
    assert rel.selados == ()
    assert notion.escritas == []
    assert notion.selo(P1) is None
    # a guarda ainda não teria selo
    assert not verificar_selo(_ler(notion, P1), armazem, _ler(notion, P1).selo)


@pytest.mark.parametrize("delta", [timedelta(minutes=5), timedelta(minutes=5, seconds=1),
                                   timedelta(hours=3)])  # fmt: skip
def test_selo_ausente_a_partir_dos_5_minutos_e_selado_numa_unica_escrita(
    delta: timedelta, armazem: ArmazemLocal
) -> None:
    notion = _um()
    esperado = calcular_selo(_ler(notion, P1), armazem)
    notion.chamadas.clear()

    rel = _varrer(notion, armazem, delta)

    assert rel.selados == (P1,)
    assert notion.escritas == [("PATCH", f"/pages/{P1}")]  # UMA escrita
    ((_, corpo),) = notion.patches
    assert set(corpo["properties"]) == {"Selo de confirmação", "Confirmado em"}  # sem estágio
    assert notion.selo(P1) == esperado
    assert notion.confirmado_em(P1) == {"start": (T_EDITADO + delta).isoformat()}
    assert notion.estagio(P1) == ID_CONFIRMADO
    # pronto: a guarda recebe selo válido só depois do intervalo
    post = _ler(notion, P1)
    assert verificar_selo(post, armazem, post.selo)


def test_confirmado_em_ja_preenchido_e_preservado(armazem: ArmazemLocal) -> None:
    notion = _um(confirmado="2026-11-07T11:58:00+00:00")
    rel = _varrer(notion, armazem)
    assert rel.selados == (P1,)
    ((_, corpo),) = notion.patches
    assert set(corpo["properties"]) == {"Selo de confirmação"}
    assert notion.confirmado_em(P1) == {"start": "2026-11-07T11:58:00+00:00"}


def test_intervalo_injetavel(armazem: ArmazemLocal) -> None:
    notion = _um()
    rel = _varrer(notion, armazem, timedelta(minutes=5), intervalo_sem_edicao=timedelta(minutes=6))
    assert rel.aguardando == (P1,)
    assert notion.escritas == []


def test_edicao_no_futuro_relativo_ao_relogio_aguarda(armazem: ArmazemLocal) -> None:
    notion = _um()
    rel = _varrer(notion, armazem, timedelta(minutes=-1))
    assert rel.aguardando == (P1,)
    assert notion.escritas == []


def test_relogio_sem_fuso_e_recusado(armazem: ArmazemLocal) -> None:
    notion = _um()
    with pytest.raises(ValueError, match="fuso"):
        varrer(
            _cliente(notion),
            _config(),
            armazem,
            lambda: datetime(2026, 11, 7, 13, 0),
            banco_posts_id=BANCO_POSTS,
        )
    assert notion.chamadas == []


# ------------------------------------------------------------------ sem data e hora


@pytest.mark.parametrize(
    ("data", "motivo"), [(None, MotivoVigia.SEM_DATA), ("2026-11-20", MotivoVigia.SEM_HORA)]
)
def test_selo_ausente_sem_data_e_hora_apos_5_minutos_volta_a_pronto(
    data: str | None, motivo: MotivoVigia, armazem: ArmazemLocal
) -> None:
    notion = _um(data=data, confirmado="2026-11-07T11:58:00+00:00")
    rel = _varrer(notion, armazem)
    assert rel.devolvidos_a_pronto == (P1,)
    assert [(o.post_id, o.motivo) for o in rel.recusas] == [(P1, motivo)]
    assert notion.estagio(P1) == ID_PRONTO
    assert notion.selo(P1) is None and notion.confirmado_em(P1) is None
    assert len(notion.patches) == 1


def test_selo_ausente_sem_data_dentro_dos_5_minutos_aguarda(armazem: ArmazemLocal) -> None:
    notion = _um(data=None)
    rel = _varrer(notion, armazem, timedelta(minutes=2))
    assert rel.aguardando == (P1,)
    assert notion.escritas == []


def test_selo_ausente_sem_plataformas_apos_5_minutos_volta_a_pronto(armazem: ArmazemLocal) -> None:
    notion = _um(plataformas=())
    rel = _varrer(notion, armazem)
    assert rel.devolvidos_a_pronto == (P1,)
    assert rel.recusas[0].motivo is MotivoVigia.SEM_PLATAFORMAS


def test_selo_ausente_com_arte_inexistente_apos_5_minutos_volta_a_pronto(
    armazem: ArmazemLocal,
) -> None:
    notion = _um(artes="serie-a/2026/11/nao_existe.png")
    rel = _varrer(notion, armazem)
    assert rel.devolvidos_a_pronto == (P1,)
    assert rel.recusas[0].motivo is MotivoVigia.ARTE_INVALIDA
    assert notion.selo(P1) is None


# ------------------------------------------------------------------ selo presente


def test_selo_valido_e_mantido_sem_escrita(armazem: ArmazemLocal) -> None:
    notion = _um()
    _selar_valido(notion, P1, armazem)
    for delta in (timedelta(minutes=1), timedelta(days=2)):
        rel = _varrer(notion, armazem, delta)
        assert rel.mantidos == (P1,)
    assert notion.escritas == []


def _mudar_corpo(p: PaginaFalsa) -> None:
    p.texto = TEXTO + " Editado."


def _mudar_plataforma(p: PaginaFalsa) -> None:
    p.propriedades["Plataformas"]["multi_select"] = [{"name": "Instagram"}]


def _mudar_data(p: PaginaFalsa) -> None:
    p.propriedades["Data e hora"]["date"] = {"start": "2026-11-20T19:30:00-03:00"}


def _mudar_arte(p: PaginaFalsa) -> None:
    p.propriedades["Artes"] = _rich(ARTE_2)


def _tirar_data(p: PaginaFalsa) -> None:
    p.propriedades["Data e hora"]["date"] = None


@pytest.mark.parametrize(
    ("mudanca", "motivo"),
    [
        (_mudar_corpo, MotivoVigia.SELO_DIVERGENTE),
        (_mudar_plataforma, MotivoVigia.SELO_DIVERGENTE),
        (_mudar_data, MotivoVigia.SELO_DIVERGENTE),
        (_mudar_arte, MotivoVigia.SELO_DIVERGENTE),
        (_tirar_data, MotivoVigia.SEM_DATA),
    ],
)
def test_selo_divergente_volta_a_pronto_com_selo_e_confirmado_em_limpos(
    mudanca: Callable[[PaginaFalsa], None], motivo: MotivoVigia, armazem: ArmazemLocal
) -> None:
    notion = _um()
    _selar_valido(notion, P1, armazem)
    mudanca(notion.paginas[P1])
    # mesmo DENTRO dos 5 minutos: divergência não espera
    rel = _varrer(notion, armazem, timedelta(minutes=1))
    assert rel.devolvidos_a_pronto == (P1,)
    assert rel.recusas[0].motivo is motivo
    assert notion.estagio(P1) == ID_PRONTO
    assert notion.selo(P1) is None
    assert notion.confirmado_em(P1) is None
    assert len(notion.patches) == 1


@pytest.mark.parametrize("lixo", ["v2:" + "a" * 64, "qualquer coisa", "v1:XYZ"])
def test_selo_malformado_ou_de_versao_desconhecida_volta_a_pronto(
    lixo: str, armazem: ArmazemLocal
) -> None:
    notion = _um(selo=lixo)
    rel = _varrer(notion, armazem)
    assert rel.devolvidos_a_pronto == (P1,)
    assert rel.recusas[0].motivo is MotivoVigia.SELO_DIVERGENTE
    assert notion.estagio(P1) == ID_PRONTO


def test_selo_presente_com_arte_removida_do_armazem_volta_a_pronto(
    armazem: ArmazemLocal, tmp_path: Path
) -> None:
    notion = _um()
    _selar_valido(notion, P1, armazem)
    (tmp_path / "artes" / ARTE).unlink()
    rel = _varrer(notion, armazem)
    assert rel.devolvidos_a_pronto == (P1,)
    assert rel.recusas[0].motivo is MotivoVigia.ARTE_INVALIDA


# ------------------------------------------------------------------ indeterminado


def test_armazem_indisponivel_e_indeterminado_sem_escrita(armazem: ArmazemLocal) -> None:
    notion = NotionFalso({P1: PaginaFalsa(_props()), _pid(2): PaginaFalsa(_props())})
    _selar_valido(notion, P1, armazem)  # P1 com selo; P2 sem selo e após o intervalo
    rel = _varrer(notion, ArmazemIndisponivel())
    assert set(rel.indeterminados) == {P1, _pid(2)}
    assert all(o.motivo is MotivoVigia.ARMAZEM_INDISPONIVEL for o in rel.ocorrencias)
    assert notion.escritas == []
    assert notion.estagio(P1) == ID_CONFIRMADO


def test_notion_indisponivel_num_post_e_indeterminado_e_segue(armazem: ArmazemLocal) -> None:
    notion = NotionFalso({P1: PaginaFalsa(_props()), _pid(2): PaginaFalsa(_props())})
    notion.blocos_indisponiveis.add(P1)
    rel = _varrer(notion, armazem)
    assert rel.indeterminados == (P1,)
    assert rel.ocorrencia(P1).motivo is MotivoVigia.NOTION_INDISPONIVEL
    assert rel.selados == (_pid(2),)
    assert notion.escritas_em(P1) == []


# ------------------------------------------------------------------ outros estágios


def test_posts_em_outros_estagios_nunca_sao_tocados(armazem: ArmazemLocal) -> None:
    notion = NotionFalso()
    for n, estagio in enumerate(ESTAGIOS, start=1):
        notion.paginas[_pid(n)] = PaginaFalsa(_props(estagio_id=estagio, data=None))
    # mesmo se a consulta devolver tudo (filtro ignorado), só o autorizado é tratado
    notion.ignorar_filtro = True
    rel = _varrer(notion, armazem)
    autorizado = _pid(list(ESTAGIOS).index(ID_CONFIRMADO) + 1)
    assert set(rel.ignorados) == {_pid(n) for n in range(1, 7)} - {autorizado}
    assert rel.devolvidos_a_pronto == (autorizado,)  # sem data e hora
    assert [p for p, _ in notion.patches] == [f"/pages/{autorizado}"]


def test_post_em_outro_estagio_com_selo_residual_nao_e_tocado(armazem: ArmazemLocal) -> None:
    notion = NotionFalso({P1: PaginaFalsa(_props(estagio_id=ID_PRONTO, selo="v1:" + "b" * 64))})
    notion.ignorar_filtro = True
    rel = _varrer(notion, armazem)
    assert rel.ignorados == (P1,)
    assert notion.escritas == []


def test_consulta_usa_filtro_pelo_estagio_autorizador(armazem: ArmazemLocal) -> None:
    notion = _um()
    _varrer(notion, armazem, timedelta(minutes=1))
    consultas = [c for c in notion.chamadas if c[1].endswith("/query")]
    assert consultas
    filtro = consultas[0][2]["filter"]  # type: ignore[index]
    assert filtro == {"property": "Estágio", "relation": {"contains": ID_CONFIRMADO}}


# ------------------------------------------------------------------ configuração (A-001)


def _dois_autorizadores() -> Configuracao:
    estagios = dict(ESTAGIOS)
    estagios["a7" * 16] = Estagio("confirmado", PapelEstagio.AUTORIZADO)
    return _config(estagios)


def _autorizador_com_nome_errado() -> Configuracao:
    errado = Estagio("pronto", PapelEstagio.PRONTO)
    object.__setattr__(errado, "papel", PapelEstagio.AUTORIZADO)
    estagios = {k: v for k, v in ESTAGIOS.items() if k != ID_CONFIRMADO}
    estagios[ID_CONFIRMADO] = errado
    return _config(estagios)


def _sem_autorizador() -> Configuracao:
    return _config({k: v for k, v in ESTAGIOS.items() if k != ID_CONFIRMADO})


def _com_problemas() -> Configuracao:
    base = _config()
    return Configuracao(base.estagios, {}, {}, {}, ("estágios: linha sem papel",))


@pytest.mark.parametrize(
    "fabrica", [_dois_autorizadores, _autorizador_com_nome_errado, _sem_autorizador, _com_problemas]
)
def test_configuracao_invalida_nao_sela_nem_reverte_nada(
    fabrica: Callable[[], Configuracao], armazem: ArmazemLocal
) -> None:
    notion = NotionFalso(
        {
            P1: PaginaFalsa(_props()),  # selaria
            _pid(2): PaginaFalsa(_props(selo="lixo")),  # reverteria
        }
    )
    rel = _varrer(notion, armazem, config=fabrica())
    assert rel.falha is MotivoVigia.CONFIG_ESTAGIOS_INVALIDA
    assert rel.ocorrencias == ()
    assert notion.escritas == []
    assert notion.chamadas == []  # nem lista


@pytest.mark.parametrize("prontos", [0, 2])
def test_sem_estagio_pronto_unico_nada_e_escrito(prontos: int, armazem: ArmazemLocal) -> None:
    estagios = {k: v for k, v in ESTAGIOS.items() if k != ID_PRONTO}
    for n in range(prontos):
        estagios[f"{n:02x}" * 16] = Estagio(f"pronto {n}", PapelEstagio.PRONTO)
    notion = NotionFalso({P1: PaginaFalsa(_props()), _pid(2): PaginaFalsa(_props(selo="lixo"))})
    rel = _varrer(notion, armazem, config=_config(estagios))
    assert rel.falha is MotivoVigia.CONFIG_SEM_PRONTO
    assert notion.escritas == []


# ------------------------------------------------------------------ varredura completa


def test_todos_os_autorizados_sao_varridos_com_paginacao(armazem: ArmazemLocal) -> None:
    notion = NotionFalso()
    datas = ["2026-11-20T18:30:00-03:00", "2030-01-01T09:00:00-03:00", "2099-12-31T23:00:00Z"]
    total = 230
    for n in range(1, total + 1):
        notion.paginas[_pid(n)] = PaginaFalsa(_props(data=datas[n % 3]))
    # alguns posts de outros estágios intercalados (a consulta filtrada os exclui)
    for n in range(1000, 1010):
        notion.paginas[_pid(n)] = PaginaFalsa(_props(estagio_id=ID_PRONTO))
    rel = _varrer(notion, armazem, timedelta(minutes=1))
    consultas = [c for c in notion.chamadas if c[1].endswith("/query")]
    assert len(consultas) == 3  # 100 + 100 + 30
    assert set(rel.aguardando) == {_pid(n) for n in range(1, total + 1)}
    assert notion.escritas == []


def test_erro_em_um_post_nao_impede_os_outros(armazem: ArmazemLocal) -> None:
    notion = NotionFalso(
        {
            _pid(1): PaginaFalsa(_props()),
            _pid(2): PaginaFalsa(_props()),  # blocos falham (erro permanente)
            _pid(3): PaginaFalsa(_props(selo="lixo")),
        }
    )
    # post ilegível: título vazio
    ruim = _props()
    ruim["Título"]["title"] = []
    notion.paginas[_pid(4)] = PaginaFalsa(ruim)
    notion.paginas[_pid(5)] = PaginaFalsa(_props())
    notion.falhar_blocos.add(_pid(2))

    rel = _varrer(notion, armazem)

    assert rel.selados == (_pid(1), _pid(5))
    assert rel.devolvidos_a_pronto == (_pid(3),)
    assert set(rel.erros) == {_pid(2), _pid(4)}
    assert rel.ocorrencia(_pid(2)).motivo is MotivoVigia.NOTION_ERRO
    assert rel.ocorrencia(_pid(4)).motivo is MotivoVigia.POST_ILEGIVEL
    assert notion.escritas_em(_pid(2)) == [] and notion.escritas_em(_pid(4)) == []


def test_falha_na_listagem_e_registrada_sem_escrita(armazem: ArmazemLocal) -> None:
    notion = _um()

    def _quebrar(*_: Any, **__: Any) -> RespostaHttp:
        return _resposta({"object": "error", "code": "unauthorized"}, 401)

    notion.enviar = _quebrar  # type: ignore[method-assign]
    rel = _varrer(notion, armazem)
    assert rel.falha is MotivoVigia.NOTION_ERRO
    assert rel.ocorrencias == ()


# ------------------------------------------------------------------ concorrência


def test_edicao_concorrente_durante_a_selagem_nao_grava_selo(armazem: ArmazemLocal) -> None:
    notion = NotionFalso({P1: PaginaFalsa(_props()), _pid(2): PaginaFalsa(_props())})
    notion.editar_na_leitura[P1] = "2026-11-07T12:09:00.000Z"
    rel = _varrer(notion, armazem)
    assert rel.ocorrencia(P1).desfecho is Desfecho.ERRO
    assert rel.ocorrencia(P1).motivo is MotivoVigia.EDICAO_CONCORRENTE
    assert notion.escritas_em(P1) == []
    assert notion.selo(P1) is None and notion.confirmado_em(P1) is None
    assert notion.estagio(P1) == ID_CONFIRMADO  # nada revertido (R2-12)
    assert rel.selados == (_pid(2),)


def test_edicao_concorrente_ao_devolver_e_registrada(armazem: ArmazemLocal) -> None:
    notion = _um(selo="lixo")
    notion.editar_na_leitura[P1] = "2026-11-07T12:09:00.000Z"
    rel = _varrer(notion, armazem)
    assert rel.ocorrencia(P1).motivo is MotivoVigia.EDICAO_CONCORRENTE
    assert notion.escritas == []


def test_conteudo_mudou_entre_a_selagem_e_a_releitura_volta_a_pronto(
    armazem: ArmazemLocal,
) -> None:
    notion = _um()
    notion.apos_patch[P1] = _mudar_corpo  # mudança sem alterar o minuto de edição
    rel = _varrer(notion, armazem)
    assert rel.devolvidos_a_pronto == (P1,)
    assert rel.recusas[0].motivo is MotivoVigia.SELO_NAO_CONFERE_APOS_GRAVAR
    assert notion.estagio(P1) == ID_PRONTO
    assert notion.selo(P1) is None


def test_post_saiu_de_autorizado_durante_a_selagem_tem_selo_residual_limpo(
    armazem: ArmazemLocal,
) -> None:
    def _desconfirmar(p: PaginaFalsa) -> None:
        p.propriedades["Estágio"]["relation"] = [{"id": ID_PAUTA}]

    notion = _um()
    notion.apos_patch[P1] = _desconfirmar
    rel = _varrer(notion, armazem)
    assert rel.ocorrencia(P1).desfecho is Desfecho.ERRO
    assert rel.ocorrencia(P1).motivo is MotivoVigia.EDICAO_CONCORRENTE
    assert notion.estagio(P1) == ID_PAUTA  # não volta a confirmado
    assert notion.selo(P1) is None and notion.confirmado_em(P1) is None


# ------------------------------------------------------------------ idempotência


def test_execucao_repetida_nao_reescreve(armazem: ArmazemLocal) -> None:
    notion = NotionFalso(
        {
            _pid(1): PaginaFalsa(_props()),
            _pid(2): PaginaFalsa(_props(selo="lixo")),
            _pid(3): PaginaFalsa(_props()),
        }
    )
    _selar_valido(notion, _pid(3), armazem)
    primeira = _varrer(notion, armazem)
    assert primeira.selados == (_pid(1),)
    assert primeira.devolvidos_a_pronto == (_pid(2),)
    assert primeira.mantidos == (_pid(3),)
    escritas = len(notion.escritas)
    assert escritas == 2

    segunda = _varrer(notion, armazem, timedelta(minutes=20))
    assert segunda.mantidos == (_pid(1), _pid(3))
    assert segunda.devolvidos_a_pronto == ()
    assert len(notion.escritas) == escritas  # nenhuma escrita nova


# ------------------------------------------------------------------ I-1: logs e relatório


def test_logs_e_relatorio_sem_token_nem_conteudo(
    armazem: ArmazemLocal, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    ruim = _props()
    ruim["Título"]["title"] = []
    notion = NotionFalso(
        {
            _pid(1): PaginaFalsa(_props()),
            _pid(2): PaginaFalsa(_props(selo="lixo")),
            _pid(3): PaginaFalsa(ruim),
            _pid(4): PaginaFalsa(_props(data=None)),
            _pid(5): PaginaFalsa(_props()),
            _pid(6): PaginaFalsa(_props(artes="serie-a/2026/11/nao_existe.png")),
        }
    )
    notion.falhar_blocos.add(_pid(5))
    rel = _varrer(notion, armazem)
    rel2 = _varrer(_um(), ArmazemIndisponivel())

    textos = [r.getMessage() for r in caplog.records]
    textos += [repr(rel), str(rel), repr(rel2)]
    textos += [repr(o) for o in rel.ocorrencias]
    proibidos = [TOKEN, TITULO, TEXTO, ARTE, "nao_existe", "Instagram", "lixo"]
    for texto in textos:
        for proibido in proibidos:
            assert proibido not in texto
    # a recusa fica registrada no log com id e motivo (A-009)
    assert any(_pid(2) in t and "selo_divergente" in t for t in textos)
