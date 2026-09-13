"""E-mail de recompra pós-compra (13/09/2026).

Postmark e Listmonk SEMPRE mockados — nenhum teste manda e-mail.
Datas: os testes passam `hoje_` fixo pro serviço e calculam `pago_em` a
partir dele (a janela é [hoje-dias-2, hoje-dias]).
"""
import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch
from urllib.parse import unquote

from app.extensions import db

HOJE = date(2026, 9, 13)


def _cfg(app):
    app.config['POSTMARK_SERVER_TOKEN'] = 'tok-teste'


def _cliente(email, nome='Maria', descadastrado=False):
    from app.models import Cliente
    from app.utils import agora
    c = Cliente(nome=nome, email=email,
                marketing_descadastro_em=agora() if descadastrado else None)
    db.session.add(c)
    db.session.commit()
    return c


def _pago_em(dias_atras, hoje_=HOJE):
    return datetime.combine(hoje_ - timedelta(days=dias_atras), time(10, 0))


def _pedido(codigo, email, dias_atras, *, nome='Maria', status='entregue',
            itens=(), divulgacao=False, destinatario=None, cartinha=None,
            pago=True, hoje_=HOJE):
    """`itens`: [(kind, obj, qtd)] — obj é Receita ou Produto."""
    from app.models import PedidoOnline, PedidoOnlineItem
    quando = _pago_em(dias_atras, hoje_)
    p = PedidoOnline(codigo=codigo, status=status, nome_cliente=nome,
                     email_cliente=email, modo_entrega='agendada',
                     subtotal=Decimal('50'), valor_total=Decimal('50'),
                     criado_em=quando, pago_em=quando if pago else None,
                     divulgacao=divulgacao, nome_destinatario=destinatario,
                     cartinha=cartinha)
    db.session.add(p)
    db.session.flush()
    for kind, obj, qtd in itens:
        p.itens.append(PedidoOnlineItem(
            kind=kind, nome=obj.nome, quantidade=qtd,
            receita_id=obj.id if kind == 'receita' else None,
            produto_id=obj.id if kind == 'produto' else None,
            preco_unitario=Decimal('10'), subtotal=Decimal('10') * qtd))
    db.session.commit()
    return p


def _receita(nome='Sourdough Integral'):
    from app.models import Receita
    r = Receita(nome=nome, categoria='Paes', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=800.0)
    db.session.add(r)
    db.session.commit()
    return r


def _cesta(nome, componentes):
    from app.models import Produto, ProdutoItem
    c = Produto(nome=nome, categoria='Cestas', preco_site=100.0, ativo=True)
    db.session.add(c)
    db.session.flush()
    for rec in componentes:
        db.session.add(ProdutoItem(produto_id=c.id, tipo='receita',
                                   receita_id=rec.id, item_nome=rec.nome,
                                   quantidade=1))
    db.session.commit()
    return c


def _owner(app):
    from app.models import Usuario
    u = Usuario(nome='Dono', login='dono', papel='admin', is_owner=True)
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(u.id)
        s['_fresh'] = True
    return c


# ── Elegibilidade ────────────────────────────────────────────────────

def test_candidatos_aplicam_todas_as_regras(app):
    from app.models import RecompraEnvio
    from app.services import recompra
    from app.utils import agora
    with app.app_context():
        rec = _receita()
        itens = [('receita', rec, 2)]
        # A: pagou há 12 dias, não voltou → ENTRA
        _cliente('a@x.com', 'Ana')
        pa = _pedido('A1', 'a@x.com', 12, nome='Ana', itens=itens)
        # B: pagou há 12 dias mas voltou a comprar há 3 → fora
        _cliente('b@x.com', 'Bia')
        _pedido('B1', 'b@x.com', 12, nome='Bia', itens=itens)
        _pedido('B2', 'b@x.com', 3, nome='Bia', itens=itens)
        # C: descadastrado → fora
        _cliente('c@x.com', 'Cid', descadastrado=True)
        _pedido('C1', 'c@x.com', 12, nome='Cid', itens=itens)
        # D: fora da janela (20 dias) → fora
        _cliente('d@x.com', 'Dan')
        _pedido('D1', 'd@x.com', 20, nome='Dan', itens=itens)
        # E: cancelado → fora ; F: divulgação → fora ; G: não pago → fora
        _cliente('e@x.com'); _pedido('E1', 'e@x.com', 12, status='cancelado', itens=itens)
        _cliente('f@x.com'); _pedido('F1', 'f@x.com', 12, divulgacao=True, itens=itens)
        _cliente('g@x.com'); _pedido('G1', 'g@x.com', 12, pago=False,
                                     status='aguardando_pagamento', itens=itens)
        # H: sem Cliente cadastrado → fora (não há onde honrar o descadastro)
        _pedido('H1', 'h@x.com', 12, itens=itens)
        # I: dois pedidos na janela (13 e 12 dias) → UM candidato, o mais recente
        _cliente('i@x.com', 'Ivo')
        _pedido('I1', 'i@x.com', 13, nome='Ivo', itens=itens)
        pi2 = _pedido('I2', 'i@x.com', 12, nome='Ivo', itens=itens)
        # J: já recebeu recompra há 5 dias (outro pedido) → anti-spam, fora
        _cliente('j@x.com', 'Jo')
        pj_old = _pedido('J0', 'j@x.com', 40, nome='Jo', itens=itens)
        _pedido('J1', 'j@x.com', 12, nome='Jo', itens=itens)
        db.session.add(RecompraEnvio(pedido_id=pj_old.id, email='j@x.com',
                                     tipo='pao',
                                     enviado_em=agora() - timedelta(days=5)))
        db.session.commit()
        # K: e-mail em caixa mista no pedido e no cadastro → casa
        _cliente('Ka@X.com', 'Kel')
        pk = _pedido('K1', 'KA@x.com', 12, nome='Kel', itens=itens)

        lista = recompra.candidatos(hoje_=HOJE, d=12)
        codigos = sorted(c['pedido'].codigo for c in lista)
        assert codigos == sorted([pa.codigo, pi2.codigo, pk.codigo])
        assert all(c['tipo'] == 'pao' for c in lista)
        assert {c['email'] for c in lista} == {'a@x.com', 'i@x.com', 'ka@x.com'}


def test_tipo_presente_por_destinatario_cartinha_ou_cesta(app):
    from app.services import recompra
    with app.app_context():
        rec = _receita()
        cesta = _cesta('Family Box', [rec])
        p_pao = _pedido('P1', 'p@x.com', 12, itens=[('receita', rec, 1)])
        p_dest = _pedido('P2', 'p@x.com', 12, itens=[('receita', rec, 1)],
                         destinatario='Vó')
        p_cart = _pedido('P3', 'p@x.com', 12, itens=[('receita', rec, 1)],
                         cartinha='Parabéns!')
        p_cesta = _pedido('P4', 'p@x.com', 12, itens=[('produto', cesta, 1)])
        assert recompra.tipo_do_pedido(p_pao) == 'pao'
        assert recompra.tipo_do_pedido(p_dest) == 'presente'
        assert recompra.tipo_do_pedido(p_cart) == 'presente'
        assert recompra.tipo_do_pedido(p_cesta) == 'presente'


def test_dias_config_torta_cai_no_padrao(app):
    from app.models import AppConfig
    from app.services import recompra
    with app.app_context():
        assert recompra.dias() == 12
        AppConfig.set(recompra.CFG_DIAS, 'abc'); db.session.commit()
        assert recompra.dias() == 12
        AppConfig.set(recompra.CFG_DIAS, '0'); db.session.commit()
        assert recompra.dias() == 12
        AppConfig.set(recompra.CFG_DIAS, '20'); db.session.commit()
        assert recompra.dias() == 20


# ── Envio ────────────────────────────────────────────────────────────

def test_nasce_desligado_e_nao_envia(app):
    from app.services import recompra
    _cfg(app)
    with app.app_context():
        rec = _receita()
        _cliente('a@x.com')
        _pedido('A1', 'a@x.com', 12, itens=[('receita', rec, 1)])
        with patch('app.services.email.enviar') as env:
            st = recompra.rodar(hoje_=HOJE)
        assert st['candidatos'] == 1
        assert st['enviados'] == 0 and st['pulou'] == 'desligado'
        env.assert_not_called()


def test_rodar_envia_registra_e_e_idempotente(app):
    from app.models import RecompraEnvio
    from app.services import recompra
    _cfg(app)
    with app.app_context():
        rec = _receita('Croissant Tradicional')
        _cliente('a@x.com', 'Ana Souza')
        p = _pedido('A1', 'a@x.com', 12, nome='Ana Souza',
                    itens=[('receita', rec, 6)])
        with patch('app.services.email.enviar',
                   return_value={'ok': True, 'id': 'msg-1'}) as env:
            st = recompra.rodar(forcar=True, hoje_=HOJE)
        assert st['enviados'] == 1 and st['erros'] == 0
        env.assert_called_once()
        args, kwargs = env.call_args
        assert args[0] == 'a@x.com'
        assert args[1] == recompra.ASSUNTO_PAO_PADRAO
        html = args[2]
        assert kwargs['stream'] == 'broadcast'          # NUNCA o transacional
        assert 'Ana,' in html and '6× Croissant Tradicional' in html
        assert 'utm_medium=recompra' in html and 'recompra-pao' in html
        assert 'https://opao.online/loja/croissant-tradicional-r' in html
        m = re.search(r'/loja/marketing/sair/([^"]+)"', html)
        assert m and recompra.email_do_token(unquote(m.group(1))) == 'a@x.com'
        # Registro + idempotência: segunda rodada não acha nada
        reg = RecompraEnvio.query.filter_by(pedido_id=p.id).one()
        assert reg.email == 'a@x.com' and reg.tipo == 'pao'
        assert reg.message_id == 'msg-1'
        with patch('app.services.email.enviar') as env2:
            st2 = recompra.rodar(forcar=True, hoje_=HOJE)
        assert st2['candidatos'] == 0 and st2['enviados'] == 0
        env2.assert_not_called()


def test_envio_falho_nao_registra_e_retenta_no_dia_seguinte(app):
    from app.models import RecompraEnvio
    from app.services import recompra
    _cfg(app)
    with app.app_context():
        rec = _receita()
        _cliente('a@x.com')
        _pedido('A1', 'a@x.com', 12, itens=[('receita', rec, 1)])
        with patch('app.services.email.enviar',
                   return_value={'ok': False, 'erro': 'Postmark 422'}):
            st = recompra.rodar(forcar=True, hoje_=HOJE)
        assert st['enviados'] == 0 and st['erros'] == 1
        assert RecompraEnvio.query.count() == 0
        # amanhã o pedido ainda está na janela (13 dias) → tenta de novo
        assert len(recompra.candidatos(hoje_=HOJE + timedelta(days=1))) == 1


def test_teto_por_rodada_bloqueia_disparo_em_massa(app, monkeypatch):
    from app.services import recompra
    _cfg(app)
    monkeypatch.setattr(recompra, 'TETO_POR_RODADA', 1)
    with app.app_context():
        rec = _receita()
        for i in range(2):
            _cliente(f'{i}@x.com')
            _pedido(f'T{i}', f'{i}@x.com', 12, itens=[('receita', rec, 1)])
        with patch('app.services.email.enviar') as env:
            st = recompra.rodar(forcar=True, hoje_=HOJE)
        assert st['candidatos'] == 2 and st['enviados'] == 0
        assert 'teto' in st['erro']
        env.assert_not_called()


def test_texto_presente_e_escape(app):
    from app.services import recompra
    with app.app_context():
        rec = _receita('Pão <b>Francês</b>')
        cesta = _cesta('Box Mimo', [rec])
        p = _pedido('X1', 'x@x.com', 12, nome='<script>alert(1)</script>',
                    itens=[('produto', cesta, 1)], destinatario='Mãe')
        ass, html, texto = recompra.montar_email(p, 'presente')
        assert ass == recompra.ASSUNTO_PRESENTE_PADRAO
        assert '<script>' not in html and '&lt;script&gt;' in html
        assert 'Ver cestas e presentes' in html
        assert 'recompra-presente' in html
        assert 'Box Mimo' in texto


# ── Descadastro ──────────────────────────────────────────────────────

def test_token_roundtrip_e_descadastro_propaga_ao_listmonk(app):
    from app.models import Cliente
    from app.services import recompra
    with app.app_context():
        c = _cliente('Ana@X.com', 'Ana')
        tok = recompra.token_sair('ana@x.com')
        assert recompra.email_do_token(tok) == 'ana@x.com'
        assert recompra.email_do_token(tok + 'x') is None
        assert recompra.email_do_token('') is None
        with patch('app.services.listmonk.disponivel', return_value=True), \
             patch('app.services.listmonk.id_por_email', return_value=77) as busca, \
             patch('app.services.listmonk.mudar_listas') as muda, \
             patch('app.services.marketing._ids_permanentes',
                   return_value=[1, 2, 3]):
            n = recompra.descadastrar('ANA@x.com')
        assert n == 1
        assert db.session.get(Cliente, c.id).marketing_descadastro_em is not None
        busca.assert_called_once_with('ana@x.com')
        muda.assert_called_once_with([77], 'unsubscribe', [1, 2, 3])
        # idempotente: segunda vez não marca ninguém de novo
        with patch('app.services.listmonk.disponivel', return_value=False):
            assert recompra.descadastrar('ana@x.com') == 0


def test_rota_publica_de_descadastro(app):
    from app.models import Cliente
    from app.services import recompra
    with app.app_context():
        c = _cliente('ana@x.com', 'Ana')
        client = app.test_client()
        assert client.get('/loja/marketing/sair/lixo').status_code == 404
        with patch('app.services.listmonk.disponivel', return_value=False):
            r = client.get(f'/loja/marketing/sair/{recompra.token_sair("ana@x.com")}')
        assert r.status_code == 200
        assert 'ana@x.com' in r.get_data(as_text=True)
        assert db.session.get(Cliente, c.id).marketing_descadastro_em is not None


# ── Tela ─────────────────────────────────────────────────────────────

def test_resumo_conta_enviados_e_voltaram(app):
    from app.models import RecompraEnvio
    from app.services import recompra
    from app.utils import agora
    with app.app_context():
        rec = _receita()
        _cliente('a@x.com'); _cliente('b@x.com')
        pa = _pedido('A1', 'a@x.com', 20, itens=[('receita', rec, 1)])
        pb = _pedido('B1', 'b@x.com', 20, itens=[('receita', rec, 1)])
        db.session.add_all([
            RecompraEnvio(pedido_id=pa.id, email='a@x.com', tipo='pao',
                          enviado_em=agora() - timedelta(days=6)),
            RecompraEnvio(pedido_id=pb.id, email='b@x.com', tipo='pao',
                          enviado_em=agora() - timedelta(days=6)),
        ])
        db.session.commit()
        # A voltou (pedido pago DEPOIS do envio); B não
        _pedido('A2', 'a@x.com', 2, itens=[('receita', rec, 1)],
                hoje_=agora().date())
        r = recompra.resumo(hoje_=HOJE)
        assert r['enviados_30d'] == 2 and r['voltaram_30d'] == 1
        assert r['taxa_pct'] == 50.0
        assert r['auto'] is False and r['dias'] == 12


def test_rotas_admin_salvar_dry_run_e_teste(app):
    from app.models import AppConfig
    from app.services import recompra
    _cfg(app)
    with app.app_context():
        rec = _receita('Brioche')
        _cliente('a@x.com', 'Ana')
        _pedido('A1', 'a@x.com', 1, nome='Ana', itens=[('receita', rec, 2)],
                hoje_=date.today())
        client = _owner(app)

        r = client.post('/admin/marketing/recompra/salvar',
                        data={'dias': '15', 'assunto_pao': 'Volta!',
                              'assunto_presente': 'Presente?', 'auto': '1'})
        assert r.status_code == 302
        assert AppConfig.get(recompra.CFG_DIAS) == '15'
        assert AppConfig.get(recompra.CFG_ATIVO) == '1'
        assert recompra.assunto('pao') == 'Volta!'
        assert recompra.assunto('presente') == 'Presente?'
        # prazo inválido = nada salvo
        r = client.post('/admin/marketing/recompra/salvar',
                        data={'dias': '999', 'auto': ''})
        assert r.status_code == 302
        assert AppConfig.get(recompra.CFG_DIAS) == '15'
        assert AppConfig.get(recompra.CFG_ATIVO) == '1'

        # dry-run nunca manda
        with patch('app.services.email.enviar') as env:
            assert client.post('/admin/marketing/recompra/rodar').status_code == 302
        env.assert_not_called()

        # teste: vai pro e-mail do dono, com o link de sair DELE, sem registro
        with patch('app.services.email.enviar',
                   return_value={'ok': True, 'id': 'm'}) as env:
            r = client.post('/admin/marketing/recompra/teste',
                            data={'email': 'dono@opao.online', 'tipo': 'presente'})
        assert r.status_code == 302
        args, kwargs = env.call_args
        assert args[0] == 'dono@opao.online'
        assert args[1].startswith('[TESTE] ')
        assert kwargs['stream'] == 'broadcast'
        m = re.search(r'/loja/marketing/sair/([^"]+)"', args[2])
        assert recompra.email_do_token(unquote(m.group(1))) == 'dono@opao.online'
        from app.models import RecompraEnvio
        assert RecompraEnvio.query.count() == 0

        # a tela renderiza com o card novo
        pagina = client.get('/admin/marketing').get_data(as_text=True)
        assert 'E-mail de recompra' in pagina


def test_email_enviar_usa_stream_pedido(app):
    """`email.enviar(stream=...)` vai no payload do Postmark; sem stream cai
    no transacional de sempre."""
    from app.services import email as email_svc
    _cfg(app)
    with app.app_context():
        class _R:
            status_code = 200
            text = '{}'

            def json(self):
                return {'ErrorCode': 0, 'MessageID': 'x'}
        with patch('app.services.email.requests.post', return_value=_R()) as post:
            email_svc.enviar('a@x.com', 'oi', '<p>x</p>', stream='broadcast')
            email_svc.enviar('a@x.com', 'oi', '<p>x</p>')
        streams = [c.kwargs['json']['MessageStream'] for c in post.call_args_list]
        assert streams == ['broadcast', 'outbound']
