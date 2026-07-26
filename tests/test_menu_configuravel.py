"""Menu degustação CONFIGURÁVEL do site (26/07/2026).

Pedido do dono: "menu degustação dos minis, uma pré-seleção de 5 de cada;
se o cliente quiser alterar as quantidades não tem problema, porém ele deve
ser obrigado a selecionar 30 unidades dos minis independente de quais".
Decisões dele: preço cadastrado POR MINI (o menu custa a soma do escolhido),
máximo 10 de cada, regra só pra este menu.

O que estes testes travam (a parte de ESTOQUE tem peso especial —
CLAUDE.md): a baixa/reserva explode pela composição ESCOLHIDA e persistida
no pedido, NUNCA pelo cadastro da cesta (que guarda só a pré-seleção).
"""
from decimal import Decimal

# ── Cenário: menu com 3 minis, pré-seleção 5/5/5, total 15, teto 10 ──────
# (mesma forma do caso real, com números menores pra o teste ficar legível)

def _menu(db, *, total=15, teto=10, precos=(2.0, 3.0, 4.0), padroes=(5, 5, 5)):
    from app.models import Produto, ProdutoItem, Receita
    menu = Produto(nome='Menu Degustação dos Minis', categoria='Cestas',
                   preco_site=1.0,          # só publica; preço real = soma
                   ativo=True, menu_configuravel=True,
                   menu_total_unidades=total, menu_max_por_item=teto)
    db.session.add(menu)
    db.session.flush()
    minis = []
    for i, (preco, padrao) in enumerate(zip(precos, padroes), start=1):
        r = Receita(nome=f'Mini {i}', categoria='Paes', rendimento_qtd=1,
                    rendimento_unidade='un', peso_base=100)
        db.session.add(r)
        db.session.flush()
        db.session.add(ProdutoItem(
            produto_id=menu.id, tipo='receita', receita_id=r.id,
            item_nome=r.nome, quantidade=padrao,
            preco_menu=Decimal(str(preco)) if preco is not None else None))
        minis.append(r)
    db.session.commit()
    return menu, minis


def _pis(menu):
    """ids dos ProdutoItem do menu, na ordem de cadastro."""
    return [pi.id for pi in sorted(menu.itens, key=lambda x: x.id)]


def _site_loja(db):
    from app.models import AppConfig, Loja
    loja = Loja(nome='Loja do Site', ativa=True, endereco='Rua Site, 1')
    db.session.add(loja)
    db.session.commit()
    AppConfig.set('loja_site_estoque_id', loja.id)
    db.session.commit()
    return loja


# ── Serviço loja_menu: a regra pura ──────────────────────────────────────

def test_composicao_padrao_vem_do_cadastro(app):
    from app.extensions import db
    from app.services import loja_menu
    with app.app_context():
        menu, _ = _menu(db)
        assert loja_menu.composicao_padrao(menu) == dict.fromkeys(_pis(menu), 5)


def test_normalizar_clampa_no_teto(app):
    from app.extensions import db
    from app.services import loja_menu
    with app.app_context():
        menu, _ = _menu(db, teto=10)
        a, b, c = _pis(menu)
        comp = loja_menu.normalizar(menu, [[a, 99], [b, 3], [c, 2]])
        assert comp[a] == 10          # 99 vira o teto, não passa
        assert comp[b] == 3 and comp[c] == 2


def test_normalizar_descarta_slot_de_outro_menu(app):
    """POST forjado com pi_id que não é deste menu: o slot some (não vira
    item do pedido nem baixa estoque)."""
    from app.extensions import db
    from app.services import loja_menu
    with app.app_context():
        menu, _ = _menu(db)
        outro, _ = _menu(db)
        a = _pis(menu)[0]
        intruso = _pis(outro)[0]
        comp = loja_menu.normalizar(menu, [[a, 5], [intruso, 5]])
        assert intruso not in comp
        assert comp == {a: 5}


def test_normalizar_vazio_cai_na_pre_selecao(app):
    from app.extensions import db
    from app.services import loja_menu
    with app.app_context():
        menu, _ = _menu(db)
        assert loja_menu.normalizar(menu, None) == \
            loja_menu.composicao_padrao(menu)


def test_validar_exige_o_total_exato(app):
    from app.extensions import db
    from app.services import loja_menu
    with app.app_context():
        menu, _ = _menu(db, total=15)
        a, b, c = _pis(menu)
        assert loja_menu.validar(menu, {a: 5, b: 5, c: 5}) is None
        erro = loja_menu.validar(menu, {a: 5, b: 5, c: 4})
        assert erro and '15' in erro and '14' in erro


def test_preco_e_a_soma_do_escolhido(app):
    """Decisão do dono: 'cadastrar preço por mini'. 10x2 + 3x3 + 2x4 = 37."""
    from app.extensions import db
    from app.services import loja_menu
    with app.app_context():
        menu, _ = _menu(db, precos=(2.0, 3.0, 4.0))
        a, b, c = _pis(menu)
        assert loja_menu.preco(menu, {a: 10, b: 3, c: 2}) == Decimal('37')


def test_preco_none_quando_mini_escolhido_nao_tem_preco(app):
    """Fail-close: sem preço cadastrado não se vende (dinheiro tem peso
    especial). Cobrar a menos seria pior que não vender."""
    from app.extensions import db
    from app.services import loja_menu
    with app.app_context():
        menu, _ = _menu(db, precos=(2.0, None, 4.0))
        a, b, c = _pis(menu)
        assert loja_menu.preco(menu, {a: 5, c: 5}) == Decimal('30')
        assert loja_menu.preco(menu, {a: 5, b: 5, c: 5}) is None


# ── Vitrine ──────────────────────────────────────────────────────────────

def test_vitrine_mostra_o_preco_da_pre_selecao(app):
    """O preco_site do menu só PUBLICA; o preço exibido é o real (soma)."""
    from app.extensions import db
    from app.services import loja_catalogo
    with app.app_context():
        menu, _ = _menu(db, precos=(2.0, 3.0, 4.0), padroes=(5, 5, 5))
        d = loja_catalogo.por_id_publicado('produto', menu.id)
        assert d['preco'] == 45.0                 # 5*(2+3+4)
        assert d['menu']['total'] == 15
        assert d['menu']['max_por_item'] == 10
        assert len(d['menu']['slots']) == 3


def test_menu_sem_preco_por_item_sai_da_vitrine(app):
    from app.extensions import db
    from app.services import loja_catalogo
    with app.app_context():
        menu, _ = _menu(db, precos=(2.0, None, 4.0))
        assert loja_catalogo.por_id_publicado('produto', menu.id) is None
        nomes = [i['nome'] for i in loja_catalogo.produtos_publicados()]
        assert menu.nome not in nomes


# ── Checkout: o SERVIDOR é a autoridade ──────────────────────────────────

def test_montar_itens_usa_a_escolha_e_recalcula_o_preco(app):
    from app.extensions import db
    from app.services import loja_checkout
    with app.app_context():
        menu, _ = _menu(db, total=15, precos=(2.0, 3.0, 4.0))
        a, b, c = _pis(menu)
        itens, avisos = loja_checkout.montar_itens([
            {'kind': 'produto', 'id': menu.id, 'qtd': 2,
             'comp': [[a, 10], [b, 3], [c, 2]]}])
        assert avisos == []
        assert len(itens) == 1
        assert itens[0]['preco'] == Decimal('37')
        assert itens[0]['subtotal'] == Decimal('74')
        assert itens[0]['comp'] == {a: 10, b: 3, c: 2}


def test_montar_itens_recusa_total_errado(app):
    """Aba parada / carrinho velho / POST forjado: sai do pedido com aviso.
    NUNCA 'conserta' em silêncio."""
    from app.extensions import db
    from app.services import loja_checkout
    with app.app_context():
        menu, _ = _menu(db, total=15)
        a, b, c = _pis(menu)
        itens, avisos = loja_checkout.montar_itens([
            {'kind': 'produto', 'id': menu.id, 'qtd': 1,
             'comp': [[a, 5], [b, 5], [c, 4]]}])
        assert itens == []
        assert avisos and '15' in avisos[0]


def test_montar_itens_forjar_acima_do_teto_nao_passa(app):
    """20 de um item vira 10 (teto) — e aí o total não fecha, então o menu
    é recusado. O cliente não leva 20 de um mini pagando por 10."""
    from app.extensions import db
    from app.services import loja_checkout
    with app.app_context():
        menu, _ = _menu(db, total=15, teto=10)
        a, b, c = _pis(menu)
        itens, avisos = loja_checkout.montar_itens([
            {'kind': 'produto', 'id': menu.id, 'qtd': 1,
             'comp': [[a, 20], [b, 0], [c, 0]]}])
        assert itens == []
        assert avisos


def test_montar_itens_sem_comp_usa_a_pre_selecao(app):
    from app.extensions import db
    from app.services import loja_checkout
    with app.app_context():
        menu, _ = _menu(db, total=15, precos=(2.0, 3.0, 4.0))
        itens, avisos = loja_checkout.montar_itens([
            {'kind': 'produto', 'id': menu.id, 'qtd': 1}])
        assert avisos == []
        assert itens[0]['preco'] == Decimal('45')


# ── ESTOQUE: o coração do risco ──────────────────────────────────────────

def _pedido_com_menu(db, menu, comp, *, loja, qtd=1, codigo='MENU0001'):
    """Cria o pedido do jeito que o checkout cria (composição persistida)."""
    from app.models import Cliente, PedidoOnline, PedidoOnlineItem, PedidoOnlineItemComponente, ProdutoItem
    cli = Cliente(nome='Maria', email=f'{codigo.lower()}@x.com')
    db.session.add(cli)
    db.session.commit()
    p = PedidoOnline(codigo=codigo, cliente_id=cli.id, nome_cliente='Maria',
                     email_cliente=cli.email, modo_entrega='retirada',
                     loja_retirada_id=loja.id, status='aguardando_pagamento',
                     subtotal=Decimal('0'), frete_valor=Decimal('0'),
                     valor_total=Decimal('0'))
    db.session.add(p)
    db.session.flush()
    it = PedidoOnlineItem(kind='produto', produto_id=menu.id, nome=menu.nome,
                          preco_unitario=Decimal('10'), quantidade=qtd,
                          subtotal=Decimal('10') * qtd)
    for pi_id, q in comp.items():
        pi = ProdutoItem.query.get(pi_id)
        it.componentes.append(PedidoOnlineItemComponente(
            produto_item_id=pi.id, tipo='receita', receita_id=pi.receita_id,
            nome=pi.item_nome, quantidade=q, preco_unitario=pi.preco_menu))
    p.itens.append(it)
    p.recalcular_total()
    db.session.commit()
    return p


def _linha(db, loja, receita):
    from app.models import EstoqueLoja
    return EstoqueLoja.query.filter_by(loja_id=loja.id,
                                       receita_id=receita.id).first()


def test_reserva_usa_a_escolha_do_cliente_nao_o_cadastro(app):
    """O CADASTRO diz 5/5/5. O cliente escolheu 10/3/2. A reserva TEM que
    seguir o cliente — senão a loja segura o item errado."""
    from app.extensions import db
    from app.models import EstoqueLoja
    from app.services import loja_estoque_reserva
    with app.app_context():
        loja = _site_loja(db)
        menu, minis = _menu(db, total=15, padroes=(5, 5, 5))
        for m in minis:
            db.session.add(EstoqueLoja(loja_id=loja.id, receita_id=m.id,
                                       quantidade=50))
        db.session.commit()
        a, b, c = _pis(menu)
        ped = _pedido_com_menu(db, menu, {a: 10, b: 3, c: 2}, loja=loja)

        loja_estoque_reserva.reservar(ped, loja_id=loja.id)
        db.session.commit()

        assert _linha(db, loja, minis[0]).quantidade_reservada == 10
        assert _linha(db, loja, minis[1]).quantidade_reservada == 3
        assert _linha(db, loja, minis[2]).quantidade_reservada == 2


def test_baixa_real_bate_com_a_reserva(app):
    """Reserva e baixa TÊM que mexer nas mesmas linhas — senão a
    `quantidade_reservada` não volta a zero e o saldo virtual fica travado."""
    from app.extensions import db
    from app.models import EstoqueLoja
    from app.services import loja_estoque_reserva
    with app.app_context():
        loja = _site_loja(db)
        menu, minis = _menu(db, total=15)
        for m in minis:
            db.session.add(EstoqueLoja(loja_id=loja.id, receita_id=m.id,
                                       quantidade=50))
        db.session.commit()
        a, b, c = _pis(menu)
        ped = _pedido_com_menu(db, menu, {a: 10, b: 3, c: 2}, loja=loja)

        loja_estoque_reserva.reservar(ped, loja_id=loja.id)
        db.session.commit()
        loja_estoque_reserva.consumir(ped, loja_id=loja.id)
        db.session.commit()

        for m, esperado in zip(minis, (10, 3, 2)):
            el = _linha(db, loja, m)
            assert el.quantidade == 50 - esperado
            assert el.quantidade_reservada == 0


def test_dois_menus_multiplicam_a_composicao(app):
    """2 menus x 10 do mini A = 20 unidades."""
    from app.extensions import db
    from app.models import EstoqueLoja
    from app.services import loja_estoque_reserva
    with app.app_context():
        loja = _site_loja(db)
        menu, minis = _menu(db, total=15)
        for m in minis:
            db.session.add(EstoqueLoja(loja_id=loja.id, receita_id=m.id,
                                       quantidade=99))
        db.session.commit()
        a, b, c = _pis(menu)
        ped = _pedido_com_menu(db, menu, {a: 10, b: 3, c: 2}, loja=loja, qtd=2)

        loja_estoque_reserva.consumir(ped, loja_id=loja.id)
        db.session.commit()

        assert _linha(db, loja, minis[0]).quantidade == 99 - 20
        assert _linha(db, loja, minis[1]).quantidade == 99 - 6
        assert _linha(db, loja, minis[2]).quantidade == 99 - 4


def test_cesta_comum_continua_explodindo_pelo_cadastro(app):
    """Regressão: cesta de composição FIXA não é afetada pelo menu."""
    from app.extensions import db
    from app.models import EstoqueLoja, Produto, ProdutoItem, Receita
    from app.services import loja_estoque_reserva
    with app.app_context():
        loja = _site_loja(db)
        r = Receita(nome='Pão da Cesta', categoria='Paes', rendimento_qtd=1,
                    rendimento_unidade='un', peso_base=500)
        db.session.add(r)
        db.session.flush()
        cesta = Produto(nome='Cesta Fixa', categoria='Cestas', preco_site=50,
                        ativo=True)
        db.session.add(cesta)
        db.session.flush()
        db.session.add(ProdutoItem(produto_id=cesta.id, tipo='receita',
                                   receita_id=r.id, item_nome=r.nome,
                                   quantidade=3))
        db.session.add(EstoqueLoja(loja_id=loja.id, receita_id=r.id,
                                   quantidade=20))
        db.session.commit()
        ped = _pedido_com_menu(db, cesta, {}, loja=loja, codigo='FIXA0001')

        loja_estoque_reserva.consumir(ped, loja_id=loja.id)
        db.session.commit()

        assert _linha(db, loja, r).quantidade == 20 - 3   # cadastro manda


# ── Carrinho na sessão: a escolha tem que sobreviver ─────────────────────

def test_sessao_preserva_a_composicao(app):
    from app.blueprints.loja import routes as loja_routes
    with app.test_request_context('/loja/'):
        from flask import session
        loja_routes._set_carrinho_sessao([
            {'kind': 'produto', 'id': 7, 'qtd': 1, 'comp': [[3, 10], [4, 5]]}])
        assert session['carrinho'][0]['comp'] == [[3, 10], [4, 5]]
        assert loja_routes._carrinho_sessao()[0]['comp'] == [[3, 10], [4, 5]]


def test_composicoes_diferentes_sao_linhas_separadas(app):
    """Sem isso, dois menus montados diferente somariam quantidade na mesma
    linha e o cliente receberia dois iguais."""
    from app.blueprints.loja import routes as loja_routes
    with app.test_request_context('/loja/'):
        norm = loja_routes._set_carrinho_sessao([
            {'kind': 'produto', 'id': 7, 'qtd': 1, 'comp': [[3, 10], [4, 5]]},
            {'kind': 'produto', 'id': 7, 'qtd': 1, 'comp': [[3, 5], [4, 10]]}])
        assert len(norm) == 2


def test_mesma_composicao_soma_na_mesma_linha(app):
    from app.blueprints.loja import routes as loja_routes
    with app.test_request_context('/loja/'):
        norm = loja_routes._set_carrinho_sessao([
            {'kind': 'produto', 'id': 7, 'qtd': 1, 'comp': [[3, 10], [4, 5]]},
            {'kind': 'produto', 'id': 7, 'qtd': 2, 'comp': [[4, 5], [3, 10]]}])
        assert len(norm) == 1
        assert norm[0]['qtd'] == 3


def test_comp_lixo_nao_derruba_o_carrinho(app):
    from app.blueprints.loja import routes as loja_routes
    with app.test_request_context('/loja/'):
        norm = loja_routes._set_carrinho_sessao([
            {'kind': 'produto', 'id': 7, 'qtd': 1,
             'comp': ['x', [1], [2, 'a'], [3, 4]]}])
        assert norm[0]['comp'] == [[3, 4]]


# ── Achados da revisão independente (26/07/2026) ─────────────────────────

def test_escolha_invalidada_nao_vira_pre_selecao_em_silencio(app):
    """CRÍTICO. `salvar_composicao` APAGA e RECRIA os ProdutoItem e o
    Postgres nunca reusa id — qualquer edição do menu invalida os `pi_id`
    de todo carrinho em voo. Antes, `normalizar` caía na pré-seleção e o
    cliente era COBRADO outro valor e RECEBIA outra composição, sem aviso."""
    from app.extensions import db
    from app.services import loja_checkout, loja_menu
    with app.app_context():
        menu, _ = _menu(db, total=15, precos=(2.0, 3.0, 4.0))
        obsoletos = [pi + 10_000 for pi in _pis(menu)]   # ids que não existem
        comp = loja_menu.normalizar(menu, [[obsoletos[0], 10],
                                           [obsoletos[1], 3],
                                           [obsoletos[2], 2]])
        assert comp == {}, 'não pode cair na pré-seleção'
        erro = loja_menu.validar(menu, comp)
        assert erro and 'mudou' in erro

        itens, avisos = loja_checkout.montar_itens([
            {'kind': 'produto', 'id': menu.id, 'qtd': 1,
             'comp': [[obsoletos[0], 10]]}])
        assert itens == []
        assert avisos and 'mudou' in avisos[0]


def test_slot_fora_da_pre_selecao_sem_preco_tira_o_menu_do_ar(app):
    """O cliente pode escolher QUALQUER slot — então todos precisam de
    preço, não só os da pré-seleção. Antes, um slot com quantidade 0 e
    preço vazio passava no gate e estourava a página do produto."""
    from app.extensions import db
    from app.services import loja_catalogo
    with app.app_context():
        menu, _ = _menu(db, total=15, precos=(2.0, 3.0, None),
                        padroes=(10, 5, 0))
        assert loja_catalogo.por_id_publicado('produto', menu.id) is None


def test_pre_selecao_que_nao_fecha_o_total_sai_da_vitrine(app):
    """Publicar assim mostraria um preço que nunca poderia ser cobrado e o
    cliente levaria um bloqueio no checkout sem saber o que fazer."""
    from app.extensions import db
    from app.services import loja_catalogo
    with app.app_context():
        menu, _ = _menu(db, total=15, padroes=(5, 5, 5))
        menu.menu_total_unidades = 30          # pré-seleção soma 15, não 30
        db.session.commit()
        assert loja_catalogo.por_id_publicado('produto', menu.id) is None


def test_preco_zero_no_menu_nao_publica(app):
    """R$ 0,00 na vitrine + 'saiu de catálogo' no checkout (o gate genérico
    trata preço 0 como ausente). Fail-close antes disso."""
    from app.extensions import db
    from app.services import loja_catalogo
    with app.app_context():
        menu, _ = _menu(db, total=15, precos=(0.0, 0.0, 0.0))
        assert loja_catalogo.por_id_publicado('produto', menu.id) is None


def test_preco_menu_negativo_do_form_e_recusado(app, admin_user):
    """`min="0"` do HTML não vale nada num POST forjado; preço negativo
    derrubaria o total do menu."""
    from app.extensions import db
    from app.models import ProdutoItem
    with app.app_context():
        menu, _ = _menu(db, total=15)
        mid = menu.id
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(admin_user.id)
        s['_fresh'] = True
    r = c.post(f'/produtos/{mid}/salvar', data={
        'nome': 'Menu Degustação dos Minis',
        'item_tipo[]': ['receita'], 'item_nome[]': ['Mini 1'],
        'quantidade[]': ['5'], 'preco_menu[]': ['-9,90'],
    }, follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        pi = ProdutoItem.query.filter_by(produto_id=mid).first()
        assert pi.preco_menu is None


def test_preco_menu_com_lixo_no_form_nao_da_500(app, admin_user):
    from app.extensions import db
    from app.models import ProdutoItem
    with app.app_context():
        menu, _ = _menu(db, total=15)
        mid = menu.id
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(admin_user.id)
        s['_fresh'] = True
    r = c.post(f'/produtos/{mid}/salvar', data={
        'nome': 'Menu Degustação dos Minis',
        'item_tipo[]': ['receita'], 'item_nome[]': ['Mini 1'],
        'quantidade[]': ['5'], 'preco_menu[]': ['abc'],
    }, follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        pi = ProdutoItem.query.filter_by(produto_id=mid).first()
        assert pi.preco_menu is None


def test_chave_da_sessao_usa_o_helper_canonico(app):
    """A chave da linha vive em 3 lugares (JS, sessão, loja_menu). Este
    teste trava que a sessão usa o helper, não uma cópia."""
    from app.blueprints.loja import routes as loja_routes
    from app.services import loja_menu
    with app.test_request_context('/loja/'):
        norm = loja_routes._set_carrinho_sessao([
            {'kind': 'produto', 'id': 7, 'qtd': 1, 'comp': [[10, 5], [9, 5]]},
            {'kind': 'produto', 'id': 7, 'qtd': 2, 'comp': [[9, 5], [10, 5]]}])
        assert len(norm) == 1 and norm[0]['qtd'] == 3
        # ordenação NUMÉRICA (não lexicográfica): 9 antes de 10
        assert loja_menu.chave({10: 5, 9: 5}) == '9:5,10:5'
