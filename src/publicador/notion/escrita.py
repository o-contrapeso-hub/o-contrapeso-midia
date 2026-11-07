"""Escrita de posts no Notion (criar e editar), sem decidir nem confirmar nada.

Regras invioláveis:
- I-2: a escrita grava exatamente os campos recebidos. Este módulo NUNCA preenche
  estágio autorizador, "Selo de confirmação" ou "Confirmado em" (isso é da T022).
- Sem cópia persistente: o Notion é a fonte da verdade. Nada é escrito em disco,
  não há cache e os logs trazem apenas ids e nomes de campos (nunca título/corpo/artes).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Final
from zoneinfo import ZoneInfo

from publicador.core.redacao import obter_logger
from publicador.notion.cliente import ClienteNotion

__all__ = [
    "NAO_ALTERAR",
    "AlteracoesPost",
    "EdicaoConcorrente",
    "NaoAlterar",
    "criar_post",
    "editar_post",
    "propriedades_notion",
]

_log = obter_logger("publicador.notion.escrita")

LIMITE_RICH_TEXT: Final = 2000
LIMITE_BLOCOS_POR_REQUISICAO: Final = 100

_PROP_TITULO: Final = "Título"
_PROP_ESTAGIO: Final = "Estágio"
_PROP_FORMATO: Final = "Formato"
_PROP_PLATAFORMAS: Final = "Plataformas"
_PROP_DATA: Final = "Data e hora"
_PROP_SERIE: Final = "Série"
_PROP_ORDEM: Final = "Ordem na série"
_PROP_TEMA: Final = "Tema"
_PROP_ARTES: Final = "Artes"

_TIPOS_TEXTO: Final = frozenset(
    {
        "paragraph",
        "heading_1",
        "heading_2",
        "heading_3",
        "bulleted_list_item",
        "numbered_list_item",
        "quote",
        "callout",
        "toggle",
        "to_do",
    }
)


class NaoAlterar(Enum):
    """Marcador de "não alterar este campo" (diferente de ``None`` = limpar)."""

    VALOR = "nao_alterar"

    def __repr__(self) -> str:
        return "NAO_ALTERAR"


NAO_ALTERAR: Final = NaoAlterar.VALOR


class EdicaoConcorrente(Exception):  # noqa: N818 - nome do domínio (RF-42, A-017)
    """O post mudou no Notion depois da leitura que o usuário viu; nada foi gravado."""


def _exigir_fuso(valor: datetime, rotulo: str) -> None:
    if valor.tzinfo is None or valor.utcoffset() is None:
        raise ValueError(f"{rotulo} deve ter fuso horário (datetime ingênuo é rejeitado)")


@dataclass(frozen=True)
class AlteracoesPost:
    """Campos a gravar. ``NAO_ALTERAR`` = não mexer; ``None``/vazio = limpar o campo.

    O título não pode ser limpo. ``corpo`` não é propriedade: vira blocos de parágrafo.
    """

    titulo: str | NaoAlterar = NAO_ALTERAR
    estagio_id: str | None | NaoAlterar = NAO_ALTERAR
    plataformas: tuple[str, ...] | NaoAlterar = NAO_ALTERAR
    formato_id: str | None | NaoAlterar = NAO_ALTERAR
    serie_id: str | None | NaoAlterar = NAO_ALTERAR
    ordem_na_serie: int | None | NaoAlterar = NAO_ALTERAR
    tema_id: str | None | NaoAlterar = NAO_ALTERAR
    publicar_em: datetime | None | NaoAlterar = NAO_ALTERAR
    artes: tuple[str, ...] | NaoAlterar = NAO_ALTERAR
    corpo: str | None | NaoAlterar = NAO_ALTERAR

    def __post_init__(self) -> None:
        if not isinstance(self.titulo, NaoAlterar) and (
            not isinstance(self.titulo, str) or not self.titulo.strip()
        ):
            raise ValueError("título não pode ser vazio")
        if isinstance(self.publicar_em, datetime):
            _exigir_fuso(self.publicar_em, "publicar_em")

    def campos_informados(self) -> list[str]:
        """Nomes dos campos que serão gravados (seguro para log)."""
        return [
            nome
            for nome in (
                "titulo",
                "estagio_id",
                "plataformas",
                "formato_id",
                "serie_id",
                "ordem_na_serie",
                "tema_id",
                "publicar_em",
                "artes",
                "corpo",
            )
            if getattr(self, nome) is not NAO_ALTERAR
        ]


# --------------------------------------------------------------------------- tradução


def _texto(conteudo: str) -> list[dict[str, Any]]:
    """Divide em itens rich_text de no máximo 2000 caracteres (limite da API)."""
    return [
        {"type": "text", "text": {"content": conteudo[i : i + LIMITE_RICH_TEXT]}}
        for i in range(0, len(conteudo), LIMITE_RICH_TEXT)
    ]


def _relacao(valor: str | None) -> dict[str, Any]:
    return {"relation": [] if valor is None else [{"id": valor}]}


def _data(valor: datetime | None) -> dict[str, Any]:
    if valor is None:
        return {"date": None}
    zona = valor.tzinfo
    if isinstance(zona, ZoneInfo):
        return {
            "date": {
                "start": valor.replace(tzinfo=None).isoformat(),
                "time_zone": zona.key,
            }
        }
    return {"date": {"start": valor.isoformat()}}


def propriedades_notion(alteracoes: AlteracoesPost) -> dict[str, Any]:
    """Traduz as alterações para o formato de propriedades da API do Notion (função pura)."""
    props: dict[str, Any] = {}
    a = alteracoes
    if not isinstance(a.titulo, NaoAlterar):
        props[_PROP_TITULO] = {"title": _texto(a.titulo)}
    if not isinstance(a.estagio_id, NaoAlterar):
        props[_PROP_ESTAGIO] = _relacao(a.estagio_id)
    if not isinstance(a.plataformas, NaoAlterar):
        props[_PROP_PLATAFORMAS] = {"multi_select": [{"name": n} for n in a.plataformas]}
    if not isinstance(a.formato_id, NaoAlterar):
        props[_PROP_FORMATO] = _relacao(a.formato_id)
    if not isinstance(a.serie_id, NaoAlterar):
        props[_PROP_SERIE] = _relacao(a.serie_id)
    if not isinstance(a.ordem_na_serie, NaoAlterar):
        props[_PROP_ORDEM] = {"number": a.ordem_na_serie}
    if not isinstance(a.tema_id, NaoAlterar):
        props[_PROP_TEMA] = _relacao(a.tema_id)
    if not isinstance(a.publicar_em, NaoAlterar):
        props[_PROP_DATA] = _data(a.publicar_em)
    if not isinstance(a.artes, NaoAlterar):
        props[_PROP_ARTES] = {"rich_text": _texto("\n".join(a.artes)) if a.artes else []}
    return props


def _blocos_de_texto(corpo: str) -> list[dict[str, Any]]:
    """Um parágrafo por linha não vazia; linhas longas viram vários itens rich_text."""
    return [
        {"object": "block", "type": "paragraph", "paragraph": {"rich_text": _texto(linha)}}
        for linha in corpo.splitlines()
        if linha.strip()
    ]


def _anexar_blocos(cliente: ClienteNotion, pagina_id: str, blocos: list[dict[str, Any]]) -> None:
    for i in range(0, len(blocos), LIMITE_BLOCOS_POR_REQUISICAO):
        lote = blocos[i : i + LIMITE_BLOCOS_POR_REQUISICAO]
        cliente.requisitar("PATCH", f"/blocks/{pagina_id}/children", {"children": lote})


# --------------------------------------------------------------------------- operações


def criar_post(cliente: ClienteNotion, banco_posts_id: str, alteracoes: AlteracoesPost) -> str:
    """Cria um post no banco Posts e devolve o id da página criada.

    Exige título. O corpo vira parágrafos (até 100 blocos na criação; o excedente é
    anexado em requisições seguintes). Grava só o que foi informado.
    """
    if isinstance(alteracoes.titulo, NaoAlterar):
        raise ValueError("título é obrigatório para criar um post")
    corpo = alteracoes.corpo if isinstance(alteracoes.corpo, str) else ""
    blocos = _blocos_de_texto(corpo)
    requisicao: dict[str, Any] = {
        "parent": {"database_id": banco_posts_id},
        "properties": propriedades_notion(alteracoes),
    }
    if blocos:
        requisicao["children"] = blocos[:LIMITE_BLOCOS_POR_REQUISICAO]
    resposta = cliente.requisitar("POST", "/pages", requisicao)
    novo_id = resposta.get("id")
    if not isinstance(novo_id, str) or not novo_id:
        raise ValueError("resposta do Notion sem id da página criada")
    _anexar_blocos(cliente, novo_id, blocos[LIMITE_BLOCOS_POR_REQUISICAO:])
    _log.info("post criado: %s; campos: %s", novo_id, ",".join(alteracoes.campos_informados()))
    return novo_id


def _instante(pagina: dict[str, Any]) -> datetime | None:
    bruto = pagina.get("last_edited_time")
    if not isinstance(bruto, str):
        return None
    try:
        valor = datetime.fromisoformat(bruto)
    except ValueError:
        return None
    return valor if valor.tzinfo is not None else None


def editar_post(
    cliente: ClienteNotion,
    pagina_id: str,
    alteracoes: AlteracoesPost,
    editado_em_esperado: datetime,
) -> None:
    """Edita um post, recusando se ele mudou desde a leitura que o usuário viu (RF-42, A-017).

    Antes de escrever, lê a página e compara ``last_edited_time`` com
    ``editado_em_esperado``; se diferir, levanta ``EdicaoConcorrente`` sem enviar PATCH.

    Janela residual: a API do Notion não tem atualização condicional, então ainda existe
    um intervalo entre esse GET e o PATCH em que outra pessoa pode editar. Isso é
    aceito: a guarda de publicação revalida tudo (estágio, selo, conteúdo) de qualquer
    forma antes de publicar. Além disso, o Notion arredonda ``last_edited_time`` ao minuto.

    Só as propriedades informadas são gravadas. O corpo só é mexido se ``corpo`` for
    informado; nesse caso os blocos de texto de nível superior antigos (parágrafos,
    títulos, listas, citações etc.) são apagados e os novos criados (primeiro cria,
    depois apaga, para nunca perder o texto em caso de falha). Limites: blocos não
    textuais (imagens etc.) e filhos aninhados são mantidos/apagados junto do pai, e os
    novos parágrafos ficam no fim da página.
    """
    _exigir_fuso(editado_em_esperado, "editado_em_esperado")
    atual = _instante(cliente.obter_pagina(pagina_id))
    if atual is None:
        raise EdicaoConcorrente(
            "Não foi possível confirmar a data da última edição do post no Notion; "
            "nada foi gravado. Recarregue o post e tente de novo."
        )
    if atual != editado_em_esperado:
        raise EdicaoConcorrente(
            "O post foi editado no Notion depois da versão que você viu; nada foi gravado. "
            "Recarregue o post, confira as mudanças e refaça a edição."
        )
    propriedades = propriedades_notion(alteracoes)
    if propriedades:
        cliente.atualizar_pagina(pagina_id, propriedades)
    if not isinstance(alteracoes.corpo, NaoAlterar):
        antigos = [
            str(b["id"])
            for b in cliente.blocos_filhos(pagina_id)
            if b.get("type") in _TIPOS_TEXTO and isinstance(b.get("id"), str)
        ]
        _anexar_blocos(cliente, pagina_id, _blocos_de_texto(alteracoes.corpo or ""))
        for bloco_id in antigos:
            cliente.requisitar("DELETE", f"/blocks/{bloco_id}")
    _log.info("post editado: %s; campos: %s", pagina_id, ",".join(alteracoes.campos_informados()))
