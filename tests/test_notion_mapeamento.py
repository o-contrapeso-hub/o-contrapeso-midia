"""Testes do mapeamento Notion -> Post (publicador.notion.mapeamento).

Todos os dados sao FICTICIOS e nenhuma requisicao real e feita (I-1). Nada aqui publica (I-2).
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from publicador.core.modelos import Estagio, PapelEstagio, Serie
from publicador.core.redacao import SegredoStr, limpar_segredos_registrados
from publicador.notion.cliente import (
    ClienteNotion,
    ErroNotion,
    RespostaHttp,
)
from publicador.notion.mapeamento import (
    PostInvalido,
    data_de_propriedade,
    ler_post,
    post_de_pagina,
    separar_secoes,
    texto_de_blocos,
)

try:  # contrato entregue por bancos.py; stub local so se o modulo ainda nao existir
    from publicador.notion.bancos import Configuracao
except ImportError:  # pragma: no cover - depende da ordem de entrega

    @dataclass(frozen=True)
    class Configuracao:  # type: ignore[no-redef]
        estagios: Mapping[str, Estagio]
        formatos: Mapping[str, str]
        series: Mapping[str, Serie]
        temas: Mapping[str, str]
        problemas: tuple[str, ...] = ()


PASTA = Path(__file__).parent / "fixtures" / "notion" / "posts"
SP = ZoneInfo("America/Sao_Paulo")
ID_PRONTO = "aaaaaaaa000040008000000000000001"
ID_PAUTA = "aaaaaaaa000040008000000000000002"
ID_FORMATO = "bbbbbbbb000040008000000000000001"
ID_SERIE = "cccccccc000040008000000000000001"
ID_TEMA = "dddddddd000040008000000000000001"
ID_COMPLETA = "11111111222243338444555555555555"
ID_PAUTA_PAGINA = "66666666777748888999000000000000"


@pytest.fixture(autouse=True)
def _limpar() -> Any:
    limpar_segredos_registrados()
    yield
    limpar_segredos_registrados()


@pytest.fixture
def config() -> Configuracao:
    return Configuracao(
        estagios={
            ID_PRONTO: Estagio("pronto", PapelEstagio.PRONTO),
            ID_PAUTA: Estagio("pauta", PapelEstagio.PAUTA),
        },
        formatos={ID_FORMATO: "Reels"},
        series={ID_SERIE: Serie("Contas de casa", True)},
        temas={ID_TEMA: "Energia"},
        problemas=(),
    )


def _carregar(nome: str) -> Any:
    return json.loads((PASTA / nome).read_text(encoding="utf-8"))


@pytest.fixture
def pagina() -> dict[str, Any]:
    return dict(_carregar("pagina_completa.json"))


@pytest.fixture
def blocos() -> list[dict[str, Any]]:
    return list(_carregar("blocos_completa.json")["results"])


def _texto(*partes: str) -> list[dict[str, Any]]:
    return [{"type": "text", "plain_text": p, "annotations": {}, "href": None} for p in partes]


def _bloco(tipo: str, *partes: str) -> dict[str, Any]:
    return {"object": "block", "type": tipo, tipo: {"rich_text": _texto(*partes)}}


# ------------------------------------------------------------------ post completo


def test_post_completo(
    pagina: dict[str, Any], blocos: list[dict[str, Any]], config: Configuracao
) -> None:
    post = post_de_pagina(pagina, config, blocos)
    assert post.id == ID_COMPLETA
    assert post.titulo == "Quanto custa a energia"
    assert post.estagio == Estagio("pronto", PapelEstagio.PRONTO)
    assert post.plataformas == ("Instagram", "YouTube")
    assert post.formato == "Reels"
    assert post.serie == "Contas de casa"
    assert post.ordem_na_serie == 2
    assert post.tema == "Energia"
    assert post.publicar_em == datetime(2026, 11, 10, 14, 0, tzinfo=timezone(timedelta(hours=-3)))
    assert post.somente_data is False
    assert post.tem_data_e_hora()
    assert post.artes == ("artes/exemplo/capa.png", "instagram:artes/exemplo/slide2.png")
    assert post.selo == "a" * 64
    assert post.confirmado_em == datetime(2026, 11, 7, 10, 0, tzinfo=timezone(timedelta(hours=-3)))
    assert post.editado_em == datetime(2026, 11, 7, 18, 30, tzinfo=UTC)
    assert post.corpo == "Texto comum com negrito e um link.\n\n- primeiro item\n- segundo item"
    assert post.secoes_por_plataforma == {
        "Instagram": "Legenda do Instagram.\n\nCitação só do Instagram",
        "YouTube": "Roteiro do YouTube.\n\n1. passo um\n2. passo dois",
    }


def test_pauta_so_com_titulo(config: Configuracao) -> None:
    pagina = _carregar("pagina_pauta.json")
    blocos = _carregar("blocos_pauta.json")["results"]
    post = post_de_pagina(pagina, config, blocos)
    assert post.id == ID_PAUTA_PAGINA
    assert post.titulo == "Ideia: explicar a conta de luz"
    assert post.estagio.papel is PapelEstagio.PAUTA
    assert post.corpo == ""
    assert post.plataformas == ()
    assert post.formato is None
    assert post.serie is None
    assert post.ordem_na_serie is None
    assert post.tema is None
    assert post.publicar_em is None
    assert post.somente_data is False
    assert post.artes == ()
    assert post.selo is None
    assert post.confirmado_em is None
    assert post.secoes_por_plataforma == {}


def test_pauta_com_propriedades_ausentes(config: Configuracao) -> None:
    pagina = _carregar("pagina_pauta.json")
    pagina["properties"] = {
        k: v for k, v in pagina["properties"].items() if k in ("Título", "Estágio")
    }
    post = post_de_pagina(pagina, config)
    assert post.titulo == "Ideia: explicar a conta de luz"
    assert post.plataformas == ()
    assert post.publicar_em is None


# ------------------------------------------------------------------ datas


def _prop_data(start: str | None, time_zone: str | None = None) -> dict[str, Any]:
    if start is None:
        return {"type": "date", "date": None}
    return {"type": "date", "date": {"start": start, "end": None, "time_zone": time_zone}}


def test_data_so_dia_nao_vira_meia_noite(pagina: dict[str, Any], config: Configuracao) -> None:
    pagina["properties"]["Data e hora"] = _prop_data("2026-11-10")
    post = post_de_pagina(pagina, config)
    assert post.publicar_em is None
    assert post.somente_data is True
    assert not post.tem_data_e_hora()


def test_data_de_propriedade_so_dia() -> None:
    assert data_de_propriedade(_prop_data("2026-11-10")) == (None, True)
    assert data_de_propriedade(_prop_data(None)) == (None, False)
    assert data_de_propriedade({}) == (None, False)


def test_data_com_hora_e_fuso_preservada(pagina: dict[str, Any], config: Configuracao) -> None:
    pagina["properties"]["Data e hora"] = _prop_data("2026-11-10T09:30:00.000+02:00")
    post = post_de_pagina(pagina, config)
    assert post.publicar_em is not None
    assert post.publicar_em.utcoffset() == timedelta(hours=2)
    assert post.publicar_em == datetime(2026, 11, 10, 7, 30, tzinfo=UTC)


def test_data_com_hora_utc_z() -> None:
    valor, so_dia = data_de_propriedade(_prop_data("2026-11-10T17:00:00.000Z"))
    assert so_dia is False
    assert valor == datetime(2026, 11, 10, 17, 0, tzinfo=UTC)


def test_data_com_hora_sem_fuso_recebe_sao_paulo(
    pagina: dict[str, Any], config: Configuracao
) -> None:
    pagina["properties"]["Data e hora"] = _prop_data("2026-11-10T14:00:00.000")
    post = post_de_pagina(pagina, config)
    assert post.publicar_em == datetime(2026, 11, 10, 14, 0, tzinfo=SP)
    assert post.publicar_em is not None
    assert post.publicar_em.utcoffset() == timedelta(hours=-3)


def test_data_sem_fuso_usa_time_zone_da_propriedade_quando_ha() -> None:
    valor, _ = data_de_propriedade(_prop_data("2026-11-10T14:00:00", "Europe/Lisbon"))
    assert valor is not None
    assert valor.utcoffset() == timedelta(hours=0)


def test_data_sem_fuso_usa_fuso_informado() -> None:
    valor, _ = data_de_propriedade(_prop_data("2026-11-10T14:00:00"), fuso="UTC")
    assert valor == datetime(2026, 11, 10, 14, 0, tzinfo=UTC)


def test_data_invalida_levanta() -> None:
    with pytest.raises(PostInvalido):
        data_de_propriedade(_prop_data("amanha cedo"))


def test_confirmado_em_so_dia_fica_none(pagina: dict[str, Any], config: Configuracao) -> None:
    pagina["properties"]["Confirmado em"] = _prop_data("2026-11-07")
    assert post_de_pagina(pagina, config).confirmado_em is None


# ------------------------------------------------------------------ estagio e invalidos


def test_estagio_desconhecido_levanta(pagina: dict[str, Any], config: Configuracao) -> None:
    pagina["properties"]["Estágio"]["relation"] = [{"id": "ffffffff-0000-4000-8000-00000000ffff"}]
    with pytest.raises(PostInvalido, match="estágio"):
        post_de_pagina(pagina, config)


@pytest.mark.parametrize("relacao", [[], None])
def test_estagio_ausente_levanta(
    pagina: dict[str, Any], config: Configuracao, relacao: list[Any] | None
) -> None:
    if relacao is None:
        del pagina["properties"]["Estágio"]
    else:
        pagina["properties"]["Estágio"]["relation"] = relacao
    with pytest.raises(PostInvalido, match="estágio"):
        post_de_pagina(pagina, config)


def test_estagio_duplicado_levanta(pagina: dict[str, Any], config: Configuracao) -> None:
    pagina["properties"]["Estágio"]["relation"] = [
        {"id": ID_PRONTO},
        {"id": ID_PAUTA},
    ]
    with pytest.raises(PostInvalido, match="estágio"):
        post_de_pagina(pagina, config)


def test_formato_desconhecido_levanta(pagina: dict[str, Any], config: Configuracao) -> None:
    pagina["properties"]["Formato"]["relation"] = [{"id": "ffffffff-0000-4000-8000-00000000ffff"}]
    with pytest.raises(PostInvalido, match="formato"):
        post_de_pagina(pagina, config)


def test_titulo_vazio_levanta(pagina: dict[str, Any], config: Configuracao) -> None:
    pagina["properties"]["Título"]["title"] = []
    with pytest.raises(PostInvalido, match="título"):
        post_de_pagina(pagina, config)


def test_pagina_arquivada_levanta(pagina: dict[str, Any], config: Configuracao) -> None:
    pagina["archived"] = True
    with pytest.raises(PostInvalido, match="arquivada"):
        post_de_pagina(pagina, config)


def test_id_da_pagina_invalido_levanta(pagina: dict[str, Any], config: Configuracao) -> None:
    pagina["id"] = "nao-e-um-id"
    with pytest.raises(PostInvalido, match="id"):
        post_de_pagina(pagina, config)


def test_ordem_nao_inteira_levanta(pagina: dict[str, Any], config: Configuracao) -> None:
    pagina["properties"]["Ordem na série"]["number"] = 1.5
    with pytest.raises(PostInvalido, match="ordem"):
        post_de_pagina(pagina, config)


def test_post_invalido_e_value_error() -> None:
    assert issubclass(PostInvalido, ValueError)


# ------------------------------------------------------------------ blocos


def test_blocos_nao_textuais_ignorados() -> None:
    blocos = [
        _bloco("paragraph", "antes"),
        {"object": "block", "type": "image", "image": {"type": "external", "external": {}}},
        {"object": "block", "type": "divider", "divider": {}},
        {"object": "block", "type": "unsupported", "unsupported": {}},
        {"object": "block", "type": "code", "code": {"rich_text": _texto("print(1)")}},
        {"object": "block"},
        _bloco("paragraph", "depois"),
    ]
    assert texto_de_blocos(blocos) == "antes\n\ndepois"


def test_texto_com_anotacoes_e_links_vira_texto_simples() -> None:
    rich = [
        {
            "type": "text",
            "plain_text": "ver ",
            "annotations": {"bold": True, "code": True},
            "href": None,
        },
        {
            "type": "text",
            "plain_text": "o site",
            "text": {"content": "o site", "link": {"url": "https://exemplo.invalid"}},
            "annotations": {"italic": True},
            "href": "https://exemplo.invalid",
        },
        {"type": "mention", "plain_text": "@Alguém", "annotations": {}, "href": None},
    ]
    bloco = {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich}}
    assert texto_de_blocos([bloco]) == "ver o site@Alguém"


def test_tipos_de_texto_suportados() -> None:
    blocos = [
        _bloco("heading_1", "Título 1"),
        _bloco("heading_3", "Título 3"),
        _bloco("bulleted_list_item", "a"),
        _bloco("bulleted_list_item", "b"),
        _bloco("paragraph", "meio"),
        _bloco("numbered_list_item", "x"),
        _bloco("numbered_list_item", "y"),
        _bloco("quote", "citado"),
    ]
    assert texto_de_blocos(blocos) == (
        "Título 1\n\nTítulo 3\n\n- a\n- b\n\nmeio\n\n1. x\n2. y\n\ncitado"
    )


def test_paragrafos_vazios_sao_ignorados() -> None:
    assert texto_de_blocos([_bloco("paragraph", ""), _bloco("paragraph", "oi")]) == "oi"


def test_separar_secoes_basico() -> None:
    blocos = [
        _bloco("paragraph", "comum"),
        _bloco("heading_2", "instagram"),
        _bloco("paragraph", "ig"),
        _bloco("heading_2", "Outro título"),
        _bloco("paragraph", "volta ao comum"),
    ]
    comum, secoes = separar_secoes(blocos, ("Instagram", "YouTube"))
    assert [b["type"] for b in comum] == ["paragraph", "heading_2", "paragraph"]
    assert texto_de_blocos(comum) == "comum\n\nOutro título\n\nvolta ao comum"
    assert set(secoes) == {"Instagram"}
    assert texto_de_blocos(secoes["Instagram"]) == "ig"


def test_titulo_de_plataforma_fora_do_post_nao_inicia_secao() -> None:
    blocos = [_bloco("paragraph", "a"), _bloco("heading_2", "TikTok"), _bloco("paragraph", "b")]
    comum, secoes = separar_secoes(blocos, ("Instagram",))
    assert secoes == {}
    assert texto_de_blocos(comum) == "a\n\nTikTok\n\nb"


def test_secao_vazia_de_plataforma_e_mantida_vazia(
    pagina: dict[str, Any], config: Configuracao
) -> None:
    blocos = [
        _bloco("heading_2", "Instagram"),
        _bloco("heading_2", "YouTube"),
        _bloco("paragraph", "yt"),
    ]
    post = post_de_pagina(pagina, config, blocos)
    assert post.corpo == ""
    assert post.secoes_por_plataforma == {"Instagram": "", "YouTube": "yt"}


def test_secao_repetida_concatena(pagina: dict[str, Any], config: Configuracao) -> None:
    blocos = [
        _bloco("heading_2", "Instagram"),
        _bloco("paragraph", "um"),
        _bloco("heading_2", "Instagram"),
        _bloco("paragraph", "dois"),
    ]
    post = post_de_pagina(pagina, config, blocos)
    assert post.secoes_por_plataforma["Instagram"] == "um\n\ndois"


# ------------------------------------------------------------------ ler_post (sem rede)


class TransporteFalso:
    def __init__(self, respostas: list[RespostaHttp]) -> None:
        self.respostas = list(respostas)
        self.chamadas: list[tuple[str, str]] = []

    def enviar(
        self,
        metodo: str,
        url: str,
        cabecalhos: Mapping[str, str],
        corpo: bytes | None,
        timeout: float,
    ) -> RespostaHttp:
        self.chamadas.append((metodo, url))
        return self.respostas.pop(0)


def _resp(dados: Any, status: int = 200) -> RespostaHttp:
    return RespostaHttp(status, {}, json.dumps(dados).encode("utf-8"))


def _cliente(transporte: TransporteFalso) -> ClienteNotion:
    return ClienteNotion(
        SegredoStr("TOKEN_FALSO_abc123XYZ789"),
        transporte=transporte,
        dormir=lambda _s: None,
        relogio=lambda: 0.0,
    )


def test_ler_post_busca_pagina_e_blocos_com_paginacao(config: Configuracao) -> None:
    blocos = _carregar("blocos_completa.json")["results"]
    transporte = TransporteFalso(
        [
            _resp(_carregar("pagina_completa.json")),
            _resp({"results": blocos[:5], "has_more": True, "next_cursor": "cur1"}),
            _resp({"results": blocos[5:], "has_more": False, "next_cursor": None}),
        ]
    )
    post = ler_post(_cliente(transporte), ID_COMPLETA, config)
    assert post.titulo == "Quanto custa a energia"
    assert set(post.secoes_por_plataforma) == {"Instagram", "YouTube"}
    assert [m for m, _ in transporte.chamadas] == ["GET", "GET", "GET"]
    assert transporte.chamadas[0][1].endswith(f"/pages/{ID_COMPLETA}")
    assert f"/blocks/{ID_COMPLETA}/children" in transporte.chamadas[1][1]
    assert "start_cursor=cur1" in transporte.chamadas[2][1]


def test_ler_post_id_invalido_recusado_sem_rede(config: Configuracao) -> None:
    transporte = TransporteFalso([])
    with pytest.raises(ValueError):
        ler_post(_cliente(transporte), "../../users", config)
    assert transporte.chamadas == []


def test_ler_post_erro_do_notion_propaga(config: Configuracao) -> None:
    transporte = TransporteFalso([_resp({"code": "object_not_found"}, status=404)])
    with pytest.raises(ErroNotion):
        ler_post(_cliente(transporte), ID_COMPLETA, config)


def test_ler_post_erro_nos_blocos_propaga(config: Configuracao) -> None:
    transporte = TransporteFalso(
        [_resp(_carregar("pagina_completa.json")), _resp({"code": "restricted_resource"}, 403)]
    )
    with pytest.raises(ErroNotion):
        ler_post(_cliente(transporte), ID_COMPLETA, config)


# ------------------------------------------------------------------ I-1


def test_nenhum_campo_do_post_contem_token(
    pagina: dict[str, Any], blocos: list[dict[str, Any]], config: Configuracao
) -> None:
    post = post_de_pagina(copy.deepcopy(pagina), config, blocos)
    nomes = {f.name.lower() for f in fields(post)}
    assert not any(p in n for n in nomes for p in ("token", "senha", "segredo", "credencial"))
    assert "TOKEN_FALSO" not in repr(post)
    assert "secret_" not in repr(post).lower()
