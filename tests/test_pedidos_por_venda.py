"""Maneira 2 — previsao de pedido por VENDA + ESTOQUE (ponto de reposicao):
sugerir_pedidos_por_venda. Mede a venda media por dia-da-semana (baixas do
EstoqueLoja) e simula o estoque dia a dia; pede o que falta arredondado pra cima
na caixa, com o excedente cobrindo os proximos dias.
"""
from datetime import datetime, time, timedelta

from app.extensions import db
from app.models import EstoqueLoja, Loja, MovEstoqueLoja, Receita
from app.services.previsao_producao import sugerir_pedidos_por_venda
from app.utils import hoje


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
