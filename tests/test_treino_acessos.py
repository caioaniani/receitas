"""Onboarding do treinamento — gerar/vincular o login do funcionário.

Migrado do módulo antigo (removido 24/07/2026) pro serviço treino_acessos +
rota treino.admin_gerar_acesso. O e-mail de boas-vindas é sempre mockado.
"""
from unittest.mock import patch

from app.extensions import db
from app.models import Funcionario, Usuario
from app.services import treino_acessos as acessos


def _func(nome='Zeca', email='zeca@opao.online', cpf='90000000001'):
    f = Funcionario(nome=nome, cpf=cpf, ativo=True, email=email)
    db.session.add(f)
    db.session.commit()
    return f


def _admin_client(app, admin_user):
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(admin_user.id)
        s['_fresh'] = True
    return c


def test_sem_email_recusa(app):
    with app.app_context():
        f = _func(email=None)
        r = acessos.gerar_acesso(f)
        assert r['motivo'] == 'sem_email' and not r['ok']
        assert f.usuario_id is None


def test_cria_conta_e_vincula(app):
    with app.app_context():
        f = _func()
        with patch('app.services.email.enviar_boas_vindas',
                   return_value={'ok': True}):
            r = acessos.gerar_acesso(f)
        assert r['ok'] and r['motivo'] == 'criado'
        assert f.usuario_id is not None
        u = db.session.get(Usuario, f.usuario_id)
        assert u.papel == 'funcionario' and u.login == 'zeca@opao.online'
        # idempotente: segunda chamada não recria
        r2 = acessos.gerar_acesso(f)
        assert r2['motivo'] == 'ja_tem'


def test_vincula_conta_existente_de_funcionario(app):
    with app.app_context():
        u = Usuario(nome='Ze', login='ze@opao.online', email='ze@opao.online',
                    papel='funcionario')
        u.set_senha('x' * 8)
        db.session.add(u)
        db.session.commit()
        f = _func(nome='Ze', email='ze@opao.online', cpf='90000000002')
        r = acessos.gerar_acesso(f)
        assert r['ok'] and r['motivo'] == 'vinculado'
        assert f.usuario_id == u.id


def test_recusa_conta_de_admin(app):
    with app.app_context():
        u = Usuario(nome='Chefe', login='chefe@opao.online',
                    email='chefe@opao.online', papel='admin')
        u.set_senha('x' * 8)
        db.session.add(u)
        db.session.commit()
        f = _func(nome='Chefe', email='chefe@opao.online', cpf='90000000003')
        r = acessos.gerar_acesso(f)
        assert not r['ok'] and r['motivo'] == 'conta_de_outro_papel'
        assert f.usuario_id is None


def test_rota_admin_gera_acesso(app, admin_user):
    with app.app_context():
        f = _func(cpf='90000000004')
        fid = f.id
    c = _admin_client(app, admin_user)
    with patch('app.services.email.enviar_boas_vindas',
               return_value={'ok': True}):
        r = c.post(f'/treino/admin/acessos/{fid}/gerar')
    assert r.status_code in (302, 303)
    with app.app_context():
        assert db.session.get(Funcionario, fid).usuario_id is not None
