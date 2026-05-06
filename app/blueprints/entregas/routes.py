from datetime import date, datetime

from flask import render_template, request, jsonify, abort, current_app
from flask_login import login_required, current_user

import requests as http_requests

from app.blueprints.entregas import entregas_bp
from app.decorators import entrega_access_required
from app.extensions import db
from app.models import CartinhaEntrega, OverrideEntrega, Driver, AtribuicaoEntrega, Produto, MateriaPrima
from app.services import vnda, rotas as rotas_svc


@entregas_bp.route('/')
@login_required
@entrega_access_required
def index():
    resp = current_app.make_response(
        render_template('entregas/index.html', hoje=date.today().isoformat())
    )
    # Evita cache do HTML (Safari teima muito com inline JS)
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp


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

        # Carrega driver atribuido (se houver)
        atribuicoes = {}
        if codes:
            for a in AtribuicaoEntrega.query.filter(AtribuicaoEntrega.pedido_code.in_(codes)).all():
                if a.driver_id:
                    drv = Driver.query.get(a.driver_id)
                    if drv:
                        atribuicoes[a.pedido_code] = {
                            'id': drv.id, 'nome': drv.nome, 'cor': drv.cor,
                        }
        for p in pedidos:
            drv = atribuicoes.get(p['code'])
            if drv:
                p['driver'] = drv

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


# ── Drivers de entrega ──

@entregas_bp.route('/api/drivers', methods=['GET'])
@login_required
@entrega_access_required
def listar_drivers():
    incluir_inativos = request.args.get('inativos') == '1'
    q = Driver.query
    if not incluir_inativos:
        q = q.filter_by(ativo=True)
    drivers = q.order_by(Driver.nome).all()
    return jsonify(drivers=[
        {'id': d.id, 'nome': d.nome, 'cor': d.cor, 'telefone': d.telefone, 'ativo': d.ativo}
        for d in drivers
    ])


@entregas_bp.route('/api/drivers', methods=['POST'])
@login_required
@entrega_access_required
def criar_driver():
    data = request.get_json(silent=True) or {}
    nome = (data.get('nome') or '').strip()
    if not nome:
        return jsonify(ok=False, erro='nome obrigatorio'), 400
    if Driver.query.filter_by(nome=nome).first():
        return jsonify(ok=False, erro='ja existe driver com esse nome'), 400
    d = Driver(
        nome=nome,
        cor=(data.get('cor') or '').strip() or None,
        telefone=(data.get('telefone') or '').strip() or None,
        ativo=True,
    )
    db.session.add(d)
    db.session.commit()
    return jsonify(ok=True, id=d.id, nome=d.nome)


@entregas_bp.route('/api/drivers/<int:did>', methods=['POST'])
@login_required
@entrega_access_required
def atualizar_driver(did):
    d = Driver.query.get_or_404(did)
    data = request.get_json(silent=True) or {}
    if 'nome' in data:
        nome = (data['nome'] or '').strip()
        if not nome:
            return jsonify(ok=False, erro='nome obrigatorio'), 400
        # Se mudou e ja existe outro com esse nome, rejeita
        outro = Driver.query.filter(Driver.nome == nome, Driver.id != did).first()
        if outro:
            return jsonify(ok=False, erro='ja existe driver com esse nome'), 400
        d.nome = nome
    if 'cor' in data:
        d.cor = (data['cor'] or '').strip() or None
    if 'telefone' in data:
        d.telefone = (data['telefone'] or '').strip() or None
    if 'ativo' in data:
        d.ativo = bool(data['ativo'])
    db.session.commit()
    return jsonify(ok=True)


@entregas_bp.route('/api/drivers/<int:did>', methods=['DELETE'])
@login_required
@entrega_access_required
def remover_driver(did):
    """Exclui o driver de vez se nao tem historico; senao apenas desativa.
    Forca exclusao com ?force=1 (cuidado: apaga atribuicoes)."""
    d = Driver.query.get_or_404(did)
    force = request.args.get('force') == '1'

    n_atrib = AtribuicaoEntrega.query.filter_by(driver_id=did).count()

    if n_atrib == 0:
        # Sem historico — exclui de vez
        nome = d.nome
        db.session.delete(d)
        db.session.commit()
        return jsonify(ok=True, acao='excluido', nome=nome)

    if force:
        # Apaga as atribuicoes tambem (cuidado!)
        AtribuicaoEntrega.query.filter_by(driver_id=did).delete()
        nome = d.nome
        db.session.delete(d)
        db.session.commit()
        return jsonify(ok=True, acao='excluido_com_historico', nome=nome, atribuicoes_apagadas=n_atrib)

    # Tem historico mas sem force — apenas desativa
    d.ativo = False
    db.session.commit()
    return jsonify(ok=True, acao='desativado', nome=d.nome, atribuicoes=n_atrib)


# ── Atribuicao pedido <-> driver ──

@entregas_bp.route('/api/atribuicao/<code>', methods=['POST'])
@login_required
@entrega_access_required
def atribuir_pedido(code):
    """Atribui um pedido a um driver (ou troca de driver). data_entrega opcional."""
    data = request.get_json(silent=True) or {}
    driver_id = data.get('driver_id')
    if driver_id is not None:
        try:
            driver_id = int(driver_id)
        except (TypeError, ValueError):
            return jsonify(ok=False, erro='driver_id invalido'), 400
        if not Driver.query.get(driver_id):
            return jsonify(ok=False, erro='driver nao encontrado'), 404

    a = AtribuicaoEntrega.query.filter_by(pedido_code=code).first()
    if not a:
        a = AtribuicaoEntrega(pedido_code=code)
        db.session.add(a)
    a.driver_id = driver_id
    if 'data_entrega' in data and data['data_entrega']:
        try:
            a.data_entrega = datetime.strptime(data['data_entrega'], '%Y-%m-%d').date()
        except ValueError:
            pass
    if 'ordem' in data:
        try:
            a.ordem = int(data['ordem'])
        except (TypeError, ValueError):
            pass
    a.atualizado_por = current_user.id
    db.session.commit()
    return jsonify(ok=True)


@entregas_bp.route('/api/atribuicao/<code>', methods=['DELETE'])
@login_required
@entrega_access_required
def remover_atribuicao(code):
    """Remove atribuicao do pedido (volta a ficar sem driver)."""
    a = AtribuicaoEntrega.query.filter_by(pedido_code=code).first()
    if a:
        db.session.delete(a)
        db.session.commit()
    return jsonify(ok=True)


@entregas_bp.route('/tiles/<int:z>/<int:x>/<int:y>.png')
@login_required
@entrega_access_required
def tile_proxy(z, x, y):
    """Proxy de tiles do OpenStreetMap. Necessario porque o ambiente
    do usuario bloqueia CDNs externas (unpkg, jsdelivr, openstreetmap.org,
    cartocdn). Servimos os tiles via nosso dominio.

    Cache no proxy + browser pra reduzir trafego."""
    import requests as r
    from flask import Response, abort
    if z < 0 or z > 19 or x < 0 or y < 0:
        abort(400)
    try:
        resp = r.get(
            f'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
            headers={'User-Agent': 'OPaoPadariaERP/1.0 (rotas-de-entrega)'},
            timeout=10,
        )
        if resp.status_code != 200:
            abort(resp.status_code)
        out = Response(resp.content, mimetype='image/png')
        # Cacheia 1 dia no browser. OSM tiles raramente mudam.
        out.headers['Cache-Control'] = 'public, max-age=86400'
        return out
    except r.RequestException:
        abort(502)


@entregas_bp.route('/api/google/limpar-falhas', methods=['POST'])
@login_required
@entrega_access_required
def api_google_limpar_falhas():
    """Apaga registros do GeocodeCache com fonte=google_fail ou lat=NULL.
    Forca re-geocoding na proxima chamada de rotas."""
    from app.models import GeocodeCache
    n = GeocodeCache.query.filter(
        db.or_(
            GeocodeCache.fonte == 'google_fail',
            GeocodeCache.lat.is_(None),
        )
    ).delete(synchronize_session=False)
    db.session.commit()
    return jsonify(ok=True, removidas=n)


@entregas_bp.route('/api/debug/google')
@login_required
@entrega_access_required
def api_debug_google():
    """Diagnostico da integracao Google Maps."""
    import requests as r
    key = (current_app.config.get('GOOGLE_MAPS_API_KEY') or '').strip()
    info = {
        'key_configurada': bool(key),
        'key_inicio': key[:10] + '...' if len(key) > 10 else '(vazio)',
        'origem_endereco': (current_app.config.get('ROTA_ORIGEM_ENDERECO') or ''),
    }
    if not key:
        info['erro'] = 'GOOGLE_MAPS_API_KEY nao configurada'
        return jsonify(info)

    # Testa geocoding com endereco conhecido
    try:
        resp = r.get(
            'https://maps.googleapis.com/maps/api/geocode/json',
            params={
                'address': 'Avenida Paulista 1000, Sao Paulo, SP',
                'key': key,
                'components': 'country:BR',
            },
            timeout=10,
        )
        info['geocode_status_http'] = resp.status_code
        try:
            data = resp.json()
            info['geocode_status_api'] = data.get('status')
            info['geocode_error_message'] = data.get('error_message', '')
            if data.get('status') == 'OK' and data.get('results'):
                loc = data['results'][0]['geometry']['location']
                info['geocode_resultado'] = f"{loc['lat']},{loc['lng']}"
                info['geocode_endereco_formatado'] = data['results'][0].get('formatted_address')
        except ValueError:
            info['geocode_body'] = resp.text[:500]
    except Exception as e:
        info['geocode_erro_conexao'] = str(e)

    # Conta cache
    try:
        from app.models import GeocodeCache
        info['cache_total'] = GeocodeCache.query.count()
        info['cache_google_ok'] = GeocodeCache.query.filter(
            GeocodeCache.fonte == 'google',
            GeocodeCache.lat.isnot(None),
        ).count()
        info['cache_google_fail'] = GeocodeCache.query.filter_by(fonte='google_fail').count()
        info['cache_outras_fontes'] = GeocodeCache.query.filter(
            ~GeocodeCache.fonte.in_(['google', 'google_fail']),
            GeocodeCache.fonte.isnot(None),
        ).count()
    except Exception as e:
        info['cache_erro'] = str(e)

    return jsonify(info)


@entregas_bp.route('/api/atribuicao/reset', methods=['POST'])
@login_required
@entrega_access_required
def resetar_atribuicoes_dia():
    """Apaga atribuicoes de TODOS os pedidos do dia escolhido.
    Pega os codes do VNDA (respeitando overrides de data) e remove suas
    AtribuicaoEntrega. Usado pra "redistribuir do zero" sem afetar outras datas."""
    data = request.get_json(silent=True) or {}
    data_str = (data.get('data') or '').strip()
    if not data_str:
        return jsonify(ok=False, erro='data obrigatoria'), 400
    try:
        target = datetime.strptime(data_str, '%Y-%m-%d').date()
    except ValueError:
        return jsonify(ok=False, erro='data invalida'), 400

    overrides = _carregar_overrides_data()
    resultado = vnda.buscar_pedidos_do_dia(target, overrides=overrides)
    if 'erro' in resultado:
        return jsonify(ok=False, erro=resultado['erro']), 500

    codes = [p['code'] for p in resultado.get('pedidos', []) if p.get('code')]
    n = 0
    if codes:
        n = AtribuicaoEntrega.query.filter(
            AtribuicaoEntrega.pedido_code.in_(codes)
        ).delete(synchronize_session=False)
        db.session.commit()
    return jsonify(ok=True, removidas=n, total_pedidos=len(codes))


@entregas_bp.route('/api/atribuicao/lote', methods=['POST'])
@login_required
@entrega_access_required
def atribuir_lote():
    """Atribui em lote: [{code, driver_id, ordem, data_entrega}...]."""
    data = request.get_json(silent=True) or {}
    items = data.get('items') or []
    if not isinstance(items, list):
        return jsonify(ok=False, erro='items deve ser lista'), 400

    drivers_validos = {d.id for d in Driver.query.all()}
    salvos = 0
    for item in items:
        code = (item.get('code') or '').strip()
        if not code:
            continue
        driver_id = item.get('driver_id')
        if driver_id is not None:
            try:
                driver_id = int(driver_id)
            except (TypeError, ValueError):
                continue
            if driver_id not in drivers_validos:
                continue
        a = AtribuicaoEntrega.query.filter_by(pedido_code=code).first()
        if not a:
            a = AtribuicaoEntrega(pedido_code=code)
            db.session.add(a)
        a.driver_id = driver_id
        if item.get('data_entrega'):
            try:
                a.data_entrega = datetime.strptime(item['data_entrega'], '%Y-%m-%d').date()
            except ValueError:
                pass
        if 'ordem' in item:
            try:
                a.ordem = int(item['ordem'])
            except (TypeError, ValueError):
                pass
        a.atualizado_por = current_user.id
        salvos += 1
    db.session.commit()
    return jsonify(ok=True, salvos=salvos)


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


@entregas_bp.route('/api/produtos')
@login_required
@entrega_access_required
def api_produtos():
    """Agrega itens dos pedidos do dia. Retorna duas listas:
    - 'vendidos': como veio do VNDA (cestas como produto unico)
    - 'producao': cestas explodidas em componentes (usa Produto+ProdutoItem do banco)"""
    data_str = request.args.get('data', date.today().isoformat())
    try:
        target = datetime.strptime(data_str, '%Y-%m-%d').date()
    except ValueError:
        target = date.today()

    janelas = [j for j in request.args.getlist('janela') if j]

    overrides = _carregar_overrides_data()
    resultado = vnda.buscar_pedidos_do_dia(target, overrides=overrides)
    if 'erro' in resultado:
        resp = jsonify(vendidos=[], producao=[], erro=resultado['erro'])
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
        return resp

    pedidos = resultado.get('pedidos', [])
    if janelas:
        pedidos = [p for p in pedidos if (p.get('periodo') or '') in janelas]

    # Carrega catalogo de Produtos cadastrados (pra expandir cestas)
    produtos_db = {}  # nome_lower -> Produto
    for prod in Produto.query.filter_by(ativo=True).all():
        if prod.nome:
            produtos_db[prod.nome.strip().lower()] = prod

    # Cache de MateriaPrima pra pegar unidade dos componentes 'mp'
    mp_db = {}  # nome_lower -> MateriaPrima
    for mp in MateriaPrima.query.all():
        if mp.nome:
            mp_db[mp.nome.strip().lower()] = mp

    # Vendidos (como veio do VNDA) — chave por SKU+nome
    vendidos = {}

    def _agg_vendido(sku, nome, qty, preco):
        chave = (sku or nome or '').strip().lower()
        if not chave:
            return
        a = vendidos.get(chave)
        if not a:
            a = {
                'sku': sku, 'nome': nome, 'quantidade': 0,
                'preco_unitario': preco, 'valor_total': 0.0,
                'componente_de': [],
            }
            vendidos[chave] = a
        a['quantidade'] += qty
        a['valor_total'] += qty * preco

    # Producao — chave SO por nome (ignora SKU). Croissant de Family Box +
    # Croissant comprado avulso somam na mesma linha.
    producao = {}

    def _agg_producao(nome, qty, unidade='un', componente_de=None):
        chave = (nome or '').strip().lower()
        if not chave:
            return
        a = producao.get(chave)
        if not a:
            a = {
                'sku': '', 'nome': nome, 'unidade': unidade, 'quantidade': 0,
                'preco_unitario': 0.0, 'valor_total': 0.0,
                'componente_de': set(),
            }
            producao[chave] = a
        # Se ja tem com unidade diferente, mantem a primeira (deveria ser sempre a mesma)
        a['quantidade'] += qty
        if componente_de:
            a['componente_de'].add(componente_de)

    for p in pedidos:
        for item in (p.get('itens') or []):
            sku = (item.get('sku') or '').strip()
            nome = (item.get('nome') or '').strip()
            if not nome and not sku:
                continue
            try:
                qty = int(item.get('quantidade') or 0)
            except (TypeError, ValueError):
                qty = 0
            try:
                preco = float(item.get('preco_unitario') or 0)
            except (TypeError, ValueError):
                preco = 0.0

            # Vendidos: sempre adiciona como veio do VNDA
            _agg_vendido(sku, nome, qty, preco)

            # Producao: se for cesta cadastrada, soma componentes
            prod_cadastrado = produtos_db.get(nome.lower())
            if prod_cadastrado and prod_cadastrado.itens:
                for comp in prod_cadastrado.itens:
                    cnome = (comp.item_nome or '').strip()
                    if not cnome:
                        continue
                    cqty_unitario = comp.quantidade or 1
                    total_qty = qty * cqty_unitario
                    if total_qty <= 0:
                        continue
                    total_qty = int(total_qty) if total_qty == int(total_qty) else total_qty
                    # Detecta unidade: 'mp' usa unidade da MateriaPrima; 'receita' = unidade
                    if (comp.tipo or '').lower() == 'mp':
                        mp = mp_db.get(cnome.lower())
                        unidade = mp.unidade if mp else 'g'
                    else:
                        unidade = 'un'
                    _agg_producao(cnome, total_qty, unidade=unidade, componente_de=nome)
            else:
                # Item simples — vai pra producao direto, sem origem
                _agg_producao(nome, qty, unidade='un')

    def _serializa(d):
        out = []
        for v in d.values():
            cd = v.get('componente_de')
            v['componente_de'] = sorted(cd) if isinstance(cd, set) else (cd or [])
            out.append(v)
        return sorted(out, key=lambda x: (-x['quantidade'], x['nome']))

    vendidos_lista = _serializa(vendidos)
    producao_lista = _serializa(producao)
    periodos = sorted({p.get('periodo') or '' for p in resultado.get('pedidos', []) if p.get('periodo')})

    # Soma producao agrupada por unidade (300 g + 12 un nao sao somaveis)
    totais_por_unidade = {}
    for p in producao_lista:
        u = p.get('unidade') or 'un'
        totais_por_unidade[u] = totais_por_unidade.get(u, 0) + p['quantidade']

    resp = jsonify(
        data=data_str,
        janelas=janelas,
        periodos_disponiveis=periodos,
        vendidos=vendidos_lista,
        producao=producao_lista,
        total_pedidos=len(pedidos),
        total_itens_vendidos=sum(p['quantidade'] for p in vendidos_lista),
        total_skus_vendidos=len(vendidos_lista),
        total_skus_producao=len(producao_lista),
        totais_producao_por_unidade=totais_por_unidade,
        valor_total=round(sum(p['valor_total'] for p in vendidos_lista), 2),
    )
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    return resp


@entregas_bp.route('/api/atribuidos')
@login_required
@entrega_access_required
def api_atribuidos():
    """Lista pedidos do dia agrupados por driver atribuido + secao 'sem driver'."""
    data_str = request.args.get('data', date.today().isoformat())
    try:
        target = datetime.strptime(data_str, '%Y-%m-%d').date()
    except ValueError:
        target = date.today()

    overrides = _carregar_overrides_data()
    resultado = vnda.buscar_pedidos_do_dia(target, overrides=overrides)
    if 'erro' in resultado:
        resp = jsonify(drivers=[], sem_driver=[], erro=resultado['erro'])
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
        return resp

    pedidos = resultado.get('pedidos', [])
    codes = [p['code'] for p in pedidos if p.get('code')]

    atribuicoes_por_code = {}
    if codes:
        for a in AtribuicaoEntrega.query.filter(AtribuicaoEntrega.pedido_code.in_(codes)).all():
            atribuicoes_por_code[a.pedido_code] = {
                'driver_id': a.driver_id,
                'ordem': a.ordem or 0,
            }

    drivers_db = Driver.query.order_by(Driver.nome).all()
    drivers_por_id = {d.id: d for d in drivers_db}

    paradas_por_driver = {}
    sem_driver = []

    for p in pedidos:
        atrib = atribuicoes_por_code.get(p['code'])
        did = atrib['driver_id'] if atrib else None
        if did and did in drivers_por_id:
            ordem = atrib.get('ordem', 0)
            paradas_por_driver.setdefault(did, []).append((ordem, p))
        else:
            sem_driver.append(p)

    drivers_resp = []
    for d in drivers_db:
        if d.id not in paradas_por_driver:
            continue
        paradas_list = sorted(paradas_por_driver[d.id], key=lambda x: (x[0], x[1].get('periodo') or ''))
        drivers_resp.append({
            'id': d.id,
            'nome': d.nome,
            'cor': d.cor,
            'telefone': d.telefone,
            'ativo': d.ativo,
            'paradas': [p for _, p in paradas_list],
            'qtd': len(paradas_list),
        })

    return jsonify(
        data=data_str,
        drivers=drivers_resp,
        sem_driver=sem_driver,
        total_pedidos=len(pedidos),
        total_atribuidos=sum(d['qtd'] for d in drivers_resp),
        drivers_disponiveis=[
            {'id': d.id, 'nome': d.nome, 'cor': d.cor}
            for d in drivers_db if d.ativo
        ],
        origem_endereco=rotas_svc.origem_endereco(current_app),
    )


@entregas_bp.route('/api/rotas')
@login_required
@entrega_access_required
def api_rotas():
    """Distribui pedidos entre drivers nominais (cadastrados em /api/drivers).
    Pedidos com atribuicao salva (AtribuicaoEntrega) preservam o driver."""
    data_str = request.args.get('data', date.today().isoformat())
    try:
        target = datetime.strptime(data_str, '%Y-%m-%d').date()
    except ValueError:
        target = date.today()

    janelas = [j for j in request.args.getlist('janela') if j]

    # Drivers ativos cadastrados; opcionalmente filtra pelos selecionados (?drivers=1,2,3)
    sel = (request.args.get('drivers') or '').strip()
    q = Driver.query.filter_by(ativo=True)
    if sel:
        try:
            ids = [int(x) for x in sel.split(',') if x.strip()]
            q = q.filter(Driver.id.in_(ids))
        except ValueError:
            pass
    drivers_db = q.order_by(Driver.nome).all()
    drivers_struct = [{'id': d.id, 'nome': d.nome, 'cor': d.cor, 'telefone': d.telefone}
                      for d in drivers_db]

    overrides = _carregar_overrides_data()
    resultado = vnda.buscar_pedidos_do_dia(target, overrides=overrides)
    if 'erro' in resultado:
        resp = jsonify(rotas=[], erro=resultado['erro'])
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
        return resp

    pedidos = resultado.get('pedidos', [])

    if janelas:
        pedidos = [p for p in pedidos if (p.get('periodo') or '') in janelas]

    # Carrega atribuicoes existentes pros pedidos do dia
    codes = [p['code'] for p in pedidos if p.get('code')]
    atribuicoes = {}
    if codes:
        for a in AtribuicaoEntrega.query.filter(AtribuicaoEntrega.pedido_code.in_(codes)).all():
            atribuicoes[a.pedido_code] = {'driver_id': a.driver_id, 'ordem': a.ordem or 0}

    if not drivers_struct:
        resp = jsonify(
            data=data_str,
            janelas=janelas,
            periodos_disponiveis=sorted({p.get('periodo') or '' for p in resultado.get('pedidos', []) if p.get('periodo')}),
            drivers_disponiveis=[],
            rotas=[],
            sem_atribuir=[
                {'code': p['code'], 'destinatario': p.get('destinatario', ''),
                 'endereco': p.get('endereco', ''), 'periodo': p.get('periodo', '')}
                for p in pedidos
            ],
            origem_endereco=rotas_svc.origem_endereco(current_app),
            sem_cep=[],
        )
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
        return resp

    geradas = rotas_svc.gerar_rotas(pedidos, drivers_struct, atribuicoes=atribuicoes)
    periodos = sorted({p.get('periodo') or '' for p in resultado.get('pedidos', []) if p.get('periodo')})

    origem_coords = rotas_svc.origem_latlng(current_app)
    origem_payload = None
    if origem_coords:
        origem_payload = {'lat': origem_coords[0], 'lng': origem_coords[1]}

    resp = jsonify(
        data=data_str,
        janelas=janelas,
        periodos_disponiveis=periodos,
        drivers_disponiveis=drivers_struct,
        rotas=geradas['rotas'],
        sem_cep=[
            {'code': p['code'], 'destinatario': p.get('destinatario', ''),
             'endereco': p.get('endereco', ''), 'periodo': p.get('periodo', '')}
            for p in geradas['sem_cep']
        ],
        origem_endereco=rotas_svc.origem_endereco(current_app),
        origem=origem_payload,
    )
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    return resp


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
