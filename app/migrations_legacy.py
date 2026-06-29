"""Migrations legacy — sistema anterior ao Alembic.

Cada deploy chama `_migrate(app)` apos `db.create_all()`. As funcoes
`_migrate_postgres` e `_migrate_sqlite` aplicam `ALTER TABLE IF NOT EXISTS`
e `CREATE INDEX IF NOT EXISTS` de forma idempotente — mesma operacao em
2 sintaxes diferentes pra cobrir prod (Postgres) e dev local (SQLite).

Status: a partir de 2026-05-21 o sistema usa Alembic (ver migrations/).
Estas funcoes continuam aqui por compatibilidade e pra cobrir mudancas
de schema que ainda nao foram migradas pra Alembic.

ADICIONAR COLUNA NOVA: prefira gerar uma migration Alembic
(`flask db migrate`). So adicione aqui se for hot-fix urgente que
nao pode esperar revisao de migration.
"""
import logging
import os

from app.extensions import db

logger = logging.getLogger(__name__)


def _migrate(app):
    """Adiciona colunas novas sem perder dados existentes."""
    uri = app.config['SQLALCHEMY_DATABASE_URI']

    if uri.startswith('sqlite'):
        _migrate_sqlite(app)
    elif 'postgresql' in uri:
        _migrate_postgres(app)

    # Em testes o conftest faz create_all (schema final) e os testes de
    # consolidacao precisam inserir duplicatas — a trava rodaria por teste sem
    # necessidade. O teste dedicado chama `_migrate_estoque_trava` diretamente.
    if not os.environ.get('PYTEST_RUNNING'):
        _migrate_estoque_trava(app)


def _migrate_estoque_trava(app):
    """Estoque por produto: consolida duplicatas legadas e cria a trava de
    unicidade. Estado vive so no PEDIDO; o estoque (loja e industria) eh 1 linha
    por produto. Roda em qualquer dialeto (db.engine), idempotente.

    Sequencia (ordem importa): consolida ANTES de criar o indice unico — senao a
    criacao falha com duplicatas existentes. Guardada por advisory lock no
    Postgres (exec unica entre workers) e por checagem de existencia do indice.
    """
    from sqlalchemy import text
    is_pg = 'postgresql' in app.config['SQLALCHEMY_DATABASE_URI']

    def _trava_existe():
        try:
            with db.engine.connect() as c:
                q = ("SELECT 1 FROM pg_indexes WHERE indexname = 'uq_estoque_loja_receita'"
                     if is_pg else
                     "SELECT 1 FROM sqlite_master WHERE type='index' "
                     "AND name='uq_estoque_loja_receita'")
                return c.execute(text(q)).first() is not None
        except Exception:
            return False

    if _trava_existe():
        return

    ddls = (
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_estoque_loja_receita "
        "ON estoque_loja(loja_id, receita_id) WHERE receita_id IS NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_estoque_loja_produto "
        "ON estoque_loja(loja_id, produto_id) WHERE produto_id IS NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_estoque_loja_mp "
        "ON estoque_loja(loja_id, materia_prima_id) WHERE materia_prima_id IS NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_estoque_producao_receita "
        "ON estoque_producao(receita_id) WHERE receita_id IS NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_estoque_producao_produto "
        "ON estoque_producao(produto_id) WHERE produto_id IS NOT NULL",
    )

    lock_conn = None
    try:
        if is_pg:
            lock_conn = db.engine.connect()
            if not lock_conn.execute(text('SELECT pg_try_advisory_lock(7740)')).scalar():
                lock_conn.close()
                return  # outro worker esta aplicando — sai sem erro
            if _trava_existe():  # outro worker terminou enquanto esperavamos
                lock_conn.execute(text('SELECT pg_advisory_unlock(7740)'))
                lock_conn.close()
                return

        from app.services.estoque_helpers import consolidar_estoque_duplicado
        n_loja, n_prod = consolidar_estoque_duplicado()
        db.session.commit()
        logger.warning('Estoque consolidado por produto: %s item(ns) de loja, '
                       '%s de producao.', n_loja, n_prod)
        for ddl in ddls:
            with db.engine.connect() as c:
                c.execute(text(ddl))
                c.commit()
        logger.warning('Trava de unicidade de estoque criada.')
    except Exception as e:
        db.session.rollback()
        logger.exception('consolidacao+trava de estoque falhou: %s', e)
    finally:
        if lock_conn is not None:
            try:
                lock_conn.execute(text('SELECT pg_advisory_unlock(7740)'))
            except Exception:
                pass
            lock_conn.close()


def _migrate_postgres(app):
    """Adiciona colunas novas no PostgreSQL. Cada ALTER em commit isolado
    para que falhas pontuais não abortem migrations seguintes."""
    import logging

    from sqlalchemy import text
    log = logging.getLogger(__name__)

    def _try(stmt):
        """Executa um DDL em sub-conexão isolada com commit imediato."""
        try:
            with db.engine.connect() as c:
                c.execute(text(stmt))
                c.commit()
        except Exception as e:
            log.warning('migrate skip (%s): %s', stmt[:60], e)

    def _cols(table):
        try:
            with db.engine.connect() as c:
                r = c.execute(text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = :t"), {'t': table})
                return {row[0] for row in r}
        except Exception:
            return set()

    with db.engine.connect() as conn:
        # Verificar e adicionar colunas faltantes em receita
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'receita'"
        ))
        colunas = {row[0] for row in result}

        migrações_receita = {
            'perda_percentual': 'ALTER TABLE receita ADD COLUMN perda_percentual REAL DEFAULT 0',
            'preco_loja': 'ALTER TABLE receita ADD COLUMN preco_loja REAL',
            'preco_site': 'ALTER TABLE receita ADD COLUMN preco_site REAL',
            'preco_interno': 'ALTER TABLE receita ADD COLUMN preco_interno REAL',
            'custo_embalagem': 'ALTER TABLE receita ADD COLUMN custo_embalagem REAL DEFAULT 0',
            'modo_preparo': 'ALTER TABLE receita ADD COLUMN modo_preparo TEXT',
            'observacao': 'ALTER TABLE receita ADD COLUMN observacao TEXT',
            'reaproveitavel': 'ALTER TABLE receita ADD COLUMN reaproveitavel BOOLEAN NOT NULL DEFAULT FALSE',
            'familia': 'ALTER TABLE receita ADD COLUMN familia VARCHAR(30)',
            'estado_padrao': 'ALTER TABLE receita ADD COLUMN estado_padrao VARCHAR(20)',
            'dias_producao': 'ALTER TABLE receita ADD COLUMN dias_producao INTEGER NOT NULL DEFAULT 0',
            'capacidade_amassadeira_g': 'ALTER TABLE receita ADD COLUMN capacidade_amassadeira_g INTEGER NOT NULL DEFAULT 50000',
            'sugerir_pedido_loja': 'ALTER TABLE receita ADD COLUMN sugerir_pedido_loja BOOLEAN NOT NULL DEFAULT TRUE',
            'lote_pedido': 'ALTER TABLE receita ADD COLUMN lote_pedido INTEGER',
            'minimo_pedido': 'ALTER TABLE receita ADD COLUMN minimo_pedido INTEGER',
        }
        for col, sql in migrações_receita.items():
            if col not in colunas:
                conn.execute(text(sql))

        # Lote de pedido padrao (UMA vez: so se NADA foi configurado ainda — uma
        # vez que o dono mexa em qualquer lote, nunca mais sobrescreve). A loja
        # pede em pacotes: pao frances 50, sourdough 20, croissant almond 15,
        # croissant tradicional 50 com minimo 250 (-> 250/300). Ajustavel na
        # ficha. Nomes conferidos no pedido real (29/06).
        ja_tem_lote = conn.execute(text(
            "SELECT COUNT(*) FROM receita WHERE lote_pedido IS NOT NULL")
        ).scalar()
        if not ja_tem_lote:
            for sql in (
                "UPDATE receita SET lote_pedido=50 WHERE "
                "LOWER(nome) LIKE '%pão franc%' OR LOWER(nome) LIKE '%pao franc%'",
                "UPDATE receita SET lote_pedido=20 WHERE "
                "LOWER(nome) LIKE '%sourdough%'",
                "UPDATE receita SET lote_pedido=15 WHERE "
                "LOWER(nome) LIKE '%croissant%almond%'",
                "UPDATE receita SET lote_pedido=50, minimo_pedido=250 WHERE "
                "LOWER(nome) LIKE '%croissant%' AND LOWER(nome) NOT LIKE '%almond%'",
            ):
                conn.execute(text(sql))

        # Brioche entra no pre-preparo como assado por default (idempotente:
        # so seta se ainda nao houver valor — preserva decisao manual posterior).
        conn.execute(text(
            "UPDATE receita SET estado_padrao='assado' "
            "WHERE LOWER(nome) LIKE '%brioche%' AND estado_padrao IS NULL"
        ))

        # receita_ingrediente
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'receita_ingrediente'"
        ))
        cols_ing = {row[0] for row in result}
        if cols_ing and 'tipo' not in cols_ing:
            conn.execute(text("ALTER TABLE receita_ingrediente ADD COLUMN tipo TEXT DEFAULT 'mp'"))
        # FK pra sub-receita (tipo='receita'): liga por ID, não só por nome —
        # necessário pra baixa de estoque confiável (ex: croissant almond consome
        # croissant tradicional congelado). Backfill por nome exato (idempotente).
        if cols_ing and 'sub_receita_id' not in cols_ing:
            conn.execute(text(
                "ALTER TABLE receita_ingrediente "
                "ADD COLUMN sub_receita_id INTEGER REFERENCES receita(id)"))
        conn.execute(text("""
            UPDATE receita_ingrediente ri SET sub_receita_id = r.id
            FROM receita r
            WHERE ri.tipo = 'receita' AND ri.sub_receita_id IS NULL
              AND lower(trim(r.nome)) = lower(trim(ri.ingrediente_nome))
        """))

        # produto
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'produto'"
        ))
        cols_prod = {row[0] for row in result}
        if cols_prod:
            migrações_produto = {
                'custo_direto': 'ALTER TABLE produto ADD COLUMN custo_direto REAL',
                'custo_embalagem': 'ALTER TABLE produto ADD COLUMN custo_embalagem REAL DEFAULT 0',
                'preco_interno': 'ALTER TABLE produto ADD COLUMN preco_interno REAL',
                'modo_preparo': 'ALTER TABLE produto ADD COLUMN modo_preparo TEXT',
                'observacao': 'ALTER TABLE produto ADD COLUMN observacao TEXT',
                'reaproveitavel': 'ALTER TABLE produto ADD COLUMN reaproveitavel BOOLEAN NOT NULL DEFAULT FALSE',
            }
            for col, sql in migrações_produto.items():
                if col not in cols_prod:
                    conn.execute(text(sql))

        # funcionario
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'funcionario'"
        ))
        cols_func = {row[0] for row in result}
        if cols_func:
            migrações_func = {
                'funcao_operacional': 'ALTER TABLE funcionario ADD COLUMN funcao_operacional VARCHAR(100)',
                'periodo': 'ALTER TABLE funcionario ADD COLUMN periodo VARCHAR(20)',
                'cadastro_pendente': 'ALTER TABLE funcionario ADD COLUMN cadastro_pendente BOOLEAN DEFAULT FALSE',
                'data_nascimento': 'ALTER TABLE funcionario ADD COLUMN data_nascimento DATE',
                'horas_extras': 'ALTER TABLE funcionario ADD COLUMN horas_extras REAL DEFAULT 0',
            }
            for col, sql in migrações_func.items():
                if col not in cols_func:
                    conn.execute(text(sql))

        # loja
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'loja'"
        ))
        cols_loja = {row[0] for row in result}
        if cols_loja:
            if 'planta_imagem' not in cols_loja:
                conn.execute(text("ALTER TABLE loja ADD COLUMN planta_imagem BYTEA"))
            if 'planta_mimetype' not in cols_loja:
                conn.execute(text("ALTER TABLE loja ADD COLUMN planta_mimetype VARCHAR(100)"))

        # slot_mapa
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'slot_mapa'"
        ))
        cols_slot = {row[0] for row in result}
        if cols_slot:
            if 'largura' not in cols_slot:
                conn.execute(text("ALTER TABLE slot_mapa ADD COLUMN largura REAL DEFAULT 15"))
            if 'altura' not in cols_slot:
                conn.execute(text("ALTER TABLE slot_mapa ADD COLUMN altura REAL DEFAULT 8"))

        # usuario.loja_id
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'usuario'"
        ))
        cols_user = {row[0] for row in result}
        if cols_user and 'loja_id' not in cols_user:
            conn.execute(text("ALTER TABLE usuario ADD COLUMN loja_id INTEGER REFERENCES loja(id)"))

        # posicao.origem
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'posicao'"
        ))
        cols_pos = {row[0] for row in result}
        if cols_pos and 'origem' not in cols_pos:
            conn.execute(text("ALTER TABLE posicao ADD COLUMN origem VARCHAR(10) DEFAULT 'manual'"))
            conn.execute(text(
                "UPDATE posicao SET origem = 'mapa' WHERE EXISTS ("
                "  SELECT 1 FROM slot_mapa WHERE slot_mapa.loja_id = posicao.loja_id "
                "  AND slot_mapa.nome = posicao.nome_posicao)"
            ))

        # materia_prima.estoque_atual
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'materia_prima'"
        ))
        cols_mp = {row[0] for row in result}
        if cols_mp and 'estoque_atual' not in cols_mp:
            conn.execute(text("ALTER TABLE materia_prima ADD COLUMN estoque_atual REAL DEFAULT 0"))
        if cols_mp and 'peso_unidade' not in cols_mp:
            conn.execute(text("ALTER TABLE materia_prima ADD COLUMN peso_unidade REAL"))

        # pedido_item.quantidade_recebida + materia_prima_id
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'pedido_item'"
        ))
        cols_pi = {row[0] for row in result}
        if cols_pi and 'quantidade_recebida' not in cols_pi:
            conn.execute(text("ALTER TABLE pedido_item ADD COLUMN quantidade_recebida INTEGER"))
        if cols_pi and 'materia_prima_id' not in cols_pi:
            conn.execute(text("ALTER TABLE pedido_item ADD COLUMN materia_prima_id INTEGER REFERENCES materia_prima(id)"))
        if cols_pi and 'estado' not in cols_pi:
            conn.execute(text("ALTER TABLE pedido_item ADD COLUMN estado VARCHAR(20)"))

        # estoque_loja.materia_prima_id + nome_pendente
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'estoque_loja'"
        ))
        cols_el = {row[0] for row in result}
        if cols_el and 'materia_prima_id' not in cols_el:
            conn.execute(text("ALTER TABLE estoque_loja ADD COLUMN materia_prima_id INTEGER REFERENCES materia_prima(id)"))
        if cols_el and 'nome_pendente' not in cols_el:
            conn.execute(text("ALTER TABLE estoque_loja ADD COLUMN nome_pendente VARCHAR(200)"))
        if cols_el and 'estado' not in cols_el:
            conn.execute(text("ALTER TABLE estoque_loja ADD COLUMN estado VARCHAR(20)"))

        # Tabelas Seru (mapeamento + idempotencia). db.create_all() cria
        # automaticamente, este bloco e so safety pra ambientes que ja
        # existiam antes do schema.
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS seru_produto_map (
                id SERIAL PRIMARY KEY,
                seru_nome VARCHAR(300) NOT NULL UNIQUE,
                seru_sku VARCHAR(100),
                receita_id INTEGER REFERENCES receita(id),
                produto_id INTEGER REFERENCES produto(id),
                ignorar BOOLEAN NOT NULL DEFAULT FALSE,
                primeira_visto_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                confirmado_em TIMESTAMP,
                confirmado_por INTEGER REFERENCES usuario(id)
            )
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_seru_produto_nome ON seru_produto_map(seru_nome)"))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS seru_loja_map (
                id SERIAL PRIMARY KEY,
                seru_company_name VARCHAR(300) NOT NULL UNIQUE,
                loja_id INTEGER REFERENCES loja(id),
                ignorar BOOLEAN NOT NULL DEFAULT FALSE,
                auto_match BOOLEAN DEFAULT FALSE,
                confirmado_em TIMESTAMP,
                confirmado_por INTEGER REFERENCES usuario(id)
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS seru_pedido_processado (
                seru_pedido_id VARCHAR(100) PRIMARY KEY,
                processado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                loja_id INTEGER REFERENCES loja(id),
                n_itens_total INTEGER DEFAULT 0,
                n_itens_baixados INTEGER DEFAULT 0,
                cancelado_em TIMESTAMP,
                estornado_em TIMESTAMP
            )
        """))

        # Coluna nova em SeruProdutoMap pra produtos compostos/fracionados
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'seru_produto_map'"
        ))
        cols_spm = {row[0] for row in result}
        if cols_spm and 'fator_quantidade' not in cols_spm:
            conn.execute(text("ALTER TABLE seru_produto_map ADD COLUMN fator_quantidade REAL NOT NULL DEFAULT 1.0"))

        # Acumulador de baixas fracionadas
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS seru_debito (
                loja_id INTEGER NOT NULL REFERENCES loja(id),
                seru_produto_map_id INTEGER NOT NULL REFERENCES seru_produto_map(id) ON DELETE CASCADE,
                fracao_pendente REAL NOT NULL DEFAULT 0.0,
                atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (loja_id, seru_produto_map_id)
            )
        """))

        # mov_estoque_loja.tipo: VARCHAR(20) era curto pra 'venda_seru_sem_estoque' (22)
        try:
            conn.execute(text("ALTER TABLE mov_estoque_loja ALTER COLUMN tipo TYPE VARCHAR(50)"))
        except Exception:
            pass

        # mov_estoque_producao.tipo: VARCHAR(20) era curto pra 'venda_b2b_sem_estoque' (21).
        # Estourava o INSERT quando uma venda B2B nao tinha estoque suficiente,
        # quebrando o POST de /b2b/vendas/nova com 500 (causa identificada via
        # log em 06/06/2026). Mesmo motivo do mov_estoque_loja acima.
        try:
            conn.execute(text("ALTER TABLE mov_estoque_producao ALTER COLUMN tipo TYPE VARCHAR(50)"))
        except Exception:
            pass

        # Tabelas VNDA (mapeamento + idempotencia + acumulador fracionario)
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS vnda_produto_map (
                id SERIAL PRIMARY KEY,
                vnda_nome VARCHAR(300) NOT NULL UNIQUE,
                vnda_sku VARCHAR(100),
                receita_id INTEGER REFERENCES receita(id),
                produto_id INTEGER REFERENCES produto(id),
                ignorar BOOLEAN NOT NULL DEFAULT FALSE,
                fator_quantidade REAL NOT NULL DEFAULT 1.0,
                primeira_visto_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                confirmado_em TIMESTAMP,
                confirmado_por INTEGER REFERENCES usuario(id)
            )
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_vnda_produto_nome ON vnda_produto_map(vnda_nome)"))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS vnda_pedido_processado (
                vnda_pedido_code VARCHAR(100) PRIMARY KEY,
                processado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                data_entrega DATE,
                n_itens_total INTEGER DEFAULT 0,
                n_itens_baixados INTEGER DEFAULT 0,
                cancelado_em TIMESTAMP,
                estornado_em TIMESTAMP
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS vnda_debito (
                vnda_produto_map_id INTEGER NOT NULL REFERENCES vnda_produto_map(id) ON DELETE CASCADE,
                componente_key VARCHAR(50) NOT NULL DEFAULT 'self',
                fracao_pendente REAL NOT NULL DEFAULT 0.0,
                atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (vnda_produto_map_id, componente_key)
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS app_config (
                key VARCHAR(100) PRIMARY KEY,
                value TEXT
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS loja_produto_map (
                id SERIAL PRIMARY KEY,
                nome_digitado VARCHAR(200) NOT NULL UNIQUE,
                receita_id INTEGER REFERENCES receita(id),
                produto_id INTEGER REFERENCES produto(id),
                materia_prima_id INTEGER REFERENCES materia_prima(id),
                ignorar BOOLEAN DEFAULT FALSE NOT NULL,
                fator_quantidade REAL NOT NULL DEFAULT 1.0,
                primeira_visto_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                confirmado_em TIMESTAMP,
                confirmado_por INTEGER REFERENCES usuario(id)
            )
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_loja_produto_map_nome ON loja_produto_map(nome_digitado)"))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS loja_debito (
                loja_id INTEGER NOT NULL REFERENCES loja(id),
                loja_produto_map_id INTEGER NOT NULL REFERENCES loja_produto_map(id) ON DELETE CASCADE,
                fracao_pendente REAL NOT NULL DEFAULT 0.0,
                atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (loja_id, loja_produto_map_id)
            )
        """))

        # pedido_loja.driver_id — quem pegou via handshake de saida
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'pedido_loja'"
        ))
        cols_pl = {row[0] for row in result}
        if cols_pl and 'driver_id' not in cols_pl:
            conn.execute(text(
                'ALTER TABLE pedido_loja ADD COLUMN driver_id INTEGER '
                'REFERENCES driver_entrega(id) ON DELETE SET NULL'
            ))
            conn.execute(text(
                'CREATE INDEX IF NOT EXISTS ix_pedido_loja_driver_id '
                'ON pedido_loja(driver_id)'
            ))

        # pedido_loja.modificado_em / modificado_por_id — quem editou e quando
        if cols_pl and 'modificado_em' not in cols_pl:
            conn.execute(text(
                'ALTER TABLE pedido_loja ADD COLUMN modificado_em TIMESTAMP'
            ))
        if cols_pl and 'modificado_por_id' not in cols_pl:
            conn.execute(text(
                'ALTER TABLE pedido_loja ADD COLUMN modificado_por_id INTEGER '
                'REFERENCES usuario(id) ON DELETE SET NULL'
            ))
            conn.execute(text(
                'CREATE INDEX IF NOT EXISTS ix_pedido_loja_modificado_por_id '
                'ON pedido_loja(modificado_por_id)'
            ))

        # pedido_item_foto — foto de conferencia por SKU
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS pedido_item_foto (
                id SERIAL PRIMARY KEY,
                pedido_item_id INTEGER NOT NULL REFERENCES pedido_item(id),
                etapa VARCHAR(10) NOT NULL,
                imagem BYTEA NOT NULL,
                mimetype VARCHAR(100),
                criado_em TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                criado_por_id INTEGER REFERENCES usuario(id),
                criado_por_driver_id INTEGER REFERENCES driver_entrega(id),
                CONSTRAINT uq_pedidoitemfoto_item_etapa UNIQUE (pedido_item_id, etapa)
            )
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_pedido_item_foto_pedido_item_id "
            "ON pedido_item_foto(pedido_item_id)"
        ))

        # M6: migracao BLOB → Dropbox em pedido_item_foto.
        # Adiciona colunas Dropbox e relaxa NOT NULL do BLOB pra novas fotos
        # ja subirem direto sem blob. Backfill popula URLs depois.
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'pedido_item_foto'"
        ))
        cols_pif = {row[0] for row in result}
        if cols_pif and 'imagem_url' not in cols_pif:
            conn.execute(text(
                'ALTER TABLE pedido_item_foto ADD COLUMN imagem_url VARCHAR(500)'
            ))
        if cols_pif and 'imagem_storage_path' not in cols_pif:
            conn.execute(text(
                'ALTER TABLE pedido_item_foto '
                'ADD COLUMN imagem_storage_path VARCHAR(500)'
            ))
        if cols_pif and 'imagem' in cols_pif:
            # Best-effort: drop NOT NULL. Em PG, eh idempotente em coluna ja
            # nullable. Se ja foi feito, este DDL ainda eh OK (no-op).
            conn.execute(text(
                'ALTER TABLE pedido_item_foto ALTER COLUMN imagem DROP NOT NULL'
            ))

        # M6: mesmas colunas em foto_recebimento (criada via db.create_all)
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'foto_recebimento'"
        ))
        cols_fr = {row[0] for row in result}
        if cols_fr and 'imagem_url' not in cols_fr:
            conn.execute(text(
                'ALTER TABLE foto_recebimento ADD COLUMN imagem_url VARCHAR(500)'
            ))
        if cols_fr and 'imagem_storage_path' not in cols_fr:
            conn.execute(text(
                'ALTER TABLE foto_recebimento '
                'ADD COLUMN imagem_storage_path VARCHAR(500)'
            ))
        if cols_fr and 'imagem' in cols_fr:
            conn.execute(text(
                'ALTER TABLE foto_recebimento ALTER COLUMN imagem DROP NOT NULL'
            ))

        # M6: imagem_dropbox_url + imagem_storage_path em receita e produto.
        # Aqui usamos nome diferente do imagem_url legado (URL externa de
        # fallback pra cardapio digital) que NAO eh substituido.
        for tabela in ('receita', 'produto'):
            result = conn.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = :t"
            ), {'t': tabela})
            cols = {row[0] for row in result}
            if cols and 'imagem_dropbox_url' not in cols:
                conn.execute(text(
                    f'ALTER TABLE {tabela} '
                    f'ADD COLUMN imagem_dropbox_url VARCHAR(500)'
                ))
            if cols and 'imagem_storage_path' not in cols:
                conn.execute(text(
                    f'ALTER TABLE {tabela} '
                    f'ADD COLUMN imagem_storage_path VARCHAR(500)'
                ))

        # driver_magic_token — magic link diario do motorista
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS driver_magic_token (
                id SERIAL PRIMARY KEY,
                driver_id INTEGER NOT NULL REFERENCES driver_entrega(id),
                token VARCHAR(64) NOT NULL UNIQUE,
                criado_em TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                expira_em TIMESTAMP NOT NULL,
                revogado BOOLEAN NOT NULL DEFAULT FALSE,
                enviado_em TIMESTAMP,
                enviado_ok BOOLEAN
            )
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_driver_magic_token_driver_id "
            "ON driver_magic_token(driver_id)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_driver_magic_token_token "
            "ON driver_magic_token(token)"
        ))

        # estoque_producao.nome_pendente (balanco aceita itens sem cadastro previo)
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'estoque_producao'"
        ))
        cols_ep = {row[0] for row in result}
        if cols_ep and 'nome_pendente' not in cols_ep:
            conn.execute(text("ALTER TABLE estoque_producao ADD COLUMN nome_pendente VARCHAR(200)"))
        if cols_ep and 'estado' not in cols_ep:
            conn.execute(text("ALTER TABLE estoque_producao ADD COLUMN estado VARCHAR(20)"))

        # vnda_debito.componente_key (cestas: PK composta para 1 acumulador por componente)
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'vnda_debito'"
        ))
        cols_vd = {row[0] for row in result}
        if cols_vd and 'componente_key' not in cols_vd:
            conn.execute(text("ALTER TABLE vnda_debito DROP CONSTRAINT IF EXISTS vnda_debito_pkey"))
            conn.execute(text(
                "ALTER TABLE vnda_debito ADD COLUMN componente_key VARCHAR(50) NOT NULL DEFAULT 'self'"
            ))
            conn.execute(text(
                "ALTER TABLE vnda_debito ADD PRIMARY KEY (vnda_produto_map_id, componente_key)"
            ))

        # B2B no padeiro (Fase 1): venda_b2b ganha data de entrega + um status
        # de ENTREGA proprio (pendente/separado/em_transporte/entregue),
        # separado do status FINANCEIRO (ativa/cancelada) pra nao mexer em
        # parcelas/faturamento. venda_b2b_item ganha estado (cru/backup/assado),
        # igual PedidoItem. So ALTER aqui; modelo entra no commit seguinte.
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'venda_b2b'"
        ))
        cols_vb = {row[0] for row in result}
        if cols_vb and 'data_entrega' not in cols_vb:
            conn.execute(text("ALTER TABLE venda_b2b ADD COLUMN data_entrega DATE"))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_venda_b2b_data_entrega "
                "ON venda_b2b(data_entrega)"
            ))
        if cols_vb and 'status_entrega' not in cols_vb:
            conn.execute(text(
                "ALTER TABLE venda_b2b ADD COLUMN status_entrega VARCHAR(20) "
                "NOT NULL DEFAULT 'pendente'"
            ))

        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'venda_b2b_item'"
        ))
        cols_vbi = {row[0] for row in result}
        if cols_vbi and 'estado' not in cols_vbi:
            conn.execute(text("ALTER TABLE venda_b2b_item ADD COLUMN estado VARCHAR(20)"))
        if cols_vbi and 'observacao' not in cols_vbi:
            conn.execute(text("ALTER TABLE venda_b2b_item ADD COLUMN observacao VARCHAR(200)"))

        conn.commit()

    # Migrações resilientes (cada ALTER em sua própria transação)
    cols_user2 = _cols('usuario')
    if cols_user2 and 'is_owner' not in cols_user2:
        _try("ALTER TABLE usuario ADD COLUMN is_owner BOOLEAN DEFAULT FALSE")
        _try("UPDATE usuario SET is_owner = TRUE WHERE id = "
             "(SELECT id FROM usuario WHERE papel = 'admin' ORDER BY id LIMIT 1)")

    # Migracao papel_v1: introduz niveis (gerente/producao/rh). Roda uma vez.
    # Downgrade de admins NAO-owner pra funcionario; owner sobe pra papel='admin' se ainda nao for.
    _try("CREATE TABLE IF NOT EXISTS migracao_marker (nome VARCHAR(50) PRIMARY KEY, executado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    try:
        with db.engine.connect() as c:
            r = c.execute(text("SELECT 1 FROM migracao_marker WHERE nome='papel_v1'")).fetchone()
            ja_rodou = bool(r)
    except Exception:
        ja_rodou = True  # se nao consegue ler, nao mexe
    if not ja_rodou:
        _try("UPDATE usuario SET papel='funcionario' WHERE papel='admin' AND (is_owner IS NULL OR is_owner = FALSE)")
        _try("UPDATE usuario SET papel='admin' WHERE is_owner = TRUE AND papel <> 'admin'")
        _try("INSERT INTO migracao_marker (nome) VALUES ('papel_v1')")

    cols_pa = _cols('projeto_area')
    if cols_pa and 'cor' not in cols_pa:
        _try("ALTER TABLE projeto_area ADD COLUMN cor VARCHAR(20)")

    cols_tp = _cols('tarefa_projeto')
    if cols_tp and 'observacao' not in cols_tp:
        _try("ALTER TABLE tarefa_projeto ADD COLUMN observacao TEXT")
    if cols_tp and 'recorrencia' not in cols_tp:
        _try("ALTER TABLE tarefa_projeto ADD COLUMN recorrencia VARCHAR(20)")

    cols_func_res = _cols('funcionario')
    if cols_func_res and 'horas_extras' not in cols_func_res:
        _try("ALTER TABLE funcionario ADD COLUMN horas_extras REAL DEFAULT 0")
    if cols_func_res and 'tem_cargo_confianca' not in cols_func_res:
        _try("ALTER TABLE funcionario ADD COLUMN tem_cargo_confianca BOOLEAN DEFAULT FALSE")
        # Liga a flag para quem ja tinha cargo_confianca > 0 (preserva comportamento)
        _try("UPDATE funcionario SET tem_cargo_confianca = TRUE WHERE cargo_confianca > 0")

    # Cargo: cria a coluna FK + popula cargos a partir das funcoes existentes
    if cols_func_res and 'cargo_id' not in cols_func_res:
        _try("ALTER TABLE funcionario ADD COLUMN cargo_id INTEGER REFERENCES cargo(id)")
        # Cria 1 cargo por funcao distinta com o salario MAIS COMUM (moda) de quem tem essa funcao
        _try("""
        INSERT INTO cargo (nome, salario_base, ativo)
        SELECT funcao, MAX(salario_base), TRUE
        FROM funcionario
        WHERE funcao IS NOT NULL AND TRIM(funcao) <> ''
        GROUP BY funcao
        ON CONFLICT (nome) DO NOTHING
        """)
        # Liga cada funcionario ao cargo correspondente
        _try("""
        UPDATE funcionario SET cargo_id = (
            SELECT id FROM cargo WHERE cargo.nome = funcionario.funcao
        ) WHERE funcao IS NOT NULL AND TRIM(funcao) <> ''
        """)

    # ── Override de data de entrega de pedido VNDA (local) ──
    _try("""
    CREATE TABLE IF NOT EXISTS override_entrega (
        id SERIAL PRIMARY KEY,
        pedido_code VARCHAR(50) NOT NULL UNIQUE,
        data_entrega DATE NOT NULL,
        motivo TEXT,
        atualizado_em TIMESTAMP DEFAULT NOW(),
        atualizado_por INTEGER REFERENCES usuario(id)
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_override_entrega_code ON override_entrega(pedido_code)")

    # ── Cache de geocoding (CEP/endereco -> lat/lng) ──
    _try("""
    CREATE TABLE IF NOT EXISTS geocode_cache (
        id SERIAL PRIMARY KEY,
        chave VARCHAR(200) NOT NULL UNIQUE,
        lat DOUBLE PRECISION,
        lng DOUBLE PRECISION,
        fonte VARCHAR(50),
        criado_em TIMESTAMP DEFAULT NOW()
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_geocode_cache_chave ON geocode_cache(chave)")
    # Aumenta coluna fonte caso ja exista com VARCHAR(20)
    _try("ALTER TABLE geocode_cache ALTER COLUMN fonte TYPE VARCHAR(50)")
    # Limpa cache de falhas legacy (Nominatim/BrasilAPI/AwesomeAPI/google_fail).
    # Endereco volta a ser geocodado na proxima execucao via Google.
    _try("DELETE FROM geocode_cache WHERE lat IS NULL")

    # ── Drivers de entrega + atribuicoes pedido<->driver ──
    _try("""
    CREATE TABLE IF NOT EXISTS driver_entrega (
        id SERIAL PRIMARY KEY,
        nome VARCHAR(80) NOT NULL UNIQUE,
        cor VARCHAR(20),
        telefone VARCHAR(30),
        ativo BOOLEAN DEFAULT TRUE,
        criado_em TIMESTAMP DEFAULT NOW()
    )
    """)
    _try("""
    CREATE TABLE IF NOT EXISTS atribuicao_entrega (
        id SERIAL PRIMARY KEY,
        pedido_code VARCHAR(50) NOT NULL UNIQUE,
        driver_id INTEGER REFERENCES driver_entrega(id) ON DELETE SET NULL,
        data_entrega DATE,
        ordem INTEGER DEFAULT 0,
        atualizado_em TIMESTAMP DEFAULT NOW(),
        atualizado_por INTEGER REFERENCES usuario(id)
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_atribuicao_pedido ON atribuicao_entrega(pedido_code)")
    _try("CREATE INDEX IF NOT EXISTS idx_atribuicao_data ON atribuicao_entrega(data_entrega)")

    # ── Comprovante de entrega: token+pin no driver, status+geo+fotos na atribuicao ──
    _try("ALTER TABLE driver_entrega ADD COLUMN token VARCHAR(32)")
    _try("CREATE UNIQUE INDEX IF NOT EXISTS idx_driver_token ON driver_entrega(token)")
    _try("ALTER TABLE driver_entrega ADD COLUMN pin VARCHAR(8)")
    _try("ALTER TABLE driver_entrega ADD COLUMN capacidade INTEGER DEFAULT 999")
    _try("ALTER TABLE atribuicao_entrega ADD COLUMN status VARCHAR(20) DEFAULT 'pendente'")
    _try("ALTER TABLE atribuicao_entrega ADD COLUMN entregue_em TIMESTAMP")
    _try("ALTER TABLE atribuicao_entrega ADD COLUMN nota VARCHAR(500)")
    _try("ALTER TABLE atribuicao_entrega ADD COLUMN motivo_falha VARCHAR(50)")
    _try("ALTER TABLE atribuicao_entrega ADD COLUMN geo_lat DOUBLE PRECISION")
    _try("ALTER TABLE atribuicao_entrega ADD COLUMN geo_lng DOUBLE PRECISION")
    _try("ALTER TABLE atribuicao_entrega ADD COLUMN proof_hash VARCHAR(32)")
    _try("CREATE UNIQUE INDEX IF NOT EXISTS idx_atribuicao_proof_hash ON atribuicao_entrega(proof_hash)")

    # Cronograma -> padeiro: alvo de unidades e quanto ja foi produzido por
    # item do plano (opcao B: a MP sai e o produto entra na producao).
    _try("ALTER TABLE planejamento_item ADD COLUMN qtd_alvo INTEGER")
    _try("ALTER TABLE planejamento_item ADD COLUMN produzido_qtd INTEGER NOT NULL DEFAULT 0")
    _try("ALTER TABLE planejamento_producao ADD COLUMN origem VARCHAR(20)")
    # Fluxo 2 passos (aprovar -> enviar): o padeiro só vê o que foi ENVIADO.
    # DEFAULT TRUE pra ordens já existentes continuarem visíveis; novas ordens
    # do "aprovar dia" nascem FALSE (rascunho) e viram TRUE no "enviar".
    _try("ALTER TABLE planejamento_producao ADD COLUMN enviado_ao_padeiro BOOLEAN DEFAULT TRUE")

    _try("""
    CREATE TABLE IF NOT EXISTS entrega_foto (
        id SERIAL PRIMARY KEY,
        atribuicao_id INTEGER NOT NULL REFERENCES atribuicao_entrega(id) ON DELETE CASCADE,
        url VARCHAR(500) NOT NULL,
        storage_path VARCHAR(500),
        tirada_em TIMESTAMP DEFAULT NOW(),
        tamanho_bytes INTEGER
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_entrega_foto_atribuicao ON entrega_foto(atribuicao_id)")

    # ── Lotes de saída ──
    _try("""
    CREATE TABLE IF NOT EXISTS lote_saida (
        id SERIAL PRIMARY KEY,
        nome VARCHAR(120) NOT NULL,
        data_entrega DATE NOT NULL,
        criado_em TIMESTAMP DEFAULT NOW(),
        janelas_json TEXT,
        status VARCHAR(20) DEFAULT 'aberto',
        criado_por INTEGER REFERENCES usuario(id)
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_lote_saida_data ON lote_saida(data_entrega)")
    _try("CREATE INDEX IF NOT EXISTS idx_lote_saida_status ON lote_saida(status)")
    _try("ALTER TABLE atribuicao_entrega ADD COLUMN lote_id INTEGER REFERENCES lote_saida(id)")
    _try("CREATE INDEX IF NOT EXISTS idx_atribuicao_lote ON atribuicao_entrega(lote_id)")

    # ── Audit Log estruturado ──
    _try("""
    CREATE TABLE IF NOT EXISTS audit_log (
        id SERIAL PRIMARY KEY,
        usuario_id INTEGER REFERENCES usuario(id),
        criado_em TIMESTAMP DEFAULT NOW(),
        tabela VARCHAR(60) NOT NULL,
        registro_id INTEGER,
        acao VARCHAR(10) NOT NULL,
        antes TEXT,
        depois TEXT,
        ip VARCHAR(45),
        user_agent VARCHAR(300)
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_audit_usuario ON audit_log(usuario_id)")
    _try("CREATE INDEX IF NOT EXISTS idx_audit_criado ON audit_log(criado_em)")
    _try("CREATE INDEX IF NOT EXISTS idx_audit_tabela ON audit_log(tabela)")
    _try("CREATE INDEX IF NOT EXISTS idx_audit_registro ON audit_log(tabela, registro_id)")

    # ── Fornecedores + historico de preco MP ──
    _try("""
    CREATE TABLE IF NOT EXISTS fornecedor (
        id SERIAL PRIMARY KEY,
        nome VARCHAR(150) NOT NULL UNIQUE,
        cnpj VARCHAR(20),
        telefone VARCHAR(30),
        email VARCHAR(120),
        contato VARCHAR(100),
        observacao TEXT,
        ativo BOOLEAN DEFAULT TRUE,
        criado_em TIMESTAMP DEFAULT NOW()
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_fornecedor_ativo ON fornecedor(ativo)")
    _try("""
    CREATE TABLE IF NOT EXISTS historico_preco_mp (
        id SERIAL PRIMARY KEY,
        materia_prima_id INTEGER NOT NULL REFERENCES materia_prima(id),
        fornecedor_id INTEGER NOT NULL REFERENCES fornecedor(id),
        preco_unitario DOUBLE PRECISION NOT NULL,
        quantidade DOUBLE PRECISION NOT NULL,
        data TIMESTAMP DEFAULT NOW(),
        referencia VARCHAR(200),
        usuario_id INTEGER REFERENCES usuario(id)
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_hpm_mp ON historico_preco_mp(materia_prima_id)")
    _try("CREATE INDEX IF NOT EXISTS idx_hpm_fornecedor ON historico_preco_mp(fornecedor_id)")
    _try("CREATE INDEX IF NOT EXISTS idx_hpm_data ON historico_preco_mp(data)")
    _try("ALTER TABLE movimentacao_estoque ADD COLUMN fornecedor_id INTEGER REFERENCES fornecedor(id)")

    # ── Copilot conversas (audit trail das interacoes com LLM) ──
    _try("""
    CREATE TABLE IF NOT EXISTS copilot_conversa (
        id SERIAL PRIMARY KEY,
        usuario_id INTEGER NOT NULL REFERENCES usuario(id),
        criado_em TIMESTAMP DEFAULT NOW(),
        prompt TEXT NOT NULL,
        interpretacao_json TEXT,
        tipo_acao VARCHAR(40),
        status VARCHAR(20) DEFAULT 'pendente',
        executado_em TIMESTAMP,
        registro_tipo VARCHAR(40),
        registro_id INTEGER,
        erro TEXT
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_copilot_usuario ON copilot_conversa(usuario_id)")
    _try("CREATE INDEX IF NOT EXISTS idx_copilot_criado ON copilot_conversa(criado_em)")
    _try("CREATE INDEX IF NOT EXISTS idx_copilot_status ON copilot_conversa(status)")

    # Backfill: cada data com atribuicoes orfas vira 1 lote 'Histórico DD/MM' concluido.
    # Idempotente — so cria se ainda houver lote_id NULL.
    _try("""
    INSERT INTO lote_saida (nome, data_entrega, criado_em, status)
    SELECT
        'Histórico ' || TO_CHAR(data_entrega, 'DD/MM/YYYY'),
        data_entrega,
        COALESCE(MIN(atualizado_em), NOW()),
        'concluido'
    FROM atribuicao_entrega
    WHERE lote_id IS NULL AND data_entrega IS NOT NULL
    GROUP BY data_entrega
    """)
    _try("""
    UPDATE atribuicao_entrega a
    SET lote_id = l.id
    FROM lote_saida l
    WHERE a.lote_id IS NULL
      AND a.data_entrega = l.data_entrega
      AND l.nome LIKE 'Histórico %'
    """)

    # ── Pedidos cadastrados fora do VNDA (manuais) ──
    _try("""
    CREATE TABLE IF NOT EXISTS pedido_local (
        id SERIAL PRIMARY KEY,
        code VARCHAR(50) NOT NULL UNIQUE,
        destinatario VARCHAR(200) NOT NULL,
        telefone VARCHAR(50) NOT NULL,
        endereco VARCHAR(500) NOT NULL,
        data_entrega DATE NOT NULL,
        periodo VARCHAR(80),
        cartinha TEXT,
        observacao TEXT,
        criado_em TIMESTAMP DEFAULT NOW(),
        criado_por INTEGER REFERENCES usuario(id)
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_pedido_local_data ON pedido_local(data_entrega)")
    _try("""
    CREATE TABLE IF NOT EXISTS pedido_local_item (
        id SERIAL PRIMARY KEY,
        pedido_local_id INTEGER NOT NULL REFERENCES pedido_local(id) ON DELETE CASCADE,
        nome VARCHAR(200) NOT NULL,
        quantidade INTEGER DEFAULT 1,
        preco_unitario REAL DEFAULT 0
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_pedido_local_item_pedido ON pedido_local_item(pedido_local_id)")

    # ── Desperdicio (sobra do dia / vencido) ──
    _try("""
    CREATE TABLE IF NOT EXISTS desperdicio (
        id SERIAL PRIMARY KEY,
        loja_id INTEGER NOT NULL REFERENCES loja(id),
        receita_id INTEGER REFERENCES receita(id),
        produto_id INTEGER REFERENCES produto(id),
        materia_prima_id INTEGER REFERENCES materia_prima(id),
        quantidade INTEGER NOT NULL,
        data DATE NOT NULL,
        motivo VARCHAR(30) NOT NULL DEFAULT 'vencido',
        observacao TEXT,
        criado_em TIMESTAMP DEFAULT NOW(),
        criado_por_id INTEGER REFERENCES usuario(id)
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_desperdicio_loja ON desperdicio(loja_id)")
    _try("CREATE INDEX IF NOT EXISTS idx_desperdicio_data ON desperdicio(data)")

    # ── Slack bot (DM/@mention → copilot) ──
    _try("""
    CREATE TABLE IF NOT EXISTS slack_vinculo (
        id SERIAL PRIMARY KEY,
        slack_user_id VARCHAR(30) NOT NULL UNIQUE,
        slack_workspace_id VARCHAR(30),
        usuario_id INTEGER NOT NULL REFERENCES usuario(id),
        ativo BOOLEAN DEFAULT TRUE,
        criado_em TIMESTAMP DEFAULT NOW(),
        criado_por_id INTEGER REFERENCES usuario(id)
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_slack_vinculo_uid ON slack_vinculo(slack_user_id)")
    _try("CREATE INDEX IF NOT EXISTS idx_slack_vinculo_ativo ON slack_vinculo(ativo)")

    _try("""
    CREATE TABLE IF NOT EXISTS slack_evento_processado (
        event_id VARCHAR(50) PRIMARY KEY,
        processado_em TIMESTAMP DEFAULT NOW()
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_slack_evento_em ON slack_evento_processado(processado_em)")

    _try("""
    CREATE TABLE IF NOT EXISTS slack_acao_pendente (
        id SERIAL PRIMARY KEY,
        token VARCHAR(40) NOT NULL UNIQUE,
        slack_user_id VARCHAR(30) NOT NULL,
        slack_channel_id VARCHAR(30),
        slack_message_ts VARCHAR(30),
        tipo_acao VARCHAR(50) NOT NULL,
        params_json TEXT NOT NULL,
        usuario_id INTEGER NOT NULL REFERENCES usuario(id),
        criado_em TIMESTAMP DEFAULT NOW(),
        executado_em TIMESTAMP,
        cancelado_em TIMESTAMP
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_slack_acao_token ON slack_acao_pendente(token)")
    _try("CREATE INDEX IF NOT EXISTS idx_slack_acao_em ON slack_acao_pendente(criado_em)")

    _try("""
    CREATE TABLE IF NOT EXISTS slack_conversa (
        id SERIAL PRIMARY KEY,
        slack_user_id VARCHAR(30) NOT NULL,
        slack_channel_id VARCHAR(30) NOT NULL,
        mensagens_json TEXT DEFAULT '[]',
        ultima_msg_em TIMESTAMP DEFAULT NOW(),
        UNIQUE (slack_user_id, slack_channel_id)
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_slack_conversa_uid ON slack_conversa(slack_user_id)")
    _try("CREATE INDEX IF NOT EXISTS idx_slack_conversa_em ON slack_conversa(ultima_msg_em)")

    # ── Lembrete de pedido pra amanha (opt-out por loja+data) ──
    _try("""
    CREATE TABLE IF NOT EXISTS lembrete_pedido_optout (
        id SERIAL PRIMARY KEY,
        loja_id INTEGER NOT NULL REFERENCES loja(id),
        data_entrega DATE NOT NULL,
        marcado_por_slack_uid VARCHAR(30),
        marcado_por_id INTEGER REFERENCES usuario(id),
        criado_em TIMESTAMP DEFAULT NOW(),
        UNIQUE (loja_id, data_entrega)
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_lembrete_optout_data ON lembrete_pedido_optout(data_entrega)")

    # Indices em tabelas que crescem por movimentacao — historico de estoque
    # e itens de pedido sao consultados muito por FK (estoque_loja_id,
    # estoque_producao_id, pedido_id) e ordenados por data. Sem indice,
    # cada listagem vira full-scan.
    _try("CREATE INDEX IF NOT EXISTS idx_mov_estoque_loja_el ON mov_estoque_loja(estoque_loja_id)")
    _try("CREATE INDEX IF NOT EXISTS idx_mov_estoque_loja_data ON mov_estoque_loja(data)")
    _try("CREATE INDEX IF NOT EXISTS idx_mov_estoque_producao_ep ON mov_estoque_producao(estoque_producao_id)")
    _try("CREATE INDEX IF NOT EXISTS idx_mov_estoque_producao_data ON mov_estoque_producao(data)")
    _try("CREATE INDEX IF NOT EXISTS idx_pedido_item_pedido ON pedido_item(pedido_id)")

    # B2B — venda da industria pra clientes externos. db.create_all cria as
    # tabelas no boot; aqui so adicionamos indices uteis e migracoes futuras.
    # Tabela preco_atacado foi criada por engano (preco ja existe em
    # Receita.preco_venda e Produto.preco_atacado). Dropa se existir.
    _try("DROP TABLE IF EXISTS preco_atacado")
    _try("CREATE INDEX IF NOT EXISTS idx_venda_b2b_data ON venda_b2b(data_venda)")
    _try("CREATE INDEX IF NOT EXISTS idx_venda_b2b_cliente ON venda_b2b(cliente_id)")
    _try("CREATE INDEX IF NOT EXISTS idx_venda_b2b_item_venda ON venda_b2b_item(venda_id)")
    _try("CREATE INDEX IF NOT EXISTS idx_venda_b2b_parcela_venda ON venda_b2b_parcela(venda_id)")
    _try("CREATE INDEX IF NOT EXISTS idx_venda_b2b_parcela_venc ON venda_b2b_parcela(vencimento)")

    # Remove o status intermediario 'pendente': pedido nasce 'confirmado'.
    # 'pendente' e 'confirmado' sempre foram o mesmo estado (mesmo rotulo/aba/
    # transicoes — ver app/constants.py). Idempotente.
    _try("UPDATE pedido_loja SET status = 'confirmado' WHERE status = 'pendente'")

    # Handshake QR Code — PIN da loja + tokens curtos por pedido.
    _try("ALTER TABLE loja ADD COLUMN IF NOT EXISTS pin VARCHAR(8)")
    _try("CREATE INDEX IF NOT EXISTS idx_pedido_qrcode_token ON pedido_qrcode(token)")
    _try("CREATE INDEX IF NOT EXISTS idx_pedido_qrcode_pedido ON pedido_qrcode(pedido_id)")
    # Auditoria de tentativas de handshake (scan + PIN) — investigar falhas.
    _try("""
    CREATE TABLE IF NOT EXISTS handshake_audit (
        id SERIAL PRIMARY KEY,
        momento TIMESTAMP DEFAULT NOW(),
        token VARCHAR(40),
        pedido_id INTEGER REFERENCES pedido_loja(id),
        tipo VARCHAR(10),
        etapa VARCHAR(20) NOT NULL,
        detalhe VARCHAR(500),
        status_pedido VARCHAR(20),
        ip VARCHAR(45),
        user_agent VARCHAR(300)
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_handshake_audit_pedido ON handshake_audit(pedido_id)")
    _try("CREATE INDEX IF NOT EXISTS idx_handshake_audit_momento ON handshake_audit(momento)")

    # Vendas manuais pra lojas sem API (Anesio): so alimenta previsao /
    # sugestao de pedido. db.create_all cria a tabela; aqui so indices.
    _try("CREATE INDEX IF NOT EXISTS idx_venda_manual_loja ON venda_manual_loja(loja_id)")
    _try("CREATE INDEX IF NOT EXISTS idx_venda_manual_data ON venda_manual_loja(data_venda)")

    # Cardapio digital: URL externa de imagem em receita + produto
    _try("ALTER TABLE receita ADD COLUMN IF NOT EXISTS imagem_url VARCHAR(400)")
    _try("ALTER TABLE produto ADD COLUMN IF NOT EXISTS imagem_url VARCHAR(400)")
    # BLOB upload (Rappi 403 forced this — admin sobe a foto direto)
    _try("ALTER TABLE receita ADD COLUMN IF NOT EXISTS imagem_blob BYTEA")
    _try("ALTER TABLE receita ADD COLUMN IF NOT EXISTS imagem_mimetype VARCHAR(50)")
    _try("ALTER TABLE produto ADD COLUMN IF NOT EXISTS imagem_blob BYTEA")
    _try("ALTER TABLE produto ADD COLUMN IF NOT EXISTS imagem_mimetype VARCHAR(50)")

    # Fase 2 contas a pagar (2026-06-10): conferencia humana — separa o que
    # um humano JA revisou do que e so extracao da IA. NULL = nao conferida.
    _try("ALTER TABLE conta_pagar ADD COLUMN IF NOT EXISTS revisada_em TIMESTAMP")
    _try("ALTER TABLE conta_pagar ADD COLUMN IF NOT EXISTS revisada_por_id INTEGER REFERENCES usuario(id)")

    # Arquivamento de receita (2026-06-10): receita com historico (pedidos,
    # vendas, estoque) NUNCA e excluida — arquivar tira ela das listas e
    # preserva o historico. NULL = ativa. ALTER vai NA FRENTE do modelo
    # (procedimento de 2 commits — ver CLAUDE.md "Schema migrations").
    _try("ALTER TABLE receita ADD COLUMN IF NOT EXISTS arquivada_em TIMESTAMP")
    _try("ALTER TABLE receita ADD COLUMN IF NOT EXISTS arquivada_por_id INTEGER REFERENCES usuario(id)")

    # Item de NF -> PRODUTO de revenda (2026-06-10): agua/refrigerante/etc
    # comprados prontos nao sao materia-prima — o mapeamento ganha o alvo
    # produto_id (espelha SeruProdutoMap, que ja tem os dois). ALTER na
    # frente do modelo (2 commits).
    _try("ALTER TABLE conta_pagar_item_map ADD COLUMN IF NOT EXISTS "
         "produto_id INTEGER REFERENCES produto(id)")
    _try("CREATE INDEX IF NOT EXISTS ix_conta_pagar_item_map_produto "
         "ON conta_pagar_item_map(produto_id)")

    # Tools usadas pelo chatbot do site antes do veredito (14/06/2026):
    # persistir a lista de ferramentas chamadas em cada decisao do bot
    # permite ao auditor distinguir handoff "preguicoso" (lista vazia ou
    # so transferir_para_humano) de handoff legitimo, e calcular a taxa
    # de contencao real. ALTER vai NA FRENTE do modelo (2 commits — ver
    # CLAUDE.md "Schema migrations").
    _try("ALTER TABLE vigia_veredito ADD COLUMN IF NOT EXISTS tools_usadas TEXT")

    # Reconhecimento de alerta no painel (banner + som). ALTER no mesmo
    # commit do modelo: _migrate roda no startup antes do gunicorn servir.
    _try("ALTER TABLE vigia_veredito ADD COLUMN IF NOT EXISTS "
         "reconhecido_em TIMESTAMP")
    _try("ALTER TABLE vigia_veredito ADD COLUMN IF NOT EXISTS "
         "reconhecido_por_id INTEGER REFERENCES usuario(id)")

    # Priority fee (gorjeta) da Lalamove pra acelerar a alocacao do
    # entregador. Coluna nova em lalamove_entrega (tabela criada via
    # db.create_all, que NAO altera tabela existente). ALTER no mesmo
    # commit do modelo eh seguro aqui: _migrate roda no startup ANTES do
    # gunicorn servir (app/__init__.py:423), entao a coluna existe antes
    # de qualquer SELECT. Dinheiro -> Numeric(10,2).
    _try("ALTER TABLE lalamove_entrega ADD COLUMN IF NOT EXISTS "
         "priority_fee NUMERIC(10, 2)")

    # Email do usuario (envio de senha/convite via Postmark, 16/06/2026).
    _try("ALTER TABLE usuario ADD COLUMN IF NOT EXISTS email VARCHAR(200)")

    # Destinatario diferente do pagador no pedido online (Fase 3+, 17/06/2026).
    # PedidoOnline e' tabela criada por db.create_all (Fase 3) -> em prod
    # ja existe sem as colunas; ALTER aplica em commit isolado.
    _try("ALTER TABLE pedido_online ADD COLUMN IF NOT EXISTS "
         "nome_destinatario VARCHAR(150)")
    _try("ALTER TABLE pedido_online ADD COLUMN IF NOT EXISTS "
         "telefone_destinatario VARCHAR(30)")

    # NF-e via Tiny (Fase 5, 17/06/2026): id do pedido/NF e status no Tiny.
    _try("ALTER TABLE pedido_online ADD COLUMN IF NOT EXISTS "
         "tiny_pedido_id VARCHAR(40)")
    _try("ALTER TABLE pedido_online ADD COLUMN IF NOT EXISTS "
         "tiny_nota_fiscal_id VARCHAR(40)")
    _try("ALTER TABLE pedido_online ADD COLUMN IF NOT EXISTS "
         "nf_status VARCHAR(40)")
    _try("ALTER TABLE pedido_online ADD COLUMN IF NOT EXISTS "
         "nf_emitida_em TIMESTAMP")
    _try("CREATE INDEX IF NOT EXISTS ix_pedido_online_nf "
         "ON pedido_online(tiny_nota_fiscal_id)")

    # Endereco estruturado pra NF-e (17/06/2026): a SEFAZ exige logradouro/
    # numero/bairro/cidade/uf SEPARADOS; antes so guardavamos a linha unica
    # em endereco_entrega e a nota saia com "endereco/bairro/cidade em branco".
    for _c, _t in (('endereco_logradouro', 'VARCHAR(200)'),
                   ('endereco_numero', 'VARCHAR(20)'),
                   ('endereco_complemento', 'VARCHAR(100)'),
                   ('endereco_bairro', 'VARCHAR(100)'),
                   ('endereco_cidade', 'VARCHAR(100)'),
                   ('endereco_uf', 'VARCHAR(2)')):
        _try(f"ALTER TABLE pedido_online ADD COLUMN IF NOT EXISTS {_c} {_t}")

    # Ordem manual na vitrine (17/06/2026): produto/receita ganham
    # `ordem_site`. NULL = vai pro fim alfabetico. Tabela `categoria_site`
    # eh criada por db.create_all (sem ALTER necessario).
    _try("ALTER TABLE produto ADD COLUMN IF NOT EXISTS ordem_site INTEGER")
    _try("ALTER TABLE receita ADD COLUMN IF NOT EXISTS ordem_site INTEGER")

    # Reserva de estoque (21/06/2026 — cutover loja propria, race condition):
    # `quantidade_reservada` segura saldo entre checkout e webhook pagar.me
    # pago (Pix expira 30min). Catalogo expoe `quantidade - quantidade_reservada`
    # como disponivel. NOT NULL DEFAULT 0 — todas as linhas existentes ficam
    # 0 (nada reservado retroativamente). `reserva_expira_em` no pedido
    # dispara liberacao via cron quando o cliente abandona o checkout.
    # ALTER no mesmo commit do modelo: _migrate roda no startup do gunicorn
    # ANTES de aceitar request, entao a coluna existe quando o codigo SELECT
    # nela. Padrao igual ao priority_fee (linha 1106) e usuario.email (1110).
    _try("ALTER TABLE estoque_loja ADD COLUMN IF NOT EXISTS "
         "quantidade_reservada INTEGER NOT NULL DEFAULT 0")
    _try("ALTER TABLE pedido_online ADD COLUMN IF NOT EXISTS "
         "reserva_expira_em TIMESTAMP")
    _try("CREATE INDEX IF NOT EXISTS ix_pedido_online_reserva_expira "
         "ON pedido_online(reserva_expira_em) "
         "WHERE reserva_expira_em IS NOT NULL")

    # Motivo do cancelamento (25/06/2026) — registra POR QUE um pedido do site
    # foi cancelado (pix_expirado / reembolso / cancelado_admin) em vez de
    # deduzir pelos timestamps. Coluna nullable; pedidos cancelados antes desta
    # coluna ficam NULL e a UI infere o motivo pelos timestamps.
    _try("ALTER TABLE pedido_online ADD COLUMN IF NOT EXISTS "
         "motivo_cancelamento VARCHAR(40)")

    # Orcamento B2B (22/06/2026) — data prevista de entrega + frete. A tabela
    # `orcamento` eh criada por db.create_all no mesmo deploy; estes ALTER
    # cobrem o caso de ela ter sido criada em deploy anterior sem as colunas.
    _try("ALTER TABLE orcamento ADD COLUMN IF NOT EXISTS data_entrega DATE")
    _try("ALTER TABLE orcamento ADD COLUMN IF NOT EXISTS "
         "frete_valor NUMERIC(10, 2) NOT NULL DEFAULT 0")

    # Descricao SEO (22/06/2026) — Receita nao tinha campo de descricao;
    # Produto tinha `descricao` curta. `descricao_seo` (TEXT) eh o que vira
    # publico na vitrine/JSON-LD/meta description.
    _try("ALTER TABLE receita ADD COLUMN IF NOT EXISTS descricao_seo TEXT")
    _try("ALTER TABLE produto ADD COLUMN IF NOT EXISTS descricao_seo TEXT")

    # Plano de estoque do site por dia (22/06/2026). NAO sai por db.create_all
    # em ambientes ja existentes (so cria tabela nova; nao toca em prod).
    # Tabela canonica vem do modelo `EstoqueSitePlano`.
    _try("""
        CREATE TABLE IF NOT EXISTS estoque_site_plano (
            id SERIAL PRIMARY KEY,
            kind VARCHAR(10) NOT NULL,
            item_id INTEGER NOT NULL,
            data DATE NOT NULL,
            qtd_planejada INTEGER NOT NULL DEFAULT 0,
            qtd_reservada INTEGER NOT NULL DEFAULT 0,
            criado_em TIMESTAMP,
            atualizado_em TIMESTAMP,
            CONSTRAINT uq_estoque_site_plano_item_data
                UNIQUE (kind, item_id, data)
        )
    """)
    _try("""
        CREATE INDEX IF NOT EXISTS ix_estoque_site_plano_data
            ON estoque_site_plano(data)
    """)
    _try("""
        CREATE INDEX IF NOT EXISTS ix_estoque_site_plano_data_kind_item
            ON estoque_site_plano(data, kind, item_id)
    """)

    # Backfill de tokens em drivers existentes (sem token)
    try:
        import secrets

        from app.models import Driver
        sem_token = Driver.query.filter(
            (Driver.token == None) | (Driver.token == '')  # noqa: E711
        ).all()
        for drv in sem_token:
            drv.token = secrets.token_urlsafe(16)
        if sem_token:
            db.session.commit()
    except Exception as e:
        app.logger.warning('backfill token driver falhou: %s', e)
        db.session.rollback()


def _migrate_sqlite(app):
    """Adiciona colunas novas no SQLite."""
    import sqlite3
    uri = app.config['SQLALCHEMY_DATABASE_URI'].replace('sqlite:///', '')
    conn = sqlite3.connect(uri)
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(receita)")
    colunas = [row[1] for row in cursor.fetchall()]
    if 'descricao_seo' not in colunas:
        cursor.execute("ALTER TABLE receita ADD COLUMN descricao_seo TEXT")
    cursor.execute("PRAGMA table_info(produto)")
    cols_prod = [row[1] for row in cursor.fetchall()]
    if 'descricao_seo' not in cols_prod:
        cursor.execute("ALTER TABLE produto ADD COLUMN descricao_seo TEXT")
    if 'perda_percentual' not in colunas:
        cursor.execute("ALTER TABLE receita ADD COLUMN perda_percentual REAL DEFAULT 0")
    if 'preco_loja' not in colunas:
        cursor.execute("ALTER TABLE receita ADD COLUMN preco_loja REAL")
    if 'preco_site' not in colunas:
        cursor.execute("ALTER TABLE receita ADD COLUMN preco_site REAL")
    if 'preco_interno' not in colunas:
        cursor.execute("ALTER TABLE receita ADD COLUMN preco_interno REAL")
    if 'custo_embalagem' not in colunas:
        cursor.execute("ALTER TABLE receita ADD COLUMN custo_embalagem REAL DEFAULT 0")
    if 'imagem_url' not in colunas:
        cursor.execute("ALTER TABLE receita ADD COLUMN imagem_url VARCHAR(400)")
    if 'imagem_blob' not in colunas:
        cursor.execute("ALTER TABLE receita ADD COLUMN imagem_blob BLOB")
    if 'imagem_mimetype' not in colunas:
        cursor.execute("ALTER TABLE receita ADD COLUMN imagem_mimetype VARCHAR(50)")
    if 'arquivada_em' not in colunas:
        cursor.execute("ALTER TABLE receita ADD COLUMN arquivada_em TIMESTAMP")
    if 'arquivada_por_id' not in colunas:
        cursor.execute(
            "ALTER TABLE receita ADD COLUMN arquivada_por_id INTEGER REFERENCES usuario(id)")
    if 'dias_producao' not in colunas:
        cursor.execute(
            "ALTER TABLE receita ADD COLUMN dias_producao INTEGER NOT NULL DEFAULT 0")
    if 'capacidade_amassadeira_g' not in colunas:
        cursor.execute("ALTER TABLE receita ADD COLUMN "
                       "capacidade_amassadeira_g INTEGER NOT NULL DEFAULT 50000")
    if 'sugerir_pedido_loja' not in colunas:
        cursor.execute("ALTER TABLE receita ADD COLUMN "
                       "sugerir_pedido_loja BOOLEAN NOT NULL DEFAULT 1")
    if 'lote_pedido' not in colunas:
        cursor.execute("ALTER TABLE receita ADD COLUMN lote_pedido INTEGER")
    if 'minimo_pedido' not in colunas:
        cursor.execute("ALTER TABLE receita ADD COLUMN minimo_pedido INTEGER")

    cursor.execute("PRAGMA table_info(conta_pagar_item_map)")
    cols_cpim = [row[1] for row in cursor.fetchall()]
    if cols_cpim and 'produto_id' not in cols_cpim:
        cursor.execute("ALTER TABLE conta_pagar_item_map ADD COLUMN "
                       "produto_id INTEGER REFERENCES produto(id)")

    cursor.execute("PRAGMA table_info(vigia_veredito)")
    cols_vv = [row[1] for row in cursor.fetchall()]
    if cols_vv and 'tools_usadas' not in cols_vv:
        cursor.execute("ALTER TABLE vigia_veredito ADD COLUMN tools_usadas TEXT")
    if cols_vv and 'reconhecido_em' not in cols_vv:
        cursor.execute("ALTER TABLE vigia_veredito ADD COLUMN reconhecido_em TIMESTAMP")
    if cols_vv and 'reconhecido_por_id' not in cols_vv:
        cursor.execute("ALTER TABLE vigia_veredito ADD COLUMN "
                       "reconhecido_por_id INTEGER REFERENCES usuario(id)")

    # Priority fee (gorjeta) da Lalamove — coluna nova em lalamove_entrega.
    # Em SQLite novo, create_all ja cria com a coluna; aqui cobre bancos
    # locais antigos. Guarda contra a tabela nao existir ainda.
    cursor.execute("PRAGMA table_info(lalamove_entrega)")
    cols_lala = [row[1] for row in cursor.fetchall()]
    if cols_lala and 'priority_fee' not in cols_lala:
        cursor.execute("ALTER TABLE lalamove_entrega ADD COLUMN "
                       "priority_fee NUMERIC(10, 2)")

    # Email do usuario (envio de senha/convite via Postmark, 16/06/2026).
    cursor.execute("PRAGMA table_info(usuario)")
    cols_user = [row[1] for row in cursor.fetchall()]
    if cols_user and 'email' not in cols_user:
        cursor.execute("ALTER TABLE usuario ADD COLUMN email VARCHAR(200)")

    # Migração tabela receita_ingrediente
    cursor.execute("PRAGMA table_info(receita_ingrediente)")
    cols_ing = [row[1] for row in cursor.fetchall()]
    if cols_ing and 'tipo' not in cols_ing:
        cursor.execute("ALTER TABLE receita_ingrediente ADD COLUMN tipo TEXT DEFAULT 'mp'")
    # FK pra sub-receita (espelha o Postgres) + backfill por nome (idempotente).
    if cols_ing and 'sub_receita_id' not in cols_ing:
        cursor.execute("ALTER TABLE receita_ingrediente ADD COLUMN sub_receita_id INTEGER")
    cursor.execute("""
        UPDATE receita_ingrediente SET sub_receita_id = (
            SELECT r.id FROM receita r
            WHERE lower(trim(r.nome)) = lower(trim(receita_ingrediente.ingrediente_nome))
            LIMIT 1)
        WHERE tipo = 'receita' AND sub_receita_id IS NULL
    """)

    # Migração tabela produto
    cursor.execute("PRAGMA table_info(produto)")
    cols_prod = [row[1] for row in cursor.fetchall()]
    if cols_prod and 'custo_direto' not in cols_prod:
        cursor.execute("ALTER TABLE produto ADD COLUMN custo_direto REAL")
    if cols_prod and 'custo_embalagem' not in cols_prod:
        cursor.execute("ALTER TABLE produto ADD COLUMN custo_embalagem REAL DEFAULT 0")
    if cols_prod and 'preco_interno' not in cols_prod:
        cursor.execute("ALTER TABLE produto ADD COLUMN preco_interno REAL")
    if cols_prod and 'imagem_url' not in cols_prod:
        cursor.execute("ALTER TABLE produto ADD COLUMN imagem_url VARCHAR(400)")
    if cols_prod and 'imagem_blob' not in cols_prod:
        cursor.execute("ALTER TABLE produto ADD COLUMN imagem_blob BLOB")
    if cols_prod and 'imagem_mimetype' not in cols_prod:
        cursor.execute("ALTER TABLE produto ADD COLUMN imagem_mimetype VARCHAR(50)")
    if cols_prod and 'modo_preparo' not in cols_prod:
        cursor.execute("ALTER TABLE produto ADD COLUMN modo_preparo TEXT")

    # Migração receita.modo_preparo
    if 'modo_preparo' not in colunas:
        cursor.execute("ALTER TABLE receita ADD COLUMN modo_preparo TEXT")
    if 'observacao' not in colunas:
        cursor.execute("ALTER TABLE receita ADD COLUMN observacao TEXT")
    if 'estado_padrao' not in colunas:
        cursor.execute("ALTER TABLE receita ADD COLUMN estado_padrao VARCHAR(20)")

    # Brioche entra no pre-preparo como assado por default (idempotente).
    cursor.execute(
        "UPDATE receita SET estado_padrao='assado' "
        "WHERE LOWER(nome) LIKE '%brioche%' AND estado_padrao IS NULL"
    )

    # Migração produto.observacao
    if cols_prod and 'observacao' not in cols_prod:
        cursor.execute("ALTER TABLE produto ADD COLUMN observacao TEXT")

    # Migração funcionario
    cursor.execute("PRAGMA table_info(funcionario)")
    cols_func = [row[1] for row in cursor.fetchall()]
    if cols_func and 'funcao_operacional' not in cols_func:
        cursor.execute("ALTER TABLE funcionario ADD COLUMN funcao_operacional VARCHAR(100)")
    if cols_func and 'periodo' not in cols_func:
        cursor.execute("ALTER TABLE funcionario ADD COLUMN periodo VARCHAR(20)")
    if cols_func and 'cadastro_pendente' not in cols_func:
        cursor.execute("ALTER TABLE funcionario ADD COLUMN cadastro_pendente BOOLEAN DEFAULT 0")
    if cols_func and 'data_nascimento' not in cols_func:
        cursor.execute("ALTER TABLE funcionario ADD COLUMN data_nascimento DATE")
    if cols_func and 'horas_extras' not in cols_func:
        cursor.execute("ALTER TABLE funcionario ADD COLUMN horas_extras REAL DEFAULT 0")
    if cols_func and 'tem_cargo_confianca' not in cols_func:
        cursor.execute("ALTER TABLE funcionario ADD COLUMN tem_cargo_confianca BOOLEAN DEFAULT 0")
        cursor.execute("UPDATE funcionario SET tem_cargo_confianca = 1 WHERE cargo_confianca > 0")
    if cols_func and 'cargo_id' not in cols_func:
        cursor.execute("ALTER TABLE funcionario ADD COLUMN cargo_id INTEGER REFERENCES cargo(id)")
        cursor.execute("""
            INSERT OR IGNORE INTO cargo (nome, salario_base, ativo)
            SELECT funcao, MAX(salario_base), 1
            FROM funcionario
            WHERE funcao IS NOT NULL AND TRIM(funcao) <> ''
            GROUP BY funcao
        """)
        cursor.execute("""
            UPDATE funcionario SET cargo_id = (
                SELECT id FROM cargo WHERE cargo.nome = funcionario.funcao
            ) WHERE funcao IS NOT NULL AND TRIM(funcao) <> ''
        """)

    # Migração loja
    cursor.execute("PRAGMA table_info(loja)")
    cols_loja = [row[1] for row in cursor.fetchall()]
    if cols_loja and 'planta_imagem' not in cols_loja:
        cursor.execute("ALTER TABLE loja ADD COLUMN planta_imagem BLOB")
    if cols_loja and 'planta_mimetype' not in cols_loja:
        cursor.execute("ALTER TABLE loja ADD COLUMN planta_mimetype VARCHAR(100)")
    if cols_loja and 'pin' not in cols_loja:
        cursor.execute("ALTER TABLE loja ADD COLUMN pin VARCHAR(8)")

    # Migração slot_mapa
    cursor.execute("PRAGMA table_info(slot_mapa)")
    cols_slot = [row[1] for row in cursor.fetchall()]
    if cols_slot and 'largura' not in cols_slot:
        cursor.execute("ALTER TABLE slot_mapa ADD COLUMN largura REAL DEFAULT 15")
    if cols_slot and 'altura' not in cols_slot:
        cursor.execute("ALTER TABLE slot_mapa ADD COLUMN altura REAL DEFAULT 8")

    # Migração usuario.loja_id
    cursor.execute("PRAGMA table_info(usuario)")
    cols_user = [row[1] for row in cursor.fetchall()]
    if cols_user and 'loja_id' not in cols_user:
        cursor.execute("ALTER TABLE usuario ADD COLUMN loja_id INTEGER REFERENCES loja(id)")

    # Migração posicao.origem
    cursor.execute("PRAGMA table_info(posicao)")
    cols_pos = [row[1] for row in cursor.fetchall()]
    if cols_pos and 'origem' not in cols_pos:
        cursor.execute("ALTER TABLE posicao ADD COLUMN origem VARCHAR(10) DEFAULT 'manual'")
        cursor.execute(
            "UPDATE posicao SET origem = 'mapa' WHERE EXISTS ("
            "  SELECT 1 FROM slot_mapa WHERE slot_mapa.loja_id = posicao.loja_id "
            "  AND slot_mapa.nome = posicao.nome_posicao)"
        )

    # Migração materia_prima.estoque_atual + peso_unidade
    cursor.execute("PRAGMA table_info(materia_prima)")
    cols_mp = [row[1] for row in cursor.fetchall()]
    if cols_mp and 'estoque_atual' not in cols_mp:
        cursor.execute("ALTER TABLE materia_prima ADD COLUMN estoque_atual REAL DEFAULT 0")
    if cols_mp and 'peso_unidade' not in cols_mp:
        cursor.execute("ALTER TABLE materia_prima ADD COLUMN peso_unidade REAL")

    # Migração pedido_item.quantidade_recebida + materia_prima_id
    cursor.execute("PRAGMA table_info(pedido_item)")
    cols_pi = [row[1] for row in cursor.fetchall()]
    if cols_pi and 'quantidade_recebida' not in cols_pi:
        cursor.execute("ALTER TABLE pedido_item ADD COLUMN quantidade_recebida INTEGER")
    if cols_pi and 'materia_prima_id' not in cols_pi:
        cursor.execute("ALTER TABLE pedido_item ADD COLUMN materia_prima_id INTEGER REFERENCES materia_prima(id)")

    # Migração estoque_loja.materia_prima_id
    cursor.execute("PRAGMA table_info(estoque_loja)")
    cols_el = [row[1] for row in cursor.fetchall()]
    if cols_el and 'materia_prima_id' not in cols_el:
        cursor.execute("ALTER TABLE estoque_loja ADD COLUMN materia_prima_id INTEGER REFERENCES materia_prima(id)")

    # Migração usuario.is_owner
    cursor.execute("PRAGMA table_info(usuario)")
    cols_user2 = [row[1] for row in cursor.fetchall()]
    if cols_user2 and 'is_owner' not in cols_user2:
        cursor.execute("ALTER TABLE usuario ADD COLUMN is_owner BOOLEAN DEFAULT 0")
        cursor.execute(
            "UPDATE usuario SET is_owner = 1 WHERE id = "
            "(SELECT id FROM usuario WHERE papel = 'admin' ORDER BY id LIMIT 1)"
        )

    # Migracao papel_v1: introduz niveis. Roda uma vez.
    try:
        cursor.execute("CREATE TABLE IF NOT EXISTS migracao_marker (nome VARCHAR(50) PRIMARY KEY, executado_em DATETIME DEFAULT CURRENT_TIMESTAMP)")
        cursor.execute("SELECT 1 FROM migracao_marker WHERE nome='papel_v1'")
        ja_rodou = cursor.fetchone() is not None
        if not ja_rodou:
            cursor.execute("UPDATE usuario SET papel='funcionario' WHERE papel='admin' AND (is_owner IS NULL OR is_owner = 0)")
            cursor.execute("UPDATE usuario SET papel='admin' WHERE is_owner = 1 AND papel <> 'admin'")
            cursor.execute("INSERT INTO migracao_marker (nome) VALUES ('papel_v1')")
    except Exception:
        pass

    # Migração projeto_area.cor
    cursor.execute("PRAGMA table_info(projeto_area)")
    cols_pa = [row[1] for row in cursor.fetchall()]
    if cols_pa and 'cor' not in cols_pa:
        cursor.execute("ALTER TABLE projeto_area ADD COLUMN cor VARCHAR(20)")

    # Migração tarefa_projeto.observacao + recorrencia
    cursor.execute("PRAGMA table_info(tarefa_projeto)")
    cols_tp = [row[1] for row in cursor.fetchall()]
    if cols_tp and 'observacao' not in cols_tp:
        cursor.execute("ALTER TABLE tarefa_projeto ADD COLUMN observacao TEXT")
    if cols_tp and 'recorrencia' not in cols_tp:
        cursor.execute("ALTER TABLE tarefa_projeto ADD COLUMN recorrencia VARCHAR(20)")

    # estoque_producao.nome_pendente
    cursor.execute("PRAGMA table_info(estoque_producao)")
    cols_ep = [row[1] for row in cursor.fetchall()]
    if cols_ep and 'nome_pendente' not in cols_ep:
        cursor.execute("ALTER TABLE estoque_producao ADD COLUMN nome_pendente VARCHAR(200)")

    # estoque_loja.nome_pendente
    cursor.execute("PRAGMA table_info(estoque_loja)")
    cols_el = [row[1] for row in cursor.fetchall()]
    if cols_el and 'nome_pendente' not in cols_el:
        cursor.execute("ALTER TABLE estoque_loja ADD COLUMN nome_pendente VARCHAR(200)")
    # Reserva de estoque (21/06/2026 — race condition no cutover).
    if cols_el and 'quantidade_reservada' not in cols_el:
        cursor.execute("ALTER TABLE estoque_loja ADD COLUMN "
                       "quantidade_reservada INTEGER NOT NULL DEFAULT 0")

    cursor.execute("PRAGMA table_info(pedido_online)")
    cols_po = [row[1] for row in cursor.fetchall()]
    if cols_po and 'reserva_expira_em' not in cols_po:
        cursor.execute("ALTER TABLE pedido_online ADD COLUMN "
                       "reserva_expira_em TIMESTAMP")
    if cols_po and 'motivo_cancelamento' not in cols_po:
        cursor.execute("ALTER TABLE pedido_online ADD COLUMN "
                       "motivo_cancelamento VARCHAR(40)")

    # seru_produto_map.fator_quantidade
    cursor.execute("PRAGMA table_info(seru_produto_map)")
    cols_spm = [row[1] for row in cursor.fetchall()]
    if cols_spm and 'fator_quantidade' not in cols_spm:
        cursor.execute("ALTER TABLE seru_produto_map ADD COLUMN fator_quantidade REAL NOT NULL DEFAULT 1.0")

    # conta_pagar: conferencia humana (Fase 2, 2026-06-10)
    cursor.execute("PRAGMA table_info(conta_pagar)")
    cols_cp = [row[1] for row in cursor.fetchall()]
    if cols_cp and 'revisada_em' not in cols_cp:
        cursor.execute("ALTER TABLE conta_pagar ADD COLUMN revisada_em TIMESTAMP")
    if cols_cp and 'revisada_por_id' not in cols_cp:
        cursor.execute("ALTER TABLE conta_pagar ADD COLUMN revisada_por_id INTEGER REFERENCES usuario(id)")

    # Status intermediario 'pendente' vira 'confirmado' direto (idempotente).
    try:
        cursor.execute("UPDATE pedido_loja SET status='confirmado' WHERE status='pendente'")
    except sqlite3.OperationalError:
        pass

    # Cronograma -> padeiro: alvo/produzido por item + origem do plano.
    cursor.execute("PRAGMA table_info(planejamento_item)")
    cols_pi = [row[1] for row in cursor.fetchall()]
    if cols_pi and 'qtd_alvo' not in cols_pi:
        cursor.execute("ALTER TABLE planejamento_item ADD COLUMN qtd_alvo INTEGER")
    if cols_pi and 'produzido_qtd' not in cols_pi:
        cursor.execute("ALTER TABLE planejamento_item ADD COLUMN "
                       "produzido_qtd INTEGER NOT NULL DEFAULT 0")
    cursor.execute("PRAGMA table_info(planejamento_producao)")
    cols_pp = [row[1] for row in cursor.fetchall()]
    if cols_pp and 'origem' not in cols_pp:
        cursor.execute("ALTER TABLE planejamento_producao ADD COLUMN origem VARCHAR(20)")
    if cols_pp and 'enviado_ao_padeiro' not in cols_pp:
        cursor.execute("ALTER TABLE planejamento_producao "
                       "ADD COLUMN enviado_ao_padeiro BOOLEAN DEFAULT 1")

    conn.commit()
    conn.close()
