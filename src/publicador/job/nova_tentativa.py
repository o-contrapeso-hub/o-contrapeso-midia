"""Nova tentativa segura: classificação e plano (T040; RF-12, RF-25, RF-30, RF-38).

Funções PURAS (sem rede, sem relógio próprio, sem escrita), usadas pelo orquestrador (decidir
se tenta, espera ou desiste) e pela pós-publicação (decidir se o post ainda aguarda a nova
tentativa ou já cai em ``falha``). Um só lugar para a regra, para os dois nunca divergirem.

Regras (spec RF-25 e critérios Dado/Quando/Então; plano, passo 5 do fluxo):

* **Classificação** do resultado do envio: ``SUCESSO`` (inclui modo privado), ``TRANSITORIO``
  (certeza de que não saiu e vale tentar de novo), ``PERMANENTE`` (certeza de que não saiu,
  repetir não ajuda) e ``DESCONHECIDO`` (pode ter saído: estado ``incerto``, NUNCA retentar,
  só resolução manual, RF-38). Exceção do adaptador e resposta fora do contrato são
  ``DESCONHECIDO`` (lado seguro).
* **No máximo UMA nova tentativa** (tentativa 2), só depois de falha TRANSITÓRIA na tentativa
  1 da MESMA confirmação, no mínimo ``nova_tentativa_minutos`` depois do fim da tentativa 1
  e dentro da janela (``janela_tolerancia_minutos`` depois do horário agendado, inclusiva como
  na guarda). Se a nova tentativa só poderia acontecer fora da janela, ou a janela já acabou,
  não há nova tentativa (RF-30: o post cai em ``falha``).
* **Permanente** (inclui a resolução manual "não publicado"), ou tentativa 2 já registrada:
  sem nova tentativa nesta confirmação.
* Este módulo não autoriza nada (I-2): o plano só é consultado DEPOIS de a guarda permitir, e
  a nova tentativa passa por TODA a guarda de novo (janela, selo, estágio, execução existente).

"Mesma confirmação" (A-003, RF-34): uma nova confirmação publica de novo as plataformas que
falharam com certeza. A execução pertence à confirmação atual quando começou em ou depois de
"Confirmado em" do post. Sem "Confirmado em" (dado incompleto), TODAS as falhas contam como da
confirmação atual (lado seguro: menos tentativas, nunca mais). A checagem de execuções que
BLOQUEIAM (sucesso, incerto, iniciada) ignora a data e a confirmação (RF-38) e é da guarda.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Final

from publicador.adaptadores.base import TipoDeResultado
from publicador.core.config import Parametros
from publicador.core.modelos import EstadoExecucao, Execucao
from publicador.job.execucoes import CodigoMotivo

__all__ = [
    "ESTADOS_BLOQUEANTES",
    "TENTATIVA_FINAL",
    "Classe",
    "Passo",
    "Plano",
    "classificar",
    "da_confirmacao_atual",
    "planejar",
]

TENTATIVA_FINAL: Final = 2
_PRIMEIRA: Final = 1

# Execuções anteriores que impedem QUALQUER envio do mesmo post e plataforma, de qualquer data
# (mesmo conjunto da verificação 10 da guarda; usado na checagem de exclusividade pós-claim).
ESTADOS_BLOQUEANTES: Final[frozenset[EstadoExecucao]] = frozenset(
    {
        EstadoExecucao.SUCESSO,
        EstadoExecucao.SUCESSO_MODO_PRIVADO,
        EstadoExecucao.INICIADA,
        EstadoExecucao.INCERTO,
    }
)


class Classe(Enum):
    SUCESSO = "sucesso"
    TRANSITORIO = "transitorio"
    PERMANENTE = "permanente"
    DESCONHECIDO = "desconhecido"


_CLASSES: Final[dict[TipoDeResultado, Classe]] = {
    TipoDeResultado.SUCESSO: Classe.SUCESSO,
    TipoDeResultado.SUCESSO_MODO_PRIVADO: Classe.SUCESSO,
    TipoDeResultado.FALHA_TRANSITORIA: Classe.TRANSITORIO,
    TipoDeResultado.FALHA_PERMANENTE: Classe.PERMANENTE,
    TipoDeResultado.INCERTO: Classe.DESCONHECIDO,
}


def classificar(tipo: object) -> Classe:
    """Classe do resultado do envio. Qualquer coisa fora do contrato é ``DESCONHECIDO``."""
    if isinstance(tipo, TipoDeResultado):
        return _CLASSES.get(tipo, Classe.DESCONHECIDO)
    return Classe.DESCONHECIDO


class Passo(Enum):
    PRIMEIRA = "primeira"  # nenhuma falha desta confirmação: tentativa 1
    AGUARDAR = "aguardar"  # falha transitória; o intervalo ainda não passou
    NOVA_TENTATIVA = "nova_tentativa"  # consultar a plataforma e, se não saiu, tentativa 2
    ENCERRADA = "encerrada"  # sem nova tentativa nesta confirmação


@dataclass(frozen=True)
class Plano:
    passo: Passo
    tentativa: int | None = None
    motivo: CodigoMotivo | None = None
    liberada_em: datetime | None = None


def _tem_fuso(valor: object) -> bool:
    return (
        isinstance(valor, datetime) and valor.tzinfo is not None and valor.utcoffset() is not None
    )


def da_confirmacao_atual(execucao: Execucao, confirmado_em: datetime | None) -> bool:
    """A execução começou em ou depois da confirmação atual (sem confirmação: conta sempre)."""
    if not _tem_fuso(confirmado_em):
        return True
    assert confirmado_em is not None
    inicio = execucao.iniciada_em
    if not _tem_fuso(inicio):
        return True
    return inicio.astimezone(UTC) >= confirmado_em.astimezone(UTC)


def _minutos(valor: object) -> int | None:
    if isinstance(valor, bool) or not isinstance(valor, int) or valor <= 0:
        return None
    return valor


def _motivo_permanente(falhas: Iterable[Execucao]) -> CodigoMotivo:
    for falha in falhas:
        if falha.motivo == CodigoMotivo.ERRO_TRANSITORIO.value:
            continue
        for codigo in CodigoMotivo:
            if codigo.value == falha.motivo:
                return codigo
        break
    return CodigoMotivo.ERRO_PERMANENTE


def planejar(
    execucoes: Iterable[Execucao],
    *,
    confirmado_em: datetime | None,
    publicar_em: datetime | None,
    agora: datetime,
    parametros: Parametros,
) -> Plano:
    """Decide o próximo passo do post numa plataforma a partir das execuções ``FALHA`` da
    confirmação atual. Outras execuções (recusa, simulação, não executado) não contam como
    tentativa; as bloqueantes são assunto da guarda. Dado inválido: ``ENCERRADA``."""
    falhas = [
        e
        for e in execucoes
        if isinstance(e, Execucao)
        and e.estado is EstadoExecucao.FALHA
        and da_confirmacao_atual(e, confirmado_em)
    ]
    if not falhas:
        return Plano(Passo.PRIMEIRA, _PRIMEIRA)
    if any(e.tentativa >= TENTATIVA_FINAL for e in falhas):
        return Plano(Passo.ENCERRADA, motivo=CodigoMotivo.TENTATIVAS_ESGOTADAS)
    if any(e.motivo != CodigoMotivo.ERRO_TRANSITORIO.value for e in falhas):
        return Plano(Passo.ENCERRADA, motivo=_motivo_permanente(falhas))

    intervalo = _minutos(getattr(parametros, "nova_tentativa_minutos", None))
    janela = _minutos(getattr(parametros, "janela_tolerancia_minutos", None))
    fins = [e.finalizada_em or e.iniciada_em for e in falhas]
    if (
        intervalo is None
        or janela is None
        or not _tem_fuso(publicar_em)
        or not _tem_fuso(agora)
        or not all(_tem_fuso(f) for f in fins)
    ):
        return Plano(Passo.ENCERRADA, motivo=CodigoMotivo.NOVA_TENTATIVA_FORA_DA_JANELA)
    assert publicar_em is not None
    liberada = max(f.astimezone(UTC) for f in fins) + timedelta(minutes=intervalo)
    fim_da_janela = publicar_em.astimezone(UTC) + timedelta(minutes=janela)
    instante = agora.astimezone(UTC)
    if liberada > fim_da_janela or instante > fim_da_janela:
        return Plano(
            Passo.ENCERRADA, motivo=CodigoMotivo.NOVA_TENTATIVA_FORA_DA_JANELA, liberada_em=liberada
        )
    if instante < liberada:
        return Plano(Passo.AGUARDAR, liberada_em=liberada)
    return Plano(Passo.NOVA_TENTATIVA, TENTATIVA_FINAL, liberada_em=liberada)
