"""Trava de regressão pra `_read_consultar_caixa` (copilot — "quanto
faturamos hoje?").

Bug real (19/06/2026 — BOT O PÃO ao dono): a tool reportou
"2 compras de MP → R$ 1.637.220,00" pra uma compra real de R$ 1.637,22.

Causa raiz: `MovimentacaoEstoque.preco_unitario` é gravado como R$/kg
(igual ao `MateriaPrima.custo_por_kg`), mas `quantidade` é gravado na
UNIDADE da MP (g/ml/kg/un). O cálculo antigo `quantidade × preco_unitario`
inflava 1000× pra MPs em g/ml (que é quase tudo numa padaria). O correto
divide por 1000 pra g/ml — mesma fórmula de `custos.py:_custo_unitario_mov`.
"""
from datetime import date, datetime, timedelta


def _user_owner():
    from types import SimpleNamespace
    return SimpleNamespace(id=1, nome='Dono', login='dono', papel='owner',
                            is_admin=lambda: True)


def _criar_mp(db, nome, unidade, custo_por_kg):
    from app.models import MateriaPrima
    mp = MateriaPrima(nome=nome, unidade=unidade, custo_por_kg=custo_por_kg)
    db.session.add(mp)
    db.session.commit()
    return mp


def _registrar_entrada(db, mp, quantidade, preco_unitario, data_dt):
    from app.models import MovimentacaoEstoque
    mov = MovimentacaoEstoque(
        materia_prima_id=mp.id, tipo='entrada',
        quantidade=quantidade, preco_unitario=preco_unitario,
        data=data_dt)
    db.session.add(mov)
    db.session.commit()
    return mov


def test_caixa_mp_em_gramas_nao_infla_1000x(app):
    """Caso real: 50.000g de farinha a R$ 4,50/kg = R$ 225,00 (NÃO R$ 225.000).
    A fórmula antiga retornava o segundo (50.000 × 4,50 = 225.000)."""
    from app.extensions import db
    from app.services.copilot import _read_consultar_caixa
    from app.utils import hoje
    with app.app_context():
        farinha = _criar_mp(db, 'Farinha de Trigo', unidade='g',
                            custo_por_kg=4.50)
        _registrar_entrada(db, farinha, quantidade=50000,
                           preco_unitario=4.50,
                           data_dt=datetime.combine(hoje(),
                                                     datetime.min.time()))
        out = _read_consultar_caixa({}, _user_owner())
    # 50.000g × R$ 4,50/kg = 50kg × R$ 4,50/kg = R$ 225,00
    assert 'R$ 225.00' in out['texto']
    assert 'R$ 225000' not in out['texto']   # o bug antigo


def test_caixa_caso_real_1637220_vira_1637_22(app):
    """O número exato do print do dono em 19/06/2026: R$ 1.637.220
    desinflado vira R$ ~1.630 (uma compra real plausível)."""
    from app.extensions import db
    from app.services.copilot import _read_consultar_caixa
    from app.utils import hoje
    with app.app_context():
        manteiga = _criar_mp(db, 'Manteiga', unidade='g', custo_por_kg=37.00)
        outro = _criar_mp(db, 'Açúcar', unidade='g', custo_por_kg=6.50)
        # 2 compras de MP em gramas: 30.000g × 37/kg + 80.000g × 6,50/kg
        # = 1.110 + 520 = R$ 1.630
        d = datetime.combine(hoje(), datetime.min.time())
        _registrar_entrada(db, manteiga, quantidade=30000,
                           preco_unitario=37.00, data_dt=d)
        _registrar_entrada(db, outro, quantidade=80000,
                           preco_unitario=6.50, data_dt=d)
        out = _read_consultar_caixa({}, _user_owner())
    assert 'R$ 1630.00' in out['texto']
    assert '1630000' not in out['texto']
    # Breakdown por MP aparece (ajuda achar item errado no banco)
    assert 'Manteiga' in out['texto']
    assert 'Açúcar' in out['texto']


def test_caixa_mp_em_un_calcula_direto(app):
    """MP em 'un' NÃO divide por 1000: 100 un × R$ 2,50/un = R$ 250,00."""
    from app.extensions import db
    from app.services.copilot import _read_consultar_caixa
    from app.utils import hoje
    with app.app_context():
        ovo = _criar_mp(db, 'Ovo', unidade='un', custo_por_kg=2.50)
        _registrar_entrada(db, ovo, quantidade=100, preco_unitario=2.50,
                           data_dt=datetime.combine(hoje(),
                                                     datetime.min.time()))
        out = _read_consultar_caixa({}, _user_owner())
    assert 'R$ 250.00' in out['texto']


def test_caixa_so_pega_movs_da_data(app):
    """Movs de OUTRO dia não entram no resumo da data pedida."""
    from app.extensions import db
    from app.services.copilot import _read_consultar_caixa
    from app.utils import hoje
    with app.app_context():
        farinha = _criar_mp(db, 'Farinha', unidade='g', custo_por_kg=4.50)
        ontem = datetime.combine(hoje() - timedelta(days=1),
                                  datetime.min.time())
        _registrar_entrada(db, farinha, quantidade=50000,
                           preco_unitario=4.50, data_dt=ontem)
        out = _read_consultar_caixa({}, _user_owner())  # hoje
    assert '0 compras de MP → R$ 0.00' in out['texto']


def test_caixa_breakdown_lista_top_5(app):
    """Quando há entradas, mostra as 5 maiores por valor + 'outros' se passar."""
    from app.extensions import db
    from app.services.copilot import _read_consultar_caixa
    from app.utils import hoje
    with app.app_context():
        d = datetime.combine(hoje(), datetime.min.time())
        for i, custo in enumerate([10.0, 8.0, 6.0, 4.0, 2.0, 1.0]):
            mp = _criar_mp(db, f'MP{i}', unidade='g', custo_por_kg=custo)
            _registrar_entrada(db, mp, quantidade=1000, preco_unitario=custo,
                               data_dt=d)
        out = _read_consultar_caixa({}, _user_owner())
    for i in range(5):
        assert f'MP{i}' in out['texto']
    assert '(+ 1 outros)' in out['texto']


def test_caixa_data_explicita_funciona(app):
    """O parâmetro `data` (YYYY-MM-DD) muda o dia consultado."""
    from app.extensions import db
    from app.services.copilot import _read_consultar_caixa
    with app.app_context():
        d = date(2026, 5, 1)
        farinha = _criar_mp(db, 'Farinha', unidade='g', custo_por_kg=4.50)
        _registrar_entrada(db, farinha, quantidade=50000, preco_unitario=4.50,
                           data_dt=datetime.combine(d, datetime.min.time()))
        out = _read_consultar_caixa({'data': '2026-05-01'}, _user_owner())
    assert '01/05/2026' in out['texto']
    assert 'R$ 225.00' in out['texto']
