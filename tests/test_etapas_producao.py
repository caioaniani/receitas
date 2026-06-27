"""Etapas de producao (processo / fluxograma).

ReceitaEtapa guarda o passo a passo de cada receita (Mise en place,
Amassamento, Fermentacao, Forno...) com duracao e equipamento. O seed por
categoria preenche o padrao artesanal pesquisado; o mise_en_place expoe o
processo pro card do padeiro.
"""
from app.constants import (
    ETAPAS_PADRAO,
    ETAPAS_PADRAO_DEFAULT,
    etapas_padrao_categoria,
)
from app.extensions import db
from app.models import Receita, ReceitaEtapa
from app.services.producao import (
    _fmt_dur,
    mise_en_place,
    seed_etapas_categoria,
)


def _receita(nome='Pão Francês', categoria='Pães', modo=''):
    r = Receita(nome=nome, categoria=categoria, rendimento_qtd=10,
                rendimento_unidade='un', peso_base=1000.0, modo_preparo=modo)
    db.session.add(r)
    db.session.commit()
    return r


# ── _fmt_dur ────────────────────────────────────────────────────────────────

def test_fmt_dur():
    assert _fmt_dur(30) == '30 min'
    assert _fmt_dur(0) == '0 min'
    assert _fmt_dur(60) == '1h'
    assert _fmt_dur(120) == '2h'
    assert _fmt_dur(150) == '2,5h'
    assert _fmt_dur(2880) == '48h'


# ── padrao por categoria ─────────────────────────────────────────────────────

def test_etapas_padrao_categoria_conhecida():
    assert etapas_padrao_categoria('Pães') is ETAPAS_PADRAO['Pães']
    assert etapas_padrao_categoria('Viennoiserie') is ETAPAS_PADRAO['Viennoiserie']


def test_etapas_padrao_categoria_desconhecida_cai_no_default():
    assert etapas_padrao_categoria('Categoria Inexistente') is ETAPAS_PADRAO_DEFAULT
    assert etapas_padrao_categoria('') is ETAPAS_PADRAO_DEFAULT
    assert etapas_padrao_categoria(None) is ETAPAS_PADRAO_DEFAULT


def test_padrao_paes_tem_etapas_passivas():
    """Fermentacao longa = passiva (entre turnos, nao ocupa mao-de-obra)."""
    paes = ETAPAS_PADRAO['Pães']
    passivas = [e for e in paes if e[3] is False]
    assert passivas, 'Pães deve ter ao menos uma etapa passiva'
    # alguma etapa usa amassadeira e outra usa forno (serializam no Gantt)
    equips = {e[2] for e in paes}
    assert 'amassadeira' in equips
    assert 'forno' in equips


# ── seed_etapas_categoria ────────────────────────────────────────────────────

def test_seed_cria_etapas_na_ordem(app):
    r = _receita(categoria='Pães')
    n = seed_etapas_categoria('Pães')
    assert n == 1
    etapas = (ReceitaEtapa.query.filter_by(receita_id=r.id)
              .order_by(ReceitaEtapa.ordem).all())
    padrao = ETAPAS_PADRAO['Pães']
    assert len(etapas) == len(padrao)
    for i, (e, (nome, dur, equip, ativa)) in enumerate(zip(etapas, padrao)):
        assert e.ordem == i
        assert e.nome == nome
        assert e.duracao_min == dur
        assert e.equipamento == equip
        assert e.ativa == ativa


def test_seed_idempotente_substitui(app):
    """Re-aplicar nao duplica — substitui as etapas existentes."""
    r = _receita(categoria='Pães')
    seed_etapas_categoria('Pães')
    seed_etapas_categoria('Pães')
    etapas = ReceitaEtapa.query.filter_by(receita_id=r.id).all()
    assert len(etapas) == len(ETAPAS_PADRAO['Pães'])  # nao dobrou


def test_seed_preenche_modo_preparo_vazio(app):
    r = _receita(categoria='Pães', modo='')
    seed_etapas_categoria('Pães')
    db.session.refresh(r)
    assert r.modo_preparo.strip()
    assert 'Mise en place' in r.modo_preparo


def test_seed_nao_sobrescreve_modo_preparo_existente(app):
    r = _receita(categoria='Pães', modo='Modo de preparo já escrito pelo dono.')
    seed_etapas_categoria('Pães')
    db.session.refresh(r)
    assert r.modo_preparo == 'Modo de preparo já escrito pelo dono.'


def test_seed_so_afeta_a_categoria_pedida(app):
    rp = _receita('Pão', categoria='Pães')
    rc = _receita('Brigadeiro', categoria='Cremes')
    seed_etapas_categoria('Pães')
    assert ReceitaEtapa.query.filter_by(receita_id=rp.id).count() > 0
    assert ReceitaEtapa.query.filter_by(receita_id=rc.id).count() == 0


def test_seed_sem_categoria_usa_default(app):
    r = _receita('Item solto', categoria='')
    n = seed_etapas_categoria('')
    assert n == 1
    etapas = ReceitaEtapa.query.filter_by(receita_id=r.id).all()
    assert len(etapas) == len(ETAPAS_PADRAO_DEFAULT)


def test_seed_ignora_arquivadas(app):
    from app.utils import agora
    r = _receita('Pão arquivado', categoria='Pães')
    r.arquivada_em = agora()
    db.session.commit()
    n = seed_etapas_categoria('Pães')
    assert n == 0
    assert ReceitaEtapa.query.filter_by(receita_id=r.id).count() == 0


# ── relationship + cascade ───────────────────────────────────────────────────

def test_receita_etapas_relationship_ordenada(app):
    r = _receita(categoria='Pães')
    db.session.add_all([
        ReceitaEtapa(receita_id=r.id, ordem=2, nome='C', duracao_min=5),
        ReceitaEtapa(receita_id=r.id, ordem=0, nome='A', duracao_min=5),
        ReceitaEtapa(receita_id=r.id, ordem=1, nome='B', duracao_min=5),
    ])
    db.session.commit()
    db.session.refresh(r)
    assert [e.nome for e in r.etapas] == ['A', 'B', 'C']


def test_etapas_cascade_delete(app):
    r = _receita(categoria='Pães')
    seed_etapas_categoria('Pães')
    rid = r.id
    db.session.delete(r)
    db.session.commit()
    assert ReceitaEtapa.query.filter_by(receita_id=rid).count() == 0


# ── mise_en_place expoe o processo ───────────────────────────────────────────

def test_mise_en_place_inclui_processo(app):
    r = _receita(categoria='Pães')
    seed_etapas_categoria('Pães')
    db.session.refresh(r)
    mep = mise_en_place(r, 20)
    assert 'processo' in mep
    assert len(mep['processo']) == len(ETAPAS_PADRAO['Pães'])
    p0 = mep['processo'][0]
    assert p0['nome'] == 'Mise en place'
    assert p0['duracao'] == '10 min'
    assert p0['ativa'] is True
    # etapa de forno tem equipamento e duracao formatada
    forno = [p for p in mep['processo'] if p['equipamento'] == 'forno'][0]
    assert forno['duracao'].endswith('min')


def test_mise_en_place_processo_vazio_sem_etapas(app):
    r = _receita(categoria='Pães')   # sem seed
    mep = mise_en_place(r, 20)
    assert mep['processo'] == []


# ── rota de seed ─────────────────────────────────────────────────────────────

def _login(app, user):
    c = app.test_client()
    c.post('/auth/login', data={'login': user.login, 'senha': '123'},
           follow_redirects=True)
    return c


def test_rota_seed_aplica_categoria(app, admin_user):
    r = _receita(categoria='Pães')
    c = _login(app, admin_user)
    resp = c.post('/receitas/amassadeira/etapas-padrao',
                  data={'categoria': 'Pães'}, follow_redirects=True)
    assert resp.status_code == 200
    assert ReceitaEtapa.query.filter_by(receita_id=r.id).count() == \
        len(ETAPAS_PADRAO['Pães'])


def test_editor_get_renderiza(app, admin_user):
    r = _receita(categoria='Pães')
    seed_etapas_categoria('Pães')
    c = _login(app, admin_user)
    resp = c.get('/receitas/%d/etapas' % r.id)
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'Mise en place' in html
    assert 'Amassamento' in html


def test_editor_post_salva_etapas(app, admin_user):
    r = _receita(categoria='Pães')
    c = _login(app, admin_user)
    resp = c.post('/receitas/%d/etapas' % r.id, data={
        'nome[]': ['Mise en place', 'Amassamento', 'Forno'],
        'duracao[]': ['10', '20', '25'],
        'recurso[]': ['padeiro', 'amassadeira', 'forno'],
    }, follow_redirects=True)
    assert resp.status_code == 200
    etapas = (ReceitaEtapa.query.filter_by(receita_id=r.id)
              .order_by(ReceitaEtapa.ordem).all())
    assert [e.nome for e in etapas] == ['Mise en place', 'Amassamento', 'Forno']
    assert etapas[1].equipamento == 'amassadeira'
    assert etapas[1].duracao_min == 20


def test_editor_amassadeira_e_maquina_nao_mao_de_obra(app, admin_user):
    """Correção do dono: amassar é MÁQUINA (padeiro livre), não mão de obra.
    No modelo: equipamento=amassadeira + ativa=True; o Gantt libera o padeiro."""
    r = _receita(categoria='Pães')
    c = _login(app, admin_user)
    c.post('/receitas/%d/etapas' % r.id, data={
        'nome[]': ['Amassamento'], 'duracao[]': ['15'],
        'recurso[]': ['amassadeira'],
    }, follow_redirects=True)
    e = ReceitaEtapa.query.filter_by(receita_id=r.id).first()
    assert e.equipamento == 'amassadeira'
    assert e.ativa is True
    # o agendador ocupa a amassadeira, NÃO o padeiro
    from app.services.gantt import _recurso
    assert _recurso(e.equipamento, e.ativa) == 'amassadeira'


def test_editor_post_substitui_e_ignora_vazias(app, admin_user):
    r = _receita(categoria='Pães')
    seed_etapas_categoria('Pães')      # estado inicial
    c = _login(app, admin_user)
    c.post('/receitas/%d/etapas' % r.id, data={
        'nome[]': ['Só essa', '', '   '],
        'duracao[]': ['15', '5', '5'],
        'recurso[]': ['padeiro', 'padeiro', 'descanso'],
    }, follow_redirects=True)
    etapas = ReceitaEtapa.query.filter_by(receita_id=r.id).all()
    assert len(etapas) == 1            # vazias ignoradas, padrão substituído
    assert etapas[0].nome == 'Só essa'


def test_editor_recurso_descanso_vira_ativa_false(app, admin_user):
    r = _receita(categoria='Pães')
    c = _login(app, admin_user)
    c.post('/receitas/%d/etapas' % r.id, data={
        'nome[]': ['Fermentação'], 'duracao[]': ['120'],
        'recurso[]': ['camara_fria'],
    }, follow_redirects=True)
    e = ReceitaEtapa.query.filter_by(receita_id=r.id).first()
    assert e.ativa is False
    assert e.equipamento == 'camara_fria'


def test_editor_acao_padrao_preenche_da_categoria(app, admin_user):
    r = _receita(categoria='Pães')
    c = _login(app, admin_user)
    c.post('/receitas/%d/etapas' % r.id, data={'acao': 'padrao'},
           follow_redirects=True)
    etapas = ReceitaEtapa.query.filter_by(receita_id=r.id).all()
    assert len(etapas) == len(ETAPAS_PADRAO['Pães'])


def test_editor_exige_admin(app):
    from app.models import Usuario
    u = Usuario(nome='func2', login='func2', papel='funcionario')
    u.set_senha('123')
    db.session.add(u)
    db.session.commit()
    r = _receita(categoria='Pães')
    c = _login(app, u)
    resp = c.get('/receitas/%d/etapas' % r.id)
    assert resp.status_code == 403


def test_rota_seed_exige_admin(app):
    from app.models import Usuario
    u = Usuario(nome='func', login='func', papel='funcionario')
    u.set_senha('123')
    db.session.add(u)
    db.session.commit()
    r = _receita(categoria='Pães')
    c = _login(app, u)
    resp = c.post('/receitas/amassadeira/etapas-padrao',
                  data={'categoria': 'Pães'}, follow_redirects=False)
    assert resp.status_code in (302, 403)
    # funcionario nao deve ter aplicado etapas
    assert ReceitaEtapa.query.filter_by(receita_id=r.id).count() == 0
