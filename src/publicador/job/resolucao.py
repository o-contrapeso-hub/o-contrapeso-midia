"""Resolução manual do estado incerto (T040; RF-38, A-018). Chamada pelo painel (T056).

Uma execução ``incerto`` (ou ``iniciada`` que ficou sem resultado: queda no meio do envio) pode
ter saído na plataforma. Enquanto não for resolvida, ela bloqueia o mesmo post e plataforma de
QUALQUER data (guarda, verificação 10). Uma pessoa confere na plataforma e escolhe:

* ``PUBLICADO``: a execução vira ``sucesso`` (código ``resolvido_publicado``, id externo
  opcional em formato fechado). Continua bloqueando para sempre (RF-12); a pós-publicação da
  próxima rodada em que o post estiver ``confirmado`` conta a plataforma como publicada.
* ``NAO_PUBLICADO``: a execução vira ``falha`` (código ``resolvido_nao_publicado``). Para de
  bloquear, mas conta como falha definitiva da confirmação a que pertence: com a MESMA
  confirmação não há nova tentativa; só uma NOVA confirmação (pela tela, com selo novo)
  publica essa plataforma de novo, e ainda assim só se a guarda permitir (janela, selo...).

Regras invioláveis:

* I-2: a resolução NUNCA habilita publicar por si só. Ela só escreve no banco Execuções; não
  toca no post, no estágio, no selo nem em "Confirmado em". Quem decide é a guarda.
* I-1: o registro guarda só o código da resolução, o instante e o responsável. O responsável é
  um nome curto em formato fechado (letras, espaço, apóstrofo, ponto e hífen; 2 a 40
  caracteres; sem dígitos nem sublinhado, o que já exclui tokens) e é recusado se parecer
  credencial (palavras como "bearer", "token", "senha" ou valor que o filtro de redação
  mascararia). O nome não vai a log nem ao resultado; mensagens de erro nunca ecoam a entrada.
* Entradas validadas ANTES de qualquer leitura ou escrita: ``post_id`` (hex/UUID), plataforma
  (formato fechado), desfecho (enum), ``agora`` com fuso (relógio injetado) e id externo.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Final

from publicador.core.modelos import EstadoExecucao
from publicador.core.redacao import obter_logger, redigir
from publicador.job.execucoes import (
    CodigoMotivo,
    RegistroExecucao,
    execucoes_do_post,
    registrar_resolucao,
)
from publicador.notion.bancos import IdsBancos
from publicador.notion.cliente import ClienteNotion

__all__ = [
    "DesfechoManual",
    "ResultadoResolucao",
    "SituacaoResolucao",
    "resolver_incerto",
    "validar_responsavel",
]

_log = obter_logger("publicador.job.resolucao")

_RE_ID: Final = re.compile(r"[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
_RE_PLATAFORMA: Final = re.compile(r"[A-Za-z0-9_\-]{1,40}")
_RE_ID_EXTERNO: Final = re.compile(r"[A-Za-z0-9_.:\-]{1,128}")
# Letras (inclusive acentuadas), separadas por espaço, apóstrofo, ponto ou hífen isolados.
_RE_RESPONSAVEL: Final = re.compile(r"[^\W\d_]+(?:[ '.\-][^\W\d_]+)*\.?")
_RESPONSAVEL_MIN: Final = 2
_RESPONSAVEL_MAX: Final = 40
_PALAVRAS_DE_CREDENCIAL: Final = frozenset(
    {
        "bearer",
        "basic",
        "token",
        "senha",
        "password",
        "passwd",
        "secret",
        "segredo",
        "apikey",
        "credencial",
        "credential",
        "cookie",
    }
)
_A_RESOLVER: Final = frozenset({EstadoExecucao.INCERTO, EstadoExecucao.INICIADA})


class DesfechoManual(Enum):
    """O que a pessoa constatou na plataforma."""

    PUBLICADO = "publicado"
    NAO_PUBLICADO = "nao_publicado"


class SituacaoResolucao(Enum):
    RESOLVIDO = "resolvido"
    NADA_A_RESOLVER = "nada_a_resolver"
    ERRO_LEITURA = "erro_leitura"
    ERRO_REGISTRO = "erro_registro"


@dataclass(frozen=True)
class ResultadoResolucao:
    """Desfecho da resolução. Só ids, plataforma e enums (I-1): nunca o responsável."""

    post_id: str
    plataforma: str
    situacao: SituacaoResolucao
    desfecho: DesfechoManual
    paginas_resolvidas: tuple[str, ...] = ()


_DE_PARA: Final[dict[DesfechoManual, tuple[EstadoExecucao, CodigoMotivo]]] = {
    DesfechoManual.PUBLICADO: (EstadoExecucao.SUCESSO, CodigoMotivo.RESOLVIDO_PUBLICADO),
    DesfechoManual.NAO_PUBLICADO: (EstadoExecucao.FALHA, CodigoMotivo.RESOLVIDO_NAO_PUBLICADO),
}


def validar_responsavel(valor: object) -> str:
    """Nome curto de quem resolveu, normalizado (NFC, espaços aparados). ``ValueError`` com
    mensagem fixa (sem ecoar a entrada) se estiver fora do formato ou parecer credencial."""
    erro = "responsável inválido: use um nome curto (letras e espaços), nunca uma credencial"
    if not isinstance(valor, str):
        raise ValueError(erro)
    nome = unicodedata.normalize("NFC", valor).strip()
    if not _RESPONSAVEL_MIN <= len(nome) <= _RESPONSAVEL_MAX:
        raise ValueError(erro)
    if _RE_RESPONSAVEL.fullmatch(nome) is None:
        raise ValueError(erro)
    palavras = {p.casefold() for p in re.split(r"[ '.\-]+", nome) if p}
    if palavras & _PALAVRAS_DE_CREDENCIAL or redigir(nome) != nome:
        raise ValueError(erro)
    return nome


def _validar(
    post_id: object,
    plataforma: object,
    desfecho: object,
    agora: object,
    id_externo: object,
) -> str:
    if not isinstance(post_id, str) or _RE_ID.fullmatch(post_id) is None:
        raise ValueError("post_id inválido (esperado id hex/UUID)")
    if not isinstance(plataforma, str) or _RE_PLATAFORMA.fullmatch(plataforma) is None:
        raise ValueError("plataforma inválida (formato fechado)")
    if not isinstance(desfecho, DesfechoManual):
        raise ValueError("desfecho inválido (use DesfechoManual)")
    if not isinstance(agora, datetime) or agora.tzinfo is None or agora.utcoffset() is None:
        raise ValueError("agora deve ser datetime com fuso horário")
    if id_externo is not None:
        if desfecho is not DesfechoManual.PUBLICADO:
            raise ValueError("identificador externo só quando o post foi publicado")
        if not isinstance(id_externo, str) or _RE_ID_EXTERNO.fullmatch(id_externo) is None:
            raise ValueError("identificador externo inválido (formato fechado)")
    return post_id.replace("-", "").lower()


def resolver_incerto(
    cliente: ClienteNotion,
    ids: IdsBancos,
    *,
    post_id: str,
    plataforma: str,
    desfecho: DesfechoManual,
    responsavel: str,
    agora: datetime,
    id_externo: str | None = None,
) -> ResultadoResolucao:
    """Resolve TODAS as execuções incertas (ou iniciadas sem resultado) do post na plataforma,
    de qualquer data (RF-38), com o desfecho escolhido pela pessoa.

    Levanta ``ValueError`` (mensagem fixa) para entrada inválida, antes de qualquer I/O. Erros
    do Notion não levantam: voltam em ``situacao``. Escrita parcial (várias execuções e falha
    no meio) devolve ``ERRO_REGISTRO`` com as páginas já resolvidas; repetir é seguro, porque
    só as que seguem incertas são tocadas.
    """
    pid = _validar(post_id, plataforma, desfecho, agora, id_externo)
    nome = validar_responsavel(responsavel)
    estado, motivo = _DE_PARA[desfecho]

    def resultado(situacao: SituacaoResolucao, paginas: tuple[str, ...] = ()) -> ResultadoResolucao:
        _log.info(
            "resolução: post %s plataforma %s: %s (%s)",
            pid,
            plataforma,
            situacao.value,
            desfecho.value,
        )
        return ResultadoResolucao(pid, plataforma, situacao, desfecho, paginas)

    try:
        pendentes: list[RegistroExecucao] = [
            r
            for r in execucoes_do_post(cliente, ids, pid, plataforma)
            if r.execucao.estado in _A_RESOLVER
        ]
    except Exception as erro:  # noqa: BLE001 - sem leitura confiável, nada é escrito
        _log.error("resolução: leitura falhou; post %s (%s)", pid, type(erro).__name__)
        return resultado(SituacaoResolucao.ERRO_LEITURA)
    if not pendentes:
        return resultado(SituacaoResolucao.NADA_A_RESOLVER)

    texto = f"{motivo.value} por {nome} em {agora.isoformat()}"
    feitas: list[str] = []
    for registro in pendentes:
        try:
            registrar_resolucao(cliente, registro, estado, motivo, id_externo, texto, agora)
        except Exception as erro:  # noqa: BLE001 - a execução segue incerta (bloqueando)
            _log.error(
                "resolução: registro não gravado; post %s plataforma %s (%s)",
                pid,
                plataforma,
                type(erro).__name__,
            )
            return resultado(SituacaoResolucao.ERRO_REGISTRO, tuple(feitas))
        feitas.append(registro.pagina_id)
    return resultado(SituacaoResolucao.RESOLVIDO, tuple(feitas))
