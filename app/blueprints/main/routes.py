import json
from datetime import datetime, timedelta

from flask import (
    Response,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required
from sqlalchemy.orm import joinedload

from app.blueprints.main import main_bp
from app.decorators import admin_required, owner_required
from app.extensions import db
from app.models import (
    AlertaEstoque,
    Atribuicao,
    AtribuicaoEntrega,
    AuditLog,
    Funcionario,
    MateriaPrima,
    MovimentacaoEstoque,
    PedidoLocal,
    PedidoLoja,
    PlanejamentoProducao,
    Produto,
    ProdutoItem,
    Receita,
    ReceitaIngrediente,
    Usuario,
)
from app.services.custos import calcular_custos_receitas, calcular_rendimento
from app.utils import agora
from app.utils import hoje as hoje_brt


@main_bp.route('/')
@login_required
def index():
    if current_user.is_padeiro():
        return redirect(url_for('padeiro.index'))
    if current_user.is_admin():
        return render_template('main/home.html')
    return render_template('main/inicio.html')


@main_bp.route('/dashboard')
@login_required
@admin_required
def dashboard():
    resultado = calcular_custos_receitas()
    custos_map = resultado.get('custos', {})
    receitas = Receita.query.all()

    custo_mp_total = sum(custos_map.values())
    receita_estimada = sum((r.preco_venda or 0) for r in receitas if r.preco_venda)

    # Eager load do cargo evita N+1 — `custo_total()` acessa `self.cargo.salario_base`.
    funcionarios_ativos = (Funcionario.query
                            .options(joinedload(Funcionario.cargo))
                            .filter_by(ativo=True).all())
    custo_mao_obra = sum(f.custo_total() for f in funcionarios_ativos)

    margem_geral = 0
    if receita_estimada > 0:
        margem_geral = (receita_estimada - custo_mp_total) / receita_estimada * 100

    alertas_estoque = db.session.query(AlertaEstoque).join(MateriaPrima).filter(
        MateriaPrima.estoque_atual < AlertaEstoque.estoque_minimo
    ).count()

    producoes_pendentes = PlanejamentoProducao.query.filter_by(status='rascunho').count()
    atribuicoes_pendentes = Atribuicao.query.filter_by(status='pendente').count()

    # ProdutoItem orfaos: cestas com componente sem FK vinculada.
    # Esses componentes NAO baixam estoque na venda — owner precisa
    # vincular manualmente em /cestas/orfaos.
    from app.services.cestas import contar_produto_itens_orfaos
    cestas_orfaos = contar_produto_itens_orfaos() if current_user.is_owner else 0

    # Pendencias do sync PDV (lojas/produtos nao mapeados travam baixa de
    # estoque na venda). So owner ve — link pro painel /pdv/saude.
    pdv_pendencias = 0
    if current_user.is_owner:
        try:
            from app.services import pdv_saude
            pdv_pendencias = pdv_saude.contar_pendencias()
        except Exception:  # noqa: BLE001
            pdv_pendencias = 0

    hoje = hoje_brt()
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
                           cestas_orfaos=cestas_orfaos,
                           pdv_pendencias=pdv_pendencias,
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
    # defer(imagem_blob) — listagem nao precisa do blob (pode ter 100KB+ cada).
    # IDs com foto (blob OU Dropbox URL) vem em query separada.
    from sqlalchemy.orm import defer
    receitas = Receita.query.options(
        defer(Receita.imagem_blob),
        defer(Receita.imagem_mimetype),
    ).order_by(Receita.categoria, Receita.nome).all()
    produtos = Produto.query.options(
        defer(Produto.imagem_blob),
        defer(Produto.imagem_mimetype),
    ).filter_by(ativo=True).order_by(Produto.categoria, Produto.nome).all()

    from sqlalchemy import or_
    receitas_com_foto = {r[0] for r in db.session.query(Receita.id).filter(
        or_(Receita.imagem_blob.isnot(None),
            Receita.imagem_dropbox_url.isnot(None))).all()}
    produtos_com_foto = {p[0] for p in db.session.query(Produto.id).filter(
        or_(Produto.imagem_blob.isnot(None),
            Produto.imagem_dropbox_url.isnot(None))).all()}

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
        if r.id in receitas_com_foto:
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
        if p.id in produtos_com_foto:
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
    """Serve imagem de receita/produto. Prioriza Dropbox URL (M6+).

    Fallback pra BLOB do banco se foto ainda nao foi migrada.
    """
    import hashlib

    from flask import abort, make_response
    from flask import request as flask_request
    from sqlalchemy.orm import load_only

    from app.models import Produto, Receita
    if tipo == 'receita':
        obj = (Receita.query.options(
            load_only(Receita.imagem_blob, Receita.imagem_mimetype,
                      Receita.imagem_dropbox_url)
        ).get(id))
    elif tipo == 'produto':
        obj = (Produto.query.options(
            load_only(Produto.imagem_blob, Produto.imagem_mimetype,
                      Produto.imagem_dropbox_url)
        ).get(id))
    else:
        abort(404)
    if not obj:
        abort(404)
    if obj.imagem_dropbox_url:
        return redirect(obj.imagem_dropbox_url, code=302)
    if not obj.imagem_blob:
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

    from app.extensions import db as _db
    from app.models import Produto, Receita
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

    from app.services import dropbox_storage
    from app.utils import comprimir_imagem
    try:
        final = comprimir_imagem(data)
        tamanho_kb = len(final) // 1024
        if dropbox_storage.disponivel():
            # Path deterministico — overwrite ao re-upload do mesmo item.
            path = f'/cardapio/{tipo}/{obj.id}.jpg'
            info = dropbox_storage.upload_publico(
                final, path, mode='overwrite', autorename=False)
            obj.imagem_dropbox_url = info['url']
            obj.imagem_storage_path = info['storage_path']
            obj.imagem_blob = None  # libera legado
        else:
            obj.imagem_blob = final
        obj.imagem_mimetype = 'image/jpeg'
    except Exception as e:  # noqa: BLE001
        flash(f'Erro processando imagem: {e}', 'danger')
        return redirect(url_back)

    _db.session.commit()
    flash(f'Imagem salva ({tamanho_kb} KB apos compressao).', 'success')
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
    from sqlalchemy import or_
    from sqlalchemy.orm import defer
    if not current_user.is_admin():
        abort(403)
    receitas_com_foto = (Receita.query
                         .options(defer(Receita.imagem_blob),
                                  defer(Receita.imagem_mimetype))
                         .filter(or_(Receita.imagem_blob.isnot(None),
                                     Receita.imagem_dropbox_url.isnot(None)))
                         .order_by(Receita.categoria, Receita.nome).all())
    produtos_com_foto = (Produto.query
                         .options(defer(Produto.imagem_blob),
                                  defer(Produto.imagem_mimetype))
                         .filter(Produto.ativo.is_(True))
                         .filter(or_(Produto.imagem_blob.isnot(None),
                                     Produto.imagem_dropbox_url.isnot(None)))
                         .order_by(Produto.categoria, Produto.nome).all())
    return render_template('main/cardapio_revisar.html',
                            receitas=receitas_com_foto,
                            produtos=produtos_com_foto)


@main_bp.route('/cardapio-img/<tipo>/<int:id>/remover', methods=['POST'])
@login_required
def cardapio_img_remover(tipo, id):
    from flask import abort, flash, redirect, url_for

    from app.extensions import db as _db
    from app.models import Produto, Receita
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
    # Delete Dropbox file best-effort antes de limpar refs
    if obj.imagem_storage_path:
        from app.services import dropbox_storage
        dropbox_storage.deletar(obj.imagem_storage_path)
    obj.imagem_blob = None
    obj.imagem_mimetype = None
    obj.imagem_url = None
    obj.imagem_dropbox_url = None
    obj.imagem_storage_path = None
    _db.session.commit()
    flash('Imagem removida.', 'info')
    return redirect(url_back)


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
                # Resolve FK por nome — item orfao (sem match) entra com
                # FK NULL e admin resolve em /cestas/orfaos.
                tipo_item = item_data['tipo']
                nome_item = item_data['item_nome']
                receita_id = None
                materia_prima_id = None
                if tipo_item == 'receita':
                    r = Receita.query.filter_by(nome=nome_item).first()
                    receita_id = r.id if r else None
                elif tipo_item == 'mp':
                    m = MateriaPrima.query.filter_by(nome=nome_item).first()
                    materia_prima_id = m.id if m else None
                item = ProdutoItem(
                    produto_id=produto.id,
                    tipo=tipo_item,
                    item_nome=nome_item,
                    receita_id=receita_id,
                    materia_prima_id=materia_prima_id,
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
    registro_f = request.args.get("registro_id", type=int)
    q = AuditLog.query
    if tabela_f:
        q = q.filter_by(tabela=tabela_f)
    if usuario_f:
        q = q.filter_by(usuario_id=usuario_f)
    if acao_f in ("insert", "update", "delete"):
        q = q.filter_by(acao=acao_f)
    if registro_f:
        q = q.filter_by(registro_id=registro_f)
    logs = q.order_by(AuditLog.criado_em.desc()).limit(200).all()
    # Parse JSON dos campos antes/depois + tradução em linguagem natural.
    from app.services import historico_humano
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
        traducao = historico_humano.traduzir_audit(l, antes, depois)
        rows.append({
            "log": l, "antes": antes, "depois": depois, "traducao": traducao,
        })
    # Lista de tabelas e usuarios pra filtros
    tabelas = [r[0] for r in db.session.query(AuditLog.tabela).distinct().all()]
    usuarios = Usuario.query.order_by(Usuario.nome).all()
    # Cartinhas atualizadas nas ultimas 48h — pra rastrear pedidos com
    # cartinha cadastrada manualmente (relatorio do auditor "cliente pediu
    # cartinha em pedido ja feito" usa a conversa do Chatwoot; aqui voce
    # ve o que o atendente efetivamente CADASTROU no banco).
    from app.models import CartinhaEntrega
    cartinhas = (CartinhaEntrega.query
                 .filter(CartinhaEntrega.atualizado_em >= agora() - timedelta(hours=48))
                 .order_by(CartinhaEntrega.atualizado_em.desc())
                 .limit(50).all())
    return render_template("main/audit.html", rows=rows, tabelas=sorted(tabelas),
                           usuarios=usuarios, cartinhas=cartinhas,
                           filtros={"tabela": tabela_f,
                           "usuario_id": usuario_f, "acao": acao_f,
                           "registro_id": registro_f})



@main_bp.route('/caixa')
@login_required
@admin_required
def caixa():
    """Dashboard de caixa diario: agrega dados LOCAIS do banco.
    Vendas PDV (Seru) NAO entram aqui pra evitar chamadas externas
    lentas — use /pdv pra esse detalhe."""
    from sqlalchemy import func as sqlfunc

    data_str = request.args.get('data', hoje_brt().isoformat())
    try:
        data_alvo = datetime.strptime(data_str, '%Y-%m-%d').date()
    except ValueError:
        data_alvo = hoje_brt()

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


@main_bp.route('/admin/debug-papeis')
@owner_required
def debug_papeis():
    """Lista usuarios + papel + tools que o copilot vai oferecer pra cada um.

    Usado pra diagnostico quando alguem reclama 'copilot disse que nao
    posso fazer X'. Owner-only.
    """
    from app.models import SlackVinculo, Usuario
    from app.services.copilot import papel_efetivo, tools_permitidas

    users = Usuario.query.order_by(Usuario.papel, Usuario.nome).all()
    # Indexa vinculos por usuario_id — pode haver MAIS DE UM por user, em
    # tese (slack_user_id diferentes). Lista pra ver todos.
    vinculos_por_user = {}
    for v in SlackVinculo.query.filter_by(ativo=True).all():
        vinculos_por_user.setdefault(v.usuario_id, []).append(v.slack_user_id)

    linhas = []
    for u in users:
        papel = papel_efetivo(u)
        tools = sorted([t['name'] for t in tools_permitidas(u)])
        slacks = vinculos_por_user.get(u.id, [])
        linhas.append({
            'id': u.id,
            'nome': u.nome,
            'login': u.login,
            'papel_db': u.papel,
            'is_owner': bool(getattr(u, 'is_owner', False)),
            'papel_efetivo': papel,
            'loja_id': u.loja_id,
            'tools_count': len(tools),
            'tem_criar_pedido': 'criar_pedido' in tools,
            'tem_receber_mp': 'receber_mp' in tools,
            'tem_registrar_desperdicio': 'registrar_desperdicio' in tools,
            'slack_user_ids': slacks,
        })

    # Tabela secundaria: TODOS os vinculos slack ativos com slack_user_id
    # e quem cada um aponta. Util pra detectar vinculo apontando pra
    # usuario errado (ex: slack do Kelvin vinculado a um funcionario).
    todos_vinculos = []
    user_por_id = {u.id: u for u in users}
    for v in SlackVinculo.query.filter_by(ativo=True).order_by(SlackVinculo.slack_user_id).all():
        alvo = user_por_id.get(v.usuario_id)
        todos_vinculos.append({
            'slack_user_id': v.slack_user_id,
            'usuario_id': v.usuario_id,
            'alvo_nome': alvo.nome if alvo else '(usuario nao encontrado!)',
            'alvo_papel': alvo.papel if alvo else '?',
        })
    return render_template('main/debug_papeis.html', linhas=linhas,
                           todos_vinculos=todos_vinculos)


@main_bp.route('/admin/permissoes', methods=['GET', 'POST'])
@owner_required
def permissoes_editar():
    """Matriz editavel papel x capacidade (web + copilot + Slack). Owner-only.

    Admin/owner nao aparecem na matriz (acesso total fixo). Os padroes espelham
    o comportamento legado — so o que voce mudar aqui passa a valer (na hora)."""
    from flask import flash

    from app.services import permissoes as perm_svc

    if request.method == 'POST':
        perm_svc.salvar(request.form)
        flash('Permissões atualizadas.', 'success')
        return redirect(url_for('main.permissoes_editar'))

    return render_template('main/permissoes.html',
                           linhas=perm_svc.estado_atual(),
                           papeis=perm_svc.PAPEIS_EDITAVEIS,
                           papel_label=perm_svc.PAPEL_LABEL)


@main_bp.route('/admin/debug-schema')
@owner_required
def debug_schema():
    """Diagnostico de schema/migrations Alembic. Owner-only."""

    from flask import current_app as _app
    from sqlalchemy import inspect, text

    from app.services import chatbot_vigia, seru_cron

    info = {
        'alembic_current': None,
        'alembic_heads': [],
        'pendentes': [],
        'erro_alembic': None,
        'colunas': [],
        'erro_colunas': None,
        'last_upgrade_log': request.args.get('log'),
        'last_upgrade_ok': request.args.get('ok'),
        'backup_status': seru_cron.status_backup(),
        'vigia_status': {
            'ligado': bool(_app.config.get('CHATBOT_VIGIA')),
            'numero_destino': chatbot_vigia._numero_destino(),
        },
    }

    # 1. Alembic current vs heads
    try:
        from alembic.config import Config
        from alembic.runtime.migration import MigrationContext
        from alembic.script import ScriptDirectory

        cfg = Config('migrations/alembic.ini')
        cfg.set_main_option('script_location', 'migrations')
        script = ScriptDirectory.from_config(cfg)
        info['alembic_heads'] = list(script.get_heads())

        with db.engine.connect() as conn:
            ctx = MigrationContext.configure(conn)
            current = ctx.get_current_revision()
            info['alembic_current'] = current

        if info['alembic_heads']:
            # walk_revisions vai de HEAD pra BASE. Pendentes = tudo desde
            # head ate (exclusive) o current. Se current=None, tudo eh
            # pendente. Se current=head, nada.
            pendentes_revs = []
            for rev in script.walk_revisions(base='base', head='heads'):
                if rev.revision == info['alembic_current']:
                    break
                pendentes_revs.append({
                    'revision': rev.revision,
                    'down': rev.down_revision,
                    'doc': (rev.doc or '')[:120],
                })
            # walk vai do head pra base, mas queremos mostrar a ordem de
            # aplicacao (base → head): inverte.
            info['pendentes'] = list(reversed(pendentes_revs))
    except Exception as e:  # noqa: BLE001
        info['erro_alembic'] = f'{type(e).__name__}: {e}'

    # 2. Colunas criticas (resultado das migrations B4/B5)
    try:
        insp = inspect(db.engine)

        def col_info(tabela, coluna):
            try:
                cols = {c['name']: c for c in insp.get_columns(tabela)}
                if coluna not in cols:
                    return {'tabela': tabela, 'coluna': coluna, 'existe': False,
                            'tipo': None, 'nullable': None}
                c = cols[coluna]
                return {'tabela': tabela, 'coluna': coluna, 'existe': True,
                        'tipo': str(c.get('type')), 'nullable': c.get('nullable')}
            except Exception as e:  # noqa: BLE001
                return {'tabela': tabela, 'coluna': coluna, 'existe': None,
                        'tipo': f'ERRO: {type(e).__name__}: {e}',
                        'nullable': None}

        info['colunas'] = [
            col_info('produto_item', 'receita_id'),
            col_info('produto_item', 'materia_prima_id'),
            col_info('produto_item', 'item_nome'),
            col_info('venda_b2b', 'valor_total'),
            col_info('venda_b2b_item', 'preco_unitario'),
            col_info('venda_b2b_parcela', 'valor'),
            col_info('venda_b2b_parcela', 'valor_pago'),
            col_info('venda_manual_loja', 'valor_unitario'),
            col_info('seru_debito_mov', 'fracao'),
        ]
    except Exception as e:  # noqa: BLE001
        info['erro_colunas'] = f'{type(e).__name__}: {e}'

    # 3. Detecta estado misto: DDL aplicado parcialmente mas alembic_version
    # atrasado. Usado pra sugerir stamp manual antes de tentar upgrade.
    info['estado_misto'] = None
    try:
        insp = inspect(db.engine)
        tabelas = set(insp.get_table_names())
        cols_pi = {c['name'] for c in insp.get_columns('produto_item')}
        cols_vb2b = {c['name']: c for c in insp.get_columns('venda_b2b')}
        vt = cols_vb2b.get('valor_total', {})
        vt_tipo = str(vt.get('type', '')) if vt else ''

        b9_ddl = 'seru_debito_mov' in tabelas
        b4_ddl = 'NUMERIC' in vt_tipo.upper()
        b5_ddl = 'receita_id' in cols_pi

        # Calcula qual e a revision mais avancada que ja teve seu DDL aplicado
        ddl_avancado_em = '69d82afed149'  # baseline
        if b9_ddl:
            ddl_avancado_em = 'ac57b6648ec4'  # B9
        if b9_ddl and b4_ddl:
            ddl_avancado_em = '643bd66e89c3'  # B4
        if b9_ddl and b4_ddl and b5_ddl:
            ddl_avancado_em = 'efb6e5837fd0'  # B5 (head)

        if info['alembic_current'] != ddl_avancado_em:
            info['estado_misto'] = {
                'alembic_diz': info['alembic_current'],
                'ddl_real': ddl_avancado_em,
                'b9_ddl': b9_ddl,
                'b4_ddl': b4_ddl,
                'b5_ddl': b5_ddl,
            }
    except Exception as e:  # noqa: BLE001
        info['estado_misto'] = {'erro': f'{type(e).__name__}: {e}'}

    # 4. Contagem rapida de orfaos (so se B5 ja aplicou)
    info['orfaos'] = None
    try:
        cols_pi = {c['name'] for c in inspect(db.engine).get_columns('produto_item')}
        if 'receita_id' in cols_pi:
            with db.engine.connect() as conn:
                o_r = conn.execute(text(
                    "SELECT COUNT(*) FROM produto_item "
                    "WHERE tipo = 'receita' AND receita_id IS NULL"
                )).scalar() or 0
                o_m = conn.execute(text(
                    "SELECT COUNT(*) FROM produto_item "
                    "WHERE tipo = 'mp' AND materia_prima_id IS NULL"
                )).scalar() or 0
                info['orfaos'] = {'receita': o_r, 'mp': o_m}
    except Exception as e:  # noqa: BLE001
        info['orfaos'] = {'erro': f'{type(e).__name__}: {e}'}

    return render_template('main/debug_schema.html', info=info)


@main_bp.route('/admin/debug-tiny')
@owner_required
def debug_tiny():
    """Owner-only: testa busca no Tiny pra (CPF, numero). Mostra exatamente o
    que a API v2 do Tiny retornou — util pra debugar bot achando 'nao
    encontrado' quando o pedido existe no painel."""
    from app.services import tiny
    cpf = (request.args.get('cpf') or '').strip()
    numero = (request.args.get('numero') or '').strip()
    resultado: dict = {'cpf': cpf, 'numero': numero,
                       'tiny_disponivel': tiny.disponivel()}
    if cpf and numero:
        try:
            cpf_d = ''.join(c for c in cpf if c.isdigit())
            # 1. Pesquisa por CPF (v2 ignora filtros de numero — visto antes)
            r_pesq = tiny._get('pedidos.pesquisa.php',
                                params={'cpf_cnpj': cpf_d, 'pagina': '1'})
            pesq_dict = r_pesq if isinstance(r_pesq, dict) else {}
            primeiros = pesq_dict.get('pedidos') or []
            campos = []
            if primeiros and isinstance(primeiros[0], dict):
                p0 = primeiros[0].get('pedido') or {}
                if isinstance(p0, dict):
                    campos = list(p0.keys())
            resultado['pesquisa'] = {
                'status': pesq_dict.get('status'),
                'qtd': len(primeiros),
                'campos_disponiveis': campos,
            }

            # 2. Funcao de alto nivel — o que o bot enxerga
            pedido = tiny.buscar_pedido_por_cpf_e_numero(cpf, numero)
            resultado['pedido_resolvido'] = pedido

            # 3. Se achou o pedido, traz o detalhe CRU
            if pedido and pedido.get('id'):
                r_det = tiny._get('pedido.obter.php',
                                   params={'id': pedido['id']})
                resultado['detalhe_cru'] = r_det if isinstance(r_det, dict) else None
                if isinstance(r_det, dict):
                    p_det = r_det.get('pedido') or {}
                    if not isinstance(p_det, dict):
                        p_det = {}
                    # v2 retorna nota_fiscal (sing) OU notas_fiscais (lista)
                    nf = p_det.get('nota_fiscal')
                    if not isinstance(nf, dict):
                        nf = {}
                    if not nf:
                        lista = p_det.get('notas_fiscais') or []
                        if isinstance(lista, list) and lista:
                            primeiro = lista[0]
                            if isinstance(primeiro, dict):
                                nf = primeiro.get('nota_fiscal') or primeiro
                                if not isinstance(nf, dict):
                                    nf = {}
                    resultado['nota_fiscal_extraida'] = nf
                    nf_id = nf.get('id') if isinstance(nf, dict) else None
                    if nf_id:
                        r_link = tiny._get('nota.fiscal.obter.link.php',
                                            params={'id': str(nf_id)})
                        resultado['link_resposta'] = r_link if isinstance(r_link, dict) else None
                        resultado['link_resolvido'] = tiny.obter_link_nota_fiscal(nf_id)
        except Exception as exc:  # noqa: BLE001
            resultado['erro_exception'] = f'{type(exc).__name__}: {exc}'
            import traceback
            resultado['traceback'] = traceback.format_exc()[-1500:]

    return jsonify(resultado), 200


@main_bp.route('/admin/debug-nflog')
@owner_required
def debug_nflog():
    """Owner-only: ultimas 50 entradas do NFLog (audit das solicitacoes de NF
    pelo bot). Util pra ver POR QUE o bot disse 'nao encontrei' num caso real:
    o `resultado` + `detalhe` revelam onde foi recusado e com qual numero."""
    from app.models import NFLog
    qs = NFLog.query.order_by(NFLog.id.desc()).limit(50).all()
    return jsonify([{
        'id': r.id,
        'em': r.criado_em.isoformat() if r.criado_em else None,
        'conv': r.conv_id, 'canal': r.canal,
        'cpf_4': r.cpf_4ultimos,
        'numero_buscado': r.numero_pedido,
        'resultado': r.resultado,
        'detalhe': r.detalhe,
    } for r in qs]), 200


@main_bp.route('/admin/debug-vnda-cartinha')
@owner_required
def debug_vnda_cartinha():
    """Owner-only: sonda a API VNDA pra descobrir se da pra ESCREVER a cartinha
    (customization) de um pedido ja fechado. So GET + OPTIONS — NAO grava nada.

    A cartinha no VNDA se grava no CARRINHO (/carts/...), nao no pedido
    (/orders/... e read-only). Esta rota investiga se o carrinho do pedido
    ainda eh alcancavel/gravavel depois de fechado.

    Uso: /admin/debug-vnda-cartinha?code=CODIGO_DO_PEDIDO
    """
    import requests

    from app.services import vnda
    code = (request.args.get('code') or '').strip()
    out: dict = {'code': code}
    if not code:
        out['erro'] = 'passe ?code=CODIGO_DO_PEDIDO (ex: ?code=DA19F38765)'
        return jsonify(out), 200

    base = vnda._base_url()
    headers = vnda._headers()

    def _probe(method, path, **kw):
        """GET/OPTIONS seguro. Devolve status + Allow + corpo (truncado)."""
        try:
            r = requests.request(method, f'{base}{path}', headers=headers,
                                  timeout=10, **kw)
            try:
                body = r.json()
            except ValueError:
                body = (r.text or '')[:400]
            return {'status': r.status_code, 'allow': r.headers.get('Allow'),
                    'body': body}
        except requests.RequestException as e:
            return {'erro': str(e)}

    try:
        # 1. Pedido completo: chaves + campos candidatos a ligar no carrinho
        ped = _probe('GET', f'/orders/{code}')
        out['pedido_status'] = ped.get('status')
        body = ped.get('body') if isinstance(ped.get('body'), dict) else {}
        out['pedido_chaves'] = sorted(body.keys()) if body else None
        # Campos que tipicamente referenciam o carrinho (sem despejar PII)
        out['campos_cart'] = {k: body.get(k) for k in
                              ('token', 'cart_id', 'cart_token', 'cart', 'id',
                               'number', 'code')
                              if k in body}
        itens = body.get('items') or []
        out['itens'] = [{'id': it.get('id'), 'sku': it.get('sku'),
                         'nome': it.get('product_name') or it.get('name'),
                         'has_customizations': it.get('has_customizations')}
                        for it in itens]

        # Campos de NIVEL DE PEDIDO que poderiam conter a "cartinha escondida"
        # (mensagem de entrega / observacao). Se o texto da cartinha aparecer
        # aqui, da pra editar via PATCH /orders — bem mais facil que o carrinho.
        out['campos_mensagem'] = {k: body.get(k) for k in
                                  ('note', 'delivery_message', 'extra',
                                   'user_code', 'agent', 'channel')
                                  if k in body}

        # 2. Customizations atuais (READ — ja sabemos que funciona)
        cust = {}
        for it in itens[:5]:
            iid = it.get('id')
            if iid:
                cust[str(iid)] = _probe(
                    'GET', f'/orders/{code}/items/{iid}/customizations')
        out['customizations_pedido'] = cust

        # 3. Tenta alcancar o CARRINHO por token E por cart_id numerico.
        # (O token deu carrinho vazio antes; o cart_id numerico pode diferir.)
        out['cart_por_token'] = None
        out['cart_por_id'] = None
        tok = body.get('token')
        cid = body.get('cart_id')
        if tok:
            out['cart_por_token'] = _probe('GET', f'/carts/{tok}/items')
        if cid:
            out['cart_por_id_meta'] = _probe('GET', f'/carts/{cid}')
            out['cart_por_id'] = _probe('GET', f'/carts/{cid}/items')
    except Exception as exc:  # noqa: BLE001
        import traceback
        out['erro_exception'] = f'{type(exc).__name__}: {exc}'
        out['traceback'] = traceback.format_exc()[-1500:]

    return jsonify(out), 200


@main_bp.route('/admin/debug-vnda-cartinha-write')
@owner_required
def debug_vnda_cartinha_write():
    """Owner-only TESTE DE ESCRITA da cartinha no pedido. Tenta um metodo HTTP
    (POST/PUT/DELETE) no endpoint de customizations do PEDIDO e reporta o
    status cru do VNDA. ESCREVE de verdade — por isso exige ?confirmo=sim.

    ⚠️ USE EM PEDIDO DE TESTE. Pode alterar/duplicar a cartinha real.

    Parametros:
      code, item_id   obrigatorios (vem do sondador read-only)
      metodo          post (default) | put | delete
      texto           texto da cartinha de teste
      grupo           group_name (default 'Cartinha')
      cust_id         id da customization (pra put/delete em recurso especifico)
      formato         body1 (default {group_name,name}) | body2 ({customizations:[...]})
    """
    import requests

    from app.services import vnda
    code = (request.args.get('code') or '').strip()
    item_id = (request.args.get('item_id') or '').strip()
    metodo = (request.args.get('metodo') or 'post').lower()
    texto = (request.args.get('texto') or 'TESTE BOT - pode apagar').strip()
    grupo = (request.args.get('grupo') or 'Cartinha').strip()
    cust_id = (request.args.get('cust_id') or '').strip()
    formato = (request.args.get('formato') or 'body1').strip()

    out: dict = {'code': code, 'item_id': item_id, 'metodo': metodo,
                 'formato': formato}
    if request.args.get('confirmo') != 'sim':
        out['erro'] = ('Faltou ?confirmo=sim. ATENCAO: esta rota ESCREVE no '
                       'VNDA. Rode so em pedido de TESTE.')
        return jsonify(out), 200
    if not code or not item_id:
        out['erro'] = 'precisa de ?code=...&item_id=... (pegue do sondador read-only)'
        return jsonify(out), 200

    base = vnda._base_url()
    headers = vnda._headers()
    path = f'/orders/{code}/items/{item_id}/customizations'
    if metodo in ('put', 'delete') and cust_id:
        path = f'{path}/{cust_id}'

    # Dois palpites de corpo — VNDA pode querer chave plana ou aninhada.
    if formato == 'body2':
        payload = {'customizations': [{'group_name': grupo, 'name': texto}]}
    else:
        payload = {'group_name': grupo, 'name': texto}

    out['url'] = f'{base}{path}'
    out['payload_enviado'] = payload
    try:
        kwargs = {} if metodo == 'delete' else {'json': payload}
        r = requests.request(metodo.upper(), f'{base}{path}',
                             headers=headers, timeout=12, **kwargs)
        try:
            rbody = r.json()
        except ValueError:
            rbody = (r.text or '')[:600]
        out['resposta'] = {'status': r.status_code,
                           'allow': r.headers.get('Allow'), 'body': rbody}
    except requests.RequestException as e:
        out['erro_req'] = str(e)

    return jsonify(out), 200


@main_bp.route('/admin/debug-schema/upgrade', methods=['POST'])
@owner_required
def debug_schema_upgrade():
    """Aplica migrations pendentes manualmente. Owner-only."""
    import io
    import logging
    import traceback as _tb

    log_buf = io.StringIO()
    handler = logging.StreamHandler(log_buf)
    handler.setLevel(logging.INFO)
    root = logging.getLogger()
    root.addHandler(handler)
    original_level = root.level
    root.setLevel(logging.INFO)

    ok = '1'
    try:
        from flask_migrate import upgrade as _upgrade
        _upgrade(directory='migrations')
        log_buf.write('\nOK: upgrade concluido sem exception.')
    except Exception:  # noqa: BLE001
        ok = '0'
        log_buf.write('\n--- TRACEBACK ---\n')
        log_buf.write(_tb.format_exc())
    finally:
        root.removeHandler(handler)
        root.setLevel(original_level)

    return redirect(url_for('main.debug_schema',
                            log=log_buf.getvalue()[-3000:], ok=ok))


@main_bp.route('/admin/debug-schema/stamp', methods=['POST'])
@owner_required
def debug_schema_stamp():
    """Marca alembic_version pra revision indicada SEM aplicar DDL.

    Uso: quando DDL ja foi aplicado por outro caminho (ex: tabela
    seru_debito_mov foi criada mas alembic_version voltou pra baseline
    por algum reset). Stamp realinha o controle sem executar migration.
    """
    import io
    import traceback as _tb

    revision = (request.form.get('revision') or '').strip()
    log_buf = io.StringIO()
    log_buf.write(f'Stamp pedido: revision={revision!r}\n')

    if not revision or len(revision) > 32 or not revision.replace('_', '').isalnum():
        log_buf.write('ERRO: revision invalida (precisa ser ID alfanumerico).')
        return redirect(url_for('main.debug_schema',
                                log=log_buf.getvalue(), ok='0'))

    ok = '1'
    try:
        from flask_migrate import stamp as _stamp
        _stamp(directory='migrations', revision=revision)
        log_buf.write(f'OK: alembic_version stampada em {revision}.\n')
    except Exception:  # noqa: BLE001
        ok = '0'
        log_buf.write('\n--- TRACEBACK ---\n')
        log_buf.write(_tb.format_exc())

    return redirect(url_for('main.debug_schema',
                            log=log_buf.getvalue()[-3000:], ok=ok))


@main_bp.route('/admin/slack/diagnostico')
@owner_required
def slack_diagnostico():
    """Diagnostico dos avisos via Slack (canais, envio, alerta de desperdicio).

    Resolve o caso comum: "recebi WhatsApp mas nao vi no Slack".
    Mostra config + permite disparar alerta na hora pra ler o motivo real.
    """
    from flask import current_app

    from app.services import desperdicio_alerta, slack

    cfg = current_app.config
    canais = [
        ('Resumo diario (04:00)', 'SLACK_CANAL_RESUMO_DIARIO',
         (cfg.get('SLACK_CANAL_RESUMO_DIARIO') or '').strip()),
        ('Lembretes pedido amanha (9/12/16/19h)', 'SLACK_CANAL_PEDIDOS',
         (cfg.get('SLACK_CANAL_PEDIDOS') or '').strip()),
        ('Alerta desperdicio (20:10/15/20/25)', 'SLACK_CANAL_COPILOT',
         (cfg.get('SLACK_CANAL_COPILOT') or '').strip()),
    ]
    info = {
        'bot_token_setado': bool((cfg.get('SLACK_BOT_TOKEN') or '').strip()),
        'signing_setado': bool((cfg.get('SLACK_SIGNING_SECRET') or '').strip()),
        'disponivel': slack.disponivel(),
        'canais': canais,
        'lojas_sem_desperdicio': desperdicio_alerta.lojas_sem_desperdicio(),
        'ultimo_resultado': request.args.get('resultado'),
    }
    return render_template('main/slack_diagnostico.html', info=info)


@main_bp.route('/admin/slack/diagnostico/testar-canal', methods=['POST'])
@owner_required
def slack_diagnostico_testar_canal():
    from flask import flash

    from app.services import slack

    canal = (request.form.get('canal') or '').strip()
    if not canal:
        flash('Canal vazio — configure a env var antes.', 'warning')
        return redirect(url_for('main.slack_diagnostico'))
    res = slack.post_message(
        canal, ':test_tube: Teste de envio do diagnostico Slack.')
    if res.get('ok'):
        msg = f'OK: mensagem postada no canal {canal} (ts={res.get("ts")}).'
        nivel = 'success'
    else:
        msg = f'FALHA ao postar em {canal}: {res.get("erro")}'
        nivel = 'danger'
    flash(msg, nivel)
    return redirect(url_for('main.slack_diagnostico'))


@main_bp.route('/admin/slack/diagnostico/disparar-desperdicio', methods=['POST'])
@owner_required
def slack_diagnostico_disparar_desperdicio():
    """Dispara `alertar_slack_pendentes` na hora e mostra retorno bruto.

    Util pra entender por que o cron 20:10/15/20/25 nao apareceu no canal.
    """
    from flask import flash

    from app.services import desperdicio_alerta

    res = desperdicio_alerta.alertar_slack_pendentes()
    if res.get('enviado'):
        flash(f'Alerta enviado no Slack ({res.get("pendentes")} loja[s] pendente[s]).',
              'success')
    else:
        motivo = res.get('motivo')
        erro = res.get('erro')
        flash(f'NAO enviado. motivo={motivo}'
              + (f' · erro={erro}' if erro else ''),
              'warning' if motivo == 'sem_pendencias' else 'danger')
    return redirect(url_for('main.slack_diagnostico'))


@main_bp.route('/admin/backup/debug-env')
@owner_required
def backup_debug_env():
    """Diagnostico do ambiente — mostra PATH, locais com pg_dump, versao.

    Usado quando backup falha com "pg_dump nao encontrado" pra entender se
    o nixpacks.toml aplicou ou se o binario esta noutro lugar.
    """
    import os as _os
    import shutil
    import subprocess

    info = {
        'PATH': _os.environ.get('PATH', ''),
        'which_pg_dump': shutil.which('pg_dump'),
    }

    # Procura pg_dump em locais comuns
    locais = []
    for caminho in ['/usr/bin', '/usr/local/bin', '/nix/store', '/usr/lib/postgresql']:
        try:
            r = subprocess.run(['bash', '-c', f'ls -la {caminho} 2>/dev/null | grep -i pg_'],
                               capture_output=True, text=True, timeout=5)
            if r.stdout:
                locais.append(f'{caminho}:\n{r.stdout}')
        except Exception as e:  # noqa: BLE001
            locais.append(f'{caminho}: ERRO {e}')

    # Procura recursiva no /nix/store (Nixpacks instala la)
    try:
        r = subprocess.run(['bash', '-c', 'find /nix/store -name pg_dump 2>/dev/null | head -5'],
                           capture_output=True, text=True, timeout=15)
        info['find_nix_pg_dump'] = r.stdout or '(nada encontrado)'
    except Exception as e:  # noqa: BLE001
        info['find_nix_pg_dump'] = f'ERRO: {e}'

    # Tenta executar
    try:
        r = subprocess.run(['pg_dump', '--version'], capture_output=True, text=True, timeout=5)
        info['pg_dump_version'] = r.stdout or r.stderr
    except FileNotFoundError:
        info['pg_dump_version'] = '(nao encontrado no PATH)'
    except Exception as e:  # noqa: BLE001
        info['pg_dump_version'] = f'ERRO: {e}'

    # Diagnostico extra: identifica se imagem eh Dockerfile-based ou Nixpacks
    try:
        r = subprocess.run(['bash', '-c',
                            'ls -la / 2>&1 | head -30; echo ---; '
                            'cat /etc/os-release 2>&1 | head -5; echo ---; '
                            'dpkg -l 2>/dev/null | grep -iE "postgres|libpq" || echo "(sem dpkg ou sem postgres)"'],
                           capture_output=True, text=True, timeout=10)
        info['ambiente'] = r.stdout
    except Exception as e:  # noqa: BLE001
        info['ambiente'] = f'ERRO: {e}'

    info['locais_listagem'] = '\n\n'.join(locais)

    # Onde fotos de entrega DEVERIAM estar indo
    from flask import current_app, jsonify

    from app.models import EntregaFoto
    info['dropbox_pasta_base_config'] = (
        current_app.config.get('DROPBOX_PASTA_BASE') or '(usando default /Apps/Receitas-Entregas)'
    )
    info['dropbox_backup_pasta_config'] = (
        current_app.config.get('DROPBOX_BACKUP_PASTA') or '(usando default /backups-postgres)'
    )
    info['entrega_foto_count'] = EntregaFoto.query.count()
    foto_recente = EntregaFoto.query.order_by(EntregaFoto.id.desc()).first()
    if foto_recente:
        info['entrega_foto_amostra'] = {
            'id': foto_recente.id,
            'storage_path': foto_recente.storage_path,
            'url': foto_recente.url,
            'tirada_em': str(foto_recente.tirada_em),
        }
    else:
        info['entrega_foto_amostra'] = '(sem fotos no banco)'

    # M6 debug: URL de uma receita migrada
    from app.models import Produto, Receita
    r = (Receita.query
         .filter(Receita.imagem_dropbox_url.isnot(None))
         .order_by(Receita.id.desc()).first())
    if r:
        info['receita_amostra'] = {
            'id': r.id, 'nome': r.nome,
            'imagem_dropbox_url': r.imagem_dropbox_url,
            'imagem_storage_path': r.imagem_storage_path,
            'tem_blob': r.imagem_blob is not None,
        }
    else:
        info['receita_amostra'] = '(nenhuma receita migrada)'

    p = (Produto.query
         .filter(Produto.imagem_dropbox_url.isnot(None))
         .order_by(Produto.id.desc()).first())
    if p:
        info['produto_amostra'] = {
            'id': p.id, 'nome': p.nome,
            'imagem_dropbox_url': p.imagem_dropbox_url,
            'imagem_storage_path': p.imagem_storage_path,
            'tem_blob': p.imagem_blob is not None,
        }
    else:
        info['produto_amostra'] = '(nenhum produto migrado)'

    return jsonify(info)


@main_bp.route('/admin/blobs/migrar/<modelo>', methods=['POST'])
@owner_required
def blobs_migrar(modelo):
    """Backfill de BLOBs antigos pra Dropbox (M6). Owner-only.

    Modelos suportados: pedido_item_foto.
    Idempotente. Processa em batches, advisory lock single-worker.
    """
    from flask import flash

    from app.services import blob_migrator

    if modelo == 'pedido_item_foto':
        resultado = blob_migrator.migrar_pedido_item_foto()
    elif modelo == 'foto_recebimento':
        resultado = blob_migrator.migrar_foto_recebimento()
    elif modelo == 'receita':
        resultado = blob_migrator.migrar_receita_imagem()
    elif modelo == 'produto':
        resultado = blob_migrator.migrar_produto_imagem()
    else:
        flash(f'Modelo invalido: {modelo}', 'danger')
        return redirect(url_for('main.debug_schema'))

    if not resultado.get('ok'):
        flash(f'Migracao falhou: {resultado.get("motivo")}', 'danger')
    else:
        msg = (f'Migracao {modelo}: {resultado["migradas"]}/{resultado["total"]} '
               f'migradas, {resultado["erros"]} erros')
        if resultado.get('detalhes'):
            msg += '. Primeiros detalhes: ' + ' | '.join(resultado['detalhes'][:3])
        cat = 'success' if resultado['erros'] == 0 else 'warning'
        flash(msg, cat)
    return redirect(url_for('main.debug_schema'))


@main_bp.route('/admin/blobs/fix-urls-dropbox', methods=['POST'])
@owner_required
def blobs_fix_urls():
    """One-shot: substitui dl=0 por raw=1 em URLs Dropbox ja populadas.

    Bug originalmente em `_converter_para_raw` deixou URLs com formato
    `...?rlkey=X&dl=0&raw=1`. Dropbox prioriza dl=0 e serve HTML preview.
    Esta rota corrige UPDATE direto no banco — sem precisar reupload.
    """
    from flask import flash
    from sqlalchemy import text

    from app.extensions import db as _db

    tabelas = [
        ('pedido_item_foto', 'imagem_url'),
        ('foto_recebimento', 'imagem_url'),
        ('receita', 'imagem_dropbox_url'),
        ('produto', 'imagem_dropbox_url'),
    ]
    # Normalizacao: itera linhas com URL Dropbox e aplica
    # _converter_para_raw (robusto a dl=0, raw=1 duplicado, etc).
    from app.services.dropbox_storage import _converter_para_raw
    resumo = []
    for tabela, coluna in tabelas:
        with _db.engine.begin() as conn:
            rows = conn.execute(text(
                f"SELECT id, {coluna} FROM {tabela} "
                f"WHERE {coluna} IS NOT NULL"
            )).fetchall()
            corrigidas = 0
            for row in rows:
                nova_url = _converter_para_raw(row[1])
                if nova_url != row[1]:
                    conn.execute(
                        text(f"UPDATE {tabela} SET {coluna} = :u "
                             f"WHERE id = :i"),
                        {'u': nova_url, 'i': row[0]})
                    corrigidas += 1
            resumo.append(f'{tabela}.{coluna}: {corrigidas}/{len(rows)}')
    flash('URLs Dropbox corrigidas: ' + ' · '.join(resumo), 'success')
    return redirect(url_for('main.debug_schema'))


@main_bp.route('/admin/backup/run', methods=['POST'])
@owner_required
def backup_run():
    """Dispara backup manual do Postgres pro Dropbox. Owner-only.

    Uso: pra testar a configuracao e gerar dump on-demand. O job
    automatico roda diariamente as 04:00 BRT via APScheduler.
    """
    from flask import flash

    from app.services import backup as backup_svc

    resultado = backup_svc.executar_backup(forcar=True)
    if resultado['ok']:
        mb = resultado['tamanho'] / 1024 / 1024
        flash(f'Backup OK: {mb:.2f} MB em {resultado["arquivo"]}', 'success')
    else:
        flash(f'Backup falhou: {resultado.get("motivo") or "ver logs"}', 'danger')
    return redirect(url_for('main.debug_schema'))


@main_bp.route('/admin/backup/drill')
@owner_required
def backup_drill():
    """Drill de restore do backup (owner-only): prova que o dump do Dropbox
    eh restauravel. Sem parametro = mostra status do ultimo drill.

    ?iniciar=1     baixa o dump mais recente + valida estrutura (pg_restore
                   --list). Rapido (~1 min), nao toca em banco nenhum.
    ?iniciar=full  alem do acima, restaura num banco temporario
                   (drill_restore_tmp), conta linhas de tabelas-chave e dropa.
                   Prova completa. Pode levar varios minutos — acompanhe
                   recarregando esta rota.

    O status fica em arquivo compartilhado (/tmp) — qualquer worker gunicorn
    responde o mesmo estado, e o resultado sobrevive a reinicio de worker.
    """
    from app.services import backup as backup_svc

    iniciar = (request.args.get('iniciar') or '').strip().lower()
    if iniciar in ('1', 'full'):
        out = backup_svc.iniciar_drill(full=(iniciar == 'full'))
        out['status'] = backup_svc.drill_status()
        return jsonify(out), 200
    return jsonify(backup_svc.drill_status()), 200


@main_bp.route('/admin/dropbox/reauth')
@owner_required
def dropbox_reauth():
    """Re-autorizacao OAuth da app Dropbox (owner-only), pra quando os ESCOPOS
    mudam — permissao nova (ex: files.content.read pro drill de restore) NAO
    vale pra refresh token ja emitido; precisa autorizar de novo.

    Fluxo em 2 passos, sem curl:
      1. GET sem parametro: mostra o link de autorizacao do Dropbox. Abra,
         clique em Permitir, copie o codigo exibido.
      2. GET ?code=<codigo>: troca o codigo por um refresh token NOVO e mostra
         o valor pra voce colar em Railway -> Variables -> DROPBOX_REFRESH_TOKEN.
    """
    import requests as _requests
    app_key = (current_app.config.get('DROPBOX_APP_KEY') or '').strip()
    app_secret = (current_app.config.get('DROPBOX_APP_SECRET') or '').strip()
    if not app_key or not app_secret:
        return jsonify(erro='DROPBOX_APP_KEY/SECRET nao configurados no env'), 200

    code = (request.args.get('code') or '').strip()
    if not code:
        url_auth = ('https://www.dropbox.com/oauth2/authorize'
                    f'?client_id={app_key}&response_type=code'
                    '&token_access_type=offline')
        return jsonify(
            passo_1=('Confirme ANTES no App Console que o escopo novo esta '
                     'marcado (Permissions -> files.content.read -> Submit).'),
            passo_2=f'Abra e autorize: {url_auth}',
            passo_3=('Copie o codigo que o Dropbox mostrar e volte aqui com '
                     '?code=<codigo>'),
        ), 200

    r = _requests.post(
        'https://api.dropbox.com/oauth2/token',
        data={'grant_type': 'authorization_code', 'code': code},
        auth=(app_key, app_secret),
        timeout=15,
    )
    if r.status_code != 200:
        return jsonify(erro=f'troca do codigo falhou: HTTP {r.status_code}',
                       detalhe=(r.text or '')[:300],
                       dica=('Codigo expira em minutos e so vale 1 vez — '
                             'gere outro no link do passo 2.')), 200
    body = r.json()
    novo_refresh = body.get('refresh_token') or ''
    if not novo_refresh:
        return jsonify(erro='resposta sem refresh_token',
                       detalhe=str(body)[:300]), 200
    return jsonify(
        ok=True,
        refresh_token=novo_refresh,
        proximo_passo=('Railway -> servico web -> Variables -> substitua '
                       'DROPBOX_REFRESH_TOKEN por este valor e salve. Apos o '
                       'redeploy, rode o drill de novo: '
                       '/admin/backup/drill?iniciar=full'),
    ), 200


@main_bp.route('/admin/teste-aviso-recebimento')
@owner_required
def teste_aviso_recebimento():
    """Teste end-to-end do aviso de pedido recebido (owner-only).

    Sem parametro: cria um PedidoLoja de TESTE (sem itens — nao toca
    estoque), sobe 2 fotos geradas pra /recebimento/<id>/ no Dropbox, marca
    'entregue' e dispara o aviso pro WhatsApp do dono com o link da pasta.

    ?limpar=<id>: apaga o pedido de teste (so se tiver o marcador de teste
    na observacao — pedido real e recusado), as fotos do banco e os
    arquivos do Dropbox.
    """
    import io
    import time as _time

    from app.models import FotoRecebimento, Loja, PedidoLoja
    from app.services import dropbox_storage, pedidos_notificacao

    MARCADOR = '[PEDIDO-TESTE-AVISO]'

    limpar_id = request.args.get('limpar')
    if limpar_id:
        p = PedidoLoja.query.get(int(limpar_id))
        if not p:
            return jsonify(erro='pedido nao encontrado'), 200
        if MARCADOR not in (p.observacao or ''):
            return jsonify(erro='esse pedido NAO eh de teste — recusado'), 200
        for f in list(p.fotos or []):
            if f.imagem_storage_path:
                dropbox_storage.deletar(f.imagem_storage_path)
        db.session.delete(p)   # cascade apaga FotoRecebimento
        db.session.commit()
        return jsonify(ok=True, apagado=int(limpar_id)), 200

    loja = Loja.query.filter_by(ativa=True).first()
    if not loja:
        return jsonify(erro='nenhuma loja ativa'), 200

    p = PedidoLoja(loja_id=loja.id, status='entregue',
                   observacao=f'{MARCADOR} criado via /admin/teste-aviso-recebimento',
                   criado_por=current_user.id)
    db.session.add(p)
    db.session.flush()

    # 2 fotos geradas (quadrados coloridos) pra pasta ter conteudo real
    fotos_ok = 0
    if dropbox_storage.disponivel():
        from PIL import Image
        for cor in ((220, 60, 90), (60, 140, 220)):
            img = Image.new('RGB', (320, 320), cor)
            buf = io.BytesIO()
            img.save(buf, format='JPEG', quality=80)
            try:
                info = dropbox_storage.upload_publico(
                    buf.getvalue(),
                    f'/recebimento/{p.id}/teste_{int(_time.time() * 1000)}.jpg',
                    mode='add', autorename=True)
                db.session.add(FotoRecebimento(
                    pedido_id=p.id, imagem_url=info['url'],
                    imagem_storage_path=info['storage_path'],
                    mimetype='image/jpeg', enviada_por=current_user.id))
                fotos_ok += 1
            except RuntimeError:
                current_app.logger.exception('teste-aviso: upload falhou')
    db.session.commit()

    pedidos_notificacao.notificar_pedido_recebido(p)

    return jsonify(
        ok=True,
        pedido_id=p.id,
        loja=loja.nome,
        fotos_enviadas=fotos_ok,
        confira='o aviso deve ter chegado no seu WhatsApp',
        limpar_depois=f'/admin/teste-aviso-recebimento?limpar={p.id}',
    ), 200


@main_bp.route('/admin/saude')
@owner_required
def saude_negocio_admin():
    """Radar de saude do negocio (owner-only): contas a pagar + receitas.

    O mesmo conteudo chega as 07:30 no WhatsApp do dono (job
    `zapi-digest-saude`; DIGEST_SAUDE=0 desliga). Aqui e a versao on-demand
    com os detalhes completos (listas, nao so contagens).
    ?enviar=1 dispara o digest no WhatsApp agora (teste)."""
    from app.services import saude_negocio

    out = {
        'contas': saude_negocio.resumo_contas(),
        'receitas': saude_negocio.resumo_receitas(),
    }
    if request.args.get('enviar') == '1':
        out['envio'] = saude_negocio.enviar_digest_saude()
    return jsonify(out), 200


@main_bp.route('/admin/debug-handshake-bypass')
@owner_required
def debug_handshake_bypass():
    """Owner-only: pedidos que avancaram (em_transporte/entregue) SEM o
    handshake de QR — responde "alguem pulou o QR?".

    Bypasses LEGITIMOS aparecem identificados: forcar_entrega (admin, gera
    HandshakeAudit proprio) e copilot via Slack (sem HandshakeAudit nenhum).
    ?dias=N (default 30) controla a janela. Atribuir motorista NAO e bypass
    (e o passo anterior ao QR)."""
    from datetime import timedelta as _td

    from app.models import HandshakeAudit, PedidoLoja
    from app.utils import agora as _agora
    dias = request.args.get('dias', 30, type=int)
    corte = _agora() - _td(days=dias)

    pedidos = (PedidoLoja.query
               .filter(PedidoLoja.status.in_(('em_transporte', 'entregue')))
               .filter(PedidoLoja.criado_em >= corte)
               .order_by(PedidoLoja.id.desc()).all())
    ids = [p.id for p in pedidos]
    audits = {}
    if ids:
        for a in HandshakeAudit.query.filter(
                HandshakeAudit.pedido_id.in_(ids)).all():
            audits.setdefault(a.pedido_id, []).append(a)

    suspeitos = []
    com_handshake = 0
    forcados = 0
    for p in pedidos:
        regs = audits.get(p.id, [])
        sucessos = [a for a in regs if a.etapa == 'sucesso']
        forcou = [a for a in regs if a.etapa == 'forcar_entrega']
        if forcou:
            forcados += 1
            suspeitos.append({
                'pedido_id': p.id, 'status': p.status,
                'classificacao': 'forcado_pelo_admin',
                'detalhe': (forcou[0].detalhe or '')[:120],
                'quando': forcou[0].momento.isoformat() if forcou[0].momento else None,
            })
        elif sucessos:
            com_handshake += 1
        else:
            suspeitos.append({
                'pedido_id': p.id, 'status': p.status,
                'classificacao': 'sem_handshake (provavel copilot/Slack)',
                'loja_id': p.loja_id,
                'driver_id': p.driver_id,
                'criado_em': p.criado_em.isoformat() if p.criado_em else None,
            })

    return jsonify(
        janela_dias=dias,
        total_avancados=len(pedidos),
        com_handshake_ok=com_handshake,
        forcados_pelo_admin=forcados,
        sem_handshake=len(suspeitos) - forcados,
        suspeitos=suspeitos[:100],
        dica=('sem_handshake = avancou sem NENHUM scan de QR. Caminho '
              'legitimo: copilot/Slack ("recebi o pedido X"). Se nao foi '
              'copilot, investigue no /audit filtrando o pedido.'),
    ), 200


@main_bp.route('/admin/debug-chapa')
@owner_required
def debug_chapa():
    """Owner-only: raio-X da baixa fracionaria (itens de chapa) no Seru.

    Responde "o desconto de fatias de pao esta funcionando?" com dados reais:
    mapeamentos com fator fracionario, debitos acumulados (fatias aguardando
    fechar 1 pao), orfaos (FK morta) e movimentos fracionarios recentes
    (prova de execucao)."""
    from datetime import timedelta as _td

    from app.models import (
        MovEstoqueLoja,
        Receita,
        SeruDebito,
        SeruProdutoMap,
    )
    from app.utils import agora as _agora

    out = {}

    mapeados = SeruProdutoMap.query.filter(
        (SeruProdutoMap.receita_id.isnot(None))
        | (SeruProdutoMap.produto_id.isnot(None))).all()
    com_fator = []
    orfaos = []
    for m in mapeados:
        fator = float(m.fator_quantidade or 1.0)
        alvo = None
        if m.receita_id:
            r = Receita.query.get(m.receita_id)
            alvo = r.nome if r else None
            if r is None:
                orfaos.append({'map_id': m.id, 'seru_nome': m.seru_nome,
                               'problema': f'receita_id={m.receita_id} nao existe'})
        if fator != 1.0:
            com_fator.append({'seru_nome': m.seru_nome, 'fator': fator,
                              'alvo': alvo})
    out['mapeados_total'] = len(mapeados)
    out['com_fator_fracionario'] = sorted(com_fator,
                                          key=lambda x: x['seru_nome'])
    out['orfaos_fk_morta'] = orfaos

    debitos = SeruDebito.query.filter(
        SeruDebito.fracao_pendente > 0.001).all()
    out['debitos_acumulados'] = [
        {'loja_id': d.loja_id, 'map_id': d.seru_produto_map_id,
         'fracao_pendente': round(float(d.fracao_pendente or 0), 3)}
        for d in debitos]

    corte = _agora() - _td(days=7)
    out['movs_fracionarios_7d'] = (
        MovEstoqueLoja.query
        .filter(MovEstoqueLoja.tipo == 'venda_seru')
        .filter(MovEstoqueLoja.referencia.like('%(fator%'))
        .filter(MovEstoqueLoja.data >= corte).count())
    out['interpretacao'] = (
        'com_fator_fracionario vazio = NENHUM item de chapa configurado '
        '(va em /pdv/mapeamentos e ajuste o fator de cada item de chapa). '
        'movs_fracionarios_7d > 0 = o desconto ESTA rodando. '
        'debitos_acumulados = fatias ja vendidas aguardando fechar 1 pao '
        'inteiro pra baixar do estoque.')
    return jsonify(out), 200


@main_bp.route('/admin/retencao')
@owner_required
def retencao_admin():
    """Retencao de dados (owner-only). Sem parametro = DRY-RUN: mostra o que
    SERIA apagado por alvo, sem tocar em nada. ?executar=1 apaga de verdade.

    O ciclo automatico roda no cron diario apos o backup OK (RETENCAO_AUTO=0
    desliga). Prazos via env: RETENCAO_LOGS_DIAS(365) /
    RETENCAO_CONVERSAS_DIAS(180) / RETENCAO_EVENTOS_DIAS(7) /
    RETENCAO_BACKUPS_DIAS(90).
    """
    from app.services import retencao

    executar = request.args.get('executar') == '1'
    rel = retencao.executar_limpeza(dry_run=not executar)
    rel['prazos_dias'] = {
        'logs': current_app.config['RETENCAO_LOGS_DIAS'],
        'conversas': current_app.config['RETENCAO_CONVERSAS_DIAS'],
        'eventos': current_app.config['RETENCAO_EVENTOS_DIAS'],
        'backups': current_app.config['RETENCAO_BACKUPS_DIAS'],
    }
    rel['auto_diaria'] = bool(current_app.config.get('RETENCAO_AUTO', True))
    return jsonify(rel), 200


def _saldo_lalamove_json():
    from app.models import LalamoveSaldo
    s = db.session.get(LalamoveSaldo, 1)
    if not s:
        return ('ainda sem evento de carteira — chega no primeiro '
                'debito/recarga apos ativar o webhook')
    return {'valor': str(s.valor) if s.valor is not None else None,
            'moeda': s.moeda,
            'atualizado_em': s.atualizado_em.isoformat(sep=' ',
                                                       timespec='seconds')
            if s.atualizado_em else None,
            'payload_cru': (s.payload_json or '')[:400]}


@main_bp.route('/admin/debug-lalamove')
@owner_required
def debug_lalamove():
    """Diagnóstico das credenciais Lalamove (owner-only). Mostra prefixos
    (nunca a chave inteira) e bate num endpoint autenticado neutro
    (GET /v3/cities): 200 = chave+assinatura OK; 401 = credencial/conta;
    outro = corpo do erro pra leitura."""
    from app.services import lalamove
    key = lalamove._cfg('LALAMOVE_API_KEY') or ''
    secret = lalamove._cfg('LALAMOVE_API_SECRET') or ''
    from app.blueprints.lalamove.routes import ultimo_hit
    out = {
        'configurado': lalamove.disponivel(),
        # ultimo acesso registrado no /lalamove/webhook deste container —
        # diz se o probe do portal chegou ao servidor ou morreu no caminho.
        'webhook_ultimo_hit': (ultimo_hit() or
                               'nenhum acesso DESDE O ULTIMO DEPLOY (o '
                               'rastro zera a cada deploy) — abra '
                               '/lalamove/webhook no navegador e recarregue '
                               'aqui pra testar o caminho de entrada'),
        'saldo_carteira': _saldo_lalamove_json(),
        'key_prefixo': key[:8] + '...' if key else None,
        'key_tamanho': len(key),
        'secret_prefixo': secret[:8] + '...' if secret else None,
        'secret_tamanho': len(secret),
        # espaco/quebra de linha copiado junto e causa classica de 401
        'key_tem_espaco': key != key.strip(),
        'secret_tem_espaco': secret != secret.strip(),
        'base_url': lalamove._base_url(),
        'market': lalamove._cfg('LALAMOVE_MARKET', 'BR') or 'BR',
        'origem_latlng_env': bool(lalamove._cfg('LALAMOVE_ORIGEM_LATLNG')),
    }
    if not out['configurado']:
        out['erro'] = 'LALAMOVE_API_KEY/SECRET ausentes'
        return jsonify(out), 200
    try:
        status, corpo = lalamove._request('GET', '/v3/cities')
        out['teste_cities_status'] = status
        out['teste_cities_ok'] = status == 200
        if status == 200:
            dados = corpo.get('data') or []
            out['cidades'] = [c.get('locode') or c.get('id') for c in dados][:10]
            out['conclusao'] = ('Credenciais e assinatura OK. Se a cotação '
                                'ainda falhar, o problema é no payload — me '
                                'mande este JSON.')
        else:
            out['teste_cities_corpo'] = str(corpo)[:600]
            out['conclusao'] = ('401/erro também no endpoint neutro = chave/'
                                'secret não conferem ou conta sem produção '
                                'ativa (Wallet/aprovação no portal). Não é '
                                'problema do payload de cotação.')
    except Exception as exc:  # noqa: BLE001
        out['erro'] = f'{type(exc).__name__}: {exc}'
    return jsonify(out), 200


@main_bp.route('/admin/debug-sentry')
@owner_required
def debug_sentry():
    """Status do monitoramento de erros (owner-only). ?testar=1 manda um
    evento de teste pro Sentry — confira se chegou no painel sentry.io."""
    import os as _os
    dsn = (_os.environ.get('SENTRY_DSN') or '').strip()
    out = {
        'dsn_configurado': bool(dsn),
        'ambiente': _os.environ.get('SENTRY_ENV', 'production'),
    }
    try:
        import sentry_sdk
        out['sdk_instalado'] = True
        client = sentry_sdk.Hub.current.client
        out['sdk_ativo'] = client is not None
        if request.args.get('testar') == '1':
            if not out['sdk_ativo']:
                out['teste'] = ('NAO enviado: SDK inativo. Configure SENTRY_DSN '
                                'no Railway e redeploye.')
            else:
                event_id = sentry_sdk.capture_message(
                    'Teste manual via /admin/debug-sentry', level='warning')
                out['teste'] = f'enviado (event_id={event_id}) — confira no sentry.io'
    except ImportError:
        out['sdk_instalado'] = False
    if not dsn:
        out['como_ativar'] = (
            '1) Crie projeto Flask gratis em sentry.io; 2) copie o DSN; '
            '3) Railway -> Variables -> SENTRY_DSN=<dsn>; 4) aguarde redeploy; '
            '5) volte aqui com ?testar=1.')
    return jsonify(out), 200


@main_bp.route('/admin/debug-chatwoot')
@owner_required
def debug_chatwoot():
    """Diagnostico do Chatwoot rodando DO SERVIDOR de prod (owner-only).

    Criado em 12/06/2026 durante incidente (WhatsApp "Falha ao enviar" +
    IG "400 Session Invalid" + app "unexpected error"). Distingue em uma
    chamada: hospedagem do Chatwoot fora x token nosso invalido x canais
    Meta desconectados — cada um tem dono e correcao diferentes.

    ?conversa=<id>: alem do diagnostico, busca a conversa e o que falhou
    (erro bruto da Meta). Numero da conversa = o #NNN no topo do
    Chatwoot."""
    from app.services import chatwoot
    out = chatwoot.diagnostico()
    conv = (request.args.get('conversa') or '').strip()
    if conv.isdigit():
        cid = int(conv)
        out['erros_da_conversa_' + conv] = chatwoot.erros_de_envio(cid)
        out['historico_da_conversa_' + conv] = (
            chatwoot.buscar_historico(cid, limite=40))
    return jsonify(out), 200


@main_bp.route('/admin/vnda/contatos')
@login_required
def vnda_contatos():
    """Endereco + contato + DATA DE ENTREGA de uma lista de codes VNDA.

    Criado em 12/06/2026 pro caso operacional 'preciso achar 11 clientes
    pra repor produto estragado'. Aceita ?codes=A,B,C (mais um por linha
    quebrada/espaco/virgula — robusto pra copia-cola do print).

    Acesso: TODOS os usuarios logados (decisao do dono 12/06/2026 — a
    equipe operacional usa pra repor/contatar; mesma classe de PII que
    /entregas/, ja aberta a todos). Era owner-only no nascimento.

    Data de entrega: a OPERACIONAL — se houver OverrideEntrega pro code
    (data alterada no nosso sistema), ela prevalece sobre a do VNDA e
    vem marcada com `data_alterada` + a original.

    Resposta:
      {ok, total, achados, nao_achados,
       clientes: [{code, destinatario, telefone, endereco, data_entrega,
                   data_alterada, data_original, periodo,
                   itens: [{nome,qtd}]}]}.
    """
    import re

    from flask import render_template

    from app.models import OverrideEntrega
    from app.services import vnda
    raw = request.args.get('codes') or request.args.get('q') or ''
    # split robusto: virgula, espaco, quebra de linha, tabs
    codes = [c.strip().upper() for c in re.split(r'[,\s]+', raw) if c.strip()]
    # dedup mantendo ordem
    seen = set()
    codes = [c for c in codes if not (c in seen or seen.add(c))]
    formato = (request.args.get('formato') or '').lower()

    overrides = {}
    if codes:
        for ov in OverrideEntrega.query.filter(
                OverrideEntrega.pedido_code.in_(codes)).all():
            overrides[ov.pedido_code] = ov.data_entrega

    clientes = []
    nao_achados = []
    for code in codes:
        order = vnda.buscar_pedido_completo(code)
        if not order:
            nao_achados.append(code)
            continue
        shipping = vnda.buscar_shipping_address(code)
        client = None
        cid = order.get('client_id')
        if cid:
            try:
                client = vnda.buscar_cliente(cid)
            except Exception:  # noqa: BLE001
                client = None
        p = vnda._normalizar_pedido(order, client_data=client,
                                     shipping_data=shipping)
        data_fmt = p.get('data_entrega_fmt') or ''
        ov = overrides.get(code)
        data_alterada = False
        data_original = None
        if ov:
            data_alterada = True
            data_original = data_fmt
            data_fmt = ov.strftime('%d/%m/%Y')
        clientes.append({
            'code': p.get('code'),
            'destinatario': p.get('destinatario') or p.get('comprador') or '',
            'telefone': p.get('telefone') or '',
            'endereco': p.get('endereco') or '',
            'data_entrega': data_fmt,
            'data_alterada': data_alterada,
            'data_original': data_original,
            'periodo': p.get('periodo') or '',
            'itens': [{'nome': it.get('nome'),
                       'qtd': it.get('quantidade')}
                      for it in (p.get('itens') or [])],
        })
    payload = {
        'ok': True,
        'total': len(codes),
        'achados': len(clientes),
        'nao_achados': nao_achados,
        'clientes': clientes,
    }
    if formato == 'json':
        return jsonify(payload), 200
    # HTML default: tela imprimivel, telefone clicavel, 1 cliente por bloco
    return render_template('main/vnda_contatos.html', dados=payload), 200


@main_bp.route('/admin/zapi/grupos')
@owner_required
def zapi_grupos():
    """Lista os grupos de WhatsApp que o numero do bot participa, com o
    ID pronto pra colar no destino de alertas (owner-only).

    Fluxo (12/06/2026, pedido do dono): criar grupo no WhatsApp →
    adicionar o numero do bot ao grupo → abrir esta rota → copiar o
    `id` (termina em '-group') → colar no Railway em
    CHATBOT_VIGIA_NUMERO (vigia do bot) e CHATWOOT_VIGIA_INFRA_NUMERO
    (vigia de infra) → Apply. O envio pra grupo tem whitelist propria
    que inclui automaticamente esses destinos.

    ?testar=<id-do-grupo>: manda uma mensagem de teste pro grupo na
    hora — fecha o loop da configuracao sem esperar um incidente real.
    Se o grupo nao estiver em nenhuma env de destino, a whitelist
    recusa e o erro aparece no JSON (tambem e diagnostico util)."""
    from app.services import zapi
    out = zapi.listar_grupos()
    testar = (request.args.get('testar') or '').strip()
    if testar:
        out['teste_envio'] = zapi.enviar_texto(
            testar,
            '✅ Teste de alerta — este grupo está configurado pra '
            'receber os avisos do vigia da O Pão.')
    return jsonify(out), 200


@main_bp.route('/admin/debug-bot')
@owner_required
def debug_bot():
    """O que o bot ENXERGA sobre um produto (owner-only).

    Caso real (12/06/2026): vigia alertou 'bot disse esgotado mas tem 872
    un em estoque'. O bot consulta o VNDA (canal de venda do site); o
    vigia compara contra EstoqueLoja (estoque fisico). Fontes diferentes
    explicam o desencontro sem o bot delirar. Esta rota mostra a verdade
    de cada fonte lado a lado pra qualquer produto.

    ?busca=Pain au Chocolat → {vnda: [...], estoque_loja: [{loja, qtd}]}
    """
    from app.services import bot_tools
    busca = (request.args.get('busca') or '').strip()
    if not busca:
        return jsonify({'erro': 'use ?busca=<termo>'}), 400
    out = {'busca': busca}
    try:
        r = bot_tools.consultar_produtos(busca)
        out['vnda'] = r
    except Exception as exc:  # noqa: BLE001
        out['vnda'] = {'erro': f'{type(exc).__name__}: {str(exc)[:200]}'}

    # Estoque interno (mesma fonte que o vigia usa pra comparar)
    from collections import defaultdict

    from app.models import EstoqueLoja
    saldos = defaultdict(lambda: {'qtd_total': 0, 'por_loja': {}})
    try:
        from app.utils import normalizar_busca
        termos = [t for t in normalizar_busca(busca).split() if len(t) > 2]
        for e in EstoqueLoja.query.filter(EstoqueLoja.quantidade > 0).all():
            nome = None
            if e.receita and e.receita.nome:
                nome = e.receita.nome.strip()
            elif e.produto and e.produto.nome:
                nome = e.produto.nome.strip()
            elif (e.nome_pendente or '').strip():
                nome = e.nome_pendente.strip()
            if not nome:
                continue
            nome_norm = normalizar_busca(nome)
            if termos and not all(t in nome_norm for t in termos):
                continue
            loja_nome = (e.loja.nome if e.loja else f'loja_{e.loja_id}')
            saldos[nome]['qtd_total'] += int(e.quantidade or 0)
            saldos[nome]['por_loja'][loja_nome] = (
                saldos[nome]['por_loja'].get(loja_nome, 0)
                + int(e.quantidade or 0))
        out['estoque_loja'] = [
            {'nome': k, **v}
            for k, v in sorted(saldos.items(),
                               key=lambda kv: -kv[1]['qtd_total'])]
    except Exception as exc:  # noqa: BLE001
        out['estoque_loja'] = {'erro': f'{type(exc).__name__}: '
                                       f'{str(exc)[:200]}'}
    return jsonify(out), 200


@main_bp.route('/admin/vigia/diag')
@owner_required
def vigia_diag():
    """Diagnostico do vigia do chatbot: mostra config + ultimos veredictos.

    Owner-only. Pra confirmar que o vigia esta avaliando conversas e que o
    pipeline (Haiku -> Z-API -> WhatsApp do dono) esta funcionando."""
    import os as _os

    from flask import current_app, jsonify

    from app.services import chatbot_vigia
    cfg = current_app.config
    return jsonify({
        'ligado': bool(cfg.get('CHATBOT_VIGIA')),
        'anthropic_api_key_configurada': bool(cfg.get('ANTHROPIC_API_KEY')
                                              or _os.environ.get('ANTHROPIC_API_KEY')),
        'numero_destino': chatbot_vigia._numero_destino(),
        'modelo': chatbot_vigia.MODELO,
        'ultimos_veredictos': chatbot_vigia.ultimos(),
        'tip': ('Pra disparar alerta de teste no seu WhatsApp: '
                'POST /admin/vigia/teste?cenario=estoque '
                '(ou cenario=irritado, ou cenario=silencio)'),
    })


@main_bp.route('/admin/auditor/run', methods=['POST'])
@owner_required
def auditor_run():
    """Roda o auditor proativo do bot AGORA (varre o dia ate este momento) e
    envia o relatorio pro WhatsApp do dono. Owner-only."""
    from flask import flash

    from app.services import chatbot_auditor
    r = chatbot_auditor.auditar_hoje(enviar=True)
    if r.get('enviado'):
        flash('Auditor rodou e enviou o relatorio pro seu WhatsApp.', 'success')
    elif r.get('pulou'):
        flash(f'Auditor pulou: {r["pulou"]}', 'warning')
    elif r.get('erro'):
        flash(f'Auditor falhou: {r["erro"]}', 'danger')
    elif r.get('ok') and not r.get('rel', {}).get('problemas'):
        flash('Auditor rodou: nenhum problema relevante encontrado no periodo.',
              'info')
    else:
        flash(f'Auditor rodou mas nao enviou (sem destino?): {r}', 'warning')
    return redirect(url_for('main.debug_schema'))


@main_bp.route('/admin/vigia/teste', methods=['POST'])
@owner_required
def vigia_teste():
    """Dispara o vigia com conversa SINTETICA pra confirmar que tudo funciona
    de ponta a ponta. Owner-only.

    Cenarios:
      estoque  - bot afirma esgotado pra item que tem nas lojas (ALERTA ALTA)
      irritado - cliente irritado com o atendimento (ALERTA ALTA)
      silencio - conversa normal (NAO deve disparar — controle)
    """
    from flask import flash, jsonify, request

    from app.services import chatbot_vigia
    cenario = (request.args.get('cenario') or request.form.get('cenario')
               or 'estoque').strip().lower()
    if cenario not in ('estoque', 'irritado', 'silencio'):
        cenario = 'estoque'
    resultado = chatbot_vigia.disparar_teste(cenario)
    if request.headers.get('Accept', '').startswith('application/json'):
        return jsonify({'cenario': cenario, 'resultado': resultado})
    if resultado.get('enviado'):
        flash(f'Vigia OK: alerta de TESTE ({cenario}) enviado pro seu WhatsApp.',
              'success')
    elif resultado.get('silencio'):
        flash(f'Vigia avaliou ({cenario}) e decidiu NAO alertar — confere se '
              'o cenario era pra disparar. Veredicto: '
              f'{resultado.get("veredicto")}', 'warning')
    elif resultado.get('pulou'):
        flash(f'Vigia pulou: {resultado["pulou"]} (cheque CHATBOT_VIGIA e '
              'ANTHROPIC_API_KEY)', 'warning')
    else:
        flash(f'Vigia teste falhou: {resultado.get("erro") or resultado}',
              'danger')
    return redirect(url_for('main.debug_schema'))


@main_bp.route('/admin/loja-online/auditoria-catalogo')
@owner_required
def loja_online_auditoria_catalogo():
    """Fase 0 da Loja Online (16/06/2026): auditoria de pre-requisitos do
    catalogo. Quantos produtos ja estao 'prontos pra vitrine' (preco_site +
    imagem) e quantos VNDA-orfaos restam mapear. Read-only — so observa o
    estado, nao muda nada.

    Plano completo: /root/.claude/plans/modular-tinkering-owl.md (Loja
    propria substituindo VNDA). docs/loja-online/fase-0-checklist.md
    lista os passos manuais (Pagar.me sandbox, contador, etc)."""
    from sqlalchemy import or_

    from app.models import Produto, Receita, VndaProdutoMap

    # Receitas
    rec_total = Receita.query.count()
    rec_ativas = Receita.query.filter(Receita.arquivada_em.is_(None)).count()
    rec_preco_site = Receita.query.filter(
        Receita.arquivada_em.is_(None),
        Receita.preco_site.isnot(None),
        Receita.preco_site > 0).count()
    rec_img = Receita.query.filter(
        Receita.arquivada_em.is_(None),
        or_(Receita.imagem_dropbox_url.isnot(None),
            Receita.imagem_url.isnot(None))).count()
    rec_prontas = Receita.query.filter(
        Receita.arquivada_em.is_(None),
        Receita.preco_site.isnot(None), Receita.preco_site > 0,
        or_(Receita.imagem_dropbox_url.isnot(None),
            Receita.imagem_url.isnot(None))).count()
    rec_faltando = (Receita.query
                    .filter(Receita.arquivada_em.is_(None))
                    .filter(or_(Receita.preco_site.is_(None),
                                Receita.preco_site == 0,
                                Receita.imagem_dropbox_url.is_(None),
                                Receita.imagem_url.is_(None)))
                    .order_by(Receita.nome).limit(40).all())

    # Produtos (cestas/kits)
    prod_total = Produto.query.count()
    prod_ativos = Produto.query.filter_by(ativo=True).count()
    prod_preco_site = Produto.query.filter(
        Produto.ativo.is_(True),
        Produto.preco_site.isnot(None),
        Produto.preco_site > 0).count()
    prod_img = Produto.query.filter(
        Produto.ativo.is_(True),
        or_(Produto.imagem_dropbox_url.isnot(None),
            Produto.imagem_url.isnot(None))).count()
    prod_prontos = Produto.query.filter(
        Produto.ativo.is_(True),
        Produto.preco_site.isnot(None), Produto.preco_site > 0,
        or_(Produto.imagem_dropbox_url.isnot(None),
            Produto.imagem_url.isnot(None))).count()
    prod_faltando = (Produto.query
                     .filter_by(ativo=True)
                     .filter(or_(Produto.preco_site.is_(None),
                                 Produto.preco_site == 0,
                                 Produto.imagem_dropbox_url.is_(None),
                                 Produto.imagem_url.is_(None)))
                     .order_by(Produto.nome).limit(40).all())

    # VndaProdutoMap (espelha o que o VNDA vende e mapeia pra catalogo nosso)
    mapa_total = VndaProdutoMap.query.count()
    mapa_mapeado = VndaProdutoMap.query.filter(
        or_(VndaProdutoMap.receita_id.isnot(None),
            VndaProdutoMap.produto_id.isnot(None))).count()
    mapa_orfao = (VndaProdutoMap.query
                  .filter(VndaProdutoMap.receita_id.is_(None),
                          VndaProdutoMap.produto_id.is_(None))
                  .order_by(VndaProdutoMap.primeira_visto_em.desc()).limit(40).all())

    return render_template(
        'admin/loja_online_auditoria_catalogo.html',
        rec_total=rec_total, rec_ativas=rec_ativas,
        rec_preco_site=rec_preco_site, rec_img=rec_img,
        rec_prontas=rec_prontas, rec_faltando=rec_faltando,
        prod_total=prod_total, prod_ativos=prod_ativos,
        prod_preco_site=prod_preco_site, prod_img=prod_img,
        prod_prontos=prod_prontos, prod_faltando=prod_faltando,
        mapa_total=mapa_total, mapa_mapeado=mapa_mapeado,
        mapa_orfao=mapa_orfao,
    )


# ── Loja Online — Fase 1: curadoria de catálogo (16/06/2026) ──────────
#
# Decisao do dono: "todo item com preco_site sobe no site". Esta tela e o
# "comando central" do catalogo: lista compacta com preço inline + upload de
# foto, sem sair da pagina. Edita rapido o que ainda esta faltando antes da
# Fase 2 (vitrine) entrar.
#
# Reusa: `dropbox_storage.upload_publico` + `app.utils.comprimir_imagem` +
# colunas `preco_site` / `imagem_dropbox_url` ja existentes. Sem schema novo.

@main_bp.route('/admin/loja-online/catalogo')
@owner_required
def loja_online_catalogo():
    """Lista combinada de Receitas + Produtos com edicao rapida de preco e
    upload de foto. Filtros via query string: ?filtro=no-site|sem-preco|
    sem-foto|todos (default: todos)."""
    from app.models import Produto, Receita
    from app.services import loja_catalogo
    filtro = (request.args.get('filtro') or 'todos').strip().lower()

    # Estoque atual na loja do site (a mesma de /pedidos/estoque-loja). None =
    # loja do site não configurada → não dá pra editar estoque aqui.
    estoque_map = loja_catalogo._estoque_site_map()

    # Receitas ativas
    rec_q = Receita.query.filter(Receita.arquivada_em.is_(None))
    # Produtos ativos
    prod_q = Produto.query.filter_by(ativo=True)

    receitas = rec_q.order_by(Receita.categoria, Receita.nome).all()
    produtos = prod_q.order_by(Produto.nome).all()

    # Unifica em uma lista com 'tipo' pra o template
    itens = []
    for r in receitas:
        tem_foto = bool(r.imagem_dropbox_url or r.imagem_url)
        tem_preco = r.preco_site is not None and r.preco_site > 0
        item = {
            'tipo': 'receita', 'id': r.id, 'nome': r.nome,
            'categoria': r.categoria or '',
            'ordem_site': r.ordem_site,
            'preco_site': r.preco_site,
            'imagem': r.imagem_dropbox_url or r.imagem_url,
            'no_site': tem_foto and tem_preco,
            'falta_foto': not tem_foto,
            'falta_preco': not tem_preco,
            'estoque': (None if estoque_map is None
                        else estoque_map.get(('receita', r.id), 0)),
        }
        itens.append(item)
    for p in produtos:
        tem_foto = bool(p.imagem_dropbox_url or p.imagem_url)
        tem_preco = p.preco_site is not None and p.preco_site > 0
        item = {
            'tipo': 'produto', 'id': p.id, 'nome': p.nome,
            'categoria': p.categoria or '(cesta/kit)',
            'ordem_site': p.ordem_site,
            'preco_site': p.preco_site,
            'imagem': p.imagem_dropbox_url or p.imagem_url,
            'no_site': tem_foto and tem_preco,
            'falta_foto': not tem_foto,
            'falta_preco': not tem_preco,
            'estoque': (None if estoque_map is None
                        else estoque_map.get(('produto', p.id), 0)),
        }
        itens.append(item)

    if filtro == 'no-site':
        itens = [i for i in itens if i['no_site']]
    elif filtro == 'sem-preco':
        itens = [i for i in itens if i['falta_preco']]
    elif filtro == 'sem-foto':
        itens = [i for i in itens if i['falta_foto']]

    contagens = {
        'todos': len(receitas) + len(produtos),
        'no_site': sum(1 for r in receitas if (r.preco_site or 0) > 0 and (r.imagem_dropbox_url or r.imagem_url))
                  + sum(1 for p in produtos if (p.preco_site or 0) > 0 and (p.imagem_dropbox_url or p.imagem_url)),
        'sem_preco': sum(1 for r in receitas if not r.preco_site or r.preco_site <= 0)
                    + sum(1 for p in produtos if not p.preco_site or p.preco_site <= 0),
        'sem_foto': sum(1 for r in receitas if not (r.imagem_dropbox_url or r.imagem_url))
                   + sum(1 for p in produtos if not (p.imagem_dropbox_url or p.imagem_url)),
    }
    # Lista de categorias já cadastradas (Produtos + Receitas) — alimenta
    # o autocomplete (datalist) na edição inline.
    cats = set()
    for r in receitas:
        if r.categoria:
            cats.add(r.categoria.strip())
    for p in produtos:
        if p.categoria:
            cats.add(p.categoria.strip())
    categorias_existentes = sorted(c for c in cats if c)
    return render_template('admin/loja_online_catalogo.html',
                            itens=itens, filtro=filtro, contagens=contagens,
                            categorias_existentes=categorias_existentes)


@main_bp.route('/admin/loja-online/catalogo/preco/<tipo>/<int:id>',
                methods=['POST'])
@owner_required
def loja_online_catalogo_preco(tipo, id):
    """Atualiza preco_site via AJAX. JSON: {preco: float|null}. Aceita
    null/0 pra TIRAR do site. Owner-only — dinheiro."""
    from decimal import Decimal, InvalidOperation

    from app.extensions import db as _db
    from app.models import Produto, Receita
    if tipo == 'receita':
        obj = Receita.query.get_or_404(id)
    elif tipo == 'produto':
        obj = Produto.query.get_or_404(id)
    else:
        return jsonify(ok=False, erro='tipo inválido'), 400
    dados = request.get_json(silent=True) or {}
    raw = dados.get('preco')
    if raw is None or raw == '' or raw == 0:
        obj.preco_site = None
    else:
        try:
            val = Decimal(str(raw).replace(',', '.'))
        except (InvalidOperation, ValueError, TypeError):
            return jsonify(ok=False, erro='preço inválido'), 400
        if val < 0 or val > 9999:
            return jsonify(ok=False, erro='preço fora da faixa (0 a 9999)'), 400
        obj.preco_site = float(val)
    _db.session.commit()
    return jsonify(ok=True,
                   preco_site=(float(obj.preco_site)
                               if obj.preco_site is not None else None))


@main_bp.route('/admin/loja-online/catalogo/estoque/<tipo>/<int:id>',
                methods=['POST'])
@owner_required
def loja_online_catalogo_estoque(tipo, id):
    """Define o estoque ATUAL do item na loja do site — a MESMA EstoqueLoja
    que /pedidos/estoque-loja usa. JSON: {estoque: int}. SET absoluto: grava
    a diferença como MovEstoqueLoja pra manter o histórico consistente.
    Owner-only (estoque tem peso especial)."""
    from app.extensions import db as _db
    from app.models import EstoqueLoja, MovEstoqueLoja
    from app.services.loja_pagamento import loja_origem_site
    if tipo not in ('receita', 'produto'):
        return jsonify(ok=False, erro='tipo inválido'), 400
    loja = loja_origem_site()
    if not loja:
        return jsonify(ok=False, erro='loja do site não configurada'), 400
    dados = request.get_json(silent=True) or {}
    raw = dados.get('estoque')
    if raw is None or str(raw).strip() == '':
        return jsonify(ok=False, erro='quantidade obrigatória'), 400
    try:
        novo = int(str(raw).strip())
    except (TypeError, ValueError):
        return jsonify(ok=False, erro='quantidade inválida'), 400
    if novo < 0 or novo > 100000:
        return jsonify(ok=False, erro='quantidade fora da faixa (0 a 100000)'), 400
    filtro = {'loja_id': loja.id,
              ('receita_id' if tipo == 'receita' else 'produto_id'): id}
    el = EstoqueLoja.query.filter_by(**filtro).first()
    atual = (el.quantidade or 0) if el else 0
    if not el:
        el = EstoqueLoja(quantidade=0, **filtro)
        _db.session.add(el)
        _db.session.flush()
    delta = novo - atual
    el.quantidade = novo
    if delta != 0:
        _db.session.add(MovEstoqueLoja(
            estoque_loja_id=el.id,
            tipo='entrada_manual' if delta > 0 else 'ajuste_negativo',
            quantidade=abs(delta),
            referencia='ajuste catálogo do site',
            usuario_id=current_user.id))
    _db.session.commit()
    return jsonify(ok=True, estoque=el.quantidade)


@main_bp.route('/admin/loja-online/catalogo/ordem/<tipo>/<int:id>',
                methods=['POST'])
@owner_required
def loja_online_catalogo_ordem(tipo, id):
    """Atualiza a `ordem_site` do item (edição inline). JSON:
    {ordem: int|null}. Vazio/null = item vai pro fim alfabético."""
    from app.extensions import db as _db
    from app.models import Produto, Receita
    if tipo == 'receita':
        obj = Receita.query.get_or_404(id)
    elif tipo == 'produto':
        obj = Produto.query.get_or_404(id)
    else:
        return jsonify(ok=False, erro='tipo inválido'), 400
    dados = request.get_json(silent=True) or {}
    raw = dados.get('ordem')
    if raw is None or raw == '':
        obj.ordem_site = None
    else:
        try:
            obj.ordem_site = int(raw)
        except (TypeError, ValueError):
            return jsonify(ok=False, erro='ordem precisa ser inteiro'), 400
    _db.session.commit()
    return jsonify(ok=True, ordem=obj.ordem_site)


@main_bp.route('/admin/loja-online/categorias/ordem', methods=['POST'])
@owner_required
def loja_online_categorias_ordem():
    """Salva a nova ordem das categorias em lote.
    Body JSON: {ordem: ['Pães', 'Bebidas', 'Conservas']}.
    Faz upsert pra cada (ordem = índice).
    """
    from app.models import CategoriaSite
    dados = request.get_json(silent=True) or {}
    nomes = dados.get('ordem') or []
    if not isinstance(nomes, list):
        return jsonify(ok=False, erro='ordem precisa ser lista'), 400
    existentes = {c.nome: c for c in CategoriaSite.query.all()}
    for i, nome in enumerate(nomes):
        nome = (nome or '').strip()[:50]
        if not nome:
            continue
        if nome in existentes:
            existentes[nome].ordem = i
        else:
            db.session.add(CategoriaSite(nome=nome, ordem=i))
    db.session.commit()
    return jsonify(ok=True, salvas=len([n for n in nomes if n]))


@main_bp.route('/admin/loja-online/produtos/ordem', methods=['POST'])
@owner_required
def loja_online_produtos_ordem():
    """Salva a nova ordem dos PRODUTOS dentro de uma categoria em lote.
    Body JSON: {itens: [{tipo: 'produto'|'receita', id: int}, ...]}.
    O índice na lista vira o `ordem_site` (do menor pro maior).
    """
    from app.models import Produto, Receita
    dados = request.get_json(silent=True) or {}
    itens = dados.get('itens') or []
    if not isinstance(itens, list):
        return jsonify(ok=False, erro='itens precisa ser lista'), 400
    salvas = 0
    for i, it in enumerate(itens):
        tipo = (it.get('tipo') or '').strip()
        try:
            iid = int(it.get('id'))
        except (TypeError, ValueError):
            continue
        if tipo == 'produto':
            obj = Produto.query.get(iid)
        elif tipo == 'receita':
            obj = Receita.query.get(iid)
        else:
            continue
        if not obj:
            continue
        obj.ordem_site = i
        salvas += 1
    db.session.commit()
    return jsonify(ok=True, salvas=salvas)


@main_bp.route('/admin/loja-online/ordem-produtos')
@owner_required
def loja_online_ordem_produtos():
    """Tela pra reordenar PRODUTOS por categoria via drag-and-drop.
    Agrupa publicados pela categoria; cada grupo é uma lista sortable."""
    from app.services import loja_catalogo
    itens = loja_catalogo.produtos_publicados()
    grupos = loja_catalogo.por_categorias(itens)
    return render_template('admin/loja_online_ordem_produtos.html',
                            grupos=grupos)


@main_bp.route('/admin/loja-online/categorias', methods=['GET', 'POST'])
@owner_required
def loja_online_categorias():
    """Gestão da ordem das categorias na vitrine. GET mostra; POST salva."""
    from app.models import CategoriaSite, Produto, Receita
    # Coleta TODAS as categorias usadas no catálogo (Produto + Receita).
    cats_uso = set()
    for r in Receita.query.with_entities(Receita.categoria).distinct():
        if r[0]:
            cats_uso.add(r[0].strip())
    for p in Produto.query.with_entities(Produto.categoria).distinct():
        if p[0]:
            cats_uso.add(p[0].strip())

    if request.method == 'POST':
        # Recebe pares (nome, ordem) e upserta. Categoria removida do form
        # vira sem peso (vai pro fim alfabético).
        nomes = request.form.getlist('nome')
        ordens = request.form.getlist('ordem')
        existentes = {c.nome: c for c in CategoriaSite.query.all()}
        for nome, ord_str in zip(nomes, ordens):
            nome = (nome or '').strip()[:50]
            if not nome:
                continue
            try:
                ordem = int(ord_str)
            except (TypeError, ValueError):
                ordem = 0
            if nome in existentes:
                existentes[nome].ordem = ordem
            else:
                db.session.add(CategoriaSite(nome=nome, ordem=ordem))
        db.session.commit()
        from flask import flash
        flash('Ordem das categorias atualizada.', 'success')
        return redirect(url_for('main.loja_online_categorias'))

    existentes = {c.nome: c.ordem for c in CategoriaSite.query.all()}
    # Combina: começa com as que TÊM ordem (em ordem), depois as outras
    # (alfabética).
    com_ordem = sorted(
        ((n, o) for n, o in existentes.items() if n in cats_uso),
        key=lambda x: (x[1], x[0].lower()))
    sem_ordem = sorted(
        ((n, 0) for n in cats_uso if n not in existentes),
        key=lambda x: x[0].lower())
    linhas = com_ordem + sem_ordem
    return render_template('admin/loja_online_categorias.html',
                            linhas=linhas)


@main_bp.route('/admin/loja-online/catalogo/categoria/<tipo>/<int:id>',
                methods=['POST'])
@owner_required
def loja_online_catalogo_categoria(tipo, id):
    """Atualiza a categoria do item (edição inline). JSON: {categoria: str}.
    Vazio limpa (item cai em 'Outros' na vitrine)."""
    from app.extensions import db as _db
    from app.models import Produto, Receita
    if tipo == 'receita':
        obj = Receita.query.get_or_404(id)
    elif tipo == 'produto':
        obj = Produto.query.get_or_404(id)
    else:
        return jsonify(ok=False, erro='tipo inválido'), 400
    dados = request.get_json(silent=True) or {}
    cat = (dados.get('categoria') or '').strip()[:50] or None
    obj.categoria = cat
    _db.session.commit()
    return jsonify(ok=True, categoria=cat or '')


@main_bp.route('/admin/loja-online/catalogo/foto/<tipo>/<int:id>',
                methods=['POST'])
@owner_required
def loja_online_catalogo_foto(tipo, id):
    """Upload de foto via AJAX. JSON de resposta: {ok, imagem_url}. Reusa
    `comprimir_imagem` + `dropbox_storage.upload_publico` (padrão de
    `cardapio_img_upload`)."""
    from app.extensions import db as _db
    from app.models import Produto, Receita
    from app.services import dropbox_storage
    from app.utils import comprimir_imagem
    if tipo == 'receita':
        obj = Receita.query.get_or_404(id)
    elif tipo == 'produto':
        obj = Produto.query.get_or_404(id)
    else:
        return jsonify(ok=False, erro='tipo inválido'), 400

    f = request.files.get('imagem_arquivo') or request.files.get('foto')
    if not f or not f.filename:
        return jsonify(ok=False, erro='nenhum arquivo enviado'), 400
    if not (f.mimetype or '').startswith('image/'):
        return jsonify(ok=False, erro='arquivo não é imagem'), 400
    data = f.read()
    if not data:
        return jsonify(ok=False, erro='arquivo vazio'), 400
    if len(data) > 25 * 1024 * 1024:
        return jsonify(ok=False, erro='imagem maior que 25MB'), 400
    try:
        final = comprimir_imagem(data)
        if dropbox_storage.disponivel():
            path = f'/cardapio/{tipo}/{obj.id}.jpg'
            info = dropbox_storage.upload_publico(
                final, path, mode='overwrite', autorename=False)
            obj.imagem_dropbox_url = info['url']
            obj.imagem_storage_path = info['storage_path']
            obj.imagem_blob = None
        else:
            obj.imagem_blob = final
        obj.imagem_mimetype = 'image/jpeg'
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, erro=f'erro ao processar: {exc}'), 500
    _db.session.commit()
    return jsonify(ok=True,
                   imagem_url=(obj.imagem_dropbox_url or ''))


@main_bp.route('/admin/loja-online/logo', methods=['POST'])
@owner_required
def loja_online_logo():
    """Upload do logotipo da loja → Dropbox → URL guardada em AppConfig
    (`loja_logo_url`). O header da vitrine renderiza o logo se setado, senão
    cai no wordmark de texto. Preserva transparência (PNG/SVG) pra não ficar
    caixa branca sobre o fundo creme."""
    from flask import flash

    from app.models import AppConfig
    from app.services import dropbox_storage
    from app.utils import comprimir_logo
    f = request.files.get('logo')
    if not f or not f.filename:
        flash('Selecione um arquivo de imagem.', 'warning')
        return redirect(url_for('main.loja_online_dashboard'))
    if not (f.mimetype or '').startswith('image/'):
        flash('O arquivo precisa ser uma imagem (PNG, SVG ou JPG).', 'warning')
        return redirect(url_for('main.loja_online_dashboard'))
    data = f.read()
    if not data:
        flash('Arquivo vazio.', 'warning')
        return redirect(url_for('main.loja_online_dashboard'))
    if len(data) > 10 * 1024 * 1024:
        flash('Logo grande demais (máx 10MB).', 'warning')
        return redirect(url_for('main.loja_online_dashboard'))
    if not dropbox_storage.disponivel():
        flash('Dropbox não configurado — não dá pra subir o logo agora.',
              'danger')
        return redirect(url_for('main.loja_online_dashboard'))
    try:
        proc, _mime, ext = comprimir_logo(data)
        info = dropbox_storage.upload_publico(
            proc, f'/loja/logo.{ext}', mode='overwrite', autorename=False)
        AppConfig.set('loja_logo_url', info['url'])
        db.session.commit()
        flash('Logo atualizado!', 'success')
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        flash(f'Erro ao subir o logo: {exc}', 'danger')
    return redirect(url_for('main.loja_online_dashboard'))


@main_bp.route('/admin/loja-online/logo/remover', methods=['POST'])
@owner_required
def loja_online_logo_remover():
    """Volta o header pro wordmark de texto (limpa `loja_logo_url`)."""
    from flask import flash

    from app.models import AppConfig
    AppConfig.set('loja_logo_url', None)
    db.session.commit()
    flash('Logo removido — header volta ao texto.', 'success')
    return redirect(url_for('main.loja_online_dashboard'))


@main_bp.route('/admin/debug-redirect-dominio')
@owner_required
def debug_redirect_dominio():
    """Confirma como está o redirect do domínio antigo. Mostra os hosts
    armados e o destino — sem segredos. Use pra checar que o
    SITE_REDIRECT_HOSTS no Railway ficou certo ANTES de mexer no DNS."""
    cfg = current_app.config
    hosts = [h.strip().lower() for h in (cfg.get('SITE_REDIRECT_HOSTS') or '')
             .split(',') if h.strip()]
    destino = (cfg.get('SITE_REDIRECT_DESTINO')
               or 'https://opao.online').rstrip('/') + '/'
    return jsonify(
        ativo=bool(hosts),
        hosts=hosts,
        destino=destino,
        instrucao=('Hosts armados — vai responder 302 pro destino quando o '
                   'DNS apontar pra cá.' if hosts else
                   'Inerte — defina SITE_REDIRECT_HOSTS no Railway.'),
    )


@main_bp.route('/admin/loja-online/prontidao')
@owner_required
def loja_online_prontidao():
    """Pré-flight do CUTOVER: o que precisa estar pronto ANTES de apontar o
    domínio antigo pro site novo. GO/NO-GO + pendências. O bloqueio nº 1 é
    LOJA_VISIVEL — sem ela, o cliente anônimo redirecionado vê 404."""
    from app.blueprints.loja.routes import _loja_visivel_publico
    from app.services import loja_catalogo, pagarme
    cfg = current_app.config

    loja_visivel = _loja_visivel_publico()
    pg_ambiente = pagarme.ambiente()
    pg_ok = pagarme.disponivel() and pg_ambiente == 'producao'
    redirect_hosts = [h.strip() for h in (cfg.get('SITE_REDIRECT_HOSTS') or '')
                      .split(',') if h.strip()]

    # Produtos no site sem estoque (aparecem como "Esgotado") — aviso, não bloqueio.
    mapa = loja_catalogo._estoque_site_map() or {}
    esgotados = sum(1 for it in loja_catalogo.produtos_publicados()
                    if not (mapa.get((it['kind'], it['id'])) or 0) > 0)

    pendencias = []
    if not loja_visivel:
        pendencias.append('BLOQUEIO: LOJA_VISIVEL não é 1 — cliente anônimo vê '
                          '404. Defina LOJA_VISIVEL=1 no Railway ANTES de trocar '
                          'o DNS.')
    if not pg_ok:
        pendencias.append(f'BLOQUEIO: Pagar.me não está produção/ok '
                          f'(ambiente={pg_ambiente}).')

    return jsonify(
        pronto=(loja_visivel and pg_ok),
        loja_visivel=loja_visivel,
        pagarme_ambiente=pg_ambiente,
        pagarme_ok=pg_ok,
        redirect_hosts_armados=redirect_hosts,
        produtos_esgotados=esgotados,
        pendencias=pendencias,
        nota=('produtos_esgotados é AVISO (eles aparecem como "Esgotado" no '
              'site até você preencher o estoque), não bloqueia o cutover.'),
    )


# ── Debug Pagar.me: valida a chave sem expor o segredo (Fase 4) ───────────
@main_bp.route('/admin/debug-pagarme')
@owner_required
def debug_pagarme():
    """Diagnóstico do Pagar.me (owner-only). Confirma se a chave cadastrada
    no Railway é válida e em qual ambiente (sandbox/produção), SEM expor o
    segredo. Útil pra saber se as chaves são reais ou placeholders."""
    from app.services import pagarme
    cfg = current_app.config
    seg = (cfg.get('PAGARME_WEBHOOK_SECRET') or '').strip()
    # Máscara do secret VIVO neste container (owner-only): len + 4 primeiros +
    # 4 últimos. Serve pra confirmar que o redeploy do Railway já aplicou o
    # valor novo (inicio muda) ANTES de reenviar o webhook no Pagar.me.
    secret_mascara = ({'len': len(seg), 'inicio': seg[:4], 'fim': seg[-4:]}
                      if len(seg) > 8 else {'len': len(seg)})
    return jsonify(
        configurado=pagarme.disponivel(),
        ambiente=pagarme.ambiente(),
        api_key_len=len((cfg.get('PAGARME_API_KEY') or '')),
        api_key_prefixo=pagarme.prefixo_chave(),
        public_key_len=len((cfg.get('PAGARME_PUBLIC_KEY') or '')),
        public_key_prefixo=pagarme.prefixo_public(),
        webhook_secret_set=bool(seg),
        webhook_secret_mascara=secret_mascara,
        resultado=pagarme.validar_chave(),
    )


@main_bp.route('/admin/debug-pagarme/ultimo-webhook')
@owner_required
def debug_pagarme_ultimo_webhook():
    """Mostra metadados MASCARADOS do último hit do webhook (esperado vs
    fornecido). Útil pra entender por que o Pagar.me marca "Falha":
    `bate: false` + `status: 401` → secret divergente. Os campos
    `inicio`/`fim` mostram só 4 chars de cada lado pra COMPARAÇÃO visual,
    sem expor o valor."""
    from app.blueprints.loja.routes import ler_ultimo_hit_pagarme
    hit = ler_ultimo_hit_pagarme()
    if not hit:
        return jsonify(erro='nenhum hit registrado neste container ainda; '
                       'reenvie um webhook pelo painel do Pagar.me e tente '
                       'de novo')
    return jsonify(hit)


@main_bp.route('/admin/debug-pagarme/conciliar/<codigo>')
@owner_required
def debug_pagarme_conciliar(codigo):
    """Conciliação manual (owner) — rede de segurança pra webhook perdido.
    Consulta o Pagar.me pelo order_id salvo; com ?aplicar=1 marca o pedido
    pago se o gateway confirmar (baixa estoque + e-mail). Sem ?aplicar=1 =
    dry-run. Idempotente: ignora a idempotência do webhook e _marcar_pago é
    no-op se já pago."""
    from app.services import loja_pagamento
    aplicar = request.args.get('aplicar') == '1'
    res = loja_pagamento.conciliar_pedido(codigo, aplicar=aplicar)
    return jsonify(res), (200 if res.get('ok') else 400)


@main_bp.route('/admin/debug-pagarme/eventos')
@owner_required
def debug_pagarme_eventos():
    """Lista os últimos eventos do webhook do Pagar.me recebidos pelo
    servidor. Diagnóstico: webhook NÃO chegou = nada aqui (URL/secret/
    seleção de eventos errados no painel do Pagar.me)."""
    from app.models import PagarmeEvento
    n = max(1, min(int(request.args.get('n', 20)), 200))
    eventos = (PagarmeEvento.query
               .order_by(PagarmeEvento.recebido_em.desc()).limit(n).all())
    return jsonify(total=len(eventos), eventos=[
        {'evento_id': e.evento_id, 'tipo': e.tipo,
         'recebido_em': e.recebido_em.isoformat(sep=' ', timespec='seconds')
                       if e.recebido_em else None}
        for e in eventos])


@main_bp.route('/admin/debug-pagarme/pedido/<codigo>')
@owner_required
def debug_pagarme_pedido(codigo):
    """Raio-X de UM pedido pra entender por que o status não mudou:
    status atual, pagamentos com order_id/charge_id do Pagar.me, e os
    eventos do webhook por id (precisa bater pelo `data.id`/`data.code`)."""
    from app.models import PagamentoOnline, PedidoOnline
    p = PedidoOnline.query.filter_by(codigo=codigo).first()
    if not p:
        return jsonify(erro='pedido nao encontrado', codigo=codigo), 404
    pagamentos = PagamentoOnline.query.filter_by(pedido_id=p.id).all()
    return jsonify(
        codigo=p.codigo, status=p.status,
        valor_total=str(p.valor_total),
        criado_em=p.criado_em.isoformat(sep=' ', timespec='seconds')
                 if p.criado_em else None,
        pagamentos=[{
            'id': pg.id, 'metodo': pg.metodo, 'status': pg.status,
            'pagarme_order_id': pg.pagarme_order_id,
            'pagarme_charge_id': pg.pagarme_charge_id,
            'criado_em': pg.criado_em.isoformat(sep=' ', timespec='seconds')
                        if pg.criado_em else None,
            'erro': pg.erro,
        } for pg in pagamentos],
    )


# ── Pedidos do site (Fase 3): acompanhamento dos PedidoOnline ─────────────
# Tela pra o dono acompanhar os pedidos que entram pelo checkout nativo. Read
# + cancelar. Em Fase 3 o pedido nasce 'aguardando_pagamento' e NAO baixa
# estoque; cancelar aqui e so mudanca de status (sem estorno/refund — isso
# entra na Fase 4 com o Pagar.me).

_STATUS_PEDIDO_ONLINE_LABEL = {
    'aguardando_pagamento': 'Aguardando pagamento',
    'pago': 'Pago',
    'em_preparo': 'Em preparo',
    'a_caminho': 'A caminho',
    'entregue': 'Entregue',
    'cancelado': 'Cancelado',
}


@main_bp.route('/admin/loja-online/estoque-vitrine')
@owner_required
def loja_online_estoque_vitrine():
    """Diagnóstico (owner): pra cada produto publicado no site, mostra o
    saldo na loja do site e se está EM ESTOQUE ou ESGOTADO (saldo 0 ou sem
    linha = esgotado). Nada some da vitrine — esgotado aparece com selo e sem
    botão de comprar. Use pra preencher estoque em `/pedidos/estoque-loja`."""
    from app.services import loja_catalogo
    from app.services.loja_pagamento import loja_origem_site
    loja = loja_origem_site()
    mapa = loja_catalogo._estoque_site_map() or {}
    itens = []
    for it in loja_catalogo.produtos_publicados():
        saldo = mapa.get((it['kind'], it['id']))  # None = sem linha
        itens.append({
            'nome': it['nome'], 'kind': it['kind'], 'id': it['id'],
            'categoria': it['categoria'], 'saldo': saldo,
            'esgotado': not (saldo and saldo > 0),
        })
    esgotados = [i for i in itens if i['esgotado']]
    return jsonify(
        loja_site=(loja.nome if loja else None),
        total_publicados=len(itens),
        em_estoque=len(itens) - len(esgotados),
        esgotados=len(esgotados),
        itens_esgotados=esgotados,
        itens=itens,
    )


@main_bp.route('/admin/loja-online')
@owner_required
def loja_online_dashboard():
    """Visão geral da loja online: contagens por status, faturamento por
    janela (hoje/semana/mês) e fila do que precisa de ação do admin."""
    from datetime import timedelta

    from sqlalchemy import func as _func

    from app.models import PedidoOnline
    from app.utils import agora
    hoje = agora().date()
    ini_semana = hoje - timedelta(days=hoje.weekday())
    ini_mes = hoje.replace(day=1)

    def _stats(desde):
        # Faturamento e contagem dos pedidos PAGOS (não cancelados) desde X.
        q = db.session.query(
            _func.coalesce(_func.sum(PedidoOnline.valor_total), 0),
            _func.count(PedidoOnline.id),
        ).filter(
            PedidoOnline.criado_em >= desde,
            PedidoOnline.status.in_(
                ('pago', 'em_preparo', 'a_caminho', 'entregue')),
        )
        valor, count = q.first()
        return {'valor': float(valor or 0), 'count': count or 0}

    janelas = {
        'hoje': _stats(hoje),
        'semana': _stats(ini_semana),
        'mes': _stats(ini_mes),
    }

    contagens = dict(db.session.query(PedidoOnline.status, _func.count())
                     .group_by(PedidoOnline.status).all())

    # Fila do admin: precisam de ação (pago = emitir NF + começar preparo;
    # em_preparo + a_caminho = entregar). Aguardando pagamento NÃO entra
    # (cliente é quem age — não atrapalha o admin).
    fila = (PedidoOnline.query
            .filter(PedidoOnline.status.in_(
                ('pago', 'em_preparo', 'a_caminho')))
            .order_by(PedidoOnline.data_entrega.asc().nullslast(),
                      PedidoOnline.criado_em.asc())
            .limit(15).all())

    from app.models import AppConfig
    return render_template(
        'admin/loja_online_dashboard.html',
        janelas=janelas, contagens=contagens, fila=fila,
        labels=_STATUS_PEDIDO_ONLINE_LABEL,
        logo_url=AppConfig.get('loja_logo_url'))


@main_bp.route('/admin/loja-online/pedidos')
@owner_required
def loja_online_pedidos():
    """Lista os pedidos do site (mais recentes primeiro), com filtros por
    status e por data de entrega (?data=YYYY-MM-DD ou intervalo
    ?data_ini=&data_fim=). Mostra a contagem por status (sempre global —
    bate com os botões de filtro)."""
    from datetime import date as _date

    from sqlalchemy import func as _func

    from app.models import PedidoOnline
    status = (request.args.get('status') or '').strip()
    data_str = (request.args.get('data') or '').strip()
    data_ini_str = (request.args.get('data_ini') or '').strip()
    data_fim_str = (request.args.get('data_fim') or '').strip()

    def _parse(s):
        try:
            return _date.fromisoformat(s) if s else None
        except ValueError:
            return None

    data = _parse(data_str)
    data_ini = _parse(data_ini_str)
    data_fim = _parse(data_fim_str)

    q = PedidoOnline.query
    if status:
        q = q.filter_by(status=status)
    if data:
        q = q.filter(PedidoOnline.data_entrega == data)
    else:
        if data_ini:
            q = q.filter(PedidoOnline.data_entrega >= data_ini)
        if data_fim:
            q = q.filter(PedidoOnline.data_entrega <= data_fim)

    pedidos = q.order_by(PedidoOnline.criado_em.desc()).limit(200).all()
    contagens = dict(db.session.query(PedidoOnline.status, _func.count())
                     .group_by(PedidoOnline.status).all())
    return render_template(
        'admin/loja_online_pedidos.html',
        pedidos=pedidos, status=status, contagens=contagens,
        total=sum(contagens.values()), labels=_STATUS_PEDIDO_ONLINE_LABEL,
        data=data_str, data_ini=data_ini_str, data_fim=data_fim_str,
        filtro_data_ativo=bool(data or data_ini or data_fim))


@main_bp.route('/admin/loja-online/buscar-pedidos')
@owner_required
def loja_online_pedidos_buscar():
    """Busca incremental (AJAX) por nome, telefone, e-mail ou código.
    Respeita o filtro de data ATIVO (passado nos params) — sem isso a busca
    sobrescreveria a lista filtrada por data com pedidos de outros dias,
    confundindo o operador (CLAUDE.md: filtros não podem se ignorar). Sem
    data ativa, busca em todos os pedidos."""
    from datetime import date as _date

    from sqlalchemy import or_

    from app.models import PedidoOnline
    q = (request.args.get('q') or '').strip()
    if len(q) < 2:
        return ''  # nada a buscar — o JS restaura a lista inicial
    termo = f'%{q}%'

    def _parse(s):
        try:
            return _date.fromisoformat(s) if s else None
        except ValueError:
            return None

    data = _parse((request.args.get('data') or '').strip())
    data_ini = _parse((request.args.get('data_ini') or '').strip())
    data_fim = _parse((request.args.get('data_fim') or '').strip())

    qry = PedidoOnline.query.filter(or_(
        PedidoOnline.nome_cliente.ilike(termo),
        PedidoOnline.telefone_cliente.ilike(termo),
        PedidoOnline.email_cliente.ilike(termo),
        PedidoOnline.codigo.ilike(termo),
    ))
    if data:
        qry = qry.filter(PedidoOnline.data_entrega == data)
    else:
        if data_ini:
            qry = qry.filter(PedidoOnline.data_entrega >= data_ini)
        if data_fim:
            qry = qry.filter(PedidoOnline.data_entrega <= data_fim)
    pedidos = (qry.order_by(PedidoOnline.criado_em.desc())
               .limit(50).all())
    return render_template('admin/_loja_online_pedidos_rows.html',
                           pedidos=pedidos, labels=_STATUS_PEDIDO_ONLINE_LABEL)


# Modos de entrega editáveis (espelha loja_checkout.criar_pedido).
_MODOS_ENTREGA = ('agendada', 'retirada', 'express')


@main_bp.route('/admin/loja-online/pedidos/<codigo>')
@owner_required
def loja_online_pedido_detalhe(codigo):
    from app.models import PedidoOnline
    from app.services import loja_checkout
    p = PedidoOnline.query.filter_by(codigo=codigo).first_or_404()
    return render_template('admin/loja_online_pedido_detalhe.html',
                           p=p, labels=_STATUS_PEDIDO_ONLINE_LABEL,
                           lojas=loja_checkout.lojas_retirada(),
                           modos=_MODOS_ENTREGA)


@main_bp.route('/admin/loja-online/pedidos/<codigo>/editar', methods=['POST'])
@owner_required
def loja_online_pedido_editar(codigo):
    """Edita os dados LOGÍSTICOS/CONTATO do pedido — o que a operação precisa
    corrigir depois do pedido feito: cartinha, data/janela, endereço, contato,
    destinatário, modo de entrega e loja de retirada.

    NÃO mexe em DINHEIRO (itens, subtotal, frete, total ficam intactos —
    CLAUDE.md: dinheiro tem peso especial; mudar valor é reembolso/novo
    pedido). Trocar o endereço NÃO recalcula frete: o cliente já pagou; isto
    é só correção de destino pra entrega."""
    from datetime import date as _date

    from flask import flash

    from app.models import PedidoOnline
    p = PedidoOnline.query.filter_by(codigo=codigo).first_or_404()
    f = request.form

    def _s(k):
        return (f.get(k) or '').strip()

    erros = []
    nome = _s('nome_cliente')
    email = _s('email_cliente')
    if not nome:
        erros.append('Nome do cliente é obrigatório.')
    if '@' not in email:
        erros.append('E-mail do cliente inválido.')
    modo = _s('modo_entrega') or p.modo_entrega
    if modo not in _MODOS_ENTREGA:
        erros.append('Modo de entrega inválido.')
    data_str = _s('data_entrega')
    data_entrega = None
    if data_str:
        try:
            data_entrega = _date.fromisoformat(data_str)
        except ValueError:
            erros.append('Data de entrega inválida (use o seletor).')
    if erros:
        for e in erros:
            flash(e, 'danger')
        return redirect(url_for('main.loja_online_pedido_detalhe',
                                codigo=codigo))

    p.nome_cliente = nome
    p.email_cliente = email
    p.telefone_cliente = _s('telefone_cliente') or None
    p.nome_destinatario = _s('nome_destinatario') or None
    p.telefone_destinatario = _s('telefone_destinatario') or None
    p.modo_entrega = modo
    p.cartinha = _s('cartinha') or None
    p.data_entrega = data_entrega
    p.janela_entrega = _s('janela_entrega') or None

    if modo == 'retirada':
        try:
            p.loja_retirada_id = int(f.get('loja_retirada_id')) or None
        except (TypeError, ValueError):
            p.loja_retirada_id = None
    else:
        p.loja_retirada_id = None
        p.endereco_cep = _s('endereco_cep') or None
        p.endereco_logradouro = _s('endereco_logradouro') or None
        p.endereco_numero = _s('endereco_numero') or None
        p.endereco_complemento = _s('endereco_complemento') or None
        p.endereco_bairro = _s('endereco_bairro') or None
        p.endereco_cidade = _s('endereco_cidade') or None
        p.endereco_uf = (_s('endereco_uf')[:2].upper()) or None
        partes = [p.endereco_logradouro, p.endereco_numero,
                  p.endereco_complemento, p.endereco_bairro,
                  p.endereco_cidade, p.endereco_uf]
        p.endereco_entrega = ', '.join(x for x in partes if x) or None

    db.session.commit()
    current_app.logger.info('pedido online %s editado por uid=%s',
                            codigo, getattr(current_user, 'id', None))
    flash(f'Pedido {p.codigo} atualizado.', 'success')
    return redirect(url_for('main.loja_online_pedido_detalhe', codigo=codigo))


@main_bp.route('/admin/loja-online/pedidos/<codigo>/imprimir.pdf')
@owner_required
def loja_online_pedido_imprimir(codigo):
    """PDF de impressão do pedido — MESMO layout do /entregas (via cliente +
    via motoboy). Reusa o serializador e o gerador de PDF de entregas pra o
    formato não divergir."""
    from app.blueprints.entregas.routes import (
        _aplicar_cartinhas,
        _serializar_pedido_online,
    )
    from app.models import PedidoOnline
    from app.services import pdf as pdf_svc
    from app.utils import hoje
    p = PedidoOnline.query.filter_by(codigo=codigo).first_or_404()
    d = _serializar_pedido_online(p)
    _aplicar_cartinhas([d])  # resolve a cartinha (manual sobrepõe, igual painel)
    data = p.data_entrega or hoje()
    conteudo = pdf_svc.gerar_pedidos_pdf([d], ['cliente', 'motorista'], data)
    resp = current_app.response_class(conteudo, mimetype='application/pdf')
    resp.headers['Content-Disposition'] = (
        f'inline; filename="pedido_{p.codigo}.pdf"')
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@main_bp.route('/admin/loja-online/pedidos/<codigo>/cancelar', methods=['POST'])
@owner_required
def loja_online_pedido_cancelar(codigo):
    """Cancela/reembolsa um pedido do site.

    - Pago: dispara REEMBOLSO no Pagar.me + estorno de estoque
      (loja_pagamento.reembolsar_pedido).
    - Aguardando pagamento: só marca cancelado (nada foi cobrado/baixado).
    - Entregue/cancelado: bloqueia."""
    from flask import flash

    from app.models import PedidoOnline
    from app.services import loja_pagamento
    from app.utils import agora
    p = PedidoOnline.query.filter_by(codigo=codigo).first_or_404()
    if p.status in ('entregue', 'cancelado'):
        flash(f'Pedido {p.codigo} nao pode ser cancelado (status '
              f'{p.status}).', 'warning')
    elif p.status == 'pago':
        ok, msg = loja_pagamento.reembolsar_pedido(p)
        flash(f'{p.codigo}: {msg}', 'success' if ok else 'danger')
    else:
        p.status = 'cancelado'
        p.cancelado_em = agora()
        db.session.commit()
        flash(f'Pedido {p.codigo} cancelado.', 'success')
    return redirect(url_for('main.loja_online_pedido_detalhe', codigo=codigo))


# Transições válidas de status pra UI (admin). 'cancelado' tem rota própria
# (cancelar) porque envolve reembolso/estorno; aqui só os avanços manuais.
_STATUS_AVANCO = ('em_preparo', 'a_caminho', 'entregue')


@main_bp.route('/admin/loja-online/pedidos/<codigo>/status', methods=['POST'])
@owner_required
def loja_online_pedido_status(codigo):
    """Avança o status do pedido manualmente. Dispara e-mail transacional
    quando entra em `a_caminho`."""
    from flask import flash

    from app.models import PedidoOnline
    from app.services import email as email_svc
    p = PedidoOnline.query.filter_by(codigo=codigo).first_or_404()
    novo = (request.form.get('novo_status') or '').strip()
    if novo not in _STATUS_AVANCO:
        flash(f'Status inválido: {novo}', 'danger')
        return redirect(url_for('main.loja_online_pedido_detalhe',
                                codigo=codigo))
    if p.status in ('cancelado', 'entregue') and novo != p.status:
        flash(f'Pedido {p.codigo} já está {p.status} — não muda.', 'warning')
        return redirect(url_for('main.loja_online_pedido_detalhe',
                                codigo=codigo))
    transicionou_para_caminho = (novo == 'a_caminho' and p.status != 'a_caminho')
    transicionou_para_entregue = (novo == 'entregue' and p.status != 'entregue')
    p.status = novo
    db.session.commit()
    if transicionou_para_caminho:
        # E-mail "saiu pra entrega" — best-effort, não derruba o request.
        try:
            if email_svc.disponivel():
                email_svc.enviar_pedido_a_caminho(p)
        except Exception:  # noqa: BLE001
            current_app.logger.exception('email a_caminho falhou')
    if transicionou_para_entregue:
        # E-mail "pedido entregue" — best-effort.
        try:
            if email_svc.disponivel():
                email_svc.enviar_pedido_entregue(p)
        except Exception:  # noqa: BLE001
            current_app.logger.exception('email entregue falhou')
    flash(f'Pedido {p.codigo}: status atualizado para {novo}.', 'success')
    return redirect(url_for('main.loja_online_pedido_detalhe', codigo=codigo))


# ── Loja Online — Fase 5: mapeamento de SKU do Tiny (NF-e) ────────────────
# Liga cada item publicado no site ao SKU dele no Tiny. Pré-requisito da
# emissão de NF (o Tiny aplica o fiscal do cadastro do produto; nós só
# mandamos SKU + quantidade + valor).

@main_bp.route('/admin/loja-online/tiny-skus')
@owner_required
def loja_online_tiny_skus():
    from app.services import tiny_nf
    itens = tiny_nf.itens_para_mapear()
    pendentes = sum(1 for i in itens if i['estado'] != 'mapeado')
    return render_template('admin/loja_online_tiny.html',
                           itens=itens, pendentes=pendentes,
                           total=len(itens))


@main_bp.route('/admin/loja-online/tiny-skus/sync', methods=['POST'])
@owner_required
def loja_online_tiny_sync():
    """Busca o catálogo do Tiny e sugere SKUs por nome pros não mapeados."""
    from flask import flash

    from app.services import tiny_nf
    res = tiny_nf.sincronizar_sugestoes(user_id=current_user.id)
    if res.get('erro'):
        flash(f'Sincronização falhou: {res["erro"]}', 'danger')
    else:
        flash(f'{res.get("exatos", 0)} confirmados (nome idêntico) + '
              f'{res.get("sugeridos", 0)} sugeridos pra conferir, '
              f'{res.get("sem_match", 0)} sem correspondência '
              f'({res.get("total_tiny", 0)} produtos no Tiny).', 'success')
    return redirect(url_for('main.loja_online_tiny_skus'))


@main_bp.route('/admin/loja-online/tiny-skus/importar', methods=['POST'])
@owner_required
def loja_online_tiny_importar():
    """Importa o export de produtos do Tiny (.xls/.csv) e mapeia SKUs por
    nome. Nome idêntico confirma automático; parecido vira sugestão."""
    from flask import flash

    from app.services import tiny_nf
    f = request.files.get('planilha')
    if not f or not f.filename:
        flash('Selecione a planilha de produtos do Tiny (.xls ou .csv).',
              'warning')
        return redirect(url_for('main.loja_online_tiny_skus'))
    conteudo = f.read()
    res = tiny_nf.importar_planilha(conteudo, f.filename,
                                    user_id=current_user.id)
    if res.get('erro'):
        flash(res['erro'], 'danger')
    else:
        flash(f'Planilha importada: {res.get("exatos", 0)} confirmados '
              f'(nome idêntico) + {res.get("sugeridos", 0)} sugeridos pra '
              f'conferir, {res.get("sem_match", 0)} sem correspondência '
              f'({res.get("total", 0)} linhas).', 'success')
    return redirect(url_for('main.loja_online_tiny_skus'))


@main_bp.route('/admin/loja-online/pedidos/<codigo>/emitir-nf', methods=['POST'])
@owner_required
def loja_online_emitir_nf(codigo):
    """Botão manual de emissão de NF via Tiny (Fase 5 plano A).

    `recriar=1`: descarta a NF rascunho anterior (que a SEFAZ rejeitou) e
    refaz o pedido+nota do zero no Tiny com o payload atual."""
    from flask import flash

    from app.models import PedidoOnline
    from app.services import tiny_nf
    p = PedidoOnline.query.filter_by(codigo=codigo).first_or_404()
    recriar = request.form.get('recriar') in ('1', 'true', 'on')
    res = tiny_nf.emitir_nf(p, user_id=current_user.id, recriar=recriar)
    flash(f'{p.codigo}: {res["msg"]}', 'success' if res.get('ok') else 'danger')
    return redirect(url_for('main.loja_online_pedido_detalhe', codigo=codigo))


@main_bp.route('/admin/loja-online/pedidos/<codigo>/danfe')
@owner_required
def loja_online_danfe(codigo):
    """Redireciona pro DANFE (PDF) da NF no Tiny. Link temporário — busca sob
    demanda (não a cada abertura do pedido)."""
    from flask import flash

    from app.models import PedidoOnline
    from app.services import tiny_nf
    p = PedidoOnline.query.filter_by(codigo=codigo).first_or_404()
    url = tiny_nf.link_danfe(p)
    if not url:
        flash(f'{p.codigo}: não consegui obter o link do DANFE no Tiny '
              '(a NF precisa estar autorizada).', 'warning')
        return redirect(url_for('main.loja_online_pedido_detalhe', codigo=codigo))
    return redirect(url)


@main_bp.route('/admin/loja-online/tiny-skus/definir', methods=['POST'])
@owner_required
def loja_online_tiny_definir():
    """Define/limpa o SKU de um item (kind + item_id + sku)."""
    from flask import flash

    from app.services import tiny_nf
    kind = (request.form.get('kind') or '').strip()
    try:
        item_id = int(request.form.get('item_id'))
    except (TypeError, ValueError):
        flash('Item inválido.', 'warning')
        return redirect(url_for('main.loja_online_tiny_skus'))
    sku = (request.form.get('sku') or '').strip()
    tiny_nf.definir_sku(kind, item_id, sku, user_id=current_user.id)
    flash('SKU salvo.' if sku else 'SKU removido.', 'success')
    return redirect(url_for('main.loja_online_tiny_skus'))


# ── Debug VNDA: o que campo a Loja usa pra marcar RETIRADA? (16/06/2026) ──
#
# Bug do dono: "pedidos de retirada nao aparecem em lugar nenhum". Causa
# provavel: `_normalizar_pedido` (vnda.py:344) nao tem deteccao de retirada,
# entao pedidos de pickup chegam misturados como entrega normal mas sem
# endereco — e o painel filtra silenciosamente.
#
# Pra eu fazer o fix sem chutar o nome do shipping_method, esta rota expoe
# o JSON BRUTO de um pedido especifico (owner abre, encontra um pedido de
# retirada que ele conhece, me passa o que aparece em shipping_method_code/
# shipping_method/shipping_label). Owner-only, read-only, nao muta nada.

@main_bp.route('/admin/debug-vnda-pedido/<code>')
@owner_required
def debug_vnda_pedido(code):
    """Mostra o JSON cru de um pedido VNDA + os campos de shipping_method
    em destaque. Owner-only, read-only."""
    from app.services import vnda
    pedido = vnda.buscar_pedido_completo(code)
    if not pedido:
        return jsonify(ok=False, erro=f'pedido {code!r} nao encontrado no VNDA'), 404
    shipping_keys = [
        'shipping_method', 'shipping_method_code', 'shipping_method_name',
        'shipping_name', 'shipping_label', 'delivery_type',
    ]
    destaque = {k: pedido.get(k) for k in shipping_keys}
    extra = pedido.get('extra') or {}
    return jsonify(
        ok=True, code=code,
        shipping_destaque=destaque,
        extra=extra,
        pedido_completo=pedido,
    )


# ── Debug email (Postmark): testa envio sem expor o token (17/06/2026) ────
@main_bp.route('/admin/debug-email', methods=['GET', 'POST'])
@owner_required
def debug_email():
    """Diagnóstico do Postmark (owner-only). GET mostra status da config;
    GET com ?para=<email>&enviar=1 (ou POST) manda um email de teste. NAO
    expoe o server token."""
    from app.services import email as email_svc
    cfg = current_app.config
    status = {
        'postmark_configurado': email_svc.disponivel(),
        'remetente': cfg.get('EMAIL_REMETENTE'),
        'remetente_nome': cfg.get('EMAIL_REMETENTE_NOME'),
        'app_base_url': cfg.get('APP_BASE_URL'),
        'token_len': len((cfg.get('POSTMARK_SERVER_TOKEN') or '')),
    }
    # GET com ?para=<email>&enviar=1 dispara o envio (mais fácil de testar
    # do navegador). POST com ?para=<email> mantém compat programático.
    para = (request.args.get('para') or request.form.get('para') or '').strip()
    deve_enviar = request.method == 'POST' or request.args.get('enviar') == '1'
    if deve_enviar:
        if not para:
            return jsonify(ok=False, erro='passe ?para=<email>&enviar=1',
                           status=status), 400
        res = email_svc.enviar(
            para, 'Teste de email — O Pão',
            '<p>Funcionou! Este é um email de teste do sistema da padaria.</p>',
            texto='Funcionou! Email de teste do sistema da padaria.')
        return jsonify(ok=res.get('ok'), resultado=res, status=status)
    return jsonify(
        ok=True, status=status,
        dica='Abra /admin/debug-email?para=seu@email.com&enviar=1 pra testar')
