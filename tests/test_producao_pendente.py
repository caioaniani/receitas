"""Produção mandada mas não confirmada — overlay verde + auditoria (01/07/2026).

- pendencias_por_receita: soma qtd_alvo − produzido_qtd das ordens ENVIADAS,
  separando agendado (data >= hoje) de vencido (data < hoje).
- listar_pendencias: lista pra auditoria (vencido/agendado, antigos fora da janela).
- Projeção NUNCA toca EstoqueProducao (é overlay calculado).
"""
from datetime import timedelta

from app.extensions import db
from app.models import (
    PlanejamentoItem,
    PlanejamentoProducao,
    Receita,
)
from app.services.producao_pendente import (
    listar_pendencias,
    pendencias_por_receita,
)
from app.utils import hoje


def _receita(nome='Croissant'):
    r = Receita(nome=nome, categoria='Paes', rendimento_qtd=10,
                rendimento_unidade='un', peso_base=1000.0)
    db.session.add(r)
    db.session.commit()
    return r


def _ordem(receita, data, alvo, produzido=0, enviado=True, origem='cronograma'):
    p = PlanejamentoProducao(data=data, origem=origem, status='aprovado',
                             enviado_ao_padeiro=enviado)
    db.session.add(p)
    db.session.flush()
    db.session.add(PlanejamentoItem(planejamento_id=p.id, receita_id=receita.id,
                                    multiplicador=1, qtd_alvo=alvo,
                                    produzido_qtd=produzido))
    db.session.commit()
    return p


# ── pendencias_por_receita ────────────────────────────────────────────────
def test_pendencia_agendada_e_vencida_separadas(app):
    r = _receita()
    _ordem(r, hoje() + timedelta(days=1), alvo=40)          # agendado
    _ordem(r, hoje() - timedelta(days=1), alvo=50)          # vencido
    pend = pendencias_por_receita()
    assert pend[r.id]['agendado'] == 40
    assert pend[r.id]['vencido'] == 50


def test_pendencia_desconta_produzido(app):
    r = _receita()
    _ordem(r, hoje(), alvo=50, produzido=30)                # hoje -> agendado, falta 20
    pend = pendencias_por_receita()
    assert pend[r.id]['agendado'] == 20                     # 50 - 30
    assert pend[r.id]['vencido'] == 0


def test_pendencia_ignora_ordem_totalmente_produzida(app):
    r = _receita()
    _ordem(r, hoje() - timedelta(days=1), alvo=50, produzido=50)   # falta 0
    pend = pendencias_por_receita()
    assert r.id not in pend                                 # nada pendente


def test_pendencia_ignora_nao_enviada(app):
    r = _receita()
    _ordem(r, hoje(), alvo=40, enviado=False)               # rascunho, não desceu
    pend = pendencias_por_receita()
    assert r.id not in pend


def test_pendencia_hoje_conta_como_agendado(app):
    r = _receita()
    _ordem(r, hoje(), alvo=25)                              # data == hoje
    pend = pendencias_por_receita()
    assert pend[r.id]['agendado'] == 25
    assert pend[r.id]['vencido'] == 0


# ── listar_pendencias (auditoria) ─────────────────────────────────────────
def test_listar_separa_e_ordena(app):
    r = _receita()
    _ordem(r, hoje() - timedelta(days=3), alvo=10)          # vencido +3
    _ordem(r, hoje() - timedelta(days=1), alvo=20)          # vencido +1
    _ordem(r, hoje() + timedelta(days=2), alvo=30)          # agendado
    dados = listar_pendencias()
    assert [x['dias'] for x in dados['vencido']] == [1, 3]  # mais recente primeiro
    assert dados['total_vencido'] == 30
    assert len(dados['agendado']) == 1
    assert dados['total_agendado'] == 30


def test_listar_vencido_antigo_vira_contagem(app):
    r = _receita()
    _ordem(r, hoje() - timedelta(days=100), alvo=15)        # fora da janela (30d)
    dados = listar_pendencias(dias_vencido=30)
    assert dados['vencido'] == []                           # não lista
    assert dados['vencidos_antigos'] == 1                   # só conta


def test_projecao_nao_toca_estoque_real(app):
    """A pendência é overlay: não cria/altera EstoqueProducao."""
    from app.models import EstoqueProducao
    r = _receita()
    _ordem(r, hoje() - timedelta(days=1), alvo=50)
    pendencias_por_receita()
    listar_pendencias()
    assert EstoqueProducao.query.filter_by(receita_id=r.id).count() == 0


# ── rotas ─────────────────────────────────────────────────────────────────
def _login(app, admin_user):
    c = app.test_client()
    c.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
           follow_redirects=True)
    return c


def test_grid_mostra_overlay_pendente(app, admin_user):
    r = _receita()
    _ordem(r, hoje() - timedelta(days=1), alvo=50)          # vencido -> aparece no grid
    c = _login(app, admin_user)
    html = c.get('/telaindustriateste/').get_data(as_text=True)
    assert 'projetado' in html
    assert 'auditoria de produção' in html


def test_rota_auditoria_renderiza(app, admin_user):
    r = _receita()
    _ordem(r, hoje() - timedelta(days=2), alvo=40, produzido=10)   # vencido, falta 30
    _ordem(r, hoje() + timedelta(days=1), alvo=20)                 # agendado
    c = _login(app, admin_user)
    resp = c.get('/telaindustriateste/auditoria')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'Vencidas' in html and 'Agendadas' in html
    assert r.nome in html


def test_rota_auditoria_exige_admin(app):
    c = app.test_client()
    resp = c.get('/telaindustriateste/auditoria')
    assert resp.status_code in (301, 302, 403)


# ── dispensa (dar OK e fechar a pendência) ────────────────────────────────
def test_dispensar_tira_da_pendencia(app, admin_user):
    from app.services.producao_pendente import dispensar_item
    r = _receita()
    p = _ordem(r, hoje() - timedelta(days=1), alvo=50)          # vencido
    item = p.itens[0]
    assert pendencias_por_receita()[r.id]['vencido'] == 50

    res = dispensar_item(item.id, admin_user.id)
    assert res['ok'] is True
    assert r.id not in pendencias_por_receita()                 # sumiu do overlay
    dados = listar_pendencias()
    assert dados['vencido'] == []                               # sumiu da auditoria
    assert len(dados['dispensadas']) == 1                       # virou rastro
    assert dados['dispensadas'][0]['dispensada_por'] == admin_user.nome


def test_dispensar_nao_credita_estoque_nem_produzido(app, admin_user):
    """A dispensa é só de auditoria: não toca produzido_qtd nem EstoqueProducao."""
    from app.models import EstoqueProducao, PlanejamentoItem
    from app.services.producao_pendente import dispensar_item
    r = _receita()
    p = _ordem(r, hoje() - timedelta(days=1), alvo=50, produzido=20)
    dispensar_item(p.itens[0].id, admin_user.id)
    it = db.session.get(PlanejamentoItem, p.itens[0].id)
    assert it.produzido_qtd == 20                               # NÃO mexeu
    assert it.dispensada_em is not None
    assert EstoqueProducao.query.filter_by(receita_id=r.id).count() == 0


def test_reverter_dispensa_volta_a_pendencia(app, admin_user):
    from app.services.producao_pendente import dispensar_item, reverter_dispensa
    r = _receita()
    p = _ordem(r, hoje() - timedelta(days=1), alvo=50)
    dispensar_item(p.itens[0].id, admin_user.id)
    assert r.id not in pendencias_por_receita()

    reverter_dispensa(p.itens[0].id)
    assert pendencias_por_receita()[r.id]['vencido'] == 50      # voltou
    assert listar_pendencias()['dispensadas'] == []


def test_dispensar_item_inexistente(app, admin_user):
    from app.services.producao_pendente import dispensar_item
    res = dispensar_item(999999, admin_user.id)
    assert res['ok'] is False


def test_rota_dispensar(app, admin_user):
    r = _receita()
    p = _ordem(r, hoje() - timedelta(days=1), alvo=50)
    c = _login(app, admin_user)
    resp = c.post('/telaindustriateste/auditoria/dispensar',
                  data={'item_id': p.itens[0].id, 'dias': 30})
    assert resp.status_code in (302, 303)
    assert r.id not in pendencias_por_receita()


def test_rota_dispensar_exige_admin(app):
    c = app.test_client()
    resp = c.post('/telaindustriateste/auditoria/dispensar',
                  data={'item_id': 1})
    assert resp.status_code in (301, 302, 403)


# ── Model B: dispensado some de tudo (padeiro/gantt/produzir) ──────────────
def test_produzir_item_dispensado_e_bloqueado(app, admin_user):
    """Produzir um item DISPENSADO é barrado ANTES de tocar estoque/MP."""
    from app.models import EstoqueProducao
    from app.services.producao import produzir_item_plano
    from app.services.producao_pendente import dispensar_item
    r = _receita()
    p = _ordem(r, hoje(), alvo=20)
    dispensar_item(p.itens[0].id, admin_user.id)

    res = produzir_item_plano(p.itens[0].id, 20, admin_user.id)
    assert res['ok'] is False
    assert 'dispensad' in res['erro'].lower()
    assert EstoqueProducao.query.filter_by(receita_id=r.id).count() == 0   # sem crédito


def test_gantt_nao_mostra_item_dispensado(app, admin_user):
    """montar_gantt ignora item dispensado (não vira tarefa do padeiro)."""
    from app.models import ReceitaEtapa
    from app.services.gantt import montar_gantt
    from app.services.producao_pendente import dispensar_item
    r = _receita('Pão Longo')
    db.session.add(ReceitaEtapa(receita_id=r.id, ordem=1, nome='Misturar',
                                duracao_min=30, equipamento='amassadeira'))
    db.session.commit()
    p = _ordem(r, hoje(), alvo=50)

    g = montar_gantt(hoje())
    assert g is not None and any(pr.get('receita_id') == r.id
                                 for pr in g['produtos'])   # aparece antes
    dispensar_item(p.itens[0].id, admin_user.id)
    g2 = montar_gantt(hoje())
    nomes = [pr.get('receita_id') for pr in (g2['produtos'] if g2 else [])]
    assert r.id not in nomes                                # sumiu depois da dispensa


def test_padeiro_plano_do_dia_esconde_dispensado(app, admin_user):
    """O plano do dia do padeiro não lista item dispensado."""
    from app.blueprints.padeiro.routes import _plano_do_dia
    from app.services.producao_pendente import dispensar_item, reverter_dispensa
    r = _receita('Focaccia')
    p = _ordem(r, hoje(), alvo=30)

    with app.test_request_context():
        pd = _plano_do_dia(hoje())
        assert any(it['receita_id'] == r.id for grp in pd['grupos']
                   for it in grp['itens']) or any(
            it['receita_id'] == r.id for it in pd['solos'])   # aparece antes
        dispensar_item(p.itens[0].id, admin_user.id)
        pd2 = _plano_do_dia(hoje())
        ids = [it['receita_id'] for grp in pd2['grupos'] for it in grp['itens']]
        ids += [it['receita_id'] for it in pd2['solos']]
        assert r.id not in ids                                 # sumiu
        reverter_dispensa(p.itens[0].id)
        pd3 = _plano_do_dia(hoje())
        ids3 = [it['receita_id'] for grp in pd3['grupos'] for it in grp['itens']]
        ids3 += [it['receita_id'] for it in pd3['solos']]
        assert r.id in ids3                                    # undo reabre
