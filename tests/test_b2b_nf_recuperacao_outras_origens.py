"""NF já vinculada ao site ou a uma transferência não pode virar NF B2B."""
from unittest.mock import patch

from app.extensions import db
from app.models import Cliente, PedidoLoja, PedidoOnline
from app.services import cobrancas_nf
from tests.test_b2b_nf_recuperacao import _nota, _sem_vinculo


def _confere_bloqueio(doc, tentativa, pedido):
    erro_anterior = tentativa.erro
    assinatura_anterior = tentativa.assinatura
    with patch('app.services.tiny.pesquisar_notas_fiscais',
               return_value=[{'id': '911314754'}]), \
            patch('app.services.tiny.obter_nota_fiscal', return_value=_nota()), \
            patch('app.services.tiny.incluir_nota_fiscal') as incluir, \
            patch('app.services.tiny.emitir_nota_fiscal') as emitir:
        resultado = cobrancas_nf.sincronizar(doc)
    assert not resultado['ok']
    assert 'já está vinculada a outra venda, fatura ou pedido' in resultado['msg']
    db.session.refresh(doc)
    db.session.refresh(tentativa)
    db.session.refresh(pedido)
    assert doc.tiny_nota_fiscal_id is None
    assert doc.nf_emitida_em is None
    assert doc.nf_numero == '012693'
    assert tentativa.erro == erro_anterior
    assert tentativa.assinatura == assinatura_anterior
    assert pedido.tiny_nota_fiscal_id == '911314754'
    incluir.assert_not_called()
    emitir.assert_not_called()


def test_nao_recupera_nf_ja_vinculada_a_pedido_online(app):
    doc, tentativa = _sem_vinculo()
    cliente = Cliente(nome=doc.cliente.nome, email='lavo@example.com',
                      cpf='40099645000148')
    db.session.add(cliente)
    db.session.flush()
    pedido = PedidoOnline(
        cliente_id=cliente.id, nome_cliente=cliente.nome, email_cliente=cliente.email,
        modo_entrega='retirada', status='pago', valor_total=doc.valor_total,
        tiny_nota_fiscal_id='911314754')
    db.session.add(pedido)
    db.session.commit()
    _confere_bloqueio(doc, tentativa, pedido)


def test_nao_recupera_nf_ja_vinculada_a_pedido_loja(app, loja):
    doc, tentativa = _sem_vinculo()
    loja.cnpj = doc.cliente.cnpj_cpf
    pedido = PedidoLoja(loja_id=loja.id, status='entregue',
                       tiny_nota_fiscal_id='911314754', nf_numero='12693')
    db.session.add(pedido)
    db.session.commit()
    _confere_bloqueio(doc, tentativa, pedido)
