"""Pedido ASSISTIDO (01/10/2026): a equipe monta o pedido do site em nome do
cliente e manda o link em que ele paga (Pix/cartão).

Decisão do dono: "preciso criar um 'criar pedido' no sistema que o operador
faz o pedido para o cliente e envia para ele a parte que ele insere os
dados para pagamento ou pix".

Cobre:
- motor = `loja_checkout.criar_pedido`: pedido nasce aguardando_pagamento,
  reserva estoque, cliente criado com origem 'balcao', cadastro existente
  NÃO alterado, aceite LGPD NÃO carimbado;
- prazo do link vira `reserva_expira_em` (cron de expiração = única fonte);
- regra do prazo (24 h, véspera, piso 2 h);
- e-mail próprio com o link + texto/URL de WhatsApp (só celular BR);
- rota admin: gate (gerente/admin sim; funcionário/produção não), POST cria
  e redireciona, erro re-renderiza com os valores, cotação JSON;
- detalhe/lista: bloco com link e selo; reenviar e-mail manda o do link;
- cancelar (gerente) só aguardando + assistido;
- página pública de pagamento lista itens + termos; cron não cancela antes
  do prazo e cancela depois.
"""
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.extensions import db

FRETE_OK = {'ok': True, 'valor': 15.0, 'gratis': False, 'fora_area': False,
            'distancia_km': 3.4, 'endereco': 'Rua X, Moema', 'aviso': ''}

_END = {'cep': '04077-000', 'logradouro': 'Rua X', 'numero': '10',
        'bairro': 'Moema', 'cidade': 'São Paulo', 'uf': 'SP'}


@pytest.fixture
def cliente(app):
    return app.test_client()


def _login(cliente, user):
    with cliente.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def _usuario(papel, login):
    from app.models import Usuario
    u = Usuario(nome=login, login=login, papel=papel)
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    return u


def _loja_site(nome='Loja Anesio Pinto Rosa'):
    from app.models import AppConfig, Loja
    lo = Loja(nome=nome, ativa=True, endereco='Rua A, 1')
    db.session.add(lo)
    db.session.commit()
    AppConfig.set('loja_site_estoque_id', lo.id)
    db.session.commit()
    return lo


def _produto(nome='Box Mimo', preco=20.0):
    from app.models import Produto
    p = Produto(nome=nome, categoria='Cestas', preco_site=preco,
                imagem_dropbox_url='https://x/p.jpg', ativo=True)
    db.session.add(p)
    db.session.commit()
    return p


def _receita_sourdough(nome='Sourdough Teste', preco=30.0):
    from app.models import Receita
    r = Receita(nome=nome, categoria='Pães', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0, preco_site=preco,
                familia='pao_sourdough')
    db.session.add(r)
    db.session.commit()
    return r


def _estoque(loja, produto=None, receita=None, qtd=50):
    from app.models import EstoqueLoja
    el = EstoqueLoja(loja_id=loja.id, quantidade=qtd,
                     produto_id=produto.id if produto else None,
                     receita_id=receita.id if receita else None)
    db.session.add(el)
    db.session.commit()
    return el


def _base():
    # Terça 10:00 (dia útil, dentro do horário de entrega).
    return datetime(2026, 10, 6, 10, 0)


def _form_retirada(loja, **over):
    from app.services import loja_checkout
    data = loja_checkout.datas_disponiveis('retirada', base=_base())[1].isoformat()
    f = {'nome': 'Maria', 'sobrenome': 'Silva', 'email': 'maria@x.com',
         'telefone': '11999998888', 'cpf': '52998224725',
         'modo_entrega': 'retirada', 'loja_id': str(loja.id),
         'data_entrega': data, 'janela_entrega': '09:00–10:00',
         'cliente_informado': '1', **_END}
    f.update(over)
    return f


def _form_agendada(**over):
    from app.services import loja_checkout
    data = loja_checkout.datas_disponiveis('agendada', base=_base())[1].isoformat()
    f = {'nome': 'Maria', 'sobrenome': 'Silva', 'email': 'maria@x.com',
         'telefone': '11999998888', 'cpf': '52998224725',
         'modo_entrega': 'agendada',
         'data_entrega': data, 'janela_entrega': '12:00–13:00',
         'cliente_informado': '1', **_END}
    f.update(over)
    return f


# ── Regra do prazo ──────────────────────────────────────────────────────────

def test_prazo_24h_quando_a_entrega_e_longe():
    from app.services import pedido_assistido as svc
    base = datetime(2026, 10, 6, 10, 0)
    assert svc.prazo_pagamento(base, base.date() + timedelta(days=5)) == \
        base + timedelta(hours=24)


def test_prazo_corta_na_vespera_da_entrega():
    from app.services import pedido_assistido as svc
    base = datetime(2026, 10, 6, 20, 0)
    amanha = base.date() + timedelta(days=1)
    assert svc.prazo_pagamento(base, amanha) == datetime(2026, 10, 6, 23, 59, 59)


def test_prazo_tem_piso_de_2h_para_entrega_hoje():
    from app.services import pedido_assistido as svc
    base = datetime(2026, 10, 6, 10, 0)
    assert svc.prazo_pagamento(base, base.date()) == base + timedelta(hours=2)
    # lançado às 22:30 pra amanhã: a véspera já quase acabou, vale o piso
    tarde = datetime(2026, 10, 6, 22, 30)
    assert svc.prazo_pagamento(tarde, tarde.date() + timedelta(days=1)) == \
        tarde + timedelta(hours=2)


def test_prazo_express_e_curto():
    """Express 'sai agora': a janela é calculada na criação — link pago horas
    depois seria um express vencido. 30 min fixos."""
    from app.services import pedido_assistido as svc
    base = datetime(2026, 10, 6, 10, 0)
    assert svc.prazo_pagamento(base, base.date(), modo_entrega='express') == \
        base + timedelta(minutes=svc.PRAZO_EXPRESS_MIN)


# ── Serviço ─────────────────────────────────────────────────────────────────

def test_criar_retirada_nasce_aguardando_com_registro_e_prazo(app, admin_user):
    from app.models import Cliente, PedidoOnlineAssistido
    from app.services import pedido_assistido as svc
    loja = _loja_site()
    prod = _produto()
    _estoque(loja, produto=prod)
    with patch('app.services.email.disponivel', return_value=True), \
         patch('app.services.email.enviar',
               return_value={'ok': True, 'id': 'x'}) as ev:
        pedido, erros, reg = svc.criar(
            _form_retirada(loja), [{'kind': 'produto', 'id': prod.id, 'qtd': 2}],
            operador_id=admin_user.id, canal='whatsapp',
            observacao='pediu pelo zap', base=_base())
    assert erros == []
    assert pedido.status == 'aguardando_pagamento'
    assert pedido.pago_em is None
    assert pedido.valor_total == Decimal('40.00')
    assert pedido.cliente_id is not None
    # Marcador: operador, canal, prazo = reserva_expira_em (fonte única)
    assert isinstance(reg, PedidoOnlineAssistido)
    assert reg.operador_id == admin_user.id and reg.canal == 'whatsapp'
    assert reg.observacao == 'pediu pelo zap'
    assert pedido.reserva_expira_em == reg.prazo_pagamento
    assert reg.prazo_pagamento == svc.prazo_pagamento(_base(), pedido.data_entrega)
    assert reg.prazo_pagamento > _base() + timedelta(minutes=35)
    # E-mail do LINK saiu (não o "recebemos seu pedido") e ficou carimbado
    assert ev.call_count == 1
    destinatario, assunto, html = ev.call_args[0][:3]
    assert destinatario == 'maria@x.com'
    assert 'pronto para pagamento' in assunto
    assert f'/loja/pedido/{pedido.codigo}/pagamento' in html
    assert 'equipe montou' in html
    assert 'Recebemos seu pedido' not in html
    assert reg.email_enviado_em is not None
    # Cliente novo nasce 'balcao' e SEM aceite carimbado (quem marcou foi o
    # operador)
    cli = Cliente.query.filter_by(email='maria@x.com').first()
    assert cli.origem == 'balcao'
    assert cli.aceite_lgpd_em is None
    assert svc.registro_de(pedido) is reg
    assert pedido.assistido is reg


def test_criar_agendada_cota_frete_e_reserva_estoque(app, admin_user):
    from app.models import EstoqueLoja
    from app.services import pedido_assistido as svc
    loja = _loja_site()
    prod = _produto()
    el = _estoque(loja, produto=prod)
    with patch('app.services.frete.consultar_frete', return_value=FRETE_OK), \
         patch('app.services.email.disponivel', return_value=False):
        pedido, erros, reg = svc.criar(
            _form_agendada(), [{'kind': 'produto', 'id': prod.id, 'qtd': 1}],
            operador_id=admin_user.id, base=_base())
    assert erros == []
    assert pedido.frete_valor == Decimal('15.00')
    assert pedido.valor_total == Decimal('35.00')
    assert pedido.distancia_km == 3.4
    db.session.refresh(el)
    assert EstoqueLoja.query.get(el.id).quantidade_reservada == 1
    assert reg.email_enviado_em is None          # e-mail desligado = sem carimbo


def test_criar_nao_altera_cadastro_existente(app, admin_user):
    from app.models import Cliente
    from app.services import pedido_assistido as svc
    loja = _loja_site()
    prod = _produto()
    cli = Cliente(nome='Maria Antiga', email='maria@x.com',
                  telefone='11911111111', cpf='52998224725', origem='site')
    db.session.add(cli)
    db.session.commit()
    with patch('app.services.email.disponivel', return_value=False):
        pedido, erros, _ = svc.criar(
            _form_retirada(loja, nome='Maria', sobrenome='Nova',
                           telefone='11922222222'),
            [{'kind': 'produto', 'id': prod.id, 'qtd': 1}],
            operador_id=admin_user.id, base=_base())
    assert erros == []
    db.session.refresh(cli)
    assert cli.nome == 'Maria Antiga' and cli.telefone == '11911111111'
    assert cli.aceite_lgpd_em is None
    assert pedido.cliente_id == cli.id
    assert pedido.nome_cliente == 'Maria Nova'        # snapshot do pedido


def test_criar_exige_confirmacao_do_operador_e_nao_chama_o_motor(app, admin_user):
    """Erro do OPERADOR curto-circuita ANTES do motor: sem isso o checkout
    cotava frete (Google) e disparava alertas pra depois recusar."""
    from app.models import Cliente, PedidoOnline, PedidoOnlineAssistido
    from app.services import pedido_assistido as svc
    loja = _loja_site()
    prod = _produto()
    with patch('app.services.loja_checkout.criar_pedido') as motor:
        pedido, erros, reg = svc.criar(
            _form_retirada(loja, cliente_informado=''),
            [{'kind': 'produto', 'id': prod.id, 'qtd': 1}],
            operador_id=admin_user.id, base=_base())
    motor.assert_not_called()
    assert pedido is None and reg is None
    assert svc.MSG_CLIENTE_NAO_INFORMADO in erros
    assert not any('aceitar os termos' in e for e in erros)
    assert PedidoOnline.query.count() == 0
    assert PedidoOnlineAssistido.query.count() == 0
    assert Cliente.query.count() == 0


def test_criar_nao_alerta_o_dono_como_cliente_barrado(app, admin_user):
    """Quem digita é a equipe: endereço não localizado NÃO manda o WhatsApp
    'cliente prestes a comprar foi barrado'; o sensor registra com origem
    própria ('assistido'). O checkout do site segue alertando."""
    from app.services import loja_checkout
    from app.services import pedido_assistido as svc
    _loja_site()
    prod = _produto()
    nao_achou = {'ok': False, 'erro': 'nao_encontrado'}
    with patch('app.services.frete.consultar_frete', return_value=nao_achou), \
         patch('app.services.loja_alerta.alertar_endereco_falho') as alerta, \
         patch('app.services.frete_sensor.registrar') as sensor:
        pedido, erros, _ = svc.criar(
            _form_agendada(), [{'kind': 'produto', 'id': prod.id, 'qtd': 1}],
            operador_id=admin_user.id, base=_base())
    assert pedido is None and erros
    alerta.assert_not_called()
    assert sensor.call_args[0][0] == 'assistido'
    # Site (default): alerta sai e o sensor registra 'checkout'
    form = _form_agendada()
    form['aceite_lgpd'] = '1'
    with patch('app.services.frete.consultar_frete', return_value=nao_achou), \
         patch('app.services.loja_alerta.alertar_endereco_falho') as alerta2, \
         patch('app.services.frete_sensor.registrar') as sensor2:
        loja_checkout.criar_pedido(
            form, [{'kind': 'produto', 'id': prod.id, 'qtd': 1}], base=_base())
    alerta2.assert_called_once()
    assert sensor2.call_args[0][0] == 'checkout'


def test_criar_recusa_duplo_clique(app, admin_user):
    from app.models import PedidoOnline
    from app.services import pedido_assistido as svc
    from app.utils import agora
    loja = _loja_site()
    prod = _produto()
    base = agora()
    from app.services import loja_checkout
    data = loja_checkout.datas_disponiveis('retirada', base=base)[1].isoformat()
    form = _form_retirada(loja, data_entrega=data, janela_entrega='12:00–13:00')
    with patch('app.services.email.disponivel', return_value=False):
        p1, erros, _ = svc.criar(form, [{'kind': 'produto', 'id': prod.id, 'qtd': 1}],
                                 operador_id=admin_user.id, base=base)
        assert erros == []
        p2, erros2, _ = svc.criar(form, [{'kind': 'produto', 'id': prod.id, 'qtd': 1}],
                                  operador_id=admin_user.id, base=base)
    assert p2 is None
    assert any(p1.codigo in e for e in erros2)
    assert PedidoOnline.query.count() == 1
    # Outro operador (ou outro cliente) não é duplicata
    outro = _usuario('gerente', 'ger2')
    with patch('app.services.email.disponivel', return_value=False):
        p3, erros3, _ = svc.criar(form, [{'kind': 'produto', 'id': prod.id, 'qtd': 1}],
                                  operador_id=outro.id, base=base)
    assert erros3 == [] and p3 is not None


def test_criar_devolve_erros_do_motor_sem_gravar(app, admin_user):
    from app.models import PedidoOnline
    from app.services import pedido_assistido as svc
    loja = _loja_site()
    prod = _produto()
    pedido, erros, _ = svc.criar(
        _form_retirada(loja, cpf='123', email='invalido'),
        [{'kind': 'produto', 'id': prod.id, 'qtd': 1}],
        operador_id=admin_user.id, base=_base())
    assert pedido is None
    assert any('CPF' in e for e in erros)
    assert any('email' in e.lower() for e in erros)
    assert PedidoOnline.query.count() == 0


def test_criar_recusa_item_fora_do_site(app, admin_user):
    from app.services import pedido_assistido as svc
    loja = _loja_site()
    prod = _produto()
    prod.site_ativo = False
    db.session.commit()
    pedido, erros, _ = svc.criar(
        _form_retirada(loja), [{'kind': 'produto', 'id': prod.id, 'qtd': 1}],
        operador_id=admin_user.id, base=_base())
    assert pedido is None
    assert any('catálogo' in e or 'vazio' in e for e in erros)


def test_criar_sourdough_fatiado_e_tetos_do_carrinho(app, admin_user):
    from app.services import pedido_assistido as svc
    loja = _loja_site()
    r = _receita_sourdough()
    _estoque(loja, receita=r)
    with patch('app.services.email.disponivel', return_value=False):
        pedido, erros, _ = svc.criar(
            _form_retirada(loja),
            [{'kind': 'receita', 'id': r.id, 'qtd': 500, 'fatiado': True}],
            operador_id=admin_user.id, base=_base())
    assert erros == []
    it = pedido.itens[0]
    assert it.fatiado is True
    assert it.quantidade == 99          # mesmo teto do carrinho do site


# ── WhatsApp / link ─────────────────────────────────────────────────────────

def test_texto_e_url_whatsapp(app, admin_user):
    from app.services import pedido_assistido as svc
    loja = _loja_site()
    prod = _produto()
    with patch('app.services.email.disponivel', return_value=False):
        pedido, erros, reg = svc.criar(
            _form_retirada(loja), [{'kind': 'produto', 'id': prod.id, 'qtd': 2}],
            operador_id=admin_user.id, base=_base())
    assert erros == []
    link = svc.link_pagamento(pedido)
    assert link.endswith(f'/loja/pedido/{pedido.codigo}/pagamento')
    assert link.startswith(app.config['LOJA_BASE_URL'].rstrip('/'))
    txt = svc.texto_whatsapp(pedido, reg)
    assert 'Olá, Maria!' in txt
    assert '2x Box Mimo' in txt
    assert 'R$ 40,00' in txt
    assert link in txt
    assert reg.prazo_pagamento.strftime('%d/%m') in txt
    url = svc.url_whatsapp(pedido, txt)
    assert url.startswith('https://wa.me/5511999998888?text=')
    # Fixo / internacional: sem wa.me (não recebe WhatsApp)
    pedido.telefone_cliente = '1130000000'
    assert svc.url_whatsapp(pedido) is None
    pedido.telefone_cliente = '+14752929850'
    assert svc.url_whatsapp(pedido) is None


# ── Rotas admin ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize('papel,esperado', [
    ('gerente', 200), ('admin', 200), ('funcionario', 403), ('producao', 403),
    ('marketing', 403), ('observador', 403),
])
def test_gate_da_tela(app, cliente, papel, esperado):
    _loja_site()
    u = _usuario(papel, 'u_' + papel)
    _login(cliente, u)
    # Papéis de consulta fixa (observador) são desviados pelo gate global
    # (302 pras telas deles) antes do decorator — barrado de qualquer jeito.
    aceitos = {esperado} if esperado == 200 else {403, 302}
    assert cliente.get('/admin/loja-online/pedido-assistido').status_code in aceitos
    assert cliente.get('/admin/loja-online/pedido-assistido/cotacao'
                       '?modo=retirada').status_code in aceitos
    assert u.pode_pedido_assistido() is (esperado == 200)


def test_dono_libera_atendente_pela_matriz_de_permissoes(app, cliente):
    """Capacidade PRÓPRIA `web_pedido_assistido`: 'funcionario' entra só com
    a liberação em /admin/permissoes — sem abrir estoque/relatório/preços
    (web_estoque_loja segue fechada)."""
    from app.models import PermissaoPapel
    _loja_site()
    u = _usuario('funcionario', 'atendente')
    _login(cliente, u)
    assert cliente.get('/admin/loja-online/pedido-assistido').status_code == 403
    db.session.add(PermissaoPapel(papel='funcionario',
                                  capacidade='web_pedido_assistido', permitido=True))
    db.session.commit()
    assert cliente.get('/admin/loja-online/pedido-assistido').status_code == 200
    assert cliente.get('/admin/loja-online/pedidos').status_code == 403
    assert u.pode_pedido_assistido() is True and u.pode_lojas() is False


def test_atendente_liberado_cria_e_ve_o_link_sem_abrir_o_detalhe(app, cliente):
    """Depois de criar, a tela volta com `?criado=` e o bloco do link — o
    atendente não precisa do detalhe do pedido (gerente_required)."""
    from app.models import PedidoOnline, PermissaoPapel
    loja = _loja_site()
    prod = _produto()
    _estoque(loja, produto=prod)
    u = _usuario('funcionario', 'atendente2')
    db.session.add(PermissaoPapel(papel='funcionario',
                                  capacidade='web_pedido_assistido', permitido=True))
    db.session.commit()
    _login(cliente, u)
    from app.services import loja_checkout
    from app.utils import agora
    data = loja_checkout.datas_disponiveis('retirada', base=agora())[1].isoformat()
    form = _form_retirada(loja, data_entrega=data, janela_entrega='12:00–13:00')
    form.update({'item_alvo[]': f'produto:{prod.id}', 'item_qtd[]': '1'})
    with patch('app.services.email.disponivel', return_value=False):
        r = cliente.post('/admin/loja-online/pedido-assistido', data=form,
                         follow_redirects=True)
    assert r.status_code == 200
    p = PedidoOnline.query.one()
    html = r.get_data(as_text=True)
    assert 'criado pela equipe' in html
    assert f'/loja/pedido/{p.codigo}/pagamento' in html
    assert 'Abrir o pedido' not in html           # sem permissão no detalhe
    # Desfazer e reenviar pelo bloco também valem pro atendente
    r = cliente.post(f'/admin/loja-online/pedidos/{p.codigo}/pedido-assistido/cancelar')
    assert r.status_code == 302
    assert r.headers['Location'].endswith(f'/admin/loja-online/pedido-assistido?criado={p.codigo}')
    db.session.refresh(p)
    assert p.status == 'cancelado'


def test_get_renderiza_catalogo_publicado(app, cliente, admin_user):
    _loja_site()
    prod = _produto('Cesta Publicada')
    fora = _produto('Cesta Pausada')
    fora.site_ativo = False
    db.session.commit()
    _login(cliente, admin_user)
    html = cliente.get('/admin/loja-online/pedido-assistido').get_data(as_text=True)
    # O catálogo embutido (JSON do typeahead) só traz o PUBLICADO — o nome
    # do pausado ainda aparece no <datalist> da busca global do layout.
    assert f'"nome": "{prod.nome}"' in html
    assert '"nome": "Cesta Pausada"' not in html
    assert 'tpl-item' in html and 'cliente_informado' in html


def test_post_cria_e_redireciona_para_o_detalhe(app, cliente, admin_user):
    from app.models import PedidoOnline, PedidoOnlineAssistido
    loja = _loja_site()
    prod = _produto()
    _estoque(loja, produto=prod)
    _login(cliente, admin_user)
    from app.services import loja_checkout
    from app.utils import agora
    data = loja_checkout.datas_disponiveis('retirada', base=agora())[1].isoformat()
    form = _form_retirada(loja, data_entrega=data, janela_entrega='12:00–13:00',
                          canal='telefone')
    form.update({'item_alvo[]': f'produto:{prod.id}', 'item_qtd[]': '3',
                 'item_comp[]': '', 'item_fatiado[]': ''})
    with patch('app.services.email.disponivel', return_value=False):
        r = cliente.post('/admin/loja-online/pedido-assistido', data=form)
    assert r.status_code == 302, r.get_data(as_text=True)[:800]
    p = PedidoOnline.query.one()
    assert r.headers['Location'].endswith(
        f'/admin/loja-online/pedido-assistido?criado={p.codigo}')
    assert p.itens[0].quantidade == 3
    reg = PedidoOnlineAssistido.query.one()
    assert reg.operador_id == admin_user.id and reg.canal == 'telefone'
    # A tela volta com o bloco do link (+ atalho pro detalhe, que o admin abre)
    html = cliente.get(r.headers['Location']).get_data(as_text=True)
    assert 'criado pela equipe' in html
    assert f'/loja/pedido/{p.codigo}/pagamento' in html
    assert 'wa.me/5511999998888' in html
    assert 'Desfazer pedido' in html
    assert f'Abrir o pedido {p.codigo}' in html
    # Detalhe mostra o mesmo bloco + selo na lista
    det = cliente.get(f'/admin/loja-online/pedidos/{p.codigo}').get_data(as_text=True)
    assert 'criado pela equipe' in det and 'wa.me/5511999998888' in det
    assert 'onsubmit="return confirm(\'' not in det     # dado externo nunca inline
    lista = cliente.get('/admin/loja-online/pedidos?status=aguardando_pagamento'
                        ).get_data(as_text=True)
    assert '🧾 equipe' in lista


def test_post_com_erro_re_renderiza_com_valores(app, cliente, admin_user):
    from app.models import PedidoOnline
    loja = _loja_site()
    prod = _produto()
    _login(cliente, admin_user)
    form = _form_retirada(loja, cpf='000', nome='Joaquina')
    form.update({'item_alvo[]': f'produto:{prod.id}', 'item_qtd[]': '2'})
    r = cliente.post('/admin/loja-online/pedido-assistido', data=form)
    assert r.status_code == 400
    html = r.get_data(as_text=True)
    assert 'CPF' in html
    assert 'value="Joaquina"' in html          # valores preservados
    assert f'"id": {prod.id}' in html or f'"id":{prod.id}' in html  # itens iniciais
    assert PedidoOnline.query.count() == 0


def test_cotacao_devolve_janelas_e_frete(app, cliente, admin_user):
    _loja_site()
    _login(cliente, admin_user)
    from app.utils import hoje
    d = (hoje() + timedelta(days=3)).isoformat()
    rj = cliente.get('/admin/loja-online/pedido-assistido/cotacao'
                     '?modo=retirada&data=' + d).get_json()
    assert rj['ok'] and len(rj['janelas']) >= 1 and rj['valor'] is None
    with patch('app.services.frete.consultar_frete',
               return_value={'ok': True, 'distancia_km': 20.0,
                             'fora_area': False, 'valor': 35.0}) as cf:
        aj = cliente.get('/admin/loja-online/pedido-assistido/cotacao'
                         '?modo=agendada&data=' + d +
                         '&logradouro=Rua+X&numero=1&bairro=Moema&cidade=SP'
                         '&uf=SP&cep=04077-000&complemento=apto+3').get_json()
    assert aj['ok'] and '08:00–09:00' not in aj['janelas']
    assert aj['valor'] == 35.0 and aj['distancia_km'] == 20.0
    from app.services import frete, loja_checkout
    assert cf.call_args.kwargs.get('canal') == frete.CANAL_PREVIEW
    # MESMA string de geocode do criar_pedido (sem complemento, com UF/CEP)
    assert cf.call_args[0][0] == loja_checkout.texto_de_geocode({
        'logradouro': 'Rua X', 'numero': '1', 'bairro': 'Moema',
        'cidade': 'SP', 'uf': 'SP', 'cep': '04077-000', 'complemento': 'apto 3'})[0]
    assert 'apto' not in cf.call_args[0][0]


def test_reenviar_emails_manda_o_do_link_no_assistido(app, cliente, admin_user):
    from app.services import pedido_assistido as svc
    loja = _loja_site()
    prod = _produto()
    with patch('app.services.email.disponivel', return_value=False):
        pedido, erros, reg = svc.criar(
            _form_retirada(loja), [{'kind': 'produto', 'id': prod.id, 'qtd': 1}],
            operador_id=admin_user.id, base=_base())
    assert erros == []
    _login(cliente, admin_user)
    with patch('app.services.email.disponivel', return_value=True), \
         patch('app.services.email.enviar',
               return_value={'ok': True, 'id': 'x'}) as ev, \
         patch('app.services.email.enviar_pedido_recebido') as recebido:
        r = cliente.post(f'/admin/loja-online/pedidos/{pedido.codigo}/reenviar-emails',
                         follow_redirects=True)
    assert r.status_code == 200
    recebido.assert_not_called()
    assert ev.call_count == 1
    assert 'pronto para pagamento' in ev.call_args[0][1]
    db.session.refresh(reg)
    assert reg.email_enviado_em is not None
    # Botão próprio do bloco também reenvia
    with patch('app.services.email.disponivel', return_value=True), \
         patch('app.services.email.enviar',
               return_value={'ok': True, 'id': 'y'}) as ev2:
        r = cliente.post(f'/admin/loja-online/pedidos/{pedido.codigo}/'
                         'pedido-assistido/reenviar-email', follow_redirects=True)
    assert r.status_code == 200 and ev2.call_count == 1


def test_cancelar_pelo_gerente_so_assistido_aguardando(app, cliente, admin_user):
    from app.models import EstoqueLoja, PedidoOnline
    from app.services import pedido_assistido as svc
    loja = _loja_site()
    prod = _produto()
    el = _estoque(loja, produto=prod)
    with patch('app.services.email.disponivel', return_value=False):
        pedido, erros, _ = svc.criar(
            _form_retirada(loja), [{'kind': 'produto', 'id': prod.id, 'qtd': 2}],
            operador_id=admin_user.id, base=_base())
    assert erros == []
    assert EstoqueLoja.query.get(el.id).quantidade_reservada == 2
    gerente = _usuario('gerente', 'ger')
    _login(cliente, gerente)
    r = cliente.post(f'/admin/loja-online/pedidos/{pedido.codigo}/'
                     'pedido-assistido/cancelar', follow_redirects=True)
    assert r.status_code == 200
    db.session.refresh(pedido)
    assert pedido.status == 'cancelado'
    assert pedido.motivo_cancelamento == 'cancelado_admin'
    assert EstoqueLoja.query.get(el.id).quantidade_reservada == 0
    # Segunda vez: já não está aguardando → recusa sem mexer
    ok, msg = svc.cancelar(pedido)
    assert ok is False and 'não está aguardando' in msg
    # Pedido COMUM do site (sem registro) não entra por aqui
    comum = PedidoOnline(nome_cliente='X', email_cliente='x@x.com',
                         modo_entrega='retirada', loja_retirada_id=loja.id,
                         subtotal=Decimal('0'), valor_total=Decimal('0'))
    db.session.add(comum)
    db.session.commit()
    ok, msg = svc.cancelar(comum)
    assert ok is False and 'não é um pedido assistido' in msg
    db.session.refresh(comum)
    assert comum.status == 'aguardando_pagamento'


# ── Cron de expiração: respeita o prazo do link ─────────────────────────────

def test_cron_nao_cancela_antes_do_prazo_e_cancela_depois(app, admin_user):
    from app.services import loja_estoque_reserva
    from app.services import pedido_assistido as svc
    loja = _loja_site()
    prod = _produto()
    _estoque(loja, produto=prod)
    with patch('app.services.email.disponivel', return_value=False):
        pedido, erros, reg = svc.criar(
            _form_retirada(loja), [{'kind': 'produto', 'id': prod.id, 'qtd': 1}],
            operador_id=admin_user.id, base=_base())
    assert erros == []
    # 1 h depois (bem além dos 35 min do checkout): segue aguardando
    assert loja_estoque_reserva.liberar_expirados(
        agora_=_base() + timedelta(hours=1)) == []
    db.session.refresh(pedido)
    assert pedido.status == 'aguardando_pagamento'
    # Depois do prazo: cancela (gateway "livre" — sem tentativa pendente)
    with patch('app.services.loja_pagamento.situacao_no_gateway',
               return_value=('livre', None)):
        codigos = loja_estoque_reserva.liberar_expirados(
            agora_=reg.prazo_pagamento + timedelta(minutes=1))
    assert pedido.codigo in codigos
    db.session.refresh(pedido)
    assert pedido.status == 'cancelado'
    assert pedido.motivo_cancelamento == 'link_expirado'      # nunca houve Pix
    assert 'Link de pagamento' in pedido.motivo_cancelamento_label
    assert svc.vencido(reg, base=reg.prazo_pagamento + timedelta(minutes=1))
    # Expiração AUTOMÁTICA: pagamento tardio REABRE como pago (regra do Pix
    # expirado) e o dono pode confirmar recebimento externo.
    from app.services import loja_pagamento, pagamento_externo
    assert pagamento_externo.pode_confirmar(pedido) is True
    with patch('app.services.loja_pagamento._baixar_estoque'), \
         patch('app.services.loja_pagamento._reservar_no_plano_do_dia'), \
         patch('app.services.loja_fiscal.agendar'), \
         patch('app.services.loja_pagamento._enviar_confirmacao'):
        assert loja_pagamento._marcar_pago(pedido, None) is True
    assert pedido.status == 'pago' and pedido.motivo_cancelamento is None
    # Cancelamento DELIBERADO continua intocável
    outro, _, _ = svc.criar(
        _form_retirada(loja, email='outra@x.com'),
        [{'kind': 'produto', 'id': prod.id, 'qtd': 1}],
        operador_id=admin_user.id, base=_base(), enviar_email=False)
    ok, _ = svc.cancelar(outro)
    assert ok and outro.motivo_cancelamento == 'cancelado_admin'
    assert pagamento_externo.pode_confirmar(outro) is False
    with patch('app.services.loja_pagamento._alertar_pedido_pago'):
        assert loja_pagamento._marcar_pago(outro, None) is False
    assert outro.status == 'cancelado'


# ── Página pública de pagamento ─────────────────────────────────────────────

@pytest.mark.loja_host
def test_pagina_de_pagamento_lista_itens_e_termos(app, admin_user):
    from app.services import pedido_assistido as svc
    loja = _loja_site()
    prod = _produto()
    r = _receita_sourdough()
    _estoque(loja, produto=prod)
    _estoque(loja, receita=r)
    with patch('app.services.email.disponivel', return_value=False):
        pedido, erros, reg = svc.criar(
            _form_retirada(loja),
            [{'kind': 'produto', 'id': prod.id, 'qtd': 2},
             {'kind': 'receita', 'id': r.id, 'qtd': 1, 'fatiado': True}],
            operador_id=admin_user.id, base=_base())
    assert erros == []
    app.config['LOJA_VISIVEL'] = '1'
    import os
    with patch.dict(os.environ, {'LOJA_VISIVEL': '1'}):
        c = app.test_client()                      # anônimo, pelo código
        html = c.get(f'/loja/pedido/{pedido.codigo}/pagamento').get_data(as_text=True)
    assert 'montado pela nossa equipe' in html
    assert '2× Box Mimo' in html
    assert 'Sourdough Teste (fatiado)' in html
    assert 'Termos de Uso' in html
    assert reg.prazo_pagamento.strftime('%d/%m') in html
    assert 'Gerar Pix' in html


def test_pedido_comum_nao_ganha_bloco_nem_registro(app):
    """Checkout do site segue idêntico: sem registro, sem bloco, e-mail
    'recebemos' e aceite carimbado como sempre."""
    from app.models import Cliente
    from app.services import loja_checkout
    from app.services import pedido_assistido as svc
    loja = _loja_site()
    prod = _produto()
    form = _form_retirada(loja)
    form['aceite_lgpd'] = '1'
    with patch('app.services.email.disponivel', return_value=True), \
         patch('app.services.email.enviar_pedido_recebido',
               return_value={'ok': True}) as recebido:
        pedido, erros = loja_checkout.criar_pedido(
            form, [{'kind': 'produto', 'id': prod.id, 'qtd': 1}], base=_base())
    assert erros == []
    recebido.assert_called_once()
    assert svc.registro_de(pedido) is None
    assert pedido.assistido is None
    cli = Cliente.query.filter_by(email='maria@x.com').first()
    assert cli.origem == 'site' and cli.aceite_lgpd_em is not None
    # TTL do checkout (35 min a partir do relógio real da reserva), não o
    # prazo de horas do pedido assistido.
    from app.services.loja_estoque_reserva import TTL_RESERVA_MIN
    from app.utils import agora
    assert pedido.reserva_expira_em is not None
    assert timedelta(0) <= (pedido.reserva_expira_em - agora()) <= \
        timedelta(minutes=TTL_RESERVA_MIN)
