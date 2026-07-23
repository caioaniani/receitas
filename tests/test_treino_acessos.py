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


def test_email_de_treino_nao_fala_de_chatwoot(app):
    """Conta de treinamento não recebe o convite do Chatwoot (nem todo
    funcionário atende cliente) — gerar_acesso manda com_chatwoot=False."""
    with app.app_context():
        f = _func()
        with patch('app.services.email.enviar_boas_vindas',
                   return_value={'ok': True}) as mock_env:
            acessos.gerar_acesso(f)
        assert mock_env.call_args.kwargs.get('com_chatwoot') is False


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


def test_vincular_conta_existente_sem_email(app):
    """Caso do dono: conta já existe (sem e-mail), liga sem criar duplicata."""
    with app.app_context():
        u = Usuario(nome='Zeca', login='zeca', papel='funcionario')  # sem email
        u.set_senha('x' * 8)
        db.session.add(u)
        db.session.commit()
        f = _func(email=None, cpf='90000000010')
        antes = Usuario.query.count()
        r = acessos.vincular_conta(f, u)
        assert r['ok'] and r['motivo'] == 'vinculado'
        assert f.usuario_id == u.id
        assert Usuario.query.count() == antes   # NÃO criou conta nova


def test_vincular_recusa_conta_de_outro_funcionario(app):
    with app.app_context():
        u = Usuario(nome='Zeca', login='zeca2', papel='funcionario')
        u.set_senha('x' * 8)
        db.session.add(u)
        db.session.commit()
        f1 = _func(nome='F1', email=None, cpf='90000000011')
        f1.usuario_id = u.id
        db.session.commit()
        f2 = _func(nome='F2', email=None, cpf='90000000012')
        r = acessos.vincular_conta(f2, u)
        assert not r['ok'] and r['motivo'] == 'conta_em_uso'
        assert f2.usuario_id is None


def test_contas_sem_vinculo_exclui_dono_e_vinculados(app):
    with app.app_context():
        livre = Usuario(nome='Livre', login='livre', papel='funcionario')
        dono = Usuario(nome='Dono', login='dono', papel='admin', is_owner=True)
        usada = Usuario(nome='Usada', login='usada', papel='funcionario')
        for u in (livre, dono, usada):
            u.set_senha('x' * 8)
        db.session.add_all([livre, dono, usada])
        db.session.commit()
        f = _func(nome='J', email=None, cpf='90000000013')
        f.usuario_id = usada.id
        db.session.commit()
        logins = {u.login for u in acessos.contas_sem_vinculo()}
        assert 'livre' in logins
        assert 'dono' not in logins and 'usada' not in logins


def test_rota_vincular_acesso(app, admin_user):
    with app.app_context():
        u = Usuario(nome='Zeca', login='zeca3', papel='funcionario')
        u.set_senha('x' * 8)
        db.session.add(u)
        db.session.commit()
        f = _func(email=None, cpf='90000000014')
        fid, uid = f.id, u.id
    c = _admin_client(app, admin_user)
    r = c.post(f'/treino/admin/acessos/{fid}/vincular',
               data={'usuario_id': str(uid)})
    assert r.status_code in (302, 303)
    with app.app_context():
        assert db.session.get(Funcionario, fid).usuario_id == uid


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
