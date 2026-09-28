import pytest

from app.services.cobrancas_destinatarios import email_valido, normalizar_adicionais


def test_separadores_dedupe_e_principal_efetivo():
    assert normalizar_adicionais(
        ' Principal@example.com; Compras@example.com,compras@example.com\nfinanceiro@example.com ',
        'principal@example.com') == ['Compras@example.com', 'financeiro@example.com']


@pytest.mark.parametrize('valor', [None, '', [], '  \n ; , '])
def test_adicionais_vazios(valor):
    assert normalizar_adicionais(valor) == []


@pytest.mark.parametrize('valor', ['sem-arroba', 'Nome <x@example.com>', 'x@example.com\x00',
                                 'a@example.com\r\nBcc: intruso@example.com',
                                 ['a@example.com,b@example.com'], {'email': 'x@example.com'},
                                 ['x@example.com', 123], 'x' * 3001,
                                 'financeiro@example..com', 'Bcc:intruso@example.com',
                                 'a..b@example.com', 'a@-example.com', 'a@example-.com',
                                 'a@ex_ample.com', 'x' * 65 + '@example.com',
                                 'caio(teste)@opao.online', '"caio"@opao.online',
                                 ','.join(f'x{i}@example.com' for i in range(11))])
def test_adicional_invalido_bloqueia_toda_lista(valor):
    with pytest.raises(ValueError):
        normalizar_adicionais(valor)


@pytest.mark.parametrize('valor', ['a@example.com,b@example.com', 'x@example.com\r\n',
                                 'x@example.com\x7f', None, 'x' * 255])
def test_email_unico_rejeita_lista_e_controles(valor):
    assert not email_valido(valor)
