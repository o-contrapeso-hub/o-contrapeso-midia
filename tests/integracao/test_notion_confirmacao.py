"""Integração (T025; RF-08, RF-09, RF-26): confirmação, selo e reverificação no Notion REAL.

Pulada sem NOTION_TOKEN_TESTE. O token só vem do ambiente e é embrulhado em SegredoStr;
nada de conteúdo de página é impresso. Cada teste cria o seu post (título único ``[T025]``)
no banco Posts de TESTE e o arquiva no final, mesmo se falhar. Os testes são independentes.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from publicador.artes.armazem import ArmazemLocal
from publicador.core.confirmacao import (
    ConfirmacaoRecusada,
    MotivoConfirmacao,
    confirmar_post,
)
from publicador.core.modelos import PapelEstagio, Post
from publicador.core.redacao import SegredoStr
from publicador.core.reverificacao import SituacaoReverificacao, reverificar_post
from publicador.core.selo import calcular_selo, verificar_selo
from publicador.job.vigia import Desfecho, varrer
from publicador.notion.bancos import Configuracao, IdsBancos, ler_configuracao
from publicador.notion.cliente import ClienteNotion
from publicador.notion.escrita import AlteracoesPost, criar_post, propriedades_notion
from publicador.notion.mapeamento import ler_post

from . import ids

pytestmark = [
    pytest.mark.integracao,
    pytest.mark.skipif(
        not os.environ.get("NOTION_TOKEN_TESTE"), reason="NOTION_TOKEN_TESTE não definido"
    ),
]

SAO_PAULO = ZoneInfo("America/Sao_Paulo")
# Data/hora futura fixa (a confirmação não exige que seja futura, mas mantém o sentido real).
DATA_FUTURA = datetime(2030, 1, 15, 10, 0, tzinfo=SAO_PAULO)
OUTRA_DATA_FUTURA = datetime(2030, 1, 16, 11, 30, tzinfo=SAO_PAULO)
PLATAFORMAS = ("Instagram",)


class _Ambiente:
    def __init__(
        self, cliente: ClienteNotion, config: Configuracao, armazem: ArmazemLocal, raiz: Path
    ) -> None:
        self.cliente = cliente
        self.config = config
        self.armazem = armazem
        self.raiz = raiz
        self.criados: list[str] = []  # SOMENTE páginas criadas por este teste

    def titulo(self, nome: str) -> str:
        return f"[T025] {nome} {uuid.uuid4().hex[:8]}"

    def estagio_id(self, papel: PapelEstagio) -> str:
        for pagina_id, estagio in self.config.estagios.items():
            if estagio.papel is papel:
                return pagina_id
        raise AssertionError(f"estágio com papel {papel.value} não encontrado")

    def criar_arte(self) -> str:
        """Cria uma arte de verdade no armazém local e devolve a referência (caminho)."""
        caminho = f"t025-{uuid.uuid4().hex[:8]}.png"
        (self.raiz / caminho).write_bytes(b"\x89PNG\r\n\x1a\n" + uuid.uuid4().bytes)
        return caminho

    def criar(self, alteracoes: AlteracoesPost) -> str:
        pagina_id = criar_post(self.cliente, ids.BANCO_POSTS, alteracoes)
        self.criados.append(pagina_id)
        return pagina_id

    def criar_pronto(
        self, nome: str, *, publicar_em: datetime | None = DATA_FUTURA, com_arte: bool = True
    ) -> str:
        """Post completo em ``pronto`` (título, corpo, plataforma, data/hora e arte)."""
        artes = (self.criar_arte(),) if com_arte else ()
        return self.criar(
            AlteracoesPost(
                titulo=self.titulo(nome),
                estagio_id=self.estagio_id(PapelEstagio.PRONTO),
                plataformas=PLATAFORMAS,
                publicar_em=publicar_em,
                artes=artes,
                corpo="Texto original do post de teste T025.",
            )
        )

    def reler(self, pagina_id: str) -> Post:
        return ler_post(self.cliente, pagina_id, self.config)

    def atualizar(self, pagina_id: str, propriedades: dict[str, Any]) -> None:
        self.cliente.atualizar_pagina(pagina_id, propriedades)


def _ids_bancos() -> IdsBancos:
    return IdsBancos(
        estagios=ids.BANCO_ESTAGIOS,
        formatos=ids.BANCO_FORMATOS,
        series=ids.BANCO_SERIES,
        temas=ids.BANCO_TEMAS,
    )


@pytest.fixture
def ambiente(tmp_path: Path) -> Iterator[_Ambiente]:
    token = SegredoStr(os.environ["NOTION_TOKEN_TESTE"])  # I-1: embrulha de imediato
    cliente = ClienteNotion(token)
    config = ler_configuracao(cliente, _ids_bancos())
    raiz = tmp_path / "artes"
    raiz.mkdir()
    amb = _Ambiente(cliente, config, ArmazemLocal(raiz), raiz)
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


def _exigir_intacto(post: Post, papel: PapelEstagio) -> None:
    assert post.estagio.papel is papel
    assert post.selo is None
    assert post.confirmado_em is None


def test_confirmar_sem_data_e_recusado(ambiente: _Ambiente) -> None:
    # Tem título e plataforma (a regra checa plataformas antes da data); falta só a data.
    pagina_id = ambiente.criar(
        AlteracoesPost(
            titulo=ambiente.titulo("sem data"),
            estagio_id=ambiente.estagio_id(PapelEstagio.PAUTA),
            plataformas=PLATAFORMAS,
        )
    )
    post = ambiente.reler(pagina_id)
    assert post.publicar_em is None and not post.somente_data

    with pytest.raises(ConfirmacaoRecusada) as info:
        confirmar_post(ambiente.cliente, post, ambiente.config, ambiente.armazem, datetime.now(UTC))
    assert info.value.motivo is MotivoConfirmacao.SEM_DATA

    _exigir_intacto(ambiente.reler(pagina_id), PapelEstagio.PAUTA)


def test_confirmar_so_com_data_sem_hora_e_recusado(ambiente: _Ambiente) -> None:
    pagina_id = ambiente.criar_pronto("sem hora", publicar_em=None)
    # A escrita do projeto só grava data com hora; o "só dia" vai direto na API.
    ambiente.atualizar(pagina_id, {"Data e hora": {"date": {"start": "2030-01-15"}}})
    post = ambiente.reler(pagina_id)
    assert post.somente_data and post.publicar_em is None

    with pytest.raises(ConfirmacaoRecusada) as info:
        confirmar_post(ambiente.cliente, post, ambiente.config, ambiente.armazem, datetime.now(UTC))
    assert info.value.motivo is MotivoConfirmacao.SEM_HORA

    _exigir_intacto(ambiente.reler(pagina_id), PapelEstagio.PRONTO)


def test_confirmar_com_data_e_hora_gera_selo(ambiente: _Ambiente) -> None:
    pagina_id = ambiente.criar_pronto("confirmar")
    post = ambiente.reler(pagina_id)
    assert post.publicar_em == DATA_FUTURA
    agora = datetime.now(UTC)

    devolvido = confirmar_post(ambiente.cliente, post, ambiente.config, ambiente.armazem, agora)
    assert devolvido.estagio.papel is PapelEstagio.AUTORIZADO
    assert devolvido.estagio.nome == "confirmado"

    relido = ambiente.reler(pagina_id)  # confere o que ficou gravado no Notion
    assert relido.estagio.papel is PapelEstagio.AUTORIZADO
    assert relido.estagio.nome == "confirmado"
    assert relido.selo is not None and relido.selo.startswith("v1:")
    assert relido.selo == devolvido.selo
    assert relido.confirmado_em is not None
    assert abs(relido.confirmado_em - agora) < timedelta(minutes=5)
    assert relido.publicar_em == DATA_FUTURA
    assert verificar_selo(relido, ambiente.armazem, relido.selo) is True


@pytest.mark.parametrize("campo", ["corpo", "data"])
def test_editar_depois_de_confirmar_devolve_a_pronto(ambiente: _Ambiente, campo: str) -> None:
    pagina_id = ambiente.criar_pronto(f"editar {campo}")
    confirmado = confirmar_post(
        ambiente.cliente,
        ambiente.reler(pagina_id),
        ambiente.config,
        ambiente.armazem,
        datetime.now(UTC),
    )
    assert confirmado.estagio.papel is PapelEstagio.AUTORIZADO

    # Simula edição feita direto no Notion depois da confirmação.
    if campo == "corpo":
        bloco = {
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": "Trecho editado depois."}}]
            },
        }
        ambiente.cliente.requisitar(
            "PATCH", f"/blocks/{pagina_id}/children", {"children": [bloco]}, idempotente=False
        )
    else:
        ambiente.atualizar(
            pagina_id, propriedades_notion(AlteracoesPost(publicar_em=OUTRA_DATA_FUTURA))
        )

    editado = ambiente.reler(pagina_id)  # relido do Notion imediatamente antes da operação
    assert editado.estagio.papel is PapelEstagio.AUTORIZADO
    assert verificar_selo(editado, ambiente.armazem, editado.selo) is False

    resultado = reverificar_post(ambiente.cliente, editado, ambiente.config, ambiente.armazem)
    assert resultado.situacao is SituacaoReverificacao.DEVOLVIDO_A_PRONTO

    _exigir_intacto(ambiente.reler(pagina_id), PapelEstagio.PRONTO)


def test_vigia_sela_confirmacao_direta_so_apos_o_intervalo(ambiente: _Ambiente) -> None:
    pagina_id = ambiente.criar_pronto("vigia")
    # Confirmação "pelo Notion": estágio vai a confirmado direto, sem selo.
    autorizador = ambiente.estagio_id(PapelEstagio.AUTORIZADO)
    ambiente.atualizar(pagina_id, {"Estágio": {"relation": [{"id": autorizador}]}})
    post = ambiente.reler(pagina_id)
    assert post.estagio.papel is PapelEstagio.AUTORIZADO
    assert post.selo is None and post.editado_em is not None
    base = post.editado_em  # o vigia compara o relógio com este mesmo instante

    # 4 min sem edição: aguarda, não sela.
    cedo = varrer(
        ambiente.cliente,
        ambiente.config,
        ambiente.armazem,
        lambda: base + timedelta(minutes=4),
        banco_posts_id=ids.BANCO_POSTS,
    )
    assert cedo.falha is None
    assert cedo.ocorrencia(post.id).desfecho is Desfecho.AGUARDANDO
    apos_cedo = ambiente.reler(pagina_id)
    assert apos_cedo.estagio.papel is PapelEstagio.AUTORIZADO
    assert apos_cedo.selo is None
    assert apos_cedo.editado_em == base  # a varredura cedo não escreveu nada

    # 6 min: sela com o selo calculado.
    tarde = varrer(
        ambiente.cliente,
        ambiente.config,
        ambiente.armazem,
        lambda: base + timedelta(minutes=6),
        banco_posts_id=ids.BANCO_POSTS,
    )
    assert tarde.falha is None
    assert tarde.ocorrencia(post.id).desfecho is Desfecho.SELADO

    selado = ambiente.reler(pagina_id)
    assert selado.estagio.papel is PapelEstagio.AUTORIZADO
    assert selado.confirmado_em is not None
    assert selado.selo == calcular_selo(selado, ambiente.armazem)
    assert verificar_selo(selado, ambiente.armazem, selado.selo) is True
