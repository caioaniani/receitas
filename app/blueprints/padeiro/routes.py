"""Tela touchscreen do padeiro (chao de fabrica).

Fluxo (botoes grandes):
1. Pedidos do dia pra separar  → botao SEPARAR.
2. Pedido separado, motorista chegou → padeiro escolhe o motorista → GERAR QR.

Reusa a logica existente: status 'separado' (igual `pedidos.separar`), helper
`handshake_qr.gerar_qr_saida` e o handshake em `/handshake/<token>`.
"""
import logging
from collections import Counter
from datetime import datetime, timedelta

from flask import flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.blueprints.padeiro import padeiro_bp
from app.decorators import admin_required, padeiro_required
from app.extensions import csrf, db
from app.models import Driver, PedidoLoja, VendaB2B
from app.utils import hoje

logger = logging.getLogger(__name__)

_A_SEPARAR = ('pendente', 'confirmado')

# Pedido do site ATIVO pra fila do padeiro (sob encomenda): pago e ainda nao
# entregue/cancelado. Fora: aguardando_pagamento (pode expirar sem pagar).
_STATUS_ONLINE_ATIVO = ('pago', 'em_preparo', 'a_caminho')
# Pra PRODUÇÃO, a DIVULGAÇÃO entra junto (23/08/2026, caso pedido 84F17F68:
# Caixa de Mini de cortesia pra segunda-feira invisível no pré-preparo — "não
# está aparecendo na tela de pré-preparo para o padeiro tirar para
# fermentar"). O "divulgação fora" vale pra FATURAMENTO/previsão, não pra
# produzir: item sob encomenda de cortesia precisa ser assado igual.
_STATUS_ONLINE_PRODUCAO = _STATUS_ONLINE_ATIVO + ('divulgacao',)


def _eager_itens_receita():
    """Eager load dos itens do plano + receita (N+1 do Sentry 13/07/2026:
    a TV do padeiro consulta o plano a cada 15s e cada item disparava 1
    SELECT de receita)."""
    from sqlalchemy.orm import selectinload

    from app.models import PlanejamentoItem, PlanejamentoProducao
    return (selectinload(PlanejamentoProducao.itens)
            .selectinload(PlanejamentoItem.receita))


def _parse_dia(valor):
    """Parse 'YYYY-MM-DD' -> date, ou None se invalido/vazio."""
    try:
        return datetime.strptime((valor or '').strip(), '%Y-%m-%d').date()
    except ValueError:
        return None


def _card_loja(p):
    return {'tipo': 'loja', 'id': p.id,
            'titulo': (p.loja.nome if p.loja else '—'),
            'data_entrega': p.data_entrega,
            'itens': [{'id': it.id, 'qtd': it.quantidade,
                       'nome': it.nome_item_com_estado, 'obs': it.observacao}
                      for it in p.itens]}


def _card_b2b(v):
    return {'tipo': 'b2b', 'id': v.id,
            'titulo': 'B2B · ' + v.cliente_display,
            'data_entrega': v.data_entrega,
            'itens': [{'id': it.id, 'qtd': it.quantidade,
                       'nome': it.nome_item_com_estado, 'obs': it.observacao}
                      for it in v.itens]}


def _card_online(p):
    """Pedido do SITE com item SOB ENCOMENDA (produzido pro pedido, D+2):
    aparece na fila do padeiro pra garantir que sera preparado (decisao do
    dono 21/07/2026). Mostra SO os itens sob encomenda — os demais itens do
    pedido saem da prateleira e nao sao produzidos aqui. E card INFORMATIVO
    (sem botao SEPARAR): a entrega do site roda pelo /entregas/painel; a
    fila do padeiro so garante a producao. A producao real entra pelo
    cronograma (balanco firme), este card e o lembrete visivel.

    MENU CONFIGURAVEL explode na COMPOSICAO ESCOLHIDA (fix 31/07/2026, caso
    real: venda do Menu Degustacao pra domingo): o card mostrava so
    "1x Menu Degustacao dos Minis" — o padeiro nao tem como produzir a
    partir disso, e a escolha do cliente (20x Nutella, 10x Danish...) so
    existia no painel de entregas. Mesma fonte do bloco 2c do balanco
    (`composicao_escolhida`); cesta comum (sem composicao no pedido) segue
    mostrando o nome do cadastro."""
    from app.services.loja_estoque_reserva import (
        composicao_escolhida,
        item_sob_encomenda,
    )
    itens = []
    for it in p.itens:
        if not item_sob_encomenda(it):
            continue
        comps = composicao_escolhida(it)
        if comps:
            qtd_item = int(it.quantidade or 1)
            itens.append({'id': it.id, 'qtd': qtd_item, 'nome': it.nome,
                          'obs': 'montado pelo cliente:'})
            itens.extend(
                {'id': f'{it.id}c{i}',
                 'qtd': int(round(qtd_item * float(qtd_por or 0))),
                 'nome': '· ' + nome, 'obs': None}
                for i, (_col, _cid, nome, qtd_por) in enumerate(comps))
        else:
            itens.append({'id': it.id, 'qtd': it.quantidade,
                          'nome': it.nome, 'obs': None})
    return {'tipo': 'online', 'id': p.id,
            'titulo': 'Site · ' + (p.nome_cliente or 'Pedido'),
            'codigo': p.codigo,
            'modo': p.modo_entrega,
            'data_entrega': p.data_entrega,
            'itens': itens}


def _card_retirada(r):
    """Retirada de sobras (loja → industria): aparece na fila do padeiro como
    RECEBIMENTO — a industria vai receber esses itens de volta, nao separar."""
    return {'tipo': 'retirada', 'id': r.id,
            'titulo': r.loja.nome if r.loja else '—',
            'data_entrega': r.data_retirada,
            'status': r.status,
            'foto_url': r.foto_url,
            'itens': [{'id': it.id, 'qtd': it.quantidade,
                       # Base da conferência do recebimento: o que o
                       # motorista COLETOU (None = coletou o declarado).
                       'qtd_coletada': it.quantidade_coletada,
                       'nome': it.nome_item,
                       'obs': ('vira ' + it.receita.retorno_receita.nome
                               if it.receita and it.receita.retorno_receita
                               else None)}
                      for it in r.itens]}


def _dados_listas(dia, eh_hoje):
    """Pedidos de loja + vendas B2B (com data de entrega) do dia, a separar e
    aguardando. Helper compartilhado entre a tela cheia (`index`) e o refresh
    parcial (`listas_html`). Loja baixa estoque da loja no recebimento; o B2B
    baixa o freezer AO SEPARAR (regime 07/07/2026 — ver separar_b2b)."""
    from sqlalchemy.orm import selectinload

    from app.models import PedidoItem
    hj = hoje()
    # Eager load (N+1 do Sentry 13/07/2026): a TV consulta isto a cada 15s
    # e cada card disparava 1 SELECT de receita POR ITEM.
    q = PedidoLoja.query.options(
        selectinload(PedidoLoja.loja),
        selectinload(PedidoLoja.itens).selectinload(PedidoItem.receita),
        selectinload(PedidoLoja.itens).selectinload(
            PedidoItem.materia_prima),
    ).filter(
        PedidoLoja.status.in_(('pendente', 'confirmado', 'separado')))
    if eh_hoje:
        # Hoje inclui atrasados nao despachados (nada se perde).
        q = q.filter((PedidoLoja.data_entrega <= hj)
                     | (PedidoLoja.data_entrega.is_(None)))
    else:
        q = q.filter(PedidoLoja.data_entrega == dia)
    pedidos = q.order_by(PedidoLoja.data_entrega).all()

    # B2B so entra na fila quando tem data de entrega (senao e venda imediata).
    from app.models import VendaB2BItem
    qb = VendaB2B.query.options(
        selectinload(VendaB2B.itens).selectinload(VendaB2BItem.receita),
        selectinload(VendaB2B.itens).selectinload(VendaB2BItem.produto),
    ).filter(
        VendaB2B.status != 'cancelada',
        VendaB2B.status_entrega.in_(('pendente', 'separado')),
        VendaB2B.data_entrega.isnot(None))
    if eh_hoje:
        qb = qb.filter(VendaB2B.data_entrega <= hj)
    else:
        qb = qb.filter(VendaB2B.data_entrega == dia)
    vendas = qb.order_by(VendaB2B.data_entrega).all()

    # Retiradas de sobras (loja → industria): entram na MESMA fila, com
    # destaque proprio — a industria vai RECEBER (nao separar). Hoje inclui
    # atrasadas (retirada de ontem que o motorista ainda nao coletou).
    from app.models import Receita, RetiradaSobra, RetiradaSobraItem
    qr_ = RetiradaSobra.query.options(
        selectinload(RetiradaSobra.loja),
        selectinload(RetiradaSobra.itens)
        .selectinload(RetiradaSobraItem.receita)
        .selectinload(Receita.retorno_receita),
    ).filter(
        RetiradaSobra.status.in_(('aguardando_coleta', 'em_transporte')))
    if eh_hoje:
        qr_ = qr_.filter(RetiradaSobra.data_retirada <= hj)
    else:
        qr_ = qr_.filter(RetiradaSobra.data_retirada == dia)
    retiradas = qr_.order_by(RetiradaSobra.data_retirada).all()

    # Pedidos do SITE com item sob encomenda (D+2, dono 21/07/2026): entram
    # na fila do padeiro como lembrete de producao — pagos (nao cancelado/
    # entregue/aguardando_pagamento), com data de entrega, e que contenham ao
    # menos um item sob encomenda. Divulgacao fica de fora (nao produz pro
    # cliente).
    #
    # Na visao de HOJE entram TAMBEM as encomendas de data FUTURA (fix
    # 31/07/2026, caso real: menu de minis vendido na sexta pra entrega no
    # domingo e a TV muda ate domingo). Diferente dos pedidos de loja/B2B, o
    # sob encomenda existe JUSTAMENTE pra ser produzido com antecedencia
    # (D+2) — o cronograma ja agenda fornadas dias antes da entrega, entao o
    # lembrete visivel tem que aparecer do pagamento ate a entrega, nao so
    # no dia. O card mostra a data de entrega; o pedido some quando vira
    # 'entregue'/'cancelado'.
    from app.models import PedidoOnline, PedidoOnlineItem
    from app.services.loja_estoque_reserva import item_sob_encomenda
    qo = PedidoOnline.query.options(
        selectinload(PedidoOnline.itens).selectinload(PedidoOnlineItem.receita),
        selectinload(PedidoOnline.itens).selectinload(PedidoOnlineItem.produto),
        selectinload(PedidoOnline.itens)
        .selectinload(PedidoOnlineItem.componentes),
    ).filter(
        PedidoOnline.status.in_(_STATUS_ONLINE_PRODUCAO),
        PedidoOnline.data_entrega.isnot(None))
    if not eh_hoje:
        qo = qo.filter(PedidoOnline.data_entrega == dia)
    onlines = [p for p in qo.order_by(PedidoOnline.data_entrega).all()
               if any(item_sob_encomenda(it) for it in p.itens)]

    a_separar = ([_card_retirada(r) for r in retiradas]
                 + [_card_loja(p) for p in pedidos if p.status in _A_SEPARAR]
                 + [_card_b2b(v) for v in vendas if v.status_entrega == 'pendente']
                 + [_card_online(p) for p in onlines])
    aguardando = ([_card_loja(p) for p in pedidos if p.status == 'separado']
                  + [_card_b2b(v) for v in vendas if v.status_entrega == 'separado'])
    drivers = Driver.query.filter_by(ativo=True).order_by(Driver.nome).all()
    # Repetidos = pedidos de loja a mais por (loja, status, data) pra juntar.
    grupos = Counter((p.loja_id, p.status, p.data_entrega)
                     for p in pedidos if p.status in _A_SEPARAR)
    n_repetidos = sum(c - 1 for c in grupos.values() if c > 1)
    return {'a_separar': a_separar, 'aguardando': aguardando,
            'drivers': drivers, 'n_repetidos': n_repetidos}


def _plano_do_dia(dia):
    """Plano de producao aprovado do cronograma pra `dia` (origem=
    'cronograma'), AGRUPADO por massa-base pro padeiro entender o que amassar e
    quanto tirar de cada. None se nao houver plano aprovado.

    Retorna {plano_id, total_falta, grupos:[{nome, base_massa_label, fornadas,
    itens:[...]}], solos:[...]} — cada item tem item_id/receita_id/nome/alvo/
    produzido/falta."""
    from app.models import MassaBaseItem, PlanejamentoProducao
    from app.services.centros_producao import (
        CENTRO_PAES,
        CENTRO_VIENNOISERIE,
        centro_trabalho_receita,
        eh_preparo_auxiliar,
    )
    from app.services.gantt import _g_label
    from app.services.massa_base import calcular_cascata, rendimento_massa_crua
    from app.services.producao import fornadas_amassadeira

    plano = (PlanejamentoProducao.query
             .options(_eager_itens_receita())
             .filter_by(data=dia, origem='cronograma')
             .filter(PlanejamentoProducao.enviado_ao_padeiro.isnot(False))
             .first())
    if plano is None:
        return None

    membership = {row.receita_id: row for row in MassaBaseItem.query.all()}

    def _item(it):
        rec = it.receita
        alvo = int(it.qtd_alvo or 0)
        feito = int(it.produzido_qtd or 0)
        # rendimento de massa CRUA (peso_unitario), sem perda do forno — a
        # produção pesa massa crua. Float pra escala exata (un × peso_unitario).
        rend = rendimento_massa_crua(rec) if rec else 1.0
        return {'item_id': it.id, 'receita_id': it.receita_id,
                'nome': rec.nome if rec else '(receita)', 'alvo': alvo,
                'produzido': feito, 'falta': max(0, alvo - feito),
                'fornadas': fornadas_amassadeira(rec, it.multiplicador),
                'centro': centro_trabalho_receita(rec),
                'auxiliar': eh_preparo_auxiliar(rec),
                '_porcoes': alvo / rend,
                '_mult': it.multiplicador, '_mbi': membership.get(it.receita_id)}

    # Item dispensado pelo admin (auditoria) sai do plano do padeiro: ele não vê
    # nem produz o que o admin já fechou. Item com falta ENCERRADA pelo próprio
    # padeiro (produziu menos e deu por feito, 17/07/2026) idem — a diferença
    # vive só na auditoria até o admin dar OK ou reagendar de volta.
    itens = [_item(it) for it in plano.itens
             if it.dispensada_em is None and it.falta_encerrada_em is None]

    # agrupa por massa-base; o resto vai pra "solos".
    por_grupo = {}
    solos = []
    for d in itens:
        mbi = d['_mbi']
        if mbi is not None:
            por_grupo.setdefault(mbi.massa_base_id, (mbi.massa_base, []))[1].append(d)
        else:
            solos.append(d)

    grupos = []
    for mb_id, (mb, ds) in por_grupo.items():
        # porções reais (qtd_alvo / rendimento) — mesma escala do modal "ver a
        # base", não o multiplicador inteiro (que infla a massa).
        porcoes = {d['receita_id']: d['_porcoes'] for d in ds}
        calc = calcular_cascata(mb, porcoes)
        grupos.append({
            'mb_id': mb_id,
            'nome': mb.nome,
            'base_massa_label': _g_label(calc['base_massa']) if calc else None,
            'fornadas': (calc['fornadas'] if calc else None),
            'itens': ds,
        })
    grupos.sort(key=lambda g: g['nome'])

    total_itens = len(itens)
    itens_concluidos = sum(1 for i in itens if i['falta'] == 0)
    itens_pendentes = total_itens - itens_concluidos
    progresso_pct = (round(itens_concluidos * 100 / total_itens)
                     if total_itens else 100)
    return {'plano_id': plano.id, 'grupos': grupos, 'solos': solos,
            'solos_paes': [
                i for i in solos
                if i['centro'] == CENTRO_PAES and not i['auxiliar']],
            'solos_viennoiserie': [
                i for i in solos
                if i['centro'] == CENTRO_VIENNOISERIE and not i['auxiliar']],
            'solos_auxiliares': [i for i in solos if i['auxiliar']],
            # `total_falta` continua sendo a trava operacional usada por
            # `_plano_em_aberto`. Ele NÃO deve virar um KPI visual: soma
            # pães, gramas de bases e insumos em unidades incompatíveis.
            'total_falta': sum(i['falta'] for i in itens),
            'total_itens': total_itens,
            'itens_concluidos': itens_concluidos,
            'itens_pendentes': itens_pendentes,
            'progresso_pct': progresso_pct}


def _plano_em_aberto(dia):
    """Plano de `dia` reduzido ao que ainda FALTA produzir. None se não há
    plano, nada falta, ou o admin já dispensou tudo.

    Persistência pós-meia-noite (pedido do dono, 03/07/2026): o padeiro
    trabalha de madrugada — a ordem do dia D é executada na madrugada de D+1,
    e na virada da meia-noite `hoje()` rola e a ordem SUMIA da tela. A visão
    "hoje" agora mostra também a ordem de ontem em aberto, até ser produzida
    ou o admin dispensá-la na auditoria."""
    p = _plano_do_dia(dia)
    if not p or not p.get('total_falta'):
        return None
    grupos = []
    for g in p['grupos']:
        abertos = [i for i in g['itens'] if i['falta'] > 0]
        if abertos:
            grupos.append(dict(g, itens=abertos))
    p['grupos'] = grupos
    p['solos'] = [i for i in p['solos'] if i['falta'] > 0]
    p['solos_paes'] = [i for i in p['solos_paes'] if i['falta'] > 0]
    p['solos_viennoiserie'] = [
        i for i in p['solos_viennoiserie'] if i['falta'] > 0]
    p['solos_auxiliares'] = [
        i for i in p['solos_auxiliares'] if i['falta'] > 0]
    return p


@padeiro_bp.route('/')
@login_required
@padeiro_required
def index():
    from app.models import AppConfig, LousaRecado
    hj = hoje()
    dia = _parse_dia(request.args.get('data')) or hj
    eh_hoje = (dia == hj)
    ontem = hj - timedelta(days=1)
    recados_lousa = (LousaRecado.query
                     .filter(LousaRecado.apagado_em.is_(None))
                     .order_by(LousaRecado.criado_em.desc()).all())
    resumo_flag = AppConfig.get(_FLAG_RESUMO_ENTREGAS) == '1'
    plano_dia = _plano_do_dia(dia)
    plano_ontem = _plano_em_aberto(ontem) if eh_hoje else None
    total_pendentes = sum(
        p.get('itens_pendentes', 0) for p in (plano_ontem, plano_dia) if p)
    return render_template(
        'padeiro/index.html', dia=dia, eh_hoje=eh_hoje,
        dia_anterior=(dia - timedelta(days=1)).isoformat(),
        dia_seguinte=(dia + timedelta(days=1)).isoformat(),
        plano_dia=plano_dia, plano_ontem=plano_ontem,
        total_pendentes=total_pendentes,
        data_ontem=ontem, n_lousa=len(recados_lousa),
        recados_lousa=recados_lousa,
        resumo_entregas=(_resumo_entregas() if resumo_flag else None),
        resumo_entregas_flag=resumo_flag,
        **_dados_listas(dia, eh_hoje))


# ── Resumo das entregas do site (2x/ano: Dia das Mães / Dia dos Pais) ────
#
# Pedido do dono 08/08/2026: a aba Produtos do /entregas (Vendidos no dia +
# A produzir) DENTRO da tela do padeiro, pro time montar as ~108 entregas do
# evento sem sair da TV. Liga/desliga fácil (AppConfig) porque fica dormente
# o resto do ano.
_FLAG_RESUMO_ENTREGAS = 'padeiro_resumo_entregas'


def _alvo_resumo(agora_dt):
    """Dia-alvo do resumo: AMANHÃ por padrão (o padeiro produz na véspera);
    antes das 10h, HOJE — na madrugada/manhã do evento a equipe está
    montando as entregas do dia EM VOO (mesma classe da ordem-de-ontem)."""
    d = agora_dt.date()
    return d if agora_dt.hour < 10 else d + timedelta(days=1)


def _resumo_entregas():
    """Resumo da aba Produtos pro dia-alvo — MESMO motor da aba e do XLSX
    (`entregas.routes._produtos_do_dia`). Best-effort: erro aqui nunca
    derruba a TV do padeiro."""
    from app.utils import agora
    try:
        from app.blueprints.entregas.routes import _produtos_do_dia
        alvo = _alvo_resumo(agora())
        d = _produtos_do_dia(alvo, [])
        d['alvo'] = alvo
        d['alvo_eh_hoje'] = (alvo == hoje())
        return d
    except Exception:  # noqa: BLE001 — card informativo, TV nunca cai
        logger.exception('resumo de entregas do padeiro falhou')
        return None


@padeiro_bp.route('/resumo-entregas/toggle', methods=['POST'])
@login_required
@admin_required
def resumo_entregas_toggle():
    """Liga/desliga o card (admin; o padeiro não vê o botão). Gesto de 2x
    por ano — véspera de Dia das Mães / Dia dos Pais."""
    from app.models import AppConfig
    ligado = AppConfig.get(_FLAG_RESUMO_ENTREGAS) == '1'
    AppConfig.set(_FLAG_RESUMO_ENTREGAS, '0' if ligado else '1')
    db.session.commit()
    flash('Resumo de entregas %s.' % ('desligado' if ligado else
                                      'LIGADO na tela do padeiro'), 'success')
    return redirect(url_for('padeiro.index'))


@padeiro_bp.route('/gantt')
@login_required
@padeiro_required
def gantt():
    """Fluxograma/Gantt da produção do dia: agenda as etapas das receitas do
    plano aprovado na linha do tempo (turnos 06–14 / 13–21), serializando
    amassadeira e forno e encaixando mise en place em paralelo."""
    from app.services.gantt import montar_gantt

    hj = hoje()
    dia = _parse_dia(request.args.get('data')) or hj
    return render_template(
        'padeiro/gantt.html', dia=dia, eh_hoje=(dia == hj),
        dia_anterior=(dia - timedelta(days=1)).isoformat(),
        dia_seguinte=(dia + timedelta(days=1)).isoformat(),
        g=montar_gantt(dia))


@padeiro_bp.route('/listas.html')
@login_required
@padeiro_required
def listas_html():
    """Fragmento HTML das listas pra TV atualizar sozinha sem recarregar a
    pagina (preserva o audio ja liberado e o estado dos paineis laterais)."""
    hj = hoje()
    dia = _parse_dia(request.args.get('data')) or hj
    eh_hoje = (dia == hj)
    return render_template('padeiro/_listas.html', dia=dia, eh_hoje=eh_hoje,
                           **_dados_listas(dia, eh_hoje))


@padeiro_bp.route('/receita/<int:receita_id>.json')
@login_required
@padeiro_required
def receita_mise(receita_id):
    """Receita escalada pra `unidades` (mise en place do modal)."""
    from flask import jsonify

    from app.models import Receita
    from app.services.producao import mise_en_place

    rec = Receita.query.get_or_404(receita_id)
    try:
        unidades = max(1, int(request.args.get('unidades', 1)))
    except (TypeError, ValueError):
        unidades = 1
    return jsonify(mise_en_place(rec, unidades))


@padeiro_bp.route('/massa-base/<int:mb_id>.json')
@login_required
@padeiro_required
def massa_base_mise(mb_id):
    """A BASE de uma massa-base escalada pro plano do dia: o que pôr na
    amassadeira + a sequência de retiradas (em unidades de pão). É isto que o
    padeiro precisa — não a receita separada de cada pão."""
    from flask import jsonify

    from app.models import MassaBase, PlanejamentoProducao
    from app.services.gantt import _g_label
    from app.services.massa_base import calcular_cascata, rendimento_massa_crua

    mb = MassaBase.query.get_or_404(mb_id)
    dia = _parse_dia(request.args.get('data')) or hoje()
    plano = (PlanejamentoProducao.query
             .options(_eager_itens_receita())
             .filter_by(data=dia, origem='cronograma')
             .filter(PlanejamentoProducao.enviado_ao_padeiro.isnot(False))
             .first())

    # Escala a base pelas UNIDADES do dia em porções REAIS (qtd_alvo /
    # rendimento de massa crua), não pelo multiplicador inteiro do item — esse
    # arredonda a fornada pra cima (ceil) e infla a massa/água. O rendimento é o
    # de massa CRUA (peso_unitario), sem perda do forno: 120 un × 500 g = 60 kg.
    membros = {it.receita_id: it.receita for it in mb.itens}
    porcoes, unidades = {}, {}
    if plano:
        for it in plano.itens:
            rec = membros.get(it.receita_id)
            if rec is None:
                continue
            if it.dispensada_em is not None:
                continue                  # dispensado: não entra na massa a preparar
            alvo = int(it.qtd_alvo or 0)
            rend = rendimento_massa_crua(rec)
            unidades[it.receita_id] = alvo
            porcoes[it.receita_id] = alvo / rend

    calc = calcular_cascata(mb, porcoes or None)
    if calc is None:
        return jsonify({'nome': mb.nome, 'vazio': True})

    base_recipe = [{'nome': n, 'qtd': _g_label(g)} for n, g in
                   sorted(calc['base_mix'].items(), key=lambda kv: -kv[1])]

    def _passo(p):
        return {'tipo': p['tipo'], 'nome': p['nome'],
                'unidades': unidades.get(p['receita_id']),
                'tirar_massa': (_g_label(p['tirar_massa'])
                                if p.get('tirar_massa') else None),
                'acrescentar': [{'nome': n, 'qtd': _g_label(g)}
                                for n, g in p['acrescentar'].items()],
                'eh_ramo': p.get('eh_ramo', False)}

    cascata = [_passo(p) for p in calc['passos']]
    return jsonify({
        'nome': mb.nome, 'base_massa': _g_label(calc['base_massa']),
        'fornadas': calc['fornadas'], 'base_recipe': base_recipe,
        'cascata': cascata})


@padeiro_bp.route('/produzir-plano/<int:item_id>', methods=['POST'])
@login_required
@padeiro_required
def produzir_plano(item_id):
    """OPCAO B: produz `unidades` de um item do plano do dia -> credita estoque
    pronto + desconta MP da ficha + avanca produzido_qtd."""
    from app.services.producao import produzir_item_plano

    try:
        unidades = int(request.form.get('unidades') or 0)
    except (TypeError, ValueError):
        unidades = 0
    encerrar = request.form.get('encerrar') == '1'
    res = produzir_item_plano(item_id, unidades, current_user.id,
                              encerrar=encerrar)
    if res.get('ok') and res.get('encerrado'):
        flash('Produzido %d un — item encerrado; a diferença (%d un) foi '
              'pra auditoria do admin.' % (unidades, res['falta_restante']),
              'success')
    elif res.get('ok'):
        flash('Produzido %d un — estoque creditado e MP descontada.'
              % unidades, 'success')
    else:
        flash(res.get('erro', 'Erro ao produzir.'), 'warning')
    return redirect(request.referrer or url_for('padeiro.index'))


@padeiro_bp.route('/plano/editar', methods=['GET', 'POST'])
@login_required
@admin_required
def editar_plano():
    """Edita a ORDEM DE PRODUÇÃO do dia (a do cronograma, que desce pro padeiro):
    muda a quantidade-alvo de cada receita, adiciona ou remove receitas. Reflete
    direto no Fluxograma e na Produção do dia. Não mexe no que já foi produzido."""
    from math import ceil

    from app.models import PlanejamentoItem, PlanejamentoProducao, Receita

    hj = hoje()
    dia = _parse_dia(request.args.get('data') or request.form.get('data')) or hj
    plano = (PlanejamentoProducao.query
             .options(_eager_itens_receita())
             .filter_by(data=dia, origem='cronograma').first())

    def _rend(rec):
        return float(rec.rendimento_qtd) if rec and rec.rendimento_qtd else 1.0

    if request.method == 'POST':
        if plano is None:                      # cria a ordem do dia se não existe
            plano = PlanejamentoProducao(
                data=dia, origem='cronograma', status='aprovado',
                nome='Produção %s' % dia.strftime('%d/%m'),
                criado_por=current_user.id, enviado_ao_padeiro=False)  # rascunho
            db.session.add(plano)
            db.session.flush()

        # 1) atualiza quantidade / remove itens existentes
        for it in list(plano.itens):
            if request.form.get('remover_%d' % it.id):
                db.session.delete(it)
                continue
            try:
                alvo = max(0, int(request.form.get('alvo_%d' % it.id) or 0))
            except (TypeError, ValueError):
                alvo = int(it.qtd_alvo or 0)
            it.qtd_alvo = alvo
            it.multiplicador = max(1, ceil(alvo / _rend(it.receita))) if alvo else 1

        # 2) adiciona novas receitas
        existentes = {it.receita_id for it in plano.itens}
        novas_rid = request.form.getlist('novo_receita_id[]')
        novas_qtd = request.form.getlist('novo_alvo[]')
        for i, rid in enumerate(novas_rid):
            if not rid:
                continue
            try:
                rid = int(rid)
                alvo = max(0, int(novas_qtd[i]) if i < len(novas_qtd)
                           and novas_qtd[i] else 0)
            except (TypeError, ValueError):
                continue
            if alvo <= 0 or rid in existentes:
                continue
            rec = db.session.get(Receita, rid)
            if rec is None:
                continue
            db.session.add(PlanejamentoItem(
                planejamento_id=plano.id, receita_id=rid, qtd_alvo=alvo,
                multiplicador=max(1, ceil(alvo / _rend(rec)))))
            existentes.add(rid)

        # Espelha a edição no rascunho do grid (CronogramaOverride) — mão dupla:
        # editar aqui passa a refletir no cronograma da indústria. Override = a
        # qtd_alvo absoluta deste dia; receita removida vira 0 (some do grid).
        # Dia FECHADO com o cadeado (🔒): a ORDEM pode ser editada (gesto
        # explícito, mesma família do enviar), mas o espelho no rascunho é
        # PULADO — o cadeado protege exatamente os overrides do dia; a
        # divergência ordem×grid fica visível no "⚠ difere do enviado".
        from app.services.cronograma_edit import dias_fechados
        db.session.flush()
        if dia not in dias_fechados():
            from app.models import CronogramaOverride
            ov_exist = {o.receita_id: o for o in
                        CronogramaOverride.query.filter_by(data=dia).all()}
            atuais = {it.receita_id: int(it.qtd_alvo or 0)
                      for it in plano.itens}
            for rid, q in atuais.items():
                o = ov_exist.get(rid)
                if o is not None:
                    o.qtd = q
                else:
                    db.session.add(CronogramaOverride(receita_id=rid, data=dia,
                                                      qtd=q))
            for rid, o in ov_exist.items():
                if rid not in atuais:
                    o.qtd = 0

        db.session.commit()
        flash('Plano de produção de %s atualizado.' % dia.strftime('%d/%m'),
              'success')
        return redirect(url_for('padeiro.editar_plano', data=dia.isoformat()))

    # GET: monta a tela
    itens = []
    if plano:
        for it in plano.itens:
            rec = it.receita
            itens.append({'id': it.id, 'nome': rec.nome if rec else '(receita)',
                          'alvo': int(it.qtd_alvo or 0),
                          'produzido': int(it.produzido_qtd or 0)})
        itens.sort(key=lambda x: x['nome'])
    receitas = (Receita.query.filter(Receita.arquivada_em.is_(None))
                .order_by(Receita.categoria, Receita.nome).all())
    return render_template('padeiro/editar_plano.html', dia=dia,
                           dia_iso=dia.isoformat(), eh_hoje=(dia == hj),
                           dia_anterior=(dia - timedelta(days=1)).isoformat(),
                           dia_seguinte=(dia + timedelta(days=1)).isoformat(),
                           plano=plano, itens=itens, receitas=receitas)


@padeiro_bp.route('/<int:id>/separar', methods=['POST'])
@login_required
@padeiro_required
def separar(id):
    data_str = (request.form.get('data') or '').strip() or None
    pedido = PedidoLoja.query.get_or_404(id)
    if pedido.status not in _A_SEPARAR:
        flash(f'Pedido #{pedido.id} nao esta mais aguardando separacao.', 'warning')
        return redirect(url_for('padeiro.index', data=data_str))
    pedido.status = 'separado'
    db.session.commit()
    flash(f'Pedido #{pedido.id} separado.', 'success')
    return redirect(url_for('padeiro.index', data=data_str))


@padeiro_bp.route('/b2b/<int:id>/separar', methods=['POST'])
@login_required
@padeiro_required
def separar_b2b(id):
    """Marca uma venda B2B como separada e BAIXA o estoque da industria —
    a separacao e o momento em que o pao sai do freezer de verdade
    (decisao do dono 07/07/2026; antes a baixa era na criacao da venda).
    Idempotente pelo marcador `estoque_baixado_em`: venda do regime antigo
    (ja baixada na criacao) ou re-separacao nao baixa de novo."""
    from app.services import vendas_b2b as vendas_svc
    data_str = (request.form.get('data') or '').strip() or None
    venda = VendaB2B.query.get_or_404(id)
    if venda.status == 'cancelada' or venda.status_entrega != 'pendente':
        flash(f'Venda B2B #{venda.id} nao esta aguardando separacao.', 'warning')
        return redirect(url_for('padeiro.index', data=data_str))
    baixou = vendas_svc.baixar_na_separacao(venda, user=current_user)
    venda.status_entrega = 'separado'
    db.session.commit()
    flash(f'Venda B2B #{venda.id} separada'
          + (' — estoque baixado.' if baixou else '.'), 'success')
    return redirect(url_for('padeiro.index', data=data_str))


@padeiro_bp.route('/b2b/<int:id>/entregue', methods=['POST'])
@login_required
@padeiro_required
def entregar_b2b(id):
    """Marca uma venda B2B separada como entregue (sai da fila do padeiro).
    Despacho simples, sem QR/motorista (isso e a Fase 2). Nao mexe em
    estoque — a baixa ja aconteceu na SEPARACAO (regime 07/07/2026)."""
    data_str = (request.form.get('data') or '').strip() or None
    venda = VendaB2B.query.get_or_404(id)
    db.session.refresh(venda, with_for_update=True)
    if venda.status == 'cancelada' or venda.status_entrega != 'separado':
        flash(f'Venda B2B #{venda.id} nao esta aguardando despacho.', 'warning')
        return redirect(url_for('padeiro.index', data=data_str))
    venda.status_entrega = 'entregue'
    from app.services.cobrancas_automacao import enfileirar
    enfileirar(venda, current_user.id)
    db.session.commit()
    flash(f'Venda B2B #{venda.id} marcada como entregue.', 'success')
    return redirect(url_for('padeiro.index', data=data_str))


@padeiro_bp.route('/<int:id>/gerar-qr', methods=['POST'])
@login_required
@padeiro_required
def gerar_qr(id):
    from app.services.handshake_qr import gerar_qr_saida
    from app.services.qrcode_svc import gerar_png_data_url

    data_str = (request.form.get('data') or '').strip() or None
    pedido = PedidoLoja.query.get_or_404(id)
    if pedido.status != 'separado':
        flash(f'Pedido #{pedido.id} precisa estar separado (atual: {pedido.status}).',
              'warning')
        return redirect(url_for('padeiro.index', data=data_str))

    drv_id = request.form.get('driver_id', type=int)
    drv = Driver.query.get(drv_id) if drv_id else None
    if not drv or not drv.ativo:
        flash('Escolha um motorista ativo.', 'warning')
        return redirect(url_for('padeiro.index', data=data_str))

    try:
        pedido.driver_id = drv.id
        db.session.commit()
        qr = gerar_qr_saida(pedido, current_user.id)
        url = url_for('handshake.handshake', token=qr.token, _external=True)
        qr_png = gerar_png_data_url(url)
    except Exception:
        db.session.rollback()
        logger.exception('padeiro.gerar_qr falhou (pedido=%s driver=%s)', id, drv_id)
        flash('Erro ao gerar o QR. O log foi registrado — avise o admin.', 'danger')
        return redirect(url_for('padeiro.index', data=data_str))

    # WhatsApp pro motorista (best-effort: nao trava a tela se o Z-API falhar).
    try:
        from app.services import driver_magic
        driver_magic.notificar_pedido(drv, pedido)
    except Exception:
        logger.exception('padeiro.gerar_qr: notificar_pedido falhou (nao bloqueia)')

    return render_template('padeiro/qr.html', pedido=pedido, drv=drv,
                           qr=qr, url=url, qr_png=qr_png, voltar_data=data_str)


@padeiro_bp.route('/retirada/<int:id>/qr', methods=['POST'])
@login_required
@padeiro_required
def retirada_qr(id):
    """Mostra o QR de RECEBIMENTO da retirada (motorista chegou com as sobras).

    So faz sentido com a retirada em transporte (coleta na loja ja feita) —
    antes disso o QR de recebimento nem valida no handshake."""
    from app.models import RetiradaSobra
    from app.services.handshake_qr import gerar_qr_retirada
    from app.services.qrcode_svc import gerar_png_data_url

    data_str = (request.form.get('data') or '').strip() or None
    ret = RetiradaSobra.query.get_or_404(id)
    if ret.status != 'em_transporte':
        flash(f'Retirada #{ret.id} ainda não foi coletada na loja '
              f'(status: {ret.status}).', 'warning')
        return redirect(url_for('padeiro.index', data=data_str))
    try:
        qr = gerar_qr_retirada(ret, 'recebimento', current_user.id)
        db.session.commit()
        url = url_for('handshake.handshake_retirada', token=qr.token,
                      _external=True)
        qr_png = gerar_png_data_url(url)
    except Exception:
        db.session.rollback()
        logger.exception('padeiro.retirada_qr falhou (retirada=%s)', id)
        flash('Erro ao gerar o QR. O log foi registrado — avise o admin.',
              'danger')
        return redirect(url_for('padeiro.index', data=data_str))
    return render_template('padeiro/qr_retirada.html', retirada=ret,
                           url=url, qr_png=qr_png, voltar_data=data_str)


@padeiro_bp.route('/retirada/<int:id>/receber', methods=['POST'])
@login_required
@padeiro_required
def retirada_receber(id):
    """Recebimento da retirada PELA TELA do padeiro, sem QR (decisão do
    dono 20/07/2026: "o padeiro deve concluir, porém ele só tem a tela do
    /padeiro — não tem como escanear"). Mesmo motor da destrava admin
    (`receber_retirada_manual`: claim atômico, conferência por item,
    guarda contra coleta já estornada); o QR do motorista segue como
    caminho alternativo. Campo `qtd_<item_id>` vazio/inválido = usa a
    quantidade coletada (mesmo contrato dos outros forms)."""
    from app.models import RetiradaSobra
    from app.services.devolucao import (
        auditar_gesto_retirada,
        receber_retirada_manual,
    )

    data_str = (request.form.get('data') or '').strip() or None
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
        resumo = receber_retirada_manual(ret, current_user.id, quantidades,
                                         origem='padeiro')
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        flash(f'Não recebi: {exc}', 'warning')
        return redirect(url_for('padeiro.index', data=data_str))
    auditar_gesto_retirada(ret, 'r_receb', 'manual',
                           f'tela do padeiro, usuario {current_user.id}')
    partes = [f"{r['qtd']}× {r.get('destino') or r['nome']}"
              for r in resumo if 'erro' not in r]
    erros = [r['nome'] for r in resumo if 'erro' in r]
    msg = (f'Retirada #{ret.id} recebida — estoque da indústria creditado'
           + (f" ({', '.join(partes)})" if partes else ' (nada a creditar)')
           + '.')
    if erros:
        msg += f" Itens sem cadastro (avise o admin): {', '.join(erros)}."
    flash(msg, 'success' if not erros else 'warning')
    return redirect(url_for('padeiro.index', data=data_str))


@padeiro_bp.route('/juntar-repetidos', methods=['POST'])
@login_required
@padeiro_required
def juntar_repetidos():
    """Limpeza retroativa: junta pedidos duplicados (mesma loja+status+data) do
    dia visto num so; os absorvidos viram 'cancelado'. (A criacao nova ja junta
    sozinha; isto eh pros que ja existiam antes do recurso.)"""
    from app.services.pedido_merge import STATUS_MESCLAVEL, consolidar_loja_data
    data_str = (request.form.get('data') or '').strip() or None
    hj = hoje()
    dia = _parse_dia(data_str) or hj
    q = PedidoLoja.query.filter(PedidoLoja.status.in_(STATUS_MESCLAVEL))
    if dia == hj:
        q = q.filter((PedidoLoja.data_entrega <= hj)
                     | (PedidoLoja.data_entrega.is_(None)))
    else:
        q = q.filter(PedidoLoja.data_entrega == dia)
    grupos = {ch for ch, c in
              Counter((p.loja_id, p.status, p.data_entrega) for p in q.all()).items()
              if c > 1}
    juntados = 0
    for loja_id, status, d_ent in grupos:
        _alvo, absorvidos = consolidar_loja_data(loja_id, d_ent, status, current_user.id)
        juntados += absorvidos
    if juntados:
        db.session.commit()
        flash(f'{juntados} pedido(s) repetido(s) juntado(s) no mais antigo.', 'success')
    else:
        flash('Nenhum pedido repetido pra juntar.', 'info')
    return redirect(url_for('padeiro.index', data=data_str))


@padeiro_bp.route('/avisos.json')
@login_required
@padeiro_required
def avisos_json():
    """Avisos ativos (nao confirmados) pra TV consultar via polling."""
    from flask import jsonify

    from app.models import Aviso
    avisos = (Aviso.query.filter(Aviso.confirmado_em.is_(None))
              .order_by(Aviso.criado_em).all())
    return jsonify(avisos=[{
        'id': a.id, 'texto': a.texto,
        'por': (a.criado_por.nome if a.criado_por else ''),
        'em': a.criado_em.strftime('%H:%M') if a.criado_em else '',
    } for a in avisos])


@padeiro_bp.route('/avisos/<int:id>/confirmar', methods=['POST'])
@login_required
@padeiro_required
def confirmar_aviso(id):
    """Marca um aviso como lido (para a campainha)."""
    from flask import jsonify

    from app.models import Aviso
    from app.utils import agora
    a = Aviso.query.get_or_404(id)
    if a.confirmado_em is None:
        a.confirmado_em = agora()
        a.confirmado_por_id = current_user.id
        db.session.commit()
    return jsonify(ok=True)


@padeiro_bp.route('/avisos-24h.json')
@login_required
@padeiro_required
def avisos_24h_json():
    """Avisos das ultimas 24h (lidos e nao lidos) pro painel lateral."""
    from flask import jsonify

    from app.models import Aviso
    from app.utils import agora
    limite = agora() - timedelta(hours=24)
    avisos = (Aviso.query.filter(Aviso.criado_em >= limite)
              .order_by(Aviso.criado_em.desc()).all())
    return jsonify(avisos=[{
        'id': a.id, 'texto': a.texto,
        'por': (a.criado_por.nome if a.criado_por else ''),
        'quando': a.criado_em.strftime('%d/%m %H:%M') if a.criado_em else '',
        'confirmado': a.confirmado,
    } for a in avisos])


@padeiro_bp.route('/preparar.json')
@login_required
@padeiro_required
def preparar_json():
    """Itens [BACKUP]/[ASSADO] dos pedidos do DIA SEGUINTE (ao dia visto), pra
    a producao adiantar o pre-preparo na vespera. Agrega por item+estado."""
    from collections import defaultdict

    from flask import jsonify
    from sqlalchemy import and_, or_

    from app.constants import ESTADO_LABEL, STATUS_PEDIDO_FINALIZADOS
    from app.models import PedidoItem, Receita, VendaB2BItem
    dia = _parse_dia(request.args.get('data')) or hoje()
    alvo = dia + timedelta(days=1)
    # Item entra no pre-preparo quando `estado` explicito for assado/backup,
    # OU quando `estado` for NULL e a `Receita.estado_padrao` for assado/backup.
    _estados_pre = ('assado', 'backup')
    from sqlalchemy.orm import selectinload
    itens = (PedidoItem.query.join(PedidoLoja)
             .outerjoin(Receita, Receita.id == PedidoItem.receita_id)
             .options(selectinload(PedidoItem.receita),
                      selectinload(PedidoItem.pedido)
                      .selectinload(PedidoLoja.loja))
             .filter(PedidoLoja.data_entrega == alvo,
                     ~PedidoLoja.status.in_(STATUS_PEDIDO_FINALIZADOS),
                     or_(PedidoItem.estado.in_(_estados_pre),
                         and_(PedidoItem.estado.is_(None),
                              Receita.estado_padrao.in_(_estados_pre))))
             .all())
    agg = defaultdict(int)
    for it in itens:
        loja = it.pedido.loja.nome if (it.pedido and it.pedido.loja) else '—'
        agg[(loja, it.nome_item, it.estado_efetivo)] += (it.quantidade or 0)
    # B2B do dia seguinte com estado [ASSADO]/[BACKUP] entram no mesmo pre-preparo.
    itens_b2b = (VendaB2BItem.query.join(VendaB2B)
                 .outerjoin(Receita, Receita.id == VendaB2BItem.receita_id)
                 .options(selectinload(VendaB2BItem.receita),
                          selectinload(VendaB2BItem.venda))
                 .filter(VendaB2B.data_entrega == alvo,
                         VendaB2B.status != 'cancelada',
                         VendaB2B.status_entrega != 'entregue',
                         or_(VendaB2BItem.estado.in_(_estados_pre),
                             and_(VendaB2BItem.estado.is_(None),
                                  Receita.estado_padrao.in_(_estados_pre))))
                 .all())
    for it in itens_b2b:
        cli = ('B2B · ' + it.venda.cliente_display) if it.venda else 'B2B'
        agg[(cli, it.nome_item, it.estado_efetivo)] += (it.quantidade or 0)
    # Pedidos do SITE sob encomenda (D+2, dono 21/07/2026): o item PRODUZIDO
    # pro pedido entra no pre-preparo da vespera (aparece na terca pra entrega
    # na quarta). Como PedidoOnlineItem nao tem `estado`, usa o estado_padrao
    # da receita (assado/backup); sem estado_padrao cai em 'assado' pra o item
    # sempre aparecer (a producao propria e o lembrete que o dono pediu).
    from app.models import PedidoOnline, PedidoOnlineItem, Receita
    from app.services.loja_estoque_reserva import (
        composicao_escolhida,
        item_sob_encomenda,
    )
    itens_online = (PedidoOnlineItem.query.join(PedidoOnline)
                    .options(selectinload(PedidoOnlineItem.receita),
                             selectinload(PedidoOnlineItem.produto),
                             selectinload(PedidoOnlineItem.componentes),
                             selectinload(PedidoOnlineItem.pedido))
                    .filter(PedidoOnline.data_entrega == alvo,
                            PedidoOnline.status.in_(_STATUS_ONLINE_PRODUCAO))
                    .all())
    for it in itens_online:
        if not item_sob_encomenda(it):
            continue
        # Menu configuravel (fix 31/07/2026): o pre-preparo listava
        # "1x Menu Degustacao" com estado chutado — o padeiro precisa dos
        # MINIS que o cliente escolheu, cada um com o SEU estado_padrao.
        comps = composicao_escolhida(it)
        if comps:
            qtd_item = int(it.quantidade or 1)
            for col, comp_id, nome, qtd_por in comps:
                q = int(round(qtd_item * float(qtd_por or 0)))
                if q <= 0:
                    continue
                rec = (Receita.query.get(comp_id)
                       if col == 'receita_id' else None)
                est = (rec.estado_padrao
                       if (rec is not None
                           and rec.estado_padrao in _estados_pre)
                       else 'assado')
                agg[('Site · encomenda', nome, est)] += q
            continue
        est = (it.receita.estado_padrao
               if (it.receita and it.receita.estado_padrao in _estados_pre)
               else 'assado')
        agg[('Site · encomenda', it.nome, est)] += (it.quantidade or 0)
    linhas = [{'loja': lj, 'nome': n, 'estado': e,
               'estado_label': ESTADO_LABEL.get(e, e.upper()), 'qtd': q}
              for (lj, n, e), q in agg.items()]
    linhas.sort(key=lambda x: (x['loja'], x['estado_label'], -x['qtd'], x['nome']))
    # TOTAIS por item+estado (soma de todas as lojas + B2B): o padeiro
    # pre-prepara o TOTAL e depois separa por loja — sem esta soma ele fazia
    # a conta de cabeca somando os grupos (pedido do dono 16/07/2026).
    tot = defaultdict(int)
    for (_lj, n, e), q in agg.items():
        tot[(n, e)] += q
    totais = [{'nome': n, 'estado': e,
               'estado_label': ESTADO_LABEL.get(e, e.upper()), 'qtd': q}
              for (n, e), q in tot.items()]
    totais.sort(key=lambda x: (x['estado_label'], -x['qtd'], x['nome']))
    # Alerta de pre-preparo: so a partir das 17h50 (BRT) e havendo itens.
    from app.utils import agora
    ag = agora()
    alertar = bool(linhas) and (ag.hour, ag.minute) >= (17, 50)
    return jsonify(dia=alvo.strftime('%d/%m'), alvo_iso=alvo.isoformat(),
                   itens=linhas, totais=totais, alertar=alertar)


@padeiro_bp.route('/congelados.json')
@login_required
@padeiro_required
def congelados_json():
    """Estoque de congelados/industria (EstoqueProducao) com saldo > 0, pro
    painel lateral do padeiro. Somente leitura — a contagem fica em
    /pedidos/congelados."""
    from flask import jsonify
    from sqlalchemy.orm import joinedload

    from app.models import EstoqueProducao
    itens = (EstoqueProducao.query
             .options(joinedload(EstoqueProducao.receita),
                      joinedload(EstoqueProducao.produto))
             .filter(EstoqueProducao.quantidade > 0).all())
    linhas = [{'nome': ep.nome_item_com_estado, 'qtd': ep.quantidade}
              for ep in itens]
    linhas.sort(key=lambda x: x['nome'].lower())
    return jsonify(itens=linhas)


@padeiro_bp.route('/buscar-receitas.json')
@login_required
@padeiro_required
def buscar_receitas():
    """Typeahead do painel Produzir: receitas E produtos/cestas cujo nome contem
    o texto. Acento-insensivel ('pao' acha 'Pão'), case-insensitive, casa todos
    os termos. Catalogo pequeno -> filtra em Python. Retorna refs
    'receita:<id>' / 'produto:<id>'."""
    from flask import jsonify

    from app.models import Produto, Receita
    from app.utils import normalizar_busca

    q = normalizar_busca((request.args.get('q') or '').strip())
    if len(q) < 2:
        return jsonify(itens=[])
    termos = q.split()

    def _casa(nome):
        n = normalizar_busca(nome)
        return all(t in n for t in termos)

    # ativas(): padeiro nao registra producao de receita arquivada
    # (varredura 19/07/2026 — criava linha morta de EstoqueProducao).
    out = [{'ref': 'receita:%d' % r.id, 'nome': r.nome}
           for r in Receita.ativas().order_by(Receita.nome).all()
           if _casa(r.nome)]
    out += [{'ref': 'produto:%d' % p.id, 'nome': p.nome}
            for p in Produto.query.filter_by(ativo=True).order_by(Produto.nome).all()
            if _casa(p.nome)]
    return jsonify(itens=out[:20])


@padeiro_bp.route('/produzir', methods=['POST'])
@login_required
@padeiro_required
def produzir():
    """Padeiro registra o que produziu -> entrada no congelado (cru) da
    industria. Corpo JSON {itens:[{ref:'receita:<id>'|'produto:<id>', quantidade}]};
    valida tudo ANTES de gravar; aplica numa transacao unica (tudo ou nada)."""
    from flask import jsonify

    from app.models import Produto, Receita
    from app.services.estoque_congelados import entrada_producao

    dados = request.get_json(silent=True) or {}
    itens = dados.get('itens') or []
    if not itens:
        return jsonify(ok=False, erro='Nenhum item informado.'), 400

    validados = []
    for i, it in enumerate(itens, 1):
        ref = (it.get('ref') or '').strip()
        tipo, _, sid = ref.partition(':')
        try:
            qtd = int(it.get('quantidade'))
        except (TypeError, ValueError):
            return jsonify(ok=False, erro=f'Item {i}: dados invalidos.'), 400
        if tipo not in ('receita', 'produto') or not sid.isdigit():
            return jsonify(ok=False, erro=f'Item {i}: item invalido.'), 400
        if qtd <= 0:
            return jsonify(ok=False, erro=f'Item {i}: quantidade deve ser positiva.'), 400
        obj = (Receita.query.get(int(sid)) if tipo == 'receita'
               else Produto.query.get(int(sid)))
        if not obj:
            return jsonify(ok=False, erro=f'Item {i}: item nao encontrado.'), 400
        validados.append((tipo, obj, qtd))

    try:
        from app.services.producao import consumir_subreceitas_prontas
        resumo = []
        for tipo, obj, qtd in validados:
            entrada_producao(
                receita_id=obj.id if tipo == 'receita' else None,
                produto_id=obj.id if tipo == 'produto' else None,
                estado=None, quantidade=qtd, usuario_id=current_user.id,
                referencia='Produção (TV padeiro)')
            # receita derivada (ex: almond) consome a sub-receita pronta do congelado
            if tipo == 'receita':
                consumir_subreceitas_prontas(obj, qtd, current_user.id)
            resumo.append({'nome': obj.nome, 'qtd': qtd})
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception('padeiro.produzir falhou')
        return jsonify(ok=False, erro='Erro ao registrar produção.'), 500
    return jsonify(ok=True, resumo=resumo)


@padeiro_bp.route('/producao-historico.json')
@login_required
@padeiro_required
def producao_historico():
    """Historico do que foi lancado pelo painel Produzir (data/hora + item + qtd).
    Ultimas ~50 entradas tipo='producao' lancadas por este painel."""
    from flask import jsonify

    from app.models import MovEstoqueProducao
    movs = (MovEstoqueProducao.query
            .filter(MovEstoqueProducao.tipo == 'producao',
                    MovEstoqueProducao.referencia == 'Produção (TV padeiro)')
            .order_by(MovEstoqueProducao.data.desc())
            .limit(50).all())
    return jsonify(historico=[{
        'quando': m.data.strftime('%d/%m %H:%M') if m.data else '',
        'item': m.estoque.nome_item if m.estoque else '?',
        'qtd': m.quantidade,
    } for m in movs])


# ── Perdas de produção (13/08/2026, pedido do dono) ──────────────────────
# "Colocar as perdas na tela do padeiro, eles precisam ter uma aba para
# lançar se queimou algo". Item PRONTO debita EstoqueProducao; FORNADA
# queimada consome a ficha (MP + subs) sem creditar. Motor:
# app/services/perda_producao.py; relatório admin em /producao/perdas.

@padeiro_bp.route('/perdas', methods=['GET', 'POST'])
@login_required
@padeiro_required
def perdas():
    from datetime import timedelta

    from app.models import PerdaProducao
    from app.services import perda_producao as pp
    from app.utils import agora

    if request.method == 'POST':
        ref = (request.form.get('item_ref') or '').strip()
        if not ref.startswith('receita:'):
            flash('Escolha o item na lista de busca (perda de produção é '
                  'por receita).', 'warning')
            return redirect(url_for('padeiro.perdas'))
        try:
            receita_id = int(ref.split(':', 1)[1])
        except (TypeError, ValueError):
            flash('Item inválido — escolha de novo na busca.', 'warning')
            return redirect(url_for('padeiro.perdas'))
        try:
            res = pp.registrar(
                receita_id=receita_id,
                quantidade=request.form.get('quantidade'),
                motivo=(request.form.get('motivo') or '').strip(),
                usuario_id=current_user.id,
                fornada=bool(request.form.get('fornada')),
                observacao=request.form.get('observacao'),
                funcionario_id=request.form.get('funcionario_id'))
        except ValueError as exc:
            flash(str(exc), 'warning')
            return redirect(url_for('padeiro.perdas'))
        except Exception:  # noqa: BLE001
            from flask import current_app
            db.session.rollback()
            current_app.logger.exception('perda_producao falhou')
            flash('Erro ao registrar a perda — nada foi gravado. Tente de '
                  'novo ou avise um admin.', 'danger')
            return redirect(url_for('padeiro.perdas'))
        flash('Perda registrada. Sentimos pelo pão — acontece! 🙏', 'success')
        for a in res['avisos']:
            flash(a, 'warning')
        return redirect(url_for('padeiro.perdas'))

    recentes = (PerdaProducao.query
                .filter(PerdaProducao.criado_em >= agora() - timedelta(days=7))
                .order_by(PerdaProducao.criado_em.desc())
                .limit(30).all())
    return render_template('padeiro/perdas.html', recentes=recentes,
                           motivos=pp.MOTIVOS,
                           responsaveis=pp.responsaveis_producao())


# ── Lousa dos padeiros (11/07/2026, pedido do dono) ──────────────────────
# Recados entre colegas de turno, escritos na própria tela do padeiro e
# visíveis durante o dia — como giz numa lousa, fica até alguém apagar.
# NÃO confundir com o Aviso (alarme escritório→produção com campainha).

@padeiro_bp.route('/lousa', methods=['GET', 'POST'])
@login_required
@padeiro_required
def lousa():
    from app.models import LousaRecado
    if request.method == 'POST':
        texto = (request.form.get('texto') or '').strip()[:500]
        if texto:
            db.session.add(LousaRecado(texto=texto,
                                       criado_por_id=current_user.id))
            db.session.commit()
        else:
            flash('Escreva o recado antes de enviar.', 'warning')
        return redirect(url_for('padeiro.lousa'))
    recados = (LousaRecado.query
               .filter(LousaRecado.apagado_em.is_(None))
               .order_by(LousaRecado.criado_em.desc()).all())
    return render_template('padeiro/lousa.html', recados=recados)


@padeiro_bp.route('/lousa.html')
@login_required
@padeiro_required
def lousa_fragmento():
    """Fragmento HTML do painel da lousa na tela do padeiro — a TV o
    recarrega por polling (mesmo padrão de listas_html), então recado novo
    de um colega aparece sem recarregar a página. Sem recado ativo o
    fragmento sai vazio e o painel some (a lousa só ocupa 1/3 da tela
    quando tem algo escrito — pedido do dono 11/07/2026)."""
    from app.models import LousaRecado
    recados = (LousaRecado.query
               .filter(LousaRecado.apagado_em.is_(None))
               .order_by(LousaRecado.criado_em.desc()).all())
    return render_template('padeiro/_lousa_painel.html',
                           recados_lousa=recados)


@padeiro_bp.route('/lousa/<int:id>/apagar', methods=['POST'])
@login_required
@padeiro_required
def lousa_apagar(id):
    """Apaga um recado da lousa (soft delete — como passar o apagador).
    Qualquer padeiro pode, igual numa lousa física; fica registrado quem."""
    from app.models import LousaRecado
    from app.utils import agora
    r = LousaRecado.query.get_or_404(id)
    if r.apagado_em is None:
        r.apagado_em = agora()
        r.apagado_por_id = current_user.id
        db.session.commit()
    # Apagar feito do painel da TV volta pra tela do padeiro; da página da
    # lousa, volta pra lousa.
    if request.form.get('volta') == 'index':
        return redirect(url_for('padeiro.index'))
    return redirect(url_for('padeiro.lousa'))


# ── Fichas de preparo (etapas por receita, preenchidas pelo padeiro) ────────
# Pedido do dono 14/07/2026: "ficha para meu padeiro preencher com as etapas
# de preparo dos pães, assim consigo alimentar o fluxograma". Mesma fonte de
# dados do editor do admin (/receitas/<id>/etapas) — parse/salvamento em
# app/services/etapas_receita.py, edição direta (sem aprovação).

@padeiro_bp.route('/fichas')
@login_required
@padeiro_required
def fichas():
    """Lista os pães (receitas ativas) com o estado da ficha de preparo:
    quantas etapas cadastradas e quantas já têm o passo a passo escrito.
    Receitas de RETORNO ficam fora — retorno nunca se produz (regra do dono
    13/07/2026), então não tem preparo a fichar."""
    from sqlalchemy.orm import selectinload

    from app.models import Receita
    receitas = (Receita.ativas()
                .options(selectinload(Receita.etapas))
                .all())
    retorno_ids = {r.retorno_receita_id for r in receitas
                   if r.retorno_receita_id}
    linhas = []
    for r in receitas:
        if r.id in retorno_ids:
            continue
        n = len(r.etapas)
        com_desc = sum(1 for e in r.etapas if (e.descricao or '').strip())
        linhas.append({'id': r.id, 'nome': r.nome,
                       'categoria': (r.categoria or '').strip()
                       or 'Sem categoria',
                       'n_etapas': n, 'com_descricao': com_desc})
    # Ordena pelo LABEL da categoria (não pela coluna): '' e NULL colapsam num
    # único cabeçalho "Sem categoria" (em SQL ordenariam em pontas opostas).
    linhas.sort(key=lambda x: (x['categoria'].lower(), x['nome'].lower()))
    return render_template('padeiro/fichas.html', linhas=linhas)


@padeiro_bp.route('/fichas/<int:id>', methods=['GET', 'POST'])
@login_required
@padeiro_required
def fichas_editar(id):
    """Ficha de preparo de UM pão: o padeiro preenche as etapas (nome,
    duração, tipo de trabalho) e o passo a passo (descrição) de cada uma.
    Salva direto — alimenta o fluxograma/Gantt e o mise en place."""
    from app.constants import etapas_padrao_categoria
    from app.models import Receita
    from app.services import etapas_receita

    receita = Receita.query.get_or_404(id)
    if receita.arquivada_em is not None:
        flash('Receita arquivada não recebe ficha de preparo.', 'warning')
        return redirect(url_for('padeiro.fichas'))

    if request.method == 'POST':
        if request.form.get('acao') == 'padrao':
            etapas_receita.set_etapas(
                receita.id,
                etapas_receita.de_tuplas(
                    etapas_padrao_categoria(receita.categoria)))
            db.session.commit()
            flash('Etapas preenchidas com o padrão da categoria — ajuste os '
                  'tempos e escreva o passo a passo.', 'info')
            return redirect(url_for('padeiro.fichas_editar', id=receita.id))
        etapas_form = etapas_receita.parse_etapas_form(request.form)
        etapas_receita.set_etapas(receita.id, etapas_form)
        db.session.commit()
        flash(f'Ficha de "{receita.nome}" salva ({len(etapas_form)} etapa(s)).',
              'success')
        return redirect(url_for('padeiro.fichas'))

    etapas_atuais = etapas_receita.listar(receita.id)
    return render_template('padeiro/fichas_editar.html', receita=receita,
                           etapas=etapas_atuais,
                           recurso_de=etapas_receita.recurso_de_etapa)


# ── Spotify (widget 🎵 da tela do padeiro, 15/07/2026) ──────────────────────
# O navegador só fala com ESTAS rotas; o servidor fala com o Spotify
# (app/services/spotify.py). Modo controle remoto: a música toca no aparelho
# de som da padaria, a tela comanda.

@padeiro_bp.route('/spotify/estado')
@login_required
@padeiro_required
def spotify_estado():
    """Estado do player pro widget (o que toca, pausado, volume, aparelho).
    ?playlists=1 inclui as playlists da conta (só quando o drawer abre)."""
    from app.services import spotify
    est = spotify.estado_player()
    if request.args.get('playlists') == '1' and est.get('ok'):
        est['playlists'] = spotify.listar_playlists()
    return jsonify(est)


@padeiro_bp.route('/spotify/acao', methods=['POST'])
@login_required
@padeiro_required
def spotify_acao():
    """Comando do widget: play/pause/next/previous/volume/playlist/
    transferir. `device_id` opcional mira a tela-player local."""
    from app.services import spotify
    dados = request.get_json(silent=True) or {}
    ok, erro = spotify.executar_acao(dados.get('acao'), dados.get('valor'),
                                     device_id=dados.get('device_id'))
    return jsonify(ok=ok, erro=erro), (200 if ok else 422)


@padeiro_bp.route('/spotify/token')
@login_required
@padeiro_required
def spotify_token():
    """Access token pro Web Playback SDK — a tela do padeiro TOCANDO a
    música localmente (decisão do dono 15/07/2026: "quero que ele toque as
    músicas, não reproduzir em outro dispositivo"). O SDK do Spotify exige o
    token no navegador; esta rota o entrega SOMENTE a papel padeiro/produção/
    admin logado (o token é da conta da padaria e expira em ~1h; o SDK pede
    um novo aqui quando vence)."""
    from app.services import spotify
    if not spotify.configurado() or not spotify.conectado():
        return jsonify(ok=False, motivo='nao_configurado'), 503
    tok, resta = spotify.token_para_player()
    if not tok:
        return jsonify(ok=False, motivo='sem_token'), 503
    return jsonify(ok=True, access_token=tok, expira_em_s=resta)


@padeiro_bp.route('/csp-report', methods=['POST'])
@csrf.exempt
def csp_report():
    """Recebe os relatórios de violação de CSP da tela do padeiro (report-uri)
    e guarda os últimos 20 em AppConfig — a sonda /api/claude/spotify-debug
    os expõe. Motivo: o áudio do Spotify vem de CDNs variados; quando a CSP
    bloqueia um host de áudio, a música morre em ~10s SEM erro visível — o
    relatório diz exatamente QUAL host faltou liberar. Sem CSRF (o navegador
    envia o report automaticamente, fora de qualquer form) e best-effort."""
    import json as _json

    from app.models import AppConfig
    from app.utils import agora
    try:
        bruto = request.get_data(as_text=True) or ''
        if len(bruto) > 10000:
            return ('', 204)
        rel = (_json.loads(bruto) or {}).get('csp-report') or {}
        entrada = {
            'em': agora().isoformat(timespec='seconds'),
            'diretiva': (rel.get('violated-directive')
                         or rel.get('effective-directive') or '')[:80],
            'bloqueado': (rel.get('blocked-uri') or '')[:200],
        }
        if not entrada['bloqueado'] and not entrada['diretiva']:
            return ('', 204)
        atuais = _json.loads(AppConfig.get('padeiro_csp_reports') or '[]')
        atuais.append(entrada)
        AppConfig.set('padeiro_csp_reports', _json.dumps(atuais[-20:]))
        db.session.commit()
    except Exception:  # noqa: BLE001 — relatório nunca pode derrubar nada
        db.session.rollback()
        logger.exception('csp-report do padeiro falhou (ignorado)')
    return ('', 204)


@padeiro_bp.route('/spotify/log', methods=['POST'])
@login_required
@padeiro_required
def spotify_log():
    """Telemetria do player da tela (últimos 30 eventos em AppConfig, lidos
    pela sonda /api/claude/spotify-debug). Motivo: o player morre na TV com
    erro visível SÓ lá — mandar o texto pro servidor fecha o ciclo de
    diagnóstico sem depender de foto da tela."""
    import json as _json

    from app.models import AppConfig
    from app.utils import agora
    try:
        dados = request.get_json(silent=True) or {}
        msg = str(dados.get('msg') or '')[:300]
        if not msg:
            return ('', 204)
        atuais = _json.loads(AppConfig.get('padeiro_spotify_log') or '[]')
        atuais.append({'em': agora().isoformat(timespec='seconds'),
                       'msg': msg})
        AppConfig.set('padeiro_spotify_log', _json.dumps(atuais[-30:]))
        db.session.commit()
    except Exception:  # noqa: BLE001 — telemetria nunca derruba nada
        db.session.rollback()
        logger.exception('spotify_log falhou (ignorado)')
    return ('', 204)
