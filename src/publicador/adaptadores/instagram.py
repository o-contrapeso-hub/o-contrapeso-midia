"""Adaptador Instagram, parte 1 (T043): conexão, validação de mídia e limite diário.

Esta parte NÃO publica (publicar é T044 a T046): ``publicar`` recusa com
``FALHA_PERMANENTE``/``nao_implementado`` sem tocar a rede e jamais devolve sucesso;
``consultar`` devolve ``DESCONHECIDO`` (a T047 implementa de verdade). O adaptador não é
registrado no ``__main__`` (modo real segue sem adaptadores até a T049).

Regras:

* I-1: o token chega como ``SegredoStr`` e só é revelado ao montar o cabeçalho
  ``Authorization`` (e o parâmetro ``input_token`` do ``debug_token``). Nenhuma exceção,
  log, ``repr`` ou retorno contém token, URL, cabeçalhos ou corpo bruto de resposta.
* I-2: o adaptador não decide autorização; quem o chama já passou pela guarda.
* Lado seguro (RF-18): resposta malformada, status inesperado ou erro de rede nunca
  resultam em conexão ``ATIVA`` (viram ``INVALIDA``); falha na consulta do limite diário
  vira "indisponível" e ``limite_diario_restante()`` devolve ``0`` (``None`` significaria
  "sem limite" no protocolo, o que seria o lado inseguro).

Transporte: reaproveita o ``TransporteHttp`` (GET, sem redirecionamento) do armazém de
artes; sem biblioteca nova.

Regras de mídia (RF-14, RF-43; fontes na documentação de Content Publishing da Meta):

* Imagens: somente JPEG (extensão ``.jpg``/``.jpeg`` E assinatura ``FFD8FF``), até 8 MB.
* Feed: 1 imagem (4:5 a 1,91:1) ou carrossel de 2 a 10 imagens, cada uma no mesmo
  intervalo. ``Carrossel`` é aceito como formato (2 a 10).
* Stories: 1 item; imagem JPEG em 9:16 (tolerância de 2%, decisão do lado seguro) ou vídeo.
* Reels: 1 vídeo (``.mp4``/``.mov``). Proporção e duração do vídeo NÃO são verificadas
  (exigiria ler o contêiner); fica como pendência.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import PurePosixPath
from typing import Final
from urllib.parse import quote

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
    ArteNaoEncontrada,
    CaminhoDeArteInvalido,
    ErroAcessoArmazem,
    TransporteHttp,
    validar_caminho_de_arte,
)
from publicador.core.modelos import ConexaoEstado, Post
from publicador.core.redacao import SegredoStr, obter_logger

__all__ = [
    "PLATAFORMA",
    "URL_BASE_GRAPH",
    "VERSAO_GRAPH",
    "AdaptadorInstagram",
    "LimiteDiario",
]

_log = obter_logger("publicador.adaptadores.instagram")

PLATAFORMA: Final = "instagram"
URL_BASE_GRAPH: Final = "https://graph.facebook.com"
VERSAO_GRAPH: Final = "v21.0"  # fixada; validar contra a conta de teste antes da F2
TIMEOUT_PADRAO: Final = 15.0
_TAMANHO_MAX_IMAGEM: Final = 8 * 1024 * 1024
_ESCOPOS_PUBLICACAO: Final = frozenset(
    {"instagram_content_publish", "instagram_business_content_publish"}
)
_EXT_IMAGEM: Final = frozenset({".jpg", ".jpeg"})
_EXT_VIDEO: Final = frozenset({".mp4", ".mov"})
_RE_ID_CONTA: Final = re.compile(r"[0-9]{1,30}")
_RE_VERSAO: Final = re.compile(r"v[0-9]{1,3}\.[0-9]{1,2}")

_FEED_MIN: Final = Fraction(4, 5)
_FEED_MAX: Final = Fraction(191, 100)
_STORY_ALVO: Final = Fraction(9, 16)
_STORY_TOL: Final = Fraction(2, 100)


@dataclass(frozen=True)
class _Regra:
    minimo: int
    maximo: int
    imagem: bool
    video: bool
    proporcao: tuple[Fraction, Fraction] | None  # só para imagens


_REGRAS: Final[dict[str, _Regra]] = {
    "feed": _Regra(1, 10, True, False, (_FEED_MIN, _FEED_MAX)),
    "carrossel": _Regra(2, 10, True, False, (_FEED_MIN, _FEED_MAX)),
    "stories": _Regra(
        1, 1, True, True, (_STORY_ALVO * (1 - _STORY_TOL), _STORY_ALVO * (1 + _STORY_TOL))
    ),
    "reels": _Regra(1, 1, False, True, None),
}


@dataclass(frozen=True)
class LimiteDiario:
    """Cota de publicações pela API nas últimas 24 h."""

    usado: int
    total: int

    @property
    def restante(self) -> int:
        return max(0, self.total - self.usado)


def _inteiro(valor: object) -> int | None:
    return valor if isinstance(valor, int) and not isinstance(valor, bool) else None


def _dimensoes_jpeg(dados: bytes) -> tuple[int, int] | None:
    """(largura, altura) lidas do primeiro marcador SOF; ``None`` se ilegível."""
    n = len(dados)
    i = 2
    while i + 4 <= n:
        if dados[i] != 0xFF:
            return None
        while i < n and dados[i] == 0xFF:  # bytes de preenchimento
            i += 1
        if i >= n:
            return None
        marcador = dados[i]
        i += 1
        if marcador == 0x01 or 0xD0 <= marcador <= 0xD8:
            continue
        if marcador in (0xD9, 0xDA):
            return None
        if i + 2 > n:
            return None
        tamanho = int.from_bytes(dados[i : i + 2], "big")
        if tamanho < 2:
            return None
        if 0xC0 <= marcador <= 0xCF and marcador not in (0xC4, 0xC8, 0xCC):
            if tamanho < 8 or i + 7 > n:
                return None
            altura = int.from_bytes(dados[i + 3 : i + 5], "big")
            largura = int.from_bytes(dados[i + 5 : i + 7], "big")
            return (largura, altura) if largura > 0 and altura > 0 else None
        i += tamanho
    return None


class AdaptadorInstagram:
    """Implementa ``PlatformAdapter`` para o Instagram (parte 1, sem publicação)."""

    def __init__(
        self,
        token: SegredoStr,
        id_conta: str,
        transporte: TransporteHttp,
        armazem: ArmazemDeArtes,
        relogio: Callable[[], datetime],
        *,
        url_base: str = URL_BASE_GRAPH,
        versao: str = VERSAO_GRAPH,
        timeout: float = TIMEOUT_PADRAO,
    ) -> None:
        if not isinstance(token, SegredoStr):
            raise TypeError("O token deve ser um SegredoStr (regra I-1).")
        if _RE_ID_CONTA.fullmatch(id_conta) is None:
            raise ValueError("id_conta inválido: deve ser numérico.")
        if not url_base.startswith("https://") or url_base.endswith("/"):
            raise ValueError("url_base inválida: use https sem barra final.")
        if _RE_VERSAO.fullmatch(versao) is None:
            raise ValueError("versao inválida: use o formato vNN.N.")
        if timeout <= 0:
            raise ValueError("timeout deve ser positivo.")
        self._token = token
        self._id_conta = id_conta
        self._transporte = transporte
        self._armazem = armazem
        self._relogio = relogio
        self._base = f"{url_base}/{versao}"
        self._timeout = timeout

    def __repr__(self) -> str:
        return f"AdaptadorInstagram(conta={self._id_conta})"

    __str__ = __repr__

    @property
    def plataforma(self) -> str:
        return PLATAFORMA

    # ------------------------------------------------------------------ HTTP interno

    def _obter_json(self, url: str, operacao: str) -> dict[str, object] | None:
        """GET autenticado; ``None`` em qualquer falha (sem vazar nada)."""
        cabecalhos = {
            "Authorization": f"Bearer {self._token.revelar()}",
            "Accept": "application/json",
            "User-Agent": "o-publicador",
        }
        try:
            status, corpo = self._transporte.obter(url, cabecalhos, self._timeout)
        except Exception:
            _log.warning("instagram %s: falha de comunicação", operacao)
            return None
        finally:
            cabecalhos.clear()
        if status != 200:
            _log.warning("instagram %s: status inesperado (%s)", operacao, status)
            return None
        try:
            dados = json.loads(corpo)
        except (ValueError, RecursionError):
            _log.warning("instagram %s: resposta malformada", operacao)
            return None
        if not isinstance(dados, dict) or "error" in dados:
            _log.warning("instagram %s: resposta recusada ou malformada", operacao)
            return None
        return dados

    # ------------------------------------------------------------------ conexão (RF-18)

    def verificar_conexao(self) -> EstadoConexao:
        agora = self._relogio()
        if agora.tzinfo is None or agora.utcoffset() is None:
            raise ValueError("o relógio deve devolver datetime com fuso")
        invalida = EstadoConexao(PLATAFORMA, ConexaoEstado.INVALIDA)
        url = f"{self._base}/debug_token?input_token={quote(self._token.revelar(), safe='')}"
        resposta = self._obter_json(url, "verificar_conexao")
        if resposta is None:
            return invalida
        dados = resposta.get("data")
        if not isinstance(dados, dict):
            return invalida
        expira = _inteiro(dados.get("expires_at"))
        if expira is None or expira < 0:
            return invalida
        valido = dados.get("is_valid")
        if valido is False:
            if expira > 0 and expira <= agora.timestamp():
                return EstadoConexao(PLATAFORMA, ConexaoEstado.EXPIRADA)
            return invalida
        if valido is not True or "error" in dados:
            return invalida
        escopos = dados.get("scopes")
        if not isinstance(escopos, list) or not _ESCOPOS_PUBLICACAO.intersection(
            e for e in escopos if isinstance(e, str)
        ):
            return invalida
        if expira == 0:  # a Graph API usa 0 para "não expira"
            return EstadoConexao(PLATAFORMA, ConexaoEstado.ATIVA)
        return EstadoConexao.de_expiracao(PLATAFORMA, datetime.fromtimestamp(expira, UTC), agora)

    # ------------------------------------------------------------------ limite diário

    def limite_diario(self) -> LimiteDiario | None:
        """Cota usada/total nas últimas 24 h; ``None`` = indisponível (tratar como bloqueado)."""
        url = f"{self._base}/{self._id_conta}/content_publishing_limit?fields=quota_usage,config"
        resposta = self._obter_json(url, "limite_diario")
        if resposta is None:
            return None
        itens = resposta.get("data")
        if not isinstance(itens, list) or not itens or not isinstance(itens[0], dict):
            return None
        usado = _inteiro(itens[0].get("quota_usage"))
        config = itens[0].get("config")
        total = _inteiro(config.get("quota_total")) if isinstance(config, dict) else None
        if usado is None or total is None or usado < 0 or total <= 0:
            return None
        return LimiteDiario(usado, total)

    def limite_diario_restante(self) -> int | None:
        limite = self.limite_diario()
        return 0 if limite is None else limite.restante

    # ------------------------------------------------------------------ mídia (RF-14, RF-43)

    def validar_midia(self, post: Post, artes: Sequence[str]) -> ResultadoValidacao:
        problemas: list[ProblemaDeMidia] = []
        if PLATAFORMA not in {p.strip().casefold() for p in post.plataformas}:
            problemas.append(ProblemaDeMidia(TipoDeProblema.PLATAFORMA_FORA_DO_POST))
        if post.formato is None or not post.formato.strip():
            problemas.append(ProblemaDeMidia(TipoDeProblema.FORMATO_AUSENTE))
            return ResultadoValidacao(tuple(problemas))
        regra = _REGRAS.get(post.formato.strip().casefold())
        if regra is None:
            problemas.append(ProblemaDeMidia(TipoDeProblema.FORMATO_NAO_SUPORTADO))
            return ResultadoValidacao(tuple(problemas))
        if not regra.minimo <= len(artes) <= regra.maximo:
            problemas.append(ProblemaDeMidia(TipoDeProblema.QUANTIDADE_DE_ARTES))
        for indice, arte in enumerate(artes):
            for tipo in self._problemas_da_arte(arte, regra):
                problemas.append(ProblemaDeMidia(tipo, indice))
        return ResultadoValidacao(tuple(problemas))

    def _problemas_da_arte(self, arte: str, regra: _Regra) -> list[TipoDeProblema]:
        try:
            validar_caminho_de_arte(arte)
        except CaminhoDeArteInvalido:
            return [TipoDeProblema.CAMINHO_INVALIDO]
        extensao = PurePosixPath(arte).suffix.casefold()
        eh_imagem = extensao in _EXT_IMAGEM and regra.imagem
        eh_video = extensao in _EXT_VIDEO and regra.video
        if not (eh_imagem or eh_video):
            return [TipoDeProblema.EXTENSAO_NAO_SUPORTADA]
        try:
            if eh_video:
                return [] if self._armazem.existe(arte) else [TipoDeProblema.ARTE_NAO_ENCONTRADA]
            dados = self._armazem.ler(arte)
        except ArteNaoEncontrada:
            return [TipoDeProblema.ARTE_NAO_ENCONTRADA]
        except CaminhoDeArteInvalido:
            return [TipoDeProblema.CAMINHO_INVALIDO]
        except ErroAcessoArmazem:
            return [TipoDeProblema.ARMAZEM_INDISPONIVEL]
        return self._problemas_da_imagem(dados, regra)

    @staticmethod
    def _problemas_da_imagem(dados: bytes, regra: _Regra) -> list[TipoDeProblema]:
        if not dados.startswith(b"\xff\xd8\xff"):
            return [TipoDeProblema.TIPO_DE_ARQUIVO_INVALIDO]
        problemas: list[TipoDeProblema] = []
        if len(dados) > _TAMANHO_MAX_IMAGEM:
            problemas.append(TipoDeProblema.TAMANHO_EXCEDIDO)
        dimensoes = _dimensoes_jpeg(dados)
        if dimensoes is None:
            problemas.append(TipoDeProblema.IMAGEM_ILEGIVEL)
        elif regra.proporcao is not None:
            minimo, maximo = regra.proporcao
            if not minimo <= Fraction(dimensoes[0], dimensoes[1]) <= maximo:
                problemas.append(TipoDeProblema.PROPORCAO_FORA_DO_PERMITIDO)
        return problemas

    # ------------------------------------------------------------------ publicar / consultar

    def publicar(self, pedido: PedidoDePublicacao) -> ResultadoPublicacao:
        """Bloqueado nesta parte (T044 a T046): sem rede e NUNCA sucesso (I-2)."""
        return ResultadoPublicacao(
            TipoDeResultado.FALHA_PERMANENTE,
            pedido.chave_idempotencia,
            simulado=pedido.simular,
            motivo="nao_implementado",
        )

    def consultar(self, chave_idempotencia: str) -> ResultadoConsulta:
        """Sem consulta real até a T047: DESCONHECIDO (nunca autoriza nova tentativa)."""
        return ResultadoConsulta(EstadoConsulta.DESCONHECIDO)
