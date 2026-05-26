"""Tela touchscreen do padeiro (chao de fabrica).

Fluxo (botoes grandes):
1. Pedidos do dia pra separar  → botao SEPARAR.
2. Pedido separado, motorista chegou → padeiro escolhe o motorista → GERAR QR.

Reusa a logica existente: status 'separado' (igual `pedidos.separar`), helper
`handshake_qr.gerar_qr_saida` e o handshake em `/handshake/<token>`.
"""
import logging
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
    import traceback
    try:
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
        return render_template(
            'padeiro/index.html', a_separar=a_separar, aguardando=aguardando,
            drivers=drivers, dia=dia, eh_hoje=eh_hoje,
            dia_anterior=(dia - timedelta(days=1)).isoformat(),
            dia_seguinte=(dia + timedelta(days=1)).isoformat())
    except Exception as e:  # noqa: BLE001 - diagnostico temporario
        logger.exception('padeiro.index falhou')
        return ('<pre style="font-size:12px;white-space:pre-wrap;padding:16px">'
                'DIAGNOSTICO PADEIRO (temporario)\n\n'
                f'{type(e).__name__}: {e}\n\n{traceback.format_exc()}</pre>'), 500


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
