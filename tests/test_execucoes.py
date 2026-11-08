"""Testes do registro de execuções (publicador.job.execucoes). Valores FICTÍCIOS, sem rede."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from publicador.core.modelos import EstadoExecucao, MotivoRecusa
from publicador.core.redacao import SegredoStr
from publicador.job.execucoes import (
    CodigoMotivo,
    ErroRegistroExecucao,
    RegistroExecucao,
    buscar_por_chave,
    chave_de,
    execucoes_do_post,
    iniciar_execucao,
    registrar_recusa,
    registrar_resultado,
    sinal_de_vida,
)
from publicador.notion.cliente import (
    ClienteNotion,
    ErroNotionIncerto,
    ErroTransporte,
    RespostaHttp,
)

TOKEN = "TOKEN_FALSO_abc123XYZ789"
BANCO = "e882e4987273430b8fe7002ce2811fa4"
POST = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
OUTRO_POST = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
TITULO_POST = "Titulo secreto do post XPTO"
CORPO_POST = "Corpo secreto do post com legenda ficticia ZZZ"
SELO = "f" * 64
AGORA = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
DEPOIS = datetime(2026, 11, 7, 12, 3, tzinfo=UTC)


@dataclass(frozen=True)
class Ids:
    execucoes: str = BANCO


IDS = Ids()


@dataclass
class Chamada:
    metodo: str
    url: str
    corpo: bytes | None

    @property
    def json(self) -> dict[str, Any]:
        assert self.corpo is not None
        dados = json.loads(self.corpo)
        assert isinstance(dados, dict)
        return dados


@dataclass
class NotionFalso:
    """Notion falso com estado: guarda páginas do banco Execuções e entende filtros."""

    paginas: list[dict[str, Any]] = field(default_factory=list)
    chamadas: list[Chamada] = field(default_factory=list)
    falhas_na_criacao: list[Exception | int] = field(default_factory=list)
    tamanho_pagina: int = 100

    def enviar(
        self,
        metodo: str,
        url: str,
        cabecalhos: Mapping[str, str],
        corpo: bytes | None,
        timeout: float,
    ) -> RespostaHttp:
        self.chamadas.append(Chamada(metodo, url, corpo))
        dados = json.loads(corpo) if corpo else {}
        if metodo == "POST" and url.endswith("/v1/pages"):
            return self._criar(dados)
        if metodo == "PATCH" and "/v1/pages/" in url:
            return self._atualizar(url.rsplit("/", 1)[1], dados)
        if metodo == "POST" and url.endswith("/query"):
            return self._consultar(dados)
        return resp(404, {"code": "object_not_found"})

    def _criar(self, dados: dict[str, Any]) -> RespostaHttp:
        if self.falhas_na_criacao:
            falha = self.falhas_na_criacao.pop(0)
            if isinstance(falha, Exception):
                raise falha
            return resp(falha, {"code": "internal_server_error"})
        novo = {"id": f"{len(self.paginas) + 1:032x}", "properties": dados["properties"]}
        self.paginas.append(novo)
        return resp(200, novo)

    def _atualizar(self, pagina_id: str, dados: dict[str, Any]) -> RespostaHttp:
        for p in self.paginas:
            if p["id"] == pagina_id:
                p["properties"].update(dados["properties"])
                return resp(200, p)
        return resp(404, {"code": "object_not_found"})

    def _consultar(self, dados: dict[str, Any]) -> RespostaHttp:
        achadas = [p for p in self.paginas if _casa(p, dados.get("filter"))]
        inicio = int(dados.get("start_cursor", 0))
        fim = inicio + min(int(dados.get("page_size", 100)), self.tamanho_pagina)
        mais = fim < len(achadas)
        return resp(
            200,
            {"results": achadas[inicio:fim], "has_more": mais, "next_cursor": str(fim)},
        )


def _casa(pagina: dict[str, Any], filtro: dict[str, Any] | None) -> bool:
    if filtro is None:
        return True
    if "and" in filtro:
        return all(_casa(pagina, f) for f in filtro["and"])
    props = pagina["properties"]
    p = props.get(filtro["property"])
    if p is None:
        return False
    if "title" in filtro:
        texto = "".join(t["text"]["content"] for t in p["title"])
        return bool(texto == filtro["title"]["equals"])
    if "select" in filtro:
        return bool((p["select"] or {}).get("name") == filtro["select"]["equals"])
    if "relation" in filtro:
        return any(r["id"] == filtro["relation"]["contains"] for r in p["relation"])
    raise AssertionError("filtro não suportado pelo Notion falso")


def resp(status: int, dados: object) -> RespostaHttp:
    return RespostaHttp(status, {}, json.dumps(dados).encode())


def montar(notion: NotionFalso | None = None) -> tuple[ClienteNotion, NotionFalso]:
    notion = notion if notion is not None else NotionFalso()
    cliente = ClienteNotion(SegredoStr(TOKEN), transporte=notion, dormir=lambda _: None)
    return cliente, notion


def chave(selo: str = SELO, post: str = POST, plataforma: str = "Instagram") -> str:
    return chave_de(post, plataforma, selo)


def iniciar(cliente: ClienteNotion, **kw: Any) -> RegistroExecucao:
    args: dict[str, Any] = {
        "post_id": POST,
        "plataforma": "Instagram",
        "chave_idempotencia": chave(),
        "tentativa": 1,
        "agora": AGORA,
    }
    args.update(kw)
    return iniciar_execucao(cliente, IDS, **args)


# ------------------------------------------------------------------ chave_de


def test_chave_deterministica_e_sem_expor_selo() -> None:
    assert chave() == chave()
    assert SELO not in chave()
    assert re.fullmatch(r"v1-[0-9a-f]{32}", chave())


def test_chave_muda_com_qualquer_componente() -> None:
    base = chave()
    assert chave(selo="e" * 64) != base
    assert chave(post=OUTRO_POST) != base
    assert chave(plataforma="TikTok") != base


def test_chave_separador_inequivoco() -> None:
    assert chave_de("ab", "c", "d") != chave_de("a", "bc", "d")
    assert chave_de("a", "b|c", "d") != chave_de("a|b", "c", "d")


@pytest.mark.parametrize(
    "args", [("", "Instagram", SELO), (POST, " ", SELO), (POST, "Instagram", "")]
)
def test_chave_rejeita_vazios(args: tuple[str, str, str]) -> None:
    with pytest.raises(ValueError):
        chave_de(*args)


# ------------------------------------------------------------------ iniciar


def test_iniciar_grava_iniciada_com_chave_tentativa_e_instante() -> None:
    cliente, notion = montar()
    reg = iniciar(cliente)
    assert len(notion.paginas) == 1
    props = notion.paginas[0]["properties"]
    assert props["Chave de idempotência"]["title"][0]["text"]["content"] == chave()
    assert props["Post"] == {"relation": [{"id": POST}]}
    assert props["Plataforma"] == {"select": {"name": "Instagram"}}
    assert props["Tentativa"] == {"select": {"name": "1"}}
    assert props["Início"]["date"]["start"].startswith("2026-11-07T12:00:00")
    assert "Resultado" not in props or props["Resultado"] == {"select": None}
    assert "Fim" not in props
    assert reg.pagina_id == notion.paginas[0]["id"]
    assert reg.execucao.estado is EstadoExecucao.INICIADA
    assert reg.execucao.chave_idempotencia == chave()
    assert reg.execucao.tentativa == 1
    assert reg.execucao.iniciada_em == AGORA
    assert notion.chamadas[0].json["parent"] == {"database_id": BANCO}


@pytest.mark.parametrize("tentativa", [0, 3, -1])
def test_iniciar_rejeita_tentativa_fora_do_esquema(tentativa: int) -> None:
    cliente, notion = montar()
    with pytest.raises(ValueError):
        iniciar(cliente, tentativa=tentativa)
    assert notion.chamadas == []


def test_iniciar_rejeita_datetime_ingenuo() -> None:
    cliente, notion = montar()
    with pytest.raises(ValueError):
        iniciar(cliente, agora=datetime(2026, 11, 7, 12, 0))
    assert notion.chamadas == []


@pytest.mark.parametrize(
    "campo,valor",
    [
        ("chave_idempotencia", TITULO_POST),
        ("chave_idempotencia", "chave com espaço"),
        ("plataforma", CORPO_POST),
        ("post_id", "não é id"),
        ("post_id", TITULO_POST),
    ],
)
def test_iniciar_rejeita_campos_livres(campo: str, valor: str) -> None:
    cliente, notion = montar()
    with pytest.raises(ValueError):
        iniciar(cliente, **{campo: valor})
    assert notion.chamadas == []


def test_erro_incerto_na_criacao_nao_e_retentado() -> None:
    notion = NotionFalso(falhas_na_criacao=[500])
    cliente, _ = montar(notion)
    with pytest.raises(ErroNotionIncerto):
        iniciar(cliente)
    criacoes = [c for c in notion.chamadas if c.metodo == "POST" and c.url.endswith("/pages")]
    assert len(criacoes) == 1
    assert len(notion.chamadas) == 1


def test_falha_de_transporte_na_criacao_nao_e_retentada() -> None:
    notion = NotionFalso(falhas_na_criacao=[ErroTransporte("x")])
    cliente, _ = montar(notion)
    with pytest.raises(ErroNotionIncerto):
        iniciar(cliente)
    assert len(notion.chamadas) == 1


def test_resposta_sem_id_e_falha_segura() -> None:
    class SemId:
        def enviar(self, *a: Any, **k: Any) -> RespostaHttp:
            return resp(200, {"properties": {}})

    cliente = ClienteNotion(SegredoStr(TOKEN), transporte=SemId(), dormir=lambda _: None)
    with pytest.raises(ErroRegistroExecucao):
        iniciar(cliente)


# ------------------------------------------------------------------ resultado


def test_registrar_resultado_atualiza_a_mesma_pagina() -> None:
    cliente, notion = montar()
    reg = iniciar(cliente)
    novo = registrar_resultado(
        cliente, reg, EstadoExecucao.SUCESSO, None, "17890000000000001", DEPOIS
    )
    assert len(notion.paginas) == 1
    props = notion.paginas[0]["properties"]
    assert props["Resultado"] == {"select": {"name": "sucesso"}}
    assert props["Fim"]["date"]["start"].startswith("2026-11-07T12:03:00")
    assert props["ID do post na plataforma"]["rich_text"][0]["text"]["content"] == (
        "17890000000000001"
    )
    patch = notion.chamadas[-1]
    assert patch.metodo == "PATCH" and patch.url.endswith(reg.pagina_id)
    assert "Chave de idempotência" not in patch.json["properties"]
    assert novo.pagina_id == reg.pagina_id
    assert novo.execucao.estado is EstadoExecucao.SUCESSO
    assert novo.execucao.finalizada_em == DEPOIS
    assert novo.id_externo == "17890000000000001"


@pytest.mark.parametrize(
    "estado,nome",
    [
        (EstadoExecucao.SUCESSO, "sucesso"),
        (EstadoExecucao.SUCESSO_MODO_PRIVADO, "sucesso em modo privado"),
        (EstadoExecucao.FALHA, "falha"),
        (EstadoExecucao.RECUSADA, "recusado"),
        (EstadoExecucao.INCERTO, "incerto"),
        (EstadoExecucao.SIMULADA, "simulado"),
        (EstadoExecucao.NAO_EXECUTADO, "não executado"),
    ],
)
def test_todos_os_resultados_mapeiam_para_nomes_reais(estado: EstadoExecucao, nome: str) -> None:
    cliente, notion = montar()
    reg = iniciar(cliente)
    registrar_resultado(cliente, reg, estado, CodigoMotivo.ERRO_TRANSITORIO, None, DEPOIS)
    assert notion.paginas[0]["properties"]["Resultado"] == {"select": {"name": nome}}
    assert (
        notion.paginas[0]["properties"]["Código do motivo"]["rich_text"][0]["text"]["content"]
        == "erro_transitorio"
    )


def test_motivo_de_recusa_da_guarda_e_aceito() -> None:
    cliente, notion = montar()
    reg = iniciar(cliente)
    registrar_resultado(
        cliente, reg, EstadoExecucao.RECUSADA, MotivoRecusa.SELO_DIVERGENTE, None, DEPOIS
    )
    props = notion.paginas[0]["properties"]
    assert props["Código do motivo"]["rich_text"][0]["text"]["content"] == "selo_divergente"


def test_motivo_livre_e_recusado() -> None:
    cliente, notion = montar()
    reg = iniciar(cliente)
    antes = len(notion.chamadas)
    for livre in (CORPO_POST, "selo_divergente", "texto livre qualquer"):
        with pytest.raises(ValueError):
            registrar_resultado(
                cliente,
                reg,
                EstadoExecucao.FALHA,
                livre,  # type: ignore[arg-type]
                None,
                DEPOIS,
            )
    assert len(notion.chamadas) == antes


def test_id_externo_livre_e_recusado() -> None:
    cliente, notion = montar()
    reg = iniciar(cliente)
    antes = len(notion.chamadas)
    with pytest.raises(ValueError):
        registrar_resultado(cliente, reg, EstadoExecucao.SUCESSO, None, CORPO_POST, DEPOIS)
    assert len(notion.chamadas) == antes


def test_resultado_iniciada_e_rejeitado_e_nao_reaplica_em_terminal() -> None:
    cliente, notion = montar()
    reg = iniciar(cliente)
    with pytest.raises(ValueError):
        registrar_resultado(cliente, reg, EstadoExecucao.INICIADA, None, None, DEPOIS)
    final = registrar_resultado(cliente, reg, EstadoExecucao.FALHA, None, None, DEPOIS)
    antes = len(notion.chamadas)
    with pytest.raises(ValueError):
        registrar_resultado(cliente, final, EstadoExecucao.SUCESSO, None, None, DEPOIS)
    assert len(notion.chamadas) == antes


# ------------------------------------------------------------------ sinal de vida


def test_sinal_de_vida_cria_registro_proprio_e_depois_so_atualiza_o_instante() -> None:
    cliente, notion = montar()
    pagina = sinal_de_vida(cliente, IDS, AGORA)
    props = notion.paginas[0]["properties"]
    assert props["Resultado"] == {"select": {"name": "sinal de vida"}}
    assert "Post" not in props
    assert sinal_de_vida(cliente, IDS, DEPOIS, pagina_id=pagina) == pagina
    patch = notion.chamadas[-1]
    assert patch.metodo == "PATCH"
    assert set(patch.json["properties"]) == {"Fim"}
    assert len(notion.paginas) == 1
    assert notion.paginas[0]["properties"]["Fim"]["date"]["start"].startswith("2026-11-07T12:03")


def test_sinal_de_vida_nao_aparece_nas_leituras() -> None:
    cliente, _ = montar()
    sinal_de_vida(cliente, IDS, AGORA)
    reg = iniciar(cliente)
    assert buscar_por_chave(cliente, IDS, chave()) == [reg]
    assert execucoes_do_post(cliente, IDS, POST, "Instagram") == [reg]


# ------------------------------------------------------------------ leitura


def test_buscar_por_chave_encontra_e_devolve_vazio_se_nao_existe() -> None:
    cliente, _ = montar()
    reg = iniciar(cliente)
    iniciar(cliente, post_id=OUTRO_POST, chave_idempotencia=chave(post=OUTRO_POST))
    assert buscar_por_chave(cliente, IDS, chave()) == [reg]
    assert buscar_por_chave(cliente, IDS, chave(selo="0" * 64)) == []


def test_leitura_reflete_resultado_gravado() -> None:
    cliente, _ = montar()
    reg = iniciar(cliente)
    final = registrar_resultado(
        cliente, reg, EstadoExecucao.INCERTO, CodigoMotivo.NOTION_INCERTO, None, DEPOIS
    )
    [lido] = buscar_por_chave(cliente, IDS, chave())
    assert lido == final
    assert lido.execucao.estado is EstadoExecucao.INCERTO
    assert lido.execucao.motivo == "notion_incerto"


def test_duas_tentativas_com_mesma_chave_voltam_juntas() -> None:
    cliente, _ = montar()
    iniciar(cliente, tentativa=1)
    iniciar(cliente, tentativa=2, agora=DEPOIS)
    achadas = buscar_por_chave(cliente, IDS, chave())
    assert sorted(r.execucao.tentativa for r in achadas) == [1, 2]


def test_execucoes_do_post_filtra_por_post_e_plataforma_independente_da_chave() -> None:
    cliente, _ = montar()
    a = iniciar(cliente)
    iniciar(cliente, plataforma="TikTok", chave_idempotencia=chave(plataforma="TikTok"))
    iniciar(cliente, post_id=OUTRO_POST, chave_idempotencia=chave(post=OUTRO_POST))
    b = iniciar(cliente, chave_idempotencia=chave(selo="1" * 64), tentativa=2)
    achadas = execucoes_do_post(cliente, IDS, POST, "Instagram")
    assert {r.pagina_id for r in achadas} == {a.pagina_id, b.pagina_id}


def test_leitura_paginada_completa_com_mais_de_100() -> None:
    notion = NotionFalso()
    cliente, _ = montar(notion)
    for i in range(230):
        notion.paginas.append(
            {
                "id": f"{i + 1000:032x}",
                "properties": {
                    "Chave de idempotência": {
                        "title": [{"type": "text", "text": {"content": chave()}}]
                    },
                    "Post": {"relation": [{"id": POST}]},
                    "Plataforma": {"select": {"name": "Instagram"}},
                    "Tentativa": {"select": {"name": "1"}},
                    "Resultado": {"select": {"name": "falha"}},
                    "Início": {"date": {"start": "2026-11-07T12:00:00.000+00:00"}},
                    "Fim": {"date": {"start": "2026-11-07T12:01:00.000+00:00"}},
                },
            }
        )
    assert len(buscar_por_chave(cliente, IDS, chave())) == 230
    consultas = [c for c in notion.chamadas if c.url.endswith("/query")]
    assert len(consultas) == 3


def test_linha_ilegivel_e_falha_segura_nao_ignorada() -> None:
    notion = NotionFalso()
    cliente, _ = montar(notion)
    notion.paginas.append(
        {
            "id": "c" * 32,
            "properties": {
                "Chave de idempotência": {
                    "title": [{"type": "text", "text": {"content": chave()}}]
                },
                "Post": {"relation": []},
            },
        }
    )
    with pytest.raises(ErroRegistroExecucao):
        buscar_por_chave(cliente, IDS, chave())


def test_atrasado_vira_nao_executado_na_leitura() -> None:
    notion = NotionFalso()
    cliente, _ = montar(notion)
    reg = iniciar(cliente)
    notion.paginas[0]["properties"]["Resultado"] = {"select": {"name": "atrasado"}}
    [lido] = buscar_por_chave(cliente, IDS, chave())
    assert lido.pagina_id == reg.pagina_id
    assert lido.execucao.estado is EstadoExecucao.NAO_EXECUTADO


# ------------------------------------------------------------------ I-1


def test_nenhuma_chamada_contem_texto_do_post_nem_token() -> None:
    cliente, notion = montar()
    reg = iniciar(cliente)
    sinal_de_vida(cliente, IDS, AGORA)
    registrar_resultado(cliente, reg, EstadoExecucao.SUCESSO, None, "abc123", DEPOIS)
    buscar_por_chave(cliente, IDS, chave())
    execucoes_do_post(cliente, IDS, POST, "Instagram")
    assert len(notion.chamadas) >= 5
    for c in notion.chamadas:
        bruto = (c.corpo or b"").decode() + c.url
        for proibido in (TITULO_POST, CORPO_POST, TOKEN, "Bearer", SELO):
            assert proibido not in bruto
    guardado = json.dumps(notion.paginas)
    assert TITULO_POST not in guardado and CORPO_POST not in guardado and TOKEN not in guardado


def test_propriedades_gravadas_sao_so_as_permitidas() -> None:
    cliente, notion = montar()
    reg = iniciar(cliente)
    registrar_resultado(
        cliente, reg, EstadoExecucao.FALHA, CodigoMotivo.ERRO_PERMANENTE, "x1", DEPOIS
    )
    permitidas = {
        "Chave de idempotência",
        "Post",
        "Plataforma",
        "Tentativa",
        "Resultado",
        "Código do motivo",
        "Início",
        "Fim",
        "ID do post na plataforma",
    }
    assert set(notion.paginas[0]["properties"]) <= permitidas
    assert "Texto do motivo" not in notion.paginas[0]["properties"]


# ------------------------------------------------------------------ recusa de escrita única (T039)


def _recusar(cliente: ClienteNotion, **kw: Any) -> RegistroExecucao:
    args: dict[str, Any] = {
        "post_id": POST,
        "plataforma": "Instagram",
        "chave_idempotencia": chave(),
        "tentativa": 1,
        "motivo": MotivoRecusa.SELO_DIVERGENTE,
        "agora": AGORA,
    }
    args.update(kw)
    return registrar_recusa(cliente, IDS, **args)


def test_registrar_recusa_faz_uma_unica_criacao_ja_concluida() -> None:
    cliente, notion = montar()
    reg = _recusar(cliente)
    assert [(c.metodo, c.url.rsplit("/", 1)[1]) for c in notion.chamadas] == [("POST", "pages")]
    props = notion.paginas[0]["properties"]
    assert props["Resultado"] == {"select": {"name": "recusado"}}
    assert props["Código do motivo"]["rich_text"][0]["text"]["content"] == "selo_divergente"
    assert props["Fim"]["date"]["start"].startswith("2026-11-07T12:00:00")
    assert reg.execucao.estado is EstadoExecucao.RECUSADA
    assert reg.execucao.finalizada_em == AGORA and reg.execucao.motivo == "selo_divergente"
    # Lida de volta, a execução já nasce recusada: nunca passou por "iniciada".
    achadas = buscar_por_chave(cliente, IDS, chave())
    assert [r.execucao.estado for r in achadas] == [EstadoExecucao.RECUSADA]


def test_registrar_recusa_nao_executado() -> None:
    cliente, notion = montar()
    reg = _recusar(
        cliente, motivo=CodigoMotivo.ERRO_DESCONHECIDO, resultado=EstadoExecucao.NAO_EXECUTADO
    )
    assert notion.paginas[0]["properties"]["Resultado"] == {"select": {"name": "não executado"}}
    assert reg.execucao.estado is EstadoExecucao.NAO_EXECUTADO
    assert len(notion.chamadas) == 1


@pytest.mark.parametrize("falha", [500, ErroTransporte("x")])
def test_registrar_recusa_com_erro_incerto_nao_retenta_e_nao_deixa_iniciada(
    falha: Exception | int,
) -> None:
    notion = NotionFalso(falhas_na_criacao=[falha])
    cliente, _ = montar(notion)
    with pytest.raises(ErroNotionIncerto):
        _recusar(cliente)
    assert len(notion.chamadas) == 1  # uma tentativa só, sem PATCH
    assert notion.paginas == []  # nada órfão (nem "iniciada")


@pytest.mark.parametrize(
    "mudanca",
    [
        {"resultado": EstadoExecucao.SUCESSO},
        {"resultado": EstadoExecucao.INICIADA},
        {"motivo": "texto livre do usuario"},
        {"motivo": None},
        {"tentativa": 3},
        {"agora": datetime(2026, 11, 7, 12, 0)},
        {"plataforma": "Insta gram!"},
        {"post_id": "nao-e-id"},
    ],
)
def test_registrar_recusa_valida_antes_de_escrever(mudanca: dict[str, Any]) -> None:
    cliente, notion = montar()
    with pytest.raises(ValueError):
        _recusar(cliente, **mudanca)
    assert notion.chamadas == []


def test_registrar_recusa_resposta_sem_id_e_falha_segura() -> None:
    class SemId:
        def enviar(self, *a: Any, **k: Any) -> RespostaHttp:
            return resp(200, {"properties": {}})

    cliente = ClienteNotion(SegredoStr(TOKEN), transporte=SemId(), dormir=lambda _: None)
    with pytest.raises(ErroRegistroExecucao):
        _recusar(cliente)
