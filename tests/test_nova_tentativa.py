"""Testes da nova tentativa segura (publicador.job.orquestrador) - T040.

RF-12, RF-25, RF-30, RF-38, I-1, I-2. Reaproveita o Notion falso com estado e os adaptadores
espiões dos testes do orquestrador. Sem rede; valores fictícios; relógio injetado.

Cenários: tabela de classificação; nova tentativa antes e depois do intervalo; janela
expirada; consulta ENCONTRADO / NAO_ENCONTRADO / DESCONHECIDO / erro; queda no meio do envio;
duas execuções concorrentes; segunda falha sem terceira tentativa; RF-38 com execuções de datas
diferentes; nada sensível vaza; e uma varredura de combinações (propriedade à mão) mostrando que
nenhum cenário gera mais de uma publicação por post e plataforma.
"""

from __future__ import annotations

import copy
import itertools
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_orquestrador import (
    AGORA,
    ARTE,
    BANCO_EXEC,
    ID_CONFIRMADO,
    ID_FALHA,
    ID_PUBLICADO,
    IDS,
    P1,
    PARAMETROS,
    TEXTO,
    TIPOS,
    TITULO,
    TOKEN,
    TOKEN_REDE,
    AdaptadorEspiao,
    NotionFalso,
    PaginaFalsa,
    _cenario,
    _cliente,
    _config,
    _props,
    _selar,
)

from publicador.adaptadores.base import (
    EstadoConsulta,
    PedidoDePublicacao,
    ResultadoConsulta,
    ResultadoPublicacao,
    TipoDeResultado,
)
from publicador.artes.armazem import ArmazemLocal
from publicador.core.config import Parametros
from publicador.core.modelos import EstadoExecucao
from publicador.job.execucoes import (
    CodigoMotivo,
    chave_de,
    iniciar_execucao,
    registrar_resultado,
)
from publicador.job.orquestrador import Desfecho, Registro, RelatorioExecucao, executar
from publicador.job.pos_publicacao import DesfechoPos

T = TipoDeResultado
INTERVALO = timedelta(minutes=PARAMETROS.nova_tentativa_minutos)
JANELA = timedelta(minutes=PARAMETROS.janela_tolerancia_minutos)
AGENDADO = datetime.fromisoformat("2026-11-07T09:00:00-03:00")  # mesmo instante de ``DATA``


@pytest.fixture
def armazem(tmp_path: Path) -> ArmazemLocal:
    destino = tmp_path / "artes" / ARTE
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_bytes(b"\x89PNG ficticio 1")
    return ArmazemLocal(tmp_path / "artes")


# ------------------------------------------------------------------ plataforma com "verdade"


class Queda(BaseException):
    """O processo do job morre no meio do envio (não é ``Exception``: nada a captura)."""


@dataclass
class Verdade:
    """O que de fato saiu na plataforma, compartilhado entre "processos" (rodadas)."""

    saidas: list[str] = field(default_factory=list)
    envios_reais: int = 0


class AdaptadorVerdade(AdaptadorEspiao):
    """Adaptador falso cuja plataforma tem estado próprio (``Verdade``), como uma rede real.

    ``roteiro``: lista COMPARTILHADA entre instâncias de (resultado, saiu). O resultado é um
    ``TipoDeResultado``, "excecao" (erro inesperado) ou "queda" (o processo morre). ``consulta``:
    "verdade" (ENCONTRADO se saiu), "desconhecido", "erro" ou "nao_encontrado" (para testes).
    """

    def __init__(
        self,
        plataforma: str,
        eventos: list[tuple[str, ...]],
        verdade: Verdade,
        roteiro: list[tuple[Any, bool]],
        consulta: str = "verdade",
        **opcoes: Any,
    ) -> None:
        super().__init__(plataforma, eventos, **opcoes)
        self.verdade = verdade
        self.roteiro = roteiro
        self.modo_consulta = consulta
        self.consultas: list[str] = []

    def publicar(self, pedido: PedidoDePublicacao) -> ResultadoPublicacao:
        if pedido.simular:
            return super().publicar(pedido)
        self._eventos.append(("publicar", self.plataforma, pedido.post.id))
        self.pedidos.append(pedido)
        self.verdade.envios_reais += 1
        tipo, saiu = self.roteiro.pop(0) if self.roteiro else (T.SUCESSO, True)
        if saiu:
            self.verdade.saidas.append(pedido.chave_idempotencia)
        if tipo == "queda":
            raise Queda()
        if tipo == "excecao":
            raise RuntimeError("erro de rede com " + TOKEN_REDE)
        if tipo in (T.SUCESSO, T.SUCESSO_MODO_PRIVADO):
            return ResultadoPublicacao(tipo, pedido.chave_idempotencia, id_externo="17890000000001")
        return ResultadoPublicacao(tipo, pedido.chave_idempotencia, motivo="programado")

    def consultar(self, chave_idempotencia: str) -> ResultadoConsulta:
        self._eventos.append(("consultar", self.plataforma, chave_idempotencia))
        self.consultas.append(chave_idempotencia)
        modo = self.modo_consulta
        if modo == "erro":
            raise RuntimeError("consulta falhou; token=" + TOKEN_REDE)
        if modo == "desconhecido":
            return ResultadoConsulta(EstadoConsulta.DESCONHECIDO)
        if modo == "nao_encontrado":
            return ResultadoConsulta(EstadoConsulta.NAO_ENCONTRADO)
        if modo == "id_ruim" and chave_idempotencia in self.verdade.saidas:
            return ResultadoConsulta(EstadoConsulta.ENCONTRADO, TITULO)
        if chave_idempotencia in self.verdade.saidas:
            return ResultadoConsulta(EstadoConsulta.ENCONTRADO, "17890000000001")
        return ResultadoConsulta(EstadoConsulta.NAO_ENCONTRADO)


@dataclass
class Mundo:
    """Uma plataforma real (verdade + roteiro) e o Notion; cada rodada = um processo novo."""

    notion: NotionFalso
    armazem: ArmazemLocal
    verdade: Verdade = field(default_factory=Verdade)
    roteiro: list[tuple[Any, bool]] = field(default_factory=list)
    consulta: str = "verdade"
    adaptadores: list[AdaptadorVerdade] = field(default_factory=list)

    def adaptador(self) -> AdaptadorVerdade:
        novo = AdaptadorVerdade(
            "Instagram",
            self.notion.eventos,
            self.verdade,
            self.roteiro,
            self.consulta,
            armazem=self.armazem,
        )
        self.adaptadores.append(novo)
        return novo

    def rodar(
        self,
        minutos: float = 0,
        *,
        publicar_real: bool = True,
        parametros: Parametros = PARAMETROS,
    ) -> RelatorioExecucao:
        instante = AGORA + timedelta(minutes=minutos)
        return executar(
            _cliente(self.notion),
            IDS,
            _config(),
            self.armazem,
            {"Instagram": self.adaptador()},
            parametros,
            lambda: instante,
            publicar_real=publicar_real,
            tipos_por_formato=TIPOS,
        )

    @property
    def consultas(self) -> int:
        return sum(len(a.consultas) for a in self.adaptadores)


def _mundo(armazem: ArmazemLocal, *roteiro: tuple[Any, bool], consulta: str = "verdade") -> Mundo:
    notion = _cenario(armazem, (P1, _props()))
    return Mundo(notion, armazem, roteiro=list(roteiro), consulta=consulta)


def _execucoes(notion: NotionFalso) -> list[dict[str, Any]]:
    """Execuções do P1 com resultado, motivo, tentativa e id externo."""
    saida = []
    for r in notion.execucoes:
        p = r["properties"]
        nome = ((p.get("Resultado") or {}).get("select") or {}).get("name")
        if nome == "sinal de vida":
            continue
        texto = "".join(
            t["text"]["content"] for t in (p.get("Código do motivo") or {}).get("rich_text", [])
        )
        externo = "".join(
            t["text"]["content"]
            for t in (p.get("ID do post na plataforma") or {}).get("rich_text", [])
        )
        saida.append(
            {
                "resultado": nome,
                "motivo": texto or None,
                "tentativa": p["Tentativa"]["select"]["name"],
                "externo": externo or None,
            }
        )
    return saida


def _sem_recusas(notion: NotionFalso) -> list[dict[str, Any]]:
    return [e for e in _execucoes(notion) if e["resultado"] != "recusado"]


# ------------------------------------------------------------------ classificação


@pytest.mark.parametrize(
    "resultado,desfecho,registrado,motivo,repete",
    [
        (T.SUCESSO, Desfecho.SUCESSO, "sucesso", None, False),
        (T.SUCESSO_MODO_PRIVADO, Desfecho.SUCESSO_MODO_PRIVADO, "sucesso em modo privado",
         "modo_privado", False),
        (T.FALHA_TRANSITORIA, Desfecho.FALHA_TRANSITORIA, "falha", "erro_transitorio", True),
        (T.FALHA_PERMANENTE, Desfecho.FALHA_PERMANENTE, "falha", "erro_permanente", False),
        (T.INCERTO, Desfecho.INCERTO, "incerto", "plataforma_nao_confirmou", False),
        ("excecao", Desfecho.INCERTO, "incerto", "erro_desconhecido", False),
    ],
)  # fmt: skip
def test_tabela_de_classificacao_e_nova_tentativa_so_no_transitorio(
    armazem: ArmazemLocal,
    resultado: Any,
    desfecho: Desfecho,
    registrado: str,
    motivo: str | None,
    repete: bool,
) -> None:
    mundo = _mundo(armazem, (resultado, False), (T.SUCESSO, True))
    mundo.consulta = "verdade"

    rel = mundo.rodar(0)
    assert rel.ocorrencia(P1, "Instagram").desfecho is desfecho
    assert _execucoes(mundo.notion)[0] == {
        "resultado": registrado, "motivo": motivo, "tentativa": "1",
        "externo": "17890000000001" if registrado.startswith("sucesso") else None,
    }  # fmt: skip

    for minutos in (31, 45, 90, 200):
        mundo.rodar(minutos)

    assert mundo.verdade.envios_reais == (2 if repete else 1)
    assert len(mundo.verdade.saidas) <= 1
    # Só o transitório consulta a plataforma antes de tentar de novo.
    assert mundo.consultas == (1 if repete else 0)


# ------------------------------------------------------------------ intervalo da nova tentativa


@pytest.mark.parametrize(
    "minutos,tenta",
    [(0, False), (10, False), (29, False), (29.99, False), (30, True), (31, True), (120, True)],
)
def test_nova_tentativa_so_depois_do_intervalo_configurado(
    armazem: ArmazemLocal, minutos: float, tenta: bool
) -> None:
    # A falha termina em AGORA (12:10 UTC); a janela vai até 14:00 UTC (agendado 12:00 + 2 h).
    mundo = _mundo(armazem, (T.FALHA_TRANSITORIA, False), (T.SUCESSO, True))
    mundo.rodar(0)
    execucoes_antes = len(_execucoes(mundo.notion))

    deslocamento = min(minutos, 110)  # dentro da janela
    rel = mundo.rodar(deslocamento)

    oc = rel.ocorrencia(P1, "Instagram")
    if tenta:
        assert oc.desfecho is Desfecho.SUCESSO
        assert mundo.consultas == 1 and mundo.verdade.envios_reais == 2
        assert _sem_recusas(mundo.notion)[-1]["tentativa"] == "2"
        assert mundo.notion.estagio(P1) == ID_PUBLICADO
    else:
        assert oc.desfecho is Desfecho.AGUARDANDO_NOVA_TENTATIVA
        assert mundo.consultas == 0 and mundo.verdade.envios_reais == 1
        assert mundo.notion.estagio(P1) == ID_CONFIRMADO
        # Aguardar não escreve nada no banco Execuções (só o sinal de vida da rodada).
        assert len(_execucoes(mundo.notion)) == execucoes_antes


def test_intervalo_vem_dos_parametros_e_nao_de_um_30_fixo(armazem: ArmazemLocal) -> None:
    mundo = _mundo(armazem, (T.FALHA_TRANSITORIA, False), (T.SUCESSO, True))
    parametros = replace(PARAMETROS, nova_tentativa_minutos=45)
    mundo.rodar(0, parametros=parametros)
    assert mundo.rodar(31, parametros=parametros).ocorrencia(P1, "Instagram").desfecho is (
        Desfecho.AGUARDANDO_NOVA_TENTATIVA
    )
    assert mundo.rodar(45, parametros=parametros).ocorrencia(P1, "Instagram").desfecho is (
        Desfecho.SUCESSO
    )
    assert mundo.verdade.envios_reais == 2


# ------------------------------------------------------------------ janela de 2 horas


def test_nova_tentativa_que_cairia_fora_da_janela_nao_acontece_e_o_post_vai_a_falha(
    armazem: ArmazemLocal,
) -> None:
    # Falha às 13:45 UTC; a nova tentativa seria às 14:15, depois do fim da janela (14:00).
    mundo = _mundo(armazem, (T.FALHA_TRANSITORIA, False), (T.SUCESSO, True))
    rel = mundo.rodar(95)

    oc = rel.ocorrencia(P1, "Instagram")
    assert oc.desfecho is Desfecho.FALHA_TRANSITORIA
    assert rel.pos_publicacao[0].desfecho is DesfechoPos.FALHA  # RF-30: sem nova tentativa
    assert mundo.notion.estagio(P1) == ID_FALHA
    for minutos in (100, 126, 200):
        mundo.rodar(minutos)
    assert mundo.verdade.envios_reais == 1 and mundo.consultas == 0


def test_janela_expirada_com_tentativa_pendente_recusa_e_leva_a_falha(
    armazem: ArmazemLocal,
) -> None:
    mundo = _mundo(armazem, (T.FALHA_TRANSITORIA, False), (T.SUCESSO, True))
    mundo.rodar(0)
    # A conexão cai e só volta depois da janela: a tentativa pendente nunca chega a acontecer.
    rel = mundo.rodar(115)  # 14:05 UTC, depois do fim da janela

    oc = rel.ocorrencia(P1, "Instagram")
    assert oc.desfecho is Desfecho.RECUSADO and oc.motivo == "fora_da_janela"
    assert mundo.notion.estagio(P1) == ID_FALHA
    assert mundo.verdade.envios_reais == 1 and mundo.consultas == 0


def test_dentro_da_janela_ainda_tenta_no_ultimo_minuto(armazem: ArmazemLocal) -> None:
    # Falha às 13:20; nova tentativa liberada às 13:50, antes do fim da janela (14:00).
    mundo = _mundo(armazem, (T.FALHA_TRANSITORIA, False), (T.SUCESSO, True))
    mundo.rodar(70)
    assert mundo.notion.estagio(P1) == ID_CONFIRMADO
    assert mundo.rodar(110).ocorrencia(P1, "Instagram").desfecho is Desfecho.SUCESSO
    assert mundo.verdade.envios_reais == 2


# ------------------------------------------------------------------ consulta antes de tentar


def test_consulta_encontrado_registra_sucesso_sem_reenviar(armazem: ArmazemLocal) -> None:
    # A plataforma devolveu erro transitório, mas o post saiu (a consulta revela).
    mundo = _mundo(armazem, (T.FALHA_TRANSITORIA, True))
    mundo.rodar(0)

    rel = mundo.rodar(31)

    oc = rel.ocorrencia(P1, "Instagram")
    assert oc.desfecho is Desfecho.SUCESSO and oc.motivo == "plataforma_confirmou_envio"
    assert oc.registro is Registro.GRAVADO and oc.id_externo == "17890000000001"
    assert mundo.verdade.envios_reais == 1 and len(mundo.verdade.saidas) == 1
    assert _sem_recusas(mundo.notion)[-1] == {
        "resultado": "sucesso", "motivo": "plataforma_confirmou_envio", "tentativa": "2",
        "externo": "17890000000001",
    }  # fmt: skip
    assert mundo.notion.estagio(P1) == ID_PUBLICADO
    mundo.rodar(60)
    assert mundo.verdade.envios_reais == 1


def test_consulta_encontrado_com_id_fora_do_padrao_nao_grava_o_id(armazem: ArmazemLocal) -> None:
    mundo = _mundo(armazem, (T.FALHA_TRANSITORIA, True), consulta="id_ruim")
    mundo.rodar(0)
    oc = mundo.rodar(31).ocorrencia(P1, "Instagram")
    assert oc.desfecho is Desfecho.SUCESSO and oc.id_externo is None
    assert TITULO not in "\n".join(mundo.notion.corpos)
    assert mundo.verdade.envios_reais == 1


def test_consulta_nao_encontrado_permite_uma_nova_tentativa(armazem: ArmazemLocal) -> None:
    mundo = _mundo(armazem, (T.FALHA_TRANSITORIA, False), (T.SUCESSO, True))
    mundo.rodar(0)

    rel = mundo.rodar(31)

    assert rel.ocorrencia(P1, "Instagram").desfecho is Desfecho.SUCESSO
    eventos = [e[0] for e in mundo.notion.eventos if e[0] in ("consultar", "publicar")]
    assert eventos[-2:] == ["consultar", "publicar"]  # consulta ANTES de reenviar
    assert [e["tentativa"] for e in _sem_recusas(mundo.notion)] == ["1", "2"]
    assert mundo.verdade.envios_reais == 2 and len(mundo.verdade.saidas) == 1


@pytest.mark.parametrize(
    "modo,motivo",
    [("desconhecido", "plataforma_nao_confirmou"), ("erro", "erro_desconhecido")],
)
def test_consulta_desconhecida_ou_com_erro_vira_incerto_sem_reenviar(
    armazem: ArmazemLocal, modo: str, motivo: str
) -> None:
    mundo = _mundo(armazem, (T.FALHA_TRANSITORIA, False), consulta=modo)
    mundo.rodar(0)

    rel = mundo.rodar(31)

    oc = rel.ocorrencia(P1, "Instagram")
    assert oc.desfecho is Desfecho.INCERTO and oc.motivo == motivo
    assert _sem_recusas(mundo.notion)[-1] == {
        "resultado": "incerto", "motivo": motivo, "tentativa": "2", "externo": None,
    }  # fmt: skip
    assert mundo.notion.estagio(P1) == ID_FALHA  # incerto nunca vira publicado
    for minutos in (45, 70, 100):
        mundo.rodar(minutos)
    assert mundo.verdade.envios_reais == 1 and mundo.consultas == 1


def test_simulacao_nao_consulta_a_plataforma(armazem: ArmazemLocal) -> None:
    """RF-17: a simulação agendada não faz verificações diretas na plataforma; aplica as
    mesmas regras de tempo e simula a tentativa 2 sem enviar nada."""
    mundo = _mundo(armazem, (T.FALHA_TRANSITORIA, False))
    mundo.rodar(0)
    assert mundo.rodar(10, publicar_real=False).ocorrencia(P1, "Instagram").desfecho is (
        Desfecho.AGUARDANDO_NOVA_TENTATIVA
    )
    rel = mundo.rodar(31, publicar_real=False)
    assert rel.ocorrencia(P1, "Instagram").desfecho is Desfecho.SIMULADO
    assert mundo.consultas == 0 and mundo.verdade.envios_reais == 1
    # A tentativa simulada não gasta a nova tentativa real.
    assert mundo.rodar(40).ocorrencia(P1, "Instagram").desfecho is Desfecho.SUCESSO
    assert mundo.verdade.envios_reais == 2


# ------------------------------------------------------------------ queda no meio do envio


@pytest.mark.parametrize("saiu", [False, True])
def test_queda_no_meio_do_envio_nunca_reenvia(armazem: ArmazemLocal, saiu: bool) -> None:
    mundo = _mundo(armazem, ("queda", saiu))
    with pytest.raises(Queda):
        mundo.rodar(0)
    # Ficou só a "iniciada" (sem resultado): pode ter saído.
    assert [e["resultado"] for e in _execucoes(mundo.notion)] == [None]

    rel = mundo.rodar(5)

    assert rel.ocorrencia(P1, "Instagram").motivo == "execucao_existente"
    assert rel.pos_publicacao[0].desfecho is DesfechoPos.FALHA  # dúvida: nunca publicado
    for minutos in (31, 60, 100):
        mundo.rodar(minutos)
    assert mundo.verdade.envios_reais == 1 and mundo.consultas == 0
    assert mundo.notion.estagio(P1) == ID_FALHA


@pytest.mark.parametrize("saiu", [False, True])
def test_queda_na_nova_tentativa_nunca_gera_terceiro_envio(
    armazem: ArmazemLocal, saiu: bool
) -> None:
    mundo = _mundo(armazem, (T.FALHA_TRANSITORIA, False), ("queda", saiu))
    mundo.rodar(0)
    with pytest.raises(Queda):
        mundo.rodar(31)
    for minutos in (40, 70, 100):
        mundo.rodar(minutos)
    assert mundo.verdade.envios_reais == 2 and len(mundo.verdade.saidas) <= 1
    assert mundo.notion.estagio(P1) == ID_FALHA


# ------------------------------------------------------------------ concorrência


@dataclass
class NotionGancho(NotionFalso):
    """Notion falso que roda um gancho (outra execução) num ponto exato da execução A.

    ``ponto`` = (tipo de chamada, n-ésima ocorrência). Tipos: "consulta_exec" (query no banco
    Execuções), "criacao" (POST /pages) e "patch_exec" (PATCH numa página de execução). O gancho
    roda ANTES de a chamada ser atendida, uma única vez.
    """

    ponto: tuple[str, int] | None = None
    gancho: Callable[[], None] | None = None
    contagem: dict[str, int] = field(default_factory=dict)

    def enviar(
        self,
        metodo: str,
        url: str,
        cabecalhos: Mapping[str, str],
        corpo: bytes | None,
        timeout: float,
    ) -> Any:
        tipo = self._tipo(metodo, url)
        if tipo is not None:
            self.contagem[tipo] = self.contagem.get(tipo, 0) + 1
            if self.gancho is not None and self.ponto == (tipo, self.contagem[tipo]):
                gancho, self.gancho = self.gancho, None
                gancho()
        return super().enviar(metodo, url, cabecalhos, corpo, timeout)

    def _tipo(self, metodo: str, url: str) -> str | None:
        if metodo == "POST" and url.endswith(f"/databases/{BANCO_EXEC}/query"):
            return "consulta_exec"
        if metodo == "POST" and url.endswith("/pages"):
            return "criacao"
        if metodo == "PATCH" and P1 not in url:
            return "patch_exec"
        return None


PONTOS = [(t, n) for t in ("consulta_exec", "criacao", "patch_exec") for n in (1, 2, 3, 4)]


@pytest.mark.parametrize("ponto", PONTOS, ids=[f"{t}-{n}" for t, n in PONTOS])
def test_duas_execucoes_concorrentes_publicam_no_maximo_uma_vez(
    armazem: ArmazemLocal, ponto: tuple[str, int]
) -> None:
    base = _cenario(armazem, (P1, _props()))
    notion = NotionGancho(base.posts)
    mundo = Mundo(notion, armazem)
    relatorios: list[RelatorioExecucao] = []
    notion.ponto = ponto
    notion.gancho = lambda: relatorios.append(mundo.rodar(0))  # execução B, outro processo

    relatorios.insert(0, mundo.rodar(0))  # execução A

    assert mundo.verdade.envios_reais <= 1
    mundo.rodar(31)  # uma rodada posterior completa o que ficou pendente, sem duplicar
    mundo.rodar(60)
    assert mundo.verdade.envios_reais == 1 and len(mundo.verdade.saidas) == 1
    # Limite conhecido: se B roda a pós-publicação com a "iniciada" de A ainda em voo, ela a vê
    # como dúvida e leva o post a "falha" (lado seguro; o sucesso fica registrado e bloqueia a
    # plataforma). O grupo de concorrência do workflow evita esse cruzamento na prática.
    assert notion.estagio(P1) in (ID_PUBLICADO, ID_FALHA)
    assert "sucesso" in [e["resultado"] for e in _execucoes(notion)]


def test_execucao_concorrente_descoberta_depois_de_iniciar_aborta_sem_enviar(
    armazem: ArmazemLocal,
) -> None:
    """As duas gravaram "iniciada" antes de qualquer uma reler: ninguém envia (lado seguro);
    a rodada seguinte publica uma única vez."""
    base = _cenario(armazem, (P1, _props()))
    notion = NotionGancho(base.posts)
    mundo = Mundo(notion, armazem)
    selo = notion.posts[P1].propriedades["Selo de confirmação"]["rich_text"][0]["plain_text"]
    chave = chave_de(P1, "Instagram", selo)
    outra: list[Any] = []

    def outra_execucao_grava_iniciada() -> None:
        outra.append(iniciar_execucao(_cliente(notion), IDS, P1, "Instagram", chave, 1, AGORA))

    notion.ponto = ("consulta_exec", 2)  # a releitura logo depois da "iniciada" de A
    notion.gancho = outra_execucao_grava_iniciada

    rel = mundo.rodar(0)

    oc = rel.ocorrencia(P1, "Instagram")
    assert oc.desfecho is Desfecho.EXECUCAO_CONCORRENTE and oc.motivo == "execucao_concorrente"
    assert mundo.verdade.envios_reais == 0
    assert {"resultado": "não executado", "motivo": "execucao_concorrente", "tentativa": "1",
            "externo": None} in _execucoes(notion)  # fmt: skip
    # A outra também abortaria (mesma regra); ela registra "não executado" e some.
    registrar_resultado(
        _cliente(notion),
        outra[0],
        EstadoExecucao.NAO_EXECUTADO,
        CodigoMotivo.EXECUCAO_CONCORRENTE,
        None,
        AGORA,
    )
    mundo.rodar(5)
    mundo.rodar(10)
    assert mundo.verdade.envios_reais == 1 and notion.estagio(P1) == ID_PUBLICADO


def test_releitura_que_nao_encontra_a_propria_iniciada_aborta(armazem: ArmazemLocal) -> None:
    """Se a releitura não mostra a própria "iniciada" (índice atrasado), não há como provar
    exclusividade: não envia."""

    @dataclass
    class NotionAtrasado(NotionGancho):
        def _consultar_execucoes(self, dados: dict[str, Any]) -> Any:
            if self.contagem.get("consulta_exec") == 2:
                return self._paginar([], dados, tamanho=100)
            return super()._consultar_execucoes(dados)

    base = _cenario(armazem, (P1, _props()))
    notion = NotionAtrasado(base.posts)
    mundo = Mundo(notion, armazem)
    rel = mundo.rodar(0)
    assert rel.ocorrencia(P1, "Instagram").desfecho is Desfecho.EXECUCAO_CONCORRENTE
    assert mundo.verdade.envios_reais == 0


def test_releitura_com_erro_aborta(armazem: ArmazemLocal) -> None:
    @dataclass
    class NotionFalhaNaReleitura(NotionGancho):
        def _consultar_execucoes(self, dados: dict[str, Any]) -> Any:
            if self.contagem.get("consulta_exec") == 2:
                from test_orquestrador import _resposta

                return _resposta({"object": "error", "code": "validation_error"}, 400)
            return super()._consultar_execucoes(dados)

    base = _cenario(armazem, (P1, _props()))
    notion = NotionFalhaNaReleitura(base.posts)
    mundo = Mundo(notion, armazem)
    assert mundo.rodar(0).ocorrencia(P1, "Instagram").desfecho is Desfecho.EXECUCAO_CONCORRENTE
    assert mundo.verdade.envios_reais == 0


def test_falha_de_outra_execucao_no_meio_da_rodada_nao_vira_tentativa_extra(
    armazem: ArmazemLocal,
) -> None:
    """B registra uma falha transitória entre a leitura da guarda e a "iniciada" de A. A não
    pode enviar na hora (seria uma segunda tentativa sem intervalo e sem consulta)."""
    base = _cenario(armazem, (P1, _props()))
    notion = NotionGancho(base.posts)
    mundo = Mundo(notion, armazem, roteiro=[(T.FALHA_TRANSITORIA, False)])
    notion.ponto = ("criacao", 1)  # logo antes da "iniciada" de A
    notion.gancho = lambda: mundo.rodar(0)  # B faz a tentativa 1 inteira e falha

    rel = mundo.rodar(0)

    assert rel.ocorrencia(P1, "Instagram").desfecho is Desfecho.EXECUCAO_CONCORRENTE
    assert mundo.verdade.envios_reais == 1
    assert mundo.rodar(10).ocorrencia(P1, "Instagram").desfecho is (
        Desfecho.AGUARDANDO_NOVA_TENTATIVA
    )
    assert mundo.rodar(31).ocorrencia(P1, "Instagram").desfecho is Desfecho.SUCESSO
    assert mundo.verdade.envios_reais == 2 and mundo.consultas == 1


# ------------------------------------------------------------------ segunda falha


def test_segunda_falha_nao_gera_terceira_tentativa(armazem: ArmazemLocal) -> None:
    mundo = _mundo(armazem, (T.FALHA_TRANSITORIA, False), (T.FALHA_TRANSITORIA, False))
    mundo.rodar(0)
    rel = mundo.rodar(31)
    assert rel.ocorrencia(P1, "Instagram").desfecho is Desfecho.FALHA_TRANSITORIA
    assert rel.pos_publicacao[0].desfecho is DesfechoPos.FALHA  # RF-30
    assert mundo.notion.estagio(P1) == ID_FALHA

    # Mesmo que o post volte a "confirmado" com o MESMO selo (pós-publicação que não gravou),
    # a mesma confirmação nunca ganha uma terceira tentativa.
    _voltar_a_confirmado_sem_reconfirmar(mundo.notion, armazem)
    rel = mundo.rodar(70)
    oc = rel.ocorrencia(P1, "Instagram")
    assert oc.desfecho is Desfecho.SEM_NOVA_TENTATIVA and oc.motivo == "tentativas_esgotadas"
    assert mundo.verdade.envios_reais == 2 and mundo.consultas == 1


def test_falha_permanente_nao_tem_nova_tentativa_na_mesma_confirmacao(
    armazem: ArmazemLocal,
) -> None:
    mundo = _mundo(armazem, (T.FALHA_PERMANENTE, False))
    mundo.rodar(0)
    _voltar_a_confirmado_sem_reconfirmar(mundo.notion, armazem)
    oc = mundo.rodar(31).ocorrencia(P1, "Instagram")
    assert oc.desfecho is Desfecho.SEM_NOVA_TENTATIVA and oc.motivo == "erro_permanente"
    assert mundo.verdade.envios_reais == 1 and mundo.consultas == 0


def _voltar_a_confirmado_sem_reconfirmar(notion: NotionFalso, armazem: ArmazemLocal) -> None:
    """Simula um post que ficou em "confirmado" com o mesmo selo (a mesma confirmação)."""
    notion.posts[P1].propriedades["Estágio"] = {
        "type": "relation",
        "relation": [{"id": ID_CONFIRMADO}],
    }
    _selar(notion, P1, armazem)


# ------------------------------------------------------------------ RF-38


def _execucao_antiga(
    notion: NotionFalso, estado: EstadoExecucao, motivo: CodigoMotivo | None, quando: datetime
) -> None:
    cliente = _cliente(notion)
    chave = chave_de(P1, "Instagram", "selo-de-outra-confirmacao")
    reg = iniciar_execucao(cliente, IDS, P1, "Instagram", chave, 1, quando)
    if estado is not EstadoExecucao.INICIADA:
        registrar_resultado(cliente, reg, estado, motivo, None, quando)
    notion.eventos.clear()
    notion.corpos.clear()


@pytest.mark.parametrize(
    "estado,motivo",
    [
        (EstadoExecucao.INCERTO, CodigoMotivo.PLATAFORMA_NAO_CONFIRMOU),
        (EstadoExecucao.INICIADA, None),
        (EstadoExecucao.SUCESSO, None),
        (EstadoExecucao.SUCESSO_MODO_PRIVADO, CodigoMotivo.MODO_PRIVADO),
    ],
)
def test_rf38_execucao_de_outra_data_e_outra_confirmacao_bloqueia(
    armazem: ArmazemLocal, estado: EstadoExecucao, motivo: CodigoMotivo | None
) -> None:
    mundo = _mundo(armazem)
    # Um mês antes, com outro selo (o post foi reagendado e reconfirmado depois).
    _execucao_antiga(mundo.notion, estado, motivo, datetime.fromisoformat("2026-10-01T12:00Z"))

    for minutos in (0, 31, 60):
        rel = mundo.rodar(minutos)
    motivos = [o.motivo for o in rel.ocorrencias]
    assert motivos in ([], ["execucao_existente"])
    assert mundo.verdade.envios_reais == 0 and mundo.consultas == 0


def test_rf38_falha_definitiva_de_outra_confirmacao_nao_impede_a_nova(
    armazem: ArmazemLocal,
) -> None:
    """RF-34/A-003: uma nova confirmação publica a plataforma que falhou antes (com certeza
    de que não saiu); a falha antiga não conta como tentativa desta confirmação."""
    mundo = _mundo(armazem)
    _execucao_antiga(
        mundo.notion,
        EstadoExecucao.FALHA,
        CodigoMotivo.ERRO_PERMANENTE,
        datetime.fromisoformat("2026-10-01T12:00Z"),
    )
    rel = mundo.rodar(0)
    assert rel.ocorrencia(P1, "Instagram").desfecho is Desfecho.SUCESSO
    assert mundo.verdade.envios_reais == 1 and mundo.consultas == 0
    assert mundo.notion.estagio(P1) == ID_PUBLICADO


# ------------------------------------------------------------------ I-1


def test_nada_sensivel_vaza_nos_fluxos_da_nova_tentativa(
    armazem: ArmazemLocal, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    relatorios: list[RelatorioExecucao] = []
    for roteiro, consulta in (
        ([(T.FALHA_TRANSITORIA, True)], "verdade"),
        ([(T.FALHA_TRANSITORIA, False), ("excecao", False)], "verdade"),
        ([(T.FALHA_TRANSITORIA, False)], "erro"),
        ([(T.FALHA_TRANSITORIA, False)], "desconhecido"),
        ([(T.FALHA_TRANSITORIA, True)], "id_ruim"),
    ):
        mundo = _mundo(armazem, *roteiro, consulta=consulta)
        relatorios += [mundo.rodar(0), mundo.rodar(31), mundo.rodar(70)]
        escrito = "\n".join(mundo.notion.corpos)
        for proibido in (TOKEN, TOKEN_REDE, TITULO, TEXTO, ARTE):
            assert proibido not in escrito
        for registro in mundo.notion.execucoes:
            assert set(registro["properties"]) <= {
                "Chave de idempotência", "Post", "Plataforma", "Tentativa", "Resultado",
                "Código do motivo", "Início", "Fim", "ID do post na plataforma",
            }  # fmt: skip
    textos = (caplog.text, repr(relatorios))
    for proibido in (TOKEN, TOKEN_REDE, TITULO, TEXTO, ARTE, "consulta falhou", "erro de rede"):
        assert all(proibido not in t for t in textos)


# ------------------------------------------------------------------ propriedade à mão


_PRIMEIROS: list[tuple[Any, bool]] = [
    (T.SUCESSO, True),
    (T.SUCESSO_MODO_PRIVADO, True),
    (T.FALHA_TRANSITORIA, False),
    (T.FALHA_TRANSITORIA, True),  # plataforma "mentiu": diz transitório, mas saiu
    (T.FALHA_PERMANENTE, False),
    (T.INCERTO, False),
    (T.INCERTO, True),
    ("excecao", False),
    ("excecao", True),
    ("queda", False),
    ("queda", True),
]
_SEGUNDOS: list[tuple[Any, bool]] = [
    (T.SUCESSO, True),
    (T.FALHA_TRANSITORIA, False),
    (T.INCERTO, True),
    ("excecao", False),
    ("queda", True),
]
_CONSULTAS = ("verdade", "desconhecido", "erro")
_RODADAS = (0, 10, 31, 45, 70, 125, 200)


def test_propriedade_nenhum_cenario_publica_mais_de_uma_vez(armazem: ArmazemLocal) -> None:
    """Varre resultado da 1ª tentativa x resultado da 2ª x resposta da consulta, com várias
    rodadas ao longo (e depois) da janela. A plataforma tem estado próprio: conta o que DE FATO
    saiu. Em nenhuma combinação sai mais de uma vez, há no máximo dois envios (1 + uma nova
    tentativa), nunca um envio fora da janela e o post nunca fica "publicado" sem ter saído."""
    modelo = _cenario(armazem, (P1, _props()))
    casos = 0
    for primeiro, segundo, consulta in itertools.product(_PRIMEIROS, _SEGUNDOS, _CONSULTAS):
        notion = NotionFalso({P1: PaginaFalsa(copy.deepcopy(modelo.posts[P1].propriedades))})
        mundo = Mundo(notion, armazem, roteiro=[primeiro, segundo], consulta=consulta)
        envios_por_rodada = []
        for minutos in _RODADAS:
            antes = mundo.verdade.envios_reais
            try:
                mundo.rodar(minutos)
            except Queda:
                pass
            envios_por_rodada.append((minutos, mundo.verdade.envios_reais - antes))
        contexto = (primeiro, segundo, consulta)
        assert len(mundo.verdade.saidas) <= 1, contexto
        assert mundo.verdade.envios_reais <= 2, contexto
        assert all(n <= 1 for _, n in envios_por_rodada), contexto
        limite = (AGENDADO + JANELA - AGORA).total_seconds() / 60
        assert all(n == 0 for m, n in envios_por_rodada if m > limite), contexto
        if notion.estagio(P1) == ID_PUBLICADO:
            assert len(mundo.verdade.saidas) == 1, contexto
        assert notion.estagio(P1) in (ID_CONFIRMADO, ID_PUBLICADO, ID_FALHA), contexto
        casos += 1
    assert casos == len(_PRIMEIROS) * len(_SEGUNDOS) * len(_CONSULTAS)


def test_selo_ficticio_nao_aparece_nos_registros(armazem: ArmazemLocal) -> None:
    mundo = _mundo(armazem, (T.FALHA_TRANSITORIA, False))
    selo = mundo.notion.posts[P1].propriedades["Selo de confirmação"]["rich_text"][0]
    mundo.rodar(0)
    mundo.rodar(31)
    assert selo["plain_text"] not in "\n".join(mundo.notion.corpos)
