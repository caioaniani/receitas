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
