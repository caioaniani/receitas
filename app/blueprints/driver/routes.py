"""Pagina mobile-first do driver: ve entregas do dia, marca status, sobe fotos.

Acesso por URL /driver/<token>. PIN exigido na primeira vez e armazenado em
session pra nao pedir de novo.
"""
import secrets
from datetime import datetime

from flask import (
    abort,
    current_app,
    jsonify,
    render_template,
    request,
    session,
    url_for,
)

from app.blueprints.driver import driver_bp
from app.extensions import db
from app.models import (
    AtribuicaoEntrega,
    Driver,
    EntregaFoto,
    PedidoLoja,
    PedidoQRCode,
)
from app.services import dropbox_storage, vnda
from app.utils import agora
from app.utils import hoje as hoje_brt


def _gerar_token():
    return secrets.token_urlsafe(16)


def _gerar_proof_hash():
    return secrets.token_urlsafe(12)


def _driver_por_token(token):
    """Resolve token → Driver. Aceita:
    - Magic token diario (DriverMagicToken valido) — modelo novo, rotaciona 5h BRT.
    - Driver.token legado (compat com URLs antigas que ja estao circulando).
    """
    if not token:
        return None
    from app.services.driver_magic import driver_por_magic_token
    drv = driver_por_magic_token(token)
    if drv:
        return drv
    return Driver.query.filter_by(token=token).first()


def _pin_ok(driver, pin_enviado):
    if not driver.pin:
        return True  # Sem PIN configurado = livre
    return (pin_enviado or '').strip() == driver.pin


def _autenticado(driver):
    """Retorna True se o cliente ja passou o PIN nesta sessao."""
    if not driver.pin:
        return True
    chave = f'driver_auth_{driver.id}'
    return session.get(chave) is True


def _marcar_autenticado(driver):
    if driver.pin:
        session[f'driver_auth_{driver.id}'] = True


def _enriquecer_pedido(pedido, atrib):
    """Adiciona status/fotos do pedido ao dict que o frontend recebe."""
    fotos = [{'id': f.id, 'url': f.url} for f in atrib.fotos.order_by(EntregaFoto.tirada_em).all()] if atrib else []
    return {
        **pedido,
        'atribuicao_id': atrib.id if atrib else None,
        'status': (atrib.status if atrib else None) or 'pendente',
        'entregue_em': atrib.entregue_em.isoformat() if atrib and atrib.entregue_em else None,
        'nota': atrib.nota if atrib else None,
        'motivo_falha': atrib.motivo_falha if atrib else None,
        'fotos': fotos,
        'proof_hash': atrib.proof_hash if atrib else None,
    }


@driver_bp.route('/<token>')
def index(token):
    driver = _driver_por_token(token)
    if not driver or not driver.ativo:
        abort(404)
    return render_template('driver/index.html', driver=driver,
                            token=token, hoje=hoje_brt().isoformat())


@driver_bp.route('/api/<token>/login', methods=['POST'])
def api_login(token):
    driver = _driver_por_token(token)
    if not driver or not driver.ativo:
        return jsonify(ok=False, erro='Driver invalido'), 404
    body = request.get_json(silent=True) or {}
    if not _pin_ok(driver, body.get('pin')):
        return jsonify(ok=False, erro='PIN incorreto'), 401
    _marcar_autenticado(driver)
    return jsonify(ok=True, driver={'id': driver.id, 'nome': driver.nome, 'cor': driver.cor})


@driver_bp.route('/api/<token>/proxima_data')
def api_proxima_data(token):
    """Retorna a primeira data >= hoje com entregas atribuidas pra esse driver.
    Se nao houver futura, devolve a mais recente passada. Se nao houver nenhuma,
    retorna hoje. Usado pra abrir a tela do driver ja na data certa."""
    driver = _driver_por_token(token)
    if not driver or not driver.ativo:
        return jsonify(ok=False, erro='Driver invalido'), 404
    if not _autenticado(driver):
        return jsonify(ok=False, erro='Autenticacao necessaria', precisa_pin=True), 401

    hoje = hoje_brt()
    futura = (AtribuicaoEntrega.query
              .filter(AtribuicaoEntrega.driver_id == driver.id,
                      AtribuicaoEntrega.data_entrega >= hoje)
              .order_by(AtribuicaoEntrega.data_entrega.asc())
              .first())
    if futura and futura.data_entrega:
        return jsonify(ok=True, data=futura.data_entrega.isoformat())
    passada = (AtribuicaoEntrega.query
               .filter(AtribuicaoEntrega.driver_id == driver.id,
                       AtribuicaoEntrega.data_entrega.isnot(None))
               .order_by(AtribuicaoEntrega.data_entrega.desc())
               .first())
    if passada and passada.data_entrega:
        return jsonify(ok=True, data=passada.data_entrega.isoformat())
    return jsonify(ok=True, data=hoje.isoformat())


@driver_bp.route('/api/<token>/debug')
def api_debug(token):
    """Diagnostico: porque um pedido aparece (ou nao) pro driver na data X."""
    driver = _driver_por_token(token)
    if not driver:
        return jsonify(ok=False, erro='token invalido'), 404
    data_str = request.args.get('data', hoje_brt().isoformat())
    try:
        target = datetime.strptime(data_str, '%Y-%m-%d').date()
    except ValueError:
        target = hoje_brt()

    autenticado = _autenticado(driver)
    atribs_data = AtribuicaoEntrega.query.filter(
        AtribuicaoEntrega.driver_id == driver.id,
        AtribuicaoEntrega.data_entrega == target,
    ).all()
    todas_atribs = AtribuicaoEntrega.query.filter_by(driver_id=driver.id).count()

    resultado = vnda.buscar_pedidos_do_dia(target, overrides={})
    vnda_codes = [p.get('code') for p in resultado.get('pedidos', []) if p.get('code')]
    matches = [a.pedido_code for a in atribs_data if a.pedido_code in vnda_codes]
    so_no_banco = [a.pedido_code for a in atribs_data if a.pedido_code not in vnda_codes]

    return jsonify(
        ok=True,
        driver={'id': driver.id, 'nome': driver.nome, 'ativo': driver.ativo},
        autenticado=autenticado,
        data=data_str,
        vnda_pedidos_na_data=len(vnda_codes),
        vnda_erro=resultado.get('erro'),
        atribuicoes_total_driver=todas_atribs,
        atribuicoes_nessa_data=len(atribs_data),
        atribuicoes_codes=[a.pedido_code for a in atribs_data],
        matches_vnda_x_atribuicao=len(matches),
        atribuicoes_sem_pedido_vnda=so_no_banco,
    )


@driver_bp.route('/api/<token>/pedidos')
def api_pedidos(token):
    driver = _driver_por_token(token)
    if not driver or not driver.ativo:
        return jsonify(ok=False, erro='Driver invalido'), 404
    if not _autenticado(driver):
        return jsonify(ok=False, erro='Autenticacao necessaria', precisa_pin=True), 401

    data_str = request.args.get('data', hoje_brt().isoformat())
    try:
        target = datetime.strptime(data_str, '%Y-%m-%d').date()
    except ValueError:
        target = hoje_brt()

    # Reaproveita a busca da API geral
    overrides = {}  # nao mexe em overrides na pagina do driver
    resultado = vnda.buscar_pedidos_do_dia(target, overrides=overrides)
    if 'erro' in resultado:
        return jsonify(ok=False, erro=resultado['erro']), 502

    pedidos = resultado.get('pedidos', [])
    codes = [p['code'] for p in pedidos if p.get('code')]
    if not codes:
        return jsonify(ok=True, pedidos=[], driver={'id': driver.id, 'nome': driver.nome, 'cor': driver.cor})

    atribs = AtribuicaoEntrega.query.filter(
        AtribuicaoEntrega.pedido_code.in_(codes),
        AtribuicaoEntrega.driver_id == driver.id,
    ).all()
    atribs_por_code = {a.pedido_code: a for a in atribs}

    pedidos_driver = []
    for p in pedidos:
        a = atribs_por_code.get(p['code'])
        if not a:
            continue
        pedidos_driver.append(_enriquecer_pedido(p, a))

    pedidos_driver.sort(key=lambda x: ((x.get('atribuicao_id') and atribs_por_code[x['code']].ordem) or 0, x.get('periodo') or ''))

    from app.services import rotas as rotas_svc
    return jsonify(
        ok=True,
        data=data_str,
        driver={'id': driver.id, 'nome': driver.nome, 'cor': driver.cor},
        origem_endereco=rotas_svc.origem_endereco(current_app),
        pedidos=pedidos_driver,
    )


@driver_bp.route('/api/<token>/status', methods=['POST'])
def api_status(token):
    driver = _driver_por_token(token)
    if not driver or not driver.ativo:
        return jsonify(ok=False, erro='Driver invalido'), 404
    if not _autenticado(driver):
        return jsonify(ok=False, erro='Autenticacao necessaria'), 401

    body = request.get_json(silent=True) or {}
    atrib_id = body.get('atribuicao_id')
    novo_status = (body.get('status') or '').strip()
    if novo_status not in ('pendente', 'entregue', 'nao_entregue'):
        return jsonify(ok=False, erro='Status invalido'), 400

    a = AtribuicaoEntrega.query.get(atrib_id)
    if not a or a.driver_id != driver.id:
        return jsonify(ok=False, erro='Pedido nao pertence a este driver'), 403

    a.status = novo_status
    a.nota = (body.get('nota') or '')[:500] or None
    if novo_status == 'entregue':
        a.entregue_em = agora()
        a.motivo_falha = None
        if not a.proof_hash:
            a.proof_hash = _gerar_proof_hash()
    elif novo_status == 'nao_entregue':
        a.motivo_falha = (body.get('motivo_falha') or 'outro')[:50]
        a.entregue_em = None
    else:
        a.entregue_em = None
        a.motivo_falha = None

    geo = body.get('geo') or {}
    if geo.get('lat') is not None and geo.get('lng') is not None:
        try:
            a.geo_lat = float(geo['lat'])
            a.geo_lng = float(geo['lng'])
        except (TypeError, ValueError):
            pass

    # Recalcula status do lote dono (aberto / em_rota / concluido)
    if a.lote_id:
        from app.blueprints.entregas.routes import _recompute_lote_status
        _recompute_lote_status(a.lote_id)

    db.session.commit()
    return jsonify(ok=True, status=a.status, proof_hash=a.proof_hash)


@driver_bp.route('/api/<token>/foto', methods=['POST'])
def api_foto(token):
    driver = _driver_por_token(token)
    if not driver or not driver.ativo:
        return jsonify(ok=False, erro='Driver invalido'), 404
    if not _autenticado(driver):
        return jsonify(ok=False, erro='Autenticacao necessaria'), 401

    atrib_id = request.form.get('atribuicao_id', type=int)
    if not atrib_id:
        return jsonify(ok=False, erro='atribuicao_id ausente'), 400
    a = AtribuicaoEntrega.query.get(atrib_id)
    if not a or a.driver_id != driver.id:
        return jsonify(ok=False, erro='Pedido nao pertence a este driver'), 403

    if a.fotos.count() >= 3:
        return jsonify(ok=False, erro='Maximo de 3 fotos por entrega'), 400

    arquivo = request.files.get('foto')
    if not arquivo:
        return jsonify(ok=False, erro='Foto ausente'), 400

    raw = arquivo.read()
    if not raw:
        return jsonify(ok=False, erro='Arquivo vazio'), 400
    if len(raw) > 10 * 1024 * 1024:  # 10 MB
        return jsonify(ok=False, erro='Foto maior que 10MB'), 400

    if not dropbox_storage.disponivel():
        return jsonify(ok=False, erro='Storage nao configurado'), 500

    try:
        info = dropbox_storage.upload_foto(raw, atrib_id, ext='jpg')
    except RuntimeError as e:
        return jsonify(ok=False, erro=str(e)), 500

    foto = EntregaFoto(
        atribuicao_id=atrib_id,
        url=info['url'],
        storage_path=info['storage_path'],
        tamanho_bytes=info['tamanho'],
    )
    db.session.add(foto)
    if not a.proof_hash:
        a.proof_hash = _gerar_proof_hash()
    db.session.commit()

    return jsonify(ok=True, foto={'id': foto.id, 'url': foto.url}, proof_hash=a.proof_hash)


@driver_bp.route('/api/<token>/foto/<int:foto_id>', methods=['DELETE'])
def api_foto_delete(token, foto_id):
    driver = _driver_por_token(token)
    if not driver or not driver.ativo:
        return jsonify(ok=False, erro='Driver invalido'), 404
    if not _autenticado(driver):
        return jsonify(ok=False, erro='Autenticacao necessaria'), 401

    foto = EntregaFoto.query.get(foto_id)
    if not foto or foto.atribuicao.driver_id != driver.id:
        return jsonify(ok=False, erro='Foto invalida'), 404

    dropbox_storage.deletar(foto.storage_path)
    db.session.delete(foto)
    db.session.commit()
    return jsonify(ok=True)


# ── Handshake QR de entrega (motorista gera, loja escaneia) ──

@driver_bp.route('/<token>/pedido/<int:pedido_id>/qr-entrega')
def qr_entrega(token, pedido_id):
    """QR de entrega pro motorista mostrar pra loja.

    Motorista so exibe o QR. Conferencia com foto eh feita pela LOJA
    na pagina do handshake apos escanear (ver handshake.routes)."""
    from datetime import timedelta

    from app.services.qrcode_svc import gerar_png_data_url
    driver = _driver_por_token(token)
    if not driver:
        abort(404)
    if not _autenticado(driver):
        return render_template('handshake/erro.html',
                                msg='Faça login no painel do motorista antes (volte e digite o PIN).'), 401
    pedido = PedidoLoja.query.get_or_404(pedido_id)
    if pedido.status != 'em_transporte':
        return render_template('handshake/erro.html',
                                msg=f'Pedido #{pedido.id} nao esta em transporte (status: {pedido.status}).'), 409

    qr = (PedidoQRCode.query
          .filter_by(pedido_id=pedido.id, tipo='entrega', usado_em=None)
          .filter(PedidoQRCode.expira_em > agora())
          .order_by(PedidoQRCode.criado_em.desc()).first())
    if not qr:
        qr = PedidoQRCode(
            token=secrets.token_urlsafe(24),
            pedido_id=pedido.id, tipo='entrega',
            expira_em=agora() + timedelta(hours=2),
        )
        db.session.add(qr)
        db.session.commit()
    url = url_for('handshake.handshake', token=qr.token, _external=True)
    qr_png = gerar_png_data_url(url)
    return render_template('driver/qr_entrega.html',
                            driver=driver, pedido=pedido, qr=qr,
                            url=url, qr_png=qr_png, token=token)


@driver_bp.route('/<token>/pedidos-loja')
def pedidos_loja(token):
    """Lista pedidos de loja em transporte pra motorista escolher e
    gerar QR de entrega. Mostra todos em_transporte (sistema interno,
    poucos motoristas)."""
    driver = _driver_por_token(token)
    if not driver or not driver.ativo:
        abort(404)
    if not _autenticado(driver):
        return render_template('handshake/erro.html',
                                msg='Faça login no painel do motorista (volte e digite o PIN).'), 401
    # So pedidos coletados POR ESTE motorista (handshake de saida amarra
    # PedidoLoja.driver_id). Compat retroativa: pedidos antigos sem
    # driver_id continuam aparecendo pra todos os motoristas ate alguem
    # processar — uma vez processados pelo novo fluxo, ficam amarrados.
    pedidos = (PedidoLoja.query
               .filter_by(status='em_transporte')
               .filter((PedidoLoja.driver_id == driver.id) |
                        (PedidoLoja.driver_id.is_(None)))
               .order_by(PedidoLoja.data_entrega.asc())
               .all())
    return render_template('driver/pedidos_loja.html',
                            driver=driver, pedidos=pedidos, token=token)
