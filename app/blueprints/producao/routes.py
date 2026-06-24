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
