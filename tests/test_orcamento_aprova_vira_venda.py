"""Aprovar orçamento VIRA venda (07/07/2026, decisão do dono).

Fazer orçamento é leve; APROVAR é amarrado aos processos: exige data de
entrega, itens vinculados ao catálogo, quantidades inteiras e desconto/
frete embutidos nos preços. Aprovação válida cria a VendaB2B vinculada
(entra na fila do padeiro SEM baixar estoque — baixa é na separação).
"""
from datetime import timedelta
from decimal import Decimal

from app.extensions import db
from app.models import ClienteB2B, EstoqueProducao, Orcamento, OrcamentoItem, VendaB2B
from app.services import orcamentos as orc_svc
from app.utils import hoje


def _orc(catalogo, cliente=None, data_entrega=1, itens=True, livre=False,
         qtd=Decimal('5'), desconto='0', frete='0'):
    o = Orcamento(codigo=f'ORC-T-{Orcamento.query.count() + 1:04d}',
                  cliente_id=cliente.id if cliente else None,
                  cliente_nome=None if cliente else 'Avulso Eventos',
                  status='enviado',
                  data_entrega=(hoje() + timedelta(days=data_entrega)
                                if data_entrega is not None else None),
                  desconto_valor=Decimal(desconto),
                  frete_valor=Decimal(frete),
                  subtotal=Decimal('50'), valor_total=Decimal('50'))
    db.session.add(o)
    db.session.flush()
    if itens:
        db.session.add(OrcamentoItem(
            orcamento_id=o.id, receita_id=catalogo['receita'].id,
            nome=catalogo['receita'].nome, quantidade=qtd,
            preco_unitario=Decimal('10'), subtotal=qtd * 10))
    if livre:
        db.session.add(OrcamentoItem(
            orcamento_id=o.id, nome='Servico de buffet',
            quantidade=Decimal('1'), preco_unitario=Decimal('200'),
            subtotal=Decimal('200')))
    db.session.commit()
    return o


def test_aprovar_exige_data_de_entrega(app, catalogo):
    with app.app_context():
        o = _orc(catalogo, data_entrega=None)
        ok, erro = orc_svc.marcar_status(o, 'aprovado')
        assert not ok and 'data de entrega' in erro
        assert o.status == 'enviado' and o.venda_id is None


def test_aprovar_recusa_linha_livre_qtd_fracionada_desconto_frete(
        app, catalogo):
    with app.app_context():
        o = _orc(catalogo, livre=True, qtd=Decimal('2.5'),
                 desconto='10', frete='15')
        ok, erro = orc_svc.marcar_status(o, 'aprovado')
        assert not ok
        assert 'Servico de buffet' in erro          # linha livre nomeada
        assert 'fracionada' in erro
        assert 'desconto' in erro and 'frete' in erro


def test_aprovar_valido_cria_venda_vinculada_sem_baixar_estoque(
        app, catalogo):
    with app.app_context():
        ep = EstoqueProducao(receita_id=catalogo['receita'].id,
                             quantidade=20)
        cli = ClienteB2B(nome='Restaurante Bom Prato', ativo=True)
        db.session.add_all([ep, cli])
        db.session.commit()
        o = _orc(catalogo, cliente=cli)
        ok, erro = orc_svc.marcar_status(o, 'aprovado', usuario_id=None)
        assert ok, erro
        assert o.status == 'aprovado' and o.venda_id
        v = db.session.get(VendaB2B, o.venda_id)
        assert v.cliente_id == cli.id
        assert v.data_entrega == o.data_entrega     # herda a data prometida
        assert v.itens[0].quantidade == 5
        assert v.itens[0].preco_unitario == Decimal('10')
        assert f'orcamento {o.codigo}' in (v.observacao or '')
        # Regime da baixa: fila do padeiro — nada baixou ainda
        db.session.refresh(ep)
        assert ep.quantidade == 20
        assert v.estoque_baixado_em is None
        # Cliente normal ganha a parcela única padrão
        assert len(v.parcelas) == 1


def test_aprovar_cliente_mensal_sem_parcela(app, catalogo):
    with app.app_context():
        cli = ClienteB2B(nome='Hotel Mensal', ativo=True,
                         faturamento_mensal=True)
        db.session.add(cli)
        db.session.commit()
        o = _orc(catalogo, cliente=cli)
        ok, erro = orc_svc.marcar_status(o, 'aprovado')
        assert ok, erro
        v = db.session.get(VendaB2B, o.venda_id)
        assert v.parcelas == []                     # vai pra conta do mês


def test_aprovado_convertido_sai_da_aba_aprovados(app, admin_user, catalogo):
    with app.app_context():
        cli = ClienteB2B(nome='Restaurante Bom Prato', ativo=True)
        db.session.add(cli)
        db.session.commit()
        o = _orc(catalogo, cliente=cli)
        ok, _ = orc_svc.marcar_status(o, 'aprovado')
        assert ok
        codigo = o.codigo
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(admin_user.id)
        s['_fresh'] = True
    corpo = c.get('/b2b/?aba=pedidos&f=aprovados').get_data(as_text=True)
    assert codigo not in corpo                      # já virou venda
    prod = c.get('/b2b/?aba=pedidos&f=producao').get_data(as_text=True)
    assert 'Restaurante Bom Prato' in prod          # a venda está aqui


def test_nao_converte_duas_vezes(app, admin_user, catalogo):
    with app.app_context():
        cli = ClienteB2B(nome='Restaurante Bom Prato', ativo=True)
        db.session.add(cli)
        db.session.commit()
        o = _orc(catalogo, cliente=cli)
        orc_svc.marcar_status(o, 'aprovado')
        oid, vid = o.id, o.venda_id
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(admin_user.id)
        s['_fresh'] = True
    # O seed manual redireciona pra venda existente em vez de duplicar
    r = c.get(f'/b2b/vendas/nova?orcamento={oid}', follow_redirects=True)
    assert f'#{vid}' in r.get_data(as_text=True) or 'já virou' in r.get_data(as_text=True)
    with app.app_context():
        assert VendaB2B.query.count() == 1


def test_aprovar_sem_cliente_recusa(app, catalogo):
    with app.app_context():
        o = _orc(catalogo)
        o.cliente_nome = None                       # nem cadastrado nem avulso
        db.session.commit()
        ok, erro = orc_svc.marcar_status(o, 'aprovado')
        assert not ok and 'cliente' in erro
        assert o.status == 'enviado' and o.venda_id is None


def test_aprovar_orcamento_avulso_cria_venda(app, catalogo):
    """Cliente avulso (só nome, sem cadastro) aprova normalmente."""
    with app.app_context():
        o = _orc(catalogo)                          # cliente_nome='Avulso Eventos'
        ok, erro = orc_svc.marcar_status(o, 'aprovado')
        assert ok, erro
        v = db.session.get(VendaB2B, o.venda_id)
        assert v.cliente_id is None
        assert v.cliente_nome == 'Avulso Eventos'


def test_claim_atomico_na_aprovacao(app, catalogo):
    """Dois POSTs de aprovar quase simultâneos: o UPDATE condicional do
    status garante que só um converte. Simula o perdedor — outro request
    já aprovou no banco, mas o objeto em memória ainda vê 'enviado'."""
    from sqlalchemy import update
    with app.app_context():
        o = _orc(catalogo)
        db.session.execute(
            update(Orcamento).where(Orcamento.id == o.id)
            .values(status='aprovado')
            .execution_options(synchronize_session=False))
        assert o.status == 'enviado'                # visão desatualizada
        ok, erro = orc_svc.marcar_status(o, 'aprovado')
        assert not ok and 'ja processado' in erro
        assert VendaB2B.query.count() == 0          # o perdedor não converteu


def test_reparo_religa_venda_orfa(app, catalogo):
    """Janela de crash entre o commit de criar_venda e o do vínculo: a
    re-aprovação religa a venda órfã (pela observação de origem) em vez
    de criar uma segunda demanda na fila do padeiro."""
    from app.services import vendas_b2b as vsvc
    with app.app_context():
        o = _orc(catalogo)
        orfa = vsvc.criar_venda(
            cliente_nome='Avulso Eventos', data_entrega=o.data_entrega,
            itens=[{'tipo': 'receita', 'id': catalogo['receita'].id,
                    'quantidade': 5, 'preco_unitario': 10.0}],
            observacao=f'Origem: orcamento {o.codigo}', user=None)
        ok, erro = orc_svc.marcar_status(o, 'aprovado')
        assert ok, erro
        assert o.venda_id == orfa.id
        assert VendaB2B.query.count() == 1


def test_post_venda_criar_orcamento_convertido_nao_duplica(
        app, admin_user, catalogo):
    """Form 'Virar venda' aberto numa aba enquanto o orçamento era
    convertido por outro caminho: o POST não pode criar a 2ª venda."""
    with app.app_context():
        cli = ClienteB2B(nome='Restaurante Bom Prato', ativo=True)
        db.session.add(cli)
        db.session.commit()
        o = _orc(catalogo, cliente=cli)
        orc_svc.marcar_status(o, 'aprovado')
        oid, vid = o.id, o.venda_id
        rid = catalogo['receita'].id
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(admin_user.id)
        s['_fresh'] = True
    r = c.post('/b2b/vendas/nova', data={
        'orcamento_id': str(oid),
        'cliente_nome': 'Restaurante Bom Prato',
        'data_venda': hoje().isoformat(),
        'data_entrega': (hoje() + timedelta(days=1)).isoformat(),
        'item_ref[]': f'receita:{rid}',
        'item_qtd[]': '5', 'item_preco[]': '10',
        'item_desc[]': '', 'item_estado[]': '',
    }, follow_redirects=True)
    assert 'já virou a venda' in r.get_data(as_text=True)
    with app.app_context():
        assert VendaB2B.query.count() == 1
        assert db.session.get(Orcamento, oid).venda_id == vid


def test_excluir_venda_libera_orcamento(app, owner_user, catalogo):
    from app.services import vendas_b2b as svc
    with app.app_context():
        cli = ClienteB2B(nome='Restaurante Bom Prato', ativo=True)
        db.session.add(cli)
        db.session.commit()
        o = _orc(catalogo, cliente=cli)
        orc_svc.marcar_status(o, 'aprovado')
        v = db.session.get(VendaB2B, o.venda_id)
        svc.excluir_venda(v, user=owner_user)
        db.session.refresh(o)
        assert o.venda_id is None                   # volta pra Aprovados
