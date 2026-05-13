"""Vendas PDV via integracao Seru. Sob demanda — sem cache local."""
from datetime import date, datetime, timedelta

from flask import render_template, request, jsonify, current_app, redirect, url_for, flash
from flask_login import login_required, current_user

from app.blueprints.pdv import pdv_bp
from app.decorators import admin_required
from app.extensions import db
from app.models import (Loja, Receita, Produto,
                        SeruProdutoMap, SeruLojaMap, SeruPedidoProcessado,
                        VndaProdutoMap, VndaPedidoProcessado, MovEstoqueLoja,
                        AppConfig)
from app.services import seru


@pdv_bp.route('/')
@login_required
@admin_required
def index():
    return render_template('pdv/index.html', hoje=date.today().isoformat())


@pdv_bp.route('/reprocessar', methods=['POST'])
@login_required
@admin_required
def reprocessar():
    """Reprocessa SOMENTE as vendas de HOJE (BRT) — vendas de ontem ou
    anteriores ficam intocadas (preferencia do usuario).

    Apaga SeruPedidoProcessado dos pedidos de hoje que NAO baixaram nada
    e re-roda processar_pedidos. Pedidos com baixados>0 nao sao apagados
    (zero risco de duplo desconto). Cancelados/estornados tambem nao."""
    from app.services import seru_sync
    from app.services.seru_cron import hoje_brt

    hoje = hoje_brt()
    # Inicio do dia BRT em UTC: 00:00 BRT = 03:00 UTC
    inicio_dia_utc = datetime.combine(hoje, datetime.min.time()) + timedelta(hours=3)

    # Apaga so os de HOJE "sem baixa"
    n_apagados = SeruPedidoProcessado.query.filter(
        SeruPedidoProcessado.processado_em >= inicio_dia_utc,
        SeruPedidoProcessado.n_itens_baixados == 0,
        SeruPedidoProcessado.estornado_em.is_(None),
        SeruPedidoProcessado.cancelado_em.is_(None),
    ).delete(synchronize_session=False)
    db.session.commit()

    try:
        stats = seru_sync.processar_pedidos(hoje, hoje, user=current_user)
    except Exception as e:
        current_app.logger.exception('reprocessar falhou')
        return jsonify(ok=False, erro=f'{type(e).__name__}: {str(e)[:300]}'), 502
    stats['n_apagados'] = n_apagados
    return jsonify(ok=True, **stats)


@pdv_bp.route('/historico-sync')
@login_required
@admin_required
def historico_sync():
    """Lista pedidos Seru ja processados pelo cron/sync manual."""
    from sqlalchemy import desc
    try:
        loja_id = int(request.args.get('loja') or 0) or None
    except ValueError:
        loja_id = None
    status_filtro = request.args.get('status', '')  # '', 'baixados', 'estornados', 'sem_loja', 'cancelados'

    q = SeruPedidoProcessado.query
    if loja_id:
        q = q.filter(SeruPedidoProcessado.loja_id == loja_id)
    if status_filtro == 'baixados':
        q = q.filter(SeruPedidoProcessado.n_itens_baixados > 0,
                     SeruPedidoProcessado.cancelado_em.is_(None))
    elif status_filtro == 'estornados':
        q = q.filter(SeruPedidoProcessado.estornado_em.isnot(None))
    elif status_filtro == 'sem_loja':
        q = q.filter(SeruPedidoProcessado.loja_id.is_(None))
    elif status_filtro == 'cancelados':
        q = q.filter(SeruPedidoProcessado.cancelado_em.isnot(None))

    pedidos = q.order_by(desc(SeruPedidoProcessado.processado_em)).limit(200).all()

    # Agregado de movs do EstoqueLoja por pedido (pra mostrar o que efetivamente baixou)
    from app.models import MovEstoqueLoja
    refs = [f'Seru #{p.seru_pedido_id}' for p in pedidos]
    refs_like = [r + '%' for r in refs]
    movs_por_pedido = {}
    if pedidos:
        from sqlalchemy import or_, func as sqlfunc
        # Busca todas as movs que comecam com 'Seru #<id>' pros pedidos listados
        clauses = [MovEstoqueLoja.referencia.like(r + '%') for r in refs]
        all_movs = MovEstoqueLoja.query.filter(or_(*clauses)).all()
        for m in all_movs:
            # extrai o pedido_id do prefixo
            pref = (m.referencia or '').split(' ', 2)
            if len(pref) >= 2 and pref[0] == 'Seru' and pref[1].startswith('#'):
                pid = pref[1][1:]  # remove '#'
                movs_por_pedido.setdefault(pid, []).append(m)

    lojas = Loja.query.filter_by(ativa=True).order_by(Loja.nome).all()
    return render_template('pdv/historico_sync.html',
                           pedidos=pedidos, lojas=lojas,
                           sel_loja=loja_id, sel_status=status_filtro,
                           movs_por_pedido=movs_por_pedido)


@pdv_bp.route('/itens-vendidos')
@login_required
@admin_required
def itens_vendidos():
    """Tela de relatorio: itens vendidos por intervalo + loja Seru."""
    from app.services import seru_cron
    receitas = Receita.query.order_by(Receita.categoria, Receita.nome).all()
    produtos = Produto.query.filter_by(ativo=True).order_by(Produto.nome).all()
    cron_status = seru_cron.status()
    return render_template('pdv/itens_vendidos.html',
                           hoje=date.today().isoformat(),
                           receitas=receitas, produtos=produtos,
                           cron_status=cron_status)


@pdv_bp.route('/api/itens-vendidos')
@login_required
@admin_required
def api_itens_vendidos():
    from app.services import vendas_itens
    inicio_str = request.args.get('inicio') or date.today().isoformat()
    fim_str = request.args.get('fim') or inicio_str
    loja = (request.args.get('loja') or '').strip() or None
    try:
        inicio = datetime.strptime(inicio_str, '%Y-%m-%d').date()
        fim = datetime.strptime(fim_str, '%Y-%m-%d').date()
    except ValueError:
        return jsonify(ok=False, erro='datas invalidas (use YYYY-MM-DD)'), 400
    if (fim - inicio).days > 92:
        return jsonify(ok=False, erro='intervalo maximo de 92 dias'), 400

    hoje = date.today()
    dias_ate_hoje = max(0, (hoje - fim).days) if fim < hoje else 0
    dias_extra = min(dias_ate_hoje, 7)
    try:
        data = vendas_itens.agregar_itens(inicio, fim, loja_seru=loja,
                                          expandir_dias_frente=dias_extra)
    except Exception as e:
        current_app.logger.exception('itens-vendidos falhou')
        return jsonify(ok=False, erro=f'{type(e).__name__}: {str(e)[:300]}'), 502
    return jsonify(ok=True, **data)


@pdv_bp.route('/api/vendas')
@login_required
@admin_required
def api_vendas():
    try:
        return _api_vendas_impl()
    except Exception as e:
        current_app.logger.exception('api_vendas: erro inesperado')
        import traceback
        return jsonify(
            ok=False,
            erro=f'{type(e).__name__}: {str(e)[:300]}',
            traceback=traceback.format_exc().splitlines()[-5:],
        ), 500


def _api_vendas_impl():
    """Lista vendas Seru no intervalo. Default: hoje.

    ?inicio=YYYY-MM-DD&fim=YYYY-MM-DD
    """
    inicio_str = request.args.get('inicio') or date.today().isoformat()
    fim_str = request.args.get('fim') or inicio_str
    try:
        inicio = datetime.strptime(inicio_str, '%Y-%m-%d').date()
        fim = datetime.strptime(fim_str, '%Y-%m-%d').date()
    except ValueError:
        return jsonify(ok=False, erro='datas invalidas (use YYYY-MM-DD)'), 400

    if (fim - inicio).days > 92:
        return jsonify(ok=False, erro='intervalo maximo de 92 dias'), 400

    # Expandimos a janela de updatedAt ate N dias pra frente do fim, pra
    # capturar pedidos criados no intervalo mas atualizados depois.
    # Ainda filtramos por createdAt local. Cada dia adicional eh +1 chamada
    # Seru, entao limitamos pra evitar timeout do gunicorn (60s).
    MAX_DIAS_EXTRA = 7
    hoje = date.today()
    dias_ate_hoje = max(0, (hoje - fim).days) if fim < hoje else 0
    dias_extra = min(dias_ate_hoje, MAX_DIAS_EXTRA)
    consulta_limitada = dias_ate_hoje > MAX_DIAS_EXTRA

    try:
        pedidos = seru.listar_pedidos_completo(inicio, fim, expandir_dias_frente=dias_extra)
    except Exception as e:
        current_app.logger.exception('Seru listar_pedidos falhou')
        return jsonify(ok=False, erro=f'{type(e).__name__}: {str(e)[:300]}'), 502

    # A API filtra por updatedAt, mas o usuario quer ver vendas POR DATA DE
    # CRIACAO (quando a venda aconteceu). Filtramos localmente pelo createdAt
    # dentro do intervalo pedido — convertendo UTC pra BRT antes de comparar.
    total_bruto = len(pedidos)
    def _passa(p):
        if not isinstance(p, dict):
            return False
        d = seru.data_local(p.get('createdAt'))
        return d is not None and inicio <= d <= fim
    pedidos = [p for p in pedidos if _passa(p)]
    fora_intervalo = total_bruto - len(pedidos)

    def _f(v):
        try:
            return float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    def _s(v):
        """Converte qualquer valor pra string utilizavel como chave (dicts/listas viram nome aninhado)."""
        if v is None:
            return ''
        if isinstance(v, str):
            return v
        if isinstance(v, dict):
            return str(v.get('name') or v.get('label') or v.get('tag') or v.get('code') or v.get('type') or '')
        if isinstance(v, (list, tuple)):
            return ', '.join(_s(x) for x in v if x is not None)
        return str(v)

    try:
        total = 0.0
        por_pagamento = {}
        por_canal = {}
        por_loja = {}
        cancelados = 0
        for p in pedidos:
            if not isinstance(p, dict):
                continue
            if p.get('canceledAt'):
                cancelados += 1
                continue
            total += _f(p.get('total'))
            for pay in (p.get('payments') or []):
                if not isinstance(pay, dict):
                    continue
                metodo = _s(pay.get('method') or pay.get('type')) or '—'
                valor = _f(pay.get('value') or pay.get('total') or pay.get('amount'))
                por_pagamento[metodo] = por_pagamento.get(metodo, 0) + valor
            sc = p.get('salesChannel') or {}
            canal = _s(sc) if isinstance(sc, dict) else _s(sc)
            if not canal:
                canal = '—'
            por_canal[canal] = por_canal.get(canal, 0) + _f(p.get('total'))
            company = p.get('company') or {}
            loja = _s(company) if isinstance(company, dict) else _s(company)
            if not loja:
                loja = '—'
            por_loja[loja] = por_loja.get(loja, 0) + _f(p.get('total'))
    except Exception as e:
        import traceback
        current_app.logger.exception('Erro agregando vendas Seru')
        return jsonify(
            ok=False,
            erro=f'{type(e).__name__} ao agregar: {str(e)[:300]}',
            traceback=traceback.format_exc().splitlines()[-5:],
            amostra_pedido=pedidos[0] if pedidos else None,
        ), 500

    try:
        resp = jsonify(
            ok=True,
            inicio=inicio.isoformat(),
            fim=fim.isoformat(),
            total_pedidos=len(pedidos),
            fora_intervalo=fora_intervalo,
            cancelados=cancelados,
            total_valor=total,
            por_pagamento=por_pagamento,
            por_canal=por_canal,
            por_loja=por_loja,
            consulta_limitada=consulta_limitada,
            pedidos=pedidos,
        )
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        resp.headers['Pragma'] = 'no-cache'
        return resp
    except Exception as e:
        current_app.logger.exception('Erro serializando resposta Seru')
        return jsonify(ok=False, erro=f'{type(e).__name__} no jsonify: {str(e)[:300]}'), 500


@pdv_bp.route('/api/vendas/<pedido_id>')
@login_required
@admin_required
def api_venda_detalhe(pedido_id):
    try:
        return jsonify(ok=True, pedido=seru.detalhes_pedido(pedido_id))
    except RuntimeError as e:
        return jsonify(ok=False, erro=str(e)[:300]), 502


# ── Fase 2: Sync Seru → EstoqueLoja (auto-baixa) ──

@pdv_bp.route('/api/mapear', methods=['POST'])
@login_required
@admin_required
def api_mapear():
    """Cria/atualiza SeruProdutoMap inline (do relatorio de itens vendidos)."""
    nome = (request.form.get('seru_nome') or request.json.get('seru_nome') if request.is_json else request.form.get('seru_nome'))
    nome = (nome or '').strip()
    if not nome:
        return jsonify(ok=False, erro='seru_nome obrigatorio'), 400
    data = request.json if request.is_json else request.form
    acao = data.get('acao')  # 'vincular' | 'ignorar' | 'desfazer'
    mp = SeruProdutoMap.query.filter_by(seru_nome=nome).first()
    if not mp:
        mp = SeruProdutoMap(seru_nome=nome)
        db.session.add(mp)
        db.session.flush()
    if acao == 'vincular':
        tipo = data.get('alvo_tipo')
        try:
            alvo_id = int(data.get('alvo_id') or 0)
        except (TypeError, ValueError):
            alvo_id = 0
        if tipo == 'receita' and alvo_id:
            mp.receita_id = alvo_id
            mp.produto_id = None
        elif tipo == 'produto' and alvo_id:
            mp.produto_id = alvo_id
            mp.receita_id = None
        else:
            return jsonify(ok=False, erro='alvo_tipo/alvo_id invalidos'), 400
        # Fator de composicao (1 venda Seru = X unidades do alvo). Default 1.0.
        try:
            fator = float(data.get('fator') or 1.0)
            if fator <= 0:
                fator = 1.0
        except (TypeError, ValueError):
            fator = 1.0
        mp.fator_quantidade = fator
        mp.ignorar = False
        mp.confirmado_em = datetime.utcnow()
        mp.confirmado_por = current_user.id
    elif acao == 'ignorar':
        mp.ignorar = True
        mp.receita_id = None
        mp.produto_id = None
        mp.confirmado_em = datetime.utcnow()
        mp.confirmado_por = current_user.id
    elif acao == 'desfazer':
        mp.ignorar = False
        mp.receita_id = None
        mp.produto_id = None
        mp.confirmado_em = None
        mp.confirmado_por = None
    else:
        return jsonify(ok=False, erro='acao desconhecida'), 400
    db.session.commit()
    return jsonify(ok=True, estado=mp.estado, alvo_nome=mp.alvo_nome, map_id=mp.id)


@pdv_bp.route('/sync', methods=['POST'])
@login_required
@admin_required
def pdv_sync():
    """Botao 'Sincronizar agora'. Processa vendas dos N dias informados."""
    from app.services import seru_sync
    try:
        dias = max(1, min(int(request.form.get('dias') or 1), 30))
    except ValueError:
        dias = 1
    fim = date.today()
    inicio = fim - timedelta(days=dias - 1)
    try:
        stats = seru_sync.processar_pedidos(inicio, fim, user=current_user)
    except Exception as e:
        current_app.logger.exception('seru_sync falhou')
        return jsonify(ok=False, erro=f'{type(e).__name__}: {str(e)[:300]}'), 502
    return jsonify(ok=True, **stats)


@pdv_bp.route('/mapeamentos')
@login_required
@admin_required
def mapeamentos():
    """Tela de mapeamento de produtos Seru e lojas Seru."""
    produtos_map = SeruProdutoMap.query.order_by(
        SeruProdutoMap.ignorar.asc(),
        SeruProdutoMap.confirmado_em.is_(None).desc(),  # pendentes no topo
        SeruProdutoMap.seru_nome,
    ).all()
    lojas_map = SeruLojaMap.query.order_by(SeruLojaMap.seru_company_name).all()
    receitas = Receita.query.order_by(Receita.categoria, Receita.nome).all()
    produtos = Produto.query.filter_by(ativo=True).order_by(Produto.nome).all()
    lojas = Loja.query.filter_by(ativa=True).order_by(Loja.nome).all()
    return render_template('pdv/mapeamentos.html',
                           produtos_map=produtos_map, lojas_map=lojas_map,
                           receitas=receitas, produtos=produtos, lojas=lojas)


@pdv_bp.route('/mapeamentos/produto/<int:map_id>', methods=['POST'])
@login_required
@admin_required
def vincular_produto(map_id):
    """Vincula/ignora/limpa um produto Seru."""
    mp = SeruProdutoMap.query.get_or_404(map_id)
    acao = request.form.get('acao')
    # Mesmo fallback do vincular_loja: se acao nao veio mas alvo_id sim,
    # assume 'vincular' (caso Enter no dropdown).
    if not acao and request.form.get('alvo_id'):
        acao = 'vincular'
    if not acao:
        flash('Clique em "Vincular", "Ignorar" ou "Desfazer".', 'warning')
        return redirect(url_for('pdv.mapeamentos'))
    if acao == 'vincular':
        tipo = request.form.get('alvo_tipo')
        try:
            alvo_id = int(request.form.get('alvo_id', ''))
        except (TypeError, ValueError):
            alvo_id = 0
        if tipo == 'receita' and alvo_id:
            mp.receita_id = alvo_id
            mp.produto_id = None
            mp.ignorar = False
        elif tipo == 'produto' and alvo_id:
            mp.produto_id = alvo_id
            mp.receita_id = None
            mp.ignorar = False
        else:
            flash('Selecione receita ou produto valido.', 'danger')
            return redirect(url_for('pdv.mapeamentos'))
        try:
            fator = float(request.form.get('fator') or 1.0)
            if fator <= 0:
                fator = 1.0
        except (TypeError, ValueError):
            fator = 1.0
        mp.fator_quantidade = fator
        mp.confirmado_em = datetime.utcnow()
        mp.confirmado_por = current_user.id
        fator_msg = '' if fator == 1.0 else f' · fator {fator}'
        flash(f'"{mp.seru_nome}" → {mp.alvo_nome}{fator_msg}', 'success')
    elif acao == 'ignorar':
        mp.ignorar = True
        mp.receita_id = None
        mp.produto_id = None
        mp.confirmado_em = datetime.utcnow()
        mp.confirmado_por = current_user.id
        flash(f'"{mp.seru_nome}" ignorado — nao baixara estoque.', 'info')
    elif acao == 'desfazer':
        mp.ignorar = False
        mp.receita_id = None
        mp.produto_id = None
        mp.confirmado_em = None
        mp.confirmado_por = None
        flash(f'"{mp.seru_nome}" voltou pra pendente.', 'info')
    db.session.commit()
    return redirect(url_for('pdv.mapeamentos'))


@pdv_bp.route('/mapeamentos/loja/<int:map_id>', methods=['POST'])
@login_required
@admin_required
def vincular_loja(map_id):
    lm = SeruLojaMap.query.get_or_404(map_id)
    acao = request.form.get('acao')
    raw_loja = request.form.get('loja_id', '')

    # Se acao nao veio (caso classico: usuario apertou Enter no dropdown
    # em vez de clicar um botao — o navegador nao envia o 'submitter'),
    # inferir pela presenca de loja_id.
    if not acao:
        if raw_loja and raw_loja.strip():
            acao = 'vincular'
        else:
            flash('Clique em "Vincular", "Ignorar" ou "OK" — nao da pra adivinhar a acao.', 'warning')
            return redirect(url_for('pdv.mapeamentos'))

    current_app.logger.info(
        'vincular_loja id=%s acao=%s raw_loja=%r form_keys=%s',
        map_id, acao, raw_loja, list(request.form.keys()))
    if acao == 'vincular':
        try:
            loja_id = int(raw_loja) if raw_loja else 0
        except (TypeError, ValueError):
            loja_id = 0
        if not loja_id:
            flash(f'Selecione uma loja antes de clicar Vincular (recebido: "{raw_loja or "vazio"}"). '
                  f'Clique no dropdown, escolha, depois Vincular.', 'danger')
            return redirect(url_for('pdv.mapeamentos'))
        loja_obj = Loja.query.get(loja_id)
        if not loja_obj:
            flash(f'Loja id={loja_id} nao existe.', 'danger')
            return redirect(url_for('pdv.mapeamentos'))
        lm.loja_id = loja_id
        lm.ignorar = False
        lm.auto_match = False
        lm.confirmado_em = datetime.utcnow()
        lm.confirmado_por = current_user.id
        db.session.commit()
        flash(f'OK: "{lm.seru_company_name}" agora vinculada a {loja_obj.nome}. '
              f'Vendas dessa company vao baixar estoque dessa loja.', 'success')
        return redirect(url_for('pdv.mapeamentos'))
    if acao == 'ignorar':
        lm.ignorar = True
        lm.loja_id = None
        lm.confirmado_em = datetime.utcnow()
        lm.confirmado_por = current_user.id
        db.session.commit()
        flash(f'"{lm.seru_company_name}" ignorada — vendas nao processarao.', 'info')
        return redirect(url_for('pdv.mapeamentos'))
    if acao == 'confirmar':
        lm.auto_match = False
        lm.confirmado_em = datetime.utcnow()
        lm.confirmado_por = current_user.id
        db.session.commit()
        flash(f'"{lm.seru_company_name}" confirmada.', 'success')
        return redirect(url_for('pdv.mapeamentos'))
    flash(f'Acao desconhecida: "{acao}".', 'warning')
    return redirect(url_for('pdv.mapeamentos'))


# ════════════════════════════════════════════════════════════════════
# VNDA (site) — espelha o Seru mas com loja fixa e baixa por data entrega
# ════════════════════════════════════════════════════════════════════

@pdv_bp.route('/vnda/mapeamentos')
@login_required
@admin_required
def vnda_mapeamentos():
    from app.services import vnda_sync as svc
    produtos_map = VndaProdutoMap.query.order_by(
        VndaProdutoMap.ignorar.asc(),
        VndaProdutoMap.confirmado_em.is_(None).desc(),
        VndaProdutoMap.vnda_nome,
    ).all()
    receitas = Receita.query.order_by(Receita.categoria, Receita.nome).all()
    produtos = Produto.query.filter_by(ativo=True).order_by(Produto.nome).all()
    lojas = Loja.query.filter_by(ativa=True).order_by(Loja.nome).all()
    loja_atual = svc.loja_vnda()
    return render_template('pdv/vnda_mapeamentos.html',
                           produtos_map=produtos_map,
                           receitas=receitas, produtos=produtos,
                           lojas=lojas, loja_atual=loja_atual)


@pdv_bp.route('/vnda/config-loja', methods=['POST'])
@login_required
@admin_required
def vnda_config_loja():
    """Salva qual loja recebe as baixas VNDA."""
    try:
        loja_id = int(request.form.get('loja_id') or 0)
    except (TypeError, ValueError):
        loja_id = 0
    if not loja_id:
        flash('Selecione uma loja valida.', 'danger')
        return redirect(url_for('pdv.vnda_mapeamentos'))
    loja = Loja.query.get(loja_id)
    if not loja:
        flash(f'Loja id={loja_id} nao existe.', 'danger')
        return redirect(url_for('pdv.vnda_mapeamentos'))
    AppConfig.set('vnda_loja_id', loja.id)
    db.session.commit()
    flash(f'Loja destino VNDA agora e "{loja.nome}". Baixas vao pra essa loja a partir de agora.', 'success')
    return redirect(url_for('pdv.vnda_mapeamentos'))


@pdv_bp.route('/vnda/mapeamentos/produto/<int:map_id>', methods=['POST'])
@login_required
@admin_required
def vnda_vincular_produto(map_id):
    mp = VndaProdutoMap.query.get_or_404(map_id)
    acao = request.form.get('acao')
    if not acao and request.form.get('alvo_id'):
        acao = 'vincular'
    if not acao:
        flash('Clique em "Vincular", "Ignorar" ou "Desfazer".', 'warning')
        return redirect(url_for('pdv.vnda_mapeamentos'))

    if acao == 'vincular':
        tipo = request.form.get('alvo_tipo')
        try:
            alvo_id = int(request.form.get('alvo_id', ''))
        except (TypeError, ValueError):
            alvo_id = 0
        if tipo == 'receita' and alvo_id:
            mp.receita_id = alvo_id; mp.produto_id = None; mp.ignorar = False
        elif tipo == 'produto' and alvo_id:
            mp.produto_id = alvo_id; mp.receita_id = None; mp.ignorar = False
        else:
            flash('Selecione receita ou produto valido.', 'danger')
            return redirect(url_for('pdv.vnda_mapeamentos'))
        try:
            fator = float(request.form.get('fator') or 1.0)
            if fator <= 0:
                fator = 1.0
        except (TypeError, ValueError):
            fator = 1.0
        mp.fator_quantidade = fator
        mp.confirmado_em = datetime.utcnow()
        mp.confirmado_por = current_user.id
        fator_msg = '' if fator == 1.0 else f' · fator {fator}'
        flash(f'"{mp.vnda_nome}" → {mp.alvo_nome}{fator_msg}', 'success')
    elif acao == 'ignorar':
        mp.ignorar = True; mp.receita_id = None; mp.produto_id = None
        mp.confirmado_em = datetime.utcnow(); mp.confirmado_por = current_user.id
        flash(f'"{mp.vnda_nome}" ignorado — nao baixara estoque.', 'info')
    elif acao == 'desfazer':
        mp.ignorar = False; mp.receita_id = None; mp.produto_id = None
        mp.confirmado_em = None; mp.confirmado_por = None
        flash(f'"{mp.vnda_nome}" voltou pra pendente.', 'info')
    db.session.commit()
    return redirect(url_for('pdv.vnda_mapeamentos'))


@pdv_bp.route('/vnda/sync', methods=['POST'])
@login_required
@admin_required
def vnda_sync():
    """Botao 'Sincronizar VNDA agora' — processa pedidos com entrega hoje."""
    from app.services import vnda_sync as svc
    from app.services.seru_cron import hoje_brt
    hoje = hoje_brt()
    try:
        stats = svc.processar_pedidos(hoje, user=current_user)
    except Exception as e:
        current_app.logger.exception('vnda_sync falhou')
        return jsonify(ok=False, erro=f'{type(e).__name__}: {str(e)[:300]}'), 502
    if stats.get('erro'):
        return jsonify(ok=False, erro=stats['erro']), 502
    return jsonify(ok=True, **stats)


@pdv_bp.route('/vnda/reprocessar', methods=['POST'])
@login_required
@admin_required
def vnda_reprocessar():
    """Apaga pedidos VNDA processados HOJE com baixados=0 e re-roda.
    Safety identica a do Seru: nao apaga os ja-baixados."""
    from app.services import vnda_sync as svc
    from app.services.seru_cron import hoje_brt
    hoje = hoje_brt()
    inicio_dia_utc = datetime.combine(hoje, datetime.min.time()) + timedelta(hours=3)
    n_apagados = VndaPedidoProcessado.query.filter(
        VndaPedidoProcessado.processado_em >= inicio_dia_utc,
        VndaPedidoProcessado.n_itens_baixados == 0,
        VndaPedidoProcessado.estornado_em.is_(None),
        VndaPedidoProcessado.cancelado_em.is_(None),
    ).delete(synchronize_session=False)
    db.session.commit()
    try:
        stats = svc.processar_pedidos(hoje, user=current_user)
    except Exception as e:
        current_app.logger.exception('vnda_reprocessar falhou')
        return jsonify(ok=False, erro=f'{type(e).__name__}: {str(e)[:300]}'), 502
    if stats.get('erro'):
        return jsonify(ok=False, erro=stats['erro']), 502
    stats['n_apagados'] = n_apagados
    return jsonify(ok=True, **stats)


@pdv_bp.route('/vnda/historico-sync')
@login_required
@admin_required
def vnda_historico_sync():
    from sqlalchemy import desc, or_
    status_filtro = request.args.get('status', '')
    q = VndaPedidoProcessado.query
    if status_filtro == 'baixados':
        q = q.filter(VndaPedidoProcessado.n_itens_baixados > 0,
                     VndaPedidoProcessado.cancelado_em.is_(None))
    elif status_filtro == 'estornados':
        q = q.filter(VndaPedidoProcessado.estornado_em.isnot(None))
    elif status_filtro == 'cancelados':
        q = q.filter(VndaPedidoProcessado.cancelado_em.isnot(None))
    elif status_filtro == 'sem_baixa':
        q = q.filter(VndaPedidoProcessado.n_itens_baixados == 0,
                     VndaPedidoProcessado.cancelado_em.is_(None),
                     VndaPedidoProcessado.estornado_em.is_(None))

    pedidos = q.order_by(desc(VndaPedidoProcessado.processado_em)).limit(200).all()

    movs_por_pedido = {}
    if pedidos:
        clauses = [MovEstoqueLoja.referencia.like(f'VNDA #{p.vnda_pedido_code}%') for p in pedidos]
        all_movs = MovEstoqueLoja.query.filter(or_(*clauses)).all()
        for m in all_movs:
            pref = (m.referencia or '').split(' ', 2)
            if len(pref) >= 2 and pref[0] == 'VNDA' and pref[1].startswith('#'):
                pid = pref[1][1:]
                movs_por_pedido.setdefault(pid, []).append(m)

    return render_template('pdv/vnda_historico_sync.html',
                           pedidos=pedidos, sel_status=status_filtro,
                           movs_por_pedido=movs_por_pedido)
