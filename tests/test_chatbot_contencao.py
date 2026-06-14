"""Pilar A da meta 90% de contenção — observabilidade.

O bot do site grava em VigiaVeredito cada decisão (responder ou handoff).
Antes do Pilar A, a lista `tools_usadas` ficava em memória dentro de
`chatbot.interpretar` e sumia no fim do request — sem ela, "handoff
preguiçoso" (transferir sem tentar nenhuma tool) só era detectável via
LLM-judge.

Estas travas garantem:
1. A nova coluna `tools_usadas` persiste como JSON.
2. O auditor calcula `contencao_pct` = 1 - (conv com handoff / conv totais).
3. Conversas-tracking (detectores deterministicos `[FOLLOWUP/[ABANDONO/
   [ESPERA_HUMANO`) NAO entram no denominador — senão inflam.
4. Handoff "preguicoso" (tools=[] ou só transferir_para_humano) é
   contado separadamente.
5. A mensagem WhatsApp do auditor cita a contenção real.
"""
from datetime import datetime, timedelta


def _setup_veredito(app, *, conv_id, bot_acao, tools=None, gravidade=None,
                     mensagem='oi', cliente='Cliente X', criado_em=None):
    import json

    from app.extensions import db
    from app.models import VigiaVeredito
    from app.utils import agora
    v = VigiaVeredito(
        criado_em=criado_em or agora(),
        conv_id=str(conv_id),
        cliente=cliente,
        mensagem_cliente=mensagem,
        bot_acao=bot_acao,
        bot_motivo='',
        gravidade=gravidade,
        motivo_vigia='',
        alerta=False,
        enviado_whatsapp=False,
        tools_usadas=(json.dumps(tools) if tools is not None else None),
    )
    db.session.add(v)
    db.session.commit()
    return v


def test_modelo_vigiaveredito_aceita_tools_usadas(app):
    """A coluna existe e aceita NULL + JSON."""
    from app.extensions import db
    from app.models import VigiaVeredito
    with app.app_context():
        v = _setup_veredito(app, conv_id='1', bot_acao='responder',
                             tools=['consultar_produtos', 'gerar_link_carrinho'])
        db.session.refresh(v)
        assert v.tools_usadas == '["consultar_produtos", "gerar_link_carrinho"]'
        # NULL tambem aceito (detectores deterministicos)
        v2 = _setup_veredito(app, conv_id='2', bot_acao='followup', tools=None)
        db.session.refresh(v2)
        assert v2.tools_usadas is None
        VigiaVeredito.query.delete()
        db.session.commit()


def test_chatbot_vigia_persiste_tools_usadas(app):
    """`_registrar` extrai `tools_usadas` do `resultado_bot` e grava no banco."""
    from app.extensions import db
    from app.models import VigiaVeredito
    from app.services.chatbot_vigia import _registrar
    with app.app_context():
        resultado = {'veredicto': {'alerta': False, 'gravidade': None,
                                    'motivo': ''}, 'enviado': False}
        resultado_bot = {'acao': 'responder', 'motivo': '',
                          'tools_usadas': ['consultar_produtos']}
        _registrar(resultado, conv_id='99', nome_contato='Joao',
                   ultima_mensagem_cliente='oi',
                   resultado_bot=resultado_bot)
        db.session.commit()
        v = VigiaVeredito.query.filter_by(conv_id='99').first()
        assert v is not None
        assert v.tools_usadas == '["consultar_produtos"]'
        VigiaVeredito.query.delete()
        db.session.commit()


def test_auditor_calcula_contencao_pct(app):
    """3 conversas: 1 sem handoff, 2 com handoff → contenção = 33,3%."""
    from app.extensions import db
    from app.models import VigiaVeredito
    from app.services.chatbot_auditor import _coletar_periodo
    from app.utils import agora
    with app.app_context():
        _setup_veredito(app, conv_id='c1', bot_acao='responder',
                         tools=['consultar_produtos'])
        _setup_veredito(app, conv_id='c2', bot_acao='handoff',
                         tools=['consultar_pedido', 'transferir_para_humano'])
        _setup_veredito(app, conv_id='c3', bot_acao='handoff',
                         tools=['transferir_para_humano'])  # preguicoso
        ini = agora() - timedelta(hours=1)
        fim = agora() + timedelta(hours=1)
        dados = _coletar_periodo(ini, fim)
        assert dados['conversas_unicas'] == 3
        assert dados['handoffs'] == 2
        assert dados['conversas_com_handoff'] == 2
        # 1/3 = 33.3% (1 conversa sem handoff de 3 totais)
        assert dados['contencao_pct'] == 33.3
        # Preguicoso = handoff sem tool de busca antes (so transferir)
        assert dados['handoffs_preguicosos'] == 1
        assert dados['conversas_preguicosas'] == 1
        VigiaVeredito.query.delete()
        db.session.commit()


def test_auditor_ignora_conversas_tracking(app):
    """Detectores [FOLLOWUP/[ABANDONO/[ESPERA_HUMANO nao sao conversa
    real de cliente — gravam VigiaVeredito so pra dedupe. Sem o filtro,
    o denominador infla e a contencao parece menor."""
    from app.extensions import db
    from app.models import VigiaVeredito
    from app.services.chatbot_auditor import _coletar_periodo
    from app.utils import agora
    with app.app_context():
        _setup_veredito(app, conv_id='cli1', bot_acao='responder',
                         tools=['consultar_produtos'])
        _setup_veredito(app, conv_id='cli1', bot_acao='followup',
                         mensagem='[FOLLOWUP 30min]')
        _setup_veredito(app, conv_id='cli2', bot_acao=None,
                         mensagem='[ESPERA_HUMANO 45min]')
        _setup_veredito(app, conv_id='teste-foo', bot_acao='responder',
                         tools=['consultar_produtos'])
        ini = agora() - timedelta(hours=1)
        fim = agora() + timedelta(hours=1)
        dados = _coletar_periodo(ini, fim)
        # so cli1 conta (cli2 = espera_humano, teste-* = sintetico)
        assert dados['conversas_unicas'] == 1
        assert dados['contencao_pct'] == 100.0
        VigiaVeredito.query.delete()
        db.session.commit()


def test_auditor_handoff_nao_preguicoso_quando_tool_de_busca(app):
    """Bot fez consultar_pedido antes do transferir → NAO eh preguicoso
    (caso legitimo: cliente reclamando 'pedido nao chegou')."""
    from app.extensions import db
    from app.models import VigiaVeredito
    from app.services.chatbot_auditor import _coletar_periodo
    from app.utils import agora
    with app.app_context():
        _setup_veredito(app, conv_id='c1', bot_acao='handoff',
                         tools=['consultar_pedido', 'transferir_para_humano'])
        ini = agora() - timedelta(hours=1)
        fim = agora() + timedelta(hours=1)
        dados = _coletar_periodo(ini, fim)
        assert dados['handoffs'] == 1
        assert dados['handoffs_preguicosos'] == 0
        VigiaVeredito.query.delete()
        db.session.commit()


def test_mensagem_whatsapp_cita_contencao(app):
    """A linha de contenção aparece logo abaixo do título do auditor."""
    from app.services.chatbot_auditor import _montar_mensagem
    rel = {'destaque': 'Tudo tranquilo hoje', 'resumo_curto': '10 conversas, 1 handoff'}
    dados = {'conversas_unicas': 10, 'conversas_com_handoff': 1,
             'contencao_pct': 90.0, 'handoffs': 1, 'handoffs_preguicosos': 0}
    ini = datetime(2026, 6, 14)
    fim = datetime(2026, 6, 15)
    msg = _montar_mensagem(rel, ini, fim, titulo='Auditor', dados=dados)
    assert 'Contenção:' in msg
    assert '90.0%' in msg
    assert '9/10 conversas' in msg


def test_mensagem_whatsapp_sem_dados_ainda_funciona(app):
    """Compat: sem `dados`, mensagem nao explode."""
    from app.services.chatbot_auditor import _montar_mensagem
    rel = {'destaque': 'tudo ok'}
    msg = _montar_mensagem(rel, datetime(2026, 6, 14), datetime(2026, 6, 15),
                            dados=None)
    assert 'tudo ok' in msg
    assert 'Contenção' not in msg
