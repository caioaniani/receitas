"""Categoria das cestas (Fase 3) — regra que libera a cartinha no checkout.

Cesta = Produto com composição. seed_cestas_categoria normaliza a categoria
sem clobrar 'Cestas Personalizadas'. A página de produto expõe data-categoria
pro carrinho.js levar a categoria e o checkout decidir mostrar a cartinha.
"""


def _admin_logado(app):
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='Admin', login='adm', papel='admin')
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(u.id)
        s['_fresh'] = True
    return c


def _cesta(db, nome, categoria=None):
    """Produto COM composição (= cesta)."""
    from app.models import Produto, ProdutoItem
    p = Produto(nome=nome, categoria=categoria, preco_site=100.0,
                imagem_dropbox_url='https://x/p.jpg', ativo=True)
    db.session.add(p)
    db.session.flush()
    db.session.add(ProdutoItem(produto_id=p.id, tipo='receita',
                               item_nome='Sourdough', quantidade=1))
    db.session.commit()
    return p


def test_seed_cestas_categoria_corrige_sem_categoria(app):
    from app.extensions import db
    from app.seed import seed_cestas_categoria
    with app.app_context():
        p = _cesta(db, 'Cesta Nova', categoria=None)
        n = seed_cestas_categoria()
        db.session.refresh(p)
        assert p.categoria == 'Cestas'
        assert n >= 1


def test_seed_cestas_nao_clobra_personalizadas(app):
    from app.extensions import db
    from app.seed import seed_cestas_categoria
    with app.app_context():
        p = _cesta(db, 'Personalizada X', categoria='Cestas Personalizadas')
        seed_cestas_categoria()
        db.session.refresh(p)
        assert p.categoria == 'Cestas Personalizadas'  # preservada


def test_seed_cestas_preserva_categoria_manual_do_dono(app):
    """REGRESSÃO (18/06/2026): o dono move uma cesta (com composição) pra
    'Acompanhamentos' na curadoria. O seed do startup NÃO pode reverter pra
    'Cestas' — a curadoria é a fonte de verdade. (Antes, o seed clobrava
    qualquer categoria sem 'cesta' no nome.)"""
    from app.extensions import db
    from app.seed import seed_cestas_categoria
    with app.app_context():
        p = _cesta(db, 'Iogurte Artesanal 600ml', categoria='Acompanhamentos')
        n = seed_cestas_categoria()
        db.session.refresh(p)
        assert p.categoria == 'Acompanhamentos'  # NÃO virou 'Cestas'
        assert n == 0  # nada foi alterado


def test_seed_cestas_ignora_produto_sem_composicao(app):
    from app.extensions import db
    from app.models import Produto
    from app.seed import seed_cestas_categoria
    with app.app_context():
        # Avulso (sem ProdutoItem) não é cesta — categoria intocada.
        avulso = Produto(nome='Granola 500g', categoria='Acompanhamentos',
                         preco_site=49.0, ativo=True,
                         imagem_dropbox_url='https://x/g.jpg')
        db.session.add(avulso)
        db.session.commit()
        seed_cestas_categoria()
        db.session.refresh(avulso)
        assert avulso.categoria == 'Acompanhamentos'


def test_produto_cesta_expoe_categoria_pro_carrinho(app, monkeypatch):
    monkeypatch.delenv('LOJA_VISIVEL', raising=False)
    from app.extensions import db
    from app.services.loja_catalogo import _slugify
    c = _admin_logado(app)
    p = _cesta(db, 'Box Mimo', categoria='Cestas')
    r = c.get(f'/loja/{_slugify(p.nome)}-p{p.id}')
    assert r.status_code == 200
    assert b'data-categoria="Cestas"' in r.data


def test_qtd_formatada_usa_unidade_real(app):
    """Incidente 22/06/2026: 'Family Box' tinha '100x peito de peru' quando
    era 100g. Componentes que sao MP em g/ml/kg/l agora aparecem com unidade
    no nome (100g / 200ml); 'un' continua como '2x'."""
    from app.extensions import db
    from app.models import MateriaPrima, Produto, ProdutoItem, Receita

    # MP em gramas (peito de peru, mussarela): mostra "100g"
    mp_peru = MateriaPrima(nome='Peito de peru', unidade='g', custo_por_kg=0)
    mp_leite = MateriaPrima(nome='Leite', unidade='ml', custo_por_kg=0)
    rec = Receita(nome='Pão Sourdough', categoria='Paes',
                  rendimento_qtd=1, rendimento_unidade='un', peso_base=500.0)
    db.session.add_all([mp_peru, mp_leite, rec])
    db.session.flush()

    box = Produto(nome='Family Box', categoria='Cestas',
                  preco_site=437.0, ativo=True)
    db.session.add(box)
    db.session.flush()
    it_peru = ProdutoItem(produto_id=box.id, tipo='mp',
                          materia_prima_id=mp_peru.id,
                          item_nome='Peito de peru', quantidade=100)
    it_leite = ProdutoItem(produto_id=box.id, tipo='mp',
                           materia_prima_id=mp_leite.id,
                           item_nome='Leite', quantidade=200)
    it_pao = ProdutoItem(produto_id=box.id, tipo='receita',
                         receita_id=rec.id,
                         item_nome='Pão Sourdough', quantidade=2)
    db.session.add_all([it_peru, it_leite, it_pao])
    db.session.commit()

    assert it_peru.qtd_formatada == '100g'
    assert it_leite.qtd_formatada == '200ml'
    assert it_pao.qtd_formatada == '2x'   # receita rendendo em 'un'

    # Trava de orfao: sem FK → 'un' default → 'Nx'
    orfao = ProdutoItem(produto_id=box.id, tipo='mp',
                        materia_prima_id=None,
                        item_nome='Algo solto', quantidade=3)
    assert orfao.qtd_formatada == '3x'

    # Vitrine devolve qtd_formatada no JSON
    from app.services import loja_catalogo
    d = loja_catalogo.por_id_publicado('produto', box.id)
    assert d is not None
    formatadas = {i['nome']: i['qtd_formatada'] for i in d['itens']}
    assert formatadas['Peito de peru'] == '100g'
    assert formatadas['Leite'] == '200ml'
    assert formatadas['Pão Sourdough'] == '2x'


def test_contagem_para_dia_explode_cestas(app):
    """Botao 'Contagem do dia' em /pedidos: pra um dia X, somar TODOS os
    componentes que vao sair de producao. Cestas DESEMPACOTADAS."""
    from datetime import date, datetime, timedelta
    from decimal import Decimal

    from app.extensions import db
    from app.models import (
        PedidoOnline,
        PedidoOnlineItem,
        Produto,
        ProdutoItem,
        Receita,
    )
    from app.services.loja_online_vendas import contagem_para_dia

    dia = date(2026, 6, 26)

    # Catalogo: 1 receita (sourdough) + 1 cesta (family box) com 3 sourdoughs
    sourdough = Receita(nome='Sourdough', categoria='Paes',
                        rendimento_qtd=1, rendimento_unidade='un',
                        peso_base=500.0, preco_site=Decimal('25'))
    db.session.add(sourdough)
    db.session.flush()

    box = Produto(nome='Family Box', categoria='Cestas',
                  preco_site=Decimal('437'), ativo=True)
    db.session.add(box)
    db.session.flush()
    db.session.add(ProdutoItem(produto_id=box.id, tipo='receita',
                               receita_id=sourdough.id,
                               item_nome='Sourdough', quantidade=3))
    db.session.commit()

    # Pedidos pra o dia: 2 boxes pagos + 4 sourdoughs avulsos pago
    def _ped(codigo, status, items):
        p = PedidoOnline(codigo=codigo, nome_cliente='C',
                         email_cliente='c@x.com', modo_entrega='agendada',
                         status=status, subtotal=Decimal('0'),
                         valor_total=Decimal('0'),
                         data_entrega=dia,
                         pago_em=datetime(2026, 6, 25, 12, 0))
        db.session.add(p)
        db.session.flush()
        for it in items:
            db.session.add(PedidoOnlineItem(pedido_id=p.id, **it))
        return p

    # 2 family boxes pago = 2 × 3 sourdough = 6
    _ped('A', 'pago', [{
        'kind': 'produto', 'produto_id': box.id, 'nome': 'Family Box',
        'quantidade': 2, 'preco_unitario': Decimal('437'),
        'subtotal': Decimal('874')}])
    # 4 sourdoughs avulso direto
    _ped('B', 'em_preparo', [{
        'kind': 'receita', 'receita_id': sourdough.id, 'nome': 'Sourdough',
        'quantidade': 4, 'preco_unitario': Decimal('25'),
        'subtotal': Decimal('100')}])
    # Cancelado e aguardando_pagamento NAO contam
    _ped('C', 'cancelado', [{
        'kind': 'receita', 'receita_id': sourdough.id, 'nome': 'Sourdough',
        'quantidade': 99, 'preco_unitario': Decimal('25'),
        'subtotal': Decimal('2475')}])
    _ped('D', 'aguardando_pagamento', [{
        'kind': 'receita', 'receita_id': sourdough.id, 'nome': 'Sourdough',
        'quantidade': 99, 'preco_unitario': Decimal('25'),
        'subtotal': Decimal('2475')}])
    # Outro dia NAO conta
    p_outro = PedidoOnline(codigo='OUTRO', nome_cliente='C',
                           email_cliente='c@x.com', modo_entrega='agendada',
                           status='pago', subtotal=Decimal('25'),
                           valor_total=Decimal('25'),
                           data_entrega=dia + timedelta(days=1))
    db.session.add(p_outro)
    db.session.flush()
    db.session.add(PedidoOnlineItem(
        pedido_id=p_outro.id, kind='receita', receita_id=sourdough.id,
        nome='Sourdough', quantidade=99,
        preco_unitario=Decimal('25'), subtotal=Decimal('2475')))
    db.session.commit()

    itens = contagem_para_dia(dia)
    # 2 boxes × 3 sourdoughs + 4 sourdoughs = 10
    sd = next(i for i in itens if i['nome'] == 'Sourdough')
    assert sd['qtd'] == 10
    # Detalhe deve ter as 2 origens
    origens = {o for o, _ in sd['detalhes']}
    assert 'Family Box' in origens
    assert 'venda direta' in origens
