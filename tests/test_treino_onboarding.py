"""v2 §16.1 e §16.3 — onboarding automático por cargo + progressão.

- trilhas_do_cargo / onboarding: o funcionário vê as trilhas do cargo dele.
- definir_cargos_da_trilha: idempotente (substitui o conjunto).
- progressao: apto quando tem selo de TODAS as obrigatórias do cargo.
- rotas: admin liga cargos à trilha; gestor vê a progressão; home destaca.
"""
from datetime import timedelta

from app.extensions import db
from app.models import (
    Cargo,
    Funcionario,
    Loja,
    TreinoProgressoVideo,
    TreinoSelo,
    TreinoTemporada,
    TreinoTrilha,
    TreinoTrilhaCargo,
    TreinoVideo,
    Usuario,
)
from app.services import treino_onboarding as ob
from app.utils import agora, hoje


def _temp():
    t = TreinoTemporada(nome='T', inicio=hoje() - timedelta(days=1),
                        fim=hoje() + timedelta(days=30), status='ATIVA')
    db.session.add(t)
    db.session.commit()
    return t


def _login(app, usuario_id):
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(usuario_id)
        s['_fresh'] = True
    return c


_SEQ = [0]


def _func(nome='Ana', papel='funcionario', cargo=None, ativo=True):
    _SEQ[0] += 1
    n = _SEQ[0]
    loja = Loja(nome=f'Brooklin {n}', ativa=True)
    u = Usuario(nome=nome, login=f'{nome}-{papel}-{n}', papel=papel)
    u.set_senha('x' * 8)
    db.session.add_all([loja, u])
    db.session.commit()
    f = Funcionario(nome=nome, cpf=str(10000000000 + n), ativo=ativo,
                    usuario_id=u.id, cargo_id=cargo.id if cargo else None)
    f.lojas.append(loja)
    db.session.add(f)
    db.session.commit()
    return u, f, loja


def test_trilhas_do_cargo_filtra_ativa_e_obrigatoria(app):
    with app.app_context():
        cargo = Cargo(nome='Padeiro')
        t_ativa = TreinoTrilha(nome='Segurança', ordem=1)
        t_off = TreinoTrilha(nome='Antiga', ordem=2, ativa=False)
        t_opc = TreinoTrilha(nome='Extra', ordem=3)
        db.session.add_all([cargo, t_ativa, t_off, t_opc])
        db.session.commit()
        db.session.add_all([
            TreinoTrilhaCargo(trilha_id=t_ativa.id, cargo_id=cargo.id),
            TreinoTrilhaCargo(trilha_id=t_off.id, cargo_id=cargo.id),
            TreinoTrilhaCargo(trilha_id=t_opc.id, cargo_id=cargo.id,
                              obrigatoria=False)])
        db.session.commit()
        todas = ob.trilhas_do_cargo(cargo.id)
        assert {t.nome for t in todas} == {'Segurança', 'Extra'}  # inativa fora
        so_obrig = ob.trilhas_do_cargo(cargo.id, so_obrigatorias=True)
        assert {t.nome for t in so_obrig} == {'Segurança'}
        assert ob.trilhas_do_cargo(None) == []


def test_onboarding_do_funcionario_vem_do_cargo(app):
    with app.app_context():
        cargo = Cargo(nome='Atendente')
        t = TreinoTrilha(nome='Caixa')
        db.session.add_all([cargo, t])
        db.session.commit()
        db.session.add(TreinoTrilhaCargo(trilha_id=t.id, cargo_id=cargo.id))
        db.session.commit()
        _, f, _ = _func(cargo=cargo)
        assert [x.nome for x in ob.onboarding_do_funcionario(f)] == ['Caixa']
        _, f2, _ = _func(nome='Sem', cargo=None)
        assert ob.onboarding_do_funcionario(f2) == []


def test_definir_cargos_da_trilha_e_idempotente(app):
    with app.app_context():
        c1 = Cargo(nome='A')
        c2 = Cargo(nome='B')
        c3 = Cargo(nome='C')
        t = TreinoTrilha(nome='X')
        db.session.add_all([c1, c2, c3, t])
        db.session.commit()
        ob.definir_cargos_da_trilha(t.id, [c1.id, c2.id])
        assert TreinoTrilhaCargo.query.filter_by(trilha_id=t.id).count() == 2
        # substitui: tira c1, mantém c2, adiciona c3
        ob.definir_cargos_da_trilha(t.id, [c2.id, c3.id])
        ids = {m.cargo_id for m in TreinoTrilhaCargo.query.filter_by(
            trilha_id=t.id)}
        assert ids == {c2.id, c3.id}
        # zera
        ob.definir_cargos_da_trilha(t.id, [])
        assert TreinoTrilhaCargo.query.filter_by(trilha_id=t.id).count() == 0


def test_progressao_apto_so_com_todos_os_selos(app):
    with app.app_context():
        cargo = Cargo(nome='Padeiro')
        t1 = TreinoTrilha(nome='Seg', ordem=1)
        t2 = TreinoTrilha(nome='Higiene', ordem=2)
        db.session.add_all([cargo, t1, t2])
        db.session.commit()
        db.session.add_all([
            TreinoTrilhaCargo(trilha_id=t1.id, cargo_id=cargo.id),
            TreinoTrilhaCargo(trilha_id=t2.id, cargo_id=cargo.id)])
        db.session.commit()
        _, f, _ = _func(cargo=cargo)
        p = ob.progressao(f)
        assert p['total'] == 2 and p['concluidas'] == 0 and not p['apto']
        # um selo → em andamento
        db.session.add(TreinoSelo(funcionario_id=f.id, trilha_id=t1.id,
                                  carga_horaria_minutos=10))
        db.session.commit()
        p = ob.progressao(f)
        assert p['concluidas'] == 1 and not p['apto']
        # os dois → apto
        db.session.add(TreinoSelo(funcionario_id=f.id, trilha_id=t2.id,
                                  carga_horaria_minutos=10))
        db.session.commit()
        p = ob.progressao(f)
        assert p['concluidas'] == 2 and p['apto']


def test_progressao_sem_cargo_nao_e_apto(app):
    with app.app_context():
        _, f, _ = _func(cargo=None)
        p = ob.progressao(f)
        assert p['total'] == 0 and not p['apto']


def test_definir_cargos_ignora_invalidos_e_inexistentes(app):
    with app.app_context():
        c1 = Cargo(nome='A')
        t = TreinoTrilha(nome='X')
        db.session.add_all([c1, t])
        db.session.commit()
        # 'abc' (não numérico), '' (vazio) e 999999 (inexistente) são ignorados;
        # só c1 (válido) vira vínculo — nada de 500 nem FK órfã.
        ob.definir_cargos_da_trilha(t.id, ['abc', '', '999999', str(c1.id)])
        vinc = TreinoTrilhaCargo.query.filter_by(trilha_id=t.id).all()
        assert [m.cargo_id for m in vinc] == [c1.id]


def test_progressao_lote_bate_com_progressao(app):
    with app.app_context():
        cargo = Cargo(nome='Padeiro')
        t1 = TreinoTrilha(nome='Seg', ordem=1)
        t2 = TreinoTrilha(nome='Higiene', ordem=2)
        db.session.add_all([cargo, t1, t2])
        db.session.commit()
        db.session.add_all([
            TreinoTrilhaCargo(trilha_id=t1.id, cargo_id=cargo.id),
            TreinoTrilhaCargo(trilha_id=t2.id, cargo_id=cargo.id)])
        db.session.commit()
        _, fa, _ = _func(nome='Com', cargo=cargo)
        _, fb, _ = _func(nome='Sem', cargo=None)
        db.session.add(TreinoSelo(funcionario_id=fa.id, trilha_id=t1.id,
                                  carga_horaria_minutos=10))
        db.session.commit()
        lote = ob.progressao_lote([fa, fb])
        pa, pb = ob.progressao(fa), ob.progressao(fb)
        assert lote[fa.id]['total'] == pa['total'] == 2
        assert lote[fa.id]['concluidas'] == pa['concluidas'] == 1
        assert lote[fa.id]['apto'] == pa['apto'] is False
        assert lote[fb.id]['total'] == pb['total'] == 0
        assert ob.progressao_lote([]) == {}


def test_rota_admin_liga_cargos(app, admin_user):
    with app.app_context():
        cargo = Cargo(nome='Padeiro')
        t = TreinoTrilha(nome='Seg')
        db.session.add_all([cargo, t])
        db.session.commit()
        tid, cid = t.id, cargo.id
    c = _login(app, admin_user.id)
    r = c.post(f'/treino/admin/trilha/{tid}/cargos',
               data={'cargo_ids': [str(cid)]})
    assert r.status_code in (302, 303)
    with app.app_context():
        assert TreinoTrilhaCargo.query.filter_by(
            trilha_id=tid, cargo_id=cid).count() == 1


def test_rota_cargos_ajax_persiste_e_devolve_json(app, admin_user):
    """Auto-salvar ao marcar (ajax=1) persiste e devolve JSON, sem redirect."""
    with app.app_context():
        cargo = Cargo(nome='Padeiro')
        t = TreinoTrilha(nome='Seg')
        db.session.add_all([cargo, t])
        db.session.commit()
        tid, cid = t.id, cargo.id
    c = _login(app, admin_user.id)
    r = c.post(f'/treino/admin/trilha/{tid}/cargos',
               data={'ajax': '1', 'cargo_ids': [str(cid)]})
    assert r.status_code == 200 and r.get_json()['ok']
    with app.app_context():
        assert TreinoTrilhaCargo.query.filter_by(
            trilha_id=tid, cargo_id=cid).count() == 1
    # desmarcar tudo (ajax sem cargo_ids) esvazia
    r2 = c.post(f'/treino/admin/trilha/{tid}/cargos', data={'ajax': '1'})
    assert r2.status_code == 200
    with app.app_context():
        assert TreinoTrilhaCargo.query.filter_by(trilha_id=tid).count() == 0


def test_rota_gestor_progressao_renderiza(app, admin_user):
    with app.app_context():
        _temp()
        cargo = Cargo(nome='Padeiro')
        t = TreinoTrilha(nome='Seg')
        db.session.add_all([cargo, t])
        db.session.commit()
        db.session.add(TreinoTrilhaCargo(trilha_id=t.id, cargo_id=cargo.id))
        db.session.commit()
        _func(nome='Bia', cargo=cargo)
    c = _login(app, admin_user.id)
    r = c.get('/treino/gestor/progressao')
    assert r.status_code == 200 and 'Progressão' in r.get_data(as_text=True)


def test_progressao_distingue_acesso_inicio_e_andamento(app, admin_user):
    with app.app_context():
        _temp()
        cargo = Cargo(nome='Atendimento')
        trilha = TreinoTrilha(nome='Cultura')
        db.session.add_all([cargo, trilha])
        db.session.commit()
        db.session.add(TreinoTrilhaCargo(
            trilha_id=trilha.id, cargo_id=cargo.id))
        video = TreinoVideo(
            trilha_id=trilha.id, titulo='Nossa história',
            video_externo_id='historia')
        db.session.add(video)
        db.session.commit()

        _, sem_acesso, _ = _func(nome='Sem Acesso', cargo=cargo)
        sem_acesso.usuario_id = None
        _, nao_iniciou, _ = _func(nome='Não Iniciou', cargo=cargo)
        _, em_andamento, _ = _func(nome='Em Andamento', cargo=cargo)
        db.session.add(TreinoProgressoVideo(
            funcionario_id=em_andamento.id, video_id=video.id,
            versao_video=video.versao, percentual=25,
            ultimo_heartbeat_em=agora()))
        db.session.commit()
        ids = sem_acesso.id, nao_iniciou.id, em_andamento.id

    html = _login(app, admin_user.id).get(
        '/treino/gestor/progressao').get_data(as_text=True)

    assert (f'data-funcionario-id="{ids[0]}" '
            'data-status="sem_acesso"') in html
    assert (f'data-funcionario-id="{ids[1]}" '
            'data-status="nao_iniciou"') in html
    assert (f'data-funcionario-id="{ids[2]}" '
            'data-status="andamento"') in html
    assert 'não significa que a pessoa já entrou no sistema' in html
    assert 'Treinamento não iniciado' in html


def test_progressao_filtra_unidade_e_cadastro_inativo(app, admin_user):
    with app.app_context():
        _, ativo_a, unidade_a = _func(nome='Ativo Unidade A')
        _, ativo_b, _ = _func(nome='Ativo Unidade B')
        _, inativo, _ = _func(nome='Cadastro Inativo', ativo=False)
        ids = ativo_a.id, ativo_b.id, inativo.id
        unidade_a_id = unidade_a.id

    cliente = _login(app, admin_user.id)
    padrao = cliente.get('/treino/gestor/progressao').get_data(as_text=True)
    assert f'data-funcionario-id="{ids[0]}"' in padrao
    assert f'data-funcionario-id="{ids[2]}"' not in padrao

    por_unidade = cliente.get(
        f'/treino/gestor/progressao?unidade={unidade_a_id}'
    ).get_data(as_text=True)
    assert f'data-funcionario-id="{ids[0]}"' in por_unidade
    assert f'data-funcionario-id="{ids[1]}"' not in por_unidade

    somente_inativos = cliente.get(
        '/treino/gestor/progressao?cadastro=inativos'
    ).get_data(as_text=True)
    assert (f'data-funcionario-id="{ids[2]}" '
            'data-status="inativo"') in somente_inativos
    assert f'data-funcionario-id="{ids[0]}"' not in somente_inativos


def test_home_destaca_onboarding(app):
    with app.app_context():
        _temp()
        cargo = Cargo(nome='Atendente')
        t = TreinoTrilha(nome='Caixa Onboarding')
        db.session.add_all([cargo, t])
        db.session.commit()
        db.session.add(TreinoTrilhaCargo(trilha_id=t.id, cargo_id=cargo.id))
        db.session.commit()
        u, f, _ = _func(cargo=cargo)
        uid = u.id
    c = _login(app, uid)
    txt = c.get('/treino/').get_data(as_text=True)
    assert 'Onboarding do seu cargo' in txt and 'Caixa Onboarding' in txt
