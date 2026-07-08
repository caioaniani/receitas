"""Painel B2B em abas (07/07/2026, esboço do dono): PEDIDOS (ciclo
orçamento → produção → entrega) separado de COBRANÇAS (pendentes /
vencidas / pagas, com filtro por data).
"""
from datetime import timedelta
from decimal import Decimal

from app.extensions import db
from app.models import (
    ClienteB2B,
    Orcamento,
    OrcamentoItem,
    Produto,
    VendaB2B,
    VendaB2BParcela,
)
from app.utils import hoje


def _login(client, uid):
    with client.session_transaction() as s:
        s['_user_id'] = str(uid)
        s['_fresh'] = True


def _cliente(nome='Restaurante Bom Prato'):
    c = ClienteB2B(nome=nome, ativo=True)
    db.session.add(c)
    db.session.commit()
    return c


def _orcamento(cli, status='enviado', codigo='ORC-2026-0001'):
    o = Orcamento(codigo=codigo, cliente_id=cli.id, status=status,
                  subtotal=Decimal('50'), valor_total=Decimal('50'))
    db.session.add(o)
    db.session.commit()
    return o


def _venda(cli, status_entrega='pendente', status='ativa',
           venc_dias=None, pago=False):
    v = VendaB2B(cliente_id=cli.id, valor_total=Decimal('100.00'),
                 status=status, status_entrega=status_entrega)
    db.session.add(v)
    db.session.flush()
    if venc_dias is not None:
        p = VendaB2BParcela(venda_id=v.id, numero=1,
                            vencimento=hoje() + timedelta(days=venc_dias),
                            valor=Decimal('100.00'))
        if pago:
            from app.utils import agora
            p.valor_pago = Decimal('100.00')
            p.pago_em = agora()
        db.session.add(p)
    db.session.commit()
    return v


def test_abas_pedidos_separam_por_estagio(app, admin_user):
    with app.app_context():
        cli = _cliente()
        _orcamento(cli, 'enviado', 'ORC-2026-0001')          # pendente
        _orcamento(cli, 'aprovado', 'ORC-2026-0002')         # aprovado
        _orcamento(cli, 'recusado', 'ORC-2026-0003')         # arquivado
        _venda(cli, status_entrega='pendente')               # produção
        _venda(cli, status_entrega='entregue')               # entregue
        _venda(cli, status='cancelada')                      # arquivado
    c = app.test_client()
    _login(c, admin_user.id)
    pend = c.get('/b2b/?aba=pedidos&f=pendentes').get_data(as_text=True)
    assert 'ORC-2026-0001' in pend and 'ORC-2026-0002' not in pend
    aprov = c.get('/b2b/?aba=pedidos&f=aprovados').get_data(as_text=True)
    assert 'ORC-2026-0002' in aprov and 'Virar venda' in aprov
    prod = c.get('/b2b/?aba=pedidos&f=producao').get_data(as_text=True)
    assert '#1' in prod and 'entregue' not in prod.split('Entregues')[0].split('badge bg-success')[0]
    entr = c.get('/b2b/?aba=pedidos&f=entregues').get_data(as_text=True)
    assert '#2' in entr
    arq = c.get('/b2b/?aba=pedidos&f=arquivados').get_data(as_text=True)
    assert 'ORC-2026-0003' in arq and '#3' in arq


def test_abas_cobrancas_pendente_vencido_pago(app, admin_user):
    with app.app_context():
        cli = _cliente()
        _venda(cli, venc_dias=5)                 # pendente
        _venda(cli, venc_dias=-3)                # vencida
        _venda(cli, venc_dias=-1, pago=True)     # paga
    c = app.test_client()
    _login(c, admin_user.id)
    pend = c.get('/b2b/?aba=cobrancas&f=cob_pendentes').get_data(as_text=True)
    assert 'venda #1' in pend
    assert 'venda #2' not in pend and 'venda #3' not in pend
    venc = c.get('/b2b/?aba=cobrancas&f=cob_vencidos').get_data(as_text=True)
    assert 'venda #2' in venc and 'venda #1' not in venc
    pagos = c.get('/b2b/?aba=cobrancas&f=cob_pagos').get_data(as_text=True)
    assert 'venda #3' in pagos and 'venda #1' not in pagos


def test_cobrancas_filtro_por_data(app, admin_user):
    with app.app_context():
        cli = _cliente()
        _venda(cli, venc_dias=5)                 # dentro do filtro
        _venda(cli, venc_dias=40)                # fora
        de = hoje().isoformat()
        ate = (hoje() + timedelta(days=10)).isoformat()
    c = app.test_client()
    _login(c, admin_user.id)
    corpo = c.get(f'/b2b/?aba=cobrancas&f=cob_pendentes&de={de}&ate={ate}'
                  ).get_data(as_text=True)
    assert 'venda #1' in corpo and 'venda #2' not in corpo


def test_virar_venda_preenche_form_do_orcamento(app, admin_user):
    with app.app_context():
        cli = _cliente()
        p = Produto(nome='Pao Frances Congelado', ativo=True)
        db.session.add(p)
        db.session.flush()
        o = _orcamento(cli, 'aprovado')
        db.session.add(OrcamentoItem(orcamento_id=o.id, produto_id=p.id,
                                     nome=p.nome, quantidade=Decimal('12'),
                                     preco_unitario=Decimal('7.50'),
                                     subtotal=Decimal('90')))
        db.session.add(OrcamentoItem(orcamento_id=o.id, nome='Buffet avulso',
                                     quantidade=Decimal('1'),
                                     preco_unitario=Decimal('200'),
                                     subtotal=Decimal('200')))
        db.session.commit()
        oid, pid, cid = o.id, p.id, cli.id
    c = app.test_client()
    _login(c, admin_user.id)
    corpo = c.get(f'/b2b/vendas/nova?orcamento={oid}').get_data(as_text=True)
    assert f'"ref": "produto:{pid}"' in corpo.replace("'", '"')
    assert '"qtd": 12' in corpo.replace("'", '"')
    assert '"preco": 7.5' in corpo.replace("'", '"')
    assert 'linha livre' in corpo                     # aviso do item pulado
    assert 'Buffet avulso' in corpo                   # citado no aviso
    # cliente pré-selecionado no select
    assert f'value="{cid}"' in corpo
