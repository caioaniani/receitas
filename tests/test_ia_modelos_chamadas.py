"""Comportamento das chamadas de IA depois da troca para o Sonnet 5.5
(28/09/2026): parâmetros enviados por função, recusa das salvaguardas
tratada pelo caminho de falha de cada uma, nível de log do erro da API e a
visibilidade (/admin/debug-ia e sonda de deploy).

A Anthropic é SEMPRE simulada aqui — a prova de que a API real aceita os
parâmetros é o /admin/debug-ia?testar=1 depois do deploy.
"""
import json
import logging
from types import SimpleNamespace

import pytest

from app.services import ia_modelos

# ── Cliente falso ───────────────────────────────────────────


class _Fake:
    """Substitui `anthropic.Anthropic`: grava os kwargs de cada create e
    devolve as respostas da fila (a última se repete)."""

    def __init__(self, respostas):
        self.respostas = list(respostas)
        self.chamadas = []
        fake = self

        class _Messages:
            @staticmethod
            def create(**kwargs):
                fake.chamadas.append(kwargs)
                if len(fake.respostas) > 1:
                    return fake.respostas.pop(0)
                return fake.respostas[0]

        self.messages = _Messages()

    def __call__(self, *args, **kwargs):
        return self


def _uso():
    return SimpleNamespace(input_tokens=10, output_tokens=5,
                           cache_read_input_tokens=0,
                           cache_creation_input_tokens=0)


def _texto(txt, stop_reason='end_turn', model='claude-sonnet-5-5'):
    return SimpleNamespace(
        content=[SimpleNamespace(type='text', text=txt)],
        stop_reason=stop_reason, stop_details=None, usage=_uso(), model=model)


def _recusa(categoria='general_harms', parcial=''):
    content = [SimpleNamespace(type='text', text=parcial)] if parcial else []
    return SimpleNamespace(
        content=content, stop_reason='refusal',
        stop_details=SimpleNamespace(category=categoria), usage=_uso(),
        model='claude-sonnet-5-5')


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-teste')

    def instalar(*respostas):
        fake = _Fake(respostas)
        monkeypatch.setattr('anthropic.Anthropic', fake)
        return fake
    return instalar


_SEM_RACIOCINIO_5_5 = {'thinking': {'type': 'between_tools'},
                       'output_config': {'effort': 'high'}}


# ── Copilot (Slack e WhatsApp do dono) ──────────────────────


def _interpretar(app, admin_user, **kwargs):
    from app.models import Usuario
    from app.services import copilot
    with app.app_context():
        u = Usuario.query.get(admin_user.id)
        return copilot.interpretar('oi', u, **kwargs)


def test_copilot_5_5_pede_notas_de_progresso(app, admin_user, api):
    fake = api(_texto('ok'))
    _interpretar(app, admin_user)
    kw = fake.chamadas[0]
    assert kw['model'] == 'claude-sonnet-5-5'
    assert kw['thinking'] == {'type': 'adaptive', 'display': 'updates'}
    assert kw['extra_headers'] == {
        'anthropic-beta': 'thinking-display-updates-2026-08-18'}
    assert kw['output_config'] == {'effort': 'high'}


def test_copilot_explicacao_vem_da_nota_de_progresso(app, admin_user, api):
    """No 5.5 o texto que acompanha a tool volta em bloco thinking — sem ler
    esse bloco o preview do Slack ficaria '(sem comentario do copilot)'."""
    resp = SimpleNamespace(
        content=[
            SimpleNamespace(type='thinking', thinking='Vou consultar as '
                            'tarefas abertas.'),
            SimpleNamespace(type='thinking', thinking=''),
        ],
        stop_reason='end_turn', stop_details=None, usage=_uso(),
        model='claude-sonnet-5-5')
    api(resp)
    out = _interpretar(app, admin_user)
    assert 'Vou consultar as tarefas abertas.' in out['explicacao']


def test_copilot_override_antigo_nao_recebe_parametro_do_5_5(app, admin_user,
                                                              api):
    fake = api(_texto('ok', model='claude-opus-4-8'))
    _interpretar(app, admin_user, modelo='claude-opus-4-8')
    kw = fake.chamadas[0]
    assert kw['model'] == 'claude-opus-4-8'
    assert 'thinking' not in kw
    assert 'extra_headers' not in kw
    assert 'output_config' not in kw


def test_copilot_recusa_vira_erro_sem_executar(app, admin_user, api):
    api(_recusa('cyber', parcial='Posso aju'))
    out = _interpretar(app, admin_user)
    assert out['tipo'] == 'erro'
    assert 'recusou' in out['explicacao']
    assert 'cyber' in out['explicacao']


def test_copilot_recusa_registra_custo(app, admin_user, api):
    from app.models import UsoIA
    api(_recusa())
    _interpretar(app, admin_user)
    with app.app_context():
        assert UsoIA.query.filter_by(funcao='copilot_slack').count() == 1


# ── Bot de atendimento (motor offline) ──────────────────────


def test_bot_recusa_vai_para_a_equipe(app, api):
    """Recusa nunca e turno vazio (que pode encerrar em silencio)."""
    from app.services import chatbot
    fake = api(_recusa())
    with app.app_context():
        out = chatbot._responder_modelo_offline(
            [{'role': 'user', 'content': 'quero comprar uma cesta'}])
    assert out['acao'] == 'handoff'
    assert 'recusa da IA' in out['motivo']
    assert fake.chamadas[0]['thinking'] == {'type': 'adaptive',
                                            'display': 'updates'}


def test_followup_recusa_nao_vira_mensagem(app, api):
    from app.services import chatbot
    fake = api(_recusa(parcial='Oi! Ainda'))
    with app.app_context(), pytest.raises(ia_modelos.RespostaRecusada):
        chatbot._followup_gerar_texto(
            'sk', [{'role': 'assistant', 'content': 'Posso ajudar?'}], 10)
    assert {k: fake.chamadas[0][k] for k in ('thinking', 'output_config')} \
        == _SEM_RACIOCINIO_5_5


# ── Vigia e auditor ─────────────────────────────────────────


def test_vigia_usa_between_tools_e_recusa_vira_erro(app, api):
    from app.services import chatbot_vigia
    fake = api(_recusa())
    with app.app_context(), pytest.raises(ia_modelos.RespostaRecusada):
        chatbot_vigia._chamar_modelo('sk', 'contexto')
    kw = fake.chamadas[0]
    assert kw['model'] == 'claude-sonnet-5-5'
    assert {k: kw[k] for k in ('thinking', 'output_config')} == \
        _SEM_RACIOCINIO_5_5


def test_vigia_abandono_usa_between_tools(app, api):
    from app.services import chatbot_vigia
    fake = api(_texto(json.dumps({'alerta': False})))
    with app.app_context():
        chatbot_vigia._chamar_modelo_abandono('sk', 'contexto')
    assert fake.chamadas[0]['thinking'] == {'type': 'between_tools'}


def test_auditor_usa_between_tools_e_recusa_vira_erro(app, api):
    from app.services import chatbot_auditor
    fake = api(_recusa())
    with app.app_context(), pytest.raises(ia_modelos.RespostaRecusada):
        chatbot_auditor._chamar_sonnet('sk', 'contexto')
    assert fake.chamadas[0]['thinking'] == {'type': 'between_tools'}


# ── OCRs, cadastro, planejamento, treino, reviews, SEO ──────


def test_ocr_cupom_recusa(app, api):
    from app.services import ocr_nota
    fake = api(_recusa())
    with app.app_context():
        out = ocr_nota.extrair_itens_nota(b'img', 'image/jpeg')
    assert 'recusou' in out['erro']
    assert fake.chamadas[0]['thinking'] == {'type': 'between_tools'}


def test_ocr_nf_recusa(app, api):
    from app.services import conta_pagar_ia
    api(_recusa())
    with app.app_context():
        out = conta_pagar_ia.extrair_documento(b'img', 'image/jpeg')
    assert 'recusou' in out['erro']


def test_ocr_nf_env_antiga_recebe_disabled(app, api, monkeypatch):
    """OCR_MODELO_OPUS=claude-opus-4-8 no Railway: o parametro sai do modelo
    que vai rodar, nunca o between_tools do 5.5."""
    from app.services import conta_pagar_ia
    monkeypatch.setattr(conta_pagar_ia, 'MODELO', 'claude-opus-4-8')
    fake = api(_texto('{"tipo": "boleto"}', model='claude-opus-4-8'))
    with app.app_context():
        conta_pagar_ia.extrair_documento(b'img', 'image/jpeg')
    assert fake.chamadas[0]['thinking'] == {'type': 'disabled'}
    assert 'output_config' not in fake.chamadas[0]


def test_cadastro_ia_recusa(app, api):
    from app.services import cadastro_ia
    api(_recusa())
    with app.app_context():
        out = cadastro_ia.analisar(texto='Misto quente R$ 12')
    assert 'recusou' in out['erro']


def test_planejamento_recusa_e_erro_generico(app, api):
    from app.services import planejamento_ia
    api(_recusa())
    with app.app_context():
        dados, erro = planejamento_ia._chamar_opus('sys', 'payload', 'teste')
    assert dados is None
    assert 'recusou' in erro


def test_planejamento_retry_de_truncamento_mantem_parametros(app, api):
    from app.services import planejamento_ia
    fake = api(_texto('{"a"', stop_reason='max_tokens'), _texto('{"a": 1}'))
    with app.app_context():
        dados, erro = planejamento_ia._chamar_opus('sys', 'payload', 'teste')
    assert dados['a'] == 1 and erro is None
    assert len(fake.chamadas) == 2
    for kw in fake.chamadas:
        assert kw['thinking'] == {'type': 'between_tools'}


def test_treino_ia_recusa(app, api):
    from app.services import treino_ia_perguntas
    api(_recusa())
    with app.app_context(), pytest.raises(treino_ia_perguntas._IAError) as e:
        treino_ia_perguntas._chamar('sys', 'instrucao')
    assert 'recusou' in str(e.value)


def test_google_reviews_recusa_nao_vira_rascunho(app, api):
    from app.extensions import db
    from app.models import GoogleReview
    from app.services import google_reviews
    api(_recusa(parcial='Obrigado pela'))
    with app.app_context():
        rev = GoogleReview(review_id='r-1', nota=2, comentario='demorou')
        db.session.add(rev)
        db.session.commit()
        texto, msg = google_reviews.rascunho_resposta(rev.id)
    assert texto is None
    assert 'recusou' in msg


def test_seo_recusa_nao_vira_sugestao(app, api):
    from app.services import seo_descricoes
    api(_recusa(parcial='Pão de fermentação'))
    with app.app_context():
        assert seo_descricoes._chamar_claude('prompt') is None


# ── Nível de log do erro da API ─────────────────────────────


class _ErroApi(Exception):
    def __init__(self, status):
        super().__init__(f'HTTP {status}')
        self.status_code = status


@pytest.mark.parametrize('status, nivel', [
    (400, logging.ERROR), (401, logging.ERROR), (403, logging.ERROR),
    (404, logging.ERROR), (413, logging.WARNING), (429, logging.WARNING),
    (500, logging.WARNING), (529, logging.WARNING), (None, logging.WARNING),
])
def test_registrar_falha_separa_permanente_de_transitoria(status, nivel,
                                                          caplog):
    log = logging.getLogger('teste.ia')
    exc = _ErroApi(status) if status else TimeoutError('rede')
    with caplog.at_level(logging.WARNING, logger='teste.ia'):
        ia_modelos.registrar_falha(log, 'funcao_x', exc)
    registros = [r for r in caplog.records if r.name == 'teste.ia']
    assert len(registros) == 1
    assert registros[0].levelno == nivel
    assert 'funcao_x' in registros[0].getMessage()


def test_400_numa_funcao_best_effort_vai_como_error(app, monkeypatch,
                                                     caplog):
    """A classe do incidente de 05-17/08/2026: parametro recusado numa
    funcao que antes so logava WARNING."""
    import anthropic
    import httpx

    from app.services import cadastro_ia
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-teste')

    class _Cli:
        def __init__(self, **kw):
            pass

        class messages:
            @staticmethod
            def create(**kw):
                req = httpx.Request('POST', 'https://api.anthropic.com/v1/'
                                    'messages')
                raise anthropic.BadRequestError(
                    'thinking.type: Input tag does not match',
                    response=httpx.Response(400, request=req), body=None)

    monkeypatch.setattr('anthropic.Anthropic', _Cli)
    with app.app_context(), caplog.at_level(logging.WARNING):
        out = cadastro_ia.analisar(texto='Misto R$ 12')
    assert 'erro' in out
    erros = [r for r in caplog.records if r.levelno == logging.ERROR
             and 'cadastro_ia' in r.getMessage()]
    assert erros, 'o 400 tem que sair como ERROR (vai ao Sentry)'


# ── Verificação ao vivo e visibilidade ──────────────────────


def _resp_tool(model='claude-sonnet-5-5'):
    return SimpleNamespace(
        content=[SimpleNamespace(type='thinking', thinking='Vou anotar.'),
                 SimpleNamespace(type='tool_use', name='anotar',
                                 input={'texto': 'ok'})],
        stop_reason='tool_use', stop_details=None, usage=_uso(), model=model)


def test_verificar_ao_vivo_usa_os_parametros_do_sistema(app, api):
    from app.models import UsoIA
    fake = api(_texto('ok'), _resp_tool())
    with app.app_context():
        out = ia_modelos.verificar_ao_vivo()
        assert UsoIA.query.filter_by(funcao='verificacao_ia').count() == 2
    assert out['ok'] is True
    sem, com = fake.chamadas
    assert {k: sem[k] for k in ('thinking', 'output_config')} == \
        _SEM_RACIOCINIO_5_5
    assert 'tools' not in sem
    assert com['thinking'] == {'type': 'adaptive', 'display': 'updates'}
    assert com['tools'][0]['name'] == 'anotar'
    assert out['testes'][1]['textos'] == ['Vou anotar.']
    assert out['testes'][1]['parametros']['beta'] == \
        'thinking-display-updates-2026-08-18'


def test_verificar_ao_vivo_acusa_modelo_trocado(app, api):
    """A API respondeu com outro modelo (fallback, alias) = nao ok."""
    api(_texto('ok', model='claude-sonnet-5'), _resp_tool('claude-sonnet-5'))
    with app.app_context():
        out = ia_modelos.verificar_ao_vivo()
    assert out['ok'] is False


def test_verificar_ao_vivo_nunca_levanta(app, monkeypatch):
    class _Cli:
        def __init__(self, **kw):
            pass

        class messages:
            @staticmethod
            def create(**kw):
                raise RuntimeError('400 thinking.type.between_tools')

    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-teste')
    monkeypatch.setattr('anthropic.Anthropic', _Cli)
    with app.app_context():
        out = ia_modelos.verificar_ao_vivo()
    assert out['ok'] is False
    assert 'between_tools' in out['testes'][0]['erro']


def test_verificar_ao_vivo_sem_chave(app, monkeypatch):
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    with app.app_context():
        out = ia_modelos.verificar_ao_vivo()
    assert out['ok'] is False
    assert 'ANTHROPIC_API_KEY' in out['erro']


def _login(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def test_debug_ia_owner_ve_modelos_sem_chamar_api(app, owner_user,
                                                  monkeypatch):
    def _nao_chame(*a, **k):
        raise AssertionError('sem ?testar=1 nao chama a API')
    monkeypatch.setattr('anthropic.Anthropic', _nao_chame)
    client = app.test_client()
    _login(client, owner_user)
    resp = client.get('/admin/debug-ia')
    assert resp.status_code == 200
    d = resp.get_json()
    assert d['padrao'] == 'claude-sonnet-5-5'
    assert d['por_funcao']['vigia'] == 'claude-sonnet-5-5'
    assert 'verificacao' not in d


def test_debug_ia_testar_roda_a_verificacao(app, owner_user, api):
    api(_texto('ok'), _resp_tool())
    client = app.test_client()
    _login(client, owner_user)
    d = client.get('/admin/debug-ia?testar=1').get_json()
    assert d['verificacao']['ok'] is True
    assert d['verificacao']['modelos'] == ['claude-sonnet-5-5']


def test_debug_ia_exige_owner(app, admin_user):
    client = app.test_client()
    _login(client, admin_user)
    assert client.get('/admin/debug-ia').status_code == 403


def test_sonda_de_deploy_mostra_o_modelo_por_funcao(app):
    app.config['CLAUDE_API_TOKEN'] = 'tok-ia'
    resp = app.test_client().get(
        '/api/claude/deploy', headers={'Authorization': 'Bearer tok-ia'})
    assert resp.status_code == 200
    ia = resp.get_json()['ia']
    assert ia['padrao'] == 'claude-sonnet-5-5'
    assert ia['fora_do_padrao'] == []


# ── Revisão 1: ponto único de chamada e fallback de recusa ───


def _tool_use(nome='consultar_estoque'):
    return SimpleNamespace(type='tool_use', id='tu_1', name=nome,
                           input={'item': 'croissant'})


def test_criar_refaz_recusa_cyber_no_sonnet_5_com_os_parametros_dele(app,
                                                                     api):
    from app.models import UsoIA
    fake = api(_recusa('cyber'), _texto('ok', model='claude-sonnet-5'))
    with app.app_context():
        client = fake()
        resp = ia_modelos.criar(client, 'teste_fb', model='claude-sonnet-5-5',
                                max_tokens=10,
                                messages=[{'role': 'user', 'content': 'x'}])
        modelos = sorted(u.modelo for u in
                         UsoIA.query.filter_by(funcao='teste_fb'))
    assert resp.stop_reason == 'end_turn'
    primeira, segunda = fake.chamadas
    assert primeira['model'] == 'claude-sonnet-5-5'
    assert primeira['thinking'] == {'type': 'between_tools'}
    assert segunda['model'] == 'claude-sonnet-5'
    assert segunda['thinking'] == {'type': 'disabled'}   # nunca between_tools
    assert 'output_config' not in segunda
    # as DUAS tentativas vao para o custo, cada uma com o seu modelo
    assert modelos == ['claude-sonnet-5', 'claude-sonnet-5-5']


def test_criar_com_ferramentas_no_fallback_nao_pede_display(app, api):
    fake = api(_recusa('frontier_llm'), _texto('ok', model='claude-sonnet-5'))
    with app.app_context():
        ia_modelos.criar(fake(), 'teste_fb', ferramentas=True,
                         model='claude-sonnet-5-5', max_tokens=10,
                         messages=[{'role': 'user', 'content': 'x'}])
    segunda = fake.chamadas[1]
    assert segunda['model'] == 'claude-sonnet-5'
    assert 'thinking' not in segunda and 'extra_headers' not in segunda


@pytest.mark.parametrize('categoria', ['general_harms', 'bio',
                                       'reasoning_extraction',
                                       'sem_categoria'])
def test_criar_nao_refaz_as_outras_categorias(app, api, categoria):
    """Igual ao fallback do servidor: so cyber e frontier_llm."""
    fake = api(_recusa(categoria if categoria != 'sem_categoria' else None))
    with app.app_context():
        resp = ia_modelos.criar(fake(), 'teste_fb', model='claude-sonnet-5-5',
                                max_tokens=10, messages=[])
    assert len(fake.chamadas) == 1
    assert resp.stop_reason == 'refusal'


def test_criar_sem_fallback_quando_desligado(app, api, monkeypatch):
    monkeypatch.setenv('IA_FALLBACK_RECUSA', '0')
    fake = api(_recusa('cyber'))
    with app.app_context():
        ia_modelos.criar(fake(), 'teste_fb', model='claude-sonnet-5-5',
                         max_tokens=10, messages=[])
    assert len(fake.chamadas) == 1


def test_criar_modelo_antigo_nao_tem_fallback(app, api):
    fake = api(_recusa('cyber', parcial=''))
    with app.app_context():
        ia_modelos.criar(fake(), 'teste_fb', model='claude-opus-4-8',
                         max_tokens=10, messages=[])
    assert len(fake.chamadas) == 1


def test_fallback_que_tambem_recusa_devolve_a_recusa(app, api):
    fake = api(_recusa('cyber'), _recusa('cyber'))
    with app.app_context():
        resp = ia_modelos.criar(fake(), 'teste_fb', model='claude-sonnet-5-5',
                                max_tokens=10, messages=[])
    assert len(fake.chamadas) == 2
    assert ia_modelos.recusa(resp) == 'cyber'


def test_copilot_fallback_explicacao_vem_do_texto_do_sonnet_5(app,
                                                              admin_user,
                                                              api):
    api(_recusa('cyber'), _texto('Aqui esta o resumo.',
                                  model='claude-sonnet-5'))
    out = _interpretar(app, admin_user)
    assert out['tipo'] != 'erro'
    assert 'Aqui esta o resumo.' in out['explicacao']


def test_ocr_nf_modelo_usado_e_quem_respondeu(app, api):
    from app.services import conta_pagar_ia
    api(_recusa('cyber'), _texto('{"tipo": "boleto"}',
                                  model='claude-sonnet-5'))
    with app.app_context():
        out = conta_pagar_ia.extrair_documento(b'img', 'image/jpeg')
    assert out['modelo_usado'] == 'claude-sonnet-5'


def test_cadastro_ia_modelo_usado_e_quem_respondeu(app, api):
    from app.services import cadastro_ia
    payload = json.dumps({'itens': [{'nome': 'Misto quente', 'preco': 12.0,
                                     'componentes': []}]})
    api(_recusa('cyber'), _texto(payload, model='claude-sonnet-5'))
    with app.app_context():
        out = cadastro_ia.analisar(texto='Misto quente R$ 12')
    assert out.get('modelo_usado') == 'claude-sonnet-5'


def test_treino_modelo_usado_e_quem_respondeu(app, api):
    from app.services import treino_ia_perguntas
    api(_texto('[]'))
    with app.app_context():
        dados, usado = treino_ia_perguntas._chamar('sys', 'instrucao')
    assert dados == [] and usado == 'claude-sonnet-5-5'


def test_planejamento_modelo_servido_vai_para_a_tela(app, api):
    from app.services import planejamento_ia
    api(_recusa('cyber'), _texto('{"a": 1}', model='claude-sonnet-5'))
    with app.app_context():
        dados, erro = planejamento_ia._chamar_opus('sys', 'payload', 'teste')
    assert erro is None
    assert dados['_modelo_servido'] == 'claude-sonnet-5'


def test_planejamento_recusa_na_segunda_resposta(app, api):
    """A resposta do retry de truncamento tambem e conferida."""
    from app.services import planejamento_ia
    api(_texto('{"a"', stop_reason='max_tokens'), _recusa())
    with app.app_context():
        dados, erro = planejamento_ia._chamar_opus('sys', 'payload', 'teste')
    assert dados is None and 'recusou' in erro


def test_vigia_abandono_recusa_vira_erro(app, api):
    from app.services import chatbot_vigia
    api(_recusa())
    with app.app_context(), pytest.raises(ia_modelos.RespostaRecusada):
        chatbot_vigia._chamar_modelo_abandono('sk', 'contexto')


# Recusa com tool_use PARCIAL: nada pode ser executado.


def test_copilot_recusa_com_tool_use_nao_executa(app, admin_user, api,
                                                 monkeypatch):
    from app.services import copilot

    def _proibido(*a, **k):
        raise AssertionError('executou ferramenta de resposta recusada')
    monkeypatch.setattr(copilot, '_executar_read', _proibido)
    monkeypatch.setattr(copilot, '_enriquecer_params', _proibido)
    recusada = _recusa('general_harms')
    recusada.content = [_tool_use()]
    api(recusada)
    out = _interpretar(app, admin_user)
    assert out['tipo'] == 'erro'


def test_bot_recusa_com_tool_use_nao_executa(app, api, monkeypatch):
    from app.services import chatbot

    def _proibido(*a, **k):
        raise AssertionError('executou ferramenta de resposta recusada')
    monkeypatch.setattr(chatbot, '_executar_tool', _proibido)
    recusada = _recusa('general_harms')
    recusada.content = [_tool_use('consultar_produtos')]
    api(recusada)
    with app.app_context():
        out = chatbot._responder_modelo_offline(
            [{'role': 'user', 'content': 'tem croissant?'}])
    assert out['acao'] == 'handoff'


# Modelo configurado (ZAPI_BOT_MODELO) com a MESMA regra na chamada e na
# visao por funcao.


@pytest.mark.parametrize('valor, esperado', [
    ('', 'claude-sonnet-5-5'), ('   ', 'claude-sonnet-5-5'),
    (None, 'claude-sonnet-5-5'), (' claude-sonnet-5 \n', 'claude-sonnet-5'),
])
def test_modelo_configurado_normaliza(valor, esperado):
    assert ia_modelos.modelo_configurado(valor) == esperado


def test_whatsapp_do_dono_usa_o_mesmo_modelo_que_a_sonda_mostra(
        app, admin_user, monkeypatch):
    from unittest.mock import patch

    from app.extensions import db
    from app.models import Usuario
    from app.services import zapi_bot
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-teste')
    app.config['ZAPI_BOT_DONO_NUMERO'] = '5511999990000'
    app.config['ZAPI_BOT_WEBHOOK_TOKEN'] = 'tok'
    app.config['ZAPI_BOT_MODELO'] = '   '
    with app.app_context():
        u = Usuario.query.get(admin_user.id)
        u.is_owner = True
        db.session.commit()
        with patch('app.services.copilot.interpretar',
                   return_value={'tipo': 'conversa', 'explicacao': 'oi'}) \
                as fake, patch('app.services.zapi_bot._responder'):
            zapi_bot.processar_payload({
                'phone': '5511999990000', 'messageId': 'm-norm-1',
                'text': {'message': 'oi'}})
        enviado = fake.call_args[1]['modelo']
        visto = ia_modelos.modelos_por_funcao()['por_funcao'][
            'copilot_whatsapp']
    assert enviado == visto == 'claude-sonnet-5-5'


# Classificacao do erro da API.


def test_type_error_do_sdk_e_permanente(caplog):
    """A causa do incidente de 05-17/08/2026 nem chega a ter status HTTP."""
    log = logging.getLogger('teste.ia')
    with caplog.at_level(logging.WARNING, logger='teste.ia'):
        ia_modelos.registrar_falha(
            log, 'f', TypeError("create() got an unexpected keyword "
                                "argument 'thinking'"))
    assert caplog.records[-1].levelno == logging.ERROR


@pytest.mark.parametrize('status', [408, 409])
def test_timeout_e_conflito_sao_transitorios(status):
    assert ia_modelos.falha_permanente(_ErroApi(status)) is False


def test_erro_de_conexao_do_sdk_e_transitorio():
    import anthropic
    import httpx
    req = httpx.Request('POST', 'https://api.anthropic.com/v1/messages')
    assert ia_modelos.falha_permanente(
        anthropic.APIConnectionError(request=req)) is False
    assert ia_modelos.falha_permanente(
        anthropic.APITimeoutError(request=req)) is False


# Auditor: recusa consome a janela e avisa uma vez.


def test_auditor_recusa_avanca_o_ponteiro_e_avisa(app, api, monkeypatch):
    from app.models import AppConfig
    from app.services import chatbot_auditor, zapi
    api(_recusa())
    enviados = []
    monkeypatch.setattr(chatbot_auditor, '_coletar_periodo',
                        lambda i, f: {'conversas': 3})
    monkeypatch.setattr(chatbot_auditor, '_numero_destino',
                        lambda: '5511999990000')
    monkeypatch.setattr(zapi, 'enviar_texto',
                        lambda n, m: enviados.append(m) or {'ok': True})
    with app.app_context():
        res = chatbot_auditor.auditar_janela_pendente(enviar=True)
        ponteiro = AppConfig.get(chatbot_auditor.CHAVE_ULTIMA_EXEC)
    assert 'erro' not in res
    assert res['recusa'] == 'general_harms'
    assert ponteiro, 'a janela recusada tem que ser dada como consumida'
    assert len(enviados) == 1 and 'recusou' in enviados[0]


def test_auditor_erro_de_rede_nao_avanca(app, monkeypatch):
    from app.models import AppConfig
    from app.services import chatbot_auditor
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-teste')
    monkeypatch.setattr(chatbot_auditor, '_coletar_periodo',
                        lambda i, f: {'conversas': 3})

    def _falha(*a, **k):
        raise ConnectionError('rede')
    monkeypatch.setattr(chatbot_auditor, '_chamar_sonnet', _falha)
    with app.app_context():
        res = chatbot_auditor.auditar_janela_pendente(enviar=False)
        assert AppConfig.get(chatbot_auditor.CHAVE_ULTIMA_EXEC) is None
    assert 'erro' in res


# Verificacao ao vivo de CADA modelo em uso.


def test_debug_ia_testar_cobre_os_modelos_das_envs(app, owner_user, api,
                                                   monkeypatch):
    monkeypatch.setenv('GOOGLE_REVIEWS_IA_MODELO', 'claude-opus-4-8')
    fake = api(_texto('ok'), _resp_tool(), _texto('ok', model='claude-opus-4-8'),
               _resp_tool('claude-opus-4-8'))
    client = app.test_client()
    _login(client, owner_user)
    d = client.get('/admin/debug-ia?testar=1').get_json()
    assert d['verificacao']['modelos'] == ['claude-opus-4-8',
                                           'claude-sonnet-5-5']
    assert set(d['verificacao']['por_modelo']) == {'claude-opus-4-8',
                                                   'claude-sonnet-5-5'}
    usados = [c['model'] for c in fake.chamadas]
    assert usados.count('claude-opus-4-8') == 2
    assert usados.count('claude-sonnet-5-5') == 2
    # o Opus 4.8 recebeu os parametros DELE
    opus = [c for c in fake.chamadas if c['model'] == 'claude-opus-4-8']
    assert opus[0]['thinking'] == {'type': 'disabled'}


# Preco de cache read por modelo.


@pytest.mark.parametrize('modelo, esperado', [
    ('claude-sonnet-5-5', '0.20'), ('claude-opus-5-5', '0.20'),
    ('claude-fable-5-1', '0.25'), ('claude-fable-5', '1.0'),
    ('claude-opus-4-8', '0.5'),
])
def test_cache_read_pelo_preco_de_cada_modelo(modelo, esperado):
    from decimal import Decimal

    from app.services.uso_ia import calcular_custo
    assert calcular_custo(modelo, 0, 0, cache_read=1_000_000) == \
        Decimal(esperado)
