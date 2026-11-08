"""Orquestrador do publicador (T038; RF-10 a RF-13, RF-17, RF-21, RF-37, RF-40, A-010, A-021).

Uma chamada de ``executar`` é uma rodada do job periódico (GitHub Actions):

1. Valida a configuração de estágios (``exigir_estagio_autorizador`` e um único ``pronto``) e
   os ids dos bancos Posts e Execuções. Inválida: NADA é lido, publicado nem registrado por
   post; o relatório traz ``falha`` (o sinal de vida ainda é emitido).
2. Lista no Notion TODOS os posts no estágio autorizador (paginação completa, mesmo filtro do
   vigia) e fica com os DEVIDOS: ``publicar_em <= agora`` (instantes absolutos). Atrasados além
   da janela também entram, para a guarda recusá-los (``FORA_DA_JANELA``) e a recusa ficar
   registrada. Sem data, só com o dia ou futuros não são devidos (o vigia cuida dos primeiros).
   Ordem determinística: ``publicar_em`` e depois o id; plataformas em ordem alfabética.
3. Para CADA post devido e CADA plataforma dele: relê o post no Notion (a lista nunca é
   confiável; erro de leitura = guarda com ``post=None`` = ``LEITURA_FALHOU``); chama
   ``reverificar_post(..., tratar_selo_ausente="ignorar")`` (``INDETERMINADO``: não publica
   nem desconfirma; ``DEVOLVIDO_A_PRONTO``: registra a recusa e segue); resolve o mundo
   (execuções anteriores de qualquer data, posts posteriores que já saíram NA plataforma,
   posts da série, conexão e validação de mídia pelo adaptador, plataformas com adaptador,
   tipos por formato) e chama ``avaliar``. Falha ao resolver o mundo = contexto com
   ``post=None`` = recusa segura daquele post e plataforma, sem afetar os demais.
4. Recusa: registra em Execuções (resultado "recusado" + ``MotivoRecusa``). NÃO muda o estágio
   (decisão do plano). Plataforma sem adaptador (ex.: Substack, RF-37) é só mais uma recusa:
   as outras plataformas do mesmo post e os outros posts seguem.
5. Permitido: grava ``iniciada`` (chave = ``chave_de(post_id, plataforma, selo)``) ANTES de
   enviar. Se essa escrita falhar (inclusive ``ErroNotionIncerto``) NÃO publica.
6. Publica com ``PedidoDePublicacao(..., simular=not publicar_real)`` e registra o resultado
   na MESMA página de execução.
7. Emite o sinal de vida e devolve ``RelatorioExecucao`` (só ids, plataformas e códigos).

Ordem iniciada -> publicar -> registrar. Se registrar o resultado falhar DEPOIS de um envio
real, nada é reenviado: a execução fica ``iniciada`` no Notion, o que a guarda trata como
bloqueante (``EXECUCAO_EXISTENTE``) nas rodadas seguintes, e o adaptador é idempotente pela
chave. O erro aparece no relatório (``Registro.FALHOU``, ``erros_de_registro``) e no log.
Exceção do adaptador durante ``publicar`` em modo real vira ``incerto`` (pode ter saído; RF-38).

Modo real (RF-40, A-019): ``executar`` recebe ``publicar_real`` explícito. O ponto de entrada
do job deve passar ``modo_real_ligado()``, que só devolve ``True`` com ``PUBLICAR_REAL``
exatamente igual a "1"; ausente, vazia, "true", "yes", " 1" etc. = SIMULAÇÃO. Em simulação o
resultado é registrado como "simulado" e nada muda no estágio do post.

Decisões (mais restritivas que a guarda, nunca menos; I-2):

* **Falha anterior** (``FALHA``) do mesmo post e plataforma: não publica nesta tarefa
  (``AGUARDANDO_NOVA_TENTATIVA``). A nova tentativa segura (consultar a plataforma, intervalo
  de 30 min, no máximo uma) é da T040; a guarda não bloqueia ``FALHA`` de propósito.
* **Simulação repetida**: em simulação, se já existe execução ``simulado`` com a MESMA chave,
  não simula nem registra de novo (evita um registro a cada 5 minutos durante a janela).
* **Recusa repetida**: se já existe recusa com a mesma chave e o mesmo código, não registra
  outra (``Registro.JA_EXISTIA``).
* **Ordem por plataforma (A-010)**: "já saiu" = execução ``sucesso``, ``sucesso em modo
  privado``, ``incerto`` ou ``iniciada`` (pode ter saído) de um post agendado entre este e
  agora. Uma página do banco Posts com data ilegível impede provar a ordem: recusa.
* **Conexão (RF-18)**: ``EstadoConexao.dias_restantes`` vira ``expira_em = agora + dias``
  (limite inferior verdadeiro). Validade desconhecida (``None``) ou 0 dia: a guarda recusa.
* **Tipos por formato (RF-43, A-023)**: por padrão vêm de ``config.tipos_por_formato`` (lido
  do banco Formatos por ``ler_configuracao``); o parâmetro explícito ``tipos_por_formato`` tem
  prioridade, inclusive vazio. Sem mapeamento = tudo recusado (``FORMATO_SEM_TIPO``).
* Recusas usam tentativa 1 e, sem selo, a chave ``chave_de(post, plataforma, "sem-selo")``.

Pós-publicação (T039, ``job/pos_publicacao.py``): em modo real, depois de processar TODAS as
plataformas de um post, ``concluir_post`` lê as execuções do post no Notion e move o estágio
para ``publicado`` ou ``falha`` num PATCH único (nunca para ``autorizado``; recusa e
simulação não mudam o estágio). Erro ali não derruba a rodada nem desfaz execuções; o
desfecho vai em ``Ocorrencia.pos_publicacao`` e ``RelatorioExecucao.pos_publicacao``.
Gancho T040 (nova tentativa) entra no ponto ``AGUARDANDO_NOVA_TENTATIVA``.

I-1: logs, registros, exceções e relatório têm só ids, plataforma, desfecho e códigos; nunca
título, corpo, artes, selo, token, mensagem de exceção ou resposta bruta. Exceções inesperadas
vão ao log só pelo nome do tipo. Requisições ao Notion respeitam o limite de ~3 req/s do
``ClienteNotion``. Relógio sempre injetado (nunca ``datetime.now``).

Limites: entre a guarda e o envio ainda há janela (TOCTOU), coberta pela releitura e pelo
recálculo do selo a cada post e plataforma. Recusas e "não executado" são gravados em UMA
única escrita (``registrar_recusa``, T039): não existe mais execução ``iniciada`` órfã por
falha na segunda escrita.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any, Final

from publicador.adaptadores.base import (
    PedidoDePublicacao,
    PlatformAdapter,
    ResultadoPublicacao,
    TipoDeResultado,
)
from publicador.artes.armazem import ArmazemDeArtes
from publicador.core.config import Parametros
from publicador.core.confirmacao import ConfirmacaoRecusada, exigir_estagio_autorizador
from publicador.core.guarda import ContextoGuarda, PublicacaoConhecida, avaliar
from publicador.core.modelos import (
    Conexao,
    EstadoExecucao,
    Execucao,
    MotivoRecusa,
    Post,
)
from publicador.core.redacao import obter_logger
from publicador.core.reverificacao import (
    ConfiguracaoSemProntoUnico,
    MotivoReverificacao,
    SituacaoReverificacao,
    exigir_estagio_pronto,
    reverificar_post,
)
from publicador.job.execucoes import (
    CodigoMotivo,
    RegistroExecucao,
    chave_de,
    execucoes_do_post,
    iniciar_execucao,
    registrar_recusa,
    registrar_resultado,
    sinal_de_vida,
)
from publicador.job.pos_publicacao import DesfechoPos, ResultadoPos, concluir_post
from publicador.notion.bancos import Configuracao, IdsBancos
from publicador.notion.cliente import ClienteNotion
from publicador.notion.mapeamento import (
    PostInvalido,
    data_de_propriedade,
    ler_post,
    post_de_pagina,
)
from publicador.notion.variacoes import artes_por_plataforma

__all__ = [
    "VARIAVEL_MODO_REAL",
    "Desfecho",
    "FalhaGeral",
    "Ocorrencia",
    "Registro",
    "RelatorioExecucao",
    "executar",
    "modo_real_ligado",
]

_log = obter_logger("publicador.job.orquestrador")

VARIAVEL_MODO_REAL: Final = "PUBLICAR_REAL"
_VALOR_LIGADO: Final = "1"
_TENTATIVA: Final = 1
_SEM_SELO: Final = "sem-selo"
_PLATAFORMA_DESCONHECIDA: Final = "?"
_PROP_ESTAGIO: Final = "Estágio"
_PROP_SERIE: Final = "Série"
_PROP_DATA: Final = "Data e hora"
_RE_ID: Final = re.compile(
    r"[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
# Mesmo padrão fechado de ``execucoes.py`` (lá é privado): id externo fora dele não é gravado.
_RE_ID_EXTERNO: Final = re.compile(r"[A-Za-z0-9_.:\-]{1,128}")

# Execuções de um post POSTERIOR que contam como "já saiu" na plataforma (A-010, lado seguro).
_JA_SAIU: Final = frozenset(
    {
        EstadoExecucao.SUCESSO,
        EstadoExecucao.SUCESSO_MODO_PRIVADO,
        EstadoExecucao.INCERTO,
        EstadoExecucao.INICIADA,
    }
)


def modo_real_ligado(ambiente: Mapping[str, str] | None = None) -> bool:
    """``True`` só se ``PUBLICAR_REAL`` vale exatamente "1" (RF-40). Qualquer outro valor,
    inclusive ausente, vazio, "true" ou com espaços, mantém a SIMULAÇÃO (padrão seguro)."""
    origem = os.environ if ambiente is None else ambiente
    return origem.get(VARIAVEL_MODO_REAL) == _VALOR_LIGADO


# --------------------------------------------------------------------------- relatório


class Desfecho(Enum):
    SIMULADO = "simulado"
    SIMULACAO_REPETIDA = "simulacao_repetida"
    SUCESSO = "sucesso"
    SUCESSO_MODO_PRIVADO = "sucesso_modo_privado"
    FALHA_TRANSITORIA = "falha_transitoria"
    FALHA_PERMANENTE = "falha_permanente"
    INCERTO = "incerto"
    RECUSADO = "recusado"
    DEVOLVIDO_A_PRONTO = "devolvido_a_pronto"
    INDETERMINADO = "indeterminado"
    AGUARDANDO_NOVA_TENTATIVA = "aguardando_nova_tentativa"
    NAO_INICIADO = "nao_iniciado"
    ERRO = "erro"


class Registro(Enum):
    """O que aconteceu com o registro da ocorrência no banco Execuções."""

    GRAVADO = "gravado"
    JA_EXISTIA = "ja_existia"
    FALHOU = "falhou"
    NAO_SE_APLICA = "nao_se_aplica"


class FalhaGeral(Enum):
    """Motivo de uma rodada que não processou nenhum post (nada publicado)."""

    IDS_INCOMPLETOS = "ids_incompletos"
    AGORA_SEM_FUSO = "agora_sem_fuso"
    CONFIG_ESTAGIOS_INVALIDA = "config_estagios_invalida"
    CONFIG_SEM_PRONTO = "config_sem_pronto"
    NOTION_INDISPONIVEL = "notion_indisponivel"


@dataclass(frozen=True)
class Ocorrencia:
    """Desfecho de um post numa plataforma. Só ids e códigos (I-1)."""

    post_id: str
    plataforma: str
    desfecho: Desfecho
    motivo: str | None = None
    registro: Registro = Registro.NAO_SE_APLICA
    # Resultado devolvido pelo adaptador (gancho da T039); ``None`` se não houve envio.
    resultado: TipoDeResultado | None = None
    id_externo: str | None = None
    pagina_execucao: str | None = None
    # Desfecho da pós-publicação do POST (T039), repetido em cada plataforma dele; ``None`` em
    # simulação ou quando o post não chegou a ser concluído nesta rodada.
    pos_publicacao: DesfechoPos | None = None


@dataclass(frozen=True)
class RelatorioExecucao:
    """Resultado de uma rodada. ``falha`` preenchida = nenhum post foi processado."""

    modo_real: bool
    ocorrencias: tuple[Ocorrencia, ...] = ()
    falha: FalhaGeral | None = None
    sinal_de_vida: str | None = None
    # Um item por post processado em modo real (T039); vazio em simulação.
    pos_publicacao: tuple[ResultadoPos, ...] = ()

    def por_desfecho(self, desfecho: Desfecho) -> tuple[Ocorrencia, ...]:
        return tuple(o for o in self.ocorrencias if o.desfecho is desfecho)

    def ocorrencia(self, post_id: str, plataforma: str) -> Ocorrencia:
        for o in self.ocorrencias:
            if o.post_id == post_id and o.plataforma == plataforma:
                return o
        raise KeyError("post e plataforma não constam do relatório")

    def contagens(self) -> dict[str, int]:
        return {d.value: len(self.por_desfecho(d)) for d in Desfecho}

    @property
    def erros_de_registro(self) -> tuple[Ocorrencia, ...]:
        return tuple(o for o in self.ocorrencias if o.registro is Registro.FALHOU)


# --------------------------------------------------------------------------- auxiliares


def _tem_fuso(valor: object) -> bool:
    return (
        isinstance(valor, datetime) and valor.tzinfo is not None and valor.utcoffset() is not None
    )


def _utc(valor: datetime) -> datetime:
    return valor.astimezone(UTC)


def _nome_erro(erro: BaseException) -> str:
    return type(erro).__name__


def _id_seguro(pagina: Mapping[str, Any]) -> str | None:
    bruto = pagina.get("id")
    if isinstance(bruto, str) and _RE_ID.fullmatch(bruto) is not None:
        return bruto.replace("-", "").lower()
    return None


def _ids_de_relacao(pagina: Mapping[str, Any], nome: str) -> tuple[str, ...] | None:
    """Ids de uma relação crua; ``None`` se ilegível (lado seguro para quem chama)."""
    props = pagina.get("properties")
    prop = props.get(nome) if isinstance(props, Mapping) else None
    if prop is None:
        return ()
    itens = prop.get("relation") if isinstance(prop, Mapping) else None
    if not isinstance(itens, list):
        return None
    ids: list[str] = []
    for item in itens:
        bruto = item.get("id") if isinstance(item, Mapping) else None
        if not isinstance(bruto, str) or _RE_ID.fullmatch(bruto) is None:
            return None
        ids.append(bruto.replace("-", "").lower())
    return tuple(ids)


def _motivo_da_devolucao(motivo: MotivoReverificacao | None, post: Post) -> MotivoRecusa:
    if motivo is MotivoReverificacao.SELO_AUSENTE:
        return MotivoRecusa.SELO_AUSENTE
    if motivo is MotivoReverificacao.SEM_DATA_HORA:
        return MotivoRecusa.SEM_HORA if post.somente_data else MotivoRecusa.SEM_DATA
    if motivo is MotivoReverificacao.ARTE_INVALIDA:
        return MotivoRecusa.ARTE_INVALIDA
    return MotivoRecusa.SELO_DIVERGENTE  # divergente, malformado, impossível


class _MundoIlegivel(Exception):  # noqa: N818 - uso interno
    """Não foi possível resolver o mundo para a guarda com segurança."""


@dataclass(frozen=True)
class _Listado:
    """Um post do banco Posts para as regras de ordem e série (sem conteúdo usado)."""

    id: str
    publicar_em: datetime | None
    data_legivel: bool
    post: Post | None  # ``None``: página ilegível como post
    serie_ids: tuple[str, ...] | None  # relação crua (só usada quando ``post`` é None)


@dataclass(frozen=True)
class _Devido:
    instante: datetime
    post_id: str
    plataformas: tuple[str, ...]


@dataclass
class _Andamento:
    """Estado de um post/plataforma: depois de ``iniciada``, nada mais é registrado por fora."""

    iniciada: RegistroExecucao | None = None
    chave: str | None = None


_SEM_MOTIVO_SIMULADO: Final[Mapping[TipoDeResultado, CodigoMotivo]] = {
    TipoDeResultado.SUCESSO: CodigoMotivo.SIMULACAO,
    TipoDeResultado.SUCESSO_MODO_PRIVADO: CodigoMotivo.SIMULACAO,
    TipoDeResultado.FALHA_TRANSITORIA: CodigoMotivo.ERRO_TRANSITORIO,
    TipoDeResultado.FALHA_PERMANENTE: CodigoMotivo.ERRO_PERMANENTE,
    TipoDeResultado.INCERTO: CodigoMotivo.PLATAFORMA_NAO_CONFIRMOU,
}
_REAL: Final[Mapping[TipoDeResultado, tuple[Desfecho, EstadoExecucao, CodigoMotivo | None]]] = {
    TipoDeResultado.SUCESSO: (Desfecho.SUCESSO, EstadoExecucao.SUCESSO, None),
    TipoDeResultado.SUCESSO_MODO_PRIVADO: (
        Desfecho.SUCESSO_MODO_PRIVADO,
        EstadoExecucao.SUCESSO_MODO_PRIVADO,
        CodigoMotivo.MODO_PRIVADO,
    ),
    TipoDeResultado.FALHA_TRANSITORIA: (
        Desfecho.FALHA_TRANSITORIA,
        EstadoExecucao.FALHA,
        CodigoMotivo.ERRO_TRANSITORIO,
    ),
    TipoDeResultado.FALHA_PERMANENTE: (
        Desfecho.FALHA_PERMANENTE,
        EstadoExecucao.FALHA,
        CodigoMotivo.ERRO_PERMANENTE,
    ),
    TipoDeResultado.INCERTO: (
        Desfecho.INCERTO,
        EstadoExecucao.INCERTO,
        CodigoMotivo.PLATAFORMA_NAO_CONFIRMOU,
    ),
}


# --------------------------------------------------------------------------- rodada


class _Rodada:
    def __init__(
        self,
        cliente: ClienteNotion,
        ids: IdsBancos,
        config: Configuracao,
        armazem: ArmazemDeArtes,
        adaptadores: Mapping[str, PlatformAdapter],
        parametros: Parametros,
        agora: Callable[[], datetime],
        publicar_real: bool,
        tipos_por_formato: Mapping[str, Mapping[str, str]],
    ) -> None:
        self._cliente = cliente
        self._ids = ids
        self._config = config
        self._armazem = armazem
        self._adaptadores = adaptadores
        self._parametros = parametros
        self._agora = agora
        self._real = publicar_real
        self._tipos = tipos_por_formato
        self._com_adaptador = frozenset(
            nome
            for nome, adaptador in adaptadores.items()
            if getattr(adaptador, "plataforma", None) == nome
        )
        self._listagem: tuple[_Listado, ...] | None = None
        self._conexoes: dict[str, Conexao | None] = {}

    # ------------------------------------------------------------------ entrada

    def processar(self, post_id: str, plataforma: str) -> Ocorrencia:
        """Um post numa plataforma, isolado: erro aqui nunca interrompe a rodada."""
        andamento = _Andamento()
        try:
            return self._processar(post_id, plataforma, andamento)
        except Exception as erro:  # noqa: BLE001 - isolamento; só o tipo vai ao log
            _log.error(
                "orquestrador: erro inesperado; post %s plataforma %s (%s)",
                post_id,
                plataforma,
                _nome_erro(erro),
            )
            if andamento.iniciada is not None:  # defesa: o envio já se trata sozinho
                return Ocorrencia(post_id, plataforma, Desfecho.ERRO, "erro_inesperado")
            registro = self._registrar_avulso(
                post_id,
                plataforma,
                andamento.chave,
                EstadoExecucao.NAO_EXECUTADO,
                CodigoMotivo.ERRO_DESCONHECIDO,
            )
            return Ocorrencia(
                post_id,
                plataforma,
                Desfecho.ERRO,
                CodigoMotivo.ERRO_DESCONHECIDO.value,
                registro,
            )

    def _processar(self, post_id: str, plataforma: str, andamento: _Andamento) -> Ocorrencia:
        post = self._reler(post_id)
        if post is not None:
            andamento.chave = self._chave(post_id, plataforma, post.selo)
            revisto = reverificar_post(
                self._cliente,
                post,
                self._config,
                self._armazem,
                tratar_selo_ausente="ignorar",
            )
            if revisto.situacao is SituacaoReverificacao.INDETERMINADO:
                codigo = revisto.motivo.value if revisto.motivo else None
                _log.warning(
                    "orquestrador: post %s plataforma %s indeterminado; não publicado",
                    post_id,
                    plataforma,
                )
                return Ocorrencia(post_id, plataforma, Desfecho.INDETERMINADO, codigo)
            if revisto.situacao is SituacaoReverificacao.DEVOLVIDO_A_PRONTO:
                recusa = _motivo_da_devolucao(revisto.motivo, post)
                registro = self._registrar_recusa(post_id, plataforma, post.selo, recusa, None)
                return Ocorrencia(
                    post_id, plataforma, Desfecho.DEVOLVIDO_A_PRONTO, recusa.value, registro
                )

        agora = self._agora()
        contexto, anteriores = self._contexto(post_id, post, plataforma, agora)
        decisao = avaliar(contexto, plataforma)
        if not decisao.permitido or post is None or not isinstance(post.selo, str):
            motivo = decisao.motivos[0] if decisao.motivos else MotivoRecusa.LEITURA_FALHOU
            selo = post.selo if post is not None else None
            registro = self._registrar_recusa(post_id, plataforma, selo, motivo, anteriores)
            _log.info(
                "orquestrador: post %s plataforma %s recusado; motivo: %s",
                post_id,
                plataforma,
                motivo.value,
            )
            return Ocorrencia(post_id, plataforma, Desfecho.RECUSADO, motivo.value, registro)
        return self._publicar(post, plataforma, anteriores or (), andamento)

    # ------------------------------------------------------------------ leitura

    def _reler(self, post_id: str) -> Post | None:
        try:
            return ler_post(self._cliente, post_id, self._config)
        except Exception as erro:  # noqa: BLE001 - falha segura: a guarda recusa
            _log.warning(
                "orquestrador: releitura do post %s falhou (%s)", post_id, _nome_erro(erro)
            )
            return None

    def _anteriores(self, post_id: str, plataforma: str) -> tuple[Execucao, ...]:
        registros = execucoes_do_post(self._cliente, self._ids, post_id, plataforma)
        return tuple(r.execucao for r in registros)

    def _listar_posts(self) -> tuple[_Listado, ...]:
        """Todos os posts (paginação completa), uma vez por rodada."""
        if self._listagem is None:
            itens: list[_Listado] = []
            for pagina in self._cliente.consultar_banco(self._ids.posts):
                pid = _id_seguro(pagina)
                if pid is None:
                    raise _MundoIlegivel("página do banco Posts sem id válido")
                try:
                    leve = post_de_pagina(pagina, self._config)
                except PostInvalido:
                    itens.append(self._listado_ilegivel(pid, pagina))
                    continue
                itens.append(_Listado(pid, leve.publicar_em, True, leve, None))
            self._listagem = tuple(itens)
        return self._listagem

    @staticmethod
    def _listado_ilegivel(pid: str, pagina: Mapping[str, Any]) -> _Listado:
        props = pagina.get("properties")
        prop = props.get(_PROP_DATA) if isinstance(props, Mapping) else None
        try:
            publicar_em, _ = data_de_propriedade(prop if isinstance(prop, Mapping) else {})
            legivel = True
        except (PostInvalido, ValueError, KeyError):
            publicar_em, legivel = None, False
        return _Listado(pid, publicar_em, legivel, None, _ids_de_relacao(pagina, _PROP_SERIE))

    def _publicados(
        self, post: Post, plataforma: str, agora: datetime
    ) -> tuple[PublicacaoConhecida, ...]:
        """Posts agendados entre este e agora que já saíram (ou podem ter saído) NA plataforma."""
        meu = post.publicar_em
        if meu is None or not _tem_fuso(meu):
            return ()  # a guarda recusa antes (sem data/hora)
        saida: list[PublicacaoConhecida] = []
        for item in self._listar_posts():
            if item.id == post.id:
                continue
            if not item.data_legivel:
                raise _MundoIlegivel("post com data ilegível impede provar a ordem")
            outro = item.publicar_em
            if outro is None or not (_utc(meu) < _utc(outro) <= _utc(agora)):
                continue
            if any(e.estado in _JA_SAIU for e in self._anteriores(item.id, plataforma)):
                saida.append(PublicacaoConhecida(item.id, outro))
        return tuple(saida)

    def _posts_da_serie(self, post: Post) -> tuple[Post, ...]:
        if post.serie is None:
            return ()
        irmaos: list[Post] = []
        for item in self._listar_posts():
            if item.post is not None:
                if item.post.serie == post.serie:
                    irmaos.append(item.post)
                continue
            ids = item.serie_ids
            if ids is None:
                raise _MundoIlegivel("post ilegível pode ser da mesma série")
            for sid in ids:
                serie = self._config.series.get(sid)
                if serie is None or serie.nome == post.serie:
                    raise _MundoIlegivel("post ilegível pode ser da mesma série")
        return tuple(irmaos)

    def _conexao(self, plataforma: str, agora: datetime) -> Conexao | None:
        if plataforma in self._conexoes:
            return self._conexoes[plataforma]
        conexao: Conexao | None = None
        adaptador = self._adaptadores.get(plataforma)
        if adaptador is not None and plataforma in self._com_adaptador:
            try:
                estado = adaptador.verificar_conexao()
                if estado.plataforma == plataforma:
                    dias = estado.dias_restantes
                    expira = None if dias is None else agora + timedelta(days=dias)
                    conexao = Conexao(plataforma, estado.estado, expira, agora)
            except Exception as erro:  # noqa: BLE001 - conexão desconhecida = inválida
                _log.warning(
                    "orquestrador: conexão de %s não verificada (%s)", plataforma, _nome_erro(erro)
                )
        self._conexoes[plataforma] = conexao
        return conexao

    def _problemas_de_midia(self, post: Post, plataforma: str) -> tuple[str, ...] | None:
        adaptador = self._adaptadores.get(plataforma)
        if adaptador is None or plataforma not in self._com_adaptador:
            return None
        try:
            artes = artes_por_plataforma(post).get(plataforma)
            if artes is None:
                return None
            validacao = adaptador.validar_midia(post, artes)
            return tuple(p.tipo.value for p in validacao.problemas)
        except Exception as erro:  # noqa: BLE001 - mídia não validada = recusa
            _log.warning(
                "orquestrador: mídia do post %s não validada para %s (%s)",
                post.id,
                plataforma,
                _nome_erro(erro),
            )
            return None

    def _contexto(
        self, post_id: str, post: Post | None, plataforma: str, agora: datetime
    ) -> tuple[ContextoGuarda, tuple[Execucao, ...] | None]:
        """Monta o contexto da guarda. Qualquer falha ao resolver o mundo vira contexto com
        ``post=None`` (a guarda recusa com ``LEITURA_FALHOU``)."""
        anteriores: tuple[Execucao, ...] | None = None
        try:
            anteriores = self._anteriores(post_id, plataforma)
            if post is None:
                raise _MundoIlegivel("post não relido")
            publicados = {plataforma: self._publicados(post, plataforma, agora)}
            serie = self._posts_da_serie(post)
        except Exception as erro:  # noqa: BLE001 - falha segura
            if not isinstance(erro, _MundoIlegivel) or post is not None:
                _log.warning(
                    "orquestrador: mundo não resolvido; post %s plataforma %s (%s)",
                    post_id,
                    plataforma,
                    _nome_erro(erro),
                )
            return self._contexto_vazio(agora), anteriores
        conexao = self._conexao(plataforma, agora)
        midia = self._problemas_de_midia(post, plataforma)
        contexto = ContextoGuarda(
            post=post,
            configuracao=self._config,
            agora=agora,
            parametros=self._parametros,
            armazem=self._armazem,
            execucoes_anteriores=anteriores,
            publicados_por_plataforma=publicados,
            posts_da_serie=serie,
            conexoes={} if conexao is None else {plataforma: conexao},
            plataformas_com_adaptador=self._com_adaptador,
            tipos_por_formato=self._tipos,
            problemas_de_midia={} if midia is None else {plataforma: midia},
        )
        return contexto, anteriores

    def _contexto_vazio(self, agora: datetime) -> ContextoGuarda:
        return ContextoGuarda(
            post=None,
            configuracao=self._config,
            agora=agora,
            parametros=self._parametros,
            armazem=self._armazem,
            execucoes_anteriores=(),
            publicados_por_plataforma={},
            posts_da_serie=(),
            conexoes={},
            plataformas_com_adaptador=frozenset(),
            tipos_por_formato={},
            problemas_de_midia={},
        )

    # ------------------------------------------------------------------ escrita

    @staticmethod
    def _chave(post_id: str, plataforma: str, selo: str | None) -> str:
        return chave_de(post_id, plataforma, selo if selo and selo.strip() else _SEM_SELO)

    def _registrar_recusa(
        self,
        post_id: str,
        plataforma: str,
        selo: str | None,
        motivo: MotivoRecusa,
        anteriores: tuple[Execucao, ...] | None,
    ) -> Registro:
        try:
            chave = self._chave(post_id, plataforma, selo)
        except ValueError:
            return Registro.FALHOU
        if anteriores is not None and any(
            e.estado is EstadoExecucao.RECUSADA
            and e.chave_idempotencia == chave
            and e.motivo == motivo.value
            for e in anteriores
        ):
            return Registro.JA_EXISTIA
        return self._registrar_avulso(post_id, plataforma, chave, EstadoExecucao.RECUSADA, motivo)

    def _registrar_avulso(
        self,
        post_id: str,
        plataforma: str,
        chave: str | None,
        estado: EstadoExecucao,
        motivo: MotivoRecusa | CodigoMotivo,
    ) -> Registro:
        """Registro sem envio (recusa ou não executado) em UMA única escrita (T039): a página
        já nasce concluída, então uma falha nunca deixa uma execução ``iniciada`` órfã."""
        try:
            chave = chave or self._chave(post_id, plataforma, None)
            registrar_recusa(
                self._cliente,
                self._ids,
                post_id,
                plataforma,
                chave,
                _TENTATIVA,
                motivo,
                self._agora(),
                estado,
            )
        except Exception as erro:  # noqa: BLE001 - registro é melhor esforço aqui
            _log.error(
                "orquestrador: %s não registrado; post %s plataforma %s (%s)",
                estado.value,
                post_id,
                plataforma,
                _nome_erro(erro),
            )
            return Registro.FALHOU
        return Registro.GRAVADO

    # ------------------------------------------------------------------ envio

    def _publicar(
        self,
        post: Post,
        plataforma: str,
        anteriores: Sequence[Execucao],
        andamento: _Andamento,
    ) -> Ocorrencia:
        assert post.selo is not None  # garantido pela guarda (SELO_AUSENTE)
        chave = chave_de(post.id, plataforma, post.selo)
        andamento.chave = chave
        if any(e.estado is EstadoExecucao.FALHA for e in anteriores):
            # Gancho T040: a nova tentativa segura (consultar, 30 min, uma vez) entra aqui.
            _log.info(
                "orquestrador: post %s plataforma %s com falha anterior; aguarda nova tentativa",
                post.id,
                plataforma,
            )
            return Ocorrencia(post.id, plataforma, Desfecho.AGUARDANDO_NOVA_TENTATIVA)
        if not self._real and any(
            e.estado is EstadoExecucao.SIMULADA and e.chave_idempotencia == chave
            for e in anteriores
        ):
            return Ocorrencia(post.id, plataforma, Desfecho.SIMULACAO_REPETIDA)

        adaptador = self._adaptadores[plataforma]
        artes = artes_por_plataforma(post)[plataforma]
        try:
            registro = iniciar_execucao(
                self._cliente, self._ids, post.id, plataforma, chave, _TENTATIVA, self._agora()
            )
        except Exception as erro:  # noqa: BLE001 - sem "iniciada" confirmada, NÃO publica
            _log.error(
                "orquestrador: 'iniciada' não gravada; post %s plataforma %s não publicado (%s)",
                post.id,
                plataforma,
                _nome_erro(erro),
            )
            return Ocorrencia(
                post.id,
                plataforma,
                Desfecho.NAO_INICIADO,
                CodigoMotivo.NOTION_INCERTO.value,
                Registro.FALHOU,
            )
        andamento.iniciada = registro

        pedido = PedidoDePublicacao(post, plataforma, artes, chave, simular=not self._real)
        try:
            resultado = adaptador.publicar(pedido)
        except Exception as erro:  # noqa: BLE001 - pode ter saído: incerto (RF-38)
            _log.error(
                "orquestrador: adaptador falhou; post %s plataforma %s (%s)",
                post.id,
                plataforma,
                _nome_erro(erro),
            )
            if self._real:
                return self._concluir(
                    registro,
                    Desfecho.INCERTO,
                    EstadoExecucao.INCERTO,
                    CodigoMotivo.ERRO_DESCONHECIDO,
                    None,
                    None,
                )
            return self._concluir(
                registro,
                Desfecho.ERRO,
                EstadoExecucao.NAO_EXECUTADO,
                CodigoMotivo.ERRO_DESCONHECIDO,
                None,
                None,
            )
        return self._classificar(registro, resultado, chave)

    def _classificar(self, registro: RegistroExecucao, resultado: object, chave: str) -> Ocorrencia:
        """Traduz o resultado do adaptador para o registro (sem confiar cegamente nele)."""
        if not isinstance(resultado, ResultadoPublicacao) or resultado.chave_idempotencia != chave:
            _log.error("orquestrador: resultado inesperado do adaptador; tratado como incerto")
            if self._real:
                return self._concluir(
                    registro,
                    Desfecho.INCERTO,
                    EstadoExecucao.INCERTO,
                    CodigoMotivo.ERRO_DESCONHECIDO,
                    None,
                    None,
                )
            return self._concluir(
                registro,
                Desfecho.ERRO,
                EstadoExecucao.NAO_EXECUTADO,
                CodigoMotivo.ERRO_DESCONHECIDO,
                None,
                None,
            )
        tipo = resultado.tipo
        if resultado.simulado is not True:
            if not self._real:  # pediu simulação e o adaptador diz que enviou: pode ter saído
                _log.error("orquestrador: adaptador não respeitou a simulação; incerto")
                return self._concluir(
                    registro,
                    Desfecho.INCERTO,
                    EstadoExecucao.INCERTO,
                    CodigoMotivo.ERRO_DESCONHECIDO,
                    None,
                    tipo,
                )
            desfecho, estado, motivo = _REAL[tipo]
            id_externo = resultado.id_externo if resultado.sucesso else None
            if id_externo is not None and _RE_ID_EXTERNO.fullmatch(id_externo) is None:
                _log.warning("orquestrador: id externo fora do padrão; não gravado")
                id_externo = None
            return self._concluir(registro, desfecho, estado, motivo, id_externo, tipo)
        if self._real:
            _log.warning("orquestrador: modo real, mas o adaptador só simulou; nada enviado")
        return self._concluir(
            registro,
            Desfecho.SIMULADO,
            EstadoExecucao.SIMULADA,
            _SEM_MOTIVO_SIMULADO[tipo],
            None,
            tipo,
        )

    def _concluir(
        self,
        registro: RegistroExecucao,
        desfecho: Desfecho,
        estado: EstadoExecucao,
        motivo: CodigoMotivo | None,
        id_externo: str | None,
        tipo: TipoDeResultado | None,
    ) -> Ocorrencia:
        """Grava o resultado na MESMA execução. Se falhar, NÃO reenvia: a execução continua
        ``iniciada`` (bloqueante para a guarda) e o erro vai ao relatório.

        O estágio do post não muda aqui: a pós-publicação (T039) roda depois que todas as
        plataformas do post foram processadas, a partir das execuções gravadas no Notion.
        """
        execucao = registro.execucao
        situacao = Registro.GRAVADO
        try:
            registrar_resultado(self._cliente, registro, estado, motivo, id_externo, self._agora())
        except Exception as erro:  # noqa: BLE001 - não repete o envio em hipótese nenhuma
            situacao = Registro.FALHOU
            _log.error(
                "orquestrador: resultado %s não registrado; post %s plataforma %s; execução "
                "segue 'iniciada' e bloqueia nova publicação (%s)",
                estado.value,
                execucao.post_id,
                execucao.plataforma,
                _nome_erro(erro),
            )
        _log.info(
            "orquestrador: post %s plataforma %s: %s",
            execucao.post_id,
            execucao.plataforma,
            desfecho.value,
        )
        return Ocorrencia(
            execucao.post_id,
            execucao.plataforma,
            desfecho,
            motivo.value if motivo is not None else None,
            situacao,
            tipo,
            id_externo,
            registro.pagina_id,
        )


# --------------------------------------------------------------------------- execução


def _devidos(
    paginas: Sequence[Mapping[str, Any]], config: Configuracao, instante: datetime
) -> tuple[list[_Devido], list[Ocorrencia]]:
    """Posts com ``publicar_em <= instante``, em ordem determinística; ilegíveis à parte."""
    vistos: set[str] = set()
    devidos: list[_Devido] = []
    ilegiveis: list[Ocorrencia] = []
    for pagina in paginas:
        pid = _id_seguro(pagina)
        if pid is not None and pid in vistos:
            continue  # paginação repetiu a página
        if pid is not None:
            vistos.add(pid)
        try:
            leve = post_de_pagina(pagina, config)
        except PostInvalido:
            ilegiveis.append(
                Ocorrencia(
                    pid or "(id inválido)", _PLATAFORMA_DESCONHECIDA, Desfecho.ERRO, "post_ilegivel"
                )
            )
            continue
        agendado = leve.publicar_em
        if agendado is None or not _tem_fuso(agendado) or _utc(agendado) > _utc(instante):
            continue
        devidos.append(_Devido(_utc(agendado), leve.id, tuple(sorted(leve.plataformas))))
    devidos.sort(key=lambda d: (d.instante, d.post_id))
    return devidos, ilegiveis


def executar(
    cliente: ClienteNotion,
    ids: IdsBancos,
    config: Configuracao,
    armazem: ArmazemDeArtes,
    adaptadores: Mapping[str, PlatformAdapter],
    parametros: Parametros,
    agora: Callable[[], datetime],
    *,
    publicar_real: bool,
    tipos_por_formato: Mapping[str, Mapping[str, str]] | None = None,
    pagina_sinal_de_vida: str | None = None,
) -> RelatorioExecucao:
    """Uma rodada do publicador. Veja o docstring do módulo para o fluxo e as decisões.

    ``publicar_real`` vem do ponto de entrada (``modo_real_ligado()``); ``False`` = simulação.
    ``adaptadores``: plataforma (texto exato de ``Post.plataformas``) -> adaptador.
    ``agora``: relógio injetado, com fuso. ``tipos_por_formato``: formato -> plataforma ->
    tipo (A-023); ausente = vazio = tudo recusado. ``pagina_sinal_de_vida``: página do sinal a
    atualizar (sem ela, cria uma). Nunca levanta por causa de um post.
    """
    modo_real = publicar_real is True
    instante = agora()
    if config.problemas_tipos:  # só a contagem: o texto do mapeamento nunca vai ao log
        _log.warning(
            "orquestrador: %d problema(s) no mapeamento de tipos por formato; os formatos "
            "afetados ficam sem tipo (recusa)",
            len(config.problemas_tipos),
        )
    ocorrencias: list[Ocorrencia] = []
    conclusoes: list[ResultadoPos] = []
    falha: FalhaGeral | None = None
    sinal: str | None = None

    if not _tem_fuso(instante):
        falha = FalhaGeral.AGORA_SEM_FUSO
    elif not ids.posts or not ids.execucoes:
        falha = FalhaGeral.IDS_INCOMPLETOS
    else:
        try:
            exigir_estagio_autorizador(config)
            exigir_estagio_pronto(config)
        except ConfirmacaoRecusada:
            falha = FalhaGeral.CONFIG_ESTAGIOS_INVALIDA
        except ConfiguracaoSemProntoUnico:
            falha = FalhaGeral.CONFIG_SEM_PRONTO

    try:
        if falha is None:
            autorizador_id, _ = exigir_estagio_autorizador(config)
            filtro = {"property": _PROP_ESTAGIO, "relation": {"contains": autorizador_id}}
            try:
                paginas = cliente.consultar_banco(ids.posts, filtro)
            except Exception as erro:  # noqa: BLE001 - sem lista, nada a publicar
                _log.error("orquestrador: listagem dos posts falhou (%s)", _nome_erro(erro))
                falha = FalhaGeral.NOTION_INDISPONIVEL
            else:
                devidos, ilegiveis = _devidos(paginas, config, instante)
                ocorrencias.extend(ilegiveis)
                rodada = _Rodada(
                    cliente,
                    ids,
                    config,
                    armazem,
                    adaptadores,
                    parametros,
                    agora,
                    modo_real,
                    tipos_por_formato
                    if tipos_por_formato is not None
                    else config.tipos_por_formato,
                )
                for devido in devidos:
                    do_post = [rodada.processar(devido.post_id, p) for p in devido.plataformas]
                    if modo_real and do_post:
                        # Todas as plataformas do post já foram processadas (T039).
                        conclusao = concluir_post(
                            cliente, ids, config, devido.post_id, modo_real=modo_real
                        )
                        conclusoes.append(conclusao)
                        do_post = [replace(o, pos_publicacao=conclusao.desfecho) for o in do_post]
                    ocorrencias.extend(do_post)
        else:
            _log.error("orquestrador: rodada sem publicação; motivo: %s", falha.value)
    finally:
        if ids.execucoes and falha is not FalhaGeral.AGORA_SEM_FUSO:
            try:
                sinal = sinal_de_vida(cliente, ids, agora(), pagina_sinal_de_vida)
            except Exception as erro:  # noqa: BLE001 - não derruba o relatório
                _log.error("orquestrador: sinal de vida não gravado (%s)", _nome_erro(erro))

    relatorio = RelatorioExecucao(modo_real, tuple(ocorrencias), falha, sinal, tuple(conclusoes))
    resumo = ", ".join(f"{k} {v}" for k, v in relatorio.contagens().items() if v)
    _log.info(
        "orquestrador: modo %s; %d ocorrências (%s); erros de registro %d",
        "real" if modo_real else "simulação",
        len(ocorrencias),
        resumo or "nenhuma",
        len(relatorio.erros_de_registro),
    )
    return relatorio
