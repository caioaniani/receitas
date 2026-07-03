"""Expandir por item na tela /pedidos/congelados (03/07/2026): últimos
5 CRÉDITOS + 5 DÉBITOS da linha do estoque da indústria, com a direção
vindo da fonte única `historico_humano.mov_producao_direcao`."""
from datetime import timedelta

from app.extensions import db
from app.models import EstoqueProducao, MovEstoqueProducao, Receita
from app.utils import agora


def _login(client, uid):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)
        sess['_fresh'] = True


def _seed(movs):
    r = Receita(nome='Massa Movs', categoria='Massas', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=1000.0)
    db.session.add(r)
    db.session.commit()
    ep = EstoqueProducao(receita_id=r.id, quantidade=50)
    db.session.add(ep)
    db.session.commit()
    base = agora()
    for i, (tipo, qtd) in enumerate(movs):
        db.session.add(MovEstoqueProducao(
            estoque_producao_id=ep.id, tipo=tipo, quantidade=qtd,
            data=base - timedelta(minutes=len(movs) - i),
            referencia=f'ref-{tipo}-{i}'))
    db.session.commit()
    return ep


def test_direcao_dos_movimentos():
    from app.services.historico_humano import mov_producao_direcao
    assert mov_producao_direcao('producao') == 'credito'
    assert mov_producao_direcao('retorno_loja') == 'credito'
    assert mov_producao_direcao('entrada_nf') == 'credito'
    assert mov_producao_direcao('estorno_saida_pedido') == 'credito'
    assert mov_producao_direcao('saida_pedido') == 'debito'
    assert mov_producao_direcao('perda') == 'debito'
    assert mov_producao_direcao('venda_b2b') == 'debito'
    assert mov_producao_direcao('consumo_subreceita') == 'debito'
    assert mov_producao_direcao('retorno_loja_estorno') == 'debito'
    # Assinado: conferência pra cima = crédito, pra baixo = débito.
    assert mov_producao_direcao('ajuste_conferencia', 4) == 'credito'
    assert mov_producao_direcao('ajuste_conferencia', -3) == 'debito'
    # Informativos não mexem em saldo.
    assert mov_producao_direcao('venda_b2b_sem_estoque') == 'neutro'
    assert mov_producao_direcao('saida_pedido_sem_estoque') == 'neutro'
    assert mov_producao_direcao('consolidacao_estado') == 'neutro'


def test_endpoint_separa_creditos_e_debitos(app, admin_user):
    with app.app_context():
        ep = _seed([
            ('producao', 20),
            ('retorno_loja', 5),
            ('ajuste_conferencia', 4),          # crédito (sinal +)
            ('ajuste_conferencia', -3),         # débito (sinal -)
            ('saida_pedido', 8),
            ('perda', 2),
            ('venda_b2b_sem_estoque', 9),       # neutro: fora das listas
            ('consolidacao_estado', 1),         # neutro: fora das listas
        ])
        c = app.test_client()
        _login(c, admin_user.id)
        resp = c.get(f'/pedidos/congelados/movs/{ep.id}')
        assert resp.status_code == 200
        d = resp.get_json()
        assert d['ok'] is True
        assert d['item'] == 'Massa Movs'
        tipos_c = [m['tipo'] for m in d['creditos']]
        tipos_d = [m['tipo'] for m in d['debitos']]
        assert len(d['creditos']) == 3 and len(d['debitos']) == 3
        # Quantidades sempre absolutas (o sinal vem da coluna da tela).
        assert all(m['quantidade'] > 0 for m in d['creditos'] + d['debitos'])
        assert not any('sem estoque' in t.lower() for t in tipos_c + tipos_d)
        assert not any('duplicadas' in t.lower() for t in tipos_c + tipos_d)


def test_endpoint_limita_a_5_mais_recentes(app, admin_user):
    with app.app_context():
        ep = _seed([('producao', i + 1) for i in range(8)])
        c = app.test_client()
        _login(c, admin_user.id)
        d = c.get(f'/pedidos/congelados/movs/{ep.id}').get_json()
        assert len(d['creditos']) == 5
        # Mais recentes primeiro: os últimos seeds (qtd 8, 7, 6, 5, 4).
        assert [m['quantidade'] for m in d['creditos']] == [8, 7, 6, 5, 4]
        assert d['debitos'] == []


def test_endpoint_exige_login(app):
    with app.app_context():
        ep = _seed([('producao', 1)])
        resp = app.test_client().get(f'/pedidos/congelados/movs/{ep.id}')
        assert resp.status_code in (302, 401)
