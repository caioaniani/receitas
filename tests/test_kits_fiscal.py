"""Fila fiscal de kits: retry durável, totais individuais e emissão sem duplicar."""
import threading
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.extensions import db
from app.models import (
    Cliente,
    CompraKit,
    EntregaKit,
    KitCafe,
    PedidoOnline,
    PedidoOnlineItem,
    Produto,
    TarefaFiscalKit,
)
from app.services import loja_fiscal as kits_fiscal
from app.services import seru_cron, tiny, tiny_nf

BASE = datetime(2026, 9, 14, 9)


@pytest.fixture(autouse=True)
def tiny_falso(app, monkeypatch):
    monkeypatch.setattr(kits_fiscal, 'agora', lambda: BASE)
    monkeypatch.setattr(tiny_nf, 'agora', lambda: BASE)
    incluir = Mock(side_effect=lambda _payload, **kwargs: {'ok': True, 'id': f'nf-{incluir.call_count}'})
    emitir = Mock(return_value={'ok': True, 'status': 'autorizada'})
    obter = Mock(return_value={})
    email = Mock(return_value={'ok': True})
    monkeypatch.setattr(tiny, 'incluir_nota_fiscal', incluir)
    monkeypatch.setattr(tiny, 'emitir_nota_fiscal', emitir)
    monkeypatch.setattr(tiny, 'obter_nota_fiscal', obter)
    monkeypatch.setattr('app.services.email.enviar_nf_emitida', email)
    monkeypatch.setattr('app.services.email.disponivel', lambda: True)
    return SimpleNamespace(incluir=incluir, emitir=emitir, obter=obter, email=email)


def _grupo(owner, quantidade=2):
    produto = Produto(nome='Kit teste', ativo=True, preco_site=20, site_ativo=True)
    cliente = Cliente(nome='Maria', email=f'maria-{quantidade}@example.com',
                       cpf='52998224725', telefone='11999998888')
    kit = KitCafe(nome='Café mensal', ativo=True, usuario_id=owner.id)
    db.session.add_all([produto, cliente, kit])
    db.session.flush()
    tiny_nf.definir_sku('produto', produto.id, 'SKU-KIT')
    pedidos = []
    for n in range(quantidade):
        pedido = PedidoOnline(
            cliente_id=cliente.id, nome_cliente=cliente.nome, email_cliente=cliente.email,
            telefone_cliente=cliente.telefone, status='pago', pago_em=BASE,
            modo_entrega='agendada', data_entrega=BASE.date(),
            janela_entrega='09:00–10:00', endereco_logradouro='Rua Teste',
            endereco_numero='10', endereco_bairro='Moema', endereco_cidade='São Paulo',
            endereco_uf='SP', endereco_cep='04077000', subtotal=Decimal('20.00'),
            frete_valor=Decimal('5.00'), valor_total=Decimal('25.00'))
        pedido.itens.append(PedidoOnlineItem(
            kind='produto', produto_id=produto.id, nome=produto.nome, quantidade=1,
            preco_unitario=Decimal('20.00'), subtotal=Decimal('20.00')))
        db.session.add(pedido)
        pedidos.append(pedido)
    db.session.flush()
    compra = CompraKit(kit_id=kit.id, kit_nome=kit.nome, pedido_principal_id=pedidos[0].id,
                        subtotal=Decimal('20.00') * quantidade,
                        frete_total=Decimal('5.00') * quantidade,
                        valor_total=Decimal('25.00') * quantidade,
                        pago_em=BASE, expira_em=BASE, checkout_token='a' * 64)
    db.session.add(compra)
    db.session.flush()
    for ordem, pedido in enumerate(pedidos, 1):
        db.session.add(EntregaKit(compra_id=compra.id, pedido_id=pedido.id, ordem=ordem))
        db.session.add(TarefaFiscalKit(pedido_id=pedido.id, criado_em=BASE,
                                       proxima_tentativa_em=BASE))
    db.session.commit()
    return compra, pedidos


def test_fila_emite_uma_nota_por_entrega_com_valor_individual(owner_user, tiny_falso):
    compra, pedidos = _grupo(owner_user)
    resumo = kits_fiscal.processar_pendentes(base=BASE)
    assert resumo == {'processados': 2, 'concluidos': 2, 'pendentes': 0, 'ignorados': 0}
    assert tiny_falso.incluir.call_count == 2
    assert tiny_falso.emitir.call_count == 2
    assert tiny_falso.email.call_count == 2
    for chamada in tiny_falso.incluir.call_args_list:
        payload = chamada.args[0]
        assert payload['valor_frete'] == 5.0
        assert payload['itens'][0]['item']['valor_unitario'] == 20.0
        assert payload['itens'][0]['item']['quantidade'] == 1.0
    assert compra.valor_total == Decimal('50.00')
    assert all(p.nf_emitida_em and p.tiny_nota_fiscal_id for p in pedidos)
    assert all(t.concluido_em and t.tentativas == 1 for t in TarefaFiscalKit.query.all())
    assert all(t.email_enviado_em == BASE for t in TarefaFiscalKit.query.all())
    assert kits_fiscal.processar_pendentes(base=BASE)['processados'] == 0
    assert tiny_falso.incluir.call_count == 2


def test_fila_respeita_limite_de_notas_por_ciclo(owner_user, tiny_falso):
    _grupo(owner_user, quantidade=7)
    assert kits_fiscal.processar_pendentes(limite=5, base=BASE)['concluidos'] == 5
    assert TarefaFiscalKit.query.filter_by(concluido_em=None).count() == 2
    assert kits_fiscal.processar_pendentes(base=BASE)['concluidos'] == 2
    assert tiny_falso.incluir.call_count == 7


def test_falha_e_retry_ficam_persistidos_sem_repetir_antes_do_prazo(owner_user, tiny_falso):
    _, pedidos = _grupo(owner_user, quantidade=1)
    tiny_falso.incluir.side_effect = None
    tiny_falso.incluir.return_value = {'ok': False, 'erro': 'Tiny indisponível'}
    assert kits_fiscal.processar_pendentes(base=BASE)['pendentes'] == 1
    tarefa = db.session.get(TarefaFiscalKit, pedidos[0].id)
    assert tarefa.concluido_em is None and tarefa.tentativas == 1
    assert 'Tiny indisponível' in tarefa.erro
    assert tarefa.proxima_tentativa_em == BASE + timedelta(minutes=5)
    assert kits_fiscal.processar_pendentes(base=BASE + timedelta(minutes=4))['processados'] == 0
    tiny_falso.incluir.return_value = {'ok': True, 'id': 'nf-recuperada'}
    assert kits_fiscal.processar_pendentes(base=BASE + timedelta(minutes=5))['concluidos'] == 1
    assert tarefa.tentativas == 2 and tarefa.erro is None
    assert pedidos[0].tiny_nota_fiscal_id == 'nf-recuperada'


def test_excecao_na_api_conserva_pendencia_e_proximo_pedido_avanca(owner_user, tiny_falso):
    _, pedidos = _grupo(owner_user)
    tiny_falso.incluir.side_effect = [RuntimeError('queda simulada'), {'ok': True, 'id': 'nf-2'}]
    resumo = kits_fiscal.processar_pendentes(base=BASE)
    assert resumo['pendentes'] == 1 and resumo['concluidos'] == 1
    assert db.session.get(TarefaFiscalKit, pedidos[0].id).concluido_em is None
    assert db.session.get(TarefaFiscalKit, pedidos[0].id).erro
    assert db.session.get(TarefaFiscalKit, pedidos[1].id).concluido_em == BASE


def test_retry_reutiliza_nota_criada_quando_autorizacao_falhou(owner_user, tiny_falso):
    _, pedidos = _grupo(owner_user, quantidade=1)
    tiny_falso.emitir.return_value = {'ok': False, 'erro': 'Autorização pendente'}
    assert kits_fiscal.processar_pendentes(base=BASE)['pendentes'] == 1
    nota_id = pedidos[0].tiny_nota_fiscal_id
    assert nota_id
    tiny_falso.emitir.return_value = {'ok': True, 'status': 'autorizada'}
    assert kits_fiscal.processar_pendentes(base=BASE + timedelta(minutes=5))['concluidos'] == 1
    tiny_falso.incluir.assert_called_once()
    assert pedidos[0].tiny_nota_fiscal_id == nota_id


def test_nf_emitida_manualmente_conclui_fila_sem_nova_nota(owner_user, tiny_falso):
    _, pedidos = _grupo(owner_user, quantidade=1)
    assert tiny_nf.emitir_nf(pedidos[0])['ok']
    assert kits_fiscal.processar_pendentes(base=BASE)['concluidos'] == 1
    tiny_falso.incluir.assert_called_once()
    tiny_falso.emitir.assert_called_once()


def test_cancelados_sao_encerrados_sem_emitir(owner_user, tiny_falso):
    _, pedidos = _grupo(owner_user, quantidade=1)
    pedidos[0].status = 'cancelado'
    db.session.commit()
    assert kits_fiscal.processar_pendentes(base=BASE)['ignorados'] == 1
    assert db.session.get(TarefaFiscalKit, pedidos[0].id).concluido_em == BASE
    tiny_falso.incluir.assert_not_called()


@pytest.mark.parametrize('status', ['pago', 'em_preparo', 'a_caminho', 'entregue'])
def test_kit_pago_pode_emitir_apos_avanco_operacional(owner_user, tiny_falso, status):
    _, pedidos = _grupo(owner_user, quantidade=1)
    pedidos[0].status = status
    db.session.commit()
    assert tiny_nf.emitir_nf(pedidos[0])['ok']
    tiny_falso.incluir.assert_called_once()


@pytest.mark.parametrize('status,pago_em', [('aguardando_pagamento', None),
                                          ('cancelado', BASE), ('em_preparo', None)])
def test_kit_sem_pagamento_ou_cancelado_nao_emite(owner_user, tiny_falso, status, pago_em):
    _, pedidos = _grupo(owner_user, quantidade=1)
    pedidos[0].status, pedidos[0].pago_em = status, pago_em
    db.session.commit()
    assert not tiny_nf.emitir_nf(pedidos[0])['ok']
    tiny_falso.incluir.assert_not_called()


def test_estado_avancado_de_pedido_normal_pago_pode_emitir(owner_user, tiny_falso):
    _, pedidos = _grupo(owner_user, quantidade=1)
    db.session.delete(db.session.get(EntregaKit, pedidos[0].id))
    pedidos[0].status = 'entregue'
    db.session.commit()
    assert tiny_nf.emitir_nf(pedidos[0])['ok']
    tiny_falso.incluir.assert_called_once()


def test_refazer_nf_ja_autorizada_nao_gera_segunda_nota(owner_user, tiny_falso):
    _, pedidos = _grupo(owner_user, quantidade=1)
    assert tiny_nf.emitir_nf(pedidos[0])['ok']
    assert tiny_nf.emitir_nf(pedidos[0], recriar=True)['ok']
    tiny_falso.incluir.assert_called_once()


def test_refazer_exige_rejeicao_confirmada_da_nota_existente(owner_user, tiny_falso):
    _, pedidos = _grupo(owner_user, quantidade=1)
    pedidos[0].tiny_nota_fiscal_id = 'rascunho'
    db.session.commit()
    assert not tiny_nf.emitir_nf(pedidos[0], recriar=True)['ok']
    tiny_falso.incluir.assert_not_called()
    tiny_falso.obter.return_value = {'situacao': 'rejeitada'}
    assert tiny_nf.emitir_nf(pedidos[0], recriar=True)['ok']
    tiny_falso.incluir.assert_called_once()


def test_postgres_lock_e_unlock_usam_mesma_conexao_inclusive_commit(owner_user, monkeypatch):
    _, pedidos = _grupo(owner_user, quantidade=1)
    pedido_id = pedidos[0].id
    engine = db.engine
    conexao = Mock()
    conexao.execute.return_value.scalar.return_value = True
    conexao.close = Mock()
    # Esta sonda exercita apenas o contexto da trava com a conexão dedicada.
    # Não pretende substituir a validação concorrente em PostgreSQL real.
    monkeypatch.setattr(engine.dialect, 'name', 'postgresql')
    monkeypatch.setattr(engine, 'connect', Mock(return_value=conexao))
    with tiny_nf._trava_nf_kit(pedido_id) as adquirido:
        assert adquirido
        assert conexao.execute.call_count == 1
        assert 'pg_try_advisory_lock' in str(conexao.execute.call_args.args[0])
        assert conexao.execute.call_args.args[1] == {'namespace': 7766, 'pedido_id': pedido_id}
        conexao.close.assert_not_called()
    assert conexao.execute.call_count == 2
    assert 'pg_advisory_unlock' in str(conexao.execute.call_args.args[0])
    conexao.close.assert_called_once()


def test_concorrencia_admin_e_fila_em_sqlite_cria_uma_nota(app, owner_user, monkeypatch, tiny_falso):
    _, pedidos = _grupo(owner_user, quantidade=1)
    pedido_id = pedidos[0].id
    entrou, liberar, segundo_iniciado = threading.Event(), threading.Event(), threading.Event()
    resultados, erros = [], []
    incluir_original = tiny_falso.incluir.side_effect

    def incluir(payload, **kwargs):
        entrou.set()
        if not liberar.wait(timeout=5):
            raise RuntimeError('Teste não liberou a emissão')
        return incluir_original(payload)
    tiny_falso.incluir.side_effect = incluir

    def emitir(segundo=False):
        try:
            with app.app_context():
                pedido = db.session.get(PedidoOnline, pedido_id)
                if segundo:
                    segundo_iniciado.set()
                resultados.append(tiny_nf.emitir_nf(pedido))
        except Exception as exc:
            erros.append(exc)

    primeiro = threading.Thread(target=emitir)
    segundo = threading.Thread(target=emitir, args=(True,))
    primeiro.start()
    try:
        assert entrou.wait(timeout=5)
        segundo.start()
        assert segundo_iniciado.wait(timeout=5)
    finally:
        liberar.set()
        primeiro.join(timeout=5)
        if segundo.ident:
            segundo.join(timeout=5)
    assert not primeiro.is_alive() and not segundo.is_alive()
    assert not erros
    assert len(resultados) == 2 and all(r['ok'] for r in resultados)
    tiny_falso.incluir.assert_called_once()
    tiny_falso.emitir.assert_called_once()


def test_cron_executa_fila_com_chave_propria(app, monkeypatch):
    lock = Mock()
    monkeypatch.setattr(seru_cron, '_com_lock', lock)
    seru_cron._run_kits_fiscal(app)
    lock.assert_called_once_with(7765, kits_fiscal.processar_pendentes, 'fila fiscal do site')


def test_falha_email_repetida_reenvia_so_danfe_sem_reemitir_nota(owner_user, tiny_falso):
    _, pedidos = _grupo(owner_user, quantidade=1)
    tiny_falso.email.return_value = {'ok': False, 'erro': 'Servidor de e-mail indisponível'}
    assert kits_fiscal.processar_pendentes(base=BASE)['pendentes'] == 1
    tarefa = db.session.get(TarefaFiscalKit, pedidos[0].id)
    assert pedidos[0].nf_emitida_em and pedidos[0].tiny_nota_fiscal_id
    assert not tarefa.concluido_em and not tarefa.email_enviado_em
    assert 'NF emitida' in tarefa.erro and 'e-mail indisponível' in tarefa.erro
    tiny_falso.email.return_value = {'ok': True}
    assert kits_fiscal.processar_pendentes(base=BASE + timedelta(minutes=5))['concluidos'] == 1
    tiny_falso.incluir.assert_called_once()
    tiny_falso.emitir.assert_called_once()
    assert tiny_falso.email.call_count == 2
    assert tarefa.email_enviado_em == BASE + timedelta(minutes=5)
    assert tarefa.erro is None


def test_email_desconfigurado_preserva_pendencia_sem_reemitir_nf(owner_user, tiny_falso, monkeypatch):
    _, pedidos = _grupo(owner_user, quantidade=1)
    monkeypatch.setattr('app.services.email.disponivel', lambda: False)
    assert kits_fiscal.processar_pendentes(base=BASE)['pendentes'] == 1
    tarefa = db.session.get(TarefaFiscalKit, pedidos[0].id)
    assert tarefa.concluido_em is None and tarefa.email_enviado_em is None
    tiny_falso.email.assert_not_called()
    monkeypatch.setattr('app.services.email.disponivel', lambda: True)
    assert kits_fiscal.processar_pendentes(base=BASE + timedelta(minutes=5))['concluidos'] == 1
    tiny_falso.incluir.assert_called_once()
    tiny_falso.emitir.assert_called_once()
    tiny_falso.email.assert_called_once()


def test_excecao_email_fica_pendente_sem_desfazer_nf(owner_user, tiny_falso):
    _, pedidos = _grupo(owner_user, quantidade=1)
    tiny_falso.email.side_effect = RuntimeError('Falha simulada')
    assert kits_fiscal.processar_pendentes(base=BASE)['pendentes'] == 1
    assert pedidos[0].nf_emitida_em and pedidos[0].tiny_nota_fiscal_id
    assert db.session.get(TarefaFiscalKit, pedidos[0].id).concluido_em is None
