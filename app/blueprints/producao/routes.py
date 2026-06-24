from datetime import datetime

from flask import flash, redirect, render_template, request, url_for
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
from app.services.producao import consolidar_lista_compras
from app.utils import hoje as hoje_brt


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
    itens = [{'receita_id': i.receita_id, 'multiplicador': i.multiplicador} for i in plano.itens]
    lista_compras = consolidar_lista_compras(itens)
    lista_ordenada = sorted(lista_compras.items(), key=lambda x: x[0])
    custo_total = sum(v['custo_estimado'] for v in lista_compras.values())
    return render_template('producao/detalhe.html',
                           plano=plano, lista_compras=lista_ordenada, custo_total=custo_total)


@producao_bp.route('/<int:id>/lista-compras')
@login_required
@producao_required
def lista_compras(id):
    plano = PlanejamentoProducao.query.get_or_404(id)
    itens = [{'receita_id': i.receita_id, 'multiplicador': i.multiplicador} for i in plano.itens]
    lista = consolidar_lista_compras(itens)
    lista_ordenada = sorted(lista.items(), key=lambda x: x[0])
    custo_total = sum(v['custo_estimado'] for v in lista.values())
    return render_template('producao/lista_compras.html',
                           plano=plano, lista_compras=lista_ordenada, custo_total=custo_total)


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

    # Zona 1 — alertas
    pedidos_atrasados = (PedidoLoja.query
                         .filter(PedidoLoja.data_entrega < hoje_d,
                                 ~PedidoLoja.status.in_(STATUS_PEDIDO_FINALIZADOS))
                         .order_by(PedidoLoja.data_entrega).all())
    cestas_orfaos = contar_produto_itens_orfaos()

    # Zona 2 — balanco da industria (estoque x comprometido x previsto)
    balanco = balanco_industria(horizonte_dias=horizonte, janela_semanas=janela)

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
                           pedidos_atrasados=pedidos_atrasados,
                           cestas_orfaos=cestas_orfaos,
                           balanco=balanco,
                           saindo_hoje=saindo_hoje)


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
    from app.models import Loja, PedidoItem, PedidoLoja

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

    balanco = balanco_industria(horizonte_dias=horizonte,
                                janela_semanas=janela, usar_cache=False)
    deficits = [it for it in balanco['itens'] if it['produzir'] > 0]
    if not deficits:
        flash('Sem deficit no horizonte — nada a planejar.', 'info')
        return redirect(url_for('producao.painel',
                                horizonte=horizonte, janela=janela))

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
