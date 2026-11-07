"""Guarda de publicação (plano 4.1; RF-10 a RF-12, RF-14, RF-18, RF-24, RF-26, RF-27,
RF-37, RF-39, RF-43; regra I-2).

Histórico: T030 fixou tipos, assinatura e contrato (``tests/test_guarda.py``); T031 implementou
as verificações 1 a 5, T032 a 6, T033 a 7 e T034 as 8 a 10. Com a T034 a guarda está COMPLETA:
``avaliar`` só devolve ``permitir()`` quando TODAS as verificações de ``_VERIFICACOES`` passam
(a trava temporária ``GuardaIncompleta`` das T031 a T033 foi removida).

Contrato:

* **Função pura e determinística.** Sem rede, sem relógio próprio (nunca ``datetime.now``), sem
  escrita. O chamador resolve o mundo externo (relê o post e a configuração no Notion, consulta
  execuções, conexões e publicações, valida a mídia com o adaptador) e entrega tudo em
  ``ContextoGuarda``. A única leitura feita aqui é o recálculo do selo no ``armazem``.
* **Um motivo só.** Devolve ``ResultadoGuarda.permitir()`` ou ``ResultadoGuarda.recusar(m)``
  com UM ``MotivoRecusa``: o da primeira verificação que falha, na ordem canônica abaixo.
* **Falha segura.** ``avaliar`` nunca levanta exceção: dado ausente, inconsistente, de tipo
  inesperado ou erro do armazém viram RECUSA. Nunca libera na dúvida. ``ContextoGuarda`` não
  valida nada no construtor, de propósito: quem valida é ``avaliar``.
* **I-1.** O ``detalhe`` do resultado nunca traz credencial nem conteúdo do post (título,
  corpo, seções, caminhos de arte, selo); no máximo o código do motivo e nomes de campos.

Ordem canônica (plano 4.1), parando na primeira que falhar:

1. ``LEITURA_FALHOU`` (post ou configuração não lidos) e depois ``CONFIG_INCONSISTENTE``
   (problemas registrados, zero ou mais de um estágio autorizador, nome diferente de
   "confirmado" ou nome reservado em outro papel) (RF-39);
2. ``ESTAGIO_NAO_AUTORIZADO`` (RF-10);
3. ``SEM_DATA`` e depois ``SEM_HORA`` (RF-10);
4. ``DATA_FUTURA`` (RF-11);
5. ``SELO_AUSENTE`` e depois ``SELO_DIVERGENTE`` (RF-26);
6. (antes dela, a plataforma pedida: vazia ou fora de ``post.plataformas`` recusa com
   ``ESTAGIO_NAO_AUTORIZADO``, ver ``_plataforma_do_post``) ``FORA_DA_JANELA`` e depois
   ``POST_POSTERIOR_JA_SAIU``, por plataforma (RF-24, A-010);
7. ``SERIE_BLOQUEADA`` (RF-27);
8. ``SEM_ADAPTADOR`` (RF-37) e depois ``CONEXAO_INVALIDA`` (RF-18);
9. ``FORMATO_SEM_TIPO`` (RF-43) e depois ``ARTE_INVALIDA`` (RF-14);
10. ``EXECUCAO_EXISTENTE`` (RF-12, RF-38).

``SEM_ADAPTADOR`` aparece no plano 4.1 antes de ``CONEXAO_INVALIDA`` e ``FORMATO_SEM_TIPO``
antes de ``ARTE_INVALIDA`` (a compatibilidade da arte depende do tipo na plataforma; decisão
da T030). Detalhes de cada uma nas docstrings das funções.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from publicador.artes.armazem import ArmazemDeArtes
from publicador.core.config import Parametros
from publicador.core.confirmacao import exigir_estagio_autorizador
from publicador.core.modelos import (
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
from publicador.core.selo import verificar_selo
from publicador.notion.bancos import Configuracao

__all__ = ["ContextoGuarda", "PublicacaoConhecida", "avaliar"]


@dataclass(frozen=True)
class PublicacaoConhecida:
    """Post que JÁ SAIU numa plataforma: só identificador e horário agendado (sem conteúdo)."""

    post_id: str
    agendado_para: datetime


@dataclass(frozen=True)
class ContextoGuarda:
    """Tudo o que a guarda precisa, já resolvido pelo chamador (sem rede dentro da guarda).

    Chaves de plataforma usam o mesmo texto de ``Post.plataformas``.
    """

    # Post relido do Notion agora; ``None`` = a leitura falhou.
    post: Post | None
    # Configuração relida (estágios, séries, problemas); ``None`` = a leitura falhou.
    configuracao: Configuracao | None
    # Instante atual, com fuso, injetado pelo chamador.
    agora: datetime
    parametros: Parametros
    # Armazém para recalcular o selo (lê as artes).
    armazem: ArmazemDeArtes
    # Execuções anteriores do MESMO post e plataforma, de qualquer data (RF-38).
    execucoes_anteriores: tuple[Execucao, ...]
    # Por plataforma: posts que já saíram nela (RF-24, regra de ordem por plataforma).
    publicados_por_plataforma: Mapping[str, tuple[PublicacaoConhecida, ...]]
    # Os demais posts da mesma série do post, relidos (vazio se o post não tem série) (RF-27).
    posts_da_serie: tuple[Post, ...]
    # Último estado conhecido da conexão por plataforma; ausente = inválida (RF-18).
    conexoes: Mapping[str, Conexao]
    # Plataformas com adaptador disponível (RF-37).
    plataformas_com_adaptador: frozenset[str]
    # Nome do formato -> plataforma -> tipo de publicação (RF-43, A-023).
    tipos_por_formato: Mapping[str, Mapping[str, str]]
    # Problemas de mídia por plataforma (``validar_midia`` do adaptador); ausente = não validado.
    problemas_de_midia: Mapping[str, tuple[str, ...]]


# Uma verificação olha UMA condição e devolve o motivo da recusa ou ``None`` (passou).
Verificacao = Callable[["ContextoGuarda", str], MotivoRecusa | None]

M = MotivoRecusa

# Detalhes fixos (I-1): só o código do motivo e nomes de campos, nunca conteúdo do post.
_DETALHES: Final[Mapping[MotivoRecusa, str]] = {
    M.LEITURA_FALHOU: "post ou configuração não lidos do Notion",
    M.CONFIG_INCONSISTENTE: "configuração de estágios inconsistente (RF-39)",
    M.ESTAGIO_NAO_AUTORIZADO: "estágio do post não é o autorizador 'confirmado'",
    M.SEM_DATA: "post sem data e hora",
    M.SEM_HORA: "post sem hora (ou sem fuso)",
    M.DATA_FUTURA: "horário agendado no futuro (ou instante atual sem fuso)",
    M.SELO_AUSENTE: "post sem selo de confirmação",
    M.SELO_DIVERGENTE: "selo de confirmação não confere com o recálculo",
    M.FORA_DA_JANELA: (
        "horário agendado passou da janela de tolerância, ou dados para medi-la ausentes (RF-24)"
    ),
    M.POST_POSTERIOR_JA_SAIU: (
        "post agendado para depois já saiu nesta plataforma, ou publicações dela não "
        "informadas (A-010)"
    ),
    M.SERIE_BLOQUEADA: (
        "série de continuidade com post anterior não publicado, ou série ou ordem na série "
        "ausente, ambígua ou repetida (RF-27)"
    ),
    M.SEM_ADAPTADOR: "plataforma sem adaptador disponível (RF-37)",
    M.CONEXAO_INVALIDA: (
        "conexão da plataforma ausente, não ativa, vencida ou com validade desconhecida (RF-18)"
    ),
    M.FORMATO_SEM_TIPO: (
        "formato do post ausente ou sem tipo de publicação definido para a plataforma (RF-43)"
    ),
    M.ARTE_INVALIDA: "mídia não validada ou com problemas para a plataforma (RF-14)",
    M.EXECUCAO_EXISTENTE: (
        "já existe execução bloqueante deste post nesta plataforma, ou lista de execuções "
        "inconsistente (RF-12, RF-38)"
    ),
}


def _tem_fuso(valor: object) -> bool:
    return (
        isinstance(valor, datetime) and valor.tzinfo is not None and (valor.utcoffset() is not None)
    )


# --------------------------------------------------------------------------- parte 1 (T031)


def _leitura(contexto: ContextoGuarda, _plataforma: str) -> MotivoRecusa | None:
    """1a. Post e configuração relidos agora (``None`` ou tipo inesperado = leitura falhou)."""
    if not isinstance(contexto.post, Post) or not isinstance(contexto.configuracao, Configuracao):
        return M.LEITURA_FALHOU
    return None


def _autorizador(contexto: ContextoGuarda) -> Estagio | None:
    """O único estágio autorizador da configuração íntegra, ou ``None`` (qualquer problema)."""
    configuracao = contexto.configuracao
    if not isinstance(configuracao, Configuracao):
        return None
    try:
        _, estagio = exigir_estagio_autorizador(configuracao)
    except Exception:  # ConfirmacaoRecusada ou dado corrompido: falha segura
        return None
    return estagio


def _configuracao(contexto: ContextoGuarda, _plataforma: str) -> MotivoRecusa | None:
    """1b. Exatamente um estágio autorizador, chamado "confirmado", sem problemas (RF-39)."""
    return M.CONFIG_INCONSISTENTE if _autorizador(contexto) is None else None


def _estagio(contexto: ContextoGuarda, _plataforma: str) -> MotivoRecusa | None:
    """2. O post está no estágio autorizador da configuração (papel E nome) (RF-10)."""
    post = contexto.post
    autorizador = _autorizador(contexto)
    if not isinstance(post, Post) or autorizador is None:
        return M.ESTAGIO_NAO_AUTORIZADO
    estagio = post.estagio
    if (
        not isinstance(estagio, Estagio)
        or estagio.papel is not PapelEstagio.AUTORIZADO
        or estagio != autorizador
    ):
        return M.ESTAGIO_NAO_AUTORIZADO
    return None


def _data(contexto: ContextoGuarda, _plataforma: str) -> MotivoRecusa | None:
    """3a. Há data: ``publicar_em`` preenchido, ou só o dia (``somente_data``) (RF-10)."""
    post = contexto.post
    if not isinstance(post, Post):
        return M.SEM_DATA
    if post.somente_data is True:
        return None  # tem o dia; a falta da hora é a verificação seguinte
    return None if isinstance(post.publicar_em, datetime) else M.SEM_DATA


def _hora(contexto: ContextoGuarda, _plataforma: str) -> MotivoRecusa | None:
    """3b. Há hora: não é "só data" e ``publicar_em`` tem fuso (RF-10)."""
    post = contexto.post
    if not isinstance(post, Post) or post.somente_data is not False:
        return M.SEM_HORA
    return None if _tem_fuso(post.publicar_em) else M.SEM_HORA


def _futuro(contexto: ContextoGuarda, _plataforma: str) -> MotivoRecusa | None:
    """4. ``publicar_em <= agora`` em instantes absolutos; ``agora`` sem fuso recusa (RF-11)."""
    post = contexto.post
    agora = contexto.agora
    agendado = post.publicar_em if isinstance(post, Post) else None
    if not isinstance(agendado, datetime) or not _tem_fuso(agendado) or not _tem_fuso(agora):
        return M.DATA_FUTURA
    # Instantes absolutos (UTC): comparar datetimes de mesmo tzinfo ignora ``fold``.
    return M.DATA_FUTURA if agendado.astimezone(UTC) > agora.astimezone(UTC) else None


def _selo_presente(contexto: ContextoGuarda, _plataforma: str) -> MotivoRecusa | None:
    """5a. Há selo gravado (não vazio, não só espaços) (RF-26)."""
    post = contexto.post
    if not isinstance(post, Post) or not isinstance(post.selo, str) or not post.selo.strip():
        return M.SELO_AUSENTE
    return None


def _selo_confere(contexto: ContextoGuarda, _plataforma: str) -> MotivoRecusa | None:
    """5b. O selo recalculado AGORA (lê as artes) é igual ao gravado (RF-26).

    Selo malformado ou de outra versão, ``SeloImpossivel``, arte ausente, variação inválida e
    erro do armazém: tudo vira ``SELO_DIVERGENTE`` (não há como provar que nada mudou).
    """
    post = contexto.post
    if not isinstance(post, Post):
        return M.SELO_DIVERGENTE
    try:
        confere = verificar_selo(post, contexto.armazem, post.selo)
    except Exception:  # falha segura: qualquer erro no recálculo recusa
        return M.SELO_DIVERGENTE
    return None if confere is True else M.SELO_DIVERGENTE


# --------------------------------------------------------------------------- parte 2 (T032)


def _em_utc(valor: datetime) -> datetime:
    """Instante absoluto em UTC.

    Comparar ou somar ``datetime`` com o MESMO ``tzinfo`` usa o relógio de parede (ignora
    ``fold`` e mudanças de horário); convertendo os dois lados para UTC antes, a conta é feita
    sempre em instantes absolutos (R13).
    """
    return valor.astimezone(UTC)


def _plataforma_do_post(contexto: ContextoGuarda, plataforma: str) -> MotivoRecusa | None:
    """Pré-6. A plataforma pedida é texto não vazio e está em ``post.plataformas``.

    A confirmação (estágio "confirmado" + selo, que cobre ``plataformas``) autoriza o post só
    nas plataformas listadas nele; pedir outra é publicar sem autorização (RF-10). Não há código
    próprio em ``MotivoRecusa``, então recusa com ``ESTAGIO_NAO_AUTORIZADO`` e um detalhe
    específico (``_DETALHES_POR_VERIFICACAO``) para o registro não confundir com o estágio.
    Comparação exata, sem normalizar caixa ou espaços (falha segura).
    """
    post = contexto.post
    if not isinstance(post, Post) or not isinstance(plataforma, str) or not plataforma.strip():
        return M.ESTAGIO_NAO_AUTORIZADO
    plataformas = post.plataformas
    if not isinstance(plataformas, tuple | list) or plataforma not in plataformas:
        return M.ESTAGIO_NAO_AUTORIZADO
    return None


def _janela(contexto: ContextoGuarda, _plataforma: str) -> MotivoRecusa | None:
    """6a. ``agora - publicar_em <= janela_tolerancia_minutos`` (RF-24).

    Janela INCLUSIVA: exatamente a tolerância depois ainda publica; um instante a mais, não.
    Instantes absolutos (UTC). ``agora`` ou ``publicar_em`` sem fuso, ou tolerância que não é
    um inteiro positivo (``bool`` não conta), recusam.
    """
    post = contexto.post
    agora = contexto.agora
    agendado = post.publicar_em if isinstance(post, Post) else None
    parametros = contexto.parametros
    tolerancia = (
        parametros.janela_tolerancia_minutos if isinstance(parametros, Parametros) else None
    )
    if (
        not isinstance(agendado, datetime)
        or not _tem_fuso(agendado)
        or not _tem_fuso(agora)
        or isinstance(tolerancia, bool)
        or not isinstance(tolerancia, int)
        or tolerancia <= 0
    ):
        return M.FORA_DA_JANELA
    atraso = _em_utc(agora) - _em_utc(agendado)
    return None if atraso <= timedelta(minutes=tolerancia) else M.FORA_DA_JANELA


def _ordem(contexto: ContextoGuarda, plataforma: str) -> MotivoRecusa | None:
    """6b. Nenhum post agendado para DEPOIS deste já saiu NESTA plataforma (RF-24, A-010).

    * Só a lista da plataforma pedida conta; o que saiu em outra plataforma não importa.
    * Chave da plataforma AUSENTE do mapeamento = informação não fornecida: RECUSA (falha
      segura). "Nenhum publicado" se diz com a chave presente e tupla vazia.
    * Post anterior já publicado não impede. O próprio post (mesmo ``post_id``) é ignorado:
      repetir publicação é assunto da verificação 10 (RF-12, RF-38).
    * Empate (mesmo instante absoluto) NÃO é "posterior": não bloqueia. Posts agendados para o
      mesmo instante não têm ordem definida entre si.
    * Item malformado (tipo inesperado, ``post_id`` vazio, horário sem fuso) recusa: não há
      como provar que não é posterior.
    """
    post = contexto.post
    publicados = contexto.publicados_por_plataforma
    if not isinstance(post, Post) or not isinstance(post.id, str) or not post.id.strip():
        return M.POST_POSTERIOR_JA_SAIU
    agendado = post.publicar_em
    if not isinstance(agendado, datetime) or not _tem_fuso(agendado):
        return M.POST_POSTERIOR_JA_SAIU
    if not isinstance(publicados, Mapping) or plataforma not in publicados:
        return M.POST_POSTERIOR_JA_SAIU
    lista = publicados[plataforma]
    if not isinstance(lista, tuple | list):
        return M.POST_POSTERIOR_JA_SAIU
    meu_instante = _em_utc(agendado)
    for item in lista:
        if (
            not isinstance(item, PublicacaoConhecida)
            or not isinstance(item.post_id, str)
            or not item.post_id.strip()
            or not _tem_fuso(item.agendado_para)
        ):
            return M.POST_POSTERIOR_JA_SAIU
        if item.post_id == post.id:
            continue
        if _em_utc(item.agendado_para) > meu_instante:
            return M.POST_POSTERIOR_JA_SAIU
    return None


# --------------------------------------------------------------------------- parte 3 (T033)


def _ordem_valida(valor: object) -> bool:
    """Ordem na série: inteiro positivo (``bool`` não conta)."""
    return isinstance(valor, int) and not isinstance(valor, bool) and valor >= 1


def _serie_da_configuracao(contexto: ContextoGuarda, nome: str) -> Serie | None:
    """A ÚNICA série da configuração com esse nome exato, ou ``None`` (ausente, ambígua ou
    configuração com item malformado: falha segura, a guarda não adivinha qual vale)."""
    configuracao = contexto.configuracao
    if not isinstance(configuracao, Configuracao) or not isinstance(configuracao.series, Mapping):
        return None
    series = list(configuracao.series.values())
    if any(not isinstance(s, Serie) or not isinstance(s.continuidade, bool) for s in series):
        return None
    achadas = [s for s in series if s.nome == nome]
    return achadas[0] if len(achadas) == 1 else None


def _serie(contexto: ContextoGuarda, _plataforma: str) -> MotivoRecusa | None:
    """7. Série de continuidade: todos os posts ANTERIORES já publicados (RF-27).

    * Post sem série (``serie is None``) não é bloqueado. Série vazia ou de tipo inesperado,
      série fora da configuração ou ambígua (mais de uma com o mesmo nome) recusam.
    * Série SEM continuidade nunca bloqueia (RF-27): os posts não dependem uns dos outros.
    * Série COM continuidade: o post precisa de ``ordem_na_serie`` inteira positiva. Cada
      post ANTERIOR (ordem menor) em ``posts_da_serie`` precisa estar num estágio de papel
      ``publicado`` (RF-35: o sucesso em modo privado também leva a esse papel). Qualquer outro
      papel bloqueia: ``falha`` (falhou), ``cancelado``, ``autorizado`` (ainda não saiu, ou
      perdeu a janela de 2 horas), ``pronto``, ``pauta``. Posteriores não importam.
    * Lacunas na ordem (1, 3, 7) são aceitas; ordem repetida (inclusive a do próprio post) ou
      ausente/inválida em qualquer post da série recusa: não há como saber quem vem antes.
    * O próprio post (mesmo ``id``) na lista é ignorado. Post de OUTRA série misturado na lista
      não conta (é ignorado, sem validar sua ordem). Item que não é ``Post`` recusa.

    **Liberação manual (RF-27; spec, critério de aceite de RF-27 e esclarecimento 11.5).** A
    spec diz só que o bloqueio "pode ser liberado manualmente pelo usuário, alterando, no
    Notion, o status do post que causou o bloqueio", sem dizer PARA QUAL status. A guarda é
    pura e relê o mundo a cada execução, sem estado persistido de "bloqueio" ou "liberação":
    a liberação acontece sozinha quando o post bloqueador deixa de bloquear. Interpretação
    adotada (a MAIS CONSERVADORA, que libera menos): só libera quando o post bloqueador passa
    a um estágio de papel ``publicado`` (por exemplo, o usuário publicou à mão e marcou). Mudar
    para ``cancelado``, ``falha``, ``pronto``, ``pauta`` ou de volta a ``confirmado`` NÃO libera,
    porque a própria RF-27 diz que falha e cancelamento bloqueiam e que um post só sai se os
    anteriores "já tiverem sido publicados". Efeito colateral, não previsto como liberação pela
    spec: tirar o bloqueador da série ou reordená-lo para depois do post (HU-09) também faz
    a guarda deixar de vê-lo como anterior. PENDENTE de confirmação com o usuário.

    Limite: a guarda só vê o que o chamador entrega em ``posts_da_serie``; um anterior omitido
    pelo chamador não é detectado (é responsabilidade de quem monta o contexto).
    """
    post = contexto.post
    if not isinstance(post, Post):
        return M.SERIE_BLOQUEADA
    nome = post.serie
    if nome is None:
        return None  # post único
    if not isinstance(nome, str) or not nome.strip():
        return M.SERIE_BLOQUEADA
    serie = _serie_da_configuracao(contexto, nome)
    if serie is None:
        return M.SERIE_BLOQUEADA
    if serie.continuidade is False:
        return None
    minha_ordem = post.ordem_na_serie
    if not _ordem_valida(minha_ordem) or not isinstance(post.id, str) or not post.id.strip():
        return M.SERIE_BLOQUEADA
    assert isinstance(minha_ordem, int)  # garantido por _ordem_valida (para o mypy)
    irmaos = contexto.posts_da_serie
    if not isinstance(irmaos, tuple | list):
        return M.SERIE_BLOQUEADA
    ordens_vistas: set[int] = {minha_ordem}
    anteriores: list[Post] = []
    for irmao in irmaos:
        if not isinstance(irmao, Post):
            return M.SERIE_BLOQUEADA
        if irmao.id == post.id:
            continue  # o próprio post não bloqueia a si mesmo
        if irmao.serie != nome:
            continue  # post de outra série não conta
        ordem = irmao.ordem_na_serie
        if not _ordem_valida(ordem):
            return M.SERIE_BLOQUEADA
        assert isinstance(ordem, int)
        if ordem in ordens_vistas:
            return M.SERIE_BLOQUEADA  # ordem repetida: não há ordem definida
        ordens_vistas.add(ordem)
        if ordem < minha_ordem:
            anteriores.append(irmao)
    for anterior in anteriores:
        estagio = anterior.estagio
        if not isinstance(estagio, Estagio) or estagio.papel is not PapelEstagio.PUBLICADO:
            return M.SERIE_BLOQUEADA
    return None


# --------------------------------------------------------------------------- parte 4 (T034)


def _texto_valido(valor: object) -> bool:
    return isinstance(valor, str) and bool(valor.strip())


def _adaptador(contexto: ContextoGuarda, plataforma: str) -> MotivoRecusa | None:
    """8a. A plataforma pedida tem adaptador (RF-37, A-008).

    Comparação exata (como em ``_plataforma_do_post``): "instagram" não casa com "Instagram".
    Só ``set``/``frozenset`` são aceitos: com ``str`` ou lista de tipo inesperado, ``in``
    poderia casar por substring; qualquer outro tipo recusa.
    """
    com_adaptador = contexto.plataformas_com_adaptador
    if not _texto_valido(plataforma) or not isinstance(com_adaptador, frozenset | set):
        return M.SEM_ADAPTADOR
    return None if plataforma in com_adaptador else M.SEM_ADAPTADOR


def _conexao(contexto: ContextoGuarda, plataforma: str) -> MotivoRecusa | None:
    """8b. A conexão da plataforma é válida AGORA (RF-18).

    Recusa quando: a plataforma não está em ``conexoes`` (estado desconhecido = inválida);
    o item não é ``Conexao`` ou é de outra plataforma; o estado não é ``ATIVA`` (``EXPIRADA``
    e ``INVALIDA`` recusam); ``expira_em`` ausente ou sem fuso (validade desconhecida não
    prova que vale); ``agora`` sem fuso; ou a conexão VENCEU mesmo marcada ativa
    (``expira_em <= agora`` em instantes absolutos: no instante exato do vencimento já não
    vale). ``verificado_em`` não é exigido (o plano não fixa idade máxima da verificação).
    """
    conexoes = contexto.conexoes
    agora = contexto.agora
    if not _texto_valido(plataforma) or not isinstance(conexoes, Mapping):
        return M.CONEXAO_INVALIDA
    if plataforma not in conexoes:
        return M.CONEXAO_INVALIDA
    conexao = conexoes[plataforma]
    if (
        not isinstance(conexao, Conexao)
        or conexao.plataforma != plataforma
        or conexao.estado is not ConexaoEstado.ATIVA
    ):
        return M.CONEXAO_INVALIDA
    expira_em = conexao.expira_em
    if not isinstance(expira_em, datetime) or not _tem_fuso(expira_em) or not _tem_fuso(agora):
        return M.CONEXAO_INVALIDA
    return None if _em_utc(expira_em) > _em_utc(agora) else M.CONEXAO_INVALIDA


def _formato(contexto: ContextoGuarda, plataforma: str) -> MotivoRecusa | None:
    """9a. O formato do post tem tipo de publicação definido para a plataforma (RF-43, A-023).

    Recusa: post sem formato (ou vazio); ``tipos_por_formato`` que não é mapeamento; formato
    fora dele; mapeamento interno malformado; plataforma sem tipo; tipo vazio ou não texto.
    Comparação exata de nomes de formato e plataforma.
    """
    post = contexto.post
    tipos = contexto.tipos_por_formato
    formato = post.formato if isinstance(post, Post) else None
    if not isinstance(formato, str) or not formato.strip() or not _texto_valido(plataforma):
        return M.FORMATO_SEM_TIPO
    if not isinstance(tipos, Mapping) or formato not in tipos:
        return M.FORMATO_SEM_TIPO
    por_plataforma = tipos[formato]
    if not isinstance(por_plataforma, Mapping) or plataforma not in por_plataforma:
        return M.FORMATO_SEM_TIPO
    return None if _texto_valido(por_plataforma[plataforma]) else M.FORMATO_SEM_TIPO


def _arte(contexto: ContextoGuarda, plataforma: str) -> MotivoRecusa | None:
    """9b. A mídia foi validada para a plataforma e não tem problemas (RF-14).

    Plataforma ausente de ``problemas_de_midia`` = mídia NÃO validada: recusa. Lista vazia
    (tupla ou lista) passa; qualquer problema listado recusa (o texto do problema nunca vai
    para o ``detalhe``, I-1). Tipo inesperado recusa.
    """
    problemas = contexto.problemas_de_midia
    if not _texto_valido(plataforma) or not isinstance(problemas, Mapping):
        return M.ARTE_INVALIDA
    if plataforma not in problemas:
        return M.ARTE_INVALIDA
    lista = problemas[plataforma]
    if not isinstance(lista, tuple | list) or len(lista) > 0:
        return M.ARTE_INVALIDA
    return None


# Estados de execução anterior que impedem publicar de novo (RF-12, RF-38; decisão da T030).
_ESTADOS_QUE_BLOQUEIAM: Final[frozenset[EstadoExecucao]] = frozenset(
    {
        EstadoExecucao.SUCESSO,
        EstadoExecucao.SUCESSO_MODO_PRIVADO,
        EstadoExecucao.INICIADA,
        EstadoExecucao.INCERTO,
    }
)


def _execucao(contexto: ContextoGuarda, plataforma: str) -> MotivoRecusa | None:
    """10. Nenhuma execução anterior do MESMO post e plataforma impede publicar (RF-12, RF-38).

    * Bloqueiam, de QUALQUER data (A-018): ``SUCESSO``, ``SUCESSO_MODO_PRIVADO`` (já saiu),
      ``INICIADA`` (pode estar saindo, ou caiu no meio do envio) e ``INCERTO`` (pode ter
      saído; só resolução manual libera, RF-38).
    * Não bloqueiam: ``RECUSADA`` e ``SIMULADA`` (nada foi enviado; decisão da T030).
    * ``FALHA`` e ``NAO_EXECUTADO`` também NÃO bloqueiam por si (decisão da T034). ``FALHA`` só
      é registrada com certeza de que não saiu (falha transitória ou permanente; dúvida é
      ``INCERTO``), e ``NAO_EXECUTADO`` nem chegou a enviar. Bloquear aqui impediria a nova
      tentativa segura (RF-25, plano 2 passo 5, T040) e a republicação das plataformas
      pendentes após nova confirmação (A-003). O limite de UMA nova tentativa, a consulta
      prévia à plataforma e o intervalo de 30 minutos são da T040, não desta guarda; depois da
      segunda falha o post vai ao estágio de papel ``falha`` (RF-30) e a verificação 2 recusa.
    * A lista precisa ser tupla ou lista. Item que não é ``Execucao``, com estado fora de
      ``EstadoExecucao``, ou de OUTRO post ou OUTRA plataforma, é erro do chamador (o contrato
      diz "do mesmo post e plataforma"): recusa, porque não há como saber o que foi omitido.
    """
    post = contexto.post
    execucoes = contexto.execucoes_anteriores
    if not isinstance(post, Post) or not _texto_valido(post.id) or not _texto_valido(plataforma):
        return M.EXECUCAO_EXISTENTE
    if not isinstance(execucoes, tuple | list):
        return M.EXECUCAO_EXISTENTE
    for execucao in execucoes:
        if (
            not isinstance(execucao, Execucao)
            or not isinstance(execucao.estado, EstadoExecucao)
            or execucao.post_id != post.id
            or execucao.plataforma != plataforma
        ):
            return M.EXECUCAO_EXISTENTE
        if execucao.estado in _ESTADOS_QUE_BLOQUEIAM:
            return M.EXECUCAO_EXISTENTE
    return None


# Detalhe específico de uma verificação que reaproveita um motivo de outra (I-1: texto fixo).
_DETALHES_POR_VERIFICACAO: Final[Mapping[Verificacao, str]] = {
    _plataforma_do_post: "plataforma pedida vazia ou fora das plataformas confirmadas do post",
}


# Ordem canônica (plano 4.1). Cada item: (verificação, motivo usado se ela levantar exceção).
# ``avaliar`` só permite quando TODAS passam: remover um item daqui enfraquece a guarda (I-2).
_VERIFICACOES: Final[tuple[tuple[Verificacao, MotivoRecusa], ...]] = (
    (_leitura, M.LEITURA_FALHOU),  # 1
    (_configuracao, M.CONFIG_INCONSISTENTE),  # 1
    (_estagio, M.ESTAGIO_NAO_AUTORIZADO),  # 2
    (_data, M.SEM_DATA),  # 3
    (_hora, M.SEM_HORA),  # 3
    (_futuro, M.DATA_FUTURA),  # 4
    (_selo_presente, M.SELO_AUSENTE),  # 5
    (_selo_confere, M.SELO_DIVERGENTE),  # 5
    (_plataforma_do_post, M.ESTAGIO_NAO_AUTORIZADO),  # pré-6 (plataforma pedida)
    (_janela, M.FORA_DA_JANELA),  # 6
    (_ordem, M.POST_POSTERIOR_JA_SAIU),  # 6
    (_serie, M.SERIE_BLOQUEADA),  # 7
    (_adaptador, M.SEM_ADAPTADOR),  # 8
    (_conexao, M.CONEXAO_INVALIDA),  # 8
    (_formato, M.FORMATO_SEM_TIPO),  # 9
    (_arte, M.ARTE_INVALIDA),  # 9
    (_execucao, M.EXECUCAO_EXISTENTE),  # 10
)


def _primeira_recusa(contexto: ContextoGuarda, plataforma: str) -> tuple[MotivoRecusa, str] | None:
    """Motivo e detalhe (fixo, I-1) da primeira verificação que falhar, ou ``None``."""
    for verificacao, motivo_em_erro in _VERIFICACOES:
        try:
            motivo = verificacao(contexto, plataforma)
        except Exception:  # falha segura: erro inesperado recusa com o motivo da verificação
            motivo = motivo_em_erro
        if motivo is not None:
            if not isinstance(motivo, MotivoRecusa):
                motivo = motivo_em_erro
            detalhe = _DETALHES_POR_VERIFICACAO.get(verificacao) or _DETALHES.get(
                motivo, motivo.value
            )
            return motivo, detalhe
    return None


def avaliar(contexto: ContextoGuarda, plataforma: str) -> ResultadoGuarda:
    """Decide se ``contexto.post`` pode ser publicado AGORA em ``plataforma``.

    Função pura; recusa na primeira verificação que falhar (ordem canônica), com UM motivo e
    detalhe fixo (I-1). Só permite quando TODAS as verificações passam. Nunca levanta
    exceção (falha segura): um erro inesperado fora das verificações também recusa.
    """
    try:
        recusa = _primeira_recusa(contexto, plataforma)
    except Exception:  # falha segura: nunca deveria ocorrer (as verificações já se protegem)
        return ResultadoGuarda.recusar(M.LEITURA_FALHOU, detalhe=_DETALHES[M.LEITURA_FALHOU])
    if recusa is not None:
        motivo, detalhe = recusa
        return ResultadoGuarda.recusar(motivo, detalhe=detalhe)
    return ResultadoGuarda.permitir()
