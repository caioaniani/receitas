"""Resumos automaticos via Slack — pedidos a entregar, etc.

Disparado pelo APScheduler (ver seru_cron.py).
"""
import logging
from collections import defaultdict

from flask import current_app

from app.utils import hoje as hoje_brt

logger = logging.getLogger(__name__)


def resumo_pedidos_dia(data=None):
    """Retorna dict com pedidos PedidoLoja com data_entrega = `data` (default hoje BRT)
    em status nao-concluido. Agrupa por loja e por item.
    """
    from app.models import PedidoLoja
    if data is None:
        data = hoje_brt()

    pedidos = (PedidoLoja.query
               .filter(PedidoLoja.data_entrega == data)
               .filter(~PedidoLoja.status.in_(['recebido', 'cancelado']))
               .all())

    por_loja = defaultdict(list)  # loja_nome -> [{item, qtd, pedido_id, status}]
    por_item = defaultdict(int)   # nome -> qtd total
    for p in pedidos:
        nome_loja = p.loja.nome if p.loja else '?'
        for it in p.itens:
            nome_item = it.nome_item if hasattr(it, 'nome_item') else _nome_item(it)
            por_loja[nome_loja].append({
                'item': nome_item, 'qtd': it.quantidade,
                'pedido_id': p.id, 'status': p.status,
            })
            por_item[nome_item] += it.quantidade

    return {
        'data': data,
        'n_pedidos': len(pedidos),
        'por_loja': dict(por_loja),
        'por_item': dict(por_item),
    }


def _nome_item(it):
    if it.receita_id and it.receita:
        return it.receita.nome
    if it.materia_prima_id and it.materia_prima:
        return it.materia_prima.nome + ' (MP)'
    return '?'


def _formatar_blocks(resumo):
    """Constroi blocks Slack pro resumo."""
    data = resumo['data']
    n = resumo['n_pedidos']
    if n == 0:
        return [
            {'type': 'header',
             'text': {'type': 'plain_text',
                      'text': f'Pedidos pra entregar HOJE ({data.strftime("%d/%m")})'}},
            {'type': 'section',
             'text': {'type': 'mrkdwn',
                      'text': '_Nenhum pedido pra entregar hoje. Bom dia._'}},
        ]

    blocks = [
        {'type': 'header',
         'text': {'type': 'plain_text',
                  'text': f'Pedidos pra entregar HOJE ({data.strftime("%d/%m")})'}},
        {'type': 'section',
         'text': {'type': 'mrkdwn',
                  'text': f'*{n} pedido{"s" if n != 1 else ""}* · '
                          f'*{sum(resumo["por_item"].values())} unidades totais*'}},
        {'type': 'divider'},
    ]

    # Total geral pra producao
    if resumo['por_item']:
        linhas = sorted(resumo['por_item'].items(), key=lambda x: -x[1])
        texto = '*ENTREGA TOTAL DO DIA:*\n' + '\n'.join(
            f'• {qtd}x {nome}' for nome, qtd in linhas[:30]
        )
        if len(linhas) > 30:
            texto += f'\n_... +{len(linhas) - 30} itens_'
        blocks.append({'type': 'section',
                       'text': {'type': 'mrkdwn', 'text': texto[:2900]}})
        blocks.append({'type': 'divider'})

    # Por loja
    for nome_loja, itens in sorted(resumo['por_loja'].items()):
        # Agrega por item dentro da loja (caso tenha 2 pedidos com mesmo item)
        agreg = defaultdict(int)
        for it in itens:
            agreg[it['item']] += it['qtd']
        linhas_loja = '\n'.join(f'  • {qtd}x {nome}'
                                 for nome, qtd in sorted(agreg.items()))
        texto = f'*{nome_loja}* ({len(itens)} {"itens" if len(itens) != 1 else "item"})\n{linhas_loja}'
        blocks.append({'type': 'section',
                       'text': {'type': 'mrkdwn', 'text': texto[:2900]}})

    return blocks


def enviar_resumo_pedidos_dia():
    """Job: posta o resumo do dia no canal configurado.

    Idempotente em UM sentido: nao verifica se ja foi enviado hoje (o
    scheduler so chama 1x). Se chamado manualmente 2x, posta 2x.
    """
    from app.services import slack as slack_api

    canal = (current_app.config.get('SLACK_CANAL_RESUMO_DIARIO') or '').strip()
    if not canal:
        logger.info('slack_resumos: SLACK_CANAL_RESUMO_DIARIO nao configurado, pulando')
        return

    if not slack_api.disponivel():
        logger.info('slack_resumos: bot nao configurado, pulando')
        return

    try:
        resumo = resumo_pedidos_dia()
        blocks = _formatar_blocks(resumo)
        res = slack_api.post_message(
            canal,
            text=f'Pedidos pra entregar hoje: {resumo["n_pedidos"]}',
            blocks=blocks,
        )
        if res.get('ok'):
            logger.info('slack_resumos: resumo enviado (%s pedidos)', resumo['n_pedidos'])
        else:
            logger.warning('slack_resumos: falha ao postar: %s', res.get('erro'))
    except Exception:
        logger.exception('slack_resumos: erro gerando/enviando resumo')
