"""Cliente minimo da API do Notion (versao FIXADA), com resiliencia e sem vazar segredos.

Regras (I-1/I-2): o token chega como ``SegredoStr`` e o cabecalho ``Authorization``
so e montado no instante do envio; nenhuma excecao, log ou ``repr`` contem o token,
cabecalhos ou o corpo bruto das respostas. Este modulo nao publica nada: apenas le
e escreve no Notion, a fonte da verdade dos posts.

Desvio do plano: o plano citava ``httpx``, que nao e instalavel neste ambiente.
O transporte e uma interface (``Transporte``) e a implementacao padrao,
``TransporteUrllib``, usa apenas ``urllib.request``. ``httpx`` pode substitui-la
depois bastando implementar ``Transporte.enviar`` (sem mudar o ``ClienteNotion``).
"""

from __future__ import annotations

import http.client
import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol
from urllib.parse import quote, urlencode, urlsplit

from publicador.core.redacao import SegredoStr, obter_logger, redigir

__all__ = [
    "ClienteNotion",
    "ErroNotion",
    "ErroNotionIncerto",
    "ErroNotionPermanente",
    "ErroNotionTransitorio",
    "ErroTransporte",
    "RespostaHttp",
    "Transporte",
    "TransporteUrllib",
]

_log = obter_logger("publicador.notion.cliente")

URL_BASE: Final = "https://api.notion.com/v1"
VERSAO_API_PADRAO: Final = "2022-06-28"
TIMEOUT_PADRAO: Final = 30.0
MAX_TENTATIVAS: Final = 5
ESPERA_BASE: Final = 1.0
ESPERA_TETO: Final = 8.0
RETRY_AFTER_TETO: Final = 60.0
INTERVALO_MINIMO: Final = 0.34  # ~3 requisicoes por segundo
TAMANHO_PAGINA: Final = 100
MAX_PAGINAS: Final = 1000
_TAMANHO_MAX_CORPO: Final = 20 * 1024 * 1024

_METODOS: Final = frozenset({"GET", "POST", "PATCH", "DELETE"})
_RE_CAMINHO: Final = re.compile(r"/[A-Za-z0-9_\-]+(?:/[A-Za-z0-9_\-]+)*")
_RE_ID: Final = re.compile(
    r"[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_RE_CODIGO: Final = re.compile(r"[A-Za-z0-9_]{1,40}")


# --------------------------------------------------------------------------- transporte


@dataclass(frozen=True)
class RespostaHttp:
    status: int
    cabecalhos: Mapping[str, str]
    corpo: bytes


class ErroTransporte(Exception):
    """Falha de rede/timeout. A mensagem e fixa: sem URL com query nem cabecalhos."""


class Transporte(Protocol):
    def enviar(
        self,
        metodo: str,
        url: str,
        cabecalhos: Mapping[str, str],
        corpo: bytes | None,
        timeout: float,
    ) -> RespostaHttp: ...


class _SemRedirecionamento(urllib.request.HTTPRedirectHandler):
    """Nao segue redirecionamentos: evita reenviar ``Authorization`` a outro host."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class TransporteUrllib:
    """Transporte real baseado em ``urllib.request`` (somente https, sem redirecionar)."""

    def __init__(self) -> None:
        self._abridor = urllib.request.build_opener(_SemRedirecionamento)

    def enviar(
        self,
        metodo: str,
        url: str,
        cabecalhos: Mapping[str, str],
        corpo: bytes | None,
        timeout: float,
    ) -> RespostaHttp:
        if urlsplit(url).scheme != "https":
            raise ErroTransporte("Falha de transporte: esquema de URL nao permitido")
        pedido = urllib.request.Request(  # noqa: S310 - esquema validado acima
            url, data=corpo, headers=dict(cabecalhos), method=metodo
        )
        try:
            with self._abridor.open(pedido, timeout=timeout) as resposta:
                return RespostaHttp(
                    resposta.status,
                    dict(resposta.headers.items()),
                    resposta.read(_TAMANHO_MAX_CORPO),
                )
        except urllib.error.HTTPError as erro:  # 4xx/5xx/3xx: ainda e uma resposta
            try:
                dados = erro.read(_TAMANHO_MAX_CORPO)
                cab = dict(erro.headers.items()) if erro.headers is not None else {}
                return RespostaHttp(erro.code, cab, dados)
            except (OSError, http.client.HTTPException):
                raise ErroTransporte("Falha de transporte ao ler a resposta") from None
            finally:
                erro.close()
        except TimeoutError:
            raise ErroTransporte("Falha de transporte: tempo esgotado") from None
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError):
            raise ErroTransporte("Falha de transporte: conexao") from None


# --------------------------------------------------------------------------- erros


class ErroNotion(Exception):
    """Erro tratavel do Notion. Nunca carrega corpo bruto nem cabecalhos."""

    def __init__(self, mensagem: str, *, status: int | None = None, codigo: str | None = None):
        super().__init__(mensagem)
        self.status = status
        self.codigo = codigo


class ErroNotionTransitorio(ErroNotion):
    """Esgotou as tentativas por 429, 5xx ou falha de transporte."""


class ErroNotionIncerto(ErroNotionTransitorio):
    """Operacao NAO idempotente falhou (5xx/transporte) e nao foi repetida.

    O Notion pode ter processado a requisicao; repetir poderia duplicar paginas ou
    blocos. A mensagem e fixa: sem corpo, cabecalhos nem token.
    """


class ErroNotionPermanente(ErroNotion):
    """Erro que repetir nao resolve (4xx, resposta inesperada)."""


def _mensagem(status: int | None) -> str:
    if status is None:
        return "Falha ao consultar o Notion (erro de transporte)"
    return f"Falha ao consultar o Notion (status {status})"


MENSAGEM_INCERTO: Final = (
    "Resultado incerto: a requisicao nao foi repetida porque o Notion pode ja ter "
    "processado a operacao. Confira no Notion antes de tentar de novo."
)


def _extrair_codigo(corpo: bytes, token: str) -> str | None:
    """Le o campo ``code`` do JSON de erro, so se for curto, alfanumerico e sem segredo."""
    try:
        dados = json.loads(corpo)
    except (ValueError, RecursionError):
        return None
    if not isinstance(dados, dict):
        return None
    codigo = dados.get("code")
    if not isinstance(codigo, str) or _RE_CODIGO.fullmatch(codigo) is None:
        return None
    if token in codigo or redigir(codigo) != codigo:  # eco do token
        return None
    return codigo


def _cabecalho(cabecalhos: Mapping[str, str], nome: str) -> str | None:
    for chave, valor in cabecalhos.items():
        if chave.lower() == nome:
            return valor
    return None


def _validar_id(valor: str) -> str:
    if not isinstance(valor, str) or _RE_ID.fullmatch(valor) is None:
        raise ValueError("Identificador do Notion invalido (esperado hex/UUID)")
    return valor


# --------------------------------------------------------------------------- cliente


class ClienteNotion:
    """Cliente sincrono da API do Notion com paginacao, retentativas e limite de taxa."""

    def __init__(
        self,
        token: SegredoStr,
        transporte: Transporte | None = None,
        dormir: Callable[[float], None] = time.sleep,
        relogio: Callable[[], float] = time.monotonic,
        versao_api: str = VERSAO_API_PADRAO,
    ) -> None:
        # Nome com mangling e SegredoStr: repr/str do valor sao "***".
        self.__token = token if isinstance(token, SegredoStr) else SegredoStr(token)
        self._transporte: Transporte = transporte if transporte is not None else TransporteUrllib()
        self._dormir = dormir
        self._relogio = relogio
        self._versao_api = versao_api
        self._ultimo_envio: float | None = None

    def __repr__(self) -> str:
        return f"ClienteNotion(versao_api={self._versao_api!r})"

    __str__ = __repr__

    # ------------------------------------------------------------------ nucleo

    def _respeitar_taxa(self) -> None:
        if self._ultimo_envio is not None:
            decorrido = self._relogio() - self._ultimo_envio
            if decorrido < INTERVALO_MINIMO:
                self._dormir(INTERVALO_MINIMO - decorrido)
        self._ultimo_envio = self._relogio()

    def _cabecalhos(self, com_corpo: bool) -> dict[str, str]:
        cabecalhos = {
            "Authorization": "Bearer " + self.__token.revelar(),  # so no envio
            "Notion-Version": self._versao_api,
            "Accept": "application/json",
        }
        if com_corpo:
            cabecalhos["Content-Type"] = "application/json"
        return cabecalhos

    def requisitar(
        self,
        metodo: str,
        caminho: str,
        corpo: dict[str, Any] | None = None,
        consulta: Mapping[str, str] | None = None,
        idempotente: bool = True,
    ) -> dict[str, Any]:
        """Executa uma requisicao e devolve o JSON (objeto) da resposta.

        Com ``idempotente=False`` (criar pagina, anexar blocos), 5xx ou falha de
        transporte NAO sao repetidos (o servidor pode ter processado a tentativa):
        levanta ``ErroNotionIncerto``. Em 429 a requisicao nao foi processada, entao
        a repeticao e segura para qualquer operacao.
        """
        metodo = metodo.upper()
        if metodo not in _METODOS:
            raise ValueError("Metodo HTTP nao permitido")
        if _RE_CAMINHO.fullmatch(caminho) is None:
            raise ValueError("Caminho da API do Notion invalido")
        url = URL_BASE + caminho
        if consulta:
            url += "?" + urlencode(consulta, quote_via=quote)
        dados = None if corpo is None else json.dumps(corpo).encode("utf-8")

        ultimo_status: int | None = None
        ultimo_codigo: str | None = None
        for tentativa in range(1, MAX_TENTATIVAS + 1):
            self._respeitar_taxa()
            espera = min(ESPERA_BASE * 2 ** (tentativa - 1), ESPERA_TETO)
            falha_de_transporte = False
            try:
                resposta = self._transporte.enviar(
                    metodo, url, self._cabecalhos(dados is not None), dados, TIMEOUT_PADRAO
                )
            except ErroTransporte:
                ultimo_status, ultimo_codigo = None, None
                motivo = "transporte"
                falha_de_transporte = True
            else:
                falha_de_transporte = False
                if 200 <= resposta.status < 300:
                    return self._decodificar(resposta)
                ultimo_status = resposta.status
                ultimo_codigo = _extrair_codigo(resposta.corpo, self.__token.revelar())
                if resposta.status == 429:
                    motivo = "429"
                    bruto = _cabecalho(resposta.cabecalhos, "retry-after")
                    try:
                        if bruto is not None:
                            espera = min(max(float(bruto.strip()), 0.0), RETRY_AFTER_TETO)
                    except ValueError:
                        pass
                elif resposta.status >= 500:
                    if not idempotente:
                        _log.warning(
                            "Notion: status %s em operacao nao idempotente", resposta.status
                        )
                        raise ErroNotionIncerto(
                            MENSAGEM_INCERTO, status=resposta.status, codigo=ultimo_codigo
                        )
                    motivo = "5xx"
                else:
                    raise ErroNotionPermanente(
                        _mensagem(resposta.status), status=resposta.status, codigo=ultimo_codigo
                    )
            if falha_de_transporte and not idempotente:
                # Fora do except: o erro novo nao fica encadeado ao ErroTransporte.
                _log.warning("Notion: falha de transporte em operacao nao idempotente")
                raise ErroNotionIncerto(MENSAGEM_INCERTO)
            if tentativa == MAX_TENTATIVAS:
                break
            _log.warning(
                "Notion indisponivel (%s, status %s); tentativa %d/%d, aguardando %.2fs",
                motivo,
                ultimo_status,
                tentativa,
                MAX_TENTATIVAS,
                espera,
            )
            self._dormir(espera)
        raise ErroNotionTransitorio(
            _mensagem(ultimo_status), status=ultimo_status, codigo=ultimo_codigo
        )

    @staticmethod
    def _decodificar(resposta: RespostaHttp) -> dict[str, Any]:
        try:
            dados = json.loads(resposta.corpo)
        except (ValueError, RecursionError):
            dados = None
        if not isinstance(dados, dict):
            raise ErroNotionPermanente(
                "Resposta do Notion em formato inesperado", status=resposta.status
            )
        return dados

    # ------------------------------------------------------------------ operacoes

    def _paginas(
        self,
        metodo: str,
        caminho: str,
        corpo_base: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        resultados: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(MAX_PAGINAS):
            if metodo == "POST":
                corpo: dict[str, Any] = {**(corpo_base or {}), "page_size": TAMANHO_PAGINA}
                if cursor is not None:
                    corpo["start_cursor"] = cursor
                dados = self.requisitar("POST", caminho, corpo)
            else:
                consulta = {"page_size": str(TAMANHO_PAGINA)}
                if cursor is not None:
                    consulta["start_cursor"] = cursor
                dados = self.requisitar("GET", caminho, None, consulta)
            itens = dados.get("results")
            if not isinstance(itens, list):
                raise ErroNotionPermanente("Resposta do Notion em formato inesperado")
            resultados.extend(i for i in itens if isinstance(i, dict))
            if not dados.get("has_more"):
                return resultados
            proximo = dados.get("next_cursor")
            if not isinstance(proximo, str) or not proximo or proximo == cursor:
                raise ErroNotionPermanente("Paginacao do Notion inconsistente")
            cursor = proximo
        raise ErroNotionPermanente("Paginacao do Notion excedeu o limite de paginas")

    def consultar_banco(
        self, banco_id: str, filtro: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Consulta um banco (POST /databases/{id}/query) percorrendo todas as paginas."""
        base = None if filtro is None else {"filter": filtro}
        return self._paginas("POST", f"/databases/{_validar_id(banco_id)}/query", base)

    def obter_pagina(self, pagina_id: str) -> dict[str, Any]:
        return self.requisitar("GET", f"/pages/{_validar_id(pagina_id)}")

    def atualizar_pagina(self, pagina_id: str, propriedades: dict[str, Any]) -> dict[str, Any]:
        return self.requisitar(
            "PATCH", f"/pages/{_validar_id(pagina_id)}", {"properties": propriedades}
        )

    def criar_pagina(self, parent: dict[str, Any], propriedades: dict[str, Any]) -> dict[str, Any]:
        return self.requisitar(
            "POST",
            "/pages",
            {"parent": parent, "properties": propriedades},
            idempotente=False,
        )

    def blocos_filhos(self, bloco_id: str) -> list[dict[str, Any]]:
        """Lista os blocos filhos de uma pagina/bloco, com paginacao."""
        return self._paginas("GET", f"/blocks/{_validar_id(bloco_id)}/children", None)
