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


def _seed_reaproveitavel_com_retorno():
    """Croissant reaproveitável apontando pra receita de retorno."""
    from app.extensions import db
    from app.models import Loja, Receita
    loja = Loja(nome='Loja Retirada', ativa=True)
    retorno = Receita(nome='Croissant Ret - Retorno', rendimento_qtd=1,
                      rendimento_unidade='un', peso_base=100.0)
    db.session.add_all([loja, retorno])
    db.session.commit()
    croissant = Receita(nome='Croissant Ret', rendimento_qtd=1,
                        rendimento_unidade='un', peso_base=100.0,
                        reaproveitavel=True, retorno_receita_id=retorno.id)
    db.session.add(croissant)
    db.session.commit()
    return loja, croissant, retorno


# ── pergunta da retirada NA HORA (preview) — combinado original do dono ────

def test_enrich_lote_sugere_retirada_no_preview(app, admin_user):
    """Combinado do dono: quando a sobra é falada, o bot JÁ pergunta
    quantos voltam — a sugestão nasce no enrich do preview, não só na
    execução pós-botão."""
    from app.services.copilot import _enriquecer_registrar_desperdicio_lote
    from app.services.slack_blocks import build_preview
    with app.app_context():
        loja, croissant, retorno = _seed_reaproveitavel_com_retorno()
        out = _enriquecer_registrar_desperdicio_lote(
            {'loja_nome': 'Loja Retirada', 'motivo': 'validade',
             'itens': [{'nome': 'Croissant Ret', 'quantidade': 15}]},
            admin_user)
        assert out['retiradas_sugeridas'] == [{
            'item': 'Croissant Ret', 'qtd_sobra': 15,
            'destino': 'Croissant Ret - Retorno'}]
        txt = json.dumps(build_preview('registrar_desperdicio_lote', out,
                                       'tok-r'), ensure_ascii=False)
        assert 'Quantos vão voltar?' in txt
        assert 'foto da sobra' in txt


def test_enrich_single_sugere_retirada_no_preview(app, admin_user):
    from app.services.copilot import _enriquecer_registrar_desperdicio
    from app.services.slack_blocks import build_preview
    with app.app_context():
        loja, croissant, retorno = _seed_reaproveitavel_com_retorno()
        out = _enriquecer_registrar_desperdicio(
            {'loja_nome': 'Loja Retirada', 'item_nome': 'Croissant Ret',
             'quantidade': 8, 'motivo': 'nao_vendeu'}, admin_user)
        assert out['retiradas_sugeridas'][0]['qtd_sobra'] == 8
        txt = json.dumps(build_preview('registrar_desperdicio', out, 'tok-s'),
                         ensure_ascii=False)
        assert 'Quantos vão voltar?' in txt


def test_enrich_nao_sugere_sem_retorno_ou_motivo_errado(app, admin_user):
    from app.services.copilot import _enriquecer_registrar_desperdicio_lote
    with app.app_context():
        loja, croissant, retorno = _seed_reaproveitavel_com_retorno()
        # Motivo não-reaproveitável (estragou) → sem sugestão.
        out = _enriquecer_registrar_desperdicio_lote(
            {'loja_nome': 'Loja Retirada', 'motivo': 'estragou',
             'itens': [{'nome': 'Croissant Ret', 'quantidade': 5}]},
            admin_user)
        assert out['retiradas_sugeridas'] == []
        # Receita sem retorno configurado → sem sugestão.
        out2 = _enriquecer_registrar_desperdicio_lote(
            {'loja_nome': 'Loja Retirada', 'motivo': 'validade',
             'itens': [{'nome': 'Croissant Ret - Retorno', 'quantidade': 5}]},
            admin_user)
        assert out2['retiradas_sugeridas'] == []


def test_modo_restrito_inclui_criar_retirada_sobras():
    """O canal de sobras roda em MODO RESTRITO (bot de pedidos OFF desde
    28/06) — sem a tool de retirada na whitelist o bot perguntava "quantos
    voltam?" e não tinha como agir na resposta (caso real 03/07/2026:
    usuário mandou foto + quantidade e o bot não entendeu)."""
    from app.services.slack_bot import _TOOLS_DESPERDICIO
    assert 'criar_retirada_sobras' in _TOOLS_DESPERDICIO


def test_pergunta_retirada_entra_no_historico(app):
    from app.services.slack_bot import _pergunta_retirada_para_historico
    txt = _pergunta_retirada_para_historico({
        'retiradas_sugeridas': [{'item': 'Croissant Ret', 'qtd_sobra': 15,
                                 'destino': 'Croissant Ret - Retorno'}]})
    assert 'quantos voltam pra industria' in txt
    assert 'criar_retirada_sobras' in txt
    assert _pergunta_retirada_para_historico({}) == ''
    assert _pergunta_retirada_para_historico(None) == ''


# ── foto em mensagem anterior (06/07/2026, prints do dono) ─────────────────
# Caso real: Kelvin mandou a FOTO num balão e "180" no outro — o bot recusava
# exigindo tudo na MESMA mensagem e obrigava a reenviar a foto. Agora o
# slack_bot busca a última foto do próprio usuário no canal (2h) e anexa.

def test_foto_recente_do_canal_pega_ultima_do_usuario(app):
    import base64
    import time

    from app.services.slack_bot import _foto_recente_do_canal
    agora = time.time()
    msgs = [
        {'user': 'U123', 'ts': f'{agora - 60:.6f}',
         'files': [{'mimetype': 'image/jpeg', 'url_private_download': 'u-nova'}]},
        {'user': 'U999', 'ts': f'{agora - 30:.6f}',        # de OUTRO usuário
         'files': [{'mimetype': 'image/jpeg', 'url_private_download': 'u-x'}]},
        {'user': 'U123', 'ts': f'{agora - 600:.6f}',       # dele, mais velha
         'files': [{'mimetype': 'image/jpeg', 'url_private_download': 'u-velha'}]},
        {'user': 'U123', 'ts': f'{agora - 40:.6f}', 'text': 'só texto'},
    ]
    with app.app_context(), \
         patch('app.services.slack.historico_canal',
               return_value=(msgs, None)), \
         patch('app.services.slack.baixar_arquivo',
               return_value={'bytes': b'foto!', 'mimetype': 'image/jpeg'}) as bx:
        fa = _foto_recente_do_canal('C1', 'U123')
    assert fa is not None
    # baixou a imagem mais NOVA do próprio usuário (não a do outro)
    assert bx.call_args[0][0]['url_private_download'] == 'u-nova'
    assert fa['imagem']['base64'] == base64.b64encode(b'foto!').decode()
    assert fa['quando'].startswith('ha ')


def test_foto_recente_sem_imagem_retorna_none(app):
    from app.services.slack_bot import _foto_recente_do_canal
    with app.app_context(), \
         patch('app.services.slack.historico_canal',
               return_value=([{'user': 'U123', 'ts': '1.0', 'text': 'oi'}],
                             None)):
        assert _foto_recente_do_canal('C1', 'U123') is None


def test_mensagem_de_quantidade_usa_foto_de_mensagem_anterior(app, admin_user):
    """Integração do caso dos prints: '180' sem anexo → o slack_bot embute a
    última foto do canal nos params da ação pendente (nada de erro pedindo a
    foto na MESMA mensagem)."""
    import base64
    import time

    from app.models import SlackAcaoPendente
    from app.services.slack_bot import processar_evento_mensagem
    with app.app_context():
        resp_tool = {'tipo': 'criar_retirada_sobras', 'requer_aprovacao': True,
                     'params': {'loja_nome': 'Loja R',
                                'itens': [{'nome': 'Croissant',
                                           'quantidade': 180}]},
                     'explicacao': 'Criando retirada'}
        msgs = [{'user': 'U123', 'ts': f'{time.time() - 60:.6f}',
                 'files': [{'mimetype': 'image/jpeg',
                            'url_private_download': 'u'}]}]
        with patch('app.services.slack_bot._resolver_usuario',
                   return_value=admin_user), \
             patch('app.services.copilot.interpretar',
                   return_value=resp_tool), \
             patch('app.services.slack.historico_canal',
                   return_value=(msgs, None)), \
             patch('app.services.slack.baixar_arquivo',
                   return_value={'bytes': b'sobra', 'mimetype': 'image/jpeg'}), \
             patch('app.services.slack.post_message',
                   return_value={'ok': True, 'ts': '9'}):
            processar_evento_mensagem({'user': 'U123', 'channel': 'D9',
                                       'text': '180', 'channel_type': 'im'})
        acao = SlackAcaoPendente.query.filter_by(
            tipo_acao='criar_retirada_sobras').first()
        assert acao is not None
        params = json.loads(acao.params_json)
        assert params['_n_imagens'] == 1
        assert params['_foto_anterior']
        assert params['imagens'][0]['base64'] == \
            base64.b64encode(b'sobra').decode()


def test_preview_retirada_mostra_origem_da_foto(app):
    from app.services.slack_blocks import build_preview
    base = {'loja_nome': 'Loja R', 'data_retirada': '2026-07-07',
            'itens': [{'nome': 'Croissant', 'quantidade': 180,
                       'resolvido': {'tipo': 'receita', 'id': 1,
                                     'nome': 'Croissant'},
                       'destino': 'Croissant - Retorno'}]}
    com_atual = json.dumps(build_preview(
        'criar_retirada_sobras', {**base, '_n_imagens': 1}, 't1'),
        ensure_ascii=False)
    assert 'anexada nesta mensagem' in com_atual
    com_anterior = json.dumps(build_preview(
        'criar_retirada_sobras',
        {**base, '_n_imagens': 1, '_foto_anterior': 'ha 3 min'}, 't2'),
        ensure_ascii=False)
    assert 'ha 3 min' in com_anterior
    sem_foto = json.dumps(build_preview('criar_retirada_sobras', base, 't3'),
                          ensure_ascii=False)
    assert 'cancele e mande a foto' in sem_foto
    assert '180x Croissant' in sem_foto


# ── resultado do desperdício diz o que houve com o estoque (06/07/2026) ────

def test_resultado_diferencia_convertido_de_sem_retorno(app):
    """Com receita de retorno o estoque foi CONVERTIDO (fresco → retorno) —
    o aviso genérico 'NÃO foi baixado' confundia. Sem retorno, mantém o
    aviso de que nada baixou."""
    from app.services.slack_blocks import build_resultado
    res = {'ok': True, 'loja': 'X', 'total_aplicados': 2,
           'reaproveitados_sem_baixa': 2,
           'aplicados': [
               {'nome': 'Croissant', 'reaproveitavel': True,
                'convertido_retorno': {'destino': 'Croissant - Retorno',
                                       'baixado': 10, 'creditado': 10}},
               {'nome': 'Bolo', 'reaproveitavel': True},
           ]}
    txt = json.dumps(build_resultado(res, ok=True), ensure_ascii=False)
    assert 'virou' in txt and 'Croissant - Retorno' in txt
    assert 'sem receita de retorno' in txt          # só o Bolo


def test_resultado_single_convertido_nao_diz_nao_baixado(app):
    from app.services.slack_blocks import build_resultado
    res = {'ok': True, 'desperdicio_id': 5, 'loja': 'X',
           'reaproveitavel_sem_baixa': True,
           'convertido_retorno': {'destino': 'Croissant - Retorno'}}
    txt = json.dumps(build_resultado(res, ok=True), ensure_ascii=False)
    assert 'virou' in txt
    assert 'NÃO foi baixado' not in txt


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
