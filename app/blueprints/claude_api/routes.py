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

    Params: ?horizonte=7 (1-14), ?janela=6 (1-26), ?inicio=0 (0-14).
    """
    from app.services.previsao_producao import cronograma_producao
    from app.services.producao_pendente import pendencias_por_receita

    crono = cronograma_producao(
        horizonte_dias=_int_arg('horizonte', 7, 1, 14),
        janela_semanas=_int_arg('janela', 6, 1, 26),
        inicio_offset_dias=_int_arg('inicio', 0, 0, 14),
        equilibrar=request.args.get('equilibrar') in ('1', 'true'))
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
        })
    return jsonify(
        ok=True,
        hoje=crono['hoje'],
        inicio=crono['inicio'],
        horizonte_dias=crono['horizonte_dias'],
        janela_semanas=crono['janela_semanas'],
        dias=crono['dias'],
        receitas=receitas,
        alertas_falta=crono.get('alertas_falta', []),
    )


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
