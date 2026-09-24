"""Contrato público: acolher e encaminhar sem conduzir vendas ou pós-venda."""
from unittest.mock import patch

import pytest

from app.services import atendimento_restrito as atendimento


def _mensagem(texto, **extras):
    return {'role': 'user', 'content': texto, **extras}


@pytest.fixture
def sem_motor(monkeypatch):
    def proibido(*args, **kwargs):
        pytest.fail('Atendimento restrito chamou modelo ou ferramenta de negócio')
    monkeypatch.setattr('anthropic.Anthropic', proibido)
    monkeypatch.setattr('app.services.chatbot._executar_tool', proibido)
    monkeypatch.setattr('app.services.bot_tools.consultar_pedido', proibido)
    monkeypatch.setattr('app.services.bot_tools.consultar_produtos', proibido)
    monkeypatch.setattr('app.services.bot_tools.gerar_link_carrinho', proibido)


@pytest.mark.parametrize('texto', [
    'Quero 15 lanches e 15 croissants',
    'Pode acrescentar 2 croissants ao meu pedido?',
    'O complemento é apartamento 72',
    'A cartinha é para minha mãe',
    'Qual o valor de 15 lanches?',
    'Cadê meu pedido?',
    'O pedido aparece entregue, mas não recebi',
    'Não quero ir ao site, quero comprar por aqui',
    'Me manda o site e separa 15 croissants',
    'Qual o site? Quero 15 lanches',
    'Oi, quero uma cesta',
    'Quero mudar o horário do pedido',
    'Tem croissant hoje?',
    'Gostaria de falar com alguém',
    'sim', 'pode ser', 'isso', '',
])
def test_compras_alteracoes_e_ambiguidade_encaminham(app, sem_motor, texto):
    out = atendimento.responder([_mensagem(texto)])
    assert out['acao'] == 'handoff'
    assert out['tools_usadas'] == []
    assert out['politica_atendimento'] == atendimento.POLITICA
    assert 'equipe' in out['texto']
    assert 'http' not in out['texto']
    assert 'entregue' not in out['texto']


@pytest.mark.parametrize('ultima', ['oi', 'obrigada', 'qual o site?'])
def test_historico_de_pedido_nao_e_apagado_por_faq(app, sem_motor, ultima):
    hist = [_mensagem('Quero complementar meu pedido'), _mensagem(ultima)]
    assert atendimento.responder(hist)['acao'] == 'handoff'


def test_confirmacao_antiga_do_bot_nao_autoriza_novo_status(app, sem_motor):
    hist = [{'role': 'assistant', 'content': 'Seu pedido já foi entregue.'},
            _mensagem('Bom dia')]
    assert atendimento.responder(hist)['acao'] == 'handoff'


def test_marcador_persistido_de_handoff_preserva_continuidade(app, sem_motor):
    hist = [_mensagem('Qual o horário?'),
            {'role': 'assistant', 'content': 'Nossa equipe continua com você.',
             'handoff_em': '2026-09-24T10:00:00'},
            _mensagem('Oi')]
    assert atendimento.responder(hist)['acao'] == 'handoff'


def test_contexto_herdado_nao_confirma_pedido_nem_prende_nova_saudacao(app, sem_motor):
    hist = [_mensagem('Meu pedido chegou?', herdada=True),
            {'role': 'assistant', 'content': 'Pedido entregue', 'herdada': True},
            _mensagem('Bom dia')]
    out = atendimento.responder(hist)
    assert out['acao'] == 'responder'
    assert 'pedido' not in out['texto'].lower()


@pytest.mark.parametrize('anexo', [
    {'imagens': [{'mimetype': 'image/jpeg'}]},
    {'attachments': [{'file_type': 'image'}]},
    {'audio': {'url': 'https://example.org/audio'}},
])
def test_anexo_com_saudacao_vai_para_equipe(app, sem_motor, anexo):
    assert atendimento.responder([_mensagem('oi', **anexo)])['acao'] == 'handoff'


def test_conteudo_estruturado_vai_para_equipe(app, sem_motor):
    out = atendimento.responder([_mensagem([{'type': 'text', 'text': 'oi'}])])
    assert out['acao'] == 'handoff'


@pytest.mark.parametrize('texto', ['Oi!', 'Olá', 'Bom dia', 'Obrigada'])
def test_conversa_simples_nunca_e_encerrada(app, sem_motor, texto):
    out = atendimento.responder([_mensagem(texto)])
    assert out['acao'] == 'responder'
    assert out['politica_atendimento'] == atendimento.POLITICA


@pytest.mark.parametrize('texto', ['Qual o site?', 'Me manda o cardápio, por favor',
                                 'Gostaria de ver o cardápio'])
def test_link_apenas_solicitado_isoladamente(app, sem_motor, texto):
    app.config['LOJA_BASE_URL'] = 'https://opao.online'
    out = atendimento.responder([_mensagem(texto)])
    assert out['acao'] == 'responder'
    assert out['texto'] == 'Nosso site: https://opao.online'
    assert 'finalize' not in out['texto']


@pytest.mark.parametrize('url', ['', 'http://opao.online', 'https://u:p@opao.online'])
def test_link_sem_config_valida_encaminha(app, sem_motor, url):
    app.config['LOJA_BASE_URL'] = url
    assert atendimento.responder([_mensagem('qual o site')])['acao'] == 'handoff'


def test_enderecos_vem_do_cadastro_ativo_sem_industria(app, sem_motor):
    from app.extensions import db
    from app.models import Loja
    db.session.add_all([
        Loja(nome='Loja Nebraska', endereco='Rua Nebraska, 294', ativa=True),
        Loja(nome='Indústria', endereco='Endereço da produção', ativa=True),
        Loja(nome='Encerrada', endereco='Endereço antigo', ativa=False),
    ])
    db.session.commit()
    out = atendimento.responder([_mensagem('Quais os endereços das lojas?')])
    assert out['acao'] == 'responder'
    assert out['texto'] == 'Loja Nebraska: Rua Nebraska, 294'


def test_endereco_de_unidade_precisa_bater_com_cadastro(app, sem_motor):
    from app.extensions import db
    from app.models import Loja
    db.session.add(Loja(nome='Loja Nebraska', endereco='Rua Nebraska, 294', ativa=True))
    db.session.commit()
    assert atendimento.responder([_mensagem('Onde fica a loja Nebraska?')])['acao'] == 'responder'
    assert atendimento.responder([_mensagem('Onde fica a loja Brooklin?')])['acao'] == 'handoff'


def test_endereco_incompleto_e_horario_sem_fonte_encaminham(app, sem_motor):
    from app.extensions import db
    from app.models import Loja
    db.session.add(Loja(nome='Loja Nebraska', ativa=True, dias_funcionamento='0123456'))
    db.session.commit()
    assert atendimento.responder([_mensagem('Qual o endereço?')])['acao'] == 'handoff'
    assert atendimento.responder([_mensagem('Qual o horário de funcionamento?')])['acao'] == 'handoff'


def test_falha_na_fonte_de_endereco_encaminha(app, sem_motor):
    with patch.object(atendimento, '_enderecos', side_effect=RuntimeError('offline')):
        assert atendimento.responder([_mensagem('Qual o endereço?')])['acao'] == 'handoff'
