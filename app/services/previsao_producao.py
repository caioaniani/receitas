"""Sugestao de producao agregada — quanto produzir nos proximos N dias.

Cache: o resultado eh cacheado por 60s em memoria do processo pra evitar
que cada refresh do painel TV refaca chamadas VNDA pesadas (VNDA paga
quando nao tem cache). Cache key inclui horizonte_dias, lookback_dias.


Producao precisa estar na frente das lojas. Como o historico de pedidos
e ralo, usamos vendas reais (Seru + VNDA + manuais) — historico bom — como
proxy de demanda. Reusa `vendas_manuais.sugerir_pedido` (que ja agrega
isso por loja) e soma de todas as lojas operacionais.

Output:
- qtd_total_lojas: demanda projetada (todas as lojas)
- qtd_em_pedidos: ja vai sair via pedido em aberto pras lojas (nao precisa
  produzir de novo)
- qtd_em_estoque_industria: ja pronto em EstoqueProducao
- qtd_a_produzir: max(0, total_lojas - em_pedidos - em_estoque)
- breakdown_por_loja: {loja_id: qtd_lojas_dessa_loja}
- stockout: True se alguma loja teve estoque atual abaixo da media diaria
  (provavel subestimativa — producao deve considerar produzir mais)

Lookback fixo de 14 dias pra calcular media diaria. Horizonte (cobertura)
e parametrizavel (1-14 dias).
"""
from collections import defaultdict
from datetime import timedelta

from app.constants import STATUS_PEDIDO_FINALIZADOS
from app.extensions import db
from app.models import (
    EstoqueProducao,
    Loja,
    PedidoItem,
    PedidoLoja,
)
from app.utils import hoje

# Status de pedido que ja "comprometem" producao — nao precisam ser
# re-produzidos porque ja estao no fluxo pra serem entregues.
STATUS_EM_FLUXO = (
    'pendente', 'confirmado', 'aprovado',
    'separado', 'em_transporte',
)


def _lojas_operacionais_ids():
    """IDs de lojas operacionais (sem Industria). Service-side, sem importar
    a rota."""
    return [
        l.id for l in Loja.query
        .filter(Loja.ativa.is_(True), Loja.nome != 'Industria')
        .order_by(Loja.nome).all()
    ]


# Cache em memoria do processo. TTL = 60s (bate com refresh do painel TV).
# Key inclui horizonte+lookback. Em multi-worker (gunicorn 2 workers),
# cada worker tem seu cache — aceitavel pra esse caso.
_SUG_CACHE = {}
_SUG_CACHE_TTL = 60  # segundos


def invalidar_sugestao_cache():
    """Chamar quando algo critico mudar (pedido criado, sync VNDA, etc) —
    forca recalculo no proximo request."""
    _SUG_CACHE.clear()


def sugerir_producao(horizonte_dias=7, lookback_dias=14, usar_cache=True):
    """Agrega previsao de demanda das lojas operacionais e retorna
    quanto produzir nos proximos `horizonte_dias`.

    Cache de 60s in-memory pra evitar refazer chamadas VNDA a cada
    refresh. `usar_cache=False` ignora.

    Retorna {'itens': [...], 'horizonte_dias': N, 'avisos_vnda': [...]}.
    Cada item:
      tipo, id, nome, qtd_total_lojas, qtd_em_pedidos,
      qtd_em_estoque_industria, qtd_a_produzir,
      breakdown_por_loja {loja_id: {nome, qtd_lojas}},
      stockout (bool)
    """
    import time

    cache_key = (int(horizonte_dias), int(lookback_dias))
    if usar_cache:
        entrada = _SUG_CACHE.get(cache_key)
        if entrada and (time.time() - entrada['t']) < _SUG_CACHE_TTL:
            return entrada['data']
    from app.services.vendas_manuais import sugerir_pedido as _sug_loja

    horizonte_dias = max(1, min(int(horizonte_dias or 7), 14))
    lookback_dias = max(7, min(int(lookback_dias or 14), 60))

    hoje_d = hoje()
    data_inicio = hoje_d - timedelta(days=lookback_dias)
    data_fim = hoje_d
    limite_entrega = hoje_d + timedelta(days=horizonte_dias)

    lojas = (Loja.query
             .filter(Loja.ativa.is_(True), Loja.nome != 'Industria')
             .order_by(Loja.nome).all())
    lojas_por_id = {l.id: l for l in lojas}

    # 1. Agrega sugestao por (tipo, id), guardando breakdown por loja
    qtd_lojas = defaultdict(int)        # {(tipo, id): qtd}
    breakdown = defaultdict(dict)        # {(tipo, id): {loja_id: qtd}}
    nome_por_chave = {}
    stockout_por_chave = defaultdict(bool)
    avisos_vnda = []

    for loja in lojas:
        res = _sug_loja(loja.id, data_inicio=data_inicio, data_fim=data_fim,
                        dias_cobertura=horizonte_dias)
        if res.get('aviso_vnda'):
            avisos_vnda.append(f'{loja.nome}: {res["aviso_vnda"]}')
        for item in res.get('itens', []):
            chave = (item['tipo'], item['id'])
            q = int(item.get('qtd_sugerida') or 0)
            if q <= 0:
                # Heuristica de stockout: se estoque_atual era < media
                # diaria, marca mesmo que qtd_sugerida tenha vindo 0
                # (zerou por subtracao de estoque ainda existente).
                if (item.get('estoque_atual') or 0) < (item.get('media_diaria') or 0):
                    stockout_por_chave[chave] = True
                continue
            qtd_lojas[chave] += q
            breakdown[chave][loja.id] = q
            nome_por_chave[chave] = item.get('nome') or '?'
            if (item.get('estoque_atual') or 0) < (item.get('media_diaria') or 0):
                stockout_por_chave[chave] = True

    if not qtd_lojas:
        resultado = {'itens': [], 'horizonte_dias': horizonte_dias,
                     'lookback_dias': lookback_dias,
                     'avisos_vnda': avisos_vnda}
        _SUG_CACHE[cache_key] = {'t': time.time(), 'data': resultado}
        return resultado

    # 2. Quanto ja esta em pedido em aberto pras lojas dessas chaves
    em_pedidos = defaultdict(int)
    itens_em_aberto = (db.session.query(PedidoItem)
                       .join(PedidoLoja, PedidoItem.pedido_id == PedidoLoja.id)
                       .filter(PedidoLoja.status.in_(STATUS_EM_FLUXO),
                               ~PedidoLoja.status.in_(STATUS_PEDIDO_FINALIZADOS),
                               PedidoLoja.data_entrega >= hoje_d,
                               PedidoLoja.data_entrega <= limite_entrega,
                               PedidoLoja.loja_id.in_(list(lojas_por_id.keys())))
                       .all())
    for pi in itens_em_aberto:
        if pi.receita_id:
            chave = ('receita', pi.receita_id)
        elif pi.produto_id:
            chave = ('produto', pi.produto_id)
        elif pi.materia_prima_id:
            chave = ('mp', pi.materia_prima_id)
        else:
            continue
        em_pedidos[chave] += int(pi.quantidade or 0)

    # 3. Estoque atual da industria por (tipo, id)
    em_estoque = defaultdict(int)
    for ep in EstoqueProducao.query.all():
        if ep.receita_id:
            chave = ('receita', ep.receita_id)
        elif ep.produto_id:
            chave = ('produto', ep.produto_id)
        else:
            continue
        em_estoque[chave] += int(ep.quantidade or 0)

    # 4. Resolve nomes pra breakdown
    nomes_loja = {l.id: l.nome for l in lojas}

    # 5. Monta saida
    itens = []
    for chave, total in qtd_lojas.items():
        tipo, item_id = chave
        em_ped = em_pedidos.get(chave, 0)
        em_est = em_estoque.get(chave, 0)
        a_produzir = max(0, total - em_ped - em_est)
        itens.append({
            'tipo': tipo,
            'id': item_id,
            'nome': nome_por_chave.get(chave, '?'),
            'qtd_total_lojas': total,
            'qtd_em_pedidos': em_ped,
            'qtd_em_estoque_industria': em_est,
            'qtd_a_produzir': a_produzir,
            'breakdown_por_loja': [
                {'loja_id': lid, 'loja_nome': nomes_loja.get(lid, '?'), 'qtd': q}
                for lid, q in sorted(breakdown[chave].items(),
                                      key=lambda kv: -kv[1])
            ],
            'stockout': stockout_por_chave.get(chave, False),
        })

    # Ordena: primeiro o que falta produzir (urgencia), depois o que ja
    # esta coberto mas tem demanda alta (visibilidade).
    itens.sort(key=lambda x: (-x['qtd_a_produzir'], -x['qtd_total_lojas']))

    resultado = {
        'itens': itens,
        'horizonte_dias': horizonte_dias,
        'lookback_dias': lookback_dias,
        'avisos_vnda': avisos_vnda,
    }
    _SUG_CACHE[cache_key] = {'t': time.time(), 'data': resultado}
    return resultado
