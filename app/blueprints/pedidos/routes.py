import csv
import io
from collections import defaultdict
from datetime import date, datetime, timedelta

from flask import render_template, redirect, url_for, flash, request, abort, send_file, Response
from flask_login import login_required, current_user

from app.blueprints.pedidos import pedidos_bp
from app.decorators import admin_required
from app.extensions import db
from app.models import (
    Loja, Receita, Produto, PedidoLoja, PedidoItem,
    EstoqueProducao, MovEstoqueProducao,
    EstoqueLoja, MovEstoqueLoja,
    PrecoLojaReceita, FotoRecebimento,
)


def _preco_para_loja(receita_id, loja_id):
    """Preco customizado da loja para a receita, ou preco_loja padrao."""
    if receita_id and loja_id:
        custom = PrecoLojaReceita.query.filter_by(
            loja_id=loja_id, receita_id=receita_id
        ).first()
        if custom:
            return custom.preco
    rec = Receita.query.get(receita_id) if receita_id else None
    return (rec.preco_loja if rec and rec.preco_loja else 0) or 0


def _loja_do_usuario():
    if current_user.is_admin():
        return None
    return current_user.loja_id


@pedidos_bp.route('/')
@login_required
def lista():
    loja_id = _loja_do_usuario()
    query = PedidoLoja.query.order_by(PedidoLoja.criado_em.desc())
    if loja_id:
        query = query.filter_by(loja_id=loja_id)
    else:
        filtro = request.args.get('loja')
        if filtro:
            query = query.filter_by(loja_id=int(filtro))
    pedidos = query.limit(100).all()
    lojas = Loja.query.filter_by(ativa=True).order_by(Loja.nome).all()
    return render_template('pedidos/lista.html', pedidos=pedidos, lojas=lojas,
                           filtro_loja=request.args.get('loja', ''))


@pedidos_bp.route('/novo', methods=['GET', 'POST'])
@login_required
def novo():
    loja_id = _loja_do_usuario()
    if not current_user.is_admin() and not loja_id:
        flash('Vincule sua conta a uma loja para criar pedidos.', 'warning')
        return redirect(url_for('pedidos.lista'))

    amanha = date.today() + timedelta(days=1)

    if request.method == 'POST':
        sel_loja = int(request.form.get('loja_id', 0)) if current_user.is_admin() else loja_id
        data_str = request.form.get('data_entrega', '')
        obs = request.form.get('observacao', '').strip()

        try:
            data_entrega = datetime.strptime(data_str, '%Y-%m-%d').date()
        except ValueError:
            data_entrega = amanha

        if data_entrega < amanha:
            flash('A data de entrega deve ser a partir de amanha.', 'warning')
            lojas = Loja.query.filter_by(ativa=True).order_by(Loja.nome).all()
            receitas = Receita.query.order_by(Receita.categoria, Receita.nome).all()
            return render_template('pedidos/novo.html', lojas=lojas,
                                   receitas=receitas, amanha=amanha, loja_id=loja_id)

        pedido = PedidoLoja(
            loja_id=sel_loja,
            data_entrega=data_entrega,
            observacao=obs or None,
            criado_por=current_user.id,
        )
        db.session.add(pedido)
        db.session.flush()

        ids = request.form.getlist('item_id[]')
        qtds = request.form.getlist('item_qtd[]')
        notas = request.form.getlist('item_obs[]')

        for i in range(len(ids)):
            if not ids[i] or not qtds[i]:
                continue
            item = PedidoItem(
                pedido_id=pedido.id,
                receita_id=int(ids[i]),
                quantidade=int(qtds[i]),
                observacao=notas[i].strip() if i < len(notas) else None,
            )
            db.session.add(item)

        db.session.commit()
        flash('Pedido criado!', 'success')
        return redirect(url_for('pedidos.detalhe', id=pedido.id))

    lojas = Loja.query.filter_by(ativa=True).order_by(Loja.nome).all()
    receitas = Receita.query.order_by(Receita.categoria, Receita.nome).all()
    return render_template('pedidos/novo.html', lojas=lojas,
                           receitas=receitas, amanha=amanha, loja_id=loja_id)


@pedidos_bp.route('/<int:id>')
@login_required
def detalhe(id):
    pedido = PedidoLoja.query.get_or_404(id)
    loja_id = _loja_do_usuario()
    if loja_id and pedido.loja_id != loja_id:
        abort(403)
    return render_template('pedidos/detalhe.html', pedido=pedido)


@pedidos_bp.route('/<int:id>/confirmar', methods=['POST'])
@login_required
@admin_required
def confirmar(id):
    pedido = PedidoLoja.query.get_or_404(id)
    pedido.status = 'confirmado'
    db.session.commit()
    flash('Pedido confirmado.', 'success')
    return redirect(url_for('pedidos.detalhe', id=id))


@pedidos_bp.route('/<int:id>/separar', methods=['POST'])
@login_required
@admin_required
def separar(id):
    pedido = PedidoLoja.query.get_or_404(id)
    if pedido.status not in ('pendente', 'confirmado'):
        flash('Pedido deve estar pendente ou confirmado para ser separado.', 'warning')
        return redirect(url_for('pedidos.detalhe', id=id))
    pedido.status = 'separado'
    db.session.commit()
    flash('Pedido marcado como separado. Estoque ainda nao foi baixado.', 'success')
    return redirect(url_for('pedidos.detalhe', id=id))


@pedidos_bp.route('/<int:id>/enviar', methods=['POST'])
@login_required
@admin_required
def enviar(id):
    pedido = PedidoLoja.query.get_or_404(id)
    if pedido.status != 'separado':
        flash('Pedido precisa estar separado para sair pra entrega.', 'warning')
        return redirect(url_for('pedidos.detalhe', id=id))

    for item in pedido.itens:
        ep = EstoqueProducao.query.filter_by(
            receita_id=item.receita_id, produto_id=item.produto_id
        ).first()
        if ep:
            ep.quantidade = max(0, ep.quantidade - item.quantidade)
            db.session.add(MovEstoqueProducao(
                estoque_producao_id=ep.id, tipo='saida_pedido',
                quantidade=item.quantidade,
                referencia=f'Pedido #{pedido.id} → {pedido.loja.nome}',
                usuario_id=current_user.id,
            ))

    pedido.status = 'em_transporte'
    db.session.commit()
    flash('Pedido em transporte. Estoque da industria baixado.', 'success')
    return redirect(url_for('pedidos.detalhe', id=id))


@pedidos_bp.route('/<int:id>/receber', methods=['POST'])
@login_required
def receber(id):
    pedido = PedidoLoja.query.get_or_404(id)
    loja_id = _loja_do_usuario()
    if loja_id and pedido.loja_id != loja_id:
        abort(403)
    if pedido.status != 'em_transporte':
        flash('Pedido precisa estar em transporte para ser recebido.', 'warning')
        return redirect(url_for('pedidos.detalhe', id=id))

    recebidos = {}
    for key, val in request.form.items():
        if key.startswith('recebido_') and val.strip():
            try:
                recebidos[int(key[len('recebido_'):])] = max(0, int(val))
            except ValueError:
                continue

    divergencias = []
    for item in pedido.itens:
        qtd_rec = recebidos.get(item.id, item.quantidade)
        item.quantidade_recebida = qtd_rec
        if qtd_rec != item.quantidade:
            divergencias.append(f'{item.nome_item}: pedido {item.quantidade}, recebido {qtd_rec}')

        if qtd_rec <= 0:
            continue

        el = EstoqueLoja.query.filter_by(
            loja_id=pedido.loja_id, receita_id=item.receita_id, produto_id=item.produto_id
        ).first()
        if not el:
            el = EstoqueLoja(loja_id=pedido.loja_id, receita_id=item.receita_id,
                             produto_id=item.produto_id)
            db.session.add(el)
            db.session.flush()
        el.quantidade += qtd_rec
        ref_div = ' (divergente)' if qtd_rec != item.quantidade else ''
        db.session.add(MovEstoqueLoja(
            estoque_loja_id=el.id, tipo='entrada_pedido',
            quantidade=qtd_rec,
            referencia=f'Pedido #{pedido.id}{ref_div}',
            usuario_id=current_user.id,
        ))

    pedido.status = 'entregue'
    if divergencias:
        nota = 'Divergencias no recebimento: ' + '; '.join(divergencias)
        pedido.observacao = (pedido.observacao + ' | ' if pedido.observacao else '') + nota

    for f in request.files.getlist('fotos'):
        if not f or not f.filename:
            continue
        content = f.read()
        if not content:
            continue
        db.session.add(FotoRecebimento(
            pedido_id=pedido.id,
            imagem=content,
            mimetype=f.mimetype or 'image/jpeg',
            enviada_por=current_user.id,
        ))

    if divergencias:
        flash('Pedido recebido com divergencias. Detalhes salvos na observacao.', 'warning')
    else:
        flash('Pedido recebido integralmente. Estoque da loja atualizado.', 'success')
    db.session.commit()
    return redirect(url_for('pedidos.detalhe', id=id))


@pedidos_bp.route('/foto/<int:foto_id>')
@login_required
def foto(foto_id):
    f = FotoRecebimento.query.get_or_404(foto_id)
    loja_id = _loja_do_usuario()
    if loja_id and f.pedido.loja_id != loja_id:
        abort(403)
    return send_file(io.BytesIO(f.imagem), mimetype=f.mimetype or 'image/jpeg')


@pedidos_bp.route('/lojas/<int:loja_id>/precos', methods=['GET', 'POST'])
@login_required
@admin_required
def precos_loja(loja_id):
    loja = Loja.query.get_or_404(loja_id)
    receitas = Receita.query.order_by(Receita.categoria, Receita.nome).all()

    if request.method == 'POST':
        for r in receitas:
            val = (request.form.get(f'preco_{r.id}', '') or '').strip().replace(',', '.')
            existente = PrecoLojaReceita.query.filter_by(
                loja_id=loja_id, receita_id=r.id
            ).first()
            if not val:
                if existente:
                    db.session.delete(existente)
                continue
            try:
                preco = float(val)
            except ValueError:
                continue
            if preco <= 0:
                if existente:
                    db.session.delete(existente)
                continue
            if existente:
                existente.preco = preco
            else:
                db.session.add(PrecoLojaReceita(
                    loja_id=loja_id, receita_id=r.id, preco=preco
                ))
        db.session.commit()
        flash(f'Precos da loja {loja.nome} atualizados.', 'success')
        return redirect(url_for('pedidos.precos_loja', loja_id=loja_id))

    precos = {p.receita_id: p.preco for p in PrecoLojaReceita.query.filter_by(loja_id=loja_id).all()}
    lojas = Loja.query.filter_by(ativa=True).order_by(Loja.nome).all()
    return render_template('pedidos/precos_loja.html', loja=loja, receitas=receitas,
                           precos=precos, lojas=lojas)


@pedidos_bp.route('/relatorio')
@login_required
@admin_required
def relatorio():
    hoje = date.today()
    loja_id = request.args.get('loja', type=int)
    de_str = request.args.get('de', '')
    ate_str = request.args.get('ate', '')
    formato = request.args.get('formato', 'html')

    try:
        de = datetime.strptime(de_str, '%Y-%m-%d').date()
    except ValueError:
        de = hoje.replace(day=1)
    try:
        ate = datetime.strptime(ate_str, '%Y-%m-%d').date()
    except ValueError:
        ate = hoje

    lojas = Loja.query.filter_by(ativa=True).order_by(Loja.nome).all()
    pedidos = []
    totais = {'qtd_pedidos': 0, 'valor_total': 0.0, 'divergencias': 0}
    por_item = defaultdict(lambda: {'quantidade': 0, 'recebido': 0, 'valor': 0.0})

    if loja_id:
        query = PedidoLoja.query.filter(
            PedidoLoja.loja_id == loja_id,
            PedidoLoja.status == 'entregue',
            PedidoLoja.data_entrega >= de,
            PedidoLoja.data_entrega <= ate,
        ).order_by(PedidoLoja.data_entrega)
        pedidos_raw = query.all()

        for p in pedidos_raw:
            subtotal = 0.0
            linhas = []
            for it in p.itens:
                preco = _preco_para_loja(it.receita_id, loja_id)
                qtd_efetiva = it.quantidade_recebida if it.quantidade_recebida is not None else it.quantidade
                valor_linha = preco * qtd_efetiva
                subtotal += valor_linha
                linhas.append({
                    'nome': it.nome_item,
                    'quantidade': it.quantidade,
                    'recebido': qtd_efetiva,
                    'preco': preco,
                    'subtotal': valor_linha,
                    'divergente': it.quantidade_recebida is not None and it.quantidade_recebida != it.quantidade,
                })
                por_item[it.nome_item]['quantidade'] += it.quantidade
                por_item[it.nome_item]['recebido'] += qtd_efetiva
                por_item[it.nome_item]['valor'] += valor_linha

            pedidos.append({'p': p, 'linhas': linhas, 'subtotal': subtotal})
            totais['qtd_pedidos'] += 1
            totais['valor_total'] += subtotal
            if p.tem_divergencia:
                totais['divergencias'] += 1

    if formato == 'csv' and loja_id:
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=';')
        w.writerow(['Data', 'Pedido', 'Item', 'Pedido (qtd)', 'Recebido (qtd)', 'Preco Unit.', 'Subtotal', 'Divergente', 'Fotos'])
        for p_info in pedidos:
            p = p_info['p']
            n_fotos = len(p.fotos)
            for l in p_info['linhas']:
                w.writerow([
                    p.data_entrega.strftime('%d/%m/%Y') if p.data_entrega else '',
                    f"#{p.id}", l['nome'], l['quantidade'], l['recebido'],
                    f"{l['preco']:.2f}".replace('.', ','),
                    f"{l['subtotal']:.2f}".replace('.', ','),
                    'SIM' if l['divergente'] else '',
                    n_fotos if n_fotos else '',
                ])
        w.writerow([])
        w.writerow(['TOTAL', '', '', '', '', '', f"{totais['valor_total']:.2f}".replace('.', ','), '', ''])
        loja_nome = next((l.nome for l in lojas if l.id == loja_id), 'loja')
        return Response(
            buf.getvalue(),
            mimetype='text/csv; charset=utf-8',
            headers={
                'Content-Disposition': f'attachment; filename="pedidos_{loja_nome}_{de}_a_{ate}.csv"'
            },
        )

    return render_template('pedidos/relatorio.html',
                           lojas=lojas, loja_id=loja_id,
                           de=de.isoformat(), ate=ate.isoformat(),
                           pedidos=pedidos, totais=totais,
                           por_item=sorted(por_item.items(), key=lambda x: x[0]))


@pedidos_bp.route('/<int:id>/cancelar', methods=['POST'])
@login_required
def cancelar(id):
    pedido = PedidoLoja.query.get_or_404(id)
    loja_id = _loja_do_usuario()
    if loja_id and pedido.loja_id != loja_id:
        abort(403)
    if pedido.status not in ('pendente', 'confirmado', 'separado'):
        flash('Só é possível cancelar pedidos pendentes, confirmados ou separados (antes do envio).', 'warning')
        return redirect(url_for('pedidos.detalhe', id=id))
    pedido.status = 'cancelado'
    db.session.commit()
    flash('Pedido cancelado.', 'success')
    return redirect(url_for('pedidos.lista'))


@pedidos_bp.route('/<int:id>/excluir', methods=['POST'])
@login_required
@admin_required
def excluir(id):
    pedido = PedidoLoja.query.get_or_404(id)
    db.session.delete(pedido)
    db.session.commit()
    flash('Pedido excluído.', 'success')
    return redirect(url_for('pedidos.lista'))


# ── Painel de Separação ──

@pedidos_bp.route('/separacao')
@login_required
@admin_required
def separacao():
    pedidos = PedidoLoja.query.filter(
        PedidoLoja.status.in_(['pendente', 'confirmado'])
    ).order_by(PedidoLoja.data_entrega).all()

    por_data = defaultdict(lambda: defaultdict(lambda: {'total': 0, 'lojas': defaultdict(int)}))
    for p in pedidos:
        chave_data = p.data_entrega or p.data_pedido
        for item in p.itens:
            nome = item.nome_item
            por_data[chave_data][nome]['total'] += item.quantidade
            por_data[chave_data][nome]['lojas'][p.loja.nome] += item.quantidade

    congelados = {ep.nome_item: ep.quantidade for ep in EstoqueProducao.query.all()}

    return render_template('pedidos/separacao.html',
                           por_data=dict(por_data), congelados=congelados)


# ── Estoque de Congelados ──

@pedidos_bp.route('/congelados')
@login_required
@admin_required
def congelados():
    itens = EstoqueProducao.query.all()
    receitas = Receita.query.order_by(Receita.categoria, Receita.nome).all()
    produtos = Produto.query.filter_by(ativo=True).order_by(Produto.nome).all()
    return render_template('pedidos/congelados.html', itens=itens,
                           receitas=receitas, produtos=produtos)


@pedidos_bp.route('/congelados/entrada', methods=['POST'])
@login_required
@admin_required
def congelados_entrada():
    tipo = request.form.get('tipo', 'receita')
    item_id = int(request.form['item_id'])
    qtd = int(request.form['quantidade'])

    ep = EstoqueProducao.query.filter_by(
        receita_id=item_id if tipo == 'receita' else None,
        produto_id=item_id if tipo == 'produto' else None,
    ).first()
    if not ep:
        ep = EstoqueProducao(
            receita_id=item_id if tipo == 'receita' else None,
            produto_id=item_id if tipo == 'produto' else None,
        )
        db.session.add(ep)
        db.session.flush()

    ep.quantidade += qtd
    db.session.add(MovEstoqueProducao(
        estoque_producao_id=ep.id, tipo='producao',
        quantidade=qtd, referencia='Entrada de produção',
        usuario_id=current_user.id,
    ))
    db.session.commit()
    flash(f'Entrada de {qtd} unidades registrada.', 'success')
    return redirect(url_for('pedidos.congelados'))


@pedidos_bp.route('/congelados/ajuste', methods=['POST'])
@login_required
@admin_required
def congelados_ajuste():
    ep_id = int(request.form['estoque_id'])
    qtd = int(request.form['quantidade'])
    tipo = request.form.get('tipo_ajuste', 'ajuste')

    ep = EstoqueProducao.query.get_or_404(ep_id)
    ep.quantidade = max(0, ep.quantidade - qtd)
    db.session.add(MovEstoqueProducao(
        estoque_producao_id=ep.id, tipo=tipo,
        quantidade=qtd, referencia=request.form.get('referencia', '').strip() or None,
        usuario_id=current_user.id,
    ))
    db.session.commit()
    flash(f'Ajuste de {qtd} unidades registrado.', 'success')
    return redirect(url_for('pedidos.congelados'))


# ── Estoque de Loja ──

@pedidos_bp.route('/estoque-loja')
@login_required
def estoque_loja():
    loja_id = _loja_do_usuario()
    if current_user.is_admin():
        sel = request.args.get('loja')
        loja_id = int(sel) if sel else None

    loja = Loja.query.get(loja_id) if loja_id else None
    itens = EstoqueLoja.query.filter_by(loja_id=loja_id).all() if loja_id else []
    lojas = Loja.query.filter_by(ativa=True).order_by(Loja.nome).all()
    receitas = Receita.query.order_by(Receita.categoria, Receita.nome).all() \
        if current_user.is_admin() else []
    return render_template('pedidos/estoque_loja.html', loja=loja, itens=itens,
                           lojas=lojas, sel_loja=loja_id, receitas=receitas)


@pedidos_bp.route('/estoque-loja/registrar', methods=['POST'])
@login_required
def estoque_loja_registrar():
    loja_id = _loja_do_usuario()
    if current_user.is_admin():
        loja_id = int(request.form.get('loja_id', 0))

    if not loja_id:
        flash('Selecione uma loja.', 'warning')
        return redirect(url_for('pedidos.estoque_loja'))

    ids = request.form.getlist('estoque_id[]')
    qtds = request.form.getlist('qtd[]')
    tipos = request.form.getlist('tipo[]')

    for i, eid in enumerate(ids):
        if not qtds[i] or int(qtds[i]) <= 0:
            continue
        el = EstoqueLoja.query.get(int(eid))
        if not el or el.loja_id != loja_id:
            continue
        qtd = int(qtds[i])
        tipo = tipos[i] if i < len(tipos) else 'venda'
        el.quantidade = max(0, el.quantidade - qtd)
        db.session.add(MovEstoqueLoja(
            estoque_loja_id=el.id, tipo=tipo, quantidade=qtd,
            referencia=f'{tipo.capitalize()} registrada',
            usuario_id=current_user.id,
        ))

    db.session.commit()
    flash('Estoque atualizado.', 'success')
    return redirect(url_for('pedidos.estoque_loja', loja=loja_id))


@pedidos_bp.route('/estoque-loja/ajuste', methods=['POST'])
@login_required
@admin_required
def estoque_loja_ajuste():
    loja_id = int(request.form.get('loja_id', 0))
    receita_id = int(request.form.get('receita_id', 0))
    qtd = int(request.form.get('quantidade', 0))
    operacao = request.form.get('operacao', 'entrada')
    motivo = request.form.get('motivo', '').strip()

    if not loja_id or not receita_id or qtd <= 0 or not motivo:
        flash('Loja, item, quantidade (>0) e motivo sao obrigatorios.', 'warning')
        return redirect(url_for('pedidos.estoque_loja', loja=loja_id or None))

    el = EstoqueLoja.query.filter_by(loja_id=loja_id, receita_id=receita_id).first()
    if not el:
        if operacao != 'entrada':
            flash('Item inexistente no estoque — so e possivel fazer entrada.', 'warning')
            return redirect(url_for('pedidos.estoque_loja', loja=loja_id))
        el = EstoqueLoja(loja_id=loja_id, receita_id=receita_id)
        db.session.add(el)
        db.session.flush()

    if operacao == 'entrada':
        el.quantidade += qtd
        tipo_mov = 'entrada_manual'
        sinal = '+'
    else:
        el.quantidade = max(0, el.quantidade - qtd)
        tipo_mov = 'ajuste_negativo'
        sinal = '-'

    db.session.add(MovEstoqueLoja(
        estoque_loja_id=el.id, tipo=tipo_mov, quantidade=qtd,
        referencia=motivo, usuario_id=current_user.id,
    ))
    db.session.commit()
    flash(f'Ajuste de estoque registrado ({sinal}{qtd}).', 'success')
    return redirect(url_for('pedidos.estoque_loja', loja=loja_id))
