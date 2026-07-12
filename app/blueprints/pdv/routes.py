"""Vendas PDV via integracao Seru. Sob demanda — sem cache local."""
from datetime import datetime, timedelta

from flask import current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.blueprints.pdv import pdv_bp
from app.decorators import admin_required, owner_required
from app.extensions import db
from app.models import (
    AppConfig,
    Loja,
    MovEstoqueLoja,
    Produto,
    Receita,
    SeruLojaMap,
    SeruPedidoProcessado,
    VendaMapa,
)
from app.services import seru
from app.utils import agora
from app.utils import hoje as hoje_brt


def _erro_externo(e):
    """Mensagem amigavel pra falha ao chamar servico externo (Seru/VNDA),
    sem expor o stacktrace tecnico (HTTPSConnectionPool...) ao usuario."""
    import requests
    s = str(e)
    servico = ('Seru' if 'plataformaseru' in s
               else 'VNDA' if 'vnda' in s.lower()
               else 'serviço externo')
    if isinstance(e, requests.exceptions.Timeout):
        return f'O {servico} demorou para responder (timeout). Tente de novo em instantes.'
    if isinstance(e, requests.exceptions.ConnectionError):
        return f'Não consegui conectar ao {servico} agora. Tente de novo em instantes.'
    return f'Falha ao consultar o {servico} ({type(e).__name__}). Tente de novo em instantes.'


@pdv_bp.route('/')
@login_required
@admin_required
def index():
    return render_template('pdv/index.html', hoje=hoje_brt().isoformat())


@pdv_bp.route('/reprocessar', methods=['POST'])
@login_required
@admin_required
def reprocessar():
    """Reprocessa SOMENTE as vendas de HOJE (BRT) — vendas de ontem ou
    anteriores ficam intocadas (preferencia do usuario).

    Apaga SeruPedidoProcessado dos pedidos de hoje que NAO baixaram nada
    e re-roda processar_pedidos. Pedidos com baixados>0 nao sao apagados
    (zero risco de duplo desconto). Cancelados/estornados tambem nao."""
    from sqlalchemy import or_

    from app.services import seru_sync
    from app.services.seru_cron import hoje_brt

    hoje = hoje_brt()
    # Inicio do dia BRT em UTC: 00:00 BRT = 03:00 UTC
    inicio_dia_utc = datetime.combine(hoje, datetime.min.time()) + timedelta(hours=3)

    # Apaga so os de HOJE "sem baixa"
    alvo_q = SeruPedidoProcessado.query.filter(
        SeruPedidoProcessado.processado_em >= inicio_dia_utc,
        SeruPedidoProcessado.n_itens_baixados == 0,
        SeruPedidoProcessado.estornado_em.is_(None),
        SeruPedidoProcessado.cancelado_em.is_(None),
    )
    ids = [p.seru_pedido_id for p in alvo_q.all()]
    # Limpa MovEstoqueLoja antigas (todas sem_estoque, ok apagar)
    if ids:
        clauses = [MovEstoqueLoja.referencia.like(f'Seru #{i}%') for i in ids]
        MovEstoqueLoja.query.filter(or_(*clauses)).delete(synchronize_session=False)
    n_apagados = alvo_q.delete(synchronize_session=False)
    db.session.commit()

    try:
        stats = seru_sync.processar_pedidos(hoje, hoje, user=current_user)
    except Exception as e:
        current_app.logger.exception('reprocessar falhou')
        return jsonify(ok=False, erro=_erro_externo(e)), 502
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
        from sqlalchemy import or_
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
    receitas = Receita.ativas().order_by(Receita.categoria, Receita.nome).all()
    produtos = Produto.query.filter_by(ativo=True).order_by(Produto.nome).all()
    cron_status = seru_cron.status()
    return render_template('pdv/itens_vendidos.html',
                           hoje=hoje_brt().isoformat(),
                           receitas=receitas, produtos=produtos,
                           cron_status=cron_status)


@pdv_bp.route('/api/itens-vendidos')
@login_required
@admin_required
def api_itens_vendidos():
    """Relatorio FLAT (consolidado por produto). Le do BANCO por padrao
    (?ao_vivo=1 forca a API). loja= filtra por company.name do Seru."""
    from app.services import vendas_diarias, vendas_itens
    loja = (request.args.get('loja') or '').strip() or None
    inicio, fim, erro = _parse_intervalo_itens()
    if erro:
        return jsonify(ok=False, erro=erro), 400
    try:
        if request.args.get('ao_vivo'):
            from app.utils import hoje as _hoje_brt
            hoje = _hoje_brt()
            dias_extra = min(max(0, (hoje - fim).days) if fim < hoje else 0, 7)
            data = vendas_itens.agregar_itens(inicio, fim, loja_seru=loja,
                                              expandir_dias_frente=dias_extra)
        else:
            data = vendas_diarias.agregar_flat(inicio, fim, loja_seru=loja)
    except Exception as e:
        current_app.logger.exception('itens-vendidos falhou')
        return jsonify(ok=False, erro=_erro_externo(e)), 502
    return jsonify(ok=True, **data)


def _parse_intervalo_itens():
    """Le inicio/fim (YYYY-MM-DD) do querystring. Retorna (inicio, fim, erro)."""
    from app.utils import hoje as _hoje_brt
    inicio_str = request.args.get('inicio') or _hoje_brt().isoformat()
    fim_str = request.args.get('fim') or inicio_str
    try:
        inicio = datetime.strptime(inicio_str, '%Y-%m-%d').date()
        fim = datetime.strptime(fim_str, '%Y-%m-%d').date()
    except ValueError:
        return None, None, 'datas invalidas (use YYYY-MM-DD)'
    if (fim - inicio).days > 92:
        return None, None, 'intervalo maximo de 92 dias'
    if fim < inicio:
        return None, None, 'fim antes do inicio'
    return inicio, fim, None


def _dados_itens_por_loja(inicio, fim, ao_vivo=False):
    """Itens vendidos por loja. Por padrao le do BANCO (VendaSeruDiaria),
    capturando os dias que faltam + SEMPRE hoje (as vendas de hoje crescem) e
    caindo pro snapshot existente se a API falhar. ao_vivo=True forca a consulta
    direta na API (nao persiste) — util pra comparar."""
    from app.services import vendas_diarias, vendas_itens
    if ao_vivo:
        from app.utils import hoje as _hoje_brt
        hoje = _hoje_brt()
        dias_extra = min(max(0, (hoje - fim).days) if fim < hoje else 0, 7)
        return vendas_itens.agregar_itens_por_loja(
            inicio, fim, expandir_dias_frente=dias_extra)
    vendas_diarias.garantir_capturado(inicio, fim)
    return vendas_diarias.agregar_por_loja_do_banco(inicio, fim)


@pdv_bp.route('/api/itens-vendidos-por-loja')
@login_required
@admin_required
def api_itens_vendidos_por_loja():
    """Itens vendidos SEPARADOS POR LOJA (secoes recolhiveis na tela). Le do
    banco por padrao (?ao_vivo=1 forca a API)."""
    inicio, fim, erro = _parse_intervalo_itens()
    if erro:
        return jsonify(ok=False, erro=erro), 400
    ao_vivo = bool(request.args.get('ao_vivo'))
    try:
        data = _dados_itens_por_loja(inicio, fim, ao_vivo=ao_vivo)
    except Exception as e:
        current_app.logger.exception('itens-vendidos-por-loja falhou')
        return jsonify(ok=False, erro=_erro_externo(e)), 502
    return jsonify(ok=True, **data)


@pdv_bp.route('/itens-vendidos.xlsx')
@login_required
@admin_required
def itens_vendidos_xlsx():
    """Exporta os itens vendidos em XLSX — uma aba por loja + Consolidado."""
    import io

    from flask import send_file

    from app.services import vendas_itens
    inicio, fim, erro = _parse_intervalo_itens()
    if erro:
        flash(erro, 'danger')
        return redirect(url_for('pdv.itens_vendidos'))
    ao_vivo = bool(request.args.get('ao_vivo'))
    try:
        dados = _dados_itens_por_loja(inicio, fim, ao_vivo=ao_vivo)
        blob = vendas_itens.gerar_xlsx_itens_por_loja(dados)
    except Exception as e:
        current_app.logger.exception('export xlsx itens-vendidos falhou')
        flash('Erro ao exportar: %s' % _erro_externo(e), 'danger')
        return redirect(url_for('pdv.itens_vendidos'))
    nome = 'itens_vendidos_%s_a_%s.xlsx' % (inicio.isoformat(), fim.isoformat())
    return send_file(
        io.BytesIO(blob),
        mimetype=('application/vnd.openxmlformats-officedocument'
                  '.spreadsheetml.sheet'),
        as_attachment=True, download_name=nome)


@pdv_bp.route('/vendas-diarias/backfill', methods=['POST'])
@login_required
@owner_required
def vendas_diarias_backfill():
    """Pre-carrega o historico de vendas Seru no banco (VendaSeruDiaria), em
    background, semana a semana. Owner-only. O relatorio ja captura sob demanda;
    isto so aquece o passado de uma vez. Status em AppConfig."""
    import threading
    from datetime import timedelta as _td

    from app.utils import hoje as _hoje_brt
    try:
        dias = max(1, min(int(request.form.get('dias') or 90), 366))
    except ValueError:
        dias = 90
    app_obj = current_app._get_current_object()
    hoje = _hoje_brt()
    ini = hoje - _td(days=dias)

    def _runner():
        from app.extensions import db as _db
        from app.models import AppConfig
        from app.services import vendas_diarias
        with app_obj.app_context():
            try:
                total = 0
                d = ini
                while d <= hoje:
                    fim_sem = min(d + _td(days=6), hoje)
                    r = vendas_diarias.capturar_periodo(d, fim_sem)
                    total += r['linhas']
                    AppConfig.set('vendas_diarias_backfill',
                                  'progresso: ate %s (%d linhas)' % (fim_sem, total))
                    _db.session.commit()
                    d = fim_sem + _td(days=1)
                AppConfig.set('vendas_diarias_backfill',
                              'ok: %s..%s (%d linhas)' % (ini, hoje, total))
                _db.session.commit()
            except Exception as e:  # noqa: BLE001
                app_obj.logger.exception('backfill vendas_diarias falhou')
                try:
                    AppConfig.set('vendas_diarias_backfill', 'erro: %s' % str(e)[:200])
                    _db.session.commit()
                except Exception:  # noqa: BLE001
                    pass

    threading.Thread(target=_runner, daemon=True).start()
    flash('Backfill de vendas iniciado (%d dias) — roda em background; '
          'recarregue em alguns minutos.' % dias, 'info')
    return redirect(url_for('pdv.itens_vendidos'))


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


def _site_block(inicio, fim):
    """Vendas do SITE (loja propria / PedidoOnline) no intervalo — outra fonte,
    fora do Seru. Faturamento = subtotal (sem frete), mesma regra do consolidado.
    Best-effort: se falhar, devolve zero + flag de erro (nunca derruba a tela)."""
    try:
        from app.services import loja_online_vendas
        fat_site = loja_online_vendas.faturamento_por_dia(inicio, fim)
        return {'total': fat_site.get('total', 0.0),
                'n_pedidos': fat_site.get('n_pedidos', 0)}
    except Exception:
        current_app.logger.exception('pdv: faturamento do site falhou')
        return {'total': 0.0, 'n_pedidos': 0, 'erro': True}


def _api_vendas_impl():
    """Vendas Seru (PDV) + site no intervalo. Default: hoje.

    Le do NOSSO banco (snapshot diario, `vendas_diarias`) por padrao — rapido e
    resiliente a quedas da API Seru, que com ~600 pedidos/dia estourava em ranges
    largos. `?ao_vivo=1` forca a consulta direta a API (traz tambem o detalhe
    pedido-a-pedido, util pra ranges curtos).

    ?inicio=YYYY-MM-DD&fim=YYYY-MM-DD
    """
    inicio_str = request.args.get('inicio') or hoje_brt().isoformat()
    fim_str = request.args.get('fim') or inicio_str
    try:
        inicio = datetime.strptime(inicio_str, '%Y-%m-%d').date()
        fim = datetime.strptime(fim_str, '%Y-%m-%d').date()
    except ValueError:
        return jsonify(ok=False, erro='datas invalidas (use YYYY-MM-DD)'), 400

    if (fim - inicio).days > 92:
        return jsonify(ok=False, erro='intervalo maximo de 92 dias'), 400

    ao_vivo = request.args.get('ao_vivo') in ('1', 'true', 'sim')

    if not ao_vivo:
        # ── Caminho padrao: le do snapshot do banco (sem tocar na API Seru). ──
        try:
            from app.services import vendas_diarias
            d = vendas_diarias.vendas_pdv_do_banco(inicio, fim)
        except Exception as e:
            current_app.logger.exception('pdv vendas: leitura do banco falhou')
            return jsonify(
                ok=False,
                erro=f'{type(e).__name__}: {str(e)[:300]}'), 500
        resp = jsonify(
            ok=True,
            inicio=inicio.isoformat(),
            fim=fim.isoformat(),
            fonte='banco',
            # total_pedidos inclui cancelados (o front faz total - cancelados
            # pra os cards), pra casar com a semantica do caminho ao vivo.
            total_pedidos=d['n_pedidos'] + d['cancelados'],
            fora_intervalo=0,
            cancelados=d['cancelados'],
            total_valor=d['total_valor'],
            por_pagamento=d['por_pagamento'],
            por_canal=d['por_canal'],
            por_loja=d['por_loja'],
            por_loja_detalhe=d['por_loja_detalhe'],
            site=_site_block(inicio, fim),
            consulta_limitada=False,
            pedidos=None,
        )
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        resp.headers['Pragma'] = 'no-cache'
        return resp

    # ── ?ao_vivo=1: consulta direta a API Seru (traz o detalhe por pedido). ──
    # Expandimos a janela de updatedAt ate N dias pra frente do fim, pra
    # capturar pedidos criados no intervalo mas atualizados depois.
    # Ainda filtramos por createdAt local. Cada dia adicional eh +1 chamada
    # Seru, entao limitamos pra evitar timeout do gunicorn (60s).
    MAX_DIAS_EXTRA = 7
    hoje = hoje_brt()
    dias_ate_hoje = max(0, (hoje - fim).days) if fim < hoje else 0
    dias_extra = min(dias_ate_hoje, MAX_DIAS_EXTRA)
    consulta_limitada = dias_ate_hoje > MAX_DIAS_EXTRA

    try:
        pedidos = seru.listar_pedidos_completo(inicio, fim, expandir_dias_frente=dias_extra)
    except Exception as e:
        current_app.logger.exception('Seru listar_pedidos falhou')
        return jsonify(ok=False, erro=_erro_externo(e)), 502

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

    # Vendas do SITE (loja propria / PedidoOnline) — outra fonte, fora do Seru.
    # O front soma no total/pedidos/ticket e numa linha "Site" do canal de venda
    # quando NAO ha filtro de loja (o site nao e uma loja Seru).
    site = _site_block(inicio, fim)

    try:
        resp = jsonify(
            ok=True,
            inicio=inicio.isoformat(),
            fim=fim.isoformat(),
            fonte='ao_vivo',
            total_pedidos=len(pedidos),
            fora_intervalo=fora_intervalo,
            cancelados=cancelados,
            total_valor=total,
            por_pagamento=por_pagamento,
            por_canal=por_canal,
            por_loja=por_loja,
            site=site,
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


@pdv_bp.route('/seru/<pedido_id>')
@login_required
@admin_required
def venda_seru_detalhe(pedido_id):
    """Mostra um pedido Seru completo com status de mapeamento de cada item.

    Util pra diagnosticar 'porque o pedido foi processado mas so X de Y itens
    baixaram': cada item nao baixado aparece com motivo (pendente / ignorado /
    sem cadastro).
    """
    try:
        pedido = seru.detalhes_pedido(pedido_id)
    except RuntimeError as e:
        flash(f'Erro Seru: {e}', 'danger')
        return redirect(url_for('pdv.itens_vendidos'))

    itens_raw = seru.extrair_itens(pedido)

    # Cross-reference com VendaMapa (canal seru)
    nomes = list({it['nome'] for it in itens_raw if it.get('nome')})
    maps_dict = {}
    if nomes:
        maps = VendaMapa.query.filter(VendaMapa.canal == 'seru',
                                      VendaMapa.nome_externo.in_(nomes)).all()
        maps_dict = {m.nome_externo: m for m in maps}

    itens = []
    for it in itens_raw:
        nome = it.get('nome', '')
        mapa = maps_dict.get(nome)
        estado = mapa.estado if mapa else 'novo'
        alvo = None
        if mapa:
            if mapa.receita_id:
                from app.models import Receita
                r = Receita.query.get(mapa.receita_id)
                alvo = ('receita', r.nome if r else f'id={mapa.receita_id}')
            elif mapa.produto_id:
                p = Produto.query.get(mapa.produto_id)
                alvo = ('produto', p.nome if p else f'id={mapa.produto_id}')
        itens.append({
            'nome': nome, 'sku': it.get('sku', ''),
            'qtd': it.get('qtd', 0), 'total': it.get('total', 0),
            'cancelado': it.get('cancelado', False),
            'estado': estado, 'alvo': alvo,
            'fator': mapa.fator_quantidade if mapa else None,
        })

    # SeruPedidoProcessado: o que registramos
    processado = SeruPedidoProcessado.query.get(pedido_id)

    return render_template('pdv/venda_seru_detalhe.html',
                           pedido_id=pedido_id, pedido_raw=pedido,
                           itens=itens, processado=processado)


# ── Fase 2: Sync Seru → EstoqueLoja (auto-baixa) ──

@pdv_bp.route('/api/mapear', methods=['POST'])
@login_required
@admin_required
def api_mapear():
    """Cria/atualiza VendaMapa(canal=seru) inline (do relatorio de itens vendidos)."""
    from app.utils import parse_fator_composicao
    data = request.json if request.is_json else request.form
    nome = (data.get('seru_nome') or '').strip()
    if not nome:
        return jsonify(ok=False, erro='seru_nome obrigatorio'), 400
    acao = data.get('acao')  # 'vincular' | 'ignorar' | 'desfazer'
    mp = VendaMapa.query.filter_by(canal='seru', nome_externo=nome).first()
    if not mp:
        mp = VendaMapa(canal='seru', nome_externo=nome)
        db.session.add(mp)
        db.session.flush()
    if acao == 'vincular':
        tipo = data.get('alvo_tipo')
        try:
            alvo_id = int(data.get('alvo_id') or 0)
        except (TypeError, ValueError):
            alvo_id = 0
        # Fator de composicao (1 venda Seru = X un do alvo). Vazio -> 1.0;
        # invalido/<=0 NAO vira 1.0 em silencio (baixaria estoque errado) -> 400.
        try:
            fator = parse_fator_composicao(data.get('fator'))
        except ValueError:
            return jsonify(ok=False, erro='fator invalido — use numero > 0 (ex: 0.2)'), 400
        if tipo == 'receita' and alvo_id:
            mp.receita_id = alvo_id
            mp.produto_id = None
        elif tipo == 'produto' and alvo_id:
            mp.produto_id = alvo_id
            mp.receita_id = None
        else:
            return jsonify(ok=False, erro='alvo_tipo/alvo_id invalidos'), 400
        mp.fator_quantidade = fator
        mp.ignorar = False
        mp.confirmado_em = agora()
        mp.confirmado_por = current_user.id
    elif acao == 'ignorar':
        mp.ignorar = True
        mp.receita_id = None
        mp.produto_id = None
        mp.confirmado_em = agora()
        mp.confirmado_por = current_user.id
    elif acao == 'desfazer':
        mp.ignorar = False
        mp.receita_id = None
        mp.produto_id = None
        mp.confirmado_em = None
        mp.confirmado_por = None
        mp.fator_quantidade = 1.0  # volta pra pristine — fator nao fica pegajoso
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
    fim = hoje_brt()
    inicio = fim - timedelta(days=dias - 1)
    try:
        stats = seru_sync.processar_pedidos(inicio, fim, user=current_user)
    except Exception as e:
        current_app.logger.exception('seru_sync falhou')
        return jsonify(ok=False, erro=_erro_externo(e)), 502
    return jsonify(ok=True, **stats)


@pdv_bp.route('/saude')
@login_required
@admin_required
def saude():
    """Painel de saude do sync: ultimo run + pendencias que travam baixas."""
    from app.services import pdv_saude
    return render_template('pdv/saude.html', s=pdv_saude.resumo())


@pdv_bp.route('/reprocessar-retroativo', methods=['POST'])
@login_required
@admin_required
def reprocessar_retroativo_rota():
    """Botao na Saude do PDV: recupera baixas perdidas da janela (pedidos com
    ZERO baixa, inclusive sem-loja) reprocessando com os mapeamentos ATUAIS.
    Parciais nao sao tocados (re-baixariam o que ja saiu)."""
    from app.services import seru_sync
    try:
        dias = max(1, min(int(request.form.get('dias') or 30), 30))
    except (TypeError, ValueError):
        dias = 30
    try:
        status, res = seru_sync.reprocessar_retroativo_manual(
            dias=dias, user=current_user)
    except Exception as e:  # noqa: BLE001 — API Seru fora nao pode dar 500
        current_app.logger.exception('reprocessar_retroativo falhou')
        flash(f'Reprocesso falhou (API Seru?): {type(e).__name__}. '
              'Tente de novo em alguns minutos.', 'danger')
        return redirect(url_for('pdv.saude'))
    if status == 'ocupado':
        flash('Já existe um reprocesso rodando (disparado por um vínculo '
              'recente). Aguarde alguns minutos e confira o resultado aqui.',
              'info')
        return redirect(url_for('pdv.saude'))
    st = res.get('stats') or {}
    if st.get('erros'):
        # Commit falhou no meio: as baixas da rodada foram REVERTIDAS —
        # nao anunciar numeros que nao estao no banco.
        flash(f"Reprocesso parcial: {len(st['erros'])} erro(s) de commit — "
              'as baixas desta rodada foram revertidas. Tente novamente em '
              'alguns minutos.', 'warning')
        return redirect(url_for('pdv.saude'))
    msg = (f"Retroativo ({dias}d): {res.get('liberados', 0)} pedido(s) sem "
           f"baixa liberados; {st.get('itens_baixados', 0)} item(ns) "
           f"baixado(s), {st.get('itens_pendentes_novos', 0)} ainda "
           'pendentes de mapa.')
    if res.get('parciais_na_janela'):
        msg += (f" {res['parciais_na_janela']} pedido(s) parciais não são "
                'recuperáveis automaticamente.')
    flash(msg, 'success' if st.get('itens_baixados') else 'info')
    return redirect(url_for('pdv.saude'))


@pdv_bp.route('/reconciliacao')
@login_required
@admin_required
def reconciliacao():
    """Reconciliacao: vendido no Seru vs baixado no estoque, no periodo."""
    from app.services import pdv_saude
    fim = hoje_brt()
    inicio = fim - timedelta(days=6)  # ultimos 7 dias
    try:
        di = datetime.strptime(request.args['inicio'], '%Y-%m-%d').date()
        df = datetime.strptime(request.args['fim'], '%Y-%m-%d').date()
        if di <= df:
            inicio, fim = di, df
    except (KeyError, ValueError):
        pass
    dados = pdv_saude.reconciliar(inicio, fim)
    # VNDA aposentado (site proprio agora) — sem aba/reconciliacao VNDA aqui.
    # Alvos pra vincular inline (mesma fonte do modal de itens-vendidos).
    receitas = Receita.ativas().order_by(Receita.nome).all()
    produtos = Produto.query.filter_by(ativo=True).order_by(Produto.nome).all()
    return render_template('pdv/reconciliacao.html', d=dados,
                           inicio=inicio.isoformat(), fim=fim.isoformat(),
                           receitas=receitas, produtos=produtos)


@pdv_bp.route('/mapeamentos')
@login_required
@admin_required
def mapeamentos():
    """Tela de mapeamento de TUDO (12/07/2026, pedido do dono): os DOIS
    canais que usam mapa (seru = PDV, lote = saida em lote) numa tabela
    unica, com estado, venda dos ultimos 14 dias, problemas da auditoria
    linha a linha e edicao completa (receita/produto/MP + fator). O SITE
    nao usa mapa (FK do PedidoOnlineItem) — so a config de loja de origem
    abaixo. Lojas Seru seguem na secao propria."""
    from app.services.auditoria_mapeamentos import (
        problemas_por_mapa,
        venda_seru_por_nome,
    )
    produtos_map = VendaMapa.query.filter(
        VendaMapa.canal.in_(('seru', 'lote'))).order_by(
        VendaMapa.canal,
        VendaMapa.ignorar.asc(),
        VendaMapa.confirmado_em.is_(None).desc(),  # pendentes no topo
        VendaMapa.nome_externo,
    ).all()
    venda_14d = venda_seru_por_nome(dias=14)
    problemas = problemas_por_mapa()
    lojas_map = SeruLojaMap.query.order_by(SeruLojaMap.seru_company_name).all()
    receitas = Receita.ativas().order_by(Receita.categoria, Receita.nome).all()
    produtos = Produto.query.filter_by(ativo=True).order_by(Produto.nome).all()
    from app.models import MateriaPrima
    mps = (MateriaPrima.query.filter(MateriaPrima.arquivada_em.is_(None))
           .order_by(MateriaPrima.nome).all())
    lojas = Loja.query.filter_by(ativa=True).order_by(Loja.nome).all()
    # Loja fisica de onde o SITE (loja propria/PedidoOnline) baixa estoque +
    # visibilidade (loja atual, se foi salva por ID ou caiu no padrao, e se ela
    # tem estoque cadastrado — senao a venda do site nao tem de onde baixar).
    from app.models import EstoqueLoja
    from app.services.loja_pagamento import loja_origem_site
    loja_site = loja_origem_site()
    loja_site_explicito = bool(AppConfig.get_int('loja_site_estoque_id'))
    loja_site_itens = (EstoqueLoja.query.filter_by(loja_id=loja_site.id).count()
                       if loja_site else 0)
    return render_template('pdv/mapeamentos.html',
                           produtos_map=produtos_map, lojas_map=lojas_map,
                           receitas=receitas, produtos=produtos, lojas=lojas,
                           loja_site=loja_site,
                           loja_site_explicito=loja_site_explicito,
                           loja_site_itens=loja_site_itens)


@pdv_bp.route('/config-site-loja', methods=['POST'])
@login_required
@admin_required
def config_site_loja():
    """Salva de qual loja fisica o SITE (loja propria/PedidoOnline) baixa estoque
    nas vendas de entrega/express. Retirada continua baixando da loja escolhida
    pelo cliente. Fixa por ID (sobrevive a renomear a loja)."""
    try:
        loja_id = int(request.form.get('loja_id') or 0)
    except (TypeError, ValueError):
        loja_id = 0
    loja = Loja.query.get(loja_id) if loja_id else None
    if not loja:
        flash('Selecione uma loja válida.', 'danger')
        return redirect(url_for('pdv.mapeamentos'))
    AppConfig.set('loja_site_estoque_id', loja.id)
    db.session.commit()
    flash('Vendas do site passam a baixar estoque de "%s".' % loja.nome, 'success')
    return redirect(url_for('pdv.mapeamentos'))


def _reprocesso_pos_mapeamento():
    """Mapeou produto / confirmou loja → agenda a recuperação das baixas
    PASSADAS (janela 7d) com os mapeamentos novos. Roda em BACKGROUND com
    coalescing (N vínculos seguidos = 1 reprocesso) — o síncrono no request
    travava a tela por minutos, 7 dias de API Seru por clique (08/07/2026).
    Best-effort: falha em agendar NÃO desfaz o vínculo (só avisa)."""
    from app.services import seru_sync
    try:
        seru_sync.agendar_reprocesso_retroativo(dias=7,
                                                user_id=current_user.id)
        flash('Recuperando baixas dos últimos 7 dias em segundo plano — '
              'o resultado aparece na Saúde do PDV em alguns minutos.', 'info')
    except Exception:
        current_app.logger.exception('agendar reprocesso pos-mapeamento')
        flash('Vínculo salvo, mas não consegui agendar o reprocesso '
              'retroativo. Rode pelo botão na Saúde do PDV.', 'warning')


@pdv_bp.route('/mapeamentos/produto/<int:map_id>', methods=['POST'])
@login_required
@admin_required
def vincular_produto(map_id):
    """Vincula/ignora/limpa um produto Seru."""
    from app.utils import parse_fator_composicao
    mp = VendaMapa.query.get_or_404(map_id)
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
        # Fator primeiro: invalido/<=0 NAO vira 1.0 em silencio (baixaria
        # estoque errado) — rejeita ANTES de mexer no vinculo.
        try:
            fator = parse_fator_composicao(request.form.get('fator'))
        except ValueError:
            flash('Fator invalido — use numero > 0 (ex: 0,2). Nada foi alterado.', 'danger')
            return redirect(url_for('pdv.mapeamentos'))
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
        mp.fator_quantidade = fator
        mp.confirmado_em = agora()
        mp.confirmado_por = current_user.id
        fator_msg = '' if fator == 1.0 else f' · fator {fator}'
        flash(f'"{mp.nome_externo}" → {mp.alvo_nome}{fator_msg}', 'success')
    elif acao == 'ignorar':
        mp.ignorar = True
        mp.receita_id = None
        mp.produto_id = None
        mp.confirmado_em = agora()
        mp.confirmado_por = current_user.id
        flash(f'"{mp.nome_externo}" ignorado — nao baixara estoque.', 'info')
    elif acao == 'desfazer':
        mp.ignorar = False
        mp.receita_id = None
        mp.produto_id = None
        mp.confirmado_em = None
        mp.confirmado_por = None
        mp.fator_quantidade = 1.0  # volta pra pristine — fator nao fica pegajoso
        flash(f'"{mp.nome_externo}" voltou pra pendente.', 'info')
    db.session.commit()
    if acao == 'vincular':
        _reprocesso_pos_mapeamento()   # recupera baixas passadas (7d)
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
        lm.confirmado_em = agora()
        lm.confirmado_por = current_user.id
        db.session.commit()
        flash(f'OK: "{lm.seru_company_name}" agora vinculada a {loja_obj.nome}. '
              f'Vendas dessa company vao baixar estoque dessa loja.', 'success')
        _reprocesso_pos_mapeamento()   # recupera pedidos sem-loja da janela
        return redirect(url_for('pdv.mapeamentos'))
    if acao == 'ignorar':
        lm.ignorar = True
        lm.loja_id = None
        lm.confirmado_em = agora()
        lm.confirmado_por = current_user.id
        db.session.commit()
        flash(f'"{lm.seru_company_name}" ignorada — vendas nao processarao.', 'info')
        return redirect(url_for('pdv.mapeamentos'))
    if acao == 'confirmar':
        lm.auto_match = False
        lm.confirmado_em = agora()
        lm.confirmado_por = current_user.id
        db.session.commit()
        flash(f'"{lm.seru_company_name}" confirmada.', 'success')
        _reprocesso_pos_mapeamento()   # os retidos "aguardando loja" entram já
        return redirect(url_for('pdv.mapeamentos'))
    flash(f'Acao desconhecida: "{acao}".', 'warning')
    return redirect(url_for('pdv.mapeamentos'))


# ════════════════════════════════════════════════════════════════════
# VNDA (site) — APOSENTADO 24/06/2026. A UI de mapeamento/sync/histórico do
# VNDA foi removida (sem entrada no menu desde o cutover; era duplicata morta
# do fluxo Seru). Serviços/modelos VNDA seguem dormentes (Camada B do
# CLAUDE.md, preservados por histórico). Resta só o diagnóstico de catálogo do
# bot (vnda_diag_produtos), que bate no VNDA pra debugar o catálogo do chatbot.
# ════════════════════════════════════════════════════════════════════


@pdv_bp.route('/vnda/diag-produtos')
@login_required
@admin_required
def vnda_diag_produtos():
    """Diagnostico do consultar_produtos do bot: bate no VNDA de verdade e
    mostra o resultado cru, pra confirmar (sem chutar) que o catalogo do bot
    responde certo em producao. Ex: /pdv/vnda/diag-produtos?q=family box"""
    from app.services import bot_tools, vnda
    q = (request.args.get('q') or 'family box').strip()

    # 1. Resultado do tool do bot, ja parseado (forca ida ao VNDA, sem cache).
    bot_tools._catalogo_cache.clear()
    try:
        resultado = bot_tools.consultar_produtos(q)
    except Exception as exc:  # noqa: BLE001
        resultado = {'erro_excecao': f'{type(exc).__name__}: {exc}'}

    # 2. Chamada crua via requests pra ver status code + corpo, mesmo em 4xx/5xx
    # (vnda._get engole esses casos e devolve None — opaco demais pra debug).
    import requests as _r
    raw_info = {}
    try:
        url = f'{vnda._base_url()}/products'
        r = _r.get(url, headers=vnda._headers(),
                   params={'available': 'true', 'per_page': 5}, timeout=10)
        raw_info['status_code'] = r.status_code
        raw_info['url_final'] = r.url
        try:
            data = r.json()
            lista = data if isinstance(data, list) else (
                data.get('products') or data.get('results') or [])
            raw_info['n_produtos'] = len(lista)
            if lista:
                p0 = lista[0]
                raw_info['exemplo_nome'] = p0.get('name') or p0.get('title')
                variants = p0.get('variants')
                raw_info['variants_tipo'] = type(variants).__name__
                if isinstance(variants, dict):
                    raw_info['variants_amostra'] = list(variants.values())[:1]
                elif isinstance(variants, list):
                    raw_info['variants_amostra'] = variants[:1]
            elif r.status_code >= 400:
                raw_info['corpo_resposta'] = (r.text or '')[:500]
        except ValueError:
            raw_info['corpo_resposta'] = (r.text or '')[:500]
    except Exception as exc:  # noqa: BLE001
        raw_info = {'erro': f'{type(exc).__name__}: {exc}'}

    # 3. Matriz de autenticacao: testa o NOSSO token com variacoes de formato
    # de Authorization e de X-Shop-Host pra achar empiricamente qual combo o
    # /products aceita (200). Nunca expoe o token — so o rotulo do formato e o
    # status. Tambem um controle no /orders (deve dar 200 com o nosso token).
    import time as _t
    token = (current_app.config.get('VNDA_API_TOKEN') or '').strip()
    if token.lower().startswith('bearer '):
        token = token[7:]
    host_cfg = (current_app.config.get('VNDA_SHOP_HOST')
                or 'www.padariaartesanalonline.com.br')
    host_alt = host_cfg[4:] if host_cfg.startswith('www.') else 'www.' + host_cfg
    base_h = {'Accept': 'application/json', 'User-Agent': 'OPaoPadaria/1.0'}
    combos = [
        (f'Bearer + X-Shop-Host={host_cfg}',
         {'Authorization': f'Bearer {token}', 'X-Shop-Host': host_cfg}),
        (f'Bearer + X-Shop-Host={host_alt}',
         {'Authorization': f'Bearer {token}', 'X-Shop-Host': host_alt}),
        ('Bearer SEM X-Shop-Host',
         {'Authorization': f'Bearer {token}'}),
        (f'token cru + X-Shop-Host={host_cfg}',
         {'Authorization': token, 'X-Shop-Host': host_cfg}),
        (f'Token token= + X-Shop-Host={host_cfg}',
         {'Authorization': f'Token token="{token}"', 'X-Shop-Host': host_cfg}),
    ]
    matriz = []
    for label, h in combos:
        try:
            rr = _r.get(f'{vnda._base_url()}/products', headers={**base_h, **h},
                        params={'per_page': 1}, timeout=10)
            matriz.append({'config': label, 'status': rr.status_code})
        except Exception as exc:  # noqa: BLE001
            matriz.append({'config': label, 'erro': type(exc).__name__})
        _t.sleep(0.4)
    try:
        rc = _r.get(f'{vnda._base_url()}/orders', headers=vnda._headers(),
                    params={'per_page': 1}, timeout=10)
        controle_orders = rc.status_code
    except Exception as exc:  # noqa: BLE001
        controle_orders = type(exc).__name__

    return jsonify({
        'busca': q,
        'token_vnda_configurado': bool(current_app.config.get('VNDA_API_TOKEN')),
        'vnda_produtos_token_setado': bool(vnda._produtos_token()),
        'x_shop_host_atual': host_cfg,
        'controle_orders_status': controle_orders,
        'auth_matrix': matriz,
        'consultar_produtos': resultado,
        'raw_products': raw_info,
    })


@pdv_bp.route('/debug-seru')
@login_required
@owner_required
def debug_seru():
    """Saude da integracao Seru (owner-only): testa AUTH + 1 REQUEST real e mostra
    o erro EXATO da API — pra diagnosticar quando a busca/sync falha ("aguardando
    primeira execucao", erro de rede na tela). Nunca vaza segredo (so presenca +
    tamanho). Somente leitura: nao muda nada, nao processa pedido."""
    import time as _time

    from app.utils import hoje

    cid = (current_app.config.get('SERU_CLIENT_ID') or '').strip()
    secret = (current_app.config.get('SERU_CLIENT_SECRET') or '').strip()
    out = {
        'config': {
            'client_id_set': bool(cid), 'client_id_len': len(cid),
            'client_secret_set': bool(secret), 'client_secret_len': len(secret),
            'base_url': getattr(seru, 'BASE', None),
        },
        'ultimo_sync': AppConfig.get('seru_ultimo_sync'),
        'auth': None,
        'request': None,
        'conclusao': None,
    }

    # 1. Autenticacao (client_credentials)
    t0 = _time.time()
    try:
        token = seru._obter_token(force_refresh=True)
        out['auth'] = {'ok': True, 'token_len': len(token or ''),
                       'ms': int((_time.time() - t0) * 1000)}
    except Exception as e:  # noqa: BLE001
        out['auth'] = {'ok': False, 'erro': str(e)[:400],
                       'ms': int((_time.time() - t0) * 1000)}
        out['conclusao'] = ('FALHA NA AUTENTICACAO. Cheque SERU_CLIENT_ID/'
                            'SERU_CLIENT_SECRET no Railway (ver auth.erro).')
        return jsonify(out)

    # 2. Um request real: pedidos de HOJE, 1 item (sem processar nada)
    hoje_d = hoje()
    t1 = _time.time()
    try:
        resp = seru.listar_pedidos(hoje_d, hoje_d, page=1, limit=1)
        data = resp.get('data') if isinstance(resp, dict) else None
        out['request'] = {
            'ok': True, 'ms': int((_time.time() - t1) * 1000),
            'total_pages': (resp or {}).get('totalPages'),
            'n_no_page': len(data or []),
            'dia_testado': hoje_d.isoformat(),
        }
        out['conclusao'] = ('API OK — auth e request funcionaram. Se a busca na '
                            'tela falha, o problema esta no navegador/webview '
                            '(sessao/JSON), NAO na API do Seru.')
    except Exception as e:  # noqa: BLE001
        out['request'] = {'ok': False, 'erro': str(e)[:400],
                          'ms': int((_time.time() - t1) * 1000),
                          'dia_testado': hoje_d.isoformat()}
        out['conclusao'] = ('Auth OK, mas o request de pedidos FALHOU — este e o '
                            'erro real da API (ver request.erro).')
    return jsonify(out)
