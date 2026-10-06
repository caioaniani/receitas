"""Faltas de saída da indústria (05/10/2026, caso brioche 76 x 22; opção A
do dono).

A saída do pedido desconta só o saldo do sistema; o resto vira
`saida_pedido_sem_estoque` (neutro) e agora também uma `FaltaSaidaIndustria`.
A falta aparece na hora (QR de saída, botão Enviar, TV do padeiro) e, ao
lançar a produção, só a CONFIRMAÇÃO do padeiro grava o débito
(`saida_pedido_quitada`, referência do pedido). Contagem encerra; estorno
devolve a quitação; saldo nunca fica negativo.
"""
from datetime import date, timedelta

import pytest

from app.extensions import db
from app.models import (
    EstoqueProducao,
    FaltaSaidaIndustria,
    MovEstoqueProducao,
    PedidoItem,
    PedidoLoja,
)
from app.utils import agora, hoje


@pytest.fixture
def cliente(app):
    return app.test_client()


def _login(cliente, user):
    with cliente.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def _receita(nome='Brioche Teste'):
    from app.models import Receita
    r = Receita(nome=nome, categoria='Doces', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0, peso_unitario=100.0)
    db.session.add(r)
    db.session.commit()
    return r


def _loja(nome='Loja Faltas'):
    from app.models import Loja
    loja = Loja(nome=nome, ativa=True, endereco='Rua F, 1')
    db.session.add(loja)
    db.session.commit()
    return loja


def _pedido(loja, receita, qtd, status='separado'):
    p = PedidoLoja(loja_id=loja.id, status=status,
                   data_entrega=date.today() + timedelta(days=1))
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p.id, receita_id=receita.id,
                              quantidade=qtd))
    db.session.commit()
    return p


def _saldo(receita):
    ep = EstoqueProducao.query.filter_by(receita_id=receita.id).first()
    return int(ep.quantidade or 0) if ep else 0


def _enviar(app, pedido, user):
    from app.blueprints.pedidos.routes import _executar_envio_pedido
    with app.test_request_context():
        return _executar_envio_pedido(pedido, user)


def _creditar(receita, qtd, user):
    from app.services.estoque_congelados import entrada_producao
    entrada_producao(receita_id=receita.id, quantidade=qtd,
                     usuario_id=user.id)
    db.session.commit()


# ── Registro na saída ────────────────────────────────────────────────────────

def test_saida_sem_saldo_registra_falta_e_avisa(app, admin_user):
    from app.blueprints.pedidos.routes import _MSG_SAIDA_COM_FALTA
    r = _receita()
    loja = _loja()
    db.session.add(EstoqueProducao(receita_id=r.id, quantidade=4))
    db.session.commit()
    p = _pedido(loja, r, 22)

    ok, msg = _enviar(app, p, admin_user)

    assert ok and msg.startswith(_MSG_SAIDA_COM_FALTA)
    assert 'tela do padeiro' in msg
    f = FaltaSaidaIndustria.query.one()
    assert (f.pedido_id, f.receita_id, f.quantidade, f.quitada) == (p.id, r.id, 18, 0)
    assert f.encerrada_em is None and f.aberta_qtd == 18
    assert _saldo(r) == 0                       # nunca negativo


def test_saida_com_saldo_nao_registra_falta(app, admin_user):
    r = _receita()
    db.session.add(EstoqueProducao(receita_id=r.id, quantidade=30))
    db.session.commit()
    p = _pedido(_loja(), r, 22)
    ok, msg = _enviar(app, p, admin_user)
    assert ok and 'ATENÇÃO' not in msg
    assert FaltaSaidaIndustria.query.count() == 0


def test_abertas_agrupa_por_item_e_massa_nao_e_quitavel(app, admin_user):
    from app.services import faltas_industria
    r = _receita()
    massa = _receita('Massa para folhar')
    loja1, loja2 = _loja('Loja Um'), _loja('Loja Dois')
    _enviar(app, _pedido(loja1, r, 10), admin_user)
    _enviar(app, _pedido(loja2, r, 8), admin_user)
    _enviar(app, _pedido(loja1, massa, 2), admin_user)
    db.session.commit()

    grupos = {g['ref']: g for g in faltas_industria.abertas_recentes()}
    g = grupos[f'receita:{r.id}']
    assert g['quantidade'] == 18 and g['quitavel'] is True
    assert {p['loja'] for p in g['pedidos']} == {'Loja Um', 'Loja Dois'}
    assert grupos[f'receita:{massa.id}']['quitavel'] is False

    resumo = faltas_industria.resumo_por_ref()
    assert resumo[f'receita:{r.id}']['quantidade'] == 18
    assert f'receita:{massa.id}' not in resumo   # massa só na contagem


# ── Quitação (confirmação humana) ────────────────────────────────────────────

def test_quitar_debita_mais_antiga_primeiro_e_encerra(app, admin_user):
    from app.services import faltas_industria
    r = _receita()
    loja = _loja()
    p1 = _pedido(loja, r, 10)
    p2 = _pedido(loja, r, 8)
    _enviar(app, p1, admin_user)
    _enviar(app, p2, admin_user)
    _creditar(r, 40, admin_user)                 # produção lançada depois

    total = faltas_industria.quitar(receita_id=r.id, maximo=14,
                                    usuario_id=admin_user.id)
    db.session.commit()

    assert total == 14
    assert _saldo(r) == 26                       # 40 − 14
    f1, f2 = FaltaSaidaIndustria.query.order_by(FaltaSaidaIndustria.id).all()
    assert f1.motivo_encerramento == 'quitada' and f1.quitada == 10
    assert f2.encerrada_em is None and f2.quitada == 4 and f2.aberta_qtd == 4
    movs = (MovEstoqueProducao.query
            .filter_by(tipo=faltas_industria.TIPO_QUITADA)
            .order_by(MovEstoqueProducao.id).all())
    assert [m.quantidade for m in movs] == [10, 4]
    assert movs[0].referencia.startswith(f'Pedido #{p1.id} →')
    assert movs[1].referencia.startswith(f'Pedido #{p2.id} →')


def test_quitar_nunca_deixa_saldo_negativo(app, admin_user):
    from app.services import faltas_industria
    r = _receita()
    _enviar(app, _pedido(_loja(), r, 20), admin_user)
    _creditar(r, 5, admin_user)
    total = faltas_industria.quitar(receita_id=r.id, maximo=20,
                                    usuario_id=admin_user.id)
    db.session.commit()
    assert total == 5 and _saldo(r) == 0
    f = FaltaSaidaIndustria.query.one()
    assert f.aberta_qtd == 15 and f.encerrada_em is None


def test_quitar_ignora_falta_fora_da_janela(app, admin_user):
    """Falta antiga quase sempre é produção nunca lançada: quitá-la comeria
    produção nova. Fica aberta até uma contagem."""
    from app.services import faltas_industria
    r = _receita()
    _enviar(app, _pedido(_loja(), r, 10), admin_user)
    f = FaltaSaidaIndustria.query.one()
    f.criada_em = agora() - timedelta(days=faltas_industria.JANELA_DIAS + 1)
    db.session.commit()
    _creditar(r, 10, admin_user)

    assert faltas_industria.quitar(receita_id=r.id, maximo=10,
                                   usuario_id=admin_user.id) == 0
    assert _saldo(r) == 10
    assert faltas_industria.resumo_por_ref() == {}
    assert faltas_industria.abertas_recentes() == []


def test_quitar_massa_para_folhar_nao_faz_nada(app, admin_user):
    from app.services import faltas_industria
    massa = _receita('Massa para folhar')
    _enviar(app, _pedido(_loja(), massa, 2), admin_user)
    assert faltas_industria.quitar(receita_id=massa.id, maximo=2,
                                   usuario_id=admin_user.id) == 0


# ── Estorno, contagem, exclusão ──────────────────────────────────────────────

def test_estorno_devolve_quitacao_e_encerra_faltas(app, admin_user):
    from app.blueprints.pedidos.routes import _aplicar_voltar_status
    from app.services import faltas_industria
    r = _receita()
    db.session.add(EstoqueProducao(receita_id=r.id, quantidade=4))
    db.session.commit()
    p = _pedido(_loja(), r, 22)
    _enviar(app, p, admin_user)                  # baixa 4, falta 18
    _creditar(r, 18, admin_user)
    faltas_industria.quitar(receita_id=r.id, maximo=10,
                            usuario_id=admin_user.id)   # quita 10
    db.session.commit()
    assert _saldo(r) == 8

    _aplicar_voltar_status(p, admin_user.id)
    db.session.commit()

    assert _saldo(r) == 22                       # 8 + 4 da saída + 10 quitados
    f = FaltaSaidaIndustria.query.one()
    assert f.motivo_encerramento == 'estorno'
    assert faltas_industria.do_pedido(p.id) == []   # saída estornada some


def test_conferencia_encerra_faltas_mesmo_sem_diferenca(app, admin_user, cliente):
    r = _receita()
    _enviar(app, _pedido(_loja(), r, 10), admin_user)
    ep = EstoqueProducao.query.filter_by(receita_id=r.id).one()
    _login(cliente, admin_user)
    resp = cliente.post('/pedidos/congelados/conferencia',
                        data={f'real_{ep.id}': '0'})
    assert resp.status_code == 302
    f = db.session.get(FaltaSaidaIndustria, FaltaSaidaIndustria.query.one().id)
    db.session.refresh(f)
    assert f.motivo_encerramento == 'contagem'
    assert f.encerrada_por_id == admin_user.id


def test_conferencia_de_outro_item_nao_encerra(app, admin_user, cliente):
    r = _receita()
    outro = _receita('Outro Item')
    db.session.add(EstoqueProducao(receita_id=outro.id, quantidade=3))
    db.session.commit()
    _enviar(app, _pedido(_loja(), r, 10), admin_user)
    ep_outro = EstoqueProducao.query.filter_by(receita_id=outro.id).one()
    _login(cliente, admin_user)
    cliente.post('/pedidos/congelados/conferencia',
                 data={f'real_{ep_outro.id}': '3'})
    f = FaltaSaidaIndustria.query.one()
    db.session.refresh(f)
    assert f.encerrada_em is None


def test_balanco_encerra_faltas_do_item_contado(app, admin_user):
    from app.services.estoque_congelados import aplicar_balanco
    r = _receita()
    _enviar(app, _pedido(_loja(), r, 10), admin_user)
    aplicar_balanco([{'quantidade': 7, 'resolvido': {
        'tipo': 'receita', 'id': r.id, 'nome': r.nome}}], admin_user)
    f = FaltaSaidaIndustria.query.one()
    db.session.refresh(f)
    assert f.motivo_encerramento == 'contagem'
    assert _saldo(r) == 7


def test_excluir_pedido_encerra_e_solta_vinculo(app, admin_user, cliente):
    r = _receita()
    p = _pedido(_loja(), r, 10)
    _enviar(app, p, admin_user)
    pid = p.id
    p.data_entrega = hoje() + timedelta(days=5)   # fora do corte das 12h
    db.session.commit()
    _login(cliente, admin_user)
    resp = cliente.post(f'/pedidos/{pid}/excluir')
    assert resp.status_code == 302
    assert db.session.get(PedidoLoja, pid) is None
    f = FaltaSaidaIndustria.query.one()
    db.session.refresh(f)
    assert f.pedido_id is None and f.motivo_encerramento == 'excluido'


# ── TV do padeiro ────────────────────────────────────────────────────────────

def _plano_com_item(receita, alvo=30):
    from app.models import PlanejamentoItem, PlanejamentoProducao
    pl = PlanejamentoProducao(data=hoje(), origem='cronograma',
                              enviado_ao_padeiro=True)
    db.session.add(pl)
    db.session.flush()
    it = PlanejamentoItem(planejamento_id=pl.id, receita_id=receita.id,
                          qtd_alvo=alvo, produzido_qtd=0)
    db.session.add(it)
    db.session.commit()
    return it


def test_produzir_plano_com_confirmacao_quita(app, admin_user, cliente):
    r = _receita()
    _enviar(app, _pedido(_loja(), r, 18), admin_user)
    it = _plano_com_item(r, alvo=30)
    _login(cliente, admin_user)
    resp = cliente.post(f'/padeiro/produzir-plano/{it.id}',
                        data={'unidades': '30', 'quitar_faltas': '1'})
    assert resp.status_code == 302
    assert _saldo(r) == 12                       # 30 produzidos − 18 que saíram
    f = FaltaSaidaIndustria.query.one()
    db.session.refresh(f)
    assert f.motivo_encerramento == 'quitada'
    with cliente.session_transaction() as sess:
        textos = [m for _c, m in sess.get('_flashes', [])]
    assert any('já tinham saído' in t for t in textos)


def test_produzir_plano_sem_confirmacao_nao_quita(app, admin_user, cliente):
    r = _receita()
    _enviar(app, _pedido(_loja(), r, 18), admin_user)
    it = _plano_com_item(r, alvo=30)
    _login(cliente, admin_user)
    cliente.post(f'/padeiro/produzir-plano/{it.id}', data={'unidades': '30'})
    assert _saldo(r) == 30
    f = FaltaSaidaIndustria.query.one()
    db.session.refresh(f)
    assert f.encerrada_em is None and f.aberta_qtd == 18


def test_producao_extra_com_confirmacao_quita(app, admin_user, cliente):
    r = _receita()
    _enviar(app, _pedido(_loja(), r, 18), admin_user)
    _login(cliente, admin_user)
    resp = cliente.post('/padeiro/produzir', json={'itens': [
        {'ref': f'receita:{r.id}', 'quantidade': 25, 'quitar_faltas': True}]})
    assert resp.status_code == 200, resp.get_json()
    resumo = resp.get_json()['resumo']
    assert resumo[0]['quitado'] == 18
    assert _saldo(r) == 7


def test_producao_extra_quitar_so_com_true_literal(app, admin_user, cliente):
    """'1'/'sim' não confirmam: só o booleano que a tela manda."""
    r = _receita()
    _enviar(app, _pedido(_loja(), r, 18), admin_user)
    _login(cliente, admin_user)
    resp = cliente.post('/padeiro/produzir', json={'itens': [
        {'ref': f'receita:{r.id}', 'quantidade': 25, 'quitar_faltas': 'sim'}]})
    assert resp.status_code == 200
    assert _saldo(r) == 25


def test_recibo_com_quitacao_nao_bate_com_envio_sem(app, admin_user):
    """A mesma chave de envio com e sem quitação é outro conteúdo: recusa."""
    from app.services.producao_avulsa import registrar_lote
    r = _receita()
    _enviar(app, _pedido(_loja(), r, 18), admin_user)
    chave = 'k' * 20
    registrar_lote([('receita', r, 25)], admin_user.id, chave)
    db.session.commit()
    with pytest.raises(ValueError):
        registrar_lote([('receita', r, 25)], admin_user.id, chave,
                       quitar={f'receita:{r.id}'})
    db.session.rollback()


def test_faltas_json_e_card_da_tv(app, admin_user, cliente):
    r = _receita()
    p = _pedido(_loja('Loja Card'), r, 18)
    _enviar(app, p, admin_user)
    _login(cliente, admin_user)

    dados = cliente.get('/padeiro/faltas.json').get_json()
    assert dados['ok'] is True
    assert dados['faltas'][f'receita:{r.id}']['quantidade'] == 18

    html = cliente.get('/padeiro/').get_data(as_text=True)
    assert 'Saíram sem produção lançada' in html
    assert f'pedido #{p.id}' in html
    assert f'"receita:{r.id}"' in html           # FALTAS da pergunta

    listas = cliente.get('/padeiro/listas.html').get_data(as_text=True)
    assert 'Saíram sem produção lançada' in listas


def test_tv_de_outro_dia_nao_mostra_card(app, admin_user, cliente):
    r = _receita()
    _enviar(app, _pedido(_loja(), r, 18), admin_user)
    _login(cliente, admin_user)
    ontem = (hoje() - timedelta(days=1)).isoformat()
    html = cliente.get(f'/padeiro/listas.html?data={ontem}').get_data(as_text=True)
    assert 'Saíram sem produção lançada' not in html


# ── QR de saída e botão Enviar ───────────────────────────────────────────────

def test_qr_de_saida_mostra_a_falta(app, admin_user):
    from app.models import Driver, HandshakeAudit, PedidoItemFoto, PedidoQRCode
    r = _receita()
    loja = _loja()
    loja.nf_dispensada = True                    # sem Tiny neste teste
    d = Driver(nome='Moto Falta', ativo=True, pin='5151', token='tok-drv-falta')
    db.session.add(d)
    db.session.commit()
    p = _pedido(loja, r, 18)
    p.driver_id = d.id
    for it in p.itens:
        db.session.add(PedidoItemFoto(pedido_item_id=it.id, etapa='saida',
                                      imagem_url='https://x/f.jpg'))
    qr = PedidoQRCode(token='tok-falta-saida', pedido_id=p.id, tipo='saida',
                      expira_em=agora() + timedelta(hours=2))
    db.session.add(qr)
    db.session.commit()

    client = app.test_client()
    resp = client.post(f'/handshake/{qr.token}', data={'pin': '5151'})
    assert resp.status_code == 303
    body = client.get(f'/handshake/{qr.token}/sucesso').get_data(as_text=True)
    assert 'Saiu sem estoque no sistema' in body
    assert r.nome in body
    assert HandshakeAudit.query.filter_by(pedido_id=p.id,
                                          etapa='falta_saida').count() == 1


def test_botao_enviar_avisa_em_amarelo(app, admin_user, cliente):
    r = _receita()
    p = _pedido(_loja(), r, 18)
    _login(cliente, admin_user)
    resp = cliente.post(f'/pedidos/{p.id}/enviar')
    assert resp.status_code == 302
    with cliente.session_transaction() as sess:
        flashes = sess.get('_flashes', [])
    assert flashes and flashes[-1][0] == 'warning'


# ── Auditoria das baixas ─────────────────────────────────────────────────────

def test_auditoria_trata_quitada_como_baixa(app, admin_user):
    from app.services import auditoria_baixa_pedidos, faltas_industria
    r = _receita()
    p = _pedido(_loja(), r, 10)
    _enviar(app, p, admin_user)
    _creditar(r, 10, admin_user)
    faltas_industria.quitar(receita_id=r.id, maximo=10, usuario_id=admin_user.id)
    db.session.commit()

    res = auditoria_baixa_pedidos.auditar(dias=7)
    assert res['resumo']['ok'] == 1 and res['resumo']['com_falta'] == 0
    item = res['faltas_por_item'][0]
    assert item['faltou'] == 10 and item['quitada'] == 10


def test_transferir_vinculos_leva_a_falta(app, admin_user, cliente):
    origem = _receita('Brioche Velho')
    destino = _receita('Brioche Novo')
    _enviar(app, _pedido(_loja(), origem, 6), admin_user)
    _login(cliente, admin_user)
    resp = cliente.post(f'/receitas/{origem.id}/vinculos/transferir',
                        data={'destino': destino.nome, 'tipo_destino': 'receita'})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    f = FaltaSaidaIndustria.query.one()
    db.session.refresh(f)
    assert f.receita_id == destino.id
