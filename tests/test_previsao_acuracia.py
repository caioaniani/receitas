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
    # chaves distintas (a unique de data_alvo/loja/receita impede colidir)
    loja = _loja()
    r = _receita('Casado')
    r2 = _receita('SemRealizado')
    hoje_d = hoje()
    _snap(loja, r, hoje_d - timedelta(days=2), previsto=10, realizado=10)  # ok
    _snap(loja, r2, hoje_d - timedelta(days=2), previsto=5)  # sem realizado
    _snap(loja, r, hoje_d - timedelta(days=200), previsto=99, realizado=1)  # velho

    res = svc.resumo_acuracia(dias=30)
    assert res['total']['previsto'] == 10      # so o casado e dentro do periodo
    assert res['total']['n'] == 1


# ── painel (rota) ─────────────────────────────────────────────────────────
def _login(client, admin_user):
    client.post('/auth/login',
                data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)


def test_painel_renderiza_e_rodar_cria_snapshot(app, admin_user):
    loja = _loja()
    r = _receita()
    hoje_d = hoje()
    for semanas in (1, 2, 3):
        _entrega(loja, r, hoje_d - timedelta(days=7 * semanas), 'recebido', 10)

    client = app.test_client()
    _login(client, admin_user)
    resp = client.get('/producao/previsao-acuracia?dias=30')
    assert resp.status_code == 200
    resp2 = client.post('/producao/previsao-acuracia/rodar')
    assert resp2.status_code == 302
    assert PrevisaoSnapshot.query.count() >= 1


# ── Fase 0.2 (02/07/2026): motores vivos, lead, re-casamento, segmentos ───

def test_snapshot_grava_motores_vivos_com_lead(app):
    """registrar_snapshot congela os DOIS motores vivos (media_pedido e
    venda_estoque) com motor e lead_dias preenchidos — a acuracia parou de
    medir o motor aposentado."""
    loja = _loja()
    r = _receita()
    hoje_d = hoje()
    for semanas in (1, 2, 3):
        _entrega(loja, r, hoje_d - timedelta(days=7 * semanas), 'recebido', 10)

    novos = svc.registrar_snapshot(horizonte_dias=7, janela_semanas=6)
    assert novos >= 1
    motores = {m for (m,) in db.session.query(PrevisaoSnapshot.motor)
               .distinct().all()}
    assert 'media_pedido' in motores
    assert 'pedido_semana' not in motores      # legado nao e mais gravado
    snap = PrevisaoSnapshot.query.filter_by(motor='media_pedido').first()
    assert snap.lead_dias is not None
    assert snap.lead_dias == (snap.data_alvo - hoje_d).days


def test_recasamento_corrige_entrega_marcada_tarde(app):
    """Pedido marcado 'entregue' DEPOIS do cron: o snapshot casado como 0 e
    RE-casado na janela de 48h (nao fica congelado errado pra sempre)."""
    loja = _loja()
    r = _receita()
    ontem = hoje() - timedelta(days=1)
    s = _snap(loja, r, ontem, previsto=10)
    # 1o casamento: nada entregue ainda -> realizado 0
    assert svc.casar_realizados() == 1
    db.session.refresh(s)
    assert s.realizado == 0
    # a entrega e marcada DEPOIS (atraso de quem opera)
    _entrega(loja, r, ontem, 'entregue', 8)
    # 2a rodada re-casa (casado_em recente) e corrige
    assert svc.casar_realizados() == 1
    db.session.refresh(s)
    assert s.realizado == 8


def test_recasamento_nao_recarimba_sem_mudanca(app):
    """Re-casamento sem mudanca de valor NAO re-carimba casado_em (senao a
    janela de 48h deslizaria pra sempre)."""
    loja = _loja()
    r = _receita()
    ontem = hoje() - timedelta(days=1)
    _entrega(loja, r, ontem, 'entregue', 8)
    s = _snap(loja, r, ontem, previsto=10)
    assert svc.casar_realizados() == 1
    db.session.refresh(s)
    carimbo = s.casado_em
    assert svc.casar_realizados() == 0        # nada mudou -> nada re-casado
    db.session.refresh(s)
    assert s.casado_em == carimbo


def test_resumo_por_motor_loja_e_lead(app):
    """O resumo filtra por motor e segmenta por loja e por lead."""
    loja_a = _loja('Loja A')
    loja_b = _loja('Loja B')
    r = _receita()
    ontem = hoje() - timedelta(days=1)
    for loja_x, prev, real, lead in ((loja_a, 10, 8, 1), (loja_b, 20, 30, 5)):
        s = PrevisaoSnapshot(data_alvo=ontem, loja_id=loja_x.id,
                             receita_id=r.id, previsto=prev, realizado=real,
                             motor='media_pedido', lead_dias=lead)
        db.session.add(s)
    s2 = PrevisaoSnapshot(data_alvo=ontem, loja_id=loja_a.id,
                          receita_id=r.id, previsto=99, realizado=1,
                          motor='venda_estoque', lead_dias=1)
    db.session.add(s2)
    db.session.commit()

    res = svc.resumo_acuracia(dias=30, motor='media_pedido')
    assert res['total']['previsto'] == 30      # so o motor filtrado
    assert {x['nome'] for x in res['por_loja']} == {'Loja A', 'Loja B'}
    assert {x['nome'] for x in res['por_lead']} == {'D-1', 'D-5'}
    assert res['motores'].get('venda_estoque') == 1

    res_todos = svc.resumo_acuracia(dias=30)
    assert res_todos['total']['previsto'] == 129


def test_circularidade_conta_pedidos_auto_gerados(app):
    """% dos pedidos entregues que nasceram da propria sugestao (rascunho
    auto-gerado) — quantifica o eco previsao->pedido->'realizado'."""
    loja = _loja()
    r = _receita()
    ontem = hoje() - timedelta(days=1)
    _entrega(loja, r, ontem, 'entregue', 10)
    p2 = _entrega(loja, r, ontem, 'recebido', 10)
    p2.observacao = 'Gerado do histórico (rascunho) — revisar e confirmar.'
    db.session.commit()

    res = svc.resumo_acuracia(dias=30)
    assert res['pedidos_entregues'] == 2
    assert res['pedidos_auto'] == 1
    assert res['circularidade_pct'] == 50.0


# ── acuracia por (loja, receita) + comparativo motor×loja (11/07/2026) ─────
def _snap_motor(loja, receita, data_alvo, previsto, realizado, motor,
                lead=1):
    s = PrevisaoSnapshot(data_alvo=data_alvo, loja_id=loja.id,
                         receita_id=receita.id, previsto=previsto,
                         realizado=realizado, motor=motor, lead_dias=lead)
    db.session.add(s)
    db.session.commit()
    return s


def test_acuracia_por_loja_receita_filtra_amostra_rasa(app):
    """Badge da grade: agrega por (loja, receita) de UM motor; par com menos
    de min_n snapshots casados fica fora (ruido)."""
    loja = _loja()
    r = _receita()
    r2 = _receita('Baguete')
    ontem = hoje() - timedelta(days=1)
    for k in range(5):
        _snap_motor(loja, r, ontem - timedelta(days=k), 12, 10,
                    'media_pedido')
    _snap_motor(loja, r2, ontem, 30, 10, 'media_pedido')   # so 1 amostra

    mapa = svc.acuracia_por_loja_receita('media_pedido', dias=60, min_n=5)
    assert (loja.id, r.id) in mapa
    assert (loja.id, r2.id) not in mapa                     # amostra rasa
    ac = mapa[(loja.id, r.id)]
    assert ac['n'] == 5
    assert ac['previsto'] == 60 and ac['realizado'] == 50
    assert ac['vies_pct'] == 20.0                           # (60-50)/50
    assert ac['wape_pct'] == 20.0                           # 10/50


def test_min_n_conta_datas_distintas_nao_leads(app):
    """Com 1 snapshot por antecedencia, um unico dia visto de 5 leads NAO
    fura a guarda de amostra rasa (min_n conta datas-alvo distintas)."""
    loja = _loja()
    r = _receita()
    ontem = hoje() - timedelta(days=1)
    for lead in range(5):
        _snap_motor(loja, r, ontem, 12, 10, 'media_pedido', lead=lead)
    assert svc.acuracia_por_loja_receita('media_pedido', min_n=5) == {}
    # 5 datas distintas (mesmo com 1 lead cada) continuam passando
    for k in range(1, 5):
        _snap_motor(loja, r, ontem - timedelta(days=k), 12, 10,
                    'media_pedido')
    mapa = svc.acuracia_por_loja_receita('media_pedido', min_n=5)
    assert (loja.id, r.id) in mapa
    assert mapa[(loja.id, r.id)]['n'] == 9      # n segue sendo snapshots


def test_acuracia_por_loja_receita_so_do_motor_pedido(app):
    loja = _loja()
    r = _receita()
    ontem = hoje() - timedelta(days=1)
    for k in range(5):
        _snap_motor(loja, r, ontem - timedelta(days=k), 12, 10,
                    'venda_estoque')
    assert svc.acuracia_por_loja_receita('media_pedido') == {}


def test_comparativo_motores_por_loja_aponta_melhor(app):
    """Tabela 'qual motor acerta mais por loja': os dois lado a lado, melhor
    = menor WAPE; loja com um motor so nao ganha veredito."""
    loja = _loja()
    loja_b = _loja('Loja B')
    r = _receita()
    ontem = hoje() - timedelta(days=1)
    # Loja A: media erra pouco (WAPE 10%), venda erra muito (50%)
    _snap_motor(loja, r, ontem, 11, 10, 'media_pedido')
    _snap_motor(loja, r, ontem, 15, 10, 'venda_estoque')
    # Loja B: so um motor tem dado
    _snap_motor(loja_b, r, ontem, 20, 10, 'media_pedido')

    comp = svc.comparativo_motores_por_loja(dias=30)
    por_nome = {c['nome']: c for c in comp}
    assert por_nome['Loja A']['melhor'] == 'media_pedido'
    assert por_nome['Loja A']['motores']['media_pedido']['wape_pct'] == 10.0
    assert por_nome['Loja A']['motores']['venda_estoque']['wape_pct'] == 50.0
    assert por_nome['Loja B']['melhor'] is None


def test_tela_acuracia_mostra_comparativo_e_links(app, admin_user):
    loja = _loja()
    r = _receita()
    ontem = hoje() - timedelta(days=1)
    _snap_motor(loja, r, ontem, 11, 10, 'media_pedido')
    _snap_motor(loja, r, ontem, 15, 10, 'venda_estoque')
    c = app.test_client()
    c.post('/auth/login', data={'login': 'admin', 'senha': '123'})
    corpo = c.get('/producao/previsao-acuracia').get_data(as_text=True)
    assert 'Qual motor acerta mais em cada loja?' in corpo
    assert '/producao/pedidos-semana/media' in corpo        # link de volta
    assert '/producao/pedidos-semana/estoque' in corpo


# ── snapshot por ANTECEDENCIA (11/07/2026, aprovado pelo dono) ─────────────
def test_snapshot_por_antecedencia_grava_um_por_lead(app):
    """A MESMA data-alvo ganha um snapshot novo a cada antecedência (o cron
    de amanhã re-congela D-1 depois do D-2 de hoje); o MESMO lead no mesmo
    dia continua idempotente."""
    loja = _loja()
    r = _receita()
    amanha = hoje() + timedelta(days=1)
    sug = {'dias': [{'data': amanha.isoformat()}],
           'lojas': [{'loja_id': loja.id, 'ja_tem': [],
                      'produtos': [{'receita_id': r.id, 'por_dia': [10]}]}]}
    n1 = svc._registrar_motor('media_pedido', sug, hoje())              # D-1
    n2 = svc._registrar_motor('media_pedido', sug, hoje())              # repete
    n3 = svc._registrar_motor('media_pedido', sug,
                              hoje() - timedelta(days=2))               # D-3
    db.session.commit()
    assert (n1, n2, n3) == (1, 0, 1)
    snaps = (PrevisaoSnapshot.query
             .filter_by(receita_id=r.id, data_alvo=amanha).all())
    assert sorted(s.lead_dias for s in snaps) == [1, 3]


def test_por_lead_compara_antecedencias_da_mesma_data(app):
    """Com snapshots de leads diferentes da MESMA data, o resumo 'por lead'
    mostra uma linha por antecedência — antes só existia o primeiro lead."""
    loja = _loja()
    r = _receita()
    ontem = hoje() - timedelta(days=1)
    for lead, previsto in ((1, 11), (3, 15)):
        db.session.add(PrevisaoSnapshot(
            data_alvo=ontem, loja_id=loja.id, receita_id=r.id,
            previsto=previsto, realizado=10, motor='media_pedido',
            lead_dias=lead))
    db.session.commit()
    res = svc.resumo_acuracia(dias=30, motor='media_pedido')
    por_lead = {x['nome']: x for x in res['por_lead']}
    assert set(por_lead) == {'D-1', 'D-3'}
    assert por_lead['D-1']['vies'] == 1
    assert por_lead['D-3']['vies'] == 5
