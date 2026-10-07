"""Minis fora do pedido loja→indústria (dono, 07/10/2026)."""
from datetime import datetime, time, timedelta

from app.extensions import db
from app.migrations_legacy import _seed_minis_fora_pedido_loja
from app.models import AppConfig, EstoqueLoja, PedidoItem, PedidoLoja, Receita
from app.services import minis_pedido_loja as svc
from app.utils import hoje


def _rec(nome):
    r = Receita(nome=nome, categoria='Mini Pães', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0)
    db.session.add(r)
    db.session.flush()
    return r


def _pedido(loja, dias, itens):
    p = PedidoLoja(loja_id=loja.id, data_entrega=hoje() + timedelta(days=dias),
                   status='confirmado', observacao='Gerado do histórico (rascunho)')
    db.session.add(p)
    db.session.flush()
    for r, q in itens:
        db.session.add(PedidoItem(pedido_id=p.id, receita_id=r.id, quantidade=q))
    db.session.commit()
    return p


def test_motor_deixa_de_pedir_mini_com_piso(app, loja):
    from app.services.previsao_producao import sugerir_pedidos_por_venda
    mini = _rec('Mini Danish de Calabresa')
    danish = _rec('Danish de Calabresa')
    for r in (mini, danish):
        db.session.add(EstoqueLoja(loja_id=loja.id, receita_id=r.id, quantidade=0,
                                   pedido_minimo_diario=2))
    db.session.commit()
    r = svc.desmarcar_minis()
    db.session.commit()
    assert r == {'desmarcados': ['Mini Danish de Calabresa'], 'pisos_zerados': 1}
    sug = sugerir_pedidos_por_venda(horizonte_dias=2, inicio_offset_dias=1)
    lj = next(x for x in sug['lojas'] if x['loja_id'] == loja.id)
    ids = {p['receita_id']: p['por_dia'] for p in lj['produtos']}
    assert mini.id not in ids and ids[danish.id] == [2, 2]


def test_seed_remove_minis_dos_pedidos_futuros_respeitando_corte(app, loja, monkeypatch):
    from app.services import pedido_corte
    monkeypatch.setattr(pedido_corte, 'agora',
                        lambda: datetime.combine(hoje(), time(19)))  # D+1 fechado
    mini = _rec('Mini Danish de Calabresa')
    danish = _rec('Danish de Calabresa')
    amanha = _pedido(loja, 1, [(mini, 2), (danish, 2)])
    misto = _pedido(loja, 2, [(mini, 2), (danish, 2)])
    so_mini = _pedido(loja, 3, [(mini, 2)])
    _seed_minis_fora_pedido_loja(app)
    db.session.expire_all()
    assert db.session.get(Receita, mini.id).sugerir_pedido_loja is False
    assert db.session.get(Receita, danish.id).sugerir_pedido_loja is True
    assert len(db.session.get(PedidoLoja, amanha.id).itens) == 2  # sob o corte
    assert [i.receita_id for i in db.session.get(PedidoLoja, misto.id).itens] == [danish.id]
    assert db.session.get(PedidoLoja, so_mini.id).status == 'cancelado'
    marker = AppConfig.get('seed_minis_fora_pedido_loja_2026_10')
    assert 'itens_removidos=2' in marker and 'pulados_corte=Ribeiro do Vale' in marker
    _seed_minis_fora_pedido_loja(app)  # uma vez só
    assert len(db.session.get(PedidoLoja, amanha.id).itens) == 2
