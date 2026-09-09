"""O preço-base e o desconto aprovado devem sobreviver à consulta e ao seed."""
import json
import re
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import ClienteB2B, Orcamento, OrcamentoItem, Receita
from app.utils import hoje


def _preparar(app, owner_user, percentual):
    cliente = ClienteB2B(nome='Cliente com outro desconto', desconto_percentual=35)
    receita = Receita(nome='Mini do orçamento', rendimento_qtd=1,
                      rendimento_unidade='un', peso_base=100, preco_venda=20)
    db.session.add_all([cliente, receita])
    db.session.flush()
    orc = Orcamento(codigo='ORC-TESTE', data=hoje(), valido_ate=hoje(),
                    data_entrega=hoje(), cliente_id=cliente.id,
                    status='aprovado')
    item = OrcamentoItem(nome=receita.nome, receita_id=receita.id,
                         quantidade=30, preco_unitario=Decimal('6.00'),
                         desconto_percentual=percentual)
    item.recalcular_subtotal()
    orc.itens.append(item)
    orc.recalcular_total()
    db.session.add(orc)
    db.session.commit()
    client = app.test_client()
    with client.session_transaction() as sessao:
        sessao['_user_id'] = str(owner_user.id)
        sessao['_fresh'] = True
    return client, orc


@pytest.mark.parametrize('percentual,subtotal', [(0, '180.00'), (12.3, '157.86')])
def test_detalhe_explicita_desconto_sem_esconder_preco_base(
        app, owner_user, percentual, subtotal):
    client, orc = _preparar(app, owner_user, percentual)
    response = client.get(f'/b2b/orcamentos/{orc.id}')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    linha = html.split('Mini do orçamento</td>', 1)[1].split('</tr>', 1)[0]
    assert 'R$ 6.00' in linha
    assert f'R$ {subtotal}' in linha
    assert ('12,3% de desconto' in linha) == bool(percentual)
    if not percentual:
        assert '% de desconto' not in linha


@pytest.mark.parametrize('percentual', [0, 12.3])
def test_virar_venda_copia_percentual_do_orcamento_sem_usar_desconto_do_cliente(
        app, owner_user, percentual):
    client, orc = _preparar(app, owner_user, percentual)
    response = client.get(f'/b2b/vendas/nova?orcamento={orc.id}')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    seed = json.loads(re.search(r'var SEED_ITENS = (.*?);', html).group(1))
    assert len(seed) == 1
    assert seed[0]['qtd'] == 30
    assert seed[0]['preco'] == 6.0
    assert seed[0]['desc'] == percentual
