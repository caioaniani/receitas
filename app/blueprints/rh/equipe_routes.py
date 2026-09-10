"""Visão macro e registro explícito do histórico de carreira (somente dono)."""
from datetime import datetime

from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.blueprints.rh import rh_bp
from app.decorators import owner_required
from app.extensions import db
from app.models import Funcionario


@rh_bp.route('/equipe')
@login_required
@owner_required
def equipe():
    from app.services.rh_equipe import carregar_visao
    filtros = {k: (request.args.get(k) or '').strip() for k in
               ('q', 'cargo', 'loja', 'lider', 'nivel', 'pendencia')}
    filtros['ativos'] = '0' if request.args.get('ativos') == '0' else '1'
    return render_template('rh/equipe.html', **carregar_visao(filtros))


@rh_bp.route('/funcionarios/<int:id>/carreira', methods=['GET', 'POST'])
@login_required
@owner_required
def carreira_funcionario(id):
    from app.models import RhMovimentacao
    from app.services import rh_movimentacao
    pessoa = Funcionario.query.get_or_404(id)
    if request.method == 'POST':
        try:
            try:
                data = datetime.strptime(request.form.get('data_efetiva', ''), '%Y-%m-%d').date()
            except ValueError:
                raise ValueError('Informe uma data válida para a promoção.') from None
            rh_movimentacao.registrar_promocao_historica(
                pessoa, data, actor_id=current_user.id,
                observacao=(request.form.get('observacao') or '').strip())
            db.session.commit()
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc) or 'Informe uma data válida para a promoção.', 'warning')
        else:
            flash('Data da promoção registrada. Cargo e salário não foram alterados.', 'success')
        return redirect(url_for('rh.carreira_funcionario', id=id))
    registros = (RhMovimentacao.query.filter_by(funcionario_id=id)
                 .order_by(RhMovimentacao.registrado_em.desc(),
                           RhMovimentacao.id.desc()).all())
    return render_template('rh/carreira_funcionario.html', pessoa=pessoa,
                           atual=rh_movimentacao.snapshot(pessoa), registros=registros)


@rh_bp.route('/cargos/unificar-atendentes', methods=['GET', 'POST'])
@login_required
@owner_required
def unificar_atendentes():
    from app.services import rh_cargos
    if request.method == 'POST':
        try:
            resultado = rh_cargos.unificar_atendentes(current_user.id, commit=True)
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'warning')
        else:
            if resultado.get('unificado'):
                flash('Atendente e Atendente 1 unificados, sem alterar salários.', 'success')
            elif resultado.get('ja_unificado'):
                flash('Os cargos já estão unificados. Nada foi alterado.', 'info')
            else:
                flash('Não há cargos Atendente para unificar.', 'info')
        return redirect(url_for('rh.unificar_atendentes'))
    return render_template('rh/unificar_atendentes.html',
                           previa=rh_cargos.resumo_unificacao_atendentes())
