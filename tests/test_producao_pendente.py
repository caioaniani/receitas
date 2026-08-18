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
    app.config['UI_V2_ENABLED'] = False  # contrato da tela CLASSICA (viva via cookie ui_classic/?legacy=1)
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


# ── dispensa em LOTE (checkboxes: dar OK em vários de uma vez) ─────────────
def test_dispensar_itens_lote(app, admin_user):
    from app.services.producao_pendente import dispensar_itens
    r = _receita()
    p1 = _ordem(r, hoje() - timedelta(days=1), alvo=50)
    p2 = _ordem(r, hoje() - timedelta(days=2), alvo=30)
    ids = [p1.itens[0].id, p2.itens[0].id]

    res = dispensar_itens(ids, admin_user.id)
    assert res['ok'] is True and res['n'] == 2
    assert r.id not in pendencias_por_receita()                 # os dois sumiram
    assert len(listar_pendencias()['dispensadas']) == 2


def test_dispensar_itens_ignora_invalidos_e_ja_dispensados(app, admin_user):
    from app.services.producao_pendente import dispensar_item, dispensar_itens
    r = _receita()
    p1 = _ordem(r, hoje() - timedelta(days=1), alvo=50)
    p2 = _ordem(r, hoje() - timedelta(days=2), alvo=30)
    dispensar_item(p1.itens[0].id, admin_user.id)               # já dispensado

    # p1 já dispensado (não reconta), 999999 inexistente, 'x' inválido, p2 novo
    res = dispensar_itens([p1.itens[0].id, 999999, 'x', p2.itens[0].id],
                          admin_user.id)
    assert res['n'] == 1                                        # só o p2 conta
    assert r.id not in pendencias_por_receita()


def test_dispensar_itens_lista_vazia(app, admin_user):
    from app.services.producao_pendente import dispensar_itens
    res = dispensar_itens([], admin_user.id)
    assert res['ok'] is False and res['n'] == 0


def test_rota_dispensar_lote(app, admin_user):
    r = _receita()
    p1 = _ordem(r, hoje() - timedelta(days=1), alvo=50)
    p2 = _ordem(r, hoje() - timedelta(days=2), alvo=30)
    c = _login(app, admin_user)
    resp = c.post('/telaindustriateste/auditoria/dispensar-lote',
                  data={'ids': [p1.itens[0].id, p2.itens[0].id], 'dias': 30})
    assert resp.status_code in (302, 303)
    assert r.id not in pendencias_por_receita()


def test_rota_dispensar_lote_exige_admin(app):
    c = app.test_client()
    resp = c.post('/telaindustriateste/auditoria/dispensar-lote',
                  data={'ids': [1]})
    assert resp.status_code in (301, 302, 403)


def test_auditoria_tem_checkboxes_e_botao_lote(app, admin_user):
    """A tela de auditoria expõe checkbox por linha + selecionar tudo + botão
    de dar OK em lote."""
    r = _receita()
    _ordem(r, hoje() - timedelta(days=1), alvo=50)
    c = _login(app, admin_user)
    html = c.get('/telaindustriateste/auditoria').get_data(as_text=True)
    assert 'aud-check' in html                                  # checkbox por linha
    assert 'aud-all' in html                                    # marcar todos (por tabela)
    assert 'Selecionar tudo' in html                            # botão global
    assert 'aud-bulk-btn' in html                               # botão de lote
    assert 'dispensar-lote' in html                             # action do form


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


# ── reagendar_para_hoje (mandar a falta pra produção de HOJE) ──────────────
def test_reagendar_move_falta_pra_hoje(app):
    from app.services.producao_pendente import reagendar_para_hoje
    r = _receita('Foccacia')
    old = _ordem(r, hoje() - timedelta(days=2), alvo=10, produzido=3)  # falta 7
    item = old.itens[0]

    res = reagendar_para_hoje([item.id], user_id=None)
    assert res == {'movidos': 1, 'unidades': 7}

    plano_hoje = (PlanejamentoProducao.query
                  .filter_by(data=hoje(), origem='cronograma').first())
    assert plano_hoje is not None
    assert plano_hoje.enviado_ao_padeiro is True          # padeiro vê em /padeiro
    it_hoje = plano_hoje.itens[0]
    assert it_hoje.receita_id == r.id
    assert it_hoje.qtd_alvo == 7 and it_hoje.produzido_qtd == 0
    # ordem antiga fechada: alvo cai pro produzido -> falta 0 (sai da auditoria)
    db.session.refresh(item)
    assert item.qtd_alvo == 3 and item.produzido_qtd == 3


def test_reagendar_produzido_zero_remove_ordem_antiga(app):
    from app.services.producao_pendente import reagendar_para_hoje
    r = _receita('Pao Frances')
    old = _ordem(r, hoje() - timedelta(days=1), alvo=8, produzido=0)   # nada feito
    item_id = old.itens[0].id

    reagendar_para_hoje([item_id], user_id=None)

    assert db.session.get(PlanejamentoItem, item_id) is None           # removida
    plano_hoje = (PlanejamentoProducao.query
                  .filter_by(data=hoje(), origem='cronograma').first())
    assert plano_hoje.itens[0].qtd_alvo == 8


def test_reagendar_soma_em_receita_ja_no_plano_de_hoje(app):
    from app.services.producao_pendente import reagendar_para_hoje
    r = _receita('Brioche')
    _ordem(r, hoje(), alvo=5)                              # ja tem 5 hoje
    old = _ordem(r, hoje() - timedelta(days=1), alvo=10, produzido=3)  # falta 7
    item_id = old.itens[0].id

    reagendar_para_hoje([item_id], user_id=None)

    plano_hoje = (PlanejamentoProducao.query
                  .filter_by(data=hoje(), origem='cronograma').first())
    itens_r = [it for it in plano_hoje.itens if it.receita_id == r.id]
    assert len(itens_r) == 1                               # nao duplicou
    assert itens_r[0].qtd_alvo == 12                       # 5 + 7


# ── produzido_no_dia (o que o padeiro confirmou ontem) ─────────────────────
def test_produzido_no_dia_le_movimentos_de_ontem(app):
    from datetime import datetime, time

    from app.models import EstoqueProducao, MovEstoqueProducao
    from app.services.producao_pendente import produzido_no_dia
    r = _receita('Sourdough')
    ep = EstoqueProducao(receita_id=r.id, quantidade=0)
    db.session.add(ep)
    db.session.flush()
    ontem = hoje() - timedelta(days=1)
    db.session.add_all([
        # produzido ONTEM (conta)
        MovEstoqueProducao(estoque_producao_id=ep.id, tipo='producao',
                           quantidade=8,
                           data=datetime.combine(ontem, time(12, 0))),
        # produzido HOJE (fora da janela de ontem)
        MovEstoqueProducao(estoque_producao_id=ep.id, tipo='producao',
                           quantidade=5,
                           data=datetime.combine(hoje(), time(9, 0))),
        # balanco de ontem (nao e 'producao' -> nao conta)
        MovEstoqueProducao(estoque_producao_id=ep.id, tipo='balanco_entrada',
                           quantidade=99,
                           data=datetime.combine(ontem, time(10, 0))),
    ])
    db.session.commit()

    res = produzido_no_dia()                               # default: ontem
    assert res['dia'] == ontem
    assert res['total'] == 8                               # so o 'producao' de ontem
    assert len(res['itens']) == 1
    assert res['itens'][0]['receita_id'] == r.id
    assert res['itens'][0]['receita_nome'] == 'Sourdough'
    assert res['itens'][0]['qtd'] == 8


def test_auditoria_pagina_renderiza_com_reagendar_e_produzido(app, admin_user):
    """Render de ponta a ponta (rota -> serviço -> template): a auditoria tem o
    botão de reagendar (checkbox+form) e a seção 'Produzido ontem'."""
    r = _receita('Foccacia')
    _ordem(r, hoje() - timedelta(days=2), alvo=10, produzido=3)   # uma vencida
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(admin_user.id)
        s['_fresh'] = True
    resp = c.get('/telaindustriateste/auditoria')
    assert resp.status_code == 200
    assert b'aud-reagendar-btn' in resp.data                     # botão "produzir hoje"
    assert b'auditoria/reagendar' in resp.data                   # formaction da rota
    assert 'Produzido ontem'.encode() in resp.data               # seção nova


# ── REGRESSÃO 02/07: reagendado NÃO pode sumir quando o plano é re-enviado ──
def test_reagendado_sobrevive_ao_reenviar_plano_de_hoje(app, admin_user):
    """O caso do padeiro ("cadê os pães?"): admin reagenda as vencidas pra
    HOJE, depois envia o plano do dia (fluxo normal) — o re-sync reconstruía
    os itens do GRID e apagava os reagendados. Agora a parcela extra
    (qtd_extra) sobrevive e o padeiro vê."""
    from app.blueprints.padeiro.routes import _plano_do_dia
    from app.services.producao import enviar_plano_do_dia
    from app.services.producao_pendente import reagendar_para_hoje
    r = _receita('Sourdough 7 Grãos')
    old = _ordem(r, hoje() - timedelta(days=1), alvo=50, produzido=0)  # vencida

    reagendar_para_hoje([old.itens[0].id], user_id=admin_user.id)
    # fluxo normal do dia: (re)enviar o plano de hoje pro padeiro — o grid de
    # hoje NÃO tem demanda desta receita (sem pedidos), então antes do fix o
    # sync apagava o item reagendado aqui.
    enviar_plano_do_dia(hoje(), user_id=admin_user.id)

    plano_hoje = (PlanejamentoProducao.query
                  .filter_by(data=hoje(), origem='cronograma').first())
    assert plano_hoje is not None
    itens = {it.receita_id: it for it in plano_hoje.itens}
    assert r.id in itens                                # NÃO sumiu
    assert itens[r.id].qtd_alvo == 50
    assert itens[r.id].qtd_extra == 50
    # e o padeiro VÊ na tela dele
    with app.test_request_context():
        pd = _plano_do_dia(hoje())
    ids = [it['receita_id'] for grp in pd['grupos'] for it in grp['itens']]
    ids += [it['receita_id'] for it in pd['solos']]
    assert r.id in ids


def test_reagendado_soma_com_alvo_do_grid_no_reenvio(app, admin_user):
    """Receita que TAMBÉM tem demanda no grid de hoje: re-enviar recalcula o
    grid e SOMA o extra (grid + reagendado), não sobrescreve."""
    from app.models import Loja, PedidoLoja
    from app.models import PedidoItem as PI
    from app.services.producao import enviar_plano_do_dia
    from app.services.producao_pendente import reagendar_para_hoje
    r = _receita('Croissant')
    loja = Loja(nome='Centro', ativa=True)
    db.session.add(loja)
    db.session.flush()
    # pedido firme pra HOJE -> o grid de hoje tem 30 desta receita
    ped = PedidoLoja(loja_id=loja.id, status='pendente', data_entrega=hoje(),
                     data_pedido=hoje())
    db.session.add(ped)
    db.session.flush()
    db.session.add(PI(pedido_id=ped.id, receita_id=r.id, quantidade=30))
    db.session.commit()
    old = _ordem(r, hoje() - timedelta(days=1), alvo=20, produzido=0)  # falta 20

    reagendar_para_hoje([old.itens[0].id], user_id=admin_user.id)
    enviar_plano_do_dia(hoje(), user_id=admin_user.id)

    plano_hoje = (PlanejamentoProducao.query
                  .filter_by(data=hoje(), origem='cronograma').first())
    it = next(x for x in plano_hoje.itens if x.receita_id == r.id)
    assert it.qtd_extra == 20
    assert it.qtd_alvo == 50                            # 30 do grid + 20 extra


def test_reagendar_reabre_item_dispensado_de_hoje(app, admin_user):
    """Se o plano de hoje já tinha a receita DISPENSADA (a tela do padeiro
    esconde), reagendar pra ela REABRE o item — antes a falta somava num item
    oculto e sumia do mesmo jeito."""
    from app.services.producao_pendente import dispensar_item, reagendar_para_hoje
    r = _receita('Baguette')
    p_hoje = _ordem(r, hoje(), alvo=10, produzido=0)    # plano de HOJE
    dispensar_item(p_hoje.itens[0].id, admin_user.id)   # dispensado (oculto)
    old = _ordem(r, hoje() - timedelta(days=1), alvo=5, produzido=0)

    reagendar_para_hoje([old.itens[0].id], user_id=admin_user.id)

    it = p_hoje.itens[0]
    db.session.refresh(it)
    assert it.dispensada_em is None                     # reaberto
    assert it.qtd_alvo == 15                            # 10 + 5
    assert it.qtd_extra == 5


def test_planejamento_item_e_auditado(app, admin_user):
    """REGRESSÃO 02/07: as quantidades das ordens vivem no PlanejamentoItem —
    sem auditoria neles, itens apagados eram irrecuperáveis. Delete/update
    agora deixam rastro no AuditLog."""
    from app.models import AuditLog
    r = _receita('Pão Auditado')
    p = _ordem(r, hoje(), alvo=40)
    item = p.itens[0]
    item.qtd_alvo = 55
    db.session.commit()
    logs = AuditLog.query.filter_by(tabela='planejamento_item',
                                    registro_id=item.id).all()
    assert any(log.acao == 'update' for log in logs)
    iid = item.id
    db.session.delete(item)
    db.session.commit()
    logs = AuditLog.query.filter_by(tabela='planejamento_item',
                                    registro_id=iid).all()
    assert any(log.acao == 'delete' for log in logs)
