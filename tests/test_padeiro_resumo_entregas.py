"""Card "entregas do site" na tela do padeiro (08/08/2026, pedido do dono).

A aba Produtos do /entregas (Vendidos no dia + A produzir) dentro do
/padeiro, pro time montar as entregas de evento (Dia das Mães/Pais) sem
sair da TV. Liga/desliga por AppConfig (`padeiro_resumo_entregas`) — a
feature fica dormente o resto do ano. Antes das 10h o alvo é HOJE (a
madrugada do evento monta as entregas do dia em voo); depois, AMANHÃ.
"""
from datetime import date, datetime, timedelta

from app.blueprints.padeiro.routes import _alvo_resumo
from app.extensions import db
from app.models import (
    AppConfig,
    PedidoOnline,
    PedidoOnlineItem,
    Produto,
    ProdutoItem,
    Receita,
    Usuario,
)
from app.utils import hoje


def _login(app, papel='admin'):
    u = Usuario(nome=papel, login=f'{papel}_resumo', papel=papel)
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(u.id)
        s['_fresh'] = True
    return c


def _pedido_amanha():
    r = Receita(nome='Croissant Tradicional', categoria='Croissants',
                rendimento_qtd=1, rendimento_unidade='un', peso_base=100)
    db.session.add(r)
    db.session.flush()
    cesta = Produto(nome='Cesta Evento', ativo=True)
    db.session.add(cesta)
    db.session.flush()
    db.session.add(ProdutoItem(produto_id=cesta.id, tipo='receita',
                               receita_id=r.id, item_nome=r.nome,
                               quantidade=3))
    p = PedidoOnline(codigo='RSM1', nome_cliente='C', email_cliente='r@x.com',
                     status='pago', modo_entrega='agendada',
                     data_entrega=hoje() + timedelta(days=1),
                     janela_entrega='06:00–10:00', valor_total=200)
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoOnlineItem(pedido_id=p.id, kind='produto',
                                    produto_id=cesta.id, nome=cesta.nome,
                                    preco_unitario=100, quantidade=2,
                                    subtotal=200))
    db.session.commit()


# ── regra do alvo ───────────────────────────────────────────────────

def test_alvo_de_madrugada_e_hoje_depois_e_amanha():
    d = date(2026, 8, 9)
    assert _alvo_resumo(datetime(2026, 8, 9, 2, 30)) == d          # madrugada
    assert _alvo_resumo(datetime(2026, 8, 9, 9, 59)) == d          # manhã
    assert _alvo_resumo(datetime(2026, 8, 8, 13, 0)) == date(2026, 8, 9)


# ── card na tela ────────────────────────────────────────────────────

def test_flag_desligada_sem_card_e_admin_ve_botao_de_ligar(app):
    with app.app_context():
        c = _login(app, 'admin')
        body = c.get('/padeiro/').get_data(as_text=True)
        assert 'ENTREGAS DO SITE' not in body
        assert 'Ligar resumo de entregas' in body


def test_flag_desligada_padeiro_nao_ve_botao(app):
    with app.app_context():
        c = _login(app, 'padeiro')
        body = c.get('/padeiro/').get_data(as_text=True)
        assert 'Ligar resumo de entregas' not in body


def test_flag_ligada_mostra_vendidos_e_a_produzir(app):
    with app.app_context():
        _pedido_amanha()
        AppConfig.set('padeiro_resumo_entregas', '1')
        db.session.commit()
        c = _login(app, 'padeiro')          # o PADEIRO vê o card (é pra ele)
        body = c.get('/padeiro/').get_data(as_text=True)
        assert 'ENTREGAS DO SITE' in body
        assert 'Cesta Evento' in body               # vendido, como vendido
        assert 'Croissant Tradicional' in body      # explodido: 2x3=6
        assert '>6 un' in body.replace('  ', ' ') or '6 un' in body


def test_card_nao_quebra_a_tv_quando_a_conta_falha(app, monkeypatch):
    """Best-effort: erro no motor da aba nunca derruba o /padeiro."""
    from app.blueprints.padeiro import routes as mod
    with app.app_context():
        AppConfig.set('padeiro_resumo_entregas', '1')
        db.session.commit()

        def _boom():
            raise RuntimeError('x')
        monkeypatch.setattr(mod, '_resumo_entregas', _boom, raising=True)
        # o route chama _resumo_entregas() direto — patch acima cobre;
        # a página tem que abrir mesmo assim pro papel padeiro.
        c = _login(app, 'padeiro')
        r = c.get('/padeiro/')
        assert r.status_code == 200


# ── toggle ──────────────────────────────────────────────────────────

def test_toggle_liga_e_desliga(app):
    with app.app_context():
        c = _login(app, 'admin')
        c.post('/padeiro/resumo-entregas/toggle')
        assert AppConfig.get('padeiro_resumo_entregas') == '1'
        c.post('/padeiro/resumo-entregas/toggle')
        assert AppConfig.get('padeiro_resumo_entregas') == '0'


def test_toggle_padeiro_nao_pode(app):
    with app.app_context():
        c = _login(app, 'padeiro')
        r = c.post('/padeiro/resumo-entregas/toggle')
        assert r.status_code in (302, 403)
        assert AppConfig.get('padeiro_resumo_entregas') != '1'
