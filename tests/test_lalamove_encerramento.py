"""Corrida Lalamove encerrada SEM entrega (CANCELED/EXPIRED/REJECTED) —
23/09/2026. Antes o webhook só gravava o status e ninguém sabia.

Contrato: VigiaVeredito ALTA (banner + som do painel) com código do pedido
e motivo; WhatsApp CRÍTICO ao dono; conversa do cliente no Chatwoot → open
SEM mensagem ao cliente; pedido NÃO vira entregue nem muda de status;
cancelamento feito pelo painel e reentrega do webhook não alertam.
Z-API e Chatwoot sempre mockados; a thread roda inline.
"""
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.extensions import db


class _PoolInline:
    """Substitui o ThreadPoolExecutor: executa na hora (teste determinístico)."""

    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


@pytest.fixture
def cfg(app):
    app.config['LALAMOVE_API_KEY'] = 'pk_test_chave'
    app.config['LALAMOVE_API_SECRET'] = 'sk_test_segredo'
    app.config['LOJA_ALERTA_NUMERO'] = '5511999990000'
    app.config['CHATWOOT_URL'] = 'https://cw.example'
    app.config['CHATWOOT_ACCOUNT_ID'] = '1'
    app.config['CHATWOOT_API_TOKEN'] = 'tok-user'
    app.config['CHATWOOT_BOT_TOKEN'] = 'tok-bot'
    app.config['CHATWOOT_WHATSAPP_INBOX_ID'] = '7'
    return app


def _pedido(codigo, telefone='11988887777', status='a_caminho'):
    from app.models import PedidoOnline
    from app.utils import hoje
    p = PedidoOnline(
        codigo=codigo, nome_cliente='Caio Cliente', email_cliente=f'{codigo.lower()}@x.com',
        telefone_cliente=telefone, modo_entrega='express', status=status,
        endereco_entrega='Rua Michigan, 560, São Paulo', endereco_cep='04571000',
        data_entrega=hoje(), janela_entrega='14:00–15:00',
        subtotal=Decimal('100'), frete_valor=Decimal('30'), valor_total=Decimal('130'))
    db.session.add(p)
    db.session.commit()
    return p


def _corrida(codigo, order_id, status='ON_GOING'):
    from app.models import LalamoveEntrega
    from app.utils import hoje
    e = LalamoveEntrega(pedido_code=codigo, data_ref=hoje(), order_id=order_id,
                        status=status, endereco_destino='Rua Michigan, 560',
                        destinatario='Caio Cliente', telefone_destino='11988887777',
                        motorista_nome='Carlos M', motorista_telefone='+5511977776666')
    db.session.add(e)
    db.session.commit()
    return e


def _conversa_local(conv_id, telefone='11988887777'):
    from app.models import ChatbotConversa
    from app.utils import agora, telefone_chave
    c = ChatbotConversa(conv_id=str(conv_id), contato_key=telefone_chave(telefone),
                        mensagens_json='[]', ultima_msg_em=agora())
    db.session.add(c)
    db.session.commit()


def _webhook(c, order_id, status, extra_order=None, extra_data=None):
    ordem = {'orderId': order_id, 'status': status}
    ordem.update(extra_order or {})
    data = {'order': ordem}
    data.update(extra_data or {})
    return c.post('/lalamove/webhook', json={
        'apiKey': 'pk_test_chave', 'eventType': 'ORDER_STATUS_CHANGED',
        'data': data})


def _vereditos():
    from app.models import VigiaVeredito
    return VigiaVeredito.query.filter_by(bot_acao='lalamove').all()


def test_canceled_alerta_painel_dono_e_abre_conversa(cfg):
    from app.models import LalamoveEntrega, PedidoOnline
    app = cfg
    with app.app_context():
        _pedido('LAL001')
        _corrida('LAL001', 'ord-1')
        _conversa_local(4321)
    c = app.test_client()
    with patch('app.services.lalamove_alerta._POOL', _PoolInline()), \
            patch('app.services.zapi.enviar_texto', return_value={'ok': True}) as zap, \
            patch('app.services.chatwoot.definir_status', return_value={'ok': True}) as st, \
            patch('app.services.chatwoot.enviar_mensagem',
                  side_effect=AssertionError('nunca escreve pro cliente')), \
            patch('app.services.loja_entrega.avancar_status_entrega') as adv:
        r = _webhook(c, 'ord-1', 'CANCELED', extra_order={'cancelReason': 'Driver unavailable'})
    assert r.status_code == 200
    with app.app_context():
        e = LalamoveEntrega.query.filter_by(order_id='ord-1').one()
        assert e.status == 'CANCELED'                    # status registrado
        p = PedidoOnline.query.filter_by(codigo='LAL001').one()
        assert p.status == 'a_caminho'                   # pedido intocado
        vs = _vereditos()
        assert len(vs) == 1
        v = vs[0]
        assert v.alerta is True and v.gravidade == 'alta'
        assert 'LAL001' in v.mensagem_cliente and 'Driver unavailable' in v.mensagem_cliente
        assert 'LAL001' in v.motivo_vigia and 'Driver unavailable' in v.motivo_vigia
        assert v.conv_id == '4321' and v.enviado_whatsapp is True
        assert v.reconhecido_em is None
    adv.assert_not_called()                              # nunca "entregue"
    # WhatsApp ao dono pelo caminho CRÍTICO, com código, motivo e motorista
    zap.assert_called_once()
    numero, texto = zap.call_args.args[:2]
    assert numero == '5511999990000' and zap.call_args.kwargs['critico'] is True
    assert 'LAL001' in texto and 'Driver unavailable' in texto
    assert 'Cancelada'.upper() in texto.upper() and 'Carlos M' in texto
    assert 'NÃO foi entregue' in texto
    # conversa do cliente vai pra fila da equipe, sem texto ao cliente
    st.assert_called_once()
    assert st.call_args.args == ('4321', 'open')


def test_expired_e_rejected_tambem_alertam_com_rotulo(cfg):
    app = cfg
    with app.app_context():
        _pedido('LAL002')
        _pedido('LAL003')
        _corrida('LAL002', 'ord-2', status='ASSIGNING_DRIVER')
        _corrida('LAL003', 'ord-3', status='ASSIGNING_DRIVER')
    c = app.test_client()
    with patch('app.services.lalamove_alerta._POOL', _PoolInline()), \
            patch('app.services.zapi.enviar_texto', return_value={'ok': True}) as zap, \
            patch('app.services.chatwoot.definir_status', return_value={'ok': True}), \
            patch('app.services.chatwoot._buscar_contato', return_value=None):
        assert _webhook(c, 'ord-2', 'EXPIRED').status_code == 200
        assert _webhook(c, 'ord-3', 'REJECTED').status_code == 200
    textos = [call.args[1] for call in zap.call_args_list]
    assert len(textos) == 2
    assert 'EXPIROU SEM ENTREGADOR' in textos[0].upper() and 'LAL002' in textos[0]
    assert 'RECUSADA PELA LALAMOVE' in textos[1].upper() and 'LAL003' in textos[1]
    assert 'motivo não informado pela Lalamove' in textos[0]
    with app.app_context():
        prefixos = sorted(v.mensagem_cliente.split(']')[0] for v in _vereditos())
        assert prefixos == ['[LALAMOVE EXPIRED', '[LALAMOVE REJECTED']


def test_reentrega_do_webhook_nao_duplica(cfg):
    app = cfg
    with app.app_context():
        _pedido('LAL004')
        _corrida('LAL004', 'ord-4')
    c = app.test_client()
    with patch('app.services.lalamove_alerta._POOL', _PoolInline()), \
            patch('app.services.zapi.enviar_texto', return_value={'ok': True}) as zap, \
            patch('app.services.chatwoot.definir_status', return_value={'ok': True}), \
            patch('app.services.chatwoot._buscar_contato', return_value=None):
        _webhook(c, 'ord-4', 'CANCELED')
        _webhook(c, 'ord-4', 'CANCELED')      # reentrega: anterior == status
    assert zap.call_count == 1
    with app.app_context():
        assert len(_vereditos()) == 1


def test_dedupe_cruza_mesmo_com_status_regredido(cfg):
    """Evento atrasado (ON_GOING depois do CANCELED) e novo CANCELED: o
    marcador em VigiaVeredito segura o 2º alerta da MESMA corrida."""
    app = cfg
    with app.app_context():
        _pedido('LAL005')
        _corrida('LAL005', 'ord-5')
    c = app.test_client()
    with patch('app.services.lalamove_alerta._POOL', _PoolInline()), \
            patch('app.services.zapi.enviar_texto', return_value={'ok': True}) as zap, \
            patch('app.services.chatwoot.definir_status', return_value={'ok': True}), \
            patch('app.services.chatwoot._buscar_contato', return_value=None):
        _webhook(c, 'ord-5', 'CANCELED')
        _webhook(c, 'ord-5', 'ON_GOING')
        _webhook(c, 'ord-5', 'CANCELED')
    assert zap.call_count == 1
    with app.app_context():
        assert len(_vereditos()) == 1


def test_cancelamento_pelo_painel_nao_alerta(cfg, admin_user):
    """A equipe cancelou no painel (a rota grava CANCELED antes do webhook):
    o evento que a Lalamove devolve depois não é novidade pra ninguém."""
    from app.models import LalamoveEntrega
    app = cfg
    with app.app_context():
        _pedido('LAL006')
        e = _corrida('LAL006', 'ord-6')
        eid = e.id
    c = app.test_client()
    with c.session_transaction() as sess:
        sess['_user_id'] = str(admin_user.id)
        sess['_fresh'] = True
    with patch('app.services.lalamove.cancelar', return_value={'ok': True}):
        r = c.post('/entregas/api/painel/lalamove/cancelar', json={'entrega_id': eid})
    assert r.get_json()['lalamove']['status'] == 'CANCELED'
    with patch('app.services.lalamove_alerta._POOL', _PoolInline()), \
            patch('app.services.zapi.enviar_texto') as zap, \
            patch('app.services.chatwoot.definir_status') as st:
        _webhook(c, 'ord-6', 'CANCELED')
    zap.assert_not_called()
    st.assert_not_called()
    with app.app_context():
        assert LalamoveEntrega.query.filter_by(order_id='ord-6').one().status == 'CANCELED'
        assert _vereditos() == []


def test_sem_conversa_local_procura_no_chatwoot_e_grava_conv(cfg):
    app = cfg
    with app.app_context():
        _pedido('LAL007')
        _corrida('LAL007', 'ord-7')
    c = app.test_client()
    with patch('app.services.lalamove_alerta._POOL', _PoolInline()), \
            patch('app.services.zapi.enviar_texto', return_value={'ok': True}), \
            patch('app.services.chatwoot._buscar_contato',
                  return_value={'id': 77, 'contact_inboxes': []}) as bc, \
            patch('app.services.chatwoot._conversa_aberta_do_contato',
                  return_value=9001) as ca, \
            patch('app.services.chatwoot.definir_status', return_value={'ok': True}) as st:
        _webhook(c, 'ord-7', 'CANCELED')
    assert bc.call_args.args[0] == '+5511988887777'
    assert ca.call_args.args == (77, '7')
    assert st.call_args.args == ('9001', 'open')
    with app.app_context():
        assert _vereditos()[0].conv_id == '9001'


def test_telefone_internacional_nao_consulta_chatwoot(cfg):
    """Cliente com telefone internacional: não há contato de WhatsApp pra
    achar — alerta sai igual, sem GET no Chatwoot."""
    app = cfg
    with app.app_context():
        _pedido('LAL008', telefone='+14752929850')
        _corrida('LAL008', 'ord-8')
    c = app.test_client()
    with patch('app.services.lalamove_alerta._POOL', _PoolInline()), \
            patch('app.services.zapi.enviar_texto', return_value={'ok': True}) as zap, \
            patch('app.services.chatwoot._buscar_contato') as bc, \
            patch('app.services.chatwoot.definir_status') as st:
        _webhook(c, 'ord-8', 'CANCELED')
    zap.assert_called_once()
    bc.assert_not_called()
    st.assert_not_called()


def test_falha_de_rede_nao_derruba_webhook_nem_perde_veredito(cfg):
    app = cfg
    with app.app_context():
        _pedido('LAL009')
        _corrida('LAL009', 'ord-9')
        _conversa_local(4444)
    c = app.test_client()
    with patch('app.services.lalamove_alerta._POOL', _PoolInline()), \
            patch('app.services.zapi.enviar_texto', side_effect=RuntimeError('zapi fora')), \
            patch('app.services.chatwoot.definir_status', side_effect=RuntimeError('cw fora')):
        r = _webhook(c, 'ord-9', 'CANCELED')
    assert r.status_code == 200
    with app.app_context():
        v = _vereditos()[0]
        assert v.enviado_whatsapp is False and v.conv_id == '4444'


def test_pedido_sem_pedido_online_ainda_alerta(cfg):
    """Corrida de pedido local/VNDA (sem PedidoOnline): o alerta sai pelo
    código; só não há conversa nem telefone pra procurar."""
    app = cfg
    with app.app_context():
        _corrida('VND-77', 'ord-10')
    c = app.test_client()
    with patch('app.services.lalamove_alerta._POOL', _PoolInline()), \
            patch('app.services.zapi.enviar_texto', return_value={'ok': True}) as zap, \
            patch('app.services.chatwoot._buscar_contato') as bc:
        assert _webhook(c, 'ord-10', 'EXPIRED').status_code == 200
    assert 'VND-77' in zap.call_args.args[1]
    bc.assert_not_called()


def test_banner_do_painel_mostra_o_alerta(cfg, admin_user):
    """Sem request anônima antes da logada (armadilha do `g._login_user`
    compartilhado no conftest): o serviço é chamado direto, o painel via HTTP."""
    from app.services import lalamove_alerta
    app = cfg
    with app.app_context():
        _pedido('LAL011')
        e = _corrida('LAL011', 'ord-11')
        with patch('app.services.lalamove_alerta._POOL', _PoolInline()), \
                patch('app.services.zapi.enviar_texto', return_value={'ok': True}), \
                patch('app.services.chatwoot._buscar_contato', return_value=None):
            res = lalamove_alerta.tratar_encerramento(
                e, 'CANCELED', 'ON_GOING', {'data': {'order': {'orderId': 'ord-11'}}})
        assert res['ok'] is True and res['veredito_id']
    c = app.test_client()
    with c.session_transaction() as sess:
        sess['_user_id'] = str(admin_user.id)
        sess['_fresh'] = True
    with patch('app.services.vnda.buscar_pedidos_do_dia', return_value={'pedidos': []}):
        d = c.get('/entregas/api/painel').get_json()
    assert d['vigia']['pendentes'] == 1
    assert 'LAL011' in d['vigia']['ultimo']['motivo']
    assert 'Lalamove' in d['vigia']['ultimo']['motivo']


def test_canceled_depois_de_completed_nao_alerta(cfg):
    """Evento fora de ordem (CANCELED chegando após COMPLETED) ou pedido já
    entregue: alertar "não entregue" mentiria."""
    from app.services import lalamove_alerta
    app = cfg
    with app.app_context():
        _pedido('LAL013', status='entregue')
        e = _corrida('LAL013', 'ord-13', status='COMPLETED')
        with patch('app.services.zapi.enviar_texto') as zap:
            res = lalamove_alerta.tratar_encerramento(e, 'CANCELED', 'COMPLETED', {})
        assert res == {'ok': True, 'ignorado': 'ja_entregue'}
        zap.assert_not_called()
        assert _vereditos() == []


def test_contencao_nao_sai_na_conversa_aberta_pelo_alerta(cfg):
    """A conversa que o alerta pôs em `open` NÃO pode receber "atendimento
    em alta demanda" no cron seguinte (contrato: sem mensagem automática ao
    cliente); a cobrança ao dono sai, dizendo que é corrida encerrada."""
    from app.services import chatbot_vigia, lalamove_alerta
    app = cfg
    with app.app_context():
        _pedido('LAL014')
        e = _corrida('LAL014', 'ord-14')
        _conversa_local(5150)
        with patch('app.services.lalamove_alerta._POOL', _PoolInline()), \
                patch('app.services.zapi.enviar_texto', return_value={'ok': True}), \
                patch('app.services.chatwoot.definir_status', return_value={'ok': True}):
            lalamove_alerta.tratar_encerramento(e, 'CANCELED', 'ON_GOING', {})
        # o vigia ainda alerta ALTA nessa conversa (o dedupe de 2h ignora o
        # veredito operacional da Lalamove)
        assert chatbot_vigia._alerta_alta_recente('5150') is False
        assert chatbot_vigia.alerta_lalamove_recente('5150') is True
        base = {'id': 5150, 'nome_contato': 'Caio', 'minutos_paradas': 15}
        hist = [{'role': 'user', 'content': 'Oi, vocês têm cesta de café?'}]
        with patch('app.services.chatbot_vigia._numero_destino',
                   return_value='5511999990000'), \
                patch('app.services.chatwoot.listar_conversas_paradas', return_value=[base]), \
                patch('app.services.chatwoot.buscar_historico', return_value=hist), \
                patch('app.services.chatwoot.enviar_mensagem') as contem, \
                patch('app.services.zapi.enviar_texto', return_value={'ok': True}) as alerta:
            chatbot_vigia.alertar_clientes_esperando_humano()
        contem.assert_not_called()
        alerta.assert_called_once()
        assert 'Lalamove' in alerta.call_args[0][1]


def test_motivo_do_payload_parse_liberal():
    from app.services.lalamove_alerta import MOTIVO_DESCONHECIDO, motivo_do_payload
    assert motivo_do_payload({'data': {'order': {'cancelReason': 'X'}}}) == 'X'
    assert motivo_do_payload({'data': {'order': {}, 'reason': {'message': 'Y'}}}) == 'Y'
    assert motivo_do_payload({'data': {'order': {'status': 'CANCELED'}}}) == MOTIVO_DESCONHECIDO
    assert motivo_do_payload(None) == MOTIVO_DESCONHECIDO


def test_expedicao_ignora_corrida_expirada_ou_recusada(app):
    """`_expedicao_com_pedido`: EXPIRED/REJECTED não são motoboy chamado."""
    from app.blueprints.main.routes import _expedicao_com_pedido
    with app.app_context():
        p = _pedido('LAL012', status='pago')
        _corrida('LAL012', 'ord-12', status='EXPIRED')
        assert _expedicao_com_pedido(p) is None
        _corrida('LAL012', 'ord-13', status='ON_GOING')
        assert 'Lalamove' in _expedicao_com_pedido(p)
