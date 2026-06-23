"""Checkout pré-preenche dados quando cliente logado (Fase 6 — PR 5).

Logado: nome/email/telefone/CPF vêm do `Cliente.*` sem precisar redigitar.
O form do request prevalece (não sobrescreve o que ele acabou de mudar).
"""
import pytest

pytestmark = pytest.mark.loja_host


def _cadastrar(client, email='c@x.com', nome='Caio Antinhani',
               telefone='11999998888', senha='senha-forte-1'):
    return client.post('/loja/cadastrar', data={
        'nome': nome, 'email': email, 'telefone': telefone,
        'senha': senha, 'aceite_lgpd': '1',
    }, follow_redirects=False)


def test_checkout_logado_preenche_dados(app, monkeypatch):
    monkeypatch.setenv('LOJA_VISIVEL', '1')   # vitrine pública pra simplificar
    from app.extensions import db
    from app.models import Cliente
    c = app.test_client()
    _cadastrar(c, email='c@x.com', nome='Caio Antinhani')
    # Adiciona CPF (cadastro não pega, mas a conta já existe)
    with app.app_context():
        cli = Cliente.query.filter_by(email='c@x.com').first()
        cli.cpf = '52998224725'
        db.session.commit()
    r = c.get('/loja/checkout')
    assert r.status_code == 200
    # Nome salvo (campo único) divide em nome + sobrenome nos 2 campos
    assert b'value="Caio"' in r.data
    assert b'value="Antinhani"' in r.data
    assert b'c@x.com' in r.data
    assert b'11999998888' in r.data


def test_checkout_anonimo_sem_preenchimento(app, monkeypatch):
    monkeypatch.setenv('LOJA_VISIVEL', '1')
    c = app.test_client()
    r = c.get('/loja/checkout')
    assert r.status_code == 200
    # Inputs aparecem vazios (sem `value=` com email pré-preenchido)
    assert b'value="c@x.com"' not in r.data


def test_form_request_prevalece_sobre_cliente(app, monkeypatch):
    """Quando o cliente edita um campo (POST com erro), o que ele digitou
    PREVALECE — não sobrescrevemos com o cadastro."""
    monkeypatch.setenv('LOJA_VISIVEL', '1')
    c = app.test_client()
    _cadastrar(c, email='orig@x.com', nome='Original')
    # POST inválido (sem aceite_lgpd / modo) — renderiza com o que ele
    # digitou, não com o que está no cadastro
    r = c.post('/loja/checkout', data={
        'nome': 'Editado', 'email': 'editado@x.com',
        'itens_json': '[]',  # carrinho vazio = erro
    })
    assert b'Editado' in r.data
    assert b'editado@x.com' in r.data
