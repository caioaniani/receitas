"""Rotas da Lista de Compras semanal."""
from datetime import datetime, timedelta

from flask import abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.blueprints.lista_compras import lista_compras_bp
from app.decorators import owner_required
from app.extensions import db
from app.models import ItemListaCompras, ListaComprasItemQtd, ListaComprasSemana, Loja
from app.services import lista_compras_svc as svc
from app.services import permissoes


def _pode_preencher():
    """admin/owner sempre; demais checa capacidade 'web_lista_compras'."""
    if current_user.is_admin():
        return True
    return permissoes.pode(getattr(current_user, 'papel', '') or '',
                           'web_lista_compras')


def _loja_alvo():
    """Loja em que o gerente vai preencher. Admin pode escolher via ?loja_id;
    gerente comum eh forçado pra propria loja."""
    if current_user.is_admin():
        lid = request.args.get('loja_id', type=int)
        if lid:
            return Loja.query.get(lid)
        # admin sem escolha: pega a primeira loja ativa
        return Loja.query.filter_by(ativa=True).order_by(Loja.nome).first()
    if current_user.loja_id:
        return Loja.query.get(current_user.loja_id)
    return None


@lista_compras_bp.route('/')
@login_required
def index():
    """Tela do gerente da loja: preencher 'quanto tenho' da semana corrente."""
    if not _pode_preencher():
        abort(403)
    loja = _loja_alvo()
    if not loja:
        flash('Sua conta não está vinculada a uma loja.', 'warning')
        return redirect(url_for('main.index'))

    semana = svc.obter_ou_criar_semana(loja.id, criado_por_id=current_user.id)
    grupos = svc.itens_da_loja_agrupados(loja.id)
    qtds = svc.quantidades_por_item(semana)
    historico = svc.historico_anterior(loja.id, semana.data_semana_inicio)

    lojas_admin = []
    if current_user.is_admin():
        lojas_admin = (Loja.query.filter_by(ativa=True)
                       .order_by(Loja.nome).all())

    return render_template('lista_compras/preencher.html',
                           loja=loja, semana=semana, grupos=grupos,
                           qtds=qtds, historico=historico,
                           lojas_admin=lojas_admin,
                           pode_consolidar=current_user.is_admin())


@lista_compras_bp.route('/salvar.json', methods=['POST'])
@login_required
def salvar_json():
    """Auto-save por item: {semana_id, item_id, tenho}."""
    if not _pode_preencher():
        abort(403)
    try:
        semana_id = int(request.form.get('semana_id') or 0)
        item_id = int(request.form.get('item_id') or 0)
        tenho = request.form.get('tenho', '0')
    except (TypeError, ValueError):
        return jsonify(ok=False, erro='parametros invalidos'), 400

    semana = ListaComprasSemana.query.get_or_404(semana_id)
    # gerente comum so pode salvar na propria loja
    if not current_user.is_admin() and semana.loja_id != current_user.loja_id:
        abort(403)
    ok, erro = svc.salvar_tenho(semana, item_id, tenho)
    if not ok:
        return jsonify(ok=False, erro=erro), 409
    return jsonify(ok=True)


@lista_compras_bp.route('/enviar', methods=['POST'])
@login_required
def enviar():
    """Gerente termina o preenchimento e envia pro gerente geral."""
    if not _pode_preencher():
        abort(403)
    semana_id = request.form.get('semana_id', type=int)
    semana = ListaComprasSemana.query.get_or_404(semana_id)
    if not current_user.is_admin() and semana.loja_id != current_user.loja_id:
        abort(403)
    ok, erro = svc.enviar_semana(semana, current_user.id)
    if ok:
        flash('Lista enviada pro gerente geral.', 'success')
    else:
        flash(f'Não foi possível enviar: {erro}', 'warning')
    return redirect(url_for('lista_compras.index'))


# ── Tela do gerente geral (admin/owner) ────────────────────────────────

@lista_compras_bp.route('/consolidada')
@login_required
@owner_required
def consolidada():
    """Visao de gerente geral: 4 lojas lado a lado, decisao de quanto pedir."""
    data = request.args.get('semana')
    if data:
        try:
            data_alvo = datetime.strptime(data, '%Y-%m-%d').date()
            data_alvo = svc.domingo_da_semana(data_alvo)
        except ValueError:
            data_alvo = svc.domingo_da_semana()
    else:
        data_alvo = svc.domingo_da_semana()

    semana_anterior = data_alvo - timedelta(days=7)
    semana_proxima = data_alvo + timedelta(days=7)

    lojas = (Loja.query.filter_by(ativa=True).order_by(Loja.nome).all())
    # Carrega semanas (loja → ListaComprasSemana ou None) + qtds da semana atual.
    por_loja = {}
    for loja in lojas:
        sem = (ListaComprasSemana.query
               .filter_by(loja_id=loja.id, data_semana_inicio=data_alvo).first())
        qtds = {q.item_id: q for q in sem.quantidades} if sem else {}
        por_loja[loja.id] = {
            'loja': loja, 'semana': sem, 'qtds': qtds,
            'itens': (ItemListaCompras.query
                      .filter_by(loja_id=loja.id, ativo=True)
                      .order_by(ItemListaCompras.grupo, ItemListaCompras.ordem,
                                ItemListaCompras.nome_item).all()),
        }
    return render_template('lista_compras/consolidada.html',
                           lojas=lojas, por_loja=por_loja, data_alvo=data_alvo,
                           semana_anterior=semana_anterior,
                           semana_proxima=semana_proxima)


@lista_compras_bp.route('/consolidar.json', methods=['POST'])
@login_required
@owner_required
def consolidar_json():
    """Salva 'pedido' e/ou 'sobrou' de um item — gerente geral."""
    try:
        semana_id = int(request.form.get('semana_id') or 0)
        item_id = int(request.form.get('item_id') or 0)
    except (TypeError, ValueError):
        return jsonify(ok=False, erro='parametros invalidos'), 400
    pedido = request.form.get('pedido')
    sobrou = request.form.get('sobrou')
    semana = ListaComprasSemana.query.get_or_404(semana_id)
    ok, erro = svc.salvar_pedido_sobrou(
        semana, item_id,
        pedido=pedido if pedido is not None else None,
        sobrou=sobrou if sobrou is not None else None,
    )
    if not ok:
        return jsonify(ok=False, erro=erro), 409
    return jsonify(ok=True)


@lista_compras_bp.route('/fechar', methods=['POST'])
@login_required
@owner_required
def fechar():
    semana_id = request.form.get('semana_id', type=int)
    semana = ListaComprasSemana.query.get_or_404(semana_id)
    ok, erro = svc.fechar_semana(semana, current_user.id)
    flash('Semana marcada como comprada.' if ok else f'Erro: {erro}',
          'success' if ok else 'warning')
    voltar = request.form.get('voltar') or url_for('lista_compras.consolidada')
    return redirect(voltar)


# ── Catalogo (CRUD admin) ──────────────────────────────────────────────

@lista_compras_bp.route('/catalogo', methods=['GET', 'POST'])
@login_required
@owner_required
def catalogo():
    if request.method == 'POST':
        acao = request.form.get('acao')
        if acao == 'add':
            try:
                loja_id = int(request.form.get('loja_id'))
            except (TypeError, ValueError):
                flash('Loja inválida.', 'warning')
                return redirect(url_for('lista_compras.catalogo'))
            grupo = (request.form.get('grupo') or '').strip().upper()
            nome = (request.form.get('nome_item') or '').strip()
            if not grupo or not nome:
                flash('Grupo e nome são obrigatórios.', 'warning')
                return redirect(url_for('lista_compras.catalogo',
                                        loja_id=loja_id))
            existe = (ItemListaCompras.query
                      .filter_by(loja_id=loja_id, grupo=grupo, nome_item=nome)
                      .first())
            if existe:
                if not existe.ativo:
                    existe.ativo = True
                    db.session.commit()
                    flash(f'Item reativado: {nome}', 'success')
                else:
                    flash('Item já existe nesse grupo.', 'warning')
            else:
                # ordem: ultima do grupo + 1
                ult = (db.session.query(db.func.max(ItemListaCompras.ordem))
                       .filter_by(loja_id=loja_id, grupo=grupo).scalar() or 0)
                db.session.add(ItemListaCompras(
                    loja_id=loja_id, grupo=grupo, nome_item=nome,
                    ordem=ult + 1, ativo=True))
                db.session.commit()
                flash(f'Item adicionado: {nome}', 'success')
            return redirect(url_for('lista_compras.catalogo', loja_id=loja_id))
        elif acao == 'toggle':
            item_id = request.form.get('item_id', type=int)
            it = ItemListaCompras.query.get_or_404(item_id)
            it.ativo = not it.ativo
            db.session.commit()
            return redirect(url_for('lista_compras.catalogo', loja_id=it.loja_id))
        elif acao == 'remover':
            item_id = request.form.get('item_id', type=int)
            it = ItemListaCompras.query.get_or_404(item_id)
            # remove se nao tiver quantidade vinculada (evita perder historico)
            tem_uso = (ListaComprasItemQtd.query
                       .filter_by(item_id=item_id).first()) is not None
            loja_id = it.loja_id
            if tem_uso:
                it.ativo = False
                db.session.commit()
                flash(f'Item tem histórico — desativado (não excluído): {it.nome_item}',
                      'info')
            else:
                db.session.delete(it)
                db.session.commit()
                flash(f'Item removido: {it.nome_item}', 'success')
            return redirect(url_for('lista_compras.catalogo', loja_id=loja_id))

    sel_loja = request.args.get('loja_id', type=int)
    lojas = Loja.query.filter_by(ativa=True).order_by(Loja.nome).all()
    itens_por_grupo = []
    loja_atual = None
    if sel_loja:
        loja_atual = Loja.query.get(sel_loja)
        itens_por_grupo = svc.itens_da_loja_agrupados(sel_loja)
        # tb mostra inativos pra reativar
        from collections import defaultdict
        ativos_set = {(g, i.id) for g, lista in itens_por_grupo for i in lista}
        inativos = (ItemListaCompras.query
                    .filter_by(loja_id=sel_loja, ativo=False)
                    .order_by(ItemListaCompras.grupo,
                              ItemListaCompras.nome_item).all())
        # Re-organiza pra inluir inativos no fim de cada grupo
        if inativos:
            mapa = defaultdict(list)
            for grupo, lista in itens_por_grupo:
                mapa[grupo] = list(lista)
            for i in inativos:
                mapa[i.grupo].append(i)
            itens_por_grupo = [(g, mapa[g]) for g in mapa]

    return render_template('lista_compras/catalogo.html',
                           lojas=lojas, loja_atual=loja_atual,
                           itens_por_grupo=itens_por_grupo, sel_loja=sel_loja)
