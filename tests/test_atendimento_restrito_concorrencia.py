"""O monitor não pode encerrar/apagar uma espera criada durante o HTTP."""
from datetime import timedelta
from threading import Event, Thread
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from app.extensions import db
from app.models import EsperaAtendimento
from app.services import atendimento_pendente as monitor
from app.utils import agora


def _criar(cid='81', **valores):
    dados = dict(conversa_id=cid, inicio_em=agora() - timedelta(hours=1),
                 estado='aguardando', mensagem='Mensagem antiga', grave=False)
    dados.update(valores)
    db.session.add(EsperaAtendimento(**dados))
    db.session.commit()


def _outro_worker(cid='81', **valores):
    # Sessão realmente independente: não atualiza o objeto no identity map
    # do monitor, reproduzindo webhook em outro processo gunicorn.
    with Session(db.engine) as sessao:
        row = sessao.get(EsperaAtendimento, cid)
        if row is None:
            row = EsperaAtendimento(conversa_id=cid, inicio_em=agora(),
                                    estado='aguardando', grave=False)
            sessao.add(row)
        for campo, valor in valores.items():
            setattr(row, campo, valor)
        sessao.commit()


def _atual(cid='81'):
    db.session.expire_all()
    return db.session.get(EsperaAtendimento, cid)


@pytest.mark.parametrize('novo_episodio', [False, True])
def test_resolved_antigo_nao_apaga_mensagem_recebida_durante_consulta(app, novo_episodio):
    _criar()
    esperado = {'mensagem': 'Quero complementar o pedido', 'estado': 'aguardando',
                'proximo_aviso_em': agora()}
    if novo_episodio:
        esperado['inicio_em'] = agora()

    def status_antigo(cid):
        _outro_worker(cid, **esperado)
        return {'status': 'resolved'}

    with patch('app.services.chatwoot.listar_conversas_paradas', return_value=[]), \
            patch('app.services.chatwoot.consultar_conversa', side_effect=status_antigo), \
            patch('app.services.presenca_humana.encerrar') as encerrar:
        assert monitor.candidatos() == []
    row = _atual()
    assert row.estado == 'aguardando'
    assert row.resolvido_em is None
    assert row.mensagem == esperado['mensagem']
    assert row.proximo_aviso_em == esperado['proximo_aviso_em']
    encerrar.assert_not_called()


@pytest.mark.parametrize('existia', [False, True])
def test_historico_sem_cliente_antigo_nao_apaga_encaminhamento_novo(app, existia):
    if existia:
        _criar(mensagem='[ABANDONO 15min]')
    with patch('app.services.chatwoot.listar_conversas_paradas',
               return_value=[{'id': 81, 'minutos_paradas': 15}]):
        candidata = monitor.candidatos()[0]
    _outro_worker(mensagem='Preciso de 15 lanches', proximo_aviso_em=agora())
    historico_antigo = [{'role': 'assistant', 'content': 'Olá!', 'humano': True}]
    assert monitor.preparar(candidata, historico_antigo) is None
    row = _atual()
    assert row.estado == 'aguardando'
    assert row.mensagem == 'Preciso de 15 lanches'
    assert row.resolvido_em is None


@pytest.mark.parametrize('hist', [
    [{'role': 'assistant', 'content': 'Olá!', 'humano': True}],
    [{'role': 'user', 'content': 'Quero uma cesta'},
     {'role': 'assistant', 'content': 'Já estou atendendo.', 'humano': True}],
    [{'role': 'user', 'content': 'Quero uma cesta'}],
])
def test_cas_impede_gravacao_quando_espera_muda_durante_preparo(app, hist):
    _criar()
    salvar = monitor._salvar_se_inalterada

    def salvar_com_concorrencia(row, observada):
        _outro_worker(mensagem='Mensagem mais nova', grave=True,
                      proximo_aviso_em=agora())
        return salvar(row, observada)

    with patch.object(monitor, '_salvar_se_inalterada', side_effect=salvar_com_concorrencia):
        assert monitor.preparar({'id': 81, 'minutos_paradas': 15}, hist) is None
    row = _atual()
    assert row.estado == 'aguardando'
    assert row.mensagem == 'Mensagem mais nova'
    assert row.grave is True
    assert row.resolvido_em is None


def test_insert_do_monitor_nao_substitui_espera_criada_por_outro_worker(app):
    salvar = monitor._salvar_se_inalterada

    def criar_antes(row, observada):
        _outro_worker(mensagem='Encaminhada pelo webhook', grave=True)
        return salvar(row, observada)

    with patch.object(monitor, '_salvar_se_inalterada', side_effect=criar_antes):
        assert monitor.preparar({'id': 81, 'minutos_paradas': 15},
                                [{'role': 'user', 'content': 'Mensagem antiga'}]) is None
    row = _atual()
    assert row.mensagem == 'Encaminhada pelo webhook'
    assert row.grave is True


def test_episodio_inalterado_ainda_e_resolvido_por_status_confirmado(app):
    _criar()
    with patch('app.services.chatwoot.listar_conversas_paradas', return_value=[]), \
            patch('app.services.chatwoot.consultar_conversa', return_value={'status': 'resolved'}), \
            patch('app.services.presenca_humana.encerrar'):
        assert monitor.candidatos() == []
    assert _atual().estado == 'resolvido'


def test_preparar_devolve_modelo_persistido_para_agendamento_do_monitor(app):
    _criar(proximo_aviso_em=agora())
    row = monitor.preparar({'id': 81, 'minutos_paradas': 15},
                           [{'role': 'user', 'content': 'Obrigada'}])
    assert row is not None
    prazo = agora() + timedelta(minutes=15)
    row.proximo_aviso_em = prazo
    db.session.commit()
    assert _atual().proximo_aviso_em == prazo


def test_mensagem_identica_aguarda_consulta_e_reabre_espera_depois(app):
    from app.blueprints.crm.routes import _lock_conv_cross_worker, _lock_para_conv
    from app.services import atendimento_humano

    texto = '??\n??\n??'
    _criar(mensagem=texto, proximo_aviso_em=agora())
    tentando, adquiriu = Event(), Event()
    erros = []

    def webhook():
        try:
            with app.app_context():
                tentando.set()
                with _lock_para_conv('81'), _lock_conv_cross_worker('81'):
                    adquiriu.set()
                    atendimento_humano.registrar_encaminhamento(
                        '81', [{'role': 'user', 'content': '??'}] * 4)
        except Exception as exc:
            erros.append(exc)

    worker = Thread(target=webhook, daemon=True)

    def status_antigo(cid):
        worker.start()
        assert tentando.wait(2)
        assert not adquiriu.is_set()  # HTTP e gravacao pertencem ao mesmo lock
        return {'status': 'resolved'}

    with patch('app.services.chatwoot.listar_conversas_paradas', return_value=[]), \
            patch('app.services.chatwoot.consultar_conversa', side_effect=status_antigo), \
            patch('app.services.presenca_humana.encerrar'):
        monitor.candidatos()
    worker.join(timeout=3)
    assert not worker.is_alive()
    assert not erros
    assert adquiriu.is_set()
    row = _atual()
    assert row.estado == 'aguardando'
    assert row.mensagem == texto
    assert row.resolvido_em is None


def test_nota_recebida_durante_consulta_nao_e_encerrada_pelo_status_antigo(app):
    from app.services import presenca_humana

    _criar()
    inicial = agora()
    relogio = {'agora': inicial}

    def status_antigo(cid):
        relogio['agora'] = inicial + timedelta(seconds=2)
        presenca_humana.registrar_nota_privada(
            cid, autor='Equipe', quando=inicial + timedelta(seconds=1))
        return {'status': 'resolved'}

    with patch.object(monitor, 'agora', side_effect=lambda: relogio['agora']), \
            patch.object(presenca_humana, 'agora', side_effect=lambda: relogio['agora']), \
            patch('app.services.chatwoot.listar_conversas_paradas', return_value=[]), \
            patch('app.services.chatwoot.consultar_conversa', side_effect=status_antigo):
        monitor.candidatos()
        assert presenca_humana.humano_presente('81') is True
