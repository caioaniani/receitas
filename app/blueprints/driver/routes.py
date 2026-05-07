"""Pagina mobile-first do driver: ve entregas do dia, marca status, sobe fotos.

Acesso por URL /driver/<token>. PIN exigido na primeira vez e armazenado em
session pra nao pedir de novo.
"""
import secrets
from datetime import date, datetime

from flask import (
    Blueprint, current_app, jsonify, render_template, request, session, abort
)

from app.extensions import db
from app.models import (
    AtribuicaoEntrega, Driver, EntregaFoto,
)
from app.services import vnda
from app.services import dropbox_storage
from app.blueprints.driver import driver_bp


def _gerar_token():
    return secrets.token_urlsafe(16)


def _gerar_proof_hash():
    return secrets.token_urlsafe(12)


def _driver_por_token(token):
    if not token:
        return None
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
    return render_template('driver/index.html', driver=driver, hoje=date.today().isoformat())


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


@driver_bp.route('/api/<token>/pedidos')
def api_pedidos(token):
    driver = _driver_por_token(token)
    if not driver or not driver.ativo:
        return jsonify(ok=False, erro='Driver invalido'), 404
    if not _autenticado(driver):
        return jsonify(ok=False, erro='Autenticacao necessaria', precisa_pin=True), 401

    data_str = request.args.get('data', date.today().isoformat())
    try:
        target = datetime.strptime(data_str, '%Y-%m-%d').date()
    except ValueError:
        target = date.today()

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

    return jsonify(
        ok=True,
        data=data_str,
        driver={'id': driver.id, 'nome': driver.nome, 'cor': driver.cor},
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
        a.entregue_em = datetime.utcnow()
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
