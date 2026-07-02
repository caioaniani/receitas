"""Integridade das baixas da indústria + tela de estoque (03/07/2026).

Auditoria das baixas achou 3 furos, todos travados aqui:
- envio pulava em silêncio item SEM linha de EstoqueProducao (`if ep:`) —
  agora cria a linha (get-or-create) e registra a falta
  (`saida_pedido_sem_estoque`);
- movimento gravava a quantidade NOMINAL mesmo quando a baixa saturava em 0 —
  agora grava a baixa REAL + a falta;
- estorno (voltar status / cancelar via copilot) devolvia a quantidade cheia
  mesmo quando a baixa tinha saturado → estoque fantasma. Agora devolve o
  líquido dos movimentos (`estorno_saida_pedido`).
"""
from datetime import date, timedelta

from app.extensions import db
from app.models import (
    EstoqueLoja,
    EstoqueProducao,
    MovEstoqueLoja,
    MovEstoqueProducao,
    PedidoItem,
    PedidoLoja,
)


def _receita(nome='Croissant Int'):
    from app.models import Receita
    r = Receita(nome=nome, categoria='Croissants', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0)
    db.session.add(r)
    db.session.commit()
    return r


def _loja(nome='Loja Integridade'):
    from app.models import Loja
    loja = Loja(nome=nome, ativa=True, endereco='Rua I, 1')
    db.session.add(loja)
    db.session.commit()
    return loja


def _pedido(loja, receita, qtd, status='separado'):
    p = PedidoLoja(loja_id=loja.id, status=status,
                   data_entrega=date.today() + timedelta(days=1))
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p.id, receita_id=receita.id,
                              quantidade=qtd))
    db.session.commit()
    return p


def _login(client, uid):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)
        sess['_fresh'] = True


# ── envio: item sem linha não é mais pulado em silêncio ────────────────────

def test_envio_sem_linha_cria_e_registra_falta(app, admin_user):
    from app.blueprints.pedidos.routes import _executar_envio_pedido
    r = _receita('Sem Linha')
    loja = _loja('Loja A')
    p = _pedido(loja, r, qtd=5)          # NENHUMA linha de EstoqueProducao

    with app.test_request_context():
        ok, msg = _executar_envio_pedido(p, admin_user)
    assert ok is True
    assert p.status == 'em_transporte'
    ep = EstoqueProducao.query.filter_by(receita_id=r.id).first()
    assert ep is not None and ep.quantidade == 0       # linha criada, sem negativo
    falta = MovEstoqueProducao.query.filter_by(
        estoque_producao_id=ep.id, tipo='saida_pedido_sem_estoque').first()
    assert falta is not None and falta.quantidade == 5  # rastro da falta
    assert 'insuficiente' in msg                        # caller é avisado


def test_envio_parcial_grava_baixa_real_e_falta(app, admin_user):
    from app.blueprints.pedidos.routes import _executar_envio_pedido
    r = _receita('Parcial')
    loja = _loja('Loja B')
    db.session.add(EstoqueProducao(receita_id=r.id, quantidade=3))
    db.session.commit()
    p = _pedido(loja, r, qtd=5)

    with app.test_request_context():
        ok, _msg = _executar_envio_pedido(p, admin_user)
    assert ok is True
    ep = EstoqueProducao.query.filter_by(receita_id=r.id).first()
    assert ep.quantidade == 0
    baixa = MovEstoqueProducao.query.filter_by(
        estoque_producao_id=ep.id, tipo='saida_pedido').first()
    assert baixa.quantidade == 3                        # REAL, não os 5 nominais
    falta = MovEstoqueProducao.query.filter_by(
        estoque_producao_id=ep.id, tipo='saida_pedido_sem_estoque').first()
    assert falta.quantidade == 2


# ── estorno espelha a baixa real ────────────────────────────────────────────

def test_estorno_devolve_so_o_que_saiu(app, admin_user):
    """Baixa saturada (3 de 5) + voltar status → devolve 3, não 5."""
    from app.blueprints.pedidos.routes import (
        _aplicar_voltar_status,
        _executar_envio_pedido,
    )
    r = _receita('Estorno Real')
    loja = _loja('Loja C')
    db.session.add(EstoqueProducao(receita_id=r.id, quantidade=3))
    db.session.commit()
    p = _pedido(loja, r, qtd=5)
    with app.test_request_context():
        _executar_envio_pedido(p, admin_user)

    res = _aplicar_voltar_status(p, admin_user.id)
    db.session.commit()
    assert res == ('em_transporte', 'separado')
    ep = EstoqueProducao.query.filter_by(receita_id=r.id).first()
    assert ep.quantidade == 3                           # voltou ao real, sem fantasma
    est = MovEstoqueProducao.query.filter_by(
        estoque_producao_id=ep.id, tipo='estorno_saida_pedido').first()
    assert est is not None and est.quantidade == 3


def test_estorno_liquido_sobrevive_a_reenvio(app, admin_user):
    """Estornar → reenviar → estornar de novo: o líquido dos movimentos
    mantém a conta certa (nada devolvido em dobro)."""
    from app.blueprints.pedidos.routes import (
        _aplicar_voltar_status,
        _executar_envio_pedido,
    )
    r = _receita('Liquido')
    loja = _loja('Loja D')
    db.session.add(EstoqueProducao(receita_id=r.id, quantidade=3))
    db.session.commit()
    p = _pedido(loja, r, qtd=5)
    with app.test_request_context():
        _executar_envio_pedido(p, admin_user)           # baixa 3 (satura)
    _aplicar_voltar_status(p, admin_user.id)            # devolve 3
    db.session.commit()
    with app.test_request_context():
        _executar_envio_pedido(p, admin_user)           # baixa 3 de novo
    _aplicar_voltar_status(p, admin_user.id)            # deve devolver SÓ 3
    db.session.commit()
    ep = EstoqueProducao.query.filter_by(receita_id=r.id).first()
    assert ep.quantidade == 3                           # não virou 6


# ── copilot usa o mesmo motor ───────────────────────────────────────────────

def test_copilot_enviar_registra_falta_como_a_web(app, admin_user):
    from app.services import copilot
    r = _receita('Copilot Envia')
    loja = _loja('Loja E')
    p = _pedido(loja, r, qtd=4, status='confirmado')    # sem linha de estoque

    with app.test_request_context():
        out = copilot.executar_mudar_status_pedido(
            {'pedido_id': p.id, 'novo_status': 'enviar'}, admin_user)
    assert out['ok'] is True
    db.session.refresh(p)
    assert p.status == 'em_transporte'
    ep = EstoqueProducao.query.filter_by(receita_id=r.id).first()
    assert ep is not None                               # linha criada (motor único)
    falta = MovEstoqueProducao.query.filter_by(
        estoque_producao_id=ep.id, tipo='saida_pedido_sem_estoque').first()
    assert falta is not None and falta.quantidade == 4


def test_copilot_cancelar_em_transporte_estorna(app, admin_user):
    from app.services import copilot
    r = _receita('Copilot Cancela')
    loja = _loja('Loja F')
    db.session.add(EstoqueProducao(receita_id=r.id, quantidade=10))
    db.session.commit()
    p = _pedido(loja, r, qtd=4, status='confirmado')
    with app.test_request_context():
        copilot.executar_mudar_status_pedido(
            {'pedido_id': p.id, 'novo_status': 'enviar'}, admin_user)
    ep = EstoqueProducao.query.filter_by(receita_id=r.id).first()
    assert ep.quantidade == 6                           # baixou 4

    with app.test_request_context():
        out = copilot.executar_mudar_status_pedido(
            {'pedido_id': p.id, 'novo_status': 'cancelar'}, admin_user)
    assert out['ok'] is True
    db.session.refresh(p)
    assert p.status == 'cancelado'
    db.session.refresh(ep)
    assert ep.quantidade == 10                          # estornado (antes ficava 6)


# ── tela de estoque da loja: baixa real + falta ─────────────────────────────

def test_tela_perda_maior_que_saldo_registra_falta(app, admin_user):
    from app.models import Produto
    loja = _loja('Loja G')
    prod = Produto(nome='Geleia Int', categoria='Conservas', preco_site=10,
                   ativo=True)
    db.session.add(prod)
    db.session.commit()
    el = EstoqueLoja(loja_id=loja.id, produto_id=prod.id, quantidade=3)
    db.session.add(el)
    db.session.commit()

    c = app.test_client()
    _login(c, admin_user.id)
    c.post('/pedidos/estoque-loja/registrar', data={
        'loja_id': str(loja.id),
        'estoque_id[]': [str(el.id)],
        'qtd[]': ['5'],                                  # mais que o saldo (3)
        'tipo[]': ['perda'],
    })
    db.session.refresh(el)
    assert el.quantidade == 0
    mov = MovEstoqueLoja.query.filter_by(
        estoque_loja_id=el.id, tipo='perda').first()
    assert mov.quantidade == 3                           # baixa REAL
    falta = MovEstoqueLoja.query.filter_by(
        estoque_loja_id=el.id, tipo='perda_sem_estoque').first()
    assert falta is not None and falta.quantidade == 2   # excedente rastreado
