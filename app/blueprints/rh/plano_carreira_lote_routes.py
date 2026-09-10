"""Seleção explícita, revisão e confirmação do enquadramento em lote."""
from flask import current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from itsdangerous import BadData, URLSafeTimedSerializer
from sqlalchemy.exc import DBAPIError

from app.blueprints.rh import rh_bp
from app.decorators import owner_required
from app.extensions import db
from app.services import plano_carreira_lote

_VALIDADE_PREVIA = 30 * 60


def _assinador():
    return URLSafeTimedSerializer(current_app.config['SECRET_KEY'],
                                  salt='rh-aprovacao-lote-v1')


def _filtros(dados):
    return {campo: (dados.get(campo) or '').strip()[:200]
            for campo in ('q', 'familia')}


@rh_bp.route('/plano-carreira/aprovar-lote', methods=['POST'])
@login_required
@owner_required
def plano_carreira_aprovar_lote():
    ids = request.form.getlist('enquadramento_ids')
    filtros = _filtros(request.form)
    previa, confirmacao, erro = None, None, None
    try:
        etapa = request.form.get('etapa', 'revisar')
        if etapa == 'confirmar':
            try:
                dados = _assinador().loads(request.form.get('confirmacao', ''),
                                           max_age=_VALIDADE_PREVIA)
            except BadData:
                raise ValueError('A revisão expirou ou é inválida. Selecione as pessoas e revise novamente.') from None
            if dados.get('actor_id') != current_user.id:
                raise ValueError('Esta revisão pertence a outro usuário. Revise novamente.')
            ids, filtros = dados['ids'], dados['filtros']
            resultado = plano_carreira_lote.confirmar(
                ids, dados['estado'], actor_id=current_user.id)
            db.session.commit()
            quantidade = resultado['resumo']['quantidade']
            flash(f'{quantidade} enquadramento(s) aprovado(s). '
                  'As decisões e os vínculos foram atualizados.', 'success')
            return redirect(url_for('rh.plano_carreira', **filtros,
                                    _anchor='enquadramento-equipe'))
        if etapa != 'revisar':
            raise ValueError('Escolha revisar ou confirmar a aprovação.')
        previa = plano_carreira_lote.prever(ids)
        ids = previa['ids']
        confirmacao = _assinador().dumps({
            'actor_id': current_user.id, 'ids': ids, 'filtros': filtros,
            'estado': previa['estado'],
        })
    except ValueError as exc:
        db.session.rollback()
        erro = str(exc)
        previa, confirmacao = None, None
    except DBAPIError as exc:
        db.session.rollback()
        codigo = getattr(exc.orig, 'sqlstate', None) or getattr(exc.orig, 'pgcode', None)
        if codigo not in ('40P01', '40001'):
            raise
        erro = 'Houve uma atualização ao mesmo tempo. Revise o lote novamente antes de confirmar.'
        previa, confirmacao = None, None
    # Reconstrói uma URL interna com filtros, nunca confia em um voltar externo.
    selecionados = [str(i) for i in ids if str(i).isascii() and str(i).isdigit()][:200]
    voltar = url_for('rh.plano_carreira', **filtros, selecionados=selecionados,
                     _anchor='enquadramento-equipe')
    return render_template('rh/plano_carreira_lote.html', previa=previa,
                           confirmacao=confirmacao, erro=erro, voltar=voltar), 400 if erro else 200
