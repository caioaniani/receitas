"""Visão macro e registro explícito do histórico de carreira (somente dono)."""
from datetime import datetime

from flask import abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError

from app.blueprints.rh import rh_bp
from app.decorators import gestao_rh_required
from app.extensions import db
from app.models import Funcionario, Loja


@rh_bp.route('/equipe')
@login_required
@gestao_rh_required
def equipe():
    from app.services.rh_equipe import carregar_visao
    filtros = {k: (request.args.get(k) or '').strip() for k in
               ('q', 'cargo', 'loja', 'lider', 'nivel', 'pendencia')}
    filtros['ativos'] = '0' if request.args.get('ativos') == '0' else '1'
    return render_template('rh/equipe.html', **carregar_visao(filtros))


@rh_bp.route('/equipe/lojas')
@login_required
@gestao_rh_required
def equipe_lojas():
    from app.services.rh_equipe_lojas import carregar_lojas
    from app.services.rh_equipe_lojas_acoes import candidatos_por_loja

    dados = carregar_lojas()
    loja_id = (request.args.get('loja') or '').strip()
    selecionada = None
    if loja_id:
        selecionada = next((loja for loja in dados['lojas']
                            if str(loja['id']) == loja_id), None)
        if selecionada is None:
            abort(404)
    lojas_exibidas = [selecionada] if selecionada else dados['lojas']
    return render_template(
        'rh/equipe_lojas.html', **dados, selecionada=selecionada,
        lojas_exibidas=lojas_exibidas,
        candidatos=candidatos_por_loja([loja['id'] for loja in lojas_exibidas]),
        voltar='loja' if selecionada else 'todas',
    )


def _voltar_para(loja, todas):
    """Volta para a mesma visão (uma loja ou todas) de onde o gesto saiu."""
    if todas or not loja.ativa:
        return redirect(url_for('rh.equipe_lojas'))
    return redirect(url_for('rh.equipe_lojas', loja=loja.id))


def _executar_gesto(loja_id, gesto, mensagem):
    """Esqueleto comum dos três POSTs: lock, flash legível e redirect.

    ``gesto`` recebe o serviço e a loja e devolve o resultado; ``mensagem``
    transforma o resultado no texto de sucesso. Falha de validação ou de
    corrida (dois cliques no mesmo vínculo) desfaz tudo e avisa.
    """
    from sqlalchemy.orm import load_only

    from app.services import rh_equipe_lojas_acoes as acoes

    # Só id/nome/ativa: a planta da loja (LargeBinary) não entra no gesto.
    loja = (Loja.query.options(load_only(Loja.id, Loja.nome, Loja.ativa))
            .filter_by(id=loja_id).first_or_404())
    todas = request.form.get('voltar') == 'todas'
    try:
        resultado = gesto(acoes, loja)
        db.session.commit()
    except acoes.EquipeLojaError as exc:
        db.session.rollback()
        flash(str(exc), 'warning')
    except IntegrityError:
        db.session.rollback()
        flash('Esse vínculo acabou de ser alterado por outra pessoa. '
              'Confira a equipe e tente de novo.', 'warning')
    else:
        flash(mensagem(resultado, loja), 'success')
    return _voltar_para(loja, todas)


def _periodo_texto(resultado):
    return (f' Período: {resultado["pessoa"].periodo}.'
            if resultado.get('periodo_mudou') else '')


@rh_bp.route('/equipe/lojas/<int:loja_id>/adicionar', methods=['POST'])
@login_required
@gestao_rh_required
def equipe_lojas_adicionar(loja_id):
    def gesto(acoes, loja):
        return acoes.adicionar_pessoa(
            loja, request.form.get('funcionario_id', type=int),
            principal=request.form.get('principal') == '1',
            periodo=request.form.get('periodo'), actor_id=current_user.id)

    def mensagem(resultado, loja):
        nome = resultado['pessoa'].nome
        if resultado['principal']:
            texto = f'{nome} agora tem {loja.nome} como unidade principal.'
            if resultado['principal_anterior']:
                texto += f' A unidade anterior ({resultado["principal_anterior"]}) ' \
                         'continua como vínculo adicional.'
        else:
            texto = (f'{nome} ganhou vínculo adicional com {loja.nome}; a unidade '
                     f'principal continua {resultado["principal_anterior"]}.')
        return texto + _periodo_texto(resultado)
    return _executar_gesto(loja_id, gesto, mensagem)


@rh_bp.route('/equipe/lojas/<int:loja_id>/importar', methods=['POST'])
@login_required
@gestao_rh_required
def equipe_lojas_importar(loja_id):
    def gesto(acoes, loja):
        return acoes.importar_pessoa(
            loja, request.form.get('funcionario_id', type=int),
            manter_origem=request.form.get('manter_origem') == '1',
            periodo=request.form.get('periodo'), actor_id=current_user.id)

    def mensagem(resultado, loja):
        nome = resultado['pessoa'].nome
        origem = resultado['origem'] or 'a loja de origem'
        if resultado['origem_mantida']:
            texto = (f'{nome} passou a ter {loja.nome} como unidade principal; '
                     f'o vínculo com {origem} foi mantido como adicional.')
        else:
            texto = f'{nome} foi transferido(a) de {origem} para {loja.nome}.'
        return texto + _periodo_texto(resultado)
    return _executar_gesto(loja_id, gesto, mensagem)


@rh_bp.route('/equipe/lojas/<int:loja_id>/remover', methods=['POST'])
@login_required
@gestao_rh_required
def equipe_lojas_remover(loja_id):
    def gesto(acoes, loja):
        return acoes.remover_pessoa(
            loja, request.form.get('funcionario_id', type=int),
            actor_id=current_user.id)

    def mensagem(resultado, loja):
        nome = resultado['pessoa'].nome
        texto = f'{nome} saiu da equipe de {loja.nome}; a ficha continua ativa no RH.'
        if resultado['nova_principal']:
            texto += (f' Com uma única loja ativa restante, {resultado["nova_principal"]} '
                      'passou a ser a unidade principal.')
        elif resultado['era_principal']:
            if resultado['restantes']:
                texto += (' A pessoa ficou sem unidade principal ativa: defina a nova '
                          'em "Editar estrutura".')
            else:
                texto += ' A pessoa ficou sem loja: aparece em "Unidade principal a definir".'
        return texto
    return _executar_gesto(loja_id, gesto, mensagem)


@rh_bp.route('/funcionarios/<int:id>/carreira', methods=['GET', 'POST'])
@login_required
@gestao_rh_required
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
@gestao_rh_required
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
