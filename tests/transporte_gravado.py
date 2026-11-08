"""Transporte HTTP falso para testes de adaptadores: respostas gravadas em fila, sem rede.

Cada chamada (GET, POST ou DELETE) consome o próximo item da fila: ``(status, corpo)`` é
devolvido, uma exceção é levantada. Fila vazia = falha do teste (chamada inesperada).
Registra metodo, url, cabeçalhos, corpo e timeout de cada chamada. Reaproveitável pelas
tarefas T044 (imagens/carrossel) e T046.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass

Resposta = tuple[int, bytes] | BaseException


@dataclass(frozen=True)
class ChamadaHttp:
    metodo: str
    url: str
    cabecalhos: dict[str, str]
    corpo: bytes | None
    timeout: float


class TransporteGravado:
    def __init__(self, *respostas: Resposta) -> None:
        self._fila: deque[Resposta] = deque(respostas)
        self.chamadas: list[ChamadaHttp] = []

    def _responder(
        self,
        metodo: str,
        url: str,
        cabecalhos: Mapping[str, str],
        corpo: bytes | None,
        timeout: float,
    ) -> tuple[int, bytes]:
        self.chamadas.append(ChamadaHttp(metodo, url, dict(cabecalhos), corpo, timeout))
        if not self._fila:
            raise AssertionError(f"chamada inesperada: {metodo} (fila de respostas vazia)")
        item = self._fila.popleft()
        if isinstance(item, BaseException):
            raise item
        return item

    def obter(self, url: str, cabecalhos: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
        return self._responder("GET", url, cabecalhos, None, timeout)

    def postar(
        self, url: str, cabecalhos: Mapping[str, str], corpo: bytes, timeout: float
    ) -> tuple[int, bytes]:
        return self._responder("POST", url, cabecalhos, corpo, timeout)

    def apagar(self, url: str, cabecalhos: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
        return self._responder("DELETE", url, cabecalhos, None, timeout)

    @property
    def restantes(self) -> int:
        return len(self._fila)

    def metodos(self) -> list[str]:
        return [c.metodo for c in self.chamadas]
