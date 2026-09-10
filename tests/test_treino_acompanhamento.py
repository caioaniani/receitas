"""Leitura individual do treinamento e separação da identidade do avaliador."""
from datetime import timedelta

import pytest

from app.extensions import db
from app.models import (
    Cargo,
    Funcionario,
    TreinoAplicacaoPratica,
    TreinoEventoPontos,
    TreinoProgressoVideo,
    TreinoQuiz,
    TreinoSelo,
    TreinoTemporada,
    TreinoTentativaQuiz,
    TreinoTrilha,
    TreinoTrilhaCargo,
    TreinoVideo,
    Usuario,
)
from app.services import treino_lideranca
from app.services.treino_acompanhamento import progresso_pessoa
from app.utils import agora, hoje


def _login(app, usuario):
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(usuario.id)
        session['_fresh'] = True
    return client


def _pessoa(nome, cpf, **kwargs):
    pessoa = Funcionario(nome=nome, cpf=cpf, ativo=True, **kwargs)
    db.session.add(pessoa)
    db.session.commit()
    return pessoa


def _usuario(nome, papel='funcionario'):
    usuario = Usuario(nome=nome, login=nome, papel=papel, somente_treino=True)
    usuario.set_senha('senha-segura')
    db.session.add(usuario)
    db.session.commit()
    return usuario


def _modulo(nome='Cultura', **kwargs):
    trilha = TreinoTrilha(nome=nome, **kwargs)
    db.session.add(trilha)
    db.session.commit()
    return trilha


def _aula(trilha, titulo, **kwargs):
    video = TreinoVideo(trilha_id=trilha.id, titulo=titulo,
                        video_externo_id=kwargs.pop('video_externo_id', titulo),
                        **kwargs)
    db.session.add(video)
    db.session.commit()
    return video


def _ciclo():
    ciclo = TreinoTemporada(nome='Ciclo atual', inicio=hoje() - timedelta(days=1),
                            fim=hoje() + timedelta(days=30), status='ATIVA')
    db.session.add(ciclo)
    db.session.commit()
    return ciclo


@pytest.mark.parametrize('fixture_usuario', ['admin_user', 'owner_user'])
def test_admin_sem_ficha_consulta_mas_nao_valida(app, request, fixture_usuario):
    usuario = request.getfixturevalue(fixture_usuario)
    pessoa = _pessoa('Pessoa acompanhada', '101')
    trilha = _modulo()
    treino_lideranca.salvar_checklist(
        trilha, 'Prática', ['Confere o pedido antes de entregar'])
    _ciclo()
    client = _login(app, usuario)

    pagina = client.get(f'/treino/gestor/progresso/{pessoa.id}')
    assert pagina.status_code == 200
    assert 'Progresso individual' in pagina.get_data(as_text=True)
    observacao = client.get(f'/treino/gestor/observar/{pessoa.id}')
    corpo = observacao.get_data(as_text=True)
    assert observacao.status_code == 200
    assert 'Consulta administrativa' in corpo
    assert 'Confere o pedido antes de entregar' in corpo
    assert 'name="itens_ok"' not in corpo
    assert 'name="evidencia"' not in corpo
    assert client.post(
        f'/treino/gestor/observar/{pessoa.id}/{trilha.id}').status_code == 403
    assert client.post('/treino/gestor/api/aplicacao', data={
        'funcionario_id': pessoa.id, 'trilha_id': trilha.id}).status_code == 403
    assert client.get('/treino/gestor/progresso/999999').status_code == 404
    assert Funcionario.query.filter_by(usuario_id=usuario.id).count() == 0
    assert TreinoAplicacaoPratica.query.count() == 0
    assert TreinoEventoPontos.query.count() == 0
    assert TreinoSelo.query.count() == 0


def test_relatorio_respeita_equipe_direta_e_papel(app):
    usuario = _usuario('lider')
    gestor = _pessoa('Líder', '201', usuario_id=usuario.id)
    pessoa = _pessoa('Pessoa da equipe', '202', lider_id=gestor.id)
    outra = _pessoa('Outra equipe', '203')
    client = _login(app, usuario)
    assert client.get(f'/treino/gestor/progresso/{pessoa.id}').status_code == 200
    assert client.get(f'/treino/gestor/progresso/{outra.id}').status_code == 403
    pessoa.ativo = False
    # Mantém um liderado ativo para o papel de gestor continuar reconhecido.
    _pessoa('Outra pessoa da equipe', '204', lider_id=gestor.id)
    db.session.commit()
    assert client.get(f'/treino/gestor/progresso/{pessoa.id}').status_code == 403

    sem_equipe = _usuario('gerente-sem-ficha', papel='gerente')
    assert _login(app, sem_equipe).get(
        f'/treino/gestor/progresso/{outra.id}').status_code == 403
    funcionario = _usuario('funcionario-comum')
    assert _login(app, funcionario).get(
        f'/treino/gestor/progresso/{outra.id}').status_code == 403


def test_avanco_parcial_so_versao_atual_publicada_e_obrigatorias_primeiro(
        app, owner_user):
    cargo = Cargo(nome='Atendimento')
    db.session.add(cargo)
    db.session.commit()
    pessoa = _pessoa('Ana', '301', cargo_id=cargo.id)
    outra = _pessoa('Outra pessoa', '302')
    opcional = _modulo('Opcional', ordem=0)
    obrigatoria = _modulo('Obrigatória', ordem=10)
    oculta = _modulo('Módulo oculto', ativa=False)
    db.session.add(TreinoTrilhaCargo(
        cargo_id=cargo.id, trilha_id=obrigatoria.id, obrigatoria=True))
    parcial = _aula(obrigatoria, 'Aula em andamento', versao=2)
    nova = _aula(obrigatoria, 'Nova versão sem início', versao=2)
    concluida = _aula(opcional, 'Aula concluída')
    _aula(obrigatoria, 'Rascunho sem arquivo', video_externo_id=None)
    _aula(obrigatoria, 'Aula desativada', ativo=False)
    _aula(oculta, 'Conteúdo oculto')
    instante = agora() - timedelta(hours=1)
    db.session.add_all([
        TreinoProgressoVideo(funcionario_id=pessoa.id, video_id=parcial.id,
            versao_video=1, percentual=100, concluido_em=agora()),
        TreinoProgressoVideo(funcionario_id=pessoa.id, video_id=parcial.id,
            versao_video=2, percentual=40, iniciado_em=instante,
            ultimo_heartbeat_em=instante),
        TreinoProgressoVideo(funcionario_id=pessoa.id, video_id=nova.id,
            versao_video=1, percentual=100, concluido_em=agora()),
        TreinoProgressoVideo(funcionario_id=pessoa.id, video_id=concluida.id,
            percentual=93, iniciado_em=instante, concluido_em=instante),
        TreinoProgressoVideo(funcionario_id=outra.id, video_id=parcial.id,
            versao_video=2, percentual=100, concluido_em=agora()),
    ])
    db.session.commit()

    dados = progresso_pessoa(pessoa, None)
    assert [m['trilha'].id for m in dados['modulos']] == [obrigatoria.id, opcional.id]
    assert dados['aulas_total'] == 3
    assert dados['aulas_iniciadas'] == 2
    assert dados['aulas_concluidas'] == 1
    assert dados['percentual_assistido'] == 47  # (40 + 0 + 100) / 3
    assert dados['ultima_atividade'] == instante
    obrigatorio = dados['modulos'][0]
    assert obrigatorio['obrigatoria']
    assert [a['percentual'] for a in obrigatorio['aulas']] == [40, 0]
    assert not any(a['concluido'] for a in obrigatorio['aulas'])
    assert obrigatorio['aulas'][1]['ultima_atividade'] is None
    corpo = _login(app, owner_user).get(
        f'/treino/gestor/progresso/{pessoa.id}').get_data(as_text=True)
    assert '40% assistido' in corpo
    assert 'Rascunho sem arquivo' not in corpo
    assert 'Aula desativada' not in corpo
    assert 'Conteúdo oculto' not in corpo


def test_quizzes_pratica_e_ausencia_de_ciclo_nao_emitem_conclusao(app):
    pessoa = _pessoa('Aprendiz', '401')
    gestor = _pessoa('Avaliador', '402')
    trilha = _modulo()
    video = _aula(trilha, 'Aula publicada')
    quiz = TreinoQuiz(trilha_id=trilha.id, titulo='Avaliação publicada')
    db.session.add(quiz)
    db.session.commit()
    ciclo = _ciclo()
    registro = TreinoAplicacaoPratica(
        funcionario_id=pessoa.id, gestor_id=gestor.id, trilha_id=trilha.id,
        temporada_id=ciclo.id, data=hoje(), itens_ok=[1],
        evidencia='Observação que já estava registrada.', status='REGISTRADA')
    db.session.add_all([
        registro,
        TreinoProgressoVideo(funcionario_id=pessoa.id, video_id=video.id,
                            percentual=100, concluido_em=agora()),
        TreinoTentativaQuiz(funcionario_id=pessoa.id, quiz_id=quiz.id,
            numero_tentativa=1, questoes_sorteadas=[], acertos=5, total=5,
            aprovada=True, finalizado_em=agora()),
    ])
    db.session.commit()

    sem_ciclo = progresso_pessoa(pessoa, None)['modulos'][0]
    assert sem_ciclo['percentual_assistido'] == 100
    assert sem_ciclo['quizzes'][0]['status'] == 'Aprovada'
    assert sem_ciclo['aplicacao'] is None
    assert not sem_ciclo['completa']
    com_ciclo = progresso_pessoa(pessoa, ciclo)['modulos'][0]
    assert com_ciclo['completa']
    assert com_ciclo['aplicacao'].id == registro.id
    assert TreinoSelo.query.count() == 0
    assert TreinoEventoPontos.query.count() == 0
    registro.status = 'ESTORNADA'
    db.session.commit()
    assert not progresso_pessoa(pessoa, ciclo)['modulos'][0]['completa']


def test_100_porcento_sem_conclusao_continua_pendente(app):
    pessoa = _pessoa('Aprendiz', '501')
    trilha = _modulo()
    video = _aula(trilha, 'Aula aguardando critérios de conclusão')
    db.session.add(TreinoProgressoVideo(
        funcionario_id=pessoa.id, video_id=video.id, percentual=100))
    db.session.commit()
    dados = progresso_pessoa(pessoa, None)
    assert dados['percentual_assistido'] == 100
    assert dados['aulas_concluidas'] == 0
    assert not dados['modulos'][0]['completa']
