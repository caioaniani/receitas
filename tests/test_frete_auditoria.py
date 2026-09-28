"""Frete do site — achados da auditoria do checkout (27/09/2026).

1. Teto diário do Google POR CANAL: o preview anônimo (/loja/api/frete,
   qualquer texto) tem sub-teto próprio e não consegue esgotar o Google do
   POST do checkout nem do despacho; resposta NEGATIVA do Google
   (ZERO_RESULTS) fica em cache e o mesmo texto repetido não paga de novo.
2. A coordenada do CEP na BrasilAPI não é mais aceita como PRECISA: a
   retentativa do Google com o logradouro oficial roda sempre que a BrasilAPI
   conhece a rua; a coordenada dela só resgata a venda como ÚLTIMO recurso,
   marcada imprecisa (fonte 'brasilapi').
3. As envs do frete chegam ao app (declaradas no config.py).
"""
import importlib.util
import logging
import os
from datetime import timedelta
from unittest.mock import patch

import requests
from flask import Flask

from app.extensions import db
from app.models import AppConfig, GeocodeCache
from app.services import frete
from app.utils import agora, hoje


class _Resp:
    def __init__(self, status=200, json_data=None):
        self.status_code = status
        self._json = json_data

    def json(self):
        return self._json


def _google_ok(lat=-23.60, lng=-46.69, location_type='ROOFTOP'):
    return _Resp(200, {'status': 'OK', 'results': [{
        'geometry': {'location': {'lat': lat, 'lng': lng},
                     'location_type': location_type}}]})


def _nominatim_vazio(url, **kw):
    """Cadeia grátis sem resultado (Nominatim devolve lista vazia)."""
    return _Resp(200, [])


def _ligar_google(app, teto=5, teto_preview=None):
    app.config['GOOGLE_MAPS_API_KEY'] = 'chave-teste'
    app.config['FRETE_GOOGLE'] = '1'
    app.config['FRETE_GOOGLE_MAX_DIA'] = str(teto)
    if teto_preview is not None:
        app.config['FRETE_GOOGLE_MAX_DIA_PREVIEW'] = str(teto_preview)


# ── 1. Cota do Google por canal ─────────────────────────────────────────────

def test_preview_anonimo_nao_esgota_o_google_do_checkout(app):
    """O achado da auditoria: 5 cotações anônimas com textos quaisquer
    esgotavam o teto e o checkout ficava sem geocode preciso até a
    meia-noite. Com teto 5, o preview para em 60% (3) e o checkout usa o
    restante."""
    _ligar_google(app, teto=5)
    with patch('app.services.google_maps.geocode_preciso',
               return_value=(-23.60, -46.69)) as g, \
         patch('app.services.frete.requests.get',
               side_effect=_nominatim_vazio):
        for i in range(5):
            frete.consultar_frete(f'Rua Anonima {i}, São Paulo',
                                  canal=frete.CANAL_PREVIEW)
        assert g.call_count == 3                  # sub-teto do preview
        r = frete.consultar_frete('Rua do Cliente, 10, São Paulo')
    assert g.call_count == 4                      # checkout ainda tem vaga
    assert r['ok'] and r['fonte'] == 'google'
    assert frete.uso_google_hoje() == {'total': 4, 'preview': 3, 'teto': 5,
                                       'teto_preview': 3}


def test_teto_total_continua_valendo_para_todos_os_canais(app):
    """O teto total segue sendo o limite de custo do dia: esgotado, nem o
    checkout chama o Google (cai na cadeia grátis)."""
    _ligar_google(app, teto=3, teto_preview=1)
    with patch('app.services.google_maps.geocode_preciso',
               return_value=(-23.60, -46.69)) as g, \
         patch('app.services.frete.requests.get',
               side_effect=_nominatim_vazio):
        frete.consultar_frete('Rua P, 1, São Paulo', canal='preview')
        frete.consultar_frete('Rua C1, 1, São Paulo')
        frete.consultar_frete('Rua C2, 1, São Paulo')
        r = frete.consultar_frete('Rua C3, 1, São Paulo')
    assert g.call_count == 3
    assert r == {'ok': False, 'erro': 'nao_encontrado'}


def test_checkout_usa_a_parte_que_o_preview_nao_usou(app):
    """A reserva do preview é um LIMITE dele, não uma cota presa: sem
    preview no dia, o checkout pode usar o teto inteiro."""
    _ligar_google(app, teto=3)
    with patch('app.services.google_maps.geocode_preciso',
               return_value=(-23.60, -46.69)) as g, \
         patch('app.services.frete.requests.get',
               side_effect=_nominatim_vazio):
        for i in range(3):
            frete.consultar_frete(f'Rua Checkout {i}, São Paulo')
    assert g.call_count == 3


def test_despacho_nao_e_limitado_pelo_sub_teto_do_preview(app):
    """O ponto da Lalamove (geocodificar_entrega) usa o restante do teto,
    como o checkout — o preview esgotado não para o motoboy."""
    _ligar_google(app, teto=5, teto_preview=0)
    with patch('app.services.google_maps.geocode_preciso',
               return_value=(-23.60, -46.69)) as g, \
         patch('app.services.frete.requests.get',
               side_effect=_nominatim_vazio):
        frete.consultar_frete('Rua Anonima, 5, São Paulo', canal='preview')
        assert g.call_count == 0                  # preview sem cota
        ponto = frete.geocodificar_entrega('Rua Destino, 120, São Paulo')
    assert ponto == (-23.60, -46.69, 'Rua Destino, 120, São Paulo')
    assert g.call_count == 1


def test_sem_canal_e_checkout_e_canal_desconhecido_avisa(app, caplog):
    """Fail-safe: quem não informa o canal é checkout. Canal com erro de
    digitação também vira checkout, mas grita no log (erro de programação
    não pode derrubar a cotação do cliente nem sumir em silêncio)."""
    _ligar_google(app, teto=5, teto_preview=0)
    with patch('app.services.google_maps.geocode_preciso',
               return_value=(-23.60, -46.69)) as g, \
         patch('app.services.frete.requests.get',
               side_effect=_nominatim_vazio), \
         caplog.at_level(logging.ERROR, logger='app.services.frete'):
        frete.consultar_frete('Rua Sem Canal, 1, São Paulo')
        frete.consultar_frete('Rua Typo, 1, São Paulo', canal='previw')
    assert g.call_count == 2
    assert any('canal desconhecido' in rec.message for rec in caplog.records)


def test_sem_chave_do_google_nao_consome_vaga(app):
    """Sem chave nenhuma chamada remota acontece — o contador do dia não
    pode andar (antes a vaga era reservada mesmo sem chave)."""
    app.config['GOOGLE_MAPS_API_KEY'] = ''
    app.config['FRETE_GOOGLE'] = '1'
    with patch('app.services.frete.requests.get',
               side_effect=_nominatim_vazio):
        frete.consultar_frete('Rua Sem Chave, 1, São Paulo', canal='preview')
    assert AppConfig.get('frete_google_dia') is None
    assert frete.uso_google_hoje()['total'] == 0


def test_tetos_configuraveis(app, caplog):
    app.config['FRETE_GOOGLE_MAX_DIA'] = '500'
    assert frete.tetos_google() == (500, 300)           # padrão: 60%
    app.config['FRETE_GOOGLE_MAX_DIA_PREVIEW'] = '120'
    assert frete.tetos_google() == (500, 120)           # absoluto
    app.config['FRETE_GOOGLE_MAX_DIA_PREVIEW'] = '25%'
    assert frete.tetos_google() == (500, 125)           # percentual
    app.config['FRETE_GOOGLE_MAX_DIA_PREVIEW'] = '9999'
    assert frete.tetos_google() == (500, 500)           # nunca passa do total
    app.config['FRETE_GOOGLE_MAX_DIA_PREVIEW'] = ''
    assert frete.tetos_google() == (500, 300)
    app.config['FRETE_GOOGLE_MAX_DIA'] = '0'
    assert frete.tetos_google() == (0, 0)               # 0 = nenhuma remota
    with caplog.at_level(logging.WARNING, logger='app.services.frete'):
        app.config['FRETE_GOOGLE_MAX_DIA'] = 'muito'
        app.config['FRETE_GOOGLE_MAX_DIA_PREVIEW'] = 'metade'
        assert frete.tetos_google() == (500, 300)
    avisos = ' '.join(rec.message for rec in caplog.records)
    assert 'FRETE_GOOGLE_MAX_DIA' in avisos and 'metade' in avisos


def test_contador_do_formato_antigo_e_de_outro_dia(app):
    """O deploy do sub-teto não zera o total do dia (formato antigo
    'AAAA-MM-DD|total' lido com preview 0); contador de ontem não conta."""
    _ligar_google(app, teto=10)
    AppConfig.set('frete_google_dia', f'{hoje().isoformat()}|7')
    db.session.commit()
    assert frete.uso_google_hoje()['total'] == 7
    assert frete.uso_google_hoje()['preview'] == 0
    ontem = (hoje() - timedelta(days=1)).isoformat()
    AppConfig.set('frete_google_dia', f'{ontem}|10|6')
    db.session.commit()
    assert frete.uso_google_hoje()['total'] == 0


def test_reserva_nao_perde_incremento_de_outro_worker(app):
    """Compare-and-swap: se outro worker incrementou entre a leitura e a
    gravação, a reserva relê e soma por cima — o ler→somar→gravar antigo
    sobrescrevia o incremento do outro e deixava passar do teto."""
    from sqlalchemy import update
    _ligar_google(app, teto=10)
    dia = hoje().isoformat()
    AppConfig.set('frete_google_dia', f'{dia}|2|0')
    db.session.commit()
    original = frete._ler_uso
    estado = {'interferiu': False}

    def ler_com_concorrente(bruto, hoje_iso):
        resultado = original(bruto, hoje_iso)
        if not estado['interferiu']:
            # O "outro worker" grava, em OUTRA conexão, entre a leitura e o
            # UPDATE desta reserva.
            estado['interferiu'] = True
            with db.engine.begin() as outro:
                outro.execute(update(AppConfig.__table__)
                              .where(AppConfig.__table__.c.key == 'frete_google_dia')
                              .values(value=f'{dia}|3|1'))
        return resultado

    with patch('app.services.frete._ler_uso', side_effect=ler_com_concorrente):
        assert frete._reservar_vaga_google(frete.CANAL_CHECKOUT) is True
    assert AppConfig.get('frete_google_dia') == f'{dia}|4|1'


def test_falha_de_banco_na_reserva_nao_descarta_o_estado_do_chamador(app,
                                                                     caplog):
    """Erro de banco no contador: sem Google nesta cotação, e a sessão do
    chamador segue intacta (o contador roda em conexão própria)."""
    from sqlalchemy.exc import OperationalError
    _ligar_google(app, teto=10)
    db.session.add(AppConfig(key='estado_do_chamador', value='preservar'))

    def banco_caiu(bruto, hoje_iso):
        raise OperationalError('SELECT', {}, Exception('conexão perdida'))

    with patch('app.services.frete._ler_uso', side_effect=banco_caiu), \
         caplog.at_level(logging.WARNING, logger='app.services.frete'):
        assert frete._reservar_vaga_google(frete.CANAL_CHECKOUT) is False
    db.session.commit()
    assert AppConfig.get('estado_do_chamador') == 'preservar'
    assert AppConfig.get('frete_google_dia') is None
    assert any('contador do teto' in rec.message for rec in caplog.records)


def test_reserva_nao_commita_a_transacao_do_chamador(app):
    """Revisão 28/09/2026: a reserva fazia `commit` da sessão do checkout (e
    soltava o FOR UPDATE da compra de kit) no meio da compra."""
    _ligar_google(app, teto=10)
    db.session.add(AppConfig(key='pendente_do_chamador', value='x'))
    assert frete._reservar_vaga_google(frete.CANAL_CHECKOUT) is True
    db.session.rollback()                     # o chamador desiste
    assert AppConfig.get('pendente_do_chamador') is None
    assert frete.uso_google_hoje()['total'] == 1   # a vaga ficou gravada


def test_zero_results_fica_em_cache_e_nao_paga_de_novo(app):
    """O mesmo texto que o Google não conhece, repetido (cliente clicando
    "Calcular frete" de novo), não gasta outra vaga nem outra chamada."""
    _ligar_google(app, teto=10)
    chamadas_google = []

    def fake_get(url, **kw):
        # google_maps e frete usam o MESMO requests.get: um fake só,
        # despachando pela URL.
        if 'googleapis' in url:
            chamadas_google.append(kw.get('params', {}).get('address'))
            return _Resp(200, {'status': 'ZERO_RESULTS', 'results': []})
        return _Resp(200, [])

    with patch('app.services.frete.requests.get', side_effect=fake_get):
        r1 = frete.consultar_frete('Rua Que Nao Existe, 1', canal='preview')
        r2 = frete.consultar_frete('Rua Que Nao Existe, 1', canal='preview')
    assert r1 == r2 == {'ok': False, 'erro': 'nao_encontrado'}
    assert chamadas_google == ['Rua Que Nao Existe, 1']
    assert frete.uso_google_hoje()['total'] == 1
    cache = GeocodeCache.query.one()
    assert cache.fonte == 'google_zero' and cache.lat is None


def test_zero_results_vencido_consulta_de_novo(app):
    """O negativo vale por um prazo — rua/condomínio novo passa a existir."""
    from app.services import google_maps
    _ligar_google(app, teto=10)
    db.session.add(GeocodeCache(
        chave='rua nova, 1', lat=None, lng=None, fonte='google_zero',
        criado_em=agora() - timedelta(days=google_maps.TTL_NEGATIVO_DIAS + 1)))
    db.session.commit()
    with patch('app.services.google_maps.requests.get',
               return_value=_google_ok()) as google:
        assert frete._google_geocode('Rua Nova, 1') == (-23.60, -46.69)
    assert google.call_count == 1
    assert GeocodeCache.query.one().fonte == 'google'


def test_status_transitorio_do_google_nao_vira_cache_negativo(app, caplog):
    """OVER_QUERY_LIMIT/REQUEST_DENIED podem dar certo depois: não cacheia,
    mas avisa no log (chave mal configurada não pode ficar muda)."""
    from app.services import google_maps
    app.config['GOOGLE_MAPS_API_KEY'] = 'chave-teste'
    negado = _Resp(200, {'status': 'REQUEST_DENIED',
                         'error_message': 'API key inválida'})
    with patch('app.services.google_maps.requests.get', return_value=negado), \
         caplog.at_level(logging.WARNING, logger='app.services.google_maps'):
        assert google_maps.geocode_preciso('Rua X, 1') is None
    assert GeocodeCache.query.count() == 0
    assert any('REQUEST_DENIED' in rec.message for rec in caplog.records)


def test_cache_aproximado_nao_consome_vaga(app):
    _ligar_google(app, teto=1)
    db.session.add(GeocodeCache(chave='rua vaga, sp', lat=-23.5, lng=-46.6,
                                fonte='google_aprox'))
    db.session.commit()
    with patch('app.services.google_maps.requests.get') as google:
        assert frete._google_geocode('Rua Vaga, SP', canal='preview') is None
    assert not google.called
    assert frete.uso_google_hoje()['total'] == 0


def test_corrida_na_gravacao_do_cache_nao_inutiliza_a_sessao(app):
    """Dois cliques em "Calcular frete" gravam a mesma chave ao mesmo tempo:
    o unique recusa um deles. Antes o IntegrityError estourava no commit e
    deixava a sessão do checkout inutilizada; agora o savepoint desfaz só a
    escrita do cache."""
    from app.services import google_maps
    app.config['GOOGLE_MAPS_API_KEY'] = 'chave-teste'
    db.session.add(GeocodeCache(chave='rua disputada, 7', lat=-23.61,
                                lng=-46.70, fonte='google'))
    db.session.commit()
    cfg = AppConfig(key='pendente_do_chamador', value='x')
    db.session.add(cfg)

    import sqlalchemy
    real_select = sqlalchemy.select

    def select_cego(*cols):
        """Simula o outro processo: a linha existe, mas este não a viu."""
        consulta = real_select(*cols)

        class _Cega:
            def where(self, *a):
                return consulta.where(
                    GeocodeCache.__table__.c.chave == '__ninguem__')
        return _Cega()

    with patch.object(sqlalchemy, 'select', select_cego):
        google_maps._gravar_cache_preciso('rua disputada, 7', (-1.0, -1.0),
                                          'google')
    db.session.commit()                         # sessão segue utilizável
    linha = GeocodeCache.query.filter_by(chave='rua disputada, 7').one()
    assert (linha.lat, linha.lng) == (-23.61, -46.70)   # a do outro ficou
    assert AppConfig.get('pendente_do_chamador') == 'x'


# ── 2. BrasilAPI: coordenada do CEP só como último recurso ──────────────────

_BRASILAPI_COM_COORD = {
    'street': 'Rua Joaquim Cândido de Azevedo Marques',
    'neighborhood': 'Vila Morumbi', 'city': 'São Paulo',
    # O "centro de SP" que a BrasilAPI devolveu para CEPs diferentes
    # (corridas 373/375/370, 03/09/2026).
    'location': {'coordinates': {'latitude': '-23.5505',
                                 'longitude': '-46.6333'}}}


def test_brasilapi_com_coordenada_nao_pula_a_retentativa_do_google(app):
    """Antes: Google falhava no texto cru, a BrasilAPI tinha coordenada e ela
    era devolvida como PRECISA ('gratis'), sem a retentativa com o
    logradouro oficial — frete cotado no centro de SP."""
    _ligar_google(app, teto=10)

    def fake_get(url, **kw):
        return _Resp(200, _BRASILAPI_COM_COORD) if 'brasilapi' in url \
            else _Resp(200, [])

    with patch('app.services.google_maps.geocode_preciso',
               side_effect=[None, (-23.6097, -46.7110)]) as g, \
         patch('app.services.frete.requests.get', side_effect=fake_get):
        r = frete.consultar_frete(
            'Rua Cândido de Azevedo Marques, 750, Morumbi, 05688-020')
    assert g.call_count == 2
    assert 'Joaquim' in g.call_args_list[1][0][0]
    assert r['fonte'] == 'google' and r['impreciso'] is False
    assert r['distancia_km'] < 3                     # não o centro de SP (~8)


def test_brasilapi_com_coordenada_cede_ao_nominatim_validado(app):
    """Sem Google, o endereço oficial no Nominatim (validado por cidade)
    vale mais que a coordenada do CEP."""
    app.config['FRETE_GOOGLE'] = '0'

    def fake_get(url, **kw):
        if 'brasilapi' in url:
            return _Resp(200, _BRASILAPI_COM_COORD)
        return _Resp(200, [{'lat': '-23.6097', 'lon': '-46.7110',
                            'display_name': 'Rua Joaquim Cândido',
                            'address': {'city': 'São Paulo'}}])

    with patch('app.services.frete.requests.get', side_effect=fake_get):
        r = frete.consultar_frete('05688-020')
    assert r['fonte'] == 'gratis' and r['impreciso'] is False
    assert r['distancia_km'] < 3


def test_coordenada_da_brasilapi_e_ultimo_recurso_imprecisa(app):
    """Nada mais localizou: a coordenada do CEP resgata a venda, mas
    IMPRECISA e com fonte própria (o checkout alerta o dono e registra no
    sensor)."""
    app.config['FRETE_GOOGLE'] = '0'

    def fake_get(url, **kw):
        return _Resp(200, _BRASILAPI_COM_COORD) if 'brasilapi' in url \
            else _Resp(200, [])

    with patch('app.services.frete.requests.get', side_effect=fake_get):
        r = frete.consultar_frete(
            'Rua Cândido de Azevedo Marques, 750, 05688-020')
    assert r['ok'] is True
    assert r['impreciso'] is True and r['fonte'] == 'brasilapi'
    assert abs(r['distancia_km'] - round(frete.distancia_km(-23.5505,
                                                            -46.6333), 1)) < 0.1


def test_centroide_do_cep_no_nominatim_vem_antes_da_brasilapi(app):
    app.config['FRETE_GOOGLE'] = '0'
    chamadas = []

    def fake_get(url, **kw):
        if 'brasilapi' in url:
            return _Resp(200, _BRASILAPI_COM_COORD)
        q = (kw.get('params') or {}).get('q', '')
        chamadas.append(q)
        if q.startswith('05688-020'):
            return _Resp(200, [{'lat': '-23.6100', 'lon': '-46.7100',
                                'display_name': 'CEP 05688-020'}])
        return _Resp(200, [])

    with patch('app.services.frete.requests.get', side_effect=fake_get):
        r = frete.consultar_frete('Rua Errada, 750, 05688-020')
    assert r['fonte'] == 'cep_centroide' and r['impreciso'] is True
    assert any(q.startswith('05688-020') for q in chamadas)


_BRASILAPI_SEM_COORD = dict(_BRASILAPI_COM_COORD, location={'coordinates': {}})


def test_com_numero_o_texto_do_cliente_vem_antes_do_rotulo_oficial(app):
    """Revisão 28/09/2026: com número, o rótulo oficial (rua SEM número) era
    tentado primeiro e aceito como PRECISO — um ponto qualquer da rua."""
    app.config['FRETE_GOOGLE'] = '0'
    consultas = []

    def fake_get(url, **kw):
        if 'brasilapi' in url:
            return _Resp(200, _BRASILAPI_SEM_COORD)
        q = (kw.get('params') or {}).get('q', '')
        consultas.append(q)
        return _Resp(200, [{'lat': '-23.6097', 'lon': '-46.7110',
                            'display_name': q, 'address': {'city': 'São Paulo'}}])

    with patch('app.services.frete.requests.get', side_effect=fake_get):
        r = frete.consultar_frete(
            'Rua Cândido de Azevedo Marques, 750, Morumbi, 05688-020')
    assert '750' in consultas[0]
    assert r['fonte'] == 'gratis' and r['impreciso'] is False


def test_numero_nao_localizado_cota_pela_rua_marcado_impreciso(app):
    app.config['FRETE_GOOGLE'] = '0'

    def fake_get(url, **kw):
        if 'brasilapi' in url:
            return _Resp(200, _BRASILAPI_SEM_COORD)
        q = (kw.get('params') or {}).get('q', '')
        if '750' in q or not q.startswith('Rua Joaquim'):
            return _Resp(200, [])
        return _Resp(200, [{'lat': '-23.6097', 'lon': '-46.7110',
                            'display_name': q, 'address': {'city': 'São Paulo'}}])

    with patch('app.services.frete.requests.get', side_effect=fake_get):
        r = frete.consultar_frete(
            'Rua Cândido de Azevedo Marques, 750, Morumbi, 05688-020')
    assert r['ok'] is True
    assert r['fonte'] == 'rua_sem_numero' and r['impreciso'] is True


def test_nominatim_fora_encerra_a_cadeia_com_a_coordenada_da_brasilapi(app):
    """Nominatim fora custava 5 × 8 s por cotação (uma chamada por etapa)."""
    app.config['FRETE_GOOGLE'] = '0'
    nominatim = []

    def fake_get(url, **kw):
        if 'brasilapi' in url:
            return _Resp(200, _BRASILAPI_COM_COORD)
        nominatim.append(url)
        raise requests.ConnectionError('fora')

    with patch('app.services.frete.requests.get', side_effect=fake_get):
        r = frete.consultar_frete(
            'Rua Cândido de Azevedo Marques, 750, Morumbi, 05688-020')
    assert len(nominatim) == 1
    assert r['ok'] is True and r['fonte'] == 'brasilapi' and r['impreciso'] is True


def test_nominatim_com_http_de_erro_e_sem_outra_fonte_nao_localiza(app):
    app.config['FRETE_GOOGLE'] = '0'
    nominatim = []

    def fake_get(url, **kw):
        if 'brasilapi' in url:
            return _Resp(200, _BRASILAPI_SEM_COORD)
        nominatim.append(url)
        return _Resp(503, None)

    with patch('app.services.frete.requests.get', side_effect=fake_get):
        r = frete.consultar_frete(
            'Rua Cândido de Azevedo Marques, 750, Morumbi, 05688-020')
    assert len(nominatim) == 1
    assert r == {'ok': False, 'erro': 'nao_encontrado'}


def test_candidato_sem_coordenada_e_pulado(app):
    with patch('app.services.frete.requests.get', return_value=_Resp(200, [
            {'lat': None, 'lon': None, 'display_name': 'sem ponto'},
            {'lat': '-23.61', 'lon': '-46.70', 'display_name': 'com ponto'}])):
        geo = frete._geocodificar_texto('Rua Qualquer, 1')
    assert geo == (-23.61, -46.70, 'com ponto')


def test_retentativa_nao_repete_o_mesmo_texto_no_google(app):
    """Se o endereço oficial montado é o mesmo texto já tentado (mesma chave
    de cache), não há segunda chamada paga."""
    _ligar_google(app, teto=10)
    brasilapi = {'street': 'Rua X', 'neighborhood': 'Bairro',
                 'city': 'São Paulo', 'location': {'coordinates': {}}}

    def fake_get(url, **kw):
        return _Resp(200, brasilapi) if 'brasilapi' in url else _Resp(200, [])

    with patch('app.services.google_maps.geocode_preciso',
               return_value=None) as g, \
         patch('app.services.frete.requests.get', side_effect=fake_get):
        frete.consultar_frete('Rua X, 10, Bairro, São Paulo, 01000-000')
    assert g.call_count == 1


def test_nominatim_com_resposta_fora_do_formato_nao_derruba(app):
    """Nominatim devolvendo objeto de erro (e não lista) fazia o laço
    estourar AttributeError fora do except — o checkout virava 500."""
    app.config['FRETE_GOOGLE'] = '0'
    with patch('app.services.frete.requests.get',
               return_value=_Resp(200, {'error': 'Too many requests'})):
        r = frete.consultar_frete('Rua Qualquer, 1, São Paulo')
    assert r == {'ok': False, 'erro': 'nao_encontrado'}


# ── Cache do Google compartilhado: frete × despacho × rotas ────────────────

def _google_resp(tipo, numero='1822'):
    return _Resp(200, {'status': 'OK', 'results': [{
        'partial_match': False, 'types': ['street_address'],
        'address_components': [{'long_name': numero, 'types': ['street_number']}],
        'geometry': {'location_type': tipo,
                     'location': {'lat': -23.6708, 'lng': -46.6883}}}]})


def test_rotas_gravam_fonte_propria_que_o_frete_nao_trata_como_precisa(app):
    """O geocode das rotas não confere `location_type` (aceita centroide):
    gravado como 'google', o frete cobrava por esse ponto como preciso."""
    from app.services import google_maps
    app.config['GOOGLE_MAPS_API_KEY'] = 'chave-teste'
    with patch.object(google_maps, '_geocode_remoto', return_value=(-23.6, -46.7)):
        assert google_maps.geocode('Rua das Rotas, 10') == (-23.6, -46.7)
        assert google_maps.geocode_em_lote(['Rua do Lote, 20']) == {
            'Rua do Lote, 20': (-23.6, -46.7)}
    for texto in ('Rua das Rotas, 10', 'Rua do Lote, 20'):
        linha = GeocodeCache.query.filter_by(
            chave=google_maps._normalizar_chave(texto)).one()
        assert linha.fonte == google_maps.FONTE_ROTA
        assert google_maps.cache_preciso(texto) == (None, None)


def test_falha_das_rotas_nao_apaga_a_resposta_negativa_do_google(app):
    """Apagar o ZERO_RESULTS do frete fazia o frete pagar de novo pelo mesmo
    texto; o cache de outra fonte (Nominatim antigo) continua sendo limpo."""
    from app.services import google_maps
    app.config['GOOGLE_MAPS_API_KEY'] = 'chave-teste'
    zero = google_maps._normalizar_chave('Rua Inexistente, 1')
    antigo = google_maps._normalizar_chave('Rua Antiga, 2')
    db.session.add_all([
        GeocodeCache(chave=zero, lat=None, lng=None,
                     fonte=google_maps.FONTE_ZERO, criado_em=agora()),
        GeocodeCache(chave=antigo, lat=-23.5, lng=-46.6, fonte='nominatim')])
    db.session.commit()
    with patch.object(google_maps, '_geocode_remoto', return_value=None):
        assert google_maps.geocode('Rua Inexistente, 1') is None
        assert google_maps.geocode_em_lote(['Rua Antiga, 2']) == {'Rua Antiga, 2': None}
    assert GeocodeCache.query.filter_by(chave=zero).one().fonte == google_maps.FONTE_ZERO
    assert GeocodeCache.query.filter_by(chave=antigo).first() is None


def test_despacho_sem_prova_da_porta_nao_desliga_o_google_do_frete(app):
    """GEOMETRIC_CENTER não prova a porta (o despacho recusa), mas é preciso
    para o frete: gravar 'google_aprox' desligava o Google daquele texto."""
    from app.services import google_maps
    app.config['GOOGLE_MAPS_API_KEY'] = 'chave-teste'
    texto = 'Avenida Sabará, 1822, São Paulo'
    with patch.object(google_maps.requests, 'get',
                      return_value=_google_resp('GEOMETRIC_CENTER')):
        assert google_maps.geocode_preciso(texto, numero_entrega='1822') is None
    assert google_maps.cache_preciso(texto) == ('preciso', (-23.6708, -46.6883))
    assert google_maps.cache_preciso(texto, numero_entrega='1822') == (None, None)


def test_despacho_aproximado_continua_negativo_para_o_frete(app):
    from app.services import google_maps
    app.config['GOOGLE_MAPS_API_KEY'] = 'chave-teste'
    texto = 'Avenida Sabará, 1822, São Paulo'
    with patch.object(google_maps.requests, 'get',
                      return_value=_google_resp('APPROXIMATE')):
        assert google_maps.geocode_preciso(texto, numero_entrega='1822') is None
    assert google_maps.cache_preciso(texto) == ('negativo', None)


def test_gravar_cache_do_google_nao_commita_o_chamador(app):
    """Revisão 28/09/2026: a gravação em savepoint fazia `commit` da sessão do
    checkout (e soltava a trava da compra de kit)."""
    from app.services import google_maps
    app.config['GOOGLE_MAPS_API_KEY'] = 'chave-teste'
    db.session.add(AppConfig(key='pendente_do_chamador', value='x'))
    # Pendência ainda não enviada ao banco (é o estado do checkout na hora da
    # cotação). A versão antiga fazia flush + commit dela aqui dentro.
    with db.session.no_autoflush, patch.object(
            google_maps.requests, 'get', return_value=_google_resp('ROOFTOP')):
        assert google_maps.geocode_preciso('Rua Nova, 5') == (-23.6708, -46.6883)
    db.session.rollback()
    assert AppConfig.get('pendente_do_chamador') is None
    assert GeocodeCache.query.filter_by(
        chave=google_maps._normalizar_chave('Rua Nova, 5')).one().fonte == 'google'


# ── Painel e vigia acompanham ──────────────────────────────────────────────

def test_painel_do_sensor_mostra_uso_por_canal(app, owner_user):
    from app.services import frete_sensor
    _ligar_google(app, teto=10, teto_preview=4)
    AppConfig.set('frete_google_dia', f'{hoje().isoformat()}|6|3')
    db.session.commit()
    r = frete_sensor.resumo(7)
    assert r['google_chamadas_hoje'] == 6
    assert r['google_preview_hoje'] == 3 and r['google_teto_preview'] == 4
    assert r['google_teto_dia'] == 10
    c = app.test_client()
    c.post('/auth/login', data={'login': owner_user.login, 'senha': '123'})
    body = c.get('/admin/frete-sensores').get_data(as_text=True)
    assert 'cotação anônima: 3 / 4' in body


def test_vigia_acusa_canario_que_so_resolveu_pelo_cep(app):
    from app.services import site_vigia

    def frete_impreciso(consulta):
        if 'Campinas' in consulta:
            return {'ok': True, 'fora_area': True, 'distancia_km': 90.0}
        if '01050' in consulta:
            return {'ok': True, 'fora_area': False, 'distancia_km': 8.4,
                    'endereco': 'centro de SP', 'impreciso': True,
                    'fonte': 'brasilapi'}
        return {'ok': True, 'fora_area': False, 'distancia_km': 1.0,
                'endereco': 'ok', 'impreciso': False, 'fonte': 'google'}

    with patch('app.services.frete.consultar_frete',
               side_effect=frete_impreciso):
        problemas = site_vigia.checar_frete()
    assert len(problemas) == 1
    assert 'resolveu sem precisão' in problemas[0] and 'brasilapi' in problemas[0]


# ── 3. As envs do frete chegam ao app ───────────────────────────────────────

def _config_recarregado():
    """Carrega uma cópia NOVA do config.py (sem mexer no módulo `config` já
    importado pelo app da sessão), como o boot do gunicorn faria."""
    caminho = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                           'config.py')
    spec = importlib.util.spec_from_file_location('config_teste_frete', caminho)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    app_teste = Flask('config_teste_frete')
    app_teste.config.from_object(modulo.Config)
    return app_teste.config


def test_config_mapeia_as_envs_do_frete(monkeypatch):
    """Mesma armadilha do Spotify: o Flask não absorve env var sozinho — sem
    declarar no config.py, a env do Railway nunca chega ao frete (o teto e o
    kill-switch ficavam presos no padrão do código)."""
    monkeypatch.setenv('FRETE_GOOGLE', '0')
    monkeypatch.setenv('FRETE_GOOGLE_MAX_DIA', '123')
    monkeypatch.setenv('FRETE_SENSOR', '0')
    cfg = _config_recarregado()
    assert cfg['FRETE_GOOGLE'] == '0'
    assert cfg['FRETE_GOOGLE_MAX_DIA'] == '123'
    assert cfg['FRETE_SENSOR'] == '0'


def test_config_mapeia_o_sub_teto_do_preview(monkeypatch):
    monkeypatch.setenv('FRETE_GOOGLE_MAX_DIA_PREVIEW', '40%')
    assert _config_recarregado().get('FRETE_GOOGLE_MAX_DIA_PREVIEW') == '40%'


# ── Integração: quem cota anonimamente usa o canal preview ─────────────────

def test_api_frete_do_checkout_cota_no_canal_preview(app, monkeypatch):
    """A cotação anônima do navegador (/loja/api/frete) é o canal que o
    sub-teto existe pra conter — sem passar o canal, ela comia a vaga do
    POST do checkout."""
    app.config['LOJA_HOSTS'] = 'localhost'
    monkeypatch.setenv('LOJA_VISIVEL', '1')
    vistos = []

    def _consultar(geo, *, canal=frete.CANAL_CHECKOUT):
        vistos.append(canal)
        return {'ok': True, 'fora_area': False, 'distancia_km': 2.0,
                'valor': 10.0, 'fonte': 'google', 'endereco': geo}

    with patch('app.blueprints.loja.routes.frete_svc.consultar_frete',
               side_effect=_consultar):
        resp = app.test_client().post(
            '/loja/api/frete', json={'endereco': 'Rua A, 10', 'cep': '04077-000'})
    assert resp.status_code == 200
    assert vistos == [frete.CANAL_PREVIEW]


def test_bot_cota_frete_no_canal_preview(app):
    """O bot responde cliente anônimo a cada mensagem — mesma cota do
    preview, nunca a do checkout."""
    from app.services import chatbot
    vistos = []

    def _consultar(geo, *, canal=frete.CANAL_CHECKOUT):
        vistos.append(canal)
        return {'ok': True, 'fora_area': False, 'distancia_km': 2.0}

    with patch('app.services.frete.consultar_frete', side_effect=_consultar):
        chatbot._executar_tool('consultar_frete', {'endereco_ou_cep': 'Moema'})
    assert vistos == [frete.CANAL_PREVIEW]


def test_sub_teto_em_percentual_absurdo_nao_derruba_a_cotacao(app):
    """'inf%' no painel do Railway estourava OverflowError fora do except e
    a cotação inteira caía; config torta vira o padrão com WARNING."""
    app.config['FRETE_GOOGLE_MAX_DIA'] = '100'
    app.config['FRETE_GOOGLE_MAX_DIA_PREVIEW'] = 'inf%'
    assert frete.tetos_google() == (100, 60)
