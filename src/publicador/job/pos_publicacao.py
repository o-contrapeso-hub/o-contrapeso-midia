"""Pós-publicação: estágio ``publicado`` ou ``falha`` do post (T039; RF-30, RF-34, RF-35,
A-003, A-022).

Chamada pelo orquestrador DEPOIS de processar todas as plataformas de um post na rodada, em
modo real. O resultado é por plataforma (banco Execuções) e o estágio é do post (A-003).

Regras invioláveis:

* I-2: a pós-publicação NUNCA habilita publicar. Ela só escreve o estágio depois de um
  resultado real já registrado e jamais para ``autorizado`` (o destino é sempre o único
  estágio de papel ``publicado`` ou o único de papel ``falha``; qualquer outro papel é
  recusado no código). Só sai do estágio ``autorizado``: se o post já foi movido por uma
  pessoa (``pronto``, ``cancelado``...), nada é alterado. Simulação nunca altera estágio.
* FALHA SEGURA: só marca ``publicado`` quando TODAS as plataformas do post têm sucesso REAL
  registrado (``sucesso`` ou ``sucesso em modo privado``) e nenhuma tem execução ``incerto``
  ou ``iniciada`` (pode ter saído e não foi confirmada). Na dúvida: ``falha`` ou nada.
* I-1: relatório e logs só têm ids, plataformas, enums e códigos; nunca conteúdo do post.

A verdade vem do banco Execuções (todas as execuções do post em cada plataforma), não só da
rodada: assim a rodada seguinte à de um erro do Notion refaz a conta, e uma nova confirmação
depois de sucesso parcial (A-003) é avaliada com o que já saiu.

Decisões (cada uma coberta por teste):

* **Todas com sucesso** -> ``publicado`` num PATCH único que muda SÓ o estágio. O selo e o
  "Confirmado em" permanecem como prova do que foi confirmado: o post já saiu, e a guarda
  recusa por estágio e por execução existente. Com sucesso em modo privado em alguma
  plataforma o resultado é ``PUBLICADO_MODO_PRIVADO`` (A-022): a marca visível está no
  banco Execuções (Resultado "sucesso em modo privado", código ``modo_privado``) e no
  relatório (``modo_privado``, ``plataformas_privadas``). O banco Posts NÃO tem propriedade
  para a marca e nenhuma foi inventada (pendência: ver o relatório da tarefa).
* **Sucesso em parte** das plataformas, ou falha definitiva (permanente, ou transitória já
  repetida) ou ``incerto`` em alguma -> ``falha`` (A-003, RF-30). O PATCH único muda o
  estágio e LIMPA selo e "Confirmado em" (mesma regra de ``sair_de_autorizado``: fora de
  ``autorizado`` o post não carrega selo válido, então uma nova confirmação exige selo
  novo). As execuções de sucesso permanecem no banco Execuções e continuam bloqueando
  aquelas plataformas (``EXECUCAO_EXISTENTE``): uma nova confirmação publica só as demais.
* **INCERTO** em qualquer plataforma (inclusive ``iniciada`` que ficou sem resultado) -> NUNCA
  ``publicado``; vai para ``falha``, e a execução incerta segue bloqueando a republicação até
  a resolução manual (T040/T056).
* **Falha transitória ainda não repetida** (``erro_transitorio`` na tentativa 1), sem sucesso
  em nenhuma plataforma e sem falha definitiva/incerto: NÃO muda o estágio
  (``AGUARDANDO_NOVA_TENTATIVA``); o post fica em ``autorizado`` para a nova tentativa segura
  da T040, que depois da segunda falha deixa o post cair em ``falha`` por esta mesma regra.
  Se alguma plataforma já saiu, vale a regra do sucesso parcial (``falha``, A-003).
* **Recusa** e execuções ``simulado``/``recusado``/``não executado`` nunca alteram o estágio.
* **Configuração**: exige exatamente um estágio de papel ``publicado`` e um de ``falha`` e
  nenhum problema registrado; senão nada é lido nem alterado (``CONFIG_INVALIDA``).
* **Erro do Notion** ou edição concorrente ao mudar o estágio: reportado, sem desfazer o
  registro da execução, sem repetir a publicação e sem repetir a escrita; a rodada seguinte
  refaz a conta. Idempotência: se o post já está no estágio de destino, nada é escrito.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Final

from publicador.core.confirmacao import propriedades_de_saida
from publicador.core.modelos import EstadoExecucao, Execucao, PapelEstagio, Post
from publicador.core.redacao import obter_logger
from publicador.job.execucoes import CodigoMotivo, execucoes_do_post
from publicador.notion.bancos import Configuracao, IdsBancos
from publicador.notion.cliente import ClienteNotion
from publicador.notion.escrita import EdicaoConcorrente
from publicador.notion.mapeamento import ler_post

__all__ = [
    "DesfechoPos",
    "ResultadoPos",
    "concluir_post",
]

_log = obter_logger("publicador.job.pos_publicacao")

_PROP_ESTAGIO: Final = "Estágio"
_TENTATIVA_FINAL: Final = 2
_SAIU: Final = frozenset({EstadoExecucao.SUCESSO, EstadoExecucao.SUCESSO_MODO_PRIVADO})
_DUVIDA: Final = frozenset({EstadoExecucao.INCERTO, EstadoExecucao.INICIADA})


class DesfechoPos(Enum):
    PUBLICADO = "publicado"
    PUBLICADO_MODO_PRIVADO = "publicado_modo_privado"
    FALHA = "falha"
    SEM_ALTERACAO = "sem_alteracao"
    AGUARDANDO_NOVA_TENTATIVA = "aguardando_nova_tentativa"
    JA_APLICADO = "ja_aplicado"
    CONFIG_INVALIDA = "config_invalida"
    ERRO_LEITURA = "erro_leitura"
    EDICAO_CONCORRENTE = "edicao_concorrente"
    ERRO_NOTION = "erro_notion"


@dataclass(frozen=True)
class ResultadoPos:
    """Desfecho da pós-publicação de um post. Só ids, plataformas, enums e códigos (I-1)."""

    post_id: str
    desfecho: DesfechoPos
    estagio_destino: PapelEstagio | None = None
    plataformas_publicadas: tuple[str, ...] = ()
    plataformas_pendentes: tuple[str, ...] = ()
    plataformas_privadas: tuple[str, ...] = ()
    motivo: str | None = None

    @property
    def modo_privado(self) -> bool:
        """Marca visível de A-022: alguma plataforma saiu em modo privado."""
        return bool(self.plataformas_privadas)

    @property
    def estagio_alterado(self) -> bool:
        return self.desfecho in (
            DesfechoPos.PUBLICADO,
            DesfechoPos.PUBLICADO_MODO_PRIVADO,
            DesfechoPos.FALHA,
        )


class _Situacao(Enum):
    SAIU = "saiu"
    SAIU_PRIVADO = "saiu_privado"
    INCERTO = "incerto"
    FALHA_DEFINITIVA = "falha_definitiva"
    AGUARDA_TENTATIVA = "aguarda_tentativa"
    PENDENTE = "pendente"


def _situacao(execucoes: tuple[Execucao, ...]) -> _Situacao:
    estados = {e.estado for e in execucoes}
    if estados & _DUVIDA:
        return _Situacao.INCERTO
    if EstadoExecucao.SUCESSO in estados:
        return _Situacao.SAIU
    if EstadoExecucao.SUCESSO_MODO_PRIVADO in estados:
        return _Situacao.SAIU_PRIVADO
    falhas = [e for e in execucoes if e.estado is EstadoExecucao.FALHA]
    if not falhas:
        return _Situacao.PENDENTE
    transitorias = all(e.motivo == CodigoMotivo.ERRO_TRANSITORIO.value for e in falhas)
    if transitorias and max(e.tentativa for e in falhas) < _TENTATIVA_FINAL:
        return _Situacao.AGUARDA_TENTATIVA
    return _Situacao.FALHA_DEFINITIVA


def _estagio_unico(config: Configuracao, papel: PapelEstagio) -> str | None:
    ids = [i for i, e in config.estagios.items() if e.papel is papel]
    return ids[0] if len(ids) == 1 else None


def _conferir_edicao(cliente: ClienteNotion, post: Post) -> None:
    """Mesma regra de ``editar_post``/``sair_de_autorizado`` (RF-42): não escreve se o post
    mudou desde a leitura."""
    bruto = cliente.obter_pagina(post.id).get("last_edited_time")
    atual: datetime | None = None
    if isinstance(bruto, str):
        try:
            atual = datetime.fromisoformat(bruto)
        except ValueError:
            atual = None
    if atual is None or atual.tzinfo is None or post.editado_em is None:
        raise EdicaoConcorrente("Não foi possível confirmar a última edição do post; nada gravado.")
    if atual != post.editado_em:
        raise EdicaoConcorrente("O post foi editado depois da leitura; nada gravado.")


def _propriedades(destino_id: str, papel: PapelEstagio) -> dict[str, Any]:
    if papel is PapelEstagio.PUBLICADO:  # só o estágio; selo e Confirmado em ficam como prova
        return {_PROP_ESTAGIO: {"relation": [{"id": destino_id}]}}
    if papel is PapelEstagio.FALHA:  # estágio + selo e Confirmado em limpos (PATCH único)
        return propriedades_de_saida(destino_id)
    raise ValueError("a pós-publicação só escreve os estágios publicado e falha")  # I-2


def concluir_post(
    cliente: ClienteNotion,
    ids: IdsBancos,
    config: Configuracao,
    post_id: str,
    *,
    modo_real: bool,
) -> ResultadoPos:
    """Avalia as execuções do post e, se couber, move o estágio para ``publicado``/``falha``.

    Nunca levanta. ``modo_real`` precisa ser exatamente ``True``: em simulação nada é lido
    nem escrito.
    """

    def resultado(desfecho: DesfechoPos, motivo: str | None = None, **campos: Any) -> ResultadoPos:
        _log.info("pós-publicação: post %s: %s", post_id, desfecho.value)
        return ResultadoPos(post_id, desfecho, motivo=motivo, **campos)

    if modo_real is not True:
        return resultado(DesfechoPos.SEM_ALTERACAO, "simulacao")
    id_publicado = _estagio_unico(config, PapelEstagio.PUBLICADO)
    id_falha = _estagio_unico(config, PapelEstagio.FALHA)
    if config.problemas or id_publicado is None or id_falha is None:
        _log.error("pós-publicação: configuração de estágios inválida; nada alterado")
        return resultado(DesfechoPos.CONFIG_INVALIDA, "config_estagios_invalida")

    try:
        post = ler_post(cliente, post_id, config)
        situacoes = {
            p: _situacao(tuple(r.execucao for r in execucoes_do_post(cliente, ids, post.id, p)))
            for p in sorted(post.plataformas)
        }
    except Exception as erro:  # noqa: BLE001 - sem leitura confiável, nada é alterado
        _log.error("pós-publicação: leitura falhou; post %s (%s)", post_id, type(erro).__name__)
        return resultado(DesfechoPos.ERRO_LEITURA, "leitura_falhou")

    saiu = tuple(p for p, s in situacoes.items() if s in (_Situacao.SAIU, _Situacao.SAIU_PRIVADO))
    privadas = tuple(p for p, s in situacoes.items() if s is _Situacao.SAIU_PRIVADO)
    pendentes = tuple(p for p in situacoes if p not in saiu)
    campos: dict[str, Any] = {
        "plataformas_publicadas": saiu,
        "plataformas_pendentes": pendentes,
        "plataformas_privadas": privadas,
    }
    valores = set(situacoes.values())
    destino: PapelEstagio
    if not situacoes:
        return resultado(DesfechoPos.SEM_ALTERACAO, "sem_plataformas", **campos)
    if not pendentes:
        destino = PapelEstagio.PUBLICADO
    elif _Situacao.INCERTO in valores or _Situacao.FALHA_DEFINITIVA in valores or saiu:
        destino = PapelEstagio.FALHA  # incerto, falha definitiva ou sucesso parcial (A-003)
    elif _Situacao.AGUARDA_TENTATIVA in valores:
        return resultado(DesfechoPos.AGUARDANDO_NOVA_TENTATIVA, "aguarda_nova_tentativa", **campos)
    else:
        return resultado(DesfechoPos.SEM_ALTERACAO, "sem_resultado_real", **campos)

    desfecho = {
        PapelEstagio.PUBLICADO: (
            DesfechoPos.PUBLICADO_MODO_PRIVADO if privadas else DesfechoPos.PUBLICADO
        ),
        PapelEstagio.FALHA: DesfechoPos.FALHA,
    }[destino]
    if post.estagio.papel is destino:
        return resultado(DesfechoPos.JA_APLICADO, None, estagio_destino=destino, **campos)
    if post.estagio.papel is not PapelEstagio.AUTORIZADO:
        return resultado(DesfechoPos.SEM_ALTERACAO, "estagio_nao_autorizado", **campos)

    try:
        propriedades = _propriedades(
            id_publicado if destino is PapelEstagio.PUBLICADO else id_falha, destino
        )
        _conferir_edicao(cliente, post)
        cliente.atualizar_pagina(post.id, propriedades)
    except EdicaoConcorrente:
        _log.warning("pós-publicação: post %s editado durante a conclusão; nada gravado", post_id)
        return resultado(DesfechoPos.EDICAO_CONCORRENTE, "edicao_concorrente", **campos)
    except Exception as erro:  # noqa: BLE001 - sem desfazer a execução e sem repetir nada
        _log.error(
            "pós-publicação: estágio não gravado; post %s (%s)", post_id, type(erro).__name__
        )
        return resultado(DesfechoPos.ERRO_NOTION, "erro_notion", **campos)
    return resultado(desfecho, None, estagio_destino=destino, **campos)
