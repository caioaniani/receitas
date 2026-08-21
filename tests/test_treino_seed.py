"""Seed da Universidade Padaria Artesanal (12/08/2026, estrutura do dono):
9 módulos (TreinoTrilha) e 140 aulas (TreinoVideo sem arquivo). Trilhas
nascem DESLIGADAS (o dono liga módulo a módulo após subir os vídeos);
one-shot com guard em AppConfig — apagar/renomear nunca ressuscita."""
from app.extensions import db
from app.models import AppConfig, TreinoTrilha, TreinoVideo
from app.services import treino_seed

ESPERADO_POR_MODULO = {
    'Módulo 1 — Cultura': 15,
    'Módulo 2 — Atendimento': 18,
    'Módulo 3 — Produtos': 20,
    'Módulo 4 — Operação': 18,
    'Módulo 5 — Caixa e Sistemas': 15,
    'Módulo 6 — Experiência do Cliente': 15,
    'Módulo 7 — Segurança e Boas Práticas': 14,
    'Módulo 8 — Desenvolvimento Profissional': 12,
    'Módulo 9 — Liderança': 13,
}


def test_cria_9_modulos_e_140_aulas(app):
    with app.app_context():
        r = treino_seed.importar_universidade()
        assert r == {'trilhas': 9, 'aulas': 140}
        trilhas = TreinoTrilha.query.order_by(TreinoTrilha.ordem).all()
        assert [t.nome for t in trilhas] == list(ESPERADO_POR_MODULO)
        for t in trilhas:
            assert t.ativa is False            # dono liga após subir vídeos
            assert (t.descricao or '').strip()
            assert len(t.videos) == ESPERADO_POR_MODULO[t.nome]
            # Aulas na ordem do dono, sem vídeo e em RASCUNHO. Publicar o
            # módulo não pode expor os 140 títulos de uma vez.
            assert [v.ordem for v in t.videos] == list(range(len(t.videos)))
            assert all(v.video_externo_id is None for v in t.videos)
            assert not any(v.ativo for v in t.videos)
        assert TreinoVideo.query.count() == 140
        assert AppConfig.get(treino_seed.CFG_SEED) == '1'


def test_conteudo_amostral_na_ordem_do_dono(app):
    with app.app_context():
        treino_seed.importar_universidade()
        cultura = TreinoTrilha.query.filter_by(
            nome='Módulo 1 — Cultura').first()
        assert cultura.videos[0].titulo == 'Nossa história'
        assert (cultura.videos[-1].titulo
                == 'O que não toleramos (comportamentos proibidos)')
        lider = TreinoTrilha.query.filter_by(
            nome='Módulo 9 — Liderança').first()
        assert lider.videos[0].titulo == 'Papel do líder'
        assert lider.videos[-1].titulo == 'Cultura através da liderança'


def test_seed_roda_uma_vez_e_nao_ressuscita(app):
    """Apagar aula/módulo depois do seed é decisão do dono — não volta."""
    with app.app_context():
        treino_seed.importar_universidade()
        cultura = TreinoTrilha.query.filter_by(
            nome='Módulo 1 — Cultura').first()
        db.session.delete(cultura.videos[0])       # dono apagou uma aula
        db.session.commit()
        r2 = treino_seed.importar_universidade()
        assert r2 == {'trilhas': 0, 'aulas': 0}
        assert TreinoVideo.query.count() == 139    # não ressuscitou


def test_forcar_nao_duplica(app):
    """`forcar=True` ignora só o guard — a dedup impede duplicata."""
    with app.app_context():
        treino_seed.importar_universidade()
        r2 = treino_seed.importar_universidade(forcar=True)
        assert r2 == {'trilhas': 0, 'aulas': 0}
        assert TreinoTrilha.query.count() == 9
        assert TreinoVideo.query.count() == 140


def test_trilha_homonima_do_dono_e_reusada_sem_mexer_na_ativa(app):
    """Trilha já criada pelo dono com o mesmo nome (acento/caixa diferentes)
    é REUSADA: ganha só as aulas que faltam e a `ativa` dele fica como está."""
    with app.app_context():
        t = TreinoTrilha(nome='MODULO 5 — CAIXA E SISTEMAS', ordem=1,
                         ativa=True)
        db.session.add(t)
        db.session.flush()
        db.session.add(TreinoVideo(trilha_id=t.id, titulo='PIX', ordem=0,
                                   ativo=True))
        db.session.commit()
        r = treino_seed.importar_universidade()
        assert r['trilhas'] == 8                   # a homônima foi reusada
        assert TreinoTrilha.query.count() == 9
        db.session.refresh(t)
        assert t.ativa is True                     # decisão do dono intocada
        titulos = [v.titulo for v in t.videos]
        assert titulos.count('PIX') == 1           # 'Pix' do seed não duplicou
        assert len(titulos) == 15                  # 1 do dono + 14 que faltavam
