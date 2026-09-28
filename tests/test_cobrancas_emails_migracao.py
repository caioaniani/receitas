"""Expansão de schema não altera cadastros nem registros de envio existentes."""
import sqlite3
from importlib import import_module
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.migrations_legacy import _migrate_cobrancas_emails, _migrate_cobrancas_emails_sqlite


@pytest.mark.parametrize('caminho', ['legacy', 'alembic'])
def test_emails_migracao_idempotente_preserva_dados(caminho):
    engine = sa.create_engine('sqlite://')
    with engine.begin() as conn:
        conn.execute(sa.text('CREATE TABLE cliente_b2b (id INTEGER PRIMARY KEY, email TEXT)'))
        conn.execute(sa.text('CREATE TABLE envio_cobranca (id INTEGER PRIMARY KEY, destinatario TEXT)'))
        conn.execute(sa.text("INSERT INTO cliente_b2b VALUES (1, 'principal@example.com')"))
        conn.execute(sa.text("INSERT INTO envio_cobranca VALUES (1, 'antigo@example.com')"))
        if caminho == 'legacy':
            _migrate_cobrancas_emails(conn)
            _migrate_cobrancas_emails(conn)
        else:
            modulo = import_module('migrations.versions.d48e7a91c203_emails_adicionais_cobranca')
            with patch.object(modulo, 'op', Operations(MigrationContext.configure(conn))):
                modulo.upgrade()
                modulo.upgrade()
        assert tuple(conn.execute(sa.text('SELECT email, emails_cobranca FROM cliente_b2b')).one()) == (
            'principal@example.com', None)
        assert tuple(conn.execute(sa.text('SELECT destinatario, copias FROM envio_cobranca')).one()) == (
            'antigo@example.com', None)
        conn.execute(sa.text("UPDATE cliente_b2b SET emails_cobranca = '[\"copia@example.com\"]'"))
        _migrate_cobrancas_emails(conn)
        assert conn.execute(sa.text('SELECT emails_cobranca FROM cliente_b2b')).scalar() == '["copia@example.com"]'
    engine.dispose()


def test_sqlite_nativo_migra_apenas_conexao_fornecida():
    with sqlite3.connect(':memory:') as conn:
        conn.execute('CREATE TABLE cliente_b2b (id INTEGER PRIMARY KEY, email TEXT)')
        conn.execute("INSERT INTO cliente_b2b VALUES (1, 'original@example.com')")
        _migrate_cobrancas_emails_sqlite(conn)
        _migrate_cobrancas_emails_sqlite(conn)
        assert conn.execute('SELECT email, emails_cobranca FROM cliente_b2b').fetchone() == (
            'original@example.com', None)
