"""Testes do selo de confirmação (publicador.core.selo) - regra I-2 / RF-26.

O selo prova que NADA do que será publicado mudou depois da confirmação. Por isso
é DETERMINÍSTICO, SENSÍVEL a todo campo publicável e INSENSÍVEL ao que não é
publicável. Nenhum teste acessa a rede; todos os valores são fictícios (I-1).
"""

from __future__ import annotations

import dataclasses
import random
import unicodedata
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from publicador.artes.armazem import ArmazemLocal, ArteNaoEncontrada, CaminhoDeArteInvalido
from publicador.core.modelos import Estagio, PapelEstagio, Post
from publicador.core.selo import SeloImpossivel, calcular_selo, verificar_selo
from publicador.notion.variacoes import VariacaoInvalida

SAO_PAULO = timezone(timedelta(hours=-3))
INSTANTE = datetime(2026, 11, 7, 18, 30, tzinfo=SAO_PAULO)

ARTE_1 = "serie-a/2026/11/arte_01.png"
ARTE_2 = "serie-a/2026/11/arte_02.png"
ARTE_IG = "serie-a/2026/11/arte_ig.png"
CONTEUDOS: dict[str, bytes] = {
    ARTE_1: b"\x89PNG conteudo ficticio 01",
    ARTE_2: b"\x89PNG conteudo ficticio 02",
    ARTE_IG: b"\x89PNG conteudo ficticio so instagram",
}

# Vetor de resposta conhecida: se mudar, o algoritmo do selo mudou (todo selo
# gravado deixaria de bater). Mudança intencional exige nova versão ("v2:").
SELO_ESPERADO_GOLDEN = "v1:681aa8f9f029f177a53dba75c4b612a556bdd078262d4a3cc23634534ee39669"


@pytest.fixture
def armazem(tmp_path: Path) -> ArmazemLocal:
    raiz = tmp_path / "artes"
    for caminho, conteudo in CONTEUDOS.items():
        destino = raiz / caminho
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(conteudo)
    return ArmazemLocal(raiz)


def _post(**mudancas: Any) -> Post:
    base: dict[str, Any] = {
        "id": "post-ficticio-0001",
        "titulo": "Quanto custa a energia?",
        "corpo": "Linha 1 do roteiro.\nLinha 2 com acentuação: ação.",
        "estagio": Estagio("confirmado", PapelEstagio.AUTORIZADO),
        "plataformas": ("Instagram", "TikTok"),
        "formato": "Feed",
        "serie": "Série fictícia",
        "ordem_na_serie": 3,
        "tema": "Energia",
        "publicar_em": INSTANTE,
        "artes": (ARTE_1, ARTE_2, f"instagram:{ARTE_IG}"),
        "selo": None,
        "confirmado_em": datetime(2026, 11, 6, 10, 0, tzinfo=UTC),
        "editado_em": datetime(2026, 11, 6, 9, 0, tzinfo=UTC),
        "secoes_por_plataforma": {"Instagram": "Legenda do Instagram.", "TikTok": "Texto TikTok."},
        "somente_data": False,
    }
    base.update(mudancas)
    return Post(**base)


# --------------------------------------------------------------------------- determinismo


def test_mesma_entrada_mesmo_selo(armazem: ArmazemLocal) -> None:
    selos = {calcular_selo(_post(), armazem) for _ in range(5)}
    assert len(selos) == 1
    (selo,) = selos
    assert selo.startswith("v1:")
    assert len(selo) == 3 + 64
    assert selo[3:] == selo[3:].lower()
    int(selo[3:], 16)


def test_ordem_de_insercao_das_secoes_nao_importa(armazem: ArmazemLocal) -> None:
    a = _post(secoes_por_plataforma={"Instagram": "IG", "TikTok": "TT"})
    b = _post(secoes_por_plataforma={"TikTok": "TT", "Instagram": "IG"})
    assert calcular_selo(a, armazem) == calcular_selo(b, armazem)


def test_caixa_da_chave_da_secao_nao_importa(armazem: ArmazemLocal) -> None:
    a = _post(secoes_por_plataforma={"Instagram": "IG"})
    b = _post(secoes_por_plataforma={"INSTAGRAM": "IG"})
    assert calcular_selo(a, armazem) == calcular_selo(b, armazem)


def test_vetor_de_resposta_conhecida(armazem: ArmazemLocal) -> None:
    assert calcular_selo(_post(), armazem) == SELO_ESPERADO_GOLDEN


# --------------------------------------------------------------------------- sensibilidade


@pytest.mark.parametrize(
    ("campo", "valor"),
    [
        ("titulo", "Quanto custa a energia!"),
        ("corpo", "Linha 1 do roteiro.\nLinha 2 com acentuação: açao."),
        (
            "secoes_por_plataforma",
            {"Instagram": "Legenda do Instagram!", "TikTok": "Texto TikTok."},
        ),
        ("plataformas", ("Instagram",)),
        ("formato", "Reels"),
        ("formato", None),
        ("publicar_em", INSTANTE + timedelta(days=1)),
        ("publicar_em", INSTANTE + timedelta(minutes=1)),
        ("publicar_em", INSTANTE + timedelta(seconds=1)),
        ("publicar_em", INSTANTE + timedelta(microseconds=1)),
        ("artes", (ARTE_2, ARTE_1, f"instagram:{ARTE_IG}")),
        ("artes", (ARTE_1, f"instagram:{ARTE_IG}")),
        ("artes", (ARTE_1, ARTE_2, f"tiktok:{ARTE_IG}")),
        ("artes", (ARTE_1, ARTE_IG, f"instagram:{ARTE_IG}")),
    ],
    ids=[
        "titulo",
        "corpo",
        "secao",
        "plataformas",
        "formato",
        "formato-none",
        "data",
        "hora",
        "segundo",
        "microssegundo",
        "ordem-artes",
        "arte-removida",
        "arte-de-outra-plataforma",
        "caminho-arte",
    ],
)
def test_campo_publicavel_muda_o_selo(armazem: ArmazemLocal, campo: str, valor: Any) -> None:
    assert calcular_selo(_post(**{campo: valor}), armazem) != calcular_selo(_post(), armazem)


def test_secao_nova_muda_o_selo(armazem: ArmazemLocal) -> None:
    secoes = {"Instagram": "Legenda do Instagram.", "TikTok": "Texto TikTok.", "YouTube": "x"}
    assert calcular_selo(_post(secoes_por_plataforma=secoes), armazem) != calcular_selo(
        _post(), armazem
    )


def test_conteudo_da_arte_muda_o_selo(armazem: ArmazemLocal, tmp_path: Path) -> None:
    antes = calcular_selo(_post(), armazem)
    (tmp_path / "artes" / ARTE_2).write_bytes(b"\x89PNG conteudo ficticio 02 editado")
    assert calcular_selo(_post(), armazem) != antes


def test_conteudo_de_arte_so_de_uma_plataforma_muda_o_selo(
    armazem: ArmazemLocal, tmp_path: Path
) -> None:
    antes = calcular_selo(_post(), armazem)
    (tmp_path / "artes" / ARTE_IG).write_bytes(b"outro")
    assert calcular_selo(_post(), armazem) != antes


def test_caminho_diferente_com_mesmo_conteudo_muda_o_selo(
    armazem: ArmazemLocal, tmp_path: Path
) -> None:
    copia = "serie-a/2026/11/arte_01_copia.png"
    (tmp_path / "artes" / copia).write_bytes(CONTEUDOS[ARTE_1])
    trocado = _post(artes=(copia, ARTE_2, f"instagram:{ARTE_IG}"))
    assert calcular_selo(trocado, armazem) != calcular_selo(_post(), armazem)


def test_espaco_extra_no_texto_muda_o_selo(armazem: ArmazemLocal) -> None:
    base = calcular_selo(_post(), armazem)
    assert calcular_selo(_post(corpo=_post().corpo + " "), armazem) != base
    assert calcular_selo(_post(titulo=" " + _post().titulo), armazem) != base
    assert calcular_selo(_post(corpo=_post().corpo + "\n"), armazem) != base


def test_caixa_diferente_no_texto_muda_o_selo(armazem: ArmazemLocal) -> None:
    base = calcular_selo(_post(), armazem)
    assert calcular_selo(_post(titulo="quanto custa a energia?"), armazem) != base
    assert calcular_selo(_post(corpo=_post().corpo.upper()), armazem) != base


def test_texto_e_campos_nao_se_confundem(armazem: ArmazemLocal) -> None:
    # Sem ambiguidade de concatenação: mover texto entre título e corpo muda o selo.
    a = _post(titulo="AB", corpo="C")
    b = _post(titulo="A", corpo="BC")
    assert calcular_selo(a, armazem) != calcular_selo(b, armazem)


# --------------------------------------------------------------------------- insensibilidade


@pytest.mark.parametrize(
    ("campo", "valor"),
    [
        ("estagio", Estagio("pronto", PapelEstagio.PRONTO)),
        ("selo", "v1:" + "0" * 64),
        ("confirmado_em", datetime(2030, 1, 1, tzinfo=UTC)),
        ("confirmado_em", None),
        ("editado_em", datetime(2030, 1, 1, tzinfo=UTC)),
        ("editado_em", None),
        ("id", "post-ficticio-9999"),
        ("tema", "Outro tema"),
        ("tema", None),
        ("serie", "Outra série"),
        ("serie", None),
        ("ordem_na_serie", 4),
        ("ordem_na_serie", None),
    ],
)
def test_campo_nao_publicavel_nao_muda_o_selo(
    armazem: ArmazemLocal, campo: str, valor: Any
) -> None:
    assert calcular_selo(_post(**{campo: valor}), armazem) == calcular_selo(_post(), armazem)


def test_mesmo_instante_em_fusos_diferentes(armazem: ArmazemLocal) -> None:
    em_utc = INSTANTE.astimezone(UTC)
    em_toquio = INSTANTE.astimezone(timezone(timedelta(hours=9)))
    base = calcular_selo(_post(), armazem)
    assert calcular_selo(_post(publicar_em=em_utc), armazem) == base
    assert calcular_selo(_post(publicar_em=em_toquio), armazem) == base


def test_ordem_e_caixa_das_plataformas_nao_importam(armazem: ArmazemLocal) -> None:
    base = calcular_selo(_post(), armazem)
    assert calcular_selo(_post(plataformas=("TikTok", "Instagram")), armazem) == base
    assert calcular_selo(_post(plataformas=("tiktok", "INSTAGRAM")), armazem) == base


def test_caixa_do_prefixo_da_arte_nao_importa(armazem: ArmazemLocal) -> None:
    variante = _post(artes=(ARTE_1, ARTE_2, f"Instagram:{ARTE_IG}"))
    assert calcular_selo(variante, armazem) == calcular_selo(_post(), armazem)


def test_unicode_composto_e_decomposto_dao_o_mesmo_selo(armazem: ArmazemLocal) -> None:
    corpo = "Ação e coração"
    composto = unicodedata.normalize("NFC", corpo)
    decomposto = unicodedata.normalize("NFD", corpo)
    assert composto != decomposto
    a = _post(corpo=composto, titulo=unicodedata.normalize("NFC", "Título"))
    b = _post(corpo=decomposto, titulo=unicodedata.normalize("NFD", "Título"))
    assert calcular_selo(a, armazem) == calcular_selo(b, armazem)


def test_quebras_de_linha_normalizadas(armazem: ArmazemLocal) -> None:
    a = _post(corpo="a\nb\nc")
    b = _post(corpo="a\r\nb\rc")
    assert calcular_selo(a, armazem) == calcular_selo(b, armazem)


# --------------------------------------------------------------------------- falha segura


def test_prefixo_de_plataforma_desconhecido_propaga(armazem: ArmazemLocal) -> None:
    with pytest.raises(VariacaoInvalida):
        calcular_selo(_post(artes=(ARTE_1, f"youtube:{ARTE_2}")), armazem)


def test_sem_data_e_impossivel(armazem: ArmazemLocal) -> None:
    with pytest.raises(SeloImpossivel):
        calcular_selo(_post(publicar_em=None), armazem)


def test_somente_data_e_impossivel(armazem: ArmazemLocal) -> None:
    with pytest.raises(SeloImpossivel):
        calcular_selo(_post(publicar_em=None, somente_data=True), armazem)
    # Inconsistente (somente_data com instante): também recusa.
    with pytest.raises(SeloImpossivel):
        calcular_selo(_post(somente_data=True), armazem)


def test_sem_plataformas_e_impossivel(armazem: ArmazemLocal) -> None:
    with pytest.raises(SeloImpossivel):
        calcular_selo(_post(plataformas=(), artes=()), armazem)
    with pytest.raises(SeloImpossivel):
        calcular_selo(_post(plataformas=("Instagram", "  "), artes=()), armazem)


def test_plataforma_duplicada_e_impossivel(armazem: ArmazemLocal) -> None:
    with pytest.raises(SeloImpossivel):
        calcular_selo(_post(plataformas=("Instagram", "instagram"), artes=()), armazem)


def test_secao_duplicada_por_caixa_e_impossivel(armazem: ArmazemLocal) -> None:
    with pytest.raises(SeloImpossivel):
        calcular_selo(_post(secoes_por_plataforma={"Instagram": "a", "instagram": "b"}), armazem)


def test_sem_titulo_e_impossivel(armazem: ArmazemLocal) -> None:
    # Post valida o título; um objeto contornando a validação ainda é recusado.
    post = _post()
    object.__setattr__(post, "titulo", "   ")
    with pytest.raises(SeloImpossivel):
        calcular_selo(post, armazem)


def test_datetime_ingenuo_e_impossivel(armazem: ArmazemLocal) -> None:
    post = _post()
    object.__setattr__(post, "publicar_em", datetime(2026, 11, 7, 18, 30))
    with pytest.raises(SeloImpossivel):
        calcular_selo(post, armazem)


def test_arte_ausente_propaga(armazem: ArmazemLocal) -> None:
    with pytest.raises(ArteNaoEncontrada):
        calcular_selo(_post(artes=("serie-a/2026/11/nao_existe.png",)), armazem)


def test_caminho_invalido_propaga(armazem: ArmazemLocal) -> None:
    with pytest.raises(CaminhoDeArteInvalido):
        calcular_selo(_post(artes=("../fora.png",)), armazem)


def test_sem_artes_e_valido(armazem: ArmazemLocal) -> None:
    assert calcular_selo(_post(artes=()), armazem).startswith("v1:")


# --------------------------------------------------------------------------- verificar_selo


@pytest.mark.parametrize(
    "gravado",
    [None, "", "v2:" + "a" * 64, "v1:", "v1:xyz", "v1:" + "A" * 64, "lixo", "v1:" + "a" * 63],
)
def test_verificar_selo_recusa_invalidos(armazem: ArmazemLocal, gravado: str | None) -> None:
    assert verificar_selo(_post(), armazem, gravado) is False


def test_verificar_selo_aceita_valido(armazem: ArmazemLocal) -> None:
    selo = calcular_selo(_post(), armazem)
    assert verificar_selo(_post(selo=selo), armazem, selo) is True


def test_verificar_selo_de_outro_post(armazem: ArmazemLocal) -> None:
    outro = calcular_selo(_post(titulo="Outro post"), armazem)
    assert verificar_selo(_post(), armazem, outro) is False


def test_verificar_selo_apos_edicao_da_arte(armazem: ArmazemLocal, tmp_path: Path) -> None:
    selo = calcular_selo(_post(), armazem)
    (tmp_path / "artes" / ARTE_1).write_bytes(b"trocada")
    assert verificar_selo(_post(), armazem, selo) is False


def test_verificar_selo_propaga_falhas(armazem: ArmazemLocal) -> None:
    selo = calcular_selo(_post(), armazem)
    with pytest.raises(SeloImpossivel):
        verificar_selo(_post(publicar_em=None), armazem, selo)
    with pytest.raises(ArteNaoEncontrada):
        verificar_selo(_post(artes=("serie-a/2026/11/nao_existe.png",)), armazem, selo)


# --------------------------------------------------------------------------- propriedade

# hypothesis não está instalado neste ambiente: fuzz determinístico com semente fixa.
_ALFABETO = "abcXYZ áçÃé\n\t.,!?0123456789́😀"


def test_fuzz_alterar_qualquer_caractere_do_corpo_muda_o_selo(armazem: ArmazemLocal) -> None:
    gerador = random.Random(20261107)
    for _ in range(500):
        tamanho = gerador.randint(1, 40)
        corpo = unicodedata.normalize(
            "NFC", "".join(gerador.choice(_ALFABETO) for _ in range(tamanho))
        )
        posicao = gerador.randrange(len(corpo))
        operacao = gerador.choice(("trocar", "inserir", "remover"))
        if operacao == "trocar":
            novo_char = gerador.choice([c for c in _ALFABETO if c != corpo[posicao]])
            alterado = corpo[:posicao] + novo_char + corpo[posicao + 1 :]
        elif operacao == "inserir":
            alterado = corpo[:posicao] + gerador.choice(_ALFABETO) + corpo[posicao:]
        else:
            alterado = corpo[:posicao] + corpo[posicao + 1 :]
        alterado = unicodedata.normalize("NFC", alterado)
        if alterado == corpo:  # a normalização pode desfazer a alteração; nada a provar
            continue
        base = dataclasses.replace(_post(), corpo=corpo, artes=())
        mudado = dataclasses.replace(base, corpo=alterado)
        assert calcular_selo(base, armazem) != calcular_selo(mudado, armazem), (corpo, alterado)
