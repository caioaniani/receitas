"""Divulgacao — pedido "como do site" SEM pagamento (brinde/PR), 21/07/2026.

Decisoes do dono (AskUserQuestion): BAIXA estoque fisico (marcado, fora de
faturamento e previsao) + tela admin nova. Follow-up 21/07: papel 'marketing'
(so owner+marketing lancam), todos os campos obrigatorios, CEP autofill.

Cobre:
- criar baixa EstoqueLoja da loja certa (entrega=origem_site, retirada=escolhida)
  com mov tipo PROPRIO (venda_site_divulgacao), fora da whitelist de demanda;
- pedido nasce status='divulgacao', pago_em NULL, divulgacao=True;
- NAO entra no faturamento do site (briefing) nem no funil do auditor;
- aparece no painel de entregas do dia, serializado com divulgacao=True;
- cancelar estorna o estoque;
- todos os campos obrigatorios (telefone/data/janela/endereco ou loja);
- GATE: owner + marketing entram; admin comum e funcionario 403.
"""
from datetime import timedelta

import pytest

from app.extensions import db

_END_OK = {'cep': '01310-100', 'logradouro': 'Av Paulista', 'numero': '1000',
           'bairro': 'Bela Vista', 'cidade': 'São Paulo', 'uf': 'SP'}


@pytest.fixture
def cliente(app):
    return app.test_client()


def _login(cliente, user):
    with cliente.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def _amanha():
    from datetime import timedelta

    from app.utils import hoje
    return hoje() + timedelta(days=1)


def _base_kw(**over):
    """kwargs validos completos (todos os campos obrigatorios). Data = AMANHA
    (nunca hoje — regra do dono 21/07)."""
    kw = dict(nome_destinatario='Padaria do Zé', telefone='11999990000',
              data_entrega=_amanha(), janela_entrega='08:00–09:00')
    kw.update(over)
    return kw


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


def _marketing_user():
    from app.models import Usuario
    u = Usuario(nome='mkt', login='mkt', papel='marketing')
    u.set_senha('x')
    db.session.add(u)
    db.session.commit()
    return u


# ── Papel marketing ────────────────────────────────────────────────────────

def test_pode_divulgacao_so_owner_e_marketing(app, admin_user, owner_user):
    mkt = _marketing_user()
    assert mkt.is_marketing() is True
    assert mkt.pode_divulgacao() is True
    assert owner_user.pode_divulgacao() is True     # dono
    assert admin_user.pode_divulgacao() is False    # admin comum NAO


# ── Serviço: criação + baixa ───────────────────────────────────────────────

def test_criar_entrega_baixa_da_loja_origem(app):
    from app.services import divulgacao as svc
    origem = _loja('Origem Site', origem=True)
    r = _receita()
    _estoque(origem, r, 10)
    p = svc.criar_divulgacao(
        itens=[{'kind': 'receita', 'id': r.id, 'qtd': 3}],
        modo_entrega='agendada', endereco=dict(_END_OK), **_base_kw())
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
        modo_entrega='retirada', loja_retirada_id=escolhida.id, **_base_kw())
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
        modo_entrega='agendada', endereco=dict(_END_OK), **_base_kw())
    el = EstoqueLoja.query.filter_by(loja_id=origem.id,
                                     receita_id=r.id).first()
    tipos = [m.tipo for m in MovEstoqueLoja.query.filter_by(
        estoque_loja_id=el.id).all()]
    assert tipos == ['venda_site_divulgacao']
    assert 'venda_site_divulgacao' not in VENDA_TIPOS_DEMANDA_COM_ESTORNO


def test_campos_obrigatorios(app):
    from app.services import divulgacao as svc
    _loja('Origem Site', origem=True)
    r = _receita()
    it = [{'kind': 'receita', 'id': r.id, 'qtd': 1}]
    # sem telefone
    with pytest.raises(ValueError):
        svc.criar_divulgacao(itens=it, modo_entrega='agendada',
                             endereco=dict(_END_OK),
                             **_base_kw(telefone=''))
    # sem janela
    with pytest.raises(ValueError):
        svc.criar_divulgacao(itens=it, modo_entrega='agendada',
                             endereco=dict(_END_OK),
                             **_base_kw(janela_entrega=''))
    # entrega sem endereco completo (falta numero)
    end = dict(_END_OK, numero='')
    with pytest.raises(ValueError):
        svc.criar_divulgacao(itens=it, modo_entrega='agendada',
                             endereco=end, **_base_kw())
    # retirada sem loja
    with pytest.raises(ValueError):
        svc.criar_divulgacao(itens=it, modo_entrega='retirada', **_base_kw())
    # sem itens
    with pytest.raises(ValueError):
        svc.criar_divulgacao(itens=[], modo_entrega='agendada',
                             endereco=dict(_END_OK), **_base_kw())


def test_data_hoje_ou_passado_recusada(app):
    """Nunca no mesmo dia — a partir de amanhã (regra do dono 21/07). Sem
    `permitir_hoje` (papel marketing), hoje segue recusado."""
    from app.services import divulgacao as svc
    from app.utils import hoje
    _loja('Origem Site', origem=True)
    r = _receita()
    it = [{'kind': 'receita', 'id': r.id, 'qtd': 1}]
    with pytest.raises(ValueError):
        svc.criar_divulgacao(itens=it, modo_entrega='agendada',
                             endereco=dict(_END_OK),
                             **_base_kw(data_entrega=hoje()))


def test_dono_pode_lancar_pra_hoje(app):
    """Decisão do dono 08/08/2026 ("eu como owner devo conseguir lançar para
    quando quiser"): com `permitir_hoje`, a data pode ser HOJE."""
    from app.services import divulgacao as svc
    from app.utils import hoje
    origem = _loja('Origem Site', origem=True)
    r = _receita()
    _estoque(origem, r, 10)
    it = [{'kind': 'receita', 'id': r.id, 'qtd': 2}]
    p = svc.criar_divulgacao(itens=it, modo_entrega='agendada',
                             endereco=dict(_END_OK), permitir_hoje=True,
                             **_base_kw(data_entrega=hoje()))
    assert p is not None and p.data_entrega == hoje()
    assert _saldo(origem, r) == 8          # baixa normal


def test_nem_o_dono_lanca_pro_passado(app):
    """"Quando quiser" não inclui ontem — não há o que entregar no passado."""
    from datetime import timedelta

    from app.services import divulgacao as svc
    from app.utils import hoje
    _loja('Origem Site', origem=True)
    r = _receita()
    it = [{'kind': 'receita', 'id': r.id, 'qtd': 1}]
    with pytest.raises(ValueError):
        svc.criar_divulgacao(
            itens=it, modo_entrega='agendada', endereco=dict(_END_OK),
            permitir_hoje=True,
            **_base_kw(data_entrega=hoje() - timedelta(days=1)))


def test_endpoint_janelas_retirada_e_agendada(app, owner_user, cliente):
    """Janelas = MESMA regra do site (loja_checkout.janelas_disponiveis).
    Retirada não tem distância; agendada com endereço geocodado usa o corte
    por distância — o frete é mockado pra não bater na rede."""
    from unittest.mock import patch
    _login(cliente, owner_user)
    d = _amanha().isoformat()          # divulgação é sempre a partir de amanhã
    # retirada: janelas base (sem distância)
    rj = cliente.get('/admin/loja-online/divulgacao/janelas'
                     '?modo=retirada&data=' + d).get_json()
    assert rj['ok'] and len(rj['janelas']) >= 1
    assert rj['distancia_km'] is None
    # agendada longe: corta a 1ª janela da manhã (08:00–09:00)
    with patch('app.services.frete.consultar_frete',
               return_value={'ok': True, 'distancia_km': 20.0,
                             'fora_area': False}):
        aj = cliente.get('/admin/loja-online/divulgacao/janelas'
                         '?modo=agendada&data=' + d +
                         '&logradouro=Rua+X&numero=1&cidade=SP').get_json()
    assert aj['ok'] and '08:00–09:00' not in aj['janelas']
    assert aj['distancia_km'] == 20.0


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
            modo_entrega='agendada', endereco=dict(_END_OK), **_base_kw())


# ── Cancelamento estorna ───────────────────────────────────────────────────

def test_cancelar_estorna_estoque(app):
    from app.services import divulgacao as svc
    origem = _loja('Origem Site', origem=True)
    r = _receita()
    _estoque(origem, r, 10)
    p = svc.criar_divulgacao(
        itens=[{'kind': 'receita', 'id': r.id, 'qtd': 3}],
        modo_entrega='agendada', endereco=dict(_END_OK), **_base_kw())
    assert _saldo(origem, r) == 7
    res = svc.cancelar_divulgacao(p)
    assert res['revertido'] >= 1           # nº de movimentos revertidos
    assert p.status == 'cancelado'
    assert _saldo(origem, r) == 10         # devolveu TODAS as 3 unidades
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
        modo_entrega='agendada', endereco=dict(_END_OK), **_base_kw())
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
        modo_entrega='agendada', endereco=dict(_END_OK), **_base_kw())
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
    origem = _loja('Origem Site', origem=True)
    r = _receita()
    _estoque(origem, r, 10)
    p = svc.criar_divulgacao(
        itens=[{'kind': 'receita', 'id': r.id, 'qtd': 1}],
        modo_entrega='agendada', endereco=dict(_END_OK),
        **_base_kw(data_entrega=_amanha()))
    pedidos = _pedidos_online_do_dia(_amanha())
    assert any(x['code'] == p.codigo for x in pedidos)
    ser = _serializar_pedido_online(PedidoOnline.query.get(p.id))
    assert ser['divulgacao'] is True
    assert ser['destinatario'] == 'Padaria do Zé'


# ── Rota admin + GATE ──────────────────────────────────────────────────────

def test_rota_get_owner_ok(app, owner_user, cliente):
    _loja('Origem Site', origem=True)
    _receita()
    _login(cliente, owner_user)
    resp = cliente.get('/admin/loja-online/divulgacao')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'Lançar divulgação' in body
    assert 'combo-input' in body           # typeahead presente
    assert '/loja/api/cep/' in body        # autofill de CEP presente


def test_rota_get_marketing_ok(app, cliente):
    mkt = _marketing_user()
    _loja('Origem Site', origem=True)
    _receita()
    _login(cliente, mkt)
    assert cliente.get('/admin/loja-online/divulgacao').status_code == 200


def test_rota_admin_comum_403(app, admin_user, cliente):
    """Admin NAO-owner nao entra ('so owner e marketing')."""
    _login(cliente, admin_user)
    assert cliente.get('/admin/loja-online/divulgacao').status_code == 403


def test_rota_funcionario_403(app, cliente):
    from app.models import Usuario
    u = Usuario(nome='func', login='func', papel='funcionario')
    u.set_senha('x')
    db.session.add(u)
    db.session.commit()
    _login(cliente, u)
    assert cliente.get('/admin/loja-online/divulgacao').status_code == 403


def test_marketing_home_redireciona_pra_divulgacao(app, cliente):
    mkt = _marketing_user()
    _login(cliente, mkt)
    resp = cliente.get('/')
    assert resp.status_code == 302
    assert '/admin/loja-online/divulgacao' in resp.headers['Location']


def test_rota_post_cria_completo(app, owner_user, cliente):
    from app.models import PedidoOnline
    origem = _loja('Origem Site', origem=True)
    r = _receita()
    _estoque(origem, r, 10)
    _login(cliente, owner_user)
    resp = cliente.post('/admin/loja-online/divulgacao', data={
        'modo_entrega': 'agendada',
        'nome_destinatario': 'Cliente PR',
        'telefone': '11999990000',
        'data_entrega': _amanha().isoformat(),
        'janela_entrega': '08:00–09:00',
        'endereco_cep': '01310-100',
        'endereco_logradouro': 'Av Paulista',
        'endereco_numero': '1000',
        'endereco_bairro': 'Bela Vista',
        'endereco_cidade': 'São Paulo',
        'endereco_uf': 'SP',
        'item_alvo[]': f'receita:{r.id}',
        'item_qtd[]': '2',
    })
    assert resp.status_code in (302, 303)
    p = PedidoOnline.query.filter_by(divulgacao=True).first()
    assert p is not None and p.status == 'divulgacao'
    assert 'Av Paulista' in (p.endereco_entrega or '')
    assert _saldo(origem, r) == 8


def test_rota_post_owner_pra_hoje_cria(app, owner_user, cliente):
    """A rota liga `permitir_hoje` pelo papel: dono lança pra HOJE."""
    from app.models import PedidoOnline
    from app.utils import hoje
    origem = _loja('Origem Site', origem=True)
    r = _receita()
    _estoque(origem, r, 10)
    _login(cliente, owner_user)
    resp = cliente.post('/admin/loja-online/divulgacao', data={
        'modo_entrega': 'agendada',
        'nome_destinatario': 'Cliente PR',
        'telefone': '11999990000',
        'data_entrega': hoje().isoformat(),
        'janela_entrega': '16:00–17:00',
        'endereco_cep': '01310-100',
        'endereco_logradouro': 'Av Paulista',
        'endereco_numero': '1000',
        'endereco_bairro': 'Bela Vista',
        'endereco_cidade': 'São Paulo',
        'endereco_uf': 'SP',
        'item_alvo[]': f'receita:{r.id}',
        'item_qtd[]': '1',
    })
    assert resp.status_code in (302, 303)
    p = PedidoOnline.query.filter_by(divulgacao=True).first()
    assert p is not None and p.data_entrega == hoje()


def test_rota_post_marketing_pra_hoje_nao_cria(app, cliente):
    """Marketing segue na regra de 21/07: a partir de amanhã."""
    from app.models import PedidoOnline
    from app.utils import hoje
    origem = _loja('Origem Site', origem=True)
    r = _receita()
    _estoque(origem, r, 10)
    mkt = _marketing_user()
    _login(cliente, mkt)
    cliente.post('/admin/loja-online/divulgacao', data={
        'modo_entrega': 'agendada',
        'nome_destinatario': 'Cliente PR',
        'telefone': '11999990000',
        'data_entrega': hoje().isoformat(),
        'janela_entrega': '16:00–17:00',
        'endereco_cep': '01310-100',
        'endereco_logradouro': 'Av Paulista',
        'endereco_numero': '1000',
        'endereco_bairro': 'Bela Vista',
        'endereco_cidade': 'São Paulo',
        'endereco_uf': 'SP',
        'item_alvo[]': f'receita:{r.id}',
        'item_qtd[]': '1',
    })
    assert PedidoOnline.query.filter_by(divulgacao=True).first() is None


def test_rota_post_incompleto_nao_cria(app, owner_user, cliente):
    origem = _loja('Origem Site', origem=True)
    r = _receita()
    _estoque(origem, r, 10)
    _login(cliente, owner_user)
    # falta telefone e endereço
    cliente.post('/admin/loja-online/divulgacao', data={
        'modo_entrega': 'agendada', 'nome_destinatario': 'X',
        'item_alvo[]': f'receita:{r.id}', 'item_qtd[]': '1',
    })
    from app.models import PedidoOnline
    assert PedidoOnline.query.filter_by(divulgacao=True).first() is None
    assert _saldo(origem, r) == 10         # nada baixou


def test_rota_cancelar_devolve_estoque(app, owner_user, cliente):
    from app.services import divulgacao as svc
    origem = _loja('Origem Site', origem=True)
    r = _receita()
    _estoque(origem, r, 10)
    p = svc.criar_divulgacao(
        itens=[{'kind': 'receita', 'id': r.id, 'qtd': 3}],
        modo_entrega='agendada', endereco=dict(_END_OK), **_base_kw())
    assert _saldo(origem, r) == 7
    _login(cliente, owner_user)
    resp = cliente.post('/admin/loja-online/divulgacao/%s/cancelar' % p.codigo)
    assert resp.status_code in (302, 303)
    db.session.refresh(p)
    assert p.status == 'cancelado'
    assert _saldo(origem, r) == 10
