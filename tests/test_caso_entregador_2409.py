"""Caso conv 2409 (20/09/2026) — entregador da Lalamove preso no bot.

O entregador escreveu "Me liga por favor", depois "Preciso de suporte só
uma entrega", mandou foto da cesta deixada na entrada do prédio e "Devido
à comunicação vou voltar para devolver os". O bot: (1) respondeu "quer que
eu passe pra equipe?" em vez de passar; (2) consultou pedidos pelo telefone
(vazio — entregador não é cliente) e pediu o número do pedido TRÊS vezes;
(3) o vigia deu MÉDIA aos dois turnos críticos e leu "Me liga por favor"
como cumprimento. Ninguém da padaria soube; o dono só foi avisado pelo
detector de abandono meia hora depois ("Não foi passado para nenhum
atendente").

Três correções: pedido de LIGAÇÃO = handoff forçado (`_LIGACAO_PATTERNS`,
dentro de `_HUMANO_PATTERNS`); TERCEIRO NA ENTREGA transfere sem número de
pedido (prompt + exceção do enforcement `_TERCEIRO_ENTREGA` ×
`_PROBLEMA_ENTREGA_EM_CURSO`); entrega em curso com problema e pedido de
ligação sem transferência viram ALTA no vigia (prompt + `_SINAIS_RECLAMACAO`).
"""
from unittest.mock import patch

import pytest

# ── 1. Pedido de ligação = pedido de humano ─────────────────────────────

@pytest.mark.parametrize('texto, esperado', [
    ('Me liga por favor', True),
    ('me liga', True),
    ('Pode me ligar?', True),
    ('podem me ligar', True),
    ('poderia me ligar agora?', True),
    ('dá pra me ligar?', True),
    ('liga pra mim', True),
    ('Me ligue, por favor', True),
    ('Por favor me liga', True),
    ('Me liga por favor 🙏', True),
    ('Oi\nMe liga por favor', True),
    ('me retorna por favor', True),
    ('quero uma ligação', True),
    ('preciso falar por telefone', True),
    ('não consegui te ligar, me liga', True),
    # NÃO é pedido de contato agora: pergunta de processo, preferência de
    # entrega, telefone da loja, negação, ligação como assunto
    ('vocês ligam antes de entregar?', False),
    ('pode me ligar quando sair pra entrega?', False),
    ('me liga quando chegar', False),
    ('me liga se tiver algum problema', False),
    ('pode me ligar amanhã de manhã?', False),
    ('não precisa me ligar', False),
    ('não me liga, prefiro por aqui', False),
    ('não quero ligação, só o preço', False),
    ('sem precisar me ligar, dá pra pagar por aqui?', False),
    ('qual o telefone da loja?', False),
    ('quero ligar pra vocês, qual o número?', False),
    ('o entregador não me ligou e foi embora', False),
    ('ligação caiu', False),
    ('a ligação de vocês não completou', False),
    # As falas do caso real que NÃO são pedido de humano (viram regra de
    # terceiro na entrega, não handoff forçado)
    ('Preciso de suporte só uma entrega', False),
    ('É uma entrega de uma cesta da Lala movie', False),
    ('Devido à comunicação vou voltar para devolver os', False),
])
def test_pedido_de_ligacao_e_pedido_de_humano(texto, esperado):
    from app.services.chatbot import _pede_ligacao, _quer_humano
    assert _quer_humano(texto) is esperado
    assert _pede_ligacao(texto) is esperado


def test_pede_ligacao_nao_confunde_com_pedido_de_atendente():
    from app.services.chatbot import _pede_ligacao, _quer_humano
    assert _quer_humano('quero falar com atendente')
    assert _pede_ligacao('quero falar com atendente') is False


def test_bot_forca_handoff_no_me_liga_com_texto_de_ligacao(app):
    """`responder` transfere ANTES do modelo em "Me liga por favor", com
    motivo próprio e texto que promete o contato (o bot não liga — a equipe
    liga). O Claude nem é chamado."""
    from app.services import chatbot
    with app.app_context():
        app.config['ANTHROPIC_API_KEY'] = 'test'
        with patch('anthropic.Anthropic') as M:
            r = chatbot.responder([{'role': 'user', 'content': 'Me liga por favor'}])
        M.return_value.messages.create.assert_not_called()
    assert r['acao'] == 'handoff'
    assert r['motivo'] == 'cliente pediu ligação'
    assert 'contato' in r['texto']
    assert r['tools_usadas'] == []


def test_bot_forca_handoff_de_atendente_segue_com_motivo_antigo(app):
    from app.services import chatbot
    with app.app_context():
        app.config['ANTHROPIC_API_KEY'] = 'test'
        with patch('anthropic.Anthropic'):
            r = chatbot.responder([{'role': 'user', 'content': 'quero falar com atendente'}])
    assert r['motivo'] == 'cliente pediu atendente'


# ── 2. Métrica do auditor e enforcement ─────────────────────────────────

@pytest.mark.parametrize('motivo, esperado', [
    ('cliente pediu ligação', True),
    ('cliente pediu para ligar', True),
    ('cliente quer que a equipe ligue', True),
    ('solicitou retorno por telefone', True),
    ('pedido de ligação do cliente', True),
    ('cliente perguntou se o motoboy liga antes', False),
    ('dúvida sobre ligação de internet', False),
    ('cliente não quer ligação', False),
])
def test_pediu_humano_reconhece_pedido_de_ligacao_no_motivo(motivo, esperado):
    from app.services.chatbot import pediu_humano
    assert pediu_humano(None, motivo) is esperado


def test_auditor_nao_conta_pedido_de_ligacao_como_preguicoso(app):
    from app.services.chatbot_vigia import handoff_foi_preguicoso
    with app.app_context():
        assert handoff_foi_preguicoso([], motivo='cliente pediu ligação') is False
        assert handoff_foi_preguicoso(
            [], mensagem_cliente='Me liga por favor') is False
        assert handoff_foi_preguicoso([], motivo='dúvida de frete') is True


@pytest.mark.parametrize('motivo, esperado', [
    ('cliente pediu ligação', True),
    ('entregador da Lalamove na portaria, ninguém atende', True),
    ('motoboy deixou a cesta na entrada do prédio e vai devolver', True),
    ('vizinha recebeu o pedido errado', True),
    ('porteiro diz que ninguém recebe a encomenda', True),
    # SÓ terceiro OU SÓ problema não abre a exceção — o enforcement segue
    # barrando o handoff preguiçoso de venda
    ('cliente perguntou se o motoboy liga antes', False),
    ('dúvida sobre entregador', False),
    ('problema com a cesta, quer trocar', False),
    ('cliente quer cotar frete, motorista', False),
    ('dúvida sobre sabor do pão', False),
])
def test_enforcement_libera_terceiro_na_entrega_com_problema(motivo, esperado):
    from app.services.chatbot import _handoff_excecao
    assert _handoff_excecao({'motivo': motivo}) is esperado


# ── 3. Vigia ────────────────────────────────────────────────────────────

def test_vigia_ao_vivo_nao_acusa_venda_em_risco_no_caso_2409(app):
    """Handoff com tools=[] depois das falas do entregador (ou de "Me liga
    por favor") NÃO é "venda em risco": reclamação/terceiro na entrega e
    pedido de contato humano desarmam o detector determinístico."""
    from app.services.chatbot_vigia import _e_handoff_preguicoso_em_compra
    with app.app_context():
        compra = [{'role': 'user', 'content': 'quanto custa a cesta brunch? quero comprar'}]
        rb = {'acao': 'handoff', 'tools_usadas': []}
        assert _e_handoff_preguicoso_em_compra(compra, rb) is True
        for fala in ('Me liga por favor',
                     'É uma entrega de uma cesta da Lalamove',
                     'sou o entregador, ninguém atende no apartamento',
                     'deixei na portaria, vou voltar para devolver'):
            hist = compra + [{'role': 'user', 'content': fala}]
            assert _e_handoff_preguicoso_em_compra(hist, rb) is False, fala


def test_prompt_do_vigia_trata_entrega_em_curso_e_ligacao_como_alta():
    from app.services.chatbot_vigia import PROMPT_VIGIA
    idx = PROMPT_VIGIA.find('GRAVIDADE=ALTA')
    idx_media = PROMPT_VIGIA.find('GRAVIDADE=MEDIA')
    assert 0 < idx < idx_media
    bloco = PROMPT_VIGIA[idx:idx_media]
    assert 'ENTREGA EM CURSO COM PROBLEMA' in bloco
    assert 'Cliente pediu LIGAÇÃO' in bloco
    assert 'NÃO é cumprimento' in bloco


# ── 4. Prompt do bot ────────────────────────────────────────────────────

def test_prompt_do_bot_ensina_ligacao_e_terceiro_na_entrega():
    from app.services.chatbot_prompt import PROMPT
    idx = PROMPT.find('ANTES DE TRANSFERIR')
    assert idx > 0
    bloco = PROMPT[idx:idx + 4500]
    assert 'pediu uma LIGAÇÃO' in bloco
    assert 'cliente pediu ligação' in bloco
    assert 'TERCEIRO NA ENTREGA' in bloco
    assert 'NO MÁXIMO' in bloco
    # A exceção antiga de correção de endereço continua listada no bloco
    assert 'Correção de ENDEREÇO/DESTINATÁRIO' in bloco
