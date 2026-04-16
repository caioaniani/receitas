from datetime import date

from app.extensions import db
from app.models import MateriaPrima, Receita, ReceitaIngrediente, Produto, ProdutoItem, Loja, Funcionario, Posicao


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

    # ── 2. Novas matérias-primas com custos reais ──
    novas_mps = [
        # Frios e proteínas
        ('Queijo Prato', 'g', 33.50),
        ('Queijo Branco', 'g', 28.00),
        ('Presunto', 'g', 27.09),
        ('Peito de Peru', 'g', 58.89),
        ('Requeijão', 'g', 39.75),          # R$15.90/400g
        ('Calabresa', 'g', 23.00),
        ('Lagarto Cozido', 'g', 49.90),
        ('Muçarela de Búfala', 'g', 50.31),
        ('Parmesão', 'g', 39.78),
        ('Gorgonzola', 'g', 61.60),
        # Frutas e vegetais
        ('Laranja', 'un', 0.61),            # R$55/18kg ~90un
        ('Abacaxi', 'g', 4.33),             # R$6.50/un ~1.5kg
        ('Açaí (polpa)', 'g', 15.10),       # R$151/10L
        ('Banana', 'g', 0),
        ('Tomate Cereja', 'g', 10.00),      # R$3.00/bandeja ~300g
        ('Couve', 'g', 17.50),              # R$3.50/maço ~200g
        ('Gengibre', 'g', 75.00),           # R$15/pct ~200g
        ('Hortelã', 'g', 160.00),           # R$8/maço ~50g
        ('Folhas/Alface', 'g', 25.00),      # R$5/maço ~200g
        ('Nozes', 'g', 50.96),
        # Laticínios e outros
        ('Iogurte', 'g', 0),
        ('Leite Vegetal', 'ml', 18.37),
        ('Leite Condensado', 'g', 24.71),   # R$64.24/2.6kg
        ('Leite em Pó', 'g', 44.34),
        ('Molho Pesto', 'g', 0),
        ('Molho Branco', 'g', 0),
        ('Canela', 'g', 19.30),             # R$9.65/500g
        ('Farinha de Amendoim', 'g', 0),
        ('Girassol', 'g', 13.65),
        ('Chocolate 823 Callebaut', 'g', 147.62),  # R$310/2.1kg
        ('Chocolate do Frade', 'g', 73.06),
        ('Toddy', 'g', 26.06),             # R$46.90/1.8kg
        ('Gelo', 'g', 1.40),               # R$7/5kg
        # Doses/unitários
        ('Dose Espresso', 'un', 1.54),
        ('Dose Café Coado', 'un', 4.35),
        ('Chá Twinings (sachê)', 'un', 1.90),  # R$18.99/~10un
        ('Pão de Queijo (congelado)', 'g', 25.90),  # R$51.80/2kg
        ('Água Mineral 300ml', 'un', 2.95),
        ('Coca-Cola', 'un', 2.85),
        ('Guaraná', 'un', 3.61),
        ('Água de Coco', 'un', 5.00),
        ('Proteína Whey 28g', 'un', 12.00),
        ('Proteína Syntha 30g', 'un', 12.00),
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

    # ── 4. Produtos do cardápio ──
    def add_prod(nome, cat, preco_loja, descricao='', custo_direto=None):
        if nome in existentes_prod:
            return None
        p = Produto(nome=nome, categoria=cat, preco_loja=preco_loja,
                    descricao=descricao, custo_direto=custo_direto)
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

    # ── Cafés e Bebidas Quentes (custo_direto dos insumos) ──
    add_prod('Café Espresso', 'Cafés e Bebidas Quentes', 12.00, custo_direto=1.54)
    add_prod('Café Espresso com Leite PQ', 'Cafés e Bebidas Quentes', 13.00, custo_direto=1.94)
    add_prod('Café Espresso Leite Médio', 'Cafés e Bebidas Quentes', 13.00, custo_direto=2.14)
    add_prod('Café Espresso Leite Vegetal', 'Cafés e Bebidas Quentes', 23.00, custo_direto=2.74)
    add_prod('Café Espresso Duplo', 'Cafés e Bebidas Quentes', 19.00, custo_direto=2.31)
    add_prod('Café Especial Coado', 'Cafés e Bebidas Quentes', 15.00, custo_direto=4.35)
    add_prod('Café Especial Coado com Leite', 'Cafés e Bebidas Quentes', 16.00, custo_direto=5.55)
    add_prod('Cappuccino com Chocolate Belga', 'Cafés e Bebidas Quentes', 27.00, custo_direto=9.61)
    add_prod('Chá Twinings', 'Cafés e Bebidas Quentes', 8.00, 'Erva-Doce, Camomila ou Verde', custo_direto=1.90)
    add_prod('Chocolate Quente', 'Cafés e Bebidas Quentes', 15.00, 'Padre ou Toddy', custo_direto=2.42)
    add_prod('Chá de Amendoim', 'Cafés e Bebidas Quentes', 19.00, custo_direto=3.52)
    add_prod('Copo de Leite', 'Cafés e Bebidas Quentes', 9.00, custo_direto=1.80)
    add_prod('Copo de Leite Vegetal', 'Cafés e Bebidas Quentes', 18.00, custo_direto=4.20)

    # ── Bebidas ──
    add_prod('Suco de Laranja Natural', 'Bebidas', 18.00, custo_direto=3.44)
    add_prod('Suco de Laranja 1 Litro', 'Bebidas', 48.00, custo_direto=11.69)
    add_prod('Suco Verde', 'Bebidas', 28.00, 'Laranja, Abacaxi, Couve e Gengibre', custo_direto=6.17)
    add_prod('Suco de Abacaxi com Hortelã', 'Bebidas', 28.00, custo_direto=2.65)
    add_prod('Água Sem Gás 300ml', 'Bebidas', 9.00, custo_direto=2.95)
    add_prod('Água São Lourenço com Gás 300ml', 'Bebidas', 9.00, custo_direto=3.15)
    add_prod('Coca Zero ou Normal', 'Bebidas', 10.00, custo_direto=2.85)
    add_prod('Guaraná Zero ou Normal', 'Bebidas', 10.00, custo_direto=3.61)
    add_prod('Chocolate Frio', 'Bebidas', 18.00, 'Toddy ou Padre', custo_direto=4.67)
    add_prod('Água de Coco (no Coco)', 'Bebidas', 18.00, custo_direto=5.00)
    add_prod('Suco de Açaí', 'Bebidas', 19.00, custo_direto=3.84)
    add_prod('Açaí com Banana Batido', 'Bebidas', 25.00)
    add_prod('Suco de Açaí com Laranja', 'Bebidas', 23.00, custo_direto=7.21)
    add_prod('Adicional Proteína Whey 28g', 'Bebidas', 29.00, custo_direto=12.00)

    # ── Cafés Gelados ──
    add_prod('Coconut Cream Coffee', 'Cafés Gelados', 23.00, custo_direto=6.05)
    add_prod('Café Latte Gelado', 'Cafés Gelados', 19.00, custo_direto=3.26)
    add_prod('Café com Leite Proteico', 'Cafés Gelados', None, custo_direto=14.44)
    add_prod('Adicional Proteína Syntha 30g', 'Cafés Gelados', 42.00, custo_direto=12.00)

    # ── Bowls ──
    add_prod('Salada de Frutas', 'Bowls', 31.00, custo_direto=6.50)
    add_prod('Salada de Frutas com Granola', 'Bowls', 40.00, custo_direto=8.60)
    add_prod('Granola Iogurte e Mel', 'Bowls', 46.00, custo_direto=8.85)
    add_prod('Iogurte com Granola', 'Bowls', 36.00, custo_direto=11.90)
    add_prod('Mini Pote de Mel', 'Bowls', 9.00, custo_direto=5.00)
    add_prod('Açaí na Tigela', 'Bowls', 45.00, custo_direto=7.55)
    add_prod('Adicional de Banana', 'Bowls', 6.00)
    add_prod('Adicional de Morangos', 'Bowls', 19.00, custo_direto=2.50)
    add_prod('Leite em Pó (adicional)', 'Bowls', 8.00, custo_direto=2.25)

    # ── Saladas Orgânicas ──
    add_prod('Salada Mix de Folhas com Pesto', 'Saladas Orgânicas', 43.00,
             'Mix de Folhas, Tomate Cereja, Muçarela de Búfala, Nozes, Molho Pesto',
             custo_direto=10.91)
    add_prod('Salada com Lagarto e Sourdough', 'Saladas Orgânicas', 52.00,
             'Lagarto Cozido Desfiado + 2 Fatias de Sourdough Tradicional',
             custo_direto=19.24)

    db.session.flush()

    # ── 5. Lanches (composição com quantidades reais em gramas) ──
    # Nota: para MPs em 'g', quantidade = gramas. Custo = (custo_por_kg/1000) * qtd_g
    add_prod_comp('Queijo Quente no Sourdough/Francês', 'Lanches', 27.00, [
        ('receita', 'Pão Francês Fermentado', 1),
        ('mp', 'Queijo Prato', 60),
        ('mp', 'Queijo Branco', 60),
    ], 'Queijo Prato + Queijo Branco')

    add_prod_comp('Queijo Quente no Brioche/Croissant', 'Lanches', 30.00, [
        ('receita', 'Croissant Tradicional', 1),
        ('mp', 'Queijo Prato', 60),
        ('mp', 'Queijo Branco', 60),
    ], 'Queijo Prato + Queijo Branco')

    add_prod_comp('Misto no Sourdough/Francês', 'Lanches', 27.00, [
        ('receita', 'Pão Francês Fermentado', 1),
        ('mp', 'Presunto', 40),
        ('mp', 'Queijo Prato', 40),
    ])

    add_prod_comp('Misto no Brioche/Croissant', 'Lanches', 30.00, [
        ('receita', 'Croissant Tradicional', 1),
        ('mp', 'Presunto', 40),
        ('mp', 'Queijo Prato', 40),
    ])

    add_prod_comp('Peito de Peru no Sourdough/Francês', 'Lanches', 35.00, [
        ('receita', 'Pão Francês Fermentado', 1),
        ('mp', 'Peito de Peru', 40),
        ('mp', 'Queijo Prato', 40),
    ])

    add_prod_comp('Peito de Peru no Brioche/Croissant', 'Lanches', 39.00, [
        ('receita', 'Croissant Tradicional', 1),
        ('mp', 'Peito de Peru', 40),
        ('mp', 'Queijo Prato', 40),
    ])

    add_prod('Queijo Branco no Prato (2 Fatias)', 'Lanches', 16.00, custo_direto=3.36)

    add_prod_comp('Cone de Pão de Queijo (10un)', 'Lanches', 14.00, [
        ('mp', 'Pão de Queijo (congelado)', 200),  # ~200g para 10un
    ])

    # ── 6. Pães na Chapa (composição com quantidades em gramas) ──
    add_prod_comp('Sourdough com Manteiga (2 fatias)', 'Pães na Chapa', 11.00, [
        ('receita', 'Sourdough Tradicional', 0.2),  # ~2 fatias = 1/5 do pão
        ('mp', 'Manteiga', 20),                      # 20g manteiga
    ])

    add_prod_comp('Sourdough com Manteiga e Requeijão', 'Pães na Chapa', 16.00, [
        ('receita', 'Sourdough Tradicional', 0.2),
        ('mp', 'Manteiga', 20),
        ('mp', 'Requeijão', 50),
    ])

    add_prod_comp('Brioche na Chapa (3 fatias)', 'Pães na Chapa', 16.00, [
        ('receita', 'Brioche', 0.25),
        ('mp', 'Manteiga', 20),
    ])

    add_prod_comp('Brioche com Manteiga e Requeijão', 'Pães na Chapa', 17.00, [
        ('receita', 'Brioche', 0.25),
        ('mp', 'Manteiga', 20),
        ('mp', 'Requeijão', 50),
    ])

    add_prod_comp('Pão Francês com Manteiga (2 fatias)', 'Pães na Chapa', 11.00, [
        ('receita', 'Pão Francês Fermentado', 2),
        ('mp', 'Manteiga', 20),
    ])

    add_prod_comp('Pão Francês com Manteiga e Requeijão', 'Pães na Chapa', 16.00, [
        ('receita', 'Pão Francês Fermentado', 2),
        ('mp', 'Manteiga', 20),
        ('mp', 'Requeijão', 50),
    ])

    add_prod_comp('Croissant Francês na Chapa', 'Pães na Chapa', 19.00, [
        ('receita', 'Croissant Tradicional', 1),
        ('mp', 'Manteiga', 20),
    ])

    add_prod_comp('Croissant Francês com Manteiga e Requeijão', 'Pães na Chapa', 21.00, [
        ('receita', 'Croissant Tradicional', 1),
        ('mp', 'Manteiga', 20),
        ('mp', 'Requeijão', 50),
    ])

    add_prod('Tablet 10g Manteiga President', 'Pães na Chapa', 4.00, custo_direto=1.00)
    add_prod('Adicional de Requeijão', 'Pães na Chapa', 6.00, custo_direto=1.00)

    add_prod_comp('Ovos Orgânicos Mexidos (3 ovos)', 'Pães na Chapa', 17.00, [
        ('mp', 'Ovos', 180),     # 3 ovos ~180g
        ('mp', 'Manteiga', 10),  # para cozinhar
    ])

    # ── 7. Pão de Queijo ──
    add_prod_comp('Cone de Pão de Queijo (5un)', 'Lanches', 7.00, [
        ('mp', 'Pão de Queijo (congelado)', 100),  # ~100g para 5un
    ])

    db.session.commit()


def seed_update_v2():
    """Atualiza custos e composições com dados reais (para bancos que já rodaram o seed antigo)."""
    # Marca: se 'Dose Espresso' já existe, este update já foi aplicado
    if MateriaPrima.query.filter_by(nome='Dose Espresso').first() is not None:
        return

    # ── 1. Atualizar custos de MPs que estavam em 0 ──
    custos_reais = {
        'Queijo Prato': ('g', 33.50),
        'Queijo Branco': ('g', 28.00),
        'Presunto': ('g', 27.09),
        'Peito de Peru': ('g', 58.89),
        'Requeijão': ('g', 39.75),
        'Leite Vegetal': ('ml', 18.37),
        'Chá Twinings (sachê)': ('un', 1.90),
        'Abacaxi': ('g', 4.33),
        'Açaí (polpa)': ('g', 15.10),
        'Tomate Cereja': ('g', 10.00),
        'Folhas/Alface': ('g', 25.00),
        'Muçarela de Búfala': ('g', 50.31),
        'Lagarto Cozido': ('g', 49.90),
        'Leite em Pó': ('g', 44.34),
        'Água Mineral 300ml': ('un', 2.95),
        'Coca-Cola': ('un', 2.85),
        'Guaraná': ('un', 3.61),
        'Água de Coco': ('un', 5.00),
        'Proteína Whey 28g': ('un', 12.00),
        'Proteína Syntha 30g': ('un', 12.00),
    }
    for mp in MateriaPrima.query.all():
        if mp.nome in custos_reais and mp.custo_por_kg == 0:
            unidade, custo = custos_reais[mp.nome]
            mp.custo_por_kg = custo
            mp.unidade = unidade

    # Laranja: mudar de 'g' para 'un'
    laranja = MateriaPrima.query.filter_by(nome='Laranja').first()
    if laranja and laranja.custo_por_kg == 0:
        laranja.unidade = 'un'
        laranja.custo_por_kg = 0.61

    # Toddy/Padre → renomear para Toddy se existir com custo 0
    toddy_mp = MateriaPrima.query.filter_by(nome='Toddy/Padre').first()
    if toddy_mp and toddy_mp.custo_por_kg == 0:
        toddy_mp.nome = 'Toddy'
        toddy_mp.custo_por_kg = 26.06

    db.session.flush()

    # ── 2. Adicionar MPs novas ──
    existentes = {mp.nome for mp in MateriaPrima.query.all()}
    novas = [
        ('Dose Espresso', 'un', 1.54),
        ('Dose Café Coado', 'un', 4.35),
        ('Calabresa', 'g', 23.00),
        ('Canela', 'g', 19.30),
        ('Leite Condensado', 'g', 24.71),
        ('Farinha de Amendoim', 'g', 0),
        ('Gorgonzola', 'g', 61.60),
        ('Parmesão', 'g', 39.78),
        ('Pão de Queijo (congelado)', 'g', 25.90),
        ('Chocolate 823 Callebaut', 'g', 147.62),
        ('Chocolate do Frade', 'g', 73.06),
        ('Nozes', 'g', 50.96),
        ('Molho Branco', 'g', 0),
        ('Girassol', 'g', 13.65),
        ('Couve', 'g', 17.50),
        ('Gengibre', 'g', 75.00),
        ('Hortelã', 'g', 160.00),
        ('Gelo', 'g', 1.40),
        ('Toddy', 'g', 26.06),
    ]
    for nome, unidade, custo in novas:
        if nome not in existentes:
            db.session.add(MateriaPrima(nome=nome, unidade=unidade, custo_por_kg=custo))
    db.session.flush()

    # ── 3. Atualizar composições existentes com quantidades reais ──
    composicoes = {
        'Queijo Quente no Sourdough/Francês': [
            ('receita', 'Pão Francês Fermentado', 1),
            ('mp', 'Queijo Prato', 60), ('mp', 'Queijo Branco', 60),
        ],
        'Queijo Quente no Brioche/Croissant': [
            ('receita', 'Croissant Tradicional', 1),
            ('mp', 'Queijo Prato', 60), ('mp', 'Queijo Branco', 60),
        ],
        'Misto no Sourdough/Francês': [
            ('receita', 'Pão Francês Fermentado', 1),
            ('mp', 'Presunto', 40), ('mp', 'Queijo Prato', 40),
        ],
        'Misto no Brioche/Croissant': [
            ('receita', 'Croissant Tradicional', 1),
            ('mp', 'Presunto', 40), ('mp', 'Queijo Prato', 40),
        ],
        'Peito de Peru no Sourdough/Francês': [
            ('receita', 'Pão Francês Fermentado', 1),
            ('mp', 'Peito de Peru', 40), ('mp', 'Queijo Prato', 40),
        ],
        'Peito de Peru no Brioche/Croissant': [
            ('receita', 'Croissant Tradicional', 1),
            ('mp', 'Peito de Peru', 40), ('mp', 'Queijo Prato', 40),
        ],
        'Sourdough com Manteiga (2 fatias)': [
            ('receita', 'Sourdough Tradicional', 0.2), ('mp', 'Manteiga', 20),
        ],
        'Sourdough com Manteiga e Requeijão': [
            ('receita', 'Sourdough Tradicional', 0.2),
            ('mp', 'Manteiga', 20), ('mp', 'Requeijão', 50),
        ],
        'Brioche na Chapa (3 fatias)': [
            ('receita', 'Brioche', 0.25), ('mp', 'Manteiga', 20),
        ],
        'Brioche com Manteiga e Requeijão': [
            ('receita', 'Brioche', 0.25),
            ('mp', 'Manteiga', 20), ('mp', 'Requeijão', 50),
        ],
        'Pão Francês com Manteiga (2 fatias)': [
            ('receita', 'Pão Francês Fermentado', 2), ('mp', 'Manteiga', 20),
        ],
        'Pão Francês com Manteiga e Requeijão': [
            ('receita', 'Pão Francês Fermentado', 2),
            ('mp', 'Manteiga', 20), ('mp', 'Requeijão', 50),
        ],
        'Croissant Francês na Chapa': [
            ('receita', 'Croissant Tradicional', 1), ('mp', 'Manteiga', 20),
        ],
        'Croissant Francês com Manteiga e Requeijão': [
            ('receita', 'Croissant Tradicional', 1),
            ('mp', 'Manteiga', 20), ('mp', 'Requeijão', 50),
        ],
        'Ovos Orgânicos Mexidos (3 ovos)': [
            ('mp', 'Ovos', 180), ('mp', 'Manteiga', 10),
        ],
    }

    for nome, itens in composicoes.items():
        p = Produto.query.filter_by(nome=nome).first()
        if not p:
            continue
        ProdutoItem.query.filter_by(produto_id=p.id).delete()
        for tipo, item_nome, qtd in itens:
            db.session.add(ProdutoItem(
                produto_id=p.id, tipo=tipo, item_nome=item_nome, quantidade=qtd))

    # ── 4. Setar custo_direto nos produtos simples ──
    custos_diretos = {
        'Café Espresso': 1.54,
        'Café Espresso com Leite PQ': 1.94,
        'Café Espresso Leite Médio': 2.14,
        'Café Espresso Leite Vegetal': 2.74,
        'Café Espresso Duplo': 2.31,
        'Café Especial Coado': 4.35,
        'Café Especial Coado com Leite': 5.55,
        'Cappuccino com Chocolate Belga': 9.61,
        'Chá Twinings': 1.90,
        'Chocolate Quente': 2.42,
        'Chá de Amendoim': 3.52,
        'Copo de Leite': 1.80,
        'Copo de Leite Vegetal': 4.20,
        'Suco de Laranja Natural': 3.44,
        'Suco de Laranja 1 Litro': 11.69,
        'Suco Verde': 6.17,
        'Suco de Abacaxi com Hortelã': 2.65,
        'Água Sem Gás 300ml': 2.95,
        'Água São Lourenço com Gás 300ml': 3.15,
        'Coca Zero ou Normal': 2.85,
        'Guaraná Zero ou Normal': 3.61,
        'Chocolate Frio': 4.67,
        'Água de Coco (no Coco)': 5.00,
        'Suco de Açaí': 3.84,
        'Suco de Açaí com Laranja': 7.21,
        'Adicional Proteína Whey 28g': 12.00,
        'Coconut Cream Coffee': 6.05,
        'Café Latte Gelado': 3.26,
        'Café com Leite Proteico': 14.44,
        'Adicional Proteína Syntha 30g': 12.00,
        'Salada de Frutas': 6.50,
        'Salada de Frutas com Granola': 8.60,
        'Granola Iogurte e Mel': 8.85,
        'Iogurte com Granola': 11.90,
        'Mini Pote de Mel': 5.00,
        'Açaí na Tigela': 7.55,
        'Adicional de Morangos': 2.50,
        'Leite em Pó (adicional)': 2.25,
        'Queijo Branco no Prato (2 Fatias)': 3.36,
        'Tablet 10g Manteiga President': 1.00,
        'Adicional de Requeijão': 1.00,
        'Salada Mix de Folhas com Pesto': 10.91,
        'Salada com Lagarto e Sourdough': 19.24,
    }
    for p in Produto.query.all():
        if p.nome in custos_diretos and not p.custo_direto:
            p.custo_direto = custos_diretos[p.nome]

    db.session.commit()


def seed_site_products():
    """Adiciona produtos do site com preco_site e composição das cestas.
    Roda em TODOS os ambientes (SQLite local + PostgreSQL produção).
    """
    # Idempotência: se Family Box já existe, já rodou
    if Produto.query.filter_by(nome='Family Box').first():
        return

    # ── 1. Renomear Granola Artesanal → Granola Artesanal 1Kg ──
    granola = Receita.query.filter_by(nome='Granola Artesanal').first()
    if granola:
        granola.nome = 'Granola Artesanal 1Kg'
        # Atualizar referências em composições
        for pi in ProdutoItem.query.filter_by(item_nome='Granola Artesanal', tipo='receita').all():
            pi.item_nome = 'Granola Artesanal 1Kg'
        for ri in ReceitaIngrediente.query.filter_by(ingrediente_nome='Granola Artesanal', tipo='receita').all():
            ri.ingrediente_nome = 'Granola Artesanal 1Kg'
    db.session.flush()

    # ── 2. Atualizar preco_site nas receitas existentes ──
    precos_site_receitas = {
        'Croissant Tradicional': 22.50,
        'Pain au Chocolat': 27.50,
        'Croissant Nutella com Morango': 32.50,
        'Croissant Almond': 32.50,
        'Croissant Nutella': 30.50,
        'Cinnamon Roll': 26.00,
        'Sourdough Tradicional': 33.50,
        'Sourdough Integral': 32.50,
        'Sourdough 7 Grãos': 39.00,
        'Sourdough Nozes e Azeitonas': 39.00,
        'Brioche': 45.00,
        'Pão Francês Fermentado': 3.50,
        'Cookie Calebaut': 13.00,
    }
    for r in Receita.query.all():
        if r.nome in precos_site_receitas:
            r.preco_site = precos_site_receitas[r.nome]
    db.session.flush()

    # ── 3. Novas matérias-primas (itens de cesta / embalagem) ──
    existentes_mp = {mp.nome for mp in MateriaPrima.query.all()}
    novas_mps = [
        # Itens unitários para composição de cestas
        ('Sachê Café Orfeu', 'un', 0),
        ('Geleia Artesanal de Morango', 'un', 0),
        ('Pote de Mel 40g', 'un', 0),
        ('Mini Manteiga President', 'un', 0),
        ('Suco de Uva Villa Piva 300ml', 'un', 0),
        ('Suco de Tangerina Villa Piva 300ml', 'un', 0),
        ('Arranjo de Flor', 'un', 0),
        # Porções para cestas
        ('Salada de Frutas 600g', 'un', 0),
        ('Salada de Frutas 100g', 'un', 0),
        ('Iogurte Artesanal 600ml', 'un', 0),
        ('Iogurte Artesanal 200ml', 'un', 0),
        ('Granola Artesanal 500g', 'un', 0),
        ('Granola Artesanal 100g', 'un', 0),
        # Embalagens
        ('Caixa MDF', 'un', 0),
        ('Caixa Madeira MDF', 'un', 0),
        ('Base Coração MDF', 'un', 0),
        ('Base Redonda MDF', 'un', 0),
        ('Base Quadrada MDF', 'un', 0),
        ('Lancheira', 'un', 0),
        ('Bandeja com Suporte', 'un', 0),
        # Frios (por kg, para porções em gramas nas cestas)
        ('Mussarela', 'g', 0),
    ]
    for nome, unidade, custo in novas_mps:
        if nome not in existentes_mp:
            db.session.add(MateriaPrima(nome=nome, unidade=unidade, custo_por_kg=custo))
    db.session.flush()

    # ── 4. Produtos avulsos vendidos no site ──
    existentes_prod = {p.nome for p in Produto.query.all()}

    def add_prod_site(nome, cat, preco_site, descricao=''):
        if nome in existentes_prod:
            return
        db.session.add(Produto(nome=nome, categoria=cat, preco_site=preco_site, descricao=descricao))
        existentes_prod.add(nome)

    add_prod_site('Granola Artesanal 500g', 'Acompanhamentos', 49.00, 'Granola artesanal sem açúcar adicionado')
    add_prod_site('Granola Artesanal 100g', 'Acompanhamentos', 19.00)
    add_prod_site('Iogurte Artesanal 600ml', 'Acompanhamentos', 33.50, 'Iogurte natural artesanal cremoso — tamanho família')
    add_prod_site('Iogurte Artesanal 200ml', 'Acompanhamentos', 20.00)
    add_prod_site('Arranjo de Flor', 'Acompanhamentos', 32.00, 'Disponível de acordo com estoque')
    add_prod_site('Suco de Tangerina Villa Piva 300ml', 'Acompanhamentos', 27.00, '100% Natural')
    add_prod_site('Suco de Uva Villa Piva 300ml', 'Acompanhamentos', 27.00, 'Tinto Integral 100% Natural')
    add_prod_site('Peito de Peru 100g', 'Acompanhamentos', 21.00, 'Fatiado, porção de 100g')
    add_prod_site('Geleia de Morango Artesanal', 'Acompanhamentos', 18.00, 'Feita com frutas selecionadas')
    add_prod_site('Salada de Frutas 100g', 'Acompanhamentos', 17.50, 'Frutas frescas em pote de 100g')
    add_prod_site('Mussarela 100g', 'Acompanhamentos', 17.00, 'Fatiada, porção de 100g')
    add_prod_site('Mel 40g', 'Acompanhamentos', 11.00, 'Mel puro em pote de 40g')
    add_prod_site('3 Mini Manteigas President', 'Acompanhamentos', 8.00, 'Porções individuais de manteiga President')
    add_prod_site('Sachê Café Orfeu', 'Acompanhamentos', 8.00, 'Sachê individual de café premium Orfeu')

    db.session.flush()

    # ── 5. Cestas com composição ──
    def add_cesta(nome, preco_site, itens, descricao='', categoria='Cestas'):
        if nome in existentes_prod:
            return
        p = Produto(nome=nome, categoria=categoria, preco_site=preco_site, descricao=descricao)
        db.session.add(p)
        db.session.flush()
        for tipo, item_nome, qtd in itens:
            db.session.add(ProdutoItem(
                produto_id=p.id, tipo=tipo, item_nome=item_nome, quantidade=qtd))
        existentes_prod.add(nome)

    add_cesta('Family Box', 437.00, [
        ('mp', 'Sachê Café Orfeu', 2),
        ('mp', 'Mussarela', 100),
        ('mp', 'Peito de Peru', 100),
        ('receita', 'Cookie Calebaut', 2),
        ('mp', 'Geleia Artesanal de Morango', 1),
        ('mp', 'Pote de Mel 40g', 2),
        ('mp', 'Mini Manteiga President', 3),
        ('receita', 'Pain au Chocolat', 2),
        ('receita', 'Croissant Tradicional', 2),
        ('receita', 'Sourdough 7 Grãos', 1),
        ('receita', 'Sourdough Tradicional', 1),
        ('receita', 'Croissant Nutella com Morango', 1),
        ('receita', 'Croissant Almond', 1),
        ('receita', 'Brioche', 1),
        ('mp', 'Salada de Frutas 600g', 1),
        ('mp', 'Iogurte Artesanal 600ml', 1),
        ('mp', 'Granola Artesanal 500g', 1),
        ('mp', 'Arranjo de Flor', 1),
    ], 'A mais completa! Seleção generosa para toda a família.')

    add_cesta('Bandeja de Café da Manhã', 401.00, [
        ('mp', 'Suco de Uva Villa Piva 300ml', 1),
        ('mp', 'Salada de Frutas 100g', 2),
        ('mp', 'Iogurte Artesanal 200ml', 1),
        ('mp', 'Granola Artesanal 100g', 1),
        ('mp', 'Mini Manteiga President', 2),
        ('mp', 'Pote de Mel 40g', 1),
        ('mp', 'Peito de Peru', 50),
        ('mp', 'Mussarela', 50),
        ('receita', 'Sourdough Tradicional', 1),
        ('receita', 'Croissant Tradicional', 1),
        ('receita', 'Pain au Chocolat', 1),
        ('receita', 'Croissant Nutella', 1),
        ('mp', 'Arranjo de Flor', 1),
        ('mp', 'Bandeja com Suporte', 1),
    ], 'Bandeja completa com suporte para apoio na cama.')

    add_cesta('Caixa Especial', 368.00, [
        ('mp', 'Caixa Madeira MDF', 1),
        ('receita', 'Croissant Tradicional', 2),
        ('receita', 'Pain au Chocolat', 2),
        ('receita', 'Croissant Almond', 2),
        ('receita', 'Croissant Nutella', 2),
        ('receita', 'Croissant Nutella com Morango', 2),
        ('mp', 'Granola Artesanal 100g', 2),
        ('mp', 'Iogurte Artesanal 200ml', 2),
        ('receita', 'Sourdough Nozes e Azeitonas', 1),
        ('receita', 'Sourdough Tradicional', 1),
        ('receita', 'Sourdough 7 Grãos', 1),
    ], 'Para ocasiões memoráveis! Produtos artesanais selecionados.')

    add_cesta('Cesta Monamour', 313.00, [
        ('mp', 'Base Coração MDF', 1),
        ('mp', 'Arranjo de Flor', 1),
        ('receita', 'Sourdough Nozes e Azeitonas', 1),
        ('receita', 'Croissant Almond', 1),
        ('receita', 'Croissant Tradicional', 1),
        ('receita', 'Pain au Chocolat', 1),
        ('receita', 'Cookie Calebaut', 2),
        ('mp', 'Iogurte Artesanal 200ml', 1),
        ('mp', 'Pote de Mel 40g', 1),
        ('mp', 'Geleia Artesanal de Morango', 1),
        ('mp', 'Granola Artesanal 100g', 1),
        ('mp', 'Salada de Frutas 100g', 1),
        ('mp', 'Suco de Uva Villa Piva 300ml', 1),
    ], 'Para momentos românticos! Cesta especial para dois.')

    add_cesta('Sweet Coffee', 236.00, [
        ('receita', 'Brioche', 1),
        ('receita', 'Croissant Tradicional', 1),
        ('receita', 'Croissant Nutella com Morango', 1),
        ('mp', 'Salada de Frutas 100g', 1),
        ('mp', 'Granola Artesanal 100g', 1),
        ('receita', 'Cookie Calebaut', 2),
        ('mp', 'Suco de Uva Villa Piva 300ml', 1),
        ('mp', 'Caixa MDF', 1),
    ], 'Para amantes de doces! Café aromático e delícias açucaradas.')

    add_cesta('Bonjour', 215.00, [
        ('receita', 'Sourdough 7 Grãos', 1),
        ('receita', 'Croissant Tradicional', 1),
        ('receita', 'Pain au Chocolat', 1),
        ('receita', 'Croissant Almond', 1),
        ('mp', 'Salada de Frutas 100g', 1),
        ('mp', 'Granola Artesanal 100g', 1),
        ('mp', 'Pote de Mel 40g', 1),
        ('mp', 'Caixa MDF', 1),
    ], 'Mimo francês! Cesta para 1 pessoa.')

    add_cesta('Abraço em Forma de Pães', 169.00, [
        ('mp', 'Base Redonda MDF', 1),
        ('receita', 'Sourdough Tradicional', 1),
        ('receita', 'Sourdough Nozes e Azeitonas', 1),
        ('receita', 'Croissant Tradicional', 2),
        ('receita', 'Pain au Chocolat', 2),
    ], 'Um presente caloroso para abraçar de longe quem você ama.')

    add_cesta('Box Mimo', 166.00, [
        ('receita', 'Sourdough Tradicional', 1),
        ('receita', 'Croissant Tradicional', 1),
        ('receita', 'Pain au Chocolat', 1),
        ('mp', 'Suco de Uva Villa Piva 300ml', 1),
        ('mp', 'Iogurte Artesanal 200ml', 1),
        ('mp', 'Granola Artesanal 100g', 1),
        ('mp', 'Caixa MDF', 1),
    ], 'Café da manhã para 1 pessoa, tamanho ideal para presentear.')

    add_cesta('Lancheira Especial', 57.00, [
        ('mp', 'Lancheira', 1),
        ('receita', 'Croissant Tradicional', 1),
        ('receita', 'Croissant Nutella', 1),
        ('receita', 'Cookie Calebaut', 1),
    ], 'Perfeita para mandar um mimo para alguém especial.')

    # ── 6. Cestas Personalizadas (só a base) ──
    add_cesta('Personalizada Base Coração', 97.00, [
        ('mp', 'Base Coração MDF', 1),
    ], 'Base em formato de coração na cor vermelha. Personalize com seus produtos.', 'Cestas Personalizadas')

    add_cesta('Personalizada Base Quadrada', 87.00, [
        ('mp', 'Base Quadrada MDF', 1),
    ], 'Formato clássico e sofisticado. Ideal para presentes corporativos.', 'Cestas Personalizadas')

    add_cesta('Personalizada Base Redonda', 72.00, [
        ('mp', 'Base Redonda MDF', 1),
    ], 'Tradicional e versátil. A opção mais popular!', 'Cestas Personalizadas')

    db.session.commit()


def seed_rh():
    """Cria lojas e importa 42 funcionários. Roda em TODOS os ambientes."""
    if Loja.query.first():
        return

    lojas = [
        Loja(nome='Industria'),
        Loja(nome='Loja Ribeiro do Vale'),
        Loja(nome='Loja Anesio Pinto Rosa'),
        Loja(nome='Loja Nebraska'),
    ]
    db.session.add_all(lojas)
    db.session.flush()

    loja_industria = Loja.query.filter_by(nome='Industria').first()

    funcionarios_data = [
        ('ALESSANDRA MARIA DA SILVA MARIANO', '000.000.001-01', 'ATENDENTE', 2130.40, date(2025, 7, 22), 0, 0, 10.80, 26, 22.00),
        ('AMANDA DE SOUZA MIGUEL', '000.000.002-02', 'ATENDENTE', 2130.40, date(2026, 1, 12), 0, 0, 18.80, 26, 22.00),
        ('BRUNO CALIXTO FILETO DE SOUZA', '000.000.003-03', 'ATENDENTE', 2130.40, date(2025, 12, 18), 0, 0, 10.80, 26, 22.00),
        ('CARMEM KARINI LEITE', '000.000.004-04', 'AUXILIAR ADMINISTRATIVO', 2210.98, date(2025, 10, 16), 0, 0, 20.10, 22, 22.00),
        ('DAKSON ALEXANDRE MORATO SOARES DE LIMA', '000.000.005-05', 'GERENTE DE RH', 4716.55, date(2024, 10, 19), 1886.62, 0, 18.80, 26, 22.00),
        ('DAVI JONATAS MORATO DOS SANTOS', '000.000.006-06', 'PADEIRO', 3204.52, date(2022, 3, 3), 0, 0, 10.80, 25, 22.00),
        ('JOAO PEDRO FERNANDES DA SILVA', '000.000.007-07', 'GERENTE', 2879.19, date(2022, 10, 11), 1151.68, 0, 10.80, 26, 22.00),
        ('JOSE FRANCISCO MARQUES', '000.000.008-08', 'AUXILIAR DE LIMPEZA', 2130.40, date(2026, 1, 8), 0, 0, 18.80, 25, 22.00),
        ('JULIA DE SOUZA MIGUEL', '000.000.009-09', 'ATENDENTE', 2130.40, date(2026, 1, 6), 0, 0, 18.80, 26, 22.00),
        ('KAIO FERREIRA DOS REIS', '000.000.010-10', 'AJUDANTE DE PADEIRO', 2257.84, date(2023, 12, 18), 0, 0, 10.80, 26, 22.00),
        ('KELVIN ROCHA ARAUJO', '000.000.011-11', 'ATENDENTE CHEFE', 2257.84, date(2023, 1, 14), 0, 0, 10.80, 26, 22.00),
        ('KETLIN BRAGA DA ANUNCIACAO', '000.000.012-12', 'ATENDENTE', 2130.40, date(2026, 2, 13), 0, 0, 18.80, 26, 22.00),
        ('LIDIANE DOS SANTOS PILOTO', '000.000.013-13', 'ATENDENTE', 2130.40, date(2026, 2, 13), 0, 0, 18.80, 26, 22.00),
        ('LUAN COSTA DE ARAUJO', '000.000.014-14', 'ATENDENTE', 2130.40, date(2025, 9, 29), 0, 0, 18.80, 26, 22.00),
        ('MARCIA DE SOUZA GONCALVES', '000.000.015-15', 'AUXILIAR DE RH', 2221.96, date(2025, 9, 1), 0, 0, 27.00, 22, 22.00),
        ('MARIA ALANE SOARES DE LIMA', '000.000.016-16', 'GERENTE GERAL', 5299.50, date(2021, 11, 5), 0, 0, 18.80, 26, 23.69),
        ('MATHEUS DOS SANTOS ARAUJO', '000.000.017-17', 'ATENDENTE', 2130.40, date(2025, 8, 18), 0, 0, 18.80, 26, 22.00),
        ('MICAELA RODRIGUES MARINHO DOS SANTOS', '000.000.018-18', 'ATENDENTE', 2130.40, date(2025, 10, 13), 0, 0, 18.80, 26, 22.00),
        ('RAFAEL JONATAS MORATO DOS SANTOS', '000.000.019-19', 'GERENTE', 2879.19, date(2022, 12, 23), 0, 0, 10.80, 26, 22.00),
        ('SIMONE CORDEIRO ALVES', '000.000.020-20', 'ATENDENTE CHEFE', 2257.84, date(2023, 1, 1), 0, 0, 10.80, 26, 22.00),
        ('SIMONE FERNANDES', '000.000.021-21', 'INSPETOR(A) DE QUALIDADE', 2130.40, date(2021, 11, 19), 0, 600.00, 10.80, 26, 22.00),
        ('VILSON SILVA SANTANA', '000.000.022-22', 'PADEIRO', 3084.00, date(2025, 7, 17), 0, 0, 21.40, 26, 22.00),
        ('WILLIAM DE MOURA', '000.000.023-23', 'MOTORISTA', 3561.00, date(2021, 9, 14), 0, 0, 0, 26, 22.00),
        ('AMANDA SILVA DE OLIVEIRA', '000.000.024-24', 'ATENDENTE', 2130.40, date(2023, 4, 5), 0, 600.00, 10.80, 26, 22.00),
        ('BRUNA KELLY ROSENO DE SOUZA', '000.000.025-25', 'ATENDENTE 2', 2330.40, date(2023, 12, 21), 0, 0, 10.80, 26, 22.00),
        ('CAMILA ALVES DA SILVA', '000.000.026-26', 'ATENDENTE', 2130.40, date(2024, 7, 16), 0, 400.00, 18.80, 26, 22.00),
        ('DAIANE CARLA OLIVEIRA DE SOUZA', '000.000.027-27', 'ATENDENTE', 2130.40, date(2026, 1, 21), 0, 0, 10.80, 26, 22.00),
        ('DEIVID FAGUNDES DOS SANTOS', '000.000.028-28', 'ATENDENTE', 2330.40, date(2025, 2, 19), 0, 0, 10.80, 26, 22.00),
        ('HELIO BRANDAO SANTOS', '000.000.029-29', 'MOTOBOY', 9.68, date(2023, 7, 13), 0, 0, 0, 26, 22.00),
        ('ISABELA FONTES ARAUJO', '000.000.030-30', 'ATENDENTE', 2130.40, date(2025, 6, 10), 0, 0, 10.80, 26, 22.00),
        ('JIMMY RIBEIRO CASTRO', '000.000.031-31', 'ATENDENTE', 2130.40, date(2025, 6, 24), 0, 0, 18.80, 26, 22.00),
        ('KAWANNY DAS NERES FERREIRA', '000.000.032-32', 'ATENDENTE', 2130.40, date(2026, 1, 21), 0, 0, 18.80, 26, 22.00),
        ('LARISSA SANTOS DA SILVA', '000.000.033-33', 'ATENDENTE CHEFE', 2257.84, date(2023, 7, 12), 0, 0, 10.80, 26, 22.00),
        ('LUCAS DOS SANTOS', '000.000.034-34', 'ATENDENTE 2', 2330.40, date(2025, 4, 3), 0, 0, 10.80, 26, 22.00),
        ('MARILZA APARECIDA FRAGA', '000.000.035-35', 'ATENDENTE', 2130.40, date(2025, 1, 7), 0, 0, 10.80, 26, 22.00),
        ('MARINA SANTOS SILVA', '000.000.036-36', 'ATENDENTE 2', 2330.40, date(2025, 5, 6), 0, 0, 12.80, 26, 22.00),
        ('NAYARA JAMILE SANTOS', '000.000.037-37', 'ATENDENTE', 2130.00, date(2025, 12, 2), 0, 0, 18.80, 26, 22.00),
        ('QUEREM RAPUQUE DE OLIVEIRA GONCALVES', '000.000.038-38', 'ATENDENTE', 2130.00, date(2025, 12, 22), 0, 0, 18.80, 26, 22.00),
        ('SABRINA MELO BARAUNA', '000.000.039-39', 'ATENDENTE CHEFE', 2257.84, date(2023, 6, 3), 0, 0, 10.80, 26, 22.00),
        ('THIAGO BATISTA DA SILVA', '000.000.040-40', 'ATENDENTE 2', 2330.40, date(2025, 5, 3), 0, 0, 18.80, 26, 22.00),
        ('THIERRY KAUE FERREIRA DE JESUS BARROS', '000.000.041-41', 'ATENDENTE', 2130.40, date(2025, 4, 25), 0, 0, 10.80, 25, 22.00),
        ('VITORIA THALITA DE JESUS', '000.000.042-42', 'ATENDENTE', 2130.40, date(2026, 2, 25), 0, 0, 18.80, 26, 22.00),
        ('WILDINA DE SOUZA OLIVEIRA', '000.000.043-43', 'ATENDENTE 2', 2330.40, date(2025, 5, 16), 0, 0, 10.80, 26, 22.00),
    ]

    for nome, cpf, funcao, salario, admissao, cargo_conf, premiacao, vt_dia, dias, vr_dia in funcionarios_data:
        func = Funcionario(
            nome=nome,
            cpf=cpf,
            funcao=funcao,
            salario_base=salario,
            data_admissao=admissao,
            cargo_confianca=cargo_conf,
            premiacao=premiacao,
            vt_dia=vt_dia,
            dias_trabalhados=dias,
            vr_dia=vr_dia,
        )
        func.lojas.append(loja_industria)
        db.session.add(func)

    db.session.commit()


def seed_rh_escala():
    """Atribui lojas corretas, função operacional, período e posições de escala.
    Idempotente: só roda se não houver posições cadastradas.
    """
    if Posicao.query.first():
        return

    loja_ribeiro = Loja.query.filter_by(nome='Loja Ribeiro do Vale').first()
    loja_anesio = Loja.query.filter_by(nome='Loja Anesio Pinto Rosa').first()
    loja_nebraska = Loja.query.filter_by(nome='Loja Nebraska').first()
    loja_industria = Loja.query.filter_by(nome='Industria').first()

    if not all([loja_ribeiro, loja_anesio, loja_nebraska, loja_industria]):
        return

    # Mapeamento: nome_funcionario → (loja, funcao_operacional, periodo, observacao)
    atribuicoes = {
        # ── Loja Ribeiro do Vale — Manhã ──
        'ISABELA FONTES ARAUJO': (loja_ribeiro, 'Caixa 1', 'Manhã', ''),
        'THIERRY KAUE FERREIRA DE JESUS BARROS': (loja_ribeiro, 'Caixa 2', 'Manhã', ''),
        'LIDIANE DOS SANTOS PILOTO': (loja_ribeiro, 'Café', 'Manhã', ''),
        'LUAN COSTA DE ARAUJO': (loja_ribeiro, 'Chapa 2', 'Manhã', ''),
        'DAKSON ALEXANDRE MORATO SOARES DE LIMA': (loja_ribeiro, 'Finalização', 'Manhã', ''),
        'KETLIN BRAGA DA ANUNCIACAO': (loja_ribeiro, 'Mesa 1', 'Manhã', ''),
        'MICAELA RODRIGUES MARINHO DOS SANTOS': (loja_ribeiro, 'Mesa 2', 'Manhã', ''),
        'JOAO PEDRO FERNANDES DA SILVA': (loja_ribeiro, 'Suco 1', 'Manhã', 'Iniciou 09/03. Também faz Finalização Tarde em Loja Nebraska'),
        'QUEREM RAPUQUE DE OLIVEIRA GONCALVES': (loja_ribeiro, 'Suco 2', 'Manhã', ''),
        'SABRINA MELO BARAUNA': (loja_ribeiro, 'Viajem', 'Manhã', 'Atendente Chefe'),
        # ── Loja Ribeiro do Vale — Tarde ──
        'LARISSA SANTOS DA SILVA': (loja_ribeiro, 'Finalização', 'Tarde', 'Atendente Chefe'),
        'THIAGO BATISTA DA SILVA': (loja_ribeiro, 'Chapa', 'Tarde', 'Girando folga do João Pedro (Nebraska)'),
        'KAWANNY DAS NERES FERREIRA': (loja_ribeiro, 'Mesa', 'Tarde', ''),
        'DAIANE CARLA OLIVEIRA DE SOUZA': (loja_ribeiro, 'Cozinha', 'Tarde', ''),
        'KELVIN ROCHA ARAUJO': (loja_ribeiro, 'Viajem', 'Tarde', 'Atendente Chefe - Girando folgas'),
        # ── Loja Anesio Pinto Rosa — Manhã ──
        'AMANDA SILVA DE OLIVEIRA': (loja_anesio, 'Caixa', 'Manhã', ''),
        'SIMONE CORDEIRO ALVES': (loja_anesio, 'Finalização', 'Manhã', 'Atendente Chefe'),
        'MATHEUS DOS SANTOS ARAUJO': (loja_anesio, 'Chapa', 'Manhã', ''),
        'BRUNA KELLY ROSENO DE SOUZA': (loja_anesio, 'Café', 'Manhã', ''),
        'SIMONE FERNANDES': (loja_anesio, 'Suco', 'Manhã', ''),
        'DEIVID FAGUNDES DOS SANTOS': (loja_anesio, 'Mesa', 'Manhã', ''),
        # ── Loja Anesio Pinto Rosa — Tarde ──
        'WILDINA DE SOUZA OLIVEIRA': (loja_anesio, 'Finalização', 'Tarde', ''),
        'AMANDA DE SOUZA MIGUEL': (loja_anesio, 'Café', 'Tarde', ''),
        # ── Loja Nebraska — Manhã ──
        'LUCAS DOS SANTOS': (loja_nebraska, 'Caixa 1', 'Manhã', ''),
        'ALESSANDRA MARIA DA SILVA MARIANO': (loja_nebraska, 'Café', 'Manhã', 'Em aviso prévio'),
        'BRUNO CALIXTO FILETO DE SOUZA': (loja_nebraska, 'Chapa 1', 'Manhã', ''),
        'RAFAEL JONATAS MORATO DOS SANTOS': (loja_nebraska, 'Finalização', 'Manhã', ''),
        # ── Loja Nebraska — Tarde ──
        'MARINA SANTOS SILVA': (loja_nebraska, 'Suco', 'Tarde', ''),
        # ── Industria — Manhã ──
        'VILSON SILVA SANTANA': (loja_industria, 'Padeiro', 'Manhã', ''),
        'DAVI JONATAS MORATO DOS SANTOS': (loja_industria, 'Padeiro', 'Manhã', ''),
        'KAIO FERREIRA DOS REIS': (loja_industria, 'Ajudante de Padeiro', 'Manhã', ''),
        'CAMILA ALVES DA SILVA': (loja_industria, 'Auxiliar de Produção', 'Manhã', ''),
        # ── Industria — Tarde ──
        'MARILZA APARECIDA FRAGA': (loja_industria, 'Auxiliar de Produção', 'Tarde', ''),
    }

    # Aplicar atribuições
    for nome, (loja, fo, periodo, obs_extra) in atribuicoes.items():
        func = Funcionario.query.filter_by(nome=nome).first()
        if not func:
            continue
        func.lojas.clear()
        func.lojas.append(loja)
        func.funcao_operacional = fo
        func.periodo = periodo
        if obs_extra:
            existing = func.observacao or ''
            func.observacao = (existing + ' | ' + obs_extra).strip(' |') if existing else obs_extra

    # João Pedro também trabalha tarde em Nebraska (adicionar 2ª loja)
    joao = Funcionario.query.filter_by(nome='JOAO PEDRO FERNANDES DA SILVA').first()
    if joao and loja_nebraska not in joao.lojas:
        joao.lojas.append(loja_nebraska)

    # Funcionários sem função operacional identificada — marcar cadastro_pendente
    # para aparecerem na lista "precisa alocação"
    sem_alocacao = [
        'CARMEM KARINI LEITE',           # Aux Administrativo
        'JOSE FRANCISCO MARQUES',         # Aux Limpeza
        'JULIA DE SOUZA MIGUEL',          # Atendente sem escala
        'MARCIA DE SOUZA GONCALVES',      # Aux RH
        'MARIA ALANE SOARES DE LIMA',     # Gerente Geral
        'WILLIAM DE MOURA',               # Motorista
        'HELIO BRANDAO SANTOS',           # Motoboy
        'JIMMY RIBEIRO CASTRO',           # Atendente sem escala
        'NAYARA JAMILE SANTOS',           # Atendente sem escala
        'VITORIA THALITA DE JESUS',       # Atendente sem escala
    ]
    for nome in sem_alocacao:
        func = Funcionario.query.filter_by(nome=nome).first()
        if func:
            # Remove de todas as lojas e marca como pendente de alocação
            func.lojas.clear()
            func.cadastro_pendente = True
            if not func.observacao:
                func.observacao = 'PENDENTE: definir loja e função operacional'

    # Cadastrar Ariane (cadastro pendente)
    if not Funcionario.query.filter_by(nome='ARIANE').first():
        ariane = Funcionario(
            nome='ARIANE',
            cpf='PEND-ARIANE',
            funcao='ATENDENTE',
            funcao_operacional='Café',
            periodo='Tarde',
            salario_base=0,
            vt_dia=0,
            vr_dia=22.00,
            dias_trabalhados=26,
            cadastro_pendente=True,
            observacao='CADASTRO PENDENTE: completar CPF, salário, admissão e dados pessoais',
        )
        ariane.lojas.append(loja_ribeiro)
        db.session.add(ariane)

    db.session.flush()

    # Criar posições padrão de escala (uma linha por slot)
    posicoes_padrao = [
        # Loja Ribeiro do Vale — Manhã
        (loja_ribeiro, 'Manhã', 'Caixa 1', 1),
        (loja_ribeiro, 'Manhã', 'Caixa 2', 2),
        (loja_ribeiro, 'Manhã', 'Café', 3),
        (loja_ribeiro, 'Manhã', 'Chapa 1', 4),
        (loja_ribeiro, 'Manhã', 'Chapa 2', 5),
        (loja_ribeiro, 'Manhã', 'Finalização', 6),
        (loja_ribeiro, 'Manhã', 'Mesa 1', 7),
        (loja_ribeiro, 'Manhã', 'Mesa 2', 8),
        (loja_ribeiro, 'Manhã', 'Suco 1', 9),
        (loja_ribeiro, 'Manhã', 'Suco 2', 10),
        (loja_ribeiro, 'Manhã', 'Suco 3', 11),
        (loja_ribeiro, 'Manhã', 'Viajem', 12),
        # Loja Ribeiro do Vale — Tarde
        (loja_ribeiro, 'Tarde', 'Caixa', 1),
        (loja_ribeiro, 'Tarde', 'Finalização', 2),
        (loja_ribeiro, 'Tarde', 'Chapa', 3),
        (loja_ribeiro, 'Tarde', 'Café', 4),
        (loja_ribeiro, 'Tarde', 'Suco', 5),
        (loja_ribeiro, 'Tarde', 'Mesa', 6),
        (loja_ribeiro, 'Tarde', 'Cozinha', 7),
        (loja_ribeiro, 'Tarde', 'Viajem', 8),
        # Loja Anesio Pinto Rosa — Manhã
        (loja_anesio, 'Manhã', 'Caixa', 1),
        (loja_anesio, 'Manhã', 'Finalização', 2),
        (loja_anesio, 'Manhã', 'Chapa', 3),
        (loja_anesio, 'Manhã', 'Café', 4),
        (loja_anesio, 'Manhã', 'Suco', 5),
        (loja_anesio, 'Manhã', 'Mesa', 6),
        # Loja Anesio Pinto Rosa — Tarde
        (loja_anesio, 'Tarde', 'Caixa', 1),
        (loja_anesio, 'Tarde', 'Finalização', 2),
        (loja_anesio, 'Tarde', 'Chapa', 3),
        (loja_anesio, 'Tarde', 'Café', 4),
        (loja_anesio, 'Tarde', 'Suco', 5),
        (loja_anesio, 'Tarde', 'Mesa', 6),
        # Loja Nebraska — Manhã
        (loja_nebraska, 'Manhã', 'Caixa 1', 1),
        (loja_nebraska, 'Manhã', 'Caixa 2', 2),
        (loja_nebraska, 'Manhã', 'Café', 3),
        (loja_nebraska, 'Manhã', 'Chapa 1', 4),
        (loja_nebraska, 'Manhã', 'Chapa 2', 5),
        (loja_nebraska, 'Manhã', 'Finalização', 6),
        (loja_nebraska, 'Manhã', 'Mesa 1', 7),
        (loja_nebraska, 'Manhã', 'Mesa 2', 8),
        (loja_nebraska, 'Manhã', 'Suco 1', 9),
        (loja_nebraska, 'Manhã', 'Suco 2', 10),
        (loja_nebraska, 'Manhã', 'Suco 3', 11),
        (loja_nebraska, 'Manhã', 'Viajem', 12),
        # Loja Nebraska — Tarde
        (loja_nebraska, 'Tarde', 'Caixa', 1),
        (loja_nebraska, 'Tarde', 'Finalização', 2),
        (loja_nebraska, 'Tarde', 'Chapa', 3),
        (loja_nebraska, 'Tarde', 'Café', 4),
        (loja_nebraska, 'Tarde', 'Suco', 5),
        (loja_nebraska, 'Tarde', 'Mesa', 6),
        (loja_nebraska, 'Tarde', 'Viajem', 7),
        # Industria — Manhã
        (loja_industria, 'Manhã', 'Padeiro 1', 1),
        (loja_industria, 'Manhã', 'Padeiro 2', 2),
        (loja_industria, 'Manhã', 'Ajudante de Padeiro', 3),
        (loja_industria, 'Manhã', 'Auxiliar de Produção', 4),
        # Industria — Tarde
        (loja_industria, 'Tarde', 'Auxiliar de Produção', 1),
    ]

    posicoes_criadas = {}
    for loja, periodo, nome_pos, ordem in posicoes_padrao:
        pos = Posicao(
            loja_id=loja.id,
            periodo=periodo,
            nome_posicao=nome_pos,
            ordem=ordem,
            status='vago',
        )
        db.session.add(pos)
        posicoes_criadas[(loja.id, periodo, nome_pos)] = pos

    db.session.flush()

    # Associar funcionários às posições
    def find_pos(loja, periodo, nome):
        return posicoes_criadas.get((loja.id, periodo, nome))

    # Loja Ribeiro do Vale - Manhã
    atribuicoes_pos = [
        ('ISABELA FONTES ARAUJO', loja_ribeiro, 'Manhã', 'Caixa 1', 'ativo'),
        ('THIERRY KAUE FERREIRA DE JESUS BARROS', loja_ribeiro, 'Manhã', 'Caixa 2', 'ativo'),
        ('LIDIANE DOS SANTOS PILOTO', loja_ribeiro, 'Manhã', 'Café', 'ativo'),
        ('LUAN COSTA DE ARAUJO', loja_ribeiro, 'Manhã', 'Chapa 2', 'ativo'),
        ('DAKSON ALEXANDRE MORATO SOARES DE LIMA', loja_ribeiro, 'Manhã', 'Finalização', 'ativo'),
        ('KETLIN BRAGA DA ANUNCIACAO', loja_ribeiro, 'Manhã', 'Mesa 1', 'ativo'),
        ('MICAELA RODRIGUES MARINHO DOS SANTOS', loja_ribeiro, 'Manhã', 'Mesa 2', 'ativo'),
        ('JOAO PEDRO FERNANDES DA SILVA', loja_ribeiro, 'Manhã', 'Suco 1', 'ativo'),
        ('QUEREM RAPUQUE DE OLIVEIRA GONCALVES', loja_ribeiro, 'Manhã', 'Suco 2', 'ativo'),
        ('SABRINA MELO BARAUNA', loja_ribeiro, 'Manhã', 'Viajem', 'ativo'),
        # Loja Ribeiro do Vale - Tarde
        ('LARISSA SANTOS DA SILVA', loja_ribeiro, 'Tarde', 'Finalização', 'ativo'),
        ('THIAGO BATISTA DA SILVA', loja_ribeiro, 'Tarde', 'Chapa', 'girando_folga'),
        ('KAWANNY DAS NERES FERREIRA', loja_ribeiro, 'Tarde', 'Mesa', 'ativo'),
        ('DAIANE CARLA OLIVEIRA DE SOUZA', loja_ribeiro, 'Tarde', 'Cozinha', 'ativo'),
        ('KELVIN ROCHA ARAUJO', loja_ribeiro, 'Tarde', 'Viajem', 'girando_folga'),
        # Loja Anesio Pinto Rosa - Manhã
        ('AMANDA SILVA DE OLIVEIRA', loja_anesio, 'Manhã', 'Caixa', 'ativo'),
        ('SIMONE CORDEIRO ALVES', loja_anesio, 'Manhã', 'Finalização', 'ativo'),
        ('MATHEUS DOS SANTOS ARAUJO', loja_anesio, 'Manhã', 'Chapa', 'ativo'),
        ('BRUNA KELLY ROSENO DE SOUZA', loja_anesio, 'Manhã', 'Café', 'ativo'),
        ('SIMONE FERNANDES', loja_anesio, 'Manhã', 'Suco', 'ativo'),
        ('DEIVID FAGUNDES DOS SANTOS', loja_anesio, 'Manhã', 'Mesa', 'ativo'),
        # Loja Anesio Pinto Rosa - Tarde
        ('WILDINA DE SOUZA OLIVEIRA', loja_anesio, 'Tarde', 'Finalização', 'ativo'),
        ('AMANDA DE SOUZA MIGUEL', loja_anesio, 'Tarde', 'Café', 'ativo'),
        # Loja Nebraska - Manhã
        ('LUCAS DOS SANTOS', loja_nebraska, 'Manhã', 'Caixa 1', 'ativo'),
        ('ALESSANDRA MARIA DA SILVA MARIANO', loja_nebraska, 'Manhã', 'Café', 'aviso_previo'),
        ('BRUNO CALIXTO FILETO DE SOUZA', loja_nebraska, 'Manhã', 'Chapa 1', 'ativo'),
        ('RAFAEL JONATAS MORATO DOS SANTOS', loja_nebraska, 'Manhã', 'Finalização', 'ativo'),
        # Loja Nebraska - Tarde
        ('JOAO PEDRO FERNANDES DA SILVA', loja_nebraska, 'Tarde', 'Finalização', 'ativo'),
        ('MARINA SANTOS SILVA', loja_nebraska, 'Tarde', 'Suco', 'ativo'),
        # Industria - Manhã
        ('VILSON SILVA SANTANA', loja_industria, 'Manhã', 'Padeiro 1', 'ativo'),
        ('DAVI JONATAS MORATO DOS SANTOS', loja_industria, 'Manhã', 'Padeiro 2', 'ativo'),
        ('KAIO FERREIRA DOS REIS', loja_industria, 'Manhã', 'Ajudante de Padeiro', 'ativo'),
        ('CAMILA ALVES DA SILVA', loja_industria, 'Manhã', 'Auxiliar de Produção', 'ativo'),
        # Industria - Tarde
        ('MARILZA APARECIDA FRAGA', loja_industria, 'Tarde', 'Auxiliar de Produção', 'ativo'),
    ]

    for nome_func, loja, periodo, nome_pos, status in atribuicoes_pos:
        func = Funcionario.query.filter_by(nome=nome_func).first()
        pos = find_pos(loja, periodo, nome_pos)
        if func and pos:
            pos.funcionario_id = func.id
            pos.status = status

    # Ariane também (Café Tarde Ribeiro)
    ariane = Funcionario.query.filter_by(nome='ARIANE').first()
    pos_ariane = find_pos(loja_ribeiro, 'Tarde', 'Café')
    if ariane and pos_ariane:
        pos_ariane.funcionario_id = ariane.id
        pos_ariane.status = 'ativo'

    db.session.commit()
