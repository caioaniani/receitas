"""Cardápio — ordem das seções por drag-and-drop (21/07/2026, pedido do
dono: "alterar as sessões com um grab and drop em editar regras").

Duas listas em AppConfig (JSON): `cardapio_ordem_categorias` (ordem das
categorias — aplicada na fonte única `_cardapio_categorias`, então tela e
PDFs saem iguais) e `cardapio_ordem_rodape` (ordem dos blocos quem_somos/
regras/preparo). Sem preferência salva, tudo fica como era: categorias em
alfabética com 'Outros' por último; rodapé quem_somos → regras → preparo.
"""
import json

import pytest

from app.extensions import db


@pytest.fixture
def cliente(app):
    return app.test_client()


def _login(cliente, user):
    with cliente.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def _receita(nome, categoria, **kw):
    from app.models import Receita
    base = dict(nome=nome, categoria=categoria, rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0, preco_venda=18.0)
    base.update(kw)
    r = Receita(**base)
    db.session.add(r)
    db.session.commit()
    return r


# ── Helpers ────────────────────────────────────────────────────────────────

def test_ordem_categorias_default_alfabetica_outros_ultimo(app):
    from app.blueprints.main.routes import _aplicar_ordem_categorias
    cats = {'Outros': [1], 'Pães': [2], 'Doces': [3]}
    assert list(_aplicar_ordem_categorias(cats)) == \
        ['Doces', 'Pães', 'Outros']


def test_ordem_categorias_salva_manda_e_nova_vai_pro_fim(app):
    from app.blueprints.main.routes import _aplicar_ordem_categorias
    from app.models import AppConfig
    AppConfig.set('cardapio_ordem_categorias',
                  json.dumps(['Pães', 'Doces']))
    db.session.commit()
    cats = {'Doces': [1], 'Bebidas': [2], 'Pães': [3], 'Outros': [4]}
    # Salvas primeiro na ordem do dono; novas depois (alfabética, Outros
    # por último — nunca somem).
    assert list(_aplicar_ordem_categorias(cats)) == \
        ['Pães', 'Doces', 'Bebidas', 'Outros']


def test_ordem_secoes_default_custom_e_invalida(app):
    """Default 21/07 (dono: "o rodapé venha para cima"): blocos ANTES dos
    produtos — SUBSTITUI a regra de 20/07 "produtos para cima". Seção fora
    da lista salva (ex.: 'produtos' pra ordem salva antes de 21/07) entra
    no fim, na ordem default."""
    from app.blueprints.main.routes import _ordem_secoes
    from app.models import AppConfig
    assert _ordem_secoes() == ['quem_somos', 'regras', 'preparo', 'produtos']

    AppConfig.set('cardapio_ordem_rodape',
                  json.dumps(['produtos', 'preparo', 'quem_somos']))
    db.session.commit()
    assert _ordem_secoes() == ['produtos', 'preparo', 'quem_somos', 'regras']

    AppConfig.set('cardapio_ordem_rodape',
                  json.dumps(['preparo', 'quem_somos']))
    db.session.commit()
    assert _ordem_secoes() == ['preparo', 'quem_somos', 'regras', 'produtos']

    AppConfig.set('cardapio_ordem_rodape', 'não é json')
    db.session.commit()
    assert _ordem_secoes() == ['quem_somos', 'regras', 'preparo', 'produtos']


# ── Tela do cardápio ───────────────────────────────────────────────────────

def test_tela_categorias_na_ordem_salva(app, admin_user, cliente):
    from app.models import AppConfig
    _receita('Bolo', 'Doces')
    _receita('Sourdough', 'Pães')
    AppConfig.set('cardapio_ordem_categorias', json.dumps(['Pães', 'Doces']))
    db.session.commit()
    _login(cliente, admin_user)
    body = cliente.get('/cardapio?tipo=atacado').get_data(as_text=True)
    assert body.index('Pães') < body.index('Doces')

    AppConfig.set('cardapio_ordem_categorias', json.dumps(['Doces', 'Pães']))
    db.session.commit()
    body = cliente.get('/cardapio?tipo=atacado').get_data(as_text=True)
    assert body.index('Doces') < body.index('Pães')


def test_tela_rodape_na_ordem_salva(app, admin_user, cliente):
    from app.models import AppConfig
    _receita('Bolo', 'Doces')
    AppConfig.set('cardapio_atacado_pedido_minimo', 'R$ 500,00')
    AppConfig.set('cardapio_ordem_rodape',
                  json.dumps(['preparo', 'regras', 'quem_somos']))
    db.session.commit()
    _login(cliente, admin_user)
    body = cliente.get('/cardapio?tipo=atacado').get_data(as_text=True)
    assert body.index('Métodos de preparo') < body.index('Regras do pedido')
    assert body.index('Regras do pedido') < body.index('Quem somos nós')


def test_tela_default_blocos_antes_dos_produtos(app, admin_user, cliente):
    """Default 21/07: sem ordem salva, a história vem ANTES dos produtos
    ("o rodapé venha para cima" — substitui a regra de 20/07)."""
    _receita('Bolo', 'Doces')
    _login(cliente, admin_user)
    body = cliente.get('/cardapio?tipo=atacado').get_data(as_text=True)
    assert body.index('Quem somos nós') < body.index('cat-heading')


def test_tela_produtos_arrastado_pro_topo(app, admin_user, cliente):
    """'produtos' na frente da ordem salva → cardápio antes dos blocos
    (volta ao layout de 20/07, agora por escolha arrastável)."""
    from app.models import AppConfig
    _receita('Bolo', 'Doces')
    AppConfig.set('cardapio_ordem_rodape',
                  json.dumps(['produtos', 'quem_somos', 'regras',
                              'preparo']))
    db.session.commit()
    _login(cliente, admin_user)
    body = cliente.get('/cardapio?tipo=atacado').get_data(as_text=True)
    assert body.index('cat-heading') < body.index('Quem somos nós')


# ── Tela de regras (edição) ────────────────────────────────────────────────

def test_form_regras_mostra_listas_e_salva_ordem(app, admin_user, cliente):
    from app.models import AppConfig
    _receita('Bolo', 'Doces')
    _receita('Sourdough', 'Pães')
    _login(cliente, admin_user)
    body = cliente.get('/admin/cardapio-atacado/regras').get_data(as_text=True)
    assert 'name="ordem_categorias"' in body
    assert 'name="ordem_secoes"' in body
    assert 'ordem-sortavel' in body
    assert 'Seções da página' in body
    assert 'data-key="produtos"' in body

    cliente.post('/admin/cardapio-atacado/regras', data={
        'ordem_categorias': json.dumps(['Pães', 'Doces']),
        'ordem_secoes': json.dumps(['produtos', 'regras', 'preparo',
                                    'quem_somos']),
    })
    assert json.loads(AppConfig.get('cardapio_ordem_categorias')) == \
        ['Pães', 'Doces']
    assert json.loads(AppConfig.get('cardapio_ordem_rodape')) == \
        ['produtos', 'regras', 'preparo', 'quem_somos']


def test_post_sem_campos_nao_apaga_ordem(app, admin_user, cliente):
    from app.models import AppConfig
    AppConfig.set('cardapio_ordem_categorias', json.dumps(['Pães']))
    db.session.commit()
    _login(cliente, admin_user)
    cliente.post('/admin/cardapio-atacado/regras', data={'prazo': 'até 14h'})
    assert json.loads(AppConfig.get('cardapio_ordem_categorias')) == ['Pães']


def test_post_ordem_invalida_nao_sobrescreve(app, admin_user, cliente):
    from app.models import AppConfig
    AppConfig.set('cardapio_ordem_rodape', json.dumps(['preparo']))
    db.session.commit()
    _login(cliente, admin_user)
    for lixo in ('{quebrado', '"escalar"', '123'):
        cliente.post('/admin/cardapio-atacado/regras',
                     data={'ordem_secoes': lixo})
        assert json.loads(AppConfig.get('cardapio_ordem_rodape')) == \
            ['preparo'], lixo
    # '' = navegador sem JS (hidden nunca preenchido): ignora em silêncio,
    # sem flash de erro e sem apagar a ordem salva.
    resp = cliente.post('/admin/cardapio-atacado/regras',
                        data={'ordem_secoes': ''}, follow_redirects=True)
    assert json.loads(AppConfig.get('cardapio_ordem_rodape')) == ['preparo']
    assert 'veio inválida' not in resp.get_data(as_text=True)


def test_post_ordem_dedupe_e_valor_gravado_duplicado(app, admin_user,
                                                     cliente):
    """POST com repetido salva deduplicado; valor JÁ gravado com repetido
    (set manual antigo) não desenha o bloco 2x."""
    from app.blueprints.main.routes import _ordem_secoes
    from app.models import AppConfig
    _login(cliente, admin_user)
    cliente.post('/admin/cardapio-atacado/regras', data={
        'ordem_secoes': json.dumps(['preparo', 'preparo', 'regras',
                                    'quem_somos'])})
    assert json.loads(AppConfig.get('cardapio_ordem_rodape')) == \
        ['preparo', 'regras', 'quem_somos']

    AppConfig.set('cardapio_ordem_rodape',
                  json.dumps(['preparo', 'preparo']))
    db.session.commit()
    assert _ordem_secoes() == ['preparo', 'quem_somos', 'regras',
                               'produtos']


def test_categoria_adormecida_continua_na_lista_de_ordenar(app, admin_user,
                                                           cliente):
    """Categoria na ordem salva SEM item precificado hoje continua
    aparecendo na lista arrastável — um save qualquer não apaga a posição
    dela (achado do revisor)."""
    from app.models import AppConfig
    _receita('Bolo', 'Doces')
    AppConfig.set('cardapio_ordem_categorias',
                  json.dumps(['Bebidas', 'Doces']))
    db.session.commit()
    _login(cliente, admin_user)
    body = cliente.get('/admin/cardapio-atacado/regras').get_data(as_text=True)
    assert 'data-key="Bebidas"' in body


# ── PDF ────────────────────────────────────────────────────────────────────

def test_pdf_respeita_ordem(app):
    """PDF itera as categorias na ordem de INSERÇÃO do dict (a fonte única
    já ordena) e aceita ordem_rodape custom sem quebrar."""
    from app.services.cardapio_pdf import gerar_cardapio_pdf
    cats = {'Pães': [{'nome': 'Sourdough', 'preco_venda': 20.0,
                      'descricao': None, 'imagem_url': None,
                      'img_ref': None}],
            'Doces': [{'nome': 'Bolo', 'preco_venda': 12.0,
                       'descricao': None, 'imagem_url': None,
                       'img_ref': None}]}
    pdf = gerar_cardapio_pdf(
        'atacado', cats, [{'label': 'Prazo', 'valor': 'até 14h'}],
        quem_somos=['Nossa história.'],
        preparo=[{'label': None, 'valor': 'assar.'}],
        ordem_rodape=['preparo', 'regras', 'quem_somos'])
    assert pdf.startswith(b'%PDF')
