"""Vigia de selo e varredura dos posts autorizados (T024; RF-26, A-001, A-009, R2-12).

O usuário pode pôr um post no estágio "confirmado" direto no Notion, sem passar pela tela; o
post fica autorizado SEM selo. A cada execução o vigia percorre TODOS os posts com papel
``autorizado`` (paginação completa, inclusive os de data distante, não só os devidos) e decide:

=====================================  ==========================================================
Situação                               Desfecho
=====================================  ==========================================================
selo ausente, < intervalo sem edição   ``AGUARDANDO`` (nenhuma leitura de arte, nenhuma escrita)
selo ausente, >= intervalo             ``SELADO``: grava o selo (e "Confirmado em" se faltar)
selo ausente, sem data e hora          ``DEVOLVIDO_A_PRONTO`` (só depois do intervalo)
selo presente e válido                 ``MANTIDO`` (nenhuma escrita)
selo divergente/malformado/v. desc.    ``DEVOLVIDO_A_PRONTO`` (mesmo dentro do intervalo)
sem data e hora / sem plataformas      ``DEVOLVIDO_A_PRONTO``
selo impossível / arte inválida        ``DEVOLVIDO_A_PRONTO``
armazém ou Notion indisponível         ``INDETERMINADO``: não reverte, só registra
post de outro papel (defesa)           ``IGNORADO``: nunca é tocado
post ilegível, edição concorrente...   ``ERRO`` com motivo enumerado; segue para o próximo
=====================================  ==========================================================

Regras e decisões:

* **A-001**: antes de qualquer leitura de post a configuração é validada com
  ``exigir_estagio_autorizador`` (único autorizador, chamado "confirmado", sem problemas) e
  exige-se exatamente um estágio de papel ``pronto`` (destino da devolução). Se algo falhar,
  NADA é selado nem revertido: o relatório traz só ``falha``.
* **R2-12**: selo ausente nunca é revertido por estar ausente; espera o intervalo e então é
  selado. A única exceção é faltar data e hora (ou plataformas, ou o selo ser impossível de
  calcular, ou a arte ser inválida) depois do intervalo: o post não pode ser confirmado como
  está (a tela recusaria), então volta a ``pronto``. Isso é ser mais estrito, nunca menos (I-2).
* **Selagem**: não usa ``confirmar_post`` (que recusa ``POST_JA_CONFIRMADO``). Grava num ÚNICO
  PATCH apenas "Selo de confirmação" e, se estiver vazio, "Confirmado em" = instante do
  relógio no início da varredura (o momento em que a confirmação passou a valer; o Notion não
  diz quando o estágio mudou). O estágio NÃO entra no PATCH, para nunca desfazer uma
  desconfirmação feita em paralelo. Antes do PATCH há a mesma conferência de edição concorrente
  da confirmação (``last_edited_time`` igual ao lido); depois, o post é relido e o selo
  conferido. Se o post saiu de ``autorizado`` nesse meio-tempo, o selo residual é limpo
  (``sair_de_autorizado`` para o estágio atual) e a ocorrência é ``EDICAO_CONCORRENTE``; se o
  selo relido não confere, o post volta a ``pronto`` (``SELO_NAO_CONFERE_APOS_GRAVAR``).
* **Relógio**: ``agora`` é chamado UMA vez por varredura. Usar o início da varredura subestima
  o tempo sem edição (lado seguro) e dá o mesmo "Confirmado em" a todos os selados da rodada.
* **INDETERMINADO**: indisponibilidade (armazém ou Notion transitório) não prova divergência;
  reverter seria agir sem informação. A guarda de publicação recalcula o selo e recusa de
  qualquer forma; a próxima varredura decide.
* **Idempotência**: um post selado tem selo válido e passa a ``MANTIDO``; um devolvido sai do
  filtro. Uma segunda varredura sem mudanças não escreve nada.
* **A-009 (recusa registrada)**: cada devolução vai no relatório (``RelatorioVigia.recusas``) e
  no log, só com id do post e código do motivo. A gravação no banco Execuções fica para a T037.
* **I-1**: logs, exceções e relatório têm apenas ids, desfechos e códigos; nunca título, corpo,
  artes, selo ou resposta bruta do Notion.

Limites conhecidos: ``last_edited_time`` do Notion é arredondado ao minuto, então o "tempo sem
edição" real pode ser até ~1 minuto menor que o intervalo, e a conferência de edição não vê
mudanças no mesmo minuto (por isso a releitura com recálculo do selo). A API não tem escrita
condicional: entre a conferência e o PATCH resta uma janela pequena, coberta pela releitura.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Final

from publicador.artes.armazem import (
    ArmazemDeArtes,
    ArteNaoEncontrada,
    CaminhoDeArteInvalido,
    ErroAcessoArmazem,
)
from publicador.core.confirmacao import (
    ConfirmacaoRecusada,
    MotivoConfirmacao,
    exigir_estagio_autorizador,
    sair_de_autorizado,
    validar_post_confirmavel,
)
from publicador.core.modelos import Estagio, PapelEstagio, Post
from publicador.core.redacao import obter_logger
from publicador.core.selo import SeloImpossivel, calcular_selo, verificar_selo
from publicador.notion.bancos import Configuracao
from publicador.notion.cliente import ClienteNotion, ErroNotion, ErroNotionTransitorio
from publicador.notion.escrita import EdicaoConcorrente
from publicador.notion.mapeamento import PostInvalido, ler_post, post_de_pagina
from publicador.notion.variacoes import VariacaoInvalida

__all__ = [
    "INTERVALO_SEM_EDICAO_PADRAO",
    "Desfecho",
    "MotivoVigia",
    "Ocorrencia",
    "RelatorioVigia",
    "varrer",
]

INTERVALO_SEM_EDICAO_PADRAO: Final = timedelta(minutes=5)

_log = obter_logger("publicador.job.vigia")

_PROP_ESTAGIO: Final = "Estágio"
_PROP_SELO: Final = "Selo de confirmação"
_PROP_CONFIRMADO: Final = "Confirmado em"
_RE_ID: Final = re.compile(
    r"[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_ID_DESCONHECIDO: Final = "(id inválido)"


class Desfecho(Enum):
    SELADO = "selado"
    AGUARDANDO = "aguardando"
    MANTIDO = "mantido"
    DEVOLVIDO_A_PRONTO = "devolvido_a_pronto"
    INDETERMINADO = "indeterminado"
    IGNORADO = "ignorado"
    ERRO = "erro"


class MotivoVigia(Enum):
    SELO_EMITIDO = "selo_emitido"
    DENTRO_DO_INTERVALO = "dentro_do_intervalo"
    SELO_VALIDO = "selo_valido"
    SELO_DIVERGENTE = "selo_divergente"
    SEM_DATA = "sem_data"
    SEM_HORA = "sem_hora"
    SEM_PLATAFORMAS = "sem_plataformas"
    SELO_IMPOSSIVEL = "selo_impossivel"
    ARTE_INVALIDA = "arte_invalida"
    SELO_NAO_CONFERE_APOS_GRAVAR = "selo_nao_confere_apos_gravar"
    ARMAZEM_INDISPONIVEL = "armazem_indisponivel"
    NOTION_INDISPONIVEL = "notion_indisponivel"
    NOTION_ERRO = "notion_erro"
    POST_ILEGIVEL = "post_ilegivel"
    SEM_VERSAO_LIDA = "sem_versao_lida"
    EDICAO_CONCORRENTE = "edicao_concorrente"
    RELEITURA_DIVERGENTE = "releitura_divergente"
    NAO_AUTORIZADO = "nao_autorizado"
    CONFIG_ESTAGIOS_INVALIDA = "config_estagios_invalida"
    CONFIG_SEM_PRONTO = "config_sem_pronto"
    ERRO_INESPERADO = "erro_inesperado"


@dataclass(frozen=True)
class Ocorrencia:
    """O que o vigia fez com um post. Só id e códigos (sem conteúdo, I-1)."""

    post_id: str
    desfecho: Desfecho
    motivo: MotivoVigia


@dataclass(frozen=True)
class RelatorioVigia:
    """Resultado de uma varredura. ``falha`` preenchida = nada foi lido nem escrito."""

    ocorrencias: tuple[Ocorrencia, ...] = ()
    falha: MotivoVigia | None = None

    def _ids(self, desfecho: Desfecho) -> tuple[str, ...]:
        return tuple(o.post_id for o in self.ocorrencias if o.desfecho is desfecho)

    @property
    def selados(self) -> tuple[str, ...]:
        return self._ids(Desfecho.SELADO)

    @property
    def aguardando(self) -> tuple[str, ...]:
        return self._ids(Desfecho.AGUARDANDO)

    @property
    def mantidos(self) -> tuple[str, ...]:
        return self._ids(Desfecho.MANTIDO)

    @property
    def devolvidos_a_pronto(self) -> tuple[str, ...]:
        return self._ids(Desfecho.DEVOLVIDO_A_PRONTO)

    @property
    def indeterminados(self) -> tuple[str, ...]:
        return self._ids(Desfecho.INDETERMINADO)

    @property
    def ignorados(self) -> tuple[str, ...]:
        return self._ids(Desfecho.IGNORADO)

    @property
    def erros(self) -> tuple[str, ...]:
        return self._ids(Desfecho.ERRO)

    @property
    def recusas(self) -> tuple[Ocorrencia, ...]:
        """Recusas registradas (A-009): posts devolvidos a ``pronto`` e o motivo."""
        return tuple(o for o in self.ocorrencias if o.desfecho is Desfecho.DEVOLVIDO_A_PRONTO)

    def ocorrencia(self, post_id: str) -> Ocorrencia:
        for o in self.ocorrencias:
            if o.post_id == post_id:
                return o
        raise KeyError("post não consta do relatório")


# --------------------------------------------------------------------------- auxiliares


def _exigir_fuso(valor: datetime, rotulo: str) -> None:
    if not isinstance(valor, datetime) or valor.tzinfo is None or valor.utcoffset() is None:
        raise ValueError(f"{rotulo} deve ter fuso horário (datetime ingênuo é rejeitado)")


def _id_seguro(pagina: Mapping[str, Any]) -> str:
    bruto = pagina.get("id")
    if isinstance(bruto, str) and _RE_ID.fullmatch(bruto) is not None:
        return bruto.replace("-", "").lower()
    return _ID_DESCONHECIDO


def _estagio_pronto(config: Configuracao) -> Estagio | None:
    prontos = [e for e in config.estagios.values() if e.papel is PapelEstagio.PRONTO]
    return prontos[0] if len(prontos) == 1 else None


def _motivo_inconfirmavel(post: Post) -> MotivoVigia | None:
    """Motivo pelo qual o post não pode estar autorizado (I-2), ou ``None``."""
    try:
        validar_post_confirmavel(post)
    except ConfirmacaoRecusada as erro:
        return {
            MotivoConfirmacao.SEM_DATA: MotivoVigia.SEM_DATA,
            MotivoConfirmacao.SEM_HORA: MotivoVigia.SEM_HORA,
            MotivoConfirmacao.SEM_PLATAFORMAS: MotivoVigia.SEM_PLATAFORMAS,
        }.get(erro.motivo, MotivoVigia.SELO_IMPOSSIVEL)
    return None


def _conferir_edicao(cliente: ClienteNotion, post_id: str, esperado: datetime) -> None:
    """Recusa (``EdicaoConcorrente``) se o post mudou desde a leitura (mesma regra de RF-42)."""
    bruto = cliente.obter_pagina(post_id).get("last_edited_time")
    atual: datetime | None = None
    if isinstance(bruto, str):
        try:
            atual = datetime.fromisoformat(bruto)
        except ValueError:
            atual = None
    if atual is None or atual.tzinfo is None:
        raise EdicaoConcorrente("data da última edição do post ilegível; nada foi gravado")
    if atual != esperado:
        raise EdicaoConcorrente("o post foi editado durante a varredura; nada foi gravado")


def _registrar(ocorrencia: Ocorrencia) -> Ocorrencia:
    desfecho, motivo, pid = ocorrencia.desfecho, ocorrencia.motivo.value, ocorrencia.post_id
    if desfecho is Desfecho.DEVOLVIDO_A_PRONTO:
        _log.warning(
            "vigia: recusa registrada; post %s devolvido a pronto; motivo: %s", pid, motivo
        )
    elif desfecho in (Desfecho.ERRO, Desfecho.INDETERMINADO):
        _log.warning("vigia: post %s %s; motivo: %s", pid, desfecho.value, motivo)
    elif desfecho is Desfecho.SELADO:
        _log.info("vigia: post %s selado; campos: selo[,confirmado_em]", pid)
    else:
        _log.debug("vigia: post %s %s; motivo: %s", pid, desfecho.value, motivo)
    return ocorrencia


# --------------------------------------------------------------------------- varredura


class _Varredura:
    def __init__(
        self,
        cliente: ClienteNotion,
        config: Configuracao,
        armazem: ArmazemDeArtes,
        instante: datetime,
        intervalo: timedelta,
        pronto: Estagio,
    ) -> None:
        self._cliente = cliente
        self._config = config
        self._armazem = armazem
        self._instante = instante
        self._intervalo = intervalo
        self._pronto = pronto

    def processar(self, pagina: Mapping[str, Any]) -> Ocorrencia:
        """Trata um post isolando qualquer falha: erro aqui nunca interrompe a varredura."""
        pid = _id_seguro(pagina)
        try:
            return self._processar(pid, pagina)
        except EdicaoConcorrente:
            return Ocorrencia(pid, Desfecho.ERRO, MotivoVigia.EDICAO_CONCORRENTE)
        except ErroAcessoArmazem:
            return Ocorrencia(pid, Desfecho.INDETERMINADO, MotivoVigia.ARMAZEM_INDISPONIVEL)
        except ErroNotionTransitorio:
            return Ocorrencia(pid, Desfecho.INDETERMINADO, MotivoVigia.NOTION_INDISPONIVEL)
        except ErroNotion:
            return Ocorrencia(pid, Desfecho.ERRO, MotivoVigia.NOTION_ERRO)
        except PostInvalido:
            return Ocorrencia(pid, Desfecho.ERRO, MotivoVigia.POST_ILEGIVEL)
        except ConfirmacaoRecusada as erro:
            motivo = {
                MotivoConfirmacao.SEM_VERSAO_LIDA: MotivoVigia.SEM_VERSAO_LIDA,
                MotivoConfirmacao.DESTINO_INVALIDO: MotivoVigia.CONFIG_SEM_PRONTO,
            }.get(erro.motivo, MotivoVigia.RELEITURA_DIVERGENTE)
            return Ocorrencia(pid, Desfecho.ERRO, motivo)
        except Exception as erro:  # noqa: BLE001 - isolamento por post; só o tipo vai ao log
            _log.error("vigia: erro inesperado no post %s (%s)", pid, type(erro).__name__)
            return Ocorrencia(pid, Desfecho.ERRO, MotivoVigia.ERRO_INESPERADO)

    def _processar(self, pid: str, pagina: Mapping[str, Any]) -> Ocorrencia:
        leve = post_de_pagina(pagina, self._config)  # sem blocos: só para decidir
        if leve.estagio.papel is not PapelEstagio.AUTORIZADO:
            return Ocorrencia(pid, Desfecho.IGNORADO, MotivoVigia.NAO_AUTORIZADO)
        if leve.selo is None:
            if leve.editado_em is None:
                return Ocorrencia(pid, Desfecho.ERRO, MotivoVigia.SEM_VERSAO_LIDA)
            if self._instante - leve.editado_em < self._intervalo:
                return Ocorrencia(pid, Desfecho.AGUARDANDO, MotivoVigia.DENTRO_DO_INTERVALO)

        post = post_de_pagina(pagina, self._config, self._cliente.blocos_filhos(leve.id))
        inconfirmavel = _motivo_inconfirmavel(post)
        if inconfirmavel is not None:
            return self._devolver(post, inconfirmavel)
        if post.selo is None:
            return self._selar(post)
        try:
            valido = verificar_selo(post, self._armazem, post.selo)
        except SeloImpossivel:
            return self._devolver(post, MotivoVigia.SELO_IMPOSSIVEL)
        except (ArteNaoEncontrada, CaminhoDeArteInvalido, VariacaoInvalida):
            return self._devolver(post, MotivoVigia.ARTE_INVALIDA)
        if valido:
            return Ocorrencia(post.id, Desfecho.MANTIDO, MotivoVigia.SELO_VALIDO)
        return self._devolver(post, MotivoVigia.SELO_DIVERGENTE)

    def _devolver(self, post: Post, motivo: MotivoVigia) -> Ocorrencia:
        """Devolve a ``pronto`` limpando selo e "Confirmado em" num único PATCH."""
        sair_de_autorizado(self._cliente, post, self._config, self._pronto)
        return Ocorrencia(post.id, Desfecho.DEVOLVIDO_A_PRONTO, motivo)

    def _selar(self, post: Post) -> Ocorrencia:
        try:
            selo = calcular_selo(post, self._armazem)
        except SeloImpossivel:
            return self._devolver(post, MotivoVigia.SELO_IMPOSSIVEL)
        except (ArteNaoEncontrada, CaminhoDeArteInvalido, VariacaoInvalida):
            return self._devolver(post, MotivoVigia.ARTE_INVALIDA)
        # ErroAcessoArmazem propaga: INDETERMINADO, sem escrita.

        if post.editado_em is None:  # já tratado em _processar; defesa
            return Ocorrencia(post.id, Desfecho.ERRO, MotivoVigia.SEM_VERSAO_LIDA)
        propriedades: dict[str, Any] = {
            _PROP_SELO: {"rich_text": [{"type": "text", "text": {"content": selo}}]}
        }
        if post.confirmado_em is None:
            propriedades[_PROP_CONFIRMADO] = {"date": {"start": self._instante.isoformat()}}

        _conferir_edicao(self._cliente, post.id, post.editado_em)
        self._cliente.atualizar_pagina(post.id, propriedades)

        novo = ler_post(self._cliente, post.id, self._config)
        if novo.estagio.papel is not PapelEstagio.AUTORIZADO:
            # Saiu de autorizado em paralelo: não deixa selo residual (nem volta a confirmado).
            if novo.selo is not None or novo.confirmado_em is not None:
                sair_de_autorizado(self._cliente, novo, self._config, novo.estagio)
            return Ocorrencia(post.id, Desfecho.ERRO, MotivoVigia.EDICAO_CONCORRENTE)
        if novo.selo != selo or not self._confere(novo):
            return self._devolver(novo, MotivoVigia.SELO_NAO_CONFERE_APOS_GRAVAR)
        return Ocorrencia(post.id, Desfecho.SELADO, MotivoVigia.SELO_EMITIDO)

    def _confere(self, post: Post) -> bool:
        try:
            return verificar_selo(post, self._armazem, post.selo)
        except (SeloImpossivel, ArteNaoEncontrada, CaminhoDeArteInvalido, VariacaoInvalida):
            return False


def varrer(
    cliente: ClienteNotion,
    config: Configuracao,
    armazem: ArmazemDeArtes,
    agora: Callable[[], datetime],
    *,
    banco_posts_id: str,
    intervalo_sem_edicao: timedelta = INTERVALO_SEM_EDICAO_PADRAO,
) -> RelatorioVigia:
    """Varre TODOS os posts ``autorizado``: sela os confirmados direto no Notion após o
    intervalo sem edição e devolve a ``pronto`` os que não podem continuar autorizados.

    ``agora`` é o relógio (injetável), chamado uma vez; deve devolver datetime com fuso
    (``ValueError`` caso contrário, antes de qualquer acesso). Falhas de configuração ou da
    listagem vão em ``RelatorioVigia.falha``; falhas de um post viram a ocorrência dele.
    """
    if intervalo_sem_edicao <= timedelta(0):
        raise ValueError("o intervalo sem edição deve ser positivo")
    instante = agora()
    _exigir_fuso(instante, "agora")

    try:
        autorizador_id, _ = exigir_estagio_autorizador(config)
    except ConfirmacaoRecusada:
        _log.error("vigia: configuração de estágios inválida; nada foi selado nem revertido")
        return RelatorioVigia(falha=MotivoVigia.CONFIG_ESTAGIOS_INVALIDA)
    pronto = _estagio_pronto(config)
    if pronto is None:
        _log.error("vigia: a configuração deve ter exatamente um estágio pronto; nada foi feito")
        return RelatorioVigia(falha=MotivoVigia.CONFIG_SEM_PRONTO)

    filtro = {"property": _PROP_ESTAGIO, "relation": {"contains": autorizador_id}}
    try:
        paginas = cliente.consultar_banco(banco_posts_id, filtro)
    except ErroNotionTransitorio:
        _log.warning("vigia: Notion indisponível ao listar os posts autorizados")
        return RelatorioVigia(falha=MotivoVigia.NOTION_INDISPONIVEL)
    except ErroNotion:
        _log.error("vigia: o Notion recusou a listagem dos posts autorizados")
        return RelatorioVigia(falha=MotivoVigia.NOTION_ERRO)

    varredura = _Varredura(cliente, config, armazem, instante, intervalo_sem_edicao, pronto)
    vistos: set[str] = set()
    ocorrencias: list[Ocorrencia] = []
    for pagina in paginas:
        pid = _id_seguro(pagina)
        if pid != _ID_DESCONHECIDO and pid in vistos:
            continue  # paginação devolveu a mesma página duas vezes
        vistos.add(pid)
        ocorrencias.append(_registrar(varredura.processar(pagina)))

    relatorio = RelatorioVigia(tuple(ocorrencias))
    _log.info(
        "vigia: %d autorizados; selados %d, aguardando %d, mantidos %d, devolvidos %d, "
        "indeterminados %d, ignorados %d, erros %d",
        len(ocorrencias),
        len(relatorio.selados),
        len(relatorio.aguardando),
        len(relatorio.mantidos),
        len(relatorio.devolvidos_a_pronto),
        len(relatorio.indeterminados),
        len(relatorio.ignorados),
        len(relatorio.erros),
    )
    return relatorio
