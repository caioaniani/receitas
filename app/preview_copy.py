"""Copia sanitizada do nucleo operacional para o ambiente de preview.

O processo e allowlist-only: tabelas novas nao entram na copia por acidente.
A origem e somente lida; clientes, RH, financeiro e integracoes nao sao
consultados.
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import MetaData, create_engine, select, text

from app.extensions import db

_MARKER_KEY = 'preview_dataset_version'
_LOCK_ID = 734_210_992

# Ordem de dependencia para insercao. Na limpeza a ordem e invertida.
SAFE_TABLES = (
    'loja',
    'massa_base',
    'produto',
    'estoque_site_plano',
    'venda_seru_dia_breakdown',
    'venda_seru_dia_loja',
    'venda_seru_diaria',
    'cronograma_dia_fechado',
    'materia_prima',
    'pedido_loja',
    'planejamento_producao',
    'receita',
    'cronograma_override',
    'desperdicio',
    'estoque_loja',
    'estoque_producao',
    'massa_base_item',
    'pedido_item',
    'perda_producao',
    'planejamento_item',
    'preco_loja_receita',
    'previsao_snapshot',
    'produto_item',
    'receita_etapa',
    'receita_ingrediente',
    'venda_manual_loja',
    'mov_estoque_loja',
    'mov_estoque_producao',
)

# Historico volumoso fica limitado. Pedidos e planejamentos sao copiados por
# inteiro para seus itens nunca ficarem orfaos.
_RECENT_DATE_COLUMNS = {
    'estoque_site_plano': 'data',
    'venda_seru_dia_breakdown': 'data',
    'venda_seru_dia_loja': 'data',
    'venda_seru_diaria': 'data',
    'cronograma_override': 'data',
    'desperdicio': 'data',
    'perda_producao': 'criado_em',
    'previsao_snapshot': 'data_alvo',
    'venda_manual_loja': 'data_venda',
    'mov_estoque_loja': 'data',
    'mov_estoque_producao': 'data',
}

_NULL_FOREIGN_KEYS = {
    'criado_por', 'criado_por_id', 'modificado_por_id', 'arquivada_por_id',
    'dispensada_por_id', 'usuario_id', 'funcionario_id', 'driver_id',
    'desperdicio_id',
}

_LOJA_PRIVATE_COLUMNS = {
    'endereco', 'telefone', 'pin', 'cnpj', 'inscricao_estadual',
    'endereco_logradouro', 'endereco_numero', 'endereco_complemento',
    'endereco_bairro', 'endereco_cep', 'endereco_cidade', 'endereco_uf',
    'razao_social', 'planta_imagem', 'planta_mimetype',
}


def preview_snapshot_loaded():
    """Indica se ja existe uma fotografia importada no banco do preview."""
    return db.session.execute(text(
        'SELECT value FROM app_config WHERE key = :key'),
        {'key': _MARKER_KEY}).scalar() is not None


def _normalizar_url(url):
    return url.replace('postgres://', 'postgresql://', 1)


def _sanitizar(nome_tabela, row):
    data = dict(row)
    for coluna in _NULL_FOREIGN_KEYS:
        if coluna in data:
            data[coluna] = None
    if nome_tabela == 'loja':
        for coluna in _LOJA_PRIVATE_COLUMNS:
            if coluna in data:
                data[coluna] = None
    if nome_tabela in {'pedido_loja', 'pedido_item', 'desperdicio',
                       'perda_producao'} and 'observacao' in data:
        data['observacao'] = 'Dados sanitizados para homologacao'
    # Imagens podem conter metadados e tornam a copia desnecessariamente pesada.
    for coluna in tuple(data):
        if coluna.endswith('_blob') or coluna == 'imagem_blob':
            data[coluna] = None
    # Evita dependencia de ordem dentro da propria tabela de receitas.
    if nome_tabela == 'receita' and 'retorno_receita_id' in data:
        data['retorno_receita_id'] = None
    return data


def _linhas_origem(source, table, *, dias_historico):
    stmt = select(table)
    date_column = _RECENT_DATE_COLUMNS.get(table.name)
    if date_column and date_column in table.c:
        stmt = stmt.where(table.c[date_column] >= date.today() - timedelta(
            days=dias_historico))
    with source.connect() as conn:
        return [_sanitizar(table.name, row)
                for row in conn.execute(stmt).mappings()]


def _limpar_destino(tables):
    if db.engine.dialect.name == 'postgresql':
        nomes = ', '.join(f'"{table.name}"' for table in tables)
        db.session.execute(text(
            f'TRUNCATE TABLE {nomes} RESTART IDENTITY CASCADE'))
        return
    db.session.execute(text('PRAGMA defer_foreign_keys=ON'))
    for table in reversed(tables):
        db.session.execute(table.delete())


def _ajustar_sequences(tables):
    if db.engine.dialect.name != 'postgresql':
        return
    for table in tables:
        if 'id' not in table.c:
            continue
        table_name = table.name.replace('"', '""')
        db.session.execute(text(
            "SELECT setval(pg_get_serial_sequence(:table_name, 'id'), "
            f'COALESCE((SELECT MAX(id) FROM "{table_name}"), 1), '
            f'(SELECT COUNT(*) > 0 FROM "{table_name}"))'
        ), {'table_name': table.name})


def copy_preview_data(source_url, version, *, dias_historico=180):
    """Substitui dados demo por uma copia operacional sanitizada.

    Retorna um resumo por tabela, ou ``None`` se a mesma versao ja foi
    importada. O chamador deve garantir que PREVIEW_MODE esta ativo.
    """
    if not source_url or not version:
        return None
    source_url = _normalizar_url(source_url)
    if _normalizar_url(str(db.engine.url)) == source_url:
        raise ValueError('A origem nao pode ser o banco do proprio preview.')

    if db.engine.dialect.name == 'postgresql':
        db.session.execute(text('SELECT pg_advisory_xact_lock(:lock_id)'),
                           {'lock_id': _LOCK_ID})

    marker = db.session.execute(text(
        'SELECT value FROM app_config WHERE key = :key'),
        {'key': _MARKER_KEY}).scalar()
    if marker == version:
        db.session.rollback()
        return None

    source = create_engine(source_url, pool_pre_ping=True)
    try:
        source_meta = MetaData()
        source_meta.reflect(bind=source, only=list(SAFE_TABLES))
        target_tables = [db.metadata.tables[name] for name in SAFE_TABLES]
        rows_by_table = {
            name: _linhas_origem(
                source, source_meta.tables[name],
                dias_historico=dias_historico,
            )
            for name in SAFE_TABLES
        }

        _limpar_destino(target_tables)
        for table in target_tables:
            target_columns = set(table.c.keys())
            rows = [
                {key: value for key, value in row.items()
                 if key in target_columns}
                for row in rows_by_table[table.name]
            ]
            for start in range(0, len(rows), 500):
                db.session.execute(table.insert(), rows[start:start + 500])
        _ajustar_sequences(target_tables)
        db.session.execute(text(
            'INSERT INTO app_config (key, value) VALUES (:key, :value) '
            'ON CONFLICT (key) DO UPDATE SET value = :value'
        ), {'key': _MARKER_KEY, 'value': version})
        db.session.commit()
        return {name: len(rows) for name, rows in rows_by_table.items()}
    except Exception:
        db.session.rollback()
        raise
    finally:
        source.dispose()
