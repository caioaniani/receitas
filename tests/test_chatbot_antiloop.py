"""Anti-loop com bot externo (03/07/2026 — caso gov.br).

Um bot externo (+55 61 3207-3332, gov.br) entrou em ciclo com o nosso:
mesma mensagem em loop, o bot respondendo sempre — 6 alertas ALTA do vigia
sem nenhum cliente real, gastando Claude a cada turno. Duas defesas:

1. `chatbot._e_loop_repetido`: 3 mensagens do cliente byte-idênticas
   (normalizadas) intercaladas com respostas do bot → encerra em silêncio
   (resolved), SEM gastar Claude; o vigia é pulado nesse encerramento.
2. Lista de números ignorados (env CHATBOT_NUMEROS_IGNORADOS, CSV): o
   webhook resolve a conversa em silêncio antes de qualquer processamento.
"""
from unittest.mock import patch

from app.services.chatbot import _e_loop_repetido, responder


class _SyncThread:
    def __init__(self, target=None, daemon=None, **kw):
        self._target = target

    def start(self):
        if self._target:
            self._target()


def _hist_loop(n=3, texto='Escolha uma opção: 1-Serviços 2-Sair'):
    hist = []
    for i in range(n):
        hist.append({'role': 'user', 'content': texto})
        if i < n - 1:
            hist.append({'role': 'assistant', 'content': 'Como posso ajudar?'})
    return hist


# ── _e_loop_repetido (unit) ───────────────────────────────────────────────

def test_loop_3_identicas_intercaladas_detecta():
    assert _e_loop_repetido(_hist_loop(3)) is True


def test_duas_identicas_nao_detecta():
    assert _e_loop_repetido(_hist_loop(2)) is False


def test_textos_diferentes_nao_detecta():
    hist = [
        {'role': 'user', 'content': 'oi'},
        {'role': 'assistant', 'content': 'olá!'},
        {'role': 'user', 'content': 'quero pão'},
        {'role': 'assistant', 'content': 'temos!'},
        {'role': 'user', 'content': 'qual o preço?'},
    ]
    assert _e_loop_repetido(hist) is False


def test_rajada_sem_resposta_do_bot_nao_detecta():
    """3 msgs iguais SEGUIDAS (sem assistant entre elas) é rajada/insistência
    humana que o debounce normalmente colapsa — não é loop bot-a-bot."""
    hist = [{'role': 'user', 'content': 'alô'}] * 3
    assert _e_loop_repetido(hist) is False


def test_normaliza_espacos_e_caixa():
    hist = [
        {'role': 'user', 'content': 'Menu  Principal'},
        {'role': 'assistant', 'content': 'oi?'},
        {'role': 'user', 'content': 'menu principal'},
        {'role': 'assistant', 'content': 'não entendi'},
        {'role': 'user', 'content': ' MENU   PRINCIPAL '},
    ]
    assert _e_loop_repetido(hist) is True


# ── responder encerra sem gastar Claude ───────────────────────────────────

def test_responder_encerra_loop_sem_chamar_claude(app):
    """Guard determinístico ANTES do Claude: em loop, devolve 'encerrar' sem
    texto (crm/routes marca resolved em silêncio)."""
    res = responder(_hist_loop(3))
    assert res['acao'] == 'encerrar'
    assert res['texto'] == ''
    assert 'loop' in res['motivo']


# ── webhook: número ignorado e vigia pulado ───────────────────────────────

def _post(client, **payload_over):
    payload = {'event': 'message_created', 'message_type': 'incoming',
               'conversation': {'id': 7, 'status': 'pending'}, 'content': 'oi'}
    payload.update(payload_over)
    return client.post('/crm/bot?k=seg', json=payload)


def test_webhook_numero_ignorado_resolve_em_silencio(app):
    app.config['CHATWOOT_BOT_SECRET'] = 'seg'
    app.config['CHATBOT_NUMEROS_IGNORADOS'] = '+55 61 3207-3332, 5511999990000'
    client = app.test_client()
    with patch('app.services.chatbot.responder') as resp, \
         patch('app.services.chatwoot.definir_status',
               return_value={'ok': True}) as status:
        r = _post(client, sender={'phone_number': '+556132073332'},
                  id=901)
    assert r.status_code == 200
    assert r.get_json()['ignorado'] == 'numero-ignorado'
    resp.assert_not_called()                     # zero Claude
    status.assert_called_once_with(7, 'resolved')


def test_webhook_numero_normal_processa(app):
    app.config['CHATWOOT_BOT_SECRET'] = 'seg'
    app.config['CHATBOT_NUMEROS_IGNORADOS'] = '+556132073332'
    client = app.test_client()
    with patch('threading.Thread', _SyncThread), \
         patch('app.services.chatbot.responder',
               return_value={'acao': 'responder', 'texto': 'olá!'}) as resp, \
         patch('app.services.chatwoot.enviar_mensagem',
               return_value={'ok': True}), \
         patch('app.services.chatbot_vigia.disponivel', return_value=False):
        r = _post(client, sender={'phone_number': '+5511988887777'}, id=902)
    assert r.status_code == 200
    resp.assert_called_once()


def test_vigia_pulado_no_encerramento_por_loop(app):
    """O vigia alertando no loop era exatamente o ruído (6 ALTAs sem cliente
    real) — encerramento por loop não passa pelo vigia."""
    app.config['CHATWOOT_BOT_SECRET'] = 'seg'
    client = app.test_client()
    with patch('threading.Thread', _SyncThread), \
         patch('app.services.chatbot.responder',
               return_value={'acao': 'encerrar', 'texto': '',
                             'motivo': 'loop de mensagens repetidas'}), \
         patch('app.services.chatwoot.definir_status',
               return_value={'ok': True}), \
         patch('app.services.chatbot_vigia.disponivel',
               return_value=True) as disp, \
         patch('app.services.chatbot_vigia.avaliar') as avaliar:
        r = _post(client, id=903)
    assert r.status_code == 200
    avaliar.assert_not_called()
    _ = disp
