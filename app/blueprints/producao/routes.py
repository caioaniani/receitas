from datetime import date, datetime

from flask import render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user

from app.blueprints.producao import producao_bp
from app.decorators import producao_required
from app.extensions import db
from app.models import PlanejamentoProducao, PlanejamentoItem, Receita, MateriaPrima, MovimentacaoEstoque
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
