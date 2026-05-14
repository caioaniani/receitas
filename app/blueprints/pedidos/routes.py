import io
from collections import defaultdict
from datetime import date, datetime, timedelta

from flask import render_template, redirect, url_for, flash, request, abort, send_file, current_app
from flask_login import login_required, current_user
from sqlalchemy.orm import joinedload, selectinload

from app.blueprints.pedidos import pedidos_bp
from app.decorators import admin_required, gerente_required, producao_required
from app.extensions import db
from app.models import (
    Loja, Receita, Produto, MateriaPrima, MovimentacaoEstoque,
    PedidoLoja, PedidoItem,
    EstoqueProducao, MovEstoqueProducao,
    EstoqueLoja, MovEstoqueLoja,
    PrecoLojaReceita, FotoRecebimento,
    LojaProdutoMap,
)


def _parse_item_id(value):
    """Decodifica 'r_5'/'mp_5'/'5' em ('receita'|'mp', id). Legacy: int puro = receita."""
    if not value:
        return None, None
    if value.startswith('r_'):
        try:
            return 'receita', int(value[2:])
        except ValueError:
            return None, None
    if value.startswith('mp_'):
        try:
            return 'mp', int(value[3:])
        except ValueError:
            return None, None
    try:
        return 'receita', int(value)
    except ValueError:
        return None, None


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


# Lojas com a 'Industria' (fabrica de producao) excluida.
# Industria existe como Loja so pra fins de RH/escala (padeiros, auxiliares),
# mas nao recebe pedidos, nao tem PDV, nao tem estoque de venda. Use em
# qualquer dropdown operacional (pedidos, estoque de loja, precos).
def _lojas_operacionais():
    return (Loja.query
            .filter(Loja.ativa.is_(True), Loja.nome != 'Industria')
            .order_by(Loja.nome)
            .all())


def _loja_do_usuario():
    if current_user.is_admin():
        return None
    return current_user.loja_id


@pedidos_bp.route('/')
@login_required
def lista():
    loja_id = _loja_do_usuario()
    query = PedidoLoja.query.options(
        joinedload(PedidoLoja.loja),
        selectinload(PedidoLoja.itens),
    ).order_by(PedidoLoja.criado_em.desc())
    if loja_id:
        # nao-admin: sempre filtra pela propria loja, ignora ?loja= do form
        query = query.filter_by(loja_id=loja_id)
    else:
        filtro = request.args.get('loja')
        if filtro:
            try:
                query = query.filter_by(loja_id=int(filtro))
            except (TypeError, ValueError):
                pass
    pedidos = query.limit(100).all()
    lojas = _lojas_operacionais()
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
        try:
            sel_loja = int(request.form.get('loja_id', 0)) if current_user.is_admin() else loja_id
        except (TypeError, ValueError):
            sel_loja = 0
        # Validacao multi-loja: nao-admin so cria pra propria loja
        if not current_user.is_admin() and sel_loja != loja_id:
            abort(403)
        # Loja precisa existir e estar ativa
        if not sel_loja or not Loja.query.filter_by(id=sel_loja, ativa=True).first():
            flash('Selecione uma loja valida.', 'warning')
            lojas = _lojas_operacionais()
            receitas = Receita.query.order_by(Receita.categoria, Receita.nome).all()
            materias = MateriaPrima.query.order_by(MateriaPrima.nome).all()
            return render_template('pedidos/novo.html', lojas=lojas,
                                   receitas=receitas, materias=materias,
                                   amanha=amanha, loja_id=loja_id)

        data_str = request.form.get('data_entrega', '')
        obs = request.form.get('observacao', '').strip()

        try:
            data_entrega = datetime.strptime(data_str, '%Y-%m-%d').date()
        except ValueError:
            data_entrega = amanha

        if data_entrega < amanha:
            flash('A data de entrega deve ser a partir de amanha.', 'warning')
            lojas = _lojas_operacionais()
            receitas = Receita.query.order_by(Receita.categoria, Receita.nome).all()
            materias = MateriaPrima.query.order_by(MateriaPrima.nome).all()
            return render_template('pedidos/novo.html', lojas=lojas,
                                   receitas=receitas, materias=materias,
                                   amanha=amanha, loja_id=loja_id)

        try:
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
                tipo, item_id = _parse_item_id(ids[i])
                if not tipo:
                    continue
                try:
                    qtd = int(qtds[i])
                except (TypeError, ValueError):
                    continue
                if qtd <= 0:
                    continue
                item = PedidoItem(
                    pedido_id=pedido.id,
                    receita_id=item_id if tipo == 'receita' else None,
                    materia_prima_id=item_id if tipo == 'mp' else None,
                    quantidade=qtd,
                    observacao=notas[i].strip() if i < len(notas) else None,
                )
                db.session.add(item)

            db.session.commit()
        except Exception as exc:  # noqa: BLE001
            db.session.rollback()
            current_app.logger.exception('Falha ao criar pedido')
            flash(f'Erro ao criar pedido: {exc}', 'danger')
            return redirect(url_for('pedidos.novo'))
        flash('Pedido criado!', 'success')
        return redirect(url_for('pedidos.detalhe', id=pedido.id))

    lojas = _lojas_operacionais()
    receitas = Receita.query.order_by(Receita.categoria, Receita.nome).all()
    materias = MateriaPrima.query.order_by(MateriaPrima.nome).all()
    return render_template('pedidos/novo.html', lojas=lojas,
                           receitas=receitas, materias=materias,
                           amanha=amanha, loja_id=loja_id)


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
@gerente_required
def confirmar(id):
    pedido = PedidoLoja.query.get_or_404(id)
    pedido.status = 'confirmado'
    db.session.commit()
    flash('Pedido confirmado.', 'success')
    return redirect(url_for('pedidos.detalhe', id=id))


@pedidos_bp.route('/<int:id>/separar', methods=['POST'])
@login_required
@gerente_required
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

    try:
        for item in pedido.itens:
            if item.materia_prima_id:
                mp = MateriaPrima.query.get(item.materia_prima_id)
                if mp:
                    mp.estoque_atual = max(0, (mp.estoque_atual or 0) - item.quantidade)
                    db.session.add(MovimentacaoEstoque(
                        materia_prima_id=mp.id, tipo='saida',
                        quantidade=item.quantidade,
                        referencia=f'Pedido #{pedido.id} → {pedido.loja.nome}',
                        usuario_id=current_user.id,
                    ))
                continue

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
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        current_app.logger.exception('Falha ao enviar pedido %s', id)
        flash(f'Erro ao processar saída do pedido: {exc}. Nada foi alterado.', 'danger')
        return redirect(url_for('pedidos.detalhe', id=id))
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

    try:
        divergencias = []
        for item in pedido.itens:
            qtd_rec = recebidos.get(item.id, item.quantidade)
            item.quantidade_recebida = qtd_rec
            if qtd_rec != item.quantidade:
                divergencias.append(f'{item.nome_item}: pedido {item.quantidade}, recebido {qtd_rec}')

            if qtd_rec <= 0:
                continue

            el = EstoqueLoja.query.filter_by(
                loja_id=pedido.loja_id,
                receita_id=item.receita_id,
                produto_id=item.produto_id,
                materia_prima_id=item.materia_prima_id,
            ).first()
            if not el:
                el = EstoqueLoja(loja_id=pedido.loja_id,
                                 receita_id=item.receita_id,
                                 produto_id=item.produto_id,
                                 materia_prima_id=item.materia_prima_id)
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

        db.session.commit()
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        current_app.logger.exception('Falha ao receber pedido %s', id)
        flash(f'Erro ao processar recebimento: {exc}. Nada foi alterado.', 'danger')
        return redirect(url_for('pedidos.detalhe', id=id))

    if divergencias:
        flash('Pedido recebido com divergencias. Detalhes salvos na observacao.', 'warning')
    else:
        flash('Pedido recebido integralmente. Estoque da loja atualizado.', 'success')
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
    lojas = _lojas_operacionais()
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
    incluir_fotos = request.args.get('fotos') == '1'

    try:
        de = datetime.strptime(de_str, '%Y-%m-%d').date()
    except ValueError:
        de = hoje.replace(day=1)
    try:
        ate = datetime.strptime(ate_str, '%Y-%m-%d').date()
    except ValueError:
        ate = hoje

    lojas = _lojas_operacionais()
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

    loja_nome = next((l.nome for l in lojas if l.id == loja_id), 'loja') if loja_id else 'loja'

    if formato == 'xlsx' and loja_id:
        from app.services.relatorio import gerar_xlsx_pedidos
        buf = gerar_xlsx_pedidos(loja_nome, de, ate, pedidos, totais, por_item)
        return send_file(
            buf, as_attachment=True,
            download_name=f'pedidos_{loja_nome}_{de}_a_{ate}.xlsx',
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )

    if formato == 'pdf' and loja_id:
        from app.services.relatorio import gerar_pdf_pedidos
        buf = gerar_pdf_pedidos(loja_nome, de, ate, pedidos, totais, por_item,
                                incluir_fotos=incluir_fotos)
        sufixo = '_com_fotos' if incluir_fotos else ''
        return send_file(
            buf, as_attachment=True,
            download_name=f'pedidos_{loja_nome}_{de}_a_{ate}{sufixo}.pdf',
            mimetype='application/pdf',
        )

    return render_template('pedidos/relatorio.html',
                           lojas=lojas, loja_id=loja_id,
                           de=de.isoformat(), ate=ate.isoformat(),
                           pedidos=pedidos, totais=totais,
                           incluir_fotos=incluir_fotos,
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
    ids_por_data = defaultdict(list)
    for p in pedidos:
        chave_data = p.data_entrega or p.data_pedido
        ids_por_data[chave_data].append(p.id)
        for item in p.itens:
            nome = item.nome_item
            por_data[chave_data][nome]['total'] += item.quantidade
            por_data[chave_data][nome]['lojas'][p.loja.nome] += item.quantidade

    congelados = {ep.nome_item: ep.quantidade for ep in EstoqueProducao.query.all()}

    return render_template('pedidos/separacao.html',
                           por_data=dict(por_data),
                           ids_por_data=dict(ids_por_data),
                           congelados=congelados)


# ── Estoque de Congelados ──

@pedidos_bp.route('/congelados/dashboard')
@login_required
@admin_required
def congelados_dashboard():
    """Visao consolidada: itens em EstoqueProducao cruzados com os mesmos
    itens em cada loja (EstoqueLoja). Mostra qtd por local + total."""
    lojas = _lojas_operacionais()
    eps = EstoqueProducao.query.all()

    # Index dos estoques de loja por (tipo_chave, item_id) → {loja_id: qtd}
    receita_loja = {}   # receita_id -> {loja_id: qtd}
    produto_loja = {}   # produto_id -> {loja_id: qtd}
    for el in EstoqueLoja.query.filter(
            (EstoqueLoja.receita_id.isnot(None)) | (EstoqueLoja.produto_id.isnot(None))
        ).all():
        if el.receita_id:
            receita_loja.setdefault(el.receita_id, {})[el.loja_id] = el.quantidade or 0
        elif el.produto_id:
            produto_loja.setdefault(el.produto_id, {})[el.loja_id] = el.quantidade or 0

    linhas = []
    for ep in eps:
        if ep.receita_id:
            por_loja = receita_loja.get(ep.receita_id, {})
            tipo = 'receita'
        elif ep.produto_id:
            por_loja = produto_loja.get(ep.produto_id, {})
            tipo = 'produto'
        else:
            por_loja = {}
            tipo = 'pendente'
        total_lojas = sum(por_loja.values())
        linhas.append({
            'nome': ep.nome_item,
            'tipo': tipo,
            'industria': ep.quantidade or 0,
            'por_loja': por_loja,
            'total_lojas': total_lojas,
            'total_geral': (ep.quantidade or 0) + total_lojas,
            'pendente': ep.pendente,
        })

    linhas.sort(key=lambda r: (r['pendente'], r['nome'].lower()))

    # Totais por coluna (Industria + cada loja)
    tot_industria = sum(r['industria'] for r in linhas)
    tot_por_loja = {l.id: 0 for l in lojas}
    for r in linhas:
        for lid, qt in r['por_loja'].items():
            if lid in tot_por_loja:
                tot_por_loja[lid] += qt
    tot_geral = tot_industria + sum(tot_por_loja.values())

    return render_template('pedidos/congelados_dashboard.html',
                           linhas=linhas, lojas=lojas,
                           tot_industria=tot_industria,
                           tot_por_loja=tot_por_loja,
                           tot_geral=tot_geral)


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


# ── Balanco de Congelados (sobrescreve com contagem fisica) ──

@pedidos_bp.route('/congelados/balanco', methods=['GET', 'POST'])
@login_required
@admin_required
def congelados_balanco():
    """Tela de balanco: usuario cola lista 'nome: qtd' e sistema sobrescreve
    EstoqueProducao.quantidade pra cada item, com auditoria por delta."""
    from app.services import estoque_congelados as svc
    texto = request.form.get('texto', '') if request.method == 'POST' else ''
    referencia = request.form.get('referencia', '').strip()
    itens = []
    if request.method == 'POST' and texto.strip():
        parseados = svc.parsear_lista(texto)
        itens = svc.resolver_lista(parseados)
    return render_template('pedidos/balanco_congelados.html',
                           texto=texto, referencia=referencia, itens=itens)


@pedidos_bp.route('/congelados/vincular', methods=['POST'])
@login_required
@admin_required
def congelados_vincular():
    """Vincula uma EstoqueProducao pendente (sem receita/produto) a uma
    receita ou produto existente. Se ja existe outra EstoqueProducao com
    aquela receita/produto, soma a quantidade e apaga o orfao."""
    ep_id = int(request.form['estoque_id'])
    alvo_tipo = request.form.get('alvo_tipo')
    try:
        alvo_id = int(request.form.get('alvo_id', ''))
    except (TypeError, ValueError):
        alvo_id = 0
    if alvo_tipo not in ('receita', 'produto') or not alvo_id:
        flash('Selecione uma receita ou produto.', 'danger')
        return redirect(url_for('pedidos.congelados'))

    orfao = EstoqueProducao.query.get_or_404(ep_id)
    if not orfao.pendente:
        flash('Este item já está vinculado.', 'warning')
        return redirect(url_for('pedidos.congelados'))

    # Existe ja uma linha com aquela receita/produto? Se sim, mescla.
    existente = EstoqueProducao.query.filter_by(
        receita_id=alvo_id if alvo_tipo == 'receita' else None,
        produto_id=alvo_id if alvo_tipo == 'produto' else None,
    ).first()

    nome_orfao = orfao.nome_pendente or '?'
    qtd_orfao = orfao.quantidade or 0

    if existente and existente.id != orfao.id:
        anterior = existente.quantidade or 0
        existente.quantidade = anterior + qtd_orfao
        if qtd_orfao:
            db.session.add(MovEstoqueProducao(
                estoque_producao_id=existente.id,
                tipo='balanco_entrada',
                quantidade=qtd_orfao,
                referencia=f'Vinculação de pendente "{nome_orfao}" (era {anterior}, ficou {existente.quantidade})',
                usuario_id=current_user.id,
            ))
        db.session.delete(orfao)
        db.session.commit()
        flash(f'"{nome_orfao}" mesclado em {existente.nome_item} (+{qtd_orfao} unidades).', 'success')
    else:
        if alvo_tipo == 'receita':
            orfao.receita_id = alvo_id
        else:
            orfao.produto_id = alvo_id
        orfao.nome_pendente = None
        db.session.commit()
        flash(f'"{nome_orfao}" vinculado com sucesso.', 'success')

    return redirect(url_for('pedidos.congelados'))


@pedidos_bp.route('/congelados/balanco/aplicar', methods=['POST'])
@login_required
@admin_required
def congelados_balanco_aplicar():
    """Aplica o balanco apos preview. Re-parseia o texto pra ser idempotente."""
    from app.services import estoque_congelados as svc
    texto = request.form.get('texto', '')
    referencia = request.form.get('referencia', '').strip() or None
    if not texto.strip():
        flash('Lista vazia — nada pra aplicar.', 'warning')
        return redirect(url_for('pedidos.congelados_balanco'))
    parseados = svc.parsear_lista(texto)
    resolvidos = svc.resolver_lista(parseados)
    resultado = svc.aplicar_balanco(resolvidos, current_user, referencia=referencia)
    n_ok = len(resultado['aplicados'])
    n_ign = len(resultado['ignorados'])
    if n_ok:
        flash(f'Balanço aplicado: {n_ok} item(ns) atualizados.'
              + (f' {n_ign} ignorados.' if n_ign else ''), 'success')
    else:
        flash(f'Nenhum item aplicado. {n_ign} ignorados.', 'warning')
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
    lojas = _lojas_operacionais()
    receitas = Receita.query.order_by(Receita.categoria, Receita.nome).all() \
        if current_user.is_admin() else []
    produtos = Produto.query.filter_by(ativo=True).order_by(Produto.nome).all() \
        if current_user.is_admin() else []
    materias = MateriaPrima.query.order_by(MateriaPrima.nome).all() \
        if current_user.is_admin() else []
    return render_template('pedidos/estoque_loja.html', loja=loja, itens=itens,
                           lojas=lojas, sel_loja=loja_id,
                           receitas=receitas, produtos=produtos, materias=materias)


# ── Entrada em lote no Estoque de Loja ──

@pedidos_bp.route('/estoque-loja/entrada-lote', methods=['GET', 'POST'])
@login_required
@admin_required
def estoque_loja_entrada_lote():
    """Preview de entrada em lote — usuario cola lista 'nome: qtd' e ve o que
    vai somar. Apply em outra rota pra ser idempotente."""
    from app.services import estoque_loja_lote as svc

    loja_id = None
    if request.method == 'POST':
        try:
            loja_id = int(request.form.get('loja_id') or 0) or None
        except ValueError:
            loja_id = None
    else:
        try:
            loja_id = int(request.args.get('loja') or 0) or None
        except ValueError:
            loja_id = None

    texto = request.form.get('texto', '') if request.method == 'POST' else ''
    referencia = request.form.get('referencia', '').strip()
    itens = []
    if request.method == 'POST' and texto.strip() and loja_id:
        parseados = svc.parsear_lista(texto)
        itens = svc.resolver_lista(parseados, loja_id)

    lojas = _lojas_operacionais()
    loja = Loja.query.get(loja_id) if loja_id else None
    return render_template('pedidos/estoque_loja_entrada_lote.html',
                           texto=texto, referencia=referencia, itens=itens,
                           lojas=lojas, loja=loja, sel_loja=loja_id)


@pedidos_bp.route('/estoque-loja/saida-lote', methods=['GET', 'POST'])
@login_required
@admin_required
def estoque_loja_saida_lote():
    """Preview de saida em lote — usuario cola lista 'nome: qtd' (vendas
    manuais sem PDV API) e ve o que vai descontar. Apply em outra rota."""
    from app.services import estoque_loja_lote as svc

    loja_id = None
    if request.method == 'POST':
        try:
            loja_id = int(request.form.get('loja_id') or 0) or None
        except ValueError:
            loja_id = None
    else:
        try:
            loja_id = int(request.args.get('loja') or 0) or None
        except ValueError:
            loja_id = None

    texto = request.form.get('texto', '') if request.method == 'POST' else ''
    referencia = request.form.get('referencia', '').strip()
    itens = []
    if request.method == 'POST' and texto.strip() and loja_id:
        parseados = svc.parsear_lista(texto)
        itens = svc.resolver_lista_saida(parseados, loja_id)

    lojas = _lojas_operacionais()
    loja = Loja.query.get(loja_id) if loja_id else None
    return render_template('pedidos/estoque_loja_saida_lote.html',
                           texto=texto, referencia=referencia, itens=itens,
                           lojas=lojas, loja=loja, sel_loja=loja_id)


@pedidos_bp.route('/estoque-loja/mapeamentos')
@login_required
@admin_required
def estoque_loja_mapeamentos():
    """Lista LojaProdutoMap pra admin vincular/ignorar nomes digitados."""
    produtos_map = LojaProdutoMap.query.order_by(
        LojaProdutoMap.ignorar.asc(),
        LojaProdutoMap.confirmado_em.is_(None).desc(),
        LojaProdutoMap.nome_digitado,
    ).all()
    receitas = Receita.query.order_by(Receita.categoria, Receita.nome).all()
    produtos = Produto.query.filter_by(ativo=True).order_by(Produto.nome).all()
    materias = MateriaPrima.query.order_by(MateriaPrima.nome).all()
    return render_template('pedidos/estoque_loja_mapeamentos.html',
                           produtos_map=produtos_map,
                           receitas=receitas, produtos=produtos, materias=materias)


@pedidos_bp.route('/estoque-loja/mapeamentos/vincular/<int:map_id>', methods=['POST'])
@login_required
@admin_required
def estoque_loja_mapeamentos_vincular(map_id):
    """Vincula/ignora/desfaz uma entrada do LojaProdutoMap."""
    from datetime import datetime
    mp = LojaProdutoMap.query.get_or_404(map_id)
    acao = (request.form.get('acao') or '').strip()
    alvo_tipo = (request.form.get('alvo_tipo') or '').strip()
    raw_alvo = request.form.get('alvo_id') or ''
    try:
        alvo_id = int(raw_alvo)
    except (TypeError, ValueError):
        alvo_id = 0
    try:
        fator = float((request.form.get('fator') or '1').replace(',', '.'))
    except (TypeError, ValueError):
        fator = 1.0
    # Sanidade: rejeita absurdos. 100 = teto generoso (compra de 1 venda = 100 unidades).
    if fator < 0.001 or fator > 100:
        fator = 1.0

    # Fallback: form sem submitter (Enter no select)
    if not acao and alvo_id and alvo_tipo in ('receita', 'produto', 'mp'):
        acao = 'vincular'

    if acao == 'vincular':
        if alvo_tipo not in ('receita', 'produto', 'mp') or not alvo_id:
            flash('Selecione um alvo valido.', 'danger')
            return redirect(url_for('pedidos.estoque_loja_mapeamentos'))
        mp.receita_id = alvo_id if alvo_tipo == 'receita' else None
        mp.produto_id = alvo_id if alvo_tipo == 'produto' else None
        mp.materia_prima_id = alvo_id if alvo_tipo == 'mp' else None
        mp.ignorar = False
        mp.fator_quantidade = fator
        mp.confirmado_em = datetime.utcnow()
        mp.confirmado_por = current_user.id
        fator_msg = f' (fator {fator:g})' if fator != 1.0 else ''
        flash(f'"{mp.nome_digitado}" → {mp.alvo_nome}{fator_msg}', 'success')
    elif acao == 'ignorar':
        mp.ignorar = True
        mp.receita_id = None
        mp.produto_id = None
        mp.materia_prima_id = None
        mp.confirmado_em = datetime.utcnow()
        mp.confirmado_por = current_user.id
        flash(f'"{mp.nome_digitado}" ignorado.', 'info')
    elif acao == 'desfazer':
        mp.ignorar = False
        mp.receita_id = None
        mp.produto_id = None
        mp.materia_prima_id = None
        mp.confirmado_em = None
        flash(f'"{mp.nome_digitado}" voltou pra pendente.', 'info')
    else:
        flash(f'Acao desconhecida: {acao!r}.', 'danger')

    db.session.commit()
    return redirect(url_for('pedidos.estoque_loja_mapeamentos'))


@pedidos_bp.route('/estoque-loja/historico-saida-lote')
@login_required
@admin_required
def estoque_loja_historico_saida_lote():
    """Lista MovEstoqueLoja de tipo saida_lote/venda_loja_sem_estoque."""
    from sqlalchemy import desc

    raw_loja = request.args.get('loja')
    try:
        loja_id = int(raw_loja) if raw_loja else None
    except ValueError:
        loja_id = None

    q = db.session.query(MovEstoqueLoja, EstoqueLoja).join(
        EstoqueLoja, MovEstoqueLoja.estoque_loja_id == EstoqueLoja.id
    ).filter(MovEstoqueLoja.tipo.in_(['saida_lote', 'venda_loja_sem_estoque']))
    if loja_id:
        q = q.filter(EstoqueLoja.loja_id == loja_id)
    rows = q.order_by(desc(MovEstoqueLoja.data)).limit(300).all()

    # Pre-carrega catalogos em batch (evita N+1)
    loja_ids = {est.loja_id for _, est in rows}
    rec_ids = {est.receita_id for _, est in rows if est.receita_id}
    prod_ids = {est.produto_id for _, est in rows if est.produto_id}
    mp_ids = {est.materia_prima_id for _, est in rows if est.materia_prima_id}
    lojas_map = {l.id: l for l in Loja.query.filter(Loja.id.in_(loja_ids)).all()} if loja_ids else {}
    rec_map = {r.id: r for r in Receita.query.filter(Receita.id.in_(rec_ids)).all()} if rec_ids else {}
    prod_map = {p.id: p for p in Produto.query.filter(Produto.id.in_(prod_ids)).all()} if prod_ids else {}
    mp_map = {m.id: m for m in MateriaPrima.query.filter(MateriaPrima.id.in_(mp_ids)).all()} if mp_ids else {}

    linhas = []
    for mov, est in rows:
        if est.receita_id:
            r = rec_map.get(est.receita_id)
            item_nome = r.nome if r else '?'
        elif est.produto_id:
            p = prod_map.get(est.produto_id)
            item_nome = p.nome if p else '?'
        elif est.materia_prima_id:
            m = mp_map.get(est.materia_prima_id)
            item_nome = m.nome if m else '?'
        elif est.nome_pendente:
            item_nome = est.nome_pendente
        else:
            item_nome = '?'
        linhas.append({
            'mov': mov, 'estoque': est,
            'loja': lojas_map.get(est.loja_id), 'item_nome': item_nome,
        })

    lojas = _lojas_operacionais()
    return render_template('pedidos/estoque_loja_historico_saida_lote.html',
                           linhas=linhas, lojas=lojas, sel_loja=loja_id)


@pedidos_bp.route('/estoque-loja/saida-lote/aplicar', methods=['POST'])
@login_required
@admin_required
def estoque_loja_saida_lote_aplicar():
    from app.services import estoque_loja_lote as svc
    try:
        loja_id = int(request.form.get('loja_id') or 0)
    except ValueError:
        loja_id = 0
    if not loja_id:
        flash('Selecione uma loja.', 'warning')
        return redirect(url_for('pedidos.estoque_loja_saida_lote'))

    texto = request.form.get('texto', '')
    referencia = request.form.get('referencia', '').strip() or None
    if not texto.strip():
        flash('Lista vazia — nada pra aplicar.', 'warning')
        return redirect(url_for('pedidos.estoque_loja_saida_lote', loja=loja_id))

    parseados = svc.parsear_lista(texto)
    resolvidos = svc.resolver_lista_saida(parseados, loja_id)
    resultado = svc.aplicar_saida_lote(resolvidos, loja_id, current_user,
                                        referencia=referencia)
    n_ok = len(resultado['aplicados'])
    n_ign = len(resultado['ignorados'])
    if n_ok:
        flash(f'Saida aplicada: {n_ok} item(ns) descontado(s).'
              + (f' {n_ign} ignorado(s)/pendente(s).' if n_ign else ''), 'success')
    else:
        flash(f'Nenhum item descontado. {n_ign} ignorado(s)/pendente(s) — '
              f'vincule em /pedidos/estoque-loja/mapeamentos.', 'warning')
    return redirect(url_for('pedidos.estoque_loja_saida_lote', loja=loja_id))


@pedidos_bp.route('/estoque-loja/entrada-lote/aplicar', methods=['POST'])
@login_required
@admin_required
def estoque_loja_entrada_lote_aplicar():
    from app.services import estoque_loja_lote as svc
    try:
        loja_id = int(request.form.get('loja_id') or 0)
    except ValueError:
        loja_id = 0
    if not loja_id:
        flash('Selecione uma loja.', 'warning')
        return redirect(url_for('pedidos.estoque_loja_entrada_lote'))

    texto = request.form.get('texto', '')
    referencia = request.form.get('referencia', '').strip() or None
    if not texto.strip():
        flash('Lista vazia — nada pra aplicar.', 'warning')
        return redirect(url_for('pedidos.estoque_loja_entrada_lote', loja=loja_id))

    parseados = svc.parsear_lista(texto)
    resolvidos = svc.resolver_lista(parseados, loja_id)
    resultado = svc.aplicar_entrada_lote(resolvidos, loja_id, current_user,
                                          referencia=referencia)
    n_ok = len(resultado['aplicados'])
    n_ign = len(resultado['ignorados'])
    if n_ok:
        flash(f'Entrada aplicada: {n_ok} item(ns).'
              + (f' {n_ign} ignorados.' if n_ign else ''), 'success')
    else:
        flash(f'Nenhum item aplicado. {n_ign} ignorados.', 'warning')
    return redirect(url_for('pedidos.estoque_loja', loja=loja_id))


@pedidos_bp.route('/estoque-loja/vincular', methods=['POST'])
@login_required
@admin_required
def estoque_loja_vincular():
    """Vincula uma EstoqueLoja pendente a uma receita/produto/MP."""
    ep_id = int(request.form['estoque_id'])
    alvo_tipo = request.form.get('alvo_tipo')
    try:
        alvo_id = int(request.form.get('alvo_id', ''))
    except (TypeError, ValueError):
        alvo_id = 0
    if alvo_tipo not in ('receita', 'produto', 'mp') or not alvo_id:
        flash('Selecione um item valido.', 'danger')
        return redirect(url_for('pedidos.estoque_loja'))

    orfao = EstoqueLoja.query.get_or_404(ep_id)
    loja_id = orfao.loja_id
    if not orfao.pendente:
        flash('Este item ja esta vinculado.', 'warning')
        return redirect(url_for('pedidos.estoque_loja', loja=loja_id))

    filtro = {'loja_id': loja_id}
    if alvo_tipo == 'receita':
        filtro['receita_id'] = alvo_id
    elif alvo_tipo == 'produto':
        filtro['produto_id'] = alvo_id
    else:
        filtro['materia_prima_id'] = alvo_id

    existente = EstoqueLoja.query.filter_by(**filtro).first()
    nome_orfao = orfao.nome_pendente or '?'
    qtd_orfao = orfao.quantidade or 0

    if existente and existente.id != orfao.id:
        anterior = existente.quantidade or 0
        existente.quantidade = anterior + qtd_orfao
        if qtd_orfao:
            db.session.add(MovEstoqueLoja(
                estoque_loja_id=existente.id,
                tipo='entrada_lote',
                quantidade=qtd_orfao,
                referencia=f'Vinculacao de pendente "{nome_orfao}" (era {anterior}, ficou {existente.quantidade})',
                usuario_id=current_user.id,
            ))
        db.session.delete(orfao)
        db.session.commit()
        flash(f'"{nome_orfao}" mesclado em {existente.nome_item} (+{qtd_orfao}).', 'success')
    else:
        if alvo_tipo == 'receita':
            orfao.receita_id = alvo_id
        elif alvo_tipo == 'produto':
            orfao.produto_id = alvo_id
        else:
            orfao.materia_prima_id = alvo_id
        orfao.nome_pendente = None
        db.session.commit()
        flash(f'"{nome_orfao}" vinculado com sucesso.', 'success')

    return redirect(url_for('pedidos.estoque_loja', loja=loja_id))


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
    TIPOS_VALIDOS = {'venda', 'ajuste', 'devolucao', 'descarte'}

    # Pre-carrega EstoqueLoja em batch (evita N+1)
    eids_int = []
    for eid in ids:
        try:
            eids_int.append(int(eid))
        except (TypeError, ValueError):
            eids_int.append(None)
    eids_validos = [e for e in eids_int if e is not None]
    els_map = {e.id: e for e in EstoqueLoja.query.filter(EstoqueLoja.id.in_(eids_validos)).all()} if eids_validos else {}

    for i, eid_int in enumerate(eids_int):
        if eid_int is None:
            continue
        try:
            qtd = int(qtds[i]) if i < len(qtds) and qtds[i] else 0
        except (TypeError, ValueError):
            continue
        if qtd <= 0:
            continue
        el = els_map.get(eid_int)
        if not el or el.loja_id != loja_id:
            continue
        tipo = tipos[i] if i < len(tipos) else 'venda'
        if tipo not in TIPOS_VALIDOS:
            tipo = 'venda'
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
    tipo, item_id = _parse_item_id(request.form.get('item_id', ''))
    qtd = int(request.form.get('quantidade', 0))
    operacao = request.form.get('operacao', 'entrada')
    motivo = request.form.get('motivo', '').strip()

    if not loja_id or not item_id or qtd <= 0 or not motivo:
        flash('Loja, item, quantidade (>0) e motivo sao obrigatorios.', 'warning')
        return redirect(url_for('pedidos.estoque_loja', loja=loja_id or None))

    filtro = {'loja_id': loja_id}
    if tipo == 'receita':
        filtro['receita_id'] = item_id
    else:
        filtro['materia_prima_id'] = item_id
    el = EstoqueLoja.query.filter_by(**filtro).first()
    if not el:
        if operacao != 'entrada':
            flash('Item inexistente no estoque — so e possivel fazer entrada.', 'warning')
            return redirect(url_for('pedidos.estoque_loja', loja=loja_id))
        el = EstoqueLoja(**filtro)
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
