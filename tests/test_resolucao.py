"""Testes da resolução manual do estado incerto (publicador.job.resolucao) - T040 (RF-38).

A função é a que o painel (T056) vai chamar: uma pessoa diz se o post saiu ou não numa
plataforma com execução ``incerto`` (ou ``iniciada`` sem resultado). Sem rede; valores
fictícios; relógio injetado. I-1: o responsável é um nome curto validado, nunca token.
I-2: a resolução nunca habilita publicar por si só; a guarda continua mandando.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from test_nova_tentativa import Mundo, _execucoes, _mundo
from test_orquestrador import (
    AGORA,
    ARTE,
    ID_CONFIRMADO,
    ID_FALHA,
    ID_PUBLICADO,
    IDS,
    P1,
    P2,
    TEXTO,
    TITULO,
    TOKEN,
    TOKEN_REDE,
    NotionFalso,
    _cliente,
    _resposta,
    _selar,
)

from publicador.adaptadores.base import TipoDeResultado
from publicador.artes.armazem import ArmazemLocal
from publicador.core.redacao import limpar_segredos_registrados, registrar_segredo
from publicador.job.orquestrador import Desfecho
from publicador.job.resolucao import (
    DesfechoManual,
    ResultadoResolucao,
    SituacaoResolucao,
    resolver_incerto,
)

T = TipoDeResultado
DEPOIS = AGORA + timedelta(hours=3)


@pytest.fixture(autouse=True)
def _limpar_segredos() -> Any:
    limpar_segredos_registrados()
    yield
    limpar_segredos_registrados()


@pytest.fixture
def armazem(tmp_path: Path) -> ArmazemLocal:
    destino = tmp_path / "artes" / ARTE
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_bytes(b"\x89PNG ficticio 1")
    return ArmazemLocal(tmp_path / "artes")


def _incerto(armazem: ArmazemLocal, roteiro: Any = (T.INCERTO, False)) -> Mundo:
    mundo = _mundo(armazem, roteiro)
    rel = mundo.rodar(0)
    assert rel.ocorrencia(P1, "Instagram").desfecho is Desfecho.INCERTO
    assert mundo.notion.estagio(P1) == ID_FALHA
    return mundo


def _resolver(
    notion: NotionFalso,
    desfecho: DesfechoManual,
    responsavel: str = "Daniel Teste",
    **kw: Any,
) -> ResultadoResolucao:
    args: dict[str, Any] = {"agora": DEPOIS, "post_id": P1, "plataforma": "Instagram"}
    args.update(kw)
    return resolver_incerto(_cliente(notion), IDS, desfecho=desfecho, responsavel=responsavel,
                            **args)  # fmt: skip


def _reconfirmar(notion: NotionFalso, armazem: ArmazemLocal, quando: datetime) -> None:
    """Uma pessoa volta o post a "confirmado" depois da resolução (nova confirmação)."""
    notion.posts[P1].propriedades["Estágio"] = {
        "type": "relation",
        "relation": [{"id": ID_CONFIRMADO}],
    }
    _selar(notion, P1, armazem)
    notion.posts[P1].propriedades["Confirmado em"] = {
        "type": "date",
        "date": {"start": quando.isoformat()},
    }


# ------------------------------------------------------------------ publicado de fato


def test_resolucao_publicado_consolida_e_continua_bloqueando(armazem: ArmazemLocal) -> None:
    mundo = _incerto(armazem)
    notion = mundo.notion

    r = _resolver(notion, DesfechoManual.PUBLICADO, id_externo="17890000000099")

    assert r.situacao is SituacaoResolucao.RESOLVIDO and r.desfecho is DesfechoManual.PUBLICADO
    assert len(r.paginas_resolvidas) == 1
    assert _execucoes(notion) == [
        {"resultado": "sucesso", "motivo": "resolvido_publicado", "tentativa": "1",
         "externo": "17890000000099"},
    ]  # fmt: skip
    pagina = notion.execucoes[0]["properties"]
    texto = pagina["Texto do motivo"]["rich_text"][0]["text"]["content"]
    assert "Daniel Teste" in texto and DEPOIS.isoformat() in texto
    # Nunca republica: uma nova confirmação é barrada pelo sucesso consolidado (RF-12).
    _reconfirmar(notion, armazem, AGORA + timedelta(minutes=20))
    rel = mundo.rodar(25)
    assert rel.ocorrencia(P1, "Instagram").motivo == "execucao_existente"
    assert mundo.verdade.envios_reais == 1
    assert notion.estagio(P1) == ID_PUBLICADO  # a pós-publicação conclui com o sucesso


def test_resolucao_publicado_sem_id_externo(armazem: ArmazemLocal) -> None:
    mundo = _incerto(armazem)
    r = _resolver(mundo.notion, DesfechoManual.PUBLICADO)
    assert r.situacao is SituacaoResolucao.RESOLVIDO
    assert _execucoes(mundo.notion)[0]["externo"] is None


# ------------------------------------------------------------------ não publicado


def test_resolucao_nao_publicado_libera_so_com_nova_confirmacao(armazem: ArmazemLocal) -> None:
    mundo = _incerto(armazem)
    notion = mundo.notion

    r = _resolver(notion, DesfechoManual.NAO_PUBLICADO)

    assert r.situacao is SituacaoResolucao.RESOLVIDO
    assert _execucoes(notion)[0]["resultado"] == "falha"
    assert _execucoes(notion)[0]["motivo"] == "resolvido_nao_publicado"
    # A resolução, sozinha, não publica: o post segue no estágio "falha".
    assert notion.estagio(P1) == ID_FALHA
    assert mundo.rodar(20).ocorrencias == ()
    assert mundo.verdade.envios_reais == 1

    # Se o post voltar a "confirmado" com a MESMA confirmação, não há nova tentativa.
    _reconfirmar(notion, armazem, datetime(2026, 11, 7, 11, 0, tzinfo=UTC))
    oc = mundo.rodar(25).ocorrencia(P1, "Instagram")
    assert oc.desfecho is Desfecho.SEM_NOVA_TENTATIVA
    assert mundo.verdade.envios_reais == 1

    # Com uma nova confirmação (posterior), a guarda decide e a plataforma é publicada.
    _reconfirmar(notion, armazem, AGORA + timedelta(minutes=28))
    rel = mundo.rodar(30)
    assert rel.ocorrencia(P1, "Instagram").desfecho is Desfecho.SUCESSO
    assert mundo.verdade.envios_reais == 2 and notion.estagio(P1) == ID_PUBLICADO


def test_resolucao_nao_publicado_nao_contorna_a_guarda(armazem: ArmazemLocal) -> None:
    """Nova confirmação, mas fora da janela: a resolução não libera nada sozinha."""
    mundo = _incerto(armazem)
    _resolver(mundo.notion, DesfechoManual.NAO_PUBLICADO)
    _reconfirmar(mundo.notion, armazem, AGORA + timedelta(minutes=150))
    rel = mundo.rodar(160)
    assert rel.ocorrencia(P1, "Instagram").motivo == "fora_da_janela"
    assert mundo.verdade.envios_reais == 1


def test_resolucao_nunca_escreve_no_post(armazem: ArmazemLocal) -> None:
    mundo = _incerto(armazem)
    mundo.notion.eventos.clear()
    _resolver(mundo.notion, DesfechoManual.PUBLICADO)
    escritas = [e for e in mundo.notion.eventos if e[1] in ("PATCH", "POST")]
    assert all(P1 not in e[2] for e in escritas)
    assert mundo.notion.estagio(P1) == ID_FALHA


# ------------------------------------------------------------------ iniciada (queda)


def test_resolucao_tambem_resolve_iniciada_sem_resultado(armazem: ArmazemLocal) -> None:
    from test_nova_tentativa import Queda

    mundo = _mundo(armazem, ("queda", True))
    with pytest.raises(Queda):
        mundo.rodar(0)
    r = _resolver(mundo.notion, DesfechoManual.PUBLICADO, id_externo="17890000000001")
    assert r.situacao is SituacaoResolucao.RESOLVIDO
    exec0 = _execucoes(mundo.notion)[0]
    assert exec0["resultado"] == "sucesso" and exec0["motivo"] == "resolvido_publicado"
    fim = mundo.notion.execucoes[0]["properties"]["Fim"]["date"]["start"]
    assert fim == DEPOIS.isoformat()


# ------------------------------------------------------------------ nada a resolver


@pytest.mark.parametrize(
    "roteiro", [(T.SUCESSO, True), (T.FALHA_PERMANENTE, False), (T.FALHA_TRANSITORIA, False)]
)
def test_sem_incerto_nada_e_escrito(armazem: ArmazemLocal, roteiro: Any) -> None:
    mundo = _mundo(armazem, roteiro)
    mundo.rodar(0)
    antes = [dict(e) for e in _execucoes(mundo.notion)]
    mundo.notion.eventos.clear()
    r = _resolver(mundo.notion, DesfechoManual.NAO_PUBLICADO)
    assert r.situacao is SituacaoResolucao.NADA_A_RESOLVER and r.paginas_resolvidas == ()
    assert _execucoes(mundo.notion) == antes
    assert not [
        e for e in mundo.notion.eventos if e[1] in ("PATCH", "POST") and "query" not in e[2]
    ]


def test_resolver_duas_vezes_e_idempotente(armazem: ArmazemLocal) -> None:
    mundo = _incerto(armazem)
    _resolver(mundo.notion, DesfechoManual.PUBLICADO)
    r = _resolver(mundo.notion, DesfechoManual.NAO_PUBLICADO)
    assert r.situacao is SituacaoResolucao.NADA_A_RESOLVER
    assert _execucoes(mundo.notion)[0]["resultado"] == "sucesso"


def test_so_resolve_a_plataforma_pedida(armazem: ArmazemLocal) -> None:
    mundo = _incerto(armazem)
    r = _resolver(mundo.notion, DesfechoManual.PUBLICADO, plataforma="TikTok")
    assert r.situacao is SituacaoResolucao.NADA_A_RESOLVER
    r = _resolver(mundo.notion, DesfechoManual.PUBLICADO, post_id=P2)
    assert r.situacao is SituacaoResolucao.NADA_A_RESOLVER
    assert _execucoes(mundo.notion)[0]["resultado"] == "incerto"


# ------------------------------------------------------------------ entradas inválidas


@pytest.mark.parametrize(
    "campo,valor",
    [
        ("responsavel", ""),
        ("responsavel", "  "),
        ("responsavel", "x"),
        ("responsavel", "a" * 41),
        ("responsavel", TOKEN),
        ("responsavel", TOKEN_REDE),
        ("responsavel", "secret_abc123"),
        ("responsavel", "Bearer abcdef"),
        ("responsavel", "token=abc"),
        ("responsavel", "Daniel\nTeste"),
        ("responsavel", "Daniel <script>"),
        ("responsavel", "ghp_FALSO000000000000000"),
        ("responsavel", 123),
        ("post_id", "nao-e-um-id"),
        ("post_id", TITULO),
        ("plataforma", "Insta gram"),
        ("plataforma", ""),
        ("desfecho", "publicado"),
        ("desfecho", None),
        ("agora", datetime(2026, 11, 7, 15, 0)),
        ("agora", "2026-11-07T15:00:00Z"),
        ("id_externo", TITULO),
        ("id_externo", "https://exemplo.invalid/x?token=1"),
    ],
)
def test_entradas_invalidas_sao_recusadas_sem_escrever(
    armazem: ArmazemLocal, campo: str, valor: Any
) -> None:
    mundo = _incerto(armazem)
    mundo.notion.eventos.clear()
    args: dict[str, Any] = {
        "post_id": P1,
        "plataforma": "Instagram",
        "desfecho": DesfechoManual.PUBLICADO,
        "responsavel": "Daniel Teste",
        "agora": DEPOIS,
    }
    args[campo] = valor
    with pytest.raises(ValueError) as erro:
        resolver_incerto(_cliente(mundo.notion), IDS, **args)
    assert mundo.notion.eventos == []  # nada lido nem escrito
    if isinstance(valor, str) and valor.strip():
        assert valor not in str(erro.value)  # a mensagem não ecoa a entrada


def test_id_externo_so_com_publicado(armazem: ArmazemLocal) -> None:
    mundo = _incerto(armazem)
    with pytest.raises(ValueError):
        _resolver(mundo.notion, DesfechoManual.NAO_PUBLICADO, id_externo="17890000000001")


def test_responsavel_que_e_segredo_registrado_e_recusado(armazem: ArmazemLocal) -> None:
    mundo = _incerto(armazem)
    registrar_segredo("Fulano Segredo")
    with pytest.raises(ValueError):
        _resolver(mundo.notion, DesfechoManual.PUBLICADO, responsavel="Fulano Segredo")


def test_aceita_nomes_com_acento_e_fuso_qualquer(armazem: ArmazemLocal) -> None:
    mundo = _incerto(armazem)
    sp = timezone(timedelta(hours=-3))
    r = _resolver(
        mundo.notion,
        DesfechoManual.NAO_PUBLICADO,
        responsavel="  João D'Ávila-Souza ",
        agora=DEPOIS.astimezone(sp),
        post_id="00000000-0000-0000-0000-000000000001",
    )
    assert r.situacao is SituacaoResolucao.RESOLVIDO
    texto = mundo.notion.execucoes[0]["properties"]["Texto do motivo"]["rich_text"]
    assert "João D'Ávila-Souza" in texto[0]["text"]["content"]


# ------------------------------------------------------------------ erros do Notion


def test_erro_de_leitura_nao_escreve(armazem: ArmazemLocal) -> None:
    mundo = _incerto(armazem)

    class Quebrado(NotionFalso):
        def _consultar_execucoes(self, dados: dict[str, Any]) -> Any:
            return _resposta({"object": "error", "code": "validation_error"}, 400)

    quebrado = Quebrado(mundo.notion.posts, mundo.notion.execucoes)
    r = _resolver(quebrado, DesfechoManual.PUBLICADO)
    assert r.situacao is SituacaoResolucao.ERRO_LEITURA
    assert _execucoes(mundo.notion)[0]["resultado"] == "incerto"


def test_erro_ao_gravar_e_reportado_e_segue_incerto(armazem: ArmazemLocal) -> None:
    mundo = _incerto(armazem)
    mundo.notion.falhar_patch_execucao = True
    r = _resolver(mundo.notion, DesfechoManual.PUBLICADO)
    assert r.situacao is SituacaoResolucao.ERRO_REGISTRO and r.paginas_resolvidas == ()
    assert _execucoes(mundo.notion)[0]["resultado"] == "incerto"  # continua bloqueando


# ------------------------------------------------------------------ I-1


def test_nada_sensivel_em_logs_resultado_e_escritas(
    armazem: ArmazemLocal, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    mundo = _incerto(armazem)
    mundo.notion.corpos.clear()
    r = _resolver(mundo.notion, DesfechoManual.PUBLICADO, id_externo="17890000000099")
    with pytest.raises(ValueError):
        _resolver(mundo.notion, DesfechoManual.PUBLICADO, responsavel=TOKEN)
    textos = ("\n".join(mundo.notion.corpos), caplog.text, repr(r))
    for proibido in (TOKEN, TOKEN_REDE, TITULO, TEXTO, ARTE):
        assert all(proibido not in t for t in textos)
    assert "Daniel Teste" not in caplog.text and "Daniel Teste" not in repr(r)
    for registro in mundo.notion.execucoes:
        assert set(registro["properties"]) <= {
            "Chave de idempotência", "Post", "Plataforma", "Tentativa", "Resultado",
            "Código do motivo", "Texto do motivo", "Início", "Fim", "ID do post na plataforma",
        }  # fmt: skip
