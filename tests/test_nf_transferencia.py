"""NF-e de TRANSFERÊNCIA indústria→loja via Tiny (20/07/2026).

Decisões do dono: valor dos itens = CUSTO calculado da ficha; MP entra na
NF (kind 'mp'); natureza NF_NATUREZA_TRANSFERENCIA; SKU herda site→b2b.
Emissão automática no scan do QR de saída é BEST-EFFORT: Tiny fora do ar
NUNCA trava a saída do caminhão. Tiny sempre mockado.
"""
from datetime import date, timedelta
from unittest.mock import patch

from app.extensions import db
from app.models import (
    Driver,
    MateriaPrima,
    PedidoItem,
    PedidoItemFoto,
    PedidoLoja,
    PedidoQRCode,
)
from app.utils import agora

CUSTOS_FAKE = {'custos': {'Croissant Tradicional': 3.5},
               'pesos': {}, 'fabricados': [], 'mp_dict': {},
               'mp_info': {}, 'circulares': []}


def _loja_fiscal(loja):
    loja.cnpj = '11.222.333/0001-44'
    loja.inscricao_estadual = '123456789'
    loja.endereco_logradouro = 'Rua das Lojas'
    loja.endereco_numero = '10'
    loja.endereco_bairro = 'Centro'
    loja.endereco_cep = '04568-001'
    loja.endereco_cidade = 'São Paulo'
    loja.endereco_uf = 'sp'
    db.session.commit()
    return loja


def _pedido(loja, catalogo, status='em_transporte', com_mp=False):
    p = PedidoLoja(loja_id=loja.id, status=status,
                   data_entrega=date.today() + timedelta(days=1))
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p.id,
                              receita_id=catalogo['receita'].id,
                              quantidade=5))
    if com_mp:
        mp = MateriaPrima(nome='Pao de Queijo Congelado', unidade='un',
                          custo_por_kg=2.0, sugerir_pedido_loja=True)
        db.session.add(mp)
        db.session.flush()
        db.session.add(PedidoItem(pedido_id=p.id, materia_prima_id=mp.id,
                                  quantidade=30))
    db.session.commit()
    return p


def _patch_custos():
    return patch('app.services.custos.calcular_custos_receitas',
                 return_value=dict(CUSTOS_FAKE))


def _patch_custos_produtos(valor=None):
    return patch('app.services.custos.calcular_custos_produtos',
                 return_value=(valor or {}))


# ── payload / emissão ────────────────────────────────────────────────────────

def test_emitir_nf_payload_completo(app, loja, catalogo):
    """Natureza de transferência, loja como destinatária PJ, item pelo
    CUSTO da ficha, MP pelo custo do cadastro ('un' = custo por unidade),
    persistência do trio de NF no pedido."""
    from app.services import tiny_nf, tiny_nf_transf
    _loja_fiscal(loja)
    p = _pedido(loja, catalogo, com_mp=True)
    # Receita SEM sku transf — herda do canal SITE (decisão do dono)
    tiny_nf.definir_sku('receita', catalogo['receita'].id, 'SKU-SITE-1',
                        canal='site')
    mp = MateriaPrima.query.filter_by(nome='Pao de Queijo Congelado').one()
    tiny_nf.definir_sku('mp', mp.id, 'SKU-MP-1', canal='transf')

    with _patch_custos(), _patch_custos_produtos(), \
         patch('app.services.tiny.incluir_nota_fiscal',
               return_value={'ok': True, 'id': 'nf-t1',
                             'numero': '900'}) as inc, \
         patch('app.services.tiny.emitir_nota_fiscal',
               return_value={'ok': True, 'status': 'autorizada'}) as emi:
        res = tiny_nf_transf.emitir_nf(p)
    assert res['ok'], res
    nota = inc.call_args[0][0]
    assert nota['natureza_operacao'] == \
        'TRANSFERÊNCIA DE PRODUÇÃO DO ESTABELECIMENTO'
    assert nota['tipo'] == 'S'
    cli = nota['cliente']
    assert cli['tipo_pessoa'] == 'J'
    assert cli['cpf_cnpj'] == '11222333000144'
    assert cli['uf'] == 'SP'
    assert cli['ie'] == '123456789'
    itens = {i['item']['codigo']: i['item'] for i in nota['itens']}
    assert itens['SKU-SITE-1']['quantidade'] == 5.0
    assert itens['SKU-SITE-1']['valor_unitario'] == 3.5   # custo da ficha
    assert itens['SKU-MP-1']['quantidade'] == 30.0
    assert itens['SKU-MP-1']['valor_unitario'] == 2.0     # 'un' = por unidade
    emi.assert_called_once_with('nf-t1')
    db.session.refresh(p)
    assert p.tiny_nota_fiscal_id == 'nf-t1'
    assert p.nf_emitida_em is not None
    assert p.nf_numero == '900'


def test_sku_transf_explicito_vence_o_herdado(app, loja, catalogo):
    from app.services import tiny_nf, tiny_nf_transf
    tiny_nf.definir_sku('receita', catalogo['receita'].id, 'SKU-SITE',
                        canal='site')
    tiny_nf.definir_sku('receita', catalogo['receita'].id, 'SKU-TRANSF',
                        canal='transf')
    assert tiny_nf_transf.sku_transferencia(
        'receita', catalogo['receita'].id) == 'SKU-TRANSF'


def test_sugestao_fuzzy_nao_confirmada_nao_vale_na_nf(app, catalogo):
    """Chute do sync (auto_match sem confirmado_em) NÃO entra em documento
    fiscal — nem no canal transf (cai no herdado confirmado do site), nem
    sozinho (vira pendente). Achado A1 da revisão."""
    from app.extensions import db as _db
    from app.models import TinyProdutoMap
    from app.services import tiny_nf, tiny_nf_transf
    rid = catalogo['receita'].id
    # transf = sugestão fuzzy pendente; site = confirmado por humano
    _db.session.add(TinyProdutoMap(canal='transf', kind='receita',
                                   item_id=rid, tiny_sku='SKU-CHUTE',
                                   auto_match=True))
    _db.session.commit()
    tiny_nf.definir_sku('receita', rid, 'SKU-SITE-OK', canal='site')
    assert tiny_nf_transf.sku_transferencia('receita', rid) == 'SKU-SITE-OK'
    # Sem herança confirmada: sugestão pendente sozinha = None (pendente)
    m = TinyProdutoMap.query.filter_by(canal='site', kind='receita',
                                       item_id=rid).one()
    _db.session.delete(m)
    _db.session.commit()
    assert tiny_nf_transf.sku_transferencia('receita', rid) is None


def test_produto_inativo_usa_custo_da_composicao(app, loja, catalogo):
    """Pedido antigo com produto soft-deletado: o custo sai da composição
    direta (calcular_custo_produto), não aborta com msg enganosa de
    'corrija a ficha'. Achado A3 da revisão."""
    from app.services import tiny_nf, tiny_nf_transf
    _loja_fiscal(loja)
    catalogo['produto'].ativo = False
    catalogo['produto'].custo_direto = 4.4
    p = PedidoLoja(loja_id=loja.id, status='entregue',
                   data_entrega=date.today())
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p.id,
                              produto_id=catalogo['produto'].id,
                              quantidade=3))
    db.session.commit()
    tiny_nf.definir_sku('produto', catalogo['produto'].id, 'SKU-INATIVO',
                        canal='transf')
    with _patch_custos(), _patch_custos_produtos({}), \
         patch('app.services.tiny.incluir_nota_fiscal',
               return_value={'ok': True, 'id': 'nf-i'}) as inc, \
         patch('app.services.tiny.emitir_nota_fiscal',
               return_value={'ok': True, 'status': 'autorizada'}):
        res = tiny_nf_transf.emitir_nf(p)
    assert res['ok'], res
    item = inc.call_args[0][0]['itens'][0]['item']
    assert item['valor_unitario'] == 4.4           # custo_direto do inativo


def test_badge_fiscal_completo_usa_regua_da_emissao(app, loja):
    """Loja com CNPJ de 13 dígitos ou endereço pela metade NÃO é 'pronta
    pra NF' (achado A5 — o badge mentia)."""
    assert loja.fiscal_completo is False
    _loja_fiscal(loja)
    assert loja.fiscal_completo is True
    loja.cnpj = '1122233300014'                    # 13 dígitos
    db.session.commit()
    assert loja.fiscal_completo is False


def test_danfe_do_driver_exige_posse_do_pedido(app, loja, catalogo):
    """Motorista A não abre a DANFE de pedido coletado pelo motorista B
    (achado B2)."""
    p, qr = _armar_saida(loja, catalogo, com_fiscal=False)
    p.tiny_nota_fiscal_id = 'nf-posse'
    outro = Driver(nome='Outro Moto', ativo=True, pin='8888',
                   token='tok-outro-drv')
    db.session.add(outro)
    db.session.commit()
    client = app.test_client()
    with client.session_transaction() as sess:
        sess[f'driver_auth_{outro.id}'] = True
    resp = client.get(f'/driver/{outro.token}/pedido/{p.id}/danfe')
    assert resp.status_code == 403


def test_loja_sem_cnpj_aborta_sem_chamar_tiny(app, loja, catalogo):
    from app.services import tiny_nf_transf
    p = _pedido(loja, catalogo)                    # loja SEM dados fiscais
    with patch('app.services.tiny.incluir_nota_fiscal') as inc:
        res = tiny_nf_transf.emitir_nf(p)
    assert not res['ok']
    assert 'CNPJ' in res['msg']
    inc.assert_not_called()


def test_item_sem_sku_aborta(app, loja, catalogo):
    from app.services import tiny_nf_transf
    _loja_fiscal(loja)
    p = _pedido(loja, catalogo)                    # nenhum SKU em canal algum
    with _patch_custos(), _patch_custos_produtos(), \
         patch('app.services.tiny.incluir_nota_fiscal') as inc:
        res = tiny_nf_transf.emitir_nf(p)
    assert not res['ok']
    assert 'SKU' in res['msg']
    inc.assert_not_called()


def test_custo_zerado_aborta(app, loja, catalogo):
    """NF de transferência a custo zero é mentira fiscal — corrige a ficha."""
    from app.services import tiny_nf, tiny_nf_transf
    _loja_fiscal(loja)
    p = _pedido(loja, catalogo)
    tiny_nf.definir_sku('receita', catalogo['receita'].id, 'SKU-X',
                        canal='transf')
    zerado = dict(CUSTOS_FAKE, custos={'Croissant Tradicional': 0})
    with patch('app.services.custos.calcular_custos_receitas',
               return_value=zerado), _patch_custos_produtos(), \
         patch('app.services.tiny.incluir_nota_fiscal') as inc:
        res = tiny_nf_transf.emitir_nf(p)
    assert not res['ok']
    assert 'CUSTO' in res['msg'].upper()
    inc.assert_not_called()


def test_status_antes_da_separacao_recusa(app, loja, catalogo):
    from app.services import tiny_nf_transf
    _loja_fiscal(loja)
    p = _pedido(loja, catalogo, status='confirmado')
    res = tiny_nf_transf.emitir_nf(p)
    assert not res['ok']
    assert 'separação' in res['msg']


def test_custo_unitario_mp_por_unidade_de_cadastro(app):
    from app.services.tiny_nf_transf import _custo_unitario_mp
    mp_g = MateriaPrima(nome='Farinha G', unidade='g', custo_por_kg=8.0)
    mp_un = MateriaPrima(nome='Caixa UN', unidade='un', custo_por_kg=1.5)
    assert _custo_unitario_mp(mp_g) == 0.008       # R$/g
    assert _custo_unitario_mp(mp_un) == 1.5        # R$/un


# ── coleta do QR: emissão automática best-effort ─────────────────────────────

def _armar_saida(loja, catalogo, com_fiscal=True):
    """Pedido separado + QR de saída + driver atribuído + fotos de saída."""
    if com_fiscal:
        _loja_fiscal(loja)
    d = Driver(nome='Moto Teste', ativo=True, pin='7777', token='tok-drv-nf')
    db.session.add(d)
    p = _pedido(loja, catalogo, status='separado')
    db.session.flush()
    p.driver_id = d.id
    for it in p.itens:
        db.session.add(PedidoItemFoto(pedido_item_id=it.id, etapa='saida',
                                      imagem_url='https://x/f.jpg'))
    qr = PedidoQRCode(token='tok-nf-saida', pedido_id=p.id, tipo='saida',
                      expira_em=agora() + timedelta(hours=2))
    db.session.add(qr)
    db.session.commit()
    return p, qr


def test_scan_do_qr_emite_nf_automaticamente(app, loja, catalogo):
    from app.services import tiny_nf
    tiny_nf.definir_sku('receita', catalogo['receita'].id, 'SKU-AUTO',
                        canal='transf')
    p, qr = _armar_saida(loja, catalogo)
    client = app.test_client()
    with _patch_custos(), _patch_custos_produtos(), \
         patch('app.services.tiny.incluir_nota_fiscal',
               return_value={'ok': True, 'id': 'nf-auto',
                             'numero': '901'}) as inc, \
         patch('app.services.tiny.emitir_nota_fiscal',
               return_value={'ok': True, 'status': 'autorizada'}):
        resp = client.post(f'/handshake/{qr.token}', data={'pin': '7777'})
    assert resp.status_code == 303
    assert inc.call_count == 1
    db.session.refresh(p)
    assert p.status == 'em_transporte'
    assert p.tiny_nota_fiscal_id == 'nf-auto'
    # Tela de sucesso (persistente) mostra o botão da DANFE
    body = client.get(f'/handshake/{qr.token}/sucesso').get_data(as_text=True)
    assert 'DANFE' in body


def test_tiny_fora_do_ar_nao_trava_a_saida(app, loja, catalogo):
    """BEST-EFFORT: exceção na emissão não derruba a coleta — o caminhão
    sai, a NF fica pendente pra reemissão manual."""
    p, qr = _armar_saida(loja, catalogo)
    client = app.test_client()
    with patch('app.services.tiny_nf_transf.emitir_nf',
               side_effect=RuntimeError('tiny caiu')):
        resp = client.post(f'/handshake/{qr.token}', data={'pin': '7777'})
    assert resp.status_code == 303                 # coleta confirmada
    db.session.refresh(p)
    assert p.status == 'em_transporte'
    assert p.tiny_nota_fiscal_id is None           # NF pendente


def test_loja_sem_fiscal_nao_trava_a_saida(app, loja, catalogo):
    """Loja sem CNPJ: a NF não sai (msg no audit), mas a coleta segue."""
    p, qr = _armar_saida(loja, catalogo, com_fiscal=False)
    client = app.test_client()
    with patch('app.services.tiny.incluir_nota_fiscal') as inc:
        resp = client.post(f'/handshake/{qr.token}', data={'pin': '7777'})
    assert resp.status_code == 303
    inc.assert_not_called()
    db.session.refresh(p)
    assert p.status == 'em_transporte'


# ── DANFE nas telas ──────────────────────────────────────────────────────────

def test_danfe_handshake_sem_nf_avisa(app, loja, catalogo):
    p, qr = _armar_saida(loja, catalogo, com_fiscal=False)
    qr.usado_em = agora()
    db.session.commit()
    client = app.test_client()
    resp = client.get(f'/handshake/{qr.token}/danfe')
    assert resp.status_code == 404
    assert 'ainda não foi emitida' in resp.get_data(as_text=True)


def test_danfe_handshake_redireciona_pro_link(app, loja, catalogo):
    p, qr = _armar_saida(loja, catalogo, com_fiscal=False)
    qr.usado_em = agora()
    p.tiny_nota_fiscal_id = 'nf-99'
    db.session.commit()
    client = app.test_client()
    with patch('app.services.tiny.obter_link_nota_fiscal_com_motivo',
               return_value=('https://tiny/danfe.pdf', None)):
        resp = client.get(f'/handshake/{qr.token}/danfe')
    assert resp.status_code == 302
    assert resp.headers['Location'] == 'https://tiny/danfe.pdf'


def test_universo_transf_inclui_mp_pedivel(app, catalogo):
    from app.services import tiny_nf
    mp = MateriaPrima(nome='MP Pedivel X', unidade='un', custo_por_kg=1,
                      sugerir_pedido_loja=True)
    db.session.add(mp)
    db.session.commit()
    itens = tiny_nf.itens_para_mapear(canal='transf')
    kinds = {(i['kind'], i['nome']) for i in itens}
    assert ('mp', 'MP Pedivel X') in kinds
    assert ('receita', catalogo['receita'].nome) in kinds
    assert ('produto', catalogo['produto'].nome) in kinds


def test_produto_no_pedido_usa_custo_de_produto(app, loja, catalogo):
    from app.services import tiny_nf, tiny_nf_transf
    _loja_fiscal(loja)
    p = PedidoLoja(loja_id=loja.id, status='em_transporte',
                   data_entrega=date.today())
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p.id,
                              produto_id=catalogo['produto'].id,
                              quantidade=2))
    db.session.commit()
    tiny_nf.definir_sku('produto', catalogo['produto'].id, 'SKU-PROD',
                        canal='transf')
    with _patch_custos(), \
         _patch_custos_produtos({catalogo['produto'].nome: 7.25}), \
         patch('app.services.tiny.incluir_nota_fiscal',
               return_value={'ok': True, 'id': 'nf-p'}) as inc, \
         patch('app.services.tiny.emitir_nota_fiscal',
               return_value={'ok': True, 'status': 'autorizada'}):
        assert tiny_nf_transf.emitir_nf(p)['ok']
    item = inc.call_args[0][0]['itens'][0]['item']
    assert item['valor_unitario'] == 7.25


# ── Razão social da loja (20/07/2026, pedido do dono) ────────────────────

def test_nf_usa_razao_social_quando_cadastrada(app, loja, catalogo):
    """O `nome` da loja é apelido interno; a NF leva a razão social LEGAL
    quando preenchida (fallback: nome — comportamento antigo intacto)."""
    from app.services.tiny_nf_transf import _payload_cliente_loja
    with app.app_context():
        _loja_fiscal(loja)
        # Sem razão social: fallback no nome interno (como era).
        payload, erro = _payload_cliente_loja(loja)
        assert erro is None
        assert payload['nome'] == loja.nome
        # Com razão social: ela prevalece.
        loja.razao_social = 'O Pao Filial Brooklin LTDA'
        db.session.commit()
        payload, erro = _payload_cliente_loja(loja)
        assert payload['nome'] == 'O Pao Filial Brooklin LTDA'


def test_form_fiscal_salva_razao_social(app, owner_user, loja):
    # owner_user, nao admin: o blueprint RH inteiro esta restrito ao dono
    # por before_request temporario (rh/routes.py::_rh_restrito_ao_owner).
    with app.app_context():
        lid = loja.id
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(owner_user.id)
        sess['_fresh'] = True
    r = client.post(f'/rh/lojas/{lid}/fiscal', data={
        'razao_social': '  O Pao Filial Brooklin LTDA  ',
        'cnpj': '11.222.333/0001-44',
    })
    assert r.status_code in (302, 303)
    with app.app_context():
        from app.models import Loja
        lj = db.session.get(Loja, lid)
        assert lj.razao_social == 'O Pao Filial Brooklin LTDA'
