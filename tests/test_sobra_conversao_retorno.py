"""Conversão de sobra → receita de retorno NO ESTOQUE DA LOJA (03/07/2026).

Decisão do dono: ao registrar a sobra do croissant, o estoque da loja converte
na hora — baixa o fresco e credita "Croissant Tradicional - Retorno" na mesma
loja. Motivos: (1) o fresco volta a refletir só o vendável (sugestão de pedido
parava de ver sobra velha como estoque bom); (2) os produtos Nutella já são
compostos do retorno — a venda baixa dali; (3) a retirada coleta o retorno.
Reaproveitável SEM retorno configurado mantém o comportamento antigo
(registro sem movimento). Exclusão do desperdício desfaz o par.
"""
import base64
from unittest.mock import patch

from app.extensions import db
from app.models import Desperdicio, EstoqueLoja, Loja, MovEstoqueLoja, Receita


def _setup(qtd_fresco=20, com_retorno=True):
    loja = Loja(nome='Loja Conv', ativa=True)
    retorno = Receita(nome='Croissant Conv - Retorno', rendimento_qtd=1,
                      rendimento_unidade='un', peso_base=100.0)
    db.session.add_all([loja, retorno])
    db.session.commit()
    croissant = Receita(nome='Croissant Conv', rendimento_qtd=1,
                        rendimento_unidade='un', peso_base=100.0,
                        reaproveitavel=True,
                        retorno_receita_id=retorno.id if com_retorno else None)
    db.session.add(croissant)
    db.session.commit()
    el = EstoqueLoja(loja_id=loja.id, receita_id=croissant.id,
                     quantidade=qtd_fresco)
    db.session.add(el)
    db.session.commit()
    return loja, croissant, retorno, el


def _el_retorno(loja, retorno):
    return EstoqueLoja.query.filter_by(loja_id=loja.id,
                                       receita_id=retorno.id).first()


def _login(client, uid):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)
        sess['_fresh'] = True


def _registrar_web(app, admin_user, loja, receita, qtd, motivo='validade'):
    c = app.test_client()
    _login(c, admin_user.id)
    resp = c.post('/pedidos/desperdicio', data={
        'loja_id': str(loja.id), 'item_id': f'r_{receita.id}',
        'quantidade': str(qtd), 'motivo': motivo,
    })
    assert resp.status_code in (302, 303)
    return c


def test_web_converte_sobra_para_retorno(app, admin_user):
    with app.app_context():
        loja, croissant, retorno, el = _setup(qtd_fresco=20)
        _registrar_web(app, admin_user, loja, croissant, 15)
        db.session.refresh(el)
        assert el.quantidade == 5                     # fresco baixou
        el_ret = _el_retorno(loja, retorno)
        assert el_ret is not None and el_ret.quantidade == 15
        desp = Desperdicio.query.filter_by(receita_id=croissant.id).first()
        assert 'convertido em Croissant Conv - Retorno' in desp.observacao
        movs = MovEstoqueLoja.query.filter_by(desperdicio_id=desp.id).all()
        assert {m.tipo for m in movs} == {'sobra_retorno',
                                          'sobra_retorno_entrada'}


def test_web_saldo_subcontado_credita_inteiro(app, admin_user):
    """Sobraram 15 fisicamente mas o sistema só tinha 10 no fresco: baixa 10
    (falta vira mov visível) e o retorno recebe os 15 — a sobra física
    existe, mesmo padrão da devolução."""
    with app.app_context():
        loja, croissant, retorno, el = _setup(qtd_fresco=10)
        _registrar_web(app, admin_user, loja, croissant, 15)
        db.session.refresh(el)
        assert el.quantidade == 0
        assert _el_retorno(loja, retorno).quantidade == 15
        desp = Desperdicio.query.filter_by(receita_id=croissant.id).first()
        tipos = {m.tipo for m in MovEstoqueLoja.query.filter_by(
            desperdicio_id=desp.id).all()}
        assert 'sobra_retorno_sem_estoque' in tipos


def test_reaproveitavel_sem_retorno_mantem_comportamento(app, admin_user):
    with app.app_context():
        loja, croissant, retorno, el = _setup(qtd_fresco=20,
                                              com_retorno=False)
        _registrar_web(app, admin_user, loja, croissant, 5)
        db.session.refresh(el)
        assert el.quantidade == 20                    # nada baixou
        desp = Desperdicio.query.filter_by(receita_id=croissant.id).first()
        assert 'nao baixou estoque' in desp.observacao
        assert MovEstoqueLoja.query.filter_by(
            desperdicio_id=desp.id).count() == 0


def test_exclusao_desfaz_conversao(app, admin_user):
    with app.app_context():
        loja, croissant, retorno, el = _setup(qtd_fresco=20)
        c = _registrar_web(app, admin_user, loja, croissant, 15)
        desp = Desperdicio.query.filter_by(receita_id=croissant.id).first()
        c.post(f'/pedidos/desperdicio/{desp.id}/excluir',
               follow_redirects=True)
        db.session.refresh(el)
        assert el.quantidade == 20                    # fresco de volta
        assert _el_retorno(loja, retorno).quantidade == 0


def test_exclusao_com_retorno_ja_consumido_avisa(app, admin_user):
    """Parte do retorno já saiu (coleta/venda Nutella): o estorno reverte só
    o saldo que resta e avisa — nunca deixa negativo."""
    with app.app_context():
        loja, croissant, retorno, el = _setup(qtd_fresco=20)
        c = _registrar_web(app, admin_user, loja, croissant, 15)
        el_ret = _el_retorno(loja, retorno)
        el_ret.quantidade = 5                        # 10 já consumidos
        db.session.commit()
        desp = Desperdicio.query.filter_by(receita_id=croissant.id).first()
        resp = c.post(f'/pedidos/desperdicio/{desp.id}/excluir',
                      follow_redirects=True)
        assert 'retorno ja consumido' in resp.get_data(as_text=True)
        db.session.refresh(el)
        assert el.quantidade == 20                    # fresco volta inteiro
        db.session.refresh(el_ret)
        assert el_ret.quantidade == 0                 # reverteu só os 5


def test_copilot_lote_converte_e_sugere_retirada(app, admin_user):
    from app.services.copilot import executar_registrar_desperdicio_lote
    with app.app_context():
        loja, croissant, retorno, el = _setup(qtd_fresco=20)
        res = executar_registrar_desperdicio_lote({
            'loja_id': loja.id, 'motivo': 'validade',
            'itens': [{'nome': 'Croissant Conv', 'quantidade': 15}],
        }, admin_user)
        assert res['ok'] is True
        ap = res['aplicados'][0]
        assert ap['convertido_retorno']['destino'] == 'Croissant Conv - Retorno'
        assert ap['retirada_sugerida']['destino'] == 'Croissant Conv - Retorno'
        db.session.refresh(el)
        assert el.quantidade == 5
        assert _el_retorno(loja, retorno).quantidade == 15


def test_copilot_single_converte(app, admin_user):
    from app.services.copilot import executar_registrar_desperdicio
    with app.app_context():
        loja, croissant, retorno, el = _setup(qtd_fresco=20)
        res = executar_registrar_desperdicio({
            'loja_id': loja.id, 'item_nome': 'Croissant Conv',
            'quantidade': 8, 'motivo': 'nao_vendeu',
        }, admin_user)
        assert res['ok'] is True
        assert res['convertido_retorno']['creditado'] == 8
        db.session.refresh(el)
        assert el.quantidade == 12
        assert _el_retorno(loja, retorno).quantidade == 8


def test_criar_retirada_carrega_receita_de_retorno(app, admin_user):
    """A retirada coleta o RETORNO (o estoque da loja já foi convertido) —
    a coleta baixa o retorno e a indústria credita o retorno (a própria)."""
    from app.models import RetiradaSobraItem
    from app.services.copilot import executar_criar_retirada_sobras
    with app.app_context():
        loja, croissant, retorno, el = _setup(qtd_fresco=20)
        img = {'base64': base64.b64encode(b'foto').decode(),
               'mimetype': 'image/jpeg'}
        with patch('app.services.dropbox_storage.upload_publico',
                   return_value={'url': 'https://dbx/x.jpg',
                                 'storage_path': '/x.jpg'}):
            res = executar_criar_retirada_sobras({
                'loja_id': loja.id, 'imagens': [img],
                'itens': [{'nome': 'Croissant Conv', 'quantidade': 10}],
            }, admin_user)
        assert res['ok'] is True
        item = RetiradaSobraItem.query.filter_by(
            retirada_id=res['retirada_id']).first()
        assert item.receita_id == retorno.id          # coleta o RETORNO
