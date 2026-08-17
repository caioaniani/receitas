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

from app.constants import STATUS_PEDIDO_EDITAVEIS, STATUS_PEDIDO_NAO_BAIXADOS
from app.extensions import db
from app.models import EstoqueProducao, Loja, PedidoItem, PedidoLoja, Receita
from app.utils import SUB_RECEITA_TIPOS, hoje, unidades_subreceita

# Minimo de ocorrencias de um mesmo dia-da-semana na janela pra confiar na
# media daquele dia. Abaixo disso, cai no fallback (media diaria simples). Vale
# tambem por LOJA no pedido semanal: a loja so recebe sugestao de um item se o
# pediu nesse dia-da-semana em >= N datas distintas (1 vez = avulso/errado).
_MIN_OCORRENCIAS_DOW = 2

# Fornada especial (ex: Focaccia): vendida SO sab/dom (decisao do dono
# 10/08/2026 — SUBSTITUI a regra de 06/07 que incluia sexta). weekday():
# seg=0 .. dom=6 -> sab=5, dom=6. O forecast de pedido NAO sugere esses
# produtos em outros dias, mesmo que o historico tenha ruido (1 pedido
# avulso num dia de semana nao vira recorrencia). Pedido FIRME lancado pra
# outro dia segue contando (firme nao passa por este gate).
_DIAS_FORNADA_ESPECIAL = frozenset({5, 6})

# Fornada especial: a PRODUCAO acontece so sex/sab (dono 10/08/2026) — o
# assado fresco do fim de semana sai da vespera da venda (sex->sab,
# sab->dom). O cronograma nunca programa (nem deixa editar) producao de
# fornada especial fora desses dias. sex=4, sab=5.
_DIAS_PRODUCAO_FORNADA = frozenset({4, 5})

# Producao NORMAL so de SEGUNDA a SEXTA (dono 17/08/2026: "Sabado e domingo
# a gente nao produz, jogar tudo para segunda a sexta, a unica coisa que
# produzimos de sabado e a fornada especial"). A demanda do fim de semana
# rola pro ultimo dia PERMITIDO anterior (sexta) — mesma mecanica da
# fornada especial. seg=0 .. sex=4.
_DIAS_PRODUCAO_NORMAL = frozenset({0, 1, 2, 3, 4})

# Motores de previsao da demanda do CRONOGRAMA/balanco (pedido do dono
# 06/07/2026 — "+1 opcao de previsao, baseada nas vendas"):
# - 'pedidos': historico de PEDIDOS loja->industria (comportamento original);
# - 'vendas':  VENDA real das lojas + merma estrutural (mesma demanda
#              unificada da sugestao de pedido por venda, Fase 0.1/1);
# - 'maior':   o MAIOR dos dois por dia (nao subproduz quando um dos sinais
#              esta defasado — mesmo espirito do max(firme, previsto)).
# O firme (pedidos ainda nao entregues) SEMPRE conta, em qualquer motor.
MOTORES_PREVISAO_PRODUCAO = ('pedidos', 'vendas', 'maior')

# Cronograma: um dia que produz MENOS que esta fracao de uma fornada (rend) rola
# pro proximo dia, pra nao mandar o padeiro acender o forno por 1-2 unidades
# ("pedido picado"). Como e fracao da fornada, receita cuja fornada rende pouco
# (rend pequeno) nao sofre — produzir 1 la ja e uma fornada cheia.
_MIN_FRACAO_FORNADA = 0.2

# Nivelamento (equilibrar): ANTECEDENCIA MAXIMA em dias — um lote pode ser
# produzido no maximo N dias ANTES do dia em que a curva de demanda o pedia
# (dono 17/08/2026, caso Brioche: "nao da para produzir tudo isso de
# brioche, ele vence em 3 dias, nao e congelado" — o nivelamento antigo
# punha a receita INTEIRA num dia so). 2 dias = produto entregue com ate 2
# dias de folga, dentro da validade de 3 do caso mais fresco. SUBSTITUI o
# "sem limite de frescor" de 29/06.
_ANTECEDENCIA_MAX_DIAS = 2

_CACHE = {}
_CACHE_TTL = 60  # segundos

# Recencia (28/06/2026): a previsao do pedido semanal pesa MAIS as entregas
# recentes (decaimento exponencial) em vez de media uniforme — pega tendencia
# de loja subindo/caindo sem sair de "media dos ultimos pedidos". Meia-vida em
# dias: uma entrega de N dias atras pesa 0.5**(N/_MEIA_VIDA_DIAS). Aumentar
# deixa mais "liso" (no limite vira media uniforme); diminuir reage mais rapido.
_MEIA_VIDA_DIAS = 21

# Guarda de ULP pro ceil de medias (05/07/2026): a media de recencia e uma
# razao de somas de floats — quando o valor exato e um INTEIRO, o resultado
# pode sair 1 ulp acima (7.000000000000001) e ceil() infla em +1 unidade (ou
# +1 caixa inteira). Subtrair 1e-9 antes do ceil tira SO o ruido do ultimo
# bit — nao e tolerancia de negocio (1e-9 << 1 unidade de pao).
_EPS_ULP = 1e-9

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
    (ex: Focaccia) só sáb/dom -> False nos outros dias (não projeta demanda;
    o produto não é vendido nesse dia). Receita normal -> sempre True."""
    return not (rec is not None
                and getattr(rec, 'fornada_especial', False)
                and dia.weekday() not in _DIAS_FORNADA_ESPECIAL)


def producao_permitida_no_dia(rec, dia):
    """True se a receita PODE ser PRODUZIDA nesse dia. Fornada especial produz
    só sex/sáb (decisão do dono 10/08/2026): a venda de sáb/dom sai da
    véspera. Receita NORMAL produz só de SEGUNDA a SEXTA (decisão do dono
    17/08/2026: fim de semana não produz; a demanda de sáb/dom sai de
    sexta). Público de propósito: o cronograma_edit usa pra recusar edição
    manual em dia bloqueado."""
    if rec is not None and getattr(rec, 'fornada_especial', False):
        return dia.weekday() in _DIAS_PRODUCAO_FORNADA
    return dia.weekday() in _DIAS_PRODUCAO_NORMAL


def _rolar_pesos_permitidos(pesos, permitido):
    """Rola o peso de cada dia BLOQUEADO pro último dia PERMITIDO anterior
    (produzir mais cedo chega a tempo; mais tarde não). Peso sem nenhum dia
    permitido antes é DESCARTADO — se nada mais puxar, a linha não produz e
    a entrega aparece como EM RISCO (decisão humana; o cronograma não viola
    a regra por conta própria). Devolve a lista ajustada."""
    if all(permitido):
        return pesos
    ajust = [0.0] * len(pesos)
    for i, w in enumerate(pesos):
        if w <= 0:
            continue
        j = next((k for k in range(i, -1, -1) if permitido[k]), None)
        if j is not None:
            ajust[j] += float(w)
    return ajust


def _hist_vendas_receita_por_dow(hist_ini, hist_fim, com_loja=False):
    """Histórico de VENDA por (receita, dow, data), somado em TODAS as lojas —
    motor 'vendas' do cronograma (06/07/2026). Demanda unificada
    (VENDA_TIPOS_DEMANDA_COM_ESTORNO, estornos com o sinal de gravação de
    cada canal) + merma estrutural (MERMA_TIPOS_PROJECAO), líquido clampado
    em 0 por (receita, data). MESMA fonte da sugestão de pedido por venda
    (Fase 0.1/1), agregada no nível da indústria: só linhas com receita_id
    (a indústria produz receitas; produto/MP ficam fora).

    Retorna (qtd_dow, soma_total, datas_total[, por_loja]) na MESMA forma do
    histórico de pedidos do balanço — média por recência, taxa residual e
    Σ_dia max(firme, previsto) funcionam sem mudança. `com_loja=True` devolve
    também dow->loja->data->qtd (pro drill-down 'de onde vem a previsão?')."""
    from datetime import datetime as _dt
    from datetime import time as _time

    from app.constants import (
        MERMA_TIPOS_PROJECAO,
        VENDA_ESTORNO_SINAL_DEMANDA,
        VENDA_TIPOS_DEMANDA_COM_ESTORNO,
    )
    from app.models import EstoqueLoja, MovEstoqueLoja

    tipos = VENDA_TIPOS_DEMANDA_COM_ESTORNO + MERMA_TIPOS_PROJECAO
    bruto = defaultdict(lambda: defaultdict(int))          # rid -> data -> qtd
    bruto_loja = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    for rid, loja_id, tipo_mov, data_mov, qtd in (db.session.query(
            EstoqueLoja.receita_id, EstoqueLoja.loja_id, MovEstoqueLoja.tipo,
            MovEstoqueLoja.data, MovEstoqueLoja.quantidade)
            .join(EstoqueLoja, MovEstoqueLoja.estoque_loja_id == EstoqueLoja.id)
            .filter(EstoqueLoja.receita_id.isnot(None),
                    MovEstoqueLoja.tipo.in_(tipos),
                    MovEstoqueLoja.data >= _dt.combine(hist_ini, _time.min),
                    MovEstoqueLoja.data <= _dt.combine(hist_fim, _time.max))
            .all()):
        if data_mov is None:
            continue
        d_mov = data_mov.date()
        if tipo_mov in MERMA_TIPOS_PROJECAO:
            q = int(qtd or 0)
        else:
            q = VENDA_ESTORNO_SINAL_DEMANDA.get(tipo_mov, 1) * int(qtd or 0)
        bruto[rid][d_mov] += q
        if com_loja:
            bruto_loja[rid][loja_id][d_mov] += q
    qtd_dow = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    soma_total = defaultdict(int)
    datas_total = defaultdict(set)
    for rid, por_data in bruto.items():
        for d_mov, v in por_data.items():
            v = max(0, v)      # estorno de outro dia: demanda não é negativa
            if v <= 0:
                continue
            qtd_dow[rid][d_mov.weekday()][d_mov] += v
            soma_total[rid] += v
            datas_total[rid].add(d_mov)
    if not com_loja:
        return qtd_dow, soma_total, datas_total
    por_loja = defaultdict(
        lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(int))))
    for rid, lojas_d in bruto_loja.items():
        for loja_id, por_data in lojas_d.items():
            for d_mov, v in por_data.items():
                if v > 0:
                    por_loja[rid][d_mov.weekday()][loja_id][d_mov] += v
    return qtd_dow, soma_total, datas_total, por_loja


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


def _caps_por_retorno(receitas, estoque_de):
    """Politica "so de sobras" (dono, 02/07/2026): pai cuja ficha consome uma
    receita de RETORNO (destino de `Receita.retorno_receita_id`, ex: Croissant
    Almond consome "Croissant Tradicional — Retorno") tem a sugestao de
    producao CAPADA ao que o estoque devolvido cobre. Retorno nao e produzivel
    — nunca puxa massa/producao fresca pra cobrir o que faltar.

    receitas: {rid: Receita}. estoque_de(rid) -> estoque disponivel da receita.
    Retorna {rid_pai: {'cap', 'sub_id', 'sub_nome', 'disponivel'}} (cap = menor
    cobertura entre as subs de retorno da ficha). Liga por FK
    (`sub_receita_id`, backfillado por nome na migracao).

    Limitacao conhecida: dois pais consumindo o MESMO retorno sao capados
    independentemente (sem rateio) — hoje so o Almond consome retorno."""
    from app.services.massa_base import rendimento_massa_crua

    retorno_ids = {rid for (rid,) in db.session.query(Receita.retorno_receita_id)
                   .filter(Receita.retorno_receita_id.isnot(None)).distinct()}
    if not retorno_ids:
        return {}, set()
    caps = {}
    for rid, rec in receitas.items():
        rend = rendimento_massa_crua(rec)
        if rend <= 0:
            continue
        for ing in rec.ingredientes:
            if (ing.tipo or '') not in SUB_RECEITA_TIPOS or not ing.sub_receita_id:
                continue
            sid = ing.sub_receita_id
            ratio = unidades_subreceita(
                ing.tipo, ing.porcentagem, rec.peso_base) / rend
            if sid not in retorno_ids or ratio <= 0:
                continue
            disponivel = int(estoque_de(sid) or 0)
            cap = int(disponivel / ratio)
            atual = caps.get(rid)
            if atual is None or cap < atual['cap']:
                sub = receitas.get(sid)
                caps[rid] = {'cap': cap, 'sub_id': sid,
                             'sub_nome': sub.nome if sub else '?',
                             'disponivel': disponivel}
    return caps, retorno_ids


def balanco_industria(horizonte_dias=7, janela_semanas=6, usar_cache=True,
                      inicio_offset_dias=0, motor='pedidos'):
    """Balanco de producao da industria por receita.

    Args:
        horizonte_dias: janela futura de planejamento (1-14).
        janela_semanas: profundidade do historico pra previsao (1-26).
        usar_cache: usa o cache de 60s (False forca recalculo).
        inicio_offset_dias: desloca o INICIO do horizonte futuro (0=hoje,
            1=amanha...). O painel usa 1 porque a producao de hoje ja esta
            decidida. O historico continua ancorado em hoje.
        motor: fonte do PREVISTO (MOTORES_PREVISAO_PRODUCAO): 'pedidos'
            (historico de pedidos — original), 'vendas' (venda real das
            lojas + merma) ou 'maior' (max dos dois por dia). O firme conta
            sempre, em qualquer motor.

    Retorna dict:
        itens: lista por receita, cada um com em_estoque, comprometido,
               previsto, produzir, tem_historico, breakdown_comprometido.
        horizonte_dias, janela_semanas, hoje, horizonte_fim, motor.
        profundidade: {n_pedidos, n_datas, n_semanas_dados, janela_semanas,
                       desde} — pra UI mostrar a confianca da previsao.
        total_produzir_itens: quantas receitas precisam producao (produzir>0).
    """
    horizonte_dias = max(1, min(int(horizonte_dias or 7), 14))
    janela_semanas = max(1, min(int(janela_semanas or 6), 26))
    inicio_offset_dias = max(0, min(int(inicio_offset_dias or 0), 14))
    if motor not in MOTORES_PREVISAO_PRODUCAO:
        motor = 'pedidos'

    cache_key = (horizonte_dias, janela_semanas, inicio_offset_dias, motor)
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

    # 1b. Producao JA MANDADA e ainda nao confirmada (WIP), com inicio ANTES do
    # horizonte ([hoje, inicio_d)): e suprimento a caminho — sem isso o balanco
    # com offset=1 (painel) re-sugeria produzir amanha o que ja esta no forno
    # HOJE. Com offset=0 (cronograma) o intervalo e VAZIO de proposito: o grid
    # de hoje e a fonte do proprio plano (descontar o plano enviado zeraria o
    # grid apos o envio). Pendencia VENCIDA (plano de dias anteriores) NAO
    # conta — pode nunca ser produzida (a auditoria trata).
    em_producao = defaultdict(int)
    if inicio_d > hoje_d:
        from app.models import PlanejamentoItem, PlanejamentoProducao
        for rid_w, alvo_w, prod_w in (db.session.query(
                PlanejamentoItem.receita_id, PlanejamentoItem.qtd_alvo,
                PlanejamentoItem.produzido_qtd)
                .join(PlanejamentoProducao,
                      PlanejamentoItem.planejamento_id == PlanejamentoProducao.id)
                .filter(PlanejamentoProducao.data >= hoje_d,
                        PlanejamentoProducao.data < inicio_d,
                        PlanejamentoProducao.enviado_ao_padeiro.isnot(False),
                        PlanejamentoItem.dispensada_em.is_(None)).all()):
            em_producao[rid_w] += max(0, int(alvo_w or 0) - int(prod_w or 0))

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

    # 2b. Vendas B2B AGUARDANDO SEPARACAO (estoque_baixado_em NULL): desde
    # 07/07/2026 a baixa do B2B acontece na separacao pelo padeiro, entao a
    # venda pendente e demanda COMPROMETIDA — sem este bloco o balanco acha
    # o freezer livre e subproduz (a demanda so "aparecia" porque a baixa
    # imediata reduzia em_estoque). Cesta explode em componentes-RECEITA
    # (mesma explosao da baixa; componente produto/MP fica fora — o balanco
    # e por receita). Quando a venda baixa (separacao), ela SAI daqui e
    # passa a reduzir em_estoque — nunca conta 2x. Alimenta firme_dia
    # (demanda iminente) e comprometido, igual ao PedidoLoja.
    from app.models import Produto, VendaB2B, VendaB2BItem
    from app.services.cestas import componentes_de_cesta
    b2b_rows = (db.session.query(VendaB2BItem, VendaB2B.data_entrega)
                .join(VendaB2B, VendaB2BItem.venda_id == VendaB2B.id)
                .filter(VendaB2B.status == 'ativa',
                        VendaB2B.estoque_baixado_em.is_(None),
                        VendaB2B.data_entrega.isnot(None),
                        VendaB2B.data_entrega >= hoje_d,
                        VendaB2B.data_entrega <= comp_fim)
                .all())
    comprometido_b2b = defaultdict(int)
    _cache_cesta = {}

    def _contrib_b2b(rid, data_ent, q):
        firme_dia[rid][data_ent] += q
        L = lead.get(rid, 0)
        if (inicio_d + timedelta(days=L) <= data_ent
                <= inicio_d + timedelta(days=L + horizonte_dias - 1)):
            comprometido[rid] += q
            comprometido_b2b[rid] += q

    for vi, data_ent in b2b_rows:
        qtd = int(vi.quantidade or 0)
        if qtd <= 0:
            continue
        if vi.receita_id:
            if vi.receita_id in receitas:
                _contrib_b2b(vi.receita_id, data_ent, qtd)
            continue
        if vi.produto_id not in _cache_cesta:
            _cache_cesta[vi.produto_id] = componentes_de_cesta(
                Produto.query.get(vi.produto_id))
        for col, comp_id, _nome, qtd_por in _cache_cesta[vi.produto_id]:
            if col != 'receita_id' or comp_id not in receitas:
                continue
            q = int(round(qtd * qtd_por))
            if q > 0:
                _contrib_b2b(comp_id, data_ent, q)

    # 2c. Pedidos do SITE sob encomenda (D+2, dono 21/07/2026): item marcado
    # `sob_encomenda` é PRODUZIDO pro pedido — não sai da prateleira (a venda
    # NÃO baixa EstoqueLoja) e NÃO é lido em nenhum outro ramo do balanço,
    # então entra aqui como demanda firme PURA (aditivo, sem risco de dobra).
    # Conta do pagamento até a entrega (status ativo, não cancelado/entregue);
    # data_entrega na janela [hoje, comp_fim]. SÓ os itens sob encomenda; os
    # demais itens do mesmo pedido saem da prateleira e não produzem aqui.
    # Cesta explode em receita (mesmo padrão do B2B). Divulgação fica fora.
    from app.models import PedidoOnline, PedidoOnlineItem
    from app.services.loja_estoque_reserva import composicao_escolhida
    enc_rows = (db.session.query(PedidoOnlineItem, PedidoOnline.data_entrega)
                .join(PedidoOnline,
                      PedidoOnlineItem.pedido_id == PedidoOnline.id)
                .filter(PedidoOnline.status.in_(
                            ('pago', 'em_preparo', 'a_caminho')),
                        PedidoOnline.divulgacao.is_(False),
                        PedidoOnline.data_entrega.isnot(None),
                        PedidoOnline.data_entrega >= hoje_d,
                        PedidoOnline.data_entrega <= comp_fim)
                .all())
    comprometido_encomenda = defaultdict(int)

    def _contrib_encomenda(rid, data_ent, q):
        firme_dia[rid][data_ent] += q
        L = lead.get(rid, 0)
        if (inicio_d + timedelta(days=L) <= data_ent
                <= inicio_d + timedelta(days=L + horizonte_dias - 1)):
            comprometido[rid] += q
            comprometido_encomenda[rid] += q

    for pi, data_ent in enc_rows:
        qtd = int(pi.quantidade or 0)
        if qtd <= 0:
            continue
        if pi.receita_id:
            # Só receita sob encomenda (a flag mora na Receita).
            rec = receitas.get(pi.receita_id)
            if rec is not None and getattr(rec, 'sob_encomenda', False):
                _contrib_encomenda(pi.receita_id, data_ent, qtd)
            continue
        if pi.produto_id:
            prod = Produto.query.get(pi.produto_id)
            if not (prod is not None and getattr(prod, 'sob_encomenda', False)):
                continue
            # Menu configurável (26/07/2026): a composição que vale é a
            # ESCOLHIDA pelo cliente, gravada no pedido — o cadastro guarda
            # só a pré-seleção e produziria a cesta errada. Não cacheia:
            # varia por item de pedido, não por produto.
            comps_pi = composicao_escolhida(pi)
            if comps_pi is None:
                if pi.produto_id not in _cache_cesta:
                    _cache_cesta[pi.produto_id] = componentes_de_cesta(prod)
                comps_pi = _cache_cesta[pi.produto_id]
            for col, comp_id, _nome, qtd_por in comps_pi:
                if col != 'receita_id' or comp_id not in receitas:
                    continue
                q = int(round(qtd * qtd_por))
                if q > 0:
                    _contrib_encomenda(comp_id, data_ent, q)

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
    #
    # DEMANDA POR DIA (Fase 2, 02/07/2026): alem do total `previsto`, soma-se
    # Σ_dia max(firme_d, previsto_d). O max no AGREGADO (max(Σfirme, Σprev))
    # subproduzia: dias ja pedidos ACIMA da media nao compensam dias ainda nao
    # pedidos que virao NA media — a demanda real e o max dia a dia (a projecao
    # do cronograma ja usava essa conta e podia acusar 'vai faltar' enquanto o
    # total do balanco dizia que nao).
    residual_rate = {rid: _taxa_residual(qtd_dow.get(rid, {}), soma_total.get(rid, 0),
                                         dias_calendario_janela)
                     for rid in receitas}
    # Motor 'vendas'/'maior': curva de VENDA real por (receita, dow, data) —
    # mesma forma do historico de pedidos, entao a media por recencia e a
    # taxa residual funcionam identicas.
    qtd_dow_v, soma_v, datas_v, residual_v = {}, {}, {}, {}
    if motor in ('vendas', 'maior'):
        qtd_dow_v, soma_v, datas_v = _hist_vendas_receita_por_dow(
            hist_ini, hist_fim)
        residual_v = {rid: _taxa_residual(qtd_dow_v.get(rid, {}),
                                          soma_v.get(rid, 0),
                                          dias_calendario_janela)
                      for rid in receitas}

    # Receitas de RETORNO (destino de retorno_receita_id) NAO sao produziveis
    # — o estoque delas entra por devolucao das lojas, nunca por fornada. No
    # motor=vendas elas ganhavam historico de venda PROPRIO (a venda do
    # Croissant de Nutella baixa o retorno DA LOJA; a coleta de retirada
    # tambem gera movimento) e viravam "previsto 164 → produzir 164" no grid
    # (bug pego pelo dono 13/07/2026). A demanda de venda delas e servida
    # pelo estoque de retorno DA LOJA (reposto pela conversao de sobras), nao
    # pela industria — aqui previsto e produzir ficam SEMPRE zerados; o firme
    # (pedido real, se existir) continua visivel na demanda.
    retorno_ids = {r for (r,) in db.session.query(Receita.retorno_receita_id)
                   .filter(Receita.retorno_receita_id.isnot(None)).distinct()}

    previsto = defaultdict(float)
    demanda_soma = defaultdict(float)
    for rid in receitas:
        rid_dow = qtd_dow.get(rid, {})
        rid_dow_v = qtd_dow_v.get(rid, {}) if motor != 'pedidos' else {}
        usa_p = (motor in ('pedidos', 'maior') and bool(datas_total.get(rid))
                 and rid not in retorno_ids)
        usa_v = (motor in ('vendas', 'maior') and bool(datas_v.get(rid))
                 and rid not in retorno_ids)
        L = lead.get(rid, 0)
        dias_rid = [inicio_d + timedelta(days=L + i)
                    for i in range(horizonte_dias)]
        rec_rid = receitas.get(rid)
        for d in dias_rid:
            f_d = float(firme_dia[rid].get(d, 0))
            p_d = 0.0
            if (usa_p or usa_v) and _fornada_no_dia(rec_rid, d):
                dow = d.weekday()
                p_ped = _previsto_dow(
                    rid_dow.get(dow), hoje_d, residual_rate[rid],
                    datas_possiveis=datas_possiveis_dow[dow]) if usa_p else 0.0
                p_ven = _previsto_dow(
                    rid_dow_v.get(dow), hoje_d, residual_v.get(rid, 0.0),
                    datas_possiveis=datas_possiveis_dow[dow]) if usa_v else 0.0
                p_d = max(p_ped, p_ven) if motor == 'maior' else (
                    p_ven if motor == 'vendas' else p_ped)
                previsto[rid] += p_d
            demanda_soma[rid] += max(f_d, p_d)

    def _previsto_dia(rid, dia):
        if not _fornada_no_dia(receitas.get(rid), dia):
            return 0.0
        dow = dia.weekday()
        p_ped = _previsto_dow(
            qtd_dow.get(rid, {}).get(dow), hoje_d, residual_rate.get(rid, 0.0),
            datas_possiveis=datas_possiveis_dow[dow])
        if motor == 'pedidos':
            return p_ped
        p_ven = _previsto_dow(
            qtd_dow_v.get(rid, {}).get(dow), hoje_d, residual_v.get(rid, 0.0),
            datas_possiveis=datas_possiveis_dow[dow])
        return p_ven if motor == 'vendas' else max(p_ped, p_ven)

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
    # Cap "so de sobras": pai que consome receita de retorno nunca sugere
    # producao alem do que o estoque devolvido cobre (ver _caps_por_retorno).
    caps_retorno, _ = _caps_por_retorno(receitas, lambda sid: em_estoque.get(sid, 0))
    itens = []
    for rid, rec in receitas.items():
        est = em_estoque.get(rid, 0)
        wip = em_producao.get(rid, 0)
        # Estoque efetivo = o que sobra DEPOIS das entregas iminentes (que
        # ja vao consumir estoque antes da janela) + a producao ja MANDADA
        # (WIP — pronta antes do inicio do horizonte; ver bloco 1b). As
        # entregas iminentes nao podem ser servidas pelo WIP (ainda nao esta
        # pronto), por isso o max() vem antes da soma.
        est_efetivo = max(0, est - pre_demanda.get(rid, 0)) + wip
        comp = comprometido.get(rid, 0)
        prev = int(ceil(previsto.get(rid, 0)))
        # Piso do estoque minimo da industria (freezer): o alvo do dia nunca
        # cai abaixo do minimo cadastrado na ficha — mantem um colchao no
        # congelador alem da demanda prevista (decisao do dono 16/07/2026).
        # Receita com minimo cadastrado NUNCA some da tela: precisa aparecer
        # pra o piso valer mesmo sem estoque/demanda no momento.
        minimo_ind = int(rec.estoque_minimo_industria or 0)
        if est == 0 and comp == 0 and prev == 0 and wip == 0 and minimo_ind == 0:
            continue
        # Demanda = Σ_dia max(firme_d, previsto_d) — ver bloco 4. Nunca menor
        # que max(comp, prev) (o agregado antigo); a diferenca e exatamente a
        # subproducao dos dias mistos.
        demanda = int(ceil(demanda_soma.get(rid, 0.0)))
        # Aplicado ANTES das travas de retorno/cap: retorno nunca produz e o
        # cap "so de sobras" ainda manda sobre o piso.
        alvo = max(demanda, minimo_ind)
        # Flag "estoque nao abate" (dono 19/07/2026, caso Massa para folhar):
        # o fisico do ledger nao e confiavel pra esta receita — so a producao
        # JA MANDADA (WIP) abate o que falta produzir. O em_estoque_efetivo
        # exibido segue sendo o real; muda so a conta do planejamento.
        nao_abate = bool(getattr(rec, 'estoque_nao_abate', False))
        est_planejamento = wip if nao_abate else est_efetivo
        produzir = max(0, alvo - est_planejamento)
        limitado_por_minimo = minimo_ind > demanda and produzir > 0
        # Retorno nunca sugere producao (nem por firme): so entra por
        # devolucao. A linha segue visivel pra visibilidade do estoque.
        if rid in retorno_ids:
            produzir = 0
            limitado_por_minimo = False
        lim = caps_retorno.get(rid)
        lim_aplicado = None
        if lim is not None and produzir > lim['cap']:
            produzir = lim['cap']
            lim_aplicado = lim
            limitado_por_minimo = False   # cap de sobras manda sobre o piso
        itens.append({
            'retorno': rid in retorno_ids,
            'limitado_por_retorno': lim_aplicado,
            'receita_id': rid,
            'nome': rec.nome,
            'em_estoque': est,
            'em_estoque_efetivo': est_efetivo,
            'em_producao': wip,
            'estoque_nao_abate': nao_abate,
            'comprometido': comp,
            'previsto': prev,
            'demanda': demanda,
            'produzir': produzir,
            # Piso do estoque minimo da industria: valor cadastrado (0 = sem
            # piso) e flag de quando ele ELEVOU o produzir acima da demanda.
            'estoque_minimo': minimo_ind,
            'limitado_por_minimo': limitado_por_minimo,
            # Historico DO MOTOR ativo (pedidos, vendas, ou qualquer um).
            'tem_historico': bool(
                (motor in ('pedidos', 'maior') and datas_total.get(rid))
                or (motor in ('vendas', 'maior') and datas_v.get(rid))),
            'dias_producao': lead.get(rid, 0),
            # Lista TODAS as lojas operacionais — mesmo com qtd=0. Visivel
            # confirma ao usuario que o motor enxergou cada loja. Ordem: qtd
            # desc, depois alfabetico (lojas_op ja vem ordenado por nome).
            # Vendas B2B aguardando separacao entram como linha propria —
            # sem ela o total do comprometido nao bate com o detalhamento.
            'breakdown_comprometido': sorted(
                [{'loja_id': l.id, 'loja_nome': l.nome,
                  'qtd': comprometido_loja.get(rid, {}).get(l.id, 0)}
                 for l in lojas_op]
                + ([{'loja_id': None, 'loja_nome': 'Vendas B2B',
                     'qtd': comprometido_b2b[rid]}]
                   if comprometido_b2b.get(rid) else [])
                + ([{'loja_id': None, 'loja_nome': 'Encomenda site',
                     'qtd': comprometido_encomenda[rid]}]
                   if comprometido_encomenda.get(rid) else []),
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
        'motor': motor,
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
    datas_possiveis_dow = _datas_por_dow(hist_ini, hist_fim)
    estimado = defaultdict(lambda: defaultdict(float))  # loja_id -> data -> q
    for d in dias_futuros:
        # Paridade com o balanco (Fase 2, 02/07/2026): mesmo gate de fornada
        # especial e mesmo denominador-com-zeros — o drill-down mostrava
        # numeros MAIORES que a linha do balanco que ele detalha (ficou sem os
        # fixes de 30/06).
        if not _fornada_no_dia(rec, d):
            continue
        dow = d.weekday()
        previsto_dia = _previsto_dow(qtd_dow.get(dow), hoje_d, residual_rate,
                                     datas_possiveis=datas_possiveis_dow[dow])
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
        # Fornada especial só é vendida sáb/dom — não sugere em outro dia.
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


def desperdicio_recente_por_item(dias=7):
    """{loja_id: {token: qtd}} do que as lojas descartaram nos ultimos N
    dias — token no idioma das grades de pedido ('<receita_id>' /
    'mp:<id>'). Desperdicio de PRODUTO (cesta) fica fora: a grade e de
    receita/MP. So leitura pra coluna 'Desp. 7d' das telas de pedidos da
    semana (o dado ja alimentava a IA; o operador humano nao via)."""
    from sqlalchemy import func

    from app.models import Desperdicio
    # dias-1: "ultimos 7 dias" INCLUINDO hoje = hoje-6..hoje (nao 8 datas).
    corte = hoje() - timedelta(days=max(0, int(dias or 7) - 1))
    rows = (db.session.query(Desperdicio.loja_id, Desperdicio.receita_id,
                             Desperdicio.materia_prima_id,
                             func.sum(Desperdicio.quantidade))
            .filter(Desperdicio.data >= corte,
                    db.or_(Desperdicio.receita_id.isnot(None),
                           Desperdicio.materia_prima_id.isnot(None)))
            .group_by(Desperdicio.loja_id, Desperdicio.receita_id,
                      Desperdicio.materia_prima_id).all())
    out = defaultdict(dict)
    for lid, rid, mid, q in rows:
        tok = str(rid) if rid is not None else f'mp:{mid}'
        out[lid][tok] = out[lid].get(tok, 0) + int(q or 0)
    return dict(out)


def _cond_sem_entrega_antecipada(hoje_d):
    """Condição SQL que EXCLUI pedido finalizado ANTES da data de entrega.

    Caso real (Anesio, 08/07/2026): pedido de EMERGÊNCIA criado de madrugada
    saiu no caminhão de HOJE, mas nasceu datado de amanhã (não-admin não pode
    datar pro mesmo dia — pedidos/routes.py data_min) e foi marcado entregue
    às 6h30. Esse pedido não muda mais e não pode ocupar o dia futuro — senão
    a grade trava a coluna e o pedido REAL da data fica bloqueado. Entregue
    NO dia (data_entrega <= hoje) continua contando: é a trava anti-pedido-
    duplicado de sempre."""
    from app.constants import STATUS_PEDIDO_ENTREGUES
    return db.not_(db.and_(PedidoLoja.status.in_(STATUS_PEDIDO_ENTREGUES),
                           PedidoLoja.data_entrega > hoje_d))


def media_semanal_pedidos(horizonte_dias=7, janela_semanas=6,
                          inicio_offset_dias=0):
    """Modo MANUAL: devolve a media de cada (loja, produto) por DIA-DA-SEMANA — o
    sinal ESTAVEL, respeitando o PADRAO da loja (sabado != terca) — distribuida
    pelos dias LIVRES do horizonte, pro admin AJUSTAR na tela.

    Media por dow (Fase 1, 02/07/2026): `_media_recencia` com cap de pico
    isolado e denominador-com-zeros desde a 1a ocorrencia — a MESMA matematica
    do balanco da industria (antes era total/janela uniforme, sem protecao:
    pico avulso inflava a media por 6 semanas e item novo saia diluido). O
    historico usa quantidade_recebida quando preenchida e EXCLUI rascunhos
    gerados pela propria grade que nunca foram confirmados (anti auto-reforco).

    So distribui em dias que a loja ainda NAO pediu (dia travado vem disabled
    na tela e nao seria enviado no POST — alocar nele perderia a parcela em
    silencio). O gerar reusa o POST de pedidos_semana_gerar.

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

    # Historico por (loja, receita, DOW, DATA): base da media POR DIA-DA-SEMANA
    # (sabado != terca), guardada POR DATA pra media recencia-ponderada com cap
    # de pico isolado (mesma matematica do balanco — Fase 1, 02/07/2026).
    # Tres protecoes no sinal:
    # - usa quantidade_recebida quando preenchida (a divergencia conferida na
    #   entrega e demanda mais real que o pedido digitado);
    # - exclui RASCUNHO ABANDONADO: pedido gerado pela propria grade
    #   (observacao 'Gerado do histórico...') que continua 'pendente' — sem
    #   isso a media re-aprende o que a media criou (auto-reforco). Filtro
    #   NULL-safe: observacao NULL nunca casa LIKE, entao a condicao e um OR
    #   explicito. DECISAO 13/08/2026 (era da automacao de pedidos): pedido-
    #   maquina que SAIU de 'pendente' (separado/entregue) ENTRA no
    #   historico mesmo sem toque humano — exclui-lo pra sempre faria a
    #   media (denominador com zeros por data) definhar ate zero em
    #   ~janela_semanas e o motor 'pedidos' subestimar; o eco e limitado
    #   pela quantidade_recebida (conferencia humana na entrega) e medido
    #   em previsao_acuracia.circularidade_pct;
    # - cancelados continuam fora.
    from app.services.pedido_merge import MARCADOR_RASCUNHO_AUTO
    hist_lrd = defaultdict(lambda: defaultdict(
        lambda: defaultdict(lambda: defaultdict(int))))
    for rid, loja_id, data_ent, qtd, qtd_rec in (db.session.query(
            PedidoItem.receita_id, PedidoLoja.loja_id,
            PedidoLoja.data_entrega, PedidoItem.quantidade,
            PedidoItem.quantidade_recebida)
            .join(PedidoLoja, PedidoItem.pedido_id == PedidoLoja.id)
            .filter(PedidoItem.receita_id.isnot(None),
                    PedidoLoja.status != 'cancelado',
                    db.or_(PedidoLoja.status != 'pendente',
                           PedidoLoja.observacao.is_(None),
                           ~PedidoLoja.observacao.like(
                               MARCADOR_RASCUNHO_AUTO + '%')),
                    PedidoLoja.data_entrega >= hist_ini,
                    PedidoLoja.data_entrega <= hist_fim).all()):
        if data_ent is None or rid not in receitas:
            continue
        q = int(qtd_rec) if qtd_rec is not None else int(qtd or 0)
        hist_lrd[loja_id][rid][data_ent.weekday()][data_ent] += q
    datas_possiveis_dow = _datas_por_dow(hist_ini, hist_fim)

    # Dias que a loja JA tem pedido no horizonte (a tela marca) + status: dia
    # com UM pedido ainda EDITAVEL (pendente/confirmado) destrava na tela e o
    # gerar ATUALIZA os itens em vez de pular (pedidos_semana.aplicar_grade).
    ja_tem = defaultdict(set)
    status_dia = defaultdict(lambda: defaultdict(list))
    for loja_id, data_ent, status_p in (db.session.query(
            PedidoLoja.loja_id, PedidoLoja.data_entrega, PedidoLoja.status)
            .filter(PedidoLoja.status != 'cancelado',
                    _cond_sem_entrega_antecipada(hoje_d),
                    PedidoLoja.data_entrega >= inicio_d,
                    PedidoLoja.data_entrega <= horizonte_fim)
            .all()):
        if data_ent is not None:
            ja_tem[loja_id].add(data_ent.isoformat())
            status_dia[loja_id][data_ent.isoformat()].append(status_p)

    # O QUE ja foi pedido por (loja, dia, receita) no horizonte — a celula
    # travada mostra esse numero (o pedido REAL do dia) em vez de um 0 apagado,
    # pra quem olha a grade saber o que ja esta encomendado sem abrir o pedido.
    # Mesmo filtro de entrega antecipada do ja_tem: sem ele, a quantidade do
    # pedido ja-entregue vazaria pra celula EDITAVEL de um pedido vivo do
    # mesmo dia e o gerar inflaria o pedido real.
    ja_pedido = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    for loja_id, data_ent, rid_e, qtd_e in (db.session.query(
            PedidoLoja.loja_id, PedidoLoja.data_entrega,
            PedidoItem.receita_id, PedidoItem.quantidade)
            .join(PedidoItem, PedidoItem.pedido_id == PedidoLoja.id)
            .filter(PedidoLoja.status != 'cancelado',
                    _cond_sem_entrega_antecipada(hoje_d),
                    PedidoItem.receita_id.isnot(None),
                    PedidoLoja.data_entrega >= inicio_d,
                    PedidoLoja.data_entrega <= horizonte_fim).all()):
        if data_ent is not None:
            ja_pedido[loja_id][data_ent.isoformat()][rid_e] += int(qtd_e or 0)

    dias_out = [{'data': d.isoformat(),
                 'label': '%s %s' % (_DOW_PT[d.weekday()], d.strftime('%d/%m')),
                 'dow': d.weekday()} for d in dias_futuros]

    lojas_out = []
    for loja in lojas_op:
        ja_tem_loja = ja_tem.get(loja.id, set())
        produtos = []
        for rid, rec in sorted(receitas.items(), key=lambda kv: kv[1].nome):
            dows = hist_lrd.get(loja.id, {}).get(rid)
            # Item com pedido JA FEITO no horizonte aparece mesmo sem sugestao
            # (linha zerada + celulas azuis do ja-pedido) — sem isto o produto
            # cuja unica atividade cai em dia travado sumia da grade e ninguem
            # via o que ja estava encomendado (pedido do dono 02/07).
            ja_ped_item = [ja_pedido.get(loja.id, {})
                           .get(d.isoformat(), {}).get(rid, 0)
                           for d in dias_futuros]
            tem_pedido_horizonte = any(ja_ped_item)
            if not dows and not tem_pedido_horizonte:
                continue
            # Media POR DIA-DA-SEMANA com as protecoes do balanco (Fase 1):
            # recencia (meia-vida 21d — tendencia aparece), cap de pico isolado
            # (pedido gigante avulso nao vira sugestao recorrente) e denominador
            # com os zeros DESDE a 1a ocorrencia (demanda que parou decai; item
            # novo nao e diluido pelas semanas antes de existir). Sem gate de
            # ocorrencias: 1 pedido avulso ja e diluido naturalmente pelos
            # zeros do denominador.
            media_por_dow = {}
            if dows:
                for dow, por_data in dows.items():
                    m = _media_recencia(
                        por_data, hoje_d,
                        datas_possiveis=datas_possiveis_dow[dow])
                    if m > 0:
                        media_por_dow[dow] = m
            media_sem = sum(media_por_dow.values())

            fe = bool(getattr(rec, 'fornada_especial', False))
            # Dias onde a sugestao PODE cair: fornada especial respeitada E dia
            # LIVRE (loja ainda nao pediu). Travado NAO recebe — alocar nele
            # perderia a parcela (input disabled nao vai no POST).
            idx_validos = [
                i for i, d in enumerate(dias_futuros)
                if not (fe and d.weekday() not in _DIAS_FORNADA_ESPECIAL)
                and d.isoformat() not in ja_tem_loja
            ]
            if not idx_validos and not tem_pedido_horizonte:
                continue
            # Peso de cada dia livre = a media DAQUELE dia-da-semana. total_alocar
            # = soma das medias dos dias livres (so o que vai mesmo pra eles; nada
            # se perde em dia travado).
            pesos = [media_por_dow.get(dias_futuros[i].weekday(), 0.0)
                     for i in idx_validos]
            total_alocar = int(round(sum(pesos)))
            if total_alocar <= 0 and not tem_pedido_horizonte:
                continue
            caixa = int(rec.lote_pedido or 0)
            por_dia = [0] * len(dias_futuros)
            abaixo_lote = False
            if total_alocar <= 0:
                pass                                  # so o ja-pedido: linha zerada
            elif caixa > 1 and total_alocar >= caixa:
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
            # Piso de pedido (minimo_pedido — 11/07/2026, aprovado pelo
            # dono): este motor devolvia `minimo` so como metadado, ao
            # contrario do venda+estoque, que impoe. Dia com sugestao
            # ABAIXO do minimo tem a quantidade FUNDIDA no dia livre de
            # maior alocacao — o TOTAL da semana nao muda (nao inflamos a
            # media), as entregas se concentram em menos dias que fecham o
            # piso. Semana inteira abaixo do minimo: NAO forcamos (mesma
            # decisao do "abaixo da caixa") — badge pro admin decidir.
            minimo = int(rec.minimo_pedido or 0)
            abaixo_minimo = False
            if minimo > 1 and sum(por_dia) > 0:
                if sum(por_dia) < minimo:
                    abaixo_minimo = True
                else:
                    while True:
                        baixos = [i for i in idx_validos
                                  if 0 < por_dia[i] < minimo]
                        if not baixos:
                            break
                        i_baixo = min(baixos, key=lambda i: por_dia[i])
                        outros = [j for j in idx_validos
                                  if j != i_baixo and por_dia[j] > 0]
                        if not outros:
                            break                 # nunca: total >= minimo
                        j_alvo = max(outros, key=lambda j: por_dia[j])
                        por_dia[j_alvo] += por_dia[i_baixo]
                        por_dia[i_baixo] = 0
            produtos.append({
                'receita_id': rid, 'nome': rec.nome,
                'media_semanal': round(media_sem, 1),
                'estoque_atual': estoque_atual[loja.id].get(rid, 0),
                'por_dia': por_dia, 'total': sum(por_dia),
                # O ja encomendado por dia (celula travada mostra isso).
                'ja_pedido': ja_ped_item,
                'lote': caixa,                       # caixa: arredonda ao dividir
                'minimo': minimo,
                'abaixo_lote': abaixo_lote,
                'abaixo_minimo': abaixo_minimo,
                # Profundidade da amostra (datas com pedido na janela) — a
                # tela marca "pouco histórico" quando a média vem de 1-2
                # pontos, pro operador saber quanto confiar no número.
                'n_datas': (sum(len(pd) for pd in dows.values())
                            if dows else 0),
            })
        if produtos:
            lojas_out.append({
                'loja_id': loja.id, 'loja_nome': loja.nome,
                'produtos': produtos,
                'ja_tem': sorted(ja_tem_loja),
                # Dias travados que a tela DESTRAVA pra edicao: exatamente UM
                # pedido, ainda editavel (dois pedidos no dia = ambiguo, e
                # separado+ ja esta no fluxo fisico — so pela tela do pedido).
                'editaveis': sorted(
                    d for d, sts in status_dia.get(loja.id, {}).items()
                    if len(sts) == 1 and sts[0] in STATUS_PEDIDO_EDITAVEIS),
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


def sugerir_pedidos_por_venda(horizonte_dias=7, janela_semanas=6,
                              inicio_offset_dias=0, seguranca_pct=0,
                              ressincronizar_datas=None):
    """Maneira 2 — previsao de pedido por VENDA + ESTOQUE (ponto de reposicao).

    `ressincronizar_datas` (10/08/2026, auto-pedidos): datas cujo RASCUNHO
    AUTOMATICO do cron (status 'pendente', sem autor humano, observacao
    'Gerado do histórico%') deve ser tratado como SUBSTITUIVEL — o pedido
    dele sai do `ja_tem` e das entregas simuladas e a sugestao nasce fresca,
    porque o `aplicar_grade` da rodada vai sobrescrever esses itens. Sem o
    param (tela), dia com QUALQUER pedido segue travado como sempre — e dia
    com pedido de HUMANO segue travado mesmo listado aqui (as linhas dele
    nao sao rascunho automatico).

    Pra cada (loja, receita): mede o consumo medio POR DIA-DA-SEMANA e simula o
    estoque dia a dia partindo do saldo ATUAL. Quando o estoque projetado nao
    cobre o consumo do dia, pede o deficit ARREDONDADO PRA CIMA na caixa (lote)
    — o excedente vira estoque que cobre os proximos dias, entao a caixa NAO
    super-pede item lento (pede 1 caixa a cada N dias). Entrega diaria (v1):
    cada dia cobre a venda daquele dia.

    Fase 1 (02/07/2026) — o sinal de consumo ficou mais fiel e protegido:
    - DEMANDA unificada (constants.VENDA_TIPOS_DEMANDA_LOJA): todos os canais
      + venda MANUAL da tela de estoque; estornos subtraem com o sinal de
      gravacao de cada canal (venda cancelada nao infla a media).
    - MERMA ESTRUTURAL projetada como consumo (constants.MERMA_TIPOS_PROJECAO:
      devolucao a industria + perda) — croissant devolvido toda semana pra
      virar Almond consome estoque e era sub-pedido. Sobra/descarte ficam FORA
      (excesso nao se repoe).
    - Media por dow via `_media_recencia` (recencia + cap de pico isolado +
      zeros desde a 1a ocorrencia) — antes era total/janela uniforme.
    - `seguranca_pct`: estoque de seguranca opcional (N% da venda do dia vira
      piso de fim de dia; 0 = repor exatamente a media, comportamento antigo).
    - minimo_pedido da receita/MP aplicado como piso do pedido do dia.

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

    # A tela cobre receitas E materias-primas MARCADAS pra pedido de loja
    # (checkbox `sugerir_pedido_loja` no banco de MPs — ex: pao de queijo
    # congelado, comprado em saco e vendido via cones; a venda do cone baixa a
    # linha MP da loja). Opt-in de proposito: nem toda MP que passa por loja e
    # pedida pra industria. Token unico por item: receita = o proprio id (int,
    # compat com o gerar existente); MP = 'mp:<id>' (o gerar reconhece o prefixo).
    from app.models import MateriaPrima
    mps = {m.id: m for m in MateriaPrima.query
           .filter(MateriaPrima.sugerir_pedido_loja.is_(True),
                   MateriaPrima.arquivada_em.is_(None)).all()}

    def _token(rid, mid):
        if rid is not None:
            return rid if rid in receitas else None
        return f'mp:{mid}' if mid in mps else None

    # Consumo por (loja, item, dow, DATA) na janela: MovEstoqueLoja x
    # EstoqueLoja (a linha diz loja+item). Guardado POR DATA pra media
    # recencia-ponderada com cap de pico. Duas series separadas:
    # - venda (demanda unificada, estornos com sinal — clampada em 0 por data);
    # - merma estrutural (devolucao a industria + perda) que tambem consome.
    from app.constants import (
        MERMA_TIPOS_PROJECAO,
        VENDA_ESTORNO_SINAL_DEMANDA,
        VENDA_TIPOS_DEMANDA_COM_ESTORNO,
    )
    venda_hist = defaultdict(lambda: defaultdict(
        lambda: defaultdict(lambda: defaultdict(int))))
    merma_hist = defaultdict(lambda: defaultdict(
        lambda: defaultdict(lambda: defaultdict(int))))
    tipos_consumo = VENDA_TIPOS_DEMANDA_COM_ESTORNO + MERMA_TIPOS_PROJECAO
    for loja_id, rid, mid, tipo_mov, data_mov, qtd in (db.session.query(
            EstoqueLoja.loja_id, EstoqueLoja.receita_id,
            EstoqueLoja.materia_prima_id, MovEstoqueLoja.tipo,
            MovEstoqueLoja.data, MovEstoqueLoja.quantidade)
            .join(EstoqueLoja, MovEstoqueLoja.estoque_loja_id == EstoqueLoja.id)
            .filter(db.or_(EstoqueLoja.receita_id.isnot(None),
                           EstoqueLoja.materia_prima_id.isnot(None)),
                    MovEstoqueLoja.tipo.in_(tipos_consumo),
                    MovEstoqueLoja.data >= datetime.combine(hist_ini, time.min),
                    MovEstoqueLoja.data <= datetime.combine(hist_fim, time.max))
            .all()):
        tok = _token(rid, mid)
        if tok is None or data_mov is None:
            continue
        d_mov = data_mov.date()
        if tipo_mov in MERMA_TIPOS_PROJECAO:
            merma_hist[loja_id][tok][d_mov.weekday()][d_mov] += int(qtd or 0)
        else:
            sinal = VENDA_ESTORNO_SINAL_DEMANDA.get(tipo_mov, 1)
            venda_hist[loja_id][tok][d_mov.weekday()][d_mov] += \
                sinal * int(qtd or 0)
    # Estorno de venda de outro dia pode deixar o liquido do dia negativo —
    # demanda negativa nao existe; clampa por data em 0.
    for por_tok in venda_hist.values():
        for por_dow in por_tok.values():
            for por_data in por_dow.values():
                for d_mov, v in list(por_data.items()):
                    if v < 0:
                        por_data[d_mov] = 0
    datas_possiveis_dow = _datas_por_dow(hist_ini, hist_fim)
    seguranca = max(0.0, min(float(seguranca_pct or 0), 100.0)) / 100.0

    # Estoque DISPONIVEL da loja por (loja, item) = quantidade - reservado
    # (reservado segura pedido online aguardando pagamento). Usar o fisico
    # contaria reserva como disponivel e sub-pediria.
    estoque_atual = defaultdict(lambda: defaultdict(int))
    # Estoque MINIMO por (loja, item): piso da sugestao — o alvo do dia nunca
    # cai abaixo dele (mantem colchao do item na loja). Vem da MESMA linha.
    minimo_loja = defaultdict(lambda: defaultdict(int))
    # Pedido minimo DIARIO por (loja, item): piso INCONDICIONAL do pedido de
    # cada dia — NAO desconta o estoque que sobrou (dono 17/08/2026,
    # danishes assadas: "receber 2 por dia impreterivelmente"). A media de
    # venda manda quando passa do piso.
    diario_loja = defaultdict(lambda: defaultdict(int))
    for loja_id, rid, mid, q, qres, emin, pdia in (db.session.query(
            EstoqueLoja.loja_id, EstoqueLoja.receita_id,
            EstoqueLoja.materia_prima_id,
            EstoqueLoja.quantidade, EstoqueLoja.quantidade_reservada,
            EstoqueLoja.estoque_minimo, EstoqueLoja.pedido_minimo_diario)
            .filter(db.or_(EstoqueLoja.receita_id.isnot(None),
                           EstoqueLoja.materia_prima_id.isnot(None))).all()):
        tok = _token(rid, mid)
        if tok is None:
            continue
        estoque_atual[loja_id][tok] += max(0, int(q or 0) - int(qres or 0))
        if emin:
            minimo_loja[loja_id][tok] = max(minimo_loja[loja_id][tok], int(emin))
        if pdia:
            diario_loja[loja_id][tok] = max(diario_loja[loja_id][tok],
                                            int(pdia))

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

    # Dias ja pedidos no horizonte (a tela trava; o gerar pula) + a QUANTIDADE ja
    # pedida por (loja, data, receita) — pra simulacao usar a entrega real do dia
    # travado como carry, em vez da sugestao (que nao sera criada).
    # Com `ressincronizar_datas`, o (loja, dia) SUBSTITUIVEL e pulado nos
    # DOIS levantamentos (dia destrava E a quantidade dele sai da entrega
    # simulada — ele vai ser sobrescrito pela propria rodada, contar seria
    # dobrar a reposicao). SUBSTITUIVEL = data listada E ocupada SO por
    # rascunho(s) automatico(s) do cron. Dia MISTO (rascunho + pedido de
    # humano) NAO e substituivel: o gerar pula o dia (protegido pelo
    # humano), o rascunho segue vivo e a quantidade dele TEM que continuar
    # no carry — excluir so a linha do rascunho num dia misto inflava D+2
    # (achado da revisao rodada 2, reproduzido).
    from app.services.pedido_merge import MARCADOR_RASCUNHO_AUTO
    ressinc = {d.isoformat() if hasattr(d, 'isoformat') else str(d)
               for d in (ressincronizar_datas or [])}

    def _rascunho_auto(status_p, criado, modif, obs):
        return (status_p == 'pendente' and criado is None and modif is None
                and (obs or '').startswith(MARCADOR_RASCUNHO_AUTO))

    ja_tem = defaultdict(set)
    status_dia = defaultdict(lambda: defaultdict(list))
    pedido_existente = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    _rows_horizonte = (db.session.query(
            PedidoLoja.loja_id, PedidoLoja.data_entrega, PedidoLoja.status,
            PedidoLoja.criado_por, PedidoLoja.modificado_por_id,
            PedidoLoja.observacao)
        .filter(PedidoLoja.status != 'cancelado',
                _cond_sem_entrega_antecipada(hoje_d),
                PedidoLoja.data_entrega >= inicio_d,
                PedidoLoja.data_entrega <= horizonte_fim)
        .all())
    _dias_sub = set()
    _dias_com_outro = set()
    for loja_id, data_ent, status_p, criado_p, modif_p, obs_p in _rows_horizonte:
        if data_ent is None:
            continue
        chave_ld = (loja_id, data_ent.isoformat())
        if _rascunho_auto(status_p, criado_p, modif_p, obs_p):
            if data_ent.isoformat() in ressinc:
                _dias_sub.add(chave_ld)
        else:
            _dias_com_outro.add(chave_ld)
    _dias_sub -= _dias_com_outro

    for loja_id, data_ent, status_p, _c, _m, _o in _rows_horizonte:
        if data_ent is None:
            continue
        if (loja_id, data_ent.isoformat()) in _dias_sub:
            continue
        ja_tem[loja_id].add(data_ent.isoformat())
        status_dia[loja_id][data_ent.isoformat()].append(status_p)
    # As entregas ja pedidas entram desde HOJE (nao so da janela): com
    # "A partir de" no futuro, a simulacao pre-janela precisa creditar o que
    # chega antes do inicio (ver dias_pre_janela abaixo). Na faixa PRE-janela
    # so entra pedido AINDA NAO entregue: o que ja virou entregue/recebido ja
    # esta dentro do estoque atual da loja (entrada_pedido no recebimento) —
    # creditar de novo contaria em dobro e sub-pediria (achado de revisao
    # 11/07/2026). Dentro da janela o comportamento segue identico (a celula
    # travada mostra o pedido do dia, entregue ou nao).
    from app.constants import STATUS_PEDIDO_FINALIZADOS
    _status_entregues = tuple(s for s in STATUS_PEDIDO_FINALIZADOS
                              if s != 'cancelado')
    for loja_id, data_ent, rid_e, mid_e, qtd_e in (db.session.query(
            PedidoLoja.loja_id, PedidoLoja.data_entrega,
            PedidoItem.receita_id, PedidoItem.materia_prima_id,
            PedidoItem.quantidade)
            .join(PedidoItem, PedidoItem.pedido_id == PedidoLoja.id)
            .filter(PedidoLoja.status != 'cancelado',
                    _cond_sem_entrega_antecipada(hoje_d),
                    db.or_(PedidoItem.receita_id.isnot(None),
                           PedidoItem.materia_prima_id.isnot(None)),
                    PedidoLoja.data_entrega >= hoje_d,
                    PedidoLoja.data_entrega <= horizonte_fim,
                    db.or_(PedidoLoja.data_entrega >= inicio_d,
                           PedidoLoja.status.notin_(_status_entregues)))
            .all()):
        tok = _token(rid_e, mid_e)
        if data_ent is None or tok is None:
            continue
        # (loja, dia) substituivel: TODO pedido do dia e rascunho do cron —
        # a entrega dele sai da simulacao (sera reescrita nesta rodada).
        if (loja_id, data_ent.isoformat()) in _dias_sub:
            continue
        pedido_existente[loja_id][data_ent.isoformat()][tok] += int(qtd_e or 0)

    # Dias entre HOJE e o inicio da janela ("A partir de" no futuro). O saldo
    # inicial da simulacao NAO pode ser o estoque de hoje: a loja consome (e
    # recebe as entregas ja pedidas) ate o inicio. Sem isso a janela deslocada
    # partia de estoque otimista e SUB-pedia (bug corrigido 11/07/2026).
    dias_pre_janela = [hoje_d + timedelta(days=i)
                       for i in range(inicio_offset_dias)]

    dias_out = [{'data': d.isoformat(),
                 'label': '%s %s' % (_DOW_PT[d.weekday()], d.strftime('%d/%m')),
                 'dow': d.weekday()} for d in dias_futuros]

    # Catalogo unificado da tela: receitas + MPs marcadas (checkbox).
    # Cada entrada: (token, nome, lote, minimo, fornada_especial, rid, mid).
    # MP tambem tem caixa/piso desde 02/07 (colunas lote_pedido/minimo_pedido
    # em MateriaPrima — ex: pao de queijo comprado em saco nao sai picado).
    catalogo = [(rid, rec.nome, int(rec.lote_pedido or 0),
                 int(rec.minimo_pedido or 0),
                 bool(getattr(rec, 'fornada_especial', False)), rid, None)
                for rid, rec in receitas.items()]
    for mid, m in mps.items():
        catalogo.append((f'mp:{mid}', m.nome,
                         int(getattr(m, 'lote_pedido', None) or 0),
                         int(getattr(m, 'minimo_pedido', None) or 0),
                         False, None, mid))
    catalogo.sort(key=lambda c: (c[1] or '').lower())

    def _media_dow(por_dow, dow_i):
        por_data = (por_dow or {}).get(dow_i)
        if not por_data:
            return 0.0
        return _media_recencia(por_data, hoje_d,
                               datas_possiveis=datas_possiveis_dow[dow_i])

    lojas_out = []
    for loja in lojas_op:
        ja_tem_loja = ja_tem.get(loja.id, set())
        pede_loja = pede_receitas.get(loja.id, set())
        produtos = []
        for tok, nome_item, caixa, minimo, fe, rid, mid in catalogo:
            v_dows = venda_hist.get(loja.id, {}).get(tok)
            m_dows = merma_hist.get(loja.id, {}).get(tok)
            est0 = estoque_atual.get(loja.id, {}).get(tok, 0)
            minimo_est = minimo_loja.get(loja.id, {}).get(tok, 0)
            diario = diario_loja.get(loja.id, {}).get(tok, 0)
            pede = tok in pede_loja
            # Pedido JA FEITO no horizonte tambem inclui o item (linha com as
            # celulas azuis do ja-pedido) — mesma regra da tela de media.
            ja_ped_item = [pedido_existente.get(loja.id, {})
                           .get(d.isoformat(), {}).get(tok, 0)
                           for d in dias_futuros]
            # Item com estoque minimo OU pedido diario cadastrado NUNCA some:
            # o dono quer o colchao/a entrega diaria, entao ele aparece e e
            # pedido mesmo sem venda/estoque.
            if not v_dows and not m_dows and est0 <= 0 and not pede \
                    and not any(ja_ped_item) and minimo_est <= 0 \
                    and diario <= 0:
                continue                          # nao vende/estoca/pede, nada pedido
            estoque = est0
            # Projeta o saldo ate o inicio da janela (offset > 0): consumo
            # previsto + entregas AINDA NAO entregues que chegam antes do
            # inicio. Nada e sugerido aqui (fora da grade). O saldo e
            # clampado em 0 por dia: estoque projetado negativo e venda
            # PERDIDA (nao vira demanda acumulada) — sem o clamp, a janela
            # abriria pedindo a venda perdida de volta e SUPER-pediria.
            # (Difere DE PROPOSITO do dia travado dentro da janela, que NAO
            # clampa: la o deficit segue visivel na propria grade.)
            for d in dias_pre_janela:
                # Fornada especial nao vende fora de sab/dom, mas uma
                # entrega agendada num dia comum ainda credita o saldo.
                if fe and d.weekday() not in _DIAS_FORNADA_ESPECIAL:
                    consumo_pre = 0.0
                else:
                    consumo_pre = (_media_dow(v_dows, d.weekday())
                                   + _media_dow(m_dows, d.weekday()))
                entrega_pre = pedido_existente.get(loja.id, {}).get(
                    d.isoformat(), {}).get(tok, 0)
                estoque = max(0.0, estoque + entrega_pre - consumo_pre)
            por_dia = [0] * len(dias_futuros)
            venda_total = 0.0
            for i, d in enumerate(dias_futuros):
                if fe and d.weekday() not in _DIAS_FORNADA_ESPECIAL:
                    continue                      # fornada especial: nao vende
                venda_d = _media_dow(v_dows, d.weekday())
                merma_d = _media_dow(m_dows, d.weekday())
                consumo_d = venda_d + merma_d     # o que baixa o estoque no dia
                venda_total += venda_d            # coluna Venda/sem = so venda
                if d.isoformat() in ja_tem_loja:
                    # Dia travado: a tela nao deixa sugerir e o gerar pula. O
                    # estoque projetado recebe a ENTREGA JA PEDIDA (qtd real),
                    # nao a sugestao — senao os dias seguintes herdariam uma
                    # reposicao que nao vai existir (sub-pedido) ou ignorariam a
                    # entrega real (super-pedido).
                    entrega = pedido_existente.get(loja.id, {}).get(
                        d.isoformat(), {}).get(tok, 0)
                    por_dia[i] = 0
                    estoque = estoque + entrega - consumo_d
                    continue
                # Alvo do dia = consumo + estoque de seguranca opcional (sobra
                # N% do consumo no fim do dia como colchao contra dia acima da
                # media). seguranca=0 -> repoe exatamente a media (v1).
                #
                # _EPS_ULP no ceil: a media de recencia e num/den de floats —
                # quando o valor exato e INTEIRO (6 segundas de 7 un = 7.0),
                # a soma pode sair 1 ulp ACIMA (7.000000000000001, varia com a
                # ordem/dia) e o ceil inflava pra 8 (ou +1 CAIXA inteira no
                # ramo com lote). Nao e tolerancia de negocio: 1e-9 << 1
                # unidade; so tira o ruido do ultimo bit. Caso real 05/07 —
                # media exibida 7,0 e sugestao 8; CI flakava no mesmo ponto.
                # Piso do estoque minimo da loja: o alvo do dia (consumo +
                # seguranca) nunca cai abaixo do minimo cadastrado — a loja
                # repoe ate o colchao mesmo em dia fraco de venda. minimo_est=0
                # (sem cadastro) mantem o comportamento antigo exato.
                deficit = max(consumo_d * (1.0 + seguranca), minimo_est) - estoque
                if deficit > 1e-9:
                    pedido = (int(ceil(deficit / caixa - _EPS_ULP)) * caixa
                              if caixa > 1 else int(ceil(deficit - _EPS_ULP)))
                    # Piso do pedido (minimo_pedido): eleva e re-fecha na caixa.
                    # O excedente vira carry — os dias seguintes pedem menos.
                    if minimo > 0 and pedido < minimo:
                        pedido = (int(ceil(minimo / caixa)) * caixa
                                  if caixa > 1 else minimo)
                else:
                    pedido = 0
                # Pedido minimo DIARIO (dono 17/08/2026, danishes assadas):
                # piso INCONDICIONAL do dia — vale mesmo com estoque
                # sobrando (a loja recebe fresco "impreterivelmente"; a
                # sobra e assunto do lancamento de sobras). A media manda
                # quando o pedido calculado ja passa do piso.
                if diario > 0 and pedido < diario:
                    pedido = (int(ceil(diario / caixa)) * caixa
                              if caixa > 1 else diario)
                por_dia[i] = pedido
                estoque = estoque + pedido - consumo_d
            # Mostra TODOS os produtos do "mundo" da loja (vende/estoca/pede),
            # mesmo com sugestao 0 (decisao do dono: nada some da tela). So pula
            # o que nem venda, nem estoque, nem pedido tem (ja filtrado acima).
            produtos.append({
                'receita_id': rid, 'materia_prima_id': mid,
                'item_key': str(tok), 'eh_mp': mid is not None,
                'nome': nome_item,
                'media_semanal': round(venda_total * 7.0 / horizonte_dias, 1),
                'estoque_atual': est0,
                'por_dia': por_dia, 'total': sum(por_dia),
                # O ja encomendado por dia (celula travada mostra isso).
                'ja_pedido': ja_ped_item,
                'lote': caixa,
                'minimo': minimo,
                # Estoque minimo da loja pra este item (0 = sem piso): a tela
                # pode mostrar/editar o colchao configurado.
                'estoque_minimo': minimo_est,
                # Pedido minimo diario (0 = sem piso incondicional).
                'pedido_minimo_diario': diario,
                'abaixo_lote': False,
                # Profundidade da amostra de VENDA (datas com baixa na
                # janela) — a tela marca "pouco histórico" quando a média
                # vem de 1-2 pontos.
                'n_datas': (sum(len(pd) for pd in v_dows.values())
                            if v_dows else 0),
            })
        if produtos:
            lojas_out.append({
                'loja_id': loja.id, 'loja_nome': loja.nome,
                'produtos': produtos,
                'ja_tem': sorted(ja_tem.get(loja.id, set())),
                # Dias travados que a tela destrava pra edicao (1 pedido, ainda
                # pendente/confirmado) — mesma regra da tela de media.
                'editaveis': sorted(
                    d for d, sts in status_dia.get(loja.id, {}).items()
                    if len(sts) == 1 and sts[0] in STATUS_PEDIDO_EDITAVEIS),
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
            if (ing.tipo or '') not in SUB_RECEITA_TIPOS:
                continue
            sid = ing.sub_receita_id
            if sid is None:                       # fallback por nome exato
                alvo = (ing.ingrediente_nome or '').strip().lower()
                sid = next((r.id for r in receitas.values()
                            if (r.nome or '').strip().lower() == alvo), None)
            if sid in receitas and rend > 0:
                out.append((sid, unidades_subreceita(
                    ing.tipo, ing.porcentagem, rec.peso_base) / rend))
        return out

    # Receitas de RETORNO (destino de retorno_receita_id): nao sao produziveis
    # — so entram por devolucao de loja. Nunca geram producao nem cascata.
    from app.models import Receita as _Receita
    retorno_ids = {rid for (rid,) in db.session.query(_Receita.retorno_receita_id)
                   .filter(_Receita.retorno_receita_id.isnot(None)).distinct()}

    # BOM transitivo a partir dos finais.
    bom = {}
    pilha = [rr['receita_id'] for rr in receitas_out]
    while pilha:
        rid = pilha.pop()
        if rid in bom:
            continue
        bom[rid] = [] if rid in retorno_ids else _subs(rid)
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
            if it.get('estoque_nao_abate'):
                # Flag da ficha (dono 19/07/2026): o fisico nao entra na
                # conta — so a producao JA MANDADA (plano de hoje, WIP)
                # cobre consumo. Vale tambem pra cobertura da vespera.
                efetivo = int(it.get('em_producao', 0) or 0)
            else:
                efetivo = int(it.get('em_estoque_efetivo', it.get('em_estoque', 0)) or 0)
            demanda = max(int(it.get('comprometido', 0) or 0),
                          int(it.get('previsto', 0) or 0))
            return max(0, efetivo - demanda)
        rec_f = receitas.get(rid)
        if rec_f is not None and getattr(rec_f, 'estoque_nao_abate', False):
            return 0
        return est_extra.get(rid, 0)

    # Cap "so de sobras" ANTES da propagacao: pai que consome retorno produz no
    # maximo o que o estoque devolvido cobre (corta dos ULTIMOS dias — os
    # primeiros seguem a curva de demanda). O balanco ja capa o total; aqui o
    # cap protege o plano re-editado/re-distribuido do cronograma tambem.
    caps_ret, _rids = _caps_por_retorno(receitas, _estoque_livre)
    for rid, lim in caps_ret.items():
        rr = linhas.get(rid)
        if rr is None:
            continue
        atual = prod.get(rid, [0] * n)
        excesso = sum(atual) - lim['cap']
        if excesso <= 0:
            continue
        for i in range(n - 1, -1, -1):
            if excesso <= 0:
                break
            corte = min(atual[i], excesso)
            atual[i] -= corte
            excesso -= corte
        prod[rid] = atual
        for i, c in enumerate(rr['por_dia']):
            c['qtd'] = atual[i]
        rr['total'] = sum(atual)
        rr['limitado_por_retorno'] = lim

    for rid in ordem:
        cons = consumo[rid]
        if sum(cons) > 0:                          # recebeu demanda de pais
            L = lead.get(rid, 0)
            # producao do dia i serve o consumo em (i+L). REGRA DA VESPERA
            # (dono, 10/07/2026): consumo que cai DENTRO do lead do insumo
            # (dias 0..L-1) nao tem vespera dentro do grid pra produzir —
            # so estoque JA PRONTO cobre (massa feita hoje so vira croissant
            # amanha). Antes esse consumo era jogado em "produzir hoje", o
            # que agendava bola de massa inutil pro proprio dia (caso real:
            # 300 croissants HOJE puxavam 6 bolas HOJE, que nao servem). O
            # que o estoque nao cobrir vira AVISO (insumo_sem_vespera), nao
            # producao.
            gross = [0.0] * n
            dentro_lead = 0.0            # consumo sem vespera possivel
            dentro_dias = []
            for d_idx in range(n):
                if cons[d_idx] > 0:
                    if d_idx < L:
                        dentro_lead += cons[d_idx]
                        dentro_dias.append(d_idx)
                    else:
                        gross[d_idx - L] += cons[d_idx]
            # NAO arredonda por dia (era o D1): dar ceil em CADA dia inflava
            # insumo de fracao baixa — "Massa para folhar" ~0,6/dia virava 1/dia
            # (67% a mais). A fracao ACUMULA entre os dias; produz o inteiro do
            # TOTAL (ceil da demanda liquida) distribuido, nao a soma dos ceils.
            livre = _estoque_livre(rid)
            # O estoque cobre PRIMEIRO o consumo sem vespera (e o mais
            # iminente — ja esta acontecendo); o que sobrar cobre o resto.
            cobre_lead = min(livre, dentro_lead)
            sem_vespera = dentro_lead - cobre_lead
            running = livre - cobre_lead
            livre_rest = running
            residual = []
            for g in gross:                          # gross FRACIONARIO
                cobre = min(running, g)
                running -= cobre
                residual.append(g - cobre)
            # Retorno NAO e produzivel: com os pais capados, o consumo cabe no
            # estoque (extra=0); este guard segura qualquer caminho que escape
            # (ex: plano re-editado a mao) — a linha mostra o consumo, sem
            # mandar o padeiro "produzir" devolucao.
            if rid in retorno_ids:
                extra = 0
            else:
                extra = int(ceil(max(0.0, sum(gross) - livre_rest)))
            pesos = residual if sum(residual) > 0 else gross
            rec = receitas.get(rid)
            # Insumo tambem respeita os dias de producao (dono 17/08/2026:
            # fim de semana nao produz): levain da vespera de segunda cairia
            # no DOMINGO — rola pro ultimo dia permitido anterior (sexta;
            # produzir mais cedo chega a tempo, custo = geladeira). Sem dia
            # permitido antes, a parcela nao produz (vespera ja passou — o
            # que o estoque nao cobrir aparece via insumo_sem_vespera/risco).
            permitido_i = [producao_permitida_no_dia(rec, p)
                           for p in dias_prod]
            pesos = _rolar_pesos_permitidos(pesos, permitido_i)
            if sum(pesos) <= 0:
                extra = 0     # _distribuir_inteiro despejaria no dia 0
            add = _distribuir_inteiro(extra, pesos)
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
                      'por_dia': por_dia, 'total': sum(novo), 'insumo': True,
                      'retorno': rid in retorno_ids}
                receitas_out.append(rr)
                linhas[rid] = rr
            else:                                  # vendida + insumo: acumula
                for i, c in enumerate(rr['por_dia']):
                    c['qtd'] = novo[i]
                    c['fornadas'] = _forn(novo[i])
                rr['total'] = sum(novo)
            # Consumo TOTAL derivado na janela — mostrado na linha do insumo
            # mesmo quando produzir=0 (estoque cobre a demanda): sem isso a
            # tela parecia "nao calculou nada" (caso real 03/07/2026: 10.000
            # pains = 333 bolas de massa, engolidas pelo estoque de 900).
            rr['consumo_janela'] = round(sum(cons), 1)
            # Regra da vespera: consumo iminente (dentro do lead) que o
            # estoque pronto NAO cobre — nao da mais tempo de produzir o
            # insumo. Vira aviso visivel na linha; NUNCA producao no grid.
            if sem_vespera > 0.01:
                rr['insumo_sem_vespera'] = {
                    'faltam': round(sem_vespera, 1),
                    'coberto': round(cobre_lead, 1),
                    'lead': L,
                    'dias': [dias_prod[i].isoformat() for i in dentro_dias],
                }
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
                        inicio_offset_dias=0, equilibrar=False,
                        motor='pedidos'):
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
    if motor not in MOTORES_PREVISAO_PRODUCAO:
        motor = 'pedidos'

    # Fonte da verdade do TOTAL por receita: o balanco. O cronograma so
    # distribui esse "Produzir" pelos dias — garante que os totais batem.
    bal = balanco_industria(horizonte_dias=horizonte_dias,
                            janela_semanas=janela_semanas, usar_cache=False,
                            inicio_offset_dias=inicio_offset_dias,
                            motor=motor)

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
    # Motor 'vendas'/'maior': curva diaria a partir da VENDA real — mesma
    # fonte usada pelo balanco acima (os totais e a curva ficam coerentes).
    qtd_dow_v, soma_v, residual_v = {}, {}, {}
    if motor in ('vendas', 'maior'):
        qtd_dow_v, soma_v, _datas_v = _hist_vendas_receita_por_dow(
            hist_ini, hist_fim)
        residual_v = {rid: _taxa_residual(qtd_dow_v.get(rid, {}),
                                          soma_v.get(rid, 0),
                                          dias_calendario_janela)
                      for rid in soma_v}

    def _previsto_dia(rid, dia):
        if not _fornada_no_dia(receitas.get(rid), dia):
            return 0.0
        dow = dia.weekday()
        p_ped = _previsto_dow(
            qtd_dow[rid].get(dow), hoje_d, residual_rate.get(rid, 0.0),
            datas_possiveis=datas_possiveis_dow[dow])
        if motor == 'pedidos':
            return p_ped
        p_ven = _previsto_dow(
            qtd_dow_v.get(rid, {}).get(dow), hoje_d, residual_v.get(rid, 0.0),
            datas_possiveis=datas_possiveis_dow[dow])
        return p_ven if motor == 'vendas' else max(p_ped, p_ven)

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
        # Flag "estoque nao abate": a curva usa o MESMO numero da conta do
        # balanco (so WIP), senao os primeiros dias sairiam esvaziados por um
        # fisico que o produzir ignorou.
        if it.get('estoque_nao_abate'):
            estoque_efetivo = int(it.get('em_producao', 0) or 0)
        else:
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
        # Dias PERMITIDOS de producao (fonte unica producao_permitida_no_dia):
        # fornada especial produz so sex/sab (dono 10/08/2026 — a venda de
        # sab/dom sai da vespera); TODA receita normal produz so seg-sex
        # (dono 17/08/2026 — fim de semana nao produz; a demanda de sab/dom
        # sai de sexta). O peso de um dia bloqueado vai pro ultimo dia
        # PERMITIDO anterior (produzir mais cedo chega a tempo; mais tarde
        # nao). Sem nenhum dia permitido antes (ex: grid comecando no
        # sabado), a demanda fica sem peso: se nada mais puxar, a linha nao
        # produz e a entrega aparece como EM RISCO — decisao humana, o
        # cronograma nao viola a regra por conta propria.
        #
        # `ref_pesos[i]` = PARCELAS da celula i por dia de DEMANDA: lista de
        # [dia_ref, peso]. A rolagem do fim de semana carrega a referencia
        # junto — o nivelamento mede a antecedencia POR PARCELA contra a
        # necessidade real (a parcela de sexta pode adiantar; a de domingo
        # rolada pra sexta nao — pao de domingo assado na quarta teria 4
        # dias). Um ref unico por celula (max) congelava a celula inteira
        # e o croissant voltava a 1000 num dia (regressao pega em prod).
        permitido = [producao_permitida_no_dia(rec, p) for p in dias_prod]
        ref_pesos = [[[i, float(pesos[i])]] if pesos[i] > 0 else []
                     for i in range(horizonte_dias)]
        if not all(permitido):
            ajust = [0.0] * horizonte_dias
            novo_refs = [[] for _ in range(horizonte_dias)]
            for i, w in enumerate(pesos):
                if w <= 0:
                    continue
                j = next((k for k in range(i, -1, -1) if permitido[k]), None)
                if j is not None:
                    ajust[j] += float(w)
                    novo_refs[j].append([i, float(w)])
            pesos = ajust
            ref_pesos = novo_refs
        # Padroniza a PRODUCAO em LOTES inteiros (nao produzir picado — decisao
        # do dono 29/06): arredonda o total pro multiplo do lote da receita e
        # distribui em pacotes inteiros pelos dias (cada dia 0 ou multiplo do
        # lote). O total passa a ser multiplo do lote — pode divergir um pouco
        # do "Produzir" exato do balanco; e o custo de produzir em batidas
        # redondas. NAO usa o 'minimo' do pedido (piso e regra de PEDIDO da loja,
        # nao de producao). Sem lote -> distribuicao exata como antes.
        #
        # lote_producao (decisao do dono 02/07, focaccia = placa de 8): lote SO
        # da producao — o pedido de loja fica livre — e arredonda PRA CIMA
        # (ceil): nunca produz menos que a demanda; a sobra da placa fica na
        # industria e o balanco desconta no dia seguinte. Sem ele, herda
        # lote_pedido com o arredondamento original (mais proximo, 29/06).
        lote_prod = int(getattr(rec, 'lote_producao', 0) or 0)
        lote = lote_prod or int(getattr(rec, 'lote_pedido', 0) or 0)
        if sum(pesos) <= 0:
            # Nenhum dia permitido atende a demanda (fornada especial fora
            # de sex/sab; grid comecando no fim de semana): nao produz — o
            # fallback do _distribuir_inteiro despejaria tudo no dia 0, que
            # pode ser um dia bloqueado, exatamente o que a regra proibe.
            liquido = [0] * horizonte_dias
        elif lote > 1 and produzir > 0:
            if lote_prod:
                n_lotes = int(ceil(produzir / lote))
            else:
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
                    # Consolida no dia anterior PERMITIDO (dribble nunca cai
                    # em fim de semana nem em dia proibido da fornada); sem
                    # dia anterior permitido, fica onde esta (i e permitido).
                    j = next((k for k in range(i - 1, -1, -1)
                              if permitido[k]), None)
                    if j is not None:
                        liquido[j] += liquido[i]
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
            # parcelas por dia de DEMANDA (frescor do nivelamento por lote)
            'ref_pesos': ref_pesos,
        })

    # Equilibrar carga POR LOTES (dono 17/08/2026, v2 — SUBSTITUI o
    # "receita inteira num unico dia" de 29/06 pelos dois casos reais da
    # primeira noite: "nao da para produzir tudo isso de brioche, ele vence
    # em 3 dias" e "por que nao redistribuir o croissant em lotes
    # menores?"): parte da CURVA de demanda (que ja rolou fim de semana e
    # consolidou dribble) e move LOTES pra dias ANTERIORES menos
    # carregados, nivelando as fornadas seg-sex. Regras:
    # - nunca move pra DEPOIS (a entrega tem que sair — igual antes);
    # - antecedencia maxima `_ANTECEDENCIA_MAX_DIAS` (frescor: nada e
    #   produzido a mais de N dias da necessidade — brioche nunca mais sai
    #   inteiro na segunda pra semana toda);
    # - lote do movimento = lote_producao > lote_pedido > 1 fornada da
    #   amassadeira (receitas SE DIVIDEM entre dias — 1000 croissants viram
    #   lotes de fornada, nao um dia-monstro);
    # - dia bloqueado da receita (fim de semana, fornada especial fora de
    #   sex/sab) nunca recebe (`producao_permitida_no_dia`).
    if equilibrar:
        n = len(dias_prod)
        itens_eq = []
        for rr in receitas_out:
            if rr['total'] <= 0:
                continue
            rec = receitas.get(rr['receita_id'])
            if rec is None:
                continue
            from app.services.massa_base import rendimento_massa_crua
            rend = rendimento_massa_crua(rec)
            lote_prod = int(getattr(rec, 'lote_producao', 0) or 0)
            lote = lote_prod or int(getattr(rec, 'lote_pedido', 0) or 0)
            cap_g = int(getattr(rec, 'capacidade_amassadeira_g', 0) or 0)
            mb = massa_receita_base(rec) if (cap_g > 0 and rend > 0) else 0
            unid_forn = (cap_g * rend / mb) if mb > 0 else 0.0
            chunk = int(lote or (round(unid_forn) if unid_forn >= 1 else 0)
                        or max(1, ceil(rr['total'] / n)))
            # Peso de nivelamento em "fornadas": 1 unidade vale 1/fornada
            # (sem amassadeira, 1/rend) — nivela o trabalho, nao a unidade
            # (levain em gramas nao pode dominar croissant em pecas).
            peso = (1.0 / unid_forn) if unid_forn >= 1 else (
                1.0 / max(1.0, rend))
            # Celula i vira SEGMENTOS [ref, qtd]: a quantidade inteira e
            # repartida pelas parcelas de demanda (ref_pesos) por proporcao
            # (sobra de arredondamento fica no MAIOR ref — conservador: a
            # parcela menos movel). Sem ref_pesos (linha injetada), a
            # celula inteira referencia o proprio dia.
            qtds = [c['qtd'] for c in rr['por_dia']]
            rp = rr.get('ref_pesos') or []
            segs = []
            for i in range(n):
                q = int(qtds[i])
                pares = sorted(p for p in (rp[i] if i < len(rp) else [])
                               if p[1] > 0)
                if q <= 0:
                    segs.append([])
                    continue
                if not pares:
                    segs.append([[i, q]])
                    continue
                tot_w = sum(w for _, w in pares)
                fatias, resto = [], q
                for ref, w in pares:
                    qi = int(q * w / tot_w)
                    fatias.append([int(ref), qi])
                    resto -= qi
                fatias[-1][1] += resto           # sobra no maior ref
                segs.append([f for f in fatias if f[1] > 0])
            itens_eq.append({'rr': rr, 'rec': rec, 'rend': rend,
                             'qtds': qtds, 'segs': segs,
                             'chunk': max(1, chunk), 'peso': peso})
        if itens_eq:
            carga = [0.0] * n
            for it in itens_eq:
                for i, q in enumerate(it['qtds']):
                    carga[i] += q * it['peso']
            # Alvo pelos dias que PODEM produzir (seg-sex no grid): dividir
            # pelos 7 (com sab/dom bloqueados) subestimava o alvo e o
            # nivelador parava cedo — croissant fatiava, sourdough nao.
            dias_uteis = sum(1 for p in dias_prod if p.weekday() < 5) or n
            alvo = (sum(carga) / dias_uteis) if dias_uteis else 0.0

            def _movel(it, s, d):
                """Qtd da celula s movel pra d: parcelas cujo dia de DEMANDA
                esta a no maximo _ANTECEDENCIA_MAX_DIAS de d (frescor por
                PARCELA — a de sexta anda, a de domingo rolada nao)."""
                return sum(q for ref, q in it['segs'][s]
                           if ref - d <= _ANTECEDENCIA_MAX_DIAS)

            for d in range(n):
                while carga[d] < alvo:
                    # Fonte: o dia MAIS carregado com parcela movel pra d.
                    melhor = None
                    for it in itens_eq:
                        if not producao_permitida_no_dia(it['rec'],
                                                         dias_prod[d]):
                            continue
                        for s in range(d + 1, n):
                            if it['qtds'][s] <= 0 or _movel(it, s, d) <= 0:
                                continue
                            if melhor is None or carga[s] > carga[melhor[1]]:
                                melhor = (it, s)
                    if melhor is None:
                        break
                    it, s = melhor
                    mv = min(_movel(it, s, d), it['chunk'])
                    # So move se MELHORA o balanco (d pos-movimento nao
                    # passa do que a fonte tinha); dia vazio aceita ao
                    # menos um lote.
                    if carga[d] > 0 and carga[d] + mv * it['peso'] > \
                            carga[s]:
                        break
                    # Consome as parcelas MOVEIS de menor ref primeiro
                    # (deixa as menos moveis onde estao).
                    falta_mv = mv
                    for f in sorted(it['segs'][s]):
                        if falta_mv <= 0:
                            break
                        ref, q = f
                        if ref - d > _ANTECEDENCIA_MAX_DIAS or q <= 0:
                            continue
                        tira = min(q, falta_mv)
                        f[1] -= tira
                        falta_mv -= tira
                        it['segs'][d].append([ref, tira])
                    it['segs'][s] = [f for f in it['segs'][s] if f[1] > 0]
                    it['qtds'][s] -= mv
                    it['qtds'][d] += mv
                    carga[s] -= mv * it['peso']
                    carga[d] += mv * it['peso']
            for it in itens_eq:   # reescreve a linha com os lotes movidos
                rend = it['rend']
                for i, c in enumerate(it['rr']['por_dia']):
                    q = int(it['qtds'][i])
                    c['qtd'] = q
                    c['fornadas'] = (fornadas_amassadeira(
                        it['rec'], max(1, ceil(q / rend)))
                        if q > 0 and rend > 0 else None)
                it['rr']['total'] = sum(int(x) for x in it['qtds'])

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
    # Marca linhas de RETORNO em qualquer caminho de injecao (balanco,
    # override legado via extra_rids, insumo do _explodir_bom): a tela trava
    # a celula e mostra a tag — retorno nao se produz (dono, 13/07/2026).
    retorno_ids_cr = {r for (r,) in db.session.query(Receita.retorno_receita_id)
                      .filter(Receita.retorno_receita_id.isnot(None)).distinct()}
    for rr in receitas_out:
        rid = rr['receita_id']
        if rid in retorno_ids_cr:
            rr['retorno'] = True
        rec = receitas.get(rid)
        rr['categoria'] = (rec.categoria or '').strip() if rec else ''
        # Flag da ficha visivel na linha (tag na tela + sonda do assistente):
        # cobre tambem a linha de insumo injetada pelo _explodir_bom.
        if rec is not None and getattr(rec, 'estoque_nao_abate', False):
            rr['estoque_nao_abate'] = True
        # Marca as células de dia SEM produção permitida pra tela travar a
        # edição — fornada especial fora de sex/sáb E (desde 17/08/2026)
        # fim de semana pra TODA receita. Vale também pras linhas injetadas
        # depois da distribuição (zeradas/override/insumo).
        if rec is not None:
            if getattr(rec, 'fornada_especial', False):
                rr['fornada_especial'] = True
            for c, p in zip(rr['por_dia'], dias_prod):
                if not producao_permitida_no_dia(rec, p):
                    c['bloqueado'] = True
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
        # Flag "estoque nao abate" (19/07/2026): saldo/produzir/projecao da
        # linha usam o MESMO numero da conta do balanco (so a producao ja
        # mandada, WIP) — manter o fisico aqui faria a caixa dizer "nao
        # falta" com a linha produzindo (a classe de bug de 30/06) e
        # deixaria estoque fantasma CALAR o alerta de entrega em risco.
        # O fisico real segue visivel em rr['em_estoque'].
        if rr.get('estoque_nao_abate'):
            est_ef = int(it.get('em_producao', 0) or 0) if it else 0
        # Demanda do balanco = Σ_dia max(firme_d, previsto_d) (Fase 2). Linha
        # fora do balanco (insumo injetado) cai no agregado antigo.
        demanda = int(it['demanda']) if it and it.get('demanda') is not None \
            else max(comp, prev)
        rr['comprometido'] = comp
        rr['previsto'] = prev        # a PREVISAO (historico) que tambem puxa producao
        rr['demanda'] = demanda      # Σ_dia max(firme, previsto) — o que o balanco usa
        rr['em_estoque_efetivo'] = est_ef   # estoque que sobra apos entregas iminentes
        # Saldo contra a DEMANDA e o estoque EFETIVO: bate com o "Produzir" da
        # linha (-saldo == produzir quando negativo). Antes era estoque -
        # comprometido (so firme), ignorando o previsto -> a caixa dizia "nao
        # falta" enquanto a linha mandava produzir (bug pego pelo dono 30/06).
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
        # Flag "estoque nao abate": a projecao parte do numero de
        # planejamento (WIP), nunca do fisico do ledger.
        running = est_ef if rr.get('estoque_nao_abate') else int(rr['em_estoque'])
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
        # Alerta "pedido programado sem produto" (dono, 02/07): projecao
        # SO-FIRME — estoque + producao programada vs as entregas FIRMES
        # datadas. Se o saldo firme fica negativo num dia COM entrega, aquela
        # entrega nao tem produto nem produzindo como programado (lead tarde
        # demais, celula editada pra baixo, estoque comido por entrega
        # anterior). Separada da projecao da tela (que desconta max(firme,
        # previsto)) de proposito: PREVISAO de historico nao pode acusar
        # entrega em risco — o alerta e sobre pedido real. Como firme <=
        # max(firme, previsto), todo alerta aqui tambem aparece como falta na
        # projecao detalhada (subconjunto, nunca contradiz a tela).
        running_f = est_ef if rr.get('estoque_nao_abate') else int(rr['em_estoque'])
        entregas_risco = []
        for i, d in enumerate(dias_prod):
            prod_i = int(rr['por_dia'][i - L]['qtd'] or 0) if i - L >= 0 else 0
            firme_i = int(firme[rid].get(d, 0))
            running_f += prod_i - firme_i
            if running_f < 0 and firme_i > 0:
                entregas_risco.append({
                    'data': dias_out[i]['data'], 'label': dias_out[i]['label'],
                    'firme': firme_i,
                    'faltam': min(firme_i, -running_f)})
        rr['entregas_risco'] = entregas_risco
        rr['risco_datas'] = [e['data'] for e in entregas_risco]
        # Edicao manual que NAO cobre entrega firme em risco vira aviso
        # IMEDIATO (dono, 10/07/2026) — sem esperar o "edicao de dia
        # anterior" do E3 (caso real: linha fixada em 600 com pedido de
        # 1600 no sabado so ganhava o 🚨, nao o ⚠️ na edicao). So quando o
        # calculo sugere OUTRO total — se o sugerido e igual ao fixado, o
        # reset nao ajudaria e o aviso mentiria.
        if (rr.get('editado') and entregas_risco
                and not rr.get('override_stale')
                and rr.get('override_sugerido') is not None
                and rr.get('override_sugerido') != rr.get('total')):
            rr['override_stale'] = True

    # Agrupa os produtos por CATEGORIA (depois por nome) — senao ficam espalhados
    # pela ordem de urgencia/demanda do balanco. Categoria vazia vai por ultimo.
    receitas_out.sort(key=lambda rr: (rr['categoria'] == '',
                                      rr['categoria'].lower(),
                                      (rr['nome'] or '').lower()))

    # Agregado pro banner de alerta da tela: so receitas com alguma entrega
    # firme descoberta no horizonte.
    alertas_falta = [{'receita_id': rr['receita_id'], 'nome': rr['nome'],
                      'entregas': rr['entregas_risco']}
                     for rr in receitas_out if rr['entregas_risco']]

    return {
        'dias': dias_out,
        'receitas': receitas_out,
        'hoje': hoje_d.isoformat(),
        'inicio': inicio_d.isoformat(),
        'inicio_offset_dias': inicio_offset_dias,
        'horizonte_dias': horizonte_dias,
        'janela_semanas': janela_semanas,
        'alertas_falta': alertas_falta,
        'motor': motor,
    }


def decompor_previsao(receita_id, horizonte_dias=7, janela_semanas=6,
                      inicio_offset_dias=0, motor='pedidos'):
    """Decompoe o `previsto` de UMA receita pra responder 'de qual dia/loja vem
    esse numero?'. Pra cada dia do horizonte mostra a entrega-alvo (dia + lead),
    o pedido FIRME por loja e a PREVISAO do historico decomposta por loja —
    os registros recentes daquele dia-da-semana (data, loja, qtd) e a media
    recencia-ponderada de cada loja. Read-only, diagnostico. Usa EXATAMENTE a
    mesma conta do cronograma (`_media_recencia`, dia-da-semana, fallback).

    `motor` segue o cronograma: 'pedidos' decompoe o historico de PEDIDOS;
    'vendas' decompoe a VENDA real (+ merma); 'maior' calcula os dois e cada
    dia mostra o que VENCEU (campo `origem`)."""
    horizonte_dias = max(1, min(int(horizonte_dias or 7), 14))
    janela_semanas = max(1, min(int(janela_semanas or 6), 26))
    inicio_offset_dias = max(0, min(int(inicio_offset_dias or 0), 14))
    if motor not in MOTORES_PREVISAO_PRODUCAO:
        motor = 'pedidos'

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

    # Fontes do historico, na MESMA forma: agg = dow->data->qtd;
    # loja = dow->loja->data->qtd. 'maior' carrega as duas.
    def _fonte_pedidos():
        por_data_agg = defaultdict(lambda: defaultdict(int))
        por_loja = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
        soma = 0
        for loja_id, data_ent, qtd in (db.session.query(
                PedidoLoja.loja_id, PedidoLoja.data_entrega,
                PedidoItem.quantidade)
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
            soma += q
        return {'agg': por_data_agg, 'loja': por_loja, 'soma': soma,
                'residual': _taxa_residual(por_data_agg, soma,
                                           dias_calendario_janela)}

    def _fonte_vendas():
        qtd_dow_v, soma_v, _datas_v, por_loja_v = _hist_vendas_receita_por_dow(
            hist_ini, hist_fim, com_loja=True)
        agg = qtd_dow_v.get(rec.id, {})
        soma = soma_v.get(rec.id, 0)
        return {'agg': agg, 'loja': por_loja_v.get(rec.id, {}), 'soma': soma,
                'residual': _taxa_residual(agg, soma, dias_calendario_janela)}

    fontes = {}
    if motor in ('pedidos', 'maior'):
        fontes['pedidos'] = _fonte_pedidos()
    if motor in ('vendas', 'maior'):
        fontes['vendas'] = _fonte_vendas()

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

    def _calc_previsto(fonte, dow, entrega):
        """MESMA conta do cronograma, sobre uma fonte (pedidos ou vendas)."""
        por_data = fonte['agg'].get(dow) or {}
        if not _fornada_no_dia(rec, entrega):
            return 0.0, 'fora_fornada'
        if por_data and len(por_data) >= _MIN_OCORRENCIAS_DOW:
            return _media_recencia(
                por_data, hoje_d,
                datas_possiveis=datas_possiveis_dow[dow]), 'media_dow'
        if fonte['residual'] > 0:
            return fonte['residual'], 'media_diaria'
        if fonte['soma']:
            return 0.0, 'sem_dow'
        return 0.0, 'sem_historico'

    dias = []
    total_previsto_frac = 0.0
    total_firme = 0
    for i in range(horizonte_dias):
        prod_d = inicio_d + timedelta(days=i)
        entrega = prod_d + timedelta(days=L)
        dow = entrega.weekday()
        # Um candidato por fonte; no 'maior' vale o que vencer no dia.
        candidatos = [(_calc_previsto(f, dow, entrega), nome_m, f)
                      for nome_m, f in fontes.items()]
        (previsto, fonte), origem, f_vence = max(
            candidatos, key=lambda c: c[0][0])

        # Decomposicao por loja (media recencia-ponderada de cada loja no dow)
        # — da fonte VENCEDORA do dia.
        previsto_lojas = []
        if fonte == 'media_dow':
            for loja_id, datas in f_vence['loja'].get(dow, {}).items():
                m = _media_recencia(
                    datas, hoje_d, datas_possiveis=datas_possiveis_dow[dow])
                if round(m) > 0:
                    previsto_lojas.append({'loja_nome': nomes_loja.get(loja_id, '?'),
                                           'media': int(round(m)),
                                           'n': len(datas)})
            previsto_lojas.sort(key=lambda x: -x['media'])

        # Registros crus do dow (os 12 mais recentes) — a prova do numero.
        historico = []
        for loja_id, datas in f_vence['loja'].get(dow, {}).items():
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
            'origem': origem,     # fonte que venceu o dia (motor 'maior')
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
        'dias': dias, 'motor': motor,
    }
