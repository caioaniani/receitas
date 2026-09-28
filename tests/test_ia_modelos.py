"""Fonte única do modelo de IA e dos parâmetros que dependem dele
(`app/services/ia_modelos.py`, troca para o Sonnet 5.5 em 28/09/2026).

Trava quatro coisas:
1. O parâmetro de raciocínio sai do MODELO QUE VAI RODAR — o Sonnet 5.5
   recusa `thinking: disabled` (400) e só ele aceita `between_tools`; uma env
   do Railway com modelo antigo nunca pode receber `between_tools`.
2. Recusa das salvaguardas (`stop_reason='refusal'`, HTTP 200) nunca é lida
   como resposta.
3. O SDK INSTALADO serializa esses parâmetros e lê `stop_details` (a suíte
   simula a Anthropic; sem este teste, um SDK velho que não conhece o campo
   só apareceria em produção — incidente de 05-17/08/2026).
4. Nenhuma chamada da API no app escreve `thinking`/`output_config` à mão nem
   deixa de passar pelos helpers (trava por AST).
"""
import ast
import json
import logging
import pathlib
from types import SimpleNamespace

import pytest

from app.services import ia_modelos

# ── 1. Família e parâmetros ─────────────────────────────────


@pytest.mark.parametrize('modelo, esperada', [
    ('claude-sonnet-5-5', 'claude-sonnet-5-5'),
    ('claude-sonnet-5-5-20260925', 'claude-sonnet-5-5'),
    ('anthropic.claude-sonnet-5-5', 'claude-sonnet-5-5'),
    ('claude-sonnet-5-5@20260925', 'claude-sonnet-5-5'),
    ('CLAUDE-SONNET-5-5', 'claude-sonnet-5-5'),
    ('claude-sonnet-5', 'claude-sonnet-5'),
    ('claude-sonnet-5-20260801', 'claude-sonnet-5'),
    ('claude-opus-5-5', 'claude-opus-5-5'),
    ('claude-opus-5', 'claude-opus-5'),
    ('claude-fable-5-1', 'claude-fable-5'),
    ('claude-opus-4-8', 'claude-opus-4-8'),
    ('claude-haiku-4-5-20251001', 'claude-haiku-4-5'),
    ('claude-sonnet-55', None),     # sem fronteira nao casa
    ('gpt-9', None),
    ('', None),
    (None, None),
])
def test_familia_pelo_prefixo_mais_longo(modelo, esperada):
    assert ia_modelos.familia(modelo) == esperada


def test_sonnet_5_5_desliga_com_between_tools_e_effort_explicito():
    opcoes = ia_modelos.opcoes_sem_raciocinio('claude-sonnet-5-5')
    assert opcoes == {'thinking': {'type': 'between_tools'},
                      'output_config': {'effort': 'high'}}
    # between_tools nao aceita nenhum outro campo dentro de thinking
    assert set(opcoes['thinking']) == {'type'}


@pytest.mark.parametrize('modelo', [
    'claude-sonnet-5', 'claude-opus-5', 'claude-opus-4-8',
    'claude-opus-4-7', 'claude-opus-4-6', 'claude-sonnet-4-6'])
def test_modelos_anteriores_seguem_com_disabled(modelo):
    """Env antiga (ex.: OCR_MODELO_OPUS=claude-opus-4-8) nunca recebe o
    parametro que so o 5.5 aceita."""
    assert ia_modelos.opcoes_sem_raciocinio(modelo) == {
        'thinking': {'type': 'disabled'}}


@pytest.mark.parametrize('modelo', ['claude-opus-5-5', 'claude-fable-5-1'])
def test_modelos_que_nao_desligam_usam_effort_low(modelo):
    assert ia_modelos.opcoes_sem_raciocinio(modelo) == {
        'output_config': {'effort': 'low'}}


@pytest.mark.parametrize('modelo', [
    'claude-haiku-4-5', 'claude-sonnet-4-5', 'modelo-desconhecido', '', None])
def test_sem_parametro_quando_o_modelo_nao_precisa_ou_e_desconhecido(modelo):
    assert ia_modelos.opcoes_sem_raciocinio(modelo) == {}
    assert ia_modelos.opcoes_com_ferramentas(modelo) == {}


def test_nenhum_modelo_alem_do_5_5_recebe_between_tools():
    for chave in ia_modelos._FAMILIAS:
        opcoes = ia_modelos.opcoes_sem_raciocinio(chave)
        tipo = (opcoes.get('thinking') or {}).get('type')
        if chave == 'claude-sonnet-5-5':
            assert tipo == 'between_tools'
        else:
            assert tipo != 'between_tools', chave


def test_com_ferramentas_no_5_5_pede_notas_de_progresso():
    opcoes = ia_modelos.opcoes_com_ferramentas('claude-sonnet-5-5')
    assert opcoes['thinking'] == {'type': 'adaptive', 'display': 'updates'}
    assert opcoes['extra_headers'] == {
        'anthropic-beta': 'thinking-display-updates-2026-08-18'}
    assert opcoes['output_config'] == {'effort': 'high'}
    assert ia_modelos.mostra_progresso(opcoes) is True


def test_com_ferramentas_em_modelo_antigo_nao_manda_nada():
    """Sonnet 5 / Opus 4.8 devolvem o texto entre tools como `text` — o
    comportamento de antes, sem beta nem display."""
    for modelo in ('claude-sonnet-5', 'claude-opus-4-8'):
        opcoes = ia_modelos.opcoes_com_ferramentas(modelo)
        assert opcoes == {}
        assert ia_modelos.mostra_progresso(opcoes) is False


def test_opcoes_sao_copias():
    """Mutar o dict devolvido nao pode contaminar a tabela de familias."""
    a = ia_modelos.opcoes_sem_raciocinio('claude-sonnet-5-5')
    a['thinking']['type'] = 'disabled'
    b = ia_modelos.opcoes_sem_raciocinio('claude-sonnet-5-5')
    assert b['thinking']['type'] == 'between_tools'


# ── Modelo por env ──────────────────────────────────────────


def test_modelo_do_ambiente_sem_env_e_o_padrao(monkeypatch):
    monkeypatch.delenv('X_MODELO_TESTE', raising=False)
    assert ia_modelos.modelo_do_ambiente('X_MODELO_TESTE') == \
        'claude-sonnet-5-5'
    assert ia_modelos.modelo_do_ambiente() == 'claude-sonnet-5-5'


@pytest.mark.parametrize('valor', ['', '   '])
def test_modelo_do_ambiente_env_vazia_vale_o_padrao(monkeypatch, valor):
    """Antes, `os.environ.get(VAR, padrao)` devolvia '' e a chamada saia sem
    modelo."""
    monkeypatch.setenv('X_MODELO_TESTE', valor)
    assert ia_modelos.modelo_do_ambiente('X_MODELO_TESTE') == \
        'claude-sonnet-5-5'


def test_modelo_do_ambiente_env_manda(monkeypatch):
    monkeypatch.setenv('X_MODELO_TESTE', ' claude-opus-4-8 ')
    assert ia_modelos.modelo_do_ambiente('X_MODELO_TESTE') == \
        'claude-opus-4-8'


def test_modelo_do_ambiente_desconhecido_fica_no_log(monkeypatch, caplog):
    monkeypatch.setenv('X_MODELO_TESTE', 'modelo-inventado')
    with caplog.at_level(logging.WARNING, logger='app.services.ia_modelos'):
        assert ia_modelos.modelo_do_ambiente('X_MODELO_TESTE') == \
            'modelo-inventado'
    assert 'X_MODELO_TESTE' in caplog.text


# ── 2. Recusa e textos ──────────────────────────────────────


def _resp(stop_reason='end_turn', categoria=None, content=None,
          model='claude-sonnet-5-5'):
    detalhes = SimpleNamespace(category=categoria) if categoria else None
    return SimpleNamespace(stop_reason=stop_reason, stop_details=detalhes,
                           content=content or [], model=model)


def test_recusa_devolve_a_categoria():
    assert ia_modelos.recusa(_resp('refusal', 'general_harms')) == \
        'general_harms'
    assert ia_modelos.recusa(_resp('refusal')) == 'sem_categoria'
    assert ia_modelos.recusa(_resp('end_turn')) is None
    assert ia_modelos.recusa(_resp('max_tokens')) is None


def test_exigir_resposta_levanta_com_mensagem_legivel():
    with pytest.raises(ia_modelos.RespostaRecusada) as exc:
        ia_modelos.exigir_resposta(_resp('refusal', 'cyber'))
    assert exc.value.categoria == 'cyber'
    assert 'recusou' in str(exc.value)
    assert 'cyber' in str(exc.value)
    ok = _resp('end_turn')
    assert ia_modelos.exigir_resposta(ok) is ok


def _bloco(tipo, **kw):
    return SimpleNamespace(type=tipo, **kw)


def test_textos_visiveis_sem_progresso_so_text():
    resp = _resp(content=[
        _bloco('thinking', thinking='nota de progresso'),
        _bloco('text', text='  resposta  '),
        _bloco('tool_use', name='x', input={}),
    ])
    assert ia_modelos.textos_visiveis(resp) == ['resposta']


def test_textos_visiveis_com_progresso_inclui_notas_em_ordem():
    resp = _resp(content=[
        _bloco('thinking', thinking='Vou buscar o pedido.'),
        _bloco('thinking', thinking=''),           # raciocinio omitido
        _bloco('redacted_thinking', data='xx'),
        _bloco('text', text='Achei.'),
        _bloco('tool_use', name='x', input={}),
    ])
    assert ia_modelos.textos_visiveis(resp, progresso=True) == [
        'Vou buscar o pedido.', 'Achei.']


def test_mesmo_modelo_compara_familia():
    assert ia_modelos._mesmo_modelo('claude-sonnet-5-5-20260925',
                                    'claude-sonnet-5-5')
    # o 5 comeca com o mesmo prefixo, mas nao e o 5.5
    assert not ia_modelos._mesmo_modelo('claude-sonnet-5',
                                        'claude-sonnet-5-5')


# ── 3. O SDK instalado aceita os parametros ─────────────────


def _cliente_com_transporte(resposta_json, capturado):
    import anthropic
    import httpx

    def handler(request):
        capturado['headers'] = dict(request.headers)
        capturado['body'] = json.loads(request.content)
        return httpx.Response(200, json=resposta_json)

    return anthropic.Anthropic(
        api_key='sk-teste', max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def _msg_json(**extra):
    base = {
        'id': 'msg_1', 'type': 'message', 'role': 'assistant',
        'model': 'claude-sonnet-5-5', 'content': [
            {'type': 'text', 'text': 'ok'}],
        'stop_reason': 'end_turn', 'stop_sequence': None,
        'usage': {'input_tokens': 5, 'output_tokens': 1},
    }
    base.update(extra)
    return base


def test_sdk_serializa_between_tools_e_effort():
    capturado = {}
    client = _cliente_com_transporte(_msg_json(), capturado)
    client.messages.create(
        model='claude-sonnet-5-5', max_tokens=16,
        messages=[{'role': 'user', 'content': 'oi'}],
        **ia_modelos.opcoes_sem_raciocinio('claude-sonnet-5-5'))
    assert capturado['body']['thinking'] == {'type': 'between_tools'}
    assert capturado['body']['output_config'] == {'effort': 'high'}


def test_sdk_serializa_display_updates_e_o_beta():
    capturado = {}
    resposta = _msg_json(content=[
        {'type': 'thinking', 'thinking': 'Vou anotar.', 'signature': 'sig'},
        {'type': 'tool_use', 'id': 'tu_1', 'name': 'anotar',
         'input': {'texto': 'ok'}},
    ], stop_reason='tool_use')
    client = _cliente_com_transporte(resposta, capturado)
    opcoes = ia_modelos.opcoes_com_ferramentas('claude-sonnet-5-5')
    resp = client.messages.create(
        model='claude-sonnet-5-5', max_tokens=100,
        tools=[{'name': 'anotar', 'description': 'x', 'input_schema': {
            'type': 'object', 'properties': {}}}],
        messages=[{'role': 'user', 'content': 'oi'}], **opcoes)
    assert capturado['body']['thinking'] == {'type': 'adaptive',
                                             'display': 'updates'}
    assert 'extra_headers' not in capturado['body']
    assert 'thinking-display-updates-2026-08-18' in \
        capturado['headers'].get('anthropic-beta', '')
    # a nota de progresso volta legivel pelo helper
    assert ia_modelos.textos_visiveis(
        resp, progresso=ia_modelos.mostra_progresso(opcoes)) == ['Vou anotar.']


def test_sdk_le_a_categoria_da_recusa():
    client = _cliente_com_transporte(_msg_json(
        content=[], stop_reason='refusal',
        stop_details={'type': 'refusal', 'category': 'general_harms',
                      'explanation': None}), {})
    resp = client.messages.create(
        model='claude-sonnet-5-5', max_tokens=16,
        messages=[{'role': 'user', 'content': 'oi'}])
    assert ia_modelos.recusa(resp) == 'general_harms'


# ── 4. Trava por AST: toda chamada passa pelos helpers ──────

_APP = pathlib.Path(__file__).resolve().parent.parent / 'app'
_HELPERS = {'opcoes_sem_raciocinio', 'opcoes_com_ferramentas'}


def _chamadas_da_api():
    """(arquivo, linha, nó) de toda chamada que manda `model=` à API:
    `<x>.messages.create(...)` e o wrapper do bot
    `_chamar_com_retry_sobrecarga(...)`."""
    achadas = []
    for arq in sorted(_APP.rglob('*.py')):
        arvore = ast.parse(arq.read_text(encoding='utf-8'))
        for no in ast.walk(arvore):
            if not isinstance(no, ast.Call):
                continue
            f = no.func
            eh_create = (isinstance(f, ast.Attribute) and f.attr == 'create'
                         and isinstance(f.value, ast.Attribute)
                         and f.value.attr == 'messages')
            eh_wrapper = (isinstance(f, ast.Name)
                          and f.id == '_chamar_com_retry_sobrecarga')
            if not (eh_create or eh_wrapper):
                continue
            if not any(k.arg == 'model' for k in no.keywords):
                continue  # o proprio wrapper repassa **kwargs
            achadas.append((arq, no.lineno, no, arvore))
    return achadas


def _opcao_vem_do_helper(no_kw, arvore):
    """O `**x` da chamada e `ia_modelos.opcoes_*(...)` direto ou um nome
    atribuido a partir de um deles no mesmo arquivo."""
    valor = no_kw.value
    if isinstance(valor, ast.Call) and isinstance(valor.func, ast.Attribute):
        return valor.func.attr in _HELPERS
    if isinstance(valor, ast.Name):
        for no in ast.walk(arvore):
            if (isinstance(no, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == valor.id
                            for t in no.targets)
                    and isinstance(no.value, ast.Call)
                    and isinstance(no.value.func, ast.Attribute)
                    and no.value.func.attr in _HELPERS):
                return True
        # parametro de funcao interna (verificar_ao_vivo._rodar recebe
        # `opcoes` ja montado pelos helpers)
        return valor.id == 'opcoes'
    return False


def test_universo_de_chamadas_conhecido():
    """Se aparecer chamada nova, este numero muda e obriga a olhar se ela
    passa pelos helpers (os outros testes da secao ja cobrem)."""
    arquivos = sorted({a.name for a, _, _, _ in _chamadas_da_api()})
    assert arquivos == sorted([
        'cadastro_ia.py', 'chatbot.py', 'chatbot_auditor.py',
        'chatbot_vigia.py', 'conta_pagar_ia.py', 'copilot.py',
        'google_reviews.py', 'ia_modelos.py', 'ocr_nota.py',
        'planejamento_ia.py', 'seo_descricoes.py', 'treino_ia_perguntas.py',
    ])
    # 14 chamadas do sistema (o bot conta 1: o wrapper de retry) + a da
    # verificacao ao vivo
    assert len(_chamadas_da_api()) == 15


def test_nenhuma_chamada_escreve_raciocinio_a_mao():
    for arq, linha, no, _ in _chamadas_da_api():
        literais = {k.arg for k in no.keywords} & {'thinking',
                                                   'output_config'}
        assert not literais, f'{arq.name}:{linha} escreve {literais} a mao'


def test_toda_chamada_passa_pelos_helpers():
    for arq, linha, no, arvore in _chamadas_da_api():
        expandidos = [k for k in no.keywords if k.arg is None]
        assert any(_opcao_vem_do_helper(k, arvore) for k in expandidos), \
            f'{arq.name}:{linha} nao usa ia_modelos.opcoes_*'


def test_todo_arquivo_com_chamada_trata_recusa():
    for arq in {a for a, _, _, _ in _chamadas_da_api()}:
        if arq.name == 'ia_modelos.py':
            continue
        src = arq.read_text(encoding='utf-8')
        assert ('ia_modelos.recusa(' in src
                or 'ia_modelos.exigir_resposta(' in src), \
            f'{arq.name} nao trata stop_reason=refusal'


def test_nenhum_id_de_modelo_literal_fora_da_fonte_unica():
    permitidos = {'ia_modelos.py', 'uso_ia.py'}
    for arq in _APP.rglob('*.py'):
        if arq.name in permitidos:
            continue
        arvore = ast.parse(arq.read_text(encoding='utf-8'))
        for no in ast.walk(arvore):
            if (isinstance(no, ast.Constant) and isinstance(no.value, str)
                    and no.value.startswith('claude-')):
                pytest.fail(f'{arq.name}:{no.lineno} tem id literal '
                            f'{no.value!r} — use ia_modelos')
