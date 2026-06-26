"""Bot de atendimento sem emoji (decisao do dono 26/06/2026).

Trava: a regra no prompt, a ausencia de emoji nas mensagens de SAIDA hardcoded
e no follow-up. Os emojis de DETECCAO de despedida do cliente (input) ficam.
"""


def test_prompt_tem_regra_sem_emoji():
    from app.services.chatbot_prompt import PROMPT
    assert 'NUNCA use emoji' in PROMPT


def test_prompt_sem_emoji_decorativo_de_saida():
    """Os emojis decorativos que o bot copiava dos exemplos sairam do prompt
    (🙂 😊 🎉 🥐 etc.). O 💛 sobra SO na lista de deteccao de despedida do
    cliente (input), nunca em exemplo de resposta."""
    from app.services.chatbot_prompt import PROMPT
    for e in ('🙂', '😊', '🎉', '🥐', '🍞', '🛒', '💰', '🚚'):
        assert e not in PROMPT, f'emoji decorativo {e} ainda no prompt'
    # 💛 so na deteccao de despedida (linha com "passo por aí"), em nenhum
    # exemplo de resposta do bot.
    for linha in PROMPT.split('\n'):
        if '💛' in linha:
            assert 'passo por aí' in linha, f'💛 fora da deteccao: {linha!r}'


def test_mensagens_hardcoded_sem_emoji():
    from app.services import chatbot
    assert '🙂' not in chatbot._FALLBACK
    assert '🙂' not in chatbot._FALLBACK_CATALOGO


def test_followup_sem_emoji():
    from app.services.chatbot import FOLLOWUP_PROMPT
    assert 'SEM emoji' in FOLLOWUP_PROMPT
    assert 'máximo 1 emoji' not in FOLLOWUP_PROMPT
