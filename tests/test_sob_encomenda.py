"""Sob encomenda D+2 (dono 21/07/2026).

Produto/receita marcado `sob_encomenda`:
- no site só vende pra ENTREGA/RETIRADA a partir de D+2 (dois dias à frente,
  janela das 08:00) — nunca hoje/amanhã nem express (same-day);
- é PRODUZIDO PRO PEDIDO: sempre disponível na vitrine (não olha plano-do-dia)
  e a venda NÃO abate o EstoqueLoja físico;
- o pedido pago entra na produção do padeiro (separação + pré-preparo) e no
  balanço firme da indústria (cronograma), estilo B2B.

Testa cada camada isoladamente.
"""
from datetime import datetime, timedelta

from app.extensions import db
from app.utils import hoje

_END_NF = {'cep': '04077-000', 'logradouro': 'Rua X', 'numero': '10',
           'bairro': 'Moema', 'cidade': 'São Paulo', 'uf': 'SP'}


# ── helpers ──────────────────────────────────────────────────────────

def _receita(nome='Mini Pain', preco=8.0, sob_encomenda=False,
             estado_padrao=None):
    from app.models import Receita
    r = Receita(nome=nome, categoria='Viennoiserie', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0, preco_site=preco,
                sob_encomenda=sob_encomenda, estado_padrao=estado_padrao)
    db.session.add(r)
    db.session.commit()
    return r


def _loja_origem():
    from app.models import AppConfig, Loja
    lo = Loja(nome='Loja do Site', ativa=True, endereco='Rua Y, 1')
    db.session.add(lo)
    db.session.commit()
    AppConfig.set('loja_site_estoque_id', lo.id)
    db.session.commit()
    return lo


def _estoque(loja, receita, qtd):
    from app.models import EstoqueLoja
    el = EstoqueLoja(loja_id=loja.id, receita_id=receita.id, quantidade=qtd)
    db.session.add(el)
    db.session.commit()
    return el


def _pedido_pago(receita, qtd=5, dias=1, status='pago'):
    from app.models import PedidoOnline, PedidoOnlineItem
    p = PedidoOnline(codigo=f'ENC{receita.id}{qtd}{dias}',
                     nome_cliente='Cliente X', email_cliente='c@x.com',
                     modo_entrega='retirada', status=status,
                     data_entrega=hoje() + timedelta(days=dias))
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoOnlineItem(
        pedido_id=p.id, kind='receita', receita_id=receita.id,
        nome=receita.nome, preco_unitario=8, quantidade=qtd, subtotal=8 * qtd))
    db.session.commit()
    return p


# ── modelo + serialização ────────────────────────────────────────────

def test_modelo_default_false(app):
    with app.app_context():
        r = _receita()
        assert r.sob_encomenda is False


def test_serializacao_expoe_flag(app):
    from app.services import loja_catalogo
    with app.app_context():
        r = _receita(sob_encomenda=True)
        d = loja_catalogo.por_id_publicado('receita', r.id)
        assert d['sob_encomenda'] is True


# ── vitrine: sempre disponível ───────────────────────────────────────

def test_vitrine_sob_encomenda_nunca_esgota(app):
    from app.services import loja_catalogo
    with app.app_context():
        r = _receita(sob_encomenda=True)
        itens = [loja_catalogo.por_id_publicado('receita', r.id)]
        loja_catalogo.anotar_esgotado(itens)
        assert itens[0]['esgotado'] is False
        assert itens[0]['esgotado_hoje'] is False
        assert itens[0]['tem_em_outros_dias'] is True


# ── datas D+2 ────────────────────────────────────────────────────────

def test_datas_disponiveis_lead_2_comeca_em_d_mais_2(app):
    from app.services import loja_checkout
    with app.app_context():
        base = datetime(2026, 6, 15, 10, 0)          # segunda
        datas = loja_checkout.datas_disponiveis(
            'retirada', base=base, lead_dias=2)
        # primeira data é D+2 (quarta), nunca hoje/amanhã
        assert datas[0] == base.date() + timedelta(days=2)
        assert base.date() not in datas
        assert base.date() + timedelta(days=1) not in datas


def test_datas_disponiveis_sem_lead_inclui_hoje_ou_amanha(app):
    from app.services import loja_checkout
    with app.app_context():
        base = datetime(2026, 6, 15, 10, 0)
        datas = loja_checkout.datas_disponiveis('retirada', base=base)
        assert datas[0] <= base.date() + timedelta(days=1)


def test_lead_do_carrinho(app):
    from app.services import loja_checkout
    with app.app_context():
        enc = _receita(nome='Enc', sob_encomenda=True)
        normal = _receita(nome='Normal', sob_encomenda=False)
        assert loja_checkout.lead_do_carrinho(
            [{'kind': 'receita', 'id': enc.id, 'qtd': 1}]) == 2
        assert loja_checkout.lead_do_carrinho(
            [{'kind': 'receita', 'id': normal.id, 'qtd': 1}]) == 0
        # misto → herda o maior (2)
        assert loja_checkout.lead_do_carrinho(
            [{'kind': 'receita', 'id': normal.id, 'qtd': 1},
             {'kind': 'receita', 'id': enc.id, 'qtd': 1}]) == 2


# ── criar_pedido: trava D+2 + bloqueia express ───────────────────────

def _form_retirada(loja, data, **over):
    f = {'nome': 'Maria Silva', 'email': 'm@x.com', 'cpf': '529.982.247-25',
         'aceite_lgpd': '1', 'modo_entrega': 'retirada',
         'loja_id': str(loja.id), 'data_entrega': data,
         'janela_entrega': '08:00–09:00', **_END_NF}
    f.update(over)
    return f


def test_criar_pedido_rejeita_data_antes_de_d2(app):
    from app.services import loja_checkout
    with app.app_context():
        loja = _loja_origem()
        r = _receita(sob_encomenda=True)
        base = datetime(2026, 6, 15, 10, 0)
        # tenta amanhã (D+1) — inválido pra sob encomenda
        data = (base.date() + timedelta(days=1)).isoformat()
        pedido, erros = loja_checkout.criar_pedido(
            _form_retirada(loja, data),
            [{'kind': 'receita', 'id': r.id, 'qtd': 2}], base=base)
        assert pedido is None
        assert any('encomenda' in e.lower() or 'd+2' in e.lower()
                   for e in erros)


def test_criar_pedido_aceita_d2(app):
    from app.models import MovEstoqueLoja
    from app.services import loja_checkout
    with app.app_context():
        loja = _loja_origem()
        r = _receita(sob_encomenda=True)
        _estoque(loja, r, 3)
        base = datetime(2026, 6, 15, 10, 0)
        data = (base.date() + timedelta(days=2)).isoformat()
        pedido, erros = loja_checkout.criar_pedido(
            _form_retirada(loja, data),
            [{'kind': 'receita', 'id': r.id, 'qtd': 2}], base=base)
        assert erros == []
        assert pedido is not None
        assert pedido.data_entrega == base.date() + timedelta(days=2)
        # criar_pedido não baixa estoque (só no pagamento); e o item é
        # produzido pro pedido de qualquer forma.
        assert MovEstoqueLoja.query.count() == 0


def test_criar_pedido_bloqueia_express(app):
    from app.services import loja_checkout
    with app.app_context():
        loja = _loja_origem()
        r = _receita(sob_encomenda=True)
        base = datetime(2026, 6, 15, 10, 0)
        form = {'nome': 'Maria Silva', 'email': 'm@x.com',
                'cpf': '529.982.247-25', 'aceite_lgpd': '1',
                'modo_entrega': 'express', **_END_NF}
        pedido, erros = loja_checkout.criar_pedido(
            form, [{'kind': 'receita', 'id': r.id, 'qtd': 1}], base=base)
        assert pedido is None
        assert any('express' in e.lower() for e in erros)


# ── não abate EstoqueLoja (produzido pro pedido) ─────────────────────

def test_expandir_estoque_pula_sob_encomenda(app):
    from app.services import loja_estoque_reserva
    with app.app_context():
        r = _receita(sob_encomenda=True)
        p = _pedido_pago(r, qtd=4)
        it = p.itens[0]
        assert loja_estoque_reserva.item_sob_encomenda(it) is True
        assert loja_estoque_reserva._expandir_estoque(it) == []


def test_consumir_nao_baixa_estoque_de_sob_encomenda(app):
    from app.models import MovEstoqueLoja
    from app.services import loja_estoque_reserva
    with app.app_context():
        loja = _loja_origem()
        r = _receita(sob_encomenda=True)
        el = _estoque(loja, r, 10)
        p = _pedido_pago(r, qtd=4)
        loja_estoque_reserva.consumir(p, loja_id=loja.id)
        db.session.commit()
        db.session.refresh(el)
        assert el.quantidade == 10  # intacto — produzido pro pedido
        # nenhum movimento de venda gerado (o item não abate a loja)
        assert MovEstoqueLoja.query.count() == 0


def test_receita_normal_ainda_baixa(app):
    """Guarda: item NÃO sob encomenda continua abatendo o EstoqueLoja."""
    from app.services import loja_estoque_reserva
    with app.app_context():
        loja = _loja_origem()
        r = _receita(sob_encomenda=False)
        el = _estoque(loja, r, 10)
        p = _pedido_pago(r, qtd=4)
        loja_estoque_reserva.consumir(p, loja_id=loja.id)
        db.session.commit()
        db.session.refresh(el)
        assert el.quantidade == 6


# ── balanço firme (produção) ─────────────────────────────────────────

def test_balanco_conta_encomenda_como_firme(app):
    from app.services.previsao_producao import balanco_industria
    with app.app_context():
        r = _receita(sob_encomenda=True)
        _pedido_pago(r, qtd=6, dias=1)
        itens = {i['receita_id']: i for i in
                 balanco_industria(horizonte_dias=7, usar_cache=False)['itens']}
        assert itens[r.id]['comprometido'] >= 6
        assert any(b['loja_nome'] == 'Encomenda site' and b['qtd'] == 6
                   for b in itens[r.id]['breakdown_comprometido'])


def test_balanco_ignora_encomenda_nao_paga_e_cancelada(app):
    from app.services.previsao_producao import balanco_industria
    with app.app_context():
        r = _receita(sob_encomenda=True)
        _pedido_pago(r, qtd=6, dias=1, status='aguardando_pagamento')
        _pedido_pago(r, qtd=9, dias=1, status='cancelado')
        itens = {i['receita_id']: i for i in
                 balanco_industria(horizonte_dias=7, usar_cache=False)['itens']}
        # nenhum pedido ativo → receita sem demanda (pode nem aparecer);
        # se aparecer, sem linha de encomenda.
        it = itens.get(r.id)
        assert it is None or not any(
            b['loja_nome'] == 'Encomenda site'
            for b in it['breakdown_comprometido'])


def test_balanco_ignora_item_normal_do_site(app):
    """Item do site NÃO sob encomenda não entra no balanço (sai da
    prateleira, não é produzido sob demanda)."""
    from app.services.previsao_producao import balanco_industria
    with app.app_context():
        r = _receita(sob_encomenda=False)
        _pedido_pago(r, qtd=6, dias=1)
        itens = {i['receita_id']: i for i in
                 balanco_industria(horizonte_dias=7, usar_cache=False)['itens']}
        it = itens.get(r.id)
        assert it is None or not any(
            b['loja_nome'] == 'Encomenda site'
            for b in it['breakdown_comprometido'])


# ── tela do padeiro: separação + pré-preparo ─────────────────────────

def test_padeiro_separacao_mostra_encomenda(app):
    from app.blueprints.padeiro.routes import _dados_listas
    with app.app_context():
        r = _receita(sob_encomenda=True)
        p = _pedido_pago(r, qtd=3, dias=0)   # entrega hoje
        dados = _dados_listas(hoje(), eh_hoje=True)
        cards = [c for c in dados['a_separar'] if c['tipo'] == 'online']
        assert len(cards) == 1
        assert cards[0]['id'] == p.id
        assert cards[0]['itens'][0]['qtd'] == 3


def test_padeiro_separacao_nao_mostra_pedido_normal(app):
    from app.blueprints.padeiro.routes import _dados_listas
    with app.app_context():
        r = _receita(sob_encomenda=False)
        _pedido_pago(r, qtd=3, dias=0)
        dados = _dados_listas(hoje(), eh_hoje=True)
        assert not [c for c in dados['a_separar'] if c['tipo'] == 'online']


def test_padeiro_pre_preparo_vespera(app):
    """Pré-preparo do dia D mostra as encomendas de D+1."""
    from app.models import Usuario
    with app.app_context():
        r = _receita(sob_encomenda=True, estado_padrao='assado')
        _pedido_pago(r, qtd=4, dias=1)       # entrega amanhã
        u = Usuario(nome='Pad', login='pad', papel='padeiro')
        u.set_senha('x' * 8)
        db.session.add(u)
        db.session.commit()
        c = app.test_client()
        with c.session_transaction() as s:
            s['_user_id'] = str(u.id)
            s['_fresh'] = True
        # dia = hoje → alvo = amanhã (a entrega da encomenda)
        resp = c.get(f'/padeiro/preparar.json?data={hoje().isoformat()}')
        assert resp.status_code == 200
        data = resp.get_json()
        nomes = [x['nome'] for x in data['itens']]
        assert r.nome in nomes
