"""Faixas de frete por distância (mapa "Fretes O pão", KML de 10/06/2026):
anéis de 1 km a partir do Brooklin — grátis até 1 km, +R$5/km, máx 15 km.
Geocodificação: BrasilAPI (CEP) com fallback Nominatim (endereço livre)."""
from unittest.mock import patch

from app.services import frete


def test_valor_para_distancia_limites_dos_aneis():
    # limites batem com o KML: anel fecha no km cheio
    assert frete.valor_para_distancia(0.0) == 0.0
    assert frete.valor_para_distancia(1.0) == 0.0      # grátis até 1 km
    assert frete.valor_para_distancia(1.01) == 5.0     # anel 1-2 km
    assert frete.valor_para_distancia(2.0) == 5.0
    assert frete.valor_para_distancia(2.5) == 10.0
    assert frete.valor_para_distancia(14.2) == 70.0
    assert frete.valor_para_distancia(15.0) == 70.0    # último anel
    assert frete.valor_para_distancia(15.1) is None    # fora da área
    assert frete.valor_para_distancia(None) is None


def test_distancia_km_no_centro_e_zero():
    assert frete.distancia_km(frete.CENTRO_LAT, frete.CENTRO_LNG) < 0.001


def _resp(status=200, json_data=None):
    class R:
        status_code = status

        def json(self):
            return json_data
    return R()


def test_consultar_frete_por_cep_brasilapi():
    # ~3.5 km ao sul do centro -> anel de R$15 (3-4 km)
    body = {'street': 'Rua Teste', 'neighborhood': 'Campo Belo',
            'city': 'São Paulo',
            'location': {'coordinates': {'latitude': '-23.630',
                                         'longitude': '-46.6937'}}}
    with patch('app.services.frete.requests.get',
               return_value=_resp(200, body)) as g:
        r = frete.consultar_frete('04613-030')
    assert r['ok'] is True and r['fora_area'] is False
    assert r['valor'] == 15.0
    assert 'Campo Belo' in r['endereco']
    assert 'brasilapi' in g.call_args_list[0][0][0]


def test_consultar_frete_fora_da_area():
    body = {'city': 'Guarulhos',
            'location': {'coordinates': {'latitude': '-23.43',
                                         'longitude': '-46.53'}}}
    with patch('app.services.frete.requests.get',
               return_value=_resp(200, body)):
        r = frete.consultar_frete('07000-000')
    assert r['ok'] is True and r['fora_area'] is True
    assert 'valor' not in r


def test_consultar_frete_cep_sem_coordenada_cai_no_nominatim():
    """BrasilAPI conhece o CEP mas sem lat/lng -> geocodifica o endereço
    resolvido no Nominatim."""
    brasilapi = _resp(200, {'street': 'Rua Ribeiro do Vale',
                            'neighborhood': 'Brooklin', 'city': 'São Paulo',
                            'location': {'coordinates': {}}})
    nominatim = _resp(200, [{'lat': '-23.5990', 'lon': '-46.6940',
                             'display_name': 'Rua Ribeiro do Vale, Brooklin'}])

    def fake_get(url, **kw):
        return brasilapi if 'brasilapi' in url else nominatim

    with patch('app.services.frete.requests.get', side_effect=fake_get):
        r = frete.consultar_frete('04568-010')
    assert r['ok'] is True and r['gratis'] is True   # colado na padaria


def test_consultar_frete_endereco_livre_sem_cep():
    nominatim = _resp(200, [{'lat': '-23.61', 'lon': '-46.70',
                             'display_name': 'Av. Teste, Brooklin'}])
    with patch('app.services.frete.requests.get',
               return_value=nominatim) as g:
        r = frete.consultar_frete('Avenida Teste 100, Brooklin')
    assert r['ok'] is True and r['valor'] == 5.0     # ~1.4 km
    # endereço sem cidade ganha "São Paulo" na consulta
    assert 'São Paulo' in g.call_args[1]['params']['q']


def test_consultar_frete_nao_encontrado_e_vazio():
    with patch('app.services.frete.requests.get',
               return_value=_resp(200, [])):
        r = frete.consultar_frete('xyzabc sem endereco')
    assert r == {'ok': False, 'erro': 'nao_encontrado'}
    assert frete.consultar_frete('')['erro'] == 'endereco_vazio'


def test_simplificar_endereco():
    # complemento, bairro, estado e CEP fora; rua + numero + cidade ficam
    assert (frete.simplificar_endereco(
        'Rua Funchal, 418, apto 72 bloco B, Vila Olímpia, São Paulo, SP, 04551-060')
        == 'Rua Funchal, 418, São Paulo')
    assert frete.simplificar_endereco('Av Brasil, 1500') == 'Av Brasil, 1500, São Paulo'
    assert frete.simplificar_endereco('') is None


def test_geocodificar_cai_pro_endereco_simplificado():
    """Endereço real de e-commerce (complemento + bairro) derruba o Nominatim
    na 1a tentativa; a simplificada (rua, numero, cidade) resolve."""
    completo = 'Rua Funchal, 418, apto 72, Vila Olímpia, São Paulo, SP'
    chamadas = []

    def fake_texto(q):
        chamadas.append(q)
        if q == 'Rua Funchal, 418, São Paulo':
            return (-23.594, -46.689, 'Rua Funchal 418')
        return None

    with patch('app.services.frete._geocodificar_texto',
               side_effect=fake_texto):
        geo = frete.geocodificar(completo)
    assert geo == (-23.594, -46.689, 'Rua Funchal 418')
    assert chamadas == [completo, 'Rua Funchal, 418, São Paulo']


def test_geocodificar_aceita_latlng_direto():
    """Válvula de escape do atendente: colar 'lat,lng' no campo do painel."""
    geo = frete.geocodificar('-23.612012, -46.687506')
    assert geo[0] == -23.612012 and geo[1] == -46.687506


def test_tool_registrada_no_chatbot():
    from app.services import chatbot
    nomes = {t['name'] for t in chatbot.TOOLS}
    assert 'consultar_frete' in nomes
    body = {'location': {'coordinates': {'latitude': '-23.60',
                                         'longitude': '-46.6937'}},
            'city': 'São Paulo'}
    with patch('app.services.frete.requests.get',
               return_value=_resp(200, body)):
        r = chatbot._executar_tool('consultar_frete',
                                   {'endereco_ou_cep': '04613-030'})
    assert r['ok'] is True and r['gratis'] is True
