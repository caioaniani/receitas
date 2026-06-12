"""Rota /admin/vnda/contatos: endereço + contato de varios pedidos VNDA
em uma pagina (owner-only). Criada em 12/06/2026 pro caso operacional
'preciso achar 11 clientes pra repor produto estragado'."""
from unittest.mock import patch


def _dono(app, login='dono_vc'):
    from app.extensions import db
    from app.models import Usuario
    with app.app_context():
        u = Usuario(nome='dono', login=login, papel='admin', is_owner=True)
        u.set_senha('senha123')
        db.session.add(u)
        db.session.commit()


def _fake_pedido(code, nome, fone, end, itens=None):
    return {
        'code': code,
        'client_id': 999,
        'items': [{'product_name': it['nome'], 'quantity': it['qtd'],
                   'sku': it.get('sku', 'X'), 'price': 10, 'subtotal': 10}
                  for it in (itens or [{'nome': 'Cesta', 'qtd': 1}])],
        'shipping_address': {'recipient_name': nome, 'phone': fone,
                              'street_name': end},
        'total': 100,
    }


def test_aberta_pra_qualquer_usuario_logado_mas_nao_anonimo(app):
    """Decisao do dono (12/06/2026): equipe operacional usa pra repor/
    contatar cliente — aberta a TODOS os logados (mesma classe de PII
    que /entregas/). Anonimo continua barrado."""
    from app.extensions import db
    from app.models import Usuario
    with app.app_context():
        u = Usuario(nome='func', login='func_vc', papel='funcionario',
                    is_owner=False)
        u.set_senha('senha123')
        db.session.add(u)
        db.session.commit()
    c = app.test_client()
    # anonimo: redirect pro login
    r = c.get('/admin/vnda/contatos?codes=A', follow_redirects=False)
    assert r.status_code in (302, 401)
    # funcionario logado: acessa
    c.post('/auth/login', data={'login': 'func_vc', 'senha': 'senha123'})
    with patch('app.services.vnda.buscar_pedido_completo',
               return_value=None):
        r = c.get('/admin/vnda/contatos?codes=A')
    assert r.status_code == 200


def test_data_de_entrega_respeita_override(app):
    """A data mostrada e a OPERACIONAL: se o admin alterou a data do
    pedido no nosso sistema (OverrideEntrega), ela prevalece sobre a do
    VNDA e vem marcada — repor produto no dia errado e desastre."""
    from app.extensions import db
    from app.models import OverrideEntrega
    _dono(app, login='dono_vc5')
    c = app.test_client()
    c.post('/auth/login', data={'login': 'dono_vc5', 'senha': 'senha123'})
    from datetime import date
    with app.app_context():
        db.session.add(OverrideEntrega(pedido_code='AAA',
                                       data_entrega=date(2026, 6, 20)))
        db.session.commit()
    pedido = _fake_pedido('AAA', 'Bia', '11 91234-5678', 'Rua X, 1')
    # VNDA diz 13/06; nosso override diz 20/06
    pedido['delivery_date'] = '2026-06-13'
    with patch('app.services.vnda.buscar_pedido_completo',
               return_value=pedido), \
         patch('app.services.vnda.buscar_shipping_address', return_value=None), \
         patch('app.services.vnda.buscar_cliente', return_value=None):
        r = c.get('/admin/vnda/contatos?codes=AAA&formato=json')
    d = r.get_json()
    cli = d['clientes'][0]
    assert cli['data_entrega'] == '20/06/2026'
    assert cli['data_alterada'] is True


def test_lista_contatos_com_destinatario_telefone_endereco(app):
    _dono(app)
    c = app.test_client()
    c.post('/auth/login', data={'login': 'dono_vc', 'senha': 'senha123'})
    pedidos = {
        '5B1A3766F7': _fake_pedido('5B1A3766F7', 'Bethania Mendes',
                                    '(11) 91234-5678',
                                    'Rua A, 100, São Paulo'),
        '7666044AD1': _fake_pedido('7666044AD1', 'Erik Lima',
                                    '(11) 99876-5432',
                                    'Av. B, 200'),
    }
    with patch('app.services.vnda.buscar_pedido_completo',
               side_effect=lambda code: pedidos.get(code)), \
         patch('app.services.vnda.buscar_shipping_address',
               return_value=None), \
         patch('app.services.vnda.buscar_cliente', return_value=None):
        r = c.get('/admin/vnda/contatos?codes=5B1A3766F7,7666044AD1'
                  '&formato=json')
    assert r.status_code == 200
    d = r.get_json()
    assert d['total'] == 2
    assert d['achados'] == 2
    assert d['nao_achados'] == []
    nomes = [x['destinatario'] for x in d['clientes']]
    assert 'Bethania Mendes' in nomes
    assert 'Erik Lima' in nomes
    bm = next(x for x in d['clientes']
              if x['destinatario'] == 'Bethania Mendes')
    assert '91234-5678' in bm['telefone']
    assert 'Rua A' in bm['endereco']


def test_codes_aceita_separadores_variados_e_dedup(app):
    """Cola do print = mistura virgula, espaco, quebra de linha. E codes
    repetidos so consultam 1 vez (proteg API VNDA)."""
    _dono(app, login='dono_vc2')
    c = app.test_client()
    c.post('/auth/login', data={'login': 'dono_vc2', 'senha': 'senha123'})
    chamados = []

    def fake_busca(code):
        chamados.append(code)
        return _fake_pedido(code, 'X', '11 99999-0000', 'Rua X, 1')

    with patch('app.services.vnda.buscar_pedido_completo',
               side_effect=fake_busca), \
         patch('app.services.vnda.buscar_shipping_address', return_value=None), \
         patch('app.services.vnda.buscar_cliente', return_value=None):
        # virgula + espaco + quebra de linha + duplicata
        r = c.get('/admin/vnda/contatos'
                  '?codes=A1, B2%0AC3 D4%0A%0AA1&formato=json')
    assert r.status_code == 200
    d = r.get_json()
    assert sorted(chamados) == ['A1', 'B2', 'C3', 'D4']
    assert d['achados'] == 4


def test_code_nao_existe_vai_pra_nao_achados(app):
    """Se um code retorna None do VNDA (errado ou cancelado), entra na
    lista de 'nao achados' — dono sabe quais re-conferir (ex: O vs 0)."""
    _dono(app, login='dono_vc3')
    c = app.test_client()
    c.post('/auth/login', data={'login': 'dono_vc3', 'senha': 'senha123'})

    def fake_busca(code):
        if code == 'BOM':
            return _fake_pedido(code, 'Ana', '11 9999', 'Rua A')
        return None

    with patch('app.services.vnda.buscar_pedido_completo',
               side_effect=fake_busca), \
         patch('app.services.vnda.buscar_shipping_address', return_value=None), \
         patch('app.services.vnda.buscar_cliente', return_value=None):
        r = c.get('/admin/vnda/contatos?codes=BOM,RUIM&formato=json')
    d = r.get_json()
    assert d['achados'] == 1
    assert d['nao_achados'] == ['RUIM']


def test_html_default_mostra_link_de_whatsapp(app):
    """HTML default e operacional: telefone clicavel + botao WhatsApp +
    botao imprimir."""
    _dono(app, login='dono_vc4')
    c = app.test_client()
    c.post('/auth/login', data={'login': 'dono_vc4', 'senha': 'senha123'})
    with patch('app.services.vnda.buscar_pedido_completo',
               return_value=_fake_pedido('AAA', 'Bia',
                                          '(11) 91234-5678',
                                          'Rua X, 1')), \
         patch('app.services.vnda.buscar_shipping_address', return_value=None), \
         patch('app.services.vnda.buscar_cliente', return_value=None):
        r = c.get('/admin/vnda/contatos?codes=AAA')
    assert r.status_code == 200
    body = r.data.decode()
    assert 'Bia' in body
    assert 'Rua X' in body
    assert 'https://wa.me/55' in body
    assert 'window.print()' in body
