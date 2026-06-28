"""Instrumentacao de acuracia do forecast (28/06/2026).

Congela o previsto do pedido semanal (PrevisaoSnapshot), casa com o realizado
(entregue) e agrega vies + WAPE pro painel. Antes nao havia NADA medindo se a
previsao acerta.
"""
from datetime import timedelta

from app.extensions import db
from app.models import Loja, PedidoItem, PedidoLoja, PrevisaoSnapshot, Receita
from app.services import previsao_acuracia as svc
from app.utils import hoje


def _loja(nome='Loja A'):
    loja = Loja(nome=nome, ativa=True)
    db.session.add(loja)
    db.session.commit()
    return loja


def _receita(nome='Croissant'):
    r = Receita(nome=nome, categoria='X', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0)
    db.session.add(r)
    db.session.commit()
    return r


def _entrega(loja, receita, data, status='entregue', qtd=10, recebida=None):
    p = PedidoLoja(loja_id=loja.id, status=status, data_entrega=data,
                   data_pedido=data)
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p.id, receita_id=receita.id,
                              quantidade=qtd, quantidade_recebida=recebida))
    db.session.commit()
    return p


def _snap(loja, receita, data_alvo, previsto, realizado=None):
    s = PrevisaoSnapshot(data_alvo=data_alvo, loja_id=loja.id,
                         receita_id=receita.id, previsto=previsto,
                         realizado=realizado)
    db.session.add(s)
    db.session.commit()
    return s


# ── registrar_snapshot ────────────────────────────────────────────────────
def test_registrar_snapshot_idempotente(app):
    loja = _loja()
    r = _receita()
    hoje_d = hoje()
    for semanas in (1, 2, 3):
        _entrega(loja, r, hoje_d - timedelta(days=7 * semanas), 'recebido', 10)

    novos1 = svc.registrar_snapshot(horizonte_dias=7, janela_semanas=6)
    assert novos1 >= 1
    # 2a rodada nao recria nada (mesma data-alvo/loja/receita)
    novos2 = svc.registrar_snapshot(horizonte_dias=7, janela_semanas=6)
    assert novos2 == 0
    # snapshot do dia0 (hoje, mesmo dow) existe com o previsto
    s = PrevisaoSnapshot.query.filter_by(loja_id=loja.id, receita_id=r.id,
                                         data_alvo=hoje_d).first()
    assert s is not None and s.previsto == 10 and s.realizado is None


# ── casar_realizados ──────────────────────────────────────────────────────
def test_casar_usa_recebida_e_so_datas_passadas(app):
    loja = _loja()
    r = _receita()
    hoje_d = hoje()
    ontem = hoje_d - timedelta(days=1)
    # snapshot de ontem (previsto 10) + entrega real: pediu 8, recebeu 7
    snap = _snap(loja, r, ontem, previsto=10)
    _entrega(loja, r, ontem, 'entregue', qtd=8, recebida=7)
    # snapshot futuro NAO deve ser casado
    futuro = _snap(loja, r, hoje_d + timedelta(days=2), previsto=99)

    casados = svc.casar_realizados()
    assert casados == 1
    db.session.refresh(snap)
    db.session.refresh(futuro)
    assert snap.realizado == 7          # coalesce(recebida, qtd) -> 7
    assert snap.casado_em is not None
    assert futuro.realizado is None     # data ainda nao passou


def test_casar_ignora_cancelado(app):
    loja = _loja()
    r = _receita()
    ontem = hoje() - timedelta(days=1)
    snap = _snap(loja, r, ontem, previsto=10)
    _entrega(loja, r, ontem, 'cancelado', qtd=8, recebida=8)

    svc.casar_realizados()
    db.session.refresh(snap)
    assert snap.realizado == 0          # cancelado nao e demanda


# ── resumo_acuracia ───────────────────────────────────────────────────────
def test_resumo_vies_e_wape(app):
    loja = _loja()
    r = _receita('Pao')
    base = hoje() - timedelta(days=3)
    _snap(loja, r, base, previsto=10, realizado=8)        # erro 2
    _snap(loja, r, base - timedelta(days=1), previsto=20, realizado=25)  # erro 5

    res = svc.resumo_acuracia(dias=30)
    assert res['total']['previsto'] == 30
    assert res['total']['realizado'] == 33
    assert res['total']['vies'] == -3                      # 30 - 33
    # WAPE = (2 + 5) / 33 = 21.2%
    assert res['total']['wape_pct'] == 21.2
    linha = res['por_receita'][0]
    assert linha['nome'] == 'Pao'
    assert linha['vies'] == -3 and linha['wape_pct'] == 21.2


def test_resumo_ignora_nao_casado_e_fora_do_periodo(app):
    loja = _loja()
    r = _receita()
    hoje_d = hoje()
    _snap(loja, r, hoje_d - timedelta(days=2), previsto=10, realizado=10)  # ok
    _snap(loja, r, hoje_d - timedelta(days=2), previsto=5)   # sem realizado
    _snap(loja, r, hoje_d - timedelta(days=200), previsto=99, realizado=1)  # velho

    res = svc.resumo_acuracia(dias=30)
    assert res['total']['previsto'] == 10      # so o casado e dentro do periodo
    assert res['total']['n'] == 1
