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

# Marcador que o gerar da grade/cron grava na observacao (pedidos_semana).
# FONTE UNICA: pedidos_semana (escrita), previsao_producao (exclusao da
# media + ressinc do motor) e previsao_acuracia (circularidade) leem daqui —
# mudar o texto num lugar so quebraria os 4 em silencio.
MARCADOR_RASCUNHO_AUTO = 'Gerado do histórico'
OBSERVACAO_RASCUNHO_AUTO = ('Gerado do histórico (rascunho) — revisar e '
                            'confirmar.')


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


def rascunho_automatico_aberto(loja_id, data_entrega):
    """Rascunho do CRON de auto-pedidos pra (loja, data): 'pendente', SEM
    autor humano (criado_por/modificado_por_id nulos) e com o marcador do
    gerar. Qualquer toque humano tira o pedido daqui (vira pedido normal)."""
    if not data_entrega:
        return None
    return (PedidoLoja.query
            .filter_by(loja_id=loja_id, data_entrega=data_entrega,
                       status='pendente')
            .filter(PedidoLoja.criado_por.is_(None),
                    PedidoLoja.modificado_por_id.is_(None),
                    PedidoLoja.observacao.like(MARCADOR_RASCUNHO_AUTO + '%'))
            .order_by(PedidoLoja.id)
            .first())


def adotar_rascunho_automatico(pedido, itens, user_id, observacao=None):
    """O humano "criou o pedido do dia" num (loja, dia) que o cron ja cobriu
    com rascunho automatico: ADOTA o rascunho em vez de duplicar (2 pedidos
    no mesmo dia = demanda em DOBRO no balanco e na ordem do padeiro).

    Semantica (10/08/2026, junto do pacote de auto-pedidos):
    - item CITADO pelo humano SUBSTITUI a quantidade do motor (somar
      dobraria — o gesto e "o pedido e este"). O match cai pra FK-sem-estado
      quando a chave exata nao existe e o rascunho tem UMA linha do item:
      o cron grava tudo com estado None, e "45 assado" citando o pao de 40
      do motor tem que substituir a linha (nao criar uma 2a — seria a dobra
      parcial que a adocao quis evitar; achado da revisao rodada 2);
    - item do motor que o humano NAO citou FICA (remove-lo em silencio
      deixaria a loja sem o item; a diferenca fica visivel no pedido);
    - status vira 'confirmado' e modificado_por_id protege o pedido do cron.
    NAO commita. Retorna {'substituidos', 'adicionados', 'mantidos'}.
    """
    idx = {_chave(it): it for it in pedido.itens}
    por_fk = {}
    for it in pedido.itens:
        fk = (it.receita_id, it.produto_id, it.materia_prima_id)
        por_fk[fk] = it if fk not in por_fk else None   # None = ambiguo
    substituidos = adicionados = 0
    tocadas = set()
    for novo in itens:
        ch = _chave(novo)
        existente = idx.get(ch)
        if existente is None:
            fk = (novo.get('receita_id'), novo.get('produto_id'),
                  novo.get('materia_prima_id'))
            candidato = por_fk.get(fk)
            if candidato is not None and _chave(candidato) not in tocadas:
                existente = candidato
        if existente is not None:
            tocadas.add(_chave(existente))
            existente.quantidade = int(novo['quantidade'])
            existente.estado = novo.get('estado')
            if novo.get('observacao'):
                existente.observacao = novo['observacao']
            substituidos += 1
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
            tocadas.add(ch)
            adicionados += 1
    mantidos = len([ch for ch in idx if ch not in tocadas])
    pedido.status = 'confirmado'
    pedido.observacao = (observacao
                         or 'Sugestão automática ajustada pelo pedido da loja.')
    pedido.modificado_em = agora()
    pedido.modificado_por_id = user_id
    return {'substituidos': substituidos, 'adicionados': adicionados,
            'mantidos': mantidos}


def absorver_rascunho_automatico(loja_id, data_entrega, user_id,
                                 excluir_id=None):
    """Ja existe pedido HUMANO no dia e o cron tambem deixou um rascunho
    automatico (estado de colisao — ex.: pedidos criados antes do merge
    cobrir o rascunho, ou pedido humano MOVIDO de data pra cima do
    rascunho): o rascunho virou redundancia e CANCELA (somar os itens dele
    seria exatamente a dobra que se quer evitar). `excluir_id` protege o
    proprio pedido em edicao de se cancelar. NAO commita. Retorna o
    rascunho cancelado ou None."""
    r = rascunho_automatico_aberto(loja_id, data_entrega)
    if r is None or r.id == excluir_id:
        return None
    r.status = 'cancelado'
    r.modificado_em = agora()
    r.modificado_por_id = user_id
    return r


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


def consolidar_loja_data(loja_id, data_entrega, status, modificado_por_id=None):
    """Junta TODOS os pedidos abertos (mesma loja + data + status) no mais
    antigo; os demais viram 'cancelado' (itens preservados no historico deles).

    Limpeza retroativa de duplicados que ja existiam antes da junção-na-criacao.
    NAO commita. Retorna (pedido_alvo|None, n_absorvidos).
    """
    if status not in STATUS_MESCLAVEL or not data_entrega:
        return None, 0
    pedidos = (PedidoLoja.query
               .filter_by(loja_id=loja_id, data_entrega=data_entrega, status=status)
               .order_by(PedidoLoja.id)
               .all())
    if len(pedidos) < 2:
        return (pedidos[0] if pedidos else None), 0
    alvo = pedidos[0]
    absorvidos = 0
    for outro in pedidos[1:]:
        itens = [{
            'receita_id': it.receita_id, 'produto_id': it.produto_id,
            'materia_prima_id': it.materia_prima_id, 'quantidade': it.quantidade,
            'estado': it.estado, 'observacao': it.observacao,
        } for it in outro.itens]
        mesclar_itens(alvo, itens, modificado_por_id=modificado_por_id)
        outro.status = 'cancelado'
        outro.modificado_em = agora()
        if modificado_por_id:
            outro.modificado_por_id = modificado_por_id
        absorvidos += 1
    return alvo, absorvidos
