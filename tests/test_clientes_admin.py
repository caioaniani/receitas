"""Tela de clientes do varejo (/admin/clientes) — 13/07/2026.

Lista os cadastros do site + portal Wi-Fi, com busca, filtro de
aniversariantes e export XLSX pra campanhas. PII: admin+owner.

O fixture `app` já mantém um app-context ativo — NÃO abrir um
`with app.app_context()` aninhado aqui (fechá-lo expira o `admin_user` e
acessá-lo depois estoura ObjectDeletedError).
"""
from app.extensions import db


def _cliente(nome, email, telefone=None, senha=None, dia=None, mes=None):
    from app.models import Cliente
    c = Cliente(nome=nome, email=email, telefone=telefone,
                aniversario_dia=dia, aniversario_mes=mes)
    if senha:
        c.set_senha(senha)
    db.session.add(c)
    db.session.commit()
    return c


def _login(c, user_id):
    with c.session_transaction() as s:
        s['_user_id'] = str(user_id)
        s['_fresh'] = True


def test_exige_admin(app):
    assert app.test_client().get('/admin/clientes').status_code \
        in (302, 401, 403)


def test_lista_e_busca(app, admin_user):
    uid = admin_user.id
    _cliente('Maria Silva', 'maria@example.com', '11988887777',
             senha='x', dia=15, mes=3)
    _cliente('João Souza', 'joao@example.com', '11955554444')
    c = app.test_client()
    _login(c, uid)
    body = c.get('/admin/clientes').get_data(as_text=True)
    assert 'Maria Silva' in body and 'João Souza' in body
    # busca por nome filtra
    b2 = c.get('/admin/clientes?q=maria').get_data(as_text=True)
    assert 'Maria Silva' in b2 and 'João Souza' not in b2


def test_filtro_aniversariantes_e_so_conta(app, admin_user):
    uid = admin_user.id
    _cliente('Aniv Março', 'a@example.com', senha='x', dia=10, mes=3)
    _cliente('Aniv Maio', 'b@example.com', dia=20, mes=5)
    _cliente('Sem conta', 'c@example.com')
    c = app.test_client()
    _login(c, uid)
    r = c.get('/admin/clientes?aniv_mes=3').get_data(as_text=True)
    assert 'Aniv Março' in r and 'Aniv Maio' not in r
    r2 = c.get('/admin/clientes?conta=1').get_data(as_text=True)
    assert 'Aniv Março' in r2 and 'Sem conta' not in r2


def test_export_xlsx(app, admin_user):
    uid = admin_user.id
    _cliente('Maria Silva', 'maria@example.com', '11988887777',
             senha='x', dia=15, mes=3)
    c = app.test_client()
    _login(c, uid)
    r = c.get('/admin/clientes.xlsx')
    assert r.status_code == 200
    assert 'spreadsheet' in r.headers['Content-Type']
    assert r.headers['Content-Disposition'].endswith('clientes.xlsx')
    assert r.data[:2] == b'PK'          # zip (xlsx) magic
