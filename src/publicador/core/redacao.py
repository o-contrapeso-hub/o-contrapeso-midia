"""Redacao de credenciais em textos, estruturas, logs e tracebacks (regra I-1).

Os logs das execucoes do GitHub Actions sao PUBLICOS. Nenhuma credencial pode
aparecer em saida alguma. Este modulo oferece duas camadas complementares:

1. **Segredos registrados** (``registrar_segredo`` / ``SegredoStr``): valores
   literais conhecidos (lidos do ambiente) sao removidos de qualquer texto, em
   qualquer posicao, junto com variantes comuns (URL-encoded, base64, escapes
   JSON/repr e cada linha de valores multilinha). Ficam apenas em memoria.
2. **Padroes**: ``Bearer <x>``, cabecalhos ``Authorization:``/``Cookie:``,
   pares ``chave=valor``/``chave: valor``/``"chave": "valor"`` com nomes
   sensiveis e credenciais embutidas em URL (``https://user:pass@host``).

Estrategia para tornar a redacao OBRIGATORIA em todo log do processo
(``configurar_logging``):

* ``logging.Logger.callHandlers`` e ``logging.Handler.handle`` sao envolvidos
  (no nivel da CLASSE) para redigir o ``LogRecord`` antes de qualquer handler o
  ver. Assim, handlers adicionados DEPOIS da configuracao, em qualquer logger
  (inclusive de bibliotecas), e chamadas diretas a ``handler.handle`` (ex.:
  ``QueueListener``) tambem passam pela redacao - nao depende de lembrar de
  instalar filtro.
* ``FiltroRedacao`` e instalado no logger raiz e em todos os handlers dele.
* ``FormatadorSeguro`` e a segunda barreira: redige o texto FINAL ja
  formatado (incluindo traceback) dos handlers configurados aqui.
* ``Handler.handleError``, ``sys.excepthook``, ``threading.excepthook`` e
  ``sys.unraisablehook`` passam a escrever tracebacks redigidos.

Decisao sobre redacao excessiva: nomes sensiveis ("token", "senha", "key"...)
so disparam redacao quando funcionam como CHAVE (seguidos de ``=``, ``:`` ou
``%3D``, ou entre aspas como chave de dict/JSON). A palavra solta em frase
normal ("o token expirou") fica intacta. "key" so conta como nome sensivel
sozinho ou apos separador/prefixo conhecido (``api_key``, ``x-api-key``,
``accessKey``), para nao pegar "monkey=" ou "keyboard=". Em caso de duvida,
prefere-se redigir demais a vazar (ex.: "Bearer <palavra>" sempre e redigido).

Limites conhecidos (o que a redacao NAO consegue pegar):

* segredos transformados de forma nao prevista: hash, criptografia, base64 de
  um texto MAIOR que contem o segredo (ex.: ``base64("user:" + segredo)`` fora
  de um cabecalho ``Authorization``), compressao, inversao, hex, etc.;
* segredos partidos em pedaços (ex.: impressos caractere a caractere, ou
  divididos entre duas mensagens/linhas de log);
* segredos NAO registrados que aparecem sem um nome de chave reconhecivel ao
  lado (ex.: ``Credencial('abc123')``) - por isso todo token deve ser lido via
  ``SegredoStr`` ou ``registrar_segredo``;
* valores registrados com menos de 6 caracteres (ignorados de proposito);
* saida que nao passa por ``redigir``/logging: ``print``, ``sys.stdout.write``,
  subprocessos, arquivos gravados diretamente, e o proprio runner do GitHub
  (use tambem ``::add-mask::`` e secrets do Actions);
* concatenacao com ``SegredoStr`` (``"x" + s``, ``json.dumps(s)``) devolve o
  valor real - so e redigido depois porque o valor foi registrado;
* valores com espacos sem aspas em ``chave=valor`` (so o primeiro trecho e
  coberto por padrao; registre o segredo para cobertura total).
"""

from __future__ import annotations

import base64
import json
import logging
import re
import sys
import threading
import traceback
from collections.abc import Callable
from types import TracebackType
from typing import Any, Final
from urllib.parse import quote, quote_plus

__all__ = [
    "MASCARA",
    "FiltroRedacao",
    "FormatadorSeguro",
    "SegredoStr",
    "configurar_logging",
    "instalar_excepthook_seguro",
    "limpar_segredos_registrados",
    "obter_logger",
    "redigir",
    "redigir_estrutura",
    "registrar_segredo",
]

MASCARA: Final = "***"
TAMANHO_MINIMO: Final = 6
NAMESPACE: Final = "publicador"
FORMATO_PADRAO: Final = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_PROFUNDIDADE_MAXIMA: Final = 64
_PASSADAS_MAXIMAS: Final = 5

# --------------------------------------------------------------------------- registro

_trava = threading.RLock()
_segredos: set[str] = set()
_variantes: set[str] = set()
_padrao_segredos: re.Pattern[str] | None = None
_HEX_PERCENT = re.compile(r"%[0-9A-F]{2}")


def _gerar_variantes(valor: str) -> set[str]:
    bases = {valor, valor.strip()}
    for linha in valor.splitlines():
        bases.add(linha.strip())
    bases = {b for b in bases if len(b) >= TAMANHO_MINIMO}
    variantes: set[str] = set()
    for base in bases:
        crus = base.encode("utf-8", "surrogatepass")
        candidatos = {
            base,
            quote(base, safe=""),
            quote(base),
            quote_plus(base),
            base64.b64encode(crus).decode("ascii"),
            base64.urlsafe_b64encode(crus).decode("ascii"),
            json.dumps(base)[1:-1],
            json.dumps(base, ensure_ascii=False)[1:-1],
            repr(base)[1:-1],
        }
        for candidato in list(candidatos):
            candidatos.add(candidato.rstrip("="))
            candidatos.add(_HEX_PERCENT.sub(lambda m: m.group(0).lower(), candidato))
        variantes |= candidatos
    return {v for v in variantes if len(v) >= TAMANHO_MINIMO}


def _recompilar() -> None:
    global _padrao_segredos
    if not _variantes:
        _padrao_segredos = None
        return
    ordenadas = sorted(_variantes, key=lambda v: (-len(v), v))  # mais longo primeiro
    _padrao_segredos = re.compile("|".join(re.escape(v) for v in ordenadas))


def registrar_segredo(valor: str) -> None:
    """Registra um valor literal (>= 6 caracteres) para ser removido de qualquer texto.

    Valores curtos, vazios ou que nao sao ``str`` sao ignorados. O registro vive
    apenas em memoria (nunca e gravado).
    """
    try:
        if not isinstance(valor, str):
            return
        valor = str.__str__(valor)  # SegredoStr -> valor real
        if len(valor.strip()) < TAMANHO_MINIMO:
            return
        with _trava:
            if valor in _segredos:
                return
            _segredos.add(valor)
            _variantes.update(_gerar_variantes(valor))
            _recompilar()
    except Exception:  # nunca propagar: registrar nao pode derrubar o job
        return


def limpar_segredos_registrados() -> None:
    """Esquece todos os segredos registrados (uso em testes)."""
    global _padrao_segredos
    with _trava:
        _segredos.clear()
        _variantes.clear()
        _padrao_segredos = None


# --------------------------------------------------------------------------- padroes

_SEP = r"[_.\-]"
_NOME_LIVRE = (
    r"token|secret|password|passwd|senha|authorization|cookies?|signature|credentials?"
    rf"|api{_SEP}?key"
)
# quantificadores LIMITADOS ({0,64}) evitam backtracking quadratico (ReDoS)
_PREFIXO = r"[A-Za-z0-9_.\-]{0,64}"
_NOME_CURTO = (  # nomes curtos/ambiguos: so sozinhos, apos separador ou prefixo conhecido
    rf"(?:{_PREFIXO}{_SEP}"
    rf"|{_PREFIXO}?(?:api|access|private|client|secret|signing|app))?(?:key|sig|pwd|pass|auth)"
)
_NOME = rf"(?:{_PREFIXO}?(?:{_NOME_LIVRE})|{_NOME_CURTO})"
_ESQUEMA = r"(?:(?:bearer|basic|token|digest)[ \t]+)?"
_VALOR_ASPAS = (
    r"b?\\+\".*?\\+\""  # "...\"valor\"..." (JSON dentro de string)
    r"|b?\\+'.*?\\+'"
    r"|b?\"(?:[^\"\\\n]|\\.)*\""
    r"|b?'(?:[^'\\\n]|\\.)*'"
)
_VALOR_NU = rf"{_ESQUEMA}[^\s&,;'\"<>)\]}}]+"
_VALOR = rf"(?:{_VALOR_ASPAS}|{_VALOR_NU})"

_RE_NOME_SENSIVEL = re.compile(rf"{_NOME}", re.IGNORECASE)
_RE_URL_CREDENCIAL = re.compile(
    r"(?<![a-z0-9+.\-])([a-z][a-z0-9+.\-]{0,31}://)[^\s/@?#'\"<>]{1,1024}@", re.IGNORECASE
)
_RE_BEARER = re.compile(r"\b(bearer)[ \t]+[A-Za-z0-9\-._~+/=]+", re.IGNORECASE)
_RE_CABECALHO = re.compile(
    r"(?<![A-Za-z0-9_\-])((?:proxy-)?authorization|set-cookie|cookie|x-api-key|x-auth-token"
    r"|api-key)([ \t]*:[ \t]*)[^\r\n]+",
    re.IGNORECASE,
)
_RE_CHAVE_ASPAS = re.compile(
    rf"(\\{{0,8}}[\"'])({_NOME})\1([ \t]*[:=][ \t]*)({_VALOR})",
    re.IGNORECASE,
)
_RE_CHAVE_VALOR = re.compile(
    rf"(?<![A-Za-z0-9_.\-])({_NOME})(?![A-Za-z0-9_])([ \t]*(?:=|%3D|:(?!//))[ \t]*)({_VALOR})",
    re.IGNORECASE,
)
_RE_ABRE_ASPAS = re.compile(r"^(b?)(\\*)([\"'])")


def _mascarar_valor(valor: str) -> str:
    aspas = _RE_ABRE_ASPAS.match(valor)
    if aspas is None:
        return MASCARA
    prefixo, barras, aspa = aspas.groups()
    return f"{prefixo}{barras}{aspa}{MASCARA}{barras}{aspa}"


def _sub_chave_aspas(m: re.Match[str]) -> str:
    aspa = m.group(1)
    return f"{aspa}{m.group(2)}{aspa}{m.group(3)}{_mascarar_valor(m.group(4))}"


def _sub_chave_valor(m: re.Match[str]) -> str:
    return f"{m.group(1)}{m.group(2)}{_mascarar_valor(m.group(3))}"


_PADROES: Final[tuple[tuple[re.Pattern[str], str | Callable[[re.Match[str]], str]], ...]] = (
    (_RE_URL_CREDENCIAL, rf"\1{MASCARA}@"),
    (_RE_BEARER, rf"\1 {MASCARA}"),
    (_RE_CABECALHO, rf"\1\2{MASCARA}"),
    (_RE_CHAVE_ASPAS, _sub_chave_aspas),
    (_RE_CHAVE_VALOR, _sub_chave_valor),
)


def _eh_nome_sensivel(nome: str) -> bool:
    return _RE_NOME_SENSIVEL.fullmatch(nome.strip()) is not None


# --------------------------------------------------------------------------- redigir


def _para_texto(valor: object) -> str:
    if type(valor) is str:
        return valor
    if isinstance(valor, bytes | bytearray | memoryview):
        return bytes(valor).decode("utf-8", "backslashreplace")
    try:
        return str(valor)  # SegredoStr.__str__ ja devolve a mascara
    except Exception:
        try:
            return repr(valor)
        except Exception:
            return MASCARA


def _redigir_texto(texto: str) -> str:
    with _trava:
        padrao = _padrao_segredos
    if padrao is not None:
        for _ in range(_PASSADAS_MAXIMAS):
            novo = padrao.sub(MASCARA, texto)
            if novo == texto:
                break
            texto = novo
    for regex, substituto in _PADROES:
        texto = regex.sub(substituto, texto)
    if padrao is not None and padrao.search(texto) is not None:
        texto = padrao.sub(MASCARA, texto)
        if padrao.search(texto) is not None:  # barreira final: na duvida, nada sai
            return MASCARA
    return texto


def redigir(texto: object) -> str:
    """Converte ``texto`` para ``str`` e troca credenciais por ``***``. Nunca lanca."""
    try:
        return _redigir_texto(_para_texto(texto))
    except Exception:
        return MASCARA


def redigir_estrutura(obj: object) -> object:
    """Copia ``obj`` (dict/list/tuple/set aninhados) com credenciais trocadas por ``***``.

    Valores de chaves com nome sensivel viram ``***``; strings passam por
    ``redigir``; numeros, booleanos e ``None`` ficam iguais; outros objetos viram
    o texto redigido de ``str(obj)``. O original nao e alterado.
    """
    try:
        return _estrutura(obj, frozenset(), 0)
    except Exception:
        return MASCARA


def _estrutura(obj: object, vistos: frozenset[int], profundidade: int) -> object:
    if obj is None or isinstance(obj, bool | int | float | complex):
        return obj
    if isinstance(obj, str | bytes | bytearray):
        return redigir(obj)
    if profundidade >= _PROFUNDIDADE_MAXIMA:
        return MASCARA
    if isinstance(obj, dict | list | tuple | set | frozenset):
        if id(obj) in vistos:
            return "<ciclo>"
        vistos = vistos | {id(obj)}
        prox = profundidade + 1
        if isinstance(obj, dict):
            novo: dict[object, object] = {}
            for chave, valor in obj.items():
                sensivel = isinstance(chave, str) and _eh_nome_sensivel(chave)
                chave_nova = _estrutura(chave, vistos, prox)
                try:
                    hash(chave_nova)
                except TypeError:
                    chave_nova = redigir(chave_nova)
                novo[chave_nova] = MASCARA if sensivel else _estrutura(valor, vistos, prox)
            return novo
        itens = [_estrutura(item, vistos, prox) for item in obj]
        if isinstance(obj, list):
            return itens
        if isinstance(obj, tuple):
            return tuple(itens)
        hashaveis = [i if _hashavel(i) else redigir(i) for i in itens]
        return frozenset(hashaveis) if isinstance(obj, frozenset) else set(hashaveis)
    return redigir(obj)


def _hashavel(obj: object) -> bool:
    try:
        hash(obj)
    except TypeError:
        return False
    return True


# --------------------------------------------------------------------------- SegredoStr


class SegredoStr(str):
    """``str`` que nunca se mostra: ``repr``/``str``/``format`` devolvem ``***``.

    Use para guardar tokens lidos do ambiente. ``revelar()`` devolve o valor real
    (somente no ponto de uso, ex.: montar o cabecalho HTTP). Ao ser criado, o
    valor e registrado com ``registrar_segredo``.
    """

    __slots__ = ()

    def __new__(cls, valor: str) -> SegredoStr:
        obj = super().__new__(cls, valor)
        registrar_segredo(str.__str__(obj))
        return obj

    def __repr__(self) -> str:
        return MASCARA

    def __str__(self) -> str:
        return MASCARA

    def __format__(self, format_spec: str) -> str:
        return MASCARA

    def revelar(self) -> str:
        """Devolve o valor real como ``str`` comum. Nunca logue o resultado."""
        return str.__str__(self)

    def __copy__(self) -> SegredoStr:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> SegredoStr:
        return self

    def __reduce_ex__(self, protocolo: Any) -> Any:
        raise TypeError("SegredoStr nao pode ser serializado (regra I-1)")


# --------------------------------------------------------------------------- logging

_ATRIBUTOS_PADRAO: Final = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


def _formatar_excecao(info: Any) -> str:
    if isinstance(info, BaseException):
        info = (type(info), info, info.__traceback__)
    tipo, valor, tb = info
    # format_exception segue __cause__, __context__, notas e ExceptionGroup
    return "".join(traceback.format_exception(tipo, valor, tb)).rstrip("\n")


def _redigir_registro(registro: logging.LogRecord) -> None:
    """Redige o LogRecord IN PLACE (mensagem+args, excecao, pilha e extras)."""
    try:
        try:
            mensagem = registro.getMessage()
        except Exception:
            mensagem = f"{redigir(registro.msg)} {redigir(registro.args)}"
        registro.msg = redigir(mensagem)
        registro.args = None  # args consolidados na mensagem ja redigida
        if registro.exc_info:
            registro.exc_text = redigir(_formatar_excecao(registro.exc_info))
            registro.exc_info = None
        elif registro.exc_text:
            registro.exc_text = redigir(registro.exc_text)
        if registro.stack_info:
            registro.stack_info = redigir(registro.stack_info)
        atributos = registro.__dict__
        if "message" in atributos:
            atributos["message"] = redigir(atributos["message"])
        for chave in [c for c in atributos if c not in _ATRIBUTOS_PADRAO]:
            valor = atributos[chave]
            atributos[chave] = MASCARA if _eh_nome_sensivel(chave) else redigir_estrutura(valor)
    except Exception:
        registro.msg = f"{MASCARA} (falha ao redigir registro de log)"
        registro.args = None
        registro.exc_info = None
        registro.exc_text = None
        registro.stack_info = None


class FiltroRedacao(logging.Filter):
    """Filtro que redige o registro inteiro. Sempre devolve ``True``."""

    def filter(self, record: logging.LogRecord) -> bool:
        _redigir_registro(record)
        return True


class FormatadorSeguro(logging.Formatter):
    """Segunda barreira: redige o texto FINAL formatado (mensagem + traceback).

    ``base`` permite envolver um formatador existente preservando o formato dele.
    """

    def __init__(
        self,
        fmt: str | None = None,
        datefmt: str | None = None,
        *,
        base: logging.Formatter | None = None,
    ) -> None:
        super().__init__(fmt, datefmt)
        self._base = base

    def format(self, record: logging.LogRecord) -> str:
        try:
            texto = self._base.format(record) if self._base is not None else super().format(record)
            return redigir(texto)
        except Exception:
            return MASCARA

    def formatException(self, ei: Any) -> str:  # noqa: N802 - API do logging
        return redigir(super().formatException(ei))

    def formatStack(self, stack_info: str) -> str:  # noqa: N802 - API do logging
        return redigir(super().formatStack(stack_info))


_MARCA: Final = "_publicador_redacao"
_configurado = False


def _marcado(funcao: object) -> bool:
    return bool(getattr(funcao, _MARCA, False))


def _instalar_ganchos_globais() -> None:
    """Envolve, na classe, os pontos por onde TODO registro passa antes de sair."""
    with _trava:
        call_original = logging.Logger.callHandlers
        if not _marcado(call_original):

            def call_handlers_seguro(self: logging.Logger, record: logging.LogRecord) -> None:
                _redigir_registro(record)
                call_original(self, record)

            setattr(call_handlers_seguro, _MARCA, True)
            setattr(logging.Logger, "callHandlers", call_handlers_seguro)  # noqa: B010 - evita method-assign no mypy

        handle_original = logging.Handler.handle
        if not _marcado(handle_original):

            def handle_seguro(self: logging.Handler, record: logging.LogRecord) -> object:
                _redigir_registro(record)
                return handle_original(self, record)

            setattr(handle_seguro, _MARCA, True)
            setattr(logging.Handler, "handle", handle_seguro)  # noqa: B010 - evita method-assign no mypy

        if not _marcado(logging.Handler.handleError):

            def handle_error_seguro(self: logging.Handler, record: logging.LogRecord) -> None:
                if not logging.raiseExceptions or sys.stderr is None:
                    return
                try:
                    texto = (
                        "--- Erro de logging ---\n"
                        + "".join(traceback.format_exception(*sys.exc_info()))
                        + f"Mensagem: {redigir(record.msg)}\n"
                    )
                    sys.stderr.write(redigir(texto))
                except Exception:
                    pass

            setattr(handle_error_seguro, _MARCA, True)
            setattr(logging.Handler, "handleError", handle_error_seguro)  # noqa: B010 - evita method-assign no mypy


def _proteger_handler(handler: logging.Handler) -> None:
    if not any(isinstance(f, FiltroRedacao) for f in handler.filters):
        handler.addFilter(FiltroRedacao())
    atual = handler.formatter
    if not isinstance(atual, FormatadorSeguro):
        if atual is None:
            handler.setFormatter(FormatadorSeguro(FORMATO_PADRAO))
        else:
            handler.setFormatter(FormatadorSeguro(base=atual))


def configurar_logging(nivel: int = logging.INFO) -> None:
    """Configura o logging do processo com redacao obrigatoria (idempotente).

    Instala os ganchos globais (``Logger.callHandlers``/``Handler.handle``), o
    ``FiltroRedacao`` no logger raiz e em todos os handlers dele, o
    ``FormatadorSeguro`` nesses handlers (cria um ``StreamHandler`` em stderr se
    nao houver nenhum) e os excepthooks seguros.
    """
    global _configurado
    _instalar_ganchos_globais()
    raiz = logging.getLogger()
    raiz.setLevel(nivel)
    if not raiz.handlers:
        raiz.addHandler(logging.StreamHandler(sys.stderr))
    for handler in list(raiz.handlers):
        _proteger_handler(handler)
    for logger in (raiz, logging.getLogger(NAMESPACE)):
        if not any(isinstance(f, FiltroRedacao) for f in logger.filters):
            logger.addFilter(FiltroRedacao())
    instalar_excepthook_seguro()
    _configurado = True


def obter_logger(nome: str) -> logging.Logger:
    """Devolve um logger sob ``publicador`` (configura o logging se preciso)."""
    if not _configurado:
        configurar_logging()
    if nome != NAMESPACE and not nome.startswith(f"{NAMESPACE}."):
        nome = f"{NAMESPACE}.{nome}"
    logger = logging.getLogger(nome)
    if not any(isinstance(f, FiltroRedacao) for f in logger.filters):
        logger.addFilter(FiltroRedacao())
    return logger


# --------------------------------------------------------------------------- excepthooks


def _escrever_erro(texto: str) -> None:
    try:
        if sys.stderr is not None:
            sys.stderr.write(redigir(texto))
            sys.stderr.flush()
    except Exception:
        pass


def _excepthook_seguro(
    tipo: type[BaseException], valor: BaseException, tb: TracebackType | None
) -> None:
    try:
        texto = "".join(traceback.format_exception(tipo, valor, tb))
    except Exception:
        texto = f"Excecao nao tratada: {tipo.__name__} (falha ao formatar)\n"
    _escrever_erro(texto)


def _threading_excepthook_seguro(args: threading.ExceptHookArgs) -> None:
    if args.exc_type is SystemExit:
        return
    nome = args.thread.name if args.thread is not None else "?"
    try:
        texto = f"Exception in thread {nome}:\n" + "".join(
            traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)
        )
    except Exception:
        texto = f"Exception in thread {nome} (falha ao formatar)\n"
    _escrever_erro(texto)


def _unraisablehook_seguro(info: Any) -> None:
    try:
        cabecalho = f"{info.err_msg or 'Exception ignored in'}: {info.object!r}\n"
        corpo = "".join(
            traceback.format_exception(info.exc_type, info.exc_value, info.exc_traceback)
        )
        texto = cabecalho + corpo
    except Exception:
        texto = "Excecao ignorada (falha ao formatar)\n"
    _escrever_erro(texto)


def instalar_excepthook_seguro() -> None:
    """Faz tracebacks nao tratados (main, threads e unraisable) sairem redigidos."""
    sys.excepthook = _excepthook_seguro
    threading.excepthook = _threading_excepthook_seguro
    sys.unraisablehook = _unraisablehook_seguro
