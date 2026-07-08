"""Arquivar/excluir cliente B2B e excluir venda de teste (07/07/2026).

Virada pra produção: cliente arquivado some da lista (histórico fica);
cliente sem histórico pode ser excluído; venda de teste é excluída
definitivamente pelo dono (estorna estoque, some das contas a receber).
"""
from decimal import Decimal
from unittest.mock import ANY  # noqa: F401  (simetria com os demais testes)

from app.extensions import db
from app.models import (
    ClienteB2B,
    EstoqueProducao,
    PrecoClienteB2B,
    Produto,
    VendaB2B,
    VendaB2BParcela,
)


def _login(client, uid):
    with client.session_transaction() as s:
        s['_user_id'] = str(uid)
        s['_fresh'] = True


def _cliente(nome='Restaurante Bom Prato', ativo=True):
    c = ClienteB2B(nome=nome, ativo=ativo)
    db.session.add(c)
    db.session.commit()
    return c


def _venda_com_estoque(cli, qtd=5):
    """Venda REAL pelo service (baixa estoque de verdade)."""
    from app.services.vendas_b2b import criar_venda
    p = Produto(nome='Pao Frances Congelado', ativo=True)
    db.session.add(p)
    db.session.flush()
    ep = EstoqueProducao(produto_id=p.id, quantidade=10)
    db.session.add(ep)
    db.session.commit()
    v = criar_venda(cliente_id=cli.id,
                    itens=[{'tipo': 'produto', 'id': p.id, 'quantidade': qtd,
                            'preco_unitario': 10.0}])
    return v, ep


# ── clientes: lista filtra arquivados ──────────────────────────────────────

def test_lista_esconde_arquivados_por_padrao(app, admin_user):
    with app.app_context():
        _cliente('Ativo Ltda')
        _cliente('Arquivado Ltda', ativo=False)
    c = app.test_client()
    _login(c, admin_user.id)
    corpo = c.get('/b2b/clientes').get_data(as_text=True)
    assert 'Ativo Ltda' in corpo
    assert 'Arquivado Ltda' not in corpo
    assert 'Arquivados (1)' in corpo          # atalho pro filtro
    corpo2 = c.get('/b2b/clientes?arquivados=1').get_data(as_text=True)
    assert 'Arquivado Ltda' in corpo2
    assert 'Ativo Ltda' not in corpo2


def test_arquivar_e_reativar_cliente(app, admin_user):
    with app.app_context():
        cli = _cliente()
        cid = cli.id
    c = app.test_client()
    _login(c, admin_user.id)
    c.post(f'/b2b/clientes/{cid}/arquivar', follow_redirects=True)
    with app.app_context():
        assert db.session.get(ClienteB2B, cid).ativo is False
    c.post(f'/b2b/clientes/{cid}/arquivar', follow_redirects=True)
    with app.app_context():
        assert db.session.get(ClienteB2B, cid).ativo is True


def test_excluir_cliente_sem_historico_apaga_com_precos(app, admin_user):
    with app.app_context():
        cli = _cliente()
        db.session.add(PrecoClienteB2B(cliente_id=cli.id, kind='receita',
                                       item_id=1, preco=Decimal('5.00')))
        db.session.commit()
        cid = cli.id
    c = app.test_client()
    _login(c, admin_user.id)
    r = c.post(f'/b2b/clientes/{cid}/excluir', follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        assert db.session.get(ClienteB2B, cid) is None
        assert PrecoClienteB2B.query.count() == 0    # cascade


def test_excluir_cliente_com_venda_recusa_e_sugere_arquivar(app, admin_user):
    with app.app_context():
        cli = _cliente()
        _venda_com_estoque(cli)
        cid = cli.id
    c = app.test_client()
    _login(c, admin_user.id)
    r = c.post(f'/b2b/clientes/{cid}/excluir', follow_redirects=True)
    assert 'Arquivar' in r.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(ClienteB2B, cid) is not None


# ── venda: exclusão definitiva (dono) ──────────────────────────────────────

def test_excluir_venda_estorna_estoque_e_some_do_contas_a_receber(
        app, owner_user):
    from app.services.vendas_b2b import excluir_venda
    with app.app_context():
        cli = _cliente()
        v, ep = _venda_com_estoque(cli, qtd=5)
        assert ep.quantidade == 5                    # baixou
        assert VendaB2BParcela.query.count() == 1    # parcela única
        vid, ep_id = v.id, ep.id
        excluir_venda(v, user=owner_user)
        assert db.session.get(VendaB2B, vid) is None
        assert VendaB2BParcela.query.count() == 0    # cascade
        assert db.session.get(EstoqueProducao, ep_id).quantidade == 10


def test_excluir_venda_cancelada_nao_estorna_em_dobro(app, owner_user):
    from app.services.vendas_b2b import cancelar_venda, excluir_venda
    with app.app_context():
        cli = _cliente()
        v, ep = _venda_com_estoque(cli, qtd=5)
        cancelar_venda(v)                            # já estornou (10)
        assert ep.quantidade == 10
        excluir_venda(v, user=owner_user)
        assert db.session.get(EstoqueProducao, ep.id).quantidade == 10


def test_excluir_venda_recusa_com_pagamento_ou_fatura(app, owner_user):
    from app.services.vendas_b2b import excluir_venda, receber_pagamento
    with app.app_context():
        cli = _cliente()
        v, _ = _venda_com_estoque(cli)
        receber_pagamento(v.parcelas[0], 10)
        db.session.commit()
        try:
            excluir_venda(v, user=owner_user)
            raise AssertionError('deveria recusar com pagamento')
        except ValueError as exc:
            assert 'pagamento' in str(exc)


def test_rota_excluir_venda_e_owner_only(app, admin_user):
    with app.app_context():
        cli = _cliente()
        v, _ = _venda_com_estoque(cli)
        vid = v.id
    c = app.test_client()
    _login(c, admin_user.id)                         # admin comum: 403
    assert c.post(f'/b2b/vendas/{vid}/excluir').status_code == 403


def test_rota_excluir_venda_pelo_dono(app, owner_user):
    with app.app_context():
        cli = _cliente()
        v, _ = _venda_com_estoque(cli)
        vid = v.id
    c = app.test_client()
    _login(c, owner_user.id)
    r = c.post(f'/b2b/vendas/{vid}/excluir', follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        assert db.session.get(VendaB2B, vid) is None
