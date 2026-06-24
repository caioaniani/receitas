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
    import time

    from app.models import ContaPagar
    from app.services import conta_pagar_slack
    p1, p2, p3, p4 = _patches()
    recente = str(time.time() - 86400)  # 1 dia atras (dentro dos 30)
    msgs = [
        {'ts': recente, 'user': 'U1', 'files': [
            {'id': 'H1', 'mimetype': 'image/jpeg', 'name': 'a.jpg',
             'url_private_download': 'u', 'size': 10}]},
        {'ts': recente, 'user': 'U2', 'files': [
            {'id': 'H2', 'mimetype': 'image/jpeg', 'name': 'b.jpg',
             'url_private_download': 'u', 'size': 10}]},
        {'ts': recente, 'user': 'U3'},  # sem files — ignora
    ]
    with app.app_context(), p1, p2, p3, p4:
        app.config['SLACK_CANAIS_NF'] = 'C_NF'
        with patch('app.services.slack.historico_canal',
                   return_value=(msgs, None)):
            n = conta_pagar_slack.importar_historico(app, dias=30)
        assert n == 2
        assert ContaPagar.query.count() == 2


def test_importar_historico_para_em_30_dias(app):
    """REGRESSAO: msgs mais antigas que `dias` sao ignoradas mesmo se a API
    devolver (paginacao por cursor as vezes ignora `oldest`)."""
    import time

    from app.models import ContaPagar
    from app.services import conta_pagar_slack
    p1, p2, p3, p4 = _patches()
    antiga = str(time.time() - 60 * 86400)  # 60 dias atras (fora dos 30)
    msgs = [
        {'ts': antiga, 'user': 'U1', 'files': [
            {'id': 'Hvelha', 'mimetype': 'image/jpeg', 'name': 'a.jpg',
             'url_private_download': 'u', 'size': 10}]},
    ]
    with app.app_context(), p1, p2, p3, p4:
        app.config['SLACK_CANAIS_NF'] = 'C_NF'
        with patch('app.services.slack.historico_canal',
                   return_value=(msgs, None)):
            n = conta_pagar_slack.importar_historico(app, dias=30)
        assert n == 0
        assert ContaPagar.query.count() == 0


def test_importar_historico_filtra_por_canal(app):
    """canais=[...] varre so os canais pedidos (e so os que estao em
    SLACK_CANAIS_NF); None varre todos os configurados."""
    from app.services import conta_pagar_slack
    with app.app_context():
        app.config['SLACK_CANAIS_NF'] = 'C_IND,C_LOJA'
        chamados = []

        def fake_hist(canal, oldest=None, cursor=None):
            chamados.append(canal)
            return [], None

        with patch('app.services.slack.historico_canal', side_effect=fake_hist):
            conta_pagar_slack.importar_historico(app, dias=30, canais=['C_IND'])
            assert chamados == ['C_IND']  # so a industria

            chamados.clear()
            conta_pagar_slack.importar_historico(app, dias=30, canais=['C_FORA'])
            assert chamados == []  # canal fora da config e ignorado

            chamados.clear()
            conta_pagar_slack.importar_historico(app, dias=30)
            assert sorted(chamados) == ['C_IND', 'C_LOJA']  # None = todos


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


def test_parse_vencimento_br_prioriza_texto_cru():
    """Data DD/MM/AAAA do documento manda — mesmo se a IA inverter no ISO."""
    from datetime import date

    from app.services import conta_pagar_slack as cps

    # IA inverteu (08/05 virou 2026-08-05), mas o texto cru salva o dia certo
    d = cps._parse_vencimento({'vencimento': '2026-08-05',
                               'vencimento_texto': '08/05/2026'})
    assert d == date(2026, 5, 8)  # 8 de maio, nao 5 de agosto

    # Dia > 12: nao ha ambiguidade
    d2 = cps._parse_vencimento({'vencimento_texto': '25/12/2026'})
    assert d2 == date(2026, 12, 25)

    # Sem texto cru: cai no ISO
    d3 = cps._parse_vencimento({'vencimento': '2026-03-10'})
    assert d3 == date(2026, 3, 10)

    # Ano com 2 digitos
    d4 = cps._parse_vencimento({'vencimento_texto': '01/02/26'})
    assert d4 == date(2026, 2, 1)

    # Nada
    assert cps._parse_vencimento({}) is None


def test_processar_usa_vencimento_br(app):
    """Captura via Slack respeita DD/MM/AAAA do documento."""
    from datetime import date

    from app.models import ContaPagar
    from app.services import conta_pagar_slack
    p1, p2, p3, _ = _patches()
    p_ia = patch('app.services.conta_pagar_ia.extrair_documento',
                 return_value={'tipo_documento': 'boleto', 'fornecedor': 'X',
                               'valor_total': 10.0, 'codigo_barras': '1',
                               'vencimento': '2026-08-05',  # IA inverteu
                               'vencimento_texto': '08/05/2026'})
    with app.app_context(), p1, p2, p3, p_ia:
        conta_pagar_slack.processar(_evento(file_id='Fvenc'))
        c = ContaPagar.query.filter_by(slack_file_id='Fvenc').first()
        assert c.vencimento == date(2026, 5, 8)


def test_webhook_deixa_passar_canal_nf(app):
    """REGRESSAO: /slack/events nao pode barrar canal de NF no filtro de
    canal (ele nao esta em SLACK_CANAIS_PERMITIDOS, mas em SLACK_CANAIS_NF)."""
    import hashlib
    import hmac
    import json
    import time

    secret = 'sig-secret-teste'
    app.config['SLACK_SIGNING_SECRET'] = secret
    app.config['SLACK_CANAIS_PERMITIDOS'] = ''       # canal NF NAO esta aqui
    app.config['SLACK_CANAIS_NF'] = 'C_NF'

    body = json.dumps({
        'type': 'event_callback',
        'event_id': 'Ev_nf_teste_1',
        'event': {
            'type': 'message', 'subtype': 'file_share',
            'channel': 'C_NF', 'channel_type': 'channel', 'user': 'U1',
            'ts': '123.45',
            'files': [{'id': 'Fweb', 'mimetype': 'image/jpeg'}],
        },
    })
    ts = str(int(time.time()))
    sig = 'v0=' + hmac.new(secret.encode(), f'v0:{ts}:{body}'.encode(),
                           hashlib.sha256).hexdigest()

    c = app.test_client()
    with patch('app.services.slack_bot.disparar_evento') as disparar:
        r = c.post('/slack/events', data=body,
                   headers={'X-Slack-Request-Timestamp': ts,
                            'X-Slack-Signature': sig,
                            'Content-Type': 'application/json'})
    assert r.status_code == 200
    disparar.assert_called_once()  # passou pelo filtro
