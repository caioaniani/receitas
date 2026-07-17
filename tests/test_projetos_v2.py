"""Projetos v2 (17/07/2026): Início orientado a ação + navegação enxuta.

- `/projetos/` (Início) mostra o bloco "Agora" (fazendo/atrasadas/hoje/
  próximos 7 dias, cada tarefa UMA vez) + quadro por área com as tarefas
  abertas aninhadas; projeto concluído fica fora do quadro.
- A home antiga de cards continua viva em `/projetos/cards` e as demais
  views seguem respondendo (nenhuma rota foi perdida).
- Recorrência: dedupe (re-concluir a mesma tarefa não duplica a próxima
  ocorrência) e o drag do kanban (`/mover`) também agenda a próxima.
- Copilot `executar_criar_tarefa`: cria de verdade (a versão anterior
  passava kwargs inexistentes e projeto_id NULL — sempre estourava),
  caindo na Inbox quando não há projeto.
"""

from datetime import timedelta

from app.utils import hoje as hoje_brt


def _login(client, uid):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)
        sess['_fresh'] = True


def _montar_quadro(app):
    """Área + 2 projetos (1 aberto com tarefas variadas, 1 concluído)."""
    from app.extensions import db
    from app.models import Projeto, ProjetoArea, TarefaProjeto
    hoje_d = hoje_brt()
    area = ProjetoArea(nome='Padaria', tipo='empresa')
    db.session.add(area)
    db.session.flush()
    p = Projeto(area_id=area.id, nome='Reforma da loja', status='ativo')
    p_done = Projeto(area_id=area.id, nome='Projeto encerrado', status='concluido')
    db.session.add_all([p, p_done])
    db.session.flush()
    tarefas = {
        'atrasada': TarefaProjeto(projeto_id=p.id, nome='Tarefa atrasada x',
                                  status='a_fazer', prazo=hoje_d - timedelta(days=3)),
        'fazendo': TarefaProjeto(projeto_id=p.id, nome='Tarefa em andamento y',
                                 status='fazendo'),
        'hoje': TarefaProjeto(projeto_id=p.id, nome='Tarefa de hoje z',
                              status='a_fazer', prazo=hoje_d),
        'semana': TarefaProjeto(projeto_id=p.id, nome='Tarefa da semana w',
                                status='a_fazer', prazo=hoje_d + timedelta(days=4)),
        'feita': TarefaProjeto(projeto_id=p.id, nome='Tarefa ja feita q',
                               status='feito'),
    }
    db.session.add_all(tarefas.values())
    db.session.commit()
    return area, p, p_done, tarefas


def test_inicio_mostra_agora_e_quadro(app, owner_user):
    _, p, p_done, tarefas = _montar_quadro(app)
    c = app.test_client()
    _login(c, owner_user.id)
    r = c.get('/projetos/')
    assert r.status_code == 200
    html = r.data.decode()

    # Bloco Agora com as 4 seções e as tarefas certas
    assert 'Fazendo agora' in html and 'Tarefa em andamento y' in html
    assert 'Atrasadas' in html and 'Tarefa atrasada x' in html
    assert 'Para hoje' in html and 'Tarefa de hoje z' in html
    assert 'Próximos 7 dias' in html and 'Tarefa da semana w' in html

    # Quadro por área: projeto aberto aparece; concluído fica fora
    assert 'Reforma da loja' in html
    assert 'Projeto encerrado' not in html
    # Tarefa feita não aparece nas abertas do quadro
    assert 'Tarefa ja feita q' not in html
    # Botão de concluir em 1 clique presente
    assert 'proj-done' in html


def test_tarefa_nao_duplica_entre_secoes_do_agora(app, owner_user):
    """Tarefa FAZENDO com prazo vencido aparece só em 'Fazendo agora'
    (cada tarefa uma vez no Agora)."""
    from app.extensions import db
    from app.models import Projeto, ProjetoArea, TarefaProjeto
    area = ProjetoArea(nome='Padaria', tipo='empresa')
    db.session.add(area)
    db.session.flush()
    p = Projeto(area_id=area.id, nome='P1', status='ativo')
    db.session.add(p)
    db.session.flush()
    db.session.add(TarefaProjeto(projeto_id=p.id, nome='Fazendo vencida k',
                                 status='fazendo',
                                 prazo=hoje_brt() - timedelta(days=1)))
    db.session.commit()

    c = app.test_client()
    _login(c, owner_user.id)
    html = c.get('/projetos/').data.decode()
    # Aparece na seção "Fazendo agora"; a seção "Atrasadas" nem renderiza
    # (a única tarefa vencida está em fazendo, então não é re-listada lá).
    assert 'Fazendo agora (1)' in html
    assert 'Atrasadas (' not in html


def test_rotas_antigas_continuam_vivas(app, owner_user):
    _montar_quadro(app)
    c = app.test_client()
    _login(c, owner_user.id)
    for rota in ('/projetos/cards', '/projetos/lista', '/projetos/hoje',
                 '/projetos/dia', '/projetos/inbox', '/projetos/kanban',
                 '/projetos/foco', '/projetos/calendario',
                 '/projetos/relatorio', '/projetos/templates'):
        r = c.get(rota)
        assert r.status_code == 200, f'{rota} devolveu {r.status_code}'
    # Cards antigos ficam acessíveis pelo menu "Mais"
    html = c.get('/projetos/').data.decode()
    assert '/projetos/cards' in html
    assert 'Início' in html and 'Mais' in html


def test_nao_owner_nao_acessa(app, admin_user):
    c = app.test_client()
    _login(c, admin_user.id)
    assert c.get('/projetos/').status_code == 403


def test_recorrencia_cria_proxima_ao_concluir(app, owner_user):
    from app.models import TarefaProjeto
    _, p, _, tarefas = _montar_quadro(app)
    t = tarefas['hoje']
    t.recorrencia = 'semanal'
    from app.extensions import db
    db.session.commit()

    c = app.test_client()
    _login(c, owner_user.id)
    r = c.post(f'/projetos/tarefa/{t.id}/editar',
               data={'campo': 'status', 'valor': 'feito'})
    assert r.status_code == 200 and r.get_json()['ok']

    abertas = TarefaProjeto.query.filter(
        TarefaProjeto.nome == t.nome,
        ~TarefaProjeto.status.in_(['feito', 'cancelado']),
    ).all()
    assert len(abertas) == 1
    assert abertas[0].prazo == t.prazo + timedelta(days=7)


def test_recorrencia_nao_duplica_em_reconclusao(app, owner_user):
    """feito → a_fazer → feito de novo NÃO cria segunda ocorrência aberta
    (era o lixo de duplicatas observado no quadro em prod)."""
    from app.extensions import db
    from app.models import TarefaProjeto
    _, p, _, tarefas = _montar_quadro(app)
    t = tarefas['hoje']
    t.recorrencia = 'semanal'
    db.session.commit()

    c = app.test_client()
    _login(c, owner_user.id)
    for valor in ('feito', 'a_fazer', 'feito'):
        r = c.post(f'/projetos/tarefa/{t.id}/editar',
                   data={'campo': 'status', 'valor': valor})
        assert r.get_json()['ok']

    abertas = TarefaProjeto.query.filter(
        TarefaProjeto.nome == t.nome,
        TarefaProjeto.id != t.id,
        ~TarefaProjeto.status.in_(['feito', 'cancelado']),
    ).count()
    assert abertas == 1


def test_recorrencia_dispara_no_mover_do_kanban(app, owner_user):
    """Arrastar pra 'feito' no kanban agenda a próxima ocorrência
    (antes só o clique no círculo/modal agendava — inconsistência)."""
    from app.extensions import db
    from app.models import TarefaProjeto
    _, p, _, tarefas = _montar_quadro(app)
    t = tarefas['semana']
    t.recorrencia = 'mensal'
    db.session.commit()

    c = app.test_client()
    _login(c, owner_user.id)
    r = c.post(f'/projetos/tarefa/{t.id}/mover', data={'status': 'feito'})
    assert r.status_code == 200 and r.get_json()['ok']

    nova = TarefaProjeto.query.filter(
        TarefaProjeto.nome == t.nome,
        TarefaProjeto.id != t.id,
    ).first()
    assert nova is not None
    assert nova.status == 'a_fazer'
    assert nova.prazo == t.prazo + timedelta(days=30)


def test_copilot_criar_tarefa_cai_na_inbox(app, owner_user):
    """Sem projeto_nome, a tarefa nasce no projeto Avulsas (Inbox) —
    a versão anterior quebrava (kwargs inexistentes + projeto_id NULL)."""
    from app.models import TarefaProjeto
    from app.services.copilot import executar_criar_tarefa

    amanha = (hoje_brt() + timedelta(days=1)).isoformat()
    res = executar_criar_tarefa(
        {'titulo': 'Ligar pro fornecedor de farinha', 'data_prazo': amanha},
        owner_user)
    assert res['ok'], res
    assert res['projeto'] == 'Avulsas'

    t = TarefaProjeto.query.get(res['tarefa_id'])
    assert t.nome == 'Ligar pro fornecedor de farinha'
    assert t.prazo.isoformat() == amanha
    assert t.projeto.nome == 'Avulsas'


def test_copilot_criar_tarefa_em_projeto_existente(app, owner_user):
    from app.models import TarefaProjeto
    from app.services.copilot import executar_criar_tarefa
    _, p, _, _ = _montar_quadro(app)

    res = executar_criar_tarefa(
        {'titulo': 'Comprar tinta', 'projeto_nome': 'reforma'}, owner_user)
    assert res['ok'], res
    assert res['projeto'] == 'Reforma da loja'
    t = TarefaProjeto.query.get(res['tarefa_id'])
    assert t.projeto_id == p.id
    assert t.prazo is None
