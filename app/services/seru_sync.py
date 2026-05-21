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

from app.extensions import db
from app.models import (
    EstoqueLoja,
    Loja,
    MovEstoqueLoja,
    SeruDebito,
    SeruLojaMap,
    SeruPedidoProcessado,
    SeruProdutoMap,
)
from app.services import seru
from app.utils import agora

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
    """Aplica baixa em EstoqueLoja considerando fator_quantidade.

    Se fator_quantidade < 1 (composto), acumula fracao em SeruDebito ate
    formar inteiros. Ex: fator=0.2, qtd=4 → debito acumulado=0.8, nao
    baixa nada. Proxima venda de 1 com fator=0.2 → debito=1.0 → baixa 1.

    Se o mapping aponta pra Produto-cesta, desempacota em componentes e
    baixa cada um (loja so tem componentes em estoque, nao a cesta).

    Retorna {baixado: bool, faltou: float}.
    """
    # CESTA: se mapping aponta pra Produto com itens, desempacota e baixa
    # cada componente individualmente.
    if mapping_produto.produto_id:
        from app.models import Produto
        from app.services.cestas import componentes_de_cesta
        produto = Produto.query.get(mapping_produto.produto_id)
        componentes = componentes_de_cesta(produto)
        if componentes:
            fator = float(mapping_produto.fator_quantidade or 1.0)
            qtd_cestas_float = float(qtd) * fator
            qtd_cestas = int(qtd_cestas_float + 1e-9)
            if qtd_cestas <= 0:
                # Acumula no debito da cesta (fracao)
                debito = SeruDebito.query.filter_by(
                    loja_id=loja_id, seru_produto_map_id=mapping_produto.id).first()
                if not debito:
                    debito = SeruDebito(loja_id=loja_id,
                                          seru_produto_map_id=mapping_produto.id,
                                          fracao_pendente=0.0)
                    db.session.add(debito)
                debito.fracao_pendente = (debito.fracao_pendente or 0.0) + qtd_cestas_float
                return {'baixado': False, 'faltou': 0, 'acumulado': debito.fracao_pendente}

            faltou_total = 0
            for col, item_id, nome_comp, qtd_por_cesta in componentes:
                qtd_baixar = int(round(qtd_cestas * qtd_por_cesta))
                if qtd_baixar <= 0:
                    continue
                filtro_c = {'loja_id': loja_id, col: item_id}
                el_c = EstoqueLoja.query.filter_by(**filtro_c).first()
                if not el_c:
                    el_c = EstoqueLoja(**filtro_c, quantidade=0)
                    db.session.add(el_c)
                    db.session.flush()
                atual_c = el_c.quantidade or 0
                real_c = min(qtd_baixar, atual_c)
                falta_c = qtd_baixar - real_c
                el_c.quantidade = atual_c - real_c
                if real_c > 0:
                    db.session.add(MovEstoqueLoja(
                        estoque_loja_id=el_c.id,
                        tipo='venda_seru',
                        quantidade=real_c,
                        referencia=(f'Seru #{seru_pedido_id} '
                                    f'[{produto.nome} → cesta] {nome_comp}'),
                        usuario_id=user_id,
                    ))
                if falta_c > 0:
                    faltou_total += falta_c
                    db.session.add(MovEstoqueLoja(
                        estoque_loja_id=el_c.id,
                        tipo='venda_seru_sem_estoque',
                        quantidade=falta_c,
                        referencia=(f'Seru #{seru_pedido_id} '
                                    f'[{produto.nome} → cesta] {nome_comp} — faltou'),
                        usuario_id=user_id,
                    ))
            return {'baixado': True, 'faltou': faltou_total}

    filtro = {'loja_id': loja_id}
    if mapping_produto.receita_id:
        filtro['receita_id'] = mapping_produto.receita_id
    elif mapping_produto.produto_id:
        filtro['produto_id'] = mapping_produto.produto_id
    else:
        return {'baixado': False, 'faltou': qtd}

    fator = float(mapping_produto.fator_quantidade or 1.0)
    a_baixar_float = float(qtd) * fator

    # Acumulador: soma a fracao pendente, separa inteiros pra baixar agora
    debito = SeruDebito.query.filter_by(
        loja_id=loja_id, seru_produto_map_id=mapping_produto.id).first()
    if not debito:
        debito = SeruDebito(loja_id=loja_id,
                            seru_produto_map_id=mapping_produto.id,
                            fracao_pendente=0.0)
        db.session.add(debito)
        db.session.flush()
    debito_total = (debito.fracao_pendente or 0.0) + a_baixar_float
    # Floor com tolerancia pra erros de float (0.9999... vira 1)
    inteiros = int(debito_total + 1e-9)
    debito.fracao_pendente = max(0.0, round(debito_total - inteiros, 6))

    if inteiros <= 0:
        # Tudo acumulado — nada baixa ainda
        return {'baixado': False, 'faltou': 0, 'acumulado': debito.fracao_pendente}

    el = EstoqueLoja.query.filter_by(**filtro).first()
    if not el:
        el = EstoqueLoja(**filtro, quantidade=0)
        db.session.add(el)
        db.session.flush()

    atual = el.quantidade or 0
    real = min(inteiros, atual)
    falta = inteiros - real

    # Referencia: se houver fator, anota
    ref_extra = '' if fator == 1.0 else f' (fator {fator})'
    if real > 0:
        el.quantidade = atual - real
        db.session.add(MovEstoqueLoja(
            estoque_loja_id=el.id,
            tipo='venda_seru',
            quantidade=real,
            referencia=f'Seru #{seru_pedido_id}{ref_extra}',
            usuario_id=user_id,
        ))
    if falta > 0:
        db.session.add(MovEstoqueLoja(
            estoque_loja_id=el.id,
            tipo='venda_seru_sem_estoque',
            quantidade=falta,
            referencia=f'Seru #{seru_pedido_id}{ref_extra} — sem estoque suficiente',
            usuario_id=user_id,
        ))
    return {'baixado': real > 0, 'faltou': falta, 'acumulado': debito.fracao_pendente}


def _estornar_pedido(reg, lojas_ativas, user_id):
    """Reverte baixas de um pedido cancelado. Lojas/mapeamento ja conhecidos.

    Usa `LIKE 'Seru #{id}%'` pra cobrir as 4 variacoes de referencia
    geradas em `_baixar_item`:
      - 'Seru #123'                         (item simples, fator=1)
      - 'Seru #123 (fator 0.2)'             (com fator < 1)
      - 'Seru #123 [Cesta → cesta] X'       (componente de cesta)
      - 'Seru #123 ... — sem estoque ...'   (qualquer das acima sem saldo)

    Versao antiga usava `==` exato, deixando estorno em branco em todos
    os casos exceto o simples.
    """
    movs = MovEstoqueLoja.query.filter(
        MovEstoqueLoja.tipo == 'venda_seru',
        MovEstoqueLoja.referencia.like(f'Seru #{reg.seru_pedido_id}%'),
    ).all()
    if not movs:
        # Nenhuma mov real pra reverter (pedido foi tudo `venda_seru_sem_estoque`
        # ou nunca chegou a baixar). Nao marca estornado_em mentindo —
        # deixa None pra distinguir de "estornado mas sem efeito".
        return
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
    reg.estornado_em = agora()


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
                reg.cancelado_em = agora()
                stats['pedidos_cancelados_estornados'] += 1
            continue

        # Pedido novo
        if cancelado_at:
            # Ja cancelado — registra mas nao processa items
            db.session.add(SeruPedidoProcessado(
                seru_pedido_id=pid,
                cancelado_em=agora(),
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

        # SALVAGUARDA: so baixa estoque se voce CONFIRMOU o mapeamento da loja.
        # Auto-fuzzy sozinho nao basta — pode ter chutado errado.
        # Pedido fica aguardando, sera retentado na proxima sync depois que
        # voce abrir /pdv/mapeamentos e clicar OK/Vincular.
        if not loja_map.confirmado_em:
            stats['pedidos_aguardando_loja'] += 1
            continue  # NAO marca como processado — retenta depois

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
