"""Produção do dia no /admin/loja-online (23/06/2026, decisão do dono).

A tela "o que preparar para o dia X de acordo com o que foi vendido pra
entregar pelo site" agora vive no painel da Loja Online (antes só existia
escondida em /pedidos/contagem-dia-site). Reusa contagem_para_dia.

E a tela que já existia (Plano do dia) foi renomeada pra "Disponibilidade
do dia" pra não confundir as duas.
"""
from datetime import timedelta
from decimal import Decimal


def _owner(app):
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='Dono', login='dono', papel='admin', is_owner=True)
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(u.id)
        s['_fresh'] = True
    return c


def _pedido_online_pago(db, receita, qtd, dia):
    from app.models import PedidoOnline, PedidoOnlineItem
    p = PedidoOnline(codigo=f'OC{receita.id}{qtd}', nome_cliente='C',
                     email_cliente='c@x.com', modo_entrega='agendada',
                     status='pago', subtotal=Decimal('10'),
                     valor_total=Decimal('10'), data_entrega=dia)
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoOnlineItem(
        pedido_id=p.id, kind='receita', receita_id=receita.id,
        nome=receita.nome, quantidade=qtd, preco_unitario=Decimal('10'),
        subtotal=Decimal('10')))
    db.session.commit()
    return p


def test_producao_do_dia_mostra_o_que_preparar(app):
    """A rota nova soma o que foi vendido no site pra o dia escolhido."""
    from app.extensions import db
    from app.models import Receita
    from app.utils import hoje

    r = Receita(nome='Sourdough', categoria='Paes', preco_site=25.0,
                rendimento_qtd=1, rendimento_unidade='un', peso_base=500.0)
    db.session.add(r)
    db.session.commit()
    dia = hoje() + timedelta(days=2)
    _pedido_online_pago(db, r, 7, dia)
    # pedido de OUTRO dia não entra na contagem do dia alvo
    _pedido_online_pago(db, r, 3, dia + timedelta(days=1))

    c = _owner(app)
    resp = c.get(f'/admin/loja-online/producao-do-dia?data={dia.isoformat()}')
    assert resp.status_code == 200
    html = resp.data.decode()
    assert 'Produção do dia' in html
    assert 'Sourdough' in html
    assert '7' in html  # qtd a preparar do dia alvo


def test_dashboard_linka_as_duas_telas(app):
    """O painel da Loja Online mostra os dois acessos separados."""
    c = _owner(app)
    resp = c.get('/admin/loja-online')
    assert resp.status_code == 200
    html = resp.data.decode()
    assert '/admin/loja-online/producao-do-dia' in html
    assert '/admin/loja-online/plano-do-dia' in html
    assert 'Produção do dia' in html
    assert 'Dias disponíveis dos produtos' in html


def test_layout_novo_mostra_disponibilidade_da_loja_online(app):
    """O dono encontra a disponibilidade por data sem voltar ao layout antigo."""
    c = _owner(app)

    area = c.get('/area/vendas')
    assert area.status_code == 200
    html = area.get_data(as_text=True)
    assert 'ui-v2-sidebar' in html
    assert 'Dias disponíveis dos produtos' in html
    assert 'Criar regras semanais' in html
    assert '/admin/loja-online/plano-do-dia' in html

    tela = c.get('/admin/loja-online/plano-do-dia')
    assert tela.status_code == 200
    html_tela = tela.get_data(as_text=True)
    assert 'Disponibilidade do site' in html_tela
    assert 'sidebar-link active' in html_tela


def test_disponibilidade_agora_prioriza_regras_semanais(app):
    """A tela deixa de expor a planilha diaria e explica o fluxo recorrente."""
    c = _owner(app)
    resp = c.get('/admin/loja-online/plano-do-dia')
    assert resp.status_code == 200
    html = resp.data.decode()
    assert 'Quando cada produto aparece' in html
    assert 'Regra semanal' in html
    assert 'Exceções por data' in html
    assert '99999 a TODOS' not in html
