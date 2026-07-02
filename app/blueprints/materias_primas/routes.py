
from flask import flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.blueprints.materias_primas import materias_primas_bp
from app.decorators import admin_required, catalogo_required
from app.extensions import db
from app.models import AlertaEstoque, MateriaPrima, MovimentacaoEstoque, ReceitaIngrediente


@materias_primas_bp.route('/')
@login_required
def banco():
    materias = MateriaPrima.query.order_by(MateriaPrima.id).all()
    return render_template('materias_primas/banco.html', materias=materias)


@materias_primas_bp.route('/salvar', methods=['POST'])
@login_required
@admin_required
def salvar():
    ids = request.form.getlist('mp_id[]')
    nomes = request.form.getlist('nome[]')
    unidades = request.form.getlist('unidade[]')
    custos = request.form.getlist('custo_por_kg[]')
    pesos_unidade = request.form.getlist('peso_unidade[]')
    fornecedores = request.form.getlist('fornecedor[]')
    observacoes_list = request.form.getlist('observacoes[]')
    # Checkbox "Loja pede" (fora dos arrays: checkbox desmarcado nao e enviado,
    # entao vai por VALOR = id da MP; ausente = desmarcado).
    sugerir_loja_ids = set(request.form.getlist('sugerir_loja_ids'))

    def _parse_peso(idx, unidade):
        if unidade != 'un' or idx >= len(pesos_unidade):
            return None
        raw = (pesos_unidade[idx] or '').replace(',', '.').strip()
        if not raw:
            return None
        try:
            v = float(raw)
            return v if v > 0 else None
        except ValueError:
            return None

    for i in range(len(nomes)):
        nome = nomes[i].strip()
        if not nome:
            continue

        custo = custos[i].replace(',', '.')
        mp_id = int(ids[i]) if ids[i] else None

        if mp_id:
            mp = MateriaPrima.query.get(mp_id)
            if mp:
                mp.nome = nome
                mp.unidade = unidades[i]
                mp.custo_por_kg = float(custo)
                mp.peso_unidade = _parse_peso(i, unidades[i])
                mp.fornecedor = fornecedores[i].strip() or None
                mp.observacoes = observacoes_list[i].strip() or None
                mp.sugerir_pedido_loja = str(mp_id) in sugerir_loja_ids
        else:
            mp = MateriaPrima(
                nome=nome,
                unidade=unidades[i],
                custo_por_kg=float(custo),
                peso_unidade=_parse_peso(i, unidades[i]),
                fornecedor=fornecedores[i].strip() or None,
                observacoes=observacoes_list[i].strip() or None,
            )
            db.session.add(mp)

    db.session.commit()
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify(success=True)
    flash('Banco de matérias-primas salvo com sucesso!', 'success')
    return redirect(url_for('materias_primas.banco'))


def _vinculos_mp(mp):
    """O que ainda referencia a MP (bloqueia exclusão). Retorna [(rótulo, n)].

    A MP ganhou FKs por todo o sistema (estoque de loja, pedidos, cestas,
    mapeamentos, financeiro...) — o delete cru estourava 500 com IntegrityError
    quando havia QUALQUER histórico (só o uso em receitas era checado)."""
    from app.models import (
        Desperdicio,
        EstoqueLoja,
        MovimentacaoEstoque,
        PedidoItem,
        ProdutoItem,
        VendaManualLoja,
        VendaMapa,
    )
    grupos = [
        ('pedido(s) de loja', PedidoItem),
        ('linha(s) de estoque de loja', EstoqueLoja),
        ('componente(s) de cesta/produto', ProdutoItem),
        ('mapeamento(s) de PDV/loja', VendaMapa),
        ('movimentação(ões) de estoque', MovimentacaoEstoque),
        ('registro(s) de desperdício', Desperdicio),
        ('venda(s) manual(is)', VendaManualLoja),
    ]
    return [(rotulo, n) for rotulo, modelo in grupos
            if (n := modelo.query.filter_by(materia_prima_id=mp.id).count())]


@materias_primas_bp.route('/excluir/<int:id>', methods=['POST'])
@login_required
@admin_required
def excluir(id):
    from sqlalchemy.exc import IntegrityError
    mp = MateriaPrima.query.get_or_404(id)
    uso = ReceitaIngrediente.query.filter_by(ingrediente_nome=mp.nome).first()
    if uso:
        flash(f'Não é possível excluir "{mp.nome}": usado em receitas.', 'danger')
        return redirect(url_for('materias_primas.banco'))
    vinculos = _vinculos_mp(mp)
    if vinculos:
        detalhe = ', '.join(f'{n} {rotulo}' for rotulo, n in vinculos)
        flash(f'Não é possível excluir "{mp.nome}": há {detalhe} apontando '
              'pra ela. Histórico não se apaga — se o cadastro está errado ou '
              'duplicado, corrija/renomeie a MP em vez de excluir.', 'danger')
        return redirect(url_for('materias_primas.banco'))
    nome = mp.nome
    # Belt-and-braces: alguma FK fora da lista (financeiro, débitos de fração,
    # alertas...) ainda pode segurar — aborta limpo em vez de 500.
    try:
        db.session.delete(mp)
        db.session.commit()
        flash(f'"{nome}" excluído com sucesso!', 'success')
    except IntegrityError:
        db.session.rollback()
        flash(f'Não é possível excluir "{nome}": há registros históricos '
              '(financeiro/estoque) vinculados a ela.', 'danger')
    return redirect(url_for('materias_primas.banco'))


# ── Controle de Estoque ──

@materias_primas_bp.route('/estoque')
@login_required
@catalogo_required
def estoque():
    materias = MateriaPrima.query.order_by(MateriaPrima.nome).all()
    alertas = {a.materia_prima_id: a.estoque_minimo for a in AlertaEstoque.query.all()}
    return render_template('materias_primas/estoque.html', materias=materias, alertas=alertas)


@materias_primas_bp.route('/estoque/ocr-nota', methods=['POST'])
@login_required
@catalogo_required
def estoque_ocr_nota():
    """Recebe upload de imagem de nota/cupom e devolve itens extraidos +
    sugestao de match com MPs cadastradas. JSON pra ser consumido por JS
    no /estoque ou na pagina de entrada."""
    from app.services.copilot import _resolver_mp
    from app.services.ocr_nota import extrair_itens_nota
    f = request.files.get('imagem')
    if not f or not f.filename:
        return jsonify(ok=False, erro='sem_imagem'), 400
    mimetype = f.mimetype or 'image/jpeg'
    if not mimetype.startswith('image/'):
        return jsonify(ok=False, erro='arquivo_nao_eh_imagem'), 400
    data = f.read()
    if len(data) > 8 * 1024 * 1024:
        return jsonify(ok=False, erro='imagem_muito_grande'), 400
    dados = extrair_itens_nota(data, mimetype=mimetype)
    if dados.get('erro'):
        return jsonify(ok=False, erro=dados['erro'],
                       raw=dados.get('raw', '')), 422
    # Enriquece cada item com sugestao de match no cadastro de MPs.
    for it in dados.get('itens', []) or []:
        nome = (it.get('nome') or '').strip()
        if nome:
            matches = _resolver_mp(nome)
            it['matches'] = matches
            it['resolvido'] = matches[0] if matches else None
    return jsonify(ok=True, dados=dados)


@materias_primas_bp.route('/estoque/entrada', methods=['POST'])
@login_required
@catalogo_required
def estoque_entrada():
    mp_id = int(request.form['mp_id'])
    quantidade = float(request.form['quantidade'].replace(',', '.'))
    preco = request.form.get('preco_unitario', '').replace(',', '.')
    preco_unitario = float(preco) if preco else None
    referencia = request.form.get('referencia', '').strip()
    atualizar_custo = request.form.get('atualizar_custo') == '1'

    mp = MateriaPrima.query.get_or_404(mp_id)

    mov = MovimentacaoEstoque(
        materia_prima_id=mp_id,
        tipo='entrada',
        quantidade=quantidade,
        preco_unitario=preco_unitario,
        referencia=referencia or None,
        usuario_id=current_user.id,
    )
    db.session.add(mov)
    mp.estoque_atual = (mp.estoque_atual or 0) + quantidade

    if atualizar_custo and preco_unitario:
        mp.custo_por_kg = preco_unitario

    db.session.commit()
    flash(f'Entrada de {quantidade} {mp.unidade} de "{mp.nome}" registrada.', 'success')
    return redirect(url_for('materias_primas.estoque'))


@materias_primas_bp.route('/estoque/saida', methods=['POST'])
@login_required
@catalogo_required
def estoque_saida():
    mp_id = int(request.form['mp_id'])
    quantidade = float(request.form['quantidade'].replace(',', '.'))
    referencia = request.form.get('referencia', '').strip()

    mp = MateriaPrima.query.get_or_404(mp_id)

    mov = MovimentacaoEstoque(
        materia_prima_id=mp_id,
        tipo='saida',
        quantidade=quantidade,
        referencia=referencia or None,
        usuario_id=current_user.id,
    )
    db.session.add(mov)
    mp.estoque_atual = max(0, (mp.estoque_atual or 0) - quantidade)

    db.session.commit()
    flash(f'Saída de {quantidade} {mp.unidade} de "{mp.nome}" registrada.', 'success')
    return redirect(url_for('materias_primas.estoque'))


@materias_primas_bp.route('/estoque/<int:mp_id>/historico')
@login_required
@catalogo_required
def estoque_historico(mp_id):
    mp = MateriaPrima.query.get_or_404(mp_id)
    movimentacoes = MovimentacaoEstoque.query.filter_by(
        materia_prima_id=mp_id
    ).order_by(MovimentacaoEstoque.data.desc()).limit(100).all()
    return render_template('materias_primas/historico_mp.html', mp=mp, movimentacoes=movimentacoes)


@materias_primas_bp.route('/estoque/alertas', methods=['POST'])
@login_required
@catalogo_required
def estoque_alertas():
    mp_ids = request.form.getlist('mp_id[]')
    minimos = request.form.getlist('estoque_minimo[]')

    for i, mp_id in enumerate(mp_ids):
        valor = minimos[i].replace(',', '.').strip()
        if not valor or float(valor) <= 0:
            AlertaEstoque.query.filter_by(materia_prima_id=int(mp_id)).delete()
            continue
        alerta = AlertaEstoque.query.filter_by(materia_prima_id=int(mp_id)).first()
        if alerta:
            alerta.estoque_minimo = float(valor)
        else:
            alerta = AlertaEstoque(materia_prima_id=int(mp_id), estoque_minimo=float(valor))
            db.session.add(alerta)

    db.session.commit()
    flash('Alertas de estoque atualizados.', 'success')
    return redirect(url_for('materias_primas.estoque'))
