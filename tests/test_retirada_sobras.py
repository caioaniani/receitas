"""Retirada de sobras loja → indústria (esteira em 2 tempos, movida por QR).

Nasce no lançamento de sobras pelo bot (pergunta "quantos voltam?" + FOTO
obrigatória); o motorista coleta no dia seguinte via QR (baixa a loja) e a
indústria recebe via QR (credita a receita de retorno). Cobre:
- ciclo completo aguardando_coleta → em_transporte → recebida com estoque
  movendo no tempo certo (2 tempos, não atômico);
- QR: single-use, TTL, status errado, PIN inválido;
- tool do bot: foto obrigatória, criação + QR de coleta, permissões;
- hint retirada_sugerida no desperdício reaproveitável com retorno;
- regressão: retirada NÃO aparece como demanda na previsão.
"""
from datetime import timedelta

import pytest

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


@pytest.fixture(autouse=True)
def _hoje_e_segunda_fixa(congela_hoje):
    """Producao seg-sex + janela semanal tornaram o motor weekday-sensivel
    — congela numa SEGUNDA fixa (mesma fixture dos arquivos do cronograma;
    caso real 19/08/2026: test_cronograma_edit quebrou na QUARTA porque o
    indice 3 do grid caiu no sabado bloqueado)."""
    congela_hoje()



def _receita(db, nome):
    r = Receita(nome=nome, categoria='Paes', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0)
    db.session.add(r)
    db.session.commit()
    return r


def _setup(db, saldo_loja=20):
    """Croissant + retorno configurado + loja com saldo + driver com PIN."""
    trad = _receita(db, 'Croissant Tradicional')
    retorno = _receita(db, 'Croissant Tradicional — Retorno')
    trad.retorno_receita_id = retorno.id
    loja = Loja(nome='Ribeiro do Vale', ativa=True)
    db.session.add(loja)
    db.session.flush()
    el = EstoqueLoja(loja_id=loja.id, receita_id=trad.id, quantidade=saldo_loja)
    driver = Driver(nome='Joao', pin='4321', ativo=True)
    db.session.add_all([el, driver])
    db.session.commit()
    return trad, retorno, loja, el, driver


def _retirada(db, loja, trad, qtd=10, criado_por=None):
    ret = RetiradaSobra(
        loja_id=loja.id, data_retirada=hoje() + timedelta(days=1),
        criado_por_id=criado_por, foto_url='https://x/foto.jpg')
    db.session.add(ret)
    db.session.flush()
    db.session.add(RetiradaSobraItem(retirada_id=ret.id, receita_id=trad.id,
                                     quantidade=qtd))
    db.session.commit()
    return ret


def _qr(db, ret, tipo='coleta', expira_horas=48):
    qr = RetiradaQRCode(token=f'tok-{tipo}-{ret.id}', retirada_id=ret.id,
                        tipo=tipo, expira_em=agora() + timedelta(hours=expira_horas))
    db.session.add(qr)
    db.session.commit()
    return qr


# ── Ciclo completo via handshake ─────────────────────────────────────────────

def test_ciclo_completo_coleta_e_recebimento(app):
    from app.extensions import db
    trad, retorno, loja, el, driver = _setup(db)
    ret = _retirada(db, loja, trad, qtd=10)
    qr = _qr(db, ret, 'coleta')
    c = app.test_client()

    # GET mostra a página de confirmação (não move nada)
    r = c.get(f'/handshake/r/{qr.token}')
    assert r.status_code == 200
    db.session.refresh(el)
    assert el.quantidade == 20

    # COLETA: PIN do driver → em_transporte + baixa a loja
    r = c.post(f'/handshake/r/{qr.token}', data={'pin': '4321'},
               follow_redirects=False)
    assert r.status_code == 303
    db.session.refresh(el)
    db.session.refresh(ret)
    assert el.quantidade == 10                       # 20 - 10
    assert ret.status == 'em_transporte'
    assert ret.driver_id == driver.id
    # Indústria AINDA não creditada (2 tempos)
    assert EstoqueProducao.query.filter_by(receita_id=retorno.id).first() is None
    # QR de recebimento já foi gerado no sucesso da coleta
    qr2 = RetiradaQRCode.query.filter_by(retirada_id=ret.id,
                                         tipo='recebimento').first()
    assert qr2 is not None and qr2.valido

    # RECEBIMENTO: PIN de driver → recebida + credita o RETORNO
    r = c.post(f'/handshake/r/{qr2.token}', data={'pin': '4321'},
               follow_redirects=False)
    assert r.status_code == 303
    db.session.refresh(ret)
    assert ret.status == 'recebida'
    ep = EstoqueProducao.query.filter_by(receita_id=retorno.id).first()
    assert ep is not None and ep.quantidade == 10
    # Nada creditado na receita original
    assert EstoqueProducao.query.filter_by(receita_id=trad.id).first() is None


def test_qr_single_use(app):
    from app.extensions import db
    trad, _r, loja, el, _d = _setup(db)
    ret = _retirada(db, loja, trad, qtd=5)
    qr = _qr(db, ret, 'coleta')
    c = app.test_client()
    c.post(f'/handshake/r/{qr.token}', data={'pin': '4321'})
    db.session.refresh(el)
    assert el.quantidade == 15
    # Segundo POST fora da janela de double-submit não baixa de novo
    qr.usado_em = agora() - timedelta(minutes=30)
    db.session.commit()
    r = c.post(f'/handshake/r/{qr.token}', data={'pin': '4321'})
    assert r.status_code == 410
    db.session.refresh(el)
    assert el.quantidade == 15                       # não baixou 2x


def test_qr_expirado(app):
    from app.extensions import db
    trad, _r, loja, _el, _d = _setup(db)
    ret = _retirada(db, loja, trad)
    qr = _qr(db, ret, 'coleta', expira_horas=-1)     # já expirado
    c = app.test_client()
    r = c.get(f'/handshake/r/{qr.token}')
    assert r.status_code == 410


def test_pin_invalido_nao_move_estoque(app):
    from app.extensions import db
    trad, _r, loja, el, _d = _setup(db)
    ret = _retirada(db, loja, trad)
    qr = _qr(db, ret, 'coleta')
    c = app.test_client()
    r = c.post(f'/handshake/r/{qr.token}', data={'pin': '0000'})
    assert r.status_code == 401
    db.session.refresh(el)
    assert el.quantidade == 20
    db.session.refresh(ret)
    assert ret.status == 'aguardando_coleta'


def test_recebimento_exige_coleta_antes(app):
    """QR de recebimento com retirada ainda aguardando_coleta → 409."""
    from app.extensions import db
    trad, _r, loja, _el, _d = _setup(db)
    ret = _retirada(db, loja, trad)
    qr2 = _qr(db, ret, 'recebimento')
    c = app.test_client()
    r = c.post(f'/handshake/r/{qr2.token}', data={'pin': '4321'})
    assert r.status_code == 409


def test_recebimento_com_divergencia_usa_quantidade_recebida(app):
    from app.extensions import db
    from app.services.devolucao import creditar_industria_retirada
    trad, retorno, loja, _el, _d = _setup(db)
    ret = _retirada(db, loja, trad, qtd=10)
    ret.itens[0].quantidade_recebida = 7             # chegaram só 7
    db.session.commit()
    creditar_industria_retirada(ret, usuario_id=None)
    db.session.commit()
    ep = EstoqueProducao.query.filter_by(receita_id=retorno.id).first()
    assert ep.quantidade == 7


# ── Tool do bot ──────────────────────────────────────────────────────────────

def _b64_pixel():
    import base64
    # PNG 1x1 válido
    raw = base64.b64decode(
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk'
        'YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==')
    return base64.b64encode(raw).decode('ascii')


def test_tool_registrada():
    from app.services.copilot import PAPEIS_POR_TOOL, REQUER_APROVACAO, TOOLS
    nomes = {t['name'] for t in TOOLS}
    assert 'criar_retirada_sobras' in nomes
    assert 'criar_retirada_sobras' in REQUER_APROVACAO
    assert PAPEIS_POR_TOOL['criar_retirada_sobras'] == {
        'admin', 'gerente', 'funcionario'}


def test_tool_exige_foto(app, admin_user):
    from app.extensions import db
    from app.services import copilot
    trad, _r, loja, _el, _d = _setup(db)
    res = copilot.executar_criar_retirada_sobras({
        'loja_nome': 'ribeiro',
        'itens': [{'nome': 'Croissant Tradicional', 'quantidade': 10}],
    }, admin_user)
    assert res['ok'] is False
    assert 'foto' in res['erro'].lower()
    assert RetiradaSobra.query.count() == 0


def test_tool_cria_retirada_com_foto(app, admin_user, monkeypatch):
    from app.extensions import db
    from app.services import copilot, dropbox_storage
    trad, _r, loja, _el, _d = _setup(db)
    monkeypatch.setattr(dropbox_storage, 'upload_publico', lambda *a, **k: {
        'url': 'https://dl.dropbox.com/x/sobra.jpg?raw=1',
        'storage_path': '/retiradas/x.jpg', 'tamanho': 10})
    # Sem request context o url_for(_external) cai no fallback APP_BASE_URL
    # (mesmo caminho do executor de pedidos rodando em thread).
    monkeypatch.setenv('APP_BASE_URL', 'https://gestao.example.com')
    res = copilot.executar_criar_retirada_sobras({
        'loja_nome': 'ribeiro',
        'itens': [{'nome': 'Croissant Tradicional', 'quantidade': 10}],
        'imagens': [{'mimetype': 'image/png', 'base64': _b64_pixel()}],
    }, admin_user)
    assert res['ok'] is True, res
    ret = RetiradaSobra.query.get(res['retirada_id'])
    assert ret.loja_id == loja.id
    assert ret.status == 'aguardando_coleta'
    assert ret.data_retirada == hoje() + timedelta(days=1)
    assert ret.foto_url.startswith('https://')
    assert ret.itens[0].quantidade == 10
    # QR de coleta pronto + URLs no resultado (Slack posta a imagem)
    qr = RetiradaQRCode.query.filter_by(retirada_id=ret.id, tipo='coleta').first()
    assert qr is not None and qr.valido
    assert res.get('qr_png_url')
    # Estoque NÃO se move na criação (só na coleta)
    el = EstoqueLoja.query.filter_by(loja_id=loja.id, receita_id=trad.id).first()
    assert el.quantidade == 20


def test_desperdicio_reaproveitavel_sugere_retirada(app, admin_user):
    from app.extensions import db
    from app.services import copilot
    trad, retorno, loja, _el, _d = _setup(db)
    trad.reaproveitavel = True
    db.session.commit()
    res = copilot.executar_registrar_desperdicio({
        'loja_id': loja.id, 'item_nome': 'Croissant Tradicional',
        'quantidade': 12, 'motivo': 'nao_vendeu',
    }, admin_user)
    assert res['ok'] is True
    sug = res.get('retirada_sugerida')
    assert sug is not None
    assert sug['qtd_sobra'] == 12
    assert sug['destino'] == 'Croissant Tradicional — Retorno'


def test_desperdicio_sem_retorno_nao_sugere(app, admin_user):
    from app.extensions import db
    from app.services import copilot
    trad, retorno, loja, _el, _d = _setup(db)
    trad.reaproveitavel = True
    trad.retorno_receita_id = None                   # sem retorno configurado
    db.session.commit()
    res = copilot.executar_registrar_desperdicio({
        'loja_id': loja.id, 'item_nome': 'Croissant Tradicional',
        'quantidade': 5, 'motivo': 'nao_vendeu',
    }, admin_user)
    assert res['ok'] is True
    assert res.get('retirada_sugerida') is None


# ── Tela do padeiro: card de recebimento ─────────────────────────────────────

def _login(client, uid):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)
        sess['_fresh'] = True


def test_padeiro_mostra_retirada_aguardando(app, admin_user):
    """Retirada do dia aparece na fila do padeiro com destaque de RECEBIMENTO
    (aguardando coleta = sem botão de QR ainda)."""
    from app.extensions import db
    trad, _r, loja, _el, _d = _setup(db)
    ret = _retirada(db, loja, trad, qtd=10)
    ret.data_retirada = hoje()                       # retirada de HOJE
    db.session.commit()
    c = app.test_client()
    _login(c, admin_user.id)
    r = c.get('/padeiro/')
    assert r.status_code == 200
    html = r.data.decode()
    assert 'RECEBIMENTO' in html
    assert 'Retirada de sobras #%d' % ret.id in html
    assert 'Aguardando o motorista coletar' in html
    assert 'QR DE RECEBIMENTO' not in html           # ainda não coletada


def test_padeiro_retirada_em_transporte_tem_botao_qr(app, admin_user):
    """Em transporte: o gesto PRINCIPAL é receber pela tela (dono
    20/07/2026 — o padeiro não tem como escanear); o QR do motorista fica
    como alternativa."""
    from app.extensions import db
    trad, _r, loja, _el, _d = _setup(db)
    ret = _retirada(db, loja, trad, qtd=10)
    ret.data_retirada = hoje()
    ret.status = 'em_transporte'
    db.session.commit()
    c = app.test_client()
    _login(c, admin_user.id)
    html = c.get('/padeiro/').data.decode()
    assert 'RECEBI — DAR ENTRADA' in html
    assert 'QR pro motorista' in html


def test_padeiro_rota_qr_recebimento(app, admin_user):
    from app.extensions import db
    trad, _r, loja, _el, _d = _setup(db)
    ret = _retirada(db, loja, trad, qtd=10)
    ret.status = 'em_transporte'
    db.session.commit()
    c = app.test_client()
    _login(c, admin_user.id)
    r = c.post(f'/padeiro/retirada/{ret.id}/qr', data={'data': ''})
    assert r.status_code == 200
    assert 'QR de recebimento' in r.data.decode()
    qr = RetiradaQRCode.query.filter_by(retirada_id=ret.id,
                                        tipo='recebimento').first()
    assert qr is not None and qr.valido


def test_padeiro_rota_qr_recusa_antes_da_coleta(app, admin_user):
    from app.extensions import db
    trad, _r, loja, _el, _d = _setup(db)
    ret = _retirada(db, loja, trad)                  # aguardando_coleta
    c = app.test_client()
    _login(c, admin_user.id)
    r = c.post(f'/padeiro/retirada/{ret.id}/qr', data={'data': ''},
               follow_redirects=False)
    assert r.status_code in (302, 303)               # volta com aviso
    assert RetiradaQRCode.query.filter_by(retirada_id=ret.id,
                                          tipo='recebimento').count() == 0


# ── Regressão: retirada não é demanda ────────────────────────────────────────

def test_retirada_nao_entra_na_previsao(app):
    """RetiradaSobra é modelo separado de PedidoLoja — o comprometido do
    balanço não pode enxergá-la (retirada não é demanda de produção)."""
    from app.extensions import db
    from app.services.previsao_producao import (
        balanco_industria,
        invalidar_sugestao_cache,
    )
    trad, _r, loja, _el, _d = _setup(db)
    _retirada(db, loja, trad, qtd=99)
    invalidar_sugestao_cache()
    bal = balanco_industria(usar_cache=False)
    item = next((i for i in bal['itens'] if i['receita_id'] == trad.id), None)
    # Croissant aparece só pelo estoque da loja? Não — balanço é da indústria;
    # sem pedido nem estoque de indústria, a retirada de 99 não vira demanda.
    assert item is None or item['comprometido'] == 0
