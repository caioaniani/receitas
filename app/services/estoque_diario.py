"""Relatorio diario de estoque por loja.

Le os movimentos JA registrados em MovEstoqueLoja (nao cria nada, nao altera
estoque). Pra cada item da loja monta:
  - estoque no inicio do dia (ontem 23:59)
  - entradas de hoje
  - baixas de hoje
  - estoque atual

E o detalhe das baixas de hoje por fonte (PDV Seru, Site VNDA, manual, etc).

Como o "estoque de ontem" sai: estoque_atual eh o saldo de agora; somando o
que entrou e subtraindo o que saiu hoje (tudo do historico de movimentos),
chega-se ao saldo de ontem. Eh leitura/aritmetica sobre dados existentes.
"""
import logging
from datetime import datetime, time

from app.models import EstoqueLoja, MovEstoqueLoja
from app.utils import hoje as hoje_brt

logger = logging.getLogger(__name__)

# Tipos de MovEstoqueLoja que AUMENTAM o saldo.
TIPOS_ENTRADA = {
    'entrada_pedido', 'entrada_lote', 'entrada_manual', 'balanco_entrada',
    'venda_seru_estorno', 'venda_vnda_estorno', 'desperdicio_estorno',
}
# Tipos que DIMINUEM o saldo.
TIPOS_SAIDA = {
    'venda_seru', 'venda_vnda', 'venda', 'ajuste', 'ajuste_negativo',
    'devolucao', 'descarte', 'desperdicio', 'saida_lote', 'saida_pedido',
}
# Tipos que NAO mexem no saldo (so registram falta).
TIPOS_NEUTRO = {
    'venda_seru_sem_estoque', 'venda_vnda_sem_estoque',
    'venda_loja_sem_estoque', 'desperdicio_sem_estoque',
}
# ajuste_conferencia: a quantidade ja vem com sinal (+ subiu / - desceu).

# Label amigavel da fonte da baixa (pro detalhe).
FONTE_LABEL = {
    'venda_seru': 'PDV (Seru)',
    'venda_vnda': 'Site (VNDA)',
    'venda': 'Venda manual',
    'ajuste': 'Ajuste manual',
    'ajuste_negativo': 'Ajuste manual (saida)',
    'ajuste_conferencia': 'Conferencia (contagem)',
    'devolucao': 'Devolucao',
    'descarte': 'Descarte',
    'desperdicio': 'Desperdicio',
    'saida_lote': 'Saida em lote',
    'saida_pedido': 'Saida p/ pedido',
}


def _delta(tipo, quantidade):
    """Delta com sinal que o movimento aplicou ao saldo."""
    q = quantidade or 0
    if tipo == 'ajuste_conferencia':
        return q  # ja vem com sinal
    if tipo in TIPOS_ENTRADA:
        return abs(q)
    if tipo in TIPOS_SAIDA:
        return -abs(q)
    if tipo in TIPOS_NEUTRO:
        return 0
    logger.warning('[estoque_diario] tipo de movimento desconhecido: %s', tipo)
    return 0


def relatorio_diario(loja_id, dia=None):
    """Retorna lista de itens da loja com saldo inicio/entradas/baixas/atual.

    Cada item: {nome, estoque_loja_id, estoque_inicio, entradas, baixas,
    estoque_atual, baixas_por_fonte: [{tipo, label, quantidade}]}.
    Itens sem movimento hoje e com saldo 0 sao omitidos.
    """
    if dia is None:
        dia = hoje_brt()
    ini_dia = datetime.combine(dia, time.min)
    fim_dia = datetime.combine(dia, time.max)

    itens = (EstoqueLoja.query
             .filter_by(loja_id=loja_id)
             .all())
    por_id = {el.id: el for el in itens}
    if not por_id:
        return []

    # Movimentos do dia, de todos os itens da loja, em 1 query.
    movs = (MovEstoqueLoja.query
            .filter(MovEstoqueLoja.estoque_loja_id.in_(list(por_id.keys())))
            .filter(MovEstoqueLoja.data >= ini_dia)
            .filter(MovEstoqueLoja.data <= fim_dia)
            .all())

    # Agrega por item.
    agg = {}  # estoque_loja_id -> {entradas, baixas, fontes:{tipo:qtd}}
    for m in movs:
        d = _delta(m.tipo, m.quantidade)
        e = agg.setdefault(m.estoque_loja_id,
                           {'entradas': 0, 'baixas': 0, 'fontes': {}})
        if d > 0:
            e['entradas'] += d
        elif d < 0:
            e['baixas'] += -d
            e['fontes'][m.tipo] = e['fontes'].get(m.tipo, 0) + (-d)

    linhas = []
    for el in itens:
        a = agg.get(el.id, {'entradas': 0, 'baixas': 0, 'fontes': {}})
        atual = el.quantidade or 0
        # saldo de ontem = atual revertendo o movimento liquido de hoje
        inicio = atual - (a['entradas'] - a['baixas'])
        # Omite item sem saldo e sem movimento (linha morta).
        if atual == 0 and inicio == 0 and not a['fontes'] and a['entradas'] == 0:
            continue
        baixas_por_fonte = sorted(
            [{'tipo': t, 'label': FONTE_LABEL.get(t, t), 'quantidade': q}
             for t, q in a['fontes'].items()],
            key=lambda x: x['quantidade'], reverse=True)
        linhas.append({
            'estoque_loja_id': el.id,
            'nome': el.nome_item,
            'estado': el.estado,
            'estoque_inicio': inicio,
            'entradas': a['entradas'],
            'baixas': a['baixas'],
            'estoque_atual': atual,
            'baixas_por_fonte': baixas_por_fonte,
        })
    linhas.sort(key=lambda x: (x['nome'] or '').lower())
    return linhas
