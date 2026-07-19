"""Baixa de estoque de pedido SEM itens via XML da NFC-e (19/07/2026).

Caso 99Food: a integração de delivery manda o pedido ao Seru só com o
TOTAL — mas a NFC-e emitida (taxInvoice.xmlUrl) lista os produtos reais
com os MESMOS nomes do SeruProdutoMap. O sync enriquece e baixa pelo
motor de sempre (pedido do dono: "as vendas pelo 99 têm que dar baixa
no estoque da loja"). API Seru e S3 SEMPRE mockados.
"""
from datetime import date
from unittest.mock import patch

PEDIDO_DIA = date(2026, 5, 20)
CREATED_AT = '2026-05-20T13:00:00Z'

XML_NF = '''<?xml version="1.0" encoding="UTF-8"?>
<nfeProc xmlns="http://www.portalfiscal.inf.br/nfe" versao="4.00">
 <NFe><infNFe>
  <det nItem="1"><prod><cProd>65</cProd><xProd>PAO FRANCES</xProd>
   <qCom>6.0000</qCom><vUnCom>4.0000</vUnCom><vProd>24.00</vProd></prod></det>
  <det nItem="2"><prod><cProd>9</cProd><xProd>Produto Sem Mapa</xProd>
   <qCom>1.0000</qCom><vUnCom>34.50</vUnCom><vProd>34.50</vProd></prod></det>
 </infNFe></NFe>
</nfeProc>'''


class _RespXML:
    status_code = 200

    def __init__(self, corpo=XML_NF):
        self.content = corpo.encode('utf-8')


class _Resp500:
    status_code = 500
    content = b''


def _pedido_sem_itens(pid, company, nf_url='https://s3/nf.xml',
                      nf_status='authorized'):
    p = {
        'id': pid,
        'createdAt': CREATED_AT,
        'canceledAt': None,
        'company': {'name': company},
        'total': 81.38,
        'items': [],
        'salesChannel': {'name': '99Food', 'tag': '99food'},
    }
    if nf_url is not None:
        p['taxInvoice'] = {'status': nf_status, 'number': 724,
                           'xml': '', 'xmlUrl': nf_url}
    return p


def _setup(qtd_estoque=10):
    from app.extensions import db
    from app.models import EstoqueLoja, Loja, Receita, SeruLojaMap, VendaMapa
    from app.utils import agora

    loja = Loja(nome='Anesio', ativa=True)
    db.session.add(loja)
    db.session.flush()
    db.session.add(SeruLojaMap(seru_company_name='Anesio', loja_id=loja.id,
                               confirmado_em=agora()))
    receita = Receita(nome='Pao Frances', categoria='Paes', rendimento_qtd=1,
                      rendimento_unidade='un', peso_base=100.0)
    db.session.add(receita)
    db.session.flush()
    db.session.add(VendaMapa(canal='seru', nome_externo='PAO FRANCES',
                             receita_id=receita.id, confirmado_em=agora(),
                             fator_quantidade=1.0))
    el = EstoqueLoja(loja_id=loja.id, receita_id=receita.id,
                     quantidade=qtd_estoque, estado=None)
    db.session.add(el)
    db.session.commit()
    return loja, receita, el


def _sync(pedidos, resp_nf=None):
    from app.services import seru_sync
    with patch('app.services.seru.listar_pedidos_completo',
               return_value=pedidos), \
         patch('app.services.seru.requests.get',
               return_value=resp_nf or _RespXML()):
        return seru_sync.processar_pedidos(PEDIDO_DIA, PEDIDO_DIA, user=None)


def test_itens_da_nf_extrai_produtos_do_xml(app):
    """Unit: XML da NFC-e (com namespace) → itens na forma do extrator."""
    from app.services import seru
    ped = _pedido_sem_itens('P1', 'Anesio')
    with patch('app.services.seru.requests.get', return_value=_RespXML()):
        itens = seru.itens_da_nf(ped)
    assert [i['nome'] for i in itens] == ['PAO FRANCES', 'Produto Sem Mapa']
    assert itens[0]['qtd'] == 6.0 and itens[0]['sku'] == '65'
    assert itens[0]['total'] == 24.0


def test_pedido_99food_baixa_estoque_pela_nf(app):
    """FIM A FIM: pedido sem itens + NF → baixa pelo motor de sempre.
    Item da NF sem mapa vira pendente (revisão em /pdv/mapeamentos)."""
    from app.models import EstoqueLoja, MovEstoqueLoja, SeruPedidoProcessado
    with app.app_context():
        loja, receita, el = _setup(qtd_estoque=10)
        eid = el.id
        stats = _sync([_pedido_sem_itens('P1', 'Anesio')])
        assert stats['itens_baixados'] == 1
        assert stats['itens_pendentes_novos'] == 1     # Produto Sem Mapa
        assert EstoqueLoja.query.get(eid).quantidade == 4   # 10 - 6
        mov = MovEstoqueLoja.query.filter_by(estoque_loja_id=eid,
                                             tipo='venda_seru').one()
        assert mov.quantidade == 6
        reg = SeruPedidoProcessado.query.get('P1')
        assert reg is not None and reg.n_itens_baixados == 1


def test_nf_download_falhou_retenta_sem_dobro(app):
    """S3 fora: pedido NÃO marca processado; ciclo seguinte baixa UMA vez."""
    from app.models import EstoqueLoja, SeruPedidoProcessado
    with app.app_context():
        loja, receita, el = _setup(qtd_estoque=10)
        eid = el.id
        stats1 = _sync([_pedido_sem_itens('P1', 'Anesio')],
                       resp_nf=_Resp500())
        assert stats1.get('pedidos_aguardando_nf') == 1
        assert SeruPedidoProcessado.query.get('P1') is None
        assert EstoqueLoja.query.get(eid).quantidade == 10
        # próximo ciclo, NF acessível: baixa normal (1x só)
        _sync([_pedido_sem_itens('P1', 'Anesio')])
        assert EstoqueLoja.query.get(eid).quantidade == 4
        _sync([_pedido_sem_itens('P1', 'Anesio')])     # idempotência
        assert EstoqueLoja.query.get(eid).quantidade == 4


def test_sem_nf_marca_processado_sem_baixa(app):
    """Cobrança sem itens E sem NF: nada de onde tirar produto — segue o
    fluxo antigo (processado com 0 itens, estoque intacto)."""
    from app.models import EstoqueLoja, SeruPedidoProcessado
    with app.app_context():
        loja, receita, el = _setup(qtd_estoque=10)
        eid = el.id
        _sync([_pedido_sem_itens('P2', 'Anesio', nf_url=None)])
        reg = SeruPedidoProcessado.query.get('P2')
        assert reg is not None and reg.n_itens_total == 0
        assert EstoqueLoja.query.get(eid).quantidade == 10


def test_nf_cancelada_nao_baixa(app):
    from app.models import EstoqueLoja, SeruPedidoProcessado
    with app.app_context():
        loja, receita, el = _setup(qtd_estoque=10)
        eid = el.id
        _sync([_pedido_sem_itens('P3', 'Anesio', nf_status='canceled')])
        reg = SeruPedidoProcessado.query.get('P3')
        assert reg is not None and reg.n_itens_total == 0
        assert EstoqueLoja.query.get(eid).quantidade == 10


def test_pedido_com_itens_nao_consulta_nf(app):
    """Pedido normal (itemizado) NUNCA baixa o XML — zero custo extra."""
    with app.app_context():
        _setup(qtd_estoque=10)
        ped = {'id': 'P4', 'createdAt': CREATED_AT, 'canceledAt': None,
               'company': {'name': 'Anesio'}, 'total': 12.0,
               'items': [{'name': 'PAO FRANCES', 'quantity': 3}]}
        from app.services import seru_sync
        with patch('app.services.seru.listar_pedidos_completo',
                   return_value=[ped]), \
             patch('app.services.seru.requests.get') as rget:
            seru_sync.processar_pedidos(PEDIDO_DIA, PEDIDO_DIA, user=None)
        assert not rget.called


def test_cancelado_por_status_novo_nao_baixa_pela_nf(app):
    """Achado de revisão: pedido novo com status='canceled' e canceledAt
    VAZIO + NF autorizada baixaria estoque de venda cancelada (e o estorno,
    keyed em canceledAt, nunca dispararia). Guard no ramo de pedido novo."""
    from app.models import EstoqueLoja, SeruPedidoProcessado
    with app.app_context():
        loja, receita, el = _setup(qtd_estoque=10)
        eid = el.id
        ped = _pedido_sem_itens('P5', 'Anesio')
        ped['status'] = 'canceled'                    # canceledAt segue None
        _sync([ped])
        reg = SeruPedidoProcessado.query.get('P5')
        assert reg is not None and reg.cancelado_em is not None
        assert EstoqueLoja.query.get(eid).quantidade == 10


def test_estorno_de_pedido_baixado_pela_nf(app):
    """Pedido baixado via NF que é cancelado depois (canceledAt): o estorno
    lê os movimentos gravados e devolve exato."""
    from app.models import EstoqueLoja, MovEstoqueLoja
    with app.app_context():
        loja, receita, el = _setup(qtd_estoque=10)
        eid = el.id
        _sync([_pedido_sem_itens('P6', 'Anesio')])
        assert EstoqueLoja.query.get(eid).quantidade == 4    # 10 - 6
        # mesmo pedido reaparece cancelado → estorna
        ped = _pedido_sem_itens('P6', 'Anesio')
        ped['canceledAt'] = '2026-05-20T15:00:00Z'
        _sync([ped])
        assert EstoqueLoja.query.get(eid).quantidade == 10
        est = MovEstoqueLoja.query.filter_by(
            estoque_loja_id=eid, tipo='venda_seru_estorno').all()
        assert len(est) == 1 and est[0].quantidade == 6


def test_qcom_zero_na_nf_nao_baixa(app):
    """qCom 0 (bonificação/ajuste) não vira baixa de 1 — mesmo contrato do
    extrair_itens (achado de revisão)."""
    from app.services import seru
    xml = XML_NF.replace('<qCom>6.0000</qCom>', '<qCom>0.0000</qCom>')
    ped = _pedido_sem_itens('P7', 'Anesio')
    with patch('app.services.seru.requests.get',
               return_value=_RespXML(xml)):
        itens = seru.itens_da_nf(ped)
    assert [i['nome'] for i in itens] == ['Produto Sem Mapa']
