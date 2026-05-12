import json
from datetime import date

from flask import redirect, url_for, jsonify, request, Response, render_template
from flask_login import login_required, current_user

from app.blueprints.main import main_bp
from app.decorators import admin_required
from app.extensions import db
from app.models import (MateriaPrima, Receita, ReceitaIngrediente, Produto, ProdutoItem,
                        Funcionario, Atribuicao, AlertaEstoque, PlanejamentoProducao,
                        AuditLog, Usuario)
from app.services.custos import calcular_custos_receitas, calcular_rendimento


@main_bp.route('/')
@login_required
def index():
    if not current_user.is_admin():
        return redirect(url_for('auth.minhas_fichas'))

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
    receitas = Receita.query.order_by(Receita.categoria, Receita.nome).all()
    produtos = Produto.query.filter_by(ativo=True).order_by(Produto.categoria, Produto.nome).all()

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
        categorias[cat].append({
            'nome': r.nome,
            'peso_unitario': r.peso_unitario,
            'descricao': None,
            'preco_venda': preco,
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
        categorias[cat].append({
            'nome': p.nome,
            'peso_unitario': None,
            'descricao': p.descricao,
            'preco_venda': preco,
        })

    return render_template('main/cardapio.html', categorias=categorias, tipo=tipo)


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

