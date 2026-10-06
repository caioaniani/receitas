"""Faltas de saída da indústria (05/10/2026, caso brioche 76 x 22; opção A
do dono).

A saída do pedido desconta só o saldo do sistema; o resto vira
`saida_pedido_sem_estoque` (neutro) e agora também uma `FaltaSaidaIndustria`.
A falta aparece na hora (QR de saída, botão Enviar, TV do padeiro) e, ao
lançar a produção, só a CONFIRMAÇÃO do padeiro grava o débito
(`saida_pedido_quitada`, referência do pedido). Contagem encerra; estorno
devolve a quitação; saldo nunca fica negativo.
"""
from datetime import timedelta

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
                   data_entrega=hoje() + timedelta(days=1))
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
    from flask import g

    from app.blueprints.pedidos.routes import _executar_envio_pedido
    with app.test_request_context():
        res = _executar_envio_pedido(pedido, user)
    # O app context do conftest fica empilhado o teste inteiro e o `g` é
    # compartilhado: o audit leu o usuário anônimo deste contexto e o
    # Flask-Login o deixou em cache — as requisições seguintes do cliente
    # herdariam o anônimo (armadilha documentada no CLAUDE.md).
    g.pop('_login_user', None)
    return res


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
                        data={'unidades': '30', 'quitar_faltas': '18'})
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
        {'ref': f'receita:{r.id}', 'quantidade': 25, 'quitar_faltas': 18}]})
    assert resp.status_code == 200, resp.get_json()
    resumo = resp.get_json()['resumo']
    assert resumo[0]['quitado'] == 18
    assert _saldo(r) == 7


@pytest.mark.parametrize('valor', [True, 'sim', '1', 0, -5, None])
def test_producao_extra_quitar_so_com_numero_confirmado(app, admin_user, cliente, valor):
    """Só o número que a pergunta mostrou confirma: booleano, texto, zero
    ou negativo não quitam nada."""
    r = _receita()
    _enviar(app, _pedido(_loja(), r, 18), admin_user)
    _login(cliente, admin_user)
    resp = cliente.post('/padeiro/produzir', json={'itens': [
        {'ref': f'receita:{r.id}', 'quantidade': 25, 'quitar_faltas': valor}]})
    assert resp.status_code == 200
    assert _saldo(r) == 25


def test_quitacao_nunca_passa_do_numero_confirmado(app, admin_user, cliente):
    """A pergunta mostrou 5; enquanto ela estava aberta saiu outro pedido com
    falta de 20. Confirmar quita só os 5 vistos (achado 2 da revisão)."""
    r = _receita()
    loja = _loja()
    _enviar(app, _pedido(loja, r, 5), admin_user)
    it = _plano_com_item(r, alvo=30)
    _enviar(app, _pedido(loja, r, 20), admin_user)     # depois da pergunta
    _login(cliente, admin_user)
    cliente.post(f'/padeiro/produzir-plano/{it.id}',
                 data={'unidades': '30', 'quitar_faltas': '5'})
    assert _saldo(r) == 25                              # 30 − 5
    abertas = [f for f in FaltaSaidaIndustria.query.order_by(FaltaSaidaIndustria.id)
               if f.encerrada_em is None]
    assert [f.aberta_qtd for f in abertas] == [20]


def test_quitacao_nunca_passa_do_lancado(app, admin_user, cliente):
    r = _receita()
    _enviar(app, _pedido(_loja(), r, 18), admin_user)
    _login(cliente, admin_user)
    resp = cliente.post('/padeiro/produzir', json={'itens': [
        {'ref': f'receita:{r.id}', 'quantidade': 10, 'quitar_faltas': 99}]})
    assert resp.get_json()['resumo'][0]['quitado'] == 10
    assert _saldo(r) == 0


def test_ref_nao_canonica_ainda_quita(app, admin_user, cliente):
    """'receita:05' vira 'receita:5' (chave das faltas) — antes a quitação
    sumia sem aviso."""
    r = _receita()
    _enviar(app, _pedido(_loja(), r, 18), admin_user)
    _login(cliente, admin_user)
    resp = cliente.post('/padeiro/produzir', json={'itens': [
        {'ref': f'receita:0{r.id}', 'quantidade': 25, 'quitar_faltas': 18}]})
    assert resp.get_json()['resumo'][0]['quitado'] == 18


def test_mesma_chave_com_outra_escolha_nao_credita_de_novo(app, admin_user, cliente):
    """Timeout: o servidor gravou +25 com quitação; a repetição com a caixa
    desmarcada usa a MESMA chave (a tela só troca a chave quando a produção
    muda) e é recusada — nunca um segundo crédito (achado 1 da revisão)."""
    r = _receita()
    _enviar(app, _pedido(_loja(), r, 18), admin_user)
    _login(cliente, admin_user)
    chave = 'c' * 32
    item = {'ref': f'receita:{r.id}', 'quantidade': 25}
    ok = cliente.post('/padeiro/produzir', json={
        'itens': [dict(item, quitar_faltas=18)], 'chave_envio': chave})
    assert ok.status_code == 200
    assert _saldo(r) == 7

    for repeticao in ([item], [dict(item, quitar_faltas=10)]):
        resp = cliente.post('/padeiro/produzir', json={
            'itens': repeticao, 'chave_envio': chave})
        assert resp.status_code == 409
        assert resp.get_json()['ja_registrado'] is True
        assert 'já foi registrada' in resp.get_json()['erro']
    assert _saldo(r) == 7

    # A MESMA escolha repetida devolve o recibo, sem crédito novo.
    rep = cliente.post('/padeiro/produzir', json={
        'itens': [dict(item, quitar_faltas=18)], 'chave_envio': chave})
    assert rep.status_code == 200 and rep.get_json()['resumo'][0]['quitado'] == 18
    assert _saldo(r) == 7


def test_recibo_antigo_sem_quitacao_continua_batendo(app, admin_user):
    """Recibo sem `assinatura_base` (gravado antes desta mudança) segue
    reconhecido como a mesma produção."""
    import json

    from app.models import AppConfig
    from app.services.producao_avulsa import registrar_lote
    r = _receita()
    chave = 'k' * 20
    registrar_lote([('receita', r, 25)], admin_user.id, chave)
    rec = db.session.get(AppConfig, f'producao_tv:{admin_user.id}:{chave}')
    dados = json.loads(rec.value)
    dados.pop('assinatura_base')
    rec.value = json.dumps(dados)
    db.session.commit()
    assert registrar_lote([('receita', r, 25)], admin_user.id, chave)[0]['qtd'] == 25
    with pytest.raises(ValueError, match='já foi registrada'):
        registrar_lote([('receita', r, 25)], admin_user.id, chave,
                       quitar={f'receita:{r.id}': 5})
    db.session.rollback()
    assert _saldo(r) == 25


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


# ── Achados da revisão (05/10/2026) ──────────────────────────────────────────

def test_estorno_apos_quitacao_total_e_novo_envio_mostra_so_a_saida_atual(app, admin_user):
    """Falta quitada por inteiro, estorno (que devolve a quitação) e novo
    envio: a tela do QR mostra só a falta da saída nova (achado 6)."""
    from app.blueprints.pedidos.routes import _aplicar_voltar_status
    from app.services import faltas_industria
    r = _receita()
    p = _pedido(_loja(), r, 10)
    _enviar(app, p, admin_user)
    _creditar(r, 10, admin_user)
    faltas_industria.quitar(receita_id=r.id, maximo=10, usuario_id=admin_user.id)
    db.session.commit()
    assert _saldo(r) == 0

    _aplicar_voltar_status(p, admin_user.id)
    db.session.commit()
    assert _saldo(r) == 10                      # quitação devolvida
    antiga = FaltaSaidaIndustria.query.one()
    assert antiga.motivo_encerramento == 'estorno' and antiga.quitada == 10
    assert faltas_industria.do_pedido(p.id) == []

    p.status = 'separado'
    p.itens[0].quantidade = 14
    db.session.commit()
    _enviar(app, p, admin_user)                  # baixa 10, falta 4
    vistas = faltas_industria.do_pedido(p.id)
    assert [(v['quantidade'], v['quitada']) for v in vistas] == [(4, 0)]


def test_conferencia_nao_encerra_falta_de_saida_posterior_ao_inicio(app, admin_user, cliente):
    """Tela da conferência aberta ANTES da saída e enviada depois: a falta
    nova não estava no que foi contado e continua aberta (achado 4)."""
    r = _receita()
    db.session.add(EstoqueProducao(receita_id=r.id, quantidade=0))
    db.session.commit()
    ep = EstoqueProducao.query.filter_by(receita_id=r.id).one()
    inicio = (agora() - timedelta(minutes=30)).isoformat(timespec='seconds')
    _enviar(app, _pedido(_loja(), r, 10), admin_user)
    _login(cliente, admin_user)
    cliente.post('/pedidos/congelados/conferencia',
                 data={f'real_{ep.id}': '0', 'iniciada_em': inicio})
    f = FaltaSaidaIndustria.query.one()
    db.session.refresh(f)
    assert f.encerrada_em is None


def test_conferencia_tela_traz_o_inicio_da_contagem(app, admin_user, cliente):
    r = _receita()
    db.session.add(EstoqueProducao(receita_id=r.id, quantidade=3))
    db.session.commit()
    _login(cliente, admin_user)
    html = cliente.get('/pedidos/congelados/conferencia').get_data(as_text=True)
    assert 'name="iniciada_em"' in html


@pytest.mark.parametrize('valor', ['', 'lixo', '2999-01-01T00:00:00',
                                   '2026-10-05T10:00:00+00:00'])
def test_inicio_contagem_invalido_ou_futuro_vira_agora(app, valor):
    from app.services import faltas_industria
    antes = agora()
    assert faltas_industria.inicio_contagem(valor) >= antes


def test_janela_vale_o_dia_inteiro(app, admin_user):
    """Por data, não por 72 h: falta de 3 dias atrás bem cedo ainda conta."""
    from datetime import datetime, time

    from app.services import faltas_industria
    r = _receita()
    _enviar(app, _pedido(_loja(), r, 10), admin_user)
    f = FaltaSaidaIndustria.query.one()
    f.criada_em = datetime.combine(
        hoje() - timedelta(days=faltas_industria.JANELA_DIAS), time(0, 5))
    db.session.commit()
    assert faltas_industria.resumo_por_ref()[f'receita:{r.id}']['quantidade'] == 10


def test_transferir_receita_para_mp_encerra_as_faltas(app, admin_user, cliente):
    from app.models import MateriaPrima
    from app.services import faltas_industria
    r = _receita('Recheio Virou MP')
    mp = MateriaPrima(nome='Recheio MP', unidade='g')
    db.session.add(mp)
    db.session.commit()
    p = _pedido(_loja(), r, 6)
    _enviar(app, p, admin_user)
    _login(cliente, admin_user)
    resp = cliente.post(f'/receitas/{r.id}/vinculos/transferir',
                        data={'destino': mp.nome, 'tipo_destino': 'mp'})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    f = FaltaSaidaIndustria.query.one()
    db.session.refresh(f)
    assert f.motivo_encerramento == 'transferido'
    assert faltas_industria.resumo_por_ref() == {}
    assert faltas_industria.do_pedido(p.id) == []


def test_mensagem_do_enviar_nao_promete_tv_para_materia_prima(app, admin_user):
    from app.models import MateriaPrima
    mp = MateriaPrima(nome='Manteiga Pedido', unidade='un',
                      estoque_atual=2, sugerir_pedido_loja=True)
    db.session.add(mp)
    loja = _loja()
    db.session.commit()
    p = PedidoLoja(loja_id=loja.id, status='separado',
                   data_entrega=hoje() + timedelta(days=1))
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p.id, materia_prima_id=mp.id, quantidade=5))
    db.session.commit()
    ok, msg = _enviar(app, p, admin_user)
    assert ok and 'Manteiga Pedido' in msg
    assert 'tela do padeiro: se a produção' not in msg
    assert 'não aparece na tela do padeiro' in msg
    assert FaltaSaidaIndustria.query.count() == 0


def test_copilot_enviar_avisa_a_falta(app, admin_user):
    from app.services.copilot import executar_mudar_status_pedido
    from app.services.slack_blocks import build_resultado
    r = _receita()
    p = _pedido(_loja(), r, 9)
    with app.test_request_context():
        res = executar_mudar_status_pedido(
            {'pedido_id': p.id, 'novo_status': 'enviar'}, admin_user)
    from flask import g
    g.pop('_login_user', None)
    assert res['ok'], res
    assert 'insuficiente na indústria' in res['falta_saida']
    texto = str(build_resultado(res, ok=True))
    assert 'insuficiente na indústria' in texto


def test_estorno_reencerra_com_quem_e_quando_do_estorno(app, admin_user):
    """Falta já contada e depois estornada: motivo, quando e quem passam a
    ser os do estorno (o histórico anterior fica no AuditLog)."""
    from app.blueprints.pedidos.routes import _aplicar_voltar_status
    from app.services import faltas_industria
    r = _receita()
    p = _pedido(_loja(), r, 10)
    _enviar(app, p, admin_user)
    faltas_industria.encerrar_por_contagem([(r.id, None)], None)
    db.session.commit()
    f = FaltaSaidaIndustria.query.one()
    assert f.motivo_encerramento == 'contagem' and f.encerrada_por_id is None
    _aplicar_voltar_status(p, admin_user.id)
    db.session.commit()
    db.session.refresh(f)
    assert f.motivo_encerramento == 'estorno' and f.encerrada_por_id == admin_user.id


def test_numero_gigante_no_formulario_nao_derruba(app, admin_user, cliente):
    r = _receita()
    _enviar(app, _pedido(_loja(), r, 18), admin_user)
    it = _plano_com_item(r, alvo=30)
    _login(cliente, admin_user)
    resp = cliente.post(f'/padeiro/produzir-plano/{it.id}',
                        data={'unidades': '30', 'quitar_faltas': '9' * 5000})
    assert resp.status_code == 302
    assert _saldo(r) == 30                       # número absurdo não quita


def test_registrar_lote_trata_true_como_nao_confirmado(app, admin_user):
    from app.services.producao_avulsa import registrar_lote
    r = _receita()
    _enviar(app, _pedido(_loja(), r, 18), admin_user)
    resumo = registrar_lote([('receita', r, 25)], admin_user.id,
                            quitar={f'receita:{r.id}': True})
    assert 'quitado' not in resumo[0]
    assert _saldo(r) == 25
