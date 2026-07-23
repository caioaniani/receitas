"""Excluir trilha não pode dar 500 (caso real: FK do mapeamento de cargo /
quiz / selo). Bloqueia com mensagem quando há conteúdo/histórico; limpa só o
mapeamento de cargo (config pura). SQLite não força FK — o guard explícito é o
que protege nos dois bancos.
"""
from app.extensions import db
from app.models import (
    Cargo,
    Funcionario,
    TreinoQuiz,
    TreinoSelo,
    TreinoTrilha,
    TreinoTrilhaCargo,
)


def _admin(app, admin_user):
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(admin_user.id)
        s['_fresh'] = True
    return c


def _trilha(app, nome='T'):
    with app.app_context():
        t = TreinoTrilha(nome=nome)
        db.session.add(t)
        db.session.commit()
        return t.id


def test_excluir_trilha_vazia(app, admin_user):
    tid = _trilha(app)
    c = _admin(app, admin_user)
    c.post(f'/treino/admin/trilha/{tid}/excluir')
    with app.app_context():
        assert db.session.get(TreinoTrilha, tid) is None


def test_excluir_trilha_com_cargo_limpa_e_exclui(app, admin_user):
    """O caso que dava 500: trilha com mapeamento de cargo."""
    with app.app_context():
        cargo = Cargo(nome='Padeiro')
        t = TreinoTrilha(nome='Seg')
        db.session.add_all([cargo, t])
        db.session.commit()
        db.session.add(TreinoTrilhaCargo(trilha_id=t.id, cargo_id=cargo.id))
        db.session.commit()
        tid = t.id
    c = _admin(app, admin_user)
    r = c.post(f'/treino/admin/trilha/{tid}/excluir')
    assert r.status_code in (302, 303)   # nunca 500
    with app.app_context():
        assert db.session.get(TreinoTrilha, tid) is None
        assert TreinoTrilhaCargo.query.filter_by(trilha_id=tid).count() == 0


def test_excluir_trilha_de_teste_com_conteudo_cascateia(app, admin_user):
    """Trilha de teste (vídeo + checkpoint + quiz+questão), SEM histórico:
    exclui cascateando o conteúdo — não dá 500 nem bloqueia."""
    from app.models import (
        TreinoAlternativa,
        TreinoCheckpoint,
        TreinoQuestao,
        TreinoVideo,
    )
    with app.app_context():
        t = TreinoTrilha(nome='Test')
        db.session.add(t)
        db.session.commit()
        v = TreinoVideo(trilha_id=t.id, titulo='Aula', video_externo_id='a' * 32)
        db.session.add(v)
        db.session.commit()
        db.session.add(TreinoCheckpoint(video_id=v.id, segundo=10,
                       enunciado='Q?', alternativas=['a', 'b'],
                       indice_correto=0))
        q = TreinoQuiz(trilha_id=t.id, titulo='Quiz')
        db.session.add(q)
        db.session.commit()
        quest = TreinoQuestao(quiz_id=q.id, enunciado='P?')
        db.session.add(quest)
        db.session.commit()
        db.session.add(TreinoAlternativa(questao_id=quest.id, texto='x',
                                         correta=True))
        db.session.commit()
        tid = t.id
    c = _admin(app, admin_user)
    r = c.post(f'/treino/admin/trilha/{tid}/excluir')
    assert r.status_code in (302, 303)   # nunca 500
    with app.app_context():
        assert db.session.get(TreinoTrilha, tid) is None   # excluída


def test_excluir_trilha_com_video_assistido_bloqueia(app, admin_user):
    """Histórico real (funcionário assistiu) preserva a trilha."""
    from app.models import TreinoProgressoVideo, TreinoVideo
    with app.app_context():
        f = Funcionario(nome='Ana', cpf='80000000009', ativo=True)
        t = TreinoTrilha(nome='Seg')
        db.session.add_all([f, t])
        db.session.commit()
        v = TreinoVideo(trilha_id=t.id, titulo='Aula')
        db.session.add(v)
        db.session.commit()
        db.session.add(TreinoProgressoVideo(funcionario_id=f.id, video_id=v.id))
        db.session.commit()
        tid = t.id
    c = _admin(app, admin_user)
    r = c.post(f'/treino/admin/trilha/{tid}/excluir')
    assert r.status_code in (302, 303)
    with app.app_context():
        assert db.session.get(TreinoTrilha, tid) is not None   # preservada


def test_excluir_trilha_com_selo_bloqueia(app, admin_user):
    with app.app_context():
        f = Funcionario(nome='Ana', cpf='80000000001', ativo=True)
        t = TreinoTrilha(nome='Seg')
        db.session.add_all([f, t])
        db.session.commit()
        db.session.add(TreinoSelo(funcionario_id=f.id, trilha_id=t.id,
                                  carga_horaria_minutos=10))
        db.session.commit()
        tid = t.id
    c = _admin(app, admin_user)
    r = c.post(f'/treino/admin/trilha/{tid}/excluir')
    assert r.status_code in (302, 303)
    with app.app_context():
        assert db.session.get(TreinoTrilha, tid) is not None   # histórico mantido
