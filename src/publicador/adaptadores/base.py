"""Interface ``PlatformAdapter`` e tipos de resultado (RF-17, RF-12, RF-25, RF-18, RF-35).

Contrato entre o orquestrador (job) e cada plataforma (Instagram, TikTok, ...).

Regras:

* I-1: nenhum tipo aqui tem campo para token ou segredo; ``repr``/``str`` nunca mostram
  conteúdo do post. Textos livres de motivo passam por ``redigir`` e as URLs devolvidas
  não podem ter credencial embutida.
* I-2: o adaptador NÃO decide autorização. Ele só é chamado depois da guarda. A simulação
  (``PedidoDePublicacao.simular``) aplica as MESMAS validações de mídia e conexão do
  adaptador real e não envia nada.

Semântica dos resultados de ``publicar``:

* ``SUCESSO`` / ``SUCESSO_MODO_PRIVADO`` (RF-35): publicado (privado, se o app não foi
  auditado); exigem ``id_externo``.
* ``FALHA_TRANSITORIA``: certeza de que NÃO saiu e vale tentar de novo (uma vez, após
  consultar).
* ``FALHA_PERMANENTE``: certeza de que não saiu e repetir não ajuda.
* ``INCERTO``: pode ter saído (ex.: tempo esgotado). NUNCA retentar; resolução manual.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

from publicador.core.modelos import ConexaoEstado, Post
from publicador.core.redacao import redigir
from publicador.notion.variacoes import conteudo_da_plataforma

__all__ = [
    "EstadoConexao",
    "EstadoConsulta",
    "PedidoDePublicacao",
    "PlatformAdapter",
    "ProblemaDeMidia",
    "ResultadoConsulta",
    "ResultadoPublicacao",
    "ResultadoValidacao",
    "TipoDeProblema",
    "TipoDeResultado",
]


def _exigir_texto(valor: str, rotulo: str) -> None:
    if not valor.strip():
        raise ValueError(f"{rotulo} não pode ser vazio")


def _exigir_url_sem_credencial(url: str | None) -> None:
    if url is None:
        return
    partes = urlsplit(url)
    if partes.username is not None or partes.password is not None or "@" in partes.netloc:
        raise ValueError("url não pode conter credenciais")


# --------------------------------------------------------------------------- conexão


@dataclass(frozen=True)
class EstadoConexao:
    """Resultado de ``verificar_conexao``: estado e validade, sem token (RF-18)."""

    plataforma: str
    estado: ConexaoEstado
    dias_restantes: int | None = None

    def __post_init__(self) -> None:
        _exigir_texto(self.plataforma, "plataforma da conexão")
        if self.dias_restantes is not None and self.dias_restantes < 0:
            raise ValueError("dias_restantes não pode ser negativo")

    @property
    def valida(self) -> bool:
        return self.estado is ConexaoEstado.ATIVA

    @classmethod
    def de_expiracao(
        cls, plataforma: str, expira_em: datetime | None, agora: datetime
    ) -> EstadoConexao:
        """Deriva o estado da data de expiração (ambas com fuso)."""
        if expira_em is None:
            return cls(plataforma, ConexaoEstado.ATIVA)
        restante = expira_em - agora
        if restante.total_seconds() <= 0:
            return cls(plataforma, ConexaoEstado.EXPIRADA)
        return cls(plataforma, ConexaoEstado.ATIVA, restante.days)


# --------------------------------------------------------------------------- mídia


class TipoDeProblema(Enum):
    PLATAFORMA_FORA_DO_POST = "plataforma_fora_do_post"
    FORMATO_AUSENTE = "formato_ausente"
    FORMATO_NAO_SUPORTADO = "formato_nao_suportado"
    QUANTIDADE_DE_ARTES = "quantidade_de_artes"
    CAMINHO_INVALIDO = "caminho_invalido"
    EXTENSAO_NAO_SUPORTADA = "extensao_nao_suportada"
    ARTE_NAO_ENCONTRADA = "arte_nao_encontrada"
    ARMAZEM_INDISPONIVEL = "armazem_indisponivel"


@dataclass(frozen=True)
class ProblemaDeMidia:
    """Problema tipado de mídia. Só o índice da arte: nunca texto nem caminho do post."""

    tipo: TipoDeProblema
    indice_arte: int | None = None


@dataclass(frozen=True)
class ResultadoValidacao:
    problemas: tuple[ProblemaDeMidia, ...] = ()

    @property
    def valida(self) -> bool:
        return not self.problemas


# --------------------------------------------------------------------------- publicação


@dataclass(frozen=True, repr=False)
class PedidoDePublicacao:
    """O que o orquestrador entrega ao adaptador (depois da guarda).

    ``artes`` são os caminhos já resolvidos PARA esta plataforma
    (``artes_por_plataforma``). ``simular=True`` (padrão, seguro) não envia nada.
    """

    post: Post
    plataforma: str
    artes: tuple[str, ...]
    chave_idempotencia: str
    simular: bool = True

    def __post_init__(self) -> None:
        _exigir_texto(self.plataforma, "plataforma do pedido")
        _exigir_texto(self.chave_idempotencia, "chave de idempotência")

    @property
    def conteudo(self) -> str:
        """Texto da plataforma (secção própria ou corpo comum). Nunca vai a log."""
        return conteudo_da_plataforma(self.post, self.plataforma)

    def __repr__(self) -> str:
        return (
            f"PedidoDePublicacao(post_id={self.post.id!r}, plataforma={self.plataforma!r}, "
            f"artes={len(self.artes)}, simular={self.simular})"
        )

    __str__ = __repr__


class TipoDeResultado(Enum):
    SUCESSO = "sucesso"
    SUCESSO_MODO_PRIVADO = "sucesso_modo_privado"
    FALHA_TRANSITORIA = "falha_transitoria"
    FALHA_PERMANENTE = "falha_permanente"
    INCERTO = "incerto"


_COM_ID = frozenset({TipoDeResultado.SUCESSO, TipoDeResultado.SUCESSO_MODO_PRIVADO})


@dataclass(frozen=True)
class ResultadoPublicacao:
    tipo: TipoDeResultado
    chave_idempotencia: str
    id_externo: str | None = None
    url: str | None = None
    simulado: bool = False
    motivo: str | None = None  # código curto; redigido (I-1)

    def __post_init__(self) -> None:
        _exigir_texto(self.chave_idempotencia, "chave de idempotência")
        if self.tipo in _COM_ID and not (self.id_externo and self.id_externo.strip()):
            raise ValueError("sucesso exige id_externo")
        _exigir_url_sem_credencial(self.url)
        if self.motivo is not None:
            object.__setattr__(self, "motivo", redigir(self.motivo))

    @property
    def sucesso(self) -> bool:
        return self.tipo in _COM_ID


class EstadoConsulta(Enum):
    ENCONTRADO = "encontrado"
    NAO_ENCONTRADO = "nao_encontrado"
    DESCONHECIDO = "desconhecido"


@dataclass(frozen=True)
class ResultadoConsulta:
    estado: EstadoConsulta
    id_externo: str | None = None
    url: str | None = None

    def __post_init__(self) -> None:
        if self.estado is EstadoConsulta.ENCONTRADO and not (
            self.id_externo and self.id_externo.strip()
        ):
            raise ValueError("consulta encontrada exige id_externo")
        _exigir_url_sem_credencial(self.url)


# --------------------------------------------------------------------------- interface


@runtime_checkable
class PlatformAdapter(Protocol):
    """Contrato de um adaptador de plataforma (seção 4.2 do plano).

    O adaptador não decide autorização (I-2): quem o chama já passou pela guarda.
    """

    @property
    def plataforma(self) -> str: ...

    def verificar_conexao(self) -> EstadoConexao:
        """Estado e validade da conexão, sem expor token (RF-18)."""
        ...

    def validar_midia(self, post: Post, artes: Sequence[str]) -> ResultadoValidacao:
        """Problemas tipados de mídia para esta plataforma (RF-14, RF-43)."""
        ...

    def limite_diario_restante(self) -> int | None:
        """Publicações que ainda cabem hoje; ``None`` se a plataforma não informa."""
        ...

    def publicar(self, pedido: PedidoDePublicacao) -> ResultadoPublicacao:
        """Publica (ou simula, se ``pedido.simular``) com idempotência pela chave."""
        ...

    def consultar(self, chave_idempotencia: str) -> ResultadoConsulta:
        """Pergunta à plataforma se a publicação da chave existe (nova tentativa segura)."""
        ...
