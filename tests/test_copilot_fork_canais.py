"""Fork por canal (11/06/2026): Slack = Sonnet operacional; WhatsApp do
dono = Opus + persona de assessor. MOTOR unico (copilot_svc) — tool nova
continua valendo nos dois canais."""
from unittest.mock import MagicMock, patch


class _FakeResp:
    def __init__(self):
        self.content = [MagicMock(type='text', text='ok')]
        self.usage = MagicMock(input_tokens=10, output_tokens=5,
                               cache_read_input_tokens=0,
                               cache_creation_input_tokens=0)
        self.stop_reason = 'end_turn'


def _interpretar(app, admin_user, **kwargs):
    """Roda interpretar com a API Anthropic mockada; devolve kwargs da
    chamada messages.create."""
    from app.models import Usuario
    from app.services import copilot as cs
    with app.app_context():
        u = Usuario.query.get(admin_user.id)
        with patch('anthropic.Anthropic') as fake_cls:
            cliente = fake_cls.return_value
            cliente.messages.create.return_value = _FakeResp()
            cs.interpretar('oi', u, **kwargs)
            return cliente.messages.create.call_args[1]


def test_default_e_sonnet_4_6(app, admin_user, monkeypatch):
    """Fork de modelo (14/06/2026, decisao do dono):
    - Slack (default, sem override `modelo=`) → Sonnet 4.6 (mais barato).
    - WhatsApp do dono (zapi_bot passa override) → Opus 4.8.
    Trava o lado Sonnet; o lado Opus tem teste separado abaixo."""
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-teste')
    chamada = _interpretar(app, admin_user)
    assert chamada['model'] == 'claude-sonnet-4-6'


def test_override_de_modelo_e_persona(app, admin_user, monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-teste')
    chamada = _interpretar(app, admin_user, modelo='claude-opus-4-8',
                           system_extra='PERSONA TESTE XYZ')
    assert chamada['model'] == 'claude-opus-4-8'
    system_texto = chamada['system'][0]['text']
    assert system_texto.rstrip().endswith('PERSONA TESTE XYZ')


def test_zapi_bot_usa_opus_e_persona(app, admin_user, monkeypatch):
    """O canal do dono passa Opus (default ou env ZAPI_BOT_MODELO) e a
    persona de assessor pro motor compartilhado."""
    from app.extensions import db
    from app.models import Usuario
    from app.services import zapi_bot
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-teste')
    app.config['ZAPI_BOT_DONO_NUMERO'] = '5511999990000'
    app.config['ZAPI_BOT_WEBHOOK_TOKEN'] = 'tok'
    with app.app_context():
        u = Usuario.query.get(admin_user.id)
        u.is_owner = True
        db.session.commit()

        with patch('app.services.copilot.interpretar',
                   return_value={'tipo': 'conversa', 'explicacao': 'oi'}) as fake, \
             patch('app.services.zapi_bot._responder'):
            zapi_bot.processar_payload({
                'phone': '5511999990000', 'messageId': 'm-fork-1',
                'text': {'message': 'resumo de hoje'},
            })
        kwargs = fake.call_args[1]
        assert kwargs['modelo'] == 'claude-opus-4-8'
        assert 'assessor executivo' in kwargs['system_extra']
        assert kwargs['apenas_leitura'] is True

        # env troca o modelo sem deploy de codigo
        app.config['ZAPI_BOT_MODELO'] = 'claude-sonnet-4-6'
        with patch('app.services.copilot.interpretar',
                   return_value={'tipo': 'conversa', 'explicacao': 'oi'}) as fake2, \
             patch('app.services.zapi_bot._responder'):
            zapi_bot.processar_payload({
                'phone': '5511999990000', 'messageId': 'm-fork-2',
                'text': {'message': 'oi'},
            })
        assert fake2.call_args[1]['modelo'] == 'claude-sonnet-4-6'


def test_prompt_tem_regra_anti_amnesia(app, admin_user):
    """Bug real (zapi_bot, 11/06/2026): 'me manda o link aqui' → bot alucinou
    'cada sessao comeca do zero', mesmo recebendo 80 turnos de historico.
    Causa: prompt nao mencionava memoria. Fix: bloco MEMORIA explicito."""
    from app.models import Usuario
    from app.services import copilot as cs
    with app.app_context():
        u = Usuario.query.get(admin_user.id)
        p = cs._build_system_prompt(u)
        assert 'MEMORIA' in p
        assert 'cada sessao comeca do zero' in p   # listado como proibido
        assert 'NUNCA diga' in p
