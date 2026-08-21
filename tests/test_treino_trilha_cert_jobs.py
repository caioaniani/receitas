"""Fases 7-8 — conclusão de trilha, certificado (§11, critério 20) e jobs
(§13, critério 16).
"""
from datetime import datetime, time, timedelta

from app.extensions import db
from app.models import (
    Funcionario,
    Loja,
    TreinoAplicacaoPratica,
    TreinoProgressoVideo,
    TreinoQuiz,
    TreinoTemporada,
    TreinoTentativaQuiz,
    TreinoTrilha,
    TreinoVideo,
)
from app.services import treino_certificado as cert
from app.services import treino_jobs as jobs
from app.services import treino_ledger as ledger
from app.services import treino_trilha as tt
from app.utils import agora, hoje


def _cenario_trilha_completa():
    temp = TreinoTemporada(nome='T', inicio=hoje() - timedelta(days=1),
                           fim=hoje() + timedelta(days=30), status='ATIVA')
    loja = Loja(nome='Brooklin', ativa=True)
    db.session.add_all([temp, loja])
    db.session.commit()
    f = Funcionario(nome='Ana', cpf='2', ativo=True)
    g = Funcionario(nome='Gestor', cpf='1', ativo=True)
    f.lojas.append(loja)
    trilha = TreinoTrilha(nome='Segurança Alimentar')
    db.session.add_all([f, g, trilha])
    db.session.commit()
    v = TreinoVideo(trilha_id=trilha.id, titulo='Higiene das mãos',
                    duracao_segundos=60, ordem=1,
                    video_externo_id='1' * 32)
    quiz = TreinoQuiz(trilha_id=trilha.id, titulo='Prova', ativo=True)
    db.session.add_all([v, quiz])
    db.session.commit()
    # vídeo concluído + quiz aprovado + aplicação registrada
    db.session.add(TreinoProgressoVideo(
        funcionario_id=f.id, video_id=v.id, versao_video=1,
        concluido_em=agora()))
    db.session.add(TreinoTentativaQuiz(
        funcionario_id=f.id, quiz_id=quiz.id, numero_tentativa=1,
        questoes_sorteadas=[], aprovada=True, finalizado_em=agora()))
    db.session.add(TreinoAplicacaoPratica(
        funcionario_id=f.id, trilha_id=trilha.id, gestor_id=g.id,
        temporada_id=temp.id, data=hoje(), evidencia='aplicou certo' * 3,
        status='REGISTRADA'))
    db.session.commit()
    return temp, f, trilha


def test_trilha_completa_emite_selo_e_credita_100(app):
    with app.app_context():
        temp, f, trilha = _cenario_trilha_completa()
        selo = tt.verificar_conclusao(f, trilha, temp)
        assert selo is not None
        assert ledger.saldo(f.id, temp.id) == 100        # TRILHA_CONCLUIDA
        assert selo.carga_horaria_minutos == 1           # 60s -> 1 min
        # idempotente: não emite 2 selos nem credita de novo
        selo2 = tt.verificar_conclusao(f, trilha, temp)
        assert selo2.id == selo.id
        assert ledger.saldo(f.id, temp.id) == 100


def test_certificado_pdf_e_verificacao(app):   # critério 20
    with app.app_context():
        temp, f, trilha = _cenario_trilha_completa()
        selo = tt.verificar_conclusao(f, trilha, temp)
        d = cert.dados_certificado(selo)
        assert 'Higiene das mãos' in d['conteudo']        # conteúdo programático
        assert d['carga_horaria_minutos'] == 1            # carga horária
        pdf = cert.gerar_pdf(selo)
        assert pdf[:4] == b'%PDF'
        # código de verificação resolve na rota pública
        assert cert.por_codigo(selo.codigo_verificacao).id == selo.id


def test_fechamento_semanal_idempotente(app):   # critério 16
    with app.app_context():
        temp = TreinoTemporada(nome='T', inicio=hoje() - timedelta(days=10),
                               fim=hoje() + timedelta(days=30), status='ATIVA')
        loja = Loja(nome='Brooklin', ativa=True)
        db.session.add_all([temp, loja])
        db.session.commit()
        f = Funcionario(nome='Ana', cpf='2', ativo=True)
        f.lojas.append(loja)
        trilha = TreinoTrilha(nome='T')
        db.session.add_all([f, trilha])
        db.session.commit()
        v = TreinoVideo(trilha_id=trilha.id, titulo='V', duracao_segundos=60)
        quiz = TreinoQuiz(trilha_id=trilha.id, titulo='Q', ativo=True)
        db.session.add_all([v, quiz])
        db.session.commit()
        # meta na semana ISO de hoje: 1 vídeo concluído + 1 quiz aprovado
        quando = datetime.combine(hoje(), time(12, 0))
        db.session.add(TreinoProgressoVideo(
            funcionario_id=f.id, video_id=v.id, versao_video=1,
            concluido_em=quando))
        db.session.add(TreinoTentativaQuiz(
            funcionario_id=f.id, quiz_id=quiz.id, numero_tentativa=1,
            questoes_sorteadas=[], aprovada=True, finalizado_em=quando))
        db.session.commit()
        ano, semana, _ = hoje().isocalendar()
        jobs.fechamento_semanal(ano, semana)
        assert ledger.saldo(f.id, temp.id) == 15          # STREAK_SEMANAL
        jobs.fechamento_semanal(ano, semana)              # roda de novo
        assert ledger.saldo(f.id, temp.id) == 15          # NÃO dobra


def test_encerramento_temporada_vencida(app):
    with app.app_context():
        vencida = TreinoTemporada(nome='V', inicio=hoje() - timedelta(days=40),
                                  fim=hoje() - timedelta(days=1), status='ATIVA')
        viva = TreinoTemporada(nome='A', inicio=hoje() - timedelta(days=1),
                               fim=hoje() + timedelta(days=30), status='ATIVA')
        db.session.add_all([vencida, viva])
        db.session.commit()
        assert jobs.encerramento_temporada() == 1
        assert db.session.get(TreinoTemporada, vencida.id).status == 'ENCERRADA'
        assert db.session.get(TreinoTemporada, viva.id).status == 'ATIVA'


def test_snapshot_ranking_grava(app):
    with app.app_context():
        from app.models import AppConfig, Funcionario, Loja
        temp = TreinoTemporada(nome='A', inicio=hoje() - timedelta(days=1),
                               fim=hoje() + timedelta(days=30), status='ATIVA')
        loja = Loja(nome='Brooklin', ativa=True)
        db.session.add_all([temp, loja])
        db.session.commit()
        f = Funcionario(nome='Ana', cpf='9', ativo=True)
        f.lojas.append(loja)
        db.session.add(f)
        db.session.commit()
        ledger.creditar(f, 'AJUSTE_MANUAL', 100, temporada=temp)
        assert jobs.snapshot_ranking() == 1
        assert db.session.get(
            AppConfig, f'treino_ranking_{hoje().isoformat()}') is not None


def test_certificado_translitera_travessao_e_aspas_curvas():
    """Achado de revisão 12/08/2026 (seed da Universidade): os módulos usam
    travessão ("Módulo 1 — Cultura") e aspas curvas — no latin-1 do FPDF
    viravam '?' no certificado RDC 216 (documento de fiscalização). O _s
    translitera antes do encode; emoji segue virando '?'."""
    from app.services.treino_certificado import _s
    assert _s('Módulo 1 — Cultura') == 'Módulo 1 - Cultura'
    assert (_s('Princípio: “Como você atenderia sua mãe?”')
            == 'Princípio: "Como você atenderia sua mãe?"')
    assert _s('08:00–09:00') == '08:00-09:00'
    assert _s("D’Ávila") == "D'Ávila"
    assert _s('café ☕') == 'café ?'          # sem equivalente: segue '?'
    assert _s(None) == ''
