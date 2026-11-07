"""Carregamento e validacao dos parametros operacionais do publicador.

Nenhuma mensagem de erro deste modulo ecoa o conteudo lido do arquivo:
os erros citam apenas o nome do campo, a regra violada e, para minutos
e limites numericos, os numeros envolvidos.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

VARIAVEL_CAMINHO = "PUBLICADOR_PARAMETROS"
_NOME_ARQUIVO = "parametros.yaml"
_NOME_PASTA = "config"
_CAMPO_FUSO = "fuso_padrao"
_TAMANHO_MAX_NOME = 40
_EXEMPLO_FUSO = "America/Sao_Paulo"


class ParametrosInvalidos(ValueError):
    """Parametros ausentes, malformados ou fora das regras de negocio."""


@dataclass(frozen=True)
class Parametros:
    janela_tolerancia_minutos: int
    nova_tentativa_minutos: int
    meta_pontualidade_minutos: int
    fuso_padrao: str
    limite_silencio_vigia_minutos: int


_CAMPOS: tuple[str, ...] = tuple(campo.name for campo in fields(Parametros))
_CAMPOS_MINUTOS: tuple[str, ...] = tuple(c for c in _CAMPOS if c != _CAMPO_FUSO)


def _caminho_padrao() -> Path:
    """Sobe a partir deste arquivo ate achar a pasta config do repositorio."""
    for pasta in Path(__file__).resolve().parents:
        candidato = pasta / _NOME_PASTA
        if candidato.is_dir():
            return candidato / _NOME_ARQUIVO
    return Path(__file__).resolve().parent / _NOME_PASTA / _NOME_ARQUIVO


def _resolver_caminho(caminho: Path | None) -> Path:
    if caminho is not None:
        return caminho
    via_ambiente = os.environ.get(VARIAVEL_CAMINHO, "").strip()
    if via_ambiente:
        return Path(via_ambiente)
    return _caminho_padrao()


def _ler_yaml(arquivo: Path) -> dict[Any, Any]:
    try:
        texto = arquivo.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ParametrosInvalidos(f"arquivo de parâmetros não encontrado: {arquivo}") from None
    except (OSError, UnicodeDecodeError):
        raise ParametrosInvalidos(
            f"não foi possível ler o arquivo de parâmetros: {arquivo}"
        ) from None

    try:
        dados: Any = yaml.safe_load(texto)
    except yaml.YAMLError as erro:
        marca = getattr(erro, "problem_mark", None)
        linha = f" (linha {marca.line + 1})" if marca is not None else ""
        raise ParametrosInvalidos(
            f"YAML inválido em {arquivo}{linha}; corrija a sintaxe do arquivo"
        ) from None

    if dados is None:
        raise ParametrosInvalidos(f"arquivo de parâmetros está vazio: {arquivo}")
    if not isinstance(dados, dict):
        raise ParametrosInvalidos(
            "o conteúdo do arquivo de parâmetros deve ser um mapa chave: valor, "
            f"não {type(dados).__name__}"
        )
    return dados


def _nome_seguro(chave: object) -> str:
    """Nome de campo para mensagens. Chaves longas ou nao textuais viram rotulo generico."""
    if isinstance(chave, str) and len(chave) <= _TAMANHO_MAX_NOME:
        return chave
    return "(nome de campo não textual ou longo)"


def _validar_chaves(dados: dict[Any, Any]) -> None:
    faltando = [c for c in _CAMPOS if c not in dados]
    if faltando:
        nomes = ", ".join(f"'{c}'" for c in faltando)
        raise ParametrosInvalidos(f"campo obrigatório ausente: {nomes}")

    desconhecidos = [_nome_seguro(c) for c in dados if c not in _CAMPOS]
    if desconhecidos:
        nomes = ", ".join(f"'{c}'" for c in desconhecidos)
        raise ParametrosInvalidos(f"campo desconhecido no arquivo de parâmetros: {nomes}")


def _validar_minutos(campo: str, valor: Any) -> int:
    if isinstance(valor, bool) or not isinstance(valor, int):
        raise ParametrosInvalidos(
            f"'{campo}' deve ser um número inteiro de minutos, "
            "sem aspas, sem casas decimais e sem true/false"
        )
    if valor <= 0:
        raise ParametrosInvalidos(f"'{campo}' deve ser maior que zero")
    return valor


def _validar_fuso(valor: Any) -> str:
    mensagem = f"'{_CAMPO_FUSO}' deve ser um nome de fuso IANA válido, por exemplo {_EXEMPLO_FUSO}"
    if not isinstance(valor, str):
        raise ParametrosInvalidos(mensagem)
    try:
        ZoneInfo(valor)
    except (KeyError, ValueError, OSError):
        raise ParametrosInvalidos(mensagem) from None
    return valor


def _validar_regras(valores: dict[str, int]) -> None:
    janela = valores["janela_tolerancia_minutos"]
    nova = valores["nova_tentativa_minutos"]
    meta = valores["meta_pontualidade_minutos"]

    if nova >= janela:
        raise ParametrosInvalidos(
            f"'nova_tentativa_minutos' ({nova}) deve ser menor que "
            f"'janela_tolerancia_minutos' ({janela})"
        )
    if meta > janela:
        raise ParametrosInvalidos(
            f"'meta_pontualidade_minutos' ({meta}) deve ser menor ou igual a "
            f"'janela_tolerancia_minutos' ({janela})"
        )


def carregar_parametros(caminho: Path | None = None) -> Parametros:
    """Carrega e valida os parametros.

    Ordem de escolha do arquivo: argumento ``caminho``, depois a variavel
    de ambiente ``PUBLICADOR_PARAMETROS``, depois ``config/parametros.yaml``
    na raiz do repositorio.

    Raises:
        ParametrosInvalidos: arquivo ausente, ilegivel, malformado ou com
            valores fora das regras.
    """
    arquivo = _resolver_caminho(caminho)
    dados = _ler_yaml(arquivo)
    _validar_chaves(dados)

    valores: dict[str, Any] = {c: dados[c] for c in _CAMPOS}
    minutos: dict[str, int] = {c: _validar_minutos(c, valores[c]) for c in _CAMPOS_MINUTOS}
    fuso = _validar_fuso(valores[_CAMPO_FUSO])
    _validar_regras(minutos)

    return Parametros(
        janela_tolerancia_minutos=minutos["janela_tolerancia_minutos"],
        nova_tentativa_minutos=minutos["nova_tentativa_minutos"],
        meta_pontualidade_minutos=minutos["meta_pontualidade_minutos"],
        fuso_padrao=fuso,
        limite_silencio_vigia_minutos=minutos["limite_silencio_vigia_minutos"],
    )
