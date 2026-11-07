"""Teste de fumaça: confere que o pacote `publicador` carrega."""

import importlib


def test_pacote_carrega() -> None:
    modulo = importlib.import_module("publicador")
    assert modulo.__name__ == "publicador"
