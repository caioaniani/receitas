from app.extensions import db
from app.models import MateriaPrima, Receita, ReceitaIngrediente, Produto, ProdutoItem


def seed_database():
    """Popula o banco com dados reais da padaria."""
    if MateriaPrima.query.first() is not None:
        return

    # ── Matérias-Primas ──
    mps = [
        MateriaPrima(nome='FarinhaT65', unidade='g', custo_por_kg=10.00),
        MateriaPrima(nome='FarinhaT45', unidade='g', custo_por_kg=10.20),
        MateriaPrima(nome='Farinha Integral', unidade='g', custo_por_kg=13.70),
        MateriaPrima(nome='Manteiga', unidade='g', custo_por_kg=40.00),
        MateriaPrima(nome='Manteiga para Folhar', unidade='g', custo_por_kg=125.00),
        MateriaPrima(nome='Leite', unidade='ml', custo_por_kg=6.00),
        MateriaPrima(nome='Acucar', unidade='g', custo_por_kg=4.20),
        MateriaPrima(nome='Mel', unidade='g', custo_por_kg=9.90),
        MateriaPrima(nome='Sal', unidade='g', custo_por_kg=3.00),
        MateriaPrima(nome='Fermento', unidade='g', custo_por_kg=22.00),
        MateriaPrima(nome='Levain', unidade='g', custo_por_kg=10.00),
        MateriaPrima(nome='Agua(1L)', unidade='ml', custo_por_kg=2.00),
        MateriaPrima(nome='Óleo de Coco', unidade='ml', custo_por_kg=55.00),
        MateriaPrima(nome='Ovos', unidade='g', custo_por_kg=30.00),
        MateriaPrima(nome='7 Grãos', unidade='g', custo_por_kg=25.35),
        MateriaPrima(nome='Nozes e Azeitonas', unidade='g', custo_por_kg=36.00),
        MateriaPrima(nome='Nutella', unidade='g', custo_por_kg=63.00),
        MateriaPrima(nome='Baton Calebaut', unidade='g', custo_por_kg=103.33),
        MateriaPrima(nome='Chocolate 811', unidade='g', custo_por_kg=177.79),
        MateriaPrima(nome='Cacau', unidade='g', custo_por_kg=50.40),
        MateriaPrima(nome='Chocolate Chips ao Leite', unidade='g', custo_por_kg=24.00),
        MateriaPrima(nome='F. de Amendoas', unidade='g', custo_por_kg=90.85),
        MateriaPrima(nome='Amendoas laminadas', unidade='g', custo_por_kg=90.27),
        MateriaPrima(nome='Castanha de Caju', unidade='g', custo_por_kg=110.27),
        MateriaPrima(nome='Cramberry', unidade='g', custo_por_kg=50.41),
        MateriaPrima(nome='Lascas de Coco', unidade='g', custo_por_kg=106.66),
        MateriaPrima(nome='Corn Flakes', unidade='g', custo_por_kg=26.02),
        MateriaPrima(nome='Aveia', unidade='g', custo_por_kg=9.00),
        MateriaPrima(nome='Chia', unidade='g', custo_por_kg=21.00),
        MateriaPrima(nome='Baunilha', unidade='ml', custo_por_kg=10.07),
        MateriaPrima(nome='Bicarbonato', unidade='g', custo_por_kg=7.00),
        MateriaPrima(nome='Morango fresco', unidade='g', custo_por_kg=20.00),
    ]
    db.session.add_all(mps)
    db.session.flush()

    # ── Helper: ingredientes base do croissant ──
    croissant_base = [
        ('FarinhaT45', 100, True, ''),
        ('Levain', 7.5, False, ''),
        ('Manteiga', 5, False, ''),
        ('Agua(1L)', 20, False, ''),
        ('Acucar', 14, False, ''),
        ('Sal', 2, False, ''),
        ('Leite', 25, False, ''),
        ('Ovos', 5, False, ''),
        ('Fermento', 0.4, False, ''),
        ('Manteiga para Folhar', 27.94, False, ''),
    ]

    def add_receita(nome, cat, rend_qtd, rend_un, peso_base, ingredientes, peso_unitario=None):
        r = Receita(nome=nome, categoria=cat, rendimento_qtd=rend_qtd,
                    rendimento_unidade=rend_un, peso_base=peso_base,
                    peso_unitario=peso_unitario)
        db.session.add(r)
        db.session.flush()
        for ing_nome, pct, base, nota in ingredientes:
            db.session.add(ReceitaIngrediente(
                receita_id=r.id, ingrediente_nome=ing_nome,
                porcentagem=pct, eh_base=base, nota=nota))

    # ── 1. Croissant Tradicional ──
    add_receita('Croissant Tradicional', 'Viennoiserie', 16, 'unidades', 1000,
                croissant_base, peso_unitario=130)

    # ── 2. Pain au Chocolat ──
    add_receita('Pain au Chocolat', 'Viennoiserie', 12, 'unidades', 1000,
                croissant_base + [('Baton Calebaut', 36, False, '')],
                peso_unitario=200)

    # ── 3. Croissant Nutella com Morango ──
    add_receita('Croissant Nutella com Morango', 'Viennoiserie', 16, 'unidades', 1000,
                croissant_base + [
                    ('Nutella', 80, False, ''),
                    ('Morango fresco', 200, False, ''),
                ], peso_unitario=300)

    # ── 4. Croissant Almond ──
    add_receita('Croissant Almond', 'Viennoiserie', 16, 'unidades', 1000,
                croissant_base + [
                    ('Ovos', 32.13, False, 'Creme de Amêndoas'),
                    ('Acucar', 66.94, False, 'Creme de Amêndoas'),
                    ('Manteiga', 66.94, False, 'Creme de Amêndoas'),
                    ('F. de Amendoas', 66.94, False, 'Creme de Amêndoas'),
                    ('FarinhaT45', 6.69, False, 'Creme de Amêndoas'),
                    ('Baunilha', 0.33, False, 'Creme de Amêndoas'),
                    ('Amendoas laminadas', 32, False, 'Cobertura'),
                ], peso_unitario=250)

    # ── 5. Sourdough Tradicional ──
    add_receita('Sourdough Tradicional', 'Pães', 4, 'pães', 1000, [
        ('FarinhaT65', 100, True, ''),
        ('Agua(1L)', 80, False, ''),
        ('Levain', 25, False, ''),
        ('Sal', 2, False, ''),
        ('Fermento', 0.5, False, ''),
    ], peso_unitario=500)

    # ── 6. Sourdough Integral ──
    add_receita('Sourdough Integral', 'Pães', 4, 'pães', 1000, [
        ('Farinha Integral', 100, True, ''),
        ('Agua(1L)', 75, False, ''),
        ('Levain', 25, False, ''),
        ('Sal', 2, False, ''),
        ('Fermento', 0.5, False, ''),
    ], peso_unitario=500)

    # ── 7. Sourdough 7 Grãos ──
    add_receita('Sourdough 7 Grãos', 'Pães', 4, 'pães', 1000, [
        ('FarinhaT65', 100, True, ''),
        ('Agua(1L)', 85, False, ''),
        ('Levain', 25, False, ''),
        ('Sal', 2, False, ''),
        ('Fermento', 0.5, False, ''),
        ('7 Grãos', 10, False, ''),
    ], peso_unitario=530)

    # ── 8. Sourdough Nozes e Azeitonas ──
    add_receita('Sourdough Nozes e Azeitonas', 'Pães', 4, 'pães', 1000, [
        ('FarinhaT65', 100, True, ''),
        ('Agua(1L)', 75, False, ''),
        ('Levain', 25, False, ''),
        ('Sal', 2, False, ''),
        ('Fermento', 0.5, False, ''),
        ('Nozes e Azeitonas', 25, False, ''),
    ], peso_unitario=520)

    # ── 9. Brioche ──
    add_receita('Brioche', 'Pães', 4, 'unidades', 1000, [
        ('FarinhaT45', 100, True, ''),
        ('Acucar', 25, False, ''),
        ('Ovos', 15, False, ''),
        ('Sal', 2, False, ''),
        ('Fermento', 7, False, ''),
        ('Manteiga', 40, False, ''),
        ('Agua(1L)', 20, False, ''),
    ], peso_unitario=500)

    # ── 10. Pão Francês Fermentado ──
    add_receita('Pão Francês Fermentado', 'Pães', 20, 'unidades', 1000, [
        ('FarinhaT65', 100, True, ''),
        ('Agua(1L)', 75, False, ''),
        ('Levain', 25, False, ''),
        ('Sal', 2, False, ''),
        ('Fermento', 0.5, False, ''),
    ], peso_unitario=100)

    # ── 11. Pão de Forma Integral ──
    add_receita('Pão de Forma Integral', 'Pães', 4, 'pães', 1000, [
        ('Farinha Integral', 100, True, ''),
        ('Acucar', 16, False, ''),
        ('Sal', 2, False, ''),
        ('Fermento', 3, False, ''),
        ('Agua(1L)', 75, False, ''),
    ], peso_unitario=500)

    # ── 12. Pão de Forma Integral com Grãos ──
    add_receita('Pão de Forma Integral com Grãos', 'Pães', 4, 'pães', 1000, [
        ('Farinha Integral', 100, True, ''),
        ('Acucar', 16, False, ''),
        ('Sal', 2, False, ''),
        ('Fermento', 3, False, ''),
        ('Agua(1L)', 75, False, ''),
        ('7 Grãos', 12, False, ''),
    ], peso_unitario=500)

    # ── 13. Cookie Calebaut ──
    add_receita('Cookie Calebaut', 'Cookies', 80, 'cookies', 280, [
        ('FarinhaT65', 100, True, ''),
        ('Manteiga', 100, False, ''),
        ('Acucar', 214.29, False, ''),
        ('Ovos', 171.43, False, ''),
        ('Chocolate 811', 342.86, False, ''),
        ('Cacau', 21.43, False, ''),
        ('Bicarbonato', 7.14, False, ''),
        ('Chocolate Chips ao Leite', 285.71, False, ''),
    ], peso_unitario=45)

    # ── 14. Granola Artesanal ──
    add_receita('Granola Artesanal', 'Granola', 30, 'embalagens 500g', 4000, [
        ('Aveia', 100, True, ''),
        ('Corn Flakes', 100, False, ''),
        ('Cramberry', 51.88, False, ''),
        ('Castanha de Caju', 26.88, False, ''),
        ('Amendoas laminadas', 26.88, False, ''),
        ('Lascas de Coco', 26.88, False, ''),
        ('Chia', 25, False, ''),
        ('Mel', 12.5, False, ''),
        ('Óleo de Coco', 12.5, False, ''),
    ], peso_unitario=500)

    db.session.commit()


def seed_cardapio():
    """Importa todos os itens do cardápio (adiciona apenas o que falta)."""
    existentes_prod = {p.nome for p in Produto.query.all()}

    # ── 1. Atualizar preco_loja das receitas existentes ──
    precos_loja = {
        'Sourdough Tradicional': 29.00,
        'Sourdough Integral': 29.00,
        'Sourdough Nozes e Azeitonas': 35.00,
        'Sourdough 7 Grãos': 35.00,
        'Brioche': 42.00,
        'Pão Francês Fermentado': 3.00,
        'Cookie Calebaut': 11.00,
        'Croissant Tradicional': 19.00,
        'Pain au Chocolat': 24.00,
        'Croissant Almond': 30.00,
        'Croissant Nutella com Morango': 30.00,
        'Pão de Forma Integral': 40.00,
        'Pão de Forma Integral com Grãos': 42.00,
        'Granola Artesanal': 7.00,
    }
    for r in Receita.query.all():
        if r.nome in precos_loja and not r.preco_loja:
            r.preco_loja = precos_loja[r.nome]
    db.session.flush()

    # ── 2. Novas matérias-primas (custo 0 para o user preencher) ──
    novas_mps = [
        ('Queijo Prato', 'g', 0),
        ('Queijo Branco', 'g', 0),
        ('Presunto', 'g', 0),
        ('Peito de Peru', 'g', 0),
        ('Requeijão', 'g', 0),
        ('Café em Grão', 'g', 0),
        ('Leite Vegetal', 'ml', 0),
        ('Chá Twinings (sachê)', 'un', 0),
        ('Toddy/Padre', 'g', 0),
        ('Laranja', 'g', 0),
        ('Abacaxi', 'g', 0),
        ('Açaí (polpa)', 'g', 0),
        ('Iogurte', 'g', 0),
        ('Banana', 'g', 0),
        ('Folhas/Alface', 'g', 0),
        ('Tomate Cereja', 'g', 0),
        ('Muçarela de Búfala', 'g', 0),
        ('Molho Pesto', 'g', 0),
        ('Lagarto Cozido', 'g', 0),
        ('Amendoim', 'g', 0),
        ('Água Mineral 300ml', 'un', 0),
        ('Coca-Cola', 'un', 0),
        ('Guaraná', 'un', 0),
        ('Água de Coco', 'un', 0),
        ('Proteína Whey 28g', 'un', 0),
        ('Proteína Syntha 30g', 'un', 0),
        ('Leite em Pó', 'g', 0),
        ('Coco (fruta)', 'un', 0),
    ]
    existentes = {mp.nome for mp in MateriaPrima.query.all()}
    for nome, unidade, custo in novas_mps:
        if nome not in existentes:
            db.session.add(MateriaPrima(nome=nome, unidade=unidade, custo_por_kg=custo))
    db.session.flush()

    # ── 3. Novas fichas técnicas (vazias, para preencher depois) ──
    novas_receitas = [
        ('Cinnamon Roll', 'Viennoiserie', 22.00),
        ('Croissant Nutella', 'Viennoiserie', 26.00),
        ('Focaccia Gorgonzola', 'Fornadas Especiais', 47.00),
        ('Baguette Francesa', 'Fornadas Especiais', 21.00),
        ('Danish de Calabresa', 'Fornadas Especiais', 23.00),
        ('Danish de Muçarela de Búfala', 'Fornadas Especiais', 23.00),
        ('Cone de Pão de Queijo', 'Pães', 1.40),
    ]
    existentes_rec = {r.nome for r in Receita.query.all()}
    for nome, cat, preco_loja in novas_receitas:
        if nome not in existentes_rec:
            db.session.add(Receita(
                nome=nome, categoria=cat, preco_loja=preco_loja,
                rendimento_qtd=1, rendimento_unidade='unidades', peso_base=1000,
            ))
    db.session.flush()

    # ── 4. Produtos — Cafés e Bebidas Quentes ──
    def add_prod(nome, cat, preco_loja, descricao=''):
        if nome in existentes_prod:
            return None
        p = Produto(nome=nome, categoria=cat, preco_loja=preco_loja, descricao=descricao)
        db.session.add(p)
        return p

    def add_prod_comp(nome, cat, preco_loja, itens, descricao=''):
        if nome in existentes_prod:
            return None
        p = Produto(nome=nome, categoria=cat, preco_loja=preco_loja, descricao=descricao)
        db.session.add(p)
        db.session.flush()
        for tipo, item_nome, qtd in itens:
            db.session.add(ProdutoItem(
                produto_id=p.id, tipo=tipo, item_nome=item_nome, quantidade=qtd))
        return p

    # Cafés e Bebidas Quentes
    add_prod('Café Espresso', 'Cafés e Bebidas Quentes', 12.00)
    add_prod('Café Espresso com Leite PQ', 'Cafés e Bebidas Quentes', 13.00)
    add_prod('Café Espresso Leite Médio', 'Cafés e Bebidas Quentes', 13.00)
    add_prod('Café Espresso Leite Vegetal', 'Cafés e Bebidas Quentes', 23.00)
    add_prod('Café Espresso Duplo', 'Cafés e Bebidas Quentes', 19.00)
    add_prod('Café Especial Coado', 'Cafés e Bebidas Quentes', 15.00)
    add_prod('Café Especial Coado com Leite', 'Cafés e Bebidas Quentes', 16.00)
    add_prod('Cappuccino com Chocolate Belga', 'Cafés e Bebidas Quentes', 27.00)
    add_prod('Chá Twinings', 'Cafés e Bebidas Quentes', 8.00, 'Erva-Doce, Camomila ou Verde')
    add_prod('Chocolate Quente', 'Cafés e Bebidas Quentes', 15.00, 'Padre ou Toddy')
    add_prod('Chá de Amendoim', 'Cafés e Bebidas Quentes', 19.00)
    add_prod('Copo de Leite', 'Cafés e Bebidas Quentes', 9.00)
    add_prod('Copo de Leite Vegetal', 'Cafés e Bebidas Quentes', 18.00)

    # Bebidas
    add_prod('Suco de Laranja Natural', 'Bebidas', 18.00)
    add_prod('Suco de Laranja 1 Litro', 'Bebidas', 48.00)
    add_prod('Suco Verde', 'Bebidas', 28.00, 'Laranja, Abacaxi, Couve e Gengibre')
    add_prod('Suco de Abacaxi com Hortelã', 'Bebidas', 28.00)
    add_prod('Água Sem Gás 300ml', 'Bebidas', 9.00)
    add_prod('Água São Lourenço com Gás 300ml', 'Bebidas', 9.00)
    add_prod('Coca Zero ou Normal', 'Bebidas', 10.00)
    add_prod('Guaraná Zero ou Normal', 'Bebidas', 10.00)
    add_prod('Chocolate Frio', 'Bebidas', 18.00, 'Toddy ou Padre')
    add_prod('Água de Coco (no Coco)', 'Bebidas', 18.00)
    add_prod('Suco de Açaí', 'Bebidas', 19.00)
    add_prod('Açaí com Banana Batido', 'Bebidas', 25.00)
    add_prod('Suco de Açaí com Laranja', 'Bebidas', 23.00)
    add_prod('Adicional Proteína Whey 28g', 'Bebidas', 29.00)

    # Cafés Gelados
    add_prod('Coconut Cream Coffee', 'Cafés Gelados', 23.00)
    add_prod('Café Latte Gelado', 'Cafés Gelados', 19.00)
    add_prod('Café com Leite Proteico', 'Cafés Gelados', None)
    add_prod('Adicional Proteína Syntha 30g', 'Cafés Gelados', 42.00)

    # Bowls
    add_prod('Salada de Frutas', 'Bowls', 31.00)
    add_prod('Salada de Frutas com Granola', 'Bowls', 40.00)
    add_prod('Granola Iogurte e Mel', 'Bowls', 46.00)
    add_prod('Iogurte com Granola', 'Bowls', 36.00)
    add_prod('Mini Pote de Mel', 'Bowls', 9.00)
    add_prod('Açaí na Tigela', 'Bowls', 45.00)
    add_prod('Adicional de Banana', 'Bowls', 6.00)
    add_prod('Adicional de Morangos', 'Bowls', 19.00)
    add_prod('Leite em Pó (adicional)', 'Bowls', 8.00)

    # Saladas Orgânicas
    add_prod('Salada Mix de Folhas com Pesto', 'Saladas Orgânicas', 43.00,
             'Mix de Folhas, Tomate Cereja, Muçarela de Búfala, Nozes, Molho Pesto')
    add_prod('Salada com Lagarto e Sourdough', 'Saladas Orgânicas', 52.00,
             'Lagarto Cozido Desfiado + 2 Fatias de Sourdough Tradicional')

    db.session.flush()

    # ── 5. Lanches (com composição) ──
    add_prod_comp('Queijo Quente no Sourdough/Francês', 'Lanches', 27.00, [
        ('receita', 'Sourdough Tradicional', 0.15),
        ('mp', 'Queijo Prato', 2),
    ], 'Queijo Prato ou Branco')

    add_prod_comp('Queijo Quente no Brioche/Croissant', 'Lanches', 30.00, [
        ('receita', 'Brioche', 0.25),
        ('mp', 'Queijo Prato', 2),
    ], 'Queijo Prato ou Branco')

    add_prod_comp('Misto no Sourdough/Francês', 'Lanches', 27.00, [
        ('receita', 'Sourdough Tradicional', 0.15),
        ('mp', 'Queijo Prato', 2),
        ('mp', 'Presunto', 2),
    ], 'Com Queijo Prato ou Branco')

    add_prod_comp('Misto no Brioche/Croissant', 'Lanches', 30.00, [
        ('receita', 'Brioche', 0.25),
        ('mp', 'Queijo Prato', 2),
        ('mp', 'Presunto', 2),
    ], 'Com Queijo Prato ou Branco')

    add_prod_comp('Peito de Peru no Sourdough/Francês', 'Lanches', 35.00, [
        ('receita', 'Sourdough Tradicional', 0.15),
        ('mp', 'Queijo Prato', 2),
        ('mp', 'Peito de Peru', 2),
    ])

    add_prod_comp('Peito de Peru no Brioche/Croissant', 'Lanches', 39.00, [
        ('receita', 'Brioche', 0.25),
        ('mp', 'Queijo Prato', 2),
        ('mp', 'Peito de Peru', 2),
    ])

    add_prod('Queijo Branco no Prato (2 Fatias)', 'Lanches', 16.00)
    add_prod('Cone de Pão de Queijo (10un)', 'Lanches', 14.00)

    # ── 6. Pães na Chapa (com composição) ──
    add_prod_comp('Sourdough com Manteiga (2 fatias)', 'Pães na Chapa', 11.00, [
        ('receita', 'Sourdough Tradicional', 0.15),
        ('mp', 'Manteiga', 1),
    ])

    add_prod_comp('Sourdough com Manteiga e Requeijão', 'Pães na Chapa', 16.00, [
        ('receita', 'Sourdough Tradicional', 0.15),
        ('mp', 'Manteiga', 1),
        ('mp', 'Requeijão', 1),
    ])

    add_prod_comp('Brioche na Chapa (3 fatias)', 'Pães na Chapa', 16.00, [
        ('receita', 'Brioche', 0.25),
        ('mp', 'Manteiga', 1),
    ])

    add_prod_comp('Brioche com Manteiga e Requeijão', 'Pães na Chapa', 17.00, [
        ('receita', 'Brioche', 0.25),
        ('mp', 'Manteiga', 1),
        ('mp', 'Requeijão', 1),
    ])

    add_prod_comp('Pão Francês com Manteiga (2 fatias)', 'Pães na Chapa', 11.00, [
        ('receita', 'Pão Francês Fermentado', 2),
        ('mp', 'Manteiga', 1),
    ])

    add_prod_comp('Pão Francês com Manteiga e Requeijão', 'Pães na Chapa', 16.00, [
        ('receita', 'Pão Francês Fermentado', 2),
        ('mp', 'Manteiga', 1),
        ('mp', 'Requeijão', 1),
    ])

    add_prod_comp('Croissant Francês na Chapa', 'Pães na Chapa', 19.00, [
        ('receita', 'Croissant Tradicional', 1),
        ('mp', 'Manteiga', 1),
    ])

    add_prod_comp('Croissant Francês com Manteiga e Requeijão', 'Pães na Chapa', 21.00, [
        ('receita', 'Croissant Tradicional', 1),
        ('mp', 'Manteiga', 1),
        ('mp', 'Requeijão', 1),
    ])

    add_prod('Tablet 10g Manteiga President', 'Pães na Chapa', 4.00)
    add_prod('Adicional de Requeijão', 'Pães na Chapa', 6.00)

    add_prod_comp('Ovos Orgânicos Mexidos (3 ovos)', 'Pães na Chapa', 17.00, [
        ('mp', 'Ovos', 3),
        ('mp', 'Manteiga', 1),
    ])

    db.session.commit()
