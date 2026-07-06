"""Tabela de preço por cliente B2B (06/07/2026).

O atacado cobra valores diferentes por cliente — `PrecoClienteB2B` guarda o
preço específico, que VENCE o atacado padrão (e não leva o desconto % em
cima). Sem linha = comportamento antigo (atacado × (1 − desconto)).
"""
from datetime import timedelta
from decimal import Decimal

from app.extensions import db
from app.models import (
    ClienteB2B,
    PrecoClienteB2B,
    Produto,
    Receita,
    VendaB2B,
    VendaB2BItem,
)
from app.services.vendas_b2b import preco_sugerido
from app.utils import hoje


def _cliente(desconto=0):
    c = ClienteB2B(nome='Restaurante Bom Prato', ativo=True,
                   desconto_percentual=desconto)
    db.session.add(c)
    db.session.commit()
    return c


def _receita(preco_venda=10.0):
    r = Receita(nome='Pao de Atacado', categoria='Paes', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0,
                preco_venda=preco_venda)
    db.session.add(r)
    db.session.commit()
    return r


def test_preco_especifico_vence_atacado_e_desconto(app):
    with app.app_context():
        cli = _cliente(desconto=20)          # 10,00 − 20% daria 8,00
        r = _receita(10.0)
        db.session.add(PrecoClienteB2B(cliente_id=cli.id, kind='receita',
                                       item_id=r.id, preco=Decimal('7.50')))
        db.session.commit()
        assert preco_sugerido(receita_id=r.id, cliente=cli) == 7.50


def test_sem_preco_especifico_cai_no_atacado_com_desconto(app):
    with app.app_context():
        cli = _cliente(desconto=20)
        r = _receita(10.0)
        assert preco_sugerido(receita_id=r.id, cliente=cli) == 8.00
        assert preco_sugerido(receita_id=r.id) == 10.00   # sem cliente


def test_preco_especifico_de_produto(app):
    with app.app_context():
        cli = _cliente()
        p = Produto(nome='Cesta Corporativa', ativo=True, preco_atacado=50.0)
        db.session.add(p)
        db.session.flush()
        db.session.add(PrecoClienteB2B(cliente_id=cli.id, kind='produto',
                                       item_id=p.id, preco=Decimal('44.90')))
        db.session.commit()
        assert preco_sugerido(produto_id=p.id, cliente=cli) == 44.90
        # Outro cliente NÃO herda o preço específico
        outro = ClienteB2B(nome='Outro', ativo=True)
        db.session.add(outro)
        db.session.commit()
        assert preco_sugerido(produto_id=p.id, cliente=outro) == 50.00


def _login(client, uid):
    with client.session_transaction() as s:
        s['_user_id'] = str(uid)
        s['_fresh'] = True


def test_tela_precos_salva_atualiza_e_remove(app, admin_user):
    with app.app_context():
        cli = _cliente(desconto=10)
        r = _receita(10.0)
        cid, rid = cli.id, r.id
    c = app.test_client()
    _login(c, admin_user.id)
    # GET renderiza com o item do atacado
    resp = c.get(f'/b2b/clientes/{cid}/precos')
    assert resp.status_code == 200
    assert 'Pao de Atacado' in resp.get_data(as_text=True)
    # Salva preço específico (aceita vírgula)
    c.post(f'/b2b/clientes/{cid}/precos',
           data={f'preco[receita:{rid}]': '7,50'}, follow_redirects=True)
    with app.app_context():
        linha = PrecoClienteB2B.query.filter_by(cliente_id=cid).one()
        assert linha.preco == Decimal('7.50')
    # Atualiza
    c.post(f'/b2b/clientes/{cid}/precos',
           data={f'preco[receita:{rid}]': '7.90'}, follow_redirects=True)
    with app.app_context():
        assert (PrecoClienteB2B.query.filter_by(cliente_id=cid).one().preco
                == Decimal('7.90'))
    # Vazio remove — volta pro padrão
    c.post(f'/b2b/clientes/{cid}/precos',
           data={f'preco[receita:{rid}]': ''}, follow_redirects=True)
    with app.app_context():
        assert PrecoClienteB2B.query.filter_by(cliente_id=cid).count() == 0


def test_tela_precos_mostra_ultimo_vendido(app, admin_user):
    with app.app_context():
        cli = _cliente()
        r = _receita(10.0)
        v = VendaB2B(cliente_id=cli.id, valor_total=Decimal('9.00'),
                     data_venda=hoje() - timedelta(days=3))
        db.session.add(v)
        db.session.flush()
        db.session.add(VendaB2BItem(venda_id=v.id, receita_id=r.id,
                                    quantidade=1,
                                    preco_unitario=Decimal('9.00')))
        db.session.commit()
        cid = cli.id
    c = app.test_client()
    _login(c, admin_user.id)
    corpo = c.get(f'/b2b/clientes/{cid}/precos').get_data(as_text=True)
    assert 'R$ 9.00' in corpo or 'R$ 9,00' in corpo


def test_form_venda_embute_precos_do_cliente(app, admin_user):
    """O form de venda leva o mapa {cliente: {ref: preco}} pro JS pré-
    preencher com o preço do cliente selecionado."""
    with app.app_context():
        cli = _cliente()
        r = _receita(10.0)
        db.session.add(PrecoClienteB2B(cliente_id=cli.id, kind='receita',
                                       item_id=r.id, preco=Decimal('7.50')))
        db.session.commit()
        cid, rid = cli.id, r.id
    c = app.test_client()
    _login(c, admin_user.id)
    corpo = c.get('/b2b/vendas/nova').get_data(as_text=True)
    assert 'PRECOS_CLIENTE' in corpo
    assert f'"receita:{rid}": 7.5' in corpo.replace("'", '"')
    assert f'"{cid}"' in corpo   # chave do cliente no JSON
