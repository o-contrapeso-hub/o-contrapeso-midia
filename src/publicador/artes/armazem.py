"""Armazem de artes: leitura (somente leitura) das imagens e videos dos posts.

As artes ficam num repositorio PRIVADO do GitHub, lido com um token SOMENTE
LEITURA. O ``caminho`` de cada arte vem do Notion e, portanto, e dado NAO
confiavel: toda implementacao valida o caminho antes de tocar disco ou rede.

Regras:

* I-1: o token e recebido como ``SegredoStr`` e so e revelado no instante de
  montar o cabecalho HTTP. Nenhuma mensagem de erro ecoa corpo, cabecalhos ou
  URL de resposta; o ``repr`` do armazem nao mostra o token.
* I-2: este modulo apenas LE. Nada aqui publica ou escreve.

Limites conhecidos: a validacao de caminho do ``ArmazemLocal`` nao elimina a
janela entre a checagem e a leitura (TOCTOU) caso alguem troque arquivos da raiz
concorrentemente; a raiz deve ser um checkout controlado. O ``TransporteHttpUrllib``
nao segue redirecionamentos (para nunca reenviar o token a outro host) e le a
resposta inteira em memoria.
"""

from __future__ import annotations

import hashlib
import re
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Final, Protocol
from urllib.parse import quote

from publicador.core.redacao import SegredoStr

__all__ = [
    "ArmazemDeArtes",
    "ArmazemGitHub",
    "ArmazemLocal",
    "ArteNaoEncontrada",
    "CaminhoDeArteInvalido",
    "ErroAcessoArmazem",
    "TransporteHttp",
    "TransporteHttpUrllib",
    "validar_caminho_de_arte",
]

_URL_BASE: Final = "https://api.github.com"
_ACCEPT: Final = "application/vnd.github.raw+json"
_TIMEOUT_PADRAO: Final = 30.0
_BLOCO_HASH: Final = 1024 * 1024
_RE_NOME_GITHUB: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class CaminhoDeArteInvalido(ValueError):
    """Caminho de arte inseguro ou malformado (absoluto, com ``..``, etc.)."""


class ArteNaoEncontrada(FileNotFoundError):
    """A arte pedida nao existe no armazem."""


class ErroAcessoArmazem(Exception):
    """Falha de acesso ao armazem (credencial recusada, erro de rede ou do servico).

    A mensagem e sempre fixa: nunca contem corpo, cabecalhos ou URL.
    """


# --------------------------------------------------------------------------- caminho


def validar_caminho_de_arte(caminho: str) -> tuple[str, ...]:
    """Valida o caminho relativo (estilo POSIX) de uma arte e devolve seus segmentos.

    Recusa: nao-``str``, vazio, bytes nulos, barra invertida, caminho absoluto,
    segmentos vazios (``a//b``, barra final), ``.`` e ``..``.
    """
    if not isinstance(caminho, str):
        raise CaminhoDeArteInvalido("Caminho de arte invalido: deve ser texto.")
    if not caminho:
        raise CaminhoDeArteInvalido("Caminho de arte invalido: vazio.")
    exibicao = repr(caminho)[:200]
    if "\x00" in caminho:
        raise CaminhoDeArteInvalido(f"Caminho de arte invalido (byte nulo): {exibicao}")
    if "\\" in caminho:
        raise CaminhoDeArteInvalido(f"Caminho de arte invalido (barra invertida): {exibicao}")
    if caminho.startswith("/"):
        raise CaminhoDeArteInvalido(f"Caminho de arte invalido (absoluto): {exibicao}")
    segmentos = tuple(caminho.split("/"))
    if any(s in ("", ".", "..") for s in segmentos):
        raise CaminhoDeArteInvalido(
            f"Caminho de arte invalido (segmento vazio, '.' ou '..'): {exibicao}"
        )
    return segmentos


def _mensagem_nao_encontrada(caminho: str) -> str:
    return f"Arte nao encontrada no armazem: {caminho!r}. Confira o caminho no Notion."


# --------------------------------------------------------------------------- interface


class ArmazemDeArtes(Protocol):
    """Leitura de artes por caminho relativo (``<serie>/<AAAA>/<MM>/<arquivo>``)."""

    def ler(self, caminho: str) -> bytes: ...

    def hash_sha256(self, caminho: str) -> str:
        """SHA-256 do CONTEUDO, em hexadecimal minusculo."""
        ...

    def existe(self, caminho: str) -> bool: ...


# --------------------------------------------------------------------------- local


class ArmazemLocal:
    """Armazem por diretorio (testes e checkouts locais)."""

    def __init__(self, raiz: Path) -> None:
        self._raiz = Path(raiz).resolve()

    def __repr__(self) -> str:
        return "ArmazemLocal(<raiz>)"

    def _resolver(self, caminho: str) -> Path:
        segmentos = validar_caminho_de_arte(caminho)
        alvo = self._raiz.joinpath(*segmentos).resolve()
        if not alvo.is_relative_to(self._raiz):
            raise CaminhoDeArteInvalido(
                f"Caminho de arte invalido (escapa da raiz do armazem): {caminho!r}"
            )
        return alvo

    def _arquivo(self, caminho: str) -> Path:
        alvo = self._resolver(caminho)
        if not alvo.is_file():
            raise ArteNaoEncontrada(_mensagem_nao_encontrada(caminho))
        return alvo

    def ler(self, caminho: str) -> bytes:
        alvo = self._arquivo(caminho)
        try:
            return alvo.read_bytes()
        except FileNotFoundError:
            raise ArteNaoEncontrada(_mensagem_nao_encontrada(caminho)) from None

    def hash_sha256(self, caminho: str) -> str:
        alvo = self._arquivo(caminho)
        resumo = hashlib.sha256()
        try:
            with alvo.open("rb") as arquivo:
                while bloco := arquivo.read(_BLOCO_HASH):
                    resumo.update(bloco)
        except FileNotFoundError:
            raise ArteNaoEncontrada(_mensagem_nao_encontrada(caminho)) from None
        return resumo.hexdigest()

    def existe(self, caminho: str) -> bool:
        return self._resolver(caminho).is_file()


# --------------------------------------------------------------------------- transporte


class TransporteHttp(Protocol):
    """Transporte HTTP minimo (GET), injetavel para testes."""

    def obter(
        self, url: str, cabecalhos: Mapping[str, str], timeout: float
    ) -> tuple[int, bytes]: ...


class _SemRedirecionamento(urllib.request.HTTPRedirectHandler):
    """Nao segue redirecionamentos: o token nunca e reenviado a outro host."""

    def redirect_request(
        self, req: object, fp: object, code: int, msg: str, headers: object, newurl: str
    ) -> None:
        return None


class TransporteHttpUrllib:
    """Implementacao de ``TransporteHttp`` com ``urllib`` (apenas HTTPS)."""

    def obter(self, url: str, cabecalhos: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
        if not url.startswith("https://"):
            raise ValueError("Somente URLs https sao permitidas.")
        requisicao = urllib.request.Request(url, headers=dict(cabecalhos), method="GET")  # noqa: S310
        abridor = urllib.request.build_opener(_SemRedirecionamento)
        try:
            with abridor.open(requisicao, timeout=timeout) as resposta:  # noqa: S310
                return int(resposta.status), bytes(resposta.read())
        except urllib.error.HTTPError as erro:
            try:
                return int(erro.code), b""
            finally:
                erro.close()


# --------------------------------------------------------------------------- github


class ArmazemGitHub:
    """Leitura via API de conteudo do GitHub, com token SOMENTE LEITURA."""

    def __init__(
        self,
        dono: str,
        repo: str,
        ref: str,
        token: SegredoStr,
        transporte: TransporteHttp,
        *,
        timeout: float = _TIMEOUT_PADRAO,
    ) -> None:
        if not isinstance(token, SegredoStr):
            raise TypeError("O token deve ser um SegredoStr (regra I-1).")
        for nome, valor in (("dono", dono), ("repo", repo)):
            if _RE_NOME_GITHUB.fullmatch(valor) is None or valor in (".", ".."):
                raise ValueError(f"Valor invalido para '{nome}'.")
        if not ref or "\x00" in ref or "\\" in ref or ref.startswith("/"):
            raise ValueError("Valor invalido para 'ref'.")
        self._dono = dono
        self._repo = repo
        self._ref = ref
        self._token = token
        self._transporte = transporte
        self._timeout = timeout

    def __repr__(self) -> str:
        return f"ArmazemGitHub({self._dono}/{self._repo}@{self._ref})"

    __str__ = __repr__

    def _url(self, caminho: str) -> str:
        segmentos = validar_caminho_de_arte(caminho)
        codificado = "/".join(quote(s, safe="") for s in segmentos)
        ref = quote(self._ref, safe="/")
        return (
            f"{_URL_BASE}/repos/{quote(self._dono, safe='')}/{quote(self._repo, safe='')}"
            f"/contents/{codificado}?ref={ref}"
        )

    def _buscar(self, caminho: str) -> bytes:
        url = self._url(caminho)
        cabecalhos = {
            "Accept": _ACCEPT,
            "Authorization": f"Bearer {self._token.revelar()}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "o-publicador",
        }
        try:
            status, corpo = self._transporte.obter(url, cabecalhos, self._timeout)
        except Exception:
            raise ErroAcessoArmazem(
                "Falha de comunicacao com o armazem de artes (detalhes omitidos)."
            ) from None
        finally:
            cabecalhos.clear()
        if status == 200:
            return corpo
        if status == 404:
            raise ArteNaoEncontrada(_mensagem_nao_encontrada(caminho))
        if status in (401, 403):
            raise ErroAcessoArmazem(
                "Acesso ao armazem de artes recusado (HTTP 401/403): "
                "verifique o token somente leitura e suas permissoes."
            )
        raise ErroAcessoArmazem(f"Armazem de artes respondeu com status inesperado ({status}).")

    def ler(self, caminho: str) -> bytes:
        return self._buscar(caminho)

    def hash_sha256(self, caminho: str) -> str:
        return hashlib.sha256(self._buscar(caminho)).hexdigest()

    def existe(self, caminho: str) -> bool:
        try:
            self._buscar(caminho)
        except ArteNaoEncontrada:
            return False
        return True
