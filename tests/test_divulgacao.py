"""Divulgacao — pedido "como do site" SEM pagamento (brinde/PR), 21/07/2026.

Decisoes do dono (AskUserQuestion): BAIXA estoque fisico (marcado, fora de
faturamento e previsao) + tela admin nova. Cobre:
- criar baixa EstoqueLoja da loja certa (entrega=origem_site, retirada=escolhida)
  com mov tipo PROPRIO (venda_site_divulgacao), fora da whitelist de demanda;
- pedido nasce status='divulgacao', pago_em NULL, divulgacao=True;
- NAO entra no faturamento do site (briefing) nem no funil do auditor;
- aparece no painel de entregas do dia, serializado com divulgacao=True;
- cancelar estorna o estoque;
- rota admin cria/valida; funcionario recebe 403.
"""
from datetime import timedelta

import pytest

from app.extensions import db


@pytest.fixture
def cliente(app):
    return app.test_client()


def _login(cliente, user):
    with cliente.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def _loja(nome='Loja do Site', origem=False):
    from app.models import AppConfig, Loja
    lo = Loja(nome=nome, ativa=True, endereco='Rua X, 1')
    db.session.add(lo)
    db.session.commit()
    if origem:
        AppConfig.set('loja_site_estoque_id', lo.id)
        db.session.commit()
    return lo


def _receita(nome='Pão Div', preco=8.0):
    from app.models import Receita
    r = Receita(nome=nome, categoria='Pães', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0, preco_site=preco)
    db.session.add(r)
    db.session.commit()
    return r


def _estoque(loja, receita, qtd):
    from app.models import EstoqueLoja
    el = EstoqueLoja(loja_id=loja.id, receita_id=receita.id, quantidade=qtd)
    db.session.add(el)
    db.session.commit()
    return el


def _saldo(loja, receita):
    from app.models import EstoqueLoja
    el = EstoqueLoja.query.filter_by(loja_id=loja.id,
                                     receita_id=receita.id).first()
    return el.quantidade if el else None


# ── Serviço: criação + baixa ───────────────────────────────────────────────

def test_criar_entrega_baixa_da_loja_origem(app):
    from app.services import divulgacao as svc
    origem = _loja('Origem Site', origem=True)
    r = _receita()
    _estoque(origem, r, 10)
    from app.utils import hoje
    p = svc.criar_divulgacao(
        itens=[{'kind': 'receita', 'id': r.id, 'qtd': 3}],
        modo_entrega='agendada', nome_destinatario='Padaria do Zé',
        data_entrega=hoje(), janela_entrega='8h-12h',
        endereco={'linha': 'Rua A, 1', 'cep': '01000-000'})
    assert p.divulgacao is True
    assert p.status == 'divulgacao'
    assert p.pago_em is None
    assert _saldo(origem, r) == 7          # baixou 3 da loja de origem


def test_criar_retirada_baixa_da_loja_escolhida(app):
    from app.services import divulgacao as svc
    origem = _loja('Origem Site', origem=True)
    escolhida = _loja('Loja Escolhida')
    r = _receita()
    _estoque(origem, r, 10)
    _estoque(escolhida, r, 10)
    p = svc.criar_divulgacao(
        itens=[{'kind': 'receita', 'id': r.id, 'qtd': 4}],
        modo_entrega='retirada', loja_retirada_id=escolhida.id,
        nome_destinatario='Influencer')
    assert p.loja_retirada_id == escolhida.id
    assert _saldo(escolhida, r) == 6       # baixou da escolhida
    assert _saldo(origem, r) == 10         # origem intacta


def test_mov_tipo_proprio_fora_da_previsao(app):
    from app.constants import VENDA_TIPOS_DEMANDA_COM_ESTORNO
    from app.models import EstoqueLoja, MovEstoqueLoja
    from app.services import divulgacao as svc
    origem = _loja('Origem Site', origem=True)
    r = _receita()
    _estoque(origem, r, 10)
    svc.criar_divulgacao(
        itens=[{'kind': 'receita', 'id': r.id, 'qtd': 2}],
        modo_entrega='agendada', nome_destinatario='X')
    el = EstoqueLoja.query.filter_by(loja_id=origem.id,
                                     receita_id=r.id).first()
    tipos = [m.tipo for m in MovEstoqueLoja.query.filter_by(
        estoque_loja_id=el.id).all()]
    assert tipos == ['venda_site_divulgacao']
    assert 'venda_site_divulgacao' not in VENDA_TIPOS_DEMANDA_COM_ESTORNO


def test_criar_sem_itens_ou_invalido_falha(app):
    from app.services import divulgacao as svc
    _loja('Origem Site', origem=True)
    with pytest.raises(ValueError):
        svc.criar_divulgacao(itens=[], modo_entrega='agendada',
                             nome_destinatario='X')
    with pytest.raises(ValueError):
        svc.criar_divulgacao(
            itens=[{'kind': 'receita', 'id': 99999, 'qtd': 1}],
            modo_entrega='agendada', nome_destinatario='X')


def test_retirada_sem_loja_falha(app):
    from app.services import divulgacao as svc
    r = _receita()
    with pytest.raises(ValueError):
        svc.criar_divulgacao(
            itens=[{'kind': 'receita', 'id': r.id, 'qtd': 1}],
            modo_entrega='retirada', nome_destinatario='X')


def test_item_arquivado_recusado(app):
    from app.services import divulgacao as svc
    from app.utils import agora
    _loja('Origem Site', origem=True)
    r = _receita()
    r.arquivada_em = agora()
    db.session.commit()
    with pytest.raises(ValueError):
        svc.criar_divulgacao(
            itens=[{'kind': 'receita', 'id': r.id, 'qtd': 1}],
            modo_entrega='agendada', nome_destinatario='X')


# ── Cancelamento estorna ───────────────────────────────────────────────────

def test_cancelar_estorna_estoque(app):
    from app.services import divulgacao as svc
    origem = _loja('Origem Site', origem=True)
    r = _receita()
    _estoque(origem, r, 10)
    p = svc.criar_divulgacao(
        itens=[{'kind': 'receita', 'id': r.id, 'qtd': 3}],
        modo_entrega='agendada', nome_destinatario='X')
    assert _saldo(origem, r) == 7
    res = svc.cancelar_divulgacao(p)
    assert res['revertido'] >= 1           # nº de movimentos revertidos
    assert p.status == 'cancelado'
    assert _saldo(origem, r) == 10         # devolveu TODAS as 3 unidades
    # idempotente
    assert svc.cancelar_divulgacao(p)['ja_cancelado'] is True


# ── Faturamento NÃO conta ──────────────────────────────────────────────────

def test_nao_entra_no_faturamento_do_site(app):
    from app.services import divulgacao as svc
    from app.services.briefing_dono import vendas_hoje
    origem = _loja('Origem Site', origem=True)
    r = _receita(preco=50.0)
    _estoque(origem, r, 10)
    svc.criar_divulgacao(
        itens=[{'kind': 'receita', 'id': r.id, 'qtd': 2}],
        modo_entrega='agendada', nome_destinatario='X')
    v = vendas_hoje(capturar=False)
    assert v['site_qtd'] == 0
    assert v['site_total'] == 0


def test_nao_entra_no_funil_do_auditor(app):
    from datetime import datetime, time

    from app.services import divulgacao as svc
    from app.services.chatbot_auditor import _funil_site
    from app.utils import hoje
    origem = _loja('Origem Site', origem=True)
    r = _receita()
    _estoque(origem, r, 10)
    svc.criar_divulgacao(
        itens=[{'kind': 'receita', 'id': r.id, 'qtd': 1}],
        modo_entrega='agendada', nome_destinatario='X')
    ini = datetime.combine(hoje(), time.min)
    fim = ini + timedelta(days=1)
    funil = _funil_site(ini, fim)
    assert funil['pedidos_criados'] == 0   # divulgação não é conversão


# ── Painel de entregas ─────────────────────────────────────────────────────

def test_aparece_no_painel_do_dia_com_flag(app):
    from app.blueprints.entregas.routes import (
        _pedidos_online_do_dia,
        _serializar_pedido_online,
    )
    from app.models import PedidoOnline
    from app.services import divulgacao as svc
    from app.utils import hoje
    origem = _loja('Origem Site', origem=True)
    r = _receita()
    _estoque(origem, r, 10)
    p = svc.criar_divulgacao(
        itens=[{'kind': 'receita', 'id': r.id, 'qtd': 1}],
        modo_entrega='agendada', nome_destinatario='Padaria do Zé',
        data_entrega=hoje())
    pedidos = _pedidos_online_do_dia(hoje())
    assert any(x['code'] == p.codigo for x in pedidos)
    ser = _serializar_pedido_online(PedidoOnline.query.get(p.id))
    assert ser['divulgacao'] is True
    assert ser['destinatario'] == 'Padaria do Zé'


# ── Rota admin ─────────────────────────────────────────────────────────────

def test_rota_get_admin_ok(app, admin_user, cliente):
    _loja('Origem Site', origem=True)
    _receita()
    _login(cliente, admin_user)
    resp = cliente.get('/admin/loja-online/divulgacao')
    assert resp.status_code == 200
    assert 'Lançar divulgação' in resp.get_data(as_text=True)


def test_rota_post_cria(app, admin_user, cliente):
    from app.models import PedidoOnline
    origem = _loja('Origem Site', origem=True)
    r = _receita()
    _estoque(origem, r, 10)
    _login(cliente, admin_user)
    resp = cliente.post('/admin/loja-online/divulgacao', data={
        'modo_entrega': 'agendada',
        'nome_destinatario': 'Cliente PR',
        'item_alvo[]': f'receita:{r.id}',
        'item_qtd[]': '2',
        'data_entrega': '',
    })
    assert resp.status_code in (302, 303)
    p = PedidoOnline.query.filter_by(divulgacao=True).first()
    assert p is not None and p.status == 'divulgacao'
    assert _saldo(origem, r) == 8


def test_rota_funcionario_403(app, cliente):
    from app.models import Usuario
    u = Usuario(nome='func', login='func', papel='funcionario')
    u.set_senha('x')
    db.session.add(u)
    db.session.commit()
    _login(cliente, u)
    resp = cliente.get('/admin/loja-online/divulgacao')
    assert resp.status_code == 403
