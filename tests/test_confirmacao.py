"""Testes das regras de confirmação (publicador.core.confirmacao) - RF-08, RF-09, I-2.

Sem rede: um transporte falso com estado simula o Notion (GET/PATCH de página e blocos).
Todos os valores são fictícios (I-1).
"""

from __future__ import annotations

import copy
import dataclasses
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from publicador.artes.armazem import ArmazemLocal
from publicador.core.confirmacao import (
    ConfirmacaoRecusada,
    MotivoConfirmacao,
    confirmar_post,
    exigir_estagio_autorizador,
    sair_de_autorizado,
    validar_post_confirmavel,
)
from publicador.core.modelos import Estagio, PapelEstagio, Post
from publicador.core.redacao import SegredoStr
from publicador.core.selo import calcular_selo, verificar_selo
from publicador.notion.bancos import Configuracao
from publicador.notion.cliente import (
    ClienteNotion,
    ErroNotionIncerto,
    ErroTransporte,
    RespostaHttp,
)
from publicador.notion.escrita import EdicaoConcorrente
from publicador.notion.mapeamento import ler_post

TOKEN = "TOKEN_FALSO_abc123XYZ789"
PAGINA = "0123456789abcdef0123456789abcdef"
ID_PAUTA = "a1" * 16
ID_PRONTO = "a2" * 16
ID_CONFIRMADO = "a3" * 16
ID_PUBLICADO = "a4" * 16
ID_FALHA = "a5" * 16
ID_CANCELADO = "a6" * 16
EDITADO = "2026-11-07T12:00:00.000Z"
EDITADO_DEPOIS = "2026-11-07T12:01:00.000Z"
AGORA = datetime(2026, 11, 7, 15, 0, tzinfo=UTC)
ARTE = "serie-a/2026/11/arte_01.png"

ESTAGIOS = {
    ID_PAUTA: Estagio("pauta", PapelEstagio.PAUTA),
    ID_PRONTO: Estagio("pronto", PapelEstagio.PRONTO),
    ID_CONFIRMADO: Estagio("confirmado", PapelEstagio.AUTORIZADO),
    ID_PUBLICADO: Estagio("publicado", PapelEstagio.PUBLICADO),
    ID_FALHA: Estagio("falha", PapelEstagio.FALHA),
    ID_CANCELADO: Estagio("cancelado", PapelEstagio.CANCELADO),
}


def _config(estagios: Mapping[str, Estagio] | None = None) -> Configuracao:
    return Configuracao(
        estagios=dict(ESTAGIOS if estagios is None else estagios),
        formatos={},
        series={},
        temas={},
        problemas=(),
    )


# ------------------------------------------------------------------ Notion falso


def _rich(texto: str | None) -> dict[str, Any]:
    if not texto:
        return {"type": "rich_text", "rich_text": []}
    return {"type": "rich_text", "rich_text": [{"type": "text", "plain_text": texto}]}


def _propriedades_iniciais(
    estagio_id: str, data: str | None, selo: str | None, confirmado: str | None
) -> dict[str, Any]:
    return {
        "Título": {
            "type": "title",
            "title": [{"type": "text", "plain_text": "Quanto custa a energia?"}],
        },
        "Estágio": {"type": "relation", "relation": [{"id": estagio_id}]},
        "Plataformas": {
            "type": "multi_select",
            "multi_select": [{"name": "Instagram"}, {"name": "TikTok"}],
        },
        "Data e hora": {"type": "date", "date": None if data is None else {"start": data}},
        "Artes": _rich(ARTE),
        "Selo de confirmação": _rich(selo),
        "Confirmado em": {
            "type": "date",
            "date": None if confirmado is None else {"start": confirmado},
        },
    }


def _normalizar(propriedade: Any) -> Any:
    """Faz o papel do Notion: devolve plain_text nos itens de texto gravados."""
    if isinstance(propriedade, dict):
        for chave in ("rich_text", "title"):
            if chave in propriedade:
                propriedade[chave] = [
                    {"type": "text", "plain_text": i["text"]["content"]} for i in propriedade[chave]
                ]
    return propriedade


@dataclass
class NotionFalso:
    """Transporte com estado: guarda a página e aplica PATCH de propriedades."""

    propriedades: dict[str, Any]
    editado: str = EDITADO
    falha_no_patch: Exception | None = None
    editar_na_leitura: str | None = None  # simula edição externa antes do GET de conferência
    chamadas: list[tuple[str, str, dict[str, Any] | None]] = field(default_factory=list)

    @property
    def patches(self) -> list[dict[str, Any]]:
        return [c[2] or {} for c in self.chamadas if c[0] == "PATCH"]

    @property
    def escritas(self) -> list[tuple[str, str]]:
        return [(m, p) for m, p, _ in self.chamadas if m not in ("GET",)]

    def enviar(
        self,
        metodo: str,
        url: str,
        cabecalhos: Mapping[str, str],
        corpo: bytes | None,
        timeout: float,
    ) -> RespostaHttp:
        caminho = re.sub(r"^https://api\.notion\.com/v1", "", url).split("?")[0]
        dados = json.loads(corpo) if corpo else None
        self.chamadas.append((metodo, caminho, dados))
        if caminho == f"/pages/{PAGINA}" and metodo == "GET":
            if self.editar_na_leitura is not None:
                self.editado = self.editar_na_leitura
                self.editar_na_leitura = None
            return self._resposta(self._pagina())
        if caminho == f"/pages/{PAGINA}" and metodo == "PATCH":
            if self.falha_no_patch is not None:
                raise self.falha_no_patch
            assert dados is not None
            for nome, valor in dados["properties"].items():
                self.propriedades[nome] = _normalizar(copy.deepcopy(valor))
            self.editado = EDITADO_DEPOIS
            return self._resposta(self._pagina())
        if caminho == f"/blocks/{PAGINA}/children" and metodo == "GET":
            bloco = {
                "id": "b" * 32,
                "type": "paragraph",
                "paragraph": {"rich_text": [{"plain_text": "Texto do roteiro."}]},
            }
            return self._resposta({"results": [bloco], "has_more": False})
        raise AssertionError(f"chamada inesperada: {metodo} {caminho}")

    def _pagina(self) -> dict[str, Any]:
        return {
            "id": PAGINA,
            "last_edited_time": self.editado,
            "properties": copy.deepcopy(self.propriedades),
        }

    @staticmethod
    def _resposta(dados: object) -> RespostaHttp:
        return RespostaHttp(200, {}, json.dumps(dados).encode())


@pytest.fixture
def armazem(tmp_path: Path) -> ArmazemLocal:
    destino = tmp_path / "artes" / ARTE
    destino.parent.mkdir(parents=True)
    destino.write_bytes(b"\x89PNG conteudo ficticio")
    return ArmazemLocal(tmp_path / "artes")


def _montar(
    estagio_id: str = ID_PRONTO,
    data: str | None = "2026-11-20T18:30:00-03:00",
    selo: str | None = None,
    confirmado: str | None = None,
    **opcoes: Any,
) -> tuple[ClienteNotion, NotionFalso]:
    notion = NotionFalso(_propriedades_iniciais(estagio_id, data, selo, confirmado), **opcoes)
    cliente = ClienteNotion(SegredoStr(TOKEN), transporte=notion, dormir=lambda _: None)
    return cliente, notion


def _ler(cliente: ClienteNotion, config: Configuracao | None = None) -> Post:
    return ler_post(cliente, PAGINA, _config() if config is None else config)


# ------------------------------------------------------------------ confirmar: caminho feliz


def test_com_data_e_hora_confirma_em_uma_unica_escrita(armazem: ArmazemLocal) -> None:
    cliente, notion = _montar()
    post = _ler(cliente)
    selo_esperado = calcular_selo(post, armazem)

    novo = confirmar_post(cliente, post, _config(), armazem, AGORA)

    assert notion.escritas == [("PATCH", f"/pages/{PAGINA}")]  # exatamente UM PATCH
    (props,) = notion.patches
    assert set(props["properties"]) == {"Estágio", "Selo de confirmação", "Confirmado em"}
    assert props["properties"]["Estágio"] == {"relation": [{"id": ID_CONFIRMADO}]}
    assert props["properties"]["Selo de confirmação"]["rich_text"][0]["text"]["content"] == (
        selo_esperado
    )
    assert props["properties"]["Confirmado em"] == {"date": {"start": AGORA.isoformat()}}
    # devolve o post relido, já autorizado e com selo válido
    assert novo.estagio.papel is PapelEstagio.AUTORIZADO
    assert novo.estagio.nome == "confirmado"
    assert novo.selo == selo_esperado
    assert novo.confirmado_em == AGORA
    assert verificar_selo(novo, armazem, novo.selo)


def test_selo_gravado_e_o_de_calcular_selo(armazem: ArmazemLocal) -> None:
    cliente, notion = _montar()
    post = _ler(cliente)
    confirmar_post(cliente, post, _config(), armazem, AGORA)
    gravado = notion.propriedades["Selo de confirmação"]["rich_text"][0]["plain_text"]
    assert gravado == calcular_selo(post, armazem)


def test_nao_altera_outros_campos(armazem: ArmazemLocal) -> None:
    cliente, notion = _montar()
    post = _ler(cliente)
    confirmar_post(cliente, post, _config(), armazem, AGORA)
    (props,) = notion.patches
    for proibido in ("Título", "Data e hora", "Artes", "Plataformas", "Formato"):
        assert proibido not in props["properties"]


# ------------------------------------------------------------------ confirmar: data e hora


def test_sem_data_recusa_sem_escrever(armazem: ArmazemLocal) -> None:
    cliente, notion = _montar(data=None)
    post = _ler(cliente)
    with pytest.raises(ConfirmacaoRecusada) as erro:
        confirmar_post(cliente, post, _config(), armazem, AGORA)
    assert erro.value.motivo is MotivoConfirmacao.SEM_DATA
    assert notion.escritas == []


def test_so_data_sem_hora_recusa_sem_escrever(armazem: ArmazemLocal) -> None:
    cliente, notion = _montar(data="2026-11-20")
    post = _ler(cliente)
    assert post.somente_data and post.publicar_em is None
    with pytest.raises(ConfirmacaoRecusada) as erro:
        confirmar_post(cliente, post, _config(), armazem, AGORA)
    assert erro.value.motivo is MotivoConfirmacao.SEM_HORA
    assert notion.escritas == []


def test_validar_post_confirmavel_e_pura_e_distingue_data_de_hora(armazem: ArmazemLocal) -> None:
    cliente, _ = _montar(data="2026-11-20")
    so_dia = _ler(cliente)
    with pytest.raises(ConfirmacaoRecusada) as erro:
        validar_post_confirmavel(so_dia)
    assert erro.value.motivo is MotivoConfirmacao.SEM_HORA
    completo = dataclasses.replace(
        so_dia, somente_data=False, publicar_em=datetime(2026, 11, 20, 18, 30, tzinfo=UTC)
    )
    validar_post_confirmavel(completo)  # não levanta


# ------------------------------------------------------------------ confirmar: configuração


def _sem_autorizador() -> Configuracao:
    return _config({k: v for k, v in ESTAGIOS.items() if k != ID_CONFIRMADO})


def _dois_autorizadores() -> Configuracao:
    estagios = dict(ESTAGIOS)
    # contorna a validação do modelo para simular configuração corrompida
    clone = Estagio("confirmado", PapelEstagio.AUTORIZADO)
    estagios["a7" * 16] = clone
    return _config(estagios)


def _autorizador_com_nome_errado() -> Configuracao:
    errado = Estagio("pronto", PapelEstagio.PRONTO)
    object.__setattr__(errado, "papel", PapelEstagio.AUTORIZADO)  # nome != "confirmado"
    estagios = {k: v for k, v in ESTAGIOS.items() if k != ID_CONFIRMADO}
    estagios["a8" * 16] = errado
    return _config(estagios)


@pytest.mark.parametrize(
    "fabrica", [_sem_autorizador, _dois_autorizadores, _autorizador_com_nome_errado]
)
def test_configuracao_de_estagios_invalida_recusa_sem_escrever(
    fabrica: Any, armazem: ArmazemLocal
) -> None:
    cliente, notion = _montar()
    post = _ler(cliente)
    config = fabrica()
    with pytest.raises(ConfirmacaoRecusada) as erro:
        confirmar_post(cliente, post, config, armazem, AGORA)
    assert erro.value.motivo is MotivoConfirmacao.CONFIG_ESTAGIOS_INVALIDA
    assert notion.escritas == []
    with pytest.raises(ConfirmacaoRecusada):
        exigir_estagio_autorizador(config)


def test_configuracao_com_problemas_registrados_recusa(armazem: ArmazemLocal) -> None:
    cliente, notion = _montar()
    post = _ler(cliente)
    config = dataclasses.replace(_config(), problemas=("estágios: linha sem papel",))
    with pytest.raises(ConfirmacaoRecusada) as erro:
        confirmar_post(cliente, post, config, armazem, AGORA)
    assert erro.value.motivo is MotivoConfirmacao.CONFIG_ESTAGIOS_INVALIDA
    assert notion.escritas == []


def test_exigir_estagio_autorizador_devolve_id_e_estagio() -> None:
    estagio_id, estagio = exigir_estagio_autorizador(_config())
    assert estagio_id == ID_CONFIRMADO
    assert estagio.nome == "confirmado"


# ------------------------------------------------------------------ confirmar: conteúdo


def test_sem_plataformas_recusa_sem_escrever(armazem: ArmazemLocal) -> None:
    cliente, notion = _montar()
    post = dataclasses.replace(_ler(cliente), plataformas=())
    with pytest.raises(ConfirmacaoRecusada) as erro:
        confirmar_post(cliente, post, _config(), armazem, AGORA)
    assert erro.value.motivo is MotivoConfirmacao.SEM_PLATAFORMAS
    assert notion.escritas == []


def test_titulo_vazio_recusa_sem_escrever(armazem: ArmazemLocal) -> None:
    cliente, notion = _montar()
    post = _ler(cliente)
    object.__setattr__(post, "titulo", "   ")  # o modelo já impede; defesa em profundidade
    with pytest.raises(ConfirmacaoRecusada) as erro:
        confirmar_post(cliente, post, _config(), armazem, AGORA)
    assert erro.value.motivo is MotivoConfirmacao.SEM_TITULO
    assert notion.escritas == []


def test_arte_ausente_recusa_sem_escrever(tmp_path: Path) -> None:
    vazio = tmp_path / "vazio"
    vazio.mkdir()
    cliente, notion = _montar()
    post = _ler(cliente)
    with pytest.raises(ConfirmacaoRecusada) as erro:
        confirmar_post(cliente, post, _config(), ArmazemLocal(vazio), AGORA)
    assert erro.value.motivo is MotivoConfirmacao.ARTE_INVALIDA
    assert notion.escritas == []


def test_arte_com_caminho_perigoso_recusa_sem_escrever(armazem: ArmazemLocal) -> None:
    cliente, notion = _montar()
    post = dataclasses.replace(_ler(cliente), artes=("../fora.png",))
    with pytest.raises(ConfirmacaoRecusada) as erro:
        confirmar_post(cliente, post, _config(), armazem, AGORA)
    assert erro.value.motivo is MotivoConfirmacao.ARTE_INVALIDA
    assert notion.escritas == []


def test_arte_com_plataforma_desconhecida_recusa_sem_escrever(armazem: ArmazemLocal) -> None:
    cliente, notion = _montar()
    post = dataclasses.replace(_ler(cliente), artes=(f"youtube:{ARTE}",))
    with pytest.raises(ConfirmacaoRecusada) as erro:
        confirmar_post(cliente, post, _config(), armazem, AGORA)
    assert erro.value.motivo is MotivoConfirmacao.ARTE_INVALIDA
    assert notion.escritas == []


def test_agora_sem_fuso_e_recusado(armazem: ArmazemLocal) -> None:
    cliente, notion = _montar()
    post = _ler(cliente)
    with pytest.raises(ValueError, match="fuso"):
        confirmar_post(cliente, post, _config(), armazem, datetime(2026, 11, 7, 15, 0))
    assert notion.escritas == []


# ------------------------------------------------------------------ confirmar: estado de origem


def test_post_ja_confirmado_recusa_sem_escrever(armazem: ArmazemLocal) -> None:
    cliente, notion = _montar(estagio_id=ID_CONFIRMADO, selo="v1:" + "0" * 64, confirmado=EDITADO)
    post = _ler(cliente)
    with pytest.raises(ConfirmacaoRecusada) as erro:
        confirmar_post(cliente, post, _config(), armazem, AGORA)
    assert erro.value.motivo is MotivoConfirmacao.POST_JA_CONFIRMADO
    assert notion.escritas == []


def test_post_ja_publicado_nao_pode_ser_confirmado(armazem: ArmazemLocal) -> None:
    cliente, notion = _montar(estagio_id=ID_PUBLICADO)
    post = _ler(cliente)
    with pytest.raises(ConfirmacaoRecusada) as erro:
        confirmar_post(cliente, post, _config(), armazem, AGORA)
    assert erro.value.motivo is MotivoConfirmacao.ESTAGIO_DE_ORIGEM_INVALIDO
    assert notion.escritas == []


def test_post_sem_editado_em_recusa_sem_escrever(armazem: ArmazemLocal) -> None:
    cliente, notion = _montar()
    post = dataclasses.replace(_ler(cliente), editado_em=None)
    with pytest.raises(ConfirmacaoRecusada) as erro:
        confirmar_post(cliente, post, _config(), armazem, AGORA)
    assert erro.value.motivo is MotivoConfirmacao.SEM_VERSAO_LIDA
    assert notion.escritas == []


# ------------------------------------------------------------------ confirmar: concorrência/falhas


def test_edicao_concorrente_propaga_e_nao_grava_nada(armazem: ArmazemLocal) -> None:
    cliente, notion = _montar()
    post = _ler(cliente)
    antes = copy.deepcopy(notion.propriedades)
    notion.editar_na_leitura = "2026-11-07T12:00:30.000Z"  # a edição externa ocorre agora
    with pytest.raises(EdicaoConcorrente):
        confirmar_post(cliente, post, _config(), armazem, AGORA)
    assert notion.escritas == []
    assert notion.propriedades == antes


def test_falha_na_escrita_propaga_e_nada_fica_meio_gravado(armazem: ArmazemLocal) -> None:
    falha = ErroNotionIncerto("falha ficticia")
    cliente, notion = _montar(falha_no_patch=falha)
    post = _ler(cliente)
    antes = copy.deepcopy(notion.propriedades)
    with pytest.raises(ErroNotionIncerto):
        confirmar_post(cliente, post, _config(), armazem, AGORA)
    assert notion.propriedades == antes  # nem estágio, nem selo, nem Confirmado em
    assert len(notion.patches) == 1  # uma única tentativa de escrita: nada de PATCHs separados


def test_erro_de_transporte_no_patch_nao_deixa_estado_parcial(armazem: ArmazemLocal) -> None:
    cliente, notion = _montar(falha_no_patch=ErroTransporte("queda ficticia"))
    post = _ler(cliente)
    antes = copy.deepcopy(notion.propriedades)
    with pytest.raises(Exception):  # noqa: B017 - qualquer erro de Notion propaga
        confirmar_post(cliente, post, _config(), armazem, AGORA)
    assert notion.propriedades == antes


def test_releitura_com_selo_diferente_levanta_recusa(armazem: ArmazemLocal) -> None:
    """Se o conteúdo mudou sem alterar o minuto de edição, o selo relido não confere."""
    cliente, notion = _montar()
    post = _ler(cliente)
    # o post em memória tem outro título do que está no Notion (edição não detectada)
    divergente = dataclasses.replace(post, titulo="Outro título")
    with pytest.raises(ConfirmacaoRecusada) as erro:
        confirmar_post(cliente, divergente, _config(), armazem, AGORA)
    assert erro.value.motivo is MotivoConfirmacao.SELO_NAO_CONFERE_APOS_GRAVAR


# ------------------------------------------------------------------ sair de autorizado


def _confirmado(armazem: ArmazemLocal) -> tuple[ClienteNotion, NotionFalso, Post]:
    cliente, notion = _montar(
        estagio_id=ID_CONFIRMADO, selo="v1:" + "a" * 64, confirmado="2026-11-07T12:00:00+00:00"
    )
    return cliente, notion, _ler(cliente)


@pytest.mark.parametrize("destino", ["pronto", "pauta", "cancelado", "falha"])
def test_sair_de_autorizado_limpa_selo_e_confirmado_em_na_mesma_escrita(
    destino: str, armazem: ArmazemLocal
) -> None:
    cliente, notion, post = _confirmado(armazem)
    novo = sair_de_autorizado(cliente, post, _config(), ESTAGIOS_POR_NOME[destino])

    assert notion.escritas == [("PATCH", f"/pages/{PAGINA}")]
    (props,) = notion.patches
    id_destino = next(i for i, e in ESTAGIOS.items() if e.nome == destino)
    assert props["properties"] == {
        "Estágio": {"relation": [{"id": id_destino}]},
        "Selo de confirmação": {"rich_text": []},
        "Confirmado em": {"date": None},
    }
    assert novo.estagio.nome == destino
    assert novo.selo is None
    assert novo.confirmado_em is None


ESTAGIOS_POR_NOME = {e.nome: e for e in ESTAGIOS.values()}


def test_devolver_a_pronto_deixa_post_sem_selo_valido(armazem: ArmazemLocal) -> None:
    cliente, _, post = _confirmado(armazem)
    novo = sair_de_autorizado(cliente, post, _config(), ESTAGIOS_POR_NOME["pronto"])
    assert novo.estagio.papel is PapelEstagio.PRONTO
    assert not verificar_selo(novo, armazem, novo.selo)


def test_sair_para_autorizado_e_recusado(armazem: ArmazemLocal) -> None:
    cliente, notion, post = _confirmado(armazem)
    with pytest.raises(ConfirmacaoRecusada) as erro:
        sair_de_autorizado(cliente, post, _config(), ESTAGIOS["a3" * 16])
    assert erro.value.motivo is MotivoConfirmacao.DESTINO_INVALIDO
    assert notion.escritas == []


def test_destino_fora_da_configuracao_e_recusado(armazem: ArmazemLocal) -> None:
    cliente, notion, post = _confirmado(armazem)
    estranho = Estagio("outro", PapelEstagio.PRONTO)
    with pytest.raises(ConfirmacaoRecusada) as erro:
        sair_de_autorizado(cliente, post, _config(), estranho)
    assert erro.value.motivo is MotivoConfirmacao.DESTINO_INVALIDO
    assert notion.escritas == []


def test_sair_com_edicao_concorrente_propaga_e_nao_grava(armazem: ArmazemLocal) -> None:
    cliente, notion, post = _confirmado(armazem)
    notion.editar_na_leitura = "2026-11-07T12:00:30.000Z"
    antes = copy.deepcopy(notion.propriedades)
    with pytest.raises(EdicaoConcorrente):
        sair_de_autorizado(cliente, post, _config(), ESTAGIOS_POR_NOME["pronto"])
    assert notion.propriedades == antes
    assert notion.escritas == []


def test_sair_funciona_mesmo_com_configuracao_sem_autorizador(armazem: ArmazemLocal) -> None:
    """Cancelar é o lado seguro: não exige a configuração do autorizador íntegra."""
    cliente, notion, post = _confirmado(armazem)
    config = _config()
    novo = sair_de_autorizado(cliente, post, config, ESTAGIOS_POR_NOME["pronto"])
    assert novo.selo is None
    assert len(notion.patches) == 1


def test_sair_de_post_que_nao_esta_autorizado_tambem_limpa_selo_residual(
    armazem: ArmazemLocal,
) -> None:
    cliente, notion = _montar(
        estagio_id=ID_PRONTO, selo="v1:" + "b" * 64, confirmado="2026-11-07T12:00:00+00:00"
    )
    post = _ler(cliente)
    novo = sair_de_autorizado(cliente, post, _config(), ESTAGIOS_POR_NOME["pauta"])
    assert novo.selo is None and novo.confirmado_em is None
    assert len(notion.patches) == 1


def test_sair_sem_editado_em_recusa(armazem: ArmazemLocal) -> None:
    cliente, notion, post = _confirmado(armazem)
    post = dataclasses.replace(post, editado_em=None)
    with pytest.raises(ConfirmacaoRecusada) as erro:
        sair_de_autorizado(cliente, post, _config(), ESTAGIOS_POR_NOME["pronto"])
    assert erro.value.motivo is MotivoConfirmacao.SEM_VERSAO_LIDA
    assert notion.escritas == []


# ------------------------------------------------------------------ I-1: nada de segredo


def test_nenhum_erro_ou_log_contem_o_token(
    armazem: ArmazemLocal, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    caplog.set_level(logging.DEBUG)
    textos: list[str] = []

    # sucesso
    cliente, _ = _montar()
    post = _ler(cliente)
    novo = confirmar_post(cliente, post, _config(), armazem, AGORA)
    textos += [repr(novo), str(novo)]
    # saída
    textos.append(repr(sair_de_autorizado(cliente, novo, _config(), ESTAGIOS_POR_NOME["pronto"])))
    # recusas
    cliente2, _ = _montar(data=None)
    for chamada in (
        lambda: confirmar_post(cliente2, _ler(cliente2), _config(), armazem, AGORA),
        lambda: confirmar_post(cliente2, _ler(cliente2), _sem_autorizador(), armazem, AGORA),
    ):
        with pytest.raises(ConfirmacaoRecusada) as erro:
            chamada()
        textos += [str(erro.value), repr(erro.value), erro.value.detalhe or ""]
    # edição concorrente
    cliente3, notion3 = _montar()
    post3 = _ler(cliente3)
    notion3.editar_na_leitura = "2026-11-07T12:00:30.000Z"
    with pytest.raises(EdicaoConcorrente) as erro_c:
        confirmar_post(cliente3, post3, _config(), armazem, AGORA)
    textos.append(str(erro_c.value))

    textos += [r.getMessage() for r in caplog.records] + [repr(cliente), str(cliente)]
    assert all(TOKEN not in t for t in textos)


def test_selo_e_estagio_nao_dependem_do_relogio_de_agora_para_o_selo(
    armazem: ArmazemLocal,
) -> None:
    """O selo não inclui 'Confirmado em': confirmar em momentos distintos dá o mesmo selo."""
    c1, _ = _montar()
    c2, _ = _montar()
    n1 = confirmar_post(c1, _ler(c1), _config(), armazem, AGORA)
    n2 = confirmar_post(c2, _ler(c2), _config(), armazem, AGORA + timedelta(days=1))
    assert n1.selo == n2.selo
