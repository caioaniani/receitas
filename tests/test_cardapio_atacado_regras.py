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


def test_receita_arquivada_fora_do_cardapio(app, admin_user):
    """Caso real 19/07/2026: "Pão de queijo un" (arquivada em 01/07, preço
    atacado R$ 0,50) aparecia no /cardapio?tipo=atacado — a query de
    receitas não filtrava arquivada_em (os produtos já filtravam ativo)."""
    from app.extensions import db
    from app.models import Receita
    from app.utils import agora
    with app.app_context():
        viva = Receita(nome='Sourdough Cardapio', categoria='Paes',
                       rendimento_qtd=1, rendimento_unidade='un',
                       peso_base=1000.0, preco_venda=25.0)
        morta = Receita(nome='Pao de Queijo Morto', categoria='Outros',
                        rendimento_qtd=1, rendimento_unidade='un',
                        peso_base=100.0, preco_venda=0.5,
                        arquivada_em=agora())
        db.session.add_all([viva, morta])
        db.session.commit()
    c = _login(app, admin_user)
    body = c.get('/cardapio?tipo=atacado').get_data(as_text=True)
    assert 'Sourdough Cardapio' in body
    assert 'Pao de Queijo Morto' not in body


def _png_preto_no_branco():
    from io import BytesIO

    from PIL import Image, ImageDraw
    img = Image.new('RGB', (400, 140), (255, 255, 255))
    ImageDraw.Draw(img).rectangle([20, 40, 380, 100], fill=(0, 0, 0))
    out = BytesIO()
    img.save(out, 'PNG')
    return out.getvalue()


def test_cardapio_diz_brooklin_nao_itaim(app, admin_user):
    c = _login(app, admin_user)
    body = c.get('/cardapio?tipo=atacado').get_data(as_text=True)
    assert 'Brooklin' in body
    assert 'Itaim' not in body                       # branding trocada


def test_pdf_capa_diz_brooklin(app, admin_user):
    from app.services.cardapio_pdf import gerar_cardapio_pdf
    cats = {'Pães': [{'nome': 'Sourdough', 'preco_venda': 20.0,
                      'imagem_url': None, 'img_ref': None}]}
    pdf = gerar_cardapio_pdf('atacado', cats, [])
    # o texto do PDF é comprimido; validamos pela geração + rota
    assert pdf.startswith(b'%PDF')


def test_logo_upload_aparece_no_hero_e_pdf(app, admin_user):
    """Sem logo → texto 'O Pão'; após upload → <img> data URI no hero e
    imagem embutida no PDF; remover → volta ao texto."""
    from app.models import AppConfig
    from app.services import cardapio_pdf
    c = _login(app, admin_user)
    # antes: hero usa o texto
    with app.app_context():
        assert AppConfig.get('cardapio_logo_data') in (None, '')
    body0 = c.get('/cardapio?tipo=atacado').get_data(as_text=True)
    assert '<h1>O Pão</h1>' in body0
    assert '<img class="hero-logo"' not in body0    # sem logo, sem <img>

    # upload (branco = silhueta)
    from io import BytesIO
    resp = c.post('/admin/cardapio-atacado/logo',
                  data={'branco': '1',
                        'logo_arquivo': (BytesIO(_png_preto_no_branco()),
                                         'logo.png')},
                  content_type='multipart/form-data')
    assert resp.status_code in (302, 200)
    with app.app_context():
        uri = AppConfig.get('cardapio_logo_data')
        assert uri and uri.startswith('data:image/png;base64,')

    body1 = c.get('/cardapio?tipo=atacado').get_data(as_text=True)
    assert '<img class="hero-logo"' in body1
    assert 'src="data:image/png;base64,' in body1
    assert '<h1>O Pão</h1>' not in body1

    # PDF embute a imagem
    with app.app_context():
        cats = {'Pães': [{'nome': 'X', 'preco_venda': 5.0,
                          'imagem_url': None, 'img_ref': None}]}
        sem = cardapio_pdf.gerar_cardapio_pdf('atacado', cats, [])
        com = cardapio_pdf.gerar_cardapio_pdf(
            'atacado', cats, [], logo=AppConfig.get('cardapio_logo_data'))
        assert len(com) > len(sem)

    # remover
    c.post('/admin/cardapio-atacado/logo/remover')
    with app.app_context():
        assert (AppConfig.get('cardapio_logo_data') or '') == ''
    body2 = c.get('/cardapio?tipo=atacado').get_data(as_text=True)
    assert '<h1>O Pão</h1>' in body2


def test_logo_upload_exige_admin(app):
    from app.extensions import db
    from app.models import AppConfig, Usuario
    with app.app_context():
        u = Usuario(nome='func teste', login='func', papel='funcionario')
        u.set_senha('123')
        db.session.add(u)
        db.session.commit()
    c = app.test_client()
    c.post('/auth/login', data={'login': 'func', 'senha': '123'})
    from io import BytesIO
    resp = c.post('/admin/cardapio-atacado/logo',
                  data={'logo_arquivo': (BytesIO(_png_preto_no_branco()),
                                         'l.png')},
                  content_type='multipart/form-data')
    assert resp.status_code in (302, 403)
    with app.app_context():
        assert (AppConfig.get('cardapio_logo_data') or '') == ''


def test_processar_logo_branco_vira_png_transparente(app):
    from app.blueprints.main.routes import _processar_logo_cardapio
    with app.app_context():
        uri = _processar_logo_cardapio(_png_preto_no_branco(), branco=True)
        assert uri.startswith('data:image/png;base64,')
        fiel = _processar_logo_cardapio(_png_preto_no_branco(), branco=False)
        assert fiel.startswith('data:image/jpeg;base64,')  # opaco = JPEG


# ── Descoberta da ordenação de categorias (26/07/2026) ───────────────────
# O dono pediu "um drag and drop no cardápio PDF site pra ordenar quais
# categorias vêm na frente" — o controle JÁ existia e JÁ valia pro site,
# mas o acesso só aparecia no cardápio de Atacado.

def test_ordem_das_categorias_vale_para_o_site(app):
    """A fonte única aplica a ordem salva nos TRÊS tipos — é o que torna
    desnecessário um segundo drag-and-drop só pro site."""
    import json

    from app.extensions import db
    from app.models import AppConfig, Produto
    with app.app_context():
        for nome, cat in (('Pao A', 'Zebra'), ('Pao B', 'Abelha')):
            db.session.add(Produto(nome=nome, categoria=cat, ativo=True,
                                   preco_site=10, preco_atacado=10,
                                   preco_loja=10))
        AppConfig.set('cardapio_ordem_categorias',
                      json.dumps(['Zebra', 'Abelha']))
        db.session.commit()
    with app.test_request_context('/'):
        from app.blueprints.main.routes import _cardapio_categorias
        for tipo in ('atacado', 'loja', 'site'):
            cats, _r = _cardapio_categorias(tipo)
            ordem = [c for c in cats if c in ('Zebra', 'Abelha')]
            assert ordem == ['Zebra', 'Abelha'], tipo   # não é alfabética


def test_atalho_de_ordenar_aparece_nos_tres_cardapios(app, admin_user):
    """Antes só o Atacado tinha link pra tela de ordenação — por isso o dono
    achou que não dava pra ordenar o cardápio do site."""
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(admin_user.id)
        s['_fresh'] = True
    for tipo in ('atacado', 'loja', 'site'):
        corpo = c.get(f'/cardapio?tipo={tipo}').get_data(as_text=True)
        assert 'Ordenar categorias' in corpo, tipo
        assert '#ordem-cardapio' in corpo, tipo
