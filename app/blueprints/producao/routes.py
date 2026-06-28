from datetime import datetime

from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.blueprints.producao import producao_bp
from app.decorators import admin_required, producao_required
from app.extensions import db
from app.models import (
    MateriaPrima,
    MovimentacaoEstoque,
    PlanejamentoItem,
    PlanejamentoProducao,
    Receita,
)
from app.services.producao import (
    consolidar_lista_compras,
    fornadas_amassadeira,
)
from app.utils import hoje as hoje_brt


def _inicio_offset():
    """Offset (em dias) do INICIO do horizonte de planejamento. Default 1 =
    amanha (a producao de hoje ja esta decidida quando se olha o painel). 0 =
    hoje. Le de args (GET) ou form (POST). Limite defensivo 0..14."""
    try:
        v = int(request.values.get('inicio', 1))
    except (TypeError, ValueError):
        v = 1
    return max(0, min(v, 14))


@producao_bp.route('/')
@login_required
@producao_required
def lista():
    planos = PlanejamentoProducao.query.order_by(
        PlanejamentoProducao.data.desc()
    ).limit(50).all()
    return render_template('producao/lista.html', planos=planos)


@producao_bp.route('/novo', methods=['GET', 'POST'])
@login_required
@producao_required
def novo():
    if request.method == 'POST':
        data_str = request.form.get('data', '')
        nome = request.form.get('nome', '').strip()
        receita_ids = request.form.getlist('receita_id[]')
        multiplicadores = request.form.getlist('multiplicador[]')

        try:
            data_plan = datetime.strptime(data_str, '%Y-%m-%d').date()
        except ValueError:
            data_plan = hoje_brt()

        plano = PlanejamentoProducao(
            data=data_plan,
            nome=nome or f'Produção {data_plan.strftime("%d/%m")}',
            criado_por=current_user.id,
        )
        db.session.add(plano)
        db.session.flush()

        for i, rid in enumerate(receita_ids):
            if not rid:
                continue
            mult = int(multiplicadores[i]) if i < len(multiplicadores) and multiplicadores[i] else 1
            item = PlanejamentoItem(
                planejamento_id=plano.id,
                receita_id=int(rid),
                multiplicador=max(1, mult),
            )
            db.session.add(item)

        db.session.commit()
        flash('Plano de produção criado!', 'success')
        return redirect(url_for('producao.detalhe', id=plano.id))

    receitas = Receita.query.order_by(Receita.categoria, Receita.nome).all()
    return render_template('producao/novo.html', receitas=receitas, hoje=hoje_brt())


@producao_bp.route('/<int:id>')
@login_required
@producao_required
def detalhe(id):
    plano = PlanejamentoProducao.query.get_or_404(id)
    itens = [{'receita_id': i.receita_id, 'multiplicador': i.multiplicador}
             for i in plano.itens]
    lista_compras = consolidar_lista_compras(itens)
    lista_ordenada = sorted(lista_compras.items(), key=lambda x: x[0])
    custo_total = sum(v['custo_estimado'] for v in lista_compras.values())

    # Enriquece cada item com FORNADAS (batidas da amassadeira) e unidades.
    # Receita que nao usa amassadeira (capacidade 0) mostra unidades, sem fornada.
    itens_view = []
    for i in plano.itens:
        rec = i.receita
        unidades = int(i.multiplicador * (rec.rendimento_qtd or 0)) if rec else 0
        itens_view.append({
            'item': i,
            'fornadas': fornadas_amassadeira(rec, i.multiplicador),
            'unidades': unidades,
        })

    return render_template('producao/detalhe.html', plano=plano,
                           itens_view=itens_view, lista_compras=lista_ordenada,
                           custo_total=custo_total)


@producao_bp.route('/<int:id>/lista-compras')
@login_required
@producao_required
def lista_compras(id):
    """Ordem de compra de MP do plano, agrupada por fornecedor, com o que ha
    A COMPRAR (deficit) destacado. Print-friendly."""
    from app.services.producao import ordem_compra_consolidada

    plano = PlanejamentoProducao.query.get_or_404(id)
    itens = [{'receita_id': i.receita_id, 'multiplicador': i.multiplicador}
             for i in plano.itens]
    ordem = ordem_compra_consolidada(itens)
    return render_template('producao/lista_compras.html', plano=plano,
                           ordem=ordem)


@producao_bp.route('/<int:id>/baixar-estoque', methods=['POST'])
@login_required
@producao_required
def baixar_estoque(id):
    plano = PlanejamentoProducao.query.get_or_404(id)
    itens = [{'receita_id': i.receita_id, 'multiplicador': i.multiplicador} for i in plano.itens]
    lista = consolidar_lista_compras(itens)
    mps = {mp.nome: mp for mp in MateriaPrima.query.all()}

    for nome, dados in lista.items():
        mp = mps.get(nome)
        if not mp:
            continue
        mov = MovimentacaoEstoque(
            materia_prima_id=mp.id,
            tipo='saida',
            quantidade=dados['quantidade'],
            referencia=f'Produção: {plano.nome}',
            usuario_id=current_user.id,
        )
        db.session.add(mov)
        mp.estoque_atual = max(0, (mp.estoque_atual or 0) - dados['quantidade'])

    plano.status = 'executado'
    db.session.commit()
    flash('Estoque baixado com base no plano de produção.', 'success')
    return redirect(url_for('producao.detalhe', id=id))


@producao_bp.route('/<int:id>/excluir', methods=['POST'])
@login_required
@producao_required
def excluir(id):
    plano = PlanejamentoProducao.query.get_or_404(id)
    db.session.delete(plano)
    db.session.commit()
    flash('Plano excluído.', 'success')
    return redirect(url_for('producao.lista'))


@producao_bp.route('/painel')
@login_required
@admin_required
def painel():
    """Balanco de producao da industria — por receita: estoque x comprometido
    x previsto (historico de PedidoLoja) -> quanto produzir. **Admin-only**:
    admin ve os dados e decide o que mandar a producao executar."""
    from datetime import timedelta

    from app.constants import STATUS_PEDIDO_FINALIZADOS
    from app.models import PedidoLoja
    from app.services.cestas import contar_produto_itens_orfaos
    from app.services.previsao_producao import balanco_industria

    hoje_d = hoje_brt()
    amanha = hoje_d + timedelta(days=1)

    try:
        horizonte = int(request.args.get('horizonte', 7))
    except ValueError:
        horizonte = 7
    horizonte = max(1, min(horizonte, 14))

    try:
        janela = int(request.args.get('janela', 6))
    except ValueError:
        janela = 6
    janela = max(1, min(janela, 26))

    inicio = _inicio_offset()
    inicio_data = hoje_d + timedelta(days=inicio)

    # Zona 1 — alertas
    pedidos_atrasados = (PedidoLoja.query
                         .filter(PedidoLoja.data_entrega < hoje_d,
                                 ~PedidoLoja.status.in_(STATUS_PEDIDO_FINALIZADOS))
                         .order_by(PedidoLoja.data_entrega).all())
    cestas_orfaos = contar_produto_itens_orfaos()

    # Zona 2 — balanco da industria (estoque x comprometido x previsto)
    balanco = balanco_industria(horizonte_dias=horizonte, janela_semanas=janela,
                                inicio_offset_dias=inicio)

    # Zona 3 — saindo hoje
    saindo_hoje = (PedidoLoja.query
                   .filter(PedidoLoja.data_entrega == hoje_d,
                           ~PedidoLoja.status.in_(STATUS_PEDIDO_FINALIZADOS))
                   .order_by(PedidoLoja.loja_id, PedidoLoja.id).all())

    return render_template('producao/painel.html',
                           hoje=hoje_d,
                           amanha=amanha,
                           horizonte=horizonte,
                           janela=janela,
                           inicio=inicio,
                           inicio_data=inicio_data,
                           pedidos_atrasados=pedidos_atrasados,
                           cestas_orfaos=cestas_orfaos,
                           balanco=balanco,
                           saindo_hoje=saindo_hoje)


@producao_bp.route('/painel/receita/<int:rid>')
@login_required
@admin_required
def painel_receita_grade(rid):
    """Grade loja x dia de UMA receita: quanto cada loja recebe em cada dia do
    horizonte. Detalha o que o balanco resume — firme (pedidos reais com data)
    + estimado (projecao do previsto rateada por loja/dia). Aberta a partir do
    balanco da producao. **Admin-only** (mesma sensibilidade do painel)."""
    from app.services.previsao_producao import grade_loja_dia

    try:
        horizonte = int(request.args.get('horizonte', 7))
    except ValueError:
        horizonte = 7
    horizonte = max(1, min(horizonte, 14))

    try:
        janela = int(request.args.get('janela', 6))
    except ValueError:
        janela = 6
    janela = max(1, min(janela, 26))

    inicio = _inicio_offset()

    # Fragmento (drop-down inline do balanco) vs pagina inteira (acesso direto).
    partial = (bool(request.args.get('partial'))
               or request.headers.get('X-Requested-With') == 'XMLHttpRequest')

    grade = grade_loja_dia(rid, horizonte_dias=horizonte, janela_semanas=janela,
                           inicio_offset_dias=inicio)
    if grade is None:
        if partial:
            return ('<div class="text-danger small py-2">'
                    'Receita não encontrada.</div>', 404)
        flash('Receita não encontrada.', 'warning')
        return redirect(url_for('producao.painel', horizonte=horizonte,
                                janela=janela, inicio=inicio))

    tpl = ('producao/_grade_receita_tabela.html' if partial
           else 'producao/grade_receita.html')
    return render_template(tpl, grade=grade, horizonte=horizonte, janela=janela,
                           inicio=inicio)


@producao_bp.route('/pedidos-semana')
@login_required
@admin_required
def pedidos_semana():
    """Sugestao de pedidos da semana por loja/dia a partir do historico — a
    inversao do fluxo: o sistema propoe, a loja nao precisa pedir. Preview
    editavel; ao gerar cria PedidoLoja em rascunho ('pendente'). **Admin**."""
    from app.services.previsao_producao import sugerir_pedidos_semana

    try:
        horizonte = int(request.args.get('horizonte', 7))
    except ValueError:
        horizonte = 7
    horizonte = max(1, min(horizonte, 14))

    try:
        janela = int(request.args.get('janela', 6))
    except ValueError:
        janela = 6
    janela = max(1, min(janela, 26))

    inicio = _inicio_offset()
    sugestao = sugerir_pedidos_semana(horizonte_dias=horizonte,
                                      janela_semanas=janela,
                                      inicio_offset_dias=inicio)
    return render_template('producao/pedidos_semana.html',
                           sugestao=sugestao, horizonte=horizonte,
                           janela=janela, inicio=inicio)


@producao_bp.route('/pedidos-semana/gerar', methods=['POST'])
@login_required
@admin_required
def pedidos_semana_gerar():
    """Cria os pedidos rascunho a partir das quantidades (ajustadas) da tela.
    Campos do form: 'qtd|<loja_id>|<data_iso>|<receita_id>' = quantidade."""
    from datetime import date

    from app.services.pedidos_semana import criar_pedidos_rascunho

    agrupado = {}   # (loja_id, data) -> list[{receita_id, qtd}]
    for chave, valor in request.form.items():
        if not chave.startswith('qtd|'):
            continue
        partes = chave.split('|')
        if len(partes) != 4:
            continue
        _, loja_s, data_s, rid_s = partes
        try:
            loja_id = int(loja_s)
            rid = int(rid_s)
            qtd = int(valor or 0)
            data_ent = date.fromisoformat(data_s)
        except (TypeError, ValueError):
            continue
        if qtd <= 0:
            continue
        agrupado.setdefault((loja_id, data_ent), []).append(
            {'receita_id': rid, 'qtd': qtd})

    pedidos = [{'loja_id': k[0], 'data_entrega': k[1], 'itens': v}
               for k, v in agrupado.items()]
    res = criar_pedidos_rascunho(pedidos, current_user.id)

    if res['criados']:
        msg = (f"{res['criados']} pedido(s) rascunho criado(s) "
               f"({res['itens']} itens). Revise e confirme em Pedidos.")
        if res['pulados_existentes']:
            msg += (f" {res['pulados_existentes']} dia(s) pulado(s) — a loja "
                    "já tinha pedido.")
        flash(msg, 'success')
    else:
        flash('Nenhum pedido criado (sem itens, ou todas as lojas já tinham '
              'pedido nas datas).', 'info')

    return redirect(url_for('producao.pedidos_semana'))


@producao_bp.route('/painel/debug')
@login_required
@admin_required
def painel_debug():
    """Dump dos pedidos que o motor balanco_industria ENXERGA — pra
    auditoria. Mostra Comprometido (proximos N dias) E Historico (M semanas)
    detalhados pedido a pedido, mais a lista de TODAS as Lojas com contagem.
    Crucial: revela se o motor esta perdendo alguma loja ou status.
    """
    from datetime import timedelta

    from app.constants import STATUS_PEDIDO_NAO_BAIXADOS
    from app.models import Loja, MateriaPrima, PedidoItem, PedidoLoja, Produto

    try:
        horizonte = int(request.args.get('horizonte', 7))
    except ValueError:
        horizonte = 7
    horizonte = max(1, min(horizonte, 14))
    try:
        janela = int(request.args.get('janela', 6))
    except ValueError:
        janela = 6
    janela = max(1, min(janela, 26))

    hoje_d = hoje_brt()
    horizonte_fim = hoje_d + timedelta(days=horizonte - 1)
    hist_ini = hoje_d - timedelta(days=7 * janela)
    hist_fim = hoje_d - timedelta(days=1)

    nomes_loja = {l.id: l.nome for l in Loja.query.all()}

    # Pedidos QUE ENTRAM no Comprometido — mesma query do motor + dados extra.
    comprometido_pedidos = (db.session.query(PedidoLoja.id, PedidoLoja.loja_id,
                                              PedidoLoja.status,
                                              PedidoLoja.data_entrega,
                                              PedidoItem.receita_id,
                                              PedidoItem.quantidade,
                                              Receita.nome)
                            .join(PedidoItem, PedidoItem.pedido_id == PedidoLoja.id)
                            .join(Receita, Receita.id == PedidoItem.receita_id)
                            .filter(PedidoItem.receita_id.isnot(None),
                                    PedidoLoja.status.in_(STATUS_PEDIDO_NAO_BAIXADOS),
                                    PedidoLoja.data_entrega >= hoje_d,
                                    PedidoLoja.data_entrega <= horizonte_fim)
                            .order_by(PedidoLoja.data_entrega, PedidoLoja.loja_id,
                                      Receita.nome)
                            .all())

    # Pedidos QUE NAO ENTRAM no Comprometido mas estao no horizonte —
    # exclusao por status (em_transporte/entregue/recebido/cancelado).
    # Crucial pra diagnosticar quando uma loja "some" do balanco.
    excluidos_status = (db.session.query(PedidoLoja.id, PedidoLoja.loja_id,
                                          PedidoLoja.status,
                                          PedidoLoja.data_entrega,
                                          PedidoItem.receita_id,
                                          PedidoItem.quantidade,
                                          Receita.nome)
                        .join(PedidoItem, PedidoItem.pedido_id == PedidoLoja.id)
                        .join(Receita, Receita.id == PedidoItem.receita_id)
                        .filter(PedidoItem.receita_id.isnot(None),
                                ~PedidoLoja.status.in_(STATUS_PEDIDO_NAO_BAIXADOS),
                                PedidoLoja.data_entrega >= hoje_d,
                                PedidoLoja.data_entrega <= horizonte_fim)
                        .order_by(PedidoLoja.data_entrega, PedidoLoja.loja_id)
                        .all())

    # Pedidos no horizonte mas com data_entrega ANTERIOR a hoje (atrasados).
    atrasados = (db.session.query(PedidoLoja.id, PedidoLoja.loja_id,
                                   PedidoLoja.status, PedidoLoja.data_entrega,
                                   PedidoItem.receita_id, PedidoItem.quantidade,
                                   Receita.nome)
                 .join(PedidoItem, PedidoItem.pedido_id == PedidoLoja.id)
                 .join(Receita, Receita.id == PedidoItem.receita_id)
                 .filter(PedidoItem.receita_id.isnot(None),
                         PedidoLoja.status.in_(STATUS_PEDIDO_NAO_BAIXADOS),
                         PedidoLoja.data_entrega < hoje_d)
                 .order_by(PedidoLoja.data_entrega.desc())
                 .limit(50).all())

    # Por loja: contagem de pedidos comprometidos, excluidos por status,
    # e historico. Lista TODAS as Lojas (incluindo inativas), pra revelar
    # alguma loja "fantasma" ou diferenca de nome.
    contagem_por_loja = {l.id: {'nome': l.nome, 'ativa': l.ativa,
                                 'comprometido': 0, 'excluido': 0,
                                 'historico': 0}
                          for l in Loja.query.order_by(Loja.nome).all()}
    for r in comprometido_pedidos:
        if r.loja_id in contagem_por_loja:
            contagem_por_loja[r.loja_id]['comprometido'] += 1
    for r in excluidos_status:
        if r.loja_id in contagem_por_loja:
            contagem_por_loja[r.loja_id]['excluido'] += 1

    historico_count_por_loja = dict(
        db.session.query(PedidoLoja.loja_id, db.func.count(PedidoLoja.id.distinct()))
        .join(PedidoItem, PedidoItem.pedido_id == PedidoLoja.id)
        .filter(PedidoItem.receita_id.isnot(None),
                PedidoLoja.status != 'cancelado',
                PedidoLoja.data_entrega >= hist_ini,
                PedidoLoja.data_entrega <= hist_fim)
        .group_by(PedidoLoja.loja_id).all())
    for lid, cnt in historico_count_por_loja.items():
        if lid in contagem_por_loja:
            contagem_por_loja[lid]['historico'] = cnt

    # SECAO "CACA O FANTASMA" — todos os PedidoItem do horizonte,
    # independente da FK. Cobre 3 hipoteses do dono (24/06/2026):
    # 1) item gravado com produto_id em vez de receita_id (motor filtra fora)
    # 2) item gravado com materia_prima_id (idem)
    # 3) item ORFAO (3 FKs NULL) — so item_nome textual
    # Pra cada linha, mostra: loja, pedido, status, data_entrega, item_nome,
    # qual FK esta setada (REC/PROD/MP/NENHUMA), nome do alvo da FK.
    # Tambem agrupa por busca textual "Pão Francês" (case-insensitive) pra
    # achar variantes.
    todos_itens_horizonte = (db.session.query(PedidoLoja.id, PedidoLoja.loja_id,
                                               PedidoLoja.status,
                                               PedidoLoja.data_entrega,
                                               PedidoItem.id.label('item_id'),
                                               PedidoItem.receita_id,
                                               PedidoItem.produto_id,
                                               PedidoItem.materia_prima_id,
                                               PedidoItem.quantidade,
                                               PedidoItem.observacao)
                              .join(PedidoItem, PedidoItem.pedido_id == PedidoLoja.id)
                              .filter(PedidoLoja.data_entrega >= hoje_d,
                                      PedidoLoja.data_entrega <= horizonte_fim)
                              .order_by(PedidoLoja.loja_id,
                                        PedidoLoja.data_entrega).all())

    # Resolve nomes das 3 FKs em lote.
    rec_ids = {r.receita_id for r in todos_itens_horizonte if r.receita_id}
    prod_ids = {r.produto_id for r in todos_itens_horizonte if r.produto_id}
    mp_ids = {r.materia_prima_id for r in todos_itens_horizonte
              if r.materia_prima_id}
    nomes_rec = {r.id: r.nome for r in
                 Receita.query.filter(Receita.id.in_(rec_ids)).all()} if rec_ids else {}
    nomes_prod = {p.id: p.nome for p in
                  Produto.query.filter(Produto.id.in_(prod_ids)).all()} if prod_ids else {}
    nomes_mp = {m.id: m.nome for m in
                MateriaPrima.query.filter(MateriaPrima.id.in_(mp_ids)).all()} if mp_ids else {}

    # Enriquece cada linha + identifica fantasmas (nao-receita).
    itens_enriquecidos = []
    fantasmas = []  # itens que nao entram no balanco mas tem nome reconhecivel
    for r in todos_itens_horizonte:
        if r.receita_id:
            fk = 'REC'
            alvo = nomes_rec.get(r.receita_id, f'?id={r.receita_id}')
        elif r.produto_id:
            fk = 'PROD'
            alvo = nomes_prod.get(r.produto_id, f'?id={r.produto_id}')
        elif r.materia_prima_id:
            fk = 'MP'
            alvo = nomes_mp.get(r.materia_prima_id, f'?id={r.materia_prima_id}')
        else:
            fk = 'NENHUMA'
            alvo = (r.observacao or '<sem FK e sem observacao>')
        entrada = {
            'pedido_id': r.id, 'loja_id': r.loja_id, 'status': r.status,
            'data_entrega': r.data_entrega, 'item_id': r.item_id,
            'fk_tipo': fk, 'alvo_nome': alvo,
            'receita_id': r.receita_id, 'produto_id': r.produto_id,
            'materia_prima_id': r.materia_prima_id,
            'quantidade': r.quantidade, 'observacao': r.observacao,
        }
        itens_enriquecidos.append(entrada)
        # Fantasma = nao entra no balanco (motor filtra receita_id NOT NULL)
        if fk != 'REC' and r.status in STATUS_PEDIDO_NAO_BAIXADOS:
            fantasmas.append(entrada)

    # Variantes de receita por nome — agrupar por "primeira palavra" pra
    # detectar receitas duplicadas (ex: "Pão Francês" vs "Pão Francês Fermentado").
    variantes_receita = {}
    for rec in Receita.query.order_by(Receita.nome).all():
        chave = (rec.nome.split()[0] if rec.nome else '?').lower()
        variantes_receita.setdefault(chave, []).append(
            {'id': rec.id, 'nome': rec.nome,
             'arquivada': rec.arquivada_em is not None})
    # So mostra grupos com 2+ variantes.
    variantes_receita = {k: v for k, v in variantes_receita.items()
                          if len(v) >= 2}

    # BUSCA POR ITEM — responde "quais lojas pediram X" pra qualquer termo.
    # Cobre os 3 tipos de FK (receita/produto/mp), pra revelar inclusive se um
    # item foi cadastrado como produto/MP em alguma loja (e por isso some do
    # balanco de producao). Janela: 60 dias atras ate o fim do horizonte.
    busca = (request.args.get('q') or '').strip()
    busca_linhas = []
    busca_resumo = []
    if busca:
        like = f'%{busca}%'
        janela_busca_ini = hoje_d - timedelta(days=60)
        q = (db.session.query(PedidoLoja.id, PedidoLoja.loja_id,
                              PedidoLoja.status, PedidoLoja.data_entrega,
                              PedidoItem.receita_id, PedidoItem.produto_id,
                              PedidoItem.materia_prima_id,
                              PedidoItem.quantidade,
                              Receita.nome.label('rec_nome'),
                              Produto.nome.label('prod_nome'),
                              MateriaPrima.nome.label('mp_nome'))
             .join(PedidoItem, PedidoItem.pedido_id == PedidoLoja.id)
             .outerjoin(Receita, Receita.id == PedidoItem.receita_id)
             .outerjoin(Produto, Produto.id == PedidoItem.produto_id)
             .outerjoin(MateriaPrima,
                        MateriaPrima.id == PedidoItem.materia_prima_id)
             .filter(PedidoLoja.data_entrega >= janela_busca_ini,
                     PedidoLoja.data_entrega <= horizonte_fim,
                     db.or_(Receita.nome.ilike(like),
                            Produto.nome.ilike(like),
                            MateriaPrima.nome.ilike(like)))
             .order_by(PedidoLoja.data_entrega.desc(), PedidoLoja.loja_id)
             .limit(300).all())
        # Agrega por loja pra um resumo no topo (responde direto a pergunta).
        resumo_por_loja = {}
        for r in q:
            if r.receita_id:
                fk, nome = 'REC', r.rec_nome
            elif r.produto_id:
                fk, nome = 'PROD', r.prod_nome
            elif r.materia_prima_id:
                fk, nome = 'MP', r.mp_nome
            else:
                fk, nome = 'NENHUMA', '?'
            no_horizonte = r.data_entrega and r.data_entrega >= hoje_d
            busca_linhas.append({
                'pedido_id': r.id, 'loja_nome': nomes_loja.get(r.loja_id, '?'),
                'status': r.status, 'data_entrega': r.data_entrega,
                'item_nome': nome, 'fk_tipo': fk, 'quantidade': r.quantidade,
                'no_horizonte': no_horizonte,
                'entra_balanco': (fk == 'REC' and no_horizonte
                                  and r.status in STATUS_PEDIDO_NAO_BAIXADOS),
            })
            chave = nomes_loja.get(r.loja_id, '?')
            ag = resumo_por_loja.setdefault(
                chave, {'loja_nome': chave, 'qtd_total': 0, 'n_linhas': 0,
                        'qtd_horizonte': 0})
            ag['qtd_total'] += int(r.quantidade or 0)
            ag['n_linhas'] += 1
            if no_horizonte:
                ag['qtd_horizonte'] += int(r.quantidade or 0)
        busca_resumo = sorted(resumo_por_loja.values(),
                              key=lambda x: -x['qtd_total'])

    return render_template(
        'producao/painel_debug.html',
        hoje=hoje_d, horizonte=horizonte, janela=janela,
        horizonte_fim=horizonte_fim, hist_ini=hist_ini, hist_fim=hist_fim,
        comprometido_pedidos=comprometido_pedidos,
        excluidos_status=excluidos_status,
        atrasados=atrasados,
        contagem_por_loja=contagem_por_loja,
        nomes_loja=nomes_loja,
        status_nao_baixados=STATUS_PEDIDO_NAO_BAIXADOS,
        itens_enriquecidos=itens_enriquecidos,
        fantasmas=fantasmas,
        variantes_receita=variantes_receita,
        busca=busca,
        busca_linhas=busca_linhas,
        busca_resumo=busca_resumo,
    )


@producao_bp.route('/painel/criar-plano-do-deficit', methods=['POST'])
@login_required
@admin_required
def criar_plano_do_deficit():
    """Cria um PlanejamentoProducao do dia ja preenchido com as receitas que
    o balanco aponta como deficit (coluna Produzir > 0). Multiplicador por
    receita = ceil(produzir / rendimento_qtd). O admin abre o plano criado,
    revisa, ajusta a olho e clica em 'Baixar estoque' como ja faz hoje."""
    from math import ceil

    from app.services.previsao_producao import balanco_industria

    try:
        horizonte = int(request.form.get('horizonte', 7))
    except ValueError:
        horizonte = 7
    horizonte = max(1, min(horizonte, 14))

    try:
        janela = int(request.form.get('janela', 6))
    except ValueError:
        janela = 6
    janela = max(1, min(janela, 26))

    inicio = _inicio_offset()
    balanco = balanco_industria(horizonte_dias=horizonte,
                                janela_semanas=janela, usar_cache=False,
                                inicio_offset_dias=inicio)
    deficits = [it for it in balanco['itens'] if it['produzir'] > 0]
    if not deficits:
        flash('Sem deficit no horizonte — nada a planejar.', 'info')
        return redirect(url_for('producao.painel',
                                horizonte=horizonte, janela=janela,
                                inicio=inicio))

    hoje_d = hoje_brt()
    plano = PlanejamentoProducao(
        data=hoje_d,
        nome=f'Producao {hoje_d.strftime("%d/%m")} (deficit {horizonte}d)',
        criado_por=current_user.id,
    )
    db.session.add(plano)
    db.session.flush()

    ignorados = []
    for it in deficits:
        rec = Receita.query.get(it['receita_id'])
        if not rec or not rec.rendimento_qtd or rec.rendimento_qtd <= 0:
            # Receita sem rendimento definido — nao da pra calcular mult.
            # Reporta na flash pro admin completar a ficha tecnica.
            ignorados.append(it['nome'])
            continue
        mult = max(1, ceil(it['produzir'] / float(rec.rendimento_qtd)))
        db.session.add(PlanejamentoItem(
            planejamento_id=plano.id, receita_id=rec.id, multiplicador=mult))

    db.session.commit()
    msg = f'Plano criado com {len(deficits) - len(ignorados)} receita(s).'
    if ignorados:
        msg += (' Sem rendimento na ficha (ignorados): '
                + ', '.join(ignorados[:5])
                + ('...' if len(ignorados) > 5 else ''))
    flash(msg, 'success' if not ignorados else 'warning')
    return redirect(url_for('producao.detalhe', id=plano.id))
