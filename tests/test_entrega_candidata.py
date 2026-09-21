"""Entrega candidata pela rua + nota privada do handoff (21/09/2026).

Dono (caso conv 2409): "o boy deveria confirmar pelo menos o nome da rua
para poder passar o número". Versão segura: a rua identifica a entrega
INTERNAMENTE e o resultado vai à equipe numa NOTA PRIVADA do Chatwoot,
junto do motivo do handoff. O contato nunca recebe código, número,
complemento ou destinatário; rua e código NÃO viram credencial no bot.
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from app.extensions import db
from app.utils import hoje


class _SyncThread:
    def __init__(self, target=None, daemon=None, **kw):
        self._target = target

    def start(self):
        if self._target:
            self._target()


def _pedido(codigo, *, rua='Rua Serra da Bocaina', numero='547', bairro='Quarta Parada',
            data=None, modo='express', status='pago', destinatario='Matheus',
            logradouro_vazio=False):
    from app.models import Cliente, PedidoOnline
    cli = Cliente(nome='Luiz Cristovao', email=f'{codigo.lower()}@x.com',
                  telefone='11988887777')
    db.session.add(cli)
    db.session.flush()
    linha = f'{rua}, {numero}, Apto 902, {bairro}, São Paulo, SP'
    p = PedidoOnline(
        codigo=codigo, cliente_id=cli.id, nome_cliente='Luiz Cristovao',
        email_cliente=cli.email, telefone_cliente='11988887777',
        nome_destinatario=destinatario, modo_entrega=modo, status=status,
        endereco_entrega=linha, endereco_cep='03185000',
        endereco_logradouro=None if logradouro_vazio else rua,
        endereco_numero=numero, endereco_bairro=bairro, endereco_cidade='São Paulo',
        endereco_uf='SP', data_entrega=data or hoje(), janela_entrega='imediato',
        subtotal=Decimal('300'), frete_valor=Decimal('10'), valor_total=Decimal('310'))
    db.session.add(p)
    db.session.commit()
    return p


def _hist(*falas_cliente, bot=None):
    h = []
    for f in falas_cliente:
        h.append({'role': 'user', 'content': f})
        if bot:
            h.append({'role': 'assistant', 'content': bot})
    return h


# ── 1. Tokens do logradouro ──

def test_tokens_logradouro_tira_tipo_de_via_e_conectivos():
    from app.services.entrega_candidata import _tokens_logradouro as tk
    assert tk('Rua Serra da Bocaina') == ['serra', 'bocaina']
    assert tk('Av. Nova York') == ['nova', 'york']
    assert tk('R. 25 de Março') == ['25', 'marco']
    assert tk('Rua A') == []          # curta demais pra identificar
    assert tk('') == []


# ── 2. Match por rua ──

def test_casa_uma_entrega_de_hoje_pela_rua_inteira(app):
    from app.services import entrega_candidata as ec
    with app.app_context():
        _pedido('B276C19B')
        hist = _hist('Me liga por favor', 'É uma entrega de uma cesta da Lalamove',
                     'Estou na Rua Serra da Bocaina e ninguém atende')
        cands = ec.candidatas_por_rua(hist)
    assert [c['codigo'] for c in cands] == ['B276C19B']
    assert cands[0]['hoje'] and cands[0]['modo'] == 'express'
    assert cands[0]['bairro'] == 'Quarta Parada' and cands[0]['numero'] == '547'


def test_rua_parcial_nao_casa(app):
    """'Rua Serra' não casa 'Rua Serra da Bocaina' — todos os tokens do
    PEDIDO precisam estar na fala, nunca o contrário."""
    from app.services import entrega_candidata as ec
    with app.app_context():
        _pedido('B276C19B')
        assert ec.candidatas_por_rua(_hist('estou na rua serra')) == []
        assert ec.candidatas_por_rua(_hist('rua bocaina')) == []
        # sem acento e com abreviação da via, casa
        assert [c['codigo'] for c in ec.candidatas_por_rua(
            _hist('to na r serra da bocaina 547'))] == ['B276C19B']


def test_retirada_e_pedido_nao_pago_ficam_fora(app):
    from app.services import entrega_candidata as ec
    with app.app_context():
        _pedido('RET00001', modo='retirada')
        _pedido('AGP00001', status='aguardando_pagamento')
        _pedido('ENT00001', status='entregue')
        assert ec.candidatas_por_rua(_hist('Rua Serra da Bocaina')) == []


def test_fala_do_bot_nao_casa(app):
    """O bot repete o endereço do pedido consultado; só a fala do CONTATO conta."""
    from app.services import entrega_candidata as ec
    with app.app_context():
        _pedido('B276C19B')
        hist = [{'role': 'user', 'content': 'quero saber do meu pedido'},
                {'role': 'assistant', 'content': 'Seu pedido vai pra Rua Serra da Bocaina, 547'}]
        assert ec.candidatas_por_rua(hist) == []


def test_amanha_entra_e_hoje_vem_primeiro(app):
    from app.services import entrega_candidata as ec
    with app.app_context():
        _pedido('AMANHA01', data=hoje() + timedelta(days=1))
        _pedido('HOJE0001')
        _pedido('DEPOIS01', data=hoje() + timedelta(days=2))
        cands = ec.candidatas_por_rua(_hist('rua serra da bocaina'))
    assert [c['codigo'] for c in cands] == ['HOJE0001', 'AMANHA01']


def test_fallback_pela_linha_unica_quando_nao_ha_logradouro(app):
    from app.services import entrega_candidata as ec
    with app.app_context():
        _pedido('SEMLOG01', logradouro_vazio=True)
        cands = ec.candidatas_por_rua(_hist('estou na Rua Serra da Bocaina'))
    assert [c['codigo'] for c in cands] == ['SEMLOG01']


def test_sinal_lalamove_em_rua_desempata_e_aparece(app):
    from app.models import LalamoveEntrega
    from app.services import entrega_candidata as ec
    with app.app_context():
        _pedido('PARADO01')
        _pedido('NARUA001')
        db.session.add(LalamoveEntrega(pedido_code='NARUA001', order_id='ord-1',
                                       status='PICKED_UP', motorista_nome='Ale'))
        db.session.commit()
        cands = ec.candidatas_por_rua(_hist('rua serra da bocaina'))
    assert [c['codigo'] for c in cands] == ['NARUA001', 'PARADO01']
    assert cands[0]['sinal'] == 'Lalamove PICKED_UP (Ale)' and cands[0]['em_rua']


# ── 3. Nota privada do handoff ──

def test_nota_de_handoff_traz_motivo_ferramentas_e_candidata(app):
    from app.services import entrega_candidata as ec
    with app.app_context():
        _pedido('B276C19B')
        resultado = {'acao': 'handoff', 'motivo': 'entregador da Lalamove na Rua Serra da '
                     'Bocaina, ninguém atende, vai devolver',
                     'tools_resumo': ['consultar_pedido: nenhum pedido para este telefone']}
        hist = _hist('Sou o entregador da Lalamove, estou na Rua Serra da Bocaina '
                     'e ninguém atende')
        nota = ec.nota_de_handoff(resultado, hist)
    assert nota.startswith(ec.PREFIXO_NOTA_BOT)
    assert 'motivo: entregador da Lalamove' in nota
    assert 'Ferramentas: consultar_pedido' in nota
    assert ('Entrega candidata pela rua citada (conferir antes de agir): '
            'B276C19B — Rua Serra da Bocaina, 547 — Quarta Parada') in nota
    assert 'p/ Matheus' in nota and 'uso interno' in nota


# ── 2b. Revisão 21/09: logradouro de um token, falas antigas, divulgação ──

def test_logradouro_de_um_token_so_casa_com_tipo_de_via_na_fala(app):
    """'Rua Nova' × 'quero uma nova cesta' e 'Rua Pinheiros' × 'moro em
    Pinheiros' eram falsos positivos; 'rua augusta' casa 'Rua Augusta'."""
    from app.services import entrega_candidata as ec
    with app.app_context():
        _pedido('NOVA0001', rua='Rua Nova', bairro='Centro')
        _pedido('AUGUS001', rua='Rua Augusta', bairro='Consolação')
        assert ec.candidatas_por_rua(_hist('quero uma nova cesta pra amanhã')) == []
        assert ec.candidatas_por_rua(_hist('estou na augusta')) == []
        assert [c['codigo'] for c in ec.candidatas_por_rua(
            _hist('to na rua augusta 100, ninguém atende'))] == ['AUGUS001']
        assert [c['codigo'] for c in ec.candidatas_por_rua(
            _hist('entrega na R. Nova, 10'))] == ['NOVA0001']


def test_conectivo_entre_a_via_e_o_nome(app):
    """'Rua da Consolação', 'Rua dos Pinheiros', 'Av. Paulista' — o
    padrão mais comum de SP (revisão 21/09, 2ª rodada)."""
    from app.services import entrega_candidata as ec
    with app.app_context():
        _pedido('CONSOL01', rua='Rua da Consolação', bairro='Consolação')
        _pedido('PINHE001', rua='Rua dos Pinheiros', bairro='Pinheiros')
        _pedido('PAULI001', rua='Av. Paulista', bairro='Bela Vista')
        assert [c['codigo'] for c in ec.candidatas_por_rua(
            _hist('na rua da consolacao, ninguém atende'))] == ['CONSOL01']
        assert [c['codigo'] for c in ec.candidatas_por_rua(
            _hist('rua dos pinheiros 500'))] == ['PINHE001']
        assert [c['codigo'] for c in ec.candidatas_por_rua(
            _hist('to na av. paulista'))] == ['PAULI001']
        assert ec.candidatas_por_rua(_hist('moro em pinheiros, tem entrega?')) == []


def test_so_as_ultimas_falas_do_contato_entram(app):
    from app.services import entrega_candidata as ec
    with app.app_context():
        _pedido('B276C19B')
        antigas = ['pode deixar na portaria?', 'Rua Serra da Bocaina']
        recentes = [f'mensagem {i}' for i in range(ec._ULTIMAS_FALAS)]
        hist = _hist(*antigas, *recentes)
        assert ec.candidatas_por_rua(hist) == []
        assert not ec.terceiro_na_conversa(hist)
        # dentro da janela, conta
        assert ec.terceiro_na_conversa(_hist(*antigas, *recentes[:-2]))


def test_divulgacao_entra_no_match(app):
    from app.services import entrega_candidata as ec
    with app.app_context():
        _pedido('DIVULG01', status='divulgacao')
        assert [c['codigo'] for c in ec.candidatas_por_rua(
            _hist('rua serra da bocaina'))] == ['DIVULG01']


def test_nota_sem_terceiro_nao_procura_rua(app):
    """Cliente comum falando de frete numa rua com entrega de outro cliente:
    a nota leva só o motivo, sem apontar pedido alheio."""
    from app.services import entrega_candidata as ec
    with app.app_context():
        _pedido('B276C19B')
        with patch('app.services.entrega_candidata.candidatas_por_rua') as busca:
            nota = ec.nota_de_handoff(
                {'acao': 'handoff', 'motivo': 'cliente pediu atendente'},
                _hist('quero uma cesta pra Rua Serra da Bocaina, quanto fica o frete?'))
    busca.assert_not_called()
    assert 'candidata' not in nota and 'B276C19B' not in nota


def test_nota_com_duas_candidatas_lista_sem_escolher(app):
    from app.services import entrega_candidata as ec
    with app.app_context():
        _pedido('UM000001')
        _pedido('DOIS0001', numero='600', destinatario='Bia')
        nota = ec.nota_de_handoff(
            {'acao': 'handoff', 'motivo': 'portaria: cesta deixada na entrada'},
            _hist('Aqui é da portaria da Rua Serra da Bocaina, deixaram uma cesta'))
    assert '2 entregas na rua citada' in nota
    assert 'UM000001' in nota and 'DOIS0001' in nota


def test_nota_com_terceiro_e_sem_rua_reconhecida_avisa(app):
    from app.services import entrega_candidata as ec
    with app.app_context():
        nota = ec.nota_de_handoff(
            {'acao': 'handoff', 'motivo': 'entregador da Lalamove sem contato com o cliente'},
            _hist('É uma entrega de uma cesta da Lalamove, ninguém atende'))
    assert 'sem rua reconhecida' in nota


# ── 4. O handoff do webhook posta a nota; a resposta normal não ──

def _post(client, **over):
    payload = {'event': 'message_created', 'id': 4242, 'message_type': 'incoming',
               'conversation': {'id': 2409, 'status': 'pending'},
               'content': 'Sou o entregador, estou na Rua Serra da Bocaina e ninguém atende',
               'sender': {'name': 'Ale', 'phone_number': '+5511910935006'}}
    payload.update(over)
    return client.post('/crm/bot?k=seg', json=payload)


def test_handoff_no_webhook_posta_nota_privada_e_fala_publica_nao_vaza(app):
    app.config['CHATWOOT_BOT_SECRET'] = 'seg'
    c = app.test_client()
    with app.app_context():
        _pedido('B276C19B')
    resultado = {'acao': 'handoff', 'texto': 'Já estou te passando pra equipe.',
                 'motivo': 'entregador da Lalamove na Rua Serra da Bocaina, ninguém atende',
                 'tools_usadas': [], 'tools_resumo': []}
    with patch('threading.Thread', _SyncThread), \
         patch('app.services.chatwoot.buscar_historico', return_value=[]), \
         patch('app.services.chatbot.responder', return_value=resultado), \
         patch('app.services.chatwoot.enviar_mensagem', return_value={'ok': True}) as env, \
         patch('app.services.chatwoot.enviar_nota_privada', return_value={'ok': True}) as nota, \
         patch('app.services.chatwoot.definir_status', return_value={'ok': True}), \
         patch('app.services.chatbot_vigia.disponivel', return_value=False):
        r = _post(c)
    assert r.status_code == 200
    env.assert_called_once()
    assert 'B276C19B' not in env.call_args[0][1]          # nada ao contato
    nota.assert_called_once()
    assert nota.call_args[0][0] == 2409
    assert 'B276C19B — Rua Serra da Bocaina, 547' in nota.call_args[0][1]


def test_resposta_normal_nao_posta_nota(app):
    app.config['CHATWOOT_BOT_SECRET'] = 'seg'
    c = app.test_client()
    with patch('threading.Thread', _SyncThread), \
         patch('app.services.chatwoot.buscar_historico', return_value=[]), \
         patch('app.services.chatbot.responder',
               return_value={'acao': 'responder', 'texto': 'Oi!'}), \
         patch('app.services.chatwoot.enviar_mensagem', return_value={'ok': True}), \
         patch('app.services.chatwoot.enviar_nota_privada') as nota, \
         patch('app.services.chatbot_vigia.disponivel', return_value=False):
        _post(c, content='oi')
    nota.assert_not_called()


def test_nota_do_proprio_bot_nao_cala_o_bot(app):
    """A nossa nota de handoff volta pelo webhook como nota privada; nem
    pelo remetente (agent_bot) nem pelo prefixo ela vira 'equipe presente'."""
    from app.models import PresencaHumanaConversa
    from app.services.entrega_candidata import PREFIXO_NOTA_BOT
    app.config['CHATWOOT_BOT_SECRET'] = 'seg'
    c = app.test_client()
    base = {'event': 'message_created', 'id': 4300, 'message_type': 'outgoing',
            'private': True, 'conversation': {'id': 2409, 'status': 'open'}}
    with app.app_context(), patch('threading.Thread', _SyncThread):
        r1 = c.post('/crm/bot?k=seg', json={**base, 'content': f'{PREFIXO_NOTA_BOT} x',
                                           'sender': {'type': 'agent_bot', 'name': 'Bot'}})
        r2 = c.post('/crm/bot?k=seg', json={**base, 'id': 4301,
                                           'content': f'  {PREFIXO_NOTA_BOT} transferido',
                                           'sender': {'type': 'user', 'name': 'Painel'}})
        assert r1.get_json()['ignorado'] == 'nota-nao-humana'
        assert r2.get_json()['ignorado'] == 'nota-nao-humana'
        assert db.session.get(PresencaHumanaConversa, '2409') is None


def test_enviar_nota_privada_usa_token_do_bot_e_private_true(app):
    from unittest.mock import MagicMock

    from app.services import chatwoot
    fake = MagicMock(status_code=200, text='{}')
    with app.app_context():
        app.config['CHATWOOT_URL'] = 'https://x.example'
        app.config['CHATWOOT_ACCOUNT_ID'] = '1'
        app.config['CHATWOOT_BOT_TOKEN'] = 'bot-tok'
        with patch('app.services.chatwoot.requests.post', return_value=fake) as post:
            res = chatwoot.enviar_nota_privada(2409, 'x')
    assert res == {'ok': True}
    kwargs = post.call_args.kwargs
    assert kwargs['json'] == {'content': 'x', 'message_type': 'outgoing', 'private': True}
    assert kwargs['headers']['api_access_token'] == 'bot-tok'
