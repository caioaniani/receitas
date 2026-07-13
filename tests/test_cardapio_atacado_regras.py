"""Regras do pedido de atacado no cardápio (13/07/2026) — texto editável pelo
dono que aparece no /cardapio?tipo=atacado e sai na impressão."""


def _login(app, user):
    c = app.test_client()
    c.post('/auth/login', data={'login': user.login, 'senha': '123'})
    return c


def test_regras_atacado_helper_so_preenchidas_em_ordem(app):
    from app.blueprints.main.routes import _regras_atacado
    from app.extensions import db
    from app.models import AppConfig
    with app.app_context():
        AppConfig.set('cardapio_atacado_pedido_minimo', 'R$ 300,00')
        AppConfig.set('cardapio_atacado_prazo', '')          # vazio = fora
        AppConfig.set('cardapio_atacado_contato', 'WhatsApp 11 90000-0000')
        db.session.commit()
        regras = _regras_atacado()
        labels = [r['label'] for r in regras]
        # só as preenchidas, e na ordem de CARDAPIO_ATACADO_CAMPOS
        assert labels == ['Pedido mínimo', 'Pedidos e contato']
        assert regras[0]['valor'] == 'R$ 300,00'


def test_post_salva_e_aparece_no_cardapio_atacado(app, admin_user):
    c = _login(app, admin_user)
    resp = c.post('/admin/cardapio-atacado/regras',
                  data={'pedido_minimo': 'R$ 250,00',
                        'entrega': 'terça a sábado de manhã'},
                  follow_redirects=False)
    assert resp.status_code == 302                          # PRG pro cardápio
    body = c.get('/cardapio?tipo=atacado').get_data(as_text=True)
    assert 'Regras do pedido' in body
    assert 'R$ 250,00' in body and 'terça a sábado de manhã' in body


def test_regras_so_no_atacado(app, admin_user):
    from app.extensions import db
    from app.models import AppConfig
    with app.app_context():
        AppConfig.set('cardapio_atacado_pedido_minimo', 'R$ 300,00')
        db.session.commit()
    c = _login(app, admin_user)
    assert 'Regras do pedido' in c.get('/cardapio?tipo=atacado').get_data(as_text=True)
    assert 'Regras do pedido' not in c.get('/cardapio?tipo=loja').get_data(as_text=True)
    assert 'Regras do pedido' not in c.get('/cardapio?tipo=site').get_data(as_text=True)


def test_edicao_carrega_valores_salvos(app, admin_user):
    from app.extensions import db
    from app.models import AppConfig
    with app.app_context():
        AppConfig.set('cardapio_atacado_pagamento', 'boleto 14 dias')
        db.session.commit()
    c = _login(app, admin_user)
    body = c.get('/admin/cardapio-atacado/regras').get_data(as_text=True)
    assert 'boleto 14 dias' in body


def test_edicao_barra_anonimo(app):
    # sem login, admin_required barra (nunca 200)
    assert app.test_client().get('/admin/cardapio-atacado/regras').status_code != 200


def test_impressao_mostra_todas_categorias(app, admin_user):
    """O CSS de print revela todas as .cat-section (antes só a ativa saía)."""
    c = _login(app, admin_user)
    body = c.get('/cardapio?tipo=atacado').get_data(as_text=True)
    # Frouxo de propósito: trava o COMPORTAMENTO (print revela as categorias)
    # sem acoplar ao espaçamento exato do CSS.
    assert '@media print' in body
    import re
    assert re.search(r'\.cat-section\s*\{[^}]*display:\s*block\s*!important', body)
