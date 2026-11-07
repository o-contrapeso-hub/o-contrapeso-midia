"""Guarda de publicação (plano 4.1; RF-10 a RF-12, RF-14, RF-18, RF-24, RF-26, RF-27,
RF-37, RF-39, RF-43; regra I-2).

Implementação em partes: T030 fixou tipos, assinatura e contrato (``tests/test_guarda.py``);
T031 (parte 1) implementa as verificações 1 a 5; T032 a T034 (partes 2 a 4) acrescentam as
verificações 6 a 10 em ``_VERIFICACOES``, na ordem canônica.

**Guarda incompleta nunca libera (decisão de segurança da T031).** Enquanto
``_PARTES_IMPLEMENTADAS < _PARTES_TOTAIS``, um post que passa em TODAS as verificações já
existentes NÃO é permitido: ``avaliar`` levanta ``GuardaIncompleta`` (subclasse de
``NotImplementedError``) indicando a parte pendente. É a única exceção que ``avaliar`` deixa sair,
e só existe até a T034: ela torna impossível que uma guarda parcial devolva "permitido" (nenhum
post sai em produção) e mantém a suíte verde sob o ``xfail(raises=NotImplementedError)`` dos
testes. Recusar com um motivo qualquer foi descartado: atribuiria a recusa a uma verificação que
ainda não existe (motivo falso no registro de execuções). A T034 remove ``GuardaIncompleta``.

Contrato:

* **Função pura e determinística.** Sem rede, sem relógio próprio (nunca ``datetime.now``), sem
  escrita. O chamador resolve o mundo externo (relê o post e a configuração no Notion, consulta
  execuções, conexões e publicações, valida a mídia com o adaptador) e entrega tudo em
  ``ContextoGuarda``. A única leitura feita aqui é o recálculo do selo no ``armazem``.
* **Um motivo só.** Devolve ``ResultadoGuarda.permitir()`` ou ``ResultadoGuarda.recusar(m)``
  com UM ``MotivoRecusa``: o da primeira verificação que falha, na ordem canônica abaixo.
* **Falha segura.** ``avaliar`` nunca levanta exceção: dado ausente, inconsistente, de tipo
  inesperado ou erro do armazém viram RECUSA. Nunca libera na dúvida. ``ContextoGuarda`` não
  valida nada no construtor, de propósito: quem valida é ``avaliar``.
* **I-1.** O ``detalhe`` do resultado nunca traz credencial nem conteúdo do post (título,
  corpo, seções, caminhos de arte, selo); no máximo o código do motivo e nomes de campos.

Ordem canônica (plano 4.1), parando na primeira que falhar:

1. ``LEITURA_FALHOU`` (post ou configuração não lidos) e depois ``CONFIG_INCONSISTENTE``
   (problemas registrados, zero ou mais de um estágio autorizador, nome diferente de
   "confirmado" ou nome reservado em outro papel) (RF-39);
2. ``ESTAGIO_NAO_AUTORIZADO`` (RF-10);
3. ``SEM_DATA`` e depois ``SEM_HORA`` (RF-10);
4. ``DATA_FUTURA`` (RF-11);
5. ``SELO_AUSENTE`` e depois ``SELO_DIVERGENTE`` (RF-26);
6. ``FORA_DA_JANELA`` e depois ``POST_POSTERIOR_JA_SAIU``, por plataforma (RF-24, A-010);
7. ``SERIE_BLOQUEADA`` (RF-27);
8. ``SEM_ADAPTADOR`` (RF-37) e depois ``CONEXAO_INVALIDA`` (RF-18);
9. ``FORMATO_SEM_TIPO`` (RF-43) e depois ``ARTE_INVALIDA`` (RF-14);
10. ``EXECUCAO_EXISTENTE`` (RF-12, RF-38).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from publicador.artes.armazem import ArmazemDeArtes
from publicador.core.config import Parametros
from publicador.core.confirmacao import exigir_estagio_autorizador
from publicador.core.modelos import (
    Conexao,
    Estagio,
    Execucao,
    MotivoRecusa,
    PapelEstagio,
    Post,
    ResultadoGuarda,
)
from publicador.core.selo import verificar_selo
from publicador.notion.bancos import Configuracao

__all__ = ["ContextoGuarda", "GuardaIncompleta", "PublicacaoConhecida", "avaliar"]


@dataclass(frozen=True)
class PublicacaoConhecida:
    """Post que JÁ SAIU numa plataforma: só identificador e horário agendado (sem conteúdo)."""

    post_id: str
    agendado_para: datetime


@dataclass(frozen=True)
class ContextoGuarda:
    """Tudo o que a guarda precisa, já resolvido pelo chamador (sem rede dentro da guarda).

    Chaves de plataforma usam o mesmo texto de ``Post.plataformas``.
    """

    # Post relido do Notion agora; ``None`` = a leitura falhou.
    post: Post | None
    # Configuração relida (estágios, séries, problemas); ``None`` = a leitura falhou.
    configuracao: Configuracao | None
    # Instante atual, com fuso, injetado pelo chamador.
    agora: datetime
    parametros: Parametros
    # Armazém para recalcular o selo (lê as artes).
    armazem: ArmazemDeArtes
    # Execuções anteriores do MESMO post e plataforma, de qualquer data (RF-38).
    execucoes_anteriores: tuple[Execucao, ...]
    # Por plataforma: posts que já saíram nela (RF-24, regra de ordem por plataforma).
    publicados_por_plataforma: Mapping[str, tuple[PublicacaoConhecida, ...]]
    # Os demais posts da mesma série do post, relidos (vazio se o post não tem série) (RF-27).
    posts_da_serie: tuple[Post, ...]
    # Último estado conhecido da conexão por plataforma; ausente = inválida (RF-18).
    conexoes: Mapping[str, Conexao]
    # Plataformas com adaptador disponível (RF-37).
    plataformas_com_adaptador: frozenset[str]
    # Nome do formato -> plataforma -> tipo de publicação (RF-43, A-023).
    tipos_por_formato: Mapping[str, Mapping[str, str]]
    # Problemas de mídia por plataforma (``validar_midia`` do adaptador); ausente = não validado.
    problemas_de_midia: Mapping[str, tuple[str, ...]]


class GuardaIncompleta(NotImplementedError):
    """Todas as verificações existentes passaram, mas faltam partes da guarda: NÃO libera.

    Temporária (T031 a T033); some na T034, quando ``avaliar`` passa a nunca levantar.
    """


# Uma verificação olha UMA condição e devolve o motivo da recusa ou ``None`` (passou).
Verificacao = Callable[["ContextoGuarda", str], MotivoRecusa | None]

M = MotivoRecusa

# Detalhes fixos (I-1): só o código do motivo e nomes de campos, nunca conteúdo do post.
_DETALHES: Final[Mapping[MotivoRecusa, str]] = {
    M.LEITURA_FALHOU: "post ou configuração não lidos do Notion",
    M.CONFIG_INCONSISTENTE: "configuração de estágios inconsistente (RF-39)",
    M.ESTAGIO_NAO_AUTORIZADO: "estágio do post não é o autorizador 'confirmado'",
    M.SEM_DATA: "post sem data e hora",
    M.SEM_HORA: "post sem hora (ou sem fuso)",
    M.DATA_FUTURA: "horário agendado no futuro (ou instante atual sem fuso)",
    M.SELO_AUSENTE: "post sem selo de confirmação",
    M.SELO_DIVERGENTE: "selo de confirmação não confere com o recálculo",
}


def _tem_fuso(valor: object) -> bool:
    return (
        isinstance(valor, datetime) and valor.tzinfo is not None and (valor.utcoffset() is not None)
    )


# --------------------------------------------------------------------------- parte 1 (T031)


def _leitura(contexto: ContextoGuarda, _plataforma: str) -> MotivoRecusa | None:
    """1a. Post e configuração relidos agora (``None`` ou tipo inesperado = leitura falhou)."""
    if not isinstance(contexto.post, Post) or not isinstance(contexto.configuracao, Configuracao):
        return M.LEITURA_FALHOU
    return None


def _autorizador(contexto: ContextoGuarda) -> Estagio | None:
    """O único estágio autorizador da configuração íntegra, ou ``None`` (qualquer problema)."""
    configuracao = contexto.configuracao
    if not isinstance(configuracao, Configuracao):
        return None
    try:
        _, estagio = exigir_estagio_autorizador(configuracao)
    except Exception:  # ConfirmacaoRecusada ou dado corrompido: falha segura
        return None
    return estagio


def _configuracao(contexto: ContextoGuarda, _plataforma: str) -> MotivoRecusa | None:
    """1b. Exatamente um estágio autorizador, chamado "confirmado", sem problemas (RF-39)."""
    return M.CONFIG_INCONSISTENTE if _autorizador(contexto) is None else None


def _estagio(contexto: ContextoGuarda, _plataforma: str) -> MotivoRecusa | None:
    """2. O post está no estágio autorizador da configuração (papel E nome) (RF-10)."""
    post = contexto.post
    autorizador = _autorizador(contexto)
    if not isinstance(post, Post) or autorizador is None:
        return M.ESTAGIO_NAO_AUTORIZADO
    estagio = post.estagio
    if (
        not isinstance(estagio, Estagio)
        or estagio.papel is not PapelEstagio.AUTORIZADO
        or estagio != autorizador
    ):
        return M.ESTAGIO_NAO_AUTORIZADO
    return None


def _data(contexto: ContextoGuarda, _plataforma: str) -> MotivoRecusa | None:
    """3a. Há data: ``publicar_em`` preenchido, ou só o dia (``somente_data``) (RF-10)."""
    post = contexto.post
    if not isinstance(post, Post):
        return M.SEM_DATA
    if post.somente_data is True:
        return None  # tem o dia; a falta da hora é a verificação seguinte
    return None if isinstance(post.publicar_em, datetime) else M.SEM_DATA


def _hora(contexto: ContextoGuarda, _plataforma: str) -> MotivoRecusa | None:
    """3b. Há hora: não é "só data" e ``publicar_em`` tem fuso (RF-10)."""
    post = contexto.post
    if not isinstance(post, Post) or post.somente_data is not False:
        return M.SEM_HORA
    return None if _tem_fuso(post.publicar_em) else M.SEM_HORA


def _futuro(contexto: ContextoGuarda, _plataforma: str) -> MotivoRecusa | None:
    """4. ``publicar_em <= agora`` em instantes absolutos; ``agora`` sem fuso recusa (RF-11)."""
    post = contexto.post
    agora = contexto.agora
    agendado = post.publicar_em if isinstance(post, Post) else None
    if not isinstance(agendado, datetime) or not _tem_fuso(agendado) or not _tem_fuso(agora):
        return M.DATA_FUTURA
    return M.DATA_FUTURA if agendado > agora else None


def _selo_presente(contexto: ContextoGuarda, _plataforma: str) -> MotivoRecusa | None:
    """5a. Há selo gravado (não vazio, não só espaços) (RF-26)."""
    post = contexto.post
    if not isinstance(post, Post) or not isinstance(post.selo, str) or not post.selo.strip():
        return M.SELO_AUSENTE
    return None


def _selo_confere(contexto: ContextoGuarda, _plataforma: str) -> MotivoRecusa | None:
    """5b. O selo recalculado AGORA (lê as artes) é igual ao gravado (RF-26).

    Selo malformado ou de outra versão, ``SeloImpossivel``, arte ausente, variação inválida e
    erro do armazém: tudo vira ``SELO_DIVERGENTE`` (não há como provar que nada mudou).
    """
    post = contexto.post
    if not isinstance(post, Post):
        return M.SELO_DIVERGENTE
    try:
        confere = verificar_selo(post, contexto.armazem, post.selo)
    except Exception:  # falha segura: qualquer erro no recálculo recusa
        return M.SELO_DIVERGENTE
    return None if confere is True else M.SELO_DIVERGENTE


# Ordem canônica (plano 4.1). Cada item: (verificação, motivo usado se ela levantar exceção).
# As T032 a T034 acrescentam as verificações 6 a 10 AO FINAL, na ordem do docstring do módulo.
_VERIFICACOES: Final[tuple[tuple[Verificacao, MotivoRecusa], ...]] = (
    (_leitura, M.LEITURA_FALHOU),  # 1
    (_configuracao, M.CONFIG_INCONSISTENTE),  # 1
    (_estagio, M.ESTAGIO_NAO_AUTORIZADO),  # 2
    (_data, M.SEM_DATA),  # 3
    (_hora, M.SEM_HORA),  # 3
    (_futuro, M.DATA_FUTURA),  # 4
    (_selo_presente, M.SELO_AUSENTE),  # 5
    (_selo_confere, M.SELO_DIVERGENTE),  # 5
)

# Parte 1 = T031 (verificações 1 a 5); partes 2 a 4 = T032 a T034 (verificações 6 a 10).
_PARTES_IMPLEMENTADAS: Final = 1
_PARTES_TOTAIS: Final = 4


def _primeira_recusa(contexto: ContextoGuarda, plataforma: str) -> MotivoRecusa | None:
    for verificacao, motivo_em_erro in _VERIFICACOES:
        try:
            motivo = verificacao(contexto, plataforma)
        except Exception:  # falha segura: erro inesperado recusa com o motivo da verificação
            return motivo_em_erro
        if motivo is not None:
            return motivo if isinstance(motivo, MotivoRecusa) else motivo_em_erro
    return None


def avaliar(contexto: ContextoGuarda, plataforma: str) -> ResultadoGuarda:
    """Decide se ``contexto.post`` pode ser publicado AGORA em ``plataforma``.

    Função pura; recusa na primeira verificação que falhar (ordem canônica), com UM motivo e
    detalhe fixo (I-1). Enquanto a guarda estiver incompleta, quem passa em todas as
    verificações existentes NÃO é liberado: levanta ``GuardaIncompleta`` (ver docstring do
    módulo). Fora isso, nunca levanta exceção (falha segura).
    """
    motivo = _primeira_recusa(contexto, plataforma)
    if motivo is not None:
        return ResultadoGuarda.recusar(motivo, detalhe=_DETALHES.get(motivo, motivo.value))
    if _PARTES_IMPLEMENTADAS < _PARTES_TOTAIS:
        raise GuardaIncompleta(
            f"guarda incompleta: parte {_PARTES_IMPLEMENTADAS + 1} de {_PARTES_TOTAIS} "
            "(verificações 6 a 10, T032 a T034) ainda não implementada; nada é liberado"
        )
    return ResultadoGuarda.permitir()
