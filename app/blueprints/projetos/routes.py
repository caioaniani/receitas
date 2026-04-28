from datetime import date, datetime

from flask import render_template, request, jsonify, redirect, url_for, flash, abort
from flask_login import login_required, current_user

from app.blueprints.projetos import projetos_bp
from app.decorators import admin_required
from app.extensions import db
from app.models import ProjetoArea, Projeto, TarefaProjeto, Usuario


WIP_LIMIT = 3
TIPOS_AREA = ('empresa', 'igreja', 'vida')
STATUS_PROJETO = ('planejado', 'ativo', 'em_espera', 'concluido')
STATUS_TAREFA = ('a_fazer', 'fazendo', 'feito', 'cancelado')
PRIORIDADES = ('alta', 'media', 'baixa')


def _contadores():
    a_fazer = TarefaProjeto.query.filter_by(status='a_fazer').count()
    fazendo = TarefaProjeto.query.filter_by(status='fazendo').count()
    atrasadas = TarefaProjeto.query.filter(
        TarefaProjeto.prazo.isnot(None),
        TarefaProjeto.prazo < date.today(),
        ~TarefaProjeto.status.in_(['feito', 'cancelado']),
    ).count()
    foco_12s = Projeto.query.filter_by(foco_12s=True).count()
    return {
        'a_fazer': a_fazer,
        'fazendo': fazendo,
        'atrasadas': atrasadas,
        'foco_12s': foco_12s,
        'wip_limit': WIP_LIMIT,
        'wip_estourado': fazendo > WIP_LIMIT,
    }


def _areas_filtradas():
    return ProjetoArea.query.filter_by(ativa=True) \
        .order_by(ProjetoArea.ordem, ProjetoArea.nome).all()


def _usuarios():
    return Usuario.query.order_by(Usuario.nome).all()


# ── Views ──

@projetos_bp.route('/')
@login_required
@admin_required
def painel():
    areas = _areas_filtradas()
    return render_template('projetos/painel.html',
                           areas=areas,
                           contadores=_contadores(),
                           usuarios=_usuarios(),
                           view='hier')


@projetos_bp.route('/kanban')
@login_required
@admin_required
def kanban():
    filtro_area = request.args.get('area', type=int)
    so_foco = request.args.get('foco') == '1'

    q = TarefaProjeto.query.join(Projeto).join(ProjetoArea)
    if filtro_area:
        q = q.filter(Projeto.area_id == filtro_area)
    if so_foco:
        q = q.filter(Projeto.foco_12s.is_(True))

    tarefas = q.order_by(TarefaProjeto.prazo.is_(None), TarefaProjeto.prazo,
                         TarefaProjeto.ordem).all()

    colunas = {'a_fazer': [], 'fazendo': [], 'feito': []}
    for t in tarefas:
        if t.status in colunas:
            colunas[t.status].append(t)

    return render_template('projetos/kanban.html',
                           colunas=colunas,
                           areas=_areas_filtradas(),
                           filtro_area=filtro_area,
                           so_foco=so_foco,
                           contadores=_contadores(),
                           usuarios=_usuarios(),
                           view='kanban')


@projetos_bp.route('/foco')
@login_required
@admin_required
def foco():
    projetos = Projeto.query.filter_by(foco_12s=True).order_by(Projeto.nome).all()
    return render_template('projetos/foco.html',
                           projetos=projetos,
                           contadores=_contadores(),
                           usuarios=_usuarios(),
                           view='foco')


# ── CRUD: Áreas ──

@projetos_bp.route('/area/nova', methods=['POST'])
@login_required
@admin_required
def area_nova():
    nome = request.form.get('nome', '').strip()
    tipo = request.form.get('tipo', 'empresa')
    if not nome:
        flash('Informe o nome da area.', 'warning')
        return redirect(url_for('projetos.painel'))
    if tipo not in TIPOS_AREA:
        tipo = 'empresa'
    if ProjetoArea.query.filter_by(nome=nome).first():
        flash(f'Area "{nome}" ja existe.', 'warning')
        return redirect(url_for('projetos.painel'))
    db.session.add(ProjetoArea(nome=nome, tipo=tipo))
    db.session.commit()
    flash(f'Area "{nome}" criada.', 'success')
    return redirect(url_for('projetos.painel'))


@projetos_bp.route('/area/<int:area_id>/excluir', methods=['POST'])
@login_required
@admin_required
def area_excluir(area_id):
    area = ProjetoArea.query.get_or_404(area_id)
    if area.projetos:
        flash(f'Area "{area.nome}" tem projetos vinculados; remova-os antes.', 'warning')
        return redirect(url_for('projetos.painel'))
    db.session.delete(area)
    db.session.commit()
    flash('Area removida.', 'success')
    return redirect(url_for('projetos.painel'))


# ── CRUD: Projetos ──

@projetos_bp.route('/projeto/novo', methods=['POST'])
@login_required
@admin_required
def projeto_novo():
    area_id = request.form.get('area_id', type=int)
    nome = request.form.get('nome', '').strip()
    if not area_id or not nome:
        flash('Area e nome sao obrigatorios.', 'warning')
        return redirect(url_for('projetos.painel'))
    p = Projeto(
        area_id=area_id,
        nome=nome,
        status=request.form.get('status', 'planejado'),
        prioridade=request.form.get('prioridade') or None,
        foco_12s=request.form.get('foco_12s') == '1',
        responsavel_id=request.form.get('responsavel_id', type=int) or None,
    )
    db.session.add(p)
    db.session.commit()
    flash(f'Projeto "{nome}" criado.', 'success')
    return redirect(url_for('projetos.painel'))


@projetos_bp.route('/projeto/<int:pid>/editar', methods=['POST'])
@login_required
@admin_required
def projeto_editar(pid):
    p = Projeto.query.get_or_404(pid)
    campo = request.form.get('campo')
    valor = request.form.get('valor', '').strip()

    if campo == 'status' and valor in STATUS_PROJETO:
        p.status = valor
    elif campo == 'prioridade':
        p.prioridade = valor if valor in PRIORIDADES else None
    elif campo == 'foco_12s':
        p.foco_12s = valor in ('1', 'true', 'on')
    elif campo == 'responsavel_id':
        p.responsavel_id = int(valor) if valor else None
    elif campo == 'nome' and valor:
        p.nome = valor
    elif campo == 'area_id':
        p.area_id = int(valor) if valor else p.area_id
    elif campo == 'observacao':
        p.observacao = valor or None
    else:
        return jsonify(ok=False, erro='campo invalido'), 400

    db.session.commit()
    return jsonify(ok=True, contadores=_contadores())


@projetos_bp.route('/projeto/<int:pid>/excluir', methods=['POST'])
@login_required
@admin_required
def projeto_excluir(pid):
    p = Projeto.query.get_or_404(pid)
    nome = p.nome
    db.session.delete(p)
    db.session.commit()
    flash(f'Projeto "{nome}" excluido.', 'success')
    return redirect(url_for('projetos.painel'))


# ── CRUD: Tarefas ──

@projetos_bp.route('/tarefa/nova', methods=['POST'])
@login_required
@admin_required
def tarefa_nova():
    projeto_id = request.form.get('projeto_id', type=int)
    nome = request.form.get('nome', '').strip()
    if not projeto_id or not nome:
        flash('Projeto e nome sao obrigatorios.', 'warning')
        return redirect(request.referrer or url_for('projetos.painel'))

    prazo = None
    raw_prazo = request.form.get('prazo', '').strip()
    if raw_prazo:
        try:
            prazo = datetime.strptime(raw_prazo, '%Y-%m-%d').date()
        except ValueError:
            pass

    t = TarefaProjeto(
        projeto_id=projeto_id,
        nome=nome,
        status=request.form.get('status', 'a_fazer'),
        tipo=request.form.get('tipo') or None,
        esforco=request.form.get('esforco') or None,
        prazo=prazo,
        responsavel_id=request.form.get('responsavel_id', type=int) or None,
    )
    db.session.add(t)
    db.session.commit()
    flash(f'Tarefa "{nome}" adicionada.', 'success')
    return redirect(request.referrer or url_for('projetos.painel'))


@projetos_bp.route('/tarefa/<int:tid>/editar', methods=['POST'])
@login_required
@admin_required
def tarefa_editar(tid):
    t = TarefaProjeto.query.get_or_404(tid)
    campo = request.form.get('campo')
    valor = request.form.get('valor', '').strip()

    if campo == 'status' and valor in STATUS_TAREFA:
        t.status = valor
        t.feito_em = datetime.utcnow() if valor == 'feito' else None
    elif campo == 'tipo':
        t.tipo = valor or None
    elif campo == 'esforco':
        t.esforco = valor or None
    elif campo == 'prazo':
        if valor:
            try:
                t.prazo = datetime.strptime(valor, '%Y-%m-%d').date()
            except ValueError:
                return jsonify(ok=False, erro='data invalida'), 400
        else:
            t.prazo = None
    elif campo == 'responsavel_id':
        t.responsavel_id = int(valor) if valor else None
    elif campo == 'nome' and valor:
        t.nome = valor
    else:
        return jsonify(ok=False, erro='campo invalido'), 400

    db.session.commit()
    return jsonify(ok=True, contadores=_contadores())


@projetos_bp.route('/tarefa/<int:tid>/excluir', methods=['POST'])
@login_required
@admin_required
def tarefa_excluir(tid):
    t = TarefaProjeto.query.get_or_404(tid)
    db.session.delete(t)
    db.session.commit()
    flash('Tarefa removida.', 'success')
    return redirect(request.referrer or url_for('projetos.painel'))


# ── Weekly Review ──

@projetos_bp.route('/weekly')
@login_required
@admin_required
def weekly():
    """Dados pra modal de Weekly Review (atrasadas, sem DRI, projetos ativos sem tarefa, etc)."""
    hoje = date.today()
    atrasadas = TarefaProjeto.query.filter(
        TarefaProjeto.prazo.isnot(None),
        TarefaProjeto.prazo < hoje,
        ~TarefaProjeto.status.in_(['feito', 'cancelado']),
    ).all()
    projetos_ativos = Projeto.query.filter_by(status='ativo').all()
    sem_dri = [p for p in projetos_ativos if not p.responsavel_id]
    sem_tarefa = [p for p in projetos_ativos if not p.tarefas_ativas]
    foco = Projeto.query.filter_by(foco_12s=True).all()
    fazendo = TarefaProjeto.query.filter_by(status='fazendo').all()

    return jsonify(
        contadores=_contadores(),
        atrasadas=[{'id': t.id, 'nome': t.nome, 'projeto': t.projeto.nome,
                    'prazo': t.prazo.isoformat() if t.prazo else None} for t in atrasadas],
        sem_dri=[{'id': p.id, 'nome': p.nome} for p in sem_dri],
        sem_tarefa=[{'id': p.id, 'nome': p.nome} for p in sem_tarefa],
        foco=[{'id': p.id, 'nome': p.nome} for p in foco],
        fazendo=[{'id': t.id, 'nome': t.nome, 'projeto': t.projeto.nome} for t in fazendo],
    )
