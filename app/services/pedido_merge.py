"""Consolidacao de pedidos: junta itens num pedido aberto da mesma loja/data
em vez de criar um novo (evita pedidos duplicados pra mesma loja no mesmo dia).

Usado pelos 3 caminhos de criacao (web /novo, web /sugerir-pedido e o copilot
`executar_criar_pedido`). Identidade de item = (receita_id, produto_id,
materia_prima_id, estado) — a mesma chave usada no recebimento
(`pedidos/routes.py`) e no EstoqueLoja.
"""
from app.extensions import db
from app.models import PedidoItem, PedidoLoja
from app.utils import agora

# Pedido so eh "mesclavel" enquanto aberto — depois de separado o estoque/QR
# ja estao em movimento e juntar item seria errado.
STATUS_MESCLAVEL = ('pendente', 'confirmado')


def pedido_aberto_para_merge(loja_id, data_entrega, status='confirmado'):
    """Retorna o PedidoLoja aberto pra mesclar (mesma loja + data + status), ou
    None. So mescla em status mesclavel e com data_entrega definida. Pega o
    mais antigo (menor id) como alvo canonico."""
    if status not in STATUS_MESCLAVEL or not data_entrega:
        return None
    return (PedidoLoja.query
            .filter_by(loja_id=loja_id, data_entrega=data_entrega, status=status)
            .order_by(PedidoLoja.id)
            .first())


def _chave(it_ou_dict):
    """Chave de identidade do item (receita/produto/mp + estado)."""
    g = it_ou_dict.get if isinstance(it_ou_dict, dict) else lambda k: getattr(it_ou_dict, k)
    return (g('receita_id'), g('produto_id'), g('materia_prima_id'), g('estado'))


def mesclar_itens(pedido, itens, modificado_por_id=None):
    """Soma/anexa `itens` no `pedido` existente.

    Cada item eh um dict com receita_id/produto_id/materia_prima_id/quantidade/
    estado/observacao. Mesma chave (receita_id, produto_id, materia_prima_id,
    estado) → soma quantidade; chave nova → cria PedidoItem. Seta
    modificado_em/_por_id. NAO commita — quem chama controla a transacao.

    Retorna {'adicionados': int, 'somados': int}.
    """
    idx = {_chave(it): it for it in pedido.itens}
    adicionados = somados = 0
    for novo in itens:
        ch = _chave(novo)
        existente = idx.get(ch)
        if existente is not None:
            existente.quantidade = (existente.quantidade or 0) + int(novo['quantidade'])
            somados += 1
        else:
            pi = PedidoItem(
                pedido_id=pedido.id,
                receita_id=novo.get('receita_id'),
                produto_id=novo.get('produto_id'),
                materia_prima_id=novo.get('materia_prima_id'),
                quantidade=int(novo['quantidade']),
                estado=novo.get('estado'),
                observacao=novo.get('observacao'),
            )
            db.session.add(pi)
            idx[ch] = pi
            adicionados += 1
    pedido.modificado_em = agora()
    if modificado_por_id:
        pedido.modificado_por_id = modificado_por_id
    return {'adicionados': adicionados, 'somados': somados}
