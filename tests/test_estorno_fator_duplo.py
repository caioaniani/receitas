"""Estorno de venda fracionaria (item de chapa): cancelar o MESMO pedido
que disparou a baixa do inteiro NAO pode devolver em dobro.

Bug real provado e corrigido em 2026-06-10: a fase 1 do estorno revertia o
MovEstoqueLoja '(fator X)' E a fase 2 devolvia de novo via acumulador
negativo — estoque da loja inflava 1 pao por cancelamento. Fix: movs com
'(fator' sao estornados SO pela fase 2 (SeruDebitoMov)."""


def test_estorno_do_pedido_que_baixou_inteiro_nao_duplica(app):
    from app.extensions import db
    from app.models import (
        EstoqueLoja,
        Loja,
        Receita,
        SeruLojaMap,
        SeruPedidoProcessado,
        SeruProdutoMap,
    )
    from app.services import seru_sync
    from app.utils import agora

    loja = Loja(nome='Loja X', ativa=True)
    r = Receita(nome='Sourdough', categoria='Paes', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0)
    db.session.add_all([loja, r])
    db.session.flush()
    el = EstoqueLoja(loja_id=loja.id, receita_id=r.id, quantidade=10)
    m = SeruProdutoMap(seru_nome='MISTO', receita_id=r.id,
                       fator_quantidade=0.2, confirmado_em=agora())
    lm = SeruLojaMap(seru_company_name='Loja X', loja_id=loja.id,
                     confirmado_em=agora())
    db.session.add_all([el, m, lm])
    db.session.commit()

    # Venda de 5 MISTOS num pedido = 5*0.2 = 1.0 -> baixa 1 pao inteiro
    res = seru_sync._baixar_item(loja.id, m, 5, 'PED-1', None)
    db.session.commit()
    assert res['baixado'] is True
    db.session.refresh(el)
    assert el.quantidade == 9          # baixou 1

    # Cancela o MESMO pedido
    reg = SeruPedidoProcessado(seru_pedido_id='PED-1', loja_id=loja.id)
    db.session.add(reg)
    db.session.commit()
    seru_sync._estornar_pedido(reg, {loja.id}, None)
    db.session.commit()
    db.session.refresh(el)

    # Esperado: estoque volta EXATAMENTE pra 10 (devolve 1, nao 2)
    assert el.quantidade == 10, (
        f'DUPLA DEVOLUCAO: estoque foi pra {el.quantidade} (esperado 10)')
