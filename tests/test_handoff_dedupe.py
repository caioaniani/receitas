"""Dedupe de handoff + valores rotulados (auditor 06/07/2026, caso Simone).

Dois problemas na mesma conversa: (1) o bot transferiu duas vezes o mesmo
assunto ("fatiamento" das mensagens da cliente); (2) mostrou R$138 e R$148
sem rótulos e o vigia/cliente leram como contradição (era subtotal + frete,
4×34,50+10 — a conta estava certa).
"""
from datetime import timedelta
from unittest.mock import patch

from app.utils import agora


def _marcar_handoff(app, conv_id, minutos_atras=5):
    from app.services import chatbot
    with app.app_context():
        chatbot.salvar_historico(conv_id, [
            {'role': 'user', 'content': 'quero confirmar meu pedido'},
        ], 'Já te passo para um atendente.', handoff=True)
        if minutos_atras:
            import json as _json

            from app.extensions import db
            from app.models import ChatbotConversa
            conv = ChatbotConversa.query.filter_by(conv_id=str(conv_id)).one()
            msgs = _json.loads(conv.mensagens_json)
            ts = (agora() - timedelta(minutes=minutos_atras)).isoformat()
            msgs[-1]['handoff_em'] = ts
            conv.mensagens_json = _json.dumps(msgs, ensure_ascii=False)
            db.session.commit()


def test_handoff_recente_detecta_janela(app):
    from app.services import chatbot
    with app.app_context():
        _marcar_handoff(app, 'conv-dd-1', minutos_atras=5)
        assert chatbot.handoff_recente('conv-dd-1') is True
        # fora da janela (91 min > HANDOFF_DEDUP_MIN=90) → pode transferir
        _marcar_handoff(app, 'conv-dd-2', minutos_atras=91)
        assert chatbot.handoff_recente('conv-dd-2') is False
        # conversa sem handoff nenhum
        assert chatbot.handoff_recente('conv-dd-inexistente') is False


def test_marcador_sobrevive_a_novo_turno(app):
    """salvar_historico reconstrói o JSON a cada turno — o handoff_em de
    turnos antigos tem que ser preservado (senão o dedupe morre no turno
    seguinte)."""
    from app.services import chatbot
    with app.app_context():
        _marcar_handoff(app, 'conv-dd-3', minutos_atras=5)
        hist = chatbot.carregar_historico('conv-dd-3')
        hist.append({'role': 'user', 'content': 'e aí, alguém me responde?'})
        chatbot.salvar_historico('conv-dd-3', hist, 'Já estão com seu caso!')
        assert chatbot.handoff_recente('conv-dd-3') is True


def test_vassoura_nao_transfere_nem_responde_de_novo(app):
    """Conversa já transferida há pouco fica em silêncio e segue na fila."""
    from app.services import chatbot
    with app.app_context():
        app.config['CHATWOOT_URL'] = 'https://x.example'
        _marcar_handoff(app, '777', minutos_atras=10)
        paradas = [{'id': 777, 'minutos_paradas': 30}]
        historico = [{'role': 'user', 'content': 'cadê vocês??'}]
        with patch('app.services.chatwoot.listar_conversas_paradas',
                   return_value=paradas), \
                patch('app.services.chatwoot.buscar_historico',
                      return_value=historico), \
                patch('app.services.chatwoot.enviar_mensagem',
                      return_value={'ok': True}) as env, \
                patch('app.services.chatwoot.definir_status') as st, \
                patch('app.services.chatbot.responder',
                      return_value={'acao': 'handoff',
                                    'texto': 'Vou te passar pra equipe.',
                                    'motivo': 'cliente insiste'}):
            chatbot.varrer_pendentes_sem_resposta()
        env.assert_not_called()
        assert chatbot.TEXTO_HANDOFF_REPETIDO == ''
        st.assert_called_once_with(777, 'open')   # fila garantida, sem 2º handoff


def test_consultar_pedido_online_vem_com_valores_rotulados(app):
    from app.extensions import db
    from app.models import PedidoOnline, PedidoOnlineItem
    from app.services import bot_tools
    with app.app_context():
        p = PedidoOnline(codigo='TESTELBL1', status='pago',
                         nome_cliente='Simone', telefone_cliente='11988887777',
                         email_cliente='simone@example.com',
                         modo_entrega='agendada',
                         subtotal=138, frete_valor=10, valor_total=148)
        db.session.add(p)
        db.session.flush()
        db.session.add(PedidoOnlineItem(pedido_id=p.id, kind='receita',
                                        nome='Sourdough Integral',
                                        quantidade=4, preco_unitario=34.50))
        db.session.commit()
        r = bot_tools.consultar_pedido('TESTELBL1',
                                       telefone_contato='+55 11 98888-7777')
        assert r['total'] == 148.0
        assert r['frete'] == 10.0
        assert r['subtotal_itens'] == 138.0
        assert r['itens'][0]['preco_unit'] == 34.5
        assert 'rotule' in r['como_apresentar']


def test_consultar_pedido_traz_link_e_rastreio_sem_horario(app):
    """08/08/2026 (dono, véspera do Dia dos Pais): a consulta autorizada
    traz o link fixo da página do pedido + o rastreio ao vivo — em rota, a
    POSIÇÃO (parada/faltam) SEM horário estimado (o ETA foi removido)."""
    from app.extensions import db
    from app.models import AtribuicaoEntrega, Driver, PedidoOnline, RotaInicio
    from app.services import bot_tools
    from app.utils import agora, hoje
    with app.app_context():
        p = PedidoOnline(codigo='TESTERAS1', status='em_preparo',
                         nome_cliente='Bia', telefone_cliente='11966665555',
                         email_cliente='bia@example.com',
                         modo_entrega='agendada', data_entrega=hoje(),
                         subtotal=90, frete_valor=0, valor_total=90)
        d = Driver(nome='Motorista Rastreio', ativo=True, token='tok-ras-123')
        db.session.add_all([p, d])
        db.session.flush()
        db.session.add(AtribuicaoEntrega(pedido_code='TESTERAS1',
                                         driver_id=d.id, data_entrega=hoje(),
                                         ordem=2, status='pendente'))
        db.session.add(RotaInicio(driver_id=d.id, data=hoje(),
                                  iniciado_em=agora(), emails_em=agora()))
        db.session.commit()
        r = bot_tools.consultar_pedido('TESTERAS1',
                                       telefone_contato='11966665555')
        assert r['link_acompanhamento'].endswith('/loja/pedido/TESTERAS1')
        assert r['rastreio']['fase'] == 'a_caminho'
        assert r['rastreio']['parada'] == 1
        assert 'eta' not in r['rastreio']       # decisão do dono: sem horário
        assert 'NUNCA prometa horário' in r['como_apresentar']


def test_consultar_pedido_cancelado_ou_retirada_sem_rastreio(app):
    """Gate canônico da página (achado de revisão 08/08/2026): pedido
    CANCELADO com atribuição viva na rota NÃO pode sair "a_caminho" ao lado
    do status oficial — o bot ditaria posição de entrega cancelada. Retirada
    idem (não há entrega)."""
    from app.extensions import db
    from app.models import AtribuicaoEntrega, Driver, PedidoOnline
    from app.services import bot_tools
    from app.utils import hoje
    with app.app_context():
        canc = PedidoOnline(codigo='TESTECANC1', status='cancelado',
                            nome_cliente='Ca', telefone_cliente='11955554444',
                            email_cliente='ca@example.com',
                            modo_entrega='agendada', data_entrega=hoje(),
                            subtotal=50, frete_valor=0, valor_total=50)
        ret = PedidoOnline(codigo='TESTERET1', status='pago',
                           nome_cliente='Re', telefone_cliente='11944443333',
                           email_cliente='re@example.com',
                           modo_entrega='retirada', data_entrega=hoje(),
                           subtotal=60, frete_valor=0, valor_total=60)
        d = Driver(nome='Motorista Canc', ativo=True, token='tok-canc-123')
        db.session.add_all([canc, ret, d])
        db.session.flush()
        db.session.add(AtribuicaoEntrega(pedido_code='TESTECANC1',
                                         driver_id=d.id, data_entrega=hoje(),
                                         ordem=1, status='pendente'))
        db.session.commit()
        r1 = bot_tools.consultar_pedido('TESTECANC1',
                                        telefone_contato='11955554444')
        assert r1['rastreio'] is None           # cancelado nunca "a caminho"
        r2 = bot_tools.consultar_pedido('TESTERET1',
                                        telefone_contato='11944443333')
        assert r2['rastreio'] is None           # retirada não tem entrega


# ── Auditor 06/07 (parte 2): cartinha sem handoff ───────────────────────

def test_consultar_pedido_devolve_cartinha(app):
    """Cliente autenticado (telefone bate) vê a própria cartinha — a
    confirmação pós-compra que antes virava handoff (auditor 06/07)."""
    from app.extensions import db
    from app.models import PedidoOnline
    from app.services import bot_tools
    with app.app_context():
        p = PedidoOnline(codigo='TESTECART1', status='pago',
                         nome_cliente='Ana', telefone_cliente='11977776666',
                         email_cliente='ana@example.com',
                         modo_entrega='agendada',
                         subtotal=100, frete_valor=0, valor_total=100,
                         cartinha='Feliz aniversário, vó!')
        db.session.add(p)
        db.session.commit()
        r = bot_tools.consultar_pedido('TESTECART1',
                                       telefone_contato='11977776666')
        assert r['cartinha'] == 'Feliz aniversário, vó!'


def test_cartinha_nao_e_mais_excecao_de_handoff(app):
    """'cartinha' saiu das exceções do enforcement (06/07): transferir por
    cartinha SEM consultar nada leva a recusa 1x (a tool resolve). As
    exceções humanas (alergia, reclamação, pedido de humano) continuam."""
    from app.services.chatbot import _handoff_excecao
    assert _handoff_excecao({'motivo': 'cliente quer conferir a cartinha'}) \
        is False
    assert _handoff_excecao({'motivo': 'cliente relata alergia'}) is True
    assert _handoff_excecao({'motivo': 'cliente pediu atendente humano'}) \
        is True
    assert _handoff_excecao({'motivo': 'pedido de estorno'}) is True
