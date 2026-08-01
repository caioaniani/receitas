"""Turno VAZIO do modelo + atraso de pedido de app (auditor 26/07/2026).

Contexto real (sonda /api/claude/vigia-vereditos, 12-26/07): 'resposta vazia'
era o motivo de handoff MAIS FREQUENTE — 3 dos 16 handoffs do periodo, TODOS
em fechamento de conversa:

  conv 842  "Obrigada. Esclareceu"                  -> vigia: handoff desnecessario
  conv 897  "Nao, muito obrigada !"                 -> vigia: handoff desnecessario
  conv 918  "Eu acabei cancelando / ...             -> venda PERDIDA, gravidade alta
             nao chegava nunca / Obrigada"

Causa: o prompt manda "NAO responda nada" no fechamento; o modelo obedece
metade (silencio) e esquece a outra (chamar `encerrar_conversa`), devolvendo
turno vazio. O codigo tratava vazio como FALHA e transferia — enchia a fila
humana e ainda contava como "handoff preguicoso" na metrica do auditor.

E o caso 918 (Rappi atrasado com o entregador JA NO BALCAO): o enforcement
anti-handoff-preguicoso bloqueava a transferencia por atraso, mas NENHUMA tool
consulta pedido de marketplace — beco sem saida que virou "veja no app".
"""
from types import SimpleNamespace

import pytest


def _resp_vazia():
    """Resposta da API sem tool e com texto em branco (o turno vazio real)."""
    return SimpleNamespace(
        content=[SimpleNamespace(type='text', text='   ')],
        stop_reason='end_turn')


@pytest.fixture
def modelo_mudo(monkeypatch):
    """Anthropic devolvendo SEMPRE turno vazio. Conta as chamadas."""
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-x')
    chamadas = {'n': 0}

    class FakeClient:
        def __init__(self, **kw):
            pass

        class messages:
            @staticmethod
            def create(**kw):
                chamadas['n'] += 1
                return _resp_vazia()

    monkeypatch.setattr('anthropic.Anthropic', FakeClient)
    return chamadas


# ── (b) fechamento sem pendencia -> SILENCIO (decisao do dono 16/06) ──

@pytest.mark.parametrize('msg', [
    'Obrigada. Esclareceu',        # conv 842
    'Não, muito obrigada !',       # conv 897
])
def test_vazio_em_fechamento_encerra_em_silencio(app, modelo_mudo, msg):
    """Fechamento que o detector estrito NAO pega (tem texto extra): o turno
    vazio do modelo E o silencio pedido pelo dono — encerra, nao transfere."""
    from app.services import chatbot
    with app.app_context():
        out = chatbot.responder([
            {'role': 'assistant', 'content': 'Qualquer coisa é só chamar.'},
            {'role': 'user', 'content': msg},
        ])
    assert out['acao'] == 'encerrar'
    assert out['texto'] == ''            # silencio absoluto
    assert modelo_mudo['n'] == 1         # passou pelo modelo (nao e a Camada 1)


def test_fechamento_com_texto_extra_nao_casa_o_detector_estrito():
    """Trava o pressuposto do teste acima: essas frases NAO sao fechamento
    puro (`_e_fechamento` e ancorado nas duas pontas). Se um dia passarem a
    casar, a Camada 1 resolve antes e os testes acima mudam de caminho."""
    from app.services.chatbot_vigia import _e_fechamento
    assert not _e_fechamento('Obrigada. Esclareceu')
    assert not _e_fechamento('Não, muito obrigada !')
    assert _e_fechamento('Obrigada')     # esse sim e puro


# ── (a) fechamento COM reclamacao -> handoff com mensagem DE VERDADE ──

def test_vazio_com_reclamacao_vira_handoff_com_mensagem_real(app, modelo_mudo):
    """Caso Gabriela (918): cancelou porque o pedido nao chegou. Silenciar
    seria o pior desfecho — vai pra fila humana, mas com mensagem real, nao
    com o 'Já te passo para um atendente.' acidental."""
    from app.services import chatbot
    # Trava o relógio "dentro do horário de atendimento": rodada à noite, a
    # suíte ganhava o prefixo "Estamos fora do nosso horário..." no texto e
    # este teste ficava VERMELHO das 20h às 7h BRT — com Wait-for-CI, isso
    # bloqueava TODO deploy noturno (achado 31/07/2026).
    _orig = chatbot._fora_horario_chat
    chatbot._fora_horario_chat = lambda: False
    try:
        with app.app_context():
            out = chatbot.responder([
                {'role': 'assistant', 'content': 'Fico à disposição.'},
                {'role': 'user',
                 'content': 'Eu acabei cancelando\nAs visitas estavam '
                            'esperando e não chegava nunca\nObrigada'},
            ])
    finally:
        chatbot._fora_horario_chat = _orig
    assert out['acao'] == 'handoff'
    assert out['motivo'] == 'resposta vazia (reclamacao)'
    # `in`, nao `==`: FORA do horario de atendimento (07:00-20:00) o bot
    # PREFIXA um aviso no texto. Assertar igualdade fazia o teste passar de
    # dia e quebrar de noite — e, com Wait-for-CI, um teste hora-dependente
    # TRAVA TODO DEPLOY (aconteceu em 31/07/2026, CI das 20:11).
    assert chatbot._TEXTO_VAZIO_RECLAMACAO in out['texto']
    assert 'Já te passo para um atendente.' not in out['texto']
    assert 'Sinto muito' in out['texto']


def test_sinais_reclamacao_cobre_o_texto_real_da_conv_918():
    """O detector nao reconhecia 'nao chegava' (so 'nao chegou') nem
    'cancelei/cancelando' (so 'cancelar meu pedido') — por isso uma venda
    perdida era lida como fechamento banal."""
    from app.services.chatbot_vigia import _SINAIS_RECLAMACAO
    for t in ('as visitas estavam esperando e não chegava nunca',
              'eu acabei cancelando',
              'cancelei',
              'não chegaram os pães',
              'nunca chegou'):
        assert _SINAIS_RECLAMACAO.search(t), t
    # nao pode virar reclamacao qualquer agradecimento
    assert not _SINAIS_RECLAMACAO.search('obrigada, esclareceu')


# ── (c) pergunta pendente -> cliente nunca no vacuo (regra P1) ──

def test_vazio_com_pergunta_pendente_nao_deixa_cliente_no_vacuo(
        app, modelo_mudo):
    """O bot tinha PERGUNTA aberta e o modelo emudeceu: nao da pra encerrar
    em silencio — o cliente estava esperando resposta."""
    from app.services import chatbot
    # Relógio travado "dentro do horário" — mesmo motivo do teste da
    # reclamação acima (suíte noturna ganhava o prefixo de fora-de-horário).
    _orig = chatbot._fora_horario_chat
    chatbot._fora_horario_chat = lambda: False
    try:
        with app.app_context():
            out = chatbot.responder([
                {'role': 'assistant', 'content': 'Qual o seu CPF?'},
                {'role': 'user', 'content': '123'},
            ])
    finally:
        chatbot._fora_horario_chat = _orig
    assert out['acao'] == 'handoff'
    # `in` pelo mesmo motivo do teste acima (prefixo de fora-de-horario).
    assert chatbot.FALLBACK_TEXTO in out['texto']
    assert out['texto']                  # nunca vazio


def test_handoff_de_madrugada_avisa_o_horario(app, modelo_mudo):
    """A outra ponta, travada de propósito: FORA do horário o texto ganha o
    aviso "07:00 às 20:00" — o cliente não pode esperar atendente às 23h."""
    from app.services import chatbot
    _orig = chatbot._fora_horario_chat
    chatbot._fora_horario_chat = lambda: True
    try:
        with app.app_context():
            out = chatbot.responder([
                {'role': 'assistant', 'content': 'Qual o seu CPF?'},
                {'role': 'user', 'content': '123'},
            ])
    finally:
        chatbot._fora_horario_chat = _orig
    assert out['acao'] == 'handoff'
    assert '07:00' in out['texto']
    assert out['texto'].endswith(chatbot.FALLBACK_TEXTO)


# ── enforcement: atraso passa; "10 pessoas" NAO e pedido de humano ──

def test_handoff_excecao_cobre_atraso_e_marketplace():
    """Atraso/entrega parada transfere DIRETO: nenhuma tool consulta pedido
    de Rappi/iFood, entao exigir consulta antes era beco sem saida. O vigia
    ja tratava atraso como handoff legitimo — o enforcement divergia."""
    from app.services.chatbot import _handoff_excecao
    assert _handoff_excecao(
        {'motivo': 'pedido atrasado no Rappi, motorista já no local'})
    assert _handoff_excecao({'motivo': 'entrega atrasou, cliente esperando'})
    assert _handoff_excecao({'motivo': 'pedido do iFood parado'})


def test_pessoas_no_plural_nao_fura_o_enforcement():
    """'cesta para 10 pessoas' e motivo de VENDA, nao pedido de humano — o
    \\w* do grupo casava 'pessoas' e anulava o enforcement justamente ali."""
    from app.services.chatbot import _handoff_excecao
    assert not _handoff_excecao(
        {'motivo': 'cliente quer cesta para 10 pessoas'})
    assert not _handoff_excecao({'motivo': 'sugestão para 20 pessoas'})
    # o caso legitimo (singular) continua passando
    assert _handoff_excecao({'motivo': 'cliente quer falar com uma pessoa'})


def test_excecoes_antigas_continuam_valendo():
    """Regressao: a reescrita do regex nao pode derrubar as excecoes que ja
    existiam (alergia, humano, atendente, estorno, reembolso, cancelamento)."""
    from app.services.chatbot import _handoff_excecao
    for motivo in ('cliente alérgico a amendoim', 'reclamação grave',
                   'pediu humano', 'quer atendente', 'pedido de estorno',
                   'reembolso solicitado', 'cancelamento do pedido'):
        assert _handoff_excecao({'motivo': motivo}), motivo
    assert not _handoff_excecao({'motivo': 'dúvida sobre sabor do pão'})


# ── prompt: pedido de app com entregador no local ──

def test_prompt_ensina_pedido_de_app_com_entregador_no_local():
    from app.services.chatbot_prompt import PROMPT
    assert 'Rappi' in PROMPT and 'iFood' in PROMPT
    assert 'ENTREGADOR JÁ ESTÁ' in PROMPT
    # a instrucao central: o gargalo e NOSSO, nao do app
    assert 'gargalo é NOSSO' in PROMPT
