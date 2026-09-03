"""Bot de atendimento (Agent Bot do Chatwoot) — Fase 1.

Cobre o cerebro (chatbot.responder, com Claude mockado) e o webhook
(filtros anti-loop + handoff), com chatbot/chatwoot mockados.
"""
from types import SimpleNamespace
from unittest.mock import patch


def _resp_texto(texto):
    return SimpleNamespace(content=[SimpleNamespace(type='text', text=texto)])


def _resp_tool(mensagem_cliente, motivo='quer humano'):
    blk = SimpleNamespace(type='tool_use', name='transferir_para_humano',
                          input={'mensagem_cliente': mensagem_cliente, 'motivo': motivo})
    return SimpleNamespace(content=[blk])


class _SyncThread:
    """Thread fake que roda o target na hora — pro webhook assíncrono virar
    síncrono no teste."""

    def __init__(self, target=None, daemon=None, **kw):
        self._target = target

    def start(self):
        if self._target:
            self._target()


# ── cérebro ──

def test_responder_texto(app):
    from app.services import chatbot
    with app.app_context():
        app.config['ANTHROPIC_API_KEY'] = 'test'
        with patch('anthropic.Anthropic') as M:
            M.return_value.messages.create.return_value = _resp_texto('Olá! Bem-vindo à O Pão. 🥖')
            r = chatbot.responder([{'role': 'user', 'content': 'oi'}])
    assert r['acao'] == 'responder'
    assert 'Pão' in r['texto']


def test_responder_handoff_via_tool(app):
    """Caminho do Claude chamando a tool transferir_para_humano. Usa uma
    mensagem que NAO casa o detector deterministico de 'quero humano' (senao
    o handoff sairia antes de chamar o Claude) — aqui o handoff vem da
    DECISAO do modelo, nao do detector."""
    from app.services import chatbot
    with app.app_context():
        app.config['ANTHROPIC_API_KEY'] = 'test'
        with patch('anthropic.Anthropic') as M:
            M.return_value.messages.create.return_value = _resp_tool('Já te passo pra um atendente!')
            r = chatbot.responder([{'role': 'user',
                                    'content': 'esse pão é sem glúten?'}])
    assert r['acao'] == 'handoff'
    assert 'atendente' in r['texto'].lower()
    assert r['motivo']


def test_responder_detector_quer_humano_forca_handoff(app):
    """Pedido explicito de humano -> handoff deterministico ANTES do Claude.
    Regressao 23/06/2026: o bot ESCREVEU 'vou te conectar' mas NAO chamou a
    tool, conversa ficou presa em 'pending' e o follow-up cutucou o cliente.
    Agora o detector forca o handoff sem depender do Claude."""
    from app.services import chatbot
    with app.app_context():
        app.config['ANTHROPIC_API_KEY'] = 'test'
        # Claude NEM e chamado — se fosse, o mock estouraria (sem return_value).
        with patch('anthropic.Anthropic') as M:
            r = chatbot.responder([{'role': 'user',
                                    'content': 'quero falar com um atendente'}])
        M.return_value.messages.create.assert_not_called()
    assert r['acao'] == 'handoff'
    assert r['motivo'] == 'cliente pediu atendente'


def test_responder_detector_variacoes_quer_humano(app):
    """Variacoes que DEVEM disparar e negacoes que NAO devem."""
    from app.services import chatbot
    assert chatbot._quer_humano('quero falar com uma pessoa')
    assert chatbot._quer_humano('me passa pra um atendente')
    assert chatbot._quer_humano('chama um humano por favor')
    assert chatbot._quer_humano('quero um atendente humano')
    assert chatbot._quer_humano('pode me transferir pra alguém?')
    # Negacao: NAO dispara (deixa o Claude tratar a nuance)
    assert not chatbot._quer_humano('não quero falar com atendente, me ajuda')
    # Mencao de passagem: NAO dispara
    assert not chatbot._quer_humano('o atendente de ontem foi ótimo')
    assert not chatbot._quer_humano('vocês têm atendimento aos domingos?')


def test_troca_de_cesta_vai_direto_para_avaliacao_humana(app):
    """Caso 2059: o bot nao pode oferecer, aceitar ou confirmar a troca."""
    from app.services import chatbot
    historico = [
        {'role': 'user', 'content':
         'Pode trocar a cesta Sweet Coffee pela Caixa Mimo no meu pedido?'},
    ]
    with app.app_context(), patch('anthropic.Anthropic') as modelo:
        r = chatbot.responder(historico)
    modelo.return_value.messages.create.assert_not_called()
    assert r['acao'] == 'handoff'
    assert 'Não consigo oferecer, aceitar ou confirmar trocas' in r['texto']
    assert 'equipe avaliar' in r['texto']
    assert 'fazer a troca' not in r['texto'].lower()


def test_troca_expressa_com_no_lugar_tambem_e_bloqueada():
    from app.services import chatbot
    assert chatbot._solicita_troca([
        {'role': 'user', 'content': 'Quero a Caixa Mimo no lugar da Sweet Coffee'},
    ])
    assert not chatbot._solicita_troca([
        {'role': 'user', 'content': 'Preciso trocar minha senha'},
    ])


def test_responder_sem_api_key_faz_handoff(app):
    from app.services import chatbot
    with app.app_context():
        app.config['ANTHROPIC_API_KEY'] = ''
        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': ''}):
            r = chatbot.responder([{'role': 'user', 'content': 'oi'}])
    assert r['acao'] == 'handoff'


# ── webhook ──

def _post(client, **payload_over):
    payload = {'event': 'message_created', 'message_type': 'incoming',
               'conversation': {'id': 7, 'status': 'pending'}, 'content': 'oi'}
    payload.update(payload_over)
    return client.post('/crm/bot?k=seg', json=payload)


def test_bot_webhook_responde(app):
    app.config['CHATWOOT_BOT_SECRET'] = 'seg'
    client = app.test_client()
    with patch('threading.Thread', _SyncThread), \
         patch('app.services.chatwoot.buscar_historico',
               return_value=[{'role': 'user', 'content': 'oi'}]), \
         patch('app.services.chatbot.responder',
               return_value={'acao': 'responder', 'texto': 'Olá!'}), \
         patch('app.services.chatwoot.enviar_mensagem', return_value={'ok': True}) as env, \
         patch('app.services.chatwoot.definir_status', return_value={'ok': True}) as st:
        r = _post(client)
    assert r.status_code == 200
    assert r.get_json()['ok'] is True
    env.assert_called_once()
    st.assert_not_called()


def test_bot_webhook_handoff_muda_status(app):
    app.config['CHATWOOT_BOT_SECRET'] = 'seg'
    client = app.test_client()
    with patch('threading.Thread', _SyncThread), \
         patch('app.services.chatwoot.buscar_historico',
               return_value=[{'role': 'user', 'content': 'quero humano'}]), \
         patch('app.services.chatbot.responder',
               return_value={'acao': 'handoff', 'texto': 'Chamando atendente', 'motivo': 'x'}), \
         patch('app.services.chatwoot.enviar_mensagem', return_value={'ok': True}) as env, \
         patch('app.services.chatwoot.definir_status', return_value={'ok': True}) as st:
        r = _post(client, content='quero humano')
    assert r.status_code == 200
    assert r.get_json()['ok'] is True
    env.assert_called_once()
    st.assert_called_once_with(7, 'open')


def test_bot_webhook_nao_responde_de_novo_durante_handoff(app):
    """Segundo balão da conversa 2059 não gera nova fala nem novo modelo."""
    from app.services import chatbot
    app.config['CHATWOOT_BOT_SECRET'] = 'seg'
    client = app.test_client()
    with app.app_context():
        chatbot.salvar_historico(
            '7', [{'role': 'user', 'content': 'quero trocar a cesta'}],
            'Encaminhei para avaliação humana.', handoff=True)
    with patch('threading.Thread', _SyncThread), \
         patch('app.services.chatbot.responder') as resp, \
         patch('app.services.chatwoot.enviar_mensagem') as env, \
         patch('app.services.chatwoot.definir_status',
               return_value={'ok': True}) as st:
        r = _post(client, content='Mas vocês disseram que podia')
    assert r.status_code == 200
    resp.assert_not_called()
    env.assert_not_called()
    st.assert_called_once_with(7, 'open')


def test_bot_webhook_ignora_outgoing(app):
    """Mensagem do proprio bot/atendente nao dispara o bot (anti-loop)."""
    app.config['CHATWOOT_BOT_SECRET'] = 'seg'
    client = app.test_client()
    with patch('app.services.chatbot.responder') as resp:
        r = _post(client, message_type='outgoing')
    assert r.status_code == 200
    assert r.get_json()['ignorado'] == 'nao-incoming'
    resp.assert_not_called()


def test_bot_webhook_ignora_conversa_aberta(app):
    """Conversa ja 'open' (humano assumiu) — bot nao responde."""
    app.config['CHATWOOT_BOT_SECRET'] = 'seg'
    client = app.test_client()
    with patch('app.services.chatbot.responder') as resp:
        r = _post(client, conversation={'id': 7, 'status': 'open'})
    assert r.status_code == 200
    assert r.get_json()['ignorado'] == 'nao-pending'
    resp.assert_not_called()


def test_bot_webhook_token_invalido(app):
    app.config['CHATWOOT_BOT_SECRET'] = 'seg'
    client = app.test_client()
    r = client.post('/crm/bot?k=errado', json={'event': 'message_created'})
    assert r.status_code == 403


def test_vigia_registra_no_historico(app):
    """`avaliar` (wrapper) grava o resultado em `_historico` pra /admin/vigia/diag."""
    from app.services import chatbot_vigia
    veredicto = {'alerta': True, 'gravidade': 'alta',
                 'motivo': 'X', 'acao_sugerida': ''}
    with app.app_context():
        chatbot_vigia._historico.clear()
        app.config['CHATBOT_VIGIA'] = True
        app.config['ANTHROPIC_API_KEY'] = 'test'
        app.config['ZAPI_NUMERO_DESTINO'] = '5511999990000'
        with patch('app.services.chatbot_vigia._chamar_modelo',
                   return_value=veredicto), \
             patch('app.services.chatbot_vigia._resumo_catalogo_site',
                   return_value=''), \
             patch('app.services.zapi.enviar_texto',
                   return_value={'ok': True}):
            chatbot_vigia.avaliar([{'role': 'user', 'content': 'tem croissant?'}],
                                  conv_id=99, nome_contato='Maria')
        ultimos = chatbot_vigia.ultimos()
    assert len(ultimos) == 1
    assert ultimos[0]['conv_id'] == 99
    assert ultimos[0]['gravidade'] == 'alta'
    assert ultimos[0]['enviado'] is True
    assert 'croissant' in ultimos[0]['mensagem_cliente']


def test_vigia_disparar_teste_cenario_estoque(app):
    """Cenario sintetico 'estoque' chega ao Haiku + Z-API + grava historico."""
    from app.services import chatbot_vigia
    veredicto = {'alerta': True, 'gravidade': 'alta',
                 'motivo': 'TESTE: bot esgotado mas tem na loja',
                 'acao_sugerida': 'atualizar VNDA'}
    with app.app_context():
        chatbot_vigia._historico.clear()
        app.config['CHATBOT_VIGIA'] = True
        app.config['ANTHROPIC_API_KEY'] = 'test'
        app.config['ZAPI_NUMERO_DESTINO'] = '5511999990000'
        with patch('app.services.chatbot_vigia._chamar_modelo',
                   return_value=veredicto), \
             patch('app.services.chatbot_vigia._resumo_catalogo_site',
                   return_value=''), \
             patch('app.services.zapi.enviar_texto',
                   return_value={'ok': True}) as send:
            r = chatbot_vigia.disparar_teste('estoque')
    assert r['enviado'] is True
    send.assert_called_once()
    assert 'teste-estoque' in str(chatbot_vigia.ultimos()[0]['conv_id'])


def test_admin_vigia_diag_route_responde_json(app):
    from app.extensions import db
    from app.models import Usuario
    from app.services import chatbot_vigia
    with app.app_context():
        chatbot_vigia._historico.clear()
        app.config['CHATBOT_VIGIA'] = True
        app.config['ANTHROPIC_API_KEY'] = 'test'
        app.config['ZAPI_NUMERO_DESTINO'] = '5511999990000'
        owner = Usuario(nome='owner', login='owner', papel='admin', is_owner=True)
        owner.set_senha('123')
        db.session.add(owner)
        db.session.commit()
        owner_id = owner.id
    client = app.test_client()
    with client.session_transaction() as s:
        s['_user_id'] = str(owner_id)
    r = client.get('/admin/vigia/diag')
    assert r.status_code == 200
    j = r.get_json()
    assert j['ligado'] is True
    assert j['numero_destino'] == '5511999990000'
    assert j['anthropic_api_key_configurada'] is True
    assert j['ultimos_veredictos'] == []


# ── Fase 2: ferramentas ──

def test_responder_loop_consultar_produtos(app):
    """Claude chama consultar_produtos, recebe o resultado e responde."""
    from app.services import chatbot
    tool_blk = SimpleNamespace(type='tool_use', name='consultar_produtos',
                               id='t1', input={'busca': 'croissant'})
    resp1 = SimpleNamespace(content=[tool_blk])
    resp2 = _resp_texto('🥐 Croissant Almond — R$32,50')
    with app.app_context():
        app.config['ANTHROPIC_API_KEY'] = 'test'
        with patch('anthropic.Anthropic') as M, \
             patch('app.services.bot_tools.consultar_produtos',
                   return_value={'produtos': [{'nome': 'Croissant Almond', 'sku': '10007',
                                               'preco': 32.5, 'disponivel': True}]}) as cp:
            M.return_value.messages.create.side_effect = [resp1, resp2]
            r = chatbot.responder([{'role': 'user', 'content': 'tem croissant de amêndoas?'}])
    assert r['acao'] == 'responder'
    assert 'Croissant' in r['texto']
    cp.assert_called_once()


# ── Fase 6: NF do Tiny ──

def test_nf_link_quando_cpf_e_numero_batem_e_pedido_vnda(app):
    from app.extensions import db
    from app.models import NFLog
    from app.services import bot_tools
    pedido = {'id': '1', 'numero': 'DA999', 'origem': 'ecommerce',
              'nota_fiscal_id': '88', 'situacao': 'aprovado'}
    with app.app_context():
        app.config['TINY_API_TOKEN'] = 'xxx'
        app.config['ZAPI_NUMERO_DESTINO'] = '5511999990000'
        with patch('app.services.tiny.buscar_pedido_por_cpf_e_numero',
                   return_value=pedido), \
             patch('app.services.tiny.obter_link_nota_fiscal',
                   return_value='https://tiny.com.br/nf/88.pdf'), \
             patch('app.services.zapi.enviar_texto',
                   return_value={'ok': True}) as send:
            r = bot_tools.buscar_nota_fiscal('111.444.777-35', 'DA999',
                                              conv_id=1, canal='whatsapp')
        assert r.get('link') == 'https://tiny.com.br/nf/88.pdf'
        log = NFLog.query.order_by(NFLog.id.desc()).first()
        assert log.resultado == 'enviada'
        assert log.cpf_4ultimos == '7735'   # ultimos 4 do CPF
        # Aviso pro dono via Z-API: 1 mensagem, formato esperado
        send.assert_called_once()
        msg = send.call_args[0][1]
        assert 'NF solicitada' in msg
        assert 'DA999' in msg
        assert '7735' in msg  # 4 ultimos do CPF
        assert '111.444.777-35' not in msg  # CPF inteiro NUNCA vai no aviso
        db.session.remove()


def test_nf_aviso_dono_pode_ser_desligado(app):
    """CHATBOT_AVISAR_NF=0 desliga o aviso (cliente ainda recebe NF, mas dono
    não recebe ping)."""
    import os as _os

    from app.services import bot_tools
    pedido = {'id': '1', 'numero': 'DA999', 'origem': 'ecommerce',
              'nota_fiscal_id': '88'}
    with app.app_context():
        app.config['TINY_API_TOKEN'] = 'xxx'
        app.config['ZAPI_NUMERO_DESTINO'] = '5511999990000'
        prev = _os.environ.get('CHATBOT_AVISAR_NF')
        _os.environ['CHATBOT_AVISAR_NF'] = '0'
        try:
            with patch('app.services.tiny.buscar_pedido_por_cpf_e_numero',
                       return_value=pedido), \
                 patch('app.services.tiny.obter_link_nota_fiscal',
                       return_value='https://tiny.com.br/x.pdf'), \
                 patch('app.services.zapi.enviar_texto') as send:
                r = bot_tools.buscar_nota_fiscal('11144477735', 'DA999')
        finally:
            if prev is None:
                _os.environ.pop('CHATBOT_AVISAR_NF', None)
            else:
                _os.environ['CHATBOT_AVISAR_NF'] = prev
    assert r.get('link')  # cliente continua recebendo
    send.assert_not_called()  # dono nao recebe ping


def test_tiny_busca_por_cpf_e_acha_via_numero_ecommerce(app):
    """A v2 do Tiny IGNORA filtros de numero — so cpf_cnpj filtra. Por isso
    busca lista TODOS pedidos do cpf, e o match e feito no codigo no campo
    `numero_ecommerce`. Depois chama `pedido.obter.php` pra trazer nota_fiscal."""
    from app.services import tiny

    def _fake_get(endpoint, params=None):
        if endpoint == 'pedidos.pesquisa.php':
            return {'pedidos': [
                {'pedido': {'id': '111', 'numero': '11287',
                            'numero_ecommerce': 'AAA111', 'situacao': 'Entregue'}},
                {'pedido': {'id': '907266869', 'numero': '98720',
                            'numero_ecommerce': 'D884A21B9E', 'situacao': 'Entregue'}},
            ]}
        if endpoint == 'pedido.obter.php' and (params or {}).get('id') == '907266869':
            # Formato REAL da v2 do Tiny: id_nota_fiscal solto + ecommerce dict
            return {'pedido': {'id': '907266869', 'numero': '98720',
                                'numero_ecommerce': 'D884A21B9E',
                                'ecommerce': {'nomeEcommerce': 'Vnda Commerce'},
                                'id_nota_fiscal': '55'}}
        return {'pedidos': []}

    with app.app_context():
        app.config['TINY_API_TOKEN'] = 'xxx'
        with patch('app.services.tiny._get', side_effect=_fake_get):
            r = tiny.buscar_pedido_por_cpf_e_numero('087.271.904-98',
                                                     '#D884A21B9E')
    assert r['id'] == '907266869'
    assert r['nota_fiscal_id'] == '55'   # veio do pedido.obter.php (pesquisa nao traz)
    assert r['numero_ecommerce'] == 'D884A21B9E'


def test_nf_handoff_se_pedido_b2b(app):
    """Pedido fora do site (B2B/local) — bot NAO entrega NF, manda pro humano."""
    from app.services import bot_tools
    pedido = {'id': '2', 'numero': 'X1', 'origem': 'b2b',
              'nota_fiscal_id': '99'}
    with app.app_context():
        app.config['TINY_API_TOKEN'] = 'xxx'
        with patch('app.services.tiny.buscar_pedido_por_cpf_e_numero',
                   return_value=pedido):
            r = bot_tools.buscar_nota_fiscal('11144477735', 'X1')
    assert r['erro'] == 'fora_site'


def test_nf_avisa_quando_nf_ainda_nao_emitida(app):
    from app.services import bot_tools
    pedido = {'id': '3', 'numero': 'Y9', 'origem': 'ecommerce',
              'nota_fiscal_id': '', 'situacao': 'em_separacao'}
    with app.app_context():
        app.config['TINY_API_TOKEN'] = 'xxx'
        with patch('app.services.tiny.buscar_pedido_por_cpf_e_numero',
                   return_value=pedido):
            r = bot_tools.buscar_nota_fiscal('11144477735', 'Y9')
    assert r['erro'] == 'sem_nf_ainda'


def test_nf_nao_encontrado_nao_vaza_outro_cliente(app):
    """CPF + numero sem match: bot NÃO retorna nada — não vaza nem confirma."""
    from app.services import bot_tools
    with app.app_context():
        app.config['TINY_API_TOKEN'] = 'xxx'
        with patch('app.services.tiny.buscar_pedido_por_cpf_e_numero',
                   return_value=None):
            r = bot_tools.buscar_nota_fiscal('11144477735', 'INEXISTENTE')
    assert r['erro'] == 'nao_encontrado'


def test_nf_api_falhou_vira_handoff_nao_nao_encontrado(app):
    """Quando a API do Tiny falha, o bot NUNCA pode dizer 'não encontrei,
    confere os dados' — isso lava as mãos da falha e culpa o cliente.
    Tem que ser handoff explicito por instabilidade. Bug visto em prod
    2026-06-09 (pedido BF6390FBCD existia, Tiny deu glitch transiente)."""
    from app.services import bot_tools

    def _fake_buscar(cpf, numero, diag=None):
        if isinstance(diag, dict):
            diag['api_falhou_em_pagina'] = 1
            diag['paginas_lidas'] = 0
            diag['pedidos_vistos'] = 0
        return None

    with app.app_context():
        app.config['TINY_API_TOKEN'] = 'xxx'
        with patch('app.services.tiny.buscar_pedido_por_cpf_e_numero',
                   side_effect=_fake_buscar):
            r = bot_tools.buscar_nota_fiscal('11144477735', 'BF6390FBCD')
    assert r['erro'] == 'tiny_indisponivel'
    assert 'instabilidade' in r['mensagem'].lower() or 'atendente' in r['mensagem'].lower()
    # NUNCA cair em "nao encontrado" quando foi falha de API
    assert r['erro'] != 'nao_encontrado'


def test_nf_recusa_sem_cpf_ou_numero(app):
    """Bot NÃO consulta o Tiny se faltar CPF ou número (regra de segurança)."""
    from app.services import bot_tools
    with app.app_context():
        app.config['TINY_API_TOKEN'] = 'xxx'
        with patch('app.services.tiny.buscar_pedido_por_cpf_e_numero') as call:
            r = bot_tools.buscar_nota_fiscal('', 'DA999')
        assert r['erro'] == 'dados_incompletos'
        call.assert_not_called()


def test_nf_sem_token_passa_pro_humano(app):
    """Sem TINY_API_TOKEN setado: bot avisa indisponivel (não inventa)."""
    from app.services import bot_tools
    with app.app_context():
        app.config['TINY_API_TOKEN'] = ''
        r = bot_tools.buscar_nota_fiscal('11144477735', 'DA999')
    assert r['erro'] == 'tiny_indisponivel'


# ── Fase: data de entrega ──

def test_consultar_pedido_retorna_data_agendada(app):
    """consultar_pedido devolve a data AGENDADA (extra.DataDeEntrega), nunca o
    expected_delivery_date bugado do VNDA (o do "entregue hoje").

    Atualizado 14/06/2026: tool agora exige autorização (telefone do canal
    OU CPF). Aqui autorizo via telefone batendo pra focar no teste original
    (data agendada vence o expected_delivery_date)."""
    from app.services import bot_tools
    order = {
        'code': 'AB123', 'status': 'paid', 'total': 321.0,
        'items': [{'product_name': 'Family Box', 'quantity': 1}],
        'extra': {'DataDeEntrega': '08/06/2026', 'Periodo': '08-09h'},
        'expected_delivery_date': '2026-06-05',  # bug do VNDA: "hoje"
    }
    with app.app_context():
        with patch('app.services.vnda.buscar_pedido_completo', return_value=order), \
             patch('app.services.vnda.telefone_do_pedido', return_value='11999998888'):
            r = bot_tools.consultar_pedido(
                'AB123', telefone_contato='5511999998888')
    assert r['data_entrega'] == '08/06/2026'   # a agendada, não a bugada
    assert r['periodo'] == '08-09h'
    assert r['numero'] == 'AB123'


def test_consultar_pedido_nao_encontrado(app):
    from app.services import bot_tools
    with app.app_context():
        with patch('app.services.vnda.buscar_pedido_completo', return_value=None):
            r = bot_tools.consultar_pedido('XYZ')
    assert 'erro' in r


def _catalogo_loja(db):
    """Loja do site + 1 cesta (produto) e 1 pão (receita) publicados E
    estocados — o catálogo PRÓPRIO (opao.online) que o bot consulta agora."""
    from decimal import Decimal

    from conftest import _make_receita

    from app.models import AppConfig, EstoqueLoja, Loja, Produto
    loja = Loja(nome='Anesio', endereco='Anésio Pinto Rosa, 78', ativa=True)
    db.session.add(loja)
    db.session.commit()
    AppConfig.set('loja_site_estoque_id', loja.id)
    box = Produto(nome='Cesta Monamour', categoria='Cestas',
                  preco_site=Decimal('200'), ativo=True,
                  descricao='Contém: 2 croissants, 1 geleia, suco de laranja')
    cr = _make_receita('Croissant Almond', categoria='Viennoiserie')
    cr.preco_site = Decimal('32.50')
    db.session.add_all([box, cr])
    db.session.commit()
    db.session.add(EstoqueLoja(loja_id=loja.id, produto_id=box.id,
                               quantidade=20))
    db.session.add(EstoqueLoja(loja_id=loja.id, receita_id=cr.id,
                               quantidade=20))
    db.session.commit()
    return {'box': box.id, 'cr': cr.id}


def test_consultar_produtos_inclui_descricao(app):
    """Match focado no catálogo PRÓPRIO traz descrição + kind/id + url."""
    from app.extensions import db
    from app.services import bot_tools
    with app.app_context():
        _catalogo_loja(db)
        r = bot_tools.consultar_produtos('monamour')
    p = r['produtos'][0]
    assert p['nome'] == 'Cesta Monamour'
    assert 'croissants' in p['descricao']
    assert p['kind'] == 'produto'
    assert p['disponivel'] is True              # estoque REAL
    assert p['url'].startswith('https://opao.online/loja/')


def test_consultar_produtos_fallback_sem_descricao(app):
    """Sem match no nome, o catálogo amplo vem SEM descrição (token-light)."""
    from app.extensions import db
    from app.services import bot_tools
    with app.app_context():
        _catalogo_loja(db)
        r = bot_tools.consultar_produtos('zzznadacasa')
    assert r['produtos']                        # catálogo amplo
    p = r['produtos'][0]
    assert 'descricao' not in p                 # stripado no fallback
    assert 'kind' in p and 'id' in p


def test_vigia_media_nao_pinga_so_registra(app):
    """Gravidade media NÃO manda WhatsApp na hora (vai pro resumo); só registra."""
    from app.services import chatbot_vigia
    veredicto = {'alerta': True, 'gravidade': 'media',
                 'motivo': 'handoff evitável (conteúdo de cesta)', 'acao_sugerida': ''}
    with app.app_context():
        chatbot_vigia._historico.clear()
        app.config['CHATBOT_VIGIA'] = True
        app.config['ANTHROPIC_API_KEY'] = 'test'
        app.config['ZAPI_NUMERO_DESTINO'] = '5511999990000'
        with patch('app.services.chatbot_vigia._chamar_modelo', return_value=veredicto), \
             patch('app.services.chatbot_vigia._resumo_catalogo_site', return_value=''), \
             patch('app.services.zapi.enviar_texto') as send:
            r = chatbot_vigia.avaliar([{'role': 'user', 'content': 'o que tem na cesta?'}],
                                      conv_id=5)
    assert r['silencio'] is True
    send.assert_not_called()
    assert chatbot_vigia.ultimos()[0]['gravidade'] == 'media'   # fica pro resumo


def test_gerar_link_carrinho(app):
    from app.services import bot_tools
    with app.app_context():
        r = bot_tools.gerar_link_carrinho([
            {'kind': 'receita', 'id': 5, 'quantidade': 2},
            {'kind': 'produto', 'id': 83, 'quantidade': 1}])
    # link de 1 clique no opao.online (r=receita, p=produto)
    assert r['link'].endswith('/loja/carrinho?add=r5:2,p83:1')
    assert r['link'].startswith('https://opao.online')


def test_gerar_link_carrinho_vazio():
    from app.services import bot_tools
    assert 'erro' in bot_tools.gerar_link_carrinho([])


def test_gerar_link_carrinho_cesta_e_avulso_um_link(app):
    """Fluxo unificado: cesta + avulsos no MESMO link (acabou o 2 passos)."""
    from app.services import bot_tools
    with app.app_context():
        r = bot_tools.gerar_link_carrinho([
            {'kind': 'produto', 'id': 42, 'quantidade': 1},   # cesta
            {'kind': 'receita', 'id': 7, 'quantidade': 3}])    # avulso
    assert r['link'].endswith('/loja/carrinho?add=p42:1,r7:3')


def test_gerar_link_carrinho_ignora_invalido(app):
    """Item sem kind/id válido é descartado; sobra só o bom."""
    from app.services import bot_tools
    with app.app_context():
        r = bot_tools.gerar_link_carrinho([
            {'kind': 'xyz', 'id': 1, 'quantidade': 1},   # kind inválido
            {'kind': 'produto', 'quantidade': 1},        # sem id
            {'kind': 'produto', 'id': 9, 'quantidade': 2}])
    assert r['link'].endswith('/loja/carrinho?add=p9:2')


def test_consultar_produtos_esgotado_disponivel_false(app):
    """Disponibilidade do site = PLANO-DO-DIA (regra do dono 01/07/2026): item
    com plano 0 na janela vem disponivel=False. O EstoqueLoja fisico nao entra
    (item sem plano = fail-open, disponivel)."""
    from decimal import Decimal

    from app.extensions import db
    from app.models import AppConfig, EstoqueLoja, Loja, Produto
    from app.services import bot_tools, loja_plano_dia
    from app.utils import hoje
    with app.app_context():
        loja = Loja(nome='Anesio', endereco='x', ativa=True)
        db.session.add(loja)
        db.session.commit()
        AppConfig.set('loja_site_estoque_id', loja.id)
        com = Produto(nome='Family Box', categoria='Cestas',
                      preco_site=Decimal('437'), ativo=True)
        sem = Produto(nome='Caixa Especial', categoria='Cestas',
                      preco_site=Decimal('368'), ativo=True)
        db.session.add_all([com, sem])
        db.session.commit()
        db.session.add(EstoqueLoja(loja_id=loja.id, produto_id=com.id,
                                   quantidade=5))
        db.session.commit()
        # Caixa Especial esgotada via plano 0; Family Box sem plano = fail-open.
        loja_plano_dia.replicar_para_proximos_dias(
            'produto', sem.id, 0, data_inicio=hoje(), dias=14)
        r = bot_tools.consultar_produtos('caixa especial')
    p = next(x for x in r['produtos'] if x['nome'] == 'Caixa Especial')
    assert p['disponivel'] is False


# ── Fase 5: auditor proativo (agente ativo) ──

def test_auditor_dia_resumo_envia_mesmo_dia_tranquilo(app):
    """Modo 'resumo' (19h): traz insights e envia MESMO sem problemas."""
    from app.extensions import db
    from app.models import VigiaVeredito
    from app.services import chatbot_auditor
    with app.app_context():
        db.session.add(VigiaVeredito(conv_id='c1', mensagem_cliente='oi',
                                      bot_acao='responder', alerta=False))
        db.session.commit()
        app.config['ANTHROPIC_API_KEY'] = 'test'
        app.config['ZAPI_NUMERO_DESTINO'] = '5511999990000'
        rel = {'destaque': 'Tudo tranquilo hoje',
               'resumo_curto': '1 conversa, 0 handoffs',
               'insights': ['Pico de mensagens entre 12h e 14h'],
               'problemas': []}
        with patch('app.services.chatbot_auditor._chamar_sonnet',
                   return_value=rel), \
             patch('app.services.zapi.enviar_texto',
                   return_value={'ok': True}) as send:
            r = chatbot_auditor.auditar_dia_resumo(enviar=True)
    assert r['enviado'] is True   # mesmo sem problemas
    msg = send.call_args[0][1]
    assert 'Resumo do dia' in msg
    assert 'Insights' in msg
    assert 'Pico' in msg


def test_auditor_janela_avanca_ponteiro_e_evita_spam(app):
    """Rodando 5x/dia, cada execução SÓ olha a janela desde a anterior. Sem
    isso, mesmo problema seria reportado várias vezes."""
    from app.extensions import db
    from app.models import AppConfig
    from app.services import chatbot_auditor
    with app.app_context():
        app.config['ANTHROPIC_API_KEY'] = 'test'
        app.config['ZAPI_NUMERO_DESTINO'] = '5511999990000'
        # 1a execucao: sem ponteiro -> roda (mas sem dados -> 'pulou'), avanca.
        with patch('app.services.chatbot_auditor._chamar_sonnet') as sonnet, \
             patch('app.services.zapi.enviar_texto') as send:
            r1 = chatbot_auditor.auditar_janela_pendente(enviar=True)
        assert AppConfig.get(chatbot_auditor.CHAVE_ULTIMA_EXEC) is not None
        # 2a execucao IMEDIATA: janela < 1min -> pula sem chamar Sonnet/Z-API.
        with patch('app.services.chatbot_auditor._chamar_sonnet') as sonnet2, \
             patch('app.services.zapi.enviar_texto') as send2:
            r2 = chatbot_auditor.auditar_janela_pendente(enviar=True)
        assert 'pulou' in r2
        sonnet2.assert_not_called()
        send2.assert_not_called()
        db.session.remove()
        _ = r1, sonnet, send  # silencia ruff/lint


def test_auditor_coleta_e_pede_resumo(app):
    """Auditor agrega VigiaVeredito, manda pro Sonnet e formata o WhatsApp."""
    from datetime import timedelta

    from app.extensions import db
    from app.models import VigiaVeredito
    from app.services import chatbot_auditor
    from app.utils import agora as _agora

    with app.app_context():
        for i in range(3):
            db.session.add(VigiaVeredito(
                conv_id=f'c{i}', cliente=f'Cliente {i}',
                mensagem_cliente=f'o que tem na cesta X{i}?',
                bot_acao='handoff', bot_motivo='conteudo de cesta',
                alerta=True, gravidade='media',
                motivo_vigia='handoff evitavel — bot devia saber',
            ))
        db.session.commit()
        app.config['ANTHROPIC_API_KEY'] = 'test'
        app.config['ZAPI_NUMERO_DESTINO'] = '5511999990000'
        relatorio = {
            'tem_problemas': True,
            'destaque': '3 handoffs evitáveis sobre cesta',
            'resumo_curto': 'Dia com 3 conversas, todas viraram handoff.',
            'problemas': [{'tema': 'Conteúdo de cesta', 'ocorrencias': 3,
                           'exemplos': ['o que tem na cesta X0?'],
                           'sugestao': 'verificar se DESCRIÇÃO está vindo do VNDA'}],
        }
        # `criado_em` salva em BRT — filtrar com BRT (não utcnow).
        inicio = _agora() - timedelta(hours=1)
        fim = _agora() + timedelta(hours=1)
        with patch('app.services.chatbot_auditor._chamar_sonnet',
                   return_value=relatorio), \
             patch('app.services.zapi.enviar_texto',
                   return_value={'ok': True}) as send:
            r = chatbot_auditor.auditar_periodo(inicio, fim, enviar=True)
    assert r['enviado'] is True
    msg = send.call_args[0][1]
    assert 'Auditor do bot' in msg
    assert 'Conteúdo de cesta' in msg
    assert '3x' in msg


def test_auditor_sem_problemas_nao_envia(app):
    """Dia tranquilo → não pinga (Sonnet retornou sem problemas)."""
    from datetime import timedelta

    from app.extensions import db
    from app.models import VigiaVeredito
    from app.services import chatbot_auditor
    from app.utils import agora as _agora
    with app.app_context():
        db.session.add(VigiaVeredito(conv_id='c1', mensagem_cliente='oi',
                                      bot_acao='responder', alerta=False))
        db.session.commit()
        app.config['ANTHROPIC_API_KEY'] = 'test'
        app.config['ZAPI_NUMERO_DESTINO'] = '5511999990000'
        rel = {'tem_problemas': False, 'destaque': 'Tudo tranquilo',
               'resumo_curto': '1 conversa', 'problemas': []}
        inicio = _agora() - timedelta(hours=1)
        fim = _agora() + timedelta(hours=1)
        with patch('app.services.chatbot_auditor._chamar_sonnet', return_value=rel), \
             patch('app.services.zapi.enviar_texto') as send:
            r = chatbot_auditor.auditar_periodo(inicio, fim, enviar=True)
    assert r['enviado'] is False
    send.assert_not_called()


def test_auditor_sem_dados_pula(app):
    """Sem nenhum VigiaVeredito no periodo → pula (e não chama Sonnet)."""
    from datetime import datetime

    from app.services import chatbot_auditor
    with app.app_context():
        app.config['ANTHROPIC_API_KEY'] = 'test'
        with patch('app.services.chatbot_auditor._chamar_sonnet') as sonnet:
            r = chatbot_auditor.auditar_periodo(
                datetime(2020, 1, 1), datetime(2020, 1, 2), enviar=False)
    assert 'pulou' in r
    sonnet.assert_not_called()


def test_vigia_persiste_em_vigiaveredito(app):
    """Cada chamada a `_registrar` cria 1 VigiaVeredito (sem isso o auditor
    fica cego entre deploys)."""
    from app.extensions import db
    from app.models import VigiaVeredito
    from app.services import chatbot_vigia
    with app.app_context():
        chatbot_vigia._historico.clear()
        n_antes = VigiaVeredito.query.count()
        veredicto = {'alerta': True, 'gravidade': 'media',
                     'motivo': 'handoff evitavel', 'acao_sugerida': ''}
        chatbot_vigia._registrar(
            {'veredicto': veredicto},
            conv_id=99, nome_contato='Maria',
            ultima_mensagem_cliente='o que tem na cesta?',
            resultado_bot={'acao': 'handoff', 'motivo': 'conteudo de cesta'})
        n_depois = VigiaVeredito.query.count()
        assert n_depois == n_antes + 1
        v = VigiaVeredito.query.order_by(VigiaVeredito.id.desc()).first()
        assert v.gravidade == 'media'
        assert v.bot_acao == 'handoff'
        assert 'cesta' in (v.mensagem_cliente or '')
        db.session.remove()


# ── Fase 4: vigia (IA supervisora) ──

def test_webhook_passa_resposta_do_bot_pro_vigia(app):
    """O historico que vai pro vigia inclui a resposta do bot. Sem isso, o
    vigia nunca consegue julgar o que o bot disse (caso real visto em prod
    em 08/06: vigia avaliando como 'bot ainda nao respondeu')."""
    app.config['CHATBOT_BOT_SECRET'] = app.config['CHATBOT_BOT_SECRET'] = 'seg'
    app.config['CHATBOT_VIGIA'] = True
    app.config['ANTHROPIC_API_KEY'] = 'test'
    app.config['ZAPI_NUMERO_DESTINO'] = '5511999990000'
    app.config['CHATWOOT_BOT_SECRET'] = 'seg'
    client = app.test_client()
    historico_cliente = [{'role': 'user', 'content': 'tem croissant?'}]
    resposta_bot = {'acao': 'responder',
                    'texto': 'Infelizmente está esgotado hoje.'}
    with patch('threading.Thread', _SyncThread), \
         patch('app.services.chatwoot.buscar_historico',
               return_value=historico_cliente), \
         patch('app.services.chatbot.responder', return_value=resposta_bot), \
         patch('app.services.chatwoot.enviar_mensagem', return_value={'ok': True}), \
         patch('app.services.chatwoot.definir_status', return_value={'ok': True}), \
         patch('app.services.chatbot_vigia.disponivel', return_value=True), \
         patch('app.services.chatbot_vigia.avaliar', return_value={}) as vigia:
        _post(client, content='tem croissant?')
    assert vigia.called, 'vigia deveria ter sido chamado'
    historico_passado = vigia.call_args.args[0]
    # Cliente + resposta do bot — vigia tem o contexto completo
    assert len(historico_passado) == 2
    assert historico_passado[0]['role'] == 'user'
    assert historico_passado[1]['role'] == 'assistant'
    assert 'esgotado' in historico_passado[1]['content']


def test_vigia_dispara_alerta_quando_gravidade_alta(app):
    """Vigia avalia, Haiku retorna alerta=alta, envia via Z-API."""
    from app.services import chatbot_vigia
    veredicto = {'alerta': True, 'gravidade': 'alta',
                 'motivo': 'Bot disse esgotado pro croissant que tem na loja',
                 'acao_sugerida': 'Atualizar VNDA'}
    historico = [{'role': 'user', 'content': 'tem croissant?'},
                 {'role': 'assistant', 'content': 'desculpe, esgotado'}]
    with app.app_context():
        app.config['CHATBOT_VIGIA'] = True
        app.config['ANTHROPIC_API_KEY'] = 'test'
        app.config['ZAPI_NUMERO_DESTINO'] = '5511999990000'
        with patch('app.services.chatbot_vigia._chamar_modelo',
                   return_value=veredicto), \
             patch('app.services.chatbot_vigia._resumo_catalogo_site',
                   return_value='- Croissant: 12 un'), \
             patch('app.services.zapi.enviar_texto',
                   return_value={'ok': True}) as send:
            r = chatbot_vigia.avaliar(historico, conv_id=42, nome_contato='Maria')
    assert r['enviado'] is True
    args = send.call_args
    assert args[0][0] == '5511999990000'   # numero do dono
    msg = args[0][1]
    assert 'ALTA' in msg
    assert 'Maria' in msg
    assert 'croissant' in msg.lower()


def test_vigia_silencia_quando_sem_alerta(app):
    """Conversa normal: vigia decide nao alertar -> Z-API NAO chamado."""
    from app.services import chatbot_vigia
    veredicto = {'alerta': False, 'gravidade': None,
                 'motivo': 'conversa normal', 'acao_sugerida': ''}
    historico = [{'role': 'user', 'content': 'oi'},
                 {'role': 'assistant', 'content': 'olá!'}]
    with app.app_context():
        app.config['CHATBOT_VIGIA'] = True
        app.config['ANTHROPIC_API_KEY'] = 'test'
        app.config['ZAPI_NUMERO_DESTINO'] = '5511999990000'
        with patch('app.services.chatbot_vigia._chamar_modelo',
                   return_value=veredicto), \
             patch('app.services.chatbot_vigia._resumo_catalogo_site',
                   return_value=''), \
             patch('app.services.zapi.enviar_texto') as send:
            r = chatbot_vigia.avaliar(historico, conv_id=42)
    assert r['silencio'] is True
    send.assert_not_called()


def test_vigia_silencia_quando_gravidade_baixa(app):
    """Anti-spam: gravidade=baixa nao dispara WhatsApp (só log)."""
    from app.services import chatbot_vigia
    veredicto = {'alerta': True, 'gravidade': 'baixa', 'motivo': 'pequeno',
                 'acao_sugerida': ''}
    with app.app_context():
        app.config['CHATBOT_VIGIA'] = True
        app.config['ANTHROPIC_API_KEY'] = 'test'
        app.config['ZAPI_NUMERO_DESTINO'] = '5511999990000'
        with patch('app.services.chatbot_vigia._chamar_modelo',
                   return_value=veredicto), \
             patch('app.services.chatbot_vigia._resumo_catalogo_site',
                   return_value=''), \
             patch('app.services.zapi.enviar_texto') as send:
            r = chatbot_vigia.avaliar([{'role': 'user', 'content': 'oi'}])
    assert r['silencio'] is True
    send.assert_not_called()


def test_vigia_desligado_pula(app):
    from app.services import chatbot_vigia
    with app.app_context():
        app.config['CHATBOT_VIGIA'] = False
        app.config['ANTHROPIC_API_KEY'] = 'test'
        with patch('app.services.chatbot_vigia._chamar_modelo') as call:
            r = chatbot_vigia.avaliar([{'role': 'user', 'content': 'oi'}])
    assert 'pulou' in r
    call.assert_not_called()


def test_vigia_resumo_e_do_catalogo_do_site_nao_estoque_loja(app):
    """Vigia compara contra o CATALOGO DO SITE (nosso, estoque REAL), mesma
    fonte que o bot consulta. Caso real (12/06/2026): Pain au Chocolat 872 un
    em estoque de loja fisica + site disponivel=true; o vigia antigo cruzava
    EstoqueLoja vs bot e mandava alerta 'esgotado mas tem 872', quando o bot
    estava alinhado com o site. Bot atende SITE; loja fisica e outra fonte."""
    from unittest.mock import patch

    from app.services import chatbot_vigia
    catalogo = [
        {'nome': 'Pain au Chocolat', 'disponivel': True},
        {'nome': 'Croissant Tradicional', 'disponivel': True},
        {'nome': 'Sourdough Especial', 'disponivel': False},
    ]
    with patch('app.services.bot_tools.catalogo_disponibilidade',
               return_value=catalogo):
        with app.app_context():
            resumo = chatbot_vigia._resumo_catalogo_site()
    assert 'Pain au Chocolat: DISPONIVEL' in resumo
    assert 'Croissant Tradicional: DISPONIVEL' in resumo
    assert 'Sourdough Especial: ESGOTADO' in resumo


def test_vigia_resumo_catalogo_indisponivel(app):
    """Catálogo fora → resumo informa sem quebrar."""
    from unittest.mock import patch

    from app.services import chatbot_vigia
    with patch('app.services.bot_tools.catalogo_disponibilidade',
               return_value=None):
        with app.app_context():
            resumo = chatbot_vigia._resumo_catalogo_site()
    assert 'indispon' in resumo.lower()


def test_vigia_nao_usa_mais_estoque_loja(app):
    """Regressao do falso alerta de 12/06/2026: o vigia agora compara so
    contra o catalogo do site (mesma fonte do bot). Estoque de loja
    fisica era apples-to-oranges e gerava 'bot delirou' quando o bot
    estava certo pela fonte dele."""
    from app.services import chatbot_vigia
    assert not hasattr(chatbot_vigia, '_resumo_estoque_loja')
    assert hasattr(chatbot_vigia, '_resumo_catalogo_site')
    # Prompt diz explicito: estoque de loja fisica NAO contradiz o bot
    assert 'DISPONÍVEL=true no catálogo do site' \
        in chatbot_vigia.PROMPT_VIGIA
    assert 'estoque de loja física é OUTRA fonte' \
        in chatbot_vigia.PROMPT_VIGIA


def test_vigia_extrai_json_com_markdown_wrapper(app):
    """Haiku as vezes responde ```json {...} ``` — parser tolera."""
    from types import SimpleNamespace

    from app.services import chatbot_vigia
    fake_resp = SimpleNamespace(content=[SimpleNamespace(
        type='text', text='```json\n{"alerta": false, "gravidade": null, '
                          '"motivo": "ok", "acao_sugerida": ""}\n```')])
    with app.app_context():
        app.config['ANTHROPIC_API_KEY'] = 'test'
        with patch('anthropic.Anthropic') as M:
            M.return_value.messages.create.return_value = fake_resp
            r = chatbot_vigia._chamar_modelo('test', 'contexto')
    assert r['alerta'] is False


def test_vigia_abandono_alerta_quando_perda_de_venda(app):
    """Cliente pediu produto, bot respondeu, cliente sumiu por 30min — alerta."""
    from app.services import chatbot_vigia
    veredicto = {'alerta': True, 'gravidade': 'alta',
                 'motivo': 'Cliente sumiu sem fechar pedido',
                 'acao_sugerida': 'mandar mensagem proativa'}
    historico = [
        {'role': 'user', 'content': 'Quero a Family Box pra amanhã'},
        {'role': 'assistant', 'content': 'Pra qual endereço?'},
    ]
    with app.app_context():
        chatbot_vigia._historico.clear()
        app.config['CHATBOT_VIGIA'] = True
        app.config['ANTHROPIC_API_KEY'] = 'test'
        app.config['ZAPI_NUMERO_DESTINO'] = '5511999990000'
        with patch('app.services.chatbot_vigia._chamar_modelo_abandono',
                   return_value=veredicto), \
             patch('app.services.zapi.enviar_texto',
                   return_value={'ok': True}) as send:
            r = chatbot_vigia.avaliar_abandono(
                historico, conv_id=88, nome_contato='Carlos',
                minutos_sem_resposta=30)
    assert r['enviado'] is True
    msg = send.call_args[0][1]
    assert '30 min sem resposta' in msg


def test_ja_avisado_abandono_persiste_no_banco(app):
    """Caso real (12/06/2026): o anti-spam do detector de abandono era um
    set em memoria que zerava a cada deploy — no dia em que o detector
    voltou a enxergar (fix do token), metralhou o dono com o backlog e
    re-metralharia a cada deploy. Agora o dedupe consulta VigiaVeredito
    (gravado pelo proprio avaliar_abandono com prefixo '[ABANDONO')."""
    from unittest.mock import patch

    from app.services import chatbot_vigia
    with app.app_context():
        chatbot_vigia._avisados_abandono.clear()
        # Nunca avaliado → False
        assert chatbot_vigia.ja_avisado_abandono(555) is False
        # Avalia (veredicto silencioso) → grava VigiaVeredito [ABANDONO...]
        app.config['CHATBOT_VIGIA'] = '1'
        with patch('app.services.chatbot_vigia._chamar_modelo_abandono',
                   return_value={'alerta': False, 'gravidade': None,
                                 'motivo': 'so cumprimento'}), \
             patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'x'}):
            chatbot_vigia.avaliar_abandono(
                [{'role': 'user', 'content': 'oi'}],
                conv_id=555, nome_contato='Teste',
                minutos_sem_resposta=30)
        # Simula DEPLOY: zera o cache em memoria
        chatbot_vigia._avisados_abandono.clear()
        # Banco lembra → True (nao re-avalia, nao re-alerta)
        assert chatbot_vigia.ja_avisado_abandono(555) is True
        # E aqueceu o cache em memoria de novo
        assert 555 in chatbot_vigia._avisados_abandono


def test_vigia_abandono_silencia_quando_conversa_so_cumprimento(app):
    """Cliente disse 'oi', bot respondeu, ninguem voltou — nao deve alertar."""
    from app.services import chatbot_vigia
    veredicto = {'alerta': False, 'gravidade': None,
                 'motivo': 'apenas cumprimento, sem demanda',
                 'acao_sugerida': ''}
    historico = [
        {'role': 'user', 'content': 'oi'},
        {'role': 'assistant', 'content': 'Olá! Como posso ajudar?'},
    ]
    with app.app_context():
        chatbot_vigia._historico.clear()
        app.config['CHATBOT_VIGIA'] = True
        app.config['ANTHROPIC_API_KEY'] = 'test'
        app.config['ZAPI_NUMERO_DESTINO'] = '5511999990000'
        with patch('app.services.chatbot_vigia._chamar_modelo_abandono',
                   return_value=veredicto), \
             patch('app.services.zapi.enviar_texto') as send:
            r = chatbot_vigia.avaliar_abandono(
                historico, conv_id=89, minutos_sem_resposta=20)
    assert r['silencio'] is True
    send.assert_not_called()


def test_chatwoot_listar_conversas_paradas_filtra_por_idade(app):
    """Conversa parada ha 20min entra; conversa de 5min nao."""
    import time as _t

    from app.services import chatwoot
    app.config['CHATWOOT_URL'] = 'https://cw.exemplo.com'
    app.config['CHATWOOT_BOT_TOKEN'] = 'tok'
    app.config['CHATWOOT_ACCOUNT_ID'] = '1'
    agora = _t.time()
    payload = {'data': {'payload': [
        {'id': 11, 'last_activity_at': agora - 20*60,  # 20 min atras
         'meta': {'sender': {'name': 'Maria'}}},
        {'id': 12, 'last_activity_at': agora - 5*60,   # 5 min atras
         'meta': {'sender': {'name': 'Joao'}}},
    ]}}
    fake = SimpleNamespace(status_code=200, text='x', json=lambda: payload)
    with app.app_context():
        with patch('requests.get', return_value=fake):
            paradas = chatwoot.listar_conversas_paradas(min_minutos=15)
    ids = [p['id'] for p in paradas]
    assert 11 in ids
    assert 12 not in ids
    assert paradas[0]['nome_contato'] == 'Maria'


# ── Fase 3: leitura de imagem ──

def test_baixar_imagem_comprime_e_base64(app):
    import base64

    from app.services import chatwoot
    app.config['CHATWOOT_URL'] = 'https://cw.exemplo.com'
    fake = SimpleNamespace(status_code=200, content=b'rawbytes',
                           headers={'Content-Type': 'image/png'})
    with app.app_context():
        with patch('requests.get', return_value=fake), \
             patch('app.utils.comprimir_imagem', return_value=b'jpegbytes'):
            r = chatwoot.baixar_imagem('/rails/blob/123')
    assert r is not None
    media_type, b64 = r
    assert media_type == 'image/jpeg'
    assert base64.b64decode(b64) == b'jpegbytes'


def test_baixar_imagem_formato_nao_suportado(app):
    from app.services import chatwoot
    fake = SimpleNamespace(status_code=200, content=b'heicbytes', headers={})
    with app.app_context():
        with patch('requests.get', return_value=fake), \
             patch('app.utils.comprimir_imagem', side_effect=ValueError('heic')):
            r = chatwoot.baixar_imagem('https://cw/x.heic')
    assert r is None


def test_buscar_historico_extrai_imagem(app):
    from app.services import chatwoot
    app.config['CHATWOOT_URL'] = 'https://cw.exemplo.com'
    app.config['CHATWOOT_BOT_TOKEN'] = 'tok'
    app.config['CHATWOOT_ACCOUNT_ID'] = '1'
    payload = {'payload': [
        {'message_type': 'incoming', 'created_at': 1, 'content': '',
         'attachments': [{'file_type': 'image', 'data_url': 'https://cw/x.png'}]},
    ]}
    fake = SimpleNamespace(status_code=200, text='x', json=lambda: payload)
    with app.app_context():
        with patch('requests.get', return_value=fake):
            hist = chatwoot.buscar_historico(7)
    assert hist[-1]['imagens'] == ['https://cw/x.png']
    assert hist[-1]['role'] == 'user'


def test_build_messages_imagem_so_na_ultima(app):
    from app.services import chatbot
    historico = [
        {'role': 'user', 'content': 'oi', 'imagens': ['https://cw/velha.png']},
        {'role': 'assistant', 'content': 'olá'},
        {'role': 'user', 'content': 'olha esse pedido',
         'imagens': ['https://cw/atual.png']},
    ]
    with app.app_context():
        with patch('app.services.chatwoot.baixar_imagem',
                   return_value=('image/jpeg', 'BASE64')) as bi:
            msgs = chatbot._build_messages(historico)
    # so a ultima mensagem do cliente vira blocos com imagem
    ult = msgs[-1]
    assert isinstance(ult['content'], list)
    tipos = [b['type'] for b in ult['content']]
    assert 'image' in tipos and 'text' in tipos
    img_block = next(b for b in ult['content'] if b['type'] == 'image')
    assert img_block['source']['data'] == 'BASE64'
    # a 1a mensagem (imagem antiga) fica só texto — baixa 1 imagem só
    assert isinstance(msgs[0]['content'], str)
    bi.assert_called_once_with('https://cw/atual.png')


def test_consultar_produtos_cesta_traz_kind_id_e_url(app):
    """Catálogo PRÓPRIO: cesta (produto) vem com kind+id e url do opao.online
    — é o que o bot passa pro gerar_link_carrinho (não mais SKU)."""
    from app.extensions import db
    from app.services import bot_tools
    with app.app_context():
        ids = _catalogo_loja(db)
        r = bot_tools.consultar_produtos('monamour')
    p = r['produtos'][0]
    assert p['kind'] == 'produto'
    assert p['id'] == ids['box']
    assert float(p['preco']) == 200.0
    assert p['url'].endswith('-p%d' % ids['box'])


def test_consultar_produtos_erro_catalogo_forca_handoff(app):
    """Falha ao carregar o catálogo -> {'erro'}, pra o bot passar pro humano
    (nunca inventar preço)."""
    from app.services import bot_tools
    with app.app_context():
        with patch('app.services.loja_catalogo.produtos_publicados',
                   side_effect=RuntimeError('db fora')):
            r = bot_tools.consultar_produtos('cesta')
    assert 'erro' in r


def _pedido_online_nf(db, codigo='ABC123', telefone='11988887777',
                      cpf='52998224725', status='a_caminho', nf_id=None):
    """Cria Cliente (com CPF) + PedidoOnline pra testar a Fase 3."""
    from datetime import date
    from decimal import Decimal

    from app.models import Cliente, PedidoOnline, PedidoOnlineItem
    cli = Cliente(nome='Maria', email='m@x.com', telefone=telefone, cpf=cpf)
    db.session.add(cli)
    db.session.commit()
    p = PedidoOnline(
        codigo=codigo, cliente_id=cli.id, nome_cliente='Maria',
        email_cliente='m@x.com', telefone_cliente=telefone,
        modo_entrega='agendada', status=status,
        data_entrega=date(2026, 6, 25), janela_entrega='08:00–09:00',
        subtotal=Decimal('40'), valor_total=Decimal('45'),
        tiny_nota_fiscal_id=nf_id)
    p.itens.append(PedidoOnlineItem(
        kind='produto', nome='Box Mimo', preco_unitario=Decimal('40'),
        quantidade=1, subtotal=Decimal('40')))
    db.session.add(p)
    db.session.commit()
    return p


def test_consultar_pedido_online_por_telefone(app):
    """Pedido NATIVO do site: autoriza pelo telefone do canal e traz status
    amigável + data + itens do NOSSO banco (sem tocar no VNDA)."""
    from app.extensions import db
    from app.services import bot_tools
    with app.app_context():
        _pedido_online_nf(db, codigo='ON123', telefone='11988887777',
                          status='a_caminho')
        r = bot_tools.consultar_pedido('ON123', telefone_contato='11988887777')
    assert r['numero'] == 'ON123'
    assert 'caminho' in r['status']
    assert r['data_entrega'] == '25/06/2026'
    assert r['itens'][0]['nome'] == 'Box Mimo'


def test_consultar_pedido_online_por_cpf(app):
    """Sem telefone do canal, autoriza pelo CPF do comprador."""
    from app.extensions import db
    from app.services import bot_tools
    with app.app_context():
        _pedido_online_nf(db, codigo='ON124', cpf='52998224725')
        r0 = bot_tools.consultar_pedido('ON124')  # sem auth
        assert r0.get('erro') == 'autorizacao_necessaria'
        r = bot_tools.consultar_pedido('ON124', cpf_cliente='529.982.247-25')
    assert r['numero'] == 'ON124'


def test_consultar_pedido_online_nao_autorizado(app):
    """Telefone e CPF errados → autorizacao_necessaria (não vaza o pedido)."""
    from app.extensions import db
    from app.services import bot_tools
    with app.app_context():
        _pedido_online_nf(db, codigo='ON125', telefone='11988887777',
                          cpf='52998224725')
        r = bot_tools.consultar_pedido('ON125', telefone_contato='11000000000',
                                       cpf_cliente='11111111111')
    assert r['erro'] == 'autorizacao_necessaria'


def test_consultar_pedido_cai_pro_vnda_se_nao_for_nosso(app):
    """Código que NÃO é PedidoOnline → fallback VNDA (transição em paralelo)."""
    from app.services import bot_tools
    order = {'code': 'VND9', 'status': 'shipped', 'total': 100,
             'items': [{'product_name': 'Croissant', 'quantity': 2}]}
    with app.app_context():
        with patch('app.services.bot_tools._autorizar_pedido',
                   return_value={'ok': True, 'order': order}), \
             patch('app.services.vnda._extrair_data_entrega', return_value=None), \
             patch('app.services.vnda._extrair_periodo', return_value=None):
            r = bot_tools.consultar_pedido('VND9', telefone_contato='11999')
    assert r['numero'] == 'VND9'
    assert r['status'] == 'shipped'


def test_nf_pedido_online_com_nota(app):
    """NF de pedido nativo: CPF bate + NF emitida → devolve o link do DANFE."""
    from app.extensions import db
    from app.services import bot_tools
    with app.app_context():
        _pedido_online_nf(db, codigo='NF100', cpf='52998224725', nf_id='99')
        with patch('app.services.tiny_nf.link_danfe',
                   return_value='https://tiny/nf/99.pdf'):
            r = bot_tools.buscar_nota_fiscal('529.982.247-25', 'NF100')
    assert r['link'] == 'https://tiny/nf/99.pdf'
    assert r['numero_pedido'] == 'NF100'


def test_nf_pedido_online_sem_nota_ainda(app):
    from app.extensions import db
    from app.services import bot_tools
    with app.app_context():
        _pedido_online_nf(db, codigo='NF101', cpf='52998224725', nf_id=None)
        r = bot_tools.buscar_nota_fiscal('529.982.247-25', 'NF101')
    assert r['erro'] == 'sem_nf_ainda'


def test_nf_pedido_online_cpf_errado_nao_vaza(app):
    """CPF não bate no pedido nativo → 'nao_encontrado' (não confirma que
    existe). NUNCA expõe NF de outro cliente."""
    from app.extensions import db
    from app.services import bot_tools
    with app.app_context():
        _pedido_online_nf(db, codigo='NF102', cpf='52998224725', nf_id='99')
        r = bot_tools.buscar_nota_fiscal('11111111111', 'NF102')
    assert r['erro'] == 'nao_encontrado'


def test_responder_erro_produtos_forca_handoff(app):
    """Se consultar_produtos falha, o bot NUNCA repassa preço de memória —
    força handoff (salvaguarda de dinheiro)."""
    from app.services import chatbot
    tool_blk = SimpleNamespace(type='tool_use', name='consultar_produtos',
                               id='t1', input={'busca': 'cesta'})
    resp1 = SimpleNamespace(content=[tool_blk])
    # Claude "tentaria" listar preços de memória — o guard tem que barrar.
    resp2 = _resp_texto('Box Mimo — R$166\nBonjour — R$215')
    with app.app_context():
        app.config['ANTHROPIC_API_KEY'] = 'test'
        with patch('anthropic.Anthropic') as M, \
             patch('app.services.bot_tools.consultar_produtos',
                   return_value={'erro': 'VNDA indisponível no momento'}):
            M.return_value.messages.create.side_effect = [resp1, resp2]
            r = chatbot.responder([{'role': 'user', 'content': 'quero uma cesta'}])
    assert r['acao'] == 'handoff'
    assert 'R$' not in r['texto']   # não vaza preço inventado


# ── Histórico persistente (fix da perda de contexto, 2026-06-09) ──

def test_historico_persiste_contexto_entre_turnos(app):
    """Cerne do fix: o contexto sobrevive no NOSSO banco entre turnos, sem
    depender do Chatwoot. Simula o caso que quebrou em prod: pedido num turno,
    CPF no turno seguinte."""
    from app.services import chatbot
    with app.app_context():
        # Turno 1: cliente pede NF. historico passado ao salvar termina na msg dele.
        chatbot.salvar_historico(
            'conv-9', [{'role': 'user', 'content': 'quero a nf do pedido BF6390FBCD'}],
            'Preciso também do CPF do pedido. Pode informar?')
        store = chatbot.carregar_historico('conv-9')
        assert store == [
            {'role': 'user', 'content': 'quero a nf do pedido BF6390FBCD'},
            {'role': 'assistant', 'content': 'Preciso também do CPF do pedido. Pode informar?'},
        ]
        # Turno 2: cliente manda o CPF sozinho. _processar faria store + [msg_atual].
        chatbot.salvar_historico(
            'conv-9', store + [{'role': 'user', 'content': '23519277883'}], 'Buscando...')
        conteudos = [m['content'] for m in chatbot.carregar_historico('conv-9')]
        # Contexto completo preservado: o pedido E o CPF estao no historico.
        assert 'quero a nf do pedido BF6390FBCD' in conteudos
        assert '23519277883' in conteudos


def test_historico_vazio_quando_nao_existe(app):
    from app.services import chatbot
    with app.app_context():
        assert chatbot.carregar_historico('inexistente') == []


def test_historico_capa_no_maximo(app):
    from app.services import chatbot
    with app.app_context():
        big = [{'role': 'user', 'content': f'msg {i}'} for i in range(60)]
        chatbot.salvar_historico('conv-cap', big, 'ok')
        assert len(chatbot.carregar_historico('conv-cap')) == chatbot.MAX_HIST_STORE


def test_historico_imagem_vira_placeholder(app):
    """Msg só-imagem (sem texto) é preservada como turno, não some do histórico."""
    from app.services import chatbot
    with app.app_context():
        chatbot.salvar_historico(
            'conv-img', [{'role': 'user', 'content': '', 'imagens': ['http://x/y.jpg']}], 'vi sua imagem')
        store = chatbot.carregar_historico('conv-img')
        assert store[0]['content'] == '[imagem enviada]'


def test_tiny_get_retry_em_glitch_transiente(app):
    """`_get` faz 3 tentativas com backoff em 503/timeout/etc. Cenario real
    visto em prod 2026-06-09: Tiny tosse na 1a e 2a, recupera na 3a."""
    from app.services import tiny

    class _Resp:
        def __init__(self, status, json_data=None):
            self.status_code = status
            self._json = json_data

        def json(self):
            return self._json

    chamadas = []
    ok = _Resp(200, {'retorno': {'status': 'OK', 'pedidos': []}})
    erros = [_Resp(503), _Resp(503), ok]

    def _post(*args, **kwargs):
        chamadas.append(1)
        return erros[len(chamadas) - 1]

    with app.app_context():
        app.config['TINY_API_TOKEN'] = 'xxx'
        with patch('app.services.tiny.requests.post', side_effect=_post), \
             patch('app.services.tiny.time.sleep'):   # nao espera de verdade
            r = tiny._get('pedidos.pesquisa.php', params={'cpf_cnpj': 'X'})
    assert r is not None
    assert r.get('status') == 'OK'
    assert len(chamadas) == 3   # tentou 3 vezes ate sucesso


def test_tiny_get_propaga_causa_quando_todas_falham(app):
    """Apos 3 503s, `_consumir_falha` deve dar a causa (HTTP 503)."""
    from app.services import tiny

    class _Resp503:
        status_code = 503

    with app.app_context():
        app.config['TINY_API_TOKEN'] = 'xxx'
        with patch('app.services.tiny.requests.post', return_value=_Resp503()), \
             patch('app.services.tiny.time.sleep'):
            r = tiny._get('pedidos.pesquisa.php')
    assert r is None
    causa = tiny._consumir_falha()
    assert causa is not None
    assert '503' in causa


def test_parse_produtos_inclui_url_da_pagina(app):
    """Caso Ben (10/06/2026): cesta sazonal fora da lista fixa do prompt fez
    o bot mandar link de OUTRA cesta. A url agora vem do slug do catalogo —
    produto novo nunca depende de lista decorada."""
    from app.services.bot_tools import _parse_produtos
    raw = [{'name': 'Cesta Especial dia dos Namorados',
            'slug': 'cesta-especial-dia-dos-namorados-51',
            'price': 350.0,
            'variants': [{'sku': 'CESTA-NAM', 'available': True}]},
           {'name': 'Sem Slug', 'variants': [{'sku': 'X1'}]}]
    out = _parse_produtos(raw)
    assert out[0]['url'] == ('https://www.padariaartesanalonline.com.br'
                             '/produto/cesta-especial-dia-dos-namorados-51')
    assert out[1]['url'] is None


# ── Follow-up automatico (bot retoma cliente que sumiu) ────────────────


def _hist_bot_por_ultimo():
    return [{'role': 'user', 'content': 'quero a family box'},
            {'role': 'assistant', 'content': 'Aqui esta o link! 😊'}]


def test_followup_envia_quando_cliente_sumiu(app):
    """Pedido do dono (12/06/2026, conversa #186): cliente silencioso
    apos mensagem NOSSA → bot manda cutucao gentil, registra
    [FOLLOWUP em VigiaVeredito e nao repete."""
    from unittest.mock import patch

    from app.models import VigiaVeredito
    from app.services import chatbot
    with app.app_context(), \
         patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'x'}), \
         patch('app.services.chatwoot.listar_conversas_paradas',
               return_value=[{'id': 42, 'nome_contato': 'Bethania',
                              'minutos_paradas': 7}]), \
         patch('app.services.chatwoot.buscar_historico',
               return_value=_hist_bot_por_ultimo()), \
         patch('app.services.chatbot._followup_gerar_texto',
               return_value='Conseguiu finalizar seu pedido? 😊'), \
         patch('app.services.chatwoot.enviar_mensagem',
               return_value={'ok': True}) as envia:
        r = chatbot.followup_conversas_paradas()
        assert r == {'avaliadas': 1, 'enviadas': 1}
        envia.assert_called_once_with(42, 'Conseguiu finalizar seu pedido? 😊')
        row = VigiaVeredito.query.filter(
            VigiaVeredito.conv_id == '42',
            VigiaVeredito.mensagem_cliente.like('[FOLLOWUP%')).first()
        assert row is not None
        assert row.bot_acao == 'followup'
        assert row.enviado_whatsapp is True

        # Segundo ciclo: dedupe via banco — NAO manda de novo
        r2 = chatbot.followup_conversas_paradas()
        assert r2['enviadas'] == 0
        assert envia.call_count == 1


def test_followup_nao_cutuca_se_ultima_msg_e_do_cliente(app):
    """Cliente mandou a ultima mensagem = quem esta devendo resposta
    somos NOS (bot falhou) — cutucar seria constrangedor."""
    from unittest.mock import patch

    from app.services import chatbot
    hist = [{'role': 'assistant', 'content': 'oi!'},
            {'role': 'user', 'content': 'quanto custa a cesta?'}]
    with app.app_context(), \
         patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'x'}), \
         patch('app.services.chatwoot.listar_conversas_paradas',
               return_value=[{'id': 43, 'minutos_paradas': 10}]), \
         patch('app.services.chatwoot.buscar_historico',
               return_value=hist), \
         patch('app.services.chatwoot.enviar_mensagem') as envia:
        r = chatbot.followup_conversas_paradas()
    assert r['enviadas'] == 0
    envia.assert_not_called()


def test_followup_ignora_conversa_fria(app):
    """Parada ha mais que CHATBOT_FOLLOWUP_MAX_MIN (default 120) = fria.
    Cutucao 19h depois e creepy e ainda esbarra na janela de 24h da
    Meta."""
    from unittest.mock import patch

    from app.services import chatbot
    with app.app_context(), \
         patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'x'}), \
         patch('app.services.chatwoot.listar_conversas_paradas',
               return_value=[{'id': 44, 'minutos_paradas': 1145}]), \
         patch('app.services.chatwoot.enviar_mensagem') as envia:
        r = chatbot.followup_conversas_paradas()
    assert r['enviadas'] == 0
    envia.assert_not_called()


def test_followup_kill_switch(app):
    from app.services import chatbot
    with app.app_context():
        app.config['CHATBOT_FOLLOWUP'] = '0'
        r = chatbot.followup_conversas_paradas()
    assert r == {'pulou': 'desligado'}


def test_followup_respeita_teto_por_ciclo(app):
    """Backlog grande escoa em ciclos (default 3 por ciclo) — nada de
    rajada nos clientes."""
    from unittest.mock import patch

    from app.services import chatbot
    paradas = [{'id': i, 'minutos_paradas': 10} for i in range(1, 9)]
    with app.app_context(), \
         patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'x'}), \
         patch('app.services.chatwoot.listar_conversas_paradas',
               return_value=paradas), \
         patch('app.services.chatwoot.buscar_historico',
               return_value=_hist_bot_por_ultimo()), \
         patch('app.services.chatbot._followup_gerar_texto',
               return_value='Oi! Tudo certo?'), \
         patch('app.services.chatwoot.enviar_mensagem',
               return_value={'ok': True}) as envia:
        r = chatbot.followup_conversas_paradas()
    assert r['enviadas'] == 3
    assert envia.call_count == 3
