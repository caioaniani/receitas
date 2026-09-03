from unittest.mock import patch

import pytest

from app.extensions import db
from app.models import GeocodeCache
from app.services import frete, google_maps, lalamove

ENDERECO = ('Avenida Nossa Senhora do Sabará, 1822, Apto 1704 torre 3, '
            'Jardim Campo Grande, São Paulo, SP, 04686-002')
POSTAL = {'street': 'Avenida Nossa Senhora do Sabará',
          'neighborhood': 'Jardim Campo Grande', 'city': 'São Paulo',
          'location': {'coordinates': {'latitude': '-23.5475', 'longitude': '-46.63611'}}}


def _resposta(body):
    class Resposta:
        status_code = 200

        def json(self):
            return body
    return Resposta()


def _google(tipo='ROOFTOP', numero='1822', parcial=False):
    return {'status': 'OK', 'results': [{
        'partial_match': parcial, 'types': ['street_address'],
        'address_components': [{'long_name': numero, 'types': ['street_number']}],
        'geometry': {'location_type': tipo,
                     'location': {'lat': -23.6708, 'lng': -46.6883}}}]}


def test_centroide_real_do_cep_nao_gera_cotacao(app):
    """Regressão da corrida 373: CEP da Sabará devolvia o centro da cidade."""
    with patch.object(lalamove, 'disponivel', return_value=True), \
         patch.object(lalamove, '_origem', return_value=(-23.6067, -46.6930, 'Loja')), \
         patch.object(frete, '_google_geocode', return_value=None), \
         patch.object(frete.requests, 'get', return_value=_resposta(POSTAL)), \
         patch.object(lalamove, '_request') as remoto, \
         patch('app.services.frete_sensor.registrar'), \
         patch('app.services.loja_alerta.alertar_endereco_falho'):
        resultado = lalamove.cotar(ENDERECO, 'moto')
    assert resultado['ok'] is False
    assert 'ponto de entrega' in resultado['erro']
    remoto.assert_not_called()


def test_limpa_complemento_e_usa_porta_validada_sem_coordenada_do_cep(app):
    app.config['GOOGLE_MAPS_API_KEY'] = 'teste'
    db.session.add(GeocodeCache(chave=google_maps._normalizar_chave(ENDERECO),
                               lat=-23.67, lng=-46.68, fonte='google_aprox'))
    db.session.commit()

    def responder(url, **kwargs):
        if 'brasilapi' in url:
            return _resposta(POSTAL)
        assert 'maps.googleapis.com' in url
        assert 'Apto' not in kwargs['params']['address']
        assert ', 1822,' in kwargs['params']['address']
        return _resposta(_google())

    with patch.object(frete.requests, 'get', side_effect=responder):
        resultado = frete.geocodificar_entrega(ENDERECO)
    assert resultado[:2] == (-23.6708, -46.6883)
    cache = GeocodeCache.query.filter_by(chave=google_maps._normalizar_chave(resultado[2])).one()
    assert cache.fonte == 'google_entrega'
    with patch.object(google_maps.requests, 'get') as remoto:
        assert google_maps.geocode_preciso(resultado[2], numero_entrega='1822') == resultado[:2]
        remoto.assert_not_called()


@pytest.mark.parametrize('tipo,numero,parcial', [
    ('APPROXIMATE', '1822', False), ('GEOMETRIC_CENTER', '1822', False),
    ('ROOFTOP', '1800', False), ('ROOFTOP', '1822', True),
])
def test_despacho_rejeita_ponto_sem_confirmacao_da_porta(app, tipo, numero, parcial):
    app.config['GOOGLE_MAPS_API_KEY'] = 'teste'
    # Cache antigo marcado 'google' também precisa ser revalidado.
    db.session.add(GeocodeCache(chave=google_maps._normalizar_chave(ENDERECO),
                               lat=-23.5475, lng=-46.63611, fonte='google'))
    db.session.commit()
    with patch.object(google_maps.requests, 'get', return_value=_resposta(_google(tipo, numero, parcial))) as remoto:
        assert google_maps.geocode_preciso(ENDERECO, numero_entrega='1822') is None
        remoto.assert_called_once()


def test_coordenada_informada_explicitamente_e_limites():
    assert frete.geocodificar_entrega('-23.60, -46.69')[:2] == (-23.60, -46.69)
    assert frete.geocodificar_entrega('99.00, -46.69') is None
    assert frete.geocodificar_entrega('04686-002') is None
