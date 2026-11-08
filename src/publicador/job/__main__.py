"""Ponto de entrada do agendador: ``python -m publicador.job`` (T041, RF-21).

Monta as peças a partir de VARIÁVEIS DE AMBIENTE, roda UMA rodada de ``executar`` e imprime
só um resumo seguro (modo, contagens por desfecho, código da falha geral). Nunca imprime nem
loga título, corpo, artes, selo, token ou o texto bruto de uma exceção (I-1).

Variáveis (somente NOMES; os valores vêm do GitHub Actions: secret ou variável do repositório):

* ``NOTION_TOKEN`` (SECRET): token da integração do Notion.
* ``PUBLICADOR_ARTES_TOKEN`` (SECRET): token SOMENTE LEITURA para ler as artes no GitHub.
* ``PUBLICADOR_DB_ESTAGIOS``, ``_FORMATOS``, ``_SERIES``, ``_TEMAS``, ``_POSTS``,
  ``_EXECUCOES`` (variáveis): ids dos bancos do Notion (``PUBLICADOR_DB_<BANCO>``).
* ``PUBLICADOR_ARTES_REPO`` (variável): ``dono/repo`` das artes.
* ``PUBLICADOR_ARTES_REF`` (variável): branch, tag ou commit das artes.
* ``PUBLICAR_REAL`` (variável do repositório): só o valor exato ``1`` liga o modo real.
  Qualquer outro valor, ou ausente, é SIMULAÇÃO (padrão seguro). Ver ``modo_real_ligado``.

Ainda NÃO lidas (ligação na T049, ver ``docs/instagram-estagio.md``):
``PUBLICADOR_ESTAGIO_REPO`` (variável, ``dono/repo`` do repositório público de estágio) e
``PUBLICADOR_ESTAGIO_TOKEN`` (SECRET, escrita só nesse repositório; A-004). Quando ligadas,
o ``EstagioGitHub`` vai ao ``AdaptadorInstagram`` (``estagio=``) e ao ``executar``
(``estagio=``, limpeza A-005 no início de cada rodada).

Só o adaptador falso existe, e só em SIMULAÇÃO (as plataformas reais entram a partir da T043).
Em modo real nenhum adaptador é registrado: toda plataforma é recusada pelo orquestrador.
Este módulo não decide nada sobre autorização: a guarda continua sendo o único caminho (I-2).

Saída: ``0`` na execução (mesmo com falha geral do Notion, que a próxima rodada retenta);
``2`` para variável de ambiente ausente/inválida; ``1`` para erro inesperado. Mensagens fixas.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Final

from publicador.adaptadores.falso import AdaptadorFalso
from publicador.artes.armazem import ArmazemGitHub, TransporteHttpUrllib
from publicador.core.config import ParametrosInvalidos, carregar_parametros
from publicador.core.redacao import SegredoStr, obter_logger
from publicador.job.orquestrador import (
    RelatorioExecucao,
    executar,
    modo_real_ligado,
)
from publicador.notion.bancos import IdsBancos, ler_configuracao
from publicador.notion.cliente import ClienteNotion, ErroNotion

__all__ = ["MENSAGEM_CONFIGURACAO", "principal"]

_log = obter_logger("publicador.job.entrada")

MENSAGEM_CONFIGURACAO: Final = (
    "Erro de configuração: variável de ambiente obrigatória ausente ou inválida."
)
MENSAGEM_INESPERADO: Final = "Erro inesperado na execução; veja o código de saída."
MENSAGEM_NOTION: Final = "Notion indisponível na leitura da configuração; nada foi publicado."

PLATAFORMAS_SIMULADAS: Final = ("Instagram", "TikTok", "YouTube")
_BANCOS: Final = ("ESTAGIOS", "FORMATOS", "SERIES", "TEMAS", "POSTS", "EXECUCOES")
_SAIDA_OK: Final = 0
_SAIDA_INESPERADO: Final = 1
_SAIDA_CONFIGURACAO: Final = 2


class _ConfiguracaoInvalida(Exception):  # noqa: N818 - uso interno
    """Sem mensagem: o chamador imprime o texto fixo."""


def _obrigatoria(ambiente: Mapping[str, str], nome: str) -> str:
    valor = ambiente.get(nome, "").strip()
    if not valor:
        raise _ConfiguracaoInvalida
    return valor


def _agora_utc() -> datetime:
    return datetime.now(UTC)


def _resumo(relatorio: RelatorioExecucao) -> str:
    modo = "real" if relatorio.modo_real else "simulação"
    partes = [f"modo={modo}"]
    if relatorio.falha is not None:
        partes.append(f"falha_geral={relatorio.falha.value}")
    contagens = relatorio.contagens()
    partes.extend(f"{nome}={contagens[nome]}" for nome in sorted(contagens))
    partes.append(f"erros_de_registro={len(relatorio.erros_de_registro)}")
    return "Resumo da rodada: " + " ".join(partes)


def principal(
    ambiente: Mapping[str, str],
    *,
    executar: Callable[..., RelatorioExecucao] = executar,
    ler_config: Callable[..., Any] = ler_configuracao,
    transporte_notion: Any | None = None,
    transporte_http: Any | None = None,
    agora: Callable[[], datetime] = _agora_utc,
) -> int:
    """Uma rodada. Dependências injetáveis para teste; ``ambiente`` é o mapa de variáveis."""
    try:
        token = SegredoStr(_obrigatoria(ambiente, "NOTION_TOKEN"))  # I-1: embrulha já
        token_artes = SegredoStr(_obrigatoria(ambiente, "PUBLICADOR_ARTES_TOKEN"))
        ids_brutos = {b: _obrigatoria(ambiente, f"PUBLICADOR_DB_{b}") for b in _BANCOS}
        repo_artes = _obrigatoria(ambiente, "PUBLICADOR_ARTES_REPO")
        ref_artes = _obrigatoria(ambiente, "PUBLICADOR_ARTES_REF")
        dono, barra, repo = repo_artes.partition("/")
        if not barra:
            raise _ConfiguracaoInvalida
        ids = IdsBancos(**{b.lower(): v for b, v in ids_brutos.items()})
        parametros = carregar_parametros()
        armazem = ArmazemGitHub(
            dono,
            repo,
            ref_artes,
            token_artes,
            transporte_http if transporte_http is not None else TransporteHttpUrllib(),
        )
    except (_ConfiguracaoInvalida, ValueError, TypeError, ParametrosInvalidos):
        # ValueError cobre ids inválidos e repo/ref inválidos; nunca ecoa o valor.
        print(MENSAGEM_CONFIGURACAO)
        return _SAIDA_CONFIGURACAO

    real = modo_real_ligado(ambiente)
    print(f"Iniciando rodada em modo {'REAL' if real else 'simulação'}.")
    try:
        cliente = (
            ClienteNotion(token, transporte_notion)
            if transporte_notion is not None
            else ClienteNotion(token)
        )
        try:
            config = ler_config(cliente, ids)
        except ErroNotion:
            print(MENSAGEM_NOTION)
            return _SAIDA_OK
        # I-2/RF-37: o adaptador falso NUNCA entra em modo real (devolveria "sucesso" sem
        # publicar nada). Sem adaptador real, a plataforma é recusada e registrada.
        adaptadores = (
            {}
            if real
            else {nome: AdaptadorFalso(nome, armazem=armazem) for nome in PLATAFORMAS_SIMULADAS}
        )
        relatorio = executar(
            cliente,
            ids,
            config,
            armazem,
            adaptadores,
            parametros,
            agora,
            publicar_real=real,
        )
    except Exception:  # noqa: BLE001 - texto bruto pode conter segredo (I-1)
        _log.error("entrada: erro inesperado (tipo omitido por segurança)")
        print(MENSAGEM_INESPERADO)
        return _SAIDA_INESPERADO
    print(_resumo(relatorio))
    return _SAIDA_OK


if __name__ == "__main__":
    import os

    sys.exit(principal(os.environ))
