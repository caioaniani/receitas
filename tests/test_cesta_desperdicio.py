"""Teste de desempacotamento de cesta em desperdicio (copilot e via UI).

Mesmo bug do estoque_loja_lote: registrar desperdicio de cesta deve baixar
componentes individualmente do estoque da loja.
"""


def test_desperdicio_cesta_via_copilot(app, admin_user, loja, catalogo):
    """Registrar desperdicio de 1 cesta deve baixar componentes."""
    from app.extensions import db
    from app.models import Desperdicio, EstoqueLoja, Produto, ProdutoItem, Receita
    from app.services.copilot import executar_registrar_desperdicio

    # Receita componente
    pao = Receita(nome='Pao Test Desp', categoria='Paes',
                   rendimento_qtd=1, rendimento_unidade='un', peso_base=100.0)
    db.session.add(pao)
    db.session.flush()

    # Cesta com 5 pao + 2 croissant
    cesta = Produto(nome='Box Desp Test', ativo=True)
    db.session.add(cesta)
    db.session.flush()
    db.session.add_all([
        ProdutoItem(produto_id=cesta.id, tipo='receita',
                     item_nome=pao.nome, receita_id=pao.id, quantidade=5),
        ProdutoItem(produto_id=cesta.id, tipo='receita',
                     item_nome=catalogo['receita'].nome,
                     receita_id=catalogo['receita'].id, quantidade=2),
    ])
    # Estoque inicial: 30 pao, 8 croissant
    db.session.add(EstoqueLoja(loja_id=loja.id, receita_id=pao.id, quantidade=30))
    db.session.add(EstoqueLoja(loja_id=loja.id,
                                  receita_id=catalogo['receita'].id, quantidade=8))
    db.session.commit()

    # Registra: 3 cestas vencidas
    params = {
        'loja_id': loja.id,
        'item_nome': cesta.nome,
        'quantidade': 3,
        'motivo': 'vencido',
    }
    resultado = executar_registrar_desperdicio(params, admin_user)

    assert resultado['ok'], f'esperava ok=True, veio {resultado}'

    # Pao: 30 - (3 cestas × 5 pao) = 15
    ep_pao = EstoqueLoja.query.filter_by(loja_id=loja.id, receita_id=pao.id).first()
    assert ep_pao.quantidade == 15, f'pao esperado 15, ficou {ep_pao.quantidade}'

    # Croissant: 8 - (3 × 2) = 2
    ep_croi = EstoqueLoja.query.filter_by(
        loja_id=loja.id, receita_id=catalogo['receita'].id).first()
    assert ep_croi.quantidade == 2, f'croissant esperado 2, ficou {ep_croi.quantidade}'

    # Cesta NAO foi criada em EstoqueLoja
    ep_cesta = EstoqueLoja.query.filter_by(loja_id=loja.id, produto_id=cesta.id).first()
    assert ep_cesta is None or ep_cesta.quantidade == 0, \
        'nao devia mexer em EstoqueLoja da cesta'

    # Desperdicio cabeca foi gravado com produto_id da cesta (rastreabilidade)
    desp = Desperdicio.query.filter_by(loja_id=loja.id, produto_id=cesta.id).first()
    assert desp is not None and desp.quantidade == 3
