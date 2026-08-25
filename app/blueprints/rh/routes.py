from datetime import datetime
from urllib.parse import quote

from flask import Response, abort, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy.orm import defer, joinedload, selectinload
from werkzeug.utils import secure_filename

from app.blueprints.rh import rh_bp
from app.decorators import owner_required, rh_required
from app.extensions import db
from app.models import (
    Atestado,
    Cargo,
    Feedback,
    Ferias,
    FolhaPagamento,
    Funcionario,
    Loja,
    Posicao,
    RegistroPonto,
    SlotMapa,
)
from app.utils import agora, parse_float_br
from app.utils import hoje as hoje_brt

ALLOWED_MIMETYPES = {'image/jpeg', 'image/png', 'image/gif', 'image/webp', 'application/pdf'}


@rh_bp.before_request
def _rh_restrito_ao_owner():
    # RH temporariamente acessivel apenas ao owner. Reverter: remover este
    # guard + trocar is_owner por pode_rh() na sidebar (base.html).
    if not current_user.is_authenticated:
        return current_app.login_manager.unauthorized()
    if (request.endpoint in {
            'rh.lideranca_preenchimento',
            'rh.lideranca_preenchimento_salvar',
            'rh.lideranca_organograma',
            'rh.lideranca_organograma_pdf',
            } and current_user.pode_organizar_equipe()):
        return None
    if not current_user.is_dono():
        abort(403)


@rh_bp.route('/')
@login_required
@rh_required
def dashboard():
    # Eager load de cargo + lojas evita N+1 ao calcular custo_total() e
    # custo por loja (cada Funcionario acessa cargo.salario_base e lojas).
    funcionarios = (
        Funcionario.query
        .options(joinedload(Funcionario.cargo), selectinload(Funcionario.lojas))
        .filter_by(ativo=True).all()
    )
    lojas = Loja.query.options(defer(Loja.planta_imagem)).filter_by(ativa=True).order_by(Loja.nome).all()

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

    h = agora()
    aniversariantes = [
        f for f in funcionarios
        if f.data_nascimento and f.data_nascimento.month == h.month
    ]
    aniversarios_casa = [
        f for f in funcionarios
        if f.data_admissao and f.data_admissao.month == h.month
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
@rh_required
def funcionarios():
    loja_id = request.args.get('loja', type=int)
    apenas_ativos = request.args.get('ativos', '1') == '1'
    view = request.args.get('view', 'cadastros')
    if view not in ('cadastros', 'acessos'):
        view = 'cadastros'

    query = Funcionario.query.options(
        joinedload(Funcionario.usuario),
        joinedload(Funcionario.cargo),
        selectinload(Funcionario.lojas),
    )
    if apenas_ativos:
        query = query.filter_by(ativo=True)
    if loja_id:
        query = query.filter(Funcionario.lojas.any(Loja.id == loja_id))

    lista_completa = query.order_by(Funcionario.nome).all()
    lojas = Loja.query.options(defer(Loja.planta_imagem)).filter_by(ativa=True).order_by(Loja.nome).all()

    contas_livres, sugestoes, modulos_por_funcionario = [], {}, {}
    resumo_acessos = {'vinculados': 0, 'possiveis': 0,
                      'prontos': 0, 'sem_email': 0}
    lista = lista_completa
    filtro_acesso = request.args.get('acesso', 'pendentes')
    if filtro_acesso not in ('pendentes', 'vinculados', 'todos'):
        filtro_acesso = 'pendentes'
    if view == 'acessos':
        from app.services import treino_acessos as acessos
        from app.services import treino_onboarding as onboarding

        contas_livres = acessos.contas_sem_vinculo()
        sugestoes = acessos.sugerir_contas(lista_completa, contas_livres)
        modulos_por_funcionario = onboarding.onboarding_lote(lista_completa)

        def _estado(f):
            if f.usuario_id:
                return 'vinculados'
            if f.id in sugestoes:
                return 'possiveis'
            if (f.email or '').strip():
                return 'prontos'
            return 'sem_email'

        for f in lista_completa:
            resumo_acessos[_estado(f)] += 1
        if filtro_acesso == 'pendentes':
            lista = [f for f in lista_completa if not f.usuario_id]
        elif filtro_acesso == 'vinculados':
            lista = [f for f in lista_completa if f.usuario_id]
        ordem = {'possiveis': 0, 'sem_email': 1,
                 'prontos': 2, 'vinculados': 3}
        lista.sort(key=lambda f: (ordem[_estado(f)], f.nome.lower()))

    return render_template('rh/funcionarios.html',
                           funcionarios=lista,
                           lojas=lojas,
                           loja_id=loja_id,
                           apenas_ativos=apenas_ativos,
                           view=view, filtro_acesso=filtro_acesso,
                           contas_livres=contas_livres,
                           sugestoes=sugestoes,
                           modulos_por_funcionario=modulos_por_funcionario,
                           resumo_acessos=resumo_acessos)


@rh_bp.route('/lideranca')
@login_required
@rh_required
def lideranca():
    """Configura a hierarquia e o checklist observado pelos líderes."""
    from app.models import TreinoChecklistAplicacao, TreinoTrilha
    from app.services import treino_lideranca as lideranca_svc

    funcionarios = (Funcionario.query
                    .options(joinedload(Funcionario.cargo),
                             joinedload(Funcionario.usuario),
                             joinedload(Funcionario.lider))
                    .filter_by(ativo=True).order_by(Funcionario.nome).all())
    lideres = [f for f in funcionarios if f.usuario_id]
    trilhas = TreinoTrilha.query.order_by(TreinoTrilha.ordem).all()
    checklists = {}
    for checklist in TreinoChecklistAplicacao.query.order_by(
            TreinoChecklistAplicacao.id).all():
        checklists.setdefault(checklist.trilha_id, checklist)
    equipes = {}
    for funcionario in funcionarios:
        if funcionario.lider_id:
            equipes.setdefault(funcionario.lider_id, []).append(funcionario)
    return render_template(
        'rh/lideranca.html', funcionarios=funcionarios, lideres=lideres,
        trilhas=trilhas, checklists=checklists, equipes=equipes,
        itens_ativos=lideranca_svc.itens_ativos)


@rh_bp.route('/lideranca/vinculos', methods=['POST'])
@login_required
@rh_required
def lideranca_vinculos():
    from app.services import treino_lideranca as lideranca_svc

    funcionarios = Funcionario.query.filter_by(ativo=True).order_by(
        Funcionario.nome).all()
    vinculos = {
        funcionario.id: request.form.get(f'lider_{funcionario.id}', type=int)
        for funcionario in funcionarios
    }
    try:
        alteracoes = lideranca_svc.salvar_vinculos(funcionarios, vinculos)
    except lideranca_svc.LiderancaError as exc:
        db.session.rollback()
        flash(str(exc), 'warning')
    else:
        flash(f'Liderança atualizada: {alteracoes} vínculo(s) alterado(s).',
              'success')
    return redirect(url_for('rh.lideranca', _anchor='equipes'))


@rh_bp.route('/lideranca/preenchimento')
@login_required
def lideranca_preenchimento():
    """Tela estreita para Dakson organizar a equipe, sem abrir o RH inteiro."""
    if not current_user.pode_organizar_equipe():
        abort(403)
    from app.services import treino_lideranca as lideranca_svc

    funcionarios = (Funcionario.query
                    .options(joinedload(Funcionario.cargo),
                             joinedload(Funcionario.usuario),
                             joinedload(Funcionario.lider),
                             selectinload(Funcionario.lojas))
                    .filter_by(ativo=True).order_by(Funcionario.nome).all())
    lojas = (Loja.query.options(defer(Loja.planta_imagem))
             .filter_by(ativa=True).order_by(Loja.nome).all())
    unidades = lideranca_svc.unidades_principais(funcionarios)
    lideres = [funcionario for funcionario in funcionarios
               if funcionario.usuario_id]
    return render_template(
        'rh/lideranca_preenchimento.html', funcionarios=funcionarios,
        lideres=lideres, lojas=lojas, unidades=unidades,
        periodos=lideranca_svc.PERIODOS_EQUIPE,
        com_lider=sum(1 for f in funcionarios if f.lider_id),
        com_unidade=sum(1 for f in funcionarios if unidades.get(f.id)),
        com_periodo=sum(1 for f in funcionarios
                        if f.periodo in lideranca_svc.PERIODOS_EQUIPE))


@rh_bp.route('/lideranca/preenchimento/salvar', methods=['POST'])
@login_required
def lideranca_preenchimento_salvar():
    if not current_user.pode_organizar_equipe():
        abort(403)
    from app.services import treino_lideranca as lideranca_svc

    funcionarios = (Funcionario.query.options(selectinload(Funcionario.lojas))
                    .filter_by(ativo=True).order_by(Funcionario.nome).all())
    vinculos = {f.id: request.form.get(f'lider_{f.id}', type=int)
                for f in funcionarios}
    unidades = {f.id: request.form.get(f'loja_{f.id}', type=int)
                for f in funcionarios}
    periodos = {f.id: request.form.get(f'periodo_{f.id}')
                for f in funcionarios}
    try:
        alteracoes = lideranca_svc.salvar_estrutura(
            funcionarios, vinculos, unidades, periodos)
    except lideranca_svc.LiderancaError as exc:
        db.session.rollback()
        flash(str(exc), 'warning')
    else:
        total = sum(alteracoes.values())
        flash(f'Organização salva: {total} campo(s) atualizado(s).', 'success')
    return redirect(url_for('rh.lideranca_preenchimento'))


def _dados_organograma():
    """Monta a árvore uma vez para a tela e para o PDF."""
    from app.services import treino_lideranca as lideranca_svc

    funcionarios = (Funcionario.query
                    .options(joinedload(Funcionario.cargo),
                             joinedload(Funcionario.lider),
                             selectinload(Funcionario.lojas))
                    .filter_by(ativo=True).order_by(Funcionario.nome).all())
    lojas = (Loja.query.options(defer(Loja.planta_imagem))
             .filter_by(ativa=True).order_by(Loja.nome).all())
    unidades = lideranca_svc.unidades_principais(funcionarios)
    lojas_por_id = {loja.id: loja for loja in lojas}
    por_id = {funcionario.id: funcionario for funcionario in funcionarios}
    filhos_por_lider = {funcionario.id: [] for funcionario in funcionarios}
    raizes = []

    for funcionario in funcionarios:
        if funcionario.lider_id in por_id:
            filhos_por_lider[funcionario.lider_id].append(funcionario)
        else:
            raizes.append(funcionario)

    def _ordem(funcionario):
        return (-len(filhos_por_lider[funcionario.id]),
                funcionario.nome.casefold())

    raizes.sort(key=_ordem)
    for liderados in filhos_por_lider.values():
        liderados.sort(key=_ordem)
    lideres = [f for f in funcionarios if filhos_por_lider[f.id]]
    pendencias = sum(
        1 for f in funcionarios
        if not unidades.get(f.id)
        or f.periodo not in lideranca_svc.PERIODOS_EQUIPE)
    maior_equipe = max(
        (len(filhos_por_lider[f.id]) for f in lideres), default=0)

    return {
        'funcionarios': funcionarios,
        'lojas': lojas,
        'lojas_por_id': lojas_por_id,
        'unidades': unidades,
        'filhos_por_lider': filhos_por_lider,
        'raizes': raizes,
        'total_lideres': len(lideres),
        'pendencias': pendencias,
        'maior_equipe': maior_equipe,
    }


@rh_bp.route('/lideranca/organograma')
@login_required
def lideranca_organograma():
    """Organograma vivo a partir dos vínculos preenchidos pela liderança."""
    if not current_user.pode_organizar_equipe():
        abort(403)
    return render_template(
        'rh/lideranca_organograma.html', **_dados_organograma())


@rh_bp.route('/lideranca/organograma.pdf')
@login_required
def lideranca_organograma_pdf():
    """Exporta a hierarquia completa em uma página ampla e horizontal."""
    if not current_user.pode_organizar_equipe():
        abort(403)
    try:
        from app.services.lideranca_organograma_pdf import gerar_pdf

        pdf = gerar_pdf(_dados_organograma(), agora())
    except Exception:  # noqa: BLE001 - falha nativa do renderizador
        current_app.logger.exception('Falha ao exportar organograma em PDF')
        flash('Não foi possível gerar o PDF agora. Tente novamente.', 'warning')
        return redirect(url_for('rh.lideranca_organograma'))
    nome = f'organograma-equipe-{hoje_brt().isoformat()}.pdf'
    return Response(pdf, mimetype='application/pdf', headers={
        'Content-Disposition': f'attachment; filename="{nome}"'})


@rh_bp.route('/lideranca/checklist/<int:trilha_id>', methods=['POST'])
@login_required
@rh_required
def lideranca_checklist(trilha_id):
    from app.models import TreinoTrilha
    from app.services import treino_lideranca as lideranca_svc

    trilha = db.session.get(TreinoTrilha, trilha_id) or abort(404)
    linhas = (request.form.get('itens') or '').splitlines()
    try:
        checklist = lideranca_svc.salvar_checklist(
            trilha, request.form.get('descricao'), linhas)
    except lideranca_svc.LiderancaError as exc:
        flash(str(exc), 'warning')
    else:
        flash(f'Checklist de “{trilha.nome}” salvo com '
              f'{len(lideranca_svc.itens_ativos(checklist))} item(ns).',
              'success')
    return redirect(url_for('rh.lideranca', _anchor=f'checklist-{trilha.id}'))


@rh_bp.route('/funcionarios/<int:id>/acesso', methods=['POST'])
@login_required
@rh_required
def funcionario_acesso(id):
    """Gerencia e-mail e conta do funcionário diretamente na lista do RH."""
    from app.models import Usuario
    from app.services import treino_acessos as acessos

    f = Funcionario.query.get_or_404(id)
    acao = (request.form.get('acao') or '').strip()
    email = request.form.get('email')
    usuario_id = request.form.get('usuario_id', type=int)

    def _voltar():
        params = {'view': 'acessos',
                  'acesso': request.form.get('filtro_acesso', 'pendentes')}
        loja = request.form.get('loja', type=int)
        if loja:
            params['loja'] = loja
        if request.form.get('apenas_ativos') == '1':
            params['ativos'] = '1'
        return redirect(url_for('rh.funcionarios', _anchor=f'acesso-{f.id}',
                                **params))

    def _erro_email(resultado):
        motivo = resultado.get('motivo')
        if motivo == 'email_invalido':
            return 'Informe um e-mail válido.'
        if motivo == 'email_de_outro_funcionario':
            outro = resultado.get('funcionario')
            return (f'Este e-mail já está na ficha de {outro.nome}. '
                    'Confira antes de continuar.')
        if motivo == 'email_de_outra_conta':
            usuario = resultado.get('usuario')
            return (f'Já existe a conta "{usuario.login}" usando este '
                    'e-mail. Se ela pertence ao funcionário, selecione-a '
                    'e clique em “Vincular conta”.')
        return None

    if acao == 'salvar_email':
        r = acessos.sincronizar_email(f, email, usuario=f.usuario)
        erro = _erro_email(r)
        if erro:
            flash(erro, 'warning')
        else:
            flash(f'E-mail de {f.nome} atualizado. O login existente não '
                  'foi alterado.', 'success')
        return _voltar()

    if acao == 'reenviar':
        if not f.ativo:
            flash(f'{f.nome} está desligado no RH. Reative a ficha antes de '
                  'reenviar o acesso.', 'warning')
            return _voltar()
        email_salvo = acessos.sincronizar_email(f, email, usuario=f.usuario)
        erro = _erro_email(email_salvo)
        if erro:
            flash(erro, 'warning')
            return _voltar()
        r = acessos.reenviar_acesso(f)
        if r.get('ok'):
            flash(f'Novo acesso de {f.nome} enviado para {r["email"]}. '
                  'A senha anterior deixou de funcionar.', 'success')
        elif r.get('motivo') == 'email_falhou':
            flash(f'Não alterei a senha de {f.nome}: o e-mail não foi '
                  f'aceito ({r.get("email_erro")}).', 'warning')
        elif r.get('motivo') == 'sem_conta':
            flash(f'{f.nome} ainda não possui uma conta vinculada.', 'warning')
        else:
            flash(f'Não foi possível reenviar o acesso de {f.nome}.',
                  'warning')
        return _voltar()

    if not f.ativo:
        flash(f'{f.nome} está desligado no RH. Reative a ficha antes de '
              'liberar acesso.', 'warning')
        return _voltar()

    if acao == 'vincular':
        usuario = db.session.get(Usuario, usuario_id or 0)
        r = acessos.vincular_conta(f, usuario, email=email)
        erro = _erro_email(r)
        if erro:
            flash(erro, 'warning')
        elif r.get('ok'):
            flash(f'{f.nome} foi vinculado à conta "{usuario.login}". '
                  'O login e a senha continuam os mesmos; o acesso ao '
                  'treinamento já está liberado.', 'success')
        elif r.get('motivo') == 'conta_em_uso':
            flash('Essa conta já pertence a outro funcionário.', 'danger')
        elif r.get('motivo') in ('owner', 'papel_invalido'):
            flash('Essa é uma conta de gestão e não pode ser vinculada como '
                  'conta do funcionário.', 'danger')
        elif r.get('motivo') == 'ja_tem':
            flash(f'{f.nome} já possui uma conta vinculada.', 'info')
        else:
            flash('Selecione a conta existente deste funcionário.', 'warning')
        return _voltar()

    if acao == 'gerar':
        if usuario_id:
            flash('Há uma conta existente selecionada. Clique em “Vincular '
                  'conta” para preservar o login e a senha atuais.', 'warning')
            return _voltar()
        email_salvo = acessos.sincronizar_email(f, email)
        erro = _erro_email(email_salvo)
        if erro:
            flash(erro, 'warning')
            return _voltar()
        r = acessos.gerar_acesso(
            f, somente_treino=request.form.get('somente_treino') == '1')
        if r['motivo'] == 'criado':
            if r.get('email_ok'):
                flash(f'Acesso de {f.nome} criado. O login e a senha '
                      f'provisória foram enviados para {f.email}.', 'success')
            else:
                flash(f'Acesso criado, mas o e-mail falhou '
                      f'({r.get("email_erro")}). Senha provisória: '
                      f'{r.get("senha")} — entregue manualmente.', 'warning')
        elif r['motivo'] == 'vinculado':
            flash(f'{f.nome} já possuía uma conta com este e-mail; ela foi '
                  'vinculada sem trocar a senha.', 'success')
        elif r['motivo'] == 'ja_tem':
            flash(f'{f.nome} já possui acesso.', 'info')
        else:
            flash('Não foi possível criar o acesso. Confira o e-mail e se '
                  'já existe uma conta para esta pessoa.', 'warning')
        return _voltar()

    flash('Escolha se deseja vincular uma conta ou criar um novo acesso.',
          'warning')
    return _voltar()


@rh_bp.route('/funcionarios/acessos/reenviar-todos', methods=['POST'])
@login_required
@rh_required
def funcionarios_reenviar_acessos():
    """Reemite a senha de todo funcionário ativo com conta vinculada.

    Exige confirmação textual porque a ação invalida as senhas anteriores.
    Cada troca é confirmada separadamente e somente após o Postmark aceitar o
    respectivo e-mail; uma falha não bloqueia os demais funcionários.
    """
    if (request.form.get('confirmacao') or '').strip().upper() != 'REENVIAR':
        flash('Reenvio cancelado. Digite REENVIAR para confirmar a troca das '
              'senhas.', 'warning')
        return redirect(url_for('rh.funcionarios', view='acessos',
                                acesso='vinculados', ativos='1'))

    from app.services import treino_acessos as acessos
    funcionarios = (Funcionario.query
                    .options(joinedload(Funcionario.usuario))
                    .filter(Funcionario.ativo.is_(True),
                            Funcionario.usuario_id.isnot(None))
                    .order_by(Funcionario.nome).all())
    enviados, problemas = 0, []
    for funcionario in funcionarios:
        resultado = acessos.reenviar_acesso(funcionario)
        if resultado.get('ok'):
            enviados += 1
            continue
        motivo = resultado.get('motivo')
        if motivo == 'sem_email':
            detalhe = 'sem e-mail válido'
        elif motivo == 'email_falhou':
            detalhe = f'e-mail recusado ({resultado.get("email_erro")})'
        elif motivo == 'owner':
            detalhe = 'conta do proprietário não foi alterada'
        else:
            detalhe = 'não foi possível confirmar o novo acesso'
        problemas.append(f'{funcionario.nome}: {detalhe}')

    if enviados:
        flash(f'{enviados} novo(s) acesso(s) aceito(s) pelo serviço de '
              'e-mail. As senhas anteriores dessas contas deixaram de '
              'funcionar.', 'success')
    else:
        flash('Nenhum novo acesso foi enviado.', 'warning')
    if problemas:
        resumo = '; '.join(problemas[:10])
        if len(problemas) > 10:
            resumo += f'; e mais {len(problemas) - 10} problema(s)'
        flash(f'Não enviados: {resumo}. As senhas dessas pessoas não foram '
              'alteradas.', 'warning')
    return redirect(url_for('rh.funcionarios', view='acessos',
                            acesso='vinculados', ativos='1'))


@rh_bp.route('/funcionarios/novo', methods=['GET', 'POST'])
@login_required
@rh_required
def novo_funcionario():
    if request.method == 'POST':
        # Salario so e' aceito do form se user e' owner; senao usa 0
        # (admin sem is_owner nao deve poder definir salarios)
        salario_in = parse_float_br(request.form.get('salario_base', ''), default=0) \
            if getattr(current_user, 'is_owner', False) else 0
        func = Funcionario(
            nome=request.form.get('nome', '').strip(),
            cpf=request.form.get('cpf', '').strip(),
            funcao=request.form.get('funcao', '').strip() or None,
            salario_base=salario_in,
            tem_cargo_confianca='tem_cargo_confianca' in request.form,
            premiacao=parse_float_br(request.form.get('premiacao', ''), default=0),
            vt_dia=parse_float_br(request.form.get('vt_dia', ''), default=0),
            vr_dia=parse_float_br(request.form.get('vr_dia', ''), default=22),
            dias_trabalhados=int(request.form.get('dias_trabalhados', '26') or 26),
            hora_extra_pct=parse_float_br(request.form.get('hora_extra_pct', ''), default=55),
            horas_extras=parse_float_br(request.form.get('horas_extras', ''), default=0),
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
        # Fichas antigas e integrações ainda enviam a função como texto. Se já
        # existe um Cargo equivalente, mantém os dois cadastros conectados.
        from app.services import rh_cargos
        rh_cargos.associar_funcionario(func)
        db.session.commit()
        flash(f'Funcionário "{func.nome}" cadastrado!', 'success')
        return redirect(url_for('rh.detalhe_funcionario', id=func.id))

    lojas = Loja.query.options(defer(Loja.planta_imagem)).filter_by(ativa=True).order_by(Loja.nome).all()
    return render_template('rh/funcionario_form.html', func=None, lojas=lojas)


# ── Pré-cadastro por QR (23/07/2026) ──────────────────────────────────────

@rh_bp.route('/pre-cadastros')
@login_required
@rh_required
def pre_cadastros():
    """QR do formulário público + lista dos pré-cadastros pendentes pra promover."""
    from app.models import Funcionario
    from app.services import precadastro as pre_svc
    from app.services import qrcode_svc
    base = (current_app.config.get('APP_BASE_URL') or '').rstrip('/')
    url_form = (base + url_for('precadastro.form')) if base \
        else url_for('precadastro.form', _external=True)
    qr = qrcode_svc.gerar_png_data_url(url_form, box_size=8)
    pendentes = pre_svc.pendentes()
    # Vincular a funcionário EXISTENTE (05/08/2026): quem veio da folha já
    # está no RH — o select lista os ativos e a sugestão por nome pré-seleciona.
    funcionarios = (Funcionario.query.filter_by(ativo=True)
                    .order_by(Funcionario.nome).all())
    # Conta do sistema JÁ existente (caso Marina): papéis operacionais sem
    # funcionário vinculado — pro segundo vínculo Funcionario↔Usuario.
    from app.models import Usuario
    usuarios = [u for u in (Usuario.query
                            .filter(Usuario.papel.in_(
                                sorted(pre_svc._PAPEIS_VINCULAVEIS)))
                            .order_by(Usuario.nome).all())
                if not getattr(u, 'is_owner', False)
                and getattr(u, 'funcionario', None) is None]
    sugestoes, sugestoes_usuario = {}, {}
    for p in pendentes:
        s = pre_svc.sugerir_funcionario(p, funcionarios)
        sugestoes[p.id] = s.id if s else None
        su = pre_svc.sugerir_funcionario(p, usuarios)  # mesmo matcher (.nome)
        sugestoes_usuario[p.id] = su.id if su else None
    return render_template('rh/pre_cadastros.html',
                           pendentes=pendentes, funcionarios=funcionarios,
                           usuarios=usuarios, sugestoes=sugestoes,
                           sugestoes_usuario=sugestoes_usuario,
                           url_form=url_form, qr=qr)


@rh_bp.route('/pre-cadastros/<int:id>/promover', methods=['POST'])
@login_required
@rh_required
def pre_cadastro_promover(id):
    from app.models import PreCadastroFuncionario
    from app.services import precadastro as pre_svc
    pre = PreCadastroFuncionario.query.get_or_404(id)
    if pre.processado_em:
        flash('Esse pré-cadastro já foi processado.', 'warning')
        return redirect(url_for('rh.pre_cadastros'))
    func, erro = pre_svc.promover(pre, request.form.get('cpf', ''))
    if erro:
        flash(erro, 'warning')
        return redirect(url_for('rh.pre_cadastros'))
    flash(f'Funcionário "{func.nome}" criado — complete cargo/salário.',
          'success')
    return redirect(url_for('rh.detalhe_funcionario', id=func.id))


@rh_bp.route('/pre-cadastros/<int:id>/vincular', methods=['POST'])
@login_required
@rh_required
def pre_cadastro_vincular(id):
    """Vincula o pré-cadastro a um funcionário JÁ existente no RH (leva
    e-mail/telefone pra ficha) e, se marcado, gera o acesso ao treinamento
    na mesma tacada (senha provisória por e-mail)."""
    from app.models import Funcionario, PreCadastroFuncionario, Usuario
    from app.services import precadastro as pre_svc
    pre = PreCadastroFuncionario.query.get_or_404(id)
    func = db.session.get(Funcionario,
                          request.form.get('funcionario_id', type=int) or 0)
    usuario = None
    usuario_id = request.form.get('usuario_id', type=int)
    if usuario_id:
        usuario = db.session.get(Usuario, usuario_id)
        if usuario is None:
            flash('Conta do sistema não encontrada.', 'warning')
            return redirect(url_for('rh.pre_cadastros'))
    gerar = request.form.get('gerar_acesso') == '1'
    func, acesso, erro = pre_svc.vincular(pre, func, gerar_acesso_treino=gerar,
                                          usuario=usuario)
    if erro:
        flash(erro, 'warning')
        return redirect(url_for('rh.pre_cadastros'))
    partes = [f'Pré-cadastro vinculado a "{func.nome}" — e-mail e telefone '
              'atualizados na ficha.']
    if acesso and acesso.get('email_substituido'):
        partes.append(f'(o e-mail anterior era '
                      f'{acesso["email_substituido"]})')
    motivo_conta = (acesso or {}).get('motivo')
    if motivo_conta == 'conta_existente':
        partes.append(f'Conta do sistema "{acesso["usuario"].login}" '
                      'vinculada — ela usa o login e a senha de sempre '
                      '(nada foi criado nem enviado).')
        flash(' '.join(partes), 'success')
        return redirect(url_for('rh.pre_cadastros'))
    if gerar:
        motivo = (acesso or {}).get('motivo')
        if motivo == 'criado':
            partes.append(f'Acesso ao treinamento criado — a senha foi '
                          f'enviada para {func.email}.')
        elif motivo == 'vinculado':
            partes.append('Já existia conta com esse e-mail — vinculada ao '
                          'funcionário.')
        elif motivo == 'ja_tem':
            partes.append('Este funcionário já tinha acesso ao sistema.')
        elif motivo == 'conta_de_outro_papel':
            flash(' '.join(partes), 'success')
            flash('NÃO gerei o acesso: esse e-mail pertence a uma conta de '
                  'admin/gestor — resolva em Treinamento → Acessos.',
                  'warning')
            return redirect(url_for('rh.pre_cadastros'))
        elif motivo == 'email_em_uso':
            flash(' '.join(partes), 'success')
            flash('NÃO gerei o acesso: esse e-mail já é o login de OUTRO '
                  'funcionário — confira se não há duplicata no RH.',
                  'warning')
            return redirect(url_for('rh.pre_cadastros'))
    flash(' '.join(partes), 'success')
    return redirect(url_for('rh.pre_cadastros'))


@rh_bp.route('/pre-cadastros/<int:id>/descartar', methods=['POST'])
@login_required
@rh_required
def pre_cadastro_descartar(id):
    from app.models import PreCadastroFuncionario
    from app.services import precadastro as pre_svc
    pre = PreCadastroFuncionario.query.get_or_404(id)
    pre_svc.descartar(pre)
    flash('Pré-cadastro descartado.', 'success')
    return redirect(url_for('rh.pre_cadastros'))


@rh_bp.route('/funcionarios/<int:id>')
@login_required
@rh_required
def detalhe_funcionario(id):
    from app.services import treino_ledger, treino_painel

    func = Funcionario.query.get_or_404(id)
    lojas = Loja.query.options(defer(Loja.planta_imagem)).filter_by(ativa=True).order_by(Loja.nome).all()
    cargos_disp = Cargo.query.filter_by(ativo=True).order_by(Cargo.nome).all()
    feedbacks = Feedback.query.filter_by(funcionario_id=id).order_by(Feedback.data.desc()).all()
    folhas = FolhaPagamento.query.filter_by(funcionario_id=id).order_by(
        FolhaPagamento.ano.desc(), FolhaPagamento.mes.desc()
    ).limit(12).all()

    return render_template('rh/funcionario_detalhe.html',
                           func=func, lojas=lojas, cargos_disponiveis=cargos_disp,
                           feedbacks=feedbacks, folhas=folhas,
                           treino_resumo=treino_painel.resumo_funcionario(
                               func, treino_ledger.temporada_ativa()))


@rh_bp.route('/funcionarios/<int:id>/salvar', methods=['POST'])
@login_required
@rh_required
def salvar_funcionario(id):
    func = Funcionario.query.get_or_404(id)

    func.nome = request.form.get('nome', '').strip() or func.nome
    novo_cpf = request.form.get('cpf', '').strip()
    if novo_cpf and novo_cpf != func.cpf:
        existente = Funcionario.query.filter(Funcionario.cpf == novo_cpf, Funcionario.id != func.id).first()
        if existente:
            flash(f'CPF já cadastrado para "{existente.nome}".', 'warning')
            return redirect(url_for('rh.detalhe_funcionario', id=func.id))
    func.cpf = novo_cpf or func.cpf
    cargo_id_raw = request.form.get('cargo_id', '').strip()
    func.cargo_id = int(cargo_id_raw) if cargo_id_raw else None
    # Sincroniza funcao (string legacy) com nome do cargo, pra compat com telas antigas
    if func.cargo_id:
        c = Cargo.query.get(func.cargo_id)
        if c:
            func.funcao = c.nome
            func.salario_base = c.salario_base  # cache, calculo usa salario_efetivo()
    func.tem_cargo_confianca = 'tem_cargo_confianca' in request.form
    func.premiacao = parse_float_br(request.form.get('premiacao', ''), default=0)
    func.vt_dia = parse_float_br(request.form.get('vt_dia', ''), default=0)
    func.vr_dia = parse_float_br(request.form.get('vr_dia', ''), default=22)
    func.dias_trabalhados = int(request.form.get('dias_trabalhados', '26') or 26)
    func.hora_extra_pct = parse_float_br(request.form.get('hora_extra_pct', ''), default=55)
    func.horas_extras = parse_float_br(request.form.get('horas_extras', ''), default=0)
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
@rh_required
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
@rh_required
def excluir_feedback(id, fb_id):
    fb = Feedback.query.get_or_404(fb_id)
    db.session.delete(fb)
    db.session.commit()
    flash('Feedback removido.', 'success')
    return redirect(url_for('rh.detalhe_funcionario', id=id))


@rh_bp.route('/lojas')
@login_required
@rh_required
def lojas():
    lista = Loja.query.options(defer(Loja.planta_imagem)).order_by(Loja.nome).all()
    return render_template('rh/lojas.html', lojas=lista)


# ── Cargos ──

@rh_bp.route('/cargos')
@login_required
@owner_required
def cargos():
    lista = Cargo.query.order_by(Cargo.nome).all()
    return render_template('rh/cargos.html', cargos=lista)


@rh_bp.route('/cargos/salvar', methods=['POST'])
@login_required
@owner_required
def salvar_cargos():
    ids = request.form.getlist('cargo_id[]')
    nomes = request.form.getlist('cargo_nome[]')
    salarios = request.form.getlist('cargo_salario[]')
    descricoes = request.form.getlist('cargo_descricao[]')
    ativos = request.form.getlist('cargo_ativo[]')  # so chega quem foi marcado

    ativos_set = set(ativos)
    for i, nome in enumerate(nomes):
        nome = nome.strip()
        if not nome:
            continue
        salario = parse_float_br(salarios[i] if i < len(salarios) else '', default=0)
        descricao = descricoes[i].strip() if i < len(descricoes) else ''
        cid = ids[i].strip() if i < len(ids) else ''
        ativo = (cid or str(i)) in ativos_set

        if cid:
            c = Cargo.query.get(int(cid))
            if c:
                c.nome = nome
                c.salario_base = salario
                c.descricao = descricao or None
                c.ativo = ativo
        else:
            db.session.add(Cargo(nome=nome, salario_base=salario,
                                  descricao=descricao or None, ativo=ativo))

    db.session.commit()
    flash('Cargos salvos.', 'success')
    return redirect(url_for('rh.cargos'))


@rh_bp.route('/cargos/<int:id>/excluir', methods=['POST'])
@login_required
@owner_required
def excluir_cargo(id):
    c = Cargo.query.get_or_404(id)
    if c.funcionarios:
        flash(f'Cargo "{c.nome}" tem {len(c.funcionarios)} funcionario(s) vinculado(s); reatribua antes.', 'warning')
        return redirect(url_for('rh.cargos'))
    db.session.delete(c)
    db.session.commit()
    flash('Cargo removido.', 'success')
    return redirect(url_for('rh.cargos'))


@rh_bp.route('/lojas/salvar', methods=['POST'])
@login_required
@rh_required
def salvar_lojas():
    ids = request.form.getlist('loja_id[]')
    nomes = request.form.getlist('loja_nome[]')
    enderecos = request.form.getlist('loja_endereco[]')
    telefones = request.form.getlist('loja_telefone[]')
    pins = request.form.getlist('loja_pin[]')

    for i in range(len(nomes)):
        nome = nomes[i].strip()
        if not nome:
            continue
        endereco = enderecos[i].strip() if i < len(enderecos) else ''
        telefone = telefones[i].strip() if i < len(telefones) else ''
        pin = pins[i].strip() if i < len(pins) else ''
        lid = ids[i].strip() if i < len(ids) else ''

        if lid:
            loja = Loja.query.get(int(lid))
            if loja:
                loja.nome = nome
                loja.endereco = endereco or None
                loja.telefone = telefone or None
                loja.pin = pin or None
        else:
            db.session.add(Loja(
                nome=nome,
                endereco=endereco or None,
                telefone=telefone or None,
                pin=pin or None,
            ))

    db.session.commit()
    flash('Lojas salvas!', 'success')
    return redirect(url_for('rh.lojas'))


@rh_bp.route('/lojas/<int:id>/fiscal', methods=['POST'])
@login_required
@rh_required
def salvar_loja_fiscal(id):
    """Dados FISCAIS da loja (NF de transferência indústria→loja,
    20/07/2026): CNPJ + IE + endereço estruturado — a SEFAZ exige campos
    separados no destinatário da NF-e. Form próprio (fora da tabela densa
    de lojas) pra não inflar a tela do RH."""
    loja = Loja.query.get_or_404(id)
    # Truncado no tamanho da coluna: texto colado maior que o campo virava
    # DataError/500 no Postgres (achado B1 da revisão). Validação de
    # conteúdo (CNPJ 14 dígitos etc.) fica na emissão + badge da tela.
    _max = {'razao_social': 200, 'cnpj': 20, 'inscricao_estadual': 20,
            'endereco_logradouro': 200, 'endereco_numero': 20,
            'endereco_complemento': 100, 'endereco_bairro': 100,
            'endereco_cep': 9, 'endereco_cidade': 100}
    for campo, tam in _max.items():
        setattr(loja, campo,
                (request.form.get(campo) or '').strip()[:tam] or None)
    loja.endereco_uf = \
        (request.form.get('endereco_uf') or '').strip().upper()[:2] or None
    # Dispensa de NF POR LOJA (dono 20/07/2026): o scan do QR pula a
    # emissao em TODOS os pedidos desta loja. Gesto fica AQUI (RH/admin)
    # de proposito — motorista/padeiro nao tem essa opcao.
    loja.nf_dispensada = request.form.get('nf_dispensada') == '1'
    # Dias em que a loja ABRE (dono 27/07/2026). Checkboxes -> digitos do
    # date.weekday() ordenados ('56' = sab+dom). NENHUM marcado = None =
    # abre todo dia (fail-open: a loja segue sendo cobrada por sobras).
    # `getlist` + whitelist: valor forjado no POST nao entra na coluna.
    _dias = sorted({d for d in request.form.getlist('dias_funcionamento')
                    if d in '0123456' and len(d) == 1})
    loja.dias_funcionamento = ''.join(_dias) or None
    db.session.commit()
    if loja.nf_dispensada:
        flash(f'Loja "{loja.nome}": NF de transferência DISPENSADA — o '
              'scan do QR não emite nota pros pedidos dela.', 'warning')
        return redirect(url_for('rh.lojas'))
    if loja.fiscal_completo:
        flash(f'Dados fiscais da loja "{loja.nome}" salvos — pronta pra NF '
              'de transferência.', 'success')
    else:
        flash(f'Dados fiscais da loja "{loja.nome}" salvos, mas ainda '
              'INCOMPLETOS pra NF (CNPJ com 14 dígitos + endereço completo).',
              'warning')
    return redirect(url_for('rh.lojas'))


@rh_bp.route('/lojas/excluir/<int:id>', methods=['POST'])
@login_required
@rh_required
def excluir_loja(id):
    loja = Loja.query.get_or_404(id)
    if loja.funcionarios:
        flash(f'Loja "{loja.nome}" tem {len(loja.funcionarios)} funcionário(s). Remova-os primeiro.', 'warning')
        return redirect(url_for('rh.lojas'))
    if Posicao.query.filter_by(loja_id=id).count():
        flash(f'Loja "{loja.nome}" tem posições na escala. Remova-as primeiro.', 'warning')
        return redirect(url_for('rh.lojas'))
    nome = loja.nome
    db.session.delete(loja)
    db.session.commit()
    flash(f'Loja "{nome}" excluída!', 'success')
    return redirect(url_for('rh.lojas'))


@rh_bp.route('/folha')
@login_required
@owner_required
def folha():
    mes = request.args.get('mes', type=int, default=agora().month)
    ano = request.args.get('ano', type=int, default=agora().year)

    folhas = FolhaPagamento.query.filter_by(mes=mes, ano=ano).all()
    funcionarios_ativos = Funcionario.query.filter_by(ativo=True).order_by(Funcionario.nome).all()

    return render_template('rh/folha.html',
                           folhas=folhas,
                           funcionarios=funcionarios_ativos,
                           mes=mes, ano=ano)


@rh_bp.route('/folha/gerar', methods=['POST'])
@login_required
@owner_required
def gerar_folha():
    mes = int(request.form.get('mes', agora().month))
    ano = int(request.form.get('ano', agora().year))

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
            cargo_confianca=f.valor_cargo_confianca(),
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
@owner_required
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
@rh_required
def escala():
    modo = request.args.get('modo', 'tabela')
    lojas = Loja.query.options(defer(Loja.planta_imagem)).filter_by(ativa=True).order_by(Loja.nome).all()

    # Modo tabela: somente posições manuais
    posicoes = Posicao.query.filter(
        Posicao.origem != 'mapa'
    ).order_by(Posicao.loja_id, Posicao.periodo, Posicao.ordem).all()

    grid = {}
    for pos in posicoes:
        lnome = pos.loja.nome
        if lnome not in grid:
            grid[lnome] = {}
        grid[lnome].setdefault(pos.periodo, []).append(pos)

    sem_loja = Funcionario.query.filter(
        Funcionario.ativo == True,
        ~Funcionario.lojas.any()
    ).order_by(Funcionario.nome).all()

    pendentes = Funcionario.query.filter_by(
        ativo=True, cadastro_pendente=True
    ).order_by(Funcionario.nome).all()

    todos_func = Funcionario.query.filter_by(ativo=True).order_by(Funcionario.nome).all()

    # Lojas com planta para modo mapa
    lojas_com_planta = [l for l in lojas if l.planta_imagem]

    return render_template('rh/escala.html',
                           modo=modo,
                           grid=grid,
                           lojas=lojas,
                           lojas_com_planta=lojas_com_planta,
                           sem_loja=sem_loja,
                           pendentes=pendentes,
                           todos_func=todos_func)


@rh_bp.route('/escala/<int:pos_id>/atribuir', methods=['POST'])
@login_required
@rh_required
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
@rh_required
def nova_posicao():
    loja_id = int(request.form.get('loja_id'))
    periodo = request.form.get('periodo', 'Manhã')
    nome_pos = request.form.get('nome_posicao', '').strip()

    if not nome_pos:
        flash('Nome da posição é obrigatório.', 'warning')
        return redirect(url_for('rh.escala'))

    existente = Posicao.query.filter_by(loja_id=loja_id, periodo=periodo, nome_posicao=nome_pos).first()
    if existente:
        flash(f'Posição "{nome_pos}" já existe nesta loja/período.', 'warning')
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
@rh_required
def excluir_posicao(pos_id):
    pos = Posicao.query.get_or_404(pos_id)
    db.session.delete(pos)
    db.session.commit()
    flash('Posição removida.', 'success')
    return redirect(url_for('rh.escala'))


@rh_bp.route('/folha/<int:folha_id>/pdf')
@login_required
@owner_required
def holerite_pdf(folha_id):
    from app.services.pdf import gerar_holerite
    folha = FolhaPagamento.query.get_or_404(folha_id)
    pdf_bytes = gerar_holerite(folha)
    nome_arq = f'holerite_{folha.funcionario.nome.replace(" ", "_")}_{folha.mes:02d}_{folha.ano}.pdf'
    return Response(pdf_bytes, mimetype='application/pdf',
                    headers={'Content-Disposition': f'inline; filename="{nome_arq}"'})


@rh_bp.route('/folha/<int:folha_id>/excluir', methods=['POST'])
@login_required
@owner_required
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
@rh_required
def excluir_folha_mes():
    mes = int(request.form.get('mes', 0))
    ano = int(request.form.get('ano', 0))
    qtd = FolhaPagamento.query.filter_by(mes=mes, ano=ano).delete()
    db.session.commit()
    flash(f'Folha de {mes:02d}/{ano} excluída ({qtd} registros).', 'success')
    return redirect(url_for('rh.folha', mes=mes, ano=ano))


@rh_bp.route('/atestado/novo', methods=['POST'])
@login_required
@rh_required
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
        if arquivo.mimetype not in ALLOWED_MIMETYPES:
            flash('Tipo de arquivo não permitido. Use imagem (JPG, PNG) ou PDF.', 'warning')
            return redirect(url_for('rh.dashboard'))
        atestado.arquivo = arquivo.read()
        atestado.arquivo_nome = secure_filename(arquivo.filename) or 'atestado'
        atestado.arquivo_mimetype = arquivo.mimetype

    db.session.add(atestado)
    db.session.commit()
    flash('Atestado registrado!', 'success')
    return redirect(url_for('rh.dashboard'))


@rh_bp.route('/atestado/<int:id>/arquivo')
@login_required
@rh_required
def ver_atestado(id):
    at = Atestado.query.get_or_404(id)
    if not at.arquivo:
        abort(404)
    filename = quote(at.arquivo_nome or 'atestado')
    return Response(
        at.arquivo,
        mimetype=at.arquivo_mimetype or 'application/octet-stream',
        headers={'Content-Disposition': f"inline; filename*=UTF-8''{filename}"}
    )


@rh_bp.route('/atestado/<int:id>/excluir', methods=['POST'])
@login_required
@rh_required
def excluir_atestado(id):
    at = Atestado.query.get_or_404(id)
    db.session.delete(at)
    db.session.commit()
    flash('Atestado removido.', 'success')
    return redirect(url_for('rh.dashboard'))


@rh_bp.route('/feedback/novo', methods=['POST'])
@login_required
@rh_required
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


# ── Mapa da Loja ──

@rh_bp.route('/mapa')
@login_required
@rh_required
def mapa_index():
    loja = Loja.query.options(defer(Loja.planta_imagem)).filter_by(ativa=True).order_by(Loja.nome).first()
    if loja:
        return redirect(url_for('rh.mapa', loja_id=loja.id))
    flash('Cadastre uma loja primeiro.', 'warning')
    return redirect(url_for('rh.lojas'))


@rh_bp.route('/mapa/<int:loja_id>')
@login_required
@rh_required
def mapa(loja_id):
    loja = Loja.query.get_or_404(loja_id)
    lojas = Loja.query.options(defer(Loja.planta_imagem)).filter_by(ativa=True).order_by(Loja.nome).all()
    funcionarios_ativos = Funcionario.query.filter_by(ativo=True).order_by(Funcionario.nome).all()
    return render_template('rh/mapa.html', loja=loja, lojas=lojas,
                           funcionarios_ativos=funcionarios_ativos)


@rh_bp.route('/mapa/<int:loja_id>/planta', methods=['POST'])
@login_required
@rh_required
def upload_planta(loja_id):
    loja = Loja.query.get_or_404(loja_id)
    arquivo = request.files.get('planta')
    if not arquivo or not arquivo.filename:
        flash('Selecione uma imagem.', 'warning')
        return redirect(url_for('rh.mapa', loja_id=loja_id))

    mimetype = arquivo.content_type or ''
    if mimetype not in {'image/jpeg', 'image/png', 'image/webp'}:
        flash('Formato inválido. Use JPG, PNG ou WebP.', 'danger')
        return redirect(url_for('rh.mapa', loja_id=loja_id))

    loja.planta_imagem = arquivo.read()
    loja.planta_mimetype = mimetype
    db.session.commit()
    flash('Planta atualizada!', 'success')
    return redirect(url_for('rh.mapa', loja_id=loja_id))


@rh_bp.route('/mapa/<int:loja_id>/planta/excluir', methods=['POST'])
@login_required
@rh_required
def excluir_planta(loja_id):
    loja = Loja.query.get_or_404(loja_id)
    Posicao.query.filter(
        Posicao.nome_posicao.in_([s.nome for s in loja.slots]),
        Posicao.loja_id == loja_id,
    ).delete(synchronize_session=False)
    SlotMapa.query.filter_by(loja_id=loja_id).delete()
    loja.planta_imagem = None
    loja.planta_mimetype = None
    db.session.commit()
    flash('Planta e posições removidas.', 'success')
    return redirect(url_for('rh.mapa', loja_id=loja_id))


@rh_bp.route('/mapa/<int:loja_id>/planta.img')
@login_required
def ver_planta(loja_id):
    loja = Loja.query.get_or_404(loja_id)
    if not loja.planta_imagem:
        abort(404)
    return Response(loja.planta_imagem, mimetype=loja.planta_mimetype,
                    headers={'Cache-Control': 'max-age=3600'})


@rh_bp.route('/mapa/api/slots/<int:loja_id>')
@login_required
@rh_required
def api_slots(loja_id):
    periodo = request.args.get('periodo', 'manha')
    slots = SlotMapa.query.filter_by(loja_id=loja_id).all()

    posicoes = {p.nome_posicao: p for p in
                Posicao.query.filter_by(loja_id=loja_id, periodo=periodo).all()}

    resultado = []
    for s in slots:
        pos = posicoes.get(s.nome)
        resultado.append({
            'id': s.id,
            'nome': s.nome,
            'pos_x': s.pos_x,
            'pos_y': s.pos_y,
            'largura': s.largura or 15,
            'altura': s.altura or 8,
            'funcionario_id': pos.funcionario_id if pos else None,
            'funcionario_nome': pos.funcionario.nome if pos and pos.funcionario else None,
        })
    return jsonify(resultado)


@rh_bp.route('/mapa/api/slot', methods=['POST'])
@login_required
@rh_required
def api_criar_slot():
    data = request.get_json()
    slot = SlotMapa(
        loja_id=data['loja_id'],
        nome=data['nome'],
        pos_x=data['pos_x'],
        pos_y=data['pos_y'],
        largura=data.get('largura', 15),
        altura=data.get('altura', 8),
    )
    db.session.add(slot)
    db.session.commit()
    return jsonify({'id': slot.id, 'nome': slot.nome})


@rh_bp.route('/mapa/api/slot/<int:slot_id>', methods=['DELETE'])
@login_required
@rh_required
def api_excluir_slot(slot_id):
    slot = SlotMapa.query.get_or_404(slot_id)
    Posicao.query.filter_by(loja_id=slot.loja_id, nome_posicao=slot.nome).delete()
    db.session.delete(slot)
    db.session.commit()
    return jsonify({'ok': True})


@rh_bp.route('/mapa/api/alocar', methods=['POST'])
@login_required
@rh_required
def api_alocar():
    data = request.get_json()
    slot = SlotMapa.query.get_or_404(data['slot_id'])
    periodo = data['periodo']
    func_id = data.get('funcionario_id')

    pos = Posicao.query.filter_by(
        loja_id=slot.loja_id, periodo=periodo, nome_posicao=slot.nome
    ).first()

    if func_id:
        if not pos:
            pos = Posicao(loja_id=slot.loja_id, periodo=periodo, nome_posicao=slot.nome, origem='mapa')
            db.session.add(pos)
        pos.funcionario_id = int(func_id)
    else:
        if pos:
            db.session.delete(pos)

    db.session.commit()
    return jsonify({'ok': True})


# ── Férias e Folgas ──

@rh_bp.route('/ferias')
@login_required
@rh_required
def ferias():
    hoje = hoje_brt()
    mes = int(request.args.get('mes', hoje.month))
    ano = int(request.args.get('ano', hoje.year))

    registros = Ferias.query.filter(
        Ferias.data_fim >= datetime(ano, mes, 1).date(),
    ).order_by(Ferias.data_inicio).all()

    # Filtra por sobreposição com o mês
    import calendar
    ultimo_dia = calendar.monthrange(ano, mes)[1]
    inicio_mes = datetime(ano, mes, 1).date()
    fim_mes = datetime(ano, mes, ultimo_dia).date()
    registros = [r for r in registros if r.data_inicio <= fim_mes and r.data_fim >= inicio_mes]

    funcionarios = Funcionario.query.filter_by(ativo=True).order_by(Funcionario.nome).all()
    return render_template('rh/ferias.html', registros=registros,
                           funcionarios=funcionarios, mes=mes, ano=ano)


@rh_bp.route('/ferias/nova', methods=['POST'])
@login_required
@rh_required
def ferias_nova():
    func_id = int(request.form['funcionario_id'])
    data_inicio = datetime.strptime(request.form['data_inicio'], '%Y-%m-%d').date()
    data_fim = datetime.strptime(request.form['data_fim'], '%Y-%m-%d').date()
    tipo = request.form.get('tipo', 'ferias')
    obs = request.form.get('observacao', '').strip()

    f = Ferias(
        funcionario_id=func_id,
        data_inicio=data_inicio,
        data_fim=data_fim,
        tipo=tipo,
        observacao=obs or None,
        criado_por=current_user.id,
    )
    db.session.add(f)
    db.session.commit()
    flash('Registro de férias/folga criado.', 'success')
    return redirect(url_for('rh.ferias'))


@rh_bp.route('/ferias/<int:id>/excluir', methods=['POST'])
@login_required
@rh_required
def ferias_excluir(id):
    f = Ferias.query.get_or_404(id)
    db.session.delete(f)
    db.session.commit()
    flash('Registro removido.', 'success')
    return redirect(url_for('rh.ferias'))


# ── Ponto Simplificado ──

@rh_bp.route('/ponto')
@login_required
@rh_required
def ponto():
    hoje = hoje_brt()
    dia = request.args.get('dia', hoje.strftime('%Y-%m-%d'))

    try:
        dia_date = datetime.strptime(dia, '%Y-%m-%d').date()
    except ValueError:
        dia_date = hoje

    registros = RegistroPonto.query.filter_by(data=dia_date).all()
    reg_map = {r.funcionario_id: r for r in registros}
    funcionarios = Funcionario.query.filter_by(ativo=True).order_by(Funcionario.nome).all()

    return render_template('rh/ponto.html', funcionarios=funcionarios,
                           reg_map=reg_map, dia=dia_date)


@rh_bp.route('/ponto/registrar', methods=['POST'])
@login_required
@rh_required
def ponto_registrar():
    func_id = int(request.form['funcionario_id'])
    dia_str = request.form['dia']
    dia_date = datetime.strptime(dia_str, '%Y-%m-%d').date()

    entrada = request.form.get('entrada', '').strip()
    saida = request.form.get('saida', '').strip()
    entrada2 = request.form.get('entrada2', '').strip()
    saida2 = request.form.get('saida2', '').strip()

    reg = RegistroPonto.query.filter_by(funcionario_id=func_id, data=dia_date).first()
    if not reg:
        reg = RegistroPonto(funcionario_id=func_id, data=dia_date)
        db.session.add(reg)

    def parse_time(s):
        if not s:
            return None
        try:
            return datetime.strptime(s, '%H:%M').time()
        except ValueError:
            return None

    reg.entrada = parse_time(entrada)
    reg.saida = parse_time(saida)
    reg.entrada2 = parse_time(entrada2)
    reg.saida2 = parse_time(saida2)
    reg.editado_por = current_user.id

    total_min = 0
    if reg.entrada and reg.saida:
        t1 = reg.entrada.hour * 60 + reg.entrada.minute
        t2 = reg.saida.hour * 60 + reg.saida.minute
        total_min += max(0, t2 - t1)
    if reg.entrada2 and reg.saida2:
        t3 = reg.entrada2.hour * 60 + reg.entrada2.minute
        t4 = reg.saida2.hour * 60 + reg.saida2.minute
        total_min += max(0, t4 - t3)

    reg.horas_trabalhadas = total_min / 60.0
    reg.horas_extras = max(0, reg.horas_trabalhadas - 8)

    db.session.commit()
    flash('Ponto registrado.', 'success')
    return redirect(url_for('rh.ponto', dia=dia_str))


@rh_bp.route('/ponto/resumo')
@login_required
@rh_required
def ponto_resumo():
    hoje = hoje_brt()
    mes = int(request.args.get('mes', hoje.month))
    ano = int(request.args.get('ano', hoje.year))

    registros = RegistroPonto.query.filter(
        db.extract('month', RegistroPonto.data) == mes,
        db.extract('year', RegistroPonto.data) == ano,
    ).all()

    funcionarios = Funcionario.query.filter_by(ativo=True).order_by(Funcionario.nome).all()

    resumo = {}
    for f in funcionarios:
        resumo[f.id] = {'nome': f.nome, 'funcao': f.funcao, 'horas': 0, 'extras': 0, 'dias': 0}
    for r in registros:
        if r.funcionario_id in resumo:
            resumo[r.funcionario_id]['horas'] += r.horas_trabalhadas or 0
            resumo[r.funcionario_id]['extras'] += r.horas_extras or 0
            resumo[r.funcionario_id]['dias'] += 1

    return render_template('rh/ponto_resumo.html', resumo=resumo.values(), mes=mes, ano=ano)


@rh_bp.route('/contatos/importar', methods=['GET', 'POST'])
def contatos_importar():
    """Importa E-MAIL + CELULAR dos funcionários por planilha (05/08/2026).

    Nasceu na rodada de assinatura eletrônica do Regulamento Interno: o
    canal que sustenta a prova é o da FICHA, então a lista coletada pelo
    gerente entra por aqui — prévia primeiro, nada gravado sem marcar.
    Owner-only pelo gate do blueprint (_rh_restrito_ao_owner)."""
    from app.services import contatos_import
    if request.method == 'GET':
        return render_template('rh/contatos_importar.html', preview=None)
    arq = request.files.get('arquivo')
    if not arq or not arq.filename:
        flash('Escolha o arquivo de contatos (.xlsx).', 'warning')
        return redirect(url_for('rh.contatos_importar'))
    raw = arq.read()
    if len(raw) > 8 * 1024 * 1024:
        flash('Arquivo maior que 8MB — confira se é a planilha certa.',
              'danger')
        return redirect(url_for('rh.contatos_importar'))
    try:
        linhas, avisos = contatos_import.ler_planilha(raw)
    except Exception as e:  # noqa: BLE001 — parse de arquivo externo
        flash(f'Não consegui ler a planilha: {e}', 'danger')
        return redirect(url_for('rh.contatos_importar'))
    preview = contatos_import.comparar(linhas)
    return render_template('rh/contatos_importar.html', preview=preview,
                           avisos=avisos, nome_arquivo=arq.filename)


@rh_bp.route('/contatos/importar/aplicar', methods=['POST'])
def contatos_aplicar():
    """Aplica o que o dono MARCOU na prévia de contatos. Cada linha viaja
    como JSON no form e é re-validada no serviço."""
    import json as _json

    from app.services import contatos_import
    escolhas = {'atualizar': [], 'precadastro': [],
                'desligar': request.form.getlist('desligar')}
    for chave in ('atualizar', 'precadastro'):
        for bruto in request.form.getlist(chave):
            try:
                escolhas[chave].append(_json.loads(bruto))
            except ValueError:
                flash('Uma linha veio ilegível e foi pulada.', 'warning')
    if not any(escolhas.values()):
        flash('Nada marcado — nada foi alterado.', 'info')
        return redirect(url_for('rh.contatos_importar'))
    stats = contatos_import.aplicar(escolhas)
    partes = []
    if stats['atualizados']:
        partes.append(f"{stats['atualizados']} ficha(s) atualizada(s)")
    if stats['precadastros']:
        partes.append(f"{stats['precadastros']} pré-cadastro(s) criado(s) — "
                      'promova com o CPF em Pré-cadastros')
    if stats['desligados']:
        partes.append(f"{stats['desligados']} desligado(s)")
    flash('Contatos aplicados: ' + (', '.join(partes) or 'nenhuma mudança')
          + '.', 'success')
    for e in stats['erros']:
        flash(e, 'warning')
    return redirect(url_for('rh.funcionarios'))


@rh_bp.route('/folha/importar', methods=['GET', 'POST'])
def folha_importar():
    """Importa a FOLHA DE PAGAMENTO da contabilidade (xlsx) — 03/08/2026.

    GET = form de upload; POST = parse + PRÉVIA (nada gravado). Salário é
    dinheiro: só a rota /folha/aplicar grava, e só o que o dono marcar.
    Owner-only pelo gate do blueprint (_rh_restrito_ao_owner)."""
    from app.services import folha_import
    if request.method == 'GET':
        return render_template('rh/folha_importar.html', preview=None)
    arq = request.files.get('arquivo')
    if not arq or not arq.filename:
        flash('Escolha o arquivo da folha (.xlsx).', 'warning')
        return redirect(url_for('rh.folha_importar'))
    raw = arq.read()
    if len(raw) > 8 * 1024 * 1024:
        flash('Arquivo maior que 8MB — confira se é a folha certa.', 'danger')
        return redirect(url_for('rh.folha_importar'))
    try:
        linhas, avisos = folha_import.ler_folha(raw)
    except Exception as e:  # noqa: BLE001 — parse de arquivo externo
        flash(f'Não consegui ler a planilha: {e}', 'danger')
        return redirect(url_for('rh.folha_importar'))
    preview = folha_import.comparar(linhas)
    # `admissao` é date — vira ISO pros hidden JSON do form da prévia.
    for ln in linhas:
        ln['admissao'] = ln['admissao'].isoformat() if ln['admissao'] else None
    return render_template('rh/folha_importar.html', preview=preview,
                           avisos=avisos, nome_arquivo=arq.filename)


@rh_bp.route('/folha/importar/aplicar', methods=['POST'])
def folha_aplicar():
    """Aplica o que o dono MARCOU na prévia. Cada linha viaja como JSON no
    form e é re-validada no serviço — a prévia é tela, não autoridade."""
    import json as _json

    from app.services import folha_import
    escolhas = {'criar': [], 'atualizar': [], 'desligar': []}
    for chave in ('criar', 'atualizar'):
        for bruto in request.form.getlist(chave):
            try:
                escolhas[chave].append(_json.loads(bruto))
            except ValueError:
                flash('Uma linha veio ilegível e foi pulada.', 'warning')
    escolhas['desligar'] = request.form.getlist('desligar')
    if not any(escolhas.values()):
        flash('Nada marcado — nada foi alterado.', 'info')
        return redirect(url_for('rh.folha_importar'))
    stats = folha_import.aplicar(escolhas)
    partes = []
    if stats['criados']:
        partes.append(f"{stats['criados']} cadastrado(s)")
    if stats['atualizados']:
        partes.append(f"{stats['atualizados']} atualizado(s)")
    if stats['reativados']:
        partes.append(f"{stats['reativados']} reativado(s)")
    if stats['desligados']:
        partes.append(f"{stats['desligados']} desligado(s)")
    flash('Folha aplicada: ' + (', '.join(partes) or 'nenhuma mudança') + '.',
          'success')
    for e in stats['erros']:
        flash(e, 'warning')
    return redirect(url_for('rh.funcionarios'))
