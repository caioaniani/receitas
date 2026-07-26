"""Cardápio PDF "explodido": a cesta traz as fotos dos componentes (26/07/2026).

Pedido do dono: "preciso que o cardápio PDF dos minis exploda para trazer as
fotos que estão na receita e também todas as fotos a mais que vou adicionar
no produto".
"""
import io


def _jpeg(cor=(230, 220, 200)):
    from PIL import Image
    b = io.BytesIO()
    Image.new('RGB', (400, 400), cor).save(b, format='JPEG')
    return b.getvalue()


def _menu_com_fotos(db):
    from app.models import CatalogoFoto, Produto, ProdutoItem, Receita
    p = Produto(nome='Menu Teste', categoria='Minis', preco_atacado=100,
                ativo=True, imagem_blob=_jpeg(), imagem_mimetype='image/jpeg',
                # SÓ menu configurável explode (dono 26/07: "somente pros
                # minis por enquanto").
                menu_configuravel=True, menu_total_unidades=15,
                menu_max_por_item=10)
    db.session.add(p)
    db.session.flush()
    receitas = []
    for i in range(1, 4):
        r = Receita(nome=f'Mini {i}', categoria='Minis', rendimento_qtd=1,
                    rendimento_unidade='un', peso_base=100,
                    imagem_blob=_jpeg(), imagem_mimetype='image/jpeg')
        db.session.add(r)
        db.session.flush()
        db.session.add(ProdutoItem(produto_id=p.id, tipo='receita',
                                   receita_id=r.id, item_nome=r.nome,
                                   quantidade=5, preco_menu=10 + i))
        receitas.append(r)
    # 1 extra do produto + 1 extra de uma receita
    db.session.add(CatalogoFoto(kind='produto', item_id=p.id, ordem=1,
                                dropbox_url='https://x/extra-produto.jpg'))
    db.session.add(CatalogoFoto(kind='receita', item_id=receitas[0].id,
                                ordem=1, dropbox_url='https://x/extra-r1.jpg'))
    db.session.commit()
    return p, receitas


def test_galeria_explodida_junta_componentes_e_extras(app):
    from app.extensions import db
    with app.app_context():
        p, receitas = _menu_com_fotos(db)
        from app.blueprints.main.routes import _galeria_explodida
        g = _galeria_explodida(p)
        # 1 extra do produto + 3 capas de receita + 1 extra de receita
        assert len(g) == 5
        assert g[0]['imagem_url'].endswith('extra-produto.jpg')
        refs = [e['img_ref'] for e in g if e.get('img_ref')]
        assert set(refs) == {('receita', r.id) for r in receitas}
        # a capa do PRÓPRIO produto não entra (já é a foto do card)
        assert ('produto', p.id) not in refs


def test_cesta_sem_componentes_nem_extras_nao_explode(app):
    """Guarda: produto simples segue com o card de sempre, sem mosaico."""
    from app.extensions import db
    from app.models import Produto
    with app.app_context():
        p = Produto(nome='Agua', categoria='Bebidas', preco_atacado=5,
                    ativo=True)
        db.session.add(p)
        db.session.commit()
        from app.blueprints.main.routes import _galeria_explodida
        assert _galeria_explodida(p) == []


def test_pdf_do_cardapio_desenha_o_mosaico(app):
    from app.extensions import db
    with app.app_context():
        _menu_com_fotos(db)
    with app.test_request_context('/'):
        from app.blueprints.main.routes import _cardapio_categorias
        from app.services import cardapio_pdf as svc
        svc.limpar_cache_fotos()
        cats, regras = _cardapio_categorias('atacado')
        alvo = [i for c in cats.values() for i in c
                if i['nome'] == 'Menu Teste']
        assert alvo and len(alvo[0]['galeria']) == 5
        conteudo = svc.gerar_cardapio_pdf('atacado', cats, regras)
    assert conteudo[:4] == b'%PDF'
    assert len(conteudo) > 2000


def test_foto_que_nao_baixa_nao_derruba_o_pdf(app, monkeypatch):
    """As extras vêm de URL: rede fora não pode impedir o cardápio de sair.
    A foto some do mosaico e o resto continua."""
    from app.extensions import db
    with app.app_context():
        _menu_com_fotos(db)

    def _explode(*a, **k):
        raise OSError('rede fora')
    import requests
    monkeypatch.setattr(requests, 'get', _explode)
    with app.test_request_context('/'):
        from app.blueprints.main.routes import _cardapio_categorias
        from app.services import cardapio_pdf as svc
        svc.limpar_cache_fotos()
        cats, regras = _cardapio_categorias('atacado')
        conteudo = svc.gerar_cardapio_pdf('atacado', cats, regras)
    assert conteudo[:4] == b'%PDF'


def test_cesta_de_composicao_FIXA_nao_explode(app):
    """Decisão do dono 26/07/2026: "não precisa para todas as cestas, queria
    somente para os minis por enquanto". Uma cesta comum, mesmo com todos os
    componentes fotografados, segue com o card de sempre."""
    from app.extensions import db
    from app.models import Produto
    with app.app_context():
        p, _receitas = _menu_com_fotos(db)
        from app.blueprints.main.routes import _galeria_explodida
        assert len(_galeria_explodida(p)) == 5      # é menu: explode

        p.menu_configuravel = False                 # vira cesta comum
        db.session.commit()
        assert _galeria_explodida(p) == []

        # e nem por ter componentes com foto uma cesta nova explode
        outra = Produto.query.filter_by(nome='Menu Teste').first()
        assert outra is not None
        assert _galeria_explodida(outra) == []


def test_mosaico_leva_preco_por_unidade_e_pedido_minimo(app):
    """Dono 26/07/2026: "trazer no cardápio PDF o valor individual dos minis
    e a observação de pedido mínimo 30 unidades"."""
    from app.extensions import db
    with app.app_context():
        p, _ = _menu_com_fotos(db)
        from app.blueprints.main.routes import _galeria_explodida
        g = _galeria_explodida(p)
        # extra do produto NÃO tem preço (retrata o menu, não um mini)
        assert g[0]['preco'] is None
        precos = sorted(e['preco'] for e in g if e['preco'] is not None)
        assert precos == [11.0, 11.0, 12.0, 13.0]   # r1 aparece 2x (capa+extra)

    with app.test_request_context('/'):
        from app.blueprints.main.routes import _cardapio_categorias
        cats, _regras = _cardapio_categorias('atacado')
        alvo = [i for c in cats.values() for i in c
                if i['nome'] == 'Menu Teste'][0]
        assert alvo['menu_regra'] == {'total': 15, 'max': 10}


def test_cesta_comum_nao_tem_menu_regra(app):
    from app.extensions import db
    from app.models import Produto
    with app.app_context():
        p = Produto(nome='Cesta Fixa', categoria='Cestas', preco_atacado=50,
                    ativo=True)
        db.session.add(p)
        db.session.commit()
    with app.test_request_context('/'):
        from app.blueprints.main.routes import _cardapio_categorias
        cats, _regras = _cardapio_categorias('atacado')
        alvo = [i for c in cats.values() for i in c
                if i['nome'] == 'Cesta Fixa'][0]
        assert alvo['menu_regra'] is None
