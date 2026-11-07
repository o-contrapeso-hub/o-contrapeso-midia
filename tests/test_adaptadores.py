"""Testes da interface PlatformAdapter e do adaptador falso/simulação (T036, RF-17).

Regras: I-1 (nenhuma credencial em repr/str/log/erro; valores fictícios) e I-2 (o
adaptador não decide autorização; a simulação aplica as mesmas validações e não envia
nada). Nenhum teste usa a rede.
"""

from __future__ import annotations

import logging
import socket
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn

import pytest

from publicador.adaptadores.base import (
    EstadoConexao,
    EstadoConsulta,
    PedidoDePublicacao,
    PlatformAdapter,
    ProblemaDeMidia,
    ResultadoConsulta,
    ResultadoPublicacao,
    ResultadoValidacao,
    TipoDeProblema,
    TipoDeResultado,
)
from publicador.adaptadores.falso import AdaptadorFalso
from publicador.artes.armazem import ArmazemLocal
from publicador.core.modelos import ConexaoEstado, Estagio, PapelEstagio, Post
from publicador.core.redacao import SegredoStr, limpar_segredos_registrados

TOKEN_FICTICIO = "tok_ficticio_ZXCV1234abcd"
TEXTO_SECRETO_DO_POST = "Roteiro ficticio ULTRA-SECRETO sobre energia"
REELS = "serie-a/2026/11/video_01.mp4"
FEED_1 = "serie-a/2026/11/arte_01.png"
FEED_2 = "serie-a/2026/11/arte_02.jpg"
CHAVE = "post-ficticio-0001|instagram|2026-11-07T18:30:00-03:00"


@pytest.fixture(autouse=True)
def _limpar_segredos() -> Any:
    yield
    limpar_segredos_registrados()


@pytest.fixture
def armazem(tmp_path: Path) -> ArmazemLocal:
    raiz = tmp_path / "artes"
    for caminho in (REELS, FEED_1, FEED_2):
        destino = raiz / caminho
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(b"conteudo ficticio " + caminho.encode())
    return ArmazemLocal(raiz)


def _post(**mudancas: Any) -> Post:
    base: dict[str, Any] = {
        "id": "post-ficticio-0001",
        "titulo": "Titulo ficticio",
        "corpo": TEXTO_SECRETO_DO_POST,
        "estagio": Estagio("confirmado", PapelEstagio.AUTORIZADO),
        "plataformas": ("instagram", "tiktok"),
        "formato": "Feed",
        "artes": (FEED_1, FEED_2),
    }
    base.update(mudancas)
    return Post(**base)


def _pedido(
    post: Post | None = None, plataforma: str = "instagram", **kw: Any
) -> PedidoDePublicacao:
    post = post or _post()
    artes = kw.pop("artes", post.artes)
    return PedidoDePublicacao(
        post=post,
        plataforma=plataforma,
        artes=tuple(artes),
        chave_idempotencia=kw.pop("chave", CHAVE),
        **kw,
    )


def _falso(armazem: ArmazemLocal, **kw: Any) -> AdaptadorFalso:
    return AdaptadorFalso("instagram", armazem=armazem, **kw)


# --------------------------------------------------------------------------- interface


def test_falso_cumpre_a_interface(armazem: ArmazemLocal) -> None:
    adaptador = _falso(armazem)
    assert isinstance(adaptador, PlatformAdapter)
    assert adaptador.plataforma == "instagram"
    for nome in (
        "verificar_conexao",
        "validar_midia",
        "limite_diario_restante",
        "publicar",
        "consultar",
    ):
        assert callable(getattr(adaptador, nome))


def test_objeto_incompleto_nao_e_adaptador() -> None:
    class Incompleto:
        plataforma = "x"

    assert not isinstance(Incompleto(), PlatformAdapter)


def test_metodos_devolvem_os_tipos_da_interface(armazem: ArmazemLocal) -> None:
    adaptador = _falso(armazem, limite_diario=5)
    post = _post()
    assert adaptador.verificar_conexao().estado is ConexaoEstado.ATIVA
    assert isinstance(adaptador.validar_midia(post, post.artes), ResultadoValidacao)
    assert adaptador.limite_diario_restante() == 5
    assert isinstance(adaptador.publicar(_pedido(post)), ResultadoPublicacao)
    assert isinstance(adaptador.consultar(CHAVE), ResultadoConsulta)


def test_limite_diario_desconhecido_e_none(armazem: ArmazemLocal) -> None:
    assert _falso(armazem).limite_diario_restante() is None


# --------------------------------------------------------------------------- sem rede


def test_simulacao_nao_abre_socket_nem_rede(
    armazem: ArmazemLocal, monkeypatch: pytest.MonkeyPatch
) -> None:
    chamadas: list[str] = []

    def proibido(*_a: Any, **_k: Any) -> NoReturn:
        chamadas.append("rede")
        raise AssertionError("rede proibida na simulação")

    monkeypatch.setattr(socket, "socket", proibido)
    monkeypatch.setattr(socket, "create_connection", proibido)
    monkeypatch.setattr(socket, "getaddrinfo", proibido)
    monkeypatch.setattr(urllib.request, "urlopen", proibido)

    adaptador = _falso(armazem, limite_diario=3)
    post = _post()
    adaptador.verificar_conexao()
    adaptador.validar_midia(post, post.artes)
    adaptador.limite_diario_restante()
    resultado = adaptador.publicar(_pedido(post))
    adaptador.consultar(CHAVE)
    assert resultado.tipo is TipoDeResultado.SUCESSO
    assert chamadas == []


# --------------------------------------------------------------------------- mídia


def test_midia_valida(armazem: ArmazemLocal) -> None:
    post = _post()
    validacao = _falso(armazem).validar_midia(post, post.artes)
    assert validacao.valida
    assert validacao.problemas == ()


@pytest.mark.parametrize(
    ("mudancas", "artes", "tipo"),
    [
        ({"formato": None}, (FEED_1,), TipoDeProblema.FORMATO_AUSENTE),
        ({"formato": "Podcast"}, (FEED_1,), TipoDeProblema.FORMATO_NAO_SUPORTADO),
        ({"formato": "Feed"}, (), TipoDeProblema.QUANTIDADE_DE_ARTES),
        ({"formato": "Feed"}, (FEED_1,) * 11, TipoDeProblema.QUANTIDADE_DE_ARTES),
        ({"formato": "Feed"}, ("../fora.png",), TipoDeProblema.CAMINHO_INVALIDO),
        (
            {"formato": "Feed"},
            ("serie-a/2026/11/nao_existe.png",),
            TipoDeProblema.ARTE_NAO_ENCONTRADA,
        ),
        ({"formato": "Feed"}, (REELS,), TipoDeProblema.EXTENSAO_NAO_SUPORTADA),
        ({"formato": "Reels"}, (FEED_1,), TipoDeProblema.EXTENSAO_NAO_SUPORTADA),
        ({"plataformas": ("tiktok",)}, (FEED_1,), TipoDeProblema.PLATAFORMA_FORA_DO_POST),
    ],
)
def test_midia_invalida_gera_problemas_tipados(
    armazem: ArmazemLocal, mudancas: dict[str, Any], artes: tuple[str, ...], tipo: TipoDeProblema
) -> None:
    post = _post(**mudancas)
    validacao = _falso(armazem).validar_midia(post, artes)
    assert not validacao.valida
    assert tipo in {p.tipo for p in validacao.problemas}
    assert all(isinstance(p, ProblemaDeMidia) for p in validacao.problemas)


def test_stories_so_no_instagram(armazem: ArmazemLocal) -> None:
    post = _post(formato="Stories", artes=(FEED_1,))
    assert AdaptadorFalso("instagram", armazem=armazem).validar_midia(post, post.artes).valida
    tiktok = AdaptadorFalso("tiktok", armazem=armazem).validar_midia(post, post.artes)
    assert TipoDeProblema.FORMATO_NAO_SUPORTADO in {p.tipo for p in tiktok.problemas}


def test_problemas_nao_carregam_conteudo_do_post(armazem: ArmazemLocal) -> None:
    post = _post(formato="Podcast", titulo="Titulo ficticio UNICO-987")
    validacao = _falso(armazem).validar_midia(post, post.artes)
    texto = repr(validacao) + str(validacao)
    assert TEXTO_SECRETO_DO_POST not in texto
    assert "UNICO-987" not in texto


def test_midia_invalida_na_publicacao_nada_e_enviado(armazem: ArmazemLocal) -> None:
    adaptador = _falso(armazem)
    post = _post(artes=("serie-a/2026/11/nao_existe.png",))
    resultado = adaptador.publicar(_pedido(post))
    assert resultado.tipo is TipoDeResultado.FALHA_PERMANENTE
    assert resultado.id_externo is None
    assert adaptador.publicacoes_efetivas == 0
    assert adaptador.consultar(CHAVE).estado is EstadoConsulta.NAO_ENCONTRADO


def test_conexao_invalida_na_publicacao_nada_e_enviado(armazem: ArmazemLocal) -> None:
    for estado in (ConexaoEstado.EXPIRADA, ConexaoEstado.INVALIDA):
        adaptador = _falso(armazem, estado_conexao=estado)
        assert adaptador.verificar_conexao().estado is estado
        resultado = adaptador.publicar(_pedido())
        assert resultado.tipo is TipoDeResultado.FALHA_PERMANENTE
        assert adaptador.publicacoes_efetivas == 0


def test_conexao_informa_dias_restantes(armazem: ArmazemLocal) -> None:
    adaptador = _falso(armazem, dias_restantes_conexao=12)
    conexao = adaptador.verificar_conexao()
    assert conexao.dias_restantes == 12
    assert conexao.valida


def test_limite_diario_esgotado_e_falha_transitoria(armazem: ArmazemLocal) -> None:
    adaptador = _falso(armazem, limite_diario=1)
    primeiro = adaptador.publicar(_pedido(chave="k1"))
    assert primeiro.tipo is TipoDeResultado.SUCESSO
    assert adaptador.limite_diario_restante() == 0
    segundo = adaptador.publicar(_pedido(chave="k2"))
    assert segundo.tipo is TipoDeResultado.FALHA_TRANSITORIA


# --------------------------------------------------------------------------- publicação


def test_simulacao_devolve_sucesso_simulado_com_id_ficticio(armazem: ArmazemLocal) -> None:
    adaptador = _falso(armazem)
    resultado = adaptador.publicar(_pedido())
    assert resultado.tipo is TipoDeResultado.SUCESSO
    assert resultado.simulado
    assert resultado.id_externo is not None
    assert resultado.id_externo.startswith("simulado-")
    assert resultado.chave_idempotencia == CHAVE
    assert resultado.url is not None
    assert ".invalid/" in resultado.url


def test_pedido_nao_simulado_nao_marca_simulado(armazem: ArmazemLocal) -> None:
    resultado = _falso(armazem).publicar(_pedido(simular=False))
    assert resultado.tipo is TipoDeResultado.SUCESSO
    assert not resultado.simulado


def test_pedido_e_simulacao_por_padrao(armazem: ArmazemLocal) -> None:
    assert _pedido().simular is True


def test_idempotencia_mesma_chave_nao_duplica(armazem: ArmazemLocal) -> None:
    adaptador = _falso(armazem, limite_diario=10)
    a = adaptador.publicar(_pedido())
    b = adaptador.publicar(_pedido())
    assert a == b
    assert adaptador.publicacoes_efetivas == 1
    assert adaptador.limite_diario_restante() == 9
    assert [c.reaproveitada for c in adaptador.chamadas if c.metodo == "publicar"] == [
        False,
        True,
    ]


def test_chaves_diferentes_publicam_separado(armazem: ArmazemLocal) -> None:
    adaptador = _falso(armazem)
    a = adaptador.publicar(_pedido(chave="k1"))
    b = adaptador.publicar(_pedido(chave="k2"))
    assert a.id_externo != b.id_externo
    assert adaptador.publicacoes_efetivas == 2


def test_consulta_apos_publicar_encontra(armazem: ArmazemLocal) -> None:
    adaptador = _falso(armazem)
    resultado = adaptador.publicar(_pedido())
    consulta = adaptador.consultar(CHAVE)
    assert consulta.estado is EstadoConsulta.ENCONTRADO
    assert consulta.id_externo == resultado.id_externo


def test_consulta_sem_publicacao_nao_encontra(armazem: ArmazemLocal) -> None:
    assert _falso(armazem).consultar("outra").estado is EstadoConsulta.NAO_ENCONTRADO


# --------------------------------------------------------------------------- programadas


@pytest.mark.parametrize(
    "tipo",
    [
        TipoDeResultado.FALHA_TRANSITORIA,
        TipoDeResultado.FALHA_PERMANENTE,
        TipoDeResultado.INCERTO,
        TipoDeResultado.SUCESSO_MODO_PRIVADO,
        TipoDeResultado.SUCESSO,
    ],
)
def test_resposta_programada_de_publicacao(armazem: ArmazemLocal, tipo: TipoDeResultado) -> None:
    adaptador = _falso(armazem)
    adaptador.programar_publicacao(tipo)
    resultado = adaptador.publicar(_pedido())
    assert resultado.tipo is tipo
    assert resultado.chave_idempotencia == CHAVE
    if tipo in (TipoDeResultado.SUCESSO, TipoDeResultado.SUCESSO_MODO_PRIVADO):
        assert resultado.id_externo is not None
    else:
        assert resultado.id_externo is None


def test_respostas_programadas_sao_consumidas_em_ordem(armazem: ArmazemLocal) -> None:
    adaptador = _falso(armazem)
    adaptador.programar_publicacao(TipoDeResultado.FALHA_TRANSITORIA, TipoDeResultado.SUCESSO)
    assert adaptador.publicar(_pedido()).tipo is TipoDeResultado.FALHA_TRANSITORIA
    # falha transitória não é gravada como publicada: nova tentativa com a mesma chave segue
    assert adaptador.publicar(_pedido()).tipo is TipoDeResultado.SUCESSO
    assert adaptador.publicacoes_efetivas == 1


def test_incerto_e_reaproveitado_e_consulta_fica_desconhecida(armazem: ArmazemLocal) -> None:
    adaptador = _falso(armazem)
    adaptador.programar_publicacao(TipoDeResultado.INCERTO)
    a = adaptador.publicar(_pedido())
    b = adaptador.publicar(_pedido())
    assert a == b
    assert b.tipo is TipoDeResultado.INCERTO
    assert adaptador.consultar(CHAVE).estado is EstadoConsulta.DESCONHECIDO


def test_modo_privado_e_encontrado_na_consulta(armazem: ArmazemLocal) -> None:
    adaptador = _falso(armazem)
    adaptador.programar_publicacao(TipoDeResultado.SUCESSO_MODO_PRIVADO)
    adaptador.publicar(_pedido())
    assert adaptador.consultar(CHAVE).estado is EstadoConsulta.ENCONTRADO


@pytest.mark.parametrize("estado", list(EstadoConsulta))
def test_resposta_programada_de_consulta(armazem: ArmazemLocal, estado: EstadoConsulta) -> None:
    adaptador = _falso(armazem)
    adaptador.programar_consulta(estado)
    consulta = adaptador.consultar(CHAVE)
    assert consulta.estado is estado
    assert (consulta.id_externo is not None) is (estado is EstadoConsulta.ENCONTRADO)


def test_programar_sucesso_com_midia_invalida_ainda_valida(armazem: ArmazemLocal) -> None:
    adaptador = _falso(armazem)
    adaptador.programar_publicacao(TipoDeResultado.SUCESSO)
    resultado = adaptador.publicar(_pedido(_post(formato="Podcast")))
    assert resultado.tipo is TipoDeResultado.FALHA_PERMANENTE
    assert adaptador.publicacoes_efetivas == 0


# --------------------------------------------------------------------------- tipos


def test_sucesso_exige_id_externo() -> None:
    with pytest.raises(ValueError):
        ResultadoPublicacao(tipo=TipoDeResultado.SUCESSO, chave_idempotencia="k")


def test_consulta_encontrada_exige_id() -> None:
    with pytest.raises(ValueError):
        ResultadoConsulta(estado=EstadoConsulta.ENCONTRADO)


def test_chave_vazia_e_rejeitada() -> None:
    with pytest.raises(ValueError):
        PedidoDePublicacao(post=_post(), plataforma="instagram", artes=(), chave_idempotencia=" ")


def test_url_com_credencial_e_rejeitada() -> None:
    with pytest.raises(ValueError):
        ResultadoPublicacao(
            tipo=TipoDeResultado.SUCESSO,
            chave_idempotencia="k",
            id_externo="1",
            url="https://usuario:senha@exemplo.invalid/p/1",
        )


def test_conexao_dias_restantes_calculados() -> None:
    agora = datetime(2026, 11, 7, tzinfo=UTC)
    estado = EstadoConexao.de_expiracao("instagram", agora + timedelta(days=10, hours=3), agora)
    assert estado.dias_restantes == 10
    expirada = EstadoConexao.de_expiracao("instagram", agora - timedelta(days=1), agora)
    assert expirada.estado is ConexaoEstado.EXPIRADA
    assert not expirada.valida


# --------------------------------------------------------------------------- I-1


def test_repr_str_e_log_nao_vazam_token_nem_texto(
    armazem: ArmazemLocal, caplog: pytest.LogCaptureFixture
) -> None:
    token = SegredoStr(TOKEN_FICTICIO)
    adaptador = _falso(armazem, credencial=token, limite_diario=2)
    pedido = _pedido()
    caplog.set_level(logging.DEBUG, logger="publicador")
    resultados: list[object] = [
        adaptador.verificar_conexao(),
        adaptador.validar_midia(pedido.post, pedido.artes),
        adaptador.publicar(pedido),
        adaptador.publicar(_pedido(_post(formato="Podcast"), chave="k2")),
        adaptador.consultar(CHAVE),
    ]
    saidas = [repr(adaptador), str(adaptador), repr(pedido), str(pedido), caplog.text]
    saidas += [repr(r) + str(r) for r in resultados]
    saidas += [repr(c) for c in adaptador.chamadas]
    junto = "\n".join(saidas)
    assert TOKEN_FICTICIO not in junto
    assert TEXTO_SECRETO_DO_POST not in junto
    assert "Titulo ficticio" not in junto


def test_registro_de_chamadas_nao_guarda_conteudo(armazem: ArmazemLocal) -> None:
    adaptador = _falso(armazem)
    adaptador.publicar(_pedido())
    adaptador.consultar(CHAVE)
    assert [c.metodo for c in adaptador.chamadas] == ["publicar", "consultar"]
    assert all(c.plataforma == "instagram" for c in adaptador.chamadas)
    assert TEXTO_SECRETO_DO_POST not in repr(adaptador.chamadas)
