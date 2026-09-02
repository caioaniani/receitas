"""Fakes de Tiny/Postmark: jamais emitir NF ou enviar cobranças de teste reais."""
from datetime import timedelta
from decimal import Decimal
from importlib import import_module
from unittest.mock import patch
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from flask import g

from app.extensions import db
from app.models import (
    AppConfig,
    AutomacaoCobranca,
    AvisoRemessa,
    Cobranca,
    CobrancaRemessa,
    ConfirmacaoRegistroBoleto,
    DelegacaoFiscalB2B,
    EnvioCobranca,
    TentativaNFB2B,
    Usuario,
    VendaB2BParcela,
)
from app.services import cobrancas_automacao as svc
from app.services import faturas_b2b, tiny_nf_b2b
from app.services.central_cobrancas import carregar
from app.services.cobrancas_trava import OperacaoEmAndamento, trava
from app.utils import hoje
from tests.test_b2b_emitir_nf import _cliente_completo, _venda
from tests.test_central_cobrancas import _client


@pytest.fixture
def provedores():
    with patch('app.services.tiny.incluir_nota_fiscal', return_value={'ok': True, 'id': 'nf-77', 'numero': '12500'}) as nf, \
            patch('app.services.tiny.emitir_nota_fiscal', return_value={'ok': True, 'status': 'autorizada'}) as emitir, \
            patch('app.services.tiny_nf.baixar_danfe_pdf_com_motivo', return_value=(b'%PDF-falso', None)), \
            patch('app.services.email.enviar', return_value={'ok': True, 'id': 'email-77'}) as email:
        yield nf, emitir, email


def _avulsa(mensal=False, parcelas=True):
    cli = _cliente_completo()
    cli.faturamento_mensal = mensal
    v = _venda(cli, sku='SKU-B2B')
    v.status_entrega = 'entregue'
    if parcelas:
        db.session.add(VendaB2BParcela(venda_id=v.id, numero=1, valor=v.valor_total,
                                     vencimento=hoje() + timedelta(days=15), forma_pagamento='boleto'))
    db.session.commit()
    return v


def _fila(v, usuario):
    j = svc.enfileirar(v, usuario.id)
    db.session.commit()
    return j


def test_fluxo_entrega_aguarda_banco_avisa_ambos_e_envia_uma_vez(app, owner_user, provedores):
    nf, emitir, email = provedores
    v = _avulsa()
    j = _fila(v, owner_user)
    svc.executar()
    assert j.estado == 'banco'
    assert nf.call_count == emitir.call_count == 1
    c, rem = Cobranca.query.one(), CobrancaRemessa.query.one()
    assert c.valor == v.valor_total and c.status == 'remessa'
    assert EnvioCobranca.query.count() == 0
    assert {a.destinatario for a in AvisoRemessa.query.all()} == set(svc.RESPONSAVEIS)
    assert all(a.estado == 'aceito' for a in AvisoRemessa.query.all())
    assert email.call_count == 2
    assert all('Sicredi' in call.args[1] and rem.nome_arquivo in call.args[2] for call in email.call_args_list)
    assert svc.enfileirar(v, owner_user.id).id == j.id
    svc.executar()
    assert email.call_count == 2 and nf.call_count == 1  # Nem novo aviso nem NF.
    svc.confirmar_registro(rem, owner_user.id)
    assert c.status == 'remessa'  # Atestado não é retorno CNAB inventado.
    svc.executar()
    assert j.estado == 'enviado' and email.call_count == 3
    e = EnvioCobranca.query.one()
    assert e.status == 'aceito' and len(e.anexos) == 2
    assert e.copias_ocultas == ['caio@opao.online', 'dakson@opao.online', 'contato@opao.online']
    svc.executar()
    assert email.call_count == 3 and Cobranca.query.count() == 1


@pytest.mark.parametrize('parcelas', [False, True])
def test_entrega_mensal_nunca_enfileira_cobranca_individual(app, owner_user, provedores, parcelas):
    v = _avulsa(mensal=True, parcelas=parcelas)
    assert _fila(v, owner_user) is None
    svc.executar()
    assert Cobranca.query.count() == AutomacaoCobranca.query.count() == 0
    provedores[0].assert_not_called()
    provedores[2].assert_not_called()


def test_fechamento_mensal_cria_uma_nf_um_boleto_so_ao_fechar(app, owner_user, provedores):
    v = _avulsa(mensal=True, parcelas=False)
    v2 = _venda(v.cliente, sku='SKU-B2B-2')
    assert svc.enfileirar(v) is None
    f = faturas_b2b.fechar_conta(v.cliente, hoje()-timedelta(days=30), hoje(), hoje()+timedelta(days=15), owner_user.id)
    j = AutomacaoCobranca.query.one()
    assert j.tipo == 'fatura' and j.documento_id == f.id
    assert f.valor_total == v.valor_total + v2.valor_total
    svc.executar()
    c = Cobranca.query.one()
    assert c.fatura_id == f.id and c.parcela_id is None and c.valor == Decimal('200')
    assert provedores[0].call_count == 1
    assert j.estado == 'banco'


def test_nao_varre_historico_e_get_nao_emite(app, owner_user, provedores):
    _avulsa()
    svc.executar()
    c = _client(app, owner_user)
    assert c.get('/cobrancas/automacao').status_code == 200
    assert c.get('/cobrancas/').status_code == 200
    assert Cobranca.query.count() == AutomacaoCobranca.query.count() == 0
    provedores[0].assert_not_called()
    provedores[2].assert_not_called()


def test_painel_mostra_ciclo_real_sem_fabricar_heartbeat_no_get(app, owner_user, provedores):
    c = _client(app, owner_user)
    corpo = c.get('/cobrancas/automacao').get_data(as_text=True)
    assert 'Aguarde o primeiro ciclo' in corpo
    assert AppConfig.get('cobrancas_automacao_ultimo_ciclo') is None
    svc.executar()
    antes = AppConfig.get('cobrancas_automacao_ultimo_ciclo')
    assert antes
    corpo = c.get('/cobrancas/automacao').get_data(as_text=True)
    assert 'Última verificação automática' in corpo
    assert AppConfig.get('cobrancas_automacao_ultimo_ciclo') == antes


def test_entrega_post_atomico_idempotente_sem_rede(app, owner_user, provedores):
    v = _avulsa()
    v.status_entrega = 'separado'
    db.session.commit()
    c = _client(app, owner_user)
    assert c.post(f'/padeiro/b2b/{v.id}/entregue').status_code == 302
    assert v.status_entrega == 'entregue' and AutomacaoCobranca.query.count() == 1
    c.post(f'/padeiro/b2b/{v.id}/entregue')
    assert AutomacaoCobranca.query.count() == 1
    provedores[0].assert_not_called()
    provedores[2].assert_not_called()


@pytest.mark.parametrize('problema', ['cancelada', 'divulgacao', 'parcial', 'paga', 'pix', 'total', 'email', 'entrega', 'mensal'])
def test_falhas_de_elegibilidade_nao_emitem(app, owner_user, provedores, problema):
    v = _avulsa()
    j = _fila(v, owner_user)
    if problema == 'cancelada':
        v.status = 'cancelada'
    elif problema == 'divulgacao':
        v.dispensa_cobranca = {'motivo': 'divulgação', 'usuario_id': owner_user.id}
    elif problema in ('parcial', 'paga'):
        v.parcelas[0].valor_pago = Decimal('1' if problema == 'parcial' else '100')
    elif problema == 'pix':
        v.parcelas[0].forma_pagamento = 'pix'
    elif problema == 'total':
        v.parcelas[0].valor = Decimal('99')
    elif problema == 'email':
        v.cliente.email = ''
    elif problema == 'entrega':
        v.status_entrega = 'pendente'
    else:
        v.cliente.faturamento_mensal = True
    db.session.commit()
    svc.executar()
    assert j.estado == 'erro' and j.erro
    assert Cobranca.query.count() == 0
    provedores[0].assert_not_called()
    provedores[2].assert_not_called()


def test_nf_timeout_sem_id_persistido_nunca_cria_novamente(app, owner_user, provedores):
    v = _avulsa()
    j = _fila(v, owner_user)
    provedores[0].side_effect = TimeoutError('resposta perdida')
    svc.executar()
    assert j.estado == 'erro' and TentativaNFB2B.query.one().estado == 'conferir'
    svc._mudar(j, 'pendente')  # Até solicitação de conferência humana não duplica.
    svc.executar()
    assert provedores[0].call_count == 1 and Cobranca.query.count() == 0
    assert 'Confira no Tiny' in j.erro


def test_prevalidacao_nf_falha_nao_cria_intencao_incerta(app, provedores):
    v = _avulsa()
    v.cliente.endereco_cep = ''
    db.session.commit()
    assert not tiny_nf_b2b.emitir_nf(v)['ok']
    assert TentativaNFB2B.query.count() == 0
    v.cliente.endereco_cep = '04568001'
    db.session.commit()
    assert tiny_nf_b2b.emitir_nf(v)['ok']
    assert provedores[0].call_count == 1


def test_nf_autorizada_nao_recria_nem_com_recriar_true(app, provedores):
    v = _avulsa()
    assert tiny_nf_b2b.emitir_nf(v)['ok']
    assert tiny_nf_b2b.emitir_nf(v, recriar=True)['ok']
    assert provedores[0].call_count == 1


def test_edicao_durante_emissao_nao_gera_boleto_com_nf_diferente(app, owner_user, provedores):
    v = _avulsa()
    j = _fila(v, owner_user)

    def editar_durante_emissao(*args):
        v.itens[0].quantidade = 20
        v.valor_total = v.parcelas[0].valor = Decimal('200')
        db.session.commit()
        return {'ok': True, 'status': 'autorizada'}

    provedores[1].side_effect = editar_durante_emissao
    svc.executar()
    assert j.estado == 'erro' and 'mudaram' in j.erro
    assert Cobranca.query.count() == 0 and EnvioCobranca.query.count() == 0


def test_verificar_nf_existente_nao_depende_de_recriar_payload(app, provedores):
    v = _avulsa()
    v.tiny_nota_fiscal_id = 'nf-existente'
    v.cliente.endereco_cep = ''
    db.session.commit()
    with patch('app.services.tiny.obter_nota_fiscal', return_value={'situacao': 'autorizada'}):
        assert tiny_nf_b2b.emitir_nf(v)['ok']
    provedores[0].assert_not_called()


def test_envio_manual_antes_do_worker_nao_duplica(app, owner_user, provedores):
    from app.services.cobrancas_envio import enviar_conjunto
    v = _avulsa()
    j = _fila(v, owner_user)
    svc.executar()
    c = Cobranca.query.one()
    r = carregar('boleto', c.id)
    e, _ = enviar_conjunto(r, r.email, str(uuid4()), owner_user, banco_confirmado=True)
    assert e.status == 'aceito'
    svc.executar()
    assert j.estado == 'enviado' and EnvioCobranca.query.count() == 1
    assert provedores[2].call_count == 3


def test_email_timeout_nao_reenvia_automaticamente(app, owner_user, provedores):
    v = _avulsa()
    j = _fila(v, owner_user)
    svc.executar()
    svc.confirmar_registro(CobrancaRemessa.query.one(), owner_user.id)
    provedores[2].side_effect = TimeoutError('resposta perdida')
    svc.executar()
    assert j.estado == 'erro' and EnvioCobranca.query.one().status == 'incerto'
    svc._mudar(j, 'pendente')
    svc.executar()
    assert provedores[2].call_count == 3
    assert EnvioCobranca.query.count() == 1


def test_aviso_interno_incerto_nao_repete_nem_afeta_o_outro_responsavel(app, owner_user, provedores):
    _fila(_avulsa(), owner_user)
    provedores[2].side_effect = [TimeoutError('resposta perdida'), {'ok': True, 'id': 'dakson-aceito'}]
    svc.executar()
    assert [a.estado for a in AvisoRemessa.query.order_by(AvisoRemessa.id)] == ['incerto', 'aceito']
    svc.executar()
    assert provedores[2].call_count == 2
    assert AutomacaoCobranca.query.one().estado == 'banco'


def test_intencao_e_entrega_voltam_juntas_no_rollback(app, owner_user):
    v = _avulsa()
    v.status_entrega = 'separado'
    db.session.commit()
    v.status_entrega = 'entregue'
    svc.enfileirar(v, owner_user.id)
    db.session.flush()
    db.session.rollback()
    assert v.status_entrega == 'separado' and AutomacaoCobranca.query.count() == 0


def test_remessa_antiga_aparece_no_painel_sem_aviso_retroativo(app, owner_user, provedores):
    from tests.test_cobrancas_sicredi import _cobranca
    c = _cobranca()
    rem = CobrancaRemessa(numero=1, n_titulos=1, conteudo='remessa antiga')
    db.session.add(rem)
    db.session.flush()
    c.status, c.remessa_id, c.nosso_numero = 'remessa', rem.id, '262000001'
    db.session.commit()
    svc.executar()
    assert svc.remessas_pendentes() == [rem]
    assert AvisoRemessa.query.count() == 0
    corpo = _client(app, owner_user).get('/cobrancas/automacao').get_data(as_text=True)
    assert rem.nome_arquivo in corpo and 'Baixar arquivo para o Sicredi' in corpo
    provedores[2].assert_not_called()


def test_boleto_quitado_antes_da_confirmacao_nao_envia_email(app, owner_user, provedores):
    _fila(_avulsa(), owner_user)
    svc.executar()
    c = Cobranca.query.one()
    c.status = 'paga'
    c.valor_pago = c.parcela.valor_pago = c.valor
    db.session.commit()
    svc.executar()
    assert AutomacaoCobranca.query.one().estado == 'erro'
    assert EnvioCobranca.query.count() == 0 and provedores[2].call_count == 2


def test_retorno_registrado_libera_e_atestado_perde_validade_quando_muda_titulo(app, owner_user, provedores):
    _fila(_avulsa(), owner_user)
    svc.executar()
    c = Cobranca.query.one()
    svc.confirmar_registro(CobrancaRemessa.query.one(), owner_user.id)
    assert svc.banco_confirmado(c)
    c.vencimento += timedelta(days=1)
    db.session.commit()
    assert not svc.banco_confirmado(c)
    c.status = 'registrada'  # Mesmo estado persistido pelo importador de retorno.
    db.session.commit()
    svc.executar()
    assert AutomacaoCobranca.query.one().estado == 'enviado'


def test_confirmacao_exige_checkbox_admin_e_registra_ator(app, owner_user, admin_user, provedores):
    _fila(_avulsa(), owner_user)
    svc.executar()
    rem = CobrancaRemessa.query.one()
    c = _client(app, admin_user)
    path = f'/cobrancas/automacao/remessa/{rem.id}/confirmar'
    assert c.get(path).status_code == 405
    c.post(path)
    assert ConfirmacaoRegistroBoleto.query.count() == 0
    c.post(path, data={'confirmado': '1'})
    assert ConfirmacaoRegistroBoleto.query.one().usuario_id == admin_user.id
    assert EnvioCobranca.query.count() == 0  # POST não chama integrações.
    antes = ConfirmacaoRegistroBoleto.query.one().confirmado_em
    app.config['WTF_CSRF_ENABLED'] = True
    assert c.post(path, data={'confirmado': '1'}).status_code == 302  # Handler CSRF mostra flash.
    assert ConfirmacaoRegistroBoleto.query.one().confirmado_em == antes


def test_delegacao_nf_restrita_revogavel_sem_promocao_a_owner(app, owner_user, admin_user, provedores):
    v = _avulsa()
    c = _client(app, admin_user)
    grant = f'/auth/usuarios/{admin_user.id}/nf-b2b'
    emitir = f'/b2b/vendas/{v.id}/emitir-nf'
    assert c.post(emitir).status_code == 403
    assert c.post(grant, data={'permitir': '1'}).status_code == 403
    owner = _client(app, owner_user)
    g.pop('_login_user', None)  # Fixture compartilha app_context entre clientes.
    assert owner.post(grant, data={'permitir': '1'}).status_code == 302
    assert admin_user.pode_emitir_nf_b2b() and not admin_user.is_owner
    g.pop('_login_user', None)
    assert c.post(emitir, data={'recriar': '1'}).status_code == 403
    assert c.post(emitir).status_code == 302 and provedores[0].call_count == 1
    assert DelegacaoFiscalB2B.query.one().concedida_por_id == owner_user.id
    g.pop('_login_user', None)
    owner.post(grant, data={'permitir': '0'})
    g.pop('_login_user', None)
    assert c.post(emitir).status_code == 403


@pytest.mark.parametrize('papel,treino', [('funcionario', False), ('observador', False), ('padeiro', False), ('admin', True)])
def test_nao_admin_ou_so_treino_nao_acessa_automacao(app, papel, treino):
    u = Usuario(nome='Restrito', login='restrito', papel=papel, somente_treino=treino, senha_hash='x')
    db.session.add(u)
    db.session.commit()
    c = _client(app, u)
    assert c.get('/cobrancas/automacao').status_code in (302, 403)
    assert c.post('/cobrancas/automacao/remessa/1/confirmar', data={'confirmado': '1'}).status_code in (302, 403)
    assert not u.pode_emitir_nf_b2b()


def test_lock_sobrevive_commit_e_libera_apos_excecao(app):
    with pytest.raises(RuntimeError):
        with trava('teste'):
            db.session.commit()
            with pytest.raises(OperacaoEmAndamento):
                with trava('teste'):
                    pass
            raise RuntimeError('falha')
    with trava('teste'):
        pass


def test_migracao_somente_tabelas_novas_idempotente():
    mod = import_module('migrations.versions.c83a91d52f06_automacao_cobranca_b2b')
    engine = sa.create_engine('sqlite://')
    with engine.begin() as conn:
        conn.execute(sa.text('CREATE TABLE usuario (id INTEGER PRIMARY KEY)'))
        with Operations.context(MigrationContext.configure(conn)):
            mod.upgrade()
            mod.upgrade()
        assert set(sa.inspect(conn).get_table_names()) == {'usuario', 'delegacao_fiscal_b2b', 'tentativa_nf_b2b', 'automacao_cobranca', 'aviso_remessa', 'confirmacao_registro_boleto'}
        assert [c['name'] for c in sa.inspect(conn).get_columns('usuario')] == ['id']
