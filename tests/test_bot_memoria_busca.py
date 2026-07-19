"""Achados do auditor 19/07/2026 — memória do bot + busca de pedido.

Dois problemas confirmados em código:
1. "Bot perdendo contexto e reiniciando conversa do zero": store chaveado
   só pelo conv_id do Chatwoot (conversa nova = zero) + vassoura
   sobrescrevendo o store com a API limitada do Chatwoot.
2. "Handoff sem tentar resolver": consultar_pedido só localizava por
   NÚMERO — cliente sem número não tinha caminho de busca.

Correções testadas aqui: busca por telefone do canal (fail-closed),
vassoura store-first, sonda /api/claude/vigia-vereditos. A memória
cross-conversa (contato_key) tem seção própria no fim.
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from app.extensions import db
from app.utils import agora

TOKEN = 'token-de-teste-bem-longo-123'


def _pedido_online(codigo, telefone, dias_atras=1, status='pago',
                   telefone_destinatario=None, cartinha=None):
    from app.models import PedidoOnline, PedidoOnlineItem
    p = PedidoOnline(codigo=codigo, status=status,
                     nome_cliente='Cliente Teste',
                     email_cliente=f'{codigo.lower()}@example.com',
                     telefone_cliente=telefone,
                     telefone_destinatario=telefone_destinatario,
                     modo_entrega='agendada',
                     subtotal=Decimal('40'), frete_valor=Decimal('10'),
                     valor_total=Decimal('50'), cartinha=cartinha)
    p.criado_em = agora() - timedelta(days=dias_atras)
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoOnlineItem(pedido_id=p.id, kind='receita',
                                    nome='Sourdough', quantidade=2,
                                    preco_unitario=Decimal('20')))
    db.session.commit()
    return p


# ── Busca por telefone do canal (consultar_pedido sem número) ──────────────

def test_sem_numero_e_sem_telefone_pede_o_numero(app):
    from app.services import bot_tools
    with app.app_context():
        r = bot_tools.consultar_pedido('', telefone_contato='')
        assert 'erro' in r and 'número' in r['erro']


def test_um_pedido_no_telefone_vem_completo(app):
    """1 achado = ficha completa direto (mesma autorização do fluxo por
    número: o telefone do canal é a credencial)."""
    from app.services import bot_tools
    with app.app_context():
        _pedido_online('TELBUSCA1', '(11) 98888-7777',
                       cartinha='Feliz aniversário!')
        r = bot_tools.consultar_pedido(
            '', telefone_contato='+55 11 98888-7777')
        assert r['numero'] == 'TELBUSCA1'
        assert r['cartinha'] == 'Feliz aniversário!'
        assert r['total'] == 50.0


def test_varios_pedidos_viram_lista_compacta(app):
    from app.services import bot_tools
    with app.app_context():
        _pedido_online('TELBUSCA2', '11 97777-6666', dias_atras=3)
        _pedido_online('TELBUSCA3', '11 97777-6666', dias_atras=1)
        r = bot_tools.consultar_pedido('', telefone_contato='11977776666')
        nums = [p['numero'] for p in r['pedidos_recentes']]
        assert nums == ['TELBUSCA3', 'TELBUSCA2']   # mais recente primeiro
        # lista compacta NÃO expõe cartinha/itens — só o suficiente pra
        # perguntar qual é
        assert 'cartinha' not in r['pedidos_recentes'][0]
        assert 'consultar_pedido' in r['instrucao']


def test_telefone_de_outro_cliente_nao_vaza(app):
    from app.services import bot_tools
    with app.app_context():
        _pedido_online('TELBUSCA4', '11 96666-5555')
        r = bot_tools.consultar_pedido('', telefone_contato='11 95555-4444')
        assert r['erro'] == 'nenhum_pedido_para_este_telefone'


def test_pedido_antigo_fora_da_janela_nao_entra(app):
    from app.services import bot_tools
    with app.app_context():
        _pedido_online('TELBUSCA5', '11 94444-3333', dias_atras=120)
        r = bot_tools.consultar_pedido('', telefone_contato='11 94444-3333')
        assert r['erro'] == 'nenhum_pedido_para_este_telefone'


def test_telefone_do_destinatario_tambem_localiza(app):
    """Presente: quem recebe pode perguntar do pedido (mesma regra da
    autorização por telefone já aceita no fluxo por número)."""
    from app.services import bot_tools
    with app.app_context():
        _pedido_online('TELBUSCA6', '11 93333-2222',
                       telefone_destinatario='11 92222-1111')
        r = bot_tools.consultar_pedido('', telefone_contato='11922221111')
        assert r['numero'] == 'TELBUSCA6'


# ── Vassoura store-first (não sobrescreve o contexto local) ────────────────

def test_vassoura_usa_store_como_base_e_nao_perde_turnos(app):
    """O store (40 msgs + marcadores) é a base; a API do Chatwoot (20 msgs,
    instável) só diz o que falta responder. Antes: a vassoura salvava a
    versão da API por cima e turnos antigos sumiam."""
    from app.services import chatbot
    with app.app_context():
        app.config['CHATWOOT_URL'] = 'https://x.example'
        antigas = []
        for i in range(12):
            antigas.append({'role': 'user', 'content': f'pergunta {i}'})
            antigas.append({'role': 'assistant', 'content': f'resposta {i}'})
        chatbot.salvar_historico('880', antigas, '')
        # API devolve só um recorte curto terminando na msg pendente
        api_hist = [{'role': 'assistant', 'content': 'resposta 11'},
                    {'role': 'user', 'content': 'e o meu pedido?'}]
        with patch('app.services.chatwoot.listar_conversas_paradas',
                   return_value=[{'id': 880, 'minutos_paradas': 30,
                                  'telefone': '+55 11 98888-7777'}]), \
                patch('app.services.chatwoot.buscar_historico',
                      return_value=api_hist), \
                patch('app.services.chatwoot.enviar_mensagem',
                      return_value={'ok': True}), \
                patch('app.services.chatbot.responder',
                      return_value={'acao': 'responder',
                                    'texto': 'Seu pedido está a caminho!'}) \
                as resp:
            chatbot.varrer_pendentes_sem_resposta()
        # O responder recebeu o CONTEXTO INTEIRO do store + a msg pendente
        hist_usado = resp.call_args[0][0]
        assert {'role': 'user', 'content': 'pergunta 0'} in hist_usado
        assert hist_usado[-1] == {'role': 'user', 'content': 'e o meu pedido?'}
        # ... e o telefone do canal foi passado (autorização de pedido)
        assert resp.call_args.kwargs['telefone_contato'] == '1188887777'
        # O store persistido manteve os turnos antigos
        store = chatbot.carregar_historico('880')
        assert {'role': 'user', 'content': 'pergunta 0'} in store
        assert store[-1]['content'] == 'Seu pedido está a caminho!'


def test_vassoura_nao_duplica_msg_ja_salva_no_store(app):
    """Crash DEPOIS do salvar mas antes do enviar: a msg pendente já está no
    store — a vassoura não pode duplicá-la."""
    from app.services import chatbot
    with app.app_context():
        app.config['CHATWOOT_URL'] = 'https://x.example'
        chatbot.salvar_historico('881', [
            {'role': 'user', 'content': 'oi'},
            {'role': 'assistant', 'content': 'Olá!'},
            {'role': 'user', 'content': 'tem sourdough?'},
        ], '')
        api_hist = [{'role': 'user', 'content': 'tem sourdough?'}]
        with patch('app.services.chatwoot.listar_conversas_paradas',
                   return_value=[{'id': 881, 'minutos_paradas': 20}]), \
                patch('app.services.chatwoot.buscar_historico',
                      return_value=api_hist), \
                patch('app.services.chatwoot.enviar_mensagem',
                      return_value={'ok': True}), \
                patch('app.services.chatbot.responder',
                      return_value={'acao': 'responder',
                                    'texto': 'Temos sim!'}) as resp:
            chatbot.varrer_pendentes_sem_resposta()
        hist_usado = resp.call_args[0][0]
        assert [m['content'] for m in hist_usado].count('tem sourdough?') == 1


# ── Lock cross-worker (SQLite = no-op; smoke) ──────────────────────────────

def test_lock_cross_worker_e_noop_no_sqlite(app):
    from app.blueprints.crm.routes import _lock_conv_cross_worker
    with app.app_context():
        with _lock_conv_cross_worker('123'):
            pass   # não pode explodir nem travar fora do Postgres


# ── Sonda /api/claude/vigia-vereditos ──────────────────────────────────────

def test_sonda_vereditos_exige_token(app):
    app.config['CLAUDE_API_TOKEN'] = TOKEN
    resp = app.test_client().get('/api/claude/vigia-vereditos',
                                 headers={'Authorization': 'Bearer errado'})
    assert resp.status_code == 401


def test_contexto_do_contato_herda_ultimas_msgs_com_marcador(app):
    """Conversa nova do MESMO contato herda o fim da conversa anterior +
    marcador de 'conversa anterior' (mesclado no último assistant pra manter
    a alternância user/assistant)."""
    from app.services import chatbot
    with app.app_context():
        chatbot.salvar_historico('700', [
            {'role': 'user', 'content': 'fiz o pedido ABC123'},
            {'role': 'assistant', 'content': 'Chega amanhã às 10h!'},
        ], '', contato_key='1188887777')
        ctx = chatbot.contexto_do_contato('1188887777', excluir_conv='701')
        assert ctx[0] == {'role': 'user', 'content': 'fiz o pedido ABC123'}
        assert ctx[-1]['role'] == 'assistant'
        assert 'Chega amanhã às 10h!' in ctx[-1]['content']
        assert 'conversa\nANTERIOR' in ctx[-1]['content'].replace(
            'conversa ANTERIOR', 'conversa\nANTERIOR')
        assert 'não se apresente' in ctx[-1]['content']


def test_contexto_do_contato_ignora_propria_conversa_e_janela(app):
    from datetime import timedelta

    from app.models import ChatbotConversa
    from app.services import chatbot
    with app.app_context():
        chatbot.salvar_historico('710', [
            {'role': 'user', 'content': 'oi'}], 'Olá!',
            contato_key='1177776666')
        # de OUTRA conversa, o contexto vem…
        assert chatbot.contexto_do_contato(
            '1177776666', excluir_conv='711')[0]['content'] == 'oi'
        # …mas a própria conversa não é "contexto anterior" (excluída)
        assert chatbot.contexto_do_contato(
            '1177776666', excluir_conv='710') == []
        # fora da janela de 30 dias: não herda
        conv = ChatbotConversa.query.filter_by(conv_id='710').one()
        conv.ultima_msg_em = agora() - timedelta(days=45)
        db.session.commit()
        assert chatbot.contexto_do_contato(
            '1177776666', excluir_conv='711') == []
        # sem chave: nada
        assert chatbot.contexto_do_contato('', excluir_conv='x') == []


def test_salvar_historico_none_nao_apaga_contato_key(app):
    from app.models import ChatbotConversa
    from app.services import chatbot
    with app.app_context():
        chatbot.salvar_historico('720', [
            {'role': 'user', 'content': 'oi'}], 'Olá!',
            contato_key='1166665555')
        # turno seguinte sem telefone (ex: caminho sem sender) NÃO apaga
        chatbot.salvar_historico('720', [
            {'role': 'user', 'content': 'oi'},
            {'role': 'assistant', 'content': 'Olá!'},
            {'role': 'user', 'content': 'tem pão?'}], 'Temos!')
        conv = ChatbotConversa.query.filter_by(conv_id='720').one()
        assert conv.contato_key == '1166665555'


def test_webhook_conversa_nova_herda_contexto_do_mesmo_contato(app):
    """FIM A FIM do achado do auditor: cliente volta dias depois, Chatwoot
    abre conversa NOVA — o bot recebe o contexto da anterior em vez de
    recomeçar do zero."""
    from app.services import chatbot
    from tests.test_chatbot import _SyncThread
    app.config['CHATWOOT_BOT_SECRET'] = 'seg'
    with app.app_context():
        chatbot.salvar_historico('730', [
            {'role': 'user', 'content': 'comprei a cesta média ontem'},
            {'role': 'assistant', 'content': 'Pedido confirmado pra sexta!'},
        ], '', contato_key='1155554444')
    payload = {
        'event': 'message_created', 'message_type': 'incoming',
        'id': 73001, 'content': 'oi, e meu pedido?',
        'conversation': {'id': 731, 'status': 'pending',
                          'meta': {'sender': {'name': 'Maria',
                                              'phone_number': '+5511955554444'}}},
        'sender': {'name': 'Maria', 'phone_number': '+5511955554444'},
    }
    with patch('threading.Thread', _SyncThread), \
            patch('app.services.chatwoot.buscar_historico',
                  return_value=[]), \
            patch('app.services.chatbot.responder',
                  return_value={'acao': 'responder',
                                'texto': 'Chega sexta!'}) as resp, \
            patch('app.services.chatwoot.enviar_mensagem',
                  return_value={'ok': True}):
        r = app.test_client().post('/crm/bot?k=seg', json=payload)
    assert r.status_code == 200
    hist = resp.call_args[0][0]
    # herdou a conversa anterior + marcador + msg atual no fim
    assert hist[0]['content'] == 'comprei a cesta média ontem'
    assert 'conversa' in hist[1]['content'] and 'ANTERIOR' in hist[1]['content']
    assert hist[-1] == {'role': 'user', 'content': 'oi, e meu pedido?'}
    # e a conversa NOVA ficou indexada pelo contato
    with app.app_context():
        from app.models import ChatbotConversa
        conv = ChatbotConversa.query.filter_by(conv_id='731').one()
        assert conv.contato_key == '1155554444'


def test_sonda_vereditos_lista_do_banco_e_traz_store(app):
    from app.models import VigiaVeredito
    from app.services import chatbot
    app.config['CLAUDE_API_TOKEN'] = TOKEN
    with app.app_context():
        db.session.add(VigiaVeredito(
            conv_id='990', cliente='Maria', mensagem_cliente='cadê meu pedido',
            bot_acao='handoff', bot_motivo='pedido nao encontrado',
            alerta=True, gravidade='media', motivo_vigia='transferiu sem tool',
            tools_usadas='[]'))
        db.session.commit()
        chatbot.salvar_historico('990', [
            {'role': 'user', 'content': 'cadê meu pedido'}], 'Já vejo!')
    resp = app.test_client().get(
        '/api/claude/vigia-vereditos?dias=1&conversa=990',
        headers={'Authorization': f'Bearer {TOKEN}'})
    assert resp.status_code == 200
    d = resp.get_json()
    v = next(x for x in d['vereditos'] if x['conv_id'] == '990')
    assert v['bot_acao'] == 'handoff'
    assert v['motivo_vigia'] == 'transferiu sem tool'
    assert d['conversa']['existe_no_store'] is True
    assert d['conversa']['mensagens'][-1]['content'] == 'Já vejo!'
