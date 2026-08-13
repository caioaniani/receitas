"""Corte do fim do dia do pedido loja→indústria (10/08/2026, regra do dono;
19:00 desde 13/08/2026).

"O pedido que as lojas fazem para receber no dia seguinte não pode ser
modificado após o corte" — horário de corte do pré-preparo do padeiro.
Gerente/funcionário barrados; admin/owner passa com aviso. Defesa em
profundidade: web novo/editar/cancelar + executores do copilot.
"""
from datetime import datetime, timedelta

import pytest

from app.extensions import db
from app.models import PedidoItem, PedidoLoja, Receita, Usuario
from app.services import pedido_corte
from app.services.pedido_corte import bloqueio_do_corte, corte_ativo
from app.utils import hoje


def _login(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def _gerente():
    u = Usuario(nome='ger', login='ger_corte', papel='gerente')
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    return u


def _receita(nome='Croissant Corte'):
    r = Receita(nome=nome, categoria='Croissants', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100)
    db.session.add(r)
    db.session.commit()
    return r


def _as_19h(monkeypatch):
    """Congela o relógio do serviço às 20:00 de hoje (dentro do corte)."""
    fake = datetime.combine(hoje(), datetime.min.time()).replace(hour=20)
    monkeypatch.setattr(pedido_corte, 'agora', lambda: fake)


# ── unidade ─────────────────────────────────────────────────────────

def test_corte_ativo_so_amanha_apos_19h():
    base = datetime(2026, 8, 10, 18, 59)
    amanha = base.date() + timedelta(days=1)
    assert corte_ativo(amanha, agora_dt=base) is False          # 18:59
    as18 = base.replace(hour=19, minute=0)
    assert corte_ativo(amanha, agora_dt=as18) is True           # 19:00
    assert corte_ativo(amanha + timedelta(days=1), agora_dt=as18) is False
    assert corte_ativo(base.date(), agora_dt=as18) is False     # hoje não
    assert corte_ativo(None, agora_dt=as18) is False


def test_bloqueio_gerente_barrado_admin_avisado(app, admin_user):
    as19 = datetime(2026, 8, 10, 19, 30)
    amanha = as19.date() + timedelta(days=1)
    with app.app_context():
        ger = _gerente()
        bloq, msg = bloqueio_do_corte([amanha], user=ger, agora_dt=as19)
        assert bloq is True and '19:00' in msg
        bloq, msg = bloqueio_do_corte([amanha], user=admin_user,
                                      agora_dt=as19)
        assert bloq is False and 'PODE prosseguir' in msg
        bloq, msg = bloqueio_do_corte([as19.date() + timedelta(days=3)],
                                      user=ger, agora_dt=as19)
        assert bloq is False and msg is None


# ── web ─────────────────────────────────────────────────────────────

def test_web_novo_pra_amanha_no_corte_gerente_barrado(app, loja,
                                                      monkeypatch):
    with app.app_context():
        _as_19h(monkeypatch)
        ger = _gerente()
        r = _receita()
        rid, lid = r.id, loja.id
        c = app.test_client()
        _login(c, ger)
        resp = c.post('/pedidos/novo', data={
            'loja_id': str(lid),
            'data_entrega': (hoje() + timedelta(days=1)).isoformat(),
            'item_id[]': f'r_{rid}',
            'item_qtd[]': '10',
            'item_estado[]': '',
            'item_obs[]': '',
        }, follow_redirects=True)
        assert 'horário de corte' in resp.get_data(as_text=True)
        assert PedidoLoja.query.filter_by(loja_id=lid).count() == 0


def test_web_novo_pra_amanha_no_corte_admin_passa_com_aviso(
        app, admin_user, loja, monkeypatch):
    with app.app_context():
        _as_19h(monkeypatch)
        r = _receita()
        rid, lid = r.id, loja.id
        c = app.test_client()
        _login(c, admin_user)
        resp = c.post('/pedidos/novo', data={
            'loja_id': str(lid),
            'data_entrega': (hoje() + timedelta(days=1)).isoformat(),
            'item_id[]': f'r_{rid}',
            'item_qtd[]': '10',
            'item_estado[]': '',
            'item_obs[]': '',
        }, follow_redirects=True)
        assert 'PODE prosseguir' in resp.get_data(as_text=True)
        assert PedidoLoja.query.filter_by(loja_id=lid).count() == 1


def test_web_novo_pra_depois_de_amanha_livre_no_corte(app, loja,
                                                      monkeypatch):
    with app.app_context():
        _as_19h(monkeypatch)
        ger = _gerente()
        r = _receita()
        rid, lid = r.id, loja.id
        c = app.test_client()
        _login(c, ger)
        c.post('/pedidos/novo', data={
            'loja_id': str(lid),
            'data_entrega': (hoje() + timedelta(days=2)).isoformat(),
            'item_id[]': f'r_{rid}',
            'item_qtd[]': '10',
            'item_estado[]': '',
            'item_obs[]': '',
        })
        assert PedidoLoja.query.filter_by(loja_id=lid).count() == 1


def test_web_editar_mover_PRA_amanha_no_corte_barrado(app, loja,
                                                      monkeypatch):
    """Mover um pedido de D+3 pra AMANHÃ depois do corte fura o pré-preparo
    do mesmo jeito — o corte olha a data NOVA também."""
    with app.app_context():
        _as_19h(monkeypatch)
        ger = _gerente()
        r = _receita()
        p = PedidoLoja(loja_id=loja.id,
                       data_entrega=hoje() + timedelta(days=3),
                       status='pendente', criado_por=ger.id)
        db.session.add(p)
        db.session.flush()
        db.session.add(PedidoItem(pedido_id=p.id, receita_id=r.id,
                                  quantidade=5))
        db.session.commit()
        pid, rid = p.id, r.id
        c = app.test_client()
        _login(c, ger)
        c.post(f'/pedidos/{pid}/editar', data={
            'data_entrega': (hoje() + timedelta(days=1)).isoformat(),
            'observacao': '',
            'item_id[]': f'r_{rid}',
            'item_qtd[]': '5',
            'item_estado[]': '',
            'item_obs[]': '',
        }, follow_redirects=True)
        assert (db.session.get(PedidoLoja, pid).data_entrega
                == hoje() + timedelta(days=3))      # não moveu


def test_web_cancelar_amanha_no_corte_barrado(app, loja, monkeypatch):
    with app.app_context():
        _as_19h(monkeypatch)
        ger = _gerente()
        p = PedidoLoja(loja_id=loja.id,
                       data_entrega=hoje() + timedelta(days=1),
                       status='confirmado', criado_por=ger.id)
        db.session.add(p)
        db.session.commit()
        pid = p.id
        c = app.test_client()
        _login(c, ger)
        c.post(f'/pedidos/{pid}/cancelar', follow_redirects=True)
        assert db.session.get(PedidoLoja, pid).status == 'confirmado'


def test_web_editar_amanha_antes_do_corte_livre(app, loja):
    """Sem monkeypatch de hora só roda o caminho: se AGORA for >= HORA_CORTE de
    verdade, o teste vira no-op honesto (pula) — sem flakiness de relógio."""
    from app.utils import agora
    if agora().hour >= pedido_corte.HORA_CORTE:
        pytest.skip('suite rodando após o corte BRT — caminho coberto pelos '
                    'testes com relógio congelado')
    with app.app_context():
        ger = _gerente()
        r = _receita()
        p = PedidoLoja(loja_id=loja.id,
                       data_entrega=hoje() + timedelta(days=1),
                       status='pendente', criado_por=ger.id)
        db.session.add(p)
        db.session.flush()
        db.session.add(PedidoItem(pedido_id=p.id, receita_id=r.id,
                                  quantidade=5))
        db.session.commit()
        pid, rid = p.id, r.id
        c = app.test_client()
        _login(c, ger)
        c.post(f'/pedidos/{pid}/editar', data={
            'data_entrega': (hoje() + timedelta(days=1)).isoformat(),
            'observacao': 'ajuste',
            'item_id[]': f'r_{rid}',
            'item_qtd[]': '8',
            'item_estado[]': '',
            'item_obs[]': '',
        })
        assert db.session.get(PedidoLoja, pid).itens[0].quantidade == 8


# ── copilot (executores — preview re-enviado não fura) ──────────────

def test_copilot_criar_pra_amanha_no_corte_gerente_recusado(
        app, loja, monkeypatch):
    from app.services.copilot import executar_criar_pedido
    with app.app_context():
        _as_19h(monkeypatch)
        ger = _gerente()
        r = _receita()
        res = executar_criar_pedido({
            'loja_id': loja.id,
            'data_entrega': (hoje() + timedelta(days=1)).isoformat(),
            'itens': [{'resolvido': {'tipo': 'receita', 'id': r.id,
                                     'nome': r.nome}, 'quantidade': 5}],
        }, ger)
        assert res['ok'] is False and 'corte' in res['erro']


def test_copilot_editar_amanha_no_corte_gerente_recusado(
        app, loja, monkeypatch):
    from app.services.copilot import executar_editar_pedido
    with app.app_context():
        _as_19h(monkeypatch)
        ger = _gerente()
        p = PedidoLoja(loja_id=loja.id,
                       data_entrega=hoje() + timedelta(days=1),
                       status='confirmado', criado_por=ger.id)
        db.session.add(p)
        db.session.commit()
        res = executar_editar_pedido({'pedido_id': p.id,
                                      'observacao': 'muda'}, ger)
        assert res['ok'] is False and 'corte' in res['erro']


def test_copilot_cancelar_amanha_no_corte_gerente_recusado(
        app, loja, monkeypatch):
    """Achado 4 da revisão de 13/08: o executor de mudar_status deixava
    CANCELAR o pedido de amanhã depois do corte — o pré-preparo já calculado
    sumia por um caminho que a rota web bloqueia."""
    from app.services.copilot import executar_mudar_status_pedido
    with app.app_context():
        _as_19h(monkeypatch)
        ger = _gerente()
        p = PedidoLoja(loja_id=loja.id,
                       data_entrega=hoje() + timedelta(days=1),
                       status='confirmado', criado_por=ger.id)
        db.session.add(p)
        db.session.commit()
        res = executar_mudar_status_pedido(
            {'pedido_id': p.id, 'novo_status': 'cancelar'}, ger)
        assert res['ok'] is False and 'corte' in res['erro']
        assert db.session.get(PedidoLoja, p.id).status == 'confirmado'


def test_copilot_cancelar_amanha_no_corte_admin_passa_com_aviso(
        app, admin_user, loja, monkeypatch):
    from app.services.copilot import executar_mudar_status_pedido
    with app.app_context():
        _as_19h(monkeypatch)
        p = PedidoLoja(loja_id=loja.id,
                       data_entrega=hoje() + timedelta(days=1),
                       status='confirmado', criado_por=admin_user.id)
        db.session.add(p)
        db.session.commit()
        res = executar_mudar_status_pedido(
            {'pedido_id': p.id, 'novo_status': 'cancelar'}, admin_user)
        assert res['ok'] is True
        assert 'PODE prosseguir' in res.get('aviso', '')
        assert db.session.get(PedidoLoja, p.id).status == 'cancelado'
