"""Item de NF -> PRODUTO de revenda (água, chiclete, iogurte pronto):
vínculo pela tela (alvo 'prod-N'), entrada de estoque em unidades
(loja/indústria) e Produto.custo_direto atualizado por NF. Etapa 2 do
pedido do dono (10/06/2026)."""
import json

from app.extensions import db
from app.services import conta_pagar_estoque as svc
from app.utils import agora


def _login(c):
    c.post('/auth/login', data={'login': 'admin', 'senha': '123'})


def _cenario(app, canal_industria=False):
    from app.models import Loja, Produto, SlackCanalLojaMap
    with app.app_context():
        loja = Loja(nome='Industria' if canal_industria else 'Centro',
                    ativa=True)
        db.session.add(loja)
        db.session.flush()
        db.session.add(SlackCanalLojaMap(
            canal_id='C_T', loja_id=loja.id,
            eh_industria=canal_industria, confirmado_em=agora()))
        prod = Produto(nome='Chicle Trident Hortelã', categoria='Revenda',
                       custo_direto=0)
        db.session.add(prod)
        db.session.commit()
        return loja.id, prod.id


def _conta(app, itens):
    from app.models import ContaPagar
    with app.app_context():
        c = ContaPagar(origem_canal='C_T', fornecedor_nome='Distribuidora',
                       status='aberto', itens_json=json.dumps(itens))
        db.session.add(c)
        db.session.commit()
        return c.id


def test_vincular_produto_pela_tela(app, admin_user):
    from app.models import ContaPagarItemMap
    _, prod_id = _cenario(app)
    with app.app_context():
        m = ContaPagarItemMap(
            item_nome_norm=svc.normalizar_item_nome('CHICLE TRIDENT 21UN'),
            item_nome_exemplo='CHICLE TRIDENT 21UN')
        db.session.add(m)
        db.session.commit()
        mid = m.id

    c = app.test_client()
    _login(c)
    r = c.post(f'/contas-pagar/mapeamentos/{mid}',
               data={'acao': 'vincular', 'alvo': f'prod-{prod_id}',
                     'unidade_compra': 'cx', 'fator_conversao': '21',
                     'estado': 'pendente'}, follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        m2 = db.session.get(ContaPagarItemMap, mid)
        assert m2.produto_id == prod_id
        assert m2.materia_prima_id is None
        assert m2.confirmado_em is not None and m2.fator_conversao == 21
        assert m2.estado == 'mapeado'
        assert '(produto)' in m2.alvo_nome

    # desfazer limpa o produto tambem
    c.post(f'/contas-pagar/mapeamentos/{mid}',
           data={'acao': 'desfazer', 'estado': 'mapeado'})
    with app.app_context():
        assert db.session.get(ContaPagarItemMap, mid).produto_id is None


def test_processa_produto_em_loja(app, admin_user):
    """NF de 1 cx (21 un, R$ 39,63): entra 21 un no EstoqueLoja do produto e
    custo_direto vira 39,63/21. Idempotente."""
    from app.models import ContaPagarItemMap, EstoqueLoja, MovEstoqueLoja, Produto
    loja_id, prod_id = _cenario(app)
    with app.app_context():
        db.session.add(ContaPagarItemMap(
            item_nome_norm=svc.normalizar_item_nome('CHICLE TRIDENT 21UN'),
            item_nome_exemplo='CHICLE TRIDENT 21UN', produto_id=prod_id,
            fator_conversao=21, confirmado_em=agora()))
        db.session.commit()
    cid = _conta(app, [{'nome': 'CHICLE TRIDENT 21UN', 'quantidade': 1,
                        'valor_total': 39.63}])
    with app.app_context():
        from app.models import ContaPagar
        conta = db.session.get(ContaPagar, cid)
        stats = svc.processar_conta(conta)
        assert stats['processados'] == 1
        el = EstoqueLoja.query.filter_by(loja_id=loja_id,
                                         produto_id=prod_id).one()
        assert el.quantidade == 21
        assert MovEstoqueLoja.query.filter_by(estoque_loja_id=el.id,
                                              tipo='entrada_nf').count() == 1
        prod = db.session.get(Produto, prod_id)
        assert abs(prod.custo_direto - 39.63 / 21) < 1e-9

        stats2 = svc.processar_conta(conta)   # idempotente
        assert stats2['ja_processados'] == 1
        assert EstoqueLoja.query.filter_by(produto_id=prod_id).one().quantidade == 21


def test_processa_produto_na_industria(app, admin_user):
    from app.models import ContaPagar, ContaPagarItemMap, EstoqueProducao
    _, prod_id = _cenario(app, canal_industria=True)
    with app.app_context():
        db.session.add(ContaPagarItemMap(
            item_nome_norm=svc.normalizar_item_nome('AGUA PRATA 12X'),
            item_nome_exemplo='AGUA PRATA 12X', produto_id=prod_id,
            fator_conversao=12, confirmado_em=agora()))
        db.session.commit()
    cid = _conta(app, [{'nome': 'AGUA PRATA 12X', 'quantidade': 2,
                        'valor_total': 60.0}])
    with app.app_context():
        stats = svc.processar_conta(db.session.get(ContaPagar, cid))
        assert stats['processados'] == 1
        ep = EstoqueProducao.query.filter_by(produto_id=prod_id).one()
        assert ep.quantidade == 24


def test_fracao_de_produto_fica_pendente(app, admin_user):
    """Estoque de produto e inteiro — fracao nao arredonda em silencio."""
    from app.models import ContaPagar, ContaPagarItemMap, EstoqueLoja
    _, prod_id = _cenario(app)
    with app.app_context():
        db.session.add(ContaPagarItemMap(
            item_nome_norm=svc.normalizar_item_nome('AGUA GALAO'),
            item_nome_exemplo='AGUA GALAO', produto_id=prod_id,
            fator_conversao=1.5, confirmado_em=agora()))
        db.session.commit()
    cid = _conta(app, [{'nome': 'AGUA GALAO', 'quantidade': 1,
                        'valor_total': 10.0}])
    with app.app_context():
        stats = svc.processar_conta(db.session.get(ContaPagar, cid))
        assert stats['fracao_loja_pendente'] == 1
        assert stats['processados'] == 0
        assert EstoqueLoja.query.filter_by(produto_id=prod_id).count() == 0


def test_lote_confirma_alvo_produto(app, admin_user):
    from app.models import ContaPagarItemMap
    _, prod_id = _cenario(app)
    with app.app_context():
        m = ContaPagarItemMap(
            item_nome_norm=svc.normalizar_item_nome('TRIDENT MENTA'),
            item_nome_exemplo='TRIDENT MENTA')
        db.session.add(m)
        db.session.commit()
        mid = m.id
    c = app.test_client()
    _login(c)
    r = c.post('/contas-pagar/mapeamentos/lote', json={
        'acao': 'vincular',
        'itens': [{'id': mid, 'alvo': f'prod-{prod_id}',
                   'unidade_compra': 'cx', 'fator_conversao': '21'}]})
    assert r.get_json() == {'ok': 1, 'falhas': []}
    with app.app_context():
        assert db.session.get(ContaPagarItemMap, mid).produto_id == prod_id


def test_dropdown_mostra_grupo_de_produtos(app, admin_user):
    from app.models import ContaPagarItemMap
    _, prod_id = _cenario(app)
    with app.app_context():
        db.session.add(ContaPagarItemMap(
            item_nome_norm=svc.normalizar_item_nome('QUALQUER'),
            item_nome_exemplo='QUALQUER'))
        db.session.commit()
    c = app.test_client()
    _login(c)
    r = c.get('/contas-pagar/mapeamentos')
    assert 'Produtos (revenda'.encode() in r.data
    assert f'value="prod-{prod_id}"'.encode() in r.data
