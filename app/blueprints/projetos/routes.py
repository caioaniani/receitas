from datetime import date, datetime, timedelta

from flask import render_template, request, jsonify, redirect, url_for, flash, abort
from flask_login import login_required, current_user

from app.blueprints.projetos import projetos_bp
from app.decorators import admin_required
from app.extensions import db
from app.models import (ProjetoArea, Projeto, TarefaProjeto, Usuario, WeeklyReview)


WIP_LIMIT = 3
TIPOS_AREA = ('empresa', 'igreja', 'vida')
STATUS_PROJETO = ('planejado', 'ativo', 'em_espera', 'concluido')
STATUS_TAREFA = ('a_fazer', 'fazendo', 'feito', 'cancelado')
PRIORIDADES = ('alta', 'media', 'baixa')


def _contadores():
    base_t = TarefaProjeto.query.join(Projeto).join(ProjetoArea)
    base_p = Projeto.query.join(ProjetoArea)
    if not current_user.is_dono():
        base_t = base_t.filter(ProjetoArea.tipo == 'empresa')
        base_p = base_p.filter(ProjetoArea.tipo == 'empresa')

    a_fazer = base_t.filter(TarefaProjeto.status == 'a_fazer').count()
    fazendo = base_t.filter(TarefaProjeto.status == 'fazendo').count()
    atrasadas = base_t.filter(
        TarefaProjeto.prazo.isnot(None),
        TarefaProjeto.prazo < date.today(),
        ~TarefaProjeto.status.in_(['feito', 'cancelado']),
    ).count()
    foco_12s = base_p.filter(Projeto.foco_12s.is_(True)).count()
    return {
        'a_fazer': a_fazer,
        'fazendo': fazendo,
        'atrasadas': atrasadas,
        'foco_12s': foco_12s,
        'wip_limit': WIP_LIMIT,
        'wip_estourado': fazendo > WIP_LIMIT,
    }


def _areas_filtradas():
    """Retorna areas visiveis para o usuario atual.
    Areas tipo 'vida' e 'igreja' so para o owner; admins comuns veem so 'empresa'.
    """
    q = ProjetoArea.query.filter_by(ativa=True)
    if not current_user.is_dono():
        q = q.filter(ProjetoArea.tipo == 'empresa')
    return q.order_by(ProjetoArea.ordem, ProjetoArea.nome).all()


def _projeto_visivel(projeto):
    if current_user.is_dono():
        return True
    return projeto.area.tipo == 'empresa'


def _tarefa_visivel(tarefa):
    return _projeto_visivel(tarefa.projeto)


def _usuarios():
    return Usuario.query.order_by(Usuario.nome).all()


def _data_relativa(prazo):
    """Retorna string amigavel: 'Hoje', 'Amanha', 'Em 3 dias', 'Atrasada ha 2 dias'."""
    if not prazo:
        return ''
    delta = (prazo - date.today()).days
    if delta == 0:
        return 'Hoje'
    if delta == 1:
        return 'Amanha'
    if delta == -1:
        return 'Atrasada ha 1 dia'
    if delta < 0:
        return f'Atrasada ha {abs(delta)} dias'
    if delta < 7:
        return f'Em {delta} dias'
    if delta < 14:
        return f'Em 1 semana'
    if delta < 30:
        return f'Em {delta // 7} semanas'
    return prazo.strftime('%d/%m/%Y')


# ── Views ──

@projetos_bp.route('/')
@login_required
@admin_required
def painel():
    from app.models import ProjetoTemplate
    areas = _areas_filtradas()
    templates_disponiveis = ProjetoTemplate.query.order_by(ProjetoTemplate.nome).all()
    return render_template('projetos/painel.html',
                           areas=areas,
                           contadores=_contadores(),
                           usuarios=_usuarios(),
                           templates_disponiveis=templates_disponiveis,
                           data_relativa=_data_relativa,
                           view='hier')


@projetos_bp.route('/kanban')
@login_required
@admin_required
def kanban():
    filtro_area = request.args.get('area', type=int)
    so_foco = request.args.get('foco') == '1'
    incluir_canceladas = request.args.get('canceladas') == '1'

    q = TarefaProjeto.query.join(Projeto).join(ProjetoArea)
    if not current_user.is_dono():
        q = q.filter(ProjetoArea.tipo == 'empresa')
    if filtro_area:
        q = q.filter(Projeto.area_id == filtro_area)
    if so_foco:
        q = q.filter(Projeto.foco_12s.is_(True))

    tarefas = q.order_by(TarefaProjeto.prazo.is_(None), TarefaProjeto.prazo,
                         TarefaProjeto.ordem).all()

    if incluir_canceladas:
        colunas = {'a_fazer': [], 'fazendo': [], 'feito': [], 'cancelado': []}
    else:
        colunas = {'a_fazer': [], 'fazendo': [], 'feito': []}
    for t in tarefas:
        if t.status in colunas:
            colunas[t.status].append(t)

    return render_template('projetos/kanban.html',
                           colunas=colunas,
                           areas=_areas_filtradas(),
                           filtro_area=filtro_area,
                           so_foco=so_foco,
                           incluir_canceladas=incluir_canceladas,
                           contadores=_contadores(),
                           usuarios=_usuarios(),
                           data_relativa=_data_relativa,
                           view='kanban')


@projetos_bp.route('/foco')
@login_required
@admin_required
def foco():
    q = Projeto.query.filter_by(foco_12s=True).join(ProjetoArea)
    if not current_user.is_dono():
        q = q.filter(ProjetoArea.tipo == 'empresa')
    projetos = q.order_by(Projeto.nome).all()
    return render_template('projetos/foco.html',
                           projetos=projetos,
                           contadores=_contadores(),
                           usuarios=_usuarios(),
                           data_relativa=_data_relativa,
                           view='foco')


@projetos_bp.route('/hoje')
@login_required
@admin_required
def hoje():
    hoje_d = date.today()

    base = TarefaProjeto.query.join(Projeto).join(ProjetoArea)
    if not current_user.is_dono():
        base = base.filter(ProjetoArea.tipo == 'empresa')

    fazendo = base.filter(TarefaProjeto.status == 'fazendo') \
        .order_by(TarefaProjeto.prazo.is_(None), TarefaProjeto.prazo).all()

    prazo_hoje = base.filter(
        TarefaProjeto.prazo == hoje_d,
        TarefaProjeto.status == 'a_fazer',
    ).all()

    atrasadas = base.filter(
        TarefaProjeto.prazo.isnot(None),
        TarefaProjeto.prazo < hoje_d,
        ~TarefaProjeto.status.in_(['feito', 'cancelado']),
    ).order_by(TarefaProjeto.prazo).all()

    semana = base.filter(
        TarefaProjeto.prazo.isnot(None),
        TarefaProjeto.prazo > hoje_d,
        TarefaProjeto.prazo <= hoje_d + timedelta(days=7),
        ~TarefaProjeto.status.in_(['feito', 'cancelado']),
    ).order_by(TarefaProjeto.prazo).all()

    return render_template('projetos/hoje.html',
                           fazendo=fazendo,
                           prazo_hoje=prazo_hoje,
                           atrasadas=atrasadas,
                           semana=semana,
                           contadores=_contadores(),
                           usuarios=_usuarios(),
                           data_relativa=_data_relativa,
                           view='hoje')


# ── CRUD: Áreas ──

@projetos_bp.route('/area/nova', methods=['POST'])
@login_required
@admin_required
def area_nova():
    nome = request.form.get('nome', '').strip()
    tipo = request.form.get('tipo', 'empresa')
    cor = request.form.get('cor', '').strip() or None
    if not nome:
        flash('Informe o nome da area.', 'warning')
        return redirect(url_for('projetos.painel'))
    if tipo not in TIPOS_AREA:
        tipo = 'empresa'
    # Apenas owner pode criar areas privadas (vida/igreja)
    if tipo in ('vida', 'igreja') and not current_user.is_dono():
        tipo = 'empresa'
    if ProjetoArea.query.filter_by(nome=nome).first():
        flash(f'Area "{nome}" ja existe.', 'warning')
        return redirect(url_for('projetos.painel'))
    db.session.add(ProjetoArea(nome=nome, tipo=tipo, cor=cor))
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
    if not _projeto_visivel(p):
        abort(403)
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
    if not _projeto_visivel(p):
        abort(403)
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

    proj_alvo = Projeto.query.get(projeto_id)
    if not proj_alvo or not _projeto_visivel(proj_alvo):
        abort(403)

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
    if not _tarefa_visivel(t):
        abort(403)
    campo = request.form.get('campo')
    valor = request.form.get('valor', '').strip()

    if campo == 'status' and valor in STATUS_TAREFA:
        antigo = t.status
        t.status = valor
        t.feito_em = datetime.utcnow() if valor == 'feito' else None
        # Recorrencia: ao marcar como feita, cria proxima ocorrencia
        if valor == 'feito' and antigo != 'feito' and t.recorrencia:
            _agendar_proxima(t)
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
    elif campo == 'observacao':
        t.observacao = valor or None
    elif campo == 'recorrencia':
        t.recorrencia = valor or None
    else:
        return jsonify(ok=False, erro='campo invalido'), 400

    db.session.commit()
    return jsonify(ok=True, contadores=_contadores())


@projetos_bp.route('/tarefa/<int:tid>/mover', methods=['POST'])
@login_required
@admin_required
def tarefa_mover(tid):
    """Drag-and-drop: muda status (coluna kanban) e atualiza ordens das tarefas afetadas."""
    t = TarefaProjeto.query.get_or_404(tid)
    novo_status = request.form.get('status', '').strip()
    if novo_status not in STATUS_TAREFA:
        return jsonify(ok=False, erro='status invalido'), 400
    if t.status != novo_status:
        t.status = novo_status
        t.feito_em = datetime.utcnow() if novo_status == 'feito' else None

    # Reordena: a lista vem como ids[]=[...] na ordem desejada na coluna de destino
    ids_ordem = request.form.getlist('ids[]')
    for i, sid in enumerate(ids_ordem):
        try:
            tar = TarefaProjeto.query.get(int(sid))
            if tar:
                tar.ordem = i
        except (TypeError, ValueError):
            continue

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
    """Dados pra modal de Weekly Review."""
    hoje_d = date.today()
    atrasadas = TarefaProjeto.query.filter(
        TarefaProjeto.prazo.isnot(None),
        TarefaProjeto.prazo < hoje_d,
        ~TarefaProjeto.status.in_(['feito', 'cancelado']),
    ).all()
    projetos_ativos = Projeto.query.filter_by(status='ativo').all()
    sem_dri = [p for p in projetos_ativos if not p.responsavel_id]
    sem_tarefa = [p for p in projetos_ativos if not p.tarefas_ativas]
    foco = Projeto.query.filter_by(foco_12s=True).all()
    fazendo = TarefaProjeto.query.filter_by(status='fazendo').all()

    historico = WeeklyReview.query.order_by(WeeklyReview.data.desc()).limit(8).all()

    return jsonify(
        contadores=_contadores(),
        atrasadas=[{'id': t.id, 'nome': t.nome, 'projeto': t.projeto.nome,
                    'prazo': t.prazo.isoformat() if t.prazo else None,
                    'relativa': _data_relativa(t.prazo)} for t in atrasadas],
        sem_dri=[{'id': p.id, 'nome': p.nome} for p in sem_dri],
        sem_tarefa=[{'id': p.id, 'nome': p.nome} for p in sem_tarefa],
        foco=[{'id': p.id, 'nome': p.nome} for p in foco],
        fazendo=[{'id': t.id, 'nome': t.nome, 'projeto': t.projeto.nome} for t in fazendo],
        historico=[{
            'id': r.id, 'data': r.data.isoformat(),
            'reflexao': r.reflexao or '',
            'snapshot': {
                'fazendo': r.fazendo_count, 'a_fazer': r.a_fazer_count,
                'atrasadas': r.atrasadas_count, 'foco': r.foco_count,
            },
            'autor': r.autor.nome if r.autor else None,
        } for r in historico],
    )


@projetos_bp.route('/weekly/salvar', methods=['POST'])
@login_required
@admin_required
def weekly_salvar():
    reflexao = request.form.get('reflexao', '').strip()
    if not reflexao:
        return jsonify(ok=False, erro='reflexao vazia'), 400
    c = _contadores()
    review = WeeklyReview(
        data=date.today(),
        reflexao=reflexao,
        fazendo_count=c['fazendo'],
        a_fazer_count=c['a_fazer'],
        atrasadas_count=c['atrasadas'],
        foco_count=c['foco_12s'],
        criado_por=current_user.id,
    )
    db.session.add(review)
    db.session.commit()
    return jsonify(ok=True, id=review.id)


# ── Recorrencia ──

_RECORRENCIA_DIAS = {
    'diaria': 1,
    'semanal': 7,
    'quinzenal': 14,
    'mensal': 30,
    'trimestral': 90,
}


def _agendar_proxima(tarefa):
    """Cria nova ocorrencia da tarefa recorrente, com prazo deslocado."""
    dias = _RECORRENCIA_DIAS.get(tarefa.recorrencia)
    if not dias:
        return
    base = tarefa.prazo or date.today()
    nova = TarefaProjeto(
        projeto_id=tarefa.projeto_id,
        nome=tarefa.nome,
        status='a_fazer',
        tipo=tarefa.tipo,
        esforco=tarefa.esforco,
        prazo=base + timedelta(days=dias),
        responsavel_id=tarefa.responsavel_id,
        observacao=tarefa.observacao,
        recorrencia=tarefa.recorrencia,
        ordem=tarefa.ordem,
    )
    db.session.add(nova)


# ── Templates de Projeto ──

@projetos_bp.route('/templates')
@login_required
@admin_required
def templates_lista():
    from app.models import ProjetoTemplate
    templates = ProjetoTemplate.query.order_by(ProjetoTemplate.nome).all()
    return render_template('projetos/templates.html',
                           templates=templates,
                           areas=_areas_filtradas(),
                           contadores=_contadores(),
                           view='templates')


@projetos_bp.route('/templates/novo', methods=['POST'])
@login_required
@admin_required
def template_novo():
    from app.models import ProjetoTemplate
    nome = request.form.get('nome', '').strip()
    if not nome:
        flash('Informe o nome do template.', 'warning')
        return redirect(url_for('projetos.templates_lista'))
    t = ProjetoTemplate(
        nome=nome,
        area_id_padrao=request.form.get('area_id_padrao', type=int) or None,
        descricao=request.form.get('descricao', '').strip() or None,
    )
    db.session.add(t)
    db.session.commit()
    flash(f'Template "{nome}" criado.', 'success')
    return redirect(url_for('projetos.template_editar', tid=t.id))


@projetos_bp.route('/templates/<int:tid>')
@login_required
@admin_required
def template_editar(tid):
    from app.models import ProjetoTemplate
    template = ProjetoTemplate.query.get_or_404(tid)
    return render_template('projetos/template_editar.html',
                           template=template,
                           areas=_areas_filtradas(),
                           contadores=_contadores(),
                           view='templates')


@projetos_bp.route('/templates/<int:tid>/atualizar', methods=['POST'])
@login_required
@admin_required
def template_atualizar(tid):
    from app.models import ProjetoTemplate
    template = ProjetoTemplate.query.get_or_404(tid)
    template.nome = request.form.get('nome', template.nome).strip() or template.nome
    template.area_id_padrao = request.form.get('area_id_padrao', type=int) or None
    template.descricao = request.form.get('descricao', '').strip() or None
    db.session.commit()
    flash('Template atualizado.', 'success')
    return redirect(url_for('projetos.template_editar', tid=tid))


@projetos_bp.route('/templates/<int:tid>/excluir', methods=['POST'])
@login_required
@admin_required
def template_excluir(tid):
    from app.models import ProjetoTemplate
    template = ProjetoTemplate.query.get_or_404(tid)
    db.session.delete(template)
    db.session.commit()
    flash('Template removido.', 'success')
    return redirect(url_for('projetos.templates_lista'))


@projetos_bp.route('/templates/<int:tid>/tarefa', methods=['POST'])
@login_required
@admin_required
def template_tarefa_nova(tid):
    from app.models import ProjetoTemplate, TarefaTemplate
    template = ProjetoTemplate.query.get_or_404(tid)
    nome = request.form.get('nome', '').strip()
    if not nome:
        flash('Informe o nome.', 'warning')
        return redirect(url_for('projetos.template_editar', tid=tid))
    dias_raw = request.form.get('dias_prazo', '').strip()
    dias = None
    if dias_raw:
        try:
            dias = int(dias_raw)
        except ValueError:
            pass
    ordem = (max([t.ordem for t in template.tarefas] + [0]) + 1) if template.tarefas else 1
    tt = TarefaTemplate(
        template_id=template.id,
        nome=nome,
        tipo=request.form.get('tipo') or None,
        esforco=request.form.get('esforco') or None,
        dias_prazo=dias,
        ordem=ordem,
    )
    db.session.add(tt)
    db.session.commit()
    return redirect(url_for('projetos.template_editar', tid=tid))


@projetos_bp.route('/templates/tarefa/<int:tt_id>/excluir', methods=['POST'])
@login_required
@admin_required
def template_tarefa_excluir(tt_id):
    from app.models import TarefaTemplate
    tt = TarefaTemplate.query.get_or_404(tt_id)
    template_id = tt.template_id
    db.session.delete(tt)
    db.session.commit()
    return redirect(url_for('projetos.template_editar', tid=template_id))


@projetos_bp.route('/projeto/novo-de-template', methods=['POST'])
@login_required
@admin_required
def projeto_novo_de_template():
    from app.models import ProjetoTemplate
    template_id = request.form.get('template_id', type=int)
    nome = request.form.get('nome', '').strip()
    area_id = request.form.get('area_id', type=int)
    if not template_id or not nome or not area_id:
        flash('Template, area e nome sao obrigatorios.', 'warning')
        return redirect(url_for('projetos.painel'))
    template = ProjetoTemplate.query.get_or_404(template_id)
    area = ProjetoArea.query.get_or_404(area_id)
    if not current_user.is_dono() and area.tipo != 'empresa':
        abort(403)

    p = Projeto(area_id=area_id, nome=nome, status='ativo',
                responsavel_id=request.form.get('responsavel_id', type=int) or None)
    db.session.add(p)
    db.session.flush()

    base_data = date.today()
    for tt in sorted(template.tarefas, key=lambda x: x.ordem):
        prazo = base_data + timedelta(days=tt.dias_prazo) if tt.dias_prazo is not None else None
        db.session.add(TarefaProjeto(
            projeto_id=p.id,
            nome=tt.nome,
            status='a_fazer',
            tipo=tt.tipo,
            esforco=tt.esforco,
            prazo=prazo,
            ordem=tt.ordem,
        ))
    db.session.commit()
    flash(f'Projeto "{nome}" criado a partir de "{template.nome}" com {len(template.tarefas)} tarefas.', 'success')
    return redirect(url_for('projetos.painel'))


# ── Calendario ──

@projetos_bp.route('/calendario')
@login_required
@admin_required
def calendario():
    ano = request.args.get('ano', type=int) or date.today().year
    mes = request.args.get('mes', type=int) or date.today().month

    if mes < 1: mes, ano = 12, ano - 1
    if mes > 12: mes, ano = 1, ano + 1

    primeiro = date(ano, mes, 1)
    if mes == 12:
        proximo_mes = date(ano + 1, 1, 1)
    else:
        proximo_mes = date(ano, mes + 1, 1)
    ultimo_dia = (proximo_mes - timedelta(days=1)).day

    base = TarefaProjeto.query.join(Projeto).join(ProjetoArea) \
        .filter(TarefaProjeto.prazo >= primeiro,
                TarefaProjeto.prazo < proximo_mes)
    if not current_user.is_dono():
        base = base.filter(ProjetoArea.tipo == 'empresa')
    tarefas = base.order_by(TarefaProjeto.prazo).all()

    por_dia = {}
    for t in tarefas:
        if t.prazo:
            por_dia.setdefault(t.prazo.day, []).append(t)

    # Calcula primeira semana (segunda=0)
    primeiro_dia_semana = primeiro.weekday()  # 0=seg

    return render_template('projetos/calendario.html',
                           ano=ano, mes=mes,
                           primeiro_dia_semana=primeiro_dia_semana,
                           ultimo_dia=ultimo_dia,
                           por_dia=por_dia,
                           hoje=date.today(),
                           contadores=_contadores(),
                           view='calendario')


# ── Relatorio de produtividade ──

@projetos_bp.route('/relatorio')
@login_required
@admin_required
def relatorio():
    hoje_d = date.today()
    de_str = request.args.get('de', '')
    ate_str = request.args.get('ate', '')
    try:
        de = datetime.strptime(de_str, '%Y-%m-%d').date()
    except ValueError:
        de = hoje_d.replace(day=1)
    try:
        ate = datetime.strptime(ate_str, '%Y-%m-%d').date()
    except ValueError:
        ate = hoje_d

    areas = _areas_filtradas()
    dados = []
    total_concluidas = 0
    total_tarefas = 0

    for area in areas:
        tarefas_q = TarefaProjeto.query.join(Projeto) \
            .filter(Projeto.area_id == area.id,
                    TarefaProjeto.criado_em >= datetime.combine(de, datetime.min.time()),
                    TarefaProjeto.criado_em <= datetime.combine(ate, datetime.max.time()))
        total = tarefas_q.count()
        concl = tarefas_q.filter(TarefaProjeto.status == 'feito').count()
        atras = tarefas_q.filter(
            TarefaProjeto.prazo.isnot(None),
            TarefaProjeto.prazo < hoje_d,
            ~TarefaProjeto.status.in_(['feito', 'cancelado']),
        ).count()
        canc = tarefas_q.filter(TarefaProjeto.status == 'cancelado').count()
        ativos = sum(1 for p in area.projetos if p.status == 'ativo')

        pct = round(100 * concl / total) if total else 0
        dados.append({
            'area': area,
            'total': total,
            'concluidas': concl,
            'atrasadas': atras,
            'canceladas': canc,
            'ativos': ativos,
            'pct': pct,
        })
        total_concluidas += concl
        total_tarefas += total

    pct_geral = round(100 * total_concluidas / total_tarefas) if total_tarefas else 0

    return render_template('projetos/relatorio.html',
                           dados=dados,
                           de=de.isoformat(), ate=ate.isoformat(),
                           total_concluidas=total_concluidas,
                           total_tarefas=total_tarefas,
                           pct_geral=pct_geral,
                           contadores=_contadores(),
                           view='relatorio')


# ── Edicao de cor de area ──

@projetos_bp.route('/area/<int:area_id>/editar', methods=['POST'])
@login_required
@admin_required
def area_editar(area_id):
    area = ProjetoArea.query.get_or_404(area_id)
    campo = request.form.get('campo')
    valor = request.form.get('valor', '').strip()
    if campo == 'cor':
        area.cor = valor or None
    elif campo == 'nome' and valor:
        area.nome = valor
    elif campo == 'tipo' and valor in TIPOS_AREA:
        area.tipo = valor
    else:
        return jsonify(ok=False, erro='campo invalido'), 400
    db.session.commit()
    return jsonify(ok=True)
