from datetime import date, datetime, timedelta

from flask import render_template, request, jsonify, redirect, url_for, flash, abort
from flask_login import login_required, current_user

from app.blueprints.projetos import projetos_bp
from app.decorators import admin_required, owner_required
from app.extensions import db
from app.utils import agora, hoje as hoje_brt
from sqlalchemy.orm import joinedload, selectinload

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

    inbox_count = 0
    inbox_area = ProjetoArea.query.filter_by(nome=INBOX_AREA_NOME).first()
    if inbox_area:
        inbox_proj = Projeto.query.filter_by(area_id=inbox_area.id, nome=INBOX_PROJETO_NOME).first()
        if inbox_proj:
            inbox_count = TarefaProjeto.query.filter(
                TarefaProjeto.projeto_id == inbox_proj.id,
                ~TarefaProjeto.status.in_(['feito', 'cancelado']),
            ).count()

    return {
        'a_fazer': a_fazer,
        'fazendo': fazendo,
        'atrasadas': atrasadas,
        'foco_12s': foco_12s,
        'inbox': inbox_count,
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


def _contexto_acao():
    """Dados que o macro botoes_acao() precisa (modais novo projeto/area/template)."""
    from app.models import ProjetoTemplate
    try:
        templates_disponiveis = ProjetoTemplate.query.order_by(ProjetoTemplate.nome).all()
    except Exception:
        templates_disponiveis = []
    return {
        'areas': _areas_filtradas(),
        'usuarios': _usuarios(),
        'templates_disponiveis': templates_disponiveis,
        'projetos_alvo': _projetos_para_select(),
    }


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


INBOX_AREA_NOME = 'Inbox'
INBOX_PROJETO_NOME = 'Avulsas'


def _get_inbox_projeto():
    """Retorna o projeto 'Avulsas' (ou cria se nao existir).
    Esse projeto aceita tarefas sem vinculo claro a um projeto real (estilo GTD inbox).
    """
    area = ProjetoArea.query.filter_by(nome=INBOX_AREA_NOME).first()
    if not area:
        area = ProjetoArea(nome=INBOX_AREA_NOME, tipo='empresa', cor='#6c757d', ordem=-10)
        db.session.add(area)
        db.session.flush()
    proj = Projeto.query.filter_by(area_id=area.id, nome=INBOX_PROJETO_NOME).first()
    if not proj:
        proj = Projeto(area_id=area.id, nome=INBOX_PROJETO_NOME, status='ativo')
        db.session.add(proj)
        db.session.commit()
    return proj


def _projetos_para_select():
    """Lista achatada de projetos visiveis, ordenada por area > nome. Pra dropdown de mover tarefa."""
    q = Projeto.query.join(ProjetoArea).options(joinedload(Projeto.area))
    if not current_user.is_dono():
        q = q.filter(ProjetoArea.tipo == 'empresa')
    return q.filter(Projeto.status != 'concluido') \
            .order_by(ProjetoArea.ordem, ProjetoArea.nome, Projeto.nome).all()


# ── Views ──

@projetos_bp.route('/')
@login_required
@owner_required
def painel():
    """Dashboard: cards de projetos agrupados por area, ordenados por urgencia."""
    import traceback
    from flask import current_app
    from collections import defaultdict
    try:
        from app.models import ProjetoTemplate

        filtro_area = request.args.get('area', type=int)
        filtro_status = request.args.get('status', '')
        so_foco = request.args.get('foco') == '1'
        busca = (request.args.get('q', '') or '').strip().lower()

        q = Projeto.query.join(ProjetoArea).options(
            joinedload(Projeto.area),
            joinedload(Projeto.responsavel),
            selectinload(Projeto.tarefas),
        )
        if not current_user.is_dono():
            q = q.filter(ProjetoArea.tipo == 'empresa')
        if filtro_area:
            q = q.filter(Projeto.area_id == filtro_area)
        if filtro_status and filtro_status in STATUS_PROJETO:
            q = q.filter(Projeto.status == filtro_status)
        if so_foco:
            q = q.filter(Projeto.foco_12s.is_(True))
        projetos = q.all()
        if busca:
            projetos = [p for p in projetos if busca in p.nome.lower()]

        hoje_d = date.today()

        def _urgencia(p, atras):
            # Menor = mais urgente
            if p.status == 'concluido':
                return 9
            if p.status == 'em_espera':
                return 7
            if atras > 0:
                return 0
            if p.foco_12s and p.status == 'ativo':
                return 1
            if p.status == 'ativo':
                return 2
            if p.foco_12s:
                return 3
            return 5  # planejado

        cards_por_area = defaultdict(list)
        contagens_area = defaultdict(lambda: {'total': 0, 'ativos': 0, 'atrasados': 0, 'foco': 0})

        for p in projetos:
            tarefas_list = list(p.tarefas)
            total = len(tarefas_list)
            feitas = sum(1 for t in tarefas_list if t.status == 'feito')
            fazendo_ = sum(1 for t in tarefas_list if t.status == 'fazendo')
            a_fazer_ = sum(1 for t in tarefas_list if t.status == 'a_fazer')
            atras = sum(1 for t in tarefas_list if t.atrasada)
            prazos = [t.prazo for t in tarefas_list
                      if t.prazo and t.status not in ('feito', 'cancelado')]
            prox_prazo = min(prazos) if prazos else None
            pct = round(100 * feitas / total) if total else 0

            card = {
                'p': p,
                'total': total, 'feitas': feitas,
                'fazendo': fazendo_, 'a_fazer': a_fazer_,
                'atrasadas': atras, 'prox_prazo': prox_prazo,
                'pct': pct,
                'urgencia': _urgencia(p, atras),
            }
            cards_por_area[p.area_id].append(card)

            ca = contagens_area[p.area_id]
            ca['total'] += 1
            if p.status == 'ativo':
                ca['ativos'] += 1
            if atras:
                ca['atrasados'] += 1
            if p.foco_12s:
                ca['foco'] += 1

        # Ordena cards dentro de cada area por urgencia
        for aid in cards_por_area:
            cards_por_area[aid].sort(key=lambda c: (c['urgencia'], -(c['p'].id or 0)))

        areas = _areas_filtradas()
        # Filtra areas que tem cards (depois dos filtros)
        areas_com_cards = [a for a in areas if cards_por_area.get(a.id)]

        try:
            templates_disponiveis = ProjetoTemplate.query.order_by(ProjetoTemplate.nome).all()
        except Exception:
            templates_disponiveis = []

        return render_template('projetos/dashboard.html',
                               areas=areas,
                               areas_com_cards=areas_com_cards,
                               cards_por_area=cards_por_area,
                               contagens_area=contagens_area,
                               total_filtrados=sum(len(v) for v in cards_por_area.values()),
                               contadores=_contadores(),
                               usuarios=_usuarios(),
                               templates_disponiveis=templates_disponiveis,
                               projetos_alvo=_projetos_para_select(),
                               filtro_area=filtro_area, filtro_status=filtro_status,
                               so_foco=so_foco, busca=busca,
                               hoje_today=hoje_d,
                               data_relativa=_data_relativa,
                               view='dashboard')
    except Exception as e:
        current_app.logger.error('Erro no dashboard de projetos: %s\n%s', e, traceback.format_exc())
        return (
            '<h1>Erro no Dashboard de Projetos</h1>'
            f'<p><strong>{type(e).__name__}:</strong> {e}</p>'
            f'<pre style="background:#f5f5f5;padding:12px;border-radius:6px;overflow:auto;">{traceback.format_exc()}</pre>'
            '<p><a href="/projetos/_migrar">Forçar migração</a> · '
            '<a href="/projetos/lista">Visão lista</a> · '
            '<a href="/">Voltar</a></p>',
            500
        )


@projetos_bp.route('/lista')
@login_required
@owner_required
def hierarquia():
    """Visão hierárquica densa: Área › Projeto › Tarefas (todos expandidos)."""
    from app.models import ProjetoTemplate
    # Eager-load: areas com projetos, projetos com tarefas, etc.
    q = ProjetoArea.query.options(
        selectinload(ProjetoArea.projetos)
            .options(
                joinedload(Projeto.responsavel),
                selectinload(Projeto.tarefas).joinedload(TarefaProjeto.responsavel),
            ),
    ).filter_by(ativa=True)
    if not current_user.is_dono():
        q = q.filter(ProjetoArea.tipo == 'empresa')
    areas = q.order_by(ProjetoArea.ordem, ProjetoArea.nome).all()

    templates_disponiveis = ProjetoTemplate.query.order_by(ProjetoTemplate.nome).all()
    return render_template('projetos/painel.html',
                           areas=areas,
                           contadores=_contadores(),
                           usuarios=_usuarios(),
                           templates_disponiveis=templates_disponiveis,
                           projetos_alvo=_projetos_para_select(),
                           data_relativa=_data_relativa,
                           view='hier')


@projetos_bp.route('/p/<int:pid>')
@login_required
@owner_required
def projeto_detalhe(pid):
    """Página dedicada de um projeto com mini-kanban."""
    p = Projeto.query.options(
        joinedload(Projeto.area),
        joinedload(Projeto.responsavel),
        selectinload(Projeto.tarefas).joinedload(TarefaProjeto.responsavel),
    ).filter_by(id=pid).first_or_404()
    if not _projeto_visivel(p):
        abort(403)

    cols = {'a_fazer': [], 'fazendo': [], 'feito': [], 'cancelado': []}
    for t in sorted(p.tarefas, key=lambda x: (x.prazo is None, x.prazo, x.ordem)):
        if t.status in cols:
            cols[t.status].append(t)

    total = len(p.tarefas)
    feitas = sum(1 for t in p.tarefas if t.status == 'feito')
    pct = round(100 * feitas / total) if total else 0

    return render_template('projetos/projeto_detalhe.html',
                           p=p, cols=cols, pct=pct,
                           total=total, feitas=feitas,
                           areas=_areas_filtradas(),
                           contadores=_contadores(),
                           usuarios=_usuarios(),
                           projetos_alvo=_projetos_para_select(),
                           data_relativa=_data_relativa,
                           view='dashboard')


@projetos_bp.route('/kanban')
@login_required
@owner_required
def kanban():
    filtro_area = request.args.get('area', type=int)
    so_foco = request.args.get('foco') == '1'
    incluir_canceladas = request.args.get('canceladas') == '1'
    mostrar_antigas = request.args.get('antigas') == '1'

    q = TarefaProjeto.query.join(Projeto).join(ProjetoArea).options(
        joinedload(TarefaProjeto.projeto).joinedload(Projeto.area),
        joinedload(TarefaProjeto.responsavel),
    )
    if not current_user.is_dono():
        q = q.filter(ProjetoArea.tipo == 'empresa')
    if filtro_area:
        q = q.filter(Projeto.area_id == filtro_area)
    if so_foco:
        q = q.filter(Projeto.foco_12s.is_(True))

    # Por padrao, oculta tarefas concluidas/canceladas ha mais de 7 dias do kanban
    # — evita poluicao da coluna Feito ao longo do tempo.
    if not mostrar_antigas:
        corte = agora() - timedelta(days=7)
        q = q.filter(
            db.or_(
                ~TarefaProjeto.status.in_(['feito', 'cancelado']),
                TarefaProjeto.feito_em.is_(None),
                TarefaProjeto.feito_em >= corte,
            )
        )

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
                           filtro_area=filtro_area,
                           so_foco=so_foco,
                           incluir_canceladas=incluir_canceladas,
                           mostrar_antigas=mostrar_antigas,
                           contadores=_contadores(),
                           data_relativa=_data_relativa,
                           view='kanban',
                           **_contexto_acao())


@projetos_bp.route('/foco')
@login_required
@owner_required
def foco():
    q = Projeto.query.filter_by(foco_12s=True).join(ProjetoArea)
    if not current_user.is_dono():
        q = q.filter(ProjetoArea.tipo == 'empresa')
    projetos = q.order_by(Projeto.nome).all()
    return render_template('projetos/foco.html',
                           projetos=projetos,
                           contadores=_contadores(),
                           data_relativa=_data_relativa,
                           view='foco',
                           **_contexto_acao())


@projetos_bp.route('/dia')
@login_required
@owner_required
def dia():
    """Tarefas de uma data especifica (similar a 'hoje' mas com seletor)."""
    data_str = request.args.get('data', '')
    try:
        alvo = datetime.strptime(data_str, '%Y-%m-%d').date()
    except ValueError:
        alvo = date.today()

    base = TarefaProjeto.query.join(Projeto).join(ProjetoArea).options(
        joinedload(TarefaProjeto.projeto).joinedload(Projeto.area),
        joinedload(TarefaProjeto.responsavel),
    )
    if not current_user.is_dono():
        base = base.filter(ProjetoArea.tipo == 'empresa')

    # Tarefas com prazo nesse dia (todos os status)
    do_dia = base.filter(TarefaProjeto.prazo == alvo).order_by(TarefaProjeto.status).all()

    # Tarefas em fazendo (independe da data)
    fazendo = base.filter(TarefaProjeto.status == 'fazendo') \
        .order_by(TarefaProjeto.prazo.is_(None), TarefaProjeto.prazo).all()

    # Atrasadas relativas a essa data
    atrasadas = base.filter(
        TarefaProjeto.prazo.isnot(None),
        TarefaProjeto.prazo < alvo,
        ~TarefaProjeto.status.in_(['feito', 'cancelado']),
    ).order_by(TarefaProjeto.prazo).all()

    # Próximos 7 dias após a data
    semana = base.filter(
        TarefaProjeto.prazo.isnot(None),
        TarefaProjeto.prazo > alvo,
        TarefaProjeto.prazo <= alvo + timedelta(days=7),
        ~TarefaProjeto.status.in_(['feito', 'cancelado']),
    ).order_by(TarefaProjeto.prazo).all()

    return render_template('projetos/dia.html',
                           alvo=alvo,
                           alvo_iso=alvo.isoformat(),
                           dia_anterior=(alvo - timedelta(days=1)).isoformat(),
                           dia_proximo=(alvo + timedelta(days=1)).isoformat(),
                           do_dia=do_dia,
                           fazendo=fazendo,
                           atrasadas=atrasadas,
                           semana=semana,
                           hoje=date.today(),
                           contadores=_contadores(),
                           data_relativa=_data_relativa,
                           view='dia',
                           **_contexto_acao())


@projetos_bp.route('/hoje')
@login_required
@owner_required
def hoje():
    hoje_d = date.today()

    base = TarefaProjeto.query.join(Projeto).join(ProjetoArea).options(
        joinedload(TarefaProjeto.projeto).joinedload(Projeto.area),
        joinedload(TarefaProjeto.responsavel),
    )
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
                           data_relativa=_data_relativa,
                           view='hoje',
                           **_contexto_acao())


# ── CRUD: Áreas ──

@projetos_bp.route('/area/nova', methods=['POST'])
@login_required
@owner_required
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
@owner_required
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
@owner_required
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
@owner_required
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
@owner_required
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

@projetos_bp.route('/tarefa/quick', methods=['POST'])
@login_required
@owner_required
def tarefa_quick():
    """Cria tarefa rapida na Inbox (sem vinculo a projeto especifico)."""
    nome = request.form.get('nome', '').strip()
    if not nome:
        return jsonify(ok=False, erro='nome obrigatorio'), 400

    inbox = _get_inbox_projeto()

    prazo = None
    raw_prazo = request.form.get('prazo', '').strip()
    if raw_prazo:
        try:
            prazo = datetime.strptime(raw_prazo, '%Y-%m-%d').date()
        except ValueError:
            pass

    t = TarefaProjeto(
        projeto_id=inbox.id,
        nome=nome,
        status='a_fazer',
        prazo=prazo,
    )
    db.session.add(t)
    db.session.commit()

    return jsonify(ok=True, id=t.id, contadores=_contadores())


@projetos_bp.route('/inbox')
@login_required
@owner_required
def inbox():
    """Caixa de entrada: tarefas avulsas aguardando classificacao."""
    inbox_proj = _get_inbox_projeto()
    pendentes = TarefaProjeto.query.filter(
        TarefaProjeto.projeto_id == inbox_proj.id,
        ~TarefaProjeto.status.in_(['feito', 'cancelado']),
    ).order_by(TarefaProjeto.criado_em.desc()).all()

    feitas_recentes = TarefaProjeto.query.filter(
        TarefaProjeto.projeto_id == inbox_proj.id,
        TarefaProjeto.status.in_(['feito', 'cancelado']),
        TarefaProjeto.feito_em.isnot(None),
        TarefaProjeto.feito_em >= agora() - timedelta(days=14),
    ).order_by(TarefaProjeto.feito_em.desc()).all()

    return render_template('projetos/inbox.html',
                           pendentes=pendentes,
                           feitas_recentes=feitas_recentes,
                           contadores=_contadores(),
                           data_relativa=_data_relativa,
                           view='inbox',
                           **_contexto_acao())


@projetos_bp.route('/tarefa/nova', methods=['POST'])
@login_required
@owner_required
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
@owner_required
def tarefa_editar(tid):
    t = TarefaProjeto.query.get_or_404(tid)
    if not _tarefa_visivel(t):
        abort(403)
    campo = request.form.get('campo')
    valor = request.form.get('valor', '').strip()

    if campo == 'status' and valor in STATUS_TAREFA:
        antigo = t.status
        t.status = valor
        t.feito_em = agora() if valor == 'feito' else None
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
@owner_required
def tarefa_mover(tid):
    """Drag-and-drop: muda status (coluna kanban) e atualiza ordens das tarefas afetadas."""
    t = TarefaProjeto.query.get_or_404(tid)
    novo_status = request.form.get('status', '').strip()
    if novo_status not in STATUS_TAREFA:
        return jsonify(ok=False, erro='status invalido'), 400
    if t.status != novo_status:
        t.status = novo_status
        t.feito_em = agora() if novo_status == 'feito' else None

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


@projetos_bp.route('/tarefa/<int:tid>/dados')
@login_required
@owner_required
def tarefa_dados(tid):
    """Retorna dados completos de uma tarefa (para o modal de edicao)."""
    t = TarefaProjeto.query.get_or_404(tid)
    if not _tarefa_visivel(t):
        abort(403)
    return jsonify({
        'id': t.id,
        'nome': t.nome,
        'projeto_id': t.projeto_id,
        'projeto_nome': t.projeto.nome,
        'tipo': t.tipo or '',
        'esforco': t.esforco or '',
        'prazo': t.prazo.isoformat() if t.prazo else '',
        'responsavel_id': t.responsavel_id or '',
        'recorrencia': t.recorrencia or '',
        'observacao': t.observacao or '',
        'status': t.status,
    })


@projetos_bp.route('/tarefa/<int:tid>/atualizar', methods=['POST'])
@login_required
@owner_required
def tarefa_atualizar(tid):
    """Atualiza todos os campos de uma tarefa de uma vez."""
    t = TarefaProjeto.query.get_or_404(tid)
    if not _tarefa_visivel(t):
        abort(403)

    nome = request.form.get('nome', '').strip()
    if not nome:
        return jsonify(ok=False, erro='nome obrigatorio'), 400

    t.nome = nome
    t.tipo = request.form.get('tipo') or None
    t.esforco = request.form.get('esforco') or None
    t.recorrencia = request.form.get('recorrencia') or None
    t.observacao = request.form.get('observacao', '').strip() or None
    t.responsavel_id = request.form.get('responsavel_id', type=int) or None

    novo_pid = request.form.get('projeto_id', type=int)
    if novo_pid and novo_pid != t.projeto_id:
        proj_alvo = Projeto.query.get(novo_pid)
        if proj_alvo and _projeto_visivel(proj_alvo):
            t.projeto_id = novo_pid

    raw_prazo = request.form.get('prazo', '').strip()
    if raw_prazo:
        try:
            t.prazo = datetime.strptime(raw_prazo, '%Y-%m-%d').date()
        except ValueError:
            pass
    else:
        t.prazo = None

    novo_status = request.form.get('status', '').strip()
    if novo_status in STATUS_TAREFA:
        antigo = t.status
        t.status = novo_status
        t.feito_em = agora() if novo_status == 'feito' else None
        if novo_status == 'feito' and antigo != 'feito' and t.recorrencia:
            _agendar_proxima(t)

    db.session.commit()
    return jsonify(ok=True, contadores=_contadores())


@projetos_bp.route('/tarefa/<int:tid>/excluir', methods=['POST'])
@login_required
@owner_required
def tarefa_excluir(tid):
    t = TarefaProjeto.query.get_or_404(tid)
    db.session.delete(t)
    db.session.commit()
    flash('Tarefa removida.', 'success')
    return redirect(request.referrer or url_for('projetos.painel'))


# ── Weekly Review ──

@projetos_bp.route('/weekly')
@login_required
@owner_required
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


@projetos_bp.route('/_migrar', methods=['GET'])
@login_required
@owner_required
def forcar_migrate():
    """Endpoint de emergencia para forcar migrations das colunas novas."""
    import traceback
    from flask import current_app
    from app import _migrate
    try:
        _migrate(current_app)
        return (
            '<h1>Migrações executadas</h1>'
            '<p>Sem erros. <a href="/projetos/">Voltar pro dashboard</a></p>',
            200
        )
    except Exception as e:
        return (
            '<h1>Erro ao migrar</h1>'
            f'<pre>{traceback.format_exc()}</pre>'
            '<p><a href="/">Início</a></p>',
            500
        )


@projetos_bp.route('/weekly/salvar', methods=['POST'])
@login_required
@owner_required
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
@owner_required
def templates_lista():
    from app.models import ProjetoTemplate
    templates = ProjetoTemplate.query.order_by(ProjetoTemplate.nome).all()
    return render_template('projetos/templates.html',
                           templates=templates,
                           contadores=_contadores(),
                           view='templates',
                           **_contexto_acao())


@projetos_bp.route('/templates/novo', methods=['POST'])
@login_required
@owner_required
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
@owner_required
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
@owner_required
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
@owner_required
def template_excluir(tid):
    from app.models import ProjetoTemplate
    template = ProjetoTemplate.query.get_or_404(tid)
    db.session.delete(template)
    db.session.commit()
    flash('Template removido.', 'success')
    return redirect(url_for('projetos.templates_lista'))


@projetos_bp.route('/templates/<int:tid>/tarefa', methods=['POST'])
@login_required
@owner_required
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
@owner_required
def template_tarefa_excluir(tt_id):
    from app.models import TarefaTemplate
    tt = TarefaTemplate.query.get_or_404(tt_id)
    template_id = tt.template_id
    db.session.delete(tt)
    db.session.commit()
    return redirect(url_for('projetos.template_editar', tid=template_id))


@projetos_bp.route('/projeto/novo-de-template', methods=['POST'])
@login_required
@owner_required
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
@owner_required
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
                           view='calendario',
                           **_contexto_acao())


# ── Relatorio de produtividade ──

@projetos_bp.route('/relatorio')
@login_required
@owner_required
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
                           view='relatorio',
                           **_contexto_acao())


# ── Edicao de cor de area ──

@projetos_bp.route('/area/<int:area_id>/editar', methods=['POST'])
@login_required
@owner_required
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
