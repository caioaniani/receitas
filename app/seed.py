from app.extensions import db
from app.models import MateriaPrima, Receita, ReceitaIngrediente


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

    def add_receita(nome, cat, rend_qtd, rend_un, peso_base, ingredientes):
        r = Receita(nome=nome, categoria=cat, rendimento_qtd=rend_qtd,
                    rendimento_unidade=rend_un, peso_base=peso_base)
        db.session.add(r)
        db.session.flush()
        for ing_nome, pct, base, nota in ingredientes:
            db.session.add(ReceitaIngrediente(
                receita_id=r.id, ingrediente_nome=ing_nome,
                porcentagem=pct, eh_base=base, nota=nota))

    # ── 1. Croissant Tradicional ──
    add_receita('Croissant Tradicional', 'Viennoiserie', 16, 'unidades', 1000,
                croissant_base)

    # ── 2. Pain au Chocolat ──
    add_receita('Pain au Chocolat', 'Viennoiserie', 12, 'unidades', 1000,
                croissant_base + [('Baton Calebaut', 36, False, '')])

    # ── 3. Croissant Nutella com Morango ──
    add_receita('Croissant Nutella com Morango', 'Viennoiserie', 16, 'unidades', 1000,
                croissant_base + [
                    ('Nutella', 80, False, ''),
                    ('Morango fresco', 200, False, ''),
                ])

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
                ])

    # ── 5. Sourdough Tradicional ──
    add_receita('Sourdough Tradicional', 'Pães', 4, 'pães', 1000, [
        ('FarinhaT65', 100, True, ''),
        ('Agua(1L)', 80, False, ''),
        ('Levain', 25, False, ''),
        ('Sal', 2, False, ''),
        ('Fermento', 0.5, False, ''),
    ])

    # ── 6. Sourdough Integral ──
    add_receita('Sourdough Integral', 'Pães', 4, 'pães', 1000, [
        ('Farinha Integral', 100, True, ''),
        ('Agua(1L)', 75, False, ''),
        ('Levain', 25, False, ''),
        ('Sal', 2, False, ''),
        ('Fermento', 0.5, False, ''),
    ])

    # ── 7. Sourdough 7 Grãos ──
    add_receita('Sourdough 7 Grãos', 'Pães', 4, 'pães', 1000, [
        ('FarinhaT65', 100, True, ''),
        ('Agua(1L)', 85, False, ''),
        ('Levain', 25, False, ''),
        ('Sal', 2, False, ''),
        ('Fermento', 0.5, False, ''),
        ('7 Grãos', 10, False, ''),
    ])

    # ── 8. Sourdough Nozes e Azeitonas ──
    add_receita('Sourdough Nozes e Azeitonas', 'Pães', 4, 'pães', 1000, [
        ('FarinhaT65', 100, True, ''),
        ('Agua(1L)', 75, False, ''),
        ('Levain', 25, False, ''),
        ('Sal', 2, False, ''),
        ('Fermento', 0.5, False, ''),
        ('Nozes e Azeitonas', 25, False, ''),
    ])

    # ── 9. Brioche ──
    add_receita('Brioche', 'Pães', 4, 'unidades', 1000, [
        ('FarinhaT45', 100, True, ''),
        ('Acucar', 25, False, ''),
        ('Ovos', 15, False, ''),
        ('Sal', 2, False, ''),
        ('Fermento', 7, False, ''),
        ('Manteiga', 40, False, ''),
        ('Agua(1L)', 20, False, ''),
    ])

    # ── 10. Pão Francês Fermentado ──
    add_receita('Pão Francês Fermentado', 'Pães', 20, 'unidades', 1000, [
        ('FarinhaT65', 100, True, ''),
        ('Agua(1L)', 75, False, ''),
        ('Levain', 25, False, ''),
        ('Sal', 2, False, ''),
        ('Fermento', 0.5, False, ''),
    ])

    # ── 11. Pão de Forma Integral ──
    add_receita('Pão de Forma Integral', 'Pães', 4, 'pães', 1000, [
        ('Farinha Integral', 100, True, ''),
        ('Acucar', 16, False, ''),
        ('Sal', 2, False, ''),
        ('Fermento', 3, False, ''),
        ('Agua(1L)', 75, False, ''),
    ])

    # ── 12. Pão de Forma Integral com Grãos ──
    add_receita('Pão de Forma Integral com Grãos', 'Pães', 4, 'pães', 1000, [
        ('Farinha Integral', 100, True, ''),
        ('Acucar', 16, False, ''),
        ('Sal', 2, False, ''),
        ('Fermento', 3, False, ''),
        ('Agua(1L)', 75, False, ''),
        ('7 Grãos', 12, False, ''),
    ])

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
    ])

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
    ])

    db.session.commit()
