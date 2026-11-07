"""Reverificação do selo de um post autorizado (RF-26, T023; regras I-1 e I-2).

Se o selo recalculado não confere com o gravado, o post sai de ``autorizado`` e volta ao
estágio de papel ``pronto`` com ``sair_de_autorizado`` (UM ÚNICO PATCH que muda o estágio e
limpa o "Selo de confirmação" e o "Confirmado em"). Toda edição de campo publicável depois da
confirmação (título, corpo, seção por plataforma, plataformas, formato, data e hora, caminho,
ordem ou CONTEÚDO de uma arte) muda o selo e, portanto, devolve o post a ``pronto``.

Por que um módulo próprio (e não ``selo.py``): ``confirmacao.py`` já importa ``selo.py``; pôr
aqui a devolução (que usa ``sair_de_autorizado``) dentro de ``selo.py`` criaria import circular.
``selo.py`` continua puro (só cálculo); este módulo junta selo + escrita.

Decisões de falha segura:

* **Post fora de ``autorizado``**: nada é lido nem escrito (``IGNORADO_NAO_AUTORIZADO``).
* **Divergência provada** (selo v1 diferente do recalculado), **selo malformado ou de versão
  desconhecida**, **recálculo impossível** (``SeloImpossivel``: sem data e hora, só data, sem
  título ou plataformas) e **arte inválida** (``ArteNaoEncontrada``, ``CaminhoDeArteInvalido``,
  ``VariacaoInvalida``): devolve a ``pronto``. Nesses casos o post não pode seguir autorizado.
* **Armazém indisponível** (``ErroAcessoArmazem``: rede, GitHub fora do ar, token recusado):
  NÃO é divergência provada. NÃO devolve; retorna ``INDETERMINADO`` sem escrever. Assim uma
  queda do GitHub não desfaz confirmações em massa. Publicar continua impossível, porque a
  guarda de publicação recalcula o selo e recusa quando não consegue ler as artes.
* **Selo ausente**: ``tratar_selo_ausente`` é explícito. ``"devolver"`` (padrão, para quem
  reverifica logo antes de publicar) devolve a ``pronto``. ``"ignorar"`` (o vigia, T024/R2-12:
  confirmação feita direto no Notion espera ~5 min para ser selada e não é revertida) devolve
  ``MANTIDO`` com motivo ``SELO_AUSENTE`` e não escreve; a guarda recusa o post (selo ausente)
  enquanto ele não for selado.
* **Configuração sem exatamente um estágio de papel ``pronto``**: levanta
  ``ConfiguracaoSemProntoUnico`` ANTES de qualquer leitura de arte ou escrita (só para post em
  ``autorizado``). O post fica como está no Notion; se o selo divergir, a guarda o recusa por
  divergência (nada é publicado); a pessoa corrige a configuração e a reverificação seguinte o
  devolve.
* **Edição concorrente** (``EdicaoConcorrente``) e erros do Notion propagam sem escrita (a
  conferência é a mesma de ``sair_de_autorizado``). ``ConfirmacaoRecusada`` de
  ``sair_de_autorizado`` (sem versão lida, releitura divergente) também propaga.

I-1: mensagens e logs trazem só o id do post e o nome do motivo; nunca título, corpo, caminhos
de arte, selo ou dados brutos do Notion. O ``repr`` do resultado omite o post.

Limites: entre a reverificação e o envio ainda há janela (TOCTOU); por isso a guarda de
publicação recalcula o selo de novo e o adaptador deve publicar os mesmos bytes verificados.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Final, Literal

from publicador.artes.armazem import (
    ArmazemDeArtes,
    ArteNaoEncontrada,
    CaminhoDeArteInvalido,
    ErroAcessoArmazem,
)
from publicador.core.confirmacao import sair_de_autorizado
from publicador.core.modelos import Estagio, PapelEstagio, Post
from publicador.core.redacao import obter_logger
from publicador.core.selo import SeloImpossivel, verificar_selo
from publicador.notion.bancos import Configuracao
from publicador.notion.cliente import ClienteNotion
from publicador.notion.variacoes import VariacaoInvalida

__all__ = [
    "ConfiguracaoSemProntoUnico",
    "MotivoReverificacao",
    "ResultadoReverificacao",
    "SituacaoReverificacao",
    "TratarSeloAusente",
    "exigir_estagio_pronto",
    "reverificar_post",
]

_log = obter_logger("publicador.core.reverificacao")

# Só para CLASSIFICAR o motivo (divergente x malformado); quem decide é ``verificar_selo``.
_RE_SELO_V1: Final = re.compile(r"v1:[0-9a-f]{64}")

TratarSeloAusente = Literal["devolver", "ignorar"]


class SituacaoReverificacao(Enum):
    MANTIDO = "mantido"
    DEVOLVIDO_A_PRONTO = "devolvido_a_pronto"
    IGNORADO_NAO_AUTORIZADO = "ignorado_nao_autorizado"
    INDETERMINADO = "indeterminado"


class MotivoReverificacao(Enum):
    SELO_DIVERGENTE = "selo_divergente"
    SELO_AUSENTE = "selo_ausente"
    SELO_INVALIDO = "selo_invalido"
    SEM_DATA_HORA = "sem_data_hora"
    SELO_IMPOSSIVEL = "selo_impossivel"
    ARTE_INVALIDA = "arte_invalida"
    ARMAZEM_INDISPONIVEL = "armazem_indisponivel"


@dataclass(frozen=True)
class ResultadoReverificacao:
    """Resultado da reverificação.

    ``MANTIDO`` com ``motivo`` ``None``: selo conferido. ``MANTIDO`` com ``SELO_AUSENTE``: selo
    ausente ignorado a pedido (não é autorização válida; a guarda recusa). ``post`` é o post
    relido do Notion após a devolução, ou o post recebido nos demais casos (fora do ``repr``).
    """

    situacao: SituacaoReverificacao
    motivo: MotivoReverificacao | None
    post: Post = field(repr=False, compare=False)


class ConfiguracaoSemProntoUnico(Exception):  # noqa: N818 - nome do domínio
    """A configuração não tem exatamente um estágio de papel ``pronto``; nada foi gravado."""


def exigir_estagio_pronto(config: Configuracao) -> Estagio:
    """Devolve o ÚNICO estágio de papel ``pronto``; senão levanta ``ConfiguracaoSemProntoUnico``."""
    prontos = [e for e in config.estagios.values() if e.papel is PapelEstagio.PRONTO]
    if len(prontos) != 1:
        raise ConfiguracaoSemProntoUnico(
            "a configuração deve ter exatamente um estágio com papel 'pronto'; nada foi gravado"
        )
    return prontos[0]


def _motivo_do_recalculo(post: Post, armazem: ArmazemDeArtes) -> MotivoReverificacao | None:
    """``None`` se o selo confere; senão o motivo. ``ErroAcessoArmazem`` propaga."""
    selo = post.selo
    try:
        if verificar_selo(post, armazem, selo):
            return None
    except SeloImpossivel:
        if post.somente_data or post.publicar_em is None:
            return MotivoReverificacao.SEM_DATA_HORA
        return MotivoReverificacao.SELO_IMPOSSIVEL
    except (ArteNaoEncontrada, CaminhoDeArteInvalido, VariacaoInvalida):
        return MotivoReverificacao.ARTE_INVALIDA
    if isinstance(selo, str) and _RE_SELO_V1.fullmatch(selo) is not None:
        return MotivoReverificacao.SELO_DIVERGENTE
    return MotivoReverificacao.SELO_INVALIDO


def reverificar_post(
    cliente: ClienteNotion,
    post: Post,
    config: Configuracao,
    armazem: ArmazemDeArtes,
    *,
    tratar_selo_ausente: TratarSeloAusente = "devolver",
) -> ResultadoReverificacao:
    """Reverifica o selo de ``post`` e, se não conferir, devolve-o ao estágio ``pronto``.

    ``post`` deve ter sido lido do Notion (``editado_em`` é usado na conferência de edição
    concorrente). Veja no docstring do módulo as decisões de falha segura.
    """
    if tratar_selo_ausente not in ("devolver", "ignorar"):
        raise ValueError("tratar_selo_ausente deve ser 'devolver' ou 'ignorar'")

    if post.estagio.papel is not PapelEstagio.AUTORIZADO:
        return ResultadoReverificacao(SituacaoReverificacao.IGNORADO_NAO_AUTORIZADO, None, post)

    destino = exigir_estagio_pronto(config)

    motivo: MotivoReverificacao | None
    if post.selo is None or not post.selo.strip():
        if tratar_selo_ausente == "ignorar":
            _log.info("reverificação: selo ausente ignorado; post %s", post.id)
            return ResultadoReverificacao(
                SituacaoReverificacao.MANTIDO, MotivoReverificacao.SELO_AUSENTE, post
            )
        motivo = MotivoReverificacao.SELO_AUSENTE
    else:
        try:
            motivo = _motivo_do_recalculo(post, armazem)
        except ErroAcessoArmazem:
            _log.warning(
                "reverificação indeterminada (armazém indisponível); post %s mantido", post.id
            )
            return ResultadoReverificacao(
                SituacaoReverificacao.INDETERMINADO,
                MotivoReverificacao.ARMAZEM_INDISPONIVEL,
                post,
            )
        if motivo is None:
            return ResultadoReverificacao(SituacaoReverificacao.MANTIDO, None, post)

    novo = sair_de_autorizado(cliente, post, config, destino)
    _log.info("reverificação: post %s devolvido a pronto; motivo: %s", post.id, motivo.value)
    return ResultadoReverificacao(SituacaoReverificacao.DEVOLVIDO_A_PRONTO, motivo, novo)
