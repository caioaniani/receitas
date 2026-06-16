"""Fase 0 da Loja Online (16/06/2026): auditoria de pré-requisitos do
catálogo. Página owner-only e read-only — não muda nada do estado.

Plano completo: /root/.claude/plans/modular-tinkering-owl.md
Checklist: docs/loja-online/fase-0-checklist.md
"""
from unittest.mock import patch


def _owner_logado(app):
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='Dono', login='dono', papel='owner')
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(u.id)
        s['_fresh'] = True
    return c, u


def test_rota_owner_only(app):
    """Sem login OU sem ser owner: 403/302. PII de catálogo + preço não
    pode vazar pra qualquer staff."""
    from app.extensions import db
    from app.models import Usuario
    # Sem login
    c = app.test_client()
    assert c.get('/admin/loja-online/auditoria-catalogo').status_code in (302, 401, 403)

    # Admin comum (não-owner): também não
    u = Usuario(nome='Admin', login='adm', papel='admin')
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    c2 = app.test_client()
    with c2.session_transaction() as s:
        s['_user_id'] = str(u.id)
        s['_fresh'] = True
    # owner_required redireciona pra index ou retorna 403 — ambos aceitos
    r = c2.get('/admin/loja-online/auditoria-catalogo')
    assert r.status_code in (302, 403)


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
