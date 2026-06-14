"""O bot do dono (WhatsApp + Slack) precisa ver TUDO que a vigia ve.

Bug real (14/06/2026): a vigia alertou no WhatsApp do dono sobre uma
reclamacao operacional grave. O dono perguntou ao bot 'qual foi a
reclamacao?' e o bot respondeu que NAO TINHA ACESSO. Causa raiz: o copilot
tem 13 tools de read mas nenhuma le VigiaVeredito ou Chatwoot — o bot do
dono podia ver pedido/estoque/financeiro, mas nao via o que a propria
vigia que o avisou tinha visto.

Estas travas garantem que o bot agora tem:
1. consultar_vigia — le VigiaVeredito (a fonte dos alertas)
2. consultar_conversa_chatwoot — le o dialogo real da conversa
3. listar_conversas_chatwoot — quem ta na fila (humana ou bot)
"""
from datetime import timedelta
from unittest.mock import patch


def _setup_veredito(grav='alta', conv_id='123', dias_atras=0,
                     mensagem='Pedido nao chegou e ja sao 18h',
                     motivo_vigia='Reclamacao operacional grave (atraso)',
                     cliente='Mariana Souza'):
    from app.extensions import db
    from app.models import VigiaVeredito
    from app.utils import agora
    v = VigiaVeredito(
        criado_em=agora() - timedelta(days=dias_atras),
        conv_id=str(conv_id),
        cliente=cliente,
        mensagem_cliente=mensagem,
        bot_acao='handoff',
        bot_motivo='Cliente reclamando — transferi pra humano',
        alerta=True,
        gravidade=grav,
        motivo_vigia=motivo_vigia,
        enviado_whatsapp=True,
    )
    db.session.add(v)
    db.session.commit()
    return v


def test_tools_estao_registradas():
    """Wiring: as 3 tools precisam aparecer na lista TOOLS."""
    from app.services import copilot
    nomes = {t['name'] for t in copilot.TOOLS}
    assert 'consultar_vigia' in nomes
    assert 'consultar_conversa_chatwoot' in nomes
    assert 'listar_conversas_chatwoot' in nomes


def test_permissoes_owner_only():
    """Mensagens de cliente + alertas internos = so o dono pode ler."""
    from app.services import copilot
    for tool in ('consultar_vigia', 'consultar_conversa_chatwoot',
                  'listar_conversas_chatwoot'):
        papeis = copilot.PAPEIS_POR_TOOL[tool]
        assert papeis == {'owner'}, \
            f'{tool} deve ser owner-only, esta {papeis}'


def test_read_handlers_estao_no_dispatch():
    """O dispatcher precisa apontar pros handlers, senao bot trava."""
    from app.services import copilot
    for tool in ('consultar_vigia', 'consultar_conversa_chatwoot',
                  'listar_conversas_chatwoot'):
        assert tool in copilot._READ_HANDLERS
        assert callable(copilot._READ_HANDLERS[tool])


def test_consultar_vigia_retorna_reclamacao_alta(app, owner_user):
    """Caso real: dono pergunta 'qual foi a reclamacao' apos receber alerta."""
    from app.services.copilot import _read_consultar_vigia
    with app.app_context():
        _setup_veredito(grav='alta', conv_id='198',
                        mensagem='Pedido nao chegou ate agora!!!',
                        motivo_vigia='Reclamacao grave — cliente irritado')
        out = _read_consultar_vigia({}, owner_user)
    txt = out['texto']
    assert 'Mariana' in txt
    assert 'conv #198' in txt
    assert 'Reclamacao' in txt or 'reclamacao' in txt.lower()
    assert 'ALTA' in txt


def test_consultar_vigia_filtra_por_palavra(app, owner_user):
    """Filtro `palavra` ajuda o dono a focar (ex: 'esperando humano')."""
    from app.services.copilot import _read_consultar_vigia
    with app.app_context():
        _setup_veredito(grav='alta', conv_id='10', cliente='Joao',
                        mensagem='Cesta de cafe?',
                        motivo_vigia='Bot mandou pro humano sem tentar consultar_produtos')
        _setup_veredito(grav='alta', conv_id='20', cliente='Patricia',
                        mensagem='cade meu pedido',
                        motivo_vigia='Cliente esperando ATENDENTE ha 30 min')
        out = _read_consultar_vigia({'palavra': 'esperando'}, owner_user)
    txt = out['texto']
    assert 'Patricia' in txt
    assert 'Joao' not in txt


def test_consultar_vigia_por_conv_id_ignora_dias(app, owner_user):
    """conv_id puxa TODO historico daquela conv, mesmo fora da janela."""
    from app.services.copilot import _read_consultar_vigia
    with app.app_context():
        _setup_veredito(conv_id='42', dias_atras=10, cliente='Antiga')
        out = _read_consultar_vigia({'conv_id': '42'}, owner_user)
    assert 'Antiga' in out['texto']


def test_consultar_vigia_sem_dados_responde_amigavel(app, owner_user):
    """Tabela vazia → mensagem clara, nao erro."""
    from app.services.copilot import _read_consultar_vigia
    with app.app_context():
        out = _read_consultar_vigia({}, owner_user)
    assert 'nenhum' in out['texto'].lower()


def test_consultar_conversa_chatwoot_renderiza_dialogo(app, owner_user):
    """Mocka chatwoot.buscar_historico e confere a renderizacao."""
    from app.services.copilot import _read_consultar_conversa_chatwoot
    fake = [
        {'role': 'user', 'content': 'Pedido nao chegou ainda'},
        {'role': 'assistant', 'content': 'Vou transferir pra um atendente'},
        {'role': 'user', 'content': 'Ja faz 1 hora'},
    ]
    with app.app_context():
        with patch('app.services.chatwoot.buscar_historico',
                   return_value=fake):
            out = _read_consultar_conversa_chatwoot(
                {'conv_id': '198'}, owner_user)
    txt = out['texto']
    assert 'Conversa #198' in txt
    assert 'Cliente' in txt
    assert 'Bot/Atendente' in txt
    assert 'Ja faz 1 hora' in txt


def test_consultar_conversa_chatwoot_sem_historico(app, owner_user):
    """Conversa sem msg / Chatwoot offline → texto amigavel."""
    from app.services.copilot import _read_consultar_conversa_chatwoot
    with app.app_context():
        with patch('app.services.chatwoot.buscar_historico', return_value=[]):
            out = _read_consultar_conversa_chatwoot(
                {'conv_id': '999'}, owner_user)
    assert '999' in out['texto']
    assert 'sem mensagens' in out['texto'].lower()


def test_listar_conversas_chatwoot_open_default(app, owner_user):
    """Default = 'open' (cliente esperando humano — o que mais importa)."""
    from app.services.copilot import _read_listar_conversas_chatwoot
    fake = [
        {'id': 198, 'nome_contato': 'Mariana', 'minutos_paradas': 45},
        {'id': 210, 'nome_contato': 'Pedro', 'minutos_paradas': 12},
    ]
    with app.app_context():
        with patch('app.services.chatwoot.listar_conversas_paradas',
                   return_value=fake) as mock:
            out = _read_listar_conversas_chatwoot({}, owner_user)
            args = mock.call_args
            assert args.kwargs.get('status') == 'open'
    txt = out['texto']
    assert 'Mariana' in txt
    assert '45min' in txt
    assert 'Pedro' in txt
