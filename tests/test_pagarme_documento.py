"""Documento (CPF/CNPJ) no customer do Pagar.me (incidente 23/07/2026).

Bug original: o código mandava o `document` como CPF pra QUALQUER tamanho ≠ 14.
Cliente que digitou CPF faltando/sobrando dígito fazia o Pagar.me RECUSAR a
cobrança inteira com "The request is invalid" — 8 tentativas (pix+cartão)
falhando ANTES de o pedido nascer no gateway (pedido 12a2f73d, R$527). O
documento é OPCIONAL: só vai se for CPF(11) ou CNPJ(14); tamanho errado é
OMITIDO, nunca enviado sujo (mesma defesa do telefone).
"""
from types import SimpleNamespace

from app.services.pagarme import _payload_customer


def _pedido(cpf=None, nome='João Silva', email='j@x.com', tel=None):
    cli = SimpleNamespace(cpf=cpf) if cpf is not None else None
    return SimpleNamespace(nome_cliente=nome, email_cliente=email,
                           telefone_cliente=tel, cliente=cli)


def test_cpf_valido_vai_como_individual():
    c = _payload_customer(_pedido(cpf='123.456.789-09'))
    assert c['document'] == '12345678909'
    assert c['document_type'] == 'cpf' and c['type'] == 'individual'


def test_cnpj_valido_vai_como_company():
    c = _payload_customer(_pedido(cpf='11.222.333/0001-81'))
    assert c['document'] == '11222333000181'
    assert c['document_type'] == 'cnpj' and c['type'] == 'company'


def test_documento_tamanho_invalido_e_omitido():
    # o que derrubava a cobrança: CPF faltando/sobrando dígito.
    for ruim in ('1234567890', '123456789012', '123', ''):
        c = _payload_customer(_pedido(cpf=ruim))
        assert 'document' not in c
        assert c['type'] == 'individual'   # nunca vira company por engano


def test_sem_cliente_nao_estoura_e_omite_documento():
    c = _payload_customer(_pedido(cpf=None))
    assert 'document' not in c


def test_nome_e_email_sao_limpos_e_nome_tem_fallback():
    c = _payload_customer(_pedido(nome='  Maria  ', email=' m@x.com '))
    assert c['name'] == 'Maria' and c['email'] == 'm@x.com'
    assert _payload_customer(_pedido(nome='   '))['name'] == 'Cliente'
