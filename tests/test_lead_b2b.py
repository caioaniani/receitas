"""Leads B2B capturados pelo bot de atendimento (16/07/2026).

O bot detecta interesse em atacado/revenda, captura nome + e-mail + WhatsApp
(tool registrar_lead_b2b), pode enviar o link do catálogo (tool catalogo_b2b,
AppConfig/env) e o dono acompanha em /b2b/leads. Anthropic/Z-API SEMPRE
mockadas.
"""
from unittest.mock import patch

from app.extensions import db
from app.models import LeadB2B
from app.services import bot_tools

# ── Tool registrar_lead_b2b ──────────────────────────────────────────────

def test_registra_lead_e_avisa_dono(app):
    with app.app_context():
        app.config['ZAPI_BOT_DONO_NUMERO'] = '5511900000000'
        with patch('app.services.zapi.enviar_texto',
                   return_value={'ok': True}) as z:
            r = bot_tools.registrar_lead_b2b(
                'Ana Souza', 'ana@cafeteria.com', '(11) 98888-7777',
                empresa='Cafeteria da Ana',
                interesse='croissants pra revenda, ~50/semana')
        assert r['ok'] is True and r['ja_registrado'] is False
        lead = db.session.get(LeadB2B, r['lead_id'])
        assert lead.email == 'ana@cafeteria.com'
        assert lead.telefone == '11988887777'
        assert lead.empresa == 'Cafeteria da Ana'
        assert lead.contatado_em is None
        z.assert_called_once()
        msg = z.call_args[0][1]
        assert 'Ana Souza' in msg and 'ana@cafeteria.com' in msg


def test_valida_email_e_telefone(app):
    with app.app_context():
        r = bot_tools.registrar_lead_b2b('Ana', 'sem-arroba', '11988887777')
        assert 'erro' in r and 'mail' in r['erro'].lower()
        r2 = bot_tools.registrar_lead_b2b('Ana', 'a@b.com', '123')
        assert 'erro' in r2 and 'telefone' in r2['erro'].lower()
        r3 = bot_tools.registrar_lead_b2b('', 'a@b.com', '11988887777')
        assert 'erro' in r3 and 'nome' in r3['erro'].lower()
        assert LeadB2B.query.count() == 0


def test_telefone_da_conversa_como_fallback(app):
    """Cliente diz "esse número mesmo": o telefone vem vazio do modelo e o
    canal injeta o telefone_contato (mesmo mecanismo do consultar_pedido)."""
    with app.app_context():
        with patch('app.services.zapi.enviar_texto', return_value={'ok': True}):
            r = bot_tools.registrar_lead_b2b(
                'Bruno', 'bruno@empresa.com', '',
                telefone_contato='5511977776666')
        assert r['ok'] is True
        lead = db.session.get(LeadB2B, r['lead_id'])
        assert lead.telefone == '5511977776666'


def test_dedupe_24h_atualiza_em_vez_de_duplicar(app):
    with app.app_context():
        with patch('app.services.zapi.enviar_texto', return_value={'ok': True}):
            r1 = bot_tools.registrar_lead_b2b('Ana', 'ana@x.com', '11988887777')
            r2 = bot_tools.registrar_lead_b2b(
                'Ana Souza', 'ana@x.com', '11988887777',
                interesse='quer o catálogo', catalogo_enviado=True)
        assert r1['ok'] and r2['ok']
        assert r2['ja_registrado'] is True
        assert LeadB2B.query.count() == 1
        lead = LeadB2B.query.first()
        assert lead.nome == 'Ana Souza'
        assert lead.catalogo_enviado is True
        assert 'catálogo' in lead.interesse


def test_aviso_zapi_falhando_nao_quebra_registro(app):
    with app.app_context():
        app.config['ZAPI_BOT_DONO_NUMERO'] = '5511900000000'
        with patch('app.services.zapi.enviar_texto',
                   side_effect=Exception('zapi fora')):
            r = bot_tools.registrar_lead_b2b('Ana', 'a@b.com', '11988887777')
        assert r['ok'] is True
        assert LeadB2B.query.count() == 1


# ── Tool catalogo_b2b ────────────────────────────────────────────────────

def test_catalogo_b2b_appconfig_env_e_vazio(app):
    from app.models import AppConfig
    with app.app_context():
        # Sem nada: orienta (não inventa URL).
        app.config['CATALOGO_B2B_URL'] = ''
        r = bot_tools.catalogo_b2b()
        assert r['url'] is None and 'aviso' in r
        # Env como fallback.
        app.config['CATALOGO_B2B_URL'] = 'https://env.exemplo/catalogo.pdf'
        assert bot_tools.catalogo_b2b()['url'] == 'https://env.exemplo/catalogo.pdf'
        # AppConfig sobrepõe a env.
        AppConfig.set('catalogo_b2b_url', 'https://drop.exemplo/cat.pdf')
        assert bot_tools.catalogo_b2b()['url'] == 'https://drop.exemplo/cat.pdf'


def test_lead_devolve_catalogo_url_quando_configurado(app):
    from app.models import AppConfig
    with app.app_context():
        AppConfig.set('catalogo_b2b_url', 'https://drop.exemplo/cat.pdf')
        with patch('app.services.zapi.enviar_texto', return_value={'ok': True}):
            r = bot_tools.registrar_lead_b2b('Ana', 'a@b.com', '11988887777')
        assert r['catalogo_url'] == 'https://drop.exemplo/cat.pdf'


# ── Registro no bot (TOOLS + _executar_tool) + prompt ────────────────────

def test_tools_registradas_no_bot(app):
    from app.services.chatbot import TOOLS, _executar_tool
    nomes = [t['name'] for t in TOOLS]
    assert 'registrar_lead_b2b' in nomes
    assert 'catalogo_b2b' in nomes
    with app.app_context():
        with patch('app.services.zapi.enviar_texto', return_value={'ok': True}):
            r = _executar_tool('registrar_lead_b2b',
                               {'nome': 'Ana', 'email': 'a@b.com',
                                'telefone': '', 'empresa': 'Café X'},
                               telefone_contato='5511966665555')
        assert r['ok'] is True
        lead = LeadB2B.query.first()
        assert lead.telefone == '5511966665555'
        r2 = _executar_tool('catalogo_b2b', {})
        assert 'url' in r2


def test_prompt_tem_secao_b2b():
    from app.services.chatbot_prompt import PROMPT
    assert 'CLIENTE B2B / ATACADO / REVENDA' in PROMPT
    assert 'não transfira para humano por interesse B2B'.lower() \
        in PROMPT.lower()
    assert 'Revenda/atacado NÃO é encomenda de evento' in PROMPT


def test_loop_do_bot_registra_lead(app):
    """Fluxo completo: modelo pede a tool → executor grava → resposta final
    confirma ao cliente (Anthropic mockada, padrão test_chatbot)."""
    from types import SimpleNamespace

    from app.services import chatbot
    tool_use = SimpleNamespace(
        content=[SimpleNamespace(
            type='tool_use', name='registrar_lead_b2b', id='tu1',
            input={'nome': 'Ana', 'email': 'ana@caf.com',
                   'telefone': '11988887777',
                   'interesse': 'revenda de croissants'})],
        stop_reason='tool_use')
    texto = SimpleNamespace(
        content=[SimpleNamespace(
            type='text',
            text='Anotei! Nossa equipe comercial entra em contato. 🥐')],
        stop_reason='end_turn')
    with app.app_context():
        app.config['ANTHROPIC_API_KEY'] = 'test'
        with patch('anthropic.Anthropic') as M, \
                patch('app.services.zapi.enviar_texto',
                      return_value={'ok': True}):
            M.return_value.messages.create.side_effect = [tool_use, texto]
            out = chatbot.responder(
                [{'role': 'user',
                  'content': 'quero revender os croissants de vocês'}],
                telefone_contato='5511988887777')
        assert out['acao'] == 'responder'
        assert 'equipe comercial' in out['texto']
        assert 'registrar_lead_b2b' in out['tools_usadas']
        assert LeadB2B.query.count() == 1


# ── Tela /b2b/leads ──────────────────────────────────────────────────────

def _login(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def test_tela_leads_lista_e_marca_contatado(app, admin_user):
    with app.app_context():
        lead = LeadB2B(nome='Ana', empresa='Café X', email='a@b.com',
                       telefone='11988887777', interesse='revenda')
        db.session.add(lead)
        db.session.commit()
        lid = lead.id
    client = app.test_client()
    _login(client, admin_user)
    html = client.get('/b2b/leads').data.decode()
    assert 'Ana' in html and 'a@b.com' in html and 'novo' in html
    r = client.post(f'/b2b/leads/{lid}/contatado')
    assert r.status_code in (302, 303)
    with app.app_context():
        assert db.session.get(LeadB2B, lid).contatado_em is not None
    # Pendentes (default) esconde o contatado; ?todos=1 mostra.
    assert 'a@b.com' not in client.get('/b2b/leads').data.decode()
    assert 'a@b.com' in client.get('/b2b/leads?todos=1').data.decode()


def test_tela_salva_link_do_catalogo(app, admin_user):
    from app.models import AppConfig
    client = app.test_client()
    _login(client, admin_user)
    r = client.post('/b2b/leads/catalogo-url',
                    data={'url': 'https://drop.exemplo/catalogo.pdf'})
    assert r.status_code in (302, 303)
    with app.app_context():
        assert AppConfig.get('catalogo_b2b_url') == 'https://drop.exemplo/catalogo.pdf'
    # Link sem http(s) é recusado (o bot mandaria um link quebrado).
    client.post('/b2b/leads/catalogo-url', data={'url': 'drop.exemplo/x'})
    with app.app_context():
        assert AppConfig.get('catalogo_b2b_url') == 'https://drop.exemplo/catalogo.pdf'
