from datetime import datetime

import requests as http_requests
from flask import abort, current_app, jsonify, render_template, request
from flask_login import current_user, login_required

from app.blueprints.entregas import entregas_bp
from app.decorators import entrega_access_required
from app.extensions import db
from app.models import (
    AtribuicaoEntrega,
    CartinhaEntrega,
    Driver,
    EntregaFoto,
    LalamoveEntrega,
    LoteSaida,
    MateriaPrima,
    OverrideEntrega,
    PainelPedidoStatus,
    PedidoLocal,
    PedidoLocalItem,
    Produto,
)
from app.services import dropbox_storage, vnda
from app.services import rotas as rotas_svc
from app.utils import agora
from app.utils import hoje as hoje_brt


@entregas_bp.route('/')
@login_required
@entrega_access_required
def index():
    resp = current_app.make_response(
        render_template('entregas/index.html', hoje=hoje_brt().isoformat())
    )
    # Evita cache do HTML (Safari teima muito com inline JS)
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp


def _aplicar_cartinhas(pedidos):
    """Resolve a cartinha de cada pedido: a manual (CartinhaEntrega, editada
    por humano) tem prioridade sobre a do VNDA. Muta os dicts in-place.

    Centralizado aqui porque a tela de entregas E o Painel do Dia precisam da
    mesma regra de prioridade — duplicar geraria divergencia silenciosa."""
    codes = [p['code'] for p in pedidos if p.get('code')]
    manuais = {}
    if codes:
        for c in CartinhaEntrega.query.filter(
                CartinhaEntrega.pedido_code.in_(codes)).all():
            manuais[c.pedido_code] = c.texto or ''
    for p in pedidos:
        manual = manuais.get(p.get('code'), '')
        auto = p.get('cartinha_vnda', '')
        p['cartinha'] = manual or auto
        p['cartinha_origem'] = 'manual' if manual else ('vnda' if auto else None)
    return pedidos


# ── Painel do Dia (tela simples pra equipe de preparo) ────────────────────
#
# Objetivo: a equipe para de olhar pedidos no VNDA e passa a olhar aqui.
# Requisitos (turma com baixa familiaridade com tela): UI grande e obvia,
# alerta SONORO quando cai pedido novo do dia, e o som so para quando alguem
# CLICA no pedido (marca como visto). O "visto" eh server-side: se uma pessoa
# clica, silencia em todos os aparelhos da equipe.

def _painel_pedidos_do_dia(target):
    """Busca pedidos do dia no VNDA (com overrides + locais) e resolve a
    cartinha. Retorna (pedidos, erro_str|None). Reusa o mesmo caminho da tela
    de entregas pra nao divergir."""
    try:
        overrides_data = {code: o['data']
                          for code, o in _carregar_overrides_full().items()}
        resultado = _injetar_pedidos_locais(
            target, vnda.buscar_pedidos_do_dia(target, overrides=overrides_data))
    except Exception as e:  # noqa: BLE001
        current_app.logger.exception('painel: erro carregando VNDA')
        return [], f'{type(e).__name__}: {str(e)[:200]}'
    if 'erro' in resultado:
        return [], resultado['erro']
    pedidos = resultado.get('pedidos', [])
    _aplicar_cartinhas(pedidos)
    return pedidos, None


@entregas_bp.route('/painel')
@login_required
def painel():
    resp = current_app.make_response(
        render_template('entregas/painel.html', hoje=hoje_brt().isoformat()))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    return resp


@entregas_bp.route('/api/painel')
@login_required
def api_painel():
    data_str = request.args.get('data', hoje_brt().isoformat())
    try:
        target = datetime.strptime(data_str, '%Y-%m-%d').date()
    except ValueError:
        target = hoje_brt()

    pedidos, erro = _painel_pedidos_do_dia(target)
    if erro:
        return jsonify(pedidos=[], data=data_str, erro=erro)

    codes = [p['code'] for p in pedidos if p.get('code')]
    status_por_code = {}
    if codes:
        for s in PainelPedidoStatus.query.filter(
                PainelPedidoStatus.pedido_code.in_(codes)).all():
            status_por_code[s.pedido_code] = s.status or 'visto'
    lala_por_code = _lalamove_por_code(codes)

    out = []
    for p in pedidos:
        code = p.get('code')
        status = status_por_code.get(code, 'novo')
        out.append({
            'code': code,
            'destinatario': p.get('destinatario') or p.get('comprador') or 'Sem nome',
            'endereco': p.get('endereco') or '',
            'periodo': p.get('periodo') or '',
            'expresso': bool(p.get('expresso')),
            'telefone': p.get('telefone') or '',
            'cartinha': p.get('cartinha') or '',
            'itens': [{'nome': it.get('nome') or '', 'qtd': it.get('quantidade') or 1}
                      for it in (p.get('itens') or [])],
            'status': status,            # novo|visto|pronto|entregue
            'novo': status == 'novo',    # mantido pra o alerta sonoro
            'lalamove': lala_por_code.get(code),
        })

    resp = jsonify(pedidos=out, data=data_str, total=len(out),
                   novos=sum(1 for p in out if p['novo']))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    return resp


@entregas_bp.route('/api/painel/status/<code>', methods=['POST'])
@login_required
def api_painel_status(code):
    """Muda o status de preparo de um pedido (visto/pronto/entregue).

    'visto' eh o que o clique automatico manda (silencia o alerta). 'pronto' e
    'entregue' vem dos botoes. Idempotente: upsert por pedido_code."""
    code = (code or '').strip()
    novo_status = (request.args.get('status')
                   or (request.get_json(silent=True) or {}).get('status')
                   or 'visto').strip().lower()
    if not code:
        return jsonify(ok=False, erro='code vazio'), 400
    if novo_status not in PainelPedidoStatus.STATUS_VALIDOS:
        return jsonify(ok=False, erro='status invalido'), 400

    uid = current_user.id if current_user.is_authenticated else None
    s = PainelPedidoStatus.query.filter_by(pedido_code=code).first()
    if s:
        # Nao regride de pronto/entregue pra visto por um clique acidental de
        # abertura — so o 'visto' automatico nao deve rebaixar status maior.
        ordem = {'visto': 1, 'pronto': 2, 'entregue': 3}
        if not (novo_status == 'visto' and ordem.get(s.status, 0) >= 2):
            s.status = novo_status
            s.atualizado_por = uid
        db.session.commit()
    else:
        s = PainelPedidoStatus(pedido_code=code, status=novo_status,
                               data_ref=hoje_brt(), atualizado_por=uid)
        db.session.add(s)
        try:
            db.session.commit()
        except Exception:  # noqa: BLE001
            db.session.rollback()  # corrida entre 2 aparelhos: ja existe, ok
    return jsonify(ok=True, status=novo_status)


# ── Lalamove (entregador sob demanda a partir do painel) ──────────────────

def _lalamove_json(e):
    """Resumo de uma LalamoveEntrega pro front do painel."""
    from app.services import lalamove as lala_svc
    return {
        'id': e.id,
        'status': e.status,
        'rotulo': ('Cotação feita — confirme a chamada'
                   if e.status == 'cotacao' else lala_svc.rotulo_status(e.status)),
        'valor': str(e.valor) if e.valor is not None else None,
        'moeda': e.moeda or 'BRL',
        'veiculo': ('moto' if e.service_type == 'MOTORCYCLE'
                    else 'carro' if e.service_type == 'CAR' else e.service_type),
        'share_link': e.share_link,
        'motorista': e.motorista_nome,
        'motorista_fone': e.motorista_telefone,
        'pode_cancelar': e.status in ('ASSIGNING_DRIVER', 'ON_GOING'),
        'encerrada': e.status in ('COMPLETED', 'CANCELED', 'REJECTED', 'EXPIRED'),
    }


def _lalamove_por_code(codes):
    """{code: resumo} da corrida mais recente JÁ CHAMADA (com order_id) de
    cada pedido. Cotações não confirmadas ficam de fora do card."""
    if not codes:
        return {}
    out = {}
    rows = (LalamoveEntrega.query
            .filter(LalamoveEntrega.pedido_code.in_(codes),
                    LalamoveEntrega.order_id.isnot(None))
            .order_by(LalamoveEntrega.criado_em.asc()).all())
    for e in rows:           # asc + sobrescrita = vence a mais recente
        out[e.pedido_code] = _lalamove_json(e)
    return out


@entregas_bp.route('/api/painel/lalamove/cotar', methods=['POST'])
@login_required
def api_lalamove_cotar():
    """Cota uma corrida pro endereço do pedido. JSON: {code, endereco,
    destinatario, telefone, veiculo: moto|carro}. Guarda a cotação
    (status='cotacao') e devolve id+preço pro atendente confirmar."""
    from app.services import lalamove as lala_svc
    dados = request.get_json(silent=True) or {}
    code = (dados.get('code') or '').strip()
    endereco = (dados.get('endereco') or '').strip()
    veiculo = (dados.get('veiculo') or 'moto').strip().lower()
    if not code or not endereco:
        return jsonify(ok=False, erro='pedido sem código ou sem endereço'), 400
    r = lala_svc.cotar(endereco, veiculo)
    if not r.get('ok'):
        return jsonify(ok=False, erro=r.get('erro')), 502
    e = LalamoveEntrega(
        pedido_code=code, data_ref=hoje_brt(),
        quotation_id=r['quotation_id'],
        sender_stop_id=r['sender_stop_id'],
        recipient_stop_id=r['recipient_stop_id'],
        status='cotacao', service_type=r['service_type'],
        valor=r.get('valor'), moeda=r.get('moeda'),
        distancia_m=r.get('distancia_m'),
        endereco_destino=endereco,
        destinatario=(dados.get('destinatario') or '')[:200] or None,
        telefone_destino=(dados.get('telefone') or '')[:40] or None,
        criado_por_id=current_user.id)
    db.session.add(e)
    db.session.commit()
    km = (f'{r["distancia_m"] / 1000:.1f} km' if r.get('distancia_m') else '')
    return jsonify(ok=True, entrega_id=e.id, valor=r.get('valor'),
                   moeda=r.get('moeda') or 'BRL', distancia=km,
                   veiculo=veiculo)


@entregas_bp.route('/api/painel/lalamove/chamar', methods=['POST'])
@login_required
def api_lalamove_chamar():
    """Confirma a corrida de uma cotação feita. JSON: {entrega_id}."""
    from app.services import lalamove as lala_svc
    dados = request.get_json(silent=True) or {}
    e = db.session.get(LalamoveEntrega, dados.get('entrega_id'))
    if not e or e.status != 'cotacao':
        return jsonify(ok=False, erro='cotação não encontrada ou já usada'), 400
    r = lala_svc.criar_ordem(
        e.quotation_id, e.sender_stop_id, e.recipient_stop_id,
        e.destinatario, e.telefone_destino,
        observacao=f'Pedido {e.pedido_code} — O Pão Padaria Artesanal')
    if not r.get('ok'):
        return jsonify(ok=False, erro=r.get('erro')), 502
    e.order_id = r['order_id']
    e.status = r.get('status') or 'ASSIGNING_DRIVER'
    e.share_link = r.get('share_link')
    if r.get('valor') is not None:
        e.valor = r['valor']
    e.atualizado_em = agora()
    db.session.commit()
    current_app.logger.info('lalamove chamada: pedido=%s order=%s por uid=%s',
                            e.pedido_code, e.order_id, current_user.id)
    return jsonify(ok=True, lalamove=_lalamove_json(e))


@entregas_bp.route('/api/painel/lalamove/cancelar', methods=['POST'])
@login_required
def api_lalamove_cancelar():
    """Cancela uma corrida já chamada. JSON: {entrega_id}."""
    from app.services import lalamove as lala_svc
    dados = request.get_json(silent=True) or {}
    e = db.session.get(LalamoveEntrega, dados.get('entrega_id'))
    if not e or not e.order_id:
        return jsonify(ok=False, erro='corrida não encontrada'), 400
    r = lala_svc.cancelar(e.order_id)
    if not r.get('ok'):
        return jsonify(ok=False, erro=r.get('erro')), 502
    e.status = 'CANCELED'
    e.atualizado_em = agora()
    db.session.commit()
    current_app.logger.info('lalamove cancelada: pedido=%s order=%s por uid=%s',
                            e.pedido_code, e.order_id, current_user.id)
    return jsonify(ok=True, lalamove=_lalamove_json(e))


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
    import traceback
    data_str = request.args.get('data', hoje_brt().isoformat())
    try:
        target = datetime.strptime(data_str, '%Y-%m-%d').date()
    except ValueError:
        target = hoje_brt()

    try:
        overrides_full = _carregar_overrides_full()
        overrides_data = {code: o['data'] for code, o in overrides_full.items()}
        resultado = _injetar_pedidos_locais(target, vnda.buscar_pedidos_do_dia(target, overrides=overrides_data))
    except Exception as e:
        current_app.logger.exception('api_pedidos: erro carregando VNDA/overrides')
        return jsonify(pedidos=[], data=data_str,
                       erro=f'{type(e).__name__}: {str(e)[:300]}')

    if 'erro' in resultado:
        resp = jsonify(pedidos=[], data=data_str, erro=resultado['erro'])
    else:
        try:
            pedidos = resultado.get('pedidos', [])
            total_janela = resultado.get('total_janela', 0)

            codes = [p['code'] for p in pedidos if p['code']]
            _aplicar_cartinhas(pedidos)

            # Info adicional de override de data
            for p in pedidos:
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
        except Exception as e:
            current_app.logger.exception('api_pedidos: erro processando pedidos')
            tb_short = traceback.format_exc().splitlines()[-3:]
            return jsonify(pedidos=[], data=data_str,
                           erro=f'{type(e).__name__}: {str(e)[:200]} | {" | ".join(tb_short)}')

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
        year, month = hoje_brt().year, hoje_brt().month

    overrides = _carregar_overrides_data()
    dias = vnda.contar_pedidos_por_dia(year, month, overrides=overrides)
    resp = jsonify(dias=dias)
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    return resp


# ── Drivers de entrega ──

@entregas_bp.route('/dropbox/setup', methods=['GET', 'POST'])
@login_required
@entrega_access_required
def dropbox_setup():
    """Wizard one-shot pra obter o refresh_token do Dropbox.

    Passos:
    1. Admin cria o app no painel da Dropbox e copia App key + App secret.
    2. Cola aqui na primeira tela -> recebe URL pra autorizar.
    3. Autoriza no Dropbox -> recebe um codigo curto.
    4. Cola o codigo aqui -> backend troca por refresh_token via API.
    5. Pagina mostra o refresh_token. Admin copia pro Railway env junto
       com app_key/app_secret. Token nao expira.
    """
    msg = None
    erro = None
    refresh_token = None

    # Pre-preencher dos envs se ja existirem (UI mostra status atual)
    cfg_app_key = (current_app.config.get('DROPBOX_APP_KEY') or '').strip()
    cfg_app_secret = (current_app.config.get('DROPBOX_APP_SECRET') or '').strip()
    cfg_refresh = (current_app.config.get('DROPBOX_REFRESH_TOKEN') or '').strip()
    legacy = (current_app.config.get('DROPBOX_ACCESS_TOKEN') or '').strip()

    app_key = cfg_app_key
    app_secret = cfg_app_secret

    if request.method == 'POST':
        app_key = (request.form.get('app_key') or '').strip()
        app_secret = (request.form.get('app_secret') or '').strip()
        code = (request.form.get('code') or '').strip()

        if app_key and app_secret and code:
            r = http_requests.post(
                'https://api.dropbox.com/oauth2/token',
                data={
                    'grant_type': 'authorization_code',
                    'code': code,
                },
                auth=(app_key, app_secret),
                timeout=15,
            )
            if r.status_code == 200:
                body = r.json()
                refresh_token = body.get('refresh_token')
                if refresh_token:
                    msg = 'Refresh token gerado com sucesso. Copie os 3 valores abaixo pro Railway.'
                else:
                    erro = ('Resposta sem refresh_token. Verifique se voce usou '
                            '"token_access_type=offline" na URL de autorizacao.')
            else:
                erro = f'Dropbox retornou {r.status_code}: {r.text[:200]}'
        elif app_key and app_secret:
            msg = 'App key e secret salvos. Agora autorize abaixo e cole o codigo.'

    return render_template(
        'entregas/dropbox_setup.html',
        msg=msg,
        erro=erro,
        refresh_token=refresh_token,
        app_key=app_key,
        app_secret=app_secret,
        cfg_app_key_set=bool(cfg_app_key),
        cfg_app_secret_set=bool(cfg_app_secret),
        cfg_refresh_set=bool(cfg_refresh),
        legacy_set=bool(legacy),
    )


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
        {
            'id': d.id, 'nome': d.nome, 'cor': d.cor, 'telefone': d.telefone, 'ativo': d.ativo,
            'token': d.token, 'pin': d.pin, 'capacidade': d.capacidade or 999,
        }
        for d in drivers
    ])


@entregas_bp.route('/api/drivers', methods=['POST'])
@login_required
@entrega_access_required
def criar_driver():
    import secrets
    data = request.get_json(silent=True) or {}
    nome = (data.get('nome') or '').strip()
    if not nome:
        return jsonify(ok=False, erro='nome obrigatorio'), 400
    if Driver.query.filter_by(nome=nome).first():
        return jsonify(ok=False, erro='ja existe driver com esse nome'), 400
    try:
        cap = int(data.get('capacidade') or 999)
    except (TypeError, ValueError):
        cap = 999
    d = Driver(
        nome=nome,
        cor=(data.get('cor') or '').strip() or None,
        telefone=(data.get('telefone') or '').strip() or None,
        ativo=True,
        token=secrets.token_urlsafe(16),
        capacidade=max(1, cap),
    )
    db.session.add(d)
    db.session.commit()
    return jsonify(ok=True, id=d.id, nome=d.nome, token=d.token)


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
    if 'pin' in data:
        pin = (data['pin'] or '').strip()
        # PIN vazio remove (acesso livre); 4-6 digitos so numero
        if pin and not (pin.isdigit() and 4 <= len(pin) <= 6):
            return jsonify(ok=False, erro='PIN deve ter 4-6 digitos'), 400
        d.pin = pin or None
    if 'capacidade' in data:
        try:
            d.capacidade = max(1, int(data['capacidade']))
        except (TypeError, ValueError):
            pass
    if data.get('regenerar_token'):
        import secrets
        d.token = secrets.token_urlsafe(16)
    if not d.token:
        import secrets
        d.token = secrets.token_urlsafe(16)
    db.session.commit()
    return jsonify(ok=True, token=d.token)


def _limpar_referencias_driver(did, apagar_atribuicoes):
    """Remove/zera TODAS as FKs que apontam pro driver, pra o delete nao quebrar
    no Postgres (FK pendente -> IntegrityError -> 500 que o front engolia, e o
    driver nunca era excluido — bug visto em prod 2026-06-09).

    - `DriverMagicToken` (driver_id NOT NULL, link efemero diario): DELETE.
    - `PedidoLoja.driver_id` (handshake de coleta) / `PedidoItemFoto.
      criado_por_driver_id` (foto do motorista) — ambos nullable: zera (preserva
      o registro, perde so a atribuicao ao motorista).
    - `AtribuicaoEntrega`: DELETE so com force (e a 'historia' de entregas)."""
    from app.models import DriverMagicToken, PedidoItemFoto, PedidoLoja
    DriverMagicToken.query.filter_by(driver_id=did).delete(synchronize_session=False)
    PedidoLoja.query.filter_by(driver_id=did).update(
        {'driver_id': None}, synchronize_session=False)
    PedidoItemFoto.query.filter_by(criado_por_driver_id=did).update(
        {'criado_por_driver_id': None}, synchronize_session=False)
    if apagar_atribuicoes:
        AtribuicaoEntrega.query.filter_by(driver_id=did).delete(synchronize_session=False)


@entregas_bp.route('/api/drivers/<int:did>', methods=['DELETE'])
@login_required
@entrega_access_required
def remover_driver(did):
    """Exclui o driver de vez se nao tem historico de entregas; senao apenas
    desativa. Forca exclusao com ?force=1 (cuidado: apaga atribuicoes)."""
    d = Driver.query.get_or_404(did)
    force = request.args.get('force') == '1'

    n_atrib = AtribuicaoEntrega.query.filter_by(driver_id=did).count()

    try:
        if n_atrib == 0:
            # Sem historico de entregas — exclui de vez (limpando magic tokens
            # e zerando refs nullable de pedido/foto, que senao travam a FK).
            nome = d.nome
            _limpar_referencias_driver(did, apagar_atribuicoes=False)
            db.session.delete(d)
            db.session.commit()
            return jsonify(ok=True, acao='excluido', nome=nome)

        if force:
            nome = d.nome
            _limpar_referencias_driver(did, apagar_atribuicoes=True)
            db.session.delete(d)
            db.session.commit()
            return jsonify(ok=True, acao='excluido_com_historico', nome=nome,
                           atribuicoes_apagadas=n_atrib)

        # Tem historico mas sem force — apenas desativa
        d.ativo = False
        db.session.commit()
        return jsonify(ok=True, acao='desativado', nome=d.nome, atribuicoes=n_atrib)
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        current_app.logger.exception('remover_driver %s falhou', did)
        return jsonify(ok=False, erro=f'Falha ao excluir: {exc}'), 500


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
    if a.lote_id:
        _recompute_lote_status(a.lote_id)
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
    from flask import Response
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
    resultado = _injetar_pedidos_locais(target, vnda.buscar_pedidos_do_dia(target, overrides=overrides))
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


def _limpar_status_atribuicao(atrib):
    atrib.status = 'pendente'
    atrib.entregue_em = None
    atrib.nota = None
    atrib.motivo_falha = None
    atrib.geo_lat = None
    atrib.geo_lng = None
    atrib.proof_hash = None


@entregas_bp.route('/api/entrega/<code>/reset', methods=['POST'])
@login_required
@entrega_access_required
def resetar_entrega(code):
    """Volta atribuicao do pedido pra 'pendente' e apaga fotos (DB + Dropbox)."""
    if not current_user.is_admin():
        return jsonify(ok=False, erro='somente admin'), 403

    atrib = AtribuicaoEntrega.query.filter_by(pedido_code=code).first()
    if not atrib:
        return jsonify(ok=False, erro='atribuicao nao encontrada'), 404

    fotos = EntregaFoto.query.filter_by(atribuicao_id=atrib.id).all()
    apagadas_dropbox = 0
    for f in fotos:
        if f.storage_path and dropbox_storage.deletar(f.storage_path):
            apagadas_dropbox += 1
        db.session.delete(f)

    _limpar_status_atribuicao(atrib)
    db.session.commit()

    return jsonify(ok=True, fotos_removidas=len(fotos),
                   fotos_dropbox_apagadas=apagadas_dropbox,
                   data=atrib.data_entrega.isoformat() if atrib.data_entrega else None)


@entregas_bp.route('/api/entrega/<code>/migrar', methods=['POST'])
@login_required
@entrega_access_required
def migrar_entrega(code):
    """Move comprovante (status + fotos + geo + proof_hash) do pedido <code> pra outro.

    Body: {destino: "OUTRO_CODE"}
    Origem fica resetada como pendente.
    Destino nao pode ja ter comprovante (precisa estar pendente/sem fotos).
    """
    if not current_user.is_admin():
        return jsonify(ok=False, erro='somente admin'), 403

    body = request.get_json(silent=True) or {}
    destino_code = (body.get('destino') or '').strip()
    if not destino_code:
        return jsonify(ok=False, erro='destino obrigatorio'), 400
    if destino_code == code:
        return jsonify(ok=False, erro='origem e destino iguais'), 400

    origem = AtribuicaoEntrega.query.filter_by(pedido_code=code).first()
    if not origem:
        return jsonify(ok=False, erro='origem nao encontrada'), 404

    destino = AtribuicaoEntrega.query.filter_by(pedido_code=destino_code).first()
    if destino is None:
        destino = AtribuicaoEntrega(
            pedido_code=destino_code,
            driver_id=origem.driver_id,
            data_entrega=origem.data_entrega,
            ordem=0,
        )
        db.session.add(destino)
        db.session.flush()
    else:
        tem_fotos = EntregaFoto.query.filter_by(atribuicao_id=destino.id).first() is not None
        if destino.proof_hash or tem_fotos or destino.status == 'entregue':
            return jsonify(ok=False, erro='destino ja tem comprovante; resete antes'), 409

    # Move campos
    destino.status = origem.status
    destino.entregue_em = origem.entregue_em
    destino.nota = origem.nota
    destino.motivo_falha = origem.motivo_falha
    destino.geo_lat = origem.geo_lat
    destino.geo_lng = origem.geo_lng
    destino.proof_hash = origem.proof_hash

    # Move fotos (sem reupload — apenas troca atribuicao_id)
    fotos = EntregaFoto.query.filter_by(atribuicao_id=origem.id).all()
    for f in fotos:
        f.atribuicao_id = destino.id

    _limpar_status_atribuicao(origem)
    db.session.commit()

    return jsonify(ok=True, fotos_movidas=len(fotos), destino=destino_code,
                   data=destino.data_entrega.isoformat() if destino.data_entrega else None)


# ── Pedidos manuais (fora do VNDA) ──

def _injetar_pedidos_locais(target_date, resultado):
    """Adiciona pedidos manuais (PedidoLocal) na lista de pedidos do dia."""
    if 'erro' in resultado:
        return resultado
    locais = PedidoLocal.query.filter_by(data_entrega=target_date).all()
    pedidos = resultado.setdefault('pedidos', [])
    for p in locais:
        pedidos.append(_serializar_pedido_local(p))
    return resultado


def _gerar_code_local():
    import secrets
    while True:
        code = 'LOC-' + secrets.token_hex(4).upper()
        if not PedidoLocal.query.filter_by(code=code).first():
            return code


def _serializar_pedido_local(p):
    return {
        'id': p.id,
        'pedido_local': True,
        'code': p.code,
        'destinatario': p.destinatario,
        'telefone': p.telefone,
        'endereco': p.endereco,
        'data_entrega': p.data_entrega.isoformat() if p.data_entrega else None,
        'data_entrega_fmt': p.data_entrega.strftime('%d/%m/%Y') if p.data_entrega else '',
        'periodo': p.periodo or '',
        'cartinha_vnda': p.cartinha or '',
        'observacao': p.observacao,
        'status_vnda': 'local',
        'data_override': False,
        'tem_customizacao': False,
        'comprador': '',
        'itens': [
            {'nome': i.nome, 'quantidade': i.quantidade, 'preco_unitario': i.preco_unitario, 'sku': '', 'subtotal': (i.quantidade or 0) * (i.preco_unitario or 0)}
            for i in p.itens
        ],
        'total': p.total,
    }


@entregas_bp.route('/api/pedido-local', methods=['POST'])
@login_required
@entrega_access_required
def criar_pedido_local():
    data = request.get_json(silent=True) or {}
    obrigatorios = ['destinatario', 'telefone', 'endereco', 'data_entrega', 'itens']
    for campo in obrigatorios:
        if not data.get(campo):
            return jsonify(ok=False, erro=f'campo obrigatorio: {campo}'), 400
    try:
        d = datetime.strptime(data['data_entrega'], '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return jsonify(ok=False, erro='data_entrega invalida'), 400

    itens = data['itens']
    if not isinstance(itens, list) or len(itens) == 0:
        return jsonify(ok=False, erro='precisa de pelo menos 1 item'), 400

    pid = data.get('id')
    if pid:
        p = PedidoLocal.query.get(pid)
        if not p:
            return jsonify(ok=False, erro='pedido nao encontrado'), 404
        # Limpa itens existentes
        for it in list(p.itens):
            db.session.delete(it)
    else:
        p = PedidoLocal(code=_gerar_code_local(), criado_por=current_user.id)
        db.session.add(p)

    p.destinatario = data['destinatario'].strip()
    p.telefone = data['telefone'].strip()
    p.endereco = data['endereco'].strip()
    p.data_entrega = d
    p.periodo = (data.get('periodo') or '').strip() or None
    p.cartinha = (data.get('cartinha') or '').strip() or None
    p.observacao = (data.get('observacao') or '').strip() or None
    db.session.flush()

    for it in itens:
        nome = (it.get('nome') or '').strip()
        if not nome:
            continue
        try:
            qtd = int(it.get('quantidade') or 1)
            preco = float(it.get('preco_unitario') or 0)
        except (ValueError, TypeError):
            qtd, preco = 1, 0.0
        db.session.add(PedidoLocalItem(pedido_local_id=p.id, nome=nome, quantidade=qtd, preco_unitario=preco))

    db.session.commit()
    return jsonify(ok=True, pedido=_serializar_pedido_local(p))


@entregas_bp.route('/api/pedido-local/<int:pid>', methods=['GET'])
@login_required
@entrega_access_required
def get_pedido_local(pid):
    p = PedidoLocal.query.get(pid)
    if not p:
        return jsonify(ok=False, erro='nao encontrado'), 404
    return jsonify(ok=True, pedido=_serializar_pedido_local(p))


@entregas_bp.route('/api/pedido-local/<int:pid>', methods=['DELETE'])
@login_required
@entrega_access_required
def deletar_pedido_local(pid):
    p = PedidoLocal.query.get(pid)
    if not p:
        return jsonify(ok=False, erro='nao encontrado'), 404
    # Apaga atribuicao do pedido se houver (mesmo code)
    AtribuicaoEntrega.query.filter_by(pedido_code=p.code).delete()
    db.session.delete(p)
    db.session.commit()
    return jsonify(ok=True)


@entregas_bp.route('/api/atribuicao/lote', methods=['POST'])
@login_required
@entrega_access_required
def atribuir_lote():
    """Atribui em lote.

    Body:
      items: [{code, driver_id, ordem, data_entrega}...]
      criar_lote (opcional): {janelas: [...], nome?: str, data_entrega: 'YYYY-MM-DD'}
        → cria um LoteSaida novo e marca todos os items com seu lote_id.
      lote_id (opcional): int — usa lote existente em vez de criar.
    """
    import json as _json
    data = request.get_json(silent=True) or {}
    items = data.get('items') or []
    if not isinstance(items, list):
        return jsonify(ok=False, erro='items deve ser lista'), 400

    # 1. Resolve lote_id (criar novo OU usar existente OU nenhum)
    lote_id = None
    criar = data.get('criar_lote')
    if criar and isinstance(criar, dict):
        # Data do lote: do payload, ou do primeiro item, ou hoje
        data_str = criar.get('data_entrega')
        if not data_str:
            for it in items:
                if it.get('data_entrega'):
                    data_str = it['data_entrega']
                    break
        try:
            data_lote = datetime.strptime(data_str, '%Y-%m-%d').date() if data_str else hoje_brt()
        except ValueError:
            data_lote = hoje_brt()
        janelas = criar.get('janelas') or []
        if not isinstance(janelas, list):
            janelas = []
        nome = (criar.get('nome') or '').strip()
        if not nome:
            ag = agora()
            jan_str = ' + '.join(j or '(sem janela)' for j in janelas) if janelas else 'todas as janelas'
            nome = data_lote.strftime('%d/%m') + ' ' + ag.strftime('%H:%M') + ' · ' + jan_str
        novo = LoteSaida(
            nome=nome[:120],
            data_entrega=data_lote,
            janelas_json=_json.dumps(janelas),
            status='aberto',
            criado_por=current_user.id,
        )
        db.session.add(novo)
        db.session.flush()
        lote_id = novo.id
    elif data.get('lote_id') is not None:
        try:
            lote_id = int(data['lote_id'])
        except (TypeError, ValueError):
            return jsonify(ok=False, erro='lote_id invalido'), 400
        if not LoteSaida.query.get(lote_id):
            return jsonify(ok=False, erro='lote nao encontrado'), 404

    drivers_validos = {d.id for d in Driver.query.all()}
    salvos = 0
    lotes_afetados = set()
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
        if lote_id is not None:
            a.lote_id = lote_id
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
        # Coleta lotes afetados conforme processa items (mais robusto que
        # inspecionar db.session.dirty, que pode incluir objetos inesperados)
        if a.lote_id:
            lotes_afetados.add(a.lote_id)
        salvos += 1
    if lote_id:
        lotes_afetados.add(lote_id)
    try:
        db.session.flush()
    except Exception:
        db.session.rollback()
        raise
    for lid in lotes_afetados:
        _recompute_lote_status(lid)
    db.session.commit()
    return jsonify(ok=True, salvos=salvos, lote_id=lote_id)


def _recompute_lote_status(lote_id):
    """Infere status do lote olhando atribuicoes filhas. Defensivo: nunca
    propaga excecao — log + segue. Status do lote e' inferencia de UI, nao
    pode quebrar salvamentos.

    aberto      = nenhuma atribuicao saiu (status != 'pendente') ainda
    em_rota     = >=1 saiu/entregue/falhou, mas falta entregar (>0 pendentes
                  com driver atribuido)
    concluido   = tudo entregue ou falhou (sem pendentes com driver)
    """
    try:
        with db.session.no_autoflush:
            lote = LoteSaida.query.get(lote_id)
            if not lote:
                return
            atribs = AtribuicaoEntrega.query.filter_by(lote_id=lote_id).all()
            if not atribs:
                return
            finais = {'entregue', 'nao_entregue'}
            n_finalizadas = sum(1 for a in atribs if (a.status or 'pendente') in finais)
            n_pendentes_com_driver = sum(
                1 for a in atribs
                if (a.status or 'pendente') == 'pendente' and a.driver_id is not None
            )
            if n_finalizadas == 0:
                novo = 'aberto'
            elif n_pendentes_com_driver == 0 and n_finalizadas > 0:
                novo = 'concluido'
            else:
                novo = 'em_rota'
            if lote.status != novo:
                lote.status = novo
    except Exception as exc:  # noqa: BLE001
        current_app.logger.warning('falha ao recalcular status do lote %s: %s', lote_id, exc)


@entregas_bp.route('/api/lotes', methods=['GET'])
@login_required
@entrega_access_required
def listar_lotes():
    """Lista lotes de uma data (?data=YYYY-MM-DD).
    Retorna metadados + contadores por status."""
    import json as _json
    data_str = request.args.get('data', hoje_brt().isoformat())
    try:
        target = datetime.strptime(data_str, '%Y-%m-%d').date()
    except ValueError:
        target = hoje_brt()
    lotes = LoteSaida.query.filter_by(data_entrega=target).order_by(LoteSaida.criado_em).all()

    # Conta atribuicoes por lote em 1 query
    from sqlalchemy import func
    contagens = dict(
        db.session.query(AtribuicaoEntrega.lote_id, func.count(AtribuicaoEntrega.id))
        .filter(AtribuicaoEntrega.lote_id.in_([l.id for l in lotes] or [0]))
        .group_by(AtribuicaoEntrega.lote_id).all()
    )

    out = []
    for l in lotes:
        try:
            janelas = _json.loads(l.janelas_json) if l.janelas_json else []
        except (ValueError, TypeError):
            janelas = []
        out.append({
            'id': l.id,
            'nome': l.nome,
            'data_entrega': l.data_entrega.isoformat() if l.data_entrega else None,
            'criado_em': l.criado_em.isoformat() if l.criado_em else None,
            'janelas': janelas,
            'status': l.status or 'aberto',
            'qtd_pedidos': contagens.get(l.id, 0),
        })
    return jsonify(lotes=out)


@entregas_bp.route('/api/lotes/<int:lote_id>', methods=['DELETE'])
@login_required
@entrega_access_required
def deletar_lote(lote_id):
    """Exclui um lote. Por padrão, desvincula as atribuições filhas
    (lote_id ← NULL) e elas voltam pro pool 'Sem lote'. Com
    ?apagar_atribuicoes=1, apaga as atribuições junto (use só pra
    limpar testes; perde dados de driver/ordem/status)."""
    lote = LoteSaida.query.get_or_404(lote_id)
    apagar = request.args.get('apagar_atribuicoes') == '1'
    # Apagar atribuicoes destroi histórico (status, fotos, comprovantes) —
    # so admin pode fazer.
    if apagar and not current_user.is_admin():
        return jsonify(ok=False, erro='Apenas admin pode apagar as atribuições do lote'), 403
    try:
        afetadas = AtribuicaoEntrega.query.filter_by(lote_id=lote_id).all()
        if apagar:
            for a in afetadas:
                db.session.delete(a)
        else:
            # UPDATE em lote via SQL: simples e evita N round-trips e
            # problema de ordem de flush antes do DELETE do lote (FK
            # constraint reclama se a UPDATE nao tiver rodado primeiro).
            AtribuicaoEntrega.query.filter_by(lote_id=lote_id) \
                .update({AtribuicaoEntrega.lote_id: None}, synchronize_session=False)
        db.session.flush()
        db.session.delete(lote)
        db.session.commit()
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        current_app.logger.exception('falha ao deletar lote %s', lote_id)
        return jsonify(ok=False, erro=str(exc)), 500
    return jsonify(ok=True, atribuicoes_afetadas=len(afetadas), apagadas=apagar)


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
    c.atualizado_em = agora()
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
    data_str = request.args.get('data', hoje_brt().isoformat())
    try:
        target = datetime.strptime(data_str, '%Y-%m-%d').date()
    except ValueError:
        target = hoje_brt()

    janelas = [j for j in request.args.getlist('janela') if j]

    overrides = _carregar_overrides_data()
    resultado = _injetar_pedidos_locais(target, vnda.buscar_pedidos_do_dia(target, overrides=overrides))
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
    data_str = request.args.get('data', hoje_brt().isoformat())
    try:
        target = datetime.strptime(data_str, '%Y-%m-%d').date()
    except ValueError:
        target = hoje_brt()

    overrides = _carregar_overrides_data()
    resultado = _injetar_pedidos_locais(target, vnda.buscar_pedidos_do_dia(target, overrides=overrides))
    if 'erro' in resultado:
        resp = jsonify(drivers=[], sem_driver=[], erro=resultado['erro'])
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
        return resp

    pedidos = resultado.get('pedidos', [])
    _aplicar_cartinhas(pedidos)   # resolve p['cartinha'] (manual > VNDA) p/ a aba Operacao
    codes = [p['code'] for p in pedidos if p.get('code')]

    atribuicoes_por_code = {}
    if codes:
        for a in AtribuicaoEntrega.query.filter(AtribuicaoEntrega.pedido_code.in_(codes)).all():
            fotos = [{'id': f.id, 'url': f.url} for f in a.fotos.all()]
            atribuicoes_por_code[a.pedido_code] = {
                'atribuicao_id': a.id,
                'driver_id': a.driver_id,
                'lote_id': a.lote_id,
                'ordem': a.ordem or 0,
                'status': a.status or 'pendente',
                'entregue_em': a.entregue_em.isoformat() if a.entregue_em else None,
                'nota': a.nota,
                'motivo_falha': a.motivo_falha,
                'fotos': fotos,
                'proof_hash': a.proof_hash,
            }

    drivers_db = Driver.query.order_by(Driver.nome).all()
    drivers_por_id = {d.id: d for d in drivers_db}

    paradas_por_driver = {}
    sem_driver = []

    for p in pedidos:
        atrib = atribuicoes_por_code.get(p['code'])
        did = atrib['driver_id'] if atrib else None
        # Enriquece o pedido com status/fotos/etc do registro de atribuicao
        if atrib:
            p['status'] = atrib.get('status') or 'pendente'
            p['entregue_em'] = atrib.get('entregue_em')
            p['nota_driver'] = atrib.get('nota')
            p['motivo_falha'] = atrib.get('motivo_falha')
            p['fotos'] = atrib.get('fotos') or []
            p['proof_hash'] = atrib.get('proof_hash')
            p['lote_id'] = atrib.get('lote_id')
        if did and did in drivers_por_id:
            ordem = atrib.get('ordem', 0)
            paradas_por_driver.setdefault(did, []).append((ordem, p))
        else:
            sem_driver.append(p)

    drivers_resp = []
    for d in drivers_db:
        if d.id not in paradas_por_driver:
            continue
        # Ordena por JANELA (expresso primeiro, depois por horario) e, dentro
        # da janela, pela ordem salva da rota. Respeita o SLA de horario mesmo
        # antes de re-otimizar.
        paradas_list = sorted(
            paradas_por_driver[d.id],
            key=lambda x: (rotas_svc.janela_rank(x[1]), x[0]))
        drivers_resp.append({
            'id': d.id,
            'nome': d.nome,
            'cor': d.cor,
            'telefone': d.telefone,
            'ativo': d.ativo,
            'paradas': [p for _, p in paradas_list],
            'qtd': len(paradas_list),
        })

    # "Sem driver" tambem por janela: expresso no topo, depois por horario —
    # quem prepara/atribui ve primeiro o que tem SLA mais apertado.
    sem_driver.sort(key=rotas_svc.janela_rank)

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
    data_str = request.args.get('data', hoje_brt().isoformat())
    try:
        target = datetime.strptime(data_str, '%Y-%m-%d').date()
    except ValueError:
        target = hoje_brt()

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
    drivers_struct = [{'id': d.id, 'nome': d.nome, 'cor': d.cor, 'telefone': d.telefone,
                       'capacidade': d.capacidade or 999}
                      for d in drivers_db]

    overrides = _carregar_overrides_data()
    resultado = _injetar_pedidos_locais(target, vnda.buscar_pedidos_do_dia(target, overrides=overrides))
    if 'erro' in resultado:
        resp = jsonify(rotas=[], erro=resultado['erro'])
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
        return resp

    pedidos = resultado.get('pedidos', [])

    if janelas:
        pedidos = [p for p in pedidos if (p.get('periodo') or '') in janelas]

    # MODO 'roteirizar lote': escopa a um lote especifico. Pega so as sobras
    # desse lote (driver_id NULL), e a capacidade dos drivers e' calculada
    # contando os atribuidos do MESMO lote (nao do dia inteiro).
    lote_alvo_id = request.args.get('lote_id')
    if lote_alvo_id:
        try:
            lote_alvo_id = int(lote_alvo_id)
        except (TypeError, ValueError):
            lote_alvo_id = None

    # Carrega atribuicoes existentes. Cada Distribuir e' uma SAIDA — pedidos
    # que ja estao em outros lotes (com driver) ja saíram pra rua, entao nao
    # consomem capacidade do driver na rodada nova. Idem pedidos finalizados.
    # Eles sao removidos do pool antes de chamar gerar_rotas.
    #
    # O que SOBROU pra distribuir nesta rodada:
    # - Pedidos novos (nunca atribuidos)
    # - Sobras de lotes anteriores (lote_id != NULL mas driver_id == NULL)
    codes = [p['code'] for p in pedidos if p.get('code')]
    atribuicoes = {}
    lote_por_code = {}
    codes_excluir = set()
    if codes:
        for a in AtribuicaoEntrega.query.filter(AtribuicaoEntrega.pedido_code.in_(codes)).all():
            if a.lote_id:
                lote_por_code[a.pedido_code] = a.lote_id
            status = (a.status or 'pendente')
            ja_finalizado = status in ('entregue', 'nao_entregue')
            if lote_alvo_id:
                # MODO LOTE: escopa ao lote alvo. Atribuidos com driver no mesmo
                # lote viram pre_atribuidos (consomem capacidade). Sobras do
                # mesmo lote viram candidatos. Pedidos de outros lotes ou ja
                # finalizados ficam fora.
                if ja_finalizado or a.lote_id != lote_alvo_id:
                    codes_excluir.add(a.pedido_code)
                else:
                    atribuicoes[a.pedido_code] = {'driver_id': a.driver_id, 'ordem': a.ordem or 0}
            else:
                # MODO PADRAO: cria lote novo, capacidade fresca
                ja_em_outro_lote = bool(a.lote_id) and a.driver_id is not None
                if ja_finalizado or ja_em_outro_lote:
                    codes_excluir.add(a.pedido_code)
                else:
                    atribuicoes[a.pedido_code] = {'driver_id': a.driver_id, 'ordem': a.ordem or 0}
    if codes_excluir:
        pedidos = [p for p in pedidos if p.get('code') not in codes_excluir]
    # MODO LOTE: tambem exclui pedidos que nao tem registro de atribuicao
    # nesse lote (nunca atribuidos ao lote_alvo).
    if lote_alvo_id:
        codes_no_lote = {c for c, lid in lote_por_code.items() if lid == lote_alvo_id}
        pedidos = [p for p in pedidos if p.get('code') in codes_no_lote]

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
    # Enriquece cada parada com lote_id da atribuicao salva (pra filtro de lote no front)
    for r in geradas.get('rotas', []):
        for p in r.get('paradas', []):
            if p.get('code') in lote_por_code:
                p['lote_id'] = lote_por_code[p['code']]
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
        sem_atribuir=[
            {'code': p['code'], 'destinatario': p.get('destinatario', ''),
             'endereco': p.get('endereco', ''), 'periodo': p.get('periodo', '')}
            for p in geradas.get('sem_atribuir') or []
        ],
        total_pedidos=len(pedidos),
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
    o.atualizado_em = agora()
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
            info['hoje'] = hoje_brt().isoformat()
            info['entrega_e_hoje'] = (de == hoje_brt()) if de else False
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


@entregas_bp.route('/api/debug/schema')
@login_required
@entrega_access_required
def api_debug_schema():
    """Confere se as colunas novas (token, status, etc) existem nas tabelas."""
    from sqlalchemy import text
    info = {}
    try:
        with db.engine.connect() as c:
            for tbl in ('driver_entrega', 'atribuicao_entrega', 'entrega_foto'):
                r = c.execute(text(
                    "SELECT column_name FROM information_schema.columns WHERE table_name = :t"
                ), {'t': tbl})
                info[tbl] = sorted([row[0] for row in r])
    except Exception as e:
        info['erro'] = f'{type(e).__name__}: {str(e)[:300]}'
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


@entregas_bp.route('/drivers/magic')
@login_required
@entrega_access_required
def drivers_magic_status():
    """Status dos magic tokens diarios de cada motorista ativo.

    Mostra ultimo token, quando criado/expira, se foi enviado, se Z-API
    confirmou ok. Permite regerar+reenviar pra um motorista especifico
    (ex: ele perdeu o link / trocou de celular)."""
    from app.models import Driver, DriverMagicToken
    from app.services import driver_magic
    from app.services import zapi as zapi_svc

    drivers = Driver.query.filter_by(ativo=True).order_by(Driver.nome).all()
    rows = []
    for d in drivers:
        mt = (DriverMagicToken.query
              .filter_by(driver_id=d.id, revogado=False)
              .filter(DriverMagicToken.expira_em > agora())
              .order_by(DriverMagicToken.criado_em.desc())
              .first())
        ultimo = (DriverMagicToken.query
                  .filter_by(driver_id=d.id)
                  .order_by(DriverMagicToken.criado_em.desc())
                  .first())
        rows.append({
            'driver': d,
            'ativo': mt,
            'ultimo': ultimo,
        })
    zapi_ok = zapi_svc.disponivel()
    whitelist = sorted(driver_magic.telefones_drivers_ativos())
    return render_template('entregas/drivers_magic.html',
                            rows=rows, zapi_ok=zapi_ok, whitelist=whitelist)


@entregas_bp.route('/drivers/magic/<int:did>/regerar', methods=['POST'])
@login_required
@entrega_access_required
def drivers_magic_regerar(did):
    """Forca regerar+reenviar magic link pra um motorista (botao na UI)."""
    from flask import flash, redirect, url_for

    from app.models import Driver
    from app.services import driver_magic
    d = Driver.query.get_or_404(did)
    if not d.ativo:
        flash(f'{d.nome} esta inativo. Reative antes.', 'warning')
        return redirect(url_for('entregas.drivers_magic_status'))
    try:
        mt = driver_magic.gerar_token(d)
        # forcar=True: admin clicou manualmente, ignora guarda de pedido pendente
        ok, msg = driver_magic.enviar_whatsapp(mt, forcar=True)
        if ok:
            flash(f'Link enviado pra {d.nome}.', 'success')
        else:
            flash(f'Token gerado mas falha no envio: {msg}', 'warning')
    except Exception as exc:  # noqa: BLE001
        flash(f'Erro: {exc}', 'danger')
    return redirect(url_for('entregas.drivers_magic_status'))


@entregas_bp.route('/drivers/bulk', methods=['GET', 'POST'])
@login_required
@entrega_access_required
def drivers_bulk():
    """Edicao em massa de drivers: telefone, pin, capacidade, ativo.

    O regerar+enviar magic link continua em rota propria (botao por linha
    aciona /drivers/magic/<did>/regerar)."""
    from flask import flash, redirect, url_for

    if request.method == 'POST':
        atualizados = 0
        erros = []
        for d in Driver.query.all():
            tel = (request.form.get(f'telefone_{d.id}', '') or '').strip() or None
            pin = (request.form.get(f'pin_{d.id}', '') or '').strip() or None
            cap_raw = (request.form.get(f'capacidade_{d.id}', '') or '').strip()
            ativo = bool(request.form.get(f'ativo_{d.id}'))

            if pin is not None and not (pin.isdigit() and 4 <= len(pin) <= 6):
                erros.append(f'{d.nome}: PIN deve ter 4-6 digitos')
                continue
            try:
                cap = max(1, int(cap_raw)) if cap_raw else (d.capacidade or 999)
            except ValueError:
                erros.append(f'{d.nome}: capacidade invalida')
                continue

            antes = (d.telefone, d.pin, d.capacidade, d.ativo)
            d.telefone = tel
            d.pin = pin
            d.capacidade = cap
            d.ativo = ativo
            if (d.telefone, d.pin, d.capacidade, d.ativo) != antes:
                atualizados += 1

        if erros:
            db.session.rollback()
            for e in erros:
                flash(e, 'danger')
            return redirect(url_for('entregas.drivers_bulk'))

        if atualizados:
            db.session.commit()
            flash(f'{atualizados} motorista(s) atualizado(s).', 'success')
        else:
            flash('Nenhuma mudança.', 'info')
        return redirect(url_for('entregas.drivers_bulk'))

    drivers = Driver.query.order_by(Driver.ativo.desc(), Driver.nome).all()
    return render_template('entregas/drivers_bulk.html', drivers=drivers)
