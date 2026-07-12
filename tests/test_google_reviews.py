"""Avaliacoes do Google (Business Profile) — 12/07/2026. A API do Google e
SEMPRE mockada (a integracao e dormente ate OAuth+aprovacao)."""
from unittest.mock import MagicMock, patch


def _conectar(app):
    """Deixa a integracao 'conectada': credenciais + token guardado."""
    from app.extensions import db
    from app.models import AppConfig
    app.config['GOOGLE_OAUTH_CLIENT_ID'] = 'cid'
    app.config['GOOGLE_OAUTH_CLIENT_SECRET'] = 'secret'
    AppConfig.set('google_oauth_token',
                  '{"refresh_token": "r", "access_token": "a", '
                  '"expiry": "2999-01-01T00:00:00"}')
    db.session.commit()


def test_estrelas_e_parse_dt():
    from app.services import google_reviews as gr
    assert gr._ESTRELAS['FIVE'] == 5 and gr._ESTRELAS['ONE'] == 1
    # RFC3339 UTC -> BRT naive (UTC-3)
    dt = gr._parse_dt('2026-07-10T12:00:00Z')
    assert dt is not None and dt.hour == 9 and dt.day == 10
    assert gr._parse_dt('') is None and gr._parse_dt('lixo') is None


def test_disponivel_e_conectado(app):
    from app.services import google_reviews as gr
    with app.app_context():
        assert gr.conectado() is False
        assert gr.disponivel() is False        # sem credencial
        _conectar(app)
        assert gr.conectado() is True
        assert gr.disponivel() is True
        app.config['GOOGLE_REVIEWS'] = '0'     # kill-switch
        assert gr.disponivel() is False


def test_sincronizar_no_op_sem_conexao(app):
    from app.services import google_reviews as gr
    with app.app_context():
        assert gr.sincronizar() == []          # nao conectado = no-op gracioso


def _fake_get_factory():
    """Side-effect de _get: accounts -> locations -> reviews."""
    def fake_get(url, params=None):
        if url.endswith('/v1/accounts'):
            return {'accounts': [{'name': 'accounts/1'}]}
        if url.endswith('/locations'):
            return {'locations': [{'name': 'locations/9', 'title': 'Nebraska'}]}
        if url.endswith('/reviews'):
            return {'reviews': [
                {'reviewId': 'rev-A', 'starRating': 'FIVE',
                 'comment': 'Otimo pao', 'createTime': '2026-07-10T12:00:00Z',
                 'reviewer': {'displayName': 'Ana'}},
                {'reviewId': 'rev-B', 'starRating': 'TWO',
                 'comment': 'Demorou', 'createTime': '2026-07-11T09:00:00Z',
                 'reviewer': {'displayName': 'Bruno'}},
            ]}
        return {}
    return fake_get


def test_sincronizar_idempotente_e_locations(app):
    from app.models import GoogleReview, GoogleReviewLocation
    from app.services import google_reviews as gr
    with app.app_context():
        _conectar(app)
        with patch.object(gr, '_get', side_effect=_fake_get_factory()):
            novas = gr.sincronizar()
        assert len(novas) == 2                             # 2 reviews novas
        assert GoogleReview.query.count() == 2
        loc = GoogleReviewLocation.query.one()
        assert loc.apelido == 'Nebraska'
        assert loc.location_name == 'accounts/1/locations/9'
        # re-sync NAO duplica (idempotente por review_id)
        with patch.object(gr, '_get', side_effect=_fake_get_factory()):
            novas2 = gr.sincronizar()
        assert novas2 == [] and GoogleReview.query.count() == 2


def test_sincronizar_e_alertar_primeiro_sync_nao_alerta(app):
    from app.services import google_reviews as gr
    with app.app_context():
        _conectar(app)
        with patch.object(gr, '_get', side_effect=_fake_get_factory()), \
             patch.object(gr, '_alertar_novas') as alerta:
            r1 = gr.sincronizar_e_alertar()
        assert r1['alertou'] is False                      # 1o sync so importa
        assert not alerta.called
        assert r1['novas'] == 2


def test_sincronizar_e_alertar_segundo_sync_alerta_novas(app):
    from app.services import google_reviews as gr
    with app.app_context():
        _conectar(app)
        # 1o sync (prime)
        with patch.object(gr, '_get', side_effect=_fake_get_factory()):
            gr.sincronizar_e_alertar()

        # 2o sync com 1 review nova
        def fake_get2(url, params=None):
            if url.endswith('/v1/accounts'):
                return {'accounts': [{'name': 'accounts/1'}]}
            if url.endswith('/locations'):
                return {'locations': [{'name': 'locations/9', 'title': 'Nebraska'}]}
            if url.endswith('/reviews'):
                return {'reviews': [
                    {'reviewId': 'rev-C', 'starRating': 'ONE',
                     'comment': 'Ruim', 'createTime': '2026-07-12T09:00:00Z',
                     'reviewer': {'displayName': 'Carla'}}]}
            return {}
        with patch.object(gr, '_get', side_effect=fake_get2), \
             patch('app.services.zapi.enviar_texto') as env:
            app.config['GOOGLE_REVIEWS_NUMERO'] = '5511999999999'
            r2 = gr.sincronizar_e_alertar()
        assert r2['novas'] == 1 and r2['alertou'] is True
        assert env.called
        msg = env.call_args[0][1]
        assert 'Carla' in msg and 'Google' in msg


def test_texto_alerta_prioriza_nota_baixa(app):
    from app.models import GoogleReview
    from app.services import google_reviews as gr
    with app.app_context():
        r5 = GoogleReview(review_id='x1', nota=5, autor='Feliz', comentario='top')
        r1 = GoogleReview(review_id='x2', nota=1, autor='Bravo', comentario='pessimo')
        txt = gr._texto_alerta([r5, r1])
        assert 'nota 1-3' in txt
        # o de nota baixa aparece antes do de nota alta no destaque
        assert txt.index('Bravo') < txt.index('Feliz')


def test_responder_publica_e_espelha_local(app):
    from app.models import GoogleReview
    from app.services import google_reviews as gr
    with app.app_context():
        from app.extensions import db
        _conectar(app)
        rev = GoogleReview(review_id='rev-A',
                           location_name='accounts/1/locations/9', nota=4)
        db.session.add(rev)
        db.session.commit()
        resp = MagicMock(status_code=200)
        with patch('app.services.google_reviews.requests.put',
                   return_value=resp) as put:
            ok, msg = gr.responder(rev.id, 'Obrigado!', user_id=None)
        assert ok is True
        assert put.called and put.call_args.kwargs['json'] == {'comment': 'Obrigado!'}
        assert rev.resposta_texto == 'Obrigado!' and rev.respondida is True


def test_responder_recusa_vazio_e_desconectado(app):
    from app.models import GoogleReview
    from app.services import google_reviews as gr
    with app.app_context():
        from app.extensions import db
        rev = GoogleReview(review_id='rev-Z', nota=3)
        db.session.add(rev)
        db.session.commit()
        ok, _ = gr.responder(rev.id, '   ')          # vazio
        assert ok is False
        ok2, _ = gr.responder(rev.id, 'oi')          # nao conectado
        assert ok2 is False


def test_trocar_codigo_guarda_token(app):
    from app.services import google_reviews as gr
    with app.app_context():
        app.config['GOOGLE_OAUTH_CLIENT_ID'] = 'cid'
        app.config['GOOGLE_OAUTH_CLIENT_SECRET'] = 'secret'
        resp = MagicMock(status_code=200)
        resp.json.return_value = {'access_token': 'AT', 'refresh_token': 'RT',
                                  'expires_in': 3600, 'scope': gr._SCOPE}
        with patch('app.services.google_reviews.requests.post',
                   return_value=resp):
            ok, _ = gr.trocar_codigo('code123', 'https://x/cb')
        assert ok is True and gr.conectado() is True
        assert gr._token_state()['refresh_token'] == 'RT'


def test_rascunho_ia_mockado(app):
    from app.models import GoogleReview
    from app.services import google_reviews as gr
    with app.app_context():
        from app.extensions import db
        rev = GoogleReview(review_id='rev-D', nota=2, autor='Dani',
                           comentario='faltou atendimento')
        db.session.add(rev)
        db.session.commit()
        fake = MagicMock()
        bloco = MagicMock(type='text', text='Oi Dani, sentimos muito...')
        fake.content = [bloco]
        fake.usage = MagicMock(input_tokens=10, output_tokens=20,
                               cache_read_input_tokens=0,
                               cache_creation_input_tokens=0)
        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'k'}), \
             patch('anthropic.Anthropic') as Cli:
            Cli.return_value.messages.create.return_value = fake
            texto, msg = gr.rascunho_resposta(rev.id)
        assert texto and 'Dani' in texto


def test_painel_owner_e_anonimo(app, owner_user):
    c = app.test_client()
    resp_anon = c.get('/admin/avaliacoes-google')
    assert resp_anon.status_code != 200                # owner_required barra
    c.post('/auth/login', data={'login': owner_user.login, 'senha': '123'})
    resp = c.get('/admin/avaliacoes-google')
    assert resp.status_code == 200
    assert 'Avaliações do Google' in resp.get_data(as_text=True)
