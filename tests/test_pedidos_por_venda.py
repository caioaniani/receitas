"""Maneira 2 — previsao de pedido por VENDA + ESTOQUE (ponto de reposicao):
sugerir_pedidos_por_venda. Mede a venda media por dia-da-semana (baixas do
EstoqueLoja) e simula o estoque dia a dia; pede o que falta arredondado pra cima
na caixa, com o excedente cobrindo os proximos dias.
"""
from datetime import datetime, time, timedelta

import pytest

from app.extensions import db
from app.models import EstoqueLoja, Loja, MovEstoqueLoja, Receita
from app.services.previsao_producao import sugerir_pedidos_por_venda
from app.utils import hoje


@pytest.fixture(autouse=True)
def _hoje_e_segunda_fixa(congela_hoje):
    """Producao seg-sex + janela semanal tornaram o motor weekday-sensivel
    — congela numa SEGUNDA fixa (mesma fixture dos arquivos do cronograma;
    caso real 19/08/2026: test_cronograma_edit quebrou na QUARTA porque o
    indice 3 do grid caiu no sabado bloqueado)."""
    congela_hoje()



def _receita(nome='Croissant', lote=0):
    r = Receita(nome=nome, categoria='Paes', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0, lote_pedido=lote)
    db.session.add(r)
    db.session.commit()
    return r


def _loja(nome='Loja A'):
    loja = Loja(nome=nome, ativa=True)
    db.session.add(loja)
    db.session.commit()
    return loja


def _estoque(loja, receita, qtd):
    el = EstoqueLoja(loja_id=loja.id, receita_id=receita.id, quantidade=qtd)
    db.session.add(el)
    db.session.commit()
    return el


def _venda(el, data, qtd, tipo='venda_seru'):
    """Registra uma baixa de venda na data (datetime no meio do dia)."""
    db.session.add(MovEstoqueLoja(
        estoque_loja_id=el.id, tipo=tipo, quantidade=qtd,
        data=datetime.combine(data, time(12, 0)), referencia='teste'))
    db.session.commit()


def _prod(grade, loja_id, rid):
    loja = next((e for e in grade['lojas'] if e['loja_id'] == loja_id), None)
    return None if loja is None else next(
        (p for p in loja['produtos'] if p['receita_id'] == rid), None)


def test_pede_o_que_falta_pra_cobrir_a_venda(app):
    """Vende ~10/dia (toda 2a-feira do historico), estoque 0 -> pede pra cobrir.
    Sem caixa: pedido do dia = venda do dia."""
    loja = _loja()
    r = _receita('Pao')           # sem lote
    el = _estoque(loja, r, 0)
    hoje_d = hoje()
    # acha a proxima 2a-feira a partir de hoje, e semeia 6 segundas no passado
    alvo = hoje_d
    while alvo.weekday() != 0:     # 0 = segunda
        alvo += timedelta(days=1)
    for sem in range(1, 7):
        _venda(el, alvo - timedelta(days=7 * sem), 10)

    grade = sugerir_pedidos_por_venda(horizonte_dias=7, janela_semanas=6,
                                      inicio_offset_dias=(alvo - hoje_d).days)
    p = _prod(grade, loja.id, r.id)
    assert p is not None
    assert p['estoque_atual'] == 0
    # 1o dia do horizonte = segunda: venda media 10, estoque 0 -> pede 10
    assert p['por_dia'][0] == 10


def test_caixa_arredonda_pra_cima_e_excedente_cobre_proximos_dias(app):
    """Vende 2/dia, caixa 6, estoque 0: pede 1 caixa (6) que cobre ~3 dias —
    item lento NAO recebe caixa todo dia."""
    loja = _loja()
    r = _receita('Item Lento', lote=6)
    el = _estoque(loja, r, 0)
    hoje_d = hoje()
    # vende 2 em TODOS os dias-da-semana (6 semanas) -> media 2/dia em qualquer dia
    for sem in range(1, 7):
        for dow in range(7):
            d = hoje_d - timedelta(days=7 * sem)
            d = d - timedelta(days=d.weekday()) + timedelta(days=dow)
            if d < hoje_d:
                _venda(el, d, 2)

    grade = sugerir_pedidos_por_venda(horizonte_dias=7, janela_semanas=6,
                                      inicio_offset_dias=0)
    p = _prod(grade, loja.id, r.id)
    assert p is not None
    # so caixas inteiras
    assert all(v % 6 == 0 for v in p['por_dia'])
    # NAO pede caixa todo dia (excedente cobre): menos pedidos que dias
    dias_com_pedido = sum(1 for v in p['por_dia'] if v > 0)
    assert dias_com_pedido < 7
    # total ~ cobre 7 dias de venda (14 un) em caixas de 6 -> 12 ou 18
    assert sum(p['por_dia']) >= 14 - 6        # tolera o estoque carregado


def test_estoque_suficiente_aparece_com_zero(app):
    """Loja com estoque alto que cobre a venda APARECE com sugestao 0 (decisao do
    dono: mostrar todos os produtos da loja, nada some da tela)."""
    loja = _loja()
    r = _receita('Pao')
    el = _estoque(loja, r, 1000)              # estoque enorme
    hoje_d = hoje()
    for sem in range(1, 7):
        _venda(el, hoje_d - timedelta(days=7 * sem), 5)

    grade = sugerir_pedidos_por_venda(horizonte_dias=7, janela_semanas=6,
                                      inicio_offset_dias=0)
    p = _prod(grade, loja.id, r.id)
    assert p is not None                      # aparece
    assert sum(p['por_dia']) == 0            # mas sem sugerir nada (estoque cobre)


def test_produto_que_a_loja_pede_aparece_mesmo_sem_venda(app):
    """MOSTRAR TODOS: um produto que a loja PEDE da industria (historico de
    pedidos) aparece com sugestao 0 mesmo sem venda rastreada nem estoque —
    era o caso da Ribeiro (mapa Seru incompleto escondia o produto)."""
    from app.models import PedidoItem, PedidoLoja
    loja = _loja()
    r = _receita('Pao Sem Venda Rastreada')
    hoje_d = hoje()
    # a loja pediu da industria nas ultimas semanas (mas nenhuma venda foi
    # registrada no EstoqueLoja, e nao ha estoque)
    for sem in (1, 2, 3):
        p = PedidoLoja(loja_id=loja.id, status='recebido',
                       data_entrega=hoje_d - timedelta(days=7 * sem),
                       data_pedido=hoje_d - timedelta(days=7 * sem))
        db.session.add(p)
        db.session.flush()
        db.session.add(PedidoItem(pedido_id=p.id, receita_id=r.id, quantidade=20))
    db.session.commit()

    grade = sugerir_pedidos_por_venda(horizonte_dias=7, janela_semanas=6,
                                      inicio_offset_dias=0)
    p = _prod(grade, loja.id, r.id)
    assert p is not None                      # NAO some so por nao ter venda
    assert p['estoque_atual'] == 0
    assert sum(p['por_dia']) == 0            # sugestao 0 -> operador preenche


def test_dia_travado_usa_entrega_real_no_carry(app):
    """JA-TEM-CARRY: dia ja pedido sugere 0 e a simulacao usa a ENTREGA JA
    PEDIDA (qtd real) como estoque dos dias seguintes — nao a sugestao
    descartada (que sumiria no POST por vir disabled)."""
    from app.models import PedidoItem, PedidoLoja
    loja = _loja()
    r = _receita('Pao')                       # sem caixa
    el = _estoque(loja, r, 0)
    hoje_d = hoje()
    # vende 10 em TODOS os dias-da-semana (6 semanas) -> media 10/dia
    for sem in range(1, 7):
        for dow in range(7):
            d = hoje_d - timedelta(days=7 * sem)
            d = d - timedelta(days=d.weekday()) + timedelta(days=dow)
            if d < hoje_d:
                _venda(el, d, 10)
    # a loja JA pediu HOJE (dia 0 do horizonte) uma reposicao GRANDE de 30
    p_fut = PedidoLoja(loja_id=loja.id, status='pendente',
                       data_entrega=hoje_d, data_pedido=hoje_d)
    db.session.add(p_fut)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p_fut.id, receita_id=r.id, quantidade=30))
    db.session.commit()

    grade = sugerir_pedidos_por_venda(horizonte_dias=7, janela_semanas=6,
                                      inicio_offset_dias=0)
    p = _prod(grade, loja.id, r.id)
    assert p is not None
    assert p['por_dia'][0] == 0               # dia travado: nao sugere
    # os 30 ja pedidos cobrem a venda dos proximos dias -> 1 e 2 ficam 0
    assert p['por_dia'][1] == 0
    assert p['por_dia'][2] == 0
    # menos que [10]*7 (=70) do bug que ignorava a entrega real
    assert sum(p['por_dia']) < 70


def test_estoque_reservado_nao_conta_como_disponivel(app):
    """ESTOQUE-RESERVADO: estoque reservado (pedido online aguardando pgto) NAO
    conta como disponivel — senao subestima o deficit e sub-pede."""
    loja = _loja()
    r = _receita('Pao')
    el = _estoque(loja, r, 10)
    el.quantidade_reservada = 10              # tudo reservado -> disponivel 0
    db.session.commit()
    hoje_d = hoje()
    for sem in range(1, 7):
        for dow in range(7):
            d = hoje_d - timedelta(days=7 * sem)
            d = d - timedelta(days=d.weekday()) + timedelta(days=dow)
            if d < hoje_d:
                _venda(el, d, 10)

    grade = sugerir_pedidos_por_venda(horizonte_dias=7, janela_semanas=6,
                                      inicio_offset_dias=0)
    p = _prod(grade, loja.id, r.id)
    assert p is not None
    assert p['estoque_atual'] == 0            # disponivel = 10 - 10 reservado
    assert p['por_dia'][0] == 10              # disponivel 0 -> pede a venda do dia


def test_offset_projeta_consumo_ate_o_inicio_da_janela(app):
    """BUG corrigido 11/07/2026: com "A partir de" no futuro, a simulação
    partia do estoque de HOJE e ignorava o consumo até o início da janela —
    estoque otimista, SUB-pedia. Agora o saldo é projetado dia a dia até o
    início (consumo previsto + entregas já pedidas)."""
    loja = _loja()
    r = _receita('Pao')
    el = _estoque(loja, r, 20)                  # cobre 2 dias de venda 10/dia
    hoje_d = hoje()
    for sem in range(1, 7):
        for dow in range(7):
            d = hoje_d - timedelta(days=7 * sem)
            d = d - timedelta(days=d.weekday()) + timedelta(days=dow)
            if d < hoje_d:
                _venda(el, d, 10)               # média 10/dia em todo dow

    # Janela começando DEPOIS de o estoque acabar (offset 3 > 20/10 dias):
    # o 1º dia da janela precisa pedir (antes do fix saía 0, como se as 20 un
    # de hoje ainda estivessem lá). A média por dow nas bordas não é exata
    # (recência/zeros), então o assert é na propriedade, não no número.
    grade = sugerir_pedidos_por_venda(horizonte_dias=7, janela_semanas=6,
                                      inicio_offset_dias=3)
    p = _prod(grade, loja.id, r.id)
    assert p is not None
    assert p['por_dia'][0] > 0


def test_offset_credita_entrega_ja_pedida_antes_da_janela(app):
    """A projeção pré-janela também CREDITA entregas já pedidas entre hoje e
    o início — pedido chegando amanhã conta no saldo da janela de depois."""
    from app.models import PedidoItem, PedidoLoja
    loja = _loja()
    r = _receita('Pao')
    el = _estoque(loja, r, 20)
    hoje_d = hoje()
    for sem in range(1, 7):
        for dow in range(7):
            d = hoje_d - timedelta(days=7 * sem)
            d = d - timedelta(days=d.weekday()) + timedelta(days=dow)
            if d < hoje_d:
                _venda(el, d, 10)
    # entrega de 30 chegando AMANHÃ (antes do início da janela em +3)
    ped = PedidoLoja(loja_id=loja.id, status='pendente',
                     data_entrega=hoje_d + timedelta(days=1),
                     data_pedido=hoje_d)
    db.session.add(ped)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=ped.id, receita_id=r.id,
                              quantidade=30))
    db.session.commit()

    grade = sugerir_pedidos_por_venda(horizonte_dias=7, janela_semanas=6,
                                      inicio_offset_dias=3)
    p = _prod(grade, loja.id, r.id)
    assert p is not None
    # saldo projetado no início ~ 20 + 30 - 3 dias de venda (~10/dia) ≈ 20:
    # cobre os primeiros dias — sem o crédito da entrega o dia 0 já pediria.
    assert p['por_dia'][0] == 0
    assert p['por_dia'][1] == 0
    assert sum(p['por_dia'][2:]) > 0            # a partir dali volta a pedir


def test_offset_pedido_ja_entregue_hoje_nao_conta_em_dobro(app):
    """Pedido ENTREGUE hoje já está dentro do estoque atual da loja
    (entrada_pedido no recebimento) — a projeção pré-janela NÃO pode
    creditá-lo de novo (achado de revisão 11/07/2026: o double-count
    inflava o saldo e a janela de amanhã sub-pedia)."""
    from app.models import PedidoItem, PedidoLoja
    loja = _loja()
    r = _receita('Pao')
    hoje_d = hoje()
    # estoque de HOJE = 10 (JÁ inclui a entrega recebida de manhã)
    el = _estoque(loja, r, 10)
    for sem in range(1, 7):
        for dow in range(7):
            d = hoje_d - timedelta(days=7 * sem)
            d = d - timedelta(days=d.weekday()) + timedelta(days=dow)
            if d < hoje_d:
                _venda(el, d, 10)               # média 10/dia
    ped = PedidoLoja(loja_id=loja.id, status='recebido',
                     data_entrega=hoje_d, data_pedido=hoje_d)
    db.session.add(ped)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=ped.id, receita_id=r.id,
                              quantidade=50))
    db.session.commit()

    grade = sugerir_pedidos_por_venda(horizonte_dias=7, janela_semanas=6,
                                      inicio_offset_dias=1)
    p = _prod(grade, loja.id, r.id)
    assert p is not None
    # saldo projetado pra amanhã ~ 10 - 10 = 0 -> amanhã PEDE. Com o
    # double-count (10 + 50 - 10 = 50) amanhã sairia 0 e sub-pediria.
    assert p['por_dia'][0] > 0


def test_offset_venda_perdida_nao_vira_pedido(app):
    """Saldo projetado NEGATIVO antes da janela é venda perdida — clampa em 0
    por dia. A janela abre pedindo a venda do dia, não a perdida acumulada."""
    loja = _loja()
    r = _receita('Pao')
    el = _estoque(loja, r, 0)                   # já sem estoque hoje
    hoje_d = hoje()
    for sem in range(1, 7):
        for dow in range(7):
            d = hoje_d - timedelta(days=7 * sem)
            d = d - timedelta(days=d.weekday()) + timedelta(days=dow)
            if d < hoje_d:
                _venda(el, d, 10)

    grade = sugerir_pedidos_por_venda(horizonte_dias=7, janela_semanas=6,
                                      inicio_offset_dias=5)
    p = _prod(grade, loja.id, r.id)
    assert p is not None
    # sem clamp seria ~1 dia de venda + 5 dias de venda perdida (~60);
    # com clamp pede só o consumo do dia (<= 10)
    assert 0 < p['por_dia'][0] <= 10


def test_rota_estoque_renderiza(app, admin_user):
    loja = _loja('Loja Centro')
    r = _receita('Pão Francês')
    el = _estoque(loja, r, 0)
    hoje_d = hoje()
    for sem in range(1, 7):
        for dow in range(7):
            d = hoje_d - timedelta(days=7 * sem)
            d = d - timedelta(days=d.weekday()) + timedelta(days=dow)
            if d < hoje_d:
                _venda(el, d, 8)

    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    resp = client.get('/producao/pedidos-semana/estoque?horizonte=7&janela=6&inicio=0')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'venda + estoque' in body
    assert 'Loja Centro' in body
    assert 'Pão Francês' in body
    assert 'Estoque' in body
    # geração POR LOJA (botão no card) + origem pra voltar pra esta tela
    assert 'Gerar só esta loja' in body
    assert 'name="so_loja" value="%d"' % loja.id in body
    assert 'name="origem" value="estoque"' in body
    # ação explícita via hidden + confirm no listener (nunca onclick inline):
    # o Safari descarta o name/value do botão quando o onclick tem confirm()
    assert 'id="form-gerar-grade"' in body
    assert 'name="gerar_todas"' in body
    assert 'data-confirm=' in body
    assert 'onclick="return confirm' not in body


def test_estoque_auto_salva_dia_com_pedido(app, admin_user):
    """A tela venda+estoque auto-salva o dia que JÁ tem pedido (decisão do
    dono 17/07/2026: 'sem eu ter que apertar atualizar'). Mesma engrenagem da
    média: célula tem-pedido + botão ↻ como status + POST so_dia/ajax. Dia
    sem pedido NÃO ganha auto-save (criar rascunho continua no Gerar)."""
    from app.models import PedidoItem, PedidoLoja
    loja = _loja('Loja Auto')
    r = _receita('Pao')
    el = _estoque(loja, r, 0)
    hoje_d = hoje()
    for sem in range(1, 7):
        for dow in range(7):
            d = hoje_d - timedelta(days=7 * sem)
            d = d - timedelta(days=d.weekday()) + timedelta(days=dow)
            if d < hoje_d:
                _venda(el, d, 10)
    amanha = hoje_d + timedelta(days=1)
    ped = PedidoLoja(loja_id=loja.id, status='pendente', data_entrega=amanha,
                     data_pedido=hoje_d)
    db.session.add(ped)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=ped.id, receita_id=r.id, quantidade=20))
    db.session.commit()

    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    body = client.get('/producao/pedidos-semana/estoque?horizonte=7&janela=6'
                      '&inicio=0').get_data(as_text=True)
    assert 'btn-atualizar-dia' in body
    assert 'tem-pedido' in body                 # célula do dia com pedido
    # engrenagem do autosave (mesma da média): POST so_dia+ajax e status
    assert "fd.set('so_dia'" in body
    assert "fd.set('ajax', '1')" in body
    assert 'salvando' in body
    assert '%d|%s' % (loja.id, amanha.isoformat()) in body


# ── Matérias-primas na tela (pão de queijo comprado, vendido via cones) ─────
def _mp(nome='Pão de Queijo (congelado)', sugerir=True):
    from app.models import MateriaPrima
    m = MateriaPrima(nome=nome, unidade='un', custo_por_kg=0.4662,
                     peso_unidade=18.0, sugerir_pedido_loja=sugerir)
    db.session.add(m)
    db.session.commit()
    return m


def _estoque_mp(loja, mp, qtd):
    el = EstoqueLoja(loja_id=loja.id, materia_prima_id=mp.id, quantidade=qtd)
    db.session.add(el)
    db.session.commit()
    return el


def _prod_mp(grade, loja_id, mid):
    loja = next((e for e in grade['lojas'] if e['loja_id'] == loja_id), None)
    return None if loja is None else next(
        (p for p in loja['produtos'] if p.get('materia_prima_id') == mid), None)


def test_mp_com_venda_aparece_e_sugere(app):
    """MP que a loja estoca e vende (baixa via cone no PDV) entra na tela com
    sugestão baseada na venda — antes só receitas apareciam e o pão de queijo
    ficava invisível."""
    loja = _loja()
    mp = _mp()
    el = _estoque_mp(loja, mp, 0)
    hoje_d = hoje()
    alvo = hoje_d
    while alvo.weekday() != 0:
        alvo += timedelta(days=1)
    for sem in range(1, 7):
        _venda(el, alvo - timedelta(days=7 * sem), 20)   # 4 cones de 5 por 2a

    grade = sugerir_pedidos_por_venda(horizonte_dias=7, janela_semanas=6,
                                      inicio_offset_dias=(alvo - hoje_d).days)
    p = _prod_mp(grade, loja.id, mp.id)
    assert p is not None
    assert p['eh_mp'] is True
    assert p['item_key'] == f'mp:{mp.id}'
    assert p['receita_id'] is None
    assert p['por_dia'][0] == 20                # venda média 20, estoque 0
    assert p['lote'] == 0                       # MP não tem caixa cadastrada


def test_mp_estoque_cobre_nao_pede(app):
    """MP com estoque que cobre a venda média não gera sugestão (mesma
    simulação de ponto de reposição das receitas)."""
    loja = _loja()
    mp = _mp()
    el = _estoque_mp(loja, mp, 500)
    hoje_d = hoje()
    for sem in range(1, 7):
        _venda(el, hoje_d - timedelta(days=7 * sem), 20)

    grade = sugerir_pedidos_por_venda(horizonte_dias=7, janela_semanas=6,
                                      inicio_offset_dias=0)
    p = _prod_mp(grade, loja.id, mp.id)
    assert p is not None
    assert p['estoque_atual'] == 500
    assert p['total'] == 0                      # 500 cobre a semana


def test_mp_sem_checkbox_nao_aparece_mesmo_com_venda(app):
    """MP SEM o checkbox 'Loja pede' fica fora da tela mesmo com estoque e
    venda — opt-in explícito (nem toda MP que passa por loja é pedida)."""
    loja = _loja()
    mp = _mp('Embalagem Cone', sugerir=False)      # sem checkbox
    el = _estoque_mp(loja, mp, 50)
    _venda(el, hoje() - timedelta(days=7), 10)
    r = _receita('Pao')                             # pra loja existir na grade
    el_r = _estoque(loja, r, 5)
    _venda(el_r, hoje() - timedelta(days=7), 3)
    grade = sugerir_pedidos_por_venda(horizonte_dias=7, janela_semanas=6)
    assert _prod_mp(grade, loja.id, mp.id) is None


def test_mp_sem_atividade_nao_aparece(app):
    """MP marcada mas sem estoque em loja, sem venda e sem pedido não polui
    a tela (mesmo critério por loja das receitas)."""
    loja = _loja()
    r = _receita('Pao')
    el = _estoque(loja, r, 5)                   # só pra loja existir na grade
    _venda(el, hoje() - timedelta(days=7), 3)
    mp = _mp('Farinha Industrial')              # MP só da indústria
    grade = sugerir_pedidos_por_venda(horizonte_dias=7, janela_semanas=6)
    assert _prod_mp(grade, loja.id, mp.id) is None


def test_gerar_cria_pedido_com_item_mp(app, admin_user):
    """POST do gerar com token 'mp:<id>' cria PedidoItem de matéria-prima."""
    from app.models import PedidoItem
    loja = _loja()
    mp = _mp()
    d = (hoje() + timedelta(days=1)).isoformat()
    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    resp = client.post('/producao/pedidos-semana/gerar', data={
        'origem': 'estoque', 'so_loja': str(loja.id),
        'qtd|%d|%s|mp:%d' % (loja.id, d, mp.id): '222'})
    assert resp.status_code == 302
    item = PedidoItem.query.one()
    assert item.materia_prima_id == mp.id
    assert item.receita_id is None
    assert item.quantidade == 222


def test_rota_estoque_renderiza_mp_com_badge(app, admin_user):
    """A tela venda+estoque mostra a MP com badge e input com token mp:<id>."""
    loja = _loja()
    mp = _mp()
    el = _estoque_mp(loja, mp, 50)
    _venda(el, hoje() - timedelta(days=7), 10)
    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    resp = client.get('/producao/pedidos-semana/estoque?horizonte=7&janela=6')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'Pão de Queijo (congelado)' in body
    assert 'mp:%d' % mp.id in body              # token no name do input
    assert '>MP</span>' in body                 # badge de matéria-prima


def test_dia_travado_expoe_o_que_foi_pedido(app):
    """Dia com pedido existente: `ja_pedido` traz o pedido REAL do dia — a
    célula travada mostra o encomendado em vez de um 0 apagado."""
    from app.models import PedidoItem, PedidoLoja
    loja = _loja()
    r = _receita('Pao')
    el = _estoque(loja, r, 0)
    _venda(el, hoje() - timedelta(days=7), 10)
    amanha = hoje() + timedelta(days=1)
    ped = PedidoLoja(loja_id=loja.id, status='pendente', data_entrega=amanha,
                     data_pedido=hoje())
    db.session.add(ped)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=ped.id, receita_id=r.id, quantidade=55))
    db.session.commit()

    grade = sugerir_pedidos_por_venda(horizonte_dias=7, janela_semanas=6,
                                      inicio_offset_dias=0)
    p = _prod(grade, loja.id, r.id)
    assert p is not None
    assert p['ja_pedido'][1] == 55                      # amanhã = dia 1
    assert p['por_dia'][1] == 0                         # travado: sem sugestão


def test_item_so_com_pedido_no_horizonte_aparece(app):
    """Item sem venda, sem estoque e sem pedido histórico, mas com pedido JÁ
    FEITO no horizonte: entra na grade (linha com as células do ja_pedido)."""
    from app.models import PedidoItem, PedidoLoja
    loja = _loja()
    r = _receita('Novo Sem Historia')
    # outra receita com venda pra loja existir na grade de qualquer forma
    r0 = _receita('Pao')
    el0 = _estoque(loja, r0, 5)
    _venda(el0, hoje() - timedelta(days=7), 3)
    amanha = hoje() + timedelta(days=1)
    ped = PedidoLoja(loja_id=loja.id, status='pendente', data_entrega=amanha,
                     data_pedido=hoje())
    db.session.add(ped)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=ped.id, receita_id=r.id, quantidade=44))
    db.session.commit()

    grade = sugerir_pedidos_por_venda(horizonte_dias=7, janela_semanas=6,
                                      inicio_offset_dias=0)
    p = _prod(grade, loja.id, r.id)
    assert p is not None
    assert p['ja_pedido'][1] == 44
    assert sum(p['por_dia']) == 0
