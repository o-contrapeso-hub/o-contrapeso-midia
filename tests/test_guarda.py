"""Testes da guarda de publicação (T030; plano 4.1; regra I-2).

Escritos ANTES da guarda: hoje todos falham com ``NotImplementedError``. As T031 a T034 a
implementam em partes, sem mudar estes testes.

Interface testada (decisão desta tarefa, ver ``publicador.core.guarda``):

* ``avaliar(contexto: ContextoGuarda, plataforma: str) -> ResultadoGuarda``.
* Função PURA e determinística: o chamador resolve o mundo externo e entrega tudo em
  ``ContextoGuarda`` (post relido ou ``None`` se a leitura falhou; configuração relida ou
  ``None``; ``agora`` com fuso; parâmetros; armazém de artes; execuções anteriores do mesmo post
  e plataforma; posts já publicados por plataforma; demais posts da série; conexões por
  plataforma; plataformas com adaptador; tipo de cada formato por plataforma; problemas de mídia
  por plataforma). Relógio injetado: nada aqui usa ``datetime.now``.
* Resultado: permitido, ou recusado com EXATAMENTE UM ``MotivoRecusa``, o da primeira
  verificação que falha na ordem abaixo.
* Falha segura: ``avaliar`` nunca levanta exceção; entrada ausente ou inconsistente = recusa.

Ordem canônica das 10 verificações (plano 4.1), parando na primeira que falhar:

 1. ``LEITURA_FALHOU`` e depois ``CONFIG_INCONSISTENTE`` (RF-39);
 2. ``ESTAGIO_NAO_AUTORIZADO`` (RF-10);
 3. ``SEM_DATA`` e depois ``SEM_HORA`` (RF-10);
 4. ``DATA_FUTURA`` (RF-11);
 5. ``SELO_AUSENTE`` e depois ``SELO_DIVERGENTE`` (RF-26);
 6. ``FORA_DA_JANELA`` e depois ``POST_POSTERIOR_JA_SAIU``, por plataforma (RF-24, A-010);
 7. ``SERIE_BLOQUEADA`` (RF-27);
 8. ``SEM_ADAPTADOR`` (RF-37) e depois ``CONEXAO_INVALIDA`` (RF-18);
 9. ``FORMATO_SEM_TIPO`` (RF-43) e depois ``ARTE_INVALIDA`` (RF-14). O plano cita a arte
    primeiro, mas a compatibilidade da arte depende do tipo da plataforma (e a T034 lista
    formato antes de arte); por isso o formato vem antes;
10. ``EXECUCAO_EXISTENTE`` (RF-12, RF-38).

Decisões fixadas aqui: a janela de 2 horas é INCLUSIVA (exatamente 120 min depois ainda
publica; 120 min e 1 s não); ``agora == publicar_em`` publica (não é futuro); execuções
anteriores ``RECUSADA`` e ``SIMULADA`` não bloqueiam; ``SUCESSO``, ``SUCESSO_MODO_PRIVADO``,
``INICIADA`` e ``INCERTO`` bloqueiam, de qualquer data.

Construção: ``_contexto_valido`` é o caminho feliz (a guarda libera); cada caso aplica UMA
mutação mínima que só quebra a verificação testada. Nenhum teste acessa a rede; todos os
valores são fictícios (I-1).
"""

from __future__ import annotations

import copy
import dataclasses
import socket
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from publicador.artes.armazem import ArmazemLocal, ErroAcessoArmazem
from publicador.core.config import Parametros
from publicador.core.guarda import ContextoGuarda, PublicacaoConhecida, avaliar
from publicador.core.modelos import (
    Conexao,
    ConexaoEstado,
    EstadoExecucao,
    Estagio,
    Execucao,
    MotivoRecusa,
    PapelEstagio,
    Post,
    ResultadoGuarda,
    Serie,
)
from publicador.core.selo import calcular_selo
from publicador.notion.bancos import Configuracao

M = MotivoRecusa
SAO_PAULO = ZoneInfo("America/Sao_Paulo")
AGENDADO = datetime(2026, 11, 7, 18, 30, tzinfo=SAO_PAULO)
AGORA = AGENDADO + timedelta(minutes=10)
PLATAFORMA = "Instagram"
OUTRA = "TikTok"

# Valores fictícios usados para provar que o resultado não vaza conteúdo nem credencial.
TOKEN_FICTICIO = "FICTICIO-token-nao-real-0000"
TITULO = "Título fictício que não pode vazar"
CORPO = f"Corpo fictício que não pode vazar. {TOKEN_FICTICIO}"
SECAO_IG = "Legenda fictícia do Instagram que não pode vazar"

ARTE_1 = "serie-ficticia/2026/11/arte_01.png"
ARTE_IG = "serie-ficticia/2026/11/arte_so_instagram.png"
CONTEUDOS: dict[str, bytes] = {
    ARTE_1: b"\x89PNG conteudo ficticio 01",
    ARTE_IG: b"\x89PNG conteudo ficticio so instagram",
}

SERIE_CONTINUA = "Série de continuidade fictícia"
SERIE_SOLTA = "Série solta fictícia"

ESTAGIOS: dict[str, Estagio] = {
    "est-pauta": Estagio("banco de pautas", PapelEstagio.PAUTA),
    "est-pronto": Estagio("posts prontos", PapelEstagio.PRONTO),
    "est-confirmado": Estagio("confirmado", PapelEstagio.AUTORIZADO),
    "est-publicado": Estagio("publicado", PapelEstagio.PUBLICADO),
    "est-falha": Estagio("falha na publicação", PapelEstagio.FALHA),
    "est-cancelado": Estagio("cancelado", PapelEstagio.CANCELADO),
}
AUTORIZADO = ESTAGIOS["est-confirmado"]

# T030: a guarda ainda não existe (``avaliar`` levanta NotImplementedError). Para o CI não ficar
# vermelho até as T031 a T034, os testes ficam como "falha esperada". REMOVER este marcador na
# T034 (quando a guarda estiver completa): a partir daí todos devem passar de verdade.
pytestmark = pytest.mark.xfail(raises=NotImplementedError, strict=False, reason="guarda T031-T034")


def _estagio(papel: PapelEstagio) -> Estagio:
    (achado,) = [e for e in ESTAGIOS.values() if e.papel is papel]
    return achado


def _estagio_bruto(nome: str, papel: PapelEstagio) -> Estagio:
    """Estágio corrompido que o construtor recusaria (simula dado inconsistente do Notion)."""
    estagio = object.__new__(Estagio)
    object.__setattr__(estagio, "nome", nome)
    object.__setattr__(estagio, "papel", papel)
    return estagio


def _configuracao(**mudancas: Any) -> Configuracao:
    base: dict[str, Any] = {
        "estagios": dict(ESTAGIOS),
        "formatos": {"fmt-feed": "Feed", "fmt-reels": "Reels"},
        "series": {
            "ser-continua": Serie(SERIE_CONTINUA, continuidade=True),
            "ser-solta": Serie(SERIE_SOLTA, continuidade=False),
        },
        "temas": {"tema-1": "Energia"},
        "problemas": (),
    }
    base.update(mudancas)
    return Configuracao(**base)


def _post_base(**mudancas: Any) -> Post:
    base: dict[str, Any] = {
        "id": "post-ficticio-0002",
        "titulo": TITULO,
        "corpo": CORPO,
        "estagio": AUTORIZADO,
        "plataformas": (PLATAFORMA, OUTRA),
        "formato": "Feed",
        "serie": SERIE_CONTINUA,
        "ordem_na_serie": 2,
        "tema": "Energia",
        "publicar_em": AGENDADO,
        "artes": (ARTE_1, f"instagram:{ARTE_IG}"),
        "selo": None,
        "confirmado_em": AGENDADO - timedelta(days=1),
        "editado_em": AGENDADO - timedelta(days=1, minutes=5),
        "secoes_por_plataforma": {PLATAFORMA: SECAO_IG},
        "somente_data": False,
    }
    base.update(mudancas)
    return Post(**base)


def _irmao(ordem: int, papel: PapelEstagio, serie: str = SERIE_CONTINUA) -> Post:
    """Outro post da mesma série (o post testado é o de ordem 2)."""
    return Post(
        id=f"post-ficticio-irmao-{ordem}",
        titulo=f"Parte {ordem} fictícia",
        corpo="Corpo fictício.",
        estagio=_estagio(papel),
        plataformas=(PLATAFORMA, OUTRA),
        formato="Feed",
        serie=serie,
        ordem_na_serie=ordem,
        publicar_em=AGENDADO + timedelta(days=ordem - 2),
    )


def _execucao(estado: EstadoExecucao, *, dias_antes: int = 0) -> Execucao:
    inicio = AGENDADO - timedelta(days=dias_antes)
    return Execucao(
        chave_idempotencia=f"post-ficticio-0002:{PLATAFORMA}:{inicio.isoformat()}",
        post_id="post-ficticio-0002",
        plataforma=PLATAFORMA,
        estado=estado,
        tentativa=1,
        iniciada_em=inicio,
    )


def _conexao(plataforma: str, estado: ConexaoEstado = ConexaoEstado.ATIVA) -> Conexao:
    return Conexao(
        plataforma=plataforma,
        estado=estado,
        expira_em=AGORA + timedelta(days=30),
        verificado_em=AGORA - timedelta(hours=1),
    )


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def raiz_artes(tmp_path: Path) -> Path:
    raiz = tmp_path / "artes"
    for caminho, conteudo in CONTEUDOS.items():
        destino = raiz / caminho
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(conteudo)
    return raiz


@pytest.fixture
def armazem(raiz_artes: Path) -> ArmazemLocal:
    return ArmazemLocal(raiz_artes)


@pytest.fixture
def contexto(armazem: ArmazemLocal) -> ContextoGuarda:
    return _contexto_valido(armazem)


def _contexto_valido(armazem: ArmazemLocal) -> ContextoGuarda:
    """Caminho feliz: post confirmado, selado, no horário, tudo válido. A guarda libera."""
    sem_selo = _post_base()
    post = dataclasses.replace(sem_selo, selo=calcular_selo(sem_selo, armazem))
    return ContextoGuarda(
        post=post,
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
        publicados_por_plataforma={
            PLATAFORMA: (PublicacaoConhecida("post-ficticio-irmao-1", AGENDADO - timedelta(1)),),
            OUTRA: (),
        },
        posts_da_serie=(
            _irmao(1, PapelEstagio.PUBLICADO),
            _irmao(3, PapelEstagio.PRONTO),  # posterior ainda não pronto: não bloqueia o 2
        ),
        conexoes={PLATAFORMA: _conexao(PLATAFORMA), OUTRA: _conexao(OUTRA)},
        plataformas_com_adaptador=frozenset({PLATAFORMA, OUTRA}),
        tipos_por_formato={"Feed": {PLATAFORMA: "imagem", OUTRA: "fotos"}},
        problemas_de_midia={PLATAFORMA: (), OUTRA: ()},
    )


# --------------------------------------------------------------------------- mutações

Mutacao = Callable[[ContextoGuarda], ContextoGuarda]


def _com(**campos: Any) -> Mutacao:
    return lambda c: dataclasses.replace(c, **campos)


def _no_post(**campos: Any) -> Mutacao:
    """Muda o post SEM refazer o selo (como uma edição após a confirmação)."""

    def aplicar(c: ContextoGuarda) -> ContextoGuarda:
        assert c.post is not None
        return dataclasses.replace(c, post=dataclasses.replace(c.post, **campos))

    return aplicar


def _no_post_resselado(**campos: Any) -> Mutacao:
    """Muda o post e grava um selo novo e válido (como uma nova confirmação)."""

    def aplicar(c: ContextoGuarda) -> ContextoGuarda:
        assert c.post is not None
        novo = dataclasses.replace(c.post, selo=None, **campos)
        return dataclasses.replace(
            c, post=dataclasses.replace(novo, selo=calcular_selo(novo, c.armazem))
        )

    return aplicar


def _na_config(**campos: Any) -> Mutacao:
    return _com(configuracao=_configuracao(**campos))


def _estagios_sem(*ids: str, **extras: Estagio) -> dict[str, Estagio]:
    estagios = {k: v for k, v in ESTAGIOS.items() if k not in ids}
    estagios.update(extras)
    return estagios


def _anterior_da_serie(papel: PapelEstagio) -> Mutacao:
    return _com(posts_da_serie=(_irmao(1, papel), _irmao(3, PapelEstagio.PRONTO)))


def _posterior_ja_publicado(plataforma: str) -> Mutacao:
    def aplicar(c: ContextoGuarda) -> ContextoGuarda:
        publicados = dict(c.publicados_por_plataforma)
        publicados[plataforma] = (
            *publicados.get(plataforma, ()),
            PublicacaoConhecida("post-ficticio-0009", AGENDADO + timedelta(minutes=1)),
        )
        return dataclasses.replace(c, publicados_por_plataforma=publicados)

    return aplicar


def _sem_adaptador(c: ContextoGuarda) -> ContextoGuarda:
    return dataclasses.replace(c, plataformas_com_adaptador=frozenset({OUTRA}))


def _conexao_como(conexao: Conexao | None) -> Mutacao:
    def aplicar(c: ContextoGuarda) -> ContextoGuarda:
        conexoes = {k: v for k, v in c.conexoes.items() if k != PLATAFORMA}
        if conexao is not None:
            conexoes[PLATAFORMA] = conexao
        return dataclasses.replace(c, conexoes=conexoes)

    return aplicar


def _execucao_anterior(estado: EstadoExecucao, *, dias_antes: int = 0) -> Mutacao:
    return _com(execucoes_anteriores=(_execucao(estado, dias_antes=dias_antes),))


def _midia_com_problema(c: ContextoGuarda) -> ContextoGuarda:
    # O texto do problema traz um token fictício: o resultado não pode ecoá-lo.
    problema = f"proporção inválida ({TOKEN_FICTICIO})"
    return dataclasses.replace(c, problemas_de_midia={PLATAFORMA: (problema,), OUTRA: ()})


SELO_OUTRO = "v1:" + "0" * 64

# --------------------------------------------------------------------------- tabela

CASOS: list[Any] = [
    # 1. leitura e configuração
    pytest.param(M.LEITURA_FALHOU, _com(post=None), id="leitura-post-falhou"),
    pytest.param(M.LEITURA_FALHOU, _com(configuracao=None), id="leitura-config-falhou"),
    pytest.param(
        M.CONFIG_INCONSISTENTE,
        _na_config(estagios=_estagios_sem("est-confirmado")),
        id="config-zero-autorizadores",
    ),
    pytest.param(
        M.CONFIG_INCONSISTENTE,
        _na_config(
            estagios=_estagios_sem(
                **{"est-confirmado-2": Estagio("confirmado", PapelEstagio.AUTORIZADO)}
            )
        ),
        id="config-dois-autorizadores",
    ),
    pytest.param(
        M.CONFIG_INCONSISTENTE,
        _na_config(
            estagios=_estagios_sem(
                "est-confirmado",
                **{"est-agendado": _estagio_bruto("agendado", PapelEstagio.AUTORIZADO)},
            )
        ),
        id="config-autorizador-com-outro-nome",
    ),
    pytest.param(
        M.CONFIG_INCONSISTENTE,
        _na_config(
            estagios=_estagios_sem(
                **{"est-falso": _estagio_bruto("confirmado", PapelEstagio.PRONTO)}
            )
        ),
        id="config-nome-reservado-em-outro-papel",
    ),
    pytest.param(
        M.CONFIG_INCONSISTENTE,
        _na_config(problemas=("estágio sem papel",)),
        id="config-com-problemas-registrados",
    ),
    # 2. estágio
    pytest.param(
        M.ESTAGIO_NAO_AUTORIZADO,
        _no_post(estagio=ESTAGIOS["est-pronto"]),
        id="estagio-pronto",
    ),
    # 3. data e hora
    pytest.param(M.SEM_DATA, _no_post(publicar_em=None, somente_data=False), id="sem-data"),
    pytest.param(M.SEM_HORA, _no_post(publicar_em=None, somente_data=True), id="sem-hora"),
    # 4. futuro
    pytest.param(M.DATA_FUTURA, _com(agora=AGENDADO - timedelta(minutes=1)), id="futuro-1min"),
    pytest.param(M.DATA_FUTURA, _com(agora=AGENDADO - timedelta(seconds=1)), id="futuro-1s"),
    # 5. selo
    pytest.param(M.SELO_AUSENTE, _no_post(selo=None), id="selo-ausente"),
    pytest.param(M.SELO_AUSENTE, _no_post(selo=""), id="selo-vazio"),
    pytest.param(M.SELO_AUSENTE, _no_post(selo="   "), id="selo-so-espacos"),
    pytest.param(M.SELO_DIVERGENTE, _no_post(selo=SELO_OUTRO), id="selo-de-outro-conteudo"),
    pytest.param(
        M.SELO_DIVERGENTE,
        _no_post(corpo=CORPO + " editado depois"),
        id="selo-corpo-editado-apos-confirmar",
    ),
    pytest.param(
        M.SELO_DIVERGENTE,
        _no_post(plataformas=(PLATAFORMA,), secoes_por_plataforma={}, artes=(ARTE_1,)),
        id="selo-plataformas-editadas-apos-confirmar",
    ),
    # 6. janela e ordem por plataforma
    pytest.param(
        M.FORA_DA_JANELA,
        _com(agora=AGENDADO + timedelta(minutes=120, seconds=1)),
        id="janela-120min-e-1s",
    ),
    pytest.param(M.FORA_DA_JANELA, _com(agora=AGENDADO + timedelta(days=1)), id="janela-1-dia"),
    pytest.param(
        M.POST_POSTERIOR_JA_SAIU,
        _posterior_ja_publicado(PLATAFORMA),
        id="posterior-ja-saiu-na-plataforma",
    ),
    # 7. série de continuidade
    pytest.param(
        M.SERIE_BLOQUEADA, _anterior_da_serie(PapelEstagio.FALHA), id="serie-anterior-falhou"
    ),
    pytest.param(
        M.SERIE_BLOQUEADA,
        _anterior_da_serie(PapelEstagio.CANCELADO),
        id="serie-anterior-cancelado",
    ),
    pytest.param(
        M.SERIE_BLOQUEADA,
        _anterior_da_serie(PapelEstagio.AUTORIZADO),
        id="serie-anterior-ainda-nao-saiu",
    ),
    pytest.param(
        M.SERIE_BLOQUEADA,
        _anterior_da_serie(PapelEstagio.PRONTO),
        id="serie-anterior-nao-confirmado",
    ),
    # 8. adaptador e conexão
    pytest.param(M.SEM_ADAPTADOR, _sem_adaptador, id="plataforma-sem-adaptador"),
    pytest.param(
        M.CONEXAO_INVALIDA,
        _conexao_como(_conexao(PLATAFORMA, ConexaoEstado.INVALIDA)),
        id="conexao-invalida",
    ),
    pytest.param(
        M.CONEXAO_INVALIDA,
        _conexao_como(_conexao(PLATAFORMA, ConexaoEstado.EXPIRADA)),
        id="conexao-expirada",
    ),
    pytest.param(
        M.CONEXAO_INVALIDA,
        _conexao_como(
            dataclasses.replace(_conexao(PLATAFORMA), expira_em=AGORA - timedelta(minutes=1))
        ),
        id="conexao-ativa-mas-vencida",
    ),
    pytest.param(M.CONEXAO_INVALIDA, _conexao_como(None), id="conexao-desconhecida"),
    # 9. formato e arte
    pytest.param(
        M.FORMATO_SEM_TIPO,
        _com(tipos_por_formato={"Feed": {OUTRA: "fotos"}}),
        id="formato-sem-tipo-na-plataforma",
    ),
    pytest.param(
        M.FORMATO_SEM_TIPO,
        _com(tipos_por_formato={"Reels": {PLATAFORMA: "reels"}}),
        id="formato-fora-do-mapeamento",
    ),
    pytest.param(M.FORMATO_SEM_TIPO, _no_post_resselado(formato=None), id="post-sem-formato"),
    pytest.param(M.ARTE_INVALIDA, _midia_com_problema, id="arte-incompativel"),
    # 10. idempotência
    pytest.param(
        M.EXECUCAO_EXISTENTE,
        _execucao_anterior(EstadoExecucao.SUCESSO),
        id="execucao-sucesso",
    ),
    pytest.param(
        M.EXECUCAO_EXISTENTE,
        _execucao_anterior(EstadoExecucao.SUCESSO_MODO_PRIVADO),
        id="execucao-sucesso-modo-privado",
    ),
    pytest.param(
        M.EXECUCAO_EXISTENTE,
        _execucao_anterior(EstadoExecucao.INICIADA),
        id="execucao-iniciada",
    ),
    pytest.param(
        M.EXECUCAO_EXISTENTE,
        _execucao_anterior(EstadoExecucao.INCERTO),
        id="execucao-incerta",
    ),
    pytest.param(
        M.EXECUCAO_EXISTENTE,
        _execucao_anterior(EstadoExecucao.INCERTO, dias_antes=3),
        id="execucao-incerta-de-outra-data",
    ),
    pytest.param(
        M.EXECUCAO_EXISTENTE,
        _execucao_anterior(EstadoExecucao.SUCESSO, dias_antes=3),
        id="execucao-sucesso-de-outra-data",
    ),
]


def _motivo_unico(resultado: ResultadoGuarda) -> MotivoRecusa:
    assert isinstance(resultado, ResultadoGuarda)
    assert resultado.permitido is False
    assert len(resultado.motivos) == 1, resultado.motivos
    return resultado.motivos[0]


def _recusou(resultado: ResultadoGuarda) -> None:
    assert isinstance(resultado, ResultadoGuarda)
    assert resultado.permitido is False
    assert resultado.motivos


# --------------------------------------------------------------------------- caminho feliz


def test_caminho_feliz_libera(contexto: ContextoGuarda) -> None:
    resultado = avaliar(contexto, PLATAFORMA)
    assert resultado.permitido is True
    assert resultado.motivos == ()


@pytest.mark.parametrize(
    "mutacao",
    [
        pytest.param(_com(agora=AGENDADO), id="exatamente-no-horario"),
        pytest.param(_com(agora=AGENDADO + timedelta(minutes=120)), id="janela-inclusiva-120min"),
        pytest.param(_com(agora=AGORA.astimezone(UTC)), id="agora-em-utc"),
        pytest.param(
            _no_post_resselado(publicar_em=AGENDADO.astimezone(UTC)), id="agendado-em-utc"
        ),
        pytest.param(_no_post(serie=None, ordem_na_serie=None), id="post-unico-sem-serie"),
        pytest.param(
            lambda c: _no_post(serie=SERIE_SOLTA)(
                _com(
                    posts_da_serie=(
                        _irmao(1, PapelEstagio.FALHA, SERIE_SOLTA),
                        _irmao(3, PapelEstagio.PRONTO, SERIE_SOLTA),
                    )
                )(c)
            ),
            id="serie-sem-continuidade-nao-bloqueia",
        ),
        pytest.param(_posterior_ja_publicado(OUTRA), id="posterior-saiu-em-outra-plataforma"),
        pytest.param(
            _execucao_anterior(EstadoExecucao.RECUSADA), id="recusa-anterior-nao-bloqueia"
        ),
        pytest.param(
            _execucao_anterior(EstadoExecucao.SIMULADA), id="simulacao-anterior-nao-bloqueia"
        ),
        pytest.param(
            lambda c: dataclasses.replace(
                c, conexoes={**c.conexoes, OUTRA: _conexao(OUTRA, ConexaoEstado.INVALIDA)}
            ),
            id="conexao-de-outra-plataforma-nao-importa",
        ),
    ],
)
def test_variacoes_validas_liberam(contexto: ContextoGuarda, mutacao: Mutacao) -> None:
    resultado = avaliar(mutacao(contexto), PLATAFORMA)
    assert resultado.permitido is True
    assert resultado.motivos == ()


# --------------------------------------------------------------------------- um por motivo


@pytest.mark.parametrize(("motivo", "mutacao"), CASOS)
def test_recusa_um_motivo_por_caso(
    contexto: ContextoGuarda, motivo: MotivoRecusa, mutacao: Mutacao
) -> None:
    assert _motivo_unico(avaliar(mutacao(contexto), PLATAFORMA)) is motivo


def test_tabela_cobre_todos_os_motivos(contexto: ContextoGuarda) -> None:
    """Todo código de ``MotivoRecusa`` tem pelo menos um caso na tabela."""
    assert {caso.values[0] for caso in CASOS} == set(MotivoRecusa)
    # O motivo de cada caso vem da guarda, não da tabela: todos recusam com o seu motivo.
    obtidos = {_motivo_unico(avaliar(caso.values[1](contexto), PLATAFORMA)) for caso in CASOS}
    assert obtidos == set(MotivoRecusa)


@pytest.mark.parametrize(
    "papel", [p for p in PapelEstagio if p is not PapelEstagio.AUTORIZADO], ids=lambda p: p.value
)
def test_todo_papel_diferente_de_autorizado_recusa(
    contexto: ContextoGuarda, papel: PapelEstagio
) -> None:
    mutado = _no_post(estagio=_estagio(papel))(contexto)
    assert _motivo_unico(avaliar(mutado, PLATAFORMA)) is M.ESTAGIO_NAO_AUTORIZADO


def test_arte_com_conteudo_trocado_apos_confirmar_diverge_o_selo(
    contexto: ContextoGuarda, raiz_artes: Path
) -> None:
    (raiz_artes / ARTE_1).write_bytes(b"\x89PNG conteudo ficticio TROCADO")
    assert _motivo_unico(avaliar(contexto, PLATAFORMA)) is M.SELO_DIVERGENTE


# --------------------------------------------------------------------------- ordem


def _encadear(*mutacoes: Mutacao) -> Mutacao:
    def aplicar(c: ContextoGuarda) -> ContextoGuarda:
        for mutacao in mutacoes:
            c = mutacao(c)
        return c

    return aplicar


_ESTAGIO_PRONTO = _no_post(estagio=ESTAGIOS["est-pronto"])
_CONFIG_ZERO = _na_config(estagios=_estagios_sem("est-confirmado"))
_SEM_DATA = _no_post(publicar_em=None, somente_data=False)
_SEM_HORA = _no_post(publicar_em=None, somente_data=True)
_FUTURO = _com(agora=AGENDADO - timedelta(minutes=1))
_SELO_AUSENTE = _no_post(selo=None)
_SELO_DIVERGENTE = _no_post(selo=SELO_OUTRO)
_FORA_DA_JANELA = _com(agora=AGENDADO + timedelta(hours=3))
_POSTERIOR = _posterior_ja_publicado(PLATAFORMA)
_SERIE = _anterior_da_serie(PapelEstagio.FALHA)
_CONEXAO = _conexao_como(_conexao(PLATAFORMA, ConexaoEstado.EXPIRADA))
_SEM_TIPO = _com(tipos_por_formato={"Feed": {OUTRA: "fotos"}})
_EXECUCAO = _execucao_anterior(EstadoExecucao.SUCESSO)


@pytest.mark.parametrize(
    ("esperado", "primeira", "segunda"),
    [
        pytest.param(M.LEITURA_FALHOU, _com(post=None), _CONFIG_ZERO, id="leitura>config"),
        pytest.param(
            M.LEITURA_FALHOU, _com(configuracao=None), _ESTAGIO_PRONTO, id="leitura>estagio"
        ),
        pytest.param(M.CONFIG_INCONSISTENTE, _CONFIG_ZERO, _ESTAGIO_PRONTO, id="config>estagio"),
        pytest.param(M.CONFIG_INCONSISTENTE, _CONFIG_ZERO, _sem_adaptador, id="config>adaptador"),
        pytest.param(M.ESTAGIO_NAO_AUTORIZADO, _ESTAGIO_PRONTO, _SEM_DATA, id="estagio>sem-data"),
        pytest.param(M.ESTAGIO_NAO_AUTORIZADO, _ESTAGIO_PRONTO, _EXECUCAO, id="estagio>execucao"),
        pytest.param(M.SEM_DATA, _SEM_DATA, _SELO_AUSENTE, id="sem-data>selo"),
        pytest.param(M.SEM_HORA, _SEM_HORA, _SELO_AUSENTE, id="sem-hora>selo"),
        pytest.param(M.DATA_FUTURA, _FUTURO, _SELO_AUSENTE, id="futuro>selo"),
        pytest.param(M.DATA_FUTURA, _FUTURO, _POSTERIOR, id="futuro>posterior"),
        pytest.param(M.SELO_AUSENTE, _SELO_AUSENTE, _FORA_DA_JANELA, id="selo-ausente>janela"),
        pytest.param(
            M.SELO_DIVERGENTE, _SELO_DIVERGENTE, _FORA_DA_JANELA, id="selo-divergente>janela"
        ),
        pytest.param(M.SELO_DIVERGENTE, _SELO_DIVERGENTE, _SERIE, id="selo-divergente>serie"),
        pytest.param(M.FORA_DA_JANELA, _FORA_DA_JANELA, _POSTERIOR, id="janela>posterior"),
        pytest.param(M.POST_POSTERIOR_JA_SAIU, _POSTERIOR, _SERIE, id="posterior>serie"),
        pytest.param(M.SERIE_BLOQUEADA, _SERIE, _sem_adaptador, id="serie>adaptador"),
        pytest.param(M.SEM_ADAPTADOR, _sem_adaptador, _CONEXAO, id="adaptador>conexao"),
        pytest.param(M.CONEXAO_INVALIDA, _CONEXAO, _SEM_TIPO, id="conexao>formato"),
        pytest.param(M.FORMATO_SEM_TIPO, _SEM_TIPO, _midia_com_problema, id="formato>arte"),
        pytest.param(M.ARTE_INVALIDA, _midia_com_problema, _EXECUCAO, id="arte>execucao"),
    ],
)
def test_ordem_entre_duas_falhas(
    contexto: ContextoGuarda,
    esperado: MotivoRecusa,
    primeira: Mutacao,
    segunda: Mutacao,
) -> None:
    """Com duas falhas, vence a da verificação anterior, em qualquer ordem de montagem."""
    for mutado in (_encadear(primeira, segunda)(contexto), _encadear(segunda, primeira)(contexto)):
        assert _motivo_unico(avaliar(mutado, PLATAFORMA)) is esperado


# Da última verificação para a primeira; cada passo ACUMULA uma falha anterior na ordem.
CASCATA: list[tuple[MotivoRecusa, Mutacao]] = [
    (M.EXECUCAO_EXISTENTE, _EXECUCAO),
    (M.ARTE_INVALIDA, _midia_com_problema),
    (M.FORMATO_SEM_TIPO, _SEM_TIPO),
    (M.CONEXAO_INVALIDA, _CONEXAO),
    (M.SEM_ADAPTADOR, _sem_adaptador),
    (M.SERIE_BLOQUEADA, _SERIE),
    (M.POST_POSTERIOR_JA_SAIU, _POSTERIOR),
    (M.FORA_DA_JANELA, _FORA_DA_JANELA),
    (M.SELO_DIVERGENTE, _SELO_DIVERGENTE),
    (M.SELO_AUSENTE, _SELO_AUSENTE),
    (M.DATA_FUTURA, _FUTURO),
    (M.SEM_HORA, _SEM_HORA),
    (M.SEM_DATA, _SEM_DATA),
    (M.ESTAGIO_NAO_AUTORIZADO, _ESTAGIO_PRONTO),
    (M.CONFIG_INCONSISTENTE, _CONFIG_ZERO),
    (M.LEITURA_FALHOU, _com(configuracao=None)),
]


@pytest.mark.parametrize("passos", range(1, len(CASCATA) + 1))
def test_ordem_em_cascata(contexto: ContextoGuarda, passos: int) -> None:
    mutado = _encadear(*(mutacao for _, mutacao in CASCATA[:passos]))(contexto)
    assert _motivo_unico(avaliar(mutado, PLATAFORMA)) is CASCATA[passos - 1][0]


# --------------------------------------------------------------------------- falha segura


class _ArmazemIndisponivel:
    def ler(self, caminho: str) -> bytes:
        raise ErroAcessoArmazem("Falha de comunicacao com o armazem (ficticio).")

    def hash_sha256(self, caminho: str) -> str:
        raise ErroAcessoArmazem("Falha de comunicacao com o armazem (ficticio).")

    def existe(self, caminho: str) -> bool:
        raise ErroAcessoArmazem("Falha de comunicacao com o armazem (ficticio).")


class _ArmazemHashInvalido:
    def ler(self, caminho: str) -> bytes:
        return b"qualquer"

    def hash_sha256(self, caminho: str) -> str:
        return "nao-e-sha256"

    def existe(self, caminho: str) -> bool:
        return True


def _com_youtube_completo(c: ContextoGuarda) -> ContextoGuarda:
    """Tudo pronto para YouTube, menos o post: só "fora do post" pode recusar."""
    return dataclasses.replace(
        c,
        conexoes={**c.conexoes, "YouTube": _conexao("YouTube")},
        plataformas_com_adaptador=c.plataformas_com_adaptador | {"YouTube"},
        tipos_por_formato={"Feed": {**c.tipos_por_formato["Feed"], "YouTube": "video"}},
        problemas_de_midia={**c.problemas_de_midia, "YouTube": ()},
        publicados_por_plataforma={**c.publicados_por_plataforma, "YouTube": ()},
    )


FALHAS_SEGURAS: list[Any] = [
    pytest.param(_com(agora=AGORA.replace(tzinfo=None)), PLATAFORMA, id="agora-sem-fuso"),
    pytest.param(_com_youtube_completo, "YouTube", id="plataforma-fora-do-post"),
    pytest.param(lambda c: c, "", id="plataforma-vazia"),
    pytest.param(_no_post(serie="Série inexistente"), PLATAFORMA, id="serie-fora-da-config"),
    pytest.param(
        _na_config(
            series={
                "ser-a": Serie(SERIE_CONTINUA, continuidade=True),
                "ser-b": Serie(SERIE_CONTINUA, continuidade=False),
            }
        ),
        PLATAFORMA,
        id="serie-ambigua-na-config",
    ),
    pytest.param(_no_post(ordem_na_serie=None), PLATAFORMA, id="continuidade-sem-ordem"),
    pytest.param(
        _com(problemas_de_midia={OUTRA: ()}), PLATAFORMA, id="midia-nao-validada-na-plataforma"
    ),
    pytest.param(_com(armazem=_ArmazemIndisponivel()), PLATAFORMA, id="armazem-indisponivel"),
    pytest.param(_com(armazem=_ArmazemHashInvalido()), PLATAFORMA, id="armazem-hash-invalido"),
]


@pytest.mark.parametrize(("mutacao", "plataforma"), FALHAS_SEGURAS)
def test_falha_segura_recusa_e_nao_levanta(
    contexto: ContextoGuarda, mutacao: Mutacao, plataforma: str
) -> None:
    _recusou(avaliar(mutacao(contexto), plataforma))


@pytest.mark.parametrize(
    "selo",
    ["abc", "v2:" + "0" * 64, "V1:" + "0" * 64, "v1:" + "0" * 63, "v1:" + "G" * 64],
)
def test_selo_malformado_recusa(contexto: ContextoGuarda, selo: str) -> None:
    motivo = _motivo_unico(avaliar(_no_post(selo=selo)(contexto), PLATAFORMA))
    assert motivo in (M.SELO_AUSENTE, M.SELO_DIVERGENTE)


def test_arte_apagada_do_armazem_recusa(contexto: ContextoGuarda, raiz_artes: Path) -> None:
    (raiz_artes / ARTE_IG).unlink()
    motivo = _motivo_unico(avaliar(contexto, PLATAFORMA))
    assert motivo in (M.ARTE_INVALIDA, M.SELO_DIVERGENTE)


def test_nao_usa_rede(contexto: ContextoGuarda, monkeypatch: pytest.MonkeyPatch) -> None:
    def proibido(*_: object, **__: object) -> None:
        raise AssertionError("a guarda não pode acessar a rede")

    monkeypatch.setattr(socket.socket, "connect", proibido)
    monkeypatch.setattr(socket, "create_connection", proibido)
    monkeypatch.setattr(socket, "getaddrinfo", proibido)
    assert avaliar(contexto, PLATAFORMA).permitido is True


def test_deterministica_e_sem_efeito_no_contexto(contexto: ContextoGuarda) -> None:
    campos = [f.name for f in dataclasses.fields(ContextoGuarda) if f.name != "armazem"]
    antes = {nome: copy.deepcopy(getattr(contexto, nome)) for nome in campos}
    for mutacao in (lambda c: c, _SERIE, _EXECUCAO):
        alvo = mutacao(contexto)
        assert avaliar(alvo, PLATAFORMA) == avaliar(alvo, PLATAFORMA)
    assert {nome: getattr(contexto, nome) for nome in campos} == antes


# --------------------------------------------------------------------------- I-1


PROIBIDOS = (TOKEN_FICTICIO, TITULO, CORPO, SECAO_IG, ARTE_1, ARTE_IG, SELO_OUTRO)


@pytest.mark.parametrize(("motivo", "mutacao"), CASOS)
def test_resultado_nao_vaza_conteudo_nem_credencial(
    contexto: ContextoGuarda, motivo: MotivoRecusa, mutacao: Mutacao
) -> None:
    mutado = mutacao(contexto)
    resultado = avaliar(mutado, PLATAFORMA)
    textos = [repr(resultado), str(resultado), resultado.detalhe or ""]
    proibidos = list(PROIBIDOS)
    if mutado.post is not None and mutado.post.selo and mutado.post.selo.strip():
        proibidos.append(mutado.post.selo)
    for texto in textos:
        for valor in proibidos:
            assert valor not in texto
