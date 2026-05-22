"""Backfill de BLOBs do Postgres pro Dropbox (M6).

Migra fotos legadas que ainda tem `imagem` blob mas nao tem `imagem_url`.
Idempotente — re-rodar pula as ja migradas.

Cada modelo expoe uma funcao `migrar_<modelo>()` que itera + processa.
"""
import logging

from sqlalchemy import text

from app.extensions import db
from app.services import dropbox_storage
from app.utils import comprimir_imagem

logger = logging.getLogger(__name__)

LOCK_KEY_MIGRACAO = 7732


def _com_lock(callback):
    """Roda callback dentro de advisory lock pra serializar entre workers."""
    uri = db.engine.url.drivername
    is_pg = 'postgres' in uri
    if not is_pg:
        return {'ok': False, 'motivo': 'so roda em postgres'}

    with db.engine.connect() as conn:
        got = conn.execute(text('SELECT pg_try_advisory_lock(:k)'),
                           {'k': LOCK_KEY_MIGRACAO}).scalar()
        if not got:
            return {'ok': False, 'motivo': 'outro worker ja esta migrando'}
        try:
            return callback()
        finally:
            conn.execute(text('SELECT pg_advisory_unlock(:k)'),
                         {'k': LOCK_KEY_MIGRACAO})
            conn.commit()


def migrar_pedido_item_foto(batch_size=20, max_batches=None):
    """Migra PedidoItemFoto.imagem (BLOB) pra Dropbox.

    Comprime cada foto (700x700 JPEG 82) antes de subir. Path determinístico
    permite re-rodar sem duplicar arquivos.

    Retorna {'ok', 'total', 'migradas', 'erros', 'detalhes'}.
    """
    from app.models import PedidoItemFoto

    def _job():
        total_pendentes = (PedidoItemFoto.query
                           .filter(PedidoItemFoto.imagem_url.is_(None))
                           .filter(PedidoItemFoto.imagem.isnot(None))
                           .count())

        migradas = 0
        erros = 0
        detalhes = []
        batches_feitas = 0

        while True:
            if max_batches and batches_feitas >= max_batches:
                detalhes.append(f'parou em max_batches={max_batches}')
                break

            fotos = (PedidoItemFoto.query
                     .filter(PedidoItemFoto.imagem_url.is_(None))
                     .filter(PedidoItemFoto.imagem.isnot(None))
                     .order_by(PedidoItemFoto.id)
                     .limit(batch_size)
                     .all())
            if not fotos:
                break

            for foto in fotos:
                try:
                    item = foto.pedido_item
                    if not item:
                        detalhes.append(f'foto #{foto.id}: pedido_item orfao')
                        erros += 1
                        continue

                    comprimida = comprimir_imagem(foto.imagem)
                    path = (f'/conferencia/{item.pedido_id}/'
                            f'{foto.pedido_item_id}_{foto.etapa}.jpg')
                    info = dropbox_storage.upload_publico(
                        comprimida, path,
                        mode='overwrite', autorename=False)
                    foto.imagem_url = info['url']
                    foto.imagem_storage_path = info['storage_path']
                    foto.imagem = None  # libera BLOB
                    foto.mimetype = 'image/jpeg'
                    migradas += 1
                except Exception as e:  # noqa: BLE001
                    detalhes.append(f'foto #{foto.id}: {type(e).__name__}: {e}')
                    erros += 1
                    logger.exception('[blob_migrator] foto #%s falhou', foto.id)

            db.session.commit()
            batches_feitas += 1
            logger.info('[blob_migrator] batch %d: migradas %d / erros %d',
                        batches_feitas, migradas, erros)

        return {
            'ok': True,
            'total': total_pendentes,
            'migradas': migradas,
            'erros': erros,
            'detalhes': detalhes[:50],  # cap pra nao explodir resposta
        }

    return _com_lock(_job)


def migrar_foto_recebimento(batch_size=20, max_batches=None):
    """Migra FotoRecebimento.imagem (BLOB) pra Dropbox.

    Mesma estrategia do PedidoItemFoto: comprime + sobe + popula URL.
    """
    import time as _time

    from app.models import FotoRecebimento

    def _job():
        total_pendentes = (FotoRecebimento.query
                           .filter(FotoRecebimento.imagem_url.is_(None))
                           .filter(FotoRecebimento.imagem.isnot(None))
                           .count())

        migradas = 0
        erros = 0
        detalhes = []
        batches_feitas = 0

        while True:
            if max_batches and batches_feitas >= max_batches:
                detalhes.append(f'parou em max_batches={max_batches}')
                break

            fotos = (FotoRecebimento.query
                     .filter(FotoRecebimento.imagem_url.is_(None))
                     .filter(FotoRecebimento.imagem.isnot(None))
                     .order_by(FotoRecebimento.id)
                     .limit(batch_size)
                     .all())
            if not fotos:
                break

            for foto in fotos:
                try:
                    comprimida = comprimir_imagem(foto.imagem)
                    # Path: <pedido_id>/<foto_id>.jpg (foto_id ja eh unico)
                    path = (f'/recebimento/{foto.pedido_id}/'
                            f'{foto.id}.jpg')
                    info = dropbox_storage.upload_publico(
                        comprimida, path,
                        mode='overwrite', autorename=False)
                    foto.imagem_url = info['url']
                    foto.imagem_storage_path = info['storage_path']
                    foto.imagem = None
                    foto.mimetype = 'image/jpeg'
                    migradas += 1
                except Exception as e:  # noqa: BLE001
                    detalhes.append(f'foto #{foto.id}: {type(e).__name__}: {e}')
                    erros += 1
                    logger.exception('[blob_migrator] foto_recebimento #%s falhou',
                                     foto.id)

            db.session.commit()
            batches_feitas += 1
            _time.sleep(0.1)  # rate limit Dropbox

        return {
            'ok': True,
            'total': total_pendentes,
            'migradas': migradas,
            'erros': erros,
            'detalhes': detalhes[:50],
        }

    return _com_lock(_job)
