"""PROBE temporário — confirma o double-count do A1. APAGAR depois."""
from datetime import timedelta

from app.extensions import db
from app.models import Loja, PedidoItem, PedidoLoja, Receita
from app.services.previsao_producao import balanco_industria
from app.utils import hoje


def _sabados(n):
    """As n datas de sábado mais recentes (dow=5) antes de hoje."""
    d = hoje() - timedelta(days=1)
    while d.weekday() != 5:
        d -= timedelta(days=1)
    out = []
    for _ in range(n):
        out.append(d)
        d -= timedelta(days=7)
    return out


def test_probe_weekend_only(app):
    loja = Loja(nome='Loja W', ativa=True)
    r = Receita(nome='Pão de Sábado', categoria='X', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0)
    db.session.add_all([loja, r])
    db.session.commit()
    for s in _sabados(6):                      # 6 sábados, 100 cada. NADA em dia útil.
        p = PedidoLoja(loja_id=loja.id, status='recebido',
                       data_entrega=s, data_pedido=s)
        db.session.add(p)
        db.session.flush()
        db.session.add(PedidoItem(pedido_id=p.id, receita_id=r.id, quantidade=100))
    db.session.commit()

    # horizonte de 7 dias cobre 1 sábado. Demanda real da semana = 100.
    bal = balanco_industria(horizonte_dias=7, janela_semanas=6, usar_cache=False)
    it = next(i for i in bal['itens'] if i['receita_id'] == r.id)
    print(f"\n>>> PREVISTO 7d (só sábados 100): {it['previsto']}  (esperado ~100)")
