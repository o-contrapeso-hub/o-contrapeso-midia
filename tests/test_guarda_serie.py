"""Testes da verificação 7 da guarda: séries de continuidade e liberação manual (T033; RF-27).

Complementam os casos de série de ``tests/test_guarda.py``. Escritos na T033, quando ``avaliar``
ainda tinha a trava ``GuardaIncompleta`` (removida na T034); por isso:

* os casos de unidade chamam a verificação interna ``_serie`` diretamente;
* os casos de ponta a ponta usam ``_primeira_recusa`` (o laço de ``avaliar`` sobre
  ``_VERIFICACOES``): devolve ``(motivo, detalhe)`` ou ``None`` quando todas as verificações
  passam. O contexto base é válido em TODAS as verificações (inclusive as 8 a 10 da T034);
  desde a T034, ``_primeira_recusa(...) is None`` equivale a ``avaliar(...).permitido``;
* quando a guarda recusa, ``avaliar`` devolve o resultado normal e é usado direto.

Valores fictícios; nada acessa a rede (I-1).
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from publicador.artes.armazem import ArmazemLocal
from publicador.core.config import Parametros
from publicador.core.guarda import (
    ContextoGuarda,
    PublicacaoConhecida,
    _primeira_recusa,
    _serie,
    avaliar,
)
from publicador.core.modelos import (
    Conexao,
    ConexaoEstado,
    Estagio,
    MotivoRecusa,
    PapelEstagio,
    Post,
    Serie,
)
from publicador.core.selo import calcular_selo
from publicador.notion.bancos import Configuracao

M = MotivoRecusa
P = PapelEstagio
SAO_PAULO = ZoneInfo("America/Sao_Paulo")
AGENDADO = datetime(2026, 11, 7, 18, 30, tzinfo=SAO_PAULO)
AGORA = AGENDADO + timedelta(minutes=10)
PLATAFORMA = "Instagram"
ARTE = "serie-ficticia/2026/11/arte_01.png"
TITULO = "Título fictício que não pode vazar"
CONTINUA = "Série de continuidade fictícia"
OUTRA_CONTINUA = "Outra série de continuidade fictícia"
SOLTA = "Série solta fictícia"
ID_POST = "post-ficticio-0005"
ORDEM = 5  # o post testado é o de ordem 5

ESTAGIOS: dict[str, Estagio] = {
    "est-pauta": Estagio("banco de pautas", P.PAUTA),
    "est-pronto": Estagio("posts prontos", P.PRONTO),
    "est-confirmado": Estagio("confirmado", P.AUTORIZADO),
    "est-publicado": Estagio("publicado", P.PUBLICADO),
    "est-falha": Estagio("falha na publicação", P.FALHA),
    "est-cancelado": Estagio("cancelado", P.CANCELADO),
}


def _estagio(papel: PapelEstagio) -> Estagio:
    (achado,) = [e for e in ESTAGIOS.values() if e.papel is papel]
    return achado


def _configuracao(**series: Serie) -> Configuracao:
    return Configuracao(
        estagios=dict(ESTAGIOS),
        formatos={"fmt-feed": "Feed"},
        series=series
        or {
            "ser-continua": Serie(CONTINUA, continuidade=True),
            "ser-outra": Serie(OUTRA_CONTINUA, continuidade=True),
            "ser-solta": Serie(SOLTA, continuidade=False),
        },
        temas={},
        problemas=(),
    )


def _irmao(
    ordem: int | None, papel: PapelEstagio, serie: str | None = CONTINUA, *, id_: str = ""
) -> Post:
    return Post(
        id=id_ or f"post-ficticio-irmao-{ordem}-{serie}",
        titulo=f"Parte {ordem} fictícia",
        corpo="Corpo fictício.",
        estagio=_estagio(papel),
        plataformas=(PLATAFORMA,),
        formato="Feed",
        serie=serie,
        ordem_na_serie=ordem,
        publicar_em=AGENDADO - timedelta(days=1),
    )


@pytest.fixture
def armazem(tmp_path: Path) -> ArmazemLocal:
    destino = tmp_path / ARTE
    destino.parent.mkdir(parents=True)
    destino.write_bytes(b"\x89PNG conteudo ficticio")
    return ArmazemLocal(tmp_path)


@pytest.fixture
def contexto(armazem: ArmazemLocal) -> ContextoGuarda:
    """Válido em TODAS as verificações; série de continuidade com 1 a 4 publicados."""
    sem_selo = Post(
        id=ID_POST,
        titulo=TITULO,
        corpo="Corpo fictício.",
        estagio=_estagio(P.AUTORIZADO),
        plataformas=(PLATAFORMA,),
        formato="Feed",
        serie=CONTINUA,
        ordem_na_serie=ORDEM,
        publicar_em=AGENDADO,
        artes=(ARTE,),
    )
    return ContextoGuarda(
        post=dataclasses.replace(sem_selo, selo=calcular_selo(sem_selo, armazem)),
        configuracao=_configuracao(),
        agora=AGORA,
        parametros=Parametros(
            janela_tolerancia_minutos=120,
            nova_tentativa_minutos=30,
            meta_pontualidade_minutos=30,
            fuso_padrao="America/Sao_Paulo",
            limite_silencio_vigia_minutos=60,
        ),
        armazem=armazem,
        execucoes_anteriores=(),
        publicados_por_plataforma={PLATAFORMA: ()},
        posts_da_serie=tuple(_irmao(n, P.PUBLICADO) for n in range(1, ORDEM)),
        conexoes={
            PLATAFORMA: Conexao(
                PLATAFORMA, ConexaoEstado.ATIVA, expira_em=AGORA + timedelta(days=30)
            )
        },
        plataformas_com_adaptador=frozenset({PLATAFORMA}),
        tipos_por_formato={"Feed": {PLATAFORMA: "imagem"}},
        problemas_de_midia={PLATAFORMA: ()},
    )


def _com_serie(contexto: ContextoGuarda, *irmaos: Post) -> ContextoGuarda:
    return dataclasses.replace(contexto, posts_da_serie=tuple(irmaos))


def _no_post(contexto: ContextoGuarda, **campos: Any) -> ContextoGuarda:
    assert contexto.post is not None
    return dataclasses.replace(contexto, post=dataclasses.replace(contexto.post, **campos))


def _libera(contexto: ContextoGuarda) -> bool:
    """A verificação 7 passa E nenhuma verificação recusa (a guarda libera)."""
    return _serie(contexto, PLATAFORMA) is None and _primeira_recusa(contexto, PLATAFORMA) is None


def _bloqueia(contexto: ContextoGuarda) -> bool:
    if _serie(contexto, PLATAFORMA) is not M.SERIE_BLOQUEADA:
        return False
    resultado = avaliar(contexto, PLATAFORMA)  # recusa sem levantar
    return resultado.permitido is False and resultado.motivos == (M.SERIE_BLOQUEADA,)


# --------------------------------------------------------------------------- base


def test_contexto_base_libera(contexto: ContextoGuarda) -> None:
    assert _libera(contexto)


# --------------------------------------------------------------------------- liberação manual


@pytest.mark.parametrize(
    "papel_bloqueador", [P.FALHA, P.CANCELADO, P.AUTORIZADO], ids=lambda p: p.value
)
def test_liberacao_manual_pelo_status_do_post_que_bloqueou(
    contexto: ContextoGuarda, papel_bloqueador: PapelEstagio
) -> None:
    """RF-27: o usuário muda, no Notion, o estágio do post que bloqueou para o de papel
    ``publicado``; na execução seguinte (contexto relido) o post seguinte é liberado. Nenhum
    estado extra é guardado: é o mesmo contexto, só com o bloqueador relido."""
    irmaos = [_irmao(n, P.PUBLICADO) for n in range(1, ORDEM)]
    irmaos[2] = _irmao(3, papel_bloqueador)
    bloqueado = _com_serie(contexto, *irmaos)
    assert _bloqueia(bloqueado)

    irmaos[2] = dataclasses.replace(irmaos[2], estagio=_estagio(P.PUBLICADO))
    assert _libera(_com_serie(bloqueado, *irmaos))


@pytest.mark.parametrize(
    "novo_papel",
    [P.CANCELADO, P.FALHA, P.PRONTO, P.PAUTA, P.AUTORIZADO],
    ids=lambda p: p.value,
)
def test_mudar_bloqueador_para_papel_que_nao_e_publicado_nao_libera(
    contexto: ContextoGuarda, novo_papel: PapelEstagio
) -> None:
    """Interpretação conservadora: só o papel ``publicado`` libera (falha e cancelamento
    bloqueiam pela própria RF-27)."""
    irmaos = [_irmao(n, P.PUBLICADO) for n in range(1, ORDEM)]
    irmaos[0] = _irmao(1, novo_papel)
    assert _bloqueia(_com_serie(contexto, *irmaos))


def test_liberar_um_bloqueador_nao_libera_se_ha_outro(contexto: ContextoGuarda) -> None:
    irmaos = [_irmao(n, P.PUBLICADO) for n in range(1, ORDEM)]
    irmaos[0] = _irmao(1, P.FALHA)
    irmaos[3] = _irmao(4, P.CANCELADO)
    assert _bloqueia(_com_serie(contexto, *irmaos))
    irmaos[0] = _irmao(1, P.PUBLICADO)
    assert _bloqueia(_com_serie(contexto, *irmaos))  # o 4 ainda bloqueia
    irmaos[3] = _irmao(4, P.PUBLICADO)
    assert _libera(_com_serie(contexto, *irmaos))


# --------------------------------------------------------------------------- bordas que liberam


def test_serie_de_um_post_so(contexto: ContextoGuarda) -> None:
    assert _libera(_no_post(_com_serie(contexto), ordem_na_serie=1))


def test_primeiro_da_serie_com_seguintes_nao_publicados(contexto: ContextoGuarda) -> None:
    ctx = _no_post(contexto, ordem_na_serie=1)
    ctx = _com_serie(ctx, _irmao(2, P.FALHA), _irmao(3, P.CANCELADO), _irmao(4, P.PRONTO))
    assert _libera(ctx)


def test_ordem_com_lacunas(contexto: ContextoGuarda) -> None:
    ctx = _com_serie(contexto, _irmao(1, P.PUBLICADO), _irmao(3, P.PUBLICADO), _irmao(9, P.FALHA))
    assert _libera(ctx)


def test_posteriores_nao_bloqueiam(contexto: ContextoGuarda) -> None:
    ctx = _com_serie(contexto, *contexto.posts_da_serie, _irmao(6, P.FALHA), _irmao(7, P.CANCELADO))
    assert _libera(ctx)


def test_o_proprio_post_na_lista_nao_bloqueia_a_si_mesmo(contexto: ContextoGuarda) -> None:
    proprio = _irmao(ORDEM, P.AUTORIZADO, id_=ID_POST)
    assert _libera(_com_serie(contexto, *contexto.posts_da_serie, proprio))


def test_post_de_outra_serie_misturado_nao_conta(contexto: ContextoGuarda) -> None:
    misturados = (
        _irmao(2, P.FALHA, OUTRA_CONTINUA),  # anterior por número, mas de outra série
        _irmao(ORDEM, P.CANCELADO, SOLTA),  # mesma ordem, outra série: não é repetição
        _irmao(None, P.PAUTA, None),  # post único, sem ordem
    )
    assert _libera(_com_serie(contexto, *contexto.posts_da_serie, *misturados))


def test_serie_sem_continuidade_nunca_bloqueia(contexto: ContextoGuarda) -> None:
    ctx = _no_post(contexto, serie=SOLTA, ordem_na_serie=None)
    ctx = _com_serie(ctx, _irmao(1, P.FALHA, SOLTA), _irmao(1, P.CANCELADO, SOLTA))
    assert _serie(ctx, PLATAFORMA) is None


def test_post_sem_serie_nao_bloqueia(contexto: ContextoGuarda) -> None:
    ctx = _no_post(contexto, serie=None, ordem_na_serie=None)
    assert _serie(_com_serie(ctx, _irmao(1, P.FALHA)), PLATAFORMA) is None


# --------------------------------------------------------------------------- bordas que bloqueiam


def test_anterior_publicado_e_anterior_com_falha(contexto: ContextoGuarda) -> None:
    ctx = _com_serie(contexto, _irmao(1, P.PUBLICADO), _irmao(2, P.FALHA), _irmao(3, P.PUBLICADO))
    assert _bloqueia(ctx)


def test_anterior_cancelado(contexto: ContextoGuarda) -> None:
    assert _bloqueia(_com_serie(contexto, _irmao(4, P.CANCELADO)))


def test_anterior_ainda_confirmado_perdeu_a_janela(contexto: ContextoGuarda) -> None:
    assert _bloqueia(_com_serie(contexto, _irmao(1, P.AUTORIZADO)))


@pytest.mark.parametrize(
    "irmaos",
    [
        pytest.param((_irmao(ORDEM, P.PUBLICADO),), id="ordem-igual-a-do-post"),
        pytest.param((_irmao(2, P.PUBLICADO), _irmao(2, P.PUBLICADO)), id="ordem-repetida"),
        pytest.param((_irmao(None, P.PUBLICADO),), id="irmao-sem-ordem"),
        pytest.param((_irmao(0, P.PUBLICADO),), id="irmao-ordem-zero"),
        pytest.param((_irmao(-1, P.PUBLICADO),), id="irmao-ordem-negativa"),
        pytest.param((_irmao(9, P.PUBLICADO, id_="x"), "nao-e-post"), id="item-nao-e-post"),
    ],
)
def test_ordem_inconsistente_recusa(contexto: ContextoGuarda, irmaos: tuple[Any, ...]) -> None:
    assert _bloqueia(_com_serie(contexto, *irmaos))


@pytest.mark.parametrize("ordem", [None, 0, True, 2.0], ids=repr)
def test_continuidade_com_ordem_invalida_no_post_recusa(
    contexto: ContextoGuarda, ordem: object
) -> None:
    assert _serie(_no_post(contexto, ordem_na_serie=ordem), PLATAFORMA) is M.SERIE_BLOQUEADA


@pytest.mark.parametrize(
    "config",
    [
        pytest.param(_configuracao(a=Serie(SOLTA, False)), id="serie-fora-da-config"),
        pytest.param(
            _configuracao(a=Serie(CONTINUA, True), b=Serie(CONTINUA, True)), id="serie-repetida"
        ),
    ],
)
def test_serie_nao_resolvida_na_configuracao_recusa(
    contexto: ContextoGuarda, config: Configuracao
) -> None:
    assert _bloqueia(dataclasses.replace(contexto, configuracao=config))


@pytest.mark.parametrize("serie", ["", "   ", 7], ids=repr)
def test_nome_de_serie_invalido_recusa(contexto: ContextoGuarda, serie: object) -> None:
    assert _serie(_no_post(contexto, serie=serie), PLATAFORMA) is M.SERIE_BLOQUEADA


def test_posts_da_serie_de_tipo_inesperado_recusa(contexto: ContextoGuarda) -> None:
    ctx = dataclasses.replace(contexto, posts_da_serie=None)  # type: ignore[arg-type]
    assert _serie(ctx, PLATAFORMA) is M.SERIE_BLOQUEADA


def test_detalhe_nao_vaza_nome_da_serie_nem_conteudo(contexto: ContextoGuarda) -> None:
    resultado = avaliar(_com_serie(contexto, _irmao(1, P.FALHA)), PLATAFORMA)
    assert resultado.motivos == (M.SERIE_BLOQUEADA,)
    for texto in (resultado.detalhe or "", repr(resultado)):
        for proibido in (CONTINUA, TITULO, ARTE, "Parte 1"):
            assert proibido not in texto


def test_verificacao_6_vem_antes_da_serie(
    contexto: ContextoGuarda,
) -> None:
    """A verificação 6 (ordem por plataforma) continua antes da 7."""
    ctx = dataclasses.replace(
        _com_serie(contexto, _irmao(1, P.FALHA)),
        publicados_por_plataforma={
            PLATAFORMA: (PublicacaoConhecida("post-ficticio-9", AGENDADO + timedelta(hours=1)),)
        },
    )
    assert avaliar(ctx, PLATAFORMA).motivos == (M.POST_POSTERIOR_JA_SAIU,)
