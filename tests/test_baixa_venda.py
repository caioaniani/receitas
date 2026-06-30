"""Motor unico de baixa de venda (app/services/baixa_venda.py).

Cobre os 3 canais via uma so logica: identidade, cesta, fator fracionario com
acumulador por ITEM FISICO (pooling entre produtos/canais), e estorno (inteiro
e fracionario, com devolucao de inteiros quando o acumulador fica negativo).
"""
from app.extensions import db
from app.models import (
    DebitoEstoque,
    EstoqueLoja,
    MovEstoqueLoja,
    Produto,
    ProdutoItem,
    Receita,
)
from app.services.baixa_venda import aplicar_venda, estornar_venda


def _receita(nome):
    r = Receita(nome=nome, categoria='Paes', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0)
    db.session.add(r)
    db.session.flush()
    return r


def _estoque(loja, col, item, qtd):
    el = EstoqueLoja(loja_id=loja.id, quantidade=qtd, **{col: item.id})
    db.session.add(el)
    db.session.flush()
    return el


def _cesta(nome, componentes):
    """componentes = [(receita, qtd_por_cesta), ...]"""
    p = Produto(nome=nome, ativo=True)
    db.session.add(p)
    db.session.flush()
    for rec, qtd in componentes:
        db.session.add(ProdutoItem(produto_id=p.id, tipo='receita',
                                   item_nome=rec.nome, receita_id=rec.id,
                                   quantidade=qtd))
    db.session.flush()
    return p


def _qtd(loja, col, item):
    el = EstoqueLoja.query.filter_by(loja_id=loja.id, **{col: item.id}).first()
    return el.quantidade if el else None


def test_identidade_baixa_direta(app, loja):
    """Venda simples (receita, fator 1) baixa exatamente a quantidade vendida."""
    r = _receita('Pao')
    _estoque(loja, 'receita_id', r, 10)
    db.session.commit()
    res = aplicar_venda(loja.id, receita_id=r.id, qtd=3, canal='seru',
                        referencia='Seru #1', pedido_ref='seru:1')
    db.session.commit()
    assert res['baixado'] == 3 and res['faltou'] == 0
    assert _qtd(loja, 'receita_id', r) == 7
    # sem acumulador pra venda inteira
    assert DebitoEstoque.query.count() == 0


def test_cesta_desempacota_componentes(app, loja):
    """Cesta 5 pao + 3 croissant: vender 2 baixa 10 pao + 6 croissant."""
    pao = _receita('Pao Cesta')
    croi = _receita('Croissant Cesta')
    cesta = _cesta('Family Box', [(pao, 5), (croi, 3)])
    _estoque(loja, 'receita_id', pao, 20)
    _estoque(loja, 'receita_id', croi, 10)
    db.session.commit()
    res = aplicar_venda(loja.id, produto_id=cesta.id, qtd=2, canal='site',
                        referencia='Site #ABC', pedido_ref='site:ABC',
                        nome_venda='Family Box')
    db.session.commit()
    assert res['baixado'] == 16            # 10 + 6
    assert _qtd(loja, 'receita_id', pao) == 10
    assert _qtd(loja, 'receita_id', croi) == 4


def test_fator_fracionario_acumula(app, loja):
    """Cafe -> 0.2 cookie: 4 vendas nao baixam (0.8); a 5a fecha 1.0 e baixa 1."""
    cookie = _receita('Cookie')
    _estoque(loja, 'receita_id', cookie, 10)
    db.session.commit()
    for i in range(4):
        aplicar_venda(loja.id, receita_id=cookie.id, qtd=1, fator=0.2,
                      canal='seru', referencia=f'Seru #{i}',
                      pedido_ref=f'seru:{i}')
    db.session.commit()
    assert _qtd(loja, 'receita_id', cookie) == 10        # nada ainda
    deb = DebitoEstoque.query.filter_by(loja_id=loja.id,
                                        receita_id=cookie.id).first()
    assert abs(deb.fracao_pendente - 0.8) < 1e-6
    aplicar_venda(loja.id, receita_id=cookie.id, qtd=1, fator=0.2, canal='seru',
                  referencia='Seru #5', pedido_ref='seru:5')
    db.session.commit()
    assert _qtd(loja, 'receita_id', cookie) == 9         # fechou 1 inteiro
    assert abs(deb.fracao_pendente - 0.0) < 1e-6


def test_pooling_por_item_entre_sellables(app, loja):
    """Fracao do MESMO item fisico vinda de sellables/fatores DIFERENTES soma
    junta num so DebitoEstoque (chave por item, nao por mapa)."""
    sour = _receita('Sourdough')
    _estoque(loja, 'receita_id', sour, 10)
    sanduiche = _cesta('Sanduiche', [(sour, 0.7)])
    db.session.commit()
    # consumo 1: receita Sourdough direto, fator 0.5 -> 0.5
    aplicar_venda(loja.id, receita_id=sour.id, qtd=1, fator=0.5, canal='seru',
                  referencia='Seru #1', pedido_ref='seru:1')
    # consumo 2: cesta com 0.7 de Sourdough -> 0.5 + 0.7 = 1.2 -> baixa 1
    aplicar_venda(loja.id, produto_id=sanduiche.id, qtd=1, canal='seru',
                  referencia='Seru #2', pedido_ref='seru:2', nome_venda='Sanduiche')
    db.session.commit()
    debitos = DebitoEstoque.query.filter_by(loja_id=loja.id,
                                            receita_id=sour.id).all()
    assert len(debitos) == 1                             # UM acumulador, nao dois
    assert abs(debitos[0].fracao_pendente - 0.2) < 1e-6
    assert _qtd(loja, 'receita_id', sour) == 9


def test_sem_estoque_nao_fica_negativo(app, loja):
    """Baixar mais que o saldo registra venda_*_sem_estoque e para no zero."""
    r = _receita('Pao')
    _estoque(loja, 'receita_id', r, 2)
    db.session.commit()
    res = aplicar_venda(loja.id, receita_id=r.id, qtd=5, canal='seru',
                        referencia='Seru #9', pedido_ref='seru:9')
    db.session.commit()
    assert res['baixado'] == 2 and res['faltou'] == 3
    assert _qtd(loja, 'receita_id', r) == 0
    assert MovEstoqueLoja.query.filter_by(tipo='venda_seru_sem_estoque').count() == 1


def test_estorno_venda_inteira_devolve(app, loja):
    """Estornar uma venda inteira devolve a quantidade e marca o estorno."""
    r = _receita('Pao')
    _estoque(loja, 'receita_id', r, 10)
    db.session.commit()
    aplicar_venda(loja.id, receita_id=r.id, qtd=3, canal='seru',
                  referencia='Seru #1', pedido_ref='seru:1')
    db.session.commit()
    assert _qtd(loja, 'receita_id', r) == 7
    res = estornar_venda('seru', 'seru:1', 'Seru #1')
    db.session.commit()
    assert res['revertido_inteiros'] == 1
    assert _qtd(loja, 'receita_id', r) == 10
    assert MovEstoqueLoja.query.filter_by(tipo='venda_seru_estorno').count() == 1


def test_estorno_fracionario_devolve_inteiro_so_do_pedido(app, loja):
    """2 pedidos fracionarios (0.6 cada) fecham 1 cookie. Estornar UM devolve
    o cookie e deixa a fracao do OUTRO no acumulador (0.6) — nao some nem dobra."""
    cookie = _receita('Cookie')
    _estoque(loja, 'receita_id', cookie, 10)
    db.session.commit()
    aplicar_venda(loja.id, receita_id=cookie.id, qtd=3, fator=0.2, canal='seru',
                  referencia='Seru #A', pedido_ref='seru:A')   # 0.6
    aplicar_venda(loja.id, receita_id=cookie.id, qtd=3, fator=0.2, canal='seru',
                  referencia='Seru #B', pedido_ref='seru:B')   # 1.2 -> baixa 1
    db.session.commit()
    assert _qtd(loja, 'receita_id', cookie) == 9
    # estorna B: baixa veio marcada (fracao) -> fase 1 nao mexe; fase 2 devolve 1
    res = estornar_venda('seru', 'seru:B', 'Seru #B')
    db.session.commit()
    assert res['revertido_inteiros'] == 0                # baixa fracionaria
    assert res['revertido_fracoes'] == 1
    assert _qtd(loja, 'receita_id', cookie) == 10        # cookie devolvido
    deb = DebitoEstoque.query.filter_by(loja_id=loja.id,
                                        receita_id=cookie.id).first()
    assert abs(deb.fracao_pendente - 0.6) < 1e-6         # sobra do pedido A
    # idempotente: estornar de novo nao mexe
    res2 = estornar_venda('seru', 'seru:B', 'Seru #B')
    db.session.commit()
    assert res2['revertido_fracoes'] == 0
    assert _qtd(loja, 'receita_id', cookie) == 10


def test_estorno_nao_colide_prefixo_de_pedido(app, loja):
    """Estornar 'Seru #1' nao pode reverter a venda de 'Seru #12' (prefixo)."""
    r = _receita('Pao')
    _estoque(loja, 'receita_id', r, 10)
    db.session.commit()
    aplicar_venda(loja.id, receita_id=r.id, qtd=2, canal='seru',
                  referencia='Seru #1', pedido_ref='seru:1')
    aplicar_venda(loja.id, receita_id=r.id, qtd=3, canal='seru',
                  referencia='Seru #12', pedido_ref='seru:12')
    db.session.commit()
    assert _qtd(loja, 'receita_id', r) == 5
    estornar_venda('seru', 'seru:1', 'Seru #1')
    db.session.commit()
    assert _qtd(loja, 'receita_id', r) == 7              # so #1 (2 un), nao #12
