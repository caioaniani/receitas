import json
from datetime import datetime, timedelta

from flask import Response, jsonify, redirect, render_template, request, url_for
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
    return render_template("main/audit.html", rows=rows, tabelas=sorted(tabelas),
                           usuarios=usuarios, filtros={"tabela": tabela_f,
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

    from sqlalchemy import inspect, text

    from app.services import seru_cron

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
