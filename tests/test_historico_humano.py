"""Tradução de registros técnicos pra linguagem natural.

Cobre o tradutor do AuditLog (frase + diff de campos), formatadores de valores
e os mapas {tipo → rotulo} usados pelos templates de historico.
"""
from datetime import date, datetime
from types import SimpleNamespace


def _log(acao='update', tabela='pedido_loja', registro_id=128, usuario_nome='Marina'):
    """Stub de AuditLog mínimo pro tradutor."""
    usuario = SimpleNamespace(nome=usuario_nome) if usuario_nome else None
    return SimpleNamespace(
        acao=acao, tabela=tabela, registro_id=registro_id, usuario=usuario,
        criado_em=datetime(2026, 5, 29, 14, 32),
    )


def test_formatar_valor_basicos():
    from app.services.historico_humano import formatar_valor
    assert formatar_valor(None) == '—'
    assert formatar_valor('') == '—'
    assert formatar_valor(True) == 'sim'
    assert formatar_valor(False) == 'não'
    assert formatar_valor(42) == '42'


def test_formatar_valor_datas():
    from app.services.historico_humano import formatar_valor
    assert formatar_valor(date(2026, 5, 29)) == '29/05/2026'
    assert formatar_valor(datetime(2026, 5, 29, 14, 32)) == '29/05/2026 14:32'
    # strings ISO (vem do JSON do audit log)
    assert formatar_valor('2026-05-29') == '29/05/2026'
    assert formatar_valor('2026-05-29T14:32:00') == '29/05/2026 14:32'


def test_formatar_valor_trunca_strings_longas():
    from app.services.historico_humano import formatar_valor
    s = 'x' * 200
    r = formatar_valor(s)
    assert r.endswith('…')
    assert len(r) <= 120


def test_labels_centralizados():
    from app.services.historico_humano import (
        mov_loja_label,
        mov_mp_label,
        mov_producao_label,
    )
    assert mov_loja_label('venda_seru') == 'Venda no PDV (Seru)'
    assert mov_loja_label('consolidacao_estado') == 'Juntou linhas duplicadas'
    assert mov_producao_label('balanco') == 'Balanço (correção)'
    assert mov_mp_label('entrada').startswith('Entrada')
    # tipo desconhecido cai no fallback (o próprio código)
    assert mov_loja_label('xyz_nao_existe') == 'xyz_nao_existe'


def test_handshake_labels():
    from app.services.historico_humano import (
        handshake_etapa_label,
        handshake_tipo_label,
    )
    assert 'PIN correto' in handshake_etapa_label('pin_ok')
    assert 'PIN incorreto' in handshake_etapa_label('pin_fail')
    assert handshake_tipo_label('saida') == 'Saída da indústria'


def test_traduzir_audit_insert_com_nome():
    from app.services.historico_humano import traduzir_audit
    log = _log(acao='insert', tabela='loja', registro_id=5)
    depois = {'id': 5, 'nome': 'Loja Centro'}
    t = traduzir_audit(log, None, depois)
    assert 'Marina criou loja "Loja Centro"' == t['frase']
    assert t['mudancas'] == []


def test_traduzir_audit_delete():
    from app.services.historico_humano import traduzir_audit
    log = _log(acao='delete', tabela='funcionario', registro_id=7,
               usuario_nome='Alane')
    antes = {'id': 7, 'nome': 'João Pedro'}
    t = traduzir_audit(log, antes, None)
    assert 'Alane excluiu funcionário "João Pedro"' == t['frase']


def test_traduzir_audit_update_com_diff():
    from app.services.historico_humano import traduzir_audit
    log = _log(acao='update', tabela='pedido_loja', registro_id=128)
    antes = {'id': 128, 'data_entrega': '2026-05-29', 'observacao': None}
    depois = {'id': 128, 'data_entrega': '2026-05-30', 'observacao': 'urgente'}
    t = traduzir_audit(log, antes, depois)
    # tem nome amigável de tabela + identificador + ambos os campos no diff
    assert 'Marina editou pedido #128' in t['frase']
    assert 'data de entrega: 29/05/2026 → 30/05/2026' in t['frase']
    assert 'observação: — → urgente' in t['frase']
    # mudancas estruturadas pra a UI exibir item por item
    campos = {m['campo'] for m in t['mudancas']}
    assert campos == {'data de entrega', 'observação'}


def test_traduzir_audit_update_suprime_ruido():
    """Timestamps automáticos (modificado_em, etc.) não entram no diff humano."""
    from app.services.historico_humano import traduzir_audit
    log = _log(acao='update', tabela='pedido_loja', registro_id=42)
    antes = {'id': 42, 'observacao': 'antiga', 'modificado_em': '2026-05-29T10:00'}
    depois = {'id': 42, 'observacao': 'nova', 'modificado_em': '2026-05-29T11:00'}
    t = traduzir_audit(log, antes, depois)
    campos = {m['campo'] for m in t['mudancas']}
    assert campos == {'observação'}  # modificado_em foi suprimido


def test_traduzir_audit_update_sem_mudanca():
    from app.services.historico_humano import traduzir_audit
    log = _log(acao='update', tabela='pedido_loja', registro_id=1)
    t = traduzir_audit(log, {'id': 1, 'observacao': 'a'}, {'id': 1, 'observacao': 'a'})
    assert 'sem mudanças detectadas' in t['frase']
    assert t['mudancas'] == []


def test_traduzir_audit_sistema_quando_sem_usuario():
    from app.services.historico_humano import traduzir_audit
    log = _log(acao='insert', tabela='loja', registro_id=9, usuario_nome=None)
    t = traduzir_audit(log, None, {'id': 9, 'nome': 'X'})
    assert t['frase'].startswith('Sistema criou loja')


def test_traduzir_audit_muitas_mudancas_resume():
    """Diff com >3 mudanças mostra as primeiras 3 + '+N outras' na frase."""
    from app.services.historico_humano import traduzir_audit
    log = _log(acao='update', tabela='receita', registro_id=10)
    antes = {f'campo_{i}': i for i in range(6)}
    depois = {f'campo_{i}': i + 1 for i in range(6)}
    t = traduzir_audit(log, antes, depois)
    # frase tem '+N outras' no fim
    assert '+3 outras' in t['frase']
    # lista completa fica em mudancas (não trunca a estrutura)
    assert len(t['mudancas']) == 6
