"""Relatorio de pedidos (/pedidos/relatorio) usa PRECO INTERNO
(transferencia loja->industria), nao preco_loja/site/atacado.

Pedido do dono em 25/06/2026: PedidoLoja e a loja pedindo a industria, entao
o valor praticado e o `preco_interno`. Antes o relatorio somava por preco_loja
(balcao) / override PrecoLojaReceita. Trava de regressao: se reapontarem pra
preco_loja, o valor_total muda (3 x 10 = 30 em vez de 3 x 2 = 6) e quebra.
"""
from datetime import timedelta

from app.blueprints.pedidos.routes import _preco_interno_item


def _login(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def test_helper_usa_interno_da_receita(app):
    from app.extensions import db
    from app.models import PedidoItem, Receita
    with app.app_context():
        r = Receita(nome='Brioche', categoria='Paes', rendimento_qtd=1,
                    rendimento_unidade='un', peso_base=100.0,
                    preco_interno=2.0, preco_loja=10.0, preco_site=12.0,
                    preco_venda=8.0)
        db.session.add(r)
        db.session.commit()
        # interno 2,00 — nao o loja 10, site 12 ou atacado 8.
        assert _preco_interno_item(PedidoItem(receita_id=r.id, quantidade=1)) == 2.0


def test_helper_usa_interno_do_produto(app):
    """Item que e Produto (ex: cesta) tambem usa preco_interno — antes o
    helper so olhava receita_id e devolvia 0 pra produto."""
    from app.extensions import db
    from app.models import PedidoItem, Produto
    with app.app_context():
        p = Produto(nome='Cesta', ativo=True, preco_interno=5.0,
                    preco_loja=20.0, preco_atacado=15.0)
        db.session.add(p)
        db.session.commit()
        assert _preco_interno_item(PedidoItem(produto_id=p.id, quantidade=1)) == 5.0


def test_helper_sem_interno_retorna_zero_nao_cai_pra_loja(app):
    """Sem preco_interno NAO cai pra preco_loja — retorna 0 (dado faltante
    explicito; misturar fontes num relatorio financeiro seria errado)."""
    from app.extensions import db
    from app.models import PedidoItem, Receita
    with app.app_context():
        r = Receita(nome='Sem Interno', categoria='Paes', rendimento_qtd=1,
                    rendimento_unidade='un', peso_base=100.0,
                    preco_interno=None, preco_loja=10.0)
        db.session.add(r)
        db.session.commit()
        assert _preco_interno_item(PedidoItem(receita_id=r.id, quantidade=1)) == 0


def test_helper_item_sem_vinculo_retorna_zero(app):
    from app.models import PedidoItem
    with app.app_context():
        assert _preco_interno_item(PedidoItem(quantidade=1)) == 0


def test_relatorio_html_soma_pelo_interno(app, admin_user, loja):
    from app.extensions import db
    from app.models import PedidoItem, PedidoLoja, Receita
    from app.utils import hoje
    with app.app_context():
        r = Receita(nome='Brioche Interno', categoria='Paes', rendimento_qtd=1,
                    rendimento_unidade='un', peso_base=100.0,
                    preco_interno=2.0, preco_loja=10.0)
        db.session.add(r)
        db.session.flush()
        p = PedidoLoja(loja_id=loja.id, status='entregue',
                       data_entrega=hoje(), data_pedido=hoje())
        db.session.add(p)
        db.session.flush()
        db.session.add(PedidoItem(pedido_id=p.id, receita_id=r.id, quantidade=3))
        db.session.commit()
        loja_id = loja.id

    client = app.test_client()
    _login(client, admin_user)
    de = (hoje() - timedelta(days=1)).isoformat()
    ate = (hoje() + timedelta(days=1)).isoformat()
    resp = client.get(f'/pedidos/relatorio?loja={loja_id}&de={de}&ate={ate}')
    assert resp.status_code == 200
    # 3 x interno 2,00 = 6,00 ; jamais 3 x loja 10,00 = 30,00.
    assert b'6,00' in resp.data
    assert b'30,00' not in resp.data
    # A nota deixa explicito que e preco interno.
    assert 'preço interno'.encode() in resp.data
