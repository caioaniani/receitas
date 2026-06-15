"""Memoria persistente do agente — CRUD admin.

UI simples pra ver/editar/arquivar as notas que o copilot acumulou nas
sessoes. Mesmo dado consumido pelo copilot (`consultar_notas`) e pelo bot
Padeiro do Chatwoot. Decisao do dono 15/06/2026.

Permissoes:
- Listar/buscar: qualquer usuario logado (todos precisam consultar regras)
- Editar/arquivar/criar: admin+owner (regra de negocio = decisao critica)
"""
from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.blueprints.notas import notas_bp
from app.decorators import admin_required
from app.models import Nota
from app.services import notas as notas_svc


@notas_bp.route('/', methods=['GET'])
@login_required
def index():
    """Lista notas (busca opcional, soft-filter por arquivadas)."""
    termo = (request.args.get('q') or '').strip()
    mostrar_arquivadas = request.args.get('arquivadas') == '1'
    if termo:
        encontradas = notas_svc.buscar(termo, limite=200)
    else:
        q = Nota.query
        if not mostrar_arquivadas:
            q = q.filter(Nota.arquivada_em.is_(None))
        encontradas = q.order_by(Nota.criada_em.desc()).limit(200).all()
    # Se admin pediu arquivadas, junta as ativas tambem (o filtro acima ja
    # nao excluiu); se nao pediu, a busca por termo pode ter pego notas
    # arquivadas (busca() filtra). Logica simples — index nao tenta cobrir
    # todos os casos extremos.
    return render_template('notas/index.html',
                            notas=encontradas, termo=termo,
                            mostrar_arquivadas=mostrar_arquivadas)


@notas_bp.route('/nova', methods=['GET', 'POST'])
@login_required
@admin_required
def nova():
    if request.method == 'POST':
        titulo = (request.form.get('titulo') or '').strip()
        conteudo = (request.form.get('conteudo') or '').strip()
        tags = (request.form.get('tags') or '').strip()
        if not titulo or not conteudo:
            flash('Título e conteúdo são obrigatórios.', 'warning')
            return render_template('notas/edit.html', nota=None,
                                    titulo=titulo, conteudo=conteudo, tags=tags)
        notas_svc.registrar(titulo, conteudo, tags=tags,
                            origem='admin', criada_por_id=current_user.id)
        flash('Nota criada.', 'success')
        return redirect(url_for('notas.index'))
    return render_template('notas/edit.html', nota=None,
                            titulo='', conteudo='', tags='')


@notas_bp.route('/<int:nid>/editar', methods=['GET', 'POST'])
@login_required
@admin_required
def editar(nid):
    n = Nota.query.get_or_404(nid)
    if request.method == 'POST':
        titulo = (request.form.get('titulo') or '').strip()
        conteudo = (request.form.get('conteudo') or '').strip()
        tags = (request.form.get('tags') or '').strip()
        if not titulo or not conteudo:
            flash('Título e conteúdo são obrigatórios.', 'warning')
        else:
            notas_svc.atualizar(nid, titulo=titulo, conteudo=conteudo, tags=tags)
            flash('Nota atualizada.', 'success')
            return redirect(url_for('notas.index'))
    return render_template('notas/edit.html', nota=n,
                            titulo=n.titulo, conteudo=n.conteudo, tags=n.tags)


@notas_bp.route('/<int:nid>/arquivar', methods=['POST'])
@login_required
@admin_required
def arquivar(nid):
    notas_svc.arquivar(nid)
    flash('Nota arquivada — não vai mais aparecer pra o copilot/bot.', 'info')
    return redirect(url_for('notas.index'))


@notas_bp.route('/<int:nid>/restaurar', methods=['POST'])
@login_required
@admin_required
def restaurar(nid):
    notas_svc.restaurar(nid)
    flash('Nota restaurada.', 'success')
    return redirect(url_for('notas.index', arquivadas=1))
