from datetime import date, datetime

from flask import render_template, request, jsonify, abort, current_app
from flask_login import login_required, current_user

import requests as http_requests

from app.blueprints.entregas import entregas_bp
from app.decorators import entrega_access_required
from app.extensions import db
from app.models import CartinhaEntrega, OverrideEntrega
from app.services import vnda, rotas as rotas_svc


@entregas_bp.route('/')
@login_required
@entrega_access_required
def index():
    return render_template('entregas/index.html', hoje=date.today().isoformat())


def _carregar_overrides_data():
    """Retorna dict {pedido_code: date} com todas as datas sobrescritas no ERP."""
    return {o.pedido_code: o.data_entrega for o in OverrideEntrega.query.all()}


def _carregar_overrides_full():
    """Como _carregar_overrides_data mas inclui motivo, autor e data de alteracao."""
    out = {}
    for o in OverrideEntrega.query.all():
        out[o.pedido_code] = {
            'data': o.data_entrega,
            'motivo': o.motivo or '',
            'autor': o.autor.nome if o.autor else '',
            'em': o.atualizado_em.isoformat() if o.atualizado_em else None,
        }
    return out


@entregas_bp.route('/api/pedidos')
@login_required
@entrega_access_required
def api_pedidos():
    data_str = request.args.get('data', date.today().isoformat())
    try:
        target = datetime.strptime(data_str, '%Y-%m-%d').date()
    except ValueError:
        target = date.today()

    overrides_full = _carregar_overrides_full()
    overrides_data = {code: o['data'] for code, o in overrides_full.items()}
    resultado = vnda.buscar_pedidos_do_dia(target, overrides=overrides_data)

    if 'erro' in resultado:
        resp = jsonify(pedidos=[], data=data_str, erro=resultado['erro'])
    else:
        pedidos = resultado.get('pedidos', [])
        total_janela = resultado.get('total_janela', 0)

        codes = [p['code'] for p in pedidos if p['code']]
        cartinhas_manuais = {}
        if codes:
            for c in CartinhaEntrega.query.filter(CartinhaEntrega.pedido_code.in_(codes)).all():
                cartinhas_manuais[c.pedido_code] = c.texto or ''

        # Cartinha manual (editada pelo usuario) tem prioridade sobre a do VNDA
        for p in pedidos:
            manual = cartinhas_manuais.get(p['code'], '')
            auto = p.get('cartinha_vnda', '')
            p['cartinha'] = manual or auto
            p['cartinha_origem'] = 'manual' if manual else ('vnda' if auto else None)

            # Info adicional de override de data
            if p.get('data_override'):
                ov = overrides_full.get(p['code'])
                if ov:
                    p['override_motivo'] = ov['motivo']
                    p['override_autor'] = ov['autor']
                    p['override_em'] = ov['em']

        resp = jsonify(pedidos=pedidos, data=data_str, total_janela=total_janela)

    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    return resp


@entregas_bp.route('/api/calendario')
@login_required
@entrega_access_required
def api_calendario():
    mes_str = request.args.get('mes', '')
    try:
        parts = mes_str.split('-')
        year, month = int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        year, month = date.today().year, date.today().month

    overrides = _carregar_overrides_data()
    dias = vnda.contar_pedidos_por_dia(year, month, overrides=overrides)
    resp = jsonify(dias=dias)
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    return resp


@entregas_bp.route('/cartinha/<code>', methods=['POST'])
@login_required
@entrega_access_required
def salvar_cartinha(code):
    data = request.get_json(silent=True) or {}
    texto = data.get('texto', '').strip()

    c = CartinhaEntrega.query.filter_by(pedido_code=code).first()
    if not c:
        c = CartinhaEntrega(pedido_code=code)
        db.session.add(c)

    c.texto = texto
    c.atualizado_em = datetime.utcnow()
    c.atualizado_por = current_user.id
    db.session.commit()

    return jsonify(ok=True)


@entregas_bp.route('/api/rotas')
@login_required
@entrega_access_required
def api_rotas():
    """Gera rotas de entrega para um dia (k-means + nearest neighbor)."""
    import traceback
    try:
        data_str = request.args.get('data', date.today().isoformat())
        try:
            target = datetime.strptime(data_str, '%Y-%m-%d').date()
        except ValueError:
            target = date.today()

        try:
            n_drivers = int(request.args.get('drivers', '2'))
        except ValueError:
            n_drivers = 2
        n_drivers = max(1, min(n_drivers, 20))

        janela = (request.args.get('janela', '') or '').strip()

        # Re-tentar geocoding pra enderecos que falharam antes
        if request.args.get('retentar') == '1':
            from app.models import GeocodeCache
            n = GeocodeCache.query.filter(GeocodeCache.lat.is_(None)).delete()
            db.session.commit()
            current_app.logger.info('Limpou %d registros de geocode falhos', n)

        overrides = _carregar_overrides_data()
        resultado = vnda.buscar_pedidos_do_dia(target, overrides=overrides)
        if 'erro' in resultado:
            resp = jsonify(rotas=[], erro=resultado['erro'])
            resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
            return resp

        pedidos = resultado.get('pedidos', [])

        # Filtra por janela (periodo) se informada
        if janela:
            pedidos = [p for p in pedidos if (p.get('periodo') or '') == janela]

        origem = rotas_svc.origem_padrao(current_app)
        geradas = rotas_svc.gerar_rotas(pedidos, n_drivers, origem)

        # Disponiveis pra dropdown de janela
        periodos = sorted({p.get('periodo') or '' for p in resultado.get('pedidos', []) if p.get('periodo')})

        resp = jsonify(
            data=data_str,
            drivers=n_drivers,
            janela=janela,
            periodos_disponiveis=periodos,
            rotas=geradas['rotas'],
            sem_geocode=[
                {'code': p['code'], 'destinatario': p.get('destinatario', ''),
                 'endereco': p.get('endereco', '')}
                for p in geradas['sem_geocode']
            ],
            tempo_geo=geradas['tempo_geo'],
            incomplete=geradas.get('incomplete', False),
            origem=geradas['origem'],
        )
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
        return resp
    except Exception as e:
        current_app.logger.error('Erro em api_rotas: %s\n%s', e, traceback.format_exc())
        return jsonify(
            rotas=[],
            erro=f'{type(e).__name__}: {str(e)[:300]}',
            traceback=traceback.format_exc()[-2000:],
        ), 500


@entregas_bp.route('/data/<code>', methods=['POST'])
@login_required
@entrega_access_required
def salvar_data_override(code):
    """Sobrescreve a data de entrega de um pedido (apenas no ERP, nao sincroniza com VNDA)."""
    data = request.get_json(silent=True) or {}
    data_str = (data.get('data') or '').strip()
    motivo = (data.get('motivo') or '').strip() or None

    try:
        nova_data = datetime.strptime(data_str, '%Y-%m-%d').date()
    except ValueError:
        return jsonify(ok=False, erro='data invalida'), 400

    o = OverrideEntrega.query.filter_by(pedido_code=code).first()
    if not o:
        o = OverrideEntrega(pedido_code=code)
        db.session.add(o)

    o.data_entrega = nova_data
    o.motivo = motivo
    o.atualizado_em = datetime.utcnow()
    o.atualizado_por = current_user.id
    db.session.commit()

    return jsonify(ok=True, data=nova_data.isoformat())


@entregas_bp.route('/data/<code>', methods=['DELETE'])
@login_required
@entrega_access_required
def remover_data_override(code):
    """Remove o override; pedido volta a usar a data original do VNDA."""
    o = OverrideEntrega.query.filter_by(pedido_code=code).first()
    if o:
        db.session.delete(o)
        db.session.commit()
    return jsonify(ok=True)


@entregas_bp.route('/cartinha/<code>')
@login_required
@entrega_access_required
def get_cartinha(code):
    c = CartinhaEntrega.query.filter_by(pedido_code=code).first()
    if not c:
        return jsonify(texto='', atualizado_em=None, atualizado_por=None)
    return jsonify(
        texto=c.texto or '',
        atualizado_em=c.atualizado_em.isoformat() if c.atualizado_em else None,
        atualizado_por=c.autor.nome if c.autor else None,
    )


@entregas_bp.route('/api/debug/pedido/<code>')
@login_required
@entrega_access_required
def api_debug_pedido(code):
    """Diagnostico de um pedido especifico."""
    token = current_app.config.get('VNDA_API_TOKEN', '')
    host = current_app.config.get('VNDA_SHOP_HOST', 'www.padariaartesanalonline.com.br')
    if token.lower().startswith('bearer '):
        token = token[7:]

    headers = {
        'Authorization': f'Bearer {token}',
        'X-Shop-Host': host,
        'Accept': 'application/json',
        'User-Agent': 'OPaoPadaria/1.0',
    }

    info = {}

    # 1) Chamada padrao
    try:
        resp = http_requests.get(
            f'https://api.vnda.com.br/api/v2/orders/{code}',
            headers=headers, timeout=15,
        )
        info['status_code'] = resp.status_code
        if resp.status_code == 200:
            try:
                order = resp.json()
            except ValueError:
                info['erro'] = 'resposta nao-json'
                return jsonify(info)

            info['todas_chaves_padrao'] = sorted(order.keys())
            info['shipping_address_padrao'] = order.get('shipping_address')
            info['client_padrao'] = order.get('client')

            for k, v in order.items():
                if isinstance(v, dict):
                    info[k] = {sk: str(sv) for sk, sv in v.items() if sv is not None}
                elif isinstance(v, list):
                    info[k] = str(v)[:5000]
                else:
                    info[k] = v
            info['items_count'] = len(order.get('items') or [])
            info['items_full'] = order.get('items')

            from app.services.vnda import _extrair_data_entrega, _extrair_periodo
            de = _extrair_data_entrega(order)
            info['data_entrega_extraida'] = de.isoformat() if de else None
            info['periodo_extraido'] = _extrair_periodo(order)
            info['hoje'] = date.today().isoformat()
            info['entrega_e_hoje'] = (de == date.today()) if de else False
        else:
            info['body'] = resp.text[:500]
    except http_requests.RequestException as e:
        info['erro_conexao'] = str(e)
    except Exception as e:
        info['erro_geral'] = str(e)

    # 2) Tentar variantes de include
    for inc in ('shipping_address', 'address', 'shipping', 'client'):
        try:
            r = http_requests.get(
                f'https://api.vnda.com.br/api/v2/orders/{code}',
                headers=headers, params={'include': inc}, timeout=10,
            )
            if r.status_code == 200:
                try:
                    o = r.json()
                    info[f'include_{inc}_chaves'] = sorted(o.keys())
                    info[f'include_{inc}_shipping'] = o.get('shipping_address')
                    info[f'include_{inc}_client'] = o.get('client')
                except ValueError:
                    pass
        except http_requests.RequestException:
            pass

    # 3) Tentar endpoints relacionados
    for path in (f'/orders/{code}/shipping_address', f'/orders/{code}/address', f'/orders/{code}/shipments', f'/orders/{code}/packages'):
        try:
            r = http_requests.get(
                f'https://api.vnda.com.br/api/v2{path}',
                headers=headers, timeout=10,
            )
            info[f'endpoint_{path}'] = {
                'status': r.status_code,
                'body': r.text[:4000] if r.status_code != 404 else None,
            }
        except http_requests.RequestException:
            pass

    # 4) Procurar 'ana' (case insensitive) em todo o JSON do pedido
    import json as _json
    try:
        full_text = _json.dumps(order, ensure_ascii=False) if 'order' in locals() else ''
        info['contains_ana_in_order'] = 'ana' in full_text.lower()
        # Caminhos onde a string 'ana' aparece (heuristic)
        if 'ana' in full_text.lower():
            paths = []
            def _walk(obj, prefix):
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        _walk(v, f'{prefix}.{k}')
                elif isinstance(obj, list):
                    for i, v in enumerate(obj):
                        _walk(v, f'{prefix}[{i}]')
                elif isinstance(obj, str) and 'ana' in obj.lower():
                    paths.append(f'{prefix} = {obj[:80]}')
            _walk(order, 'order')
            info['ana_paths'] = paths[:30]
    except Exception as e:
        info['walk_erro'] = str(e)

    # 5) Endpoints adicionais (label/print/customizations/notes/extra)
    for path in (f'/orders/{code}/customizations', f'/orders/{code}/notes', f'/orders/{code}/label', f'/orders/{code}/print', f'/orders/{code}/print_layout', f'/orders/{code}/extra', f'/orders/{code}/items'):
        try:
            r = http_requests.get(
                f'https://api.vnda.com.br/api/v2{path}',
                headers=headers, timeout=10,
            )
            info[f'endpoint_{path}'] = {
                'status': r.status_code,
                'body': r.text[:3000] if r.status_code != 404 else None,
            }
        except http_requests.RequestException:
            pass

    # 6) Tentativas de buscar customizations por item_id
    if 'order' in locals():
        item_ids = [str(i.get('id')) for i in (order.get('items') or []) if i.get('id')]
        cart_id = order.get('cart_id')
        for item_id in item_ids:
            for path in (f'/orders/{code}/items/{item_id}/customizations',
                         f'/orders/{code}/items/{item_id}',
                         f'/items/{item_id}/customizations',
                         f'/items/{item_id}',
                         f'/customizations/{item_id}',
                         f'/cart_items/{item_id}/customizations'):
                try:
                    r = http_requests.get(
                        f'https://api.vnda.com.br/api/v2{path}',
                        headers=headers, timeout=8,
                    )
                    if r.status_code == 200:
                        info[f'endpoint_{path}'] = {
                            'status': r.status_code,
                            'body': r.text[:3000],
                        }
                except http_requests.RequestException:
                    pass
        if cart_id:
            for path in (f'/carts/{cart_id}', f'/carts/{cart_id}/items', f'/carts/{cart_id}/customizations'):
                try:
                    r = http_requests.get(
                        f'https://api.vnda.com.br/api/v2{path}',
                        headers=headers, timeout=8,
                    )
                    if r.status_code == 200:
                        info[f'endpoint_{path}'] = {
                            'status': r.status_code,
                            'body': r.text[:3000],
                        }
                except http_requests.RequestException:
                    pass

    # 7) include=customizations
    try:
        r = http_requests.get(
            f'https://api.vnda.com.br/api/v2/orders/{code}',
            headers=headers, params={'include': 'customizations'}, timeout=10,
        )
        if r.status_code == 200:
            try:
                o = r.json()
                info['include_customizations_chaves'] = sorted(o.keys())
                info['include_customizations_items_extra'] = [i.get('extra') for i in (o.get('items') or [])]
                info['include_customizations_customizations'] = o.get('customizations')
            except ValueError:
                pass
    except http_requests.RequestException:
        pass

    return jsonify(info)


@entregas_bp.route('/api/debug')
@login_required
@entrega_access_required
def api_debug():
    """Diagnostico da conexao com a API Vnda."""
    token = current_app.config.get('VNDA_API_TOKEN', '')
    host = current_app.config.get('VNDA_SHOP_HOST', 'www.padariaartesanalonline.com.br')

    if token.lower().startswith('bearer '):
        token = token[7:]

    info = {
        'token_configurado': bool(token),
        'token_inicio': token[:8] + '...' if len(token) > 8 else '(vazio)',
        'host': host,
        'base_url': 'https://api.vnda.com.br/api/v2',
    }

    if not token:
        info['erro'] = 'VNDA_API_TOKEN nao configurado'
        return jsonify(info)

    headers = {
        'Authorization': f'Bearer {token}',
        'X-Shop-Host': host,
        'Accept': 'application/json',
        'User-Agent': 'OPaoPadaria/1.0',
    }

    try:
        resp = http_requests.get(
            'https://api.vnda.com.br/api/v2/orders',
            headers=headers,
            params={'per_page': 2},
            timeout=15,
        )
        info['status_code'] = resp.status_code
        info['response_headers'] = dict(resp.headers)

        try:
            body = resp.json()
            if isinstance(body, list):
                info['tipo_resposta'] = 'lista'
                info['quantidade'] = len(body)
                if body:
                    primeiro = body[0]
                    info['campos_pedido'] = list(primeiro.keys())
                    info['exemplo_code'] = primeiro.get('code', '')
                    info['exemplo_status'] = primeiro.get('status', '')
                    info['exemplo_expected_delivery'] = primeiro.get('expected_delivery_date', '')
                    info['exemplo_extra'] = primeiro.get('extra', {})
                    info['exemplo_client_id'] = primeiro.get('client_id', '')
                    info['exemplo_items_count'] = len(primeiro.get('items') or [])

                    detail_resp = http_requests.get(
                        'https://api.vnda.com.br/api/v2/orders/' + str(primeiro.get('code', '')),
                        headers=headers, timeout=15,
                    )
                    if detail_resp.status_code == 200:
                        try:
                            detail = detail_resp.json()
                            info['detalhe_campos'] = list(detail.keys())
                            info['detalhe_shipping_address'] = detail.get('shipping_address')
                            info['detalhe_client_name'] = detail.get('client_name', '')
                        except ValueError:
                            info['detalhe_erro'] = 'resposta nao-json'
            elif isinstance(body, dict):
                info['tipo_resposta'] = 'dict'
                info['chaves'] = list(body.keys())
                if 'results' in body:
                    info['quantidade'] = len(body['results'])
                    if body['results']:
                        primeiro = body['results'][0]
                        info['campos_pedido'] = list(primeiro.keys())
                        info['exemplo_code'] = primeiro.get('code', '')
                        info['exemplo_extra'] = primeiro.get('extra', {})
                elif 'error' in body or 'message' in body:
                    info['erro_api'] = body
            else:
                info['tipo_resposta'] = str(type(body))
                info['body_raw'] = str(body)[:500]
        except ValueError:
            info['resposta_texto'] = resp.text[:500]

    except http_requests.RequestException as e:
        info['erro_conexao'] = str(e)

    return jsonify(info)
