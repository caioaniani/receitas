"""Radar de saude do negocio: contas a pagar + receitas (digest 07:30)."""
from datetime import timedelta
from unittest.mock import patch

from app.utils import hoje


def _semear_contas(db):
    from app.models import ContaPagar
    h = hoje()
    db.session.add_all([
        # vencida (5 dias atras), R$ 100
        ContaPagar(status='aberto', valor_total=100,
                   vencimento=h - timedelta(days=5)),
        # vencendo em 3 dias, R$ 50
        ContaPagar(status='aberto', valor_total=50,
                   vencimento=h + timedelta(days=3)),
        # extracao incompleta (IA nao leu valor nem vencimento)
        ContaPagar(status='aberto', tipo_documento='desconhecido'),
        # paga (nao conta em nada de aberto)
        ContaPagar(status='pago', valor_total=999,
                   vencimento=h - timedelta(days=2)),
        # vencendo LONGE (30d) — fora da janela de 7
        ContaPagar(status='aberto', valor_total=70,
                   vencimento=h + timedelta(days=30)),
    ])
    db.session.commit()


def test_resumo_contas(app):
    from app.extensions import db
    from app.services import saude_negocio
    _semear_contas(db)
    r = saude_negocio.resumo_contas()
    assert r['vencidas'] == 1
    assert r['vencidas_total'] == 100.0
    assert r['vencendo_7d'] == 1
    assert r['vencendo_7d_total'] == 50.0
    assert r['extracao_incompleta'] == 1
    assert r['abertas'] == 4          # tudo menos a paga
    assert r['novas_24h'] == 5        # todas criadas agora


def test_resumo_receitas_classifica(app):
    """Sem ingredientes → ficha incompleta; com ingrediente mas sem preco →
    sem_preco; preco baixo vs custo → margem critica."""
    from app.extensions import db
    from app.models import IngredienteReceita, MateriaPrima, Receita
    from app.services import saude_negocio

    sem_ficha = Receita(nome='Sem Ficha', categoria='Paes', rendimento_qtd=1,
                        rendimento_unidade='un', peso_base=100.0)
    mp = MateriaPrima(nome='Farinha', unidade='kg', custo_por_kg=10.0)
    com_tudo = Receita(nome='Croissant', categoria='Croissants',
                       rendimento_qtd=1, rendimento_unidade='un',
                       peso_base=100.0, preco_venda=10.0)
    sem_preco = Receita(nome='Sem Preco', categoria='Paes', rendimento_qtd=1,
                        rendimento_unidade='un', peso_base=100.0)
    db.session.add_all([sem_ficha, mp, com_tudo, sem_preco])
    db.session.flush()
    db.session.add_all([
        IngredienteReceita(receita_id=com_tudo.id, materia_prima_id=mp.id,
                           quantidade=100, unidade='g'),
        IngredienteReceita(receita_id=sem_preco.id, materia_prima_id=mp.id,
                           quantidade=50, unidade='g'),
    ])
    db.session.commit()

    # custo mockado: croissant custa 9 e vende 10 → margem 10% < 30% → critica
    fake = {'custos': {'Croissant': 9.0, 'Sem Preco': 1.0},
            'pesos': {}, 'circulares': []}
    with patch('app.services.custos.calcular_custos_receitas',
               return_value=fake):
        r = saude_negocio.resumo_receitas(margem_minima=30)

    assert 'Sem Ficha' in r['ficha_incompleta']
    assert 'Sem Preco' in r['sem_preco']
    assert len(r['margem_critica']) == 1
    crit = r['margem_critica'][0]
    assert crit['nome'] == 'Croissant'
    assert crit['canal'] == 'atacado'
    assert crit['margem'] == 10.0


def test_digest_texto_com_alertas(app):
    from app.extensions import db
    from app.services import saude_negocio
    _semear_contas(db)
    fake = {'custos': {}, 'pesos': {}, 'circulares': []}
    with patch('app.services.custos.calcular_custos_receitas',
               return_value=fake):
        texto = saude_negocio.montar_digest_saude()
    assert 'Radar do negocio' in texto
    assert 'VENCIDA' in texto
    assert 'R$ 100,00' in texto
    assert 'IA nao leu' in texto
    assert 'Catalogo saudavel' in texto   # sem receitas cadastradas = ok


def test_digest_tudo_em_dia(app):
    from app.services import saude_negocio
    fake = {'custos': {}, 'pesos': {}, 'circulares': []}
    with patch('app.services.custos.calcular_custos_receitas',
               return_value=fake):
        texto = saude_negocio.montar_digest_saude()
    assert 'Em dia' in texto
    assert 'Catalogo saudavel' in texto


def test_envio_usa_numero_do_dono(app):
    from app.services import saude_negocio
    app.config['ZAPI_BOT_DONO_NUMERO'] = '5511988887777'
    fake = {'custos': {}, 'pesos': {}, 'circulares': []}
    with patch('app.services.custos.calcular_custos_receitas',
               return_value=fake), \
         patch('app.services.zapi.enviar_texto',
               return_value={'ok': True}) as send:
        r = saude_negocio.enviar_digest_saude()
    assert r['ok'] is True
    assert send.call_args[0][0] == '5511988887777'


def test_rota_admin_saude(app):
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='Dono', login='dono3', papel='admin', is_owner=True)
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(u.id)
        s['_fresh'] = True
    fake = {'custos': {}, 'pesos': {}, 'circulares': []}
    with patch('app.services.custos.calcular_custos_receitas',
               return_value=fake):
        r = c.get('/admin/saude')
    data = r.get_json()
    assert 'contas' in data and 'receitas' in data
    assert data['contas']['abertas'] == 0
