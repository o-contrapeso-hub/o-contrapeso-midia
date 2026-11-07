"""Leitura da configuracao do Publicador a partir dos bancos do Notion.

Le os bancos de Estagios, Formatos, Series e Temas e devolve uma ``Configuracao``
imutavel. Regras (I-1/I-2):

* Nada aqui publica nem escreve no Notion; so le (via ``ClienteNotion``).
* Inconsistencias NAO sao escondidas nem corrigidas: a linha invalida e omitida e
  a razao vai para ``Configuracao.problemas`` (mensagens sem dados sensiveis).
  Quem usa a configuracao (a guarda) decide recusar. Em especial, o estagio de
  papel ``autorizado`` deve se chamar exatamente "confirmado" e ser unico.
* Falhas do Notion (``ErroNotion``) propagam; nada e engolido.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from publicador.core.modelos import (
    NOME_ESTAGIO_AUTORIZADOR,
    Estagio,
    PapelEstagio,
    Serie,
)
from publicador.notion.cliente import ClienteNotion

__all__ = [
    "Configuracao",
    "ErroLinhaConfiguracao",
    "IdsBancos",
    "estagio_de_pagina",
    "formato_de_pagina",
    "ler_configuracao",
    "serie_de_pagina",
    "tema_de_pagina",
]


class ErroLinhaConfiguracao(ValueError):
    """Linha de configuracao invalida; a mensagem nao contem dados sensiveis."""


def _normalizar_id(valor: str) -> str:
    return valor.replace("-", "").lower()


@dataclass(frozen=True)
class IdsBancos:
    """Ids dos bancos do Notion (normalizados: sem hífen, minúsculas).

    ``posts`` e ``execucoes`` (T038) são opcionais para não quebrar quem só lê a
    configuração; vazio = não informado. Quem precisa deles (o orquestrador) exige-os.
    """

    estagios: str
    formatos: str
    series: str
    temas: str
    posts: str = ""
    execucoes: str = ""

    def __post_init__(self) -> None:
        for campo in ("estagios", "formatos", "series", "temas", "posts", "execucoes"):
            object.__setattr__(self, campo, _normalizar_id(getattr(self, campo)))


@dataclass(frozen=True)
class Configuracao:
    estagios: Mapping[str, Estagio]
    formatos: Mapping[str, str]
    series: Mapping[str, Serie]
    temas: Mapping[str, str]
    problemas: tuple[str, ...]


# --------------------------------------------------------------------------- extracao


def _id_da_pagina(pagina: Mapping[str, Any]) -> str:
    valor = pagina.get("id")
    if not isinstance(valor, str) or not valor:
        raise ErroLinhaConfiguracao("linha sem identificador")
    return _normalizar_id(valor)


def _propriedade(pagina: Mapping[str, Any], nome: str) -> Mapping[str, Any] | None:
    props = pagina.get("properties")
    if not isinstance(props, dict):
        return None
    prop = props.get(nome)
    return prop if isinstance(prop, dict) else None


def _titulo(pagina: Mapping[str, Any]) -> str:
    """Texto da propriedade do tipo title (qualquer nome); vazio se ausente."""
    props = pagina.get("properties")
    if isinstance(props, dict):
        for prop in props.values():
            if isinstance(prop, dict) and prop.get("type") == "title":
                partes = prop.get("title")
                if isinstance(partes, list):
                    return "".join(
                        p.get("plain_text", "") for p in partes if isinstance(p, dict)
                    ).strip()
    return ""


def _titulo_obrigatorio(pagina: Mapping[str, Any]) -> str:
    titulo = _titulo(pagina)
    if not titulo:
        raise ErroLinhaConfiguracao("linha sem título")
    return titulo


def estagio_de_pagina(pagina: dict[str, Any]) -> Estagio:
    """Converte uma linha do banco de Estagios; levanta ``ErroLinhaConfiguracao``."""
    nome = _titulo_obrigatorio(pagina)
    prop = _propriedade(pagina, "Papel")
    selecao = prop.get("select") if prop else None
    bruto = selecao.get("name") if isinstance(selecao, dict) else None
    if not isinstance(bruto, str) or not bruto:
        raise ErroLinhaConfiguracao("estágio sem papel")
    try:
        papel = PapelEstagio(bruto)
    except ValueError:
        raise ErroLinhaConfiguracao("estágio com papel desconhecido") from None
    if papel is PapelEstagio.AUTORIZADO and nome != NOME_ESTAGIO_AUTORIZADOR:
        raise ErroLinhaConfiguracao(
            f"estágio autorizador com nome diferente de '{NOME_ESTAGIO_AUTORIZADOR}'"
        )
    if papel is not PapelEstagio.AUTORIZADO and nome == NOME_ESTAGIO_AUTORIZADOR:
        raise ErroLinhaConfiguracao(
            f"o nome '{NOME_ESTAGIO_AUTORIZADOR}' é reservado ao papel autorizado"
        )
    try:
        return Estagio(nome, papel)
    except ValueError as erro:  # defesa: o modelo recusou por outro motivo
        raise ErroLinhaConfiguracao(f"estágio recusado pelo modelo: {erro}") from None


def formato_de_pagina(pagina: dict[str, Any]) -> tuple[str, str]:
    return _id_da_pagina(pagina), _titulo_obrigatorio(pagina)


def tema_de_pagina(pagina: dict[str, Any]) -> tuple[str, str]:
    return _id_da_pagina(pagina), _titulo_obrigatorio(pagina)


def serie_de_pagina(pagina: dict[str, Any]) -> tuple[str, Serie]:
    nome = _titulo_obrigatorio(pagina)
    prop = _propriedade(pagina, "Continuidade")
    continuidade = bool(prop.get("checkbox")) if prop else False
    return _id_da_pagina(pagina), Serie(nome, continuidade)


# --------------------------------------------------------------------------- leitura


def _ler_banco[T](
    cliente: ClienteNotion,
    banco_id: str,
    rotulo: str,
    converter: Callable[[dict[str, Any]], T],
    problemas: list[str],
) -> list[tuple[str, T]]:
    itens: list[tuple[str, T]] = []
    for pagina in cliente.consultar_banco(banco_id):
        try:
            itens.append((_id_da_pagina(pagina), converter(pagina)))
        except ErroLinhaConfiguracao as erro:
            problemas.append(f"{rotulo}: {erro}")
    return itens


def ler_configuracao(cliente: ClienteNotion, ids_bancos: IdsBancos) -> Configuracao:
    """Le os quatro bancos de configuracao. ``ErroNotion`` propaga."""
    problemas: list[str] = []
    estagios = dict(
        _ler_banco(cliente, ids_bancos.estagios, "estágios", estagio_de_pagina, problemas)
    )
    formatos = dict(
        _ler_banco(
            cliente, ids_bancos.formatos, "formatos", lambda p: formato_de_pagina(p)[1], problemas
        )
    )
    series = dict(
        _ler_banco(cliente, ids_bancos.series, "séries", lambda p: serie_de_pagina(p)[1], problemas)
    )
    temas = dict(
        _ler_banco(cliente, ids_bancos.temas, "temas", lambda p: tema_de_pagina(p)[1], problemas)
    )

    autorizadores = sum(1 for e in estagios.values() if e.papel is PapelEstagio.AUTORIZADO)
    if autorizadores == 0:
        problemas.append("nenhum estágio autorizador")
    elif autorizadores > 1:
        problemas.append("mais de um estágio autorizador")

    return Configuracao(
        estagios=estagios,
        formatos=formatos,
        series=series,
        temas=temas,
        problemas=tuple(problemas),
    )
