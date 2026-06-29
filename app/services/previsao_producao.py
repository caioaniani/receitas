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

# Cronograma: um dia que produz MENOS que esta fracao de uma fornada (rend) rola
# pro proximo dia, pra nao mandar o padeiro acender o forno por 1-2 unidades
# ("pedido picado"). Como e fracao da fornada, receita cuja fornada rende pouco
# (rend pequeno) nao sofre — produzir 1 la ja e uma fornada cheia.
_MIN_FRACAO_FORNADA = 0.2

_CACHE = {}
_CACHE_TTL = 60  # segundos

# Recencia (28/06/2026): a previsao do pedido semanal pesa MAIS as entregas
# recentes (decaimento exponencial) em vez de media uniforme — pega tendencia
# de loja subindo/caindo sem sair de "media dos ultimos pedidos". Meia-vida em
# dias: uma entrega de N dias atras pesa 0.5**(N/_MEIA_VIDA_DIAS). Aumentar
# deixa mais "liso" (no limite vira media uniforme); diminuir reage mais rapido.
_MEIA_VIDA_DIAS = 21


def _media_recencia(qtd_por_data, hoje_d, meia_vida=_MEIA_VIDA_DIAS):
    """Media recencia-ponderada de {data: quantidade}: entrega recente pesa
    mais. Com meia_vida -> infinito recai EXATAMENTE na media uniforme
    (sum/len), entao e uma generalizacao segura da media atual."""
    num = den = 0.0
    for data, q in qtd_por_data.items():
        peso = 0.5 ** (max(0, (hoje_d - data).days) / meia_vida)
        num += peso * q
        den += peso
    return num / den if den else 0.0


def _distribuir_inteiro(total, pesos):
    """Distribui um inteiro `total` entre len(pesos) baldes, proporcional aos
    pesos, somando EXATAMENTE `total` (metodo do maior resto). Usado pra
    espalhar o "Produzir" do balanco pelos dias sem inventar nem perder
    unidades no arredondamento. Pesos todos zero -> tudo no primeiro balde."""
    n = len(pesos)
    if n == 0 or total <= 0:
        return [0] * n
    soma = sum(pesos)
    if soma <= 0:
        return [total] + [0] * (n - 1)
    raw = [total * p / soma for p in pesos]
    base = [int(x) for x in raw]
    resto = total - sum(base)
    # o resto vai pros baldes de maior parte fracionaria
    ordem = sorted(range(n), key=lambda i: raw[i] - base[i], reverse=True)
    for k in range(resto):
        base[ordem[k]] += 1
    return base


def invalidar_sugestao_cache():
    """Forca recalculo no proximo request (chamar quando pedido/estoque da
    industria mudar). Mantido com o nome antigo por compat com chamadores."""
    _CACHE.clear()


def balanco_industria(horizonte_dias=7, janela_semanas=6, usar_cache=True,
                      inicio_offset_dias=0):
    """Balanco de producao da industria por receita.

    Args:
        horizonte_dias: janela futura de planejamento (1-14).
        janela_semanas: profundidade do historico pra previsao (1-26).
        usar_cache: usa o cache de 60s (False forca recalculo).
        inicio_offset_dias: desloca o INICIO do horizonte futuro (0=hoje,
            1=amanha...). O painel usa 1 porque a producao de hoje ja esta
            decidida. O historico continua ancorado em hoje.

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
    inicio_offset_dias = max(0, min(int(inicio_offset_dias or 0), 14))

    cache_key = (horizonte_dias, janela_semanas, inicio_offset_dias)
    if usar_cache:
        ent = _CACHE.get(cache_key)
        if ent and (time.time() - ent['t']) < _CACHE_TTL:
            return ent['data']

    hoje_d = hoje()
    # Inicio do horizonte FUTURO (planejamento). 0 = hoje; o painel usa 1
    # (amanha). O historico abaixo segue ancorado em hoje — passado e passado.
    inicio_d = hoje_d + timedelta(days=inicio_offset_dias)
    horizonte_fim = inicio_d + timedelta(days=horizonte_dias - 1)
    hist_ini = hoje_d - timedelta(days=7 * janela_semanas)
    hist_fim = hoje_d - timedelta(days=1)

    # Receitas ativas (nao arquivadas). Producao = ficha tecnica = Receita.
    receitas = {r.id: r for r in Receita.query
                .filter(Receita.arquivada_em.is_(None)).all()}
    nomes_loja = {l.id: l.nome for l in Loja.query.all()}
    # Lead time de producao por receita (dias). 0 = assa no mesmo dia. Desloca
    # a janela de demanda: "produzir HOJE = entregas em (hoje + lead)". Pra o
    # pao de 48h (lead=2) nao faltar, o plano de hoje ja olha 2 dias a frente.
    # Com tudo em 0 (padrao), o balanco e identico ao comportamento anterior.
    lead = {rid: int(rec.dias_producao or 0) for rid, rec in receitas.items()}
    max_lead = max(lead.values(), default=0)
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

    # 2. Firme por (receita, dia de entrega) — pedidos ainda nao baixados, de
    # HOJE ate o fim da janela de producao+lead. Capturado POR DIA pra:
    #  (a) somar o Comprometido da janela PRODUCIVEL [inicio+lead, ...]; e
    #  (b) medir a demanda IMINENTE (entregas entre hoje e o inicio da janela)
    #      que consome estoque mas nao da mais pra produzir neste horizonte.
    firme_dia = defaultdict(lambda: defaultdict(int))   # rid -> data -> qtd
    comprometido = defaultdict(int)
    comprometido_loja = defaultdict(lambda: defaultdict(int))
    comp_fim = horizonte_fim + timedelta(days=max_lead)
    rows = (db.session.query(PedidoItem.receita_id, PedidoLoja.loja_id,
                             PedidoLoja.data_entrega, PedidoItem.quantidade)
            .join(PedidoLoja, PedidoItem.pedido_id == PedidoLoja.id)
            .filter(PedidoItem.receita_id.isnot(None),
                    PedidoLoja.status.in_(STATUS_PEDIDO_NAO_BAIXADOS),
                    PedidoLoja.data_entrega >= hoje_d,
                    PedidoLoja.data_entrega <= comp_fim)
            .all())
    for rid, loja_id, data_ent, qtd in rows:
        if data_ent is None:
            continue
        q = int(qtd or 0)
        firme_dia[rid][data_ent] += q
        L = lead.get(rid, 0)
        if (inicio_d + timedelta(days=L) <= data_ent
                <= inicio_d + timedelta(days=L + horizonte_dias - 1)):
            comprometido[rid] += q
            comprometido_loja[rid][loja_id] += q

    # 3. Historico pra previsao por dia-da-semana. Conta DATAS distintas (nao
    # linhas) pra a media: varias lojas no mesmo dia somam, mas a media e por
    # dia-calendario observado. Exclui cancelados (nao foram demanda real).
    # rid -> dow -> data -> q (per-data pra media recencia-ponderada)
    qtd_dow = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
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
        qtd_dow[rid][dow][data_ent] += q
        soma_total[rid] += q
        datas_total[rid].add(data_ent)
        pedidos_hist.add(pid)
        datas_hist_global.add(data_ent)

    dias_calendario_janela = 7 * janela_semanas

    # 4. Previsao: pra cada dia da janela de entrega da receita (deslocada pelo
    # lead), soma a media do dia-da-semana correspondente (com fallback pra
    # media diaria simples). Receita sem historico fica com previsto 0
    # (produzir vem so do comprometido).
    previsto = defaultdict(float)
    for rid in receitas:
        if not datas_total.get(rid):
            continue
        rid_dow = qtd_dow.get(rid, {})
        rid_soma_total = soma_total.get(rid, 0)
        L = lead.get(rid, 0)
        dias_rid = [inicio_d + timedelta(days=L + i)
                    for i in range(horizonte_dias)]
        for d in dias_rid:
            dow = d.weekday()
            por_data = rid_dow.get(dow)
            if por_data and len(por_data) >= _MIN_OCORRENCIAS_DOW:
                previsto[rid] += _media_recencia(por_data, hoje_d)
            else:
                previsto[rid] += rid_soma_total / dias_calendario_janela

    def _previsto_dia(rid, dia):
        dow = dia.weekday()
        por_data = qtd_dow.get(rid, {}).get(dow)
        if por_data and len(por_data) >= _MIN_OCORRENCIAS_DOW:
            return _media_recencia(por_data, hoje_d)
        if soma_total.get(rid):
            return soma_total[rid] / dias_calendario_janela
        return 0.0

    # 4b. Demanda IMINENTE: entregas entre HOJE e o inicio da janela de
    # producao de cada receita ([hoje, inicio+lead-1]). Elas CONSOMEM estoque
    # mas nao entram no "produzir" (nao da mais pra produzi-las neste
    # horizonte). Ignorar isso superestimava o estoque disponivel e
    # SUBPRODUZIA (ex: estoque "coberto" por entregas de amanha era contado
    # como livre pra a semana). O estoque efetivo desconta essa demanda.
    pre_demanda = defaultdict(int)
    for rid in receitas:
        L = lead.get(rid, 0)
        d = hoje_d
        fim_pre = inicio_d + timedelta(days=L - 1)
        while d <= fim_pre:
            pre_demanda[rid] += max(int(firme_dia[rid].get(d, 0)),
                                    int(round(_previsto_dia(rid, d))))
            d += timedelta(days=1)

    # 5. Monta itens — so receitas com algum sinal (estoque/comprometido/
    # previsto). Nao listar centenas de receitas zeradas.
    itens = []
    for rid, rec in receitas.items():
        est = em_estoque.get(rid, 0)
        # Estoque efetivo = o que sobra DEPOIS das entregas iminentes (que
        # ja vao consumir estoque antes da janela). E esse que cobre a janela.
        est_efetivo = max(0, est - pre_demanda.get(rid, 0))
        comp = comprometido.get(rid, 0)
        prev = int(ceil(previsto.get(rid, 0)))
        if est == 0 and comp == 0 and prev == 0:
            continue
        demanda = max(comp, prev)
        produzir = max(0, demanda - est_efetivo)
        itens.append({
            'receita_id': rid,
            'nome': rec.nome,
            'em_estoque': est,
            'em_estoque_efetivo': est_efetivo,
            'comprometido': comp,
            'previsto': prev,
            'produzir': produzir,
            'tem_historico': bool(datas_total.get(rid)),
            'dias_producao': lead.get(rid, 0),
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
        'inicio_offset_dias': inicio_offset_dias,
        'hoje': hoje_d.isoformat(),
        'inicio': inicio_d.isoformat(),
        'horizonte_fim': horizonte_fim.isoformat(),
        'profundidade': profundidade,
        'total_produzir_itens': sum(1 for i in itens if i['produzir'] > 0),
    }
    _CACHE[cache_key] = {'t': time.time(), 'data': resultado}
    return resultado


# Dias da semana abreviados em PT-BR (Monday=0 .. Sunday=6, igual weekday()).
_DOW_PT = ['Seg', 'Ter', 'Qua', 'Qui', 'Sex', 'Sáb', 'Dom']


def grade_loja_dia(receita_id, horizonte_dias=7, janela_semanas=6,
                   inicio_offset_dias=0):
    """Grade loja x dia de UMA receita: quanto cada loja recebe em cada dia do
    horizonte. Detalha o que o balanco resume na linha da receita.

    Duas camadas por celula (loja, dia):

    - **firme**: pedidos REAIS (PedidoLoja ainda nao baixados, mesma regra do
      `comprometido` do balanco) com `data_entrega` naquele dia. Exato — e o
      que a loja JA pediu, com data certa.
    - **estimado**: projecao do `previsto` do balanco, decomposta por dia e por
      loja. O previsto diario (mesma formula de `balanco_industria`: media do
      dia-da-semana, fallback media diaria) e rateado entre as lojas
      OPERACIONAIS pela participacao historica de cada uma naquele
      dia-da-semana (fallback: participacao geral). E ESTIMATIVA — nao ha
      pedido por tras; serve pra antecipar de onde a demanda ainda-nao-pedida
      tende a vir.

    Decomposicao top-down: a soma do `estimado` de todas as lojas num dia bate
    com o `previsto` daquele dia (a menos de arredondamento por celula). A
    grade NAO inventa demanda alem da que o balanco ja projeta.

    Lista TODAS as lojas operacionais como linhas (mesmo zeradas) — mesma UX do
    `breakdown_comprometido`: o usuario confirma que o motor olhou cada loja.

    Retorna dict (ou None se a receita nao existir):
        receita_id, receita_nome, horizonte_dias, janela_semanas, hoje,
        tem_historico,
        dias: [{data, label, dow}]                       # colunas
        lojas: [{loja_id, loja_nome, celulas: [{data, firme, estimado}],
                 total_firme, total_estimado}]           # linhas
        totais_dia: [{data, label, firme, estimado}]
        total_firme, total_estimado.
    """
    horizonte_dias = max(1, min(int(horizonte_dias or 7), 14))
    janela_semanas = max(1, min(int(janela_semanas or 6), 26))
    inicio_offset_dias = max(0, min(int(inicio_offset_dias or 0), 14))

    rec = Receita.query.get(receita_id)
    if rec is None:
        return None

    hoje_d = hoje()
    inicio_d = hoje_d + timedelta(days=inicio_offset_dias)
    horizonte_fim = inicio_d + timedelta(days=horizonte_dias - 1)
    hist_ini = hoje_d - timedelta(days=7 * janela_semanas)
    hist_fim = hoje_d - timedelta(days=1)
    dias_futuros = [inicio_d + timedelta(days=i) for i in range(horizonte_dias)]
    dias_calendario_janela = 7 * janela_semanas

    lojas_op = (Loja.query
                .filter(Loja.ativa.is_(True), Loja.nome != 'Industria')
                .order_by(Loja.nome).all())

    # 1. Firme: pedidos nao baixados desta receita, por (loja, data_entrega).
    firme = defaultdict(lambda: defaultdict(int))   # loja_id -> data -> qtd
    rows = (db.session.query(PedidoLoja.loja_id, PedidoLoja.data_entrega,
                             PedidoItem.quantidade)
            .join(PedidoItem, PedidoItem.pedido_id == PedidoLoja.id)
            .filter(PedidoItem.receita_id == receita_id,
                    PedidoLoja.status.in_(STATUS_PEDIDO_NAO_BAIXADOS),
                    PedidoLoja.data_entrega >= inicio_d,
                    PedidoLoja.data_entrega <= horizonte_fim)
            .all())
    for loja_id, data_ent, qtd in rows:
        if data_ent is None:
            continue
        firme[loja_id][data_ent] += int(qtd or 0)

    # 2. Historico desta receita: por (loja, dow), por dow global e por loja
    #    total. Base da projecao diaria E do rateio por loja. Exclui cancelado.
    soma_loja_dow = defaultdict(lambda: defaultdict(int))  # loja -> dow -> q
    qtd_dow = defaultdict(lambda: defaultdict(int))        # dow -> data -> q
    soma_loja_total = defaultdict(int)                     # loja -> q
    soma_total = 0
    datas_total = set()
    hist_rows = (db.session.query(PedidoLoja.loja_id, PedidoLoja.data_entrega,
                                  PedidoItem.quantidade)
                 .join(PedidoItem, PedidoItem.pedido_id == PedidoLoja.id)
                 .filter(PedidoItem.receita_id == receita_id,
                         PedidoLoja.status != 'cancelado',
                         PedidoLoja.data_entrega >= hist_ini,
                         PedidoLoja.data_entrega <= hist_fim)
                 .all())
    for loja_id, data_ent, qtd in hist_rows:
        if data_ent is None:
            continue
        dow = data_ent.weekday()
        q = int(qtd or 0)
        soma_loja_dow[loja_id][dow] += q
        qtd_dow[dow][data_ent] += q
        soma_loja_total[loja_id] += q
        soma_total += q
        datas_total.add(data_ent)

    # 3. Estimado por (loja, dia). Previsto diario igual ao do balanco; rateio
    #    normalizado entre as lojas OPERACIONAIS pra a grade fechar no previsto
    #    (demanda de loja desativada/Industria nao fica orfa — redistribui
    #    proporcional entre as operacionais de hoje).
    soma_op_total = sum(soma_loja_total.get(l.id, 0) for l in lojas_op)
    estimado = defaultdict(lambda: defaultdict(float))  # loja_id -> data -> q
    for d in dias_futuros:
        dow = d.weekday()
        por_data = qtd_dow.get(dow)
        if por_data and len(por_data) >= _MIN_OCORRENCIAS_DOW:
            previsto_dia = _media_recencia(por_data, hoje_d)
        elif soma_total:
            previsto_dia = soma_total / dias_calendario_janela
        else:
            previsto_dia = 0.0
        if previsto_dia <= 0:
            continue
        base_dow_op = sum(soma_loja_dow.get(l.id, {}).get(dow, 0)
                          for l in lojas_op)
        for loja in lojas_op:
            if base_dow_op:
                share = soma_loja_dow.get(loja.id, {}).get(dow, 0) / base_dow_op
            elif soma_op_total:
                share = soma_loja_total.get(loja.id, 0) / soma_op_total
            else:
                share = 0.0
            if share:
                estimado[loja.id][d] = previsto_dia * share

    # 4. Monta linhas (lojas) e colunas (dias). Totais somados das celulas JA
    #    arredondadas, pra o que o usuario ve fechar na conta.
    dias_out = [{'data': d.isoformat(),
                 'label': '%s %s' % (_DOW_PT[d.weekday()], d.strftime('%d/%m')),
                 'dow': d.weekday()} for d in dias_futuros]

    lojas_out = []
    for loja in lojas_op:
        celulas = []
        tot_f = tot_e = 0
        for d in dias_futuros:
            f = int(firme.get(loja.id, {}).get(d, 0))
            e = int(round(estimado.get(loja.id, {}).get(d, 0.0)))
            celulas.append({'data': d.isoformat(), 'firme': f, 'estimado': e})
            tot_f += f
            tot_e += e
        lojas_out.append({
            'loja_id': loja.id, 'loja_nome': loja.nome,
            'celulas': celulas, 'total_firme': tot_f, 'total_estimado': tot_e,
        })

    # Ordena: maior demanda (firme + estimado) primeiro, depois alfabetico.
    lojas_out.sort(key=lambda x: (-(x['total_firme'] + x['total_estimado']),
                                  x['loja_nome']))

    totais_dia = []
    for i, d in enumerate(dias_futuros):
        f = sum(l['celulas'][i]['firme'] for l in lojas_out)
        e = sum(l['celulas'][i]['estimado'] for l in lojas_out)
        totais_dia.append({'data': d.isoformat(), 'label': dias_out[i]['label'],
                           'firme': f, 'estimado': e})

    return {
        'receita_id': receita_id,
        'receita_nome': rec.nome,
        'horizonte_dias': horizonte_dias,
        'janela_semanas': janela_semanas,
        'inicio_offset_dias': inicio_offset_dias,
        'hoje': hoje_d.isoformat(),
        'inicio': inicio_d.isoformat(),
        'tem_historico': bool(datas_total),
        'dias': dias_out,
        'lojas': lojas_out,
        'totais_dia': totais_dia,
        'total_firme': sum(t['firme'] for t in totais_dia),
        'total_estimado': sum(t['estimado'] for t in totais_dia),
    }


def sugerir_pedidos_semana(horizonte_dias=7, janela_semanas=6,
                           inicio_offset_dias=0):
    """Sugere os pedidos da semana POR loja POR dia de ENTREGA, a partir do
    historico — a inversao do fluxo: em vez de cada loja pedir, o sistema
    propoe o pedido e o admin ajusta.

    Datado pela ENTREGA (NAO desloca por lead — lead e da producao, nao do
    pedido da loja). Pra cada (loja, dia, receita) calcula o estimado =
    previsto do dia-da-semana rateado pela participacao historica da loja —
    mesma matematica de `grade_loja_dia`, generalizada pra todas as receitas
    de uma vez (uma query de historico em vez de uma por receita).

    Marca `ja_tem_pedido` quando a loja JA tem pedido nao-cancelado naquela
    data: o gerador pula esses (a loja ja pediu — nao duplica). O total_pedidos
    conta quantos rascunhos seriam criados (loja/dia com item e sem pedido).

    Retorna dict:
        dias: [{data, label, dow}]                       # colunas (cabecalho)
        lojas: [{loja_id, loja_nome, dias: [{data, label, ja_tem_pedido,
                 itens: [{receita_id, nome, qtd}], total}]}]
        horizonte_dias, janela_semanas, hoje, total_pedidos.
    """
    horizonte_dias = max(1, min(int(horizonte_dias or 7), 14))
    janela_semanas = max(1, min(int(janela_semanas or 6), 26))
    inicio_offset_dias = max(0, min(int(inicio_offset_dias or 0), 14))

    hoje_d = hoje()
    inicio_d = hoje_d + timedelta(days=inicio_offset_dias)
    horizonte_fim = inicio_d + timedelta(days=horizonte_dias - 1)
    hist_ini = hoje_d - timedelta(days=7 * janela_semanas)
    hist_fim = hoje_d - timedelta(days=1)
    dias_futuros = [inicio_d + timedelta(days=i) for i in range(horizonte_dias)]
    dias_calendario_janela = 7 * janela_semanas

    receitas = {r.id: r for r in Receita.query
                .filter(Receita.arquivada_em.is_(None)).all()}
    lojas_op = (Loja.query
                .filter(Loja.ativa.is_(True), Loja.nome != 'Industria')
                .order_by(Loja.nome).all())

    # Historico por (receita, loja, dow) + agregados. UMA query pra todas as
    # receitas (mesma logica da grade, mas sem o filtro por receita_id).
    soma_rld = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    # rid -> dow -> data -> q : quantidade por data, pra media recencia-ponderada
    qtd_rd = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    soma_rl = defaultdict(lambda: defaultdict(int))    # rid -> loja -> q
    soma_r = defaultdict(int)                           # rid -> q
    datas_r = defaultdict(set)                          # rid -> {datas}
    hist_rows = (db.session.query(PedidoItem.receita_id, PedidoLoja.loja_id,
                                  PedidoLoja.data_entrega, PedidoItem.quantidade)
                 .join(PedidoLoja, PedidoItem.pedido_id == PedidoLoja.id)
                 .filter(PedidoItem.receita_id.isnot(None),
                         PedidoLoja.status != 'cancelado',
                         PedidoLoja.data_entrega >= hist_ini,
                         PedidoLoja.data_entrega <= hist_fim)
                 .all())
    for rid, loja_id, data_ent, qtd in hist_rows:
        if data_ent is None or rid not in receitas:
            continue
        dow = data_ent.weekday()
        q = int(qtd or 0)
        soma_rld[rid][loja_id][dow] += q
        qtd_rd[rid][dow][data_ent] += q
        soma_rl[rid][loja_id] += q
        soma_r[rid] += q
        datas_r[rid].add(data_ent)

    # Pedidos nao-cancelados por (loja, data) no horizonte — onde a loja JA
    # pediu, nao geramos rascunho (anti-duplicacao).
    ja_tem = set()
    for loja_id, data_ent in (db.session.query(
            PedidoLoja.loja_id, PedidoLoja.data_entrega)
            .filter(PedidoLoja.status != 'cancelado',
                    PedidoLoja.data_entrega >= inicio_d,
                    PedidoLoja.data_entrega <= horizonte_fim)
            .distinct().all()):
        if data_ent is not None:
            ja_tem.add((loja_id, data_ent))

    # sugestao[loja_id][data] = [ {receita_id, nome, qtd} ]
    soma_op_r = {rid: sum(soma_rl[rid].get(l.id, 0) for l in lojas_op)
                 for rid in receitas}
    sugestao = defaultdict(lambda: defaultdict(list))
    for rid, rec in receitas.items():
        if not datas_r.get(rid):
            continue
        for d in dias_futuros:
            dow = d.weekday()
            por_data = qtd_rd[rid].get(dow)
            if por_data and len(por_data) >= _MIN_OCORRENCIAS_DOW:
                previsto_dia = _media_recencia(por_data, hoje_d)
            elif soma_r[rid]:
                previsto_dia = soma_r[rid] / dias_calendario_janela
            else:
                previsto_dia = 0.0
            if previsto_dia <= 0:
                continue
            # Rateio por loja com round() INDEPENDENTE: a fracao marginal de
            # uma loja (< 0,5 un) cai pra 0 e a loja nao entra no pedido. E de
            # proposito — a padaria NAO pulveriza 1-2 un em loja que mal pede o
            # item ("pedidos picados"). NAO trocar por distribuicao de maior
            # resto: a soma exata espalha sobra pras lojas marginais (revertido
            # em 28/06/2026 apos o dono apontar os pedidos picados).
            base_dow_op = sum(soma_rld[rid].get(l.id, {}).get(dow, 0)
                              for l in lojas_op)
            for loja in lojas_op:
                if base_dow_op:
                    share = (soma_rld[rid].get(loja.id, {}).get(dow, 0)
                             / base_dow_op)
                elif soma_op_r[rid]:
                    share = soma_rl[rid].get(loja.id, 0) / soma_op_r[rid]
                else:
                    share = 0.0
                qtd = int(round(previsto_dia * share))
                if qtd > 0:
                    sugestao[loja.id][d].append(
                        {'receita_id': rid, 'nome': rec.nome, 'qtd': qtd})

    dias_out = [{'data': d.isoformat(),
                 'label': '%s %s' % (_DOW_PT[d.weekday()], d.strftime('%d/%m')),
                 'dow': d.weekday()} for d in dias_futuros]

    lojas_out = []
    total_pedidos = 0
    for loja in lojas_op:
        dias_loja = []
        for d in dias_futuros:
            itens = sorted(sugestao.get(loja.id, {}).get(d, []),
                           key=lambda x: x['nome'])
            tem = (loja.id, d) in ja_tem
            if itens and not tem:
                total_pedidos += 1
            dias_loja.append({
                'data': d.isoformat(),
                'label': '%s %s' % (_DOW_PT[d.weekday()], d.strftime('%d/%m')),
                'ja_tem_pedido': tem,
                'itens': itens,
                'total': sum(i['qtd'] for i in itens),
            })
        lojas_out.append({'loja_id': loja.id, 'loja_nome': loja.nome,
                          'dias': dias_loja})

    return {
        'horizonte_dias': horizonte_dias,
        'janela_semanas': janela_semanas,
        'inicio_offset_dias': inicio_offset_dias,
        'hoje': hoje_d.isoformat(),
        'inicio': inicio_d.isoformat(),
        'dias': dias_out,
        'lojas': lojas_out,
        'total_pedidos': total_pedidos,
    }


def cronograma_producao(horizonte_dias=7, janela_semanas=6,
                        inicio_offset_dias=0):
    """Cronograma de producao POR DIA — a MESMA conta do balanco, distribuida.

    O total por receita e EXATAMENTE o "Produzir" do `balanco_industria`
    (mesma janela, mesmo estoque, mesma previsao). O cronograma so ESPALHA esse
    total pelos dias do horizonte seguindo a curva de demanda diaria: producao
    do dia P mira a entrega de (P + lead), e o estoque pronto cobre os dias
    mais proximos primeiro. Assim as duas telas (Painel e Cronograma) nunca se
    contradizem — uma e o agregado, a outra a mesma coisa por dia.

    Retorna dict:
        dias: [{data, label, dow}]                          # dias de producao
        receitas: [{receita_id, nome, dias_producao, em_estoque,
                    por_dia: [{data, qtd, fornadas}], total}]
        hoje, inicio, inicio_offset_dias, horizonte_dias, janela_semanas.
    """
    from app.services.producao import fornadas_amassadeira

    horizonte_dias = max(1, min(int(horizonte_dias or 7), 14))
    janela_semanas = max(1, min(int(janela_semanas or 6), 26))
    inicio_offset_dias = max(0, min(int(inicio_offset_dias or 0), 14))

    # Fonte da verdade do TOTAL por receita: o balanco. O cronograma so
    # distribui esse "Produzir" pelos dias — garante que os totais batem.
    bal = balanco_industria(horizonte_dias=horizonte_dias,
                            janela_semanas=janela_semanas, usar_cache=False,
                            inicio_offset_dias=inicio_offset_dias)

    hoje_d = hoje()
    inicio_d = hoje_d + timedelta(days=inicio_offset_dias)
    hist_ini = hoje_d - timedelta(days=7 * janela_semanas)
    hist_fim = hoje_d - timedelta(days=1)
    dias_calendario_janela = 7 * janela_semanas

    receitas = {r.id: r for r in Receita.query
                .filter(Receita.arquivada_em.is_(None)).all()}
    lead = {rid: int(rec.dias_producao or 0) for rid, rec in receitas.items()}
    max_lead = max(lead.values(), default=0)

    # firme por (receita, dia de entrega) na janela de entrega do horizonte —
    # so pra dar FORMATO a curva diaria (o total ja vem do balanco).
    deliv_fim = inicio_d + timedelta(days=horizonte_dias - 1 + max_lead)
    firme = defaultdict(lambda: defaultdict(int))
    for rid, data_ent, qtd in (db.session.query(
            PedidoItem.receita_id, PedidoLoja.data_entrega,
            PedidoItem.quantidade)
            .join(PedidoLoja, PedidoItem.pedido_id == PedidoLoja.id)
            .filter(PedidoItem.receita_id.isnot(None),
                    PedidoLoja.status.in_(STATUS_PEDIDO_NAO_BAIXADOS),
                    PedidoLoja.data_entrega >= inicio_d,
                    PedidoLoja.data_entrega <= deliv_fim).all()):
        if data_ent is not None:
            firme[rid][data_ent] += int(qtd or 0)

    # historico por (receita, dow) pra o previsto (curva diaria)
    qtd_dow = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    soma_total = defaultdict(int)
    for rid, data_ent, qtd in (db.session.query(
            PedidoItem.receita_id, PedidoLoja.data_entrega,
            PedidoItem.quantidade)
            .join(PedidoLoja, PedidoItem.pedido_id == PedidoLoja.id)
            .filter(PedidoItem.receita_id.isnot(None),
                    PedidoLoja.status != 'cancelado',
                    PedidoLoja.data_entrega >= hist_ini,
                    PedidoLoja.data_entrega <= hist_fim).all()):
        if data_ent is None or rid not in receitas:
            continue
        dow = data_ent.weekday()
        qtd_dow[rid][dow][data_ent] += int(qtd or 0)
        soma_total[rid] += int(qtd or 0)

    def _previsto_dia(rid, dia):
        dow = dia.weekday()
        por_data = qtd_dow[rid].get(dow)
        if por_data and len(por_data) >= _MIN_OCORRENCIAS_DOW:
            return _media_recencia(por_data, hoje_d)
        if soma_total[rid]:
            return soma_total[rid] / dias_calendario_janela
        return 0.0

    dias_prod = [inicio_d + timedelta(days=i) for i in range(horizonte_dias)]
    dias_out = [{'data': d.isoformat(),
                 'label': '%s %s' % (_DOW_PT[d.weekday()], d.strftime('%d/%m')),
                 'dow': d.weekday()} for d in dias_prod]

    receitas_out = []
    for it in bal['itens']:
        rid = it['receita_id']
        produzir = int(it['produzir'])
        if produzir <= 0:
            continue
        rec = receitas.get(rid)
        if rec is None:
            continue
        L = lead.get(rid, 0)
        estoque = int(it['em_estoque'])
        # Estoque EFETIVO (apos as entregas iminentes) e o que cobre a janela —
        # mesmo numero que o balanco usou pra achar o "Produzir". Usar o estoque
        # bruto aqui front-loadaria estoque que ja vai embora antes da janela.
        estoque_efetivo = int(it.get('em_estoque_efetivo', estoque))

        # Curva de demanda diaria: producao do dia i mira a entrega (i + lead).
        gross = []
        for i in range(horizonte_dias):
            entrega = dias_prod[i] + timedelta(days=L)
            gross.append(max(int(firme[rid].get(entrega, 0)),
                             int(round(_previsto_dia(rid, entrega)))))
        # Estoque (efetivo) cobre os dias mais PROXIMOS primeiro -> os primeiros
        # dias produzem menos. O residual (demanda apos estoque) vira o PESO da
        # distribuicao; o total continua sendo o "Produzir" do balanco.
        running = estoque_efetivo
        residual = []
        for g in gross:
            cobre = min(running, g)
            running -= cobre
            residual.append(g - cobre)
        pesos = residual if sum(residual) > 0 else gross
        liquido = _distribuir_inteiro(produzir, pesos)

        rend = int(rec.rendimento_qtd) if rec.rendimento_qtd else 0
        # Anti-"acender fornada por dribble" (ex: produzir 1 pao num dia): um dia
        # que produz menos que uma fracao de fornada rola pro PROXIMO dia,
        # acumulando ate valer um lote. So MOVE (total preservado); o ultimo dia
        # e o sumidouro. Pra rend pequeno o limiar vira 1 e nada rola — produzir
        # 1 ali ja e uma fornada cheia, nao e desperdicio.
        minimo = ceil(rend * _MIN_FRACAO_FORNADA) if rend > 0 else 0
        if minimo > 1:
            for i in range(len(liquido) - 1):
                if 0 < liquido[i] < minimo:
                    liquido[i + 1] += liquido[i]
                    liquido[i] = 0
        por_dia = []
        for i, p in enumerate(dias_prod):
            qtd = liquido[i]
            if qtd > 0 and rend > 0:
                fornadas = fornadas_amassadeira(rec, max(1, ceil(qtd / rend)))
            else:
                fornadas = None
            por_dia.append({'data': p.isoformat(), 'qtd': qtd,
                            'fornadas': fornadas})
        receitas_out.append({
            'receita_id': rid, 'nome': rec.nome, 'dias_producao': L,
            'em_estoque': estoque,
            'por_dia': por_dia, 'total': produzir,
        })

    # Mantem a ordem do balanco (urgencia/demanda) — telas consistentes.
    return {
        'dias': dias_out,
        'receitas': receitas_out,
        'hoje': hoje_d.isoformat(),
        'inicio': inicio_d.isoformat(),
        'inicio_offset_dias': inicio_offset_dias,
        'horizonte_dias': horizonte_dias,
        'janela_semanas': janela_semanas,
    }
