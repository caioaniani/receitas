"""Email de boas-vindas pra novo usuário via Resend (16/06/2026).

Admin cadastra usuário com email → senha gerada → email enviado. Sem email
ou se Resend falhar, senha aparece no flash pro admin copiar (fallback).
"""
from unittest.mock import patch


def _admin(app):
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='Adm', login='adm', papel='admin')
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(u.id)
        s['_fresh'] = True
    return c


# ── Service ─────────────────────────────────────────────────────────────

def test_email_service_sem_chave_devolve_erro(app):
    from app.services import email as email_svc
    with app.app_context():
        app.config['RESEND_API_KEY'] = ''
        r = email_svc.enviar('x@y.com', 'a', '<p>b</p>')
        assert r['ok'] is False
        assert 'RESEND' in r['erro']


def test_email_service_destinatario_invalido(app):
    from app.services import email as email_svc
    with app.app_context():
        app.config['RESEND_API_KEY'] = 're_fake'
        r = email_svc.enviar('sem-arroba', 'a', '<p>b</p>')
        assert r['ok'] is False


def test_email_service_monta_payload_e_envia(app):
    from app.services import email as email_svc
    with app.app_context():
        app.config['RESEND_API_KEY'] = 're_fake'
        app.config['EMAIL_REMETENTE'] = 'noreply@opao.online'
        app.config['EMAIL_REMETENTE_NOME'] = 'O Pão'

        class FakeResp:
            status_code = 200
            def json(self):
                return {'id': 'em_123'}
        with patch('app.services.email.requests.post',
                    return_value=FakeResp()) as post:
            r = email_svc.enviar('cliente@x.com', 'Assunto', '<p>oi</p>',
                                  texto='oi')
        assert r['ok'] is True and r['id'] == 'em_123'
        # payload certinho
        _, kwargs = post.call_args
        body = kwargs['json']
        assert body['to'] == ['cliente@x.com']
        assert 'noreply@opao.online' in body['from']
        assert body['subject'] == 'Assunto'
        assert 'Bearer re_fake' in kwargs['headers']['Authorization']


def test_boas_vindas_inclui_senha_e_login(app):
    from app.services import email as email_svc
    with app.app_context():
        app.config['RESEND_API_KEY'] = 're_fake'

        class FakeResp:
            status_code = 200
            def json(self): return {'id': 'em_1'}
        with patch('app.services.email.requests.post',
                    return_value=FakeResp()) as post:
            email_svc.enviar_boas_vindas('novo@x.com', 'João', 'joao', 'senha123')
        html = post.call_args[1]['json']['html']
        assert 'joao' in html
        assert 'senha123' in html
        assert 'Chatwoot' in html  # instrução do atendimento


def test_boas_vindas_resend_falha_propaga_erro(app):
    from app.services import email as email_svc
    with app.app_context():
        app.config['RESEND_API_KEY'] = 're_fake'

        class FakeResp:
            status_code = 422
            text = 'domain not verified'
            def json(self): return {'message': 'domain not verified'}
        with patch('app.services.email.requests.post', return_value=FakeResp()):
            r = email_svc.enviar_boas_vindas('x@y.com', 'N', 'n', 's')
        assert r['ok'] is False
        assert 'domain not verified' in r['erro']


# ── Cadastro de usuário ───────────────────────────────────────────────

def test_cadastro_com_email_gera_senha_e_envia(app):
    from app.models import Usuario
    c = _admin(app)
    with patch('app.services.email.enviar_boas_vindas',
                return_value={'ok': True, 'id': 'em_1'}) as envia:
        r = c.post('/auth/usuarios/novo', data={
            'nome': 'Maria', 'login': 'maria',
            'email': 'maria@x.com', 'papel': 'funcionario',
        }, follow_redirects=False)
    assert r.status_code == 302
    u = Usuario.query.filter_by(login='maria').first()
    assert u is not None
    assert u.email == 'maria@x.com'
    # senha foi gerada (hash existe) e NÃO é vazia
    assert u.senha_hash
    # email foi disparado com a senha gerada
    envia.assert_called_once()
    args = envia.call_args[0]
    assert args[0] == 'maria@x.com'   # destinatário
    assert args[2] == 'maria'         # login
    senha_gerada = args[3]
    assert senha_gerada and len(senha_gerada) >= 6
    # a senha gerada confere com o hash salvo
    assert u.check_senha(senha_gerada)


def test_cadastro_sem_email_mostra_senha_no_flash(app):
    from app.models import Usuario
    c = _admin(app)
    with patch('app.services.email.enviar_boas_vindas') as envia:
        r = c.post('/auth/usuarios/novo', data={
            'nome': 'Sem Email', 'login': 'sememail', 'papel': 'funcionario',
        }, follow_redirects=True)
    # Não tenta enviar email se não tem endereço
    envia.assert_not_called()
    u = Usuario.query.filter_by(login='sememail').first()
    assert u is not None and u.email is None
    assert b'Senha:' in r.data  # flash com a senha pro admin copiar


def test_cadastro_nao_exige_mais_campo_senha_manual(app):
    """Regressão: o form antigo exigia senha digitada. Agora é gerada.
    Cadastro sem 'senha' no POST tem que funcionar."""
    from app.models import Usuario
    c = _admin(app)
    with patch('app.services.email.enviar_boas_vindas',
                return_value={'ok': True}):
        c.post('/auth/usuarios/novo', data={
            'nome': 'Auto', 'login': 'autosenha',
            'email': 'auto@x.com', 'papel': 'funcionario',
        })
    assert Usuario.query.filter_by(login='autosenha').first() is not None


def test_cadastro_email_falha_mostra_senha_pra_copiar(app):
    """Se Resend falhar, admin NÃO fica sem a senha — aparece no flash."""
    c = _admin(app)
    with patch('app.services.email.enviar_boas_vindas',
                return_value={'ok': False, 'erro': 'domain not verified'}):
        r = c.post('/auth/usuarios/novo', data={
            'nome': 'Falhou', 'login': 'falhou',
            'email': 'falhou@x.com', 'papel': 'funcionario',
        }, follow_redirects=True)
    assert b'Senha:' in r.data  # fallback: senha no flash
    assert b'email falhou' in r.data or b'falhou' in r.data
