"""REGIME DA BAIXA B2B (07/07/2026, decisão do dono): o estoque da
indústria só baixa quando o padeiro SEPARA o pedido na tela /padeiro.

- venda com data_entrega: criada SEM baixa (fila do padeiro);
- venda imediata (sem data): baixa na criação;
- separação baixa (idempotente pelo marcador `estoque_baixado_em`;
  venda do regime antigo não baixa em dobro);
- reverter separado→pendente estorna; re-separar baixa de novo;
- enquanto não baixa, a venda é demanda COMPROMETIDA no balanço e
  desconta do estoque disponível dos forms/previews.
"""
from datetime import timedelta

from app.extensions import db
from app.models import EstoqueProducao, Produto, ProdutoItem, VendaB2B
from app.services import vendas_b2b as svc
from app.utils import agora, hoje


def _estoque(catalogo, qtd=20):
    ep = EstoqueProducao(receita_id=catalogo['receita'].id, quantidade=qtd)
    db.session.add(ep)
    db.session.commit()
    return ep


def _venda_fila(catalogo, admin_user, qtd=5, dias=1):
    return svc.criar_venda(
        cliente_nome='Zion Church',
        data_entrega=hoje() + timedelta(days=dias),
        itens=[{'tipo': 'receita', 'id': catalogo['receita'].id,
                'quantidade': qtd, 'preco_unitario': 10.0}],
        user=admin_user)


def test_venda_com_data_nao_baixa_na_criacao(app, admin_user, catalogo):
    with app.app_context():
        ep = _estoque(catalogo)
        v = _venda_fila(catalogo, admin_user)
        db.session.refresh(ep)
        assert ep.quantidade == 20
        assert v.estoque_baixado_em is None


def test_venda_imediata_baixa_na_criacao(app, admin_user, catalogo):
    with app.app_context():
        ep = _estoque(catalogo)
        v = svc.criar_venda(
            cliente_nome='Balcao', data_entrega=None,
            itens=[{'tipo': 'receita', 'id': catalogo['receita'].id,
                    'quantidade': 5, 'preco_unitario': 10.0}],
            user=admin_user)
        db.session.refresh(ep)
        assert ep.quantidade == 15
        assert v.estoque_baixado_em is not None


def test_separacao_baixa_e_e_idempotente(app, admin_user, catalogo):
    with app.app_context():
        ep = _estoque(catalogo)
        v = _venda_fila(catalogo, admin_user)
        assert svc.baixar_na_separacao(v, user=admin_user) is True
        db.session.commit()
        db.session.refresh(ep)
        assert ep.quantidade == 15
        # Duplo clique / re-separação: não baixa de novo
        assert svc.baixar_na_separacao(v, user=admin_user) is False
        db.session.refresh(ep)
        assert ep.quantidade == 15


def test_venda_do_regime_antigo_nao_baixa_em_dobro(app, admin_user, catalogo):
    """Venda legada (baixou na criação — backfill marca o regime): a
    separação pós-deploy NÃO pode baixar de novo."""
    with app.app_context():
        ep = _estoque(catalogo)
        v = _venda_fila(catalogo, admin_user)
        # Simula o legado: baixa manual + marcador (como o backfill deixa)
        svc._baixar_venda(v, admin_user)
        db.session.commit()
        db.session.refresh(ep)
        assert ep.quantidade == 15
        assert svc.baixar_na_separacao(v, user=admin_user) is False
        db.session.refresh(ep)
        assert ep.quantidade == 15


def test_rota_padeiro_separar_baixa(app, admin_user, catalogo):
    with app.app_context():
        ep = _estoque(catalogo)
        v = _venda_fila(catalogo, admin_user, dias=0)
        vid, epid = v.id, ep.id
    c = app.test_client()
    c.post('/auth/login', data={'login': 'admin', 'senha': '123'})
    r = c.post(f'/padeiro/b2b/{vid}/separar', follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        v = db.session.get(VendaB2B, vid)
        assert v.status_entrega == 'separado'
        assert v.estoque_baixado_em is not None
        assert db.session.get(EstoqueProducao, epid).quantidade == 15


def test_reverter_separado_estorna_e_reseparar_baixa_uma_vez(
        app, admin_user, catalogo):
    with app.app_context():
        ep = _estoque(catalogo)
        v = _venda_fila(catalogo, admin_user)
        svc.baixar_na_separacao(v, user=admin_user)
        v.status_entrega = 'separado'
        db.session.commit()
        # Voltar pra pendente devolve o pão pro freezer (sistema = físico)
        svc.reverter_status_entrega(v, user=admin_user)
        db.session.refresh(ep)
        assert ep.quantidade == 20
        assert v.estoque_baixado_em is None
        # Re-separar baixa de novo — UMA vez
        assert svc.baixar_na_separacao(v, user=admin_user) is True
        db.session.commit()
        db.session.refresh(ep)
        assert ep.quantidade == 15


def test_cancelar_venda_pendente_nao_mexe_no_estoque(
        app, admin_user, catalogo):
    with app.app_context():
        ep = _estoque(catalogo)
        v = _venda_fila(catalogo, admin_user)
        svc.cancelar_venda(v, user=admin_user)
        db.session.refresh(ep)
        assert ep.quantidade == 20              # nunca baixou, nada volta


def test_editar_venda_pendente_na_fila_nao_baixa(app, admin_user, catalogo):
    with app.app_context():
        ep = _estoque(catalogo)
        v = _venda_fila(catalogo, admin_user, qtd=5)
        svc.editar_venda(v, cliente_nome='Zion Church',
                         data_entrega=v.data_entrega,
                         itens=[{'tipo': 'receita',
                                 'id': catalogo['receita'].id,
                                 'quantidade': 8, 'preco_unitario': 10.0}],
                         user=admin_user)
        db.session.refresh(ep)
        assert ep.quantidade == 20
        assert v.estoque_baixado_em is None


def test_editar_venda_ja_separada_rebaixa_qtd_nova(app, admin_user, catalogo):
    with app.app_context():
        ep = _estoque(catalogo)
        v = _venda_fila(catalogo, admin_user, qtd=5)
        svc.baixar_na_separacao(v, user=admin_user)
        v.status_entrega = 'separado'
        db.session.commit()
        svc.editar_venda(v, cliente_nome='Zion Church',
                         data_entrega=v.data_entrega,
                         itens=[{'tipo': 'receita',
                                 'id': catalogo['receita'].id,
                                 'quantidade': 3, 'preco_unitario': 10.0}],
                         user=admin_user)
        db.session.refresh(ep)
        assert ep.quantidade == 17              # estornou 5, baixou 3
        assert v.estoque_baixado_em is not None


def test_sincronizar_baixa_com_data(app, admin_user, catalogo):
    """Limpar a data (vira imediata) baixa na hora; dar data a uma venda
    imediata (entra na fila) estorna — a separação baixa de novo."""
    with app.app_context():
        ep = _estoque(catalogo)
        v = _venda_fila(catalogo, admin_user)
        v.data_entrega = None
        svc.sincronizar_baixa_com_data(v, user=admin_user)
        db.session.commit()
        db.session.refresh(ep)
        assert ep.quantidade == 15 and v.estoque_baixado_em is not None
        v.data_entrega = hoje() + timedelta(days=2)
        svc.sincronizar_baixa_com_data(v, user=admin_user)
        db.session.commit()
        db.session.refresh(ep)
        assert ep.quantidade == 20 and v.estoque_baixado_em is None


def test_comprometido_b2b_pendente_explode_cesta(app, admin_user, catalogo):
    with app.app_context():
        cesta = Produto(nome='Cesta Cafe', ativo=True)
        db.session.add(cesta)
        db.session.flush()
        db.session.add(ProdutoItem(produto_id=cesta.id, tipo='receita',
                                   receita_id=catalogo['receita'].id,
                                   item_nome=catalogo['receita'].nome,
                                   quantidade=2))
        db.session.commit()
        svc.criar_venda(
            cliente_nome='Zion Church',
            data_entrega=hoje() + timedelta(days=1),
            itens=[{'tipo': 'produto', 'id': cesta.id, 'quantidade': 3,
                    'preco_unitario': 30.0},
                   {'tipo': 'receita', 'id': catalogo['receita'].id,
                    'quantidade': 4, 'preco_unitario': 10.0}],
            user=admin_user)
        pend = svc.comprometido_b2b_pendente()
        # 3 cestas × 2 + 4 avulsas = 10 da receita comprometidas
        assert pend[('receita', catalogo['receita'].id)] == 10


def test_balanco_conta_venda_pendente_e_nao_conta_baixada(
        app, admin_user, catalogo):
    """O balanço enxerga a venda B2B aguardando separação como demanda
    comprometida; depois da separação ela SAI (o estoque já refletiu) —
    nunca conta duas vezes."""
    from app.services.previsao_producao import balanco_industria
    with app.app_context():
        rid = catalogo['receita'].id
        _estoque(catalogo, qtd=20)
        v = _venda_fila(catalogo, admin_user, qtd=6, dias=1)
        itens = {i['receita_id']: i for i in
                 balanco_industria(horizonte_dias=7, usar_cache=False)['itens']}
        assert itens[rid]['comprometido'] >= 6
        assert any(b['loja_nome'] == 'Vendas B2B' and b['qtd'] == 6
                   for b in itens[rid]['breakdown_comprometido'])
        # Separou: sai do comprometido, entra na redução do estoque
        svc.baixar_na_separacao(v, user=admin_user)
        db.session.commit()
        itens2 = {i['receita_id']: i for i in
                  balanco_industria(horizonte_dias=7, usar_cache=False)['itens']}
        assert itens2[rid]['comprometido'] == itens[rid]['comprometido'] - 6
        assert itens2[rid]['em_estoque'] == 14


def test_estorno_total_limpa_marcador(app, admin_user, catalogo):
    with app.app_context():
        _estoque(catalogo)
        v = svc.criar_venda(
            cliente_nome='Balcao', data_entrega=None,
            itens=[{'tipo': 'receita', 'id': catalogo['receita'].id,
                    'quantidade': 5, 'preco_unitario': 10.0}],
            user=admin_user)
        assert v.estoque_baixado_em is not None
        svc.cancelar_venda(v, user=admin_user)
        assert v.estoque_baixado_em is None


def test_venda_pendente_sem_marcador_apos_backfill_simulado(app):
    """Sanidade do marcador: venda criada direto no modelo (como as dos
    testes antigos) nasce sem marcador — a separação baixará."""
    with app.app_context():
        v = VendaB2B(cliente_nome='X', valor_total=0,
                     data_entrega=hoje() + timedelta(days=1))
        db.session.add(v)
        db.session.commit()
        assert v.estoque_baixado_em is None
        v.estoque_baixado_em = agora()          # coluna gravável
        db.session.commit()
