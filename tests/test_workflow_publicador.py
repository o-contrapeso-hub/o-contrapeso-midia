"""T041 (RF-21): workflow do agendador e ponto de entrada ``python -m publicador.job``."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from publicador.job import __main__ as entrada
from publicador.job.orquestrador import RelatorioExecucao

RAIZ = Path(__file__).resolve().parent.parent
ARQUIVO = RAIZ / ".github" / "workflows" / "publicador.yml"
TEXTO = ARQUIVO.read_text(encoding="utf-8") if ARQUIVO.exists() else ""


def _carregar() -> dict[Any, Any]:
    dados = yaml.safe_load(TEXTO)
    assert isinstance(dados, dict)
    return dados


def _gatilhos(dados: dict[Any, Any]) -> dict[str, Any]:
    # YAML 1.1: a chave ``on`` vira True.
    gatilhos = dados.get("on", dados.get(True))
    assert isinstance(gatilhos, dict)
    return gatilhos


def _passos(dados: dict[Any, Any]) -> list[dict[str, Any]]:
    jobs = dados["jobs"]
    assert len(jobs) == 1
    return list(next(iter(jobs.values()))["steps"])


def _job(dados: dict[Any, Any]) -> dict[str, Any]:
    return dict(next(iter(dados["jobs"].values())))


# ------------------------------------------------------------------ workflow


def test_arquivo_existe() -> None:
    assert ARQUIVO.is_file()


def test_cron_a_cada_5_minutos_em_minutos_nao_redondos() -> None:
    crons = [s["cron"] for s in _gatilhos(_carregar())["schedule"]]
    assert len(crons) == 1
    campos = crons[0].split()
    assert len(campos) == 5 and campos[1:] == ["*", "*", "*", "*"]
    assert not campos[0].startswith("*/")
    m = re.fullmatch(r"(\d+)-(\d+)/5", campos[0])
    assert m is not None
    inicio, fim = int(m[1]), int(m[2])
    minutos = list(range(inicio, fim + 1, 5))
    assert 0 <= inicio < fim <= 59
    assert all(x % 5 != 0 for x in minutos)  # nenhum minuto redondo (0, 5, 10...)
    assert len(minutos) == 12  # a cada 5 minutos, a hora toda


def test_execucao_manual_sem_inputs() -> None:
    gatilhos = _gatilhos(_carregar())
    assert "workflow_dispatch" in gatilhos
    despacho = gatilhos["workflow_dispatch"] or {}
    assert "inputs" not in despacho  # ninguém liga o modo real pelo botão
    assert set(gatilhos) == {"schedule", "workflow_dispatch"}


def test_concorrencia_unica_sem_cancelar() -> None:
    conc = _carregar()["concurrency"]
    assert conc["cancel-in-progress"] is False
    grupo = conc["group"]
    assert isinstance(grupo, str) and grupo and "${{" not in grupo


def test_permissoes_minimas() -> None:
    dados = _carregar()
    assert dados["permissions"] == {"contents": "read"}
    assert "permissions" not in _job(dados) or _job(dados)["permissions"] == {"contents": "read"}
    assert not re.search(r":\s*write\b", TEXTO)


def test_modo_real_vem_da_variavel_do_repositorio() -> None:
    env = _job(_carregar()).get("env", {})
    passo = next(p for p in _passos(_carregar()) if "publicador.job" in p.get("run", ""))
    env = {**env, **passo.get("env", {})}
    assert env["PUBLICAR_REAL"] == "${{ vars.PUBLICAR_REAL }}"
    assert "secrets.PUBLICAR_REAL" not in TEXTO
    assert "github.event.inputs" not in TEXTO and "inputs." not in TEXTO
    # nenhum outro lugar define PUBLICAR_REAL (nem literal "1")
    assert len(re.findall(r"PUBLICAR_REAL\s*:", TEXTO)) == 1


def test_segredos_so_por_nome_no_bloco_env() -> None:
    dados = _carregar()
    for passo in _passos(dados):
        assert "secrets." not in passo.get("run", "")
        assert "secrets." not in str(passo.get("with", ""))
    referencias = re.findall(r"\$\{\{\s*secrets\.([A-Z0-9_]+)\s*\}\}", TEXTO)
    assert referencias  # o token do Notion entra por aqui
    assert TEXTO.count("secrets.") == len(referencias)  # nenhuma forma fora do padrão
    for passo in _passos(dados):
        for nome, valor in (passo.get("env") or {}).items():
            if "secret" in str(valor):
                assert re.fullmatch(r"\$\{\{ secrets\.[A-Z0-9_]+ \}\}", str(valor)), nome
    # segredos só no passo que roda o publicador
    com_segredo = [p for p in _passos(dados) if "secrets." in str(p.get("env", ""))]
    assert [p["run"].strip() for p in com_segredo] == ["python -m publicador.job"]
    assert not re.search(r"^\s*(?:-\s*)?run:.*\becho\b.*\$", TEXTO, re.M)
    assert "set -x" not in TEXTO and "ACTIONS_STEP_DEBUG" not in TEXTO
    assert "ACTIONS_RUNNER_DEBUG" not in TEXTO
    assert "printenv" not in TEXTO and not re.search(r"^\s*(?:run:\s*)?env\s*$", TEXTO, re.M)


def test_checkout_sem_credenciais_e_timeout() -> None:
    dados = _carregar()
    assert isinstance(_job(dados)["timeout-minutes"], int)
    checkout = next(
        p for p in _passos(dados) if str(p.get("uses", "")).startswith("actions/checkout")
    )
    assert checkout["with"]["persist-credentials"] is False


def test_executa_o_modulo_do_job() -> None:
    comandos = [p.get("run", "").strip() for p in _passos(_carregar())]
    assert "python -m publicador.job" in comandos


def test_nenhum_valor_literal_de_credencial() -> None:
    assert not re.search(
        r"(?i)(token|secret|senha|password)\w*\s*:\s*[\"']?(?!\$\{\{)[A-Za-z0-9_\-]{12,}", TEXTO
    )


def test_comentario_aponta_token_de_escrita_t044() -> None:
    assert "T044" in TEXTO and "A-004" in TEXTO


# ------------------------------------------------------------------ ponto de entrada

TOKEN_FALSO = "ntn_FALSO_para_teste_0123456789abcdef"
BASE = {
    "NOTION_TOKEN": TOKEN_FALSO,
    "PUBLICADOR_DB_ESTAGIOS": "a" * 32,
    "PUBLICADOR_DB_FORMATOS": "b" * 32,
    "PUBLICADOR_DB_SERIES": "c" * 32,
    "PUBLICADOR_DB_TEMAS": "d" * 32,
    "PUBLICADOR_DB_POSTS": "e" * 32,
    "PUBLICADOR_DB_EXECUCOES": "f" * 32,
    "PUBLICADOR_ARTES_REPO": "dono/repo",
    "PUBLICADOR_ARTES_REF": "main",
    "PUBLICADOR_ARTES_TOKEN": "ghp_FALSO_leitura_0123456789abcdef",
}


class _Espiao:
    def __init__(self) -> None:
        self.chamadas: list[dict[str, Any]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> RelatorioExecucao:
        self.chamadas.append({"args": args, "kwargs": kwargs})
        return RelatorioExecucao(modo_real=bool(kwargs["publicar_real"]))


def _config_falsa(*_a: Any, **_k: Any) -> object:
    return object()  # sem rede: o executor é um espião


def _rodar(ambiente: dict[str, str], espiao: _Espiao | None = None) -> tuple[int, _Espiao]:
    espiao = espiao or _Espiao()
    codigo = entrada.principal(ambiente, executar=espiao, ler_config=_config_falsa)
    return codigo, espiao


def test_simulacao_por_padrao(capsys: pytest.CaptureFixture[str]) -> None:
    codigo, espiao = _rodar(dict(BASE))
    assert codigo == 0
    assert espiao.chamadas[0]["kwargs"]["publicar_real"] is False
    adaptadores = espiao.chamadas[0]["args"][4]
    assert set(adaptadores) == {"Instagram", "TikTok", "YouTube"}
    agora = espiao.chamadas[0]["args"][6]()
    assert isinstance(agora, datetime) and agora.utcoffset() is not None
    assert "simulação" in capsys.readouterr().out.lower()


@pytest.mark.parametrize("valor", ["", "true", "0", " 1", "1 ", "sim", "TRUE", "yes"])
def test_valor_diferente_de_1_continua_simulacao(valor: str) -> None:
    codigo, espiao = _rodar({**BASE, "PUBLICAR_REAL": valor})
    assert codigo == 0
    assert espiao.chamadas[0]["kwargs"]["publicar_real"] is False


def test_exatamente_1_liga_o_modo_real() -> None:
    codigo, espiao = _rodar({**BASE, "PUBLICAR_REAL": "1"})
    assert codigo == 0
    assert espiao.chamadas[0]["kwargs"]["publicar_real"] is True


def test_modo_real_nao_registra_o_adaptador_falso() -> None:
    # I-2/RF-37: o falso devolveria "sucesso" sem publicar; em modo real nada é registrado.
    _, espiao = _rodar({**BASE, "PUBLICAR_REAL": "1"})
    assert espiao.chamadas[0]["args"][4] == {}


@pytest.mark.parametrize("nome", sorted(BASE))
def test_variavel_ausente_falha_com_mensagem_fixa(
    nome: str, capsys: pytest.CaptureFixture[str]
) -> None:
    ambiente = {k: v for k, v in BASE.items() if k != nome}
    codigo, espiao = _rodar(ambiente)
    assert codigo != 0
    assert espiao.chamadas == []
    saida = capsys.readouterr()
    texto = saida.out + saida.err
    assert entrada.MENSAGEM_CONFIGURACAO in texto
    assert TOKEN_FALSO not in texto


def test_token_vazio_conta_como_ausente() -> None:
    codigo, espiao = _rodar({**BASE, "NOTION_TOKEN": "  "})
    assert codigo != 0 and espiao.chamadas == []


def test_token_nunca_aparece_na_saida_nem_no_log(
    capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    _rodar({**BASE, "PUBLICAR_REAL": "1"})
    _rodar({k: v for k, v in BASE.items() if k != "PUBLICADOR_DB_POSTS"})
    saida = capsys.readouterr()
    tudo = saida.out + saida.err + caplog.text
    for segredo in (TOKEN_FALSO, BASE["PUBLICADOR_ARTES_TOKEN"]):
        assert segredo not in tudo


def test_excecao_do_executor_nao_vaza_o_texto(
    capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    def explode(*_a: Any, **_k: Any) -> RelatorioExecucao:
        raise RuntimeError(f"falhou com {TOKEN_FALSO}")

    caplog.set_level(logging.DEBUG)
    codigo = entrada.principal(dict(BASE), executar=explode, ler_config=_config_falsa)
    saida = capsys.readouterr()
    assert codigo != 0
    assert TOKEN_FALSO not in saida.out + saida.err + caplog.text


def test_resumo_so_tem_contagens(capsys: pytest.CaptureFixture[str]) -> None:
    _rodar(dict(BASE))
    saida = capsys.readouterr().out
    assert "simulado=0" in saida
    assert "falha geral" not in saida.lower()


def test_modulo_nao_importa_logica_de_autorizacao() -> None:
    fonte = Path(entrada.__file__).read_text(encoding="utf-8")
    assert "autorizado" not in fonte.lower().replace("estágio_autorizador", "")
