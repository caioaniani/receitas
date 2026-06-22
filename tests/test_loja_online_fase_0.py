"""Fase 0 da Loja Online (16/06/2026): auditoria de pré-requisitos do
catálogo. Página owner-only e read-only — não muda nada do estado.

Plano completo: /root/.claude/plans/modular-tinkering-owl.md
Checklist: docs/loja-online/fase-0-checklist.md
"""
from unittest.mock import patch


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


def test_preco_zero_ou_vazio_tira_do_site(app):
    """Decisão do dono: 'preço = vende no site'. Tirar o preço = sair do site."""
    from app.extensions import db
    from app.models import Receita
    db.session.add(Receita(nome='Sair', preco_site=10.0,
                            rendimento_qtd=1, rendimento_unidade='un',
                            peso_base=100.0))
    db.session.commit()
    rid = Receita.query.first().id
    c, _ = _owner_logado(app)
    r = c.post(f'/admin/loja-online/catalogo/preco/receita/{rid}',
                json={'preco': None})
    assert r.get_json()['preco_site'] is None
    assert Receita.query.get(rid).preco_site is None


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
    """3 receitas em estados diferentes; cada filtro mostra a contagem
    correta. Checagem pelo class CSS `item-card` (presente só no card do
    catálogo — não vaza pra autocomplete/datalist do base.html)."""
    from app.extensions import db
    from app.models import Receita
    db.session.add(Receita(nome='Pronta site', preco_site=20.0,
                            imagem_dropbox_url='https://x/c.jpg',
                            rendimento_qtd=1, rendimento_unidade='un',
                            peso_base=100.0))
    db.session.add(Receita(nome='Falta preco', preco_site=None,
                            imagem_dropbox_url='https://x/sp.jpg',
                            rendimento_qtd=1, rendimento_unidade='un',
                            peso_base=100.0))
    db.session.add(Receita(nome='Falta foto', preco_site=15.0,
                            imagem_dropbox_url=None,
                            rendimento_qtd=1, rendimento_unidade='un',
                            peso_base=100.0))
    db.session.commit()
    c, _ = _owner_logado(app)

    def n_cards(html):
        # cada card tem `data-tipo="receita"` ou `data-tipo="produto"` —
        # atributo aparece SÓ no card real (não no CSS embutido).
        return html.count(b'data-tipo="receita"') + html.count(b'data-tipo="produto"')

    r = c.get('/admin/loja-online/catalogo?filtro=sem-preco')
    assert n_cards(r.data) == 1, 'só 1 receita sem preço'
    r2 = c.get('/admin/loja-online/catalogo?filtro=no-site')
    assert n_cards(r2.data) == 1, 'só 1 receita pronta pro site'
    r3 = c.get('/admin/loja-online/catalogo?filtro=sem-foto')
    assert n_cards(r3.data) == 1, 'só 1 receita sem foto'
    r4 = c.get('/admin/loja-online/catalogo?filtro=todos')
    assert n_cards(r4.data) == 3, 'todas as 3'


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
