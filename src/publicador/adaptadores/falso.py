"""Adaptador falso / simulação (RF-17): implementa a interface inteira SEM rede.

* Aplica as mesmas validações de mídia e conexão que um adaptador real e não envia nada.
* Registra em memória as chamadas (só metodo, plataforma, chave: nunca conteúdo do post).
* Permite programar respostas para testar o orquestrador (T038/T040).
* Idempotente: a mesma chave não "publica" duas vezes; devolve o mesmo resultado
  (sucesso, modo privado e incerto ficam gravados; falhas não, para permitir nova tentativa).

I-1: a credencial opcional é guardada como ``SegredoStr`` apenas para exercitar o contrato
(repr nunca a mostra) e nunca é usada nem logada. I-2: o adaptador não decide autorização.
"""

from __future__ import annotations

import hashlib
import logging
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final

from publicador.adaptadores.base import (
    EstadoConexao,
    EstadoConsulta,
    PedidoDePublicacao,
    ProblemaDeMidia,
    ResultadoConsulta,
    ResultadoPublicacao,
    ResultadoValidacao,
    TipoDeProblema,
    TipoDeResultado,
)
from publicador.artes.armazem import (
    ArmazemDeArtes,
    CaminhoDeArteInvalido,
    ErroAcessoArmazem,
    validar_caminho_de_arte,
)
from publicador.core.modelos import ConexaoEstado, Post
from publicador.core.redacao import SegredoStr

__all__ = ["AdaptadorFalso", "ChamadaRegistrada", "RegraDeFormato", "regras_padrao"]

_log = logging.getLogger("publicador.adaptadores.falso")
_DOMINIO_FICTICIO: Final = "simulado.invalid"  # TLD reservado: nunca resolve


@dataclass(frozen=True)
class RegraDeFormato:
    minimo_de_artes: int
    maximo_de_artes: int
    extensoes: frozenset[str]


_IMAGEM: Final = frozenset({".jpg", ".jpeg", ".png"})
_VIDEO: Final = frozenset({".mp4", ".mov"})


def regras_padrao(plataforma: str) -> Mapping[str, RegraDeFormato]:
    """Mapeamento padrão de formato por plataforma (A-023); chaves em minúsculas."""
    regras = {
        "feed": RegraDeFormato(1, 10, _IMAGEM),
        "reels": RegraDeFormato(1, 1, _VIDEO),
    }
    if plataforma.strip().casefold() == "instagram":
        regras["stories"] = RegraDeFormato(1, 1, _IMAGEM | _VIDEO)
    return regras


@dataclass(frozen=True)
class ChamadaRegistrada:
    metodo: str
    plataforma: str
    chave_idempotencia: str | None = None
    reaproveitada: bool = False


class AdaptadorFalso:
    """Implementa ``PlatformAdapter`` em memória, sem rede."""

    def __init__(
        self,
        plataforma: str,
        *,
        armazem: ArmazemDeArtes | None = None,
        regras: Mapping[str, RegraDeFormato] | None = None,
        estado_conexao: ConexaoEstado = ConexaoEstado.ATIVA,
        dias_restantes_conexao: int | None = None,
        limite_diario: int | None = None,
        credencial: SegredoStr | None = None,
    ) -> None:
        if not plataforma.strip():
            raise ValueError("plataforma não pode ser vazia")
        self._plataforma = plataforma
        self._armazem = armazem
        origem = regras if regras is not None else regras_padrao(plataforma)
        self._regras = {k.strip().casefold(): v for k, v in origem.items()}
        self._estado_conexao = estado_conexao
        self._dias = dias_restantes_conexao
        self._limite = limite_diario
        self._credencial = credencial  # nunca usada nem exibida
        self._concluidas: dict[str, ResultadoPublicacao] = {}
        self._fila_publicacao: deque[TipoDeResultado] = deque()
        self._fila_consulta: deque[EstadoConsulta] = deque()
        self._chamadas: list[ChamadaRegistrada] = []
        self._efetivas = 0

    # ------------------------------------------------------------------ introspecção

    def __repr__(self) -> str:
        return f"AdaptadorFalso(plataforma={self._plataforma!r})"

    __str__ = __repr__

    @property
    def plataforma(self) -> str:
        return self._plataforma

    @property
    def chamadas(self) -> tuple[ChamadaRegistrada, ...]:
        return tuple(self._chamadas)

    @property
    def publicacoes_efetivas(self) -> int:
        """Quantas publicações (simuladas) foram aceitas, sem contar reaproveitadas."""
        return self._efetivas

    # ------------------------------------------------------------------ programação

    def programar_publicacao(self, *tipos: TipoDeResultado) -> None:
        """Enfileira resultados para as próximas publicações que passarem nas validações."""
        self._fila_publicacao.extend(tipos)

    def programar_consulta(self, *estados: EstadoConsulta) -> None:
        self._fila_consulta.extend(estados)

    # ------------------------------------------------------------------ interface

    def verificar_conexao(self) -> EstadoConexao:
        self._chamadas.append(ChamadaRegistrada("verificar_conexao", self._plataforma))
        dias = self._dias if self._estado_conexao is ConexaoEstado.ATIVA else None
        return EstadoConexao(self._plataforma, self._estado_conexao, dias)

    def limite_diario_restante(self) -> int | None:
        self._chamadas.append(ChamadaRegistrada("limite_diario_restante", self._plataforma))
        return self._limite

    def validar_midia(self, post: Post, artes: Sequence[str]) -> ResultadoValidacao:
        self._chamadas.append(ChamadaRegistrada("validar_midia", self._plataforma))
        return self._validar(post, artes)

    def publicar(self, pedido: PedidoDePublicacao) -> ResultadoPublicacao:
        chave = pedido.chave_idempotencia
        anterior = self._concluidas.get(chave)
        if anterior is not None:
            self._chamadas.append(
                ChamadaRegistrada("publicar", self._plataforma, chave, reaproveitada=True)
            )
            return anterior
        self._chamadas.append(ChamadaRegistrada("publicar", self._plataforma, chave))

        if self._estado_conexao is not ConexaoEstado.ATIVA:
            return self._falha(pedido, TipoDeResultado.FALHA_PERMANENTE, "conexao_invalida")
        if not self._validar(pedido.post, pedido.artes).valida:
            return self._falha(pedido, TipoDeResultado.FALHA_PERMANENTE, "midia_invalida")
        if self._limite is not None and self._limite <= 0:
            return self._falha(pedido, TipoDeResultado.FALHA_TRANSITORIA, "limite_diario")

        tipo = self._fila_publicacao.popleft() if self._fila_publicacao else TipoDeResultado.SUCESSO
        if tipo in (TipoDeResultado.FALHA_TRANSITORIA, TipoDeResultado.FALHA_PERMANENTE):
            return self._falha(pedido, tipo, "programado")

        if tipo is TipoDeResultado.INCERTO:
            resultado = ResultadoPublicacao(
                tipo, chave, simulado=pedido.simular, motivo="programado"
            )
        else:
            identificador = "simulado-" + hashlib.sha256(chave.encode("utf-8")).hexdigest()[:12]
            resultado = ResultadoPublicacao(
                tipo,
                chave,
                id_externo=identificador,
                url=f"https://{_DOMINIO_FICTICIO}/{self._plataforma}/{identificador}",
                simulado=pedido.simular,
            )
            self._efetivas += 1
            if self._limite is not None:
                self._limite -= 1
        self._concluidas[chave] = resultado
        _log.info("publicar plataforma=%s resultado=%s", self._plataforma, tipo.value)
        return resultado

    def consultar(self, chave_idempotencia: str) -> ResultadoConsulta:
        self._chamadas.append(ChamadaRegistrada("consultar", self._plataforma, chave_idempotencia))
        gravado = self._concluidas.get(chave_idempotencia)
        if self._fila_consulta:
            estado = self._fila_consulta.popleft()
            if estado is EstadoConsulta.ENCONTRADO:
                if gravado is not None and gravado.id_externo is not None:
                    return ResultadoConsulta(estado, gravado.id_externo, gravado.url)
                identificador = "simulado-programado"
                return ResultadoConsulta(
                    estado, identificador, f"https://{_DOMINIO_FICTICIO}/{identificador}"
                )
            return ResultadoConsulta(estado)
        if gravado is None:
            return ResultadoConsulta(EstadoConsulta.NAO_ENCONTRADO)
        if gravado.tipo is TipoDeResultado.INCERTO:
            return ResultadoConsulta(EstadoConsulta.DESCONHECIDO)
        return ResultadoConsulta(EstadoConsulta.ENCONTRADO, gravado.id_externo, gravado.url)

    # ------------------------------------------------------------------ internos

    def _falha(
        self, pedido: PedidoDePublicacao, tipo: TipoDeResultado, motivo: str
    ) -> ResultadoPublicacao:
        _log.info(
            "publicar plataforma=%s resultado=%s motivo=%s", self._plataforma, tipo.value, motivo
        )
        return ResultadoPublicacao(
            tipo, pedido.chave_idempotencia, simulado=pedido.simular, motivo=motivo
        )

    def _validar(self, post: Post, artes: Sequence[str]) -> ResultadoValidacao:
        problemas: list[ProblemaDeMidia] = []
        alvo = self._plataforma.strip().casefold()
        if alvo not in {p.strip().casefold() for p in post.plataformas}:
            problemas.append(ProblemaDeMidia(TipoDeProblema.PLATAFORMA_FORA_DO_POST))
        if post.formato is None or not post.formato.strip():
            problemas.append(ProblemaDeMidia(TipoDeProblema.FORMATO_AUSENTE))
            return ResultadoValidacao(tuple(problemas))
        regra = self._regras.get(post.formato.strip().casefold())
        if regra is None:
            problemas.append(ProblemaDeMidia(TipoDeProblema.FORMATO_NAO_SUPORTADO))
            return ResultadoValidacao(tuple(problemas))
        if not regra.minimo_de_artes <= len(artes) <= regra.maximo_de_artes:
            problemas.append(ProblemaDeMidia(TipoDeProblema.QUANTIDADE_DE_ARTES))
        for indice, arte in enumerate(artes):
            problema = self._problema_da_arte(arte, regra)
            if problema is not None:
                problemas.append(ProblemaDeMidia(problema, indice))
        return ResultadoValidacao(tuple(problemas))

    def _problema_da_arte(self, arte: str, regra: RegraDeFormato) -> TipoDeProblema | None:
        try:
            validar_caminho_de_arte(arte)
        except CaminhoDeArteInvalido:
            return TipoDeProblema.CAMINHO_INVALIDO
        if PurePosixPath(arte).suffix.casefold() not in regra.extensoes:
            return TipoDeProblema.EXTENSAO_NAO_SUPORTADA
        if self._armazem is None:
            return None
        try:
            if not self._armazem.existe(arte):
                return TipoDeProblema.ARTE_NAO_ENCONTRADA
        except CaminhoDeArteInvalido:
            return TipoDeProblema.CAMINHO_INVALIDO
        except ErroAcessoArmazem:
            return TipoDeProblema.ARMAZEM_INDISPONIVEL
        return None
