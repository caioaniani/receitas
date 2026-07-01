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
# media daquele dia. Abaixo disso, cai no fallback (media diaria simples). Vale
# tambem por LOJA no pedido semanal: a loja so recebe sugestao de um item se o
# pediu nesse dia-da-semana em >= N datas distintas (1 vez = avulso/errado).
_MIN_OCORRENCIAS_DOW = 2

# Fornada especial (ex: Focaccia): vendida SO sex/sab/dom. weekday(): seg=0 ..
# dom=6 -> sex=4, sab=5, dom=6. O forecast de pedido NAO sugere esses produtos
# em outros dias, mesmo que o historico tenha ruido (1 pedido avulso num dia de
# semana nao vira recorrencia).
_DIAS_FORNADA_ESPECIAL = frozenset({4, 5, 6})

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

# Robustez a PICO ISOLADO (29/06/2026): um pedido pontual gigante (ex: evento)
# nao pode dominar a previsao. Quando a MAIOR ocorrencia eh um pico isolado
# (> _OUTLIER_FATOR x mediana E estritamente acima da 2a maior), ela eh capada
# na 2a maior. Tendencia REAL (2+ valores altos -> a 2a maior tambem eh alta)
# fica intacta, entao a recencia continua pegando loja subindo/caindo.
_OUTLIER_FATOR = 2.5
# Com SO 2 datas nao ha mediana confiavel — exige um salto bem maior pra capar
# (so pico OBVIO de uma vez, ex: 30 e 300). Variacao normal de 2-3x (10 e 30)
# NAO eh capada.
_OUTLIER_FATOR_2PTS = 5.0


def _teto_pico_isolado(qtd_por_data):
    """Teto pra capar um pico ISOLADO: a 2a maior ocorrencia, mas SO quando a
    maior eh um outlier de verdade. Senao retorna +inf (nao capa). 1 ponto: nao
    da pra julgar.

    Com 2 pontos NAO ha mediana confiavel, mas era exatamente a faixa que ficava
    SEM nenhuma protecao (a previsao do dia-da-semana liga com 2 datas) — um
    pedido pontual gigante estourava a previsao por semanas (bug pego 30/06).
    Agora, com 2 pontos, capa se o topo for > _OUTLIER_FATOR x o outro valor
    (pico isolado claro); com 3+ usa a mediana, como antes."""
    valores = sorted(qtd_por_data.values())
    n = len(valores)
    if n < 2:
        return float('inf')
    topo, segundo = valores[-1], valores[-2]
    if n == 2:
        return segundo if (segundo > 0 and topo > _OUTLIER_FATOR_2PTS * segundo) \
            else float('inf')
    mediana = (valores[n // 2] if n % 2
               else (valores[n // 2 - 1] + valores[n // 2]) / 2)
    if mediana > 0 and topo > _OUTLIER_FATOR * mediana and topo > segundo:
        return segundo
    return float('inf')


def _media_recencia(qtd_por_data, hoje_d, meia_vida=_MEIA_VIDA_DIAS,
                    datas_possiveis=None):
    """Media recencia-ponderada de {data: quantidade}: entrega recente pesa mais.
    Pico isolado eh capado (ver _teto_pico_isolado).

    `datas_possiveis`: TODAS as datas daquele dia-da-semana na janela (mesmo as
    SEM pedido). Quando passada, o DENOMINADOR soma o peso de todas elas — os
    sabados sem pedido contam como 0. Sem isso, a media era so sobre as datas
    OBSERVADAS e superestimava demanda INTERMITENTE (3 sabados com 100 + 3 sem
    pedido davam 100, nao 50) e nao decaia quando a demanda PARAVA (sabados
    recentes vazios nao puxavam pra baixo). Bug pego pelo dono (30/06). Sem o
    parametro, mantem o comportamento antigo (denominador so sobre as observadas)."""
    if not qtd_por_data:
        return 0.0
    teto = _teto_pico_isolado(qtd_por_data)
    num = 0.0
    for data, q in qtd_por_data.items():
        qc = q if q <= teto else teto   # capa so a ocorrencia de topo (pico)
        num += (0.5 ** (max(0, (hoje_d - data).days) / meia_vida)) * qc
    if datas_possiveis:
        # Conta os "zeros" SO a partir da 1a vez que o item foi pedido nesse dow:
        # senao um item NOVO / em ramp-up seria penalizado pelas semanas ANTES de
        # existir. Gap DENTRO do periodo ativo (sabado sem pedido) conta; antes do
        # 1o pedido, nao. (Refinamento do A2 — 30/06.)
        ini = min(qtd_por_data)
        base_den = [d for d in datas_possiveis if d >= ini]
    else:
        base_den = qtd_por_data
    den = sum(0.5 ** (max(0, (hoje_d - d).days) / meia_vida) for d in base_den)
    return num / den if den else 0.0


def _datas_por_dow(hist_ini, hist_fim):
    """{dow: [datas]} de TODOS os dias-da-semana no intervalo [hist_ini, hist_fim]
    — o denominador da media recencia (conta os dias SEM pedido como 0, ver
    `_media_recencia`)."""
    out = defaultdict(list)
    d = hist_ini
    while d <= hist_fim:
        out[d.weekday()].append(d)
        d += timedelta(days=1)
    return out


def _taxa_residual(qtd_dow_receita, soma_total_receita, dias_janela):
    """Taxa diaria do volume da receita que NAO tem padrao de dia-da-semana
    confiavel — o que sobra depois de tirar os dows que ja usam a media propria.

    Antes o fallback era `soma_total / dias_janela` cru (A1): pra um item com
    padrao forte de dia (ex: so vende sabado), o volume do sabado entrava no
    `soma_total`, era dividido pelos 42 dias e SOMADO em cada dia util — ou seja,
    contado 2x (uma vez como media do sabado, outra diluido nos dias vazios). Um
    item so-de-sabado de 100 previa ~186 na semana (86% inflado). Tirando do
    numerador o volume dos dows com media (>= _MIN_OCORRENCIAS_DOW datas), os dias
    sem padrao recebem so o RESIDUO real; item de giro baixo SEM nenhum dow
    dominante (todos < 2) mantem a media diaria antiga (residuo == soma_total)."""
    soma_mean = sum(sum(datas.values()) for datas in qtd_dow_receita.values()
                    if len(datas) >= _MIN_OCORRENCIAS_DOW)
    residual = max(0, soma_total_receita - soma_mean)
    return residual / dias_janela if dias_janela else 0.0


def _previsto_dow(por_data, hoje_d, residual_rate, datas_possiveis=None):
    """Previsao de UM dia pelo historico do MESMO dia-da-semana. Com dados
    suficientes naquele dow (>= _MIN_OCORRENCIAS_DOW datas) usa a media recencia-
    ponderada do dow; senao usa a `residual_rate` (taxa do volume SEM padrao de
    dow, ver `_taxa_residual`) — nao a media diaria crua, que misturava escalas e
    inflava item com padrao de dia-da-semana (A1)."""
    if por_data and len(por_data) >= _MIN_OCORRENCIAS_DOW:
        return _media_recencia(por_data, hoje_d, datas_possiveis=datas_possiveis)
    return residual_rate


def _fornada_no_dia(rec, dia):
    """True se a receita PODE ser vendida/projetada nesse dia. Fornada especial
    (ex: Focaccia) só sex/sáb/dom -> False nos outros dias (não projeta demanda;
    o produto não é vendido nesse dia). Receita normal -> sempre True."""
    return not (rec is not None
                and getattr(rec, 'fornada_especial', False)
                and dia.weekday() not in _DIAS_FORNADA_ESPECIAL)


def _padronizar_qtd(qtd, lote, minimo):
    """Arredonda a sugestao pro LOTE de pedido da receita (pacote padrao) e
    aplica o piso. 'Nao pedir picado' (decisao do dono 29/06): a loja pede em
    pacotes inteiros. lote None/<=1 -> sem padronizacao (passthrough). Arredonda
    pro multiplo MAIS PROXIMO; loja que pede o item (qtd>0) recebe ao menos 1
    pacote. minimo (se >0) so vale quando ja ha pedido (qtd>0)."""
    qtd = int(qtd)
    if qtd <= 0:
        return 0
    lote = int(lote or 0)
    if lote > 1:
        qtd = int(round(qtd / lote)) * lote or lote   # nunca menos que 1 pacote
    minimo = int(minimo or 0)
    if minimo > 0 and qtd < minimo:
        qtd = minimo
    return qtd


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
    datas_possiveis_dow = _datas_por_dow(hist_ini, hist_fim)   # denom da media

    # 4. Previsao: pra cada dia da janela de entrega da receita (deslocada pelo
    # lead), soma a media do dia-da-semana correspondente (com fallback pra taxa
    # RESIDUAL — volume sem padrao de dow, sem o double-count do A1). Receita sem
    # historico fica com previsto 0 (produzir vem so do comprometido).
    residual_rate = {rid: _taxa_residual(qtd_dow.get(rid, {}), soma_total.get(rid, 0),
                                         dias_calendario_janela)
                     for rid in receitas}
    previsto = defaultdict(float)
    for rid in receitas:
        if not datas_total.get(rid):
            continue
        rid_dow = qtd_dow.get(rid, {})
        L = lead.get(rid, 0)
        dias_rid = [inicio_d + timedelta(days=L + i)
                    for i in range(horizonte_dias)]
        rec_rid = receitas.get(rid)
        for d in dias_rid:
            if not _fornada_no_dia(rec_rid, d):
                continue   # fornada especial fora de sex/sáb/dom: não projeta
            dow = d.weekday()
            previsto[rid] += _previsto_dow(
                rid_dow.get(dow), hoje_d, residual_rate[rid],
                datas_possiveis=datas_possiveis_dow[dow])

    def _previsto_dia(rid, dia):
        if not _fornada_no_dia(receitas.get(rid), dia):
            return 0.0
        dow = dia.weekday()
        return _previsto_dow(
            qtd_dow.get(rid, {}).get(dow), hoje_d, residual_rate.get(rid, 0.0),
            datas_possiveis=datas_possiveis_dow[dow])

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
_DOW_PT_LONGO = ['segunda-feira', 'terça-feira', 'quarta-feira', 'quinta-feira',
                 'sexta-feira', 'sábado', 'domingo']


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
    residual_rate = _taxa_residual(qtd_dow, soma_total, dias_calendario_janela)
    estimado = defaultdict(lambda: defaultdict(float))  # loja_id -> data -> q
    for d in dias_futuros:
        dow = d.weekday()
        previsto_dia = _previsto_dow(qtd_dow.get(dow), hoje_d, residual_rate)
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

    # So receitas que a loja PEDE (exclui insumo/etapa de producao, ex: Creme
    # de Amendoas, que vai dentro do Croissant Almond e nunca e pedido direto).
    receitas = {r.id: r for r in Receita.query
                .filter(Receita.arquivada_em.is_(None),
                        Receita.sugerir_pedido_loja.isnot(False)).all()}
    lojas_op = (Loja.query
                .filter(Loja.ativa.is_(True), Loja.nome != 'Industria')
                .order_by(Loja.nome).all())

    # Historico por (receita, loja, dow, data): quanto CADA loja pediu do item
    # naquele dia-da-semana, por data — base da media recencia-ponderada POR
    # LOJA (cada loja prevista do historico DELA, nao do total da operacao).
    # UMA query pra todas as receitas.
    qtd_rld = defaultdict(lambda: defaultdict(
        lambda: defaultdict(lambda: defaultdict(int))))  # rid->loja->dow->data->q
    datas_r = defaultdict(set)                            # rid -> {datas} (skip)
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
        qtd_rld[rid][loja_id][data_ent.weekday()][data_ent] += int(qtd or 0)
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
    sugestao = defaultdict(lambda: defaultdict(list))
    for rid, rec in receitas.items():
        if not datas_r.get(rid):
            continue
        # Fornada especial só é vendida sex/sáb/dom — não sugere em outro dia.
        fornada_especial = bool(getattr(rec, 'fornada_especial', False))
        for d in dias_futuros:
            dow = d.weekday()
            if fornada_especial and dow not in _DIAS_FORNADA_ESPECIAL:
                continue
            for loja in lojas_op:
                # Previsao da LOJA a partir do historico DELA naquele dia-da-
                # semana (media recencia-ponderada das datas em que ela pediu).
                # NAO usamos "total da operacao x participacao": aquilo dividia o
                # pedido da loja pelo nº de datas em que QUALQUER loja pediu —
                # diluindo a loja que pede em MENOS semanas (o "pedido picado de
                # cookie", 29/06/2026: a loja recebia metade do que costuma
                # pedir). Aqui cada loja recebe o tamanho TIPICO do pedido dela.
                #
                # Exige >= _MIN_OCORRENCIAS_DOW datas nesse dow: 1 vez e pedido
                # avulso (mata o "1 creme de amendoas" pedido sem querer) e
                # abaixo disso nao ha media confiavel. Sem rateio do total ->
                # nao ha sobra de arredondamento pulverizada em loja marginal.
                por_data = qtd_rld[rid].get(loja.id, {}).get(dow)
                if not por_data or len(por_data) < _MIN_OCORRENCIAS_DOW:
                    continue
                qtd = int(round(_media_recencia(por_data, hoje_d)))
                # Padroniza no pacote da receita (nao pedir picado).
                qtd = _padronizar_qtd(qtd, rec.lote_pedido, rec.minimo_pedido)
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


def media_semanal_pedidos(horizonte_dias=7, janela_semanas=6,
                          inicio_offset_dias=0):
    """Modo MANUAL: devolve a media de cada (loja, produto) por DIA-DA-SEMANA — o
    sinal ESTAVEL, respeitando o PADRAO da loja (sabado != terca) — distribuida
    pelos dias LIVRES do horizonte, pro admin AJUSTAR na tela. media por dow =
    total daquele dia-da-semana na janela / nº de semanas; a soma sobre a semana
    reconstroi a media semanal. So distribui em dias que a loja ainda NAO pediu
    (dia travado vem disabled na tela e nao seria enviado no POST — alocar nele
    perderia a parcela em silencio). O gerar reusa o POST de pedidos_semana_gerar.

    Retorna dict:
        dias: [{data, label, dow}]
        lojas: [{loja_id, loja_nome, produtos: [{receita_id, nome,
                 media_semanal, por_dia: [qtd por dia], total}],
                 ja_tem: [data_iso, ...]}]   # dias que a loja JA pediu
        horizonte_dias, janela_semanas, inicio_offset_dias, hoje, inicio.
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

    receitas = {r.id: r for r in Receita.query
                .filter(Receita.arquivada_em.is_(None),
                        Receita.sugerir_pedido_loja.isnot(False)).all()}
    lojas_op = (Loja.query
                .filter(Loja.ativa.is_(True), Loja.nome != 'Industria')
                .order_by(Loja.nome).all())

    # Estoque atual DISPONIVEL por (loja, receita) = fisico - reservado (a reserva
    # segura pedido online aguardando pagamento). MESMA conta da tela venda+estoque
    # — so pra MOSTRAR na coluna Estoque (nao entra no calculo da media, que e o
    # sinal estavel de venda; o admin cruza os dois a olho).
    from app.models import EstoqueLoja
    estoque_atual = defaultdict(lambda: defaultdict(int))
    for loja_id, rid, q, qres in (db.session.query(
            EstoqueLoja.loja_id, EstoqueLoja.receita_id,
            EstoqueLoja.quantidade, EstoqueLoja.quantidade_reservada)
            .filter(EstoqueLoja.receita_id.isnot(None)).all()):
        estoque_atual[loja_id][rid] += max(0, int(q or 0) - int(qres or 0))

    # Venda historica por (loja, receita, DOW): total pedido naquele dia-da-semana
    # na janela. Base da media POR DIA-DA-SEMANA (sabado != terca).
    soma_lrd = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    for rid, loja_id, data_ent, qtd in (db.session.query(
            PedidoItem.receita_id, PedidoLoja.loja_id,
            PedidoLoja.data_entrega, PedidoItem.quantidade)
            .join(PedidoLoja, PedidoItem.pedido_id == PedidoLoja.id)
            .filter(PedidoItem.receita_id.isnot(None),
                    PedidoLoja.status != 'cancelado',
                    PedidoLoja.data_entrega >= hist_ini,
                    PedidoLoja.data_entrega <= hist_fim).all()):
        if data_ent is None or rid not in receitas:
            continue
        soma_lrd[loja_id][rid][data_ent.weekday()] += int(qtd or 0)

    # Dias que a loja JA tem pedido no horizonte (o gerar pula; a tela marca).
    ja_tem = defaultdict(set)
    for loja_id, data_ent in (db.session.query(
            PedidoLoja.loja_id, PedidoLoja.data_entrega)
            .filter(PedidoLoja.status != 'cancelado',
                    PedidoLoja.data_entrega >= inicio_d,
                    PedidoLoja.data_entrega <= horizonte_fim)
            .distinct().all()):
        if data_ent is not None:
            ja_tem[loja_id].add(data_ent.isoformat())

    dias_out = [{'data': d.isoformat(),
                 'label': '%s %s' % (_DOW_PT[d.weekday()], d.strftime('%d/%m')),
                 'dow': d.weekday()} for d in dias_futuros]

    lojas_out = []
    for loja in lojas_op:
        ja_tem_loja = ja_tem.get(loja.id, set())
        produtos = []
        for rid, rec in sorted(receitas.items(), key=lambda kv: kv[1].nome):
            dows = soma_lrd.get(loja.id, {}).get(rid)
            if not dows:
                continue
            # media por dia-da-semana = total daquele dow / nº semanas. A soma
            # sobre a semana reconstroi a media semanal estavel; o split por dow
            # respeita o PADRAO da loja (em que dia ela costuma pedir).
            media_por_dow = {dow: tot / janela_semanas for dow, tot in dows.items()}
            media_sem = sum(media_por_dow.values())   # = total_janela / semanas

            fe = bool(getattr(rec, 'fornada_especial', False))
            # Dias onde a sugestao PODE cair: fornada especial respeitada E dia
            # LIVRE (loja ainda nao pediu). Travado NAO recebe — alocar nele
            # perderia a parcela (input disabled nao vai no POST).
            idx_validos = [
                i for i, d in enumerate(dias_futuros)
                if not (fe and d.weekday() not in _DIAS_FORNADA_ESPECIAL)
                and d.isoformat() not in ja_tem_loja
            ]
            if not idx_validos:
                continue
            # Peso de cada dia livre = a media DAQUELE dia-da-semana. total_alocar
            # = soma das medias dos dias livres (so o que vai mesmo pra eles; nada
            # se perde em dia travado).
            pesos = [media_por_dow.get(dias_futuros[i].weekday(), 0.0)
                     for i in idx_validos]
            total_alocar = int(round(sum(pesos)))
            if total_alocar <= 0:
                continue
            caixa = int(rec.lote_pedido or 0)
            por_dia = [0] * len(dias_futuros)
            abaixo_lote = False
            if caixa > 1 and total_alocar >= caixa:
                # Fecha >= 1 caixa: distribui em CAIXAS inteiras, ponderadas pelo
                # dia-da-semana (mais caixas no pico).
                n_lotes = int(round(total_alocar / caixa))
                partes = _distribuir_inteiro(n_lotes, pesos)
                for k, i in enumerate(idx_validos):
                    por_dia[i] = partes[k] * caixa
            elif caixa > 1 and total_alocar > 0:
                # Demanda ABAIXO de 1 caixa: NAO forca a caixa (item lento). Mostra
                # a media real ponderada por dow + flag pro admin decidir.
                abaixo_lote = True
                partes = _distribuir_inteiro(total_alocar, pesos)
                for k, i in enumerate(idx_validos):
                    por_dia[i] = partes[k]
            else:
                # Sem regra de caixa: distribui ponderado por dia-da-semana.
                partes = _distribuir_inteiro(total_alocar, pesos)
                for k, i in enumerate(idx_validos):
                    por_dia[i] = partes[k]
            produtos.append({
                'receita_id': rid, 'nome': rec.nome,
                'media_semanal': round(media_sem, 1),
                'estoque_atual': estoque_atual[loja.id].get(rid, 0),
                'por_dia': por_dia, 'total': sum(por_dia),
                'lote': caixa,                       # caixa: arredonda ao dividir
                'minimo': int(rec.minimo_pedido or 0),
                'abaixo_lote': abaixo_lote,
            })
        if produtos:
            lojas_out.append({
                'loja_id': loja.id, 'loja_nome': loja.nome,
                'produtos': produtos,
                'ja_tem': sorted(ja_tem_loja),
            })

    return {
        'horizonte_dias': horizonte_dias,
        'janela_semanas': janela_semanas,
        'inicio_offset_dias': inicio_offset_dias,
        'hoje': hoje_d.isoformat(),
        'inicio': inicio_d.isoformat(),
        'dias': dias_out,
        'lojas': lojas_out,
    }


# Tipos de MovEstoqueLoja que representam DEMANDA de venda da loja (gross): a
# baixa real + o que faltou (stockout = demanda nao atendida). Estornos ficam de
# fora (cancelamento raro; refinar depois). Base unica do motor de baixa.
_DEMANDA_VENDA_TIPOS = (
    'venda_seru', 'venda_seru_sem_estoque',
    'venda_site', 'venda_site_sem_estoque',
    'saida_lote', 'venda_loja_sem_estoque',
)


def sugerir_pedidos_por_venda(horizonte_dias=7, janela_semanas=6,
                              inicio_offset_dias=0):
    """Maneira 2 — previsao de pedido por VENDA + ESTOQUE (ponto de reposicao).

    Pra cada (loja, receita): mede a venda media POR DIA-DA-SEMANA (movimentos de
    baixa do EstoqueLoja) e simula o estoque dia a dia partindo do saldo ATUAL.
    Quando o estoque projetado nao cobre a venda do dia, pede o deficit
    ARREDONDADO PRA CIMA na caixa (lote) — o excedente vira estoque que cobre os
    proximos dias, entao a caixa NAO super-pede item lento (pede 1 caixa a cada
    N dias). Entrega diaria (v1): cada dia cobre a venda daquele dia.

    Mesma forma de retorno que `media_semanal_pedidos` (+ `estoque_atual` por
    produto), pro mesmo template/gerar.
    """
    from datetime import datetime, time
    from math import ceil

    from app.models import EstoqueLoja, MovEstoqueLoja

    horizonte_dias = max(1, min(int(horizonte_dias or 7), 14))
    janela_semanas = max(1, min(int(janela_semanas or 6), 26))
    inicio_offset_dias = max(0, min(int(inicio_offset_dias or 0), 14))

    hoje_d = hoje()
    inicio_d = hoje_d + timedelta(days=inicio_offset_dias)
    horizonte_fim = inicio_d + timedelta(days=horizonte_dias - 1)
    hist_ini = hoje_d - timedelta(days=7 * janela_semanas)
    hist_fim = hoje_d - timedelta(days=1)
    dias_futuros = [inicio_d + timedelta(days=i) for i in range(horizonte_dias)]

    receitas = {r.id: r for r in Receita.query
                .filter(Receita.arquivada_em.is_(None),
                        Receita.sugerir_pedido_loja.isnot(False)).all()}
    lojas_op = (Loja.query
                .filter(Loja.ativa.is_(True), Loja.nome != 'Industria')
                .order_by(Loja.nome).all())

    # A tela cobre receitas E materias-primas que a loja estoca/vende/pede
    # (ex: pao de queijo congelado, comprado em saco e vendido via cones —
    # a venda do cone baixa a linha MP da loja). Token unico por item:
    # receita = o proprio id (int, compat com o gerar existente);
    # MP = 'mp:<id>' (o gerar reconhece o prefixo).
    def _token(rid, mid):
        if rid is not None:
            return rid if rid in receitas else None
        return f'mp:{mid}' if mid is not None else None

    mp_ids = set()

    # Venda por (loja, item, dow) na janela: MovEstoqueLoja x EstoqueLoja (a
    # linha diz loja+item); dow = dia-da-semana da venda.
    venda_dow = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    for loja_id, rid, mid, data_mov, qtd in (db.session.query(
            EstoqueLoja.loja_id, EstoqueLoja.receita_id,
            EstoqueLoja.materia_prima_id,
            MovEstoqueLoja.data, MovEstoqueLoja.quantidade)
            .join(EstoqueLoja, MovEstoqueLoja.estoque_loja_id == EstoqueLoja.id)
            .filter(db.or_(EstoqueLoja.receita_id.isnot(None),
                           EstoqueLoja.materia_prima_id.isnot(None)),
                    MovEstoqueLoja.tipo.in_(_DEMANDA_VENDA_TIPOS),
                    MovEstoqueLoja.data >= datetime.combine(hist_ini, time.min),
                    MovEstoqueLoja.data <= datetime.combine(hist_fim, time.max))
            .all()):
        tok = _token(rid, mid)
        if tok is not None and data_mov is not None:
            venda_dow[loja_id][tok][data_mov.weekday()] += int(qtd or 0)
            if mid is not None:
                mp_ids.add(mid)

    # Estoque DISPONIVEL da loja por (loja, item) = quantidade - reservado
    # (reservado segura pedido online aguardando pagamento). Usar o fisico
    # contaria reserva como disponivel e sub-pediria.
    estoque_atual = defaultdict(lambda: defaultdict(int))
    for loja_id, rid, mid, q, qres in (db.session.query(
            EstoqueLoja.loja_id, EstoqueLoja.receita_id,
            EstoqueLoja.materia_prima_id,
            EstoqueLoja.quantidade, EstoqueLoja.quantidade_reservada)
            .filter(db.or_(EstoqueLoja.receita_id.isnot(None),
                           EstoqueLoja.materia_prima_id.isnot(None))).all()):
        tok = _token(rid, mid)
        if tok is None:
            continue
        estoque_atual[loja_id][tok] += max(0, int(q or 0) - int(qres or 0))
        if mid is not None:
            mp_ids.add(mid)

    # Produtos que a loja PEDE da industria (historico de pedidos na janela).
    # A previsao por venda so "ve" o que teve baixa registrada; sem isto, um item
    # que a loja pede mas cuja venda nao esta rastreada (mapa Seru incompleto)
    # sumiria da tela. Incluimos pra MOSTRAR TODOS com sugestao 0 (decisao do
    # dono) — nada e esquecido; o operador preenche na mao o que faltar.
    pede_receitas = defaultdict(set)
    for loja_id, rid_p, mid_p in (db.session.query(
            PedidoLoja.loja_id, PedidoItem.receita_id,
            PedidoItem.materia_prima_id)
            .join(PedidoItem, PedidoItem.pedido_id == PedidoLoja.id)
            .filter(db.or_(PedidoItem.receita_id.isnot(None),
                           PedidoItem.materia_prima_id.isnot(None)),
                    PedidoLoja.status != 'cancelado',
                    PedidoLoja.data_entrega >= hist_ini,
                    PedidoLoja.data_entrega <= hist_fim)
            .distinct().all()):
        tok = _token(rid_p, mid_p)
        if tok is not None:
            pede_receitas[loja_id].add(tok)
            if mid_p is not None:
                mp_ids.add(mid_p)

    # Dias ja pedidos no horizonte (a tela trava; o gerar pula) + a QUANTIDADE ja
    # pedida por (loja, data, receita) — pra simulacao usar a entrega real do dia
    # travado como carry, em vez da sugestao (que nao sera criada).
    ja_tem = defaultdict(set)
    pedido_existente = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    for loja_id, data_ent in (db.session.query(
            PedidoLoja.loja_id, PedidoLoja.data_entrega)
            .filter(PedidoLoja.status != 'cancelado',
                    PedidoLoja.data_entrega >= inicio_d,
                    PedidoLoja.data_entrega <= horizonte_fim)
            .distinct().all()):
        if data_ent is not None:
            ja_tem[loja_id].add(data_ent.isoformat())
    for loja_id, data_ent, rid_e, qtd_e in (db.session.query(
            PedidoLoja.loja_id, PedidoLoja.data_entrega,
            PedidoItem.receita_id, PedidoItem.quantidade)
            .join(PedidoItem, PedidoItem.pedido_id == PedidoLoja.id)
            .filter(PedidoLoja.status != 'cancelado',
                    PedidoItem.receita_id.isnot(None),
                    PedidoLoja.data_entrega >= inicio_d,
                    PedidoLoja.data_entrega <= horizonte_fim).all()):
        if data_ent is not None:
            pedido_existente[loja_id][data_ent.isoformat()][rid_e] += int(qtd_e or 0)

    dias_out = [{'data': d.isoformat(),
                 'label': '%s %s' % (_DOW_PT[d.weekday()], d.strftime('%d/%m')),
                 'dow': d.weekday()} for d in dias_futuros]

    lojas_out = []
    for loja in lojas_op:
        ja_tem_loja = ja_tem.get(loja.id, set())
        pede_loja = pede_receitas.get(loja.id, set())
        produtos = []
        for rid, rec in sorted(receitas.items(), key=lambda kv: kv[1].nome):
            dows = venda_dow.get(loja.id, {}).get(rid)
            est0 = estoque_atual.get(loja.id, {}).get(rid, 0)
            pede = rid in pede_loja
            if not dows and est0 <= 0 and not pede:
                continue                          # nao vende, sem estoque, nem pede
            caixa = int(rec.lote_pedido or 0)
            fe = bool(getattr(rec, 'fornada_especial', False))
            estoque = est0
            por_dia = [0] * len(dias_futuros)
            venda_total = 0.0
            for i, d in enumerate(dias_futuros):
                if fe and d.weekday() not in _DIAS_FORNADA_ESPECIAL:
                    continue                      # fornada especial: nao vende
                venda_d = (dows.get(d.weekday(), 0) / janela_semanas) if dows else 0.0
                venda_total += venda_d
                if d.isoformat() in ja_tem_loja:
                    # Dia travado: a tela nao deixa sugerir e o gerar pula. O
                    # estoque projetado recebe a ENTREGA JA PEDIDA (qtd real),
                    # nao a sugestao — senao os dias seguintes herdariam uma
                    # reposicao que nao vai existir (sub-pedido) ou ignorariam a
                    # entrega real (super-pedido).
                    entrega = pedido_existente.get(loja.id, {}).get(
                        d.isoformat(), {}).get(rid, 0)
                    por_dia[i] = 0
                    estoque = estoque + entrega - venda_d
                    continue
                deficit = venda_d - estoque
                if deficit > 1e-9:
                    pedido = (int(ceil(deficit / caixa)) * caixa
                              if caixa > 1 else int(ceil(deficit)))
                else:
                    pedido = 0
                por_dia[i] = pedido
                estoque = estoque + pedido - venda_d
            # Mostra TODOS os produtos do "mundo" da loja (vende/estoca/pede),
            # mesmo com sugestao 0 (decisao do dono: nada some da tela). So pula
            # o que nem venda, nem estoque, nem pedido tem (ja filtrado acima).
            produtos.append({
                'receita_id': rid, 'nome': rec.nome,
                'media_semanal': round(venda_total * 7.0 / horizonte_dias, 1),
                'estoque_atual': est0,
                'por_dia': por_dia, 'total': sum(por_dia),
                'lote': caixa,
                'minimo': int(rec.minimo_pedido or 0),
                'abaixo_lote': False,
            })
        if produtos:
            lojas_out.append({
                'loja_id': loja.id, 'loja_nome': loja.nome,
                'produtos': produtos,
                'ja_tem': sorted(ja_tem.get(loja.id, set())),
            })

    return {
        'horizonte_dias': horizonte_dias,
        'janela_semanas': janela_semanas,
        'inicio_offset_dias': inicio_offset_dias,
        'hoje': hoje_d.isoformat(),
        'inicio': inicio_d.isoformat(),
        'dias': dias_out,
        'lojas': lojas_out,
    }


def _explodir_bom(receitas_out, dias_prod, receitas, lead, bal):
    """MRP: explode sub-receitas (ReceitaIngrediente tipo='receita') em linhas de
    producao proprias no cronograma. Produto final que consome uma sub-receita
    gera demanda dela; a sub e produzida ANTES (pelo lead dela) pra estar pronta
    quando o pai for produzido (ex: massa para folhar no dia 1 -> croissant no
    dia 2). RECURSIVO (sub de sub) e SOMA por sub usada por varios finais
    (massa do croissant + danish + pain). Receita VENDIDA que tambem e insumo
    (ex: croissant tradicional, vendido E consumido pelo croissant almond)
    acumula a demanda dos pais na linha dela. Desconta o estoque da sub
    (geladeira) e respeita o lead. Muta receitas_out. No-op sem sub-receita."""
    from collections import deque

    from app.models import EstoqueProducao
    from app.services.producao import fornadas_amassadeira

    n = len(dias_prod)
    if not receitas_out or n == 0:
        return

    def _subs(rid):
        from app.services.massa_base import rendimento_massa_crua
        rec = receitas.get(rid)
        if rec is None:
            return []
        rend = rendimento_massa_crua(rec)
        out = []
        for ing in rec.ingredientes:
            if (ing.tipo or '') != 'receita':
                continue
            sid = ing.sub_receita_id
            if sid is None:                       # fallback por nome exato
                alvo = (ing.ingrediente_nome or '').strip().lower()
                sid = next((r.id for r in receitas.values()
                            if (r.nome or '').strip().lower() == alvo), None)
            if sid in receitas and rend > 0:
                out.append((sid, (ing.porcentagem or 0) / rend))
        return out

    # BOM transitivo a partir dos finais.
    bom = {}
    pilha = [rr['receita_id'] for rr in receitas_out]
    while pilha:
        rid = pilha.pop()
        if rid in bom:
            continue
        bom[rid] = _subs(rid)
        for sid, _ in bom[rid]:
            if sid not in bom:
                pilha.append(sid)
    if not any(bom.get(rr['receita_id']) for rr in receitas_out):
        return   # nenhum final tem sub-receita -> caminho normal

    # Ordem topologica: pai antes da sub (indeg = nº de pais).
    indeg = {rid: 0 for rid in bom}
    for rid in bom:
        for sid, _ in bom[rid]:
            indeg[sid] = indeg.get(sid, 0) + 1
    fila = deque(rid for rid in bom if indeg[rid] == 0)
    ordem = []
    while fila:
        rid = fila.popleft()
        ordem.append(rid)
        for sid, _ in bom.get(rid, []):
            indeg[sid] -= 1
            if indeg[sid] == 0:
                fila.append(sid)

    linhas = {rr['receita_id']: rr for rr in receitas_out}
    prod = {rr['receita_id']: [c['qtd'] for c in rr['por_dia']]
            for rr in receitas_out}
    consumo = defaultdict(lambda: [0.0] * n)
    # insumo -> pai -> {'pai': producao do pai, 'insumo': qtd de insumo gerada}
    consumo_origem = defaultdict(lambda: defaultdict(
        lambda: {'pai': 0.0, 'insumo': 0.0}))

    # Estoque (geladeira) das sub-receitas que nao estao no balanco.
    bal_map = {it['receita_id']: it for it in bal['itens']}
    sem_bal = [rid for rid in bom if rid not in bal_map]
    est_extra = defaultdict(int)
    if sem_bal:
        for ep in (EstoqueProducao.query
                   .filter(EstoqueProducao.receita_id.in_(sem_bal)).all()):
            est_extra[ep.receita_id] += int(ep.quantidade or 0)

    def _estoque_livre(rid):
        """Estoque da receita disponivel PRA OS PAIS (alem da demanda propria)."""
        it = bal_map.get(rid)
        if it is not None:
            efetivo = int(it.get('em_estoque_efetivo', it.get('em_estoque', 0)) or 0)
            demanda = max(int(it.get('comprometido', 0) or 0),
                          int(it.get('previsto', 0) or 0))
            return max(0, efetivo - demanda)
        return est_extra.get(rid, 0)

    for rid in ordem:
        cons = consumo[rid]
        if sum(cons) > 0:                          # recebeu demanda de pais
            L = lead.get(rid, 0)
            # producao do dia i serve o consumo em (i+L); consumo antes do
            # horizonte cai no dia 0 (produzir o quanto antes).
            gross = [0.0] * n
            for d_idx in range(n):
                if cons[d_idx] > 0:
                    gross[max(0, d_idx - L)] += cons[d_idx]
            # NAO arredonda por dia (era o D1): dar ceil em CADA dia inflava
            # insumo de fracao baixa — "Massa para folhar" ~0,6/dia virava 1/dia
            # (67% a mais). A fracao ACUMULA entre os dias; produz o inteiro do
            # TOTAL (ceil da demanda liquida) distribuido, nao a soma dos ceils.
            livre = _estoque_livre(rid)
            running = livre
            residual = []
            for g in gross:                          # gross FRACIONARIO
                cobre = min(running, g)
                running -= cobre
                residual.append(g - cobre)
            extra = int(ceil(max(0.0, sum(gross) - livre)))
            pesos = residual if sum(residual) > 0 else gross
            add = _distribuir_inteiro(extra, pesos)
            rec = receitas.get(rid)
            from app.services.massa_base import rendimento_massa_crua
            rend = rendimento_massa_crua(rec) if rec else 1.0
            base = prod.get(rid, [0] * n)
            novo = [base[i] + add[i] for i in range(n)]
            prod[rid] = novo

            def _forn(q, rec=rec, rend=rend):
                return (fornadas_amassadeira(rec, max(1, ceil(q / rend)))
                        if q > 0 and rend > 0 else None)

            rr = linhas.get(rid)
            if rr is None:                         # sub-receita nao vendida
                por_dia = [{'data': dias_prod[i].isoformat(), 'qtd': novo[i],
                            'fornadas': _forn(novo[i])} for i in range(n)]
                rr = {'receita_id': rid, 'nome': rec.nome if rec else '(sub)',
                      'dias_producao': L, 'em_estoque': est_extra.get(rid, 0),
                      'por_dia': por_dia, 'total': sum(novo), 'insumo': True}
                receitas_out.append(rr)
                linhas[rid] = rr
            else:                                  # vendida + insumo: acumula
                for i, c in enumerate(rr['por_dia']):
                    c['qtd'] = novo[i]
                    c['fornadas'] = _forn(novo[i])
                rr['total'] = sum(novo)
            # Rastreabilidade: de QUAIS produtos finais vem a demanda do insumo
            # (ex: Massa para folhar ← Croissant Tradicional N un). Pro expandir.
            origem = consumo_origem.get(rid, {})
            rr['breakdown_bom'] = sorted(
                ({'nome': (receitas[pai].nome if pai in receitas else '(?)'),
                  'pai_qtd': int(round(v['pai'])),
                  'qtd': int(round(v['insumo']))}
                 for pai, v in origem.items() if round(v['insumo']) > 0),
                key=lambda b: -b['qtd'])
        # Propaga a producao desta receita pras sub-receitas dela, registrando
        # a contribuicao de cada pai (pra a rastreabilidade do insumo acima):
        # quanto o pai produz e quanto disso vira insumo.
        for sid, ratio in bom.get(rid, []):
            base = prod.get(rid, [0] * n)
            pai_tot = 0.0
            contrib = 0.0
            for i in range(n):
                consumo[sid][i] += base[i] * ratio
                pai_tot += base[i]
                contrib += base[i] * ratio
            if contrib > 0:
                ag = consumo_origem[sid][rid]
                ag['pai'] += pai_tot
                ag['insumo'] += contrib


def cronograma_producao(horizonte_dias=7, janela_semanas=6,
                        inicio_offset_dias=0, equilibrar=False):
    """Cronograma de producao POR DIA — a MESMA conta do balanco, distribuida.

    O total por receita parte do "Produzir" do `balanco_industria` (mesma
    janela, mesmo estoque, mesma previsao); o cronograma ESPALHA esse total
    pelos dias do horizonte seguindo a curva de demanda diaria: producao do dia
    P mira a entrega de (P + lead), e o estoque pronto cobre os dias mais
    proximos primeiro. Receita com `lote_pedido` produz em LOTES inteiros (nao
    picado): o total e arredondado pro multiplo do lote, entao pode divergir um
    pouco do "Produzir" exato do balanco (custo de produzir em batidas
    redondas). Sem lote, o total bate exatamente com o balanco.

    Retorna dict:
        dias: [{data, label, dow}]                          # dias de producao
        receitas: [{receita_id, nome, dias_producao, em_estoque,
                    por_dia: [{data, qtd, fornadas}], total}]
        hoje, inicio, inicio_offset_dias, horizonte_dias, janela_semanas.
    """
    from app.services.producao import fornadas_amassadeira, massa_receita_base

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
    # so pra dar FORMATO a curva diaria (o total ja vem do balanco). Tambem por
    # LOJA (firme_loja) pra a projecao do saldo mostrar as saidas DATADAS com a
    # loja de cada entrega.
    deliv_fim = inicio_d + timedelta(days=horizonte_dias - 1 + max_lead)
    nomes_loja = {l.id: l.nome for l in Loja.query.all()}
    firme = defaultdict(lambda: defaultdict(int))
    firme_loja = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    for rid, loja_id, data_ent, qtd in (db.session.query(
            PedidoItem.receita_id, PedidoLoja.loja_id,
            PedidoLoja.data_entrega, PedidoItem.quantidade)
            .join(PedidoLoja, PedidoItem.pedido_id == PedidoLoja.id)
            .filter(PedidoItem.receita_id.isnot(None),
                    PedidoLoja.status.in_(STATUS_PEDIDO_NAO_BAIXADOS),
                    PedidoLoja.data_entrega >= inicio_d,
                    PedidoLoja.data_entrega <= deliv_fim).all()):
        if data_ent is not None:
            firme[rid][data_ent] += int(qtd or 0)
            firme_loja[rid][data_ent][loja_id] += int(qtd or 0)

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

    datas_possiveis_dow = _datas_por_dow(hist_ini, hist_fim)   # denom da media
    residual_rate = {rid: _taxa_residual(qtd_dow.get(rid, {}), soma_total.get(rid, 0),
                                         dias_calendario_janela)
                     for rid in soma_total}

    def _previsto_dia(rid, dia):
        if not _fornada_no_dia(receitas.get(rid), dia):
            return 0.0
        dow = dia.weekday()
        return _previsto_dow(
            qtd_dow[rid].get(dow), hoje_d, residual_rate.get(rid, 0.0),
            datas_possiveis=datas_possiveis_dow[dow])

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
        # FRACIONARIO de proposito: o previsto eh fracao/dia (ex: 0,43). Arredondar
        # cada dia pra int dava 0 em item de giro baixo -> pesos todos zero ->
        # _distribuir_inteiro empilhava TUDO no dia 0 (pico irreal). Mantendo a
        # fracao, os pesos sao nao-nulos e a producao espalha pelos dias certos.
        gross = []
        for i in range(horizonte_dias):
            entrega = dias_prod[i] + timedelta(days=L)
            gross.append(max(float(firme[rid].get(entrega, 0)),
                             float(_previsto_dia(rid, entrega))))
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
        # Padroniza a PRODUCAO em LOTES inteiros (nao produzir picado — decisao
        # do dono 29/06): arredonda o total pro multiplo do lote da receita e
        # distribui em pacotes inteiros pelos dias (cada dia 0 ou multiplo do
        # lote). O total passa a ser multiplo do lote — pode divergir um pouco
        # do "Produzir" exato do balanco; e o custo de produzir em batidas
        # redondas. NAO usa o 'minimo' do pedido (piso e regra de PEDIDO da loja,
        # nao de producao). Sem lote -> distribuicao exata como antes.
        lote = int(getattr(rec, 'lote_pedido', 0) or 0)
        if lote > 1 and produzir > 0:
            n_lotes = int(round(produzir / lote)) or 1
            liquido = [x * lote for x in _distribuir_inteiro(n_lotes, pesos)]
        else:
            liquido = _distribuir_inteiro(produzir, pesos)

        from app.services.massa_base import rendimento_massa_crua
        rend = rendimento_massa_crua(rec)
        # Anti-"acender fornada por dribble" (ex: produzir 1 pao num dia): um dia
        # que produz menos que uma fracao de UMA FORNADA (batida da amassadeira)
        # consolida com o DIA ANTERIOR (produz ANTES), acumulando ate valer um
        # lote. So MOVE (total preservado); o dia 0 (hoje) e o sumidouro.
        # Consolida PRA TRAS, nao pra frente (era o C1): a producao do dia i mira
        # a entrega i+lead; empurrar o dribble pro dia i+1 entregaria i+1+lead =
        # UM DIA TARDE (a entrega que o dribble servia ja passou). Puxar pro dia
        # i-1 produz mais cedo -> pronto a tempo (custo: 1 dia a mais de estoque,
        # aceitavel pra congelado/industria). Dribble numa entrega IMINENTE (dia
        # 0, hoje) nao tem dia anterior: fica no dia 0 (produz hoje, no prazo) em
        # vez de atrasar — cumprir o prazo vale mais que poupar uma batida.
        # unidades_por_fornada = capacidade_amassadeira x rend / massa de 1
        # receita — quantas unidades enchem uma batida. So aplica a receita que
        # passa pela amassadeira (cap>0); item sem fornada (Moeda/creme) nao
        # consolida (produzir 1 la nao desperdica batida).
        cap = int(getattr(rec, 'capacidade_amassadeira_g', 0) or 0)
        massa_base = massa_receita_base(rec) if (cap > 0 and rend > 0) else 0
        if massa_base > 0:
            unid_por_fornada = cap * rend / massa_base
            minimo = ceil(unid_por_fornada * _MIN_FRACAO_FORNADA)
            for i in range(len(liquido) - 1, 0, -1):
                if 0 < liquido[i] < minimo:
                    liquido[i - 1] += liquido[i]
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
            'por_dia': por_dia, 'total': sum(liquido),
        })

    # Equilibrar carga (opt-in): em vez de espalhar cada receita pela curva de
    # demanda, poe cada receita INTEIRA num unico dia e nivela as FORNADAS por
    # dia, ADIANTANDO receitas (puxando pra frente) pra encher dias ociosos.
    # Cada receita pode ir de hoje ate seu deadline (1o dia que ja produzia) —
    # nunca depois (a entrega tem que sair). Sem limite de frescor (decisao do
    # dono 29/06): qualquer receita pode ser adiantada. Nao divide receita.
    if equilibrar:
        n = len(dias_prod)
        itens_eq = []
        for rr in receitas_out:
            total = rr['total']
            if total <= 0:
                continue
            rec = receitas.get(rr['receita_id'])
            from app.services.massa_base import rendimento_massa_crua
            rend = rendimento_massa_crua(rec)
            forn = fornadas_amassadeira(rec, max(1, ceil(total / rend))) or 1
            # deadline = 1o dia com producao na distribuicao normal (o mais
            # tarde que da pra produzir sem atrasar a entrega); pode adiantar.
            deadline = next((i for i, c in enumerate(rr['por_dia'])
                             if c['qtd'] > 0), n - 1)
            itens_eq.append({'rr': rr, 'total': total, 'rec': rec, 'rend': rend,
                             'F': forn, 'dia': deadline})
        if itens_eq:
            carga = [0.0] * n
            for it in itens_eq:
                carga[it['dia']] += it['F']
            alvo = sum(it['F'] for it in itens_eq) / n
            # Enche da frente: puxa receita INTEIRA do dia mais carregado (mais
            # tarde) pra ca ate chegar perto do alvo; dia ocioso aceita ao menos
            # uma. So adianta (dia > d) — nunca atrasa.
            for d in range(n):
                while carga[d] < alvo:
                    cands = sorted((it for it in itens_eq if it['dia'] > d),
                                   key=lambda it: -carga[it['dia']])
                    if not cands:
                        break
                    escolha = next((it for it in cands
                                    if carga[d] + it['F'] <= alvo
                                    or carga[d] == 0), None)
                    if escolha is None:
                        break
                    carga[escolha['dia']] -= escolha['F']
                    escolha['dia'] = d
                    carga[d] += escolha['F']
            for it in itens_eq:   # reescreve: receita inteira no dia escolhido
                rec, rend, total, dia = (it['rec'], it['rend'], it['total'],
                                         it['dia'])
                forn = fornadas_amassadeira(rec, max(1, ceil(total / rend)))
                for i, c in enumerate(it['rr']['por_dia']):
                    c['qtd'] = total if i == dia else 0
                    c['fornadas'] = forn if i == dia else None

    # Receitas que so existem como edicao manual (override) e nao tem demanda
    # prevista — ex: adicionadas na tela 'editar plano' do padeiro. Sem isto nao
    # apareceriam no grid e seriam apagadas no proximo 'enviar' (que reconstroi a
    # ordem a partir do grid). Injeta linha zerada; aplicar_overrides preenche.
    from app.models import CronogramaOverride
    ja = {rr['receita_id'] for rr in receitas_out}
    extra_rids = {o.receita_id for o in CronogramaOverride.query.filter(
        CronogramaOverride.data.in_(dias_prod),
        CronogramaOverride.qtd > 0).all()
        if o.receita_id not in ja and o.receita_id in receitas}
    if extra_rids:
        est_bal = {it['receita_id']: int(it.get('em_estoque', 0) or 0)
                   for it in bal['itens']}
        for rid in extra_rids:
            rec = receitas[rid]
            receitas_out.append({
                'receita_id': rid, 'nome': rec.nome,
                'dias_producao': lead.get(rid, 0),
                'em_estoque': est_bal.get(rid, 0),
                'por_dia': [{'data': d.isoformat(), 'qtd': 0, 'fornadas': None}
                            for d in dias_prod],
                'total': 0,
            })

    # Edicao manual da grade: aplica os overrides salvos (sobrepoe a sugestao
    # calculada/equilibrada). No-op quando nao ha override.
    from app.services.cronograma_edit import aplicar_overrides
    aplicar_overrides(receitas_out, dias_prod)

    # MRP: explode sub-receitas (massa para folhar, creme de amendoas...) em
    # linhas de producao proprias, produzidas ANTES do produto final que as
    # consome. Usa a producao final ja calculada/editada. No-op sem sub-receita.
    _explodir_bom(receitas_out, dias_prod, receitas, lead, bal)

    # Produtos que a loja PEDE mas estao SEM demanda nesta janela (o balanco os
    # exclui pra nao listar zeros). Pro PLANEJAMENTO o usuario quer ve-los na
    # grade pra poder programar (ex: Pain au Chocolat sem pedido nesta semana).
    # Injeta linha zerada com o estoque atual; nao entram na explosao (0 demanda).
    ja_out = {rr['receita_id'] for rr in receitas_out}
    bal_est = {it['receita_id']: it for it in bal['itens']}
    vendaveis = (Receita.query
                 .filter(Receita.arquivada_em.is_(None),
                         Receita.sugerir_pedido_loja.isnot(False)).all())
    for rec in sorted(vendaveis, key=lambda r: r.id):
        if rec.id in ja_out:
            continue
        itb = bal_est.get(rec.id)
        receitas_out.append({
            'receita_id': rec.id, 'nome': rec.nome,
            'dias_producao': lead.get(rec.id, 0),
            'em_estoque': int(itb['em_estoque']) if itb else 0,
            'por_dia': [{'data': d.isoformat(), 'qtd': 0, 'fornadas': None}
                        for d in dias_prod],
            'total': 0,
        })

    # Categoria + detalhe do SALDO por receita (pro expandir da tela): estoque -
    # pedido programado (comprometido = pedidos das lojas no horizonte) = saldo,
    # mais a PROJECAO dia a dia (saidas datadas + producao programada -> saldo do
    # dia; marca o 1o dia que fica negativo, "vai faltar").
    bal_idx = {it['receita_id']: it for it in bal['itens']}
    for rr in receitas_out:
        rid = rr['receita_id']
        rec = receitas.get(rid)
        rr['categoria'] = (rec.categoria or '').strip() if rec else ''
        it = bal_idx.get(rid)
        # Estoque REAL da linha vem do balanco quando a receita esta la. Corrige o
        # INSUMO: `_explodir_bom` cria a linha da sub-receita com
        # `em_estoque=est_extra` (=0 pra sub que ESTA no balanco por ter estoque),
        # entao a massa ja batida aparecia como "em estoque: 0" mesmo cobrindo a
        # demanda dos croissants — a producao 0 estava certa, o numero exibido nao.
        # No-op pra produto (ja vinha de it['em_estoque']).
        if it is not None:
            rr['em_estoque'] = int(it['em_estoque'])
        comp = int(it['comprometido']) if it else 0
        prev = int(it['previsto']) if it else 0
        est_ef = int(it['em_estoque_efetivo']) if it else int(rr['em_estoque'])
        demanda = max(comp, prev)
        rr['comprometido'] = comp
        rr['previsto'] = prev        # a PREVISAO (historico) que tambem puxa producao
        rr['demanda'] = demanda      # firme OU previsto, o maior — o que o balanco usa
        rr['em_estoque_efetivo'] = est_ef   # estoque que sobra apos entregas iminentes
        # Saldo contra a DEMANDA (max comp, prev) e o estoque EFETIVO: bate com o
        # "Produzir" da linha (-saldo == produzir quando negativo). Antes era
        # estoque - comprometido (so firme), ignorando o previsto -> a caixa dizia
        # "nao falta" enquanto a linha mandava produzir (bug pego pelo dono 30/06).
        rr['saldo'] = est_ef - demanda
        rr['produzir'] = max(0, demanda - est_ef)
        rr['breakdown'] = ([b for b in it['breakdown_comprometido'] if b['qtd'] > 0]
                           if it else [])
        rr.setdefault('breakdown_bom', [])   # so insumo tem; produto fica vazio
        # Projecao dia a dia: estoque + producao PRONTA no dia - DEMANDA do dia
        # (firme datado OU previsto do dia, o maior) => saldo no fim do dia. 1o dia
        # negativo = "vai faltar". A demanda inclui o previsto desde 30/06; antes
        # so descontava o firme e a projecao dizia "nao falta" a toa.
        # A producao entra no estoque quando fica PRONTA (dia de inicio + lead),
        # nao no dia em que COMECA (era o C2): por_dia[i] e a producao INICIADA no
        # dia i (mira a entrega i+lead), entao a que fica pronta no dia i comecou
        # em i-lead. Creditar no dia de inicio mostrava o estoque L dias cedo
        # demais e escondia falta no intervalo (a coluna "Producao" da projecao =
        # recebimento pronto no dia, coerente com o saldo; o grid de cima e por
        # dia de INICIO). Producao iniciada nos ultimos L dias fica pronta depois
        # do horizonte -> nao entra na projecao (correto).
        L = lead.get(rid, 0)
        running = int(rr['em_estoque'])
        projecao = []
        rr['dia_falta'] = None
        for i, d in enumerate(dias_prod):
            prod_i = int(rr['por_dia'][i - L]['qtd'] or 0) if i - L >= 0 else 0
            firme_i = int(firme[rid].get(d, 0))
            prev_i = int(round(_previsto_dia(rid, d)))   # previsto saindo no dia d
            saida_i = max(firme_i, prev_i)               # demanda do dia
            running += prod_i - saida_i
            if running < 0 and rr['dia_falta'] is None:
                rr['dia_falta'] = dias_out[i]['label']
            saida_lojas = sorted(
                ({'loja_nome': nomes_loja.get(lid, '?'), 'qtd': q}
                 for lid, q in firme_loja[rid].get(d, {}).items() if q > 0),
                key=lambda b: -b['qtd'])
            projecao.append({
                'label': dias_out[i]['label'], 'saida': saida_i,
                'saida_firme': firme_i, 'saida_lojas': saida_lojas,
                'producao': prod_i, 'previsto': prev_i, 'saldo': running,
                'falta': running < 0})
        rr['projecao'] = projecao

    # Agrupa os produtos por CATEGORIA (depois por nome) — senao ficam espalhados
    # pela ordem de urgencia/demanda do balanco. Categoria vazia vai por ultimo.
    receitas_out.sort(key=lambda rr: (rr['categoria'] == '',
                                      rr['categoria'].lower(),
                                      (rr['nome'] or '').lower()))

    return {
        'dias': dias_out,
        'receitas': receitas_out,
        'hoje': hoje_d.isoformat(),
        'inicio': inicio_d.isoformat(),
        'inicio_offset_dias': inicio_offset_dias,
        'horizonte_dias': horizonte_dias,
        'janela_semanas': janela_semanas,
    }


def decompor_previsao(receita_id, horizonte_dias=7, janela_semanas=6,
                      inicio_offset_dias=0):
    """Decompoe o `previsto` de UMA receita pra responder 'de qual dia/loja vem
    esse numero?'. Pra cada dia do horizonte mostra a entrega-alvo (dia + lead),
    o pedido FIRME por loja e a PREVISAO do historico decomposta por loja —
    as entregas recentes daquele dia-da-semana (data, loja, qtd) e a media
    recencia-ponderada de cada loja. Read-only, diagnostico. Usa EXATAMENTE a
    mesma conta do cronograma (`_media_recencia`, dia-da-semana, fallback)."""
    horizonte_dias = max(1, min(int(horizonte_dias or 7), 14))
    janela_semanas = max(1, min(int(janela_semanas or 6), 26))
    inicio_offset_dias = max(0, min(int(inicio_offset_dias or 0), 14))

    rec = Receita.query.get(int(receita_id))
    if rec is None or rec.arquivada_em is not None:
        return None

    hoje_d = hoje()
    inicio_d = hoje_d + timedelta(days=inicio_offset_dias)
    hist_ini = hoje_d - timedelta(days=7 * janela_semanas)
    hist_fim = hoje_d - timedelta(days=1)
    dias_calendario_janela = 7 * janela_semanas
    L = int(rec.dias_producao or 0)
    nomes_loja = {l.id: l.nome for l in Loja.query.all()}

    # Historico: entregas (nao-canceladas) da janela, por dia-da-semana, com
    # loja e data — a materia-prima da previsao.
    por_data_agg = defaultdict(lambda: defaultdict(int))      # dow -> data -> qtd
    por_loja = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))  # dow->loja->data->qtd
    soma_total = 0
    for loja_id, data_ent, qtd in (db.session.query(
            PedidoLoja.loja_id, PedidoLoja.data_entrega, PedidoItem.quantidade)
            .join(PedidoItem, PedidoItem.pedido_id == PedidoLoja.id)
            .filter(PedidoItem.receita_id == rec.id,
                    PedidoLoja.status != 'cancelado',
                    PedidoLoja.data_entrega >= hist_ini,
                    PedidoLoja.data_entrega <= hist_fim).all()):
        if data_ent is None:
            continue
        q = int(qtd or 0)
        dow = data_ent.weekday()
        por_data_agg[dow][data_ent] += q
        por_loja[dow][loja_id][data_ent] += q
        soma_total += q

    # Firme: pedidos atuais (ainda nao baixados) na janela de entrega.
    deliv_fim = inicio_d + timedelta(days=horizonte_dias - 1 + L)
    firme = defaultdict(lambda: defaultdict(int))            # data -> loja -> qtd
    for loja_id, data_ent, qtd in (db.session.query(
            PedidoLoja.loja_id, PedidoLoja.data_entrega, PedidoItem.quantidade)
            .join(PedidoItem, PedidoItem.pedido_id == PedidoLoja.id)
            .filter(PedidoItem.receita_id == rec.id,
                    PedidoLoja.status.in_(STATUS_PEDIDO_NAO_BAIXADOS),
                    PedidoLoja.data_entrega >= inicio_d,
                    PedidoLoja.data_entrega <= deliv_fim).all()):
        if data_ent is not None:
            firme[data_ent][loja_id] += int(qtd or 0)

    datas_possiveis_dow = _datas_por_dow(hist_ini, hist_fim)   # denom da media
    residual_rate = _taxa_residual(por_data_agg, soma_total, dias_calendario_janela)
    dias = []
    total_previsto_frac = 0.0
    total_firme = 0
    for i in range(horizonte_dias):
        prod_d = inicio_d + timedelta(days=i)
        entrega = prod_d + timedelta(days=L)
        dow = entrega.weekday()
        # Previsto agregado: a MESMA conta do cronograma.
        por_data = por_data_agg.get(dow) or {}
        if not _fornada_no_dia(rec, entrega):
            previsto = 0.0
            fonte = 'fora_fornada'
        elif por_data and len(por_data) >= _MIN_OCORRENCIAS_DOW:
            previsto = _media_recencia(
                por_data, hoje_d, datas_possiveis=datas_possiveis_dow[dow])
            fonte = 'media_dow'
        elif residual_rate > 0:
            previsto = residual_rate      # taxa residual (volume sem padrao de dow)
            fonte = 'media_diaria'
        elif soma_total:
            previsto = 0.0                # vende, mas so em dows com padrao proprio
            fonte = 'sem_dow'
        else:
            previsto = 0.0
            fonte = 'sem_historico'

        # Decomposicao por loja (media recencia-ponderada de cada loja no dow).
        previsto_lojas = []
        if fonte == 'media_dow':
            for loja_id, datas in por_loja.get(dow, {}).items():
                m = _media_recencia(
                    datas, hoje_d, datas_possiveis=datas_possiveis_dow[dow])
                if round(m) > 0:
                    previsto_lojas.append({'loja_nome': nomes_loja.get(loja_id, '?'),
                                           'media': int(round(m)),
                                           'n': len(datas)})
            previsto_lojas.sort(key=lambda x: -x['media'])

        # Entregas cruas do dow (as 12 mais recentes) — a prova do numero.
        historico = []
        for loja_id, datas in por_loja.get(dow, {}).items():
            for data_h, q in datas.items():
                historico.append({'data': data_h.isoformat(),
                                  'data_label': data_h.strftime('%d/%m'),
                                  'loja_nome': nomes_loja.get(loja_id, '?'),
                                  'qtd': q})
        historico.sort(key=lambda h: h['data'], reverse=True)
        historico = historico[:12]

        firme_d = firme.get(entrega, {})
        firme_lojas = sorted(
            ({'loja_nome': nomes_loja.get(lid, '?'), 'qtd': q}
             for lid, q in firme_d.items() if q > 0), key=lambda x: -x['qtd'])
        firme_i = sum(x['qtd'] for x in firme_lojas)
        # Acumula a FRACAO (ex: 0,4/dia) e da ceil no fim — mesma conta do
        # balanco (previsao_producao.py:348). Antes arredondava cada dia
        # (round->0 em item de giro baixo) e o total da pagina dava 0 enquanto
        # o grid mandava produzir 3 (a ferramenta desmentia o plano).
        total_previsto_frac += previsto
        total_firme += firme_i
        dias.append({
            'data': prod_d.isoformat(),
            'label': '%s %s' % (_DOW_PT[prod_d.weekday()], prod_d.strftime('%d/%m')),
            'entrega_label': '%s %s' % (_DOW_PT[dow], entrega.strftime('%d/%m')),
            'dow_nome': _DOW_PT_LONGO[dow],
            'firme': firme_i, 'firme_lojas': firme_lojas,
            # Fracionario (1 casa): "em media 0,4/dia" e honesto; o total
            # (ceil) mostra o inteiro acionavel. usado compara com a fracao.
            'previsto': round(previsto, 1), 'fonte': fonte,
            'usado': 'firme' if firme_i >= previsto else 'previsto',
            'previsto_lojas': previsto_lojas, 'historico': historico,
        })

    total_previsto = int(ceil(total_previsto_frac))
    return {
        'receita': {'id': rec.id, 'nome': rec.nome, 'lead': L},
        'hoje': hoje_d.isoformat(), 'inicio': inicio_d.isoformat(),
        'horizonte_dias': horizonte_dias, 'janela_semanas': janela_semanas,
        'hist_ini': hist_ini.isoformat(), 'hist_fim': hist_fim.isoformat(),
        'total_previsto': total_previsto, 'total_firme': total_firme,
        'dias': dias,
    }
