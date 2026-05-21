"""Deteccao de anomalias diarias.

Roda 1x por dia (23:00 BRT, apos fechamento de loja). Compara metricas
do dia com baseline historico e envia digest WhatsApp via Z-API se algo
foge do padrao. Numero destino: o mesmo do digest de tarefas
(ZAPI_NUMERO_DESTINO).

Se nao houver nenhuma anomalia, NAO envia mensagem (evita spam).

Detectores:
- Vendas atipicas por loja: hoje vs media do mesmo dia-da-semana nas
  ultimas 4 semanas. Alerta se desvio >= 30% e baseline >= 30 unidades.
- Itens com queda forte: vendas dos ultimos 7 dias vs 7 dias anteriores.
  Top 5 maiores quedas com queda >= 30% e baseline >= 20 unidades.
- Estoque parado: receita com EstoqueLoja > 7 dias de venda media.
- Pedidos travados: data_entrega ja passou e status nao terminal.
"""
import logging
from collections import defaultdict
from datetime import datetime, timedelta

from flask import current_app

from app.constants import STATUS_PEDIDO_FINALIZADOS, VENDA_TIPOS_LOJA
from app.utils import hoje as hoje_brt

logger = logging.getLogger(__name__)

# Thresholds
DESVIO_LOJA_PCT = 0.30
MIN_VENDAS_BASE_LOJA = 30
DESVIO_ITEM_PCT = 0.30
MIN_VENDAS_ITEM = 20
TOP_N_QUEDAS = 5
ESTOQUE_DIAS_PARADO = 7
MIN_QTD_ESTOQUE_PARADO = 5
TOP_N_ESTOQUE_PARADO = 10
TOP_N_PEDIDOS_TRAVADOS = 10


# ── Agregadores de vendas ─────────────────────────────────────────────

def _vendas_loja_no_dia(data_alvo):
    """Retorna dict {loja_id: qtd_total} pra `data_alvo`."""
    from app.extensions import db
    from app.models import EstoqueLoja, MovEstoqueLoja

    ini = datetime.combine(data_alvo, datetime.min.time())
    fim = ini + timedelta(days=1)

    rows = (db.session.query(EstoqueLoja.loja_id,
                             db.func.coalesce(db.func.sum(MovEstoqueLoja.quantidade), 0))
            .join(MovEstoqueLoja,
                  MovEstoqueLoja.estoque_loja_id == EstoqueLoja.id)
            .filter(MovEstoqueLoja.tipo.in_(VENDA_TIPOS_LOJA),
                    MovEstoqueLoja.data >= ini,
                    MovEstoqueLoja.data < fim)
            .group_by(EstoqueLoja.loja_id)
            .all())
    return {loja_id: int(qtd) for loja_id, qtd in rows}


def _vendas_item_periodo(data_ini, data_fim):
    """Retorna dict {(loja_id, nome_item): qtd} no intervalo [data_ini, data_fim).

    `nome_item` usa o helper do EstoqueLoja (resolve receita/produto/MP/pendente).
    """
    from app.extensions import db
    from app.models import EstoqueLoja, MovEstoqueLoja

    ini = datetime.combine(data_ini, datetime.min.time())
    fim = datetime.combine(data_fim, datetime.min.time())

    rows = (db.session.query(EstoqueLoja,
                             db.func.coalesce(db.func.sum(MovEstoqueLoja.quantidade), 0))
            .join(MovEstoqueLoja,
                  MovEstoqueLoja.estoque_loja_id == EstoqueLoja.id)
            .filter(MovEstoqueLoja.tipo.in_(VENDA_TIPOS_LOJA),
                    MovEstoqueLoja.data >= ini,
                    MovEstoqueLoja.data < fim)
            .group_by(EstoqueLoja.id)
            .all())
    out = defaultdict(int)
    for el, qtd in rows:
        out[(el.loja_id, el.nome_item)] += int(qtd)
    return dict(out)


# ── Detector 1: Vendas atipicas por loja ───────────────────────────────

def detectar_anomalias_loja(data_alvo=None):
    """Compara vendas de `data_alvo` (default hoje) com media das ultimas
    4 ocorrencias do mesmo dia-da-semana.

    Retorna lista [{loja_id, loja_nome, qtd_hoje, baseline, desvio_pct,
    direcao}] ordenada por |desvio_pct| desc.
    """
    from app.models import Loja

    if data_alvo is None:
        data_alvo = hoje_brt()

    qtd_hoje = _vendas_loja_no_dia(data_alvo)

    # Baseline: D-7, D-14, D-21, D-28
    baselines = defaultdict(list)
    for k in (1, 2, 3, 4):
        d = data_alvo - timedelta(days=7 * k)
        for loja_id, qtd in _vendas_loja_no_dia(d).items():
            baselines[loja_id].append(qtd)

    lojas = {l.id: l for l in Loja.query.filter(
        Loja.ativa.is_(True), Loja.nome != 'Industria').all()}

    anomalias = []
    for loja_id, amostras in baselines.items():
        if len(amostras) < 2:
            continue  # pouca historia, pula
        media = sum(amostras) / len(amostras)
        if media < MIN_VENDAS_BASE_LOJA:
            continue
        atual = qtd_hoje.get(loja_id, 0)
        desvio = (atual - media) / media
        if abs(desvio) < DESVIO_LOJA_PCT:
            continue
        loja = lojas.get(loja_id)
        if not loja:
            continue
        anomalias.append({
            'loja_id': loja_id,
            'loja_nome': loja.nome,
            'qtd_hoje': atual,
            'baseline': round(media, 1),
            'desvio_pct': desvio,
            'direcao': 'queda' if desvio < 0 else 'pico',
        })
    anomalias.sort(key=lambda a: -abs(a['desvio_pct']))
    return anomalias


# ── Detector 2: Itens com queda forte ──────────────────────────────────

def detectar_quedas_item(data_alvo=None):
    """Compara vendas dos ultimos 7 dias com os 7 anteriores.

    Janelas:
    - Atual: [data_alvo - 6, data_alvo] inclusivo = 7 dias
    - Anterior: [data_alvo - 13, data_alvo - 7] inclusivo = 7 dias

    Top N quedas absolutas com queda >= 30% e baseline >= 20 unidades.
    Retorna lista [{loja_id, loja_nome, nome_item, qtd_atual,
    qtd_anterior, queda_abs, queda_pct}].
    """
    from app.models import Loja

    if data_alvo is None:
        data_alvo = hoje_brt()

    fim_atual = data_alvo + timedelta(days=1)
    ini_atual = data_alvo - timedelta(days=6)
    fim_ant = ini_atual
    ini_ant = ini_atual - timedelta(days=7)

    atual = _vendas_item_periodo(ini_atual, fim_atual)
    anterior = _vendas_item_periodo(ini_ant, fim_ant)

    lojas = {l.id: l for l in Loja.query.all()}

    quedas = []
    for chave, qtd_ant in anterior.items():
        if qtd_ant < MIN_VENDAS_ITEM:
            continue
        qtd_at = atual.get(chave, 0)
        if qtd_at >= qtd_ant:
            continue  # nao caiu
        queda_pct = (qtd_at - qtd_ant) / qtd_ant
        if abs(queda_pct) < DESVIO_ITEM_PCT:
            continue
        loja_id, nome_item = chave
        loja = lojas.get(loja_id)
        if not loja or loja.nome == 'Industria':
            continue
        quedas.append({
            'loja_id': loja_id,
            'loja_nome': loja.nome,
            'nome_item': nome_item,
            'qtd_atual': qtd_at,
            'qtd_anterior': qtd_ant,
            'queda_abs': qtd_ant - qtd_at,
            'queda_pct': queda_pct,
        })
    quedas.sort(key=lambda q: -q['queda_abs'])
    return quedas[:TOP_N_QUEDAS]


# ── Detector 3: Estoque parado ─────────────────────────────────────────

def detectar_estoque_parado(data_alvo=None):
    """EstoqueLoja com quantidade > N dias de venda media.

    Venda media calculada nos ultimos 14 dias. Item nunca vendido nesse
    periodo eh ignorado (catalogo morto, nao 'parou').

    Retorna top N items por dias_de_estoque desc.
    """
    from app.models import EstoqueLoja, Loja

    if data_alvo is None:
        data_alvo = hoje_brt()

    fim = data_alvo + timedelta(days=1)
    ini = data_alvo - timedelta(days=13)
    vendas = _vendas_item_periodo(ini, fim)
    # Agrega vendas por (loja_id, nome_item) → dia
    dias = (fim - ini).days  # = 14
    venda_media = {chave: qtd / dias for chave, qtd in vendas.items()}

    lojas = {l.id: l for l in Loja.query.all()}

    parados = []
    estoques = (EstoqueLoja.query
                .filter(EstoqueLoja.quantidade >= MIN_QTD_ESTOQUE_PARADO)
                .all())
    for el in estoques:
        loja = lojas.get(el.loja_id)
        if not loja or loja.nome == 'Industria':
            continue
        nome = el.nome_item
        media = venda_media.get((el.loja_id, nome), 0)
        if media <= 0:
            continue  # nunca vende, nao alarmamos
        dias_est = el.quantidade / media
        if dias_est <= ESTOQUE_DIAS_PARADO:
            continue
        parados.append({
            'loja_id': el.loja_id,
            'loja_nome': loja.nome,
            'nome_item': nome,
            'quantidade': el.quantidade,
            'venda_dia': round(media, 1),
            'dias_estoque': round(dias_est, 1),
        })
    parados.sort(key=lambda p: -p['dias_estoque'])
    return parados[:TOP_N_ESTOQUE_PARADO]


# ── Detector 4: Pedidos travados ───────────────────────────────────────

def detectar_pedidos_travados(data_alvo=None):
    """PedidoLoja com data_entrega < data_alvo e status nao terminal."""
    from app.models import PedidoLoja

    if data_alvo is None:
        data_alvo = hoje_brt()

    pedidos = (PedidoLoja.query
               .filter(PedidoLoja.data_entrega < data_alvo)
               .filter(~PedidoLoja.status.in_(STATUS_PEDIDO_FINALIZADOS))
               .order_by(PedidoLoja.data_entrega.asc())
               .limit(TOP_N_PEDIDOS_TRAVADOS)
               .all())
    out = []
    for p in pedidos:
        out.append({
            'pedido_id': p.id,
            'loja_nome': p.loja.nome if p.loja else '?',
            'status': p.status,
            'data_entrega': p.data_entrega,
            'dias_atras': (data_alvo - p.data_entrega).days,
        })
    return out


# ── Resumo + formatacao ────────────────────────────────────────────────

def gerar_resumo(data_alvo=None):
    """Roda os 4 detectores e devolve dict com tudo."""
    if data_alvo is None:
        data_alvo = hoje_brt()
    return {
        'data': data_alvo,
        'lojas': detectar_anomalias_loja(data_alvo),
        'quedas': detectar_quedas_item(data_alvo),
        'estoque_parado': detectar_estoque_parado(data_alvo),
        'pedidos_travados': detectar_pedidos_travados(data_alvo),
    }


def tem_anomalias(resumo):
    return bool(resumo['lojas'] or resumo['quedas']
                or resumo['estoque_parado'] or resumo['pedidos_travados'])


def _fmt_pct(p):
    sinal = '+' if p >= 0 else ''
    return f'{sinal}{round(p * 100)}%'


def montar_texto_whatsapp(resumo):
    """Texto pronto pro WhatsApp (markdown leve: *bold*, _italic_)."""
    data = resumo['data']
    linhas = [f'*Alertas do dia {data.strftime("%d/%m/%Y")}*']

    # Vendas por loja
    if resumo['lojas']:
        linhas.append('')
        linhas.append(f'*Vendas atipicas por loja ({len(resumo["lojas"])})*')
        for a in resumo['lojas']:
            seta = '↓' if a['direcao'] == 'queda' else '↑'
            linhas.append(
                f'{seta} *{a["loja_nome"]}*: {a["qtd_hoje"]} un. '
                f'(esperado ~{a["baseline"]}, {_fmt_pct(a["desvio_pct"])})'
            )

    # Quedas de item
    if resumo['quedas']:
        linhas.append('')
        linhas.append('*Itens em queda forte (7d vs 7d)*')
        for q in resumo['quedas']:
            linhas.append(
                f'• *{q["nome_item"]}* em _{q["loja_nome"]}_: '
                f'{q["qtd_atual"]} vs {q["qtd_anterior"]} '
                f'({_fmt_pct(q["queda_pct"])}, -{q["queda_abs"]} un.)'
            )

    # Estoque parado
    if resumo['estoque_parado']:
        linhas.append('')
        linhas.append(f'*Estoque parado (>{ESTOQUE_DIAS_PARADO}d de cobertura)*')
        for e in resumo['estoque_parado']:
            linhas.append(
                f'• *{e["nome_item"]}* em _{e["loja_nome"]}_: '
                f'{e["quantidade"]} un., vende {e["venda_dia"]}/dia '
                f'(~{e["dias_estoque"]}d de estoque)'
            )

    # Pedidos travados
    if resumo['pedidos_travados']:
        linhas.append('')
        linhas.append('*Pedidos travados (entrega passou)*')
        for p in resumo['pedidos_travados']:
            linhas.append(
                f'• Pedido *#{p["pedido_id"]}* {p["loja_nome"]} '
                f'({p["status"]}) — entrega {p["data_entrega"].strftime("%d/%m")} '
                f'({p["dias_atras"]}d atras)'
            )

    return '\n'.join(linhas)


def enviar_digest_whatsapp():
    """Job: gera resumo e envia pro ZAPI_NUMERO_DESTINO se houver anomalias."""
    from app.services import zapi

    numero = (current_app.config.get('ZAPI_NUMERO_DESTINO') or '').strip()
    if not numero:
        logger.info('anomalias: ZAPI_NUMERO_DESTINO nao configurado, pulando')
        return
    if not zapi.disponivel():
        logger.info('anomalias: Z-API nao configurado, pulando')
        return

    resumo = gerar_resumo()
    if not tem_anomalias(resumo):
        logger.info('anomalias: nenhuma anomalia detectada, nada a enviar')
        return

    texto = montar_texto_whatsapp(resumo)
    res = zapi.enviar_texto(numero, texto)
    if res.get('ok'):
        n_total = (len(resumo['lojas']) + len(resumo['quedas'])
                   + len(resumo['estoque_parado']) + len(resumo['pedidos_travados']))
        logger.info('anomalias: digest enviado pra %s (%d itens)', numero, n_total)
    else:
        logger.warning('anomalias: falha ao enviar: %s', res.get('erro'))
