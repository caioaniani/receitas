"""Desperdício unificado tela×copilot (03/07/2026).

A tela /pedidos/desperdicio ignorava a regra do reaproveitável e gravava
motivos legados ('vencido'), então o MESMO croissant baixava pela tela e não
baixava pelo copilot — e o motivo divergia por canal. Agora os dois usam a
fonte única `app/services/desperdicio_core.py`, e a confirmação do Slack
AVISA quando registrou sem baixar.
"""
from app.extensions import db
from app.models import Desperdicio, EstoqueLoja, Loja, MovEstoqueLoja, Receita


def _setup(reaproveitavel=True, qtd_estoque=10):
    loja = Loja(nome='Loja Desp', ativa=True, endereco='Rua D, 1')
    r = Receita(nome='Croissant Reap', categoria='Croissants',
                rendimento_qtd=1, rendimento_unidade='un', peso_base=100.0,
                reaproveitavel=reaproveitavel)
    db.session.add_all([loja, r])
    db.session.commit()
    el = EstoqueLoja(loja_id=loja.id, receita_id=r.id, quantidade=qtd_estoque)
    db.session.add(el)
    db.session.commit()
    return loja, r, el


def _login(client, uid):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)
        sess['_fresh'] = True


def _post_tela(app, admin_user, loja, receita, qtd, motivo):
    c = app.test_client()
    _login(c, admin_user.id)
    return c.post('/pedidos/desperdicio', data={
        'loja_id': str(loja.id),
        'item_id': f'r_{receita.id}',
        'quantidade': str(qtd),
        'motivo': motivo,
    }, follow_redirects=False)


# ── core (fonte única) ──────────────────────────────────────────────────────

def test_normalizar_motivo_legados_e_invalidos():
    from app.services.desperdicio_core import normalizar_motivo
    assert normalizar_motivo('vencido') == 'validade'
    assert normalizar_motivo('estragado') == 'estragou'
    assert normalizar_motivo('queimado') == 'queimou'
    assert normalizar_motivo('nao_vendeu') == 'nao_vendeu'
    assert normalizar_motivo('QUALQUER COISA') == 'validade'   # default
    assert normalizar_motivo(None) == 'validade'


# ── tela respeita a regra (antes ignorava) ──────────────────────────────────

def test_tela_reaproveitavel_registra_sem_baixar(app, admin_user):
    loja, r, el = _setup(reaproveitavel=True)
    resp = _post_tela(app, admin_user, loja, r, qtd=3, motivo='validade')
    assert resp.status_code in (302, 303)
    db.session.refresh(el)
    assert el.quantidade == 10                       # NÃO baixou (regra do dono)
    desp = Desperdicio.query.filter_by(receita_id=r.id).first()
    assert desp is not None and desp.quantidade == 3
    assert 'reaproveitavel' in (desp.observacao or '')
    assert MovEstoqueLoja.query.filter_by(
        estoque_loja_id=el.id).count() == 0          # zero movimento


def test_tela_motivo_legado_vira_canonico(app, admin_user):
    """'vencido' (dropdown antigo / POST velho) grava como 'validade' — e
    dispara a regra do reaproveitável igual ao copilot."""
    loja, r, el = _setup(reaproveitavel=True)
    _post_tela(app, admin_user, loja, r, qtd=2, motivo='vencido')
    desp = Desperdicio.query.filter_by(receita_id=r.id).first()
    assert desp.motivo == 'validade'                 # normalizado
    db.session.refresh(el)
    assert el.quantidade == 10                       # regra aplicada


def test_tela_nao_reaproveitavel_continua_baixando(app, admin_user):
    loja, r, el = _setup(reaproveitavel=False)
    _post_tela(app, admin_user, loja, r, qtd=4, motivo='validade')
    db.session.refresh(el)
    assert el.quantidade == 6                        # baixou normal
    mov = MovEstoqueLoja.query.filter_by(
        estoque_loja_id=el.id, tipo='desperdicio').first()
    assert mov is not None and mov.quantidade == 4


def test_tela_motivo_nao_reaproveitavel_baixa_mesmo_com_flag(app, admin_user):
    """Item reaproveitável mas motivo 'estragou' (não-reaproveitável) →
    baixa normal (mofo não vira almond)."""
    loja, r, el = _setup(reaproveitavel=True)
    _post_tela(app, admin_user, loja, r, qtd=2, motivo='estragou')
    db.session.refresh(el)
    assert el.quantidade == 8


# ── confirmação do Slack avisa quando não baixou ────────────────────────────

def test_slack_avisa_reaproveitavel_single():
    from app.services.slack_blocks import build_resultado
    blocks = build_resultado({'ok': True, 'desperdicio_id': 1,
                              'loja': 'Loja X', 'quantidade': 3,
                              'reaproveitavel_sem_baixa': True})
    texto = str(blocks)
    assert 'NÃO' in texto and 'reaproveitável' in texto


def test_slack_avisa_reaproveitavel_lote():
    from app.services.slack_blocks import build_resultado
    blocks = build_resultado({'ok': True, 'total_aplicados': 5,
                              'loja': 'Loja X',
                              'reaproveitados_sem_baixa': 2})
    texto = str(blocks)
    assert '2 item(ns) reaproveitável(is)' in texto


def test_slack_sem_reaproveitavel_nao_avisa():
    from app.services.slack_blocks import build_resultado
    blocks = build_resultado({'ok': True, 'desperdicio_id': 1,
                              'loja': 'Loja X',
                              'reaproveitavel_sem_baixa': False})
    assert 'reaproveitável' not in str(blocks)


# ── copilot continua com a MESMA regra (fonte única) ────────────────────────

def test_copilot_single_marca_flag_no_resultado(app, admin_user):
    from app.services import copilot
    loja, r, el = _setup(reaproveitavel=True)
    out = copilot.executar_registrar_desperdicio(
        {'loja_nome': loja.nome, 'item_nome': r.nome, 'quantidade': 2,
         'motivo': 'validade'}, admin_user)
    assert out['ok'] is True
    assert out['reaproveitavel_sem_baixa'] is True
    db.session.refresh(el)
    assert el.quantidade == 10                       # não baixou


def test_copilot_lote_conta_reaproveitados(app, admin_user):
    from app.services import copilot
    loja, r, el = _setup(reaproveitavel=True)
    out = copilot.executar_registrar_desperdicio_lote(
        {'loja': loja.nome, 'motivo': 'nao_vendeu',
         'itens': [{'nome': r.nome, 'quantidade': 3}]}, admin_user)
    assert out['ok'] is True
    assert out['reaproveitados_sem_baixa'] == 1
    db.session.refresh(el)
    assert el.quantidade == 10
