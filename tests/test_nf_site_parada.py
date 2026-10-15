"""NF do site PARADA vira aviso ao dono (15/10/2026, caso ca37c72e: compra
com CNPJ de 28/09 esperando a conferência fiscal do dono havia 17 dias, sem
ninguém saber). A conferência continua sendo gesto do dono; o que muda é o
aviso: WhatsApp 1x/dia (8h-21h), item no "Precisa de você hoje" e sonda.
"""
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import Mock

import pytest

from app.extensions import db
from app.models import (
    AppConfig,
    Cliente,
    FiscalPedidoOnline,
    PedidoOnline,
    PedidoOnlineItem,
    Produto,
    TarefaFiscalPedido,
)
from app.services import loja_fiscal, tiny, zapi

BASE = datetime(2026, 10, 15, 10)
CNPJ = '48104494000180'
DECLARADO = ('Dados informados pelo cliente. O dono precisa conferir o cadastro fiscal '
             'e a inscrição estadual antes de autorizar a nota.')


@pytest.fixture(autouse=True)
def _sem_rede(app, monkeypatch):
    explode = Mock(side_effect=AssertionError('não deveria chamar o Tiny'))
    for nome in ('incluir_nota_fiscal', 'emitir_nota_fiscal', 'obter_nota_fiscal',
                 'buscar_contato_por_documento'):
        monkeypatch.setattr(tiny, nome, explode)
    monkeypatch.setattr(loja_fiscal, 'agora', lambda: BASE)
    # Base pública responde SEM a lista de inscrições: não sabe a IE, então
    # o cadastro declarado continua com o dono.
    from app.services import consulta_empresa
    monkeypatch.setattr(consulta_empresa, 'consultar', lambda _d: {
        'origem': 'cnpj_ws', 'ie_ativas_uf': None, 'atualizado_em': None,
        'dados': {'nome': 'SECURITY SYARD BRASIL LTDA', 'endereco': 'Av Berrini',
                  'numero': '1748', 'complemento': '', 'bairro': 'Cidade Moncoes',
                  'cidade': 'São Paulo', 'uf': 'SP', 'cep': '04571000', 'ie': '',
                  'situacao_ie': None}})
    app.config['ZAPI_NUMERO_DESTINO'] = '5511999990000'
    app.config['APP_BASE_URL'] = 'https://gestao.exemplo'


def _pedido_declarado(codigo='ca37c72e', *, status='pago', tentativas=0, erro=None):
    produto = Produto.query.first() or Produto(nome='Sweet Coffee', ativo=True,
                                               site_ativo=True, preco_site=250)
    db.session.add(produto)
    cli = Cliente(nome='Security', email=f'{codigo}@x.com', cpf=CNPJ)
    db.session.add(cli)
    db.session.flush()
    p = PedidoOnline(codigo=codigo, cliente_id=cli.id, nome_cliente='Security',
                     email_cliente=cli.email, telefone_cliente='11988887777',
                     modo_entrega='express', data_entrega=BASE.date() - timedelta(days=17),
                     janela_entrega='11:00–12:00', status=status,
                     pago_em=BASE - timedelta(days=17),
                     subtotal=Decimal('250'), frete_valor=Decimal('0'),
                     valor_total=Decimal('250'))
    p.itens.append(PedidoOnlineItem(kind='produto', produto_id=produto.id, nome=produto.nome,
                                    quantidade=1, preco_unitario=Decimal('250'),
                                    subtotal=Decimal('250')))
    db.session.add(p)
    db.session.flush()
    db.session.add(FiscalPedidoOnline(
        pedido_id=p.id, documento=CNPJ, origem='checkout_declarado',
        situacao_ie='desconhecida', erro=DECLARADO, criado_em=BASE,
        dados={'nome': 'SECURITY SYARD BRASIL LTDA', 'endereco': 'AVENIDA ENG LUIZ CARLOS BERRINI',
               'numero': '1748', 'complemento': '', 'bairro': 'Cidade Moncoes',
               'cidade': 'São Paulo', 'uf': 'SP', 'cep': '04571000', 'ie': ''}))
    db.session.add(TarefaFiscalPedido(pedido_id=p.id, tentativas=tentativas, erro=erro,
                                      proxima_tentativa_em=BASE - timedelta(minutes=1)))
    db.session.commit()
    return p


def test_fila_trava_no_cadastro_declarado_e_aparece_como_parada(app):
    p = _pedido_declarado()
    loja_fiscal.processar_pendentes(base=BASE)
    loja_fiscal.processar_pendentes(base=BASE + timedelta(hours=2))
    t = db.session.get(TarefaFiscalPedido, p.id)
    assert t.concluido_em is None and t.tentativas == 2 and 'conferir' in t.erro
    itens = loja_fiscal.travadas(BASE + timedelta(hours=2))
    assert [i['codigo'] for i in itens] == ['ca37c72e']
    assert itens[0]['aguarda_dono'] is True


def test_uma_tentativa_so_nao_conta_e_cancelado_fica_fora(app):
    _pedido_declarado('a1', tentativas=1, erro='x')
    _pedido_declarado('b2', tentativas=5, erro='x', status='cancelado')
    assert loja_fiscal.travadas(BASE) == []


def test_aviso_whatsapp_uma_vez_por_dia_com_link(app, monkeypatch):
    _pedido_declarado(tentativas=3, erro=DECLARADO)
    enviar = Mock(return_value={'ok': True})
    monkeypatch.setattr(zapi, 'enviar_texto', enviar)
    assert loja_fiscal.avisar_travadas(BASE)['enviado'] is True
    texto = enviar.call_args.args[1]
    assert 'ca37c72e' in texto and 'não resolveu o cadastro fiscal' in texto
    assert 'https://gestao.exemplo/admin/loja-online/pedidos/ca37c72e' in texto
    # Mesmo dia (outro worker, outro minuto): não repete.
    assert loja_fiscal.avisar_travadas(BASE + timedelta(hours=3))['enviado'] is False
    assert enviar.call_count == 1
    # Dia seguinte, ainda parada: cobra de novo.
    assert loja_fiscal.avisar_travadas(BASE + timedelta(days=1))['enviado'] is True
    assert enviar.call_count == 2


def test_aviso_respeita_horario_e_devolve_claim_se_falhar(app, monkeypatch):
    _pedido_declarado(tentativas=3, erro=DECLARADO)
    enviar = Mock(return_value={'ok': False})
    monkeypatch.setattr(zapi, 'enviar_texto', enviar)
    assert loja_fiscal.avisar_travadas(BASE.replace(hour=23))['motivo'] == 'fora_do_horario'
    assert enviar.call_count == 0
    assert loja_fiscal.avisar_travadas(BASE)['motivo'] == 'falha_envio'
    enviar.return_value = {'ok': True}
    assert loja_fiscal.avisar_travadas(BASE + timedelta(minutes=1))['enviado'] is True
    assert AppConfig.get('nf_site_travadas_aviso') == BASE.date().isoformat()


def test_conferencia_do_dono_tira_da_lista(app, owner_user):
    p = _pedido_declarado(tentativas=3, erro=DECLARADO)
    f = db.session.get(FiscalPedidoOnline, p.id)
    f.confirmado_em, f.origem, f.erro = BASE, 'owner', None
    db.session.commit()
    assert loja_fiscal.travadas(BASE)[0]['aguarda_dono'] is False


def test_pendencia_na_home(app):
    from app.services import briefing_dono
    _pedido_declarado(tentativas=3, erro=DECLARADO)
    item = next(i for i in briefing_dono.pendencias() if i['chave'] == 'nf_site_parada')
    assert item['qtd'] == 1 and item['url'] == '/admin/loja-online/pedidos/ca37c72e'


def test_sonda_nf_site_fila(app):
    app.config['CLAUDE_API_TOKEN'] = 'tok-teste'
    _pedido_declarado(tentativas=3, erro=DECLARADO)
    c = app.test_client()
    h = {'Authorization': 'Bearer tok-teste'}
    d = c.get('/api/claude/nf-site-fila', headers=h).get_json()
    assert d['n'] == 1 and d['aguardando_dono'] == 1
    assert d['itens'][0]['fiscal']['origem'] == 'checkout_declarado'
    d = c.get('/api/claude/nf-site-fila?codigo=ca37', headers=h).get_json()
    assert d['pedido']['tarefa']['tentativas'] == 3
    assert d['pedido']['endereco_nf']['uf'] is None or isinstance(d['pedido']['endereco_nf'], dict)


def test_aviso_de_email_recusado_diz_o_que_fazer(app, monkeypatch):
    p = _pedido_declarado(tentativas=3,
                          erro='NF emitida; envio do DANFE por e-mail pendente: Postmark recusou '
                               '(422/406): ... Found inactive addresses: x@gmail.cm.')
    f = db.session.get(FiscalPedidoOnline, p.id)
    f.confirmado_em, f.origem, f.erro = BASE, 'owner', None
    p.nf_emitida_em, p.tiny_nota_fiscal_id = BASE, 'nf-1'
    db.session.commit()
    enviar = Mock(return_value={'ok': True})
    monkeypatch.setattr(zapi, 'enviar_texto', enviar)
    loja_fiscal.avisar_travadas(BASE)
    assert 'corrija o e-mail no pedido' in enviar.call_args.args[1]


# ── E-mail recusado de vez não é repetido (15/10/2026) ──────────────────────

def _nf_emitida_com_email(monkeypatch, resposta):
    from app.services import email as email_svc
    p = _pedido_declarado('e406', tentativas=1, erro='x')
    f = db.session.get(FiscalPedidoOnline, p.id)
    f.confirmado_em, f.origem, f.erro = BASE, 'owner', None
    p.nf_emitida_em, p.tiny_nota_fiscal_id = BASE, 'nf-1'
    db.session.commit()
    enviar = Mock(return_value=resposta)
    monkeypatch.setattr(email_svc, 'enviar_nf_emitida', enviar)
    monkeypatch.setattr(email_svc, 'disponivel', lambda: True)
    return p, enviar


def test_destinatario_inativo_nao_repete_ate_corrigir_o_email(app, monkeypatch):
    p, enviar = _nf_emitida_com_email(monkeypatch, {
        'ok': False, 'codigo': 406,
        'erro': 'Postmark recusou (422/406): Found inactive addresses: x@gmail.cm.'})
    loja_fiscal.processar_pendentes(base=BASE)
    t = db.session.get(TarefaFiscalPedido, p.id)
    assert enviar.call_count == 1 and t.concluido_em is None
    assert t.proxima_tentativa_em >= BASE + timedelta(days=29)
    # Ciclos seguintes (horas depois) não chamam o e-mail de novo.
    loja_fiscal.processar_pendentes(base=BASE + timedelta(hours=5))
    assert enviar.call_count == 1
    # Continua na lista de NF parada para o dono corrigir.
    assert [i['codigo'] for i in loja_fiscal.travadas(BASE)] == ['e406']
    # Mesmo e-mail regravado: nada muda.
    assert loja_fiscal.reenviar_apos_email_corrigido(p, p.email_cliente) is False
    # E-mail corrigido: volta para o ciclo seguinte e sai.
    anterior = p.email_cliente
    p.email_cliente = 'certo@gmail.com'
    assert loja_fiscal.reenviar_apos_email_corrigido(p, anterior, base=BASE + timedelta(hours=6))
    db.session.commit()
    enviar.return_value = {'ok': True}
    loja_fiscal.processar_pendentes(base=BASE + timedelta(hours=6, minutes=1))
    t = db.session.get(TarefaFiscalPedido, p.id)
    assert enviar.call_count == 2 and t.concluido_em is not None and t.email_enviado_em


def test_falha_passageira_do_email_segue_repetindo(app, monkeypatch):
    p, enviar = _nf_emitida_com_email(monkeypatch, {'ok': False, 'erro': 'timeout', 'incerto': True})
    loja_fiscal.processar_pendentes(base=BASE)
    t = db.session.get(TarefaFiscalPedido, p.id)
    assert t.proxima_tentativa_em <= BASE + timedelta(hours=1)


def test_editar_pedido_com_email_novo_retoma_o_envio(app, owner_user, monkeypatch):
    p, _ = _nf_emitida_com_email(monkeypatch, {'ok': False, 'codigo': 406, 'erro': 'inactive'})
    loja_fiscal.processar_pendentes(base=BASE)
    assert db.session.get(TarefaFiscalPedido, p.id).proxima_tentativa_em > BASE + timedelta(days=1)
    app.config['WTF_CSRF_ENABLED'] = False
    c = app.test_client()
    with c.session_transaction() as sess:
        sess['_user_id'] = str(owner_user.id)
        sess['_fresh'] = True
    r = c.post(f'/admin/loja-online/pedidos/{p.codigo}/editar', data={
        'nome_cliente': p.nome_cliente, 'email_cliente': 'certo@gmail.com',
        'modo_entrega': p.modo_entrega, 'data_entrega': p.data_entrega.isoformat(),
        'janela_entrega': p.janela_entrega})
    assert r.status_code in (302, 303)
    db.session.expire_all()
    t = db.session.get(TarefaFiscalPedido, p.id)
    assert db.session.get(PedidoOnline, p.id).email_cliente == 'certo@gmail.com'
    assert t.proxima_tentativa_em <= BASE + timedelta(minutes=1)


def test_email_enviar_devolve_codigo_do_postmark(app, monkeypatch):
    from app.services import email as email_svc
    resposta = Mock(status_code=422, text='x')
    resposta.json.return_value = {'ErrorCode': 406, 'Message': 'inactive'}
    monkeypatch.setattr(email_svc.requests, 'post', Mock(return_value=resposta))
    app.config['POSTMARK_SERVER_TOKEN'] = 'tok'
    app.config['MAIL_FROM'] = 'pedidos@opao.online'
    r = email_svc.enviar('a@b.com', 'assunto', '<p>x</p>')
    assert r['ok'] is False and r['codigo'] == 406
