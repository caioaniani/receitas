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

    def fake_texto(q, ref=None, cep_ref=None):
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


def test_geocode_aceita_cidade_certa_mesmo_com_cep_torto_no_osm():
    """Caso real (09/07/2026) Alameda Porcelana, São Caetano do Sul (CEP
    09531-150): BrasilAPI sem coords; o Nominatim acha o nó CERTO (12 km,
    dentro da área), mas o OSM etiquetou ele com um CEP de OUTRO distrito
    (08671). O check por PREFIXO DE CEP barrava a venda. Agora, como a cidade
    do candidato bate com a resolvida pela BrasilAPI, aceita apesar do CEP
    torto — e cota o frete."""
    brasilapi = _resp(200, {'street': 'Alameda Porcelana',
                            'neighborhood': 'Cerâmica',
                            'city': 'São Caetano do Sul',
                            'location': {'coordinates': {}}})
    nominatim = _resp(200, [{
        'lat': '-23.6261115', 'lon': '-46.5775682',
        'display_name': 'Alameda Porcelana, Cerâmica, São Caetano do Sul, '
                        'São Paulo, 08671-035, Brasil',
        'address': {'road': 'Alameda Porcelana', 'suburb': 'Cerâmica',
                    'city': 'São Caetano do Sul', 'state': 'São Paulo',
                    'postcode': '08671-035'}}])

    def fake_get(url, **kw):
        return brasilapi if 'brasilapi' in url else nominatim

    with patch('app.services.frete.requests.get', side_effect=fake_get):
        r = frete.consultar_frete('Alameda Porcelana, Cerâmica, '
                                  'São Caetano do Sul, SP, 09531-150')
    assert r['ok'] is True and r['fora_area'] is False
    assert 'São Caetano do Sul' in r['endereco']
    assert 10 < r['distancia_km'] < 15           # ~12 km, dentro dos 25


def test_geocodificar_texto_cidade_e_sinal_forte_sobre_postcode():
    """Unidade do check de sanidade — a cidade manda sobre o postcode:
    - cidade divergente + postcode 'batendo' -> REJEITA (não reabre Arujá);
    - cidade batendo + postcode divergente -> ACEITA (conserta São Caetano)."""
    aruja = _resp(200, [{'lat': '-23.3965', 'lon': '-46.3210',
                         'display_name': 'Rua Qualquer, Arujá',
                         'address': {'city': 'Arujá', 'postcode': '01050-111'}}])
    with patch('app.services.frete.requests.get', return_value=aruja):
        geo = frete._geocodificar_texto(
            'Rua Qualquer', ref={'cidade': 'São Paulo'}, cep_ref='01050000')
    assert geo is None                     # cidade Arujá != São Paulo

    ok = _resp(200, [{'lat': '-23.6261', 'lon': '-46.5776',
                      'display_name': 'Alameda Porcelana, São Caetano do Sul',
                      'address': {'city': 'São Caetano do Sul',
                                  'postcode': '08671-035'}}])
    with patch('app.services.frete.requests.get', return_value=ok):
        geo = frete._geocodificar_texto(
            'Alameda Porcelana', ref={'cidade': 'São Caetano do Sul'},
            cep_ref='09531150')
    assert geo is not None and abs(geo[0] - (-23.6261)) < 1e-3   # CEP torto ignorado


def test_geocode_resgata_pelo_cep_quando_endereco_cai_no_homonimo():
    """Caso real (09/07/2026) Rua Guararapes, Brooklin, CEP 04561-000: a
    BrasilAPI conhece o CEP mas sem coords; "Rua Guararapes" é HOMÔNIMA
    (Brooklin × Lapa) e as tentativas por texto caem na da Lapa (postcode
    05079), corretamente rejeitada. O último recurso — geocodificar SÓ o CEP —
    resgata a venda com o centroide do distrito, em vez de 'nao_encontrado'."""
    brasilapi = _resp(200, {'street': 'Rua Guararapes',
                            'neighborhood': 'Brooklin Paulista',
                            'city': 'São Paulo',
                            'location': {'coordinates': {}}})
    lapa = _resp(200, [{'lat': '-23.5232', 'lon': '-46.7160',
                        'display_name': 'Rua Guararapes, Lapa',
                        'address': {'postcode': '05079-200'}}])   # sem city
    centroide = _resp(200, [{'lat': '-23.5724', 'lon': '-46.6585',
                             'display_name': '04561-000, Jardim Paulista',
                             'address': {'postcode': '04561-000'}}])

    def fake_get(url, **kw):
        if 'brasilapi' in url:
            return brasilapi
        q = (kw.get('params') or {}).get('q', '')
        return centroide if q.strip().startswith('04561-000') else lapa

    with patch('app.services.frete.requests.get', side_effect=fake_get):
        r = frete.consultar_frete('Rua Guararapes, 225, Brooklin Paulista, '
                                  'São Paulo, SP, 04561-000')
    assert r['ok'] is True and r['fora_area'] is False
    assert r['valor'] == 20.0                     # centroide ~4,6 km
    assert r['distancia_km'] < 6                   # Brooklin, não a Lapa (8,7)
    assert r['impreciso'] is True                  # resolveu só pelo CEP


def test_endereco_preciso_nao_marca_impreciso():
    """Endereço que resolve por texto (não pelo centroide) NÃO é impreciso."""
    nominatim = _resp(200, [{'lat': '-23.61', 'lon': '-46.70',
                             'display_name': 'Av. Teste, Brooklin'}])
    with patch('app.services.frete.requests.get', return_value=nominatim):
        r = frete.consultar_frete('Avenida Teste 100, Brooklin')
    assert r['ok'] is True and r.get('impreciso') is False


# ── Google como fonte precisa (09/07/2026) ─────────────────────────────────

def test_google_primeiro_quando_ativo(app):
    """Google ativo + resolve → usa Google (fonte=google) e NEM toca a cadeia
    grátis (nenhum requests.get de BrasilAPI/Nominatim)."""
    with app.app_context():
        app.config['GOOGLE_MAPS_API_KEY'] = 'k'
        app.config['FRETE_GOOGLE'] = '1'
        with patch('app.services.google_maps.geocode_preciso',
                   return_value=(-23.60, -46.69)) as g, \
             patch('app.services.frete.requests.get') as reqs:
            r = frete.consultar_frete('Rua Qualquer, 100, São Paulo, 01000-000')
    assert g.called and not reqs.called
    assert r['ok'] and r['fonte'] == 'google' and r['impreciso'] is False


def test_google_desligado_por_env_cai_na_cadeia_gratis(app):
    with app.app_context():
        app.config['GOOGLE_MAPS_API_KEY'] = 'k'
        app.config['FRETE_GOOGLE'] = '0'
        nominatim = _resp(200, [{'lat': '-23.61', 'lon': '-46.70',
                                 'display_name': 'X'}])
        with patch('app.services.google_maps.geocode_preciso') as g, \
             patch('app.services.frete.requests.get', return_value=nominatim):
            r = frete.consultar_frete('Avenida Teste 100, Brooklin')
    assert not g.called                            # kill-switch respeitado
    assert r['ok'] and r['fonte'] == 'gratis'


def test_google_teto_diario_para_de_chamar(app):
    """Teto diário: consumido o cap, para de bater no Google e cai na grátis."""
    with app.app_context():
        app.config['GOOGLE_MAPS_API_KEY'] = 'k'
        app.config['FRETE_GOOGLE'] = '1'
        app.config['FRETE_GOOGLE_MAX_DIA'] = 2

        def fake_get(url, **kw):
            # BrasilAPI COM coordenada (fonte grátis resolve sem Nominatim).
            if 'brasilapi' in url:
                return _resp(200, {'city': 'São Paulo', 'location': {
                    'coordinates': {'latitude': -23.60, 'longitude': -46.69}}})
            return _resp(200, [{'lat': '-23.61', 'lon': '-46.70',
                                'display_name': 'X'}])

        with patch('app.services.google_maps.geocode_preciso',
                   return_value=(-23.60, -46.69)) as g, \
             patch('app.services.frete.requests.get', side_effect=fake_get):
            frete.consultar_frete('Rua A, 1, São Paulo, 01000-000')
            frete.consultar_frete('Rua B, 2, São Paulo, 02000-000')
            r3 = frete.consultar_frete('Rua C, 3, São Paulo, 03000-000')
    assert g.call_count == 2                        # teto=2 → 3ª não bate
    assert r3['fonte'] == 'gratis'


def test_google_retenta_com_logradouro_oficial_do_cep(app):
    """Caso Mirelle (17/07/2026): cliente digitou "Rua Cândido de Azevedo
    Marques" — o nome oficial é "Rua JOAQUIM Cândido de Azevedo Marques".
    O Google falha no texto cru; a BrasilAPI conhece o CEP (sem coordenada)
    e devolve o logradouro OFICIAL → re-tenta o Google com ele (+ número) e
    resolve, em vez de morrer em nao_encontrado."""
    brasilapi = _resp(200, {
        'street': 'Rua Joaquim Cândido de Azevedo Marques',
        'neighborhood': 'Vila Morumbi', 'city': 'São Paulo',
        'location': {'coordinates': {}}})
    urls = []

    def fake_get(url, **kw):
        urls.append(url)
        return brasilapi

    with app.app_context():
        app.config['GOOGLE_MAPS_API_KEY'] = 'k'
        app.config['FRETE_GOOGLE'] = '1'
        with patch('app.services.google_maps.geocode_preciso',
                   side_effect=[None, (-23.6097, -46.7110)]) as g, \
             patch('app.services.frete.requests.get', side_effect=fake_get):
            r = frete.consultar_frete(
                'Rua Cândido de Azevedo Marques, 750, Morumbi, Sp, Sp, '
                '05688-020')
    assert r['ok'] is True and r['fora_area'] is False
    assert r['fonte'] == 'google' and r['impreciso'] is False
    assert g.call_count == 2
    canonico = g.call_args_list[1][0][0]
    assert 'Joaquim' in canonico and ', 750,' in canonico
    assert '05688-020' in canonico
    # Google resolveu ANTES da cadeia grátis: nenhum Nominatim na linha.
    assert all('brasilapi' in u for u in urls)


def test_cep_sem_logradouro_nao_retenta_google(app):
    """Guard da retentativa: BrasilAPI sem 'street' (CEP geral) → NÃO chama o
    Google de novo (bairro/cidade viram centroide, que o geocode_preciso
    rejeitaria — seria chamada paga inútil); segue pra cadeia grátis."""
    brasilapi = _resp(200, {
        'street': None, 'neighborhood': 'Centro', 'city': 'São Paulo',
        'location': {'coordinates': {}}})
    nominatim = _resp(200, [{'lat': '-23.61', 'lon': '-46.70',
                             'display_name': 'Centro, São Paulo',
                             'address': {'city': 'São Paulo'}}])

    def fake_get(url, **kw):
        return brasilapi if 'brasilapi' in url else nominatim

    with app.app_context():
        app.config['GOOGLE_MAPS_API_KEY'] = 'k'
        app.config['FRETE_GOOGLE'] = '1'
        with patch('app.services.google_maps.geocode_preciso',
                   return_value=None) as g, \
             patch('app.services.frete.requests.get', side_effect=fake_get):
            r = frete.consultar_frete('Rua Qualquer, 10, São Paulo, 01000-000')
    assert g.call_count == 1                       # só o texto cru
    assert r['ok'] is True and r['fonte'] == 'gratis'


def test_geocode_preciso_rejeita_approximate_e_aceita_rooftop(app):
    """O Google APPROXIMATE (centroide de cidade) NÃO vale como preciso — vira
    None pra cair na cadeia grátis; ROOFTOP vale."""
    from app.services import google_maps
    with app.app_context():
        app.config['GOOGLE_MAPS_API_KEY'] = 'k'
        aprox = _resp(200, {'status': 'OK', 'results': [{'geometry': {
            'location': {'lat': -23.5, 'lng': -46.6},
            'location_type': 'APPROXIMATE'}}]})
        with patch('app.services.google_maps.requests.get', return_value=aprox):
            assert google_maps.geocode_preciso('Rua Vaga, São Paulo') is None
        roof = _resp(200, {'status': 'OK', 'results': [{'geometry': {
            'location': {'lat': -23.6, 'lng': -46.69},
            'location_type': 'ROOFTOP'}}]})
        with patch('app.services.google_maps.requests.get', return_value=roof):
            assert google_maps.geocode_preciso(
                'Rua Certa, 100, São Paulo') == (-23.6, -46.69)


def test_geocode_partial_match_do_google_nao_vale(app):
    from app.services import google_maps
    with app.app_context():
        app.config['GOOGLE_MAPS_API_KEY'] = 'k'
        parcial = _resp(200, {'status': 'OK', 'results': [{'partial_match': True,
            'geometry': {'location': {'lat': -23.6, 'lng': -46.69},
                         'location_type': 'ROOFTOP'}}]})
        with patch('app.services.google_maps.requests.get', return_value=parcial):
            assert google_maps.geocode_preciso('Rua Meio Certa, SP') is None


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
