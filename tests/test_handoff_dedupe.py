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


def test_vassoura_nao_transfere_de_novo(app):
    """Conversa já transferida há pouco: a vassoura responde 'já está com a
    equipe' em vez de um 2º 'vou te passar' (mesma regra do webhook)."""
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
        texto_enviado = env.call_args[0][1]
        assert texto_enviado == chatbot.TEXTO_HANDOFF_REPETIDO
        st.assert_called_once_with(777, 'open')   # fila garantida, sem 2º handoff


def test_consultar_pedido_online_vem_com_valores_rotulados(app):
    from app.extensions import db
    from app.models import PedidoOnline, PedidoOnlineItem
    from app.services import bot_tools
    with app.app_context():
        p = PedidoOnline(codigo='TESTELBL1', status='pago',
                         nome_cliente='Simone', telefone_cliente='11988887777',
                         frete_valor=10, valor_total=148)
        db.session.add(p)
        db.session.flush()
        db.session.add(PedidoOnlineItem(pedido_id=p.id, nome='Sourdough Integral',
                                        quantidade=4, preco_unitario=34.50))
        db.session.commit()
        r = bot_tools.consultar_pedido('TESTELBL1',
                                       telefone_contato='+55 11 98888-7777')
        assert r['total'] == 148.0
        assert r['frete'] == 10.0
        assert r['subtotal_itens'] == 138.0
        assert r['itens'][0]['preco_unit'] == 34.5
        assert 'rotule' in r['como_apresentar']
