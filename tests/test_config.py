"""Testes do carregador de parametros (publicador.core.config).

Todos os valores usados aqui sao ficticios. Nenhum teste le segredos.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest
import yaml

from publicador.core.config import ParametrosInvalidos, carregar_parametros

CAMPOS = (
    "janela_tolerancia_minutos",
    "nova_tentativa_minutos",
    "meta_pontualidade_minutos",
    "fuso_padrao",
    "limite_silencio_vigia_minutos",
)


@pytest.fixture(autouse=True)
def _sem_variavel_de_ambiente(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PUBLICADOR_PARAMETROS", raising=False)


def _base() -> dict[str, Any]:
    return {
        "janela_tolerancia_minutos": 120,
        "nova_tentativa_minutos": 30,
        "meta_pontualidade_minutos": 30,
        "fuso_padrao": "America/Sao_Paulo",
        "limite_silencio_vigia_minutos": 60,
    }


def _arquivo(tmp_path: Path, dados: object) -> Path:
    caminho = tmp_path / "parametros.yaml"
    caminho.write_text(yaml.safe_dump(dados), encoding="utf-8")
    return caminho


def test_carrega_parametros_padrao() -> None:
    p = carregar_parametros()

    assert p.janela_tolerancia_minutos == 120
    assert p.nova_tentativa_minutos == 30
    assert p.meta_pontualidade_minutos == 30
    assert p.fuso_padrao == "America/Sao_Paulo"
    assert p.limite_silencio_vigia_minutos == 60


def test_parametros_sao_imutaveis() -> None:
    p = carregar_parametros()

    with pytest.raises(dataclasses.FrozenInstanceError):
        p.janela_tolerancia_minutos = 1  # type: ignore[misc]


def test_carrega_arquivo_informado(tmp_path: Path) -> None:
    caminho = _arquivo(tmp_path, _base() | {"janela_tolerancia_minutos": 200})

    p = carregar_parametros(caminho)

    assert p.janela_tolerancia_minutos == 200


def test_variavel_de_ambiente_sobrescreve_caminho_padrao(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    caminho = _arquivo(tmp_path, _base() | {"nova_tentativa_minutos": 10})
    monkeypatch.setenv("PUBLICADOR_PARAMETROS", str(caminho))

    p = carregar_parametros()

    assert p.nova_tentativa_minutos == 10


def test_rejeita_campo_faltando(tmp_path: Path) -> None:
    dados = _base()
    del dados["fuso_padrao"]

    with pytest.raises(ParametrosInvalidos, match="fuso_padrao"):
        carregar_parametros(_arquivo(tmp_path, dados))


def test_rejeita_campo_desconhecido(tmp_path: Path) -> None:
    dados = _base() | {"campo_inventado": 5}

    with pytest.raises(ParametrosInvalidos, match="campo_inventado"):
        carregar_parametros(_arquivo(tmp_path, dados))


@pytest.mark.parametrize("campo", CAMPOS[:3] + CAMPOS[4:])
@pytest.mark.parametrize("valor", [0, -5])
def test_rejeita_minutos_nao_positivos(tmp_path: Path, campo: str, valor: int) -> None:
    dados = _base() | {campo: valor}

    with pytest.raises(ParametrosInvalidos, match=campo):
        carregar_parametros(_arquivo(tmp_path, dados))


@pytest.mark.parametrize("campo", CAMPOS[:3] + CAMPOS[4:])
@pytest.mark.parametrize("valor", ["120", "abc", None, [60]])
def test_rejeita_minutos_que_nao_sao_inteiros(tmp_path: Path, campo: str, valor: object) -> None:
    dados = _base() | {campo: valor}

    with pytest.raises(ParametrosInvalidos, match=campo):
        carregar_parametros(_arquivo(tmp_path, dados))


@pytest.mark.parametrize("campo", CAMPOS[:3] + CAMPOS[4:])
def test_rejeita_bool_como_minutos(tmp_path: Path, campo: str) -> None:
    dados = _base() | {campo: True}

    with pytest.raises(ParametrosInvalidos, match=campo):
        carregar_parametros(_arquivo(tmp_path, dados))


@pytest.mark.parametrize("campo", CAMPOS[:3] + CAMPOS[4:])
def test_rejeita_float_como_minutos(tmp_path: Path, campo: str) -> None:
    dados = _base() | {campo: 30.5}

    with pytest.raises(ParametrosInvalidos, match=campo):
        carregar_parametros(_arquivo(tmp_path, dados))


def test_rejeita_nova_tentativa_igual_a_janela(tmp_path: Path) -> None:
    dados = _base() | {"nova_tentativa_minutos": 120, "janela_tolerancia_minutos": 120}

    with pytest.raises(ParametrosInvalidos, match="nova_tentativa_minutos"):
        carregar_parametros(_arquivo(tmp_path, dados))


def test_rejeita_nova_tentativa_maior_que_janela(tmp_path: Path) -> None:
    dados = _base() | {"nova_tentativa_minutos": 150, "janela_tolerancia_minutos": 120}

    with pytest.raises(ParametrosInvalidos, match="nova_tentativa_minutos"):
        carregar_parametros(_arquivo(tmp_path, dados))


def test_rejeita_meta_maior_que_janela(tmp_path: Path) -> None:
    dados = _base() | {"meta_pontualidade_minutos": 121, "janela_tolerancia_minutos": 120}

    with pytest.raises(ParametrosInvalidos, match="meta_pontualidade_minutos"):
        carregar_parametros(_arquivo(tmp_path, dados))


def test_aceita_meta_igual_a_janela(tmp_path: Path) -> None:
    dados = _base() | {"meta_pontualidade_minutos": 120}

    p = carregar_parametros(_arquivo(tmp_path, dados))

    assert p.meta_pontualidade_minutos == 120


def test_rejeita_fuso_invalido(tmp_path: Path) -> None:
    dados = _base() | {"fuso_padrao": "Mars/Olympus_Mons"}

    with pytest.raises(ParametrosInvalidos, match="fuso_padrao"):
        carregar_parametros(_arquivo(tmp_path, dados))


def test_rejeita_fuso_que_nao_e_texto(tmp_path: Path) -> None:
    dados = _base() | {"fuso_padrao": -3}

    with pytest.raises(ParametrosInvalidos, match="fuso_padrao"):
        carregar_parametros(_arquivo(tmp_path, dados))


def test_rejeita_arquivo_ausente(tmp_path: Path) -> None:
    with pytest.raises(ParametrosInvalidos, match="encontrado"):
        carregar_parametros(tmp_path / "nao_existe.yaml")


def test_rejeita_yaml_vazio(tmp_path: Path) -> None:
    caminho = tmp_path / "parametros.yaml"
    caminho.write_text("", encoding="utf-8")

    with pytest.raises(ParametrosInvalidos, match="vazio"):
        carregar_parametros(caminho)


def test_rejeita_yaml_que_e_lista(tmp_path: Path) -> None:
    caminho = _arquivo(tmp_path, ["janela_tolerancia_minutos", 120])

    with pytest.raises(ParametrosInvalidos, match="mapa"):
        carregar_parametros(caminho)


def test_rejeita_yaml_ilegivel_sem_ecoar_conteudo(tmp_path: Path) -> None:
    segredo_ficticio = "SEGREDO-FICTICIO-12345"
    caminho = tmp_path / "parametros.yaml"
    caminho.write_text(f"janela: [{segredo_ficticio}\n  : : :\n", encoding="utf-8")

    with pytest.raises(ParametrosInvalidos) as info:
        carregar_parametros(caminho)

    assert segredo_ficticio not in str(info.value)
