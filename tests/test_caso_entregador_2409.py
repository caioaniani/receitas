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
ancorados nas DUAS pontas — cauda de cortesia no fim e `_ABERTURA_LIGACAO`
no começo da oração, mais `_CONDICAO_ENTREGA` na oração anterior);
TERCEIRO NA ENTREGA transfere sem número de pedido (prompt + fonte única
`motivo_terceiro_na_entrega` no enforcement e na métrica do auditor);
entrega em curso com problema e pedido de ligação sem transferência viram
ALTA no vigia (prompt + `_SINAIS_RECLAMACAO` ancorado a problema).

A 1ª versão (refutação 20/09/2026) errava nos dois sentidos: "quando chegar
me liga", "qualquer coisa me liga", "o motoboy me liga?" e "não é pra me
ligar" viravam handoff forçado antes do modelo; "pode deixar na portaria?"
furava o enforcement de venda; e "o motoboy foi super educado" virava
reclamação. Os casos abaixo travam os dois lados.
"""
from types import SimpleNamespace
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
    ('será que dá pra me ligar?', True),
    ('vc pode me ligar?', True),
    ('liga pra mim', True),
    ('Me ligue, por favor', True),
    ('Por favor me liga', True),
    ('Me liga por favor 🙏', True),
    ('Oi\nMe liga por favor', True),
    ('me retorna por favor', True),
    ('me telefona', True),
    ('quero uma ligação', True),
    ('preciso falar por telefone', True),
    ('me liga aí', True),
    ('liga aí', True),
    ('me liga por favor urgente', True),
    ('me liga no 11 99999-9999', True),
    ('liga no meu número por favor', True),
    ('pode entrar em contato comigo?', True),
    ('podem entrar em contato?', True),
    ('entra em contato comigo', True),
    ('me dá um retorno', True),
    ('Alguém me liga por favor', True),
    # vírgula LEGÍTIMA antes do pedido (oração anterior sem condição)
    ('não consegui te ligar, me liga', True),
    ('Sim, me liga', True),
    ('ok, me liga', True),
    ('quero cesta, me liga', True),
    ('Sou o entregador, me liga', True),
    ('Preciso de suporte, me liga por favor', True),
    ('Vou comprar amanhã. Me liga por favor', True),
    # pergunta NEGATIVA é pedido (regra decidida em 20/09)
    ('não me liga?', True),
    ('não liga pra mim?', True),
    # NÃO é pedido de contato agora: pergunta de processo, preferência de
    # entrega, telefone da loja, negação, ligação como assunto
    ('vocês ligam antes de entregar?', False),
    ('pode me ligar quando sair pra entrega?', False),
    ('quando sair pra entrega pode me ligar?', False),
    ('me liga quando chegar', False),
    ('me liga se tiver algum problema', False),
    ('pode me ligar amanhã de manhã?', False),
    ('não precisa me ligar', False),
    ('não me liga, prefiro por aqui', False),
    ('não me liga, por favor', False),
    ('não me liga não', False),
    ('não é pra me ligar', False),
    ('não quero ligação, só o preço', False),
    ('sem precisar me ligar, dá pra pagar por aqui?', False),
    ('qual o telefone da loja?', False),
    ('quero ligar pra vocês, qual o número?', False),
    ('o entregador não me ligou e foi embora', False),
    ('ligação caiu', False),
    ('a ligação de vocês não completou', False),
    ('me liga no 11 99999-9999 depois das 18h', False),
    # 2ª refutação (20/09): CONDIÇÃO TOPICALIZADA (antes do verbo), com e
    # sem vírgula, inclusive dentro de frase de venda com rajada e emoji
    ('Quero 2 cestas brunch pra amanhã de manhã, quando chegar me liga', False),
    ('vou querer a cesta café da manhã pra sábado. qualquer coisa me liga', False),
    ('Boa tarde! Quanto custa a cesta brunch? Qualquer dúvida me liga', False),
    ('pode entregar amanhã às 9h? ao chegar, me liga por favor', False),
    ('assim que chegar me liga', False),
    ('quando estiver na porta me liga', False),
    ('chegando lá me liga', False),
    ('se o entregador não achar o prédio me liga', False),
    ('de preferência me ligue', False),
    ('quero 2 cestas\nqualquer coisa me liga 🙏', False),
    ('quando chegar, me liga', False),
    ('qualquer coisa, me liga', False),
    ('se tiver algum problema, me liga por favor', False),
    ('de preferência, me ligue', False),
    ('assim que chegar, me liga', False),
    ('na hora da entrega, me liga', False),
    ('Quero a cesta café pra sábado. Qualquer coisa, me liga 🙏', False),
    # 2ª refutação: SUJEITO de 3ª pessoa — a 3ª do indicativo tem a mesma
    # forma do imperativo ("o entregador me liga?" é pergunta de processo)
    ('Oi, vou comprar pelo site. O entregador me liga?', False),
    ('O motoboy me liga?', False),
    ('o motoboy liga pra mim?', False),
    ('a loja me liga?', False),
    ('a padaria liga pra mim?', False),
    ('a lalamove me liga?', False),
    ('se eu não estiver em casa o entregador me liga?', False),
    ('no dia da entrega o motoboy me liga?', False),
    ('quero uma cesta pra amanhã, o entregador me liga?', False),
    ('vou comprar a cesta brunch. o motoboy liga pra mim?', False),
    ('vcs entregam no Brooklin? o entregador vai me ligar?', False),
    ('quem entrega me liga?', False),
    ('ele me liga?', False),
    ('vocês me ligam?', False),
    ('quando chegar vocês me ligam?', False),
    ('e aí, vocês me ligam?', False),
    # relato, gíria e FECHAMENTO do cliente nunca são pedido de contato
    ('a moça disse que ia me ligar', False),
    ('disseram que iam me ligar hoje', False),
    ('se liga aí!', False),
    ('vou entrar em contato', False),
    ('Obrigada! Depois entro em contato', False),
    ('qualquer coisa entro em contato', False),
    ('a gente entra em contato', False),
    ('ok, entrarei em contato', False),
    ('entra em contato comigo quando o pedido sair', False),
    ('me dá um retorno quando tiver a data', False),
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


@pytest.mark.parametrize('texto', [
    'Quero 2 cestas brunch pra amanhã de manhã, quando chegar me liga',
    'O motoboy me liga?',
    'não é pra me ligar',
])
def test_frase_de_venda_com_me_liga_vai_pro_modelo(app, texto):
    """A frase de venda NÃO vira handoff forçado: o Claude é chamado."""
    from app.services import chatbot
    with app.app_context():
        app.config['ANTHROPIC_API_KEY'] = 'test'
        with patch('anthropic.Anthropic') as M:
            M.return_value.messages.create.return_value = SimpleNamespace(
                content=[SimpleNamespace(type='text', text='Posso ajudar!')],
                stop_reason='end_turn')
            r = chatbot.responder([{'role': 'user', 'content': texto}])
        M.return_value.messages.create.assert_called()
    assert r['acao'] != 'handoff'


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
    ('ligação solicitada pelo cliente', True),
    ('cliente perguntou se o motoboy liga antes', False),
    ('faz ligação antes de entregar?', False),
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


MOTIVOS_TERCEIRO_REAL = [
    'entregador da Lalamove na portaria, ninguém atende',
    'motoboy deixou a cesta na entrada do prédio e vai devolver',
    'vizinha recebeu o pedido errado',
    'porteiro diz que ninguém recebe a encomenda',
    'entregador não consegue contato com o cliente',
    'entregador relata problema na entrega',
    'entregador da Lalamove com problema na entrega, precisa de suporte',
    'motoboy está com a cesta na entrada do prédio',
    'entregador diz que vai voltar',
    'entregador deixou com o porteiro e foi embora',
    'porteiro não quis receber a cesta',
    'Entregador da Lalamove precisa de suporte em uma entrega de cesta',
    'Entregador da Lalamove: cesta deixada na entrada do prédio, ninguém '
    'atende, vai voltar para devolver',
    'Portaria do prédio informa que ninguém recebe a encomenda',
    'Vizinho recebeu a cesta por engano e quer devolver',
]
# SÓ terceiro, SÓ problema, HIPÓTESE ou VENDA em curso — o enforcement de
# venda não afrouxa ("pode deixar na portaria?" é a pergunta de entrega
# mais comum do canal; 2ª refutação 20/09)
MOTIVOS_VENDA_COM_TERCEIRO = [
    'cliente perguntou se o motoboy liga antes',
    'dúvida sobre entregador',
    'problema com a cesta, quer trocar',
    'cliente quer cotar frete, motorista',
    'dúvida sobre sabor do pão',
    'cliente perguntou se o motoboy pode deixar na portaria',
    'cliente quer saber se entregam na portaria do prédio',
    'cliente quer saber se pode deixar com o porteiro',
    'dúvida se o motoboy liga quando ninguém atende',
    'cliente perguntou se o entregador espera na porta',
    'cliente quer cotar frete e saber se o entregador deixa na portaria',
    'aguardando o valor, cliente quer saber se tem portaria',
    'espera ela voltar do trabalho, entregador pode passar depois',
    'cliente esperando em casa o motoboy',
    'endereço errado no cadastro, cliente quer corrigir antes de fechar o pedido',
    'cliente pergunta se tem problema o entregador deixar na portaria',
    'faz ligação antes de entregar?',
    'recebe ligação do entregador',
    'não quer ligação',
    'ligação de internet',
    'entregador parado na portaria, entrega express com frete pago',
    'entregador não teve problema, cliente só quer saber o frete',
    'cliente quer suporte na entrega do presente pro vizinho',
]


@pytest.mark.parametrize('motivo', MOTIVOS_TERCEIRO_REAL)
def test_enforcement_libera_terceiro_na_entrega_com_problema(motivo):
    from app.services.chatbot import _handoff_excecao, motivo_terceiro_na_entrega
    assert motivo_terceiro_na_entrega(motivo) is True
    assert _handoff_excecao({'motivo': motivo}) is True


@pytest.mark.parametrize('motivo', MOTIVOS_VENDA_COM_TERCEIRO)
def test_enforcement_segue_barrando_venda_com_terceiro(motivo):
    from app.services.chatbot import _handoff_excecao, motivo_terceiro_na_entrega
    assert motivo_terceiro_na_entrega(motivo) is False
    assert _handoff_excecao({'motivo': motivo}) is False


def test_enforcement_reconhece_pedido_de_ligacao_no_motivo():
    from app.services.chatbot import _handoff_excecao
    for m in ('cliente pediu ligação', 'ligação solicitada pelo cliente',
              'cliente pediu para ligar', 'solicitou retorno por telefone'):
        assert _handoff_excecao({'motivo': m}) is True, m


def test_auditor_concorda_com_enforcement_no_terceiro_na_entrega(app):
    """Fonte única: o que o enforcement libera sem consulta, o auditor não
    conta como preguiça — e o que o enforcement barra, o auditor conta."""
    from app.services.chatbot_vigia import handoff_foi_preguicoso
    with app.app_context():
        for m in MOTIVOS_TERCEIRO_REAL:
            assert handoff_foi_preguicoso([], motivo=m) is False, m
        for m in MOTIVOS_VENDA_COM_TERCEIRO:
            assert handoff_foi_preguicoso([], motivo=m) is True, m


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
                     'deixei na portaria, vou voltar para devolver',
                     'o motoboy não achou meu prédio e foi embora',
                     'estou esperando o motoboy há uma hora',
                     'a lalamove cancelou minha entrega'):
            hist = compra + [{'role': 'user', 'content': fala}]
            assert _e_handoff_preguicoso_em_compra(hist, rb) is False, fala


@pytest.mark.parametrize('fala', [
    'o motoboy entrega até que horas?',
    'tem portaria no prédio, pode deixar lá?',
    'vocês entregam por lalamove?',
    'pode deixar com o porteiro?',
    'vou voltar às 18h, o motoboy pode vir depois?',
    'o entregador liga antes de subir?',
    'quero comprar 2 croissants, o motoboy chega em quanto tempo?',
    'Preciso de uma pessoa em casa para receber ou pode deixar na portaria?',
])
def test_vigia_ao_vivo_segue_acusando_venda_em_risco_em_logistica_de_venda(app, fala):
    """Substantivo solto (motoboy/portaria/lalamove) NÃO é reclamação: a
    pergunta de entrega dentro da compra continua vigiada."""
    from app.services.chatbot_vigia import _e_handoff_preguicoso_em_compra
    with app.app_context():
        hist = [{'role': 'user', 'content': 'quanto custa a cesta brunch? quero comprar'},
                {'role': 'user', 'content': fala}]
        assert _e_handoff_preguicoso_em_compra(
            hist, {'acao': 'handoff', 'tools_usadas': []}) is True


@pytest.mark.parametrize('msg', [
    'Obrigada! O motoboy foi super educado',
    'chegou tudo certinho, o entregador foi muito gentil, obrigada',
    'Perfeito, deixa na portaria então, obrigada',
])
def test_elogio_ao_motoboy_nao_vira_reclamacao(app, monkeypatch, msg):
    """Turno vazio do modelo num fechamento que cita o entregador continua
    SILÊNCIO (decisão do dono 16/06): a 1ª versão o mandava pra fila humana
    com pedido de desculpas."""
    from app.services import chatbot
    from app.services.chatbot_vigia import _SINAIS_RECLAMACAO
    assert not _SINAIS_RECLAMACAO.search(msg.lower())
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-x')

    class FakeClient:
        def __init__(self, **kw):
            pass

        class messages:
            @staticmethod
            def create(**kw):
                return SimpleNamespace(
                    content=[SimpleNamespace(type='text', text='   ')],
                    stop_reason='end_turn')

    monkeypatch.setattr('anthropic.Anthropic', FakeClient)
    monkeypatch.setattr(chatbot, '_fora_horario_chat', lambda: False)
    with app.app_context():
        out = chatbot.responder([
            {'role': 'assistant', 'content': 'Qualquer coisa é só chamar.'},
            {'role': 'user', 'content': msg},
        ])
    assert out['acao'] == 'encerrar'


def test_prompt_do_vigia_trata_entrega_em_curso_e_ligacao_como_alta():
    from app.services.chatbot_vigia import PROMPT_VIGIA
    idx = PROMPT_VIGIA.find('GRAVIDADE=ALTA')
    idx_media = PROMPT_VIGIA.find('GRAVIDADE=MEDIA')
    assert 0 < idx < idx_media
    bloco = PROMPT_VIGIA[idx:idx_media]
    assert 'ENTREGA EM CURSO COM PROBLEMA' in bloco
    assert 'Cliente pediu LIGAÇÃO' in bloco
    assert 'NÃO é cumprimento' in bloco


# ── 4. Prompt do bot e do auditor ───────────────────────────────────────

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


def test_prompt_do_auditor_exclui_ligacao_e_terceiro_da_preguica():
    from app.services.chatbot_auditor import PROMPT_AUDITOR_RESUMO
    assert 'LIGAÇÃO' in PROMPT_AUDITOR_RESUMO
    assert 'TERCEIRO na entrega' in PROMPT_AUDITOR_RESUMO
