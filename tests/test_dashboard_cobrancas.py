"""Painel de consulta: atalhos precisos sem efeitos financeiros."""
import re
from datetime import timedelta
from decimal import Decimal
from html.parser import HTMLParser
from unittest.mock import patch

import pytest

from app.extensions import db
from app.models import ClienteB2B, Cobranca, EnvioCobranca, FaturaB2B, Usuario, VendaB2B, VendaB2BParcela
from app.services.central_cobrancas import filtrar_etapa, painel, resumo_dashboard
from app.utils import hoje
from tests.test_b2b_email_docs import _cenario
from tests.test_central_cobrancas import _client, _mensal


class _Links(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.links = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == 'a' and 'href' in dict(attrs):
            self.links.append(dict(attrs)['href'])


def _venda(nome):
    cli, v, p, c = _cenario(nosso_numero=f'252{100000+Cobranca.query.count():06d}')
    cli.nome = nome
    db.session.commit()
    return cli, v, p, c


def test_dashboard_contagens_mesma_base_sem_duplicar_fechamento(app):
    f, _, _ = _mensal()
    f.cliente.nome = 'Fatura agrupada'
    db.session.commit()
    _, v, _, c = _venda('NF e boleto pendentes')
    c.nosso_numero, c.status = None, 'pendente'
    db.session.commit()
    linhas = painel()
    resumo = resumo_dashboard(linhas)
    assert resumo['aberto'] == Decimal('1000')
    assert resumo['nf_pendente'] == 1
    assert resumo['boleto_pendente'] == 1
    assert resumo['sem_historico'] == 2
    assert resumo['fechamentos'] == 0
    assert resumo['banco'] == 0
    assert filtrar_etapa(linhas, 'nf_pendente')[0].documento.id == v.id


def test_pagas_canceladas_e_zeros_fora_das_pendencias(app, admin_user):
    _, v, p, c = _venda('Sem valor')
    p.valor = v.valor_total = c.valor = Decimal('0')
    _, v2, _, _ = _venda('Cancelada')
    v2.status = 'cancelada'
    _, _, p3, _ = _venda('Quitada')
    p3.valor_pago = p3.valor
    db.session.commit()
    resumo = resumo_dashboard(painel())
    assert resumo['aberto'] == 0 and resumo['pagas'] == 1
    assert resumo['nf_pendente'] == resumo['boleto_pendente'] == resumo['banco'] == 0
    # A antiga lista de candidatas a boleto também não sugere cobrar R$ 0.
    corpo = _client(app, admin_user).get('/cobrancas/banco').get_data(as_text=True)
    assert 'Parcelas B2B em aberto sem cobrança' not in corpo


def test_candidatas_sem_boleto_nao_mostram_zeros_quitadas_ou_canceladas(app, admin_user):
    for nome, valor, cancelada, paga in [('Zero', '0', False, False), ('Cancelada', '10', True, False),
                                       ('Paga', '10', False, True), ('Real', '10', False, False)]:
        _, v, p, c = _venda(nome)
        db.session.delete(c)
        p.valor = v.valor_total = Decimal(valor)
        v.status = 'cancelada' if cancelada else 'ativa'
        p.valor_pago = p.valor if paga else Decimal('0')
        db.session.commit()
    corpo = _client(app, admin_user).get('/cobrancas/banco').get_data(as_text=True)
    candidatas = re.search(r'<table\b.*?</table>', corpo, re.S).group()
    assert 'Real' in candidatas
    assert all(nome not in candidatas for nome in ['Zero', 'Cancelada', 'Paga'])


def test_banco_remessa_rejeicao_e_saldo_divergente_nao_viram_boletos_a_criar(app):
    for nome, estado in [('Remessa', 'remessa'), ('Rejeitada', 'rejeitada'), ('Parcial', 'registrada')]:
        _, _, p, c = _venda(nome)
        c.status = estado
        if nome == 'Parcial':
            p.valor_pago = Decimal('100')
        db.session.commit()
    resumo = resumo_dashboard(painel())
    assert resumo['banco'] == 3
    assert resumo['boleto_pendente'] == 0


def test_fechamentos_contam_clientes_e_nao_vendas_ja_parceladas(app):
    cli = ClienteB2B(nome='Mensal', ativo=True, faturamento_mensal=True)
    db.session.add(cli)
    db.session.flush()
    for valor in (100, 200):
        db.session.add(VendaB2B(cliente_id=cli.id, data_venda=hoje(), valor_total=valor, status='ativa'))
    cli2, v, _, _ = _venda('Já parcelada')
    cli2.faturamento_mensal = True
    inativo = ClienteB2B(nome='Inativo', ativo=False, faturamento_mensal=True)
    futuro = ClienteB2B(nome='Futuro', ativo=True, faturamento_mensal=True)
    zerado = ClienteB2B(nome='Zerado', ativo=True, faturamento_mensal=True)
    db.session.add_all([inativo, futuro, zerado])
    db.session.flush()
    for cliente, data, valor in [(inativo, hoje(), 100), (futuro, hoje()+timedelta(days=1), 100),
                                  (zerado, hoje(), 0)]:
        db.session.add(VendaB2B(cliente_id=cliente.id, data_venda=data, valor_total=valor, status='ativa'))
    db.session.commit()
    assert resumo_dashboard(painel())['fechamentos'] == 1
    assert FaturaB2B.query.count() == 0


def test_atalhos_dashboard_sao_get_e_nao_emitem_nem_enviam(app, admin_user):
    _mensal()
    client = _client(app, admin_user)
    modelos = (FaturaB2B, Cobranca, VendaB2BParcela, EnvioCobranca)
    antes = [m.query.count() for m in modelos]
    with patch('app.services.email.enviar') as email, \
            patch('app.services.tiny_nf_b2b.emitir_nf') as nota, \
            patch('app.services.tiny_nf_b2b.emitir_nf_fatura') as nota_fatura, \
            patch('app.services.sicredi_cnab.gerar_remessa') as banco:
        response = client.get('/cobrancas/painel')
        assert response.status_code == 200
        main = re.search(r'<main\b.*?</main>', response.get_data(as_text=True), re.S).group()
        assert '<form' not in main
        for href in _Links(main).links:
            assert client.get(href).status_code == 200
        for tool in (email, nota, nota_fatura, banco):
            tool.assert_not_called()
    assert [m.query.count() for m in modelos] == antes
    assert 'não emitem nem enviam' in main
    assert 'não significa que o cliente nunca recebeu' in main


def test_filtro_notas_mantem_busca_e_paginacao_e_leva_a_origem(app, admin_user):
    _, v, p, _ = _venda('Notas da cafeteria')
    for n in range(2, 33):
        db.session.add(VendaB2BParcela(venda_id=v.id, numero=n, vencimento=p.vencimento, valor=10))
    db.session.commit()
    client = _client(app, admin_user)
    body = client.get('/cobrancas/?etapa=nf_pendente&q=cafeteria').get_data(as_text=True)
    assert '<h1>Notas fiscais a conferir</h1>' in body
    assert f'/b2b/vendas/{v.id}#nota-fiscal' in body
    next_link = next(href for href in _Links(body).links if 'pagina=2' in href)
    assert 'etapa=nf_pendente' in next_link and 'q=cafeteria' in next_link
    assert 'Página 2 de 2' in client.get(next_link).get_data(as_text=True)
    assert 'value="nf_pendente" selected' in body
    assert 'id="nota-fiscal"' in client.get(f'/b2b/vendas/{v.id}').get_data(as_text=True)


def test_filtro_boletos_encaminha_para_boleto_da_fatura(app, admin_user):
    f, _, c = _mensal()
    db.session.delete(c)
    db.session.commit()
    client = _client(app, admin_user)
    body = client.get('/cobrancas/?etapa=boleto_pendente').get_data(as_text=True)
    assert f'/b2b/faturas/{f.id}#boletos' in body
    assert 'id="boletos"' in client.get(f'/b2b/faturas/{f.id}').get_data(as_text=True)
    assert client.get('/cobrancas/?etapa=inexistente').status_code == 200


def test_valores_e_contadores_da_lista_correspondem_ao_atalho_escolhido(app, admin_user):
    f, _, _ = _mensal()
    f.cliente.nome = 'Fatura com NF'
    db.session.commit()
    _venda('Venda sem NF')
    corpo = _client(app, admin_user).get('/cobrancas/?etapa=nf_pendente').get_data(as_text=True)
    assert 'R$ 500,00' in corpo and 'R$ 1.000,00' not in corpo
    assert 'A receber <span>1</span>' in corpo and 'Todas <span>1</span>' in corpo


@pytest.mark.parametrize('papel', ['treinamento', 'loja', 'producao'])
def test_dashboard_continua_restrito_a_admin(app, papel):
    user = Usuario(nome='Restrito', login='restrito', papel=papel)
    user.set_senha('local-test')
    db.session.add(user)
    db.session.commit()
    assert _client(app, user).get('/cobrancas/painel').status_code == 403


def test_dashboard_exige_login(app):
    assert app.test_client().get('/cobrancas/painel').status_code == 302


@pytest.mark.parametrize('v2', [True, False])
def test_entrada_financeiro_aponta_para_dashboard_nos_dois_layouts(app, admin_user, v2):
    app.config['UI_V2_ENABLED'] = v2
    client = _client(app, admin_user)
    assert '/cobrancas/painel' in client.get('/area/financeiro').get_data(as_text=True)
    assert client.get('/cobrancas/painel').status_code == 200
