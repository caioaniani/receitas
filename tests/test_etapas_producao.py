"""Etapas de producao (processo / fluxograma).

ReceitaEtapa guarda o passo a passo de cada receita (Batimento, Fermentacao,
Forno...) com duracao, equipamento e os parametros da ficha de producao. O
padrao generico por categoria (seed pesquisado) foi REMOVIDO em 02/10/2026 —
as etapas vem da planilha de fichas (tests/test_fichas_producao.py); o
mise_en_place expoe o processo pro card do padeiro.
"""
from app.extensions import db
from app.models import Receita, ReceitaEtapa
from app.services.producao import _fmt_dur, mise_en_place


def _receita(nome='Pão Francês', categoria='Pães', modo=''):
    r = Receita(nome=nome, categoria=categoria, rendimento_qtd=10,
                rendimento_unidade='un', peso_base=1000.0, modo_preparo=modo)
    db.session.add(r)
    db.session.commit()
    return r


ETAPAS_EXEMPLO = [
    ('Mise en place', 10, None, True, None),
    ('Amassamento', 15, 'amassadeira', True,
     {'velocidade_1_min': 14, 'velocidade_2_min': 1, 'temp_liquido_c': 5.4}),
    ('Fermentação', 120, None, False, None),
    ('Forno', 25, 'forno', True, {'forno_c': 235, 'teto': 100, 'lastro': 100}),
]


def _cadastrar(r, etapas=ETAPAS_EXEMPLO):
    """Substitui as etapas da receita (no lugar do antigo seed por categoria)."""
    ReceitaEtapa.query.filter_by(receita_id=r.id).delete()
    for i, (nome, dur, equip, ativa, params) in enumerate(etapas):
        db.session.add(ReceitaEtapa(receita_id=r.id, ordem=i, nome=nome,
                                    duracao_min=dur, equipamento=equip,
                                    ativa=ativa, parametros=params))
    db.session.commit()
    db.session.refresh(r)


# ── _fmt_dur ────────────────────────────────────────────────────────────────

def test_fmt_dur():
    assert _fmt_dur(30) == '30 min'
    assert _fmt_dur(0) == '0 min'
    assert _fmt_dur(60) == '1h'
    assert _fmt_dur(120) == '2h'
    assert _fmt_dur(150) == '2,5h'
    assert _fmt_dur(2880) == '48h'


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
    _cadastrar(r)
    rid = r.id
    db.session.delete(r)
    db.session.commit()
    assert ReceitaEtapa.query.filter_by(receita_id=rid).count() == 0


# ── mise_en_place expoe o processo ───────────────────────────────────────────

def test_mise_en_place_inclui_processo(app):
    r = _receita(categoria='Pães')
    _cadastrar(r)
    mep = mise_en_place(r, 20)
    assert 'processo' in mep
    assert len(mep['processo']) == len(ETAPAS_EXEMPLO)
    p0 = mep['processo'][0]
    assert p0['nome'] == 'Mise en place'
    assert p0['duracao'] == '10 min'
    assert p0['ativa'] is True
    assert p0['parametros'] is None and p0['parametros_label'] == ''
    # etapa de forno tem equipamento, duracao formatada e os parametros da
    # ficha de producao prontos pra TV (mesma forma do snapshot de batelada)
    forno = [p for p in mep['processo'] if p['equipamento'] == 'forno'][0]
    assert forno['duracao'].endswith('min')
    assert forno['parametros'] == {'forno_c': 235, 'teto': 100, 'lastro': 100}
    assert forno['parametros_label'] == '235 °C · teto 100 · lastro 100'
    amass = mep['processo'][1]
    assert amass['parametros_label'] == 'V1 14 min · V2 1 min · líquido 5,4 °C'


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


def test_rota_seed_generico_nao_existe_mais(app, admin_user):
    """O seed 'Aplicar padrão' por categoria foi removido (02/10/2026) — a
    URL antiga responde 404 e nada é gravado."""
    r = _receita(categoria='Pães')
    c = _login(app, admin_user)
    resp = c.post('/receitas/amassadeira/etapas-padrao',
                  data={'categoria': 'Pães'}, follow_redirects=False)
    assert resp.status_code == 404
    assert ReceitaEtapa.query.filter_by(receita_id=r.id).count() == 0
    html = c.get('/receitas/amassadeira').get_data(as_text=True)
    assert 'Aplicar padrão' not in html
    assert 'etapas-padrao' not in html


def test_editor_get_renderiza(app, admin_user):
    r = _receita(categoria='Pães')
    _cadastrar(r)
    c = _login(app, admin_user)
    resp = c.get('/receitas/%d/etapas' % r.id)
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'Mise en place' in html
    assert 'Amassamento' in html
    # parâmetros da ficha: rótulo legível + hidden que os preserva no save
    assert '⚙ V1 14 min · V2 1 min · líquido 5,4 °C' in html
    assert 'name="parametros[]"' in html
    assert 'Preencher com o padrão da categoria' not in html


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
    _cadastrar(r)      # estado inicial
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


def test_editor_recurso_congelar(app, admin_user):
    """Tipo 'congelar' (freezer, passo final): salva equipamento='congelar',
    passivo, e faz o round-trip no select (recurso_de_etapa)."""
    from app.blueprints.receitas.routes import _recurso_de_etapa
    r = _receita(categoria='Viennoiserie')
    c = _login(app, admin_user)
    c.post('/receitas/%d/etapas' % r.id, data={
        'nome[]': ['Modelagem', 'Congelar'], 'duracao[]': ['60', '60'],
        'recurso[]': ['padeiro', 'congelar'],
    }, follow_redirects=True)
    cong = ReceitaEtapa.query.filter_by(receita_id=r.id, nome='Congelar').first()
    assert cong is not None
    assert cong.equipamento == 'congelar'
    assert cong.ativa is False
    assert _recurso_de_etapa(cong) == 'congelar'


def test_gantt_congelar_nao_vira_camara_fria(app):
    """No Gantt, 'congelar' fica inline com ícone de freezer e NÃO vira marcador
    de câmara fria (mesmo longo) — é o passo final, não fermentação."""
    from datetime import date

    from app.models import PlanejamentoItem, PlanejamentoProducao
    from app.services.gantt import montar_gantt
    r = _receita(categoria='Viennoiserie')
    ReceitaEtapa.query.filter_by(receita_id=r.id).delete()
    for i, (n, d, eq, at) in enumerate([
            ('Modelagem', 30, None, True),
            ('Congelar', 300, 'congelar', False)]):   # 5h, mas é congelar
        db.session.add(ReceitaEtapa(receita_id=r.id, ordem=i, nome=n,
                                    duracao_min=d, equipamento=eq, ativa=at))
    pl = PlanejamentoProducao(data=date(2026, 10, 1), origem='cronograma')
    db.session.add(pl)
    db.session.flush()
    db.session.add(PlanejamentoItem(planejamento_id=pl.id, receita_id=r.id,
                                    multiplicador=1, qtd_alvo=10))
    db.session.commit()
    g = montar_gantt(date(2026, 10, 1))
    prod = g['produtos'][0]
    assert prod.get('destino') is None                 # NÃO cortou pra câmara fria
    icones = [t['icone'] for t in prod['tarefas']]
    assert '🧊' in icones                              # congelar inline, freezer


def test_editor_acao_padrao_nao_grava_nada(app, admin_user):
    """Aba antiga com o botão 'padrão da categoria': o POST chega sem linhas
    e NÃO pode zerar a ficha — avisa e devolve ao editor."""
    r = _receita(categoria='Pães')
    _cadastrar(r)
    c = _login(app, admin_user)
    resp = c.post('/receitas/%d/etapas' % r.id, data={'acao': 'padrao'},
                  follow_redirects=False)
    assert resp.status_code == 303
    etapas = ReceitaEtapa.query.filter_by(receita_id=r.id).all()
    assert len(etapas) == len(ETAPAS_EXEMPLO)      # intactas


def test_editor_post_preserva_parametros_da_ficha(app, admin_user):
    """Salvar a etapa no editor mantém os parâmetros (hidden JSON); linha
    nova sem hidden ou com JSON torto fica sem parâmetros."""
    import json
    r = _receita(categoria='Pães')
    c = _login(app, admin_user)
    c.post('/receitas/%d/etapas' % r.id, data={
        'nome[]': ['Batimento', 'Forno', 'Nova'],
        'duracao[]': ['15', '25', '5'],
        'recurso[]': ['amassadeira', 'forno', 'padeiro'],
        'descricao[]': ['', '', ''],
        'parametros[]': [json.dumps({'velocidade_1_min': 14}), 'não é json',
                         ''],
    }, follow_redirects=True)
    etapas = (ReceitaEtapa.query.filter_by(receita_id=r.id)
              .order_by(ReceitaEtapa.ordem).all())
    assert etapas[0].parametros == {'velocidade_1_min': 14}
    assert etapas[1].parametros is None
    assert etapas[2].parametros is None


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


# ── Aplicar ESTAS etapas a toda a categoria (26/06/2026) ─────────────────────

def test_aplicar_etapas_a_toda_categoria(app, admin_user):
    """'Aplicar a toda a categoria' copia as etapas DESTA receita pra todos os
    produtos ativos da mesma categoria, sobrescrevendo as deles. Categoria
    diferente NAO e tocada."""
    fonte = _receita('Moeda Preta', categoria='Moedas')
    alvo1 = _receita('Moeda Branca', categoria='Moedas')
    alvo2 = _receita('Moeda Doce', categoria='Moedas')
    outra = _receita('Pão Francês', categoria='Pães')  # outra categoria
    # alvo2 ja tem uma etapa antiga que DEVE ser substituida
    db.session.add(ReceitaEtapa(receita_id=alvo2.id, ordem=0, nome='Velha',
                                duracao_min=99, equipamento=None, ativa=True))
    db.session.commit()

    c = _login(app, admin_user)
    # o botao "Aplicar a toda a categoria" aparece (ha >1 produto na categoria)
    pagina = c.get('/receitas/%d/etapas' % fonte.id).data.decode()
    assert 'aplicar_categoria' in pagina
    assert 'Aplicar a toda a categoria' in pagina

    resp = c.post('/receitas/%d/etapas' % fonte.id, data={
        'acao': 'aplicar_categoria',
        'nome[]': ['40 moedas', 'Guardar moedas e materia prima'],
        'duracao[]': ['10', '5'],
        'recurso[]': ['padeiro', 'padeiro'],
    }, follow_redirects=True)
    assert resp.status_code == 200

    # os 3 produtos da categoria 'Moedas' ficaram com as 2 etapas, na ordem
    for r in (fonte, alvo1, alvo2):
        ets = (ReceitaEtapa.query.filter_by(receita_id=r.id)
               .order_by(ReceitaEtapa.ordem).all())
        assert [e.nome for e in ets] == ['40 moedas',
                                         'Guardar moedas e materia prima']
        assert [e.duracao_min for e in ets] == [10, 5]
    # a etapa velha do alvo2 foi substituida
    assert 'Velha' not in [e.nome for e in
                           ReceitaEtapa.query.filter_by(receita_id=alvo2.id)]
    # receita de OUTRA categoria nao foi tocada
    assert ReceitaEtapa.query.filter_by(receita_id=outra.id).count() == 0


def test_aplicar_categoria_exige_admin(app):
    """Funcionario nao pode aplicar etapas em massa (rota e admin_required)."""
    from app.models import Usuario
    u = Usuario(nome='func2', login='func2', papel='funcionario')
    u.set_senha('123')
    db.session.add(u)
    fonte = _receita('Sonho', categoria='Doces')
    _receita('Sonho 2', categoria='Doces')
    db.session.commit()
    c = _login(app, u)
    resp = c.post('/receitas/%d/etapas' % fonte.id, data={
        'acao': 'aplicar_categoria', 'nome[]': ['X'], 'duracao[]': ['5'],
        'recurso[]': ['padeiro']}, follow_redirects=False)
    assert resp.status_code in (302, 403)
    assert ReceitaEtapa.query.filter_by(receita_id=fonte.id).count() == 0
