"""Handshake fisico via QR Code.

Fluxo (saida industria):
1. Producao gera QR no `/pedidos/<id>/qr-saida` (URL aponta pra /handshake/<token>)
2. Motorista escaneia com celular, abre form pedindo PIN do Driver
3. Valida PIN. OK → muda status do pedido pra `em_transporte` + baixa estoque industria.

Fluxo (entrega loja):
1. Motorista gera QR no `/driver/<token>/pedido/<id>/qr-entrega`
2. Funcionario da loja escaneia, abre form pedindo PIN da Loja
3. Valida PIN. OK → muda status pra `entregue` + sobe estoque loja.

Tokens tem TTL de 2h e sao single-use (usado_em preenchido). Pin invalido
mostra erro mas mantem token vivo (pode tentar de novo).
"""
import io

from flask import render_template, request, redirect, url_for, flash, send_file, abort

from app.blueprints.handshake import handshake_bp
from app.extensions import db, csrf
from app.models import PedidoQRCode, Driver, Loja
from app.utils import agora


# CSRF off: handshake e aberto (mobile, sem login). Atomicidade vem do
# token unico + PIN; nao precisa de cookie de sessao Flask aqui.
csrf.exempt(handshake_bp)


@handshake_bp.route('/qr-img/<token>.png')
def qr_img(token):
    """Serve o PNG do QR Code direto pelo token. Usado pelo Slack pra
    embed via image block. Token aqui SO valida que existe — nao consome
    nem expira o handshake."""
    import qrcode
    qr_row = PedidoQRCode.query.filter_by(token=token).first()
    if not qr_row:
        abort(404)
    url = url_for('handshake.handshake', token=token, _external=True)
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return send_file(buf, mimetype='image/png',
                      download_name=f'qr-{token[:8]}.png',
                      max_age=0)


@handshake_bp.route('/<token>', methods=['GET', 'POST'])
def handshake(token):
    qr = PedidoQRCode.query.filter_by(token=token).first()
    if not qr:
        return render_template('handshake/erro.html',
                                msg='Token nao encontrado. Pode ja ter sido usado ou estar errado.'), 404
    if not qr.valido:
        motivo = 'expirado' if qr.expira_em <= agora() else 'ja usado'
        return render_template('handshake/erro.html',
                                msg=f'Este QR Code esta {motivo}.'), 410

    pedido = qr.pedido
    # Validacao de estado: garante que pedido ainda esta no status certo
    if qr.tipo == 'saida' and pedido.status != 'separado':
        return render_template('handshake/erro.html',
                                msg=f'Pedido #{pedido.id} nao esta mais aguardando saida (status: {pedido.status}).'), 409
    if qr.tipo == 'entrega' and pedido.status != 'em_transporte':
        return render_template('handshake/erro.html',
                                msg=f'Pedido #{pedido.id} nao esta em transporte (status: {pedido.status}).'), 409

    if request.method == 'GET':
        return render_template('handshake/confirmar.html', qr=qr, pedido=pedido)

    # POST: valida PIN, executa
    pin_enviado = (request.form.get('pin') or '').strip()
    if not pin_enviado:
        flash('Digite o PIN.', 'danger')
        return render_template('handshake/confirmar.html', qr=qr, pedido=pedido), 400

    if qr.tipo == 'saida':
        return _handshake_saida(qr, pedido, pin_enviado)
    if qr.tipo == 'entrega':
        return _handshake_entrega(qr, pedido, pin_enviado)
    return render_template('handshake/erro.html', msg='Tipo de QR invalido.'), 400


def _handshake_saida(qr, pedido, pin):
    """PIN do motorista → muda status pra em_transporte."""
    from app.blueprints.pedidos.routes import _executar_envio_pedido
    drivers = Driver.query.filter_by(ativo=True).all()
    driver_match = next((d for d in drivers if d.pin and d.pin == pin), None)
    if not driver_match:
        flash('PIN invalido. Confirme com o gerente.', 'danger')
        return render_template('handshake/confirmar.html', qr=qr, pedido=pedido), 401
    try:
        ok, msg = _executar_envio_pedido(
            pedido, user=None,
            ref_extra=f'via QR / motorista {driver_match.nome}',
        )
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        return render_template('handshake/erro.html',
                                msg=f'Erro ao processar: {exc}'), 500
    if not ok:
        return render_template('handshake/erro.html', msg=msg), 409
    qr.usado_em = agora()
    qr.usado_por_descricao = f'driver:{driver_match.nome}'
    db.session.commit()
    # Link de proximo passo: motorista vai na loja gerar QR de entrega.
    # driver_match.token leva pra pagina do motorista; daí ele clica em
    # 'Pedidos de loja' ou usa o link direto pro pedido.
    proximo_url = url_for('driver.qr_entrega',
                           token=driver_match.token,
                           pedido_id=pedido.id, _external=True)
    return render_template('handshake/sucesso.html',
                            msg=f'Saida confirmada por {driver_match.nome}.',
                            pedido=pedido,
                            proximo_label='Quando chegar na loja, gerar QR de entrega',
                            proximo_url=proximo_url)


def _handshake_entrega(qr, pedido, pin):
    """PIN da loja → muda status pra entregue."""
    from app.blueprints.pedidos.routes import _executar_recebimento_pedido
    loja = pedido.loja
    if not loja or not loja.pin:
        return render_template('handshake/erro.html',
                                msg=f'Loja {loja.nome if loja else "?"} sem PIN cadastrado. Admin precisa definir em /rh/lojas.'), 409
    if pin != loja.pin:
        flash('PIN invalido. Confirme com o gerente da loja.', 'danger')
        return render_template('handshake/confirmar.html', qr=qr, pedido=pedido), 401
    try:
        ok, msg, divergencias = _executar_recebimento_pedido(
            pedido, user=None,
            recebidos_map=None,  # sem divergencia (loja confirmou tudo)
            ref_extra=f'via QR / loja {loja.nome}',
        )
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        return render_template('handshake/erro.html',
                                msg=f'Erro ao processar: {exc}'), 500
    if not ok:
        return render_template('handshake/erro.html', msg=msg), 409
    qr.usado_em = agora()
    qr.usado_por_descricao = f'loja:{loja.nome}'
    db.session.commit()
    return render_template('handshake/sucesso.html',
                            msg=f'Entrega confirmada em {loja.nome}.',
                            pedido=pedido)
