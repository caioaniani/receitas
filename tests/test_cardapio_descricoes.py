"""Cardápio atacado — descrições sinceras por receita + métodos de preparo
(20/07/2026, ditado do dono: "descricao sincera de cada produto b2b, quanto
menos é mais... fala dos ingredientes... Colocar tambem os metodos de
preparo").

Peças: coluna `Receita.descricao_atacado` (seed ÚNICO na criação — depois a
ficha manda), descrição nos cards/listas do /cardapio?tipo=atacado (tela e
PDF) e o bloco "Métodos de preparo" (AppConfig com default no código:
chave ausente = default; gravada vazia = escondido de propósito).
"""
import pytest

from app.extensions import db


@pytest.fixture
def cliente(app):
    return app.test_client()


def _login(cliente, user):
    with cliente.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def _receita(nome='Brioche', **kw):
    from app.models import Receita
    base = dict(nome=nome, categoria='Pães', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0, preco_venda=18.0)
    base.update(kw)
    r = Receita(**base)
    db.session.add(r)
    db.session.commit()
    return r


# ── Backfill único (migração SQLite espelha o bloco Postgres) ──────────────

def test_backfill_seed_uma_vez_e_nao_sobrescreve(app):
    """Seed roda SÓ quando a coluna é criada; re-rodar a migração não
    sobrescreve o que o dono editou na ficha (senão todo boot desfazia a
    edição)."""
    from sqlalchemy import text

    from app.migrations_legacy import _migrate_sqlite
    _receita('Brioche')
    db.session.execute(text(
        'ALTER TABLE receita DROP COLUMN descricao_atacado'))
    db.session.commit()

    _migrate_sqlite(app)
    db.session.remove()
    v = db.session.execute(text(
        "SELECT descricao_atacado FROM receita WHERE nome='Brioche'"
    )).scalar()
    assert v and 'T45' in v and 'fresco' in v

    db.session.execute(text(
        "UPDATE receita SET descricao_atacado='editado pelo dono' "
        "WHERE nome='Brioche'"))
    db.session.commit()
    _migrate_sqlite(app)
    db.session.remove()
    v2 = db.session.execute(text(
        "SELECT descricao_atacado FROM receita WHERE nome='Brioche'"
    )).scalar()
    assert v2 == 'editado pelo dono'


# ── Montagem das categorias (fonte única tela+PDF) ─────────────────────────

def test_descricao_entra_so_no_tipo_atacado(app):
    """A descrição é do CARDÁPIO DE ATACADO — loja/site seguem sem
    descrição de receita (produto continua com a própria)."""
    from app.blueprints.main.routes import _cardapio_categorias
    _receita('Brioche', preco_loja=20.0,
             descricao_atacado='Farinha T45, manteiga e ovos.')
    with app.test_request_context():
        cats_atacado, _ = _cardapio_categorias('atacado')
        cats_loja, _ = _cardapio_categorias('loja')
    item_a = next(i for i in cats_atacado['Pães'] if i['nome'] == 'Brioche')
    item_l = next(i for i in cats_loja['Pães'] if i['nome'] == 'Brioche')
    assert item_a['descricao'] == 'Farinha T45, manteiga e ovos.'
    assert item_l['descricao'] is None


def test_tela_atacado_mostra_descricao(app, admin_user, cliente):
    _receita('Brioche', descricao_atacado='Farinha T45, manteiga e ovos.')
    _login(cliente, admin_user)
    body = cliente.get('/cardapio?tipo=atacado').get_data(as_text=True)
    assert 'Farinha T45, manteiga e ovos.' in body


# ── Ficha da receita ───────────────────────────────────────────────────────

def test_ficha_mostra_e_salva_descricao(app, admin_user, cliente):
    r = _receita('Brioche', descricao_atacado='antiga')
    _login(cliente, admin_user)
    body = cliente.get(f'/receitas/{r.id}').get_data(as_text=True)
    assert 'descricao_atacado' in body and 'antiga' in body

    resp = cliente.post(f'/receitas/{r.id}/salvar', data={
        'nome': 'Brioche', 'categoria': 'Pães',
        'descricao_atacado': '  Farinha T45, manteiga, ovos e açúcar.  ',
    })
    assert resp.status_code in (302, 303)
    db.session.refresh(r)
    assert r.descricao_atacado == 'Farinha T45, manteiga, ovos e açúcar.'

    # Vazio limpa (sem descrição no cardápio)
    cliente.post(f'/receitas/{r.id}/salvar', data={
        'nome': 'Brioche', 'categoria': 'Pães', 'descricao_atacado': '  ',
    })
    db.session.refresh(r)
    assert r.descricao_atacado is None


def test_form_sem_o_campo_nao_apaga(app, admin_user, cliente):
    """POST antigo/lote sem `descricao_atacado` no form NÃO zera o texto
    gravado (o salvar só mexe quando o campo veio no request)."""
    r = _receita('Brioche', descricao_atacado='fica')
    _login(cliente, admin_user)
    cliente.post(f'/receitas/{r.id}/salvar',
                 data={'nome': 'Brioche', 'categoria': 'Pães'})
    db.session.refresh(r)
    assert r.descricao_atacado == 'fica'


def test_duplicar_copia_descricao(app, admin_user, cliente):
    from app.models import Receita
    r = _receita('Brioche', descricao_atacado='desc do original')
    _login(cliente, admin_user)
    cliente.post(f'/receitas/{r.id}/duplicar')
    copia = Receita.query.filter_by(nome='Cópia de Brioche').first()
    assert copia is not None
    assert copia.descricao_atacado == 'desc do original'


# ── Métodos de preparo ─────────────────────────────────────────────────────

def test_preparo_default_sem_config(app):
    """Chave AUSENTE no AppConfig → default do código (os 4 métodos que o
    dono ditou: backup, assado congelado, sourdough 14 fatias, brioche)."""
    from app.blueprints.main.routes import _preparo_atacado
    metodos = _preparo_atacado()
    assert len(metodos) == 4
    labels = [m['label'] for m in metodos]
    assert any('backup' in (lb or '') for lb in labels)
    assert any('14 fatias' in m['valor'] for m in metodos)


def test_preparo_custom_e_vazio(app):
    from app.blueprints.main.routes import _preparo_atacado
    from app.models import AppConfig
    AppConfig.set('cardapio_atacado_preparo', 'Croissant: assar 12 min.')
    db.session.commit()
    metodos = _preparo_atacado()
    assert metodos == [{'label': 'Croissant', 'valor': 'assar 12 min.'}]

    # Gravada VAZIA = dono apagou de propósito → bloco some (≠ default)
    AppConfig.set('cardapio_atacado_preparo', '')
    db.session.commit()
    assert _preparo_atacado() == []


def test_preparo_linha_sem_rotulo(app):
    """':' tardio ou ausente = linha corrida, sem rótulo (não fatiar frase
    no meio)."""
    from app.blueprints.main.routes import _preparo_atacado
    from app.models import AppConfig
    AppConfig.set('cardapio_atacado_preparo',
                  'Retire do freezer e espere perder o gelo antes de assar')
    db.session.commit()
    metodos = _preparo_atacado()
    assert metodos[0]['label'] is None
    assert metodos[0]['valor'].startswith('Retire do freezer')


def test_tela_atacado_mostra_preparo_e_loja_nao(app, admin_user, cliente):
    _receita('Brioche')
    _login(cliente, admin_user)
    body = cliente.get('/cardapio?tipo=atacado').get_data(as_text=True)
    assert 'Métodos de preparo' in body
    assert 'método backup' in body
    body_loja = cliente.get('/cardapio?tipo=loja').get_data(as_text=True)
    assert 'Métodos de preparo' not in body_loja


def test_form_regras_salva_preparo(app, admin_user, cliente):
    from app.models import AppConfig
    _login(cliente, admin_user)
    body = cliente.get('/admin/cardapio-atacado/regras').get_data(as_text=True)
    assert 'name="preparo"' in body
    assert 'método backup' in body          # textarea pré-preenchida c/ default
    cliente.post('/admin/cardapio-atacado/regras',
                 data={'preparo': 'Sourdough: descongelar 2h.'})
    assert AppConfig.get('cardapio_atacado_preparo') == \
        'Sourdough: descongelar 2h.'


# ── PDF ────────────────────────────────────────────────────────────────────

def _cats(desc=None):
    return {'Pães': [{'nome': 'Sourdough', 'preco_venda': 20.0,
                      'descricao': desc, 'imagem_url': None, 'img_ref': None}]}


def test_pdf_cresce_com_descricao(app):
    from app.services.cardapio_pdf import gerar_cardapio_pdf
    sem = gerar_cardapio_pdf('atacado', _cats(), [])
    com = gerar_cardapio_pdf(
        'atacado', _cats('Farinha francesa T65, água, sal e levain. '
                         'Vendido congelado; rende 14 fatias.'), [])
    assert com.startswith(b'%PDF')
    assert len(com) > len(sem) + 30


def test_pdf_cresce_com_preparo(app):
    from app.services.cardapio_pdf import gerar_cardapio_pdf
    preparo = [{'label': 'Backup', 'valor': 'descongelar, egg wash, assar.'},
               {'label': None, 'valor': 'Brioche fresco, 3 dias.'}]
    sem = gerar_cardapio_pdf('atacado', _cats(), [], preparo=None)
    com = gerar_cardapio_pdf('atacado', _cats(), [], preparo=preparo)
    assert com.startswith(b'%PDF')
    assert len(com) > len(sem) + 50
    # No tipo loja o bloco NÃO entra mesmo se passado (regra só do atacado)
    loja = gerar_cardapio_pdf('loja', _cats(), [], preparo=preparo)
    assert loja.startswith(b'%PDF')


def test_altura_categoria_com_descricao():
    """Categoria com alguma descrição usa card/linha mais altos na conta do
    keep-together (senão a estimativa mente e a categoria quebra de página)."""
    from app.services.cardapio_pdf import (
        _CARD_H_DESC,
        _GAP,
        _LINHA_H_DESC,
        _altura_categoria,
    )
    seis = [{'nome': f'x{i}', 'preco_venda': 1,
             'descricao': 'desc'} for i in range(6)]
    assert abs(_altura_categoria(seis, True)
               - (12.5 + 2 * (_CARD_H_DESC + _GAP))) < 0.01
    assert abs(_altura_categoria(seis, False)
               - (12.5 + 3 * (_LINHA_H_DESC + 2) + 2)) < 0.01
