"""Retirada informada ao bot não pode virar outra entrada de retorno."""
import base64
import json
from datetime import timedelta
from unittest.mock import Mock

import pytest

from app.extensions import db
from app.models import (
    Desperdicio,
    EstoqueLoja,
    Loja,
    MovEstoqueLoja,
    Receita,
    RetiradaSobra,
    SlackAcaoPendente,
    SlackVinculo,
    Usuario,
)
from app.services import copilot, slack_bot
from app.services.slack_sobras import ACAO_NOVA_SOBRA, ACAO_PREPARAR_RETIRADA
from app.utils import agora, hoje


@pytest.fixture
def cenario(app, admin_user, monkeypatch):
    loja = Loja(nome='Ribeiro do Vale', ativa=True)
    outra = Loja(nome='Anesio Pinto Rosa', ativa=True)
    retorno = Receita(nome='Croissant Tradicional - Retorno', rendimento_qtd=1,
                      rendimento_unidade='un', peso_base=100)
    kelvin = Usuario(nome='Kelvin', login='kelvin', papel='gerente')
    kelvin.set_senha('senha-exclusiva-do-teste')
    db.session.add_all([loja, outra, retorno, kelvin])
    db.session.flush()
    fresco = Receita(nome='Croissant Tradicional', rendimento_qtd=1,
                     rendimento_unidade='un', peso_base=100,
                     reaproveitavel=True, retorno_receita_id=retorno.id)
    db.session.add(fresco)
    db.session.flush()
    saldo = EstoqueLoja(loja_id=loja.id, receita_id=retorno.id, quantidade=200)
    saldo_fresco = EstoqueLoja(loja_id=loja.id, receita_id=fresco.id, quantidade=400)
    saldo_outra = EstoqueLoja(loja_id=outra.id, receita_id=retorno.id, quantidade=77)
    # A sobra já foi registrada por OUTRA pessoa e em OUTRO dia.
    anterior = Desperdicio(loja_id=loja.id, receita_id=fresco.id, quantidade=185,
                          motivo='nao_vendeu', data=hoje() - timedelta(days=1),
                          criado_por_id=admin_user.id)
    db.session.add_all([saldo, saldo_fresco, saldo_outra, anterior,
                        SlackVinculo(slack_user_id='UK', usuario_id=kelvin.id, ativo=True)])
    db.session.commit()
    post = Mock(side_effect=lambda *a, **kw: {'ok': True, 'ts': f'2000.{post.call_count}'})
    update = Mock(return_value={'ok': True})
    monkeypatch.setattr('app.services.slack.post_message', post)
    monkeypatch.setattr('app.services.slack.update_message', update)
    monkeypatch.setattr('app.services.slack.baixar_arquivo', lambda f: {
        'bytes': b'foto-teste', 'mimetype': 'image/jpeg'})
    monkeypatch.setattr(slack_bot, '_foto_recente_do_canal', lambda *a: None)
    monkeypatch.setattr('app.services.dropbox_storage.upload_publico',
                        lambda *a, **kw: {'url': 'https://example.test/foto.jpg'})
    return loja, fresco, retorno, kelvin, saldo, saldo_fresco, saldo_outra, update


def _proposta(cenario, monkeypatch, lote=False):
    loja, fresco, retorno, kelvin, *_ = cenario
    params = {'loja_id': loja.id, 'motivo': 'nao_vendeu'}
    if lote:
        tipo = 'registrar_desperdicio_lote'
        params['itens'] = [{'nome': fresco.nome, 'quantidade': 185}]
        params = copilot._enriquecer_registrar_desperdicio_lote(params, kelvin)
    else:
        tipo = 'registrar_desperdicio'
        params.update(item_nome=fresco.nome, quantidade=185)
        params = copilot._enriquecer_registrar_desperdicio(params, kelvin)
    # Reprodução: o modelo ESCOLHE ERRADO apesar de a mensagem pedir retirada.
    monkeypatch.setattr(copilot, 'interpretar', lambda *a, **kw: {
        'tipo': tipo, 'params': params, 'requer_aprovacao': True,
        'explicacao': 'Vou registrar as sobras.'})
    slack_bot.processar_evento_mensagem({
        'user': 'UK', 'channel': 'C1', 'channel_type': 'im', 'ts': '1000.1',
        'text': '185 croissants para o motorista retirar da Ribeiro do Vale',
        'files': [{'mimetype': 'image/jpeg'}],
    })
    return SlackAcaoPendente.query.one()


def _clicar(acao, action):
    slack_bot.processar_interacao_botao(action, acao.token, 'UK', 'C1', acao.slack_message_ts)


@pytest.mark.parametrize('lote', [False, True])
def test_modelo_erra_mas_retirada_nao_soma_retorno(cenario, monkeypatch, lote):
    loja, fresco, retorno, kelvin, saldo, saldo_fresco, saldo_outra, update = cenario
    acao = _proposta(cenario, monkeypatch, lote)
    params = json.loads(acao.params_json)
    assert params['_origem_slack']['ts'] == '1000.1'
    assert params['_opcoes_sobra']['conversoes'][0]['saldo_retorno'] == 200
    assert params['imagens'][0]['base64'] == base64.b64encode(b'foto-teste').decode()
    # Nem a confirmação genérica nem 'sim' podem fazer uma conversão.
    _clicar(acao, 'copilot_confirmar')
    assert slack_bot._tentar_confirmar_por_texto('sim', 'UK', 'C1')
    assert Desperdicio.query.count() == 1
    assert saldo.quantidade == 200
    assert MovEstoqueLoja.query.count() == 0
    _clicar(acao, ACAO_PREPARAR_RETIRADA)
    nova = SlackAcaoPendente.query.filter_by(tipo_acao='criar_retirada_sobras').one()
    assert nova.token != acao.token
    assert json.loads(nova.params_json)['itens'][0]['resolvido']['id'] == fresco.id
    assert nova.slack_message_ts != acao.slack_message_ts
    assert RetiradaSobra.query.count() == 0  # só preparou a revisão
    # Clique antigo, inclusive na alternativa de nova sobra, é inofensivo.
    _clicar(acao, ACAO_NOVA_SOBRA)
    _clicar(acao, ACAO_PREPARAR_RETIRADA)
    assert SlackAcaoPendente.query.count() == 2
    _clicar(nova, 'copilot_confirmar')
    _clicar(nova, 'copilot_confirmar')
    retirada = RetiradaSobra.query.one()
    assert retirada.itens[0].receita_id == retorno.id
    assert Desperdicio.query.count() == 1
    assert saldo.quantidade == 200
    assert saldo_fresco.quantidade == 400
    from app.services.devolucao import baixar_loja_retirada
    baixar_loja_retirada(retirada, usuario_id=kelvin.id)
    db.session.commit()
    assert saldo.quantidade == 15  # 200 - 185, sem somar antes
    assert saldo_outra.quantidade == 77
    assert MovEstoqueLoja.query.filter_by(tipo='sobra_retorno_entrada').count() == 0


@pytest.mark.parametrize('lote', [False, True])
def test_nova_sobra_legitima_com_saldo_so_converte_uma_vez(cenario, monkeypatch, lote):
    *_, saldo, saldo_fresco, saldo_outra, update = cenario
    acao = _proposta(cenario, monkeypatch, lote)
    _clicar(acao, ACAO_NOVA_SOBRA)
    _clicar(acao, ACAO_NOVA_SOBRA)
    _clicar(acao, ACAO_PREPARAR_RETIRADA)
    assert Desperdicio.query.count() == 2
    assert saldo.quantidade == 385
    assert saldo_fresco.quantidade == 215
    assert saldo_outra.quantidade == 77
    assert RetiradaSobra.query.count() == 0
    assert MovEstoqueLoja.query.filter_by(tipo='sobra_retorno_entrada').count() == 1
    novo = Desperdicio.query.order_by(Desperdicio.id.desc()).first()
    assert 'convertido em' in novo.observacao
    assert 'nao baixou estoque' not in novo.observacao


def test_token_legado_sem_flag_exige_escolha(cenario, monkeypatch):
    acao = _proposta(cenario, monkeypatch)
    params = json.loads(acao.params_json)
    params.pop('_opcoes_sobra')
    acao.params_json = json.dumps(params)
    db.session.commit()
    _clicar(acao, 'copilot_confirmar')
    assert acao.executado_em is None
    assert Desperdicio.query.count() == 1
    text = json.dumps(cenario[-1].call_args.kwargs['blocks'], ensure_ascii=False)
    assert ACAO_NOVA_SOBRA in text
    assert ACAO_PREPARAR_RETIRADA in text


def test_lote_misto_nao_descarta_itens_ao_preparar_retirada(cenario, monkeypatch):
    acao = _proposta(cenario, monkeypatch, lote=True)
    params = json.loads(acao.params_json)
    params['itens'].append({'nome': 'Item desconhecido', 'quantidade': 2})
    acao.params_json = json.dumps(params)
    db.session.commit()
    _clicar(acao, ACAO_PREPARAR_RETIRADA)
    assert acao.executado_em is None and acao.cancelado_em is None
    assert SlackAcaoPendente.query.count() == 1
    assert Desperdicio.query.count() == 1
    assert RetiradaSobra.query.count() == 0


def test_escolha_so_pelo_usuario_e_canal_da_proposta(cenario, monkeypatch):
    acao = _proposta(cenario, monkeypatch)
    slack_bot.processar_interacao_botao(ACAO_NOVA_SOBRA, acao.token, 'OUTRO', 'C1', '2000.1')
    slack_bot.processar_interacao_botao(ACAO_NOVA_SOBRA, acao.token, 'UK', 'OUTRO', '2000.1')
    assert Desperdicio.query.count() == 1
    assert acao.executado_em is None


@pytest.mark.parametrize('revogacao', ['vinculo', 'papel', 'retirada'])
def test_permissao_revogada_apos_proposta_nao_executa(cenario, monkeypatch, revogacao):
    acao = _proposta(cenario, monkeypatch)
    if revogacao == 'vinculo':
        SlackVinculo.query.one().ativo = False
    elif revogacao == 'papel':
        cenario[3].papel = 'relatorio_loja'
    else:
        pode_usar = copilot.pode_usar
        monkeypatch.setattr(copilot, 'pode_usar',
                            lambda tipo, user: tipo != 'criar_retirada_sobras' and pode_usar(tipo, user))
    db.session.commit()
    _clicar(acao, ACAO_PREPARAR_RETIRADA if revogacao == 'retirada' else ACAO_NOVA_SOBRA)
    assert Desperdicio.query.count() == 1
    assert RetiradaSobra.query.count() == 0
    assert SlackAcaoPendente.query.count() == 1
    assert acao.executado_em is None


@pytest.mark.parametrize('cadastro', ['cadeia', 'homonimo_arquivado'])
def test_retirada_preserva_destino_canonico_sem_novo_match(cenario, monkeypatch, cadastro):
    loja, fresco, retorno, kelvin, *_ = cenario
    acao = _proposta(cenario, monkeypatch)
    outro = Receita(nome=retorno.nome if cadastro == 'homonimo_arquivado' else 'Outro retorno',
                    rendimento_qtd=1, rendimento_unidade='un', peso_base=100)
    db.session.add(outro)
    db.session.flush()
    if cadastro == 'cadeia':
        retorno.retorno_receita_id = outro.id
    else:
        retorno.arquivada_em = agora()
    db.session.commit()
    _clicar(acao, ACAO_PREPARAR_RETIRADA)
    nova = SlackAcaoPendente.query.filter_by(tipo_acao='criar_retirada_sobras').one()
    _clicar(nova, 'copilot_confirmar')
    retirada = RetiradaSobra.query.one()
    assert retirada.itens[0].receita_id == retorno.id
    assert Desperdicio.query.count() == 1


def test_message_e_mention_da_mesma_mensagem_so_processam_uma_vez(app, monkeypatch):
    client = app.test_client()
    monkeypatch.setattr('app.services.slack.verify_signing', lambda *a: True)
    app.config['SLACK_CANAIS_PERMITIDOS'] = 'C1'
    disparar = Mock()
    monkeypatch.setattr(slack_bot, 'disparar_evento', disparar)
    for event_id, tipo, ts in [('E1', 'message', '100.1'),
                               ('E2', 'app_mention', '100.1'),
                               ('E3', 'message', '100.2')]:
        result = client.post('/slack/events', json={
            'type': 'event_callback', 'team_id': 'T1', 'event_id': event_id,
            'event': {'type': tipo, 'ts': ts, 'user': 'UK', 'channel': 'C1',
                      'text': '<@BOT> 185 croissants para retirada'},
        })
        assert result.status_code == 200
    assert disparar.call_count == 2
