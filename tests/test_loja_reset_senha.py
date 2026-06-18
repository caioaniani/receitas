"""Recuperação de senha do cliente (Fase 6 — PR 3).

Foco: anti-enumeração (mesma resposta pra existe/não existe), token
single-use + expira em 1h, email é best-effort.
"""
from datetime import timedelta
from unittest.mock import patch


def _cliente_com_senha(db, email='c@x.com', nome='C'):
    from app.models import Cliente
    cli = Cliente(nome=nome, email=email)
    cli.set_senha('senha-antiga')
    db.session.add(cli)
    db.session.commit()
    return cli


def test_pedir_reset_dispara_email_para_conta_existente(app):
    from app.extensions import db
    from app.services import loja_auth
    with app.app_context():
        _cliente_com_senha(db, email='ok@x.com')
        with patch('app.services.email.enviar_reset_senha') as enviar:
            loja_auth.iniciar_reset('ok@x.com')
        enviar.assert_called_once()


def test_pedir_reset_nao_existe_silencia(app):
    """Email inexistente: NÃO dispara email, mas devolve True (anti-enum)."""
    from app.services import loja_auth
    with app.app_context():
        with patch('app.services.email.enviar_reset_senha') as enviar:
            res = loja_auth.iniciar_reset('nao-existe@x.com')
        assert res is True
        enviar.assert_not_called()


def test_pedir_reset_guest_sem_senha_silencia(app):
    """Guest (sem senha) não recebe link de reset — tem que se cadastrar."""
    from app.extensions import db
    from app.models import Cliente
    from app.services import loja_auth
    with app.app_context():
        cli = Cliente(nome='G', email='guest@x.com')
        db.session.add(cli)
        db.session.commit()
        with patch('app.services.email.enviar_reset_senha') as enviar:
            loja_auth.iniciar_reset('guest@x.com')
        enviar.assert_not_called()


def test_rota_esqueci_senha_resposta_uniforme(app):
    """A página devolve a MESMA mensagem ('se houver conta...') tanto pra
    email que existe quanto pra um que não — anti-enumeração via HTTP."""
    from app.extensions import db
    c = app.test_client()
    with app.app_context():
        _cliente_com_senha(db, email='exist@x.com')
    with patch('app.services.email.enviar_reset_senha'):
        r_exist = c.post('/loja/esqueci-senha',
                          data={'email': 'exist@x.com'})
        r_nao = c.post('/loja/esqueci-senha',
                        data={'email': 'naoexiste@x.com'})
    assert r_exist.status_code == 200 and r_nao.status_code == 200
    # Mesma página de "enviado" pros dois (corpos batem na mensagem chave)
    assert b'mandamos um link' in r_exist.data
    assert b'mandamos um link' in r_nao.data


def test_token_valido_redefine_senha_e_loga(app):
    from app.extensions import db
    from app.models import ClienteResetSenha
    from app.services import loja_auth
    c = app.test_client()
    with app.app_context():
        cli = _cliente_com_senha(db, email='r@x.com')
        with patch('app.services.email.enviar_reset_senha'):
            loja_auth.iniciar_reset('r@x.com')
        reg = ClienteResetSenha.query.filter_by(cliente_id=cli.id).first()
        token = reg.token
    # Form com nova senha
    r = c.post(f'/loja/redefinir-senha/{token}',
                data={'senha': 'senha-nova-1', 'confirmar_senha': 'senha-nova-1'},
                follow_redirects=False)
    assert r.status_code == 302
    assert '/loja/conta' in r.headers['Location']
    # Senha trocada + token marcado usado + cliente já logado
    with app.app_context():
        from app.models import Cliente
        cli = Cliente.query.filter_by(email='r@x.com').first()
        assert cli.check_senha('senha-nova-1')
        assert not cli.check_senha('senha-antiga')
        reg = ClienteResetSenha.query.filter_by(cliente_id=cli.id).first()
        assert reg.usado_em is not None


def test_token_usado_nao_funciona_segunda_vez(app):
    """Token single-use: segunda tentativa rejeitada como inválido."""
    from app.extensions import db
    from app.models import ClienteResetSenha
    from app.services import loja_auth
    c = app.test_client()
    with app.app_context():
        cli = _cliente_com_senha(db, email='u@x.com')
        with patch('app.services.email.enviar_reset_senha'):
            loja_auth.iniciar_reset('u@x.com')
        token = ClienteResetSenha.query.filter_by(
            cliente_id=cli.id).first().token
    # Primeira: ok
    c.post(f'/loja/redefinir-senha/{token}',
           data={'senha': 'senha-nova-1', 'confirmar_senha': 'senha-nova-1'})
    # Segunda: GET mostra mensagem de inválido (400)
    r = c.get(f'/loja/redefinir-senha/{token}')
    assert r.status_code == 400
    assert b'inv' in r.data.lower() or b'expir' in r.data.lower()


def test_token_expirado_rejeita(app):
    """Token > 1h sem uso: rejeita. Cliente pede novo."""
    from app.extensions import db
    from app.models import ClienteResetSenha
    from app.utils import agora
    c = app.test_client()
    with app.app_context():
        cli = _cliente_com_senha(db, email='e@x.com')
        from app.services import loja_auth
        with patch('app.services.email.enviar_reset_senha'):
            loja_auth.iniciar_reset('e@x.com')
        reg = ClienteResetSenha.query.filter_by(cliente_id=cli.id).first()
        # Mata o token: expira_em no passado
        reg.expira_em = agora() - timedelta(minutes=1)
        db.session.commit()
        token = reg.token
    r = c.get(f'/loja/redefinir-senha/{token}')
    assert r.status_code == 400


def test_token_invalido_inexistente_rejeita(app):
    c = app.test_client()
    r = c.get('/loja/redefinir-senha/token-falsa-aleatoria-12345')
    assert r.status_code == 400


def test_senhas_nao_batem_rejeita(app):
    from app.extensions import db
    from app.models import ClienteResetSenha
    from app.services import loja_auth
    c = app.test_client()
    with app.app_context():
        cli = _cliente_com_senha(db, email='n@x.com')
        with patch('app.services.email.enviar_reset_senha'):
            loja_auth.iniciar_reset('n@x.com')
        token = ClienteResetSenha.query.filter_by(
            cliente_id=cli.id).first().token
    r = c.post(f'/loja/redefinir-senha/{token}',
                data={'senha': 'senha-nova-1',
                      'confirmar_senha': 'outra-senha-1'})
    assert r.status_code == 400
    assert b'batem' in r.data
    # Senha NÃO foi trocada
    with app.app_context():
        from app.models import Cliente
        cli = Cliente.query.filter_by(email='n@x.com').first()
        assert cli.check_senha('senha-antiga')


def test_senha_curta_rejeita(app):
    from app.extensions import db
    from app.models import ClienteResetSenha
    from app.services import loja_auth
    c = app.test_client()
    with app.app_context():
        cli = _cliente_com_senha(db, email='s@x.com')
        with patch('app.services.email.enviar_reset_senha'):
            loja_auth.iniciar_reset('s@x.com')
        token = ClienteResetSenha.query.filter_by(
            cliente_id=cli.id).first().token
    r = c.post(f'/loja/redefinir-senha/{token}',
                data={'senha': '123', 'confirmar_senha': '123'})
    assert r.status_code == 400


def test_link_esqueci_no_entrar(app):
    c = app.test_client()
    r = c.get('/loja/entrar')
    assert r.status_code == 200
    assert b'Esqueci minha senha' in r.data
