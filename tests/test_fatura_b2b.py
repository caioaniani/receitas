"""Fechamento mensal da conta B2B (07/07/2026).

Cliente `faturamento_mensal` compra o mês (vendas sem parcela); na virada
a conta fecha numa FaturaB2B: 1 parcela por venda (vencimento da fatura),
1 NF consolidada no Tiny e 1 boleto Sicredi do total — a liquidação quita
tudo junto.
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from app.extensions import db
from app.models import (
    ClienteB2B,
    Cobranca,
    FaturaB2B,
    Produto,
    VendaB2B,
    VendaB2BItem,
    VendaB2BParcela,
)
from app.services import faturas_b2b
from app.utils import hoje


def _cliente(mensal=True, completo=True):
    c = ClienteB2B(
        nome='Restaurante Bom Prato', cnpj_cpf='11.222.333/0001-44',
        email='compras@bomprato.com.br', faturamento_mensal=mensal,
        ativo=True)
    if completo:
        c.endereco_logradouro = 'Rua das Laranjeiras'
        c.endereco_numero = '100'
        c.endereco_bairro = 'Centro'
        c.endereco_cep = '04568-001'
        c.endereco_cidade = 'São Paulo'
        c.endereco_uf = 'SP'
    db.session.add(c)
    db.session.commit()
    return c


def _produto(nome='Pao Frances Congelado', sku=None):
    from app.services import tiny_nf
    p = Produto(nome=nome, ativo=True)
    db.session.add(p)
    db.session.flush()
    if sku:
        tiny_nf.definir_sku('produto', p.id, sku, canal='b2b')
    db.session.commit()
    return p


def _venda(cliente, produto, dia, qtd=10, preco='10.00', com_parcela=False):
    v = VendaB2B(cliente_id=cliente.id, data_venda=dia,
                 valor_total=Decimal(preco) * qtd)
    db.session.add(v)
    db.session.flush()
    db.session.add(VendaB2BItem(venda_id=v.id, produto_id=produto.id,
                                quantidade=qtd,
                                preco_unitario=Decimal(preco)))
    if com_parcela:
        db.session.add(VendaB2BParcela(venda_id=v.id, numero=1,
                                       vencimento=dia, valor=v.valor_total))
    db.session.commit()
    return v


def _periodo():
    fim = hoje()
    return fim - timedelta(days=30), fim, fim + timedelta(days=10)


# ── fechar_conta ───────────────────────────────────────────────────────────

def test_fechar_conta_agrupa_vendas_e_cria_parcelas(app):
    with app.app_context():
        cli = _cliente()
        p = _produto()
        ini, fim, venc = _periodo()
        v1 = _venda(cli, p, fim - timedelta(days=20))          # R$ 100
        v2 = _venda(cli, p, fim - timedelta(days=5), qtd=5)    # R$ 50
        _venda(cli, p, fim - timedelta(days=3), com_parcela=True)  # fora
        _venda(cli, p, fim - timedelta(days=60))               # fora (data)
        fat = faturas_b2b.fechar_conta(cli, ini, fim, venc)
        assert fat.valor_total == Decimal('150.00')
        assert {v.id for v in fat.vendas} == {v1.id, v2.id}
        parcelas = VendaB2BParcela.query.filter_by(fatura_id=fat.id).all()
        assert len(parcelas) == 2
        assert all(pc.vencimento == venc for pc in parcelas)
        assert {pc.venda_id for pc in parcelas} == {v1.id, v2.id}
        # Idempotência do universo: fechar de novo não acha nada
        try:
            faturas_b2b.fechar_conta(cli, ini, fim, venc)
            raise AssertionError('deveria ter levantado ValueError')
        except ValueError:
            pass


def test_fechar_conta_sem_vendas_levanta(app):
    with app.app_context():
        cli = _cliente()
        ini, fim, venc = _periodo()
        try:
            faturas_b2b.fechar_conta(cli, ini, fim, venc)
            raise AssertionError('deveria ter levantado ValueError')
        except ValueError as exc:
            assert 'em aberto' in str(exc)


# ── NF consolidada ─────────────────────────────────────────────────────────

def test_nf_consolidada_agrupa_por_item_e_preco(app):
    from app.services import tiny_nf_b2b
    with app.app_context():
        cli = _cliente()
        p = _produto(sku='SKU-MES')
        ini, fim, venc = _periodo()
        _venda(cli, p, fim - timedelta(days=20), qtd=10)            # 10,00
        _venda(cli, p, fim - timedelta(days=10), qtd=5)             # 10,00
        _venda(cli, p, fim - timedelta(days=2), qtd=3, preco='9.00')
        fat = faturas_b2b.fechar_conta(cli, ini, fim, venc)
        with patch('app.services.tiny.incluir_nota_fiscal',
                   return_value={'ok': True, 'id': 'nf-fat',
                                 'numero': '11600'}) as inc, \
             patch('app.services.tiny.emitir_nota_fiscal',
                   return_value={'ok': True, 'status': 'autorizada'}):
            res = tiny_nf_b2b.emitir_nf_fatura(fat)
        assert res['ok']
        itens = sorted((i['item'] for i in inc.call_args[0][0]['itens']),
                       key=lambda i: i['valor_unitario'])
        assert len(itens) == 2                      # 2 preços = 2 linhas
        assert itens[0]['valor_unitario'] == 9.0
        assert itens[0]['quantidade'] == 3.0
        assert itens[1]['valor_unitario'] == 10.0
        assert itens[1]['quantidade'] == 15.0       # 10 + 5 somados
        assert inc.call_args[0][0]['cliente']['cpf_cnpj'] == '11222333000144'
        db.session.refresh(fat)
        assert fat.nf_emitida_em is not None
        assert fat.nf_numero == '11600'


def test_nf_fatura_sem_sku_aborta(app):
    from app.services import tiny_nf_b2b
    with app.app_context():
        cli = _cliente()
        p = _produto(sku=None)
        ini, fim, venc = _periodo()
        _venda(cli, p, fim - timedelta(days=1))
        fat = faturas_b2b.fechar_conta(cli, ini, fim, venc)
        with patch('app.services.tiny.incluir_nota_fiscal') as inc:
            res = tiny_nf_b2b.emitir_nf_fatura(fat)
        assert not res['ok'] and 'SKU B2B' in res['msg']
        inc.assert_not_called()


# ── boleto + liquidação ────────────────────────────────────────────────────

def test_liquidacao_do_boleto_quita_fatura_e_parcelas(app, admin_user):
    from app.services.sicredi_cnab import gerar_remessa, processar_retorno
    from tests.test_cobrancas_sicredi import _linha_retorno_tipo1
    with app.app_context():
        cli = _cliente()
        p = _produto()
        ini, fim, venc = _periodo()
        _venda(cli, p, fim - timedelta(days=20))
        _venda(cli, p, fim - timedelta(days=5), qtd=5)
        fat = faturas_b2b.fechar_conta(cli, ini, fim, venc)
        cob = Cobranca(fatura_id=fat.id, pagador_nome=cli.nome,
                       pagador_cnpj_cpf=cli.cnpj_cpf,
                       pagador_endereco='Rua das Laranjeiras 100',
                       pagador_cep='04568001', valor=fat.valor_total,
                       vencimento=venc, emissao=hoje(),
                       seu_numero=fat.codigo[:10])
        db.session.add(cob)
        db.session.commit()
        rem, erros = gerar_remessa([cob], user_id=admin_user.id)
        assert erros == []
        res = processar_retorno(_linha_retorno_tipo1(
            cob.nosso_numero, '06', valor_pago_centavos=15000) + '\r\n')
        assert res['pagas'] == 1
        db.session.refresh(fat)
        assert fat.status == 'paga' and fat.pago_em is not None
        for pc in fat.parcelas:
            assert pc.pago_em is not None
            assert pc.valor_pago == pc.valor
            assert pc.forma_pagamento == 'boleto'
        for v in fat.vendas:
            assert v.valor_aberto == Decimal('0')


# ── cancelamento ───────────────────────────────────────────────────────────

def test_cancelar_fatura_desfaz_vinculos(app):
    with app.app_context():
        cli = _cliente()
        p = _produto()
        ini, fim, venc = _periodo()
        v = _venda(cli, p, fim - timedelta(days=1))
        fat = faturas_b2b.fechar_conta(cli, ini, fim, venc)
        faturas_b2b.cancelar_fatura(fat)
        assert fat.status == 'cancelada'
        db.session.refresh(v)
        assert v.fatura_id is None
        assert VendaB2BParcela.query.filter_by(venda_id=v.id).count() == 0
        # As vendas voltam pro universo de fechamento
        assert len(faturas_b2b.vendas_para_fechar(cli.id, ini, fim)) == 1


def test_cancelar_fatura_recusa_com_nf_ou_pagamento(app):
    from app.utils import agora
    with app.app_context():
        cli = _cliente()
        p = _produto()
        ini, fim, venc = _periodo()
        _venda(cli, p, fim - timedelta(days=1))
        fat = faturas_b2b.fechar_conta(cli, ini, fim, venc)
        fat.nf_emitida_em = agora()
        db.session.commit()
        try:
            faturas_b2b.cancelar_fatura(fat)
            raise AssertionError('deveria recusar com NF emitida')
        except ValueError as exc:
            assert 'NF' in str(exc)
        fat.nf_emitida_em = None
        faturas_b2b.quitar_fatura(fat)
        db.session.commit()
        try:
            faturas_b2b.cancelar_fatura(fat)
            raise AssertionError('deveria recusar fatura paga')
        except ValueError as exc:
            assert 'paga' in str(exc)


# ── rotas ──────────────────────────────────────────────────────────────────

def _login(client, uid):
    with client.session_transaction() as s:
        s['_user_id'] = str(uid)
        s['_fresh'] = True


def test_rotas_fechar_detalhe_e_gerar_boleto(app, admin_user):
    with app.app_context():
        cli = _cliente()
        p = _produto()
        fim = hoje()
        _venda(cli, p, fim - timedelta(days=3))
        cid = cli.id
        ini_s = (fim - timedelta(days=30)).isoformat()
        fim_s = fim.isoformat()
        venc_s = (fim + timedelta(days=10)).isoformat()
    c = app.test_client()
    _login(c, admin_user.id)
    # Lista mostra a conta em aberto
    corpo = c.get('/b2b/faturas').get_data(as_text=True)
    assert 'Restaurante Bom Prato' in corpo
    # Fecha a conta
    r = c.post('/b2b/faturas/fechar',
               data={'cliente_id': cid, 'data_inicio': ini_s,
                     'data_fim': fim_s, 'vencimento': venc_s},
               follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        fat = FaturaB2B.query.one()
        fid = fat.id
        assert fat.valor_total == Decimal('100.00')
    # Detalhe renderiza
    assert 'FAT' in c.get(f'/b2b/faturas/{fid}').get_data(as_text=True)
    # Gera o boleto da fatura (cobrança única)
    r2 = c.post(f'/cobrancas/gerar-da-fatura/{fid}', follow_redirects=True)
    assert r2.status_code == 200
    with app.app_context():
        cob = Cobranca.query.filter_by(fatura_id=fid).one()
        assert cob.valor == Decimal('100.00')
        assert (cob.vencimento - cob.emissao).days >= 7   # regra Sicredi
        # duplicado é recusado
    r3 = c.post(f'/cobrancas/gerar-da-fatura/{fid}', follow_redirects=True)
    assert 'já tem cobrança' in r3.get_data(as_text=True)


# ── fixes da revisão (07/07/2026) ──────────────────────────────────────────

def test_criar_venda_de_cliente_mensal_nao_cria_parcela(app):
    """Achado nº 1 da revisão: `criar_venda` com parcelas=None criava a
    parcela única automática e tirava a venda do universo do fechamento —
    a feature nunca fechava conta nenhuma. Cliente mensal SEM parcelas
    explícitas fica sem parcela; cliente normal mantém a única; parcela
    explícita do mensal vale (exceção negociada, fora do fechamento)."""
    from app.services.vendas_b2b import criar_venda
    with app.app_context():
        mensal = _cliente()
        normal = ClienteB2B(nome='Avulso Ltda', ativo=True)
        p = _produto()
        db.session.add(normal)
        db.session.commit()
        item = [{'tipo': 'produto', 'id': p.id, 'quantidade': 2,
                 'preco_unitario': 10.0}]
        v1 = criar_venda(cliente_id=mensal.id, itens=item)
        assert v1.parcelas == []                      # vai pra conta do mês
        v2 = criar_venda(cliente_id=normal.id, itens=item)
        assert len(v2.parcelas) == 1                  # comportamento antigo
        v3 = criar_venda(cliente_id=mensal.id, itens=item,
                         parcelas=[{'vencimento': hoje(), 'valor': 20.0}])
        assert len(v3.parcelas) == 1                  # exceção negociada
        # E o fechamento pega SÓ a venda sem parcela
        fechaveis = faturas_b2b.vendas_para_fechar(
            mensal.id, hoje() - timedelta(days=1), hoje())
        assert [v.id for v in fechaveis] == [v1.id]


def test_venda_faturada_nao_cancela_nem_edita(app):
    """Achados nº 2 e 3: cancelar/editar venda dentro de fatura fechada
    dessincronizava fatura/boleto/NF (cobrança de venda morta, parcela
    órfã). Agora o service recusa — cancele a fatura primeiro."""
    from app.services.vendas_b2b import cancelar_venda, editar_venda
    with app.app_context():
        cli = _cliente()
        p = _produto()
        ini, fim, venc = _periodo()
        v = _venda(cli, p, fim - timedelta(days=1))
        faturas_b2b.fechar_conta(cli, ini, fim, venc)
        db.session.refresh(v)
        try:
            cancelar_venda(v)
            raise AssertionError('cancelar deveria recusar venda faturada')
        except ValueError as exc:
            assert 'faturada' in str(exc)
        try:
            editar_venda(v, cliente_id=cli.id,
                         itens=[{'tipo': 'produto', 'id': p.id,
                                 'quantidade': 1, 'preco_unitario': 5.0}])
            raise AssertionError('editar deveria recusar venda faturada')
        except ValueError as exc:
            assert 'faturada' in str(exc)
        assert v.status == 'ativa'                    # nada mudou


def test_parcela_de_fatura_nao_gera_boleto_individual(app, admin_user):
    """Achado nº 4: as parcelas do fechamento apareciam como candidatas a
    boleto individual em /cobrancas — cliente receberia o boleto do total
    E os boletos das mesmas vendas (dupla cobrança)."""
    with app.app_context():
        cli = _cliente()
        p = _produto()
        ini, fim, venc = _periodo()
        _venda(cli, p, fim - timedelta(days=1))
        fat = faturas_b2b.fechar_conta(cli, ini, fim, venc)
        pid = fat.parcelas[0].id
    c = app.test_client()
    _login(c, admin_user.id)
    corpo = c.get('/cobrancas/banco').get_data(as_text=True)
    assert 'Restaurante Bom Prato' not in corpo       # fora da lista
    r = c.post(f'/cobrancas/gerar-da-parcela/{pid}', follow_redirects=True)
    assert 'fatura mensal' in r.get_data(as_text=True)
    with app.app_context():
        assert Cobranca.query.filter_by(parcela_id=pid).count() == 0


def test_quitar_fatura_com_valor_divergente_avisa_e_rateia(app):
    """Achado nº 5: pagamento menor que o total era silenciosamente
    registrado como quitação cheia. Agora rateia o que ENTROU (última
    parcela fica parcial) e devolve aviso."""
    with app.app_context():
        cli = _cliente()
        p = _produto()
        ini, fim, venc = _periodo()
        _venda(cli, p, fim - timedelta(days=20))               # R$ 100
        _venda(cli, p, fim - timedelta(days=5), qtd=5)         # R$ 50
        fat = faturas_b2b.fechar_conta(cli, ini, fim, venc)
        aviso = faturas_b2b.quitar_fatura(fat, valor_pago=Decimal('120'))
        db.session.commit()
        assert aviso and 'difere' in aviso or 'R$' in aviso
        parcelas = sorted(fat.parcelas, key=lambda x: x.id)
        assert parcelas[0].valor_pago == Decimal('100')        # quitada
        assert parcelas[0].pago_em is not None
        assert parcelas[1].valor_pago == Decimal('20')         # parcial
        assert parcelas[1].pago_em is None                     # em aberto
        assert fat.status == 'paga'
