"""Faixas de frete por distância: anéis de 1 km a partir do Brooklin —
grátis até 1 km, +R$5/km, máx 25 km (decisão do dono 19/06/2026; era 15 km).
Geocodificação: BrasilAPI (CEP) com fallback Nominatim (endereço livre)."""
from unittest.mock import patch

from app.services import frete


def test_valor_para_distancia_limites_dos_aneis():
    # anel fecha no km cheio
    assert frete.valor_para_distancia(0.0) == 0.0
    assert frete.valor_para_distancia(1.0) == 0.0      # grátis até 1 km
    assert frete.valor_para_distancia(1.01) == 5.0     # anel 1-2 km (2º km)
    assert frete.valor_para_distancia(2.0) == 5.0
    assert frete.valor_para_distancia(2.5) == 10.0
    assert frete.valor_para_distancia(14.2) == 70.0
    # Novo raio = 25 km: o que antes era "fora" (>15) agora entrega.
    assert frete.valor_para_distancia(20.0) == 95.0    # 5 * (20-1)
    assert frete.valor_para_distancia(25.0) == 120.0   # último anel (5*24)
    assert frete.valor_para_distancia(25.1) is None    # fora da área
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


# ── Sanidade de CEP no geocode (05/07/2026 — homônimos) ─────────────────

def test_geocode_rejeita_homonimo_de_outro_distrito():
    """Caso real "Rua Nova York": BrasilAPI sem coords; Nominatim devolve a
    homônima do GRAJAÚ (postcode 04853) na frente da certa (04560). O check
    do CEP pula a errada e fica com a do Brooklin."""
    brasilapi = _resp(200, {'street': 'Rua Nova York',
                            'neighborhood': 'Brooklin', 'city': 'São Paulo',
                            'location': {'coordinates': {}}})
    nominatim = _resp(200, [
        {'lat': '-23.7720', 'lon': '-46.6795',
         'display_name': 'Rua Nova York, Grajaú',
         'address': {'postcode': '04853-080'}},
        {'lat': '-23.6153', 'lon': '-46.6848',
         'display_name': 'Rua Nova York, Brooklin Novo',
         'address': {'postcode': '04560-000'}},
    ])

    def fake_get(url, **kw):
        return brasilapi if 'brasilapi' in url else nominatim

    with patch('app.services.frete.requests.get', side_effect=fake_get):
        r = frete.consultar_frete('Rua Nova York, Brooklin, São Paulo, '
                                  '04560-000')
    assert r['ok'] is True and r['fora_area'] is False
    assert 'Brooklin' in r['endereco']
    assert r['distancia_km'] < 5          # nunca os 19,3 km do Grajaú


def test_geocode_rejeita_cidade_errada_e_cai_pro_texto():
    """Caso real D Lucas (CEP 01050-000): o rótulo da BrasilAPI caía na
    "Rua Martins Fontes" de ARUJÁ (44 km → bloqueado). Com o check, a
    tentativa do rótulo morre e a do texto cru resolve o Centro (7,4 km)."""
    brasilapi = _resp(200, {'street': 'Rua Martins Fontes',
                            'neighborhood': 'Centro', 'city': 'São Paulo',
                            'location': {'coordinates': {}}})
    aruja = _resp(200, [{'lat': '-23.3965', 'lon': '-46.3210',
                         'display_name': 'Rua Martins Fontes, Arujá',
                         'address': {'postcode': '07402-000'}}])
    centro = _resp(200, [{'lat': '-23.5492', 'lon': '-46.6445',
                          'display_name': '01050-000, República, São Paulo',
                          'address': {'postcode': '01050-000'}}])
    chamadas = []

    def fake_get(url, **kw):
        if 'brasilapi' in url:
            return brasilapi
        chamadas.append(kw.get('params', {}).get('q', ''))
        return aruja if len(chamadas) == 1 else centro

    with patch('app.services.frete.requests.get', side_effect=fake_get):
        r = frete.consultar_frete('01050-000')
    assert r['ok'] is True and r['fora_area'] is False
    assert r['distancia_km'] < 10         # República, não Arujá (44 km)


def test_geocode_sem_postcode_no_resultado_aceita():
    """OSM sem postcode no candidato: o check é só contra divergência
    POSITIVA — sem dado, aceita (comportamento antigo preservado)."""
    brasilapi = _resp(200, {'street': 'Rua X', 'neighborhood': 'Brooklin',
                            'city': 'São Paulo',
                            'location': {'coordinates': {}}})
    nominatim = _resp(200, [{'lat': '-23.5990', 'lon': '-46.6940',
                             'display_name': 'Rua X, Brooklin',
                             'address': {}}])

    def fake_get(url, **kw):
        return brasilapi if 'brasilapi' in url else nominatim

    with patch('app.services.frete.requests.get', side_effect=fake_get):
        r = frete.consultar_frete('Rua X, 04568-010')
    assert r['ok'] is True and r['gratis'] is True


def test_geocode_todos_divergentes_vira_nao_encontrado():
    """Nenhum candidato com CEP compatível: melhor falhar honesto ("não
    consegui localizar") do que inventar frete de outra cidade."""
    brasilapi = _resp(200, {'street': 'Rua Y', 'neighborhood': 'Centro',
                            'city': 'São Paulo',
                            'location': {'coordinates': {}}})
    errado = _resp(200, [{'lat': '-23.39', 'lon': '-46.32',
                          'display_name': 'Rua Y, Arujá',
                          'address': {'postcode': '07402-000'}}])

    def fake_get(url, **kw):
        return brasilapi if 'brasilapi' in url else errado

    with patch('app.services.frete.requests.get', side_effect=fake_get):
        r = frete.consultar_frete('Rua Y, 123, Centro, 01050-000')
    assert r == {'ok': False, 'erro': 'nao_encontrado'}
