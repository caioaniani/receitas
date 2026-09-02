"""Central: saldos sem duplicação, envio explícito e histórico verificável."""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

import pytest

from app.extensions import db
from app.models import EnvioCobranca, FaturaB2B, Usuario, VendaB2BParcela
from app.services.central_cobrancas import carregar, de_fatura, historico, painel
from app.utils import agora, hoje
from tests.test_b2b_email_docs import _cenario, _login


def _mensal():
    cli, v, p, c = _cenario()
    f = FaturaB2B(cliente_id=cli.id, data_inicio=hoje()-timedelta(days=30),
                  data_fim=hoje(), vencimento=p.vencimento, valor_total=v.valor_total,
                  tiny_nota_fiscal_id='nf-fatura', nf_numero='12500', nf_emitida_em=agora(),
                  nf_status='autorizada')
    db.session.add(f)
    db.session.flush()
    p.fatura_id = f.id
    v.fatura_id = f.id
    c.parcela_id = None
    c.fatura_id = f.id
    db.session.commit()
    return f, p, c


def _client(app, user):
    c = app.test_client()
    _login(c, user.id)
    return c


def _post(client, f, **data):
    return client.post(f'/cobrancas/fatura/{f.id}/documentos', data={
        'email': 'financeiro@cliente.com.br', 'chave': str(uuid4()), **data}, follow_redirects=True)


def test_painel_nao_duplica_faturas_parcelas_ou_boletos(app):
    f, p, cob = _mensal()
    f.cliente.nome = 'Cliente mensal'
    db.session.commit()
    _, v2, p2, c2 = _cenario(nosso_numero='252000042')
    linhas = painel()
    assert {(r.tipo, r.id) for r in linhas} == {('fatura', f.id), ('parcela', p2.id)}
    assert sum(r.saldo for r in linhas) == Decimal('1000')
    assert len([r for r in linhas if r.cobranca.id == cob.id]) == 1


def test_pagamento_parcial_nao_desaparece_mesmo_com_fatura_marcada_paga(app):
    f, p, c = _mensal()
    p.valor_pago = Decimal('100')
    f.status = 'paga'  # fluxo legado liquida título mesmo se retorno diverge
    c.status, c.valor_pago = 'paga', Decimal('100')
    db.session.commit()
    r = de_fatura(f)
    assert r.saldo == Decimal('400')
    assert r.pagamento == 'Parcial'
    assert r.bloqueio


def test_painel_usa_vencimento_do_boleto_sem_alterar_parcela(app):
    _, v, p, c = _cenario()
    c.vencimento = p.vencimento + timedelta(days=3)
    db.session.commit()
    r = painel()[0]
    assert r.vencimento == c.vencimento
    assert p.vencimento != c.vencimento


def test_sem_historico_nao_significa_nao_enviado(app, admin_user):
    f, p, c = _mensal()
    corpo = _client(app, admin_user).get('/cobrancas/').get_data(as_text=True)
    assert 'Sem histórico' in corpo
    assert 'Não significa que nunca foram enviadas' in corpo
    assert 'Não enviado' not in corpo


def test_resumo_ignora_canceladas_e_zeros_mas_todas_mostra_canceladas(app, admin_user):
    f, p, c = _mensal()
    f.status = 'cancelada'
    f.cliente.nome = 'Cliente mensal'
    db.session.commit()
    _, v, p2, c2 = _cenario(nosso_numero='252000042')
    p2.valor = v.valor_total = c2.valor = Decimal('0')
    db.session.commit()
    client = _client(app, admin_user)
    aberto = client.get('/cobrancas/').get_data(as_text=True)
    assert f'FAT{f.id:05d}' not in aberto
    assert 'R$ 0,00' in aberto
    todas = client.get('/cobrancas/?situacao=todas').get_data(as_text=True)
    assert f'FAT{f.id:05d}' in todas and 'Cancelada' in todas
    assert f'Venda #{v.id} · parcela' not in todas


def test_filtros_busca_vencimento_envio_e_paginacao(app, admin_user):
    cli, v, p, c = _cenario()
    for n in range(2, 37):
        db.session.add(VendaB2BParcela(venda_id=v.id, numero=n, vencimento=p.vencimento,
                                     valor=Decimal('10')))
    db.session.commit()
    client = _client(app, admin_user)
    corpo = client.get('/cobrancas/?q=Bom&envio=sem_historico').get_data(as_text=True)
    assert '36 cobrança(s)' in corpo
    assert 'Página 1 de 2' in corpo
    assert 'R$ 850,00' in corpo  # total antes da paginação
    segunda = client.get('/cobrancas/?q=Bom&pagina=2').get_data(as_text=True)
    assert 'Página 2 de 2' in segunda
    vazio = client.get('/cobrancas/?q=inexistente').get_data(as_text=True)
    assert 'Nenhuma cobrança neste filtro' in vazio
    futuro = (p.vencimento+timedelta(days=1)).isoformat()
    assert 'Nenhuma cobrança neste filtro' in client.get(f'/cobrancas/?de={futuro}').get_data(as_text=True)
    assert client.get('/cobrancas/?de=invalida&pagina=-2').status_code == 200


def test_detalhe_confirma_anexos_destinatario_sem_enviar_no_get(app, admin_user):
    f, p, c = _mensal()
    with patch('app.services.email.enviar') as enviar:
        response = _client(app, admin_user).get(f'/cobrancas/fatura/{f.id}/documentos')
    assert response.status_code == 200
    corpo = response.get_data(as_text=True)
    assert 'Enviar NF + boleto' in corpo and 'Ver DANFE (PDF)' in corpo and 'Ver boleto (PDF)' in corpo
    assert 'compras@bomprato.com.br' in corpo
    assert 'Histórico de envio' in corpo
    enviar.assert_not_called()
    assert EnvioCobranca.query.count() == 0


def test_envio_fatura_leva_dois_pdfs_e_registra_sem_mutar_financeiro(app, admin_user):
    f, p, c = _mensal()
    antes = (f.valor_total, f.status, p.valor_pago, c.status, c.nosso_numero)
    with patch('app.services.tiny_nf.baixar_danfe_pdf_com_motivo', return_value=(b'%PDF-nf', None)), \
            patch('app.services.email.enviar', return_value={'ok': True, 'id': 'provider-123'}) as enviar:
        response = _post(_client(app, admin_user), f)
    assert response.status_code == 200
    enviar.assert_called_once()
    anexos = enviar.call_args.kwargs['anexos']
    assert len(anexos) == 2
    assert all(content.startswith(b'%PDF') and mime == 'application/pdf' for _, content, mime in anexos)
    assert f'fatura {f.codigo}' in enviar.call_args.kwargs['texto']
    assert enviar.call_args.args[0] == 'financeiro@cliente.com.br'
    e = EnvioCobranca.query.one()
    assert e.fatura_id == f.id and e.venda_id is None and e.cobranca_ids == [c.id]
    assert e.status == 'aceito' and e.provedor_id == 'provider-123'
    assert e.anexos == [a[0] for a in anexos]
    assert e.usuario_id == admin_user.id
    assert (f.valor_total, f.status, p.valor_pago, c.status, c.nosso_numero) == antes
    assert 'Aceito pelo serviço de e-mail' in response.get_data(as_text=True)


def test_repetir_post_nao_duplica_envio(app, admin_user):
    f, _, _ = _mensal()
    client = _client(app, admin_user)
    chave = str(uuid4())
    with patch('app.services.tiny_nf.baixar_danfe_pdf_com_motivo', return_value=(b'%PDF-nf', None)), \
            patch('app.services.email.enviar', return_value={'ok': True, 'id': 'm'}) as enviar:
        _post(client, f, chave=chave)
        segunda = _post(client, f, chave=chave)
    enviar.assert_called_once()
    assert EnvioCobranca.query.count() == 1
    assert 'Nenhum novo e-mail' in segunda.get_data(as_text=True)


@pytest.mark.parametrize('alteracao', ['destinatario', 'origem', 'chave_invalida'])
def test_chave_nao_pode_ser_reutilizada_para_outro_envio(app, admin_user, alteracao):
    f, _, c = _mensal()
    chave = str(uuid4())
    db.session.add(EnvioCobranca(chave=chave, fatura_id=f.id if alteracao != 'origem' else f.id+1,
                                cobranca_ids=[c.id], referencia=f.codigo, documentos='nf_boleto',
                                destinatario='financeiro@cliente.com.br', status='preparando', anexos=[]))
    db.session.commit()
    dados = {'chave': chave}
    if alteracao == 'destinatario':
        dados['email'] = 'outro@cliente.com.br'
    if alteracao == 'chave_invalida':
        dados['chave'] = 'invalida'
    with patch('app.services.email.enviar') as enviar:
        corpo = _post(_client(app, admin_user), f, **dados).get_data(as_text=True)
    assert 'Reabra a tela' in corpo
    enviar.assert_not_called()
    assert EnvioCobranca.query.count() == 1


def test_envio_interrompido_nao_e_repetido_automaticamente(app, admin_user):
    f, _, c = _mensal()
    chave = str(uuid4())
    db.session.add(EnvioCobranca(chave=chave, fatura_id=f.id, cobranca_ids=[c.id],
                                referencia=f.codigo, destinatario='financeiro@cliente.com.br',
                                documentos='nf_boleto', status='preparando', anexos=[]))
    db.session.commit()
    with patch('app.services.email.enviar') as enviar:
        corpo = _post(_client(app, admin_user), f, chave=chave).get_data(as_text=True)
    enviar.assert_not_called()
    assert 'pode ter sido interrompido' in corpo
    assert EnvioCobranca.query.one().status == 'preparando'


def test_excecao_apos_iniciar_envio_exige_conferencia_antes_de_repetir(app, admin_user):
    f, _, _ = _mensal()
    with patch('app.services.tiny_nf.baixar_danfe_pdf_com_motivo', return_value=(b'%PDF-nf', None)), \
            patch('app.services.email.enviar_nf_e_boleto_b2b', side_effect=TimeoutError('sem resposta')):
        _post(_client(app, admin_user), f)
    e = EnvioCobranca.query.one()
    assert e.status == 'incerto'
    assert 'Confira antes de reenviar' in e.erro


@pytest.mark.parametrize('motivo', ['nf_indisponivel', 'boleto_indisponivel'])
def test_falha_em_um_pdf_nao_envia_nada(app, admin_user, motivo):
    f, _, _ = _mensal()
    nf = (None, 'em processamento') if motivo == 'nf_indisponivel' else (b'%PDF-nf', None)
    with patch('app.services.tiny_nf.baixar_danfe_pdf_com_motivo', return_value=nf), \
            patch('app.services.sicredi_boleto.gerar_boleto_pdf', return_value=b'erro'), \
            patch('app.services.email.enviar') as enviar:
        response = _post(_client(app, admin_user), f)
    assert response.status_code == 200
    enviar.assert_not_called()
    assert EnvioCobranca.query.one().status == 'falha'
    assert 'Nada foi enviado' in response.get_data(as_text=True)


@pytest.mark.parametrize('resultado,estado', [
    ({'ok': False, 'erro': 'Postmark recusou o destinatário'}, 'falha'),
    ({'ok': True}, 'incerto'),
    ({'ok': False, 'erro': 'timeout', 'incerto': True}, 'incerto'),
])
def test_provedor_sem_confirmacao_nao_e_marcado_como_enviado(app, admin_user, resultado, estado):
    f, _, _ = _mensal()
    with patch('app.services.tiny_nf.baixar_danfe_pdf_com_motivo', return_value=(b'%PDF-nf', None)), \
            patch('app.services.email.enviar', return_value=resultado):
        _post(_client(app, admin_user), f)
    assert EnvioCobranca.query.one().status == estado
    corpo = _client(app, admin_user).get('/cobrancas/?envio=problema').get_data(as_text=True)
    assert f.codigo in corpo


@pytest.mark.parametrize('bloqueio', ['cancelada', 'paga', 'parcial', 'sem_nf', 'sem_boleto', 'rejeitada', 'baixada'])
def test_estados_impedem_reenvio_de_cobranca_invalida(app, admin_user, bloqueio):
    f, p, c = _mensal()
    if bloqueio == 'cancelada':
        f.status = 'cancelada'
    elif bloqueio == 'paga':
        p.valor_pago = p.valor
    elif bloqueio == 'parcial':
        p.valor_pago = Decimal('100')
    elif bloqueio == 'sem_nf':
        f.nf_emitida_em = None
    elif bloqueio == 'sem_boleto':
        c.nosso_numero = None
    else:
        c.status = bloqueio
    db.session.commit()
    with patch('app.services.email.enviar') as enviar:
        response = _post(_client(app, admin_user), f)
    assert response.status_code == 200
    enviar.assert_not_called()
    assert EnvioCobranca.query.count() == 0


@pytest.mark.parametrize('email', ['', 'nao-e-email', 'a@b.com,c@d.com', 'a@b.com\r\nBcc:x@y.com', 'a'*260+'@b.com'])
def test_destinatario_invalido_bloqueado_antes_do_provedor(app, admin_user, email):
    f, _, _ = _mensal()
    with patch('app.services.email.enviar') as enviar:
        _post(_client(app, admin_user), f, email=email)
    enviar.assert_not_called()
    assert EnvioCobranca.query.count() == 0


def test_remessa_exige_confirmacao_sem_marcar_boleto_registrado(app, admin_user):
    f, _, c = _mensal()
    c.status = 'remessa'
    db.session.commit()
    client = _client(app, admin_user)
    with patch('app.services.tiny_nf.baixar_danfe_pdf_com_motivo', return_value=(b'%PDF-nf', None)), \
            patch('app.services.email.enviar', return_value={'ok': True, 'id': 'm'}) as enviar:
        _post(client, f)
        enviar.assert_not_called()
        _post(client, f, banco_confirmado='1')
        enviar.assert_called_once()
    assert c.status == 'remessa'


def test_parcela_faturada_redireciona_sem_disparar(app, admin_user):
    f, p, c = _mensal()
    client = _client(app, admin_user)
    assert carregar('parcela', p.id).tipo == 'fatura'
    with patch('app.services.email.enviar') as enviar:
        response = client.post(f'/cobrancas/parcela/{p.id}/documentos', data={'email': 'x@y.com', 'chave': str(uuid4())})
    assert response.status_code == 302
    assert f'/cobrancas/fatura/{f.id}/documentos' in response.location
    enviar.assert_not_called()


def test_historico_legado_nf_boleto_e_nf_somente(app, admin_user):
    f, _, c = _mensal()
    client = _client(app, admin_user)
    with patch('app.services.tiny_nf.baixar_danfe_pdf_com_motivo', return_value=(b'%PDF-nf', None)), \
            patch('app.services.email.enviar', return_value={'ok': True, 'id': 'antigo'}):
        client.post(f'/b2b/faturas/{f.id}/enviar-nf-email')
        client.post(f'/cobrancas/{c.id}/enviar-email')
    registros = historico(de_fatura(f))
    assert {e.documentos for e in registros} == {'nf', 'boleto'}
    assert len(registros) == 2


def test_historico_de_parcela_nao_atribui_boleto_de_outra(app, admin_user):
    _, v, p, c = _cenario()
    p2 = VendaB2BParcela(venda_id=v.id, numero=2, valor=Decimal('50'), vencimento=p.vencimento)
    db.session.add(p2)
    db.session.commit()
    with patch('app.services.email.enviar', return_value={'ok': True, 'id': 'm'}):
        _client(app, admin_user).post(f'/cobrancas/{c.id}/enviar-email')
    assert len(historico(carregar('parcela', p.id))) == 1
    assert not historico(carregar('parcela', p2.id))


@pytest.mark.parametrize('papel', ['treinamento', 'loja', 'producao'])
def test_somente_admin_pode_ver_e_enviar(app, papel):
    f, _, _ = _mensal()
    u = Usuario(nome='restrito', login='restrito', papel=papel)
    u.set_senha('test')
    db.session.add(u)
    db.session.commit()
    client = _client(app, u)
    with patch('app.services.email.enviar') as enviar:
        assert client.get('/cobrancas/').status_code == 403
        assert client.get('/cobrancas/banco').status_code == 403
        assert client.post(f'/cobrancas/fatura/{f.id}/documentos').status_code == 403
    enviar.assert_not_called()


def test_csrf_e_autenticacao_obrigatorios(app, admin_user):
    f, _, _ = _mensal()
    assert app.test_client().get('/cobrancas/').status_code == 302
    app.config['WTF_CSRF_ENABLED'] = True
    with patch('app.services.email.enviar') as enviar:
        response = _client(app, admin_user).post(f'/cobrancas/fatura/{f.id}/documentos', json={})
        assert response.status_code == 400
        assert response.json['erro'] == 'csrf_expirada'
    assert EnvioCobranca.query.count() == 0
    enviar.assert_not_called()


def test_navegacao_v2_e_classica_incluem_central(app, admin_user):
    f, p, c = _mensal()
    client = _client(app, admin_user)
    for layout in (True, False):
        app.config['UI_V2_ENABLED'] = layout
        assert client.get('/area/financeiro').status_code == 200
        assert 'A receber, NF + boleto' in client.get('/area/financeiro').get_data(as_text=True)
        assert f'/cobrancas/fatura/{f.id}/documentos' in client.get(f'/b2b/faturas/{f.id}').get_data(as_text=True)
        assert 'Histórico' in client.get(f'/b2b/vendas/{p.venda_id}').get_data(as_text=True)


def test_erro_html_escapado_no_historico(app, admin_user):
    f, _, c = _mensal()
    db.session.add(EnvioCobranca(chave=str(uuid4()), fatura_id=f.id, cobranca_ids=[c.id],
                                referencia=f.codigo, destinatario='x@y.com', documentos='nf_boleto',
                                status='falha', erro='<script>alert(1)</script>', anexos=[]))
    db.session.commit()
    corpo = _client(app, admin_user).get(f'/cobrancas/fatura/{f.id}/documentos').get_data(as_text=True)
    assert '<script>alert(1)</script>' not in corpo
    assert '&lt;script&gt;' in corpo


def test_boleto_avulso_pede_conferencia_sem_inventar_nf(app, admin_user):
    _, _, _, c = _cenario()
    c.parcela_id = None
    db.session.commit()
    r = carregar('boleto', c.id)
    assert r.nf_label == 'NF não vinculada'
    assert r.acao == 'Conferir origem'
    corpo = _client(app, admin_user).get(f'/cobrancas/boleto/{c.id}/documentos').get_data(as_text=True)
    assert 'não tem uma venda ou fatura vinculada' in corpo
    assert 'id="cob-send-form"' not in corpo


def test_migracao_isolada_idempotente_preserva_dados_e_historico():
    from importlib import import_module

    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    migracao = import_module('migrations.versions.6d9e3c7a2f10_central_cobrancas_historico')
    engine = sa.create_engine('sqlite://')
    with engine.begin() as conexao:
        conexao.execute(sa.text('CREATE TABLE saldo_existente (valor NUMERIC)'))
        conexao.execute(sa.text('INSERT INTO saldo_existente VALUES (123.45)'))
        with patch.object(migracao, 'op', Operations(MigrationContext.configure(conexao))):
            migracao.upgrade()
            conexao.execute(sa.text("""INSERT INTO envio_cobranca
                (chave, cobranca_ids, referencia, destinatario, documentos, anexos, status, criado_em)
                VALUES ('teste', '[]', 'FAT00001', 'teste@example.com', 'nf_boleto', '[]', 'aceito', CURRENT_TIMESTAMP)"""))
            migracao.upgrade()
        assert conexao.execute(sa.text('SELECT COUNT(*) FROM envio_cobranca')).scalar() == 1
        assert conexao.execute(sa.text('SELECT valor FROM saldo_existente')).scalar() == 123.45
        assert {i['name'] for i in sa.inspect(conexao).get_indexes('envio_cobranca')} == {
            'ix_envio_cobranca_fatura_id', 'ix_envio_cobranca_venda_id', 'ix_envio_cobranca_criado_em'}
    engine.dispose()
