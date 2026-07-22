"""Treinamento — lado do FUNCIONÁRIO: correção do quiz, progresso,
elegibilidade e geração de acesso pelo RH. E-mail (Postmark) sempre mockado."""
import pytest

from app.extensions import db
from app.models import (
    Funcionario,
    Treinamento,
    TreinamentoOpcao,
    TreinamentoPergunta,
    Usuario,
)
from app.services import treinamento as svc


def _treino(titulo='T', nota=70, n_perg=2, ativo=True):
    """Cria um treinamento com `n_perg` perguntas (2 opções, a 1ª correta)."""
    t = Treinamento(titulo=titulo, nota_minima=nota, ativo=ativo)
    db.session.add(t)
    db.session.flush()
    for i in range(n_perg):
        p = TreinamentoPergunta(treinamento_id=t.id, enunciado=f'Q{i}', ordem=i)
        db.session.add(p)
        db.session.flush()
        db.session.add_all([
            TreinamentoOpcao(pergunta_id=p.id, texto='certa', correta=True, ordem=0),
            TreinamentoOpcao(pergunta_id=p.id, texto='errada', correta=False, ordem=1),
        ])
    db.session.commit()
    return t


def _func_user(nome='Aluno', papel='funcionario'):
    u = Usuario(nome=nome, login=f'{nome.lower()}-treino', papel=papel)
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    return u


def _respostas_certas(t):
    return {p.id: next(o.id for o in p.opcoes if o.correta) for p in t.perguntas}


def _resposta_uma_errada(t):
    r = _respostas_certas(t)
    p0 = t.perguntas[0]
    r[p0.id] = next(o.id for o in p0.opcoes if not o.correta)
    return r


def test_corrigir_aprova_e_atualiza_conclusao(app):
    with app.app_context():
        t = _treino(nota=70)
        u = _func_user()
        res = svc.corrigir_e_registrar(t, u, _respostas_certas(t))
        assert res['acertos'] == 2 and res['percentual'] == 100 and res['aprovado']
        c = svc.conclusao_de(u.id, t.id)
        assert c.aprovado_em is not None and c.melhor_pontos == 2


def test_corrigir_reprova_abaixo_da_nota(app):
    with app.app_context():
        t = _treino(nota=70)
        u = _func_user()
        res = svc.corrigir_e_registrar(t, u, _resposta_uma_errada(t))
        assert res['percentual'] == 50 and not res['aprovado']
        c = svc.conclusao_de(u.id, t.id)
        assert c.aprovado_em is None and c.melhor_pontos == 1


def test_marcar_assistido_uma_vez(app):
    with app.app_context():
        t = _treino()
        u = _func_user()
        svc.marcar_assistido(t, u)
        c1 = svc.conclusao_de(u.id, t.id)
        primeiro = c1.assistido_em
        assert primeiro is not None
        svc.marcar_assistido(t, u)
        assert svc.conclusao_de(u.id, t.id).assistido_em == primeiro


def test_completo_exige_assistido_e_aprovado(app):
    with app.app_context():
        t = _treino()
        u = _func_user()
        svc.corrigir_e_registrar(t, u, _respostas_certas(t))   # aprovado, sem assistir
        prog = {p['treinamento'].id: p for p in svc.progresso(u)}
        assert prog[t.id]['aprovado'] and not prog[t.id]['completo']
        svc.marcar_assistido(t, u)
        prog = {p['treinamento'].id: p for p in svc.progresso(u)}
        assert prog[t.id]['completo']


def test_elegivel_so_quem_completou_todos(app):
    with app.app_context():
        t1, t2 = _treino('A'), _treino('B')
        completo = _func_user('Completo')
        parcial = _func_user('Parcial')
        for t in (t1, t2):
            svc.marcar_assistido(t, completo)
            svc.corrigir_e_registrar(t, completo, _respostas_certas(t))
        # parcial só faz o t1
        svc.marcar_assistido(t1, parcial)
        svc.corrigir_e_registrar(t1, parcial, _respostas_certas(t1))
        nomes = [e['usuario'].nome for e in svc.elegiveis()]
        assert 'Completo' in nomes and 'Parcial' not in nomes


def test_quiz_exige_assistido_primeiro(app, admin_user):
    with app.app_context():
        t = _treino()
        u = _func_user()
        uid, tid = u.id, t.id
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(uid)
        s['_fresh'] = True
    # tenta responder sem ter marcado assistido -> redireciona sem gravar
    r = c.post(f'/treinamento/{tid}/quiz', data={})
    assert r.status_code == 302
    with app.app_context():
        from app.models import TreinamentoTentativa
        assert TreinamentoTentativa.query.filter_by(treinamento_id=tid).count() == 0


def test_fluxo_completo_do_funcionario(app):
    with app.app_context():
        t = _treino()
        u = _func_user()
        uid, tid = u.id, t.id
        certas = _respostas_certas(t)
        # "assistido" agora vem do rastreio real de vídeo (testado à parte);
        # aqui o foco é o quiz, então marco pelo serviço.
        svc.marcar_assistido(t, u)
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(uid)
        s['_fresh'] = True
    assert c.get('/treinamento/').status_code == 200
    assert c.get(f'/treinamento/{tid}/assistir').status_code == 200
    data = {f'pergunta_{pid}': str(oid) for pid, oid in certas.items()}
    r = c.post(f'/treinamento/{tid}/quiz', data=data)
    assert r.status_code == 200 and 'Passou' in r.get_data(as_text=True)
    with app.app_context():
        assert svc.conclusao_de(uid, tid).aprovado_em is not None


def test_gerar_acesso_cria_conta_e_vincula(app, admin_user, monkeypatch):
    enviados = {}

    def _fake(*a, **k):
        enviados['ok'] = True
        return {'ok': True}
    monkeypatch.setattr('app.services.email.enviar_boas_vindas', _fake)
    with app.app_context():
        f = Funcionario(nome='José', cpf='111.111.111-11', email='jose@opao.online')
        db.session.add(f)
        db.session.commit()
        r = svc.gerar_acesso(f)
        assert r['ok'] and r['motivo'] == 'criado'
        db.session.refresh(f)
        assert f.usuario_id and f.usuario.login == 'jose@opao.online'
        assert f.usuario.papel == 'funcionario'
        # idempotente: 2ª vez não recria
        assert svc.gerar_acesso(f)['motivo'] == 'ja_tem'
    assert enviados.get('ok')


def test_gerar_acesso_recusa_conta_de_admin(app):
    """E-mail que já é de um admin NÃO vincula (seria a conta errada)."""
    with app.app_context():
        chefe = Usuario(nome='Chefe', login='chefe@opao.online',
                        email='chefe@opao.online', papel='admin')
        chefe.set_senha('x' * 8)
        db.session.add(chefe)
        f = Funcionario(nome='Xará', cpf='444.444.444-44',
                        email='chefe@opao.online')
        db.session.add(f)
        db.session.commit()
        r = svc.gerar_acesso(f)
        assert r['motivo'] == 'conta_de_outro_papel'
        db.session.refresh(f)
        assert f.usuario_id is None


def test_nota_de_corte_compara_sem_arredondar(app):
    """2/3 = 66,66% (arredonda a 67) NÃO passa com nota mínima 67."""
    with app.app_context():
        t = _treino(nota=67, n_perg=3)
        u = _func_user()
        r = _respostas_certas(t)
        p2 = t.perguntas[2]
        r[p2.id] = next(o.id for o in p2.opcoes if not o.correta)   # erra 1
        res = svc.corrigir_e_registrar(t, u, r)
        assert res['acertos'] == 2 and res['percentual'] == 67
        assert not res['aprovado']


def test_treinamento_sem_quiz_completa_ao_assistir(app):
    """Treinamento só-vídeo (0 perguntas) não pode travar a elegibilidade —
    completa ao assistir."""
    with app.app_context():
        t = _treino('SoVideo', n_perg=0)
        u = _func_user()
        svc.marcar_assistido(t, u)
        prog = {p['treinamento'].id: p for p in svc.progresso(u)}
        assert prog[t.id]['completo']
        assert 'Aluno' in [e['usuario'].nome for e in svc.elegiveis()]


def test_gerar_acesso_sem_email_recusa(app):
    with app.app_context():
        f = Funcionario(nome='Sem Mail', cpf='222.222.222-22', email=None)
        db.session.add(f)
        db.session.commit()
        assert svc.gerar_acesso(f)['motivo'] == 'sem_email'
        db.session.refresh(f)
        assert f.usuario_id is None


def test_admin_acessos_e_elegiveis_renderizam(app, admin_user):
    with app.app_context():
        _treino('Aula')
        db.session.add(Funcionario(nome='Ana', cpf='333.333.333-33',
                                   email='ana@opao.online'))
        db.session.commit()
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(admin_user.id)
        s['_fresh'] = True
    assert c.get('/treinamento/admin/acessos').status_code == 200
    assert c.get('/treinamento/admin/elegiveis').status_code == 200


@pytest.mark.parametrize('rota', ['/treinamento/', '/treinamento/admin'])
def test_rotas_exigem_login(app, rota):
    assert app.test_client().get(rota).status_code in (302, 401)
