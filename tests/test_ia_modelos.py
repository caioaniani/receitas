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


# ── 4. Trava por AST: toda chamada passa por ia_modelos.criar ─

_APP = pathlib.Path(__file__).resolve().parent.parent / 'app'
_HELPERS = {'opcoes_sem_raciocinio', 'opcoes_com_ferramentas'}
# Encaminha a `ia_modelos.criar` com um retry de sobrecarga (chatbot).
_WRAPPERS = {'_chamar_com_retry_sobrecarga'}


def _arvores():
    for arq in sorted(_APP.rglob('*.py')):
        yield arq, ast.parse(arq.read_text(encoding='utf-8'))


def _eh_messages_create(no):
    f = no.func
    return (isinstance(f, ast.Attribute) and f.attr == 'create'
            and isinstance(f.value, ast.Attribute)
            and f.value.attr == 'messages')


def _eh_criar(no):
    f = no.func
    if isinstance(f, ast.Attribute) and f.attr == 'criar' \
            and isinstance(f.value, ast.Name) and f.value.id == 'ia_modelos':
        return True
    return isinstance(f, ast.Name) and f.id in _WRAPPERS


def _chamadas_criar():
    """(arquivo, função, nó) de cada chamada de IA do sistema: `ia_modelos.
    criar(...)` ou o wrapper do bot com `model=` (o próprio wrapper repassa
    **kwargs e fica de fora)."""
    achadas = []
    for arq, arvore in _arvores():
        for func in ast.walk(arvore):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for no in ast.walk(func):
                if (isinstance(no, ast.Call) and _eh_criar(no)
                        and any(k.arg == 'model' for k in no.keywords)):
                    achadas.append((arq, func, no))
    return achadas


def test_messages_create_so_dentro_de_ia_modelos():
    """Ninguem chama a API direto: os parametros por modelo, o custo e o
    fallback de recusa ficam num lugar so."""
    for arq, arvore in _arvores():
        for no in ast.walk(arvore):
            if isinstance(no, ast.Call) and _eh_messages_create(no):
                assert arq.name == 'ia_modelos.py', \
                    f'{arq.name}:{no.lineno} chama messages.create direto'


def test_helpers_de_parametros_so_dentro_de_ia_modelos():
    for arq, arvore in _arvores():
        if arq.name == 'ia_modelos.py':
            continue
        for no in ast.walk(arvore):
            if (isinstance(no, ast.Call)
                    and isinstance(no.func, ast.Attribute)
                    and no.func.attr in _HELPERS):
                pytest.fail(f'{arq.name}:{no.lineno} monta parametros de '
                            'raciocinio fora do ia_modelos.criar')


def test_universo_de_chamadas_conhecido():
    """Se aparecer chamada nova, a contagem muda e obriga a olhar a secao."""
    achadas = _chamadas_criar()
    por_arquivo = {}
    for arq, _, _ in achadas:
        por_arquivo[arq.name] = por_arquivo.get(arq.name, 0) + 1
    assert por_arquivo == {
        'cadastro_ia.py': 1, 'chatbot.py': 2, 'chatbot_auditor.py': 1,
        'chatbot_vigia.py': 2, 'conta_pagar_ia.py': 1, 'copilot.py': 1,
        'google_reviews.py': 1, 'ocr_nota.py': 1, 'planejamento_ia.py': 2,
        'seo_descricoes.py': 1, 'treino_ia_perguntas.py': 1,
    }


def test_nenhuma_chamada_escreve_raciocinio_a_mao():
    proibidos = {'thinking', 'output_config', 'extra_headers', 'extra_body'}
    for arq, _, no in _chamadas_criar():
        literais = {k.arg for k in no.keywords} & proibidos
        assert not literais, f'{arq.name}:{no.lineno} escreve {literais}'
        assert not any(k.arg is None for k in no.keywords), \
            f'{arq.name}:{no.lineno} expande **kwargs numa chamada de IA'


def _alvo_da_atribuicao(func, chamada):
    for no in ast.walk(func):
        if isinstance(no, ast.Assign) and no.value is chamada \
                and len(no.targets) == 1 \
                and isinstance(no.targets[0], ast.Name):
            return no.targets[0].id
    return None


def test_toda_chamada_confere_recusa_antes_de_usar():
    """POR CHAMADA (nao por arquivo): o resultado de cada criar passa por
    `ia_modelos.recusa`/`exigir_resposta` depois dela e antes de a mesma
    variavel receber outra resposta."""
    for arq, func, no in _chamadas_criar():
        nome = _alvo_da_atribuicao(func, no)
        assert nome, f'{arq.name}:{no.lineno} resposta sem variavel'
        proxima = min((n.lineno for n in ast.walk(func)
                       if isinstance(n, ast.Assign) and n.lineno > no.lineno
                       and any(isinstance(t, ast.Name) and t.id == nome
                               for t in n.targets)), default=10**9)
        conferida = any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr in {'recusa', 'exigir_resposta'}
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == 'ia_modelos'
            and n.args and isinstance(n.args[0], ast.Name)
            and n.args[0].id == nome
            and no.lineno < n.lineno < proxima
            for n in ast.walk(func))
        assert conferida, (f'{arq.name}:{no.lineno} ({func.name}) nao '
                           f'confere a recusa de {nome!r}')


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
