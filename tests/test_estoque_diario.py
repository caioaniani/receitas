"""Testa o relatorio diario de estoque (le dos movimentos, nao altera nada)."""


def _setup(db):
    from app.models import EstoqueLoja, Loja, Receita
    loja = Loja(nome='Ribeiro do Vale', ativa=True)
    receita = Receita(nome='Pao Frances', categoria='Paes', rendimento_qtd=1,
                      rendimento_unidade='un', peso_base=100.0)
    db.session.add_all([loja, receita])
    db.session.flush()
    el = EstoqueLoja(loja_id=loja.id, receita_id=receita.id, quantidade=7)
    db.session.add(el)
    db.session.commit()
    return loja, receita, el


def test_relatorio_le_movimentos_do_dia(app):
    """Comecou o dia com 0, entrou 10, vendeu 3 (Seru) → atual 7."""
    from app.extensions import db
    from app.models import MovEstoqueLoja
    from app.services import estoque_diario
    from app.utils import agora
    from app.utils import hoje as hoje_brt

    with app.app_context():
        loja, receita, el = _setup(db)
        db.session.add_all([
            MovEstoqueLoja(estoque_loja_id=el.id, tipo='entrada_pedido',
                           quantidade=10, data=agora()),
            MovEstoqueLoja(estoque_loja_id=el.id, tipo='venda_seru',
                           quantidade=3, data=agora()),
        ])
        db.session.commit()

        linhas = estoque_diario.relatorio_diario(loja.id, hoje_brt())

    assert len(linhas) == 1
    x = linhas[0]
    assert x['estoque_atual'] == 7
    assert x['entradas'] == 10
    assert x['baixas'] == 3
    assert x['estoque_inicio'] == 0  # 7 - (10 - 3)
    # Detalhe por fonte
    assert x['baixas_por_fonte'] == [
        {'tipo': 'venda_seru', 'label': 'PDV (Seru)', 'quantidade': 3}]


def test_baixas_de_fontes_diferentes_somam_e_listam(app):
    """Vende no Seru e dá ajuste manual → detalhe separa as fontes."""
    from app.extensions import db
    from app.models import MovEstoqueLoja
    from app.services import estoque_diario
    from app.utils import agora
    from app.utils import hoje as hoje_brt

    with app.app_context():
        loja, receita, el = _setup(db)  # atual = 7
        db.session.add_all([
            MovEstoqueLoja(estoque_loja_id=el.id, tipo='venda_seru',
                           quantidade=2, data=agora()),
            MovEstoqueLoja(estoque_loja_id=el.id, tipo='ajuste_negativo',
                           quantidade=1, data=agora()),
        ])
        db.session.commit()
        linhas = estoque_diario.relatorio_diario(loja.id, hoje_brt())

    x = linhas[0]
    assert x['baixas'] == 3            # 2 + 1
    assert x['estoque_inicio'] == 10   # 7 + 3 baixados, 0 entradas
    fontes = {f['tipo']: f['quantidade'] for f in x['baixas_por_fonte']}
    assert fontes == {'venda_seru': 2, 'ajuste_negativo': 1}


def test_conferencia_usa_sinal_da_quantidade(app):
    """ajuste_conferencia negativo conta como baixa (quantidade ja tem sinal)."""
    from app.extensions import db
    from app.models import MovEstoqueLoja
    from app.services import estoque_diario
    from app.utils import agora
    from app.utils import hoje as hoje_brt

    with app.app_context():
        loja, receita, el = _setup(db)  # atual = 7
        # conferencia tirou 2 (sistema 9 → real 7)
        db.session.add(MovEstoqueLoja(estoque_loja_id=el.id,
                                      tipo='ajuste_conferencia',
                                      quantidade=-2, data=agora()))
        db.session.commit()
        linhas = estoque_diario.relatorio_diario(loja.id, hoje_brt())

    x = linhas[0]
    assert x['baixas'] == 2
    assert x['estoque_inicio'] == 9    # 7 + 2


def test_sem_estoque_nao_conta_como_baixa(app):
    """venda_seru_sem_estoque registra falta mas NAO mexe no saldo."""
    from app.extensions import db
    from app.models import MovEstoqueLoja
    from app.services import estoque_diario
    from app.utils import agora
    from app.utils import hoje as hoje_brt

    with app.app_context():
        loja, receita, el = _setup(db)  # atual = 7
        db.session.add(MovEstoqueLoja(estoque_loja_id=el.id,
                                      tipo='venda_seru_sem_estoque',
                                      quantidade=5, data=agora()))
        db.session.commit()
        linhas = estoque_diario.relatorio_diario(loja.id, hoje_brt())

    x = linhas[0]
    assert x['baixas'] == 0
    assert x['estoque_inicio'] == 7   # nao mudou


def test_rota_renderiza(app, admin_user):
    from app.extensions import db
    with app.app_context():
        _setup(db)
    c = app.test_client()
    c.post('/auth/login', data={'login': 'admin', 'senha': '123'})
    r = c.get('/pedidos/estoque-loja/diario')
    assert r.status_code == 200
    assert b'Movimento do dia' in r.data
