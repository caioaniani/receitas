"""Sincronizacao Seru → estoque das lojas (auto-baixa).

Fluxo idempotente:
1. Busca pedidos da Seru no intervalo (createdAt no fuso BRT).
2. Pra cada pedido nao registrado em SeruPedidoProcessado:
   a. Resolve a loja via SeruLojaMap (auto-fuzzy se primeira vez).
   b. Pra cada item do pedido:
      - Mapeado (SeruProdutoMap.estado='mapeado'): baixa de EstoqueLoja
        e cria MovEstoqueLoja(tipo='venda_seru', referencia='Seru #<id>').
      - Ignorado: pula sem alarmes.
      - Pendente (nao visto antes): cria SeruProdutoMap em estado pendente
        pra revisao manual; nao baixa.
   c. Marca SeruPedidoProcessado.
3. Pra pedidos ja processados que aparecem com canceledAt: gera estorno
   (cria movs de tipo='venda_seru_estorno') e marca cancelado_em.

Estoque nunca fica negativo: se nao tem o suficiente, baixa o que tem
e registra MovEstoqueLoja(tipo='venda_seru_sem_estoque') com a falta.
"""
import logging
import re
import unicodedata
from datetime import datetime

from app.extensions import db
from app.models import (Loja, EstoqueLoja, MovEstoqueLoja,
                        SeruProdutoMap, SeruLojaMap, SeruPedidoProcessado)
from app.services import seru

logger = logging.getLogger(__name__)


def _ascii(s):
    if not s:
        return ''
    nf = unicodedata.normalize('NFD', s)
    return ''.join(c for c in nf if unicodedata.category(c) != 'Mn').lower().strip()


def _fuzzy_loja(seru_company_name, lojas):
    """Tenta achar nossa Loja pelo nome da company da Seru.
    Retorna Loja ou None. Estrategia: ascii match exato, depois substring."""
    alvo = _ascii(seru_company_name)
    if not alvo:
        return None
    for l in lojas:
        if _ascii(l.nome) == alvo:
            return l
    for l in lojas:
        la = _ascii(l.nome)
        if alvo in la or la in alvo:
            return l
    # Tentativa final: token-overlap. Util pra "Padaria Opao Ribeiro" vs "Loja Ribeiro do Vale"
    tokens_alvo = set(re.split(r'\s+', alvo)) - {'loja', 'padaria', 'do', 'da', 'de', 'opao'}
    melhor = None
    melhor_overlap = 0
    for l in lojas:
        tokens_l = set(re.split(r'\s+', _ascii(l.nome))) - {'loja', 'padaria', 'do', 'da', 'de'}
        overlap = len(tokens_alvo & tokens_l)
        if overlap > melhor_overlap:
            melhor_overlap = overlap
            melhor = l
    return melhor if melhor_overlap >= 1 else None


def _resolver_loja(seru_company_name, lojas_ativas):
    """Devolve (loja, mapping). Se nao existia mapping, cria via fuzzy.
    Se fuzzy nao acertou nada, cria mapping vazio (estado=pendente) e
    devolve (None, mapping)."""
    if not seru_company_name:
        return None, None
    mapping = SeruLojaMap.query.filter_by(seru_company_name=seru_company_name).first()
    if mapping:
        if mapping.ignorar:
            return None, mapping
        if mapping.loja_id:
            return mapping.loja, mapping
        return None, mapping  # pendente
    # primeira vez: tenta fuzzy
    loja = _fuzzy_loja(seru_company_name, lojas_ativas)
    mapping = SeruLojaMap(
        seru_company_name=seru_company_name,
        loja_id=loja.id if loja else None,
        auto_match=bool(loja),
    )
    db.session.add(mapping)
    db.session.flush()
    return loja, mapping


def _resolver_produto(seru_nome, seru_sku):
    """Devolve SeruProdutoMap (criando se for primeira vez como pendente)."""
    mp = SeruProdutoMap.query.filter_by(seru_nome=seru_nome).first()
    if mp:
        # Atualiza sku se chegou agora
        if seru_sku and not mp.seru_sku:
            mp.seru_sku = seru_sku
        return mp
    mp = SeruProdutoMap(seru_nome=seru_nome, seru_sku=seru_sku or None)
    db.session.add(mp)
    db.session.flush()
    return mp


def _baixar_item(loja_id, mapping_produto, qtd, seru_pedido_id, user_id):
    """Aplica baixa em EstoqueLoja. Se nao tiver row, cria com 0.
    Retorna {baixado: bool, faltou: float}."""
    filtro = {'loja_id': loja_id}
    if mapping_produto.receita_id:
        filtro['receita_id'] = mapping_produto.receita_id
    elif mapping_produto.produto_id:
        filtro['produto_id'] = mapping_produto.produto_id
    else:
        return {'baixado': False, 'faltou': qtd}

    el = EstoqueLoja.query.filter_by(**filtro).first()
    if not el:
        el = EstoqueLoja(**filtro, quantidade=0)
        db.session.add(el)
        db.session.flush()

    atual = el.quantidade or 0
    qtd_int = int(qtd)
    real = min(qtd_int, atual)
    falta = qtd_int - real

    if real > 0:
        el.quantidade = atual - real
        db.session.add(MovEstoqueLoja(
            estoque_loja_id=el.id,
            tipo='venda_seru',
            quantidade=real,
            referencia=f'Seru #{seru_pedido_id}',
            usuario_id=user_id,
        ))
    if falta > 0:
        # Marca a falta sem zerar mais — auditoria
        db.session.add(MovEstoqueLoja(
            estoque_loja_id=el.id,
            tipo='venda_seru_sem_estoque',
            quantidade=falta,
            referencia=f'Seru #{seru_pedido_id} — sem estoque suficiente',
            usuario_id=user_id,
        ))
    return {'baixado': real > 0, 'faltou': falta}


def _estornar_pedido(reg, lojas_ativas, user_id):
    """Reverte baixas de um pedido cancelado. Lojas/mapeamento ja conhecidos."""
    movs = MovEstoqueLoja.query.filter(
        MovEstoqueLoja.tipo == 'venda_seru',
        MovEstoqueLoja.referencia == f'Seru #{reg.seru_pedido_id}',
    ).all()
    for m in movs:
        el = EstoqueLoja.query.get(m.estoque_loja_id)
        if el:
            el.quantidade = (el.quantidade or 0) + m.quantidade
            db.session.add(MovEstoqueLoja(
                estoque_loja_id=el.id,
                tipo='venda_seru_estorno',
                quantidade=m.quantidade,
                referencia=f'Estorno Seru #{reg.seru_pedido_id} (cancelada)',
                usuario_id=user_id,
            ))
    reg.estornado_em = datetime.utcnow()


def processar_pedidos(data_inicial, data_final, user=None,
                      expandir_dias_frente=0):
    """Sincroniza Seru → EstoqueLoja no intervalo.

    Retorna dict com resumo: pedidos novos, pedidos cancelados,
    itens baixados, itens ignorados, itens pendentes (novos),
    sem estoque, lojas pendentes.
    """
    user_id = getattr(user, 'id', None) if user else None
    lojas_ativas = Loja.query.filter_by(ativa=True).all()

    pedidos = seru.listar_pedidos_completo(
        data_inicial, data_final, expandir_dias_frente=expandir_dias_frente)

    stats = {
        'pedidos_novos': 0,
        'pedidos_ja_processados': 0,
        'pedidos_cancelados_estornados': 0,
        'pedidos_sem_loja_mapeada': 0,
        'pedidos_aguardando_loja': 0,
        'itens_baixados': 0,
        'itens_ignorados': 0,
        'itens_pendentes_novos': 0,
        'itens_sem_estoque': 0,
        'erros': [],
    }

    for p in pedidos:
        if not isinstance(p, dict):
            continue
        pid = str(p.get('id') or p.get('orderNumber') or p.get('code') or '').strip()
        if not pid:
            continue
        # Filtra createdAt dentro da janela BRT solicitada
        d = seru.data_local(p.get('createdAt'))
        if not d or not (data_inicial <= d <= data_final):
            continue

        reg = SeruPedidoProcessado.query.get(pid)
        cancelado_at = p.get('canceledAt')

        # Caso ja processado
        if reg:
            stats['pedidos_ja_processados'] += 1
            # Se foi cancelado depois e ainda nao estornamos, estornar
            if cancelado_at and not reg.estornado_em:
                _estornar_pedido(reg, lojas_ativas, user_id)
                reg.cancelado_em = datetime.utcnow()
                stats['pedidos_cancelados_estornados'] += 1
            continue

        # Pedido novo
        if cancelado_at:
            # Ja cancelado — registra mas nao processa items
            db.session.add(SeruPedidoProcessado(
                seru_pedido_id=pid,
                cancelado_em=datetime.utcnow(),
                n_itens_total=len(seru.extrair_itens(p)),
                n_itens_baixados=0,
            ))
            continue

        # Resolve loja
        company = p.get('company') or {}
        cname = ''
        if isinstance(company, dict):
            cname = (company.get('name') or '').strip()
        elif isinstance(company, str):
            cname = company.strip()

        loja, loja_map = _resolver_loja(cname, lojas_ativas)
        if not loja:
            # Sem loja mapeada — registra pedido como processado mas sem baixar
            stats['pedidos_sem_loja_mapeada'] += 1
            db.session.add(SeruPedidoProcessado(
                seru_pedido_id=pid,
                loja_id=None,
                n_itens_total=len(seru.extrair_itens(p)),
                n_itens_baixados=0,
            ))
            continue

        itens = seru.extrair_itens(p)
        n_total = len(itens)
        n_baixados = 0

        for it in itens:
            if it['cancelado']:
                continue
            mp = _resolver_produto(it['nome'], it['sku'])
            if mp.ignorar:
                stats['itens_ignorados'] += 1
                continue
            if mp.estado == 'pendente':
                # Primeira vez OU ja era pendente — apenas conta pra revisao
                if not mp.id or not mp.primeira_visto_em:
                    pass
                stats['itens_pendentes_novos'] += 1
                continue
            # Mapeado — baixa
            res = _baixar_item(loja.id, mp, it['qtd'], pid, user_id)
            if res['baixado']:
                stats['itens_baixados'] += 1
                n_baixados += 1
            if res['faltou']:
                stats['itens_sem_estoque'] += 1

        db.session.add(SeruPedidoProcessado(
            seru_pedido_id=pid,
            loja_id=loja.id,
            n_itens_total=n_total,
            n_itens_baixados=n_baixados,
        ))
        stats['pedidos_novos'] += 1

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.exception('seru_sync commit falhou')
        stats['erros'].append(f'commit: {type(e).__name__}: {str(e)[:200]}')

    return stats


def reprocessar_pedido(pid, user=None):
    """Forca reprocessamento de um pedido (apaga registro + reaplica).
    Util quando o admin acabou de mapear um produto e quer aplicar retroativo.
    NAO estorna baixas anteriores — caller deve garantir que faz sentido.
    """
    reg = SeruPedidoProcessado.query.get(pid)
    if reg:
        db.session.delete(reg)
        db.session.commit()
    # Caller pode chamar processar_pedidos com janela cobrindo o pedido.
