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

from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.blueprints.padeiro import padeiro_bp
from app.decorators import padeiro_required
from app.extensions import db
from app.models import Driver, PedidoLoja, VendaB2B
from app.utils import hoje

logger = logging.getLogger(__name__)

_A_SEPARAR = ('pendente', 'confirmado')


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
            'itens': [{'qtd': it.quantidade, 'nome': it.nome_item_com_estado,
                       'obs': it.observacao}
                      for it in p.itens]}


def _card_b2b(v):
    return {'tipo': 'b2b', 'id': v.id,
            'titulo': 'B2B · ' + v.cliente_display,
            'data_entrega': v.data_entrega,
            'itens': [{'qtd': it.quantidade, 'nome': it.nome_item_com_estado,
                       'obs': it.observacao}
                      for it in v.itens]}


def _dados_listas(dia, eh_hoje):
    """Pedidos de loja + vendas B2B (com data de entrega) do dia, a separar e
    aguardando. Helper compartilhado entre a tela cheia (`index`) e o refresh
    parcial (`listas_html`). Loja baixa estoque da loja no recebimento; o B2B
    ja baixou do freezer na venda — aqui e so producao/separacao (sem estoque)."""
    hj = hoje()
    q = PedidoLoja.query.filter(
        PedidoLoja.status.in_(('pendente', 'confirmado', 'separado')))
    if eh_hoje:
        # Hoje inclui atrasados nao despachados (nada se perde).
        q = q.filter((PedidoLoja.data_entrega <= hj)
                     | (PedidoLoja.data_entrega.is_(None)))
    else:
        q = q.filter(PedidoLoja.data_entrega == dia)
    pedidos = q.order_by(PedidoLoja.data_entrega).all()

    # B2B so entra na fila quando tem data de entrega (senao e venda imediata).
    qb = VendaB2B.query.filter(
        VendaB2B.status != 'cancelada',
        VendaB2B.status_entrega.in_(('pendente', 'separado')),
        VendaB2B.data_entrega.isnot(None))
    if eh_hoje:
        qb = qb.filter(VendaB2B.data_entrega <= hj)
    else:
        qb = qb.filter(VendaB2B.data_entrega == dia)
    vendas = qb.order_by(VendaB2B.data_entrega).all()

    a_separar = ([_card_loja(p) for p in pedidos if p.status in _A_SEPARAR]
                 + [_card_b2b(v) for v in vendas if v.status_entrega == 'pendente'])
    aguardando = ([_card_loja(p) for p in pedidos if p.status == 'separado']
                  + [_card_b2b(v) for v in vendas if v.status_entrega == 'separado'])
    drivers = Driver.query.filter_by(ativo=True).order_by(Driver.nome).all()
    # Repetidos = pedidos de loja a mais por (loja, status, data) pra juntar.
    grupos = Counter((p.loja_id, p.status, p.data_entrega)
                     for p in pedidos if p.status in _A_SEPARAR)
    n_repetidos = sum(c - 1 for c in grupos.values() if c > 1)
    return {'a_separar': a_separar, 'aguardando': aguardando,
            'drivers': drivers, 'n_repetidos': n_repetidos}


@padeiro_bp.route('/')
@login_required
@padeiro_required
def index():
    hj = hoje()
    dia = _parse_dia(request.args.get('data')) or hj
    eh_hoje = (dia == hj)
    return render_template(
        'padeiro/index.html', dia=dia, eh_hoje=eh_hoje,
        dia_anterior=(dia - timedelta(days=1)).isoformat(),
        dia_seguinte=(dia + timedelta(days=1)).isoformat(),
        **_dados_listas(dia, eh_hoje))


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
    """Marca uma venda B2B como separada (status_entrega). Nao mexe em estoque
    — o B2B ja baixou do freezer na venda; aqui e so producao/separacao."""
    data_str = (request.form.get('data') or '').strip() or None
    venda = VendaB2B.query.get_or_404(id)
    if venda.status == 'cancelada' or venda.status_entrega != 'pendente':
        flash(f'Venda B2B #{venda.id} nao esta aguardando separacao.', 'warning')
        return redirect(url_for('padeiro.index', data=data_str))
    venda.status_entrega = 'separado'
    db.session.commit()
    flash(f'Venda B2B #{venda.id} separada.', 'success')
    return redirect(url_for('padeiro.index', data=data_str))


@padeiro_bp.route('/b2b/<int:id>/entregue', methods=['POST'])
@login_required
@padeiro_required
def entregar_b2b(id):
    """Marca uma venda B2B separada como entregue (sai da fila do padeiro).
    Despacho simples, sem QR/motorista (isso e a Fase 2). Nao mexe em estoque —
    o B2B ja baixou do freezer na venda."""
    data_str = (request.form.get('data') or '').strip() or None
    venda = VendaB2B.query.get_or_404(id)
    if venda.status == 'cancelada' or venda.status_entrega != 'separado':
        flash(f'Venda B2B #{venda.id} nao esta aguardando despacho.', 'warning')
        return redirect(url_for('padeiro.index', data=data_str))
    venda.status_entrega = 'entregue'
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

    from app.constants import ESTADO_LABEL, STATUS_PEDIDO_FINALIZADOS
    from app.models import PedidoItem, VendaB2BItem
    dia = _parse_dia(request.args.get('data')) or hoje()
    alvo = dia + timedelta(days=1)
    itens = (PedidoItem.query.join(PedidoLoja)
             .filter(PedidoLoja.data_entrega == alvo,
                     ~PedidoLoja.status.in_(STATUS_PEDIDO_FINALIZADOS),
                     PedidoItem.estado.in_(('assado', 'backup')))
             .all())
    agg = defaultdict(int)
    for it in itens:
        loja = it.pedido.loja.nome if (it.pedido and it.pedido.loja) else '—'
        agg[(loja, it.nome_item, it.estado)] += (it.quantidade or 0)
    # B2B do dia seguinte com estado [ASSADO]/[BACKUP] entram no mesmo pre-preparo.
    itens_b2b = (VendaB2BItem.query.join(VendaB2B)
                 .filter(VendaB2B.data_entrega == alvo,
                         VendaB2B.status != 'cancelada',
                         VendaB2B.status_entrega != 'entregue',
                         VendaB2BItem.estado.in_(('assado', 'backup')))
                 .all())
    for it in itens_b2b:
        cli = ('B2B · ' + it.venda.cliente_display) if it.venda else 'B2B'
        agg[(cli, it.nome_item, it.estado)] += (it.quantidade or 0)
    linhas = [{'loja': lj, 'nome': n, 'estado': e,
               'estado_label': ESTADO_LABEL.get(e, e.upper()), 'qtd': q}
              for (lj, n, e), q in agg.items()]
    linhas.sort(key=lambda x: (x['loja'], x['estado_label'], -x['qtd'], x['nome']))
    # Alerta de pre-preparo: so a partir das 17h50 (BRT) e havendo itens.
    from app.utils import agora
    ag = agora()
    alertar = bool(linhas) and (ag.hour, ag.minute) >= (17, 50)
    return jsonify(dia=alvo.strftime('%d/%m'), alvo_iso=alvo.isoformat(),
                   itens=linhas, alertar=alertar)


@padeiro_bp.route('/buscar-receitas.json')
@login_required
@padeiro_required
def buscar_receitas():
    """Typeahead do painel Produzir: receitas E produtos/cestas cujo nome contem
    o texto. Acento-insensivel ('pao' acha 'Pão'), case-insensitive, casa todos
    os termos. Catalogo pequeno -> filtra em Python. Retorna refs
    'receita:<id>' / 'produto:<id>'."""
    import unicodedata

    from flask import jsonify

    from app.models import Produto, Receita

    def _norm(s):
        s = unicodedata.normalize('NFKD', s or '')
        return ''.join(c for c in s if not unicodedata.combining(c)).lower()

    q = _norm((request.args.get('q') or '').strip())
    if len(q) < 2:
        return jsonify(itens=[])
    termos = q.split()

    def _casa(nome):
        n = _norm(nome)
        return all(t in n for t in termos)

    out = [{'ref': 'receita:%d' % r.id, 'nome': r.nome}
           for r in Receita.query.order_by(Receita.nome).all() if _casa(r.nome)]
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
        resumo = []
        for tipo, obj, qtd in validados:
            entrada_producao(
                receita_id=obj.id if tipo == 'receita' else None,
                produto_id=obj.id if tipo == 'produto' else None,
                estado=None, quantidade=qtd, usuario_id=current_user.id,
                referencia='Produção (TV padeiro)')
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
