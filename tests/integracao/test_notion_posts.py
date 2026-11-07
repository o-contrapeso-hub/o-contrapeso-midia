"""Integração com o Notion REAL (workspace de teste). Pulada sem NOTION_TOKEN_TESTE.

Regras: o token só vem do ambiente e é embrulhado em SegredoStr; nada de conteúdo de
página é impresso; só são arquivadas as páginas criadas por esta execução.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from publicador.core.modelos import PapelEstagio
from publicador.core.redacao import SegredoStr
from publicador.notion.bancos import Configuracao, IdsBancos, ler_configuracao
from publicador.notion.cliente import ClienteNotion
from publicador.notion.escrita import AlteracoesPost, EdicaoConcorrente, criar_post, editar_post
from publicador.notion.mapeamento import ler_post
from publicador.notion.variacoes import (
    artes_por_plataforma,
    blocos_de_conteudo,
    conteudo_da_plataforma,
)

from . import ids

pytestmark = [
    pytest.mark.integracao,
    pytest.mark.skipif(
        not os.environ.get("NOTION_TOKEN_TESTE"), reason="NOTION_TOKEN_TESTE não definido"
    ),
]


class _Ambiente:
    def __init__(self, cliente: ClienteNotion, config: Configuracao, rotulo: str) -> None:
        self.cliente = cliente
        self.config = config
        self.rotulo = rotulo
        self.criados: list[str] = []  # SOMENTE páginas criadas por esta execução

    def titulo(self, nome: str) -> str:
        return f"{self.rotulo} {nome}"

    def estagio_id(self, papel: PapelEstagio) -> str:
        for pagina_id, estagio in self.config.estagios.items():
            if estagio.papel is papel:
                return pagina_id
        raise AssertionError(f"estágio com papel {papel.value} não encontrado")

    def criar(self, alteracoes: AlteracoesPost) -> str:
        pagina_id = criar_post(self.cliente, ids.BANCO_POSTS, alteracoes)
        self.criados.append(pagina_id)
        return pagina_id


def _ids_bancos() -> IdsBancos:
    return IdsBancos(
        estagios=ids.BANCO_ESTAGIOS,
        formatos=ids.BANCO_FORMATOS,
        series=ids.BANCO_SERIES,
        temas=ids.BANCO_TEMAS,
    )


@pytest.fixture
def ambiente() -> Iterator[_Ambiente]:
    token = SegredoStr(os.environ["NOTION_TOKEN_TESTE"])  # I-1: embrulha de imediato
    cliente = ClienteNotion(token)
    config = ler_configuracao(cliente, _ids_bancos())
    carimbo = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    amb = _Ambiente(cliente, config, f"[teste-integracao {carimbo}]")
    try:
        yield amb
    finally:
        falhas = 0
        for pagina_id in amb.criados:
            try:
                cliente.requisitar("PATCH", f"/pages/{pagina_id}", {"archived": True})
            except Exception:  # noqa: BLE001 - limpeza tenta todas; sem expor detalhes
                falhas += 1
        if falhas:
            pytest.fail(f"limpeza: {falhas} página(s) de teste não arquivadas", pytrace=False)


def test_configuracao_do_workspace_de_teste(ambiente: _Ambiente) -> None:
    config = ambiente.config
    assert len(config.estagios) == 6, "esperados 6 estágios"
    autorizados = [e for e in config.estagios.values() if e.papel is PapelEstagio.AUTORIZADO]
    assert len(autorizados) == 1, "esperado exatamente um estágio autorizador"
    assert autorizados[0].nome == "confirmado"
    assert config.problemas == (), f"configuração com {len(config.problemas)} problema(s)"


def test_pauta_edicao_externa_e_edicao_concorrente(ambiente: _Ambiente) -> None:
    titulo = ambiente.titulo("pauta")
    pagina_id = ambiente.criar(
        AlteracoesPost(titulo=titulo, estagio_id=ambiente.estagio_id(PapelEstagio.PAUTA))
    )
    post = ler_post(ambiente.cliente, pagina_id, ambiente.config)
    assert post.titulo == titulo
    assert post.estagio.papel is PapelEstagio.PAUTA
    assert post.publicar_em is None
    assert post.artes == ()
    assert post.editado_em is not None

    # Simula alguém editando direto no Notion.
    novo_titulo = ambiente.titulo("pauta editada fora")
    ambiente.cliente.requisitar(
        "PATCH",
        f"/pages/{pagina_id}",
        {"properties": {"Título": {"title": [{"type": "text", "text": {"content": novo_titulo}}]}}},
    )
    relido = ler_post(ambiente.cliente, pagina_id, ambiente.config)
    assert relido.titulo == novo_titulo, "edição direta não apareceu na releitura"

    # Versão desatualizada (o Notion arredonda ao minuto; 1h atrás é inequivocamente velha).
    desatualizado = post.editado_em - timedelta(hours=1)
    with pytest.raises(EdicaoConcorrente):
        editar_post(
            ambiente.cliente,
            pagina_id,
            AlteracoesPost(titulo=ambiente.titulo("nao deve gravar")),
            editado_em_esperado=desatualizado,
        )
    depois = ler_post(ambiente.cliente, pagina_id, ambiente.config)
    assert depois.titulo == novo_titulo, "página foi alterada apesar da edição concorrente"


def test_post_completo_ida_e_volta(ambiente: _Ambiente) -> None:
    plataformas = ("Instagram", "TikTok")
    artes = ("capa.png", "Instagram:ig/slide1.png", "TikTok:tt/video.mp4")
    corpo = "Texto comum do post de teste."
    secoes = {
        "Instagram": "Legenda específica do Instagram.",
        "TikTok": "Legenda específica do TikTok.",
    }
    pagina_id = ambiente.criar(
        AlteracoesPost(
            titulo=ambiente.titulo("completo"),
            estagio_id=ambiente.estagio_id(PapelEstagio.PRONTO),
            plataformas=plataformas,
            artes=artes,
        )
    )
    blocos = blocos_de_conteudo(corpo, secoes)
    ambiente.cliente.requisitar("PATCH", f"/blocks/{pagina_id}/children", {"children": blocos})

    post = ler_post(ambiente.cliente, pagina_id, ambiente.config)
    assert set(post.plataformas) == set(plataformas)
    assert post.estagio.papel is not PapelEstagio.AUTORIZADO
    assert post.selo is None
    assert post.confirmado_em is None

    por_plataforma = artes_por_plataforma(post)
    assert por_plataforma["Instagram"] == ("capa.png", "ig/slide1.png")
    assert por_plataforma["TikTok"] == ("capa.png", "tt/video.mp4")

    assert conteudo_da_plataforma(post, "Instagram") == secoes["Instagram"]
    assert conteudo_da_plataforma(post, "TikTok") == secoes["TikTok"]
    assert post.corpo.strip() == corpo
