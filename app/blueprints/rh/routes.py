from datetime import datetime

from flask import render_template, redirect, url_for, flash, request, Response, abort
from flask_login import login_required, current_user

from app.blueprints.rh import rh_bp
from app.decorators import admin_required
from app.extensions import db
from app.models import Funcionario, Loja, FolhaPagamento, Feedback, Posicao, Atestado, funcionario_loja
from app.utils import parse_float_br


@rh_bp.route('/')
@login_required
@admin_required
def dashboard():
    funcionarios = Funcionario.query.filter_by(ativo=True).all()
    lojas = Loja.query.filter_by(ativa=True).order_by(Loja.nome).all()

    total_salarios = sum(f.salario_base for f in funcionarios)
    total_custo = sum(f.custo_total() for f in funcionarios)
    total_funcionarios = len(funcionarios)

    custo_por_loja = {}
    for loja in lojas:
        funcs_loja = [f for f in funcionarios if loja in f.lojas]
        custo_por_loja[loja.nome] = {
            'qtd': len(funcs_loja),
            'custo': sum(f.custo_total() for f in funcs_loja),
        }

    hoje = datetime.now()
    aniversariantes = [
        f for f in funcionarios
        if f.data_nascimento and f.data_nascimento.month == hoje.month
    ]
    aniversarios_casa = [
        f for f in funcionarios
        if f.data_admissao and f.data_admissao.month == hoje.month
    ]

    atestados_recentes = (
        Atestado.query
        .order_by(Atestado.data.desc(), Atestado.criado_em.desc())
        .limit(10).all()
    )

    return render_template('rh/dashboard.html',
                           total_funcionarios=total_funcionarios,
                           total_salarios=total_salarios,
                           total_custo=total_custo,
                           custo_por_loja=custo_por_loja,
                           aniversariantes=aniversariantes,
                           aniversarios_casa=aniversarios_casa,
                           atestados_recentes=atestados_recentes,
                           funcionarios_ativos=sorted(funcionarios, key=lambda f: f.nome),
                           lojas=lojas)


@rh_bp.route('/funcionarios')
@login_required
@admin_required
def funcionarios():
    loja_id = request.args.get('loja', type=int)
    apenas_ativos = request.args.get('ativos', '1') == '1'

    query = Funcionario.query
    if apenas_ativos:
        query = query.filter_by(ativo=True)
    if loja_id:
        query = query.filter(Funcionario.lojas.any(Loja.id == loja_id))

    lista = query.order_by(Funcionario.nome).all()
    lojas = Loja.query.filter_by(ativa=True).order_by(Loja.nome).all()

    return render_template('rh/funcionarios.html',
                           funcionarios=lista,
                           lojas=lojas,
                           loja_id=loja_id,
                           apenas_ativos=apenas_ativos)


@rh_bp.route('/funcionarios/novo', methods=['GET', 'POST'])
@login_required
@admin_required
def novo_funcionario():
    if request.method == 'POST':
        func = Funcionario(
            nome=request.form.get('nome', '').strip(),
            cpf=request.form.get('cpf', '').strip(),
            funcao=request.form.get('funcao', '').strip() or None,
            salario_base=parse_float_br(request.form.get('salario_base', ''), default=0),
            cargo_confianca=parse_float_br(request.form.get('cargo_confianca', ''), default=0),
            premiacao=parse_float_br(request.form.get('premiacao', ''), default=0),
            vt_dia=parse_float_br(request.form.get('vt_dia', ''), default=0),
            vr_dia=parse_float_br(request.form.get('vr_dia', ''), default=22),
            dias_trabalhados=int(request.form.get('dias_trabalhados', '26') or 26),
            hora_extra_pct=parse_float_br(request.form.get('hora_extra_pct', ''), default=55),
            telefone=request.form.get('telefone', '').strip() or None,
            email=request.form.get('email', '').strip() or None,
            observacao=request.form.get('observacao', '').strip() or None,
        )
        data_str = request.form.get('data_admissao', '').strip()
        if data_str:
            try:
                func.data_admissao = datetime.strptime(data_str, '%Y-%m-%d').date()
            except ValueError:
                pass

        nasc_str = request.form.get('data_nascimento', '').strip()
        if nasc_str:
            try:
                func.data_nascimento = datetime.strptime(nasc_str, '%Y-%m-%d').date()
            except ValueError:
                pass

        loja_ids = request.form.getlist('lojas[]')
        for lid in loja_ids:
            loja = Loja.query.get(int(lid))
            if loja:
                func.lojas.append(loja)

        db.session.add(func)
        db.session.commit()
        flash(f'Funcionário "{func.nome}" cadastrado!', 'success')
        return redirect(url_for('rh.detalhe_funcionario', id=func.id))

    lojas = Loja.query.filter_by(ativa=True).order_by(Loja.nome).all()
    return render_template('rh/funcionario_form.html', func=None, lojas=lojas)


@rh_bp.route('/funcionarios/<int:id>')
@login_required
@admin_required
def detalhe_funcionario(id):
    func = Funcionario.query.get_or_404(id)
    lojas = Loja.query.filter_by(ativa=True).order_by(Loja.nome).all()
    feedbacks = Feedback.query.filter_by(funcionario_id=id).order_by(Feedback.data.desc()).all()
    folhas = FolhaPagamento.query.filter_by(funcionario_id=id).order_by(
        FolhaPagamento.ano.desc(), FolhaPagamento.mes.desc()
    ).limit(12).all()

    return render_template('rh/funcionario_detalhe.html',
                           func=func, lojas=lojas, feedbacks=feedbacks, folhas=folhas)


@rh_bp.route('/funcionarios/<int:id>/salvar', methods=['POST'])
@login_required
@admin_required
def salvar_funcionario(id):
    func = Funcionario.query.get_or_404(id)

    func.nome = request.form.get('nome', '').strip() or func.nome
    func.cpf = request.form.get('cpf', '').strip() or func.cpf
    func.funcao = request.form.get('funcao', '').strip() or None
    func.salario_base = parse_float_br(request.form.get('salario_base', ''), default=0)
    func.cargo_confianca = parse_float_br(request.form.get('cargo_confianca', ''), default=0)
    func.premiacao = parse_float_br(request.form.get('premiacao', ''), default=0)
    func.vt_dia = parse_float_br(request.form.get('vt_dia', ''), default=0)
    func.vr_dia = parse_float_br(request.form.get('vr_dia', ''), default=22)
    func.dias_trabalhados = int(request.form.get('dias_trabalhados', '26') or 26)
    func.hora_extra_pct = parse_float_br(request.form.get('hora_extra_pct', ''), default=55)
    func.telefone = request.form.get('telefone', '').strip() or None
    func.email = request.form.get('email', '').strip() or None
    func.observacao = request.form.get('observacao', '').strip() or None
    func.ativo = 'ativo' in request.form

    data_adm = request.form.get('data_admissao', '').strip()
    if data_adm:
        try:
            func.data_admissao = datetime.strptime(data_adm, '%Y-%m-%d').date()
        except ValueError:
            pass

    data_dem = request.form.get('data_demissao', '').strip()
    if data_dem:
        try:
            func.data_demissao = datetime.strptime(data_dem, '%Y-%m-%d').date()
        except ValueError:
            pass
    else:
        func.data_demissao = None

    nasc_str = request.form.get('data_nascimento', '').strip()
    if nasc_str:
        try:
            func.data_nascimento = datetime.strptime(nasc_str, '%Y-%m-%d').date()
        except ValueError:
            pass
    else:
        func.data_nascimento = None

    func.lojas.clear()
    loja_ids = request.form.getlist('lojas[]')
    for lid in loja_ids:
        loja = Loja.query.get(int(lid))
        if loja:
            func.lojas.append(loja)

    db.session.commit()
    flash(f'"{func.nome}" atualizado!', 'success')
    return redirect(url_for('rh.detalhe_funcionario', id=func.id))


@rh_bp.route('/funcionarios/<int:id>/feedback', methods=['POST'])
@login_required
@admin_required
def add_feedback(id):
    func = Funcionario.query.get_or_404(id)
    texto = request.form.get('texto', '').strip()
    tipo = request.form.get('tipo', 'neutro')

    if not texto:
        flash('Texto do feedback é obrigatório.', 'warning')
        return redirect(url_for('rh.detalhe_funcionario', id=id))

    fb = Feedback(
        funcionario_id=id,
        autor_id=current_user.id,
        tipo=tipo,
        texto=texto,
    )
    db.session.add(fb)
    db.session.commit()
    flash('Feedback registrado!', 'success')
    return redirect(url_for('rh.detalhe_funcionario', id=id))


@rh_bp.route('/funcionarios/<int:id>/feedback/<int:fb_id>/excluir', methods=['POST'])
@login_required
@admin_required
def excluir_feedback(id, fb_id):
    fb = Feedback.query.get_or_404(fb_id)
    db.session.delete(fb)
    db.session.commit()
    flash('Feedback removido.', 'success')
    return redirect(url_for('rh.detalhe_funcionario', id=id))


@rh_bp.route('/lojas')
@login_required
@admin_required
def lojas():
    lista = Loja.query.order_by(Loja.nome).all()
    return render_template('rh/lojas.html', lojas=lista)


@rh_bp.route('/lojas/salvar', methods=['POST'])
@login_required
@admin_required
def salvar_lojas():
    ids = request.form.getlist('loja_id[]')
    nomes = request.form.getlist('loja_nome[]')
    enderecos = request.form.getlist('loja_endereco[]')
    telefones = request.form.getlist('loja_telefone[]')

    for i in range(len(nomes)):
        nome = nomes[i].strip()
        if not nome:
            continue
        endereco = enderecos[i].strip() if i < len(enderecos) else ''
        telefone = telefones[i].strip() if i < len(telefones) else ''
        lid = ids[i].strip() if i < len(ids) else ''

        if lid:
            loja = Loja.query.get(int(lid))
            if loja:
                loja.nome = nome
                loja.endereco = endereco or None
                loja.telefone = telefone or None
        else:
            db.session.add(Loja(
                nome=nome,
                endereco=endereco or None,
                telefone=telefone or None,
            ))

    db.session.commit()
    flash('Lojas salvas!', 'success')
    return redirect(url_for('rh.lojas'))


@rh_bp.route('/lojas/excluir/<int:id>', methods=['POST'])
@login_required
@admin_required
def excluir_loja(id):
    loja = Loja.query.get_or_404(id)
    nome = loja.nome
    db.session.delete(loja)
    db.session.commit()
    flash(f'Loja "{nome}" excluída!', 'success')
    return redirect(url_for('rh.lojas'))


@rh_bp.route('/folha')
@login_required
@admin_required
def folha():
    mes = request.args.get('mes', type=int, default=datetime.now().month)
    ano = request.args.get('ano', type=int, default=datetime.now().year)

    folhas = FolhaPagamento.query.filter_by(mes=mes, ano=ano).all()
    funcionarios_ativos = Funcionario.query.filter_by(ativo=True).order_by(Funcionario.nome).all()

    return render_template('rh/folha.html',
                           folhas=folhas,
                           funcionarios=funcionarios_ativos,
                           mes=mes, ano=ano)


@rh_bp.route('/folha/gerar', methods=['POST'])
@login_required
@admin_required
def gerar_folha():
    mes = int(request.form.get('mes', datetime.now().month))
    ano = int(request.form.get('ano', datetime.now().year))

    existente = FolhaPagamento.query.filter_by(mes=mes, ano=ano).first()
    if existente:
        flash(f'Folha de {mes:02d}/{ano} já existe!', 'warning')
        return redirect(url_for('rh.folha', mes=mes, ano=ano))

    funcionarios = Funcionario.query.filter_by(ativo=True).all()
    for f in funcionarios:
        folha = FolhaPagamento(
            funcionario_id=f.id,
            mes=mes,
            ano=ano,
            salario_base=f.salario_base,
            cargo_confianca=f.cargo_confianca or 0,
            horas_extras=0,
            premiacao=f.premiacao or 0,
            vt_dia=f.vt_dia or 0,
            vr_dia=f.vr_dia or 0,
            dias_trabalhados=f.dias_trabalhados or 26,
            descontos=0,
        )
        db.session.add(folha)

    db.session.commit()
    flash(f'Folha de {mes:02d}/{ano} gerada com {len(funcionarios)} funcionários!', 'success')
    return redirect(url_for('rh.folha', mes=mes, ano=ano))


@rh_bp.route('/folha/<int:folha_id>/salvar', methods=['POST'])
@login_required
@admin_required
def salvar_folha_item(folha_id):
    f = FolhaPagamento.query.get_or_404(folha_id)
    f.dias_trabalhados = int(request.form.get('dias_trabalhados', '26') or 26)
    f.horas_extras = parse_float_br(request.form.get('horas_extras', ''), default=0)
    f.premiacao = parse_float_br(request.form.get('premiacao', ''), default=0)
    f.descontos = parse_float_br(request.form.get('descontos', ''), default=0)
    f.observacao = request.form.get('observacao', '').strip() or None
    db.session.commit()
    flash('Folha atualizada!', 'success')
    return redirect(url_for('rh.folha', mes=f.mes, ano=f.ano))


@rh_bp.route('/escala')
@login_required
@admin_required
def escala():
    lojas = Loja.query.filter_by(ativa=True).order_by(Loja.nome).all()
    posicoes = Posicao.query.order_by(Posicao.loja_id, Posicao.periodo, Posicao.ordem).all()

    # Agrupar: {loja_nome: {periodo: [posicoes]}}
    grid = {}
    for pos in posicoes:
        lnome = pos.loja.nome
        if lnome not in grid:
            grid[lnome] = {'Manhã': [], 'Tarde': []}
        grid[lnome][pos.periodo].append(pos)

    # Funcionários sem loja (precisam alocação)
    sem_loja = Funcionario.query.filter(
        Funcionario.ativo == True,
        ~Funcionario.lojas.any()
    ).order_by(Funcionario.nome).all()

    pendentes = Funcionario.query.filter_by(
        ativo=True, cadastro_pendente=True
    ).order_by(Funcionario.nome).all()

    # Lista de todos os funcionários ativos para atribuir
    todos_func = Funcionario.query.filter_by(ativo=True).order_by(Funcionario.nome).all()

    return render_template('rh/escala.html',
                           grid=grid,
                           lojas=lojas,
                           sem_loja=sem_loja,
                           pendentes=pendentes,
                           todos_func=todos_func)


@rh_bp.route('/escala/<int:pos_id>/atribuir', methods=['POST'])
@login_required
@admin_required
def atribuir_posicao(pos_id):
    pos = Posicao.query.get_or_404(pos_id)
    func_id = request.form.get('funcionario_id', '').strip()
    status = request.form.get('status', 'ativo')
    obs = request.form.get('observacao', '').strip()

    if func_id:
        pos.funcionario_id = int(func_id)
    else:
        pos.funcionario_id = None
        status = 'vago'

    pos.status = status
    pos.observacao = obs or None
    db.session.commit()
    flash(f'Posição "{pos.nome_posicao}" atualizada!', 'success')
    return redirect(url_for('rh.escala'))


@rh_bp.route('/escala/posicao/nova', methods=['POST'])
@login_required
@admin_required
def nova_posicao():
    loja_id = int(request.form.get('loja_id'))
    periodo = request.form.get('periodo', 'Manhã')
    nome_pos = request.form.get('nome_posicao', '').strip()

    if not nome_pos:
        flash('Nome da posição é obrigatório.', 'warning')
        return redirect(url_for('rh.escala'))

    max_ordem = db.session.query(db.func.max(Posicao.ordem)).filter_by(
        loja_id=loja_id, periodo=periodo
    ).scalar() or 0

    pos = Posicao(
        loja_id=loja_id,
        periodo=periodo,
        nome_posicao=nome_pos,
        ordem=max_ordem + 1,
        status='vago',
    )
    db.session.add(pos)
    db.session.commit()
    flash(f'Posição "{nome_pos}" criada!', 'success')
    return redirect(url_for('rh.escala'))


@rh_bp.route('/escala/posicao/<int:pos_id>/excluir', methods=['POST'])
@login_required
@admin_required
def excluir_posicao(pos_id):
    pos = Posicao.query.get_or_404(pos_id)
    db.session.delete(pos)
    db.session.commit()
    flash('Posição removida.', 'success')
    return redirect(url_for('rh.escala'))


@rh_bp.route('/folha/<int:folha_id>/excluir', methods=['POST'])
@login_required
@admin_required
def excluir_folha_item(folha_id):
    f = FolhaPagamento.query.get_or_404(folha_id)
    mes, ano = f.mes, f.ano
    nome = f.funcionario.nome
    db.session.delete(f)
    db.session.commit()
    flash(f'Folha de {nome} ({mes:02d}/{ano}) removida.', 'success')
    return redirect(url_for('rh.folha', mes=mes, ano=ano))


@rh_bp.route('/folha/excluir-mes', methods=['POST'])
@login_required
@admin_required
def excluir_folha_mes():
    mes = int(request.form.get('mes', 0))
    ano = int(request.form.get('ano', 0))
    qtd = FolhaPagamento.query.filter_by(mes=mes, ano=ano).delete()
    db.session.commit()
    flash(f'Folha de {mes:02d}/{ano} excluída ({qtd} registros).', 'success')
    return redirect(url_for('rh.folha', mes=mes, ano=ano))


@rh_bp.route('/atestado/novo', methods=['POST'])
@login_required
@admin_required
def novo_atestado():
    func_id = request.form.get('funcionario_id', '').strip()
    data_str = request.form.get('data', '').strip()
    motivo = request.form.get('motivo', '').strip()
    arquivo = request.files.get('arquivo')

    if not func_id or not data_str:
        flash('Funcionário e data são obrigatórios.', 'warning')
        return redirect(url_for('rh.dashboard'))

    try:
        data = datetime.strptime(data_str, '%Y-%m-%d').date()
    except ValueError:
        flash('Data inválida.', 'warning')
        return redirect(url_for('rh.dashboard'))

    atestado = Atestado(
        funcionario_id=int(func_id),
        data=data,
        motivo=motivo or None,
        criado_por=current_user.id,
    )

    if arquivo and arquivo.filename:
        atestado.arquivo = arquivo.read()
        atestado.arquivo_nome = arquivo.filename
        atestado.arquivo_mimetype = arquivo.mimetype or 'application/octet-stream'

    db.session.add(atestado)
    db.session.commit()
    flash('Atestado registrado!', 'success')
    return redirect(url_for('rh.dashboard'))


@rh_bp.route('/atestado/<int:id>/arquivo')
@login_required
@admin_required
def ver_atestado(id):
    at = Atestado.query.get_or_404(id)
    if not at.arquivo:
        abort(404)
    return Response(
        at.arquivo,
        mimetype=at.arquivo_mimetype or 'application/octet-stream',
        headers={'Content-Disposition': f'inline; filename="{at.arquivo_nome or "atestado"}"'}
    )


@rh_bp.route('/atestado/<int:id>/excluir', methods=['POST'])
@login_required
@admin_required
def excluir_atestado(id):
    at = Atestado.query.get_or_404(id)
    db.session.delete(at)
    db.session.commit()
    flash('Atestado removido.', 'success')
    return redirect(url_for('rh.dashboard'))


@rh_bp.route('/feedback/novo', methods=['POST'])
@login_required
@admin_required
def novo_feedback_dashboard():
    func_id = request.form.get('funcionario_id', '').strip()
    texto = request.form.get('texto', '').strip()
    tipo = request.form.get('tipo', 'neutro')

    if not func_id or not texto:
        flash('Funcionário e texto do feedback são obrigatórios.', 'warning')
        return redirect(url_for('rh.dashboard'))

    fb = Feedback(
        funcionario_id=int(func_id),
        autor_id=current_user.id,
        tipo=tipo,
        texto=texto,
    )
    db.session.add(fb)
    db.session.commit()
    flash('Feedback registrado!', 'success')
    return redirect(url_for('rh.dashboard'))
