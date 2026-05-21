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

Toda tentativa eh registrada em HandshakeAudit pra investigar falhas.
"""
import io
import logging

from flask import abort, flash, render_template, request, send_file, session, url_for

from app.blueprints.handshake import handshake_bp
from app.extensions import csrf, db
from app.models import Driver, HandshakeAudit, PedidoQRCode
from app.utils import agora

logger = logging.getLogger(__name__)


# CSRF off: handshake e aberto (mobile, sem login). Atomicidade vem do
# token unico + PIN; nao precisa de cookie de sessao Flask aqui.
csrf.exempt(handshake_bp)


def _audit(token, pedido, tipo, etapa, detalhe=None):
    """Registra tentativa de handshake. Nao falha o request se audit der erro."""
    try:
        ua = (request.headers.get('User-Agent') or '')[:300]
        ip = request.headers.get('X-Forwarded-For', request.remote_addr or '')[:45]
        db.session.add(HandshakeAudit(
            token=token, pedido_id=pedido.id if pedido else None,
            tipo=tipo, etapa=etapa, detalhe=(detalhe or '')[:500],
            status_pedido=pedido.status if pedido else None,
            ip=ip, user_agent=ua,
        ))
        db.session.commit()
    except Exception:
        logger.exception('handshake audit falhou')
        db.session.rollback()


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


def _qr_ativo(token, etapa_esperada=None):
    """Resolve token + valida (existe, nao expirou, nao usado, etapa bate).
    Retorna (qr, pedido, erro_msg). Em erro, qr/pedido=None."""
    qr = PedidoQRCode.query.filter_by(token=token).first()
    if not qr:
        return None, None, 'Token nao encontrado.'
    if not qr.valido:
        motivo = 'expirado' if qr.expira_em <= agora() else 'ja usado'
        return None, None, f'QR Code esta {motivo}.'
    if etapa_esperada and qr.tipo != etapa_esperada:
        return None, None, f'QR de tipo {qr.tipo}, esperava {etapa_esperada}.'
    return qr, qr.pedido, None


@handshake_bp.route('/<token>/foto/<int:item_id>', methods=['POST'])
def upload_foto(token, item_id):
    """Upload de foto de conferencia por SKU, autenticado por QR token.

    Quem usa:
    - SAIDA: motorista (apos escanear QR de saida).
    - ENTREGA: loja (apos escanear QR de entrega).

    Substitui foto anterior do mesmo item+etapa se existir."""
    from app.models import PedidoItem
    from app.services.conferencia import salvar_foto
    qr, pedido, erro = _qr_ativo(token)
    if erro:
        return {'ok': False, 'erro': erro}, 400
    item = PedidoItem.query.get_or_404(item_id)
    if item.pedido_id != pedido.id:
        return {'ok': False, 'erro': 'item nao pertence ao pedido'}, 400
    file = request.files.get('foto')
    if not file:
        return {'ok': False, 'erro': 'campo foto ausente'}, 400
    foto, erro_salvar = salvar_foto(item.id, qr.tipo, file)
    if erro_salvar:
        return {'ok': False, 'erro': erro_salvar}, 400
    return {'ok': True, 'foto_id': foto.id,
            'url': url_for('handshake.foto_serve',
                            token=token, foto_id=foto.id)}


@handshake_bp.route('/<token>/foto/<int:foto_id>.jpg')
def foto_serve(token, foto_id):
    """Serve a imagem associada a um QR token ativo."""
    from app.models import PedidoItemFoto
    qr, pedido, erro = _qr_ativo(token)
    if erro:
        abort(404)
    foto = PedidoItemFoto.query.get_or_404(foto_id)
    if foto.pedido_item.pedido_id != pedido.id:
        abort(404)
    return send_file(io.BytesIO(foto.imagem),
                      mimetype=foto.mimetype or 'image/jpeg',
                      max_age=0)


@handshake_bp.route('/<token>', methods=['GET', 'POST'])
def handshake(token):
    qr = PedidoQRCode.query.filter_by(token=token).first()
    if not qr:
        _audit(token, None, None, 'scan_falha', 'token nao encontrado')
        return render_template('handshake/erro.html',
                                msg='Token nao encontrado. Pode ja ter sido usado ou estar errado.'), 404
    if not qr.valido:
        motivo = 'expirado' if qr.expira_em <= agora() else 'ja usado'
        _audit(token, qr.pedido, qr.tipo, 'scan_falha', f'qr {motivo}')
        return render_template('handshake/erro.html',
                                msg=f'Este QR Code esta {motivo}.'), 410

    pedido = qr.pedido
    # Validacao de estado: garante que pedido ainda esta no status certo
    if qr.tipo == 'saida' and pedido.status != 'separado':
        _audit(token, pedido, qr.tipo, 'erro_status',
               f'esperava separado, achou {pedido.status}')
        return render_template('handshake/erro.html',
                                msg=f'Pedido #{pedido.id} nao esta mais aguardando saida (status: {pedido.status}). '
                                    'Peca pro admin recolocar como separado, ou usar "Forcar entrega" na ficha do pedido.'), 409
    if qr.tipo == 'entrega' and pedido.status != 'em_transporte':
        _audit(token, pedido, qr.tipo, 'erro_status',
               f'esperava em_transporte, achou {pedido.status}')
        return render_template('handshake/erro.html',
                                msg=f'Pedido #{pedido.id} nao esta em transporte (status: {pedido.status}). '
                                    'Peca pro admin executar o QR de saida antes, ou usar "Forcar entrega" na ficha do pedido.'), 409

    from app.services.conferencia import faltam_fotos, fotos_presentes
    fotos = fotos_presentes(pedido, qr.tipo)
    n_falta = len(faltam_fotos(pedido, qr.tipo))

    if request.method == 'GET':
        _audit(token, pedido, qr.tipo, 'scan', 'pagina aberta')
        return render_template('handshake/confirmar.html', qr=qr, pedido=pedido,
                                fotos=fotos, n_falta=n_falta)

    # POST: precisa de TODAS as fotos antes do PIN
    if n_falta > 0:
        _audit(token, pedido, qr.tipo, 'pin_sem_fotos', f'faltam {n_falta}')
        flash(f'Tire foto dos {n_falta} item(ns) que falta(m) antes do PIN.', 'danger')
        return render_template('handshake/confirmar.html', qr=qr, pedido=pedido,
                                fotos=fotos, n_falta=n_falta), 400

    pin_enviado = (request.form.get('pin') or '').strip()
    if not pin_enviado:
        _audit(token, pedido, qr.tipo, 'pin_vazio')
        flash('Digite o PIN.', 'danger')
        return render_template('handshake/confirmar.html', qr=qr, pedido=pedido,
                                fotos=fotos, n_falta=n_falta), 400

    if qr.tipo == 'saida':
        return _handshake_saida(qr, pedido, pin_enviado)
    if qr.tipo == 'entrega':
        return _handshake_entrega(qr, pedido, pin_enviado)
    _audit(token, pedido, qr.tipo, 'erro_tipo')
    return render_template('handshake/erro.html', msg='Tipo de QR invalido.'), 400


def _handshake_saida(qr, pedido, pin):
    """PIN do motorista → muda status pra em_transporte."""
    from app.blueprints.pedidos.routes import _executar_envio_pedido
    drivers = Driver.query.filter_by(ativo=True).all()
    driver_match = next((d for d in drivers if d.pin and d.pin == pin), None)
    if not driver_match:
        _audit(qr.token, pedido, qr.tipo, 'pin_fail', f'PIN tentado: {pin[:4]}***')
        flash('PIN invalido. Confirme com o gerente.', 'danger')
        from app.services.conferencia import faltam_fotos, fotos_presentes
        return render_template('handshake/confirmar.html', qr=qr, pedido=pedido,
                                fotos=fotos_presentes(pedido, qr.tipo),
                                n_falta=len(faltam_fotos(pedido, qr.tipo))), 401
    _audit(qr.token, pedido, qr.tipo, 'pin_ok', f'driver:{driver_match.nome}')
    try:
        ok, msg = _executar_envio_pedido(
            pedido, user=None,
            ref_extra=f'via QR / motorista {driver_match.nome}',
        )
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        _audit(qr.token, pedido, qr.tipo, 'erro_executor', str(exc)[:500])
        return render_template('handshake/erro.html',
                                msg=f'Erro ao processar: {exc}'), 500
    if not ok:
        _audit(qr.token, pedido, qr.tipo, 'erro_executor', msg[:500])
        return render_template('handshake/erro.html', msg=msg), 409
    qr.usado_em = agora()
    qr.usado_por_descricao = f'driver:{driver_match.nome}'
    # Amarra pedido ao motorista que pegou: painel /driver/<token>
    # filtra por driver_id pra cada motorista so ver os pedidos que coletou.
    pedido.driver_id = driver_match.id
    db.session.commit()
    _audit(qr.token, pedido, qr.tipo, 'sucesso', f'driver:{driver_match.nome}')
    # Marca o motorista autenticado na session do navegador dele.
    # Sem isso, o proximo passo (/driver/<token>/pedido/<id>/qr-entrega)
    # cairia em "Faça login no painel do motorista" — o PIN ja foi validado
    # aqui, nao faz sentido pedir de novo.
    if driver_match.pin:
        session[f'driver_auth_{driver_match.id}'] = True
    # Link de proximo passo: motorista vai na loja gerar QR de entrega.
    # driver_match.token leva pra pagina do motorista; daí ele clica em
    # 'Pedidos de loja' ou usa o link direto pro pedido.
    proximo_url = url_for('driver.qr_entrega',
                           token=driver_match.token,
                           pedido_id=pedido.id, _external=True)
    return render_template('handshake/sucesso.html',
                            msg=f'Saida confirmada por {driver_match.nome}.',
                            pedido=pedido,
                            proximo_label='Conferir e entregar na loja',
                            proximo_url=proximo_url)


def _handshake_entrega(qr, pedido, pin):
    """PIN da loja → muda status pra entregue."""
    from app.blueprints.pedidos.routes import _executar_recebimento_pedido
    loja = pedido.loja
    if not loja or not loja.pin:
        _audit(qr.token, pedido, qr.tipo, 'erro_loja_sem_pin', f'loja:{loja.nome if loja else "?"}')
        return render_template('handshake/erro.html',
                                msg=f'Loja {loja.nome if loja else "?"} sem PIN cadastrado. Admin precisa definir em /rh/lojas.'), 409
    if pin != loja.pin:
        _audit(qr.token, pedido, qr.tipo, 'pin_fail', f'loja:{loja.nome} pin_tentado:{pin[:2]}***')
        flash('PIN invalido. Confirme com o gerente da loja.', 'danger')
        from app.services.conferencia import faltam_fotos, fotos_presentes
        return render_template('handshake/confirmar.html', qr=qr, pedido=pedido,
                                fotos=fotos_presentes(pedido, qr.tipo),
                                n_falta=len(faltam_fotos(pedido, qr.tipo))), 401
    _audit(qr.token, pedido, qr.tipo, 'pin_ok', f'loja:{loja.nome}')
    try:
        ok, msg, divergencias = _executar_recebimento_pedido(
            pedido, user=None,
            recebidos_map=None,  # sem divergencia (loja confirmou tudo)
            ref_extra=f'via QR / loja {loja.nome}',
        )
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        _audit(qr.token, pedido, qr.tipo, 'erro_executor', str(exc)[:500])
        return render_template('handshake/erro.html',
                                msg=f'Erro ao processar: {exc}'), 500
    if not ok:
        _audit(qr.token, pedido, qr.tipo, 'erro_executor', msg[:500])
        return render_template('handshake/erro.html', msg=msg), 409
    qr.usado_em = agora()
    qr.usado_por_descricao = f'loja:{loja.nome}'
    db.session.commit()
    _audit(qr.token, pedido, qr.tipo, 'sucesso', f'loja:{loja.nome}')
    return render_template('handshake/sucesso.html',
                            msg=f'Entrega confirmada em {loja.nome}.',
                            pedido=pedido)
