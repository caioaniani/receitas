"""Módulo de treinamento — AUTORIA (admin): criar treinamento, montar quiz,
subir e servir vídeo. A fase do funcionário (assistir + responder) vem depois.
"""

from app.extensions import db
from app.models import Treinamento, TreinamentoPergunta, Usuario


def _admin(app, admin_user):
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(admin_user.id)
        s['_fresh'] = True
    return c


def test_admin_exige_admin(app):
    with app.app_context():
        u = Usuario(nome='F', login='func-treino', papel='funcionario')
        u.set_senha('x' * 8)
        db.session.add(u)
        db.session.commit()
        uid = u.id
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(uid)
        s['_fresh'] = True
    assert c.get('/treinamento/admin').status_code == 403


def test_criar_treinamento_nasce_rascunho(app, admin_user):
    """Nasce ativo=False — não vaza pro funcionário nem trava elegibilidade
    até o admin subir vídeo/quiz e publicar."""
    c = _admin(app, admin_user)
    r = c.post('/treinamento/admin/novo', data={'titulo': 'Higiene'})
    assert r.status_code == 302
    with app.app_context():
        t = Treinamento.query.filter_by(titulo='Higiene').first()
        assert t and t.ativo is False and t.nota_minima == 70


def test_video_de_inativo_bloqueia_funcionario(app, tmp_path):
    # Só requests de FUNCIONÁRIO nesta função (a conftest cacheia _login_user
    # por contexto de app — misturar admin+func no mesmo teste vaza a sessão).
    app.config['TREINAMENTO_MEDIA_DIR'] = str(tmp_path)
    from io import BytesIO

    from werkzeug.datastructures import FileStorage

    from app.services import treinamento_video as tv
    with app.app_context():
        t = Treinamento(titulo='Rasc', ativo=False)
        db.session.add(t)
        db.session.commit()
        ref = tv.salvar_video(
            FileStorage(stream=BytesIO(b'0123456789'), filename='a.mp4'), t.id)
        t.video_tipo, t.video_ref = 'arquivo', ref
        db.session.commit()
        tid = t.id
        u = Usuario(nome='F2', login='f2-inat', papel='funcionario')
        u.set_senha('x' * 8)
        db.session.add(u)
        db.session.commit()
        uid = u.id
    cf = app.test_client()
    with cf.session_transaction() as s:
        s['_user_id'] = str(uid)
        s['_fresh'] = True
    assert cf.get(f'/treinamento/video/{tid}').status_code == 404


def test_video_de_inativo_admin_pre_visualiza(app, admin_user, tmp_path):
    app.config['TREINAMENTO_MEDIA_DIR'] = str(tmp_path)
    with app.app_context():
        t = Treinamento(titulo='Rasc2', ativo=False)
        db.session.add(t)
        db.session.commit()
        tid = t.id
    ca = _admin(app, admin_user)
    ca.post(f'/treinamento/admin/{tid}/video?nome=a.mp4',
            data=b'0123456789', content_type='video/mp4')
    assert ca.get(f'/treinamento/video/{tid}').status_code in (200, 206)


def test_add_pergunta_correta_fica_no_slot_certo(app, admin_user):
    """Slot vazio no meio NÃO pode deslocar a marcação da correta."""
    with app.app_context():
        t = Treinamento(titulo='T')
        db.session.add(t)
        db.session.commit()
        tid = t.id
    c = _admin(app, admin_user)
    # slot 0 vazio; slots 1 e 2 preenchidos; a correta é o slot 2 ('Sim').
    c.post(f'/treinamento/admin/{tid}/pergunta', data={
        'enunciado': 'Lavar as mãos?',
        'opcao[]': ['', 'Não', 'Sim', ''],
        'correta': '2',
    })
    with app.app_context():
        p = TreinamentoPergunta.query.filter_by(treinamento_id=tid).first()
        assert p is not None
        assert [(o.texto, o.correta) for o in p.opcoes] == [
            ('Não', False), ('Sim', True)]


def test_pergunta_sem_correta_marcada_recusa(app, admin_user):
    with app.app_context():
        t = Treinamento(titulo='T2')
        db.session.add(t)
        db.session.commit()
        tid = t.id
    c = _admin(app, admin_user)
    # correta aponta pra slot vazio (0) -> recusa
    c.post(f'/treinamento/admin/{tid}/pergunta', data={
        'enunciado': 'X?', 'opcao[]': ['', 'A', 'B', ''], 'correta': '0',
    })
    with app.app_context():
        assert TreinamentoPergunta.query.filter_by(treinamento_id=tid).count() == 0


def test_upload_e_servir_video_com_range(app, admin_user, tmp_path):
    app.config['TREINAMENTO_MEDIA_DIR'] = str(tmp_path)
    with app.app_context():
        t = Treinamento(titulo='V')
        db.session.add(t)
        db.session.commit()
        tid = t.id
    c = _admin(app, admin_user)
    # Upload por CORPO BRUTO (XHR): nome na query, bytes no body.
    r = c.post(f'/treinamento/admin/{tid}/video?nome=aula.mp4',
               data=b'0123456789', content_type='video/mp4')
    assert r.status_code == 204
    with app.app_context():
        t = db.session.get(Treinamento, tid)
        assert t.video_tipo == 'arquivo' and t.video_ref.endswith('.mp4')
    # Serve com HTTP Range (o <video> arrasta a barra).
    r2 = c.get(f'/treinamento/video/{tid}', headers={'Range': 'bytes=2-5'})
    assert r2.status_code == 206
    assert r2.headers['Content-Range'] == 'bytes 2-5/10'
    assert r2.get_data() == b'2345'


def test_upload_video_funciona_com_csrf_ligado(app, admin_user, tmp_path):
    """Regressão (caso real 24/07: "não consegui subir vídeo"): com CSRF
    LIGADO como em prod, o upload passa. A rota é isenta do CSRF automático
    (que parsearia o multipart sob o teto de 25 MB e estouraria o vídeo) e
    valida o token na mão DEPOIS de liberar o teto."""
    import re
    app.config['TREINAMENTO_MEDIA_DIR'] = str(tmp_path)
    app.config['WTF_CSRF_ENABLED'] = True
    with app.app_context():
        t = Treinamento(titulo='CsrfVid')
        db.session.add(t)
        db.session.commit()
        tid = t.id
    c = _admin(app, admin_user)
    page = c.get(f'/treinamento/admin/{tid}').get_data(as_text=True)
    m = re.search(r'name="csrf_token" value="([^"]+)"', page)
    assert m, 'token CSRF presente no form'
    # CSRF na query, vídeo no corpo bruto.
    r = c.post(
        f'/treinamento/admin/{tid}/video?csrf={m.group(1)}&nome=aula.mp4',
        data=b'0123456789', content_type='video/mp4')
    assert r.status_code == 204
    with app.app_context():
        assert db.session.get(Treinamento, tid).video_ref is not None


def test_upload_chunked_monta_e_serve(app, admin_user, tmp_path):
    """Upload por PEDAÇOS (o caminho da tela): 2 pedaços viram 1 vídeo, servido
    com HTTP Range. Cada request é pequena — não toca o teto de 25 MB."""
    app.config['TREINAMENTO_MEDIA_DIR'] = str(tmp_path)
    with app.app_context():
        t = Treinamento(titulo='Chunk')
        db.session.add(t)
        db.session.commit()
        tid = t.id
    c = _admin(app, admin_user)
    tok = 'a' * 16
    base = f'/treinamento/admin/{tid}/video/chunk?upload={tok}&nome=aula.mp4'
    r0 = c.post(f'{base}&i=0&n=2', data=b'01234', content_type='video/mp4')
    assert r0.status_code == 204
    r1 = c.post(f'{base}&i=1&n=2', data=b'56789', content_type='video/mp4')
    assert r1.status_code == 204
    with app.app_context():
        t = db.session.get(Treinamento, tid)
        assert t.video_tipo == 'arquivo' and t.video_ref.endswith('.mp4')
    r2 = c.get(f'/treinamento/video/{tid}', headers={'Range': 'bytes=2-5'})
    assert r2.status_code == 206
    assert r2.headers['Content-Range'] == 'bytes 2-5/10'
    assert r2.get_data() == b'2345'


def test_upload_chunked_rejeita_nao_video(app, admin_user, tmp_path):
    """Extensão inválida é barrada JÁ no primeiro pedaço (falha cedo)."""
    app.config['TREINAMENTO_MEDIA_DIR'] = str(tmp_path)
    with app.app_context():
        t = Treinamento(titulo='ChunkBad')
        db.session.add(t)
        db.session.commit()
        tid = t.id
    c = _admin(app, admin_user)
    r = c.post(f'/treinamento/admin/{tid}/video/chunk?upload={"b"*16}'
               '&nome=virus.exe&i=0&n=1', data=b'x', content_type='video/mp4')
    assert r.status_code == 400
    with app.app_context():
        assert db.session.get(Treinamento, tid).video_ref is None


def test_upload_chunked_respeita_teto(app, admin_user, tmp_path):
    """O teto acumulado corta o upload (e apaga o parcial) — sem depender de
    limite global nem de proxy."""
    app.config['TREINAMENTO_MEDIA_DIR'] = str(tmp_path)
    app.config['TREINAMENTO_MAX_VIDEO'] = 4   # 4 bytes
    with app.app_context():
        t = Treinamento(titulo='ChunkTeto')
        db.session.add(t)
        db.session.commit()
        tid = t.id
    c = _admin(app, admin_user)
    r = c.post(f'/treinamento/admin/{tid}/video/chunk?upload={"c"*16}'
               '&nome=aula.mp4&i=0&n=1', data=b'0123456789',
               content_type='video/mp4')
    assert r.status_code == 400
    with app.app_context():
        assert db.session.get(Treinamento, tid).video_ref is None


def test_upload_chunked_token_invalido(app, admin_user, tmp_path):
    """Token fora do alfabeto hex (traversal/barra) é recusado."""
    app.config['TREINAMENTO_MEDIA_DIR'] = str(tmp_path)
    with app.app_context():
        t = Treinamento(titulo='ChunkTok')
        db.session.add(t)
        db.session.commit()
        tid = t.id
    c = _admin(app, admin_user)
    r = c.post(f'/treinamento/admin/{tid}/video/chunk?upload=../etc'
               '&nome=aula.mp4&i=0&n=1', data=b'x', content_type='video/mp4')
    assert r.status_code == 400


def test_upload_chunked_com_csrf_ligado(app, admin_user, tmp_path):
    """Com CSRF LIGADO (como em prod), o token vai na query e o upload por
    pedaços passa."""
    import re
    app.config['TREINAMENTO_MEDIA_DIR'] = str(tmp_path)
    app.config['WTF_CSRF_ENABLED'] = True
    with app.app_context():
        t = Treinamento(titulo='ChunkCsrf')
        db.session.add(t)
        db.session.commit()
        tid = t.id
    c = _admin(app, admin_user)
    page = c.get(f'/treinamento/admin/{tid}').get_data(as_text=True)
    m = re.search(r'name="csrf_token" value="([^"]+)"', page)
    assert m
    r = c.post(f'/treinamento/admin/{tid}/video/chunk?csrf={m.group(1)}'
               f'&upload={"d"*16}&nome=aula.mp4&i=0&n=1',
               data=b'0123456789', content_type='video/mp4')
    assert r.status_code == 204
    with app.app_context():
        assert db.session.get(Treinamento, tid).video_ref is not None


def test_upload_rejeita_nao_video(app, admin_user, tmp_path):
    app.config['TREINAMENTO_MEDIA_DIR'] = str(tmp_path)
    with app.app_context():
        t = Treinamento(titulo='V3')
        db.session.add(t)
        db.session.commit()
        tid = t.id
    c = _admin(app, admin_user)
    r = c.post(f'/treinamento/admin/{tid}/video?nome=virus.exe',
               data=b'x', content_type='video/mp4')
    assert r.status_code == 400
    with app.app_context():
        assert db.session.get(Treinamento, tid).video_ref is None


def test_video_exige_login(app):
    with app.app_context():
        t = Treinamento(titulo='V4', video_tipo='arquivo', video_ref='x.mp4')
        db.session.add(t)
        db.session.commit()
        tid = t.id
    c = app.test_client()
    assert c.get(f'/treinamento/video/{tid}').status_code in (302, 401)


def test_arquivar_some_da_lista(app, admin_user):
    with app.app_context():
        t = Treinamento(titulo='V5')
        db.session.add(t)
        db.session.commit()
        tid = t.id
    c = _admin(app, admin_user)
    c.post(f'/treinamento/admin/{tid}/excluir')
    body = c.get('/treinamento/admin').get_data(as_text=True)
    assert 'V5' not in body
