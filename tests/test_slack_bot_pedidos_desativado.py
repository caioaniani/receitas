"""Bot de pedidos do Slack DESATIVADO (decisao do dono 28/06/2026).

Pedidos nao sao mais feitos pelo Slack. O copilot (DM/@mention) para de
responder e avisa uma vez; a captura de NF/boleto (Contas a Pagar) e os
posts automaticos de saida continuam. Reativavel via SLACK_BOT_PEDIDOS_ATIVO=1.
"""
import os
from unittest.mock import patch


def _evento_dm(text='cria um pedido', user='U500', channel='D500'):
    return {'user': user, 'channel': channel, 'text': text,
            'channel_type': 'im', 'type': 'message'}


def test_flag_default_off_e_reativavel():
    from app.services.slack_bot import _bot_pedidos_ativo
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop('SLACK_BOT_PEDIDOS_ATIVO', None)
        assert _bot_pedidos_ativo() is False          # default: desativado
    with patch.dict(os.environ, {'SLACK_BOT_PEDIDOS_ATIVO': '1'}):
        assert _bot_pedidos_ativo() is True            # reativado por env
    with patch.dict(os.environ, {'SLACK_BOT_PEDIDOS_ATIVO': '0'}):
        assert _bot_pedidos_ativo() is False


def test_desativado_nao_chama_copilot_e_avisa(app, admin_user):
    from app.extensions import db
    from app.models import SlackVinculo
    from app.services import slack_bot
    with app.app_context():
        db.session.add(SlackVinculo(slack_user_id='U500',
                                    usuario_id=admin_user.id, ativo=True))
        db.session.commit()
        with patch.dict(os.environ, {'SLACK_BOT_PEDIDOS_ATIVO': '0'}), \
                patch('app.services.copilot.interpretar') as interp, \
                patch('app.services.slack.post_message') as post:
            slack_bot.processar_evento_mensagem(_evento_dm())
    interp.assert_not_called()
    post.assert_called_once()
    assert 'desativado' in post.call_args.kwargs['text'].lower()


def test_captura_de_nf_continua_mesmo_com_bot_desativado(app, admin_user):
    """Canal de NF retorna antes do gate: a captura de NF/boleto NAO depende
    do bot de pedidos estar ativo."""
    from app.services import slack_bot
    evento = {'user': 'U9', 'channel': 'CNF', 'channel_type': 'channel',
              'type': 'message', 'files': [{'mimetype': 'image/jpeg'}]}
    with app.app_context():
        with patch.dict(os.environ, {'SLACK_BOT_PEDIDOS_ATIVO': '0'}), \
                patch('app.services.conta_pagar_slack.canal_de_nf',
                      return_value=True), \
                patch('app.services.conta_pagar_slack.processar') as proc, \
                patch('app.services.copilot.interpretar') as interp, \
                patch('app.services.slack.post_message') as post:
            slack_bot.processar_evento_mensagem(evento)
    proc.assert_called_once()       # NF processada
    interp.assert_not_called()      # copilot nao tocado
    post.assert_not_called()        # bot nao responde em canal de NF
