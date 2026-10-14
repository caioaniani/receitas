"""Uma corrida Lalamove por pedido (14/10/2026, dono: "às vezes ao pedir
motoboy Lalamove ele multiplica e chama 2 motoboys para o mesmo pedido").

Portas fechadas: cotação reutilizada / em voo (claim atômico), segunda
cotação do mesmo pedido com corrida ativa, Lalamove sem resposta (a corrida
pode existir — bloqueia até liberar) e erro depois da corrida criada (antes
virava 500 e a equipe chamava de novo)."""
from datetime import timedelta
from unittest.mock import patch

import requests

from app.extensions import db
from app.models import LalamoveEntrega
from app.utils import agora, hoje

COTAR = '/entregas/api/painel/lalamove/cotar'
CHAMAR = '/entregas/api/painel/lalamove/chamar'
LIBERAR = '/entregas/api/painel/lalamove/liberar'

ORDEM = {'ok': True, 'order_id': 'O-1', 'status': 'ASSIGNING_DRIVER',
         'share_link': 'https://share/x', 'valor': '20.00', 'moeda': 'BRL'}


def _config(app):
    app.config['LALAMOVE_API_KEY'] = 'pk_test_chave'
    app.config['LALAMOVE_API_SECRET'] = 'sk_test_segredo'
    app.config['LALAMOVE_REMETENTE_FONE'] = '11999990000'
    app.config['LALAMOVE_ORIGEM_LATLNG'] = '-23.6095,-46.6907'


def _cliente(app):
    _config(app)
    c = app.test_client()
    c.post('/auth/login', data={'login': 'admin', 'senha': '123'})
    return c


def _cotacao(code='PED-1', status='cotacao', order_id=None, **kw):
    e = LalamoveEntrega(pedido_code=code, data_ref=hoje(), quotation_id='Q',
                        sender_stop_id='S0', recipient_stop_id='S1',
                        status=status, order_id=order_id, service_type='LALAGO',
                        valor=20, endereco_destino='Rua X, 1', **kw)
    db.session.add(e)
    db.session.commit()
    return e.id


def _cotar(c, code='PED-1'):
    cot = {'ok': True, 'quotation_id': 'Q', 'valor': '20.00', 'moeda': 'BRL',
           'distancia_m': 3000, 'sender_stop_id': 'S0', 'recipient_stop_id': 'S1',
           'service_type': 'LALAGO'}
    with patch('app.services.lalamove.cotar', return_value=cot) as m:
        r = c.post(COTAR, json={'code': code, 'endereco': 'Rua X, 1',
                                'destinatario': 'Ana', 'telefone': '11988887777'})
    return r, m


def test_segunda_cotacao_do_mesmo_pedido_nao_chama_segundo_motoboy(app, admin_user):
    c = _cliente(app)
    a, b = _cotacao(), _cotacao()
    with patch('app.services.lalamove.criar_ordem', return_value=ORDEM) as m:
        assert c.post(CHAMAR, json={'entrega_id': a}).get_json()['ok'] is True
        r = c.post(CHAMAR, json={'entrega_id': b})
    assert r.status_code == 409
    assert 'cancele a corrida atual' in r.get_json()['erro']
    assert m.call_count == 1
    assert db.session.get(LalamoveEntrega, b).status == 'cotacao'


def test_cotacao_em_voo_nao_e_chamada_de_novo(app, admin_user):
    """Duplo clique / dois aparelhos com a MESMA cotação: a 2ª requisição
    chega com a linha já reivindicada ('chamando')."""
    c = _cliente(app)
    eid = _cotacao(status='chamando', atualizado_em=agora())
    with patch('app.services.lalamove.criar_ordem', return_value=ORDEM) as m:
        r = c.post(CHAMAR, json={'entrega_id': eid})
    assert r.status_code == 409
    assert 'sendo chamado agora' in r.get_json()['erro']
    m.assert_not_called()


def test_cotar_recusa_quando_ha_corrida_ativa(app, admin_user):
    c = _cliente(app)
    _cotacao(status='ASSIGNING_DRIVER', order_id='O-9')
    r, m = _cotar(c)
    assert r.status_code == 409
    assert r.get_json()['lalamove']['status'] == 'ASSIGNING_DRIVER'
    m.assert_not_called()


def test_corrida_cancelada_nao_impede_chamar_outra(app, admin_user):
    c = _cliente(app)
    _cotacao(status='CANCELED', order_id='O-velha')
    r, _ = _cotar(c)
    assert r.status_code == 200
    with patch('app.services.lalamove.criar_ordem', return_value=ORDEM):
        assert c.post(CHAMAR, json={'entrega_id': r.get_json()['entrega_id']}
                      ).get_json()['ok'] is True


def test_lalamove_sem_resposta_bloqueia_ate_liberar(app, admin_user):
    c = _cliente(app)
    eid = _cotacao()
    with patch('app.services.lalamove._request',
               side_effect=requests.Timeout('read timed out')):
        r = c.post(CHAMAR, json={'entrega_id': eid})
    d = r.get_json()
    assert r.status_code == 502 and d['incerto'] is True
    assert 'PODE ter sido criada' in d['erro']
    assert d['lalamove']['pode_liberar'] is True
    assert db.session.get(LalamoveEntrega, eid).status == 'sem_resposta'

    # nem cota nem chama outra enquanto ninguém conferiu no app
    r2, m = _cotar(c)
    assert r2.status_code == 409 and 'Confira no app' in r2.get_json()['erro']
    m.assert_not_called()
    outra = _cotacao()
    with patch('app.services.lalamove.criar_ordem', return_value=ORDEM) as m2:
        assert c.post(CHAMAR, json={'entrega_id': outra}).status_code == 409
    m2.assert_not_called()

    # conferido: liberar destrava
    r3 = c.post(LIBERAR, json={'entrega_id': eid})
    assert r3.get_json()['ok'] is True
    assert r3.get_json()['lalamove']['encerrada'] is True
    with patch('app.services.lalamove.criar_ordem', return_value=ORDEM):
        assert c.post(CHAMAR, json={'entrega_id': outra}).get_json()['ok'] is True


def test_erro_5xx_e_incerto_e_4xx_devolve_a_cotacao(app, admin_user):
    from app.services import lalamove as lala_svc
    _config(app)
    with patch('app.services.lalamove._request', return_value=(503, {})):
        r = lala_svc.criar_ordem('Q', 'S0', 'S1', 'Ana', '11988887777')
    assert r['ok'] is False and r['incerto'] is True
    with patch('app.services.lalamove._request',
               return_value=(422, {'errors': [{'id': 'ERR_QUOTATION_EXPIRED'}]})):
        r = lala_svc.criar_ordem('Q', 'S0', 'S1', 'Ana', '11988887777')
    assert r['ok'] is False and r['incerto'] is False

    c = _cliente(app)
    eid = _cotacao()
    with patch('app.services.lalamove.criar_ordem',
               return_value={'ok': False, 'incerto': False, 'erro': 'cotação expirada'}):
        assert c.post(CHAMAR, json={'entrega_id': eid}).status_code == 502
    assert db.session.get(LalamoveEntrega, eid).status == 'cotacao'
    with patch('app.services.lalamove.criar_ordem', return_value=ORDEM):
        assert c.post(CHAMAR, json={'entrega_id': eid}).get_json()['ok'] is True


def test_erro_depois_da_corrida_criada_vira_aviso_nunca_500(app, admin_user):
    """Saída da produção recusada (caso do kit, 12/10) estourava 500 com a
    corrida já criada: o painel dizia "sem conexão" e a equipe chamava de
    novo."""
    c = _cliente(app)
    eid = _cotacao()
    with patch('app.services.lalamove.criar_ordem', return_value=ORDEM), \
         patch('app.services.loja_entrega.avancar_status_entrega',
               side_effect=ValueError('composição sem vínculo')):
        r = c.post(CHAMAR, json={'entrega_id': eid})
    d = r.get_json()
    assert r.status_code == 200 and d['ok'] is True
    assert 'NÃO passou para "a caminho"' in d['aviso']
    assert d['lalamove']['status'] == 'ASSIGNING_DRIVER'
    assert db.session.get(LalamoveEntrega, eid).order_id == 'O-1'
    r2, _ = _cotar(c)
    assert r2.status_code == 409


def test_chamando_orfa_vira_sem_resposta_e_fresca_nao_libera(app, admin_user):
    c = _cliente(app)
    fresca = _cotacao(code='PED-A', status='chamando', atualizado_em=agora())
    assert c.post(LIBERAR, json={'entrega_id': fresca}).status_code == 409
    orfa = _cotacao(code='PED-B', status='chamando',
                    atualizado_em=agora() - timedelta(minutes=10))
    from app.blueprints.entregas.routes import _lalamove_por_code
    resumo = _lalamove_por_code(['PED-B'])['PED-B']
    assert resumo['status'] == 'sem_resposta' and resumo['pode_liberar'] is True
    assert c.post(LIBERAR, json={'entrega_id': orfa}).get_json()['ok'] is True


def test_card_mostra_a_corrida_ativa_e_a_chamada_sem_resposta(app, admin_user):
    from app.blueprints.entregas.routes import _lalamove_por_code
    _cotacao(code='PED-1', status='ON_GOING', order_id='O-ativa')
    _cotacao(code='PED-1', status='CANCELED', order_id='O-cancelada-depois')
    _cotacao(code='PED-2', status='sem_resposta')
    _cotacao(code='PED-3', status='cotacao')
    out = _lalamove_por_code(['PED-1', 'PED-2', 'PED-3'])
    assert out['PED-1']['status'] == 'ON_GOING'
    assert out['PED-2']['status'] == 'sem_resposta'
    assert 'PED-3' not in out


def test_front_trava_chamada_em_voo_e_tem_liberar():
    src = open('app/templates/entregas/painel_pedidos.html', encoding='utf-8').read()
    assert 'lalaChamando' in src
    assert "data-lala=\"liberar\"" in src or 'data-lala="liberar"' in src
    assert 'LALA_LIBERAR' in src
    assert 'a corrida PODE ter sido criada' in src


def test_sonda_lista_pedidos_com_duas_corridas(app):
    app.config['CLAUDE_API_TOKEN'] = 'tok'
    _cotacao(code='PED-D', status='ASSIGNING_DRIVER', order_id='O-a')
    _cotacao(code='PED-D', status='ON_GOING', order_id='O-b')
    _cotacao(code='PED-U', status='COMPLETED', order_id='O-c')
    _cotacao(code='PED-R', status='CANCELED', order_id='O-d')     # rechamada legítima
    _cotacao(code='PED-R', status='ON_GOING', order_id='O-e')
    _cotacao(code='PED-S', status='descartada')                    # sem número
    _cotacao(code='PED-S', status='ASSIGNING_DRIVER', order_id='O-f')
    d = app.test_client().get('/api/claude/lalamove-corridas?dias=3',
                              headers={'Authorization': 'Bearer tok'}).get_json()
    assert d['ok'] is True
    dup = {x['pedido']: x for x in d['duplicados']}
    assert set(dup) == {'PED-D', 'PED-R', 'PED-S'}
    assert dup['PED-D']['sobreposta'] is True and dup['PED-D']['corridas'] == 2
    assert dup['PED-R']['sobreposta'] is False
    assert dup['PED-S']['sobreposta'] is False


def test_resposta_sem_order_id_e_incerta(app):
    from app.services import lalamove as lala_svc
    _config(app)
    with patch('app.services.lalamove._request', return_value=(201, {'data': {}})):
        r = lala_svc.criar_ordem('Q', 'S0', 'S1', 'Ana', '11988887777')
    assert r['ok'] is False and r['incerto'] is True
    with patch('app.services.lalamove._request', return_value=(200, {'raw': 'oops'})):
        assert lala_svc.criar_ordem('Q', 'S0', 'S1', 'Ana', '11988887777')['incerto'] is True


def test_sem_conexao_nao_e_incerto_e_devolve_a_cotacao(app, admin_user):
    c = _cliente(app)
    eid = _cotacao()
    with patch('app.services.lalamove._request',
               side_effect=requests.ConnectTimeout('connect timed out')):
        r = c.post(CHAMAR, json={'entrega_id': eid})
    assert r.status_code == 502 and not r.get_json().get('incerto')
    assert db.session.get(LalamoveEntrega, eid).status == 'cotacao'


def test_webhook_perdido_e_reconciliado_antes_de_recusar(app, admin_user):
    """Corrida expirou na Lalamove e o webhook não chegou: a linha seguia
    ASSIGNING_DRIVER e travaria o pedido para sempre."""
    c = _cliente(app)
    viva = _cotacao(status='ASSIGNING_DRIVER', order_id='O-perdida')
    with patch('app.services.lalamove.detalhes',
               return_value={'ok': True, 'status': 'EXPIRED'}) as det:
        r, _ = _cotar(c)
    det.assert_called_once_with('O-perdida')
    assert r.status_code == 200
    assert db.session.get(LalamoveEntrega, viva).status == 'EXPIRED'
    # Lalamove confirma viva (ou fora do ar): continua bloqueando.
    _cotacao(code='PED-2', status='ON_GOING', order_id='O-viva')
    with patch('app.services.lalamove.detalhes',
               return_value={'ok': True, 'status': 'ON_GOING'}):
        assert _cotar(c, 'PED-2')[0].status_code == 409
    with patch('app.services.lalamove.detalhes', side_effect=requests.Timeout()):
        assert _cotar(c, 'PED-2')[0].status_code == 409


def test_entregue_nao_bloqueia_reentrega(app, admin_user):
    c = _cliente(app)
    _cotacao(status='COMPLETED', order_id='O-entregue')
    assert _cotar(c)[0].status_code == 200


def test_desfecho_tardio_nao_sobrescreve_liberacao_mas_corrida_criada_grava(app):
    from app.blueprints.entregas.routes import _finalizar_chamada
    eid = _cotacao(status='descartada')
    assert _finalizar_chamada(eid, {'status': 'cotacao'}) is True
    assert db.session.get(LalamoveEntrega, eid).status == 'descartada'
    assert _finalizar_chamada(eid, {'order_id': 'O-tarde', 'status': 'ASSIGNING_DRIVER'},
                              corrida_criada=True) is True
    e = db.session.get(LalamoveEntrega, eid)
    assert e.order_id == 'O-tarde' and e.status == 'ASSIGNING_DRIVER'


def test_falha_ao_gravar_corrida_criada_ainda_grava_o_numero(app):
    from app.blueprints.entregas.routes import _finalizar_chamada
    eid = _cotacao(status='chamando', atualizado_em=agora())
    real = db.session.commit
    chamadas = {'n': 0}

    def commit_falha_uma_vez():
        chamadas['n'] += 1
        if chamadas['n'] == 1:
            raise RuntimeError('blip do banco')
        return real()
    with patch.object(db.session, 'commit', side_effect=commit_falha_uma_vez):
        ok = _finalizar_chamada(eid, {'order_id': 'O-x', 'status': 'ASSIGNING_DRIVER',
                                      'share_link': 's'}, corrida_criada=True)
    assert ok is False
    e = db.session.get(LalamoveEntrega, eid)
    assert e.order_id == 'O-x' and e.status == 'ASSIGNING_DRIVER'


def test_id_gigante_e_400(app, admin_user):
    c = _cliente(app)
    assert c.post(CHAMAR, json={'entrega_id': 10 ** 12}).status_code == 400
    assert c.post(LIBERAR, json={'entrega_id': -1}).status_code == 400


def test_sonda_conferir_pergunta_a_lalamove_sem_gravar(app):
    app.config['CLAUDE_API_TOKEN'] = 'tok'
    vivo = _cotacao(code='PED-V', status='ASSIGNING_DRIVER', order_id='O-v')
    _cotacao(code='PED-F', status='COMPLETED', order_id='O-f')   # fora: nem dup nem vivo
    with patch('app.services.lalamove.detalhes',
               return_value={'ok': True, 'status': 'EXPIRED', 'driver_id': None}) as det:
        d = app.test_client().get('/api/claude/lalamove-corridas?dias=3&conferir=1',
                                  headers={'Authorization': 'Bearer tok'}).get_json()
    det.assert_called_once_with('O-v')
    assert d['conferidas'] == [{'pedido': 'PED-V', 'order_id': 'O-v',
                                'status_aqui': 'ASSIGNING_DRIVER',
                                'status_lalamove': 'EXPIRED', 'driver_id': None,
                                'erro': None}]
    assert db.session.get(LalamoveEntrega, vivo).status == 'ASSIGNING_DRIVER'
