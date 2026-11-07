"""Teste de propriedade da regra I-2 na guarda de publicação (T035; plano 4.1 e seção 7).

Requisito: para QUALQUER combinação de entradas, a guarda só permite se o post está no estágio
``autorizado`` (chamado "confirmado", numa configuração íntegra), com data E hora (com fuso),
com selo válido recalculado agora e sem horário futuro.

Como funciona:

* **Gerador próprio e determinístico** (``random.Random`` com semente fixa, sem dependência).
  Cada caso escolhe, para cada DIMENSÃO de entrada, uma OPÇÃO válida ou inválida (tabela
  ``DIMENSOES``): parâmetros, instante atual, data e hora, estágio, configuração, série,
  plataforma pedida, adaptadores, conexões, tipos por formato, mídia, execuções, publicados,
  selo (inclusive post editado depois de selar e arte trocada no armazém) e "lixo" (um campo
  do contexto trocado por valor de tipo errado). Cada caso usa o próprio
  ``Random(semente * 1_000_003 + índice)``: semente e índice bastam para reproduzi-lo.
* **Oráculo independente** (``deveria_permitir``): a conjunção, escrita aqui em forma de
  tabela de requisitos e a partir do plano 4.1 e das decisões documentadas (não copiando a
  guarda), de todas as condições necessárias.
* **Propriedades checadas em cada caso** (``_checar_caso``):

  1. ``avaliar`` nunca levanta exceção e devolve ``ResultadoGuarda`` bem formado;
  2. SEGURANÇA, direção única (asserção separada, independente do oráculo): se ``avaliar``
     permite, valem as quatro condições do I-2 verificadas direto no post;
  3. ``avaliar(...).permitido == deveria_permitir(...)``;
  4. o gerador concorda com o oráculo (todas as opções válidas <=> oráculo permite);
  5. determinismo (duas chamadas iguais dão o mesmo resultado);
  6. o resultado nunca contém conteúdo do post, caminho de arte, selo nem o token fictício.

* **Mutação de UM campo**: a partir de um caminho feliz aleatório, troca exatamente uma
  dimensão por uma opção inválida (todas, centenas de variações por dimensão): nunca permite.
* **Autoteste**: guardas defeituosas (sempre permitir; sem estágio; sem horário futuro; sem
  selo) precisam ser apanhadas pelas propriedades.
* **Hypothesis** (extra ``dev``): mesma tabela de dimensões como estratégias, para o CI. Sem o
  pacote instalado, esse teste é pulado.

Reprodução: as falhas mostram semente, índice e os NOMES das opções escolhidas (nunca conteúdo
do post). Variáveis de ambiente opcionais: ``PUBLICADOR_T035_SEMENTE`` e
``PUBLICADOR_T035_CASOS``. Todos os valores são fictícios e nada acessa a rede (I-1).
"""

from __future__ import annotations

import copy
import os
import random
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final
from zoneinfo import ZoneInfo

import pytest

from publicador.artes.armazem import ArmazemLocal, ErroAcessoArmazem
from publicador.core import guarda as modulo_guarda
from publicador.core.config import Parametros
from publicador.core.guarda import ContextoGuarda, PublicacaoConhecida, avaliar
from publicador.core.modelos import (
    NOME_ESTAGIO_AUTORIZADOR,
    Conexao,
    ConexaoEstado,
    EstadoExecucao,
    Estagio,
    Execucao,
    MotivoRecusa,
    PapelEstagio,
    Post,
    ResultadoGuarda,
    Serie,
)
from publicador.core.selo import calcular_selo, verificar_selo
from publicador.notion.bancos import Configuracao

P = PapelEstagio
E = EstadoExecucao

# --------------------------------------------------------------------------- parâmetros

SEMENTE: Final = int(os.environ.get("PUBLICADOR_T035_SEMENTE", "20261107"))
CASOS: Final = int(os.environ.get("PUBLICADOR_T035_CASOS", "6000"))
MINIMO_DE_CASOS: Final = 5000
MUTACOES_POR_DIMENSAO: Final = 240

# --------------------------------------------------------------------------- valores fictícios

SAO_PAULO: Final = ZoneInfo("America/Sao_Paulo")
FUSOS: Final[tuple[tzinfo, ...]] = (
    SAO_PAULO,
    UTC,
    timezone(timedelta(hours=5, minutes=30)),
    timezone(timedelta(hours=-11)),
)
AGORA_BASE: Final = datetime(2026, 11, 7, 18, 40, tzinfo=SAO_PAULO)

PLATAFORMAS_DO_POST: Final = ("Instagram", "TikTok", "YouTube")
# "Substack" fica configurada em tudo, mas nunca está no post: serve de "plataforma fora".
TODAS: Final = (*PLATAFORMAS_DO_POST, "Substack")
ID_POST: Final = "post-ficticio-t035"

TOKEN_FICTICIO: Final = "FICTICIO-token-nao-real-t035"
TITULO: Final = "Título fictício da T035 que não pode vazar"
CORPO: Final = f"Corpo fictício da T035.\nSegunda linha que não pode vazar. {TOKEN_FICTICIO}"
SECAO: Final = "Legenda fictícia por plataforma que não pode vazar"
PROBLEMA_DE_MIDIA: Final = f"proporção inválida fictícia ({TOKEN_FICTICIO})"

ARTE_1: Final = "serie-ficticia-t035/2026/11/arte_01.png"
ARTE_2: Final = "serie-ficticia-t035/2026/11/arte_02.png"

SERIE_CONTINUA: Final = "Série de continuidade fictícia T035"
SERIE_SOLTA: Final = "Série solta fictícia T035"
SERIE_EXTRA: Final = "Outra série fictícia T035"

SELO_DE_LIXO: Final = "v1:" + "1" * 64  # usado quando o selo não pode ser calculado
ESTADOS_BLOQUEANTES: Final = frozenset({E.SUCESSO, E.SUCESSO_MODO_PRIVADO, E.INICIADA, E.INCERTO})
ESTADOS_NAO_BLOQUEANTES: Final = tuple(e for e in E if e not in ESTADOS_BLOQUEANTES)
LIXOS: Final[tuple[Any, ...]] = (None, 42, 3.5, "lixo", b"lixo", object())

ESTAGIOS: Final[dict[str, Estagio]] = {
    "est-pauta": Estagio("banco de pautas", P.PAUTA),
    "est-pronto": Estagio("posts prontos", P.PRONTO),
    "est-confirmado": Estagio(NOME_ESTAGIO_AUTORIZADOR, P.AUTORIZADO),
    "est-publicado": Estagio("publicado", P.PUBLICADO),
    "est-falha": Estagio("falha na publicação", P.FALHA),
    "est-cancelado": Estagio("cancelado", P.CANCELADO),
}
# Estágios de outros papéis com nomes parecidos com o reservado (permitidos pela RF-39).
PARECIDOS: Final[dict[str, Estagio]] = {
    "est-confirmado-maiusculo": Estagio("Confirmado", P.PRONTO),
    "est-confirmado-espaco": Estagio("confirmado ", P.PRONTO),
    "est-confirmados": Estagio("confirmados", P.PUBLICADO),
    "est-agendado": Estagio("agendado", P.PRONTO),
}


def _estagio_de(papel: PapelEstagio) -> Estagio:
    (achado,) = [e for e in ESTAGIOS.values() if e.papel is papel]
    return achado


def _bruto(classe: type[Any], **campos: Any) -> Any:
    """Instância SEM passar pelo construtor (dado corrompido que o modelo recusaria)."""
    objeto = object.__new__(classe)
    for nome, valor in campos.items():
        object.__setattr__(objeto, nome, valor)
    return objeto


def _forcar(objeto: object, **campos: Any) -> None:
    """Altera um dataclass congelado no lugar (só em objetos criados para o próprio caso)."""
    for nome, valor in campos.items():
        object.__setattr__(objeto, nome, valor)


# --------------------------------------------------------------------------- armazéns


class _ArmazemIndisponivel:
    def ler(self, caminho: str) -> bytes:
        raise ErroAcessoArmazem("Falha de comunicacao com o armazem (ficticio).")

    def hash_sha256(self, caminho: str) -> str:
        raise ErroAcessoArmazem("Falha de comunicacao com o armazem (ficticio).")

    def existe(self, caminho: str) -> bool:
        raise ErroAcessoArmazem("Falha de comunicacao com o armazem (ficticio).")


@dataclass(frozen=True)
class Armazens:
    principal: ArmazemLocal
    principal_outra_instancia: ArmazemLocal
    trocado: ArmazemLocal  # ARTE_1 com outro conteúdo
    sem_arte_1: ArmazemLocal  # ARTE_1 apagada


def _criar_armazens(raiz: Path) -> Armazens:
    conteudos = {
        "principal": {ARTE_1: b"\x89PNG ficticio 01", ARTE_2: b"\x89PNG ficticio 02"},
        "trocado": {ARTE_1: b"\x89PNG ficticio 01 TROCADO", ARTE_2: b"\x89PNG ficticio 02"},
        "sem_arte_1": {ARTE_2: b"\x89PNG ficticio 02"},
    }
    for pasta, arquivos in conteudos.items():
        for caminho, dados in arquivos.items():
            destino = raiz / pasta / caminho
            destino.parent.mkdir(parents=True, exist_ok=True)
            destino.write_bytes(dados)
    return Armazens(
        principal=ArmazemLocal(raiz / "principal"),
        principal_outra_instancia=ArmazemLocal(raiz / "principal"),
        trocado=ArmazemLocal(raiz / "trocado"),
        sem_arte_1=ArmazemLocal(raiz / "sem_arte_1"),
    )


@pytest.fixture(scope="module")
def armazens(tmp_path_factory: pytest.TempPathFactory) -> Armazens:
    return _criar_armazens(tmp_path_factory.mktemp("artes_t035"))


# --------------------------------------------------------------------------- rascunho


@dataclass
class Rascunho:
    """Entradas de um caso, ainda mutáveis; ``_construir`` gera o contexto final."""

    alvo: str
    pedida: Any
    agora: Any
    parametros: Any
    campos_post: dict[str, Any]
    configuracao: Any
    posts_da_serie: Any
    adaptadores: Any
    conexoes: Any
    tipos: Any
    midia: Any
    execucoes: Any
    publicados: Any
    armazem_selagem: Any
    armazem_contexto: Any
    selo_fixo: Any = None
    selar: bool = True
    apos_selar: list[Callable[[Post], None]] = field(default_factory=list)
    lixo: tuple[str, Any] | None = None


def _com_fuso(valor: object) -> datetime | None:
    if isinstance(valor, datetime) and valor.tzinfo is not None and valor.utcoffset() is not None:
        return valor
    return None


def _agora_ref(r: Rascunho) -> datetime:
    return _com_fuso(r.agora) or AGORA_BASE


def _tolerancia_ref(r: Rascunho) -> int:
    p = r.parametros
    if isinstance(p, Parametros) and type(p.janela_tolerancia_minutos) is int:
        if p.janela_tolerancia_minutos > 0:
            return p.janela_tolerancia_minutos
    return 120


def _agendado_ref(r: Rascunho) -> datetime:
    return _com_fuso(r.campos_post.get("publicar_em")) or (_agora_ref(r) - timedelta(minutes=10))


def _fuso(rng: random.Random) -> tzinfo:
    return rng.choice(FUSOS)


def _parametros(tolerancia: Any) -> Parametros:
    return Parametros(
        janela_tolerancia_minutos=tolerancia,
        nova_tentativa_minutos=30,
        meta_pontualidade_minutos=30,
        fuso_padrao="America/Sao_Paulo",
        limite_silencio_vigia_minutos=60,
    )


def _configuracao(
    estagios: Mapping[str, Any],
    series: Mapping[str, Any] | None = None,
    problemas: tuple[str, ...] = (),
) -> Configuracao:
    return Configuracao(
        estagios=dict(estagios),
        formatos={"fmt-feed": "Feed", "fmt-reels": "Reels"},
        series=dict(
            series
            if series is not None
            else {
                "ser-continua": Serie(SERIE_CONTINUA, continuidade=True),
                "ser-solta": Serie(SERIE_SOLTA, continuidade=False),
                "ser-extra": Serie(SERIE_EXTRA, continuidade=True),
            }
        ),
        temas={"tema-1": "Energia"},
        problemas=problemas,
    )


def _embaralhado(rng: random.Random, itens: Mapping[str, Any]) -> dict[str, Any]:
    pares = list(itens.items())
    rng.shuffle(pares)
    return dict(pares)


def _tipos_validos() -> dict[str, dict[str, str]]:
    return {
        "Feed": {p: "imagem" for p in TODAS},
        "Reels": {p: "video" for p in TODAS},
        "Stories": {"Instagram": "story"},
    }


def _conexao(plataforma: str, estado: ConexaoEstado, expira: datetime | None) -> Conexao:
    return Conexao(plataforma=plataforma, estado=estado, expira_em=expira, verificado_em=None)


def _conexoes_validas(rng: random.Random, r: Rascunho) -> dict[str, Conexao]:
    agora = _agora_ref(r)
    return {
        p: Conexao(
            plataforma=p,
            estado=ConexaoEstado.ATIVA,
            expira_em=(agora + timedelta(seconds=rng.randint(60, 60 * 86400))).astimezone(
                _fuso(rng)
            ),
            verificado_em=rng.choice([None, agora - timedelta(hours=1)]),
        )
        for p in TODAS
    }


def _execucao(estado: Any, *, post_id: str = ID_POST, plataforma: str, dias: int = 0) -> Execucao:
    inicio = AGORA_BASE - timedelta(days=dias, minutes=5)
    return Execucao(
        chave_idempotencia=f"{post_id}:{plataforma}:{inicio.isoformat()}",
        post_id=post_id,
        plataforma=plataforma,
        estado=estado,
        tentativa=1 + dias % 2,
        iniciada_em=inicio,
    )


def _irmao(ordem: Any, papel: PapelEstagio, *, serie: Any = SERIE_CONTINUA, id_: str = "") -> Post:
    return Post(
        id=id_ or f"post-ficticio-t035-irmao-{ordem}",
        titulo=f"Parte {ordem} fictícia",
        corpo="Corpo fictício de irmão.",
        estagio=_estagio_de(papel),
        plataformas=PLATAFORMAS_DO_POST,
        formato="Feed",
        serie=serie,
        ordem_na_serie=ordem,
    )


def _rascunho_base(rng: random.Random, arm: Armazens) -> Rascunho:
    """Caminho feliz (todas as dimensões ainda recebem uma opção depois)."""
    alvo = rng.choice(PLATAFORMAS_DO_POST)
    outras = [p for p in PLATAFORMAS_DO_POST if p != alvo]
    plataformas = [alvo, *rng.sample(outras, rng.randint(0, len(outras)))]
    rng.shuffle(plataformas)
    artes: list[str] = [ARTE_1]
    if rng.random() < 0.5:
        artes.append(f"{rng.choice([alvo, alvo.lower(), alvo.upper()])}:{ARTE_2}")
        rng.shuffle(artes)
    secoes = {alvo: SECAO} if rng.random() < 0.5 else {}
    agendado = AGORA_BASE - timedelta(minutes=10)
    return Rascunho(
        alvo=alvo,
        pedida=alvo,
        agora=AGORA_BASE,
        parametros=_parametros(120),
        campos_post={
            "id": ID_POST,
            "titulo": TITULO,
            "corpo": CORPO,
            "estagio": Estagio(NOME_ESTAGIO_AUTORIZADOR, P.AUTORIZADO),
            "plataformas": tuple(plataformas),
            "formato": rng.choice(["Feed", "Reels"]),
            "serie": None,
            "ordem_na_serie": None,
            "tema": "Energia",
            "publicar_em": agendado,
            "artes": tuple(artes),
            "confirmado_em": agendado - timedelta(days=1),
            "editado_em": agendado - timedelta(days=1, minutes=5),
            "secoes_por_plataforma": secoes,
            "somente_data": False,
        },
        configuracao=_configuracao(ESTAGIOS),
        posts_da_serie=(),
        adaptadores=frozenset(TODAS),
        conexoes={},  # preenchido em ``gerar_caso`` (depende do rascunho)
        tipos=_tipos_validos(),
        midia={p: () for p in TODAS},
        execucoes=(),
        publicados={p: () for p in TODAS},
        armazem_selagem=arm.principal,
        armazem_contexto=arm.principal,
    )


# --------------------------------------------------------------------------- dimensões

Aplicar = Callable[[random.Random, Rascunho, Armazens], None]


@dataclass(frozen=True)
class Opcao:
    nome: str
    valida: bool
    aplicar: Aplicar


def _ok(nome: str, aplicar: Aplicar) -> Opcao:
    return Opcao(nome, True, aplicar)


def _ruim(nome: str, aplicar: Aplicar) -> Opcao:
    return Opcao(nome, False, aplicar)


def _no_rascunho(campo: str, gerar: Callable[[random.Random, Rascunho], Any]) -> Aplicar:
    def aplicar(rng: random.Random, r: Rascunho, _arm: Armazens) -> None:
        setattr(r, campo, gerar(rng, r))

    return aplicar


def _no_post(gerar: Callable[[random.Random, Rascunho], Mapping[str, Any]]) -> Aplicar:
    def aplicar(rng: random.Random, r: Rascunho, _arm: Armazens) -> None:
        r.campos_post.update(gerar(rng, r))

    return aplicar


# 1. parâmetros (antes da data: a janela depende da tolerância)
OPCOES_PARAMETROS: Final = (
    _ok("tolerancia_120", _no_rascunho("parametros", lambda g, r: _parametros(120))),
    _ok(
        "tolerancia_variada",
        _no_rascunho("parametros", lambda g, r: _parametros(g.choice([1, 30, 60, 240, 1440]))),
    ),
    _ruim("tolerancia_zero", _no_rascunho("parametros", lambda g, r: _parametros(0))),
    _ruim("tolerancia_negativa", _no_rascunho("parametros", lambda g, r: _parametros(-5))),
    _ruim("tolerancia_bool", _no_rascunho("parametros", lambda g, r: _parametros(True))),
    _ruim("tolerancia_texto", _no_rascunho("parametros", lambda g, r: _parametros("120"))),
    _ruim("tolerancia_float", _no_rascunho("parametros", lambda g, r: _parametros(120.0))),
    _ruim("parametros_none", _no_rascunho("parametros", lambda g, r: None)),
)

# 2. instante atual
OPCOES_AGORA: Final = (
    _ok("agora_sao_paulo", _no_rascunho("agora", lambda g, r: AGORA_BASE)),
    _ok("agora_utc", _no_rascunho("agora", lambda g, r: AGORA_BASE.astimezone(UTC))),
    _ok(
        "agora_variado",
        _no_rascunho(
            "agora",
            lambda g, r: (AGORA_BASE + timedelta(seconds=g.randint(-(10**7), 10**7))).astimezone(
                _fuso(g)
            ),
        ),
    ),
    _ruim("agora_sem_fuso", _no_rascunho("agora", lambda g, r: AGORA_BASE.replace(tzinfo=None))),
    _ruim("agora_none", _no_rascunho("agora", lambda g, r: None)),
    _ruim("agora_texto", _no_rascunho("agora", lambda g, r: AGORA_BASE.isoformat())),
    _ruim("agora_date", _no_rascunho("agora", lambda g, r: AGORA_BASE.date())),
)


def _agendar(atraso: Callable[[random.Random, Rascunho], timedelta]) -> Aplicar:
    """``publicar_em = agora - atraso`` (atraso negativo = futuro), num fuso aleatório."""
    return _no_post(
        lambda g, r: {
            "publicar_em": (_agora_ref(r) - atraso(g, r)).astimezone(_fuso(g)),
            "somente_data": False,
        }
    )


def _limite(r: Rascunho) -> timedelta:
    return timedelta(minutes=_tolerancia_ref(r))


_US = timedelta(microseconds=1)

# 3. data e hora (com bordas de futuro e da janela)
OPCOES_DATA: Final = (
    _ok(
        "dentro_da_janela",
        _agendar(
            lambda g, r: timedelta(microseconds=g.randint(0, _tolerancia_ref(r) * 60_000_000))
        ),
    ),
    _ok("exatamente_agora", _agendar(lambda g, r: timedelta(0))),
    _ok("exatamente_no_limite_da_janela", _agendar(lambda g, r: _limite(r))),
    _ok("um_us_antes_do_limite", _agendar(lambda g, r: _limite(r) - _US)),
    _ruim("futuro_1us", _agendar(lambda g, r: -_US)),
    _ruim("futuro_1s", _agendar(lambda g, r: -timedelta(seconds=1))),
    _ruim("futuro_1min", _agendar(lambda g, r: -timedelta(minutes=1))),
    _ruim("futuro_dias", _agendar(lambda g, r: -timedelta(days=g.randint(1, 60)))),
    _ruim("passou_da_janela_1us", _agendar(lambda g, r: _limite(r) + _US)),
    _ruim("passou_da_janela_1s", _agendar(lambda g, r: _limite(r) + timedelta(seconds=1))),
    _ruim("passou_da_janela_dias", _agendar(lambda g, r: timedelta(days=g.randint(2, 400)))),
    _ruim("sem_data", _no_post(lambda g, r: {"publicar_em": None, "somente_data": False})),
    _ruim("so_data", _no_post(lambda g, r: {"publicar_em": None, "somente_data": True})),
    _ruim(
        "so_data_mas_com_horario",
        _no_post(lambda g, r: {"publicar_em": _agora_ref(r) - _US, "somente_data": True}),
    ),
    _ruim(
        "somente_data_none",
        _no_post(lambda g, r: {"publicar_em": _agora_ref(r) - _US, "somente_data": None}),
    ),
    _ruim(
        "sem_fuso",
        _no_post(
            lambda g, r: {
                "publicar_em": (_agora_ref(r) - timedelta(minutes=1)).replace(tzinfo=None),
                "somente_data": False,
            }
        ),
    ),
    _ruim(
        "data_texto",
        _no_post(lambda g, r: {"publicar_em": _agora_ref(r).isoformat(), "somente_data": False}),
    ),
)


def _estagio_post(gerar: Callable[[random.Random], Any]) -> Aplicar:
    return _no_post(lambda g, r: {"estagio": gerar(g)})


def _sempre(valor: Any) -> Callable[[random.Random], Any]:
    return lambda _g: valor


# 4. estágio do post (cada papel e nomes parecidos)
OPCOES_ESTAGIO: Final = (
    _ok("confirmado_autorizado", _estagio_post(lambda g: Estagio("confirmado", P.AUTORIZADO))),
    *(
        _ruim(f"papel_{papel.value}", _estagio_post(_sempre(_estagio_de(papel))))
        for papel in P
        if papel is not P.AUTORIZADO
    ),
    _ruim(
        "confirmado_com_papel_pronto",
        _estagio_post(lambda g: _bruto(Estagio, nome="confirmado", papel=P.PRONTO)),
    ),
    _ruim(
        "Confirmado_maiusculo_autorizado",
        _estagio_post(lambda g: _bruto(Estagio, nome="Confirmado", papel=P.AUTORIZADO)),
    ),
    _ruim(
        "confirmado_com_espaco_autorizado",
        _estagio_post(lambda g: _bruto(Estagio, nome="confirmado ", papel=P.AUTORIZADO)),
    ),
    _ruim("confirmados_pronto", _estagio_post(lambda g: Estagio("confirmados", P.PRONTO))),
    _ruim("estagio_texto", _estagio_post(lambda g: "confirmado")),
    _ruim(
        "estagio_papel_texto",
        _estagio_post(lambda g: _bruto(Estagio, nome="confirmado", papel="autorizado")),
    ),
    _ruim("estagio_none", _estagio_post(lambda g: None)),
)


def _config(gerar: Callable[[random.Random], Any]) -> Aplicar:
    return _no_rascunho("configuracao", lambda g, r: gerar(g))


def _sem_autorizador(**extras: Any) -> dict[str, Any]:
    estagios: dict[str, Any] = {k: v for k, v in ESTAGIOS.items() if k != "est-confirmado"}
    estagios.update(extras)
    return estagios


# 5. configuração de estágios
OPCOES_CONFIG: Final = (
    _ok("integra", _config(lambda g: _configuracao(_embaralhado(g, ESTAGIOS)))),
    _ok(
        "integra_com_nomes_parecidos",
        _config(lambda g: _configuracao(_embaralhado(g, {**ESTAGIOS, **PARECIDOS}))),
    ),
    _ruim("zero_autorizadores", _config(lambda g: _configuracao(_sem_autorizador()))),
    _ruim(
        "dois_autorizadores",
        _config(
            lambda g: _configuracao(
                {**ESTAGIOS, "est-confirmado-2": Estagio("confirmado", P.AUTORIZADO)}
            )
        ),
    ),
    _ruim(
        "autorizador_com_outro_nome",
        _config(
            lambda g: _configuracao(
                _sem_autorizador(
                    **{
                        "est-x": _bruto(
                            Estagio, nome=g.choice(["agendado", "Confirmado"]), papel=P.AUTORIZADO
                        )
                    }
                )
            )
        ),
    ),
    _ruim(
        "nome_reservado_em_outro_papel",
        _config(
            lambda g: _configuracao(
                {
                    **ESTAGIOS,
                    "est-x": _bruto(
                        Estagio,
                        nome="confirmado",
                        papel=g.choice([p for p in P if p is not P.AUTORIZADO]),
                    ),
                }
            )
        ),
    ),
    _ruim(
        "com_problemas",
        _config(lambda g: _configuracao(ESTAGIOS, problemas=("estágio sem papel (fictício)",))),
    ),
    _ruim("config_none", _config(lambda g: None)),
    _ruim("config_dict", _config(lambda g: {"estagios": dict(ESTAGIOS)})),
)


# 6. série
def _serie_continua(
    *,
    minimo: int = 1,
    maximo: int = 7,
    anterior_bloqueado: bool = False,
    extras: Callable[[random.Random, int], list[Any]] | None = None,
    lista: bool = False,
) -> Aplicar:
    def aplicar(rng: random.Random, r: Rascunho, _arm: Armazens) -> None:
        k = rng.randint(minimo, maximo)
        anteriores = rng.sample(range(1, k), rng.randint(1 if anterior_bloqueado else 0, k - 1))
        posteriores = rng.sample(range(k + 1, k + 9), rng.randint(0, 3))
        irmaos: list[Any] = [_irmao(o, P.PUBLICADO) for o in anteriores]
        if anterior_bloqueado:
            ruim = rng.choice(anteriores)
            irmaos = [i for i in irmaos if i.ordem_na_serie != ruim]
            papeis = [p for p in P if p is not P.PUBLICADO]
            irmaos.append(_irmao(ruim, rng.choice(papeis)))
        irmaos += [_irmao(o, rng.choice(list(P))) for o in posteriores]
        if rng.random() < 0.4:  # o próprio post relido na lista: ignorado
            irmaos.append(_irmao(rng.randint(1, 9), rng.choice(list(P)), id_=ID_POST))
        if rng.random() < 0.4:  # post de outra série: ignorado
            irmaos.append(
                _irmao(
                    rng.choice([k, 1, None, 0]),
                    rng.choice(list(P)),
                    serie=rng.choice([SERIE_SOLTA, SERIE_EXTRA, None, "Série alheia"]),
                    id_="post-ficticio-t035-alheio",
                )
            )
        if extras is not None:
            irmaos += extras(rng, k)
        rng.shuffle(irmaos)
        r.campos_post.update(serie=SERIE_CONTINUA, ordem_na_serie=k)
        r.posts_da_serie = list(irmaos) if lista or rng.random() < 0.2 else tuple(irmaos)

    return aplicar


def _depois(aplicar: Aplicar, ajuste: Callable[[random.Random, Rascunho], None]) -> Aplicar:
    def combinado(rng: random.Random, r: Rascunho, arm: Armazens) -> None:
        aplicar(rng, r, arm)
        ajuste(rng, r)

    return combinado


def _irmaos_quaisquer(rng: random.Random) -> Any:
    return rng.choice(
        [
            (),
            None,
            ("não é post",),
            tuple(_irmao(o, rng.choice(list(P)), serie=SERIE_SOLTA) for o in (1, 1, 3)),
        ]
    )


def _ajustar_series_da_config(
    mudar: Callable[[dict[str, Any]], None],
) -> Callable[[random.Random, Rascunho], None]:
    def ajuste(_rng: random.Random, r: Rascunho) -> None:
        if isinstance(r.configuracao, Configuracao):
            series = dict(r.configuracao.series)
            mudar(series)
            r.configuracao = replace(r.configuracao, series=series)

    return ajuste


def _ordem_do_post(valor: Any) -> Callable[[random.Random, Rascunho], None]:
    def ajuste(_rng: random.Random, r: Rascunho) -> None:
        r.campos_post["ordem_na_serie"] = valor

    return ajuste


def _com_irmao(gerar: Callable[[random.Random, int], Any]) -> Aplicar:
    return _serie_continua(extras=lambda g, k: [gerar(g, k)])


def _serie_que_nao_bloqueia(serie: str | None, ordens: tuple[Any, ...]) -> Aplicar:
    """Sem série, ou série sem continuidade: ordem e irmãos (até malformados) não importam."""

    def aplicar(rng: random.Random, r: Rascunho, _arm: Armazens) -> None:
        r.campos_post.update(serie=serie, ordem_na_serie=rng.choice(ordens))
        r.posts_da_serie = _irmaos_quaisquer(rng)

    return aplicar


def _ordem_repetida(rng: random.Random, k: int) -> list[Any]:
    o = rng.choice([x for x in range(1, k + 6) if x != k])
    return [
        _irmao(o, P.PUBLICADO, id_="post-ficticio-t035-rep-a"),
        _irmao(o, P.PUBLICADO, id_="post-ficticio-t035-rep-b"),
    ]


OPCOES_SERIE: Final = (
    _ok("sem_serie", _serie_que_nao_bloqueia(None, (None, 0, 3))),
    _ok("serie_sem_continuidade", _serie_que_nao_bloqueia(SERIE_SOLTA, (None, 0, 2, "x"))),
    _ok("continua_anteriores_publicados", _serie_continua()),
    _ok("continua_primeiro_da_serie", _serie_continua(maximo=1)),
    _ok("continua_em_lista", _serie_continua(lista=True)),
    _ruim("anterior_nao_publicado", _serie_continua(minimo=2, anterior_bloqueado=True)),
    _ruim("ordem_repetida_entre_irmaos", _serie_continua(extras=_ordem_repetida)),
    _ruim(
        "irmao_com_a_minha_ordem",
        _com_irmao(lambda g, k: _irmao(k, P.PUBLICADO, id_="post-ficticio-t035-gemeo")),
    ),
    _ruim("post_sem_ordem", _depois(_serie_continua(), _ordem_do_post(None))),
    _ruim("ordem_zero", _depois(_serie_continua(), _ordem_do_post(0))),
    _ruim("ordem_bool", _depois(_serie_continua(), _ordem_do_post(True))),
    _ruim("ordem_texto", _depois(_serie_continua(), _ordem_do_post("2"))),
    _ruim(
        "irmao_sem_ordem",
        _com_irmao(lambda g, k: _irmao(None, P.PUBLICADO, id_="post-ficticio-t035-sem-ordem")),
    ),
    _ruim("irmao_nao_post", _com_irmao(lambda g, k: "post-ficticio-t035-texto")),
    _ruim(
        "lista_none",
        _depois(_serie_continua(), lambda g, r: setattr(r, "posts_da_serie", None)),
    ),
    _ruim(
        "serie_inexistente",
        _depois(_serie_continua(), lambda g, r: r.campos_post.update(serie="Série inexistente")),
    ),
    _ruim(
        "serie_vazia",
        _depois(_serie_continua(), lambda g, r: r.campos_post.update(serie=g.choice(["", "  "]))),
    ),
    _ruim(
        "serie_ambigua_na_config",
        _depois(
            _serie_continua(),
            _ajustar_series_da_config(
                lambda s: s.update({"ser-dup": Serie(SERIE_CONTINUA, continuidade=False)})
            ),
        ),
    ),
    _ruim(
        "config_com_serie_malformada",
        _depois(
            _serie_continua(),
            _ajustar_series_da_config(lambda s: s.update({"ser-lixo": "não é série"})),
        ),
    ),
    _ruim(
        "continuidade_nao_bool",
        _depois(
            _serie_continua(),
            _ajustar_series_da_config(
                lambda s: s.update({"ser-continua": Serie(SERIE_CONTINUA, continuidade="sim")})  # type: ignore[arg-type]
            ),
        ),
    ),
)


def _pedida(gerar: Callable[[random.Random, Rascunho], Any]) -> Aplicar:
    return _no_rascunho("pedida", gerar)


def _fora_do_post(rng: random.Random, r: Rascunho) -> str:
    return rng.choice([p for p in TODAS if p not in r.campos_post["plataformas"]])


# 7. plataforma pedida
OPCOES_PEDIDA: Final = (
    _ok("no_post", _pedida(lambda g, r: r.alvo)),
    _ruim("fora_do_post", _pedida(_fora_do_post)),
    _ruim("vazia", _pedida(lambda g, r: "")),
    _ruim("espacos", _pedida(lambda g, r: "   ")),
    _ruim("caixa_baixa", _pedida(lambda g, r: r.alvo.lower())),
    _ruim("caixa_alta", _pedida(lambda g, r: r.alvo.upper())),
    _ruim("com_espaco", _pedida(lambda g, r: f" {r.alvo}")),
    _ruim("none", _pedida(lambda g, r: None)),
    _ruim("inteiro", _pedida(lambda g, r: 42)),
    _ruim("bytes", _pedida(lambda g, r: r.alvo.encode())),
)


def _adapt(gerar: Callable[[random.Random, Rascunho], Any]) -> Aplicar:
    return _no_rascunho("adaptadores", gerar)


# 8. adaptadores
OPCOES_ADAPTADORES: Final = (
    _ok("todas_frozenset", _adapt(lambda g, r: frozenset(TODAS))),
    _ok("todas_set", _adapt(lambda g, r: set(TODAS))),
    _ok(
        "subconjunto_com_alvo",
        _adapt(lambda g, r: frozenset({r.alvo, *g.sample(TODAS, g.randint(0, len(TODAS)))})),
    ),
    _ruim("sem_alvo", _adapt(lambda g, r: frozenset(p for p in TODAS if p != r.alvo))),
    _ruim("caixa_diferente", _adapt(lambda g, r: frozenset(p.lower() for p in TODAS))),
    _ruim("lista", _adapt(lambda g, r: list(TODAS))),
    _ruim("tupla", _adapt(lambda g, r: tuple(TODAS))),
    _ruim("texto", _adapt(lambda g, r: " ".join(TODAS))),
    _ruim("dict", _adapt(lambda g, r: dict.fromkeys(TODAS, True))),
    _ruim("none", _adapt(lambda g, r: None)),
)


def _conexoes(ajuste: Callable[[random.Random, Rascunho, dict[str, Any]], Any] | None) -> Aplicar:
    def aplicar(rng: random.Random, r: Rascunho, _arm: Armazens) -> None:
        conexoes: dict[str, Any] = _conexoes_validas(rng, r)
        resultado = ajuste(rng, r, conexoes) if ajuste is not None else None
        r.conexoes = conexoes if resultado is None else resultado

    return aplicar


def _alvo_como(
    gerar: Callable[[random.Random, Rascunho], Any],
) -> Callable[[random.Random, Rascunho, dict[str, Any]], None]:
    def ajuste(rng: random.Random, r: Rascunho, conexoes: dict[str, Any]) -> None:
        conexoes[r.alvo] = gerar(rng, r)

    return ajuste


def _outras_invalidas(rng: random.Random, r: Rascunho, conexoes: dict[str, Any]) -> None:
    for p in TODAS:
        if p != r.alvo:
            conexoes[p] = _conexao(p, rng.choice(list(ConexaoEstado)), None)


def _sem_conexao_do_alvo(_rng: random.Random, r: Rascunho, conexoes: dict[str, Any]) -> None:
    del conexoes[r.alvo]


# 9. conexões
OPCOES_CONEXOES: Final = (
    _ok("ativa", _conexoes(None)),
    _ok(
        "ativa_vence_daqui_1us",
        _conexoes(
            _alvo_como(lambda g, r: _conexao(r.alvo, ConexaoEstado.ATIVA, _agora_ref(r) + _US))
        ),
    ),
    _ok("outras_plataformas_invalidas", _conexoes(_outras_invalidas)),
    _ok("mapa_somente_leitura", _conexoes(lambda g, r, c: MappingProxyType(dict(c)))),
    _ruim(
        "expirada",
        _conexoes(
            _alvo_como(
                lambda g, r: _conexao(
                    r.alvo, ConexaoEstado.EXPIRADA, _agora_ref(r) + timedelta(days=9)
                )
            )
        ),
    ),
    _ruim(
        "invalida",
        _conexoes(
            _alvo_como(
                lambda g, r: _conexao(
                    r.alvo, ConexaoEstado.INVALIDA, _agora_ref(r) + timedelta(days=9)
                )
            )
        ),
    ),
    _ruim(
        "ativa_vence_agora",
        _conexoes(_alvo_como(lambda g, r: _conexao(r.alvo, ConexaoEstado.ATIVA, _agora_ref(r)))),
    ),
    _ruim(
        "ativa_vencida",
        _conexoes(
            _alvo_como(
                lambda g, r: _conexao(
                    r.alvo,
                    ConexaoEstado.ATIVA,
                    _agora_ref(r) - timedelta(seconds=g.choice([1, 60, 86400 * 30])),
                )
            )
        ),
    ),
    _ruim(
        "ativa_sem_validade",
        _conexoes(_alvo_como(lambda g, r: _conexao(r.alvo, ConexaoEstado.ATIVA, None))),
    ),
    _ruim(
        "validade_sem_fuso",
        _conexoes(
            _alvo_como(
                lambda g, r: _bruto(
                    Conexao,
                    plataforma=r.alvo,
                    estado=ConexaoEstado.ATIVA,
                    expira_em=(_agora_ref(r) + timedelta(days=9)).replace(tzinfo=None),
                    verificado_em=None,
                )
            )
        ),
    ),
    _ruim(
        "de_outra_plataforma",
        _conexoes(
            _alvo_como(
                lambda g, r: _conexao(
                    g.choice([p for p in TODAS if p != r.alvo]),
                    ConexaoEstado.ATIVA,
                    _agora_ref(r) + timedelta(days=9),
                )
            )
        ),
    ),
    _ruim(
        "estado_texto",
        _conexoes(
            _alvo_como(
                lambda g, r: _bruto(
                    Conexao,
                    plataforma=r.alvo,
                    estado="ativa",
                    expira_em=_agora_ref(r) + timedelta(days=9),
                    verificado_em=None,
                )
            )
        ),
    ),
    _ruim("ausente", _conexoes(_sem_conexao_do_alvo)),
    _ruim("mapa_lista", _conexoes(lambda g, r, c: list(c.items()))),
    _ruim("mapa_none", _no_rascunho("conexoes", lambda g, r: None)),
)


def _tipos(
    ajuste: Callable[[random.Random, Rascunho, dict[str, Any]], Any] | None,
    **campos_post: Any,
) -> Aplicar:
    def aplicar(rng: random.Random, r: Rascunho, _arm: Armazens) -> None:
        tipos: dict[str, Any] = _tipos_validos()
        r.campos_post.update(campos_post)
        resultado = ajuste(rng, r, tipos) if ajuste is not None else None
        r.tipos = tipos if resultado is None else resultado

    return aplicar


def _tipo_do_alvo(valor: Any) -> Callable[[random.Random, Rascunho, dict[str, Any]], None]:
    def ajuste(_rng: random.Random, r: Rascunho, tipos: dict[str, Any]) -> None:
        tipos[r.campos_post["formato"]][r.alvo] = valor

    return ajuste


def _sem_tipo_do_alvo(_rng: random.Random, r: Rascunho, tipos: dict[str, Any]) -> None:
    del tipos[r.campos_post["formato"]][r.alvo]


# 10. tipos por formato (RF-43)
OPCOES_TIPOS: Final = (
    _ok("feed", _tipos(None, formato="Feed")),
    _ok("reels", _tipos(None, formato="Reels")),
    _ok("mapa_somente_leitura", _tipos(lambda g, r, t: MappingProxyType(t))),
    _ruim("formato_fora_do_mapeamento", _tipos(None, formato="Carrossel")),
    _ruim("formato_caixa_diferente", _tipos(None, formato="feed")),
    _ruim("formato_none", _tipos(None, formato=None)),
    _ruim("formato_vazio", _tipos(None, formato="  ")),
    _ruim("plataforma_sem_tipo", _tipos(_sem_tipo_do_alvo)),
    _ruim("tipo_vazio", _tipos(_tipo_do_alvo(""))),
    _ruim("tipo_espacos", _tipos(_tipo_do_alvo("   "))),
    _ruim("tipo_nao_texto", _tipos(_tipo_do_alvo(42))),
    _ruim(
        "mapa_interno_lista",
        _tipos(lambda g, r, t: t.update({r.campos_post["formato"]: list(TODAS)})),
    ),
    _ruim("tipos_none", _tipos(lambda g, r, t: [])),
)


def _midia(gerar: Callable[[random.Random, Rascunho], Any] | None) -> Aplicar:
    def aplicar(rng: random.Random, r: Rascunho, _arm: Armazens) -> None:
        midia: dict[str, Any] = {p: () for p in TODAS}
        if gerar is not None:
            midia[r.alvo] = gerar(rng, r)
        r.midia = midia

    return aplicar


def _midia_sem_alvo(rng: random.Random, r: Rascunho, _arm: Armazens) -> None:
    r.midia = {p: () for p in TODAS if p != r.alvo}


def _outras_com_problema(rng: random.Random, r: Rascunho, _arm: Armazens) -> None:
    r.midia = {p: (() if p == r.alvo else (PROBLEMA_DE_MIDIA,)) for p in TODAS}


# 11. mídia (RF-14)
OPCOES_MIDIA: Final = (
    _ok("validada_sem_problemas", _midia(None)),
    _ok("lista_vazia", _midia(lambda g, r: [])),
    _ok("outras_plataformas_com_problema", _outras_com_problema),
    _ruim("com_problema", _midia(lambda g, r: (PROBLEMA_DE_MIDIA,))),
    _ruim("nao_validada", _midia_sem_alvo),
    _ruim("texto_vazio", _midia(lambda g, r: "")),
    _ruim("none_na_plataforma", _midia(lambda g, r: None)),
    _ruim("mapa_none", _no_rascunho("midia", lambda g, r: None)),
)


def _nao_bloqueantes(rng: random.Random, r: Rascunho) -> list[Any]:
    return [
        _execucao(rng.choice(ESTADOS_NAO_BLOQUEANTES), plataforma=r.alvo, dias=rng.randint(0, 40))
        for _ in range(rng.randint(0, 3))
    ]


def _execucoes(
    extra: Callable[[random.Random, Rascunho], Any] | None, lista: bool = False
) -> Aplicar:
    def aplicar(rng: random.Random, r: Rascunho, _arm: Armazens) -> None:
        itens = _nao_bloqueantes(rng, r)
        if extra is not None:
            itens.append(extra(rng, r))
        rng.shuffle(itens)
        r.execucoes = list(itens) if lista else tuple(itens)

    return aplicar


def _bloqueante(estado: EstadoExecucao) -> Callable[[random.Random, Rascunho], Execucao]:
    return lambda g, r: _execucao(estado, plataforma=r.alvo, dias=g.choice([0, 3, 400]))


# 12. execuções anteriores (RF-12, RF-38)
OPCOES_EXECUCOES: Final = (
    _ok("nenhuma", _no_rascunho("execucoes", lambda g, r: ())),
    _ok("nao_bloqueantes", _execucoes(None)),
    _ok("nao_bloqueantes_em_lista", _execucoes(None, lista=True)),
    *(
        _ruim(
            f"bloqueante_{estado.value}",
            _execucoes(_bloqueante(estado)),
        )
        for estado in sorted(ESTADOS_BLOQUEANTES, key=lambda e: e.value)
    ),
    _ruim(
        "de_outro_post",
        _execucoes(
            lambda g, r: _execucao(
                g.choice(list(E)), post_id="post-ficticio-t035-outro", plataforma=r.alvo
            )
        ),
    ),
    _ruim(
        "de_outra_plataforma",
        _execucoes(
            lambda g, r: _execucao(
                g.choice(list(E)), plataforma=g.choice([p for p in TODAS if p != r.alvo])
            )
        ),
    ),
    _ruim("item_nao_execucao", _execucoes(lambda g, r: "execucao-ficticia")),
    _ruim("estado_texto", _execucoes(lambda g, r: _execucao("recusada", plataforma=r.alvo))),
    _ruim("lista_none", _no_rascunho("execucoes", lambda g, r: None)),
)


def _publicados(
    extra: Callable[[random.Random, Rascunho], list[Any]] | None,
    *,
    lista: bool = False,
    final: Callable[[random.Random, Rascunho, dict[str, Any]], Any] | None = None,
) -> Aplicar:
    def aplicar(rng: random.Random, r: Rascunho, _arm: Armazens) -> None:
        meu = _agendado_ref(r)
        anteriores: list[Any] = [
            PublicacaoConhecida(
                f"post-ficticio-t035-ant-{i}",
                (meu - timedelta(microseconds=rng.randint(1, 30 * 86400 * 10**6))).astimezone(
                    _fuso(rng)
                ),
            )
            for i in range(rng.randint(0, 3))
        ]
        if extra is not None:
            anteriores += extra(rng, r)
        rng.shuffle(anteriores)
        publicados: dict[str, Any] = {p: () for p in TODAS}
        publicados[r.alvo] = list(anteriores) if lista else tuple(anteriores)
        resultado = final(rng, r, publicados) if final is not None else None
        r.publicados = publicados if resultado is None else resultado

    return aplicar


def _posterior(atraso: timedelta) -> Callable[[random.Random, Rascunho], list[Any]]:
    return lambda g, r: [
        PublicacaoConhecida("post-ficticio-t035-depois", (_agendado_ref(r) + atraso))
    ]


def _posteriores_em_outras(rng: random.Random, r: Rascunho, pub: dict[str, Any]) -> None:
    for p in TODAS:
        if p != r.alvo:
            pub[p] = (PublicacaoConhecida("post-ficticio-t035-x", _agendado_ref(r) + timedelta(1)),)


def _sem_chave_do_alvo(_rng: random.Random, r: Rascunho, publicados: dict[str, Any]) -> None:
    del publicados[r.alvo]


# 13. publicados por plataforma (RF-24, A-010)
OPCOES_PUBLICADOS: Final = (
    _ok("so_anteriores", _publicados(None)),
    _ok("anteriores_em_lista", _publicados(None, lista=True)),
    _ok(
        "empate_no_mesmo_instante",
        _publicados(
            lambda g, r: [
                PublicacaoConhecida("post-ficticio-t035-empate", _agendado_ref(r).astimezone(UTC))
            ]
        ),
    ),
    _ok(
        "o_proprio_post_depois",
        _publicados(lambda g, r: [PublicacaoConhecida(ID_POST, _agendado_ref(r) + timedelta(1))]),
    ),
    _ok("posteriores_em_outras_plataformas", _publicados(None, final=_posteriores_em_outras)),
    _ruim("posterior_1us", _publicados(_posterior(_US))),
    _ruim("posterior_1s", _publicados(_posterior(timedelta(seconds=1)))),
    _ruim("posterior_dias", _publicados(_posterior(timedelta(days=12)))),
    _ruim("chave_ausente", _publicados(None, final=_sem_chave_do_alvo)),
    _ruim(
        "item_sem_fuso",
        _publicados(
            lambda g, r: [
                PublicacaoConhecida(
                    "post-ficticio-t035-sf", (_agendado_ref(r) - timedelta(1)).replace(tzinfo=None)
                )
            ]
        ),
    ),
    _ruim(
        "item_id_vazio",
        _publicados(
            lambda g, r: [
                PublicacaoConhecida(g.choice(["", "  "]), _agendado_ref(r) - timedelta(1))
            ]
        ),
    ),
    _ruim(
        "item_nao_publicacao",
        _publicados(lambda g, r: [("post-ficticio-t035-tupla", _agendado_ref(r) - timedelta(1))]),
    ),
    _ruim(
        "valor_texto",
        _publicados(None, final=lambda g, r, p: p.update({r.alvo: "nenhum"})),
    ),
    _ruim("mapa_none", _no_rascunho("publicados", lambda g, r: None)),
)


# 14. selo (RF-26): aplicado por último; edições "depois de selar" vêm em ``apos_selar``
def _selo_fixo(valor: Any) -> Aplicar:
    def aplicar(_rng: random.Random, r: Rascunho, _arm: Armazens) -> None:
        r.selar = False
        r.selo_fixo = valor

    return aplicar


def _apos_selar(editar: Callable[[random.Random, Rascunho, Armazens, Post], None]) -> Aplicar:
    def aplicar(rng: random.Random, r: Rascunho, arm: Armazens) -> None:
        r.apos_selar.append(lambda post: editar(rng, r, arm, post))

    return aplicar


def _editar(**campos: Any) -> Aplicar:
    return _apos_selar(lambda g, r, a, post: _forcar(post, **campos))


def _editar_com(gerar: Callable[[Post], Mapping[str, Any]]) -> Aplicar:
    return _apos_selar(lambda g, r, a, post: _forcar(post, **gerar(post)))


def _armazens(selagem: str, contexto: str) -> Aplicar:
    def aplicar(_rng: random.Random, r: Rascunho, arm: Armazens) -> None:
        r.armazem_selagem = getattr(arm, selagem)
        r.armazem_contexto = getattr(arm, contexto)

    return aplicar


def _selo_de_outro_post(rng: random.Random, r: Rascunho, arm: Armazens, post: Post) -> None:
    outro = copy.copy(post)
    _forcar(outro, corpo=post.corpo + " (outro post fictício)")
    try:
        selo = calcular_selo(outro, arm.principal)
    except Exception:
        selo = SELO_DE_LIXO
    _forcar(post, selo=selo)


def _plataformas_editadas(post: Post) -> dict[str, Any]:
    atuais = list(post.plataformas)
    if len(atuais) > 1:
        return {"plataformas": tuple(atuais[:-1])}
    return {"plataformas": (*atuais, "Substack")}


def _artes_trocadas(post: Post) -> dict[str, Any]:
    return {"artes": tuple(ARTE_2 if a == ARTE_1 else a for a in post.artes)}


def _formato_trocado(post: Post) -> dict[str, Any]:
    return {"formato": "Reels" if post.formato == "Feed" else "Feed"}


def _somar_ao_horario(post: Post) -> dict[str, Any]:
    if isinstance(post.publicar_em, datetime):
        return {"publicar_em": post.publicar_em - timedelta(seconds=1)}
    return {"titulo": post.titulo + " (editado)"}


def _arte_orfa(rng: random.Random, r: Rascunho, _arm: Armazens) -> None:
    r.campos_post["artes"] = (*r.campos_post["artes"], f"Pinterest:{ARTE_2}")


def _armazem_indisponivel(rng: random.Random, r: Rascunho, _arm: Armazens) -> None:
    r.armazem_contexto = _ArmazemIndisponivel()


OPCOES_SELO: Final = (
    _ok("valido", lambda g, r, a: None),
    _ok(
        "valido_edicao_fora_do_selo",
        _editar(tema="Outro tema fictício", editado_em=AGORA_BASE, confirmado_em=AGORA_BASE),
    ),
    _ok(
        "valido_plataformas_reordenadas",
        _editar_com(lambda p: {"plataformas": tuple(reversed(p.plataformas))}),
    ),
    _ok(
        "valido_quebra_de_linha_crlf",
        _editar_com(lambda p: {"corpo": p.corpo.replace("\n", "\r\n")}),
    ),
    _ok("valido_outra_instancia_do_armazem", _armazens("principal", "principal_outra_instancia")),
    _ok("valido_armazem_trocado_nos_dois", _armazens("trocado", "trocado")),
    _ruim("ausente", _selo_fixo(None)),
    _ruim("vazio", _selo_fixo("")),
    _ruim("espacos", _selo_fixo("   ")),
    _ruim("nao_texto", _selo_fixo(12345)),
    _ruim("de_outro_conteudo", _selo_fixo("v1:" + "0" * 64)),
    _ruim("versao_desconhecida", _selo_fixo("v2:" + "a" * 64)),
    _ruim("prefixo_maiusculo", _selo_fixo("V1:" + "a" * 64)),
    _ruim("hash_curto", _selo_fixo("v1:" + "a" * 63)),
    _ruim("lixo", _selo_fixo("selo-ficticio-lixo")),
    _ruim("de_outro_post", _apos_selar(_selo_de_outro_post)),
    _ruim("editado_corpo", _editar_com(lambda p: {"corpo": p.corpo + " editado"})),
    _ruim("editado_corpo_espaco_final", _editar_com(lambda p: {"corpo": p.corpo + " "})),
    _ruim("editado_titulo", _editar_com(lambda p: {"titulo": p.titulo + " editado"})),
    _ruim(
        "editado_secao",
        _editar_com(
            lambda p: {"secoes_por_plataforma": {**p.secoes_por_plataforma, "TikTok": "nova"}}
        ),
    ),
    _ruim("editado_formato", _editar_com(_formato_trocado)),
    _ruim("editado_plataformas", _editar_com(_plataformas_editadas)),
    _ruim("editado_data", _editar_com(_somar_ao_horario)),
    _ruim("editado_artes", _editar_com(_artes_trocadas)),
    _ruim("arte_com_conteudo_trocado", _armazens("principal", "trocado")),
    _ruim("arte_apagada", _armazens("principal", "sem_arte_1")),
    _ruim("arte_orfa", _arte_orfa),
    _ruim("armazem_indisponivel", _armazem_indisponivel),
)

CAMPOS_DE_LIXO: Final = (
    "post",
    "configuracao",
    "agora",
    "parametros",
    "armazem",
    "execucoes_anteriores",
    "publicados_por_plataforma",
    "conexoes",
    "plataformas_com_adaptador",
    "tipos_por_formato",
    "problemas_de_midia",
)


def _lixo_em(campo: str) -> Aplicar:
    def aplicar(rng: random.Random, r: Rascunho, _arm: Armazens) -> None:
        r.lixo = (campo, rng.choice(LIXOS))

    return aplicar


# 15. lixo: um campo do contexto com tipo errado (não inclui ``posts_da_serie``, que só
# importa em série de continuidade e já tem opções próprias na dimensão "serie")
OPCOES_LIXO: Final = (
    _ok("nenhum", lambda g, r, a: None),
    *(_ruim(f"lixo_em_{campo}", _lixo_em(campo)) for campo in CAMPOS_DE_LIXO),
)

# Ordem de aplicação: quem depende de outra dimensão vem depois dela.
DIMENSOES: Final[dict[str, tuple[Opcao, ...]]] = {
    "parametros": OPCOES_PARAMETROS,
    "agora": OPCOES_AGORA,
    "data": OPCOES_DATA,
    "estagio": OPCOES_ESTAGIO,
    "config": OPCOES_CONFIG,
    "serie": OPCOES_SERIE,
    "pedida": OPCOES_PEDIDA,
    "adaptadores": OPCOES_ADAPTADORES,
    "conexoes": OPCOES_CONEXOES,
    "tipos": OPCOES_TIPOS,
    "midia": OPCOES_MIDIA,
    "execucoes": OPCOES_EXECUCOES,
    "publicados": OPCOES_PUBLICADOS,
    "selo": OPCOES_SELO,
    "lixo": OPCOES_LIXO,
}
_POR_NOME: Final = {d: {o.nome: o for o in opcoes} for d, opcoes in DIMENSOES.items()}


# --------------------------------------------------------------------------- casos


@dataclass(frozen=True)
class Caso:
    semente: int
    indice: int
    escolhas: Mapping[str, str]
    valido_pelo_gerador: bool
    contexto: ContextoGuarda
    plataforma: Any
    proibidos: tuple[str, ...]

    def rotulo(self) -> str:
        """Identificação SEM conteúdo do post: semente, índice e nomes das opções."""
        opcoes = ", ".join(f"{d}={n}" for d, n in self.escolhas.items())
        return f"semente={self.semente} indice={self.indice} [{opcoes}]"


def _rng_do_caso(semente: int, indice: int) -> random.Random:
    return random.Random(semente * 1_000_003 + indice)


def _sortear_escolhas(
    rng: random.Random, invalidar: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Todas válidas (40%) ou de 1 a 3 dimensões inválidas; ``invalidar`` fixa escolhas."""
    if invalidar is not None:
        ruins = set(invalidar)
    elif rng.random() < 0.4:
        ruins = set()
    else:
        ruins = set(rng.sample(list(DIMENSOES), rng.randint(1, 3)))
    escolhas: dict[str, str] = {}
    for dim, opcoes in DIMENSOES.items():
        if invalidar is not None and dim in invalidar:
            escolhas[dim] = invalidar[dim]
            continue
        candidatas = [o for o in opcoes if o.valida != (dim in ruins)]
        escolhas[dim] = rng.choice(candidatas).nome
    return escolhas


def _construir_post(r: Rascunho) -> Post:
    campos = dict(r.campos_post)
    publicar_em = campos.pop("publicar_em")
    somente_data = campos.pop("somente_data")
    post = Post(**campos, publicar_em=None, somente_data=False)
    _forcar(post, publicar_em=publicar_em, somente_data=somente_data)
    if r.selar:
        try:
            selo: Any = calcular_selo(post, r.armazem_selagem)
        except Exception:  # dado inválido (sem data, arte órfã...): selo que não confere
            selo = SELO_DE_LIXO
    else:
        selo = r.selo_fixo
    _forcar(post, selo=selo)
    for editar in r.apos_selar:
        editar(post)
    return post


def gerar_caso(
    arm: Armazens,
    semente: int,
    indice: int,
    escolhas: Mapping[str, str] | None = None,
    *,
    invalidar: Mapping[str, str] | None = None,
) -> Caso:
    rng = _rng_do_caso(semente, indice)
    if escolhas is None:
        escolhas = _sortear_escolhas(rng, invalidar)
    r = _rascunho_base(rng, arm)
    r.conexoes = _conexoes_validas(rng, r)
    for dim in DIMENSOES:
        _POR_NOME[dim][escolhas[dim]].aplicar(rng, r, arm)
    post = _construir_post(r)
    contexto = ContextoGuarda(
        post=post,
        configuracao=r.configuracao,
        agora=r.agora,
        parametros=r.parametros,
        armazem=r.armazem_contexto,
        execucoes_anteriores=r.execucoes,
        publicados_por_plataforma=r.publicados,
        posts_da_serie=r.posts_da_serie,
        conexoes=r.conexoes,
        plataformas_com_adaptador=r.adaptadores,
        tipos_por_formato=r.tipos,
        problemas_de_midia=r.midia,
    )
    if r.lixo is not None:
        campo, valor = r.lixo
        contexto = replace(contexto, **{campo: valor})
    proibidos = [TOKEN_FICTICIO, TITULO, CORPO, SECAO, PROBLEMA_DE_MIDIA, ARTE_1, ARTE_2]
    if isinstance(post.selo, str) and len(post.selo.strip()) >= 8:
        proibidos.append(post.selo)
    return Caso(
        semente=semente,
        indice=indice,
        escolhas=dict(escolhas),
        valido_pelo_gerador=all(_POR_NOME[d][n].valida for d, n in escolhas.items()),
        contexto=contexto,
        plataforma=r.pedida,
        proibidos=tuple(proibidos),
    )


# --------------------------------------------------------------------------- oráculo
#
# Escrito a partir do plano 4.1 e das decisões documentadas (T030 a T034), como uma tabela de
# requisitos independentes. Cada requisito diz "sim" só quando consegue PROVAR a condição;
# qualquer exceção conta como "não" (falha segura).


def _instante(valor: object) -> datetime | None:
    """Instante absoluto (UTC) de um ``datetime`` com fuso; ``None`` para qualquer outra coisa."""
    if not isinstance(valor, datetime) or valor.tzinfo is None or valor.utcoffset() is None:
        return None
    return valor.astimezone(UTC)


def _preenchido(valor: object) -> bool:
    return isinstance(valor, str) and valor.strip() != ""


def _inteiro_positivo(valor: object) -> bool:
    return type(valor) is int and valor > 0


def _sequencia(valor: object) -> bool:
    return type(valor) in (tuple, list)


def _post(c: ContextoGuarda) -> Post:
    assert isinstance(c.post, Post)
    return c.post


def _config_de(c: ContextoGuarda) -> Configuracao:
    assert isinstance(c.configuracao, Configuracao)
    return c.configuracao


def _unico_autorizador(cfg: Configuracao) -> Estagio | None:
    """O estágio autorizador de uma configuração íntegra (RF-39), ou ``None``."""
    if len(cfg.problemas) != 0:
        return None
    estagios = list(cfg.estagios.values())
    autorizadores = [e for e in estagios if e.papel is P.AUTORIZADO]
    usam_o_nome = [e for e in estagios if e.nome == NOME_ESTAGIO_AUTORIZADOR]
    if len(autorizadores) != 1 or autorizadores[0].nome != NOME_ESTAGIO_AUTORIZADOR:
        return None
    if any(e.papel is not P.AUTORIZADO for e in usam_o_nome):
        return None
    return autorizadores[0]


def _req_leitura(c: ContextoGuarda, _p: Any) -> bool:
    return isinstance(c.post, Post) and isinstance(c.configuracao, Configuracao)


def _req_estagio(c: ContextoGuarda, _p: Any) -> bool:
    autorizador = _unico_autorizador(_config_de(c))
    estagio = _post(c).estagio
    return (
        autorizador is not None
        and isinstance(estagio, Estagio)
        and estagio.papel is P.AUTORIZADO
        and estagio.nome == autorizador.nome
        and estagio.papel == autorizador.papel
    )


def _req_data_e_hora(c: ContextoGuarda, _p: Any) -> bool:
    post = _post(c)
    return post.somente_data is False and _instante(post.publicar_em) is not None


def _req_nao_futuro(c: ContextoGuarda, _p: Any) -> bool:
    agendado, agora = _instante(_post(c).publicar_em), _instante(c.agora)
    return agendado is not None and agora is not None and agendado <= agora


def _req_selo(c: ContextoGuarda, _p: Any) -> bool:
    post = _post(c)
    return _preenchido(post.selo) and verificar_selo(post, c.armazem, post.selo) is True


def _req_plataforma_do_post(c: ContextoGuarda, p: Any) -> bool:
    plataformas = _post(c).plataformas
    return _preenchido(p) and _sequencia(plataformas) and p in plataformas


def _req_janela(c: ContextoGuarda, _p: Any) -> bool:
    agendado, agora = _instante(_post(c).publicar_em), _instante(c.agora)
    parametros = c.parametros
    if agendado is None or agora is None or not isinstance(parametros, Parametros):
        return False
    tolerancia = parametros.janela_tolerancia_minutos
    return _inteiro_positivo(tolerancia) and agora - agendado <= timedelta(minutes=tolerancia)


def _req_ordem(c: ContextoGuarda, p: Any) -> bool:
    post = _post(c)
    meu = _instante(post.publicar_em)
    publicados = c.publicados_por_plataforma
    if meu is None or not _preenchido(post.id) or not isinstance(publicados, Mapping):
        return False
    if p not in publicados or not _sequencia(publicados[p]):
        return False
    for item in publicados[p]:
        if not isinstance(item, PublicacaoConhecida) or not _preenchido(item.post_id):
            return False
        quando = _instante(item.agendado_para)
        if quando is None:
            return False
        if item.post_id != post.id and quando > meu:
            return False
    return True


def _req_serie(c: ContextoGuarda, _p: Any) -> bool:
    post = _post(c)
    nome = post.serie
    if nome is None:
        return True
    series = _config_de(c).series
    if not _preenchido(nome) or not isinstance(series, Mapping):
        return False
    todas = list(series.values())
    if not all(isinstance(s, Serie) and type(s.continuidade) is bool for s in todas):
        return False
    com_o_nome = [s for s in todas if s.nome == nome]
    if len(com_o_nome) != 1:
        return False
    if not com_o_nome[0].continuidade:
        return True
    minha = post.ordem_na_serie
    irmaos = c.posts_da_serie
    if not _inteiro_positivo(minha) or not _preenchido(post.id) or not _sequencia(irmaos):
        return False
    if not all(isinstance(i, Post) for i in irmaos):
        return False
    da_serie = [i for i in irmaos if i.id != post.id and i.serie == nome]
    ordens = [minha, *(i.ordem_na_serie for i in da_serie)]
    if not all(_inteiro_positivo(o) for o in ordens) or len(set(ordens)) != len(ordens):
        return False
    return all(
        isinstance(i.estagio, Estagio) and i.estagio.papel is P.PUBLICADO
        for i in da_serie
        if i.ordem_na_serie < minha  # type: ignore[operator]
    )


def _req_adaptador(c: ContextoGuarda, p: Any) -> bool:
    disponiveis = c.plataformas_com_adaptador
    return _preenchido(p) and type(disponiveis) in (set, frozenset) and p in disponiveis


def _req_conexao(c: ContextoGuarda, p: Any) -> bool:
    conexoes, agora = c.conexoes, _instante(c.agora)
    if not _preenchido(p) or not isinstance(conexoes, Mapping) or p not in conexoes:
        return False
    conexao = conexoes[p]
    if not isinstance(conexao, Conexao) or conexao.plataforma != p:
        return False
    expira = _instante(conexao.expira_em)
    return (
        conexao.estado is ConexaoEstado.ATIVA
        and expira is not None
        and agora is not None
        and expira > agora
    )


def _req_formato(c: ContextoGuarda, p: Any) -> bool:
    formato: Any = _post(c).formato
    tipos: Any = c.tipos_por_formato
    if not _preenchido(formato) or not _preenchido(p) or not isinstance(tipos, Mapping):
        return False
    if formato not in tipos or not isinstance(tipos[formato], Mapping):
        return False
    return p in tipos[formato] and _preenchido(tipos[formato][p])


def _req_midia(c: ContextoGuarda, p: Any) -> bool:
    midia = c.problemas_de_midia
    if not _preenchido(p) or not isinstance(midia, Mapping) or p not in midia:
        return False
    return _sequencia(midia[p]) and len(midia[p]) == 0


def _req_execucoes(c: ContextoGuarda, p: Any) -> bool:
    post, execucoes = _post(c), c.execucoes_anteriores
    if not _preenchido(post.id) or not _preenchido(p) or not _sequencia(execucoes):
        return False
    for execucao in execucoes:
        if not isinstance(execucao, Execucao) or not isinstance(execucao.estado, E):
            return False
        if execucao.post_id != post.id or execucao.plataforma != p:
            return False
        if execucao.estado in ESTADOS_BLOQUEANTES:
            return False
    return True


REQUISITOS: Final[tuple[tuple[str, Callable[[ContextoGuarda, Any], bool]], ...]] = (
    ("leitura", _req_leitura),
    ("estagio_autorizador", _req_estagio),
    ("data_e_hora", _req_data_e_hora),
    ("nao_futuro", _req_nao_futuro),
    ("selo", _req_selo),
    ("plataforma_do_post", _req_plataforma_do_post),
    ("janela", _req_janela),
    ("ordem_por_plataforma", _req_ordem),
    ("serie", _req_serie),
    ("adaptador", _req_adaptador),
    ("conexao", _req_conexao),
    ("formato", _req_formato),
    ("midia", _req_midia),
    ("execucoes", _req_execucoes),
)


def _cumpre(requisito: Callable[[ContextoGuarda, Any], bool], c: ContextoGuarda, p: Any) -> bool:
    try:
        return requisito(c, p) is True
    except Exception:
        return False


def deveria_permitir(contexto: ContextoGuarda, plataforma: Any) -> bool:
    """Oráculo: a conjunção de TODOS os requisitos (plano 4.1)."""
    return all(_cumpre(requisito, contexto, plataforma) for _, requisito in REQUISITOS)


def violacoes_do_i2(contexto: ContextoGuarda) -> list[str]:
    """As quatro condições do I-2, checadas DIRETO no post (independente do oráculo)."""
    falhas: list[str] = []
    post, cfg = contexto.post, contexto.configuracao
    if not isinstance(post, Post) or not isinstance(cfg, Configuracao):
        return ["post_ou_configuracao_ausente"]
    try:
        autorizadores = [e for e in cfg.estagios.values() if e.papel is P.AUTORIZADO]
        reservado_em_outro = [
            e
            for e in cfg.estagios.values()
            if e.nome == NOME_ESTAGIO_AUTORIZADOR and e.papel is not P.AUTORIZADO
        ]
        if not (
            isinstance(post.estagio, Estagio)
            and post.estagio.papel is P.AUTORIZADO
            and post.estagio.nome == NOME_ESTAGIO_AUTORIZADOR
            and not cfg.problemas
            and len(autorizadores) == 1
            and autorizadores[0].nome == NOME_ESTAGIO_AUTORIZADOR
            and not reservado_em_outro
        ):
            falhas.append("papel_autorizado_confirmado")
    except Exception:
        falhas.append("papel_autorizado_confirmado")
    agendado = _instante(post.publicar_em)
    if post.somente_data is not False or agendado is None:
        falhas.append("data_e_hora_com_fuso")
    try:
        if verificar_selo(post, contexto.armazem, post.selo) is not True:
            falhas.append("selo_valido")
    except Exception:
        falhas.append("selo_valido")
    agora = _instante(contexto.agora)
    if agendado is None or agora is None or agendado > agora:
        falhas.append("sem_horario_futuro")
    return falhas


# --------------------------------------------------------------------------- propriedades

AvaliarFn = Callable[[ContextoGuarda, Any], ResultadoGuarda]


def _checar_caso(caso: Caso, avaliar_fn: AvaliarFn = avaliar) -> tuple[list[str], bool | None]:
    """Devolve (propriedades violadas, ``permitido``). Mensagens sem conteúdo do post."""
    falhas: list[str] = []
    c, p = caso.contexto, caso.plataforma
    try:
        resultado = avaliar_fn(c, p)
    except Exception as erro:  # propriedade 1: nunca levanta
        return [f"levantou_excecao:{type(erro).__name__}"], None
    if not isinstance(resultado, ResultadoGuarda) or type(resultado.permitido) is not bool:
        return ["resultado_malformado"], None
    if not resultado.permitido and (
        len(resultado.motivos) != 1 or not isinstance(resultado.motivos[0], MotivoRecusa)
    ):
        falhas.append("recusa_sem_exatamente_um_motivo")
    # Propriedade 2 (a mais importante): direção única, independente do oráculo.
    if resultado.permitido:
        falhas += [f"SEGURANCA_I2_violada:{v}" for v in violacoes_do_i2(c)]
    esperado = deveria_permitir(c, p)
    if resultado.permitido != esperado:
        falhas.append(f"diverge_do_oraculo(guarda={resultado.permitido},oraculo={esperado})")
    if esperado != caso.valido_pelo_gerador:
        reprovados = [n for n, req in REQUISITOS if not _cumpre(req, c, p)]
        falhas.append(f"gerador_diverge_do_oraculo(requisitos_reprovados={reprovados})")
    try:
        if avaliar_fn(c, p) != resultado:
            falhas.append("nao_deterministico")
    except Exception as erro:
        falhas.append(f"levantou_excecao_na_repeticao:{type(erro).__name__}")
    textos = (repr(resultado), str(resultado), resultado.detalhe or "")
    if any(proibido in texto for texto in textos for proibido in caso.proibidos):
        falhas.append("resultado_vaza_conteudo_ou_token")
    return falhas, resultado.permitido


@dataclass
class Relatorio:
    casos: int = 0
    permitidos: int = 0
    falhas: list[str] = field(default_factory=list)
    opcoes_usadas: set[tuple[str, str]] = field(default_factory=set)

    def registrar(self, caso: Caso, avaliar_fn: AvaliarFn = avaliar) -> None:
        self.casos += 1
        self.opcoes_usadas.update(caso.escolhas.items())
        falhas, permitido = _checar_caso(caso, avaliar_fn)
        self.permitidos += permitido is True
        if falhas:
            self.falhas.append(f"{caso.rotulo()} -> {falhas}")

    def mensagem(self, titulo: str) -> str:
        primeiras = "\n".join(self.falhas[:10])
        return (
            f"{titulo}: {len(self.falhas)} de {self.casos} casos violaram propriedades "
            f"(reproduza com PUBLICADOR_T035_SEMENTE e o índice):\n{primeiras}"
        )


def _casos_aleatorios(arm: Armazens, semente: int, quantidade: int) -> Iterator[Caso]:
    for indice in range(quantidade):
        yield gerar_caso(arm, semente, indice)


def _casos_de_mutacao(
    arm: Armazens, semente: int, por_dimensao: int, dimensoes: tuple[str, ...] | None = None
) -> Iterator[Caso]:
    """Caminho feliz aleatório com EXATAMENTE uma dimensão trocada por uma opção inválida."""
    indice = 0
    for dim in dimensoes or tuple(DIMENSOES):
        invalidas = [o.nome for o in DIMENSOES[dim] if not o.valida]
        por_opcao = max(1, -(-por_dimensao // len(invalidas)))
        for nome in invalidas:
            for _ in range(por_opcao):
                indice += 1
                yield gerar_caso(arm, semente + 7, indice, invalidar={dim: nome})


# --------------------------------------------------------------------------- testes


def test_propriedade_i2_com_gerador_deterministico(
    armazens: Armazens, capsys: pytest.CaptureFixture[str]
) -> None:
    """Milhares de casos aleatórios: segurança, oráculo, sem exceção, determinismo, I-1."""
    relatorio = Relatorio()
    for caso in _casos_aleatorios(armazens, SEMENTE, CASOS):
        relatorio.registrar(caso)
    assert not relatorio.falhas, relatorio.mensagem("Propriedade I-2")
    assert relatorio.casos >= MINIMO_DE_CASOS or CASOS < MINIMO_DE_CASOS
    # O gerador precisa exercitar os dois lados e TODAS as opções de todas as dimensões.
    assert relatorio.permitidos >= relatorio.casos // 5, relatorio.permitidos
    assert relatorio.casos - relatorio.permitidos >= relatorio.casos // 5
    todas = {(d, o.nome) for d, opcoes in DIMENSOES.items() for o in opcoes}
    if relatorio.casos >= MINIMO_DE_CASOS:
        assert todas <= relatorio.opcoes_usadas, sorted(todas - relatorio.opcoes_usadas)
    with capsys.disabled():
        print(
            f"\n[T035] semente={SEMENTE} casos={relatorio.casos} "
            f"permitidos={relatorio.permitidos} recusados={relatorio.casos - relatorio.permitidos}"
        )


def test_mutacao_de_um_campo_do_caminho_feliz_nunca_permite(armazens: Armazens) -> None:
    """Cada opção inválida de cada dimensão, sozinha, sobre um caminho feliz: sempre recusa."""
    relatorio = Relatorio()
    permitidos: list[str] = []
    contagem: dict[str, int] = dict.fromkeys(DIMENSOES, 0)
    for caso in _casos_de_mutacao(armazens, SEMENTE, MUTACOES_POR_DIMENSAO):
        assert caso.valido_pelo_gerador is False
        relatorio.registrar(caso)
        contagem[next(d for d, n in caso.escolhas.items() if not _POR_NOME[d][n].valida)] += 1
        if avaliar(caso.contexto, caso.plataforma).permitido:
            permitidos.append(caso.rotulo())
    assert not permitidos, f"{len(permitidos)} mutações permitidas:\n" + "\n".join(permitidos[:10])
    assert not relatorio.falhas, relatorio.mensagem("Mutação de um campo")
    assert min(contagem.values()) >= MUTACOES_POR_DIMENSAO, contagem


def test_caminho_feliz_do_gerador_libera(armazens: Armazens) -> None:
    """Sanidade do gerador: todas as dimensões válidas -> a guarda permite."""
    for indice in range(200):
        rng = _rng_do_caso(SEMENTE + 11, indice)
        caso = gerar_caso(armazens, SEMENTE + 11, indice, _sortear_escolhas(rng, {}))
        assert caso.valido_pelo_gerador
        assert avaliar(caso.contexto, caso.plataforma).permitido is True, caso.rotulo()


def test_gerador_e_reprodutivel(armazens: Armazens) -> None:
    """Mesma semente e índice -> mesmas escolhas e mesmo resultado (para reproduzir falhas)."""
    for indice in (0, 1, 17, 999, 4321):
        a = gerar_caso(armazens, SEMENTE, indice)
        b = gerar_caso(armazens, SEMENTE, indice)
        assert a.escolhas == b.escolhas
        assert a.plataforma == b.plataforma or a.plataforma is b.plataforma
        assert avaliar(a.contexto, a.plataforma) == avaliar(b.contexto, b.plataforma)


def _guarda_sem(*verificacoes: str) -> list[Any]:
    nomes = set(verificacoes)
    return [item for item in modulo_guarda._VERIFICACOES if item[0].__name__ not in nomes]


@pytest.mark.parametrize(
    "removidas",
    [
        pytest.param(("_estagio",), id="sem-verificacao-de-estagio"),
        pytest.param(("_futuro",), id="sem-verificacao-de-horario-futuro"),
        pytest.param(("_selo_confere",), id="sem-recalculo-do-selo"),
        pytest.param(("_hora",), id="sem-verificacao-de-hora"),
        pytest.param(("_execucao",), id="sem-verificacao-de-execucao"),
    ],
)
def test_propriedades_apanham_guarda_defeituosa(
    armazens: Armazens, monkeypatch: pytest.MonkeyPatch, removidas: tuple[str, ...]
) -> None:
    """Autoteste: com uma verificação removida (só em memória), as propriedades falham."""
    monkeypatch.setattr(modulo_guarda, "_VERIFICACOES", tuple(_guarda_sem(*removidas)))
    relatorio = Relatorio()
    for caso in _casos_de_mutacao(armazens, SEMENTE, 30):
        relatorio.registrar(caso)
    assert relatorio.falhas, f"guarda sem {removidas} passou despercebida"


def test_propriedades_apanham_guarda_que_sempre_permite(armazens: Armazens) -> None:
    def sempre_permite(_c: ContextoGuarda, _p: Any) -> ResultadoGuarda:
        return ResultadoGuarda.permitir()

    relatorio = Relatorio()
    for caso in _casos_aleatorios(armazens, SEMENTE, 300):
        relatorio.registrar(caso, sempre_permite)
    assert any("SEGURANCA_I2_violada" in f for f in relatorio.falhas)


def test_propriedade_i2_com_hypothesis(armazens: Armazens) -> None:
    """Mesmas dimensões como estratégias do hypothesis (extra ``dev``; pulado sem o pacote)."""
    hypothesis = pytest.importorskip("hypothesis")
    st = pytest.importorskip("hypothesis.strategies")

    # Opções válidas primeiro: o hypothesis encolhe para o caminho feliz e isola a culpada.
    estrategia = st.fixed_dictionaries(
        {
            dim: st.sampled_from(
                [o.nome for o in opcoes if o.valida] + [o.nome for o in opcoes if not o.valida]
            )
            for dim, opcoes in DIMENSOES.items()
        }
    )

    def propriedade(escolhas: dict[str, str], semente: int) -> None:
        caso = gerar_caso(armazens, semente, 0, escolhas)
        falhas, _ = _checar_caso(caso)
        assert not falhas, f"{caso.rotulo()} -> {falhas}"

    executar = hypothesis.settings(
        max_examples=int(os.environ.get("PUBLICADOR_T035_HYPOTHESIS", "1500")),
        deadline=None,
        database=None,
        derandomize=True,
    )(hypothesis.given(escolhas=estrategia, semente=st.integers(0, 2**32 - 1))(propriedade))
    executar()
