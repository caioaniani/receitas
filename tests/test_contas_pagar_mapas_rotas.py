"""Telas de canal->loja, item->MP e variacoes: render + acoes."""


def _login(c):
    c.post('/auth/login', data={'login': 'admin', 'senha': '123'})


def test_tela_canais_e_vincular(app, admin_user):
    from app.extensions import db
    from app.models import Loja, SlackCanalLojaMap
    app.config['SLACK_CANAIS_NF'] = 'C_IND,C_LOJA'
    app.config['SLACK_CANAIS_NF_NOMES'] = 'C_IND=Industria;C_LOJA=Ribeiro do Vale'
    with app.app_context():
        db.session.add_all([Loja(nome='Industria', ativa=True),
                            Loja(nome='Ribeiro do Vale', ativa=True)])
        db.session.commit()
        rib_id = Loja.query.filter_by(nome='Ribeiro do Vale').first().id

    c = app.test_client()
    _login(c)
    r = c.get('/contas-pagar/canais')
    assert r.status_code == 200
    assert 'Ribeiro do Vale'.encode() in r.data

    # auto-fuzzy: o canal da industria deve ter sido detectado como eh_industria
    with app.app_context():
        ind = SlackCanalLojaMap.query.filter_by(canal_id='C_IND').first()
        assert ind is not None and ind.eh_industria is True

    r2 = c.post('/contas-pagar/canais/C_LOJA',
                data={'acao': 'vincular', 'destino': str(rib_id)},
                follow_redirects=True)
    assert r2.status_code == 200
    with app.app_context():
        m = SlackCanalLojaMap.query.filter_by(canal_id='C_LOJA').first()
        assert m.loja_id == rib_id and m.confirmado_em is not None

    # Furo de roteamento fechado: escolher a loja "Industria" pelo id roteia
    # pro estoque global (eh_industria), nunca pra uma EstoqueLoja.
    with app.app_context():
        ind_id = Loja.query.filter_by(nome='Industria').first().id
    c.post('/contas-pagar/canais/C_LOJA',
           data={'acao': 'vincular', 'destino': str(ind_id)},
           follow_redirects=True)
    with app.app_context():
        m = SlackCanalLojaMap.query.filter_by(canal_id='C_LOJA').first()
        assert m.eh_industria is True and m.loja_id is None


def test_tela_mapeamentos_e_vincular(app, admin_user):
    from app.extensions import db
    from app.models import ContaPagarItemMap, MateriaPrima
    from app.services import conta_pagar_estoque as svc
    with app.app_context():
        mp = MateriaPrima(nome='Acucar', unidade='kg', custo_por_kg=0)
        db.session.add(mp)
        db.session.flush()
        mp_id = mp.id
        db.session.add(ContaPagarItemMap(
            item_nome_norm=svc.normalizar_item_nome('ACUCAR CRISTAL'),
            item_nome_exemplo='ACUCAR CRISTAL'))
        db.session.commit()

    c = app.test_client()
    _login(c)
    r = c.get('/contas-pagar/mapeamentos')
    assert r.status_code == 200
    assert b'ACUCAR CRISTAL' in r.data

    with app.app_context():
        mid = ContaPagarItemMap.query.first().id
    r2 = c.post(f'/contas-pagar/mapeamentos/{mid}',
                data={'acao': 'vincular', 'materia_prima_id': str(mp_id),
                      'unidade_compra': 'sc', 'fator_conversao': '25',
                      'estado': 'pendente'},
                follow_redirects=True)
    assert r2.status_code == 200
    with app.app_context():
        m = db.session.get(ContaPagarItemMap, mid)
        assert m.materia_prima_id == mp_id
        assert m.fator_conversao == 25
        assert m.confirmado_em is not None


def test_vincular_unidade_metrica_impossivel_bloqueia(app, admin_user):
    """Trava de fisica (caso Toddy 2026-06-10): MP em g + '1 kg = 1,8 g'
    daria entrada de 1,8 g por caixa de 1,8 kg. O servidor recusa, sugere o
    fator certo (1800) e nada fica confirmado."""
    from app.extensions import db
    from app.models import ContaPagarItemMap, MateriaPrima
    from app.services import conta_pagar_estoque as svc
    with app.app_context():
        mp = MateriaPrima(nome='Toddy', unidade='g', custo_por_kg=0)
        db.session.add(mp)
        db.session.flush()
        mp_id = mp.id
        m = ContaPagarItemMap(
            item_nome_norm=svc.normalizar_item_nome('ACHOCOLATADO PO TODDY CX 1,8KG'),
            item_nome_exemplo='ACHOCOLATADO PO TODDY CX 1,8KG')
        db.session.add(m)
        db.session.commit()
        mid = m.id

    c = app.test_client()
    _login(c)
    r = c.post(f'/contas-pagar/mapeamentos/{mid}',
               data={'acao': 'vincular', 'materia_prima_id': str(mp_id),
                     'unidade_compra': 'kg', 'fator_conversao': '1,8',
                     'estado': 'pendente'},
               follow_redirects=True)
    assert r.status_code == 200
    assert 'use fator 1800'.encode() in r.data
    with app.app_context():
        m2 = db.session.get(ContaPagarItemMap, mid)
        assert m2.confirmado_em is None
        assert m2.materia_prima_id is None   # rollback: nada pela metade


def test_vincular_compra_em_kg_fator_fisico_passa(app, admin_user):
    """Compra quantificada em kg (NF conta quilos, MP em g): '1 kg = 1000 g'
    e o unico fator valido e confirma normal."""
    from app.extensions import db
    from app.models import ContaPagarItemMap, MateriaPrima
    from app.services import conta_pagar_estoque as svc
    with app.app_context():
        mp = MateriaPrima(nome='Farinha', unidade='g', custo_por_kg=0)
        db.session.add(mp)
        db.session.flush()
        mp_id = mp.id
        m = ContaPagarItemMap(
            item_nome_norm=svc.normalizar_item_nome('FARINHA GRANEL'),
            item_nome_exemplo='FARINHA GRANEL')
        db.session.add(m)
        db.session.commit()
        mid = m.id

    c = app.test_client()
    _login(c)
    r = c.post(f'/contas-pagar/mapeamentos/{mid}',
               data={'acao': 'vincular', 'materia_prima_id': str(mp_id),
                     'unidade_compra': 'kg', 'fator_conversao': '1000',
                     'estado': 'pendente'},
               follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        m2 = db.session.get(ContaPagarItemMap, mid)
        assert m2.confirmado_em is not None
        assert m2.fator_conversao == 1000


def test_prefill_converte_sugestao_kg_para_g(app, admin_user):
    """A IA leu 'CX 1,8KG' e sugeriu 1.8/kg; a MP sugerida e em g e a NF
    conta em cx -> o form ja abre com '1 cx = 1800 g', sem exigir que o
    humano multiplique por 1000."""
    import json
    from unittest.mock import patch

    from app.extensions import db
    from app.models import ContaPagar, ContaPagarItemMap, MateriaPrima
    from app.services import conta_pagar_estoque as svc
    nome = 'ACHOCOLATADO PO TODDY CX 1,8KG'
    with app.app_context():
        mp = MateriaPrima(nome='Toddy', unidade='g', custo_por_kg=0)
        db.session.add(mp)
        db.session.flush()
        mp_id = mp.id
        db.session.add(ContaPagarItemMap(
            item_nome_norm=svc.normalizar_item_nome(nome),
            item_nome_exemplo=nome,
            ia_unidade_sugerida='kg', ia_fator_sugerido=1.8))
        conta = ContaPagar(
            tipo_documento='nota_fiscal', fornecedor_nome='Dist',
            status='aberto',
            itens_json=json.dumps([{'nome': nome, 'quantidade': 1,
                                    'valor_unitario': 41.72,
                                    'valor_total': 41.72, 'unidade': 'cx'}]))
        db.session.add(conta)
        db.session.commit()
        conta_id = conta.id

    c = app.test_client()
    _login(c)
    with patch('app.services.conta_pagar_estoque.sugerir_para_item',
               return_value=[{'id': mp_id, 'nome': 'Toddy', 'unidade': 'g',
                              'match': 'fuzzy'}]):
        r = c.get('/contas-pagar/mapeamentos')
    assert r.status_code == 200
    assert b'value="1800"' in r.data    # fator ja convertido kg -> g
    assert b'value="cx"' in r.data      # unidade de compra real da NF
    # link "ver NF" abre o detalhe da conta de onde o exemplo veio
    assert f'/contas-pagar/{conta_id}'.encode() in r.data


def test_item_vincular_no_detalhe(app, admin_user):
    """Vincular um item de NF a uma MP direto da tela de detalhe cria o
    ContaPagarItemMap por nome (mesmo que ainda nao exista) e confirma."""
    import json

    from app.extensions import db
    from app.models import ContaPagar, ContaPagarItemMap, MateriaPrima
    from app.services import conta_pagar_estoque as svc
    with app.app_context():
        mp = MateriaPrima(nome='Farinha de Trigo', unidade='kg', custo_por_kg=0)
        db.session.add(mp)
        db.session.flush()
        mp_id = mp.id
        conta = ContaPagar(
            tipo_documento='nota_fiscal', fornecedor_nome='Moinho', status='aberto',
            itens_json=json.dumps([
                {'nome': 'FARINHA TRIGO SC 25KG', 'quantidade': 1,
                 'valor_unitario': 125, 'valor_total': 125, 'unidade': 'sc'}]))
        db.session.add(conta)
        db.session.commit()
        conta_id = conta.id

    c = app.test_client()
    _login(c)
    r = c.get(f'/contas-pagar/{conta_id}')
    assert r.status_code == 200
    assert b'FARINHA TRIGO SC 25KG' in r.data  # item aparece pra vincular

    r2 = c.post(f'/contas-pagar/{conta_id}/item/0/vincular',
                data={'acao': 'vincular', 'materia_prima_id': str(mp_id),
                      'unidade_compra': 'sc', 'fator_conversao': '25'},
                follow_redirects=True)
    assert r2.status_code == 200
    with app.app_context():
        norm = svc.normalizar_item_nome('FARINHA TRIGO SC 25KG')
        m = ContaPagarItemMap.query.filter_by(item_nome_norm=norm).first()
        assert m is not None
        assert m.materia_prima_id == mp_id
        assert m.fator_conversao == 25
        assert m.confirmado_em is not None


def test_lote_ignorar_e_confirmar_misto(app, admin_user):
    """Lote: ignora varios de uma vez; confirmar exige MP+fator por linha
    (sem default silencioso de 1.0) e aplica a trava de fisica — quem falha
    fica intacto e volta em `falhas`, quem passa confirma num commit so."""
    from app.extensions import db
    from app.models import ContaPagarItemMap, MateriaPrima
    from app.services import conta_pagar_estoque as svc

    def _mapa(nome):
        m = ContaPagarItemMap(item_nome_norm=svc.normalizar_item_nome(nome),
                              item_nome_exemplo=nome)
        db.session.add(m)
        return m

    with app.app_context():
        mp = MateriaPrima(nome='Toddy', unidade='g', custo_por_kg=0)
        db.session.add(mp)
        db.session.flush()
        mp_id = mp.id
        a = _mapa('ALCOOL LIQ 70')
        b = _mapa('ALCOOL GEL 70')
        c = _mapa('TODDY CX 1,8KG')
        d = _mapa('TODDY SEM FATOR')
        e = _mapa('TODDY KG IMPOSSIVEL')
        db.session.commit()
        ids = {x.item_nome_exemplo: x.id for x in (a, b, c, d, e)}

    cli = app.test_client()
    _login(cli)

    # ignorar em lote (caso alcool/limpeza)
    r = cli.post('/contas-pagar/mapeamentos/lote', json={
        'acao': 'ignorar',
        'itens': [{'id': ids['ALCOOL LIQ 70']}, {'id': ids['ALCOOL GEL 70']}]})
    assert r.get_json() == {'ok': 2, 'falhas': []}
    with app.app_context():
        assert db.session.get(ContaPagarItemMap, ids['ALCOOL LIQ 70']).ignorar
        assert db.session.get(ContaPagarItemMap, ids['ALCOOL GEL 70']).ignorar

    # confirmar em lote: 1 valida, 1 sem fator, 1 fisicamente impossivel
    r2 = cli.post('/contas-pagar/mapeamentos/lote', json={
        'acao': 'vincular',
        'itens': [
            {'id': ids['TODDY CX 1,8KG'], 'materia_prima_id': str(mp_id),
             'unidade_compra': 'cx', 'fator_conversao': '1800'},
            {'id': ids['TODDY SEM FATOR'], 'materia_prima_id': str(mp_id),
             'unidade_compra': 'cx', 'fator_conversao': ''},
            {'id': ids['TODDY KG IMPOSSIVEL'], 'materia_prima_id': str(mp_id),
             'unidade_compra': 'kg', 'fator_conversao': '1,8'},
        ]})
    data = r2.get_json()
    assert data['ok'] == 1
    assert len(data['falhas']) == 2
    assert any('sem fator' in f for f in data['falhas'])
    assert any('use fator 1800' in f for f in data['falhas'])
    with app.app_context():
        ok = db.session.get(ContaPagarItemMap, ids['TODDY CX 1,8KG'])
        assert ok.confirmado_em is not None and ok.fator_conversao == 1800
        for nome in ('TODDY SEM FATOR', 'TODDY KG IMPOSSIVEL'):
            falho = db.session.get(ContaPagarItemMap, ids[nome])
            assert falho.confirmado_em is None
            assert falho.materia_prima_id is None   # intacto

    # payload invalido
    assert cli.post('/contas-pagar/mapeamentos/lote',
                    json={'acao': 'apagar', 'itens': []}).status_code == 400


def test_detalhe_prefill_convertido_e_frase_viva(app, admin_user):
    """Caso Callebaut (2026-06-10): item 'CALLEBAUT ... 2.01 KG', NF em cx,
    MP sugerida em g -> o detalhe da conta ja abre com '1 cx = 2010 g' (mesma
    logica da tela de mapeamentos) e com o preview (data-qtd/data-vunit)."""
    import json
    from unittest.mock import patch

    from app.extensions import db
    from app.models import ContaPagar, MateriaPrima
    nome = 'CALLEBAUT CHOCOLATE AO LEITE 33.6% MOEDAS 2.01 KG'
    with app.app_context():
        mp = MateriaPrima(nome='Chocolate ao leite Callebaut', unidade='g',
                          custo_por_kg=0)
        db.session.add(mp)
        db.session.flush()
        mp_id = mp.id
        conta = ContaPagar(
            tipo_documento='nota_fiscal', fornecedor_nome='Gratinatto',
            status='aberto',
            itens_json=json.dumps([{'nome': nome, 'quantidade': 2,
                                    'unidade': 'cx', 'valor_unitario': 250.0,
                                    'valor_total': 500.0,
                                    'fator_embalagem': 2.01,
                                    'unidade_base_sugerida': 'kg'}]))
        db.session.add(conta)
        db.session.commit()
        conta_id = conta.id

    c = app.test_client()
    _login(c)
    with patch('app.services.conta_pagar_estoque.sugerir_para_item',
               return_value=[{'id': mp_id, 'nome': 'Chocolate ao leite Callebaut',
                              'unidade': 'g', 'match': 'fuzzy'}]):
        r = c.get(f'/contas-pagar/{conta_id}')
    assert r.status_code == 200
    assert b'value="2010"' in r.data         # 2.01 kg -> 2010 g
    assert b'value="cx"' in r.data           # unidade de compra da NF
    assert b'data-unid="g"' in r.data        # frase-viva sabe a unidade da MP
    assert b'data-qtd="2"' in r.data         # preview com os numeros da nota
    assert b'mapa-preview' in r.data


def test_tela_variacoes_e_aprovar(app, admin_user):
    from app.extensions import db
    from app.models import MateriaPrima, VariacaoPrecoMP
    with app.app_context():
        mp = MateriaPrima(nome='Manteiga', unidade='kg', custo_por_kg=10)
        db.session.add(mp)
        db.session.flush()
        db.session.add(VariacaoPrecoMP(
            materia_prima_id=mp.id, custo_anterior=8, custo_novo=10,
            variacao_pct=25, status='novo'))
        db.session.commit()

    c = app.test_client()
    _login(c)
    r = c.get('/contas-pagar/variacoes')
    assert r.status_code == 200
    assert b'Manteiga' in r.data

    with app.app_context():
        vid = VariacaoPrecoMP.query.first().id
    r2 = c.post(f'/contas-pagar/variacoes/{vid}',
                data={'acao': 'aprovar', 'f': 'todos'}, follow_redirects=True)
    assert r2.status_code == 200
    with app.app_context():
        v = db.session.get(VariacaoPrecoMP, vid)
        assert v.status == 'aprovado' and v.revisado_em is not None


def test_criar_mp_inline_vincula_na_hora(app, admin_user):
    """'+ nova MP' no card: cria a MP (com peso_unidade pro custo de ficha)
    e ja confirma o vinculo com o fator da linha. Falhou o vinculo -> a MP
    NAO fica criada (rollback). Nome duplicado -> erro claro."""
    from app.extensions import db
    from app.models import ContaPagarItemMap, MateriaPrima
    from app.services import conta_pagar_estoque as svc
    with app.app_context():
        m = ContaPagarItemMap(
            item_nome_norm=svc.normalizar_item_nome('BEBIDA DE AVEIA NUDE 1L'),
            item_nome_exemplo='BEBIDA DE AVEIA NUDE 1L')
        db.session.add(m)
        db.session.commit()
        mid = m.id

    c = app.test_client()
    _login(c)

    # sem fator -> recusa e nao deixa MP orfa pra tras
    r = c.post(f'/contas-pagar/mapeamentos/{mid}/criar-mp', json={
        'nome': 'Leite de aveia NUDE', 'unidade': 'un',
        'unidade_compra': 'un', 'fator_conversao': ''})
    assert r.status_code == 400 and 'sem fator' in r.get_json()['erro']
    with app.app_context():
        assert MateriaPrima.query.filter_by(nome='Leite de aveia NUDE').count() == 0

    r2 = c.post(f'/contas-pagar/mapeamentos/{mid}/criar-mp', json={
        'nome': 'Leite de aveia NUDE', 'unidade': 'un', 'peso_unidade': '1000',
        'unidade_compra': 'un', 'fator_conversao': '1'})
    assert r2.get_json()['ok'] is True
    with app.app_context():
        mp = MateriaPrima.query.filter_by(nome='Leite de aveia NUDE').one()
        assert mp.unidade == 'un' and mp.peso_unidade == 1000
        m2 = db.session.get(ContaPagarItemMap, mid)
        assert m2.materia_prima_id == mp.id
        assert m2.confirmado_em is not None and m2.fator_conversao == 1

    # duplicado
    with app.app_context():
        m3 = ContaPagarItemMap(item_nome_norm='outro', item_nome_exemplo='OUTRO')
        db.session.add(m3)
        db.session.commit()
        m3id = m3.id
    r3 = c.post(f'/contas-pagar/mapeamentos/{m3id}/criar-mp', json={
        'nome': 'leite de aveia nude', 'unidade': 'un',
        'unidade_compra': 'un', 'fator_conversao': '1'})
    assert r3.status_code == 400 and 'já existe' in r3.get_json()['erro']


def test_reprocessar_nfs_recentes(app, admin_user):
    """Vinculo feito DEPOIS da captura: o botao reprocessa as NFs dos
    ultimos dias (janela curta — nota velha ja foi acertada por balanco)
    e da a entrada que ficou faltando. Idempotente."""
    import json as _json
    from datetime import timedelta

    from app.extensions import db
    from app.models import ContaPagar, ContaPagarItemMap, Loja, MateriaPrima, SlackCanalLojaMap
    from app.services import conta_pagar_estoque as svc
    from app.utils import agora
    with app.app_context():
        ind = Loja(nome='Industria', ativa=True)
        db.session.add(ind)
        db.session.flush()
        db.session.add(SlackCanalLojaMap(canal_id='C_IND', loja_id=ind.id,
                                         eh_industria=True, confirmado_em=agora()))
        mp = MateriaPrima(nome='Farinha', unidade='kg', custo_por_kg=0,
                          estoque_atual=0)
        db.session.add(mp)
        db.session.flush()
        mp_id = mp.id
        db.session.add(ContaPagarItemMap(
            item_nome_norm=svc.normalizar_item_nome('FARINHA SC 25KG'),
            item_nome_exemplo='FARINHA SC 25KG', materia_prima_id=mp_id,
            fator_conversao=25, confirmado_em=agora()))
        item = [{'nome': 'FARINHA SC 25KG', 'quantidade': 1, 'valor_total': 125}]
        recente = ContaPagar(origem_canal='C_IND', fornecedor_nome='Moinho',
                             status='aberto', itens_json=_json.dumps(item))
        antiga = ContaPagar(origem_canal='C_IND', fornecedor_nome='Moinho',
                            status='aberto', itens_json=_json.dumps(item))
        db.session.add_all([recente, antiga])
        db.session.flush()
        antiga.criado_em = agora() - timedelta(days=10)   # fora da janela
        db.session.commit()

    c = app.test_client()
    _login(c)
    r = c.post('/contas-pagar/reprocessar', data={'dias': '2'},
               follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        mp = db.session.get(MateriaPrima, mp_id)
        assert mp.estoque_atual == 25      # so a NF recente entrou
        assert mp.custo_por_kg == 5.0      # 125 / 25

    # segunda rodada nao duplica (idempotente)
    c.post('/contas-pagar/reprocessar', data={'dias': '2'},
           follow_redirects=True)
    with app.app_context():
        assert db.session.get(MateriaPrima, mp_id).estoque_atual == 25
