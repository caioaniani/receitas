from app.extensions import db
from app.models import MateriaPrima, Receita, ReceitaIngrediente


def seed_database():
    """Popula o banco de dados com dados iniciais."""
    if MateriaPrima.query.first() is not None:
        return

    materias = {
        'farinha': MateriaPrima(nome='Farinha de Trigo', unidade='kg', preco=5.50, fornecedor='Moinho Sul'),
        'acucar': MateriaPrima(nome='Açúcar Refinado', unidade='kg', preco=4.80, fornecedor='União'),
        'sal': MateriaPrima(nome='Sal', unidade='kg', preco=2.50, fornecedor='Cisne'),
        'fermento_bio': MateriaPrima(nome='Fermento Biológico', unidade='kg', preco=35.00, fornecedor='Fleischmann'),
        'fermento_quim': MateriaPrima(nome='Fermento Químico', unidade='kg', preco=28.00, fornecedor='Royal'),
        'manteiga': MateriaPrima(nome='Manteiga', unidade='kg', preco=45.00, fornecedor='Aviação'),
        'leite': MateriaPrima(nome='Leite Integral', unidade='litro', preco=5.20, fornecedor='Italac'),
        'ovos': MateriaPrima(nome='Ovos', unidade='unidade', preco=0.80, fornecedor='Granja Mantiqueira'),
        'oleo': MateriaPrima(nome='Óleo de Soja', unidade='litro', preco=7.50, fornecedor='Soya'),
        'chocolate': MateriaPrima(nome='Chocolate em Pó', unidade='kg', preco=32.00, fornecedor='Nestlé'),
        'leite_cond': MateriaPrima(nome='Leite Condensado', unidade='unidade', preco=6.50, fornecedor='Moça'),
        'creme_leite': MateriaPrima(nome='Creme de Leite', unidade='unidade', preco=4.80, fornecedor='Nestlé'),
        'mussarela': MateriaPrima(nome='Queijo Mussarela', unidade='kg', preco=42.00, fornecedor='Polenghi'),
        'presunto': MateriaPrima(nome='Presunto', unidade='kg', preco=28.00, fornecedor='Sadia'),
    }

    for mp in materias.values():
        db.session.add(mp)
    db.session.flush()

    # Pão Francês
    pao_frances = Receita(
        nome='Pão Francês',
        categoria='pao',
        rendimento_qtd=20,
        rendimento_unidade='unidades',
        margem_lucro=150,
        custo_adicional_pct=20,
    )
    db.session.add(pao_frances)
    db.session.flush()

    ingredientes_pao = [
        ReceitaIngrediente(receita_id=pao_frances.id, materia_prima_id=materias['farinha'].id, quantidade=1.0, eh_base=True),
        ReceitaIngrediente(receita_id=pao_frances.id, materia_prima_id=materias['sal'].id, quantidade=0.02),
        ReceitaIngrediente(receita_id=pao_frances.id, materia_prima_id=materias['fermento_bio'].id, quantidade=0.03),
        ReceitaIngrediente(receita_id=pao_frances.id, materia_prima_id=materias['acucar'].id, quantidade=0.01),
        ReceitaIngrediente(receita_id=pao_frances.id, materia_prima_id=materias['manteiga'].id, quantidade=0.03),
    ]
    db.session.add_all(ingredientes_pao)

    # Bolo de Chocolate
    bolo_choco = Receita(
        nome='Bolo de Chocolate',
        categoria='bolo',
        rendimento_qtd=12,
        rendimento_unidade='fatias',
        margem_lucro=200,
        custo_adicional_pct=15,
    )
    db.session.add(bolo_choco)
    db.session.flush()

    ingredientes_bolo = [
        ReceitaIngrediente(receita_id=bolo_choco.id, materia_prima_id=materias['farinha'].id, quantidade=0.3),
        ReceitaIngrediente(receita_id=bolo_choco.id, materia_prima_id=materias['acucar'].id, quantidade=0.25),
        ReceitaIngrediente(receita_id=bolo_choco.id, materia_prima_id=materias['chocolate'].id, quantidade=0.1),
        ReceitaIngrediente(receita_id=bolo_choco.id, materia_prima_id=materias['ovos'].id, quantidade=4),
        ReceitaIngrediente(receita_id=bolo_choco.id, materia_prima_id=materias['manteiga'].id, quantidade=0.15),
        ReceitaIngrediente(receita_id=bolo_choco.id, materia_prima_id=materias['leite'].id, quantidade=0.25),
        ReceitaIngrediente(receita_id=bolo_choco.id, materia_prima_id=materias['fermento_quim'].id, quantidade=0.015),
    ]
    db.session.add_all(ingredientes_bolo)

    # Pão de Queijo
    pao_queijo = Receita(
        nome='Pão de Queijo',
        categoria='salgado',
        rendimento_qtd=30,
        rendimento_unidade='unidades',
        margem_lucro=180,
        custo_adicional_pct=15,
    )
    db.session.add(pao_queijo)
    db.session.flush()

    ingredientes_pq = [
        ReceitaIngrediente(receita_id=pao_queijo.id, materia_prima_id=materias['farinha'].id, quantidade=0.5, eh_base=True),
        ReceitaIngrediente(receita_id=pao_queijo.id, materia_prima_id=materias['mussarela'].id, quantidade=0.3),
        ReceitaIngrediente(receita_id=pao_queijo.id, materia_prima_id=materias['ovos'].id, quantidade=3),
        ReceitaIngrediente(receita_id=pao_queijo.id, materia_prima_id=materias['oleo'].id, quantidade=0.1),
        ReceitaIngrediente(receita_id=pao_queijo.id, materia_prima_id=materias['leite'].id, quantidade=0.2),
        ReceitaIngrediente(receita_id=pao_queijo.id, materia_prima_id=materias['sal'].id, quantidade=0.01),
    ]
    db.session.add_all(ingredientes_pq)

    db.session.commit()
