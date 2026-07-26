from flask import flash, jsonify, redirect, render_template, request, url_for
from flask_login import login_required

from app.blueprints.produtos import produtos_bp
from app.decorators import admin_required, catalogo_required
from app.extensions import db
from app.models import MateriaPrima, Produto, ProdutoItem, Receita
from app.services.custos import calcular_custo_produto, calcular_custos_receitas
from app.utils import parse_float_br


@produtos_bp.route('/')
@login_required
def lista():
    produtos = (Produto.query.filter_by(ativo=True)
                .order_by(Produto.categoria, Produto.nome).all())
    resultado = calcular_custos_receitas()
    fabricados = resultado['fabricados']

    # Calcular custo de cada cesta — passa dict de produto_custos pra
    # resolver componentes tipo='produto' (cesta-de-cesta).
    from app.services.custos import calcular_custos_produtos
    produto_custos_idx = calcular_custos_produtos(resultado['custos'],
                                                    resultado['mp_info'])
    cestas = []
    for p in produtos:
        custo = calcular_custo_produto(p, resultado['custos'],
                                        resultado['mp_info'],
                                        produto_custos_idx)
        cestas.append({
            'id': p.id,
            'nome': p.nome,
            'categoria': p.categoria or '',
            'descricao': p.descricao or '',
            'num_itens': len(p.itens),
            'custo': custo,
            'preco_atacado': p.preco_atacado or 0,
            'preco_loja': p.preco_loja or 0,
            'preco_site': p.preco_site or 0,
            'observacao': p.observacao or '',
        })

    return render_template('produtos/lista.html', fabricados=fabricados, cestas=cestas)


@produtos_bp.route('/novo', methods=['POST'])
@login_required
@admin_required
def novo():
    """Cria cesta/kit (composicao de itens) OU produto simples de revenda
    (agua, chiclete, iogurte comprado pronto — so custo_direto + preco,
    sem componentes). Antes o botao so criava 'Nova Cesta' e nao havia
    caminho pra revenda (apontado pelo dono em 10/06/2026)."""
    tipo = (request.form.get('tipo') or 'cesta').strip().lower()
    nome = (request.form.get('nome') or '').strip()
    if tipo == 'simples':
        produto = Produto(nome=nome or 'Novo Produto',
                          categoria=(request.form.get('categoria') or 'Revenda').strip())
    else:
        produto = Produto(nome=nome or 'Nova Cesta', categoria='Cestas')
    db.session.add(produto)
    db.session.commit()
    return redirect(url_for('produtos.detalhe', id=produto.id))


@produtos_bp.route('/<int:id>/duplicar', methods=['POST'])
@login_required
@admin_required
def duplicar(id):
    """Duplica um Produto (cesta/kit ou revenda) com a composicao inteira.

    NAO copia de proposito:
    - preco_site/ordem_site: publicacao na vitrine e preco_site > 0
      (loja_catalogo.produtos_publicados) — a copia nao pode aparecer no
      site antes de ser revisada;
    - imagem_*: remover a imagem deleta o arquivo do Dropbox pelo
      storage_path (main/routes.py::cardapio_img_remover) — copia
      compartilhando o mesmo arquivo perderia a imagem dos DOIS.
    """
    original = Produto.query.get_or_404(id)
    copia = Produto(
        nome=f'Cópia de {original.nome}',
        categoria=original.categoria,
        descricao=original.descricao,
        descricao_seo=original.descricao_seo,
        preco_atacado=original.preco_atacado,
        preco_loja=original.preco_loja,
        preco_interno=original.preco_interno,
        custo_direto=original.custo_direto,
        custo_embalagem=original.custo_embalagem,
        modo_preparo=original.modo_preparo,
        observacao=original.observacao,
        reaproveitavel=original.reaproveitavel,
        sob_encomenda=original.sob_encomenda,
        # Menu configuravel: as travas viajam com a copia (o preco_site nao
        # vem, entao a copia so publica quando o admin revisar).
        menu_configuravel=original.menu_configuravel,
        menu_total_unidades=original.menu_total_unidades,
        menu_max_por_item=original.menu_max_por_item,
    )
    db.session.add(copia)
    db.session.flush()

    # Componentes com FK (item_nome e so fallback humano-legivel — a baixa
    # de estoque resolve SEMPRE pela FK).
    for item in original.itens:
        db.session.add(ProdutoItem(
            produto_id=copia.id,
            tipo=item.tipo,
            receita_id=item.receita_id,
            produto_componente_id=item.produto_componente_id,
            materia_prima_id=item.materia_prima_id,
            item_nome=item.item_nome,
            quantidade=item.quantidade,
            preco_menu=item.preco_menu,
        ))

    db.session.commit()
    flash(f'Produto duplicado: "{copia.nome}". Preço do site NÃO foi copiado '
          '— defina quando quiser publicar a cópia na vitrine.', 'success')
    return redirect(url_for('produtos.detalhe', id=copia.id))


@produtos_bp.route('/<int:id>')
@login_required
def detalhe(id):
    produto = Produto.query.get_or_404(id)
    resultado = calcular_custos_receitas()
    receita_custos = resultado['custos']
    mp_info = resultado['mp_info']

    # Indice de custo de cada Produto. Considera composicao: se o produto
    # tem ProdutoItens (cesta), soma componentes; se nao tem, usa custo_direto.
    # Suporta cesta-dentro-de-cesta via iteracao.
    from app.services.custos import calcular_custos_produtos
    produto_custos = calcular_custos_produtos(receita_custos, mp_info)

    # Lookups normalizados (case/espaco-tolerant) pra evitar custo zero
    # quando grafia do item_nome divergir do cadastro (ex: "Iogurte 200ml"
    # vs "iogurte 200ml ").
    def _norm(s):
        return (s or '').strip().casefold()
    receita_custos_n = {_norm(k): v for k, v in receita_custos.items()}
    produto_custos_n = {_norm(k): v for k, v in produto_custos.items()}
    mp_info_n = {_norm(k): v for k, v in mp_info.items()}

    # Custo de cada item para exibir no template. Nome via FK
    # (nome_resolvido): `item_nome` pode ter ficado com grafia antiga apos
    # rename do componente — alem de zerar o custo, o nome velho no input
    # faria o Salvar re-resolver a FK errado e ORFANAR o item (a baixa de
    # venda para em silencio). Caso real: iogurte, 03/07/2026.
    itens_data = []
    for item in produto.itens:
        info = {}
        nome = item.nome_resolvido
        if item.tipo == 'receita':
            custo_un = receita_custos.get(nome)
            if custo_un is None:
                custo_un = receita_custos_n.get(_norm(nome), 0)
            unidade = 'un'
        elif item.tipo == 'produto':
            custo_un = produto_custos.get(nome)
            if custo_un is None:
                custo_un = produto_custos_n.get(_norm(nome), 0)
            unidade = 'un'
        else:
            info = mp_info.get(nome) or mp_info_n.get(_norm(nome), {})
            custo_kg = info.get('custo_por_kg', 0)
            unidade = info.get('unidade', 'un')
            if unidade in ('g', 'ml'):
                custo_un = custo_kg / 1000
            else:
                custo_un = custo_kg
        itens_data.append({
            'tipo': item.tipo,
            'item_nome': nome,
            'quantidade': item.quantidade,
            'custo_un': custo_un,
            'unidade': unidade,
            'custo_por_kg': info.get('custo_por_kg', 0) if item.tipo == 'mp' else None,
            # Preço por unidade dentro do menu configurável (26/07/2026).
            'preco_menu': item.preco_menu,
        })

    custo_total = sum(i['custo_un'] * i['quantidade'] for i in itens_data)

    return render_template('produtos/detalhe.html',
                           produto=produto,
                           itens_data=itens_data,
                           custo_total=custo_total,
                           receita_custos=receita_custos,
                           produto_custos=produto_custos)


def _menu_no_modelo():
    """True quando o MODELO já tem as colunas do menu configurável.

    Procedimento de 2 commits (CLAUDE.md "Schema migrations"): o ALTER
    deploya ANTES do modelo. Entre os dois deploys esta tela não pode
    quebrar — ler/gravar as colunas fica atrás desta guarda. Depois que o
    modelo sobe, tudo funciona normalmente e a guarda vira sempre True."""
    return hasattr(Produto, 'menu_configuravel')


def _int_ou_none(bruto):
    """Inteiro POSITIVO do form, ou None (campo em branco / lixo). Usado nas
    travas do menu configurável — em branco significa "usa o default do
    `loja_menu`", nunca zero (zero travaria a venda em silêncio)."""
    try:
        v = int(str(bruto or '').strip())
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


@produtos_bp.route('/<int:id>/salvar', methods=['POST'])
@login_required
@admin_required
def salvar_composicao(id):
    produto = Produto.query.get_or_404(id)

    nome_antigo = produto.nome
    produto.nome = request.form.get('nome', '').strip() or produto.nome
    if produto.nome != nome_antigo:
        # Rename: sincroniza o nome-fallback nas cestas que usam ESTE produto
        # como componente (a FK produto_componente_id e quem manda; o nome
        # desatualizado zerava o custo do componente e, no Salvar da cesta-mae,
        # orfanava o vinculo — mesmo padrao do rename de receita).
        ProdutoItem.query.filter(
            ProdutoItem.produto_componente_id == produto.id,
            ProdutoItem.produto_id != produto.id,
        ).update({'item_nome': produto.nome})
    produto.categoria = request.form.get('categoria', '').strip() or None
    produto.descricao = request.form.get('descricao', '').strip() or None
    produto.imagem_url = request.form.get('imagem_url', '').strip() or None

    produto.preco_atacado = parse_float_br(request.form.get('preco_atacado', ''))
    produto.preco_loja = parse_float_br(request.form.get('preco_loja', ''))
    produto.preco_site = parse_float_br(request.form.get('preco_site', ''))
    produto.preco_interno = parse_float_br(
        request.form.get('preco_interno', ''))
    produto.custo_direto = parse_float_br(request.form.get('custo_direto', ''))
    produto.custo_embalagem = parse_float_br(request.form.get('custo_embalagem', ''), default=0)
    produto.modo_preparo = request.form.get('modo_preparo', '').strip() or None
    produto.observacao = request.form.get('observacao', '').strip() or None
    produto.reaproveitavel = bool(request.form.get('reaproveitavel'))
    # Sob encomenda D+2 (dono 21/07/2026): so vende D+2 no site, produzido pro
    # pedido (nao abate prateleira), entra na producao do padeiro.
    produto.sob_encomenda = bool(request.form.get('sob_encomenda'))
    # Menu configuravel no site (26/07/2026): cliente escolhe as quantidades
    # de cada componente, com total obrigatorio e teto por item; o preco vira
    # a soma do `preco_menu` do que ele escolher. Campo em branco = usa o
    # default do `loja_menu` (30 un / 10 por item, os numeros do dono).
    if _menu_no_modelo():
        produto.menu_configuravel = bool(request.form.get('menu_configuravel'))
        produto.menu_total_unidades = _int_ou_none(
            request.form.get('menu_total_unidades'))
        produto.menu_max_por_item = _int_ou_none(
            request.form.get('menu_max_por_item'))

    # Recriar itens. Antes de apagar, guarda as FKs atuais por (tipo, nome):
    # GRANDFATHER da linha existente (pos-revisao 19/07/2026) — componente
    # cuja receita foi ARQUIVADA depois de vinculado nao pode virar orfao em
    # silencio num salvar que mexeu em OUTRA linha (a baixa de venda dele
    # pararia). Sem match ativo, a linha reusa a FK que ja tinha.
    fks_atuais = {}
    precos_menu_atuais = {}
    for it in ProdutoItem.query.filter_by(produto_id=produto.id).all():
        chave_it = (it.tipo, (it.item_nome or '').strip())
        fks_atuais[chave_it] = (
            it.receita_id, it.produto_componente_id, it.materia_prima_id)
        precos_menu_atuais[chave_it] = getattr(it, 'preco_menu', None)
    ProdutoItem.query.filter_by(produto_id=produto.id).delete()

    tipos = request.form.getlist('item_tipo[]')
    nomes = request.form.getlist('item_nome[]')
    qtds = request.form.getlist('quantidade[]')
    # Preço por unidade DENTRO do menu configurável. POST sem o campo (form
    # antigo / outra tela) NÃO apaga o preço já cadastrado — mesmo cuidado do
    # grandfather das FKs logo abaixo (a linha é recriada a cada salvamento,
    # então "não veio" tem que significar "mantém", não "zera").
    precos_menu = request.form.getlist('preco_menu[]')
    tem_campo_preco_menu = 'preco_menu[]' in request.form

    for i in range(len(nomes)):
        nome = nomes[i].strip()
        if not nome:
            continue
        tipo = tipos[i] if i < len(tipos) else 'receita'
        qtd_str = qtds[i].replace(',', '.').strip() if i < len(qtds) else '1'
        qtd = float(qtd_str) if qtd_str else 1

        # Resolve FK por nome exato — se nao bater, item fica orfao
        # e admin precisa vincular em /produtos/cestas/orfaos.
        receita_id = None
        produto_componente_id = None
        materia_prima_id = None
        if tipo == 'receita':
            # ativas(): com homonima (arquivada + recriada) o .first() cru
            # podia amarrar a FK na MORTA em silencio — mesma classe do caso
            # iogurte de 03/07 (varredura 19/07/2026). Sem match ativo, vira
            # orfao pro admin resolver.
            r = Receita.ativas().filter_by(nome=nome).first()
            receita_id = r.id if r else None
        elif tipo == 'produto':
            p = Produto.query.filter_by(nome=nome, ativo=True).first()
            # Nao deixa cesta apontar pra ela mesma (loop infinito).
            if p and p.id != produto.id:
                produto_componente_id = p.id
        elif tipo == 'mp':
            m = MateriaPrima.ativas().filter_by(nome=nome).first()
            materia_prima_id = m.id if m else None

        # Sem match ATIVO: reusa a FK que a linha ja tinha (grandfather) —
        # so linha NOVA com nome de arquivada vira orfao de verdade.
        if not (receita_id or produto_componente_id or materia_prima_id):
            antigos = fks_atuais.get((tipo, nome))
            if antigos:
                receita_id, produto_componente_id, materia_prima_id = antigos

        if tem_campo_preco_menu:
            pm = parse_float_br(precos_menu[i]) if i < len(precos_menu) else None
        else:
            pm = precos_menu_atuais.get((tipo, nome))

        item = ProdutoItem(
            produto_id=produto.id,
            tipo=tipo,
            item_nome=nome,
            receita_id=receita_id,
            produto_componente_id=produto_componente_id,
            materia_prima_id=materia_prima_id,
            quantidade=qtd,
            preco_menu=pm,
        )
        db.session.add(item)

    db.session.commit()
    flash(f'"{produto.nome}" salvo com sucesso!', 'success')
    return redirect(url_for('produtos.detalhe', id=produto.id))


@produtos_bp.route('/api/nova-mp', methods=['POST'])
@login_required
@admin_required
def nova_mp():
    """Cria matéria-prima via AJAX (sem sair da página da cesta)."""
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

    mp = MateriaPrima(nome=nome, unidade='un', custo_por_kg=custo_float)
    db.session.add(mp)
    db.session.commit()

    return jsonify(success=True, nome=nome, custo=custo_float)


@produtos_bp.route('/excluir/<int:id>', methods=['POST'])
@login_required
@admin_required
def excluir(id):
    from sqlalchemy.exc import IntegrityError
    produto = Produto.query.get_or_404(id)
    nome = produto.nome
    # Hard-delete so eh seguro pra produto sem vinculos. Se houver historico
    # (pedidos, vendas B2B, desperdicio), estoque ou mapeamentos de PDV
    # apontando pra ele, o FK aborta — nesse caso DESATIVA em vez de excluir
    # (preserva historico/estoque; o produto some da lista por ficar inativo).
    try:
        db.session.delete(produto)
        db.session.commit()
        flash(f'"{nome}" excluido!', 'success')
    except IntegrityError:
        db.session.rollback()
        produto.ativo = False
        db.session.commit()
        flash(f'"{nome}" tem histórico ou estoque vinculado e foi DESATIVADO '
              f'(removido do catálogo) em vez de excluído — assim nada do '
              f'histórico se perde.', 'warning')
    return redirect(url_for('produtos.lista'))


@produtos_bp.route('/cestas')
@login_required
def cestas():
    """Lista produtos compostos (cestas) com diagnóstico de componentes.

    Cestas configuradas: produto com >=1 ProdutoItem. Aparece com a lista
    resumida dos componentes.
    Sem componentes: produto ativo sem nenhum ProdutoItem — pode ser uma
    cesta esquecida OU um produto simples. Admin decide.
    """
    from sqlalchemy.orm import joinedload
    produtos = (Produto.query
                .filter_by(ativo=True)
                .options(joinedload(Produto.itens))
                .order_by(Produto.categoria, Produto.nome).all())
    com_componentes = []
    sem_componentes = []
    for p in produtos:
        n = len(p.itens) if p.itens else 0
        if n > 0:
            com_componentes.append({'produto': p, 'n_componentes': n})
        else:
            sem_componentes.append({'produto': p})
    return render_template('produtos/cestas.html',
                            com_componentes=com_componentes,
                            sem_componentes=sem_componentes)


@produtos_bp.route('/cestas/orfaos')
@login_required
@catalogo_required
def cestas_orfaos():
    """Lista ProdutoItems sem FK vinculada (tipo definido mas receita_id /
    materia_prima_id NULL). Esses componentes NAO baixam estoque na venda.

    Owner ve isso destacado no dashboard. Admin pode vincular manualmente
    aqui — selecionar Receita/MP do dropdown ou marcar como `removido`
    (caso o componente nao deveria estar na cesta).
    """
    from sqlalchemy import or_
    orfaos = (ProdutoItem.query
              .filter(or_(
                  (ProdutoItem.tipo == 'receita') & (ProdutoItem.receita_id.is_(None)),
                  (ProdutoItem.tipo == 'produto') & (ProdutoItem.produto_componente_id.is_(None)),
                  (ProdutoItem.tipo == 'mp') & (ProdutoItem.materia_prima_id.is_(None)),
              ))
              .all())
    # ativas(): orfao de cesta VIVA nao pode ser vinculado a receita
    # arquivada (varredura 19/07/2026 — a baixa de venda debitaria linha
    # morta). Produto e MP ao lado ja filtravam.
    receitas = Receita.ativas().order_by(Receita.nome).all()
    produtos = Produto.query.filter(Produto.ativo.is_(True)).order_by(Produto.nome).all()
    mps = MateriaPrima.ativas().order_by(MateriaPrima.nome).all()
    return render_template('produtos/cestas_orfaos.html',
                            orfaos=orfaos, receitas=receitas,
                            produtos=produtos, mps=mps)


@produtos_bp.route('/cestas/orfaos/<int:id>/vincular', methods=['POST'])
@login_required
@admin_required
def vincular_orfao(id):
    """Vincula um ProdutoItem orfao a uma Receita, Produto ou MateriaPrima."""
    pi = ProdutoItem.query.get_or_404(id)
    alvo = (request.form.get('alvo') or '').strip()
    if not alvo or ':' not in alvo:
        flash('Selecione um item.', 'warning')
        return redirect(url_for('produtos.cestas_orfaos'))
    tipo, id_str = alvo.split(':', 1)
    try:
        target_id = int(id_str)
    except ValueError:
        flash('ID invalido.', 'warning')
        return redirect(url_for('produtos.cestas_orfaos'))

    if tipo == 'receita':
        r = Receita.query.get(target_id)
        if not r:
            flash('Receita nao encontrada.', 'warning')
            return redirect(url_for('produtos.cestas_orfaos'))
        pi.tipo = 'receita'
        pi.receita_id = r.id
        pi.produto_componente_id = None
        pi.materia_prima_id = None
        pi.item_nome = r.nome
    elif tipo == 'produto':
        p = Produto.query.get(target_id)
        if not p:
            flash('Produto nao encontrado.', 'warning')
            return redirect(url_for('produtos.cestas_orfaos'))
        if p.id == pi.produto_id:
            flash('Cesta nao pode conter ela mesma como componente.', 'warning')
            return redirect(url_for('produtos.cestas_orfaos'))
        pi.tipo = 'produto'
        pi.produto_componente_id = p.id
        pi.receita_id = None
        pi.materia_prima_id = None
        pi.item_nome = p.nome
    elif tipo == 'mp':
        m = MateriaPrima.query.get(target_id)
        if not m:
            flash('MP nao encontrada.', 'warning')
            return redirect(url_for('produtos.cestas_orfaos'))
        pi.tipo = 'mp'
        pi.materia_prima_id = m.id
        pi.receita_id = None
        pi.produto_componente_id = None
        pi.item_nome = m.nome
    else:
        flash('Tipo invalido.', 'warning')
        return redirect(url_for('produtos.cestas_orfaos'))

    db.session.commit()
    flash(f'Componente vinculado a "{pi.nome_resolvido}".', 'success')
    return redirect(url_for('produtos.cestas_orfaos'))


@produtos_bp.route('/cestas/orfaos/<int:id>/excluir', methods=['POST'])
@login_required
@admin_required
def excluir_orfao(id):
    """Remove um ProdutoItem orfao da cesta (caso o componente nao deveria
    estar la — ex: receita que foi deletada do catalogo)."""
    pi = ProdutoItem.query.get_or_404(id)
    nome = pi.item_nome
    db.session.delete(pi)
    db.session.commit()
    flash(f'Componente "{nome}" removido da cesta.', 'success')
    return redirect(url_for('produtos.cestas_orfaos'))


# ── Cadastro assistido por IA (08/07/2026, pedido do dono) ──────────────
# Cola um print/lista (imagem ou texto), a IA propõe produtos com
# componentes inferidos dos parecidos já cadastrados, o humano REVISA na
# tabela e só então salva. Ver app/services/cadastro_ia.py.

_CADASTRO_IA_MIMETYPES = {'image/jpeg', 'image/png', 'image/webp',
                          'image/gif'}


@produtos_bp.route('/cadastro-ia')
@login_required
@admin_required
def cadastro_ia():
    from app.services import cadastro_ia as svc_ia
    return render_template('produtos/cadastro_ia.html', itens=None,
                           campo_preco='preco_site',
                           CAMPOS_PRECO=svc_ia.CAMPOS_PRECO)


@produtos_bp.route('/cadastro-ia/analisar', methods=['POST'])
@login_required
@admin_required
def cadastro_ia_analisar():
    from app.services import cadastro_ia as svc_ia
    campo_preco = request.form.get('campo_preco') or 'preco_site'
    if campo_preco not in svc_ia.CAMPOS_PRECO:
        campo_preco = 'preco_site'
    texto = (request.form.get('texto') or '').strip()
    arquivo = request.files.get('imagem')
    file_bytes = mimetype = None
    if arquivo and arquivo.filename:
        if arquivo.mimetype not in _CADASTRO_IA_MIMETYPES:
            flash('Formato não suportado — mande JPG/PNG/WebP ou cole o '
                  'texto.', 'warning')
            return redirect(url_for('produtos.cadastro_ia'))
        file_bytes = arquivo.read()
        # 5 MB: limite por imagem da API Anthropic (o base64 ainda infla
        # ~33%) — acima disso a falha viraria erro cru do SDK.
        if len(file_bytes) > 5 * 1024 * 1024:
            flash('Imagem acima de 5 MB — reduza e tente de novo.',
                  'warning')
            return redirect(url_for('produtos.cadastro_ia'))
        mimetype = arquivo.mimetype
    if not file_bytes and not texto:
        flash('Mande uma imagem ou cole o texto da lista.', 'warning')
        return redirect(url_for('produtos.cadastro_ia'))

    out = svc_ia.analisar(file_bytes=file_bytes, mimetype=mimetype,
                          texto=texto or None)
    if out.get('erro'):
        flash(f'Análise falhou: {out["erro"]}', 'danger')
        return redirect(url_for('produtos.cadastro_ia'))
    for aviso in out.get('avisos') or []:
        flash(aviso, 'warning')
    return render_template('produtos/cadastro_ia.html',
                           itens=out['itens'], campo_preco=campo_preco,
                           CAMPOS_PRECO=svc_ia.CAMPOS_PRECO,
                           modelo_usado=out.get('modelo_usado'))


@produtos_bp.route('/cadastro-ia/salvar', methods=['POST'])
@login_required
@admin_required
def cadastro_ia_salvar():
    """Grava o que o humano marcou na revisão. Cada linha volta com o JSON
    da proposta (hidden) + overrides editáveis (nome/preço/categoria,
    checkbox por item e por componente, quantidade)."""
    import json as _json

    from flask_login import current_user

    from app.services import cadastro_ia as svc_ia
    campo_preco = request.form.get('campo_preco') or 'preco_site'
    if campo_preco not in svc_ia.CAMPOS_PRECO:
        flash('Campo de preço inválido.', 'danger')
        return redirect(url_for('produtos.cadastro_ia'))
    try:
        n = min(int(request.form.get('n_itens') or 0), 500)
    except ValueError:
        n = 0

    def _num(bruto, padrao):
        """parse_float_br levanta ValueError em valor presente porém
        inválido ("abc") — aqui mantém o valor da proposta e avisa, em
        vez de derrubar a revisão inteira com 500."""
        try:
            v = parse_float_br(bruto)
        except ValueError:
            flash(f'Valor "{bruto}" inválido — mantive o proposto.',
                  'warning')
            return padrao
        return padrao if v is None else v

    itens = []
    for i in range(n):
        if not request.form.get(f'it{i}_incluir'):
            continue
        try:
            it = _json.loads(request.form.get(f'it{i}_json') or '{}')
        except ValueError:
            # item MARCADO com dados corrompidos: nunca sumir em silêncio
            flash(f'Item {i + 1} ignorado: dados corrompidos — rode a '
                  'análise de novo.', 'danger')
            continue
        it['nome'] = (request.form.get(f'it{i}_nome') or
                      it.get('nome') or '').strip()
        it['preco'] = _num(request.form.get(f'it{i}_preco'),
                           it.get('preco') or 0)
        it['categoria'] = (request.form.get(f'it{i}_categoria') or '').strip()
        comps = []
        for j, c in enumerate(it.get('componentes') or []):
            if not request.form.get(f'it{i}_c{j}_incluir'):
                continue
            c['quantidade'] = _num(request.form.get(f'it{i}_c{j}_qtd'),
                                   c.get('quantidade') or 1)
            comps.append(c)
        it['componentes'] = comps
        itens.append(it)
    if not itens:
        flash('Nenhum item marcado para cadastrar.', 'warning')
        return redirect(url_for('produtos.cadastro_ia'))

    resumo = svc_ia.salvar_lote(itens, campo_preco, user=current_user)
    if resumo['criados']:
        flash(f'{len(resumo["criados"])} produto(s) criado(s): '
              + ', '.join(resumo['criados']), 'success')
    if resumo['mps_criadas']:
        flash(f'{len(resumo["mps_criadas"])} MP(s) nova(s): '
              + ', '.join(resumo['mps_criadas']), 'info')
    if resumo['pulados']:
        flash('Já existiam (pulados): ' + ', '.join(resumo['pulados']),
              'warning')
    for aviso in resumo['avisos']:
        flash(aviso, 'warning')
    return redirect(url_for('produtos.lista'))
