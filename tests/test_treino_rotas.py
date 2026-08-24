"""Sistema gamificado — camada web (§12). Smoke: telas renderizam sem 500;
verificação pública de certificado; critério 18 (desligado).
"""
from datetime import timedelta

from app.extensions import db
from app.models import (
    Funcionario,
    Loja,
    TreinoSelo,
    TreinoTemporada,
    TreinoTrilha,
    TreinoVideo,
    Usuario,
)
from app.services import treino_ledger as ledger
from app.utils import hoje


def _login(app, usuario_id):
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(usuario_id)
        s['_fresh'] = True
    return c


def _temp():
    t = TreinoTemporada(nome='T', inicio=hoje() - timedelta(days=1),
                        fim=hoje() + timedelta(days=30), status='ATIVA')
    db.session.add(t)
    db.session.commit()
    return t


def _func_logado(app, papel='funcionario', ativo=True):
    loja = Loja(nome='Brooklin', ativa=True)
    u = Usuario(nome='Ana', login=f'ana-{papel}', papel=papel)
    u.set_senha('x' * 8)
    db.session.add_all([loja, u])
    db.session.commit()
    f = Funcionario(nome='Ana', cpf='2', ativo=ativo, usuario_id=u.id)
    f.lojas.append(loja)
    db.session.add(f)
    db.session.commit()
    return u, f, loja


def test_telas_admin_renderizam(app, admin_user):
    with app.app_context():
        _temp()
        db.session.add(TreinoTrilha(nome='Seg'))
        db.session.commit()
    c = _login(app, admin_user.id)
    for rota in ['/treino/', '/treino/ranking', '/treino/recompensas',
                 '/treino/gestor/', '/treino/gestor/resgates', '/treino/admin/']:
        assert c.get(rota).status_code == 200, rota


def test_funcionario_ve_trilha_e_video(app):
    with app.app_context():
        _temp()
        u, f, _ = _func_logado(app)
        trilha = TreinoTrilha(nome='Seg')
        db.session.add(trilha)
        db.session.commit()
        v = TreinoVideo(trilha_id=trilha.id, titulo='Aula 1',
                        duracao_segundos=60)
        db.session.add(v)
        db.session.commit()
        uid, tid, vid = u.id, trilha.id, v.id
    c = _login(app, uid)
    assert c.get('/treino/').status_code == 200
    assert c.get(f'/treino/trilha/{tid}').status_code == 200
    assert c.get(f'/treino/video/{vid}').status_code == 200
    assert c.get('/treino/extrato').status_code == 200


def test_video_abre_com_progresso_salvo(app):
    """Vídeo já concluído abre mostrando o progresso salvo, não 0%."""
    from app.models import TreinoProgressoVideo
    from app.utils import agora
    with app.app_context():
        _temp()
        u, f, _ = _func_logado(app)
        trilha = TreinoTrilha(nome='Seg')
        db.session.add(trilha)
        db.session.commit()
        v = TreinoVideo(trilha_id=trilha.id, titulo='A', duracao_segundos=100)
        db.session.add(v)
        db.session.commit()
        db.session.add(TreinoProgressoVideo(
            funcionario_id=f.id, video_id=v.id, versao_video=v.versao,
            percentual=100, concluido_em=agora()))
        db.session.commit()
        uid, vid = u.id, v.id
    c = _login(app, uid)
    body = c.get(f'/treino/video/{vid}').get_data(as_text=True)
    assert 'width:100%' in body and 'Concluído' in body


def test_video_sem_fullscreen_no_celular(app):
    """Celular: bloqueia fullscreen também por Permissions Policy; desktop
    mantém a opção e aguarda a saída antes de mostrar a pergunta."""
    from unittest.mock import patch
    with app.app_context():
        _temp()
        u, f, _ = _func_logado(app)
        trilha = TreinoTrilha(nome='Seg')
        db.session.add(trilha)
        db.session.commit()
        v = TreinoVideo(trilha_id=trilha.id, titulo='A', duracao_segundos=100,
                        video_externo_id='a' * 32, provedor='cloudflare')
        db.session.add(v)
        db.session.commit()
        uid, vid = u.id, v.id
    c = _login(app, uid)
    with patch('app.services.treinamento_stream.embed_url',
               return_value='https://x.cloudflarestream.com/abc/iframe'):
        # desktop: UA padrão do test client não casa mobile → mantém fullscreen
        resp_d = c.get(f'/treino/video/{vid}')
        body_d = resp_d.get_data(as_text=True)
        iframe_d = body_d.split('<iframe id="vf"', 1)[1].split('</iframe>', 1)[0]
        assert 'allowfullscreen="true"' in iframe_d
        assert '?controls=false' not in iframe_d
        assert 'fullscreen=()' not in resp_d.headers.get('Permissions-Policy', '')
        assert 'sairFullscreen' in body_d          # exit-fullscreen segue no desktop
        assert 'sairFullscreen().then' in body_d   # overlay espera a saída terminar
        # celular (iPhone): sem allowfullscreen + aviso
        resp_m = c.get(f'/treino/video/{vid}', headers={
            'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)'
        })
        body_m = resp_m.get_data(as_text=True)
    iframe_m = body_m.split('<iframe id="vf"', 1)[1].split('</iframe>', 1)[0]
    assert 'allowfullscreen="true"' not in iframe_m
    assert "fullscreen 'none'" in iframe_m
    assert '?controls=false' in iframe_m
    assert 'class="mobile-locked"' in body_m
    assert 'id="mobile-play"' in body_m and 'id="mobile-back"' in body_m
    assert 'player.controls=false' in body_m
    assert '#playerwrap.mobile-locked{padding-top:min(66.6667%,480px);}' in body_m
    assert '#playerwrap.mobile-locked #vf{pointer-events:none;}' in body_m
    assert 'fullscreen=()' in resp_m.headers['Permissions-Policy']
    assert 'A tela cheia foi removida' in body_m


def test_funcionario_nao_recebe_player_antes_do_cloudflare_pronto(
        app, admin_user):
    from unittest.mock import patch
    with app.app_context():
        trilha = TreinoTrilha(nome='Seg')
        db.session.add(trilha)
        db.session.flush()
        v = TreinoVideo(
            trilha_id=trilha.id, titulo='Aguardando', ativo=True,
            video_externo_id='a' * 32, duracao_segundos=0)
        db.session.add(v)
        db.session.commit()
        vid = v.id
    c = _login(app, admin_user.id)
    with patch('app.services.treinamento_stream.status', return_value={
            'pronto': False, 'pct': 20, 'duracao': 0, 'erro': None}), patch(
            'app.services.treinamento_stream.embed_url',
            return_value='https://x.cloudflarestream.com/abc/iframe'):
        body = c.get(f'/treino/video/{vid}').get_data(as_text=True)
    assert 'Vídeo em preparação' in body
    assert 'https://x.cloudflarestream.com/abc/iframe' not in body


def test_verificacao_publica_de_certificado(app):   # §11
    with app.app_context():
        _temp()
        _, f, _ = _func_logado(app)
        trilha = TreinoTrilha(nome='Seg')
        db.session.add(trilha)
        db.session.commit()
        selo = TreinoSelo(funcionario_id=f.id, trilha_id=trilha.id,
                          carga_horaria_minutos=30)
        db.session.add(selo)
        db.session.commit()
        codigo = selo.codigo_verificacao
    c = app.test_client()      # rota PÚBLICA (sem login)
    ok = c.get(f'/treino/verificar/{codigo}')
    assert ok.status_code == 200 and 'válido' in ok.get_data(as_text=True)
    ruim = c.get('/treino/verificar/naoexiste')
    assert ruim.status_code == 200 and 'inválido' in ruim.get_data(as_text=True)


def test_desligado_mantem_certificado_e_sai_do_ativo(app):   # critério 18
    with app.app_context():
        temp = _temp()
        u, f, loja = _func_logado(app, ativo=False)   # DESLIGADO
        trilha = TreinoTrilha(nome='Seg')
        db.session.add(trilha)
        db.session.commit()
        selo = TreinoSelo(funcionario_id=f.id, trilha_id=trilha.id,
                          carga_horaria_minutos=30)
        db.session.add(selo)
        db.session.commit()
        # histórico preservado
        ledger.creditar(f, 'AJUSTE_MANUAL', 50, temporada=temp)
        assert ledger.saldo(f.id, temp.id) == 50
        # não conta como ativo da unidade
        from app.services import treino_ranking as rk
        assert rk._ativos_por_unidade(loja.id) == 0
        codigo = selo.codigo_verificacao
    # certificado ainda válido publicamente
    assert 'válido' in app.test_client().get(
        f'/treino/verificar/{codigo}').get_data(as_text=True)
