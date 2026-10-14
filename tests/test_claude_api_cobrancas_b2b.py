"""Sonda /api/claude/cobrancas-b2b (14/10/2026) — leitura das vendas,
parcelas e boletos de um cliente B2B."""
from datetime import timedelta
from decimal import Decimal

from app.extensions import db
from app.models import ClienteB2B, Cobranca, VendaB2B, VendaB2BParcela
from app.utils import hoje

TOK = 'tok-teste'


def _h():
    return {'Authorization': f'Bearer {TOK}'}


def test_lista_venda_parcela_e_boleto_do_cliente(app):
    client = app.test_client()
    app.config['CLAUDE_API_TOKEN'] = TOK
    c = ClienteB2B(nome='Lavô Street Ltda')
    outro = ClienteB2B(nome='Outro Cliente')
    db.session.add_all([c, outro])
    db.session.flush()
    v = VendaB2B(cliente_id=c.id, data_venda=hoje(), valor_total=Decimal('514.00'))
    db.session.add(v)
    db.session.flush()
    p = VendaB2BParcela(venda_id=v.id, numero=1, vencimento=hoje() + timedelta(days=7),
                        valor=Decimal('514.00'), valor_pago=Decimal('514.00'),
                        forma_pagamento='pix')
    db.session.add(p)
    db.session.flush()
    db.session.add(Cobranca(parcela_id=p.id, pagador_nome='Lavô Street Ltda',
                            pagador_cnpj_cpf='00000000000191', valor=Decimal('514.00'),
                            vencimento=hoje() + timedelta(days=7), emissao=hoje(),
                            seu_numero='V1', status='registrada', nosso_numero='252000041'))
    db.session.add(VendaB2B(cliente_id=outro.id, data_venda=hoje(), valor_total=Decimal('9')))
    db.session.commit()

    r = client.get('/api/claude/cobrancas-b2b?cliente=lavo', headers=_h())
    assert r.status_code == 200
    d = r.get_json()
    assert [x['nome'] for x in d['clientes']] == ['Lavô Street Ltda']
    assert len(d['vendas']) == 1
    parc = d['vendas'][0]['parcelas'][0]
    assert parc['forma_pagamento'] == 'pix' and parc['valor_pago'] == 514.0
    assert parc['cobrancas'][0]['status'] == 'registrada'
    assert parc['cobrancas'][0]['nosso_numero'] == '25/200004-1'


def test_exige_cliente_e_token(app):
    client = app.test_client()
    app.config['CLAUDE_API_TOKEN'] = TOK
    assert client.get('/api/claude/cobrancas-b2b', headers=_h()).status_code == 400
    assert client.get('/api/claude/cobrancas-b2b?cliente=x').status_code in (401, 403)
