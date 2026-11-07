"""Guarda de publicação (plano 4.1; RF-10 a RF-12, RF-14, RF-18, RF-24, RF-26, RF-27,
RF-37, RF-39, RF-43; regra I-2).

ESQUELETO (T030): só os tipos de entrada e a assinatura pública. A lógica vem nas T031 a T034;
o contrato está fixado pelos testes de ``tests/test_guarda.py``.

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
6. ``FORA_DA_JANELA`` e depois ``POST_POSTERIOR_JA_SAIU``, por plataforma (RF-24, A-010);
7. ``SERIE_BLOQUEADA`` (RF-27);
8. ``SEM_ADAPTADOR`` (RF-37) e depois ``CONEXAO_INVALIDA`` (RF-18);
9. ``FORMATO_SEM_TIPO`` (RF-43) e depois ``ARTE_INVALIDA`` (RF-14);
10. ``EXECUCAO_EXISTENTE`` (RF-12, RF-38).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from publicador.artes.armazem import ArmazemDeArtes
from publicador.core.config import Parametros
from publicador.core.modelos import Conexao, Execucao, Post, ResultadoGuarda
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


def avaliar(contexto: ContextoGuarda, plataforma: str) -> ResultadoGuarda:
    """Decide se ``contexto.post`` pode ser publicado AGORA em ``plataforma``.

    Função pura; nunca levanta exceção (falha segura). Veja o docstring do módulo.
    """
    raise NotImplementedError("guarda de publicação ainda não implementada (T031 a T034)")
