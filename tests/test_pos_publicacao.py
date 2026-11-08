"""Testes da pós-publicação (publicador.job.pos_publicacao) - T039.

RF-30, RF-34, RF-35, A-003, A-022, I-1, I-2. Reaproveita o Notion falso com estado e os
adaptadores espiões dos testes do orquestrador. Sem rede; valores fictícios.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from test_orquestrador import (
    AGORA,
    ARTE,
    BANCO_EXEC,
    ESTAGIOS,
    ID_CONFIRMADO,
    ID_FALHA,
    ID_PRONTO,
    ID_PUBLICADO,
    IDS,
    P1,
    P2,
    TEXTO,
    TIPOS,
    TITULO,
    TOKEN,
    AdaptadorEspiao,
    NotionFalso,
    _adaptadores,
    _cenario,
    _cliente,
    _config,
    _executar,
    _props,
    _resposta,
    _selar,
)

from publicador.adaptadores.base import TipoDeResultado
from publicador.artes.armazem import ArmazemLocal
from publicador.core.modelos import EstadoExecucao, Estagio, PapelEstagio
from publicador.job.execucoes import (
    CodigoMotivo,
    chave_de,
    iniciar_execucao,
    registrar_resultado,
)
from publicador.job.orquestrador import Desfecho, Registro
from publicador.job.pos_publicacao import (
    DesfechoPos,
    ResultadoPos,
    _propriedades,
    concluir_post,
)

D = DesfechoPos


@pytest.fixture
def armazem(tmp_path: Path) -> ArmazemLocal:
    destino = tmp_path / "artes" / ARTE
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_bytes(b"\x89PNG ficticio 1")
    return ArmazemLocal(tmp_path / "artes")


DUAS = ("Instagram", "TikTok")


@dataclass
class NotionPos(NotionFalso):
    """Notion falso com ganchos para os casos de erro da pós-publicação."""

    falhar_patch_post: bool = False
    editar_no_get: int = 0  # edita o post em outra aba no N-ésimo GET da página (0 = nunca)
    gets: int = 0
    patches_post: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def _pagina(self, metodo: str, pid: str, dados: dict[str, Any]) -> Any:
        if pid in self.posts:
            if metodo == "GET":
                self.gets += 1
                if self.editar_no_get and self.gets == self.editar_no_get:
                    self.posts[pid].editado = "2026-11-07T12:05:00.000Z"
            if metodo == "PATCH":
                self.patches_post.append((pid, copy.deepcopy(dados["properties"])))
                if self.falhar_patch_post:
                    return _resposta({"object": "error", "code": "validation_error"}, 400)
        return super()._pagina(metodo, pid, dados)


def _notion(armazem: ArmazemLocal, plataformas: tuple[str, ...] = DUAS, **kw: Any) -> NotionPos:
    base = _cenario(armazem, (P1, _props(plataformas=plataformas)))
    notion = NotionPos(base.posts)
    for nome, valor in kw.items():
        setattr(notion, nome, valor)
    return notion


def _exec(
    notion: NotionFalso,
    plataforma: str,
    estado: EstadoExecucao,
    motivo: CodigoMotivo | None = None,
    tentativa: int = 1,
    pid: str = P1,
) -> None:
    """Grava uma execução no banco Execuções pelo próprio módulo de registro."""
    cliente = _cliente(notion)
    chave = chave_de(pid, plataforma, f"selo-{tentativa}-{estado.value}")
    reg = iniciar_execucao(cliente, IDS, pid, plataforma, chave, tentativa, AGORA)
    if estado is not EstadoExecucao.INICIADA:
        registrar_resultado(cliente, reg, estado, motivo, None, AGORA)
    notion.eventos.clear()
    notion.corpos.clear()


def _pos(
    notion: NotionFalso, *, modo_real: bool = True, config: Any = None, pid: str = P1
) -> ResultadoPos:
    return concluir_post(_cliente(notion), IDS, config or _config(), pid, modo_real=modo_real)


def _patches(notion: NotionFalso, pid: str = P1) -> list[tuple[str, ...]]:
    return [e for e in notion.eventos if e == ("notion", "PATCH", f"/pages/{pid}")]


def _registros(notion: NotionFalso) -> list[dict[str, str | None]]:
    return notion.registros(P1)


S, PRIV = EstadoExecucao.SUCESSO, EstadoExecucao.SUCESSO_MODO_PRIVADO
F, I = EstadoExecucao.FALHA, EstadoExecucao.INCERTO  # noqa: E741


# ------------------------------------------------------------------ um caso por desfecho


def test_sucesso_em_todas_as_plataformas_marca_publicado_num_patch_unico(
    armazem: ArmazemLocal,
) -> None:
    notion = _notion(armazem)
    selo = notion.posts[P1].propriedades["Selo de confirmação"]
    confirmado = notion.posts[P1].propriedades["Confirmado em"]
    for p in DUAS:
        _exec(notion, p, S)

    r = _pos(notion)

    assert r.desfecho is D.PUBLICADO and r.estagio_destino is PapelEstagio.PUBLICADO
    assert r.plataformas_publicadas == DUAS and r.plataformas_pendentes == ()
    assert r.modo_privado is False and r.estagio_alterado is True
    assert notion.estagio(P1) == ID_PUBLICADO
    assert len(_patches(notion)) == 1  # PATCH único
    assert notion.patches_post == [(P1, {"Estágio": {"relation": [{"id": ID_PUBLICADO}]}})]
    # Decisão: selo e "Confirmado em" ficam como prova do que foi confirmado.
    assert notion.posts[P1].propriedades["Selo de confirmação"] == selo
    assert notion.posts[P1].propriedades["Confirmado em"] == confirmado
    assert len(_registros(notion)) == 2  # execuções intactas


def test_sucesso_em_modo_privado_conta_como_publicado_com_marca_visivel(
    armazem: ArmazemLocal,
) -> None:
    notion = _notion(armazem)
    _exec(notion, "Instagram", S)
    _exec(notion, "TikTok", PRIV, CodigoMotivo.MODO_PRIVADO)

    r = _pos(notion)

    assert r.desfecho is D.PUBLICADO_MODO_PRIVADO
    assert r.modo_privado is True and r.plataformas_privadas == ("TikTok",)
    assert notion.estagio(P1) == ID_PUBLICADO
    # A marca visível no Notion está na execução (Resultado e código do motivo).
    assert {"plataforma": "TikTok", "resultado": "sucesso em modo privado",
            "motivo": "modo_privado"} in _registros(notion)  # fmt: skip


def test_todas_em_modo_privado_tambem_publica(armazem: ArmazemLocal) -> None:
    notion = _notion(armazem, ("TikTok",))
    _exec(notion, "TikTok", PRIV, CodigoMotivo.MODO_PRIVADO)
    r = _pos(notion)
    assert r.desfecho is D.PUBLICADO_MODO_PRIVADO and notion.estagio(P1) == ID_PUBLICADO


def test_sucesso_em_parte_vira_falha_e_preserva_o_que_saiu(armazem: ArmazemLocal) -> None:
    notion = _notion(armazem)
    _exec(notion, "Instagram", S)
    _exec(notion, "TikTok", F, CodigoMotivo.ERRO_PERMANENTE)

    r = _pos(notion)

    assert r.desfecho is D.FALHA and r.estagio_destino is PapelEstagio.FALHA
    assert r.plataformas_publicadas == ("Instagram",) and r.plataformas_pendentes == ("TikTok",)
    assert notion.estagio(P1) == ID_FALHA
    assert len(_patches(notion)) == 1
    # Fora de "autorizado" o post não carrega selo válido; a prova está no banco Execuções.
    assert notion.posts[P1].propriedades["Selo de confirmação"]["rich_text"] == []
    assert notion.posts[P1].propriedades["Confirmado em"]["date"] is None
    assert {"plataforma": "Instagram", "resultado": "sucesso", "motivo": None} in _registros(notion)


def test_sucesso_em_uma_e_outra_sem_execucao_vira_falha(armazem: ArmazemLocal) -> None:
    notion = _notion(armazem)
    _exec(notion, "Instagram", S)
    _exec(notion, "TikTok", EstadoExecucao.RECUSADA, CodigoMotivo.ERRO_DESCONHECIDO)
    assert _pos(notion).desfecho is D.FALHA and notion.estagio(P1) == ID_FALHA


def test_falha_permanente_sem_nenhum_sucesso_vira_falha(armazem: ArmazemLocal) -> None:
    notion = _notion(armazem, ("Instagram",))
    _exec(notion, "Instagram", F, CodigoMotivo.ERRO_PERMANENTE)
    assert _pos(notion).desfecho is D.FALHA and notion.estagio(P1) == ID_FALHA


@pytest.mark.parametrize(
    "outras",
    [
        pytest.param([], id="so-incerto"),
        pytest.param([("TikTok", S)], id="incerto-e-sucesso"),
        pytest.param([("TikTok", EstadoExecucao.INICIADA)], id="incerto-e-iniciada"),
    ],
)
def test_incerto_nunca_marca_publicado_e_vai_para_falha(
    armazem: ArmazemLocal, outras: list[tuple[str, EstadoExecucao]]
) -> None:
    notion = _notion(armazem)
    _exec(notion, "Instagram", I, CodigoMotivo.PLATAFORMA_NAO_CONFIRMOU)
    for plataforma, estado in outras:
        _exec(notion, plataforma, estado)

    r = _pos(notion)

    assert r.desfecho is D.FALHA
    assert notion.estagio(P1) == ID_FALHA and notion.estagio(P1) != ID_PUBLICADO
    assert {"plataforma": "Instagram", "resultado": "incerto",
            "motivo": "plataforma_nao_confirmou"} in _registros(notion)  # fmt: skip


def test_execucao_iniciada_sem_resultado_conta_como_duvida_e_nao_publica(
    armazem: ArmazemLocal,
) -> None:
    notion = _notion(armazem, ("Instagram",))
    _exec(notion, "Instagram", EstadoExecucao.INICIADA)
    r = _pos(notion)
    assert r.desfecho is D.FALHA and notion.estagio(P1) == ID_FALHA


def test_falha_transitoria_ainda_nao_repetida_aguarda_a_nova_tentativa(
    armazem: ArmazemLocal,
) -> None:
    notion = _notion(armazem, ("Instagram",))
    _exec(notion, "Instagram", F, CodigoMotivo.ERRO_TRANSITORIO)
    r = _pos(notion)
    assert r.desfecho is D.AGUARDANDO_NOVA_TENTATIVA and r.estagio_alterado is False
    assert notion.estagio(P1) == ID_CONFIRMADO and _patches(notion) == []


def test_falha_transitoria_repetida_vira_falha(armazem: ArmazemLocal) -> None:
    notion = _notion(armazem, ("Instagram",))
    _exec(notion, "Instagram", F, CodigoMotivo.ERRO_TRANSITORIO)
    _exec(notion, "Instagram", F, CodigoMotivo.ERRO_TRANSITORIO, tentativa=2)
    assert _pos(notion).desfecho is D.FALHA and notion.estagio(P1) == ID_FALHA


def test_transitoria_em_uma_e_sucesso_na_outra_segue_a_regra_do_parcial(
    armazem: ArmazemLocal,
) -> None:
    notion = _notion(armazem)
    _exec(notion, "Instagram", S)
    _exec(notion, "TikTok", F, CodigoMotivo.ERRO_TRANSITORIO)
    assert _pos(notion).desfecho is D.FALHA and notion.estagio(P1) == ID_FALHA  # A-003


@pytest.mark.parametrize(
    "estado",
    [EstadoExecucao.RECUSADA, EstadoExecucao.SIMULADA, EstadoExecucao.NAO_EXECUTADO],
)
def test_recusa_simulacao_e_nao_executado_nao_alteram_o_estagio(
    armazem: ArmazemLocal, estado: EstadoExecucao
) -> None:
    notion = _notion(armazem)
    _exec(notion, "Instagram", estado, CodigoMotivo.SIMULACAO)
    r = _pos(notion)
    assert r.desfecho is D.SEM_ALTERACAO and r.estagio_alterado is False
    assert notion.estagio(P1) == ID_CONFIRMADO and _patches(notion) == []


def test_post_sem_nenhuma_execucao_nao_muda(armazem: ArmazemLocal) -> None:
    notion = _notion(armazem)
    assert _pos(notion).desfecho is D.SEM_ALTERACAO
    assert notion.estagio(P1) == ID_CONFIRMADO


def test_simulacao_nunca_le_nem_altera(armazem: ArmazemLocal) -> None:
    notion = _notion(armazem)
    for p in DUAS:
        _exec(notion, p, S)
    r = _pos(notion, modo_real=False)
    assert r.desfecho is D.SEM_ALTERACAO and r.motivo == "simulacao"
    assert notion.eventos == [] and notion.estagio(P1) == ID_CONFIRMADO


# ------------------------------------------------------------------ configuração


def _sem(papel: PapelEstagio) -> Any:
    estagios = {i: e for i, e in ESTAGIOS.items() if e.papel is not papel}
    return _config(estagios=estagios)


@pytest.mark.parametrize(
    "config",
    [
        pytest.param(_sem(PapelEstagio.PUBLICADO), id="sem-publicado"),
        pytest.param(_sem(PapelEstagio.FALHA), id="sem-falha"),
        pytest.param(
            _config(
                estagios={**ESTAGIOS, "e1" * 16: Estagio("publicado 2", PapelEstagio.PUBLICADO)}
            ),
            id="dois-publicado",
        ),
        pytest.param(
            _config(estagios={**ESTAGIOS, "e2" * 16: Estagio("falha 2", PapelEstagio.FALHA)}),
            id="dois-falha",
        ),
        pytest.param(_config(problemas=("estágios: linha sem título",)), id="com-problemas"),
    ],
)
def test_configuracao_invalida_nao_altera_nada_e_reporta(
    armazem: ArmazemLocal, config: Any
) -> None:
    notion = _notion(armazem)
    for p in DUAS:
        _exec(notion, p, S)
    r = _pos(notion, config=config)
    assert r.desfecho is D.CONFIG_INVALIDA and r.estagio_alterado is False
    assert notion.eventos == []  # nem leitura: falha segura
    assert notion.estagio(P1) == ID_CONFIRMADO


# ------------------------------------------------------------------ idempotência e estágio


def test_rodar_de_novo_nao_muda_nada(armazem: ArmazemLocal) -> None:
    notion = _notion(armazem)
    for p in DUAS:
        _exec(notion, p, S)
    assert _pos(notion).desfecho is D.PUBLICADO
    notion.eventos.clear()

    r = _pos(notion)

    assert r.desfecho is D.JA_APLICADO and r.estagio_alterado is False
    assert _patches(notion) == [] and notion.estagio(P1) == ID_PUBLICADO
    assert len(notion.patches_post) == 1


def test_falha_tambem_e_idempotente(armazem: ArmazemLocal) -> None:
    notion = _notion(armazem, ("Instagram",))
    _exec(notion, "Instagram", F, CodigoMotivo.ERRO_PERMANENTE)
    assert _pos(notion).desfecho is D.FALHA
    assert _pos(notion).desfecho is D.JA_APLICADO
    assert len(notion.patches_post) == 1


def test_so_sai_de_autorizado_post_movido_por_pessoa_nao_e_tocado(armazem: ArmazemLocal) -> None:
    notion = _notion(armazem)
    for p in DUAS:
        _exec(notion, p, S)
    notion.posts[P1].propriedades["Estágio"] = {
        "type": "relation",
        "relation": [{"id": ID_PRONTO}],
    }
    r = _pos(notion)
    assert r.desfecho is D.SEM_ALTERACAO and r.motivo == "estagio_nao_autorizado"
    assert notion.estagio(P1) == ID_PRONTO and notion.patches_post == []


def test_pos_publicacao_nunca_escreve_o_estagio_autorizador() -> None:
    for papel in (PapelEstagio.AUTORIZADO, PapelEstagio.PRONTO, PapelEstagio.CANCELADO):
        with pytest.raises(ValueError):
            _propriedades(ID_CONFIRMADO, papel)
    assert _propriedades(ID_PUBLICADO, PapelEstagio.PUBLICADO)["Estágio"]["relation"] == [
        {"id": ID_PUBLICADO}
    ]


@pytest.mark.parametrize(
    "execucoes",
    [
        [("Instagram", S), ("TikTok", S)],
        [("Instagram", S), ("TikTok", F)],
        [("Instagram", I), ("TikTok", S)],
        [("Instagram", F), ("TikTok", F)],
        [("Instagram", EstadoExecucao.RECUSADA), ("TikTok", EstadoExecucao.SIMULADA)],
    ],
)
def test_nenhum_cenario_escreve_autorizado(
    armazem: ArmazemLocal, execucoes: list[tuple[str, EstadoExecucao]]
) -> None:
    notion = _notion(armazem)
    for plataforma, estado in execucoes:
        motivo = CodigoMotivo.ERRO_PERMANENTE if estado is F else None
        _exec(notion, plataforma, estado, motivo)
    _pos(notion)
    for _, props in notion.patches_post:
        assert props["Estágio"]["relation"][0]["id"] in (ID_PUBLICADO, ID_FALHA)


# ------------------------------------------------------------------ erros do Notion


def test_edicao_concorrente_nao_grava_e_nao_desfaz_a_execucao(armazem: ArmazemLocal) -> None:
    notion = _notion(armazem, editar_no_get=2)  # 1º GET = leitura; 2º = conferência
    for p in DUAS:
        _exec(notion, p, S)
    r = _pos(notion)
    assert r.desfecho is D.EDICAO_CONCORRENTE and r.estagio_alterado is False
    assert notion.patches_post == [] and notion.estagio(P1) == ID_CONFIRMADO
    assert len(_registros(notion)) == 2


def test_erro_do_notion_ao_mudar_o_estagio_e_reportado_sem_repetir(armazem: ArmazemLocal) -> None:
    notion = _notion(armazem, falhar_patch_post=True)
    for p in DUAS:
        _exec(notion, p, S)
    r = _pos(notion)  # não levanta
    assert r.desfecho is D.ERRO_NOTION and r.estagio_alterado is False
    assert len(notion.patches_post) == 1  # sem nova tentativa de escrita
    assert len(_registros(notion)) == 2 and notion.estagio(P1) == ID_CONFIRMADO


def test_erro_de_leitura_nao_altera_nada(armazem: ArmazemLocal) -> None:
    notion = _notion(armazem)
    for p in DUAS:
        _exec(notion, p, S)
    notion.falhar_leitura.add(P1)
    r = _pos(notion)
    assert r.desfecho is D.ERRO_LEITURA and notion.patches_post == []


# ------------------------------------------------------------------ pelo orquestrador


def _adapt(notion: NotionFalso, armazem: ArmazemLocal, *nomes: str) -> dict[str, AdaptadorEspiao]:
    return _adaptadores(notion, *nomes, armazem=armazem)


def test_orquestrador_publica_tudo_e_conclui_o_post(armazem: ArmazemLocal) -> None:
    notion = _notion(armazem)
    adaptadores = _adapt(notion, armazem, *DUAS)

    rel = _executar(notion, armazem, adaptadores, publicar_real=True)

    assert [rel.ocorrencia(P1, p).desfecho for p in DUAS] == [Desfecho.SUCESSO] * 2
    assert all(rel.ocorrencia(P1, p).pos_publicacao is D.PUBLICADO for p in DUAS)
    assert [r.desfecho for r in rel.pos_publicacao] == [D.PUBLICADO]
    assert notion.estagio(P1) == ID_PUBLICADO

    rel2 = _executar(notion, armazem, adaptadores, publicar_real=True)  # idempotente
    assert rel2.ocorrencias == () and rel2.pos_publicacao == ()
    assert [a.publicacoes for a in adaptadores.values()] == [1, 1]
    assert len(notion.patches_post) == 1


def test_orquestrador_sucesso_em_modo_privado(armazem: ArmazemLocal) -> None:
    notion = _notion(armazem)
    adaptadores = _adapt(notion, armazem, *DUAS)
    adaptadores["TikTok"].programar_publicacao(TipoDeResultado.SUCESSO_MODO_PRIVADO)

    rel = _executar(notion, armazem, adaptadores, publicar_real=True)

    pos = rel.pos_publicacao[0]
    assert pos.desfecho is D.PUBLICADO_MODO_PRIVADO and pos.plataformas_privadas == ("TikTok",)
    assert rel.ocorrencia(P1, "TikTok").pos_publicacao is D.PUBLICADO_MODO_PRIVADO
    assert notion.estagio(P1) == ID_PUBLICADO


def test_orquestrador_sucesso_parcial_vira_falha(armazem: ArmazemLocal) -> None:
    notion = _notion(armazem)
    adaptadores = _adapt(notion, armazem, *DUAS)
    adaptadores["TikTok"].programar_publicacao(TipoDeResultado.FALHA_PERMANENTE)

    rel = _executar(notion, armazem, adaptadores, publicar_real=True)

    assert rel.pos_publicacao[0].desfecho is D.FALHA
    assert notion.estagio(P1) == ID_FALHA
    assert {r["resultado"] for r in _registros(notion)} == {"sucesso", "falha"}


def test_orquestrador_incerto_em_uma_plataforma_nao_publica_o_post(armazem: ArmazemLocal) -> None:
    notion = _notion(armazem)
    adaptadores = _adapt(notion, armazem, *DUAS)
    adaptadores["TikTok"].explodir = True  # exceção durante o envio = incerto (RF-38)

    rel = _executar(notion, armazem, adaptadores, publicar_real=True)

    assert rel.ocorrencia(P1, "TikTok").desfecho is Desfecho.INCERTO
    assert rel.pos_publicacao[0].desfecho is D.FALHA
    assert notion.estagio(P1) == ID_FALHA


def test_orquestrador_recusa_nao_altera_o_estagio(armazem: ArmazemLocal) -> None:
    notion = _notion(armazem, ("Instagram",))
    adaptadores = {
        "Instagram": AdaptadorEspiao("Instagram", notion.eventos, dias_restantes_conexao=None)
    }
    rel = _executar(notion, armazem, adaptadores, publicar_real=True)
    assert rel.ocorrencia(P1, "Instagram").desfecho is Desfecho.RECUSADO
    assert rel.pos_publicacao[0].desfecho is D.SEM_ALTERACAO
    assert notion.estagio(P1) == ID_CONFIRMADO and notion.patches_post == []


def test_orquestrador_simulacao_nunca_altera_o_estagio(armazem: ArmazemLocal) -> None:
    notion = _notion(armazem)
    adaptadores = _adapt(notion, armazem, *DUAS)
    rel = _executar(notion, armazem, adaptadores)  # simulação
    assert rel.pos_publicacao == () and all(o.pos_publicacao is None for o in rel.ocorrencias)
    assert notion.estagio(P1) == ID_CONFIRMADO and notion.patches_post == []


def test_orquestrador_config_sem_estagio_de_falha_publica_mas_nao_move(
    armazem: ArmazemLocal,
) -> None:
    notion = _notion(armazem, ("Instagram",))
    adaptadores = _adapt(notion, armazem, "Instagram")
    rel = _executar(
        notion, armazem, adaptadores, publicar_real=True, config=_sem(PapelEstagio.FALHA)
    )
    assert rel.ocorrencia(P1, "Instagram").desfecho is Desfecho.SUCESSO
    assert rel.pos_publicacao[0].desfecho is D.CONFIG_INVALIDA
    assert notion.estagio(P1) == ID_CONFIRMADO  # segue bloqueado pela execução existente
    rel2 = _executar(notion, armazem, adaptadores, publicar_real=True)
    assert rel2.ocorrencia(P1, "Instagram").motivo == "execucao_existente"
    assert adaptadores["Instagram"].publicacoes == 1


def test_erro_do_notion_na_pos_nao_derruba_a_rodada_e_a_proxima_conclui(
    armazem: ArmazemLocal,
) -> None:
    notion = _notion(armazem, falhar_patch_post=True)
    adaptadores = _adapt(notion, armazem, *DUAS)

    rel = _executar(notion, armazem, adaptadores, publicar_real=True)

    assert [rel.ocorrencia(P1, p).desfecho for p in DUAS] == [Desfecho.SUCESSO] * 2
    assert rel.ocorrencia(P1, "Instagram").registro is Registro.GRAVADO
    assert rel.pos_publicacao[0].desfecho is D.ERRO_NOTION
    assert notion.estagio(P1) == ID_CONFIRMADO

    notion.falhar_patch_post = False  # o Notion volta: a rodada seguinte refaz a conta
    rel2 = _executar(notion, armazem, adaptadores, publicar_real=True)
    assert rel2.pos_publicacao[0].desfecho is D.PUBLICADO
    assert notion.estagio(P1) == ID_PUBLICADO
    assert [a.publicacoes for a in adaptadores.values()] == [1, 1]  # nunca republicou


def test_a003_nova_confirmacao_publica_so_as_pendentes(armazem: ArmazemLocal) -> None:
    """Instagram sai; o TikTok é recusado (conexão sem validade) -> falha. Depois da nova
    confirmação, só o TikTok é publicado: o Instagram é bloqueado pela execução existente."""
    notion = _notion(armazem)
    insta = AdaptadorEspiao("Instagram", notion.eventos, armazem=armazem, dias_restantes_conexao=30)
    tiktok_ruim = AdaptadorEspiao(
        "TikTok", notion.eventos, armazem=armazem, dias_restantes_conexao=None
    )

    rel = _executar(
        notion, armazem, {"Instagram": insta, "TikTok": tiktok_ruim}, publicar_real=True
    )

    assert rel.ocorrencia(P1, "TikTok").desfecho is Desfecho.RECUSADO
    assert rel.pos_publicacao[0].desfecho is D.FALHA and notion.estagio(P1) == ID_FALHA

    # Nova confirmação (a pessoa volta o post a "confirmado"; selo novo): a conexão já funciona.
    notion.posts[P1].propriedades["Estágio"] = {
        "type": "relation",
        "relation": [{"id": ID_CONFIRMADO}],
    }
    _selar(notion, P1, armazem)
    tiktok = AdaptadorEspiao("TikTok", notion.eventos, armazem=armazem, dias_restantes_conexao=30)
    rel2 = _executar(notion, armazem, {"Instagram": insta, "TikTok": tiktok}, publicar_real=True)

    assert rel2.ocorrencia(P1, "Instagram").motivo == "execucao_existente"
    assert rel2.ocorrencia(P1, "TikTok").desfecho is Desfecho.SUCESSO
    assert insta.publicacoes == 1 and tiktok.publicacoes == 1  # Instagram nunca de novo
    assert rel2.pos_publicacao[0].desfecho is D.PUBLICADO
    assert notion.estagio(P1) == ID_PUBLICADO


def test_t040_nova_confirmacao_republica_so_a_plataforma_que_falhou_com_certeza(
    armazem: ArmazemLocal,
) -> None:
    """Antes era ``test_pendencia_t040_...``: documentava que uma plataforma com execução
    ``falha`` ficava presa mesmo depois de nova confirmação. A T040 fecha o limite: a falha
    definitiva pertence à confirmação anterior; com a MESMA confirmação não há nova tentativa
    (``SEM_NOVA_TENTATIVA``), e uma nova confirmação (posterior à falha) publica só a pendente,
    passando por toda a guarda. O Instagram, que já saiu, nunca é reenviado."""
    notion = _notion(armazem)
    adaptadores = _adapt(notion, armazem, *DUAS)
    adaptadores["TikTok"].programar_publicacao(TipoDeResultado.FALHA_PERMANENTE)
    _executar(notion, armazem, adaptadores, publicar_real=True)
    assert notion.estagio(P1) == ID_FALHA

    # Mesma confirmação (selo e "Confirmado em" anteriores à falha): sem nova tentativa.
    notion.posts[P1].propriedades["Estágio"] = {
        "type": "relation",
        "relation": [{"id": ID_CONFIRMADO}],
    }
    _selar(notion, P1, armazem)
    rel = _executar(notion, armazem, adaptadores, publicar_real=True)
    assert rel.ocorrencia(P1, "TikTok").desfecho is Desfecho.SEM_NOVA_TENTATIVA
    assert rel.ocorrencia(P1, "TikTok").motivo == "erro_permanente"
    assert adaptadores["TikTok"].publicacoes == 1 and adaptadores["Instagram"].publicacoes == 1
    assert notion.estagio(P1) == ID_FALHA

    # Nova confirmação (depois da falha): só o TikTok sai; o post fica publicado.
    notion.posts[P1].propriedades["Estágio"] = {
        "type": "relation",
        "relation": [{"id": ID_CONFIRMADO}],
    }
    _selar(notion, P1, armazem)
    confirmado = AGORA + timedelta(minutes=20)
    notion.posts[P1].propriedades["Confirmado em"] = {
        "type": "date",
        "date": {"start": confirmado.isoformat()},
    }
    rel = _executar(
        notion, armazem, adaptadores, publicar_real=True, agora=AGORA + timedelta(minutes=25)
    )
    assert rel.ocorrencia(P1, "Instagram").motivo == "execucao_existente"
    assert rel.ocorrencia(P1, "TikTok").desfecho is Desfecho.SUCESSO
    assert adaptadores["TikTok"].publicacoes == 2 and adaptadores["Instagram"].publicacoes == 1
    assert notion.estagio(P1) == ID_PUBLICADO


def test_dois_posts_sao_concluidos_de_forma_independente(armazem: ArmazemLocal) -> None:
    base = _cenario(
        armazem,
        (P1, _props(plataformas=("Instagram",))),
        (P2, _props(plataformas=("Instagram",), data="2026-11-07T09:01:00-03:00")),
    )
    notion = NotionPos(base.posts)
    adaptadores = _adapt(notion, armazem, "Instagram")
    adaptadores["Instagram"].programar_publicacao(
        TipoDeResultado.SUCESSO, TipoDeResultado.FALHA_PERMANENTE
    )

    rel = _executar(notion, armazem, adaptadores, publicar_real=True)

    assert [r.desfecho for r in rel.pos_publicacao] == [D.PUBLICADO, D.FALHA]
    assert (notion.estagio(P1), notion.estagio(P2)) == (ID_PUBLICADO, ID_FALHA)


# ------------------------------------------------------------------ I-1


def test_nada_sensivel_em_logs_relatorio_e_escritas(
    armazem: ArmazemLocal, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    notion = _notion(armazem)
    adaptadores = _adapt(notion, armazem, *DUAS)
    adaptadores["TikTok"].programar_publicacao(TipoDeResultado.SUCESSO_MODO_PRIVADO)

    rel = _executar(notion, armazem, adaptadores, publicar_real=True)

    textos = ("\n".join(notion.corpos), caplog.text, repr(rel), repr(rel.pos_publicacao))
    for proibido in (TOKEN, TITULO, TEXTO, ARTE):
        assert all(proibido not in t for t in textos)
    patch = [p for _, p in notion.patches_post]
    assert patch == [{"Estágio": {"relation": [{"id": ID_PUBLICADO}]}}]


def test_constantes_do_teste_batem_com_o_banco_de_execucoes() -> None:
    assert BANCO_EXEC == IDS.execucoes and TIPOS["Feed"]["TikTok"] == "fotos"
