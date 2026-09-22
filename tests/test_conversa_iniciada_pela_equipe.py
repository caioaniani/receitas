"""Conversa INICIADA PELA EQUIPE ("Chamar cliente") em que o cliente nunca
escreveu não é conversa do bot nem espera de atendimento (22/09/2026).

Caso real, conv 2429: a equipe clicou "Chamar" (template) às 10:15; a
conversa nasceu na fila do bot (inbox com Agent Bot cria em `pending`), o
bot mandou follow-up às 10:25, o vigia deu "[ABANDONO 17min]" (ALTA +
WhatsApp) às 10:45 e a espera-humana cobrou "Urgente" no painel e no
WhatsApp a cada 15-20 min até o meio-dia — e quando a equipe abria a
conversa, "não tinha conversa nenhuma" (só os nossos templates).
"""
from datetime import timedelta
from unittest.mock import patch

from app.extensions import db
from app.models import EsperaAtendimento, VigiaVeredito
from app.utils import agora

_SO_NOSSAS = [
    # O template REAL saiu com `status='failed'` na Meta ("131026: Message
    # undeliverable" nas quatro tentativas — número que não recebe WhatsApp;
    # sonda /api/claude/atendimento-painel?conv=2429). `_mensagem_humana`
    # não conta mensagem falhada como humana, e foi por isso que o gate
    # `somente_bot` do follow-up não segurou o cutucão das 10:25.
    {'role': 'assistant', 'humano': False, 'entregue': False,
     'erro_canal': '131026: Message undeliverable',
     'content': 'Olá Cintia, aqui é da O Pão. Ficamos com uma dúvida sobre o seu pedido E3862E49.'},
    {'role': 'assistant', 'humano': False,
     'content': 'Oi, Cintia! Ainda ficou aquela dúvida sobre o pedido, você consegue nos ajudar?'},
]


def _msg(id_, tipo, content, **extra):
    """Mensagem CRUA da API do Chatwoot (listagem /messages)."""
    m = {'id': id_, 'message_type': tipo, 'content': content, 'private': False,
         'created_at': 1790082000 + id_, 'status': 'sent', 'attachments': []}
    if tipo == 'outgoing':
        m['sender'] = {'type': 'user', 'name': 'Micaela'}
    m.update(extra)
    return m


def _seed_2429(estado='aguardando', resolvido_em=None):
    inicio = agora() - timedelta(minutes=78)
    db.session.add(EsperaAtendimento(
        conversa_id='2429', inicio_em=inicio, nome='Cintia Tuyama',
        mensagem='[ABANDONO 17min] ', grave=True, estado=estado,
        resolvido_em=resolvido_em))
    db.session.add(VigiaVeredito(
        criado_em=inicio, conv_id='2429', cliente='Cintia Tuyama',
        mensagem_cliente='[ABANDONO 17min] ', bot_acao=None, alerta=True,
        gravidade='alta', motivo_vigia='[17 min sem resposta] cliente sumiu'))
    db.session.commit()


def test_cliente_ja_falou_e_fonte_unica():
    from app.services.chatbot import cliente_ja_falou
    assert cliente_ja_falou(_SO_NOSSAS) is False
    assert cliente_ja_falou([]) is False
    assert cliente_ja_falou(None) is False
    assert cliente_ja_falou(_SO_NOSSAS + [{'role': 'user', 'content': 'Oi'}]) is True


def test_abandono_nao_avalia_conversa_sem_fala_do_cliente(app):
    from app.services import chatbot_vigia
    with app.app_context(), \
         patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'x'}), \
         patch('app.services.chatbot_vigia._chamar_modelo_abandono') as modelo, \
         patch('app.services.zapi.enviar_texto') as zapi:
        app.config['CHATBOT_VIGIA'] = '1'
        res = chatbot_vigia.avaliar_abandono(
            _SO_NOSSAS, conv_id=2429, nome_contato='Cintia Tuyama',
            minutos_sem_resposta=17)
    assert 'equipe' in res['pulou']
    modelo.assert_not_called()
    zapi.assert_not_called()
    with app.app_context():
        assert VigiaVeredito.query.filter(VigiaVeredito.conv_id == '2429').count() == 0


def test_followup_nao_cutuca_conversa_sem_fala_do_cliente(app):
    from app.services import chatbot
    with app.app_context(), \
         patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'x'}), \
         patch('app.services.chatwoot.listar_conversas_paradas',
               return_value=[{'id': 2429, 'nome_contato': 'Cintia Tuyama',
                              'minutos_paradas': 9}]), \
         patch('app.services.chatwoot.buscar_historico',
               return_value=list(_SO_NOSSAS[:1])), \
         patch('app.services.chatbot._followup_gerar_texto') as gerar, \
         patch('app.services.chatwoot.enviar_mensagem') as envia:
        r = chatbot.followup_conversas_paradas()
    assert r == {'avaliadas': 0, 'enviadas': 0}
    gerar.assert_not_called()
    envia.assert_not_called()


def test_preparar_conversa_sem_cliente_sai_do_painel_e_da_cobranca(app):
    from app.services import atendimento_pendente
    conversa = {'id': 2429, 'nome_contato': 'Cintia Tuyama', 'minutos_paradas': 78,
                'status': 'open', 'telefone': '5514999999999'}
    with app.app_context():
        _seed_2429()
        # Antes: a linha aguardando + ALTA de abandono aparece no painel.
        assert [a for a in atendimento_pendente.alertas_painel() if a['conv_id'] == '2429']
        assert atendimento_pendente.preparar(conversa, list(_SO_NOSSAS)) is None
        row = db.session.get(EsperaAtendimento, '2429')
        assert row.estado == 'sem_cliente'
        assert row.resolvido_em is not None and row.proximo_aviso_em is None
        # Depois: some do painel (mesmo com o ALTA recente no banco)...
        assert not [a for a in atendimento_pendente.alertas_painel() if a['conv_id'] == '2429']
        # ...e da lista de candidatas da cobrança ao dono.
        with patch('app.services.chatwoot.listar_conversas_paradas', return_value=[]), \
             patch('app.services.chatwoot.consultar_conversa') as consulta:
            assert atendimento_pendente.candidatos() == []
            consulta.assert_not_called()


def test_preparar_cliente_que_responde_depois_volta_a_aguardar_sem_gravidade_fantasma(app):
    import time

    from app.services import atendimento_pendente
    conversa = {'id': 2429, 'nome_contato': 'Cintia Tuyama', 'minutos_paradas': 20}
    with app.app_context():
        # Marcada `sem_cliente` num ciclo anterior (10 min atrás); segue assim
        # enquanto só houver mensagens nossas.
        _seed_2429(estado='sem_cliente', resolvido_em=agora() - timedelta(minutes=10))
        assert atendimento_pendente.preparar(conversa, list(_SO_NOSSAS)) is None
        assert db.session.get(EsperaAtendimento, '2429').estado == 'sem_cliente'
        # A cliente respondeu há 30 s: volta a ser espera normal.
        historico = list(_SO_NOSSAS) + [{'role': 'user', 'content': 'Oi, pode entregar de novo?',
                                         'humano': False, 'created_at': time.time() - 30}]
        row = atendimento_pendente.preparar(conversa, historico, min_minutos=0)
        assert row is not None and row.estado == 'aguardando'
        # O ALTA de "abandono" da fase sem cliente não vira "Urgente" agora.
        assert row.grave is False
        assert row.inicio_em > row.resolvido_em


def test_resposta_da_equipe_no_painel_nao_reabre_sem_cliente(app):
    from app.services import atendimento_pendente
    conversa = {'id': 2429, 'nome_contato': 'Cintia Tuyama', 'minutos_paradas': 78}
    with app.app_context():
        _seed_2429()
        atendimento_pendente.preparar(conversa, list(_SO_NOSSAS))
        # Alguém digita "ola" na conversa pelo painel (caso real, 12:04).
        atendimento_pendente.confirmar_acao_painel(
            '2429', 'responder', iniciado_em=agora(), usuario_id=1)
        assert db.session.get(EsperaAtendimento, '2429').estado == 'sem_cliente'
        assert not [a for a in atendimento_pendente.alertas_painel() if a['conv_id'] == '2429']
        # Resolver continua resolvendo.
        atendimento_pendente.confirmar_acao_painel(
            '2429', 'resolved', iniciado_em=agora(), usuario_id=1)
        assert db.session.get(EsperaAtendimento, '2429').estado == 'resolvido'


def test_chamar_cliente_tira_a_conversa_da_fila_do_bot(app):
    """A conversa criada pelo "Chamar" nasce `pending` (inbox com Agent Bot);
    o serviço a passa para `open` logo após o template — a resposta do
    cliente cai na fila da equipe, não no bot."""
    from test_chamar_cliente_whatsapp import _cfg_whatsapp, _resp

    from app.services import chatwoot
    with app.app_context():
        _cfg_whatsapp(app)

        def fake_get(url, **kw):
            if '/contacts/search' in url:
                return _resp(200, {'payload': []})
            if url.endswith('/conversations'):
                return _resp(200, {'payload': []})
            return _resp(404, {})

        def fake_post(url, json=None, **kw):
            if url.endswith('/contacts'):
                return _resp(200, {'payload': {'contact': {
                    'id': 55,
                    'contact_inboxes': [{'inbox': {'id': 7}, 'source_id': 'src-7'}],
                }}})
            if url.endswith('/conversations'):
                return _resp(200, {'id': 900})
            if '/messages' in url:
                return _resp(200, {'id': 1})
            return _resp(404, {})

        with patch.object(chatwoot.requests, 'get', side_effect=fake_get), \
                patch.object(chatwoot.requests, 'post', side_effect=fake_post), \
                patch('app.services.chatwoot.definir_status',
                      return_value={'ok': True}) as status:
            res = chatwoot.iniciar_conversa_whatsapp(
                '11999998888', 'Simone', params=['Simone', 'ABC123'])
        assert res['ok'] is True and res['aberta'] is True
        assert status.call_args.args[:2] == (900, 'open')
        # Falha no toggle não derruba o envio (o template já saiu).
        with patch.object(chatwoot.requests, 'get', side_effect=fake_get), \
                patch.object(chatwoot.requests, 'post', side_effect=fake_post), \
                patch('app.services.chatwoot.definir_status',
                      return_value={'ok': False, 'erro': 'HTTP 500'}):
            res = chatwoot.iniciar_conversa_whatsapp(
                '11999998888', 'Simone', params=['Simone', 'ABC123'])
        assert res['ok'] is True and res['aberta'] is False


# ── Revisão 22/09/2026 (2ª rodada) ───────────────────────────────────────

def test_resposta_curta_ao_template_nao_ressuscita_a_cobranca_grave(app):
    """`sem_cliente` preservava `grave=True` e a "[ABANDONO 17min]": um
    "Sim"/"Ok"/"Obrigada" ao template caía no ramo de fechamento com a
    gravidade fantasma e virava "Caso grave ainda aberto" no WhatsApp do
    dono a cada 15 min (reproduzido pelo revisor)."""
    import time

    from app.services import atendimento_pendente, chatbot_vigia
    conversa = {'id': 2429, 'nome_contato': 'Cintia Tuyama', 'minutos_paradas': 78,
                'status': 'open', 'telefone': '5514999999999'}
    with app.app_context():
        _seed_2429()
        assert atendimento_pendente.preparar(conversa, list(_SO_NOSSAS)) is None
        row = db.session.get(EsperaAtendimento, '2429')
        assert (row.estado, row.grave, row.mensagem) == ('sem_cliente', False, '')
        for resposta in ('Sim', 'Ok', 'Obrigada'):
            historico = list(_SO_NOSSAS) + [{'role': 'user', 'content': resposta, 'humano': False,
                                             'created_at': time.time() - 30}]
            atendimento_pendente.preparar(conversa, historico, min_minutos=0)
            row = db.session.get(EsperaAtendimento, '2429')
            assert row.grave is False and '[ABANDONO' not in (row.mensagem or ''), resposta
            assert row.estado != 'em_atendimento', resposta
            with patch('app.services.chatbot_vigia._numero_destino', return_value='5511999990000'), \
                 patch('app.services.chatwoot.listar_conversas_paradas', return_value=[conversa]), \
                 patch('app.services.chatwoot.buscar_historico', return_value=historico), \
                 patch('app.services.zapi.enviar_texto', return_value={'ok': True}) as zapi:
                chatbot_vigia.alertar_clientes_esperando_humano(min_minutos=0)
            for chamada in zapi.call_args_list:
                texto = chamada.args[1]
                assert 'Caso grave' not in texto and '[ABANDONO' not in texto, (resposta, texto)


def test_fala_do_cliente_no_segundo_da_marcacao_reabre_e_aparece_no_painel(app):
    """Mensagem criada entre o GET do histórico e o `agora()` da marcação
    ficava com `inicio <= resolvido_em` e presa para sempre atrás do guard.
    Em `sem_cliente` toda fala do cliente é nova por construção."""
    from app.services import atendimento_pendente
    from app.utils import BRT
    conversa = {'id': 2429, 'nome_contato': 'Cintia Tuyama', 'minutos_paradas': 11}
    with app.app_context():
        fala_em = agora() - timedelta(minutes=11)
        _seed_2429(estado='sem_cliente', resolvido_em=fala_em + timedelta(seconds=1))
        historico = list(_SO_NOSSAS) + [{'role': 'user', 'content': 'Oi, pode entregar de novo?',
                                         'humano': False,
                                         'created_at': fala_em.replace(tzinfo=BRT).timestamp()}]
        row = atendimento_pendente.preparar(conversa, historico)
        assert row is not None and row.estado == 'aguardando' and row.grave is False
        assert row.inicio_em > row.resolvido_em
        assert [a for a in atendimento_pendente.alertas_painel() if a['conv_id'] == '2429']


def test_chamar_cliente_manda_toggle_status_open_com_o_token_do_bot(app, caplog):
    """O `/toggle_status` REAL (token do Agent Bot, corpo {'status': 'open'});
    falha do toggle é WARNING, não ERROR (best-effort — política de ruído do
    Sentry, 17/08/2026)."""
    import logging

    from test_chamar_cliente_whatsapp import _cfg_whatsapp, _resp

    from app.services import chatwoot
    with app.app_context():
        _cfg_whatsapp(app)
        app.config['CHATWOOT_BOT_TOKEN'] = 'tok-bot'
        toggles = []
        codigo_toggle = {'status': 200}

        def fake_get(url, **kw):
            if '/contacts/search' in url:
                return _resp(200, {'payload': []})
            if url.endswith('/conversations'):
                return _resp(200, {'payload': []})
            return _resp(404, {})

        def fake_post(url, json=None, headers=None, **kw):
            if url.endswith('/toggle_status'):
                toggles.append((url, json, headers))
                return _resp(codigo_toggle['status'], {'payload': {'success': True}})
            if url.endswith('/contacts'):
                return _resp(200, {'payload': {'contact': {
                    'id': 55,
                    'contact_inboxes': [{'inbox': {'id': 7}, 'source_id': 'src-7'}],
                }}})
            if url.endswith('/conversations'):
                return _resp(200, {'id': 900})
            if '/messages' in url:
                return _resp(200, {'id': 1})
            return _resp(404, {})

        with patch.object(chatwoot.requests, 'get', side_effect=fake_get), \
                patch.object(chatwoot.requests, 'post', side_effect=fake_post):
            res = chatwoot.iniciar_conversa_whatsapp(
                '11999998888', 'Simone', params=['Simone', 'ABC123'])
        assert res['ok'] is True and res['aberta'] is True
        assert len(toggles) == 1
        url, corpo, headers = toggles[0]
        assert url.endswith('/conversations/900/toggle_status')
        assert corpo == {'status': 'open'}
        assert headers['api_access_token'] == 'tok-bot'

        codigo_toggle['status'] = 500
        with caplog.at_level(logging.WARNING), \
                patch.object(chatwoot.requests, 'get', side_effect=fake_get), \
                patch.object(chatwoot.requests, 'post', side_effect=fake_post):
            res = chatwoot.iniciar_conversa_whatsapp(
                '11999998888', 'Simone', params=['Simone', 'ABC123'])
        assert res['ok'] is True and res['aberta'] is False
        do_chatwoot = [r for r in caplog.records if r.name == 'app.services.chatwoot']
        assert any('definir_status' in r.getMessage() for r in do_chatwoot)
        assert all(r.levelno < logging.ERROR for r in do_chatwoot)


def test_buscar_historico_pagina_ate_a_fala_do_cliente(app):
    """A paginação parava numa resposta HUMANA: com 20 mensagens nossas na
    primeira página, a fala do cliente (anterior) ficava fora e `preparar`
    marcaria `sem_cliente` uma espera legítima em acompanhamento."""
    from test_chamar_cliente_whatsapp import _cfg_whatsapp, _resp

    from app.services import chatwoot
    from app.services.chatbot import cliente_ja_falou
    pagina1 = [_msg(30 + i, 'outgoing', f'resposta {i}') for i in range(20)]
    pagina2 = [_msg(10, 'incoming', 'quero uma cesta'), _msg(11, 'outgoing', 'claro!')]
    chamadas = []

    def fake_get(url, headers=None, params=None, **kw):
        chamadas.append(params)
        if params and params.get('before') == 30:
            return _resp(200, {'payload': pagina2})
        return _resp(200, {'payload': [] if params else pagina1})

    with app.app_context():
        _cfg_whatsapp(app)
        with patch.object(chatwoot.requests, 'get', side_effect=fake_get):
            hist = chatwoot.buscar_historico(2431, incluir_autoria=True)
        assert cliente_ja_falou(hist) and hist[0]['content'] == 'quero uma cesta'
        assert chamadas == [None, {'before': 30}]
        # `somente_bot`: humano na primeira página basta (devolve [] de
        # qualquer jeito) — não gasta a segunda página.
        chamadas.clear()
        with patch.object(chatwoot.requests, 'get', side_effect=fake_get):
            assert chatwoot.buscar_historico(2431, incluir_autoria=True, somente_bot=True) == []
        assert chamadas == [None]
        # Conversa iniciada pela equipe: chega ao INÍCIO (página anterior
        # vazia) e só então conclui que o cliente nunca falou.
        chamadas.clear()

        def fake_get_equipe(url, headers=None, params=None, **kw):
            chamadas.append(params)
            return _resp(200, {'payload': [] if params else pagina1[:2]})

        with patch.object(chatwoot.requests, 'get', side_effect=fake_get_equipe):
            hist = chatwoot.buscar_historico(2429, incluir_autoria=True)
        assert not cliente_ja_falou(hist) and len(hist) == 2
        assert chamadas == [None, {'before': 30}]


def test_buscar_historico_anota_mensagem_recusada_pela_meta(app):
    """Bolha "⚠ Não entregue" do painel: a Meta recusou o template
    (status failed + external_error) e a equipe precisa ver isso na thread
    em vez de clicar "Chamar" quatro vezes. Mesma regra da sonda
    `erros_de_envio` (`_erro_de_entrega`)."""
    from test_chamar_cliente_whatsapp import _cfg_whatsapp, _resp

    from app.services import chatwoot
    msgs = [_msg(1, 'incoming', 'oi'),
            _msg(2, 'outgoing', 'template', status='failed',
                 content_attributes={'external_error': '131026: Message undeliverable'}),
            _msg(3, 'outgoing', 'chegou')]
    with app.app_context():
        _cfg_whatsapp(app)
        with patch.object(chatwoot.requests, 'get', return_value=_resp(200, {'payload': msgs})):
            hist = chatwoot.buscar_historico(2429)
            erros = chatwoot.erros_de_envio(2429)
    assert [m.get('entregue') for m in hist] == [None, False, None]
    assert hist[1]['erro_canal'].startswith('131026')
    assert erros['qtd_falhas'] == 1 and '131026' in erros['falhas'][0]['erro_canal']


def test_cron_abandono_conversa_pulada_nao_gasta_a_vaga_do_ciclo(app):
    """O early-return do abandono contava em `avaliadas`: cinco conversas
    iniciadas pela equipe esgotavam o teto do ciclo (5) e a sexta, um
    abandono real, ficava sem avaliação até o próximo ciclo."""
    from app.services import chatbot_vigia, seru_cron
    paradas = [{'id': 3000 + i, 'nome_contato': f'Equipe {i}', 'minutos_paradas': 20}
               for i in range(5)]
    paradas.append({'id': 3999, 'nome_contato': 'Cliente real', 'minutos_paradas': 20})
    real = [{'role': 'user', 'content': 'quero uma cesta'},
            {'role': 'assistant', 'content': 'temos! qual sabor?'}]

    def fake_hist(conv_id, **kw):
        return real if conv_id == 3999 else list(_SO_NOSSAS)

    with app.app_context(), \
         patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'x'}), \
         patch.object(chatbot_vigia, '_avisados_abandono', set()), \
         patch('app.services.chatwoot.disponivel', return_value=True), \
         patch('app.services.chatwoot.bot_disponivel', return_value=True), \
         patch('app.services.chatwoot.listar_conversas_paradas', return_value=paradas), \
         patch('app.services.chatwoot.buscar_historico', side_effect=fake_hist), \
         patch('app.services.chatbot_vigia.ja_avisado_abandono', return_value=False), \
         patch('app.services.chatbot_vigia.alertar_clientes_esperando_humano',
               return_value={}), \
         patch('app.services.chatbot_vigia._chamar_modelo_abandono',
               return_value={'alerta': False, 'gravidade': 'baixa'}) as modelo, \
         patch('app.services.zapi.enviar_texto'):
        app.config['CHATBOT_VIGIA'] = '1'
        app.config['CHATBOT_VIGIA_ABANDONO_MAX_POR_CICLO'] = 5
        seru_cron._run_vigia_abandono(app)
    modelo.assert_called_once()
    assert 'Cliente real' in modelo.call_args.args[1]
