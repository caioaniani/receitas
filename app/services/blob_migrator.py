"""Backfill de BLOBs do Postgres pro Dropbox (M6).

Migra fotos legadas que ainda tem `imagem` blob mas nao tem `imagem_url`.
Idempotente — re-rodar pula as ja migradas.

Cada modelo expoe uma funcao `migrar_<modelo>()` que itera + processa.

M6 Commit D (11/07/2026): as colunas BLOB sairam do MODELO SQLAlchemy —
este servico passou a ler/zerar os BLOBs por **SQL cru**, porque a tabela
ainda pode te-los ate o DROP guardado de `migrations_legacy` rodar (o DROP
so acontece quando nao resta nenhuma linha com BLOB; enquanto restar, o
dono drena por aqui, via card "Migracao BLOB" do /admin/debug-schema).
Coluna ja dropada = no-op com aviso, nunca erro.
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


def _coluna_existe(tabela, coluna):
    return bool(db.session.execute(
        text('SELECT 1 FROM information_schema.columns '
             'WHERE table_name = :t AND column_name = :c'),
        {'t': tabela, 'c': coluna}).scalar())


def _ja_dropada(tabela, coluna):
    """Resultado padrao quando a coluna BLOB ja foi dropada (Commit D)."""
    return {'ok': True, 'total': 0, 'migradas': 0, 'erros': 0,
            'detalhes': [f'{tabela}.{coluna} ja foi dropada '
                         '(M6 Commit D concluido) — nada a migrar']}


def _drenar(tabela, blob_col, url_col, sql_lote, montar_path, set_extra,
            batch_size=20, max_batches=None):
    """Motor generico do dreno por SQL cru.

    `sql_lote`: SELECT que devolve linhas com ao menos (id, blob) — colunas
    extras ficam disponiveis pro `montar_path(row)`. `montar_path` devolve o
    path Dropbox ou levanta ValueError com o motivo (ex: orfao).
    `set_extra`: fragmento SQL adicional do UPDATE (ex: ", mimetype='image/jpeg'").
    """
    def _job():
        if not _coluna_existe(tabela, blob_col):
            return _ja_dropada(tabela, blob_col)

        total_pendentes = db.session.execute(text(
            f'SELECT COUNT(*) FROM {tabela} '
            f'WHERE {url_col} IS NULL AND {blob_col} IS NOT NULL')).scalar()

        migradas = 0
        erros = 0
        detalhes = []
        batches_feitas = 0
        vistos_com_erro = set()

        while True:
            if max_batches and batches_feitas >= max_batches:
                detalhes.append(f'parou em max_batches={max_batches}')
                break

            rows = db.session.execute(
                text(sql_lote), {'n': batch_size}).mappings().all()
            # Linhas que ja falharam nesta rodada voltariam no proximo lote
            # (continuam com URL NULL) — sem este filtro o loop nunca anda.
            rows = [r for r in rows if r['id'] not in vistos_com_erro]
            if not rows:
                break

            for row in rows:
                try:
                    path = montar_path(row)
                    comprimida = comprimir_imagem(bytes(row[blob_col]))
                    info = dropbox_storage.upload_publico(
                        comprimida, path, mode='overwrite', autorename=False)
                    db.session.execute(text(
                        f'UPDATE {tabela} SET {url_col} = :u, '
                        f'imagem_storage_path = :p, {blob_col} = NULL'
                        f'{set_extra} WHERE id = :id'),
                        {'u': info['url'], 'p': info['storage_path'],
                         'id': row['id']})
                    migradas += 1
                except Exception as e:  # noqa: BLE001
                    detalhes.append(
                        f'{tabela} #{row["id"]}: {type(e).__name__}: {e}')
                    erros += 1
                    vistos_com_erro.add(row['id'])
                    logger.exception('[blob_migrator] %s #%s falhou',
                                     tabela, row['id'])

            db.session.commit()
            batches_feitas += 1
            logger.info('[blob_migrator] %s batch %d: migradas %d / erros %d',
                        tabela, batches_feitas, migradas, erros)

        return {
            'ok': True,
            'total': total_pendentes,
            'migradas': migradas,
            'erros': erros,
            'detalhes': detalhes[:50],  # cap pra nao explodir resposta
        }

    return _com_lock(_job)


def migrar_pedido_item_foto(batch_size=20, max_batches=None):
    """Migra pedido_item_foto.imagem (BLOB) pra Dropbox.

    Comprime cada foto (700x700 JPEG 82) antes de subir. Path determinístico
    permite re-rodar sem duplicar arquivos.

    Retorna {'ok', 'total', 'migradas', 'erros', 'detalhes'}.
    """
    def _path(row):
        if row['pedido_id'] is None:
            raise ValueError('pedido_item orfao')
        return (f'/conferencia/{row["pedido_id"]}/'
                f'{row["pedido_item_id"]}_{row["etapa"]}.jpg')

    sql = ('SELECT f.id, f.imagem, f.pedido_item_id, f.etapa, pi.pedido_id '
           'FROM pedido_item_foto f '
           'LEFT JOIN pedido_item pi ON pi.id = f.pedido_item_id '
           'WHERE f.imagem_url IS NULL AND f.imagem IS NOT NULL '
           'ORDER BY f.id LIMIT :n')
    return _drenar('pedido_item_foto', 'imagem', 'imagem_url', sql, _path,
                   ", mimetype = 'image/jpeg'", batch_size, max_batches)


def migrar_foto_recebimento(batch_size=20, max_batches=None):
    """Migra foto_recebimento.imagem (BLOB) pra Dropbox."""
    def _path(row):
        # Path: <pedido_id>/<foto_id>.jpg (foto_id ja eh unico)
        return f'/recebimento/{row["pedido_id"]}/{row["id"]}.jpg'

    sql = ('SELECT id, imagem, pedido_id FROM foto_recebimento '
           'WHERE imagem_url IS NULL AND imagem IS NOT NULL '
           'ORDER BY id LIMIT :n')
    return _drenar('foto_recebimento', 'imagem', 'imagem_url', sql, _path,
                   ", mimetype = 'image/jpeg'", batch_size, max_batches)


def _migrar_catalogo(tabela, tipo, batch_size=20, max_batches=None):
    """Migra receita.imagem_blob ou produto.imagem_blob pra Dropbox."""
    def _path(row):
        return f'/cardapio/{tipo}/{row["id"]}.jpg'

    sql = (f'SELECT id, imagem_blob FROM {tabela} '
           'WHERE imagem_dropbox_url IS NULL AND imagem_blob IS NOT NULL '
           'ORDER BY id LIMIT :n')
    return _drenar(tabela, 'imagem_blob', 'imagem_dropbox_url', sql, _path,
                   ", imagem_mimetype = 'image/jpeg'", batch_size,
                   max_batches)


def migrar_receita_imagem(batch_size=20, max_batches=None):
    return _migrar_catalogo('receita', 'receita', batch_size, max_batches)


def migrar_produto_imagem(batch_size=20, max_batches=None):
    return _migrar_catalogo('produto', 'produto', batch_size, max_batches)
