"""Cronograma de produção da indústria (/telaindustriateste).

Painel da ADMINISTRAÇÃO: distribui a produção da semana POR DIA acompanhando as
entregas (deslocado pelo lead time da receita) — um pouco de cada dia, sem
faltar nem sobrar. "Aprovar plano do dia" cria a ordem de produção daquele dia
(origem='cronograma') que DESCE pro padeiro. O estoque/MP só mexem quando o
padeiro produz (opção B). NÃO mexe na /padeiro oficial.
"""
from datetime import date

from flask import flash, jsonify, redirect, render_template, request, url_for
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


def _inicio_offset():
    """Inicio do horizonte (dias). Default 1 = amanha — MESMO padrao do Painel,
    pra as duas telas baterem. 0 = hoje. Limite 0..14."""
    try:
        v = int(request.values.get('inicio', 1))
    except (TypeError, ValueError):
        v = 1
    return max(0, min(v, 14))


def _equilibrar():
    """Modo 'equilibrar carga': cada receita inteira num dia, fornadas niveladas
    (adianta receitas pra encher dia ocioso). Off por padrao."""
    return request.values.get('equilibrar') in ('1', 'true', 'on')


@industria_teste_bp.route('/')
@login_required
@admin_required
def index():
    from app.models import PlanejamentoProducao
    from app.services.previsao_producao import cronograma_producao

    horizonte, janela = _horizonte_janela()
    inicio = _inicio_offset()
    equilibrar = _equilibrar()
    crono = cronograma_producao(horizonte_dias=horizonte, janela_semanas=janela,
                                inicio_offset_dias=inicio, equilibrar=equilibrar)
    # Estado da ordem por dia (fluxo 2 passos): rascunho/aprovado x enviado.
    estados = {}
    for p in PlanejamentoProducao.query.filter_by(origem='cronograma').all():
        estados[p.data.isoformat()] = {
            'enviado': p.enviado_ao_padeiro is not False, 'plano_id': p.id}
    return render_template('industria_teste/teste.html', crono=crono,
                           horizonte=horizonte, janela=janela, inicio=inicio,
                           equilibrar=equilibrar, estados=estados)


@industria_teste_bp.route('/aprovar', methods=['POST'])
@login_required
@admin_required
def aprovar():
    """Aprova a coluna de um dia -> cria a ordem de produção (desce pro
    padeiro)."""
    from app.services.producao import aprovar_plano_do_dia

    horizonte, janela = _horizonte_janela()
    inicio = _inicio_offset()
    equilibrar = _equilibrar()
    try:
        data_alvo = date.fromisoformat(request.form.get('data', ''))
    except (TypeError, ValueError):
        flash('Data inválida.', 'warning')
        return redirect(url_for('industria_teste.index', horizonte=horizonte,
                                janela=janela, inicio=inicio,
                                equilibrar=1 if equilibrar else None))

    # IMPORTANTE: aprovar com o MESMO equilibrar da tela — senao o que desce pro
    # padeiro nao bate com o que voce viu/equilibrou.
    plano = aprovar_plano_do_dia(data_alvo, current_user.id,
                                 horizonte_dias=horizonte, janela_semanas=janela,
                                 inicio_offset_dias=inicio, equilibrar=equilibrar)
    if plano:
        flash('Plano de %s aprovado (%d receita(s)). Revise/edite e clique em '
              '"enviar ao padeiro" quando estiver pronto.'
              % (data_alvo.strftime('%d/%m'), len(plano.itens)), 'success')
    else:
        flash('Nada a produzir em %s.' % data_alvo.strftime('%d/%m'), 'info')
    return redirect(url_for('industria_teste.index', horizonte=horizonte,
                            janela=janela, inicio=inicio,
                            equilibrar=1 if equilibrar else None))


@industria_teste_bp.route('/enviar', methods=['POST'])
@login_required
@admin_required
def enviar():
    """Empurra o cronograma ATUAL do dia (com as edições do grid) pro padeiro:
    reconstrói a ordem a partir do grid e marca enviado. Re-pressável — serve
    pro 1º envio E pra ATUALIZAR a produção depois de editar o grid (a edição
    do grid só chega no padeiro quando se aperta isto)."""
    from app.services.producao import enviar_plano_do_dia

    horizonte, janela = _horizonte_janela()
    inicio = _inicio_offset()
    equilibrar = _equilibrar()
    eq = 1 if equilibrar else None
    try:
        data_alvo = date.fromisoformat(request.form.get('data', ''))
    except (TypeError, ValueError):
        flash('Data inválida.', 'warning')
        return redirect(url_for('industria_teste.index', horizonte=horizonte,
                                janela=janela, inicio=inicio, equilibrar=eq))
    plano = enviar_plano_do_dia(data_alvo, current_user.id,
                                horizonte_dias=horizonte, janela_semanas=janela,
                                inicio_offset_dias=inicio, equilibrar=equilibrar)
    if plano:
        flash('Produção de %s enviada ao padeiro (%d receita(s)).'
              % (data_alvo.strftime('%d/%m'), len(plano.itens)), 'success')
    else:
        flash('Nada a produzir em %s.' % data_alvo.strftime('%d/%m'), 'info')
    return redirect(url_for('industria_teste.index', horizonte=horizonte,
                            janela=janela, inicio=inicio, equilibrar=eq))


@industria_teste_bp.route('/excluir', methods=['POST'])
@login_required
@admin_required
def excluir():
    """Exclui a ordem de produção de um dia (desfaz um envio errado). Bloqueia
    se já houve produção — o estoque/MP reais já mexeram."""
    from app.services.producao import excluir_plano_do_dia

    horizonte, janela = _horizonte_janela()
    inicio = _inicio_offset()
    eq = 1 if _equilibrar() else None
    try:
        data_alvo = date.fromisoformat(request.form.get('data', ''))
    except (TypeError, ValueError):
        flash('Data inválida.', 'warning')
        return redirect(url_for('industria_teste.index', horizonte=horizonte,
                                janela=janela, inicio=inicio, equilibrar=eq))
    res = excluir_plano_do_dia(data_alvo)
    if res['ok']:
        flash('Ordem de %s excluída.' % data_alvo.strftime('%d/%m'), 'success')
    elif res.get('erro') == 'ja_produzido':
        flash('Não dá pra excluir a ordem de %s: já houve produção (%d un) — '
              'isso já creditou estoque e baixou matéria-prima.'
              % (data_alvo.strftime('%d/%m'), res.get('produzido', 0)),
              'warning')
    else:
        flash('Não há ordem em %s pra excluir.'
              % data_alvo.strftime('%d/%m'), 'warning')
    return redirect(url_for('industria_teste.index', horizonte=horizonte,
                            janela=janela, inicio=inicio, equilibrar=eq))


def _payload_int(payload, key, default, lo, hi):
    try:
        return max(lo, min(int(payload.get(key, default)), hi))
    except (TypeError, ValueError):
        return default


@industria_teste_bp.route('/celula', methods=['POST'])
@login_required
@admin_required
def celula():
    """Autosave da edição manual de uma célula (receita×dia). Redistribui no
    servidor mantendo o total da receita, salva o rascunho (override) e devolve
    a LINHA recalculada (todas as células da receita + fornadas). JSON."""
    from app.services.cronograma_edit import editar_celula

    p = request.get_json(silent=True) or request.form
    try:
        receita_id = int(p.get('receita_id'))
        qtd = int(p.get('qtd'))
    except (TypeError, ValueError):
        return jsonify(ok=False, erro='parametros'), 400
    res = editar_celula(
        receita_id, p.get('data') or '', qtd,
        horizonte_dias=_payload_int(p, 'horizonte', 7, 1, 14),
        janela_semanas=_payload_int(p, 'janela', 6, 1, 26),
        inicio_offset_dias=_payload_int(p, 'inicio', 1, 0, 14),
        equilibrar=str(p.get('equilibrar', '')) in ('1', 'true', 'on', 'True'))
    if res is None:
        return jsonify(ok=False, erro='nao_encontrado'), 404
    return jsonify(ok=True, **res)


@industria_teste_bp.route('/limpar-edicoes', methods=['POST'])
@login_required
@admin_required
def limpar_edicoes():
    """Apaga TODAS as edições manuais (rascunhos) do cronograma — tudo volta pra
    sugestão calculada. Não toca em pedido enviado, estoque nem MP."""
    from app.services.cronograma_edit import limpar_todos_overrides

    horizonte, janela = _horizonte_janela()
    inicio = _inicio_offset()
    eq = 1 if _equilibrar() else None
    n = limpar_todos_overrides()
    flash('%d edição(ões) manual(is) apagada(s) — cronograma voltou pro cálculo.'
          % n if n else 'Não havia edição manual pra limpar.', 'success')
    return redirect(url_for('industria_teste.index', horizonte=horizonte,
                            janela=janela, inicio=inicio, equilibrar=eq))


@industria_teste_bp.route('/celula/reset', methods=['POST'])
@login_required
@admin_required
def celula_reset():
    """Apaga a edição manual de uma receita (volta pra sugestão calculada)."""
    from app.services.cronograma_edit import resetar_receita

    p = request.get_json(silent=True) or request.form
    try:
        receita_id = int(p.get('receita_id'))
    except (TypeError, ValueError):
        return jsonify(ok=False, erro='parametros'), 400
    datas = p.get('datas') or []
    if isinstance(datas, str):
        datas = [d for d in datas.split(',') if d]
    resetar_receita(receita_id, datas)
    return jsonify(ok=True)
