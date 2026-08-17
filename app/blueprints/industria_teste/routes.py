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
    """Inicio do horizonte (dias). Default 0 = HOJE — esta e a tela de PRODUCAO
    (o padeiro produz hoje), entao a 1a coluna (amarela = 'hoje') tem que ser
    mesmo hoje pra bater com a tela do padeiro. Antes era 1 (amanha, copiado do
    Painel), o que punha a coluna amarela em amanha e a edicao/envio caia no dia
    errado. 0 = hoje. Limite 0..14."""
    try:
        v = int(request.values.get('inicio', 0))
    except (TypeError, ValueError):
        v = 0
    return max(0, min(v, 14))


def _equilibrar():
    """Modo 'equilibrar carga': cada receita inteira num dia, fornadas
    niveladas (adianta receitas pra encher dia ocioso). LIGADO por padrão
    desde 17/08/2026 (dono: "o sistema deve equilibrar sozinho" — mesma
    régua da automação); desligar = ?equilibrar=0 explícito (o select da
    tela sempre manda o valor)."""
    v = request.values.get('equilibrar')
    if v is None:
        return True
    return v in ('1', 'true', 'on')


def _motor():
    """Motor de previsao da demanda (pedido do dono 06/07/2026): 'pedidos'
    (historico de pedidos das lojas — o original), 'vendas' (venda real
    das lojas + merma) ou 'maior' (o maior dos dois por dia). Default =
    'vendas' desde 17/08/2026 (dono: "producao da semana programada baseado
    no historico de vendas e estoque" — mesma regua da automacao)."""
    from app.services.previsao_producao import MOTORES_PREVISAO_PRODUCAO
    m = (request.values.get('motor') or 'vendas').strip()
    return m if m in MOTORES_PREVISAO_PRODUCAO else 'vendas'


def _params_visao(**extra):
    """Query params que preservam a visao atual nos redirects. Motor E
    equilibrar vao SEMPRE explicitos: omitir "quando e o default" ja causou
    classe de bug — o default mudou (17/08/2026) e URL/form sem o param
    voltaria pro comportamento errado em silencio."""
    p = {'horizonte': _horizonte_janela()[0], 'janela': _horizonte_janela()[1],
         'inicio': _inicio_offset(), 'motor': _motor(),
         'equilibrar': 1 if _equilibrar() else 0}
    p.update(extra)
    return p


@industria_teste_bp.route('/')
@login_required
@admin_required
def index():
    from app.models import PlanejamentoProducao
    from app.services.previsao_producao import cronograma_producao
    from app.services.producao_pendente import pendencias_por_receita

    horizonte, janela = _horizonte_janela()
    inicio = _inicio_offset()
    equilibrar = _equilibrar()
    motor = _motor()
    crono = cronograma_producao(horizonte_dias=horizonte, janela_semanas=janela,
                                inicio_offset_dias=inicio, equilibrar=equilibrar,
                                motor=motor)
    # Overlay "verde": produção mandada e ainda não confirmada pelo padeiro.
    # Projeção pura (não é estoque real) — soma por cima do em_estoque no grid.
    pend = pendencias_por_receita()
    for rr in crono['receitas']:
        p = pend.get(rr['receita_id'])
        rr['pend_agendado'] = p['agendado'] if p else 0
        rr['pend_vencido'] = p['vencido'] if p else 0
    # Estado da ordem por dia (fluxo 2 passos): rascunho/aprovado x enviado.
    estados = {}
    planos_por_dia = {}
    for p in PlanejamentoProducao.query.filter_by(origem='cronograma').all():
        estados[p.data.isoformat()] = {
            'enviado': p.enviado_ao_padeiro is not False, 'plano_id': p.id}
        planos_por_dia[p.data.isoformat()] = p

    # A ordem ENVIADA de volta na tela (pedido do dono 08/07/2026): o grid
    # recalcula a sugestão e se descola do que o padeiro está vendo; sem isso
    # a diferença fica invisível até alguém lembrar do "🔄 atualizar produção".
    # ordem_enviada[data][rid] = qtd_alvo (o que o padeiro vê agora);
    # difere[data] = set de rids cujo re-envio mudaria a ordem
    # (expected = max(grid + extra, produzido) != qtd_alvo — a MESMA conta do
    # _sync_itens_do_cronograma; item dispensado fica fora: dispensa é decisão
    # explícita, não atualização pendente).
    ordem_enviada, difere = {}, {}
    dias_grid = {d['data'] for d in crono['dias']}
    for iso, p in planos_por_dia.items():
        # So dias VISIVEIS no grid: plano de fora do horizonte (ex: ordem de
        # ontem) nao tem coluna pra comparar — o loop "saiu do grid" marcaria
        # diferenca falsa em tudo que ainda nao foi produzido.
        if p.enviado_ao_padeiro is False or iso not in dias_grid:
            continue
        itens = {it.receita_id: it for it in p.itens
                 if it.dispensada_em is None}
        # Dispensado fica fora da comparação DOS DOIS lados: a dispensa é
        # decisão explícita, e o "🔄 atualizar produção" NÃO a desfaz (o sync
        # mantém dispensada_em) — comparar geraria um "difere" que nenhum
        # re-envio limpa.
        dispensados = {it.receita_id for it in p.itens
                       if it.dispensada_em is not None}
        ordem_enviada[iso] = {rid: int(it.qtd_alvo or 0)
                              for rid, it in itens.items()}
        dif = set()
        vistos = set()
        for rr in crono['receitas']:
            rid = rr['receita_id']
            if rid in dispensados:
                continue
            c = next((c for c in rr['por_dia'] if c['data'] == iso), None)
            if c is None:
                continue
            vistos.add(rid)
            it = itens.get(rid)
            extra = int(it.qtd_extra or 0) if it else 0
            produzido = int(it.produzido_qtd or 0) if it else 0
            esperado = max(int(c['qtd'] or 0) + extra, produzido)
            enviado_qtd = int(it.qtd_alvo or 0) if it else 0
            if esperado != enviado_qtd:
                dif.add(rid)
        # Item da ordem que saiu do grid (linha nem aparece mais): re-envio
        # também mexeria nele (remove/trava no piso) — conta como diferença.
        for rid, it in itens.items():
            if rid in vistos:
                continue
            piso = max(int(it.produzido_qtd or 0), int(it.qtd_extra or 0))
            if int(it.qtd_alvo or 0) != piso:
                dif.add(rid)
        if dif:
            difere[iso] = dif

    # Cadeado por dia (🔒): edição/limpar/reset não mexem em dia fechado.
    # Cadeado de dia que já passou é podado (o grid nunca mais o mostra;
    # deixá-lo blindaria overrides mortos do "limpar edições" pra sempre).
    from app.services.cronograma_edit import (
        dias_fechados,
        podar_dias_fechados_passados,
    )
    podar_dias_fechados_passados()
    fechados = {d.isoformat() for d in dias_fechados()}

    # Totais por dia (rodapé do grid): unidades de produto final (insumo fica
    # de fora — massa em bolas somada com croissants inflaria o número) e
    # fornadas de TODAS as linhas (carga real de trabalho). O dia mais
    # carregado ganha destaque — é o que o "equilibrar carga" tenta aliviar.
    totais_dia = []
    for i, _dia in enumerate(crono['dias']):
        un = forn = 0
        for rr in crono['receitas']:
            c = rr['por_dia'][i]
            if not rr.get('insumo'):
                un += c['qtd'] or 0
            if c.get('fornadas'):
                forn += c['fornadas']
        totais_dia.append({'un': un, 'fornadas': forn})
    pico_idx = None
    if any(t['un'] or t['fornadas'] for t in totais_dia):
        pico_idx = max(range(len(totais_dia)),
                       key=lambda i: (totais_dia[i]['fornadas'],
                                      totais_dia[i]['un']))

    # Resumo pro topo da tela: o que importa antes de mergulhar no grid.
    resumo = {
        'risco_n': len(crono.get('alertas_falta') or []),
        'pend_agendado': sum(r['pend_agendado'] for r in crono['receitas']),
        'pend_vencido': sum(r['pend_vencido'] for r in crono['receitas']),
        'editados': sum(1 for r in crono['receitas'] if r.get('editado')),
        'stale_n': sum(1 for r in crono['receitas'] if r.get('override_stale')),
        'zerados': sum(1 for r in crono['receitas'] if not r.get('total')),
    }

    # "Próximos passos": a lista do que está pedindo ação AGORA, com o gesto
    # correspondente ao lado — o antídoto do "abri a tela e não sei por onde
    # começar". Só estados acionáveis (o que está em dia não vira item).
    labels = {d['data']: d['label'] for d in crono['dias']}
    acoes = []
    if crono['receitas']:
        dia0 = crono['dias'][0]
        est0 = estados.get(dia0['data'])
        tem_producao0 = bool(totais_dia[0]['un'] or totais_dia[0]['fornadas'])
        # Enviar hoje só quando a 1ª coluna é HOJE mesmo (inicio=0): o padeiro
        # trabalha a ordem do dia — dia futuro se envia no dia.
        if tem_producao0 and dia0['data'] == crono['hoje'] and not est0:
            acoes.append({'tipo': 'enviar_hoje', 'data': dia0['data'],
                          'label': dia0['label']})
        # Rascunho aprovado e nunca enviado = gesto pela metade, em qualquer
        # dia do grid (o admin criou de propósito; falta concluir).
        for iso in sorted(estados):
            if iso in labels and not estados[iso]['enviado']:
                acoes.append({'tipo': 'rascunho', 'data': iso,
                              'label': labels[iso]})
    for iso in sorted(difere):
        acoes.append({'tipo': 'difere', 'data': iso,
                      'label': labels.get(iso, iso), 'n': len(difere[iso])})
    if resumo['pend_vencido']:
        acoes.append({'tipo': 'vencido', 'n': resumo['pend_vencido']})
    if resumo['risco_n']:
        acoes.append({'tipo': 'risco', 'n': resumo['risco_n']})
    if resumo['stale_n']:
        acoes.append({'tipo': 'stale', 'n': resumo['stale_n']})

    return render_template('industria_teste/teste.html', crono=crono,
                           horizonte=horizonte, janela=janela, inicio=inicio,
                           equilibrar=equilibrar, motor=motor, estados=estados,
                           totais_dia=totais_dia, pico_idx=pico_idx,
                           resumo=resumo, ordem_enviada=ordem_enviada,
                           difere=difere, fechados=fechados, acoes=acoes)


@industria_teste_bp.route('/auditoria')
@login_required
@admin_required
def auditoria():
    """Auditoria da produção mandada: ordens enviadas ao padeiro ainda NÃO
    confirmadas. VENCIDAS (data passou, ninguém marcou produção) em destaque;
    AGENDADAS (hoje/futuro) em tom mais leve. Não mexe em estoque — é leitura."""
    from app.services.producao_pendente import listar_pendencias, produzido_no_dia

    try:
        dias = max(1, min(int(request.values.get('dias', 30)), 365))
    except (TypeError, ValueError):
        dias = 30
    dados = listar_pendencias(dias_vencido=dias)
    produzido_ontem = produzido_no_dia()   # confirmado ontem (fonte: movimentos)
    return render_template('industria_teste/auditoria.html', dados=dados,
                           dias=dias, produzido_ontem=produzido_ontem)


@industria_teste_bp.route('/auditoria/dispensar', methods=['POST'])
@login_required
@admin_required
def dispensar():
    """Fecha a pendência de um item (o admin verificou e deu OK). Não credita
    estoque — só para de mostrar como pendente."""
    from app.services.producao_pendente import dispensar_item

    try:
        item_id = int(request.form.get('item_id'))
    except (TypeError, ValueError):
        flash('Item inválido.', 'warning')
        return redirect(url_for('industria_teste.auditoria'))
    res = dispensar_item(item_id, current_user.id)
    if res['ok']:
        flash('Pendência de %s dispensada (não conta mais como a produzir).'
              % res.get('receita', 'produção'), 'success')
    else:
        flash(res.get('erro', 'Não deu pra dispensar.'), 'warning')
    return redirect(url_for('industria_teste.auditoria',
                            dias=request.form.get('dias') or 30))


@industria_teste_bp.route('/auditoria/dispensar-lote', methods=['POST'])
@login_required
@admin_required
def dispensar_lote():
    """Dispensa em lote os itens marcados (checkboxes da auditoria). Mesma
    semântica do OK individual — só fecha a pendência, não credita estoque."""
    from app.services.producao_pendente import dispensar_itens

    res = dispensar_itens(request.form.getlist('ids'), current_user.id)
    if res.get('n'):
        flash('%d pendência(s) dispensada(s) (não contam mais como a produzir).'
              % res['n'], 'success')
    else:
        flash(res.get('erro', 'Nenhuma pendência marcada.'), 'warning')
    return redirect(url_for('industria_teste.auditoria',
                            dias=request.form.get('dias') or 30))


@industria_teste_bp.route('/auditoria/reverter', methods=['POST'])
@login_required
@admin_required
def reverter_dispensa_rota():
    """Desfaz uma dispensa (volta a mostrar a pendência)."""
    from app.services.producao_pendente import reverter_dispensa

    try:
        item_id = int(request.form.get('item_id'))
    except (TypeError, ValueError):
        flash('Item inválido.', 'warning')
        return redirect(url_for('industria_teste.auditoria'))
    reverter_dispensa(item_id)
    flash('Dispensa desfeita — voltou pra pendente.', 'info')
    return redirect(url_for('industria_teste.auditoria',
                            dias=request.form.get('dias') or 30))


@industria_teste_bp.route('/auditoria/reagendar', methods=['POST'])
@login_required
@admin_required
def reagendar():
    """Manda a FALTA das ordens selecionadas pra a produção de HOJE (o padeiro
    passa a ver em /padeiro). As ordens antigas saem da auditoria (movidas)."""
    from app.services.producao_pendente import reagendar_para_hoje

    ids = request.form.getlist('ids')   # mesmos checkboxes do dispensar em lote
    res = reagendar_para_hoje(ids, current_user.id)
    if res['movidos']:
        flash('%d ordem(ns) · %d un enviada(s) pra produção de HOJE — o padeiro '
              'já vê em /padeiro.' % (res['movidos'], res['unidades']), 'success')
    else:
        flash('Nada pra reagendar (marque ordens com falta a produzir).',
              'warning')
    return redirect(url_for('industria_teste.auditoria',
                            dias=request.form.get('dias') or 30))


@industria_teste_bp.route('/previsao/<int:receita_id>')
@login_required
@admin_required
def previsao(receita_id):
    """Diagnostico: de onde sai o `previsto` de uma receita. Mostra, por dia do
    horizonte, a entrega-alvo, o pedido firme por loja e a previsao do historico
    decomposta por loja (entregas recentes daquele dia-da-semana). Responde 'de
    qual dia/loja vem esse numero?' — abre do expandir do cronograma."""
    from app.services.previsao_producao import decompor_previsao

    horizonte, janela = _horizonte_janela()
    inicio = _inicio_offset()
    motor = _motor()
    dec = decompor_previsao(receita_id, horizonte_dias=horizonte,
                            janela_semanas=janela, inicio_offset_dias=inicio,
                            motor=motor)
    if dec is None:
        flash('Receita não encontrada.', 'warning')
        return redirect(url_for('industria_teste.index', **_params_visao()))
    return render_template('industria_teste/previsao.html', dec=dec,
                           horizonte=horizonte, janela=janela, inicio=inicio,
                           motor=motor)


@industria_teste_bp.route('/aprovar', methods=['POST'])
@login_required
@admin_required
def aprovar():
    """Aprova a coluna de um dia -> cria a ordem de produção (desce pro
    padeiro)."""
    from app.services.producao import PlanoJaEnviadoError, aprovar_plano_do_dia

    try:
        data_alvo = date.fromisoformat(request.form.get('data', ''))
    except (TypeError, ValueError):
        flash('Data inválida.', 'warning')
        return redirect(url_for('industria_teste.index', **_params_visao()))

    # IMPORTANTE: aprovar com o MESMO equilibrar/motor da tela — senao o que
    # desce pro padeiro nao bate com o que voce viu/equilibrou.
    try:
        plano = aprovar_plano_do_dia(data_alvo, current_user.id,
                                     horizonte_dias=_horizonte_janela()[0],
                                     janela_semanas=_horizonte_janela()[1],
                                     inicio_offset_dias=_inicio_offset(),
                                     equilibrar=_equilibrar(),
                                     motor=_motor())
    except PlanoJaEnviadoError:
        # Garantia do dono (04/07/2026): ordem ENVIADA nunca muda por caminho
        # implícito — só pelo "🔄 atualizar produção" explícito daquele dia.
        flash('O dia %s já foi ENVIADO ao padeiro — "aprovar" não mexe em '
              'ordem enviada. Pra aplicar o grid atual na produção, use '
              '"🔄 atualizar produção" naquele dia.'
              % data_alvo.strftime('%d/%m'), 'warning')
        return redirect(url_for('industria_teste.index', **_params_visao()))
    if plano:
        flash('Plano de %s aprovado (%d receita(s)). Revise/edite e clique em '
              '"enviar ao padeiro" quando estiver pronto.'
              % (data_alvo.strftime('%d/%m'), len(plano.itens)), 'success')
    else:
        flash('Nada a produzir em %s.' % data_alvo.strftime('%d/%m'), 'info')
    return redirect(url_for('industria_teste.index', **_params_visao()))


@industria_teste_bp.route('/enviar', methods=['POST'])
@login_required
@admin_required
def enviar():
    """Empurra o cronograma ATUAL do dia (com as edições do grid) pro padeiro:
    reconstrói a ordem a partir do grid e marca enviado. Re-pressável — serve
    pro 1º envio E pra ATUALIZAR a produção depois de editar o grid (a edição
    do grid só chega no padeiro quando se aperta isto)."""
    from app.services.producao import enviar_plano_do_dia

    try:
        data_alvo = date.fromisoformat(request.form.get('data', ''))
    except (TypeError, ValueError):
        flash('Data inválida.', 'warning')
        return redirect(url_for('industria_teste.index', **_params_visao()))
    plano = enviar_plano_do_dia(data_alvo, current_user.id,
                                horizonte_dias=_horizonte_janela()[0],
                                janela_semanas=_horizonte_janela()[1],
                                inicio_offset_dias=_inicio_offset(),
                                equilibrar=_equilibrar(), motor=_motor())
    if plano:
        flash('Produção de %s enviada ao padeiro (%d receita(s)).'
              % (data_alvo.strftime('%d/%m'), len(plano.itens)), 'success')
    else:
        flash('Nada a produzir em %s.' % data_alvo.strftime('%d/%m'), 'info')
    return redirect(url_for('industria_teste.index', **_params_visao()))


@industria_teste_bp.route('/excluir', methods=['POST'])
@login_required
@admin_required
def excluir():
    """Exclui a ordem de produção de um dia (desfaz um envio errado). Bloqueia
    se já houve produção — o estoque/MP reais já mexeram."""
    from app.services.producao import excluir_plano_do_dia

    try:
        data_alvo = date.fromisoformat(request.form.get('data', ''))
    except (TypeError, ValueError):
        flash('Data inválida.', 'warning')
        return redirect(url_for('industria_teste.index', **_params_visao()))
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
    return redirect(url_for('industria_teste.index', **_params_visao()))


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
    from app.services.previsao_producao import MOTORES_PREVISAO_PRODUCAO
    motor = str(p.get('motor') or 'vendas')
    if motor not in MOTORES_PREVISAO_PRODUCAO:
        motor = 'vendas'
    res = editar_celula(
        receita_id, p.get('data') or '', qtd,
        horizonte_dias=_payload_int(p, 'horizonte', 7, 1, 14),
        janela_semanas=_payload_int(p, 'janela', 6, 1, 26),
        inicio_offset_dias=_payload_int(p, 'inicio', 1, 0, 14),
        equilibrar=str(p.get('equilibrar', '1')) in ('1', 'true', 'on', 'True'),
        motor=motor)
    if res is None:
        return jsonify(ok=False, erro='nao_encontrado'), 404
    if res.get('erro'):
        # ex: dia_bloqueado (fornada especial produz só sex/sáb)
        return jsonify(ok=False, erro=res['erro'], msg=res.get('msg')), 422
    return jsonify(ok=True, **res)


@industria_teste_bp.route('/limpar-edicoes', methods=['POST'])
@login_required
@admin_required
def limpar_edicoes():
    """Apaga as edições manuais (rascunhos) do cronograma — tudo volta pra
    sugestão calculada, EXCETO dias fechados com o cadeado (🔒). Não toca em
    pedido enviado, estoque nem MP."""
    from app.services.cronograma_edit import limpar_todos_overrides

    n, preservados = limpar_todos_overrides()
    msg = ('%d edição(ões) manual(is) apagada(s) — cronograma voltou pro '
           'cálculo.' % n if n else 'Não havia edição manual pra limpar.')
    if preservados:
        msg += (' %d edição(ões) de dia(s) fechado(s) com o cadeado (🔒) '
                'foram preservadas.' % preservados)
    flash(msg, 'success')
    return redirect(url_for('industria_teste.index', **_params_visao()))


@industria_teste_bp.route('/reverter-ordem', methods=['POST'])
@login_required
@admin_required
def reverter_ordem():
    """Desfaz as edições do grid de um dia e traz de volta o que foi ENVIADO
    ao padeiro (inverso do "🔄 atualizar produção"). Não toca na ordem em si —
    só no rascunho do grid (overrides)."""
    from app.services.cronograma_edit import reverter_dia_para_ordem_enviada

    try:
        data_alvo = date.fromisoformat(request.form.get('data', ''))
    except (TypeError, ValueError):
        flash('Data inválida.', 'warning')
        return redirect(url_for('industria_teste.index', **_params_visao()))
    res = reverter_dia_para_ordem_enviada(
        data_alvo, horizonte_dias=_horizonte_janela()[0],
        janela_semanas=_horizonte_janela()[1],
        inicio_offset_dias=_inicio_offset(), equilibrar=_equilibrar(),
        motor=_motor())
    if res['ok']:
        flash('Grid de %s desfeito — voltou à ordem que o padeiro está vendo '
              '(%d receita(s)).' % (data_alvo.strftime('%d/%m'), res['n']),
              'success')
    elif res.get('erro') == 'dia_fechado':
        flash('Dia %s está fechado (🔒) — reabra o cadeado para desfazer.'
              % data_alvo.strftime('%d/%m'), 'warning')
    else:
        flash('Não há ordem enviada em %s para reverter.'
              % data_alvo.strftime('%d/%m'), 'warning')
    return redirect(url_for('industria_teste.index', **_params_visao()))


@industria_teste_bp.route('/dia/cadeado', methods=['POST'])
@login_required
@admin_required
def dia_cadeado():
    """Fecha/reabre o cadeado (🔒) de um dia do grid. Dia fechado: edição de
    célula recusada e as ações em massa (limpar edições, reset por linha)
    PULAM o dia. Enviar/atualizar produção continua permitido."""
    from app.services.cronograma_edit import alternar_dia_fechado

    try:
        data_alvo = date.fromisoformat(request.form.get('data', ''))
    except (TypeError, ValueError):
        flash('Data inválida.', 'warning')
        return redirect(url_for('industria_teste.index', **_params_visao()))
    fechado = alternar_dia_fechado(data_alvo, current_user.id)
    if fechado:
        flash('Dia %s fechado (🔒): edições e ações em massa não mexem mais '
              'nele até você reabrir.' % data_alvo.strftime('%d/%m'), 'success')
    else:
        flash('Dia %s reaberto (🔓): voltou a aceitar edições.'
              % data_alvo.strftime('%d/%m'), 'success')
    return redirect(url_for('industria_teste.index', **_params_visao()))


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
    apagados, preservados = resetar_receita(receita_id, datas)
    # preservados > 0 = havia override em dia fechado (🔒) que ficou — o JS
    # avisa antes do reload (senão o ✏️ que sobra parece bug).
    return jsonify(ok=True, apagados=apagados, preservados=preservados)


@industria_teste_bp.route('/mp-dia')
@login_required
@admin_required
def mp_dia():
    """Matéria-prima necessária pra produção de UM dia do grid (com as
    edições aplicadas) vs estoque atual de MP — "tenho insumo pra isso?"
    antes de enviar. Read-only, JSON (modal da tela). Mesma explosão da
    pré-baixa/baixa real (`producao.mp_necessaria_do_dia`)."""
    from app.services.producao import mp_necessaria_do_dia

    try:
        data_alvo = date.fromisoformat(request.args.get('data', ''))
    except (TypeError, ValueError):
        return jsonify(ok=False, erro='data'), 400
    horizonte, janela = _horizonte_janela()
    res = mp_necessaria_do_dia(
        data_alvo, horizonte_dias=horizonte, janela_semanas=janela,
        inicio_offset_dias=_inicio_offset(), equilibrar=_equilibrar(),
        motor=_motor())
    if res is None:
        return jsonify(ok=False, erro='fora_do_grid'), 404
    return jsonify(ok=True, **res)


@industria_teste_bp.route('/ia-proposta', methods=['POST'])
@login_required
@admin_required
def ia_proposta():
    """Análise do cronograma pela IA (Opus 4.8): devolve AJUSTES de célula
    propostos com motivo + parecer. Read-only — aplicar é outro gesto."""
    from app.services import planejamento_ia
    from app.services.previsao_producao import MOTORES_PREVISAO_PRODUCAO

    p = request.get_json(silent=True) or request.form
    motor = str(p.get('motor') or 'vendas')
    if motor not in MOTORES_PREVISAO_PRODUCAO:
        motor = 'vendas'
    out = planejamento_ia.analisar_producao_ia(
        horizonte_dias=_payload_int(p, 'horizonte', 7, 1, 14),
        janela_semanas=_payload_int(p, 'janela', 6, 1, 26),
        # inicio default 0 = HOJE, mesma base da tela de produção
        # (_inicio_offset); a 1ª coluna do grid é hoje.
        inicio_offset_dias=_payload_int(p, 'inicio', 0, 0, 14),
        equilibrar=str(p.get('equilibrar', '1')) in ('1', 'true', 'on', 'True'),
        motor=motor)
    if out.get('erro'):
        return jsonify(ok=False, erro=out['erro']), 502
    return jsonify(ok=True, **out)


@industria_teste_bp.route('/ia-aplicar', methods=['POST'])
@login_required
@admin_required
def ia_aplicar():
    """Aplica os ajustes MARCADOS da proposta da IA — cada um vira um
    override de rascunho via editar_celula (mesmas guardas da edição
    manual: fornada especial, receita fora do grid etc.). ENVIAR ao
    padeiro continua gesto humano na tela."""
    from app.services.cronograma_edit import editar_celula
    from app.services.previsao_producao import MOTORES_PREVISAO_PRODUCAO

    p = request.get_json(silent=True) or {}
    motor = str(p.get('motor') or 'vendas')
    if motor not in MOTORES_PREVISAO_PRODUCAO:
        motor = 'vendas'
    ajustes = p.get('ajustes') or []
    if not isinstance(ajustes, list) or not ajustes:
        return jsonify(ok=False, erro='nenhum ajuste marcado'), 400
    # Cap defensivo: a IA propõe poucos ajustes; cada editar_celula
    # recomputa o cronograma 2x (pesado), então limita o abuso via POST.
    aplicados, falhas = [], []
    for a in ajustes[:50]:
        try:
            rid = int(a.get('receita_id'))
            qtd = max(0, int(a.get('qtd')))
            # valida a data AQUI: editar_celula faz date.fromisoformat sem
            # try, então data ruim viraria 500 no meio do loop (com
            # ajustes anteriores já commitados).
            data = date.fromisoformat(str(a.get('data') or '')).isoformat()
        except (TypeError, ValueError, AttributeError):
            falhas.append({'receita_id': a.get('receita_id')
                           if isinstance(a, dict) else None,
                           'data': a.get('data') if isinstance(a, dict)
                           else None, 'erro': 'parametros'})
            continue
        res = editar_celula(
            rid, data, qtd,
            horizonte_dias=_payload_int(p, 'horizonte', 7, 1, 14),
            janela_semanas=_payload_int(p, 'janela', 6, 1, 26),
            inicio_offset_dias=_payload_int(p, 'inicio', 0, 0, 14),
            equilibrar=str(p.get('equilibrar', '')) in ('1', 'true', 'on',
                                                        'True'),
            motor=motor)
        if res is None:
            falhas.append({'receita_id': rid, 'data': data,
                           'erro': 'nao_encontrado'})
        elif res.get('erro'):
            falhas.append({'receita_id': rid, 'data': data,
                           'erro': res['erro'], 'msg': res.get('msg')})
        else:
            aplicados.append({'receita_id': rid, 'data': data, 'qtd': qtd})
    return jsonify(ok=True, aplicados=aplicados, falhas=falhas)
