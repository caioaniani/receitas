"""Testa a captura de NF/boleto via Slack → ContaPagar."""
from unittest.mock import patch


def _evento(channel='C_NF', file_id='F1', mime='image/jpeg'):
    return {
        'channel': channel, 'user': 'U1', 'ts': '1716500000.0001',
        'files': [{'id': file_id, 'mimetype': mime, 'name': 'nf.jpg',
                   'url_private_download': 'https://files.slack/x', 'size': 1000}],
    }


def _patches():
    """Mocks de download Slack + upload Dropbox + extracao IA."""
    return (
        patch('app.services.slack.baixar_arquivo',
              return_value={'bytes': b'fakejpeg', 'mimetype': 'image/jpeg', 'name': 'nf.jpg'}),
        patch('app.services.slack.info_usuario',
              return_value={'real_name': 'Joao da Loja'}),
        patch('app.services.dropbox_storage.upload_publico',
              return_value={'url': 'https://dropbox/x?raw=1',
                            'storage_path': '/contas-pagar/x.jpg', 'tamanho': 1000}),
        patch('app.services.conta_pagar_ia.extrair_documento',
              return_value={'tipo_documento': 'nota_fiscal', 'fornecedor': 'Moinho X',
                            'valor_total': 250.0, 'nf_numero': '123',
                            'modelo_usado': 'claude-sonnet-4-6'}),
    )


def test_canal_de_nf(app):
    from app.services import conta_pagar_slack
    with app.app_context():
        app.config['SLACK_CANAIS_NF'] = 'C_NF,C_OUTRO'
        assert conta_pagar_slack.canal_de_nf('C_NF') is True
        assert conta_pagar_slack.canal_de_nf('C_FORA') is False
        app.config['SLACK_CANAIS_NF'] = ''
        assert conta_pagar_slack.canal_de_nf('C_NF') is False


def test_processar_cria_conta(app):
    from app.extensions import db
    from app.models import ContaPagar
    from app.services import conta_pagar_slack
    p1, p2, p3, p4 = _patches()
    with app.app_context(), p1, p2, p3, p4:
        n = conta_pagar_slack.processar(_evento())
        assert n == 1
        c = ContaPagar.query.filter_by(slack_file_id='F1').first()
        assert c is not None
        assert c.fornecedor_nome == 'Moinho X'
        assert float(c.valor_total) == 250.0
        assert c.imagem_url == 'https://dropbox/x?raw=1'
        assert c.origem_canal == 'C_NF'
        assert c.enviado_por == 'Joao da Loja'
        assert db.session.query(ContaPagar).count() == 1


def test_idempotente_nao_duplica(app):
    from app.models import ContaPagar
    from app.services import conta_pagar_slack
    p1, p2, p3, p4 = _patches()
    with app.app_context(), p1, p2, p3, p4:
        conta_pagar_slack.processar(_evento(file_id='F9'))
        conta_pagar_slack.processar(_evento(file_id='F9'))  # 2x mesmo file
        assert ContaPagar.query.filter_by(slack_file_id='F9').count() == 1


def test_ignora_arquivo_nao_imagem(app):
    from app.models import ContaPagar
    from app.services import conta_pagar_slack
    p1, p2, p3, p4 = _patches()
    with app.app_context(), p1, p2, p3, p4:
        n = conta_pagar_slack.processar(_evento(mime='text/plain'))
        assert n == 0
        assert ContaPagar.query.count() == 0


def test_pdf_e_aceito(app):
    from app.models import ContaPagar
    from app.services import conta_pagar_slack
    p1, p2, p3, p4 = _patches()
    with app.app_context(), p1, p2, p3, p4:
        n = conta_pagar_slack.processar(_evento(file_id='Fpdf', mime='application/pdf'))
        assert n == 1
        assert ContaPagar.query.filter_by(slack_file_id='Fpdf').count() == 1


def test_ia_falha_ainda_cria_conta_com_doc(app):
    """Se a IA falhar, a conta eh criada (documento no Dropbox preservado)."""
    from app.models import ContaPagar
    from app.services import conta_pagar_slack
    p1, p2, p3, _ = _patches()
    p_ia = patch('app.services.conta_pagar_ia.extrair_documento',
                 return_value={'erro': 'json_invalido'})
    with app.app_context(), p1, p2, p3, p_ia:
        n = conta_pagar_slack.processar(_evento(file_id='Ferr'))
        assert n == 1
        c = ContaPagar.query.filter_by(slack_file_id='Ferr').first()
        assert c.imagem_url == 'https://dropbox/x?raw=1'  # doc salvo
        assert c.tipo_documento == 'desconhecido'


def test_importar_historico(app):
    """Varre 2 mensagens com files do historico → cria 2 contas."""
    from app.models import ContaPagar
    from app.services import conta_pagar_slack
    p1, p2, p3, p4 = _patches()
    msgs = [
        {'ts': '100.1', 'user': 'U1', 'files': [
            {'id': 'H1', 'mimetype': 'image/jpeg', 'name': 'a.jpg',
             'url_private_download': 'u', 'size': 10}]},
        {'ts': '100.2', 'user': 'U2', 'files': [
            {'id': 'H2', 'mimetype': 'image/jpeg', 'name': 'b.jpg',
             'url_private_download': 'u', 'size': 10}]},
        {'ts': '100.3', 'user': 'U3'},  # sem files — ignora
    ]
    with app.app_context(), p1, p2, p3, p4:
        app.config['SLACK_CANAIS_NF'] = 'C_NF'
        with patch('app.services.slack.historico_canal',
                   return_value=(msgs, None)):
            n = conta_pagar_slack.importar_historico(app, dias=30)
        assert n == 2
        assert ContaPagar.query.count() == 2


def test_slack_bot_intercepta_canal_nf(app):
    """slack_bot roteia canal de NF pro handler e NAO chama copilot."""
    from app.services import slack_bot
    with app.app_context():
        app.config['SLACK_CANAIS_NF'] = 'C_NF'
        with patch('app.services.conta_pagar_slack.processar') as proc, \
             patch('app.services.copilot.interpretar') as interp:
            slack_bot.processar_evento_mensagem(_evento(channel='C_NF'))
            proc.assert_called_once()
            interp.assert_not_called()
