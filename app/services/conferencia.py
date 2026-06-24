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
    """Salva foto pra um item, sobe pro Dropbox. Substitui foto anterior.

    Pos-M6: imagem vai pro Dropbox (~150KB apos compressao PIL 700px JPEG).
    Banco guarda url + storage_path. Coluna `imagem` BLOB so popula em
    fotos legadas pre-migracao.

    Retorna (PedidoItemFoto, erro). Em erro, primeiro item eh None.
    """
    from app.services import dropbox_storage
    from app.utils import comprimir_imagem

    if etapa not in ETAPAS:
        return None, f'etapa invalida: {etapa}'
    if not file_storage or not getattr(file_storage, 'filename', None):
        return None, 'arquivo vazio'

    blob = file_storage.read()
    if not blob:
        return None, 'imagem vazia'
    if len(blob) > 8 * 1024 * 1024:
        return None, 'imagem maior que 8MB'
    mimetype = (file_storage.mimetype or '').lower()
    if not mimetype.startswith('image/'):
        return None, f'mimetype invalido: {mimetype}'

    item = PedidoItem.query.get(pedido_item_id)
    if not item:
        return None, 'pedido_item nao encontrado'

    if not dropbox_storage.disponivel():
        return None, 'storage de fotos nao configurado'

    # Comprime + sobe Dropbox. Path deterministico (overwrite ao re-tirar).
    try:
        comprimida = comprimir_imagem(blob)
    except ValueError as e:
        return None, f'erro comprimindo: {e}'

    path = f'/conferencia/{item.pedido_id}/{pedido_item_id}_{etapa}.jpg'
    try:
        info = dropbox_storage.upload_publico(
            comprimida, path, mode='overwrite', autorename=False)
    except RuntimeError as e:
        return None, f'upload Dropbox falhou: {e}'

    # Substitui registro anterior se existir (mesma chave (item, etapa))
    existente = (PedidoItemFoto.query
                 .filter_by(pedido_item_id=pedido_item_id, etapa=etapa)
                 .first())
    if existente:
        existente.imagem = None  # libera BLOB legado se tinha
        existente.imagem_url = info['url']
        existente.imagem_storage_path = info['storage_path']
        existente.mimetype = 'image/jpeg'  # comprimir_imagem sempre devolve JPEG
        if criado_por_id is not None:
            existente.criado_por_id = criado_por_id
        if criado_por_driver_id is not None:
            existente.criado_por_driver_id = criado_por_driver_id
        db.session.commit()
        return existente, None

    foto = PedidoItemFoto(
        pedido_item_id=pedido_item_id,
        etapa=etapa,
        imagem=None,
        imagem_url=info['url'],
        imagem_storage_path=info['storage_path'],
        mimetype='image/jpeg',
        criado_por_id=criado_por_id,
        criado_por_driver_id=criado_por_driver_id,
    )
    db.session.add(foto)
    db.session.commit()
    return foto, None
