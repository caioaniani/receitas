import json
from datetime import date

from flask import redirect, url_for, jsonify, request, Response, render_template
from flask_login import login_required, current_user

from app.blueprints.main import main_bp
from app.decorators import admin_required
from app.extensions import db, csrf
from app.models import (MateriaPrima, Receita, ReceitaIngrediente, Produto, ProdutoItem,
                        Funcionario, Atribuicao, AlertaEstoque, PlanejamentoProducao,
                        AuditLog, Usuario, PedidoLoja, PedidoLocal, AtribuicaoEntrega,
                        MovimentacaoEstoque, Driver, Loja, Fornecedor, HistoricoPrecoMP)
from app.services.custos import calcular_custos_receitas, calcular_rendimento


@main_bp.route('/')
@login_required
def index():
    if not current_user.is_admin():
        return redirect(url_for('auth.minhas_fichas'))
    return render_template('main/home.html')


@main_bp.route('/dashboard')
@login_required
@admin_required
def dashboard():
    resultado = calcular_custos_receitas()
    custos_map = resultado.get('custos', {})
    receitas = Receita.query.all()

    custo_mp_total = sum(custos_map.values())
    receita_estimada = sum((r.preco_venda or 0) for r in receitas if r.preco_venda)

    funcionarios_ativos = Funcionario.query.filter_by(ativo=True).all()
    custo_mao_obra = sum(f.custo_total() for f in funcionarios_ativos)

    margem_geral = 0
    if receita_estimada > 0:
        margem_geral = (receita_estimada - custo_mp_total) / receita_estimada * 100

    alertas_estoque = db.session.query(AlertaEstoque).join(MateriaPrima).filter(
        MateriaPrima.estoque_atual < AlertaEstoque.estoque_minimo
    ).count()

    producoes_pendentes = PlanejamentoProducao.query.filter_by(status='rascunho').count()
    atribuicoes_pendentes = Atribuicao.query.filter_by(status='pendente').count()

    hoje = date.today()
    aniversariantes = [f for f in funcionarios_ativos
                       if f.data_nascimento and f.data_nascimento.month == hoje.month]

    return render_template('main/dashboard.html',
                           custo_mp_total=custo_mp_total,
                           receita_estimada=receita_estimada,
                           custo_mao_obra=custo_mao_obra,
                           margem_geral=margem_geral,
                           alertas_estoque=alertas_estoque,
                           producoes_pendentes=producoes_pendentes,
                           atribuicoes_pendentes=atribuicoes_pendentes,
                           aniversariantes=aniversariantes,
                           total_funcionarios=len(funcionarios_ativos))


@main_bp.route('/rentabilidade')
@login_required
def rentabilidade():
    resultado = calcular_custos_receitas()
    custos_receita = resultado['custos']
    receitas = Receita.query.order_by(Receita.categoria, Receita.nome).all()

    dados = []
    for r in receitas:
        custo_un = custos_receita.get(r.nome, 0)
        rendimento = calcular_rendimento(r)
        custo_total = custo_un * rendimento

        preco_at = r.preco_venda or 0
        lucro_at = preco_at - custo_un if preco_at > 0 else None
        margem_at = (lucro_at / preco_at * 100) if (preco_at > 0 and lucro_at is not None) else None

        preco_lj = r.preco_loja or 0
        lucro_lj = preco_lj - custo_un if preco_lj > 0 else None
        margem_lj = (lucro_lj / preco_lj * 100) if (preco_lj > 0 and lucro_lj is not None) else None

        preco_st = r.preco_site or 0
        lucro_st = preco_st - custo_un if preco_st > 0 else None
        margem_st = (lucro_st / preco_st * 100) if (preco_st > 0 and lucro_st is not None) else None

        dados.append({
            'id': r.id,
            'nome': r.nome,
            'categoria': r.categoria or 'Outros',
            'rendimento': rendimento,
            'custo_total': custo_total,
            'custo_un': custo_un,
            'preco_atacado': preco_at,
            'lucro_atacado': lucro_at,
            'margem_atacado': margem_at,
            'preco_loja': preco_lj,
            'lucro_loja': lucro_lj,
            'margem_loja': margem_lj,
            'preco_site': preco_st,
            'lucro_site': lucro_st,
            'margem_site': margem_st,
        })

    return render_template('main/rentabilidade.html', dados=dados)


@main_bp.route('/cardapio')
@login_required
def cardapio():
    tipo = request.args.get('tipo', 'atacado')
    # defer(imagem_blob/mimetype) — listagem nao precisa do blob (pode ter
    # 100KB+ cada, estoura RAM do worker). IDs com blob vem em query separada.
    from sqlalchemy.orm import defer
    receitas = Receita.query.options(
        defer(Receita.imagem_blob),
        defer(Receita.imagem_mimetype),
    ).order_by(Receita.categoria, Receita.nome).all()
    produtos = Produto.query.options(
        defer(Produto.imagem_blob),
        defer(Produto.imagem_mimetype),
    ).filter_by(ativo=True).order_by(Produto.categoria, Produto.nome).all()

    receitas_com_blob = {r[0] for r in db.session.query(Receita.id)
                         .filter(Receita.imagem_blob.isnot(None)).all()}
    produtos_com_blob = {p[0] for p in db.session.query(Produto.id)
                         .filter(Produto.imagem_blob.isnot(None)).all()}

    campo = {'atacado': 'preco_venda', 'loja': 'preco_loja', 'site': 'preco_site'}
    attr = campo.get(tipo, 'preco_venda')

    categorias = {}

    # Receitas fabricadas
    for r in receitas:
        preco = getattr(r, attr, None) or (r.preco_venda if tipo == 'atacado' else None)
        if not preco or preco <= 0:
            continue
        cat = r.categoria or 'Outros'
        if cat not in categorias:
            categorias[cat] = []
        if r.id in receitas_com_blob:
            img = url_for('main.cardapio_img', tipo='receita', id=r.id)
        else:
            img = r.imagem_url
        categorias[cat].append({
            'nome': r.nome,
            'peso_unitario': r.peso_unitario,
            'descricao': None,
            'preco_venda': preco,
            'imagem_url': img,
        })

    # Produtos cadastrados (cestas, kits, etc.)
    campo_prod = {'atacado': 'preco_atacado', 'loja': 'preco_loja', 'site': 'preco_site'}
    attr_prod = campo_prod.get(tipo, 'preco_atacado')
    for p in produtos:
        preco = getattr(p, attr_prod, None)
        if not preco or preco <= 0:
            continue
        cat = p.categoria or 'Outros'
        if cat not in categorias:
            categorias[cat] = []
        if p.id in produtos_com_blob:
            img = url_for('main.cardapio_img', tipo='produto', id=p.id)
        else:
            img = p.imagem_url
        categorias[cat].append({
            'nome': p.nome,
            'peso_unitario': None,
            'descricao': p.descricao,
            'preco_venda': preco,
            'imagem_url': img,
        })

    return render_template('main/cardapio.html', categorias=categorias, tipo=tipo)


@main_bp.route('/cardapio-img/<tipo>/<int:id>')
def cardapio_img(tipo, id):
    """Serve a imagem (BLOB) de uma receita/produto. Publico pra carregar
    no <img src>. Suporta ETag + 304 pra evitar reenviar BLOB toda hora."""
    from flask import abort, request as flask_request, make_response
    from app.models import Receita, Produto
    from sqlalchemy.orm import load_only
    import hashlib
    if tipo == 'receita':
        obj = (Receita.query.options(
            load_only(Receita.imagem_blob, Receita.imagem_mimetype)
        ).get(id))
    elif tipo == 'produto':
        obj = (Produto.query.options(
            load_only(Produto.imagem_blob, Produto.imagem_mimetype)
        ).get(id))
    else:
        abort(404)
    if not obj or not obj.imagem_blob:
        abort(404)
    etag = hashlib.md5(obj.imagem_blob).hexdigest()[:16]
    if flask_request.headers.get('If-None-Match') == etag:
        return ('', 304)
    resp = make_response(obj.imagem_blob)
    resp.mimetype = obj.imagem_mimetype or 'image/jpeg'
    resp.headers['Cache-Control'] = 'public, max-age=86400'
    resp.headers['ETag'] = etag
    return resp


@main_bp.route('/cardapio-img/<tipo>/<int:id>/upload', methods=['POST'])
@login_required
def cardapio_img_upload(tipo, id):
    """Recebe upload de foto pra receita/produto. PIL comprime
    automaticamente: redimensiona pra 700px max + JPEG quality 82.
    Aceita ate 25MB no upload (celular tira fotos enormes), mas o que
    fica no banco e ~50-150KB."""
    from flask import abort, flash, redirect, url_for
    from app.models import Receita, Produto
    from app.extensions import db as _db
    if not current_user.is_admin():
        abort(403)
    if tipo == 'receita':
        obj = Receita.query.get_or_404(id)
        url_back = url_for('receitas.ficha', id=id)
    elif tipo == 'produto':
        obj = Produto.query.get_or_404(id)
        url_back = url_for('produtos.detalhe', id=id)
    else:
        abort(404)

    f = request.files.get('imagem_arquivo')
    if not f or not f.filename:
        flash('Selecione um arquivo de imagem.', 'danger')
        return redirect(url_back)
    if not (f.mimetype or '').startswith('image/'):
        flash('Arquivo nao eh imagem.', 'danger')
        return redirect(url_back)
    data = f.read()
    if not data:
        flash('Arquivo vazio.', 'danger')
        return redirect(url_back)
    if len(data) > 25 * 1024 * 1024:
        flash(f'Imagem muito grande ({len(data)//1024//1024}MB > 25MB). '
              'Tira de novo com qualidade menor.', 'danger')
        return redirect(url_back)

    # Compressao: PIL reduz pra 700x700 max e converte pra JPEG quality 82.
    # Aplica EXIF orientation pra fotos de celular nao virarem deitadas.
    try:
        from PIL import Image, ImageOps
        import io as _io
        img = Image.open(_io.BytesIO(data))
        img = ImageOps.exif_transpose(img)  # corrige rotacao de iPhone/Android
        if img.mode in ('RGBA', 'P', 'LA'):
            img = img.convert('RGB')
        img.thumbnail((700, 700), Image.LANCZOS)
        out = _io.BytesIO()
        img.save(out, format='JPEG', quality=82, optimize=True, progressive=True)
        final = out.getvalue()
        obj.imagem_blob = final
        obj.imagem_mimetype = 'image/jpeg'
        tamanho_kb = len(final) // 1024
    except Exception as e:  # noqa: BLE001
        flash(f'Erro processando imagem: {e}', 'danger')
        return redirect(url_back)

    _db.session.commit()
    flash('Imagem salva.', 'success')
    return redirect(url_back)


def _norm(s):
    import unicodedata
    if not s:
        return ''
    nfd = unicodedata.normalize('NFD', s)
    return ''.join(c for c in nfd if unicodedata.category(c) != 'Mn').lower().strip()


@main_bp.route('/cardapio-img/revisar')
@login_required
def cardapio_img_revisar():
    """Grid de revisao das fotos atribuidas. Admin ve thumbnail + nome,
    identifica matches errados e remove com 1 clique. Defer blob pra nao
    estourar RAM — o thumbnail eh servido pela rota /cardapio-img/<tipo>/<id>."""
    from flask import abort
    from sqlalchemy.orm import defer
    if not current_user.is_admin():
        abort(403)
    receitas_com_foto = (Receita.query
                         .options(defer(Receita.imagem_blob),
                                  defer(Receita.imagem_mimetype))
                         .filter(Receita.imagem_blob.isnot(None))
                         .order_by(Receita.categoria, Receita.nome).all())
    produtos_com_foto = (Produto.query
                         .options(defer(Produto.imagem_blob),
                                  defer(Produto.imagem_mimetype))
                         .filter(Produto.ativo.is_(True),
                                 Produto.imagem_blob.isnot(None))
                         .order_by(Produto.categoria, Produto.nome).all())
    return render_template('main/cardapio_revisar.html',
                            receitas=receitas_com_foto,
                            produtos=produtos_com_foto)


@main_bp.route('/cardapio-img/<tipo>/<int:id>/remover', methods=['POST'])
@login_required
def cardapio_img_remover(tipo, id):
    from flask import abort, flash, redirect, url_for
    from app.models import Receita, Produto
    from app.extensions import db as _db
    if not current_user.is_admin():
        abort(403)
    if tipo == 'receita':
        obj = Receita.query.get_or_404(id)
        url_back = url_for('receitas.ficha', id=id)
    elif tipo == 'produto':
        obj = Produto.query.get_or_404(id)
        url_back = url_for('produtos.detalhe', id=id)
    else:
        abort(404)
    # Permite redirect pra revisar (next=revisar) em vez da ficha
    if request.form.get('next') == 'revisar':
        url_back = url_for('main.cardapio_img_revisar')
    obj.imagem_blob = None
    obj.imagem_mimetype = None
    obj.imagem_url = None
    _db.session.commit()
    flash('Imagem removida.', 'info')
    return redirect(url_back)


@main_bp.route('/admin/popular-imagens-cardapio', methods=['GET'])
@login_required
def popular_imagens_preview():
    """Mostra tabela com sugestoes de match (nome banco → URL do Rappi)
    pra admin revisar antes de aplicar. Aceita ?threshold=N (default 70)
    e ?incluir_ja_tem=1 pra revisar tambem as que ja tem imagem."""
    if not current_user.is_admin():
        from flask import abort
        abort(403)
    from app.services.cardapio_imagens import preview_matches
    try:
        threshold = int(request.args.get('threshold', 70))
    except ValueError:
        threshold = 70
    threshold = max(50, min(100, threshold))
    incluir_ja_tem = bool(request.args.get('incluir_ja_tem'))
    todos = preview_matches(threshold=threshold)
    if not incluir_ja_tem:
        todos = [it for it in todos if not it['ja_tem']]
    com_match = [it for it in todos if it.get('url')]
    sem_match = [it for it in todos if not it.get('url')]
    return render_template('main/popular_imagens.html',
                            com_match=com_match, sem_match=sem_match,
                            threshold=threshold,
                            incluir_ja_tem=incluir_ja_tem)


@main_bp.route('/admin/popular-imagens-cardapio', methods=['POST'])
@login_required
def popular_imagens_cardapio():
    """Aplica imagens APROVADAS no preview. Form passa aprovados[] como
    'receita:5' / 'produto:12'. Sem checkbox marcado = nao aplica."""
    if not current_user.is_admin():
        from flask import abort
        abort(403)
    from app.services.cardapio_imagens import popular_imagens
    from flask import flash, redirect, url_for
    aprovados_raw = request.form.getlist('aprovados[]')
    ids_aprovados = set()
    for ref in aprovados_raw:
        tipo, _, sid = ref.partition(':')
        if tipo in ('receita', 'produto') and sid.isdigit():
            ids_aprovados.add((tipo, int(sid)))
    try:
        threshold = int(request.form.get('threshold', 70))
    except ValueError:
        threshold = 70
    resultado = popular_imagens(sobrescrever=False, threshold=threshold,
                                  ids_aprovados=ids_aprovados or None)
    flash(
        f'Imagens populadas: {resultado["receitas_alteradas"]} receita(s) + '
        f'{resultado["produtos_alterados"]} produto(s).',
        'success',
    )
    return redirect(url_for('main.cardapio'))


@main_bp.route('/api/exportar')
@login_required
def exportar():
    mps = MateriaPrima.query.order_by(MateriaPrima.id).all()
    receitas = Receita.query.order_by(Receita.id).all()
    produtos = Produto.query.order_by(Produto.id).all()

    data = {
        'materias_primas': [mp.to_dict() for mp in mps],
        'receitas': [r.to_dict() for r in receitas],
        'produtos': [p.to_dict() for p in produtos],
    }

    json_str = json.dumps(data, ensure_ascii=False, indent=2)
    return Response(
        json_str,
        mimetype='application/json',
        headers={'Content-Disposition': 'attachment; filename=padaria_backup.json'}
    )


@main_bp.route('/api/importar', methods=['POST'])
@login_required
@admin_required
def importar():
    file = request.files.get('file')
    if not file:
        return jsonify(success=False, error='Nenhum arquivo enviado')

    try:
        data = json.loads(file.read().decode('utf-8'))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return jsonify(success=False, error='Arquivo JSON inválido')

    # Validar estrutura antes de apagar qualquer coisa
    if 'materias_primas' not in data or 'receitas' not in data:
        return jsonify(success=False, error='Arquivo nao tem a estrutura esperada')

    try:
        # Limpa tudo
        ProdutoItem.query.delete()
        ReceitaIngrediente.query.delete()
        Receita.query.delete()
        MateriaPrima.query.delete()
        Produto.query.delete()

        # Recria matérias-primas
        for mp_data in data.get('materias_primas', []):
            mp = MateriaPrima(
                nome=mp_data['nome'],
                unidade=mp_data.get('unidade', 'g'),
                custo_por_kg=mp_data['custo_por_kg'],
                fornecedor=mp_data.get('fornecedor') or None,
                observacoes=mp_data.get('observacoes') or None,
            )
            db.session.add(mp)

        db.session.flush()

        # Recria receitas
        for r_data in data.get('receitas', []):
            receita = Receita(
                nome=r_data['nome'],
                categoria=r_data.get('categoria') or None,
                preco_venda=r_data.get('preco_venda'),
                preco_loja=r_data.get('preco_loja'),
                preco_site=r_data.get('preco_site'),
                rendimento_qtd=r_data['rendimento_qtd'],
                rendimento_unidade=r_data['rendimento_unidade'],
                peso_base=r_data['peso_base'],
                peso_unitario=r_data.get('peso_unitario'),
                perda_percentual=r_data.get('perda_percentual', 0),
                custo_embalagem=r_data.get('custo_embalagem', 0),
                modo_preparo=r_data.get('modo_preparo') or None,
                observacao=r_data.get('observacao') or None,
            )
            db.session.add(receita)
            db.session.flush()

            for ing_data in r_data.get('ingredientes', []):
                ing = ReceitaIngrediente(
                    receita_id=receita.id,
                    tipo=ing_data.get('tipo', 'mp'),
                    ingrediente_nome=ing_data['ingrediente_nome'],
                    porcentagem=ing_data['porcentagem'],
                    eh_base=ing_data.get('eh_base', False),
                    nota=ing_data.get('nota') or None,
                )
                db.session.add(ing)

        # Recria produtos (cestas, kits, etc.)
        for p_data in data.get('produtos', []):
            produto = Produto(
                nome=p_data['nome'],
                categoria=p_data.get('categoria') or None,
                descricao=p_data.get('descricao') or None,
                preco_atacado=p_data.get('preco_atacado'),
                preco_loja=p_data.get('preco_loja'),
                preco_site=p_data.get('preco_site'),
                custo_direto=p_data.get('custo_direto'),
                custo_embalagem=p_data.get('custo_embalagem', 0),
                modo_preparo=p_data.get('modo_preparo') or None,
                observacao=p_data.get('observacao') or None,
                ativo=p_data.get('ativo', True),
            )
            db.session.add(produto)
            db.session.flush()

            for item_data in p_data.get('itens', []):
                item = ProdutoItem(
                    produto_id=produto.id,
                    tipo=item_data['tipo'],
                    item_nome=item_data['item_nome'],
                    quantidade=item_data['quantidade'],
                )
                db.session.add(item)

        db.session.commit()
        return jsonify(success=True)

    except Exception:
        db.session.rollback()
        return jsonify(success=False, error='Erro ao importar dados. Verifique o formato do arquivo.')


@main_bp.route('/todo')
@login_required
@admin_required
def todo():
    receitas = Receita.query.filter(
        Receita.observacao.isnot(None), Receita.observacao != ''
    ).order_by(Receita.nome).all()
    produtos = Produto.query.filter(
        Produto.observacao.isnot(None), Produto.observacao != ''
    ).order_by(Produto.nome).all()
    return render_template('main/todo.html', receitas=receitas, produtos=produtos)


@main_bp.route("/audit")
@login_required
@admin_required
def audit():
    """Visualizador do audit log. So admin pode ver."""
    import json as _json
    tabela_f = request.args.get("tabela") or None
    usuario_f = request.args.get("usuario_id", type=int)
    acao_f = request.args.get("acao") or None
    q = AuditLog.query
    if tabela_f:
        q = q.filter_by(tabela=tabela_f)
    if usuario_f:
        q = q.filter_by(usuario_id=usuario_f)
    if acao_f in ("insert", "update", "delete"):
        q = q.filter_by(acao=acao_f)
    logs = q.order_by(AuditLog.criado_em.desc()).limit(200).all()
    # Parse JSON dos campos antes/depois pra exibir formatado
    rows = []
    for l in logs:
        try:
            antes = _json.loads(l.antes) if l.antes else None
        except Exception:
            antes = None
        try:
            depois = _json.loads(l.depois) if l.depois else None
        except Exception:
            depois = None
        rows.append({
            "log": l, "antes": antes, "depois": depois,
        })
    # Lista de tabelas e usuarios pra filtros
    tabelas = [r[0] for r in db.session.query(AuditLog.tabela).distinct().all()]
    usuarios = Usuario.query.order_by(Usuario.nome).all()
    return render_template("main/audit.html", rows=rows, tabelas=sorted(tabelas),
                           usuarios=usuarios, filtros={"tabela": tabela_f,
                           "usuario_id": usuario_f, "acao": acao_f})



@main_bp.route('/caixa')
@login_required
@admin_required
def caixa():
    """Dashboard de caixa diario: agrega dados LOCAIS do banco.
    Vendas PDV (Seru) NAO entram aqui pra evitar chamadas externas
    lentas — use /pdv pra esse detalhe."""
    from datetime import date, timedelta
    from sqlalchemy import func as sqlfunc

    data_str = request.args.get('data', date.today().isoformat())
    try:
        data_alvo = datetime.strptime(data_str, '%Y-%m-%d').date()
    except ValueError:
        data_alvo = date.today()

    ontem = data_alvo - timedelta(days=1)
    semana_atras = data_alvo - timedelta(days=7)

    def metricas_do_dia(d):
        """Sumario de um dia: pedidos locais, pedidos loja, entregas,
        movimentacoes de estoque."""
        # Pedidos locais (entregas avulsas) — tem valor_total
        locais = PedidoLocal.query.filter(PedidoLocal.data_entrega == d).all()
        valor_locais = sum(p.valor_total for p in locais)

        # Pedidos entre lojas — quantidade
        pedidos_loja = PedidoLoja.query.filter(PedidoLoja.data_entrega == d).all()
        n_ped_loja = len(pedidos_loja)
        n_ped_loja_entregue = sum(1 for p in pedidos_loja if p.status == 'entregue')

        # Entregas atribuidas — quantidade + entregues
        atribs = AtribuicaoEntrega.query.filter(AtribuicaoEntrega.data_entrega == d).all()
        n_entregas = len(atribs)
        n_entregas_feitas = sum(1 for a in atribs if a.status == 'entregue')
        n_entregas_falhas = sum(1 for a in atribs if a.status == 'nao_entregue')

        # Entradas de MP (compras) — valor + count
        movs = MovimentacaoEstoque.query.filter(
            MovimentacaoEstoque.tipo == 'entrada',
            sqlfunc.date(MovimentacaoEstoque.data) == d,
        ).all()
        valor_compras = sum((m.quantidade or 0) * (m.preco_unitario or 0) for m in movs)
        n_compras = len(movs)

        return {
            'data': d,
            'valor_locais': valor_locais,
            'n_locais': len(locais),
            'n_ped_loja': n_ped_loja,
            'n_ped_loja_entregue': n_ped_loja_entregue,
            'n_entregas': n_entregas,
            'n_entregas_feitas': n_entregas_feitas,
            'n_entregas_falhas': n_entregas_falhas,
            'valor_compras': valor_compras,
            'n_compras': n_compras,
        }

    hoje_m = metricas_do_dia(data_alvo)
    ontem_m = metricas_do_dia(ontem)
    semana_m = metricas_do_dia(semana_atras)

    def delta_pct(atual, anterior):
        if not anterior:
            return None
        return ((atual - anterior) / anterior) * 100

    return render_template('main/caixa.html',
                           hoje=hoje_m, ontem=ontem_m, semana=semana_m,
                           data_alvo=data_alvo, ontem_data=ontem,
                           semana_data=semana_atras,
                           delta_locais=delta_pct(hoje_m['valor_locais'], ontem_m['valor_locais']),
                           delta_entregas=delta_pct(hoje_m['n_entregas'], ontem_m['n_entregas']))
