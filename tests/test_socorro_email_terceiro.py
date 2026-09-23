"""Bot de atendimento — socorro antes de credencial (23/09/2026).

Três decisões do dono, num dia de problemas de entrega:
- Falha operacional EM CURSO ("não recebi", "motoboy foi embora", "veio
  errado", "ninguém responde") transfere na PRIMEIRA mensagem, sem pedir
  CPF/número/e-mail e sem a macro "atendimento em alta demanda". O pedido é
  localizado em paralelo e vai pra nota interna do handoff.
- E-MAIL localiza e autoriza pedido (normalizado); CPF que não confere
  porque o cadastro não tem CPF vira nota interna + handoff, nunca "não
  localizei". A tool distingue não existe / existe sem autorização /
  autorizado — a fala ao cliente não revela existência; a nota interna sim.
- TERCEIRO falando pelo titular (presente = três pessoas) é handoff com
  contexto, não interrogatório.
Anthropic, Chatwoot e Z-API sempre mockados.
"""
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.extensions import db
from app.utils import agora

POSITIVOS = [
    'não recebi meu pedido', 'o motoboy foi embora e não entregou',
    'veio errado, faltou a cesta', 'chegou estragado', 'meu pedido não chegou ainda',
    'ninguém me responde desde ontem', 'estou sem resposta', 'entregaram o pedido errado',
    'ainda não recebi', 'passou do horário e nada', 'Não recebi minha cesta!',
    'a entrega não aconteceu', 'o entregador não apareceu', 'até agora não chegou',
    'veio faltando 2 croissants', 'ta faltando o pão de queijo',
    'não consigo falar com ninguém', 'não recebi nenhuma resposta',
    'Bom dia, meu pedido de ontem não chegou', 'recebi outro pedido, não é o meu',
    'pedido veio incompleto', 'não recebi o pedido nem o e-mail',
]
NEGATIVOS = [
    'quero 2 cestas, se não receber até as 10 eu cancelo',
    'pode deixar na portaria se eu não receber?',
    'não recebi o cardápio, pode mandar de novo?',
    'nunca recebi nada errado de vocês',
    'quero fechar o pedido, não recebi o link',
    'o motoboy entrega até que horas?', 'e se vier errado, vocês trocam?',
    'não veio errado, veio certinho, obrigada', 'tem croissant hoje?',
    'cadê meu pedido?', 'vocês entregam em Moema?',
    'ainda não recebi o e-mail de confirmação', 'não recebi o código pix',
    'caso não chegue até as 10, o que faço?', 'ok',
    'quero saber se meu pedido já saiu', 'Amei! chegou tudo certinho',
    'o cardápio nunca chegou no meu e-mail',
    'se o motoboy não achar o prédio me liga', 'quando chega meu pedido?',
    'quanto custa o frete pra Moema?',
]


@pytest.mark.parametrize('texto', POSITIVOS)
def test_falha_operacional_positivos(texto):
    from app.services.chatbot import falha_operacional
    assert falha_operacional(texto) is True


@pytest.mark.parametrize('texto', NEGATIVOS)
def test_falha_operacional_negativos(texto):
    from app.services.chatbot import falha_operacional
    assert falha_operacional(texto) is False


# ── helpers ──────────────────────────────────────────────────────────────

def _pedido(codigo, telefone='11988887777', email='cliente@example.com',
            cpf=None, com_cliente=True, dias=1):
    from app.models import Cliente, PedidoOnline, PedidoOnlineItem
    cliente_id = None
    if com_cliente:
        cli = Cliente(nome='Ana Compradora', email=email, telefone=telefone, cpf=cpf)
        db.session.add(cli)
        db.session.flush()
        cliente_id = cli.id
    p = PedidoOnline(codigo=codigo, status='pago', cliente_id=cliente_id,
                     nome_cliente='Ana Compradora', email_cliente=email,
                     telefone_cliente=telefone, modo_entrega='agendada',
                     subtotal=Decimal('40'), frete_valor=Decimal('10'),
                     valor_total=Decimal('50'), cartinha='Feliz aniversário!')
    p.criado_em = agora() - timedelta(days=dias)
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoOnlineItem(pedido_id=p.id, kind='receita', nome='Sourdough',
                                    quantidade=2, preco_unitario=Decimal('20')))
    db.session.commit()
    return p


def _modelo_texto(texto='Posso ajudar!'):
    return SimpleNamespace(content=[SimpleNamespace(type='text', text=texto)],
                           stop_reason='end_turn')


# ── Item 4: falha operacional → handoff antes do modelo, com localização ─

def test_falha_operacional_transfere_sem_pedir_nada(app):
    from app.services import chatbot
    with app.app_context():
        _pedido('SOC00001', telefone='11988887777')
        app.config['ANTHROPIC_API_KEY'] = 'test'
        with patch('anthropic.Anthropic') as M:
            r = chatbot.responder([{'role': 'user', 'content': 'não recebi meu pedido'}],
                                  telefone_contato='5511988887777')
        M.return_value.messages.create.assert_not_called()
    assert r['acao'] == 'handoff'
    assert r['motivo'].startswith('falha operacional')
    # (fora do horário do chat o `_resp_handoff` prefixa o aviso — o texto
    # do socorro continua lá)
    assert chatbot.TEXTO_FALHA_OPERACIONAL in r['texto']
    for proibido in ('CPF', 'número do pedido', 'alta demanda', 'paciência'):
        assert proibido not in r['texto']
    # localizou em paralelo pelo telefone do canal — vai pra nota interna
    assert r['tools_usadas'] == ['consultar_pedido']
    assert 'SOC00001' in r['tools_resumo'][0] and 'AUTORIZADO' in r['tools_resumo'][0]
    # e o texto ao cliente NÃO carrega dados do pedido
    assert 'SOC00001' not in r['texto'] and 'pago' not in r['texto']


def test_falha_operacional_com_codigo_de_terceiro_anota_existencia(app):
    """Telefone do canal não bate, mas a fala traz o código: a nota interna
    diz que o pedido EXISTE e não foi autorizado; o cliente não fica
    sabendo — e recebe socorro do mesmo jeito."""
    from app.services import chatbot, entrega_candidata
    with app.app_context():
        _pedido('SOC0002A', telefone='11977776666')
        app.config['ANTHROPIC_API_KEY'] = 'test'
        hist = [{'role': 'user', 'content': 'pedido SOC0002A não chegou até agora'}]
        with patch('anthropic.Anthropic') as M:
            r = chatbot.responder(hist, telefone_contato='5511900000000')
        M.return_value.messages.create.assert_not_called()
        nota = entrega_candidata.nota_de_handoff(r, hist)
    assert r['acao'] == 'handoff'
    resumo = r['tools_resumo'][0]
    assert 'NAO autorizado' in resumo and 'SOC0002A EXISTE' in resumo
    assert 'SOC0002A EXISTE' in nota and 'uso interno' in nota
    assert 'EXISTE' not in r['texto']


def test_falha_operacional_usa_email_da_conversa(app):
    from app.services import chatbot
    with app.app_context():
        _pedido('SOC0003A', telefone='11977776666', email='maria@example.com')
        app.config['ANTHROPIC_API_KEY'] = 'test'
        hist = [{'role': 'user', 'content': 'comprei com maria@example.com'},
                {'role': 'assistant', 'content': 'Oi! Como posso ajudar?'},
                {'role': 'user', 'content': 'o motoboy foi embora sem entregar'}]
        with patch('anthropic.Anthropic'):
            r = chatbot.responder(hist, telefone_contato='')
    assert r['acao'] == 'handoff'
    assert 'SOC0003A' in r['tools_resumo'][0] and 'AUTORIZADO' in r['tools_resumo'][0]


def test_busca_paralela_falhando_nao_impede_o_socorro(app):
    from app.services import chatbot
    with app.app_context():
        app.config['ANTHROPIC_API_KEY'] = 'test'
        with patch('anthropic.Anthropic'), \
                patch('app.services.bot_tools.consultar_pedido',
                      side_effect=RuntimeError('banco fora')):
            r = chatbot.responder([{'role': 'user', 'content': 'veio errado, faltou a cesta'}],
                                  telefone_contato='5511988887777')
    assert r['acao'] == 'handoff' and r['tools_usadas'] == []


@pytest.mark.parametrize('texto', [
    'quero 2 cestas, se não receber até as 10 eu cancelo',
    'não recebi o link de pagamento, pode mandar de novo?',
])
def test_frase_de_venda_vai_pro_modelo(app, texto):
    from app.services import chatbot
    with app.app_context():
        app.config['ANTHROPIC_API_KEY'] = 'test'
        with patch('anthropic.Anthropic') as M:
            M.return_value.messages.create.return_value = _modelo_texto()
            r = chatbot.responder([{'role': 'user', 'content': texto}])
        M.return_value.messages.create.assert_called()
    assert r['acao'] != 'handoff'


@pytest.mark.parametrize('motivo, esperado', [
    ('cliente não recebeu o pedido', True),
    ('cliente relata que o motoboy foi embora', True),
    ('pedido veio incompleto, faltou a cesta', True),
    ('falha operacional relatada pelo cliente: estou sem resposta', True),
    ('cliente não recebeu o link de pagamento', False),
    ('cliente quer saber o frete pra Moema', False),
    ('cliente não reclamou de nada, só quer o cardápio', False),
    # hipótese/pergunta de processo no motivo NÃO é falha em curso
    ('cliente pergunta o que acontece se o pedido não chegou no horário', False),
    ('dúvida se o motoboy liga quando ninguém atende', False),
    ('cliente quer saber se entregam quando ninguém responde o interfone', False),
])
def test_enforcement_libera_falha_operacional_no_motivo(motivo, esperado):
    from app.services.chatbot import _handoff_excecao, motivo_excecao_legitima
    assert _handoff_excecao({'motivo': motivo}) is esperado
    if esperado:
        assert motivo_excecao_legitima(motivo) is True
    else:
        assert motivo_excecao_legitima(motivo) is False


def test_contencao_nao_sai_sobre_reclamacao_mas_dono_e_avisado(app):
    """"Atendimento em alta demanda, obrigado pela paciência" NUNCA sobre
    reclamação: o cliente que não recebeu não ganha macro; o dono recebe a
    cobrança com o aviso de que é problema."""
    from app.services import chatbot_vigia
    base = {'id': 4501, 'nome_contato': 'Bia', 'minutos_paradas': 15}
    hist = [{'role': 'user', 'content': 'não recebi meu pedido, o motoboy foi embora'}]
    with app.app_context(), \
            patch('app.services.chatbot_vigia._numero_destino',
                  return_value='5511999990000'), \
            patch('app.services.chatwoot.listar_conversas_paradas', return_value=[base]), \
            patch('app.services.chatwoot.buscar_historico', return_value=hist), \
            patch('app.services.chatwoot.enviar_mensagem') as contem, \
            patch('app.services.zapi.enviar_texto', return_value={'ok': True}) as alerta:
        chatbot_vigia.alertar_clientes_esperando_humano()
    contem.assert_not_called()
    alerta.assert_called_once()
    assert 'PROBLEMA' in alerta.call_args[0][1]
    assert 'NÃO foi enviada' in alerta.call_args[0][1]


def test_contencao_segue_para_duvida_comum(app):
    from app.services import chatbot_vigia
    base = {'id': 4502, 'nome_contato': 'Cau', 'minutos_paradas': 15}
    hist = [{'role': 'user', 'content': 'Vocês têm cesta de café?'}]
    with app.app_context(), \
            patch('app.services.chatbot_vigia._numero_destino',
                  return_value='5511999990000'), \
            patch('app.services.chatwoot.listar_conversas_paradas', return_value=[base]), \
            patch('app.services.chatwoot.buscar_historico', return_value=hist), \
            patch('app.services.chatwoot.enviar_mensagem',
                  return_value={'ok': True}) as contem, \
            patch('app.services.zapi.enviar_texto', return_value={'ok': True}) as alerta:
        chatbot_vigia.alertar_clientes_esperando_humano()
    contem.assert_called_once()
    assert 'PROBLEMA' not in alerta.call_args[0][1]


def test_vigia_nao_acusa_venda_em_risco_no_handoff_de_socorro():
    from app.services.chatbot_vigia import _e_handoff_preguicoso_em_compra
    hist = [{'role': 'user', 'content': 'quero a cesta brunch pra amanhã'},
            {'role': 'assistant', 'content': 'Fechado! Link enviado.'},
            {'role': 'user', 'content': 'estou sem resposta, ninguém me responde'}]
    rb = {'acao': 'handoff', 'tools_usadas': [], 'motivo': 'falha operacional'}
    assert _e_handoff_preguicoso_em_compra(hist, rb) is False


# ── Item 5: e-mail localiza/autoriza; cadastro sem CPF vira nota ─────────

def test_email_autoriza_pedido_pelo_numero(app):
    from app.services import bot_tools
    with app.app_context():
        _pedido('EML00001', telefone='11977776666', email='Cliente@Example.com')
        r = bot_tools.consultar_pedido('EML00001', telefone_contato='11900000000',
                                       email_cliente='  cliente@example.com ')
        assert r['numero'] == 'EML00001' and r['autorizado_como'] == 'email'
        assert r['cartinha'] == 'Feliz aniversário!'
        neg = bot_tools.consultar_pedido('EML00001', telefone_contato='11900000000',
                                         email_cliente='outra@example.com')
        assert neg['erro'] == 'autorizacao_necessaria'
        assert 'numero' not in neg and 'itens' not in neg and 'cartinha' not in neg
        assert 'e-mail informado NAO confere' in neg['_nota_interna']
        assert 'EML00001 EXISTE' in neg['_nota_interna']


def test_email_localiza_sem_numero(app):
    from app.services import bot_tools
    with app.app_context():
        _pedido('EML00002', telefone='11977776666', email='joao@example.com')
        r = bot_tools.consultar_pedido('', telefone_contato='',
                                       email_cliente='JOAO@example.com')
        assert r['numero'] == 'EML00002' and r['autorizado_como'] == 'email'
        _pedido('EML00003', telefone='11977776666', email='joao@example.com', dias=2,
                com_cliente=False)   # Cliente.email é unique; 2º pedido do mesmo e-mail
        lista = bot_tools.consultar_pedido('', telefone_contato='',
                                           email_cliente='joao@example.com')
        assert [p['numero'] for p in lista['pedidos_recentes']] == ['EML00002', 'EML00003']
        assert 'cartinha' not in lista['pedidos_recentes'][0]
        nada = bot_tools.consultar_pedido('', telefone_contato='',
                                          email_cliente='ninguem@example.com')
        assert nada['erro'] == 'nenhum_pedido_para_este_email'


def test_sem_telefone_nem_email_orienta_os_dois(app):
    from app.services import bot_tools
    with app.app_context():
        r = bot_tools.consultar_pedido('', telefone_contato='')
        assert 'e-mail' in r['erro'] and 'número' in r['erro']


def test_cpf_com_cadastro_sem_cpf_vira_nota_e_transferencia(app):
    from app.services import bot_tools
    from app.services.chatbot import _resumo_tool
    with app.app_context():
        _pedido('CPF00001', telefone='11977776666', cpf=None)
        r = bot_tools.consultar_pedido('CPF00001', telefone_contato='11900000000',
                                       cpf_cliente='529.982.247-25')
        assert r['erro'] == 'autorizacao_necessaria'
        assert 'cadastro deste pedido nao tem CPF' in r['instrucao']
        assert 'transfira' in r['instrucao'] and 'NAO peca o CPF de novo' in r['instrucao']
        assert 'NAO tem CPF' in r['_nota_interna']
        resumo = _resumo_tool('consultar_pedido', r)
        assert 'NAO autorizado' in resumo and 'NAO tem CPF' in resumo
        # CPF que simplesmente não bate: instrução de sempre, nota diz que não confere
        _pedido('CPF00002', telefone='11977776666', cpf='52998224725',
                email='b@example.com')
        r2 = bot_tools.consultar_pedido('CPF00002', telefone_contato='11900000000',
                                        cpf_cliente='11111111111')
        assert 'CPF informado NAO confere' in r2['_nota_interna']
        assert 'nao tem CPF' not in r2['instrucao']


def test_tres_respostas_da_tool(app):
    from app.services import bot_tools
    with app.app_context():
        _pedido('TRES0001', telefone='11977776666')
        with patch('app.services.bot_tools.vnda.buscar_pedido_completo', return_value=None):
            nao_existe = bot_tools.consultar_pedido('NADA0000', telefone_contato='11977776666')
        assert nao_existe['erro'] == 'pedido_nao_encontrado'
        sem_auth = bot_tools.consultar_pedido('TRES0001', telefone_contato='11900000000')
        assert sem_auth['erro'] == 'autorizacao_necessaria' and '_nota_interna' in sem_auth
        ok = bot_tools.consultar_pedido('TRES0001', telefone_contato='11977776666')
        assert ok['numero'] == 'TRES0001' and ok['autorizado_como'] == 'comprador'


def test_nota_interna_nunca_chega_ao_modelo(app):
    """O tool_result mandado ao Claude NÃO carrega `_nota_interna`; ela
    fica no resumo (nota privada)."""
    import json

    from app.services import chatbot
    capturado = {}

    class FakeMsgs:
        def __init__(self):
            self.n = 0

        def create(self, **kw):
            self.n += 1
            capturado.setdefault('chamadas', []).append(kw['messages'])
            if self.n == 1:
                return SimpleNamespace(content=[SimpleNamespace(
                    type='tool_use', name='consultar_pedido', id='t1',
                    input={'numero': 'NOTA0001'})], stop_reason='tool_use')
            return _modelo_texto('Pra confirmar, me passa o CPF ou o e-mail da compra?')

    class FakeClient:
        def __init__(self, **kw):
            self.messages = FakeMsgs()

    with app.app_context():
        _pedido('NOTA0001', telefone='11977776666')
        app.config['ANTHROPIC_API_KEY'] = 'test'
        with patch('anthropic.Anthropic', FakeClient):
            r = chatbot.responder([{'role': 'user', 'content': 'quero ver o pedido NOTA0001'}],
                                  telefone_contato='11900000000')
    assert r['acao'] == 'responder'
    segunda = capturado['chamadas'][1]
    tool_results = [b for m in segunda if isinstance(m.get('content'), list)
                    for b in m['content']
                    if isinstance(b, dict) and b.get('type') == 'tool_result']
    assert tool_results
    payload = json.loads(tool_results[-1]['content'])
    assert payload['erro'] == 'autorizacao_necessaria'
    assert '_nota_interna' not in payload and 'EXISTE' not in json.dumps(payload)
    assert 'NOTA0001 EXISTE' in r['tools_resumo'][0]


# ── Item 6: terceiro falando pelo titular ────────────────────────────────

@pytest.mark.parametrize('motivo, esperado', [
    ('terceiro pelo titular: pedido ABCD1234 comprado pela mãe, relata que não veio', True),
    ('filha do comprador, e-mail ana@x.com, quer saber da entrega', True),
    ('minha mãe comprou a cesta, pedido em nome dela', True),
    ('quem comprou foi o marido; destinatária é a esposa', True),
    ('cliente quer cesta para a mãe', False),           # venda, sem identificação
    ('mãe comprou, quer cotar frete de outra cesta', False),  # venda em curso
    ('cliente perguntou o horário da loja', False),
])
def test_motivo_terceiro_pelo_titular(motivo, esperado):
    from app.services.chatbot import _handoff_excecao, motivo_terceiro_pelo_titular
    assert motivo_terceiro_pelo_titular(motivo) is esperado
    if esperado:
        assert _handoff_excecao({'motivo': motivo}) is True


def test_conversa_terceiro_pelo_titular_vira_handoff_com_contexto(app):
    """Filha escreve pelo pedido da mãe com o código: 1 consulta (não
    autorizada), depois handoff com o contexto — sem interrogatório e sem
    revelar ao terceiro que o pedido existe."""
    from app.services import chatbot, entrega_candidata
    fala = ('Minha mãe comprou uma cesta pra minha avó, pedido TERC0001, '
            'e não chegou até agora')

    class FakeMsgs:
        def __init__(self):
            self.n = 0

        def create(self, **kw):
            self.n += 1
            if self.n == 1:
                return SimpleNamespace(content=[SimpleNamespace(
                    type='tool_use', name='consultar_pedido', id='t1',
                    input={'numero': 'TERC0001'})], stop_reason='tool_use')
            return SimpleNamespace(content=[SimpleNamespace(
                type='tool_use', name='transferir_para_humano', id='t2',
                input={'mensagem_cliente': 'Vou passar pra equipe, que confirma com quem comprou.',
                       'motivo': 'terceiro pelo titular: pedido TERC0001 comprado pela mãe, '
                                 'recebe a avó — relata que não chegou'})],
                stop_reason='tool_use')

    class FakeClient:
        def __init__(self, **kw):
            self.messages = FakeMsgs()

    with app.app_context():
        _pedido('TERC0001', telefone='11977776666')
        app.config['ANTHROPIC_API_KEY'] = 'test'
        hist = [{'role': 'user', 'content': 'Oi, tudo bem?'},
                {'role': 'assistant', 'content': 'Oi! Em que posso ajudar?'},
                {'role': 'user', 'content': fala.replace('e não chegou até agora',
                                                          'e quero saber da entrega')}]
        with patch('anthropic.Anthropic', FakeClient):
            r = chatbot.responder(hist, telefone_contato='11900000000')
        nota = entrega_candidata.nota_de_handoff(r, hist)
    assert r['acao'] == 'handoff'
    assert r['tools_usadas'] == ['consultar_pedido']
    assert 'TERC0001 EXISTE' in r['tools_resumo'][0]
    assert 'terceiro pelo titular' in nota and 'TERC0001 EXISTE' in nota
    assert 'EXISTE' not in r['texto'] and 'pago' not in r['texto']


def test_terceiro_relatando_falha_recebe_socorro_na_primeira_mensagem(app):
    """A mesma filha, mas relatando que NÃO chegou: Camada 1 transfere na
    hora (sem chamar o modelo), com a existência do pedido na nota interna."""
    from app.services import chatbot
    with app.app_context():
        _pedido('TERC0002', telefone='11977776666')
        app.config['ANTHROPIC_API_KEY'] = 'test'
        with patch('anthropic.Anthropic') as M:
            r = chatbot.responder(
                [{'role': 'user', 'content': 'Minha mãe comprou o pedido TERC0002 pra '
                                             'minha avó e não chegou até agora'}],
                telefone_contato='11900000000')
        M.return_value.messages.create.assert_not_called()
    assert r['acao'] == 'handoff'
    assert 'TERC0002 EXISTE' in r['tools_resumo'][0]
    assert 'TERC0002' not in r['texto']


def test_prompt_tem_as_secoes_novas():
    from app.services.chatbot_prompt import PROMPT
    assert 'FALHA OPERACIONAL EM CURSO' in PROMPT
    assert 'TERCEIRO FALANDO PELO TITULAR' in PROMPT
    assert 'email_cliente' in PROMPT
    assert 'alta demanda' in PROMPT           # a macro proibida está nomeada
    assert 'nunca decide se a pessoa recebe ajuda' in PROMPT
    # a busca por e-mail existe: o texto antigo "não tem busca por e-mail" saiu
    assert 'não tem busca por e-mail' not in PROMPT
