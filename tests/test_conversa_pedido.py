"""Item 8 (spec do dono, caso E3862E49, 22/09/2026): vínculo conversa ↔
pedido e nota privada à equipe quando o status da entrega muda.

O pedido E3862E49 gerou 3 conversas (2429 WhatsApp de saída, 2431 Instagram
da compradora, 2432 WhatsApp do marido). Agora o vínculo é gravado quando
o bot identifica o pedido, quando a equipe clica "Chamar cliente" e quando
a Lalamove abre a conversa; na mudança de status, cada conversa ABERTA do
pedido recebe nota privada listando as outras. Nunca mensagem ao cliente.
Chatwoot/Anthropic sempre mockados; threads rodam em linha.
"""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from app.extensions import db


class _PoolInline:
    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


def _pedido(codigo='E3862E49', telefone='11988887777', status='pago'):
    from app.models import Cliente, PedidoOnline
    cli = Cliente(nome='Cintia', email=f'{codigo.lower()}@x.com', telefone=telefone)
    db.session.add(cli)
    db.session.flush()
    p = PedidoOnline(codigo=codigo, status=status, cliente_id=cli.id, nome_cliente='Cintia',
                     email_cliente=cli.email, telefone_cliente=telefone,
                     modo_entrega='agendada', subtotal=Decimal('100'),
                     frete_valor=Decimal('0'), valor_total=Decimal('100'))
    db.session.add(p)
    db.session.commit()
    return p


def _vinculos(code='E3862E49'):
    from app.models import ConversaPedido
    return {(r.conv_id, r.origem) for r in ConversaPedido.query.filter_by(pedido_code=code).all()}


def _autorizacao(code='E3862E49'):
    from app.models import ConversaPedido
    return {r.conv_id: r.autorizada for r in ConversaPedido.query.filter_by(pedido_code=code).all()}


# ── vincular ────────────────────────────────────────────────────────────

def test_vincular_idempotente_e_ignora_conversa_invalida(app):
    from app.services import conversa_pedido
    with app.app_context():
        assert conversa_pedido.vincular(2431, 'e3862e49', 'bot') is True
        assert conversa_pedido.vincular('2431', 'E3862E49', 'chamar') is False   # já existe
        assert conversa_pedido.vincular('teste-x', 'E3862E49', 'bot') is False  # conv não numérica
        assert conversa_pedido.vincular(2432, '', 'bot') is False
        assert conversa_pedido.vincular(2432, 'E3862E49', 'chamar') is True
        assert conversa_pedido.conversas_do_pedido('e3862e49') == ['2431', '2432']
        assert _vinculos() == {('2431', 'bot'), ('2432', 'chamar')}
        assert conversa_pedido.conversas_do_pedido('e3862e49', detalhado=True) == [
            {'conv_id': '2431', 'autorizada': True, 'origem': 'bot'},
            {'conv_id': '2432', 'autorizada': True, 'origem': 'chamar'}]


def test_vincular_nao_autorizada_e_promovida_por_autorizacao_posterior(app):
    """Terceiro que só tem o código (revisão 23/09/2026): o vínculo nasce
    `autorizada=False`; se a mesma conversa provar posse depois, promove."""
    from app.services import conversa_pedido
    with app.app_context():
        assert conversa_pedido.vincular(2432, 'E3862E49', 'bot', autorizada=False) is True
        assert _autorizacao() == {'2432': False}
        # repetir sem autorização não muda nada
        assert conversa_pedido.vincular(2432, 'E3862E49', 'bot', autorizada=False) is False
        assert _autorizacao() == {'2432': False}
        # autorização posterior promove (não é vínculo novo → False)
        assert conversa_pedido.vincular(2432, 'E3862E49', 'bot', autorizada=True) is False
        assert _autorizacao() == {'2432': True}
        # autorizada nunca é rebaixada por uma consulta não autorizada depois
        assert conversa_pedido.vincular(2432, 'E3862E49', 'socorro', autorizada=False) is False
        assert _autorizacao() == {'2432': True}


def test_vincular_tolera_corrida_no_unique(app):
    """Dois turnos da mesma conversa gravando o mesmo par: o 2º bate no
    unique e vira "já existe" (info no log), não exceção nem ERROR."""
    from sqlalchemy.exc import IntegrityError

    from app.services import conversa_pedido
    with app.app_context():
        assert conversa_pedido.vincular(2431, 'E3862E49', 'bot') is True
        # Simula a corrida: a leitura não vê a linha (outro processo gravou
        # entre o SELECT e o INSERT) e o commit estoura o unique.

        class _QueryVazia:
            def filter_by(self, **kw):
                return self

            def first(self):
                return None

        with patch('sqlalchemy.orm.Session.query', return_value=_QueryVazia()), \
                patch('sqlalchemy.orm.Session.commit',
                      side_effect=IntegrityError('insert', {}, Exception('uq_conversa_pedido'))), \
                patch('app.services.conversa_pedido.logger') as log:
            assert conversa_pedido.vincular(2431, 'E3862E49', 'bot') is False
        log.exception.assert_not_called()
        assert any('corrida' in str(c) for c in log.info.call_args_list)
        assert _vinculos() == {('2431', 'bot')}


# ── bot identifica o pedido ─────────────────────────────────────────────

def _cliente_com_tool(monkeypatch, tool_input):
    """Modelo mockado: 1º turno chama consultar_pedido, 2º responde texto."""
    blk = SimpleNamespace(type='tool_use', name='consultar_pedido', id='t1', input=tool_input)
    chamadas = {'n': 0}

    class FakeClient:
        def __init__(self, **kw):
            def create(**kw):
                chamadas['n'] += 1
                if chamadas['n'] == 1:
                    return SimpleNamespace(content=[blk], stop_reason='tool_use')
                return SimpleNamespace(
                    content=[SimpleNamespace(type='text', text='Achei seu pedido!')],
                    stop_reason='end_turn')
            self.messages = SimpleNamespace(create=create)
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-x')
    monkeypatch.setattr('anthropic.Anthropic', FakeClient)


def test_bot_autorizado_vincula_a_conversa(app, monkeypatch):
    from app.services import chatbot
    with app.app_context():
        _pedido()
        _cliente_com_tool(monkeypatch, {'numero': 'E3862E49'})
        r = chatbot.responder([{'role': 'user', 'content': 'cadê meu pedido E3862E49?'}],
                              telefone_contato='11988887777', conversa_id=2431)
        assert r['acao'] == 'responder'
        assert _vinculos() == {('2431', 'bot')}


def test_bot_pedido_existente_sem_autorizacao_vincula_mas_modelo_nao_ve(app, monkeypatch):
    """Terceiro sem credencial: o vínculo é gravado (a nota interna já diz o
    código à equipe), mas `_pedido_existente` nunca chega ao modelo."""
    import json

    from app.services import chatbot
    with app.app_context():
        _pedido()
        vistos = []
        blk = SimpleNamespace(type='tool_use', name='consultar_pedido', id='t1',
                              input={'numero': 'E3862E49'})

        class FakeClient:
            def __init__(self, **kw):
                def create(**kw):
                    for m in kw.get('messages') or []:
                        if isinstance(m.get('content'), list):
                            for c in m['content']:
                                if isinstance(c, dict) and c.get('type') == 'tool_result':
                                    vistos.append(json.loads(c['content']))
                    if not vistos:
                        return SimpleNamespace(content=[blk], stop_reason='tool_use')
                    return SimpleNamespace(
                        content=[SimpleNamespace(type='text', text='Me confirma o CPF?')],
                        stop_reason='end_turn')
                self.messages = SimpleNamespace(create=create)
        monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-x')
        monkeypatch.setattr('anthropic.Anthropic', FakeClient)
        chatbot.responder([{'role': 'user', 'content': 'vi o pedido E3862E49 da minha esposa'}],
                          telefone_contato='11900000000', conversa_id=2432)
        assert _vinculos() == {('2432', 'bot')}
        # Sem prova de posse o vínculo nasce NÃO autorizado (revisão 23/09):
        # a equipe vê a conversa listada, mas o status da entrega não vai lá.
        assert _autorizacao() == {'2432': False}
        assert vistos and vistos[0]['erro'] == 'autorizacao_necessaria'
        assert '_pedido_existente' not in vistos[0] and '_nota_interna' not in vistos[0]


def test_bot_autorizado_grava_vinculo_autorizado(app, monkeypatch):
    from app.services import chatbot
    with app.app_context():
        _pedido()
        _cliente_com_tool(monkeypatch, {'numero': 'E3862E49'})
        chatbot.responder([{'role': 'user', 'content': 'cadê meu pedido E3862E49?'}],
                          telefone_contato='11988887777', conversa_id=2431)
        assert _autorizacao() == {'2431': True}


def test_lista_de_varios_pedidos_nao_vincula(app, monkeypatch):
    from app.services import chatbot
    with app.app_context():
        _pedido('AAA11111')
        p2 = _pedido('BBB22222')
        # dois pedidos do mesmo telefone: o 2º sem Cliente próprio (e-mail é unique)
        from app.models import PedidoOnline
        PedidoOnline.query.filter_by(codigo='BBB22222').update({'cliente_id': None})
        db.session.commit()
        assert p2.telefone_cliente == '11988887777'
        _cliente_com_tool(monkeypatch, {'numero': ''})
        chatbot.responder([{'role': 'user', 'content': 'cadê meu pedido?'}],
                          telefone_contato='11988887777', conversa_id=2440)
        from app.models import ConversaPedido
        assert ConversaPedido.query.count() == 0


def test_socorro_vincula_o_pedido_localizado(app):
    from app.services import chatbot
    with app.app_context():
        _pedido()
        app.config['ANTHROPIC_API_KEY'] = 'test'
        with patch('anthropic.Anthropic'):
            r = chatbot.responder([{'role': 'user', 'content': 'não recebi meu pedido'}],
                                  telefone_contato='11988887777', conversa_id=2431)
        assert r['acao'] == 'handoff'
        assert _vinculos() == {('2431', 'socorro')}


# ── "Chamar cliente" e Lalamove ─────────────────────────────────────────

def test_chamar_cliente_vincula_a_conversa(app, admin_user):
    with app.app_context():
        _pedido()
        app.config['CHATWOOT_WHATSAPP_TEMPLATE'] = 'duvida_pedido'
    c = app.test_client()
    with c.session_transaction() as sess:
        sess['_user_id'] = str(admin_user.id)
        sess['_fresh'] = True
    with patch('app.services.chatwoot.iniciar_conversa_whatsapp',
               return_value={'ok': True, 'conversation_id': 2429, 'nova': True,
                             'erro': None, 'aberta': True}):
        r = c.post('/entregas/api/atendimento/chamar-cliente', json={'codigo': 'E3862E49'})
    assert r.status_code == 200
    with app.app_context():
        assert _vinculos() == {('2429', 'chamar')}


def test_lalamove_vincula_e_avisa_as_conversas_abertas(app):
    """Corrida cancelada: a conversa local vira vínculo 'lalamove' e as
    conversas ABERTAS do pedido recebem a nota com as outras."""
    from app.models import ChatbotConversa, LalamoveEntrega
    from app.services import lalamove_alerta
    from app.utils import agora, hoje, telefone_chave
    with app.app_context():
        app.config['LOJA_ALERTA_NUMERO'] = '5511999990000'
        _pedido(status='a_caminho')
        e = LalamoveEntrega(pedido_code='E3862E49', data_ref=hoje(), order_id='ord-9',
                            status='ON_GOING', destinatario='Cintia')
        db.session.add(e)
        db.session.add(ChatbotConversa(conv_id='2432', contato_key=telefone_chave('11988887777'),
                                       mensagens_json='[]', ultima_msg_em=agora()))
        db.session.commit()
        from app.services import conversa_pedido
        conversa_pedido.vincular(2431, 'E3862E49', 'bot')      # Instagram da compradora
        conversa_pedido.vincular(2429, 'E3862E49', 'chamar')   # já resolvida
        estados = {'2431': {'status': 'open', 'meta': {'channel': 'Channel::Instagram'}},
                   '2432': {'status': 'pending', 'meta': {'channel': 'Channel::Whatsapp'}},
                   '2429': {'status': 'resolved', 'meta': {'channel': 'Channel::Whatsapp'}}}
        with patch('app.services.lalamove_alerta._POOL', _PoolInline()), \
                patch('app.services.zapi.enviar_texto', return_value={'ok': True}), \
                patch('app.services.chatwoot.definir_status', return_value={'ok': True}), \
                patch('app.services.chatwoot.consultar_conversa',
                      side_effect=lambda cid: estados.get(str(cid))), \
                patch('app.services.chatwoot.enviar_nota_privada',
                      return_value={'ok': True}) as nota, \
                patch('app.services.chatwoot.enviar_mensagem',
                      side_effect=AssertionError('nunca ao cliente')):
            lalamove_alerta.tratar_encerramento(e, 'CANCELED', 'ON_GOING',
                                                {'data': {'order': {'cancelReason': 'sem motoboy'}}})
        assert _vinculos() == {('2431', 'bot'), ('2429', 'chamar'), ('2432', 'lalamove')}
        avisadas = {k.args[0] for k in nota.call_args_list}
        assert avisadas == {'2431', '2432'}            # resolvida não recebe
        texto_2431 = next(k.args[1] for k in nota.call_args_list if k.args[0] == '2431')
        assert 'E3862E49' in texto_2431 and 'CANCELADA' in texto_2431.upper()
        assert '#2432 (WhatsApp, com o bot)' in texto_2431
        assert '#2431' not in texto_2431.split('Outras conversas')[1]
        assert 'sem motoboy' in texto_2431


# ── mudança de status da entrega ────────────────────────────────────────

def test_avancar_status_avisa_so_as_conversas_abertas(app):
    from app.services import conversa_pedido, loja_entrega
    with app.app_context():
        _pedido(status='pago')
        conversa_pedido.vincular(2431, 'E3862E49', 'bot')
        conversa_pedido.vincular(2432, 'E3862E49', 'bot')
        conversa_pedido.vincular(2429, 'E3862E49', 'chamar')
        estados = {'2431': {'status': 'open', 'meta': {'channel': 'Channel::Instagram'}},
                   '2432': {'status': 'open', 'meta': {'channel': 'Channel::Whatsapp'}},
                   '2429': {'status': 'resolved', 'meta': {}}}
        with patch('app.services.conversa_pedido._POOL', _PoolInline()), \
                patch('app.services.email.disponivel', return_value=False), \
                patch('app.services.chatwoot.consultar_conversa',
                      side_effect=lambda cid: estados.get(str(cid))), \
                patch('app.services.chatwoot.enviar_nota_privada',
                      return_value={'ok': True}) as nota, \
                patch('app.services.chatwoot.enviar_mensagem',
                      side_effect=AssertionError('nunca ao cliente')):
            loja_entrega.avancar_status_entrega('E3862E49', 'a_caminho')
        from app.models import PedidoOnline
        assert PedidoOnline.query.filter_by(codigo='E3862E49').one().status == 'a_caminho'
        assert {k.args[0] for k in nota.call_args_list} == {'2431', '2432'}
        t = next(k.args[1] for k in nota.call_args_list if k.args[0] == '2432')
        assert 'A CAMINHO' in t and '#2431 (Instagram, aberta)' in t
        assert 'o cliente NÃO recebeu mensagem' in t
        # única aberta: nota diz isso, sem lista
        estados['2431']['status'] = 'resolved'
        with patch('app.services.conversa_pedido._POOL', _PoolInline()), \
                patch('app.services.email.disponivel', return_value=False), \
                patch('app.services.chatwoot.consultar_conversa',
                      side_effect=lambda cid: estados.get(str(cid))), \
                patch('app.services.chatwoot.enviar_nota_privada',
                      return_value={'ok': True}) as nota2:
            loja_entrega.avancar_status_entrega('E3862E49', 'entregue')
        assert [k.args[0] for k in nota2.call_args_list] == ['2432']
        assert 'única conversa aberta' in nota2.call_args.args[1]


def test_sem_vinculo_nao_consulta_o_chatwoot(app):
    from app.services import loja_entrega
    with app.app_context():
        _pedido(status='pago')
        with patch('app.services.conversa_pedido._POOL', _PoolInline()), \
                patch('app.services.email.disponivel', return_value=False), \
                patch('app.services.chatwoot.consultar_conversa') as cc:
            loja_entrega.avancar_status_entrega('E3862E49', 'a_caminho')
        cc.assert_not_called()


def test_chatwoot_fora_nao_derruba_o_avanco_de_status(app):
    from app.services import conversa_pedido, loja_entrega
    with app.app_context():
        _pedido(status='pago')
        conversa_pedido.vincular(2431, 'E3862E49', 'bot')
        with patch('app.services.conversa_pedido._POOL', _PoolInline()), \
                patch('app.services.email.disponivel', return_value=False), \
                patch('app.services.chatwoot.consultar_conversa',
                      side_effect=RuntimeError('chatwoot fora')):
            loja_entrega.avancar_status_entrega('E3862E49', 'entregue')
        from app.models import PedidoOnline
        assert PedidoOnline.query.filter_by(codigo='E3862E49').one().status == 'entregue'
