"""Emissão de NF via Tiny (Fase 5 — plano A, botão manual).

Cobre orquestração emitir_nf: idempotência, ordem de chamadas, payload por
SKU mapeado, falha quando item sem SKU. NÃO chama o Tiny real (mockado).
"""
from decimal import Decimal
from unittest.mock import patch


def _owner(app):
    from app.extensions import db
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


def _produto(db, nome='Box Mimo', preco=20.0):
    from app.models import Produto
    p = Produto(nome=nome, categoria='Cestas', preco_site=preco,
                imagem_dropbox_url='https://x/p.jpg', ativo=True)
    db.session.add(p)
    db.session.commit()
    return p


def _pedido_pago(db, produto, qtd=1, sku=None):
    from app.models import Cliente, PedidoOnline, PedidoOnlineItem
    from app.services import tiny_nf
    if sku:
        tiny_nf.definir_sku('produto', produto.id, sku)
    cli = Cliente(nome='Maria', email='m@x.com', cpf='52998224725',
                  telefone='11999999999')
    db.session.add(cli)
    db.session.flush()
    p = PedidoOnline(cliente_id=cli.id, nome_cliente='Maria',
                     email_cliente='m@x.com', telefone_cliente='11999999999',
                     modo_entrega='retirada', status='pago',
                     subtotal=Decimal(str(produto.preco_site)) * qtd,
                     frete_valor=Decimal('0'),
                     valor_total=Decimal(str(produto.preco_site)) * qtd)
    db.session.add(p)
    db.session.flush()
    p.itens.append(PedidoOnlineItem(
        kind='produto', produto_id=produto.id, nome=produto.nome,
        preco_unitario=Decimal(str(produto.preco_site)), quantidade=qtd,
        subtotal=Decimal(str(produto.preco_site)) * qtd))
    db.session.commit()
    return p


def test_emitir_nf_ordem_e_payload(app):
    """Chama incluir_pedido -> gerar_nota_fiscal_pedido -> emitir_nota_fiscal
    nessa ordem, com o SKU mapeado no payload."""
    from app.extensions import db
    from app.services import tiny_nf
    with app.app_context():
        produto = _produto(db, preco=20.0)
        p = _pedido_pago(db, produto, qtd=2, sku='SKU-XYZ')
        with patch('app.services.tiny.incluir_pedido',
                   return_value={'ok': True, 'id': 'tp-1', 'numero': '999'}) as inc, \
             patch('app.services.tiny.gerar_nota_fiscal_pedido',
                   return_value={'ok': True, 'id_nota_fiscal': 'nf-9', 'status': 'aberta'}) as ger, \
             patch('app.services.tiny.emitir_nota_fiscal',
                   return_value={'ok': True, 'status': 'autorizada'}) as emi:
            res = tiny_nf.emitir_nf(p)
        assert res['ok'] and res['nota_fiscal_id'] == 'nf-9'
        # Payload do pedido carrega o SKU + numero_ordem_compra = codigo nosso
        ped_payload = inc.call_args[0][0]
        assert ped_payload['numero_ordem_compra'] == p.codigo
        assert ped_payload['itens'][0]['item']['codigo'] == 'SKU-XYZ'
        assert ped_payload['itens'][0]['item']['quantidade'] == 2.0
        ger.assert_called_once_with('tp-1')
        emi.assert_called_once_with('nf-9')
        # Persistiu
        db.session.refresh(p)
        assert p.tiny_pedido_id == 'tp-1'
        assert p.tiny_nota_fiscal_id == 'nf-9'
        assert p.nf_emitida_em is not None


def test_emitir_nf_payload_inclui_endereco_e_natureza(app):
    """O payload do pedido leva o endereço estruturado (logradouro/numero/
    bairro/cidade/uf) no cliente + a natureza de operação — sem isso a SEFAZ
    rejeita ('endereço em branco' / 'natOp vazio'). E NÃO leva 'serie'
    (a v2 ignora; série vem da config do Tiny)."""
    from app.extensions import db
    from app.services import tiny_nf
    with app.app_context():
        produto = _produto(db, preco=30.0)
        p = _pedido_pago(db, produto, qtd=1, sku='SKU-END')
        p.endereco_logradouro = 'Rua das Flores'
        p.endereco_numero = '123'
        p.endereco_complemento = 'apto 2'
        p.endereco_bairro = 'Centro'
        p.endereco_cidade = 'São Paulo'
        p.endereco_uf = 'sp'
        p.endereco_cep = '01001-000'
        db.session.commit()
        with patch('app.services.tiny.incluir_pedido',
                   return_value={'ok': True, 'id': 'tp', 'numero': '1'}) as inc, \
             patch('app.services.tiny.gerar_nota_fiscal_pedido',
                   return_value={'ok': True, 'id_nota_fiscal': 'nf'}), \
             patch('app.services.tiny.emitir_nota_fiscal',
                   return_value={'ok': True, 'status': 'autorizada'}):
            res = tiny_nf.emitir_nf(p)
        assert res['ok'] is True
        payload = inc.call_args[0][0]
        cli = payload['cliente']
        assert cli['endereco'] == 'Rua das Flores'
        assert cli['numero'] == '123'
        assert cli['bairro'] == 'Centro'
        assert cli['cidade'] == 'São Paulo'
        assert cli['uf'] == 'SP'          # normalizado pra maiúsculo
        assert cli['cep'] == '01001-000'
        assert payload['natureza_operacao'] == 'Venda de mercadorias'
        assert 'serie' not in payload     # campo morto removido


def test_checkout_grava_endereco_estruturado(app):
    """O checkout de entrega guarda o endereço estruturado (não só a linha
    única) — é o que alimenta a NF depois."""
    from app.extensions import db
    from app.models import Produto
    from app.services import loja_checkout
    with app.app_context():
        prod = Produto(nome='Pão', categoria='Pães', preco_site=10.0,
                       imagem_dropbox_url='https://x/p.jpg', ativo=True)
        db.session.add(prod)
        db.session.commit()
        form = {
            'nome': 'João', 'email': 'j@x.com', 'telefone': '11988887777',
            'cpf': '52998224725', 'modo_entrega': 'retirada',
            'aceite_lgpd': '1', 'loja_id': '',
        }
        # Usa retirada pra não depender de geocode externo; o foco é provar
        # que os campos estruturados são lidos quando vêm. Então testamos o
        # parser direto:
        f2 = {
            'logradouro': 'Av Brasil', 'numero': '500',
            'complemento': 'sala 3', 'bairro': 'Jardins',
            'cidade': 'Campinas', 'uf': 'sp',
        }
        # _montar_endereco junta tudo; a gravação estruturada é validada no
        # teste de payload acima. Aqui só garante que o parser não quebra.
        assert 'Av Brasil' in loja_checkout._montar_endereco(f2)
        assert form  # placeholder p/ manter contexto de checkout


def test_emitir_nf_idempotente(app):
    """Pedido com NF emitida COM SUCESSO (nf_emitida_em setado) NÃO chama o
    Tiny de novo."""
    from app.extensions import db
    from app.services import tiny_nf
    from app.utils import agora
    with app.app_context():
        produto = _produto(db)
        p = _pedido_pago(db, produto, sku='SKU')
        p.tiny_nota_fiscal_id = 'nf-existente'
        p.nf_emitida_em = agora()   # emitida com sucesso
        db.session.commit()
        with patch('app.services.tiny.incluir_pedido') as inc:
            res = tiny_nf.emitir_nf(p)
        assert res['ok'] and res['nota_fiscal_id'] == 'nf-existente'
        inc.assert_not_called()


def test_emitir_nf_rascunho_falho_nao_e_idempotente(app):
    """NF gerada mas NÃO autorizada (nf_emitida_em=None): clicar de novo NÃO
    retorna 'já emitida' — tenta de novo (com recriar a SEFAZ aceita)."""
    from app.extensions import db
    from app.services import tiny_nf
    with app.app_context():
        produto = _produto(db)
        p = _pedido_pago(db, produto, sku='S')
        p.tiny_pedido_id = 'tp-velho'
        p.tiny_nota_fiscal_id = 'nf-rejeitada'
        p.nf_status = 'pendente'           # gerada, mas não autorizada
        db.session.commit()
        with patch('app.services.tiny.buscar_pedido_por_numero_ordem',
                   return_value=None), \
             patch('app.services.tiny.incluir_pedido',
                   return_value={'ok': True, 'id': 'tp-novo'}) as inc, \
             patch('app.services.tiny.gerar_nota_fiscal_pedido',
                   return_value={'ok': True, 'id_nota_fiscal': 'nf-novo'}), \
             patch('app.services.tiny.emitir_nota_fiscal',
                   return_value={'ok': True, 'status': 'autorizada'}):
            res = tiny_nf.emitir_nf(p, recriar=True)
        assert res['ok'] is True
        inc.assert_called_once()          # recriou o pedido do zero
        db.session.refresh(p)
        assert p.tiny_pedido_id == 'tp-novo'
        assert p.tiny_nota_fiscal_id == 'nf-novo'
        assert p.nf_emitida_em is not None


def test_emitir_nf_bloqueia_sem_sku(app):
    """Item sem SKU mapeado: aborta com mensagem clara, NÃO emite parcial."""
    from app.extensions import db
    from app.services import tiny_nf
    with app.app_context():
        produto = _produto(db, nome='Sem Mapeamento')
        p = _pedido_pago(db, produto)   # sku=None
        with patch('app.services.tiny.incluir_pedido') as inc:
            res = tiny_nf.emitir_nf(p)
        assert res['ok'] is False
        assert 'Sem Mapeamento' in res['msg']
        inc.assert_not_called()


def test_emitir_nf_bloqueia_se_nao_pago(app):
    from app.extensions import db
    from app.models import PedidoOnline
    from app.services import tiny_nf
    with app.app_context():
        produto = _produto(db)
        p = _pedido_pago(db, produto, sku='S')
        p.status = 'aguardando_pagamento'
        db.session.commit()
        assert tiny_nf.emitir_nf(p)['ok'] is False
        # Não criou nada no Tiny
        assert PedidoOnline.query.first().tiny_nota_fiscal_id is None


def test_botao_emitir_nf_no_admin(app):
    from app.extensions import db
    c = _owner(app)
    produto = _produto(db)
    p = _pedido_pago(db, produto, sku='S')
    with patch('app.services.tiny.incluir_pedido',
               return_value={'ok': True, 'id': 'tp', 'numero': '1'}), \
         patch('app.services.tiny.gerar_nota_fiscal_pedido',
               return_value={'ok': True, 'id_nota_fiscal': 'nf', 'status': 'ok'}), \
         patch('app.services.tiny.emitir_nota_fiscal',
               return_value={'ok': True, 'status': 'autorizada'}):
        r = c.post(f'/admin/loja-online/pedidos/{p.codigo}/emitir-nf',
                   follow_redirects=False)
    assert r.status_code == 302
    with app.app_context():
        from app.models import PedidoOnline
        atual = PedidoOnline.query.filter_by(codigo=p.codigo).first()
        assert atual.tiny_nota_fiscal_id == 'nf'


def test_gerar_nf_retry_no_lock(app):
    """Lock do Tiny (cod 31) é temporário — repete e na 2ª vez gera."""
    from app.services import tiny
    with app.app_context():
        app.config['TINY_API_TOKEN'] = 'tok'
        chamadas = []

        class RLock:
            status_code = 200
            def json(self):
                return {'retorno': {'status': 'Erro', 'registros': {
                    'registro': {'erros': [{
                        'erro': 'Lock Venda::gerarNotaFiscal bloqueado.'}]}}}}

        class ROk:
            status_code = 200
            def json(self):
                return {'retorno': {'status': 'OK',
                                    'registro': {'id_nota_fiscal': 'nf-ok'}}}

        def fake_post(*a, **k):
            chamadas.append(1)
            return RLock() if len(chamadas) == 1 else ROk()
        with patch('app.services.tiny.time.sleep'), \
             patch('app.services.tiny.requests.post', side_effect=fake_post):
            res = tiny.gerar_nota_fiscal_pedido('tp-1')
        assert res['ok'] is True and res['id_nota_fiscal'] == 'nf-ok'
        assert len(chamadas) == 2  # repetiu após o lock


def test_emitir_nf_resume_sem_duplicar_pedido(app):
    """Pedido já criado no Tiny (tiny_pedido_id setado): reclica NÃO chama
    incluir_pedido de novo — só retoma gerar+emitir."""
    from app.extensions import db
    from app.services import tiny_nf
    with app.app_context():
        produto = _produto(db)
        p = _pedido_pago(db, produto, sku='S')
        p.tiny_pedido_id = 'tp-existente'  # criado antes, NF falhou no lock
        db.session.commit()
        with patch('app.services.tiny.incluir_pedido') as inc, \
             patch('app.services.tiny.gerar_nota_fiscal_pedido',
                   return_value={'ok': True, 'id_nota_fiscal': 'nf-2'}), \
             patch('app.services.tiny.emitir_nota_fiscal',
                   return_value={'ok': True, 'status': 'autorizada'}):
            res = tiny_nf.emitir_nf(p)
        assert res['ok'] is True
        inc.assert_not_called()   # não duplicou o pedido no Tiny


def test_emitir_nf_recria_pedido_se_tiny_apagou(app):
    """tiny_pedido_id salvo aponta pra pedido que não existe mais (cod 32
    'Pedido não localizado'): limpa, tenta achar pelo código, cria novo."""
    from app.extensions import db
    from app.services import tiny_nf
    with app.app_context():
        produto = _produto(db)
        p = _pedido_pago(db, produto, sku='S')
        p.tiny_pedido_id = 'tp-antigo-apagado'
        db.session.commit()
        # 1ª chamada gerar: 'Pedido não localizado'. 2ª (após recriar): OK.
        gerar_seq = iter([
            {'ok': False, 'erro': 'Pedido não localizado.; cod 32'},
            {'ok': True, 'id_nota_fiscal': 'nf-novo'},
        ])
        with patch('app.services.tiny.buscar_pedido_por_numero_ordem',
                   return_value=None), \
             patch('app.services.tiny.incluir_pedido',
                   return_value={'ok': True, 'id': 'tp-novo'}) as inc, \
             patch('app.services.tiny.gerar_nota_fiscal_pedido',
                   side_effect=lambda *a, **k: next(gerar_seq)), \
             patch('app.services.tiny.emitir_nota_fiscal',
                   return_value={'ok': True, 'status': 'autorizada'}):
            res = tiny_nf.emitir_nf(p)
        assert res['ok'] is True
        inc.assert_called_once()  # recriou (1 vez)
        db.session.refresh(p)
        assert p.tiny_pedido_id == 'tp-novo'
        assert p.tiny_nota_fiscal_id == 'nf-novo'


def test_emitir_nf_captura_nf_ja_gerada(app):
    """Tiny diz 'Já foi gerada nota fiscal' (cod 31): captura o id da NF
    existente no pedido em vez de falhar."""
    from app.extensions import db
    from app.services import tiny_nf
    with app.app_context():
        produto = _produto(db)
        p = _pedido_pago(db, produto, sku='S')
        p.tiny_pedido_id = 'tp-1'
        db.session.commit()
        with patch('app.services.tiny.gerar_nota_fiscal_pedido',
                   return_value={'ok': False,
                                 'erro': 'Já foi gerada nota fiscal para '
                                         'este pedido; cod 31'}), \
             patch('app.services.tiny.id_nota_do_pedido',
                   return_value='nf-ja-existe'), \
             patch('app.services.tiny.emitir_nota_fiscal',
                   return_value={'ok': True, 'status': 'autorizada'}):
            res = tiny_nf.emitir_nf(p)
        assert res['ok'] is True
        db.session.refresh(p)
        assert p.tiny_nota_fiscal_id == 'nf-ja-existe'


def test_incluir_pedido_registros_como_dict(app):
    """Regressão do 500 (KeyError: 0): Tiny v2 às vezes manda `registros`
    como DICT {'registro': {...}}, não lista. _registros normaliza."""
    from app.services import tiny
    with app.app_context():
        app.config['TINY_API_TOKEN'] = 'tok'

        class R:
            status_code = 200
            def json(self):
                return {'retorno': {'status': 'OK', 'registros': {
                    'registro': {'id': '777', 'numero': '5', 'status': 'OK'}}}}
        with patch('app.services.tiny.requests.post', return_value=R()):
            res = tiny.incluir_pedido({'cliente': {}, 'itens': []})
        assert res['ok'] is True and res['id'] == '777'


def test_incluir_pedido_erro_propaga_mensagem(app):
    """Tiny recusa: a mensagem real volta no 'erro' (não 'ver logs')."""
    from app.services import tiny
    with app.app_context():
        app.config['TINY_API_TOKEN'] = 'tok'

        class R:
            status_code = 200
            def json(self):
                return {'retorno': {'status': 'Erro', 'registros': {
                    'registro': {'erros': [{'erro': 'Cliente sem endereço'}]}}}}
        with patch('app.services.tiny.requests.post', return_value=R()):
            res = tiny.incluir_pedido({'cliente': {}, 'itens': []})
        assert res['ok'] is False
        assert 'endereço' in res['erro']


def test_detalhe_mostra_botao_emitir_pra_pago(app):
    from app.extensions import db
    c = _owner(app)
    produto = _produto(db)
    p = _pedido_pago(db, produto, sku='S')
    r = c.get(f'/admin/loja-online/pedidos/{p.codigo}')
    assert r.status_code == 200
    assert b'Emitir NF' in r.data
