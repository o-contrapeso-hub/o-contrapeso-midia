"""Repositório público de estágio: cópia pública TEMPORÁRIA das imagens do Instagram (T044).

Decisão D1 (opção A): o Instagram só baixa imagens por URL pública. Cada imagem é copiada do
repositório PRIVADO de artes para uma release de um repositório PÚBLICO de estágio, separado
e exclusivo; o Instagram baixa pela URL do asset e, depois da publicação (sucesso, falha ou
dúvida), a release é apagada.

Configuração (somente NOMES; os valores vêm do GitHub Actions):

* ``PUBLICADOR_ESTAGIO_REPO`` (variável): ``dono/repo`` do repositório público de estágio.
* ``PUBLICADOR_ESTAGIO_TOKEN`` (secret): token com escrita SOMENTE nesse repositório.

Regras:

* A-004: o token de escrita é usado só aqui (criar/subir/apagar/listar releases do estágio).
  Nunca vai ao Instagram nem ao armazém de artes, e só vai a URLs montadas por este módulo
  (``api.github.com`` e ``uploads.github.com``), nunca a uma URL ditada por uma resposta.
* A-005: ``limpar_antigas`` apaga cópias com mais de 15 minutos (órfãs de execuções
  canceladas) e só toca em releases com o marcador do publicador; o registro tem só contagens.
* I-1: o token chega como ``SegredoStr`` e só é revelado ao montar o cabeçalho. Exceções têm
  mensagem fixa e nunca encadeiam a causa; logs têm só códigos e contagens.
* I-2: este módulo não decide nada sobre publicação; só copia e apaga.

Marcador (formato fechado): tag e nome da release iguais a
``publicador-tmp-AAAAMMDDTHHMMSSZ-<16 hex>``, com o instante (UTC) da criação e um sufixo
aleatório. O arquivo se chama ``<nome_unico>.jpg``; o chamador deriva ``nome_unico`` de um
hash (nunca de título, corpo ou caminho da arte).

Campos e rotas da API do GitHub escritos de memória (validar contra o repositório real):
``POST /repos/{o}/{r}/releases``, ``POST uploads.github.com/.../releases/{id}/assets?name=``,
``DELETE /releases/assets/{id}``, ``DELETE /releases/{id}``, ``DELETE /git/refs/tags/{tag}``,
``GET /releases?per_page=&page=``; resposta com ``id``, ``tag_name``, ``name``, ``assets`` e
``browser_download_url``.
"""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final, Protocol, runtime_checkable
from urllib.parse import quote, urlsplit

from publicador.artes.armazem import TransporteHttpEscrita
from publicador.core.redacao import SegredoStr, obter_logger

__all__ = [
    "IDADE_MAXIMA",
    "PREFIXO_TAG",
    "CopiaPublica",
    "ErroEstagio",
    "EstagioGitHub",
    "EstagioPublico",
    "ResultadoLimpeza",
    "limpar_copias_antigas",
]

_log = obter_logger("publicador.artes.estagio")

PREFIXO_TAG: Final = "publicador-tmp-"
IDADE_MAXIMA: Final = timedelta(minutes=15)
_API: Final = "https://api.github.com"
_UPLOADS: Final = "https://uploads.github.com"
_DOWNLOAD: Final = "https://github.com"
_RE_TAG: Final = re.compile(r"publicador-tmp-([0-9]{8}T[0-9]{6}Z)-([0-9a-f]{16})")
_RE_SUFIXO: Final = re.compile(r"[0-9a-f]{16}")
_RE_NOME_UNICO: Final = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_RE_NOME_GITHUB: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
_FORMATO_INSTANTE: Final = "%Y%m%dT%H%M%SZ"
_CORPO_RELEASE: Final = (
    "Copia temporaria do O Publicador; apagada automaticamente depois da publicacao."
)
_TIMEOUT: Final = 30.0
_TIMEOUT_UPLOAD: Final = 120.0
_POR_PAGINA: Final = 100
_MAX_PAGINAS: Final = 10
_APAGADO: Final = frozenset({200, 204, 404})
_TAG_APAGADA: Final = frozenset({200, 204, 404, 422})


class ErroEstagio(Exception):
    """Falha no repositório de estágio. Mensagem fixa; ``codigo`` é curto e sem dados."""

    def __init__(self, codigo: str, *, transitorio: bool) -> None:
        super().__init__("Falha no repositorio publico de estagio (detalhes omitidos).")
        self.codigo = codigo
        self.transitorio = transitorio


@dataclass(frozen=True)
class CopiaPublica:
    """Cópia temporária publicada. A URL não aparece no ``repr`` (é pública, mas expõe a arte
    antes da publicação; não deve ir a log)."""

    url: str = field(repr=False)
    tag: str
    id_release: int
    id_asset: int | None = None


@dataclass(frozen=True)
class ResultadoLimpeza:
    """Contagens da limpeza de cópias antigas (A-005); nunca nomes nem conteúdo."""

    apagadas: int
    falhas: int
    listagem_ok: bool


@runtime_checkable
class EstagioPublico(Protocol):
    """Local público temporário para as imagens do Instagram (D1)."""

    def publicar_copia(self, nome_unico: str, dados: bytes) -> CopiaPublica:
        """Publica ``dados`` (JPEG) e devolve a cópia com a URL https. Em falha levanta
        ``ErroEstagio`` sem deixar nada publicado (melhor esforço; o resto, A-005)."""
        ...

    def apagar_copia(self, copia: CopiaPublica) -> bool:
        """Apaga a cópia. Idempotente (já apagada = ok). Nunca levanta ``Exception``."""
        ...

    def limpar_antigas(
        self, agora: datetime, idade_max: timedelta = IDADE_MAXIMA
    ) -> ResultadoLimpeza:
        """Apaga cópias do publicador com mais de ``idade_max`` (A-005)."""
        ...


def _tem_fuso(valor: datetime) -> bool:
    return valor.tzinfo is not None and valor.utcoffset() is not None


def _inteiro_positivo(valor: object) -> int | None:
    if isinstance(valor, int) and not isinstance(valor, bool) and valor > 0:
        return valor
    return None


def _json(corpo: bytes) -> object:
    try:
        return json.loads(corpo)
    except (ValueError, RecursionError):
        return None


def _erro_http(etapa: str, status: int) -> ErroEstagio:
    transitorio = status == 429 or status >= 500
    return ErroEstagio(f"{etapa}_http_{status}", transitorio=transitorio)


def _instante_do_marcador(tag: str) -> datetime | None:
    casamento = _RE_TAG.fullmatch(tag)
    if casamento is None:
        return None
    try:
        return datetime.strptime(casamento.group(1), _FORMATO_INSTANTE).replace(tzinfo=UTC)
    except ValueError:
        return None


def _sufixo_aleatorio() -> str:
    return secrets.token_hex(8)


class EstagioGitHub:
    """``EstagioPublico`` sobre releases de um repositório público do GitHub."""

    def __init__(
        self,
        repositorio: str,
        token: SegredoStr,
        transporte: TransporteHttpEscrita,
        relogio: Callable[[], datetime],
        *,
        timeout: float = _TIMEOUT,
        timeout_upload: float = _TIMEOUT_UPLOAD,
        gerar_sufixo: Callable[[], str] = _sufixo_aleatorio,
        max_paginas: int = _MAX_PAGINAS,
    ) -> None:
        if not isinstance(token, SegredoStr):
            raise TypeError("O token deve ser um SegredoStr (regra I-1).")
        dono, barra, repo = repositorio.partition("/")
        for valor in (dono, repo):
            if _RE_NOME_GITHUB.fullmatch(valor) is None or valor in (".", ".."):
                raise ValueError("Repositorio de estagio invalido: use dono/repo.")
        if not barra or timeout <= 0 or timeout_upload <= 0 or max_paginas < 1:
            raise ValueError("Parametros do repositorio de estagio invalidos.")
        self._dono = dono
        self._repo = repo
        self._token = token
        self._transporte = transporte
        self._relogio = relogio
        self._timeout = timeout
        self._timeout_upload = timeout_upload
        self._gerar_sufixo = gerar_sufixo
        self._max_paginas = max_paginas
        self._api = f"{_API}/repos/{quote(dono, safe='')}/{quote(repo, safe='')}"
        self._uploads = f"{_UPLOADS}/repos/{quote(dono, safe='')}/{quote(repo, safe='')}"

    def __repr__(self) -> str:
        return f"EstagioGitHub({self._dono}/{self._repo})"

    __str__ = __repr__

    # ------------------------------------------------------------------ HTTP interno

    def _cabecalhos(self, tipo: str | None = None) -> dict[str, str]:
        cabecalhos = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token.revelar()}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "o-publicador",
        }
        if tipo is not None:
            cabecalhos["Content-Type"] = tipo
        return cabecalhos

    def _chamar(
        self,
        etapa: str,
        metodo: str,
        url: str,
        corpo: bytes | None = None,
        tipo: str | None = None,
        timeout: float | None = None,
    ) -> tuple[int, bytes]:
        """Uma chamada; falha de comunicação vira ``ErroEstagio`` transitório sem causa."""
        cabecalhos = self._cabecalhos(tipo)
        prazo = self._timeout if timeout is None else timeout
        try:
            if metodo == "GET":
                return self._transporte.obter(url, cabecalhos, prazo)
            if metodo == "DELETE":
                return self._transporte.apagar(url, cabecalhos, prazo)
            return self._transporte.postar(url, cabecalhos, corpo or b"", prazo)
        except Exception:  # noqa: BLE001 - rede/timeout: nunca ecoar a mensagem
            raise ErroEstagio(f"{etapa}_rede", transitorio=True) from None
        finally:
            cabecalhos.clear()

    def _apagar(self, etapa: str, url: str, aceitos: frozenset[int]) -> bool:
        try:
            status, _ = self._chamar(etapa, "DELETE", url)
        except ErroEstagio:
            _log.warning("estagio %s: falha de comunicacao", etapa)
            return False
        if status in aceitos:
            return True
        _log.warning("estagio %s: status %s", etapa, status)
        return False

    def _apagar_release(self, tag: str, id_release: int, id_asset: int | None) -> bool:
        """Tenta TODAS as etapas mesmo se uma falhar. ``True`` se a release sumiu."""
        if id_asset is not None:
            self._apagar("apagar_asset", f"{self._api}/releases/assets/{id_asset}", _APAGADO)
        ok = self._apagar("apagar_release", f"{self._api}/releases/{id_release}", _APAGADO)
        tag_ok = self._apagar(
            "apagar_tag", f"{self._api}/git/refs/tags/{quote(tag, safe='')}", _TAG_APAGADA
        )
        return ok and tag_ok

    # ------------------------------------------------------------------ publicar

    def publicar_copia(self, nome_unico: str, dados: bytes) -> CopiaPublica:
        if not isinstance(nome_unico, str) or _RE_NOME_UNICO.fullmatch(nome_unico) is None:
            raise ValueError("nome_unico invalido: use [a-z0-9-], ate 64 caracteres.")
        if not dados:
            raise ValueError("a copia nao pode ser vazia")
        agora = self._relogio()
        if not _tem_fuso(agora):
            raise ValueError("o relogio deve devolver datetime com fuso")
        sufixo = self._gerar_sufixo()
        if _RE_SUFIXO.fullmatch(sufixo) is None:
            raise ValueError("sufixo aleatorio invalido")
        tag = f"{PREFIXO_TAG}{agora.astimezone(UTC).strftime(_FORMATO_INSTANTE)}-{sufixo}"
        id_release = self._criar_release(tag)
        try:
            return self._subir_asset(tag, id_release, f"{nome_unico}.jpg", dados)
        except BaseException:
            # Falha (ou cancelamento) no envio: não deixar release pública para trás.
            if not self._apagar_release(tag, id_release, None):
                _log.warning("estagio: copia_nao_apagada apos falha no envio (A-005 limpa)")
            raise

    def _criar_release(self, tag: str) -> int:
        corpo = json.dumps(
            {
                "tag_name": tag,
                "name": tag,
                "body": _CORPO_RELEASE,
                "draft": False,  # asset de rascunho não é público
                "prerelease": True,
                "make_latest": "false",
            }
        ).encode("utf-8")
        status, resposta = self._chamar(
            "criar_release", "POST", f"{self._api}/releases", corpo, "application/json"
        )
        if not 200 <= status < 300:
            _log.warning("estagio criar_release: status %s", status)
            raise _erro_http("criar_release", status)
        dados = _json(resposta)
        identificador = _inteiro_positivo(dados.get("id")) if isinstance(dados, dict) else None
        if identificador is None or not isinstance(dados, dict) or dados.get("tag_name") != tag:
            _log.warning("estagio criar_release: resposta fora do contrato")
            # A release pode ter sido criada; a limpeza A-005 a encontra pelo marcador.
            raise ErroEstagio("criar_release_fora_do_contrato", transitorio=False)
        return identificador

    def _subir_asset(self, tag: str, id_release: int, nome: str, dados: bytes) -> CopiaPublica:
        url = f"{self._uploads}/releases/{id_release}/assets?name={quote(nome, safe='')}"
        status, resposta = self._chamar(
            "subir_asset", "POST", url, bytes(dados), "image/jpeg", self._timeout_upload
        )
        if not 200 <= status < 300:
            _log.warning("estagio subir_asset: status %s", status)
            raise _erro_http("subir_asset", status)
        corpo = _json(resposta)
        if not isinstance(corpo, dict):
            raise ErroEstagio("subir_asset_fora_do_contrato", transitorio=False)
        id_asset = _inteiro_positivo(corpo.get("id"))
        endereco = corpo.get("browser_download_url")
        estado = corpo.get("state", "uploaded")
        if (
            id_asset is None
            or estado != "uploaded"
            or not isinstance(endereco, str)
            or not self._url_esperada(endereco, tag, nome)
        ):
            _log.warning("estagio subir_asset: resposta fora do contrato")
            raise ErroEstagio("subir_asset_fora_do_contrato", transitorio=False)
        return CopiaPublica(url=endereco, tag=tag, id_release=id_release, id_asset=id_asset)

    def _url_esperada(self, endereco: str, tag: str, nome: str) -> bool:
        """Só aceita ``https://github.com/<dono>/<repo>/releases/download/<tag>/<nome>``."""
        try:
            partes = urlsplit(endereco)
        except ValueError:
            return False
        if (
            partes.scheme != "https"
            or partes.netloc != "github.com"
            or partes.query
            or partes.fragment
        ):
            return False
        esperado = f"/{self._dono}/{self._repo}/releases/download/{tag}/{nome}".casefold()
        caminho = partes.path.casefold()
        sufixo = f"/releases/download/{tag}/{nome}"
        # dono/repo podem voltar em outra caixa; tag e nome têm de ser exatos.
        return caminho == esperado and partes.path.endswith(sufixo)

    # ------------------------------------------------------------------ apagar

    def apagar_copia(self, copia: CopiaPublica) -> bool:
        if _RE_TAG.fullmatch(copia.tag) is None or _inteiro_positivo(copia.id_release) is None:
            _log.warning("estagio apagar: copia fora do formato do publicador; ignorada")
            return False
        return self._apagar_release(copia.tag, copia.id_release, copia.id_asset)

    # ------------------------------------------------------------------ limpeza (A-005)

    def _listar(self) -> list[dict[str, object]] | None:
        releases: list[dict[str, object]] = []
        for pagina in range(1, self._max_paginas + 1):
            url = f"{self._api}/releases?per_page={_POR_PAGINA}&page={pagina}"
            try:
                status, corpo = self._chamar("listar", "GET", url)
            except ErroEstagio:
                _log.warning("estagio listar: falha de comunicacao")
                return None
            if status != 200:
                _log.warning("estagio listar: status %s", status)
                return None
            itens = _json(corpo)
            if not isinstance(itens, list):
                _log.warning("estagio listar: resposta fora do contrato")
                return None
            releases.extend(i for i in itens if isinstance(i, dict))
            if len(itens) < _POR_PAGINA:
                break
        return releases

    def limpar_antigas(
        self, agora: datetime, idade_max: timedelta = IDADE_MAXIMA
    ) -> ResultadoLimpeza:
        if not _tem_fuso(agora):
            raise ValueError("agora deve ter fuso")
        releases = self._listar()
        if releases is None:
            return ResultadoLimpeza(0, 0, False)
        limite = agora - idade_max
        apagadas = falhas = 0
        for release in releases:
            tag = release.get("tag_name")
            identificador = _inteiro_positivo(release.get("id"))
            if not isinstance(tag, str) or identificador is None or release.get("name") != tag:
                continue
            criada = _instante_do_marcador(tag)
            if criada is None or criada >= limite:  # "mais de" 15 minutos: estrito
                continue
            assets = release.get("assets")
            ids_assets: list[int] = []
            for item in assets if isinstance(assets, list) else []:
                id_asset = _inteiro_positivo(item.get("id")) if isinstance(item, dict) else None
                if id_asset is not None:
                    ids_assets.append(id_asset)
            for id_asset in ids_assets:
                self._apagar("apagar_asset", f"{self._api}/releases/assets/{id_asset}", _APAGADO)
            ok = self._apagar("apagar_release", f"{self._api}/releases/{identificador}", _APAGADO)
            self._apagar(
                "apagar_tag", f"{self._api}/git/refs/tags/{quote(tag, safe='')}", _TAG_APAGADA
            )
            if ok:
                apagadas += 1
            else:
                falhas += 1
        _log.info(
            "estagio: %d cópia(s) temporária(s) antiga(s) apagada(s); %d falha(s)",
            apagadas,
            falhas,
        )
        return ResultadoLimpeza(apagadas, falhas, True)


def limpar_copias_antigas(
    estagio: EstagioPublico, agora: datetime, idade_max: timedelta = IDADE_MAXIMA
) -> ResultadoLimpeza | None:
    """Início de CADA execução (A-005): apaga cópias antigas (a contagem é registrada por
    ``limpar_antigas``, sem nomes).

    Nunca levanta ``Exception`` (a rodada segue; a próxima tenta de novo); devolve ``None``
    se a limpeza não pôde rodar. Cancelamento (``BaseException``) é propagado.
    """
    try:
        resultado = estagio.limpar_antigas(agora, idade_max)
    except Exception as erro:  # noqa: BLE001 - nunca derruba a rodada nem vaza detalhe
        _log.error("estagio: limpeza de copias antigas nao rodou (%s)", type(erro).__name__)
        return None
    if not resultado.listagem_ok:
        _log.warning("estagio: limpeza de copias antigas sem listagem; tenta na proxima rodada")
    return resultado
