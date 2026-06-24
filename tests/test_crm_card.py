"""Card de CRM (histórico do cliente por telefone) embutido no Chatwoot.

Cobre: match de telefone em formatos BR diferentes, débito B2B, auth por
token, telefone desconhecido, e o header que libera o iframe pro Chatwoot.
"""
from datetime import date
from decimal import Decimal


def test_telefone_chave_formatos_brasileiros():
    from app.utils import telefone_chave
    alvo = telefone_chave('5511999998888')   # como o WhatsApp manda
    assert alvo != ''
    assert telefone_chave('(11) 99999-8888') == alvo
    assert telefone_chave('11999998888') == alvo
    assert telefone_chave('+55 11 99999-8888') == alvo
    assert telefone_chave('1199998888') == alvo   # formato antigo, sem o 9
    # sem DDD suficiente → vazio (não tenta adivinhar)
    assert telefone_chave('99998888') == ''
    assert telefone_chave('') == ''


def test_card_casa_pedido_local_por_telefone(app):
    from app.extensions import db
    from app.models import PedidoLocal, PedidoLocalItem
    app.config['CHATWOOT_CARD_TOKEN'] = 'segredo'
    with app.app_context():
        p = PedidoLocal(code='PL-1', destinatario='Maria',
                        telefone='(11) 99999-8888', endereco='Rua X',
                        data_entrega=date(2026, 6, 10))
        p.itens.append(PedidoLocalItem(nome='Brioche', quantidade=2,
                                       preco_unitario=10.0))
        db.session.add(p)
        db.session.commit()
    client = app.test_client()
    r = client.get('/crm/card.json?phone=5511999998888&k=segredo')
    assert r.status_code == 200
    j = r.get_json()
    assert j['encontrado'] is True
    assert len(j['pedidos_locais']) == 1
    assert j['pedidos_locais'][0]['code'] == 'PL-1'
    assert j['pedidos_locais'][0]['total'] == 20.0
    assert j['pedidos_locais'][0]['itens'][0]['nome'] == 'Brioche'


def test_card_mostra_debito_b2b(app):
    from app.extensions import db
    from app.models import ClienteB2B, VendaB2B, VendaB2BParcela
    app.config['CHATWOOT_CARD_TOKEN'] = 'segredo'
    with app.app_context():
        c = ClienteB2B(nome='Zion', telefone='11 98888-7777')
        db.session.add(c)
        db.session.flush()
        v = VendaB2B(cliente_id=c.id, data_venda=date(2026, 6, 1),
                     valor_total=Decimal('100.00'), status='ativa')
        db.session.add(v)
        db.session.flush()
        # pagou 40 de 100 → 60 em aberto
        db.session.add(VendaB2BParcela(venda_id=v.id, numero=1,
                       vencimento=date(2026, 6, 30), valor=Decimal('100.00'),
                       valor_pago=Decimal('40.00')))
        db.session.commit()
    client = app.test_client()
    r = client.get('/crm/card.json?phone=5511988887777&k=segredo')
    assert r.status_code == 200
    j = r.get_json()
    assert j['encontrado'] is True
    assert j['b2b']['nome'] == 'Zion'
    assert j['b2b']['debito_aberto'] == 60.0
    assert len(j['b2b']['vendas']) == 1


def test_card_telefone_desconhecido_vazio(app):
    app.config['CHATWOOT_CARD_TOKEN'] = 'segredo'
    client = app.test_client()
    r = client.get('/crm/card.json?phone=5511900000000&k=segredo')
    assert r.status_code == 200
    j = r.get_json()
    assert j['encontrado'] is False
    assert j['pedidos_locais'] == []
    assert j['b2b'] is None


def test_card_token_invalido_403(app):
    app.config['CHATWOOT_CARD_TOKEN'] = 'segredo'
    client = app.test_client()
    r = client.get('/crm/card.json?phone=5511999998888&k=errado')
    assert r.status_code == 403


def test_card_sem_token_configurado_nega(app):
    app.config['CHATWOOT_CARD_TOKEN'] = ''
    client = app.test_client()
    r = client.get('/crm/card.json?phone=5511999998888&k=qualquer')
    assert r.status_code == 403


def test_card_html_503_sem_token(app):
    app.config['CHATWOOT_CARD_TOKEN'] = ''
    client = app.test_client()
    assert client.get('/crm/card').status_code == 503


def test_card_html_ok_e_libera_iframe_chatwoot(app):
    app.config['CHATWOOT_CARD_TOKEN'] = 'segredo'
    app.config['CHATWOOT_URL'] = 'https://atendimento.exemplo.com'
    client = app.test_client()
    r = client.get('/crm/card?k=segredo')
    assert r.status_code == 200
    assert b'chatwoot-dashboard-app' in r.data
    # iframe cross-origin: X-Frame-Options removido, frame-ancestors com a URL
    assert 'X-Frame-Options' not in r.headers
    csp = r.headers.get('Content-Security-Policy', '')
    assert 'frame-ancestors' in csp
    assert 'atendimento.exemplo.com' in csp


def test_outras_rotas_mantem_x_frame_deny(app):
    """A exceção de iframe é SÓ pro /crm/card — o resto continua DENY."""
    app.config['CHATWOOT_URL'] = 'https://atendimento.exemplo.com'
    client = app.test_client()
    r = client.get('/auth/login')
    assert r.headers.get('X-Frame-Options') == 'DENY'
