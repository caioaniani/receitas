"""Emissão de NF via Tiny (Fase 5 — Plano B: cria a NF direto).

Fluxo: nota.fiscal.incluir (cabeçalho fiscal explícito — tipo/série/natureza
+ cliente com endereço + itens por SKU) → nota.fiscal.emitir (autoriza na
SEFAZ). Não cria pedido no Tiny (o gerar.nota.fiscal.pedido não aplicava a
natureza, deixando natOp vazio + série fora de ordem em prod). NÃO chama o
Tiny real (mockado).
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
    """Cria a NF (incluir_nota_fiscal) e emite (emitir_nota_fiscal) nessa
    ordem, com SKU mapeado + cabeçalho fiscal (tipo/série/natureza)."""
    from app.extensions import db
    from app.services import tiny_nf
    with app.app_context():
        produto = _produto(db, preco=20.0)
        p = _pedido_pago(db, produto, qtd=2, sku='SKU-XYZ')
        with patch('app.services.tiny.incluir_nota_fiscal',
                   return_value={'ok': True, 'id': 'nf-9',
                                 'numero': '11428'}) as inc, \
             patch('app.services.tiny.emitir_nota_fiscal',
                   return_value={'ok': True, 'status': 'autorizada'}) as emi:
            res = tiny_nf.emitir_nf(p)
        assert res['ok'] and res['nota_fiscal_id'] == 'nf-9'
        nota = inc.call_args[0][0]
        assert nota['tipo'] == 'S'
        assert nota['serie'] == '1'
        assert nota['natureza_operacao'] == 'Venda de mercadorias'
        assert nota['frete_por_conta'] == 'R'   # letra (Tiny PHP trata "0" como vazio)
        assert nota['itens'][0]['item']['codigo'] == 'SKU-XYZ'
        assert nota['itens'][0]['item']['quantidade'] == 2.0
        emi.assert_called_once_with('nf-9')
        db.session.refresh(p)
        assert p.tiny_nota_fiscal_id == 'nf-9'
        assert p.nf_emitida_em is not None


def test_emitir_nf_payload_inclui_endereco_e_serie(app):
    """A NF leva o endereço estruturado no cliente + natureza + série
    explícitas — sem isso a SEFAZ rejeitava (endereço/natOp vazios, série
    fora de ordem)."""
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
        with patch('app.services.tiny.incluir_nota_fiscal',
                   return_value={'ok': True, 'id': 'nf'}) as inc, \
             patch('app.services.tiny.emitir_nota_fiscal',
                   return_value={'ok': True, 'status': 'autorizada'}):
            res = tiny_nf.emitir_nf(p)
        assert res['ok'] is True
        nota = inc.call_args[0][0]
        cli = nota['cliente']
        assert cli['endereco'] == 'Rua das Flores'
        assert cli['numero'] == '123'
        assert cli['bairro'] == 'Centro'
        assert cli['cidade'] == 'São Paulo'
        assert cli['uf'] == 'SP'           # normalizado pra maiúsculo
        assert cli['cep'] == '01001-000'
        assert nota['natureza_operacao'] == 'Venda de mercadorias'
        assert nota['serie'] == '1'


def test_emitir_nf_idempotente(app):
    """NF emitida COM SUCESSO (nf_emitida_em setado) NÃO chama o Tiny."""
    from app.extensions import db
    from app.services import tiny_nf
    from app.utils import agora
    with app.app_context():
        produto = _produto(db)
        p = _pedido_pago(db, produto, sku='SKU')
        p.tiny_nota_fiscal_id = 'nf-existente'
        p.nf_emitida_em = agora()           # emitida com sucesso
        db.session.commit()
        with patch('app.services.tiny.incluir_nota_fiscal') as inc:
            res = tiny_nf.emitir_nf(p)
        assert res['ok'] and res['nota_fiscal_id'] == 'nf-existente'
        inc.assert_not_called()


def test_emitir_nf_resume_nota_existente(app):
    """NF já criada mas emissão falhou (sem recriar): reusa o id e só retoma
    a emissão — não cria outra NF (evita duplicar)."""
    from app.extensions import db
    from app.services import tiny_nf
    with app.app_context():
        produto = _produto(db)
        p = _pedido_pago(db, produto, sku='S')
        p.tiny_nota_fiscal_id = 'nf-rascunho'
        p.nf_status = 'pendente'            # criada, não autorizada
        db.session.commit()
        with patch('app.services.tiny.incluir_nota_fiscal') as inc, \
             patch('app.services.tiny.emitir_nota_fiscal',
                   return_value={'ok': True, 'status': 'autorizada'}) as emi:
            res = tiny_nf.emitir_nf(p)
        assert res['ok'] is True
        inc.assert_not_called()             # não criou outra NF
        emi.assert_called_once_with('nf-rascunho')


def test_emitir_nf_recriar_descarta_nota_anterior(app):
    """recriar=True descarta a NF rejeitada e cria uma nova do zero (não
    reusa a rejeitada)."""
    from app.extensions import db
    from app.services import tiny_nf
    with app.app_context():
        produto = _produto(db)
        p = _pedido_pago(db, produto, sku='S')
        p.tiny_nota_fiscal_id = 'nf-rejeitada'
        p.nf_status = 'pendente'
        db.session.commit()
        with patch('app.services.tiny.incluir_nota_fiscal',
                   return_value={'ok': True, 'id': 'nf-novo'}) as inc, \
             patch('app.services.tiny.emitir_nota_fiscal',
                   return_value={'ok': True, 'status': 'autorizada'}):
            res = tiny_nf.emitir_nf(p, recriar=True)
        assert res['ok'] is True
        inc.assert_called_once()            # criou NF nova do zero
        db.session.refresh(p)
        assert p.tiny_nota_fiscal_id == 'nf-novo'
        assert p.nf_emitida_em is not None


def test_emitir_nf_bloqueia_sem_sku(app):
    """Item sem SKU mapeado: aborta com mensagem clara, NÃO cria NF parcial."""
    from app.extensions import db
    from app.services import tiny_nf
    with app.app_context():
        produto = _produto(db, nome='Sem Mapeamento')
        p = _pedido_pago(db, produto)       # sku=None
        with patch('app.services.tiny.incluir_nota_fiscal') as inc:
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


def test_emitir_nf_falha_na_emissao_propaga_erro(app):
    """incluir OK mas emitir falha (rejeição SEFAZ): guarda o id, marca o
    status e devolve erro reenviável. NÃO marca nf_emitida_em."""
    from app.extensions import db
    from app.services import tiny_nf
    with app.app_context():
        produto = _produto(db)
        p = _pedido_pago(db, produto, sku='S')
        with patch('app.services.tiny.incluir_nota_fiscal',
                   return_value={'ok': True, 'id': 'nf-rasc'}), \
             patch('app.services.tiny.emitir_nota_fiscal',
                   return_value={'ok': False, 'status': 'pendente',
                                 'erro': "natOp minLength"}):
            res = tiny_nf.emitir_nf(p)
        assert res['ok'] is False
        assert 'natOp' in res['msg']
        db.session.refresh(p)
        assert p.tiny_nota_fiscal_id == 'nf-rasc'   # guardou pra reenviar
        assert p.nf_emitida_em is None


def test_botao_emitir_nf_no_admin(app):
    from app.extensions import db
    c = _owner(app)
    produto = _produto(db)
    p = _pedido_pago(db, produto, sku='S')
    with patch('app.services.tiny.incluir_nota_fiscal',
               return_value={'ok': True, 'id': 'nf', 'numero': '1'}), \
         patch('app.services.tiny.emitir_nota_fiscal',
               return_value={'ok': True, 'status': 'autorizada'}):
        r = c.post(f'/admin/loja-online/pedidos/{p.codigo}/emitir-nf',
                   follow_redirects=False)
    assert r.status_code == 302
    with app.app_context():
        from app.models import PedidoOnline
        atual = PedidoOnline.query.filter_by(codigo=p.codigo).first()
        assert atual.tiny_nota_fiscal_id == 'nf'


def test_botao_refazer_nf_passa_recriar(app):
    """O botão 'Refazer NF' (recriar=1) descarta a NF rejeitada e cria nova."""
    from app.extensions import db
    c = _owner(app)
    produto = _produto(db)
    with app.app_context():
        from app.models import PedidoOnline
        p = _pedido_pago(db, produto, sku='S')
        p.tiny_nota_fiscal_id = 'nf-ruim'
        p.nf_status = 'pendente'
        db.session.commit()
        codigo = p.codigo
    with patch('app.services.tiny.incluir_nota_fiscal',
               return_value={'ok': True, 'id': 'nf-bom'}) as inc, \
         patch('app.services.tiny.emitir_nota_fiscal',
               return_value={'ok': True, 'status': 'autorizada'}):
        r = c.post(f'/admin/loja-online/pedidos/{codigo}/emitir-nf',
                   data={'recriar': '1'}, follow_redirects=False)
    assert r.status_code == 302
    inc.assert_called_once()
    with app.app_context():
        from app.models import PedidoOnline
        atual = PedidoOnline.query.filter_by(codigo=codigo).first()
        assert atual.tiny_nota_fiscal_id == 'nf-bom'


def test_incluir_nota_fiscal_ok(app):
    """incluir_nota_fiscal devolve o id da NF criada (registros normalizados)."""
    from app.services import tiny
    with app.app_context():
        app.config['TINY_API_TOKEN'] = 'tok'

        class R:
            status_code = 200

            def json(self):
                return {'retorno': {'status': 'OK', 'registros': {
                    'registro': {'id': '908', 'numero': '11428'}}}}
        with patch('app.services.tiny.requests.post', return_value=R()):
            res = tiny.incluir_nota_fiscal({'tipo': 'S', 'itens': []})
        assert res['ok'] is True and res['id'] == '908'


def test_incluir_nota_fiscal_erro_propaga_mensagem(app):
    """Tiny recusa a NF: a mensagem real volta no 'erro'."""
    from app.services import tiny
    with app.app_context():
        app.config['TINY_API_TOKEN'] = 'tok'

        class R:
            status_code = 200

            def json(self):
                return {'retorno': {'status': 'Erro', 'registros': {
                    'registro': {'erros': [{'erro': 'natureza invalida'}]}}}}
        with patch('app.services.tiny.requests.post', return_value=R()):
            res = tiny.incluir_nota_fiscal({'tipo': 'S'})
        assert res['ok'] is False and 'natureza' in res['erro']


# ── tiny.py: funções do Plano A (criar pedido + gerar NF do pedido) ────────
# Mantidas como fallback enquanto o Plano B (nota.fiscal.incluir) não está
# 100% confirmado em prod. Testam o cliente da API, não o orquestrador.

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


def test_emitir_nota_fiscal_status_3_e_autorizada(app):
    """status_processamento '3' = autorizada (confirmado em prod com a NF
    011428). Antes o '3' caía como falha apesar de a SEFAZ ter autorizado."""
    from app.services import tiny
    with app.app_context():
        app.config['TINY_API_TOKEN'] = 'tok'

        class R:
            status_code = 200

            def json(self):
                return {'retorno': {'status': 'OK', 'status_processamento': '3'}}
        with patch('app.services.tiny.requests.post', return_value=R()):
            res = tiny.emitir_nota_fiscal('nf-1')
        assert res['ok'] is True and res['status'] == 'autorizada'


def test_emitir_nota_fiscal_ja_autorizada_e_sucesso(app):
    """Reemitir uma NF já autorizada é sucesso (não marca falha em algo
    válido na SEFAZ — evita o usuário 'Refazer' e duplicar)."""
    from app.services import tiny
    with app.app_context():
        app.config['TINY_API_TOKEN'] = 'tok'

        class R:
            status_code = 200

            def json(self):
                return {'retorno': {'status': 'Erro', 'registros': {
                    'registro': {'erros': [{
                        'erro': 'Nota fiscal já autorizada'}]}}}}
        with patch('app.services.tiny.requests.post', return_value=R()):
            res = tiny.emitir_nota_fiscal('nf-1')
        assert res['ok'] is True


def test_emitir_nota_fiscal_status_2_e_erro(app):
    """status_processamento '2' = rejeitada → falha com o motivo SEFAZ."""
    from app.services import tiny
    with app.app_context():
        app.config['TINY_API_TOKEN'] = 'tok'

        class R:
            status_code = 200

            def json(self):
                return {'retorno': {'status': 'OK', 'status_processamento': '2',
                                    'erros': [{'erro': 'Rejeicao SEFAZ 999'}]}}
        with patch('app.services.tiny.requests.post', return_value=R()):
            res = tiny.emitir_nota_fiscal('nf-1')
        assert res['ok'] is False
        assert 'Rejeicao' in res['erro']


def test_reenviar_detecta_nf_ja_autorizada_via_obter(app):
    """Reenviar uma NF que JÁ autorizou em background (status_processamento
    ambíguo no emitir): o sincronizar via `obter` resolve. Regressão 011428."""
    from app.extensions import db
    from app.services import tiny_nf
    with app.app_context():
        produto = _produto(db)
        p = _pedido_pago(db, produto, sku='S')
        p.tiny_nota_fiscal_id = 'nf-autorizada-bg'
        p.nf_status = '2'                   # status ambíguo do emitir anterior
        db.session.commit()
        with patch('app.services.tiny.obter_nota_fiscal',
                   return_value={'situacao': 'Autorizada'}), \
             patch('app.services.tiny.incluir_nota_fiscal') as inc, \
             patch('app.services.tiny.emitir_nota_fiscal') as emi:
            res = tiny_nf.emitir_nf(p)
        assert res['ok'] is True
        assert 'autorizada' in res['msg'].lower()
        inc.assert_not_called()             # não criou duplicata
        emi.assert_not_called()             # nem precisou re-emitir
        db.session.refresh(p)
        assert p.nf_emitida_em is not None
        assert p.nf_status == 'autorizada'


def test_reenviar_detecta_nf_rejeitada_via_obter(app):
    """NF rejeitada pela SEFAZ: o sincronizar avisa e orienta a Refazer."""
    from app.extensions import db
    from app.services import tiny_nf
    with app.app_context():
        produto = _produto(db)
        p = _pedido_pago(db, produto, sku='S')
        p.tiny_nota_fiscal_id = 'nf-rej'
        db.session.commit()
        with patch('app.services.tiny.obter_nota_fiscal',
                   return_value={'situacao': 'Rejeitada'}), \
             patch('app.services.tiny.emitir_nota_fiscal') as emi:
            res = tiny_nf.emitir_nf(p)
        assert res['ok'] is False
        assert 'rejeit' in res['msg'].lower()
        assert 'refazer' in res['msg'].lower()
        emi.assert_not_called()             # não tenta re-emitir uma rejeitada
        db.session.refresh(p)
        assert p.nf_emitida_em is None      # NÃO marcou como emitida


def test_emitir_obter_confirma_quando_emit_devolve_ambiguo(app):
    """emitir_nota_fiscal retorna ok=False (status ambíguo) mas a NF
    autorizou em background — o obter pós-emit captura isso."""
    from app.extensions import db
    from app.services import tiny_nf
    with app.app_context():
        produto = _produto(db)
        p = _pedido_pago(db, produto, sku='S')
        with patch('app.services.tiny.incluir_nota_fiscal',
                   return_value={'ok': True, 'id': 'nf-x'}), \
             patch('app.services.tiny.emitir_nota_fiscal',
                   return_value={'ok': False, 'status': '2',
                                 'erro': 'status 2'}), \
             patch('app.services.tiny.obter_nota_fiscal',
                   return_value={'situacao': 'autorizada'}):
            res = tiny_nf.emitir_nf(p)
        assert res['ok'] is True
        db.session.refresh(p)
        assert p.nf_emitida_em is not None


def test_danfe_redireciona_pro_link(app):
    """A rota do DANFE redireciona pro link temporário do Tiny."""
    from app.extensions import db
    c = _owner(app)
    produto = _produto(db)
    with app.app_context():
        p = _pedido_pago(db, produto, sku='S')
        p.tiny_nota_fiscal_id = 'nf-1'
        db.session.commit()
        codigo = p.codigo
    with patch('app.services.tiny_nf.link_danfe',
               return_value='https://tiny/danfe.pdf'):
        r = c.get(f'/admin/loja-online/pedidos/{codigo}/danfe',
                  follow_redirects=False)
    assert r.status_code == 302
    assert r.headers['Location'] == 'https://tiny/danfe.pdf'


def test_danfe_sem_link_volta_pro_detalhe(app):
    """Sem link (NF não autorizada): avisa e volta pro detalhe, sem 500."""
    from app.extensions import db
    c = _owner(app)
    produto = _produto(db)
    with app.app_context():
        p = _pedido_pago(db, produto, sku='S')
        codigo = p.codigo
    with patch('app.services.tiny_nf.link_danfe', return_value=None):
        r = c.get(f'/admin/loja-online/pedidos/{codigo}/danfe',
                  follow_redirects=False)
    assert r.status_code == 302
    assert f'/pedidos/{codigo}' in r.headers['Location']


def test_detalhe_mostra_botao_emitir_pra_pago(app):
    from app.extensions import db
    c = _owner(app)
    produto = _produto(db)
    p = _pedido_pago(db, produto, sku='S')
    r = c.get(f'/admin/loja-online/pedidos/{p.codigo}')
    assert r.status_code == 200
    assert b'Emitir NF' in r.data
