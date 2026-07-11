from datetime import datetime

from flask import flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.blueprints.producao import producao_bp
from app.decorators import admin_required, producao_required
from app.extensions import db
from app.models import (
    MateriaPrima,
    MovimentacaoEstoque,
    PlanejamentoItem,
    PlanejamentoProducao,
    Receita,
)
from app.services.producao import (
    consolidar_lista_compras,
    fornadas_amassadeira,
)
from app.utils import hoje as hoje_brt


def _inicio_offset():
    """Offset (em dias) do INICIO do horizonte de planejamento. Default 1 =
    amanha (a producao de hoje ja esta decidida quando se olha o painel). 0 =
    hoje. Le de args (GET) ou form (POST). Limite defensivo 0..14."""
    try:
        v = int(request.values.get('inicio', 1))
    except (TypeError, ValueError):
        v = 1
    return max(0, min(v, 14))


@producao_bp.route('/')
@login_required
@producao_required
def lista():
    planos = PlanejamentoProducao.query.order_by(
        PlanejamentoProducao.data.desc()
    ).limit(50).all()
    return render_template('producao/lista.html', planos=planos)


@producao_bp.route('/novo', methods=['GET', 'POST'])
@login_required
@producao_required
def novo():
    if request.method == 'POST':
        data_str = request.form.get('data', '')
        nome = request.form.get('nome', '').strip()
        receita_ids = request.form.getlist('receita_id[]')
        multiplicadores = request.form.getlist('multiplicador[]')

        try:
            data_plan = datetime.strptime(data_str, '%Y-%m-%d').date()
        except ValueError:
            data_plan = hoje_brt()

        plano = PlanejamentoProducao(
            data=data_plan,
            nome=nome or f'Produção {data_plan.strftime("%d/%m")}',
            criado_por=current_user.id,
        )
        db.session.add(plano)
        db.session.flush()

        for i, rid in enumerate(receita_ids):
            if not rid:
                continue
            mult = int(multiplicadores[i]) if i < len(multiplicadores) and multiplicadores[i] else 1
            item = PlanejamentoItem(
                planejamento_id=plano.id,
                receita_id=int(rid),
                multiplicador=max(1, mult),
            )
            db.session.add(item)

        db.session.commit()
        flash('Plano de produção criado!', 'success')
        return redirect(url_for('producao.detalhe', id=plano.id))

    receitas = Receita.query.order_by(Receita.categoria, Receita.nome).all()
    return render_template('producao/novo.html', receitas=receitas, hoje=hoje_brt())


@producao_bp.route('/<int:id>')
@login_required
@producao_required
def detalhe(id):
    plano = PlanejamentoProducao.query.get_or_404(id)
    itens = [{'receita_id': i.receita_id, 'multiplicador': i.multiplicador}
             for i in plano.itens]
    lista_compras = consolidar_lista_compras(itens)
    lista_ordenada = sorted(lista_compras.items(), key=lambda x: x[0])
    custo_total = sum(v['custo_estimado'] for v in lista_compras.values())

    # Enriquece cada item com FORNADAS (batidas da amassadeira) e unidades.
    # Receita que nao usa amassadeira (capacidade 0) mostra unidades, sem fornada.
    itens_view = []
    for i in plano.itens:
        rec = i.receita
        unidades = int(i.multiplicador * (rec.rendimento_qtd or 0)) if rec else 0
        itens_view.append({
            'item': i,
            'fornadas': fornadas_amassadeira(rec, i.multiplicador),
            'unidades': unidades,
        })

    return render_template('producao/detalhe.html', plano=plano,
                           itens_view=itens_view, lista_compras=lista_ordenada,
                           custo_total=custo_total)


@producao_bp.route('/<int:id>/lista-compras')
@login_required
@producao_required
def lista_compras(id):
    """Ordem de compra de MP do plano, agrupada por fornecedor, com o que ha
    A COMPRAR (deficit) destacado. Print-friendly."""
    from app.services.producao import ordem_compra_consolidada

    plano = PlanejamentoProducao.query.get_or_404(id)
    itens = [{'receita_id': i.receita_id, 'multiplicador': i.multiplicador}
             for i in plano.itens]
    ordem = ordem_compra_consolidada(itens)
    return render_template('producao/lista_compras.html', plano=plano,
                           ordem=ordem)


@producao_bp.route('/<int:id>/baixar-estoque', methods=['POST'])
@login_required
@producao_required
def baixar_estoque(id):
    plano = PlanejamentoProducao.query.get_or_404(id)
    itens = [{'receita_id': i.receita_id, 'multiplicador': i.multiplicador} for i in plano.itens]
    lista = consolidar_lista_compras(itens)
    mps = {mp.nome: mp for mp in MateriaPrima.query.all()}

    for nome, dados in lista.items():
        mp = mps.get(nome)
        if not mp:
            continue
        mov = MovimentacaoEstoque(
            materia_prima_id=mp.id,
            tipo='saida',
            quantidade=dados['quantidade'],
            referencia=f'Produção: {plano.nome}',
            usuario_id=current_user.id,
        )
        db.session.add(mov)
        mp.estoque_atual = max(0, (mp.estoque_atual or 0) - dados['quantidade'])

    plano.status = 'executado'
    db.session.commit()
    flash('Estoque baixado com base no plano de produção.', 'success')
    return redirect(url_for('producao.detalhe', id=id))


@producao_bp.route('/<int:id>/excluir', methods=['POST'])
@login_required
@producao_required
def excluir(id):
    plano = PlanejamentoProducao.query.get_or_404(id)
    db.session.delete(plano)
    db.session.commit()
    flash('Plano excluído.', 'success')
    return redirect(url_for('producao.lista'))


@producao_bp.route('/painel')
@login_required
@admin_required
def painel():
    """Balanco de producao da industria — por receita: estoque x comprometido
    x previsto (historico de PedidoLoja) -> quanto produzir. **Admin-only**:
    admin ve os dados e decide o que mandar a producao executar."""
    from datetime import timedelta

    from app.constants import STATUS_PEDIDO_FINALIZADOS
    from app.models import PedidoLoja
    from app.services.cestas import contar_produto_itens_orfaos
    from app.services.previsao_producao import balanco_industria

    hoje_d = hoje_brt()
    amanha = hoje_d + timedelta(days=1)

    try:
        horizonte = int(request.args.get('horizonte', 7))
    except ValueError:
        horizonte = 7
    horizonte = max(1, min(horizonte, 14))

    try:
        janela = int(request.args.get('janela', 6))
    except ValueError:
        janela = 6
    janela = max(1, min(janela, 26))

    inicio = _inicio_offset()
    inicio_data = hoje_d + timedelta(days=inicio)

    # Zona 1 — alertas
    pedidos_atrasados = (PedidoLoja.query
                         .filter(PedidoLoja.data_entrega < hoje_d,
                                 ~PedidoLoja.status.in_(STATUS_PEDIDO_FINALIZADOS))
                         .order_by(PedidoLoja.data_entrega).all())
    cestas_orfaos = contar_produto_itens_orfaos()

    # Zona 2 — balanco da industria (estoque x comprometido x previsto)
    balanco = balanco_industria(horizonte_dias=horizonte, janela_semanas=janela,
                                inicio_offset_dias=inicio)

    # Zona 3 — saindo hoje
    saindo_hoje = (PedidoLoja.query
                   .filter(PedidoLoja.data_entrega == hoje_d,
                           ~PedidoLoja.status.in_(STATUS_PEDIDO_FINALIZADOS))
                   .order_by(PedidoLoja.loja_id, PedidoLoja.id).all())

    return render_template('producao/painel.html',
                           hoje=hoje_d,
                           amanha=amanha,
                           horizonte=horizonte,
                           janela=janela,
                           inicio=inicio,
                           inicio_data=inicio_data,
                           pedidos_atrasados=pedidos_atrasados,
                           cestas_orfaos=cestas_orfaos,
                           balanco=balanco,
                           saindo_hoje=saindo_hoje)


@producao_bp.route('/painel/receita/<int:rid>')
@login_required
@admin_required
def painel_receita_grade(rid):
    """Grade loja x dia de UMA receita: quanto cada loja recebe em cada dia do
    horizonte. Detalha o que o balanco resume — firme (pedidos reais com data)
    + estimado (projecao do previsto rateada por loja/dia). Aberta a partir do
    balanco da producao. **Admin-only** (mesma sensibilidade do painel)."""
    from app.services.previsao_producao import grade_loja_dia

    try:
        horizonte = int(request.args.get('horizonte', 7))
    except ValueError:
        horizonte = 7
    horizonte = max(1, min(horizonte, 14))

    try:
        janela = int(request.args.get('janela', 6))
    except ValueError:
        janela = 6
    janela = max(1, min(janela, 26))

    inicio = _inicio_offset()

    # Fragmento (drop-down inline do balanco) vs pagina inteira (acesso direto).
    partial = (bool(request.args.get('partial'))
               or request.headers.get('X-Requested-With') == 'XMLHttpRequest')

    grade = grade_loja_dia(rid, horizonte_dias=horizonte, janela_semanas=janela,
                           inicio_offset_dias=inicio)
    if grade is None:
        if partial:
            return ('<div class="text-danger small py-2">'
                    'Receita não encontrada.</div>', 404)
        flash('Receita não encontrada.', 'warning')
        return redirect(url_for('producao.painel', horizonte=horizonte,
                                janela=janela, inicio=inicio))

    tpl = ('producao/_grade_receita_tabela.html' if partial
           else 'producao/grade_receita.html')
    return render_template(tpl, grade=grade, horizonte=horizonte, janela=janela,
                           inicio=inicio)


@producao_bp.route('/pedidos-semana')
@login_required
@admin_required
def pedidos_semana():
    """APOSENTADA (01/07/2026, decisao do dono): a tela de 'previsao automatica
    por dia' saiu de uso — a de MEDIA SEMANAL virou a principal. A rota fica como
    REDIRECT pra nao quebrar bookmarks/links antigos; o motor `sugerir_pedidos_
    semana` continua vivo (a acuracia do forecast depende dele, ver
    previsao_acuracia.py). NAO ressuscitar a tela sem ordem explicita."""
    try:
        horizonte = max(1, min(int(request.args.get('horizonte', 7)), 14))
        janela = max(1, min(int(request.args.get('janela', 6)), 26))
    except (TypeError, ValueError):
        horizonte, janela = 7, 6
    return redirect(url_for('producao.pedidos_semana_media', horizonte=horizonte,
                            janela=janela, inicio=_inicio_offset()))


@producao_bp.route('/pedidos-semana/media')
@login_required
@admin_required
def pedidos_semana_media():
    """Modo MANUAL: media semanal por loja/produto dividida igual entre os dias
    do horizonte, pro admin AJUSTAR o split. Mesmo POST de gerar. **Admin**."""
    from app.services.previsao_producao import media_semanal_pedidos

    try:
        horizonte = int(request.args.get('horizonte', 7))
    except ValueError:
        horizonte = 7
    horizonte = max(1, min(horizonte, 14))
    try:
        janela = int(request.args.get('janela', 6))
    except ValueError:
        janela = 6
    janela = max(1, min(janela, 26))

    inicio = _inicio_offset()
    grade = media_semanal_pedidos(horizonte_dias=horizonte,
                                  janela_semanas=janela,
                                  inicio_offset_dias=inicio)
    # Contraprova opcional (?comparar=1): o numero do OUTRO motor
    # (venda+estoque) aparece sob cada celula — nao muda a conta de nada,
    # so poe os dois lado a lado pro operador decidir.
    contraprova = {}
    comparar = request.args.get('comparar') == '1'
    if comparar:
        from app.services.previsao_producao import sugerir_pedidos_por_venda
        venda = sugerir_pedidos_por_venda(horizonte_dias=horizonte,
                                          janela_semanas=janela,
                                          inicio_offset_dias=inicio)
        for lj in venda['lojas']:
            contraprova[lj['loja_id']] = {
                p['receita_id']: p['por_dia'] for p in lj['produtos']
                if p.get('receita_id') and not p.get('eh_mp')}
    from app.services.previsao_acuracia import acuracia_por_loja_receita
    from app.services.previsao_producao import desperdicio_recente_por_item
    return render_template('producao/pedidos_semana_media.html',
                           grade=grade, horizonte=horizonte,
                           janela=janela, inicio=inicio,
                           comparar=comparar, contraprova=contraprova,
                           acuracia=acuracia_por_loja_receita('media_pedido'),
                           desperdicio=desperdicio_recente_por_item())


@producao_bp.route('/pedidos-semana/estoque')
@login_required
@admin_required
def pedidos_semana_estoque():
    """Maneira 2: previsao por VENDA + ESTOQUE (ponto de reposicao). Pede o que
    falta pra cobrir a venda prevista (media por dia-da-semana) menos o estoque
    atual da loja, arredondado pra cima na caixa. Mesmo POST de gerar. **Admin**."""
    from app.services.previsao_producao import sugerir_pedidos_por_venda

    try:
        horizonte = int(request.args.get('horizonte', 7))
    except ValueError:
        horizonte = 7
    horizonte = max(1, min(horizonte, 14))
    try:
        janela = int(request.args.get('janela', 6))
    except ValueError:
        janela = 6
    janela = max(1, min(janela, 26))

    inicio = _inicio_offset()
    # Estoque de seguranca opcional (% do consumo do dia que sobra no fim do
    # dia como colchao). 0 = repor exatamente a media (comportamento v1).
    try:
        seguranca = int(request.args.get('seguranca', 0))
    except ValueError:
        seguranca = 0
    seguranca = max(0, min(seguranca, 100))
    grade = sugerir_pedidos_por_venda(horizonte_dias=horizonte,
                                      janela_semanas=janela,
                                      inicio_offset_dias=inicio,
                                      seguranca_pct=seguranca)
    # Contraprova opcional (?comparar=1): o numero do motor de MEDIA sob
    # cada celula (MP nao tem contraprova — so existe nesta grade).
    contraprova = {}
    comparar = request.args.get('comparar') == '1'
    if comparar:
        from app.services.previsao_producao import media_semanal_pedidos
        media = media_semanal_pedidos(horizonte_dias=horizonte,
                                      janela_semanas=janela,
                                      inicio_offset_dias=inicio)
        for lj in media['lojas']:
            contraprova[lj['loja_id']] = {
                p['receita_id']: p['por_dia'] for p in lj['produtos']}
    from app.services.previsao_acuracia import acuracia_por_loja_receita
    from app.services.previsao_producao import desperdicio_recente_por_item
    return render_template('producao/pedidos_semana_estoque.html',
                           grade=grade, horizonte=horizonte,
                           janela=janela, inicio=inicio,
                           seguranca=seguranca,
                           comparar=comparar, contraprova=contraprova,
                           acuracia=acuracia_por_loja_receita('venda_estoque'),
                           desperdicio=desperdicio_recente_por_item())


@producao_bp.route('/pedidos-semana/gerar', methods=['POST'])
@login_required
@admin_required
def pedidos_semana_gerar():
    """Aplica a grade da tela: dia SEM pedido vira rascunho; dia COM pedido
    ainda EDITAVEL (pendente/confirmado) tem os itens ATUALIZADOS (a tela da
    média destrava essas células — qtd 0 remove o item).
    Campos do form: 'qtd|<loja_id>|<data_iso>|<receita_id>' = quantidade.

    Botão "Gerar só esta loja" manda `so_loja=<loja_id>`: gera APENAS aquela loja
    (o dono pediu enviar loja a loja, não tudo de uma vez). Gerar TODAS exige
    `gerar_todas=1` EXPLÍCITO — POST sem nenhuma ação não gera nada (fail-closed):
    o Safari descarta o name/value do submit button quando o clique passa por um
    confirm(), e o "só esta loja" chegava sem so_loja e gerava todas (06/07/2026).
    """
    from datetime import date

    from app.services.pedidos_semana import aplicar_grade

    def _primeiro_nao_vazio(nome):
        # A ação vem em DOIS lugares com o mesmo nome: o hidden preenchido por
        # JS no clique e o name/value do próprio botão — qualquer um dos dois
        # pode faltar (Safari dropa o do botão; sem JS falta o hidden).
        for v in request.form.getlist(nome):
            v = (v or '').strip()
            if v:
                return v
        return None

    def _voltar():
        # Preserva a visão (horizonte/janela/inicio) E a tela de origem — na
        # geração por loja você continua na MESMA tela pra mandar a próxima.
        try:
            horizonte = max(1, min(int(request.form.get('horizonte', 7)), 14))
            janela = max(1, min(int(request.form.get('janela', 6)), 26))
            inicio = max(0, min(int(request.form.get('inicio', 0)), 14))
        except (TypeError, ValueError):
            horizonte, janela, inicio = 7, 6, 0
        destino = ('producao.pedidos_semana_estoque'
                   if request.form.get('origem') == 'estoque'
                   else 'producao.pedidos_semana_media')
        return redirect(url_for(destino, horizonte=horizonte, janela=janela,
                                inicio=inicio))

    try:
        so_loja = int(_primeiro_nao_vazio('so_loja') or 0) or None
    except (TypeError, ValueError):
        so_loja = None
    # Botao "atualizar" do cabecalho do dia: aplica SO aquele (loja, dia) —
    # atualiza o pedido existente sem mexer no resto da grade.
    so_dia = None
    bruto = _primeiro_nao_vazio('so_dia') or ''
    if bruto and '|' in bruto:
        loja_s, data_s = bruto.split('|', 1)
        try:
            from datetime import date as _date
            so_dia = (int(loja_s), _date.fromisoformat(data_s))
        except (TypeError, ValueError):
            so_dia = None
    gerar_todas = _primeiro_nao_vazio('gerar_todas') is not None

    if so_loja is None and so_dia is None and not gerar_todas:
        # Nenhuma ação identificada: NUNCA cair no "gera todas" por omissão.
        if request.form.get('ajax') == '1':
            return jsonify(ok=False, mudou=False,
                           msg='Ação não identificada — nada foi gerado.'), 400
        flash('Nenhum pedido foi gerado: não deu pra identificar qual botão '
              'foi clicado (proteção contra gerar TODAS as lojas sem querer). '
              'Tente de novo pelo botão desejado.', 'warning')
        return _voltar()

    agrupado = {}   # (loja_id, data) -> list[{receita_id|materia_prima_id, qtd}]
    for chave, valor in request.form.items():
        if not chave.startswith('qtd|'):
            continue
        partes = chave.split('|')
        if len(partes) != 4:
            continue
        _, loja_s, data_s, item_s = partes
        try:
            loja_id = int(loja_s)
            qtd = int(valor or 0)
            data_ent = date.fromisoformat(data_s)
            # Item: int puro = receita (formato original); 'mp:<id>' =
            # materia-prima (ex: pao de queijo congelado — a loja pede e a
            # industria ENVIA sem produzir).
            if item_s.startswith('mp:'):
                item = {'materia_prima_id': int(item_s[3:])}
            else:
                item = {'receita_id': int(item_s)}
        except (TypeError, ValueError):
            continue
        if qtd < 0:
            continue                      # 0 passa: em dia editavel REMOVE o item
        if so_dia is not None and (loja_id, data_ent) != so_dia:
            continue                      # "atualizar este dia": ignora o resto
        if so_dia is None and so_loja is not None and loja_id != so_loja:
            continue                      # "só esta loja": ignora as outras
        agrupado.setdefault((loja_id, data_ent), []).append(
            {**item, 'qtd': qtd})

    pedidos = [{'loja_id': k[0], 'data_entrega': k[1], 'itens': v}
               for k, v in agrupado.items()]
    res = aplicar_grade(pedidos, current_user.id)

    if so_loja is not None:
        from app.models import Loja
        loja = db.session.get(Loja, so_loja)
        alvo = ('de %s' % loja.nome) if loja else 'da loja'
    else:
        alvo = 'de todas as lojas'
    partes_msg = []
    if res['criados']:
        partes_msg.append(f"{res['criados']} pedido(s) rascunho {alvo} "
                          f"criado(s) ({res['itens']} itens)")
    if res['atualizados']:
        partes_msg.append(f"{res['atualizados']} pedido(s) existente(s) "
                          f"atualizado(s) ({res['itens_ajustados']} item(ns))")
    if res['itens_ambiguos']:
        partes_msg.append(f"{res['itens_ambiguos']} item(ns) com estados "
                          "(assado/backup) não ajustados — edite pelo pedido")
    if res['pulados_nao_editavel']:
        partes_msg.append(f"{res['pulados_nao_editavel']} dia(s) já em "
                          "separação/entrega — não editados")

    # Auto-save da tela da média (ajax=1): mesma lógica, resposta JSON no
    # lugar de flash+redirect — a tela salva a coluna enquanto o usuário
    # digita, sem recarregar a página.
    if request.form.get('ajax') == '1':
        mudou = bool(res['criados'] or res['atualizados'])
        if partes_msg:
            msg = '. '.join(partes_msg) + '.'
        else:
            msg = 'Nada a atualizar (coluna igual ao pedido).'
        return jsonify(ok=True, mudou=mudou, msg=msg, res=res)

    if partes_msg:
        flash('. '.join(partes_msg) + '. Revise e confirme em Pedidos.',
              'success' if (res['criados'] or res['atualizados']) else 'warning')
    else:
        flash('Nada a criar nem atualizar (grade igual aos pedidos existentes).',
              'info')

    return _voltar()


@producao_bp.route('/painel/debug')
@login_required
@admin_required
def painel_debug():
    """Dump dos pedidos que o motor balanco_industria ENXERGA — pra
    auditoria. Mostra Comprometido (proximos N dias) E Historico (M semanas)
    detalhados pedido a pedido, mais a lista de TODAS as Lojas com contagem.
    Crucial: revela se o motor esta perdendo alguma loja ou status.
    """
    from datetime import timedelta

    from app.constants import STATUS_PEDIDO_NAO_BAIXADOS
    from app.models import Loja, MateriaPrima, PedidoItem, PedidoLoja, Produto

    try:
        horizonte = int(request.args.get('horizonte', 7))
    except ValueError:
        horizonte = 7
    horizonte = max(1, min(horizonte, 14))
    try:
        janela = int(request.args.get('janela', 6))
    except ValueError:
        janela = 6
    janela = max(1, min(janela, 26))

    hoje_d = hoje_brt()
    horizonte_fim = hoje_d + timedelta(days=horizonte - 1)
    hist_ini = hoje_d - timedelta(days=7 * janela)
    hist_fim = hoje_d - timedelta(days=1)

    nomes_loja = {l.id: l.nome for l in Loja.query.all()}

    # Pedidos QUE ENTRAM no Comprometido — mesma query do motor + dados extra.
    comprometido_pedidos = (db.session.query(PedidoLoja.id, PedidoLoja.loja_id,
                                              PedidoLoja.status,
                                              PedidoLoja.data_entrega,
                                              PedidoItem.receita_id,
                                              PedidoItem.quantidade,
                                              Receita.nome)
                            .join(PedidoItem, PedidoItem.pedido_id == PedidoLoja.id)
                            .join(Receita, Receita.id == PedidoItem.receita_id)
                            .filter(PedidoItem.receita_id.isnot(None),
                                    PedidoLoja.status.in_(STATUS_PEDIDO_NAO_BAIXADOS),
                                    PedidoLoja.data_entrega >= hoje_d,
                                    PedidoLoja.data_entrega <= horizonte_fim)
                            .order_by(PedidoLoja.data_entrega, PedidoLoja.loja_id,
                                      Receita.nome)
                            .all())

    # Pedidos QUE NAO ENTRAM no Comprometido mas estao no horizonte —
    # exclusao por status (em_transporte/entregue/recebido/cancelado).
    # Crucial pra diagnosticar quando uma loja "some" do balanco.
    excluidos_status = (db.session.query(PedidoLoja.id, PedidoLoja.loja_id,
                                          PedidoLoja.status,
                                          PedidoLoja.data_entrega,
                                          PedidoItem.receita_id,
                                          PedidoItem.quantidade,
                                          Receita.nome)
                        .join(PedidoItem, PedidoItem.pedido_id == PedidoLoja.id)
                        .join(Receita, Receita.id == PedidoItem.receita_id)
                        .filter(PedidoItem.receita_id.isnot(None),
                                ~PedidoLoja.status.in_(STATUS_PEDIDO_NAO_BAIXADOS),
                                PedidoLoja.data_entrega >= hoje_d,
                                PedidoLoja.data_entrega <= horizonte_fim)
                        .order_by(PedidoLoja.data_entrega, PedidoLoja.loja_id)
                        .all())

    # Pedidos no horizonte mas com data_entrega ANTERIOR a hoje (atrasados).
    atrasados = (db.session.query(PedidoLoja.id, PedidoLoja.loja_id,
                                   PedidoLoja.status, PedidoLoja.data_entrega,
                                   PedidoItem.receita_id, PedidoItem.quantidade,
                                   Receita.nome)
                 .join(PedidoItem, PedidoItem.pedido_id == PedidoLoja.id)
                 .join(Receita, Receita.id == PedidoItem.receita_id)
                 .filter(PedidoItem.receita_id.isnot(None),
                         PedidoLoja.status.in_(STATUS_PEDIDO_NAO_BAIXADOS),
                         PedidoLoja.data_entrega < hoje_d)
                 .order_by(PedidoLoja.data_entrega.desc())
                 .limit(50).all())

    # Por loja: contagem de pedidos comprometidos, excluidos por status,
    # e historico. Lista TODAS as Lojas (incluindo inativas), pra revelar
    # alguma loja "fantasma" ou diferenca de nome.
    contagem_por_loja = {l.id: {'nome': l.nome, 'ativa': l.ativa,
                                 'comprometido': 0, 'excluido': 0,
                                 'historico': 0}
                          for l in Loja.query.order_by(Loja.nome).all()}
    for r in comprometido_pedidos:
        if r.loja_id in contagem_por_loja:
            contagem_por_loja[r.loja_id]['comprometido'] += 1
    for r in excluidos_status:
        if r.loja_id in contagem_por_loja:
            contagem_por_loja[r.loja_id]['excluido'] += 1

    historico_count_por_loja = dict(
        db.session.query(PedidoLoja.loja_id, db.func.count(PedidoLoja.id.distinct()))
        .join(PedidoItem, PedidoItem.pedido_id == PedidoLoja.id)
        .filter(PedidoItem.receita_id.isnot(None),
                PedidoLoja.status != 'cancelado',
                PedidoLoja.data_entrega >= hist_ini,
                PedidoLoja.data_entrega <= hist_fim)
        .group_by(PedidoLoja.loja_id).all())
    for lid, cnt in historico_count_por_loja.items():
        if lid in contagem_por_loja:
            contagem_por_loja[lid]['historico'] = cnt

    # SECAO "CACA O FANTASMA" — todos os PedidoItem do horizonte,
    # independente da FK. Cobre 3 hipoteses do dono (24/06/2026):
    # 1) item gravado com produto_id em vez de receita_id (motor filtra fora)
    # 2) item gravado com materia_prima_id (idem)
    # 3) item ORFAO (3 FKs NULL) — so item_nome textual
    # Pra cada linha, mostra: loja, pedido, status, data_entrega, item_nome,
    # qual FK esta setada (REC/PROD/MP/NENHUMA), nome do alvo da FK.
    # Tambem agrupa por busca textual "Pão Francês" (case-insensitive) pra
    # achar variantes.
    todos_itens_horizonte = (db.session.query(PedidoLoja.id, PedidoLoja.loja_id,
                                               PedidoLoja.status,
                                               PedidoLoja.data_entrega,
                                               PedidoItem.id.label('item_id'),
                                               PedidoItem.receita_id,
                                               PedidoItem.produto_id,
                                               PedidoItem.materia_prima_id,
                                               PedidoItem.quantidade,
                                               PedidoItem.observacao)
                              .join(PedidoItem, PedidoItem.pedido_id == PedidoLoja.id)
                              .filter(PedidoLoja.data_entrega >= hoje_d,
                                      PedidoLoja.data_entrega <= horizonte_fim)
                              .order_by(PedidoLoja.loja_id,
                                        PedidoLoja.data_entrega).all())

    # Resolve nomes das 3 FKs em lote.
    rec_ids = {r.receita_id for r in todos_itens_horizonte if r.receita_id}
    prod_ids = {r.produto_id for r in todos_itens_horizonte if r.produto_id}
    mp_ids = {r.materia_prima_id for r in todos_itens_horizonte
              if r.materia_prima_id}
    nomes_rec = {r.id: r.nome for r in
                 Receita.query.filter(Receita.id.in_(rec_ids)).all()} if rec_ids else {}
    nomes_prod = {p.id: p.nome for p in
                  Produto.query.filter(Produto.id.in_(prod_ids)).all()} if prod_ids else {}
    nomes_mp = {m.id: m.nome for m in
                MateriaPrima.query.filter(MateriaPrima.id.in_(mp_ids)).all()} if mp_ids else {}

    # Enriquece cada linha + identifica fantasmas (nao-receita).
    itens_enriquecidos = []
    fantasmas = []  # itens que nao entram no balanco mas tem nome reconhecivel
    for r in todos_itens_horizonte:
        if r.receita_id:
            fk = 'REC'
            alvo = nomes_rec.get(r.receita_id, f'?id={r.receita_id}')
        elif r.produto_id:
            fk = 'PROD'
            alvo = nomes_prod.get(r.produto_id, f'?id={r.produto_id}')
        elif r.materia_prima_id:
            fk = 'MP'
            alvo = nomes_mp.get(r.materia_prima_id, f'?id={r.materia_prima_id}')
        else:
            fk = 'NENHUMA'
            alvo = (r.observacao or '<sem FK e sem observacao>')
        entrada = {
            'pedido_id': r.id, 'loja_id': r.loja_id, 'status': r.status,
            'data_entrega': r.data_entrega, 'item_id': r.item_id,
            'fk_tipo': fk, 'alvo_nome': alvo,
            'receita_id': r.receita_id, 'produto_id': r.produto_id,
            'materia_prima_id': r.materia_prima_id,
            'quantidade': r.quantidade, 'observacao': r.observacao,
        }
        itens_enriquecidos.append(entrada)
        # Fantasma = nao entra no balanco (motor filtra receita_id NOT NULL)
        if fk != 'REC' and r.status in STATUS_PEDIDO_NAO_BAIXADOS:
            fantasmas.append(entrada)

    # Variantes de receita por nome — agrupar por "primeira palavra" pra
    # detectar receitas duplicadas (ex: "Pão Francês" vs "Pão Francês Fermentado").
    variantes_receita = {}
    for rec in Receita.query.order_by(Receita.nome).all():
        chave = (rec.nome.split()[0] if rec.nome else '?').lower()
        variantes_receita.setdefault(chave, []).append(
            {'id': rec.id, 'nome': rec.nome,
             'arquivada': rec.arquivada_em is not None})
    # So mostra grupos com 2+ variantes.
    variantes_receita = {k: v for k, v in variantes_receita.items()
                          if len(v) >= 2}

    # BUSCA POR ITEM — responde "quais lojas pediram X" pra qualquer termo.
    # Cobre os 3 tipos de FK (receita/produto/mp), pra revelar inclusive se um
    # item foi cadastrado como produto/MP em alguma loja (e por isso some do
    # balanco de producao). Janela: 60 dias atras ate o fim do horizonte.
    busca = (request.args.get('q') or '').strip()
    busca_linhas = []
    busca_resumo = []
    if busca:
        like = f'%{busca}%'
        janela_busca_ini = hoje_d - timedelta(days=60)
        q = (db.session.query(PedidoLoja.id, PedidoLoja.loja_id,
                              PedidoLoja.status, PedidoLoja.data_entrega,
                              PedidoItem.receita_id, PedidoItem.produto_id,
                              PedidoItem.materia_prima_id,
                              PedidoItem.quantidade,
                              Receita.nome.label('rec_nome'),
                              Produto.nome.label('prod_nome'),
                              MateriaPrima.nome.label('mp_nome'))
             .join(PedidoItem, PedidoItem.pedido_id == PedidoLoja.id)
             .outerjoin(Receita, Receita.id == PedidoItem.receita_id)
             .outerjoin(Produto, Produto.id == PedidoItem.produto_id)
             .outerjoin(MateriaPrima,
                        MateriaPrima.id == PedidoItem.materia_prima_id)
             .filter(PedidoLoja.data_entrega >= janela_busca_ini,
                     PedidoLoja.data_entrega <= horizonte_fim,
                     db.or_(Receita.nome.ilike(like),
                            Produto.nome.ilike(like),
                            MateriaPrima.nome.ilike(like)))
             .order_by(PedidoLoja.data_entrega.desc(), PedidoLoja.loja_id)
             .limit(300).all())
        # Agrega por loja pra um resumo no topo (responde direto a pergunta).
        resumo_por_loja = {}
        for r in q:
            if r.receita_id:
                fk, nome = 'REC', r.rec_nome
            elif r.produto_id:
                fk, nome = 'PROD', r.prod_nome
            elif r.materia_prima_id:
                fk, nome = 'MP', r.mp_nome
            else:
                fk, nome = 'NENHUMA', '?'
            no_horizonte = r.data_entrega and r.data_entrega >= hoje_d
            busca_linhas.append({
                'pedido_id': r.id, 'loja_nome': nomes_loja.get(r.loja_id, '?'),
                'status': r.status, 'data_entrega': r.data_entrega,
                'item_nome': nome, 'fk_tipo': fk, 'quantidade': r.quantidade,
                'no_horizonte': no_horizonte,
                'entra_balanco': (fk == 'REC' and no_horizonte
                                  and r.status in STATUS_PEDIDO_NAO_BAIXADOS),
            })
            chave = nomes_loja.get(r.loja_id, '?')
            ag = resumo_por_loja.setdefault(
                chave, {'loja_nome': chave, 'qtd_total': 0, 'n_linhas': 0,
                        'qtd_horizonte': 0})
            ag['qtd_total'] += int(r.quantidade or 0)
            ag['n_linhas'] += 1
            if no_horizonte:
                ag['qtd_horizonte'] += int(r.quantidade or 0)
        busca_resumo = sorted(resumo_por_loja.values(),
                              key=lambda x: -x['qtd_total'])

    return render_template(
        'producao/painel_debug.html',
        hoje=hoje_d, horizonte=horizonte, janela=janela,
        horizonte_fim=horizonte_fim, hist_ini=hist_ini, hist_fim=hist_fim,
        comprometido_pedidos=comprometido_pedidos,
        excluidos_status=excluidos_status,
        atrasados=atrasados,
        contagem_por_loja=contagem_por_loja,
        nomes_loja=nomes_loja,
        status_nao_baixados=STATUS_PEDIDO_NAO_BAIXADOS,
        itens_enriquecidos=itens_enriquecidos,
        fantasmas=fantasmas,
        variantes_receita=variantes_receita,
        busca=busca,
        busca_linhas=busca_linhas,
        busca_resumo=busca_resumo,
    )


@producao_bp.route('/painel/criar-plano-do-deficit', methods=['POST'])
@login_required
@admin_required
def criar_plano_do_deficit():
    """Cria um PlanejamentoProducao do dia ja preenchido com as receitas que
    o balanco aponta como deficit (coluna Produzir > 0). Multiplicador por
    receita = ceil(produzir / rendimento_qtd). O admin abre o plano criado,
    revisa, ajusta a olho e clica em 'Baixar estoque' como ja faz hoje."""
    from math import ceil

    from app.services.previsao_producao import balanco_industria

    try:
        horizonte = int(request.form.get('horizonte', 7))
    except ValueError:
        horizonte = 7
    horizonte = max(1, min(horizonte, 14))

    try:
        janela = int(request.form.get('janela', 6))
    except ValueError:
        janela = 6
    janela = max(1, min(janela, 26))

    inicio = _inicio_offset()
    balanco = balanco_industria(horizonte_dias=horizonte,
                                janela_semanas=janela, usar_cache=False,
                                inicio_offset_dias=inicio)
    deficits = [it for it in balanco['itens'] if it['produzir'] > 0]
    if not deficits:
        flash('Sem deficit no horizonte — nada a planejar.', 'info')
        return redirect(url_for('producao.painel',
                                horizonte=horizonte, janela=janela,
                                inicio=inicio))

    hoje_d = hoje_brt()
    plano = PlanejamentoProducao(
        data=hoje_d,
        nome=f'Producao {hoje_d.strftime("%d/%m")} (deficit {horizonte}d)',
        criado_por=current_user.id,
    )
    db.session.add(plano)
    db.session.flush()

    ignorados = []
    for it in deficits:
        rec = Receita.query.get(it['receita_id'])
        if not rec or not rec.rendimento_qtd or rec.rendimento_qtd <= 0:
            # Receita sem rendimento definido — nao da pra calcular mult.
            # Reporta na flash pro admin completar a ficha tecnica.
            ignorados.append(it['nome'])
            continue
        mult = max(1, ceil(it['produzir'] / float(rec.rendimento_qtd)))
        db.session.add(PlanejamentoItem(
            planejamento_id=plano.id, receita_id=rec.id, multiplicador=mult))

    db.session.commit()
    msg = f'Plano criado com {len(deficits) - len(ignorados)} receita(s).'
    if ignorados:
        msg += (' Sem rendimento na ficha (ignorados): '
                + ', '.join(ignorados[:5])
                + ('...' if len(ignorados) > 5 else ''))
    flash(msg, 'success' if not ignorados else 'warning')
    return redirect(url_for('producao.detalhe', id=plano.id))


@producao_bp.route('/previsao-acuracia')
@login_required
@admin_required
def previsao_acuracia():
    """Painel de acuracia do forecast: vies (super/subprevisao) e WAPE por
    receita, dos snapshots ja casados com o realizado. **Admin**."""
    from app.services.previsao_acuracia import (
        MOTOR_LABEL,
        MOTORES_VIVOS,
        comparativo_motores_por_loja,
        resumo_acuracia,
    )
    try:
        dias = int(request.args.get('dias', 30))
    except ValueError:
        dias = 30
    dias = max(7, min(dias, 180))
    motor = request.args.get('motor') or None
    if motor not in MOTOR_LABEL:
        motor = None
    resumo = resumo_acuracia(dias=dias, motor=motor)
    return render_template('producao/previsao_acuracia.html',
                           resumo=resumo, dias=dias, motor=motor,
                           motor_labels=MOTOR_LABEL,
                           motores_vivos=MOTORES_VIVOS,
                           comparativo=comparativo_motores_por_loja(dias=dias))


@producao_bp.route('/previsao-acuracia/rodar', methods=['POST'])
@login_required
@admin_required
def previsao_acuracia_rodar():
    """Roda o snapshot + casamento manualmente (sem esperar o cron). Util pra
    semear os primeiros dados ou conferir agora."""
    from app.services import previsao_acuracia as svc
    novos = svc.registrar_snapshot()
    casados = svc.casar_realizados()
    flash(f'Acuracia atualizada: {novos} previsao(es) congelada(s), '
          f'{casados} casada(s) com o realizado.', 'success')
    return redirect(url_for('producao.previsao_acuracia'))


@producao_bp.route('/pedidos-semana/ia', methods=['POST'])
@login_required
@admin_required
def pedidos_semana_ia():
    """Proposta da IA (Opus 4.8) para o pedido de UMA loja — preenche a
    grade das telas /pedidos-semana/media (modo='media', default) e
    /pedidos-semana/estoque (modo='venda') via JS. NADA é criado aqui:
    o pedido continua nascendo pelos botões Gerar de sempre."""
    from app.services import planejamento_ia

    p = request.get_json(silent=True) or request.form
    try:
        loja_id = int(p.get('loja_id'))
    except (TypeError, ValueError):
        return jsonify(ok=False, erro='loja_id obrigatório'), 400

    def _int(key, default, lo, hi):
        try:
            return max(lo, min(int(p.get(key, default)), hi))
        except (TypeError, ValueError):
            return default
    modo = 'venda' if p.get('modo') == 'venda' else 'media'
    out = planejamento_ia.sugerir_pedido_loja_ia(
        loja_id,
        horizonte_dias=_int('horizonte', 7, 1, 14),
        janela_semanas=_int('janela', 6, 1, 26),
        inicio_offset_dias=_int('inicio', 1, 0, 14),
        modo=modo,
        seguranca_pct=_int('seguranca', 0, 0, 100))
    if out.get('erro'):
        return jsonify(ok=False, erro=out['erro']), 502
    return jsonify(ok=True, **out)
