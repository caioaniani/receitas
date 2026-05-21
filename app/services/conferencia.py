"""Conferencia com foto SKU por SKU em pedidos.

Fluxo:
1. Pedido em status 'separado' (saida) ou 'em_transporte' (entrega).
2. Operador (industria pra saida, motorista pra entrega) tira foto de
   CADA PedidoItem do pedido.
3. So apos TODAS as fotos presentes, o QR pode ser gerado.
4. QR escaneado + PIN do destinatario muda status.

Etapas:
- saida: PedidoItemFoto.etapa='saida'. Quem tira: industria (Usuario admin/producao).
- entrega: PedidoItemFoto.etapa='entrega'. Quem tira: motorista (Driver).
"""
from app.extensions import db
from app.models import PedidoItem, PedidoItemFoto

ETAPAS = ('saida', 'entrega')


def fotos_presentes(pedido, etapa):
    """Retorna dict {pedido_item_id: PedidoItemFoto} pra etapa dada."""
    if etapa not in ETAPAS:
        return {}
    item_ids = [it.id for it in (pedido.itens or [])]
    if not item_ids:
        return {}
    fotos = (PedidoItemFoto.query
             .filter(PedidoItemFoto.pedido_item_id.in_(item_ids))
             .filter_by(etapa=etapa)
             .all())
    return {f.pedido_item_id: f for f in fotos}


def faltam_fotos(pedido, etapa):
    """Retorna lista de PedidoItem sem foto na etapa dada. Vazia = pronto."""
    presentes = fotos_presentes(pedido, etapa)
    return [it for it in (pedido.itens or []) if it.id not in presentes]


def conferencia_completa(pedido, etapa):
    """True se todas as fotos da etapa foram tiradas."""
    if not pedido or not pedido.itens:
        return False
    return len(faltam_fotos(pedido, etapa)) == 0


def salvar_foto(pedido_item_id, etapa, file_storage,
                criado_por_id=None, criado_por_driver_id=None):
    """Salva foto pra um item. Substitui foto anterior se existir.

    Retorna (PedidoItemFoto, erro). Em erro, primeiro item eh None.
    """
    if etapa not in ETAPAS:
        return None, f'etapa invalida: {etapa}'
    if not file_storage or not getattr(file_storage, 'filename', None):
        return None, 'arquivo vazio'

    # Le os bytes
    blob = file_storage.read()
    if not blob:
        return None, 'imagem vazia'
    if len(blob) > 8 * 1024 * 1024:
        return None, 'imagem maior que 8MB'
    mimetype = (file_storage.mimetype or '').lower()
    if not mimetype.startswith('image/'):
        return None, f'mimetype invalido: {mimetype}'

    # Confere existencia do item
    item = PedidoItem.query.get(pedido_item_id)
    if not item:
        return None, 'pedido_item nao encontrado'

    # Substitui foto anterior (mesma chave (item, etapa))
    existente = (PedidoItemFoto.query
                 .filter_by(pedido_item_id=pedido_item_id, etapa=etapa)
                 .first())
    if existente:
        existente.imagem = blob
        existente.mimetype = mimetype
        if criado_por_id is not None:
            existente.criado_por_id = criado_por_id
        if criado_por_driver_id is not None:
            existente.criado_por_driver_id = criado_por_driver_id
        db.session.commit()
        return existente, None

    foto = PedidoItemFoto(
        pedido_item_id=pedido_item_id,
        etapa=etapa,
        imagem=blob,
        mimetype=mimetype,
        criado_por_id=criado_por_id,
        criado_por_driver_id=criado_por_driver_id,
    )
    db.session.add(foto)
    db.session.commit()
    return foto, None
