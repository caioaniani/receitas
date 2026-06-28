"""Cronograma de produção da indústria (/telaindustriateste).

Painel da ADMINISTRAÇÃO: distribui a produção da semana POR DIA acompanhando as
entregas (deslocado pelo lead time da receita) — um pouco de cada dia, sem
faltar nem sobrar. "Aprovar plano do dia" cria a ordem de produção daquele dia
(origem='cronograma') que DESCE pro padeiro. O estoque/MP só mexem quando o
padeiro produz (opção B). NÃO mexe na /padeiro oficial.
"""
from datetime import date

from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.blueprints.industria_teste import industria_teste_bp
from app.decorators import admin_required


def _horizonte_janela():
    try:
        horizonte = max(1, min(int(request.values.get('horizonte', 7)), 14))
    except (TypeError, ValueError):
        horizonte = 7
    try:
        janela = max(1, min(int(request.values.get('janela', 6)), 26))
    except (TypeError, ValueError):
        janela = 6
    return horizonte, janela


@industria_teste_bp.route('/')
@login_required
@admin_required
def index():
    from app.models import PlanejamentoProducao
    from app.services.previsao_producao import cronograma_producao

    horizonte, janela = _horizonte_janela()
    crono = cronograma_producao(horizonte_dias=horizonte, janela_semanas=janela)
    # Estado da ordem por dia (fluxo 2 passos): rascunho/aprovado x enviado.
    estados = {}
    for p in PlanejamentoProducao.query.filter_by(origem='cronograma').all():
        estados[p.data.isoformat()] = {
            'enviado': p.enviado_ao_padeiro is not False, 'plano_id': p.id}
    return render_template('industria_teste/teste.html', crono=crono,
                           horizonte=horizonte, janela=janela, estados=estados)


@industria_teste_bp.route('/aprovar', methods=['POST'])
@login_required
@admin_required
def aprovar():
    """Aprova a coluna de um dia -> cria a ordem de produção (desce pro
    padeiro)."""
    from app.services.producao import aprovar_plano_do_dia

    horizonte, janela = _horizonte_janela()
    try:
        data_alvo = date.fromisoformat(request.form.get('data', ''))
    except (TypeError, ValueError):
        flash('Data inválida.', 'warning')
        return redirect(url_for('industria_teste.index',
                                horizonte=horizonte, janela=janela))

    plano = aprovar_plano_do_dia(data_alvo, current_user.id,
                                 horizonte_dias=horizonte, janela_semanas=janela)
    if plano:
        flash('Plano de %s aprovado (%d receita(s)). Revise/edite e clique em '
              '"enviar ao padeiro" quando estiver pronto.'
              % (data_alvo.strftime('%d/%m'), len(plano.itens)), 'success')
    else:
        flash('Nada a produzir em %s.' % data_alvo.strftime('%d/%m'), 'info')
    return redirect(url_for('industria_teste.index',
                            horizonte=horizonte, janela=janela))


@industria_teste_bp.route('/enviar', methods=['POST'])
@login_required
@admin_required
def enviar():
    """2º passo: envia a ordem aprovada do dia pro padeiro (ela só aparece no
    Fluxograma / Produção do dia depois disto)."""
    from app.services.producao import enviar_plano_do_dia

    horizonte, janela = _horizonte_janela()
    try:
        data_alvo = date.fromisoformat(request.form.get('data', ''))
    except (TypeError, ValueError):
        flash('Data inválida.', 'warning')
        return redirect(url_for('industria_teste.index',
                                horizonte=horizonte, janela=janela))
    plano = enviar_plano_do_dia(data_alvo)
    if plano:
        flash('Plano de %s enviado ao padeiro.' % data_alvo.strftime('%d/%m'),
              'success')
    else:
        flash('Não há ordem aprovada em %s pra enviar.'
              % data_alvo.strftime('%d/%m'), 'warning')
    return redirect(url_for('industria_teste.index',
                            horizonte=horizonte, janela=janela))
