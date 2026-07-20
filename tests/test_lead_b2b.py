"""Leads B2B capturados pelo bot de atendimento (16/07/2026; fluxo revisto
20/07/2026 — decisão do dono: o bot NÃO envia catálogo; captura nome +
e-mail + WhatsApp, registra, TRANSFERE a conversa pra equipe e o dono
recebe WhatsApp com o link da conversa).

Anthropic/Z-API SEMPRE mockadas.
"""
from unittest.mock import patch

from app.extensions import db
from app.models import LeadB2B
from app.services import bot_tools

# ── Tool registrar_lead_b2b ──────────────────────────────────────────────

def test_registra_lead_e_avisa_dono_com_link_da_conversa(app):
    with app.app_context():
        app.config['ZAPI_BOT_DONO_NUMERO'] = '5511900000000'
        app.config['CHATWOOT_URL'] = 'https://atendimento.opao.x'
        app.config['CHATWOOT_ACCOUNT_ID'] = '1'
        with patch('app.services.zapi.enviar_texto',
                   return_value={'ok': True}) as z:
            r = bot_tools.registrar_lead_b2b(
                'Ana Souza', 'ana@cafeteria.com', '(11) 98888-7777',
                empresa='Cafeteria da Ana',
                interesse='croissants pra revenda, ~50/semana',
                conversa_id=482)
        assert r['ok'] is True and r['ja_registrado'] is False
        assert 'transfira' in r['proximo_passo'].lower()
        assert 'catalogo_url' not in r
        lead = db.session.get(LeadB2B, r['lead_id'])
        assert lead.email == 'ana@cafeteria.com'
        assert lead.telefone == '11988887777'
        assert lead.conversa_id == 482
        z.assert_called_once()
        msg = z.call_args[0][1]
        assert 'Ana Souza' in msg and 'ana@cafeteria.com' in msg
        # Link da CONVERSA no aviso (decisão do dono 20/07/2026).
        assert 'Conversa:' in msg and '482' in msg


def test_aviso_sem_conversa_id_sai_sem_link(app):
    with app.app_context():
        app.config['ZAPI_BOT_DONO_NUMERO'] = '5511900000000'
        with patch('app.services.zapi.enviar_texto',
                   return_value={'ok': True}) as z:
            r = bot_tools.registrar_lead_b2b('Bruno C', 'b@emp.com',
                                             '11977776666')
        assert r['ok'] is True
        assert 'Conversa:' not in z.call_args[0][1]


def test_valida_email_e_telefone(app):
    with app.app_context():
        r = bot_tools.registrar_lead_b2b('Ana', 'sem-arroba', '11988887777')
        assert 'erro' in r and 'mail' in r['erro'].lower()
        r2 = bot_tools.registrar_lead_b2b('Ana', 'a@b.com', '123')
        assert 'erro' in r2 and 'telefone' in r2['erro'].lower()
        r3 = bot_tools.registrar_lead_b2b('', 'a@b.com', '11988887777')
        assert 'erro' in r3 and 'nome' in r3['erro'].lower()
        assert LeadB2B.query.count() == 0


def test_email_maiusculo_normaliza_e_gigante_recusa(app):
    with app.app_context():
        with patch('app.services.zapi.enviar_texto', return_value={'ok': True}):
            r = bot_tools.registrar_lead_b2b('Ana', 'ANA@Cafeteria.COM',
                                             '11988887777')
        assert r['ok'] is True
        assert LeadB2B.query.first().email == 'ana@cafeteria.com'
        gigante = ('a' * 250) + '@x.com'
        r2 = bot_tools.registrar_lead_b2b('Bia', gigante, '11977776666')
        assert 'erro' in r2


def test_nome_gigante_e_truncado_no_cap_da_coluna(app):
    with app.app_context():
        with patch('app.services.zapi.enviar_texto', return_value={'ok': True}):
            r = bot_tools.registrar_lead_b2b('N' * 400, 'n@x.com',
                                             '11988887777')
        assert r['ok'] is True
        assert len(LeadB2B.query.first().nome) == 150


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
                interesse='quer revender croissants')
        assert r1['ok'] and r2['ok']
        assert r2['ja_registrado'] is True
        assert LeadB2B.query.count() == 1
        lead = LeadB2B.query.first()
        assert lead.nome == 'Ana Souza'
        assert 'croissants' in lead.interesse


def test_dedupe_email_primeiro_nao_faz_merge_destrutivo(app):
    """Email de A + telefone de B numa chamada só: atualiza A (identidade =
    e-mail) e NUNCA sobrescreve o e-mail de B (achado do revisor)."""
    with app.app_context():
        with patch('app.services.zapi.enviar_texto', return_value={'ok': True}):
            bot_tools.registrar_lead_b2b('Ana', 'a@x.com', '11911111111')
            bot_tools.registrar_lead_b2b('Bia', 'b@y.com', '11922222222')
            bot_tools.registrar_lead_b2b('Ana S', 'a@x.com', '11922222222')
        assert LeadB2B.query.count() == 2
        b = LeadB2B.query.filter_by(email='b@y.com').first()
        assert b is not None and b.nome == 'Bia'  # B intacto
        a = LeadB2B.query.filter_by(email='a@x.com').first()
        assert a.nome == 'Ana S'                  # A atualizado


def test_aviso_zapi_falhando_nao_quebra_registro(app):
    with app.app_context():
        app.config['ZAPI_BOT_DONO_NUMERO'] = '5511900000000'
        with patch('app.services.zapi.enviar_texto',
                   side_effect=Exception('zapi fora')):
            r = bot_tools.registrar_lead_b2b('Ana', 'a@b.com', '11988887777')
        assert r['ok'] is True
        assert LeadB2B.query.count() == 1


# ── Registro no bot (TOOLS + _executar_tool) + prompt ────────────────────

def test_tool_registrada_e_catalogo_removido(app):
    """registrar_lead_b2b registrada; catalogo_b2b NÃO pode voltar (decisão
    do dono 20/07/2026: o bot não envia catálogo)."""
    from app.services.chatbot import TOOLS, _executar_tool
    nomes = [t['name'] for t in TOOLS]
    assert 'registrar_lead_b2b' in nomes
    assert 'catalogo_b2b' not in nomes
    with app.app_context():
        with patch('app.services.zapi.enviar_texto', return_value={'ok': True}):
            r = _executar_tool('registrar_lead_b2b',
                               {'nome': 'Ana', 'email': 'a@b.com',
                                'telefone': '', 'empresa': 'Café X'},
                               telefone_contato='5511966665555',
                               conversa_id=77)
        assert r['ok'] is True
        lead = LeadB2B.query.first()
        assert lead.telefone == '5511966665555'
        assert lead.conversa_id == 77
        assert _executar_tool('catalogo_b2b', {}).get('erro')


def test_prompt_tem_secao_b2b_com_transferencia():
    from app.services.chatbot_prompt import PROMPT
    assert 'CLIENTE B2B / ATACADO / REVENDA' in PROMPT
    assert 'Capture O CONTATO antes de transferir' in PROMPT
    assert 'TRANSFIRA a conversa' in PROMPT
    assert 'NÃO envie cardápio, catálogo' in PROMPT
    # O fluxo antigo ("registrar já resolve, não transfira") NÃO pode voltar.
    assert 'não transfira para humano por interesse B2B' not in PROMPT.lower()


def test_nomes_das_tools_no_filtro_de_vazamento():
    from app.services.chatbot import _OUTPUT_VAZOU_MARCADORES
    assert 'registrar_lead_b2b' in _OUTPUT_VAZOU_MARCADORES


def test_loop_do_bot_registra_e_transfere(app):
    """Fluxo completo do dono (20/07): registrar o lead → transferir pra
    equipe. O handoff vem DEPOIS de uma tool → não é 'preguiçoso' nem é
    bloqueado pelo enforcement (Anthropic mockada)."""
    from types import SimpleNamespace

    from app.services import chatbot
    tool_use = SimpleNamespace(
        content=[SimpleNamespace(
            type='tool_use', name='registrar_lead_b2b', id='tu1',
            input={'nome': 'Ana', 'email': 'ana@caf.com',
                   'telefone': '11988887777',
                   'interesse': 'revenda de croissants'})],
        stop_reason='tool_use')
    handoff = SimpleNamespace(
        content=[SimpleNamespace(
            type='tool_use', name='transferir_para_humano', id='tu2',
            input={'motivo': 'lead B2B/atacado',
                   'resumo': 'Cafeteria quer revender croissants',
                   'mensagem_cliente': ('Perfeito! Já passei seus dados pra '
                                        'nossa equipe comercial — um '
                                        'atendente continua com você.')})],
        stop_reason='tool_use')
    with app.app_context():
        app.config['ANTHROPIC_API_KEY'] = 'test'
        with patch('anthropic.Anthropic') as M, \
                patch('app.services.zapi.enviar_texto',
                      return_value={'ok': True}):
            M.return_value.messages.create.side_effect = [tool_use, handoff]
            out = chatbot.responder(
                [{'role': 'user',
                  'content': 'quero revender os croissants de vocês'}],
                telefone_contato='5511988887777', conversa_id=99)
        assert out['acao'] == 'handoff'
        assert 'equipe comercial' in out['texto']
        assert 'registrar_lead_b2b' in out['tools_usadas']
        lead = LeadB2B.query.first()
        assert lead is not None
        assert lead.conversa_id == 99


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
