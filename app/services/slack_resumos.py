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

    # So pedidos ainda nao processados pela industria — pendente ou confirmado.
    # Separado/em_transporte/recebido/cancelado = nao precisa lembrar a industria.
    pedidos = (PedidoLoja.query
               .filter(PedidoLoja.data_entrega == data)
               .filter(PedidoLoja.status.in_(['pendente', 'confirmado']))
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


def lojas_sem_pedido_amanha():
    """Retorna lista de Loja operacional que NAO tem pedido pra amanha
    (data_entrega = hoje + 1) e NAO foi marcada como opt-out."""
    from datetime import timedelta
    from app.models import Loja, PedidoLoja, LembretePedidoOptOut

    amanha = hoje_brt() + timedelta(days=1)

    # Pedidos pra amanha (qualquer status menos cancelado)
    pedidos = (PedidoLoja.query
               .filter(PedidoLoja.data_entrega == amanha)
               .filter(PedidoLoja.status != 'cancelado')
               .all())
    lojas_com_pedido = {p.loja_id for p in pedidos}

    # Opt-outs pra amanha
    optouts = LembretePedidoOptOut.query.filter_by(data_entrega=amanha).all()
    lojas_optout = {o.loja_id for o in optouts}

    # Lojas operacionais (ativa, nome != 'Industria')
    lojas = (Loja.query
             .filter(Loja.ativa.is_(True))
             .filter(Loja.nome != 'Industria')
             .order_by(Loja.nome)
             .all())

    return [(l, amanha) for l in lojas
            if l.id not in lojas_com_pedido and l.id not in lojas_optout]


def enviar_lembretes_pedido_amanha():
    """Posta no canal #producao um lembrete por loja que nao fez pedido
    pra amanha. Cada lembrete tem 2 botoes (sem pedido / fazer pedido).
    """
    from app.services import slack as slack_api

    canal = (current_app.config.get('SLACK_CANAL_PEDIDOS') or '').strip()
    if not canal:
        logger.info('slack_resumos: SLACK_CANAL_PEDIDOS nao configurado, pulando lembretes')
        return
    if not slack_api.disponivel():
        return

    pendentes = lojas_sem_pedido_amanha()
    if not pendentes:
        logger.info('slack_resumos: todas as lojas tem pedido (ou opt-out) pra amanha')
        return

    base_url = (current_app.config.get('PUBLIC_BASE_URL')
                or 'https://gestao.opaopadariaartesanal.com.br').rstrip('/')

    for loja, data in pendentes:
        # value codifica loja_id:YYYY-MM-DD pra os botoes
        valor = f'{loja.id}:{data.isoformat()}'
        url_pedido = f'{base_url}/pedidos/novo?loja={loja.id}'
        blocks = [
            {'type': 'header',
             'text': {'type': 'plain_text',
                      'text': f'⚠️ {loja.nome} sem pedido pra {data.strftime("%d/%m")}'}},
            {'type': 'section',
             'text': {'type': 'mrkdwn',
                      'text': (f'A *{loja.nome}* ainda nao fez pedido pra entrega '
                                f'em *{data.strftime("%d/%m/%Y")}*.\n'
                                'Avisa aqui se nao vai ter pedido, ou cria um agora.')}},
            {'type': 'actions',
             'elements': [
                 {'type': 'button', 'style': 'danger',
                  'text': {'type': 'plain_text', 'text': 'Nao vai ter pedido'},
                  'action_id': 'lembrete_no_pedido', 'value': valor},
                 {'type': 'button', 'style': 'primary',
                  'text': {'type': 'plain_text', 'text': 'Fazer pedido'},
                  'action_id': 'lembrete_fazer_pedido',
                  'url': url_pedido, 'value': valor},
             ]},
        ]
        slack_api.post_message(canal,
                                text=f'{loja.nome} sem pedido pra {data.strftime("%d/%m")}',
                                blocks=blocks)


def pedidos_hoje_pendentes(data=None):
    """Retorna lista de PedidoLoja com data_entrega = hoje (ou `data`) e
    status diferente de 'entregue', 'recebido' e 'cancelado'.

    Status 'entregue' e 'recebido' existem por historico (site usava
    'entregue', copilot recente usa 'recebido') — filtramos ambos.

    Retorna dict {loja_id: {'loja': Loja, 'pedidos': [PedidoLoja, ...]}}.
    """
    from app.models import PedidoLoja
    if data is None:
        data = hoje_brt()
    pedidos = (PedidoLoja.query
               .filter(PedidoLoja.data_entrega == data)
               .filter(~PedidoLoja.status.in_(['entregue', 'recebido', 'cancelado']))
               .order_by(PedidoLoja.criado_em)
               .all())
    por_loja = {}
    for p in pedidos:
        if not p.loja_id:
            continue
        por_loja.setdefault(p.loja_id, {'loja': p.loja, 'pedidos': []})['pedidos'].append(p)
    return por_loja


def _formatar_pedido_section(p):
    """Section de um pedido (header + itens) + botao acao."""
    itens_txt = '\n'.join(
        f'  • {it.quantidade}x {it.nome_item if hasattr(it, "nome_item") else _nome_item(it)}'
        for it in p.itens
    ) or '  _(sem itens)_'
    criado = (p.criado_em.strftime('%d/%m %H:%M') if p.criado_em else '?')
    texto = (f'*Pedido #{p.id}* (status: _{p.status}_)\n'
             f'{itens_txt}\n'
             f'_Criado: {criado}_')
    return [
        {'type': 'section', 'text': {'type': 'mrkdwn', 'text': texto[:2900]}},
        {'type': 'actions', 'elements': [
            {'type': 'button', 'style': 'primary',
             'text': {'type': 'plain_text', 'text': f'Marcar #{p.id} como recebido'},
             'action_id': 'lembrete_receber_pedido',
             'value': str(p.id)},
        ]},
    ]


def enviar_lembrete_pedidos_hoje_pendentes():
    """Posta no canal SLACK_CANAL_PEDIDOS uma mensagem POR LOJA com os
    pedidos de hoje ainda nao recebidos. Cada pedido tem botao de marcar
    como recebido. Roda de hora em hora das 10h as 19h.
    """
    from app.services import slack as slack_api

    canal = (current_app.config.get('SLACK_CANAL_PEDIDOS') or '').strip()
    if not canal:
        logger.info('slack_resumos: SLACK_CANAL_PEDIDOS nao configurado, pulando hoje-pendentes')
        return
    if not slack_api.disponivel():
        return

    hoje = hoje_brt()
    por_loja = pedidos_hoje_pendentes(hoje)
    if not por_loja:
        logger.info('slack_resumos: nenhum pedido pendente pra hoje, nada a postar')
        return

    for loja_id, info in por_loja.items():
        loja = info['loja']
        pedidos = info['pedidos']
        loja_nome = loja.nome if loja else f'id={loja_id}'

        n = len(pedidos)
        header_txt = (f':warning: *{loja_nome}* — {n} pedido{"" if n == 1 else "s"} '
                       f'a entregar hoje ({hoje.strftime("%d/%m")})')

        blocks = [
            {'type': 'section', 'text': {'type': 'mrkdwn', 'text': header_txt}},
            {'type': 'divider'},
        ]
        for p in pedidos:
            blocks.extend(_formatar_pedido_section(p))
            blocks.append({'type': 'divider'})
        # Tira o ultimo divider
        if blocks and blocks[-1].get('type') == 'divider':
            blocks.pop()

        slack_api.post_message(
            canal,
            text=f'{loja_nome}: {n} pedido(s) ainda nao entregue(s) hoje',
            blocks=blocks,
        )
    logger.info('slack_resumos: lembrete pedidos hoje postado (%d lojas)', len(por_loja))


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


def alertar_pedido_emergencia(pedido):
    """Posta alerta no Slack quando um pedido eh feito no MESMO dia da entrega.
    Pedido emergencia = `criado_em.date() == data_entrega`. Notifica o canal
    SLACK_CANAL_PEDIDOS para producao saber e correr.
    """
    from app.services import slack as slack_api

    if not pedido or not pedido.data_entrega or not pedido.criado_em:
        return
    if pedido.data_entrega != pedido.criado_em.date():
        return  # nao eh emergencia

    canal = (current_app.config.get('SLACK_CANAL_PEDIDOS') or '').strip()
    if not canal:
        return
    if not slack_api.disponivel():
        return

    base_url = (current_app.config.get('PUBLIC_BASE_URL')
                or 'https://gestao.opaopadariaartesanal.com.br').rstrip('/')

    loja_nome = pedido.loja.nome if pedido.loja else '?'
    n_itens = len(pedido.itens) if pedido.itens else 0
    qtd_total = sum(i.quantidade for i in (pedido.itens or []))
    url_pedido = f'{base_url}/pedidos/{pedido.id}'

    # Lista os primeiros itens pra producao ver o que precisa fazer
    itens_lista = []
    for it in (pedido.itens or [])[:8]:
        nome = (it.receita.nome if it.receita
                else it.produto.nome if it.produto
                else it.materia_prima.nome if it.materia_prima
                else '?')
        itens_lista.append(f'• {it.quantidade}× {nome}')
    if len(pedido.itens or []) > 8:
        itens_lista.append(f'... e mais {len(pedido.itens) - 8} item(ns)')

    blocks = [
        {'type': 'header',
         'text': {'type': 'plain_text',
                  'text': f'🚨 PEDIDO EMERGÊNCIA — {loja_nome}'}},
        {'type': 'section',
         'text': {'type': 'mrkdwn',
                  'text': (f'Pedido *#{pedido.id}* feito *hoje* para entregar *hoje*.\n'
                            f'• Loja: *{loja_nome}*\n'
                            f'• Itens: *{n_itens}* SKUs / *{qtd_total}* unidades total\n'
                            f'• Criado: {pedido.criado_em.strftime("%H:%M")} BRT\n\n'
                            'Não é o fluxo normal — produção precisa correr.')}},
        {'type': 'section',
         'text': {'type': 'mrkdwn',
                  'text': '\n'.join(itens_lista) or '_sem itens_'}},
        {'type': 'actions',
         'elements': [
             {'type': 'button', 'style': 'primary',
              'text': {'type': 'plain_text', 'text': 'Ver pedido'},
              'url': url_pedido},
         ]},
    ]
    try:
        slack_api.post_message(canal,
                                text=f'🚨 Emergência: pedido #{pedido.id} hoje pra {loja_nome}',
                                blocks=blocks)
    except Exception:
        logger.exception('alertar_pedido_emergencia falhou')
