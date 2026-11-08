"""Adaptador Instagram: conexão, mídia, limite diário (T043) e vídeo/Reels por upload
resumable (T045).

``publicar`` publica SOMENTE vídeo (Reels, ou Story em vídeo) por upload resumable, sem URL
pública; imagens e carrossel seguem devolvendo ``FALHA_PERMANENTE``/``nao_implementado``
sem tocar a rede (T044). ``consultar`` devolve ``DESCONHECIDO`` (a T047 implementa de
verdade). O adaptador não é registrado no ``__main__`` (modo real segue sem adaptadores
até a T049).

Fluxo real do vídeo (cada etapa só começa se a anterior deu certo):

1. ``verificar_conexao()`` ATIVA e ``limite_diario_restante()`` > 0 (senão nada é enviado);
2. ``POST /{conta}/media`` (``upload_type=resumable``, ``media_type`` REELS/STORIES, legenda)
   devolve ``id`` do container e ``uri`` de upload;
3. ``POST {uri}`` com os bytes (``Authorization: OAuth``, ``offset``, ``file_size``);
4. ``GET /{container}?fields=status_code`` até ``FINISHED`` (sondagem limitada);
5. ``POST /{conta}/media_publish`` com ``creation_id``: o ÚNICO passo que publica.

Classificação (RF-12, RF-25, RF-38), a parte crítica:

* Falha ANTES de enviar ``media_publish``: erro de rede/5xx/429/limite = ``FALHA_TRANSITORIA``;
  4xx de validação/permissão/token e resposta fora do contrato = ``FALHA_PERMANENTE``.
* Depois de ENVIAR ``media_publish`` qualquer dúvida (timeout, queda, QUALQUER status que
  não seja 2xx com ``id`` válido, corpo malformado, sucesso sem ``id``) = ``INCERTO``: pode
  ter saído, nunca retentar. Sucesso só com ``id`` de mídia válido. Decisão conservadora:
  até um 4xx claro após o envio vira ``INCERTO`` (custa uma resolução manual, nunca um
  post duplicado); refinar só depois de teste real.
* Container já ``PUBLISHED`` na sondagem (sem ``media_publish`` nosso) = ``INCERTO``.

Idempotência: a Graph API não tem chave nativa. O adaptador guarda, NA INSTÂNCIA e por
``chave_idempotencia``, o andamento (container, upload feito, publish enviado, resultado).
Repetir o pedido reaproveita o container, nunca reenvia ``media_publish`` depois de enviado
e devolve o mesmo sucesso. Limitação: o estado vive só na instância (um novo processo
começa do zero); entre processos quem protege é o registro ``iniciada`` do orquestrador e a
``consultar`` da T047.

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

import hashlib
import json
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import PurePosixPath
from typing import Final
from urllib.parse import quote, urlencode, urlsplit

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
    TransporteHttpEscrita,
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
_RE_ID_GRAPH: Final = re.compile(r"[0-9]{1,40}")
_LEGENDA_MAX: Final = 2200
_TIMEOUT_UPLOAD: Final = 300.0
_INTERVALO_STATUS: Final = 5.0
_MAX_TENTATIVAS_STATUS: Final = 36
_HOSTS_UPLOAD: Final = frozenset({"rupload.facebook.com"})
_DOMINIO_FICTICIO: Final = "simulado.invalid"
# Códigos de erro da Graph API (de memória; validar contra a conta de teste).
_CODIGOS_LIMITE: Final = frozenset({4, 17, 32, 341, 613})
_CODIGOS_TEMPORARIOS: Final = frozenset({1, 2})

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


class _Encerrar(Exception):
    """Interrompe o fluxo com um resultado já classificado (mensagem sempre fixa)."""

    def __init__(self, tipo: TipoDeResultado, motivo: str) -> None:
        super().__init__("fluxo de publicacao encerrado")
        self.tipo = tipo
        self.motivo = motivo


@dataclass
class _Andamento:
    """Estado de uma chave de idempotência dentro da instância."""

    container: str | None = None
    uri: str | None = None
    enviado: bool = False
    publish_enviado: bool = False
    resultado: ResultadoPublicacao | None = None


def _json_dict(corpo: bytes) -> dict[str, object] | None:
    try:
        dados = json.loads(corpo)
    except (ValueError, RecursionError):
        return None
    return dados if isinstance(dados, dict) else None


def _classificar_erro(status: int, corpo: bytes) -> tuple[TipoDeResultado, str]:
    """Erro ANTES de ``media_publish``. O corpo só é lido por campos estritos (int/bool)."""
    erro = (_json_dict(corpo) or {}).get("error")
    codigo = _inteiro(erro.get("code")) if isinstance(erro, dict) else None
    transitorio = isinstance(erro, dict) and erro.get("is_transient") is True
    sufixo = f"_{codigo}" if codigo is not None else ""
    if status == 429 or status >= 500:
        return TipoDeResultado.FALHA_TRANSITORIA, f"http_{status}"
    if 300 <= status < 400:
        return TipoDeResultado.FALHA_PERMANENTE, "redirecionamento"
    if 400 <= status < 500:
        if codigo in _CODIGOS_LIMITE:
            return TipoDeResultado.FALHA_TRANSITORIA, f"limite_taxa{sufixo}"
        if codigo in _CODIGOS_TEMPORARIOS or transitorio:
            return TipoDeResultado.FALHA_TRANSITORIA, f"erro_temporario{sufixo}"
        return TipoDeResultado.FALHA_PERMANENTE, f"http_{status}{sufixo}"
    return TipoDeResultado.FALHA_PERMANENTE, "status_inesperado"


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
        timeout_upload: float = _TIMEOUT_UPLOAD,
        intervalo_status: float = _INTERVALO_STATUS,
        max_tentativas_status: int = _MAX_TENTATIVAS_STATUS,
        dormir: Callable[[float], None] = time.sleep,
        hosts_upload: frozenset[str] = _HOSTS_UPLOAD,
    ) -> None:
        if not isinstance(token, SegredoStr):
            raise TypeError("O token deve ser um SegredoStr (regra I-1).")
        if _RE_ID_CONTA.fullmatch(id_conta) is None:
            raise ValueError("id_conta inválido: deve ser numérico.")
        if not url_base.startswith("https://") or url_base.endswith("/"):
            raise ValueError("url_base inválida: use https sem barra final.")
        if _RE_VERSAO.fullmatch(versao) is None:
            raise ValueError("versao inválida: use o formato vNN.N.")
        if timeout <= 0 or timeout_upload <= 0:
            raise ValueError("timeout deve ser positivo.")
        if intervalo_status < 0 or max_tentativas_status < 1:
            raise ValueError("parâmetros de sondagem inválidos.")
        self._token = token
        self._id_conta = id_conta
        self._transporte = transporte
        self._armazem = armazem
        self._relogio = relogio
        self._base = f"{url_base}/{versao}"
        self._timeout = timeout
        self._timeout_upload = timeout_upload
        self._intervalo_status = intervalo_status
        self._max_tentativas_status = max_tentativas_status
        self._dormir = dormir
        self._hosts_upload = hosts_upload
        self._andamentos: dict[str, _Andamento] = {}

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
        """Vídeo (Reels/Story em vídeo) por upload resumable; o resto é da T044 (I-2: quem
        decide se pode publicar é a guarda do orquestrador, nunca este método)."""
        chave = pedido.chave_idempotencia
        tipo_midia = self._tipo_de_video(pedido)
        if tipo_midia is None:
            return ResultadoPublicacao(
                TipoDeResultado.FALHA_PERMANENTE,
                chave,
                simulado=pedido.simular,
                motivo="nao_implementado",
            )
        if pedido.plataforma.strip().casefold() != PLATAFORMA:
            return ResultadoPublicacao(
                TipoDeResultado.FALHA_PERMANENTE,
                chave,
                simulado=pedido.simular,
                motivo="plataforma_incorreta",
            )
        if pedido.simular:
            return self._simular(pedido)
        try:
            return self._publicar_video(pedido, tipo_midia)
        except _Encerrar as parada:
            return ResultadoPublicacao(parada.tipo, chave, motivo=parada.motivo)
        except Exception as erro:  # noqa: BLE001 - defeito interno: nunca vaza detalhe
            _log.error("instagram publicar: erro interno (%s)", type(erro).__name__)
            andamento = self._andamentos.get(chave)
            if andamento is not None and andamento.publish_enviado:
                return ResultadoPublicacao(
                    TipoDeResultado.INCERTO, chave, motivo="erro_interno_apos_publish"
                )
            return ResultadoPublicacao(
                TipoDeResultado.FALHA_PERMANENTE, chave, motivo="erro_interno"
            )

    # ------------------------------------------------------------------ vídeo (T045)

    @staticmethod
    def _tipo_de_video(pedido: PedidoDePublicacao) -> str | None:
        """``media_type`` do vídeo ou ``None`` (imagem/carrossel/formato desconhecido)."""
        formato = (pedido.post.formato or "").strip().casefold()
        if formato == "reels":
            return "REELS"
        if formato == "stories" and len(pedido.artes) == 1:
            if PurePosixPath(pedido.artes[0]).suffix.casefold() in _EXT_VIDEO:
                return "STORIES"
        return None

    def _problema_local(self, pedido: PedidoDePublicacao) -> tuple[TipoDeResultado, str] | None:
        """Validações sem Instagram (mídia no armazém e legenda)."""
        problemas = self.validar_midia(pedido.post, pedido.artes).problemas
        if problemas:
            if all(p.tipo is TipoDeProblema.ARMAZEM_INDISPONIVEL for p in problemas):
                return TipoDeResultado.FALHA_TRANSITORIA, "armazem_indisponivel"
            return TipoDeResultado.FALHA_PERMANENTE, "midia_invalida"
        if len(pedido.conteudo) > _LEGENDA_MAX:
            return TipoDeResultado.FALHA_PERMANENTE, "legenda_longa"
        return None

    def _simular(self, pedido: PedidoDePublicacao) -> ResultadoPublicacao:
        """Sem nenhuma chamada ao Instagram: mesmas validações locais, nada enviado."""
        chave = pedido.chave_idempotencia
        try:
            problema = self._problema_local(pedido)
        except Exception:  # noqa: BLE001 - simulação nunca derruba o job
            problema = (TipoDeResultado.FALHA_PERMANENTE, "erro_interno")
        if problema is not None:
            return ResultadoPublicacao(problema[0], chave, simulado=True, motivo=problema[1])
        identificador = "simulado-" + hashlib.sha256(chave.encode("utf-8")).hexdigest()[:12]
        return ResultadoPublicacao(
            TipoDeResultado.SUCESSO,
            chave,
            id_externo=identificador,
            url=f"https://{_DOMINIO_FICTICIO}/{PLATAFORMA}/{identificador}",
            simulado=True,
        )

    def _cabecalhos_graph(self, *, formulario: bool = False) -> dict[str, str]:
        cabecalhos = {
            "Authorization": f"Bearer {self._token.revelar()}",
            "Accept": "application/json",
            "User-Agent": "o-publicador",
        }
        if formulario:
            cabecalhos["Content-Type"] = "application/x-www-form-urlencoded"
        return cabecalhos

    def _escrita(self) -> TransporteHttpEscrita:
        transporte = self._transporte
        if not (callable(getattr(transporte, "postar", None))):
            raise _Encerrar(TipoDeResultado.FALHA_PERMANENTE, "transporte_sem_escrita")
        return transporte  # type: ignore[return-value]

    def _etapa(self, operacao: str, chamada: Callable[[], tuple[int, bytes]]) -> dict[str, object]:
        """Etapa ANTES de ``media_publish``: 2xx com objeto JSON sem ``error`` ou encerra."""
        try:
            status, corpo = chamada()
        except Exception:  # noqa: BLE001 - rede/timeout: nada foi publicado ainda
            _log.warning("instagram %s: falha de comunicação", operacao)
            raise _Encerrar(TipoDeResultado.FALHA_TRANSITORIA, f"{operacao}_rede") from None
        if not 200 <= status < 300:
            tipo, motivo = _classificar_erro(status, corpo)
            _log.warning("instagram %s: status %s", operacao, status)
            raise _Encerrar(tipo, f"{operacao}_{motivo}")
        dados = _json_dict(corpo)
        if dados is None or "error" in dados:
            _log.warning("instagram %s: resposta fora do contrato", operacao)
            raise _Encerrar(TipoDeResultado.FALHA_PERMANENTE, f"{operacao}_fora_do_contrato")
        return dados

    def _ler_video(self, arte: str) -> bytes:
        try:
            dados = self._armazem.ler(arte)
        except ArteNaoEncontrada:
            raise _Encerrar(TipoDeResultado.FALHA_PERMANENTE, "arte_nao_encontrada") from None
        except CaminhoDeArteInvalido:
            raise _Encerrar(TipoDeResultado.FALHA_PERMANENTE, "caminho_invalido") from None
        except ErroAcessoArmazem:
            raise _Encerrar(TipoDeResultado.FALHA_TRANSITORIA, "armazem_indisponivel") from None
        if not dados:
            raise _Encerrar(TipoDeResultado.FALHA_PERMANENTE, "arte_vazia")
        return dados

    def _publicar_video(self, pedido: PedidoDePublicacao, tipo_midia: str) -> ResultadoPublicacao:
        chave = pedido.chave_idempotencia
        andamento = self._andamentos.get(chave)
        if andamento is not None:
            if andamento.resultado is not None:  # já publicado: mesmo sucesso, sem rede
                return andamento.resultado
            if andamento.publish_enviado:  # pode ter saído: NUNCA reenviar
                raise _Encerrar(TipoDeResultado.INCERTO, "publish_ja_enviado")
        problema = self._problema_local(pedido)
        if problema is not None:
            raise _Encerrar(*problema)
        transporte = self._escrita()
        if not self.verificar_conexao().valida:
            raise _Encerrar(TipoDeResultado.FALHA_PERMANENTE, "conexao_invalida")
        restante = self.limite_diario_restante()
        if restante is None or restante <= 0:
            raise _Encerrar(TipoDeResultado.FALHA_TRANSITORIA, "limite_diario")

        andamento = self._andamentos.setdefault(chave, _Andamento())
        try:
            dados = b"" if andamento.enviado else self._ler_video(pedido.artes[0])
            if andamento.container is None:
                self._criar_container(transporte, andamento, pedido, tipo_midia)
            if not andamento.enviado:
                self._enviar_bytes(transporte, andamento, dados)
            self._aguardar_container(transporte, andamento)
        except _Encerrar as parada:
            if parada.tipo is TipoDeResultado.FALHA_PERMANENTE:
                self._andamentos.pop(chave, None)  # container inútil: recomeçar do zero
            raise
        return self._publicar_container(transporte, andamento, chave)

    def _criar_container(
        self,
        transporte: TransporteHttpEscrita,
        andamento: _Andamento,
        pedido: PedidoDePublicacao,
        tipo_midia: str,
    ) -> None:
        campos = {"media_type": tipo_midia, "upload_type": "resumable"}
        if pedido.conteudo.strip():
            campos["caption"] = pedido.conteudo
        corpo = urlencode(campos).encode("utf-8")
        url = f"{self._base}/{self._id_conta}/media"
        cabecalhos = self._cabecalhos_graph(formulario=True)
        try:
            dados = self._etapa(
                "container", lambda: transporte.postar(url, cabecalhos, corpo, self._timeout)
            )
        finally:
            cabecalhos.clear()
        identificador = dados.get("id")
        uri = dados.get("uri")
        if not (isinstance(identificador, str) and _RE_ID_GRAPH.fullmatch(identificador)):
            raise _Encerrar(TipoDeResultado.FALHA_PERMANENTE, "container_fora_do_contrato")
        if not isinstance(uri, str) or not self._uri_permitida(uri):
            raise _Encerrar(TipoDeResultado.FALHA_PERMANENTE, "container_uri_invalida")
        andamento.container = identificador
        andamento.uri = uri

    def _uri_permitida(self, uri: str) -> bool:
        """O token só vai a hosts conhecidos da Meta (nunca a uma URL ditada pela resposta)."""
        try:
            partes = urlsplit(uri)
            return (
                partes.scheme == "https"
                and partes.hostname in self._hosts_upload
                and partes.username is None
                and partes.password is None
                and partes.port is None
            )
        except ValueError:
            return False

    def _enviar_bytes(
        self, transporte: TransporteHttpEscrita, andamento: _Andamento, dados: bytes
    ) -> None:
        uri = andamento.uri
        if uri is None:
            raise _Encerrar(TipoDeResultado.FALHA_PERMANENTE, "estado_invalido")
        cabecalhos = {
            "Authorization": f"OAuth {self._token.revelar()}",
            "offset": "0",
            "file_size": str(len(dados)),
            "Content-Type": "application/octet-stream",
            "User-Agent": "o-publicador",
        }
        try:
            resposta = self._etapa(
                "upload", lambda: transporte.postar(uri, cabecalhos, dados, self._timeout_upload)
            )
        finally:
            cabecalhos.clear()
        if resposta.get("success") is not True:
            raise _Encerrar(TipoDeResultado.FALHA_PERMANENTE, "upload_fora_do_contrato")
        andamento.enviado = True

    def _consultar_status(self, transporte: TransporteHttpEscrita, url: str) -> dict[str, object]:
        cabecalhos = self._cabecalhos_graph()
        try:
            return self._etapa("status", lambda: transporte.obter(url, cabecalhos, self._timeout))
        finally:
            cabecalhos.clear()

    def _aguardar_container(self, transporte: TransporteHttpEscrita, andamento: _Andamento) -> None:
        url = f"{self._base}/{andamento.container}?fields=status_code"
        for tentativa in range(self._max_tentativas_status):
            dados = self._consultar_status(transporte, url)
            situacao = dados.get("status_code")
            if situacao == "FINISHED":
                return
            if situacao == "PUBLISHED":  # publicado sem o nosso media_publish: pode ter saído
                andamento.publish_enviado = True
                raise _Encerrar(TipoDeResultado.INCERTO, "container_ja_publicado")
            if situacao == "ERROR":
                raise _Encerrar(TipoDeResultado.FALHA_PERMANENTE, "container_erro")
            if situacao == "EXPIRED":
                raise _Encerrar(TipoDeResultado.FALHA_PERMANENTE, "container_expirado")
            if situacao != "IN_PROGRESS":
                raise _Encerrar(TipoDeResultado.FALHA_PERMANENTE, "status_fora_do_contrato")
            if tentativa + 1 < self._max_tentativas_status:
                self._dormir(self._intervalo_status)
        raise _Encerrar(TipoDeResultado.FALHA_TRANSITORIA, "sondagem_esgotada")

    def _publicar_container(
        self, transporte: TransporteHttpEscrita, andamento: _Andamento, chave: str
    ) -> ResultadoPublicacao:
        """Passo que publica. Daqui em diante NENHUMA dúvida vira falha: vira INCERTO."""
        corpo = urlencode({"creation_id": andamento.container}).encode("utf-8")
        url = f"{self._base}/{self._id_conta}/media_publish"
        cabecalhos = self._cabecalhos_graph(formulario=True)
        andamento.publish_enviado = True  # marcado ANTES de enviar
        try:
            status, resposta = transporte.postar(url, cabecalhos, corpo, self._timeout)
        except Exception:  # noqa: BLE001 - timeout/queda: pode ter saído
            _log.warning("instagram media_publish: sem resposta; resultado incerto")
            return ResultadoPublicacao(
                TipoDeResultado.INCERTO, chave, motivo="publish_sem_resposta"
            )
        finally:
            cabecalhos.clear()
        try:
            dados = _json_dict(resposta) if 200 <= status < 300 else None
            identificador = dados.get("id") if dados is not None else None
            if (
                dados is not None
                and "error" not in dados
                and isinstance(identificador, str)
                and _RE_ID_GRAPH.fullmatch(identificador)
            ):
                resultado = ResultadoPublicacao(
                    TipoDeResultado.SUCESSO, chave, id_externo=identificador
                )
                andamento.resultado = resultado
                return resultado
        except Exception:  # noqa: BLE001 - qualquer defeito aqui continua sendo dúvida
            pass
        _log.warning("instagram media_publish: status %s sem id válido; resultado incerto", status)
        motivo = "publish_resposta_invalida" if 200 <= status < 300 else f"publish_http_{status}"
        return ResultadoPublicacao(TipoDeResultado.INCERTO, chave, motivo=motivo)

    def consultar(self, chave_idempotencia: str) -> ResultadoConsulta:
        """Sem consulta real até a T047: DESCONHECIDO (nunca autoriza nova tentativa)."""
        return ResultadoConsulta(EstadoConsulta.DESCONHECIDO)
