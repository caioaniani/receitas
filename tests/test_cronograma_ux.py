"""UX do cronograma (/telaindustriateste): painel "próximos passos", trilha
de dias, ordenação do grid e a função "MP do dia" (explosão de matéria-prima
da produção de um dia vs estoque de MP — mesma conta da pré-baixa/baixa real).

Ficha dos cenários (mesma do test_pre_baixa_mp): 1000 g de MP (mp_direto) por
batida, peso_unitario 100 g → rendimento massa crua = 10 un/batida →
1 un = 100 g de MP.
"""
from datetime import timedelta

import pytest

from app.extensions import db
from app.models import (
    Loja,
    MateriaPrima,
    PedidoItem,
    PedidoLoja,
    Receita,
    ReceitaIngrediente,
)
from app.services.producao import mp_necessaria_do_dia
from app.utils import hoje


@pytest.fixture(autouse=True)
def _hoje_e_segunda_fixa(congela_hoje):
    """Producao so seg-sex (dono 17/08/2026) tornou o shaping do cronograma
    sensivel ao dia da semana — congela hoje() numa SEGUNDA pros cenarios
    hoje()+N deste arquivo cairem sempre em dia util, em qualquer dia em que
    a suite rode (ver conftest.congela_hoje)."""
    congela_hoje()

G_POR_UN = 100.0


def _cenario_mp(qtd=50, dias_entrega=2, estoque_mp=10000.0):
    mp = MateriaPrima(nome='Farinha UX', unidade='g', custo_por_kg=5.0,
                      estoque_atual=estoque_mp)
    r = Receita(nome='Pao UX', categoria='Paes', rendimento_qtd=10,
                rendimento_unidade='un', peso_base=1000.0, peso_unitario=100.0)
    loja = Loja(nome='Loja UX', ativa=True)
    db.session.add_all([mp, r, loja])
    db.session.flush()
    db.session.add(ReceitaIngrediente(receita_id=r.id, ingrediente_nome=mp.nome,
                                      tipo='mp_direto', porcentagem=1000.0))
    dd = hoje() + timedelta(days=dias_entrega)
    p = PedidoLoja(loja_id=loja.id, status='pendente', data_entrega=dd,
                   data_pedido=dd)
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p.id, receita_id=r.id, quantidade=qtd))
    db.session.commit()
    return mp, r


def _dia_com_producao(rid, horizonte=7):
    """Dia em que o grid agendou a produção (o teste segue o grid, não fixa a
    regra de antecedência)."""
    from datetime import date

    from app.services.previsao_producao import cronograma_producao
    crono = cronograma_producao(horizonte_dias=horizonte)
    rr = next(x for x in crono['receitas'] if x['receita_id'] == rid)
    iso = next(c['data'] for c in rr['por_dia'] if c['qtd'] > 0)
    return date.fromisoformat(iso)


def _login_admin(app, admin_user):
    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    return client


# ── mp_necessaria_do_dia (service) ─────────────────────────────────────────

def test_mp_do_dia_estoque_cobre(app):
    """50 un × 100 g = 5.000 g necessários; estoque 10.000 g → nada falta."""
    mp, r = _cenario_mp(qtd=50, estoque_mp=10000.0)
    dia = _dia_com_producao(r.id)
    res = mp_necessaria_do_dia(dia)
    assert res is not None
    item = next(x for x in res['itens'] if x['nome'] == mp.nome)
    assert item['necessario'] == 50 * G_POR_UN
    assert item['falta'] == 0
    assert res['faltam_n'] == 0
    assert res['receitas_n'] >= 1


def test_mp_do_dia_aponta_falta(app):
    """Estoque 1.000 g pra 5.000 g necessários → falta 4.000 g, item em
    falta vem PRIMEIRO na lista."""
    mp, r = _cenario_mp(qtd=50, estoque_mp=1000.0)
    dia = _dia_com_producao(r.id)
    res = mp_necessaria_do_dia(dia)
    item = res['itens'][0]          # falta primeiro
    assert item['nome'] == mp.nome
    assert item['falta'] == 4000.0
    assert res['faltam_n'] == 1


def test_mp_do_dia_usa_o_grid_editado(app):
    """A explosão parte do GRID (com override), não da sugestão original:
    editar a célula pra 80 un muda o necessário pra 8.000 g."""
    from app.services.cronograma_edit import editar_celula
    mp, r = _cenario_mp(qtd=50, estoque_mp=10000.0)
    dia = _dia_com_producao(r.id)
    res = editar_celula(r.id, dia.isoformat(), 80)
    assert res and not res.get('erro')
    out = mp_necessaria_do_dia(dia)
    item = next(x for x in out['itens'] if x['nome'] == mp.nome)
    assert item['necessario'] == 80 * G_POR_UN


def test_mp_do_dia_fora_do_grid(app):
    """Data fora do horizonte → None (a rota devolve 404)."""
    _cenario_mp()
    assert mp_necessaria_do_dia(hoje() + timedelta(days=30)) is None


def test_mp_do_dia_credita_reserva_do_proprio_dia(app, admin_user):
    """Dia já ENVIADO: a pré-baixa reservou a MP deste dia (debitou o
    estoque_atual) — sem creditar a reserva de volta, o insumo que está na
    prateleira reservado pra ESTA produção apareceria como falta (falso
    alarme no fluxo '🔄 atualizar produção')."""
    from app.services.producao import enviar_plano_do_dia
    mp, r = _cenario_mp(qtd=50, estoque_mp=5000.0)   # exatamente o necessário
    dia = _dia_com_producao(r.id)
    plano = enviar_plano_do_dia(dia, admin_user.id)
    assert plano is not None and plano.enviado_ao_padeiro
    db.session.refresh(mp)
    assert mp.estoque_atual == 0          # pré-baixa reservou tudo

    res = mp_necessaria_do_dia(dia)
    item = next(x for x in res['itens'] if x['nome'] == mp.nome)
    assert item['reservado'] == 5000.0
    assert item['estoque'] == 5000.0      # estoque físico + reserva deste dia
    assert item['falta'] == 0             # sem falso alarme
    assert res['faltam_n'] == 0
    assert res['reservado_total_n'] == 1


def test_mp_do_dia_aponta_ingrediente_sem_cadastro(app):
    """Ingrediente de ficha sem MP correspondente fica fora da conta (mesma
    semântica da calculadora), mas o modal AVISA — falta não passa em
    silêncio."""
    _mp, r = _cenario_mp()
    db.session.add(ReceitaIngrediente(receita_id=r.id,
                                      ingrediente_nome='Fermento Fantasma',
                                      tipo='mp_direto', porcentagem=10.0))
    db.session.commit()
    dia = _dia_com_producao(r.id)
    res = mp_necessaria_do_dia(dia)
    assert res['sem_cadastro'] == ['Fermento Fantasma']


# ── rota /telaindustriateste/mp-dia ────────────────────────────────────────

def test_rota_mp_dia_json(app, admin_user):
    mp, r = _cenario_mp(qtd=50, estoque_mp=1000.0)
    dia = _dia_com_producao(r.id)
    client = _login_admin(app, admin_user)
    d = client.get('/telaindustriateste/mp-dia?data=%s&horizonte=7'
                   % dia.isoformat()).get_json()
    assert d['ok'] is True
    assert d['faltam_n'] == 1
    assert d['itens'][0]['nome'] == mp.nome
    assert d['itens'][0]['unidade'] == 'g'


def test_rota_mp_dia_data_invalida(app, admin_user):
    client = _login_admin(app, admin_user)
    assert client.get('/telaindustriateste/mp-dia?data=banana').status_code == 400
    assert client.get('/telaindustriateste/mp-dia?data=%s'
                      % (hoje() + timedelta(days=30)).isoformat()
                      ).status_code == 404


# ── próximos passos + trilha de dias + ordenação (index) ──────────────────

def test_index_proximos_passos_enviar_hoje(app, admin_user):
    """Produção agendada pra HOJE sem ordem → o painel 'Próximos passos'
    aparece com o gesto de enviar; a trilha de dias e o seletor de ordenação
    também rendem."""
    loja = Loja(nome='Loja Passos', ativa=True)
    r = Receita(nome='Pao Passos', categoria='Paes', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=1000.0)
    db.session.add_all([loja, r])
    db.session.flush()
    p = PedidoLoja(loja_id=loja.id, status='pendente', data_entrega=hoje(),
                   data_pedido=hoje())
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p.id, receita_id=r.id, quantidade=40))
    db.session.commit()

    client = _login_admin(app, admin_user)
    html = client.get('/telaindustriateste/').get_data(as_text=True)
    assert 'id="proximos-passos"' in html
    assert 'ainda não foi enviada ao padeiro' in html
    assert 'id="dias-strip"' in html
    assert 'id="crono-ordem"' in html
    assert 'mp-dia-btn' in html
    assert 'data-total=' in html      # atributo que alimenta a ordenação


def test_index_proximos_passos_rascunho(app, admin_user):
    """Plano aprovado como rascunho (não enviado) vira item acionável."""
    from app.services.producao import aprovar_plano_do_dia
    loja = Loja(nome='Loja Rasc', ativa=True)
    r = Receita(nome='Pao Rasc', categoria='Paes', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=1000.0)
    db.session.add_all([loja, r])
    db.session.flush()
    dd = hoje() + timedelta(days=2)
    p = PedidoLoja(loja_id=loja.id, status='pendente', data_entrega=dd,
                   data_pedido=dd)
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p.id, receita_id=r.id, quantidade=40))
    db.session.commit()
    plano = aprovar_plano_do_dia(dd, admin_user.id)
    assert plano is not None and plano.enviado_ao_padeiro is False

    client = _login_admin(app, admin_user)
    html = client.get('/telaindustriateste/').get_data(as_text=True)
    assert 'id="proximos-passos"' in html
    assert 'aprovado como <strong>rascunho</strong>' in html


def test_index_sem_pendencias_sem_painel(app, admin_user):
    """Nada acionável → o painel não aparece (a tela não grita à toa)."""
    client = _login_admin(app, admin_user)
    html = client.get('/telaindustriateste/').get_data(as_text=True)
    assert 'id="proximos-passos"' not in html


# ── Regra da véspera (dono, 10/07/2026): consumo de insumo DENTRO do lead
# não agenda produção inútil — massa feita hoje só vira croissant amanhã. ──

def _cenario_bom_vespera(qtd=100, dias_entrega=0, estoque_massa=0):
    """Croissant (lead 0, 1 bola por 50 un) consome 'Massa Vesp' (lead 1d).
    Entrega em hoje+dias_entrega → croissant produzido nesse dia."""
    from app.models import EstoqueProducao, ReceitaIngrediente
    loja = Loja(nome='Loja Vesp', ativa=True)
    massa = Receita(nome='Massa Vesp', categoria='Paes', rendimento_qtd=1,
                    rendimento_unidade='un', peso_base=1000.0,
                    dias_producao=1)
    cro = Receita(nome='Croissant Vesp', categoria='Folhados',
                  rendimento_qtd=50, rendimento_unidade='un',
                  peso_base=1000.0)
    db.session.add_all([loja, massa, cro])
    db.session.flush()
    db.session.add(ReceitaIngrediente(
        receita_id=cro.id, tipo='receita', sub_receita_id=massa.id,
        ingrediente_nome='Massa Vesp', porcentagem=1))
    if estoque_massa:
        db.session.add(EstoqueProducao(receita_id=massa.id,
                                       quantidade=estoque_massa))
    dd = hoje() + timedelta(days=dias_entrega)
    p = PedidoLoja(loja_id=loja.id, status='pendente', data_entrega=dd,
                   data_pedido=dd)
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p.id, receita_id=cro.id,
                              quantidade=qtd))
    db.session.commit()
    return massa, cro


def test_vespera_consumo_hoje_nao_agenda_massa_hoje(app):
    """Croissant produzido HOJE consome massa cuja véspera já passou: o grid
    NÃO agenda bolas pra hoje (não serviriam) — vira aviso insumo_sem_vespera
    com a falta e o dia do consumo."""
    from app.services.previsao_producao import cronograma_producao
    massa, cro = _cenario_bom_vespera(qtd=100, dias_entrega=0)
    crono = cronograma_producao(horizonte_dias=7)
    rc = next(x for x in crono['receitas'] if x['receita_id'] == cro.id)
    rm = next(x for x in crono['receitas'] if x['receita_id'] == massa.id)
    assert rc['por_dia'][0]['qtd'] == 100          # croissant hoje
    assert rm['total'] == 0                        # nenhuma bola agendada
    assert rm['consumo_janela'] == 2.0             # 100 × 1/50
    sv = rm['insumo_sem_vespera']
    assert sv['faltam'] == 2.0
    assert sv['lead'] == 1
    assert sv['dias'] == [hoje().isoformat()]


def test_vespera_estoque_pronto_cobre_sem_aviso(app):
    """Com massa da véspera em estoque, o consumo de hoje é coberto — sem
    aviso e sem produção."""
    from app.services.previsao_producao import cronograma_producao
    massa, _cro = _cenario_bom_vespera(qtd=100, dias_entrega=0,
                                       estoque_massa=5)
    crono = cronograma_producao(horizonte_dias=7)
    rm = next(x for x in crono['receitas'] if x['receita_id'] == massa.id)
    assert rm['total'] == 0
    assert 'insumo_sem_vespera' not in rm


def test_vespera_consumo_futuro_agenda_na_vespera(app):
    """Consumo com véspera DENTRO do grid segue normal: croissant de hoje+3
    puxa a massa pra hoje+2, sem aviso (regressão do comportamento bom)."""
    from app.services.previsao_producao import cronograma_producao
    massa, cro = _cenario_bom_vespera(qtd=100, dias_entrega=3)
    crono = cronograma_producao(horizonte_dias=7)
    rc = next(x for x in crono['receitas'] if x['receita_id'] == cro.id)
    rm = next(x for x in crono['receitas'] if x['receita_id'] == massa.id)
    dia_cro = next(i for i, c in enumerate(rc['por_dia']) if c['qtd'] > 0)
    dia_massa = next(i for i, c in enumerate(rm['por_dia']) if c['qtd'] > 0)
    assert rm['total'] == 2
    assert dia_massa == dia_cro - 1                # produzida na véspera
    assert 'insumo_sem_vespera' not in rm


def test_vespera_rota_renderiza_aviso(app, admin_user):
    """A tela mostra a tag '⚠ sem véspera' quando o aviso existe."""
    _cenario_bom_vespera(qtd=100, dias_entrega=0)
    client = _login_admin(app, admin_user)
    html = client.get('/telaindustriateste/').get_data(as_text=True)
    assert 'sem véspera' in html


# ── Edição manual que não cobre entrega em risco = aviso IMEDIATO (dono,
# 10/07/2026): sem esperar o "edição de dia anterior" do E3. Caso real:
# linha fixada em 600 com pedido firme de 1600 no sábado. ──

def _cenario_edicao(qtd_firme=100, dias_entrega=1):
    loja = Loja(nome='Loja Edit', ativa=True)
    r = Receita(nome='Pao Edit', categoria='Paes', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=1000.0)
    db.session.add_all([loja, r])
    db.session.flush()
    dd = hoje() + timedelta(days=dias_entrega)
    p = PedidoLoja(loja_id=loja.id, status='pendente', data_entrega=dd,
                   data_pedido=dd)
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p.id, receita_id=r.id,
                              quantidade=qtd_firme))
    db.session.commit()
    return r, dd


def test_edicao_hoje_que_descobre_entrega_vira_stale_na_hora(app):
    """Fixar a linha ABAIXO do pedido firme deixa a entrega em risco → o
    ⚠️ liga na hora, mesmo com a edição sendo de hoje."""
    from app.services.cronograma_edit import editar_celula
    from app.services.previsao_producao import cronograma_producao
    r, dd = _cenario_edicao(qtd_firme=100, dias_entrega=1)
    res = editar_celula(r.id, dd.isoformat(), 10)   # fixa 10 pra firme 100
    assert res and not res.get('erro')
    crono = cronograma_producao(horizonte_dias=7)
    rr = next(x for x in crono['receitas'] if x['receita_id'] == r.id)
    assert rr['editado'] is True
    assert rr['entregas_risco']                     # entrega descoberta
    assert rr.get('override_stale') is True         # aviso imediato
    assert rr['override_sugerido'] == 100


# ── Retorno nunca produzível (dono, 13/07/2026): no motor=vendas a
# receita-retorno tem histórico de venda PRÓPRIO (a venda do Nutella baixa o
# retorno da loja) e virava "previsto → produzir" no grid. Caso real:
# Croissant Tradicional - Retorno com previsto 164 e 45 un numa ordem. ──

def _cenario_retorno_com_vendas(estoque_industria=0):
    """Tradicional (retorno_receita_id → ret) + vendas semanais na linha do
    RETORNO da loja (como a venda do Nutella faz)."""
    from datetime import datetime as _dt
    from datetime import time as _time

    from app.models import EstoqueLoja, EstoqueProducao, MovEstoqueLoja
    loja = Loja(nome='Loja Ret', ativa=True)
    ret = Receita(nome='Croissant Ret - Retorno', categoria='Viennoiserie',
                  rendimento_qtd=1, rendimento_unidade='un', peso_base=1000.0)
    db.session.add_all([loja, ret])
    db.session.flush()
    trad = Receita(nome='Croissant Ret', categoria='Viennoiserie',
                   rendimento_qtd=50, rendimento_unidade='un',
                   peso_base=1000.0, retorno_receita_id=ret.id)
    db.session.add(trad)
    db.session.flush()
    el = EstoqueLoja(loja_id=loja.id, receita_id=ret.id, quantidade=100)
    db.session.add(el)
    if estoque_industria:
        db.session.add(EstoqueProducao(receita_id=ret.id,
                                       quantidade=estoque_industria))
    db.session.flush()
    alvo = hoje() + timedelta(days=2)
    for sem in range(1, 5):
        d = alvo - timedelta(days=7 * sem)
        db.session.add(MovEstoqueLoja(
            estoque_loja_id=el.id, tipo='venda_seru', quantidade=20,
            data=_dt.combine(d, _time(12, 0)), referencia='teste-retorno'))
    db.session.commit()
    return ret, trad


def test_retorno_nao_ganha_previsto_nem_producao_no_motor_vendas(app):
    """Balanço motor=vendas: a receita-retorno com histórico de venda fica
    com previsto 0 e produzir 0; o grid não agenda nenhuma célula dela."""
    from app.services.previsao_producao import (
        balanco_industria,
        cronograma_producao,
    )
    ret, _trad = _cenario_retorno_com_vendas(estoque_industria=5)
    bal = balanco_industria(motor='vendas', usar_cache=False)
    item = next((i for i in bal['itens'] if i['receita_id'] == ret.id), None)
    assert item is not None                    # estoque > 0 mantém visível
    assert item['retorno'] is True
    assert item['previsto'] == 0
    assert item['produzir'] == 0

    crono = cronograma_producao(horizonte_dias=7, motor='vendas')
    rr = next((x for x in crono['receitas'] if x['receita_id'] == ret.id),
              None)
    if rr is not None:                         # linha injetada (vendável)
        assert rr.get('retorno') is True
        assert rr['total'] == 0


def test_editar_celula_recusa_receita_retorno(app):
    from app.services.cronograma_edit import editar_celula
    ret, _trad = _cenario_retorno_com_vendas(estoque_industria=5)
    res = editar_celula(ret.id, (hoje() + timedelta(days=1)).isoformat(), 10)
    assert res is not None
    assert res.get('erro') == 'receita_retorno'


def test_override_legado_de_retorno_e_ignorado_e_nao_vai_pro_plano(app,
                                                                   admin_user):
    """Override criado ANTES do guard não re-põe produção de retorno no grid
    nem na ordem enviada ao padeiro."""
    from app.models import CronogramaOverride, PlanejamentoItem
    from app.services.previsao_producao import cronograma_producao
    from app.services.producao import enviar_plano_do_dia
    ret, _trad = _cenario_retorno_com_vendas(estoque_industria=5)
    amanha = hoje() + timedelta(days=1)
    db.session.add(CronogramaOverride(receita_id=ret.id, data=amanha, qtd=10))
    db.session.commit()

    crono = cronograma_producao(horizonte_dias=7, motor='vendas')
    rr = next((x for x in crono['receitas'] if x['receita_id'] == ret.id),
              None)
    if rr is not None:
        assert rr['total'] == 0                # override ignorado

    plano = enviar_plano_do_dia(amanha, admin_user.id, motor='vendas')
    if plano is not None:
        itens_ret = [it for it in PlanejamentoItem.query
                     .filter_by(planejamento_id=plano.id).all()
                     if it.receita_id == ret.id]
        assert itens_ret == []                 # padeiro nunca vê retorno


def test_edicao_hoje_que_cobre_nao_ganha_stale(app):
    """Edição de hoje que COBRE a demanda não ganha o aviso (o E3 de
    'dia anterior' continua valendo pro caso sem risco)."""
    from app.services.cronograma_edit import editar_celula
    from app.services.previsao_producao import cronograma_producao
    r, dd = _cenario_edicao(qtd_firme=100, dias_entrega=1)
    res = editar_celula(r.id, dd.isoformat(), 150)  # fixa ACIMA do firme
    assert res and not res.get('erro')
    crono = cronograma_producao(horizonte_dias=7)
    rr = next(x for x in crono['receitas'] if x['receita_id'] == r.id)
    assert rr['editado'] is True
    assert not rr['entregas_risco']
    assert not rr.get('override_stale')
