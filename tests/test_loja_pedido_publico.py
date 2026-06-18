"""Acompanhar pedido pelo código — sem login (Fase 6 — PR 8).

Link do email transacional funciona pra cliente guest também. Escope
pelo CÓDIGO do pedido (random hex 8 = 4 bilhões, não enumerável).
"""
from decimal import Decimal
from unittest.mock import patch


def _pedido_simples(db, codigo='PUB001', status='pago', nf=False):
    from app.models import Cliente, PedidoOnline
    cli = Cliente(nome='C', email=f'{codigo.lower()}@x.com')
    db.session.add(cli)
    db.session.flush()
    p = PedidoOnline(codigo=codigo, cliente_id=cli.id,
                     nome_cliente='C', email_cliente=cli.email,
                     telefone_cliente='', modo_entrega='retirada',
                     status=status,
                     subtotal=Decimal('10'), frete_valor=Decimal('0'),
                     valor_total=Decimal('10'))
    if nf:
        from app.utils import agora
        p.tiny_nota_fiscal_id = 'nf-1'
        p.nf_emitida_em = agora()
    db.session.add(p)
    db.session.commit()
    return p


def test_anonimo_abre_pedido_pelo_codigo(app):
    """Anônimo em modo teste consegue /loja/pedido/<codigo> (link do email)."""
    from app.extensions import db
    c = app.test_client()
    with app.app_context():
        _pedido_simples(db, codigo='ABRIR1')
    r = c.get('/loja/pedido/ABRIR1')
    assert r.status_code == 200
    assert b'ABRIR1' in r.data


def test_codigo_inexistente_da_404(app):
    c = app.test_client()
    r = c.get('/loja/pedido/NAOEXISTE')
    assert r.status_code == 404


def test_pedido_com_nf_emitida_mostra_botao_pdf(app):
    from app.extensions import db
    c = app.test_client()
    with app.app_context():
        _pedido_simples(db, codigo='COMNF1', nf=True)
    r = c.get('/loja/pedido/COMNF1')
    assert r.status_code == 200
    assert b'Ver nota fiscal' in r.data


def test_danfe_publico_redireciona_quando_disponivel(app):
    from app.extensions import db
    c = app.test_client()
    with app.app_context():
        _pedido_simples(db, codigo='DANFE1', nf=True)
    with patch('app.services.tiny_nf.link_danfe',
               return_value='https://tiny/danfe.pdf'):
        r = c.get('/loja/pedido/DANFE1/nf', follow_redirects=False)
    assert r.status_code == 302
    assert r.headers['Location'] == 'https://tiny/danfe.pdf'


def test_danfe_sem_link_volta_pro_pedido(app):
    from app.extensions import db
    c = app.test_client()
    with app.app_context():
        _pedido_simples(db, codigo='SEMNF1')
    with patch('app.services.tiny_nf.link_danfe', return_value=None):
        r = c.get('/loja/pedido/SEMNF1/nf', follow_redirects=False)
    assert r.status_code == 302
    assert '/pedido/SEMNF1' in r.headers['Location']


def test_status_atual_aparece_no_template(app):
    from app.extensions import db
    c = app.test_client()
    with app.app_context():
        _pedido_simples(db, codigo='STAT1', status='a_caminho')
    r = c.get('/loja/pedido/STAT1')
    assert r.status_code == 200
    assert b'caminho' in r.data.lower()
