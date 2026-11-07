"""Testes dos modelos de domínio (T008). Só valores fictícios."""

import dataclasses
from datetime import UTC, datetime
from typing import Any

import pytest

from publicador.core import modelos
from publicador.core.modelos import (
    NOME_ESTAGIO_AUTORIZADOR,
    Conexao,
    ConexaoEstado,
    EstadoExecucao,
    Estagio,
    Execucao,
    Formato,
    MotivoRecusa,
    PapelEstagio,
    Plataforma,
    Post,
    ResultadoGuarda,
    Serie,
    Tema,
)

AGORA = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
INGENUO = datetime(2026, 11, 7, 12, 0)


def _post(**extra: Any) -> Post:
    base: dict[str, Any] = {
        "id": "p1",
        "titulo": "Titulo",
        "corpo": "Corpo",
        "estagio": Estagio("pauta", PapelEstagio.PAUTA),
        "plataformas": ("instagram",),
    }
    base.update(extra)
    return Post(**base)


def test_papeis_fixos() -> None:
    assert {p.value for p in PapelEstagio} == {
        "pauta",
        "pronto",
        "autorizado",
        "publicado",
        "falha",
        "cancelado",
    }
    assert len(PapelEstagio) == 6


def test_nome_autorizador_constante() -> None:
    assert NOME_ESTAGIO_AUTORIZADOR == "confirmado"
    assert Estagio("confirmado", PapelEstagio.AUTORIZADO).papel is PapelEstagio.AUTORIZADO


def test_autorizado_com_outro_nome_rejeitado() -> None:
    with pytest.raises(ValueError):
        Estagio("aprovado", PapelEstagio.AUTORIZADO)


def test_confirmado_com_outro_papel_rejeitado() -> None:
    with pytest.raises(ValueError):
        Estagio("confirmado", PapelEstagio.PRONTO)


def test_estagio_nome_vazio() -> None:
    for nome in ("", "   "):
        with pytest.raises(ValueError):
            Estagio(nome, PapelEstagio.PAUTA)


def test_estagio_nome_livre_para_outros_papeis() -> None:
    assert Estagio("Rascunho", PapelEstagio.PAUTA).nome == "Rascunho"


def test_plataforma_valida() -> None:
    assert Plataforma("instagram").chave == "instagram"
    with pytest.raises(ValueError):
        Plataforma(" ")


def test_nomes_nao_vazios() -> None:
    assert Formato("reels").nome == "reels"
    assert Tema("dados").nome == "dados"
    assert Serie("s", continuidade=True).continuidade is True
    for fabrica in (Formato, Tema):
        with pytest.raises(ValueError):
            fabrica("")
    with pytest.raises(ValueError):
        Serie("", continuidade=False)


@pytest.mark.parametrize("campo", ["publicar_em", "confirmado_em", "editado_em"])
def test_datetime_ingenuo_rejeitado(campo: str) -> None:
    with pytest.raises(ValueError):
        _post(**{campo: INGENUO})
    assert getattr(_post(**{campo: AGORA}), campo) == AGORA


def test_post_sem_data_e_valido() -> None:
    post = _post()
    assert post.publicar_em is None
    assert post.tem_data_e_hora() is False
    assert post.selo is None and post.artes == ()


def test_tem_data_e_hora() -> None:
    assert _post(publicar_em=AGORA).tem_data_e_hora() is True


def test_post_titulo_vazio() -> None:
    with pytest.raises(ValueError):
        _post(titulo="  ")


def test_post_imutavel() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        _post().titulo = "x"  # type: ignore[misc]


def test_conexao() -> None:
    c = Conexao("instagram", ConexaoEstado.ATIVA, None, None)
    assert c.estado is ConexaoEstado.ATIVA
    with pytest.raises(ValueError):
        Conexao("", ConexaoEstado.ATIVA, None, None)
    with pytest.raises(ValueError):
        Conexao("instagram", ConexaoEstado.ATIVA, INGENUO, None)
    with pytest.raises(ValueError):
        Conexao("instagram", ConexaoEstado.ATIVA, None, INGENUO)


def test_estados_de_execucao() -> None:
    assert {e.name for e in EstadoExecucao} == {
        "INICIADA",
        "SUCESSO",
        "SUCESSO_MODO_PRIVADO",
        "FALHA",
        "RECUSADA",
        "INCERTO",
        "NAO_EXECUTADO",
        "SIMULADA",
    }


def _execucao(tentativa: int = 1, **extra: Any) -> Execucao:
    base: dict[str, Any] = {
        "chave_idempotencia": "k",
        "post_id": "p1",
        "plataforma": "instagram",
        "estado": EstadoExecucao.INICIADA,
        "tentativa": tentativa,
        "iniciada_em": AGORA,
        "finalizada_em": None,
        "motivo": None,
    }
    base.update(extra)
    return Execucao(**base)


def test_execucao_tentativa_minima() -> None:
    assert _execucao(1).tentativa == 1
    for t in (0, -1):
        with pytest.raises(ValueError):
            _execucao(t)


def test_execucao_datas_com_fuso() -> None:
    with pytest.raises(ValueError):
        _execucao(iniciada_em=INGENUO)
    with pytest.raises(ValueError):
        _execucao(finalizada_em=INGENUO)


def test_motivos_recusa() -> None:
    nomes = [
        "LEITURA_FALHOU", "CONFIG_INCONSISTENTE", "ESTAGIO_NAO_AUTORIZADO", "SEM_DATA",
        "SEM_HORA", "DATA_FUTURA", "SELO_AUSENTE", "SELO_DIVERGENTE", "FORA_DA_JANELA",
        "POST_POSTERIOR_JA_SAIU", "SERIE_BLOQUEADA", "SEM_ADAPTADOR", "FORMATO_SEM_TIPO",
        "CONEXAO_INVALIDA", "ARTE_INVALIDA", "EXECUCAO_EXISTENTE",
    ]  # fmt: skip
    assert [m.name for m in MotivoRecusa] == nomes
    assert all(m.value == m.name.lower() for m in MotivoRecusa)


def test_resultado_permitir() -> None:
    r = ResultadoGuarda.permitir()
    assert r.permitido is True and r.motivos == () and r.detalhe is None


def test_resultado_recusar() -> None:
    r = ResultadoGuarda.recusar(MotivoRecusa.SEM_DATA, MotivoRecusa.SELO_AUSENTE, detalhe="x")
    assert r.permitido is False
    assert r.motivos == (MotivoRecusa.SEM_DATA, MotivoRecusa.SELO_AUSENTE)
    assert r.detalhe == "x"


def test_resultado_recusar_sem_motivo() -> None:
    with pytest.raises(ValueError):
        ResultadoGuarda.recusar()


def test_resultado_invariantes_no_construtor() -> None:
    with pytest.raises(ValueError):
        ResultadoGuarda(True, (MotivoRecusa.SEM_DATA,), None)
    with pytest.raises(ValueError):
        ResultadoGuarda(False, (), None)


def test_nenhum_campo_de_credencial() -> None:
    proibidos = ("token", "senha", "segredo", "credencial", "secret", "password")
    classes = [
        obj
        for obj in vars(modelos).values()
        if dataclasses.is_dataclass(obj) and isinstance(obj, type)
    ]
    assert len(classes) >= 8
    for classe in classes:
        for campo in dataclasses.fields(classe):
            assert not any(p in campo.name.lower() for p in proibidos), (classe, campo.name)
