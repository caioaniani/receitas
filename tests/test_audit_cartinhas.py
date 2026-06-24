"""Tela /audit lista cartinhas atualizadas nas ultimas 48h — pra rastrear
quem cadastrou cartinha em qual pedido (pedido do dono 10/06/2026)."""
from datetime import timedelta


def _login(c):
    c.post('/auth/login', data={'login': 'admin', 'senha': '123'})


def test_audit_mostra_cartinhas_recentes_e_oculta_antigas(app, admin_user):
    from app.extensions import db
    from app.models import CartinhaEntrega, Usuario
    from app.utils import agora
    with app.app_context():
        ate = Usuario(nome='Ana atendente', login='ana_at', papel='gerente')
        ate.set_senha('x' * 8)
        db.session.add(ate)
        db.session.flush()
        db.session.add_all([
            CartinhaEntrega(pedido_code='VND-AAA',
                            texto='Feliz dia dos namorados, meu amor!',
                            atualizado_em=agora(), atualizado_por=ate.id),
            CartinhaEntrega(pedido_code='VND-OLD',
                            texto='cartinha de semana passada',
                            atualizado_em=agora() - timedelta(days=4),
                            atualizado_por=ate.id),
        ])
        db.session.commit()
    c = app.test_client()
    _login(c)
    r = c.get('/audit')
    assert r.status_code == 200
    assert b'Cartinhas cadastradas/editadas' in r.data
    assert b'VND-AAA' in r.data
    assert 'Feliz dia dos namorados, meu amor!'.encode() in r.data
    assert 'Ana atendente'.encode() in r.data
    assert b'VND-OLD' not in r.data         # fora da janela 48h


def test_audit_sem_cartinhas_recentes_nao_mostra_secao(app, admin_user):
    c = app.test_client()
    _login(c)
    r = c.get('/audit')
    assert r.status_code == 200
    assert b'Cartinhas cadastradas/editadas' not in r.data
