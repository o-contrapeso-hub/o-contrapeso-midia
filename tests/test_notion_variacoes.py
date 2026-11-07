"""Testes das variacoes por plataforma (RF-23). Dados FICTICIOS, sem rede (I-1), sem publicar."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from publicador.core.modelos import Estagio, PapelEstagio, Post
from publicador.notion.mapeamento import separar_secoes, texto_de_blocos
from publicador.notion.variacoes import (
    VariacaoInvalida,
    artes_por_plataforma,
    blocos_de_conteudo,
    conteudo_da_plataforma,
    referencias_de_artes,
)

PASTA = Path(__file__).parent / "fixtures" / "notion" / "variacoes"


def _post(**extra: Any) -> Post:
    base: dict[str, Any] = {
        "id": "a" * 32,
        "titulo": "Post",
        "corpo": "comum",
        "estagio": Estagio("pauta", PapelEstagio.PAUTA),
        "plataformas": ("Instagram", "YouTube"),
    }
    base.update(extra)
    return Post(**base)


def _lido_do_notion(blocos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Simula a leitura da API: cada item de rich_text ganha ``plain_text``."""
    saida = copy.deepcopy(blocos)
    for b in saida:
        for item in b[b["type"]]["rich_text"]:
            item["plain_text"] = item["text"]["content"]
    return saida


def _volta(corpo: str, secoes: Mapping[str, str], plataformas: tuple[str, ...]) -> Any:
    blocos = _lido_do_notion(blocos_de_conteudo(corpo, secoes))
    comum, por = separar_secoes(blocos, plataformas)
    return texto_de_blocos(comum), {p: texto_de_blocos(b) for p, b in por.items()}


def test_fixture_duas_plataformas_conteudo_diferente() -> None:
    d = json.loads((PASTA / "post_duas_plataformas.json").read_text(encoding="utf-8"))
    post = _post(
        corpo=d["corpo"],
        plataformas=tuple(d["plataformas"]),
        secoes_por_plataforma=d["secoes"],
        artes=tuple(d["artes"]),
    )
    assert conteudo_da_plataforma(post, "Instagram") == d["secoes"]["Instagram"]
    assert conteudo_da_plataforma(post, "YouTube") == d["secoes"]["YouTube"]
    assert artes_por_plataforma(post) == {
        "Instagram": ("artes/exemplo/capa.png", "artes/exemplo/slide2.png"),
        "YouTube": ("artes/exemplo/capa.png", "artes/exemplo/miniatura.png"),
    }
    assert _volta(d["corpo"], d["secoes"], post.plataformas) == (d["corpo"], d["secoes"])


def test_conteudo_cai_no_corpo_sem_secao_ou_com_secao_vazia() -> None:
    post = _post(secoes_por_plataforma={"Instagram": "ig", "YouTube": "  "})
    assert conteudo_da_plataforma(post, "Instagram") == "ig"
    assert conteudo_da_plataforma(post, "YouTube") == "comum"
    assert conteudo_da_plataforma(post, "TikTok") == "comum"


def test_arte_comum_e_exclusiva() -> None:
    post = _post(artes=("a.png", "instagram:b.png", "c.png"))
    assert artes_por_plataforma(post) == {
        "Instagram": ("a.png", "b.png", "c.png"),
        "YouTube": ("a.png", "c.png"),
    }


def test_prefixo_desconhecido_vira_erro() -> None:
    with pytest.raises(VariacaoInvalida, match="tiktok"):
        artes_por_plataforma(_post(artes=("tiktok:x.png",)))


def test_prefixo_sem_diferenciar_maiusculas() -> None:
    post = _post(artes=("YOUTUBE:x.png", "iNsTaGrAm:y.png"))
    assert artes_por_plataforma(post) == {"Instagram": ("y.png",), "YouTube": ("x.png",)}


def test_caminho_com_url_ou_unidade_nao_e_prefixo() -> None:
    post = _post(artes=("https://exemplo.invalido/a.png", "C:\\artes\\a.png"))
    assert artes_por_plataforma(post)["Instagram"] == post.artes


def test_ida_e_volta_de_artes() -> None:
    plataformas = ("Instagram", "YouTube")
    for artes in [
        ("a.png", "instagram:b.png", "youtube:c.png"),
        ("a.png", "b.png"),
        (),
        ("youtube:c.png", "youtube:d.png"),
    ]:
        mapa = artes_por_plataforma(_post(artes=artes))
        refs = referencias_de_artes(mapa, plataformas)
        assert artes_por_plataforma(_post(artes=refs)) == mapa


def test_referencias_formato_sem_prefixo_quando_igual_para_todas() -> None:
    mapa = {"Instagram": ("a.png", "b.png"), "YouTube": ("a.png", "b.png")}
    assert referencias_de_artes(mapa, ("Instagram", "YouTube")) == ("a.png", "b.png")
    mapa2 = {"Instagram": ("a.png",), "YouTube": ("a.png", "b.png")}
    assert referencias_de_artes(mapa2, ("Instagram", "YouTube")) == (
        "Instagram:a.png",
        "YouTube:a.png",
        "YouTube:b.png",
    )
    assert referencias_de_artes({"Instagram": ["z.png"]}, ("Instagram",)) == ("z.png",)


def test_referencias_plataforma_fora_da_lista_e_erro() -> None:
    with pytest.raises(VariacaoInvalida):
        referencias_de_artes({"TikTok": ["a.png"]}, ("Instagram",))


def test_blocos_estrutura() -> None:
    blocos = blocos_de_conteudo("a\n\nb", {"Instagram": "ig1\n\nig2"})
    assert [b["type"] for b in blocos] == [
        "paragraph", "paragraph", "heading_2", "paragraph", "paragraph",
    ]  # fmt: skip
    assert blocos[2]["heading_2"]["rich_text"][0]["text"]["content"] == "Instagram"


def test_ida_e_volta_do_conteudo() -> None:
    corpo = "Primeiro.\n\nSegundo com\nquebra simples."
    secoes = {"Instagram": "ig um\n\nig dois", "YouTube": "yt"}
    assert _volta(corpo, secoes, ("Instagram", "YouTube")) == (corpo, secoes)


def test_texto_longo_dividido_e_remontado() -> None:
    longo = "x" * 4500 + " fim"
    blocos = blocos_de_conteudo(longo, {"Instagram": "y" * 2000 + "z"})
    for b in blocos:
        for item in b[b["type"]]["rich_text"]:
            assert len(item["text"]["content"]) <= 2000
    assert len(blocos[0]["paragraph"]["rich_text"]) == 3
    assert _volta(longo, {"Instagram": "y" * 2000 + "z"}, ("Instagram",)) == (
        longo,
        {"Instagram": "y" * 2000 + "z"},
    )


def test_secao_vazia_preservada_na_volta() -> None:
    corpo, secoes = _volta("comum", {"Instagram": ""}, ("Instagram",))
    assert (corpo, secoes) == ("comum", {"Instagram": ""})


def test_corpo_vazio_sem_secoes() -> None:
    assert blocos_de_conteudo("", {}) == []


def test_post_de_uma_plataforma_sem_variacoes() -> None:
    post = _post(plataformas=("Instagram",), artes=("a.png", "b.png"))
    assert artes_por_plataforma(post) == {"Instagram": ("a.png", "b.png")}
    assert conteudo_da_plataforma(post, "Instagram") == "comum"
    assert referencias_de_artes(artes_por_plataforma(post), post.plataformas) == post.artes
    assert _volta("comum", {}, post.plataformas) == ("comum", {})


def test_sem_mutacao_das_entradas() -> None:
    secoes = {"Instagram": "ig"}
    mapa = {"Instagram": ["a.png"], "YouTube": ["a.png"]}
    post = _post(artes=("a.png", "instagram:b.png"), secoes_por_plataforma=secoes)
    antes = (copy.deepcopy(secoes), copy.deepcopy(mapa), post.artes)
    artes_por_plataforma(post)
    conteudo_da_plataforma(post, "Instagram")
    referencias_de_artes(mapa, ("Instagram", "YouTube"))
    blocos_de_conteudo(post.corpo, secoes)
    assert (secoes, mapa, post.artes) == antes
