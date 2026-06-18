"""Dashboard admin da loja online.

Visão geral: faturamento por janela (hoje/semana/mês), pedidos por status,
fila de ação. Pago/em_preparo/a_caminho/entregue contam como faturamento;
cancelado e aguardando_pagamento NÃO contam.
"""
from datetime import datetime, timedelta
from decimal import Decimal


def _owner(app):
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='Dono', login='dono', papel='admin', is_owner=True)
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(u.id)
        s['_fresh'] = True
    return c


def _pedido(db, codigo, status, valor=Decimal('20'), criado_em=None):
    from app.models import Cliente, PedidoOnline
    cli = Cliente(nome='X', email=f'{codigo.lower()}@x.com')
    db.session.add(cli)
    db.session.flush()
    p = PedidoOnline(codigo=codigo, cliente_id=cli.id,
                     nome_cliente='X', email_cliente=cli.email,
                     telefone_cliente='', modo_entrega='retirada',
                     status=status,
                     subtotal=valor, frete_valor=Decimal('0'),
                     valor_total=valor)
    if criado_em:
        p.criado_em = criado_em
    db.session.add(p)
    db.session.commit()
    return p


def test_dashboard_rota_e_link_sidebar(app):
    """GET /admin/loja-online renderiza; link na sidebar."""
    c = _owner(app)
    r = c.get('/admin/loja-online')
    assert r.status_code == 200
    assert b'Loja Online' in r.data
    # Sidebar tem o link
    assert b'/admin/loja-online' in r.data


def test_faturamento_so_conta_pedidos_pagos(app):
    """Pago/em_preparo/a_caminho/entregue contam. Cancelado e
    aguardando_pagamento NÃO contam pro faturamento."""
    from app.extensions import db
    c = _owner(app)
    with app.app_context():
        # Hoje, R$ 100 em pedidos válidos
        _pedido(db, 'PG001', 'pago', valor=Decimal('30'))
        _pedido(db, 'EP001', 'em_preparo', valor=Decimal('40'))
        _pedido(db, 'EN001', 'entregue', valor=Decimal('30'))
        # Estes NÃO contam
        _pedido(db, 'CA001', 'cancelado', valor=Decimal('999'))
        _pedido(db, 'AG001', 'aguardando_pagamento', valor=Decimal('999'))
    r = c.get('/admin/loja-online')
    assert r.status_code == 200
    # R$ 100,00 aparece no card "Hoje"
    assert b'100,00' in r.data


def test_fila_admin_ignora_aguardando_pagamento(app):
    """A fila do admin mostra só pedidos que o admin tem que ATUAR
    (pago/em_preparo/a_caminho). Cliente não pagou ainda → não é fila do
    admin (cliente é que age)."""
    from app.extensions import db
    c = _owner(app)
    with app.app_context():
        _pedido(db, 'FAQAG', 'aguardando_pagamento')
        _pedido(db, 'FAPAG', 'pago')
    r = c.get('/admin/loja-online')
    assert b'FAPAG' in r.data
    assert b'FAQAG' not in r.data


def test_dashboard_sem_pedidos_renderiza(app):
    """Banco zerado: dashboard não estoura."""
    c = _owner(app)
    r = c.get('/admin/loja-online')
    assert r.status_code == 200
    assert b'0,00' in r.data   # faturamento zero


def test_pedidos_antigos_nao_entram_no_hoje(app):
    """Pedido criado em mês anterior aparece em 'Este mês'? Depende: do
    1º dia do mês até hoje. Pedido com `criado_em` no mês passado NÃO
    aparece nem em 'hoje' nem em 'mês'."""
    from app.extensions import db
    c = _owner(app)
    with app.app_context():
        passado = datetime.now() - timedelta(days=60)   # ~2 meses atrás
        _pedido(db, 'OLD01', 'pago', valor=Decimal('500'),
                criado_em=passado)
    r = c.get('/admin/loja-online')
    # 500 NÃO aparece nem em hoje nem em semana nem em mês
    assert b'500,00' not in r.data


def test_dashboard_exige_owner(app):
    """Sem login owner: 302 pra login admin."""
    c = app.test_client()
    r = c.get('/admin/loja-online', follow_redirects=False)
    assert r.status_code in (302, 401, 403)
