

# -------- Opus 4.8 + regra "responder antes de perguntar" (14/06/2026) ---

def test_copilot_usa_opus_4_8():
    """Trava que o modelo default do copilot é Opus 4.8."""
    from app.services.copilot import MODELO_DEFAULT
    assert MODELO_DEFAULT == 'claude-opus-4-8', f'modelo mudou: {MODELO_DEFAULT}'


def test_copilot_prompt_tem_regra_responder_antes_de_perguntar(app):
    """O system prompt do copilot deve preferir responder a perguntar."""
    from types import SimpleNamespace

    from app.services.copilot import _build_system_prompt
    user = SimpleNamespace(id=1, nome='Caio', login='caio', papel='owner')
    with app.app_context():
        system = _build_system_prompt(user)
    assert 'PREFIRA RESPONDER A PERGUNTAR' in system
