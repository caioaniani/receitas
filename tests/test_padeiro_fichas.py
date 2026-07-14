"""Ficha de preparo do padeiro (/padeiro/fichas, 14/07/2026, pedido do dono):
o padeiro preenche as etapas de preparo de cada pão (nome, duração, tipo de
trabalho, passo a passo/descrição) e isso alimenta o fluxograma/Gantt e o
mise en place. Mesma fonte de dados do editor do admin
(/receitas/<id>/etapas) — serviço em app/services/etapas_receita.py.
"""
from app.extensions import db
from app.models import Receita, ReceitaEtapa, Usuario


def _login(c, login, senha):
    return c.post('/auth/login', data={'login': login, 'senha': senha})


def _padeiro(login='padfch'):
    u = Usuario(nome='Padeiro Fichas', login=login, papel='padeiro')
    u.set_senha('12345678')
    db.session.add(u)
    db.session.commit()
    return u


def _receita(nome='Baguete Ficha', categoria='Paes'):
    r = Receita(nome=nome, categoria=categoria)
    db.session.add(r)
    db.session.commit()
    return r


def test_lista_fichas_mostra_receita_e_estado(app):
    with app.app_context():
        _padeiro('padl1')
        r = _receita('Ciabatta Ficha')
        db.session.add(ReceitaEtapa(receita_id=r.id, ordem=0,
                                    nome='Amassar', duracao_min=15))
        db.session.commit()
    c = app.test_client()
    _login(c, 'padl1', '12345678')
    body = c.get('/padeiro/fichas').get_data(as_text=True)
    assert 'Ciabatta Ficha' in body
    # 1 etapa sem passo a passo = badge parcial
    assert '1 etapa(s)' in body and '0 com passo a passo' in body


def test_padeiro_salva_ficha_com_descricao(app):
    with app.app_context():
        _padeiro('padl2')
        r = _receita('Focaccia Ficha')
        rid = r.id
    c = app.test_client()
    _login(c, 'padl2', '12345678')
    resp = c.post(f'/padeiro/fichas/{rid}', data={
        'nome[]': ['Mise en place', 'Descanso', 'Forno'],
        'duracao[]': ['10', '120', '25'],
        'recurso[]': ['padeiro', 'descanso', 'forno'],
        'descricao[]': ['Pese farinha, água e sal.',
                        '',
                        'Asse a 230 °C com vapor.'],
    })
    assert resp.status_code == 302
    with app.app_context():
        etapas = (ReceitaEtapa.query.filter_by(receita_id=rid)
                  .order_by(ReceitaEtapa.ordem).all())
        assert [e.nome for e in etapas] == ['Mise en place', 'Descanso', 'Forno']
        assert etapas[0].descricao == 'Pese farinha, água e sal.'
        assert etapas[1].descricao is None          # vazio vira NULL
        assert etapas[1].ativa is False             # descanso = passiva
        assert etapas[2].equipamento == 'forno'
        assert etapas[2].descricao == 'Asse a 230 °C com vapor.'


def test_padeiro_preenche_do_padrao_da_categoria(app):
    with app.app_context():
        _padeiro('padl3')
        r = _receita('Sourdough Ficha', categoria='Paes')
        rid = r.id
    c = app.test_client()
    _login(c, 'padl3', '12345678')
    resp = c.post(f'/padeiro/fichas/{rid}', data={'acao': 'padrao'})
    assert resp.status_code == 302
    with app.app_context():
        assert ReceitaEtapa.query.filter_by(receita_id=rid).count() > 0


def test_funcionario_de_loja_nao_acessa_fichas(app, loja):
    with app.app_context():
        u = Usuario(nome='Func Loja', login='funcfch', papel='funcionario',
                    loja_id=loja.id)
        u.set_senha('12345678')
        db.session.add(u)
        db.session.commit()
    c = app.test_client()
    _login(c, 'funcfch', '12345678')
    assert c.get('/padeiro/fichas').status_code == 403


def test_editor_admin_tambem_grava_descricao(app, admin_user):
    """Regressão do serviço compartilhado: o editor do admin
    (/receitas/<id>/etapas) grava a MESMA descrição que a ficha do padeiro."""
    with app.app_context():
        r = _receita('Brioche Ficha Admin')
        rid = r.id
    c = app.test_client()
    _login(c, 'admin', '123')
    resp = c.post(f'/receitas/{rid}/etapas', data={
        'nome[]': ['Amassar'],
        'duracao[]': ['20'],
        'recurso[]': ['amassadeira'],
        'descricao[]': ['Velocidade 2 por 12 min, depois 4 por 8 min.'],
    })
    assert resp.status_code == 302
    with app.app_context():
        e = ReceitaEtapa.query.filter_by(receita_id=rid).one()
        assert e.descricao == 'Velocidade 2 por 12 min, depois 4 por 8 min.'
        assert e.equipamento == 'amassadeira'


def test_mise_en_place_expoe_descricao_no_processo(app):
    """O drawer de mise en place do padeiro recebe o passo a passo da ficha."""
    from app.services.producao import mise_en_place
    with app.app_context():
        r = _receita('Pao Processo Ficha')
        db.session.add(ReceitaEtapa(receita_id=r.id, ordem=0, nome='Modelar',
                                    duracao_min=30,
                                    descricao='Bolas de 90 g bem apertadas.'))
        db.session.commit()
        mep = mise_en_place(r, 10)
        assert mep['processo'][0]['descricao'] == 'Bolas de 90 g bem apertadas.'
