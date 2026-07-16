"""Opção "fatiado?" nos sourdoughs (16/07/2026).

O cliente escolhe se o pão sourdough vem fatiado, por item. Só preferência
de corte — NÃO mexe em preço nem estoque. Fatiado e inteiro do mesmo pão são
linhas separadas no carrinho/pedido. Aparece no painel de entregas
(cozinha/impressão) e no detalhe admin do pedido.
"""
from datetime import datetime

from app.extensions import db
from app.models import (
    AppConfig,
    EstoqueLoja,
    Loja,
    PedidoOnlineItem,
    Receita,
)


def _sourdough(nome='Sourdough Tradicional', familia='pao_sourdough',
               preco=32.0):
    r = Receita(nome=nome, categoria='Pães', familia=familia,
                rendimento_qtd=1, rendimento_unidade='un', peso_base=1000.0,
                preco_site=preco, imagem_dropbox_url='https://x/s.jpg')
    db.session.add(r)
    db.session.commit()
    return r


def _loja_site():
    """Loja do site que estoca TODAS as receitas já criadas."""
    loja = Loja(nome='Brooklin', endereco='Ribeiro do Vale, 455', ativa=True)
    db.session.add(loja)
    db.session.commit()
    AppConfig.set('loja_site_estoque_id', loja.id)
    for r in Receita.query.all():
        db.session.add(EstoqueLoja(loja_id=loja.id, receita_id=r.id,
                                   quantidade=999))
    db.session.commit()
    return loja


# ── receita_fatiavel: só sourdough (família OU nome), nunca granola ────────

def test_fatiavel_por_familia(app):
    from app.services.loja_catalogo import receita_fatiavel
    r = _sourdough('Sourdough Nozes e Azeitonas', familia='pao_sourdough')
    assert receita_fatiavel(r) is True


def test_pao_frances_nao_fatiavel_mesmo_sendo_familia_sourdough(app):
    """Pão Francês Fermentado é família sourdough mas é PÃOZINHO — não se
    fatia (dono, 16/07/2026). Baguete idem."""
    from app.services.loja_catalogo import receita_fatiavel
    pf = _sourdough('Pão Francês Fermentado', familia='pao_sourdough')
    bg = _sourdough('Baguette Francesa', familia='pao_sourdough')
    assert receita_fatiavel(pf) is False
    assert receita_fatiavel(bg) is False


def test_fatiavel_por_nome_sem_familia(app):
    """Mini Sourdough tem família NULL no cadastro — o nome resgata."""
    from app.services.loja_catalogo import receita_fatiavel
    r = _sourdough('Mini Sourdough Tradicional', familia=None)
    assert receita_fatiavel(r) is True


def test_nao_fatiavel_granola_familia_nula(app):
    """Granola/iogurte são Receita com família NULL — NÃO podem virar
    fatiáveis (o default NULL→sourdough pegaria elas; por isso não usamos)."""
    from app.services.loja_catalogo import receita_fatiavel
    r = _sourdough('Produção - Granola Artesanal', familia=None)
    assert receita_fatiavel(r) is False


def test_serializer_expoe_fatiavel(app):
    from app.services.loja_catalogo import _serializar_receita
    sd = _sourdough()
    gr = _sourdough('Granola', familia=None)
    assert _serializar_receita(sd)['fatiavel'] is True
    assert _serializar_receita(gr)['fatiavel'] is False


# ── montar_itens: sanitiza no servidor (não confia no navegador) ───────────

def test_montar_itens_aceita_fatiado_em_sourdough(app):
    from app.services.loja_checkout import montar_itens
    sd = _sourdough()
    _loja_site()
    itens, avisos = montar_itens([{'kind': 'receita', 'id': sd.id, 'qtd': 2,
                                   'fatiado': True}])
    assert avisos == []
    assert itens[0]['fatiado'] is True


def test_montar_itens_ignora_fatiado_forjado_em_nao_sourdough(app):
    """POST forjado com fatiado=true num item que NÃO é sourdough é
    descartado no servidor."""
    from app.services.loja_checkout import montar_itens
    gr = _sourdough('Granola', familia=None)
    _loja_site()
    itens, _ = montar_itens([{'kind': 'receita', 'id': gr.id, 'qtd': 1,
                              'fatiado': True}])
    assert itens[0]['fatiado'] is False


# ── criar_pedido persiste o fatiado no PedidoOnlineItem ────────────────────

def _form_retirada(loja, base):
    from app.services import loja_checkout
    data = loja_checkout.datas_disponiveis('retirada', base=base)[1].isoformat()
    return {'nome': 'Maria Silva', 'email': 'm@x.com',
            'cpf': '529.982.247-25', 'aceite_lgpd': '1',
            'modo_entrega': 'retirada', 'loja_id': str(loja.id),
            'data_entrega': data, 'janela_entrega': '08:00–09:00'}


def test_criar_pedido_grava_fatiado(app):
    from app.services import loja_checkout
    sd = _sourdough()
    loja = _loja_site()
    base = datetime(2026, 6, 17, 10, 0)
    pedido, erros = loja_checkout.criar_pedido(
        _form_retirada(loja, base),
        [{'kind': 'receita', 'id': sd.id, 'qtd': 1, 'fatiado': True}],
        base=base)
    assert erros == []
    it = PedidoOnlineItem.query.filter_by(pedido_id=pedido.id).first()
    assert it.fatiado is True


def test_criar_pedido_sem_fatiado_fica_none(app):
    from app.services import loja_checkout
    sd = _sourdough()
    loja = _loja_site()
    base = datetime(2026, 6, 17, 10, 0)
    pedido, erros = loja_checkout.criar_pedido(
        _form_retirada(loja, base),
        [{'kind': 'receita', 'id': sd.id, 'qtd': 1}], base=base)
    assert erros == []
    it = PedidoOnlineItem.query.filter_by(pedido_id=pedido.id).first()
    assert not it.fatiado          # None = inteiro


# ── Sessão do carrinho preserva fatiado e separa as linhas ─────────────────

def test_sessao_separa_fatiado_de_inteiro(app):
    """Fatiado e inteiro do MESMO sourdough são linhas distintas (não somam
    qtd) e a flag sobrevive à normalização da sessão."""
    from app.blueprints.loja.routes import _set_carrinho_sessao
    sd = _sourdough()
    with app.test_request_context():
        norm = _set_carrinho_sessao([
            {'kind': 'receita', 'id': sd.id, 'qtd': 2, 'fatiado': True},
            {'kind': 'receita', 'id': sd.id, 'qtd': 1, 'fatiado': False},
            {'kind': 'receita', 'id': sd.id, 'qtd': 3, 'fatiado': True},
        ])
    linhas = sorted(norm, key=lambda x: x['fatiado'])
    assert len(norm) == 2                       # fatiado + inteiro
    assert linhas[0]['fatiado'] is False and linhas[0]['qtd'] == 1
    assert linhas[1]['fatiado'] is True and linhas[1]['qtd'] == 5  # 2+3 somam


# ── Painel de entregas: o item leva fatiado pra cozinha ────────────────────

def _slug(nome):
    from app.services.loja_catalogo import _slugify
    return _slugify(nome)


def test_pagina_produto_sourdough_tem_checkbox(app, monkeypatch):
    """Página de um sourdough mostra o checkbox 'fatiado'; um não-sourdough
    (granola) não mostra."""
    monkeypatch.delenv('LOJA_VISIVEL', raising=False)
    sd = _sourdough('Sourdough Integral')
    gr = _sourdough('Granola Artesanal', familia=None)
    _loja_site()
    c = app.test_client()
    with c.session_transaction() as s:
        from app.models import Usuario
        u = Usuario(nome='A', login='a2', papel='admin')
        u.set_senha('x' * 8)
        db.session.add(u)
        db.session.commit()
        s['_user_id'] = str(u.id)
        s['_fresh'] = True
    html_sd = c.get(f'/loja/{_slug(sd.nome)}-r{sd.id}').get_data(as_text=True)
    html_gr = c.get(f'/loja/{_slug(gr.nome)}-r{gr.id}').get_data(as_text=True)
    assert 'quer-fatiado' in html_sd
    assert 'Quero <strong>fatiado</strong>' in html_sd
    assert 'quer-fatiado' not in html_gr


def test_painel_serializa_fatiado(app):
    from app.blueprints.entregas.routes import _serializar_pedido_online
    from app.services import loja_checkout
    sd = _sourdough()
    loja = _loja_site()
    base = datetime(2026, 6, 17, 10, 0)
    pedido, erros = loja_checkout.criar_pedido(
        _form_retirada(loja, base),
        [{'kind': 'receita', 'id': sd.id, 'qtd': 1, 'fatiado': True}],
        base=base)
    assert erros == []
    card = _serializar_pedido_online(pedido)
    assert card['itens'][0]['fatiado'] is True
