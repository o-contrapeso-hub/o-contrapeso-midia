"""Testes da leitura da configuracao (publicador.notion.bancos).

Transporte falso com respostas gravadas (fixtures): nenhuma rede, token FICTICIO.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from publicador.core.modelos import Estagio, PapelEstagio, Serie
from publicador.core.redacao import SegredoStr
from publicador.notion.bancos import (
    Configuracao,
    IdsBancos,
    estagio_de_pagina,
    formato_de_pagina,
    ler_configuracao,
    serie_de_pagina,
    tema_de_pagina,
    tipos_de_pagina,
)
from publicador.notion.cliente import ClienteNotion, ErroNotion, RespostaHttp

PASTA = Path(__file__).parent / "fixtures" / "notion" / "bancos"
IDS = IdsBancos(
    estagios="a89a9a3bf7484d5cbf9d73ce526e0e4a",
    formatos="42c6a461a7664571b63300098b5b2b02",
    series="7bb1e2ea3cf34188a709e5dfe409a55f",
    temas="43087b4914d24a09a03f6b1fcc85571e",
)


def carregar(nome: str) -> dict[str, Any]:
    dados = json.loads((PASTA / f"{nome}.json").read_text(encoding="utf-8"))
    assert isinstance(dados, dict)
    return dados


def resposta(corpo: dict[str, Any], status: int = 200) -> RespostaHttp:
    return RespostaHttp(status, {}, json.dumps(corpo).encode("utf-8"))


@dataclass
class TransporteFalso:
    """Devolve, por banco (id na URL), uma fila de respostas."""

    por_banco: dict[str, list[RespostaHttp]]
    urls: list[str] = field(default_factory=list)

    def enviar(
        self,
        metodo: str,
        url: str,
        cabecalhos: Mapping[str, str],
        corpo: bytes | None,
        timeout: float,
    ) -> RespostaHttp:
        self.urls.append(url)
        banco = url.split("/databases/")[1].split("/")[0]
        return self.por_banco[banco].pop(0)


def montar(
    estagios: dict[str, Any] | None = None, **outros: RespostaHttp
) -> tuple[ClienteNotion, TransporteFalso]:
    por_banco = {
        IDS.estagios: [resposta(estagios or carregar("estagios"))],
        IDS.formatos: [outros.get("formatos") or resposta(carregar("formatos"))],
        IDS.series: [outros.get("series") or resposta(carregar("series"))],
        IDS.temas: [outros.get("temas") or resposta(carregar("temas"))],
    }
    transporte = TransporteFalso(por_banco)
    cliente = ClienteNotion(
        SegredoStr("TOKEN_FALSO_bancos_123456"), transporte, dormir=lambda _: None
    )
    return cliente, transporte


def com_estagios(linhas: list[tuple[str | None, str | None]]) -> dict[str, Any]:
    """Monta resposta de estagios a partir de (nome, papel), reaproveitando o molde."""
    molde = carregar("estagios")["results"][0]
    itens = []
    for i, (nome, papel) in enumerate(linhas):
        item = copy.deepcopy(molde)
        item["id"] = f"00000000-0000-4000-8000-{900 + i:012d}"
        item["properties"]["Nome"]["title"] = (
            [] if nome is None else [{"type": "text", "plain_text": nome}]
        )
        item["properties"]["Papel"]["select"] = None if papel is None else {"name": papel}
        itens.append(item)
    return {"object": "list", "results": itens, "next_cursor": None, "has_more": False}


# --------------------------------------------------------------------------- valida


def test_configuracao_valida_do_workspace_de_teste() -> None:
    cliente, _ = montar()
    cfg = ler_configuracao(cliente, IDS)
    assert isinstance(cfg, Configuracao)
    assert cfg.problemas == ()
    assert len(cfg.estagios) == 6
    papeis = {e.nome: e.papel for e in cfg.estagios.values()}
    assert papeis == {
        "pauta": PapelEstagio.PAUTA,
        "pronto": PapelEstagio.PRONTO,
        "confirmado": PapelEstagio.AUTORIZADO,
        "publicado": PapelEstagio.PUBLICADO,
        "falha na publicação": PapelEstagio.FALHA,
        "cancelado": PapelEstagio.CANCELADO,
    }
    assert cfg.estagios["00000000000040008000000000000102"] == Estagio(
        "confirmado", PapelEstagio.AUTORIZADO
    )
    assert set(cfg.formatos.values()) == {"Carrossel de fatos", "Imagem única", "Vídeo curto"}
    assert set(cfg.temas.values()) == {"Economia", "Saúde", "Tecnologia"}
    assert cfg.series["00000000000040008000000000000200"] == Serie("Fatos da semana", True)
    assert cfg.series["00000000000040008000000000000201"] == Serie("Dados soltos", False)


def test_ids_normalizados_sem_hifen_e_minusculos() -> None:
    cliente, _ = montar()
    cfg = ler_configuracao(cliente, IDS)
    for mapa in (cfg.estagios, cfg.formatos, cfg.series, cfg.temas):
        for chave in mapa:
            assert len(chave) == 32
            assert chave == chave.lower()
            assert "-" not in chave


def test_ids_de_banco_com_hifen_e_maiusculas_sao_normalizados() -> None:
    cliente, transporte = montar()
    ids = IdsBancos(
        estagios="A89A9A3B-F748-4D5C-BF9D-73CE526E0E4A",
        formatos=IDS.formatos,
        series=IDS.series,
        temas=IDS.temas,
    )
    ler_configuracao(cliente, ids)
    assert len(transporte.urls) == 4
    assert f"/databases/{IDS.estagios}/query" in transporte.urls[0]


def test_ids_de_posts_e_execucoes_sao_opcionais_e_normalizados() -> None:
    assert (IDS.posts, IDS.execucoes) == ("", "")
    ids = IdsBancos(
        estagios=IDS.estagios,
        formatos=IDS.formatos,
        series=IDS.series,
        temas=IDS.temas,
        posts="E1B19D71-705B-4FBC-8ECD-5FEDB8092FEC",
        execucoes="E882E498-7273-430B-8FE7-002CE2811FA4",
    )
    assert ids.posts == "e1b19d71705b4fbc8ecd5fedb8092fec"
    assert ids.execucoes == "e882e4987273430b8fe7002ce2811fa4"


def test_funcoes_puras_normalizam_id_em_maiusculas() -> None:
    pagina = carregar("temas")["results"][0]
    pagina["id"] = pagina["id"].upper()
    assert tema_de_pagina(pagina) == ("00000000000040008000000000000400", "Economia")


def test_funcoes_puras_de_formato_e_serie() -> None:
    assert formato_de_pagina(carregar("formatos")["results"][1])[1] == "Imagem única"
    assert serie_de_pagina(carregar("series")["results"][2]) == (
        "00000000000040008000000000000202",
        Serie("Série de teste", True),
    )


def test_estagio_de_pagina_pura() -> None:
    pagina = carregar("estagios")["results"][4]
    assert estagio_de_pagina(pagina) == Estagio("falha na publicação", PapelEstagio.FALHA)


# --------------------------------------------------------------------------- problemas


def test_autorizador_renomeado_vira_problema_e_e_omitido() -> None:
    linhas: list[tuple[str | None, str | None]] = [
        ("pauta", "pauta"),
        ("liberado", "autorizado"),
        ("publicado", "publicado"),
    ]
    cliente, _ = montar(com_estagios(linhas))
    cfg = ler_configuracao(cliente, IDS)
    assert {e.nome for e in cfg.estagios.values()} == {"pauta", "publicado"}
    assert any("nome diferente de 'confirmado'" in p for p in cfg.problemas)
    assert any("nenhum estágio autorizador" in p for p in cfg.problemas)


def test_dois_autorizadores() -> None:
    cliente, _ = montar(com_estagios([("confirmado", "autorizado"), ("confirmado", "autorizado")]))
    cfg = ler_configuracao(cliente, IDS)
    assert any("mais de um estágio autorizador" in p for p in cfg.problemas)


def test_nenhum_autorizador() -> None:
    cliente, _ = montar(com_estagios([("pauta", "pauta"), ("pronto", "pronto")]))
    cfg = ler_configuracao(cliente, IDS)
    assert cfg.problemas == ("nenhum estágio autorizador",)
    assert len(cfg.estagios) == 2


def test_nome_confirmado_em_outro_papel_e_problema() -> None:
    cliente, _ = montar(com_estagios([("confirmado", "pronto"), ("confirmado", "autorizado")]))
    cfg = ler_configuracao(cliente, IDS)
    assert len(cfg.estagios) == 1
    assert len(cfg.problemas) == 1
    assert "reservado" in cfg.problemas[0]


def test_papel_desconhecido_e_ausente() -> None:
    linhas: list[tuple[str | None, str | None]] = [
        ("confirmado", "autorizado"),
        ("estranho", "inventado"),
        ("sem papel", None),
    ]
    cliente, _ = montar(com_estagios(linhas))
    cfg = ler_configuracao(cliente, IDS)
    assert [e.nome for e in cfg.estagios.values()] == ["confirmado"]
    assert len(cfg.problemas) == 2
    assert any("papel desconhecido" in p for p in cfg.problemas)
    assert any("sem papel" in p for p in cfg.problemas)


def test_linha_sem_titulo() -> None:
    cliente, _ = montar(com_estagios([(None, "pauta"), ("confirmado", "autorizado")]))
    cfg = ler_configuracao(cliente, IDS)
    assert len(cfg.estagios) == 1
    assert cfg.problemas == ("estágios: linha sem título",)


def test_serie_e_formato_e_tema_sem_titulo_vao_para_problemas() -> None:
    series = carregar("series")
    series["results"][0]["properties"]["Nome"]["title"] = []
    temas = carregar("temas")
    temas["results"][1]["properties"]["Nome"]["title"] = []
    formatos = carregar("formatos")
    formatos["results"][2]["properties"]["Nome"]["title"] = []
    cliente, _ = montar(series=resposta(series), temas=resposta(temas), formatos=resposta(formatos))
    cfg = ler_configuracao(cliente, IDS)
    assert len(cfg.series) == 2
    assert len(cfg.temas) == 2
    assert len(cfg.formatos) == 2
    assert sorted(cfg.problemas) == [
        "formatos: linha sem título",
        "séries: linha sem título",
        "temas: linha sem título",
    ]


def test_problemas_nao_vazam_dados_sensiveis() -> None:
    cliente, _ = montar(com_estagios([("liberado", "autorizado")]))
    cfg = ler_configuracao(cliente, IDS)
    assert "TOKEN_FALSO" not in " ".join(cfg.problemas)


# --------------------------------------------------------------------------- paginacao e erros


def test_paginacao_reaproveitada_do_cliente() -> None:
    completo = carregar("temas")
    p1 = {**completo, "results": completo["results"][:2], "has_more": True, "next_cursor": "c2"}
    p2 = {**completo, "results": completo["results"][2:], "has_more": False, "next_cursor": None}
    cliente, transporte = montar()
    transporte.por_banco[IDS.temas] = [resposta(p1), resposta(p2)]
    cfg = ler_configuracao(cliente, IDS)
    assert set(cfg.temas.values()) == {"Economia", "Saúde", "Tecnologia"}


def test_falha_do_notion_propaga() -> None:
    cliente, _ = montar(series=resposta({"code": "unauthorized"}, status=401))
    with pytest.raises(ErroNotion):
        ler_configuracao(cliente, IDS)


# --------------------------------------------------------------------------- tipos por formato

ID_FORMATO_CARROSSEL = "00000000-0000-4000-8000-000000000300"
ID_FORMATO_IMAGEM = "00000000-0000-4000-8000-000000000301"


def formatos_com(
    mapeamentos: dict[str, str], compativeis: dict[str, list[str]] | None = None
) -> RespostaHttp:
    """Fixture de Formatos com o texto de "Mapeamento por plataforma" preenchido."""
    dados = copy.deepcopy(carregar("formatos"))
    for item in dados["results"]:
        props = item["properties"]
        if item["id"] in mapeamentos:
            props["Mapeamento por plataforma"]["rich_text"] = [
                {"type": "text", "plain_text": mapeamentos[item["id"]]}
            ]
        if compativeis and item["id"] in compativeis:
            props["Plataformas compatíveis"]["multi_select"] = [
                {"id": f"m{i}", "name": n} for i, n in enumerate(compativeis[item["id"]])
            ]
    return resposta(dados)


def test_fixture_sem_mapeamento_nao_tem_tipos_nem_problemas() -> None:
    cliente, _ = montar()
    cfg = ler_configuracao(cliente, IDS)
    assert cfg.tipos_por_formato == {}  # RF-43: sem mapeamento, a guarda recusa
    assert cfg.problemas_tipos == ()
    assert cfg.problemas == ()


def test_tipos_por_formato_no_formato_que_a_guarda_espera() -> None:
    resp_formatos = formatos_com(
        {
            ID_FORMATO_CARROSSEL: "Instagram: carrossel\nTikTok = fotos",
            ID_FORMATO_IMAGEM: "Instagram: imagem; TikTok: fotos\n\n",
        },
        {
            ID_FORMATO_CARROSSEL: ["Instagram", "TikTok"],
            ID_FORMATO_IMAGEM: ["Instagram", "TikTok"],
        },
    )
    cliente, transporte = montar(formatos=resp_formatos)
    cfg = ler_configuracao(cliente, IDS)
    assert cfg.tipos_por_formato == {
        "Carrossel de fatos": {"Instagram": "carrossel", "TikTok": "fotos"},
        "Imagem única": {"Instagram": "imagem", "TikTok": "fotos"},
    }
    assert cfg.problemas_tipos == () and cfg.problemas == ()
    # Mesma consulta ao banco Formatos: nenhuma requisição extra.
    assert sum("/databases/" + IDS.formatos in u for u in transporte.urls) == 1


def test_entrada_malformada_conflitante_ou_fora_das_compativeis_nao_entra() -> None:
    texto = "Instagram: carrossel\nsem separador\nTikTok: fotos\nTikTok: video\nYouTube: shorts"
    pagina = copy.deepcopy(carregar("formatos")["results"][0])
    pagina["properties"]["Mapeamento por plataforma"]["rich_text"] = [
        {"type": "text", "plain_text": texto}
    ]
    pagina["properties"]["Plataformas compatíveis"]["multi_select"] = [
        {"id": "a", "name": "Instagram"},
        {"id": "b", "name": "TikTok"},
    ]
    tipos, problemas = tipos_de_pagina(pagina)
    assert tipos == {"Instagram": "carrossel"}  # TikTok conflita; YouTube não é compatível
    assert len(problemas) == 3
    for linha in ("sem separador", "fotos", "shorts"):
        assert all(linha not in p for p in problemas)  # nada do texto livre nas mensagens


def test_plataforma_nao_compativel_e_recusada_mesmo_com_mapeamento() -> None:
    resp_formatos = formatos_com({ID_FORMATO_CARROSSEL: "TikTok: fotos"})  # só Instagram
    cliente, _ = montar(formatos=resp_formatos)
    cfg = ler_configuracao(cliente, IDS)
    assert cfg.tipos_por_formato == {}
    assert len(cfg.problemas_tipos) == 1
    assert cfg.problemas == ()  # não derruba a configuração dos estágios


def test_formatos_com_o_mesmo_nome_ficam_sem_tipos() -> None:
    dados = copy.deepcopy(carregar("formatos"))
    dados["results"][1]["properties"]["Nome"]["title"][0]["plain_text"] = "Carrossel de fatos"
    for item in dados["results"][:2]:
        item["properties"]["Mapeamento por plataforma"]["rich_text"] = [
            {"type": "text", "plain_text": "Instagram: imagem"}
        ]
    cliente, _ = montar(formatos=resposta(dados))
    cfg = ler_configuracao(cliente, IDS)
    assert "Carrossel de fatos" not in cfg.tipos_por_formato
    assert any("mesmo nome" in p for p in cfg.problemas_tipos)


def test_configuracao_continua_construivel_sem_tipos() -> None:
    cfg = Configuracao(estagios={}, formatos={}, series={}, temas={}, problemas=())
    assert cfg.tipos_por_formato == {} and cfg.problemas_tipos == ()
