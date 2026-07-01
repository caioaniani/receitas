"""Inflação da previsão — A1 (double-count do fallback) + A2 (zeros implícitos)
+ A3 (outlier 2 pontos), 30/06.

A1: o fallback da previsão era `soma_total / dias_janela` cru. Pra um item com
padrão forte de dia (ex: só vende sábado), o volume do sábado entrava no
soma_total, era dividido por 42 e somado em CADA dia útil — contado 2x (média do
sábado + diluído nos dias vazios). Um item só-de-sábado de 100 previa ~186 na
semana. Agora o fallback usa a taxa RESIDUAL (tira o volume dos dows com média
própria); item sem padrão de dow (giro baixo) mantém a média diária.

A2: a média por dia-da-semana era feita só sobre as datas COM pedido. Demanda
intermitente (sábados alternados) saía superestimada. Agora o denominador conta
os sábados SEM pedido — a partir do 1º pedido (item novo não é penalizado pelas
semanas antes de existir).

A3: a faixa de 2 datas ficava sem proteção a outlier (o cap só ligava com 3+).
Um pico obvio (50x) estourava a previsão. Agora 2 pontos com salto obvio (> 5x)
são capados; variação normal (2-3x) não.
"""
from datetime import date

from app.services.previsao_producao import (
    _media_recencia,
)

# ── A2: zeros implícitos ──────────────────────────────────────────────────
_SABADOS = [date(2026, 5, 23), date(2026, 5, 30), date(2026, 6, 6),
            date(2026, 6, 13), date(2026, 6, 20), date(2026, 6, 27)]
_HOJE = date(2026, 7, 1)


def test_a2_intermitente_nao_e_superestimado():
    # pedido de 100 em 3 dos 6 sábados (alternados), todos no período ativo.
    obs = {_SABADOS[0]: 100, _SABADOS[2]: 100, _SABADOS[4]: 100}
    sem = _media_recencia(obs, _HOJE)                         # só observados ~100
    com = _media_recencia(obs, _HOJE, datas_possiveis=_SABADOS)
    assert sem > 90                                           # comportamento antigo
    assert com < 70, com          # sábados vazios puxam pra baixo (intermitente)
    assert com > 40, com          # mas não zera


def test_a2_demanda_consistente_nao_e_penalizada():
    # pedido em TODOS os 6 sábados -> sem zeros -> média = o tamanho do pedido.
    obs = {s: 100 for s in _SABADOS}
    com = _media_recencia(obs, _HOJE, datas_possiveis=_SABADOS)
    assert abs(com - 100) < 1e-6


def test_a2_item_novo_nao_e_penalizado_pelo_passado():
    # só os 3 sábados MAIS RECENTES têm pedido (item começou há 3 semanas). Os
    # sábados ANTES do 1º pedido não contam -> não penaliza item novo.
    obs = {_SABADOS[3]: 100, _SABADOS[4]: 100, _SABADOS[5]: 100}
    com = _media_recencia(obs, _HOJE, datas_possiveis=_SABADOS)
    assert abs(com - 100) < 1e-6, com   # 3 de 3 no período ativo -> 100


# ── A3: outlier com 2 pontos ──────────────────────────────────────────────
def test_a3_pico_obvio_2pts_nao_estoura_a_media():
    # 10 e 500 (50x): capa no 10 -> média perto de 10, não de 255.
    m = _media_recencia({date(2026, 6, 20): 10, date(2026, 6, 27): 500}, _HOJE)
    assert m < 30, m


def test_a3_variacao_normal_2pts_nao_e_capada():
    # 10 e 30 (3x): variação normal, NÃO capa -> média entre os dois.
    m = _media_recencia({date(2026, 6, 20): 10, date(2026, 6, 27): 30}, _HOJE)
    assert 18 < m < 30, m
