"""Migração preparatória: o desconto novo não recalcula snapshots antigos."""
import sqlite3
from types import SimpleNamespace

from sqlalchemy import create_engine

from app.extensions import db
from app.migrations_legacy import _migrate_sqlite


def test_migracao_desconto_preserva_snapshot_e_e_idempotente(app, tmp_path):
    arquivo = tmp_path / 'orcamento-legado.sqlite'
    uri = f'sqlite:///{arquivo}'
    engine = create_engine(uri)
    db.metadata.create_all(engine)
    engine.dispose()
    with sqlite3.connect(arquivo) as conn:
        # Também reproduz schema antigo depois que o modelo ganhar a coluna.
        colunas = {row[1] for row in conn.execute('PRAGMA table_info(orcamento_item)')}
        if 'desconto_percentual' in colunas:
            conn.execute('ALTER TABLE orcamento_item DROP COLUMN desconto_percentual')
        conn.execute(
            'INSERT INTO orcamento_item '
            '(id, orcamento_id, nome, quantidade, preco_unitario, subtotal) '
            "VALUES (1, 99, 'Preço negociado antigo', 100, 10.10, 1010.00)")

    legado = SimpleNamespace(config={'SQLALCHEMY_DATABASE_URI': uri})
    _migrate_sqlite(legado)
    with sqlite3.connect(arquivo) as conn:
        coluna = next(row for row in conn.execute('PRAGMA table_info(orcamento_item)')
                      if row[1] == 'desconto_percentual')
        assert coluna[3] == 1  # NOT NULL
        assert coluna[4] == '0'  # DEFAULT 0
        assert conn.execute(
            'SELECT quantidade, preco_unitario, subtotal, desconto_percentual '
            'FROM orcamento_item WHERE id=1').fetchone() == (100, 10.1, 1010, 0)
        conn.execute('UPDATE orcamento_item SET desconto_percentual=5 WHERE id=1')
        # Enquanto só o ALTER estiver no ar, o modelo antigo omite a coluna.
        conn.execute(
            'INSERT INTO orcamento_item '
            '(id, orcamento_id, nome, quantidade, preco_unitario, subtotal) '
            "VALUES (2, 99, 'Criado pelo modelo antigo', 20, 6.37, 127.40)")

    _migrate_sqlite(legado)
    with sqlite3.connect(arquivo) as conn:
        assert conn.execute(
            'SELECT quantidade, preco_unitario, subtotal, desconto_percentual '
            'FROM orcamento_item ORDER BY id').fetchall() == [
                (100, 10.1, 1010, 5), (20, 6.37, 127.4, 0)]
