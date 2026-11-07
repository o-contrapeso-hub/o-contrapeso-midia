"""Regras de confirmação do post (RF-08, RF-09) e a regra de saída do estágio autorizado.

Regras invioláveis:

* I-2: um post só passa a "autorizado" (estágio ``confirmado``) com data E hora e com o selo
  válido gravado, na MESMA operação. Configuração inconsistente = recusa (falha segura).
* I-1: nada aqui lida com credenciais; mensagens de erro são fixas (nunca ecoam resposta do
  Notion, cabeçalhos ou conteúdo do post) e o log traz só o id do post e nomes de campos.

Atomicidade: estágio, "Selo de confirmação" e "Confirmado em" vão em UM ÚNICO ``PATCH`` de
página (``ClienteNotion.atualizar_pagina``); o Notion aplica as propriedades de uma página
juntas, então ou tudo é gravado ou nada. Não se usa ``editar_post`` porque o módulo de escrita
(por regra) nunca grava selo nem "Confirmado em" e não tem esses campos; por isso a conferência
de edição concorrente (mesma regra de ``editar_post``: compara ``last_edited_time`` com o
``editado_em`` do post lido, levanta ``EdicaoConcorrente`` sem escrever) é repetida aqui,
imediatamente antes do PATCH.

Cancelar ao sair de ``autorizado``: ``sair_de_autorizado`` muda o estágio e LIMPA o selo e o
"Confirmado em" no mesmo PATCH. Assim um post fora de ``autorizado`` nunca carrega selo válido
e o job (que só publica posts em ``autorizado`` com selo conferido) não publica mais nada dele.
Não há cópia local nem fila a cancelar: o Notion é a fonte da verdade.

Limites conhecidos: (1) a API do Notion não tem escrita condicional; entre a conferência e o
PATCH há uma janela pequena, e ``last_edited_time`` é arredondado ao minuto. Por isso, depois
de gravar, o post é relido e o selo é recalculado; se não bater levanta
``SELO_NAO_CONFERE_APOS_GRAVAR`` (o post pode ter ficado em ``autorizado`` com selo que não
confere, e a guarda de publicação o recusará por divergência; corrija desconfirmando). (2) Se a
releitura após um PATCH bem-sucedido falhar, o erro propaga mas a gravação já ocorreu.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Final

from publicador.artes.armazem import (
    ArmazemDeArtes,
    ArteNaoEncontrada,
    CaminhoDeArteInvalido,
    ErroAcessoArmazem,
)
from publicador.core.modelos import NOME_ESTAGIO_AUTORIZADOR, Estagio, PapelEstagio, Post
from publicador.core.redacao import obter_logger
from publicador.core.selo import SeloImpossivel, calcular_selo, verificar_selo
from publicador.notion.bancos import Configuracao
from publicador.notion.cliente import ClienteNotion
from publicador.notion.escrita import EdicaoConcorrente
from publicador.notion.mapeamento import ler_post
from publicador.notion.variacoes import VariacaoInvalida

__all__ = [
    "ConfirmacaoRecusada",
    "MotivoConfirmacao",
    "confirmar_post",
    "exigir_estagio_autorizador",
    "propriedades_de_confirmacao",
    "propriedades_de_saida",
    "sair_de_autorizado",
    "validar_post_confirmavel",
]


_log = obter_logger("publicador.core.confirmacao")

_PROP_ESTAGIO: Final = "Estágio"
_PROP_SELO: Final = "Selo de confirmação"
_PROP_CONFIRMADO: Final = "Confirmado em"


class MotivoConfirmacao(Enum):
    SEM_DATA = "sem_data"
    SEM_HORA = "sem_hora"
    SEM_TITULO = "sem_titulo"
    SEM_PLATAFORMAS = "sem_plataformas"
    CONFIG_ESTAGIOS_INVALIDA = "config_estagios_invalida"
    POST_JA_CONFIRMADO = "post_ja_confirmado"
    ESTAGIO_DE_ORIGEM_INVALIDO = "estagio_de_origem_invalido"
    SEM_VERSAO_LIDA = "sem_versao_lida"
    SELO_IMPOSSIVEL = "selo_impossivel"
    ARTE_INVALIDA = "arte_invalida"
    ARMAZEM_INDISPONIVEL = "armazem_indisponivel"
    SELO_NAO_CONFERE_APOS_GRAVAR = "selo_nao_confere_apos_gravar"
    DESTINO_INVALIDO = "destino_invalido"
    RELEITURA_DIVERGENTE = "releitura_divergente"


class ConfirmacaoRecusada(Exception):  # noqa: N818 - nome do domínio
    """A confirmação (ou a saída de ``autorizado``) foi recusada; ``motivo`` diz por quê.

    A mensagem é fixa e sem dados sensíveis.
    """

    def __init__(self, motivo: MotivoConfirmacao, detalhe: str) -> None:
        super().__init__(detalhe)
        self.motivo = motivo
        self.detalhe = detalhe


# --------------------------------------------------------------------------- validações puras


def exigir_estagio_autorizador(config: Configuracao) -> tuple[str, Estagio]:
    """Devolve ``(id, estágio)`` do ÚNICO estágio autorizador, chamado "confirmado".

    Recusa (``CONFIG_ESTAGIOS_INVALIDA``) se a configuração tiver problemas registrados, não
    tiver estágio autorizador, tiver mais de um, ou se o nome for diferente de "confirmado" ou
    o nome reservado estiver em estágio de outro papel.
    """

    def _recusar(detalhe: str) -> ConfirmacaoRecusada:
        return ConfirmacaoRecusada(MotivoConfirmacao.CONFIG_ESTAGIOS_INVALIDA, detalhe)

    if config.problemas:
        raise _recusar("a configuração de estágios tem problemas; corrija-a no Notion")
    autorizadores = [
        (i, e) for i, e in config.estagios.items() if e.papel is PapelEstagio.AUTORIZADO
    ]
    if len(autorizadores) != 1:
        raise _recusar("a configuração deve ter exatamente um estágio autorizador")
    estagio_id, estagio = autorizadores[0]
    if estagio.nome != NOME_ESTAGIO_AUTORIZADOR:
        raise _recusar(f"o estágio autorizador deve se chamar '{NOME_ESTAGIO_AUTORIZADOR}'")
    reservado = [
        e for e in config.estagios.values()
        if e.nome == NOME_ESTAGIO_AUTORIZADOR and e.papel is not PapelEstagio.AUTORIZADO
    ]  # fmt: skip
    if reservado:
        raise _recusar(f"o nome '{NOME_ESTAGIO_AUTORIZADOR}' é reservado ao estágio autorizador")
    return estagio_id, estagio


def validar_post_confirmavel(post: Post) -> None:
    """Exige título, plataformas e data E hora. Função pura; levanta ``ConfirmacaoRecusada``."""
    if not post.titulo.strip():
        raise ConfirmacaoRecusada(MotivoConfirmacao.SEM_TITULO, "o post não tem título")
    if not post.plataformas:
        raise ConfirmacaoRecusada(MotivoConfirmacao.SEM_PLATAFORMAS, "o post não tem plataformas")
    if post.somente_data:
        raise ConfirmacaoRecusada(
            MotivoConfirmacao.SEM_HORA, "o post tem só a data; informe também a hora"
        )
    if post.publicar_em is None:
        raise ConfirmacaoRecusada(MotivoConfirmacao.SEM_DATA, "o post não tem data e hora")


def _validar_origem(post: Post) -> None:
    papel = post.estagio.papel
    if papel is PapelEstagio.AUTORIZADO:
        raise ConfirmacaoRecusada(
            MotivoConfirmacao.POST_JA_CONFIRMADO,
            "o post já está confirmado; devolva-o a outro estágio antes de confirmar de novo",
        )
    if papel is PapelEstagio.PUBLICADO:
        raise ConfirmacaoRecusada(
            MotivoConfirmacao.ESTAGIO_DE_ORIGEM_INVALIDO,
            "o post já foi publicado e não pode ser confirmado de novo",
        )


def _exigir_versao_lida(post: Post) -> datetime:
    if post.editado_em is None:
        raise ConfirmacaoRecusada(
            MotivoConfirmacao.SEM_VERSAO_LIDA,
            "o post não traz a data da última edição lida; recarregue-o do Notion",
        )
    return post.editado_em


def _exigir_fuso(valor: datetime, rotulo: str) -> None:
    if valor.tzinfo is None or valor.utcoffset() is None:
        raise ValueError(f"{rotulo} deve ter fuso horário (datetime ingênuo é rejeitado)")


# --------------------------------------------------------------------------- propriedades


def propriedades_de_confirmacao(
    estagio_autorizador_id: str, selo: str, agora: datetime
) -> dict[str, Any]:
    """Propriedades do PATCH único da confirmação: estágio + selo + "Confirmado em"."""
    _exigir_fuso(agora, "agora")
    return {
        _PROP_ESTAGIO: {"relation": [{"id": estagio_autorizador_id}]},
        _PROP_SELO: {"rich_text": [{"type": "text", "text": {"content": selo}}]},
        _PROP_CONFIRMADO: {"date": {"start": agora.isoformat()}},
    }


def propriedades_de_saida(estagio_destino_id: str) -> dict[str, Any]:
    """Propriedades do PATCH único da saída: novo estágio, selo e "Confirmado em" limpos."""
    return {
        _PROP_ESTAGIO: {"relation": [{"id": estagio_destino_id}]},
        _PROP_SELO: {"rich_text": []},
        _PROP_CONFIRMADO: {"date": None},
    }


# --------------------------------------------------------------------------- escrita


def _conferir_edicao(cliente: ClienteNotion, post: Post, esperado: datetime) -> None:
    """Mesma regra de ``editar_post``: recusa se o post mudou desde a leitura (RF-42)."""
    bruto = cliente.obter_pagina(post.id).get("last_edited_time")
    atual: datetime | None = None
    if isinstance(bruto, str):
        try:
            atual = datetime.fromisoformat(bruto)
        except ValueError:
            atual = None
    if atual is None or atual.tzinfo is None:
        raise EdicaoConcorrente(
            "Não foi possível confirmar a data da última edição do post no Notion; "
            "nada foi gravado. Recarregue o post e tente de novo."
        )
    if atual != esperado:
        raise EdicaoConcorrente(
            "O post foi editado no Notion depois da versão que você viu; nada foi gravado. "
            "Recarregue o post, confira as mudanças e refaça a operação."
        )


def _calcular_selo_seguro(post: Post, armazem: ArmazemDeArtes) -> str:
    try:
        return calcular_selo(post, armazem)
    except SeloImpossivel:
        raise ConfirmacaoRecusada(
            MotivoConfirmacao.SELO_IMPOSSIVEL,
            "não foi possível calcular o selo: confira data, hora, título e plataformas",
        ) from None
    except (ArteNaoEncontrada, CaminhoDeArteInvalido, VariacaoInvalida):
        raise ConfirmacaoRecusada(
            MotivoConfirmacao.ARTE_INVALIDA,
            "alguma arte do post não existe ou tem referência inválida; confira o campo Artes",
        ) from None
    except ErroAcessoArmazem:
        raise ConfirmacaoRecusada(
            MotivoConfirmacao.ARMAZEM_INDISPONIVEL,
            "não foi possível ler as artes no armazém; tente de novo",
        ) from None


def confirmar_post(
    cliente: ClienteNotion,
    post: Post,
    config: Configuracao,
    armazem: ArmazemDeArtes,
    agora: datetime,
) -> Post:
    """Confirma o post: grava estágio ``confirmado``, selo e "Confirmado em" num ÚNICO PATCH.

    Ordem (nada é escrito antes de tudo ser validado): configuração de estágios, estágio de
    origem, título/plataformas/data e hora, versão lida, selo (lê as artes), conferência de
    edição concorrente, PATCH, releitura. Devolve o post relido do Notion, já conferido.

    ``agora`` deve ter fuso. Erros: ``ConfirmacaoRecusada`` (motivo tipado), ``EdicaoConcorrente``
    e ``ErroNotion`` (propagam; sem estado parcial, pois o PATCH é um só).
    """
    _exigir_fuso(agora, "agora")
    estagio_id, _ = exigir_estagio_autorizador(config)
    _validar_origem(post)
    validar_post_confirmavel(post)
    esperado = _exigir_versao_lida(post)
    selo = _calcular_selo_seguro(post, armazem)

    _conferir_edicao(cliente, post, esperado)
    cliente.atualizar_pagina(post.id, propriedades_de_confirmacao(estagio_id, selo, agora))
    _log.info("post confirmado: %s; campos: estagio,selo,confirmado_em", post.id)

    novo = ler_post(cliente, post.id, config)
    if novo.estagio.papel is not PapelEstagio.AUTORIZADO or novo.selo != selo:
        raise ConfirmacaoRecusada(
            MotivoConfirmacao.RELEITURA_DIVERGENTE,
            "o Notion não devolveu o post confirmado como gravado; confira o post",
        )
    if not _selo_confere(novo, armazem):
        raise ConfirmacaoRecusada(
            MotivoConfirmacao.SELO_NAO_CONFERE_APOS_GRAVAR,
            "o conteúdo do post mudou durante a confirmação; desconfirme e confirme de novo",
        )
    return novo


def _selo_confere(post: Post, armazem: ArmazemDeArtes) -> bool:
    try:
        return verificar_selo(post, armazem, post.selo)
    except (
        SeloImpossivel,
        ArteNaoEncontrada,
        CaminhoDeArteInvalido,
        VariacaoInvalida,
        ErroAcessoArmazem,
    ):
        return False


def sair_de_autorizado(
    cliente: ClienteNotion,
    post: Post,
    config: Configuracao,
    destino: Estagio,
) -> Post:
    """Tira o post de ``autorizado`` (publicação cancelada): muda o estágio e limpa o selo e
    o "Confirmado em" num ÚNICO PATCH. Serve para qualquer destino que não seja autorizador
    (``pronto``, ``pauta``, ``cancelado``...). Também limpa selo residual de post que já estava
    fora de ``autorizado``.

    Não exige a configuração do autorizador íntegra: cancelar é o lado seguro. O destino precisa
    existir exatamente uma vez na configuração e não pode ser autorizador (para isso use
    ``confirmar_post``). Mesma conferência de edição concorrente da confirmação.
    """
    if destino.papel is PapelEstagio.AUTORIZADO:
        raise ConfirmacaoRecusada(
            MotivoConfirmacao.DESTINO_INVALIDO,
            "o destino não pode ser o estágio autorizador; use a confirmação",
        )
    ids = [i for i, e in config.estagios.items() if e == destino]
    if len(ids) != 1:
        raise ConfirmacaoRecusada(
            MotivoConfirmacao.DESTINO_INVALIDO,
            "o estágio de destino não existe (ou é ambíguo) na configuração",
        )
    esperado = _exigir_versao_lida(post)

    _conferir_edicao(cliente, post, esperado)
    cliente.atualizar_pagina(post.id, propriedades_de_saida(ids[0]))
    _log.info("post fora de autorizado: %s; campos: estagio,selo,confirmado_em", post.id)

    novo = ler_post(cliente, post.id, config)
    if novo.estagio != destino or novo.selo is not None or novo.confirmado_em is not None:
        raise ConfirmacaoRecusada(
            MotivoConfirmacao.RELEITURA_DIVERGENTE,
            "o Notion não devolveu o post como gravado; confira o post",
        )
    return novo
