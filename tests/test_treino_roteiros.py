"""Import do plano de conteúdo do treinamento (roteiros xlsx) — 13/08/2026.

Módulo vira trilha, aula vira TreinoVideo RASCUNHO com o roteiro anexado.
Tudo nasce desativado; re-importar atualiza sem duplicar e sem tocar em
aula com vídeo gravado.
"""
import io

from openpyxl import Workbook

from app.extensions import db
from app.models import TreinoTrilha, TreinoVideo

CAB = ['Código', 'Módulo', 'Nº da aula', 'Título do vídeo', 'Objetivo',
       'Roteiro do vídeo', 'Demonstração / exemplo',
       'Comportamento esperado', 'Desafio da semana',
       'Duração sugerida (min)', 'Público', 'Status de produção',
       'Responsável', 'Observações']


def _xlsx(linhas):
    wb = Workbook()
    ws = wb.active
    ws.title = 'Roteiros'
    ws.append(CAB)
    for ln in linhas:
        ws.append(ln)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _linha(codigo='M01.01', modulo='Módulo 1 — Cultura', aula=1,
           titulo='Nossa história', minutos=5, publico='Todos'):
    return [codigo, modulo, aula, titulo, 'Ensinar o padrão',
            'GANCHO — abra com uma situação real', 'Grave uma situação',
            'Aplica no turno', 'Desafio: pergunte o que pode fazer',
            minutos, publico, 'Não iniciado', '', '']


def test_importar_cria_trilha_e_aulas_desativadas(app):
    from app.services import treino_roteiros
    st = treino_roteiros.importar(_xlsx([
        _linha(), _linha('M01.02', aula=2, titulo='Missão e visão'),
        _linha('M09.01', 'Módulo 9 — Liderança', 1, 'Liderar pelo exemplo',
               6, 'Líderes')]))
    assert st['trilhas_criadas'] == 2 and st['aulas_criadas'] == 3
    t1 = TreinoTrilha.query.filter_by(nome='Módulo 1 — Cultura').one()
    t9 = TreinoTrilha.query.filter_by(nome='Módulo 9 — Liderança').one()
    assert t1.ativa is False and t9.ativa is False       # rascunho: ninguém vê
    assert t1.ordem == 1 and t9.ordem == 9               # ordem = nº do módulo
    assert t1.carga_horaria_minutos == 10
    v = next(x for x in t1.videos if x.ordem == 1)
    assert v.ativo is False and v.video_externo_id is None
    assert v.duracao_segundos == 300
    assert 'GANCHO' in v.roteiro and '[M01.01]' in v.roteiro
    assert 'DESAFIO DA SEMANA' in v.roteiro


def test_reimportar_atualiza_sem_duplicar(app):
    from app.services import treino_roteiros
    treino_roteiros.importar(_xlsx([_linha()]))
    st = treino_roteiros.importar(_xlsx([
        _linha(titulo='Nossa história (rev 2)', minutos=7)]))
    assert st['trilhas_criadas'] == 0 and st['aulas_criadas'] == 0
    assert st['roteiros_atualizados'] == 0               # roteiro igual
    assert TreinoVideo.query.count() == 1
    v = TreinoVideo.query.one()
    assert v.titulo == 'Nossa história (rev 2)'          # rascunho acompanha
    assert v.duracao_segundos == 420


def test_aula_com_video_gravado_preserva_titulo_e_duracao(app):
    """Produção no ar não muda por planilha — só o roteiro acompanha."""
    from app.services import treino_roteiros
    treino_roteiros.importar(_xlsx([_linha()]))
    v = TreinoVideo.query.one()
    v.video_externo_id = 'a' * 32
    v.duracao_segundos = 313                             # detectada no stream
    v.titulo = 'Nossa história (como gravado)'
    db.session.commit()
    linha_rev = _linha(titulo='Nossa história v3', minutos=9)
    linha_rev[5] = 'GANCHO NOVO — roteiro revisado'
    st = treino_roteiros.importar(_xlsx([linha_rev]))
    assert st['aulas_com_video_preservadas'] == 1
    assert st['roteiros_atualizados'] == 1
    db.session.refresh(v)
    assert v.titulo == 'Nossa história (como gravado)'   # intocado
    assert v.duracao_segundos == 313                     # intocado
    assert 'GANCHO NOVO' in v.roteiro                    # roteiro acompanha


def test_importar_nao_reativa_nem_desativa(app):
    from app.services import treino_roteiros
    treino_roteiros.importar(_xlsx([_linha()]))
    t = TreinoTrilha.query.one()
    t.ativa = True                                       # dono ativou o módulo
    db.session.commit()
    treino_roteiros.importar(_xlsx([_linha(minutos=6)]))
    db.session.refresh(t)
    assert t.ativa is True                               # import não desativa


def test_trilha_existente_com_mesmo_nome_e_reusada(app):
    from app.services import treino_roteiros
    db.session.add(TreinoTrilha(nome='Módulo 1 — Cultura', ordem=3,
                                ativa=True))
    db.session.commit()
    st = treino_roteiros.importar(_xlsx([_linha()]))
    assert st['trilhas_criadas'] == 0
    assert TreinoTrilha.query.count() == 1


def test_linha_torta_vira_aviso_e_nao_some(app):
    from app.services import treino_roteiros
    ruim = _linha(codigo='M01.99', titulo='')
    st = treino_roteiros.importar(_xlsx([_linha(), ruim]))
    assert st['aulas_criadas'] == 1
    assert any('M01.99' in a for a in st['avisos'])


def test_planilha_real_do_dono(app):
    """A planilha REAL (9 módulos, 140 aulas) importa inteira e sem avisos."""
    from app.services import treino_roteiros
    raw = open('/root/.claude/uploads/80083e22-11d6-5981-8fcc-1d3374ee28b5/'
               '82a1ce27-roteiros_treinamento_9_modulos.xlsx', 'rb').read()
    st = treino_roteiros.importar(raw)
    assert st['trilhas_criadas'] == 9
    assert st['aulas_criadas'] == 140
    assert not st['avisos']
    assert TreinoVideo.query.filter(TreinoVideo.ativo.is_(True)).count() == 0


def _login(c, user_id):
    with c.session_transaction() as s:
        s['_user_id'] = str(user_id)
        s['_fresh'] = True


def test_rota_importa_pelo_admin(app, admin_user):
    uid = admin_user.id
    c = app.test_client()
    _login(c, uid)
    r = c.post('/treino/admin/roteiros/importar',
               data={'arquivo': (io.BytesIO(_xlsx([_linha()])),
                                 'roteiros.xlsx')},
               content_type='multipart/form-data', follow_redirects=True)
    assert r.status_code == 200
    assert TreinoVideo.query.count() == 1


def test_rota_exige_login(app):
    r = app.test_client().post('/treino/admin/roteiros/importar')
    assert r.status_code in (302, 401, 403)
