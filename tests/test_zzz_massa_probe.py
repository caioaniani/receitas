"""PROBE temporário — massa não soma croissant + danishes. APAGAR depois."""
from datetime import timedelta

from app.extensions import db
from app.models import Loja, PedidoItem, PedidoLoja, Receita, ReceitaIngrediente
from app.services.previsao_producao import cronograma_producao
from app.utils import hoje


def _amass(nome, rend, cap=5000, peso=5000):
    r = Receita(nome=nome, categoria='Paes', rendimento_qtd=rend,
                rendimento_unidade='un', peso_base=float(peso),
                capacidade_amassadeira_g=cap)
    db.session.add(r)
    db.session.flush()
    db.session.add(ReceitaIngrediente(receita_id=r.id, tipo='mp',
                                      ingrediente_nome='Farinha', porcentagem=100))
    return r


def test_probe_massa(app):
    loja = Loja(nome='Loja', ativa=True)
    massa = _amass('Massa para folhar', rend=50)      # insumo
    massa.dias_producao = 1
    cro = _amass('Croissant Tradicional', rend=50)    # 1 massa -> 50 croissants
    cro.dias_producao = 2
    dan = _amass('Danish de Calabresa', rend=31)      # 1 massa -> 31 danishes
    dan.dias_producao = 1
    db.session.add_all([loja])
    db.session.flush()
    # cada final consome massa (sub-receita), porcentagem 1
    db.session.add_all([
        ReceitaIngrediente(receita_id=cro.id, tipo='receita',
                           sub_receita_id=massa.id,
                           ingrediente_nome='Massa para folhar', porcentagem=1),
        ReceitaIngrediente(receita_id=dan.id, tipo='receita',
                           sub_receita_id=massa.id,
                           ingrediente_nome='Massa para folhar', porcentagem=1),
    ])
    db.session.commit()

    d = hoje() + timedelta(days=2)
    for r, q in ((cro, 500), (dan, 62)):              # firme
        p = PedidoLoja(loja_id=loja.id, status='pendente', data_entrega=d,
                       data_pedido=d)
        db.session.add(p)
        db.session.flush()
        db.session.add(PedidoItem(pedido_id=p.id, receita_id=r.id, quantidade=q))
    db.session.commit()

    crono = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0)
    rc = next(x for x in crono['receitas'] if x['receita_id'] == cro.id)
    rd = next(x for x in crono['receitas'] if x['receita_id'] == dan.id)
    rm = next((x for x in crono['receitas'] if x['receita_id'] == massa.id), None)
    print(f"\n>>> croissant total={rc['total']}  danish total={rd['total']}")
    print(f">>> MASSA total={rm['total'] if rm else None}")
    print(f">>> breakdown_bom={rm['breakdown_bom'] if rm else None}")
    # esperado: 500/50 + 62/31 = 10 + 2 = 12 massa
