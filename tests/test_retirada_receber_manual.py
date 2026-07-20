"""Destrava de baixas presas da retirada de sobras (19/07/2026).

Caso real: retirada #16 Nebraska coletada 07:00 e presa `em_transporte` 12h+
— a loja já tinha baixado, a indústria nunca foi creditada e o único caminho
era o QR de recebimento (PIN de motorista), que ninguém escaneava. Pacote:
- `receber_retirada_manual`: admin confirma o recebimento SEM QR (com
  conferência por item — primeiro caminho que escreve `quantidade_recebida`);
- `cancelar_retirada` agora aceita `em_transporte` com estorno EXATO da
  baixa da coleta (mercadoria que nunca chegou / voltou pra loja);
- alerta de WhatsApp ganha o link/gesto de destrava;
- pendência "baixas presas" no bloco "Precisa de você hoje".
"""
from datetime import timedelta

from app.extensions import db
from app.models import (
    Driver,
    EstoqueLoja,
    EstoqueProducao,
    Loja,
    MovEstoqueLoja,
    Receita,
    RetiradaQRCode,
    RetiradaSobra,
    RetiradaSobraItem,
)
from app.utils import agora, hoje


def _setup(saldo_loja=20):
    trad = Receita(nome='Croissant Tradicional', categoria='Paes',
                   rendimento_qtd=1, rendimento_unidade='un', peso_base=100.0)
    retorno = Receita(nome='Croissant Tradicional — Retorno',
                      categoria='Paes', rendimento_qtd=1,
                      rendimento_unidade='un', peso_base=100.0)
    db.session.add_all([trad, retorno])
    db.session.flush()
    trad.retorno_receita_id = retorno.id
    loja = Loja(nome='Nebraska', ativa=True)
    db.session.add(loja)
    db.session.flush()
    el = EstoqueLoja(loja_id=loja.id, receita_id=trad.id,
                     quantidade=saldo_loja)
    db.session.add(el)
    db.session.commit()
    return trad, retorno, loja, el


def _retirada_em_transporte(loja, trad, qtd=10, coletada=None,
                            baixar=True, horas_atras=13):
    """Retirada coletada há `horas_atras`h (com a baixa da loja aplicada,
    como o handshake de coleta faria) e presa em transporte."""
    from app.services.devolucao import baixar_loja_retirada
    ret = RetiradaSobra(loja_id=loja.id, data_retirada=hoje(),
                        foto_url='https://x/foto.jpg')
    db.session.add(ret)
    db.session.flush()
    db.session.add(RetiradaSobraItem(retirada_id=ret.id, receita_id=trad.id,
                                     quantidade=qtd,
                                     quantidade_coletada=coletada))
    db.session.flush()
    if baixar:
        baixar_loja_retirada(ret)
    ret.status = 'em_transporte'
    ret.coletada_em = agora() - timedelta(hours=horas_atras)
    db.session.commit()
    return ret


def _estoque_retorno(retorno):
    ep = EstoqueProducao.query.filter_by(receita_id=retorno.id).first()
    return int(ep.quantidade) if ep else 0


# ── Service: receber_retirada_manual ─────────────────────────────────────────

def test_receber_manual_credita_e_fecha(app):
    from app.services.devolucao import receber_retirada_manual
    trad, retorno, loja, el = _setup()
    ret = _retirada_em_transporte(loja, trad, qtd=10)
    assert el.quantidade == 10                     # coleta já baixou a loja
    receber_retirada_manual(ret, usuario_id=None)
    db.session.commit()
    assert ret.status == 'recebida'
    assert ret.recebida_em is not None
    assert _estoque_retorno(retorno) == 10
    assert el.quantidade == 10                     # loja não muda de novo


def test_receber_manual_com_conferencia_grava_quantidade_recebida(app):
    """A conferência da indústria vira `quantidade_recebida` (campo que
    NENHUM caminho escrevia — o form do QR só pede PIN) e o crédito usa
    ela."""
    from app.services.devolucao import receber_retirada_manual
    trad, retorno, loja, _el = _setup()
    ret = _retirada_em_transporte(loja, trad, qtd=10)
    it = ret.itens[0]
    receber_retirada_manual(ret, usuario_id=None, quantidades={it.id: 7})
    db.session.commit()
    assert it.quantidade_recebida == 7
    assert _estoque_retorno(retorno) == 7


def test_receber_manual_recusa_fora_de_transporte(app):
    from app.services.devolucao import receber_retirada_manual
    trad, _retorno, loja, _el = _setup()
    ret = RetiradaSobra(loja_id=loja.id, data_retirada=hoje(),
                        foto_url='https://x/f.jpg')
    db.session.add(ret)
    db.session.commit()
    try:
        receber_retirada_manual(ret, usuario_id=None)
        raise AssertionError('deveria recusar aguardando_coleta')
    except ValueError as exc:
        assert 'em transporte' in str(exc)


def test_receber_manual_mata_qr_e_scan_atrasado_nao_duplica(app):
    """Depois do manual, o QR de recebimento pendente morre: o scan
    atrasado do motorista leva 410 e NÃO credita a indústria de novo."""
    from app.services.devolucao import receber_retirada_manual
    trad, retorno, loja, _el = _setup()
    ret = _retirada_em_transporte(loja, trad, qtd=10)
    qr = RetiradaQRCode(token='tok-receb-tarde', retirada_id=ret.id,
                        tipo='recebimento',
                        expira_em=agora() + timedelta(hours=48))
    driver = Driver(nome='Joao', pin='4321', ativo=True)
    db.session.add_all([qr, driver])
    db.session.commit()
    receber_retirada_manual(ret, usuario_id=None)
    db.session.commit()
    assert qr.usado_em is not None
    # Scan HORAS depois (fora da janela de double-submit do handshake, que
    # suprime com 303 sem executar): QR usado leva 410.
    qr.usado_em = agora() - timedelta(hours=1)
    db.session.commit()
    c = app.test_client()
    resp = c.post('/handshake/r/tok-receb-tarde', data={'pin': '4321'})
    assert resp.status_code == 410
    assert _estoque_retorno(retorno) == 10         # não creditou 2x


# ── Service: cancelar em transporte com estorno ──────────────────────────────

def test_cancelar_em_transporte_estorna_coleta(app):
    from app.services.devolucao import cancelar_retirada
    trad, retorno, loja, el = _setup()
    ret = _retirada_em_transporte(loja, trad, qtd=10)
    assert el.quantidade == 10
    avisos = cancelar_retirada(ret, usuario_id=None)
    db.session.commit()
    assert ret.status == 'cancelada'
    assert el.quantidade == 20                     # baixa da coleta estornada
    assert _estoque_retorno(retorno) == 0          # indústria nunca creditada
    assert avisos                                  # reporta o que devolveu
    estorno = MovEstoqueLoja.query.filter_by(
        tipo='devolucao_industria_estorno').all()
    assert len(estorno) == 1 and estorno[0].quantidade == 10


def test_cancelar_em_transporte_estorna_o_coletado_nao_o_declarado(app):
    """Divergência na coleta (declarou 10, saíram 7): o estorno devolve os
    7 que saíram — os 3 nunca deixaram a loja."""
    from app.services.devolucao import cancelar_retirada
    trad, _retorno, loja, el = _setup()
    ret = _retirada_em_transporte(loja, trad, qtd=10, coletada=7)
    assert el.quantidade == 13                     # baixou o coletado
    cancelar_retirada(ret, usuario_id=None)
    db.session.commit()
    assert el.quantidade == 20


def test_estorno_generico_antes_nao_duplica_credito(app):
    """Achado de revisão (A1): admin roda o estorno GENÉRICO por token
    (ret-<id>) com a retirada ainda em transporte — a loja é re-creditada
    mas o status fica órfão. Depois: cancelar NÃO re-credita de novo (só
    fecha, com aviso) e receber manual RECUSA (creditaria a indústria com
    a loja já devolvida — estoque nas duas pontas)."""
    from app.services.devolucao import (
        cancelar_retirada,
        estornar_devolucao,
        receber_retirada_manual,
    )
    trad, retorno, loja, el = _setup()
    ret = _retirada_em_transporte(loja, trad, qtd=10)
    assert el.quantidade == 10
    estornar_devolucao(ret.token_mov, usuario_id=None)   # devolveu à loja
    assert el.quantidade == 20
    try:
        receber_retirada_manual(ret, usuario_id=None)
        raise AssertionError('deveria recusar — coleta já estornada')
    except ValueError as exc:
        assert 'estornada' in str(exc)
    db.session.rollback()
    avisos = cancelar_retirada(ret, usuario_id=None)
    db.session.commit()
    assert ret.status == 'cancelada'
    assert el.quantidade == 20                     # NÃO duplicou o crédito
    assert any('já tinha sido estornada' in a for a in avisos)
    assert _estoque_retorno(retorno) == 0


def test_cancelar_coleta_que_baixou_zero(app):
    """Coleta sem saldo na loja (só movs *_sem_estoque): cancelar fecha sem
    devolver nada — não há o que estornar."""
    from app.services.devolucao import cancelar_retirada
    trad, _retorno, loja, el = _setup(saldo_loja=0)
    ret = _retirada_em_transporte(loja, trad, qtd=10)
    assert el.quantidade == 0
    avisos = cancelar_retirada(ret, usuario_id=None)
    db.session.commit()
    assert ret.status == 'cancelada'
    assert el.quantidade == 0
    assert not [a for a in avisos if 'devolvido' in a]


def test_receber_manual_item_de_produto(app):
    """Item de PRODUTO (não receita) credita o próprio produto no
    EstoqueProducao — mesmo destino do handshake."""
    from app.models import Produto
    from app.services.devolucao import receber_retirada_manual
    _trad, _retorno, loja, _el = _setup()
    prod = Produto(nome='Cesta Retorno', categoria='Cestas', ativo=True)
    db.session.add(prod)
    db.session.flush()
    db.session.add(EstoqueLoja(loja_id=loja.id, produto_id=prod.id,
                               quantidade=5))
    ret = RetiradaSobra(loja_id=loja.id, data_retirada=hoje(),
                        foto_url='https://x/f.jpg')
    db.session.add(ret)
    db.session.flush()
    db.session.add(RetiradaSobraItem(retirada_id=ret.id, produto_id=prod.id,
                                     quantidade=3))
    db.session.flush()
    from app.services.devolucao import baixar_loja_retirada
    baixar_loja_retirada(ret)
    ret.status = 'em_transporte'
    ret.coletada_em = agora() - timedelta(hours=13)
    db.session.commit()
    receber_retirada_manual(ret, usuario_id=None)
    db.session.commit()
    ep = EstoqueProducao.query.filter_by(produto_id=prod.id).first()
    assert ep is not None and int(ep.quantidade) == 3


def test_cancelar_recebida_recusa(app):
    from app.services.devolucao import cancelar_retirada
    trad, _retorno, loja, _el = _setup()
    ret = _retirada_em_transporte(loja, trad, qtd=10)
    ret.status = 'recebida'
    db.session.commit()
    try:
        cancelar_retirada(ret, usuario_id=None)
        raise AssertionError('recebida não pode cancelar')
    except ValueError:
        pass


def test_cancelar_aguardando_coleta_segue_sem_mexer_em_estoque(app):
    """Regressão do comportamento antigo: antes da coleta, cancelar não
    toca estoque nenhum."""
    from app.services.devolucao import cancelar_retirada
    trad, _retorno, loja, el = _setup()
    ret = RetiradaSobra(loja_id=loja.id, data_retirada=hoje(),
                        foto_url='https://x/f.jpg')
    db.session.add(ret)
    db.session.flush()
    db.session.add(RetiradaSobraItem(retirada_id=ret.id, receita_id=trad.id,
                                     quantidade=10))
    db.session.commit()
    cancelar_retirada(ret, usuario_id=None)
    db.session.commit()
    assert ret.status == 'cancelada'
    assert el.quantidade == 20
    assert MovEstoqueLoja.query.count() == 0


# ── Rotas web ────────────────────────────────────────────────────────────────

def _login(client, login, senha='123'):
    client.post('/auth/login', data={'login': login, 'senha': senha})


def test_rota_receber_manual_fluxo_completo(app, admin_user):
    trad, retorno, loja, _el = _setup()
    ret = _retirada_em_transporte(loja, trad, qtd=10)
    it_id = ret.itens[0].id
    c = app.test_client()
    _login(c, 'admin')
    resp = c.post(f'/pedidos/retiradas/{ret.id}/receber-manual',
                  data={f'qtd_{it_id}': '8'}, follow_redirects=True)
    assert resp.status_code == 200
    assert ret.status == 'recebida'
    assert _estoque_retorno(retorno) == 8
    from app.models import HandshakeAudit
    audit = HandshakeAudit.query.filter_by(tipo='r_receb',
                                           etapa='manual').first()
    assert audit is not None and f'retirada:{ret.id}' in audit.detalhe


def test_rota_receber_manual_exige_admin(app):
    from app.models import Usuario
    trad, retorno, loja, _el = _setup()
    ret = _retirada_em_transporte(loja, trad, qtd=10)
    u = Usuario(nome='func', login='func', papel='funcionario')
    u.set_senha('123')
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    _login(c, 'func')
    resp = c.post(f'/pedidos/retiradas/{ret.id}/receber-manual', data={})
    assert resp.status_code in (302, 403)
    assert ret.status == 'em_transporte'
    assert _estoque_retorno(retorno) == 0


def test_rota_cancelar_em_transporte(app, admin_user):
    trad, _retorno, loja, el = _setup()
    ret = _retirada_em_transporte(loja, trad, qtd=10)
    c = app.test_client()
    _login(c, 'admin')
    resp = c.post(f'/pedidos/retiradas/{ret.id}/cancelar',
                  follow_redirects=True)
    assert resp.status_code == 200
    assert ret.status == 'cancelada'
    assert el.quantidade == 20


def test_tela_retiradas_mostra_destrava_pro_admin(app, admin_user):
    trad, _retorno, loja, _el = _setup()
    _retirada_em_transporte(loja, trad, qtd=10)
    c = app.test_client()
    _login(c, 'admin')
    body = c.get('/pedidos/retiradas').get_data(as_text=True)
    assert 'Confirmar recebimento' in body
    assert 'estorna coleta' in body


# ── Tela do padeiro: recebimento SEM QR (dono 20/07/2026) ────────────────────

def test_padeiro_recebe_pela_tela(app, admin_user):
    """"O padeiro deve concluir, porém ele só tem a tela do /padeiro":
    o POST da tela credita a indústria com a conferência dele — sem QR."""
    trad, retorno, loja, _el = _setup()
    ret = _retirada_em_transporte(loja, trad, qtd=10)
    it_id = ret.itens[0].id
    c = app.test_client()
    _login(c, 'admin')
    resp = c.post(f'/padeiro/retirada/{ret.id}/receber',
                  data={f'qtd_{it_id}': '9'}, follow_redirects=True)
    assert resp.status_code == 200
    assert ret.status == 'recebida'
    assert _estoque_retorno(retorno) == 9
    assert ret.itens[0].quantidade_recebida == 9
    from app.models import HandshakeAudit
    audit = HandshakeAudit.query.filter_by(tipo='r_receb',
                                           etapa='manual').first()
    assert audit is not None and 'tela do padeiro' in audit.detalhe


def test_padeiro_receber_recusa_antes_da_coleta(app, admin_user):
    trad, retorno, loja, _el = _setup()
    ret = RetiradaSobra(loja_id=loja.id, data_retirada=hoje(),
                        foto_url='https://x/f.jpg')
    db.session.add(ret)
    db.session.flush()
    db.session.add(RetiradaSobraItem(retirada_id=ret.id, receita_id=trad.id,
                                     quantidade=10))
    db.session.commit()
    c = app.test_client()
    _login(c, 'admin')
    resp = c.post(f'/padeiro/retirada/{ret.id}/receber', data={},
                  follow_redirects=True)
    assert resp.status_code == 200
    assert ret.status == 'aguardando_coleta'       # nada mudou
    assert _estoque_retorno(retorno) == 0


# ── Alerta + pendência da home ───────────────────────────────────────────────

def test_alerta_mensagem_tem_link_de_destrava(app):
    from app.services.alertas_operacionais import (
        _montar_mensagem,
        verificar_baixas_presas,
    )
    trad, _retorno, loja, _el = _setup()
    _retirada_em_transporte(loja, trad, qtd=10, horas_atras=13)
    d = verificar_baixas_presas()
    assert d['retiradas']
    msg = _montar_mensagem(d, base_url='https://x')
    assert 'https://x/pedidos/retiradas' in msg
    assert 'Confirmar recebimento' in msg
    assert 'QR de recebimento' in msg              # instrução antiga fica


def test_pendencia_home_lista_baixas_presas(app):
    from app.services.briefing_dono import pendencias
    trad, _retorno, loja, _el = _setup()
    _retirada_em_transporte(loja, trad, qtd=10, horas_atras=13)
    itens = pendencias(incluir_owner=False)
    presas = [i for i in itens if i['chave'] == 'retiradas_presas']
    assert len(presas) == 1
    assert presas[0]['qtd'] == 1
    assert presas[0]['url'] == '/pedidos/retiradas'


def test_pendencia_home_sem_presas_nao_lista(app):
    from app.services.briefing_dono import pendencias
    itens = pendencias(incluir_owner=False)
    assert not [i for i in itens if i['chave'] in ('retiradas_presas',
                                                   'separados_presos')]
