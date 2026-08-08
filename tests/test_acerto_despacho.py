"""Acerto de despacho direto da indústria (08/08/2026, Dia dos Pais).

Pedido do site baixa a LOJA no pagamento e nunca debita a indústria; quando
a mercadoria sai DIRETO da indústria, o acerto (owner, dry-run por default)
estorna a loja por código de pedido e debita a indústria pela composição
despachada. Idempotente por pedido.
"""
from datetime import date, timedelta

from app.extensions import db
from app.models import (
    AppConfig,
    EstoqueLoja,
    EstoqueProducao,
    MateriaPrima,
    MovEstoqueLoja,
    MovEstoqueProducao,
    PedidoOnline,
    PedidoOnlineItem,
    Produto,
    ProdutoItem,
    Receita,
)
from app.services import acerto_despacho as svc
from app.services.baixa_venda import aplicar_venda
from app.utils import hoje

_DIA = date(2026, 8, 9)


def _setup(codigo='ACERT1', qtd_cestas=2, status='pago', baixar=True,
           dia=_DIA):
    """Loja origem + cesta (2 croissants + 100 g de mussarela por cesta) +
    pedido pago com a baixa REAL do motor único (como o webhook faz)."""
    from app.models import Loja
    loja = Loja(nome='Loja Anesio Pinto Rosa', ativa=True)
    db.session.add(loja)
    db.session.flush()
    AppConfig.set('loja_site_estoque_id', loja.id)

    rec = Receita(nome='Croissant Tradicional', categoria='Croissants',
                  rendimento_qtd=1, rendimento_unidade='un', peso_base=100)
    mp = MateriaPrima(nome='Mussarela', unidade='g', custo_por_kg=40.0,
                      estoque_atual=5000.0)
    db.session.add_all([rec, mp])
    db.session.flush()
    cesta = Produto(nome='Cesta Dia dos Pais', ativo=True)
    db.session.add(cesta)
    db.session.flush()
    db.session.add_all([
        ProdutoItem(produto_id=cesta.id, tipo='receita', receita_id=rec.id,
                    item_nome=rec.nome, quantidade=2),
        ProdutoItem(produto_id=cesta.id, tipo='mp', materia_prima_id=mp.id,
                    item_nome=mp.nome, quantidade=100),
    ])
    db.session.add_all([
        EstoqueLoja(loja_id=loja.id, receita_id=rec.id, quantidade=50),
        EstoqueProducao(receita_id=rec.id, quantidade=100),
    ])
    p = PedidoOnline(codigo=codigo, nome_cliente='C', email_cliente='a@x.com',
                     status=status, modo_entrega='agendada',
                     data_entrega=dia, janela_entrega='06:00–10:00',
                     valor_total=430)
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoOnlineItem(pedido_id=p.id, kind='produto',
                                    produto_id=cesta.id, nome=cesta.nome,
                                    preco_unitario=215,
                                    quantidade=qtd_cestas,
                                    subtotal=215 * qtd_cestas))
    if baixar:
        # A baixa que o webhook de pagamento fez (motor único, canal site;
        # MP sem linha de EstoqueLoja é pulada — pular_sem_linha).
        aplicar_venda(loja.id, produto_id=cesta.id, qtd=qtd_cestas,
                      canal='site', referencia=f'Site #{codigo}',
                      pedido_ref=f'site:{codigo}', nome_venda=cesta.nome,
                      pular_sem_linha=True)
    db.session.commit()
    return loja, rec, mp, cesta, p


def _saldo_loja(loja, rec):
    el = EstoqueLoja.query.filter_by(loja_id=loja.id,
                                     receita_id=rec.id).first()
    return int(el.quantidade or 0) if el else 0


def _saldo_industria(rec):
    ep = EstoqueProducao.query.filter_by(receita_id=rec.id).first()
    return int(ep.quantidade or 0) if ep else 0


# ── dry-run ─────────────────────────────────────────────────────────

def test_dry_run_monta_plano_sem_escrever(app):
    with app.app_context():
        loja, rec, mp, cesta, p = _setup()
        assert _saldo_loja(loja, rec) == 46          # 50 - 2x2
        plano = svc.acertar(_DIA, executar=False)
        assert plano['executado'] is False
        assert plano['pedidos_a_acertar'] == ['ACERT1']
        assert plano['credito_por_loja'] == {
            'Loja Anesio Pinto Rosa': {'Croissant Tradicional': 4}}
        assert plano['credito_loja_total_un'] == 4
        deb = {d['nome']: d for d in plano['debito_industria']}
        assert deb['Croissant Tradicional']['qtd'] == 4
        assert deb['Mussarela']['qtd'] == 200.0      # 2 cestas x 100 g
        # NADA mudou:
        assert _saldo_loja(loja, rec) == 46
        assert _saldo_industria(rec) == 100
        assert float(mp.estoque_atual) == 5000.0
        assert AppConfig.get(f'acerto_despacho_{_DIA.isoformat()}') is None


# ── executar ────────────────────────────────────────────────────────

def test_executar_credita_loja_e_debita_industria(app):
    with app.app_context():
        loja, rec, mp, cesta, p = _setup()
        plano = svc.acertar(_DIA, executar=True)
        assert plano['executado'] is True
        assert _saldo_loja(loja, rec) == 50           # devolvido
        assert _saldo_industria(rec) == 96            # 100 - 4
        assert float(mp.estoque_atual) == 4800.0      # 5000 - 200
        # Movimentos rastreáveis:
        assert (MovEstoqueLoja.query
                .filter(MovEstoqueLoja.tipo == 'venda_site_estorno').count()) > 0
        mv = (MovEstoqueProducao.query
              .filter_by(tipo='saida_site_direto').first())
        assert mv is not None and 'Acerto despacho' in mv.referencia


def test_idempotente_segunda_rodada_nao_mexe(app):
    with app.app_context():
        loja, rec, mp, cesta, p = _setup()
        svc.acertar(_DIA, executar=True)
        plano2 = svc.acertar(_DIA, executar=True)
        assert plano2['pedidos_a_acertar'] == []
        assert plano2['ja_acertados'] == ['ACERT1']
        assert _saldo_loja(loja, rec) == 50           # não creditou 2x
        assert _saldo_industria(rec) == 96            # não debitou 2x
        assert float(mp.estoque_atual) == 4800.0


def test_cancelado_e_aguardando_ficam_fora(app):
    with app.app_context():
        loja, rec, mp, cesta, p = _setup(codigo='CANC1', status='cancelado')
        plano = svc.acertar(_DIA, executar=False)
        assert plano['pedidos_a_acertar'] == []
        p.status = 'aguardando_pagamento'
        db.session.commit()
        assert svc.acertar(_DIA)['pedidos_a_acertar'] == []


def test_sob_encomenda_entra_no_debito_mas_nao_no_credito(app):
    """Item sob encomenda nunca baixou a loja (nada a devolver), mas saiu
    fisicamente da indústria (debita)."""
    with app.app_context():
        from app.models import Loja
        loja = Loja(nome='Loja Anesio Pinto Rosa', ativa=True)
        db.session.add(loja)
        db.session.flush()
        AppConfig.set('loja_site_estoque_id', loja.id)
        rec = Receita(nome='Mini Pain', categoria='Minis', rendimento_qtd=1,
                      rendimento_unidade='un', peso_base=50,
                      sob_encomenda=True)
        db.session.add(rec)
        db.session.flush()
        db.session.add(EstoqueProducao(receita_id=rec.id, quantidade=30))
        p = PedidoOnline(codigo='SOB1', nome_cliente='C',
                         email_cliente='s@x.com', status='pago',
                         modo_entrega='agendada', data_entrega=_DIA,
                         janela_entrega='06:00–10:00', valor_total=80)
        db.session.add(p)
        db.session.flush()
        db.session.add(PedidoOnlineItem(pedido_id=p.id, kind='receita',
                                        receita_id=rec.id, nome=rec.nome,
                                        preco_unitario=8, quantidade=10,
                                        subtotal=80))
        db.session.commit()          # sob encomenda: NENHUMA baixa de loja
        plano = svc.acertar(_DIA, executar=True)
        assert plano['credito_por_loja'] == {}
        assert _saldo_industria(rec) == 20            # 30 - 10


def test_falta_na_industria_nunca_negativa(app):
    with app.app_context():
        loja, rec, mp, cesta, p = _setup(qtd_cestas=2)
        ep = EstoqueProducao.query.filter_by(receita_id=rec.id).first()
        ep.quantidade = 3                             # débito seria 4
        db.session.commit()
        plano = svc.acertar(_DIA, executar=True)
        assert _saldo_industria(rec) == 0             # baixou até zero
        assert any('faltaram' in a for a in plano['avisos'])


# ── rota ────────────────────────────────────────────────────────────

def _owner_client(app, owner_user):
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(owner_user.id)
        s['_fresh'] = True
    return c


def test_rota_dry_run_ok_e_executar_trava_antes_do_dia(app, owner_user):
    with app.app_context():
        amanha = hoje() + timedelta(days=1)
        _setup(dia=amanha)
        c = _owner_client(app, owner_user)
        r = c.get(f'/admin/acerto-despacho?data={amanha.isoformat()}')
        assert r.status_code == 200 and r.get_json()['executado'] is False
        # Executar ANTES do dia do despacho = recusado (mercadoria nem saiu).
        r2 = c.get(f'/admin/acerto-despacho?data={amanha.isoformat()}'
                   '&executar=1')
        assert r2.status_code == 400
        assert 'DEPOIS' in r2.get_json()['erro']


def test_rota_executa_dia_passado(app, owner_user):
    with app.app_context():
        ontem = hoje() - timedelta(days=1)
        loja, rec, mp, cesta, p = _setup(dia=ontem)
        c = _owner_client(app, owner_user)
        r = c.get(f'/admin/acerto-despacho?data={ontem.isoformat()}'
                  '&executar=1')
        assert r.status_code == 200 and r.get_json()['executado'] is True
        assert _saldo_loja(loja, rec) == 50


def test_rota_sem_data_400_e_admin_comum_403(app, admin_user):
    with app.app_context():
        c = app.test_client()
        with c.session_transaction() as s:
            s['_user_id'] = str(admin_user.id)
            s['_fresh'] = True
        assert c.get('/admin/acerto-despacho?data=2026-08-09').status_code == 403


def test_rota_owner_sem_data_400(app, owner_user):
    with app.app_context():
        c = _owner_client(app, owner_user)
        assert c.get('/admin/acerto-despacho').status_code == 400
