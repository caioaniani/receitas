"""Sessão permanente + upload grande (incidente 25/07/2026).

Caso real: dono foi subir a foto na ficha da receita e levou "Sessão de
segurança expirada". Causa: o cookie de sessão era "de navegador" (morria ao
fechar/reciclar a aba) enquanto o remember-me durava 1 ano — o Flask-Login
relogava numa sessão NOVA e o token CSRF da página já aberta parava de bater.
Fix: sessão PERMANENTE (30d rolando) + handler 413 amigável (antes, foto acima
do limite caía numa página crua "Request Entity Too Large").
"""
import io
from datetime import timedelta


def test_sessao_e_permanente(app):
    """A sessão precisa sobreviver a fechar o navegador — senão o CSRF quebra."""
    c = app.test_client()
    with c:
        c.get('/auth/login')
        from flask import session
        assert session.permanent is True


def test_lifetime_de_30_dias(app):
    assert app.config['PERMANENT_SESSION_LIFETIME'] == timedelta(days=30)


def test_csrf_nao_expira_por_tempo(app):
    """Companheiro do fix: token que vence por tempo derrubava form de aba
    aberta o dia todo (02/07/2026). Os dois juntos é que resolvem."""
    assert app.config['WTF_CSRF_TIME_LIMIT'] is None


def test_upload_acima_do_limite_avisa_o_tamanho(app, admin_user):
    """413 vira mensagem clara (com o limite em MB), não página crua."""
    app.config['MAX_CONTENT_LENGTH'] = 1024          # 1 KB só pro teste
    # TESTING=True propaga a exceção; em prod ela cai no errorhandler. Queremos
    # exercitar o handler (é ele que o usuário vê).
    app.config['PROPAGATE_EXCEPTIONS'] = False
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(admin_user.id)
        s['_fresh'] = True
    grande = io.BytesIO(b'x' * 5000)                 # 5 KB > 1 KB
    r = c.post('/cardapio-img/receita/1/upload',
               data={'imagem_arquivo': (grande, 'foto.jpg')},
               content_type='multipart/form-data', follow_redirects=True)
    corpo = r.get_data(as_text=True)
    assert 'grande demais' in corpo and 'MB' in corpo
    assert 'Sessão de segurança expirada' not in corpo   # não confunde mais
