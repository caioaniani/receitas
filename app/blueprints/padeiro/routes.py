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
from app.models import Driver, PedidoLoja
from app.utils import hoje

logger = logging.getLogger(__name__)

_A_SEPARAR = ('pendente', 'confirmado')


def _parse_dia(valor):
    """Parse 'YYYY-MM-DD' -> date, ou None se invalido/vazio."""
    try:
        return datetime.strptime((valor or '').strip(), '%Y-%m-%d').date()
    except ValueError:
        return None


@padeiro_bp.route('/')
@login_required
@padeiro_required
def index():
    hj = hoje()
    dia = _parse_dia(request.args.get('data')) or hj
    eh_hoje = (dia == hj)
    q = PedidoLoja.query.filter(
        PedidoLoja.status.in_(('pendente', 'confirmado', 'separado')))
    if eh_hoje:
        # Hoje inclui atrasados nao despachados (nada se perde).
        q = q.filter((PedidoLoja.data_entrega <= hj)
                     | (PedidoLoja.data_entrega.is_(None)))
    else:
        q = q.filter(PedidoLoja.data_entrega == dia)
    pedidos = q.order_by(PedidoLoja.data_entrega).all()
    a_separar = [p for p in pedidos if p.status in _A_SEPARAR]
    aguardando = [p for p in pedidos if p.status == 'separado']
    drivers = Driver.query.filter_by(ativo=True).order_by(Driver.nome).all()
    # Repetidos = pedidos a mais por (loja, status, data) que dariam pra juntar.
    grupos = Counter((p.loja_id, p.status, p.data_entrega) for p in a_separar)
    n_repetidos = sum(c - 1 for c in grupos.values() if c > 1)
    return render_template(
        'padeiro/index.html', a_separar=a_separar, aguardando=aguardando,
        drivers=drivers, dia=dia, eh_hoje=eh_hoje, n_repetidos=n_repetidos,
        dia_anterior=(dia - timedelta(days=1)).isoformat(),
        dia_seguinte=(dia + timedelta(days=1)).isoformat())


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
