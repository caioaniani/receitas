"""Lista web de retiradas de sobras + QR de coleta regenerável + cancelar
(10/07/2026). Incidente real: o QR de coleta (TTL 48h) das retiradas #4/#5
da Nebraska expirou e elas ficaram presas em `aguardando_coleta` pra sempre
— sem tela pra regenerar o QR e sem caminho de cancelar. Estes testes
travam o desbloqueio:
- /pedidos/retiradas lista as abertas;
- o botão QR de coleta REGENERA quando o antigo expirou (e o novo valida
  no handshake);
- cancelar só antes da coleta, sem mexer em estoque;
- em transporte não cancela (a loja já baixou).
"""
from datetime import timedelta

from app.extensions import db
from app.models import (
    EstoqueLoja,
    Loja,
    Receita,
    RetiradaQRCode,
    RetiradaSobra,
    RetiradaSobraItem,
)
from app.utils import agora, hoje


def _setup(saldo_loja=20):
    trad = Receita(nome='Croissant Tradicional', categoria='Paes',
                   rendimento_qtd=1, rendimento_unidade='un',
                   peso_base=100.0)
    retorno = Receita(nome='Croissant Tradicional — Retorno',
                      categoria='Paes', rendimento_qtd=1,
                      rendimento_unidade='un', peso_base=100.0)
    db.session.add_all([trad, retorno])
    db.session.flush()
    trad.retorno_receita_id = retorno.id
    loja = Loja(nome='Nebraska', ativa=True)
    db.session.add(loja)
    db.session.flush()
    el = EstoqueLoja(loja_id=loja.id, receita_id=retorno.id,
                     quantidade=saldo_loja)
    db.session.add(el)
    db.session.commit()
    return trad, retorno, loja, el


def _retirada(loja, receita, qtd=6, dias_atras=4):
    """Retirada antiga (como a #4 do incidente: criada dias atrás)."""
    ret = RetiradaSobra(loja_id=loja.id,
                        data_retirada=hoje() - timedelta(days=dias_atras),
                        foto_url='https://x/foto.jpg')
    db.session.add(ret)
    db.session.flush()
    db.session.add(RetiradaSobraItem(retirada_id=ret.id,
                                     receita_id=receita.id, quantidade=qtd))
    db.session.commit()
    return ret


def _qr_expirado(ret):
    qr = RetiradaQRCode(token=f'tok-velho-{ret.id}', retirada_id=ret.id,
                        tipo='coleta',
                        expira_em=agora() - timedelta(hours=1))
    db.session.add(qr)
    db.session.commit()
    return qr


def _login(c):
    return c.post('/auth/login', data={'login': 'admin', 'senha': '123'})


def test_lista_mostra_abertas_e_finalizadas(app, admin_user):
    with app.app_context():
        trad, retorno, loja, _ = _setup()
        r1 = _retirada(loja, retorno)
        r2 = _retirada(loja, retorno, qtd=16, dias_atras=1)
        r2.status = 'cancelada'
        db.session.commit()
        rid1 = r1.id
    c = app.test_client()
    _login(c)
    corpo = c.get('/pedidos/retiradas').get_data(as_text=True)
    assert 'Nebraska' in corpo
    assert 'aguardando coleta' in corpo
    assert 'cancelada' in corpo
    assert f'/pedidos/retiradas/{rid1}/qr-coleta' in corpo


def test_qr_coleta_regenera_expirado_e_novo_valida(app, admin_user):
    """O caso do incidente: QR de coleta expirado → o botão gera um NOVO
    QR válido, e o handshake aceita o novo token."""
    with app.app_context():
        trad, retorno, loja, _ = _setup()
        ret = _retirada(loja, retorno)
        _qr_expirado(ret)
        rid = ret.id
    c = app.test_client()
    _login(c)
    r = c.post(f'/pedidos/retiradas/{rid}/qr-coleta')
    assert r.status_code == 200
    assert b'data:image/png' in r.data              # QR PNG na tela
    with app.app_context():
        novos = (RetiradaQRCode.query
                 .filter_by(retirada_id=rid, tipo='coleta', usado_em=None)
                 .filter(RetiradaQRCode.expira_em > agora()).all())
        assert len(novos) == 1                      # emitiu UM novo válido
        token = novos[0].token
    # o handshake abre a página de coleta com o token novo
    r2 = c.get(f'/handshake/r/{token}')
    assert r2.status_code == 200


def test_qr_coleta_reusa_ativo(app, admin_user):
    """QR ainda válido não é duplicado — reusa (single-use preservado)."""
    with app.app_context():
        trad, retorno, loja, _ = _setup()
        ret = _retirada(loja, retorno, dias_atras=0)
        qr = RetiradaQRCode(token='tok-vivo', retirada_id=ret.id,
                            tipo='coleta',
                            expira_em=agora() + timedelta(hours=24))
        db.session.add(qr)
        db.session.commit()
        rid = ret.id
    c = app.test_client()
    _login(c)
    r = c.post(f'/pedidos/retiradas/{rid}/qr-coleta')
    assert r.status_code == 200
    with app.app_context():
        assert RetiradaQRCode.query.filter_by(
            retirada_id=rid, tipo='coleta').count() == 1    # não duplicou


def test_qr_coleta_recusa_status_errado(app, admin_user):
    with app.app_context():
        trad, retorno, loja, _ = _setup()
        ret = _retirada(loja, retorno)
        ret.status = 'em_transporte'
        db.session.commit()
        rid = ret.id
    c = app.test_client()
    _login(c)
    r = c.post(f'/pedidos/retiradas/{rid}/qr-coleta', follow_redirects=True)
    assert 'não está aguardando coleta' in r.get_data(as_text=True)


def test_cancelar_antes_da_coleta_nao_mexe_estoque(app, admin_user):
    with app.app_context():
        trad, retorno, loja, el = _setup(saldo_loja=20)
        ret = _retirada(loja, retorno)
        rid, elid = ret.id, el.id
    c = app.test_client()
    _login(c)
    r = c.post(f'/pedidos/retiradas/{rid}/cancelar', follow_redirects=True)
    assert 'cancelada' in r.get_data(as_text=True)
    with app.app_context():
        ret = db.session.get(RetiradaSobra, rid)
        assert ret.status == 'cancelada'
        assert ret.cancelada_em is not None
        # estoque da loja intocado (nada tinha sido baixado)
        assert db.session.get(EstoqueLoja, elid).quantidade == 20


def test_cancelar_em_transporte_recusa(app, admin_user):
    with app.app_context():
        trad, retorno, loja, _ = _setup()
        ret = _retirada(loja, retorno)
        ret.status = 'em_transporte'
        db.session.commit()
        rid = ret.id
    c = app.test_client()
    _login(c)
    r = c.post(f'/pedidos/retiradas/{rid}/cancelar', follow_redirects=True)
    assert 'Não cancelei' in r.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(RetiradaSobra, rid).status == 'em_transporte'


def test_cancelar_exige_admin(app):
    """Gerente vê a lista e o QR, mas cancelar é admin."""
    from app.models import Usuario
    with app.app_context():
        trad, retorno, loja, _ = _setup()
        ret = _retirada(loja, retorno)
        rid = ret.id
        u = Usuario(nome='Gerente', login='ger', papel='gerente')
        u.set_senha('12345678')
        db.session.add(u)
        db.session.commit()
    c = app.test_client()
    c.post('/auth/login', data={'login': 'ger', 'senha': '12345678'})
    assert c.get('/pedidos/retiradas').status_code == 200      # lista OK
    assert c.post(f'/pedidos/retiradas/{rid}/cancelar').status_code == 403
