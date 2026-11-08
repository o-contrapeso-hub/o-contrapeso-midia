"""Leitura da configuracao do Publicador a partir dos bancos do Notion.

Le os bancos de Estagios, Formatos, Series e Temas e devolve uma ``Configuracao``
imutavel. Regras (I-1/I-2):

* Nada aqui publica nem escreve no Notion; so le (via ``ClienteNotion``).
* Inconsistencias NAO sao escondidas nem corrigidas: a linha invalida e omitida e
  a razao vai para ``Configuracao.problemas`` (mensagens sem dados sensiveis).
  Quem usa a configuracao (a guarda) decide recusar. Em especial, o estagio de
  papel ``autorizado`` deve se chamar exatamente "confirmado" e ser unico.
* Falhas do Notion (``ErroNotion``) propagam; nada e engolido.
* Tipos por formato (T039; RF-43, A-023): lidos da MESMA consulta ao banco Formatos, das
  propriedades que o esquema real tem: "Mapeamento por plataforma" (rich_text, texto livre)
  e "Plataformas compatíveis" (multi_select). Formato do texto (decisao desta tarefa, o
  esquema nao o define): uma entrada por linha (ou separada por ";"), ``Plataforma: tipo``
  (aceita tambem ``=``). Uma entrada so vale se a plataforma esta em "Plataformas
  compativeis" (intersecao: lado seguro). Entrada malformada, repetida com tipos
  diferentes, fora das compativeis ou formato com nome repetido NAO entra no mapa e vira
  ``problemas_tipos`` (mensagens sem o texto da linha). Sem mapeamento = formato sem tipos
  = a guarda recusa (``FORMATO_SEM_TIPO``). Problemas de tipos NAO entram em ``problemas``
  (nao derrubam a rodada inteira; so o formato afetado fica sem tipo).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

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
    "tipos_de_pagina",
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
    # Nome do formato -> plataforma -> tipo de publicacao (RF-43, A-023); o formato que
    # ``ContextoGuarda.tipos_por_formato`` espera. Ausente = vazio = tudo recusado.
    tipos_por_formato: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    problemas_tipos: tuple[str, ...] = ()


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


_PROP_MAPEAMENTO: Final = "Mapeamento por plataforma"
_PROP_COMPATIVEIS: Final = "Plataformas compatíveis"
_RE_ENTRADA: Final = re.compile(r"\s*([A-Za-z0-9_\-]{1,40})\s*[:=]\s*([\w .\-]{1,40}?)\s*")


def _texto_rico(prop: Mapping[str, Any] | None) -> str:
    partes = prop.get("rich_text") if prop else None
    if not isinstance(partes, list):
        return ""
    return "".join(
        p.get("plain_text", "") for p in partes if isinstance(p, dict) and "plain_text" in p
    )


def tipos_de_pagina(pagina: Mapping[str, Any]) -> tuple[Mapping[str, str], tuple[str, ...]]:
    """Tipos por plataforma de uma linha do banco Formatos e os problemas encontrados.

    Veja o docstring do modulo para o formato do texto. Nunca levanta; os problemas sao
    mensagens fixas (sem o texto da linha).
    """
    prop = _propriedade(pagina, _PROP_MAPEAMENTO)
    texto = _texto_rico(prop)
    compat_prop = _propriedade(pagina, _PROP_COMPATIVEIS)
    itens = compat_prop.get("multi_select") if compat_prop else None
    compativeis = {
        i["name"] for i in itens or [] if isinstance(i, dict) and isinstance(i.get("name"), str)
    }
    tipos: dict[str, str] = {}
    conflito: set[str] = set()
    problemas: list[str] = []
    for bruta in re.split(r"[\n;]", texto):
        if not bruta.strip():
            continue
        casou = _RE_ENTRADA.fullmatch(bruta)
        if casou is None:
            problemas.append("mapeamento por plataforma com entrada malformada")
            continue
        plataforma, tipo = casou.group(1), casou.group(2)
        if plataforma in tipos and tipos[plataforma] != tipo:
            conflito.add(plataforma)
        tipos[plataforma] = tipo
    for plataforma in sorted(conflito):
        del tipos[plataforma]
        problemas.append("mapeamento por plataforma com tipos conflitantes para a mesma plataforma")
    for plataforma in sorted(set(tipos) - compativeis):
        del tipos[plataforma]
        problemas.append("mapeamento por plataforma fora das plataformas compatíveis do formato")
    return tipos, tuple(problemas)


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
    linhas_formatos = _ler_banco(
        cliente, ids_bancos.formatos, "formatos", lambda p: (formato_de_pagina(p)[1], p), problemas
    )
    formatos = {i: nome for i, (nome, _) in linhas_formatos}
    tipos: dict[str, Mapping[str, str]] = {}
    problemas_tipos: list[str] = []
    nomes_repetidos: set[str] = set()
    for _, (nome, pagina) in linhas_formatos:
        mapa, achados = tipos_de_pagina(pagina)
        problemas_tipos.extend(achados)
        if nome in tipos or nome in nomes_repetidos:  # dois formatos com o mesmo nome
            nomes_repetidos.add(nome)
            tipos.pop(nome, None)
            continue
        tipos[nome] = dict(mapa)
    problemas_tipos.extend(
        "formatos com o mesmo nome; nenhum tipo é usado para esse nome" for _ in nomes_repetidos
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
        tipos_por_formato={nome: mapa for nome, mapa in tipos.items() if mapa},
        problemas_tipos=tuple(problemas_tipos),
    )
