"""Módulo de treinamento gamificado (24/07/2026, pedido do dono).

Esta fase = AUTORIA (admin): cria o treinamento, sobe o vídeo (self-host no
volume /data) e monta o quiz com nota de corte. A fase do FUNCIONÁRIO
(assistir + responder + pontuar + elegibilidade a sorteio/bônus) vem em
seguida. O vídeo é servido com HTTP Range pela MESMA origem — nada de
terceiro, o funcionário nunca sai do site.
"""
from flask import abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.blueprints.treinamento import treinamento_bp
from app.decorators import admin_required
from app.extensions import db
from app.models import Treinamento, TreinamentoOpcao, TreinamentoPergunta
from app.services import treinamento_video as tv
from app.utils import agora


def _ativos():
    return Treinamento.query.filter(Treinamento.apagado_em.is_(None))


# ── Admin: autoria ──────────────────────────────────────────────────────
@treinamento_bp.route('/admin')
@login_required
@admin_required
def admin_lista():
    treinos = _ativos().order_by(Treinamento.ordem, Treinamento.id).all()
    return render_template('treinamento/admin_lista.html', treinos=treinos)


@treinamento_bp.route('/admin/novo', methods=['POST'])
@login_required
@admin_required
def admin_novo():
    titulo = (request.form.get('titulo') or '').strip()[:200]
    if not titulo:
        flash('Dê um título ao treinamento.', 'warning')
        return redirect(url_for('treinamento.admin_lista'))
    ordem = (db.session.query(db.func.max(Treinamento.ordem)).scalar() or 0) + 1
    t = Treinamento(titulo=titulo, criado_por_id=current_user.id, ordem=ordem)
    db.session.add(t)
    db.session.commit()
    return redirect(url_for('treinamento.admin_editar', id=t.id))


@treinamento_bp.route('/admin/<int:id>')
@login_required
@admin_required
def admin_editar(id):
    t = _ativos().filter_by(id=id).first_or_404()
    return render_template('treinamento/admin_editar.html', t=t)


@treinamento_bp.route('/admin/<int:id>/salvar', methods=['POST'])
@login_required
@admin_required
def admin_salvar(id):
    t = _ativos().filter_by(id=id).first_or_404()
    titulo = (request.form.get('titulo') or '').strip()[:200]
    if titulo:
        t.titulo = titulo
    t.descricao = (request.form.get('descricao') or '').strip() or None
    try:
        nm = int(request.form.get('nota_minima') or 70)
    except (TypeError, ValueError):
        nm = 70
    t.nota_minima = max(0, min(100, nm))
    t.ativo = request.form.get('ativo') == '1'
    db.session.commit()
    flash('Treinamento salvo.', 'success')
    return redirect(url_for('treinamento.admin_editar', id=t.id))


@treinamento_bp.route('/admin/<int:id>/video', methods=['POST'])
@login_required
@admin_required
def admin_video(id):
    # Libera o teto de upload SÓ nesta rota (o global de 25 MB segue pras
    # fotos). request.max_content_length é setável por request no Werkzeug 3.
    request.max_content_length = current_app.config['TREINAMENTO_MAX_VIDEO']
    t = _ativos().filter_by(id=id).first_or_404()
    arq = request.files.get('video')
    if not arq or not arq.filename:
        flash('Escolha um arquivo de vídeo.', 'warning')
        return redirect(url_for('treinamento.admin_editar', id=t.id))
    try:
        ref = tv.salvar_video(arq, t.id)
    except ValueError as e:
        flash(str(e), 'danger')
        return redirect(url_for('treinamento.admin_editar', id=t.id))
    # Troca o vídeo: apaga o arquivo antigo (se era self-host) e aponta o novo.
    if t.video_tipo == 'arquivo' and t.video_ref:
        tv.remover_video(t.video_ref)
    t.video_tipo = 'arquivo'
    t.video_ref = ref
    db.session.commit()
    flash('Vídeo enviado.', 'success')
    return redirect(url_for('treinamento.admin_editar', id=t.id))


@treinamento_bp.route('/admin/<int:id>/pergunta', methods=['POST'])
@login_required
@admin_required
def admin_add_pergunta(id):
    t = _ativos().filter_by(id=id).first_or_404()
    enunciado = (request.form.get('enunciado') or '').strip()
    opcoes = [o.strip() for o in request.form.getlist('opcao[]') if o.strip()]
    try:
        correta_idx = int(request.form.get('correta'))
    except (TypeError, ValueError):
        correta_idx = -1
    if not enunciado or len(opcoes) < 2 or not (0 <= correta_idx < len(opcoes)):
        flash('A pergunta precisa de enunciado, ao menos 2 opções e a '
              'correta marcada.', 'warning')
        return redirect(url_for('treinamento.admin_editar', id=t.id))
    ordem = (db.session.query(db.func.max(TreinamentoPergunta.ordem))
             .filter_by(treinamento_id=t.id).scalar() or 0) + 1
    p = TreinamentoPergunta(treinamento_id=t.id, enunciado=enunciado,
                            ordem=ordem)
    db.session.add(p)
    db.session.flush()
    for i, texto in enumerate(opcoes):
        db.session.add(TreinamentoOpcao(
            pergunta_id=p.id, texto=texto[:500],
            correta=(i == correta_idx), ordem=i))
    db.session.commit()
    flash('Pergunta adicionada.', 'success')
    return redirect(url_for('treinamento.admin_editar', id=t.id))


@treinamento_bp.route('/admin/pergunta/<int:pid>/excluir', methods=['POST'])
@login_required
@admin_required
def admin_del_pergunta(pid):
    p = TreinamentoPergunta.query.get_or_404(pid)
    tid = p.treinamento_id
    db.session.delete(p)
    db.session.commit()
    flash('Pergunta removida.', 'success')
    return redirect(url_for('treinamento.admin_editar', id=tid))


@treinamento_bp.route('/admin/<int:id>/excluir', methods=['POST'])
@login_required
@admin_required
def admin_excluir(id):
    t = _ativos().filter_by(id=id).first_or_404()
    t.apagado_em = agora()
    db.session.commit()
    flash('Treinamento arquivado.', 'success')
    return redirect(url_for('treinamento.admin_lista'))


# ── Vídeo (serve com Range; admin preview + funcionário) ────────────────
@treinamento_bp.route('/video/<int:id>')
@login_required
def video(id):
    t = _ativos().filter_by(id=id).first_or_404()
    if t.video_tipo != 'arquivo' or not t.video_ref:
        abort(404)
    return tv.resposta_range(t.video_ref)
