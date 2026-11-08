"""Testes de ``AdaptadorInstagram.consultar`` (T047; RF-12, RF-25, RF-38; plano 4.2 e R8).

Só respostas FABRICADAS (tests/fixtures/instagram) e token FALSO: nenhuma rede real. A regra
central: ``NAO_ENCONTRADO`` só com CERTEZA (um falso "não encontrado" gera post duplicado, o
pior defeito do projeto); qualquer dúvida é ``DESCONHECIDO``. I-1: o token falso nunca
aparece em repr/str/log/exceção/resultado.

Relógio dos testes unitários: 2025-12-27T21:00Z; janela de consulta de 3 h => limite 18:00Z.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_nova_tentativa import AdaptadorVerdade, Mundo, T, _execucoes, _sem_recusas
from test_orquestrador import AGORA as AGORA_ORQ
from test_orquestrador import P1, _cenario, _props
from transporte_gravado import Resposta, TransporteGravado

from publicador.adaptadores.base import (
    EstadoConsulta,
    PedidoDePublicacao,
    ResultadoConsulta,
    TipoDeResultado,
)
from publicador.adaptadores.instagram import AdaptadorInstagram
from publicador.artes.armazem import ArmazemLocal
from publicador.core.modelos import Estagio, PapelEstagio, Post
from publicador.core.redacao import SegredoStr, limpar_segredos_registrados
from publicador.job.orquestrador import Desfecho

FIXTURES = Path(__file__).parent / "fixtures" / "instagram"
TOKEN = "tok_ficticio_ZXCV1234abcd"
ID_CONTA = "17841400000000001"
AGORA = datetime(2025, 12, 27, 21, 0, tzinfo=UTC)
CHAVE = "v1-0123456789abcdef0123456789abcdef"
ARTE = "serie-a/2026/11/video.mp4"
VIDEO = b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 4
ID_MIDIA = "17895000000000042"
CORPO_UNICO = "CORPO-BRUTO-QUE-NAO-PODE-IR-A-LOG"
BASE = "https://graph.facebook.com/v21.0"
T_ = TipoDeResultado


@pytest.fixture(autouse=True)
def _limpar_segredos() -> Any:
    yield
    limpar_segredos_registrados()


def _f(nome: str) -> bytes:
    return (FIXTURES / nome).read_bytes()


@pytest.fixture
def raiz(tmp_path: Path) -> Path:
    destino = tmp_path / "artes" / ARTE
    destino.parent.mkdir(parents=True)
    destino.write_bytes(VIDEO)
    return tmp_path / "artes"


def _adaptador(transporte: Any, raiz: Path, **kw: Any) -> AdaptadorInstagram:
    return AdaptadorInstagram(
        SegredoStr(TOKEN),
        ID_CONTA,
        transporte,
        ArmazemLocal(raiz),
        lambda: AGORA,
        dormir=lambda s: None,
        **kw,
    )


def _post() -> Post:
    return Post(
        id="post-ficticio-0001",
        titulo="Titulo ficticio",
        corpo="Legenda ficticia",
        estagio=Estagio("confirmado", PapelEstagio.AUTORIZADO),
        plataformas=("instagram",),
        formato="Reels",
    )


def _pedido() -> Any:
    return PedidoDePublicacao(_post(), "instagram", (ARTE,), CHAVE, simular=False)


ATIVA = (200, _f("debug_token_ativa.json"))
PRE = [ATIVA, (200, _f("limite_restante.json"))]
CONTAINER = (200, _f("container_resumable.json"))
UPLOAD = (200, _f("upload_ok.json"))
EM_ANDAMENTO = (200, _f("status_em_andamento.json"))
FINALIZADO = (200, _f("status_finalizado.json"))
PUBLICADO_OK = (200, _f("publish_ok.json"))


def _m(nome: str) -> Resposta:
    return (200, _f(nome))


VAZIAS: list[Resposta] = [_m("midia_vazia.json"), _m("historias_vazia.json")]


def _consulta(transporte: Any, raiz: Path) -> ResultadoConsulta:
    return _adaptador(transporte, raiz).consultar(CHAVE)


def _urls(transporte: TransporteGravado) -> list[str]:
    return [c.url for c in transporte.chamadas]


def _sem_post(transporte: TransporteGravado) -> bool:
    return all(c.metodo == "GET" for c in transporte.chamadas)


# --------------------------------------------------------------------------- andamento local


def test_encontrado_por_andamento_local_com_resultado_guardado(raiz: Path) -> None:
    transporte = TransporteGravado(*PRE, CONTAINER, UPLOAD, FINALIZADO, PUBLICADO_OK)
    adaptador = _adaptador(transporte, raiz)
    assert adaptador.publicar(_pedido()).tipo is T_.SUCESSO
    n = len(transporte.chamadas)

    consulta = adaptador.consultar(CHAVE)

    assert consulta.estado is EstadoConsulta.ENCONTRADO
    assert consulta.id_externo == ID_MIDIA
    assert len(transporte.chamadas) == n  # sem rede


def test_nao_encontrado_por_andamento_quando_publish_nunca_foi_enviado(raiz: Path) -> None:
    transporte = TransporteGravado(*PRE, CONTAINER, UPLOAD, *[EM_ANDAMENTO] * 2)
    adaptador = _adaptador(transporte, raiz, max_tentativas_status=2)
    assert adaptador.publicar(_pedido()).motivo == "sondagem_esgotada"
    n = len(transporte.chamadas)

    consulta = adaptador.consultar(CHAVE)

    assert consulta.estado is EstadoConsulta.NAO_ENCONTRADO
    assert len(transporte.chamadas) == n  # sem rede
    assert not any(u.endswith("/media_publish") for u in _urls(transporte))


@pytest.mark.parametrize(
    "tail",
    [
        [TimeoutError("lento")],  # publish sem resposta
        [(500, b"")],  # publish com 5xx
        [(200, b'{"sem":"id"}')],  # sucesso sem id
        [(400, b'{"error":{"code":100}}')],  # até 4xx após o envio é dúvida
    ],
)
def test_andamento_com_publish_enviado_sem_resultado_e_desconhecido(
    raiz: Path, tail: list[Resposta]
) -> None:
    transporte = TransporteGravado(*PRE, CONTAINER, UPLOAD, FINALIZADO, *tail)
    adaptador = _adaptador(transporte, raiz)
    assert adaptador.publicar(_pedido()).tipo is T_.INCERTO
    n = len(transporte.chamadas)

    consulta = adaptador.consultar(CHAVE)

    assert consulta.estado is EstadoConsulta.DESCONHECIDO  # nunca NAO_ENCONTRADO após INCERTO
    assert len(transporte.chamadas) == n  # nem tenta a listagem


def test_container_ja_publicado_na_sondagem_tambem_e_desconhecido(raiz: Path) -> None:
    transporte = TransporteGravado(*PRE, CONTAINER, UPLOAD, (200, _f("status_publicado.json")))
    adaptador = _adaptador(transporte, raiz)
    assert adaptador.publicar(_pedido()).tipo is T_.INCERTO
    n = len(transporte.chamadas)
    assert adaptador.consultar(CHAVE).estado is EstadoConsulta.DESCONHECIDO
    assert len(transporte.chamadas) == n


def test_repetir_consulta_apos_incerto_continua_desconhecido(raiz: Path) -> None:
    transporte = TransporteGravado(*PRE, CONTAINER, UPLOAD, FINALIZADO, TimeoutError("x"))
    adaptador = _adaptador(transporte, raiz)
    adaptador.publicar(_pedido())
    for _ in range(3):
        assert adaptador.consultar(CHAVE).estado is EstadoConsulta.DESCONHECIDO


def test_chave_de_outro_andamento_nao_contamina(raiz: Path) -> None:
    transporte = TransporteGravado(*PRE, CONTAINER, UPLOAD, FINALIZADO, TimeoutError("x"), ATIVA)
    adaptador = _adaptador(transporte, raiz)
    adaptador.publicar(_pedido())  # INCERTO na CHAVE
    # outra chave, sem andamento: cai na listagem (aqui a conexão devolve 'sem resposta')
    assert adaptador.consultar("v1-ffffffffffffffffffffffffffffffff").estado in (
        EstadoConsulta.DESCONHECIDO,
        EstadoConsulta.NAO_ENCONTRADO,
    )


# --------------------------------------------------------------------------- listagem


def test_nao_encontrado_por_listagem_completa_e_vazia_na_janela(raiz: Path) -> None:
    transporte = TransporteGravado(ATIVA, *VAZIAS)

    consulta = _consulta(transporte, raiz)

    assert consulta.estado is EstadoConsulta.NAO_ENCONTRADO
    assert consulta.id_externo is None
    assert _sem_post(transporte)
    urls = _urls(transporte)
    assert urls[1].startswith(f"{BASE}/{ID_CONTA}/media?fields=id,timestamp&limit=")
    assert urls[2].startswith(f"{BASE}/{ID_CONTA}/stories?fields=id,timestamp&limit=")
    assert transporte.restantes == 0


def test_nao_encontrado_com_midias_apenas_anteriores_a_janela(raiz: Path) -> None:
    # as duas listas trazem só mídias mais antigas que 3 h; a paginação restante é ignorada
    # porque a ordem é decrescente e já se viu uma mídia anterior ao limite
    transporte = TransporteGravado(ATIVA, _m("midia_antiga.json"), _m("midia_antiga_fim.json"))
    assert _consulta(transporte, raiz).estado is EstadoConsulta.NAO_ENCONTRADO


def test_midia_exatamente_no_limite_da_janela_conta_como_possivel_correspondencia(
    raiz: Path,
) -> None:
    limite = (AGORA - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S+0000")
    corpo = json.dumps({"data": [{"id": "17895000000000111", "timestamp": limite}]}).encode()
    transporte = TransporteGravado(ATIVA, (200, corpo), _m("historias_vazia.json"))
    assert _consulta(transporte, raiz).estado is EstadoConsulta.DESCONHECIDO


def test_midia_recente_na_conta_impede_nao_encontrado(raiz: Path) -> None:
    # sem marcador não há como saber se é a nossa: DESCONHECIDO (resolução manual)
    transporte = TransporteGravado(ATIVA, _m("midia_recente.json"), _m("historias_vazia.json"))
    consulta = _consulta(transporte, raiz)
    assert consulta.estado is EstadoConsulta.DESCONHECIDO
    assert consulta.id_externo is None  # listagem nunca vira ENCONTRADO sem marcador


def test_story_recente_impede_nao_encontrado(raiz: Path) -> None:
    transporte = TransporteGravado(ATIVA, *VAZIAS[:1], _m("historias_recente.json"))
    assert _consulta(transporte, raiz).estado is EstadoConsulta.DESCONHECIDO


@pytest.mark.parametrize(
    "midia,historias",
    [
        ("midia_paginada_na_janela.json", "historias_vazia.json"),  # truncada na janela
        ("midia_vazia.json", "historias_paginada.json"),  # lista vazia mas com próxima página
        ("midia_desordenada_antiga.json", "historias_vazia.json"),  # ordem não decrescente
        ("midia_sem_timestamp.json", "historias_vazia.json"),
        ("midia_timestamp_invalido.json", "historias_vazia.json"),
        ("midia_sem_fuso.json", "historias_vazia.json"),
        ("midia_id_invalido.json", "historias_vazia.json"),
        ("midia_data_nao_lista.json", "historias_vazia.json"),
        ("midia_sem_data.json", "historias_vazia.json"),
        ("midia_malformada.json", "historias_vazia.json"),
        ("midia_vazia.json", "midia_malformada.json"),
        ("midia_vazia.json", "midia_sem_timestamp.json"),
    ],
)
def test_listagem_incompleta_ou_ilegivel_e_desconhecido(
    raiz: Path, midia: str, historias: str
) -> None:
    transporte = TransporteGravado(ATIVA, _m(midia), _m(historias))
    assert _consulta(transporte, raiz).estado is EstadoConsulta.DESCONHECIDO
    assert _sem_post(transporte)


@pytest.mark.parametrize(
    "falha",
    [
        (400, b'{"error":{"code":100,"message":"x"}}'),
        (401, b'{"error":{"code":190}}'),
        (403, b""),
        (404, b""),
        (429, b""),
        (500, b""),
        (503, b'{"error":{"code":2,"is_transient":true}}'),
        (302, b""),
        (200, b"nao e json"),
        (200, b"[]"),
        (200, b'{"error":{"code":1}}'),
        OSError("sem rede"),
        TimeoutError("lento"),
    ],
)
@pytest.mark.parametrize("onde", ["midia", "historias"])
def test_falha_na_listagem_e_desconhecido(raiz: Path, falha: Resposta, onde: str) -> None:
    respostas: list[Resposta] = [ATIVA, *VAZIAS]
    respostas[1 if onde == "midia" else 2] = falha
    transporte = TransporteGravado(*respostas)
    assert _consulta(transporte, raiz).estado is EstadoConsulta.DESCONHECIDO


@pytest.mark.parametrize(
    "conexao",
    [
        (200, _f("debug_token_expirado.json")),
        (200, _f("debug_token_invalido.json")),
        (200, _f("debug_token_malformada.json")),
        (200, _f("debug_token_sem_escopo.json")),
        (500, b""),
        OSError("sem rede"),
    ],
)
def test_conexao_invalida_e_desconhecido_sem_listar(raiz: Path, conexao: Resposta) -> None:
    transporte = TransporteGravado(conexao, *VAZIAS)
    assert _consulta(transporte, raiz).estado is EstadoConsulta.DESCONHECIDO
    assert len(transporte.chamadas) == 1  # nem tenta listar


def test_relogio_sem_fuso_ou_que_quebra_e_desconhecido(raiz: Path) -> None:
    sem_fuso = AdaptadorInstagram(
        SegredoStr(TOKEN),
        ID_CONTA,
        TransporteGravado(ATIVA, *VAZIAS),
        ArmazemLocal(raiz),
        lambda: datetime(2025, 12, 27, 21, 0),
    )
    assert sem_fuso.consultar(CHAVE).estado is EstadoConsulta.DESCONHECIDO

    def quebra() -> datetime:
        raise RuntimeError("relogio quebrado")

    quebrado = AdaptadorInstagram(
        SegredoStr(TOKEN), ID_CONTA, TransporteGravado(), ArmazemLocal(raiz), quebra
    )
    assert quebrado.consultar(CHAVE).estado is EstadoConsulta.DESCONHECIDO


@pytest.mark.parametrize("chave", ["", "   ", None, 123])
def test_chave_invalida_e_desconhecido_sem_rede(raiz: Path, chave: Any) -> None:
    transporte = TransporteGravado()
    assert _adaptador(transporte, raiz).consultar(chave).estado is EstadoConsulta.DESCONHECIDO
    assert transporte.chamadas == []


def test_transporte_inesperado_nunca_propaga_excecao(raiz: Path) -> None:
    class Quebrado:
        def obter(self, *a: Any) -> Any:
            raise RuntimeError("defeito do transporte")

    assert _consulta(Quebrado(), raiz).estado is EstadoConsulta.DESCONHECIDO


def test_janela_de_consulta_configuravel_e_validada(raiz: Path) -> None:
    corpo = json.dumps(
        {"data": [{"id": "17895000000000111", "timestamp": "2025-12-27T15:30:00+0000"}]}
    ).encode()
    # janela de 6 h (limite 15:00Z): a mídia das 15:30Z passa a ser possível correspondência
    transporte = TransporteGravado(ATIVA, (200, corpo), _m("historias_vazia.json"))
    longa = _adaptador(transporte, raiz, janela_consulta=timedelta(hours=6))
    assert longa.consultar(CHAVE).estado is EstadoConsulta.DESCONHECIDO
    for invalida in (timedelta(0), timedelta(hours=-1)):
        with pytest.raises(ValueError):
            _adaptador(TransporteGravado(), raiz, janela_consulta=invalida)


# --------------------------------------------------------------------------- I-1


def _tudo(adaptador: AdaptadorInstagram, r: object, caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join([repr(adaptador), str(adaptador), repr(r), str(r), caplog.text])


@pytest.mark.parametrize("modo", ["eco_4xx", "eco_5xx", "excecao", "eco_200"])
def test_token_e_corpo_bruto_nunca_aparecem(
    raiz: Path, caplog: pytest.LogCaptureFixture, modo: str
) -> None:
    caplog.set_level(logging.DEBUG)
    eco = f'{{"error":{{"message":"{TOKEN} {CORPO_UNICO}","code":100}}}}'.encode()
    falha: Resposta = {
        "eco_4xx": (400, eco),
        "eco_5xx": (500, eco),
        "excecao": OSError(f"falha {TOKEN} {CORPO_UNICO} Bearer {TOKEN}"),
        "eco_200": (
            200,
            f'{{"data":[],"x":"{TOKEN} {CORPO_UNICO}","paging":{{"next":"https://x/?access_token={TOKEN}"}}}}'.encode(),
        ),
    }[modo]
    transporte = TransporteGravado(ATIVA, falha, falha)
    adaptador = _adaptador(transporte, raiz)
    r = adaptador.consultar(CHAVE)
    assert r.estado is EstadoConsulta.DESCONHECIDO
    texto = _tudo(adaptador, r, caplog)
    assert TOKEN not in texto and CORPO_UNICO not in texto


def test_token_so_vai_no_cabecalho_da_listagem_nunca_na_url(raiz: Path) -> None:
    transporte = TransporteGravado(ATIVA, *VAZIAS)
    _consulta(transporte, raiz)
    for chamada in transporte.chamadas[1:]:
        assert TOKEN not in chamada.url
        assert chamada.cabecalhos["Authorization"] == f"Bearer {TOKEN}"


def test_paginacao_nunca_e_seguida(raiz: Path) -> None:
    transporte = TransporteGravado(
        ATIVA, _m("midia_paginada_na_janela.json"), _m("historias_vazia.json")
    )
    _consulta(transporte, raiz)
    # debug_token e media; a lista truncada já encerra em DESCONHECIDO e 'next' não é seguido
    assert [c.url.split("?")[0].rsplit("/", 1)[-1] for c in transporte.chamadas] == [
        "debug_token",
        "media",
    ]
    assert all("QVFFICTICIO" not in u for u in _urls(transporte))


# ----------------------------------------------------------------- contrato com o orquestrador


def _debug_valido(em: datetime) -> bytes:
    dados = {
        "data": {
            "is_valid": True,
            "expires_at": int((em + timedelta(days=40)).timestamp()),
            "scopes": ["instagram_basic", "instagram_content_publish"],
        }
    }
    return json.dumps(dados).encode()


def _lista(*timestamps: datetime) -> Resposta:
    itens = [
        {"id": f"1789500000000{i:04d}", "timestamp": t.strftime("%Y-%m-%dT%H:%M:%S+0000")}
        for i, t in enumerate(timestamps)
    ]
    return 200, json.dumps({"data": itens}).encode()


class AdaptadorReal(AdaptadorVerdade):
    """Plataforma de teste: ``publicar`` conta o envio; ``consultar`` é o do Instagram REAL
    (processo novo a cada rodada, sem andamento em memória), sobre um transporte gravado."""

    transporte_da_rodada: Any = None
    agora_da_rodada: datetime = AGORA_ORQ
    raiz_artes: Path = Path()

    def consultar(self, chave_idempotencia: str) -> ResultadoConsulta:
        self._eventos.append(("consultar", self.plataforma, chave_idempotencia))
        self.consultas.append(chave_idempotencia)
        real = AdaptadorInstagram(
            SegredoStr(TOKEN),
            ID_CONTA,
            self.transporte_da_rodada,
            ArmazemLocal(self.raiz_artes),
            lambda: self.agora_da_rodada,
        )
        return real.consultar(chave_idempotencia)


class MundoReal(Mundo):
    transporte: Any = None

    def adaptador(self) -> AdaptadorVerdade:
        novo = AdaptadorReal(
            "Instagram",
            self.notion.eventos,
            self.verdade,
            self.roteiro,
            "verdade",
            armazem=self.armazem,
        )
        novo.transporte_da_rodada = self.transporte
        novo.agora_da_rodada = self._instante
        novo.raiz_artes = self.armazem._raiz  # type: ignore[attr-defined]
        self.adaptadores.append(novo)
        return novo

    _instante: datetime = AGORA_ORQ

    def rodar(self, minutos: float = 0, **kw: Any) -> Any:  # type: ignore[override]
        self._instante = AGORA_ORQ + timedelta(minutes=minutos)
        return super().rodar(minutos, **kw)


def _mundo_real(armazem: ArmazemLocal, *roteiro: tuple[Any, bool]) -> MundoReal:
    notion = _cenario(armazem, (P1, _props()))
    return MundoReal(notion, armazem, roteiro=list(roteiro))


@pytest.fixture
def armazem_orq(tmp_path: Path) -> ArmazemLocal:
    from test_orquestrador import ARTE as ARTE_ORQ

    destino = tmp_path / "artes" / ARTE_ORQ
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_bytes(b"\x89PNG ficticio 1")
    return ArmazemLocal(tmp_path / "artes")


def test_contrato_listagem_vazia_libera_a_nova_tentativa(armazem_orq: ArmazemLocal) -> None:
    mundo = _mundo_real(armazem_orq, (T.FALHA_TRANSITORIA, False), (T.SUCESSO, True))
    mundo.rodar(0)
    depois = AGORA_ORQ + timedelta(minutes=31)
    mundo.transporte = TransporteGravado(
        (200, _debug_valido(depois)), (200, b'{"data":[]}'), (200, b'{"data":[]}')
    )

    rel = mundo.rodar(31)

    assert rel.ocorrencia(P1, "Instagram").desfecho is Desfecho.SUCESSO
    assert mundo.verdade.envios_reais == 2 and len(mundo.verdade.saidas) == 1
    assert [e["tentativa"] for e in _sem_recusas(mundo.notion)] == ["1", "2"]


@pytest.mark.parametrize(
    "respostas",
    [
        "midia_recente",  # há mídia na janela: pode ser a nossa
        "listagem_500",
        "conexao_invalida",
        "json_malformado",
    ],
)
def test_contrato_incerto_do_adaptador_real_nunca_gera_segundo_envio(
    armazem_orq: ArmazemLocal, respostas: str
) -> None:
    mundo = _mundo_real(armazem_orq, (T.FALHA_TRANSITORIA, True))
    mundo.rodar(0)
    depois = AGORA_ORQ + timedelta(minutes=31)
    valido = (200, _debug_valido(depois))
    transporte = {
        "midia_recente": TransporteGravado(
            valido, _lista(depois - timedelta(minutes=35)), (200, b'{"data":[]}')
        ),
        "listagem_500": TransporteGravado(valido, (500, b""), (500, b"")),
        "conexao_invalida": TransporteGravado((401, b'{"error":{"code":190}}')),
        "json_malformado": TransporteGravado(valido, (200, b"{"), (200, b"{")),
    }[respostas]
    mundo.transporte = transporte

    rel = mundo.rodar(31)

    oc = rel.ocorrencia(P1, "Instagram")
    assert oc.desfecho is Desfecho.INCERTO and oc.motivo == "plataforma_nao_confirmou"
    assert _sem_recusas(mundo.notion)[-1]["resultado"] == "incerto"
    for minutos in (45, 70, 100):
        mundo.transporte = TransporteGravado()  # qualquer chamada nova quebraria o teste
        mundo.rodar(minutos)
    assert mundo.verdade.envios_reais == 1  # um único envio, nunca um segundo
    assert all(c.metodo == "GET" for c in transporte.chamadas)


def test_contrato_sanidade_do_helper_de_execucoes(armazem_orq: ArmazemLocal) -> None:
    mundo = _mundo_real(armazem_orq, (T.SUCESSO, True))
    mundo.rodar(0)
    assert [e["resultado"] for e in _execucoes(mundo.notion) if e["resultado"] != "recusado"] == [
        "sucesso"
    ]
