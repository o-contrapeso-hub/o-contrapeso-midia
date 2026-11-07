"""Testes da escrita no Notion (publicador.notion.escrita). Valores FICTICIOS, sem rede."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from publicador.core.redacao import SegredoStr
from publicador.notion.cliente import (
    ClienteNotion,
    ErroNotion,
    ErroNotionIncerto,
    ErroTransporte,
    RespostaHttp,
)
from publicador.notion.escrita import (
    NAO_ALTERAR,
    AlteracoesPost,
    EdicaoConcorrente,
    criar_post,
    editar_post,
    propriedades_notion,
)

TOKEN = "TOKEN_FALSO_abc123XYZ789"
BANCO = "e1b19d71705b4fbc8ecd5fedb8092fec"
PAGINA = "0123456789abcdef0123456789abcdef"
REL_A = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
REL_B = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
EDITADO = "2026-11-07T12:00:00.000Z"
EDITADO_DT = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
SP = ZoneInfo("America/Sao_Paulo")


@dataclass
class Chamada:
    metodo: str
    url: str
    corpo: bytes | None

    @property
    def json(self) -> dict[str, Any]:
        assert self.corpo is not None
        dados = json.loads(self.corpo)
        assert isinstance(dados, dict)
        return dados


@dataclass
class TransporteFalso:
    fila: list[RespostaHttp | Exception]
    chamadas: list[Chamada] = field(default_factory=list)

    def enviar(
        self,
        metodo: str,
        url: str,
        cabecalhos: Mapping[str, str],
        corpo: bytes | None,
        timeout: float,
    ) -> RespostaHttp:
        self.chamadas.append(Chamada(metodo, url, corpo))
        item = self.fila.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def resp(status: int, dados: object) -> RespostaHttp:
    return RespostaHttp(status, {}, json.dumps(dados).encode())


def montar(fila: list[RespostaHttp | Exception]) -> tuple[ClienteNotion, TransporteFalso]:
    transp = TransporteFalso(fila)
    cliente = ClienteNotion(SegredoStr(TOKEN), transporte=transp, dormir=lambda _: None)
    return cliente, transp


def pagina(editado: str = EDITADO) -> dict[str, Any]:
    return {"id": PAGINA, "last_edited_time": editado, "properties": {}}


def vazio() -> dict[str, Any]:
    return {"results": [], "has_more": False}


# ------------------------------------------------------------------ propriedades_notion


def test_nada_alterado_gera_dicionario_vazio() -> None:
    assert propriedades_notion(AlteracoesPost()) == {}


def test_traducao_completa_usa_nomes_reais() -> None:
    alt = AlteracoesPost(
        titulo="Titulo ficticio",
        estagio_id=REL_A,
        plataformas=("Instagram", "TikTok"),
        formato_id=REL_B,
        serie_id=REL_A,
        ordem_na_serie=3,
        tema_id=REL_B,
        publicar_em=datetime(2026, 12, 1, 9, 30, tzinfo=SP),
        artes=("a/1.png", "tiktok:a/2.mp4"),
        corpo="nao e propriedade",
    )
    props = propriedades_notion(alt)
    assert set(props) == {
        "Título",
        "Estágio",
        "Plataformas",
        "Formato",
        "Série",
        "Ordem na série",
        "Tema",
        "Data e hora",
        "Artes",
    }
    assert props["Título"] == {"title": [{"type": "text", "text": {"content": "Titulo ficticio"}}]}
    assert props["Estágio"] == {"relation": [{"id": REL_A}]}
    assert props["Plataformas"] == {"multi_select": [{"name": "Instagram"}, {"name": "TikTok"}]}
    assert props["Ordem na série"] == {"number": 3}
    assert props["Data e hora"] == {
        "date": {"start": "2026-12-01T09:30:00", "time_zone": "America/Sao_Paulo"}
    }
    assert props["Artes"] == {
        "rich_text": [{"type": "text", "text": {"content": "a/1.png\ntiktok:a/2.mp4"}}]
    }


def test_data_com_offset_fixo_vai_com_offset_sem_time_zone() -> None:
    fuso = datetime(2026, 12, 1, 9, 30, tzinfo=timezone_menos3())
    props = propriedades_notion(AlteracoesPost(publicar_em=fuso))
    assert props["Data e hora"] == {"date": {"start": "2026-12-01T09:30:00-03:00"}}


def timezone_menos3() -> Any:
    from datetime import timezone

    return timezone(timedelta(hours=-3))


def test_limpar_versus_nao_alterar() -> None:
    props = propriedades_notion(
        AlteracoesPost(
            serie_id=None,
            ordem_na_serie=None,
            tema_id=None,
            formato_id=None,
            estagio_id=None,
            publicar_em=None,
            artes=(),
            plataformas=(),
        )
    )
    assert props["Série"] == {"relation": []}
    assert props["Tema"] == {"relation": []}
    assert props["Formato"] == {"relation": []}
    assert props["Estágio"] == {"relation": []}
    assert props["Ordem na série"] == {"number": None}
    assert props["Data e hora"] == {"date": None}
    assert props["Artes"] == {"rich_text": []}
    assert props["Plataformas"] == {"multi_select": []}
    assert "Título" not in props
    assert AlteracoesPost().serie_id is NAO_ALTERAR


def test_datetime_ingenuo_recusado() -> None:
    with pytest.raises(ValueError, match="fuso"):
        AlteracoesPost(publicar_em=datetime(2026, 12, 1, 9, 30))


def test_titulo_vazio_recusado() -> None:
    with pytest.raises(ValueError):
        AlteracoesPost(titulo="   ")
    with pytest.raises(ValueError):
        AlteracoesPost(titulo=None)


def test_nunca_preenche_selo_nem_confirmado_em() -> None:
    props = propriedades_notion(AlteracoesPost(titulo="x", estagio_id=REL_A))
    assert "Selo de confirmação" not in props
    assert "Confirmado em" not in props
    assert set(props) == {"Título", "Estágio"}


# ------------------------------------------------------------------ criar_post


def test_criar_pauta_so_com_titulo() -> None:
    cliente, transp = montar([resp(200, {"id": PAGINA})])
    novo = criar_post(cliente, BANCO, AlteracoesPost(titulo="Pauta ficticia"))
    assert novo == PAGINA
    assert len(transp.chamadas) == 1
    c = transp.chamadas[0]
    assert (c.metodo, c.url) == ("POST", "https://api.notion.com/v1/pages")
    assert c.json == {
        "parent": {"database_id": BANCO},
        "properties": {
            "Título": propriedades_notion(AlteracoesPost(titulo="Pauta ficticia"))["Título"]
        },
    }


def test_criar_exige_titulo() -> None:
    cliente, transp = montar([])
    with pytest.raises(ValueError, match="título"):
        criar_post(cliente, BANCO, AlteracoesPost(corpo="so corpo"))
    with pytest.raises(ValueError, match="título"):
        criar_post(cliente, BANCO, AlteracoesPost(titulo=None))
    assert transp.chamadas == []


def test_criar_post_completo_envia_corpo_em_paragrafos() -> None:
    cliente, transp = montar([resp(200, {"id": PAGINA})])
    alt = AlteracoesPost(
        titulo="Completo",
        estagio_id=REL_A,
        plataformas=("Instagram",),
        ordem_na_serie=2,
        publicar_em=datetime(2026, 12, 1, 9, 30, tzinfo=SP),
        corpo="Linha um\n\nLinha dois",
    )
    criar_post(cliente, BANCO, alt)
    corpo = transp.chamadas[0].json
    assert corpo["properties"] == propriedades_notion(alt)
    assert corpo["children"] == [
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": "Linha um"}}]},
        },
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": "Linha dois"}}]},
        },
    ]


def test_corpo_longo_dividido_em_pedacos_de_2000() -> None:
    cliente, transp = montar([resp(200, {"id": PAGINA})])
    longo = "x" * 4500
    criar_post(cliente, BANCO, AlteracoesPost(titulo="Longo", corpo=longo))
    blocos = transp.chamadas[0].json["children"]
    assert len(blocos) == 1
    itens = blocos[0]["paragraph"]["rich_text"]
    tamanhos = [len(i["text"]["content"]) for i in itens]
    assert tamanhos == [2000, 2000, 500]
    assert "".join(i["text"]["content"] for i in itens) == longo


def test_mais_de_100_blocos_o_excedente_vai_por_append() -> None:
    cliente, transp = montar([resp(200, {"id": PAGINA}), resp(200, vazio())])
    corpo = "\n".join(f"l{i}" for i in range(130))
    criar_post(cliente, BANCO, AlteracoesPost(titulo="Muitos", corpo=corpo))
    assert len(transp.chamadas[0].json["children"]) == 100
    c2 = transp.chamadas[1]
    assert (c2.metodo, c2.url) == ("PATCH", f"https://api.notion.com/v1/blocks/{PAGINA}/children")
    assert len(c2.json["children"]) == 30


def test_erro_do_notion_propaga_na_criacao() -> None:
    cliente, _ = montar([resp(400, {"code": "validation_error"})])
    with pytest.raises(ErroNotion) as info:
        criar_post(cliente, BANCO, AlteracoesPost(titulo="X"))
    assert info.value.status == 400


def test_criar_post_5xx_envia_uma_unica_requisicao_e_levanta_incerto() -> None:
    cliente, transp = montar([resp(500, {"code": "internal"}), resp(200, {"id": PAGINA})])
    with pytest.raises(ErroNotionIncerto) as info:
        criar_post(cliente, BANCO, AlteracoesPost(titulo="X"))
    assert len(transp.chamadas) == 1
    assert "confira no notion" in str(info.value).lower()
    assert TOKEN not in str(info.value)


def test_criar_post_erro_de_transporte_nao_repete() -> None:
    cliente, transp = montar([ErroTransporte("x"), resp(200, {"id": PAGINA})])
    with pytest.raises(ErroNotionIncerto):
        criar_post(cliente, BANCO, AlteracoesPost(titulo="X"))
    assert len(transp.chamadas) == 1


def test_criar_post_429_depois_sucesso_repete_sem_duplicar() -> None:
    cliente, transp = montar(
        [RespostaHttp(429, {"Retry-After": "1"}, b""), resp(200, {"id": PAGINA})]
    )
    assert criar_post(cliente, BANCO, AlteracoesPost(titulo="X")) == PAGINA
    assert [c.metodo for c in transp.chamadas] == ["POST", "POST"]
    # a 429 nao foi processada: o mesmo corpo e reenviado uma vez, resultando em uma pagina
    assert transp.chamadas[0].corpo == transp.chamadas[1].corpo


def test_anexo_de_blocos_erro_de_transporte_nao_repete() -> None:
    cliente, transp = montar([resp(200, {"id": PAGINA}), ErroTransporte("x"), resp(200, vazio())])
    corpo = "\n".join(f"l{i}" for i in range(130))
    with pytest.raises(ErroNotionIncerto):
        criar_post(cliente, BANCO, AlteracoesPost(titulo="Muitos", corpo=corpo))
    assert [c.metodo for c in transp.chamadas] == ["POST", "PATCH"]


def test_anexo_de_blocos_5xx_nao_repete_na_edicao_e_nao_apaga_antigos() -> None:
    blocos = {
        "results": [{"id": "11111111111111111111111111111111", "type": "paragraph"}],
        "has_more": False,
    }
    cliente, transp = montar(
        [resp(200, pagina()), resp(200, blocos), resp(502, {}), resp(200, vazio())]
    )
    with pytest.raises(ErroNotionIncerto):
        editar_post(cliente, PAGINA, AlteracoesPost(corpo="Novo"), EDITADO_DT)
    assert [c.metodo for c in transp.chamadas] == ["GET", "GET", "PATCH"]


def test_anexo_de_blocos_429_repete() -> None:
    cliente, transp = montar(
        [
            resp(200, {"id": PAGINA}),
            RespostaHttp(429, {"Retry-After": "1"}, b""),
            resp(200, vazio()),
        ]
    )
    corpo = "\n".join(f"l{i}" for i in range(130))
    criar_post(cliente, BANCO, AlteracoesPost(titulo="Muitos", corpo=corpo))
    assert [c.metodo for c in transp.chamadas] == ["POST", "PATCH", "PATCH"]


# ------------------------------------------------------------------ editar_post


def test_editar_com_last_edited_igual_envia_patch() -> None:
    cliente, transp = montar([resp(200, pagina()), resp(200, pagina())])
    editar_post(cliente, PAGINA, AlteracoesPost(titulo="Novo", serie_id=None), EDITADO_DT)
    assert [c.metodo for c in transp.chamadas] == ["GET", "PATCH"]
    assert transp.chamadas[0].url.endswith(f"/pages/{PAGINA}")
    patch = transp.chamadas[1]
    assert patch.url.endswith(f"/pages/{PAGINA}")
    assert patch.json == {
        "properties": {
            "Título": {"title": [{"type": "text", "text": {"content": "Novo"}}]},
            "Série": {"relation": []},
        }
    }


def test_editar_aceita_mesmo_instante_em_outro_fuso() -> None:
    cliente, transp = montar([resp(200, pagina()), resp(200, pagina())])
    esperado = EDITADO_DT.astimezone(SP)
    editar_post(cliente, PAGINA, AlteracoesPost(titulo="Novo"), esperado)
    assert [c.metodo for c in transp.chamadas] == ["GET", "PATCH"]


def test_editar_com_last_edited_diferente_nao_escreve() -> None:
    cliente, transp = montar([resp(200, pagina("2026-11-07T12:05:00.000Z"))])
    with pytest.raises(EdicaoConcorrente) as info:
        editar_post(cliente, PAGINA, AlteracoesPost(titulo="Novo"), EDITADO_DT)
    assert "editad" in str(info.value).lower()
    assert [c.metodo for c in transp.chamadas] == ["GET"]


def test_editar_sem_last_edited_na_resposta_nao_escreve() -> None:
    cliente, transp = montar([resp(200, {"id": PAGINA})])
    with pytest.raises(EdicaoConcorrente):
        editar_post(cliente, PAGINA, AlteracoesPost(titulo="Novo"), EDITADO_DT)
    assert [c.metodo for c in transp.chamadas] == ["GET"]


def test_editar_esperado_ingenuo_recusado() -> None:
    cliente, transp = montar([])
    with pytest.raises(ValueError, match="fuso"):
        editar_post(cliente, PAGINA, AlteracoesPost(titulo="N"), datetime(2026, 11, 7, 12))
    assert transp.chamadas == []


def test_editar_nao_toca_no_corpo_sem_corpo_informado() -> None:
    cliente, transp = montar([resp(200, pagina()), resp(200, pagina())])
    editar_post(cliente, PAGINA, AlteracoesPost(titulo="N"), EDITADO_DT)
    assert all("/blocks" not in c.url for c in transp.chamadas)


def test_editar_sem_alteracoes_nao_envia_patch() -> None:
    cliente, transp = montar([resp(200, pagina())])
    editar_post(cliente, PAGINA, AlteracoesPost(), EDITADO_DT)
    assert [c.metodo for c in transp.chamadas] == ["GET"]


def test_editar_corpo_apaga_blocos_de_texto_e_cria_novos() -> None:
    blocos = {
        "results": [
            {"id": "11111111111111111111111111111111", "type": "paragraph", "paragraph": {}},
            {"id": "22222222222222222222222222222222", "type": "image", "image": {}},
            {"id": "33333333333333333333333333333333", "type": "heading_2", "heading_2": {}},
        ],
        "has_more": False,
    }
    cliente, transp = montar(
        [
            resp(200, pagina()),
            resp(200, blocos),
            resp(200, vazio()),
            resp(200, {}),
            resp(200, {}),
        ]
    )
    editar_post(cliente, PAGINA, AlteracoesPost(corpo="Novo texto"), EDITADO_DT)
    resumo = [(c.metodo, c.url.split("/v1")[1]) for c in transp.chamadas]
    assert resumo == [
        ("GET", f"/pages/{PAGINA}"),
        ("GET", f"/blocks/{PAGINA}/children?page_size=100"),
        ("PATCH", f"/blocks/{PAGINA}/children"),
        ("DELETE", "/blocks/11111111111111111111111111111111"),
        ("DELETE", "/blocks/33333333333333333333333333333333"),
    ]
    assert transp.chamadas[2].json["children"][0]["paragraph"]["rich_text"][0]["text"] == {
        "content": "Novo texto"
    }


def test_patch_de_propriedades_com_5xx_continua_repetindo() -> None:
    cliente, transp = montar([resp(200, pagina()), resp(500, {}), resp(200, pagina())])
    editar_post(cliente, PAGINA, AlteracoesPost(titulo="N"), EDITADO_DT)
    assert [c.metodo for c in transp.chamadas] == ["GET", "PATCH", "PATCH"]


def test_apagar_bloco_com_5xx_continua_repetindo() -> None:
    blocos = {
        "results": [{"id": "11111111111111111111111111111111", "type": "paragraph"}],
        "has_more": False,
    }
    cliente, transp = montar(
        [
            resp(200, pagina()),
            resp(200, blocos),
            resp(200, vazio()),
            resp(500, {}),
            resp(200, {}),
        ]
    )
    editar_post(cliente, PAGINA, AlteracoesPost(corpo="Novo"), EDITADO_DT)
    assert [c.metodo for c in transp.chamadas] == ["GET", "GET", "PATCH", "DELETE", "DELETE"]


def test_erro_do_notion_propaga_na_edicao() -> None:
    cliente, transp = montar([resp(200, pagina()), resp(403, {"code": "restricted_resource"})])
    with pytest.raises(ErroNotion) as info:
        editar_post(cliente, PAGINA, AlteracoesPost(titulo="N"), EDITADO_DT)
    assert info.value.status == 403
    assert len(transp.chamadas) == 2


# ------------------------------------------------------------------ ausencia de copia


def _listar(raiz: Path) -> set[str]:
    return {str(p.relative_to(raiz)) for p in raiz.rglob("*")}


def test_ausencia_de_copia_persistente(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    antes = _listar(tmp_path)
    titulo = "TITULO_SECRETO_DO_POST"
    corpo = "CORPO_SECRETO_DO_POST " * 200
    cliente, _ = montar(
        [
            resp(200, {"id": PAGINA}),
            resp(200, pagina()),
            resp(200, pagina()),
            resp(200, vazio()),
            resp(200, vazio()),
        ]
    )
    with caplog.at_level(logging.DEBUG):
        criar_post(cliente, BANCO, AlteracoesPost(titulo=titulo, corpo=corpo))
        editar_post(
            cliente,
            PAGINA,
            AlteracoesPost(titulo=titulo + "2", corpo=corpo + "novo", artes=("a.png",)),
            EDITADO_DT,
        )
    assert _listar(tmp_path) == antes
    assert Path.cwd() == tmp_path
    texto_log = caplog.text + "".join(r.getMessage() for r in caplog.records)
    assert "TITULO_SECRETO" not in texto_log
    assert "CORPO_SECRETO" not in texto_log
    assert "a.png" not in texto_log
