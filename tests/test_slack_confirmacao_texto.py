"""Teste do interceptor de confirmacao por texto no Slack bot.

Bug original: usuario respondia 'Confirmadissimo' (texto) em vez de clicar
no botao Confirmar. O bot chamava o Claude que alucinava 'registrado!'
sem realmente executar a tool. Fix: detectar texto de confirmacao e
acionar a SlackAcaoPendente como se fosse clique do botao.
"""
import json
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock


def test_texto_confirmacao_dispara_acao_pendente(app, admin_user):
    """Usuario tem SlackAcaoPendente e responde 'sim' → executor roda."""
    from app.extensions import db
    from app.models import SlackVinculo, SlackAcaoPendente
    from app.services.slack_bot import _tentar_confirmar_por_texto

    # Vincula admin ao slack_uid
    db.session.add(SlackVinculo(slack_user_id='U123', usuario_id=admin_user.id, ativo=True))
    # Cria acao pendente
    acao = SlackAcaoPendente(
        token='tok-test-123',
        slack_user_id='U123',
        slack_channel_id='C456',
        slack_message_ts='1000.000',
        tipo_acao='registrar_desperdicio',
        params_json=json.dumps({'item_nome': 'pao', 'quantidade': 1, 'loja_id': 1}),
        usuario_id=admin_user.id,
    )
    db.session.add(acao)
    db.session.commit()

    # Mocka processar_interacao_botao pra nao chamar copilot real
    with patch('app.services.slack_bot.processar_interacao_botao') as mock_botao:
        interceptado = _tentar_confirmar_por_texto('Confirmadíssimo', 'U123', 'C456')

    assert interceptado is True
    mock_botao.assert_called_once()
    args = mock_botao.call_args[0]
    assert args[0] == 'copilot_confirmar'
    assert args[1] == 'tok-test-123'


def test_texto_cancelamento_dispara_cancelar(app, admin_user):
    """'cancela' / 'esquece' / 'nao' → action_id=cancelar."""
    from app.extensions import db
    from app.models import SlackAcaoPendente
    from app.services.slack_bot import _tentar_confirmar_por_texto

    acao = SlackAcaoPendente(
        token='tok-cancel',
        slack_user_id='U999', slack_channel_id='C999',
        slack_message_ts='2000.000',
        tipo_acao='criar_pedido',
        params_json=json.dumps({}),
        usuario_id=admin_user.id,
    )
    db.session.add(acao)
    db.session.commit()

    with patch('app.services.slack_bot.processar_interacao_botao') as mock:
        ok = _tentar_confirmar_por_texto('esquece', 'U999', 'C999')
    assert ok is True
    assert mock.call_args[0][0] == 'copilot_cancelar'


def test_sem_acao_pendente_nao_intercepta(app, admin_user):
    """Sem acao pendente, texto 'sim' nao intercepta (deixa Claude responder)."""
    from app.services.slack_bot import _tentar_confirmar_por_texto
    with patch('app.services.slack_bot.processar_interacao_botao') as mock:
        ok = _tentar_confirmar_por_texto('sim', 'U_NAO_EXISTE', 'C_NAO_EXISTE')
    assert ok is False
    mock.assert_not_called()


def test_acao_velha_nao_intercepta(app, admin_user):
    """Acao criada ha > 10min nao deve ser usada."""
    from app.extensions import db
    from app.models import SlackAcaoPendente
    from app.services.slack_bot import _tentar_confirmar_por_texto

    acao = SlackAcaoPendente(
        token='tok-velha',
        slack_user_id='U_VELHA', slack_channel_id='C_VELHA',
        slack_message_ts='1.000',
        tipo_acao='registrar_desperdicio',
        params_json='{}',
        usuario_id=admin_user.id,
        criado_em=datetime.now() - timedelta(minutes=15),
    )
    db.session.add(acao)
    db.session.commit()

    with patch('app.services.slack_bot.processar_interacao_botao') as mock:
        ok = _tentar_confirmar_por_texto('sim', 'U_VELHA', 'C_VELHA')
    assert ok is False
    mock.assert_not_called()


def test_texto_qualquer_nao_intercepta(app, admin_user):
    """Texto que nao parece confirmacao deve seguir pro Claude."""
    from app.extensions import db
    from app.models import SlackAcaoPendente
    from app.services.slack_bot import _tentar_confirmar_por_texto

    acao = SlackAcaoPendente(
        token='tok-x',
        slack_user_id='U_X', slack_channel_id='C_X',
        tipo_acao='registrar_desperdicio',
        params_json='{}',
        usuario_id=admin_user.id,
    )
    db.session.add(acao)
    db.session.commit()

    with patch('app.services.slack_bot.processar_interacao_botao') as mock:
        # textos que nao sao confirmacao
        for t in ['quanto', 'mostra os pedidos', 'cria pedido', 'oi', 'tudo bem?']:
            ok = _tentar_confirmar_por_texto(t, 'U_X', 'C_X')
            assert ok is False, f'nao devia interceptar: {t}'
    mock.assert_not_called()
