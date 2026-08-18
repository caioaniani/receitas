"""Detector determinístico: handoff preguiçoso EM VENDA → alerta na hora.

16/06/2026 — auditor reportou venda perdida (caso Ale): bot transferiu
sem chamar nenhuma ferramenta no meio da compra. Esse tipo de handoff já
era detectado pelo Haiku em teoria, mas falhou na prática. Agora há uma
camada DETERMINÍSTICA que combina 3 sinais auditáveis (acao=handoff +
tools sem busca + sinais fortes de compra sem reclamação) e, quando bate,
PULA o Haiku, força gravidade=alta e dispara o alerta na hora.

Trade-off: falso positivo gera ruído no WhatsApp do dono. Falso negativo
perde venda. Detector é conservador, mas erra pra mais alerta.
"""
from unittest.mock import patch

# ── Detector puro ───────────────────────────────────────────────────────

def _hist(*frases_user):
    return [{'role': 'user', 'content': f} for f in frases_user]


def test_detector_pega_compra_obvia_sem_tool():
    """Caso da Ale: cliente disse 'quero comprar', bot transferiu sem
    consultar nada."""
    from app.services import chatbot_vigia as v
    rb = {'acao': 'handoff', 'tools_usadas': ['transferir_para_humano']}
    h = _hist('oi, quero comprar uma cesta')
    assert v._e_handoff_preguicoso_em_compra(h, rb)


def test_detector_pega_quando_tools_vazia():
    from app.services import chatbot_vigia as v
    rb = {'acao': 'handoff', 'tools_usadas': []}
    h = _hist('quanto custa o sourdough?')
    assert v._e_handoff_preguicoso_em_compra(h, rb)


def test_detector_NAO_dispara_quando_bot_consultou_antes():
    """Bot chamou `consultar_produtos` e depois transferiu — não é
    preguiçoso, é legítimo."""
    from app.services import chatbot_vigia as v
    rb = {'acao': 'handoff',
          'tools_usadas': ['consultar_produtos', 'transferir_para_humano']}
    h = _hist('quero comprar')
    assert not v._e_handoff_preguicoso_em_compra(h, rb)


def test_detector_NAO_dispara_quando_cliente_pediu_humano(app):
    """FALSO POSITIVO corrigido 26/06: cliente vendo cestas que PEDE humano
    explicitamente -> handoff legitimo (excecao do prompt), mesmo com tools=[].
    Nao pode ser flagrado como preguicoso."""
    from app.services import chatbot_vigia as v
    rb = {'acao': 'handoff', 'tools_usadas': []}
    with app.app_context():
        # compra em curso (cesta) + pedido explicito de humano na mesma janela
        h = _hist('queria ver as cestas de café',
                  'prefiro falar com um atendente')
        assert not v._e_handoff_preguicoso_em_compra(h, rb)
        # so "falar com atendente", sem compra: tambem nao alerta
        h2 = _hist('quero falar com uma pessoa')
        assert not v._e_handoff_preguicoso_em_compra(h2, rb)


def test_detector_ainda_pega_preguicoso_sem_pedido_de_humano(app):
    """Regressao da regressao: compra em curso SEM o cliente pedir humano
    continua sendo flagrada (a correcao nao pode cegar o detector)."""
    from app.services import chatbot_vigia as v
    rb = {'acao': 'handoff', 'tools_usadas': []}
    with app.app_context():
        h = _hist('quanto custa a cesta brunch? quero comprar')
        assert v._e_handoff_preguicoso_em_compra(h, rb)


def test_tool_handoff_nao_convida_preguica():
    """Correcao 26/06: a descricao da tool transferir_para_humano nao pode
    convidar handoff preguicoso ('quando nao tiver certeza') nem mandar escalar
    entrega/CEP direto (existe consultar_frete). Tem que REFORCAR o prompt."""
    from app.services.chatbot import TOOL_HANDOFF
    desc = TOOL_HANDOFF['description'].lower()
    assert 'tiver certeza' not in desc           # escape removido
    assert 'ltimo recurso' in desc               # 'ÚLTIMO recurso'
    assert 'consultar_frete' in desc             # entrega/CEP -> frete


def test_detector_NAO_dispara_quando_e_reclamacao():
    """Reclamação = handoff humano é correto. Não classifica como
    preguiçoso mesmo sem tool."""
    from app.services import chatbot_vigia as v
    rb = {'acao': 'handoff', 'tools_usadas': []}
    casos = [
        'meu pedido não chegou',
        'veio errado',
        'quero reembolso',
        'cancelar meu pedido',
        'falar com responsável',
    ]
    for frase in casos:
        h = _hist(frase)
        assert not v._e_handoff_preguicoso_em_compra(h, rb), \
            f'falso positivo em reclamação: {frase!r}'


def test_detector_NAO_dispara_em_saudacao_simples():
    """Conversa que começou com 'oi' e foi pra handoff (provavelmente
    cliente pediu humano direto) — não é venda em risco."""
    from app.services import chatbot_vigia as v
    rb = {'acao': 'handoff', 'tools_usadas': []}
    h = _hist('oi, tudo bem?', 'pode falar com atendente')
    assert not v._e_handoff_preguicoso_em_compra(h, rb)


def test_detector_NAO_dispara_quando_acao_eh_responder():
    from app.services import chatbot_vigia as v
    rb = {'acao': 'responder', 'tools_usadas': []}
    h = _hist('quero comprar uma cesta')
    assert not v._e_handoff_preguicoso_em_compra(h, rb)


def test_detector_NAO_dispara_quando_tools_None_versao_antiga():
    """Bot antigo não persistia tools_usadas — tratar como 'desconhecido'
    e não promover (evita falso positivo em backfill de logs velhos)."""
    from app.services import chatbot_vigia as v
    rb = {'acao': 'handoff', 'tools_usadas': None}
    h = _hist('quero comprar')
    assert not v._e_handoff_preguicoso_em_compra(h, rb)


def test_detector_reclamacao_E_compra_combinados_NAO_alerta():
    """Cliente reclama + diz 'quero outro' — handoff humano é apropriado,
    não conta como preguiçoso (escolhemos a interpretação mais conservadora)."""
    from app.services import chatbot_vigia as v
    rb = {'acao': 'handoff', 'tools_usadas': []}
    h = _hist('meu pedido não chegou, queria comprar outro')
    assert not v._e_handoff_preguicoso_em_compra(h, rb)


# ── Integração com _avaliar_interno ──────────────────────────────────

def test_avaliar_pula_haiku_quando_detector_bate(app):
    """Quando o detector determinístico bate, o Haiku NÃO é chamado.
    Economia + reação imediata + auditabilidade."""
    from app.services import chatbot_vigia as v
    app.config['CHATBOT_VIGIA'] = '1'
    app.config['ANTHROPIC_API_KEY'] = 'sk-x'
    app.config['ZAPI_NUMERO_DESTINO'] = '5511999999999'
    with app.app_context():
        with patch.object(v, '_chamar_modelo') as haiku, \
             patch('app.services.zapi.enviar_texto',
                    return_value={'ok': True}) as send:
            r = v._avaliar_interno(
                _hist('quero comprar uma cesta'),
                conv_id=42, nome_contato='Ale',
                resultado_bot={'acao': 'handoff', 'tools_usadas': []})
    haiku.assert_not_called()
    # Veredicto montado pelo detector — gravidade alta
    assert r['veredicto']['gravidade'] == 'alta'
    assert 'Venda em risco' in r['veredicto']['motivo']
    # Mensagem foi pro WhatsApp do dono
    send.assert_called_once()


def test_avaliar_chama_haiku_quando_detector_NAO_bate(app):
    """Em conversa normal, o caminho do Haiku continua igual ao antigo
    (sem regressão)."""
    from app.services import chatbot_vigia as v
    app.config['CHATBOT_VIGIA'] = '1'
    app.config['ANTHROPIC_API_KEY'] = 'sk-x'
    with app.app_context():
        with patch.object(v, '_chamar_modelo',
                           return_value={'alerta': False, 'gravidade': None,
                                         'motivo': '', 'acao_sugerida': ''}) as haiku:
            v._avaliar_interno(
                _hist('oi'),
                conv_id=99, nome_contato='X',
                resultado_bot={'acao': 'responder', 'tools_usadas': []})
    haiku.assert_called_once()


def test_alerta_persistido_em_VigiaVeredito_pro_banner_do_painel(app):
    """Detector → veredicto alta → banner do painel também acende. O
    caminho `avaliar` (publico) grava em VigiaVeredito automaticamente."""
    from app.models import VigiaVeredito
    from app.services import chatbot_vigia as v
    app.config['CHATBOT_VIGIA'] = '1'
    app.config['ANTHROPIC_API_KEY'] = 'sk-x'
    app.config['ZAPI_NUMERO_DESTINO'] = '5511999999999'
    with app.app_context():
        with patch('app.services.zapi.enviar_texto',
                    return_value={'ok': True}):
            v.avaliar(
                _hist('quero comprar a cesta Box Mimo'),
                conv_id=123, nome_contato='Ale',
                resultado_bot={'acao': 'handoff', 'tools_usadas': []})
        vv = VigiaVeredito.query.filter_by(conv_id='123').first()
        assert vv is not None
        assert vv.alerta is True
        assert vv.gravidade == 'alta'
        assert 'Venda em risco' in (vv.motivo_vigia or '')


# ── Falsos positivos reportados pelo dono 23/06/2026 ─────────────────────

def test_reclamacao_de_tamanho_nao_e_handoff_preguicoso(app):
    """Croissant 'pequenininho' = reclamação pós-venda. O bot fez handoff
    direto (correto) e o vigia NÃO pode marcar como venda perdida."""
    from app.services.chatbot_vigia import _e_handoff_preguicoso_em_compra
    hist = [{'role': 'user',
             'content': 'Peguei um croissant agora mas tá tão pequenininho. '
                        'Todos estão assim?'}]
    rb = {'acao': 'handoff', 'tools_usadas': []}
    assert _e_handoff_preguicoso_em_compra(hist, rb) is False


def test_compra_real_ainda_alerta(app):
    """Não enfraquece o detector: compra ativa sem tool segue alertando."""
    from app.services.chatbot_vigia import _e_handoff_preguicoso_em_compra
    hist = [{'role': 'user', 'content': 'quero comprar 10 croissants'}]
    rb = {'acao': 'handoff', 'tools_usadas': []}
    assert _e_handoff_preguicoso_em_compra(hist, rb) is True


def test_fechamento_nao_dispara_espera(app):
    """'Ok' depois do humano resolver = encerramento, não 'cliente esperando'."""
    from app.services.chatbot_vigia import _e_fechamento
    assert _e_fechamento('Ok') is True
    assert _e_fechamento('ok obrigada') is True
    assert _e_fechamento('valeu mesmo!') is True
    assert _e_fechamento('👍') is True
    # mensagem que PRECISA de resposta não é fechamento
    assert _e_fechamento('ok, mas e a entrega?') is False
    assert _e_fechamento('Bem e vcs?') is False


def test_fechamento_com_emoji_fora_da_whitelist(app):
    """Caso real conv 1697 (18/08/2026): "Obrigada ✨" disparou alerta de
    'esperando atendente' à 01:25 + contenção pro cliente — o ✨ não estava
    na whitelist do regex. Emoji DECORATIVO desconhecido agora é ignorado
    genericamente (a whitelist já tinha falhado na criação também)."""
    from app.services.chatbot_vigia import _e_fechamento
    assert _e_fechamento('Obrigada ✨') is True
    assert _e_fechamento('valeu 🥖') is True
    assert _e_fechamento('perfeito 🌟🌟') is True
    # emoji NEGATIVO muda o sentido — não é fechamento tranquilo
    assert _e_fechamento('obrigada 😡') is False
    assert _e_fechamento('ok 😭') is False
    # só-emoji desconhecido não afirma fechamento (strip esvazia)
    assert _e_fechamento('✨') is False
    # e continua exigindo que o TEXTO seja de encerramento
    assert _e_fechamento('cadê meu pedido? ✨') is False
