"""Power (WhatsApp pessoal do dono) — copilot READ-ONLY via /api/bot/copilot.

Implementacao de 10/06/2026: o Power passa a chamar copilot_svc com
apenas_leitura=True (mesma inteligencia do Slack, sem writes), memoria
multi-turn por telefone, auth BOT_API_TOKEN + whitelist BOT_ALLOWED_PHONES.
"""
from unittest.mock import patch

from app.extensions import db


def _setup(app, telefone='5511999990000'):
    """Token + whitelist + owner ativo."""
    from app.models import Usuario
    app.config['BOT_API_TOKEN'] = 'tk_power'
    app.config['BOT_ALLOWED_PHONES'] = telefone
    with app.app_context():
        u = Usuario(nome='Caio Owner', login='owner_power', papel='admin',
                    is_owner=True, ativo=True)
        u.set_senha('x' * 8)
        db.session.add(u)
        db.session.commit()
        return u.id


def test_power_sem_token_401(app):
    _setup(app)
    c = app.test_client()
    assert c.post('/api/bot/copilot?telefone=5511999990000',
                  json={'mensagem': 'oi'}).status_code == 401


def test_power_telefone_fora_da_whitelist_403(app):
    _setup(app, telefone='5511999990000')
    c = app.test_client()
    r = c.post('/api/bot/copilot?telefone=5511988887777',
               headers={'Authorization': 'Bearer tk_power'},
               json={'mensagem': 'oi'})
    assert r.status_code == 403


def test_power_mensagem_vazia_400(app):
    _setup(app)
    c = app.test_client()
    r = c.post('/api/bot/copilot?telefone=5511999990000',
               headers={'Authorization': 'Bearer tk_power'},
               json={'mensagem': '   '})
    assert r.status_code == 400


def test_power_conversa_simples_devolve_resposta(app):
    """Sem tool: copilot_svc devolve tipo='conversa' com explicacao —
    Power encaminha como texto e SALVA no historico multi-turn."""
    _setup(app)
    c = app.test_client()
    with patch('app.services.copilot.interpretar',
               return_value={'tipo': 'conversa', 'explicacao': 'Oi! 👋'}):
        r = c.post('/api/bot/copilot?telefone=5511999990000',
                   headers={'Authorization': 'Bearer tk_power'},
                   json={'mensagem': 'oi'})
    d = r.get_json()
    assert r.status_code == 200
    assert d['ok'] is True and d['tipo'] == 'conversa'
    assert d['resposta'] == 'Oi! 👋'
    # Memoria criada
    from app.models import ChatbotConversa
    with app.app_context():
        conv = ChatbotConversa.query.filter_by(
            conv_id='wpp-power-5511999990000').one()
        assert 'oi' in conv.mensagens_json
        assert 'Oi! 👋' in conv.mensagens_json


def test_power_so_leitura_passa_flag_ao_copilot(app):
    """Salvaguarda: o copilot precisa ser chamado com apenas_leitura=True
    pra Claude NEM VER tools de write."""
    _setup(app)
    c = app.test_client()
    with patch('app.services.copilot.interpretar',
               return_value={'tipo': 'conversa', 'explicacao': 'ok'}) as fake:
        c.post('/api/bot/copilot?telefone=5511999990000',
               headers={'Authorization': 'Bearer tk_power'},
               json={'mensagem': 'crie um pedido'})
    kwargs = fake.call_args[1]
    assert kwargs['apenas_leitura'] is True


def test_power_tool_read_formata_resultado(app):
    """tipo='consultar_cartinhas' com resultado.texto -> concatena com a
    explicacao e devolve."""
    _setup(app)
    c = app.test_client()
    with patch('app.services.copilot.interpretar',
               return_value={'tipo': 'consultar_cartinhas',
                             'explicacao': 'Olhei aqui:',
                             'resultado': {'texto': '• VND-1 — feliz dia!',
                                           'total': 1}}):
        r = c.post('/api/bot/copilot?telefone=5511999990000',
                   headers={'Authorization': 'Bearer tk_power'},
                   json={'mensagem': 'quais cartinhas hoje?'})
    d = r.get_json()
    assert d['tipo'] == 'tool:consultar_cartinhas'
    assert 'Olhei aqui:' in d['resposta'] and 'VND-1' in d['resposta']


def test_power_multi_turn_envia_historico(app):
    """Segunda mensagem traz historico da primeira pro copilot_svc."""
    _setup(app)
    c = app.test_client()
    with patch('app.services.copilot.interpretar',
               return_value={'tipo': 'conversa', 'explicacao': 'A1'}) as fake:
        c.post('/api/bot/copilot?telefone=5511999990000',
               headers={'Authorization': 'Bearer tk_power'},
               json={'mensagem': 'pergunta 1'})
        assert fake.call_args[1]['historico'] == []   # 1a vazia

        c.post('/api/bot/copilot?telefone=5511999990000',
               headers={'Authorization': 'Bearer tk_power'},
               json={'mensagem': 'pergunta 2'})
        h2 = fake.call_args[1]['historico']
        assert len(h2) == 2
        assert h2[0] == {'role': 'user', 'content': 'pergunta 1'}
        assert h2[1] == {'role': 'assistant', 'content': 'A1'}


def test_power_reset_apaga_memoria(app):
    _setup(app)
    c = app.test_client()
    with patch('app.services.copilot.interpretar',
               return_value={'tipo': 'conversa', 'explicacao': 'ok'}):
        c.post('/api/bot/copilot?telefone=5511999990000',
               headers={'Authorization': 'Bearer tk_power'},
               json={'mensagem': 'oi'})
    r = c.post('/api/bot/copilot/reset?telefone=5511999990000',
               headers={'Authorization': 'Bearer tk_power'})
    assert r.get_json() == {'ok': True, 'apagada': True}
    from app.models import ChatbotConversa
    with app.app_context():
        assert ChatbotConversa.query.filter_by(
            conv_id='wpp-power-5511999990000').count() == 0


def test_power_sem_owner_no_sistema_503(app):
    """Se nao tem usuario owner ativo, Power se recusa (sem contexto de
    permissao pra filtrar tools)."""
    app.config['BOT_API_TOKEN'] = 'tk_power'
    app.config['BOT_ALLOWED_PHONES'] = '5511999990000'
    c = app.test_client()
    r = c.post('/api/bot/copilot?telefone=5511999990000',
               headers={'Authorization': 'Bearer tk_power'},
               json={'mensagem': 'oi'})
    assert r.status_code == 503
    assert 'owner' in r.get_json()['erro']


def test_tool_consultar_cartinhas_no_copilot_svc(app, admin_user):
    """A tool nova existe, e o dispatcher do _executar_read sabe chama-la."""
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
        nomes = [t['name'] for t in cs.TOOLS]
        assert 'consultar_cartinhas' in nomes
        u = Usuario.query.get(uid)
        r = cs._executar_read('consultar_cartinhas', {'dias': 2}, u)
        assert 'VND-AAA' in r['texto'] and r['total'] == 1
