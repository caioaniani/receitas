"""Item 9 (spec do dono, caso E3862E49, 22/09/2026): reconhecer um alerta
do Vigia SILENCIA o som — não encerra o caso.

- "alerta silenciado" (`VigiaVeredito.reconhecido_em`) ≠ "caso resolvido"
  (`VigiaAlertaResolucao`);
- ALTA só resolvido com resposta HUMANA enviada na conversa DEPOIS do
  alerta (painel ou Chatwoot), conversa resolvida, ou motivo por escrito;
- silenciado e não resolvido continua na fila do painel (banner + contador),
  só sem som.
"""
from datetime import timedelta

import pytest


@pytest.fixture
def admin_logado(app):
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='Gerente', login='ger9', papel='admin')
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(u.id)
        sess['_fresh'] = True
    return client


def _alerta(**kw):
    from app.extensions import db
    from app.models import VigiaVeredito
    from app.utils import agora
    base = dict(conv_id='115', cliente='Bruna', alerta=True, gravidade='alta',
                motivo_vigia='Cliente reclamou de entrega e o bot não transferiu',
                mensagem_cliente='meu pedido não chegou', criado_em=agora())
    base.update(kw)
    v = VigiaVeredito(**base)
    db.session.add(v)
    db.session.commit()
    return v


def _pendentes_ids():
    from app.services import chatbot_vigia
    return [v.id for v in chatbot_vigia._query_pendentes().all()]


def _ts(dt):
    """Epoch como o Chatwoot manda (`created_at`), a partir de um `agora()`
    naive em BRT — sem isto o `.timestamp()` usaria o fuso do container."""
    from app.utils import BRT
    return int(dt.replace(tzinfo=BRT).timestamp())


# ── reconhecer = silenciar ─────────────────────────────────────────────

def test_reconhecer_silencia_mas_alerta_continua_pendente(app, admin_logado):
    from app.models import VigiaVeredito
    from app.services import chatbot_vigia
    with app.app_context():
        v = _alerta()
        vid = v.id
    r = admin_logado.post('/entregas/api/painel/vigia/reconhecer', json={})
    assert r.get_json()['reconhecidos'] == 1
    with app.app_context():
        v = VigiaVeredito.query.get(vid)
        assert v.reconhecido_em is not None
        assert _pendentes_ids() == [vid]            # continua na fila
        resumo = chatbot_vigia.alertas_pendentes_resumo()
        assert resumo['pendentes'] == 1
        assert resumo['nao_reconhecidos'] == 0      # mas o som para
        assert resumo['ultimo']['reconhecido'] is True
        assert resumo['ultimo']['resolvido'] is False
    # 2º clique: nada novo a silenciar (idempotente), e segue pendente
    r2 = admin_logado.post('/entregas/api/painel/vigia/reconhecer', json={})
    assert r2.get_json()['reconhecidos'] == 0
    with app.app_context():
        assert _pendentes_ids() == [vid]


def test_resumo_mostra_o_nao_reconhecido_primeiro(app):
    """Com um silenciado e um novo, o banner descreve o que ainda toca."""
    from app.services import chatbot_vigia
    from app.utils import agora
    with app.app_context():
        _alerta(conv_id='1', cliente='Velho', criado_em=agora() - timedelta(minutes=30),
                reconhecido_em=agora())
        novo = _alerta(conv_id='2', cliente='Novo', criado_em=agora() - timedelta(minutes=40))
        resumo = chatbot_vigia.alertas_pendentes_resumo()
        assert resumo['pendentes'] == 2 and resumo['nao_reconhecidos'] == 1
        assert resumo['ultimo']['id'] == novo.id


# ── resolver manual (motivo obrigatório) ───────────────────────────────

def test_resolver_manual_exige_motivo_e_tira_da_fila(app, admin_logado):
    from app.models import VigiaAlertaResolucao
    with app.app_context():
        v = _alerta()
        vid = v.id
    sem_motivo = admin_logado.post('/entregas/api/painel/vigia/resolver',
                                   json={'ids': [vid], 'motivo': '   '})
    assert sem_motivo.status_code == 400
    assert 'motivo' in sem_motivo.get_json()['erro'].lower()
    sem_ids = admin_logado.post('/entregas/api/painel/vigia/resolver',
                                json={'ids': [], 'motivo': 'x'})
    assert sem_ids.status_code == 400
    with app.app_context():
        assert _pendentes_ids() == [vid]
    ok = admin_logado.post('/entregas/api/painel/vigia/resolver',
                           json={'ids': [vid], 'motivo': 'Liguei pra cliente, reentrega combinada'})
    assert ok.status_code == 200 and ok.get_json()['resolvidos'] == 1
    with app.app_context():
        assert _pendentes_ids() == []
        r = VigiaAlertaResolucao.query.filter_by(veredito_id=vid).one()
        assert r.via == 'manual' and 'reentrega' in r.motivo
        assert r.usuario_id is not None
        # resolver implica silenciar
        from app.models import VigiaVeredito
        assert VigiaVeredito.query.get(vid).reconhecido_em is not None
    # idempotente
    de_novo = admin_logado.post('/entregas/api/painel/vigia/resolver',
                                json={'ids': [vid], 'motivo': 'outra vez'})
    assert de_novo.get_json()['resolvidos'] == 0
    hist = admin_logado.get('/entregas/api/painel/vigia/historico').get_json()['alertas']
    assert hist[0]['resolvido'] is True
    assert hist[0]['resolvido_via'] == 'manual'
    assert 'reentrega' in hist[0]['resolvido_motivo']


def test_resolver_via_invalida_e_media_nao_resolve(app):
    from app.services import chatbot_vigia
    with app.app_context():
        media = _alerta(gravidade='media')
        with pytest.raises(ValueError):
            chatbot_vigia.resolver_alertas([media.id], via='qualquer')
        with pytest.raises(ValueError):
            chatbot_vigia.resolver_alertas([media.id], via='manual', motivo='')
        # so ALTA vira caso; media nunca entra na fila nem ganha resolucao
        assert chatbot_vigia.resolver_alertas([media.id], via='manual', motivo='x') == 0
        assert chatbot_vigia.resolver_alertas(['abc', None], via='manual', motivo='x') == 0


# ── resolução automática: resposta humana depois do alerta ─────────────

def test_resposta_pelo_painel_resolve_alertas_anteriores_nao_os_posteriores(app):
    from app.models import VigiaAlertaResolucao
    from app.services import atendimento_pendente, chatbot_vigia
    from app.utils import agora
    with app.app_context():
        antes = _alerta(conv_id='500', criado_em=agora() - timedelta(minutes=20))
        momento = agora()
        atendimento_pendente.confirmar_acao_painel(
            '500', 'responder', iniciado_em=momento, usuario_id=7)
        depois = _alerta(conv_id='500', criado_em=momento + timedelta(minutes=5))
        assert _pendentes_ids() == [depois.id]
        r = VigiaAlertaResolucao.query.filter_by(veredito_id=antes.id).one()
        assert r.via == 'resposta_humana' and r.usuario_id == 7
        assert r.resolvido_em == momento
        # resolver pelo painel: conversa resolvida (DEPOIS do 2º alerta)
        atendimento_pendente.confirmar_acao_painel(
            '500', 'resolved', iniciado_em=momento + timedelta(minutes=10), usuario_id=7)
        assert _pendentes_ids() == []
        r2 = VigiaAlertaResolucao.query.filter_by(veredito_id=depois.id).one()
        assert r2.via == 'conversa_resolvida'
        assert chatbot_vigia.alertas_pendentes_resumo()['pendentes'] == 0


def test_resposta_humana_no_chatwoot_resolve_via_preparar(app):
    """A equipe respondeu pelo Chatwoot (não pelo painel): o leitor
    periódico (`preparar`, com autoria) fecha o alerta anterior à resposta.
    A fala do BOT (`humano=False`) não resolve nada."""
    from app.models import VigiaAlertaResolucao
    from app.services import atendimento_pendente
    from app.utils import agora
    with app.app_context():
        alerta_em = agora() - timedelta(minutes=30)
        v = _alerta(conv_id='600', criado_em=alerta_em)
        conversa = {'id': 600, 'nome_contato': 'Bruna', 'minutos_paradas': 5}
        ts = _ts
        # Só bot depois do alerta → segue pendente
        hist_bot = [
            {'role': 'user', 'content': 'meu pedido não chegou', 'humano': False,
             'created_at': ts(alerta_em - timedelta(minutes=1))},
            {'role': 'assistant', 'content': 'Sinto muito! Já estou passando pra equipe.',
             'humano': False, 'created_at': ts(alerta_em)},
            {'role': 'user', 'content': 'e aí?', 'humano': False,
             'created_at': ts(agora() - timedelta(minutes=5))},
        ]
        atendimento_pendente.preparar(conversa, hist_bot)
        assert _pendentes_ids() == [v.id]
        # Humano respondeu DEPOIS do alerta → resolve
        resposta_em = alerta_em + timedelta(minutes=10)
        hist_humano = hist_bot[:2] + [
            {'role': 'assistant', 'content': 'Oi Bruna, aqui é a Ana. Já reenviamos!',
             'humano': True, 'created_at': ts(resposta_em)},
            {'role': 'user', 'content': 'obrigada', 'humano': False,
             'created_at': ts(resposta_em + timedelta(minutes=2))},
        ]
        atendimento_pendente.preparar(conversa, hist_humano)
        assert _pendentes_ids() == []
        r = VigiaAlertaResolucao.query.filter_by(veredito_id=v.id).one()
        assert r.via == 'resposta_humana'
        # resolvido no instante da resposta (com tolerância de segundo)
        assert abs((r.resolvido_em - resposta_em).total_seconds()) < 2


def test_resposta_humana_anterior_ao_alerta_nao_resolve(app):
    from app.services import atendimento_pendente
    from app.utils import agora
    with app.app_context():
        alerta_em = agora() - timedelta(minutes=5)
        v = _alerta(conv_id='700', criado_em=alerta_em)
        conversa = {'id': 700, 'nome_contato': 'Bruna', 'minutos_paradas': 5}
        ts = _ts
        hist = [
            {'role': 'assistant', 'content': 'Oi! Aqui é a Ana.', 'humano': True,
             'created_at': ts(alerta_em - timedelta(minutes=30))},
            {'role': 'user', 'content': 'meu pedido não chegou', 'humano': False,
             'created_at': ts(alerta_em - timedelta(minutes=1))},
        ]
        atendimento_pendente.preparar(conversa, hist)
        assert _pendentes_ids() == [v.id]


def test_conversa_resolvida_no_chatwoot_resolve_pelo_candidatos(app):
    from unittest.mock import patch

    from app.models import EsperaAtendimento, VigiaAlertaResolucao
    from app.services import atendimento_pendente
    from app.utils import agora
    with app.app_context():
        v = _alerta(conv_id='800', criado_em=agora() - timedelta(minutes=15))
        atendimento_pendente.registrar_alerta(v)
        assert EsperaAtendimento.query.get('800').estado == 'aguardando'
        with patch('app.services.chatwoot.listar_conversas_paradas', return_value=[]), \
                patch('app.services.chatwoot.consultar_conversa',
                      return_value={'id': 800, 'status': 'resolved'}), \
                patch('app.services.presenca_humana.encerrar'):
            atendimento_pendente.candidatos()
        assert _pendentes_ids() == []
        assert VigiaAlertaResolucao.query.filter_by(veredito_id=v.id).one().via == 'conversa_resolvida'


def test_webhook_status_resolved_resolve_alertas(app):
    from app.models import VigiaAlertaResolucao
    with app.app_context():
        app.config['CHATWOOT_BOT_SECRET'] = 'seg'
        v = _alerta(conv_id='900')
        vid = v.id
    c = app.test_client()
    from unittest.mock import patch
    with patch('app.services.presenca_humana.encerrar', return_value=False):
        r = c.post('/crm/bot?k=seg', json={'event': 'conversation_status_changed',
                                            'conversation': {'id': 900, 'status': 'resolved'}})
    assert r.status_code == 200
    with app.app_context():
        assert VigiaAlertaResolucao.query.filter_by(veredito_id=vid).one().via == 'conversa_resolvida'


# ── briefing do dono ───────────────────────────────────────────────────

def test_briefing_conta_nao_resolvidos_mesmo_silenciados(app):
    from app.services import briefing_dono
    from app.utils import agora
    with app.app_context():
        _alerta(reconhecido_em=agora())
        pend = [p for p in briefing_dono.pendencias() if p.get('chave') == 'vigia_bot']
        assert pend and pend[0]['qtd'] == 1
        assert 'resolução' in pend[0]['rotulo']
