"""Regressão: relatório de pedidos com por_item dict (19/06/2026).

Bug em produção (Sentry 5c9972..., /pedidos/relatorio?formato=pdf): `por_item`
é um defaultdict montado na rota; `gerar_pdf_pedidos`/`gerar_xlsx_pedidos`
faziam `for nome, d in por_item:` — iterar dict dá as CHAVES (nomes), e
desempacotar um nome com >2 letras em (nome, d) estoura "too many values to
unpack (expected 2)". Faltava `.items()`.
"""
from datetime import date


def _dados():
    por_item = {
        'Croissant Tradicional': {'quantidade': 5, 'recebido': 5, 'valor': 50.0},
        'Pão Francês Fermentado': {'quantidade': 10, 'recebido': 9, 'valor': 45.0},
    }
    totais = {'qtd_pedidos': 2, 'valor_total': 95.0, 'divergencias': 1}
    return por_item, totais


def test_gerar_pdf_pedidos_por_item_dict_nao_estoura(app):
    from app.services.relatorio import gerar_pdf_pedidos
    por_item, totais = _dados()
    with app.app_context():
        buf = gerar_pdf_pedidos('Loja X', date(2026, 6, 1), date(2026, 6, 18),
                                [], totais, por_item)
    assert buf.getbuffer().nbytes > 0


def test_gerar_xlsx_pedidos_por_item_dict_nao_estoura(app):
    from app.services.relatorio import gerar_xlsx_pedidos
    por_item, totais = _dados()
    with app.app_context():
        buf = gerar_xlsx_pedidos('Loja X', date(2026, 6, 1), date(2026, 6, 18),
                                 [], totais, por_item)
    assert buf.getbuffer().nbytes > 0


def test_relatorio_pdf_rota_nao_estoura(app):
    """Ponta a ponta: a rota /pedidos/relatorio?formato=pdf com pedidos
    entregues reais não pode mais dar 500 (era o caso do Sentry)."""
    from decimal import Decimal

    from app.extensions import db
    from app.models import Loja, PedidoItem, PedidoLoja, Usuario
    from app.utils import hoje
    with app.app_context():
        u = Usuario(nome='Adm', login='adm', papel='admin')
        u.set_senha('x' * 8)
        loja = Loja(nome='Loja Rel', ativa=True, endereco='Rua R, 1')
        db.session.add_all([u, loja])
        db.session.commit()
        ped = PedidoLoja(loja_id=loja.id, status='entregue',
                         data_entrega=hoje(), valor_total=Decimal('50'))
        db.session.add(ped)
        db.session.flush()
        ped.itens.append(PedidoItem(
            nome_item='Croissant Tradicional', quantidade=5,
            quantidade_recebida=5))
        db.session.commit()
        loja_id, uid = loja.id, u.id
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(uid)
        s['_fresh'] = True
    r = c.get(f'/pedidos/relatorio?loja={loja_id}&de={date(2026,6,1)}'
              f'&ate={hoje_iso()}&formato=pdf&fotos=1')
    assert r.status_code == 200
    assert r.mimetype == 'application/pdf'


def hoje_iso():
    from app.utils import hoje
    return hoje().isoformat()
