"""Avisos pra producao: qualquer usuario logado posta um recado que aparece
na TV do padeiro (/padeiro). Tela simples — escreve e envia."""
from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.blueprints.avisos import avisos_bp
from app.extensions import db
from app.models import Aviso


@avisos_bp.route('/', methods=['GET', 'POST'])
@login_required
def index():
    if request.method == 'POST':
        texto = (request.form.get('texto') or '').strip()[:500]
        if texto:
            db.session.add(Aviso(texto=texto, criado_por_id=current_user.id))
            db.session.commit()
            flash('Aviso enviado para a producao — vai aparecer na TV.', 'success')
        else:
            flash('Escreva o aviso antes de enviar.', 'warning')
        return redirect(url_for('avisos.index'))
    recentes = (Aviso.query.order_by(Aviso.criado_em.desc()).limit(30).all())
    return render_template('avisos/index.html', recentes=recentes)
