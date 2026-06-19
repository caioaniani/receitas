

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


def test_copilot_prompt_tem_regra_sintetizar_em_vez_de_ecoar(app):
    """Pergunta analítica (previsão/média/tendência) → o copilot tem que
    FAZER a conta com os dados, não despejar a lista crua da tool. Trava de
    regressão pro caso 19/06/2026 (Anesio): bot devolveu 21 pedidos em vez
    de prever o pedido da semana."""
    from types import SimpleNamespace

    from app.services.copilot import _build_system_prompt
    user = SimpleNamespace(id=1, nome='Caio', login='caio', papel='owner')
    with app.app_context():
        system = _build_system_prompt(user)
    assert 'SINTETIZE' in system
    assert 'NUNCA ECOE' in system
    # Cobre os 3 verbos-gatilho que mais vamos receber.
    for verbo in ('previsao', 'media', 'tendencia'):
        assert verbo in system.lower()
