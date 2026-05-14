from datetime import datetime

from flask import render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user

from app.blueprints.materias_primas import materias_primas_bp
from app.decorators import admin_required, catalogo_required
from app.extensions import db
from app.models import MateriaPrima, ReceitaIngrediente, MovimentacaoEstoque, AlertaEstoque


@materias_primas_bp.route('/')
@login_required
def banco():
    materias = MateriaPrima.query.order_by(MateriaPrima.id).all()
    return render_template('materias_primas/banco.html', materias=materias)


@materias_primas_bp.route('/salvar', methods=['POST'])
@login_required
@admin_required
def salvar():
    ids = request.form.getlist('mp_id[]')
    nomes = request.form.getlist('nome[]')
    unidades = request.form.getlist('unidade[]')
    custos = request.form.getlist('custo_por_kg[]')
    pesos_unidade = request.form.getlist('peso_unidade[]')
    fornecedores = request.form.getlist('fornecedor[]')
    observacoes_list = request.form.getlist('observacoes[]')

    def _parse_peso(idx, unidade):
        if unidade != 'un' or idx >= len(pesos_unidade):
            return None
        raw = (pesos_unidade[idx] or '').replace(',', '.').strip()
        if not raw:
            return None
        try:
            v = float(raw)
            return v if v > 0 else None
        except ValueError:
            return None

    for i in range(len(nomes)):
        nome = nomes[i].strip()
        if not nome:
            continue

        custo = custos[i].replace(',', '.')
        mp_id = int(ids[i]) if ids[i] else None

        if mp_id:
            mp = MateriaPrima.query.get(mp_id)
            if mp:
                mp.nome = nome
                mp.unidade = unidades[i]
                mp.custo_por_kg = float(custo)
                mp.peso_unidade = _parse_peso(i, unidades[i])
                mp.fornecedor = fornecedores[i].strip() or None
                mp.observacoes = observacoes_list[i].strip() or None
        else:
            mp = MateriaPrima(
                nome=nome,
                unidade=unidades[i],
                custo_por_kg=float(custo),
                peso_unidade=_parse_peso(i, unidades[i]),
                fornecedor=fornecedores[i].strip() or None,
                observacoes=observacoes_list[i].strip() or None,
            )
            db.session.add(mp)

    db.session.commit()
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify(success=True)
    flash('Banco de matérias-primas salvo com sucesso!', 'success')
    return redirect(url_for('materias_primas.banco'))


@materias_primas_bp.route('/excluir/<int:id>', methods=['POST'])
@login_required
@admin_required
def excluir(id):
    mp = MateriaPrima.query.get_or_404(id)
    uso = ReceitaIngrediente.query.filter_by(ingrediente_nome=mp.nome).first()
    if uso:
        flash(f'Não é possível excluir "{mp.nome}": usado em receitas.', 'danger')
    else:
        db.session.delete(mp)
        db.session.commit()
        flash(f'"{mp.nome}" excluído com sucesso!', 'success')
    return redirect(url_for('materias_primas.banco'))


# ── Controle de Estoque ──

@materias_primas_bp.route('/estoque')
@login_required
@admin_required
def estoque():
    materias = MateriaPrima.query.order_by(MateriaPrima.nome).all()
    alertas = {a.materia_prima_id: a.estoque_minimo for a in AlertaEstoque.query.all()}
    return render_template('materias_primas/estoque.html', materias=materias, alertas=alertas)


@materias_primas_bp.route('/estoque/entrada', methods=['POST'])
@login_required
@admin_required
def estoque_entrada():
    mp_id = int(request.form['mp_id'])
    quantidade = float(request.form['quantidade'].replace(',', '.'))
    preco = request.form.get('preco_unitario', '').replace(',', '.')
    preco_unitario = float(preco) if preco else None
    referencia = request.form.get('referencia', '').strip()
    atualizar_custo = request.form.get('atualizar_custo') == '1'

    mp = MateriaPrima.query.get_or_404(mp_id)

    mov = MovimentacaoEstoque(
        materia_prima_id=mp_id,
        tipo='entrada',
        quantidade=quantidade,
        preco_unitario=preco_unitario,
        referencia=referencia or None,
        usuario_id=current_user.id,
    )
    db.session.add(mov)
    mp.estoque_atual = (mp.estoque_atual or 0) + quantidade

    if atualizar_custo and preco_unitario:
        mp.custo_por_kg = preco_unitario

    db.session.commit()
    flash(f'Entrada de {quantidade} {mp.unidade} de "{mp.nome}" registrada.', 'success')
    return redirect(url_for('materias_primas.estoque'))


@materias_primas_bp.route('/estoque/saida', methods=['POST'])
@login_required
@admin_required
def estoque_saida():
    mp_id = int(request.form['mp_id'])
    quantidade = float(request.form['quantidade'].replace(',', '.'))
    referencia = request.form.get('referencia', '').strip()

    mp = MateriaPrima.query.get_or_404(mp_id)

    mov = MovimentacaoEstoque(
        materia_prima_id=mp_id,
        tipo='saida',
        quantidade=quantidade,
        referencia=referencia or None,
        usuario_id=current_user.id,
    )
    db.session.add(mov)
    mp.estoque_atual = max(0, (mp.estoque_atual or 0) - quantidade)

    db.session.commit()
    flash(f'Saída de {quantidade} {mp.unidade} de "{mp.nome}" registrada.', 'success')
    return redirect(url_for('materias_primas.estoque'))


@materias_primas_bp.route('/estoque/<int:mp_id>/historico')
@login_required
@admin_required
def estoque_historico(mp_id):
    mp = MateriaPrima.query.get_or_404(mp_id)
    movimentacoes = MovimentacaoEstoque.query.filter_by(
        materia_prima_id=mp_id
    ).order_by(MovimentacaoEstoque.data.desc()).limit(100).all()
    return render_template('materias_primas/historico_mp.html', mp=mp, movimentacoes=movimentacoes)


@materias_primas_bp.route('/estoque/alertas', methods=['POST'])
@login_required
@admin_required
def estoque_alertas():
    mp_ids = request.form.getlist('mp_id[]')
    minimos = request.form.getlist('estoque_minimo[]')

    for i, mp_id in enumerate(mp_ids):
        valor = minimos[i].replace(',', '.').strip()
        if not valor or float(valor) <= 0:
            AlertaEstoque.query.filter_by(materia_prima_id=int(mp_id)).delete()
            continue
        alerta = AlertaEstoque.query.filter_by(materia_prima_id=int(mp_id)).first()
        if alerta:
            alerta.estoque_minimo = float(valor)
        else:
            alerta = AlertaEstoque(materia_prima_id=int(mp_id), estoque_minimo=float(valor))
            db.session.add(alerta)

    db.session.commit()
    flash('Alertas de estoque atualizados.', 'success')
    return redirect(url_for('materias_primas.estoque'))
