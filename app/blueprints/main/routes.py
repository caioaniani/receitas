import json

from flask import redirect, url_for, jsonify, request, Response, render_template
from flask_login import login_required

from app.blueprints.main import main_bp
from app.extensions import db
from app.models import MateriaPrima, Receita, ReceitaIngrediente, Produto, ProdutoItem


@main_bp.route('/')
@login_required
def index():
    return redirect(url_for('materias_primas.banco'))


@main_bp.route('/rentabilidade')
@login_required
def rentabilidade():
    receitas = Receita.query.order_by(Receita.categoria, Receita.nome).all()
    mp_dict = {mp.nome: mp.custo_por_kg for mp in MateriaPrima.query.all()}

    # Multi-pass para resolver sub-receitas
    custos_receita = {}
    remaining = list(receitas)
    for _ in range(5):
        still_remaining = []
        for r in remaining:
            can_calc = True
            custo_total = 0
            sum_pct = 0
            qtd_direto = 0

            for ing in r.ingredientes:
                tipo = ing.tipo or 'mp'
                if tipo == 'receita':
                    if ing.ingrediente_nome not in custos_receita:
                        can_calc = False
                        break
                    custo_total += custos_receita[ing.ingrediente_nome] * ing.porcentagem
                elif tipo == 'mp_direto':
                    qtd_g = ing.porcentagem
                    custo_kg = mp_dict.get(ing.ingrediente_nome, 0)
                    custo_total += qtd_g / 1000 * custo_kg
                    qtd_direto += qtd_g
                else:
                    sum_pct += ing.porcentagem
                    qtd_g = r.peso_base * ing.porcentagem / 100
                    custo_kg = mp_dict.get(ing.ingrediente_nome, 0)
                    custo_total += qtd_g / 1000 * custo_kg

            if not can_calc:
                still_remaining.append(r)
                continue

            total_qtd = r.peso_base * sum_pct / 100 + qtd_direto
            perda = r.perda_percentual or 0
            peso_pos_perda = total_qtd * (1 - perda / 100)

            if r.peso_unitario and r.peso_unitario > 0 and peso_pos_perda > 0:
                rendimento = int(peso_pos_perda / r.peso_unitario)
            else:
                rendimento = int(r.rendimento_qtd)

            embalagem = r.custo_embalagem or 0
            custo_un = (custo_total / rendimento + embalagem) if rendimento > 0 else 0
            custos_receita[r.nome] = custo_un

        remaining = still_remaining
        if not remaining:
            break

    # Agora gerar dados para o template
    dados = []
    for r in receitas:
        custo_un = custos_receita.get(r.nome, 0)

        # Recalcular rendimento para exibir
        sum_pct = sum(ing.porcentagem for ing in r.ingredientes if (ing.tipo or 'mp') == 'mp')
        qtd_dir = sum(ing.porcentagem for ing in r.ingredientes if ing.tipo == 'mp_direto')
        total_qtd = r.peso_base * sum_pct / 100 + qtd_dir
        perda = r.perda_percentual or 0
        peso_pos_perda = total_qtd * (1 - perda / 100)
        if r.peso_unitario and r.peso_unitario > 0 and peso_pos_perda > 0:
            rendimento = int(peso_pos_perda / r.peso_unitario)
        else:
            rendimento = int(r.rendimento_qtd)

        custo_total = custo_un * rendimento

        preco_at = r.preco_venda or 0
        lucro_at = preco_at - custo_un if preco_at > 0 else None
        margem_at = (lucro_at / preco_at * 100) if (preco_at > 0 and lucro_at is not None) else None

        preco_lj = r.preco_loja or 0
        lucro_lj = preco_lj - custo_un if preco_lj > 0 else None
        margem_lj = (lucro_lj / preco_lj * 100) if (preco_lj > 0 and lucro_lj is not None) else None

        preco_st = r.preco_site or 0
        lucro_st = preco_st - custo_un if preco_st > 0 else None
        margem_st = (lucro_st / preco_st * 100) if (preco_st > 0 and lucro_st is not None) else None

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
            'preco_site': preco_st,
            'lucro_site': lucro_st,
            'margem_site': margem_st,
        })

    return render_template('main/rentabilidade.html', dados=dados)


@main_bp.route('/cardapio')
@login_required
def cardapio():
    tipo = request.args.get('tipo', 'atacado')
    receitas = Receita.query.order_by(Receita.categoria, Receita.nome).all()
    produtos = Produto.query.filter_by(ativo=True).order_by(Produto.categoria, Produto.nome).all()

    campo = {'atacado': 'preco_venda', 'loja': 'preco_loja', 'site': 'preco_site'}
    attr = campo.get(tipo, 'preco_venda')

    categorias = {}

    # Receitas fabricadas
    for r in receitas:
        preco = getattr(r, attr, None) or (r.preco_venda if tipo == 'atacado' else None)
        if not preco or preco <= 0:
            continue
        cat = r.categoria or 'Outros'
        if cat not in categorias:
            categorias[cat] = []
        categorias[cat].append({
            'nome': r.nome,
            'peso_unitario': r.peso_unitario,
            'descricao': None,
            'preco_venda': preco,
        })

    # Produtos cadastrados (cestas, kits, etc.)
    campo_prod = {'atacado': 'preco_atacado', 'loja': 'preco_loja', 'site': 'preco_site'}
    attr_prod = campo_prod.get(tipo, 'preco_atacado')
    for p in produtos:
        preco = getattr(p, attr_prod, None)
        if not preco or preco <= 0:
            continue
        cat = p.categoria or 'Outros'
        if cat not in categorias:
            categorias[cat] = []
        categorias[cat].append({
            'nome': p.nome,
            'peso_unitario': None,
            'descricao': p.descricao,
            'preco_venda': preco,
        })

    return render_template('main/cardapio.html', categorias=categorias, tipo=tipo)


@main_bp.route('/api/exportar')
@login_required
def exportar():
    mps = MateriaPrima.query.order_by(MateriaPrima.id).all()
    receitas = Receita.query.order_by(Receita.id).all()
    produtos = Produto.query.order_by(Produto.id).all()

    data = {
        'materias_primas': [mp.to_dict() for mp in mps],
        'receitas': [r.to_dict() for r in receitas],
        'produtos': [p.to_dict() for p in produtos],
    }

    json_str = json.dumps(data, ensure_ascii=False, indent=2)
    return Response(
        json_str,
        mimetype='application/json',
        headers={'Content-Disposition': 'attachment; filename=padaria_backup.json'}
    )


@main_bp.route('/api/importar', methods=['POST'])
@login_required
def importar():
    file = request.files.get('file')
    if not file:
        return jsonify(success=False, error='Nenhum arquivo enviado')

    try:
        data = json.loads(file.read().decode('utf-8'))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return jsonify(success=False, error='Arquivo JSON inválido')

    # Limpa tudo
    ProdutoItem.query.delete()
    ReceitaIngrediente.query.delete()
    Receita.query.delete()
    MateriaPrima.query.delete()
    Produto.query.delete()

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
            preco_site=r_data.get('preco_site'),
            rendimento_qtd=r_data['rendimento_qtd'],
            rendimento_unidade=r_data['rendimento_unidade'],
            peso_base=r_data['peso_base'],
            peso_unitario=r_data.get('peso_unitario'),
            perda_percentual=r_data.get('perda_percentual', 0),
            custo_embalagem=r_data.get('custo_embalagem', 0),
            modo_preparo=r_data.get('modo_preparo') or None,
        )
        db.session.add(receita)
        db.session.flush()

        for ing_data in r_data.get('ingredientes', []):
            ing = ReceitaIngrediente(
                receita_id=receita.id,
                tipo=ing_data.get('tipo', 'mp'),
                ingrediente_nome=ing_data['ingrediente_nome'],
                porcentagem=ing_data['porcentagem'],
                eh_base=ing_data.get('eh_base', False),
                nota=ing_data.get('nota') or None,
            )
            db.session.add(ing)

    # Recria produtos (cestas, kits, etc.)
    for p_data in data.get('produtos', []):
        produto = Produto(
            nome=p_data['nome'],
            categoria=p_data.get('categoria') or None,
            descricao=p_data.get('descricao') or None,
            preco_atacado=p_data.get('preco_atacado'),
            preco_loja=p_data.get('preco_loja'),
            preco_site=p_data.get('preco_site'),
            custo_direto=p_data.get('custo_direto'),
            custo_embalagem=p_data.get('custo_embalagem', 0),
            modo_preparo=p_data.get('modo_preparo') or None,
            ativo=p_data.get('ativo', True),
        )
        db.session.add(produto)
        db.session.flush()

        for item_data in p_data.get('itens', []):
            item = ProdutoItem(
                produto_id=produto.id,
                tipo=item_data['tipo'],
                item_nome=item_data['item_nome'],
                quantidade=item_data['quantidade'],
            )
            db.session.add(item)

    db.session.commit()
    return jsonify(success=True)
