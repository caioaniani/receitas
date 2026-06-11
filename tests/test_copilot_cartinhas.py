"""Tool consultar_cartinhas no copilot_svc — herdada automaticamente pelo
bot WhatsApp do dono (zapi_bot, copilot read-only) e pelo Slack."""
from app.extensions import db


def test_tool_consultar_cartinhas_registrada_e_executa(app, admin_user):
    from app.models import CartinhaEntrega, Usuario
    from app.services import copilot as cs
    from app.utils import agora
    uid = admin_user.id
    with app.app_context():
        db.session.add(CartinhaEntrega(pedido_code='VND-AAA',
                                       texto='Feliz dia!',
                                       atualizado_em=agora(),
                                       atualizado_por=uid))
        db.session.commit()
        assert 'consultar_cartinhas' in [t['name'] for t in cs.TOOLS]
        assert cs.PAPEIS_POR_TOOL['consultar_cartinhas'] == {'admin', 'gerente'}
        u = Usuario.query.get(uid)
        r = cs._executar_read('consultar_cartinhas', {'dias': 2}, u)
        assert 'VND-AAA' in r['texto'] and r['total'] == 1


def test_cartinhas_e_read_tool_visivel_no_modo_leitura(app, admin_user):
    """REQUER_APROVACAO nao contem a tool — o bot do dono (apenas_leitura)
    consegue usa-la."""
    from app.services import copilot as cs
    assert 'consultar_cartinhas' not in cs.REQUER_APROVACAO
