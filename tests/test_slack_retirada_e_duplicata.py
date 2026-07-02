"""Fixes do caso real 02/07/2026 (Nebraska): lote de desperdício duplicado e
retirada de sobras que nunca nascia.

1. Claim atômico no botão Confirmar: dois cliques quase simultâneos (ou retry
   do Slack) executavam a ação DUAS vezes — `executado_em` só era setado
   depois do executar.
2. `retirada_sugerida` do executor aparecia em lugar nenhum: o resultado no
   Slack virava só "✓ N desperdício(s)" e o modelo não ficava sabendo — a
   pergunta "quantos voltam pra virar almond?" nunca acontecia e a retirada
   não era criada.
3. Preview do lote agora avisa itens já registrados HOJE na mesma loja
   (o modelo re-enviou a lista inteira pra acrescentar 1 item e duplicou 4).
"""
import json
from unittest.mock import patch


def _acao(app, admin_user, token='tok-claim', tipo='registrar_desperdicio_lote',
          params=None):
    from app.extensions import db
    from app.models import SlackAcaoPendente
    acao = SlackAcaoPendente(
        token=token, slack_user_id='U123', slack_channel_id='C456',
        slack_message_ts='1000.000', tipo_acao=tipo,
        params_json=json.dumps(params or {}), usuario_id=admin_user.id,
    )
    db.session.add(acao)
    db.session.commit()
    return acao


# ── 1. claim atômico ────────────────────────────────────────────────────────

def test_confirmar_duas_vezes_executa_uma_so(app, admin_user):
    from app.services.slack_bot import processar_interacao_botao
    with app.app_context():
        _acao(app, admin_user)
        with patch('app.services.copilot.executar',
                   return_value={'ok': True}) as ex, \
             patch('app.services.slack.update_message') as up:
            processar_interacao_botao('copilot_confirmar', 'tok-claim',
                                      'U123', 'C456', '1000.000')
            processar_interacao_botao('copilot_confirmar', 'tok-claim',
                                      'U123', 'C456', '1000.000')
        ex.assert_called_once()
        # Segundo clique morre em 'ja processada'.
        assert up.call_args_list[-1].kwargs.get('text') == 'ja processada'


def test_falha_marca_cancelado_e_nao_reexecuta(app, admin_user):
    from app.extensions import db
    from app.models import SlackAcaoPendente
    from app.services.slack_bot import processar_interacao_botao
    with app.app_context():
        _acao(app, admin_user, token='tok-falha')
        with patch('app.services.copilot.executar',
                   return_value={'ok': False, 'erro': 'x'}) as ex, \
             patch('app.services.slack.update_message'):
            processar_interacao_botao('copilot_confirmar', 'tok-falha',
                                      'U123', 'C456', '1000.000')
            processar_interacao_botao('copilot_confirmar', 'tok-falha',
                                      'U123', 'C456', '1000.000')
        ex.assert_called_once()
        db.session.expire_all()
        acao = SlackAcaoPendente.query.filter_by(token='tok-falha').first()
        assert acao.cancelado_em is not None
        assert acao.executado_em is None


# ── 2. retirada sugerida no resultado + na conversa ────────────────────────

_RESULTADO_LOTE = {
    'ok': True, 'loja': 'Loja Nebraska', 'total_aplicados': 2,
    'aplicados': [
        {'nome': 'Croissant Tradicional', 'tipo': 'receita', 'quantidade': 15,
         'reaproveitavel': True,
         'retirada_sugerida': {'item': 'Croissant Tradicional',
                               'qtd_sobra': 15,
                               'destino': 'Croissant Tradicional - Retorno'}},
        {'nome': 'Pain au Chocolat', 'tipo': 'receita', 'quantidade': 1,
         'baixado': 1, 'saldo_anterior': 3},
    ],
}


def test_retiradas_sugeridas_de_extrai_lote_e_unitario():
    from app.services.slack_blocks import retiradas_sugeridas_de
    assert retiradas_sugeridas_de(_RESULTADO_LOTE)[0]['qtd_sobra'] == 15
    unit = {'ok': True, 'retirada_sugerida': {'item': 'X', 'qtd_sobra': 2,
                                              'destino': 'Y'}}
    assert retiradas_sugeridas_de(unit)[0]['item'] == 'X'
    assert retiradas_sugeridas_de({'ok': True}) == []
    assert retiradas_sugeridas_de(None) == []


def test_build_resultado_mostra_retirada_sugerida(app):
    from app.services.slack_blocks import build_resultado
    blocks = build_resultado(_RESULTADO_LOTE, ok=True)
    txt = json.dumps(blocks, ensure_ascii=False)
    assert '♻️' in txt
    assert '15x Croissant Tradicional' in txt
    assert 'foto da sobra' in txt


def test_confirmacao_apende_contexto_na_conversa(app, admin_user):
    """Depois do Confirmar, a SlackConversa ganha o contexto da retirada —
    sem isso o modelo não sabia do que se tratava quando o usuário
    respondia '10 voltam'."""
    from app.models import SlackConversa
    from app.services.slack_bot import processar_interacao_botao
    with app.app_context():
        _acao(app, admin_user, token='tok-ctx')
        with patch('app.services.copilot.executar',
                   return_value=_RESULTADO_LOTE), \
             patch('app.services.slack.update_message'):
            processar_interacao_botao('copilot_confirmar', 'tok-ctx',
                                      'U123', 'C456', '1000.000')
        sc = SlackConversa.query.filter_by(slack_user_id='U123',
                                           slack_channel_id='C456').first()
        assert sc is not None
        hist = json.loads(sc.mensagens_json)
        assert hist[-1]['role'] == 'assistant'
        assert 'criar_retirada_sobras' in hist[-1]['content']
        assert '15x Croissant Tradicional' in hist[-1]['content']


def test_apendar_contexto_mescla_no_ultimo_assistant(app, admin_user):
    """Histórico terminando em assistant: mescla (a API não aceita dois
    turnos assistant seguidos)."""
    from app.extensions import db
    from app.models import SlackConversa
    from app.services.slack_bot import _apendar_contexto_retirada
    with app.app_context():
        db.session.add(SlackConversa(
            slack_user_id='U123', slack_channel_id='C456',
            mensagens_json=json.dumps([
                {'role': 'user', 'content': 'anota as sobras'},
                {'role': 'assistant', 'content': 'Preview criado.'},
            ])))
        db.session.commit()
        acao = _acao(app, admin_user, token='tok-merge')
        _apendar_contexto_retirada(acao, _RESULTADO_LOTE)
        sc = SlackConversa.query.filter_by(slack_user_id='U123',
                                           slack_channel_id='C456').first()
        hist = json.loads(sc.mensagens_json)
        assert len(hist) == 2                       # mesclou, não anexou
        assert hist[-1]['content'].startswith('Preview criado.')
        assert 'criar_retirada_sobras' in hist[-1]['content']


def test_sem_sugestao_nao_mexe_na_conversa(app, admin_user):
    from app.models import SlackConversa
    from app.services.slack_bot import _apendar_contexto_retirada
    with app.app_context():
        acao = _acao(app, admin_user, token='tok-nada')
        _apendar_contexto_retirada(acao, {'ok': True, 'aplicados': [
            {'nome': 'Pao', 'quantidade': 1}]})
        assert SlackConversa.query.count() == 0


# ── 3. aviso de duplicata no preview do lote ────────────────────────────────

def _seed_loja_receita(nome_receita='Croissant Duplicata'):
    from app.extensions import db
    from app.models import Loja, Receita
    loja = Loja(nome='Loja Dup', ativa=True)
    rec = Receita(nome=nome_receita, rendimento_qtd=1,
                  rendimento_unidade='un', peso_base=100.0)
    db.session.add_all([loja, rec])
    db.session.commit()
    return loja, rec


def test_enriquecer_lote_marca_ja_registrado_hoje(app, admin_user):
    from app.extensions import db
    from app.models import Desperdicio
    from app.services.copilot import _enriquecer_registrar_desperdicio_lote
    with app.app_context():
        loja, rec = _seed_loja_receita()
        db.session.add(Desperdicio(loja_id=loja.id, receita_id=rec.id,
                                   quantidade=15, motivo='validade',
                                   criado_por_id=admin_user.id))
        db.session.commit()
        out = _enriquecer_registrar_desperdicio_lote(
            {'loja_nome': 'Loja Dup', 'motivo': 'validade',
             'itens': [{'nome': 'Croissant Duplicata', 'quantidade': 15}]},
            admin_user)
        assert out['itens'][0]['ja_registrado_hoje'] == 15

        # Preview mostra o alerta antes dos botões.
        from app.services.slack_blocks import build_preview
        blocks = build_preview('registrar_desperdicio_lote', out, 'tok-x')
        txt = json.dumps(blocks, ensure_ascii=False)
        assert 'Ja registrado HOJE' in txt
        assert 'DUPLICA' in txt


def test_enriquecer_lote_sem_registro_previo_nao_avisa(app, admin_user):
    from app.services.copilot import _enriquecer_registrar_desperdicio_lote
    from app.services.slack_blocks import build_preview
    with app.app_context():
        _seed_loja_receita('Croissant Limpo')
        out = _enriquecer_registrar_desperdicio_lote(
            {'loja_nome': 'Loja Dup', 'motivo': 'validade',
             'itens': [{'nome': 'Croissant Limpo', 'quantidade': 3}]},
            admin_user)
        assert out['itens'][0]['ja_registrado_hoje'] == 0
        blocks = build_preview('registrar_desperdicio_lote', out, 'tok-y')
        assert 'Ja registrado HOJE' not in json.dumps(blocks, ensure_ascii=False)


def test_enriquecer_lote_preserva_motivo_nao_vendeu(app, admin_user):
    """O preview normalizava com vocabulário velho e 'nao_vendeu' virava
    'vencido' — divergindo do executor. Agora usa a MESMA normalização."""
    from app.services.copilot import _enriquecer_registrar_desperdicio_lote
    with app.app_context():
        _seed_loja_receita('Pao Sobra')
        out = _enriquecer_registrar_desperdicio_lote(
            {'loja_nome': 'Loja Dup', 'motivo': 'nao_vendeu',
             'itens': [{'nome': 'Pao Sobra', 'quantidade': 2}]},
            admin_user)
        assert out['motivo'] == 'nao_vendeu'
        # Sinônimo antigo continua aceito.
        out2 = _enriquecer_registrar_desperdicio_lote(
            {'loja_nome': 'Loja Dup', 'motivo': 'vencido',
             'itens': [{'nome': 'Pao Sobra', 'quantidade': 2}]},
            admin_user)
        assert out2['motivo'] == 'validade'
