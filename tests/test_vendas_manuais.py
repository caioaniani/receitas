"""Smoke tests vendas manuais + sugestao de pedido."""
from datetime import date, datetime, timedelta


def test_aplicar_vendas_manuais_nao_baixa_estoque(app, admin_user, loja, catalogo):
    """VendaManualLoja grava historico mas EstoqueLoja fica intacto."""
    from app.extensions import db
    from app.models import EstoqueLoja, VendaManualLoja
    from app.services import vendas_manuais as svc

    el = EstoqueLoja(loja_id=loja.id, receita_id=catalogo['receita'].id,
                     quantidade=20)
    db.session.add(el)
    db.session.commit()

    parseados = svc.parsear_lista('Croissant Tradicional: 7')
    resolvidos = svc.resolver_lista(parseados, loja.id)
    resultado = svc.aplicar_vendas_manuais(resolvidos, loja.id,
                                             date.today(), admin_user)
    assert len(resultado['aplicados']) == 1
    assert resultado['aplicados'][0]['quantidade'] == 7

    # Estoque NAO foi mexido
    db.session.refresh(el)
    assert el.quantidade == 20

    # VendaManualLoja foi criada
    vendas = VendaManualLoja.query.filter_by(loja_id=loja.id).all()
    assert len(vendas) == 1
    assert vendas[0].quantidade == 7
    assert vendas[0].receita_id == catalogo['receita'].id


def test_sugerir_pedido_combina_real_e_manual(app, admin_user, loja, catalogo):
    """Sugestao soma MovEstoqueLoja venda_vnda + VendaManualLoja no
    intervalo data_inicio..data_fim, e separa por fonte."""
    from app.extensions import db
    from app.models import EstoqueLoja, MovEstoqueLoja, VendaManualLoja
    from app.services import vendas_manuais as svc

    el = EstoqueLoja(loja_id=loja.id, receita_id=catalogo['receita'].id,
                     quantidade=5)
    db.session.add(el)
    db.session.flush()

    hoje_ = date.today()
    inicio = hoje_ - timedelta(days=13)
    fim = hoje_

    # 7 vendas reais via vnda dentro do periodo
    for i in range(7):
        db.session.add(MovEstoqueLoja(
            estoque_loja_id=el.id, tipo='venda_vnda', quantidade=2,
            data=datetime.combine(hoje_ - timedelta(days=i + 1),
                                   datetime.min.time()),
        ))
    # 7 vendas manuais dentro do periodo
    for i in range(7):
        db.session.add(VendaManualLoja(
            loja_id=loja.id, receita_id=catalogo['receita'].id,
            data_venda=hoje_ - timedelta(days=i),
            quantidade=1,
        ))
    db.session.commit()

    out = svc.sugerir_pedido(loja.id, data_inicio=inicio,
                              data_fim=fim, dias_cobertura=7)
    assert len(out) == 1
    item = out[0]
    # Total: 7*2 (vnda) + 7*1 (manual) = 21 vendas / 14 dias = 1.5/dia
    assert item['vendas_periodo'] == 21
    assert abs(item['media_diaria'] - 1.5) < 0.01
    # Ideal: ceil(1.5 * 7) = 11. Estoque atual 5. Pedir: 11 - 5 = 6.
    assert item['ideal_cobertura'] == 11
    assert item['qtd_sugerida'] == 6
    assert set(item['fontes']) == {'vnda', 'manual'}
    assert item['por_fonte'] == {'vnda': 14, 'manual': 7}


def test_sugerir_pedido_sem_dados(app, loja):
    """Loja sem vendas: lista vazia."""
    from app.services import vendas_manuais as svc
    out = svc.sugerir_pedido(loja.id)
    assert out == []
