"""Testes do cliente da API do Notion (publicador.notion.cliente) - regra I-1.

Todos os valores aqui sao FICTICIOS. O transporte e falso: nenhuma requisicao real.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field

import pytest

from publicador.core.redacao import SegredoStr, limpar_segredos_registrados
from publicador.notion.cliente import (
    ClienteNotion,
    ErroNotion,
    ErroNotionPermanente,
    ErroNotionTransitorio,
    ErroTransporte,
    RespostaHttp,
    Transporte,
)

TOKEN = "TOKEN_FALSO_abc123XYZ789"
ID_PAGINA = "0123456789abcdef0123456789abcdef"
ID_UUID = "01234567-89ab-cdef-0123-456789abcdef"
ECO = "token invalido TOKEN_FALSO_abc123XYZ789 rejeitado"


@pytest.fixture(autouse=True)
def _limpar() -> Iterator[None]:
    limpar_segredos_registrados()
    yield
    limpar_segredos_registrados()


@dataclass
class Chamada:
    metodo: str
    url: str
    cabecalhos: Mapping[str, str]
    corpo: bytes | None
    timeout: float


@dataclass
class TransporteFalso:
    """Transporte programavel: cada ``enviar`` consome o proximo item da fila."""

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
        self.chamadas.append(Chamada(metodo, url, dict(cabecalhos), corpo, timeout))
        item = self.fila.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class RelogioFalso:
    def __init__(self) -> None:
        self.agora = 100.0
        self.sonos: list[float] = []

    def relogio(self) -> float:
        return self.agora

    def dormir(self, segundos: float) -> None:
        self.sonos.append(segundos)
        self.agora += segundos


def resp(status: int, dados: object | None = None, **cab: str) -> RespostaHttp:
    corpo = b"" if dados is None else json.dumps(dados).encode()
    return RespostaHttp(status=status, cabecalhos=dict(cab), corpo=corpo)


def montar(
    fila: list[RespostaHttp | Exception],
) -> tuple[ClienteNotion, TransporteFalso, RelogioFalso]:
    transporte = TransporteFalso(fila)
    rel = RelogioFalso()
    cliente = ClienteNotion(
        SegredoStr(TOKEN), transporte=transporte, dormir=rel.dormir, relogio=rel.relogio
    )
    # Garante que o proprio codigo nao vaza: sem a camada de segredos registrados.
    limpar_segredos_registrados()
    return cliente, transporte, rel


def test_transporte_falso_satisfaz_protocolo() -> None:
    transporte: Transporte = TransporteFalso([])
    assert transporte is not None


def test_cabecalhos_e_versao_da_api() -> None:
    cliente, transp, _ = montar([resp(200, {"id": ID_PAGINA})])
    assert cliente.obter_pagina(ID_PAGINA) == {"id": ID_PAGINA}
    c = transp.chamadas[0]
    assert c.metodo == "GET"
    assert c.url == f"https://api.notion.com/v1/pages/{ID_PAGINA}"
    assert c.cabecalhos["Notion-Version"] == "2022-06-28"
    assert c.cabecalhos["Authorization"] == f"Bearer {TOKEN}"
    assert c.timeout > 0


def test_versao_api_personalizada() -> None:
    transp = TransporteFalso([resp(200, {})])
    cliente = ClienteNotion(
        SegredoStr(TOKEN), transporte=transp, versao_api="2099-01-01", dormir=lambda _: None
    )
    cliente.obter_pagina(ID_PAGINA)
    assert transp.chamadas[0].cabecalhos["Notion-Version"] == "2099-01-01"


def test_repr_do_cliente_nao_mostra_token() -> None:
    cliente, _, _ = montar([])
    assert TOKEN not in repr(cliente)
    assert TOKEN not in str(cliente)
    assert TOKEN not in repr(vars(cliente) if hasattr(cliente, "__dict__") else "")
    assert TOKEN not in repr(getattr(cliente, "__slots__", ""))


def test_corpo_json_enviado() -> None:
    cliente, transp, _ = montar([resp(200, {"id": ID_PAGINA})])
    cliente.atualizar_pagina(ID_PAGINA, {"Status": {"select": {"name": "Publicado"}}})
    c = transp.chamadas[0]
    assert c.metodo == "PATCH"
    assert c.cabecalhos["Content-Type"] == "application/json"
    assert c.corpo is not None
    assert json.loads(c.corpo) == {"properties": {"Status": {"select": {"name": "Publicado"}}}}


def test_criar_pagina() -> None:
    cliente, transp, _ = montar([resp(200, {"id": ID_PAGINA})])
    cliente.criar_pagina({"database_id": ID_UUID}, {"Nome": {"title": []}})
    c = transp.chamadas[0]
    assert (c.metodo, c.url) == ("POST", "https://api.notion.com/v1/pages")
    assert c.corpo is not None
    assert json.loads(c.corpo)["parent"] == {"database_id": ID_UUID}


def test_429_respeita_retry_after() -> None:
    cliente, transp, rel = montar(
        [resp(429, {"code": "rate_limited"}, **{"Retry-After": "2"}), resp(200, {"ok": True})]
    )
    assert cliente.requisitar("GET", f"/pages/{ID_PAGINA}") == {"ok": True}
    assert rel.sonos == [2.0]
    assert len(transp.chamadas) == 2


def test_retry_after_minusculo_e_invalido_cai_no_backoff() -> None:
    cliente, _, rel = montar(
        [
            resp(429, None, **{"retry-after": "3"}),
            resp(429, None, **{"Retry-After": "abc"}),
            resp(200, {}),
        ]
    )
    cliente.requisitar("GET", f"/pages/{ID_PAGINA}")
    assert rel.sonos == [3.0, 2.0]  # segunda tentativa: backoff 1, 2


def test_5xx_repetido_e_depois_sucesso() -> None:
    cliente, transp, rel = montar([resp(500), resp(502), resp(200, {"ok": 1})])
    assert cliente.requisitar("GET", f"/pages/{ID_PAGINA}") == {"ok": 1}
    assert rel.sonos == [1.0, 2.0]
    assert len(transp.chamadas) == 3


def test_5xx_esgotando_tentativas() -> None:
    cliente, transp, rel = montar([resp(500, {"code": "internal_server_error"})] * 5)
    with pytest.raises(ErroNotionTransitorio) as info:
        cliente.requisitar("GET", f"/pages/{ID_PAGINA}")
    assert len(transp.chamadas) == 5
    assert rel.sonos == [1.0, 2.0, 4.0, 8.0]
    assert info.value.status == 500
    assert info.value.codigo == "internal_server_error"
    assert str(info.value) == "Falha ao consultar o Notion (status 500)"


def test_queda_de_transporte_repete_e_esgota() -> None:
    cliente, transp, rel = montar([ErroTransporte("Falha de transporte")] * 5)
    with pytest.raises(ErroNotionTransitorio) as info:
        cliente.requisitar("GET", f"/pages/{ID_PAGINA}")
    assert info.value.status is None
    assert len(transp.chamadas) == 5
    assert rel.sonos == [1.0, 2.0, 4.0, 8.0]


def test_queda_de_transporte_e_depois_sucesso() -> None:
    cliente, _, rel = montar([ErroTransporte("x"), resp(200, {"ok": 1})])
    assert cliente.requisitar("GET", f"/pages/{ID_PAGINA}") == {"ok": 1}
    assert rel.sonos == [1.0]


def test_429_esgotando_tentativas_e_transitorio() -> None:
    cliente, _, _ = montar([resp(429, None, **{"Retry-After": "1"})] * 5)
    with pytest.raises(ErroNotionTransitorio) as info:
        cliente.requisitar("GET", f"/pages/{ID_PAGINA}")
    assert info.value.status == 429


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409])
def test_4xx_e_permanente_sem_repeticao(status: int) -> None:
    cliente, transp, rel = montar([resp(status, {"code": "object_not_found", "message": ECO})])
    with pytest.raises(ErroNotionPermanente) as info:
        cliente.requisitar("GET", f"/pages/{ID_PAGINA}")
    assert len(transp.chamadas) == 1
    assert rel.sonos == []
    assert info.value.status == status
    assert info.value.codigo == "object_not_found"
    assert str(info.value) == f"Falha ao consultar o Notion (status {status})"


def test_codigo_invalido_e_descartado() -> None:
    for codigo in ("tem espaco", "x" * 200, 123, "TOKEN_FALSO_abc123XYZ789", "a<b>"):
        cliente, _, _ = montar([resp(400, {"code": codigo})])
        with pytest.raises(ErroNotion) as info:
            cliente.requisitar("GET", f"/pages/{ID_PAGINA}")
        assert info.value.codigo is None


def test_corpo_nao_json_vira_erro_tratavel() -> None:
    cliente, _, _ = montar([RespostaHttp(400, {}, b"<html>" + ECO.encode())])
    with pytest.raises(ErroNotionPermanente) as info:
        cliente.requisitar("GET", f"/pages/{ID_PAGINA}")
    assert info.value.codigo is None


def test_sucesso_com_corpo_invalido_e_erro_permanente() -> None:
    for corpo in (b"nao json", b"[1, 2]", b""):
        cliente, _, _ = montar([RespostaHttp(200, {}, corpo)])
        with pytest.raises(ErroNotionPermanente):
            cliente.requisitar("GET", f"/pages/{ID_PAGINA}")


def test_paginacao_consultar_banco_tres_paginas() -> None:
    cliente, transp, _ = montar(
        [
            resp(200, {"results": [{"id": "a"}], "has_more": True, "next_cursor": "c1"}),
            resp(200, {"results": [{"id": "b"}], "has_more": True, "next_cursor": "c2"}),
            resp(200, {"results": [{"id": "c"}], "has_more": False, "next_cursor": None}),
        ]
    )
    filtro = {"property": "Status", "select": {"equals": "Agendado"}}
    itens = cliente.consultar_banco(ID_PAGINA, filtro)
    assert [i["id"] for i in itens] == ["a", "b", "c"]
    assert len(transp.chamadas) == 3
    corpos = [json.loads(c.corpo or b"{}") for c in transp.chamadas]
    assert all(c.metodo == "POST" for c in transp.chamadas)
    assert transp.chamadas[0].url.endswith(f"/databases/{ID_PAGINA}/query")
    assert "start_cursor" not in corpos[0]
    assert corpos[1]["start_cursor"] == "c1"
    assert corpos[2]["start_cursor"] == "c2"
    assert all(c["filter"] == filtro for c in corpos)


def test_paginacao_sem_cursor_nao_entra_em_laco() -> None:
    cliente, _, _ = montar([resp(200, {"results": [], "has_more": True, "next_cursor": None})])
    with pytest.raises(ErroNotionPermanente):
        cliente.consultar_banco(ID_PAGINA)


def test_blocos_filhos_paginados_com_cursor_codificado() -> None:
    cliente, transp, _ = montar(
        [
            resp(200, {"results": [{"id": "1"}], "has_more": True, "next_cursor": "a b&c=d"}),
            resp(200, {"results": [{"id": "2"}], "has_more": False, "next_cursor": None}),
        ]
    )
    blocos = cliente.blocos_filhos(ID_UUID)
    assert [b["id"] for b in blocos] == ["1", "2"]
    assert transp.chamadas[0].metodo == "GET"
    assert f"/blocks/{ID_UUID}/children?page_size=100" in transp.chamadas[0].url
    assert "start_cursor=a%20b%26c%3Dd" in transp.chamadas[1].url


@pytest.mark.parametrize(
    "ruim",
    [
        "",
        "abc",
        "../users",
        ID_PAGINA + "/x",
        ID_PAGINA + "?a=1",
        "g" * 32,
        ID_UUID + "0",
        " " + ID_PAGINA,
    ],
)
def test_id_invalido_recusado_antes_de_enviar(ruim: str) -> None:
    cliente, transp, _ = montar([])
    for chamada in (
        lambda: cliente.obter_pagina(ruim),
        lambda: cliente.atualizar_pagina(ruim, {}),
        lambda: cliente.consultar_banco(ruim),
        lambda: cliente.blocos_filhos(ruim),
    ):
        with pytest.raises(ValueError):
            chamada()
    assert transp.chamadas == []


@pytest.mark.parametrize("caminho", ["pages/x", "/pages/../users", "/pages?x=1", "//evil", "/a b"])
def test_caminho_invalido_recusado(caminho: str) -> None:
    cliente, transp, _ = montar([])
    with pytest.raises(ValueError):
        cliente.requisitar("GET", caminho)
    assert transp.chamadas == []


def test_metodo_invalido_recusado() -> None:
    cliente, _, _ = montar([])
    with pytest.raises(ValueError):
        cliente.requisitar("TRACE", "/pages")


def test_limite_de_taxa_entre_requisicoes() -> None:
    cliente, transp, rel = montar([resp(200, {})] * 3)
    for _ in range(3):
        cliente.requisitar("GET", f"/pages/{ID_PAGINA}")
    assert rel.sonos == pytest.approx([0.34, 0.34])
    assert len(transp.chamadas) == 3


def test_sem_espera_quando_ja_passou_tempo() -> None:
    cliente, _, rel = montar([resp(200, {})] * 2)
    cliente.requisitar("GET", f"/pages/{ID_PAGINA}")
    rel.agora += 5
    cliente.requisitar("GET", f"/pages/{ID_PAGINA}")
    assert rel.sonos == []


def _percorrer(erro: BaseException | None) -> Iterator[BaseException]:
    vistos: set[int] = set()
    while erro is not None and id(erro) not in vistos:
        vistos.add(id(erro))
        yield erro
        erro = erro.__cause__ or erro.__context__


def _sem_segredo(erro: BaseException) -> None:
    for e in _percorrer(erro):
        textos = [str(e), repr(e), *[repr(a) for a in e.args]]
        for texto in textos:
            assert TOKEN not in texto
            assert "TOKEN_FALSO" not in texto
            assert "rejeitado" not in texto


@pytest.mark.parametrize(
    ("fila", "esperado"),
    [
        ([resp(401, {"code": "unauthorized", "message": ECO})], ErroNotionPermanente),
        ([resp(500, {"code": "internal", "message": ECO})] * 5, ErroNotionTransitorio),
        ([RespostaHttp(502, {"x-eco": ECO}, ECO.encode())] * 5, ErroNotionTransitorio),
        ([ErroTransporte(f"falha {ECO}")] * 5, ErroNotionTransitorio),
    ],
)
def test_token_nunca_vaza_em_erros_nem_logs(
    caplog: pytest.LogCaptureFixture,
    fila: list[RespostaHttp | Exception],
    esperado: type[ErroNotion],
) -> None:
    cliente, _, _ = montar(list(fila))
    with caplog.at_level(logging.DEBUG, logger="publicador"):
        with pytest.raises(esperado) as info:
            cliente.requisitar("GET", f"/pages/{ID_PAGINA}")
    _sem_segredo(info.value)
    assert TOKEN not in repr(cliente)
    saida = caplog.text + "".join(r.getMessage() for r in caplog.records)
    assert TOKEN not in saida
    assert "TOKEN_FALSO" not in saida
    assert "rejeitado" not in saida
    assert "Bearer" not in saida
    assert "Authorization" not in saida


def test_logs_de_repeticao_existem_e_sao_seguros(caplog: pytest.LogCaptureFixture) -> None:
    cliente, _, _ = montar([resp(500, {"message": ECO}), resp(200, {})])
    with caplog.at_level(logging.DEBUG, logger="publicador"):
        cliente.requisitar("GET", f"/pages/{ID_PAGINA}")
    assert any(r.name.startswith("publicador") for r in caplog.records)
    assert "rejeitado" not in caplog.text
