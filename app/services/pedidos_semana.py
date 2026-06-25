"""Geracao de pedidos da semana em RASCUNHO a partir da sugestao do historico
(Fatia 2). O sistema propoe (previsao_producao.sugerir_pedidos_semana), o admin
ajusta na tela e gera; a criacao real dos PedidoLoja acontece aqui, sempre como
status 'pendente' (rascunho) — pra o admin revisar e confirmar depois.
"""
from app.extensions import db
from app.models import PedidoItem, PedidoLoja
from app.services.previsao_producao import invalidar_sugestao_cache
from app.utils import hoje


def criar_pedidos_rascunho(pedidos, user_id):
    """Cria PedidoLoja em rascunho ('pendente') a partir de uma lista
    [{loja_id, data_entrega(date), itens: [{receita_id, qtd}]}].

    Salvaguardas:
    - Pula (loja, data) que JA tem pedido nao-cancelado — anti-duplicacao,
      re-checado no banco no momento da criacao (cobre corrida com a tela).
    - Ignora itens com qtd <= 0 e pedidos que ficam sem item.

    Retorna {'criados': n, 'pulados_existentes': n, 'itens': n}.
    """
    criados = pulados = total_itens = 0
    hoje_d = hoje()
    for ped in pedidos:
        loja_id = ped.get('loja_id')
        data_ent = ped.get('data_entrega')
        itens = [it for it in (ped.get('itens') or [])
                 if it.get('receita_id') and int(it.get('qtd') or 0) > 0]
        if not loja_id or not data_ent or not itens:
            continue
        # Anti-duplicacao: re-checa no banco (a tela ja sinaliza, mas pode
        # ter mudado entre o GET e o POST).
        existe = (PedidoLoja.query
                  .filter(PedidoLoja.loja_id == loja_id,
                          PedidoLoja.data_entrega == data_ent,
                          PedidoLoja.status != 'cancelado')
                  .first())
        if existe:
            pulados += 1
            continue
        pedido = PedidoLoja(
            loja_id=loja_id,
            data_entrega=data_ent,
            data_pedido=hoje_d,
            status='pendente',
            criado_por=user_id,
            observacao='Gerado do histórico (rascunho) — revisar e confirmar.',
        )
        db.session.add(pedido)
        db.session.flush()
        for it in itens:
            db.session.add(PedidoItem(
                pedido_id=pedido.id,
                receita_id=int(it['receita_id']),
                quantidade=int(it['qtd']),
            ))
            total_itens += 1
        criados += 1

    db.session.commit()
    invalidar_sugestao_cache()
    return {'criados': criados, 'pulados_existentes': pulados,
            'itens': total_itens}
