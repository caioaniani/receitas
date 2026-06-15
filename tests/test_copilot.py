

# -------- Fork de modelo Slack/WhatsApp + regra "responder antes" (14/06) -

def test_copilot_default_e_sonnet_no_slack():
    """Fork (14/06/2026): default = Sonnet 4.6 (Slack). WhatsApp do dono
    sobrescreve pra Opus 4.8 via override `modelo=` no zapi_bot."""
    from app.services.copilot import MODELO_DEFAULT
    assert MODELO_DEFAULT == 'claude-sonnet-4-6', \
        f'modelo mudou: {MODELO_DEFAULT}'


def test_copilot_prompt_tem_regra_responder_antes_de_perguntar(app):
    """O system prompt do copilot deve preferir responder a perguntar."""
    from types import SimpleNamespace

    from app.services.copilot import _build_system_prompt
    user = SimpleNamespace(id=1, nome='Caio', login='caio', papel='owner')
    with app.app_context():
        system = _build_system_prompt(user)
    assert 'PREFIRA RESPONDER A PERGUNTAR' in system
