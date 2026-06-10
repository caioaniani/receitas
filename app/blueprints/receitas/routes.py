import difflib
import io
import os
import zipfile

from flask import (
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from app.blueprints.receitas import receitas_bp
from app.decorators import admin_required
from app.extensions import db
from app.models import Atribuicao, MateriaPrima, Produto, Receita, ReceitaIngrediente
from app.services.custos import calcular_custos_receitas
from app.utils import agora, dividir_etapas_preparo, parse_float_br


@receitas_bp.route('/<int:id>')
@login_required
def ficha(id):
    receita = Receita.query.get_or_404(id)

    # Funcionário só acessa fichas atribuídas
    if not current_user.is_admin():
        atribuida = Atribuicao.query.filter_by(
            receita_id=id, usuario_id=current_user.id
        ).first()
        if not atribuida:
            abort(403)

    mp_dict = {mp.nome: mp for mp in MateriaPrima.query.all()}

    resultado = calcular_custos_receitas()

    # Lista de usuarios pra dropdown "Atribuir" (admin so ve)
    from app.models import Usuario
    funcionarios = []
    if current_user.is_admin():
        funcionarios = (Usuario.query
                        .filter(Usuario.papel != 'admin')
                        .order_by(Usuario.nome)
                        .all())

    return render_template('receitas/ficha.html', receita=receita, mp_dict=mp_dict,
                           funcionarios=funcionarios,
                           etapas_preparo=dividir_etapas_preparo(receita.modo_preparo),
                           receita_custos=resultado['custos'],
                           receita_pesos=resultado['pesos'])


@receitas_bp.route('/padeiro')
@login_required
def padeiro_lista():
    receitas = (Receita.query.filter(Receita.arquivada_em.is_(None))
                .order_by(Receita.categoria, Receita.nome).all())
    categorias = {}
    for r in receitas:
        cat = r.categoria or 'Outros'
        categorias.setdefault(cat, []).append(r)
    arquivadas = (Receita.query.filter(Receita.arquivada_em.isnot(None))
                  .order_by(Receita.nome).all())
    return render_template('receitas/padeiro_lista.html', categorias=categorias,
                           arquivadas=arquivadas)


@receitas_bp.route('/familias', methods=['GET', 'POST'])
@login_required
@admin_required
def familias():
    """Tela bulk pra atribuir Receita.familia em lote.

    GET: lista todas as receitas com dropdown de familia, agrupadas por
    categoria. Botoes "Aplicar X a todos da categoria" pra rapido.
    POST: salva todas as familias enviadas (input name=`familia_<id>`).
    Familia vazia = limpa (NULL).
    """
    if request.method == 'POST':
        atualizados = 0
        permitidos = {'viennoiserie', 'pao_sourdough', 'fornada_especial'}
        for key, val in request.form.items():
            if not key.startswith('familia_'):
                continue
            try:
                rid = int(key[len('familia_'):])
            except ValueError:
                continue
            r = Receita.query.get(rid)
            if not r:
                continue
            v = (val or '').strip().lower() or None
            nova = v if v in permitidos else None
            if r.familia != nova:
                r.familia = nova
                atualizados += 1
        if atualizados:
            db.session.commit()
            flash(f'{atualizados} receita(s) atualizada(s).', 'success')
        else:
            flash('Nenhuma mudança.', 'info')
        return redirect(url_for('receitas.familias'))

    receitas = (Receita.query.filter(Receita.arquivada_em.is_(None))
                .order_by(Receita.categoria, Receita.nome).all())
    categorias = {}
    for r in receitas:
        cat = r.categoria or 'Outros'
        categorias.setdefault(cat, []).append(r)
    return render_template('receitas/familias.html', categorias=categorias)


@receitas_bp.route('/<int:id>/padeiro')
@login_required
def padeiro(id):
    receita = Receita.query.get_or_404(id)
    resultado = calcular_custos_receitas()
    return render_template('receitas/padeiro.html', receita=receita,
                           etapas_preparo=dividir_etapas_preparo(receita.modo_preparo),
                           receita_custos=resultado['custos'],
                           receita_pesos=resultado['pesos'])


@receitas_bp.route('/precos', methods=['GET', 'POST'])
@login_required
@admin_required
def precos():
    """Tela bulk pra editar preco_loja/preco_site/preco_venda de todas as receitas.

    GET: agrupa por categoria. POST: parseia preco_loja_<id>/preco_site_<id>/preco_venda_<id>
    e salva so o que mudou."""
    if request.method == 'POST':
        atualizados = 0
        for r in Receita.query.all():
            # So mexe em quem veio no form — arquivadas (fora da tela) nao
            # podem ter os precos zerados por ausencia.
            if f'preco_loja_{r.id}' not in request.form:
                continue
            antes = (r.preco_loja, r.preco_site, r.preco_venda)
            r.preco_loja = parse_float_br(request.form.get(f'preco_loja_{r.id}', ''))
            r.preco_site = parse_float_br(request.form.get(f'preco_site_{r.id}', ''))
            r.preco_venda = parse_float_br(request.form.get(f'preco_venda_{r.id}', ''))
            depois = (r.preco_loja, r.preco_site, r.preco_venda)
            if antes != depois:
                atualizados += 1
        if atualizados:
            db.session.commit()
            flash(f'{atualizados} receita(s) com preço atualizado.', 'success')
        else:
            flash('Nenhuma mudança.', 'info')
        return redirect(url_for('receitas.precos'))

    receitas = (Receita.query.filter(Receita.arquivada_em.is_(None))
                .order_by(Receita.categoria, Receita.nome).all())
    categorias = {}
    for r in receitas:
        cat = r.categoria or 'Outros'
        categorias.setdefault(cat, []).append(r)
    return render_template('receitas/precos.html', categorias=categorias)


@receitas_bp.route('/reaproveitavel', methods=['GET', 'POST'])
@login_required
@admin_required
def reaproveitavel():
    """Tela bulk pra marcar Receita.reaproveitavel e Produto.reaproveitavel.

    Item reaproveitavel: desperdicio com motivo='validade' nao baixa estoque
    (vira outra coisa — ex: croissant vencido vira croissant amande)."""
    if request.method == 'POST':
        atualizados_r = 0
        atualizados_p = 0
        marcados_r = {int(k[len('reap_r_'):]) for k in request.form.keys()
                      if k.startswith('reap_r_')}
        marcados_p = {int(k[len('reap_p_'):]) for k in request.form.keys()
                      if k.startswith('reap_p_')}
        # Checkbox desmarcado nao vem no form — arquivada (fora da tela)
        # nao pode ser "desmarcada" por ausencia.
        for r in Receita.query.filter(Receita.arquivada_em.is_(None)).all():
            novo = r.id in marcados_r
            if bool(r.reaproveitavel) != novo:
                r.reaproveitavel = novo
                atualizados_r += 1
        for p in Produto.query.all():
            novo = p.id in marcados_p
            if bool(p.reaproveitavel) != novo:
                p.reaproveitavel = novo
                atualizados_p += 1
        if atualizados_r or atualizados_p:
            db.session.commit()
            flash(f'{atualizados_r} receita(s) + {atualizados_p} produto(s) atualizados.',
                  'success')
        else:
            flash('Nenhuma mudança.', 'info')
        return redirect(url_for('receitas.reaproveitavel'))

    receitas = (Receita.query.filter(Receita.arquivada_em.is_(None))
                .order_by(Receita.categoria, Receita.nome).all())
    produtos = Produto.query.order_by(Produto.categoria, Produto.nome).all()
    receitas_por_cat = {}
    for r in receitas:
        cat = r.categoria or 'Outros'
        receitas_por_cat.setdefault(cat, []).append(r)
    produtos_por_cat = {}
    for p in produtos:
        cat = p.categoria or 'Outros'
        produtos_por_cat.setdefault(cat, []).append(p)
    return render_template('receitas/reaproveitavel.html',
                           receitas_por_cat=receitas_por_cat,
                           produtos_por_cat=produtos_por_cat)


@receitas_bp.route('/imagens/upload', methods=['GET', 'POST'])
@login_required
@admin_required
def imagens_upload():
    """Upload em massa de fotos de receita via .zip.

    Cada arquivo .jpg/.png/.webp no zip eh casado contra Receita.nome
    (exato case-insensitive, fallback fuzzy via difflib). Casou -> popula
    imagem_blob + imagem_mimetype. Nao casou -> aparece no relatorio.
    """
    if request.method == 'GET':
        return render_template('receitas/imagens_upload.html')

    arquivo = request.files.get('zipfile')
    if not arquivo or not arquivo.filename:
        flash('Selecione um arquivo .zip.', 'warning')
        return redirect(url_for('receitas.imagens_upload'))

    EXT_OK = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
              '.png': 'image/png', '.webp': 'image/webp'}
    MAX_IMG = 5 * 1024 * 1024  # 5 MB por imagem

    receitas = Receita.query.order_by(Receita.nome).all()
    por_nome_lower = {r.nome.lower(): r for r in receitas}
    nomes_lower = list(por_nome_lower.keys())

    casados = []        # [(nome_arquivo, receita)]
    nao_casados = []    # [(nome_arquivo, motivo)]
    atualizadas = 0

    try:
        bruto = arquivo.read()
        zf = zipfile.ZipFile(io.BytesIO(bruto))
    except zipfile.BadZipFile:
        flash('Arquivo invalido — nao parece ser um .zip.', 'danger')
        return redirect(url_for('receitas.imagens_upload'))

    for info in zf.infolist():
        if info.is_dir():
            continue
        nome_base = os.path.basename(info.filename)
        if not nome_base or nome_base.startswith('.') or nome_base.startswith('__'):
            continue  # .DS_Store, __MACOSX/
        raiz, ext = os.path.splitext(nome_base)
        ext = ext.lower()
        if ext not in EXT_OK:
            nao_casados.append((nome_base, f'extensao {ext or "sem"} nao suportada'))
            continue
        if info.file_size > MAX_IMG:
            nao_casados.append((nome_base, f'arquivo > 5 MB ({info.file_size // 1024} KB)'))
            continue

        raiz_l = raiz.lower().strip()
        r = por_nome_lower.get(raiz_l)
        if not r:
            sugest = difflib.get_close_matches(raiz_l, nomes_lower, n=1, cutoff=0.85)
            if sugest:
                r = por_nome_lower[sugest[0]]
        if not r:
            nao_casados.append((nome_base, 'nao casou com nenhuma receita'))
            continue

        with zf.open(info) as f:
            raw_bytes = f.read()
        from app.services import dropbox_storage
        from app.utils import comprimir_imagem
        if dropbox_storage.disponivel():
            try:
                comprimida = comprimir_imagem(raw_bytes)
                path = f'/cardapio/receita/{r.id}.jpg'
                upload_info = dropbox_storage.upload_publico(
                    comprimida, path, mode='overwrite', autorename=False)
                r.imagem_dropbox_url = upload_info['url']
                r.imagem_storage_path = upload_info['storage_path']
                r.imagem_blob = None
                r.imagem_mimetype = 'image/jpeg'
            except (ValueError, RuntimeError):
                # Fallback BLOB se Dropbox falhar
                r.imagem_blob = raw_bytes
                r.imagem_mimetype = EXT_OK[ext]
        else:
            r.imagem_blob = raw_bytes
            r.imagem_mimetype = EXT_OK[ext]
        casados.append((nome_base, r))
        atualizadas += 1

    db.session.commit()
    return render_template('receitas/imagens_relatorio.html',
                           casados=casados, nao_casados=nao_casados,
                           atualizadas=atualizadas)


@receitas_bp.route('/modos-preparo')
@login_required
@admin_required
def modos_preparo():
    """Tela em lote pra cadastrar o modo de preparo de cada receita.

    Filtros: pendentes (sem texto), preenchidas (com texto), todas.
    Auto-save por textarea via POST /receitas/modos-preparo/salvar.json.
    """
    filtro = request.args.get('filtro', 'pendentes')
    q = Receita.query
    vazio = db.or_(Receita.modo_preparo.is_(None), Receita.modo_preparo == '')
    if filtro == 'pendentes':
        q = q.filter(vazio)
    elif filtro == 'preenchidas':
        q = q.filter(db.not_(vazio))
    receitas = q.order_by(Receita.categoria, Receita.nome).all()
    total = Receita.query.count()
    preenchidas = Receita.query.filter(db.not_(vazio)).count()
    return render_template('receitas/modos_preparo.html',
                           receitas=receitas, filtro=filtro,
                           total=total, preenchidas=preenchidas)


@receitas_bp.route('/modos-preparo/salvar.json', methods=['POST'])
@login_required
def modos_preparo_salvar():
    if not (current_user.is_admin()
            or current_user.is_owner
            or current_user.is_padeiro()):
        return jsonify({'ok': False, 'erro': 'sem permissao'}), 403
    receita_id = request.form.get('receita_id', type=int)
    if not receita_id:
        return jsonify({'ok': False, 'erro': 'receita_id ausente'}), 400
    receita = Receita.query.get(receita_id)
    if not receita:
        return jsonify({'ok': False, 'erro': 'receita não encontrada'}), 404
    receita.modo_preparo = (request.form.get('texto', '') or '').strip() or None
    db.session.commit()
    return jsonify({'ok': True})


@receitas_bp.route('/<int:id>/salvar', methods=['POST'])
@login_required
def salvar(id):
    receita = Receita.query.get_or_404(id)

    # Funcionário só pode salvar fichas atribuídas
    if not current_user.is_admin():
        atribuida = Atribuicao.query.filter_by(
            receita_id=id, usuario_id=current_user.id
        ).first()
        if not atribuida:
            abort(403)

    receita.nome = request.form.get('nome', receita.nome).strip()
    receita.categoria = request.form.get('categoria', '').strip() or None
    fam = (request.form.get('familia') or '').strip().lower() or None
    if fam in ('viennoiserie', 'pao_sourdough', 'fornada_especial'):
        receita.familia = fam
    elif fam is None or fam == '':
        receita.familia = None
    receita.preco_venda = parse_float_br(request.form.get('preco_venda', ''))
    receita.preco_loja = parse_float_br(request.form.get('preco_loja', ''))
    receita.preco_site = parse_float_br(request.form.get('preco_site', ''))
    receita.rendimento_qtd = parse_float_br(request.form.get('rendimento_qtd', ''), default=1)
    receita.rendimento_unidade = request.form.get('rendimento_unidade', 'unidades').strip()
    receita.peso_base = parse_float_br(request.form.get('peso_base', ''), default=1000)
    receita.peso_unitario = parse_float_br(request.form.get('peso_unitario', ''))
    receita.perda_percentual = parse_float_br(request.form.get('perda_percentual', ''), default=0)
    receita.custo_embalagem = parse_float_br(request.form.get('custo_embalagem', ''), default=0)
    # Modo de preparo: a ficha nova manda etapas separadas (1 modulo por
    # etapa); junta com linha em branco — mesmo separador que a leitura usa
    # (dividir_etapas_preparo). Forms antigos/lote seguem mandando o texto
    # inteiro em `modo_preparo`.
    if request.form.get('tem_etapas'):
        etapas = [e.replace('\r\n', '\n').replace('\r', '\n').strip()
                  for e in request.form.getlist('modo_preparo_etapa[]')]
        receita.modo_preparo = '\n\n'.join(e for e in etapas if e) or None
    else:
        receita.modo_preparo = request.form.get('modo_preparo', '').strip() or None
    receita.observacao = request.form.get('observacao', '').strip() or None
    ep = (request.form.get('estado_padrao') or '').strip().lower()
    receita.estado_padrao = ep if ep in ('assado', 'backup') else None
    receita.reaproveitavel = bool(request.form.get('reaproveitavel'))
    receita.imagem_url = request.form.get('imagem_url', '').strip() or None

    # Atualiza ingredientes
    ReceitaIngrediente.query.filter_by(receita_id=receita.id).delete()

    tipos = request.form.getlist('ingrediente_tipo[]')
    nomes = request.form.getlist('ingrediente_nome[]')
    porcentagens = request.form.getlist('porcentagem[]')
    bases = request.form.getlist('eh_base[]')
    notas = request.form.getlist('nota[]')

    for i in range(len(nomes)):
        nome = nomes[i].strip()
        pct_str = porcentagens[i].replace(',', '.').strip()
        if not nome or not pct_str:
            continue
        tipo = tipos[i] if i < len(tipos) else 'mp'
        ing = ReceitaIngrediente(
            receita_id=receita.id,
            tipo=tipo,
            ingrediente_nome=nome,
            porcentagem=float(pct_str),
            eh_base=(bases[i] == '1') if i < len(bases) else False,
            nota=notas[i].strip() if i < len(notas) else None,
        )
        db.session.add(ing)

    db.session.commit()
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify(success=True)
    flash('Ficha salva com sucesso!', 'success')
    return redirect(url_for('receitas.ficha', id=receita.id))


@receitas_bp.route('/nova', methods=['POST'])
@login_required
@admin_required
def nova():
    receita = Receita(
        nome='Novo Produto',
        categoria='',
        rendimento_qtd=1,
        rendimento_unidade='unidades',
        peso_base=1000,
    )
    db.session.add(receita)
    db.session.commit()
    flash('Novo produto criado!', 'success')
    return redirect(url_for('receitas.ficha', id=receita.id))


@receitas_bp.route('/<int:id>/duplicar', methods=['POST'])
@login_required
@admin_required
def duplicar(id):
    original = Receita.query.get_or_404(id)
    copia = Receita(
        nome=f'Cópia de {original.nome}',
        categoria=original.categoria,
        preco_venda=original.preco_venda,
        preco_loja=original.preco_loja,
        preco_site=original.preco_site,
        rendimento_qtd=original.rendimento_qtd,
        rendimento_unidade=original.rendimento_unidade,
        peso_base=original.peso_base,
        peso_unitario=original.peso_unitario,
        perda_percentual=original.perda_percentual,
        custo_embalagem=original.custo_embalagem,
        modo_preparo=original.modo_preparo,
    )
    db.session.add(copia)
    db.session.flush()

    for ing in original.ingredientes:
        novo_ing = ReceitaIngrediente(
            receita_id=copia.id,
            tipo=ing.tipo or 'mp',
            ingrediente_nome=ing.ingrediente_nome,
            porcentagem=ing.porcentagem,
            eh_base=ing.eh_base,
            nota=ing.nota,
        )
        db.session.add(novo_ing)

    db.session.commit()
    flash(f'Receita duplicada: "{copia.nome}"', 'success')
    return redirect(url_for('receitas.ficha', id=copia.id))


def _vinculos_receita(receita):
    """Agrupa tudo que referencia a receita, separando o que tem resolucao
    automatica SEGURA (configuracao: cestas, mapeamentos de PDV, precos,
    atribuicoes, uso como ingrediente) do que e HISTORICO e nunca se apaga
    por aqui (pedidos, vendas, estoque, desperdicio — peso especial).
    Retorna (grupos, pode_excluir)."""
    from app.models import (
        Atribuicao,
        Desperdicio,
        EstoqueLoja,
        EstoqueProducao,
        LojaProdutoMap,
        PedidoItem,
        PlanejamentoItem,
        PrecoLojaReceita,
        ProdutoItem,
        SeruProdutoMap,
        VendaB2BItem,
        VendaManualLoja,
        VndaProdutoMap,
    )
    rid = receita.id
    grupos = []

    def _grupo(chave, titulo, resolvivel, descricao, itens, qtd=None):
        if qtd or itens:
            grupos.append({'chave': chave, 'titulo': titulo,
                           'resolvivel': resolvivel, 'descricao': descricao,
                           'qtd': qtd if qtd is not None else len(itens),
                           'itens': itens[:10]})

    # ── Resolviveis (configuracao, nao historico) ──
    itens_cesta = ProdutoItem.query.filter_by(receita_id=rid).all()
    _grupo('cestas', 'Componente de cestas/produtos', True,
           'Remove esta receita da composição das cestas listadas.',
           [{'label': (i.produto.nome if getattr(i, 'produto', None)
                       else f'cesta #{i.produto_id}'),
             'url': url_for('produtos.detalhe', id=i.produto_id)}
            for i in itens_cesta])

    usos = (ReceitaIngrediente.query
            .filter(ReceitaIngrediente.tipo == 'receita',
                    ReceitaIngrediente.ingrediente_nome == receita.nome,
                    ReceitaIngrediente.receita_id != rid).all())
    _grupo('ingrediente_em_fichas', 'Usada como ingrediente em outras fichas',
           True,
           'Remove o ingrediente das fichas listadas — a composição e o '
           'custo DELAS mudam.',
           [{'label': (u.receita.nome if u.receita else f'ficha #{u.receita_id}'),
             'url': url_for('receitas.ficha', id=u.receita_id)} for u in usos])

    maps = []
    for m in SeruProdutoMap.query.filter_by(receita_id=rid).all():
        maps.append({'label': f'Seru: {m.seru_nome}',
                     'url': url_for('pdv.mapeamentos')})
    for m in VndaProdutoMap.query.filter_by(receita_id=rid).all():
        maps.append({'label': f'Site: {m.vnda_nome}', 'url': None})
    for m in LojaProdutoMap.query.filter_by(receita_id=rid).all():
        maps.append({'label': f'Loja: {m.nome_digitado}', 'url': None})
    _grupo('mapeamentos', 'Mapeamentos de PDV/site/loja', True,
           'Desfaz os vínculos — os nomes voltam pra fila de pendentes.',
           maps)

    precos = PrecoLojaReceita.query.filter_by(receita_id=rid).count()
    _grupo('precos_loja', 'Preços por loja', True,
           'Apaga os preços específicos por loja desta receita.',
           [], qtd=precos)

    atribs = Atribuicao.query.filter_by(receita_id=rid).count()
    _grupo('atribuicoes', 'Atribuições a funcionários', True,
           'Apaga as atribuições de preparo desta receita.',
           [], qtd=atribs)

    # ── Historico: NUNCA apagavel por aqui ──
    historicos = (
        ('Pedidos de loja', PedidoItem),
        ('Vendas B2B', VendaB2BItem),
        ('Vendas manuais de loja', VendaManualLoja),
        ('Estoque de produção/congelados', EstoqueProducao),
        ('Estoque de loja', EstoqueLoja),
        ('Registros de desperdício', Desperdicio),
        ('Planos de produção', PlanejamentoItem),
    )
    for titulo, modelo in historicos:
        n = modelo.query.filter_by(receita_id=rid).count()
        _grupo(f'hist_{modelo.__tablename__}', titulo, False,
               'Histórico — não se apaga, mas dá pra TRANSFERIR: use o campo '
               '"Transferir vínculos" abaixo pra reapontar tudo pra outra '
               'receita (pedidos, vendas e estoque passam a contar lá) e aí '
               'excluir esta.',
               [], qtd=n)

    pode_excluir = not grupos
    return grupos, pode_excluir


@receitas_bp.route('/<int:id>/vinculos')
@login_required
@admin_required
def vinculos(id):
    """JSON pro modal de exclusão: o que ainda referencia a receita."""
    receita = Receita.query.get_or_404(id)
    grupos, pode = _vinculos_receita(receita)
    return jsonify(grupos=grupos, pode_excluir=pode)


@receitas_bp.route('/<int:id>/vinculos/resolver', methods=['POST'])
@login_required
@admin_required
def vinculos_resolver(id):
    """Resolve UM grupo de vínculos (ação explícita do admin no modal).
    Só grupos de configuração — histórico nunca passa por aqui."""
    from app.models import (
        Atribuicao,
        LojaProdutoMap,
        PrecoLojaReceita,
        ProdutoItem,
        SeruProdutoMap,
        VndaProdutoMap,
    )
    receita = Receita.query.get_or_404(id)
    chave = request.form.get('chave') or ''
    if chave == 'cestas':
        ProdutoItem.query.filter_by(receita_id=receita.id).delete()
    elif chave == 'ingrediente_em_fichas':
        ReceitaIngrediente.query.filter(
            ReceitaIngrediente.tipo == 'receita',
            ReceitaIngrediente.ingrediente_nome == receita.nome,
            ReceitaIngrediente.receita_id != receita.id).delete()
    elif chave == 'mapeamentos':
        # Volta pra pendente (receita_id NULL) — nao apaga o nome mapeado.
        for modelo in (SeruProdutoMap, VndaProdutoMap, LojaProdutoMap):
            for m in modelo.query.filter_by(receita_id=receita.id).all():
                m.receita_id = None
                if hasattr(m, 'confirmado_em'):
                    m.confirmado_em = None
    elif chave == 'precos_loja':
        PrecoLojaReceita.query.filter_by(receita_id=receita.id).delete()
    elif chave == 'atribuicoes':
        Atribuicao.query.filter_by(receita_id=receita.id).delete()
    else:
        return jsonify(erro=f'grupo "{chave}" não tem resolução automática'), 400
    db.session.commit()
    grupos, pode = _vinculos_receita(receita)
    return jsonify(grupos=grupos, pode_excluir=pode)


@receitas_bp.route('/<int:id>/vinculos/transferir', methods=['POST'])
@login_required
@admin_required
def vinculos_transferir(id):
    """Transfere TODOS os vínculos da receita pra outra (fusão de duplicata,
    ex: "Molho Pesto 100g" -> "Molho Pesto"). Histórico não se apaga — se
    REAPONTA: pedidos/vendas/desperdício mudam a FK; estoque FUNDE com a
    linha equivalente do destino (mesma loja/estado) somando quantidades e
    reapontando as movimentações pra linha que fica — nada se perde.
    Estoque/dinheiro têm peso especial: tudo explícito, 1 commit no fim."""
    from sqlalchemy import func

    from app.models import (
        Atribuicao,
        Desperdicio,
        EstoqueLoja,
        EstoqueProducao,
        LojaProdutoMap,
        MovEstoqueLoja,
        MovEstoqueProducao,
        PedidoItem,
        PlanejamentoItem,
        PrecoLojaReceita,
        ProdutoItem,
        SeruProdutoMap,
        VendaB2BItem,
        VendaManualLoja,
        VndaProdutoMap,
    )
    origem = Receita.query.get_or_404(id)
    nome_destino = (request.form.get('destino') or '').strip()
    destino = (Receita.query
               .filter(func.lower(Receita.nome) == nome_destino.lower())
               .first()) if nome_destino else None
    if not destino:
        return jsonify(erro=f'receita "{nome_destino}" não encontrada — '
                            'use o nome exato (o campo autocompleta)'), 400
    if destino.id == origem.id:
        return jsonify(erro='o destino é a própria receita'), 400

    movidos = {}

    def _conta(chave, n):
        if n:
            movidos[chave] = movidos.get(chave, 0) + n

    # FKs simples: o registro histórico fica intacto, só muda o alvo.
    for chave, modelo in (('pedidos', PedidoItem),
                          ('vendas_b2b', VendaB2BItem),
                          ('vendas_manuais', VendaManualLoja),
                          ('desperdicio', Desperdicio),
                          ('planejamento', PlanejamentoItem),
                          ('atribuicoes', Atribuicao)):
        _conta(chave, modelo.query.filter_by(receita_id=origem.id)
               .update({'receita_id': destino.id}, synchronize_session=False))

    # Cestas: reaponta a FK e corrige o nome humano-legível.
    _conta('cestas', ProdutoItem.query.filter_by(receita_id=origem.id)
           .update({'receita_id': destino.id, 'item_nome': destino.nome},
                   synchronize_session=False))

    # Uso como ingrediente em outras fichas (vínculo por NOME).
    _conta('ingrediente_em_fichas', ReceitaIngrediente.query
           .filter(ReceitaIngrediente.tipo == 'receita',
                   ReceitaIngrediente.ingrediente_nome == origem.nome,
                   ReceitaIngrediente.receita_id != origem.id)
           .update({'ingrediente_nome': destino.nome},
                   synchronize_session=False))

    # Mapeamentos de PDV/site/loja (mantém confirmação e fator).
    for modelo in (SeruProdutoMap, VndaProdutoMap, LojaProdutoMap):
        _conta('mapeamentos', modelo.query.filter_by(receita_id=origem.id)
               .update({'receita_id': destino.id}, synchronize_session=False))

    # Preços por loja: unique (loja, receita) — se o destino já tem preço
    # naquela loja, o preço dele prevalece e o da origem é descartado.
    for p in PrecoLojaReceita.query.filter_by(receita_id=origem.id).all():
        ja_tem = PrecoLojaReceita.query.filter_by(
            receita_id=destino.id, loja_id=p.loja_id).first()
        if ja_tem:
            db.session.delete(p)
        else:
            p.receita_id = destino.id
        _conta('precos_loja', 1)

    # Estoque: funde com a linha equivalente do destino (mesmo estado/loja).
    # As movimentações são reapontadas ANTES de apagar a linha da origem —
    # o histórico de movimento sobrevive inteiro na linha que fica.
    for e in EstoqueProducao.query.filter_by(receita_id=origem.id).all():
        alvo = EstoqueProducao.query.filter_by(
            receita_id=destino.id, estado=e.estado).first()
        if alvo:
            alvo.quantidade = (alvo.quantidade or 0) + (e.quantidade or 0)
            MovEstoqueProducao.query.filter_by(estoque_producao_id=e.id).update(
                {'estoque_producao_id': alvo.id}, synchronize_session=False)
            db.session.delete(e)
        else:
            e.receita_id = destino.id
        _conta('estoque_producao', 1)

    for e in EstoqueLoja.query.filter_by(receita_id=origem.id).all():
        alvo = EstoqueLoja.query.filter_by(
            receita_id=destino.id, loja_id=e.loja_id, estado=e.estado).first()
        if alvo:
            alvo.quantidade = (alvo.quantidade or 0) + (e.quantidade or 0)
            MovEstoqueLoja.query.filter_by(estoque_loja_id=e.id).update(
                {'estoque_loja_id': alvo.id}, synchronize_session=False)
            db.session.delete(e)
        else:
            e.receita_id = destino.id
        _conta('estoque_loja', 1)

    db.session.commit()
    current_app.logger.info(
        'vinculos de receita transferidos: "%s" (#%s) -> "%s" (#%s) por %s: %s',
        origem.nome, origem.id, destino.nome, destino.id,
        current_user.login, movidos)
    grupos, pode = _vinculos_receita(origem)
    return jsonify(grupos=grupos, pode_excluir=pode, movidos=movidos,
                   destino=destino.nome)


@receitas_bp.route('/<int:id>/arquivar', methods=['POST'])
@login_required
@admin_required
def arquivar(id):
    """Arquiva/desarquiva. Arquivada = fora das listas e seletores (padeiro,
    datalists, copilot, vendas), historico 100% preservado — e o caminho pra
    receita descontinuada que tem pedidos/vendas/estoque e nao pode ser
    excluida nem faz sentido transferir."""
    receita = Receita.query.get_or_404(id)
    if receita.arquivada_em:
        receita.arquivada_em = None
        receita.arquivada_por_id = None
        db.session.commit()
        flash(f'"{receita.nome}" desarquivada — voltou pras listas.', 'success')
        return redirect(url_for('receitas.ficha', id=id))
    receita.arquivada_em = agora()
    receita.arquivada_por_id = current_user.id
    db.session.commit()
    flash(f'"{receita.nome}" arquivada. O histórico fica intacto; ela só '
          'sai das listas. Dá pra desarquivar na própria ficha.', 'success')
    return redirect(url_for('receitas.padeiro_lista'))


@receitas_bp.route('/<int:id>/excluir', methods=['POST'])
@login_required
@admin_required
def excluir(id):
    from sqlalchemy.exc import IntegrityError
    receita = Receita.query.get_or_404(id)
    nome = receita.nome
    # Delete cru estourava 500 quando a receita era referenciada (pedidos,
    # estoque, produtos/cestas, mapeamentos de PDV) — FKs sem cascade. Aborta
    # de forma limpa com mensagem em vez de 500; o historico fica intacto.
    try:
        db.session.delete(receita)
        db.session.commit()
        flash(f'"{nome}" excluído com sucesso!', 'success')
    except IntegrityError:
        db.session.rollback()
        flash(f'Não é possível excluir "{nome}": há pedidos, estoque, produtos '
              f'ou mapeamentos de PDV vinculados a ela. Desvincule-os primeiro '
              f'(ou me peça para arquivar a receita).', 'danger')
    return redirect(url_for('receitas.padeiro_lista'))


@receitas_bp.route('/api/nova-mp', methods=['POST'])
@login_required
@admin_required
def nova_mp():
    """Cria matéria-prima via AJAX (sem sair da ficha técnica)."""
    nome = request.form.get('mp_nome', '').strip()
    custo = request.form.get('mp_custo', '').replace(',', '.').strip()

    if not nome or not custo:
        return jsonify(success=False, error='Preencha nome e custo.')

    if MateriaPrima.query.filter_by(nome=nome).first():
        return jsonify(success=False, error=f'"{nome}" ja existe no banco de MP.')

    try:
        custo_float = float(custo)
    except ValueError:
        return jsonify(success=False, error='Custo invalido.')

    mp = MateriaPrima(nome=nome, unidade='g', custo_por_kg=custo_float)
    db.session.add(mp)
    db.session.commit()

    return jsonify(success=True, nome=nome, custo=custo_float)
