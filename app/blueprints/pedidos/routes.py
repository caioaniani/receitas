import io
from collections import defaultdict
from datetime import date, datetime, timedelta

from flask import render_template, redirect, url_for, flash, request, abort, send_file, current_app
from flask_login import login_required, current_user
from sqlalchemy.orm import joinedload, selectinload

from app.blueprints.pedidos import pedidos_bp
from app.decorators import (admin_required, gerente_required, producao_required,
                              operacional_pedido_required)
from app.extensions import db
from app.utils import agora, hoje as hoje_brt
from app.models import (
    Loja, Receita, Produto, MateriaPrima, MovimentacaoEstoque,
    PedidoLoja, PedidoItem,
    EstoqueProducao, MovEstoqueProducao,
    EstoqueLoja, MovEstoqueLoja,
    PrecoLojaReceita, FotoRecebimento,
    LojaProdutoMap, Desperdicio, PedidoQRCode,
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
    """Retorna a loja com que o usuario opera por padrao.

    Admin e gerente: None (podem ver/atuar em qualquer loja).
    Outros papeis: forca a loja vinculada ao usuario.
    """
    if current_user.is_admin() or current_user.is_gerente():
        return None
    return current_user.loja_id


@pedidos_bp.route('/')
@login_required
@gerente_required
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
@gerente_required
def novo():
    loja_id = _loja_do_usuario()
    if not current_user.is_admin() and not loja_id:
        flash('Vincule sua conta a uma loja para criar pedidos.', 'warning')
        return redirect(url_for('pedidos.lista'))

    amanha = hoje_brt() + timedelta(days=1)

    if request.method == 'POST':
        # Admin e gerente podem escolher qualquer loja no form;
        # outros papeis sao forcados pra propria loja.
        pode_qualquer_loja = current_user.is_admin() or current_user.is_gerente()
        try:
            sel_loja = (int(request.form.get('loja_id', 0)) if pode_qualquer_loja
                        else (loja_id or current_user.loja_id))
        except (TypeError, ValueError):
            sel_loja = 0
        if not pode_qualquer_loja and sel_loja != current_user.loja_id:
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
@gerente_required
def detalhe(id):
    pedido = PedidoLoja.query.get_or_404(id)
    loja_id = _loja_do_usuario()
    if loja_id and pedido.loja_id != loja_id:
        abort(403)
    return render_template('pedidos/detalhe.html', pedido=pedido)


@pedidos_bp.route('/<int:id>/confirmar', methods=['POST'])
@login_required
@operacional_pedido_required
def confirmar(id):
    pedido = PedidoLoja.query.get_or_404(id)
    pedido.status = 'confirmado'
    db.session.commit()
    flash('Pedido confirmado.', 'success')
    return redirect(url_for('pedidos.detalhe', id=id))


@pedidos_bp.route('/<int:id>/separar', methods=['POST'])
@login_required
@operacional_pedido_required
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
@operacional_pedido_required
def enviar(id):
    pedido = PedidoLoja.query.get_or_404(id)
    try:
        ok, msg = _executar_envio_pedido(pedido, current_user, ref_extra=None)
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        current_app.logger.exception('Falha ao enviar pedido %s', id)
        flash(f'Erro ao processar saída do pedido: {exc}. Nada foi alterado.', 'danger')
        return redirect(url_for('pedidos.detalhe', id=id))
    flash(msg, 'success' if ok else 'warning')
    return redirect(url_for('pedidos.detalhe', id=id))


def _executar_envio_pedido(pedido, user, ref_extra=None):
    """Baixa estoque da industria + status separado → em_transporte.

    Chamavel por (1) rota /pedidos/<id>/enviar (admin click) ou (2)
    handshake QR (motorista escaneia + digita PIN). Levanta Exception em
    falha pra caller fazer rollback. `ref_extra` adiciona texto na
    referencia do movimento (ex: 'via QR / motorista Joao').
    """
    if pedido.status != 'separado':
        return False, f'Pedido precisa estar separado (atual: {pedido.status}).'

    ref_base = f'Pedido #{pedido.id} → {pedido.loja.nome}'
    if ref_extra:
        ref_base += f' ({ref_extra})'

    for item in pedido.itens:
        if item.materia_prima_id:
            mp = MateriaPrima.query.get(item.materia_prima_id)
            if mp:
                mp.estoque_atual = max(0, (mp.estoque_atual or 0) - item.quantidade)
                db.session.add(MovimentacaoEstoque(
                    materia_prima_id=mp.id, tipo='saida',
                    quantidade=item.quantidade,
                    referencia=ref_base,
                    usuario_id=getattr(user, 'id', None),
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
                referencia=ref_base,
                usuario_id=getattr(user, 'id', None),
            ))

    pedido.status = 'em_transporte'
    db.session.commit()
    return True, 'Pedido em transporte. Estoque da industria baixado.'


@pedidos_bp.route('/<int:id>/qr-saida')
@login_required
@operacional_pedido_required
def qr_saida(id):
    """Gera/recicla token QR pra handshake de saida (separado → em_transporte).

    Mostra QR Code apontando pra /handshake/<token>. Motorista escaneia
    com o celular, digita PIN, e o status muda. Reutiliza token valido
    existente pra evitar lixo se admin re-abrir a pagina."""
    import secrets
    from datetime import timedelta
    from app.services.qrcode_svc import gerar_png_data_url
    pedido = PedidoLoja.query.get_or_404(id)
    if pedido.status != 'separado':
        flash(f'Pedido precisa estar separado (atual: {pedido.status}).', 'warning')
        return redirect(url_for('pedidos.detalhe', id=id))
    qr = (PedidoQRCode.query
          .filter_by(pedido_id=pedido.id, tipo='saida', usado_em=None)
          .filter(PedidoQRCode.expira_em > agora())
          .order_by(PedidoQRCode.criado_em.desc()).first())
    if not qr:
        qr = PedidoQRCode(
            token=secrets.token_urlsafe(24),
            pedido_id=pedido.id, tipo='saida',
            criado_por_id=current_user.id,
            expira_em=agora() + timedelta(hours=2),
        )
        db.session.add(qr)
        db.session.commit()
    url = url_for('handshake.handshake', token=qr.token, _external=True)
    qr_png = gerar_png_data_url(url)
    return render_template('pedidos/qr_saida.html',
                            pedido=pedido, qr=qr, url=url, qr_png=qr_png)


@pedidos_bp.route('/<int:id>/receber', methods=['POST'])
@login_required
def receber(id):
    pedido = PedidoLoja.query.get_or_404(id)
    loja_id = _loja_do_usuario()
    if loja_id and pedido.loja_id != loja_id:
        abort(403)

    recebidos = {}
    for key, val in request.form.items():
        if key.startswith('recebido_') and val.strip():
            try:
                recebidos[int(key[len('recebido_'):])] = max(0, int(val))
            except ValueError:
                continue

    fotos_payload = []
    for f in request.files.getlist('fotos'):
        if not f or not f.filename:
            continue
        content = f.read()
        if not content:
            continue
        fotos_payload.append({'imagem': content,
                              'mimetype': f.mimetype or 'image/jpeg'})

    try:
        ok, msg, divergencias = _executar_recebimento_pedido(
            pedido, current_user, recebidos_map=recebidos,
            fotos=fotos_payload,
        )
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        current_app.logger.exception('Falha ao receber pedido %s', id)
        flash(f'Erro ao processar recebimento: {exc}. Nada foi alterado.', 'danger')
        return redirect(url_for('pedidos.detalhe', id=id))

    if not ok:
        flash(msg, 'warning')
        return redirect(url_for('pedidos.detalhe', id=id))
    flash(msg, 'warning' if divergencias else 'success')
    return redirect(url_for('pedidos.detalhe', id=id))


def _executar_recebimento_pedido(pedido, user, recebidos_map=None, fotos=None,
                                  ref_extra=None):
    """Sobe estoque da loja + status em_transporte → entregue.

    recebidos_map: {item_id: qtd_recebida}. Itens omitidos assumem qtd
    igual a do pedido (sem divergencia). Usado pra preencher
    PedidoItem.quantidade_recebida + criar MovEstoqueLoja.
    fotos: lista de {imagem, mimetype} salvas como FotoRecebimento.
    ref_extra: texto extra nas movimentacoes (ex: 'via QR / loja X').

    Retorna (ok, msg, divergencias). Exception em caller faz rollback.
    """
    if pedido.status != 'em_transporte':
        return False, f'Pedido precisa estar em transporte (atual: {pedido.status}).', []

    recebidos_map = recebidos_map or {}
    fotos = fotos or []
    divergencias = []

    for item in pedido.itens:
        qtd_rec = recebidos_map.get(item.id, item.quantidade)
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
        ref = f'Pedido #{pedido.id}{ref_div}'
        if ref_extra:
            ref += f' ({ref_extra})'
        db.session.add(MovEstoqueLoja(
            estoque_loja_id=el.id, tipo='entrada_pedido',
            quantidade=qtd_rec,
            referencia=ref,
            usuario_id=getattr(user, 'id', None),
        ))

    pedido.status = 'entregue'
    if divergencias:
        nota = 'Divergencias no recebimento: ' + '; '.join(divergencias)
        pedido.observacao = (pedido.observacao + ' | ' if pedido.observacao else '') + nota

    for foto in fotos:
        db.session.add(FotoRecebimento(
            pedido_id=pedido.id,
            imagem=foto['imagem'],
            mimetype=foto.get('mimetype', 'image/jpeg'),
            enviada_por=getattr(user, 'id', None),
        ))

    db.session.commit()
    msg = ('Pedido recebido com divergencias. Detalhes salvos na observacao.'
           if divergencias else
           'Pedido recebido integralmente. Estoque da loja atualizado.')
    return True, msg, divergencias


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
@gerente_required
def relatorio():
    hoje = hoje_brt()
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


@pedidos_bp.route('/<int:id>/voltar-status', methods=['POST'])
@login_required
@admin_required
def voltar_status(id):
    """Volta o pedido pra o status anterior, estornando movimentos de estoque
    se necessario. So admin (risco de descompasso de estoque).

    Transicoes:
      recebido     -> em_transporte (estorna estoque loja)
      em_transporte-> separado      (estorna estoque producao + MP)
      separado     -> confirmado    (so status)
      confirmado   -> pendente      (so status)
    Cancelado/pendente: nao volta.
    """
    pedido = PedidoLoja.query.get_or_404(id)
    status_atual = pedido.status
    novo_status = None

    try:
        if status_atual in ('entregue', 'recebido'):
            # Estorna o que somou no estoque da loja
            for item in pedido.itens:
                qtd = item.quantidade_recebida or 0
                if qtd <= 0:
                    continue
                el = EstoqueLoja.query.filter_by(
                    loja_id=pedido.loja_id,
                    receita_id=item.receita_id,
                    produto_id=item.produto_id,
                    materia_prima_id=item.materia_prima_id,
                ).first()
                if el:
                    el.quantidade = max(0, (el.quantidade or 0) - qtd)
                    db.session.add(MovEstoqueLoja(
                        estoque_loja_id=el.id,
                        tipo='ajuste_negativo',
                        quantidade=qtd,
                        referencia=f'Estorno pedido #{pedido.id} (voltar status)',
                        usuario_id=current_user.id,
                    ))
                item.quantidade_recebida = None
            novo_status = 'em_transporte'
        elif status_atual == 'em_transporte':
            # Estorna baixa do estoque producao/MP
            for item in pedido.itens:
                if item.materia_prima_id:
                    mp = MateriaPrima.query.get(item.materia_prima_id)
                    if mp:
                        mp.estoque_atual = (mp.estoque_atual or 0) + item.quantidade
                        db.session.add(MovimentacaoEstoque(
                            materia_prima_id=mp.id, tipo='entrada',
                            quantidade=item.quantidade,
                            referencia=f'Estorno pedido #{pedido.id} (voltar status)',
                            usuario_id=current_user.id,
                        ))
                    continue
                ep = EstoqueProducao.query.filter_by(
                    receita_id=item.receita_id, produto_id=item.produto_id
                ).first()
                if ep:
                    ep.quantidade = (ep.quantidade or 0) + item.quantidade
                    db.session.add(MovEstoqueProducao(
                        estoque_producao_id=ep.id, tipo='ajuste',
                        quantidade=item.quantidade,
                        referencia=f'Estorno pedido #{pedido.id} (voltar status)',
                        usuario_id=current_user.id,
                    ))
            novo_status = 'separado'
        elif status_atual == 'separado':
            novo_status = 'confirmado'
        elif status_atual == 'confirmado':
            novo_status = 'pendente'
        else:
            flash(f'Nao da pra voltar status "{status_atual}".', 'warning')
            return redirect(url_for('pedidos.detalhe', id=id))

        pedido.status = novo_status
        db.session.commit()
        flash(f'Status revertido: {status_atual} → {novo_status}.', 'success')
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        current_app.logger.exception('Falha ao voltar status pedido %s', id)
        flash(f'Erro ao voltar status: {exc}. Nada foi alterado.', 'danger')
    return redirect(url_for('pedidos.detalhe', id=id))


@pedidos_bp.route('/<int:id>/cancelar', methods=['POST'])
@login_required
@gerente_required
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
@producao_required
def separacao():
    pedidos = PedidoLoja.query.filter(
        PedidoLoja.status.in_(['pendente', 'confirmado'])
    ).order_by(PedidoLoja.data_entrega).all()

    por_data = defaultdict(lambda: defaultdict(lambda: {'total': 0, 'lojas': defaultdict(int)}))
    ids_por_data = defaultdict(list)
    # data → loja_nome → [(item, qtd, pedido_id)]
    por_data_loja = defaultdict(lambda: defaultdict(list))
    ids_por_data_loja = defaultdict(lambda: defaultdict(list))

    for p in pedidos:
        chave_data = p.data_entrega or p.data_pedido
        ids_por_data[chave_data].append(p.id)
        loja_nome = p.loja.nome if p.loja else '?'
        ids_por_data_loja[chave_data][loja_nome].append(p.id)
        for item in p.itens:
            nome = item.nome_item
            por_data[chave_data][nome]['total'] += item.quantidade
            por_data[chave_data][nome]['lojas'][loja_nome] += item.quantidade
            por_data_loja[chave_data][loja_nome].append({
                'item': nome, 'qtd': item.quantidade, 'pedido_id': p.id,
            })

    # Agrega items duplicados dentro de cada loja (mesmo item em 2 pedidos)
    for data, lojas in por_data_loja.items():
        for loja_nome, itens in lojas.items():
            agreg = defaultdict(int)
            for it in itens:
                agreg[it['item']] += it['qtd']
            lojas[loja_nome] = sorted(
                [{'item': nome, 'qtd': qtd} for nome, qtd in agreg.items()],
                key=lambda x: x['item']
            )

    congelados = {ep.nome_item: ep.quantidade for ep in EstoqueProducao.query.all()}

    return render_template('pedidos/separacao.html',
                           por_data=dict(por_data),
                           ids_por_data=dict(ids_por_data),
                           por_data_loja={d: dict(ls) for d, ls in por_data_loja.items()},
                           ids_por_data_loja={d: dict(ls) for d, ls in ids_por_data_loja.items()},
                           congelados=congelados)


# ── Estoque de Congelados ──

@pedidos_bp.route('/congelados/dashboard')
@login_required
@producao_required
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
@producao_required
def congelados():
    itens = EstoqueProducao.query.options(
        joinedload(EstoqueProducao.receita),
        joinedload(EstoqueProducao.produto),
    ).all()
    receitas = Receita.query.order_by(Receita.categoria, Receita.nome).all()
    produtos = Produto.query.filter_by(ativo=True).order_by(Produto.nome).all()
    from app.services import estoque_congelados as svc_cong
    pendentes = [it for it in itens if it.pendente]
    sugestoes = svc_cong.sugerir_para_pendentes(pendentes) if pendentes else {}
    return render_template('pedidos/congelados.html', itens=itens,
                           receitas=receitas, produtos=produtos,
                           sugestoes=sugestoes)


@pedidos_bp.route('/congelados/historico')
@login_required
@producao_required
def congelados_historico():
    """Historico de movimentos do estoque da industria (EstoqueProducao).

    Filtros: item (id), tipo, periodo (de/ate).
    """
    from sqlalchemy import desc

    item_id = request.args.get('item', type=int)
    tipo_filtro = (request.args.get('tipo') or '').strip()
    de_str = (request.args.get('de') or '').strip()
    ate_str = (request.args.get('ate') or '').strip()
    try:
        de = datetime.strptime(de_str, '%Y-%m-%d').date() if de_str else None
    except ValueError:
        de = None
    try:
        ate = datetime.strptime(ate_str, '%Y-%m-%d').date() if ate_str else None
    except ValueError:
        ate = None

    tipos_disp = ['entrada', 'saida_pedido', 'ajuste', 'balanco', 'desperdicio']

    q = db.session.query(MovEstoqueProducao, EstoqueProducao).join(
        EstoqueProducao, MovEstoqueProducao.estoque_producao_id == EstoqueProducao.id
    )
    if item_id:
        q = q.filter(EstoqueProducao.id == item_id)
    if tipo_filtro:
        q = q.filter(MovEstoqueProducao.tipo == tipo_filtro)
    if de:
        q = q.filter(MovEstoqueProducao.data >= datetime.combine(de, datetime.min.time()))
    if ate:
        q = q.filter(MovEstoqueProducao.data <= datetime.combine(ate, datetime.max.time()))

    rows = q.order_by(desc(MovEstoqueProducao.data)).limit(500).all()

    from app.models import Usuario
    user_ids = {mov.usuario_id for mov, _ in rows if mov.usuario_id}
    user_map = {u.id: u for u in Usuario.query.filter(Usuario.id.in_(user_ids)).all()} if user_ids else {}

    linhas = []
    for mov, ep in rows:
        linhas.append({
            'mov': mov, 'estoque': ep,
            'item_nome': ep.nome_item,
            'usuario': user_map.get(mov.usuario_id) if mov.usuario_id else None,
        })

    from collections import defaultdict
    totais = defaultdict(lambda: {'qtd': 0, 'n': 0})
    for mov, _ in rows:
        totais[mov.tipo]['qtd'] += mov.quantidade or 0
        totais[mov.tipo]['n'] += 1

    itens_disp = EstoqueProducao.query.order_by(EstoqueProducao.id).all()
    item_sel = EstoqueProducao.query.get(item_id) if item_id else None

    return render_template('pedidos/congelados_historico.html',
                           linhas=linhas, itens_disp=itens_disp, item_sel=item_sel,
                           tipos_disp=tipos_disp, tipo_filtro=tipo_filtro,
                           de=de_str, ate=ate_str, totais=dict(totais))


@pedidos_bp.route('/congelados/entrada', methods=['POST'])
@login_required
@producao_required
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
@producao_required
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
@producao_required
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
@producao_required
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

    # Apelido global — proximo balanco com o mesmo nome resolve direto.
    _salvar_apelido_global(nome_orfao, alvo_tipo, alvo_id)

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
@producao_required
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
@gerente_required
def estoque_loja():
    loja_id = _loja_do_usuario()
    if current_user.is_admin():
        sel = request.args.get('loja')
        loja_id = int(sel) if sel else None

    loja = Loja.query.get(loja_id) if loja_id else None
    itens = (EstoqueLoja.query.filter_by(loja_id=loja_id)
             .options(joinedload(EstoqueLoja.receita),
                      joinedload(EstoqueLoja.produto),
                      joinedload(EstoqueLoja.materia_prima))
             .all()) if loja_id else []
    lojas = _lojas_operacionais()
    receitas = Receita.query.order_by(Receita.categoria, Receita.nome).all() \
        if current_user.is_admin() else []
    produtos = Produto.query.filter_by(ativo=True).order_by(Produto.nome).all() \
        if current_user.is_admin() else []
    materias = MateriaPrima.query.order_by(MateriaPrima.nome).all() \
        if current_user.is_admin() else []
    sugestoes = {}
    if current_user.is_admin() and itens:
        from app.services import estoque_loja_lote as svc_lote
        pendentes = [it for it in itens if it.pendente]
        sugestoes = svc_lote.sugerir_para_pendentes(pendentes)
    return render_template('pedidos/estoque_loja.html', loja=loja, itens=itens,
                           lojas=lojas, sel_loja=loja_id,
                           receitas=receitas, produtos=produtos, materias=materias,
                           sugestoes=sugestoes)


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
@gerente_required
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
        mp.confirmado_em = agora()
        mp.confirmado_por = current_user.id
        fator_msg = f' (fator {fator:g})' if fator != 1.0 else ''
        flash(f'"{mp.nome_digitado}" → {mp.alvo_nome}{fator_msg}', 'success')
    elif acao == 'ignorar':
        mp.ignorar = True
        mp.receita_id = None
        mp.produto_id = None
        mp.materia_prima_id = None
        mp.confirmado_em = agora()
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


@pedidos_bp.route('/estoque-loja/historico')
@login_required
@gerente_required
def estoque_loja_historico():
    """Lista TODAS as movimentacoes de MovEstoqueLoja, filtravel por loja/tipo/data."""
    from sqlalchemy import desc

    raw_loja = request.args.get('loja')
    try:
        loja_id = int(raw_loja) if raw_loja else None
    except ValueError:
        loja_id = None

    tipos_disp = [
        'entrada_pedido', 'entrada_manual', 'ajuste_negativo',
        'saida_lote', 'venda_loja_sem_estoque',
        'venda_seru', 'venda_seru_estorno', 'venda_seru_sem_estoque',
        'venda_vnda', 'venda_vnda_estorno', 'venda_vnda_sem_estoque',
        'desperdicio', 'desperdicio_sem_estoque', 'desperdicio_estorno',
    ]
    tipo_filtro = request.args.get('tipo', '').strip()

    de_str = (request.args.get('de') or '').strip()
    ate_str = (request.args.get('ate') or '').strip()
    try:
        de = datetime.strptime(de_str, '%Y-%m-%d').date() if de_str else None
    except ValueError:
        de = None
    try:
        ate = datetime.strptime(ate_str, '%Y-%m-%d').date() if ate_str else None
    except ValueError:
        ate = None

    # Nao-admin so ve a propria loja
    loja_user = _loja_do_usuario()
    if loja_user:
        loja_id = loja_user

    q = db.session.query(MovEstoqueLoja, EstoqueLoja).join(
        EstoqueLoja, MovEstoqueLoja.estoque_loja_id == EstoqueLoja.id
    )
    if loja_id:
        q = q.filter(EstoqueLoja.loja_id == loja_id)
    if tipo_filtro:
        q = q.filter(MovEstoqueLoja.tipo == tipo_filtro)
    if de:
        q = q.filter(MovEstoqueLoja.data >= datetime.combine(de, datetime.min.time()))
    if ate:
        q = q.filter(MovEstoqueLoja.data <= datetime.combine(ate, datetime.max.time()))

    rows = q.order_by(desc(MovEstoqueLoja.data)).limit(500).all()

    loja_ids = {est.loja_id for _, est in rows}
    rec_ids = {est.receita_id for _, est in rows if est.receita_id}
    prod_ids = {est.produto_id for _, est in rows if est.produto_id}
    mp_ids = {est.materia_prima_id for _, est in rows if est.materia_prima_id}
    user_ids = {mov.usuario_id for mov, _ in rows if mov.usuario_id}

    from app.models import Usuario
    lojas_map = {l.id: l for l in Loja.query.filter(Loja.id.in_(loja_ids)).all()} if loja_ids else {}
    rec_map = {r.id: r for r in Receita.query.filter(Receita.id.in_(rec_ids)).all()} if rec_ids else {}
    prod_map = {p.id: p for p in Produto.query.filter(Produto.id.in_(prod_ids)).all()} if prod_ids else {}
    mp_map = {m.id: m for m in MateriaPrima.query.filter(MateriaPrima.id.in_(mp_ids)).all()} if mp_ids else {}
    user_map = {u.id: u for u in Usuario.query.filter(Usuario.id.in_(user_ids)).all()} if user_ids else {}

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
            item_nome = m.nome + ' (MP)' if m else '?'
        elif est.nome_pendente:
            item_nome = est.nome_pendente
        else:
            item_nome = '?'
        linhas.append({
            'mov': mov, 'estoque': est,
            'loja': lojas_map.get(est.loja_id),
            'item_nome': item_nome,
            'usuario': user_map.get(mov.usuario_id) if mov.usuario_id else None,
        })

    # Totais por tipo
    from collections import defaultdict
    totais = defaultdict(lambda: {'qtd': 0, 'n': 0})
    for mov, _ in rows:
        totais[mov.tipo]['qtd'] += mov.quantidade or 0
        totais[mov.tipo]['n'] += 1

    lojas = _lojas_operacionais()
    return render_template('pedidos/estoque_loja_historico.html',
                           linhas=linhas, lojas=lojas, sel_loja=loja_id,
                           tipos_disp=tipos_disp, tipo_filtro=tipo_filtro,
                           de=de_str, ate=ate_str, totais=dict(totais))


@pedidos_bp.route('/estoque-loja/historico-saida-lote')
@login_required
@gerente_required
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
@gerente_required
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


def _salvar_apelido_global(nome_digitado, alvo_tipo, alvo_id):
    """Cria/atualiza LojaProdutoMap (apelido global) ao vincular um pendente.

    Vale pra qualquer loja — apelido 'PFR' vinculado uma vez em Ribeiro
    serve tambem em Anesio. Confirmado_em preenchido = entrada/saida em
    lote usa direto sem virar pendente.
    """
    nome = (nome_digitado or '').strip()
    if not nome or nome == '?' or alvo_tipo not in ('receita', 'produto', 'mp'):
        return
    from sqlalchemy import func as sa_func
    mp = LojaProdutoMap.query.filter(
        sa_func.lower(LojaProdutoMap.nome_digitado) == nome.lower()
    ).first()
    if not mp:
        mp = LojaProdutoMap(nome_digitado=nome)
        db.session.add(mp)
    mp.receita_id = alvo_id if alvo_tipo == 'receita' else None
    mp.produto_id = alvo_id if alvo_tipo == 'produto' else None
    mp.materia_prima_id = alvo_id if alvo_tipo == 'mp' else None
    mp.ignorar = False
    mp.confirmado_em = agora()
    mp.confirmado_por = current_user.id


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

    # Salva apelido global pra reusar em lancamentos futuros (qualquer loja).
    _salvar_apelido_global(nome_orfao, alvo_tipo, alvo_id)

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


# ── Desperdicio (sobra do dia / vencido) ──

@pedidos_bp.route('/desperdicio', methods=['GET', 'POST'])
@login_required
@gerente_required
def desperdicio():
    """Registra sobra do dia descartada (vencida) + lista historico.

    Form: loja, item (receita/produto/MP), quantidade, data (default hoje),
    observacao. Cria Desperdicio + MovEstoqueLoja(tipo='desperdicio')
    e baixa do estoque (limitado ao saldo, sem ficar negativo).
    """
    loja_id_user = _loja_do_usuario()
    pode_qualquer_loja = current_user.is_admin() or current_user.is_gerente()

    if request.method == 'POST':
        try:
            sel_loja = (int(request.form.get('loja_id') or 0)
                        if pode_qualquer_loja else loja_id_user)
        except (TypeError, ValueError):
            sel_loja = 0
        if not pode_qualquer_loja and sel_loja != current_user.loja_id:
            abort(403)
        if not sel_loja:
            flash('Selecione uma loja.', 'warning')
            return redirect(url_for('pedidos.desperdicio'))

        raw = request.form.get('item_id', '')
        tipo_item, item_id = None, None
        try:
            if raw.startswith('r_'):
                tipo_item, item_id = 'receita', int(raw[2:])
            elif raw.startswith('mp_'):
                tipo_item, item_id = 'mp', int(raw[3:])
            elif raw.startswith('p_'):
                tipo_item, item_id = 'produto', int(raw[2:])
        except ValueError:
            tipo_item, item_id = None, None
        if not tipo_item or not item_id:
            flash('Selecione um item valido.', 'warning')
            return redirect(url_for('pedidos.desperdicio', loja=sel_loja))

        try:
            qtd = int(request.form.get('quantidade') or 0)
        except (TypeError, ValueError):
            qtd = 0
        if qtd <= 0:
            flash('Quantidade deve ser > 0.', 'warning')
            return redirect(url_for('pedidos.desperdicio', loja=sel_loja))

        data_str = request.form.get('data', '')
        try:
            data_desp = datetime.strptime(data_str, '%Y-%m-%d').date()
        except ValueError:
            data_desp = hoje_brt()

        observacao = (request.form.get('observacao') or '').strip() or None
        motivo = (request.form.get('motivo') or 'vencido').strip() or 'vencido'

        filtro = {'loja_id': sel_loja}
        if tipo_item == 'receita':
            filtro['receita_id'] = item_id
        elif tipo_item == 'produto':
            filtro['produto_id'] = item_id
        elif tipo_item == 'mp':
            filtro['materia_prima_id'] = item_id

        el = EstoqueLoja.query.filter_by(**filtro).first()
        if not el:
            el = EstoqueLoja(**filtro, quantidade=0)
            db.session.add(el)
            db.session.flush()

        saldo = el.quantidade or 0
        baixa = min(qtd, saldo)
        el.quantidade = saldo - baixa

        desp = Desperdicio(
            loja_id=sel_loja,
            receita_id=item_id if tipo_item == 'receita' else None,
            produto_id=item_id if tipo_item == 'produto' else None,
            materia_prima_id=item_id if tipo_item == 'mp' else None,
            quantidade=qtd,
            data=data_desp,
            motivo=motivo,
            observacao=observacao,
            criado_por_id=current_user.id,
        )
        db.session.add(desp)
        if baixa > 0:
            db.session.add(MovEstoqueLoja(
                estoque_loja_id=el.id, tipo='desperdicio', quantidade=baixa,
                referencia=f'Desperdicio {motivo}'
                + (f' — {observacao}' if observacao else ''),
                usuario_id=current_user.id,
            ))
        if qtd > baixa:
            falta = qtd - baixa
            db.session.add(MovEstoqueLoja(
                estoque_loja_id=el.id, tipo='desperdicio_sem_estoque',
                quantidade=falta,
                referencia=f'Desperdicio {motivo} — registrado sem estoque ({falta})',
                usuario_id=current_user.id,
            ))
        db.session.commit()
        flash(f'Desperdicio registrado: {qtd} un de {desp.nome_item}.', 'success')
        return redirect(url_for('pedidos.desperdicio', loja=sel_loja))

    # GET: form + lista
    if pode_qualquer_loja:
        sel = request.args.get('loja')
        loja_filtro = int(sel) if sel else None
    else:
        loja_filtro = loja_id_user

    lojas = _lojas_operacionais()
    loja = Loja.query.get(loja_filtro) if loja_filtro else None

    receitas = Receita.query.order_by(Receita.categoria, Receita.nome).all()
    produtos = Produto.query.filter_by(ativo=True).order_by(Produto.nome).all()
    materias = MateriaPrima.query.order_by(MateriaPrima.nome).all()

    # Historico: 30 dias
    desde = hoje_brt() - timedelta(days=30)
    q = (Desperdicio.query
         .filter(Desperdicio.data >= desde)
         .order_by(Desperdicio.data.desc(), Desperdicio.criado_em.desc()))
    if loja_filtro:
        q = q.filter(Desperdicio.loja_id == loja_filtro)
    registros = q.limit(200).all()

    return render_template('pedidos/desperdicio.html',
                           lojas=lojas, loja=loja, sel_loja=loja_filtro,
                           receitas=receitas, produtos=produtos, materias=materias,
                           registros=registros, hoje=hoje_brt().isoformat())


@pedidos_bp.route('/desperdicio/<int:id>/excluir', methods=['POST'])
@login_required
@admin_required
def desperdicio_excluir(id):
    """Exclui registro de desperdicio e estorna o estoque baixado."""
    desp = Desperdicio.query.get_or_404(id)
    loja_id = desp.loja_id

    filtro = {'loja_id': desp.loja_id}
    if desp.receita_id:
        filtro['receita_id'] = desp.receita_id
    elif desp.produto_id:
        filtro['produto_id'] = desp.produto_id
    elif desp.materia_prima_id:
        filtro['materia_prima_id'] = desp.materia_prima_id

    el = EstoqueLoja.query.filter_by(**filtro).first()
    if el:
        el.quantidade = (el.quantidade or 0) + desp.quantidade
        db.session.add(MovEstoqueLoja(
            estoque_loja_id=el.id, tipo='desperdicio_estorno',
            quantidade=desp.quantidade,
            referencia=f'Estorno desperdicio #{desp.id}',
            usuario_id=current_user.id,
        ))

    db.session.delete(desp)
    db.session.commit()
    flash('Desperdicio excluido e estoque estornado.', 'success')
    return redirect(url_for('pedidos.desperdicio', loja=loja_id))


# ── Vendas manuais loja (sem PDV API) + sugestao de pedido ──

@pedidos_bp.route('/lojas/<int:loja_id>/vendas-manuais', methods=['GET', 'POST'])
@login_required
@admin_required
def vendas_manuais(loja_id):
    """Lanca vendas manuais de uma loja (sem API PDV). Texto colado igual
    balanco. NAO baixa estoque — so registra pra previsao/sugestao."""
    from app.services import vendas_manuais as svc
    from app.models import VendaManualLoja
    loja = Loja.query.get_or_404(loja_id)
    parsed = None
    resultado = None

    if request.method == 'POST':
        acao = request.form.get('acao')
        texto = (request.form.get('texto') or '').strip()
        data_str = (request.form.get('data_venda') or '').strip()
        try:
            data_venda = date.fromisoformat(data_str) if data_str else hoje_brt()
        except ValueError:
            data_venda = hoje_brt()

        parseados = svc.parsear_lista(texto)
        resolvidos = svc.resolver_lista(parseados, loja_id) if parseados else []

        if acao == 'aplicar' and resolvidos:
            resultado = svc.aplicar_vendas_manuais(
                resolvidos, loja_id, data_venda, current_user,
            )
            flash(f'{len(resultado["aplicados"])} venda(s) lançada(s), '
                  f'{len(resultado["ignorados"])} ignorada(s).', 'success')
            return redirect(url_for('pedidos.vendas_manuais', loja=loja_id))
        parsed = {'data_venda': data_venda.isoformat(),
                  'texto': texto, 'itens': resolvidos}

    historico = (VendaManualLoja.query.filter_by(loja_id=loja_id)
                 .order_by(VendaManualLoja.data_venda.desc(),
                           VendaManualLoja.id.desc())
                 .limit(50).all())
    return render_template('pedidos/vendas_manuais.html', loja=loja,
                            parsed=parsed, resultado=resultado,
                            historico=historico, hoje=hoje_brt().isoformat())


@pedidos_bp.route('/lojas/<int:loja_id>/sugerir-pedido', methods=['GET', 'POST'])
@login_required
@admin_required
def sugerir_pedido(loja_id):
    """Mostra sugestao de pedido baseada em vendas (reais + manuais) +
    estoque atual. POST cria pedido com as qtds informadas."""
    from app.services import vendas_manuais as svc
    loja = Loja.query.get_or_404(loja_id)

    if request.method == 'POST':
        try:
            data_entrega = date.fromisoformat(request.form.get('data_entrega') or '')
        except ValueError:
            flash('Data invalida.', 'danger')
            return redirect(url_for('pedidos.sugerir_pedido', loja_id=loja_id))
        refs = request.form.getlist('item_ref[]')
        qtds = request.form.getlist('item_qtd[]')
        itens = []
        for i, ref in enumerate(refs):
            ref = (ref or '').strip()
            if not ref or ':' not in ref:
                continue
            tipo, _, sid = ref.partition(':')
            try:
                qtd = int(qtds[i])
            except (IndexError, ValueError):
                continue
            if qtd <= 0 or tipo not in ('receita', 'produto', 'mp') or not sid.isdigit():
                continue
            itens.append({'tipo': tipo, 'id': int(sid), 'quantidade': qtd})
        if not itens:
            flash('Nenhum item com quantidade > 0.', 'warning')
            return redirect(url_for('pedidos.sugerir_pedido', loja_id=loja_id))
        pedido = PedidoLoja(loja_id=loja_id, data_entrega=data_entrega,
                            criado_por=current_user.id, status='pendente')
        db.session.add(pedido)
        db.session.flush()
        for it in itens:
            pi = PedidoItem(pedido_id=pedido.id, quantidade=it['quantidade'])
            if it['tipo'] == 'receita':
                pi.receita_id = it['id']
            elif it['tipo'] == 'produto':
                pi.produto_id = it['id']
            else:
                pi.materia_prima_id = it['id']
            db.session.add(pi)
        db.session.commit()
        flash(f'Pedido #{pedido.id} criado a partir da sugestao.', 'success')
        return redirect(url_for('pedidos.detalhe', id=pedido.id))

    # Periodo: default = ultimos 14 dias
    di_str = request.args.get('inicio') or ''
    df_str = request.args.get('fim') or ''
    try:
        data_inicio = date.fromisoformat(di_str) if di_str else hoje_brt() - timedelta(days=14)
    except ValueError:
        data_inicio = hoje_brt() - timedelta(days=14)
    try:
        data_fim = date.fromisoformat(df_str) if df_str else hoje_brt()
    except ValueError:
        data_fim = hoje_brt()
    try:
        dias_cobertura = int(request.args.get('cobertura', 7))
    except ValueError:
        dias_cobertura = 7
    res = svc.sugerir_pedido(loja_id, data_inicio=data_inicio,
                              data_fim=data_fim,
                              dias_cobertura=dias_cobertura)
    sugestao = res.get('itens', [])
    aviso_vnda = res.get('aviso_vnda')
    dias_periodo = (data_fim - data_inicio).days + 1
    return render_template('pedidos/sugerir_pedido.html', loja=loja,
                            sugestao=sugestao,
                            aviso_vnda=aviso_vnda,
                            data_inicio=data_inicio.isoformat(),
                            data_fim=data_fim.isoformat(),
                            dias_periodo=dias_periodo,
                            dias_cobertura=dias_cobertura,
                            amanha=(hoje_brt() + timedelta(days=1)).isoformat())


@pedidos_bp.route('/lojas/<int:loja_id>/vendas-manuais/template.xlsx')
@login_required
@admin_required
def vendas_manuais_template(loja_id):
    """Download da planilha modelo pra preencher e fazer upload depois."""
    from app.services import vendas_manuais as svc
    from flask import send_file
    import io
    loja = Loja.query.get_or_404(loja_id)
    blob = svc.gerar_template_xlsx(loja)
    nome = f'vendas_{loja.nome.lower().replace(" ", "_")}_modelo.xlsx'
    return send_file(io.BytesIO(blob),
                      as_attachment=True, download_name=nome,
                      mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@pedidos_bp.route('/lojas/<int:loja_id>/vendas-manuais/upload', methods=['POST'])
@login_required
@admin_required
def vendas_manuais_upload(loja_id):
    """Recebe upload de xlsx + aplica em lote. Linhas com erro vao pra
    ignorados (mostra no flash)."""
    from app.services import vendas_manuais as svc
    loja = Loja.query.get_or_404(loja_id)
    f = request.files.get('planilha')
    if not f or not f.filename:
        flash('Selecione um arquivo .xlsx.', 'danger')
        return redirect(url_for('pedidos.vendas_manuais', loja_id=loja_id))
    if not f.filename.lower().endswith('.xlsx'):
        flash('So aceita arquivo .xlsx (Excel).', 'danger')
        return redirect(url_for('pedidos.vendas_manuais', loja_id=loja_id))

    parseados = svc.parsear_xlsx(f, loja_id)
    resultado = svc.aplicar_vendas_xlsx(parseados, loja_id, current_user)

    n_ok = len(resultado['aplicados'])
    n_ign = len(resultado['ignorados'])
    n_datas = len(resultado['datas_unicas'])
    if n_ok:
        flash(f'{n_ok} venda(s) lançadas em {n_datas} data(s) distinta(s). '
              f'{n_ign} linha(s) ignoradas.', 'success')
    else:
        flash(f'Nenhuma venda lançada. {n_ign} linha(s) ignoradas. '
              'Verifique formato (Data, Produto, Qtd) e nomes do catálogo.',
              'warning')

    # Salva ignorados na session pra mostrar na tela depois (caso queira)
    if resultado['ignorados']:
        from flask import session
        session['vendas_manuais_ultimos_ignorados'] = resultado['ignorados'][:50]
    return redirect(url_for('pedidos.vendas_manuais', loja_id=loja_id))
