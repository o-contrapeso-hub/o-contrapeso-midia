"""Modelos de domínio do Publicador.

Regras invioláveis refletidas aqui:
- I-1: nenhum modelo tem campo para token, senha ou segredo.
- I-2: os modelos tornam verificável a autorização (estágio com papel `autorizado`,
  chamado "confirmado", com data/hora e selo); eles não decidem publicar.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

NOME_ESTAGIO_AUTORIZADOR = "confirmado"


def _exigir_texto(valor: str, rotulo: str) -> None:
    if not valor.strip():
        raise ValueError(f"{rotulo} não pode ser vazio")


def _exigir_fuso(valor: datetime | None, rotulo: str) -> None:
    if valor is not None and (valor.tzinfo is None or valor.utcoffset() is None):
        raise ValueError(f"{rotulo} deve ter fuso horário (datetime ingênuo é rejeitado)")


class PapelEstagio(Enum):
    """Papéis fixos; o nome do estágio é livre, o papel vem desta lista."""

    PAUTA = "pauta"
    PRONTO = "pronto"
    AUTORIZADO = "autorizado"
    PUBLICADO = "publicado"
    FALHA = "falha"
    CANCELADO = "cancelado"


@dataclass(frozen=True)
class Estagio:
    nome: str
    papel: PapelEstagio

    def __post_init__(self) -> None:
        _exigir_texto(self.nome, "nome do estágio")
        eh_autorizador = self.papel is PapelEstagio.AUTORIZADO
        if eh_autorizador and self.nome != NOME_ESTAGIO_AUTORIZADOR:
            raise ValueError(
                f"o estágio com papel autorizado deve se chamar '{NOME_ESTAGIO_AUTORIZADOR}'"
            )
        if not eh_autorizador and self.nome == NOME_ESTAGIO_AUTORIZADOR:
            raise ValueError(f"o nome '{NOME_ESTAGIO_AUTORIZADOR}' é reservado ao papel autorizado")


@dataclass(frozen=True)
class Plataforma:
    """Plataforma configurável (não é enum fixa): apenas uma chave não vazia."""

    chave: str

    def __post_init__(self) -> None:
        _exigir_texto(self.chave, "chave da plataforma")


@dataclass(frozen=True)
class Formato:
    nome: str

    def __post_init__(self) -> None:
        _exigir_texto(self.nome, "nome do formato")


@dataclass(frozen=True)
class Tema:
    nome: str

    def __post_init__(self) -> None:
        _exigir_texto(self.nome, "nome do tema")


@dataclass(frozen=True)
class Serie:
    nome: str
    continuidade: bool

    def __post_init__(self) -> None:
        _exigir_texto(self.nome, "nome da série")


@dataclass(frozen=True)
class Post:
    id: str
    titulo: str
    corpo: str
    estagio: Estagio
    plataformas: tuple[str, ...]
    formato: str | None = None
    serie: str | None = None
    ordem_na_serie: int | None = None
    tema: str | None = None
    publicar_em: datetime | None = None
    artes: tuple[str, ...] = ()
    selo: str | None = None
    confirmado_em: datetime | None = None
    editado_em: datetime | None = None

    def __post_init__(self) -> None:
        _exigir_texto(self.titulo, "título do post")
        _exigir_fuso(self.publicar_em, "publicar_em")
        _exigir_fuso(self.confirmado_em, "confirmado_em")
        _exigir_fuso(self.editado_em, "editado_em")

    def tem_data_e_hora(self) -> bool:
        return self.publicar_em is not None


class ConexaoEstado(Enum):
    ATIVA = "ativa"
    EXPIRADA = "expirada"
    INVALIDA = "invalida"


@dataclass(frozen=True)
class Conexao:
    """Estado de uma conexão com plataforma. Sem credencial (I-1)."""

    plataforma: str
    estado: ConexaoEstado
    expira_em: datetime | None = None
    verificado_em: datetime | None = None

    def __post_init__(self) -> None:
        _exigir_texto(self.plataforma, "plataforma da conexão")
        _exigir_fuso(self.expira_em, "expira_em")
        _exigir_fuso(self.verificado_em, "verificado_em")


class EstadoExecucao(Enum):
    INICIADA = "iniciada"
    SUCESSO = "sucesso"
    SUCESSO_MODO_PRIVADO = "sucesso_modo_privado"
    FALHA = "falha"
    RECUSADA = "recusada"
    INCERTO = "incerto"
    NAO_EXECUTADO = "nao_executado"
    SIMULADA = "simulada"


@dataclass(frozen=True)
class Execucao:
    """Registro de uma tentativa de publicação. Sem conteúdo do post."""

    chave_idempotencia: str
    post_id: str
    plataforma: str
    estado: EstadoExecucao
    tentativa: int
    iniciada_em: datetime
    finalizada_em: datetime | None = None
    motivo: str | None = None

    def __post_init__(self) -> None:
        _exigir_texto(self.chave_idempotencia, "chave de idempotência")
        _exigir_texto(self.post_id, "post_id")
        _exigir_texto(self.plataforma, "plataforma da execução")
        if self.tentativa < 1:
            raise ValueError("tentativa deve ser >= 1")
        _exigir_fuso(self.iniciada_em, "iniciada_em")
        _exigir_fuso(self.finalizada_em, "finalizada_em")


class MotivoRecusa(Enum):
    LEITURA_FALHOU = "leitura_falhou"
    CONFIG_INCONSISTENTE = "config_inconsistente"
    ESTAGIO_NAO_AUTORIZADO = "estagio_nao_autorizado"
    SEM_DATA = "sem_data"
    SEM_HORA = "sem_hora"
    DATA_FUTURA = "data_futura"
    SELO_AUSENTE = "selo_ausente"
    SELO_DIVERGENTE = "selo_divergente"
    FORA_DA_JANELA = "fora_da_janela"
    POST_POSTERIOR_JA_SAIU = "post_posterior_ja_saiu"
    SERIE_BLOQUEADA = "serie_bloqueada"
    SEM_ADAPTADOR = "sem_adaptador"
    FORMATO_SEM_TIPO = "formato_sem_tipo"
    CONEXAO_INVALIDA = "conexao_invalida"
    ARTE_INVALIDA = "arte_invalida"
    EXECUCAO_EXISTENTE = "execucao_existente"


@dataclass(frozen=True)
class ResultadoGuarda:
    permitido: bool
    motivos: tuple[MotivoRecusa, ...] = ()
    detalhe: str | None = None

    def __post_init__(self) -> None:
        if self.permitido and self.motivos:
            raise ValueError("resultado permitido não pode ter motivos de recusa")
        if not self.permitido and not self.motivos:
            raise ValueError("resultado recusado exige pelo menos um motivo")

    @classmethod
    def permitir(cls) -> ResultadoGuarda:
        return cls(permitido=True)

    @classmethod
    def recusar(cls, *motivos: MotivoRecusa, detalhe: str | None = None) -> ResultadoGuarda:
        return cls(permitido=False, motivos=tuple(motivos), detalhe=detalhe)
