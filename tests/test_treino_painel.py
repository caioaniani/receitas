"""Painéis do treinamento: prioridade, equipe e integração com o RH."""
from datetime import timedelta

from app.extensions import db
from app.models import (
    Cargo,
    Funcionario,
    TreinoProgressoVideo,
    TreinoTrilha,
    TreinoTrilhaCargo,
    TreinoVideo,
    Usuario,
)
from app.services import treino_painel as painel
from app.utils import agora


def _usuario(nome, papel='funcionario'):
    u = Usuario(nome=nome, login=nome.lower().replace(' ', '-'), papel=papel)
    u.set_senha('senha-segura')
    db.session.add(u)
    db.session.commit()
    return u


def _funcionario(nome, cpf, usuario=None, cargo=None):
    f = Funcionario(
        nome=nome, cpf=cpf, ativo=True,
        usuario_id=usuario.id if usuario else None,
        cargo_id=cargo.id if cargo else None,
    )
    db.session.add(f)
    db.session.commit()
    return f


def test_proximo_passo_prioriza_obrigatorio_e_retomada(app):
    cargo = Cargo(nome='Atendente')
    opcional = TreinoTrilha(nome='Opcional', ordem=1)
    obrigatoria = TreinoTrilha(nome='Cultura', ordem=20)
    db.session.add_all([cargo, opcional, obrigatoria])
    db.session.commit()
    db.session.add(TreinoTrilhaCargo(
        trilha_id=obrigatoria.id, cargo_id=cargo.id))
    v_opcional = TreinoVideo(
        trilha_id=opcional.id, titulo='Extra', video_externo_id='extra')
    v_obrigatorio = TreinoVideo(
        trilha_id=obrigatoria.id, titulo='Nossa história',
        video_externo_id='historia')
    db.session.add_all([v_opcional, v_obrigatorio])
    db.session.commit()
    f = _funcionario('Ana', '100', cargo=cargo)
    db.session.add(TreinoProgressoVideo(
        funcionario_id=f.id, video_id=v_obrigatorio.id,
        versao_video=1, percentual=42))
    db.session.commit()

    proximo = painel.proximo_passo(
        f, [opcional, obrigatoria], {obrigatoria.id})

    assert proximo['video'].titulo == 'Nossa história'
    assert proximo['percentual'] == 42
    assert proximo['rotulo'] == 'Continuar aula'
    assert proximo['obrigatorio']


def test_resumo_admin_mostra_so_pendencias_acionaveis(app):
    trilha = TreinoTrilha(nome='Cultura')
    db.session.add(trilha)
    db.session.commit()
    db.session.add_all([
        TreinoVideo(trilha_id=trilha.id, titulo='Sem arquivo'),
        TreinoVideo(trilha_id=trilha.id, titulo='Processando',
                    video_externo_id='processando', duracao_segundos=0),
        TreinoVideo(trilha_id=trilha.id, titulo='Pronta',
                    video_externo_id='pronta', duracao_segundos=60,
                    ativo=False),
        TreinoVideo(trilha_id=trilha.id, titulo='No ar',
                    video_externo_id='publicada', duracao_segundos=60,
                    ativo=True),
    ])
    db.session.commit()
    com_acesso = _funcionario('Com acesso', '101', _usuario('Com acesso'))
    sem_acesso = _funcionario('Sem acesso', '102')

    resumo = painel.resumo_admin([trilha], [com_acesso, sem_acesso])

    assert resumo['aulas_publicadas'] == 2
    assert resumo['aulas_sem_arquivo'] == 1
    assert resumo['sem_acesso'] == 1
    assert {p['tipo'] for p in resumo['pendencias']} == {
        'processando', 'rascunho', 'acesso'}


def test_painel_equipe_coloca_sem_acesso_e_parado_primeiro(app):
    cargo = Cargo(nome='Padeiro')
    trilha = TreinoTrilha(nome='Higiene')
    db.session.add_all([cargo, trilha])
    db.session.commit()
    db.session.add(TreinoTrilhaCargo(trilha_id=trilha.id, cargo_id=cargo.id))
    video = TreinoVideo(
        trilha_id=trilha.id, titulo='Limpeza', video_externo_id='limpeza')
    db.session.add(video)
    db.session.commit()
    sem_acesso = _funcionario('Ana', '103', cargo=cargo)
    parado = _funcionario('Bruno', '104', _usuario('Bruno'), cargo=cargo)
    db.session.add(TreinoProgressoVideo(
        funcionario_id=parado.id, video_id=video.id, versao_video=1,
        iniciado_em=agora() - timedelta(days=9),
        ultimo_heartbeat_em=agora() - timedelta(days=8)))
    db.session.commit()

    resultado = painel.painel_equipe([parado, sem_acesso], None)

    assert [item['status'] for item in resultado['linhas']] == [
        'sem_acesso', 'parado']
    assert resultado['contagens']['precisam_atencao'] == 2


def test_painel_equipe_nao_trata_inativo_como_pendencia(app):
    inativo = _funcionario('Inativo', '106')
    inativo.ativo = False
    db.session.commit()

    resultado = painel.painel_equipe([inativo], None)

    assert resultado['linhas'][0]['status'] == 'inativo'
    assert resultado['contagens']['inativo'] == 1
    assert resultado['contagens']['precisam_atencao'] == 0


def test_ficha_rh_exibe_resumo_do_treinamento(app, owner_user):
    cargo = Cargo(nome='Atendente')
    db.session.add(cargo)
    db.session.commit()
    func = _funcionario('Maria da Silva', '105', cargo=cargo)
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(owner_user.id)
        session['_fresh'] = True

    html = client.get(f'/rh/funcionarios/{func.id}?v2=1').get_data(as_text=True)

    assert 'Jornada de Maria' in html
    assert 'Não criado' in html
    assert 'Criar ou vincular acesso' in html


def test_avanco_parcial_aparece_sem_cargo_nem_temporada(app):
    trilha = TreinoTrilha(nome='Cultura')
    db.session.add(trilha)
    db.session.flush()
    video = TreinoVideo(trilha_id=trilha.id, titulo='Nossa história',
                        video_externo_id='historia')
    db.session.add(video)
    db.session.commit()
    pessoa = _funcionario('Pessoa em treinamento', '107')
    progresso = TreinoProgressoVideo(funcionario_id=pessoa.id,
                                     video_id=video.id, versao_video=1,
                                     percentual=82)
    db.session.add(progresso)
    db.session.commit()

    linha = painel.painel_equipe([pessoa], None)['linhas'][0]
    resumo = painel.resumo_funcionario(pessoa, None)

    assert linha['aulas'] == resumo['aulas'] == {
        'total': 1, 'iniciadas': 1, 'concluidas': 0, 'percentual': 82}
    assert linha['percentual'] == 0
    assert progresso.concluido_em is None


def test_resumo_aulas_exclui_rascunho_modulo_oculto_e_versao_antiga(app):
    ativo, oculto = TreinoTrilha(nome='No ar'), TreinoTrilha(nome='Oculto', ativa=False)
    db.session.add_all([ativo, oculto])
    db.session.flush()
    atual = TreinoVideo(trilha_id=ativo.id, titulo='Atual', video_externo_id='a', versao=2)
    outras = [
        TreinoVideo(trilha_id=ativo.id, titulo='Rascunho', video_externo_id='r', ativo=False),
        TreinoVideo(trilha_id=ativo.id, titulo='Sem arquivo'),
        TreinoVideo(trilha_id=oculto.id, titulo='Oculta', video_externo_id='o'),
    ]
    db.session.add_all([atual, *outras])
    db.session.commit()
    pessoa = _funcionario('Outra pessoa', '108')
    for video in [atual, *outras]:
        db.session.add(TreinoProgressoVideo(funcionario_id=pessoa.id,
                       video_id=video.id, versao_video=1, percentual=100,
                       concluido_em=agora()))
    db.session.commit()

    assert painel.resumo_aulas_lote([pessoa])[pessoa.id] == {
        'total': 1, 'iniciadas': 0, 'concluidas': 0, 'percentual': 0}
