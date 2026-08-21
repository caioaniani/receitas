"""Exclusão segura de trilhas, inclusive limpeza explícita de dados de teste.

Histórico real bloqueia a exclusão comum. No fluxo reforçado, progresso e
respostas são apagados, enquanto pontos recebem estornos auditáveis.
"""
from datetime import date

from app.extensions import db
from app.models import (
    Cargo,
    Funcionario,
    TreinoAplicacaoPratica,
    TreinoEventoPontos,
    TreinoProgressoVideo,
    TreinoQuiz,
    TreinoRespostaCheckpoint,
    TreinoRespostaQuiz,
    TreinoSelo,
    TreinoTemporada,
    TreinoTentativaQuiz,
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


def test_admin_mostra_motivo_e_acao_de_limpeza_no_layout_novo(app, admin_user):
    """O bloqueio aparece no próprio card, inclusive dentro do shell v2."""
    from app.models import TreinoVideo
    with app.app_context():
        f = Funcionario(nome='Ana', cpf='80000000007', ativo=True)
        t = TreinoTrilha(nome='Test')
        db.session.add_all([f, t])
        db.session.commit()
        v = TreinoVideo(trilha_id=t.id, titulo='Aula')
        db.session.add(v)
        db.session.commit()
        db.session.add(TreinoProgressoVideo(
            funcionario_id=f.id, video_id=v.id, versao_video=1))
        db.session.commit()

    html = _admin(app, admin_user).get(
        '/treino/admin/?v2=1').get_data(as_text=True)
    assert 'Esta trilha já foi usada.' in html
    assert '1 vídeo iniciado' in html
    assert 'Limpar progresso e excluir' in html
    assert 'name="apagar_historico" value="1"' in html
    assert 'ui-v2-sidebar' in html


def test_limpar_historico_e_excluir_trilha_de_teste(app, admin_user,
                                                    monkeypatch):
    """A confirmação forte limpa todo o grafo, estorna pontos e não toca em
    outra trilha."""
    from app.models import (
        TreinoAlternativa,
        TreinoCheckpoint,
        TreinoQuestao,
        TreinoVideo,
    )
    apagados_cloudflare = []
    monkeypatch.setattr(
        'app.services.treinamento_stream.deletar',
        lambda uid: apagados_cloudflare.append(uid))
    with app.app_context():
        f = Funcionario(nome='Ana', cpf='80000000008', ativo=True)
        gestor = Funcionario(nome='Gestor', cpf='80000000006', ativo=True)
        temp = TreinoTemporada(
            nome='Teste', inicio=date(2026, 1, 1),
            fim=date(2026, 12, 31), status='ATIVA')
        t = TreinoTrilha(nome='Test')
        outra = TreinoTrilha(nome='Real')
        db.session.add_all([f, gestor, temp, t, outra])
        db.session.commit()
        v = TreinoVideo(
            trilha_id=t.id, titulo='Aula', video_externo_id='a' * 32)
        v_outra = TreinoVideo(trilha_id=outra.id, titulo='Outra')
        db.session.add_all([v, v_outra])
        db.session.commit()
        cp = TreinoCheckpoint(
            video_id=v.id, segundo=10, enunciado='Q?',
            alternativas=['a', 'b'], indice_correto=0)
        quiz = TreinoQuiz(trilha_id=t.id, titulo='Quiz')
        quiz_video = TreinoQuiz(video_id=v.id, titulo='Quiz vídeo')
        db.session.add_all([cp, quiz, quiz_video])
        db.session.commit()
        questao = TreinoQuestao(quiz_id=quiz.id, enunciado='Pergunta?')
        db.session.add(questao)
        db.session.commit()
        alternativa = TreinoAlternativa(
            questao_id=questao.id, texto='Certa', correta=True)
        db.session.add(alternativa)
        db.session.commit()
        tentativa = TreinoTentativaQuiz(
            funcionario_id=f.id, quiz_id=quiz.id, numero_tentativa=1,
            questoes_sorteadas=[], total=1)
        db.session.add(tentativa)
        db.session.commit()
        db.session.add_all([
            TreinoRespostaQuiz(
                tentativa_id=tentativa.id, questao_id=questao.id,
                alternativa_id=alternativa.id, correta=True,
                segundos_na_questao=5, pontuou=True),
            TreinoProgressoVideo(
                funcionario_id=f.id, video_id=v.id, versao_video=1),
            TreinoRespostaCheckpoint(
                funcionario_id=f.id, checkpoint_id=cp.id,
                indice_escolhido=0, correta=True),
        ])
        aplicacao = TreinoAplicacaoPratica(
            funcionario_id=f.id, trilha_id=t.id, gestor_id=gestor.id,
            temporada_id=temp.id, data=date(2026, 8, 1),
            evidencia='teste')
        selo = TreinoSelo(funcionario_id=f.id, trilha_id=t.id)
        db.session.add_all([aplicacao, selo])
        db.session.commit()
        refs = [
            ('video', v.id), ('checkpoint', cp.id), ('quiz', quiz.id),
            ('tentativa', tentativa.id), ('aplicacao', aplicacao.id),
            ('trilha', t.id),
        ]
        for i, (tipo, ref_id) in enumerate(refs):
            db.session.add(TreinoEventoPontos(
                funcionario_id=f.id, temporada_id=temp.id,
                tipo=f'TESTE_{i}', referencia_tipo=tipo,
                referencia_id=ref_id, pontos=10))
        db.session.commit()
        tid, outro_id, vid, cpid = t.id, outra.id, v.id, cp.id

    r = _admin(app, admin_user).post(
        f'/treino/admin/trilha/{tid}/excluir',
        data={'apagar_historico': '1', 'confirmar': 'EXCLUIR'},
        follow_redirects=True)
    assert r.status_code == 200
    assert 'Trilha excluída' in r.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(TreinoTrilha, tid) is None
        assert db.session.get(TreinoTrilha, outro_id) is not None
        assert TreinoProgressoVideo.query.filter_by(video_id=vid).count() == 0
        assert TreinoRespostaCheckpoint.query.filter_by(
            checkpoint_id=cpid).count() == 0
        eventos = TreinoEventoPontos.query.all()
        assert len([e for e in eventos if e.estorno_de_id is None]) == len(refs)
        assert len([e for e in eventos if e.estorno_de_id is not None]) == len(refs)
        assert sum(e.pontos for e in eventos) == 0
    assert apagados_cloudflare == ['a' * 32]


def test_limpeza_exige_confirmacao_exata(app, admin_user):
    from app.models import TreinoVideo
    with app.app_context():
        f = Funcionario(nome='Ana', cpf='80000000005', ativo=True)
        t = TreinoTrilha(nome='Test')
        db.session.add_all([f, t])
        db.session.commit()
        v = TreinoVideo(trilha_id=t.id, titulo='Aula')
        db.session.add(v)
        db.session.commit()
        db.session.add(TreinoProgressoVideo(
            funcionario_id=f.id, video_id=v.id, versao_video=1))
        db.session.commit()
        tid = t.id
    _admin(app, admin_user).post(
        f'/treino/admin/trilha/{tid}/excluir',
        data={'apagar_historico': '1', 'confirmar': 'excluir'})
    with app.app_context():
        assert db.session.get(TreinoTrilha, tid) is not None


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
