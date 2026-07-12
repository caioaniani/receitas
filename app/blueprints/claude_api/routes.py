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
    ?motor=pedidos|vendas|maior (fonte da demanda prevista).
    """
    from app.services.previsao_producao import (
        MOTORES_PREVISAO_PRODUCAO,
        cronograma_producao,
    )
    from app.services.producao_pendente import pendencias_por_receita

    motor = (request.args.get('motor') or 'pedidos').strip()
    if motor not in MOTORES_PREVISAO_PRODUCAO:
        motor = 'pedidos'
    crono = cronograma_producao(
        horizonte_dias=_int_arg('horizonte', 7, 1, 14),
        janela_semanas=_int_arg('janela', 6, 1, 26),
        inicio_offset_dias=_int_arg('inicio', 0, 0, 14),
        equilibrar=request.args.get('equilibrar') in ('1', 'true'),
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
        })
    return jsonify(
        ok=True,
        hoje=crono['hoje'],
        inicio=crono['inicio'],
        horizonte_dias=crono['horizonte_dias'],
        janela_semanas=crono['janela_semanas'],
        motor=crono.get('motor', 'pedidos'),
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


@claude_api_bp.route('/deploy')
@_claude_auth_required
def deploy_info():
    """Qual commit esta NO AR (11/07/2026): o procedimento de 2 commits de
    schema exige confirmar que o ALTER deployou antes de subir o modelo —
    antes disso o assistente precisava pedir ao dono que conferisse o
    Railway. O Railway injeta RAILWAY_GIT_COMMIT_SHA no build. Read-only."""
    import os
    return jsonify(
        ok=True,
        commit=os.environ.get('RAILWAY_GIT_COMMIT_SHA'),
        branch=os.environ.get('RAILWAY_GIT_BRANCH'),
        deployment_id=os.environ.get('RAILWAY_DEPLOYMENT_ID'),
    )


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
