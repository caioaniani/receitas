"""Sob encomenda D+2 (dono 21/07/2026).

Produto/receita marcado `sob_encomenda`:
- no site só vende pra ENTREGA/RETIRADA a partir de D+2 (dois dias à frente,
  janela das 08:00) — nunca hoje/amanhã nem express (same-day);
- é PRODUZIDO PRO PEDIDO. Desde 07/08/2026 RESPEITA o plano-do-dia
  (curadoria por data — decisão do dono, caso Caixa de Mini); estoque
  FÍSICO segue fora
  e a venda NÃO abate o EstoqueLoja físico;
- o pedido pago entra na produção do padeiro (separação + pré-preparo) e no
  balanço firme da indústria (cronograma), estilo B2B.

Testa cada camada isoladamente.
"""
from datetime import datetime, timedelta

import pytest

from app.extensions import db
from app.utils import hoje


@pytest.fixture(autouse=True)
def _hoje_e_segunda_fixa(congela_hoje):
    """Producao seg-sex + janela semanal tornaram o motor weekday-sensivel
    — congela numa SEGUNDA fixa (mesma fixture dos arquivos do cronograma;
    caso real 19/08/2026: test_cronograma_edit quebrou na QUARTA porque o
    indice 3 do grid caiu no sabado bloqueado)."""
    congela_hoje()


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


# ── vitrine: disponível por default, mas o PLANO-DO-DIA manda ────────
# CONTRATO NOVO 07/08/2026 (decisão do dono, caso "Caixa de Mini vendida
# pro Dia dos Pais" — SUBSTITUI o "sempre disponível" de 21/07): sob
# encomenda respeita o plano-do-dia como qualquer item. Sem plano = segue
# fail-open (disponível); plano zerado no dia curado = barrado. O estoque
# FÍSICO continua fora (produzido pro pedido).

def test_vitrine_sob_encomenda_sem_plano_segue_disponivel(app):
    from app.services import loja_catalogo
    with app.app_context():
        r = _receita(sob_encomenda=True)
        itens = [loja_catalogo.por_id_publicado('receita', r.id)]
        loja_catalogo.anotar_esgotado(itens)
        assert itens[0]['esgotado'] is False
        assert itens[0]['esgotado_hoje'] is False
        assert itens[0]['tem_em_outros_dias'] is True


def test_vitrine_sob_encomenda_esgota_quando_plano_zera_a_janela(app):
    """Plano zerado em TODOS os dias >= D+2 da janela → esgotado duro; um
    único dia >= D+2 com saldo → disponível de novo."""
    from app.services import loja_catalogo, loja_plano_dia
    with app.app_context():
        r = _receita(sob_encomenda=True)
        for i in range(2, 16):
            loja_plano_dia.definir('receita', r.id,
                                   hoje() + timedelta(days=i), 0)
        itens = [loja_catalogo.por_id_publicado('receita', r.id)]
        loja_catalogo.anotar_esgotado(itens)
        assert itens[0]['esgotado'] is True

        loja_plano_dia.definir('receita', r.id,
                               hoje() + timedelta(days=5), 10)
        itens = [loja_catalogo.por_id_publicado('receita', r.id)]
        loja_catalogo.anotar_esgotado(itens)
        assert itens[0]['esgotado'] is False


def test_tem_estoque_para_dia_respeita_plano_em_sob_encomenda(app):
    from app.services import loja_catalogo, loja_plano_dia
    with app.app_context():
        r = _receita(sob_encomenda=True)
        alvo = hoje() + timedelta(days=3)
        assert loja_catalogo.tem_estoque_para_dia('receita', r.id, alvo)
        loja_plano_dia.definir('receita', r.id, alvo, 0)
        assert not loja_catalogo.tem_estoque_para_dia('receita', r.id, alvo)
        loja_plano_dia.definir('receita', r.id, alvo, 4)
        assert loja_catalogo.tem_estoque_para_dia('receita', r.id, alvo)


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


def test_rebaixar_pedido_pula_sob_encomenda(app):
    """Correção owner (reduzir qtd de pedido pago) NÃO pode reintroduzir a
    baixa de item sob encomenda — senão ele passa a contar 2x (venda + firme).
    Achado de revisão 21/07/2026."""
    from app.models import PedidoOnline, PedidoOnlineItem
    from app.services import loja_pagamento
    with app.app_context():
        loja = _loja_origem()
        enc = _receita(nome='Enc', sob_encomenda=True)
        nrm = _receita(nome='Normal', sob_encomenda=False)
        el_enc = _estoque(loja, enc, 10)
        el_nrm = _estoque(loja, nrm, 10)
        p = PedidoOnline(codigo='MIX1', nome_cliente='X',
                         email_cliente='c@x.com', modo_entrega='retirada',
                         status='pago', data_entrega=hoje() + timedelta(days=2))
        db.session.add(p)
        db.session.flush()
        for r, q in ((enc, 3), (nrm, 3)):
            db.session.add(PedidoOnlineItem(
                pedido_id=p.id, kind='receita', receita_id=r.id, nome=r.nome,
                preco_unitario=8, quantidade=q, subtotal=8 * q))
        db.session.commit()
        loja_pagamento._rebaixar_pedido(p, loja.id, 'Site #MIX1#v1',
                                        'site:MIX1#v1')
        db.session.commit()
        db.session.refresh(el_enc)
        db.session.refresh(el_nrm)
        assert el_enc.quantidade == 10   # sob encomenda intacto
        assert el_nrm.quantidade == 7    # normal baixou 3


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


@pytest.mark.loja_host
def test_pagina_do_produto_respeita_o_D2_no_seletor_de_data(app, monkeypatch):
    """Caso real (dono 26/07/2026): o checkout já travava em D+2, mas a
    página do produto abria o seletor em HOJE dizendo "✓ disponível pra
    essa data" — o cliente só descobria o bloqueio no fim. O `min` e o
    valor padrão do seletor têm que nascer com o lead."""
    from datetime import timedelta

    from app.extensions import db
    from app.models import Produto
    from app.services import loja_checkout
    from app.utils import hoje
    with app.app_context():
        p = Produto(nome='Caixa Sob Encomenda', categoria='Cestas',
                    preco_site=100, ativo=True, sob_encomenda=True)
        db.session.add(p)
        db.session.commit()
        pid, slug = p.id, 'caixa-sob-encomenda'
        esperado = (hoje() + timedelta(
            days=loja_checkout.ENCOMENDA_LEAD_DIAS)).isoformat()

    monkeypatch.setenv('LOJA_VISIVEL', '1')
    c = app.test_client()
    r = c.get(f'/loja/{slug}-p{pid}')
    corpo = r.get_data(as_text=True)
    assert r.status_code == 200
    assert f'min="{esperado}"' in corpo
    assert f'value="{esperado}"' in corpo
    assert f'min="{hoje().isoformat()}"' not in corpo   # nunca oferece hoje


@pytest.mark.loja_host
def test_produto_normal_continua_podendo_hoje(app, monkeypatch):
    """Guarda: o lead só vale pra sob encomenda."""
    from app.extensions import db
    from app.models import Produto
    from app.utils import hoje
    with app.app_context():
        p = Produto(nome='Caixa Normal', categoria='Cestas',
                    preco_site=100, ativo=True)
        db.session.add(p)
        db.session.commit()
        pid = p.id
    monkeypatch.setenv('LOJA_VISIVEL', '1')
    c = app.test_client()
    corpo = c.get(f'/loja/caixa-normal-p{pid}').get_data(as_text=True)
    assert f'min="{hoje().isoformat()}"' in corpo


def _menu_pago(dias=2, escolha=((20, 'Mini Croissant Nutella'),
                                (10, 'Mini Danish de alho poró'))):
    """Menu configurável SOB ENCOMENDA pago, com composição ESCOLHIDA
    persistida no pedido (como o checkout grava)."""
    from app.models import PedidoOnline, PedidoOnlineItem, PedidoOnlineItemComponente, Produto, ProdutoItem
    minis = []
    for _q, nome in escolha:
        minis.append(_receita(nome=nome, sob_encomenda=True,
                              estado_padrao='assado'))
    menu = Produto(nome='Menu Degustação dos Minis', categoria='Cestas',
                   preco_site=300.0, ativo=True, menu_configuravel=True,
                   menu_total_unidades=30, sob_encomenda=True)
    db.session.add(menu)
    db.session.commit()
    pis = []
    for m in minis:
        pi = ProdutoItem(produto_id=menu.id, tipo='receita',
                         item_nome=m.nome, quantidade=5, receita_id=m.id)
        db.session.add(pi)
        pis.append(pi)
    db.session.commit()
    p = PedidoOnline(codigo=f'MENU{dias}', nome_cliente='Cliente Menu',
                     email_cliente='m@x.com', modo_entrega='agendada',
                     status='pago', data_entrega=hoje() + timedelta(days=dias))
    db.session.add(p)
    db.session.flush()
    it = PedidoOnlineItem(pedido_id=p.id, kind='produto',
                          produto_id=menu.id, nome=menu.nome,
                          preco_unitario=300, quantidade=1, subtotal=300)
    db.session.add(it)
    db.session.flush()
    for (q, _nome), m, pi in zip(escolha, minis, pis):
        db.session.add(PedidoOnlineItemComponente(
            item_id=it.id, produto_item_id=pi.id, tipo='receita',
            receita_id=m.id, nome=m.nome, quantidade=q))
    db.session.commit()
    return p, menu, minis


# ── Fixes 31/07/2026 (caso real: menu de minis vendido pra domingo) ───

def test_padeiro_hoje_mostra_encomenda_de_data_futura(app):
    """Vendido na sexta pra entrega no domingo: a TV de HOJE tem que
    mostrar — o item é D+2 justamente porque a produção começa antes; o
    cronograma já agenda fornadas dias antes da entrega. Antes o card só
    aparecia no próprio dia (e o padeiro ficou sem saber da venda)."""
    from app.blueprints.padeiro.routes import _dados_listas
    with app.app_context():
        r = _receita(sob_encomenda=True)
        p = _pedido_pago(r, qtd=3, dias=2)   # entrega DEPOIS de amanhã
        dados = _dados_listas(hoje(), eh_hoje=True)
        cards = [c for c in dados['a_separar'] if c['tipo'] == 'online']
        assert [c['id'] for c in cards] == [p.id]
        assert cards[0]['data_entrega'] == hoje() + timedelta(days=2)


def test_padeiro_hoje_nao_mostra_encomenda_entregue(app):
    """O card some quando o pedido conclui — senão a fila acumula lixo."""
    from app.blueprints.padeiro.routes import _dados_listas
    with app.app_context():
        r = _receita(sob_encomenda=True)
        _pedido_pago(r, qtd=3, dias=2, status='entregue')
        dados = _dados_listas(hoje(), eh_hoje=True)
        assert not [c for c in dados['a_separar'] if c['tipo'] == 'online']


def test_card_do_menu_explode_a_composicao_escolhida(app):
    """"1x Menu Degustação" não produz nada: o padeiro precisa dos MINIS que
    o cliente montou (20x Nutella, 10x Danish) — a mesma fonte do bloco 2c
    do balanço (composição persistida no pedido, nunca a pré-seleção)."""
    from app.blueprints.padeiro.routes import _dados_listas
    with app.app_context():
        p, _menu, _minis = _menu_pago(dias=2)
        dados = _dados_listas(hoje(), eh_hoje=True)
        card = [c for c in dados['a_separar'] if c['tipo'] == 'online'][0]
        linhas = {i['nome'].lstrip('· '): i['qtd'] for i in card['itens']}
        assert linhas.get('Mini Croissant Nutella') == 20
        assert linhas.get('Mini Danish de alho poró') == 10
        # E o total escolhido, não a pré-seleção do cadastro (5+5).
        assert 20 + 10 == 30


def test_pre_preparo_do_menu_lista_os_minis(app):
    """A véspera precisa listar os minis escolhidos, cada um com o SEU
    estado — não "1x Menu Degustação, assado"."""
    from app.models import Usuario
    with app.app_context():
        _menu_pago(dias=1)                   # entrega amanhã
        u = Usuario(nome='Pad', login='padm', papel='padeiro')
        u.set_senha('x' * 8)
        db.session.add(u)
        db.session.commit()
        c = app.test_client()
        with c.session_transaction() as s:
            s['_user_id'] = str(u.id)
            s['_fresh'] = True
        resp = c.get(f'/padeiro/preparar.json?data={hoje().isoformat()}')
        data = resp.get_json()
        por_nome = {x['nome']: x['qtd'] for x in data['itens']}
        assert por_nome.get('Mini Croissant Nutella') == 20
        assert por_nome.get('Mini Danish de alho poró') == 10
        assert 'Menu Degustação dos Minis' not in por_nome


# ── Plano-do-dia no CHECKOUT e na RESERVA (contrato novo 07/08/2026) ──

def test_checkout_barra_encomenda_em_dia_zerado_no_plano(app):
    """Plano zerado na data escolhida barra a encomenda no criar_pedido —
    é o que garante "dos 9 somente as 4 cestas do plano" (caso Caixa de
    Mini no Dia dos Pais)."""
    from app.services import loja_checkout, loja_plano_dia
    with app.app_context():
        lo = _loja_origem()
        r = _receita(sob_encomenda=True)
        alvo = hoje() + timedelta(days=4)
        loja_plano_dia.definir('receita', r.id, alvo, 0)
        form = {
            'modo_entrega': 'retirada', 'nome': 'Fulano de Tal',
            'email': 'f@x.com', 'telefone': '11999998888',
            'cpf': '529.982.247-25', 'aceite_lgpd': '1',
            'loja_id': str(lo.id), 'data_entrega': alvo.isoformat(),
            'janela_entrega': '10:00–11:00',
            'cep': '04077-000', 'logradouro': 'Rua X', 'numero': '10',
            'bairro': 'Moema', 'cidade': 'São Paulo', 'uf': 'SP',
        }
        itens = [{'kind': 'receita', 'id': r.id, 'qtd': 1}]
        _, erros = loja_checkout.criar_pedido(form, itens)
        assert any('não está disponível' in e for e in erros), erros

        # Com saldo no plano, o mesmo pedido passa.
        loja_plano_dia.definir('receita', r.id, alvo, 5)
        pedido, erros = loja_checkout.criar_pedido(form, itens)
        assert erros == [] and pedido is not None


def test_pagamento_reserva_e_devolve_plano_pra_encomenda(app):
    """Sem a reserva, o cap do plano não seguraria nada (10 planejados
    venderiam 100). O cancelamento devolve — e pedido ANTIGO (nunca
    reservou, linha inexistente) não cria saldo fantasma."""
    from app.models import EstoqueSitePlano
    from app.services import loja_pagamento, loja_plano_dia
    with app.app_context():
        _loja_origem()
        r = _receita(sob_encomenda=True)
        p = _pedido_pago(r, qtd=3, dias=4)
        alvo = p.data_entrega
        loja_plano_dia.definir('receita', r.id, alvo, 10)

        loja_pagamento._reservar_no_plano_do_dia(p)
        linha = EstoqueSitePlano.query.filter_by(
            kind='receita', item_id=r.id, data=alvo).first()
        assert linha.qtd_reservada == 3

        loja_pagamento._devolver_ao_plano_do_dia(p)
        db.session.refresh(linha)
        assert linha.qtd_reservada == 0

        # Devolver de novo (pedido antigo/duplicado): trunca em 0.
        loja_pagamento._devolver_ao_plano_do_dia(p)
        db.session.refresh(linha)
        assert linha.qtd_reservada == 0
