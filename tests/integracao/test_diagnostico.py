"""Diagnóstico TEMPORÁRIO de acesso (só códigos de status e sim/não; nunca títulos nem conteúdo)."""

import os

import pytest

from publicador.core.redacao import SegredoStr
from publicador.notion.cliente import ClienteNotion, ErroNotion

from . import ids

pytestmark = [
    pytest.mark.integracao,
    pytest.mark.skipif(
        not os.environ.get("NOTION_TOKEN_TESTE"), reason="NOTION_TOKEN_TESTE não definido"
    ),
]

PAGINA_TESTE = "4119dfaa9e4b81fba503f92cefdc5567"
PAGINA_PROJETO = "4119dfaa9e4b81e4b8d0c0ef5e657498"


def _status(cliente: ClienteNotion, metodo: str, caminho: str, corpo: dict | None = None) -> str:
    try:
        cliente.requisitar(metodo, caminho, corpo)
        return "200"
    except ErroNotion as erro:
        return str(erro.status)


def test_diagnostico_de_acesso() -> None:
    cliente = ClienteNotion(SegredoStr(os.environ["NOTION_TOKEN_TESTE"]))
    linhas = {
        "token valido (GET /users/me)": _status(cliente, "GET", "/users/me"),
        "pagina Projeto O Publicador": _status(cliente, "GET", f"/pages/{PAGINA_PROJETO}"),
        "pagina Publicador Teste": _status(cliente, "GET", f"/pages/{PAGINA_TESTE}"),
        "GET banco Estagios": _status(cliente, "GET", f"/databases/{ids.BANCO_ESTAGIOS}"),
        "GET banco Posts": _status(cliente, "GET", f"/databases/{ids.BANCO_POSTS}"),
        "POST query Estagios": _status(
            cliente, "POST", f"/databases/{ids.BANCO_ESTAGIOS}/query", {"page_size": 1}
        ),
    }
    try:
        busca = cliente.requisitar("POST", "/search", {"query": "Publicador", "page_size": 100})
        resultados = busca.get("results", [])
        linhas["search: total de resultados visiveis"] = str(len(resultados))
        achados = {str(r.get("id", "")).replace("-", "") for r in resultados}
        linhas["search: enxerga Publicador Teste"] = str(PAGINA_TESTE in achados)
        linhas["search: enxerga banco Estagios"] = str(ids.BANCO_ESTAGIOS in achados)
    except ErroNotion as erro:
        linhas["search"] = str(erro.status)
    pytest.fail("DIAGNOSTICO " + "; ".join(f"{k}={v}" for k, v in linhas.items()), pytrace=False)
