"""Testes do audit log de edicao de pedido.

Cobre:
- Edicao via web seta `modificado_em` + `modificado_por_id` no PedidoLoja
- Edicao via copilot tambem seta esses campos
- AuditLog (listener automatico) popula `usuario_id` via session.info
  (fluxo Slack — webhook anonimo do Flask-Login)
- Filtro `registro_id` no /audit
"""
from datetime import date, timedelta


def _login(cliente):
    return cliente.post('/auth/login', data={'login': 'admin', 'senha': '123'})


def _pedido_pendente(loja, admin_user, catalogo):
    from app.extensions import db
    from app.models import PedidoItem, PedidoLoja
    p = PedidoLoja(loja_id=loja.id, status='pendente',
                   data_entrega=date.today() + timedelta(days=1),
                   criado_por=admin_user.id)
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p.id,
                              receita_id=catalogo['receita'].id,
                              quantidade=10))
    db.session.commit()
    return p


def test_editar_via_web_seta_modificado_por(
        app, admin_user, loja, catalogo):
    from app.models import PedidoLoja
    p = _pedido_pendente(loja, admin_user, catalogo)
    cliente = app.test_client()
    _login(cliente)

    nova_data = (date.today() + timedelta(days=3)).strftime('%Y-%m-%d')
    cliente.post(f'/pedidos/{p.id}/editar', data={
        'data_entrega': nova_data,
        'observacao': '',
        'item_id[]': [f'r_{catalogo["receita"].id}'],
        'item_qtd[]': ['12'],
        'item_estado[]': [''],
        'item_obs[]': [''],
    })

    p2 = PedidoLoja.query.get(p.id)
    assert p2.modificado_em is not None
    assert p2.modificado_por_id == admin_user.id


def test_editar_via_copilot_seta_modificado_por(
        app, admin_user, loja, catalogo):
    from app.models import PedidoLoja
    from app.services.copilot import executar_editar_pedido
    p = _pedido_pendente(loja, admin_user, catalogo)

    nova_data = (date.today() + timedelta(days=3)).strftime('%Y-%m-%d')
    res = executar_editar_pedido({
        'pedido_id': p.id,
        'data_entrega': nova_data,
    }, admin_user)
    assert res['ok'], res

    p2 = PedidoLoja.query.get(p.id)
    assert p2.modificado_em is not None
    assert p2.modificado_por_id == admin_user.id


def test_audit_log_via_session_info_popula_usuario_id(
        app, admin_user, loja, catalogo):
    """Simula fluxo Slack: seta db.session.info['audit_user_id'] antes do
    commit. _current_user_id() pega via session.info quando fora de request."""
    from app.extensions import db
    from app.models import AuditLog
    p = _pedido_pendente(loja, admin_user, catalogo)

    db.session.info['audit_user_id'] = admin_user.id
    try:
        p.observacao = 'editado via Slack'
        db.session.commit()
    finally:
        db.session.info.pop('audit_user_id', None)

    log = (AuditLog.query
           .filter_by(tabela='pedido_loja', registro_id=p.id, acao='update')
           .order_by(AuditLog.criado_em.desc()).first())
    assert log is not None
    assert log.usuario_id == admin_user.id


def test_filtro_registro_id_no_audit(app, admin_user, loja, catalogo):
    """GET /audit?registro_id=X retorna so logs daquele registro."""
    from app.extensions import db
    p1 = _pedido_pendente(loja, admin_user, catalogo)
    p2 = _pedido_pendente(loja, admin_user, catalogo)

    cliente = app.test_client()
    _login(cliente)

    db.session.info['audit_user_id'] = admin_user.id
    try:
        p1.observacao = 'mudanca pedido 1'
        p2.observacao = 'mudanca pedido 2'
        db.session.commit()
    finally:
        db.session.info.pop('audit_user_id', None)

    r = cliente.get(f'/audit?registro_id={p1.id}')
    assert r.status_code == 200
    assert b'mudanca pedido 1' in r.data
    assert b'mudanca pedido 2' not in r.data
