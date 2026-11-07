"""Selo de confirmação (RF-26): prova de que nada publicável mudou após a confirmação (I-2).

O selo é gravado na confirmação e recalculado imediatamente antes de publicar; qualquer
divergência impede a publicação. Por isso ele é:

* **determinístico**: mesma entrada, mesmo selo (estrutura JSON canônica);
* **sensível** a tudo o que será publicado: título, corpo comum, seções por plataforma,
  plataformas, formato, data e hora (instante absoluto) e, por plataforma, a lista ORDENADA
  de artes com caminho e SHA-256 do conteúdo lido do armazém;
* **insensível** ao que não é publicado: estágio, selo, confirmado_em, editado_em, id, tema,
  série e ordem na série.

Normalização: Unicode NFC e quebras de linha ``\\r\\n``/``\\r`` -> ``\\n`` em todo texto. NÃO
se aparam espaços nem se muda a caixa do texto publicável (isso seria mudança real). Chaves de
plataforma (nomes, não conteúdo) são comparadas sem diferenciar maiúsculas e ordenadas.

Falha segura: entrada incompleta levanta ``SeloImpossivel``; erros do armazém e de variação
(``ArteNaoEncontrada``, ``CaminhoDeArteInvalido``, ``ErroAcessoArmazem``, ``VariacaoInvalida``)
propagam. Nunca há selo "padrão" ou parcial.

Formato: ``"v1:" + sha256 hex minúsculo``. O prefixo versiona o algoritmo; ``verificar_selo``
recusa versão desconhecida.

Limites conhecidos (o que o selo NÃO detecta): mudanças fora do post (configuração do formato,
mapeamento de plataformas, conta conectada); troca de arte entre o recálculo e o envio (TOCTOU,
o adaptador deve publicar os mesmos bytes cujo hash foi verificado); colisão de SHA-256.
Seções vazias ou de plataformas fora do post entram no selo mesmo sem serem publicadas
(sensibilidade a mais: pode bloquear sem necessidade, nunca liberar indevidamente).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import unicodedata
from datetime import UTC
from typing import Final

from publicador.artes.armazem import ArmazemDeArtes
from publicador.core.modelos import Post
from publicador.notion.variacoes import artes_por_plataforma

__all__ = ["VERSAO_SELO", "SeloImpossivel", "calcular_selo", "verificar_selo"]

VERSAO_SELO: Final = "v1"
_PREFIXO: Final = f"{VERSAO_SELO}:"
_RE_SELO_V1: Final = re.compile(r"v1:[0-9a-f]{64}")
_RE_QUEBRA: Final = re.compile(r"\r\n?")


class SeloImpossivel(ValueError):
    """Não há como calcular o selo (falta data e hora, título ou plataformas, ou dado ambíguo)."""


def _texto(valor: str) -> str:
    """Texto publicável: NFC e quebras de linha em ``\\n``; espaços e caixa intactos."""
    if not isinstance(valor, str):
        raise SeloImpossivel("campo de texto do post não é texto")
    return unicodedata.normalize("NFC", _RE_QUEBRA.sub("\n", valor))


def _chave_plataforma(nome: str) -> str:
    """Nome de plataforma comparável: NFC, sem espaços nas bordas, sem diferenciar caixa."""
    if not isinstance(nome, str):
        raise SeloImpossivel("nome de plataforma não é texto")
    chave = unicodedata.normalize("NFC", nome).strip().casefold()
    if not chave:
        raise SeloImpossivel("plataforma vazia")
    return chave


def _instante(post: Post) -> str:
    if post.somente_data:
        raise SeloImpossivel("post só tem data, sem hora")
    momento = post.publicar_em
    if momento is None:
        raise SeloImpossivel("post sem data e hora")
    if momento.tzinfo is None or momento.utcoffset() is None:
        raise SeloImpossivel("data e hora sem fuso horário")
    em_utc = momento.astimezone(UTC)
    precisao = "seconds" if em_utc.microsecond == 0 else "microseconds"
    return em_utc.isoformat(timespec=precisao)


def _plataformas(post: Post) -> list[str]:
    chaves = [_chave_plataforma(p) for p in post.plataformas]
    if not chaves:
        raise SeloImpossivel("post sem plataformas")
    if len(set(chaves)) != len(chaves):
        raise SeloImpossivel("plataforma repetida (sem diferenciar maiúsculas)")
    return sorted(chaves)


def _secoes(post: Post) -> dict[str, str]:
    secoes: dict[str, str] = {}
    for nome, texto in post.secoes_por_plataforma.items():
        chave = _chave_plataforma(nome)
        if chave in secoes:
            raise SeloImpossivel("seção de plataforma repetida (sem diferenciar maiúsculas)")
        secoes[chave] = _texto(texto)
    return secoes


def _artes(post: Post, armazem: ArmazemDeArtes) -> dict[str, list[dict[str, str]]]:
    hashes: dict[str, str] = {}

    def _hash(caminho: str) -> str:
        if caminho not in hashes:
            resumo = armazem.hash_sha256(caminho)  # erros do armazém propagam
            if not isinstance(resumo, str) or re.fullmatch(r"[0-9a-f]{64}", resumo) is None:
                raise SeloImpossivel("armazém devolveu hash SHA-256 inválido")
            hashes[caminho] = resumo
        return hashes[caminho]

    resultado: dict[str, list[dict[str, str]]] = {}
    for plataforma, caminhos in artes_por_plataforma(post).items():
        resultado[_chave_plataforma(plataforma)] = [
            {"caminho": _texto(c), "sha256": _hash(c)} for c in caminhos
        ]
    return resultado


def _estrutura_canonica(post: Post, armazem: ArmazemDeArtes) -> dict[str, object]:
    titulo = _texto(post.titulo)
    if not titulo.strip():
        raise SeloImpossivel("post sem título")
    instante = _instante(post)
    plataformas = _plataformas(post)
    return {
        "versao": VERSAO_SELO,
        "titulo": titulo,
        "corpo": _texto(post.corpo),
        "secoes": _secoes(post),
        "plataformas": plataformas,
        "formato": None if post.formato is None else _texto(post.formato),
        "publicar_em": instante,
        "artes": _artes(post, armazem),
    }


def calcular_selo(post: Post, armazem: ArmazemDeArtes) -> str:
    """Selo ``v1:<sha256>`` dos campos publicáveis do post e do conteúdo de cada arte."""
    serializado = json.dumps(
        _estrutura_canonica(post, armazem),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return _PREFIXO + hashlib.sha256(serializado).hexdigest()


def verificar_selo(post: Post, armazem: ArmazemDeArtes, selo_gravado: str | None) -> bool:
    """True só se ``selo_gravado`` for um selo v1 válido igual ao recalculado agora.

    Selo ausente, vazio, de versão desconhecida ou malformado -> False. Falha no recálculo
    (``SeloImpossivel``, erro do armazém ou de variação) PROPAGA: quem chama recusa.
    """
    if not isinstance(selo_gravado, str) or _RE_SELO_V1.fullmatch(selo_gravado) is None:
        return False
    atual = calcular_selo(post, armazem)
    return hmac.compare_digest(atual.encode("ascii"), selo_gravado.encode("ascii"))
