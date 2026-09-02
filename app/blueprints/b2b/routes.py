"""B2B — venda da industria pra clientes externos.

Rotas:
- /b2b — dashboard (vendas recentes + contas a receber)
- /b2b/clientes — CRUD cliente B2B
- /b2b/precos — tabela de preco atacado
- /b2b/vendas/nova — formulario nova venda
- /b2b/vendas/<id> — detalhe + receber pagamento + cancelar
- /b2b/contas-a-receber — parcelas em aberto

Baixa do estoque ocorre em vendas_b2b.criar_venda (service).
Cancelamento estorna automaticamente.
"""
from datetime import date

from flask import flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy.orm import joinedload

from app.blueprints.b2b import b2b_bp
from app.decorators import admin_required, owner_required
from app.extensions import db
from app.models import (
    ClienteB2B,
    EstoqueProducao,
    FaturaB2B,
    PrecoClienteB2B,
    Produto,
    Receita,
    VendaB2B,
    VendaB2BItem,
    VendaB2BParcela,
)
from app.services import tiny_nf_b2b
from app.services import vendas_b2b as svc
from app.utils import hoje

# ── Dashboard ──
# Reorganizado em ABAS (pedido do dono 07/07/2026): PEDIDOS (ciclo
# orçamento → produção → entrega) separado de COBRANÇAS (financeiro,
# com filtro por data). Esboço dele mapeado nas entidades existentes:
#   Pendentes   = orçamentos rascunho/enviado (aguardando o cliente)
#   Aprovados   = orçamentos aprovados (prontos pra virar venda)
#   Em produção = vendas ativas ainda não entregues (fila do padeiro)
#   Entregues   = vendas ativas com entrega concluída
#   Arquivados  = orçamentos recusados + vendas canceladas
#   Cobranças   = parcelas: pendentes / vencidas / pagas.

def _dashboard_counts(hoje_, de, ate):
    """Contadores das pills (as listas só carregam pra aba ativa)."""
    from app.models import Orcamento
    parc = (VendaB2BParcela.query.join(
        VendaB2B, VendaB2BParcela.venda_id == VendaB2B.id)
        .filter(VendaB2B.status == 'ativa', VendaB2B.dispensa_cobranca.is_(None)))
    parc_aberta = parc.filter(VendaB2BParcela.pago_em.is_(None))
    parc_paga = parc.filter(VendaB2BParcela.pago_em.isnot(None))
    if de:
        parc_aberta = parc_aberta.filter(VendaB2BParcela.vencimento >= de)
        parc_paga = parc_paga.filter(
            db.func.date(VendaB2BParcela.pago_em) >= de)
    if ate:
        parc_aberta = parc_aberta.filter(VendaB2BParcela.vencimento <= ate)
        parc_paga = parc_paga.filter(
            db.func.date(VendaB2BParcela.pago_em) <= ate)
    return {
        'pendentes': Orcamento.query.filter(
            Orcamento.status.in_(('rascunho', 'enviado')),
            Orcamento.arquivado_em.is_(None)).count(),
        # Aprovado agora VIRA venda na hora (07/07/2026) — aqui ficam so
        # os aprovados AINDA sem venda (legado/excecao).
        'aprovados': Orcamento.query.filter_by(
            status='aprovado', venda_id=None).count(),
        'producao': VendaB2B.query.filter(
            VendaB2B.status == 'ativa',
            VendaB2B.status_entrega != 'entregue').count(),
        'entregues': VendaB2B.query.filter_by(
            status='ativa', status_entrega='entregue').count(),
        'arquivados': (Orcamento.query.filter(db.or_(
                           Orcamento.status == 'recusado',
                           Orcamento.arquivado_em.isnot(None))).count()
                       + VendaB2B.query.filter_by(
                           status='cancelada').count()),
        'cob_pendentes': parc_aberta.filter(
            VendaB2BParcela.vencimento >= hoje_).count(),
        'cob_vencidos': parc_aberta.filter(
            VendaB2BParcela.vencimento < hoje_).count(),
        'cob_pagos': parc_paga.count(),
    }


@b2b_bp.route('/')
@login_required
@admin_required
def dashboard():
    from app.models import Orcamento
    from app.services import orcamentos as orc_svc

    aba = request.args.get('aba') or 'pedidos'
    if aba not in ('pedidos', 'cobrancas'):
        aba = 'pedidos'
    f = request.args.get('f') or ('pendentes' if aba == 'pedidos'
                                  else 'cob_pendentes')
    hoje_ = hoje()

    # Filtro por data (só faz sentido nas cobranças): vencimento nas
    # abertas/vencidas; data do PAGAMENTO nas pagas.
    de = ate = None
    try:
        if request.args.get('de'):
            de = date.fromisoformat(request.args['de'])
        if request.args.get('ate'):
            ate = date.fromisoformat(request.args['ate'])
    except ValueError:
        flash('Data do filtro inválida — ignorada.', 'warning')

    counts = _dashboard_counts(hoje_, de, ate)

    orcamentos = vendas = parcelas = vendas_canceladas = None
    if aba == 'pedidos':
        if f == 'pendentes':
            orcamentos = (Orcamento.query
                          .filter(Orcamento.status.in_(('rascunho',
                                                        'enviado')),
                                  Orcamento.arquivado_em.is_(None))
                          .order_by(Orcamento.criado_em.desc())
                          .limit(200).all())
        elif f == 'aprovados':
            orcamentos = (Orcamento.query
                          .filter_by(status='aprovado', venda_id=None)
                          .order_by(Orcamento.aprovado_em.desc())
                          .limit(200).all())
        elif f == 'entregues':
            vendas = (VendaB2B.query
                      .options(joinedload(VendaB2B.cliente))
                      .filter_by(status='ativa', status_entrega='entregue')
                      .order_by(VendaB2B.data_venda.desc(),
                                VendaB2B.id.desc())
                      .limit(200).all())
        elif f == 'arquivados':
            orcamentos = (Orcamento.query
                          .filter(db.or_(
                              Orcamento.status == 'recusado',
                              Orcamento.arquivado_em.isnot(None)))
                          .order_by(Orcamento.criado_em.desc())
                          .limit(100).all())
            vendas_canceladas = (VendaB2B.query
                                 .options(joinedload(VendaB2B.cliente))
                                 .filter_by(status='cancelada')
                                 .order_by(VendaB2B.id.desc())
                                 .limit(100).all())
        else:
            f = 'producao'
            vendas = (VendaB2B.query
                      .options(joinedload(VendaB2B.cliente))
                      .filter(VendaB2B.status == 'ativa',
                              VendaB2B.status_entrega != 'entregue')
                      .order_by(VendaB2B.data_entrega.asc().nullslast(),
                                VendaB2B.data_venda.asc())
                      .limit(200).all())
    else:
        q = (VendaB2BParcela.query
             .join(VendaB2B, VendaB2BParcela.venda_id == VendaB2B.id)
             .options(joinedload(VendaB2BParcela.venda)
                      .joinedload(VendaB2B.cliente))
             .filter(VendaB2B.status == 'ativa', VendaB2B.dispensa_cobranca.is_(None)))
        if f == 'cob_pagos':
            q = q.filter(VendaB2BParcela.pago_em.isnot(None))
            if de:
                q = q.filter(db.func.date(VendaB2BParcela.pago_em) >= de)
            if ate:
                q = q.filter(db.func.date(VendaB2BParcela.pago_em) <= ate)
            q = q.order_by(VendaB2BParcela.pago_em.desc())
        else:
            if f != 'cob_vencidos':
                f = 'cob_pendentes'
            q = q.filter(VendaB2BParcela.pago_em.is_(None))
            q = (q.filter(VendaB2BParcela.vencimento < hoje_)
                 if f == 'cob_vencidos'
                 else q.filter(VendaB2BParcela.vencimento >= hoje_))
            if de:
                q = q.filter(VendaB2BParcela.vencimento >= de)
            if ate:
                q = q.filter(VendaB2BParcela.vencimento <= ate)
            q = q.order_by(VendaB2BParcela.vencimento.asc())
        parcelas = q.limit(200).all()

    return render_template('b2b/dashboard.html', aba=aba, f=f,
                           counts=counts, orcamentos=orcamentos,
                           vendas=vendas, parcelas=parcelas,
                           vendas_canceladas=vendas_canceladas,
                           de=de, ate=ate, hoje_=hoje_,
                           STATUS_LABEL=orc_svc.STATUS_LABEL)


# ── Clientes ──

@b2b_bp.route('/leads')
@login_required
@admin_required
def leads():
    """Leads de atacado capturados pelo bot de atendimento (16/07/2026;
    fluxo 20/07: o bot registra e TRANSFERE pro atendente — sem catálogo).
    O dono acompanha aqui e marca como contatado."""
    from app.models import LeadB2B
    from app.services.chatbot_vigia import link_chatwoot
    pendentes = request.args.get('todos') != '1'
    q = LeadB2B.query
    if pendentes:
        q = q.filter(LeadB2B.contatado_em.is_(None))
    lds = q.order_by(LeadB2B.criado_em.desc()).limit(300).all()
    n_pendentes = LeadB2B.query.filter(
        LeadB2B.contatado_em.is_(None)).count()
    return render_template(
        'b2b/leads.html', leads=lds, pendentes=pendentes,
        n_pendentes=n_pendentes,
        link_conversa_chatwoot=link_chatwoot)


@b2b_bp.route('/leads/<int:lid>/contatado', methods=['POST'])
@login_required
@admin_required
def lead_contatado(lid):
    """Marca (ou desmarca, ?desfazer=1) o lead como contatado."""
    from app.models import LeadB2B
    from app.utils import agora
    lead = LeadB2B.query.get_or_404(lid)
    if request.form.get('desfazer') == '1':
        lead.contatado_em = None
        lead.contatado_por_id = None
    else:
        lead.contatado_em = agora()
        lead.contatado_por_id = current_user.id
    db.session.commit()
    return redirect(url_for('b2b.leads'))


@b2b_bp.route('/clientes')
@login_required
@admin_required
def clientes():
    """Lista os clientes ATIVOS por padrão; arquivados ficam atrás do
    filtro `?arquivados=1` (pedido do dono 07/07/2026 — inativo na lista
    principal só atrapalha)."""
    arquivados = request.args.get('arquivados') == '1'
    q = ClienteB2B.query.filter_by(ativo=not arquivados)
    cs = q.order_by(ClienteB2B.nome.asc()).all()
    n_arquivados = ClienteB2B.query.filter_by(ativo=False).count()
    return render_template('b2b/clientes.html', clientes=cs,
                           arquivados=arquivados,
                           n_arquivados=n_arquivados)


@b2b_bp.route('/clientes/<int:cid>/arquivar', methods=['POST'])
@login_required
@admin_required
def cliente_arquivar(cid):
    """Alterna ativo/arquivado. Arquivado some da lista, dos selects de
    venda/orçamento e do fechamento mensal — o histórico fica intacto."""
    c = ClienteB2B.query.get_or_404(cid)
    c.ativo = not c.ativo
    db.session.commit()
    if c.ativo:
        flash(f'{c.nome} reativado.', 'success')
        return redirect(url_for('b2b.clientes'))
    flash(f'{c.nome} arquivado — o histórico continua nas vendas/faturas.',
          'success')
    return redirect(url_for('b2b.clientes'))


@b2b_bp.route('/clientes/<int:cid>/excluir', methods=['POST'])
@login_required
@admin_required
def cliente_excluir(cid):
    """Exclui DEFINITIVAMENTE um cliente SEM histórico (cadastro de teste
    ou duplicado). Com venda/orçamento/fatura no nome, recusa — arquive."""
    from app.models import Orcamento
    c = ClienteB2B.query.get_or_404(cid)
    tem = []
    if VendaB2B.query.filter_by(cliente_id=cid).count():
        tem.append('vendas')
    if FaturaB2B.query.filter_by(cliente_id=cid).count():
        tem.append('faturas')
    if Orcamento.query.filter_by(cliente_id=cid).count():
        tem.append('orçamentos')
    if tem:
        flash(f'{c.nome} tem {", ".join(tem)} no histórico — não dá pra '
              'excluir sem perder registro. Use "Arquivar".', 'warning')
        return redirect(url_for('b2b.clientes'))
    db.session.delete(c)        # tabela de preços some junto (cascade)
    db.session.commit()
    flash(f'{c.nome} excluído.', 'success')
    return redirect(url_for('b2b.clientes'))


@b2b_bp.route('/clientes/novo', methods=['POST'])
@login_required
@admin_required
def cliente_novo():
    nome = (request.form.get('nome') or '').strip()
    if not nome:
        flash('Nome obrigatorio.', 'danger')
        return redirect(url_for('b2b.clientes'))
    if ClienteB2B.query.filter_by(nome=nome).first():
        flash(f'Cliente "{nome}" ja existe.', 'warning')
        return redirect(url_for('b2b.clientes'))
    c = ClienteB2B(
        nome=nome,
        cnpj_cpf=(request.form.get('cnpj_cpf') or '').strip() or None,
        telefone=(request.form.get('telefone') or '').strip() or None,
        email=(request.form.get('email') or '').strip() or None,
        endereco=(request.form.get('endereco') or '').strip() or None,
        contato=(request.form.get('contato') or '').strip() or None,
        desconto_percentual=float(request.form.get('desconto_percentual') or 0),
        faturamento_mensal=bool(request.form.get('faturamento_mensal')),
        observacao=(request.form.get('observacao') or '').strip() or None,
    )
    _aplicar_endereco_estruturado(c)
    db.session.add(c)
    db.session.commit()
    flash(f'Cliente "{nome}" cadastrado.', 'success')
    return redirect(url_for('b2b.clientes'))


def _aplicar_endereco_estruturado(c):
    """Lê os campos de endereço estruturado (NF-e) do form pro cliente.
    A SEFAZ exige logradouro/número/bairro/CEP/cidade/UF separados."""
    c.endereco_logradouro = (request.form.get('endereco_logradouro') or '').strip() or None
    c.endereco_numero = (request.form.get('endereco_numero') or '').strip() or None
    c.endereco_complemento = (request.form.get('endereco_complemento') or '').strip() or None
    c.endereco_bairro = (request.form.get('endereco_bairro') or '').strip() or None
    c.endereco_cep = (request.form.get('endereco_cep') or '').strip() or None
    c.endereco_cidade = (request.form.get('endereco_cidade') or '').strip() or None
    c.endereco_uf = ((request.form.get('endereco_uf') or '').strip().upper()
                     or None)


@b2b_bp.route('/clientes/<int:cid>/editar', methods=['POST'])
@login_required
@admin_required
def cliente_editar(cid):
    c = ClienteB2B.query.get_or_404(cid)
    nome = (request.form.get('nome') or '').strip()
    if not nome:
        flash('Nome obrigatorio.', 'danger')
        return redirect(url_for('b2b.clientes'))
    # nome e unique — barra colisao com OUTRO cliente (o proprio pode
    # manter o nome). As vendas/faturas referenciam por FK (cliente_id),
    # entao renomear nao mexe no historico.
    if nome != c.nome and ClienteB2B.query.filter(
            ClienteB2B.nome == nome, ClienteB2B.id != c.id).first():
        flash(f'Ja existe outro cliente chamado "{nome}".', 'warning')
        return redirect(url_for('b2b.clientes'))
    c.nome = nome
    c.cnpj_cpf = (request.form.get('cnpj_cpf') or '').strip() or None
    c.telefone = (request.form.get('telefone') or '').strip() or None
    c.email = (request.form.get('email') or '').strip() or None
    c.endereco = (request.form.get('endereco') or '').strip() or None
    c.contato = (request.form.get('contato') or '').strip() or None
    c.desconto_percentual = float(request.form.get('desconto_percentual') or 0)
    c.faturamento_mensal = bool(request.form.get('faturamento_mensal'))
    c.observacao = (request.form.get('observacao') or '').strip() or None
    c.ativo = bool(request.form.get('ativo'))
    _aplicar_endereco_estruturado(c)
    db.session.commit()
    flash(f'{c.nome} atualizado.', 'success')
    return redirect(url_for('b2b.clientes'))


@b2b_bp.route('/clientes/<int:cid>/precos', methods=['GET', 'POST'])
@login_required
@admin_required
def cliente_precos(cid):
    """Tabela de preço POR CLIENTE (o atacado cobra valores diferentes por
    cliente). Preço específico VENCE o atacado padrão na venda; vazio =
    remove a linha e o cliente volta pro padrão (com o desconto %)."""
    from decimal import Decimal, InvalidOperation

    cliente = ClienteB2B.query.get_or_404(cid)

    if request.method == 'POST':
        alterados = removidos = 0
        for chave, valor in request.form.items():
            if not chave.startswith('preco['):        # preco[receita:5]
                continue
            ref = chave[6:-1]
            kind, _, sid = ref.partition(':')
            if kind not in ('receita', 'produto') or not sid.isdigit():
                continue
            linha = PrecoClienteB2B.query.filter_by(
                cliente_id=cid, kind=kind, item_id=int(sid)).first()
            valor = (valor or '').strip().replace(',', '.')
            if not valor:
                if linha:
                    db.session.delete(linha)
                    removidos += 1
                continue
            # Guard server-side (pos-revisao 19/07/2026): aba velha/POST
            # direto nao salva preco NOVO pra item arquivado/inativo —
            # remover (ramo acima) continua permitido.
            obj = (Receita.query.get(int(sid)) if kind == 'receita'
                   else Produto.query.get(int(sid)))
            morto = (obj is None
                     or (kind == 'receita' and obj.arquivada_em)
                     or (kind == 'produto' and not obj.ativo))
            if morto:
                flash(f'Item de {ref} está arquivado/inativo — preço '
                      'ignorado.', 'warning')
                continue
            try:
                preco = Decimal(valor)
            except InvalidOperation:
                flash(f'Preço inválido em {ref} — ignorado.', 'warning')
                continue
            if preco <= 0:
                flash(f'Preço deve ser maior que zero ({ref}) — ignorado.',
                      'warning')
                continue
            if not linha:
                linha = PrecoClienteB2B(cliente_id=cid, kind=kind,
                                        item_id=int(sid))
                db.session.add(linha)
            if linha.preco != preco:
                linha.preco = preco
                linha.atualizado_por_id = current_user.id
                alterados += 1
        db.session.commit()
        flash(f'Tabela de {cliente.nome}: {alterados} preço(s) salvos, '
              f'{removidos} removido(s).', 'success')
        return redirect(url_for('b2b.cliente_precos', cid=cid))

    # Universo: catálogo de atacado + itens já vendidos a ESSE cliente +
    # itens que já têm preço específico (não some da tela ao tirar o preço
    # de atacado do cadastro).
    especificos = {(pc.kind, pc.item_id): pc
                   for pc in PrecoClienteB2B.query.filter_by(cliente_id=cid)}
    ultimos = {}   # último preço praticado pra esse cliente (referência)
    for it, venda in (db.session.query(VendaB2BItem, VendaB2B)
                      .join(VendaB2B, VendaB2BItem.venda_id == VendaB2B.id)
                      .filter(VendaB2B.cliente_id == cid,
                              VendaB2B.status == 'ativa')
                      .order_by(VendaB2B.data_venda.asc(), VendaB2B.id.asc())
                      .all()):
        kind = 'receita' if it.receita_id else 'produto'
        ultimos[(kind, it.receita_id or it.produto_id)] = float(
            it.preco_unitario or 0)

    itens = []
    vistos = set()

    def _add(kind, obj, atacado):
        chave = (kind, obj.id)
        if chave in vistos:
            return
        vistos.add(chave)
        esp = especificos.get(chave)
        com_desc = None
        if atacado and cliente.desconto_percentual:
            com_desc = round(
                atacado * (1 - cliente.desconto_percentual / 100.0), 2)
        itens.append({
            'ref': f'{kind}:{obj.id}', 'nome': obj.nome,
            'categoria': getattr(obj, 'categoria', '') or '',
            'atacado': atacado, 'com_desconto': com_desc,
            'ultimo': ultimos.get(chave),
            'preco': (float(esp.preco) if esp else None),
        })

    for r in Receita.query.filter(Receita.arquivada_em.is_(None),
                                  Receita.preco_venda.isnot(None),
                                  Receita.preco_venda > 0
                                  ).order_by(Receita.nome).all():
        _add('receita', r, r.preco_venda)
    for p in Produto.query.filter(Produto.ativo.is_(True),
                                  Produto.preco_atacado.isnot(None),
                                  Produto.preco_atacado > 0
                                  ).order_by(Produto.nome).all():
        _add('produto', p, p.preco_atacado)
    faltantes = ({k for k in ultimos} | {k for k in especificos}) - vistos
    for kind, iid in sorted(faltantes):
        obj = (Receita.query.get(iid) if kind == 'receita'
               else Produto.query.get(iid))
        # Item arquivado/desativado fica FORA da gestao de precos futuros
        # (varredura 19/07/2026): aparecia misturado aos ativos sem badge e
        # o POST aceitava salvar preco novo pra ele. O historico de preco
        # segue no banco; a tela e do portfolio vivo.
        if obj is None:
            continue
        if kind == 'receita' and obj.arquivada_em:
            continue
        if kind == 'produto' and not obj.ativo:
            continue
        _add(kind, obj, (obj.preco_venda if kind == 'receita'
                         else obj.preco_atacado) or None)

    return render_template('b2b/cliente_precos.html', cliente=cliente,
                           itens=itens)


@b2b_bp.route('/api/cnpj/<cnpj>')
@login_required
@admin_required
def api_cnpj(cnpj):
    """Consulta o CNPJ na base pública da Receita (BrasilAPI + fallback) e
    devolve os dados normalizados pro botão "Buscar" do cadastro de cliente
    preencher razão social/endereço/e-mail — igual ao Tiny."""
    from app.services import cnpj as cnpj_svc
    res = cnpj_svc.consultar(cnpj)
    if res.get('erro'):
        return jsonify(res), 404 if 'não encontrado' in res['erro'] else 400
    return jsonify(res)


# ── Faturas mensais (fechamento da conta do cliente) ──
# Cliente `faturamento_mensal` compra o mês inteiro (vendas sem parcela);
# aqui a conta FECHA: fatura agrupa as vendas, emite UMA NF consolidada e
# UM boleto do total. Serviço: app/services/faturas_b2b.py.

@b2b_bp.route('/faturas')
@login_required
@admin_required
def faturas():
    from app.services import faturas_b2b as fat_svc
    lista = (FaturaB2B.query
             .options(joinedload(FaturaB2B.cliente))
             .order_by(FaturaB2B.id.desc()).limit(100).all())
    # Contas em aberto por cliente mensal: total das vendas ainda sem
    # fatura/parcela (o que fecharia se a conta fosse fechada hoje).
    hoje_ = hoje()
    inicio_mes = hoje_.replace(day=1)
    abertas = []
    for cli in (ClienteB2B.query
                .filter_by(ativo=True, faturamento_mensal=True)
                .order_by(ClienteB2B.nome).all()):
        vendas = fat_svc.vendas_para_fechar(cli.id, date(2000, 1, 1), hoje_)
        if vendas:
            from decimal import Decimal
            total = sum((Decimal(v.valor_total or 0) for v in vendas),
                        Decimal('0'))
            abertas.append({'cliente': cli, 'n_vendas': len(vendas),
                            'total': total,
                            'primeira': min(v.data_venda for v in vendas),
                            'ultima': max(v.data_venda for v in vendas)})
    return render_template('b2b/faturas.html', faturas=lista,
                           abertas=abertas, hoje=hoje_,
                           inicio_mes=inicio_mes)


@b2b_bp.route('/faturas/fechar', methods=['POST'])
@login_required
@admin_required
def fatura_fechar():
    from app.services import faturas_b2b as fat_svc
    cliente = ClienteB2B.query.get_or_404(
        request.form.get('cliente_id', type=int) or 0)
    try:
        data_inicio = date.fromisoformat(request.form.get('data_inicio') or '')
        data_fim = date.fromisoformat(request.form.get('data_fim') or '')
        vencimento = date.fromisoformat(request.form.get('vencimento') or '')
    except ValueError:
        flash('Datas inválidas — preencha início, fim e vencimento.',
              'danger')
        return redirect(url_for('b2b.faturas'))
    try:
        fatura = fat_svc.fechar_conta(cliente, data_inicio, data_fim,
                                      vencimento, user_id=current_user.id)
    except ValueError as exc:
        db.session.rollback()
        flash(f'Erro: {exc}', 'danger')
        return redirect(url_for('b2b.faturas'))
    flash(f'Conta fechada: fatura {fatura.codigo} de {cliente.nome} — '
          f'{len(fatura.vendas)} venda(s), R$ {fatura.valor_total}. Agora '
          'NF e boleto entraram na fila automática. Acompanhe em Cobranças → Automação e Sicredi.', 'success')
    return redirect(url_for('b2b.fatura_detalhe', fid=fatura.id))


@b2b_bp.route('/faturas/<int:fid>')
@login_required
@admin_required
def fatura_detalhe(fid):
    fatura = (FaturaB2B.query
              .options(joinedload(FaturaB2B.cliente),
                       joinedload(FaturaB2B.vendas))
              .get_or_404(fid))
    cobranca = fatura.cobrancas[0] if fatura.cobrancas else None
    return render_template('b2b/fatura_detalhe.html', fatura=fatura,
                           cobranca=cobranca)


@b2b_bp.route('/faturas/<int:fid>/cancelar', methods=['POST'])
@login_required
@admin_required
def fatura_cancelar(fid):
    from app.services import faturas_b2b as fat_svc
    fatura = FaturaB2B.query.get_or_404(fid)
    try:
        fat_svc.cancelar_fatura(fatura, user_id=current_user.id)
    except ValueError as exc:
        flash(f'Não cancelei: {exc}', 'danger')
        return redirect(url_for('b2b.fatura_detalhe', fid=fid))
    flash(f'Fatura {fatura.codigo} cancelada — as vendas voltaram pra conta '
          'aberta do cliente.', 'warning')
    return redirect(url_for('b2b.faturas'))


@b2b_bp.route('/faturas/<int:fid>/emitir-nf', methods=['POST'])
@login_required
def fatura_emitir_nf(fid):
    """NF consolidada da fatura no Tiny (mesma semântica da venda:
    `recriar=1` descarta rascunho rejeitado e refaz)."""
    from flask import abort
    if not current_user.pode_emitir_nf_b2b():
        abort(403)
    fatura = FaturaB2B.query.get_or_404(fid)
    recriar = request.form.get('recriar') in ('1', 'true', 'on')
    if recriar and not current_user.is_dono():
        abort(403)
    res = tiny_nf_b2b.emitir_nf_fatura(fatura, user_id=current_user.id,
                                       recriar=recriar)
    flash(f'Fatura {fatura.codigo}: {res["msg"]}',
          'success' if res.get('ok') else 'danger')
    return redirect(url_for('b2b.fatura_detalhe', fid=fid))


@b2b_bp.route('/faturas/<int:fid>/danfe')
@login_required
@admin_required
def fatura_danfe(fid):
    from app.services import tiny
    fatura = FaturaB2B.query.get_or_404(fid)
    url, motivo = tiny.obter_link_nota_fiscal_com_motivo(
        fatura.tiny_nota_fiscal_id)
    if not url:
        flash(f'Fatura {fatura.codigo}: não consegui obter o DANFE no '
              f'Tiny — {motivo}.', 'warning')
        return redirect(url_for('b2b.fatura_detalhe', fid=fid))
    return redirect(url)


@b2b_bp.route('/faturas/<int:fid>/enviar-nf-email', methods=['POST'])
@login_required
@admin_required
def fatura_enviar_nf_email(fid):
    """Compatibilidade com telas antigas, sem envio individual."""
    FaturaB2B.query.get_or_404(fid)
    flash('O envio agora é sempre NF + boleto. Confira os documentos antes de confirmar.', 'info')
    return redirect(url_for('cobrancas.documentos', tipo='fatura', ref=fid), code=303)


# ── Vendas ──

@b2b_bp.route('/vendas')
@login_required
@admin_required
def vendas():
    q = (VendaB2B.query
         .options(joinedload(VendaB2B.cliente))
         .order_by(VendaB2B.data_venda.desc(), VendaB2B.id.desc()))
    status = request.args.get('status')
    if status in ('ativa', 'cancelada'):
        q = q.filter_by(status=status)
    return render_template('b2b/vendas.html', vendas=q.limit(100).all(),
                           status_filtro=status)


def _catalogo_venda(excluir_venda_id=None):
    """Catalogo + precos + estoque compartilhados pelos forms de nova/editar
    venda. `excluir_venda_id` (form de editar): o comprometido da propria
    venda nao desconta do disponivel exibido pra ela mesma."""
    clientes = ClienteB2B.query.filter_by(ativo=True).order_by(ClienteB2B.nome).all()
    # ativas(): receita arquivada nao entra em venda NOVA (varredura
    # 19/07/2026 — Produto ao lado ja filtrava; a divergencia era o furo).
    receitas = Receita.ativas().order_by(Receita.categoria, Receita.nome).all()
    produtos = Produto.query.filter_by(ativo=True).order_by(Produto.nome).all()
    # GRANDFATHER no editar (mesma regra do MP pedivel de 07/07/2026): item
    # que JA esta na venda em edicao continua no catalogo mesmo arquivado —
    # sem isso o re-salvar do form derrubaria a linha existente.
    if excluir_venda_id:
        venda_atual = VendaB2B.query.get(excluir_venda_id)
        for it in (venda_atual.itens if venda_atual else []):
            if it.receita_id and it.receita and it.receita.arquivada_em:
                receitas.append(it.receita)
            if it.produto_id and it.produto and not it.produto.ativo:
                produtos.append(it.produto)
    # Preco atacado vem do cadastro: Receita.preco_venda, Produto.preco_atacado
    # (mesma logica de /cardapio?tipo=atacado).
    precos_map = {}
    for r in receitas:
        if r.preco_venda:
            precos_map[f'receita:{r.id}'] = r.preco_venda
    for p in produtos:
        if p.preco_atacado:
            precos_map[f'produto:{p.id}'] = p.preco_atacado
    # Preco ESPECIFICO por cliente (tabela PrecoClienteB2B) — vence o
    # atacado padrao no form; o JS troca quando o cliente e selecionado.
    precos_cliente_map = {}
    for pc in PrecoClienteB2B.query.all():
        (precos_cliente_map.setdefault(pc.cliente_id, {})
         )[f'{pc.kind}:{pc.item_id}'] = float(pc.preco)
    # Estoque DISPONIVEL por item = fisico − comprometido com vendas B2B
    # ainda nao separadas (a baixa e na separacao, 07/07/2026). Mostrar o
    # fisico cru deixava duas vendas serem aprovadas contra o mesmo saldo.
    pendente = svc.comprometido_b2b_pendente(excluir_venda_id=excluir_venda_id)
    estoque_map = {}
    for ep in EstoqueProducao.query.all():
        if ep.receita_id:
            chave, ref = ('receita', ep.receita_id), f'receita:{ep.receita_id}'
        elif ep.produto_id:
            chave, ref = ('produto', ep.produto_id), f'produto:{ep.produto_id}'
        else:
            continue
        estoque_map[ref] = (ep.quantidade or 0) - pendente.get(chave, 0)
    return {'clientes': clientes, 'receitas': receitas, 'produtos': produtos,
            'precos_map': precos_map, 'estoque_map': estoque_map,
            'precos_cliente_map': precos_cliente_map}


def _parse_venda_form():
    """Le o form de venda (nova/editar). Retorna (campos, itens, parcelas,
    frete_valor).

    `campos` casa com a assinatura de criar_venda/editar_venda/editar_cabecalho;
    `frete_valor` vai à parte porque editar_cabecalho NÃO aceita frete.
    """
    cliente_id = request.form.get('cliente_id', type=int) or None
    cliente_nome = (request.form.get('cliente_nome') or '').strip() or None
    data_str = request.form.get('data_venda') or hoje().isoformat()
    try:
        data_venda = date.fromisoformat(data_str)
    except ValueError:
        data_venda = hoje()
    data_ent_str = (request.form.get('data_entrega') or '').strip()
    try:
        data_entrega = date.fromisoformat(data_ent_str) if data_ent_str else None
    except ValueError:
        data_entrega = None
    campos = {
        'cliente_id': cliente_id,
        'cliente_nome': cliente_nome,
        'data_venda': data_venda,
        'data_entrega': data_entrega,
        'nf_numero': (request.form.get('nf_numero') or '').strip() or None,
        'observacao': (request.form.get('observacao') or '').strip() or None,
    }

    # Itens: item_ref[]="receita:5", item_qtd[], item_preco[], item_desc[],
    # item_estado[], item_obs[]
    refs = request.form.getlist('item_ref[]')
    qtds = request.form.getlist('item_qtd[]')
    precos = request.form.getlist('item_preco[]')
    descs = request.form.getlist('item_desc[]')
    estados = request.form.getlist('item_estado[]')
    obss = request.form.getlist('item_obs[]')
    itens = []
    for i, ref in enumerate(refs):
        ref = (ref or '').strip()
        if not ref or ':' not in ref:
            continue
        tipo, _, sid = ref.partition(':')
        if tipo not in ('receita', 'produto') or not sid.isdigit():
            continue
        try:
            qtd = int(qtds[i])
        except (IndexError, ValueError):
            continue
        if qtd <= 0:
            continue
        try:
            preco = float((precos[i] or '0').replace(',', '.'))
        except (IndexError, ValueError):
            preco = 0
        try:
            desc = float((descs[i] or '0').replace(',', '.'))
        except (IndexError, ValueError):
            desc = 0
        est = (estados[i].strip().lower() if i < len(estados) else '') or None
        obs = (obss[i].strip() if i < len(obss) else '') or None
        itens.append({'tipo': tipo, 'id': int(sid), 'quantidade': qtd,
                      'preco_unitario': preco, 'desconto_percentual': desc,
                      'estado': est, 'observacao': obs})

    # Frete da entrega (R$). FORA de `campos` de propósito: o caminho de
    # venda PAGA usa editar_cabecalho(**campos), e frete muda o total — fica
    # travado junto com itens/parcelas (só criar_venda/editar_venda recebem).
    # parse_float_br: '1.234,56' funciona e valor INVÁLIDO levanta
    # ValueError (dinheiro não vira zero calado — convenção do projeto);
    # os POSTs traduzem em flash + redirect.
    from app.utils import parse_float_br
    frete_valor = parse_float_br(request.form.get('frete_valor'), default=0)

    # Parcelas: parcela_venc[], parcela_valor[], parcela_forma[]
    vencs = request.form.getlist('parcela_venc[]')
    valores = request.form.getlist('parcela_valor[]')
    formas = request.form.getlist('parcela_forma[]')
    parcelas = []
    for i, v in enumerate(vencs):
        v = (v or '').strip()
        if not v:
            continue
        try:
            valor = float((valores[i] or '0').replace(',', '.'))
        except (IndexError, ValueError):
            valor = 0
        if valor <= 0:
            continue
        try:
            venc = date.fromisoformat(v)
        except ValueError:
            continue
        forma = (formas[i] if i < len(formas) else '') or None
        parcelas.append({'vencimento': venc, 'valor': valor,
                         'forma_pagamento': forma})
    return campos, itens, parcelas, frete_valor


@b2b_bp.route('/vendas/nova')
@login_required
@admin_required
def venda_nova():
    """Form de nova venda. `?orcamento=<id>` pré-preenche cliente + itens
    a partir de um orçamento APROVADO (o "→ Virar venda" da aba Aprovados).
    Item de linha livre do orçamento (sem vínculo com o catálogo) não vira
    item de venda — avisa e pula."""
    from app.models import Orcamento
    itens_seed, cliente_pre = [], None
    orc_id = request.args.get('orcamento', type=int)
    if orc_id:
        orc = Orcamento.query.get_or_404(orc_id)
        if orc.venda_id:
            flash(f'O orçamento {orc.codigo} já virou a venda '
                  f'#{orc.venda_id} — não converta duas vezes.', 'warning')
            return redirect(url_for('b2b.venda_detalhe', vid=orc.venda_id))
        cliente_pre = orc.cliente_id
        pulados = []
        for it in orc.itens:
            if not (it.receita_id or it.produto_id):
                pulados.append(it.nome)
                continue
            itens_seed.append({
                'ref': (f'receita:{it.receita_id}' if it.receita_id
                        else f'produto:{it.produto_id}'),
                'nome': it.nome,
                'qtd': int(round(float(it.quantidade or 1))),
                'estado': '',
                'preco': float(it.preco_unitario or 0),
                'desc': 0,
                'obs': it.observacao or '',
            })
        flash(f'Itens e cliente vindos do orçamento {orc.codigo} — confira '
              'preços e a data de entrega antes de salvar.', 'info')
        if (orc.desconto_valor or 0) > 0:
            flash(f'O orçamento tem desconto de R$ '
                  f'{orc.desconto_valor:.2f} que NÃO entra automaticamente '
                  '— embuta nos preços unitários (a venda não tem campo de '
                  'desconto em R$), senão a venda sai MAIOR que o '
                  'orçamento.', 'warning')
        if pulados:
            flash('Itens de linha livre do orçamento não entram na venda '
                  '(sem vínculo com o catálogo): ' + ', '.join(pulados),
                  'warning')
    return render_template('b2b/venda_nova.html', venda=None,
                           itens_seed=itens_seed, parcelas_seed=[],
                           pago=False, cliente_pre=cliente_pre,
                           orcamento_id=orc_id,
                           data_entrega_pre=(orc.data_entrega.isoformat()
                                             if orc_id and orc.data_entrega
                                             else ''),
                           frete_pre=(float(orc.frete_valor or 0)
                                      if orc_id else 0),
                           hoje=hoje().isoformat(), **_catalogo_venda())


@b2b_bp.route('/vendas/nova', methods=['POST'])
@login_required
@admin_required
def venda_criar():
    # Guard ANTES de criar (o GET de venda_nova tem o mesmo, mas o form
    # pode ter ficado aberto numa aba enquanto o orçamento era convertido
    # por outro caminho — sem isso a demanda entraria em DOBRO na fila).
    orc_id = request.form.get('orcamento_id', type=int)
    # Erro re-abre o form JÁ com o seed do orçamento (sem o param, o
    # retry perdia o vínculo e o guard anti-conversão-dupla).
    url_form = (url_for('b2b.venda_nova', orcamento=orc_id) if orc_id
                else url_for('b2b.venda_nova'))
    try:
        campos, itens, parcelas, frete_valor = _parse_venda_form()
    except ValueError as exc:
        flash(f'Erro: {exc}', 'danger')
        return redirect(url_form)
    orc = Orcamento.query.get(orc_id) if orc_id else None
    if orc and orc.venda_id:
        flash(f'O orçamento {orc.codigo} já virou a venda #{orc.venda_id} '
              '— nada foi criado.', 'warning')
        return redirect(url_for('b2b.venda_detalhe', vid=orc.venda_id))
    if not campos['data_entrega']:
        flash('Informe a data de entrega ao padeiro.', 'warning')
        return redirect(url_form)
    if not itens:
        flash('Adicione pelo menos 1 item.', 'danger')
        return redirect(url_form)
    try:
        venda = svc.criar_venda(**campos, itens=itens,
                                parcelas=parcelas or None,
                                frete_valor=frete_valor, user=current_user)
    except ValueError as exc:
        db.session.rollback()
        flash(f'Erro: {exc}', 'danger')
        return redirect(url_form)

    # Venda criada a partir de um orçamento (seed manual): grava o vínculo
    # pra ele sair da fila de Aprovados e não ser convertido de novo.
    if orc and not orc.venda_id:
        orc.venda_id = venda.id
        db.session.commit()

    flash(f'Venda B2B #{venda.id} criada — R$ {venda.valor_total:.2f}.', 'success')
    return redirect(url_for('b2b.venda_detalhe', vid=venda.id))


@b2b_bp.route('/vendas/<int:vid>/editar')
@login_required
@admin_required
def venda_editar(vid):
    venda = (VendaB2B.query
             .options(joinedload(VendaB2B.itens), joinedload(VendaB2B.parcelas))
             .get_or_404(vid))
    if venda.status == 'cancelada':
        flash('Venda cancelada — reabra antes de editar.', 'warning')
        return redirect(url_for('b2b.venda_detalhe', vid=vid))
    if venda.fatura_id:
        flash(f'Venda faturada ({venda.fatura.codigo}) — cancele a fatura '
              'em B2B → Faturas mensais antes de editar.', 'warning')
        return redirect(url_for('b2b.venda_detalhe', vid=vid))
    pago = bool(venda.valor_pago and venda.valor_pago > 0)
    itens_seed = [{
        'ref': (f'receita:{it.receita_id}' if it.receita_id
                else f'produto:{it.produto_id}'),
        'nome': it.nome_item,
        'qtd': it.quantidade,
        'estado': it.estado or '',
        'preco': float(it.preco_unitario or 0),
        'desc': it.desconto_percentual or 0,
        'obs': it.observacao or '',
    } for it in venda.itens]
    parcelas_seed = [{
        'venc': p.vencimento.isoformat(),
        'valor': float(p.valor or 0),
        'forma': p.forma_pagamento or '',
    } for p in sorted(venda.parcelas, key=lambda x: x.numero)]
    return render_template('b2b/venda_nova.html', venda=venda,
                           itens_seed=itens_seed, parcelas_seed=parcelas_seed,
                           pago=pago, hoje=hoje().isoformat(),
                           **_catalogo_venda(excluir_venda_id=vid))


@b2b_bp.route('/vendas/<int:vid>/editar', methods=['POST'])
@login_required
@admin_required
def venda_editar_post(vid):
    venda = VendaB2B.query.get_or_404(vid)
    if venda.status == 'cancelada':
        flash('Venda cancelada — reabra antes de editar.', 'warning')
        return redirect(url_for('b2b.venda_detalhe', vid=vid))
    if venda.fatura_id:
        # Inclui o caminho do editar_cabecalho: mudar cliente/valores de
        # venda faturada dessincroniza fatura/boleto/NF.
        flash(f'Venda faturada ({venda.fatura.codigo}) — cancele a fatura '
              'em B2B → Faturas mensais antes de editar.', 'warning')
        return redirect(url_for('b2b.venda_detalhe', vid=vid))
    try:
        campos, itens, parcelas, frete_valor = _parse_venda_form()
    except ValueError as exc:
        flash(f'Erro: {exc}', 'danger')
        return redirect(url_for('b2b.venda_editar', vid=vid))
    if not campos['data_entrega']:
        flash('Informe a data de entrega ao padeiro.', 'warning')
        return redirect(url_for('b2b.venda_editar', vid=vid))
    # Venda com pagamento: itens travados — atualiza so o cabecalho.
    if venda.valor_pago and venda.valor_pago > 0:
        try:
            svc.editar_cabecalho(venda, **campos)
        except ValueError as exc:
            db.session.rollback()
            flash(f'Erro: {exc}', 'danger')
            return redirect(url_for('b2b.venda_editar', vid=vid))
        flash('Cabecalho atualizado (itens travados: venda com pagamento).', 'success')
        return redirect(url_for('b2b.venda_detalhe', vid=vid))
    if not itens:
        flash('Adicione pelo menos 1 item.', 'danger')
        return redirect(url_for('b2b.venda_editar', vid=vid))
    try:
        svc.editar_venda(venda, **campos, itens=itens,
                         parcelas=parcelas or None,
                         frete_valor=frete_valor, user=current_user)
    except ValueError as exc:
        db.session.rollback()
        flash(f'Erro: {exc}', 'danger')
        return redirect(url_for('b2b.venda_editar', vid=vid))
    flash(f'Venda #{vid} atualizada.'
          + (' Estoque reajustado.' if venda.estoque_baixado_em
             else ' O estoque sai na separacao pelo padeiro.'), 'success')
    return redirect(url_for('b2b.venda_detalhe', vid=vid))


@b2b_bp.route('/vendas/<int:vid>/reabrir', methods=['POST'])
@login_required
@admin_required
def venda_reabrir(vid):
    venda = VendaB2B.query.get_or_404(vid)
    svc.reabrir_venda(venda, user=current_user)
    flash(f'Venda #{vid} reaberta.'
          + (' Estoque baixado de novo.' if venda.estoque_baixado_em
             else ' O estoque sai na separacao pelo padeiro.'), 'success')
    return redirect(url_for('b2b.venda_detalhe', vid=vid))


@b2b_bp.route('/vendas/<int:vid>/status-voltar', methods=['POST'])
@login_required
@admin_required
def venda_status_voltar(vid):
    venda = VendaB2B.query.get_or_404(vid)
    svc.reverter_status_entrega(venda, user=current_user)
    flash(f'Status de entrega revertido para "{venda.status_entrega}".'
          + ('' if venda.estoque_baixado_em or venda.data_entrega is None
             else ' Baixa estornada — o estoque sai de novo na separacao.'),
          'info')
    return redirect(url_for('b2b.venda_detalhe', vid=vid))


@b2b_bp.route('/vendas/<int:vid>')
@login_required
@admin_required
def venda_detalhe(vid):
    venda = (VendaB2B.query
             .options(joinedload(VendaB2B.cliente),
                      joinedload(VendaB2B.itens),
                      joinedload(VendaB2B.parcelas))
             .get_or_404(vid))
    # Tem boleto pronto (cobrança com nosso número) → habilita o botão
    # "Enviar NF + Boleto juntos".
    tem_boleto = any(p.cobranca and p.cobranca[0].nosso_numero
                     for p in venda.parcelas)
    return render_template('b2b/venda_detalhe.html', venda=venda,
                           tem_boleto=tem_boleto)


@b2b_bp.route('/vendas/<int:vid>/entrega', methods=['POST'])
@login_required
@admin_required
def venda_entrega(vid):
    """Define/limpa a data de entrega de uma venda B2B (entra/sai da fila do
    padeiro). Vazio = volta a ser venda imediata (nao aparece no padeiro).
    O regime da baixa acompanha: virou imediata sem ter baixado → baixa
    agora; entrou na fila antes de separar → estorna (a separacao baixa)."""
    venda = VendaB2B.query.get_or_404(vid)
    data_str = (request.form.get('data_entrega') or '').strip()
    tinha_baixado = bool(venda.estoque_baixado_em)
    try:
        venda.data_entrega = date.fromisoformat(data_str) if data_str else None
    except ValueError:
        flash('Data invalida.', 'warning')
        return redirect(url_for('b2b.venda_detalhe', vid=vid))
    svc.sincronizar_baixa_com_data(venda, user=current_user)
    db.session.commit()
    if bool(venda.estoque_baixado_em) != tinha_baixado:
        flash('Data de entrega atualizada — '
              + ('estoque baixado agora (venda imediata, fora da fila '
                 'do padeiro).' if venda.estoque_baixado_em
                 else 'baixa estornada; o estoque sai na separacao pelo '
                      'padeiro.'), 'success')
    else:
        flash('Data de entrega atualizada.', 'success')
    return redirect(url_for('b2b.venda_detalhe', vid=vid))


@b2b_bp.route('/vendas/<int:vid>/cancelar', methods=['POST'])
@login_required
@admin_required
def venda_cancelar(vid):
    venda = VendaB2B.query.get_or_404(vid)
    try:
        svc.cancelar_venda(venda, user=current_user)
    except ValueError as exc:
        flash(f'Não cancelei: {exc}', 'danger')
        return redirect(url_for('b2b.venda_detalhe', vid=vid))
    flash(f'Venda #{vid} cancelada e estoque estornado.', 'warning')
    return redirect(url_for('b2b.venda_detalhe', vid=vid))


@b2b_bp.route('/vendas/<int:vid>/excluir', methods=['POST'])
@owner_required
def venda_excluir(vid):
    """Exclusão DEFINITIVA (dono) — limpeza de venda de teste/errada.
    O service estorna o estoque e recusa venda faturada/paga/com boleto
    no banco."""
    venda = VendaB2B.query.get_or_404(vid)
    try:
        svc.excluir_venda(venda, user=current_user)
    except ValueError as exc:
        flash(f'Não excluí: {exc}', 'danger')
        return redirect(url_for('b2b.venda_detalhe', vid=vid))
    flash(f'Venda #{vid} excluída definitivamente (estoque estornado; '
          'os movimentos ficam no histórico).', 'warning')
    return redirect(url_for('b2b.dashboard'))


# ── NF-e via Tiny (06/07/2026) ──
# Mesmo fluxo do site (loja online): botão manual, emissão pelo dono.
# Enviar por e-mail (NF já emitida) pode ser feito por admin.

@b2b_bp.route('/vendas/<int:vid>/emitir-nf', methods=['POST'])
@login_required
def venda_emitir_nf(vid):
    """Emite a NF da venda no Tiny. `recriar=1` descarta o rascunho
    rejeitado e refaz do zero (mesma semântica do site)."""
    from flask import abort
    if not current_user.pode_emitir_nf_b2b():
        abort(403)
    venda = VendaB2B.query.get_or_404(vid)
    recriar = request.form.get('recriar') in ('1', 'true', 'on')
    if recriar and not current_user.is_dono():
        abort(403)
    res = tiny_nf_b2b.emitir_nf(venda, user_id=current_user.id,
                                recriar=recriar)
    flash(f'Venda #{vid}: {res["msg"]}',
          'success' if res.get('ok') else 'danger')
    return redirect(url_for('b2b.venda_detalhe', vid=vid))


# ── Mapeamento de SKUs do Tiny — canal B2B ──
# No Tiny o B2B é OUTRO cadastro/lista de preço: o mesmo item nosso pode
# ter SKU diferente do site. Mesma UX da tela do site
# (/admin/loja-online/tiny-skus), mas gravando com canal='b2b'.

@b2b_bp.route('/tiny-skus')
@owner_required
def tiny_skus():
    from app.services import tiny_nf
    itens = tiny_nf.itens_para_mapear(canal='b2b')
    pendentes = sum(1 for i in itens if i['estado'] != 'mapeado')
    return render_template(
        'tiny_skus.html', itens=itens, pendentes=pendentes,
        total=len(itens),
        titulo='SKUs do Tiny (NF-e) — B2B',
        descricao='A lista cobre o catálogo de atacado (receitas com preço '
                  'de atacado, produtos com preço de atacado e itens já '
                  'vendidos em venda B2B). Use o export/planilha do '
                  'CADASTRO B2B do Tiny — o site tem mapa próprio.',
        url_definir=url_for('b2b.tiny_definir'),
        url_sync=url_for('b2b.tiny_sync'),
        url_importar=url_for('b2b.tiny_importar'),
        vazio_msg='Nenhum item de atacado ainda (precisa de preço de '
                  'atacado no cadastro ou de uma venda B2B).')


@b2b_bp.route('/tiny-skus/sync', methods=['POST'])
@owner_required
def tiny_sync():
    """Busca o catálogo do Tiny e sugere SKUs por nome pros itens B2B."""
    from app.services import tiny_nf
    res = tiny_nf.sincronizar_sugestoes(user_id=current_user.id, canal='b2b')
    if res.get('erro'):
        flash(f'Sincronização falhou: {res["erro"]}', 'danger')
    else:
        flash(f'{res.get("exatos", 0)} confirmados (nome idêntico) + '
              f'{res.get("sugeridos", 0)} sugeridos pra conferir, '
              f'{res.get("sem_match", 0)} sem correspondência '
              f'({res.get("total_tiny", 0)} produtos no Tiny).', 'success')
    return redirect(url_for('b2b.tiny_skus'))


@b2b_bp.route('/tiny-skus/importar', methods=['POST'])
@owner_required
def tiny_importar():
    """Importa o export de produtos B2B do Tiny (.xls/.csv) e mapeia SKUs
    por nome. Nome idêntico confirma automático; parecido vira sugestão."""
    from app.services import tiny_nf
    f = request.files.get('planilha')
    if not f or not f.filename:
        flash('Selecione a planilha de produtos do Tiny (.xls ou .csv).',
              'warning')
        return redirect(url_for('b2b.tiny_skus'))
    conteudo = f.read()
    res = tiny_nf.importar_planilha(conteudo, f.filename,
                                    user_id=current_user.id, canal='b2b')
    if res.get('erro'):
        flash(res['erro'], 'danger')
    else:
        flash(f'Planilha importada: {res.get("exatos", 0)} confirmados '
              f'(nome idêntico) + {res.get("sugeridos", 0)} sugeridos pra '
              f'conferir, {res.get("sem_match", 0)} sem correspondência '
              f'({res.get("total", 0)} linhas).', 'success')
    return redirect(url_for('b2b.tiny_skus'))


@b2b_bp.route('/tiny-skus/definir', methods=['POST'])
@owner_required
def tiny_definir():
    """Define/limpa o SKU B2B de um item (kind + item_id + sku)."""
    from app.services import tiny_nf
    kind = (request.form.get('kind') or '').strip()
    try:
        item_id = int(request.form.get('item_id'))
    except (TypeError, ValueError):
        flash('Item inválido.', 'warning')
        return redirect(url_for('b2b.tiny_skus'))
    sku = (request.form.get('sku') or '').strip()
    tiny_nf.definir_sku(kind, item_id, sku, user_id=current_user.id,
                        canal='b2b')
    flash('SKU salvo.' if sku else 'SKU removido.', 'success')
    return redirect(url_for('b2b.tiny_skus'))


@b2b_bp.route('/vendas/<int:vid>/danfe')
@login_required
@admin_required
def venda_danfe(vid):
    """Redireciona pro DANFE (PDF) no Tiny. Link temporário — busca sob
    demanda."""
    from app.services import tiny
    venda = VendaB2B.query.get_or_404(vid)
    url, motivo = tiny.obter_link_nota_fiscal_com_motivo(
        venda.tiny_nota_fiscal_id)
    if not url:
        flash(f'Venda #{vid}: não consegui obter o DANFE no Tiny — '
              f'{motivo}.', 'warning')
        return redirect(url_for('b2b.venda_detalhe', vid=vid))
    return redirect(url)


@b2b_bp.route('/vendas/<int:vid>/enviar-nf-email', methods=['POST'])
@login_required
@admin_required
def venda_enviar_nf_email(vid):
    """Compatibilidade com telas antigas, sem envio individual."""
    VendaB2B.query.get_or_404(vid)
    flash('O envio agora é sempre NF + boleto. Confira os documentos antes de confirmar.', 'info')
    return redirect(url_for('b2b.venda_documentos', vid=vid), code=303)


@b2b_bp.route('/vendas/<int:vid>/enviar-nf-boleto-email', methods=['POST'])
@login_required
@admin_required
def venda_enviar_nf_boleto_email(vid):
    """Centraliza inclusive o formulário conjunto antigo na confirmação auditada."""
    VendaB2B.query.get_or_404(vid)
    flash('Confira os documentos e confirme o envio na central de cobranças.', 'info')
    return redirect(url_for('b2b.venda_documentos', vid=vid), code=303)


@b2b_bp.route('/vendas/<int:vid>/documentos')
@login_required
@admin_required
def venda_documentos(vid):
    """Abre o conjunto correto; vendas parceladas mantêm a escolha da parcela."""
    venda = VendaB2B.query.get_or_404(vid)
    if venda.fatura_id:
        return redirect(url_for('cobrancas.documentos', tipo='fatura', ref=venda.fatura_id))
    if len(venda.parcelas) == 1:
        return redirect(url_for('cobrancas.documentos', tipo='parcela', ref=venda.parcelas[0].id))
    flash('Escolha a parcela em Documentos / Histórico para enviar NF + boleto.', 'info')
    return redirect(url_for('b2b.venda_detalhe', vid=vid, _anchor='boletos'))


@b2b_bp.route('/vendas/<int:vid>/sem-cobranca', methods=['POST'])
@login_required
@owner_required
def venda_sem_cobranca(vid):
    from app.services.cobrancas_dispensa import dispensar
    VendaB2B.query.get_or_404(vid)
    if request.form.get('confirmar') != '1':
        flash('Confirme que se trata de divulgação, sem cobrança ao cliente.', 'warning')
    else:
        try:
            dispensar(vid, current_user, request.form.get('motivo'))
            db.session.commit()
            flash('Divulgação registrada como sem cobrança. Venda, entrega e estoque preservados.', 'success')
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'warning')
    return redirect(url_for('b2b.venda_detalhe', vid=vid), code=303)


@b2b_bp.route('/parcelas/<int:pid>/receber', methods=['POST'])
@login_required
@admin_required
def parcela_receber(pid):
    p = VendaB2BParcela.query.get_or_404(pid)
    try:
        v = float((request.form.get('valor') or '0').replace(',', '.'))
    except ValueError:
        flash('Valor invalido.', 'danger')
        return redirect(url_for('b2b.venda_detalhe', vid=p.venda_id))
    forma = (request.form.get('forma_pagamento') or '').strip() or None
    obs = (request.form.get('observacao') or '').strip() or None
    try:
        svc.receber_pagamento(p, v, forma_pagamento=forma, observacao=obs)
    except ValueError as exc:
        flash(f'Erro: {exc}', 'danger')
        return redirect(url_for('b2b.venda_detalhe', vid=p.venda_id))
    flash(f'Pagamento de R$ {v:.2f} registrado.', 'success')
    return redirect(url_for('b2b.venda_detalhe', vid=p.venda_id))


# ── Contas a receber ──

@b2b_bp.route('/contas-a-receber')
@login_required
@admin_required
def contas_receber():
    parcelas = (VendaB2BParcela.query
                .join(VendaB2B)
                .options(joinedload(VendaB2BParcela.venda)
                         .joinedload(VendaB2B.cliente))
                .filter(VendaB2B.status == 'ativa',
                        VendaB2B.dispensa_cobranca.is_(None),
                        VendaB2BParcela.pago_em.is_(None))
                .order_by(VendaB2BParcela.vencimento.asc())
                .all())
    total_aberto = sum(p.saldo for p in parcelas)
    return render_template('b2b/contas_receber.html', parcelas=parcelas,
                           total_aberto=total_aberto, hoje=hoje())


# ── Orcamentos B2B (encomendas corporativas, eventos, cestas em volume) ──

from app.models import Orcamento
from app.services import orcamentos as orc_svc


def _parse_itens_form(form):
    """Extrai linhas de item do form: itens[i][kind|id|nome|qtd|...].

    O form do template envia um nome estavel por indice (i=0,1,2...) e o JS
    permite adicionar/remover linhas. Aqui agrupamos por indice sem assumir
    contiguidade — linhas removidas no front simplesmente nao chegam.
    """
    import re
    pat = re.compile(r'^itens\[(\d+)\]\[([a-z_]+)\]$')
    grupos = {}
    for k, v in form.items():
        m = pat.match(k)
        if not m:
            continue
        idx, campo = int(m.group(1)), m.group(2)
        grupos.setdefault(idx, {})[campo] = v
    return [grupos[i] for i in sorted(grupos)]


def _ctx_form(form=None, erros=None, orc=None):
    """Contexto compartilhado entre GET e POST do form."""
    return dict(
        clientes=ClienteB2B.query
            .filter_by(ativo=True).order_by(ClienteB2B.nome).all(),
        receitas=Receita.query
            .filter(Receita.arquivada_em.is_(None))
            .order_by(Receita.nome).all(),
        produtos=Produto.query
            .filter_by(ativo=True).order_by(Produto.nome).all(),
        form=form or {},
        erros=erros or [],
        orc=orc,
    )


@b2b_bp.route('/orcamentos')
@login_required
@admin_required
def orcamentos():
    status = (request.args.get('status') or '').strip() or None
    q = Orcamento.query
    if status and status in orc_svc.STATUS_VALIDOS:
        q = q.filter_by(status=status)
    lista = q.order_by(Orcamento.criado_em.desc()).limit(200).all()
    contagens = {s: Orcamento.query.filter_by(status=s).count()
                 for s in orc_svc.STATUS_VALIDOS}
    return render_template('b2b/orcamentos.html', orcamentos=lista,
                           filtro_status=status, contagens=contagens,
                           STATUS_LABEL=orc_svc.STATUS_LABEL,
                           hoje_=hoje())


@b2b_bp.route('/orcamentos/novo', methods=['GET', 'POST'])
@login_required
@admin_required
def orcamento_novo():
    if request.method == 'POST':
        itens_raw = _parse_itens_form(request.form)
        orc, erros = orc_svc.criar_orcamento(
            request.form, itens_raw,
            usuario_id=getattr(current_user, 'id', None))
        if erros:
            flash('; '.join(erros), 'danger')
            return render_template(
                'b2b/orcamento_form.html',
                **_ctx_form(form=request.form, erros=erros))
        flash(f'Orcamento {orc.codigo} criado.', 'success')
        return redirect(url_for('b2b.orcamento_detalhe', oid=orc.id))
    return render_template('b2b/orcamento_form.html', **_ctx_form())


@b2b_bp.route('/orcamentos/<int:oid>')
@login_required
@admin_required
def orcamento_detalhe(oid):
    orc = Orcamento.query.get_or_404(oid)
    return render_template('b2b/orcamento_detalhe.html', orc=orc,
                           STATUS_LABEL=orc_svc.STATUS_LABEL)


@b2b_bp.route('/orcamentos/<int:oid>/editar', methods=['GET', 'POST'])
@login_required
@admin_required
def orcamento_editar(oid):
    orc = Orcamento.query.get_or_404(oid)
    if orc.status not in ('rascunho', 'enviado'):
        flash('Orcamento ja finalizado — nao editavel.', 'warning')
        return redirect(url_for('b2b.orcamento_detalhe', oid=orc.id))
    if request.method == 'POST':
        itens_raw = _parse_itens_form(request.form)
        ok, erros = orc_svc.atualizar_orcamento(orc, request.form, itens_raw)
        if not ok:
            flash('; '.join(erros), 'danger')
            return render_template(
                'b2b/orcamento_form.html',
                **_ctx_form(form=request.form, erros=erros, orc=orc))
        flash(f'Orcamento {orc.codigo} atualizado.', 'success')
        return redirect(url_for('b2b.orcamento_detalhe', oid=orc.id))
    return render_template('b2b/orcamento_form.html',
                           **_ctx_form(orc=orc))


@b2b_bp.route('/orcamentos/<int:oid>/status', methods=['POST'])
@login_required
@admin_required
def orcamento_status(oid):
    orc = Orcamento.query.get_or_404(oid)
    novo = (request.form.get('status') or '').strip()
    ok, erro = orc_svc.marcar_status(orc, novo,
                                     usuario_id=getattr(current_user, 'id', None))
    if not ok:
        flash(f'Erro: {erro}', 'danger')
    else:
        flash(f'Orcamento marcado como {orc_svc.STATUS_LABEL[novo]}.', 'success')
    return redirect(url_for('b2b.orcamento_detalhe', oid=orc.id))


@b2b_bp.route('/orcamentos/<int:oid>/arquivar', methods=['POST'])
@login_required
@admin_required
def orcamento_arquivar(oid):
    """Arquiva/desarquiva um RASCUNHO (toggle). Rascunho que não foi pra
    frente sai de Pendentes sem virar 'recusado' (recusado = cliente disse
    não). Pedido do dono 08/07/2026."""
    orc = Orcamento.query.get_or_404(oid)
    if orc.arquivado_em:
        ok, erro = orc_svc.desarquivar(orc)
        msg = f'Orçamento {orc.codigo} desarquivado — voltou pra Pendentes.'
    else:
        ok, erro = orc_svc.arquivar(orc)
        msg = (f'Orçamento {orc.codigo} arquivado — some de Pendentes; '
               'desarquive quando quiser retomar.')
    flash(msg if ok else f'Erro: {erro}', 'success' if ok else 'danger')
    return redirect(url_for('b2b.orcamento_detalhe', oid=orc.id))


@b2b_bp.route('/orcamentos/<int:oid>/pdf')
@login_required
@admin_required
def orcamento_pdf(oid):
    orc = Orcamento.query.get_or_404(oid)
    from app.services.pdf import gerar_orcamento_pdf
    pdf_bytes = gerar_orcamento_pdf(orc)
    from flask import Response
    return Response(pdf_bytes, mimetype='application/pdf',
                    headers={'Content-Disposition':
                             f'inline; filename="{orc.codigo}.pdf"'})
