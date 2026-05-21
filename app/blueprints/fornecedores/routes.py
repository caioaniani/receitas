"""CRUD de Fornecedores + historico de preco de MP por fornecedor."""

from flask import current_app, flash, redirect, render_template, request, url_for
from flask_login import login_required

from app.blueprints.fornecedores import fornecedores_bp
from app.decorators import catalogo_required
from app.extensions import db
from app.models import Fornecedor, HistoricoPrecoMP, MateriaPrima


@fornecedores_bp.route('/')
@login_required
@catalogo_required
def lista():
    incluir_inativos = request.args.get('inativos') == '1'
    q = Fornecedor.query
    if not incluir_inativos:
        q = q.filter_by(ativo=True)
    fornecedores = q.order_by(Fornecedor.nome).all()
    return render_template('fornecedores/lista.html', fornecedores=fornecedores,
                           incluir_inativos=incluir_inativos)


@fornecedores_bp.route('/novo', methods=['GET', 'POST'])
@login_required
@catalogo_required
def novo():
    if request.method == 'POST':
        nome = (request.form.get('nome') or '').strip()
        if not nome:
            flash('Nome obrigatorio.', 'warning')
            return redirect(url_for('fornecedores.novo'))
        if Fornecedor.query.filter_by(nome=nome).first():
            flash('Ja existe fornecedor com esse nome.', 'warning')
            return redirect(url_for('fornecedores.novo'))
        try:
            f = Fornecedor(
                nome=nome,
                cnpj=(request.form.get('cnpj') or '').strip() or None,
                telefone=(request.form.get('telefone') or '').strip() or None,
                email=(request.form.get('email') or '').strip() or None,
                contato=(request.form.get('contato') or '').strip() or None,
                observacao=(request.form.get('observacao') or '').strip() or None,
            )
            db.session.add(f)
            db.session.commit()
        except Exception as exc:  # noqa: BLE001
            db.session.rollback()
            current_app.logger.exception('Falha ao criar fornecedor')
            flash(f'Erro: {exc}', 'danger')
            return redirect(url_for('fornecedores.novo'))
        flash('Fornecedor criado.', 'success')
        return redirect(url_for('fornecedores.detalhe', id=f.id))
    return render_template('fornecedores/form.html', fornecedor=None)


@fornecedores_bp.route('/<int:id>')
@login_required
@catalogo_required
def detalhe(id):
    f = Fornecedor.query.get_or_404(id)
    historico = (HistoricoPrecoMP.query
                 .filter_by(fornecedor_id=id)
                 .order_by(HistoricoPrecoMP.data.desc())
                 .limit(50).all())
    # Resumo: MPs distintas + total gasto
    mps_distintas = {h.materia_prima_id for h in historico}
    total_gasto = sum((h.preco_unitario or 0) * (h.quantidade or 0) for h in historico)
    return render_template('fornecedores/detalhe.html', fornecedor=f,
                           historico=historico, mps_distintas=len(mps_distintas),
                           total_gasto=total_gasto)


@fornecedores_bp.route('/<int:id>/editar', methods=['GET', 'POST'])
@login_required
@catalogo_required
def editar(id):
    f = Fornecedor.query.get_or_404(id)
    if request.method == 'POST':
        nome = (request.form.get('nome') or '').strip()
        if not nome:
            flash('Nome obrigatorio.', 'warning')
            return redirect(url_for('fornecedores.editar', id=id))
        try:
            f.nome = nome
            f.cnpj = (request.form.get('cnpj') or '').strip() or None
            f.telefone = (request.form.get('telefone') or '').strip() or None
            f.email = (request.form.get('email') or '').strip() or None
            f.contato = (request.form.get('contato') or '').strip() or None
            f.observacao = (request.form.get('observacao') or '').strip() or None
            f.ativo = bool(request.form.get('ativo'))
            db.session.commit()
        except Exception as exc:  # noqa: BLE001
            db.session.rollback()
            current_app.logger.exception('Falha ao editar fornecedor %s', id)
            flash(f'Erro: {exc}', 'danger')
            return redirect(url_for('fornecedores.editar', id=id))
        flash('Atualizado.', 'success')
        return redirect(url_for('fornecedores.detalhe', id=id))
    return render_template('fornecedores/form.html', fornecedor=f)


@fornecedores_bp.route('/<int:id>/excluir', methods=['POST'])
@login_required
@catalogo_required
def excluir(id):
    f = Fornecedor.query.get_or_404(id)
    # Se tiver histórico, só desativa (preserva auditoria)
    tem_historico = HistoricoPrecoMP.query.filter_by(fornecedor_id=id).first()
    if tem_historico:
        f.ativo = False
        db.session.commit()
        flash('Fornecedor tem historico de compras — apenas desativado.', 'info')
    else:
        db.session.delete(f)
        db.session.commit()
        flash('Fornecedor excluido.', 'success')
    return redirect(url_for('fornecedores.lista'))


@fornecedores_bp.route('/comparar/<int:mp_id>')
@login_required
@catalogo_required
def comparar_precos(mp_id):
    """Compara historico de preco de uma MP entre fornecedores."""
    mp = MateriaPrima.query.get_or_404(mp_id)
    # Agrupa por fornecedor: ultimo preco + media
    historico = (HistoricoPrecoMP.query
                 .filter_by(materia_prima_id=mp_id)
                 .order_by(HistoricoPrecoMP.data.desc())
                 .limit(200).all())
    por_fornecedor = {}
    for h in historico:
        fid = h.fornecedor_id
        if fid not in por_fornecedor:
            por_fornecedor[fid] = {
                'fornecedor': h.fornecedor, 'ultimo_preco': h.preco_unitario,
                'ultima_data': h.data, 'total_qtd': 0, 'precos': [],
            }
        por_fornecedor[fid]['total_qtd'] += h.quantidade or 0
        por_fornecedor[fid]['precos'].append(h.preco_unitario or 0)
    for fid, d in por_fornecedor.items():
        d['preco_medio'] = sum(d['precos']) / len(d['precos']) if d['precos'] else 0
        d['min_preco'] = min(d['precos']) if d['precos'] else 0
        d['max_preco'] = max(d['precos']) if d['precos'] else 0
    return render_template('fornecedores/comparar.html', mp=mp,
                           por_fornecedor=list(por_fornecedor.values()),
                           historico=historico)
