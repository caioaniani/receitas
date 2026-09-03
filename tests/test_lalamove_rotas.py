from unittest.mock import patch

from app.extensions import db
from app.models import LalamoveEntrega


def _client(app, usuario):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(usuario.id)
        sess['_fresh'] = True
    return client


def test_rotas_consulta_pontos_sem_criar_corrida(app, owner_user):
    entrega = LalamoveEntrega(pedido_code='PEDIDO', order_id='LALA123',
                              status='ON_GOING', endereco_destino='Rua X, 10')
    db.session.add(entrega)
    db.session.commit()
    client = _client(app, owner_user)
    app.config['LALAMOVE_ORIGEM_LATLNG'] = '-23.6095,-46.6907'
    with patch('app.services.lalamove._request') as remote:
        lista = client.get('/admin/lalamove-rotas').get_json()
        remote.assert_not_called()
        assert lista['origem']['coordenadas_fixas'] == '-23.6095,-46.6907'
        assert lista['corridas'][0]['pedido'] == 'PEDIDO'
        remote.return_value = (200, {'data': {
            'status': 'ON_GOING', 'stops': [
                {'address': 'Rua X, 10', 'coordinates': {'lat': '-23.55', 'lng': '-46.63'},
                 'phone': 'dado-nao-exposto'}]}})
        resposta = client.get(f'/admin/lalamove-rotas?entrega={entrega.id}')
        assert resposta.status_code == 200
        remote.assert_called_once_with('GET', '/v3/orders/LALA123')
        assert resposta.json['corrida_consultada']['pontos'][0]['coordenadas']['lat'] == '-23.55'
        assert 'dado-nao-exposto' not in resposta.get_data(as_text=True)
    assert entrega.status == 'ON_GOING'
    assert LalamoveEntrega.query.count() == 1


def test_rotas_restritas_ao_dono(app, admin_user):
    with patch('app.services.lalamove._request') as remote:
        assert _client(app, admin_user).get('/admin/lalamove-rotas').status_code == 403
        remote.assert_not_called()


def test_rotas_valida_entrega_e_falha_externa(app, owner_user):
    client = _client(app, owner_user)
    with patch('app.services.lalamove._request') as remote:
        assert client.get('/admin/lalamove-rotas?entrega=abc').status_code == 400
        assert client.get('/admin/lalamove-rotas?entrega=99999').status_code == 404
        remote.assert_not_called()
        entrega = LalamoveEntrega(pedido_code='P', order_id='O', status='COMPLETED')
        db.session.add(entrega)
        db.session.commit()
        remote.return_value = (403, {'errors': []})
        assert client.get(f'/admin/lalamove-rotas?entrega={entrega.id}').status_code == 502
