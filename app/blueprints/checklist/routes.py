"""Checklist de loja (03/08/2026): preencher (turno), configurar (admin) e
conferir (admin). Regras de negócio em app/services/checklist_loja.py."""
from uuid import uuid4

from flask import abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError

from app.blueprints.checklist import checklist_bp
from app.constants import CHECKLIST_TIPO_LABEL, CHECKLIST_TIPOS
from app.decorators import admin_required, checklist_required
from app.extensions import db, limiter
from app.models import (
    ChecklistEdicao,
    ChecklistEnvio,
    ChecklistItemAjuste,
    ChecklistItemModelo,
    ChecklistPreenchimento,
    ChecklistResponsavel,
    ChecklistResposta,
    Funcionario,
    Loja,
)
from app.services import checklist_loja, checklist_responsaveis
from app.services.treino_lideranca import PERIODOS_EQUIPE
from app.utils import hoje


def _lojas_escolhiveis():
    return sorted(checklist_loja.lojas_operacionais(),
                  key=lambda lj: (lj.nome or '').casefold())


def _item_do_form():
    """Resolve o item_id do POST sem estourar 500 em valor forjado."""
    try:
        iid = int(request.form.get('item_id') or 0)
    except (TypeError, ValueError):
        return None
    return db.session.get(ChecklistItemModelo, iid) if iid else None


def _resolver_loja(valor):
    try:
        lid = int(valor or 0)
    except (TypeError, ValueError):
        return None
    lj = db.session.get(Loja, lid) if lid else None
    if lj is None or not lj.ativa or lj.nome == 'Industria':
        return None
    return lj


def _envio_json():
    return (request.headers.get('X-Checklist-Request') == '1'
            and 'application/json' in request.headers.get('Accept', '').lower())


def _json_envio(dados, status=200):
    resposta = jsonify(dados)
    resposta.status_code = status
    resposta.headers['Cache-Control'] = 'no-store, private'
    return resposta


def _erro_envio(mensagem, status=422, erro='validacao'):
    db.session.rollback()
    return _json_envio(dict(ok=False, erro=erro, mensagem=mensagem), status)


def _sucesso_envio(p, duplicado=False):
    return _json_envio(dict(
        ok=True, salvo=True, preenchimento_id=p.id,
        url_confirmacao=url_for('checklist.comprovante', preenchimento_id=p.id),
        mensagem=f'Checklist salvo. {CHECKLIST_TIPO_LABEL[p.tipo]} da {p.loja.nome} registrada.',
        duplicado=duplicado))


def _envio_do_usuario(token, loja_id, tipo):
    """Token não é credencial: seu dono e escopo também precisam coincidir."""
    envio = db.session.get(ChecklistEnvio, token)
    if envio is not None and (
        envio.usuario_id != current_user.id or envio.loja_id != loja_id or envio.tipo != tipo
    ):
        return None, _erro_envio('Este envio não pertence a este checklist.', 403, 'acesso')
    return envio, None


@checklist_bp.route('/envio-status')
@login_required
@checklist_required
def envio_status():
    loja = _resolver_loja(request.args.get('loja'))
    tipo = request.args.get('tipo')
    if loja is None or tipo not in CHECKLIST_TIPOS:
        return _erro_envio('Escolha a loja e o tipo de checklist.', 400)
    try:
        token = checklist_loja.validar_envio_token(request.args.get('token'))
    except ValueError as exc:
        return _erro_envio(str(exc), 400)
    envio, erro = _envio_do_usuario(token, loja.id, tipo)
    if erro is not None:
        return erro
    if envio is not None:
        return _sucesso_envio(envio.preenchimento, duplicado=True)
    return _json_envio(dict(ok=True, salvo=False))


@checklist_bp.route('/comprovante/<int:preenchimento_id>')
@login_required
@checklist_required
def comprovante(preenchimento_id):
    p = db.get_or_404(ChecklistPreenchimento, preenchimento_id)
    if p.usuario_id != current_user.id and not current_user.is_admin():
        abort(403)
    resposta = render_template('checklist/comprovante.html', p=p, loja=p.loja,
                              label=CHECKLIST_TIPO_LABEL[p.tipo])
    return resposta, 200, {'Cache-Control': 'no-store, private'}


@checklist_bp.route('/')
@login_required
@checklist_required
def index():
    """Hub: escolhe a loja (default = a do usuário) e o tipo de checklist.
    Mostra o que já foi preenchido hoje pra ninguém preencher em dobro sem
    querer (preencher de novo é permitido — vira outro registro)."""
    lojas = _lojas_escolhiveis()
    loja = (_resolver_loja(request.args.get('loja'))
            or _resolver_loja(checklist_responsaveis.loja_do_usuario(current_user))
            or _resolver_loja(current_user.loja_id)
            or (lojas[0] if lojas else None))
    tipos = checklist_loja.tipos_configurados(loja.id) if loja else {}
    feitos_hoje = {}
    if loja:
        # Janela hoje+ontem e filtro pelo "dia do turno" de cada tipo: o
        # fechamento de madrugada grava data=ontem e ainda precisa aparecer
        # como "✓ hoje" (revisão rodada 2).
        from datetime import timedelta
        for p in (ChecklistPreenchimento.query
                  .filter(ChecklistPreenchimento.loja_id == loja.id,
                          ChecklistPreenchimento.data
                          >= hoje() - timedelta(days=1))
                  .order_by(ChecklistPreenchimento.criado_em).all()):
            if p.data == checklist_loja._data_do_registro(p.tipo):
                feitos_hoje.setdefault(p.tipo, []).append(p)
    return render_template(
        'checklist/index.html', lojas=lojas, loja=loja,
        tipos=CHECKLIST_TIPOS, labels=CHECKLIST_TIPO_LABEL,
        configurados=tipos, feitos_hoje=feitos_hoje,
        pode_editar=bool(loja and checklist_responsaveis.pode_editar(current_user, loja.id)),
        equipe=checklist_responsaveis.quadro(loja.id) if loja else None)


@checklist_bp.route('/responsaveis')
@login_required
@admin_required
def responsaveis():
    return render_template('checklist/responsaveis.html',
                           equipe=checklist_responsaveis.quadro(),
                           gerenciar_responsaveis=True,
                           candidatos=checklist_responsaveis.candidatos())


@checklist_bp.route('/responsaveis/adicionar', methods=['POST'])
@login_required
@admin_required
def responsavel_adicionar():
    loja = _resolver_loja(request.form.get('loja_id'))
    funcionario_id = request.form.get('funcionario_id', type=int)
    funcionario = db.session.get(Funcionario, funcionario_id) if funcionario_id else None
    periodo = request.form.get('periodo')
    if (not loja or not funcionario or not funcionario.ativo
            or periodo not in PERIODOS_EQUIPE):
        flash('Escolha uma pessoa ativa, a loja e o período.', 'warning')
        return redirect(url_for('checklist.responsaveis'))
    if funcionario.usuario and funcionario.usuario.is_observador():
        flash('Essa conta é somente de consulta e não pode preencher checklists.', 'warning')
        return redirect(url_for('checklist.responsaveis'))
    filtro = dict(funcionario_id=funcionario.id, loja_id=loja.id, periodo=periodo)
    vinculo = ChecklistResponsavel.query.filter_by(**filtro).first()
    if vinculo is None:
        vinculo = ChecklistResponsavel(**filtro, criado_por_id=current_user.id)
        db.session.add(vinculo)
    vinculo.ativo = True
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        # Dois cliques simultâneos: só considere sucesso se o vínculo já existe ativo.
        if not ChecklistResponsavel.query.filter_by(**filtro, ativo=True).first():
            raise
    if funcionario.usuario:
        flash(f'{funcionario.nome}: checklist liberado. A pessoa usa a conta que já tem.',
              'success')
    else:
        flash(f'{funcionario.nome} adicionado. Falta criar ou vincular a conta para entrar.',
              'warning')
    return redirect(url_for('checklist.responsaveis', _anchor=f'loja-{loja.id}'))


@checklist_bp.route('/responsaveis/<int:vinculo_id>/remover', methods=['POST'])
@login_required
@admin_required
def responsavel_remover(vinculo_id):
    vinculo = db.get_or_404(ChecklistResponsavel, vinculo_id)
    vinculo.ativo = False
    db.session.commit()
    flash('Vínculo adicional retirado. Os vínculos de Organizar equipe continuam valendo.',
          'success')
    return redirect(url_for('checklist.responsaveis', _anchor=f'loja-{vinculo.loja_id}'))


@checklist_bp.route('/preencher', methods=['GET', 'POST'])
@login_required
@checklist_required
def preencher():
    resposta_json = _envio_json()
    loja = _resolver_loja(request.values.get('loja'))
    tipo = (request.values.get('tipo') or '').strip()
    if loja is None or tipo not in CHECKLIST_TIPOS:
        if resposta_json:
            return _erro_envio('Escolha a loja e o tipo de checklist.', 400)
        flash('Escolha a loja e o tipo de checklist.', 'warning')
        return redirect(url_for('checklist.index'))
    envio_token = uuid4().hex
    if request.method == 'POST':
        token_recebido = request.form.get('envio_token')
        if resposta_json or token_recebido:
            try:
                envio_token = checklist_loja.validar_envio_token(token_recebido)
            except ValueError as exc:
                if resposta_json:
                    return _erro_envio(str(exc), 400)
                flash(str(exc), 'danger')
                return redirect(url_for('checklist.preencher', loja=loja.id, tipo=tipo))
        else:
            # Formulários antigos sem JavaScript continuam aceitos.
            envio_token = None
        # Edição e fechamento da mesma loja não podem cruzar seus snapshots.
        Loja.query.filter_by(id=loja.id).with_for_update().first()
        if envio_token:
            envio, erro = _envio_do_usuario(envio_token, loja.id, tipo)
            if erro is not None:
                return erro if resposta_json else abort(403)
            if envio is not None:
                if resposta_json:
                    return _sucesso_envio(envio.preenchimento, duplicado=True)
                flash('Checklist salvo. Este envio já estava registrado.', 'success')
                return redirect(url_for('checklist.comprovante',
                                        preenchimento_id=envio.preenchimento_id))
    itens = checklist_loja.itens_para(loja.id, tipo)
    pode_editar = checklist_responsaveis.pode_editar(current_user, loja.id)
    if not itens and not pode_editar:
        if resposta_json:
            return _erro_envio('Esse checklist ainda não tem itens cadastrados. Peça ao admin para configurar.')
        flash('Esse checklist ainda não tem itens cadastrados — peça ao '
              'admin pra configurar.', 'warning')
        return redirect(url_for('checklist.index', loja=loja.id))

    if request.method == 'POST':
        # Anti-duplo-submit (achado da revisão 03/08/2026): upload de foto em
        # 3G dá janela real pro segundo clique. Mesmo (loja, tipo, usuário)
        # criado há < 30s = duplicata acidental, não re-preenchimento
        # intencional (esse continua permitido depois da janela).
        from datetime import timedelta

        from app.utils import agora
        recente = (ChecklistPreenchimento.query
                   .filter(ChecklistPreenchimento.loja_id == loja.id,
                           ChecklistPreenchimento.tipo == tipo,
                           ChecklistPreenchimento.usuario_id == current_user.id,
                           ChecklistPreenchimento.criado_em
                           >= agora() - timedelta(seconds=30))
                   .first())
        if recente is not None:
            if envio_token:
                checklist_loja.vincular_envio(envio_token, recente)
                try:
                    db.session.commit()
                except IntegrityError:
                    db.session.rollback()
                    envio, erro = _envio_do_usuario(envio_token, loja.id, tipo)
                    if erro is not None:
                        return erro if resposta_json else abort(403)
                    if envio is None:
                        raise
                    recente = envio.preenchimento
            if resposta_json:
                return _sucesso_envio(recente, duplicado=True)
            flash('Esse checklist acabou de ser registrado — não gravei em '
                  'dobro. (Pra preencher de novo de propósito, aguarde '
                  'meio minuto.)', 'info')
            return redirect(url_for('checklist.index', loja=loja.id))
        if request.form.get('revisao_itens') == '1' and (
            set(request.form.getlist('itens_presentes')) != {str(it.id) for it in itens}
            or any(request.form.get(f'versao_{it.id}') != str(it.versao) for it in itens)
        ):
            if resposta_json:
                return _erro_envio(
                    'O checklist foi alterado por outra pessoa. Suas respostas foram preservadas. '
                    'Reabra a página e revise os pontos antes de enviar novamente.',
                    409, 'checklist_alterado')
            flash('O checklist foi alterado por outra pessoa. Revise os pontos antes de enviar; '
                  'as fotos precisam ser anexadas novamente.', 'warning')
            return render_template('checklist/preencher.html', loja=loja, tipo=tipo,
                label=CHECKLIST_TIPO_LABEL[tipo], itens=itens,
                grupos=checklist_loja.agrupar_por_setor(itens), anteriores=_do_dia(loja, tipo),
                form=request.form, pode_editar=pode_editar,
                envio_token=envio_token or uuid4().hex,
                checklist_dia=checklist_loja._data_do_registro(tipo).isoformat(),
                equipe=checklist_responsaveis.quadro(loja.id)), 409
        respostas = {}
        for it in itens:
            estado = request.form.get(f'ok_{it.id}')
            foto = request.files.get(f'foto_{it.id}')
            raw = foto.read() if foto and foto.filename else None
            respostas[it.id] = {
                'ok': {'ok': True, 'problema': False}.get(estado),
                'observacao': request.form.get(f'obs_{it.id}'),
                'foto': raw or None,
            }
        try:
            p = checklist_loja.registrar(
                loja, tipo, current_user.id, respostas,
                observacao=request.form.get('observacao'), envio_token=envio_token)
        except ValueError as exc:
            if resposta_json:
                return _erro_envio(str(exc))
            # Re-render mantém marcações e observações; fotos o navegador
            # SEMPRE descarta em file input — avisar poupa o susto.
            flash(f'{exc} (As fotos precisam ser anexadas de novo.)',
                  'danger')
            return render_template(
                'checklist/preencher.html', loja=loja, tipo=tipo,
                label=CHECKLIST_TIPO_LABEL[tipo], itens=itens,
                grupos=checklist_loja.agrupar_por_setor(itens),
                anteriores=_do_dia(loja, tipo),
                form=request.form, pode_editar=pode_editar,
                envio_token=envio_token or uuid4().hex,
                checklist_dia=checklist_loja._data_do_registro(tipo).isoformat(),
                equipe=checklist_responsaveis.quadro(loja.id)), 422
        except IntegrityError:
            db.session.rollback()
            if envio_token:
                envio, erro = _envio_do_usuario(envio_token, loja.id, tipo)
                if erro is not None:
                    return erro if resposta_json else abort(403)
                if envio is not None:
                    if resposta_json:
                        return _sucesso_envio(envio.preenchimento, duplicado=True)
                    return redirect(url_for('checklist.comprovante',
                                            preenchimento_id=envio.preenchimento_id))
            raise
        if resposta_json:
            return _sucesso_envio(p)
        flash(f'Checklist de {CHECKLIST_TIPO_LABEL[tipo].lower()} da '
              f'{loja.nome} registrado ({len(p.respostas)} pontos'
              + (f', {p.n_problemas} com problema' if p.n_problemas else '')
              + ').', 'success')
        return redirect(url_for('checklist.index', loja=loja.id))

    return render_template(
        'checklist/preencher.html', loja=loja, tipo=tipo,
        label=CHECKLIST_TIPO_LABEL[tipo], itens=itens,
        grupos=checklist_loja.agrupar_por_setor(itens),
        anteriores=_do_dia(loja, tipo), form=None, pode_editar=pode_editar,
        envio_token=envio_token,
        checklist_dia=checklist_loja._data_do_registro(tipo).isoformat(),
        equipe=checklist_responsaveis.quadro(loja.id))


@checklist_bp.route('/itens', methods=['POST'])
@login_required
@checklist_required
@limiter.limit('10 per minute', key_func=lambda: str(current_user.id))
def editar_item():
    loja = _resolver_loja(request.form.get('loja'))
    tipo = request.form.get('tipo')
    if not loja or tipo not in CHECKLIST_TIPOS:
        return jsonify(erro='Loja ou checklist inválido.'), 400
    if not checklist_responsaveis.pode_editar(current_user, loja.id):
        abort(403)
    acao = request.form.get('acao')
    if acao not in ('novo', 'editar', 'excluir'):
        return jsonify(erro='Ação inválida.'), 400
    if acao == 'excluir' and not current_user.check_senha(request.form.get('senha') or ''):
        return jsonify(erro='Senha incorreta. Use a senha da sua própria conta.'), 403
    texto = (request.form.get('texto') or '').strip()
    setor = (request.form.get('setor') or '').strip()
    if acao != 'excluir' and (not texto or len(texto) > 300 or len(setor) > 60):
        return jsonify(erro='Informe o ponto (até 300 caracteres) e o setor (até 60).'), 400
    Loja.query.filter_by(id=loja.id).with_for_update().first()
    antes = None
    if acao == 'novo':
        modelo = ChecklistItemModelo(loja_id=loja.id, tipo=tipo, texto=texto,
            setor=setor or None, exige_foto=request.form.get('exige_foto') == '1')
        db.session.add(modelo)
        db.session.flush()
        item_id = modelo.id
    else:
        item_id = request.form.get('item_id', type=int)
        # Serializa edições do mesmo item e protege a criação do ajuste único.
        modelo = (ChecklistItemModelo.query.filter_by(id=item_id)
                  .with_for_update().first())
        item = next((it for it in checklist_loja.itens_para(loja.id, tipo)
                     if it.id == item_id), None)
        if not modelo or not item:
            return jsonify(erro='Este ponto não está mais disponível. Atualize a página.'), 409
        if request.form.get('versao') != str(item.versao):
            return jsonify(erro='Outra pessoa alterou este ponto. Atualize a página para revisar.'), 409
        antes = {c: getattr(item, c) for c in ('texto', 'setor', 'exige_foto')}
        ajuste = db.session.get(ChecklistItemAjuste, (item_id, loja.id))
        if ajuste is None:
            ajuste = ChecklistItemAjuste(item_id=item_id, loja_id=loja.id, **antes, ativo=True, versao=0)
            db.session.add(ajuste)
        ajuste.versao += 1
        if acao == 'excluir':
            ajuste.ativo = False
        else:
            ajuste.texto, ajuste.setor = texto, setor or None
            ajuste.exige_foto = request.form.get('exige_foto') == '1'
    db.session.flush()
    item = next((it for it in checklist_loja.itens_para(loja.id, tipo) if it.id == item_id), None)
    depois = {c: getattr(item, c) for c in ('texto', 'setor', 'exige_foto')} if item else None
    db.session.add(ChecklistEdicao(loja_id=loja.id, usuario_id=current_user.id,
        item_id=item_id, acao=acao, antes=antes, depois=depois))
    db.session.commit()
    return jsonify(item_id=item_id, html=render_template('checklist/_item.html',
        it=item, form=None, pode_editar=True) if item else None)


def _do_dia(loja, tipo):
    """Preenchimentos do "dia do turno" corrente — pra fechamento de
    madrugada, é ONTEM (`_data_do_registro`): sem isso o registro de 00:15
    sumia do aviso "já preenchido" e o mesmo fechamento entrava em dobro
    sem alerta (regressão apontada na revisão, rodada 2)."""
    return (ChecklistPreenchimento.query
            .filter_by(loja_id=loja.id, tipo=tipo,
                       data=checklist_loja._data_do_registro(tipo))
            .order_by(ChecklistPreenchimento.criado_em).all())


# ── Configuração dos itens (admin) ───────────────────────────────────

@checklist_bp.route('/config', methods=['GET', 'POST'])
@login_required
@admin_required
def config():
    if request.method == 'POST':
        acao = (request.form.get('acao') or '').strip()
        if acao == 'novo':
            texto = (request.form.get('texto') or '').strip()[:300]
            tipo = (request.form.get('tipo') or '').strip()
            if not texto or tipo not in CHECKLIST_TIPOS:
                flash('Informe o texto do item e o tipo.', 'danger')
                return redirect(url_for('checklist.config'))
            loja_raw = (request.form.get('loja_id') or '').strip()
            loja = _resolver_loja(loja_raw)
            if loja_raw and loja is None:
                # Loja informada mas inválida (desativada no meio-tempo,
                # POST velho): criar como GLOBAL em silêncio cobraria TODAS
                # as lojas — recusa com aviso (achado da revisão).
                flash('A loja escolhida não está mais disponível — item '
                      'não criado. Escolha de novo.', 'danger')
                return redirect(url_for('checklist.config'))
            try:
                ordem = int(request.form.get('ordem') or 0)
            except ValueError:
                ordem = 0
            db.session.add(ChecklistItemModelo(
                tipo=tipo, texto=texto,
                setor=(request.form.get('setor') or '').strip()[:60] or None,
                exige_foto=bool(request.form.get('exige_foto')),
                ordem=ordem, loja_id=loja.id if loja else None))
            db.session.commit()
            flash('Item adicionado.', 'success')
        elif acao == 'editar':
            it = _item_do_form()
            if it is None:
                flash('Item não encontrado.', 'danger')
                return redirect(url_for('checklist.config'))
            texto = (request.form.get('texto') or '').strip()[:300]
            if texto:
                it.texto = texto
            it.exige_foto = bool(request.form.get('exige_foto'))
            try:
                it.ordem = int(request.form.get('ordem') or it.ordem)
            except ValueError:
                pass
            db.session.commit()
            flash('Item atualizado.', 'success')
        elif acao == 'toggle':
            it = _item_do_form()
            if it is None:
                flash('Item não encontrado.', 'danger')
            else:
                it.ativo = not it.ativo
                db.session.commit()
                flash(('Item reativado.' if it.ativo
                       else 'Item desativado — sai dos próximos checklists; '
                            'o histórico não muda.'), 'success')
        elif acao == 'excluir':
            it = _item_do_form()
            if it is None:
                flash('Item não encontrado.', 'danger')
            else:
                usado = (ChecklistResposta.query
                         .filter_by(item_id=it.id).first() is not None)
                if usado or ChecklistItemAjuste.query.filter_by(item_id=it.id).first():
                    # Item com história nunca some — desativa (a FK das
                    # respostas antigas fica viva; snapshot cobre o texto).
                    it.ativo = False
                    db.session.commit()
                    flash('Esse item já tem respostas no histórico — foi '
                          'DESATIVADO em vez de excluído.', 'warning')
                else:
                    db.session.delete(it)
                    db.session.commit()
                    flash('Item excluído.', 'success')
        return redirect(url_for('checklist.config'))

    itens = (ChecklistItemModelo.query
             .order_by(ChecklistItemModelo.ordem, ChecklistItemModelo.id)
             .all())
    # Por tipo e, dentro dele, agrupado por setor (mesma ordem da tela de
    # preenchimento — com 169 pontos, uma lista corrida era ilegível).
    por_tipo = {t: checklist_loja.agrupar_por_setor(
        [i for i in itens if i.tipo == t]) for t in CHECKLIST_TIPOS}
    n_por_tipo = {t: sum(1 for i in itens if i.tipo == t)
                  for t in CHECKLIST_TIPOS}
    setores = []
    for i in itens:
        if i.setor and i.setor not in setores:
            setores.append(i.setor)
    return render_template('checklist/config.html', por_tipo=por_tipo,
                           n_por_tipo=n_por_tipo, setores=setores,
                           tipos=CHECKLIST_TIPOS,
                           labels=CHECKLIST_TIPO_LABEL,
                           lojas=_lojas_escolhiveis())


# ── Conferência (admin) ──────────────────────────────────────────────

@checklist_bp.route('/conferencia')
@login_required
@admin_required
def conferencia():
    """Histórico: quem preencheu, quando, problemas e fotos — e quem está
    DEVENDO hoje/ontem (mesma conta da pendência da home)."""
    from datetime import timedelta

    try:
        dias = min(max(int(request.args.get('dias') or 7), 1), 90)
    except (TypeError, ValueError):
        dias = 7
    from sqlalchemy.orm import joinedload, selectinload

    loja = _resolver_loja(request.args.get('loja'))
    di = hoje() - timedelta(days=dias - 1)
    q = (ChecklistPreenchimento.query
         .options(joinedload(ChecklistPreenchimento.loja),
                  joinedload(ChecklistPreenchimento.usuario),
                  # selectinload: o template lê respostas + n_problemas de
                  # cada linha — sem isso, ~3 lazy-loads × 400 linhas.
                  selectinload(ChecklistPreenchimento.respostas))
         .filter(ChecklistPreenchimento.data >= di,
                 ChecklistPreenchimento.data <= hoje()))
    if loja:
        q = q.filter(ChecklistPreenchimento.loja_id == loja.id)
    preenchidos = (q.order_by(ChecklistPreenchimento.data.desc(),
                              ChecklistPreenchimento.criado_em.desc())
                   .limit(400).all())
    faltando = {
        'abertura_hoje': checklist_loja.lojas_faltando('abertura', hoje()),
        'fechamento_ontem': checklist_loja.lojas_faltando(
            'fechamento', hoje() - timedelta(days=1)),
    }
    return render_template(
        'checklist/conferencia.html', preenchidos=preenchidos,
        labels=CHECKLIST_TIPO_LABEL, lojas=_lojas_escolhiveis(),
        loja=loja, dias=dias, faltando=faltando)
