"""Lousa dos padeiros (11/07/2026, pedido do dono): aba na tela do padeiro
onde eles escrevem recados pros colegas de turno — como giz numa lousa,
fica até alguém apagar. NÃO confundir com o Aviso (alarme com campainha):
a lousa não apita nem exige confirmação.
"""
from app.extensions import db
from app.models import LousaRecado, Usuario


def _login_admin(c):
    return c.post('/auth/login', data={'login': 'admin', 'senha': '123'})


def _padeiro(login='pad1'):
    u = Usuario(nome='Padeiro Um', login=login, papel='padeiro')
    u.set_senha('12345678')
    db.session.add(u)
    db.session.commit()
    return u


def test_lousa_vazia_e_escrever(app, admin_user):
    c = app.test_client()
    _login_admin(c)
    corpo = c.get('/padeiro/lousa').get_data(as_text=True)
    assert 'Lousa limpa' in corpo
    r = c.post('/padeiro/lousa',
               data={'texto': 'Sobraram 2 bolas de massa na geladeira'},
               follow_redirects=True)
    assert 'Sobraram 2 bolas de massa' in r.get_data(as_text=True)
    with app.app_context():
        rec = LousaRecado.query.one()
        assert rec.criado_por_id == admin_user.id
        assert rec.apagado_em is None


def test_apagar_recado_soft_delete(app, admin_user):
    with app.app_context():
        rec = LousaRecado(texto='Forno 2 esquentando pouco',
                          criado_por_id=admin_user.id)
        db.session.add(rec)
        db.session.commit()
        rid = rec.id
    c = app.test_client()
    _login_admin(c)
    r = c.post(f'/padeiro/lousa/{rid}/apagar', follow_redirects=True)
    assert 'Forno 2' not in r.get_data(as_text=True)   # saiu da lousa
    with app.app_context():
        rec = db.session.get(LousaRecado, rid)
        assert rec is not None                          # histórico fica
        assert rec.apagado_em is not None
        assert rec.apagado_por_id == admin_user.id


def test_texto_vazio_nao_cria(app, admin_user):
    c = app.test_client()
    _login_admin(c)
    r = c.post('/padeiro/lousa', data={'texto': '   '},
               follow_redirects=True)
    assert 'Escreva o recado' in r.get_data(as_text=True)
    with app.app_context():
        assert LousaRecado.query.count() == 0


def test_texto_longo_trunca_em_500(app, admin_user):
    c = app.test_client()
    _login_admin(c)
    c.post('/padeiro/lousa', data={'texto': 'x' * 900})
    with app.app_context():
        assert len(LousaRecado.query.one().texto) == 500


def test_botao_lousa_no_index_com_contador(app, admin_user):
    with app.app_context():
        db.session.add(LousaRecado(texto='a', criado_por_id=admin_user.id))
        db.session.add(LousaRecado(texto='b', criado_por_id=admin_user.id))
        db.session.commit()
    c = app.test_client()
    _login_admin(c)
    corpo = c.get('/padeiro/').get_data(as_text=True)
    assert '/padeiro/lousa' in corpo
    assert 'Lousa (2)' in corpo


def test_papel_padeiro_acessa_e_escreve(app):
    with app.app_context():
        _padeiro()
    c = app.test_client()
    c.post('/auth/login', data={'login': 'pad1', 'senha': '12345678'})
    assert c.get('/padeiro/lousa').status_code == 200
    r = c.post('/padeiro/lousa', data={'texto': 'assar o brioche às 9h'},
               follow_redirects=True)
    assert 'brioche' in r.get_data(as_text=True)


def test_papel_sem_permissao_403(app):
    with app.app_context():
        u = Usuario(nome='Vendas', login='vend', papel='funcionario')
        u.set_senha('12345678')
        db.session.add(u)
        db.session.commit()
    c = app.test_client()
    c.post('/auth/login', data={'login': 'vend', 'senha': '12345678'})
    assert c.get('/padeiro/lousa').status_code == 403
    assert c.post('/padeiro/lousa', data={'texto': 'x'}).status_code == 403


def test_painel_no_index_ocupa_terco_da_tela_com_recado(app, admin_user):
    """Com recado, a lousa aparece na própria tela do padeiro ocupando 1/3
    da tela (height:33vh); sem recado, o painel some por completo."""
    c = app.test_client()
    _login_admin(c)
    corpo = c.get('/padeiro/').get_data(as_text=True)
    assert 'class="lousa-painel"' not in corpo      # lousa limpa: sem painel
    assert 'height:33vh' in corpo                   # CSS pronto pra quando tiver
    with app.app_context():
        db.session.add(LousaRecado(texto='puxar massa da geladeira',
                                   criado_por_id=admin_user.id))
        db.session.commit()
    corpo = c.get('/padeiro/').get_data(as_text=True)
    assert 'class="lousa-painel"' in corpo
    assert 'puxar massa da geladeira' in corpo


def test_fragmento_lousa_para_polling_da_tv(app, admin_user):
    """A TV recarrega o painel via GET /padeiro/lousa.html sem recarregar a
    página (padrão listas_html): vazio sem recado, painel com recado."""
    c = app.test_client()
    _login_admin(c)
    assert c.get('/padeiro/lousa.html').get_data(as_text=True).strip() == ''
    with app.app_context():
        db.session.add(LousaRecado(texto='forno 2 ligado',
                                   criado_por_id=admin_user.id))
        db.session.commit()
    corpo = c.get('/padeiro/lousa.html').get_data(as_text=True)
    assert 'lousa-painel' in corpo
    assert 'forno 2 ligado' in corpo


def test_apagar_do_painel_volta_pro_index(app, admin_user):
    with app.app_context():
        rec = LousaRecado(texto='x', criado_por_id=admin_user.id)
        db.session.add(rec)
        db.session.commit()
        rid = rec.id
    c = app.test_client()
    _login_admin(c)
    r = c.post(f'/padeiro/lousa/{rid}/apagar', data={'volta': 'index'})
    assert r.headers['Location'].endswith('/padeiro/')
    with app.app_context():
        assert db.session.get(LousaRecado, rid).apagado_em is not None


def test_recado_nao_vira_aviso_com_campainha(app, admin_user):
    """A lousa é SEPARADA do sistema de Aviso (ticker + campainha): escrever
    na lousa não pode disparar o alarme da TV."""
    c = app.test_client()
    _login_admin(c)
    c.post('/padeiro/lousa', data={'texto': 'recado tranquilo'})
    r = c.get('/padeiro/avisos.json')
    assert r.get_json()['avisos'] == []
