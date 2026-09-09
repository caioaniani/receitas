"""Audit log NAO pode carregar relationships pra detectar mudanca (09/09/2026).

Achado do Sentry GESTAO-PADARIA-37 (N+1 em `loja.webhook_pagarme`):
`audit._capture_changes` chamava `load_history()` em TODOS os atributos do
objeto sujo — inclusive relationships. Num atributo nao carregado isso emite o
lazy load inteiro: pra `EstoqueLoja.movimentacoes` era um SELECT de TODO o
historico de movimentos da linha a CADA flush que mexia no estoque (webhook
do site, sync do Seru, lote, desperdicio...). O custo crescia com o tamanho
do historico e nao servia pra nada — a auditoria so serializa colunas.

Estes testes travam: (1) atualizar uma linha de estoque com historico NAO
consulta `mov_estoque_loja`; (2) a mudanca de coluna continua auditada.
"""
import json

from sqlalchemy import event


def _contar_selects_mov(db, fn):
    """Executa `fn()` contando SELECTs em mov_estoque_loja no engine."""
    vistos = []

    def _listener(conn, cursor, statement, parameters, context, executemany):
        if 'FROM mov_estoque_loja' in statement:
            vistos.append(statement)

    event.listen(db.engine, 'before_cursor_execute', _listener)
    try:
        fn()
    finally:
        event.remove(db.engine, 'before_cursor_execute', _listener)
    return vistos


def _linha_com_historico(db, n_movs=3):
    from app.models import EstoqueLoja, Loja, MovEstoqueLoja, Produto
    loja = Loja(nome='Loja A', ativa=True, endereco='Rua A, 1')
    prod = Produto(nome='Pao', categoria='Paes', preco_site=10.0, ativo=True)
    db.session.add_all([loja, prod])
    db.session.flush()
    el = EstoqueLoja(loja_id=loja.id, produto_id=prod.id, quantidade=10)
    db.session.add(el)
    db.session.flush()
    for i in range(n_movs):
        db.session.add(MovEstoqueLoja(estoque_loja_id=el.id, tipo='entrada',
                                      quantidade=1, referencia=f'mov {i}'))
    db.session.commit()
    return el.id


def test_update_de_estoque_nao_carrega_movimentacoes(app):
    from app.extensions import db
    from app.models import EstoqueLoja
    with app.app_context():
        el_id = _linha_com_historico(db)
        db.session.expire_all()  # colecao `movimentacoes` NAO carregada
        el = db.session.get(EstoqueLoja, el_id)

        def _baixar():
            el.quantidade = (el.quantidade or 0) - 1
            db.session.flush()

        selects = _contar_selects_mov(db, _baixar)
        assert selects == [], (
            'audit carregou mov_estoque_loja ao auditar um UPDATE de '
            f'estoque_loja: {selects}')
        db.session.commit()


def test_mudanca_de_coluna_continua_auditada(app):
    from app.extensions import db
    from app.models import AuditLog, EstoqueLoja
    with app.app_context():
        el_id = _linha_com_historico(db)
        db.session.expire_all()
        el = db.session.get(EstoqueLoja, el_id)
        el.quantidade = 7
        db.session.commit()

        log = (AuditLog.query.filter_by(tabela='estoque_loja', acao='update',
                                        registro_id=el_id)
               .order_by(AuditLog.id.desc()).first())
        assert log is not None
        antes, depois = json.loads(log.antes), json.loads(log.depois)
        assert antes['quantidade'] == 10
        assert depois['quantidade'] == 7
        # Relationship nao mexida NAO aparece (nem carrega).
        assert 'movimentacoes' not in depois


def test_atribuicao_de_relationship_segue_auditada_sem_load(app):
    """`mov.estoque = outra_linha` (consolidacao em `obter_linha_loja`) e
    mudanca de FK via relationship: continua na trilha, sem SELECT extra da
    colecao da linha destino."""
    from app.extensions import db
    from app.models import AuditLog, EstoqueLoja, MovEstoqueLoja
    with app.app_context():
        el_a = _linha_com_historico(db, n_movs=1)
        el_b = _linha_com_historico(db, n_movs=0)
        db.session.expire_all()
        mov = MovEstoqueLoja.query.filter_by(estoque_loja_id=el_a).first()
        destino = db.session.get(EstoqueLoja, el_b)

        def _reatribuir():
            mov.estoque = destino
            db.session.flush()

        selects = _contar_selects_mov(db, _reatribuir)
        # A unica leitura tolerada e a do proprio `mov` (ja carregado acima) —
        # nenhuma pela auditoria.
        assert selects == [], selects
        db.session.commit()
        log = (AuditLog.query.filter_by(tabela='mov_estoque_loja', acao='update',
                                        registro_id=mov.id)
               .order_by(AuditLog.id.desc()).first())
        assert log is not None
        assert 'estoque' in json.loads(log.depois)
