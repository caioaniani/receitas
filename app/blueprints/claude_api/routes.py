"""API read-only do ASSISTENTE (Claude Code) — /api/claude/*.

Criada em 02/07/2026 a pedido do dono ("pois vá ter acesso"): o ambiente de
desenvolvimento do Claude não enxerga o Postgres de produção, então estas
rotas expõem LEITURAS específicas (hoje: o cronograma de produção) via HTTPS
com token, pra ele responder perguntas como "qual pedido de produção enviar
amanhã?" com os números reais.

Segurança (mesmo padrão do BOT_API_TOKEN / segredos de webhook da casa):
- `Authorization: Bearer <CLAUDE_API_TOKEN>` (header; evita token em log de
  proxy). `?token=` também é aceito por conveniência.
- Sem CLAUDE_API_TOKEN configurado no env → 503 (rotas desligadas).
- Comparação com `secrets.compare_digest` (timing-safe).
- READ-ONLY estrito: nenhuma rota aqui escreve nada. Writes continuam
  exclusivos dos canais com aprovação humana (telas e copilot).
"""
import secrets as _secrets
from functools import wraps

from flask import current_app, jsonify, request

from app.blueprints.claude_api import claude_api_bp


def _claude_auth_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        token_cfg = (current_app.config.get('CLAUDE_API_TOKEN') or '').strip()
        if not token_cfg:
            return jsonify(ok=False,
                           erro='CLAUDE_API_TOKEN nao configurado'), 503
        auth = request.headers.get('Authorization', '')
        recebido = auth[7:].strip() if auth.lower().startswith('bearer ') else ''
        if not recebido:
            recebido = (request.args.get('token') or '').strip()
        if not recebido or not _secrets.compare_digest(recebido, token_cfg):
            return jsonify(ok=False, erro='token invalido'), 401
        return f(*args, **kwargs)
    return wrapper


def _int_arg(nome, default, lo, hi):
    try:
        return max(lo, min(int(request.args.get(nome, default)), hi))
    except (TypeError, ValueError):
        return default


@claude_api_bp.route('/cronograma')
@_claude_auth_required
def cronograma():
    """Cronograma de produção em JSON — a MESMA conta da tela
    /telaindustriateste (por dia, com overrides, lotes, MRP, caps de retorno,
    pendências do padeiro e alertas de entrega em risco). Campos pesados de
    UI (projeção dia a dia, breakdowns) ficam de fora do payload.

    Params: ?horizonte=7 (1-14), ?janela=6 (1-26), ?inicio=0 (0-14),
    ?motor=pedidos|vendas|maior (fonte da demanda prevista; default
    'vendas' desde 17/08/2026 — mesma régua da tela e da automação).
    """
    from app.services.previsao_producao import (
        MOTORES_PREVISAO_PRODUCAO,
        cronograma_producao,
    )
    from app.services.producao_pendente import pendencias_por_receita

    motor = (request.args.get('motor') or 'vendas').strip()
    if motor not in MOTORES_PREVISAO_PRODUCAO:
        motor = 'vendas'
    crono = cronograma_producao(
        horizonte_dias=_int_arg('horizonte', 7, 1, 14),
        janela_semanas=_int_arg('janela', 6, 1, 26),
        inicio_offset_dias=_int_arg('inicio', 0, 0, 14),
        equilibrar=request.args.get('equilibrar', '1') in ('1', 'true'),
        motor=motor)
    pend = pendencias_por_receita()

    receitas = []
    for rr in crono['receitas']:
        p = pend.get(rr['receita_id'])
        receitas.append({
            'receita_id': rr['receita_id'],
            'nome': rr['nome'],
            'categoria': rr.get('categoria') or '',
            'insumo': bool(rr.get('insumo')),
            'dias_producao': rr.get('dias_producao', 0),
            'em_estoque': rr.get('em_estoque', 0),
            'em_estoque_efetivo': rr.get('em_estoque_efetivo', 0),
            'consumo_janela': rr.get('consumo_janela'),
            'comprometido': rr.get('comprometido', 0),
            'previsto': rr.get('previsto', 0),
            'demanda': rr.get('demanda', 0),
            'produzir': rr.get('produzir', 0),
            'por_dia': rr.get('por_dia', []),
            'total': rr.get('total', 0),
            'editado': bool(rr.get('editado')),
            'pend_agendado': p['agendado'] if p else 0,
            'pend_vencido': p['vencido'] if p else 0,
            'entregas_risco': rr.get('entregas_risco', []),
            'limitado_por_retorno': bool(rr.get('limitado_por_retorno')),
            # Regra da véspera (10/07/2026): consumo iminente de insumo sem
            # estoque pronto — dict {faltam, coberto, lead, dias} ou None.
            'insumo_sem_vespera': rr.get('insumo_sem_vespera'),
            # Flag da ficha (19/07/2026): estoque físico não abate a
            # produção sugerida — só a produção já mandada conta.
            'estoque_nao_abate': bool(rr.get('estoque_nao_abate')),
        })
    return jsonify(
        ok=True,
        hoje=crono['hoje'],
        inicio=crono['inicio'],
        horizonte_dias=crono['horizonte_dias'],
        janela_semanas=crono['janela_semanas'],
        motor=crono.get('motor', 'vendas'),
        dias=crono['dias'],
        receitas=receitas,
        alertas_falta=crono.get('alertas_falta', []),
    )


@claude_api_bp.route('/loja-vendas-debug')
@_claude_auth_required
def loja_vendas_debug():
    """Diagnóstico "a venda desta loja está baixando o estoque?" — criado
    06/07/2026 (suspeita do dono sobre a Ribeiro do Vale). Cruza, POR DIA, o
    que o Seru REPORTOU de venda na loja (VendaSeruDiaria, snapshot da API)
    com o que BAIXOU no estoque dela (MovEstoqueLoja), e lista os produtos
    vendidos na janela com o estado do mapeamento — pendente/ignorado/sem_map
    NÃO baixam. Read-only estrito, como todo o blueprint.

    Params: ?loja=<nome fuzzy|id>, ?dias=7 (1-30).
    """
    from datetime import datetime as _dt
    from datetime import time as _time
    from datetime import timedelta

    from sqlalchemy import func

    from app.constants import VENDA_TIPOS_DEMANDA_COM_ESTORNO
    from app.extensions import db
    from app.models import (
        EstoqueLoja,
        Loja,
        MovEstoqueLoja,
        SeruLojaMap,
        VendaMapa,
        VendaSeruDiaria,
    )
    from app.services import vendas_diarias
    from app.utils import hoje, resolver_loja_por_nome

    try:
        dias_n = max(1, min(int(request.args.get('dias', 7)), 30))
    except (TypeError, ValueError):
        dias_n = 7
    hoje_d = hoje()
    ini = hoje_d - timedelta(days=dias_n - 1)

    bruto = (request.args.get('loja') or '').strip()
    if not bruto:
        # MODO GLOBAL: todos os company names que o Seru reportou na janela e
        # pra qual Loja cada um está vinculado — revela company vendendo SEM
        # vínculo (ou vinculado à loja errada), que a visão por loja não pega.
        nomes_loja = {x.id: x.nome for x in Loja.query.all()}
        mapas_all = {m.seru_company_name: m for m in SeruLojaMap.query.all()}
        companies = []
        for nome_c, qtd, n_dias in (db.session.query(
                VendaSeruDiaria.loja_seru,
                func.sum(VendaSeruDiaria.qtd),
                func.count(func.distinct(VendaSeruDiaria.data)))
                .filter(VendaSeruDiaria.data >= ini,
                        VendaSeruDiaria.data <= hoje_d)
                .group_by(VendaSeruDiaria.loja_seru)
                .order_by(func.sum(VendaSeruDiaria.qtd).desc()).all()):
            m = mapas_all.get(nome_c)
            companies.append({
                'seru_company': nome_c,
                'itens_vendidos': int(qtd or 0),
                'dias_com_venda': int(n_dias or 0),
                'loja': nomes_loja.get(m.loja_id) if m and m.loja_id else None,
                'confirmado': bool(m.confirmado_em) if m else False,
                'ignorar': bool(m.ignorar) if m else False,
                'sem_mapa': m is None,
            })
        return jsonify(ok=True, modo='global',
                       janela={'inicio': ini.isoformat(),
                               'fim': hoje_d.isoformat()},
                       companies=companies,
                       lojas=[x.nome for x in Loja.query
                              .filter_by(ativa=True).order_by(Loja.nome)])

    loja = None
    if bruto.isdigit():
        loja = db.session.get(Loja, int(bruto))
    if loja is None:
        loja = resolver_loja_por_nome(bruto)
    if loja is None:
        return jsonify(ok=False, erro='loja nao encontrada',
                       lojas=[x.nome for x in Loja.query
                              .filter_by(ativa=True).order_by(Loja.nome)]), 404

    # Snapshot das vendas Seru na janela (best-effort — API fora, usa o banco).
    captura_erro = None
    try:
        vendas_diarias.garantir_capturado(ini, hoje_d)
    except Exception as e:  # noqa: BLE001 — diagnostico segue com o banco
        captura_erro = f'{type(e).__name__}: {str(e)[:160]}'

    # Vinculo Seru<->Loja: sem `confirmado_em`, o sync NAO baixa nada da loja.
    mapas = SeruLojaMap.query.filter_by(loja_id=loja.id).all()
    mapa_loja = [{'seru_company': m.seru_company_name,
                  'confirmado_em': (m.confirmado_em.isoformat()
                                    if m.confirmado_em else None),
                  'ignorar': bool(m.ignorar),
                  'auto_match': bool(m.auto_match)} for m in mapas]
    nomes_seru = [m.seru_company_name for m in mapas]
    loja_confirmada = any(m.confirmado_em and not m.ignorar for m in mapas)

    conds = [VendaSeruDiaria.loja_id == loja.id]
    if nomes_seru:
        conds.append(VendaSeruDiaria.loja_seru.in_(nomes_seru))
    filtro_loja = db.or_(*conds)

    # Reportado pelo Seru, por dia.
    reportado = {str(d): int(q or 0) for d, q in (
        db.session.query(VendaSeruDiaria.data, func.sum(VendaSeruDiaria.qtd))
        .filter(VendaSeruDiaria.data >= ini, VendaSeruDiaria.data <= hoje_d,
                filtro_loja)
        .group_by(VendaSeruDiaria.data).all())}

    # Baixado no estoque DA LOJA, por dia e tipo (vendas + estornos de todos
    # os canais — a pergunta é "baixou?", não só Seru).
    baixas = {}
    for d_mov, tipo, q in (
            db.session.query(func.date(MovEstoqueLoja.data),
                             MovEstoqueLoja.tipo,
                             func.sum(MovEstoqueLoja.quantidade))
            .join(EstoqueLoja,
                  MovEstoqueLoja.estoque_loja_id == EstoqueLoja.id)
            .filter(EstoqueLoja.loja_id == loja.id,
                    MovEstoqueLoja.tipo.in_(VENDA_TIPOS_DEMANDA_COM_ESTORNO),
                    MovEstoqueLoja.data >= _dt.combine(ini, _time.min))
            .group_by(func.date(MovEstoqueLoja.data), MovEstoqueLoja.tipo)
            .all()):
        baixas.setdefault(str(d_mov)[:10], {})[tipo] = int(q or 0)

    dias_out = []
    for i in range(dias_n):
        d = ini + timedelta(days=i)
        iso = d.isoformat()
        b = baixas.get(iso, {})
        dias_out.append({
            'data': iso,
            'seru_reportado_itens': reportado.get(iso, 0),
            'baixas_por_tipo': b,
            'baixado_total': sum(b.values()),
        })

    # Produtos vendidos na janela + estado do mapeamento (o que explica gap).
    prod_rows = (db.session.query(VendaSeruDiaria.seru_nome,
                                  func.sum(VendaSeruDiaria.qtd))
                 .filter(VendaSeruDiaria.data >= ini,
                         VendaSeruDiaria.data <= hoje_d, filtro_loja)
                 .group_by(VendaSeruDiaria.seru_nome)
                 .order_by(func.sum(VendaSeruDiaria.qtd).desc())
                 .limit(60).all())
    vmapas = {vm.nome_externo: vm for vm in (
        VendaMapa.query.filter_by(canal='seru')
        .filter(VendaMapa.nome_externo.in_([n for n, _ in prod_rows] or ['']))
        .all())}
    produtos = []
    for nome_p, qtd in prod_rows:
        vm = vmapas.get(nome_p)
        if vm is None:
            estado = 'sem_map'      # sync nunca viu — não baixa
        elif vm.ignorar:
            estado = 'ignorado'     # decisão explícita — não baixa
        elif vm.receita_id or vm.produto_id or vm.materia_prima_id:
            estado = 'mapeado'
        else:
            estado = 'pendente'     # fila de revisão — não baixa
        produtos.append({
            'seru_nome': nome_p,
            'qtd_vendida': int(qtd or 0),
            'estado_map': estado,
            'receita_id': vm.receita_id if vm else None,
            'produto_id': vm.produto_id if vm else None,
            'fator': (float(vm.fator_quantidade)
                      if vm and vm.fator_quantidade is not None else None),
        })
    nao_baixam = sum(p['qtd_vendida'] for p in produtos
                     if p['estado_map'] != 'mapeado')

    return jsonify(
        ok=True,
        loja={'id': loja.id, 'nome': loja.nome},
        loja_confirmada_no_seru=loja_confirmada,
        mapa_loja=mapa_loja,
        janela={'inicio': ini.isoformat(), 'fim': hoje_d.isoformat()},
        captura_erro=captura_erro,
        dias=dias_out,
        produtos=produtos,
        itens_vendidos_sem_baixa_por_mapa=nao_baixam,
    )


@claude_api_bp.route('/vendas-snapshot')
@_claude_auth_required
def vendas_snapshot():
    """Faturamento POR DIA do snapshot `VendaSeruDiaLoja` — criado 18/07/2026
    pro caso "card Por loja do /pdv/ mostra Nebraska R$10.355 mas foi
    R$3.327": expõe exatamente o que o card soma (`faturamento_pedidos`) por
    (dia, company), pra auditar de fora se um número da tela é soma de
    período ou linha errada. NÃO dispara captura (o estado cru é a
    evidência). Read-only estrito, como todo o blueprint.

    Params: ?dias=5 (1-30, janela terminando hoje), ?loja= (substring
    case-insensitive do company name) e ?pedidos=1 (adiciona a lista AO VIVO
    dos pedidos da janela — id, total, soma dos itens, canal — pra achar
    pedido com `total` divergente dos itens; janela capada em 3 dias
    nesse modo pra não estourar a API).
    """
    from datetime import timedelta

    from app.models import VendaSeruDiaLoja
    from app.utils import hoje

    # ?detalhe=<pedido_id>: payload CRU de UM pedido (GET /orders/{id} da
    # Seru) — criado 18/07/2026 pra descobrir se pedido de delivery (99Food)
    # traz os itens no DETALHE mesmo vindo vazio na listagem (decide se da
    # pra dar baixa de estoque nesses pedidos). Read-only.
    detalhe_id = (request.args.get('detalhe') or '').strip()
    if detalhe_id:
        from app.services import seru
        try:
            return jsonify(ok=True, pedido=seru.detalhes_pedido(detalhe_id))
        except Exception as e:  # noqa: BLE001 — sonda mostra o erro cru
            return jsonify(ok=False,
                           erro=f'{type(e).__name__}: {str(e)[:200]}'), 502

    dias_n = _int_arg('dias', 5, 1, 30)
    com_pedidos = bool(request.args.get('pedidos'))
    if com_pedidos:
        dias_n = min(dias_n, 3)
    hoje_d = hoje()
    ini = hoje_d - timedelta(days=dias_n - 1)
    filtro = (request.args.get('loja') or '').strip().lower()

    linhas = []
    por_loja_total = {}
    q = (VendaSeruDiaLoja.query
         .filter(VendaSeruDiaLoja.data >= ini,
                 VendaSeruDiaLoja.data <= hoje_d)
         .order_by(VendaSeruDiaLoja.data, VendaSeruDiaLoja.loja_seru))
    for r in q.all():
        if filtro and filtro not in (r.loja_seru or '').lower():
            continue
        fat_ped = float(r.faturamento_pedidos or 0)
        linhas.append({
            'data': r.data.isoformat(),
            'loja_seru': r.loja_seru,
            'n_pedidos': int(r.n_pedidos or 0),
            'faturamento_itens': float(r.faturamento or 0),
            'faturamento_pedidos': fat_ped,
            'atualizado_em': (r.atualizado_em.isoformat()
                              if r.atualizado_em else None),
        })
        por_loja_total[r.loja_seru] = round(
            por_loja_total.get(r.loja_seru, 0) + fat_ped, 2)
    pedidos_vivo = None
    if com_pedidos:
        from decimal import Decimal

        from app.services import seru
        pedidos_vivo = []
        try:
            for p in seru.listar_pedidos_completo(ini, hoje_d):
                if not isinstance(p, dict):
                    continue
                comp = ((p.get('company') or {}).get('name') or '(sem loja)')
                if filtro and filtro not in comp.lower():
                    continue
                soma_itens = Decimal('0')
                n_itens = 0
                for it in seru.extrair_itens(p):
                    if not it['cancelado']:
                        soma_itens += Decimal(str(it['total']))
                        n_itens += 1
                total = float(p.get('total') or 0)
                dh = seru.datahora_local(p.get('createdAt'))
                # NF: `taxInvoice` da API — None = venda SEM nota emitida.
                nf_raw = p.get('taxInvoice') or None
                nf = None
                if isinstance(nf_raw, dict):
                    nf = {'status': nf_raw.get('status'),
                          'numero': nf_raw.get('number'),
                          'serie': nf_raw.get('serialNumber'),
                          'emitida_em': nf_raw.get('receiptDate')
                          or nf_raw.get('createdAt'),
                          'url': nf_raw.get('url')}
                pags = [(x.get('method') or x.get('type'))
                        for x in (p.get('payments') or [])
                        if isinstance(x, dict)]
                pedidos_vivo.append({
                    'id': p.get('id') or p.get('orderNumber') or p.get('code'),
                    'codigo': p.get('code'),
                    'data': dh.date().isoformat() if dh else '?',
                    'hora': dh.strftime('%H:%M:%S') if dh else '?',
                    'company': comp,
                    'total': total,
                    'subtotal': float(p.get('subtotal') or 0),
                    'desconto': float(p.get('discount') or 0),
                    'soma_itens': float(soma_itens),
                    'diferenca': round(total - float(soma_itens), 2),
                    'n_itens': n_itens,
                    'canal': p.get('salesChannel'),
                    'cancelado': seru.pedido_cancelado(p),
                    'status': p.get('status'),
                    'caixa': (p.get('cashier') or {}).get('code'),
                    'nf': nf,
                    'pagamentos': pags,
                    'obs': p.get('note'),
                })
            pedidos_vivo.sort(key=lambda x: -abs(x['diferenca']))
            pedidos_vivo = pedidos_vivo[:80]
        except Exception as e:  # noqa: BLE001 — sonda segue com o snapshot
            pedidos_vivo = [{'erro': f'{type(e).__name__}: {str(e)[:160]}'}]
    return jsonify(ok=True,
                   janela={'inicio': ini.isoformat(),
                           'fim': hoje_d.isoformat()},
                   linhas=linhas,
                   soma_por_loja_na_janela=por_loja_total,
                   pedidos_ao_vivo=pedidos_vivo,
                   nota='faturamento_pedidos = o que o card "Por loja (PDV)" '
                        'soma no período selecionado na tela')


@claude_api_bp.route('/seru-companies')
@_claude_auth_required
def seru_companies():
    """Companies CRUS da API do Seru (id + name + volume), dos pedidos dos
    últimos ?dias=2. Criado 07/07/2026 pra destrinchar o incidente do renome
    das lojas no Seru (Ribeiro sem baixa desde ~22/06): mostra se a API
    expõe `company.id` (âncora estável pra sobreviver a renome) e quantos
    companies distintos existem HOJE. Read-only; bate na API ao vivo."""
    from collections import defaultdict
    from datetime import timedelta

    from app.services import seru
    from app.utils import hoje

    try:
        dias_n = max(1, min(int(request.args.get('dias', 2)), 7))
    except (TypeError, ValueError):
        dias_n = 2
    hoje_d = hoje()
    # Janela HISTORICA opcional (?inicio=&fim=, max 7 dias): pra ler id/CNPJ
    # de company que ja MORREU (ex: OPAO PADARIA, morto em ~21/06) — pedido
    # do dono 07/07/2026, "puxando o faturamento da epoca a gente ve".
    ini_d, fim_d = hoje_d - timedelta(days=dias_n - 1), hoje_d
    di, df = request.args.get('inicio'), request.args.get('fim')
    if di and df:
        from datetime import date as _date
        try:
            ini_d, fim_d = _date.fromisoformat(di), _date.fromisoformat(df)
        except ValueError:
            return jsonify(ok=False, erro='inicio/fim invalidos (ISO)'), 400
        if not (0 <= (fim_d - ini_d).days <= 7):
            return jsonify(ok=False, erro='janela max de 7 dias'), 400
    try:
        pedidos = seru.listar_pedidos_completo(ini_d, fim_d)
    except Exception as e:  # noqa: BLE001 — diagnóstico devolve o erro cru
        return jsonify(ok=False, erro=f'{type(e).__name__}: {str(e)[:300]}'), 502

    agg = {}
    exemplo_company = None
    por_dia = defaultdict(lambda: defaultdict(int))
    docs = defaultdict(set)
    estrutura = None
    for p in pedidos or []:
        c = p.get('company') or {}
        if isinstance(c, dict):
            cid = c.get('id')
            cname = (c.get('name') or '').strip()
            if c.get('document'):
                docs[cid].add(str(c['document']))
            if exemplo_company is None and c:
                exemplo_company = {k: c.get(k) for k in list(c.keys())[:12]}
        else:
            cid, cname = None, str(c).strip()
        chave = (cid, cname)
        agg.setdefault(chave, 0)
        agg[chave] += 1
        criado = (p.get('createdAt') or '')[:10]
        if criado:
            por_dia[f'{cid}|{cname}'][criado] += 1
        # Estrutura do PEDIDO (sem valores — nada de PII): chaves do topo +
        # sub-chaves de objetos candidatos a discriminar a LOJA física caso
        # duas lojas dividam o mesmo company.
        if estrutura is None and request.args.get('estrutura'):
            estrutura = {'chaves': sorted(p.keys())}
            for k, v in p.items():
                if isinstance(v, dict) and k not in ('customer', 'client',
                                                     'buyer', 'address'):
                    estrutura[f'sub:{k}'] = sorted(v.keys())
    companies = [{'id': cid, 'name': cname, 'n_pedidos': n,
                  'documents': sorted(docs.get(cid, [])),
                  'pedidos_por_dia': dict(por_dia.get(f'{cid}|{cname}', {}))}
                 for (cid, cname), n in sorted(agg.items(),
                                               key=lambda kv: -kv[1])]
    return jsonify(ok=True, dias=dias_n,
                   janela={'inicio': ini_d.isoformat(),
                           'fim': fim_d.isoformat()},
                   total_pedidos=len(pedidos or []),
                   companies=companies, exemplo_company=exemplo_company,
                   estrutura_pedido=estrutura)


@claude_api_bp.route('/receita')
@_claude_auth_required
def receita():
    """Ficha completa de uma receita em JSON — cadastro (rendimento/pesos/
    lotes/preços), ingredientes, mapeamentos de venda (VendaMapa), cestas
    que a contêm (ProdutoItem) e estoques atuais (indústria + por loja).
    Serve pra conferir cadastro sem acesso direto ao Postgres.

    Params: ?id=<receita_id> OU ?nome=<trecho> (case-insensitive). Trecho
    com mais de um match devolve só a lista de candidatos (id + nome) pra
    refinar. Inclui arquivadas (cadastro e histórico continuam legíveis).
    """
    from sqlalchemy import func

    from app.models import (
        EstoqueLoja,
        EstoqueProducao,
        Produto,
        ProdutoItem,
        Receita,
        VendaMapa,
    )

    rid = (request.args.get('id') or '').strip()
    nome = (request.args.get('nome') or '').strip()
    if rid:
        try:
            recs = [r for r in [Receita.query.get(int(rid))] if r]
        except (TypeError, ValueError):
            return jsonify(ok=False, erro='id invalido'), 400
    elif nome:
        recs = (Receita.query
                .filter(func.lower(Receita.nome).contains(nome.lower()))
                .order_by(Receita.nome).all())
    else:
        return jsonify(ok=False, erro='informe ?id= ou ?nome='), 400

    if not recs:
        return jsonify(ok=False, erro='receita nao encontrada'), 404
    if len(recs) > 1:
        return jsonify(ok=True, multiplos=True, candidatos=[
            {'id': r.id, 'nome': r.nome,
             'arquivada': r.arquivada_em is not None} for r in recs])

    rec = recs[0]
    est_industria = [
        {'quantidade': int(ep.quantidade or 0),
         'nome_pendente': ep.nome_pendente}
        for ep in EstoqueProducao.query.filter_by(receita_id=rec.id).all()]
    est_lojas = [
        {'loja': el.loja.nome if el.loja else el.loja_id,
         'estado': el.estado,
         'quantidade': int(el.quantidade or 0),
         'reservada': int(el.quantidade_reservada or 0)}
        for el in EstoqueLoja.query.filter_by(receita_id=rec.id).all()]
    mapas = [
        {'canal': m.canal, 'nome_externo': m.nome_externo,
         'fator_quantidade': m.fator_quantidade, 'ignorar': m.ignorar,
         'confirmado': m.confirmado_em is not None}
        for m in VendaMapa.query.filter_by(receita_id=rec.id).all()]
    cestas = []
    for pi in ProdutoItem.query.filter_by(receita_id=rec.id).all():
        prod = Produto.query.get(pi.produto_id)
        cestas.append({'produto_id': pi.produto_id,
                       'produto': prod.nome if prod else pi.produto_id,
                       'quantidade': pi.quantidade})

    return jsonify(ok=True, receita={
        'id': rec.id,
        'nome': rec.nome,
        'categoria': rec.categoria or '',
        'familia': rec.familia,
        'arquivada_em': (rec.arquivada_em.isoformat()
                         if rec.arquivada_em else None),
        'rendimento_qtd': rec.rendimento_qtd,
        'rendimento_unidade': rec.rendimento_unidade,
        'peso_base': rec.peso_base,
        'peso_unitario': rec.peso_unitario,
        'perda_percentual': rec.perda_percentual or 0,
        'custo_embalagem': rec.custo_embalagem or 0,
        'dias_producao': rec.dias_producao or 0,
        'capacidade_amassadeira_g': rec.capacidade_amassadeira_g,
        'estado_padrao': rec.estado_padrao,
        'lote_pedido': rec.lote_pedido,
        'minimo_pedido': rec.minimo_pedido,
        'lote_producao': rec.lote_producao,
        'fornada_especial': rec.fornada_especial,
        'sugerir_pedido_loja': rec.sugerir_pedido_loja,
        'reaproveitavel': rec.reaproveitavel,
        'estoque_nao_abate': rec.estoque_nao_abate,
        'sob_encomenda': rec.sob_encomenda,
        'antecedencia_max_dias': rec.antecedencia_max_dias,
        'cobra_sobra_diaria': rec.cobra_sobra_diaria,
        'descricao_atacado': rec.descricao_atacado,
        'retorno_receita': ({'id': rec.retorno_receita.id,
                             'nome': rec.retorno_receita.nome}
                            if rec.retorno_receita else None),
        'precos': {'venda': rec.preco_venda, 'loja': rec.preco_loja,
                   'site': rec.preco_site, 'interno': rec.preco_interno},
        'ingredientes': [
            {'tipo': i.tipo or 'mp', 'nome': i.ingrediente_nome,
             'porcentagem_ou_qtd': i.porcentagem, 'eh_base': i.eh_base,
             'sub_receita_id': i.sub_receita_id, 'nota': i.nota or ''}
            for i in rec.ingredientes],
        'estoque_industria': est_industria,
        'estoque_lojas': est_lojas,
        'mapeamentos_venda': mapas,
        'em_cestas': cestas,
    })


@claude_api_bp.route('/pedidos-semana')
@_claude_auth_required
def pedidos_semana():
    """Sugestão de pedido loja→indústria em JSON — a MESMA conta das telas
    'Pedidos da semana'. Read-only: NÃO cria pedido nenhum.

    Params: ?modo=venda (default; venda+estoque, ponto de reposição) |
    ?modo=media (média do histórico de pedidos); ?horizonte=7 (1-14),
    ?janela=6 (1-26), ?inicio=1 (0-14; default amanhã, igual às telas),
    ?seguranca=0 (0-100, só no modo venda).

    Produtos sem sugestão e sem pedido já feito ficam fora do payload.
    """
    from app.services.previsao_producao import (
        media_semanal_pedidos,
        sugerir_pedidos_por_venda,
    )

    modo = request.args.get('modo', 'venda')
    kw = dict(horizonte_dias=_int_arg('horizonte', 7, 1, 14),
              janela_semanas=_int_arg('janela', 6, 1, 26),
              inicio_offset_dias=_int_arg('inicio', 1, 0, 14))
    if modo == 'media':
        grade = media_semanal_pedidos(**kw)
    else:
        modo = 'venda'
        grade = sugerir_pedidos_por_venda(
            seguranca_pct=_int_arg('seguranca', 0, 0, 100), **kw)

    lojas = []
    for lj in grade['lojas']:
        produtos = [p for p in lj['produtos']
                    if sum(p['por_dia']) > 0 or any(p.get('ja_pedido') or [])]
        lojas.append({
            'loja_id': lj['loja_id'], 'loja_nome': lj['loja_nome'],
            'ja_tem': lj.get('ja_tem', []),
            'editaveis': lj.get('editaveis', []),
            'produtos': produtos,
        })
    return jsonify(
        ok=True,
        modo=modo,
        hoje=grade['hoje'],
        inicio=grade['inicio'],
        horizonte_dias=grade['horizonte_dias'],
        janela_semanas=grade['janela_semanas'],
        dias=grade['dias'],
        lojas=lojas,
    )


@claude_api_bp.route('/frete-debug')
@_claude_auth_required
def frete_debug():
    """Diagnóstico da geocodificação do frete (read-only).

    ?q=<endereço ou CEP> — roda CADA etapa da cadeia do `geocodificar`
    separadamente (BrasilAPI pelo CEP, Nominatim pelo texto completo e pelo
    simplificado) e devolve lat/lng/distância de cada uma + o resultado do
    `consultar_frete` oficial. Criada em 05/07/2026 pra investigar frete
    errado no checkout (Rua Nova York a "19,3 km"; CEP 01050-000 bloqueado
    como fora da área — casos reais do dia). Não grava nada.
    """
    from app.services import frete as frete_svc

    q = (request.args.get('q') or '').strip()
    if not q:
        return jsonify(ok=False, erro='use ?q=<endereço ou CEP>'), 400

    def _etapa(geo):
        if not geo:
            return None
        lat, lng, rotulo = geo[:3]   # _geocodificar_cep devolve 4 (com ref)
        if lat is None:
            return {'rotulo': rotulo, 'coords': None}
        return {'rotulo': rotulo, 'lat': lat, 'lng': lng,
                'distancia_km': round(frete_svc.distancia_km(lat, lng), 2)}

    cep = frete_svc._extrair_cep(q)
    etapas = {
        'cep_extraido': cep,
        'brasilapi_cep': _etapa(frete_svc._geocodificar_cep(cep)) if cep else None,
        'nominatim_texto': _etapa(frete_svc._geocodificar_texto(q)),
    }
    simples = frete_svc.simplificar_endereco(q)
    etapas['simplificado'] = simples
    if simples and simples.lower() != q.lower():
        etapas['nominatim_simplificado'] = _etapa(
            frete_svc._geocodificar_texto(simples))
    return jsonify(ok=True, consulta=q, etapas=etapas,
                   oficial=frete_svc.consultar_frete(q))


@claude_api_bp.route('/pedidos-dia')
@_claude_auth_required
def pedidos_dia():
    """Pedidos loja→indústria de UMA data de entrega (read-only) — TODOS os
    status, inclusive cancelado (a lista de /pedidos fatia por aba e um pedido
    em status inesperado 'some' da vista; esta sonda mostra tudo).

    Criada em 08/07/2026 pra diagnosticar "cadê o pedido da Anesio pra
    amanhã?" — a grade da média via um pedido não-editável que nenhuma aba
    de /pedidos mostrava.

    Params: ?data=YYYY-MM-DD (default amanhã, BRT) e ?loja=<trecho do nome>
    (opcional; fuzzy via resolver_loja_por_nome).
    """
    from datetime import date, timedelta

    from app.models import Loja, PedidoLoja, Usuario
    from app.utils import hoje, resolver_loja_por_nome

    data_s = (request.args.get('data') or '').strip()
    if data_s:
        try:
            data_ent = date.fromisoformat(data_s)
        except ValueError:
            return jsonify(ok=False, erro='data invalida (YYYY-MM-DD)'), 400
    else:
        data_ent = hoje() + timedelta(days=1)

    q = PedidoLoja.query.filter(PedidoLoja.data_entrega == data_ent)
    loja_arg = (request.args.get('loja') or '').strip()
    if loja_arg:
        loja = resolver_loja_por_nome(loja_arg)
        if loja is None:
            return jsonify(ok=False, erro=f'loja nao encontrada: {loja_arg!r}'), 404
        q = q.filter(PedidoLoja.loja_id == loja.id)

    def _nome_usuario(uid):
        if not uid:
            return None
        u = Usuario.query.get(uid)
        return u.nome if u else f'#{uid}'

    pedidos = []
    for p in q.order_by(PedidoLoja.id).all():
        loja_p = Loja.query.get(p.loja_id)
        pedidos.append({
            'id': p.id,
            'loja': loja_p.nome if loja_p else f'#{p.loja_id}',
            'status': p.status,
            'data_pedido': p.data_pedido.isoformat() if p.data_pedido else None,
            'criado_em': (p.criado_em.strftime('%Y-%m-%d %H:%M')
                          if getattr(p, 'criado_em', None) else None),
            'criado_por': _nome_usuario(getattr(p, 'criado_por', None)),
            'modificado_em': (p.modificado_em.strftime('%Y-%m-%d %H:%M')
                              if getattr(p, 'modificado_em', None) else None),
            'modificado_por': _nome_usuario(
                getattr(p, 'modificado_por_id', None)),
            'observacao': p.observacao or '',
            'itens': [{'nome': (it.receita.nome if it.receita_id and it.receita
                                else it.materia_prima.nome
                                if it.materia_prima_id and it.materia_prima
                                else '?'),
                       'qtd': it.quantidade}
                      for it in p.itens],
        })
    return jsonify(ok=True, data=data_ent.isoformat(), total=len(pedidos),
                   pedidos=pedidos)


@claude_api_bp.route('/tiny-danfe-debug')
@_claude_auth_required
def tiny_danfe_debug():
    """SONDA read-only do DANFE no Tiny/Olist (10/07/2026): mostra a resposta
    crua do link e, se o download não vier em PDF, a ESTRUTURA da página do
    visualizador do Olist (candidatos de PDF + trecho do HTML) — pra eu saber
    como extrair o PDF embutido. Uso: ?id=<id_da_nota_no_tiny>.

    Não escreve nada — só lê o Tiny e a página pública do documento."""
    import requests

    from app.services import tiny, tiny_nf
    nota_id = (request.args.get('id') or '').strip()
    if not nota_id:
        return jsonify(ok=False, erro='passe ?id=<id_da_nota>'), 400
    out = {'ok': True, 'nota_id': nota_id, 'tiny_disponivel': tiny.disponivel()}
    retorno = tiny._get('nota.fiscal.obter.link.php',
                        params={'id': nota_id}, retornar_erro=True)
    if isinstance(retorno, dict):
        out['retorno_status'] = retorno.get('status')
        out['campos_link'] = {k: retorno.get(k) for k in
                              ('link_danfe', 'link_pdf', 'link_nfe', 'link')
                              if retorno.get(k)}
        out['erros'] = tiny._extrair_erros(retorno) or None
    else:
        out['motivo_falha'] = tiny._consumir_falha()
    link, motivo = tiny.obter_link_nota_fiscal_com_motivo(nota_id)
    out['link_resolvido'] = link
    out['motivo'] = motivo
    if link:
        pdf, motivo_pdf = tiny_nf.baixar_danfe_pdf_com_motivo(nota_id)
        out['pdf_ok'] = bool(pdf)
        out['pdf_motivo'] = motivo_pdf
        out['pdf_tamanho'] = len(pdf) if pdf else 0
        if not pdf:
            # O Olist renderiza o DANFE como HTML (doc.view). Testa variações
            # candidatas pra descobrir se existe um PDF nativo (param/rota).
            ua = {'User-Agent': tiny_nf._UA_NAVEGADOR}
            base = link
            sep = '&' if '?' in base else '?'
            candidatos = [
                base + sep + 'saida=pdf',
                base + sep + 'formato=pdf',
                base + sep + 'pdf=1',
                base + sep + 'output=pdf',
                base + sep + 'tipo=pdf',
                base + sep + 'imprimir=1',
                base.replace('/doc.view', '/doc.pdf'),
                base.replace('doc.view?id=', 'nfe.danfe.pdf?id='),
            ]
            testes = []
            for u in candidatos:
                try:
                    rr = requests.get(u, timeout=20, headers=ua)
                    ct = (rr.headers.get('Content-Type') or '')
                    testes.append({'url': u, 'status': rr.status_code,
                                   'ctype': ct,
                                   'eh_pdf': 'pdf' in ct.lower(),
                                   'tam': len(rr.content or b'')})
                except requests.RequestException as exc:
                    testes.append({'url': u, 'erro': str(exc)})
            out['candidatos_pdf_nativo'] = testes
            # HTML completo do DANFE (pra eu testar conversao HTML->PDF).
            try:
                rr = requests.get(base, timeout=20, headers=ua)
                out['html_completo'] = rr.text or ''
            except requests.RequestException as exc:
                out['html_completo_erro'] = str(exc)
            # Accept: application/pdf na URL base (content negotiation).
            try:
                rr = requests.get(base, timeout=20,
                                  headers={**ua, 'Accept': 'application/pdf'})
                out['accept_pdf'] = {
                    'status': rr.status_code,
                    'ctype': rr.headers.get('Content-Type'),
                    'eh_pdf': 'pdf' in (rr.headers.get('Content-Type')
                                        or '').lower()}
            except requests.RequestException as exc:
                out['accept_pdf'] = {'erro': str(exc)}
    return jsonify(out)


@claude_api_bp.route('/db-tamanho')
@_claude_auth_required
def db_tamanho():
    """Tamanho do banco POR TABELA (17/07/2026, volume Railway a 75%):
    heap/TOAST/índices + tuplas mortas, pra saber O QUE ocupa o disco antes
    de decidir entre VACUUM FULL, retenção ou resize. Read-only estrito
    (só catálogos pg_*). Suspeitos históricos: TOAST morto da migração de
    BLOBs (M6) e tabelas de log/snapshot que crescem por dia."""
    from sqlalchemy import text

    from app.extensions import db
    if db.engine.dialect.name != 'postgresql':
        return jsonify(ok=False, erro='disponivel so em Postgres'), 400
    total = db.session.execute(text(
        'SELECT pg_database_size(current_database())')).scalar()
    dead = {r[0]: int(r[1] or 0) for r in db.session.execute(text(
        'SELECT relname, n_dead_tup FROM pg_stat_user_tables')).all()}
    tabelas = []
    for nome, tot, heap, toast, idx, linhas in db.session.execute(text("""
            SELECT c.relname,
                   pg_total_relation_size(c.oid),
                   pg_relation_size(c.oid),
                   COALESCE(pg_total_relation_size(c.reltoastrelid), 0),
                   pg_indexes_size(c.oid),
                   c.reltuples::bigint
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind = 'r'
            ORDER BY pg_total_relation_size(c.oid) DESC
            LIMIT 40
    """)).all():
        tabelas.append({
            'tabela': nome,
            'total_mb': round(tot / 1048576, 1),
            'heap_mb': round(heap / 1048576, 1),
            'toast_mb': round(toast / 1048576, 1),
            'indices_mb': round(idx / 1048576, 1),
            'linhas_estimadas': int(linhas or 0),
            'tuplas_mortas': dead.get(nome, 0),
        })
    return jsonify(ok=True,
                   banco_total_mb=round(total / 1048576, 1),
                   tabelas=tabelas)


@claude_api_bp.route('/deploy')
@_claude_auth_required
def deploy_info():
    """Qual commit esta NO AR (11/07/2026): o procedimento de 2 commits de
    schema exige confirmar que o ALTER deployou antes de subir o modelo —
    antes disso o assistente precisava pedir ao dono que conferisse o
    Railway. O Railway injeta RAILWAY_GIT_COMMIT_SHA no build. Read-only.

    `?colunas=tabela.coluna,outra.coluna` (05/08/2026) fecha o buraco que
    sobrava no procedimento: o commit estar no ar NAO prova que o ALTER
    pegou — `_try` engole a falha com WARNING no log, e o assistente nao ve
    log. Sem esta checagem, subir o modelo com a coluna faltando derruba
    TODA tela que le a tabela (incidente de 22/05/2026). Consulta o
    information_schema; nao le dado nenhum das linhas.
    """
    import os

    from sqlalchemy import text

    from app.extensions import db
    from app.services import instancia as _inst

    pode_enviar, motivo_inst = _inst.status()
    out = {
        'ok': True,
        'commit': os.environ.get('RAILWAY_GIT_COMMIT_SHA'),
        'branch': os.environ.get('RAILWAY_GIT_BRANCH'),
        'deployment_id': os.environ.get('RAILWAY_DEPLOYMENT_ID'),
        # Guarda de instancia canonica (20/08/2026): confirma de FORA que a
        # producao NAO foi silenciada por engano — um branch renomeado
        # calaria os alertas em silencio, que e a pior falha possivel aqui.
        'alertas': {'pode_enviar': pode_enviar, 'motivo': motivo_inst,
                    'branch_producao': _inst.BRANCH_PRODUCAO},
    }
    pedidas = [c.strip() for c in
               (request.args.get('colunas') or '').split(',') if c.strip()]
    if pedidas:
        achadas = {}
        for item in pedidas[:20]:
            tabela, _, coluna = item.partition('.')
            if not tabela or not coluna:
                achadas[item] = 'formato invalido (use tabela.coluna)'
                continue
            try:
                with db.engine.connect() as c:
                    r = c.execute(text(
                        'SELECT 1 FROM information_schema.columns '
                        'WHERE table_name = :t AND column_name = :c'),
                        {'t': tabela.lower(), 'c': coluna.lower()}).first()
                achadas[item] = bool(r)
            except Exception as exc:                          # noqa: BLE001
                achadas[item] = f'erro: {type(exc).__name__}'
        out['colunas'] = achadas
        out['todas_presentes'] = all(v is True for v in achadas.values())
    # ?seeds=1 (17/08/2026, caso "seed das danishes nao pegou"): expoe os
    # MARKERS de one-shot (AppConfig com prefixo de seed/retro) — "o seed
    # rodou?" responde-se de fora, sem log do Railway. Read-only; so chaves
    # de marker, nunca dado de negocio.
    if request.args.get('seeds'):
        from app.models import AppConfig
        prefixos = ('seed_', 'ordens_semana_retro', 'checklist_seed',
                    'acerto_')
        try:
            out['seeds'] = {
                row.key: row.value
                for row in AppConfig.query.all()
                if any(row.key.startswith(p) for p in prefixos)
            }
        except Exception as exc:                              # noqa: BLE001
            out['seeds'] = f'erro: {type(exc).__name__}'
    return jsonify(out)


@claude_api_bp.route('/projetos')
@_claude_auth_required
def projetos():
    """Projetos e tarefas da tela /projetos em JSON (16/07/2026) — o dono
    usa o quadro pra planejar ("traz o v2 aqui") e o assistente precisa ler
    o conteúdo sem acesso ao Postgres. Read-only estrito.

    Params: ?id=<projeto_id> OU ?nome=<trecho> (case-insensitive; mais de um
    match devolve só a lista de candidatos) OU nada (resumo de todos, com
    contagem de tarefas). Match único vem completo, com as tarefas na ordem
    do quadro.
    """
    from sqlalchemy import func

    from app.models import Projeto

    def _resumo(p):
        ativas = p.tarefas_ativas
        return {
            'id': p.id, 'nome': p.nome,
            'area': p.area.nome if p.area else None,
            'status': p.status, 'prioridade': p.prioridade,
            'foco_12s': bool(p.foco_12s),
            'tarefas_total': len(p.tarefas),
            'tarefas_abertas': len(ativas),
            'tem_atrasada': p.tem_atrasada,
        }

    def _completo(p):
        out = _resumo(p)
        out['observacao'] = p.observacao
        out['criado_em'] = p.criado_em.isoformat() if p.criado_em else None
        out['tarefas'] = [{
            'id': t.id, 'nome': t.nome, 'status': t.status,
            'tipo': t.tipo, 'esforco': t.esforco,
            'prazo': t.prazo.isoformat() if t.prazo else None,
            'atrasada': t.atrasada,
            'recorrencia': t.recorrencia,
            'responsavel': t.responsavel.nome if t.responsavel else None,
            'observacao': t.observacao,
            'feito_em': t.feito_em.isoformat() if t.feito_em else None,
        } for t in p.tarefas]
        return out

    pid = (request.args.get('id') or '').strip()
    nome = (request.args.get('nome') or '').strip()
    if pid:
        try:
            projs = [p for p in [Projeto.query.get(int(pid))] if p]
        except (TypeError, ValueError):
            return jsonify(ok=False, erro='id invalido'), 400
        if not projs:
            return jsonify(ok=False, erro='projeto nao encontrado'), 404
        return jsonify(ok=True, projeto=_completo(projs[0]))
    if nome:
        projs = (Projeto.query
                 .filter(func.lower(Projeto.nome).contains(nome.lower()))
                 .order_by(Projeto.nome).all())
        if not projs:
            return jsonify(ok=False, erro='nenhum projeto com esse nome'), 404
        if len(projs) > 1:
            return jsonify(ok=True, candidatos=[
                {'id': p.id, 'nome': p.nome} for p in projs])
        return jsonify(ok=True, projeto=_completo(projs[0]))
    projs = Projeto.query.order_by(Projeto.criado_em.desc()).all()
    return jsonify(ok=True, projetos=[_resumo(p) for p in projs])


@claude_api_bp.route('/acuracia')
@_claude_auth_required
def acuracia_previsao():
    """Acurácia da previsão, read-only (16/07/2026, mutirão de confiança):
    o resumo do painel /producao/previsao-acuracia + o WAPE por (loja,
    receita) dos motores vivos. `?dias=`/`?motor=` valem só pro RESUMO; o
    bloco por_loja_receita é janela FIXA de 60 dias — a mesma dos selos
    WAPE das grades, pra bater 1:1 com o que a tela mostra."""
    from app.extensions import db
    from app.models import Loja, Receita
    from app.services.previsao_acuracia import (
        MOTOR_LABEL,
        MOTORES_VIVOS,
        acuracia_por_loja_receita,
        resumo_acuracia,
    )
    dias = _int_arg('dias', 30, 7, 180)
    motor = request.args.get('motor') or None
    if motor not in MOTOR_LABEL:
        motor = None
    resumo = resumo_acuracia(dias=dias, motor=motor)

    nomes_r = {r.id: r.nome for r in
               db.session.query(Receita.id, Receita.nome).all()}
    nomes_l = {lj.id: lj.nome for lj in
               db.session.query(Loja.id, Loja.nome).all()}
    por_item = {}
    for m in MOTORES_VIVOS:
        linhas = []
        for (loja_id, receita_id), vals in \
                acuracia_por_loja_receita(m, dias=60).items():
            linhas.append({
                'loja_id': loja_id, 'loja': nomes_l.get(loja_id),
                'receita_id': receita_id,
                'receita': nomes_r.get(receita_id),
                **vals,
            })
        linhas.sort(key=lambda x: -(x.get('wape_pct') or 0))
        por_item[m] = linhas
    return jsonify(ok=True, dias=dias, motor=motor,
                   motores=dict(MOTOR_LABEL), resumo=resumo,
                   por_loja_receita=por_item)


@claude_api_bp.route('/auditoria-mapeamentos')
@_claude_auth_required
def auditoria_mapeamentos():
    """Auditoria read-only dos mapeamentos de venda→estoque (12/07/2026,
    "tem dado diferenca nos estoques"): lojas sem vinculo, pendentes com
    venda, alvos mortos, fatores, duplicatas, cestas vazias, movimentos
    sem_estoque, debitos travados e pedidos com itens nao baixados.
    `?dias=N` (default 14, max 60)."""
    from app.services.auditoria_mapeamentos import auditar
    try:
        dias = int(request.args.get('dias', 14))
    except (TypeError, ValueError):
        dias = 14
    return jsonify(ok=True, **auditar(dias=dias))


@claude_api_bp.route('/pedidos-site')
@_claude_auth_required
def pedidos_site():
    """Pedidos do SITE dos últimos N dias com a linha do tempo de status e
    cobranças (15/07/2026, caso "cliente recebeu cancelamento de pedido que
    não cancelei") — permite reconstruir QUEM foi cancelado, quando e por
    quê. Read-only. Params: ?dias=3 (1-30), ?status=cancelado (opcional)."""
    from datetime import datetime, time, timedelta

    from app.models import PedidoOnline
    from app.utils import hoje

    dias = max(1, min(request.args.get('dias', 3, type=int), 30))
    status_f = (request.args.get('status') or '').strip()
    ini_dt = datetime.combine(hoje() - timedelta(days=dias - 1), time.min)
    q = PedidoOnline.query.filter(PedidoOnline.criado_em >= ini_dt)
    if status_f:
        q = q.filter(PedidoOnline.status == status_f)
    out = []
    for p in q.order_by(PedidoOnline.criado_em.desc()).limit(200).all():
        out.append({
            'codigo': p.codigo, 'status': p.status,
            'nome_cliente': p.nome_cliente,
            # Presente/entrega (10/08/2026, pergunta do dono "teve compra
            # para ou de Gaelle?"): sem destinatário/endereço a sonda só
            # enxergava o COMPRADOR — ausência não provava nada sobre
            # presente. Read-only, mesmo gate Bearer de sempre.
            'nome_destinatario': p.nome_destinatario,
            'email_cliente': p.email_cliente,
            'endereco_entrega': p.endereco_entrega,
            'valor_total': float(p.valor_total or 0),
            'modo_entrega': p.modo_entrega,
            'data_entrega': (p.data_entrega.isoformat()
                             if p.data_entrega else None),
            'criado_em': p.criado_em.isoformat() if p.criado_em else None,
            'pago_em': p.pago_em.isoformat() if p.pago_em else None,
            'cancelado_em': p.cancelado_em.isoformat()
            if getattr(p, 'cancelado_em', None) else None,
            'motivo_cancelamento': p.motivo_cancelamento,
            # Itens (07/08/2026, caso "como saiu Caixa de Mini pro dia dos
            # pais?!"): sem eles a sonda nao dizia O QUE foi comprado nem
            # pra QUANDO — nao dava pra ligar o item da tela ao pedido.
            'itens': [{
                'nome': it.nome, 'qtd': it.quantidade,
                'preco_unitario': float(it.preco_unitario or 0),
                'subtotal': float(it.subtotal or 0),
                'kind': it.kind,
                'componentes': [{
                    'nome': c.nome, 'qtd': c.quantidade,
                    'preco': float(c.preco_unitario or 0),
                } for c in (it.componentes or [])],
            } for it in p.itens],
            'pagamentos': [{
                'metodo': pg.metodo, 'status': pg.status,
                'valor': float(pg.valor or 0),
                'charge_id': pg.pagarme_charge_id,
            } for pg in p.pagamentos],
        })
    return jsonify(ok=True, dias=dias, pedidos=out)


@claude_api_bp.route('/auditoria-baixa-pedidos')
@_claude_auth_required
def auditoria_baixa_pedidos():
    """A saída do pedido loja→indústria baixou os congelados? (14/07/2026,
    "está dando diferença no estoque"). Read-only — compara cada pedido que
    saiu com os movimentos reais de MovEstoqueProducao. ?dias=N (default 14)."""
    from app.services import auditoria_baixa_pedidos as svc
    dias = max(1, min(request.args.get('dias', 14, type=int), 120))
    return jsonify(ok=True, **svc.auditar(dias=dias))


@claude_api_bp.route('/site-metricas')
@_claude_auth_required
def site_metricas():
    """Métricas do site/loja própria (13/07/2026, pergunta do dono sobre
    alcance): funil de pedidos, faturamento por dia, ticket, clientes novos
    vs recorrentes, top produtos, modos de entrega e sensores de frete.
    O alcance de VISITAS (GA4/Meta Pixel) vive nos painéis do Google/Meta —
    aqui devolvemos só se estão configurados. Read-only."""
    from datetime import datetime, time, timedelta

    from sqlalchemy import func

    from app.extensions import db
    from app.models import FreteSensor, PedidoOnline
    from app.services import loja_online_vendas as lov
    from app.utils import hoje

    dias = max(1, min(request.args.get('dias', 30, type=int), 365))
    fim = hoje()
    ini = fim - timedelta(days=dias - 1)
    ini_dt = datetime.combine(ini, time.min)
    fim_dt = datetime.combine(fim + timedelta(days=1), time.min)

    fat = lov.faturamento_por_dia(ini, fim)
    clientes = lov.resumo_clientes(ini, fim)
    prods = lov.produtos_vendidos(ini, fim)
    ticket = round(fat['total'] / fat['n_pedidos'], 2) if fat['n_pedidos'] else 0.0

    # Funil por CRIADO no período (pago_em marca conversão; status transita
    # depois) — mesma conta do auditor (`chatbot_auditor._funil_site`).
    criados = (PedidoOnline.query
               .filter(PedidoOnline.criado_em >= ini_dt,
                       PedidoOnline.criado_em < fim_dt).all())
    pagos_criados = [p for p in criados if p.pago_em is not None]
    funil = {
        'criados': len(criados),
        'pagos': len(pagos_criados),
        'cancelados': sum(1 for p in criados if p.status == 'cancelado'),
        'abandonados': sum(1 for p in criados if p.pago_em is None
                           and p.status == 'aguardando_pagamento'),
        'conversao_pct': (round(100.0 * len(pagos_criados) / len(criados), 1)
                          if criados else None),
    }

    modos = dict(
        db.session.query(PedidoOnline.modo_entrega, func.count())
        .filter(PedidoOnline.pago_em >= ini_dt,
                PedidoOnline.pago_em < fim_dt,
                PedidoOnline.status != 'cancelado')
        .group_by(PedidoOnline.modo_entrega).all())

    sensores = dict(
        db.session.query(FreteSensor.desfecho, func.count())
        .filter(FreteSensor.criado_em >= ini_dt,
                FreteSensor.criado_em < fim_dt)
        .group_by(FreteSensor.desfecho).all())

    return jsonify(
        ok=True, dias=dias, inicio=ini.isoformat(), fim=fim.isoformat(),
        faturamento={'total': fat['total'], 'n_pedidos': fat['n_pedidos'],
                     'por_dia': {d.isoformat(): v
                                 for d, v in sorted(fat['por_dia'].items())}},
        ticket_medio=ticket,
        clientes=clientes,
        funil=funil,
        modos_entrega=modos,
        top_produtos=prods['produtos'][:15],
        frete_sensores=sensores,
        rastreio={'ga4_configurado': bool(current_app.config.get('GA4_ID')),
                  'meta_pixel_configurado': bool(
                      current_app.config.get('META_PIXEL_ID')),
                  # IDs públicos (saem no HTML de todo visitante) — servem
                  # pra conferir se o painel GA4/Meta aberto é o MESMO
                  # fluxo/pixel que o site usa. Segredos NUNCA aqui.
                  'ga4_id': (current_app.config.get('GA4_ID') or '').strip(),
                  'meta_pixel_id': (current_app.config.get('META_PIXEL_ID')
                                    or '').strip(),
                  'ga4_api_secret_configurado': bool(
                      (current_app.config.get('GA4_API_SECRET') or '').strip()),
                  'meta_capi_token_configurado': bool(
                      (current_app.config.get('META_CAPI_TOKEN') or '').strip())},
    )


@claude_api_bp.route('/custos')
@_claude_auth_required
def custos():
    """Custos unitários do catálogo inteiro (13/07/2026, planilha "Custos
    faltantes" do dono): receitas (custo calculado pela ficha), produtos/
    cestas (composição ou custo_direto) e matérias-primas (custo cadastrado
    + última entrada com preço). Read-only — serve pra responder "quanto
    custa X?" sem acesso direto ao Postgres."""
    from sqlalchemy import func

    from app.extensions import db
    from app.models import MateriaPrima, MovimentacaoEstoque, Produto, Receita
    from app.services.custos import calcular_custos_produtos, calcular_custos_receitas

    base = calcular_custos_receitas()
    produto_custos = calcular_custos_produtos(base['custos'], base['mp_info'])

    receitas = [
        {'id': r.id, 'nome': r.nome,
         'custo_unitario': round(base['custos'].get(r.nome, 0), 4),
         'arquivada': r.arquivada_em is not None}
        for r in Receita.query.order_by(Receita.nome).all()]

    produtos = []
    for p in Produto.query.order_by(Produto.nome).all():
        produtos.append({
            'id': p.id, 'nome': p.nome, 'ativo': bool(p.ativo),
            'custo': (round(produto_custos[p.nome], 4)
                      if p.nome in produto_custos else None),
            'custo_direto': p.custo_direto,
            'custo_embalagem': p.custo_embalagem or 0,
            'n_itens': len(p.itens),
            'precos': {'atacado': p.preco_atacado, 'loja': p.preco_loja,
                       'site': p.preco_site, 'interno': p.preco_interno},
        })

    # Última ENTRADA com preço de cada MP — melhor proxy de "custo do
    # fornecedor" quando o cadastro está zerado/desatualizado.
    ult_ids = dict(
        db.session.query(MovimentacaoEstoque.materia_prima_id,
                         func.max(MovimentacaoEstoque.id))
        .filter(MovimentacaoEstoque.tipo == 'entrada',
                MovimentacaoEstoque.preco_unitario.isnot(None),
                MovimentacaoEstoque.preco_unitario > 0)
        .group_by(MovimentacaoEstoque.materia_prima_id).all())
    ult_movs = {}
    if ult_ids:
        for mv in MovimentacaoEstoque.query.filter(
                MovimentacaoEstoque.id.in_(list(ult_ids.values()))).all():
            ult_movs[mv.materia_prima_id] = mv

    mps = []
    for mp in MateriaPrima.query.order_by(MateriaPrima.nome).all():
        if mp.unidade == 'un':
            custo_un = mp.custo_por_kg
        elif mp.peso_unidade:
            custo_un = (mp.custo_por_kg or 0) * mp.peso_unidade / 1000.0
        else:
            custo_un = None
        mv = ult_movs.get(mp.id)
        mps.append({
            'id': mp.id, 'nome': mp.nome, 'unidade': mp.unidade,
            'custo_por_kg': mp.custo_por_kg,
            'peso_unidade': mp.peso_unidade,
            'custo_unitario': round(custo_un, 4) if custo_un else None,
            'fornecedor': mp.fornecedor,
            'arquivada': mp.arquivada_em is not None,
            'ultima_entrada': ({'data': mv.data.isoformat() if mv.data else None,
                                'preco_unitario': mv.preco_unitario,
                                'quantidade': mv.quantidade} if mv else None),
        })

    return jsonify(ok=True, receitas=receitas, produtos=produtos,
                   materias_primas=mps,
                   circulares=base['circulares'])


@claude_api_bp.route('/seru-debug')
@_claude_auth_required
def seru_debug():
    """Sonda da API do Seru (12/07/2026, 'a API do Seru parou'): auth + 1
    request real do ponto de vista de PRODUCAO, com o erro exato. O
    container do assistente nao alcanca o host do Seru — esta e a unica
    forma de ele diagnosticar. Read-only, sem segredos (so presenca)."""
    from datetime import date

    from app.services.pdv_saude import debug_seru_status
    dia = None
    if request.args.get('dia'):
        try:
            dia = date.fromisoformat(request.args['dia'])
        except ValueError:
            return jsonify(ok=False, erro='dia invalido (YYYY-MM-DD)'), 400
    try:
        limit = int(request.args.get('limit', 1))
    except ValueError:
        limit = 1
    return jsonify(ok=True, **debug_seru_status(dia=dia, limit=limit))


@claude_api_bp.route('/spotify-debug')
@_claude_auth_required
def spotify_debug():
    """Diagnóstico read-only da integração Spotify (widget do padeiro):
    presença das envs (NUNCA o valor), conexão da conta, redirect URI em uso
    e um teste real do player — pro assistente achar em qual elo parou
    (env → conexão → aparelho/Premium) sem depender de print do dono."""
    from app.services import spotify
    envs = {}
    for k in ('SPOTIFY_CLIENT_ID', 'SPOTIFY_CLIENT_SECRET',
              'SPOTIFY_REDIRECT_URI'):
        v = (current_app.config.get(k) or '').strip()
        envs[k] = {'presente': bool(v), 'tamanho': len(v)}
    out = {
        'ok': True,
        'envs': envs,
        'configurado': spotify.configurado(),
        'conectado': spotify.conectado(),
        'conta': spotify.conta_display(),
        'redirect_uri_em_uso': spotify.redirect_uri(),
        # 'streaming' presente = pode tocar NA tela; ausente = precisa
        # reconectar em /admin/spotify (autorização antiga não ganha escopo).
        'escopos_concedidos': spotify.escopos_concedidos(),
        'tem_streaming': 'streaming' in spotify.escopos_concedidos(),
    }
    if spotify.configurado() and spotify.conectado():
        out['estado_player'] = spotify.estado_player()
        out['n_playlists'] = len(spotify.listar_playlists())
    # Violações de CSP da tela do padeiro (report-uri): quando a música morre
    # em ~10s, aqui aparece QUAL host de áudio a CSP bloqueou.
    try:
        import json as _json

        from app.models import AppConfig
        out['csp_reports'] = _json.loads(
            AppConfig.get('padeiro_csp_reports') or '[]')
        # Telemetria do player da tela (erros do SDK + transições de estado).
        out['spotify_log'] = _json.loads(
            AppConfig.get('padeiro_spotify_log') or '[]')
    except Exception:  # noqa: BLE001
        out['csp_reports'] = out.get('csp_reports') or []
        out['spotify_log'] = []
    return jsonify(out)


@claude_api_bp.route('/vigia-vereditos')
@_claude_auth_required
def vigia_vereditos():
    """Vereditos do vigia do chatbot direto do BANCO (19/07/2026).

    O /admin/vigia/diag é memória volátil (zera no deploy) e o relatório do
    auditor não traz conv_id — sem esta sonda, investigar "bot perdeu
    contexto 2x" de fora exigia query manual no Postgres.

    Params: ?dias=1 (1-30), ?limite=200 (1-500), ?conv=<conv_id> (filtro),
    ?conversa=<conv_id> devolve TAMBÉM o store da conversa
    (ChatbotConversa.mensagens_json) pra ler o diálogo como o bot viu.
    """
    from datetime import timedelta

    from app.models import ChatbotConversa, VigiaVeredito
    from app.utils import agora

    dias = _int_arg('dias', 1, 1, 30)
    limite = _int_arg('limite', 200, 1, 500)
    corte = agora() - timedelta(days=dias)
    q = VigiaVeredito.query.filter(VigiaVeredito.criado_em >= corte)
    conv_filtro = (request.args.get('conv') or '').strip()
    if conv_filtro:
        q = q.filter(VigiaVeredito.conv_id == conv_filtro)
    linhas = (q.order_by(VigiaVeredito.criado_em.desc())
              .limit(limite).all())
    out = {
        'ok': True,
        'dias': dias,
        'total': len(linhas),
        'vereditos': [{
            'id': v.id,
            'criado_em': v.criado_em.isoformat() if v.criado_em else None,
            'conv_id': v.conv_id,
            'cliente': v.cliente,
            'mensagem_cliente': (v.mensagem_cliente or '')[:300],
            'bot_acao': v.bot_acao,
            'bot_motivo': v.bot_motivo,
            'alerta': bool(v.alerta),
            'gravidade': v.gravidade,
            'motivo_vigia': v.motivo_vigia,
            'tools_usadas': v.tools_usadas,
            'enviado_whatsapp': bool(v.enviado_whatsapp),
        } for v in linhas],
    }
    conversa = (request.args.get('conversa') or '').strip()
    if conversa:
        import json as _json
        c = ChatbotConversa.query.filter_by(conv_id=conversa).first()
        try:
            msgs = _json.loads(c.mensagens_json) if c else []
        except (ValueError, TypeError):
            msgs = []
        out['conversa'] = {
            'conv_id': conversa,
            'existe_no_store': c is not None,
            'ultima_msg_em': (c.ultima_msg_em.isoformat()
                              if c and c.ultima_msg_em else None),
            'mensagens': msgs,
        }
    return jsonify(out)


@claude_api_bp.route('/treinamento-diag')
@_claude_auth_required
def treinamento_diag():
    """Diagnóstico do armazenamento de vídeo de treinamento (por que o upload
    falha): mostra a pasta configurada, se EXISTE e é GRAVÁVEL de fato (escreve
    e apaga um arquivo de teste), o espaço livre e restos de upload por pedaços.
    Read-only quanto ao negócio — só sonda o volume."""
    import os
    import shutil

    from app.services import treinamento_stream as ts
    d = current_app.config.get('TREINAMENTO_MEDIA_DIR')
    out = {
        'ok': True,
        'media_dir': d,
        'teto_video_mb': round(
            (current_app.config.get('TREINAMENTO_MAX_VIDEO') or 0) / 1048576, 1),
        # Status do Cloudflare Stream — SEM vazar segredo (só presença + o
        # subdomínio de entrega, que é público).
        'cloudflare': {
            'account_id_set': bool(
                (current_app.config.get('CLOUDFLARE_ACCOUNT_ID') or '').strip()),
            'token_set': bool(
                (current_app.config.get('CLOUDFLARE_STREAM_TOKEN') or '').strip()),
            'configurado': ts.configurado(),
            'subdomain': ts.subdomain(),
        },
    }
    try:
        os.makedirs(d, exist_ok=True)
        out['existe'] = os.path.isdir(d)
    except OSError as e:
        out['existe'] = False
        out['erro_makedirs'] = f'{type(e).__name__}: {e}'
        return jsonify(out)
    # Teste real de escrita+leitura+remoção.
    teste = os.path.join(d, '.diag-escrita')
    try:
        with open(teste, 'wb') as f:
            f.write(b'x' * 1024)
        out['gravavel'] = os.path.getsize(teste) == 1024
        os.remove(teste)
    except OSError as e:
        out['gravavel'] = False
        out['erro_escrita'] = f'{type(e).__name__}: {e}'
    # Espaço livre no ponto de montagem.
    try:
        uso = shutil.disk_usage(d)
        out['livre_gb'] = round(uso.free / 1073741824, 2)
        out['total_gb'] = round(uso.total / 1073741824, 2)
    except OSError as e:
        out['erro_disco'] = f'{type(e).__name__}: {e}'
    # Arquivos presentes (vídeos finais + parciais de upload).
    try:
        nomes = os.listdir(d)
        out['n_arquivos'] = len(nomes)
        out['parciais'] = [n for n in nomes if n.startswith('.part-')][:20]
        out['videos'] = [n for n in nomes if n.startswith('treino-')][:20]
    except OSError as e:
        out['erro_listar'] = f'{type(e).__name__}: {e}'
    return jsonify(out)


@claude_api_bp.route('/pagamento-debug')
@_claude_auth_required
def pagamento_debug():
    """Diagnostico de checkout que o Pagar.me RECUSA na validacao ("The request
    is invalid") — o pedido nem chega a nascer no gateway. Mostra o payload que
    seria enviado + checagens (documento/email/telefone/soma dos itens). Com
    ?post=1 faz UM POST real e devolve o corpo COMPLETO do erro (campo `errors`
    do Pagar.me), que e a fonte definitiva do campo invalido. Read-only exceto
    o post opcional (que so cria mais uma tentativa falha, inofensivo).
    Param: ?codigo=<codigo do pedido> [&post=1]."""
    from app.models import PedidoOnline
    from app.services import pagarme
    codigo = (request.args.get('codigo') or '').strip()
    if not codigo:
        return jsonify(ok=False, erro='informe ?codigo=<codigo do pedido>'), 400
    pedido = PedidoOnline.query.filter_by(codigo=codigo).first()
    if pedido is None:
        return jsonify(ok=False, erro=f'pedido {codigo} nao encontrado'), 404

    cust = pagarme._payload_customer(pedido)
    itens = pagarme._payload_items(pedido)
    soma_itens = sum(i['amount'] * i['quantity'] for i in itens)
    total_c = pagarme._centavos(pedido.valor_total)
    cli = getattr(pedido, 'cliente', None)
    doc = pagarme._so_digitos(getattr(cli, 'cpf', '') if cli else '')
    email = (pedido.email_cliente or '')
    out = {
        'ok': True,
        'codigo': codigo,
        'status_pedido': pedido.status,
        'valor_total': str(pedido.valor_total),
        'total_centavos': total_c,
        'soma_itens_centavos': soma_itens,
        'itens_batem_com_total': soma_itens == total_c,
        'n_itens': len(itens),
        'customer_enviado': {k: v for k, v in cust.items() if k != 'document'},
        'documento': {
            'digitos': len(doc),
            'valido': len(doc) in (11, 14),
            'seria_enviado': 'document' in cust,
        },
        'email': {
            'valor': email,
            'tem_arroba': '@' in email,
            'tem_espaco': (email != email.strip()) or (' ' in email),
        },
        'telefone_valido': pagarme._telefone_br(pedido.telefone_cliente)
        is not None,
    }
    if request.args.get('post') == '1':
        payload = {
            'customer': cust, 'items': itens,
            'payments': [{'payment_method': 'pix',
                          'amount': total_c, 'pix': {'expires_in': 1800}}],
            'code': pedido.codigo,
        }
        st, body = pagarme._post_order(payload)
        out['pagarme_http'] = st
        out['pagarme_message'] = body.get('message')
        out['pagarme_errors'] = body.get('errors')
    return jsonify(out)


@claude_api_bp.route('/cancelados-estorno')
@_claude_auth_required
def cancelados_estorno():
    """Auditoria dos pedidos CANCELADOS de um dia: tinham item de verdade? o
    estoque foi baixado? foi estornado? (pergunta do dono 25/07/2026 ao abrir o
    drill-down de cancelados na home).

    Cruza a API Seru (fonte do cancelamento) com `SeruPedidoProcessado` (o que
    o sync fez). O gatilho de ESTORNO é keyed em `canceledAt`: pedido cancelado
    SÓ por `status=='canceled'` que já tinha baixado estoque NÃO estorna
    sozinho — é o risco que esta sonda expõe (`veredito=BAIXOU_SEM_ESTORNO`).
    Read-only estrito. Params: ?dia=YYYY-MM-DD (default hoje BRT).
    """
    from datetime import date as _date

    from app.extensions import db
    from app.models import SeruPedidoProcessado
    from app.services import seru
    from app.services.briefing_dono import _resolver_loja_seru
    from app.services.vendas_itens import _nome_loja
    from app.utils import hoje

    dia_s = (request.args.get('dia') or '').strip()
    try:
        dia = _date.fromisoformat(dia_s) if dia_s else hoje()
    except ValueError:
        return jsonify(ok=False, erro='dia inválido (use YYYY-MM-DD)'), 400

    try:
        pedidos = seru.listar_pedidos_completo(dia, dia)
    except Exception as exc:  # noqa: BLE001 — Seru fora vira 502 claro
        return jsonify(ok=False, erro=f'Seru indisponível: {exc}'), 502

    vinculo = _resolver_loja_seru()
    linhas, alertas = [], 0
    for p in pedidos:
        if not isinstance(p, dict):
            continue
        try:
            if seru.data_local(p.get('createdAt')) != dia:
                continue
            if not seru.pedido_cancelado(p):
                continue
            itens = seru.extrair_itens(p)
            vivos = [i for i in itens if not i.get('cancelado')]
            pid = str(p.get('id') or '')
            reg = db.session.get(SeruPedidoProcessado, pid) if pid else None
            baixados = int(getattr(reg, 'n_itens_baixados', 0) or 0)
            estornado = getattr(reg, 'estornado_em', None)
            if reg is None:
                veredito = 'NAO_PROCESSADO'      # sync ainda não viu / filtrou
            elif baixados == 0:
                veredito = 'NUNCA_BAIXOU'        # registrado já cancelado — ok
            elif estornado:
                veredito = 'BAIXOU_E_ESTORNOU'   # ok
            else:
                veredito = 'BAIXOU_SEM_ESTORNO'  # ⚠ estoque a menos
                alertas += 1
            dh = seru.datahora_local(p.get('createdAt'))
            ln = _nome_loja(p) or '(sem loja)'
            cx = p.get('cashier')
            linhas.append({
                'codigo': p.get('code'),
                'hora': dh.strftime('%H:%M') if dh else '?',
                'loja': vinculo.get(ln, ln),
                'valor': round(float(p.get('total') or 0), 2),
                'caixa': cx.get('code') if isinstance(cx, dict) else None,
                'itens_total': len(itens),
                'itens_nao_cancelados': len(vivos),
                'cancelado_por': 'canceledAt' if p.get('canceledAt')
                                 else 'status',
                'n_itens_baixados': baixados,
                'estornado_em': estornado.isoformat() if estornado else None,
                'veredito': veredito,
            })
        except Exception as exc:  # noqa: BLE001 — um pedido torto não derruba
            linhas.append({'codigo': p.get('code'), 'erro': str(exc)[:200]})
    linhas.sort(key=lambda x: x.get('hora') or '')
    return jsonify(ok=True, dia=dia.isoformat(), n_cancelados=len(linhas),
                   valor_total=round(sum(x.get('valor') or 0 for x in linhas), 2),
                   alertas_estoque=alertas, pedidos=linhas)


@claude_api_bp.route('/echo-upload', methods=['POST'])
@_claude_auth_required
def echo_upload():
    """Diagnostico de UPLOAD: devolve exatamente o que o servidor RECEBEU do
    corpo da request (25/07/2026 — "Sessao de seguranca expirada" ao subir foto
    na ficha, com [token-ausente · 0 campos] = corpo chegando VAZIO).

    Permite testar de fora (curl) sem precisar do dono repetir o teste: mostra
    Content-Length/Transfer-Encoding/boundary, quantos campos e arquivos o
    Werkzeug conseguiu parsear e `wsgi.input_terminated` — que e o que decide se
    um corpo CHUNKED e lido ou vira 0 byte atras do proxy. Read-only (nao grava
    nada, nao toca banco)."""
    env = request.environ
    try:
        campos = {k: (v[:40] if isinstance(v, str) else str(v)[:40])
                  for k, v in request.form.items()}
        erro_form = None
    except Exception as exc:  # noqa: BLE001
        campos, erro_form = {}, f'{type(exc).__name__}: {exc}'[:200]
    arquivos = {}
    try:
        for nome, fs in request.files.items():
            dados = fs.read()
            arquivos[nome] = {'filename': fs.filename, 'bytes': len(dados),
                              'mimetype': fs.mimetype}
    except Exception as exc:  # noqa: BLE001
        arquivos = {'_erro': f'{type(exc).__name__}: {exc}'[:200]}
    return jsonify(
        ok=True,
        content_length=request.content_length,
        content_type=request.content_type,
        transfer_encoding=request.headers.get('Transfer-Encoding'),
        wsgi_input_terminated=bool(env.get('wsgi.input_terminated')),
        servidor=env.get('SERVER_SOFTWARE'),
        max_content_length=current_app.config.get('MAX_CONTENT_LENGTH'),
        n_campos=len(campos), campos=campos, erro_form=erro_form,
        n_arquivos=len([k for k in arquivos if not k.startswith('_')]),
        arquivos=arquivos,
    )


@claude_api_bp.route('/tiny-vendas')
@_claude_auth_required
def tiny_vendas():
    """SONDA read-only das VENDAS no Tiny por PERIODO (27/07/2026).

    Motivo: a Cantina vende pelo PDV do Tiny, e a integracao existente e de
    MAO UNICA — `tiny.py` so EMITE nota e busca pedido pontual (por CPF ou
    por numero_ordem_compra); nada le venda por data. Antes de escrever a
    importacao (que baixa EstoqueLoja e entra em faturamento/previsao),
    preciso ver o payload REAL: se a venda de PDV vira `pedido`, se vira so
    NFC-e, quais campos trazem produto/quantidade/valor e o que identifica a
    loja. Codificar mapeamento as cegas e como nascem bug de estoque.

    Uso: ?de=2026-07-25&ate=2026-07-26[&detalhe=1][&paginas=2]
    - `de`/`ate` em ISO (converto pro dd/mm/aaaa que a v2 espera).
    - `detalhe=1` abre os N primeiros pedidos (`pedido.obter.php`) pra
      mostrar os ITENS — e o que decide o mapeamento produto->receita.

    Consulta os DOIS caminhos (pedidos e notas fiscais) porque o PDV pode
    gravar so um deles. READ-ONLY: nao escreve nada, nao baixa estoque.
    """
    from datetime import date as _date

    from app.services import tiny

    def _br(iso, fallback_dias):
        """ISO -> dd/mm/aaaa (formato da API v2). Sem valor: hoje-N dias."""
        from datetime import timedelta

        from app.utils import hoje
        try:
            d = _date.fromisoformat((iso or '').strip())
        except ValueError:
            d = hoje() - timedelta(days=fallback_dias)
        return d.strftime('%d/%m/%Y'), d.isoformat()

    de_br, de_iso = _br(request.args.get('de'), 7)
    ate_br, ate_iso = _br(request.args.get('ate'), 0)
    paginas = _int_arg('paginas', 2, 1, 10)
    detalhe = request.args.get('detalhe') in ('1', 'true', 'sim')
    max_detalhe = _int_arg('max_detalhe', 5, 1, 20)

    out = {'ok': True, 'de': de_iso, 'ate': ate_iso,
           'de_br': de_br, 'ate_br': ate_br,
           'tiny_disponivel': tiny.disponivel()}

    # O que JA foi IMPORTADO pro nosso banco (01/08/2026) — mesma conta do
    # faturamento da tela /pdv/tiny e do cockpit da home. Vem ANTES do gate
    # do token de proposito: responde "quanto a Cantina faturou" mesmo com a
    # API do Tiny fora, e diferencia "nao vendeu" de "nao importado".
    from app.services import tiny_pdv_sync
    _fat = tiny_pdv_sync.faturamento_periodo(
        _date.fromisoformat(de_iso), _date.fromisoformat(ate_iso))
    out['importado'] = {
        'loja': _fat['loja'], 'total': _fat['total'],
        'n_pedidos': _fat['n_pedidos'], 'sem_data': _fat['sem_data'],
        'por_dia': {d.isoformat(): v for d, v in _fat['por_dia'].items()},
    }

    if not tiny.disponivel():
        out['ok'] = False
        out['erro'] = 'TINY_API_TOKEN nao configurado neste ambiente'
        return jsonify(out), 503

    # ── 1) pedidos.pesquisa.php por data ────────────────────────────
    pedidos = []
    erros_pedidos = []
    for pagina in range(1, paginas + 1):
        retorno = tiny._get('pedidos.pesquisa.php',
                            params={'dataInicial': de_br, 'dataFinal': ate_br,
                                    'pagina': str(pagina)},
                            retornar_erro=True)
        if not isinstance(retorno, dict):
            erros_pedidos.append(tiny._consumir_falha() or 'sem resposta')
            break
        erros = tiny._extrair_erros(retorno)
        if erros:
            erros_pedidos.extend(erros)
            break
        lote = retorno.get('pedidos') or []
        for item in lote:
            p = item.get('pedido') if isinstance(item, dict) else None
            if isinstance(p, dict):
                pedidos.append(p)
        if len(lote) < 100:
            break
    out['pedidos_n'] = len(pedidos)
    out['pedidos_erros'] = erros_pedidos or None
    # Amostra ENXUTA: so os campos que interessam pro desenho da importacao.
    out['pedidos'] = [
        {k: p.get(k) for k in
         ('id', 'numero', 'data_pedido', 'nome', 'situacao', 'valor',
          'id_natureza_operacao', 'numero_ordem_compra', 'id_vendedor',
          'nome_vendedor', 'deposito', 'id_lista_preco', 'descricao_curta')
         if p.get(k) not in (None, '')}
        for p in pedidos[:30]
    ]
    # Chaves CRUAS do 1o pedido: mostra o que existe alem do que eu chutei.
    if pedidos:
        out['pedido_chaves_disponiveis'] = sorted(pedidos[0].keys())

    # ── 2) detalhe (itens) dos primeiros pedidos ────────────────────
    if detalhe and pedidos:
        detalhes = []
        for p in pedidos[:max_detalhe]:
            pid = str(p.get('id') or '')
            if not pid:
                continue
            det = tiny.obter_pedido_detalhe(pid)
            if not isinstance(det, dict):
                detalhes.append({'id': pid,
                                 'erro': tiny._consumir_falha() or 'sem detalhe'})
                continue
            itens = []
            for it in (det.get('itens') or []):
                i = it.get('item') if isinstance(it, dict) else None
                if isinstance(i, dict):
                    itens.append({k: i.get(k) for k in
                                  ('id_produto', 'codigo', 'descricao',
                                   'quantidade', 'valor_unitario', 'unidade')
                                  if i.get(k) not in (None, '')})
            detalhes.append({
                'id': pid, 'numero': det.get('numero'),
                'data_pedido': det.get('data_pedido'),
                'situacao': det.get('situacao'),
                'deposito': det.get('deposito'),
                'valor': det.get('total_pedido') or det.get('valor'),
                'itens': itens,
                'chaves_disponiveis': sorted(det.keys()),
            })
        out['detalhes'] = detalhes

    # ── 3) notas fiscais por data (o PDV pode gravar so NFC-e) ──────
    notas = []
    erros_notas = []
    retorno = tiny._get('notas.fiscais.pesquisa.php',
                        params={'dataInicial': de_br, 'dataFinal': ate_br,
                                'pagina': '1'},
                        retornar_erro=True)
    if isinstance(retorno, dict):
        erros_notas = tiny._extrair_erros(retorno) or []
        for item in (retorno.get('notas_fiscais') or []):
            n = item.get('nota_fiscal') if isinstance(item, dict) else None
            if isinstance(n, dict):
                notas.append({k: n.get(k) for k in
                              ('id', 'numero', 'data_emissao', 'modelo',
                               'situacao', 'valor', 'cliente_nome', 'serie')
                              if n.get(k) not in (None, '')})
    else:
        erros_notas = [tiny._consumir_falha() or 'sem resposta']
    out['notas_n'] = len(notas)
    out['notas'] = notas[:30]
    out['notas_erros'] = erros_notas or None
    return jsonify(out)


@claude_api_bp.route('/conferencia-loja')
@_claude_auth_required
def conferencia_loja():
    """As CORREÇÕES de conferência que o dono aplicou no estoque das lojas
    (31/07/2026, "na conferência de estoque das lojas tem dado uma diferença
    enorme"). Lê os `MovEstoqueLoja` tipo='ajuste_conferencia' da janela — que
    é o registro do que ELE contou na prateleira contra o que o sistema dizia —
    e ordena pelo tamanho da diferença, pra achar de onde vem o rombo.

    Sinal (mesma convenção dos dois caminhos de conferência):
      diff > 0  -> real MAIOR que o sistema: o sistema baixou DEMAIS (sangrou).
      diff < 0  -> real MENOR que o sistema: o sistema NÃO baixou o que saiu.

    Read-only estrito. Params: ?dias=N (default 2, max 60), ?loja=<nome|id>,
    ?limite=N (default 60, max 300).
    """
    from datetime import datetime, time, timedelta

    from app.extensions import db
    from app.models import EstoqueLoja, Loja, MovEstoqueLoja
    from app.utils import hoje, resolver_loja_por_nome

    dias = max(1, min(request.args.get('dias', 2, type=int) or 2, 60))
    limite = max(1, min(request.args.get('limite', 60, type=int) or 60, 300))
    ini = datetime.combine(hoje() - timedelta(days=dias - 1), time.min)

    q = (db.session.query(MovEstoqueLoja, EstoqueLoja, Loja)
         .join(EstoqueLoja, MovEstoqueLoja.estoque_loja_id == EstoqueLoja.id)
         .join(Loja, EstoqueLoja.loja_id == Loja.id)
         .filter(MovEstoqueLoja.tipo == 'ajuste_conferencia',
                 MovEstoqueLoja.data >= ini))

    bruto = (request.args.get('loja') or '').strip()
    loja_sel = None
    if bruto:
        if bruto.isdigit():
            loja_sel = db.session.get(Loja, int(bruto))
        if loja_sel is None:
            loja_sel = resolver_loja_por_nome(bruto)
        if loja_sel is None:
            return jsonify(ok=False, erro='loja nao encontrada'), 404
        q = q.filter(EstoqueLoja.loja_id == loja_sel.id)

    linhas = q.order_by(MovEstoqueLoja.data.desc()).limit(3000).all()

    ajustes, por_loja = [], {}
    for mov, el, loja in linhas:
        diff = int(mov.quantidade or 0)
        ajustes.append({
            'data': mov.data.isoformat() if mov.data else None,
            'loja': loja.nome,
            'item': el.nome_item,
            'tipo_item': ('receita' if el.receita_id else
                          'produto' if el.produto_id else
                          'materia_prima' if el.materia_prima_id else
                          'pendente'),
            'diff': diff,
            'saldo_atual': int(el.quantidade or 0),
            'referencia': mov.referencia,
        })
        b = por_loja.setdefault(loja.nome, {
            'n_ajustes': 0, 'faltava_no_sistema': 0, 'sobrava_no_sistema': 0})
        b['n_ajustes'] += 1
        if diff > 0:
            b['faltava_no_sistema'] += diff     # sistema baixou demais
        else:
            b['sobrava_no_sistema'] += -diff    # sistema nao baixou

    # Agregado por ITEM — é aqui que o padrão aparece (mesmo item em várias lojas).
    por_item = {}
    for a in ajustes:
        k = (a['item'], a['tipo_item'])
        b = por_item.setdefault(k, {'item': a['item'], 'tipo_item': a['tipo_item'],
                                    'n': 0, 'soma_diff': 0, 'lojas': set()})
        b['n'] += 1
        b['soma_diff'] += a['diff']
        b['lojas'].add(a['loja'])
    itens = sorted(({**v, 'lojas': sorted(v['lojas'])} for v in por_item.values()),
                   key=lambda x: -abs(x['soma_diff']))

    return jsonify(
        ok=True,
        janela={'inicio': ini.date().isoformat(), 'dias': dias},
        loja=(loja_sel.nome if loja_sel else None),
        total_ajustes=len(ajustes),
        por_loja=por_loja,
        por_item=itens[:limite],
        maiores=sorted(ajustes, key=lambda a: -abs(a['diff']))[:limite],
    )


@claude_api_bp.route('/estoque-ledger')
@_claude_auth_required
def estoque_ledger():
    """Razão do estoque de UM item numa loja: todo `MovEstoqueLoja` da janela
    somado POR TIPO (entrou x saiu), pra responder "onde foi parar" quando a
    conferência acusa diferença (31/07/2026, caso "diferença enorme").

    ATENCAO — o SINAL de `MovEstoqueLoja.quantidade` NAO indica direcao: o
    canal Seru/lote grava a BAIXA como POSITIVO (convencao historica, ver
    `baixa_venda._SINAL_ESTORNO`). Por isso este endpoint NAO tenta somar
    "entrou x saiu": devolve o bruto POR TIPO, que e o unico corte confiavel.

    Sem `?item=`, devolve o resumo por tipo da loja inteira. Read-only.
    Params: ?loja=<nome|id> (obrigatório), ?item=<trecho do nome>,
    ?dias=N (default 14, max 90).
    """
    from datetime import datetime, time, timedelta

    from sqlalchemy import func

    from app.extensions import db
    from app.models import EstoqueLoja, Loja, MovEstoqueLoja
    from app.utils import hoje, resolver_loja_por_nome

    dias = max(1, min(request.args.get('dias', 14, type=int) or 14, 90))
    ini = datetime.combine(hoje() - timedelta(days=dias - 1), time.min)

    bruto = (request.args.get('loja') or '').strip()
    if not bruto:
        return jsonify(ok=False, erro='informe ?loja='), 400
    loja = db.session.get(Loja, int(bruto)) if bruto.isdigit() else None
    if loja is None:
        loja = resolver_loja_por_nome(bruto)
    if loja is None:
        return jsonify(ok=False, erro='loja nao encontrada'), 404

    alvo = (request.args.get('item') or '').strip().lower()
    linhas = (EstoqueLoja.query.filter_by(loja_id=loja.id).all())
    if alvo:
        linhas = [el for el in linhas if alvo in (el.nome_item or '').lower()]
    if not linhas:
        return jsonify(ok=False, erro='nenhuma linha de estoque casou',
                       loja=loja.nome), 404

    ids = [el.id for el in linhas]
    somas = {}
    for eid, tipo, soma, n in (
            db.session.query(MovEstoqueLoja.estoque_loja_id, MovEstoqueLoja.tipo,
                             func.sum(MovEstoqueLoja.quantidade),
                             func.count(MovEstoqueLoja.id))
            .filter(MovEstoqueLoja.estoque_loja_id.in_(ids),
                    MovEstoqueLoja.data >= ini)
            .group_by(MovEstoqueLoja.estoque_loja_id, MovEstoqueLoja.tipo).all()):
        somas.setdefault(eid, {})[tipo] = {'soma': int(soma or 0), 'n': int(n or 0)}

    itens = []
    for el in linhas:
        por_tipo = somas.get(el.id, {})
        itens.append({
            'item': el.nome_item,
            'estoque_loja_id': el.id,
            'estado': el.estado,
            'tipo_item': ('receita' if el.receita_id else
                          'produto' if el.produto_id else
                          'materia_prima' if el.materia_prima_id else 'pendente'),
            'saldo_atual': int(el.quantidade or 0),
            'n_movimentos': sum(v['n'] for v in por_tipo.values()),
            'por_tipo': por_tipo,
        })
    itens.sort(key=lambda x: -x['n_movimentos'])

    return jsonify(ok=True, loja=loja.nome,
                   janela={'inicio': ini.date().isoformat(), 'dias': dias},
                   itens=itens[:40])


@claude_api_bp.route('/plano-dia')
@_claude_auth_required
def plano_dia_site():
    """Plano-do-dia do SITE de uma data (read-only, 07/08/2026 — criada pra
    conferir a curadoria do Dia dos Pais sem login de owner): linhas do
    plano com nome resolvido + itens publicados SEM linha (fail-open =
    vendem livre) + bloqueios da data especial. Params: ?data=YYYY-MM-DD
    (default amanhã)."""
    from datetime import date as _date
    from datetime import timedelta

    from app.models import EstoqueSitePlano
    from app.services import loja_catalogo, loja_data_especial
    from app.utils import hoje

    data_s = (request.args.get('data') or '').strip()
    if data_s:
        try:
            alvo = _date.fromisoformat(data_s)
        except ValueError:
            return jsonify(ok=False, erro='data invalida (YYYY-MM-DD)'), 400
    else:
        alvo = hoje() + timedelta(days=1)

    publicados = {(it['kind'], it['id']): it['nome']
                  for it in loja_catalogo.produtos_publicados()}
    linhas = (EstoqueSitePlano.query.filter_by(data=alvo)
              .order_by(EstoqueSitePlano.kind, EstoqueSitePlano.item_id)
              .all())
    out = []
    for ln in linhas:
        out.append({
            'kind': ln.kind, 'item_id': ln.item_id,
            'nome': publicados.get((ln.kind, ln.item_id)),
            'publicado': (ln.kind, ln.item_id) in publicados,
            'qtd_planejada': int(ln.qtd_planejada or 0),
            'qtd_reservada': int(ln.qtd_reservada or 0),
        })
    com_linha = {(ln.kind, ln.item_id) for ln in linhas}
    sem_linha = [{'kind': k, 'item_id': i, 'nome': n}
                 for (k, i), n in sorted(publicados.items())
                 if (k, i) not in com_linha]
    regra = loja_data_especial.regra_do_dia(alvo)
    return jsonify(
        ok=True, data=alvo.isoformat(), linhas=out,
        publicados_sem_linha_vendem_livre=sem_linha,
        data_especial=None if regra is None else {
            'rotulo': regra.rotulo,
            'janelas': regra.lista_janelas(),
            'express_bloqueado': bool(regra.express_bloqueado),
            'bloquear_itens': regra.lista_bloqueios(),
        })


@claude_api_bp.route('/catalogo-site')
@_claude_auth_required
def catalogo_site():
    """SONDA read-only da vitrine do site (05/08/2026).

    Nasceu pra montar e-mail de campanha: o assistente precisa do NOME, do
    LINK e da FOTO de cada produto pra escrever a peça, e as paginas da loja
    so respondem no host da loja (opao.online), inalcancavel daqui.

    ?busca=texto filtra por nome (acento-insensivel, "contem").
    """
    from app.services.loja_catalogo import produtos_publicados
    from app.utils import normalizar_busca

    base = (current_app.config.get('LOJA_BASE_URL') or '').rstrip('/')
    busca = normalizar_busca(request.args.get('busca') or '')
    itens = []
    for it in produtos_publicados():
        if busca and busca not in normalizar_busca(it.get('nome') or ''):
            continue
        # `href` vem do próprio catálogo (mesma fonte do sitemap e da
        # vitrine) — montar o slug aqui divergiria no dia de um rename.
        itens.append({
            'nome': it.get('nome'), 'kind': it.get('kind'), 'id': it.get('id'),
            'categoria': it.get('categoria'), 'preco': it.get('preco'),
            'imagem': it.get('imagem') or '',
            'url': base + it.get('href', ''),
        })
    return jsonify({'ok': True, 'total': len(itens), 'itens': itens})


@claude_api_bp.route('/funcionarios')
@_claude_auth_required
def funcionarios():
    """SONDA read-only do quadro de funcionários (05/08/2026).

    Nasceu pra montar o lote de assinatura eletrônica do Regulamento
    Interno (Autentique): o assistente precisa de nome + e-mail + telefone
    DA FICHA de cada funcionário — o canal cadastrado no RH é o que amarra
    a prova da assinatura em juízo (associação unívoca da Lei 14.063).

    Default = só ativos; ?todos=1 inclui desligados. PII: mesma classe das
    demais sondas (Bearer token, read-only).
    """
    from sqlalchemy.orm import selectinload

    from app.models import Funcionario

    q = Funcionario.query.options(selectinload(Funcionario.lojas))
    if request.args.get('todos') != '1':
        q = q.filter(Funcionario.ativo.is_(True))
    itens = []
    for f in q.order_by(Funcionario.nome).all():
        itens.append({
            'id': f.id, 'nome': f.nome, 'cpf': f.cpf,
            'funcao': f.funcao or f.funcao_operacional or '',
            'email': (f.email or '').strip(),
            'telefone': (f.telefone or '').strip(),
            'lojas': [l.nome for l in f.lojas],
            'ativo': bool(f.ativo),
            'cadastro_pendente': bool(f.cadastro_pendente),
        })
    return jsonify({
        'ok': True, 'total': len(itens),
        'sem_email': sum(1 for x in itens if not x['email']),
        'sem_telefone': sum(1 for x in itens if not x['telefone']),
        'funcionarios': itens,
    })


@claude_api_bp.route('/checklist')
@_claude_auth_required
def checklist_estado():
    """SONDA read-only do checklist de loja (03/08/2026).

    Serve pra duas coisas: confirmar de fora que a importacao do checklist
    em papel entrou (itens por tipo/setor) e ver quem esta DEVENDO hoje —
    a mesma conta que alimenta o "Precisa de voce hoje" da home.

    ?dias=N (default 7) inclui os preenchimentos recentes.
    """
    from datetime import timedelta

    from sqlalchemy import func

    from app.constants import CHECKLIST_TIPO_LABEL
    from app.extensions import db
    from app.models import ChecklistItemModelo, ChecklistPreenchimento
    from app.services import checklist_loja
    from app.utils import hoje

    dias = _int_arg('dias', 7, 1, 90)
    itens = ChecklistItemModelo.query.all()
    por_tipo = {}
    for it in itens:
        d = por_tipo.setdefault(it.tipo, {'total': 0, 'ativos': 0,
                                          'exigem_foto': 0, 'setores': {}})
        d['total'] += 1
        if it.ativo:
            d['ativos'] += 1
            d['setores'][it.setor or 'Geral'] = (
                d['setores'].get(it.setor or 'Geral', 0) + 1)
        if it.exige_foto:
            d['exigem_foto'] += 1

    di = hoje() - timedelta(days=dias - 1)
    recentes = (db.session.query(
        ChecklistPreenchimento.data, ChecklistPreenchimento.tipo,
        func.count(ChecklistPreenchimento.id))
        .filter(ChecklistPreenchimento.data >= di)
        .group_by(ChecklistPreenchimento.data,
                  ChecklistPreenchimento.tipo).all())

    return jsonify(
        ok=True,
        itens_cadastrados=len(itens),
        por_tipo={CHECKLIST_TIPO_LABEL.get(t, t): v
                  for t, v in por_tipo.items()},
        devendo={
            'abertura_hoje': checklist_loja.lojas_faltando('abertura', hoje()),
            'fechamento_ontem': checklist_loja.lojas_faltando(
                'fechamento', hoje() - timedelta(days=1)),
        },
        preenchimentos={'janela_dias': dias,
                        'por_dia': [{'data': d.isoformat(), 'tipo': t,
                                     'n': int(n)} for d, t, n in recentes]},
        pendencias_na_home=checklist_loja.pendencias_checklist(),
    )


@claude_api_bp.route('/drivers')
@_claude_auth_required
def drivers():
    """SONDA read-only dos motoristas de entrega (07/08/2026).

    Nasceu pra confirmar de fora o seed dos motoristas do Dia dos Pais —
    o container de dev nao enxerga o Postgres de prod e nao havia sonda de
    Driver. NUNCA expoe `token` nem `pin` (o token abre a pagina do
    motorista); so presenca.

    Default = so ativos; ?todos=1 inclui inativos.
    """
    from app.models import Driver

    q = Driver.query
    if request.args.get('todos') != '1':
        q = q.filter(Driver.ativo.is_(True))
    itens = [{
        'id': d.id, 'nome': d.nome,
        'telefone': (d.telefone or '').strip(),
        'ativo': bool(d.ativo),
        'capacidade': d.capacidade or 999,
        'tem_token': bool(d.token),
        'tem_pin': bool(d.pin),
        'criado_em': d.criado_em.isoformat() if d.criado_em else None,
    } for d in q.order_by(Driver.nome).all()]
    return jsonify(ok=True, total=len(itens),
                   sem_telefone=sum(1 for x in itens if not x['telefone']),
                   drivers=itens)


@claude_api_bp.route('/ordens-producao')
@_claude_auth_required
def ordens_producao():
    """Ordens de produção (PlanejamentoProducao) por data — read-only.

    Criada em 17/08/2026 pra diagnosticar "o envio automático das 19:00 não
    está enviando": mostra, por dia, se a ordem existe, se está enviada ao
    padeiro, quem criou e quando — o que o cron fez (ou não fez) fica
    visível de fora. Params: ?de=YYYY-MM-DD&ate=YYYY-MM-DD (default:
    últimos 7 dias até amanhã).
    """
    from datetime import date, timedelta

    from app.extensions import db
    from app.models import PlanejamentoProducao, Usuario
    from app.utils import hoje

    try:
        de = date.fromisoformat((request.args.get('de') or '').strip())
    except ValueError:
        de = hoje() - timedelta(days=7)
    try:
        ate = date.fromisoformat((request.args.get('ate') or '').strip())
    except ValueError:
        ate = hoje() + timedelta(days=1)

    planos = (PlanejamentoProducao.query
              .filter(PlanejamentoProducao.data >= de,
                      PlanejamentoProducao.data <= ate)
              .order_by(PlanejamentoProducao.data, PlanejamentoProducao.id)
              .all())

    def _nome(uid):
        if not uid:
            return None
        u = db.session.get(Usuario, uid)
        return u.nome if u else f'#{uid}'

    out = []
    for p in planos:
        itens = p.itens or []
        out.append({
            'id': p.id,
            'data': p.data.isoformat() if p.data else None,
            'nome': p.nome,
            'origem': p.origem,
            'status': p.status,
            'enviado_ao_padeiro': bool(p.enviado_ao_padeiro),
            'criado_em': (p.criado_em.strftime('%Y-%m-%d %H:%M:%S')
                          if p.criado_em else None),
            'criado_por': _nome(p.criado_por),
            'n_itens': len(itens),
            'soma_alvo': sum(int(i.qtd_alvo or 0) for i in itens),
            'soma_produzido': sum(int(i.produzido_qtd or 0) for i in itens),
        })
    return jsonify(ok=True, de=de.isoformat(), ate=ate.isoformat(),
                   ordens=out)


@claude_api_bp.route('/pedidos-itens')
@_claude_auth_required
def pedidos_itens():
    """Pedidos loja->industria que contem UM item (por trecho do nome) —
    criada 18/08/2026 na auditoria "granola/iogurte em potes x gramas": o
    relatorio de pedidos so agrega por item, e sem esta sonda nao da pra
    dizer de fora QUAL pedido carregou a quantidade suspeita. Read-only.

    Params: ?item=<trecho do nome> (obrigatorio, >=3 chars),
    ?dias=N (janela por data_entrega, default 60, max 180),
    ?loja=<nome|id> (opcional). Cap 200 linhas, mais recentes primeiro.
    """
    from datetime import timedelta

    from sqlalchemy import or_

    from app.models import (
        Loja,
        MateriaPrima,
        PedidoItem,
        PedidoLoja,
        Produto,
        Receita,
        Usuario,
    )
    from app.utils import hoje, normalizar_busca, resolver_loja_por_nome

    trecho = (request.args.get('item') or '').strip()
    if len(trecho) < 3:
        return jsonify(ok=False, erro='?item= obrigatorio (>= 3 chars)'), 400
    dias = max(1, min(request.args.get('dias', 60, type=int) or 60, 180))
    corte = hoje() - timedelta(days=dias - 1)

    loja = None
    bruto_loja = (request.args.get('loja') or '').strip()
    if bruto_loja:
        loja = (Loja.query.get(int(bruto_loja)) if bruto_loja.isdigit()
                else resolver_loja_por_nome(bruto_loja))
        if not loja:
            return jsonify(ok=False, erro=f'loja {bruto_loja!r} nao achada'), 404

    q = normalizar_busca(trecho)
    termos = q.split()

    def _casa(nome):
        n = normalizar_busca(nome or '')
        return all(t in n for t in termos)

    # Resolve os alvos que casam o trecho (receita/produto/MP — inclui
    # arquivados de proposito: pedido antigo pode apontar pra item morto).
    rec_ids = [r.id for r in Receita.query.all() if _casa(r.nome)]
    prod_ids = [p.id for p in Produto.query.all() if _casa(p.nome)]
    mp_ids = [m.id for m in MateriaPrima.query.all() if _casa(m.nome)]
    if not (rec_ids or prod_ids or mp_ids):
        return jsonify(ok=True, itens=[], aviso='nenhum item casa o trecho')

    filtros = []
    if rec_ids:
        filtros.append(PedidoItem.receita_id.in_(rec_ids))
    if prod_ids:
        filtros.append(PedidoItem.produto_id.in_(prod_ids))
    if mp_ids:
        filtros.append(PedidoItem.materia_prima_id.in_(mp_ids))

    query = (PedidoItem.query.join(PedidoLoja)
             .filter(or_(*filtros))
             .filter(or_(PedidoLoja.data_entrega >= corte,
                         PedidoLoja.data_entrega.is_(None)))
             .order_by(PedidoLoja.data_entrega.desc().nullslast(),
                       PedidoLoja.id.desc()))
    if loja:
        query = query.filter(PedidoLoja.loja_id == loja.id)
    linhas = query.limit(200).all()

    usuarios = {u.id: u.nome for u in Usuario.query.all()}
    lojas_map = {lj.id: lj.nome for lj in Loja.query.all()}
    out = []
    for it in linhas:
        p = it.pedido
        out.append({
            'pedido_id': p.id,
            'loja': lojas_map.get(p.loja_id),
            'data_entrega': p.data_entrega.isoformat() if p.data_entrega else None,
            'status': p.status,
            'item': it.nome_item,
            'quantidade': it.quantidade,
            'quantidade_recebida': it.quantidade_recebida,
            'observacao_item': it.observacao,
            'observacao_pedido': (p.observacao or '')[:120] or None,
            'criado_por': usuarios.get(p.criado_por),
        })
    return jsonify(ok=True, dias=dias, trecho=trecho, n=len(out), itens=out)


@claude_api_bp.route('/alertas-debug')
@_claude_auth_required
def alertas_debug():
    """Sonda de DIAGNOSTICO dos alertas de WhatsApp (20/08/2026, caso
    "Continua duplicando" com pdv_vigia 00:03/06:03 + digest de tarefas
    2x as 07:00). Read-only:
    - `envios`: log NotificacaoWhatsapp (SO digest de tarefas e automacoes
      passam pelo `whatsapp.notificar` — os vigias chamam zapi direto e NAO
      aparecem aqui).
    - `automacoes`: AutomacaoWhatsapp cadastradas (mensagem FIXA agendada).
    - `seru_loja_map`: vinculos company->loja com confirmado_em (o check 3
      do pdv_vigia acusa company vendendo sem vinculo confirmado).
    - `pdv_vigia`: estado em AppConfig — `ultima_assinatura` guarda o texto
      EXATO do ultimo problema alertado.
    Params: ?horas=48 (1-720, janela do log de envios).
    """
    from datetime import timedelta

    from app.models import (
        AppConfig,
        AutomacaoWhatsapp,
        Loja,
        NotificacaoWhatsapp,
        SeruLojaMap,
    )
    from app.utils import agora

    horas = _int_arg('horas', 48, 1, 720)
    corte = agora() - timedelta(hours=horas)
    envios = [{
        'em': n.criado_em.isoformat() if n.criado_em else None,
        'origem': n.origem, 'ok': n.ok, 'erro': n.erro or None,
        'mensagem_inicio': (n.mensagem or '')[:120],
    } for n in (NotificacaoWhatsapp.query
                .filter(NotificacaoWhatsapp.criado_em >= corte)
                .order_by(NotificacaoWhatsapp.criado_em.desc())
                .limit(100).all())]
    automacoes = [{
        'id': a.id, 'nome': a.nome, 'ativo': a.ativo,
        'horario': a.horario, 'dias_semana': a.dias_semana or 'todos',
        'destino': a.destino or '(padrao)',
        'ultimo_disparo_em': (a.ultimo_disparo_em.isoformat()
                              if a.ultimo_disparo_em else None),
        'mensagem_inicio': (a.mensagem or '')[:120],
    } for a in AutomacaoWhatsapp.query.order_by(AutomacaoWhatsapp.id).all()]
    lojas = {x.id: x.nome for x in Loja.query.all()}
    mapas = [{
        'seru_company_name': m.seru_company_name,
        'seru_company_id': m.seru_company_id,
        'loja': lojas.get(m.loja_id),
        'ignorar': m.ignorar, 'auto_match': m.auto_match,
        'confirmado_em': (m.confirmado_em.isoformat()
                          if m.confirmado_em else None),
    } for m in SeruLojaMap.query.order_by(SeruLojaMap.id).all()]
    vigia = {k: AppConfig.get(k) for k in (
        'pdv_vigia_quebrado_desde', 'pdv_vigia_ultimo_alerta_em',
        'pdv_vigia_ultima_assinatura')}
    return jsonify(ok=True, horas=horas, envios=envios,
                   automacoes=automacoes, seru_loja_map=mapas,
                   pdv_vigia=vigia)


@claude_api_bp.route('/chatwoot-thread')
@_claude_auth_required
def chatwoot_thread():
    """Thread AO VIVO de uma conversa do Chatwoot (20/08/2026).

    Criada no caso "duplo texto do bot" (dono: alerta de espera-humano
    chegando 2x): o container de desenvolvimento NÃO alcança o host do
    Chatwoot (proxy corta) e o /admin/debug-chatwoot exige sessão web —
    sem esta sonda, não dá pra provar de fora se uma mensagem do SISTEMA
    (contenção, resposta do bot) foi entregue DUAS vezes na thread do
    cliente. Duplicata na thread = houve DOIS remetentes; o dedupe do
    nosso banco só cobre a instância que o consulta.

    Read-only estrito (GET no Chatwoot). Params: ?conv=<id>
    (obrigatório), ?limite=40 (1-100).
    """
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    from app.services import chatwoot

    conv = (request.args.get('conv') or '').strip()
    if not conv:
        return jsonify(ok=False, erro='informe ?conv=<id da conversa>'), 400
    limite = _int_arg('limite', 40, 1, 100)

    try:
        hist = chatwoot.buscar_historico(conv, limite=limite)
    except Exception as e:  # noqa: BLE001 — sonda nunca 500
        return jsonify(ok=False, erro=f'{type(e).__name__}: {e}'), 502

    def _hora(ts):
        """epoch UTC -> HH:MM:SS BRT (o Chatwoot devolve epoch). Usa
        ZoneInfo, não `-3h` fixo (regra da casa sobre fuso)."""
        if not ts:
            return None
        try:
            return (datetime.fromtimestamp(int(ts), tz=timezone.utc)
                    .astimezone(ZoneInfo('America/Sao_Paulo'))
                    .strftime('%d/%m %H:%M:%S'))
        except (TypeError, ValueError, OSError):
            return None

    msgs = [{
        'role': m.get('role'),
        'hora_brt': _hora(m.get('created_at')),
        'created_at': m.get('created_at'),
        'content': (m.get('content') or '')[:400],
        'imagens': len(m.get('imagens') or []),
    } for m in hist]

    # Duplicatas: MESMO texto aparecendo mais de uma vez (o que interessa
    # é a mensagem do SISTEMA repetida — dois remetentes distintos).
    por_texto = {}
    for m in msgs:
        chave = (m['role'], (m['content'] or '').strip()[:120])
        if not chave[1]:
            continue
        por_texto.setdefault(chave, []).append(m['hora_brt'])
    duplicatas = [{'role': k[0], 'trecho': k[1], 'vezes': len(v),
                   'horas': v}
                  for k, v in por_texto.items() if len(v) > 1]

    return jsonify(ok=True, conv=conv, total=len(msgs),
                   duplicatas=duplicatas, mensagens=msgs)
