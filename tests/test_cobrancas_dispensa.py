"""Divulgação não vira dívida, pagamento, estorno de estoque ou boleto."""
from decimal import Decimal
from importlib import import_module
from unittest.mock import patch
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.extensions import db
from app.models import Cobranca, CobrancaRemessa, EnvioCobranca, VendaB2B
from app.services.central_cobrancas import carregar, painel, resumo_dashboard
from app.services.cobrancas_dispensa import dispensar
from app.services.faturas_b2b import vendas_para_fechar
from app.services.sicredi_cnab import gerar_remessa
from app.services.vendas_b2b import receber_pagamento
from app.utils import agora, hoje
from tests.test_b2b_email_docs import _cenario
from tests.test_central_cobrancas import _client, _mensal


def _divulgacao():
    cli, v, p, c = _cenario(nosso_numero=None)
    c.status = 'pendente'
    v.status_entrega = 'entregue'
    v.estoque_baixado_em = agora()
    db.session.commit()
    return cli, v, p, c


def test_divulgacao_preserva_venda_parcela_boleto_estoque_e_audita(app, owner_user):
    _, v, p, c = _divulgacao()
    original = (v.valor_total, v.status, v.status_entrega, v.estoque_baixado_em,
                p.valor, p.valor_pago, p.pago_em, c.status, c.valor)
    client = _client(app, owner_user)
    with patch('app.services.email.enviar') as email, \
            patch('app.services.tiny_nf_b2b.emitir_nf') as nf:
        response = client.post(f'/b2b/vendas/{v.id}/sem-cobranca', data={
            'motivo': 'Divulgação autorizada', 'confirmar': '1'}, follow_redirects=True)
    assert response.status_code == 200
    assert 'Divulgação · sem cobrança' in response.get_data(as_text=True)
    email.assert_not_called()
    nf.assert_not_called()
    assert original == (v.valor_total, v.status, v.status_entrega, v.estoque_baixado_em,
                        p.valor, p.valor_pago, p.pago_em, c.status, c.valor)
    assert v.dispensa_cobranca['usuario_id'] == owner_user.id
    assert v.dispensa_cobranca['motivo'] == 'Divulgação autorizada'
    assert v.dispensa_cobranca['registrado_em']
    assert p.status == 'sem_cobranca' and p.saldo == v.valor_aberto == 0
    assert VendaB2B.query.count() == Cobranca.query.count() == 1
    assert EnvioCobranca.query.count() == CobrancaRemessa.query.count() == 0
    registrado = dict(v.dispensa_cobranca)
    client.post(f'/b2b/vendas/{v.id}/sem-cobranca', data={'motivo': 'outro', 'confirmar': '1'})
    assert v.dispensa_cobranca == registrado


def test_divulgacao_fora_de_pendencias_totais_banco_e_fechamento(app, owner_user):
    cli, v, p, _ = _divulgacao()
    cli.nome = 'DIVULGACAO UNICA TESTE'
    dispensar(v.id, owner_user, 'Divulgação')
    db.session.commit()
    r = carregar('parcela', p.id)
    assert r.sem_cobranca and r.saldo == 0 and r.pagamento != 'Paga'
    resumo = resumo_dashboard(painel())
    assert all(resumo[k] == 0 for k in ('aberto', 'vencido', 'pagas', 'nf_pendente',
                                      'boleto_pendente', 'banco', 'sem_historico'))
    client = _client(app, owner_user)
    for url in ('/cobrancas/', '/cobrancas/?situacao=pagas', '/cobrancas/banco',
                '/b2b/?aba=cobrancas', '/b2b/contas-a-receber'):
        assert cli.nome not in client.get(url).get_data(as_text=True), url
    for url in ('/cobrancas/?situacao=sem_cobranca', '/cobrancas/?situacao=todas'):
        assert cli.nome in client.get(url).get_data(as_text=True)
    # Divulgação mensal sem parcelas também não entra em fechamento futuro.
    mensal = VendaB2B(cliente_id=cli.id, valor_total=100, data_venda=hoje(),
                     dispensa_cobranca=dict(v.dispensa_cobranca))
    cli.faturamento_mensal = True
    db.session.add(mensal)
    db.session.commit()
    assert not vendas_para_fechar(cli.id, hoje(), hoje())
    assert resumo_dashboard(painel())['fechamentos'] == 0


def test_divulgacao_nao_pode_receber_gerar_remessa_ou_enviar(app, owner_user):
    _, v, p, c = _divulgacao()
    dispensar(v.id, owner_user, 'Divulgação')
    db.session.commit()
    client = _client(app, owner_user)
    with patch('app.services.email.enviar') as email:
        corpo = client.post(f'/cobrancas/parcela/{p.id}/documentos', data={
            'chave': str(uuid4()), 'email': 'cliente@example.com'}, follow_redirects=True).get_data(as_text=True)
        assert 'Divulgação — sem cobrança' in corpo
        assert 'id="cob-send-form"' not in corpo
        email.assert_not_called()
    client.post(f'/cobrancas/gerar-da-parcela/{p.id}')
    assert Cobranca.query.count() == 1
    rem, erros = gerar_remessa([c])
    assert rem is None and any('divulgação sem cobrança' in erro for erro in erros)
    assert c.status == 'pendente' and c.nosso_numero is None
    with pytest.raises(ValueError, match='Divulgação sem cobrança'):
        receber_pagamento(p, Decimal('500'))
    assert not p.valor_pago and not p.pago_em


@pytest.mark.parametrize('campo,valor', [('status', 'remessa'), ('status', 'registrada'),
                                      ('nosso_numero', '262000099'), ('valor_pago', Decimal('10'))])
def test_nao_oculta_boleto_ja_numerado_ou_movimentado(app, owner_user, campo, valor):
    _, v, _, c = _divulgacao()
    setattr(c, campo, valor)
    db.session.commit()
    with pytest.raises(ValueError, match='boleto numerado ou movimentado'):
        dispensar(v.id, owner_user, 'Divulgação')
    assert not v.sem_cobranca


def test_nao_oculta_fatura_consolidada(app, owner_user):
    _, p, _ = _mensal()
    with pytest.raises(ValueError, match='pertence a uma fatura'):
        dispensar(p.venda_id, owner_user, 'Divulgação')
    assert not p.venda.sem_cobranca


def test_nao_reclassifica_pagamento_existente(app, owner_user):
    _, v, p, _ = _divulgacao()
    p.valor_pago = 1
    db.session.commit()
    with pytest.raises(ValueError, match='pagamento registrado'):
        dispensar(v.id, owner_user, 'Divulgação')
    assert not v.sem_cobranca


def test_exige_dono_confirmacao_e_motivo(app, admin_user):
    _, v, _, _ = _divulgacao()
    response = _client(app, admin_user).post(f'/b2b/vendas/{v.id}/sem-cobranca', data={
        'confirmar': '1', 'motivo': 'Divulgação'})
    assert response.status_code == 403 and not v.sem_cobranca


@pytest.mark.parametrize('dados', [{'motivo': 'Divulgação'}, {'confirmar': '1'},
                                 {'confirmar': '1', 'motivo': 'x' * 301}])
def test_dono_precisa_confirmar_com_motivo(app, owner_user, dados):
    _, v, _, _ = _divulgacao()
    _client(app, owner_user).post(f'/b2b/vendas/{v.id}/sem-cobranca', data=dados)
    assert not v.sem_cobranca


def test_migracao_nao_reclassifica_vendas_antigas(tmp_path):
    engine = sa.create_engine(f'sqlite:///{tmp_path / "dispensa.db"}')
    with engine.begin() as conn:
        conn.exec_driver_sql('CREATE TABLE venda_b2b (id INTEGER PRIMARY KEY, valor_total NUMERIC)')
        conn.exec_driver_sql('INSERT INTO venda_b2b VALUES (38, 400), (43, 1050)')
        migration = import_module('migrations.versions.b7248c1d9e02_divulgacao_sem_cobranca')
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            migration.upgrade()
        assert conn.exec_driver_sql('SELECT id, valor_total, dispensa_cobranca FROM venda_b2b ORDER BY id').all() == [
            (38, 400, None), (43, 1050, None)]
