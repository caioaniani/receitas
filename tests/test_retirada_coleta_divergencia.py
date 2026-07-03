"""Conferência do motorista na COLETA da retirada (03/07/2026).

A loja marcou 15 pra sair mas o motorista levou 12 — antes não tinha onde
editar. Agora a tela do QR de coleta tem a quantidade por item (default =
declarado): a baixa da loja usa o COLETADO, o recebimento na indústria parte
dele, e a divergência avisa o canal de pedidos no Slack. Os que ficam
continuam no estoque de retorno da loja (as vendas de Nutella baixam dali —
por isso NÃO há entrada manual a fazer).
"""
from datetime import timedelta
from unittest.mock import patch

from app.extensions import db
from app.models import (
    Driver,
    EstoqueLoja,
    EstoqueProducao,
    Loja,
    Receita,
    RetiradaQRCode,
    RetiradaSobra,
    RetiradaSobraItem,
)
from app.utils import agora, hoje


def _receita(nome):
    r = Receita(nome=nome, categoria='Paes', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0)
    db.session.add(r)
    db.session.commit()
    return r


def _setup(saldo_retorno=15):
    """Retirada carrega a receita de RETORNO (fluxo pós-conversão)."""
    retorno = _receita('Croissant Div - Retorno')
    loja = Loja(nome='Loja Div', ativa=True)
    db.session.add(loja)
    db.session.flush()
    el = EstoqueLoja(loja_id=loja.id, receita_id=retorno.id,
                     quantidade=saldo_retorno)
    driver = Driver(nome='Zeca', pin='9876', ativo=True)
    db.session.add_all([el, driver])
    db.session.commit()
    ret = RetiradaSobra(loja_id=loja.id,
                        data_retirada=hoje() + timedelta(days=1),
                        foto_url='https://x/foto.jpg')
    db.session.add(ret)
    db.session.flush()
    item = RetiradaSobraItem(retirada_id=ret.id, receita_id=retorno.id,
                             quantidade=15)
    db.session.add(item)
    qr = RetiradaQRCode(token=f'tok-div-{ret.id}', retirada_id=ret.id,
                        tipo='coleta',
                        expira_em=agora() + timedelta(hours=48))
    db.session.add(qr)
    db.session.commit()
    return retorno, loja, el, ret, item, qr


def test_coleta_com_divergencia_baixa_o_coletado_e_avisa_slack(app):
    with app.app_context():
        app.config['SLACK_CANAL_PEDIDOS'] = 'C-PEDIDOS'
        retorno, loja, el, ret, item, qr = _setup()
        c = app.test_client()
        with patch('app.services.slack.post_message') as post:
            r = c.post(f'/handshake/r/{qr.token}',
                       data={'pin': '9876', f'qtd_{item.id}': '12'})
        assert r.status_code == 303
        db.session.refresh(el)
        assert el.quantidade == 3            # baixou 12, os 3 FICAM no retorno
        db.session.refresh(item)
        assert item.quantidade_coletada == 12
        db.session.refresh(ret)
        assert ret.status == 'em_transporte'
        post.assert_called_once()
        args, kwargs = post.call_args
        assert args[0] == 'C-PEDIDOS'
        texto = kwargs.get('text') or (args[1] if len(args) > 1 else '')
        assert 'marcou' in texto and '15' in texto and '12' in texto
        assert 'estoque de retorno da loja' in texto


def test_recebimento_credita_o_coletado(app):
    with app.app_context():
        app.config['SLACK_CANAL_PEDIDOS'] = 'C-PEDIDOS'
        retorno, loja, el, ret, item, qr = _setup()
        c = app.test_client()
        with patch('app.services.slack.post_message'):
            c.post(f'/handshake/r/{qr.token}',
                   data={'pin': '9876', f'qtd_{item.id}': '12'})
        qr2 = RetiradaQRCode.query.filter_by(retirada_id=ret.id,
                                             tipo='recebimento').first()
        assert qr2 is not None               # gerado na coleta
        r = c.post(f'/handshake/r/{qr2.token}', data={'pin': '9876'})
        assert r.status_code == 303
        ep = EstoqueProducao.query.filter_by(receita_id=retorno.id).first()
        assert ep is not None and ep.quantidade == 12   # base = coletado


def test_coleta_igual_ao_declarado_nao_marca_nem_avisa(app):
    with app.app_context():
        app.config['SLACK_CANAL_PEDIDOS'] = 'C-PEDIDOS'
        retorno, loja, el, ret, item, qr = _setup()
        c = app.test_client()
        with patch('app.services.slack.post_message') as post:
            r = c.post(f'/handshake/r/{qr.token}',
                       data={'pin': '9876', f'qtd_{item.id}': '15'})
        assert r.status_code == 303
        db.session.refresh(el)
        assert el.quantidade == 0
        db.session.refresh(item)
        assert item.quantidade_coletada is None          # sem divergência
        post.assert_not_called()


def test_coleta_sem_campo_usa_declarado(app):
    """Compat: POST antigo (só PIN, sem os campos) coleta o declarado."""
    with app.app_context():
        retorno, loja, el, ret, item, qr = _setup()
        c = app.test_client()
        r = c.post(f'/handshake/r/{qr.token}', data={'pin': '9876'})
        assert r.status_code == 303
        db.session.refresh(el)
        assert el.quantidade == 0
        db.session.refresh(item)
        assert item.quantidade_coletada is None
