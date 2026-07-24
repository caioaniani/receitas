"""Pré-cadastro de funcionário por QR (23/07/2026): formulário público →
lista no RH → promover pra Funcionario."""
from app.extensions import db
from app.models import Funcionario, PreCadastroFuncionario
from app.services import precadastro as svc

_OK = {'nome': 'Ana', 'sobrenome': 'Souza', 'email': 'ana@exemplo.com',
       'telefone': '11987654321'}


def _admin(app, owner_user):
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(owner_user.id)
        s['_fresh'] = True
    return c


# ── Formulário público ────────────────────────────────────────────────────

def test_form_publico_abre_sem_login(app):
    r = app.test_client().get('/cadastro-funcionario')
    assert r.status_code == 200
    assert b'Sobrenome' in r.data


def test_post_valido_cria_precadastro(app):
    r = app.test_client().post('/cadastro-funcionario', data=_OK)
    assert r.status_code == 200 and 'Cadastro recebido' in r.get_data(as_text=True)
    with app.app_context():
        p = PreCadastroFuncionario.query.filter_by(email='ana@exemplo.com').first()
        assert p is not None
        assert p.nome == 'Ana' and p.sobrenome == 'Souza'
        assert p.processado_em is None


def test_email_invalido_recusa(app):
    r = app.test_client().post('/cadastro-funcionario',
                               data=dict(_OK, email='naoehemail'))
    assert r.status_code == 400
    with app.app_context():
        assert PreCadastroFuncionario.query.count() == 0


def test_telefone_invalido_recusa(app):
    r = app.test_client().post('/cadastro-funcionario',
                               data=dict(_OK, telefone='123'))
    assert r.status_code == 400
    with app.app_context():
        assert PreCadastroFuncionario.query.count() == 0


def test_reenvio_mesmo_email_nao_duplica(app):
    cli = app.test_client()
    cli.post('/cadastro-funcionario', data=_OK)
    cli.post('/cadastro-funcionario', data=dict(_OK, sobrenome='Souza Lima'))
    with app.app_context():
        rows = PreCadastroFuncionario.query.filter_by(email='ana@exemplo.com').all()
        assert len(rows) == 1 and rows[0].sobrenome == 'Souza Lima'   # atualizou


# ── RH: QR + promover ─────────────────────────────────────────────────────

def test_tela_rh_exige_rh(app):
    assert app.test_client().get('/rh/pre-cadastros').status_code in (302, 403)


def test_tela_rh_mostra_qr_e_pendentes(app, owner_user):
    with app.app_context():
        svc.criar(dict(_OK))
    c = _admin(app, owner_user)
    r = c.get('/rh/pre-cadastros')
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'data:image/png;base64' in body and 'Ana Souza' in body


def test_promover_cria_funcionario_pendente(app, owner_user):
    with app.app_context():
        pid = svc.criar(dict(_OK)).id
    c = _admin(app, owner_user)
    c.post(f'/rh/pre-cadastros/{pid}/promover', data={'cpf': '123.456.789-00'})
    with app.app_context():
        p = db.session.get(PreCadastroFuncionario, pid)
        assert p.processado_em is not None and p.funcionario_id is not None
        f = db.session.get(Funcionario, p.funcionario_id)
        assert f.nome == 'Ana Souza' and f.email == 'ana@exemplo.com'
        assert f.telefone == '11987654321' and f.cadastro_pendente is True


def test_promover_sem_cpf_recusa(app, owner_user):
    with app.app_context():
        pid = svc.criar(dict(_OK)).id
    c = _admin(app, owner_user)
    c.post(f'/rh/pre-cadastros/{pid}/promover', data={'cpf': ''})
    with app.app_context():
        assert db.session.get(PreCadastroFuncionario, pid).processado_em is None
        assert Funcionario.query.count() == 0


def test_promover_cpf_duplicado_recusa(app, owner_user):
    with app.app_context():
        db.session.add(Funcionario(nome='Outro', cpf='111', ativo=True))
        db.session.commit()
        pid = svc.criar(dict(_OK)).id
    c = _admin(app, owner_user)
    c.post(f'/rh/pre-cadastros/{pid}/promover', data={'cpf': '111'})
    with app.app_context():
        assert db.session.get(PreCadastroFuncionario, pid).processado_em is None


def test_descartar_remove(app, owner_user):
    with app.app_context():
        pid = svc.criar(dict(_OK)).id
    c = _admin(app, owner_user)
    c.post(f'/rh/pre-cadastros/{pid}/descartar')
    with app.app_context():
        assert db.session.get(PreCadastroFuncionario, pid) is None


# ── Segurança / correção (achados da revisão) ─────────────────────────────

def test_nome_malicioso_nao_vira_handler_inline(app, owner_user):
    """Nome vindo do form PÚBLICO anônimo nunca pode cair num onsubmit inline
    (breakout de string = XSS na sessão do owner)."""
    mal = "x');alert(document.cookie);//"
    with app.app_context():
        svc.criar(dict(_OK, sobrenome=mal))
    c = _admin(app, owner_user)
    body = c.get('/rh/pre-cadastros').get_data(as_text=True)
    # Sem handler inline em lugar nenhum da página, e o nome só num data-attr.
    assert 'onsubmit=' not in body
    assert 'data-nome=' in body
    # A sequência de breakout com aspa literal não aparece (Jinja escapa p/ &#39;).
    assert "');alert(document.cookie)" not in body


def test_nome_com_apostrofo_renderiza(app, owner_user):
    """Nome legítimo com apóstrofo (D'Ávila) não quebra a tela."""
    with app.app_context():
        svc.criar(dict(_OK, nome="D'Ávila", sobrenome='Nunes'))
    c = _admin(app, owner_user)
    r = c.get('/rh/pre-cadastros')
    assert r.status_code == 200
    assert 'Nunes' in r.get_data(as_text=True)


def test_promover_trunca_nome_em_200(app, owner_user):
    """nome[:100] + sobrenome[:100] = até 201 chars; Funcionario.nome é 200."""
    with app.app_context():
        pid = svc.criar(dict(_OK, nome='A' * 100, sobrenome='B' * 100)).id
    c = _admin(app, owner_user)
    c.post(f'/rh/pre-cadastros/{pid}/promover', data={'cpf': '999'})
    with app.app_context():
        f = Funcionario.query.filter_by(cpf='999').first()
        assert f is not None and len(f.nome) <= 200


def test_criar_poda_processados_antigos(app):
    """Pré-cadastro já processado e velho é podado no próximo criar (PII/LGPD)."""
    from datetime import timedelta

    from app.utils import agora
    with app.app_context():
        velho = svc.criar(dict(_OK, email='velho@exemplo.com'))
        velho.processado_em = agora() - timedelta(days=svc._PODAR_PROCESSADOS_DIAS + 1)
        recente = svc.criar(dict(_OK, email='recente@exemplo.com'))
        recente.processado_em = agora()  # processado mas novo — fica
        db.session.commit()
        svc.criar(dict(_OK, email='novo@exemplo.com'))  # dispara a poda
        emails = {p.email for p in PreCadastroFuncionario.query.all()}
        assert 'velho@exemplo.com' not in emails
        assert 'recente@exemplo.com' in emails and 'novo@exemplo.com' in emails
