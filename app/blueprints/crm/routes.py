"""CRM card: histórico do cliente por telefone, embutido no Chatwoot.

O atendente abre uma conversa no Chatwoot; um Dashboard App (iframe servido
por `/crm/card`) mostra o que esse telefone já comprou da padaria. O Chatwoot
passa o contato pro iframe via `postMessage`; o iframe extrai o telefone e
chama `/crm/card.json`.

Auth: token compartilhado `CHATWOOT_CARD_TOKEN` embutido na URL do Dashboard
App (`?k=...`) — o iframe repassa em cada chamada do JSON. Sem token válido,
o JSON nega. O endpoint nunca ecoa input arbitrário: só devolve dados de
clientes que já existem no banco.

Match de telefone via `app.utils.telefone_chave` (canônico BR: ignora +55 e
o 9º dígito de celular), porque o WhatsApp manda '5511999998888' mas o
cadastro pode ter '(11) 99999-8888'.
"""
import logging
import secrets
from decimal import Decimal

from flask import current_app, jsonify, render_template, request

from app.blueprints.crm import crm_bp
from app.extensions import csrf
from app.utils import telefone_chave

logger = logging.getLogger(__name__)

# Chamado de iframe externo (Chatwoot) — sem token CSRF. Autenticidade vem
# do CHATWOOT_CARD_TOKEN, não do cookie de sessão.
csrf.exempt(crm_bp)


def _token_ok(recebido):
    esperado = (current_app.config.get('CHATWOOT_CARD_TOKEN') or '').strip()
    if not esperado:
        return False
    return secrets.compare_digest(str(recebido or ''), esperado)


@crm_bp.route('/card')
def card():
    """Página HTML enxuta pro iframe do Chatwoot (Dashboard App)."""
    if not (current_app.config.get('CHATWOOT_CARD_TOKEN') or '').strip():
        return ('Integração Chatwoot não configurada '
                '(CHATWOOT_CARD_TOKEN ausente).'), 503
    return render_template('crm/card.html', token=request.args.get('k', ''))


@crm_bp.route('/card.json')
def card_json():
    if not _token_ok(request.args.get('k')):
        return jsonify({'ok': False, 'erro': 'token inválido'}), 403
    chave = telefone_chave(request.args.get('phone', ''))
    if not chave:
        return jsonify({'ok': True, 'encontrado': False, 'telefone': '',
                        'pedidos_locais': [], 'b2b': None})
    logger.info('crm card consultado: chave=%s', chave)
    return jsonify(_buscar_por_telefone(chave))


def _buscar_por_telefone(chave):
    """Agrega histórico do cliente por chave de telefone canônica.

    Como `telefone_chave` é Python-side (não SQL), filtramos em memória.
    Volume de PedidoLocal/ClienteB2B é baixo (pedidos manuais + clientes
    recorrentes); se crescer, adicionar coluna normalizada indexada.
    """
    from app.models import ClienteB2B, PedidoLocal, VendaB2B

    pedidos = []
    for p in (PedidoLocal.query
              .order_by(PedidoLocal.data_entrega.desc())
              .all()):
        if telefone_chave(p.telefone) != chave:
            continue
        pedidos.append({
            'code': p.code,
            'destinatario': p.destinatario,
            'data_entrega': p.data_entrega.strftime('%d/%m/%Y') if p.data_entrega else '',
            'total': round(p.total, 2),
            'itens': [{'nome': i.nome, 'qtd': i.quantidade,
                       'preco': round(i.preco_unitario or 0, 2)} for i in p.itens],
        })

    b2b = None
    cliente = next((c for c in ClienteB2B.query.all()
                    if telefone_chave(c.telefone) == chave), None)
    if cliente:
        vendas = (VendaB2B.query
                  .filter_by(cliente_id=cliente.id)
                  .filter(VendaB2B.status != 'cancelada')
                  .order_by(VendaB2B.data_venda.desc())
                  .all())
        debito = sum((v.valor_aberto for v in vendas), Decimal('0'))
        b2b = {
            'nome': cliente.nome,
            'contato': cliente.contato,
            'debito_aberto': float(debito),
            'vendas': [{
                'id': v.id,
                'data': (v.data_entrega or v.data_venda).strftime('%d/%m/%Y')
                        if (v.data_entrega or v.data_venda) else '',
                'valor_total': float(v.valor_total or 0),
                'valor_aberto': float(v.valor_aberto),
            } for v in vendas[:20]],
        }

    return {
        'ok': True,
        'encontrado': bool(pedidos or b2b),
        'telefone': chave,
        'pedidos_locais': pedidos,
        'b2b': b2b,
    }
