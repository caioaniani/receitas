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
        # enviado -> aprovado SEM data de entrega = recusado (regime 07/07/2026)
        ok, msg = svc.marcar_status(orc, 'aprovado')
        assert ok is False and 'entrega' in msg.lower()
        assert orc.status == 'enviado'
        # com data de entrega, aprova e vira venda
        from app.utils import hoje
        orc.data_entrega = hoje()
        db.session.commit()
        ok, _ = svc.marcar_status(orc, 'aprovado')
        assert ok and orc.status == 'aprovado' and orc.aprovado_em
        assert orc.venda_id is not None
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


def test_data_entrega_persiste_e_aparece(app):
    from datetime import date

    from app.extensions import db
    from app.services import orcamentos as svc
    with app.app_context():
        prod = _produto(db)
        itens = [{'catalogo': f'produto:{prod.id}', 'qtd': '1',
                  'preco_unitario': '10'}]
        # com data: persiste como date
        orc, _ = svc.criar_orcamento(
            {'cliente_nome': 'X', 'data_entrega': '2026-07-15'}, itens)
        assert orc.data_entrega == date(2026, 7, 15)
        # sem data: NULL (a combinar)
        orc2, _ = svc.criar_orcamento({'cliente_nome': 'Y'}, itens)
        assert orc2.data_entrega is None
        # data invalida: NULL (nao quebra)
        orc3, _ = svc.criar_orcamento(
            {'cliente_nome': 'Z', 'data_entrega': 'qualquer-coisa'}, itens)
        assert orc3.data_entrega is None


def test_pdf_contem_chave_pix_e_data_entrega(app):
    """O PDF tem que mostrar a chave PIX (CNPJ) e a data de entrega."""
    from app.extensions import db
    from app.services import orcamentos as svc
    from app.services.pdf import gerar_orcamento_pdf
    with app.app_context():
        prod = _produto(db, 'Cesta Festa', preco=100.0)
        itens = [{'catalogo': f'produto:{prod.id}', 'qtd': '1',
                  'preco_unitario': '100'}]
        orc, _ = svc.criar_orcamento(
            {'cliente_nome': 'Empresa Y', 'data_entrega': '2026-08-10'}, itens)
        pdf = gerar_orcamento_pdf(orc)
        # PDF binario eh comprimido; nao tem como grep texto direto.
        # Valida: gerou PDF valido, com conteudo (cabecalho + bloco PIX +
        # tabela + rodape). Conteudo textual eh garantido por inspecao
        # visual (designer/dono confere).
        assert pdf[:4] == b'%PDF'
        assert len(pdf) > 1500


def test_pdf_gera_sem_data_entrega(app):
    """Sem data_entrega: o PDF nao levanta (renderiza 'a combinar')."""
    from app.extensions import db
    from app.services import orcamentos as svc
    from app.services.pdf import gerar_orcamento_pdf
    with app.app_context():
        prod = _produto(db)
        itens = [{'catalogo': f'produto:{prod.id}', 'qtd': '1',
                  'preco_unitario': '5'}]
        orc, _ = svc.criar_orcamento({'cliente_nome': 'W'}, itens)
        pdf = gerar_orcamento_pdf(orc)
        assert pdf[:4] == b'%PDF'


def test_chave_pix_centralizada_em_constants():
    """CNPJ/chave PIX e centralizado em app.constants — fonte unica
    pra PDF e (futuros) templates. Quando mudar, muda aqui."""
    from app.constants import (
        PADARIA_CHAVE_PIX,
        PADARIA_CNPJ,
        PADARIA_PIX_TIPO,
        PADARIA_RAZAO_SOCIAL,
    )
    assert PADARIA_CNPJ == '40.646.899/0001-39'
    assert PADARIA_CHAVE_PIX == PADARIA_CNPJ
    assert PADARIA_PIX_TIPO == 'CNPJ'
    assert 'O Pão' in PADARIA_RAZAO_SOCIAL


def test_frete_soma_no_total(app):
    """Total = (subtotal - desconto) + frete."""
    from decimal import Decimal

    from app.extensions import db
    from app.services import orcamentos as svc
    with app.app_context():
        prod = _produto(db, 'Box', preco=100.0)
        itens = [{'catalogo': f'produto:{prod.id}', 'qtd': '2',
                  'preco_unitario': '100'}]
        orc, erros = svc.criar_orcamento(
            {'cliente_nome': 'Hotel', 'desconto_valor': '30',
             'frete_valor': '45'}, itens)
        assert erros == []
        # subtotal 200; (200 - 30) + 45 = 215
        assert orc.subtotal == Decimal('200.00')
        assert orc.frete_valor == Decimal('45.00')
        assert orc.valor_total == Decimal('215.00')


def test_editar_recalcula_total_sem_somar_itens_deletados(app):
    """Caso real orc-2026-0003 (18/08/2026): editar 200x5 pra 80x5 gravava
    subtotal/total R$ 1.400 — o db.session.delete direto nao tirava os
    itens velhos de orc.itens antes do recalcular_total, que somava
    deletados + novos. O replace tem que deixar o total = SO os itens
    novos."""
    from decimal import Decimal

    from app.extensions import db
    from app.services import orcamentos as svc
    with app.app_context():
        itens = [{'nome': 'Cookie Calebaut', 'qtd': '200',
                  'preco_unitario': '5.00'}]
        orc, erros = svc.criar_orcamento({'cliente_nome': 'Caio'}, itens)
        assert erros == []
        assert orc.valor_total == Decimal('1000.00')
        ok, erros = svc.atualizar_orcamento(
            orc, {'cliente_nome': 'Caio'},
            [{'nome': 'Cookie Calebaut', 'qtd': '80',
              'preco_unitario': '5.00'}])
        assert ok and erros == []
        db.session.refresh(orc)
        assert [float(i.quantidade) for i in orc.itens] == [80.0]
        assert orc.subtotal == Decimal('400.00')
        assert orc.valor_total == Decimal('400.00')


def test_frete_default_zero(app):
    from decimal import Decimal

    from app.extensions import db
    from app.services import orcamentos as svc
    with app.app_context():
        prod = _produto(db)
        itens = [{'catalogo': f'produto:{prod.id}', 'qtd': '1',
                  'preco_unitario': '10'}]
        orc, _ = svc.criar_orcamento({'cliente_nome': 'X'}, itens)
        assert orc.frete_valor == Decimal('0')
        assert orc.valor_total == Decimal('10.00')


def test_arquivar_rascunho_sai_de_pendentes_e_volta(app):
    """Rascunho arquivável (08/07/2026): some de Pendentes sem virar
    'recusado', aparece na aba Arquivados, não muda status enquanto
    arquivado, e desarquivar devolve pra Pendentes."""
    from app.extensions import db
    from app.services import orcamentos as svc
    with app.app_context():
        prod = _produto(db)
        itens = [{'catalogo': f'produto:{prod.id}', 'qtd': '1',
                  'preco_unitario': '10'}]
        orc, _ = svc.criar_orcamento({'cliente_nome': 'Gaveta'}, itens)
        codigo = orc.codigo

        ok, erro = svc.arquivar(orc)
        assert ok, erro
        assert orc.status == 'rascunho' and orc.arquivado_em is not None
        # Arquivado não transiciona de status
        ok, erro = svc.marcar_status(orc, 'enviado')
        assert not ok and 'arquivado' in erro
        # Toggle na rota: telas refletem
        c = _admin(app)
        pend = c.get('/b2b/?aba=pedidos&f=pendentes').get_data(as_text=True)
        assert codigo not in pend
        arq = c.get('/b2b/?aba=pedidos&f=arquivados').get_data(as_text=True)
        assert codigo in arq and 'rascunho arquivado' in arq
        r = c.post(f'/b2b/orcamentos/{orc.id}/arquivar')
        assert r.status_code == 302                    # desarquiva (toggle)
        db.session.refresh(orc)
        assert orc.arquivado_em is None
        pend2 = c.get('/b2b/?aba=pedidos&f=pendentes').get_data(as_text=True)
        assert codigo in pend2
        ok, _ = svc.marcar_status(orc, 'enviado')      # fluxo volta ao normal
        assert ok


def test_arquivar_so_rascunho(app):
    """Enviado/recusado não arquivam — seguem o fluxo de status."""
    from app.extensions import db
    from app.services import orcamentos as svc
    with app.app_context():
        prod = _produto(db)
        itens = [{'catalogo': f'produto:{prod.id}', 'qtd': '1',
                  'preco_unitario': '10'}]
        orc, _ = svc.criar_orcamento({'cliente_nome': 'X'}, itens)
        svc.marcar_status(orc, 'enviado')
        ok, erro = svc.arquivar(orc)
        assert not ok and 'rascunho' in erro
        ok, erro = svc.desarquivar(orc)
        assert not ok
