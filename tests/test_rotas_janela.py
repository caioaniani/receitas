"""Logística: ordenação das paradas por janela de horário + expresso.

Fix 2026-06-09: a roteirização ignorava horário — expresso podia ficar no fim
da rota e um pedido das 8h ser visitado às 14h. Agora EXPRESSO vem primeiro,
depois por horário, preservando a otimização geográfica dentro de cada janela.
"""
from unittest.mock import patch


def test_janela_rank():
    from app.services.rotas import janela_rank
    assert janela_rank({'expresso': True}) == -1          # expresso sempre 1o
    assert janela_rank({'periodo': '8h às 9h'}) == 8
    assert janela_rank({'periodo': '9h às 10h'}) == 9
    assert janela_rank({'periodo': '14h às 16h'}) == 14
    assert janela_rank({'periodo': ''}) == 99             # sem horario por ultimo
    assert janela_rank({}) == 99
    # expresso tem prioridade sobre qualquer periodo
    assert janela_rank({'expresso': True, 'periodo': '14h às 15h'}) == -1


def test_gerar_rotas_ordena_por_janela_e_expresso(app):
    """Sem Google (fallback por CEP), as paradas de um driver saem ordenadas:
    EXPRESSO primeiro, depois por horario crescente."""
    from app.services import rotas
    with app.app_context():
        app.config['GOOGLE_MAPS_API_KEY'] = ''   # forca o caminho sem otimizacao geo
        pedidos = [
            {'code': 'A', 'endereco': 'Rua X, 01310-100', 'periodo': '14h às 15h'},
            {'code': 'B', 'endereco': 'Rua Y, 01310-200', 'periodo': '8h às 9h'},
            {'code': 'C', 'endereco': 'Rua Z, 01310-300',
             'expresso': True, 'periodo': 'Expresso (1h)'},
            {'code': 'D', 'endereco': 'Rua W, 01310-400', 'periodo': '9h às 10h'},
        ]
        drivers = [{'id': 1, 'nome': 'Joao', 'cor': '#f00', 'capacidade': 999}]
        res = rotas.gerar_rotas(pedidos, drivers, app=app)
    assert len(res['rotas']) == 1
    ordem = [p['code'] for p in res['rotas'][0]['paradas']]
    assert ordem == ['C', 'B', 'D', 'A']   # expresso, 8h, 9h, 14h
    # o campo 'ordem' tambem reflete (1..N)
    assert [p['ordem'] for p in res['rotas'][0]['paradas']] == [1, 2, 3, 4]


def test_api_atribuidos_sem_driver_ordena_por_janela(app):
    """A tela default (aba Operacao) deve listar 'sem driver' com expresso no
    topo, mesmo sem re-otimizar."""
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='Admin', login='adm', papel='admin')
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(u.id)
        sess['_fresh'] = True

    pedidos = [
        {'code': 'A', 'destinatario': 'Ana', 'endereco': 'R X', 'periodo': '14h às 15h',
         'expresso': False, 'itens': [], 'cartinha_vnda': '', 'comprador': '', 'telefone': ''},
        {'code': 'B', 'destinatario': 'Bia', 'endereco': 'R Y', 'periodo': '8h às 9h',
         'expresso': False, 'itens': [], 'cartinha_vnda': '', 'comprador': '', 'telefone': ''},
        {'code': 'C', 'destinatario': 'Cau', 'endereco': 'R Z', 'periodo': 'Expresso (1h)',
         'expresso': True, 'itens': [], 'cartinha_vnda': '', 'comprador': '', 'telefone': ''},
    ]
    with patch('app.services.vnda.buscar_pedidos_do_dia',
               return_value={'pedidos': pedidos}):
        r = client.get('/entregas/api/atribuidos')
    data = r.get_json()
    codes = [p['code'] for p in data['sem_driver']]
    assert codes == ['C', 'B', 'A']   # expresso, 8h, 14h
