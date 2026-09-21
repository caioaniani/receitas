import sqlite3

import pytest

from app.migrations_legacy import _migrate_mp_custo_opcional_sqlite


def _banco_antigo():
    conn = sqlite3.connect(':memory:')
    conn.execute('PRAGMA foreign_keys=ON')
    conn.executescript('''
        CREATE TABLE usuario (id INTEGER PRIMARY KEY);
        INSERT INTO usuario VALUES (1);
        CREATE TABLE materia_prima (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL UNIQUE,
            custo_por_kg FLOAT NOT NULL,
            estoque_atual REAL DEFAULT 0 CHECK (estoque_atual >= 0),
            arquivada_por_id INTEGER REFERENCES usuario(id),
            observacoes TEXT DEFAULT 'não perder'
        );
        CREATE INDEX ix_mp_estoque ON materia_prima(estoque_atual);
        CREATE TABLE movimento (
            id INTEGER PRIMARY KEY,
            mp_id INTEGER REFERENCES materia_prima(id) ON DELETE CASCADE
        );
        CREATE TABLE auditoria (mp_id INTEGER);
        CREATE TRIGGER audita_mp AFTER UPDATE ON materia_prima
            BEGIN INSERT INTO auditoria VALUES (NEW.id); END;
        CREATE VIEW mp_nomes AS SELECT id, nome FROM materia_prima;
        INSERT INTO materia_prima(id, nome, custo_por_kg, estoque_atual, arquivada_por_id)
            VALUES (7, 'Farinha', 5.5, 1234, 1);
        INSERT INTO movimento VALUES (3, 7);
        INSERT INTO materia_prima(id, nome, custo_por_kg) VALUES (100, 'Removida', 1);
        DELETE FROM materia_prima WHERE id=100;
    ''')
    return conn


def test_relaxa_custo_preservando_schema_dados_vinculos_e_sequencia():
    conn = _banco_antigo()
    antes = conn.execute('SELECT * FROM materia_prima').fetchall()
    _migrate_mp_custo_opcional_sqlite(conn)
    _migrate_mp_custo_opcional_sqlite(conn)

    assert conn.execute('SELECT * FROM materia_prima').fetchall() == antes
    assert conn.execute('SELECT * FROM movimento').fetchall() == [(3, 7)]
    assert conn.execute('SELECT * FROM mp_nomes').fetchall() == [(7, 'Farinha')]
    assert conn.execute('PRAGMA foreign_keys').fetchone() == (1,)
    assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
    assert conn.execute('PRAGMA legacy_alter_table').fetchone() == (0,)
    assert 'ix_mp_estoque' in {r[1] for r in conn.execute('PRAGMA index_list(materia_prima)')}

    conn.execute("INSERT INTO materia_prima(nome, custo_por_kg) VALUES ('Azeitonas', NULL)")
    assert conn.execute("SELECT id, custo_por_kg, observacoes FROM materia_prima "
                        "WHERE nome='Azeitonas'").fetchone() == (101, None, 'não perder')
    conn.execute('UPDATE materia_prima SET custo_por_kg=6 WHERE id=7')
    assert conn.execute('SELECT * FROM auditoria').fetchall() == [(7,)]
    for sql in [
        "INSERT INTO materia_prima(nome) VALUES ('Farinha')",
        'INSERT INTO materia_prima(nome) VALUES (NULL)',
        "INSERT INTO materia_prima(nome, estoque_atual) VALUES ('X', -1)",
        "INSERT INTO materia_prima(nome, arquivada_por_id) VALUES ('Y', 999)",
    ]:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql)
    conn.close()


def test_falha_na_reconstrucao_reverte_tudo_e_restaura_pragmas():
    conn = _banco_antigo()

    class FalhaIndice:
        def __getattr__(self, attr):
            return getattr(conn, attr)

        def execute(self, sql, *args):
            if sql.startswith('CREATE INDEX ix_mp_estoque'):
                raise sqlite3.OperationalError('falha simulada')
            return conn.execute(sql, *args)

    with pytest.raises(sqlite3.OperationalError, match='falha simulada'):
        _migrate_mp_custo_opcional_sqlite(FalhaIndice())
    assert conn.execute('SELECT nome, custo_por_kg FROM materia_prima').fetchall() == [('Farinha', 5.5)]
    assert conn.execute('SELECT * FROM movimento').fetchall() == [(3, 7)]
    assert conn.execute('PRAGMA foreign_keys').fetchone() == (1,)
    assert conn.execute('PRAGMA legacy_alter_table').fetchone() == (0,)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO materia_prima(nome) VALUES ('Sem preço')")
    conn.close()
