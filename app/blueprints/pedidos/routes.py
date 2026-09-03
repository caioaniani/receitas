import io
from collections import defaultdict
from datetime import date, datetime, timedelta

from flask import (
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_login import current_user, login_required
from sqlalchemy.orm import joinedload, selectinload

from app.blueprints.pedidos import pedidos_bp
from app.decorators import (
    admin_required,
    consulta_pedidos_required,
    gerente_required,
    operacional_pedido_required,
    owner_required,
    pedidos_required,
    producao_required,
)
from app.extensions import db
from app.models import (
    Desperdicio,
    EstoqueLoja,
    EstoqueProducao,
    FotoRecebimento,
    Loja,
    MateriaPrima,
    MovEstoqueLoja,
    MovEstoqueProducao,
    PedidoItem,
    PedidoLocal,
    PedidoLoja,
    PedidoOnline,
    PedidoQRCode,
    PrecoLojaReceita,
    Produto,
    Receita,
    VendaB2B,
    VendaMapa,
    VendaMapaUso,
)
from app.utils import agora
from app.utils import hoje as hoje_brt


def _mps_pediveis():
    """MPs que a loja PODE pedir da indústria — só as marcadas no checkbox
    "sugerir pedido loja" do Banco de MPs (decisão do dono 07/07/2026: loja
    pedia MP que não devia; a flag que já alimentava a sugestão semanal
    virou TRAVA do pedido). Receitas e produtos seguem livres; MP é opt-in.
    Vale pro typeahead, pros forms de novo/editar e pra validação do POST."""
    return (MateriaPrima.ativas()
            .filter(MateriaPrima.sugerir_pedido_loja.is_(True))
            .order_by(MateriaPrima.nome))


def _mps_nao_pediveis(itens_norm):
    """Nomes das MPs em `itens_norm` que NÃO estão liberadas pra pedido de
    loja. Lista vazia = tudo certo. Usada na validação server-side do POST
    (o typeahead já filtra, mas POST direto/aba velha não pode furar)."""
    mp_ids = [it['materia_prima_id'] for it in itens_norm
              if it.get('materia_prima_id')]
    if not mp_ids:
        return []
    bloqueadas = (MateriaPrima.query
                  .filter(MateriaPrima.id.in_(mp_ids),
                          MateriaPrima.sugerir_pedido_loja.is_(False)).all())
    return [m.nome for m in bloqueadas]


def _parse_item_id(value):
    """Decodifica 'r_5'/'p_5'/'mp_5'/'5' em ('receita'|'produto'|'mp', id).
    Legacy: int puro = receita."""
    if not value:
        return None, None
    if value.startswith('r_'):
        try:
            return 'receita', int(value[2:])
        except ValueError:
            return None, None
    if value.startswith('p_'):
        try:
            return 'produto', int(value[2:])
        except ValueError:
            return None, None
    if value.startswith('mp_'):
        try:
            return 'mp', int(value[3:])
        except ValueError:
            return None, None
    try:
        return 'receita', int(value)
    except ValueError:
        return None, None


def _preco_interno_item(it):
    """Preco INTERNO (transferencia loja->industria) do item do pedido.

    Usado no /pedidos/relatorio: PedidoLoja e a loja pedindo a industria,
    entao o valor praticado e o `preco_interno` da receita/produto — NAO o
    preco de balcao (preco_loja), do site (preco_site) nem de atacado
    (preco_venda na receita / preco_atacado no produto).

    `preco_interno` e unico por receita/produto (nao ha override por loja como
    no PrecoLojaReceita, que e do preco de balcao). Item sem preco_interno
    setado retorna 0 — de proposito NAO caimos pra preco_loja: misturar fontes
    de preco num relatorio financeiro esconderia o dado faltante. O dono
    preenche o interno em /receitas/precos.
    """
    if it.receita_id:
        rec = Receita.query.get(it.receita_id)
        return (rec.preco_interno or 0) if rec else 0
    if it.produto_id:
        prod = Produto.query.get(it.produto_id)
        return (prod.preco_interno or 0) if prod else 0
    return 0


# Lojas com a 'Industria' (fabrica de producao) excluida.
# Industria existe como Loja so pra fins de RH/escala (padeiros, auxiliares),
# mas nao recebe pedidos, nao tem PDV, nao tem estoque de venda. Use em
# qualquer dropdown operacional (pedidos, estoque de loja, precos).
def _lojas_operacionais():
    return (Loja.query
            .filter(Loja.ativa.is_(True), Loja.nome != 'Industria')
            .order_by(Loja.nome)
            .all())


def _loja_do_usuario():
    """Retorna a loja com que o usuario opera por padrao.

    Admin e gerente: None (podem ver/atuar em qualquer loja).
    Outros papeis: forca a loja vinculada ao usuario.
    """
    if current_user.is_admin() or current_user.is_gerente():
        return None
    return current_user.loja_id


def _item_consulta(nome, quantidade):
    return {'nome': nome or '(item)', 'quantidade': int(quantidade or 0)}


@pedidos_bp.route('/observador')
@login_required
@consulta_pedidos_required
def painel_observador():
    """Sala de controle operacional, sem qualquer acao de escrita."""
    from app.services.painel_observador import montar_painel

    return render_template(
        'pedidos/painel_observador.html', painel=montar_painel())


@pedidos_bp.route('/consulta')
@login_required
@consulta_pedidos_required
def consulta():
    """Central multicanal sem qualquer acao operacional.

    O papel observador so chega a esta rota pelo gate global. Admin/owner
    tambem podem abri-la para conferir exatamente a experiencia concedida.
    """
    from app.utils import normalizar_busca

    canal = (request.args.get('canal') or 'todos').strip().lower()
    if canal not in {'todos', 'loja', 'site', 'b2b', 'manual'}:
        canal = 'todos'
    periodo = (request.args.get('periodo') or '30').strip().lower()
    if periodo not in {'7', '30', '90', 'todos'}:
        periodo = '30'
    status = (request.args.get('status') or '').strip().lower()
    busca = normalizar_busca((request.args.get('q') or '').strip())

    registros = []
    limite_canal = 150

    if canal in {'todos', 'loja'}:
        pedidos_loja = (PedidoLoja.query.options(
            joinedload(PedidoLoja.loja), selectinload(PedidoLoja.itens))
            .order_by(PedidoLoja.criado_em.desc()).limit(limite_canal).all())
        for p in pedidos_loja:
            registros.append({
                'canal': 'loja', 'canal_label': 'Loja',
                'referencia': f'LOJA-{p.id}',
                'destino': p.loja.nome if p.loja else 'Loja',
                'subtitulo': 'Pedido interno da loja',
                'criado_em': p.criado_em, 'entrega': p.data_entrega,
                'status': p.status or 'sem_status', 'valor': None,
                'observacao': p.observacao,
                'itens': [_item_consulta(it.nome_item, it.quantidade)
                          for it in p.itens],
            })

    if canal in {'todos', 'site'}:
        pedidos_site = (PedidoOnline.query.options(
            joinedload(PedidoOnline.loja_retirada),
            selectinload(PedidoOnline.itens))
            .order_by(PedidoOnline.criado_em.desc()).limit(limite_canal).all())
        for p in pedidos_site:
            modo = (p.modo_entrega or 'site').replace('_', ' ')
            if p.loja_retirada:
                modo += f' · {p.loja_retirada.nome}'
            registros.append({
                'canal': 'site', 'canal_label': 'Site',
                'referencia': p.codigo or f'SITE-{p.id}',
                'destino': p.nome_cliente,
                'subtitulo': modo, 'criado_em': p.criado_em,
                'entrega': p.data_entrega, 'status': p.status or 'sem_status',
                'valor': p.valor_total, 'observacao': p.cartinha,
                'itens': [_item_consulta(it.nome, it.quantidade)
                          for it in p.itens],
            })

    if canal in {'todos', 'b2b'}:
        vendas_b2b = (VendaB2B.query.options(
            joinedload(VendaB2B.cliente), selectinload(VendaB2B.itens))
            .order_by(VendaB2B.criado_em.desc()).limit(limite_canal).all())
        for p in vendas_b2b:
            situacao = ('cancelada' if p.status == 'cancelada'
                        else (p.status_entrega or p.status or 'sem_status'))
            registros.append({
                'canal': 'b2b', 'canal_label': 'B2B',
                'referencia': f'B2B-{p.id}', 'destino': p.cliente_display,
                'subtitulo': 'Venda para empresa', 'criado_em': p.criado_em,
                'entrega': p.data_entrega, 'status': situacao,
                'valor': p.valor_total, 'observacao': p.observacao,
                'itens': [_item_consulta(it.nome_item, it.quantidade)
                          for it in p.itens],
            })

    if canal in {'todos', 'manual'}:
        pedidos_manuais = (PedidoLocal.query.options(
            selectinload(PedidoLocal.itens))
            .order_by(PedidoLocal.criado_em.desc()).limit(limite_canal).all())
        for p in pedidos_manuais:
            registros.append({
                'canal': 'manual', 'canal_label': 'Manual',
                'referencia': p.code or f'MANUAL-{p.id}',
                'destino': p.destinatario, 'subtitulo': p.periodo or 'Agendado',
                'criado_em': p.criado_em, 'entrega': p.data_entrega,
                'status': 'agendado', 'valor': p.total,
                'observacao': p.observacao,
                'itens': [_item_consulta(it.nome, it.quantidade)
                          for it in p.itens],
            })

    if periodo != 'todos':
        corte = hoje_brt() - timedelta(days=int(periodo))
        registros = [r for r in registros
                     if ((r['criado_em'] and r['criado_em'].date() >= corte)
                         or (r['entrega'] and r['entrega'] >= hoje_brt()))]
    status_opcoes = sorted({r['status'] for r in registros})
    if status:
        registros = [r for r in registros if r['status'].lower() == status]
    if busca:
        def _texto(r):
            partes = [r['referencia'], r['destino'], r['canal_label'],
                      r['status'], r.get('observacao') or '']
            partes.extend(it['nome'] for it in r['itens'])
            return normalizar_busca(' '.join(str(x or '') for x in partes))
        registros = [r for r in registros if busca in _texto(r)]

    registros.sort(
        key=lambda r: (r['criado_em'].isoformat() if r['criado_em'] else ''),
        reverse=True)
    registros = registros[:300]
    # Totais globais deixam as abas honestas mesmo quando uma delas esta
    # selecionada (sem isso as outras apareciam como zero por nao terem sido
    # carregadas naquele request).
    contagens = {
        'loja': PedidoLoja.query.count(),
        'site': PedidoOnline.query.count(),
        'b2b': VendaB2B.query.count(),
        'manual': PedidoLocal.query.count(),
    }
    return render_template(
        'pedidos/consulta.html', pedidos=registros, canal_atual=canal,
        periodo_atual=periodo, status_atual=status, busca=busca,
        contagens=contagens, status_opcoes=status_opcoes)


@pedidos_bp.route('/buscar-itens.json')
@login_required
@pedidos_required
def buscar_itens():
    """Typeahead para novo pedido: receitas + produtos (ativos) + matérias-primas.
    Acento-insensível, multi-termo, mínimo 2 caracteres.
    Retorna formato compatível com _parse_item_id: r_<id>, p_<id>, mp_<id>."""

    from app.utils import normalizar_busca

    q = normalizar_busca((request.args.get('q') or '').strip())
    if len(q) < 2:
        return jsonify(itens=[])
    termos = q.split()

    def _casa(nome):
        n = normalizar_busca(nome)
        return all(t in n for t in termos)

    out = []
    # ativas(): receita arquivada nao pode entrar em pedido NOVO (varredura
    # 19/07/2026 — o typeahead era o unico picker do arquivo sem o filtro).
    # em_gramas: item medido em g/ml (granola/iogurte "Produção - *") — a
    # tela avisa quando a quantidade parece POTES (caso 18/08/2026, relatorio
    # inflado ~1000x). So aviso; nada bloqueia.
    # lote: só de item em g/ml — o form recusa quantidade que não seja
    # múltiplo (iogurte 3000/granola 5000, dono 18/08/2026) e o JS avisa
    # antes do POST. Receita em unidades fica lote 0 (lá o lote_pedido só
    # arredonda sugestão, nunca trava — croissant 45 segue válido).
    out += [{'id': f'r_{r.id}', 'nome': r.nome,
             'em_gramas': r.medida_em_gramas,
             'lote': int(r.lote_pedido or 0) if r.medida_em_gramas else 0}
            for r in Receita.ativas().order_by(Receita.nome).all()
            if _casa(r.nome)]
    out += [{'id': f'p_{p.id}', 'nome': p.nome, 'em_gramas': False}
            for p in Produto.query.filter_by(ativo=True).order_by(Produto.nome).all()
            if _casa(p.nome)]
    out += [{'id': f'mp_{m.id}', 'nome': m.nome,
             'em_gramas': (m.unidade or '').strip().lower() in ('g', 'ml', 'kg', 'l')}
            for m in _mps_pediveis().all() if _casa(m.nome)]
    return jsonify(itens=out[:50])


@pedidos_bp.route('/contagem-dia-site')
@login_required
@pedidos_required
def contagem_dia_site():
    """Pra o dia X, mostra TUDO que tem que sair de producao por causa de
    pedidos do site (PedidoOnline). Cestas sao DESEMPACOTADAS — Family Box ja
    aparece como N croissants + N sourdoughs + ... Decisao do dono 22/06/2026:
    o operacional precisava saber quanto produzir por dia, considerando o
    que vai dentro das cestas."""
    from datetime import date as _date

    from app.services import loja_online_vendas
    from app.utils import hoje
    data_str = (request.args.get('data') or '').strip()
    try:
        alvo = _date.fromisoformat(data_str) if data_str else hoje()
    except ValueError:
        alvo = hoje()
    itens = loja_online_vendas.contagem_para_dia(alvo)
    return render_template('pedidos/contagem_dia_site.html',
                           itens=itens, data=alvo,
                           data_str=alvo.isoformat())


@pedidos_bp.route('/')
@login_required
@pedidos_required
def lista():
    from sqlalchemy import func

    from app.constants import STATUS_PEDIDO_ABAS

    loja_id = _loja_do_usuario()
    filtro_loja_arg = None
    if not loja_id:
        filtro_loja_arg = request.args.get('loja')

    # Aba selecionada (grupo de status). Default: pendentes.
    grupos = {slug: sts for slug, _, sts in STATUS_PEDIDO_ABAS}
    aba = request.args.get('aba', 'pendentes')
    if aba not in grupos:
        aba = 'pendentes'
    status_da_aba = grupos[aba]

    def _aplica_loja(q):
        if loja_id:
            return q.filter(PedidoLoja.loja_id == loja_id)
        if filtro_loja_arg:
            try:
                return q.filter(PedidoLoja.loja_id == int(filtro_loja_arg))
            except (TypeError, ValueError):
                pass
        return q

    # Contagem por status (1 query) → soma por grupo de aba pros badges.
    cont_q = _aplica_loja(db.session.query(PedidoLoja.status, func.count()))
    cont_por_status = dict(cont_q.group_by(PedidoLoja.status).all())
    contagens = {slug: sum(cont_por_status.get(s, 0) for s in sts)
                 for slug, _, sts in STATUS_PEDIDO_ABAS}

    # Contagem POR LOJA dentro da aba atual — badges da linha de abas de
    # loja (pedido do dono 16/07/2026: "aba todas as lojas / ribeiro / etc"
    # abaixo das abas de status). Sem o filtro de loja de propósito: mostra
    # a distribuição pra guiar o clique.
    cont_lojas = dict(
        db.session.query(PedidoLoja.loja_id, func.count())
        .filter(PedidoLoja.status.in_(status_da_aba))
        .group_by(PedidoLoja.loja_id).all()) if not loja_id else {}

    query = PedidoLoja.query.options(
        joinedload(PedidoLoja.loja),
        selectinload(PedidoLoja.itens),
        selectinload(PedidoLoja.qrcodes),
    ).filter(PedidoLoja.status.in_(status_da_aba)).order_by(
        PedidoLoja.criado_em.desc())
    query = _aplica_loja(query)
    pedidos = query.limit(100).all()
    # Constroi mapa pedido_id → nome do motorista (do QR de saida usado)
    motoristas = {}
    for p in pedidos:
        for qr in p.qrcodes:
            if qr.tipo == 'saida' and qr.usado_por_descricao:
                # formato: 'driver:Nome' → extrai o nome
                motoristas[p.id] = qr.usado_por_descricao.replace('driver:', '', 1)
                break
    lojas = _lojas_operacionais()
    return render_template('pedidos/lista.html', pedidos=pedidos, lojas=lojas,
                           filtro_loja=request.args.get('loja', ''),
                           motoristas=motoristas,
                           abas=STATUS_PEDIDO_ABAS, aba_atual=aba,
                           contagens=contagens, cont_lojas=cont_lojas)


@pedidos_bp.route('/novo', methods=['GET', 'POST'])
@login_required
@pedidos_required
def novo():
    loja_id = _loja_do_usuario()
    # Admin e gerente escolhem qualquer loja no form; demais papeis sao
    # forcados pra propria loja e precisam ter uma vinculada.
    pode_qualquer_loja = current_user.is_admin() or current_user.is_gerente()
    if not pode_qualquer_loja and not current_user.loja_id:
        flash('Vincule sua conta a uma loja para criar pedidos.', 'warning')
        return redirect(url_for('pedidos.lista'))

    amanha = hoje_brt() + timedelta(days=1)
    # Entrega no MESMO dia liberada pra TODOS os papeis (decisao do dono
    # 15/07/2026 — antes so admin; funcionario da loja precisava esperar
    # amanha). Passado continua bloqueado. O default do form segue amanha.
    data_min = hoje_brt()

    if request.method == 'POST':
        try:
            sel_loja = (int(request.form.get('loja_id', 0)) if pode_qualquer_loja
                        else (loja_id or current_user.loja_id))
        except (TypeError, ValueError):
            sel_loja = 0
        if not pode_qualquer_loja and sel_loja != current_user.loja_id:
            abort(403)
        # Loja precisa existir e estar ativa
        if not sel_loja or not Loja.query.filter_by(id=sel_loja, ativa=True).first():
            flash('Selecione uma loja valida.', 'warning')
            lojas = _lojas_operacionais()
            return render_template('pedidos/novo.html', lojas=lojas,
                                   amanha=amanha, data_min=data_min,
                                   loja_id=loja_id)

        data_str = request.form.get('data_entrega', '')
        obs = request.form.get('observacao', '').strip()

        try:
            data_entrega = datetime.strptime(data_str, '%Y-%m-%d').date()
        except ValueError:
            data_entrega = amanha

        if data_entrega < data_min:
            flash(f'A data de entrega deve ser a partir de {data_min.strftime("%d/%m")}.', 'warning')
            lojas = _lojas_operacionais()
            return render_template('pedidos/novo.html', lojas=lojas,
                                   amanha=amanha, data_min=data_min,
                                   loja_id=loja_id)

        # Corte do fim do dia (dono 10/08/2026): pedido pra AMANHÃ fecha na HORA_CORTE
        # (pré-preparo do padeiro). Admin passa com aviso; loja é barrada.
        # Checado ANTES do merge — criar "novo" pode virar mesclar num
        # pedido de amanhã já existente.
        from app.services.pedido_corte import bloqueio_do_corte
        bloqueado, aviso_corte = bloqueio_do_corte([data_entrega],
                                                   user=current_user)
        if bloqueado:
            flash(aviso_corte, 'warning')
            lojas = _lojas_operacionais()
            return render_template('pedidos/novo.html', lojas=lojas,
                                   amanha=amanha, data_min=data_min,
                                   loja_id=loja_id)
        if aviso_corte:
            flash(aviso_corte, 'warning')

        # Monta a lista de itens normalizada antes de decidir merge vs novo.
        ids = request.form.getlist('item_id[]')
        qtds = request.form.getlist('item_qtd[]')
        notas = request.form.getlist('item_obs[]')
        estados = request.form.getlist('item_estado[]')
        itens_norm = []
        for i in range(len(ids)):
            if not ids[i] or not qtds[i]:
                continue
            tipo, item_id = _parse_item_id(ids[i])
            if not tipo:
                continue
            try:
                qtd = int(qtds[i])
            except (TypeError, ValueError):
                continue
            if qtd <= 0:
                continue
            est = (estados[i].strip().lower() if i < len(estados) else '') or None
            if est not in (None, 'backup', 'assado'):
                est = None
            itens_norm.append({
                'receita_id': item_id if tipo == 'receita' else None,
                'produto_id': item_id if tipo == 'produto' else None,
                'materia_prima_id': item_id if tipo == 'mp' else None,
                'quantidade': qtd,
                'observacao': notas[i].strip() if i < len(notas) else None,
                'estado': est,
            })

        # Sem o <select required> antigo, o form pode chegar sem nenhum item
        # valido (texto digitado sem escolher). Barra aqui pra nao criar pedido vazio.
        if not itens_norm:
            flash('Adicione ao menos um item ao pedido.', 'warning')
            lojas = _lojas_operacionais()
            return render_template('pedidos/novo.html', lojas=lojas,
                                   amanha=amanha, data_min=data_min, loja_id=loja_id)

        # MP só entra se liberada pra pedido de loja (checkbox no Banco de
        # MPs — decisão do dono 07/07/2026). O typeahead já não oferece as
        # bloqueadas; isto barra POST direto/aba desatualizada.
        bloqueadas = _mps_nao_pediveis(itens_norm)
        if bloqueadas:
            flash('Matéria(s)-prima(s) não liberada(s) pra pedido de loja: '
                  + ', '.join(bloqueadas) + '. Um admin pode liberar no '
                  'Banco de MPs (checkbox "sugerir pedido loja").', 'warning')
            lojas = _lojas_operacionais()
            return render_template('pedidos/novo.html', lojas=lojas,
                                   amanha=amanha, data_min=data_min,
                                   loja_id=loja_id)

        # Item em g/ml com lote definido só aceita MÚLTIPLO do lote
        # (iogurte 3000 / granola 5000 — dono 18/08/2026, caso "potes").
        from app.services.pedido_lote import violacoes_por_ids
        fora_do_lote = violacoes_por_ids(itens_norm)
        if fora_do_lote:
            for msg in fora_do_lote:
                flash(msg, 'warning')
            lojas = _lojas_operacionais()
            return render_template('pedidos/novo.html', lojas=lojas,
                                   amanha=amanha, data_min=data_min,
                                   loja_id=loja_id)

        try:
            from app.services.pedido_merge import (
                absorver_rascunho_automatico,
                adotar_rascunho_automatico,
                mesclar_itens,
                pedido_aberto_para_merge,
                rascunho_automatico_aberto,
            )
            # Ja existe pedido aberto da loja nessa data? Junta nele em vez de duplicar.
            alvo = pedido_aberto_para_merge(sel_loja, data_entrega, 'confirmado')
            if alvo:
                mesclar_itens(alvo, itens_norm, modificado_por_id=current_user.id)
                if obs:
                    alvo.observacao = ((alvo.observacao + ' | ') if alvo.observacao else '') + obs
                # Colisao com o cron de auto-pedidos: se um rascunho
                # automatico tambem cobre o dia, ele virou redundancia —
                # somar os itens dele seria demanda em dobro.
                absorvido = absorver_rascunho_automatico(
                    sel_loja, data_entrega, current_user.id)
                db.session.commit()
                flash(f'Itens adicionados ao pedido #{alvo.id} — ja existia '
                      'para esta loja nesta data.', 'success')
                if absorvido is not None:
                    flash(f'O rascunho automático #{absorvido.id} do mesmo dia '
                          'foi cancelado — o pedido da loja manda.', 'info')
                return redirect(url_for('pedidos.detalhe', id=alvo.id))

            # Dia coberto pelo CRON de auto-pedidos (10/08/2026): adota o
            # rascunho em vez de criar um segundo pedido (2 pedidos no mesmo
            # dia = producao em dobro na ordem enviada no corte). Item citado
            # SUBSTITUI a quantidade do motor; item do motor nao citado FICA.
            rascunho = rascunho_automatico_aberto(sel_loja, data_entrega)
            if rascunho is not None:
                res_adote = adotar_rascunho_automatico(
                    rascunho, itens_norm, current_user.id, observacao=obs)
                db.session.commit()
                flash(f'Pedido #{rascunho.id} confirmado a partir da sugestão '
                      'automática do dia: suas quantidades substituíram as '
                      'sugeridas.', 'success')
                if res_adote['mantidos']:
                    flash(f'{res_adote["mantidos"]} item(ns) da sugestão '
                          'automática que você não citou foram MANTIDOS no '
                          'pedido — revise e ajuste se não quiser.', 'warning')
                return redirect(url_for('pedidos.detalhe', id=rascunho.id))

            pedido = PedidoLoja(
                loja_id=sel_loja,
                data_entrega=data_entrega,
                observacao=obs or None,
                criado_por=current_user.id,
            )
            db.session.add(pedido)
            db.session.flush()
            for it in itens_norm:
                db.session.add(PedidoItem(pedido_id=pedido.id, **it))
            db.session.commit()
        except Exception as exc:  # noqa: BLE001
            db.session.rollback()
            current_app.logger.exception('Falha ao criar pedido')
            flash(f'Erro ao criar pedido: {exc}', 'danger')
            return redirect(url_for('pedidos.novo'))
        # Alerta Slack se for emergencia (criado hoje pra entrega hoje)
        try:
            from app.services.slack_resumos import alertar_pedido_emergencia
            alertar_pedido_emergencia(pedido)
        except Exception:  # noqa: BLE001
            current_app.logger.exception('Alerta emergencia falhou')
        flash('Pedido criado!', 'success')
        return redirect(url_for('pedidos.detalhe', id=pedido.id))

    lojas = _lojas_operacionais()
    return render_template('pedidos/novo.html', lojas=lojas,
                           amanha=amanha, data_min=data_min, loja_id=loja_id)


@pedidos_bp.route('/<int:id>/editar', methods=['GET', 'POST'])
@login_required
@operacional_pedido_required
def editar(id):
    """Edita pedido em status pendente/confirmado.

    Permite mexer em data_entrega, observacao do pedido, e itens
    (qtd/estado/obs/adicionar/remover). NAO toca em loja_id (muda rota
    fisica, melhor cancelar+recriar) nem em status/driver_id. Itens vao
    via REPLACE total — DELETE + INSERT da lista nova."""
    from app.constants import STATUS_PEDIDO_EDITAVEIS
    pedido = PedidoLoja.query.get_or_404(id)
    if pedido.status not in STATUS_PEDIDO_EDITAVEIS:
        flash(f'Pedido {pedido.status} nao pode ser editado. Cancele e recrie.', 'warning')
        return redirect(url_for('pedidos.detalhe', id=id))

    if request.method == 'POST':
        # Mesmo dia liberado pra todos (15/07/2026); passado segue bloqueado.
        data_min = hoje_brt()
        data_str = request.form.get('data_entrega', '')
        obs = request.form.get('observacao', '').strip()
        try:
            data_entrega = datetime.strptime(data_str, '%Y-%m-%d').date()
        except ValueError:
            data_entrega = pedido.data_entrega
        if data_entrega < data_min:
            flash(f'A data de entrega deve ser a partir de {data_min.strftime("%d/%m")}.', 'warning')
            return redirect(url_for('pedidos.editar', id=id))

        # Corte do fim do dia (dono 10/08/2026): olha a data ATUAL e a NOVA —
        # mover um pedido PRA amanhã (ou tirar de amanhã) depois do corte
        # muda o pré-preparo do padeiro do mesmo jeito.
        from app.services.pedido_corte import bloqueio_do_corte
        bloqueado, aviso_corte = bloqueio_do_corte(
            [pedido.data_entrega, data_entrega], user=current_user)
        if bloqueado:
            flash(aviso_corte, 'warning')
            return redirect(url_for('pedidos.detalhe', id=id))
        if aviso_corte:
            flash(aviso_corte, 'warning')

        # MP NOVA só entra se liberada pra pedido de loja (checkbox no Banco
        # de MPs). MP que JÁ estava no pedido continua válida — sem isso,
        # desmarcar a flag travaria a edição de pedidos antigos legítimos.
        mp_ids_antes = {it.materia_prima_id for it in pedido.itens
                        if it.materia_prima_id}
        candidatos = []
        for v in request.form.getlist('item_id[]'):
            t, iid = _parse_item_id(v)
            if t == 'mp' and iid not in mp_ids_antes:
                candidatos.append({'materia_prima_id': iid})
        bloqueadas = _mps_nao_pediveis(candidatos)
        if bloqueadas:
            flash('Matéria(s)-prima(s) não liberada(s) pra pedido de loja: '
                  + ', '.join(bloqueadas) + '. Um admin pode liberar no '
                  'Banco de MPs (checkbox "sugerir pedido loja").', 'warning')
            return redirect(url_for('pedidos.editar', id=id))

        # Item em g/ml com lote definido só aceita MÚLTIPLO do lote
        # (iogurte 3000 / granola 5000 — dono 18/08/2026, caso "potes").
        # Validado ANTES do REPLACE dos itens; vale também pro item que já
        # estava no pedido (o dono escolheu SEM grandfather: 9360 antigo
        # tem que virar 9000/12000 ao editar).
        from app.services.pedido_lote import violacoes_por_ids
        itens_lote = []
        _ids = request.form.getlist('item_id[]')
        _qtds = request.form.getlist('item_qtd[]')
        for i in range(len(_ids)):
            t, iid = _parse_item_id(_ids[i])
            if t != 'receita':
                continue
            try:
                q = int(_qtds[i]) if i < len(_qtds) else 0
            except (TypeError, ValueError):
                continue
            itens_lote.append({'receita_id': iid, 'quantidade': q})
        fora_do_lote = violacoes_por_ids(itens_lote)
        if fora_do_lote:
            for msg in fora_do_lote:
                flash(msg, 'warning')
            return redirect(url_for('pedidos.editar', id=id))

        try:
            data_mudou = (data_entrega != pedido.data_entrega)
            pedido.data_entrega = data_entrega
            pedido.observacao = obs or None
            pedido.modificado_em = agora()
            pedido.modificado_por_id = current_user.id
            if data_mudou:
                # Mover o pedido pra um dia que o cron de auto-pedidos já
                # cobriu deixaria rascunho + pedido humano no mesmo dia
                # (demanda em dobro) — o rascunho vira redundância e cai.
                from app.services.pedido_merge import (
                    absorver_rascunho_automatico,
                )
                absorvido = absorver_rascunho_automatico(
                    pedido.loja_id, data_entrega, current_user.id,
                    excluir_id=pedido.id)
                if absorvido is not None:
                    flash(f'O rascunho automático #{absorvido.id} do dia de '
                          'destino foi cancelado — o seu pedido manda.',
                          'info')

            # REPLACE total dos itens. Deletar VIA ORM (não Query.delete em
            # massa) pra disparar o cascade 'all, delete-orphan' das fotos de
            # conferência (pedido_item_foto). O bulk delete pulava o cascade e
            # batia direto na FK (que não tem ON DELETE CASCADE), quebrando a
            # edição de qualquer pedido que já tivesse foto de item.
            for _it in list(pedido.itens):
                db.session.delete(_it)
            db.session.flush()

            ids = request.form.getlist('item_id[]')
            qtds = request.form.getlist('item_qtd[]')
            notas = request.form.getlist('item_obs[]')
            estados = request.form.getlist('item_estado[]')

            salvos = 0
            for i in range(len(ids)):
                if not ids[i] or not qtds[i]:
                    continue
                tipo, item_id = _parse_item_id(ids[i])
                if not tipo:
                    continue
                try:
                    qtd = int(qtds[i])
                except (TypeError, ValueError):
                    continue
                if qtd <= 0:
                    continue
                est = (estados[i].strip().lower()
                       if i < len(estados) else '') or None
                if est not in (None, 'backup', 'assado'):
                    est = None
                item = PedidoItem(
                    pedido_id=pedido.id,
                    receita_id=item_id if tipo == 'receita' else None,
                    produto_id=item_id if tipo == 'produto' else None,
                    materia_prima_id=item_id if tipo == 'mp' else None,
                    quantidade=qtd,
                    observacao=notas[i].strip() if i < len(notas) else None,
                    estado=est,
                )
                db.session.add(item)
                salvos += 1

            if salvos == 0:
                db.session.rollback()
                flash('Pedido precisa ter pelo menos 1 item.', 'warning')
                return redirect(url_for('pedidos.editar', id=id))

            db.session.commit()
        except Exception as exc:  # noqa: BLE001
            db.session.rollback()
            current_app.logger.exception('Falha ao editar pedido')
            flash(f'Erro ao editar pedido: {exc}', 'danger')
            return redirect(url_for('pedidos.editar', id=id))

        flash('Pedido atualizado.', 'success')
        return redirect(url_for('pedidos.detalhe', id=pedido.id))

    # A tela usa typeahead (/pedidos/buscar-itens.json) em vez de <select> com
    # o catálogo inteiro — os itens existentes já vêm pré-preenchidos pela FK,
    # então não é preciso carregar receitas/produtos/MPs aqui. O grandfather de
    # MP bloqueada segue vivo no POST (candidatos = só ids que NÃO estavam no
    # pedido), preservando itens antigos legítimos.
    amanha = hoje_brt() + timedelta(days=1)
    data_min = hoje_brt()   # mesmo dia liberado pra todos (15/07/2026)
    return render_template('pedidos/editar.html', pedido=pedido,
                           amanha=amanha, data_min=data_min)


@pedidos_bp.route('/<int:id>')
@login_required
@gerente_required
def detalhe(id):
    from app.models import Driver, PedidoItemFoto
    pedido = PedidoLoja.query.get_or_404(id)
    loja_id = _loja_do_usuario()
    if loja_id and pedido.loja_id != loja_id:
        abort(403)
    drivers = Driver.query.filter_by(ativo=True).order_by(Driver.nome).all()

    # Fotos de conferencia (saida = industria/motorista, entrega = loja),
    # por SKU. Agrupa por etapa pra exibir no detalhe.
    item_ids = [it.id for it in pedido.itens]
    fotos_conf = {'saida': [], 'entrega': []}
    if item_ids:
        for f in (PedidoItemFoto.query
                  .filter(PedidoItemFoto.pedido_item_id.in_(item_ids))
                  .all()):
            if f.etapa in fotos_conf:
                fotos_conf[f.etapa].append(f)
    return render_template('pedidos/detalhe.html', pedido=pedido,
                            drivers=drivers, fotos_conf=fotos_conf)


@pedidos_bp.route('/<int:id>/confirmar', methods=['POST'])
@login_required
@operacional_pedido_required
def confirmar(id):
    pedido = PedidoLoja.query.get_or_404(id)
    pedido.status = 'confirmado'
    # Carimbo do gesto humano (10/08/2026, auto-pedidos): confirmar um
    # rascunho automático SEM mexer em item é revisão — o carimbo protege o
    # pedido do re-sync do cron (a observação do rascunho pede exatamente
    # "revisar e confirmar").
    pedido.modificado_em = agora()
    pedido.modificado_por_id = current_user.id
    db.session.commit()
    flash('Pedido confirmado.', 'success')
    return redirect(url_for('pedidos.detalhe', id=id))


@pedidos_bp.route('/<int:id>/separar', methods=['POST'])
@login_required
@operacional_pedido_required
def separar(id):
    pedido = PedidoLoja.query.get_or_404(id)
    if pedido.status not in ('pendente', 'confirmado'):
        flash('Pedido deve estar pendente ou confirmado para ser separado.', 'warning')
        return redirect(url_for('pedidos.detalhe', id=id))
    pedido.status = 'separado'
    db.session.commit()
    flash('Pedido marcado como separado. Estoque ainda nao foi baixado.', 'success')
    return redirect(url_for('pedidos.detalhe', id=id))


@pedidos_bp.route('/<int:id>/handshake-audit')
@login_required
@admin_required
def handshake_audit(id):
    """Mostra historico de tentativas de handshake QR pra este pedido.
    Util pra diagnosticar entregas que travaram."""
    from app.models import HandshakeAudit
    pedido = PedidoLoja.query.get_or_404(id)
    eventos = (HandshakeAudit.query
               .filter_by(pedido_id=pedido.id)
               .order_by(HandshakeAudit.momento.desc()).all())
    return render_template('pedidos/handshake_audit.html',
                            pedido=pedido, eventos=eventos)


# Rota 'forcar-entrega' REMOVIDA em 2026-06-10 por ordem do dono: entrega
# passa SEMPRE pelo handshake QR. Historico antigo preservado no
# HandshakeAudit (etapa='forcar_entrega').


@pedidos_bp.route('/<int:id>/enviar', methods=['POST'])
@login_required
@operacional_pedido_required
def enviar(id):
    pedido = PedidoLoja.query.get_or_404(id)
    try:
        ok, msg = _executar_envio_pedido(pedido, current_user, ref_extra=None)
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        current_app.logger.exception('Falha ao enviar pedido %s', id)
        flash(f'Erro ao processar saída do pedido: {exc}. Nada foi alterado.', 'danger')
        return redirect(url_for('pedidos.detalhe', id=id))
    flash(msg, 'success' if ok else 'warning')
    return redirect(url_for('pedidos.detalhe', id=id))


def _executar_envio_pedido(pedido, user, ref_extra=None):
    """Baixa estoque da industria + status separado → em_transporte.

    Chamavel por (1) rota /pedidos/<id>/enviar (admin click) ou (2)
    handshake QR (motorista escaneia + digita PIN). Levanta Exception em
    falha pra caller fazer rollback. `ref_extra` adiciona texto na
    referencia do movimento (ex: 'via QR / motorista Joao').
    """
    if pedido.status != 'separado':
        return False, f'Pedido precisa estar separado (atual: {pedido.status}).'

    # Motor único de baixa (03/07/2026): get-or-create da linha + movimento
    # com a quantidade REAL + falta registrada como saida_pedido_sem_estoque.
    # Item sem linha de estoque NÃO é mais pulado em silêncio.
    from app.services.pedido_estoque import baixar_industria_pedido
    faltas = baixar_industria_pedido(
        pedido, getattr(user, 'id', None), ref_extra=ref_extra)

    pedido.status = 'em_transporte'
    db.session.commit()
    if faltas:
        quais = '; '.join(f"{f['item']}: pedido {f['pedido']:g}, "
                          f"baixado {f['baixado']:g}" for f in faltas)
        return True, ('Pedido em transporte. ATENÇÃO — saiu com estoque '
                      f'insuficiente na indústria ({quais}). A falta ficou '
                      'registrada no histórico (saida_pedido_sem_estoque).')
    return True, 'Pedido em transporte. Estoque da industria baixado.'


@pedidos_bp.route('/<int:id>/atribuir-motorista', methods=['POST'])
@login_required
@operacional_pedido_required
def atribuir_motorista(id):
    """Define qual motorista vai pegar o pedido. Obrigatorio antes do QR.

    Quando atribuido, dispara envio WhatsApp pro motorista com magic link
    do dia (cria se nao existir) + descricao do pedido."""
    from app.models import Driver
    pedido = PedidoLoja.query.get_or_404(id)
    drv_id = request.form.get('driver_id', type=int)
    if not drv_id:
        flash('Selecione um motorista.', 'warning')
        return redirect(url_for('pedidos.detalhe', id=id))
    drv = Driver.query.get(drv_id)
    if not drv or not drv.ativo:
        flash('Motorista inativo ou inexistente.', 'danger')
        return redirect(url_for('pedidos.detalhe', id=id))
    pedido.driver_id = drv.id
    db.session.commit()
    flash(f'Motorista {drv.nome} atribuido ao pedido #{pedido.id}.', 'success')
    # Envia WhatsApp on-demand
    try:
        from app.services import driver_magic
        ok, msg, _ = driver_magic.notificar_pedido(drv, pedido)
        if ok:
            flash(f'WhatsApp enviado pra {drv.nome}.', 'info')
        else:
            flash(f'Atribuicao OK mas WhatsApp falhou: {msg}', 'warning')
    except Exception as exc:  # noqa: BLE001
        flash(f'WhatsApp erro: {exc}', 'warning')
    return redirect(url_for('pedidos.detalhe', id=id))


@pedidos_bp.route('/<int:id>/qr-saida')
@login_required
@operacional_pedido_required
def qr_saida(id):
    """QR de saida pra industria mostrar pro motorista.

    Producao so exibe o QR. Conferencia com foto eh feita pelo motorista
    na pagina do handshake apos escanear (ver handshake.routes).

    Exige motorista atribuido em `pedido.driver_id` — sem isso, redireciona
    pra ficha do pedido. Handshake da saida tambem so aceita o PIN desse
    motorista especifico (nenhum outro pode escanear)."""
    import secrets
    from datetime import timedelta

    from app.services.qrcode_svc import gerar_png_data_url

    pedido = PedidoLoja.query.get_or_404(id)
    if pedido.status != 'separado':
        flash(f'Pedido precisa estar separado (atual: {pedido.status}).', 'warning')
        return redirect(url_for('pedidos.detalhe', id=id))
    if not pedido.driver_id:
        flash('Atribua o motorista responsavel antes de gerar o QR.', 'warning')
        return redirect(url_for('pedidos.detalhe', id=id))
    qr = (PedidoQRCode.query
          .filter_by(pedido_id=pedido.id, tipo='saida', usado_em=None)
          .filter(PedidoQRCode.expira_em > agora())
          .order_by(PedidoQRCode.criado_em.desc()).first())
    if not qr:
        qr = PedidoQRCode(
            token=secrets.token_urlsafe(24),
            pedido_id=pedido.id, tipo='saida',
            criado_por_id=current_user.id,
            expira_em=agora() + timedelta(hours=2),
        )
        db.session.add(qr)
        db.session.commit()
    url = url_for('handshake.handshake', token=qr.token, _external=True)
    qr_png = gerar_png_data_url(url)
    return render_template('pedidos/qr_saida.html',
                            pedido=pedido, qr=qr, url=url, qr_png=qr_png)


@pedidos_bp.route('/<int:id>/receber', methods=['POST'])
@login_required
def receber(id):
    pedido = PedidoLoja.query.get_or_404(id)
    loja_id = _loja_do_usuario()
    if loja_id and pedido.loja_id != loja_id:
        abort(403)

    recebidos = {}
    for key, val in request.form.items():
        if key.startswith('recebido_') and val.strip():
            try:
                recebidos[int(key[len('recebido_'):])] = max(0, int(val))
            except ValueError:
                continue

    fotos_payload = []
    for f in request.files.getlist('fotos'):
        if not f or not f.filename:
            continue
        content = f.read()
        if not content:
            continue
        fotos_payload.append({'imagem': content,
                              'mimetype': f.mimetype or 'image/jpeg'})

    try:
        ok, msg, divergencias = _executar_recebimento_pedido(
            pedido, current_user, recebidos_map=recebidos,
            fotos=fotos_payload,
        )
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        current_app.logger.exception('Falha ao receber pedido %s', id)
        flash(f'Erro ao processar recebimento: {exc}. Nada foi alterado.', 'danger')
        return redirect(url_for('pedidos.detalhe', id=id))

    if not ok:
        flash(msg, 'warning')
        return redirect(url_for('pedidos.detalhe', id=id))
    flash(msg, 'warning' if divergencias else 'success')
    return redirect(url_for('pedidos.detalhe', id=id))


def _executar_recebimento_pedido(pedido, user, recebidos_map=None, fotos=None,
                                  ref_extra=None):
    """Sobe estoque da loja + status em_transporte → entregue.

    recebidos_map: {item_id: qtd_recebida}. Itens omitidos assumem qtd
    igual a do pedido (sem divergencia). Usado pra preencher
    PedidoItem.quantidade_recebida + criar MovEstoqueLoja.
    fotos: lista de {imagem, mimetype} salvas como FotoRecebimento.
    ref_extra: texto extra nas movimentacoes (ex: 'via QR / loja X').

    Retorna (ok, msg, divergencias). Exception em caller faz rollback.
    """
    if pedido.status != 'em_transporte':
        return False, f'Pedido precisa estar em transporte (atual: {pedido.status}).', []

    recebidos_map = recebidos_map or {}
    fotos = fotos or []

    # REGRA (decisao do dono 13/06/2026): entrega exige PELO MENOS 1 foto de
    # comprovacao — nao se entrega "no escuro". Tres fontes contam como prova,
    # checadas nesta ordem pra NAO quebrar o caminho do QR:
    #   1. `fotos` novas deste recebimento manual (upload do admin/loja);
    #   2. PedidoItemFoto da conferencia de ENTREGA — o caminho QR: o motorista
    #      fotografa cada item antes do PIN, ja salvas quando este executor roda
    #      (o handshake NAO passa `fotos` aqui, as dele sao PedidoItemFoto);
    #   3. FotoRecebimento ja anexada antes (tentativa anterior).
    # Sem nenhuma das tres → recusa, e a rota mostra a mensagem no flash.
    if not fotos:
        from app.services.conferencia import fotos_presentes
        tem_conferencia = bool(fotos_presentes(pedido, 'entrega'))
        tem_recebimento = FotoRecebimento.query.filter_by(
            pedido_id=pedido.id).first() is not None
        if not tem_conferencia and not tem_recebimento:
            return False, ('Entrega exige pelo menos 1 foto de comprovação. '
                           'Anexe a foto do pedido recebido e confirme de novo.'), []

    divergencias = []

    for item in pedido.itens:
        qtd_rec = recebidos_map.get(item.id, item.quantidade)
        item.quantidade_recebida = qtd_rec
        if qtd_rec != item.quantidade:
            divergencias.append(f'{item.nome_item}: pedido {item.quantidade}, recebido {qtd_rec}')
        if qtd_rec <= 0:
            continue

        # Estoque de loja eh por PRODUTO (sem estado): o estado eh instrucao do
        # pedido (industria prepara fora do padrao), nao dimensao de estoque.
        # Soma sempre na linha unica do produto.
        from app.services.estoque_helpers import obter_linha_loja
        el = obter_linha_loja(
            pedido.loja_id,
            receita_id=item.receita_id,
            produto_id=item.produto_id,
            materia_prima_id=item.materia_prima_id,
            usuario_id=getattr(user, 'id', None),
        )
        el.quantidade = (el.quantidade or 0) + qtd_rec
        ref_div = ' (divergente)' if qtd_rec != item.quantidade else ''
        # Tag de estado fica so na referencia (auditoria), nao na linha.
        from app.constants import estado_label
        ref_estado = estado_label(item.estado)
        ref = f'Pedido #{pedido.id}{ref_div} {ref_estado}'.rstrip()
        if ref_extra:
            ref += f' ({ref_extra})'
        db.session.add(MovEstoqueLoja(
            estoque_loja_id=el.id, tipo='entrada_pedido',
            quantidade=qtd_rec,
            referencia=ref,
            usuario_id=getattr(user, 'id', None),
        ))

    pedido.status = 'entregue'
    # Momento REAL do recebimento (14/08/2026): o digest das 12:00 usa
    # `modificado_em` na janela de pendentes — sem o carimbo, pedido
    # recebido com atraso (>3 dias da data planejada) nunca seria avisado
    # (`data_entrega` e a data PLANEJADA, nao a efetiva). So o timestamp:
    # `modificado_por_id` fica como esta (semantica de protecao dos
    # auto-pedidos e outra decisao).
    pedido.modificado_em = agora()
    if divergencias:
        nota = 'Divergencias no recebimento: ' + '; '.join(divergencias)
        pedido.observacao = (pedido.observacao + ' | ' if pedido.observacao else '') + nota

    import time as _time

    from app.services import dropbox_storage
    from app.utils import comprimir_imagem
    for foto in fotos:
        url = None
        storage_path = None
        if dropbox_storage.disponivel() and foto.get('imagem'):
            try:
                comprimida = comprimir_imagem(foto['imagem'])
                path = (f'/recebimento/{pedido.id}/'
                        f'{int(_time.time() * 1000)}.jpg')
                info = dropbox_storage.upload_publico(
                    comprimida, path, mode='add', autorename=True)
                url = info['url']
                storage_path = info['storage_path']
            except (ValueError, RuntimeError):
                current_app.logger.exception(
                    'foto_recebimento dropbox falhou — fallback BLOB')
        db.session.add(FotoRecebimento(
            pedido_id=pedido.id,
            imagem=None if url else foto['imagem'],
            imagem_url=url,
            imagem_storage_path=storage_path,
            mimetype='image/jpeg' if url else foto.get('mimetype', 'image/jpeg'),
            enviada_por=getattr(user, 'id', None),
        ))

    db.session.commit()
    # O aviso pro WhatsApp do dono NAO sai mais aqui (14/08/2026, "esta
    # ficando flodado"): o digest das 12:00 (pedidos_notificacao.
    # enviar_digest_recebimentos, cron do seru_cron) varre os entregues
    # sem sentinela e manda UMA mensagem com todos.
    msg = ('Pedido recebido com divergencias. Detalhes salvos na observacao.'
           if divergencias else
           'Pedido recebido integralmente. Estoque da loja atualizado.')
    return True, msg, divergencias


@pedidos_bp.route('/foto/<int:foto_id>')
@login_required
def foto(foto_id):
    f = FotoRecebimento.query.get_or_404(foto_id)
    loja_id = _loja_do_usuario()
    if loja_id and f.pedido.loja_id != loja_id:
        abort(403)
    if f.imagem_url:
        return redirect(f.imagem_url, code=302)
    if f.imagem:
        return send_file(io.BytesIO(f.imagem), mimetype=f.mimetype or 'image/jpeg')
    abort(404)


@pedidos_bp.route('/conferencia-foto/<int:foto_id>')
@login_required
@gerente_required
def conferencia_foto(foto_id):
    """Serve foto de conferencia por SKU (PedidoItemFoto) pro detalhe do
    pedido. Gate por loja. Prioriza Dropbox; fallback BLOB legado."""
    from app.models import PedidoItemFoto
    f = PedidoItemFoto.query.get_or_404(foto_id)
    loja_id = _loja_do_usuario()
    pedido = f.pedido_item.pedido if f.pedido_item else None
    if loja_id and (not pedido or pedido.loja_id != loja_id):
        abort(403)
    if f.imagem_url:
        return redirect(f.imagem_url, code=302)
    if f.imagem:
        return send_file(io.BytesIO(f.imagem), mimetype=f.mimetype or 'image/jpeg')
    abort(404)


@pedidos_bp.route('/lojas/<int:loja_id>/precos', methods=['GET', 'POST'])
@login_required
@admin_required
def precos_loja(loja_id):
    loja = Loja.query.get_or_404(loja_id)
    receitas = Receita.ativas().order_by(Receita.categoria, Receita.nome).all()

    if request.method == 'POST':
        for r in receitas:
            val = (request.form.get(f'preco_{r.id}', '') or '').strip().replace(',', '.')
            existente = PrecoLojaReceita.query.filter_by(
                loja_id=loja_id, receita_id=r.id
            ).first()
            if not val:
                if existente:
                    db.session.delete(existente)
                continue
            try:
                preco = float(val)
            except ValueError:
                continue
            if preco <= 0:
                if existente:
                    db.session.delete(existente)
                continue
            if existente:
                existente.preco = preco
            else:
                db.session.add(PrecoLojaReceita(
                    loja_id=loja_id, receita_id=r.id, preco=preco
                ))
        db.session.commit()
        flash(f'Precos da loja {loja.nome} atualizados.', 'success')
        return redirect(url_for('pedidos.precos_loja', loja_id=loja_id))

    precos = {p.receita_id: p.preco for p in PrecoLojaReceita.query.filter_by(loja_id=loja_id).all()}
    lojas = _lojas_operacionais()
    return render_template('pedidos/precos_loja.html', loja=loja, receitas=receitas,
                           precos=precos, lojas=lojas)


@pedidos_bp.route('/relatorio')
@login_required
@gerente_required
def relatorio():
    hoje = hoje_brt()
    loja_id = request.args.get('loja', type=int)
    de_str = request.args.get('de', '')
    ate_str = request.args.get('ate', '')
    formato = request.args.get('formato', 'html')
    incluir_fotos = request.args.get('fotos') == '1'
    # Etapa das fotos do QR no PDF: 'saida' | 'entrega' | None (ambas).
    etapa_foto = request.args.get('etapa_foto') or None
    if etapa_foto not in ('saida', 'entrega'):
        etapa_foto = None

    try:
        de = datetime.strptime(de_str, '%Y-%m-%d').date()
    except ValueError:
        de = hoje.replace(day=1)
    try:
        ate = datetime.strptime(ate_str, '%Y-%m-%d').date()
    except ValueError:
        ate = hoje

    lojas = _lojas_operacionais()
    pedidos = []
    totais = {'qtd_pedidos': 0, 'valor_total': 0.0, 'divergencias': 0}
    por_item = defaultdict(lambda: {'quantidade': 0, 'recebido': 0, 'valor': 0.0})

    if loja_id:
        from app.constants import STATUS_PEDIDO_ENTREGUES
        query = PedidoLoja.query.filter(
            PedidoLoja.loja_id == loja_id,
            PedidoLoja.status.in_(STATUS_PEDIDO_ENTREGUES),
            PedidoLoja.data_entrega >= de,
            PedidoLoja.data_entrega <= ate,
        ).order_by(PedidoLoja.data_entrega)
        pedidos_raw = query.all()

        for p in pedidos_raw:
            subtotal = 0.0
            linhas = []
            for it in p.itens:
                preco = _preco_interno_item(it)
                qtd_efetiva = it.quantidade_recebida if it.quantidade_recebida is not None else it.quantidade
                valor_linha = preco * qtd_efetiva
                subtotal += valor_linha
                linhas.append({
                    'nome': it.nome_item,
                    'quantidade': it.quantidade,
                    'recebido': qtd_efetiva,
                    'preco': preco,
                    'subtotal': valor_linha,
                    'divergente': it.quantidade_recebida is not None and it.quantidade_recebida != it.quantidade,
                })
                por_item[it.nome_item]['quantidade'] += it.quantidade
                por_item[it.nome_item]['recebido'] += qtd_efetiva
                por_item[it.nome_item]['valor'] += valor_linha

            pedidos.append({'p': p, 'linhas': linhas, 'subtotal': subtotal})
            totais['qtd_pedidos'] += 1
            totais['valor_total'] += subtotal
            if p.tem_divergencia:
                totais['divergencias'] += 1

    loja_nome = next((l.nome for l in lojas if l.id == loja_id), 'loja') if loja_id else 'loja'

    if formato == 'xlsx' and loja_id:
        from app.services.relatorio import gerar_xlsx_pedidos
        buf = gerar_xlsx_pedidos(loja_nome, de, ate, pedidos, totais, por_item)
        return send_file(
            buf, as_attachment=True,
            download_name=f'pedidos_{loja_nome}_{de}_a_{ate}.xlsx',
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )

    if formato == 'pdf' and loja_id:
        from app.services.relatorio import gerar_pdf_pedidos
        buf = gerar_pdf_pedidos(loja_nome, de, ate, pedidos, totais, por_item,
                                incluir_fotos=incluir_fotos,
                                etapa_foto=etapa_foto)
        sufixo = '_com_fotos' if incluir_fotos else ''
        return send_file(
            buf, as_attachment=True,
            download_name=f'pedidos_{loja_nome}_{de}_a_{ate}{sufixo}.pdf',
            mimetype='application/pdf',
        )

    return render_template('pedidos/relatorio.html',
                           lojas=lojas, loja_id=loja_id,
                           de=de.isoformat(), ate=ate.isoformat(),
                           pedidos=pedidos, totais=totais,
                           incluir_fotos=incluir_fotos,
                           etapa_foto=etapa_foto or '',
                           por_item=sorted(por_item.items(), key=lambda x: x[0]))


def _aplicar_voltar_status(pedido, usuario_id):
    """Reverte UM pedido um passo, estornando estoque quando preciso. MUTA a
    sessao (NAO commita) — quem chama decide a transacao. Retorna
    (status_anterior, novo_status) ou None se nao da pra voltar (pendente/
    cancelado). Centralizado pra o single e o lote usarem a MESMA logica.

    Transicoes:
      recebido/entregue -> em_transporte (estorna estoque loja)
      em_transporte     -> separado      (estorna estoque producao + MP)
      separado          -> confirmado    (so status)
      confirmado        -> pendente      (so status)
    """
    status_atual = pedido.status
    if status_atual in ('entregue', 'recebido'):
        from app.services.estoque_helpers import serializar_loja
        serializar_loja(pedido.loja_id)  # lock por loja antes do estorno
        # Estorna o que somou no estoque da loja
        for item in pedido.itens:
            qtd = item.quantidade_recebida or 0
            if qtd <= 0:
                continue
            el = EstoqueLoja.query.filter_by(
                loja_id=pedido.loja_id,
                receita_id=item.receita_id,
                produto_id=item.produto_id,
                materia_prima_id=item.materia_prima_id,
            ).first()
            if el:
                el.quantidade = max(0, (el.quantidade or 0) - qtd)
                db.session.add(MovEstoqueLoja(
                    estoque_loja_id=el.id,
                    tipo='ajuste_negativo',
                    quantidade=qtd,
                    referencia=f'Estorno pedido #{pedido.id} (voltar status)',
                    usuario_id=usuario_id,
                ))
            item.quantidade_recebida = None
        novo_status = 'em_transporte'
    elif status_atual == 'em_transporte':
        # Estorna a baixa producao/MP pelo que os MOVIMENTOS dizem que saiu
        # (nao a quantidade nominal do item) — baixa que saturou em 0 nao
        # vira estoque fantasma no estorno (fix 03/07/2026).
        from app.services.pedido_estoque import estornar_industria_pedido
        estornar_industria_pedido(pedido, usuario_id, motivo='voltar status')
        novo_status = 'separado'
    elif status_atual == 'separado':
        novo_status = 'confirmado'
    elif status_atual == 'confirmado':
        novo_status = 'pendente'
    else:
        return None

    pedido.status = novo_status
    # Carimbo do gesto humano (10/08/2026, auto-pedidos): mexer no status é
    # decisão de gente — protege o pedido do re-sync do cron.
    pedido.modificado_em = agora()
    pedido.modificado_por_id = usuario_id
    return (status_atual, novo_status)


@pedidos_bp.route('/<int:id>/voltar-status', methods=['POST'])
@login_required
@admin_required
def voltar_status(id):
    """Volta UM pedido pra o status anterior, estornando estoque se necessario.
    So admin (risco de descompasso de estoque)."""
    pedido = PedidoLoja.query.get_or_404(id)
    status_atual = pedido.status
    try:
        res = _aplicar_voltar_status(pedido, current_user.id)
        if res is None:
            flash(f'Nao da pra voltar status "{status_atual}".', 'warning')
            return redirect(url_for('pedidos.detalhe', id=id))
        db.session.commit()
        flash(f'Status revertido: {res[0]} → {res[1]}.', 'success')
        if pedido.nf_emitida_em:
            # A NF de transferência NÃO é desfeita aqui (não há cancelamento
            # de NF pelo sistema). Se a carga mudar e sair de novo, a DANFE
            # antiga não vale — refazer do zero no card de NF (e cancelar a
            # antiga no Tiny). Achado A6 da revisão 20/07/2026.
            flash('Atenção: este pedido JÁ TEM NF de transferência emitida '
                  f'(nº {pedido.nf_numero or pedido.tiny_nota_fiscal_id}). '
                  'Se os itens mudarem, refaça a NF do zero no card de NF '
                  'e cancele a antiga no Tiny — senão a carga nova viaja '
                  'com a nota velha.', 'warning')
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        current_app.logger.exception('Falha ao voltar status pedido %s', id)
        flash(f'Erro ao voltar status: {exc}. Nada foi alterado.', 'danger')
    return redirect(url_for('pedidos.detalhe', id=id))


@pedidos_bp.route('/voltar-status-lote', methods=['POST'])
@login_required
@admin_required
def voltar_status_lote():
    """Volta o status de VARIOS pedidos de uma vez (selecao em massa). Tudo numa
    UNICA transacao: o estorno de estoque de pedidos da MESMA loja/receita soma
    certo (sem perder atualizacao concorrente, que aconteceria com N requests
    paralelos batendo na mesma linha de EstoqueProducao/EstoqueLoja). So admin.
    Retorna JSON {ok, revertidos, ignorados}."""
    ids = request.form.getlist('ids[]') or request.form.getlist('id[]')
    revertidos = 0
    ignorados = 0
    try:
        for raw in ids:
            try:
                pid = int(raw)
            except (TypeError, ValueError):
                continue
            pedido = PedidoLoja.query.get(pid)
            if pedido is None:
                ignorados += 1
                continue
            if _aplicar_voltar_status(pedido, current_user.id) is None:
                ignorados += 1
            else:
                revertidos += 1
        db.session.commit()
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        current_app.logger.exception('Falha ao voltar status em lote')
        return jsonify(ok=False, erro=str(exc)), 500
    return jsonify(ok=True, revertidos=revertidos, ignorados=ignorados)


@pedidos_bp.route('/<int:id>/cancelar', methods=['POST'])
@login_required
@gerente_required
def cancelar(id):
    pedido = PedidoLoja.query.get_or_404(id)
    loja_id = _loja_do_usuario()
    if loja_id and pedido.loja_id != loja_id:
        abort(403)
    if pedido.status not in ('pendente', 'confirmado', 'separado'):
        flash('Só é possível cancelar pedidos pendentes, confirmados ou separados (antes do envio).', 'warning')
        return redirect(url_for('pedidos.detalhe', id=id))
    # Corte do fim do dia (dono 10/08/2026): cancelar o pedido de amanhã
    # depois do corte também muda o pré-preparo já calculado.
    from app.services.pedido_corte import bloqueio_do_corte
    bloqueado, aviso_corte = bloqueio_do_corte([pedido.data_entrega],
                                               user=current_user)
    if bloqueado:
        flash(aviso_corte, 'warning')
        return redirect(url_for('pedidos.detalhe', id=id))
    if aviso_corte:
        flash(aviso_corte, 'warning')
    pedido.status = 'cancelado'
    # Carimbo do gesto humano (revisão 13/08): cancelar um pedido de dia
    # FUTURO é "não quero pedido nesse dia" — o carimbo impede o cron de
    # auto-pedidos de recriar o pedido na rodada seguinte.
    pedido.modificado_em = agora()
    pedido.modificado_por_id = current_user.id
    db.session.commit()
    flash('Pedido cancelado.', 'success')
    if pedido.nf_emitida_em:
        flash('Atenção: o pedido tinha NF de transferência emitida '
              f'(nº {pedido.nf_numero or pedido.tiny_nota_fiscal_id}) — '
              'cancele a nota no Tiny (o sistema não cancela NF sozinho).',
              'warning')
    return redirect(url_for('pedidos.lista'))


@pedidos_bp.route('/<int:id>/excluir', methods=['POST'])
@login_required
@admin_required
def excluir(id):
    pedido = PedidoLoja.query.get_or_404(id)
    # HandshakeAudit referencia pedido_id sem ondelete cascade — em Postgres
    # bloqueia o delete com FK violation. Limpa antes (audits viram orfaos
    # com pedido_id=NULL).
    from app.models import HandshakeAudit
    HandshakeAudit.query.filter_by(pedido_id=pedido.id).update(
        {'pedido_id': None})
    db.session.delete(pedido)
    db.session.commit()
    flash('Pedido excluído.', 'success')
    return redirect(url_for('pedidos.lista'))


# ── Painel de Separação ──

@pedidos_bp.route('/separacao')
@login_required
@producao_required
def separacao():
    pedidos = PedidoLoja.query.filter(
        PedidoLoja.status.in_(['pendente', 'confirmado'])
    ).order_by(PedidoLoja.data_entrega).all()

    por_data = defaultdict(lambda: defaultdict(lambda: {'total': 0, 'lojas': defaultdict(int)}))
    ids_por_data = defaultdict(list)
    # data → loja_nome → [(item, qtd, pedido_id)]
    por_data_loja = defaultdict(lambda: defaultdict(list))
    ids_por_data_loja = defaultdict(lambda: defaultdict(list))

    for p in pedidos:
        chave_data = p.data_entrega or p.data_pedido
        ids_por_data[chave_data].append(p.id)
        loja_nome = p.loja.nome if p.loja else '?'
        ids_por_data_loja[chave_data][loja_nome].append(p.id)
        for item in p.itens:
            nome = item.nome_item
            por_data[chave_data][nome]['total'] += item.quantidade
            por_data[chave_data][nome]['lojas'][loja_nome] += item.quantidade
            por_data_loja[chave_data][loja_nome].append({
                'item': nome, 'qtd': item.quantidade, 'pedido_id': p.id,
            })

    # Agrega items duplicados dentro de cada loja (mesmo item em 2 pedidos)
    for data, lojas in por_data_loja.items():
        for loja_nome, itens in lojas.items():
            agreg = defaultdict(int)
            for it in itens:
                agreg[it['item']] += it['qtd']
            lojas[loja_nome] = sorted(
                [{'item': nome, 'qtd': qtd} for nome, qtd in agreg.items()],
                key=lambda x: x['item']
            )

    congelados = {ep.nome_item: ep.quantidade for ep in EstoqueProducao.query.all()}

    return render_template('pedidos/separacao.html',
                           por_data=dict(por_data),
                           ids_por_data=dict(ids_por_data),
                           por_data_loja={d: dict(ls) for d, ls in por_data_loja.items()},
                           ids_por_data_loja={d: dict(ls) for d, ls in ids_por_data_loja.items()},
                           congelados=congelados)


# ── Estoque de Congelados ──

@pedidos_bp.route('/congelados/dashboard')
@login_required
@producao_required
def congelados_dashboard():
    """Visao consolidada: itens em EstoqueProducao cruzados com os mesmos
    itens em cada loja (EstoqueLoja). Mostra qtd por local + total."""
    lojas = _lojas_operacionais()
    eps = EstoqueProducao.query.all()

    # Index dos estoques de loja por (tipo_chave, item_id) → {loja_id: qtd}
    receita_loja = {}   # receita_id -> {loja_id: qtd}
    produto_loja = {}   # produto_id -> {loja_id: qtd}
    for el in EstoqueLoja.query.filter(
            (EstoqueLoja.receita_id.isnot(None)) | (EstoqueLoja.produto_id.isnot(None))
        ).all():
        if el.receita_id:
            receita_loja.setdefault(el.receita_id, {})[el.loja_id] = el.quantidade or 0
        elif el.produto_id:
            produto_loja.setdefault(el.produto_id, {})[el.loja_id] = el.quantidade or 0

    linhas = []
    for ep in eps:
        if ep.receita_id:
            por_loja = receita_loja.get(ep.receita_id, {})
            tipo = 'receita'
        elif ep.produto_id:
            por_loja = produto_loja.get(ep.produto_id, {})
            tipo = 'produto'
        else:
            por_loja = {}
            tipo = 'pendente'
        total_lojas = sum(por_loja.values())
        linhas.append({
            'nome': ep.nome_item,
            'tipo': tipo,
            'industria': ep.quantidade or 0,
            'por_loja': por_loja,
            'total_lojas': total_lojas,
            'total_geral': (ep.quantidade or 0) + total_lojas,
            'pendente': ep.pendente,
        })

    linhas.sort(key=lambda r: (r['pendente'], r['nome'].lower()))

    # Totais por coluna (Industria + cada loja)
    tot_industria = sum(r['industria'] for r in linhas)
    tot_por_loja = {l.id: 0 for l in lojas}
    for r in linhas:
        for lid, qt in r['por_loja'].items():
            if lid in tot_por_loja:
                tot_por_loja[lid] += qt
    tot_geral = tot_industria + sum(tot_por_loja.values())

    return render_template('pedidos/congelados_dashboard.html',
                           linhas=linhas, lojas=lojas,
                           tot_industria=tot_industria,
                           tot_por_loja=tot_por_loja,
                           tot_geral=tot_geral)


@pedidos_bp.route('/congelados')
@login_required
@producao_required
def congelados():
    itens = EstoqueProducao.query.options(
        joinedload(EstoqueProducao.receita),
        joinedload(EstoqueProducao.produto),
    ).all()
    receitas = Receita.ativas().order_by(Receita.categoria, Receita.nome).all()
    produtos = Produto.query.filter_by(ativo=True).order_by(Produto.nome).all()
    from app.services import estoque_congelados as svc_cong
    pendentes = [it for it in itens if it.pendente]
    sugestoes = svc_cong.sugerir_para_pendentes(pendentes) if pendentes else {}
    return render_template('pedidos/congelados.html', itens=itens,
                           receitas=receitas, produtos=produtos,
                           sugestoes=sugestoes)


@pedidos_bp.route('/congelados/movs/<int:estoque_id>')
@login_required
@producao_required
def congelados_movs(estoque_id):
    """Ultimos 5 CREDITOS + 5 DEBITOS de uma linha do estoque da industria —
    alimenta o expandir por item na tela /pedidos/congelados. Direcao vem da
    fonte unica `historico_humano.mov_producao_direcao` (movimentos
    informativos `*_sem_estoque`/consolidacao ficam fora das duas listas)."""
    from sqlalchemy import and_, or_

    from app.models import Usuario
    from app.services import historico_humano as hh

    ep = EstoqueProducao.query.get_or_404(estoque_id)
    base = (MovEstoqueProducao.query
            .filter_by(estoque_producao_id=ep.id)
            .order_by(MovEstoqueProducao.data.desc(),
                      MovEstoqueProducao.id.desc()))
    cond_credito = or_(
        MovEstoqueProducao.tipo.in_(sorted(hh.MOV_PRODUCAO_CREDITOS)),
        and_(MovEstoqueProducao.tipo == 'ajuste_conferencia',
             MovEstoqueProducao.quantidade >= 0))
    cond_neutro = or_(
        MovEstoqueProducao.tipo.in_(sorted(hh.MOV_PRODUCAO_NEUTROS)),
        MovEstoqueProducao.tipo.endswith('_sem_estoque', autoescape=True))
    creditos = base.filter(cond_credito).limit(5).all()
    debitos = base.filter(~cond_credito, ~cond_neutro).limit(5).all()

    user_ids = {m.usuario_id for m in creditos + debitos if m.usuario_id}
    users = ({u.id: u.nome for u in
              Usuario.query.filter(Usuario.id.in_(user_ids)).all()}
             if user_ids else {})

    def _fmt(m):
        return {
            'data': m.data.strftime('%d/%m %H:%M') if m.data else '',
            'tipo': hh.mov_producao_label(m.tipo),
            'quantidade': abs(int(m.quantidade or 0)),
            'referencia': m.referencia or '',
            'usuario': users.get(m.usuario_id) or '',
        }

    return jsonify(ok=True, item=ep.nome_item,
                   creditos=[_fmt(m) for m in creditos],
                   debitos=[_fmt(m) for m in debitos])


@pedidos_bp.route('/congelados/historico')
@login_required
@producao_required
def congelados_historico():
    """Historico de movimentos do estoque da industria (EstoqueProducao).

    Filtros: item (id), tipo, periodo (de/ate).
    """
    from sqlalchemy import desc

    item_id = request.args.get('item', type=int)
    tipo_filtro = (request.args.get('tipo') or '').strip()
    de_str = (request.args.get('de') or '').strip()
    ate_str = (request.args.get('ate') or '').strip()
    try:
        de = datetime.strptime(de_str, '%Y-%m-%d').date() if de_str else None
    except ValueError:
        de = None
    try:
        ate = datetime.strptime(ate_str, '%Y-%m-%d').date() if ate_str else None
    except ValueError:
        ate = None

    tipos_disp = ['entrada', 'saida_pedido', 'saida_pedido_sem_estoque',
                  'estorno_saida_pedido', 'ajuste', 'ajuste_conferencia',
                  'balanco', 'desperdicio', 'perda_producao',
                  'perda_producao_sem_estoque', 'perda_producao_estorno']

    q = db.session.query(MovEstoqueProducao, EstoqueProducao).join(
        EstoqueProducao, MovEstoqueProducao.estoque_producao_id == EstoqueProducao.id
    )
    if item_id:
        q = q.filter(EstoqueProducao.id == item_id)
    if tipo_filtro:
        q = q.filter(MovEstoqueProducao.tipo == tipo_filtro)
    if de:
        q = q.filter(MovEstoqueProducao.data >= datetime.combine(de, datetime.min.time()))
    if ate:
        q = q.filter(MovEstoqueProducao.data <= datetime.combine(ate, datetime.max.time()))

    rows = q.order_by(desc(MovEstoqueProducao.data)).limit(500).all()

    from app.models import Usuario
    user_ids = {mov.usuario_id for mov, _ in rows if mov.usuario_id}
    user_map = {u.id: u for u in Usuario.query.filter(Usuario.id.in_(user_ids)).all()} if user_ids else {}

    linhas = []
    for mov, ep in rows:
        linhas.append({
            'mov': mov, 'estoque': ep,
            'item_nome': ep.nome_item,
            'usuario': user_map.get(mov.usuario_id) if mov.usuario_id else None,
        })

    from collections import defaultdict
    totais = defaultdict(lambda: {'qtd': 0, 'n': 0})
    for mov, _ in rows:
        totais[mov.tipo]['qtd'] += mov.quantidade or 0
        totais[mov.tipo]['n'] += 1

    itens_disp = EstoqueProducao.query.order_by(EstoqueProducao.id).all()
    item_sel = EstoqueProducao.query.get(item_id) if item_id else None

    return render_template('pedidos/congelados_historico.html',
                           linhas=linhas, itens_disp=itens_disp, item_sel=item_sel,
                           tipos_disp=tipos_disp, tipo_filtro=tipo_filtro,
                           de=de_str, ate=ate_str, totais=dict(totais))


@pedidos_bp.route('/congelados/entrada', methods=['POST'])
@login_required
@producao_required
def congelados_entrada():
    # Caminho canonico (mesmo helper do painel Produzir da TV): entrada=soma na
    # linha cru (estado=None). Antes filtrava sem estado e o .first() podia
    # somar na linha 'backup' por engano.
    from app.services.estoque_congelados import entrada_producao
    tipo = request.form.get('tipo', 'receita')
    try:
        item_id = int(request.form['item_id'])
        qtd = int(request.form['quantidade'])
        entrada_producao(
            receita_id=item_id if tipo == 'receita' else None,
            produto_id=item_id if tipo == 'produto' else None,
            estado=None, quantidade=qtd, usuario_id=current_user.id,
            referencia='Entrada de produção',
        )
        db.session.commit()
    except (KeyError, ValueError):
        db.session.rollback()
        flash('Dados invalidos para a entrada de producao.', 'warning')
        return redirect(url_for('pedidos.congelados'))
    except Exception:  # noqa: BLE001
        db.session.rollback()
        current_app.logger.exception('congelados_entrada falhou')
        flash('Erro ao registrar a entrada. O log foi registrado.', 'danger')
        return redirect(url_for('pedidos.congelados'))
    flash(f'Entrada de {qtd} unidades registrada.', 'success')
    return redirect(url_for('pedidos.congelados'))


@pedidos_bp.route('/congelados/ajuste', methods=['POST'])
@login_required
@producao_required
def congelados_ajuste():
    ep_id = int(request.form['estoque_id'])
    qtd = int(request.form['quantidade'])
    tipo = request.form.get('tipo_ajuste', 'ajuste')

    ep = EstoqueProducao.query.get_or_404(ep_id)
    ep.quantidade = max(0, ep.quantidade - qtd)
    db.session.add(MovEstoqueProducao(
        estoque_producao_id=ep.id, tipo=tipo,
        quantidade=qtd, referencia=request.form.get('referencia', '').strip() or None,
        usuario_id=current_user.id,
    ))
    db.session.commit()
    flash(f'Ajuste de {qtd} unidades registrado.', 'success')
    return redirect(url_for('pedidos.congelados'))


# ── Balanco de Congelados (sobrescreve com contagem fisica) ──

@pedidos_bp.route('/congelados/balanco', methods=['GET', 'POST'])
@login_required
@producao_required
def congelados_balanco():
    """Tela de balanco: usuario cola lista 'nome: qtd' e sistema sobrescreve
    EstoqueProducao.quantidade pra cada item, com auditoria por delta."""
    from app.services import estoque_congelados as svc
    texto = request.form.get('texto', '') if request.method == 'POST' else ''
    referencia = request.form.get('referencia', '').strip()
    itens = []
    if request.method == 'POST' and texto.strip():
        parseados = svc.parsear_lista(texto)
        itens = svc.resolver_lista(parseados)
    return render_template('pedidos/balanco_congelados.html',
                           texto=texto, referencia=referencia, itens=itens)


@pedidos_bp.route('/congelados/vincular', methods=['POST'])
@login_required
@producao_required
def congelados_vincular():
    """Vincula uma EstoqueProducao pendente (sem receita/produto) a uma
    receita ou produto existente. Se ja existe outra EstoqueProducao com
    aquela receita/produto, soma a quantidade e apaga o orfao."""
    ep_id = int(request.form['estoque_id'])
    alvo_tipo = request.form.get('alvo_tipo')
    try:
        alvo_id = int(request.form.get('alvo_id', ''))
    except (TypeError, ValueError):
        alvo_id = 0
    if alvo_tipo not in ('receita', 'produto') or not alvo_id:
        flash('Selecione uma receita ou produto.', 'danger')
        return redirect(url_for('pedidos.congelados'))

    orfao = EstoqueProducao.query.get_or_404(ep_id)
    if not orfao.pendente:
        flash('Este item já está vinculado.', 'warning')
        return redirect(url_for('pedidos.congelados'))

    # Existe ja uma linha com aquela receita/produto? Se sim, mescla.
    existente = EstoqueProducao.query.filter_by(
        receita_id=alvo_id if alvo_tipo == 'receita' else None,
        produto_id=alvo_id if alvo_tipo == 'produto' else None,
    ).first()

    nome_orfao = orfao.nome_pendente or '?'
    qtd_orfao = orfao.quantidade or 0

    # Apelido global — proximo balanco com o mesmo nome resolve direto.
    _salvar_apelido_global(nome_orfao, alvo_tipo, alvo_id)

    if existente and existente.id != orfao.id:
        anterior = existente.quantidade or 0
        existente.quantidade = anterior + qtd_orfao
        if qtd_orfao:
            db.session.add(MovEstoqueProducao(
                estoque_producao_id=existente.id,
                tipo='balanco_entrada',
                quantidade=qtd_orfao,
                referencia=f'Vinculação de pendente "{nome_orfao}" (era {anterior}, ficou {existente.quantidade})',
                usuario_id=current_user.id,
            ))
        db.session.delete(orfao)
        db.session.commit()
        flash(f'"{nome_orfao}" mesclado em {existente.nome_item} (+{qtd_orfao} unidades).', 'success')
    else:
        if alvo_tipo == 'receita':
            orfao.receita_id = alvo_id
        else:
            orfao.produto_id = alvo_id
        orfao.nome_pendente = None
        db.session.commit()
        flash(f'"{nome_orfao}" vinculado com sucesso.', 'success')

    return redirect(url_for('pedidos.congelados'))


@pedidos_bp.route('/congelados/balanco/aplicar', methods=['POST'])
@login_required
@producao_required
def congelados_balanco_aplicar():
    """Aplica o balanco apos preview. Re-parseia o texto pra ser idempotente."""
    from app.services import estoque_congelados as svc
    texto = request.form.get('texto', '')
    referencia = request.form.get('referencia', '').strip() or None
    if not texto.strip():
        flash('Lista vazia — nada pra aplicar.', 'warning')
        return redirect(url_for('pedidos.congelados_balanco'))
    parseados = svc.parsear_lista(texto)
    resolvidos = svc.resolver_lista(parseados)
    resultado = svc.aplicar_balanco(resolvidos, current_user, referencia=referencia)
    n_ok = len(resultado['aplicados'])
    n_ign = len(resultado['ignorados'])
    if n_ok:
        flash(f'Balanço aplicado: {n_ok} item(ns) atualizados.'
              + (f' {n_ign} ignorados.' if n_ign else ''), 'success')
    else:
        flash(f'Nenhum item aplicado. {n_ign} ignorados.', 'warning')
    return redirect(url_for('pedidos.congelados'))


@pedidos_bp.route('/congelados/conferencia', methods=['GET', 'POST'])
@login_required
@producao_required
def congelados_conferencia():
    """Conferencia fisica do estoque da industria (EstoqueProducao): conta cada
    item, digita a quantidade real, ve a divergencia e ajusta pra bater (com
    auditoria em MovEstoqueProducao). Espelha a conferencia de loja. Permite
    adicionar item que apareceu no fisico mas ainda nao tem linha de estoque."""
    if request.method == 'POST':
        ajustes = 0
        # 1) ajusta os itens existentes (campos real_<id>)
        for key, val in request.form.items():
            if not key.startswith('real_') or not val.strip():
                continue
            try:
                ep_id = int(key[len('real_'):])
                real = int(val)
            except (TypeError, ValueError):
                continue
            if real < 0:
                continue
            ep = EstoqueProducao.query.get(ep_id)
            if not ep:
                continue
            diff = real - (ep.quantidade or 0)
            if diff == 0:
                continue
            db.session.add(MovEstoqueProducao(
                estoque_producao_id=ep.id, tipo='ajuste_conferencia', quantidade=diff,
                referencia=(f'Conferência por {current_user.nome}: '
                            f'sistema {ep.quantidade} → real {real} (diff {diff:+d})'),
                usuario_id=current_user.id))
            ep.quantidade = real
            ajustes += 1
        # 2) adiciona itens novos (receita/produto sem linha): novo_alvo[] + novo_qtd[]
        alvos = request.form.getlist('novo_alvo')
        qtds = request.form.getlist('novo_qtd')
        for alvo, qtd_raw in zip(alvos, qtds):
            if not alvo or not (qtd_raw or '').strip():
                continue
            try:
                qtd = int(qtd_raw)
            except (TypeError, ValueError):
                continue
            if qtd <= 0:
                continue
            tipo, _, rid = alvo.partition(':')
            if tipo not in ('receita', 'produto') or not rid.isdigit():
                continue
            filtro = ({'receita_id': int(rid)} if tipo == 'receita'
                      else {'produto_id': int(rid)})
            ep = EstoqueProducao.query.filter_by(estado=None, **filtro).first()
            if not ep:
                ep = EstoqueProducao(quantidade=0, estado=None, **filtro)
                db.session.add(ep)
                db.session.flush()
            diff = qtd - (ep.quantidade or 0)
            if diff == 0:
                continue
            db.session.add(MovEstoqueProducao(
                estoque_producao_id=ep.id, tipo='ajuste_conferencia', quantidade=diff,
                referencia=f'Conferência (item adicionado) por {current_user.nome}: → {qtd}',
                usuario_id=current_user.id))
            ep.quantidade = qtd
            ajustes += 1
        if ajustes:
            db.session.commit()
            flash(f'Conferência aplicada: {ajustes} ajuste(s) registrado(s).', 'success')
        else:
            flash('Nenhum ajuste necessário — o estoque já bate.', 'info')
        return redirect(url_for('pedidos.congelados_conferencia'))

    itens = (EstoqueProducao.query
             .options(joinedload(EstoqueProducao.receita),
                      joinedload(EstoqueProducao.produto))
             .all())
    itens = [it for it in itens if not it.pendente]

    def _chave(it):
        if it.receita:
            return (0, it.receita.categoria or 'ZZZ', it.receita.nome)
        if it.produto:
            return (1, it.produto.categoria or 'ZZZ', it.produto.nome)
        return (9, '', it.nome_item)
    itens.sort(key=_chave)

    com_rec = {it.receita_id for it in itens if it.receita_id}
    com_prod = {it.produto_id for it in itens if it.produto_id}
    receitas_add = [r for r in Receita.ativas().order_by(Receita.categoria, Receita.nome).all()
                    if r.id not in com_rec]
    produtos_add = [p for p in Produto.query.filter_by(ativo=True).order_by(Produto.nome).all()
                    if p.id not in com_prod]
    return render_template('pedidos/congelados_conferencia.html', itens=itens,
                           receitas_add=receitas_add, produtos_add=produtos_add)


# ── Estoque de Loja ──

@pedidos_bp.route('/conferencia', methods=['GET', 'POST'])
@login_required
@gerente_required
def conferencia():
    """Conferencia fisica de estoque da loja.

    Gerente conta cada item, digita a quantidade real, e o sistema:
    - Mostra a divergencia (sistema - real)
    - Ao submeter, ajusta o estoque para bater com o fisico
    - Cria MovEstoqueLoja com tipo='ajuste_conferencia' pra auditoria
    """
    loja_id = _loja_do_usuario()
    if current_user.is_admin():
        sel = request.args.get('loja')
        loja_id = int(sel) if sel else None

    loja = Loja.query.get(loja_id) if loja_id else None

    if request.method == 'POST' and loja:
        from app.services.estoque_helpers import serializar_loja
        serializar_loja(loja.id)  # lock por loja antes dos SET de conferencia
        # Form: campos 'real_<estoque_id>' com a quantidade contada
        ajustes = 0
        for key, val in request.form.items():
            if not key.startswith('real_') or not val.strip():
                continue
            try:
                estoque_id = int(key.replace('real_', ''))
                real = int(val)
            except (TypeError, ValueError):
                continue
            if real < 0:
                continue
            el = EstoqueLoja.query.get(estoque_id)
            if not el or el.loja_id != loja.id:
                continue
            diff = real - (el.quantidade or 0)
            if diff == 0:
                continue
            # Cria movimento tipando como ajuste manual
            mov = MovEstoqueLoja(
                estoque_loja_id=el.id,
                tipo='ajuste_conferencia',
                quantidade=abs(diff),
                referencia=(f'Conferência por {current_user.nome}: '
                            f'sistema {el.quantidade} → real {real} (diff {diff:+d})'),
                usuario_id=current_user.id,
            )
            if diff < 0:
                mov.quantidade = -mov.quantidade  # registra saida
            db.session.add(mov)
            el.quantidade = real
            ajustes += 1
        if ajustes > 0:
            db.session.commit()
            flash(f'Conferência aplicada: {ajustes} ajuste(s) registrado(s).', 'success')
        else:
            flash('Nenhum ajuste necessário — o estoque já bate.', 'info')
        return redirect(url_for('pedidos.conferencia', loja=loja.id))

    itens = []
    if loja_id:
        itens = (EstoqueLoja.query.filter_by(loja_id=loja_id)
                 .options(joinedload(EstoqueLoja.receita),
                          joinedload(EstoqueLoja.produto),
                          joinedload(EstoqueLoja.materia_prima))
                 .all())
        # Filtra orfaos (sem vinculo) — conferencia so faz sentido em itens
        # com nome resolvido
        itens = [it for it in itens if not it.pendente]
        # Ordena: receita > produto > MP, depois por nome
        def chave(it):
            if it.receita:
                return (0, it.receita.categoria or 'ZZZ', it.receita.nome)
            if it.produto:
                return (1, it.produto.categoria or 'ZZZ', it.produto.nome)
            if it.materia_prima:
                return (2, '', it.materia_prima.nome)
            return (9, '', '')
        itens.sort(key=chave)

    lojas = _lojas_operacionais()
    return render_template('pedidos/conferencia.html', loja=loja, itens=itens,
                            lojas=lojas, sel_loja=loja_id)


@pedidos_bp.route('/estoque-loja')
@login_required
@gerente_required
def estoque_loja():
    loja_id = _loja_do_usuario()
    if current_user.is_admin():
        sel = request.args.get('loja')
        loja_id = int(sel) if sel else None

    loja = Loja.query.get(loja_id) if loja_id else None
    itens = (EstoqueLoja.query.filter_by(loja_id=loja_id)
             .options(joinedload(EstoqueLoja.receita),
                      joinedload(EstoqueLoja.produto),
                      joinedload(EstoqueLoja.materia_prima))
             .all()) if loja_id else []
    lojas = _lojas_operacionais()
    receitas = Receita.ativas().order_by(Receita.categoria, Receita.nome).all() \
        if current_user.is_admin() else []
    produtos = Produto.query.filter_by(ativo=True).order_by(Produto.nome).all() \
        if current_user.is_admin() else []
    materias = MateriaPrima.ativas().order_by(MateriaPrima.nome).all() \
        if current_user.is_admin() else []
    sugestoes = {}
    if current_user.is_admin() and itens:
        from app.services import estoque_loja_lote as svc_lote
        pendentes = [it for it in itens if it.pendente]
        sugestoes = svc_lote.sugerir_para_pendentes(pendentes)

    # Devoluções loja→indústria recentes (7 dias), agrupadas por token —
    # admin pode estornar (as duas pontas) direto daqui.
    devolucoes = []
    if loja_id:
        from collections import OrderedDict
        from datetime import timedelta as _td

        from app.services.devolucao import (
            TIPO_BAIXA_LOJA,
            TIPO_BAIXA_LOJA_ESTORNO,
        )
        from app.utils import agora as _agora
        desde = _agora() - _td(days=7)
        movs_dev = (MovEstoqueLoja.query
                    .join(EstoqueLoja)
                    .filter(EstoqueLoja.loja_id == loja_id,
                            MovEstoqueLoja.tipo.in_(
                                [TIPO_BAIXA_LOJA, TIPO_BAIXA_LOJA_ESTORNO]),
                            MovEstoqueLoja.data >= desde)
                    .order_by(MovEstoqueLoja.data.desc())
                    .all())
        grupos = OrderedDict()
        estornados = set()
        for m in movs_dev:
            token = (m.referencia or '').rsplit(' ', 1)[-1]
            if not token.startswith('dev-'):
                continue
            if m.tipo == TIPO_BAIXA_LOJA_ESTORNO:
                estornados.add(token)
                continue
            g = grupos.setdefault(token, {'token': token, 'data': m.data,
                                          'itens': []})
            g['itens'].append({'nome': m.estoque.nome_item,
                               'qtd': m.quantidade})
        devolucoes = [dict(g, estornada=(g['token'] in estornados))
                      for g in grupos.values()]

    return render_template('pedidos/estoque_loja.html', loja=loja, itens=itens,
                           lojas=lojas, sel_loja=loja_id,
                           receitas=receitas, produtos=produtos, materias=materias,
                           sugestoes=sugestoes, devolucoes=devolucoes)


@pedidos_bp.route('/estoque-loja/balanco-template.xlsx')
@login_required
@gerente_required
def estoque_loja_balanco_template():
    """Baixa a planilha de CONTAGEM em branco (todos os itens que a loja pede +
    produtos ativos) pra alguem preencher a quantidade fisica. O 'caminho ao
    contrario' do balanco: entrega a folha certa em vez de mapear uma preenchida.
    Reimportar depois casa 100% porque os nomes saem exatos do catalogo."""
    from app.services import estoque_loja_lote as svc
    try:
        blob = svc.gerar_xlsx_template_balanco()
    except Exception:
        current_app.logger.exception('template de balanco de loja falhou')
        flash('Erro ao gerar a planilha de balanço.', 'danger')
        return redirect(url_for('pedidos.estoque_loja'))
    return send_file(
        io.BytesIO(blob),
        mimetype=('application/vnd.openxmlformats-officedocument'
                  '.spreadsheetml.sheet'),
        as_attachment=True, download_name='contagem_estoque_loja.xlsx')


@pedidos_bp.route('/estoque-loja/diario')
@login_required
@gerente_required
def estoque_loja_diario():
    """Movimento do dia por loja: estoque ontem | entradas | baixas | atual.
    Le dos movimentos ja registrados (MovEstoqueLoja). Detalhe por fonte
    ao expandir cada item."""
    from app.services import estoque_diario

    loja_id = _loja_do_usuario()
    if current_user.is_admin():
        sel = request.args.get('loja')
        loja_id = int(sel) if sel else None

    dia = hoje_brt()
    sel_dia = request.args.get('dia')
    if sel_dia:
        try:
            dia = datetime.strptime(sel_dia, '%Y-%m-%d').date()
        except ValueError:
            pass

    loja = Loja.query.get(loja_id) if loja_id else None
    linhas = estoque_diario.relatorio_diario(loja_id, dia) if loja_id else []
    totais = {
        'inicio': sum(x['estoque_inicio'] for x in linhas),
        'entradas': sum(x['entradas'] for x in linhas),
        'baixas': sum(x['baixas'] for x in linhas),
        'atual': sum(x['estoque_atual'] for x in linhas),
    }
    return render_template('pedidos/estoque_loja_diario.html',
                           loja=loja, linhas=linhas, totais=totais,
                           lojas=_lojas_operacionais(), sel_loja=loja_id,
                           dia=dia.isoformat())


# ── Entrada em lote no Estoque de Loja ──

@pedidos_bp.route('/estoque-loja/entrada-lote', methods=['GET', 'POST'])
@login_required
@admin_required
def estoque_loja_entrada_lote():
    """Preview de entrada em lote — usuario cola lista 'nome: qtd' e ve o que
    vai somar. Apply em outra rota pra ser idempotente."""
    from app.services import estoque_loja_lote as svc

    loja_id = None
    if request.method == 'POST':
        try:
            loja_id = int(request.form.get('loja_id') or 0) or None
        except ValueError:
            loja_id = None
    else:
        try:
            loja_id = int(request.args.get('loja') or 0) or None
        except ValueError:
            loja_id = None

    texto = request.form.get('texto', '') if request.method == 'POST' else ''
    referencia = request.form.get('referencia', '').strip()
    itens = []
    if request.method == 'POST' and texto.strip() and loja_id:
        parseados = svc.parsear_lista(texto)
        itens = svc.resolver_lista(parseados, loja_id)

    lojas = _lojas_operacionais()
    loja = Loja.query.get(loja_id) if loja_id else None
    return render_template('pedidos/estoque_loja_entrada_lote.html',
                           texto=texto, referencia=referencia, itens=itens,
                           lojas=lojas, loja=loja, sel_loja=loja_id)


@pedidos_bp.route('/estoque-loja/conferencia-lote', methods=['GET', 'POST'])
@login_required
@admin_required
def estoque_loja_conferencia_lote():
    """Preview da CONFERÊNCIA em lote (balanço): cola a contagem 'nome: qtd' e
    vê o ajuste (sistema → contado) antes de aplicar. SETA (não soma). Regra:
    qtd em branco não mexe; qtd 0 zera. Apply em outra rota (idempotente)."""
    from app.services import estoque_loja_lote as svc

    loja_id = None
    if request.method == 'POST':
        try:
            loja_id = int(request.form.get('loja_id') or 0) or None
        except ValueError:
            loja_id = None
    else:
        try:
            loja_id = int(request.args.get('loja') or 0) or None
        except ValueError:
            loja_id = None

    texto = request.form.get('texto', '') if request.method == 'POST' else ''
    referencia = request.form.get('referencia', '').strip()
    itens = []
    if request.method == 'POST' and texto.strip() and loja_id:
        parseados = svc.parsear_conferencia(texto)
        itens = svc.resolver_conferencia(parseados, loja_id)

    lojas = _lojas_operacionais()
    loja = Loja.query.get(loja_id) if loja_id else None
    return render_template('pedidos/estoque_loja_conferencia_lote.html',
                           texto=texto, referencia=referencia, itens=itens,
                           lojas=lojas, loja=loja, sel_loja=loja_id)


@pedidos_bp.route('/estoque-loja/saude')
@login_required
@gerente_required
def estoque_loja_saude():
    """Diagnostico de saude do estoque por loja: orfaos/pendentes, dias desde
    a ultima conferencia e a ultima saida em lote, nomes ainda nao vinculados
    (que NAO baixam na saida em lote) e itens DUPLICADOS (mesmo item em 2+ linhas
    de EstoqueLoja). Somente leitura — nao altera nada."""
    from app.models import EstoqueLoja, MovEstoqueLoja, SeruLojaMap, VendaMapa
    from app.utils import agora

    lojas = _lojas_operacionais()
    seru_ids = {m.loja_id for m in SeruLojaMap.query.filter(
        SeruLojaMap.loja_id.isnot(None),
        SeruLojaMap.confirmado_em.isnot(None)).all()}

    def _ultima(loja_id, tipo):
        m = (MovEstoqueLoja.query
             .join(EstoqueLoja, MovEstoqueLoja.estoque_loja_id == EstoqueLoja.id)
             .filter(EstoqueLoja.loja_id == loja_id, MovEstoqueLoja.tipo == tipo)
             .order_by(MovEstoqueLoja.data.desc()).first())
        return m.data if m else None

    def _chave_logica(it):
        """Chave do item independente do estado: (tipo, fk_id). None = pendente."""
        if it.receita_id:
            return ('receita', it.receita_id)
        if it.produto_id:
            return ('produto', it.produto_id)
        if it.materia_prima_id:
            return ('mp', it.materia_prima_id)
        return None

    agora_dt = agora()
    linhas = []
    for loja in lojas:
        itens = (EstoqueLoja.query.filter_by(loja_id=loja.id)
                 .options(joinedload(EstoqueLoja.receita),
                          joinedload(EstoqueLoja.produto),
                          joinedload(EstoqueLoja.materia_prima))
                 .all())
        ult_conf = _ultima(loja.id, 'ajuste_conferencia')
        ult_saida = _ultima(loja.id, 'saida_lote')

        # Agrupa linhas vinculadas pela chave logica; 2+ linhas = duplicidade.
        grupos = defaultdict(list)
        for it in itens:
            if it.pendente:
                continue
            chave = _chave_logica(it)
            if chave is not None:
                grupos[chave].append(it)
        duplicados = []
        for grupo in grupos.values():
            if len(grupo) < 2:
                continue
            estados = {(g.estado or None) for g in grupo}
            duplicados.append({
                'nome': grupo[0].nome_item,
                'tipo': _chave_logica(grupo[0])[0],
                'n_linhas': len(grupo),
                'total_qtd': sum((g.quantidade or 0) for g in grupo),
                # Mesmo estado em todas = duplicata pura (merge seguro na etapa 2).
                # Estados distintos = separacao por estado (backup/assado).
                'classificacao': 'duplicata_pura' if len(estados) == 1 else 'por_estado',
                'linhas': sorted(
                    ({'id': g.id, 'estado': g.estado, 'qtd': g.quantidade or 0}
                     for g in grupo),
                    key=lambda x: x['id']),
            })
        duplicados.sort(key=lambda d: (-d['n_linhas'], d['nome']))

        linhas.append({
            'loja': loja,
            'integrada': loja.id in seru_ids,
            'total': len(itens),
            'com_saldo': sum(1 for it in itens if (it.quantidade or 0) > 0),
            'orfaos': sum(1 for it in itens if it.pendente),
            'dias_conf': (agora_dt - ult_conf).days if ult_conf else None,
            'dias_saida': (agora_dt - ult_saida).days if ult_saida else None,
            'duplicados': duplicados,
            'n_duplicados': len(duplicados),
        })

    nao_vinculados = (VendaMapa.query
                      .filter(VendaMapa.canal == 'lote',
                              VendaMapa.ignorar.is_(False),
                              VendaMapa.receita_id.is_(None),
                              VendaMapa.produto_id.is_(None),
                              VendaMapa.materia_prima_id.is_(None))
                      .order_by(VendaMapa.nome_externo).all())

    # Cadastros homonimos no catalogo (raiz provavel da duplicidade): nome de
    # Receita/Produto nao tem unique constraint, entao pode haver cadastros
    # repetidos, e o mesmo nome pode existir nas duas tabelas.
    def _agrupar_por_nome(objs):
        by = defaultdict(list)
        for o in objs:
            chave = (o.nome or '').strip().lower()
            if chave:
                by[chave].append(o)
        return by

    rec_by = _agrupar_por_nome(Receita.query.all())
    prod_by = _agrupar_por_nome(Produto.query.all())
    receitas_homonimas = sorted(
        ({'nome': objs[0].nome, 'ids': sorted(o.id for o in objs)}
         for objs in rec_by.values() if len(objs) > 1),
        key=lambda d: d['nome'])
    produtos_homonimos = sorted(
        ({'nome': objs[0].nome, 'ids': sorted(o.id for o in objs)}
         for objs in prod_by.values() if len(objs) > 1),
        key=lambda d: d['nome'])
    colisoes_rec_prod = sorted(
        ({'nome': rec_by[k][0].nome,
          'receita_ids': sorted(o.id for o in rec_by[k]),
          'produto_ids': sorted(o.id for o in prod_by[k])}
         for k in (set(rec_by) & set(prod_by))),
        key=lambda d: d['nome'])

    return render_template('pedidos/estoque_loja_saude.html',
                           linhas=linhas, nao_vinculados=nao_vinculados,
                           receitas_homonimas=receitas_homonimas,
                           produtos_homonimos=produtos_homonimos,
                           colisoes_rec_prod=colisoes_rec_prod)


@pedidos_bp.route('/estoque-loja/consolidar', methods=['POST'])
@login_required
@admin_required
def estoque_loja_consolidar():
    """Consolida linhas duplicadas em 1 por produto (estado ignorado), na loja e
    na producao. Soma quantidade, audita (`consolidacao_estado`) e remove sobras.
    Idempotente — rodar de novo com tudo unico nao faz nada. Admin/owner."""
    from app.services.estoque_helpers import consolidar_estoque_duplicado

    consolidados_loja, consolidados_prod = consolidar_estoque_duplicado(
        usuario_id=current_user.id)
    db.session.commit()
    if consolidados_loja or consolidados_prod:
        flash(f'Consolidação concluída: {consolidados_loja} item(ns) de loja e '
              f'{consolidados_prod} de produção unificados em 1 linha.', 'success')
    else:
        flash('Nada a consolidar — cada produto já tem uma linha única.', 'info')
    return redirect(url_for('pedidos.estoque_loja_saude'))


@pedidos_bp.route('/estoque-loja/saida-lote', methods=['GET', 'POST'])
@login_required
@gerente_required
def estoque_loja_saida_lote():
    """Preview de saida em lote — usuario cola lista 'nome: qtd' (vendas
    manuais sem PDV API) e ve o que vai descontar. Apply em outra rota."""
    from app.services import estoque_loja_lote as svc

    loja_id = None
    if request.method == 'POST':
        try:
            loja_id = int(request.form.get('loja_id') or 0) or None
        except ValueError:
            loja_id = None
    else:
        try:
            loja_id = int(request.args.get('loja') or 0) or None
        except ValueError:
            loja_id = None

    texto = request.form.get('texto', '') if request.method == 'POST' else ''
    referencia = request.form.get('referencia', '').strip()
    itens = []
    if request.method == 'POST' and texto.strip() and loja_id:
        parseados = svc.parsear_lista(texto)
        itens = svc.resolver_lista_saida(parseados, loja_id)

    lojas = _lojas_operacionais()
    loja = Loja.query.get(loja_id) if loja_id else None
    return render_template('pedidos/estoque_loja_saida_lote.html',
                           texto=texto, referencia=referencia, itens=itens,
                           lojas=lojas, loja=loja, sel_loja=loja_id)


@pedidos_bp.route('/estoque-loja/mapeamentos')
@login_required
@admin_required
def estoque_loja_mapeamentos():
    """Lista VendaMapa (canal lote) pra admin vincular/ignorar nomes digitados."""
    produtos_map = VendaMapa.query.filter(VendaMapa.canal == 'lote').order_by(
        VendaMapa.ignorar.asc(),
        VendaMapa.confirmado_em.is_(None).desc(),
        VendaMapa.nome_externo,
    ).all()
    receitas = Receita.ativas().order_by(Receita.categoria, Receita.nome).all()
    produtos = Produto.query.filter_by(ativo=True).order_by(Produto.nome).all()
    materias = MateriaPrima.ativas().order_by(MateriaPrima.nome).all()
    # Lojas que JA usaram cada mapeamento (VendaMapaUso e criado pra TODA saida
    # em lote aplicada — fator 1 ou fracionado). E o vinculo confiavel mapeamento
    # -> loja (o VendaMapa em si e global, sem loja). Pendente/nunca-usado fica
    # sem loja (ninguem aplicou ainda).
    from collections import defaultdict
    lojas_por_map = defaultdict(list)
    for map_id, loja_nome in (db.session.query(
            VendaMapaUso.venda_mapa_id, Loja.nome)
            .join(Loja, VendaMapaUso.loja_id == Loja.id)
            .order_by(Loja.nome).all()):
        lojas_por_map[map_id].append(loja_nome)
    return render_template('pedidos/estoque_loja_mapeamentos.html',
                           produtos_map=produtos_map, lojas_por_map=lojas_por_map,
                           receitas=receitas, produtos=produtos, materias=materias)


@pedidos_bp.route('/estoque-loja/mapeamentos/vincular/<int:map_id>', methods=['POST'])
@login_required
@admin_required
def estoque_loja_mapeamentos_vincular(map_id):
    """Vincula/ignora/desfaz uma entrada do VendaMapa (canal lote)."""
    mp = VendaMapa.query.get_or_404(map_id)
    acao = (request.form.get('acao') or '').strip()
    alvo_tipo = (request.form.get('alvo_tipo') or '').strip()
    raw_alvo = request.form.get('alvo_id') or ''
    try:
        alvo_id = int(raw_alvo)
    except (TypeError, ValueError):
        alvo_id = 0
    try:
        fator = float((request.form.get('fator') or '1').replace(',', '.'))
    except (TypeError, ValueError):
        fator = 1.0
    # Sanidade: rejeita absurdos. 100 = teto generoso (compra de 1 venda = 100 unidades).
    if fator < 0.001 or fator > 100:
        fator = 1.0

    # Fallback: form sem submitter (Enter no select)
    if not acao and alvo_id and alvo_tipo in ('receita', 'produto', 'mp'):
        acao = 'vincular'

    if acao == 'vincular':
        if alvo_tipo not in ('receita', 'produto', 'mp') or not alvo_id:
            flash('Selecione um alvo valido.', 'danger')
            return redirect(url_for('pedidos.estoque_loja_mapeamentos'))
        mp.receita_id = alvo_id if alvo_tipo == 'receita' else None
        mp.produto_id = alvo_id if alvo_tipo == 'produto' else None
        mp.materia_prima_id = alvo_id if alvo_tipo == 'mp' else None
        mp.ignorar = False
        mp.fator_quantidade = fator
        mp.confirmado_em = agora()
        mp.confirmado_por = current_user.id
        fator_msg = f' (fator {fator:g})' if fator != 1.0 else ''
        flash(f'"{mp.nome_externo}" → {mp.alvo_nome}{fator_msg}', 'success')
    elif acao == 'ignorar':
        mp.ignorar = True
        mp.receita_id = None
        mp.produto_id = None
        mp.materia_prima_id = None
        mp.confirmado_em = agora()
        mp.confirmado_por = current_user.id
        flash(f'"{mp.nome_externo}" ignorado.', 'info')
    elif acao == 'desfazer':
        mp.ignorar = False
        mp.receita_id = None
        mp.produto_id = None
        mp.materia_prima_id = None
        mp.confirmado_em = None
        flash(f'"{mp.nome_externo}" voltou pra pendente.', 'info')
    else:
        flash(f'Acao desconhecida: {acao!r}.', 'danger')

    db.session.commit()
    return redirect(url_for('pedidos.estoque_loja_mapeamentos'))


def _grupos_nomes_duplicados(rows, *, tem_categoria=False):
    """Agrupa por nome normalizado (minusculo, sem espaco nas pontas) e devolve
    so os grupos com 2+ itens. `colisao_exata` marca os que batem TAMBEM no
    rotulo do typeahead (mesmo nome E, pra receita, mesma categoria) — esses
    ganham o sufixo #id na busca. Ordena por nº de repeticoes desc."""
    grupos = {}
    for r in rows:
        chave = (r.nome or '').strip().lower()
        grupos.setdefault(chave, []).append(r)
    out = []
    for itens in grupos.values():
        if len(itens) < 2:
            continue
        det = []
        for r in itens:
            cat = (getattr(r, 'categoria', None) or '') if tem_categoria else None
            det.append({'id': r.id, 'nome': r.nome, 'categoria': cat})
        # colisao exata no typeahead: rotulo identico (nome + categoria p/ receita)
        rotulos = {(d['nome'], d['categoria']) for d in det}
        out.append({'nome': itens[0].nome, 'n': len(itens), 'itens': det,
                    'colisao_exata': len(rotulos) < len(det)})
    out.sort(key=lambda g: (-g['n'], (g['nome'] or '').lower()))
    return out


@pedidos_bp.route('/catalogo/nomes-duplicados')
@login_required
@admin_required
def nomes_duplicados():
    """Diagnostico read-only: nomes repetidos de Receita / Produto / Materia-
    Prima. Nome nao e unico no banco, entao nomes iguais colidem no typeahead
    de vinculo (resolvido com sufixo #id) e confundem em geral. Aqui pra limpar."""
    receitas = _grupos_nomes_duplicados(
        Receita.query.filter(Receita.arquivada_em.is_(None))
        .order_by(Receita.nome).all(), tem_categoria=True)
    produtos = _grupos_nomes_duplicados(
        Produto.query.filter_by(ativo=True).order_by(Produto.nome).all())
    materias = _grupos_nomes_duplicados(
        MateriaPrima.ativas().order_by(MateriaPrima.nome).all())
    return render_template('pedidos/nomes_duplicados.html',
                           receitas=receitas, produtos=produtos, materias=materias,
                           total=len(receitas) + len(produtos) + len(materias))


@pedidos_bp.route('/estoque-loja/historico')
@login_required
@gerente_required
def estoque_loja_historico():
    """Lista TODAS as movimentacoes de MovEstoqueLoja, filtravel por loja/tipo/data."""
    from sqlalchemy import desc

    raw_loja = request.args.get('loja')
    try:
        loja_id = int(raw_loja) if raw_loja else None
    except ValueError:
        loja_id = None

    tipos_disp = [
        'entrada_pedido', 'entrada_manual', 'entrada_lote', 'ajuste_negativo',
        'saida_lote', 'venda_loja_sem_estoque',
        'venda_seru', 'venda_seru_estorno', 'venda_seru_sem_estoque',
        'venda_vnda', 'venda_vnda_estorno', 'venda_vnda_sem_estoque',
        'venda_site', 'venda_site_estorno', 'venda_site_sem_estoque',
        'desperdicio', 'desperdicio_sem_estoque', 'desperdicio_estorno',
        # Movimentos da tela de estoque da loja (03/07/2026 — antes existiam
        # na lista mas nao no filtro, dando sensacao de "nao registrou").
        'venda', 'perda', 'sobra', 'descarte', 'devolucao', 'ajuste',
        'ajuste_conferencia',
        'perda_sem_estoque', 'sobra_sem_estoque', 'descarte_sem_estoque',
        'ajuste_sem_estoque',
    ]
    tipo_filtro = request.args.get('tipo', '').strip()

    de_str = (request.args.get('de') or '').strip()
    ate_str = (request.args.get('ate') or '').strip()
    try:
        de = datetime.strptime(de_str, '%Y-%m-%d').date() if de_str else None
    except ValueError:
        de = None
    try:
        ate = datetime.strptime(ate_str, '%Y-%m-%d').date() if ate_str else None
    except ValueError:
        ate = None

    # Nao-admin so ve a propria loja
    loja_user = _loja_do_usuario()
    if loja_user:
        loja_id = loja_user

    q = db.session.query(MovEstoqueLoja, EstoqueLoja).join(
        EstoqueLoja, MovEstoqueLoja.estoque_loja_id == EstoqueLoja.id
    )
    if loja_id:
        q = q.filter(EstoqueLoja.loja_id == loja_id)
    if tipo_filtro:
        q = q.filter(MovEstoqueLoja.tipo == tipo_filtro)
    if de:
        q = q.filter(MovEstoqueLoja.data >= datetime.combine(de, datetime.min.time()))
    if ate:
        q = q.filter(MovEstoqueLoja.data <= datetime.combine(ate, datetime.max.time()))

    rows = q.order_by(desc(MovEstoqueLoja.data)).limit(500).all()

    loja_ids = {est.loja_id for _, est in rows}
    rec_ids = {est.receita_id for _, est in rows if est.receita_id}
    prod_ids = {est.produto_id for _, est in rows if est.produto_id}
    mp_ids = {est.materia_prima_id for _, est in rows if est.materia_prima_id}
    user_ids = {mov.usuario_id for mov, _ in rows if mov.usuario_id}

    from app.models import Usuario
    lojas_map = {l.id: l for l in Loja.query.filter(Loja.id.in_(loja_ids)).all()} if loja_ids else {}
    rec_map = {r.id: r for r in Receita.query.filter(Receita.id.in_(rec_ids)).all()} if rec_ids else {}
    prod_map = {p.id: p for p in Produto.query.filter(Produto.id.in_(prod_ids)).all()} if prod_ids else {}
    mp_map = {m.id: m for m in MateriaPrima.query.filter(MateriaPrima.id.in_(mp_ids)).all()} if mp_ids else {}
    user_map = {u.id: u for u in Usuario.query.filter(Usuario.id.in_(user_ids)).all()} if user_ids else {}

    linhas = []
    for mov, est in rows:
        if est.receita_id:
            r = rec_map.get(est.receita_id)
            item_nome = r.nome if r else '?'
        elif est.produto_id:
            p = prod_map.get(est.produto_id)
            item_nome = p.nome if p else '?'
        elif est.materia_prima_id:
            m = mp_map.get(est.materia_prima_id)
            item_nome = m.nome + ' (MP)' if m else '?'
        elif est.nome_pendente:
            item_nome = est.nome_pendente
        else:
            item_nome = '?'
        linhas.append({
            'mov': mov, 'estoque': est,
            'loja': lojas_map.get(est.loja_id),
            'item_nome': item_nome,
            'usuario': user_map.get(mov.usuario_id) if mov.usuario_id else None,
        })

    # Totais por tipo
    from collections import defaultdict
    totais = defaultdict(lambda: {'qtd': 0, 'n': 0})
    for mov, _ in rows:
        totais[mov.tipo]['qtd'] += mov.quantidade or 0
        totais[mov.tipo]['n'] += 1

    lojas = _lojas_operacionais()
    return render_template('pedidos/estoque_loja_historico.html',
                           linhas=linhas, lojas=lojas, sel_loja=loja_id,
                           tipos_disp=tipos_disp, tipo_filtro=tipo_filtro,
                           de=de_str, ate=ate_str, totais=dict(totais))


@pedidos_bp.route('/estoque-loja/historico-saida-lote')
@login_required
@gerente_required
def estoque_loja_historico_saida_lote():
    """Lista MovEstoqueLoja de tipo saida_lote/venda_loja_sem_estoque."""
    from sqlalchemy import desc

    raw_loja = request.args.get('loja')
    try:
        loja_id = int(raw_loja) if raw_loja else None
    except ValueError:
        loja_id = None

    q = db.session.query(MovEstoqueLoja, EstoqueLoja).join(
        EstoqueLoja, MovEstoqueLoja.estoque_loja_id == EstoqueLoja.id
    ).filter(MovEstoqueLoja.tipo.in_(['saida_lote', 'venda_loja_sem_estoque']))
    if loja_id:
        q = q.filter(EstoqueLoja.loja_id == loja_id)
    rows = q.order_by(desc(MovEstoqueLoja.data)).limit(300).all()

    # Pre-carrega catalogos em batch (evita N+1)
    loja_ids = {est.loja_id for _, est in rows}
    rec_ids = {est.receita_id for _, est in rows if est.receita_id}
    prod_ids = {est.produto_id for _, est in rows if est.produto_id}
    mp_ids = {est.materia_prima_id for _, est in rows if est.materia_prima_id}
    lojas_map = {l.id: l for l in Loja.query.filter(Loja.id.in_(loja_ids)).all()} if loja_ids else {}
    rec_map = {r.id: r for r in Receita.query.filter(Receita.id.in_(rec_ids)).all()} if rec_ids else {}
    prod_map = {p.id: p for p in Produto.query.filter(Produto.id.in_(prod_ids)).all()} if prod_ids else {}
    mp_map = {m.id: m for m in MateriaPrima.query.filter(MateriaPrima.id.in_(mp_ids)).all()} if mp_ids else {}

    linhas = []
    for mov, est in rows:
        if est.receita_id:
            r = rec_map.get(est.receita_id)
            item_nome = r.nome if r else '?'
        elif est.produto_id:
            p = prod_map.get(est.produto_id)
            item_nome = p.nome if p else '?'
        elif est.materia_prima_id:
            m = mp_map.get(est.materia_prima_id)
            item_nome = m.nome if m else '?'
        elif est.nome_pendente:
            item_nome = est.nome_pendente
        else:
            item_nome = '?'
        linhas.append({
            'mov': mov, 'estoque': est,
            'loja': lojas_map.get(est.loja_id), 'item_nome': item_nome,
        })

    lojas = _lojas_operacionais()
    return render_template('pedidos/estoque_loja_historico_saida_lote.html',
                           linhas=linhas, lojas=lojas, sel_loja=loja_id)


@pedidos_bp.route('/estoque-loja/saida-lote/aplicar', methods=['POST'])
@login_required
@gerente_required
def estoque_loja_saida_lote_aplicar():
    from app.services import estoque_loja_lote as svc
    try:
        loja_id = int(request.form.get('loja_id') or 0)
    except ValueError:
        loja_id = 0
    if not loja_id:
        flash('Selecione uma loja.', 'warning')
        return redirect(url_for('pedidos.estoque_loja_saida_lote'))

    texto = request.form.get('texto', '')
    referencia = request.form.get('referencia', '').strip() or None
    if not texto.strip():
        flash('Lista vazia — nada pra aplicar.', 'warning')
        return redirect(url_for('pedidos.estoque_loja_saida_lote', loja=loja_id))

    parseados = svc.parsear_lista(texto)
    resolvidos = svc.resolver_lista_saida(parseados, loja_id)
    resultado = svc.aplicar_saida_lote(resolvidos, loja_id, current_user,
                                        referencia=referencia)
    n_ok = len(resultado['aplicados'])
    n_ign = len(resultado['ignorados'])
    if n_ok:
        flash(f'Saida aplicada: {n_ok} item(ns) descontado(s).'
              + (f' {n_ign} ignorado(s)/pendente(s).' if n_ign else ''), 'success')
    else:
        flash(f'Nenhum item descontado. {n_ign} ignorado(s)/pendente(s) — '
              f'vincule em /pedidos/estoque-loja/mapeamentos.', 'warning')
    return redirect(url_for('pedidos.estoque_loja_saida_lote', loja=loja_id))


@pedidos_bp.route('/estoque-loja/entrada-lote/aplicar', methods=['POST'])
@login_required
@admin_required
def estoque_loja_entrada_lote_aplicar():
    from app.services import estoque_loja_lote as svc
    try:
        loja_id = int(request.form.get('loja_id') or 0)
    except ValueError:
        loja_id = 0
    if not loja_id:
        flash('Selecione uma loja.', 'warning')
        return redirect(url_for('pedidos.estoque_loja_entrada_lote'))

    texto = request.form.get('texto', '')
    referencia = request.form.get('referencia', '').strip() or None
    if not texto.strip():
        flash('Lista vazia — nada pra aplicar.', 'warning')
        return redirect(url_for('pedidos.estoque_loja_entrada_lote', loja=loja_id))

    parseados = svc.parsear_lista(texto)
    resolvidos = svc.resolver_lista(parseados, loja_id)
    resultado = svc.aplicar_entrada_lote(resolvidos, loja_id, current_user,
                                          referencia=referencia)
    n_ok = len(resultado['aplicados'])
    n_ign = len(resultado['ignorados'])
    if n_ok:
        flash(f'Entrada aplicada: {n_ok} item(ns).'
              + (f' {n_ign} ignorados.' if n_ign else ''), 'success')
    else:
        flash(f'Nenhum item aplicado. {n_ign} ignorados.', 'warning')
    return redirect(url_for('pedidos.estoque_loja', loja=loja_id))


@pedidos_bp.route('/estoque-loja/conferencia-lote/aplicar', methods=['POST'])
@login_required
@admin_required
def estoque_loja_conferencia_lote_aplicar():
    """Aplica a conferência em lote: SETA cada item ao valor contado e registra
    ajuste_conferencia com a diferença. Re-parseia do texto (idempotente)."""
    from app.services import estoque_loja_lote as svc
    try:
        loja_id = int(request.form.get('loja_id') or 0)
    except ValueError:
        loja_id = 0
    if not loja_id:
        flash('Selecione uma loja.', 'warning')
        return redirect(url_for('pedidos.estoque_loja_conferencia_lote'))

    texto = request.form.get('texto', '')
    referencia = request.form.get('referencia', '').strip() or None
    if not texto.strip():
        flash('Contagem vazia — nada pra conferir.', 'warning')
        return redirect(url_for('pedidos.estoque_loja_conferencia_lote', loja=loja_id))

    parseados = svc.parsear_conferencia(texto)
    resolvidos = svc.resolver_conferencia(parseados, loja_id)
    resultado = svc.aplicar_conferencia(resolvidos, loja_id, current_user,
                                        referencia=referencia)
    ajustes = [a for a in resultado['aplicados'] if a['diff'] != 0]
    n_ajuste = len(ajustes)
    n_igual = len(resultado['aplicados']) - n_ajuste
    n_ign = len(resultado['ignorados'])
    if n_ajuste:
        flash(f'Conferência aplicada: {n_ajuste} ajuste(s) de estoque'
              + (f', {n_igual} já batiam' if n_igual else '')
              + (f', {n_ign} ignorados (em branco/unidade/sem match).' if n_ign else '.'),
              'success')
    else:
        flash('Nenhum ajuste — o estoque já bate com a contagem.'
              + (f' {n_ign} linha(s) ignorada(s).' if n_ign else ''), 'info')
    return redirect(url_for('pedidos.estoque_loja', loja=loja_id))


def _salvar_apelido_global(nome_digitado, alvo_tipo, alvo_id):
    """Cria/atualiza VendaMapa (apelido global, canal lote) ao vincular um pendente.

    Vale pra qualquer loja — apelido 'PFR' vinculado uma vez em Ribeiro
    serve tambem em Anesio. Confirmado_em preenchido = entrada/saida em
    lote usa direto sem virar pendente.
    """
    nome = (nome_digitado or '').strip()
    if not nome or nome == '?' or alvo_tipo not in ('receita', 'produto', 'mp'):
        return
    from sqlalchemy import func as sa_func
    mp = VendaMapa.query.filter(
        VendaMapa.canal == 'lote',
        sa_func.lower(VendaMapa.nome_externo) == nome.lower(),
    ).first()
    if not mp:
        mp = VendaMapa(canal='lote', nome_externo=nome)
        db.session.add(mp)
    mp.receita_id = alvo_id if alvo_tipo == 'receita' else None
    mp.produto_id = alvo_id if alvo_tipo == 'produto' else None
    mp.materia_prima_id = alvo_id if alvo_tipo == 'mp' else None
    mp.ignorar = False
    mp.confirmado_em = agora()
    mp.confirmado_por = current_user.id


@pedidos_bp.route('/estoque-loja/vincular', methods=['POST'])
@login_required
@admin_required
def estoque_loja_vincular():
    """Vincula uma EstoqueLoja pendente a uma receita/produto/MP."""
    ep_id = int(request.form['estoque_id'])
    alvo_tipo = request.form.get('alvo_tipo')
    try:
        alvo_id = int(request.form.get('alvo_id', ''))
    except (TypeError, ValueError):
        alvo_id = 0
    if alvo_tipo not in ('receita', 'produto', 'mp') or not alvo_id:
        flash('Selecione um item valido.', 'danger')
        return redirect(url_for('pedidos.estoque_loja'))

    orfao = EstoqueLoja.query.get_or_404(ep_id)
    loja_id = orfao.loja_id
    if not orfao.pendente:
        flash('Este item ja esta vinculado.', 'warning')
        return redirect(url_for('pedidos.estoque_loja', loja=loja_id))

    filtro = {'loja_id': loja_id}
    if alvo_tipo == 'receita':
        filtro['receita_id'] = alvo_id
    elif alvo_tipo == 'produto':
        filtro['produto_id'] = alvo_id
    else:
        filtro['materia_prima_id'] = alvo_id

    from app.services.estoque_helpers import serializar_loja
    serializar_loja(loja_id)  # lock por loja antes de fundir o orfao
    existente = EstoqueLoja.query.filter_by(**filtro).first()
    nome_orfao = orfao.nome_pendente or '?'
    qtd_orfao = orfao.quantidade or 0

    # Salva apelido global pra reusar em lancamentos futuros (qualquer loja).
    _salvar_apelido_global(nome_orfao, alvo_tipo, alvo_id)

    if existente and existente.id != orfao.id:
        anterior = existente.quantidade or 0
        existente.quantidade = anterior + qtd_orfao
        if qtd_orfao:
            db.session.add(MovEstoqueLoja(
                estoque_loja_id=existente.id,
                tipo='entrada_lote',
                quantidade=qtd_orfao,
                referencia=f'Vinculacao de pendente "{nome_orfao}" (era {anterior}, ficou {existente.quantidade})',
                usuario_id=current_user.id,
            ))
        db.session.delete(orfao)
        db.session.commit()
        flash(f'"{nome_orfao}" mesclado em {existente.nome_item} (+{qtd_orfao}).', 'success')
    else:
        if alvo_tipo == 'receita':
            orfao.receita_id = alvo_id
        elif alvo_tipo == 'produto':
            orfao.produto_id = alvo_id
        else:
            orfao.materia_prima_id = alvo_id
        orfao.nome_pendente = None
        db.session.commit()
        flash(f'"{nome_orfao}" vinculado com sucesso.', 'success')

    return redirect(url_for('pedidos.estoque_loja', loja=loja_id))


@pedidos_bp.route('/estoque-loja/registrar', methods=['POST'])
@login_required
def estoque_loja_registrar():
    loja_id = _loja_do_usuario()
    if current_user.is_admin():
        loja_id = int(request.form.get('loja_id', 0))

    if not loja_id:
        flash('Selecione uma loja.', 'warning')
        return redirect(url_for('pedidos.estoque_loja'))

    from app.services.estoque_helpers import serializar_loja
    serializar_loja(loja_id)  # lock por loja antes dos UPDATE em EstoqueLoja

    ids = request.form.getlist('estoque_id[]')
    qtds = request.form.getlist('qtd[]')
    tipos = request.form.getlist('tipo[]')
    # 'sobra' e 'perda' sao o que o form envia desde sempre — antes NAO estavam
    # na lista e caiam no fallback 'venda' (perda virava "venda manual" no
    # historico). 'devolucao' = devolver a INDUSTRIA (duas pontas, abaixo).
    TIPOS_VALIDOS = {'venda', 'ajuste', 'devolucao', 'descarte', 'sobra', 'perda'}

    # Pre-carrega EstoqueLoja em batch (evita N+1)
    eids_int = []
    for eid in ids:
        try:
            eids_int.append(int(eid))
        except (TypeError, ValueError):
            eids_int.append(None)
    eids_validos = [e for e in eids_int if e is not None]
    els_map = {e.id: e for e in EstoqueLoja.query.filter(EstoqueLoja.id.in_(eids_validos)).all()} if eids_validos else {}

    devolver = []   # linhas 'devolucao' → duas pontas via service, 1 token
    for i, eid_int in enumerate(eids_int):
        if eid_int is None:
            continue
        try:
            qtd = int(qtds[i]) if i < len(qtds) and qtds[i] else 0
        except (TypeError, ValueError):
            continue
        if qtd <= 0:
            continue
        el = els_map.get(eid_int)
        if not el or el.loja_id != loja_id:
            continue
        tipo = tipos[i] if i < len(tipos) else 'venda'
        if tipo not in TIPOS_VALIDOS:
            tipo = 'venda'
        if tipo == 'devolucao' and (el.receita_id or el.produto_id):
            # Devolucao vai pra INDUSTRIA: baixa a loja E credita o congelado
            # (service devolucao.py). MP nao tem estoque na industria — segue
            # no caminho antigo (so baixa a loja).
            devolver.append({
                'tipo': 'receita' if el.receita_id else 'produto',
                'id': el.receita_id or el.produto_id, 'qtd': qtd})
            continue
        # Integridade do ledger (03/07/2026): o movimento grava a quantidade
        # REALMENTE baixada; o que passou do saldo vira mov *_sem_estoque
        # (antes gravava a qtd cheia com o saldo travado em 0 — o histórico
        # e o Movimento do dia ficavam inflados).
        saldo = el.quantidade or 0
        baixa = min(qtd, saldo)
        falta = qtd - baixa
        el.quantidade = saldo - baixa
        if baixa > 0:
            db.session.add(MovEstoqueLoja(
                estoque_loja_id=el.id, tipo=tipo, quantidade=baixa,
                referencia=f'{tipo.capitalize()} registrada',
                usuario_id=current_user.id,
            ))
        if falta > 0:
            tipo_falta = ('venda_loja_sem_estoque' if tipo == 'venda'
                          else f'{tipo}_sem_estoque')
            db.session.add(MovEstoqueLoja(
                estoque_loja_id=el.id, tipo=tipo_falta, quantidade=falta,
                referencia=f'{tipo.capitalize()} registrada (sem saldo)',
                usuario_id=current_user.id,
            ))

    if devolver:
        from app.services.devolucao import devolver_industria
        r = devolver_industria(loja_id, devolver, current_user.id, commit=False)
        for aviso in r['avisos']:
            flash(aviso, 'warning')
        flash(f'Devolução {r["token"]}: {len(r["itens"])} item(ns) '
              'creditado(s) na indústria.', 'info')

    db.session.commit()
    flash('Estoque atualizado.', 'success')
    return redirect(url_for('pedidos.estoque_loja', loja=loja_id))


@pedidos_bp.route('/devolucao/estornar', methods=['POST'])
@login_required
@admin_required
def devolucao_estornar():
    """Estorna uma devolução loja→indústria pelo token (as duas pontas)."""
    from app.services.devolucao import estornar_devolucao
    token = (request.form.get('token') or '').strip()
    loja_id = request.form.get('loja_id')
    try:
        r = estornar_devolucao(token, current_user.id)
    except ValueError as e:
        flash(str(e), 'warning')
    else:
        for aviso in r['avisos']:
            flash(aviso, 'warning')
        flash(f'Devolução {token} estornada (loja re-creditada, '
              'indústria re-baixada).', 'success')
    return redirect(url_for('pedidos.estoque_loja', loja=loja_id or None))


@pedidos_bp.route('/estoque-loja/ajuste', methods=['POST'])
@login_required
@admin_required
def estoque_loja_ajuste():
    loja_id = int(request.form.get('loja_id', 0))
    tipo, item_id = _parse_item_id(request.form.get('item_id', ''))
    qtd = int(request.form.get('quantidade', 0))
    operacao = request.form.get('operacao', 'entrada')
    motivo = request.form.get('motivo', '').strip()

    if not loja_id or not item_id or qtd <= 0 or not motivo:
        flash('Loja, item, quantidade (>0) e motivo sao obrigatorios.', 'warning')
        return redirect(url_for('pedidos.estoque_loja', loja=loja_id or None))

    from app.services.estoque_helpers import serializar_loja
    serializar_loja(loja_id)  # lock por loja antes do UPDATE em EstoqueLoja

    filtro = {'loja_id': loja_id}
    if tipo == 'receita':
        filtro['receita_id'] = item_id
    elif tipo == 'produto':
        filtro['produto_id'] = item_id
    else:
        filtro['materia_prima_id'] = item_id
    el = EstoqueLoja.query.filter_by(**filtro).first()
    if not el:
        if operacao != 'entrada':
            flash('Item inexistente no estoque — so e possivel fazer entrada.', 'warning')
            return redirect(url_for('pedidos.estoque_loja', loja=loja_id))
        el = EstoqueLoja(**filtro)
        db.session.add(el)
        db.session.flush()

    if operacao == 'entrada':
        el.quantidade += qtd
        tipo_mov = 'entrada_manual'
        sinal = '+'
    else:
        el.quantidade = max(0, el.quantidade - qtd)
        tipo_mov = 'ajuste_negativo'
        sinal = '-'

    db.session.add(MovEstoqueLoja(
        estoque_loja_id=el.id, tipo=tipo_mov, quantidade=qtd,
        referencia=motivo, usuario_id=current_user.id,
    ))
    db.session.commit()
    flash(f'Ajuste de estoque registrado ({sinal}{qtd}).', 'success')
    return redirect(url_for('pedidos.estoque_loja', loja=loja_id))


@pedidos_bp.route('/estoque-loja/minimos', methods=['POST'])
@login_required
@gerente_required
def estoque_loja_minimos():
    """Salva o estoque MINIMO por item da loja (piso da sugestao de pedido
    loja->industria, motor venda+estoque). E CONFIG, nao movimento: nao mexe
    na quantidade nem gera MovEstoqueLoja — por isso nao precisa do lock de
    serializacao (que existe pra proteger UPDATE de saldo)."""
    loja_id = _loja_do_usuario()
    if current_user.is_admin():
        loja_id = int(request.form.get('loja_id') or 0)
    if not loja_id:
        flash('Selecione uma loja.', 'warning')
        return redirect(url_for('pedidos.estoque_loja'))

    ids = request.form.getlist('estoque_id[]')
    minimos = request.form.getlist('minimo[]')
    diarios = request.form.getlist('diario[]')
    venda_diaria_ids = set()
    for valor in request.form.getlist('venda_diaria[]'):
        try:
            venda_diaria_ids.add(int(valor))
        except (TypeError, ValueError):
            continue
    eids = []
    for eid in ids:
        try:
            eids.append(int(eid))
        except (TypeError, ValueError):
            eids.append(None)
    validos = [e for e in eids if e is not None]
    els = {e.id: e for e in EstoqueLoja.query.filter(
        EstoqueLoja.id.in_(validos)).all()} if validos else {}

    def _piso(lista, i):
        """0/vazio -> sem piso (NULL); ilegivel -> None = pula o campo."""
        raw = lista[i] if i < len(lista) else ''
        try:
            v = int(raw) if str(raw).strip() != '' else 0
        except (TypeError, ValueError):
            return 'pula'
        return v if v > 0 else None

    alterados = 0
    for i, eid in enumerate(eids):
        el = els.get(eid)
        if not el or el.loja_id != loja_id:      # so a loja do form
            continue
        novo = _piso(minimos, i)
        if novo != 'pula' and el.estoque_minimo != novo:
            el.estoque_minimo = novo
            alterados += 1
        # Pedido minimo DIARIO (piso incondicional — dono 17/08/2026).
        novo_d = _piso(diarios, i)
        if novo_d != 'pula' and el.pedido_minimo_diario != novo_d:
            el.pedido_minimo_diario = novo_d
            alterados += 1
        novo_venda_dia = eid in venda_diaria_ids
        if bool(el.reposicao_por_venda_diaria) != novo_venda_dia:
            el.reposicao_por_venda_diaria = novo_venda_dia
            alterados += 1
    if alterados:
        db.session.commit()
        flash('Regras de reposição atualizadas (%d %s).'
              % (alterados, 'ajuste' if alterados == 1 else 'ajustes'), 'success')
    else:
        flash('Nenhuma regra de reposição alterada.', 'info')
    return redirect(url_for('pedidos.estoque_loja', loja=loja_id))


# ── Desperdicio (sobra do dia / vencido) ──

@pedidos_bp.route('/desperdicio', methods=['GET', 'POST'])
@login_required
@gerente_required
def desperdicio():
    """Registra sobra do dia descartada (vencida) + lista historico.

    Form: loja, item (receita/produto/MP), quantidade, data (default hoje),
    observacao. Cria Desperdicio + MovEstoqueLoja(tipo='desperdicio')
    e baixa do estoque (limitado ao saldo, sem ficar negativo).
    """
    loja_id_user = _loja_do_usuario()
    pode_qualquer_loja = current_user.is_admin() or current_user.is_gerente()

    if request.method == 'POST':
        try:
            sel_loja = (int(request.form.get('loja_id') or 0)
                        if pode_qualquer_loja else loja_id_user)
        except (TypeError, ValueError):
            sel_loja = 0
        if not pode_qualquer_loja and sel_loja != current_user.loja_id:
            abort(403)
        if not sel_loja:
            flash('Selecione uma loja.', 'warning')
            return redirect(url_for('pedidos.desperdicio'))

        from app.services.estoque_helpers import serializar_loja
        serializar_loja(sel_loja)  # lock por loja antes da baixa do desperdicio

        raw = request.form.get('item_id', '')
        tipo_item, item_id = None, None
        try:
            if raw.startswith('r_'):
                tipo_item, item_id = 'receita', int(raw[2:])
            elif raw.startswith('mp_'):
                tipo_item, item_id = 'mp', int(raw[3:])
            elif raw.startswith('p_'):
                tipo_item, item_id = 'produto', int(raw[2:])
        except ValueError:
            tipo_item, item_id = None, None
        if not tipo_item or not item_id:
            flash('Selecione um item valido.', 'warning')
            return redirect(url_for('pedidos.desperdicio', loja=sel_loja))

        try:
            qtd = int(request.form.get('quantidade') or 0)
        except (TypeError, ValueError):
            qtd = 0
        if qtd <= 0:
            flash('Quantidade deve ser > 0.', 'warning')
            return redirect(url_for('pedidos.desperdicio', loja=sel_loja))

        data_str = request.form.get('data', '')
        try:
            data_desp = datetime.strptime(data_str, '%Y-%m-%d').date()
        except ValueError:
            data_desp = hoje_brt()

        observacao = (request.form.get('observacao') or '').strip() or None
        # Fonte única com o copilot (03/07/2026): motivo canônico ('vencido'
        # legado vira 'validade') + regra do reaproveitável — antes a tela
        # IGNORAVA a flag e o mesmo croissant baixava aqui e não no copilot.
        from app.services.desperdicio_core import (
            normalizar_motivo,
            reaproveita_sem_baixa,
        )
        motivo = normalizar_motivo(request.form.get('motivo'))
        reaproveita = reaproveita_sem_baixa(tipo_item, item_id, motivo)

        # CESTA: se for produto-cesta, baixa componentes em vez do produto
        componentes_cesta = []
        if tipo_item == 'produto' and not reaproveita:
            from app.models import Produto as _Produto
            from app.services.cestas import componentes_de_cesta
            produto = _Produto.query.get(item_id)
            componentes_cesta = componentes_de_cesta(produto)

        # Desperdicio criado ANTES dos movimentos: cada MovEstoqueLoja leva
        # `desperdicio_id` — e o que permite excluir o registro estornando
        # exatamente o que ele baixou (desperdicio_excluir).
        desp = Desperdicio(
            loja_id=sel_loja,
            receita_id=item_id if tipo_item == 'receita' else None,
            produto_id=item_id if tipo_item == 'produto' else None,
            materia_prima_id=item_id if tipo_item == 'mp' else None,
            quantidade=qtd,
            data=data_desp,
            motivo=motivo,
            observacao=observacao,
            criado_por_id=current_user.id,
        )
        db.session.add(desp)
        db.session.flush()

        conv = None
        if reaproveita:
            # Reaproveitável COM receita de retorno: converte no estoque da
            # loja (baixa o fresco + credita o retorno — decisão do dono
            # 03/07/2026). Sem retorno configurado: registro sem movimento,
            # como antes.
            baixa = 0
            if tipo_item == 'receita':
                from app.services.desperdicio_core import (
                    converter_sobra_para_retorno,
                )
                conv = converter_sobra_para_retorno(
                    sel_loja, item_id, qtd, current_user.id, desp.id)
            if conv:
                desp.observacao = ((observacao + ' ') if observacao else '') \
                    + f'[convertido em {conv["destino"]}]'
            else:
                desp.observacao = ((observacao + ' ') if observacao else '') \
                    + '[reaproveitavel — nao baixou estoque]'
        elif componentes_cesta:
            # Loja so estoca componentes — desconta cada um
            for col, comp_id, nome_comp, qtd_por_cesta in componentes_cesta:
                qtd_baixar = int(round(qtd * qtd_por_cesta))
                if qtd_baixar <= 0:
                    continue
                filtro_c = {'loja_id': sel_loja, col: comp_id}
                el_c = EstoqueLoja.query.filter_by(**filtro_c).first()
                if not el_c:
                    el_c = EstoqueLoja(**filtro_c, quantidade=0)
                    db.session.add(el_c)
                    db.session.flush()
                saldo_c = el_c.quantidade or 0
                baixa_c = min(qtd_baixar, saldo_c)
                el_c.quantidade = saldo_c - baixa_c
                db.session.add(MovEstoqueLoja(
                    estoque_loja_id=el_c.id, tipo='desperdicio',
                    quantidade=baixa_c,
                    referencia=f'Desperdicio cesta [{produto.nome}] {nome_comp}',
                    usuario_id=current_user.id,
                    desperdicio_id=desp.id,
                ))
            # Desperdicio "cabeca" aponta pra cesta (rastreabilidade)
        else:
            filtro = {'loja_id': sel_loja}
            if tipo_item == 'receita':
                filtro['receita_id'] = item_id
            elif tipo_item == 'produto':
                filtro['produto_id'] = item_id
            elif tipo_item == 'mp':
                filtro['materia_prima_id'] = item_id

            el = EstoqueLoja.query.filter_by(**filtro).first()
            if not el:
                el = EstoqueLoja(**filtro, quantidade=0)
                db.session.add(el)
                db.session.flush()

            saldo = el.quantidade or 0
            baixa = min(qtd, saldo)
            el.quantidade = saldo - baixa

        # Pra cestas, a baixa por componente ja foi feita no if acima; item
        # reaproveitavel NAO gera movimento nenhum (nem falta — a decisao de
        # nao baixar e da regra, nao falta de saldo).
        if not reaproveita and not componentes_cesta and baixa > 0:
            db.session.add(MovEstoqueLoja(
                estoque_loja_id=el.id, tipo='desperdicio', quantidade=baixa,
                referencia=f'Desperdicio {motivo}'
                + (f' — {observacao}' if observacao else ''),
                usuario_id=current_user.id,
                desperdicio_id=desp.id,
            ))
        if not reaproveita and not componentes_cesta and qtd > baixa:
            falta = qtd - baixa
            db.session.add(MovEstoqueLoja(
                estoque_loja_id=el.id, tipo='desperdicio_sem_estoque',
                quantidade=falta,
                referencia=f'Desperdicio {motivo} — registrado sem estoque ({falta})',
                usuario_id=current_user.id,
                desperdicio_id=desp.id,
            ))
        db.session.commit()
        if reaproveita and conv:
            aviso_falta = (f' (saldo do fresco estava subcontado em '
                           f'{conv["faltou"]})' if conv['faltou'] else '')
            flash(f'Desperdicio registrado: {qtd} un de {desp.nome_item} '
                  f'convertida(s) em "{conv["destino"]}" no estoque da '
                  f'loja{aviso_falta}.', 'info')
        elif reaproveita:
            flash(f'Desperdicio registrado: {qtd} un de {desp.nome_item} — '
                  'item reaproveitavel: o estoque NAO foi baixado (vira '
                  'retorno/outra receita).', 'info')
        else:
            flash(f'Desperdicio registrado: {qtd} un de {desp.nome_item}.',
                  'success')
        return redirect(url_for('pedidos.desperdicio', loja=sel_loja))

    # GET: form + lista
    if pode_qualquer_loja:
        sel = request.args.get('loja')
        loja_filtro = int(sel) if sel else None
    else:
        loja_filtro = loja_id_user

    lojas = _lojas_operacionais()
    loja = Loja.query.get(loja_filtro) if loja_filtro else None

    receitas = Receita.ativas().order_by(Receita.categoria, Receita.nome).all()
    produtos = Produto.query.filter_by(ativo=True).order_by(Produto.nome).all()
    materias = MateriaPrima.ativas().order_by(MateriaPrima.nome).all()

    # Historico: 30 dias
    desde = hoje_brt() - timedelta(days=30)
    q = (Desperdicio.query
         .filter(Desperdicio.data >= desde)
         .order_by(Desperdicio.data.desc(), Desperdicio.criado_em.desc()))
    if loja_filtro:
        q = q.filter(Desperdicio.loja_id == loja_filtro)
    registros = q.limit(200).all()

    return render_template('pedidos/desperdicio.html',
                           lojas=lojas, loja=loja, sel_loja=loja_filtro,
                           receitas=receitas, produtos=produtos, materias=materias,
                           registros=registros, hoje=hoje_brt().isoformat())


@pedidos_bp.route('/desperdicio/<int:id>/excluir', methods=['POST'])
@login_required
@admin_required
def desperdicio_excluir(id):
    """Exclui registro de desperdicio estornando EXATAMENTE o que ele baixou.

    O estorno le os MovEstoqueLoja vinculados por `desperdicio_id` (cobre
    baixa parcial, cesta com baixa por componente e reaproveitavel sem
    baixa). Registro ANTIGO (anterior a coluna, sem movimento vinculado) e
    excluido SEM mexer em estoque — creditar `desp.quantidade` as cegas
    criava estoque fantasma: reaproveitavel nunca baixou, parcial baixou
    menos, cesta baixou nos componentes (e nao no produto-cesta)."""
    desp = Desperdicio.query.get_or_404(id)
    loja_id = desp.loja_id

    from app.services.desperdicio_core import (
        TIPO_CONVERSAO_ENTRADA,
        TIPO_CONVERSAO_SAIDA,
    )
    from app.services.estoque_helpers import serializar_loja
    serializar_loja(loja_id)  # lock por loja antes dos UPDATE do estorno

    movs = MovEstoqueLoja.query.filter_by(desperdicio_id=desp.id).all()
    estornado = 0
    avisos = []
    for m in movs:
        if (m.tipo in ('desperdicio', TIPO_CONVERSAO_SAIDA)
                and (m.quantidade or 0) > 0):
            # Saida (baixa do desperdicio OU do fresco na conversao):
            # devolve na mesma linha.
            el = EstoqueLoja.query.get(m.estoque_loja_id)
            if el:
                el.quantidade = (el.quantidade or 0) + m.quantidade
                db.session.add(MovEstoqueLoja(
                    estoque_loja_id=el.id, tipo='desperdicio_estorno',
                    quantidade=m.quantidade,
                    referencia=f'Estorno desperdicio #{desp.id}',
                    usuario_id=current_user.id,
                ))
                estornado += m.quantidade
        elif (m.tipo == TIPO_CONVERSAO_ENTRADA
                and (m.quantidade or 0) > 0):
            # Entrada no retorno: o reverso e BAIXAR — limitado ao saldo
            # (parte pode ja ter sido coletada/vendida como Nutella).
            el = EstoqueLoja.query.get(m.estoque_loja_id)
            if el:
                saldo = el.quantidade or 0
                baixa = min(m.quantidade, saldo)
                el.quantidade = saldo - baixa
                if baixa > 0:
                    db.session.add(MovEstoqueLoja(
                        estoque_loja_id=el.id, tipo='desperdicio_estorno',
                        quantidade=baixa,
                        referencia=(f'Estorno desperdicio #{desp.id} — '
                                    'reverte conversao de sobra'),
                        usuario_id=current_user.id,
                    ))
                    estornado += baixa
                if baixa < m.quantidade:
                    avisos.append(
                        f'retorno ja consumido: {m.quantidade - baixa} un '
                        'nao puderam ser revertidas')
        # '*_sem_estoque' nao mexeu em saldo — nada a devolver.
        # Solta o vinculo antes do delete (portavel: SQLite local nao aplica
        # o ON DELETE SET NULL do Postgres).
        m.desperdicio_id = None

    db.session.delete(desp)
    db.session.commit()
    for a in avisos:
        flash(f'Atencao: {a}.', 'warning')
    if estornado:
        flash(f'Desperdicio excluido — estoque estornado '
              f'({estornado} un movimentada(s)).', 'success')
    elif movs or '[reaproveitavel' in (desp.observacao or ''):
        flash('Desperdicio excluido — este registro nao tinha baixado '
              'estoque, nada a estornar.', 'info')
    else:
        flash('Desperdicio excluido. Registro antigo, sem movimento '
              'vinculado — o estoque NAO foi alterado; se este registro '
              'baixou estoque, faca a entrada manual correspondente.',
              'warning')
    return redirect(url_for('pedidos.desperdicio', loja=loja_id))


# ── Vendas manuais loja (sem PDV API) + sugestao de pedido ──

@pedidos_bp.route('/lojas/<int:loja_id>/vendas-manuais', methods=['GET', 'POST'])
@login_required
@admin_required
def vendas_manuais(loja_id):
    """Lanca vendas manuais de uma loja (sem API PDV). Texto colado igual
    balanco. NAO baixa estoque — so registra pra previsao/sugestao."""
    from app.models import VendaManualLoja
    from app.services import vendas_manuais as svc
    loja = Loja.query.get_or_404(loja_id)
    parsed = None
    resultado = None

    if request.method == 'POST':
        acao = request.form.get('acao')
        texto = (request.form.get('texto') or '').strip()
        data_str = (request.form.get('data_venda') or '').strip()
        try:
            data_venda = date.fromisoformat(data_str) if data_str else hoje_brt()
        except ValueError:
            data_venda = hoje_brt()

        parseados = svc.parsear_lista(texto)
        resolvidos = svc.resolver_lista(parseados, loja_id) if parseados else []

        if acao == 'aplicar' and resolvidos:
            resultado = svc.aplicar_vendas_manuais(
                resolvidos, loja_id, data_venda, current_user,
            )
            flash(f'{len(resultado["aplicados"])} venda(s) lançada(s), '
                  f'{len(resultado["ignorados"])} ignorada(s).', 'success')
            return redirect(url_for('pedidos.vendas_manuais', loja=loja_id))
        parsed = {'data_venda': data_venda.isoformat(),
                  'texto': texto, 'itens': resolvidos}

    # Resumo agregado por data — mostra TODAS as datas com totais, em vez
    # de uma listagem que cortava em 50 ultimas (confundia: parecia que so
    # tinha lancado um dia).
    from sqlalchemy import func as sa_func
    resumo_q = (db.session.query(
                    VendaManualLoja.data_venda.label('data'),
                    sa_func.count(VendaManualLoja.id).label('n_itens'),
                    sa_func.sum(VendaManualLoja.quantidade).label('qtd_total'),
                )
                .filter(VendaManualLoja.loja_id == loja_id)
                .group_by(VendaManualLoja.data_venda)
                .order_by(VendaManualLoja.data_venda.desc())
                .all())
    resumo_datas = [{'data': r.data, 'n_itens': r.n_itens or 0,
                     'qtd_total': int(r.qtd_total or 0)}
                    for r in resumo_q]
    total_geral = {
        'n_vendas': sum(r['n_itens'] for r in resumo_datas),
        'qtd_total': sum(r['qtd_total'] for r in resumo_datas),
        'n_dias': len(resumo_datas),
    }
    # Filtra historico detalhado por uma data especifica se solicitado
    filtro_data = request.args.get('detalhe_data')
    detalhe = []
    if filtro_data:
        try:
            d = date.fromisoformat(filtro_data)
            detalhe = (VendaManualLoja.query
                       .filter_by(loja_id=loja_id, data_venda=d)
                       .order_by(VendaManualLoja.id.desc()).all())
        except ValueError:
            pass

    return render_template('pedidos/vendas_manuais.html', loja=loja,
                            parsed=parsed, resultado=resultado,
                            resumo_datas=resumo_datas,
                            total_geral=total_geral,
                            detalhe=detalhe,
                            filtro_data=filtro_data,
                            hoje=hoje_brt().isoformat())


@pedidos_bp.route('/lojas/<int:loja_id>/sugerir-pedido', methods=['GET', 'POST'])
@login_required
@admin_required
def sugerir_pedido(loja_id):
    """Mostra sugestao de pedido baseada em vendas (reais + manuais) +
    estoque atual. POST cria pedido com as qtds informadas."""
    from app.services import vendas_manuais as svc
    loja = Loja.query.get_or_404(loja_id)

    if request.method == 'POST':
        try:
            data_entrega = date.fromisoformat(request.form.get('data_entrega') or '')
        except ValueError:
            flash('Data invalida.', 'danger')
            return redirect(url_for('pedidos.sugerir_pedido', loja_id=loja_id))
        # Corte do fim do dia: a rota é admin (passa), mas o aviso de que o
        # pré-preparo de amanhã já foi calculado vai junto — mesmo contrato
        # dos outros caminhos de escrita (defesa em profundidade).
        from app.services.pedido_corte import bloqueio_do_corte
        bloqueado_corte, aviso_corte = bloqueio_do_corte(
            [data_entrega], user=current_user)
        if bloqueado_corte:
            flash(aviso_corte, 'warning')
            return redirect(url_for('pedidos.sugerir_pedido',
                                    loja_id=loja_id))
        if aviso_corte:
            flash(aviso_corte, 'warning')
        refs = request.form.getlist('item_ref[]')
        qtds = request.form.getlist('item_qtd[]')
        itens = []
        for i, ref in enumerate(refs):
            ref = (ref or '').strip()
            if not ref or ':' not in ref:
                continue
            tipo, _, sid = ref.partition(':')
            try:
                qtd = int(qtds[i])
            except (IndexError, ValueError):
                continue
            if qtd <= 0 or tipo not in ('receita', 'produto', 'mp') or not sid.isdigit():
                continue
            itens.append({'tipo': tipo, 'id': int(sid), 'quantidade': qtd})
        if not itens:
            flash('Nenhum item com quantidade > 0.', 'warning')
            return redirect(url_for('pedidos.sugerir_pedido', loja_id=loja_id))
        itens_norm = [{
            'receita_id': it['id'] if it['tipo'] == 'receita' else None,
            'produto_id': it['id'] if it['tipo'] == 'produto' else None,
            'materia_prima_id': it['id'] if it['tipo'] == 'mp' else None,
            'quantidade': it['quantidade'], 'estado': None, 'observacao': None,
        } for it in itens]

        from app.services.pedido_merge import (
            absorver_rascunho_automatico,
            adotar_rascunho_automatico,
            mesclar_itens,
            pedido_aberto_para_merge,
            rascunho_automatico_aberto,
        )
        alvo = pedido_aberto_para_merge(loja_id, data_entrega, 'confirmado')
        if alvo:
            mesclar_itens(alvo, itens_norm, modificado_por_id=current_user.id)
            absorvido = absorver_rascunho_automatico(
                loja_id, data_entrega, current_user.id)
            db.session.commit()
            flash(f'Itens adicionados ao pedido #{alvo.id} — ja existia '
                  'para esta loja nesta data.', 'success')
            if absorvido is not None:
                flash(f'O rascunho automático #{absorvido.id} do mesmo dia '
                      'foi cancelado — o pedido da loja manda.', 'info')
            return redirect(url_for('pedidos.detalhe', id=alvo.id))

        # Dia coberto pelo cron de auto-pedidos: adota o rascunho (mesma
        # regra do /pedidos/novo — nunca um segundo pedido no mesmo dia).
        rascunho = rascunho_automatico_aberto(loja_id, data_entrega)
        if rascunho is not None:
            res_adote = adotar_rascunho_automatico(
                rascunho, itens_norm, current_user.id)
            db.session.commit()
            flash(f'Pedido #{rascunho.id} confirmado a partir da sugestão '
                  'automática do dia: suas quantidades substituíram as '
                  'sugeridas.', 'success')
            if res_adote['mantidos']:
                flash(f'{res_adote["mantidos"]} item(ns) da sugestão '
                      'automática que você não citou foram MANTIDOS no '
                      'pedido — revise e ajuste se não quiser.', 'warning')
            return redirect(url_for('pedidos.detalhe', id=rascunho.id))

        pedido = PedidoLoja(loja_id=loja_id, data_entrega=data_entrega,
                            criado_por=current_user.id, status='confirmado')
        db.session.add(pedido)
        db.session.flush()
        for it in itens_norm:
            db.session.add(PedidoItem(pedido_id=pedido.id, **it))
        db.session.commit()
        flash(f'Pedido #{pedido.id} criado a partir da sugestao.', 'success')
        return redirect(url_for('pedidos.detalhe', id=pedido.id))

    # Periodo: default = ultimos 14 dias
    di_str = request.args.get('inicio') or ''
    df_str = request.args.get('fim') or ''
    try:
        data_inicio = date.fromisoformat(di_str) if di_str else hoje_brt() - timedelta(days=14)
    except ValueError:
        data_inicio = hoje_brt() - timedelta(days=14)
    try:
        data_fim = date.fromisoformat(df_str) if df_str else hoje_brt()
    except ValueError:
        data_fim = hoje_brt()
    try:
        dias_cobertura = int(request.args.get('cobertura', 7))
    except ValueError:
        dias_cobertura = 7
    res = svc.sugerir_pedido(loja_id, data_inicio=data_inicio,
                              data_fim=data_fim,
                              dias_cobertura=dias_cobertura)
    sugestao = res.get('itens', [])
    aviso_vnda = res.get('aviso_vnda')
    dias_periodo = (data_fim - data_inicio).days + 1
    return render_template('pedidos/sugerir_pedido.html', loja=loja,
                            sugestao=sugestao,
                            aviso_vnda=aviso_vnda,
                            data_inicio=data_inicio.isoformat(),
                            data_fim=data_fim.isoformat(),
                            dias_periodo=dias_periodo,
                            dias_cobertura=dias_cobertura,
                            amanha=(hoje_brt() + timedelta(days=1)).isoformat())


@pedidos_bp.route('/lojas/<int:loja_id>/vendas-manuais/template.xlsx')
@login_required
@admin_required
def vendas_manuais_template(loja_id):
    """Download da planilha modelo pra preencher e fazer upload depois."""
    import io

    from flask import send_file

    from app.services import vendas_manuais as svc
    loja = Loja.query.get_or_404(loja_id)
    blob = svc.gerar_template_xlsx(loja)
    nome = f'vendas_{loja.nome.lower().replace(" ", "_")}_modelo.xlsx'
    return send_file(io.BytesIO(blob),
                      as_attachment=True, download_name=nome,
                      mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@pedidos_bp.route('/lojas/<int:loja_id>/vendas-manuais/upload', methods=['POST'])
@login_required
@admin_required
def vendas_manuais_upload(loja_id):
    """Recebe upload de xlsx + aplica em lote. Linhas com erro vao pra
    ignorados (mostra no flash)."""
    from app.services import vendas_manuais as svc
    loja = Loja.query.get_or_404(loja_id)
    f = request.files.get('planilha')
    if not f or not f.filename:
        flash('Selecione um arquivo .xlsx.', 'danger')
        return redirect(url_for('pedidos.vendas_manuais', loja_id=loja_id))
    if not f.filename.lower().endswith('.xlsx'):
        flash('So aceita arquivo .xlsx (Excel).', 'danger')
        return redirect(url_for('pedidos.vendas_manuais', loja_id=loja_id))

    parseados = svc.parsear_xlsx(f, loja_id)
    resultado = svc.aplicar_vendas_xlsx(parseados, loja_id, current_user)

    n_ok = len(resultado['aplicados'])
    n_ign = len(resultado['ignorados'])
    n_datas = len(resultado['datas_unicas'])
    if n_ok:
        flash(f'{n_ok} venda(s) lançadas em {n_datas} data(s) distinta(s). '
              f'{n_ign} linha(s) ignoradas.', 'success')
    else:
        flash(f'Nenhuma venda lançada. {n_ign} linha(s) ignoradas. '
              'Verifique formato (Data, Produto, Qtd) e nomes do catálogo.',
              'warning')

    # Salva ignorados na session pra mostrar na tela depois (caso queira)
    if resultado['ignorados']:
        from flask import session
        session['vendas_manuais_ultimos_ignorados'] = resultado['ignorados'][:50]
    return redirect(url_for('pedidos.vendas_manuais', loja_id=loja_id))


@pedidos_bp.route('/lojas/<int:loja_id>/vendas-manuais/limpar', methods=['POST'])
@login_required
@admin_required
def vendas_manuais_limpar(loja_id):
    """Apaga TODAS as vendas manuais de uma loja. Pra refazer upload do
    zero se subiu errado. Exige confirmacao no front (onsubmit)."""
    from app.models import VendaManualLoja
    n = VendaManualLoja.query.filter_by(loja_id=loja_id).delete()
    db.session.commit()
    flash(f'{n} venda(s) manual(is) apagada(s). Pode refazer o upload.', 'warning')
    return redirect(url_for('pedidos.vendas_manuais', loja_id=loja_id))


@pedidos_bp.route('/lojas/<int:loja_id>/sugerir-pedido/xlsx')
@login_required
@admin_required
def sugerir_pedido_xlsx(loja_id):
    """Baixa a sugestao de pedido em xlsx. Mesmos params da tela
    (?inicio=YYYY-MM-DD&fim=YYYY-MM-DD&cobertura=N)."""
    import io

    from flask import send_file

    from app.services import vendas_manuais as svc
    loja = Loja.query.get_or_404(loja_id)

    di_str = request.args.get('inicio') or ''
    df_str = request.args.get('fim') or ''
    try:
        data_inicio = date.fromisoformat(di_str) if di_str else hoje_brt() - timedelta(days=14)
    except ValueError:
        data_inicio = hoje_brt() - timedelta(days=14)
    try:
        data_fim = date.fromisoformat(df_str) if df_str else hoje_brt()
    except ValueError:
        data_fim = hoje_brt()
    try:
        dias_cobertura = int(request.args.get('cobertura', 7))
    except ValueError:
        dias_cobertura = 7

    res = svc.sugerir_pedido(loja_id, data_inicio=data_inicio,
                              data_fim=data_fim,
                              dias_cobertura=dias_cobertura)
    itens = res.get('itens', [])
    blob = svc.exportar_sugestao_xlsx(loja, itens, data_inicio, data_fim,
                                        dias_cobertura)
    nome = f'sugestao_{loja.nome.lower().replace(" ", "_")}_{data_inicio}_{data_fim}.xlsx'
    return send_file(io.BytesIO(blob), as_attachment=True, download_name=nome,
                      mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


# ── Retiradas de sobras: lista web + QR de coleta + cancelamento ─────────
# (10/07/2026 — retiradas cujo QR de coleta expirava em 48h ficavam presas
# em `aguardando_coleta` pra sempre: sem tela pra regenerar o QR e sem
# caminho de cancelar. Era o pendente documentado do ciclo de sobras.)

@pedidos_bp.route('/retiradas')
@login_required
@gerente_required
def retiradas_sobras():
    """Lista as retiradas de sobras: abertas (aguardando coleta / em
    transporte, qualquer data) + as últimas finalizadas. A loja usa esta
    tela pra mostrar o QR de coleta ao motorista (regenerado se expirou)."""
    from app.models import RetiradaSobra
    abertas = (RetiradaSobra.query
               .filter(RetiradaSobra.status.in_(('aguardando_coleta',
                                                 'em_transporte')))
               .order_by(RetiradaSobra.data_retirada,
                         RetiradaSobra.id).all())
    finalizadas = (RetiradaSobra.query
                   .filter(RetiradaSobra.status.in_(('recebida',
                                                     'cancelada')))
                   .order_by(RetiradaSobra.id.desc())
                   .limit(20).all())
    return render_template('pedidos/retiradas.html', abertas=abertas,
                           finalizadas=finalizadas)


@pedidos_bp.route('/retiradas/<int:id>/qr-coleta', methods=['POST'])
@login_required
@gerente_required
def retirada_qr_coleta(id):
    """Mostra o QR de COLETA da retirada (o motorista escaneia NA LOJA).

    `gerar_qr_retirada` reusa o QR ativo ou emite um novo — é o caminho de
    destravar retirada cujo QR original (TTL 48h) expirou."""
    from app.models import RetiradaSobra
    from app.services.handshake_qr import gerar_qr_retirada
    from app.services.qrcode_svc import gerar_png_data_url

    ret = RetiradaSobra.query.get_or_404(id)
    if ret.status != 'aguardando_coleta':
        flash(f'Retirada #{ret.id} não está aguardando coleta '
              f'(status: {ret.status}).', 'warning')
        return redirect(url_for('pedidos.retiradas_sobras'))
    try:
        qr = gerar_qr_retirada(ret, 'coleta', current_user.id)
        db.session.commit()
        url = url_for('handshake.handshake_retirada', token=qr.token,
                      _external=True)
        qr_png = gerar_png_data_url(url)
    except Exception:
        db.session.rollback()
        current_app.logger.exception('retirada_qr_coleta falhou (retirada=%s)', id)
        flash('Erro ao gerar o QR. O log foi registrado — avise o admin.',
              'danger')
        return redirect(url_for('pedidos.retiradas_sobras'))
    return render_template('pedidos/retirada_qr_coleta.html', retirada=ret,
                           qr_png=qr_png, url=url)


def _audit_retirada_web(ret, tipo, etapa, detalhe):
    """Audita gesto administrativo da retirada — delega pro helper do
    service (sessão isolada, best-effort), compartilhado com a tela do
    padeiro."""
    from app.services.devolucao import auditar_gesto_retirada
    auditar_gesto_retirada(ret, tipo, etapa, detalhe)


@pedidos_bp.route('/retiradas/<int:id>/receber-manual', methods=['POST'])
@login_required
@admin_required
def retirada_receber_manual(id):
    """Destrava de baixa presa (19/07/2026): confirma o recebimento na
    indústria SEM o QR — pra retirada em transporte cuja mercadoria chegou
    mas ninguém escaneou (motorista foi embora, QR expirado). Aceita
    conferência por item (`qtd_<item_id>`; vazio/inválido = usa a coletada,
    mesmo contrato do form da coleta)."""
    from app.models import RetiradaSobra
    from app.services.devolucao import receber_retirada_manual

    ret = RetiradaSobra.query.get_or_404(id)
    quantidades = {}
    for it in ret.itens:
        bruto = (request.form.get(f'qtd_{it.id}') or '').strip()
        if bruto:
            try:
                quantidades[it.id] = int(bruto)
            except ValueError:
                pass
    try:
        resumo = receber_retirada_manual(ret, current_user.id, quantidades)
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        flash(f'Não recebi: {exc}', 'warning')
        return redirect(url_for('pedidos.retiradas_sobras'))
    _audit_retirada_web(ret, 'r_receb', 'manual',
                        f'recebimento manual por usuario {current_user.id}')
    partes = [f"{r['qtd']}× {r.get('destino') or r['nome']}"
              for r in resumo if 'erro' not in r]
    erros = [r['nome'] for r in resumo if 'erro' in r]
    msg = (f'Retirada #{ret.id} recebida manualmente — indústria creditada'
           + (f" ({', '.join(partes)})" if partes else ' (nada a creditar)')
           + '.')
    if erros:
        msg += f" Itens sem cadastro (não creditados): {', '.join(erros)}."
    flash(msg, 'success' if not erros else 'warning')
    return redirect(url_for('pedidos.retiradas_sobras'))


@pedidos_bp.route('/retiradas/<int:id>/cancelar', methods=['POST'])
@login_required
@admin_required
def retirada_cancelar(id):
    """Cancela retirada em aberto. Antes da coleta nada foi baixado (o
    retorno segue no estoque da loja); em transporte (19/07/2026) estorna a
    baixa da coleta — pra mercadoria que nunca chegou/voltou pra loja. Se
    ela CHEGOU na indústria, o gesto é o recebimento manual."""
    from app.models import RetiradaSobra
    from app.services.devolucao import cancelar_retirada

    ret = RetiradaSobra.query.get_or_404(id)
    estava_transporte = ret.status == 'em_transporte'
    try:
        avisos = cancelar_retirada(ret, usuario_id=current_user.id)
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        flash(f'Não cancelei: {exc}', 'warning')
        return redirect(url_for('pedidos.retiradas_sobras'))
    if estava_transporte:
        _audit_retirada_web(ret, 'r_coleta', 'cancel_estorno',
                            f'cancelada em transporte por usuario '
                            f'{current_user.id}')
        detalhe = ('; '.join(avisos) if avisos
                   else 'a coleta não tinha baixado nada')
        flash(f'Retirada #{ret.id} cancelada com estorno da coleta — '
              f'{detalhe}.', 'success')
    else:
        flash(f'Retirada #{ret.id} cancelada — o retorno segue no estoque '
              'da loja (as vendas de Nutella continuam baixando dali).',
              'success')
    return redirect(url_for('pedidos.retiradas_sobras'))


# ── NF de TRANSFERÊNCIA indústria→loja (20/07/2026) ─────────────────────────
# Emitida best-effort no scan do QR de saída (handshake); aqui ficam a
# reemissão manual (Tiny fora do ar na hora do scan) e o mapa de SKUs do
# canal 'transf' (fallback site→b2b — a tela é só pra exceções e MPs).

@pedidos_bp.route('/<int:id>/emitir-nf', methods=['POST'])
@login_required
@operacional_pedido_required
def emitir_nf_transferencia(id):
    """(Re)emite a NF de transferência do pedido. `recriar=1` refaz do
    zero (mesma semântica do B2B — risco de duplicidade é do gesto)."""
    from app.services import tiny_nf_transf
    pedido = PedidoLoja.query.get_or_404(id)
    recriar = request.form.get('recriar') == '1'
    res = tiny_nf_transf.emitir_nf(pedido, user_id=current_user.id,
                                   recriar=recriar)
    flash(('NF de transferência: %s' % res.get('msg', 'ok')),
          'success' if res.get('ok') else 'danger')
    return redirect(request.referrer
                    or url_for('pedidos.detalhe', id=pedido.id))


@pedidos_bp.route('/<int:id>/nf-dispensar', methods=['POST'])
@login_required
@admin_required
def nf_dispensar(id):
    """Liga/desliga a dispensa de NF DESTE pedido. ADMIN-only de propósito
    (decisão do dono 20/07/2026: 'não posso dar essa opção para o
    motorista e o padeiro') — as telas operacionais só obedecem."""
    pedido = PedidoLoja.query.get_or_404(id)
    pedido.nf_dispensada = not pedido.nf_dispensada
    db.session.commit()
    if pedido.nf_dispensada:
        aviso = 'NF de transferência DISPENSADA para este pedido.'
        if pedido.nf_emitida_em:
            aviso += (' Atenção: já existe NF emitida '
                      f'(nº {pedido.nf_numero or pedido.tiny_nota_fiscal_id})'
                      ' — a dispensa não cancela a nota (isso é no Tiny).')
        flash(aviso, 'warning')
    else:
        flash('NF de transferência REATIVADA para este pedido.', 'success')
    return redirect(url_for('pedidos.detalhe', id=pedido.id))


@pedidos_bp.route('/<int:id>/danfe')
@login_required
@operacional_pedido_required
def danfe_transferencia(id):
    """DANFE da NF de transferência (link temporário do Tiny, resolvido
    sob demanda — mesmo padrão do B2B)."""
    from app.services import tiny
    pedido = PedidoLoja.query.get_or_404(id)
    if not pedido.tiny_nota_fiscal_id:
        flash('Este pedido ainda não tem NF de transferência emitida.',
              'warning')
        return redirect(url_for('pedidos.detalhe', id=pedido.id))
    link, motivo = tiny.obter_link_nota_fiscal_com_motivo(
        pedido.tiny_nota_fiscal_id)
    if not link:
        flash(f'DANFE ainda não disponível: {motivo}', 'warning')
        return redirect(url_for('pedidos.detalhe', id=pedido.id))
    return redirect(link)


@pedidos_bp.route('/tiny-skus-transferencia')
@owner_required
def tiny_skus_transferencia():
    """Mapa de SKUs do canal 'transf'. A emissão HERDA o SKU do site→b2b
    quando não há registro aqui — a tela mostra o herdado e serve pra
    exceção e pra MP (que não existe nos outros canais)."""
    from app.services import tiny_nf, tiny_nf_transf
    itens = tiny_nf.itens_para_mapear(canal='transf')
    for it in itens:
        if not it['sku'] and it['kind'] in ('receita', 'produto'):
            herdado = tiny_nf_transf.sku_transferencia(it['kind'], it['id'])
            if herdado:
                it['sku_herdado'] = herdado
    pendentes = sum(1 for i in itens
                    if i['estado'] != 'mapeado' and not i.get('sku_herdado'))
    return render_template(
        'tiny_skus.html', itens=itens, pendentes=pendentes,
        total=len(itens),
        titulo='SKUs do Tiny (NF-e) — Transferência indústria → loja',
        descricao='A NF de transferência HERDA o SKU do site (e depois do '
                  'B2B) quando não há SKU próprio aqui — mapeie só as '
                  'exceções e as matérias-primas (que não existem nos '
                  'outros canais).',
        url_definir=url_for('pedidos.tiny_definir_transf'),
        url_sync=url_for('pedidos.tiny_sync_transf'),
        url_importar=url_for('pedidos.tiny_importar_transf'),
        vazio_msg='Nenhum item pedível ainda.')


@pedidos_bp.route('/tiny-skus-transferencia/definir', methods=['POST'])
@owner_required
def tiny_definir_transf():
    from app.services import tiny_nf
    kind = (request.form.get('kind') or '').strip()
    try:
        item_id = int(request.form.get('item_id'))
    except (TypeError, ValueError):
        flash('Item inválido.', 'warning')
        return redirect(url_for('pedidos.tiny_skus_transferencia'))
    sku = (request.form.get('sku') or '').strip()
    tiny_nf.definir_sku(kind, item_id, sku, user_id=current_user.id,
                        canal='transf')
    flash('SKU salvo.' if sku else 'SKU removido.', 'success')
    return redirect(url_for('pedidos.tiny_skus_transferencia'))


@pedidos_bp.route('/tiny-skus-transferencia/sync', methods=['POST'])
@owner_required
def tiny_sync_transf():
    from app.services import tiny_nf
    res = tiny_nf.sincronizar_sugestoes(user_id=current_user.id,
                                        canal='transf')
    if res.get('erro'):
        flash(f'Sincronização falhou: {res["erro"]}', 'danger')
    else:
        flash(f'{res.get("exatos", 0)} confirmados (nome idêntico) + '
              f'{res.get("sugeridos", 0)} sugeridos pra conferir, '
              f'{res.get("sem_match", 0)} sem correspondência '
              f'({res.get("total_tiny", 0)} produtos no Tiny).', 'success')
    return redirect(url_for('pedidos.tiny_skus_transferencia'))


@pedidos_bp.route('/tiny-skus-transferencia/importar', methods=['POST'])
@owner_required
def tiny_importar_transf():
    from app.services import tiny_nf
    f = request.files.get('planilha')
    if not f or not f.filename:
        flash('Selecione a planilha de produtos do Tiny (.xls ou .csv).',
              'warning')
        return redirect(url_for('pedidos.tiny_skus_transferencia'))
    conteudo = f.read()
    res = tiny_nf.importar_planilha(conteudo, f.filename,
                                    user_id=current_user.id, canal='transf')
    if res.get('erro'):
        flash(res['erro'], 'danger')
    else:
        flash(f'Planilha importada: {res.get("exatos", 0)} confirmados '
              f'(nome idêntico) + {res.get("sugeridos", 0)} sugeridos pra '
              f'conferir, {res.get("sem_match", 0)} sem correspondência '
              f'({res.get("total", 0)} linhas).', 'success')
    return redirect(url_for('pedidos.tiny_skus_transferencia'))
