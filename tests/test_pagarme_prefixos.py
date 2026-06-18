"""Mascaragem dos prefixos de chave no /admin/debug-pagarme.

Owner-only, mas mesmo assim NUNCA pode vazar o segredo. Mostra os 8
primeiros chars + elipse — suficiente pra confirmar ambiente
(sk_test_… / sk_live_… / sk_…) sem expor o resto.
"""


def _owner(app):
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='Dono', login='dono', papel='admin', is_owner=True)
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(u.id)
        s['_fresh'] = True
    return c


def test_prefixo_chave_mascara_resto(app):
    """Mostra exatamente os 8 primeiros chars + elipse. Usamos string
    NÃO no formato de chave real pra não trombar com secret scanner."""
    from app.services import pagarme
    with app.app_context():
        app.config['PAGARME_API_KEY'] = 'AAAAAAAA' + 'segredo_que_nao_pode_vazar'
        assert pagarme.prefixo_chave() == 'AAAAAAAA…'


def test_prefixo_chave_chave_curta(app):
    from app.services import pagarme
    with app.app_context():
        app.config['PAGARME_API_KEY'] = 'abcdef'
        assert pagarme.prefixo_chave() == '…'


def test_prefixo_chave_vazia(app):
    from app.services import pagarme
    with app.app_context():
        app.config['PAGARME_API_KEY'] = ''
        assert pagarme.prefixo_chave() == ''


def test_debug_pagarme_nao_vaza_segredo(app):
    """A rota inclui os prefixos das duas chaves, sem expor o segredo."""
    c = _owner(app)
    with app.app_context():
        app.config['PAGARME_API_KEY'] = 'AAAAAAAA' + 'segredo_que_nao_pode_vazar'
        app.config['PAGARME_PUBLIC_KEY'] = 'BBBBBBBB' + 'publica_resto'
    r = c.get('/admin/debug-pagarme')
    assert r.status_code == 200
    body = r.get_json()
    assert body['api_key_prefixo'] == 'AAAAAAAA…'
    assert body['public_key_prefixo'] == 'BBBBBBBB…'
    # Garantia: o resto da SECRET key NUNCA aparece no JSON
    assert b'segredo_que_nao_pode_vazar' not in r.data


def test_ambiente_chave_sk_simples_e_producao(app):
    """Pagar.me Stone (padrão novo, confirmado 18/06/2026): chave começa
    com `sk_` direto, sem `test_` ou `live_`. É produção — sandbox sempre
    tem `_test_` explícito."""
    from app.services import pagarme
    with app.app_context():
        app.config['PAGARME_API_KEY'] = 'sk_HASH_QUALQUER'
        assert pagarme.ambiente() == 'producao'


def test_ambiente_desconhecido_quando_nao_e_pagarme(app):
    """Chave que não começa com `sk_` (= não é Pagar.me) → desconhecido."""
    from app.services import pagarme
    with app.app_context():
        app.config['PAGARME_API_KEY'] = 'qualquer_outro_prefixo'
        assert pagarme.ambiente() == 'desconhecido'
