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


def test_excluir_trilha_com_quiz_bloqueia(app, admin_user):
    with app.app_context():
        t = TreinoTrilha(nome='Seg')
        db.session.add(t)
        db.session.commit()
        db.session.add(TreinoQuiz(trilha_id=t.id, titulo='Q'))
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
