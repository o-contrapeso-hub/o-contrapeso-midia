"""Variacoes por plataforma (RF-23): conteudo e artes diferentes por plataforma.

Funcoes PURAS: nao fazem rede, nao tem credenciais (I-1) e nao publicam (I-2).

Regras:

* **Artes**: ``plataforma:caminho`` vale so para aquela plataforma (sem diferenciar maiusculas);
  sem prefixo vale para todas as plataformas do post. Prefixo de plataforma que nao esta no post
  e erro (``VariacaoInvalida``), para nao esconder arte orfa. Nao sao tratados como prefixo:
  uma letra so (unidade de disco, ``C:\\...``) e o que vier seguido de ``//`` (URL).
* **Conteudo**: a secao da plataforma, se nao vazia; senao o corpo comum.
* **Blocos**: texto comum em paragrafos; depois, por plataforma com secao, um ``heading_2`` com o
  nome da plataforma e os paragrafos dela. Linha em branco separa paragrafos (varias linhas em
  branco seguidas viram uma so na volta). Cada ``rich_text`` tem no maximo 2000 caracteres.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

from publicador.core.modelos import Post

__all__ = [
    "LIMITE_RICH_TEXT",
    "VariacaoInvalida",
    "artes_por_plataforma",
    "blocos_de_conteudo",
    "conteudo_da_plataforma",
    "referencias_de_artes",
]

LIMITE_RICH_TEXT: Final = 2000

_RE_PREFIXO: Final = re.compile(r"^([^:/\\]+):(?!//)(.*)$", re.DOTALL)
_RE_PARAGRAFOS: Final = re.compile(r"\n[^\S\n]*\n\s*")


class VariacaoInvalida(ValueError):
    """Variacao inconsistente (por exemplo, arte de plataforma que o post nao tem)."""


def _separar_prefixo(referencia: str) -> tuple[str | None, str]:
    achado = _RE_PREFIXO.match(referencia)
    if achado is None:
        return None, referencia
    prefixo = achado.group(1).strip()
    if len(prefixo) <= 1:  # unidade de disco, nao plataforma
        return None, referencia
    return prefixo, achado.group(2).strip()


def artes_por_plataforma(post: Post) -> Mapping[str, tuple[str, ...]]:
    """Distribui as artes do post por plataforma, preservando a ordem relativa."""
    plataformas: tuple[str, ...] = tuple(post.plataformas)
    nomes = {p.strip().casefold(): p for p in plataformas}
    resultado: dict[str, list[str]] = {p: [] for p in plataformas}
    for referencia in post.artes:
        prefixo, caminho = _separar_prefixo(referencia)
        if prefixo is None:
            for lista in resultado.values():
                lista.append(referencia)
            continue
        plataforma = nomes.get(prefixo.casefold())
        if plataforma is None:
            raise VariacaoInvalida(
                f"arte com prefixo de plataforma desconhecida '{prefixo}' "
                f"(plataformas do post: {', '.join(plataformas) or 'nenhuma'})"
            )
        resultado[plataforma].append(caminho)
    return {p: tuple(v) for p, v in resultado.items()}


def conteudo_da_plataforma(post: Post, plataforma: str) -> str:
    """Secao especifica da plataforma, se nao vazia; senao o corpo comum."""
    alvo = plataforma.strip().casefold()
    for nome, texto in post.secoes_por_plataforma.items():
        if nome.strip().casefold() == alvo and texto.strip():
            return str(texto)
    return str(post.corpo)


def referencias_de_artes(
    por_plataforma: Mapping[str, Sequence[str]], plataformas: Sequence[str]
) -> tuple[str, ...]:
    """Inverso de ``artes_por_plataforma``: sem prefixo se igual para todas, senao com prefixo."""
    nomes = {p.strip().casefold() for p in plataformas}
    for chave in por_plataforma:
        if chave.strip().casefold() not in nomes:
            raise VariacaoInvalida(f"artes para plataforma fora do post: '{chave}'")
    mapa = {p.strip().casefold(): tuple(v) for p, v in por_plataforma.items()}
    listas = [mapa.get(p.strip().casefold(), ()) for p in plataformas]
    if not listas:
        return ()
    if all(lista == listas[0] for lista in listas):
        return listas[0]
    return tuple(
        f"{p}:{arte}" for p, lista in zip(plataformas, listas, strict=True) for arte in lista
    )


def _rich_text(texto: str) -> list[dict[str, Any]]:
    return [
        {"type": "text", "text": {"content": texto[i : i + LIMITE_RICH_TEXT]}}
        for i in range(0, len(texto), LIMITE_RICH_TEXT)
    ]


def _paragrafos(texto: str) -> list[dict[str, Any]]:
    return [
        {"object": "block", "type": "paragraph", "paragraph": {"rich_text": _rich_text(p)}}
        for p in _RE_PARAGRAFOS.split(texto.strip())
        if p.strip()
    ]


def blocos_de_conteudo(corpo: str, secoes: Mapping[str, str]) -> list[dict[str, Any]]:
    """Blocos do Notion: texto comum e, por plataforma, ``heading_2`` + paragrafos."""
    blocos = _paragrafos(corpo)
    for plataforma, texto in secoes.items():
        blocos.append(
            {
                "object": "block",
                "type": "heading_2",
                "heading_2": {"rich_text": _rich_text(plataforma)},
            }
        )
        blocos.extend(_paragrafos(texto))
    return blocos
