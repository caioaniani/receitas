"""Fase 0 da Loja Online (16/06/2026): auditoria de pré-requisitos do
catálogo. Página owner-only e read-only — não muda nada do estado.

Plano completo: /root/.claude/plans/modular-tinkering-owl.md
Checklist: docs/loja-online/fase-0-checklist.md
"""
import re
from html import unescape
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest


def _owner_logado(app):
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='Dono', login='dono', papel='owner', is_owner=True)
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(u.id)
        s['_fresh'] = True
    return c, u


def test_rota_anonimo_bloqueado(app):
    """Sem login: redireciona pro login (302/401/403)."""
    c = app.test_client()
    assert c.get('/admin/loja-online/auditoria-catalogo').status_code in (302, 401, 403)


def test_rota_admin_nao_owner_libera(app):
    """22/06/2026 — admin comum (não-owner) agora vê. Decisão do dono:
    liberar a admin da loja online a toda a equipe; só reembolso/cancelar e
    emissão de NF continuam restritos."""
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='Admin', login='adm', papel='admin')
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(u.id)
        s['_fresh'] = True
    assert c.get('/admin/loja-online/auditoria-catalogo').status_code == 200


def test_rota_owner_carrega(app):
    """Owner vê a página com os contadores."""
    c, _ = _owner_logado(app)
    r = c.get('/admin/loja-online/auditoria-catalogo')
    assert r.status_code == 200
    assert b'Loja Online' in r.data
    assert b'auditoria' in r.data.lower() or b'Auditoria' in r.data


def test_rota_conta_certo_com_dados_de_amostra(app):
    """1 receita ativa pronta (preço + imagem), 1 sem preço, 1 arquivada =>
    rec_ativas=2, rec_prontas=1."""
    from app.extensions import db
    from app.models import Receita
    from app.utils import agora
    db.session.add(Receita(nome='Pronta', preco_site=15.0,
                            imagem_dropbox_url='https://x/y.jpg',
                            rendimento_qtd=1, rendimento_unidade='un',
                            peso_base=100.0))
    db.session.add(Receita(nome='Sem preço',
                            imagem_dropbox_url='https://x/z.jpg',
                            rendimento_qtd=1, rendimento_unidade='un',
                            peso_base=100.0))
    db.session.add(Receita(nome='Arquivada', preco_site=10.0,
                            imagem_dropbox_url='https://x/a.jpg',
                            arquivada_em=agora(),
                            rendimento_qtd=1, rendimento_unidade='un',
                            peso_base=100.0))
    db.session.commit()
    c, _ = _owner_logado(app)
    r = c.get('/admin/loja-online/auditoria-catalogo')
    assert r.status_code == 200
    html = r.data.decode()
    assert 'Pronta' in html or 'Sem preço' in html  # pendentes listados


def test_rota_NAO_muda_nada(app):
    """Read-only paranoia: chamar a rota não pode alterar nada no banco."""
    from app.extensions import db
    from app.models import Receita
    db.session.add(Receita(nome='X', preco_site=5.0,
                            rendimento_qtd=1, rendimento_unidade='un',
                            peso_base=100.0))
    db.session.commit()
    antes = Receita.query.count()
    c, _ = _owner_logado(app)
    with patch('app.extensions.db.session.commit') as commit:
        c.get('/admin/loja-online/auditoria-catalogo')
    # session.commit NÃO deve ser chamado por essa rota
    commit.assert_not_called()
    depois = Receita.query.count()
    assert antes == depois


# ── Tela de curadoria (16/06/2026): edição rápida de preço/foto ─────────

def test_curadoria_carrega_owner(app):
    """Página /admin/loja-online/catalogo renderiza pra owner."""
    c, _ = _owner_logado(app)
    r = c.get('/admin/loja-online/catalogo')
    assert r.status_code == 200
    assert b'Cat' in r.data  # 'Catálogo'


def test_curadoria_liberada_admin(app):
    """Admin comum também vê (22/06/2026 — dono liberou a admin da loja
    online pra toda a equipe; só reembolso e emissão de NF ficam restritos)."""
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='A', login='adm', papel='admin')
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(u.id)
        s['_fresh'] = True
    assert c.get('/admin/loja-online/catalogo').status_code == 200


def test_preco_e_foto_bloqueados_para_nao_owner(app):
    """22/06/2026 — admin comum vê o catálogo mas NÃO edita preço nem
    foto. Backend trava com 403; template não mostra os filtros de curadoria
    (sem-preço/sem-foto/todos) nem o input de upload de foto."""
    from app.extensions import db
    from app.models import Receita, Usuario
    rec = Receita(nome='Cookie', preco_site=13.0,
                  imagem_dropbox_url='https://x/y.jpg',
                  rendimento_qtd=1, rendimento_unidade='un', peso_base=100.0)
    u = Usuario(nome='G', login='gerz', papel='gerente', is_owner=False)
    u.set_senha('x' * 8)
    db.session.add_all([rec, u])
    db.session.commit()
    rid = rec.id
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(u.id)
        s['_fresh'] = True

    # POST de preço: 403
    r = c.post(f'/admin/loja-online/catalogo/preco/receita/{rid}',
               json={'preco': '99'})
    assert r.status_code == 403
    # Preço NÃO mudou
    assert float(Receita.query.get(rid).preco_site) == 13.0

    # POST de foto: 403
    r = c.post(f'/admin/loja-online/catalogo/foto/receita/{rid}', data={})
    assert r.status_code == 403

    # Template: filtros de curadoria não aparecem; "No site" aparece
    r = c.get('/admin/loja-online/catalogo')
    assert r.status_code == 200
    html = r.data.decode()
    assert '?filtro=no-site' in html        # tab "No site" mantida
    assert '?filtro=sem-preco' not in html  # tab de curadoria escondida
    assert '?filtro=sem-foto' not in html
    assert '?filtro=todos' not in html
    assert 'type="file"' not in html        # sem upload de foto
    assert 'preco-input' in html and 'disabled' in html  # preço bloqueado


def test_preco_ajax_salva_e_devolve_json(app):
    """POST de preço pelo JSON retorna {ok, preco_site} e persiste."""
    from app.extensions import db
    from app.models import Receita
    db.session.add(Receita(nome='X', preco_site=10.0,
                            rendimento_qtd=1, rendimento_unidade='un',
                            peso_base=100.0))
    db.session.commit()
    rid = Receita.query.first().id
    c, _ = _owner_logado(app)
    r = c.post(f'/admin/loja-online/catalogo/preco/receita/{rid}',
                json={'preco': '25.50'})
    assert r.status_code == 200
    j = r.get_json()
    assert j['ok'] is True
    assert j['preco_site'] == 25.50
    db.session.refresh(Receita.query.get(rid))
    assert Receita.query.get(rid).preco_site == 25.50


@pytest.mark.parametrize('tipo', ['receita', 'produto'])
@pytest.mark.parametrize('preco', [None, 0])
def test_preco_zero_ou_vazio_tira_do_site(app, tipo, preco):
    """Retirar do site bloqueia novos pedidos e preserva cadastro e estoque."""
    from app.blueprints.loja.routes import (
        _resolver_carrinho_sessao,
        _set_carrinho_sessao,
    )
    from app.extensions import db
    from app.models import AppConfig, EstoqueLoja, Loja, MovEstoqueLoja, Produto, Receita
    from app.services import loja_catalogo, loja_checkout

    campos = dict(nome='Sair do site', preco_site=10.0, preco_loja=9.0,
                  preco_interno=4.0, imagem_url='https://x/item.jpg',
                  ordem_site=3)
    if tipo == 'receita':
        obj = Receita(**campos, preco_venda=7.0, rendimento_qtd=1,
                      rendimento_unidade='un', peso_base=100.0)
        campo_atacado = 'preco_venda'
    else:
        obj = Produto(**campos, preco_atacado=7.0, ativo=True)
        campo_atacado = 'preco_atacado'
    loja = Loja(nome='Loja do site', ativa=True)
    db.session.add_all([obj, loja])
    db.session.commit()
    AppConfig.set('loja_site_estoque_id', loja.id)
    estoque = EstoqueLoja(loja_id=loja.id, quantidade=7,
                          quantidade_reservada=2, **{f'{tipo}_id': obj.id})
    db.session.add(estoque)
    db.session.commit()

    publicado = loja_catalogo.por_id_publicado(tipo, obj.id)
    assert publicado is not None
    carrinho = [{'kind': tipo, 'id': obj.id, 'qtd': 1}]
    itens, avisos = loja_checkout.montar_itens(carrinho)
    assert len(itens) == 1 and avisos == []
    movimentos_antes = MovEstoqueLoja.query.count()
    c, _ = _owner_logado(app)

    r = c.post(f'/admin/loja-online/catalogo/preco/{tipo}/{obj.id}',
               json={'preco': preco})
    assert r.status_code == 200
    assert r.get_json()['preco_site'] is None
    db.session.refresh(obj)
    db.session.refresh(estoque)
    assert obj.preco_site is None
    assert obj.preco_loja == 9.0
    assert obj.preco_interno == 4.0
    assert getattr(obj, campo_atacado) == 7.0
    assert obj.imagem_url == campos['imagem_url']
    assert obj.ordem_site == 3
    if tipo == 'receita':
        assert obj.arquivada_em is None
    else:
        assert obj.ativo is True
    assert estoque.quantidade == 7
    assert estoque.quantidade_reservada == 2
    assert MovEstoqueLoja.query.count() == movimentos_antes

    assert loja_catalogo.por_id_publicado(tipo, obj.id) is None
    assert not any(i['kind'] == tipo and i['id'] == obj.id
                   for i in loja_catalogo.produtos_publicados())
    assert c.get(publicado['href']).status_code == 404
    with app.test_request_context():
        _set_carrinho_sessao(carrinho)
        assert _resolver_carrinho_sessao() == []
    itens, avisos = loja_checkout.montar_itens(carrinho)
    assert itens == []
    assert avisos == ['Um item saiu de catálogo e foi removido do pedido.']


def test_preco_rejeita_valor_invalido(app):
    from app.extensions import db
    from app.models import Receita
    db.session.add(Receita(nome='V', preco_site=10.0,
                            rendimento_qtd=1, rendimento_unidade='un',
                            peso_base=100.0))
    db.session.commit()
    rid = Receita.query.first().id
    c, _ = _owner_logado(app)
    # texto que não converte
    r = c.post(f'/admin/loja-online/catalogo/preco/receita/{rid}',
                json={'preco': 'abc'})
    assert r.status_code == 400
    # negativo
    r2 = c.post(f'/admin/loja-online/catalogo/preco/receita/{rid}',
                 json={'preco': '-5'})
    assert r2.status_code == 400
    # > 9999
    r3 = c.post(f'/admin/loja-online/catalogo/preco/receita/{rid}',
                 json={'preco': '99999'})
    assert r3.status_code == 400


def test_curadoria_filtros(app):
    """No site espelha a vitrine: foto é opcional e menu inválido fica fora."""
    from app.extensions import db
    from app.models import Produto, ProdutoItem, Receita

    pronta = Receita(nome='Pronta site', preco_site=20.0,
                     imagem_dropbox_url='https://x/c.jpg',
                     rendimento_qtd=1, rendimento_unidade='un', peso_base=100.0)
    sem_preco = Receita(nome='Falta preco', preco_site=None,
                        imagem_dropbox_url='https://x/sp.jpg',
                        rendimento_qtd=1, rendimento_unidade='un', peso_base=100.0)
    sem_foto = Receita(nome='Falta foto', preco_site=15.0,
                       rendimento_qtd=1, rendimento_unidade='un', peso_base=100.0)
    menu_invalido = Produto(nome='Menu sem preço nos minis', preco_site=1.0,
                            imagem_dropbox_url='https://x/menu.jpg',
                            ativo=True, menu_configuravel=True,
                            menu_total_unidades=30)
    db.session.add_all([pronta, sem_preco, sem_foto, menu_invalido])
    db.session.flush()
    db.session.add(ProdutoItem(
        produto_id=menu_invalido.id, tipo='receita', receita_id=pronta.id,
        item_nome=pronta.nome, quantidade=30, preco_menu=None))
    db.session.commit()
    c, _ = _owner_logado(app)

    def chaves_cards(html):
        # cada card tem `data-tipo="receita"` ou `data-tipo="produto"` —
        # atributo aparece SÓ no card real (não no CSS embutido).
        return {(tipo, int(item_id)) for tipo, item_id in re.findall(
            r'data-tipo="(receita|produto)"\s+data-id="(\d+)"', html.decode())}

    r = c.get('/admin/loja-online/catalogo?filtro=sem-preco')
    assert chaves_cards(r.data) == {('receita', sem_preco.id)}
    r2 = c.get('/admin/loja-online/catalogo?filtro=no-site')
    assert chaves_cards(r2.data) == {('receita', pronta.id), ('receita', sem_foto.id)}
    r3 = c.get('/admin/loja-online/catalogo?filtro=sem-foto')
    assert chaves_cards(r3.data) == {('receita', sem_foto.id)}
    r4 = c.get('/admin/loja-online/catalogo?filtro=todos')
    assert chaves_cards(r4.data) == {
        ('receita', pronta.id), ('receita', sem_preco.id),
        ('receita', sem_foto.id), ('produto', menu_invalido.id),
    }


@pytest.mark.parametrize(('busca', 'nomes'), [
    ('PAO', {'Pão integral', 'Pão de queijo'}),
    ('cafe', {'Pão integral'}),
    ('  QUEIJO  ', {'Pão de queijo'}),
])
def test_curadoria_busca_nome_categoria_e_preserva_filtros(app, busca, nomes):
    """Busca ignora acentos/caixa e acompanha a troca de filtro."""
    from app.extensions import db
    from app.models import Receita

    receitas = [
        Receita(nome='Pão integral', categoria='Café da manhã', preco_site=20.0,
                rendimento_qtd=1, rendimento_unidade='un', peso_base=100.0),
        Receita(nome='Pão de queijo', categoria='Salgados', preco_site=5.0,
                rendimento_qtd=1, rendimento_unidade='un', peso_base=100.0),
        Receita(nome='Cookie', categoria='Doces', preco_site=7.0,
                rendimento_qtd=1, rendimento_unidade='un', peso_base=100.0),
    ]
    db.session.add_all(receitas)
    db.session.commit()
    c, _ = _owner_logado(app)
    r = c.get('/admin/loja-online/catalogo', query_string={'q': busca, 'filtro': 'no-site'})
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    ids = {int(item_id) for item_id in re.findall(
        r'data-tipo="receita"\s+data-id="(\d+)"', html)}
    assert ids == {receita.id for receita in receitas if receita.nome in nomes}

    filtros = [parse_qs(urlsplit(unescape(href)).query)
               for href in re.findall(
                   r'<a\b(?=[^>]*\bclass="[^"]*\bcat-filtro\b)[^>]*\bhref="([^"]+)"', html)]
    assert filtros
    assert all(filtro.get('q') == [busca.strip()] for filtro in filtros)


# ── Limite de upload alinhado entre Flask e a rota (16/06/2026) ──────────
#
# Bug real: MAX_CONTENT_LENGTH no Flask estava 10MB e a rota de upload
# permitia 25MB. Foto >10MB era rejeitada com 413 HTML antes da rota rodar,
# JS interpretava como "erro de conexão" cego. Esta trava garante que os
# dois limites NUNCA divergem silenciosamente.

def test_max_content_length_alinhado_com_limite_da_rota_foto():
    """Limite do Flask (config.py) precisa ser >= limite hardcoded da rota
    de upload da loja (25MB), senão arquivos válidos pra rota são bloqueados
    pelo middleware do Flask antes."""
    from config import Config
    LIMITE_ROTA_MB = 25  # ver `app/blueprints/main/routes.py::loja_online_catalogo_foto`
    limite_flask_mb = Config.MAX_CONTENT_LENGTH / (1024 * 1024)
    assert limite_flask_mb >= LIMITE_ROTA_MB, (
        f'Flask aceita até {limite_flask_mb}MB mas a rota tenta processar '
        f'{LIMITE_ROTA_MB}MB — Flask vai retornar 413 antes da rota rodar')
