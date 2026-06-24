"""Balanco de producao da industria — baseado em PedidoLoja (demanda real
loja->industria), NAO no PDV (Seru/VNDA).

Por que PedidoLoja e nao PDV: o PDV (Seru/VNDA) registra venda ao CONSUMIDOR
final (balcao/site) — inclui lanche, cafe, insumo avulso, coisa que a
industria nao produz. Pra planejar a PRODUCAO da industria, a demanda
relevante e quanto as LOJAS pedem a industria pra se reabastecer. Essa
demanda mora em PedidoLoja/PedidoItem.

Conceitos (por receita; produto/MP ficam de fora — producao = ficha tecnica
= Receita, decisao do dono 2026-06):

- em_estoque: EstoqueProducao.quantidade da industria (ja pronto).
- comprometido: itens de pedidos AINDA NAO enviados (status em
  STATUS_PEDIDO_NAO_BAIXADOS) com data_entrega dentro do horizonte. A baixa
  do EstoqueProducao so ocorre na transicao separado->em_transporte
  (pedidos/routes.py::_executar_envio_pedido); logo, pedido ja enviado JA
  saiu do estoque e NAO entra no comprometido (senao contaria duas vezes).
- previsto: demanda projetada pelo historico de PedidoLoja, por dia-da-semana
  (padaria tem pico de fim de semana — um sabado nao se parece com uma
  terca), com fallback pra media diaria simples quando aquele dia-da-semana
  tem poucas ocorrencias na janela.
- produzir: max(0, max(comprometido, previsto) - em_estoque). Usa o MAIOR
  entre comprometido e previsto pra nao contar duas vezes: se as lojas ja
  pediram tudo do horizonte, o firme (comprometido) manda; se ainda vao
  pedir, o historico (previsto) cobre o gap.

A tela expoe metadados de profundidade (quantos pedidos / semanas o historico
tem) pra o usuario calibrar a confianca da previsao.

Cache: 60s in-memory por (horizonte, janela) — bate com o refresh do painel.
Em multi-worker (gunicorn) cada worker tem o seu; aceitavel pra este caso.
"""
import time
from collections import defaultdict
from datetime import timedelta
from math import ceil

from app.constants import STATUS_PEDIDO_NAO_BAIXADOS
from app.extensions import db
from app.models import EstoqueProducao, Loja, PedidoItem, PedidoLoja, Receita
from app.utils import hoje

# Minimo de ocorrencias de um mesmo dia-da-semana na janela pra confiar na
# media daquele dia. Abaixo disso, cai no fallback (media diaria simples).
_MIN_OCORRENCIAS_DOW = 2

_CACHE = {}
_CACHE_TTL = 60  # segundos


def invalidar_sugestao_cache():
    """Forca recalculo no proximo request (chamar quando pedido/estoque da
    industria mudar). Mantido com o nome antigo por compat com chamadores."""
    _CACHE.clear()


def balanco_industria(horizonte_dias=7, janela_semanas=6, usar_cache=True):
    """Balanco de producao da industria por receita.

    Args:
        horizonte_dias: janela futura de planejamento (1-14).
        janela_semanas: profundidade do historico pra previsao (1-26).
        usar_cache: usa o cache de 60s (False forca recalculo).

    Retorna dict:
        itens: lista por receita, cada um com em_estoque, comprometido,
               previsto, produzir, tem_historico, breakdown_comprometido.
        horizonte_dias, janela_semanas, hoje, horizonte_fim.
        profundidade: {n_pedidos, n_datas, n_semanas_dados, janela_semanas,
                       desde} — pra UI mostrar a confianca da previsao.
        total_produzir_itens: quantas receitas precisam producao (produzir>0).
    """
    horizonte_dias = max(1, min(int(horizonte_dias or 7), 14))
    janela_semanas = max(1, min(int(janela_semanas or 6), 26))

    cache_key = (horizonte_dias, janela_semanas)
    if usar_cache:
        ent = _CACHE.get(cache_key)
        if ent and (time.time() - ent['t']) < _CACHE_TTL:
            return ent['data']

    hoje_d = hoje()
    horizonte_fim = hoje_d + timedelta(days=horizonte_dias - 1)
    hist_ini = hoje_d - timedelta(days=7 * janela_semanas)
    hist_fim = hoje_d - timedelta(days=1)

    # Receitas ativas (nao arquivadas). Producao = ficha tecnica = Receita.
    receitas = {r.id: r for r in Receita.query
                .filter(Receita.arquivada_em.is_(None)).all()}
    nomes_loja = {l.id: l.nome for l in Loja.query.all()}
    # Lojas OPERACIONAIS (ativas + sem a "Industria"). Usada no breakdown
    # pra listar TODAS as lojas que VAO ser olhadas (mesmo com qtd=0) — sem
    # essa lista o usuario nao consegue distinguir "loja nao pediu essa
    # receita" de "motor filtrou a loja".
    lojas_op = (Loja.query
                .filter(Loja.ativa.is_(True), Loja.nome != 'Industria')
                .order_by(Loja.nome).all())

    # 1. Estoque da industria por receita.
    em_estoque = defaultdict(int)
    for ep in (EstoqueProducao.query
               .filter(EstoqueProducao.receita_id.isnot(None)).all()):
        em_estoque[ep.receita_id] += int(ep.quantidade or 0)

    # 2. Comprometido: pedidos ainda nao enviados, data_entrega no horizonte.
    comprometido = defaultdict(int)
    comprometido_loja = defaultdict(lambda: defaultdict(int))
    rows = (db.session.query(PedidoItem.receita_id, PedidoLoja.loja_id,
                             PedidoItem.quantidade)
            .join(PedidoLoja, PedidoItem.pedido_id == PedidoLoja.id)
            .filter(PedidoItem.receita_id.isnot(None),
                    PedidoLoja.status.in_(STATUS_PEDIDO_NAO_BAIXADOS),
                    PedidoLoja.data_entrega >= hoje_d,
                    PedidoLoja.data_entrega <= horizonte_fim)
            .all())
    for rid, loja_id, qtd in rows:
        comprometido[rid] += int(qtd or 0)
        comprometido_loja[rid][loja_id] += int(qtd or 0)

    # 3. Historico pra previsao por dia-da-semana. Conta DATAS distintas (nao
    # linhas) pra a media: varias lojas no mesmo dia somam, mas a media e por
    # dia-calendario observado. Exclui cancelados (nao foram demanda real).
    soma_dow = defaultdict(lambda: defaultdict(int))   # rid -> dow -> total
    datas_dow = defaultdict(lambda: defaultdict(set))  # rid -> dow -> {datas}
    soma_total = defaultdict(int)
    datas_total = defaultdict(set)
    pedidos_hist = set()
    datas_hist_global = set()
    hist_rows = (db.session.query(PedidoLoja.id, PedidoItem.receita_id,
                                  PedidoLoja.data_entrega,
                                  PedidoItem.quantidade)
                 .join(PedidoItem, PedidoItem.pedido_id == PedidoLoja.id)
                 .filter(PedidoItem.receita_id.isnot(None),
                         PedidoLoja.status != 'cancelado',
                         PedidoLoja.data_entrega >= hist_ini,
                         PedidoLoja.data_entrega <= hist_fim)
                 .all())
    for pid, rid, data_ent, qtd in hist_rows:
        if data_ent is None:
            continue
        dow = data_ent.weekday()
        q = int(qtd or 0)
        soma_dow[rid][dow] += q
        datas_dow[rid][dow].add(data_ent)
        soma_total[rid] += q
        datas_total[rid].add(data_ent)
        pedidos_hist.add(pid)
        datas_hist_global.add(data_ent)

    dias_calendario_janela = 7 * janela_semanas

    # 4. Previsao: pra cada dia do horizonte, soma a media do dia-da-semana
    # correspondente (com fallback pra media diaria simples). Receita sem
    # historico fica com previsto 0 (produzir vem so do comprometido).
    dias_futuros = [hoje_d + timedelta(days=i) for i in range(horizonte_dias)]
    previsto = defaultdict(float)
    for rid in receitas:
        if not datas_total.get(rid):
            continue
        rid_dow = datas_dow.get(rid, {})
        rid_soma_total = soma_total.get(rid, 0)
        for d in dias_futuros:
            dow = d.weekday()
            datas = rid_dow.get(dow)
            if datas and len(datas) >= _MIN_OCORRENCIAS_DOW:
                previsto[rid] += soma_dow[rid][dow] / len(datas)
            else:
                previsto[rid] += rid_soma_total / dias_calendario_janela

    # 5. Monta itens — so receitas com algum sinal (estoque/comprometido/
    # previsto). Nao listar centenas de receitas zeradas.
    itens = []
    for rid, rec in receitas.items():
        est = em_estoque.get(rid, 0)
        comp = comprometido.get(rid, 0)
        prev = int(ceil(previsto.get(rid, 0)))
        if est == 0 and comp == 0 and prev == 0:
            continue
        demanda = max(comp, prev)
        produzir = max(0, demanda - est)
        itens.append({
            'receita_id': rid,
            'nome': rec.nome,
            'em_estoque': est,
            'comprometido': comp,
            'previsto': prev,
            'produzir': produzir,
            'tem_historico': bool(datas_total.get(rid)),
            # Lista TODAS as lojas operacionais — mesmo com qtd=0. Visivel
            # confirma ao usuario que o motor enxergou cada loja. Ordem: qtd
            # desc, depois alfabetico (lojas_op ja vem ordenado por nome).
            'breakdown_comprometido': sorted(
                [{'loja_id': l.id, 'loja_nome': l.nome,
                  'qtd': comprometido_loja.get(rid, {}).get(l.id, 0)}
                 for l in lojas_op],
                key=lambda b: (-b['qtd'], b['loja_nome']),
            ),
        })

    # Ordena: primeiro o que falta produzir (urgencia), depois maior demanda.
    itens.sort(key=lambda x: (-x['produzir'],
                              -max(x['comprometido'], x['previsto'])))

    # 6. Profundidade do historico (confianca da previsao).
    profundidade = {
        'n_pedidos': len(pedidos_hist),
        'n_datas': len(datas_hist_global),
        'n_semanas_dados': len({(d.isocalendar()[0], d.isocalendar()[1])
                                for d in datas_hist_global}),
        'janela_semanas': janela_semanas,
        'desde': hist_ini.isoformat(),
    }

    resultado = {
        'itens': itens,
        'horizonte_dias': horizonte_dias,
        'janela_semanas': janela_semanas,
        'hoje': hoje_d.isoformat(),
        'horizonte_fim': horizonte_fim.isoformat(),
        'profundidade': profundidade,
        'total_produzir_itens': sum(1 for i in itens if i['produzir'] > 0),
    }
    _CACHE[cache_key] = {'t': time.time(), 'data': resultado}
    return resultado
