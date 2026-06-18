"""Meus pedidos (Fase 6 — PR 2).

Foco em SEGURANÇA: cliente A jamais vê pedido do cliente B. Lista escopada
por `cliente_id`, detalhe 404 quando código é de outro cliente (não 403,
pra não confessar que existe).
"""
from decimal import Decimal
from unittest.mock import patch


def _cadastrar(client, email='maria@x.com', senha='senha-forte-1',
               nome='Maria'):
    return client.post('/loja/cadastrar', data={
        'nome': nome, 'email': email, 'telefone': '119',
        'senha': senha, 'aceite_lgpd': '1',
    }, follow_redirects=False)


def _pedido_pra(db, cliente, codigo='AAA111', status='pago'):
    from app.models import PedidoOnline, PedidoOnlineItem, Produto
    prod = Produto(nome='Pão', categoria='Pães', preco_site=10.0,
                   imagem_dropbox_url='https://x/p.jpg', ativo=True)
    db.session.add(prod)
    db.session.commit()
    p = PedidoOnline(codigo=codigo, cliente_id=cliente.id,
                     nome_cliente=cliente.nome, email_cliente=cliente.email,
                     telefone_cliente=cliente.telefone or '',
                     modo_entrega='retirada', status=status,
                     subtotal=Decimal('10'), frete_valor=Decimal('0'),
                     valor_total=Decimal('10'))
    db.session.add(p)
    db.session.flush()
    p.itens.append(PedidoOnlineItem(
        kind='produto', produto_id=prod.id, nome=prod.nome,
        preco_unitario=Decimal('10'), quantidade=1, subtotal=Decimal('10')))
    db.session.commit()
    return p


def test_lista_mostra_so_pedidos_do_cliente(app):
    """Cliente A NÃO vê pedidos de cliente B na lista (escopo por cliente_id)."""
    from app.extensions import db
    from app.models import Cliente
    c = app.test_client()
    _cadastrar(c, email='a@x.com', nome='A')
    with app.app_context():
        cli_a = Cliente.query.filter_by(email='a@x.com').first()
        cli_b = Cliente(nome='B', email='b@x.com')
        cli_b.set_senha('xxxxxxxx')
        db.session.add(cli_b)
        db.session.commit()
        _pedido_pra(db, cli_a, codigo='DOA111')
        _pedido_pra(db, cli_b, codigo='DOB222')
    r = c.get('/loja/conta/pedidos')
    assert r.status_code == 200
    assert b'DOA111' in r.data
    assert b'DOB222' not in r.data   # não vaza


def test_detalhe_pedido_proprio_abre(app):
    from app.extensions import db
    from app.models import Cliente
    c = app.test_client()
    _cadastrar(c, email='c@x.com')
    with app.app_context():
        cli = Cliente.query.filter_by(email='c@x.com').first()
        _pedido_pra(db, cli, codigo='OWN1A1')
    r = c.get('/loja/conta/pedidos/OWN1A1')
    assert r.status_code == 200
    assert b'OWN1A1' in r.data


def test_detalhe_pedido_alheio_da_404(app):
    """Cliente A tenta abrir pedido de B → 404 (não 403, pra não confessar).
    Sem isso, enumeração de códigos vazaria pedido de outro cliente."""
    from app.extensions import db
    from app.models import Cliente
    c_a = app.test_client()
    _cadastrar(c_a, email='aa@x.com')
    with app.app_context():
        cli_b = Cliente(nome='B', email='bb@x.com')
        cli_b.set_senha('xxxxxxxx')
        db.session.add(cli_b)
        db.session.commit()
        _pedido_pra(db, cli_b, codigo='ENEMY1')
    r = c_a.get('/loja/conta/pedidos/ENEMY1')
    assert r.status_code == 404


def test_meus_pedidos_exige_login(app, monkeypatch):
    """Anônimo → redirect pra /loja/entrar (não 200, não 404 — vai logar)."""
    monkeypatch.setenv('LOJA_VISIVEL', '1')
    c = app.test_client()
    r = c.get('/loja/conta/pedidos', follow_redirects=False)
    assert r.status_code == 302
    assert '/loja/entrar' in r.headers['Location']


def test_danfe_so_quando_emitida(app):
    """Pedido SEM NF emitida: clicar DANFE volta com mensagem, não 500."""
    from app.extensions import db
    from app.models import Cliente
    c = app.test_client()
    _cadastrar(c, email='dn@x.com')
    with app.app_context():
        cli = Cliente.query.filter_by(email='dn@x.com').first()
        _pedido_pra(db, cli, codigo='NONF11')
    with patch('app.services.tiny_nf.link_danfe', return_value=None):
        r = c.get('/loja/conta/pedidos/NONF11/nf', follow_redirects=False)
    assert r.status_code == 302
    assert '/conta/pedidos/NONF11' in r.headers['Location']


def test_danfe_redireciona_quando_disponivel(app):
    from app.extensions import db
    from app.models import Cliente
    c = app.test_client()
    _cadastrar(c, email='dn2@x.com')
    with app.app_context():
        cli = Cliente.query.filter_by(email='dn2@x.com').first()
        p = _pedido_pra(db, cli, codigo='OKNF11')
        p.tiny_nota_fiscal_id = 'nf-1'
        db.session.commit()
    with patch('app.services.tiny_nf.link_danfe',
               return_value='https://tiny/danfe.pdf'):
        r = c.get('/loja/conta/pedidos/OKNF11/nf', follow_redirects=False)
    assert r.status_code == 302
    assert r.headers['Location'] == 'https://tiny/danfe.pdf'


def test_danfe_pedido_alheio_da_404(app):
    """Cliente A tenta puxar DANFE do pedido de B → 404 (não vaza NF)."""
    from app.extensions import db
    from app.models import Cliente
    c_a = app.test_client()
    _cadastrar(c_a, email='aa2@x.com')
    with app.app_context():
        cli_b = Cliente(nome='B', email='bb2@x.com')
        cli_b.set_senha('xxxxxxxx')
        db.session.add(cli_b)
        db.session.commit()
        p = _pedido_pra(db, cli_b, codigo='ENEMY2')
        p.tiny_nota_fiscal_id = 'nf-vazio'
        db.session.commit()
    r = c_a.get('/loja/conta/pedidos/ENEMY2/nf', follow_redirects=False)
    assert r.status_code == 404


def test_lista_vazia_renderiza(app, monkeypatch):
    """Sem pedidos: mostra 'você ainda não tem pedidos' (não 500)."""
    monkeypatch.setenv('LOJA_VISIVEL', '1')
    c = app.test_client()
    _cadastrar(c, email='nov@x.com')
    r = c.get('/loja/conta/pedidos')
    assert r.status_code == 200
    assert b'ainda n' in r.data.lower()   # 'ainda não tem pedidos'


def test_detalhe_mostra_botao_nf_so_quando_emitida(app):
    from app.extensions import db
    from app.models import Cliente
    c = app.test_client()
    _cadastrar(c, email='bn@x.com')
    with app.app_context():
        cli = Cliente.query.filter_by(email='bn@x.com').first()
        p = _pedido_pra(db, cli, codigo='BOT111')
    # Sem nf_emitida_em → botão NÃO aparece
    r = c.get('/loja/conta/pedidos/BOT111')
    assert r.status_code == 200
    assert b'Ver nota fiscal' not in r.data
    # Com nf_emitida_em → botão APARECE
    with app.app_context():
        from app.models import PedidoOnline
        from app.utils import agora
        p = PedidoOnline.query.filter_by(codigo='BOT111').first()
        p.tiny_nota_fiscal_id = 'nf-1'
        p.nf_emitida_em = agora()
        db.session.commit()
    r = c.get('/loja/conta/pedidos/BOT111')
    assert b'Ver nota fiscal' in r.data
