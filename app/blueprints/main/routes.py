import json

from flask import redirect, url_for, jsonify, request, Response, render_template

from app.blueprints.main import main_bp
from app.extensions import db
from app.models import MateriaPrima, Receita, ReceitaIngrediente


@main_bp.route('/')
def index():
    return redirect(url_for('materias_primas.banco'))


@main_bp.route('/rentabilidade')
def rentabilidade():
    receitas = Receita.query.order_by(Receita.categoria, Receita.nome).all()
    mp_dict = {mp.nome: mp.custo_por_kg for mp in MateriaPrima.query.all()}

    dados = []
    for r in receitas:
        # Calcular custo total da receita
        custo_total = 0
        sum_pct = 0
        for ing in r.ingredientes:
            sum_pct += ing.porcentagem
            qtd_g = r.peso_base * ing.porcentagem / 100
            custo_kg = mp_dict.get(ing.ingrediente_nome, 0)
            custo_total += qtd_g / 1000 * custo_kg

        # Calcular rendimento considerando perda
        total_qtd = r.peso_base * sum_pct / 100
        perda = r.perda_percentual or 0
        peso_pos_perda = total_qtd * (1 - perda / 100)

        if r.peso_unitario and r.peso_unitario > 0 and peso_pos_perda > 0:
            rendimento = int(peso_pos_perda / r.peso_unitario)
        else:
            rendimento = int(r.rendimento_qtd)

        custo_un = custo_total / rendimento if rendimento > 0 else 0

        preco_at = r.preco_venda or 0
        lucro_at = preco_at - custo_un if preco_at > 0 else None
        margem_at = (lucro_at / preco_at * 100) if (preco_at > 0 and lucro_at is not None) else None

        preco_lj = r.preco_loja or 0
        lucro_lj = preco_lj - custo_un if preco_lj > 0 else None
        margem_lj = (lucro_lj / preco_lj * 100) if (preco_lj > 0 and lucro_lj is not None) else None

        dados.append({
            'id': r.id,
            'nome': r.nome,
            'categoria': r.categoria or 'Outros',
            'rendimento': rendimento,
            'custo_total': custo_total,
            'custo_un': custo_un,
            'preco_atacado': preco_at,
            'lucro_atacado': lucro_at,
            'margem_atacado': margem_at,
            'preco_loja': preco_lj,
            'lucro_loja': lucro_lj,
            'margem_loja': margem_lj,
        })

    return render_template('main/rentabilidade.html', dados=dados)


@main_bp.route('/cardapio')
def cardapio():
    tipo = request.args.get('tipo', 'atacado')
    receitas = Receita.query.order_by(Receita.categoria, Receita.nome).all()

    categorias = {}
    for r in receitas:
        preco = r.preco_venda if tipo == 'atacado' else r.preco_loja
        if not preco or preco <= 0:
            continue
        cat = r.categoria or 'Outros'
        if cat not in categorias:
            categorias[cat] = []
        categorias[cat].append({
            'nome': r.nome,
            'peso_unitario': r.peso_unitario,
            'rendimento_unidade': r.rendimento_unidade,
            'preco_venda': preco,
        })

    return render_template('main/cardapio.html', categorias=categorias, tipo=tipo)


@main_bp.route('/api/exportar')
def exportar():
    mps = MateriaPrima.query.order_by(MateriaPrima.id).all()
    receitas = Receita.query.order_by(Receita.id).all()

    data = {
        'materias_primas': [mp.to_dict() for mp in mps],
        'receitas': [r.to_dict() for r in receitas],
    }

    json_str = json.dumps(data, ensure_ascii=False, indent=2)
    return Response(
        json_str,
        mimetype='application/json',
        headers={'Content-Disposition': 'attachment; filename=padaria_backup.json'}
    )


@main_bp.route('/api/importar', methods=['POST'])
def importar():
    file = request.files.get('file')
    if not file:
        return jsonify(success=False, error='Nenhum arquivo enviado')

    try:
        data = json.loads(file.read().decode('utf-8'))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return jsonify(success=False, error='Arquivo JSON inválido')

    # Limpa tudo
    ReceitaIngrediente.query.delete()
    Receita.query.delete()
    MateriaPrima.query.delete()

    # Recria matérias-primas
    for mp_data in data.get('materias_primas', []):
        mp = MateriaPrima(
            nome=mp_data['nome'],
            unidade=mp_data.get('unidade', 'g'),
            custo_por_kg=mp_data['custo_por_kg'],
            fornecedor=mp_data.get('fornecedor') or None,
            observacoes=mp_data.get('observacoes') or None,
        )
        db.session.add(mp)

    db.session.flush()

    # Recria receitas
    for r_data in data.get('receitas', []):
        receita = Receita(
            nome=r_data['nome'],
            categoria=r_data.get('categoria') or None,
            preco_venda=r_data.get('preco_venda'),
            preco_loja=r_data.get('preco_loja'),
            rendimento_qtd=r_data['rendimento_qtd'],
            rendimento_unidade=r_data['rendimento_unidade'],
            peso_base=r_data['peso_base'],
            peso_unitario=r_data.get('peso_unitario'),
            perda_percentual=r_data.get('perda_percentual', 0),
        )
        db.session.add(receita)
        db.session.flush()

        for ing_data in r_data.get('ingredientes', []):
            ing = ReceitaIngrediente(
                receita_id=receita.id,
                ingrediente_nome=ing_data['ingrediente_nome'],
                porcentagem=ing_data['porcentagem'],
                eh_base=ing_data.get('eh_base', False),
                nota=ing_data.get('nota') or None,
            )
            db.session.add(ing)

    db.session.commit()
    return jsonify(success=True)
