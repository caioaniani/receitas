"""Frete na venda B2B (20/07/2026, pedido do dono via Bruno).

`VendaB2B.frete_valor` soma no valor_total (parcela única/boleto/fatura
mensal herdam) e vai no campo `valor_frete` da NF do Tiny — mesmo padrão
da NF do site (o Tiny fecha o total da nota = Σ itens + valor_frete, então
NF e boleto saem no MESMO valor). Orçamento com frete aprova e o frete
viaja pra venda (regra antiga "embuta o frete" caiu — existia só porque a
venda não tinha o campo; a de DESCONTO continua). Venda paga trava o frete
junto com os itens.
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.extensions import db
from app.models import ClienteB2B, Produto, VendaB2B
from app.services import vendas_b2b
from app.utils import hoje


def _produto():
    p = Produto(nome='Pao Frete Congelado', ativo=True)
    db.session.add(p)
    db.session.commit()
    return p


def _cliente(nome='Restaurante Frete', **kw):
    c = ClienteB2B(nome=nome, ativo=True, **kw)
    db.session.add(c)
    db.session.commit()
    return c


def _cliente_nf():
    return _cliente(
        nome='Restaurante NF Frete', cnpj_cpf='11.222.333/0001-44',
        endereco_logradouro='Rua A', endereco_numero='1',
        endereco_bairro='Centro', endereco_cep='04568-001',
        endereco_cidade='São Paulo', endereco_uf='SP')


# ── serviço: total/parcela ───────────────────────────────────────────────────

def test_frete_soma_no_total_e_na_parcela_unica(app):
    p = _produto()
    v = vendas_b2b.criar_venda(
        cliente_nome='Avulso', data_entrega=hoje() + timedelta(days=1),
        itens=[{'tipo': 'produto', 'id': p.id, 'quantidade': 10,
                'preco_unitario': 10, 'desconto_percentual': 0}],
        frete_valor=25)
    assert v.frete_valor == Decimal('25.00')
    assert v.valor_total == Decimal('125.00')       # 10×10 + 25
    assert len(v.parcelas) == 1
    assert v.parcelas[0].valor == Decimal('125.00')  # boleto sai com frete


def test_frete_default_zero_nao_muda_nada(app):
    p = _produto()
    v = vendas_b2b.criar_venda(
        cliente_nome='Avulso', data_entrega=hoje() + timedelta(days=1),
        itens=[{'tipo': 'produto', 'id': p.id, 'quantidade': 10,
                'preco_unitario': 10, 'desconto_percentual': 0}])
    assert v.frete_valor == Decimal('0.00')
    assert v.valor_total == Decimal('100.00')


def test_frete_negativo_recusado(app):
    p = _produto()
    with pytest.raises(ValueError, match='negativo'):
        vendas_b2b.criar_venda(
            cliente_nome='Avulso', data_entrega=hoje() + timedelta(days=1),
            itens=[{'tipo': 'produto', 'id': p.id, 'quantidade': 1,
                    'preco_unitario': 10, 'desconto_percentual': 0}],
            frete_valor=-5)


def test_editar_venda_recalcula_frete_e_parcela(app):
    p = _produto()
    v = vendas_b2b.criar_venda(
        cliente_nome='Avulso', data_entrega=hoje() + timedelta(days=1),
        itens=[{'tipo': 'produto', 'id': p.id, 'quantidade': 10,
                'preco_unitario': 10, 'desconto_percentual': 0}],
        frete_valor=25)
    vendas_b2b.editar_venda(
        v, cliente_nome='Avulso', data_entrega=v.data_entrega,
        itens=[{'tipo': 'produto', 'id': p.id, 'quantidade': 10,
                'preco_unitario': 10, 'desconto_percentual': 0}],
        frete_valor=40)
    assert v.frete_valor == Decimal('40.00')
    assert v.valor_total == Decimal('140.00')
    assert v.parcelas[0].valor == Decimal('140.00')
    # Editar SEM frete zera (o form sempre manda o campo; ausência = 0)
    vendas_b2b.editar_venda(
        v, cliente_nome='Avulso', data_entrega=v.data_entrega,
        itens=[{'tipo': 'produto', 'id': p.id, 'quantidade': 10,
                'preco_unitario': 10, 'desconto_percentual': 0}])
    assert v.frete_valor == Decimal('0.00')
    assert v.valor_total == Decimal('100.00')


def test_venda_paga_nao_edita_frete(app):
    """Frete muda o total: venda com pagamento trava itens E frete (o
    caminho vira editar_cabecalho, que nem recebe frete)."""
    p = _produto()
    v = vendas_b2b.criar_venda(
        cliente_nome='Avulso', data_entrega=hoje() + timedelta(days=1),
        itens=[{'tipo': 'produto', 'id': p.id, 'quantidade': 10,
                'preco_unitario': 10, 'desconto_percentual': 0}],
        frete_valor=25)
    vendas_b2b.receber_pagamento(v.parcelas[0], Decimal('125.00'))
    with pytest.raises(ValueError, match='pagamento'):
        vendas_b2b.editar_venda(
            v, cliente_nome='Avulso', data_entrega=v.data_entrega,
            itens=[{'tipo': 'produto', 'id': p.id, 'quantidade': 10,
                    'preco_unitario': 10, 'desconto_percentual': 0}],
            frete_valor=99)


# ── NF via Tiny ──────────────────────────────────────────────────────────────

def _venda_nf(cliente, frete='30'):
    from app.services import tiny_nf
    p = _produto()
    tiny_nf.definir_sku('produto', p.id, 'SKU-FRETE', canal='b2b')
    v = vendas_b2b.criar_venda(
        cliente_id=cliente.id, data_entrega=hoje() + timedelta(days=1),
        itens=[{'tipo': 'produto', 'id': p.id, 'quantidade': 10,
                'preco_unitario': 10, 'desconto_percentual': 0}],
        frete_valor=Decimal(frete))
    return v


def test_nf_leva_o_frete_da_venda(app):
    """valor_frete no payload = frete da venda — o Tiny fecha o total da
    nota (itens + frete) igual ao valor_total/boleto."""
    from app.services import tiny_nf_b2b
    v = _venda_nf(_cliente_nf(), frete='30')
    with patch('app.services.tiny.incluir_nota_fiscal',
               return_value={'ok': True, 'id': 'nf-f1'}) as inc, \
         patch('app.services.tiny.emitir_nota_fiscal',
               return_value={'ok': True, 'status': 'autorizada'}):
        assert tiny_nf_b2b.emitir_nf(v)['ok']
    nota = inc.call_args[0][0]
    assert nota['valor_frete'] == 30.0
    assert nota['frete_por_conta'] == 'R'
    # Σ itens (100) + valor_frete (30) = valor_total da venda (130)
    soma_itens = sum(i['item']['quantidade'] * i['item']['valor_unitario']
                     for i in nota['itens'])
    assert soma_itens + nota['valor_frete'] == float(v.valor_total)


def test_nf_sem_frete_continua_zero(app):
    from app.services import tiny_nf_b2b
    v = _venda_nf(_cliente_nf(), frete='0')
    with patch('app.services.tiny.incluir_nota_fiscal',
               return_value={'ok': True, 'id': 'nf-f0'}) as inc, \
         patch('app.services.tiny.emitir_nota_fiscal',
               return_value={'ok': True, 'status': 'autorizada'}):
        assert tiny_nf_b2b.emitir_nf(v)['ok']
    assert inc.call_args[0][0]['valor_frete'] == 0.0


def test_nf_da_fatura_mensal_soma_os_fretes(app):
    """NF consolidada do fechamento: valor_frete = Σ fretes das vendas —
    fecha exato com o valor_total da fatura (que soma os totais c/ frete)."""
    from app.services import faturas_b2b, tiny_nf, tiny_nf_b2b
    cli = _cliente_nf()
    cli.faturamento_mensal = True
    db.session.commit()
    p = _produto()
    tiny_nf.definir_sku('produto', p.id, 'SKU-FAT', canal='b2b')
    for frete in ('10', '15'):
        vendas_b2b.criar_venda(
            cliente_id=cli.id, data_venda=hoje(),
            data_entrega=hoje() + timedelta(days=1),
            itens=[{'tipo': 'produto', 'id': p.id, 'quantidade': 5,
                    'preco_unitario': 10, 'desconto_percentual': 0}],
            frete_valor=Decimal(frete))
    fat = faturas_b2b.fechar_conta(cli, hoje() - timedelta(days=1),
                                   hoje() + timedelta(days=1),
                                   hoje() + timedelta(days=10))
    # Fatura soma os valor_total (que já incluem frete): 2×(50+X) = 125
    assert fat.valor_total == Decimal('125.00')
    with patch('app.services.tiny.incluir_nota_fiscal',
               return_value={'ok': True, 'id': 'nf-fat'}) as inc, \
         patch('app.services.tiny.emitir_nota_fiscal',
               return_value={'ok': True, 'status': 'autorizada'}):
        assert tiny_nf_b2b.emitir_nf_fatura(fat)['ok']
    nota = inc.call_args[0][0]
    assert nota['valor_frete'] == 25.0              # 10 + 15
    soma_itens = sum(i['item']['quantidade'] * i['item']['valor_unitario']
                     for i in nota['itens'])
    assert soma_itens + nota['valor_frete'] == float(fat.valor_total)


# ── web: form de venda ───────────────────────────────────────────────────────

def _login(client, uid):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)
        sess['_fresh'] = True


def test_form_web_cria_venda_com_frete(app, admin_user):
    p = _produto()
    c = app.test_client()
    _login(c, admin_user.id)
    r = c.post('/b2b/vendas/nova', data={
        'cliente_nome': 'Avulso Web',
        'data_venda': hoje().isoformat(),
        'data_entrega': (hoje() + timedelta(days=1)).isoformat(),
        'frete_valor': '12,50',                     # vírgula pt-BR
        'item_ref[]': [f'produto:{p.id}'],
        'item_qtd[]': ['4'],
        'item_preco[]': ['10'],
        'item_desc[]': ['0'],
        'item_estado[]': [''],
        'item_obs[]': [''],
    })
    assert r.status_code == 302
    v = VendaB2B.query.order_by(VendaB2B.id.desc()).first()
    assert v.frete_valor == Decimal('12.50')
    assert v.valor_total == Decimal('52.50')


def test_editar_venda_com_boleto_no_banco_recusa(app):
    """Boleto que já foi ao banco trava a edição (o valor do título não
    pode mudar por baixo); boleto PENDENTE é apagado junto — sem Cobranca
    órfã com valor velho (achado crítico da revisão 20/07/2026)."""
    from app.models import Cobranca
    p = _produto()
    v = vendas_b2b.criar_venda(
        cliente_nome='Avulso', data_entrega=hoje() + timedelta(days=1),
        itens=[{'tipo': 'produto', 'id': p.id, 'quantidade': 10,
                'preco_unitario': 10, 'desconto_percentual': 0}],
        frete_valor=25)
    cob = Cobranca(parcela_id=v.parcelas[0].id, pagador_nome='X',
                   pagador_cnpj_cpf='', pagador_endereco='', pagador_cep='',
                   valor=v.parcelas[0].valor, vencimento=v.data_venda,
                   emissao=v.data_venda, seu_numero='T1', status='registrada')
    db.session.add(cob)
    db.session.commit()
    itens = [{'tipo': 'produto', 'id': p.id, 'quantidade': 10,
              'preco_unitario': 10, 'desconto_percentual': 0}]
    with pytest.raises(ValueError, match='banco'):
        vendas_b2b.editar_venda(v, cliente_nome='Avulso',
                                data_entrega=v.data_entrega,
                                itens=itens, frete_valor=40)
    # Pendente: edição passa e a cobrança velha SOME (gera-se outra depois)
    cob.status = 'pendente'
    db.session.commit()
    vendas_b2b.editar_venda(v, cliente_nome='Avulso',
                            data_entrega=v.data_entrega,
                            itens=itens, frete_valor=40)
    assert Cobranca.query.count() == 0
    assert v.parcelas[0].valor == Decimal('140.00')


def test_frete_infinito_vira_erro_tratado(app):
    """POST forjado com inf/nan não pode virar 500 (InvalidOperation)."""
    p = _produto()
    with pytest.raises(ValueError, match='invalido'):
        vendas_b2b.criar_venda(
            cliente_nome='Avulso', data_entrega=hoje() + timedelta(days=1),
            itens=[{'tipo': 'produto', 'id': p.id, 'quantidade': 1,
                    'preco_unitario': 10, 'desconto_percentual': 0}],
            frete_valor=float('inf'))


def test_form_web_frete_invalido_avisa_em_vez_de_zerar(app, admin_user):
    """Frete ilegível no form NÃO vira R$ 0 calado — flash de erro e nada
    é criado (convenção: dinheiro nunca zera em silêncio)."""
    p = _produto()
    c = app.test_client()
    _login(c, admin_user.id)
    antes = VendaB2B.query.count()
    r = c.post('/b2b/vendas/nova', data={
        'cliente_nome': 'Avulso Web',
        'data_venda': hoje().isoformat(),
        'data_entrega': (hoje() + timedelta(days=1)).isoformat(),
        'frete_valor': 'abc',
        'item_ref[]': [f'produto:{p.id}'],
        'item_qtd[]': ['4'],
        'item_preco[]': ['10'],
        'item_desc[]': ['0'],
        'item_estado[]': [''],
        'item_obs[]': [''],
    }, follow_redirects=True)
    assert r.status_code == 200
    assert VendaB2B.query.count() == antes          # nada criado


def test_parcela_de_venda_cancelada_nao_vira_boleto(app, admin_user):
    from app.models import Cobranca
    p = _produto()
    v = vendas_b2b.criar_venda(
        cliente_nome='Avulso', data_entrega=hoje() + timedelta(days=1),
        itens=[{'tipo': 'produto', 'id': p.id, 'quantidade': 10,
                'preco_unitario': 10, 'desconto_percentual': 0}])
    vendas_b2b.cancelar_venda(v)
    c = app.test_client()
    _login(c, admin_user.id)
    r = c.post(f'/cobrancas/gerar-da-parcela/{v.parcelas[0].id}',
               follow_redirects=True)
    assert r.status_code == 200
    assert Cobranca.query.count() == 0


def test_copilot_criar_venda_b2b_com_frete(app, admin_user):
    from app.services.copilot import executar_criar_venda_b2b
    p = _produto()
    res = executar_criar_venda_b2b({
        'cliente_nome': 'Avulso Bot',
        'data_entrega': (hoje() + timedelta(days=1)).isoformat(),
        'frete_valor': 8,
        'itens': [{'nome': p.nome, 'quantidade': 2, 'preco_unitario': 10}],
    }, admin_user)
    assert res['ok'], res
    v = db.session.get(VendaB2B, res['venda_id'])
    assert v.frete_valor == Decimal('8.00')
    assert v.valor_total == Decimal('28.00')
    assert res['frete_valor'] == Decimal('8.00')
