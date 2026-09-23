"""Item 10 (spec do dono, caso E3862E49, 22/09/2026): o MESMO template de
WhatsApp para o MESMO destino sobre o MESMO pedido/assunto não sai de novo
em 60 min — a rota devolve 409 dizendo que já foi enviado e quando, com a
conversa para abrir. Chatwoot sempre mockado.
"""
from datetime import timedelta
from unittest.mock import patch

import pytest

from app.extensions import db


def _login(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def _pedido(codigo='E3862E49', telefone='11999998888'):
    from app.models import PedidoOnline
    p = PedidoOnline(codigo=codigo, status='pago', nome_cliente='Cintia',
                     telefone_cliente=telefone, email_cliente=f'{codigo.lower()}@x.com',
                     modo_entrega='agendada', subtotal=100, frete_valor=0,
                     valor_total=100)
    db.session.add(p)
    db.session.commit()
    return p


def _corrida(codigo='PED42', fone='11988887777'):
    from app.models import LalamoveEntrega
    e = LalamoveEntrega(pedido_code=codigo, order_id=f'ord-{codigo}', status='ON_GOING',
                        motorista_nome='Carlos', motorista_telefone=fone)
    db.session.add(e)
    db.session.commit()
    return e


_OK = {'ok': True, 'conversation_id': 2429, 'nova': True, 'erro': None, 'aberta': True}


@pytest.fixture
def cfg(app):
    app.config['CHATWOOT_WHATSAPP_TEMPLATE'] = 'duvida_pedido'
    return app


def test_segundo_clique_no_mesmo_pedido_e_bloqueado_com_quando(cfg, admin_user):
    from app.models import TemplateWhatsappEnvio
    with cfg.app_context():
        _pedido()
    c = cfg.test_client()
    _login(c, admin_user)
    with patch('app.services.chatwoot.iniciar_conversa_whatsapp', return_value=dict(_OK)) as m:
        r1 = c.post('/entregas/api/atendimento/chamar-cliente', json={'codigo': 'E3862E49'})
        r2 = c.post('/entregas/api/atendimento/chamar-cliente', json={'codigo': 'E3862E49'})
    assert r1.status_code == 200 and r1.get_json()['ok'] is True
    assert r2.status_code == 409
    d = r2.get_json()
    assert d['ok'] is False and d['ja_enviado'] is True
    assert 'já enviado às' in d['erro'] and 'E3862E49' in d['erro']
    assert d['ja_enviado_em'] and d['ha_minutos'] == 0
    assert d['conversation_id'] == 2429         # o painel abre a conversa existente
    assert d['nome'] == 'Cintia'
    m.assert_called_once()                       # o template saiu UMA vez
    with cfg.app_context():
        row = TemplateWhatsappEnvio.query.one()
        assert row.destino_chave == '1199998888'  # chave canonica (DDD + 8)
        assert row.template == 'duvida_pedido' and row.referencia == 'E3862E49'
        assert row.conversation_id == 2429 and row.usuario_id == admin_user.id


def test_mesmo_numero_em_formato_diferente_e_o_mesmo_destino(cfg, admin_user):
    """'+55 (11) 99999-8888' e '11999998888' colapsam na mesma chave."""
    with cfg.app_context():
        _pedido('AAA111', telefone='+55 (11) 99999-8888')
    c = cfg.test_client()
    _login(c, admin_user)
    with patch('app.services.chatwoot.iniciar_conversa_whatsapp', return_value=dict(_OK)):
        c.post('/entregas/api/atendimento/chamar-cliente', json={'codigo': 'AAA111'})
    with cfg.app_context():
        from app.services import template_dedupe
        assert template_dedupe.envio_recente('11999998888', 'duvida_pedido', 'aaa111') is not None
        assert template_dedupe.envio_recente('11999998888', 'duvida_pedido', 'BBB222') is None
        assert template_dedupe.envio_recente('11999997777', 'duvida_pedido', 'AAA111') is None
        assert template_dedupe.envio_recente('11999998888', 'outro_template', 'AAA111') is None


def test_depois_de_60_min_pode_de_novo(cfg, admin_user):
    from app.models import TemplateWhatsappEnvio
    from app.utils import agora
    with cfg.app_context():
        _pedido()
    c = cfg.test_client()
    _login(c, admin_user)
    with patch('app.services.chatwoot.iniciar_conversa_whatsapp', return_value=dict(_OK)):
        c.post('/entregas/api/atendimento/chamar-cliente', json={'codigo': 'E3862E49'})
        with cfg.app_context():
            row = TemplateWhatsappEnvio.query.one()
            row.criado_em = agora() - timedelta(minutes=61)
            db.session.commit()
        r = c.post('/entregas/api/atendimento/chamar-cliente', json={'codigo': 'E3862E49'})
    assert r.status_code == 200 and r.get_json()['ok'] is True
    with cfg.app_context():
        assert TemplateWhatsappEnvio.query.count() == 2


def test_envio_recusado_nao_conta_e_permite_tentar_de_novo(cfg, admin_user):
    from app.models import TemplateWhatsappEnvio
    with cfg.app_context():
        _pedido()
    c = cfg.test_client()
    _login(c, admin_user)
    falha = {'ok': False, 'conversation_id': 2429, 'nova': True, 'erro': 'HTTP 422: template'}
    with patch('app.services.chatwoot.iniciar_conversa_whatsapp', return_value=falha):
        r1 = c.post('/entregas/api/atendimento/chamar-cliente', json={'codigo': 'E3862E49'})
    assert r1.status_code == 502
    with cfg.app_context():
        assert TemplateWhatsappEnvio.query.count() == 0
    with patch('app.services.chatwoot.iniciar_conversa_whatsapp', return_value=dict(_OK)):
        r2 = c.post('/entregas/api/atendimento/chamar-cliente', json={'codigo': 'E3862E49'})
    assert r2.status_code == 200


def test_chamar_telefone_dedupe_por_assunto(cfg, admin_user):
    c = cfg.test_client()
    _login(c, admin_user)
    with patch('app.services.chatwoot.iniciar_conversa_whatsapp', return_value=dict(_OK)) as m:
        r1 = c.post('/entregas/api/atendimento/chamar-telefone',
                    json={'telefone': '11999998888', 'nome': 'Ana', 'sobre': 'Cesta dia dos pais'})
        r2 = c.post('/entregas/api/atendimento/chamar-telefone',
                    json={'telefone': '11999998888', 'nome': 'Ana', 'sobre': 'cesta DIA dos pais'})
        r3 = c.post('/entregas/api/atendimento/chamar-telefone',
                    json={'telefone': '11999998888', 'nome': 'Ana', 'sobre': 'Bolo de aniversário'})
    assert r1.status_code == 200
    assert r2.status_code == 409 and r2.get_json()['ja_enviado'] is True   # mesmo assunto, sem caixa
    assert r3.status_code == 200                                            # assunto novo: sai
    assert m.call_count == 2


def test_chamar_motorista_dedupe_por_pedido_e_template_efetivo(cfg, admin_user):
    from app.models import TemplateWhatsappEnvio
    with cfg.app_context():
        e = _corrida()
        eid = e.id
    c = cfg.test_client()
    _login(c, admin_user)
    ok = dict(_OK, conversation_id=901)
    with patch('app.services.chatwoot.iniciar_conversa_whatsapp', return_value=ok) as m:
        r1 = c.post('/entregas/api/atendimento/chamar-motorista', json={'entrega_id': eid})
        r2 = c.post('/entregas/api/atendimento/chamar-motorista', json={'entrega_id': eid})
    assert r1.status_code == 200
    assert r2.status_code == 409
    assert r2.get_json()['nome'] == 'Motoboy Carlos'
    assert r2.get_json()['conversation_id'] == 901
    m.assert_called_once()
    with cfg.app_context():
        row = TemplateWhatsappEnvio.query.one()
        assert row.referencia == 'PED42' and row.destino_chave == '1198888777'
        # Template dedicado do motoboy e OUTRO template: nao colide com o padrao
        cfg.config['CHATWOOT_WHATSAPP_TEMPLATE_MOTOBOY'] = 'motoboy_chegando'
        cfg.config['CHATWOOT_WHATSAPP_TEMPLATE_MOTOBOY_CORPO'] = 'Oi {{1}}, pedido {{2}}'
    with patch('app.services.chatwoot.iniciar_conversa_whatsapp', return_value=ok) as m2:
        r3 = c.post('/entregas/api/atendimento/chamar-motorista', json={'entrega_id': eid})
    assert r3.status_code == 200
    m2.assert_called_once()


def test_registrar_e_best_effort_e_retencao_poda(app):
    """Falha ao registrar nunca levanta; a linha entra na retencao de
    eventos (idempotencia), nao e dado de negocio."""
    from app.models import TemplateWhatsappEnvio
    from app.services import retencao, template_dedupe
    from app.utils import agora
    with app.app_context():
        assert template_dedupe.registrar('', 'tpl', 'X') is None
        assert template_dedupe.registrar('11999998888', 'tpl', '') is None
        with patch.object(db.session, 'commit', side_effect=RuntimeError('boom')):
            assert template_dedupe.registrar('11999998888', 'tpl', 'X') is None
        db.session.rollback()
        row = template_dedupe.registrar('11999998888', 'tpl', 'X', conversation_id='12')
        assert row is not None and row.conversation_id == 12
        row.criado_em = agora() - timedelta(days=app.config['RETENCAO_EVENTOS_DIAS'] + 1)
        db.session.commit()
        rel = retencao.limpar(dry_run=False)
        assert rel['template_whatsapp_envio'] == 1
        assert TemplateWhatsappEnvio.query.count() == 0
