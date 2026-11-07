"""Testes do armazem de artes (publicador.artes.armazem) - regras I-1 e I-2.

Nenhum teste acessa a rede: o ArmazemGitHub usa um transporte falso e o token
e FICTICIO.
"""

from __future__ import annotations

import hashlib
import os
import traceback
from collections.abc import Mapping
from pathlib import Path

import pytest

from publicador.artes.armazem import (
    ArmazemDeArtes,
    ArmazemGitHub,
    ArmazemLocal,
    ArteNaoEncontrada,
    CaminhoDeArteInvalido,
    ErroAcessoArmazem,
)
from publicador.core.redacao import SegredoStr

TOKEN_FALSO = "ghp_FICTICIO_tokenSomenteLeitura_123456"
ARTE = "serie-a/2026/11/2026-11-07_exemplo_01.png"
CONTEUDO = b"\x89PNG conteudo ficticio da arte"


# --------------------------------------------------------------------------- local


@pytest.fixture
def raiz(tmp_path: Path) -> Path:
    pasta = tmp_path / "artes"
    destino = pasta / "serie-a" / "2026" / "11"
    destino.mkdir(parents=True)
    (destino / "2026-11-07_exemplo_01.png").write_bytes(CONTEUDO)
    return pasta


def test_local_le_arquivo_existente(raiz: Path) -> None:
    armazem = ArmazemLocal(raiz)
    assert armazem.ler(ARTE) == CONTEUDO
    assert armazem.existe(ARTE) is True


def test_local_satisfaz_protocol(raiz: Path) -> None:
    armazem: ArmazemDeArtes = ArmazemLocal(raiz)
    assert armazem.existe(ARTE)


def test_hash_e_sha256_hex_minusculo(raiz: Path) -> None:
    esperado = hashlib.sha256(CONTEUDO).hexdigest()
    resultado = ArmazemLocal(raiz).hash_sha256(ARTE)
    assert resultado == esperado
    assert resultado == resultado.lower()
    assert len(resultado) == 64


def test_hash_estavel_entre_chamadas_e_instancias(raiz: Path) -> None:
    assert ArmazemLocal(raiz).hash_sha256(ARTE) == ArmazemLocal(raiz).hash_sha256(ARTE)


def test_hash_muda_se_conteudo_muda(raiz: Path) -> None:
    armazem = ArmazemLocal(raiz)
    antes = armazem.hash_sha256(ARTE)
    (raiz / ARTE).write_bytes(CONTEUDO + b"x")
    assert armazem.hash_sha256(ARTE) != antes


def test_hash_independe_do_nome(raiz: Path) -> None:
    copia = raiz / "outra-serie" / "2027" / "01" / "2027-01-01_outro_02.png"
    copia.parent.mkdir(parents=True)
    copia.write_bytes(CONTEUDO)
    armazem = ArmazemLocal(raiz)
    assert armazem.hash_sha256(ARTE) == armazem.hash_sha256(
        "outra-serie/2027/01/2027-01-01_outro_02.png"
    )


def test_hash_de_arquivo_grande(raiz: Path) -> None:
    grande = os.urandom(3 * 1024 * 1024 + 7)
    (raiz / "serie-a" / "grande.bin").write_bytes(grande)
    assert (
        ArmazemLocal(raiz).hash_sha256("serie-a/grande.bin") == hashlib.sha256(grande).hexdigest()
    )


def test_arquivo_ausente_levanta_arte_nao_encontrada_clara(raiz: Path) -> None:
    armazem = ArmazemLocal(raiz)
    ausente = "serie-a/2026/11/2026-11-08_fantasma_01.png"
    assert armazem.existe(ausente) is False
    for operacao in (armazem.ler, armazem.hash_sha256):
        with pytest.raises(ArteNaoEncontrada) as info:
            operacao(ausente)
        mensagem = str(info.value)
        assert ausente in mensagem
        assert "nao encontrada" in mensagem.lower() or "não encontrada" in mensagem.lower()
        assert str(raiz) not in mensagem  # nada do caminho absoluto do servidor
        assert isinstance(info.value, FileNotFoundError)


def test_diretorio_nao_e_arte(raiz: Path) -> None:
    armazem = ArmazemLocal(raiz)
    assert armazem.existe("serie-a/2026") is False
    with pytest.raises(ArteNaoEncontrada):
        armazem.ler("serie-a/2026")


@pytest.mark.parametrize(
    "caminho",
    [
        "../x",
        "/etc/passwd",
        "a/../../b",
        "a/../b",
        "a\\b",
        "..\\x",
        "",
        "a\x00b",
        "a//b",
        "./a",
        "a/./b",
        "..",
        ".",
        "a/",
    ],
)
def test_local_recusa_caminhos_maliciosos(raiz: Path, caminho: str) -> None:
    armazem = ArmazemLocal(raiz)
    for operacao in (armazem.ler, armazem.hash_sha256, armazem.existe):
        with pytest.raises(CaminhoDeArteInvalido) as info:
            operacao(caminho)
        assert isinstance(info.value, ValueError)
        assert str(raiz) not in str(info.value)


def test_local_recusa_nao_str(raiz: Path) -> None:
    with pytest.raises(CaminhoDeArteInvalido):
        ArmazemLocal(raiz).ler(None)  # type: ignore[arg-type]


def test_local_recusa_symlink_que_escapa(raiz: Path, tmp_path: Path) -> None:
    segredo = tmp_path / "fora.txt"
    segredo.write_text("conteudo fora da raiz")
    (raiz / "serie-a" / "atalho.txt").symlink_to(segredo)
    (raiz / "serie-a" / "pasta-fora").symlink_to(tmp_path, target_is_directory=True)
    armazem = ArmazemLocal(raiz)
    for caminho in ("serie-a/atalho.txt", "serie-a/pasta-fora/fora.txt"):
        with pytest.raises(CaminhoDeArteInvalido):
            armazem.ler(caminho)
        with pytest.raises(CaminhoDeArteInvalido):
            armazem.hash_sha256(caminho)
        with pytest.raises(CaminhoDeArteInvalido):
            armazem.existe(caminho)


def test_local_aceita_symlink_que_fica_dentro_da_raiz(raiz: Path) -> None:
    (raiz / "serie-a" / "atalho.png").symlink_to(raiz / ARTE)
    assert ArmazemLocal(raiz).ler("serie-a/atalho.png") == CONTEUDO


# --------------------------------------------------------------------------- github


class TransporteFalso:
    def __init__(self, status: int = 200, corpo: bytes = CONTEUDO) -> None:
        self.status = status
        self.corpo = corpo
        self.chamadas: list[tuple[str, dict[str, str], float]] = []

    def obter(self, url: str, cabecalhos: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
        self.chamadas.append((url, dict(cabecalhos), timeout))
        return self.status, self.corpo


class TransporteQueExplode:
    def obter(self, url: str, cabecalhos: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
        raise OSError(f"falha com {cabecalhos.get('Authorization')} em {url}")


def _armazem(transporte: object) -> ArmazemGitHub:
    return ArmazemGitHub(
        "dono-x",
        "artes-privado",
        "main",
        SegredoStr(TOKEN_FALSO),
        transporte,  # type: ignore[arg-type]
    )


def _texto_completo(erro: BaseException) -> str:
    partes = [str(erro), repr(erro), repr(erro.args)]
    partes.append("".join(traceback.format_exception(type(erro), erro, erro.__traceback__)))
    return "\n".join(partes)


def test_github_200_devolve_conteudo_e_hash() -> None:
    transporte = TransporteFalso(200, CONTEUDO)
    armazem = _armazem(transporte)
    assert armazem.ler(ARTE) == CONTEUDO
    assert armazem.hash_sha256(ARTE) == hashlib.sha256(CONTEUDO).hexdigest()
    assert armazem.existe(ARTE) is True


def test_github_monta_url_com_segmentos_codificados_e_ref() -> None:
    transporte = TransporteFalso()
    armazem = ArmazemGitHub(
        "dono-x", "artes-privado", "feature/x y", SegredoStr(TOKEN_FALSO), transporte
    )
    armazem.ler("serie a/2026/11/2026-11-07_ação #1_01.png")
    url, cabecalhos, timeout = transporte.chamadas[0]
    assert url == (
        "https://api.github.com/repos/dono-x/artes-privado/contents/"
        "serie%20a/2026/11/2026-11-07_a%C3%A7%C3%A3o%20%231_01.png?ref=feature/x%20y"
    )
    assert cabecalhos["Accept"] == "application/vnd.github.raw+json"
    assert timeout > 0


def test_github_token_so_no_cabecalho() -> None:
    transporte = TransporteFalso()
    armazem = _armazem(transporte)
    armazem.ler(ARTE)
    url, cabecalhos, _ = transporte.chamadas[0]
    assert TOKEN_FALSO not in url
    assert TOKEN_FALSO in cabecalhos["Authorization"]
    assert cabecalhos["Authorization"].startswith("Bearer ")


def test_github_repr_e_str_nao_mostram_token() -> None:
    armazem = _armazem(TransporteFalso())
    for texto in (repr(armazem), str(armazem), f"{armazem}", f"{armazem!r}"):
        assert TOKEN_FALSO not in texto
        assert "FICTICIO" not in texto
    assert "dono-x/artes-privado" in repr(armazem)


def test_github_404_vira_arte_nao_encontrada() -> None:
    armazem = _armazem(TransporteFalso(404, b'{"message":"Not Found"}'))
    assert armazem.existe(ARTE) is False
    with pytest.raises(ArteNaoEncontrada) as info:
        armazem.ler(ARTE)
    assert ARTE in str(info.value)
    assert TOKEN_FALSO not in _texto_completo(info.value)


@pytest.mark.parametrize("status", [401, 403])
def test_github_401_403_viram_erro_de_acesso_sem_vazar(status: int) -> None:
    corpo = f"eco do token {TOKEN_FALSO} e cabecalhos".encode()
    armazem = _armazem(TransporteFalso(status, corpo))
    with pytest.raises(ErroAcessoArmazem) as info:
        armazem.ler(ARTE)
    texto = _texto_completo(info.value)
    assert TOKEN_FALSO not in texto
    assert "eco do token" not in texto
    assert "Authorization" not in texto
    assert str(info.value)  # mensagem fixa, nao vazia
    with pytest.raises(ErroAcessoArmazem):
        armazem.existe(ARTE)


def test_github_outro_status_vira_erro_de_acesso_sem_eco() -> None:
    armazem = _armazem(TransporteFalso(500, f"boom {TOKEN_FALSO}".encode()))
    with pytest.raises(ErroAcessoArmazem) as info:
        armazem.ler(ARTE)
    assert "boom" not in _texto_completo(info.value)
    assert TOKEN_FALSO not in _texto_completo(info.value)


def test_github_falha_de_transporte_nao_vaza_token() -> None:
    armazem = _armazem(TransporteQueExplode())
    with pytest.raises(ErroAcessoArmazem) as info:
        armazem.ler(ARTE)
    assert TOKEN_FALSO not in _texto_completo(info.value)
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__ is True


@pytest.mark.parametrize(
    "caminho", ["../x", "/etc/passwd", "a/../../b", "a\\b", "", "a\x00b", "a//b"]
)
def test_github_valida_caminho_antes_de_chamar_transporte(caminho: str) -> None:
    transporte = TransporteFalso()
    armazem = _armazem(transporte)
    with pytest.raises(CaminhoDeArteInvalido):
        armazem.ler(caminho)
    assert transporte.chamadas == []


def test_github_recusa_dono_repo_ref_invalidos() -> None:
    token = SegredoStr(TOKEN_FALSO)
    transporte = TransporteFalso()
    with pytest.raises(ValueError):
        ArmazemGitHub("do/no", "repo", "main", token, transporte)
    with pytest.raises(ValueError):
        ArmazemGitHub("dono", "../repo", "main", token, transporte)
    with pytest.raises(ValueError):
        ArmazemGitHub("dono", "repo", "", token, transporte)


def test_github_recusa_token_que_nao_e_segredostr() -> None:
    with pytest.raises(TypeError) as info:
        ArmazemGitHub("dono", "repo", "main", TOKEN_FALSO, TransporteFalso())  # type: ignore[arg-type]
    assert TOKEN_FALSO not in _texto_completo(info.value)
