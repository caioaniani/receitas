import json

from flask import render_template, redirect, url_for, flash, request, abort, jsonify
from flask_login import login_required, current_user

from app.blueprints.receitas import receitas_bp
from app.decorators import admin_required
from app.extensions import db
from app.models import MateriaPrima, Receita, ReceitaIngrediente, Atribuicao
from app.services.custos import calcular_custos_receitas
from app.utils import parse_float_br


@receitas_bp.route('/<int:id>')
@login_required
def ficha(id):
    receita = Receita.query.get_or_404(id)

    # Funcionário só acessa fichas atribuídas
    if not current_user.is_admin():
        atribuida = Atribuicao.query.filter_by(
            receita_id=id, usuario_id=current_user.id
        ).first()
        if not atribuida:
            abort(403)

    mp_dict = {mp.nome: mp for mp in MateriaPrima.query.all()}

    resultado = calcular_custos_receitas()

    return render_template('receitas/ficha.html', receita=receita, mp_dict=mp_dict,
                           receita_custos_json=json.dumps(resultado['custos'], ensure_ascii=False),
                           receita_pesos_json=json.dumps(resultado['pesos'], ensure_ascii=False))


@receitas_bp.route('/padeiro')
@login_required
def padeiro_lista():
    receitas = Receita.query.order_by(Receita.categoria, Receita.nome).all()
    categorias = {}
    for r in receitas:
        cat = r.categoria or 'Outros'
        categorias.setdefault(cat, []).append(r)
    return render_template('receitas/padeiro_lista.html', categorias=categorias)


@receitas_bp.route('/<int:id>/padeiro')
@login_required
def padeiro(id):
    receita = Receita.query.get_or_404(id)
    resultado = calcular_custos_receitas()
    return render_template('receitas/padeiro.html', receita=receita,
                           receita_custos_json=json.dumps(resultado['custos'], ensure_ascii=False),
                           receita_pesos_json=json.dumps(resultado['pesos'], ensure_ascii=False))


@receitas_bp.route('/<int:id>/salvar', methods=['POST'])
@login_required
def salvar(id):
    receita = Receita.query.get_or_404(id)

    # Funcionário só pode salvar fichas atribuídas
    if not current_user.is_admin():
        atribuida = Atribuicao.query.filter_by(
            receita_id=id, usuario_id=current_user.id
        ).first()
        if not atribuida:
            abort(403)

    receita.nome = request.form.get('nome', receita.nome).strip()
    receita.categoria = request.form.get('categoria', '').strip() or None
    receita.preco_venda = parse_float_br(request.form.get('preco_venda', ''))
    receita.preco_loja = parse_float_br(request.form.get('preco_loja', ''))
    receita.preco_site = parse_float_br(request.form.get('preco_site', ''))
    receita.rendimento_qtd = parse_float_br(request.form.get('rendimento_qtd', ''), default=1)
    receita.rendimento_unidade = request.form.get('rendimento_unidade', 'unidades').strip()
    receita.peso_base = parse_float_br(request.form.get('peso_base', ''), default=1000)
    receita.peso_unitario = parse_float_br(request.form.get('peso_unitario', ''))
    receita.perda_percentual = parse_float_br(request.form.get('perda_percentual', ''), default=0)
    receita.custo_embalagem = parse_float_br(request.form.get('custo_embalagem', ''), default=0)
    receita.modo_preparo = request.form.get('modo_preparo', '').strip() or None
    receita.observacao = request.form.get('observacao', '').strip() or None

    # Atualiza ingredientes
    ReceitaIngrediente.query.filter_by(receita_id=receita.id).delete()

    tipos = request.form.getlist('ingrediente_tipo[]')
    nomes = request.form.getlist('ingrediente_nome[]')
    porcentagens = request.form.getlist('porcentagem[]')
    bases = request.form.getlist('eh_base[]')
    notas = request.form.getlist('nota[]')

    for i in range(len(nomes)):
        nome = nomes[i].strip()
        pct_str = porcentagens[i].replace(',', '.').strip()
        if not nome or not pct_str:
            continue
        tipo = tipos[i] if i < len(tipos) else 'mp'
        ing = ReceitaIngrediente(
            receita_id=receita.id,
            tipo=tipo,
            ingrediente_nome=nome,
            porcentagem=float(pct_str),
            eh_base=(bases[i] == '1') if i < len(bases) else False,
            nota=notas[i].strip() if i < len(notas) else None,
        )
        db.session.add(ing)

    db.session.commit()
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify(success=True)
    flash('Ficha salva com sucesso!', 'success')
    return redirect(url_for('receitas.ficha', id=receita.id))


@receitas_bp.route('/nova', methods=['POST'])
@login_required
@admin_required
def nova():
    receita = Receita(
        nome='Novo Produto',
        categoria='',
        rendimento_qtd=1,
        rendimento_unidade='unidades',
        peso_base=1000,
    )
    db.session.add(receita)
    db.session.commit()
    flash('Novo produto criado!', 'success')
    return redirect(url_for('receitas.ficha', id=receita.id))


@receitas_bp.route('/<int:id>/duplicar', methods=['POST'])
@login_required
@admin_required
def duplicar(id):
    original = Receita.query.get_or_404(id)
    copia = Receita(
        nome=f'Cópia de {original.nome}',
        categoria=original.categoria,
        preco_venda=original.preco_venda,
        preco_loja=original.preco_loja,
        preco_site=original.preco_site,
        rendimento_qtd=original.rendimento_qtd,
        rendimento_unidade=original.rendimento_unidade,
        peso_base=original.peso_base,
        peso_unitario=original.peso_unitario,
        perda_percentual=original.perda_percentual,
        custo_embalagem=original.custo_embalagem,
        modo_preparo=original.modo_preparo,
    )
    db.session.add(copia)
    db.session.flush()

    for ing in original.ingredientes:
        novo_ing = ReceitaIngrediente(
            receita_id=copia.id,
            tipo=ing.tipo or 'mp',
            ingrediente_nome=ing.ingrediente_nome,
            porcentagem=ing.porcentagem,
            eh_base=ing.eh_base,
            nota=ing.nota,
        )
        db.session.add(novo_ing)

    db.session.commit()
    flash(f'Receita duplicada: "{copia.nome}"', 'success')
    return redirect(url_for('receitas.ficha', id=copia.id))


@receitas_bp.route('/<int:id>/excluir', methods=['POST'])
@login_required
@admin_required
def excluir(id):
    receita = Receita.query.get_or_404(id)
    nome = receita.nome
    db.session.delete(receita)
    db.session.commit()
    flash(f'"{nome}" excluído com sucesso!', 'success')
    return redirect(url_for('receitas.padeiro_lista'))


@receitas_bp.route('/api/nova-mp', methods=['POST'])
@login_required
@admin_required
def nova_mp():
    """Cria matéria-prima via AJAX (sem sair da ficha técnica)."""
    nome = request.form.get('mp_nome', '').strip()
    custo = request.form.get('mp_custo', '').replace(',', '.').strip()

    if not nome or not custo:
        return jsonify(success=False, error='Preencha nome e custo.')

    if MateriaPrima.query.filter_by(nome=nome).first():
        return jsonify(success=False, error=f'"{nome}" ja existe no banco de MP.')

    try:
        custo_float = float(custo)
    except ValueError:
        return jsonify(success=False, error='Custo invalido.')

    mp = MateriaPrima(nome=nome, unidade='g', custo_por_kg=custo_float)
    db.session.add(mp)
    db.session.commit()

    return jsonify(success=True, nome=nome, custo=custo_float)
