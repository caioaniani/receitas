"""A lista de Fichas Técnicas (receitas.padeiro_lista) leva o ADMIN pra ficha
editável (receitas.ficha); padeiro/funcionário continua na calculadora
read-only (receitas.padeiro)."""
from app.extensions import db
from app.models import Receita, Usuario


def _receita(app, nome='Pão Teste'):
    with app.app_context():
        r = Receita(nome=nome, categoria='Paes', rendimento_qtd=10,
                    rendimento_unidade='un', peso_base=1000.0)
        db.session.add(r)
        db.session.commit()
        return r.id


def _usuario(app, login, papel):
    with app.app_context():
        u = Usuario(nome=login.capitalize(), login=login, papel=papel)
        u.set_senha('123')
        db.session.add(u)
        db.session.commit()
        return u.id


def test_admin_lista_aponta_pra_ficha_editavel(app, admin_user):
    rid = _receita(app, 'Pão Francês')
    c = app.test_client()
    c.post('/auth/login', data={'login': 'admin', 'senha': '123'})
    pg = c.get('/receitas/padeiro')
    html = pg.get_data(as_text=True)
    assert 'Fichas Técnicas' in html
    # card aponta pra ficha editável (/receitas/<id>), NÃO pra calculadora
    assert ('href="/receitas/%d"' % rid) in html
    assert ('href="/receitas/%d/padeiro"' % rid) not in html


def test_padeiro_lista_aponta_pra_calculadora(app):
    rid = _receita(app, 'Pão Francês')
    _usuario(app, 'pad', 'padeiro')
    c = app.test_client()
    c.post('/auth/login', data={'login': 'pad', 'senha': '123'})
    pg = c.get('/receitas/padeiro')
    html = pg.get_data(as_text=True)
    # não-admin: calculadora read-only, sem virar tela de edição
    assert ('href="/receitas/%d/padeiro"' % rid) in html
    assert 'Calculadora de Produção' in html
