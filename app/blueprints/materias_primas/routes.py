
from flask import flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import or_

from app.blueprints.materias_primas import materias_primas_bp
from app.decorators import admin_required, catalogo_required
from app.extensions import db
from app.models import AlertaEstoque, MateriaPrima, MovimentacaoEstoque, ReceitaIngrediente
from app.ui_v2 import ui_v2_ativo
from app.utils import SUB_RECEITA_TIPOS


@materias_primas_bp.route('/')
@login_required
def banco():
    query = MateriaPrima.ativas()
    busca = (request.args.get('q') or '').strip()
    if busca:
        termo = f'%{busca}%'
        query = query.filter(or_(MateriaPrima.nome.ilike(termo),
                                 MateriaPrima.fornecedor.ilike(termo)))

    if ui_v2_ativo():
        page = request.args.get('page', 1, type=int)
        paginacao = db.paginate(
            query.order_by(MateriaPrima.nome),
            page=max(page, 1),
            per_page=30,
            error_out=False,
        )
        arquivadas = (MateriaPrima.query
                      .filter(MateriaPrima.arquivada_em.isnot(None))
                      .order_by(MateriaPrima.nome).all())
        return render_template(
            'materias_primas/banco_v2.html',
            materias=paginacao.items,
            paginacao=paginacao,
            busca=busca,
            arquivadas=arquivadas,
        )

    materias = query.order_by(MateriaPrima.id).all()
    arquivadas = (MateriaPrima.query
                  .filter(MateriaPrima.arquivada_em.isnot(None))
                  .order_by(MateriaPrima.nome).all())
    return render_template('materias_primas/banco.html', materias=materias,
                           arquivadas=arquivadas)


@materias_primas_bp.route('/arquivar/<int:id>', methods=['POST'])
@login_required
@admin_required
def arquivar(id):
    """Arquiva/desarquiva MP. Arquivada = fora de circulação (autocompletes,
    matchers, pickers, telas de pedido) — ninguém conecta nada nela de novo.
    Histórico preservado; reversível aqui mesmo. É o destino da MP que virou
    receita mas carrega histórico inapagável (movimentações/preço)."""
    from app.utils import agora
    mp = MateriaPrima.query.get_or_404(id)
    if mp.arquivada_em:
        mp.arquivada_em = None
        mp.arquivada_por_id = None
        db.session.commit()
        flash(f'"{mp.nome}" desarquivada — voltou pra circulação.', 'success')
    else:
        mp.arquivada_em = agora()
        mp.arquivada_por_id = current_user.id
        # Fora de circulação também nas telas de pedido de loja.
        mp.sugerir_pedido_loja = False
        db.session.commit()
        flash(f'"{mp.nome}" arquivada: some dos autocompletes, matchers e '
              'telas — ninguém conecta mais nada nela. O histórico fica. '
              'Dá pra desarquivar aqui no banco de MPs.', 'success')
    return redirect(url_for('materias_primas.banco'))


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
    # Caixa/piso do pedido de loja (arrays alinhados com as linhas).
    lotes_pedido = request.form.getlist('lote_pedido[]')
    minimos_pedido = request.form.getlist('minimo_pedido[]')

    def _parse_int_opt(lista, idx):
        if idx >= len(lista):
            return None
        raw = (lista[idx] or '').strip()
        try:
            v = int(raw)
            return v if v > 0 else None
        except ValueError:
            return None

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
                mp.lote_pedido = _parse_int_opt(lotes_pedido, i)
                mp.minimo_pedido = _parse_int_opt(minimos_pedido, i)
        else:
            mp = MateriaPrima(
                nome=nome,
                unidade=unidades[i],
                custo_por_kg=float(custo),
                peso_unidade=_parse_peso(i, unidades[i]),
                fornecedor=fornecedores[i].strip() or None,
                observacoes=observacoes_list[i].strip() or None,
                lote_pedido=_parse_int_opt(lotes_pedido, i),
                minimo_pedido=_parse_int_opt(minimos_pedido, i),
                sugerir_pedido_loja=f'novo-{i}' in sugerir_loja_ids,
            )
            db.session.add(mp)

    db.session.commit()
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify(success=True)
    flash('Banco de matérias-primas salvo com sucesso!', 'success')
    busca = (request.form.get('q') or '').strip()
    page = request.form.get('page', type=int)
    return redirect(url_for('materias_primas.banco',
                            q=busca or None, page=page or None))


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


@materias_primas_bp.route('/<int:id>/transferir', methods=['GET', 'POST'])
@login_required
@admin_required
def transferir(id):
    """Transfere pra uma RECEITA os vínculos de uma MP que na verdade é
    PRODUZIDA (ex: 'Geleia Artesanal de Morango' cadastrada como MP) — o
    espelho do transferir receita→MP da ficha de receitas.

    Movem: pedidos de loja, vendas manuais, desperdício, estoque de loja
    (funde com a linha da receita), cestas, mapeamentos e o uso como
    ingrediente em fichas (tipo mp→receita, custo passa a seguir a ficha).
    FICAM (sem equivalente em receita): movimentações de estoque de MP,
    histórico/variação de preço e vínculos do financeiro — histórico não se
    apaga; com eles zerados a exclusão libera."""
    from sqlalchemy import func

    from app.models import (
        Desperdicio,
        EstoqueLoja,
        MovEstoqueLoja,
        PedidoItem,
        ProdutoItem,
        Receita,
        VendaManualLoja,
        VendaMapa,
    )
    mp = MateriaPrima.query.get_or_404(id)

    if request.method == 'GET':
        return render_template('materias_primas/transferir.html', mp=mp,
                               vinculos=_vinculos_mp(mp))

    nome_destino = (request.form.get('destino') or '').strip()
    destino = (Receita.query
               .filter(func.lower(Receita.nome) == nome_destino.lower())
               .first()) if nome_destino else None
    if not destino:
        flash(f'Receita "{nome_destino}" não encontrada — use o nome exato '
              '(o campo autocompleta). Se a ficha ainda não existe, crie '
              'primeiro em Receitas.', 'danger')
        return redirect(url_for('materias_primas.transferir', id=mp.id))

    movidos = {}

    def _conta(chave, n):
        if n:
            movidos[chave] = movidos.get(chave, 0) + n

    swap = {'materia_prima_id': None, 'receita_id': destino.id}
    for chave, modelo in (('pedidos', PedidoItem),
                          ('vendas_manuais', VendaManualLoja),
                          ('desperdicio', Desperdicio)):
        _conta(chave, modelo.query.filter_by(materia_prima_id=mp.id)
               .update(dict(swap), synchronize_session=False))

    _conta('cestas', ProdutoItem.query.filter_by(materia_prima_id=mp.id)
           .update({**swap, 'tipo': 'receita', 'item_nome': destino.nome},
                   synchronize_session=False))

    # Ingrediente em fichas: tipo mp -> receita (o custo passa a seguir a
    # FICHA da receita, não mais o custo/kg da MP).
    _conta('ingrediente_em_fichas', ReceitaIngrediente.query
           .filter(ReceitaIngrediente.tipo == 'mp',
                   ReceitaIngrediente.ingrediente_nome == mp.nome)
           .update({'tipo': 'receita', 'ingrediente_nome': destino.nome,
                    'sub_receita_id': destino.id}, synchronize_session=False))

    _conta('mapeamentos', VendaMapa.query.filter_by(materia_prima_id=mp.id)
           .update(dict(swap), synchronize_session=False))

    # Estoque de loja: funde com a linha da receita (mesma loja/estado);
    # movimentações reapontadas ANTES de apagar a linha da origem.
    from app.services.estoque_helpers import serializar_lojas
    _els_fusao = EstoqueLoja.query.filter_by(materia_prima_id=mp.id).all()
    serializar_lojas({e.loja_id for e in _els_fusao})  # lock ascendente multi-loja
    for e in _els_fusao:
        alvo = EstoqueLoja.query.filter_by(
            receita_id=destino.id, loja_id=e.loja_id, estado=e.estado).first()
        if alvo:
            alvo.quantidade = (alvo.quantidade or 0) + (e.quantidade or 0)
            MovEstoqueLoja.query.filter_by(estoque_loja_id=e.id).update(
                {'estoque_loja_id': alvo.id}, synchronize_session=False)
            db.session.delete(e)
        else:
            e.materia_prima_id = None
            e.receita_id = destino.id
        _conta('estoque_loja', 1)

    db.session.commit()
    ficaram = _vinculos_mp(mp)
    detalhe = ', '.join(f'{n} {rotulo}' for rotulo, n in ficaram)
    total = sum(movidos.values())
    msg = (f'{total} vínculo(s) transferido(s) de "{mp.nome}" pra receita '
           f'"{destino.nome}".')
    if ficaram:
        msg += (f' Ficaram como histórico: {detalhe} — a MP não pode ser '
                'excluída enquanto existirem.')
    else:
        msg += ' A MP ficou livre — dá pra excluir no banco de MPs.'
    flash(msg, 'success' if not ficaram else 'warning')
    return redirect(url_for('materias_primas.banco'))


@materias_primas_bp.route('/excluir/<int:id>', methods=['POST'])
@login_required
@admin_required
def excluir(id):
    from sqlalchemy.exc import IntegrityError
    mp = MateriaPrima.query.get_or_404(id)
    # So ingrediente de tipo MP bloqueia — uma RECEITA homonima usada como
    # ingrediente (sub-receita 'receita'/'sub_pct', caso pos-transferencia
    # MP->receita, mesmo nome) nao e uso desta MP.
    uso = (ReceitaIngrediente.query
           .filter(ReceitaIngrediente.ingrediente_nome == mp.nome,
                   ReceitaIngrediente.tipo.notin_(SUB_RECEITA_TIPOS))
           .first())
    if uso:
        flash(f'Não é possível excluir "{mp.nome}": usado em receitas.', 'danger')
        return redirect(url_for('materias_primas.banco'))
    vinculos = _vinculos_mp(mp)
    if vinculos:
        detalhe = ', '.join(f'{n} {rotulo}' for rotulo, n in vinculos)
        flash(f'Não é possível excluir "{mp.nome}": há {detalhe} apontando '
              'pra ela. Histórico não se apaga — se ela na verdade é uma '
              'RECEITA (produzida), use o botão → da linha pra transferir os '
              'vínculos; o que sobrar de histórico, resolva com ARQUIVAR '
              '(botão caixinha): ela sai de circulação e ninguém conecta '
              'mais nada nela.', 'danger')
        return redirect(url_for('materias_primas.banco'))
    nome = mp.nome
    # Alerta de estoque mínimo é CONFIG (não histórico) — vai junto da MP.
    AlertaEstoque.query.filter_by(materia_prima_id=mp.id).delete()
    # Belt-and-braces: alguma FK fora da lista (financeiro, débitos de fração,
    # histórico de preço...) ainda pode segurar — aborta limpo em vez de 500.
    try:
        db.session.delete(mp)
        db.session.commit()
        flash(f'"{nome}" excluído com sucesso!', 'success')
    except IntegrityError:
        db.session.rollback()
        flash(f'Não é possível excluir "{nome}": há registros históricos '
              '(financeiro/estoque) vinculados a ela. Use ARQUIVAR pra '
              'tirá-la de circulação preservando o histórico.', 'danger')
    return redirect(url_for('materias_primas.banco'))


# ── Controle de Estoque ──

@materias_primas_bp.route('/estoque')
@login_required
@catalogo_required
def estoque():
    materias = MateriaPrima.ativas().order_by(MateriaPrima.nome).all()
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
