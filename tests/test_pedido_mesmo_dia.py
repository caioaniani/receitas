"""Entrega no MESMO dia liberada pra todos os papéis (decisão do dono
15/07/2026) — antes só admin; funcionário/gerente só podiam pedir pra
amanhã em diante. Passado continua bloqueado."""
from datetime import timedelta

from app.utils import hoje


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user_id)
        sess['_fresh'] = True


def _funcionario(loja):
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='Func Loja', login='func-mesmo-dia',
                papel='funcionario', loja_id=loja.id)
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    return u


def _receita():
    from app.extensions import db
    from app.models import Receita
    r = Receita(nome='Pao Mesmo Dia', categoria='Paes', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0)
    db.session.add(r)
    db.session.commit()
    return r


def test_funcionario_cria_pedido_para_hoje(app, loja):
    from app.models import PedidoLoja
    with app.app_context():
        func = _funcionario(loja)
        r = _receita()
        rid, lid, uid = r.id, loja.id, func.id
    client = app.test_client()
    _login(client, uid)
    resp = client.post('/pedidos/novo', data={
        'loja_id': str(lid),
        'data_entrega': hoje().isoformat(),
        'item_id[]': f'r_{rid}',
        'item_qtd[]': '10',
        'item_estado[]': '',
        'item_obs[]': '',
    })
    assert resp.status_code in (302, 303)
    with app.app_context():
        p = PedidoLoja.query.filter_by(loja_id=lid).first()
        assert p is not None
        assert p.data_entrega == hoje()
        assert p.criado_por == uid


def test_data_no_passado_continua_recusada(app, loja, admin_user):
    from app.models import PedidoLoja
    with app.app_context():
        r = _receita()
        rid, lid = r.id, loja.id
    client = app.test_client()
    _login(client, admin_user.id)
    resp = client.post('/pedidos/novo', data={
        'loja_id': str(lid),
        'data_entrega': (hoje() - timedelta(days=1)).isoformat(),
        'item_id[]': f'r_{rid}',
        'item_qtd[]': '10',
        'item_estado[]': '',
        'item_obs[]': '',
    }, follow_redirects=True)
    assert 'data de entrega deve ser a partir de' in resp.get_data(as_text=True)
    with app.app_context():
        assert PedidoLoja.query.filter_by(loja_id=lid).count() == 0


def test_editar_para_hoje_liberado(app, loja, admin_user):
    """Editar a data para HOJE também liberado (funcionário não edita —
    capacidade web_pedido_operar, regra pré-existente e intocada)."""
    from app.extensions import db
    from app.models import PedidoItem, PedidoLoja
    with app.app_context():
        r = _receita()
        p = PedidoLoja(loja_id=loja.id, status='pendente',
                       data_entrega=hoje() + timedelta(days=2),
                       data_pedido=hoje())
        db.session.add(p)
        db.session.flush()
        db.session.add(PedidoItem(pedido_id=p.id, receita_id=r.id,
                                  quantidade=5))
        db.session.commit()
        pid, rid = p.id, r.id
    client = app.test_client()
    _login(client, admin_user.id)
    resp = client.post(f'/pedidos/{pid}/editar', data={
        'data_entrega': hoje().isoformat(),
        'observacao': '',
        'item_id[]': f'r_{rid}',
        'item_qtd[]': '5',
        'item_estado[]': '',
        'item_obs[]': '',
    })
    assert resp.status_code in (302, 303)
    with app.app_context():
        from app.extensions import db as _db
        p2 = _db.session.get(PedidoLoja, pid)
        assert p2.data_entrega == hoje()
