import difflib
import io
import os
import zipfile

from flask import abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.blueprints.receitas import receitas_bp
from app.decorators import admin_required
from app.extensions import db
from app.models import Atribuicao, MateriaPrima, Produto, Receita, ReceitaIngrediente
from app.services.custos import calcular_custos_receitas
from app.utils import dividir_etapas_preparo, parse_float_br


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
    receitas = Receita.query.order_by(Receita.categoria, Receita.nome).all()
    categorias = {}
    for r in receitas:
        cat = r.categoria or 'Outros'
        categorias.setdefault(cat, []).append(r)
    return render_template('receitas/padeiro_lista.html', categorias=categorias)


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

    receitas = Receita.query.order_by(Receita.categoria, Receita.nome).all()
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

    receitas = Receita.query.order_by(Receita.categoria, Receita.nome).all()
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
        for r in Receita.query.all():
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

    receitas = Receita.query.order_by(Receita.categoria, Receita.nome).all()
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
