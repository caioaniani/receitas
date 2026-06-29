"""Cronograma de producao POR DIA (previsao_producao.cronograma_producao) +
rota /telaindustriateste.

Distribui a producao por dia acompanhando as entregas (deslocado pelo lead),
descontando o estoque dos primeiros dias.
"""
from datetime import timedelta

from app.extensions import db
from app.models import EstoqueProducao, Loja, PedidoItem, PedidoLoja, Receita
from app.services.previsao_producao import cronograma_producao
from app.utils import hoje


def _receita(nome='Pão'):
    r = Receita(nome=nome, categoria='Paes', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=1000.0)
    db.session.add(r)
    db.session.commit()
    return r


def _loja(nome='Loja A'):
    loja = Loja(nome=nome, ativa=True)
    db.session.add(loja)
    db.session.commit()
    return loja


def _pedido(loja, status, data_entrega, receita, qtd):
    p = PedidoLoja(loja_id=loja.id, status=status, data_entrega=data_entrega,
                   data_pedido=data_entrega)
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p.id, receita_id=receita.id,
                              quantidade=qtd))
    db.session.commit()
    return p


def _rec_out(crono, rid):
    return next((x for x in crono['receitas'] if x['receita_id'] == rid), None)


def test_distribui_firme_por_dia(app):
    """Pedido firme cai no dia de producao = dia de entrega (lead 0)."""
    loja = _loja()
    r = _receita()
    _pedido(loja, 'pendente', hoje() + timedelta(days=2), r, 50)

    crono = cronograma_producao(horizonte_dias=7, janela_semanas=6)
    rr = _rec_out(crono, r.id)
    assert rr is not None
    assert rr['por_dia'][2]['qtd'] == 50    # entrega hoje+2, lead 0
    assert rr['por_dia'][0]['qtd'] == 0     # nada hoje
    assert rr['total'] == 50


def test_lead_antecipa_producao(app):
    """Com lead 2, a producao de uma entrega em hoje+2 cai HOJE."""
    loja = _loja()
    r = _receita()
    r.dias_producao = 2
    db.session.commit()
    _pedido(loja, 'pendente', hoje() + timedelta(days=2), r, 30)

    crono = cronograma_producao(horizonte_dias=7)
    rr = _rec_out(crono, r.id)
    assert rr['por_dia'][0]['qtd'] == 30    # produz hoje p/ entregar em hoje+2


def test_estoque_cobre_primeiros_dias(app):
    """Estoque pronto desconta dos dias mais proximos primeiro."""
    loja = _loja()
    r = _receita()
    db.session.add(EstoqueProducao(receita_id=r.id, quantidade=20))
    db.session.commit()
    _pedido(loja, 'pendente', hoje() + timedelta(days=1), r, 30)
    _pedido(loja, 'pendente', hoje() + timedelta(days=3), r, 30)

    crono = cronograma_producao(horizonte_dias=7)
    rr = _rec_out(crono, r.id)
    assert rr['por_dia'][1]['qtd'] == 10    # 30 - 20 de estoque
    assert rr['por_dia'][3]['qtd'] == 30
    assert rr['total'] == 40


def test_lead_com_estoque_unifica_com_balanco(app):
    """Unificado E correto: o cronograma distribui o "Produzir" do BALANÇO, e o
    estoque é alocado cronologicamente. Com lead 2 e estoque 40: a entrega de
    hoje+1 (iminente, 30) consome 30 do estoque -> sobram 10 efetivos pra a
    janela (hoje+2 + hoje+4 = 60), logo produzir = 50. O estoque NÃO é contado
    duas vezes (bug que subproduziria pra 20)."""
    from app.services.previsao_producao import balanco_industria
    loja = _loja()
    r = _receita()
    r.dias_producao = 2
    db.session.commit()
    db.session.add(EstoqueProducao(receita_id=r.id, quantidade=40))
    db.session.commit()
    _pedido(loja, 'pendente', hoje() + timedelta(days=1), r, 30)
    _pedido(loja, 'pendente', hoje() + timedelta(days=2), r, 30)
    _pedido(loja, 'pendente', hoje() + timedelta(days=4), r, 30)

    crono = cronograma_producao(horizonte_dias=7)
    rr = _rec_out(crono, r.id)
    bal = balanco_industria(horizonte_dias=7, inicio_offset_dias=0,
                            usar_cache=False)
    bit = next(i for i in bal['itens'] if i['receita_id'] == r.id)
    assert bit['em_estoque_efetivo'] == 10       # 40 - 30 da entrega iminente
    assert bit['produzir'] == 50                 # 60 - 10 efetivo (NÃO 20)
    assert rr['total'] == bit['produzir']         # a unificação
    # hoje+2: falta 20 -> produz dia 0 (hoje); hoje+4: 30 -> dia 2 (hoje+4-lead2)
    assert rr['por_dia'][0]['qtd'] == 20
    assert rr['por_dia'][2]['qtd'] == 30


def test_cronograma_total_bate_com_balanco(app):
    """Invariante da unificação: para cada receita, o total do cronograma é
    EXATAMENTE o 'Produzir' do balanço (mesmas receitas, mesmos totais), em
    qualquer início — e a soma das células de cada receita == o total."""
    from app.services.previsao_producao import balanco_industria
    loja_a = _loja('A')
    loja_b = _loja('B')
    r1 = _receita('Pão')
    r2 = _receita('Croissant')
    r1.dias_producao = 1
    r2.dias_producao = 1
    db.session.add(EstoqueProducao(receita_id=r1.id, quantidade=15))
    db.session.commit()
    hoje_d = hoje()
    for d in (2, 3, 5):
        _pedido(loja_a, 'pendente', hoje_d + timedelta(days=d), r1, 20)
        _pedido(loja_b, 'pendente', hoje_d + timedelta(days=d), r2, 12)

    for off in (0, 1, 2):
        crono = cronograma_producao(horizonte_dias=7, inicio_offset_dias=off)
        bal = balanco_industria(horizonte_dias=7, inicio_offset_dias=off,
                                usar_cache=False)
        prod = {i['receita_id']: i['produzir']
                for i in bal['itens'] if i['produzir'] > 0}
        crono_tot = {x['receita_id']: x['total'] for x in crono['receitas']}
        assert crono_tot == prod, f'offset {off}: {crono_tot} != {prod}'
        for x in crono['receitas']:
            assert sum(c['qtd'] for c in x['por_dia']) == x['total']


def _receita_amassadeira(nome, rend, peso_base, cap):
    """Receita que passa pela amassadeira: rend un/receita, massa = peso_base
    (1 ingrediente mp a 100%), capacidade cap g. unidades/fornada = cap*rend/
    massa_base = cap*rend/peso_base."""
    from app.models import ReceitaIngrediente
    r = Receita(nome=nome, categoria='Paes', rendimento_qtd=rend,
                rendimento_unidade='un', peso_base=float(peso_base),
                capacidade_amassadeira_g=cap)
    db.session.add(r)
    db.session.flush()
    db.session.add(ReceitaIngrediente(receita_id=r.id, tipo='mp',
                                      ingrediente_nome='Farinha', porcentagem=100))
    db.session.commit()
    return r


def test_dribble_diario_rola_pro_proximo_dia(app):
    """Anti-'produzir 1 pao': um dia com producao irrisoria (< fracao de UMA
    fornada) NAO fica sozinho — rola pro proximo, consolidando no dia de
    producao real. Total preservado."""
    loja = _loja()
    # cap=5000, rend=50, massa_base=peso_base=5000 -> unid/fornada=50, minimo=10.
    r = _receita_amassadeira('Sourdough', rend=50, peso_base=5000, cap=5000)
    # 1 un pra entregar hoje (dribble) + 60 pra hoje+3 (producao real).
    _pedido(loja, 'pendente', hoje(), r, 1)
    _pedido(loja, 'pendente', hoje() + timedelta(days=3), r, 60)

    crono = cronograma_producao(horizonte_dias=7, janela_semanas=6)
    rr = _rec_out(crono, r.id)
    assert rr['total'] == 61                       # balanco: 61 a produzir
    # o "1" do dia 0 nao fica sozinho — rolou e somou no dia da producao real
    assert rr['por_dia'][0]['qtd'] == 0
    assert rr['por_dia'][3]['qtd'] == 61
    minimo = 10                                    # 20% de 50 un/fornada
    for c in rr['por_dia']:
        assert not (0 < c['qtd'] < minimo), f"dribble nao consolidado: {c}"
    assert sum(c['qtd'] for c in rr['por_dia']) == rr['total']


def test_sem_amassadeira_nao_consolida(app):
    """Item que NAO passa pela amassadeira (cap=0: Moeda, creme) nao tem
    'fornada' a desperdicar — produzir 1 num dia continua valendo, nao rola."""
    loja = _loja()
    r = Receita(nome='Moeda', categoria='Paes', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=10.0,
                capacidade_amassadeira_g=0)
    db.session.add(r)
    db.session.commit()
    _pedido(loja, 'pendente', hoje() + timedelta(days=1), r, 1)

    crono = cronograma_producao(horizonte_dias=7, janela_semanas=6)
    rr = _rec_out(crono, r.id)
    assert rr['por_dia'][1]['qtd'] == 1            # produz o 1 no dia, sem rolar
    assert rr['total'] == 1


def test_equilibrar_enche_dia_ocioso(app):
    """Modo equilibrar: cada receita INTEIRA num dia; adianta receitas pra
    encher dia vazio (nivela fornadas). cap pequeno -> nao dispara a
    consolidacao anti-dribble (minimo=1)."""
    loja = _loja()
    r1 = _receita_amassadeira('Pao A', rend=50, peso_base=5000, cap=500)
    r2 = _receita_amassadeira('Pao B', rend=50, peso_base=5000, cap=500)
    d1 = hoje() + timedelta(days=1)
    _pedido(loja, 'pendente', d1, r1, 30)
    _pedido(loja, 'pendente', d1, r2, 30)

    # SEM equilibrar: ambas caem no dia 1 (entrega); dia 0 ocioso.
    base = cronograma_producao(horizonte_dias=2, inicio_offset_dias=0)
    a = _rec_out(base, r1.id)
    b = _rec_out(base, r2.id)
    assert a['por_dia'][0]['qtd'] == 0 and b['por_dia'][0]['qtd'] == 0
    assert a['por_dia'][1]['qtd'] == 30 and b['por_dia'][1]['qtd'] == 30

    # COM equilibrar: cada receita inteira num unico dia; dia 0 nao fica ocioso.
    eq = cronograma_producao(horizonte_dias=2, inicio_offset_dias=0,
                             equilibrar=True)
    ae = _rec_out(eq, r1.id)
    be = _rec_out(eq, r2.id)
    for rr in (ae, be):
        dias_com = [c for c in rr['por_dia'] if c['qtd'] > 0]
        assert len(dias_com) == 1, 'receita nao pode ser dividida'
        assert dias_com[0]['qtd'] == 30
        assert sum(c['qtd'] for c in rr['por_dia']) == rr['total'] == 30
    assert (ae['por_dia'][0]['qtd'] > 0) or (be['por_dia'][0]['qtd'] > 0), \
        'equilibrar deveria adiantar uma receita pro dia ocioso'
    assert {ae['por_dia'][0]['qtd'], be['por_dia'][0]['qtd']} == {0, 30}


def test_rota_telaindustriateste(app, admin_user):
    loja = _loja()
    r = _receita()
    _pedido(loja, 'pendente', hoje() + timedelta(days=1), r, 10)

    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    resp = client.get('/telaindustriateste/')
    assert resp.status_code == 200
    assert 'cronograma' in resp.get_data(as_text=True).lower()


def test_aprovar_cria_plano_do_dia(app, admin_user):
    from app.models import PlanejamentoProducao
    from app.services.producao import aprovar_plano_do_dia

    loja = _loja()
    r = _receita()
    d2 = hoje() + timedelta(days=2)
    _pedido(loja, 'pendente', d2, r, 50)

    plano = aprovar_plano_do_dia(d2, admin_user.id, horizonte_dias=7)
    assert plano is not None
    assert plano.data == d2
    assert plano.origem == 'cronograma'
    assert plano.status == 'aprovado'
    assert len(plano.itens) == 1
    it = plano.itens[0]
    assert it.receita_id == r.id
    assert it.qtd_alvo == 50
    assert it.produzido_qtd == 0
    assert PlanejamentoProducao.query.filter_by(
        data=d2, origem='cronograma').count() == 1


def test_reaprovar_substitui(app, admin_user):
    from app.models import PlanejamentoProducao
    from app.services.producao import aprovar_plano_do_dia

    loja = _loja()
    r = _receita()
    d2 = hoje() + timedelta(days=2)
    _pedido(loja, 'pendente', d2, r, 50)

    aprovar_plano_do_dia(d2, admin_user.id, horizonte_dias=7)
    aprovar_plano_do_dia(d2, admin_user.id, horizonte_dias=7)
    # re-aprovar nao duplica
    assert PlanejamentoProducao.query.filter_by(
        data=d2, origem='cronograma').count() == 1


def test_rota_aprovar(app, admin_user):
    from app.models import PlanejamentoProducao

    loja = _loja()
    r = _receita()
    d2 = hoje() + timedelta(days=2)
    _pedido(loja, 'pendente', d2, r, 30)

    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    resp = client.post('/telaindustriateste/aprovar',
                       data={'data': d2.isoformat(), 'horizonte': 7,
                             'janela': 6})
    assert resp.status_code == 302
    assert PlanejamentoProducao.query.filter_by(
        data=d2, origem='cronograma').first() is not None


# ── fluxo 2 passos: aprovar (rascunho) -> enviar (padeiro vê) ──────────────────

def test_aprovar_cria_rascunho_padeiro_nao_ve(app, admin_user):
    """Aprovar cria a ordem como RASCUNHO (enviado_ao_padeiro=False); o padeiro
    NÃO vê (Produção do dia e Fluxograma vazios) até o passo 'enviar'."""
    from app.blueprints.padeiro.routes import _plano_do_dia
    from app.models import PlanejamentoProducao
    from app.services.gantt import montar_gantt
    from app.services.producao import aprovar_plano_do_dia

    loja = _loja()
    r = _receita()
    d2 = hoje() + timedelta(days=2)
    _pedido(loja, 'pendente', d2, r, 50)
    plano = aprovar_plano_do_dia(d2, admin_user.id, horizonte_dias=7)
    assert plano.enviado_ao_padeiro is False        # rascunho
    assert _plano_do_dia(d2) is None                 # padeiro não vê
    assert montar_gantt(d2) is None
    # passo 2: enviar
    from app.services.producao import enviar_plano_do_dia
    enviar_plano_do_dia(d2)
    db.session.expire_all()
    assert PlanejamentoProducao.query.filter_by(
        data=d2, origem='cronograma').first().enviado_ao_padeiro is True
    assert _plano_do_dia(d2) is not None             # agora o padeiro vê


def test_rota_enviar(app, admin_user):
    from app.models import PlanejamentoItem, PlanejamentoProducao
    r = _receita('Pão Enviar')
    plano = PlanejamentoProducao(data=hoje(), origem='cronograma',
                                 enviado_ao_padeiro=False)
    db.session.add(plano); db.session.flush()
    db.session.add(PlanejamentoItem(planejamento_id=plano.id, receita_id=r.id,
                                    multiplicador=1, qtd_alvo=10))
    db.session.commit()
    pid = plano.id
    c = app.test_client()
    c.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
           follow_redirects=True)
    resp = c.post('/telaindustriateste/enviar', data={'data': hoje().isoformat()})
    assert resp.status_code in (302, 303)
    db.session.expire_all()
    assert db.session.get(PlanejamentoProducao, pid).enviado_ao_padeiro is True


def test_plano_antigo_sem_flag_continua_visivel(app, admin_user):
    """Ordem antiga (criada sem o flag) tem DEFAULT True -> padeiro continua
    vendo (não quebra produção em andamento)."""
    from app.blueprints.padeiro.routes import _plano_do_dia
    from app.models import PlanejamentoItem, PlanejamentoProducao
    r = _receita('Pão Antigo')
    plano = PlanejamentoProducao(data=hoje(), origem='cronograma')  # sem flag
    db.session.add(plano); db.session.flush()
    db.session.add(PlanejamentoItem(planejamento_id=plano.id, receita_id=r.id,
                                    multiplicador=1, qtd_alvo=10))
    db.session.commit()
    assert _plano_do_dia(hoje()) is not None         # default True -> visível


# ── excluir ordem de produção enviada (desfazer envio errado) ──────────────

def test_excluir_plano_enviado_sem_producao(app, admin_user):
    from app.models import PlanejamentoProducao
    from app.services.producao import aprovar_plano_do_dia, enviar_plano_do_dia, excluir_plano_do_dia
    loja = _loja()
    r = _receita()
    d2 = hoje() + timedelta(days=2)
    _pedido(loja, 'pendente', d2, r, 50)
    aprovar_plano_do_dia(d2, admin_user.id, horizonte_dias=7)
    enviar_plano_do_dia(d2)

    res = excluir_plano_do_dia(d2)
    assert res['ok'] is True
    assert PlanejamentoProducao.query.filter_by(
        data=d2, origem='cronograma').first() is None


def test_excluir_bloqueia_se_ja_produziu(app, admin_user):
    from app.models import PlanejamentoProducao
    from app.services.producao import aprovar_plano_do_dia, excluir_plano_do_dia
    loja = _loja()
    r = _receita()
    d2 = hoje() + timedelta(days=2)
    _pedido(loja, 'pendente', d2, r, 50)
    plano = aprovar_plano_do_dia(d2, admin_user.id, horizonte_dias=7)
    plano.itens[0].produzido_qtd = 5            # padeiro ja produziu parte
    db.session.commit()

    res = excluir_plano_do_dia(d2)
    assert res['ok'] is False
    assert res['erro'] == 'ja_produzido' and res['produzido'] == 5
    # plano preservado (a producao real ja mexeu no estoque/MP)
    assert PlanejamentoProducao.query.filter_by(
        data=d2, origem='cronograma').first() is not None


def test_excluir_inexistente(app):
    from app.services.producao import excluir_plano_do_dia
    res = excluir_plano_do_dia(hoje() + timedelta(days=9))
    assert res['ok'] is False and res['erro'] == 'nao_encontrado'


def test_rota_excluir(app, admin_user):
    from app.models import PlanejamentoProducao
    from app.services.producao import aprovar_plano_do_dia, enviar_plano_do_dia
    loja = _loja()
    r = _receita()
    d2 = hoje() + timedelta(days=2)
    _pedido(loja, 'pendente', d2, r, 30)
    aprovar_plano_do_dia(d2, admin_user.id, horizonte_dias=7)
    enviar_plano_do_dia(d2)

    c = app.test_client()
    c.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
           follow_redirects=True)
    resp = c.post('/telaindustriateste/excluir',
                  data={'data': d2.isoformat(), 'horizonte': 7, 'janela': 6})
    assert resp.status_code == 302
    assert PlanejamentoProducao.query.filter_by(
        data=d2, origem='cronograma').first() is None
