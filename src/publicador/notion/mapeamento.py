"""Mapeamento de paginas do Notion (banco Posts) para o modelo ``Post``.

Este modulo apenas LE e converte; nao publica nada (I-2) e nao tem credenciais (I-1).
O Notion e a fonte da verdade: o que faltar la fica ``None`` no ``Post`` e a guarda decide.

Decisoes:

* **Estagio** e obrigatorio em ``Post``. Relacao ausente, vazia, com mais de um item ou com id
  fora de ``config.estagios`` levanta ``PostInvalido`` (nao ha estagio sentinela). O mesmo vale
  para uma relacao de formato, serie ou tema cujo id nao esteja na configuracao (config
  inconsistente nao e silenciada); relacao vazia/ausente vira ``None``.
* **Datas**: com hora e fuso, preserva; com hora e sem fuso, usa o ``time_zone`` da propriedade
  quando houver e, senao, o fuso padrao do projeto (``America/Sao_Paulo``). Data so-dia NUNCA
  vira meia-noite: ``publicar_em`` fica ``None`` e ``Post.somente_data`` fica ``True``.
  ``confirmado_em`` so-dia tambem fica ``None`` (nao se inventa hora).
* **Artes**: uma referencia por linha, exatamente como escrita (inclusive o prefixo opcional
  ``plataforma:``, interpretado depois pela validacao das artes).
* **Corpo**: so blocos de texto do nivel superior da pagina (filhos aninhados nao sao lidos).
  Um titulo (heading) cujo texto e o nome de uma plataforma DO POST inicia a secao dela, que vai
  ate o proximo titulo qualquer; o resto, inclusive o que vem depois desse proximo titulo, e
  conteudo comum (``corpo``).
* Paginas arquivadas sao recusadas.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from publicador.core.modelos import Estagio, Post
from publicador.notion.cliente import ClienteNotion

if TYPE_CHECKING:
    from publicador.notion.bancos import Configuracao

__all__ = [
    "FUSO_PADRAO",
    "PostInvalido",
    "data_de_propriedade",
    "ler_post",
    "post_de_pagina",
    "separar_secoes",
    "texto_de_blocos",
]

FUSO_PADRAO: Final = "America/Sao_Paulo"

_RE_ID: Final = re.compile(
    r"[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_TITULOS: Final = frozenset({"heading_1", "heading_2", "heading_3"})
_TIPOS_TEXTO: Final = frozenset(
    {
        "paragraph",
        "heading_1",
        "heading_2",
        "heading_3",
        "bulleted_list_item",
        "numbered_list_item",
        "to_do",
        "quote",
        "callout",
        "toggle",
    }
)

_PROP_TITULO: Final = "Título"
_PROP_ESTAGIO: Final = "Estágio"
_PROP_FORMATO: Final = "Formato"
_PROP_PLATAFORMAS: Final = "Plataformas"
_PROP_DATA: Final = "Data e hora"
_PROP_SERIE: Final = "Série"
_PROP_ORDEM: Final = "Ordem na série"
_PROP_TEMA: Final = "Tema"
_PROP_ARTES: Final = "Artes"
_PROP_SELO: Final = "Selo de confirmação"
_PROP_CONFIRMADO: Final = "Confirmado em"


class PostInvalido(ValueError):
    """A pagina nao pode virar um ``Post`` valido (motivo claro, sem dados sensiveis)."""


# --------------------------------------------------------------------------- utilitarios


def _normalizar_id(valor: object, rotulo: str) -> str:
    if not isinstance(valor, str) or _RE_ID.fullmatch(valor) is None:
        raise PostInvalido(f"id inválido para {rotulo}")
    return valor.replace("-", "").lower()


def _propriedade(pagina: Mapping[str, Any], nome: str) -> Mapping[str, Any]:
    props = pagina.get("properties")
    if not isinstance(props, Mapping):
        return {}
    valor = props.get(nome)
    return valor if isinstance(valor, Mapping) else {}


def _texto_rico(itens: object) -> str:
    """Concatena ``plain_text`` (sem anotacoes nem links) de uma lista rich_text."""
    if not isinstance(itens, Sequence) or isinstance(itens, str):
        return ""
    partes: list[str] = []
    for item in itens:
        if isinstance(item, Mapping):
            texto = item.get("plain_text")
            if isinstance(texto, str):
                partes.append(texto)
    return "".join(partes)


def _ids_relacao(prop: Mapping[str, Any]) -> list[str]:
    itens = prop.get("relation")
    if not isinstance(itens, Sequence) or isinstance(itens, str):
        return []
    return [_normalizar_id(i.get("id"), "relação") for i in itens if isinstance(i, Mapping)]


def _resolver_opcional[T](
    pagina: Mapping[str, Any], nome: str, tabela: Mapping[str, T], rotulo: str
) -> T | None:
    ids = _ids_relacao(_propriedade(pagina, nome))
    if not ids:
        return None
    if len(ids) > 1:
        raise PostInvalido(f"{rotulo} com mais de um item")
    achado = tabela.get(ids[0])
    if achado is None:
        raise PostInvalido(f"{rotulo} desconhecido na configuração")
    return achado


def _estagio(pagina: Mapping[str, Any], config: Configuracao) -> Estagio:
    ids = _ids_relacao(_propriedade(pagina, _PROP_ESTAGIO))
    if len(ids) != 1:
        raise PostInvalido("estágio ausente ou ambíguo (exige exatamente um)")
    estagio = config.estagios.get(ids[0])
    if estagio is None:
        raise PostInvalido("estágio desconhecido na configuração")
    return estagio


# --------------------------------------------------------------------------- datas


def _com_fuso(valor: datetime, fuso: str, time_zone: object) -> datetime:
    if valor.tzinfo is not None and valor.utcoffset() is not None:
        return valor
    nome = time_zone if isinstance(time_zone, str) and time_zone else fuso
    try:
        zona = ZoneInfo(nome)
    except (ZoneInfoNotFoundError, ValueError):
        if nome == fuso:
            raise
        zona = ZoneInfo(fuso)
    return valor.replace(tzinfo=zona)


def data_de_propriedade(
    propriedade: Mapping[str, Any], fuso: str = FUSO_PADRAO
) -> tuple[datetime | None, bool]:
    """Le uma propriedade ``date`` e devolve ``(data_hora_com_fuso, somente_data)``.

    Sem valor: ``(None, False)``. So o dia (sem hora): ``(None, True)``, nunca meia-noite.
    """
    dado = propriedade.get("date")
    if not isinstance(dado, Mapping):
        return None, False
    inicio = dado.get("start")
    if not isinstance(inicio, str) or not inicio.strip():
        return None, False
    inicio = inicio.strip()
    try:
        if len(inicio) == 10:
            date.fromisoformat(inicio)
            return None, True
        valor = datetime.fromisoformat(inicio)
    except ValueError:
        raise PostInvalido("data inválida na propriedade de data") from None
    return _com_fuso(valor, fuso, dado.get("time_zone")), False


# --------------------------------------------------------------------------- blocos


def _texto_do_bloco(bloco: Mapping[str, Any]) -> tuple[str, str] | None:
    """``(tipo, texto)`` de um bloco de texto nao vazio, ou ``None`` se nao for texto."""
    tipo = bloco.get("type")
    if not isinstance(tipo, str) or tipo not in _TIPOS_TEXTO:
        return None
    conteudo = bloco.get(tipo)
    if not isinstance(conteudo, Mapping):
        return None
    texto = _texto_rico(conteudo.get("rich_text")).strip()
    return (tipo, texto) if texto else None


def texto_de_blocos(blocos: Sequence[Mapping[str, Any]]) -> str:
    """Converte blocos de texto em texto simples (sem anotacoes, links ou marcacao).

    Paragrafos, titulos, citacoes etc. ficam separados por linha em branco; itens de lista
    consecutivos ficam em linhas seguidas (``- item`` / ``1. item``). Blocos nao textuais,
    desconhecidos ou vazios sao ignorados sem falhar.
    """
    saida = ""
    anterior: str | None = None
    numero = 0
    for bloco in blocos:
        if not isinstance(bloco, Mapping):
            continue
        item = _texto_do_bloco(bloco)
        if item is None:
            continue
        tipo, texto = item
        if tipo == "numbered_list_item":
            numero = numero + 1 if anterior == tipo else 1
            texto = f"{numero}. {texto}"
        elif tipo == "bulleted_list_item":
            texto = f"- {texto}"
        if anterior is None:
            separador = ""
        elif tipo == anterior and tipo in ("bulleted_list_item", "numbered_list_item"):
            separador = "\n"
        else:
            separador = "\n\n"
        saida += separador + texto
        anterior = tipo
    return saida


def separar_secoes(
    blocos: Sequence[Mapping[str, Any]], plataformas: Sequence[str]
) -> tuple[list[Mapping[str, Any]], dict[str, list[Mapping[str, Any]]]]:
    """Separa o conteudo comum das secoes por plataforma (RF-23).

    Um titulo cujo texto (sem diferenciar maiusculas) e o nome de uma plataforma de
    ``plataformas`` inicia a secao dela; qualquer outro titulo a encerra. O titulo da secao
    nao entra no conteudo. Secoes repetidas da mesma plataforma sao concatenadas.
    """
    nomes = {p.strip().casefold(): p for p in plataformas}
    comum: list[Mapping[str, Any]] = []
    secoes: dict[str, list[Mapping[str, Any]]] = {}
    atual: str | None = None
    for bloco in blocos:
        if not isinstance(bloco, Mapping):
            continue
        if bloco.get("type") in _TITULOS:
            texto = _texto_rico(_conteudo(bloco)).strip().casefold()
            plataforma = nomes.get(texto)
            if plataforma is not None:
                atual = plataforma
                secoes.setdefault(plataforma, [])
                continue
            atual = None
        (comum if atual is None else secoes[atual]).append(bloco)
    return comum, secoes


def _conteudo(bloco: Mapping[str, Any]) -> object:
    tipo = bloco.get("type")
    interno = bloco.get(tipo) if isinstance(tipo, str) else None
    return interno.get("rich_text") if isinstance(interno, Mapping) else None


# --------------------------------------------------------------------------- post


def _plataformas(pagina: Mapping[str, Any]) -> tuple[str, ...]:
    itens = _propriedade(pagina, _PROP_PLATAFORMAS).get("multi_select")
    if not isinstance(itens, Sequence) or isinstance(itens, str):
        return ()
    nomes: list[str] = []
    for item in itens:
        nome = item.get("name") if isinstance(item, Mapping) else None
        if isinstance(nome, str) and nome.strip() and nome.strip() not in nomes:
            nomes.append(nome.strip())
    return tuple(nomes)


def _ordem(pagina: Mapping[str, Any]) -> int | None:
    numero = _propriedade(pagina, _PROP_ORDEM).get("number")
    if numero is None:
        return None
    if isinstance(numero, bool) or not isinstance(numero, int | float):
        raise PostInvalido("ordem na série inválida")
    if isinstance(numero, float) and not numero.is_integer():
        raise PostInvalido("ordem na série deve ser inteira")
    return int(numero)


def _artes(pagina: Mapping[str, Any]) -> tuple[str, ...]:
    texto = _texto_rico(_propriedade(pagina, _PROP_ARTES).get("rich_text"))
    return tuple(linha.strip() for linha in texto.splitlines() if linha.strip())


def post_de_pagina(
    pagina: Mapping[str, Any],
    config: Configuracao,
    blocos: Sequence[Mapping[str, Any]] = (),
    *,
    fuso: str = FUSO_PADRAO,
) -> Post:
    """Converte uma pagina do banco Posts (e seus blocos) em ``Post``.

    Levanta ``PostInvalido`` se a pagina nao puder ser lida com seguranca (id, titulo ou
    estagio invalidos, relacao desconhecida, data invalida, pagina arquivada).
    """
    if pagina.get("archived") is True or pagina.get("in_trash") is True:
        raise PostInvalido("página arquivada")
    post_id = _normalizar_id(pagina.get("id"), "página")
    titulo = _texto_rico(_propriedade(pagina, _PROP_TITULO).get("title")).strip()
    if not titulo:
        raise PostInvalido("título vazio ou ausente")
    estagio = _estagio(pagina, config)
    plataformas = _plataformas(pagina)
    publicar_em, somente_data = data_de_propriedade(_propriedade(pagina, _PROP_DATA), fuso)
    confirmado_em, _ = data_de_propriedade(_propriedade(pagina, _PROP_CONFIRMADO), fuso)
    serie = _resolver_opcional(pagina, _PROP_SERIE, config.series, "série")
    comum, secoes = separar_secoes(blocos, plataformas)
    selo = _texto_rico(_propriedade(pagina, _PROP_SELO).get("rich_text")).strip() or None
    editado_em = _editado_em(pagina)
    try:
        return Post(
            id=post_id,
            titulo=titulo,
            corpo=texto_de_blocos(comum),
            estagio=estagio,
            plataformas=plataformas,
            formato=_resolver_opcional(pagina, _PROP_FORMATO, config.formatos, "formato"),
            serie=serie.nome if serie is not None else None,
            ordem_na_serie=_ordem(pagina),
            tema=_resolver_opcional(pagina, _PROP_TEMA, config.temas, "tema"),
            publicar_em=publicar_em,
            artes=_artes(pagina),
            selo=selo,
            confirmado_em=confirmado_em,
            editado_em=editado_em,
            secoes_por_plataforma={p: texto_de_blocos(b) for p, b in secoes.items()},
            somente_data=somente_data,
        )
    except ValueError as erro:
        if isinstance(erro, PostInvalido):
            raise
        raise PostInvalido(f"post inválido: {erro}") from None


def _editado_em(pagina: Mapping[str, Any]) -> datetime | None:
    bruto = pagina.get("last_edited_time")
    if not isinstance(bruto, str):
        return None
    try:
        valor = datetime.fromisoformat(bruto)
    except ValueError:
        raise PostInvalido("last_edited_time inválido") from None
    if valor.tzinfo is None:
        raise PostInvalido("last_edited_time sem fuso")
    return valor


def ler_post(cliente: ClienteNotion, pagina_id: str, config: Configuracao) -> Post:
    """Busca a pagina e seus blocos (paginados) no Notion e a converte em ``Post``.

    Id invalido levanta ``ValueError`` (do cliente) sem rede; falhas do Notion propagam como
    ``ErroNotion``.
    """
    pagina = cliente.obter_pagina(pagina_id)
    blocos = cliente.blocos_filhos(pagina_id)
    return post_de_pagina(pagina, config, blocos)
