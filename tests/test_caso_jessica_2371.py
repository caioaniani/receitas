"""Caso Jéssica Santos — conv 2371, 19/09/2026 (regressões).

O auditor das 12h relatou "bot confundiu pedido dela com o de outra pessoa
3 vezes". Investigação (5 investigadores + refutadores, código real):
o pedido E49C374A ERA da própria cliente (criado 09:48, pago 09:49, print
enviado 09:49:51). Quatro defeitos reais, todos travados aqui:

1. O VIGIA descartava a mensagem só-imagem (content '') e via duas falas
   do bot seguidas; também só recebia o NOME das tools, sem saber que
   consultar_pedido é fail-closed pelo telefone do canal → 3 ALTAs falsos.
2. O BOT tratou "Gaspar Lourenço 200. Ap 72" (7 s após confirmar um
   express pago gravado com "Rua X, 72") como cotação de frete, não como
   CORREÇÃO do pedido — e a consulta nem devolvia o endereço gravado.
3. O ALTA falso criou um incidente grave e o espera-humano alertou
   "esperando ATENDENTE há 4min em conversa open" numa conversa PENDING
   que o bot atendia (os 4 min eram a idade do ALTA).
4. O AUDITOR contou 3 vereditos da mesma conversa como "3 vezes" e colou
   o único pedido pago da janela ("R$430") na conversa — pagamento
   anterior à confusão.
"""
import time
from datetime import timedelta
from unittest.mock import patch

import pytest

IMG = 'https://cw.example/rails/active_storage/blobs/abc/print.jpg'


def _ctx_vigia(app, historico, resultado_bot):
    """Roda `_avaliar_interno` com o modelo mockado e devolve o contexto que
    o vigia receberia."""
    from app.services import chatbot_vigia
    capturado = {}

    def fake_modelo(api_key, contexto):
        capturado['ctx'] = contexto
        return {'alerta': False, 'gravidade': None, 'motivo': 'ok'}

    with patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'x'}), \
            patch('app.services.chatbot_vigia._chamar_modelo',
                  side_effect=fake_modelo), \
            patch('app.services.chatbot_vigia._resumo_catalogo_site',
                  return_value=''):
        app.config['CHATBOT_VIGIA'] = '1'
        chatbot_vigia._avaliar_interno(historico, conv_id=2371,
                                       nome_contato='Jéssica',
                                       resultado_bot=resultado_bot)
    return capturado['ctx']


HIST_IMAGEM = [
    {'role': 'user', 'content': 'Bom dia.\nGostaria de encomendar uma cesta de café'},
    {'role': 'assistant', 'content': 'Temos a Bandeja de Café da Manhã — R$435,00'},
    {'role': 'user', 'content': '', 'imagens': [IMG]},          # o print
    {'role': 'assistant', 'content': 'Encontrei seu pedido! E49C374A — Caixa Especial'},
]


# ── 1. Vigia enxerga imagem e resultado das tools ─────────────────────────

def test_vigia_ve_turno_so_imagem_e_resultado_das_tools(app):
    with app.app_context():
        ctx = _ctx_vigia(app, HIST_IMAGEM, {
            'acao': 'responder', 'tools_usadas': ['consultar_pedido'],
            'tools_resumo': ['consultar_pedido: pedido E49C374A localizado e '
                             'AUTORIZADO (telefone deste canal ou CPF '
                             'conferido) — status pago, em preparo'],
        })
    # A fala da cliente entre as duas do bot não some mais
    assert 'CLIENTE: [imagem enviada]' in ctx
    assert ctx.index('CLIENTE: [imagem enviada]') < ctx.index('Encontrei seu pedido')
    assert 'IMAGENS NESTE TURNO: o cliente enviou 1 imagem' in ctx
    assert 'AUTORIZADO' in ctx and 'E49C374A' in ctx


def test_vigia_sem_imagem_e_sem_tools_diz_isso(app):
    with app.app_context():
        ctx = _ctx_vigia(app, [{'role': 'user', 'content': 'oi'}],
                         {'acao': 'responder', 'tools_usadas': []})
    assert 'IMAGENS NESTE TURNO: nenhuma' in ctx
    assert 'RESULTADO DAS FERRAMENTAS (resumo): (nenhum)' in ctx


def test_veredito_registra_marcador_de_imagem_como_mensagem_do_cliente(app):
    """O 7245 real gravou a fala das 09:41 como se fosse a do turno."""
    from app.models import VigiaVeredito
    from app.services import chatbot_vigia
    with app.app_context(), \
            patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'x'}), \
            patch('app.services.chatbot_vigia._chamar_modelo',
                  return_value={'alerta': False, 'gravidade': None,
                                'motivo': 'ok'}), \
            patch('app.services.chatbot_vigia._resumo_catalogo_site',
                  return_value=''):
        app.config['CHATBOT_VIGIA'] = '1'
        chatbot_vigia.avaliar(HIST_IMAGEM, conv_id=2371, nome_contato='Jéssica',
                              resultado_bot={'acao': 'responder',
                                             'tools_usadas': ['consultar_pedido']})
        row = (VigiaVeredito.query.filter_by(conv_id='2371')
               .order_by(VigiaVeredito.id.desc()).first())
        assert row.mensagem_cliente == '[imagem enviada]'


def test_texto_da_mensagem_e_fonte_unica_do_store_e_do_vigia(app):
    from app.services import chatbot, chatbot_vigia
    assert chatbot.texto_da_mensagem({'content': '', 'imagens': [IMG]}) == '[imagem enviada]'
    assert chatbot.texto_da_mensagem({'content': ' oi ', 'imagens': [IMG]}) == 'oi'
    assert chatbot.texto_da_mensagem({'content': ''}) == ''
    assert chatbot.texto_da_mensagem(None) == ''
    texto = chatbot_vigia._formatar_historico(HIST_IMAGEM)
    assert 'CLIENTE: [imagem enviada]' in texto
    assert (texto.index('BOT: Temos') < texto.index('CLIENTE: [imagem enviada]')
            < texto.index('BOT: Encontrei'))


def test_prompt_vigia_tem_regra_de_posse_do_pedido_e_de_imagem():
    from app.services.chatbot_vigia import PROMPT_VIGIA
    assert 'POSSE DO PEDIDO' in PROMPT_VIGIA
    assert 'FAIL-CLOSED' in PROMPT_VIGIA
    assert 'NUNCA acuse "pedido de outra' in PROMPT_VIGIA
    assert '[imagem enviada]' in PROMPT_VIGIA
    assert 'AVALIE SÓ A ÚLTIMA RESPOSTA DO BOT' in PROMPT_VIGIA


def test_resumo_tool_consultar_pedido_sem_pii():
    from app.services.chatbot import _resumo_tool
    ok = _resumo_tool('consultar_pedido', {
        'numero': 'E49C374A', 'status': 'pago, em preparo',
        'cartinha': 'Desejo uma boa recuperação!', 'total': 430.0})
    assert 'AUTORIZADO' in ok and 'E49C374A' in ok
    assert 'recuperação' not in ok and '430' not in ok      # sem PII/valores
    assert 'NAO autorizado' in _resumo_tool(
        'consultar_pedido', {'erro': 'autorizacao_necessaria'})
    assert '2 pedidos recentes' in _resumo_tool(
        'consultar_pedido', {'pedidos_recentes': [{}, {}]})
    assert _resumo_tool('consultar_frete', {'erro': 'nao_encontrado'}).startswith(
        'consultar_frete: erro')
    assert _resumo_tool('consultar_produtos', {'itens': []}) == 'consultar_produtos: ok'


def test_responder_devolve_tools_resumo_em_todo_retorno_pos_tool():
    """Todo caminho de retorno que carrega tools_usadas carrega tambem o
    resumo — senao o vigia perde o sinal justo no caminho que faltou."""
    import pathlib
    src = pathlib.Path('app/services/chatbot.py').read_text()
    trecho = src[src.index('tools_resumo = []'):src.index('# ── Follow-up')]
    assert trecho.count('tools_usadas=tools_usadas') == trecho.count(
        'tools_resumo=tools_resumo')
    assert "'tools_resumo': tools_resumo" in trecho
    assert 'tools_resumo.append(_resumo_tool(b.name, out))' in trecho


# ── 2. Correção pós-confirmação de pedido pago ────────────────────────────

def test_prompt_bot_tem_correcao_pos_confirmacao():
    from app.services.chatbot_prompt import PROMPT
    idx = PROMPT.find('CORREÇÃO PÓS-CONFIRMAÇÃO')
    assert idx > 0
    bloco = PROMPT[idx:idx + 1200]
    assert 'NÃO chame consultar_frete' in bloco
    assert 'transferir_para_humano' in bloco
    assert 'corrigir endereço/destinatário do pedido' in bloco
    # A regra ⚡ de frete aponta pra exceção (senão o modelo segue cotando)
    idx_f = PROMPT.find('ANTES DE PEDIR CEP')
    assert 'EXCEÇÃO ÚNICA' in PROMPT[idx_f:idx_f + 600]
    # E o bloco pós-compra manda repetir o destino ao confirmar
    assert 'REPITA o endereço de entrega' in PROMPT
    # Exceção listada no bloco ANTES DE TRANSFERIR
    idx_t = PROMPT.find('ANTES DE TRANSFERIR')
    assert 'Correção de ENDEREÇO/DESTINATÁRIO' in PROMPT[idx_t:idx_t + 3000]


@pytest.mark.parametrize('motivo, esperado', [
    ('corrigir endereço do pedido E49C374A: Rua Gaspar Lourenço 200 ap 72, '
     'destinatária Ângela', True),
    ('Correção de endereço do pedido E49C374A: número 200, ap 72', True),
    ('alterar destinatário do pedido para a vizinha', True),
    ('mudar o número da casa no pedido pago', True),
    ('cliente quer trocar o apto do pedido', True),
    ('atualizar endereço da entrega, pedido 1A2B3C4D', True),
    # Só o código (sem a palavra "pedido") também ancora
    ('editar endereço de 1A2B3C4D: Rua Nova 10', True),
    ('cliente perguntou o endereço da loja', False),       # sem verbo de mudança
    ('endereco de entrega: pedido E49C374A', False),       # idem
    ('número do pedido não localizado', False),
    ('cesta para 10 pessoas', False),
    ('duvida de frete para Moema', False),
    # Revisão 19/09/2026: venda EM CURSO (frete) nunca é exceção — o
    # enforcement anti-handoff-preguiçoso existe justamente pra isso.
    ('cliente mudou de endereço e quer saber o frete novo', False),
    ('cliente quer corrigir o endereço para calcular o frete', False),
    ('ajustar endereço para cotação de frete', False),
    ('cliente trocou de endereço, cotar frete de novo', False),
    ('dúvida de frete: cliente mudou o número da casa', False),
    # `número` solto casava telefone/cartão/quantidade
    ('cliente quer alterar o número de pães da cesta do pedido', False),
    ('cliente quer trocar o número do telefone de contato do pedido', False),
    ('cliente quer alterar o número do cartão', False),
    # `alter\w*` engolia "alternativa"
    ('alternativa de endereço para retirada do pedido', False),
    ('mudar quantidade e endereço de e-mail', False),
])
def test_handoff_excecao_correcao_de_endereco_e_estreita(motivo, esperado):
    from app.services.chatbot import _handoff_excecao
    assert _handoff_excecao({'motivo': motivo}) is esperado


def test_descricao_da_tool_transferir_cita_correcao_de_endereco():
    from app.services.chatbot import TOOLS
    desc = next(t for t in TOOLS if t['name'] == 'transferir_para_humano')['description']
    assert 'CORREÇÃO de endereço' in desc


# ── 3. consultar_pedido devolve o destino ─────────────────────────────────

def test_consultar_pedido_devolve_endereco_destinatario_e_modo(app):
    from app.extensions import db
    from app.models import PedidoOnline
    from app.services import bot_tools
    with app.app_context():
        p = PedidoOnline(codigo='TESTEJESS1', status='pago',
                         nome_cliente='JESSICA SANTOS',
                         telefone_cliente='11977776666',
                         email_cliente='jess@example.com',
                         modo_entrega='express',
                         endereco_entrega='Rua Gaspar Lourenço, 72, Vila Mariana, São Paulo, SP',
                         endereco_complemento='',
                         nome_destinatario=None,
                         subtotal=400, frete_valor=30, valor_total=430)
        db.session.add(p)
        db.session.commit()
        r = bot_tools.consultar_pedido('TESTEJESS1',
                                       telefone_contato='+55 11 97777-6666')
        assert r['modo_entrega'] == 'express'
        assert r['endereco_entrega'].startswith('Rua Gaspar Lourenço, 72')
        assert r['endereco_complemento'] is None
        assert r['nome_destinatario'] is None
        assert 'CORREÇÃO' in r['como_apresentar']
        # Sem autorização, nada disso vaza
        neg = bot_tools.consultar_pedido('TESTEJESS1', telefone_contato='11900000000')
        assert neg.get('erro') == 'autorizacao_necessaria'
        assert 'endereco_entrega' not in neg


# ── 4. Espera-humano em conversa PENDING ──────────────────────────────────

def test_caso_grave_em_conversa_pending_nao_diz_esperando_atendente(app):
    """Reprodução do veredito 7249: ALTA do vigia cria o incidente; o cron
    do espera-humano acha a conversa PENDING (bot respondendo). O texto e o
    motivo dizem isso — e a chave de contato vem preenchida."""
    from app.extensions import db
    from app.models import VigiaVeredito
    from app.services import atendimento_pendente, chatbot_vigia
    from app.utils import agora
    with app.app_context():
        alta = VigiaVeredito(criado_em=agora() - timedelta(minutes=4),
                             conv_id='2371', cliente='Jéssica Santos',
                             mensagem_cliente='[imagem enviada]',
                             bot_acao='responder', alerta=True,
                             gravidade='alta',
                             motivo_vigia='Bot respondeu com pedido de outra pessoa',
                             tools_usadas='["consultar_pedido"]')
        db.session.add(alta)
        db.session.commit()
        atendimento_pendente.registrar_alerta(alta)
        agora_epoch = time.time()
        historico = [
            {'role': 'user', 'content': 'Está tudo certo com o meu pedido?',
             'humano': False, 'created_at': agora_epoch - 120},
            {'role': 'assistant', 'content': 'Seu pedido E49C374A está confirmado',
             'humano': False, 'created_at': agora_epoch - 110},   # BOT, não humano
        ]
        with patch('app.services.chatbot_vigia._numero_destino',
                   return_value='5511999990000'), \
                patch('app.services.chatwoot.listar_conversas_paradas',
                      return_value=[]), \
                patch('app.services.chatwoot.consultar_conversa',
                      return_value={'id': 2371, 'status': 'pending',
                                    'meta': {'sender': {
                                        'phone_number': '+5511987654321'}}}), \
                patch('app.services.chatwoot.buscar_historico',
                      return_value=historico), \
                patch('app.services.chatwoot.enviar_mensagem') as contencao, \
                patch('app.services.zapi.enviar_texto',
                      return_value={'ok': True}) as envia:
            r = chatbot_vigia.alertar_clientes_esperando_humano(min_minutos=10)
        assert r == {'avaliadas': 1, 'enviadas': 1}
        msg = envia.call_args[0][1]
        assert 'Caso grave apontado pelo Vigia' in msg
        assert 'status pending' in msg
        assert 'esperando ATENDENTE' not in msg
        assert 'assumidas por humano' not in msg
        contencao.assert_not_called()          # o bot está no turno
        row = VigiaVeredito.query.filter(
            VigiaVeredito.conv_id == '2371',
            VigiaVeredito.mensagem_cliente.like('[ESPERA_HUMANO%')).one()
        assert 'pending' in row.motivo_vigia
        assert 'open' not in row.motivo_vigia
        assert 'c:5511987654321]' in row.mensagem_cliente


def test_candidata_da_tabela_carrega_status_e_telefone(app):
    from app.extensions import db
    from app.models import VigiaVeredito
    from app.services import atendimento_pendente
    from app.utils import agora
    with app.app_context():
        alta = VigiaVeredito(criado_em=agora() - timedelta(minutes=3),
                             conv_id='4242', cliente='Alguém',
                             mensagem_cliente='x', bot_acao='responder',
                             alerta=True, gravidade='alta', motivo_vigia='m')
        db.session.add(alta)
        db.session.commit()
        atendimento_pendente.registrar_alerta(alta)
        with patch('app.services.chatwoot.listar_conversas_paradas',
                   return_value=[]), \
                patch('app.services.chatwoot.consultar_conversa',
                      return_value={'id': 4242, 'status': 'pending',
                                    'meta': {'sender': {'identifier': '5511911112222'}}}):
            cands = atendimento_pendente.candidatos()
        assert len(cands) == 1
        assert cands[0]['status'] == 'pending'
        assert cands[0]['telefone'] == '5511911112222'
        assert cands[0]['minutos_paradas'] == 3


def test_conversa_open_mantem_texto_de_esperando_atendente(app):
    """Contrato original (12/06/2026) intacto quando a conversa É open."""
    from app.models import VigiaVeredito
    from app.services import chatbot_vigia
    with app.app_context(), \
            patch('app.services.chatbot_vigia._numero_destino',
                  return_value='5511999990000'), \
            patch('app.services.chatwoot.listar_conversas_paradas',
                  return_value=[{'id': 198, 'nome_contato': 'Mariana',
                                 'telefone': '+5511955554444',
                                 'minutos_paradas': 25}]), \
            patch('app.services.chatwoot.buscar_historico',
                  return_value=[{'role': 'assistant', 'content': 'um atendente vai te ajudar'},
                                {'role': 'user', 'content': 'Olá'}]), \
            patch('app.services.chatwoot.enviar_mensagem',
                  return_value={'ok': True}), \
            patch('app.services.zapi.enviar_texto',
                  return_value={'ok': True}) as envia:
        r = chatbot_vigia.alertar_clientes_esperando_humano()
        assert r['enviadas'] == 1
        assert 'esperando ATENDENTE' in envia.call_args[0][1]
        row = VigiaVeredito.query.filter(
            VigiaVeredito.conv_id == '198',
            VigiaVeredito.mensagem_cliente.like('[ESPERA_HUMANO%')).one()
        assert row.motivo_vigia == 'cliente esperando atendente em conversa open'
        assert 'c:5511955554444]' in row.mensagem_cliente


# ── 5. Auditor ────────────────────────────────────────────────────────────

def test_auditor_conta_conversas_com_alta_e_traz_hora_dos_pagos(app):
    from datetime import datetime as _dt
    from decimal import Decimal

    from app.extensions import db
    from app.models import PedidoOnline, VigiaVeredito
    from app.services.chatbot_auditor import _coletar_periodo, _resumo_comparativo
    from app.utils import hoje
    with app.app_context():
        d = hoje()
        base = _dt.combine(d, _dt.min.time())
        for hh, mm in ((9, 50), (9, 50), (9, 51)):
            db.session.add(VigiaVeredito(
                criado_em=base.replace(hour=hh, minute=mm), conv_id='2371',
                cliente='Jéssica', mensagem_cliente='x', bot_acao='responder',
                alerta=True, gravidade='alta',
                motivo_vigia='Bot respondeu com pedido de outra pessoa',
                tools_usadas='["consultar_pedido"]'))
        db.session.add(PedidoOnline(
            codigo='E49C374A', nome_cliente='JESSICA SANTOS',
            email_cliente='j@x.com', modo_entrega='express', status='pago',
            valor_total=Decimal('430.00'),
            criado_em=base.replace(hour=9, minute=48),
            pago_em=base.replace(hour=9, minute=49)))
        db.session.commit()
        dados = _coletar_periodo(base, base + timedelta(days=1))
        assert dados['gravidade_alta'] == 3
        assert dados['conversas_com_alta'] == 1
        assert {c['conv_id'] for c in dados['casos_alta']} == {'2371'}
        # Cada caso ALTA leva a HORA (com data — a janela das 07:00 cruza a
        # meia-noite): sem ela a regra "pagamento POSTERIOR" não tinha com
        # o que comparar.
        assert [c['hora'] for c in dados['casos_alta']] == [
            f'{d:%d/%m} 09:50', f'{d:%d/%m} 09:50', f'{d:%d/%m} 09:51']
        assert dados['funil_site']['pagos_detalhe'] == [
            {'codigo': 'E49C374A', 'pago_em': f'{d:%d/%m} 09:49',
             'valor': 430.0}]
        assert dados['funil_site']['pagos_detalhe_omitidos'] == 0
        # O comparativo (tendência) não carrega a lista de ontem, mas leva
        # a contagem de CONVERSAS com ALTA (senão "vs ontem" compararia
        # turnos).
        comp = _resumo_comparativo(dados)
        assert 'pagos_detalhe' not in comp['funil_site']
        assert 'pagos_detalhe_omitidos' not in comp['funil_site']
        assert comp['conversas_com_alta'] == 1


def test_pagos_detalhe_trunca_mantendo_os_mais_recentes_e_avisa(app):
    """Data especial (~100 pagos): o teto fica com os ÚLTIMOS pagamentos —
    os únicos que podem ser POSTERIORES a uma conversa — e diz quantos
    ficaram de fora (a lista não pode parecer completa)."""
    from datetime import datetime as _dt
    from decimal import Decimal

    from app.extensions import db
    from app.models import PedidoOnline
    from app.services.chatbot_auditor import _MAX_PAGOS_DETALHE, _funil_site
    from app.utils import hoje
    with app.app_context():
        d = hoje()
        base = _dt.combine(d, _dt.min.time())
        n = _MAX_PAGOS_DETALHE + 2
        for i in range(n):
            db.session.add(PedidoOnline(
                codigo=f'C{i:07d}', nome_cliente='X', email_cliente='x@x.com',
                modo_entrega='retirada', status='pago',
                valor_total=Decimal('10.00'),
                criado_em=base.replace(hour=8) + timedelta(minutes=i),
                pago_em=base.replace(hour=8) + timedelta(minutes=i)))
        db.session.commit()
        funil = _funil_site(base, base + timedelta(days=1))
        assert funil['pedidos_pagos'] == n
        assert funil['pagos_detalhe_omitidos'] == 2
        assert len(funil['pagos_detalhe']) == _MAX_PAGOS_DETALHE
        # Os dois mais ANTIGOS saem; o mais recente fica
        codigos = [p['codigo'] for p in funil['pagos_detalhe']]
        assert 'C0000000' not in codigos and 'C0000001' not in codigos
        assert codigos[-1] == f'C{n - 1:07d}'


def test_prompts_do_auditor_explicam_conv_id_e_pagamento_posterior():
    from app.services.chatbot_auditor import PROMPT_AUDITOR, PROMPT_AUDITOR_RESUMO
    for p in (PROMPT_AUDITOR, PROMPT_AUDITOR_RESUMO):
        assert 'conversas_com_alta' in p
        assert 'pagos_detalhe' in p
        assert 'pagos_detalhe_omitidos' in p
        assert 'POSTERIOR' in p
        assert 'venda fechada pelo bot' in p
