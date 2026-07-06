"""Vigia do site (05/07/2026) — canários de frete/catálogo/agenda + alerta.

Nasceu no incidente do frete: BrasilAPI sem coordenadas + geocode caindo em
rua homônima = R$ 95 pra vizinho e Centro bloqueado, sem ninguém reclamar
(cliente só abandonava). O vigia roda os mesmos serviços do checkout e
alerta o dono no WhatsApp na transição saudável→doente.
"""
from unittest.mock import patch

from app.extensions import db
from app.models import AppConfig, Produto


def _frete_ok(consulta):
    """Stub do consultar_frete que responde certo pra cada canário."""
    if 'Campinas' in consulta:
        return {'ok': True, 'fora_area': True, 'distancia_km': 90.0}
    if 'Ribeiro do Vale' in consulta:
        return {'ok': True, 'fora_area': False, 'distancia_km': 0.1,
                'endereco': 'padaria'}
    if 'Nova York' in consulta:
        return {'ok': True, 'fora_area': False, 'distancia_km': 2.1,
                'endereco': 'Brooklin Novo'}
    return {'ok': True, 'fora_area': False, 'distancia_km': 7.4,
            'endereco': 'República'}


def _produto_site():
    p = Produto(nome='Cesta Vigia', categoria='Teste', ativo=True,
                preco_site=50.0)
    db.session.add(p)
    db.session.commit()
    return p


def test_saudavel_quando_tudo_ok(app):
    from app.services import site_vigia
    with app.app_context():
        _produto_site()
        with patch('app.services.frete.consultar_frete',
                   side_effect=_frete_ok):
            out = site_vigia.rodar_checks()
        assert out['saudavel'] is True, out['problemas']


def test_canario_pega_geocode_em_outra_cidade(app):
    """O caso do incidente: vizinho da padaria geocodificado a 19,3 km."""
    from app.services import site_vigia

    def frete_ruim(consulta):
        r = _frete_ok(consulta)
        if 'Nova York' in consulta:
            r = {'ok': True, 'fora_area': False, 'distancia_km': 19.3,
                 'endereco': 'Rua Nova York, Grajaú'}
        return r

    with app.app_context():
        _produto_site()
        with patch('app.services.frete.consultar_frete',
                   side_effect=frete_ruim):
            out = site_vigia.rodar_checks()
        assert out['saudavel'] is False
        assert any('faixa esperada' in p and 'Grajaú' in p
                   for p in out['problemas'])


def test_canario_pega_bloqueio_indevido_e_area_furada(app):
    """Dentro virando 'fora da área' (caso D Lucas) e o inverso (Campinas
    passando) são acusados."""
    from app.services import site_vigia

    def frete_ruim(consulta):
        r = _frete_ok(consulta)
        if '01050' in consulta:
            r = {'ok': True, 'fora_area': True, 'distancia_km': 44.2,
                 'endereco': 'Arujá'}
        if 'Campinas' in consulta:
            r = {'ok': True, 'fora_area': False, 'distancia_km': 3.0,
                 'endereco': 'errado'}
        return r

    with app.app_context():
        _produto_site()
        with patch('app.services.frete.consultar_frete',
                   side_effect=frete_ruim):
            out = site_vigia.rodar_checks()
        acusa = ' | '.join(out['problemas'])
        assert 'esperado DENTRO' in acusa
        assert 'esperado FORA' in acusa


def test_catalogo_vazio_e_problema(app):
    from app.services import site_vigia
    with app.app_context():
        Produto.query.update({'ativo': False})
        db.session.commit()
        with patch('app.services.frete.consultar_frete',
                   side_effect=_frete_ok):
            out = site_vigia.rodar_checks()
        assert any('catálogo do site vazio' in p for p in out['problemas'])


def test_vigiar_alerta_na_transicao_e_avisa_recuperacao(app):
    """Doente → 1 alerta WhatsApp (não re-spamma no ciclo seguinte);
    saudável de novo → aviso de normalização e estado limpo."""
    from app.services import site_vigia
    with app.app_context():
        app.config['ZAPI_BOT_DONO_NUMERO'] = '5511999999999'
        _produto_site()

        def frete_ruim(consulta):
            r = _frete_ok(consulta)
            if 'Nova York' in consulta:
                r = {'ok': True, 'fora_area': False, 'distancia_km': 19.3,
                     'endereco': 'Grajaú'}
            return r

        with patch('app.services.zapi.enviar_texto') as tx, \
                patch('app.services.frete.consultar_frete',
                      side_effect=frete_ruim):
            r1 = site_vigia.vigiar()
            r2 = site_vigia.vigiar()
        assert r1['tipo'] == 'alerta' and r1['enviado'] is True
        assert r2['tipo'] == 'alerta_suprimido'      # mesmo problema < 6h
        assert tx.call_count == 1
        assert 'Vigia do SITE' in tx.call_args[0][1]

        with patch('app.services.zapi.enviar_texto') as tx2, \
                patch('app.services.frete.consultar_frete',
                      side_effect=_frete_ok):
            r3 = site_vigia.vigiar()
        assert r3['tipo'] == 'recuperacao'
        assert 'normalizou' in tx2.call_args[0][1]
        assert AppConfig.get('site_vigia_quebrado_desde') is None


def test_rota_owner_roda_checks(app, owner_user):
    from app.models import Produto as _P
    with app.app_context():
        db.session.add(_P(nome='Cesta Rota Vigia', categoria='T', ativo=True,
                          preco_site=10.0))
        db.session.commit()
        c = app.test_client()
        with c.session_transaction() as sess:
            sess['_user_id'] = str(owner_user.id)
            sess['_fresh'] = True
        with patch('app.services.frete.consultar_frete',
                   side_effect=_frete_ok):
            resp = c.get('/admin/vigia-site')
        assert resp.status_code == 200
        assert resp.get_json()['saudavel'] is True
