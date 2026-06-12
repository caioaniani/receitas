"""Endpoints da NUVEM para os servidores locais das lojas (sync).

Auth: header `Authorization: Bearer <SYNC_API_TOKEN>` — sem session/login,
mesmo padrão do blueprint bot. O cliente é app/services/sync.py rodando
no servidor de cada loja.

- GET  /pdv/api/sync/catalogo  → cadastros que a loja precisa pra vender
- POST /pdv/api/sync/vendas    → recebe vendas finalizadas (idempotente
  por Venda.uuid: o que já existe é só confirmado de volta)
"""
import secrets
from functools import wraps

from flask import request, jsonify, current_app
from sqlalchemy.exc import IntegrityError

from app.blueprints.pdv import pdv_bp
from app.extensions import db, csrf
from app.models import (Loja, Receita, Produto, PrecoLojaReceita,
                        Venda, VendaItem, VendaPagamento)
from app.services.sync import _parse_dt


def sync_token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token_cfg = (current_app.config.get('SYNC_API_TOKEN') or '').strip()
        if not token_cfg:
            return jsonify(ok=False, erro='SYNC_API_TOKEN nao configurado no servidor'), 503
        auth = request.headers.get('Authorization', '')
        recebido = auth[7:].strip() if auth.startswith('Bearer ') else ''
        if not recebido or not secrets.compare_digest(recebido, token_cfg):
            return jsonify(ok=False, erro='token invalido'), 401
        return f(*args, **kwargs)
    return decorated


@pdv_bp.route('/api/sync/catalogo')
@csrf.exempt
@sync_token_required
def sync_catalogo():
    lojas = Loja.query.order_by(Loja.id).all()
    receitas = Receita.query.order_by(Receita.id).all()
    produtos = Produto.query.order_by(Produto.id).all()
    precos = PrecoLojaReceita.query.all()
    return jsonify(
        ok=True,
        lojas=[{'id': l.id, 'nome': l.nome, 'endereco': l.endereco,
                'telefone': l.telefone, 'ativa': l.ativa} for l in lojas],
        receitas=[{'id': r.id, 'nome': r.nome, 'categoria': r.categoria,
                   'setor': r.setor, 'preco_venda': r.preco_venda,
                   'preco_loja': r.preco_loja, 'preco_site': r.preco_site,
                   'rendimento_qtd': r.rendimento_qtd,
                   'rendimento_unidade': r.rendimento_unidade,
                   'peso_base': r.peso_base} for r in receitas],
        produtos=[{'id': p.id, 'nome': p.nome, 'categoria': p.categoria,
                   'setor': p.setor, 'descricao': p.descricao,
                   'preco_atacado': p.preco_atacado, 'preco_loja': p.preco_loja,
                   'preco_site': p.preco_site, 'ativo': p.ativo} for p in produtos],
        precos_loja=[{'loja_id': pl.loja_id, 'receita_id': pl.receita_id,
                      'preco': pl.preco} for pl in precos],
    )


def _importar_venda(doc):
    """Insere uma venda vinda da loja. Retorna 'nova' | 'existente'.
    Levanta ValueError para payload inválido."""
    uid = (doc.get('uuid') or '').strip()
    code = (doc.get('code') or '').strip()[:30]
    if not uid or len(uid) > 32 or not code:
        raise ValueError('uuid/code obrigatórios')
    if Venda.query.filter_by(uuid=uid).first():
        return 'existente'

    loja_id = doc.get('loja_id')
    if loja_id and not db.session.get(Loja, loja_id):
        loja_id = None
    status = doc.get('status')
    if status not in ('paga', 'cancelada'):
        raise ValueError(f'status inválido: {status}')

    venda = Venda(
        uuid=uid,
        code=code,
        loja_id=loja_id,
        usuario_id=None,
        operador=(doc.get('operador') or '')[:100] or None,
        status=status,
        subtotal=doc.get('subtotal') or 0,
        desconto=doc.get('desconto') or 0,
        total=doc.get('total') or 0,
        observacao=(doc.get('observacao') or '')[:300] or None,
        criado_em=_parse_dt(doc.get('criado_em')),
        finalizado_em=_parse_dt(doc.get('finalizado_em')),
        sincronizada_em=None,
    )
    itens = doc.get('itens') or []
    if not itens or len(itens) > 200:
        raise ValueError('venda sem itens (ou itens demais)')
    for it in itens:
        receita_id = it.get('receita_id')
        produto_id = it.get('produto_id')
        # Cadastro pode ter sido excluído na nuvem — mantém só o snapshot
        if receita_id and not db.session.get(Receita, receita_id):
            receita_id = None
        if produto_id and not db.session.get(Produto, produto_id):
            produto_id = None
        venda.itens.append(VendaItem(
            receita_id=receita_id,
            produto_id=produto_id,
            descricao=(it.get('descricao') or '?')[:200],
            setor=(it.get('setor') or '')[:30] or None,
            quantidade=it.get('quantidade') or 1,
            preco_unitario=it.get('preco_unitario') or 0,
            subtotal=it.get('subtotal') or 0,
        ))
    for pg in (doc.get('pagamentos') or [])[:20]:
        venda.pagamentos.append(VendaPagamento(
            metodo=(pg.get('metodo') or '?')[:20],
            valor=pg.get('valor') or 0,
            valor_recebido=pg.get('valor_recebido'),
            troco=pg.get('troco'),
            status=(pg.get('status') or '?')[:30],
            capturado_via=(pg.get('capturado_via') or '')[:20] or None,
            clover_external_id=(pg.get('clover_external_id') or '')[:40] or None,
            clover_payment_id=(pg.get('clover_payment_id') or '')[:60] or None,
            erro=(pg.get('erro') or '')[:300] or None,
            criado_em=_parse_dt(pg.get('criado_em')),
        ))

    db.session.add(venda)
    try:
        db.session.commit()
    except IntegrityError:
        # code colidiu (ex: venda criada direto na nuvem no mesmo dia) —
        # o uuid é a identidade; ajusta o code e tenta de novo
        db.session.rollback()
        venda.code = f'{code[:25]}~{uid[:4]}'
        db.session.add(venda)
        db.session.commit()
    return 'nova'


@pdv_bp.route('/api/sync/vendas', methods=['POST'])
@csrf.exempt
@sync_token_required
def sync_vendas():
    data = request.get_json(silent=True) or {}
    docs = data.get('vendas') or []
    if not isinstance(docs, list) or len(docs) > 200:
        return jsonify(ok=False, erro='payload inválido (máx 200 vendas)'), 400
    aceitas, novas, erros = [], 0, []
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        try:
            resultado = _importar_venda(doc)
            aceitas.append(doc.get('uuid'))
            if resultado == 'nova':
                novas += 1
        except Exception as e:
            db.session.rollback()
            current_app.logger.warning('sync: venda %s recusada: %s',
                                       doc.get('uuid'), e)
            erros.append({'uuid': doc.get('uuid'), 'erro': str(e)[:200]})
    return jsonify(ok=True, aceitas=aceitas, novas=novas, erros=erros)
