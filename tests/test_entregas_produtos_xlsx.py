"""Export XLSX da aba Produtos do /entregas (08/08/2026, pedido do dono).

Rota `/entregas/produtos.xlsx` re-agrega no SERVIDOR pelo MESMO motor da aba
(`_produtos_do_dia`) — nunca o estado do navegador. Duas abas: "Vendidos no
dia" (cesta = 1 linha, com valor) e "A produzir" (cesta explodida em
componentes).
"""
import io

from openpyxl import load_workbook

from app.extensions import db
from app.models import (
    PedidoOnline,
    PedidoOnlineItem,
    Produto,
    ProdutoItem,
    Receita,
    Usuario,
)
from app.utils import hoje


def _admin_client(app):
    u = Usuario(nome='Adm', login='adm_xlsx', papel='admin')
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(u.id)
        s['_fresh'] = True
    return c


def _cenario(janela='06:00–10:00'):
    """Cesta (2 croissants por unidade) vendida 3x + 1 item simples."""
    r = Receita(nome='Croissant Tradicional', categoria='Croissants',
                rendimento_qtd=1, rendimento_unidade='un', peso_base=100)
    db.session.add(r)
    db.session.flush()
    cesta = Produto(nome='Cesta Dia dos Pais', ativo=True)
    db.session.add(cesta)
    db.session.flush()
    db.session.add(ProdutoItem(produto_id=cesta.id, tipo='receita',
                               receita_id=r.id, item_nome=r.nome,
                               quantidade=2))
    p = PedidoOnline(codigo='XLSX1', nome_cliente='C', email_cliente='x@x.com',
                     status='pago', modo_entrega='agendada',
                     data_entrega=hoje(), janela_entrega=janela,
                     valor_total=450)
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoOnlineItem(pedido_id=p.id, kind='produto',
                                    produto_id=cesta.id, nome=cesta.nome,
                                    preco_unitario=150, quantidade=3,
                                    subtotal=450))
    db.session.commit()
    return r, cesta


def _abrir(resp):
    return load_workbook(io.BytesIO(resp.data), read_only=True)


def test_xlsx_tem_as_duas_abas_com_cesta_e_explosao(app):
    with app.app_context():
        _cenario()
        c = _admin_client(app)
        resp = c.get(f'/entregas/produtos.xlsx?data={hoje().isoformat()}')
        assert resp.status_code == 200
        assert 'spreadsheetml' in resp.mimetype
        assert hoje().isoformat() in resp.headers.get(
            'Content-Disposition', '')
        wb = _abrir(resp)
        assert wb.sheetnames == ['Vendidos no dia', 'A produzir']

        vend = [[c_.value for c_ in row] for row in wb['Vendidos no dia'].rows]
        # A cesta aparece COMO VENDIDA (1 linha, qtd 3, valor 450).
        linha = next(li for li in vend if li[0] == 'Cesta Dia dos Pais')
        assert linha[2] == 3 and float(linha[4]) == 450.0
        total = next(li for li in vend if li[0] == 'TOTAL')
        assert total[2] == 3 and float(total[4]) == 450.0

        prod = [[c_.value for c_ in row] for row in wb['A produzir'].rows]
        # Explodida: 3 cestas x 2 croissants = 6 un, com a origem anotada.
        linha = next(li for li in prod if li[0] == 'Croissant Tradicional')
        assert linha[1] == 6 and linha[2] == 'un'
        assert 'Cesta Dia dos Pais' in (linha[3] or '')
        total = next(li for li in prod if li[0] == 'TOTAL')
        assert '6 un' in str(total[1])


def test_filtro_de_janela_vale_no_xlsx(app):
    """Mesmos filtros da aba: janela que não casa = lista vazia."""
    with app.app_context():
        _cenario(janela='06:00–10:00')
        c = _admin_client(app)
        resp = c.get(f'/entregas/produtos.xlsx?data={hoje().isoformat()}'
                     '&janela=09:00–10:00')
        wb = _abrir(resp)
        vend = [[c_.value for c_ in row] for row in wb['Vendidos no dia'].rows]
        assert not any(li[0] == 'Cesta Dia dos Pais' for li in vend)
        # E o subtítulo diz qual janela foi aplicada (rastreável no papel).
        assert any('09:00–10:00' in str(li[0]) for li in vend if li and li[0])


def test_xlsx_exige_login(app):
    r = app.test_client().get('/entregas/produtos.xlsx')
    assert r.status_code in (302, 401, 403)
