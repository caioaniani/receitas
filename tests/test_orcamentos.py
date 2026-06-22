"""Orcamento B2B: criacao (catalogo + livre), totais, status, PDF.

Dinheiro com peso especial (CLAUDE.md): subtotal/desconto/total exatos.
"""
from decimal import Decimal


def _admin(app):
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='Dona', login='dona', papel='admin', is_owner=True)
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(u.id)
        s['_fresh'] = True
    return c


def _produto(db, nome='Cesta Festa', preco=120.0):
    from app.models import Produto
    p = Produto(nome=nome, categoria='Cestas', preco_site=preco, ativo=True)
    db.session.add(p)
    db.session.commit()
    return p


def test_criar_orcamento_catalogo_e_livre(app):
    from app.extensions import db
    from app.services import orcamentos as svc
    with app.app_context():
        prod = _produto(db, 'Box Corporativo', preco=200.0)
        form = {
            'cliente_nome': 'Hotel Central',
            'cliente_email': 'compras@hotel.com',
            'validade_dias': '10',
            'desconto_valor': '50',
        }
        itens = [
            {'catalogo': f'produto:{prod.id}', 'nome': '', 'qtd': '3',
             'unidade': 'cx', 'preco_unitario': '200'},
            {'catalogo': 'livre', 'nome': 'Servico de buffet', 'qtd': '1',
             'preco_unitario': '500'},
        ]
        orc, erros = svc.criar_orcamento(form, itens, usuario_id=None)
        assert erros == []
        assert orc.codigo.startswith('ORC-')
        assert orc.status == 'rascunho'
        assert len(orc.itens) == 2
        # nome do item de catalogo veio preenchido do produto
        cat_item = next(i for i in orc.itens if i.produto_id == prod.id)
        assert cat_item.nome == 'Box Corporativo'
        # subtotal = 3*200 + 1*500 = 1100; total = 1100 - 50 = 1050
        assert orc.subtotal == Decimal('1100.00')
        assert orc.valor_total == Decimal('1050.00')


def test_criar_orcamento_exige_cliente_e_item(app):
    from app.services import orcamentos as svc
    with app.app_context():
        orc, erros = svc.criar_orcamento({'cliente_nome': ''}, [])
        assert orc is None
        assert any('cliente' in e.lower() for e in erros)
        assert any('item' in e.lower() for e in erros)


def test_codigo_sequencial_por_ano(app):
    from app.extensions import db
    from app.services import orcamentos as svc
    with app.app_context():
        prod = _produto(db)
        itens = [{'catalogo': f'produto:{prod.id}', 'qtd': '1',
                  'preco_unitario': '10'}]
        o1, _ = svc.criar_orcamento({'cliente_nome': 'A'}, itens)
        o2, _ = svc.criar_orcamento({'cliente_nome': 'B'}, itens)
        n1 = int(o1.codigo.split('-')[-1])
        n2 = int(o2.codigo.split('-')[-1])
        assert n2 == n1 + 1


def test_transicao_status(app):
    from app.extensions import db
    from app.services import orcamentos as svc
    with app.app_context():
        prod = _produto(db)
        itens = [{'catalogo': f'produto:{prod.id}', 'qtd': '1',
                  'preco_unitario': '10'}]
        orc, _ = svc.criar_orcamento({'cliente_nome': 'C'}, itens)
        # rascunho -> aprovado direto NAO permitido
        ok, _ = svc.marcar_status(orc, 'aprovado')
        assert ok is False
        # rascunho -> enviado OK
        ok, _ = svc.marcar_status(orc, 'enviado')
        assert ok and orc.status == 'enviado' and orc.enviado_em
        # enviado -> aprovado OK
        ok, _ = svc.marcar_status(orc, 'aprovado')
        assert ok and orc.status == 'aprovado' and orc.aprovado_em
        # aprovado -> qualquer = final
        ok, _ = svc.marcar_status(orc, 'recusado')
        assert ok is False


def test_pdf_gera_bytes(app):
    from app.extensions import db
    from app.services import orcamentos as svc
    from app.services.pdf import gerar_orcamento_pdf
    with app.app_context():
        prod = _produto(db, 'Cesta Natal', preco=80.0)
        itens = [{'catalogo': f'produto:{prod.id}', 'qtd': '2',
                  'preco_unitario': '80'}]
        orc, _ = svc.criar_orcamento({'cliente_nome': 'Empresa Z'}, itens)
        pdf = gerar_orcamento_pdf(orc)
        assert isinstance(pdf, bytes)
        assert pdf[:4] == b'%PDF'


def test_rota_lista_e_novo_abrem(app):
    with app.app_context():
        c = _admin(app)
        r = c.get('/b2b/orcamentos')
        assert r.status_code == 200
        r2 = c.get('/b2b/orcamentos/novo')
        assert r2.status_code == 200


def test_rota_criar_e_detalhe_e_pdf(app):
    from app.extensions import db
    with app.app_context():
        prod = _produto(db, 'Kit Evento', preco=300.0)
        c = _admin(app)
        r = c.post('/b2b/orcamentos/novo', data={
            'cliente_nome': 'Buffet ABC',
            'validade_dias': '7',
            'itens[0][catalogo]': f'produto:{prod.id}',
            'itens[0][nome]': '',
            'itens[0][qtd]': '5',
            'itens[0][preco_unitario]': '300',
        }, follow_redirects=False)
        assert r.status_code in (302, 303)
        from app.models import Orcamento
        orc = Orcamento.query.order_by(Orcamento.id.desc()).first()
        assert orc is not None
        assert orc.valor_total == Decimal('1500.00')
        det = c.get(f'/b2b/orcamentos/{orc.id}')
        assert det.status_code == 200
        pdf = c.get(f'/b2b/orcamentos/{orc.id}/pdf')
        assert pdf.status_code == 200
        assert pdf.data[:4] == b'%PDF'
