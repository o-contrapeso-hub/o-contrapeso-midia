"""Registro de execuções no Notion (banco Execuções): RF-13, RF-15, RF-29, RF-12.

Regras invioláveis:
- I-1: o registro só contém ids, plataforma, estado, CÓDIGO de motivo, chave de
  idempotência, tentativa, instantes e identificador externo. Nunca título, corpo,
  legenda nem credencial. Todo valor gravado passa por validação de formato (ids,
  códigos e chaves têm padrão fechado); o motivo é um enum, nunca texto livre, e a
  propriedade "Texto do motivo" do banco NÃO é usada por este módulo.
- I-2: este módulo não autoriza nada. Gravar ``iniciada`` não é permissão; quem decide
  é a guarda. Aqui só se registra e se lê.

Falha segura: ``iniciar_execucao`` grava ANTES de enviar. Se a escrita falhar (inclusive
``ErroNotionIncerto``), o erro propaga SEM nova tentativa: sem registro confirmado,
quem chama NÃO deve publicar. Se a criação foi incerta, a página pode existir; a
checagem por chave/post (RF-12/RF-38) a encontrará na próxima execução.

Mapeamento pelos nomes reais do banco (docs/notion-schema.md):
  Chave de idempotência (title), Post (relation), Plataforma (select), Tentativa (select
  "1"/"2"), Resultado (select), Código do motivo (rich_text), Início/Fim (date), ID do
  post na plataforma (rich_text).

Decisões onde o esquema de teste não cobre o plano (pendências):
- Não existe a opção ``iniciada`` em "Resultado": estado ``iniciada`` = Resultado vazio
  e Fim vazio. Ao concluir, grava-se Resultado e Fim.
- Não existe propriedade "último sinal": o sinal de vida é um registro próprio com
  Resultado "sinal de vida" (plano, seção 3), sem Post; "Fim" guarda o instante do
  último sinal e é a única propriedade atualizada nas batidas seguintes.
- "atrasado" (calculado pelo painel) é lido como ``NAO_EXECUTADO``.
- ``IdsBancos`` ainda não tem o id do banco Execuções; usa-se qualquer objeto com o
  atributo ``execucoes``.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Any, Final, Protocol

from publicador.core.modelos import EstadoExecucao, Execucao, MotivoRecusa
from publicador.core.redacao import obter_logger
from publicador.notion.cliente import ClienteNotion, ErroNotionIncerto

__all__ = [
    "CodigoMotivo",
    "ErroRegistroExecucao",
    "RegistroExecucao",
    "buscar_por_chave",
    "chave_de",
    "execucoes_do_post",
    "iniciar_execucao",
    "registrar_resultado",
    "sinal_de_vida",
]

_log = obter_logger("publicador.job.execucoes")

_PROP_CHAVE: Final = "Chave de idempotência"
_PROP_POST: Final = "Post"
_PROP_PLATAFORMA: Final = "Plataforma"
_PROP_TENTATIVA: Final = "Tentativa"
_PROP_RESULTADO: Final = "Resultado"
_PROP_MOTIVO: Final = "Código do motivo"
_PROP_INICIO: Final = "Início"
_PROP_FIM: Final = "Fim"
_PROP_ID_EXTERNO: Final = "ID do post na plataforma"

_RESULTADO_SINAL: Final = "sinal de vida"
_CHAVE_SINAL: Final = "sinal-de-vida"
_TENTATIVAS_VALIDAS: Final = frozenset({1, 2})

_NOME_RESULTADO: Final[dict[EstadoExecucao, str]] = {
    EstadoExecucao.SUCESSO: "sucesso",
    EstadoExecucao.SUCESSO_MODO_PRIVADO: "sucesso em modo privado",
    EstadoExecucao.FALHA: "falha",
    EstadoExecucao.RECUSADA: "recusado",
    EstadoExecucao.INCERTO: "incerto",
    EstadoExecucao.SIMULADA: "simulado",
    EstadoExecucao.NAO_EXECUTADO: "não executado",
}
_ESTADO_DO_NOME: Final[dict[str, EstadoExecucao]] = {
    **{nome: estado for estado, nome in _NOME_RESULTADO.items()},
    "atrasado": EstadoExecucao.NAO_EXECUTADO,
}

_RE_ID: Final = re.compile(r"[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
_RE_CHAVE: Final = re.compile(r"[A-Za-z0-9_\-]{1,80}")
_RE_PLATAFORMA: Final = re.compile(r"[A-Za-z0-9_\-]{1,40}")
_RE_ID_EXTERNO: Final = re.compile(r"[A-Za-z0-9_.:\-]{1,128}")
_RE_CODIGO: Final = re.compile(r"[a-z0-9_]{1,40}")


class CodigoMotivo(Enum):
    """Códigos de motivo além dos de recusa da guarda (``MotivoRecusa``). Nunca texto livre."""

    ERRO_TRANSITORIO = "erro_transitorio"
    ERRO_PERMANENTE = "erro_permanente"
    ERRO_DESCONHECIDO = "erro_desconhecido"
    NOTION_INCERTO = "notion_incerto"
    PLATAFORMA_NAO_CONFIRMOU = "plataforma_nao_confirmou"
    PLATAFORMA_CONFIRMOU_ENVIO = "plataforma_confirmou_envio"
    MODO_PRIVADO = "modo_privado"
    SIMULACAO = "simulacao"
    EXECUCAO_PERDIDA = "execucao_perdida"
    SEM_CONFIRMACAO_DA_PLATAFORMA = "sem_confirmacao_da_plataforma"


class ErroRegistroExecucao(Exception):
    """Registro ilegível ou resposta inesperada do Notion. Mensagem sem dados sensíveis."""


class _TemBancoExecucoes(Protocol):
    @property
    def execucoes(self) -> str: ...


@dataclass(frozen=True)
class RegistroExecucao:
    """Uma linha do banco Execuções: a página do Notion mais a ``Execucao`` do domínio."""

    pagina_id: str
    execucao: Execucao
    id_externo: str | None = None


# --------------------------------------------------------------------------- validação


def _normalizar_id(valor: str, rotulo: str) -> str:
    if not isinstance(valor, str) or _RE_ID.fullmatch(valor) is None:
        raise ValueError(f"{rotulo} inválido (esperado id hex/UUID)")
    return valor.replace("-", "").lower()


def _padrao(valor: str, regex: re.Pattern[str], rotulo: str) -> str:
    if not isinstance(valor, str) or regex.fullmatch(valor) is None:
        raise ValueError(f"{rotulo} inválido (formato fechado; texto livre não é aceito)")
    return valor


def _exigir_fuso(valor: datetime, rotulo: str) -> None:
    if valor.tzinfo is None or valor.utcoffset() is None:
        raise ValueError(f"{rotulo} deve ter fuso horário (datetime ingênuo é rejeitado)")


def _codigo(motivo: MotivoRecusa | CodigoMotivo | None) -> str | None:
    if motivo is None:
        return None
    if not isinstance(motivo, MotivoRecusa | CodigoMotivo):
        raise ValueError("motivo deve ser um código de enum, nunca texto livre")
    return _padrao(motivo.value, _RE_CODIGO, "código do motivo")


def chave_de(post_id: str, plataforma: str, selo: str) -> str:
    """Chave de idempotência determinística: ``v1-`` + 32 hex do SHA-256 do trio.

    O trio é codificado com prefixo de tamanho (``len:valor``), então nenhum valor,
    mesmo com separadores, colide com outro trio. O selo (ele próprio um hash) entra
    só no hash: a chave não o expõe. Mudar o conteúdo muda o selo e, portanto, a chave;
    a checagem de duplicidade entre selos é por post e plataforma (``execucoes_do_post``).
    Nenhum conteúdo do post entra aqui.
    """
    for rotulo, valor in (("post_id", post_id), ("plataforma", plataforma), ("selo", selo)):
        if not isinstance(valor, str) or not valor.strip():
            raise ValueError(f"{rotulo} não pode ser vazio")
    canonico = "".join(f"{len(v)}:{v};" for v in (post_id, plataforma, selo))
    resumo = hashlib.sha256(canonico.encode("utf-8")).hexdigest()[:32]
    return f"v1-{resumo}"


# --------------------------------------------------------------------------- tradução


def _texto(conteudo: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": {"content": conteudo}}]


def _data(valor: datetime) -> dict[str, Any]:
    return {"date": {"start": valor.isoformat()}}


def _ler_texto(prop: Any) -> str:
    if not isinstance(prop, dict):
        return ""
    for tipo in ("title", "rich_text"):
        partes = prop.get(tipo)
        if isinstance(partes, list):
            return "".join(
                p.get("plain_text") or (p.get("text") or {}).get("content", "")
                for p in partes
                if isinstance(p, dict)
            )
    return ""


def _ler_select(prop: Any) -> str | None:
    sel = prop.get("select") if isinstance(prop, dict) else None
    nome = sel.get("name") if isinstance(sel, dict) else None
    return nome if isinstance(nome, str) and nome else None


def _ler_data(prop: Any) -> datetime | None:
    d = prop.get("date") if isinstance(prop, dict) else None
    inicio = d.get("start") if isinstance(d, dict) else None
    if not isinstance(inicio, str):
        return None
    try:
        valor = datetime.fromisoformat(inicio)
    except ValueError:
        raise ErroRegistroExecucao("data inválida em registro de execução") from None
    if valor.tzinfo is None:
        raise ErroRegistroExecucao("data sem fuso em registro de execução")
    return valor


def _registro_de_pagina(pagina: dict[str, Any]) -> RegistroExecucao | None:
    """Converte a linha; ``None`` para sinal de vida; ``ErroRegistroExecucao`` se ilegível."""
    props = pagina.get("properties")
    pagina_id = pagina.get("id")
    if not isinstance(props, dict) or not isinstance(pagina_id, str) or not pagina_id:
        raise ErroRegistroExecucao("registro de execução ilegível")
    nome = _ler_select(props.get(_PROP_RESULTADO))
    if nome == _RESULTADO_SINAL:
        return None
    if nome is None:
        estado = EstadoExecucao.INICIADA
    elif nome in _ESTADO_DO_NOME:
        estado = _ESTADO_DO_NOME[nome]
    else:
        raise ErroRegistroExecucao("resultado desconhecido em registro de execução")
    relacao = (props.get(_PROP_POST) or {}).get("relation")
    if not isinstance(relacao, list) or len(relacao) != 1 or not isinstance(relacao[0], dict):
        raise ErroRegistroExecucao("registro de execução sem post único")
    tentativa_bruta = _ler_select(props.get(_PROP_TENTATIVA))
    plataforma = _ler_select(props.get(_PROP_PLATAFORMA))
    inicio = _ler_data(props.get(_PROP_INICIO))
    if tentativa_bruta is None or not tentativa_bruta.isdigit() or plataforma is None:
        raise ErroRegistroExecucao("registro de execução incompleto")
    if inicio is None:
        raise ErroRegistroExecucao("registro de execução sem início")
    try:
        execucao = Execucao(
            chave_idempotencia=_ler_texto(props.get(_PROP_CHAVE)),
            post_id=_normalizar_id(str(relacao[0].get("id")), "post"),
            plataforma=plataforma,
            estado=estado,
            tentativa=int(tentativa_bruta),
            iniciada_em=inicio,
            finalizada_em=_ler_data(props.get(_PROP_FIM)),
            motivo=_ler_texto(props.get(_PROP_MOTIVO)) or None,
        )
    except ValueError:
        raise ErroRegistroExecucao("registro de execução inválido") from None
    return RegistroExecucao(pagina_id, execucao, _ler_texto(props.get(_PROP_ID_EXTERNO)) or None)


def _ler_registros(paginas: list[dict[str, Any]]) -> list[RegistroExecucao]:
    achados: list[RegistroExecucao] = []
    for pagina in paginas:
        registro = _registro_de_pagina(pagina)
        if registro is not None:
            achados.append(registro)
    return achados


# --------------------------------------------------------------------------- escrita


def iniciar_execucao(
    cliente: ClienteNotion,
    ids: _TemBancoExecucoes,
    post_id: str,
    plataforma: str,
    chave_idempotencia: str,
    tentativa: int,
    agora: datetime,
) -> RegistroExecucao:
    """Grava a execução como ``iniciada`` ANTES de enviar à plataforma.

    Escrita não idempotente: ``ErroNotionIncerto`` (e qualquer ``ErroNotion``) propaga sem
    nova tentativa. Sem registro confirmado, o chamador NÃO deve publicar e deve tratar o
    caso como incerto (a página pode ter sido criada).
    """
    banco = _normalizar_id(ids.execucoes, "banco Execuções")
    post = _normalizar_id(post_id, "post_id")
    _padrao(plataforma, _RE_PLATAFORMA, "plataforma")
    _padrao(chave_idempotencia, _RE_CHAVE, "chave de idempotência")
    if tentativa not in _TENTATIVAS_VALIDAS:
        raise ValueError("tentativa deve ser 1 ou 2")
    _exigir_fuso(agora, "agora")
    propriedades: dict[str, Any] = {
        _PROP_CHAVE: {"title": _texto(chave_idempotencia)},
        _PROP_POST: {"relation": [{"id": post}]},
        _PROP_PLATAFORMA: {"select": {"name": plataforma}},
        _PROP_TENTATIVA: {"select": {"name": str(tentativa)}},
        _PROP_INICIO: _data(agora),
    }
    try:
        resposta = cliente.criar_pagina({"database_id": banco}, propriedades)
    except ErroNotionIncerto:
        _log.warning("execução %s: criação incerta; não publicar", chave_idempotencia)
        raise
    pagina_id = resposta.get("id")
    if not isinstance(pagina_id, str) or not pagina_id:
        raise ErroRegistroExecucao("resposta do Notion sem id do registro de execução")
    execucao = Execucao(
        chave_idempotencia=chave_idempotencia,
        post_id=post,
        plataforma=plataforma,
        estado=EstadoExecucao.INICIADA,
        tentativa=tentativa,
        iniciada_em=agora,
    )
    _log.info("execução iniciada: %s tentativa %d", chave_idempotencia, tentativa)
    return RegistroExecucao(pagina_id, execucao)


def registrar_resultado(
    cliente: ClienteNotion,
    registro: RegistroExecucao,
    resultado: EstadoExecucao,
    motivo: MotivoRecusa | CodigoMotivo | None,
    id_externo: str | None,
    agora: datetime,
) -> RegistroExecucao:
    """Atualiza a MESMA página com o resultado, o código do motivo e o fim.

    Só aceita registro ainda ``iniciada`` e resultado terminal. ``motivo`` é enum;
    ``id_externo`` segue padrão fechado (sem texto livre).
    """
    if resultado not in _NOME_RESULTADO:
        raise ValueError("resultado deve ser um estado terminal")
    if registro.execucao.estado is not EstadoExecucao.INICIADA:
        raise ValueError("só uma execução iniciada recebe resultado")
    codigo = _codigo(motivo)
    if id_externo is not None:
        _padrao(id_externo, _RE_ID_EXTERNO, "identificador externo")
    _exigir_fuso(agora, "agora")
    propriedades: dict[str, Any] = {
        _PROP_RESULTADO: {"select": {"name": _NOME_RESULTADO[resultado]}},
        _PROP_FIM: _data(agora),
        _PROP_MOTIVO: {"rich_text": _texto(codigo) if codigo else []},
    }
    if id_externo is not None:
        propriedades[_PROP_ID_EXTERNO] = {"rich_text": _texto(id_externo)}
    cliente.atualizar_pagina(registro.pagina_id, propriedades)
    execucao = replace(registro.execucao, estado=resultado, finalizada_em=agora, motivo=codigo)
    _log.info("execução %s: %s", registro.execucao.chave_idempotencia, resultado.value)
    return RegistroExecucao(registro.pagina_id, execucao, id_externo)


def sinal_de_vida(
    cliente: ClienteNotion,
    ids: _TemBancoExecucoes,
    agora: datetime,
    pagina_id: str | None = None,
) -> str:
    """Grava o sinal de vida do job e devolve o id da página do sinal.

    Sem ``pagina_id``: cria o registro próprio (Resultado "sinal de vida", sem Post).
    Com ``pagina_id``: atualiza SOMENTE a propriedade "Fim" (instante do último sinal).
    """
    _exigir_fuso(agora, "agora")
    if pagina_id is not None:
        cliente.atualizar_pagina(_normalizar_id(pagina_id, "pagina_id"), {_PROP_FIM: _data(agora)})
        return pagina_id
    banco = _normalizar_id(ids.execucoes, "banco Execuções")
    resposta = cliente.criar_pagina(
        {"database_id": banco},
        {
            _PROP_CHAVE: {"title": _texto(_CHAVE_SINAL)},
            _PROP_RESULTADO: {"select": {"name": _RESULTADO_SINAL}},
            _PROP_INICIO: _data(agora),
            _PROP_FIM: _data(agora),
        },
    )
    novo = resposta.get("id")
    if not isinstance(novo, str) or not novo:
        raise ErroRegistroExecucao("resposta do Notion sem id do sinal de vida")
    return novo


# --------------------------------------------------------------------------- leitura


def buscar_por_chave(
    cliente: ClienteNotion, ids: _TemBancoExecucoes, chave_idempotencia: str
) -> list[RegistroExecucao]:
    """Todas as execuções com a chave (todas as tentativas); leitura paginada completa.

    Linha ilegível levanta ``ErroRegistroExecucao`` (falha segura: a guarda nega).
    """
    _padrao(chave_idempotencia, _RE_CHAVE, "chave de idempotência")
    filtro = {"property": _PROP_CHAVE, "title": {"equals": chave_idempotencia}}
    paginas = cliente.consultar_banco(_normalizar_id(ids.execucoes, "banco Execuções"), filtro)
    return [
        r for r in _ler_registros(paginas) if r.execucao.chave_idempotencia == chave_idempotencia
    ]


def execucoes_do_post(
    cliente: ClienteNotion, ids: _TemBancoExecucoes, post_id: str, plataforma: str
) -> list[RegistroExecucao]:
    """Execuções anteriores do mesmo post e plataforma, qualquer chave/data (RF-38)."""
    post = _normalizar_id(post_id, "post_id")
    _padrao(plataforma, _RE_PLATAFORMA, "plataforma")
    filtro = {
        "and": [
            {"property": _PROP_POST, "relation": {"contains": post}},
            {"property": _PROP_PLATAFORMA, "select": {"equals": plataforma}},
        ]
    }
    paginas = cliente.consultar_banco(_normalizar_id(ids.execucoes, "banco Execuções"), filtro)
    return [
        r
        for r in _ler_registros(paginas)
        if r.execucao.post_id == post and r.execucao.plataforma == plataforma
    ]
