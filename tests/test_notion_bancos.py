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
