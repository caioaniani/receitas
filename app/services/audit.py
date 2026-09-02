"""Audit log automatico via SQLAlchemy events.

Conecta listeners em `before_flush` e `after_flush` que capturam mutacoes
nos modelos REGISTRADOS abaixo e inserem registros em audit_log.

Pra ativar pra um modelo novo, adicione em AUDITED_MODELS.

NAO audita o proprio AuditLog (evita recursao infinita) nem modelos
de alto volume/baixo valor (logs de sessao, caches, etc).
"""
import json
import logging
from datetime import date, datetime
from decimal import Decimal

from flask import has_request_context, request
from flask_login import current_user
from sqlalchemy import event, inspect

from app.extensions import db

logger = logging.getLogger(__name__)


# Modelos sensiveis cujas mutacoes geram auditoria.
# Mantenha pequeno — auditoria tem overhead e gera volume.
AUDITED_MODELS = {
    'delegacao_fiscal_b2b', 'confirmacao_registro_boleto', 'tentativa_nf_b2b',
    'automacao_cobranca', 'aviso_remessa',
    # Negocio
    'pedido_loja', 'pedido_item', 'movimentacao_estoque',
    'estoque_loja', 'mov_estoque_loja',
    'estoque_producao', 'mov_estoque_producao',
    # Ordens de producao: o PAI ja era auditado, mas as QUANTIDADES vivem nos
    # itens (qtd_alvo/produzido/extra/dispensada) — sem audita-los, o incidente
    # de 02/07 (reagendados apagados pelo re-sync) ficou irrecuperavel.
    'planejamento_producao', 'planejamento_item',
    'fornecedor', 'historico_preco_mp',
    # Entregas
    'driver_entrega', 'atribuicao_entrega', 'lote_saida',
    # RH (mutacoes podem afetar folha)
    'funcionario', 'cargo', 'loja', 'folha_pagamento',
    # Sistema
    'usuario', 'materia_prima', 'receita', 'produto',
    # Loja online: correcao de pedido pago (reducao de qtd) mexe em dinheiro —
    # a mudanca de quantidade/valor precisa de trilha (08/07/2026).
    'pedido_online', 'pedido_online_item',
    # Patrimonio (20/07/2026): baixa/reativacao e valor de aquisicao sao
    # dinheiro-adjacentes e baixo volume — trilha barata. Conferencias
    # ficam fora (ja sao o proprio registro de evento).
    'ativo',
    # Checklist de loja (03/08/2026): editar/desativar item muda o que os
    # turnos sao cobrados a comprovar — baixo volume, trilha barata.
    # Preenchimentos/respostas ficam fora (ja sao o proprio registro).
    'checklist_item_modelo',
    # Perda de producao do padeiro (13/08/2026): registro mexe em estoque da
    # industria e a EXCLUSAO estorna — dinheiro-adjacente, baixo volume.
    'perda_producao',
}


def _snapshot(obj):
    """Serializa um objeto SQLAlchemy pra dict de colunas escalares."""
    mapper = inspect(obj.__class__)
    out = {}
    for col in mapper.columns:
        val = getattr(obj, col.key, None)
        if isinstance(val, (datetime, date)):
            val = val.isoformat()
        elif isinstance(val, Decimal):
            val = float(val)
        elif isinstance(val, bytes):
            # binary blob — guarda só tamanho
            val = f'<blob {len(val)} bytes>'
        out[col.key] = val
    return out


def _request_meta():
    """IP + user-agent do request atual, ou None se fora de request."""
    if not has_request_context():
        return None, None
    ip = request.headers.get('X-Forwarded-For') or request.remote_addr or None
    if ip and ',' in ip:
        ip = ip.split(',')[0].strip()
    ua = (request.headers.get('User-Agent') or '')[:300]
    return ip, ua


def _current_user_id(session=None):
    # Caminho Slack (thread async, so app_context): handler seta
    # session.info['audit_user_id'] = Usuario.id antes do commit.
    if session is not None:
        sess_uid = session.info.get('audit_user_id')
        if sess_uid:
            return sess_uid
    # Caminho web: pega Flask-Login.
    if not has_request_context():
        return None
    try:
        if current_user and current_user.is_authenticated:
            return current_user.id
    except Exception:  # noqa: BLE001
        pass
    return None


def _registrar(session, obj, acao, antes=None, depois=None):
    from app.models import AuditLog  # import local pra evitar circular
    tabela = obj.__class__.__tablename__
    if tabela not in AUDITED_MODELS or tabela == 'audit_log':
        return
    ip, ua = _request_meta()
    log = AuditLog(
        usuario_id=_current_user_id(session),
        tabela=tabela,
        registro_id=getattr(obj, 'id', None),
        acao=acao,
        antes=json.dumps(antes, ensure_ascii=False, default=str) if antes else None,
        depois=json.dumps(depois, ensure_ascii=False, default=str) if depois else None,
        ip=ip,
        user_agent=ua,
    )
    session.add(log)


def _capture_changes(obj):
    """Pra UPDATE: monta dict {coluna: [antes, depois]} a partir do
    histórico do SQLAlchemy (state.committed_state)."""
    state = inspect(obj)
    if not state.modified:
        return None, None
    antes, depois = {}, {}
    for attr in state.attrs:
        hist = attr.load_history()
        if hist.has_changes():
            old = hist.deleted[0] if hist.deleted else None
            new = hist.added[0] if hist.added else getattr(obj, attr.key)
            if isinstance(old, (datetime, date)):
                old = old.isoformat()
            if isinstance(new, (datetime, date)):
                new = new.isoformat()
            antes[attr.key] = old
            depois[attr.key] = new
    if not antes:
        return None, None
    return antes, depois


def _before_flush(session, flush_context, instances):
    """Captura mutacoes ANTES do flush — pra UPDATE, le valores antigos."""
    # Inserts
    for obj in session.new:
        tabela = getattr(obj.__class__, '__tablename__', None)
        if not tabela or tabela not in AUDITED_MODELS or tabela == 'audit_log':
            continue
        # registra apos flush (pra ter id)
        obj.__audit_pending__ = ('insert', None, None)

    # Updates
    for obj in session.dirty:
        tabela = getattr(obj.__class__, '__tablename__', None)
        if not tabela or tabela not in AUDITED_MODELS or tabela == 'audit_log':
            continue
        antes, depois = _capture_changes(obj)
        if antes is not None:
            obj.__audit_pending__ = ('update', antes, depois)

    # Deletes
    for obj in session.deleted:
        tabela = getattr(obj.__class__, '__tablename__', None)
        if not tabela or tabela not in AUDITED_MODELS or tabela == 'audit_log':
            continue
        obj.__audit_pending__ = ('delete', _snapshot(obj), None)


def _after_flush(session, flush_context):
    """Apos flush, cria os registros de AuditLog (inserts agora tem id)."""
    pendentes = []
    for obj in list(session.new) + list(session.dirty) + list(session.deleted):
        info = getattr(obj, '__audit_pending__', None)
        if not info:
            continue
        acao, antes, depois = info
        if acao == 'insert':
            depois = _snapshot(obj)
        pendentes.append((obj, acao, antes, depois))
        try:
            del obj.__audit_pending__
        except AttributeError:
            pass
    # cria todos os registros num batch
    for obj, acao, antes, depois in pendentes:
        try:
            _registrar(session, obj, acao, antes, depois)
        except Exception:  # noqa: BLE001
            logger.exception('falha ao registrar audit log pra %s', obj)


def init_audit():
    """Conecta os listeners. Chamar uma vez no create_app."""
    event.listen(db.session, 'before_flush', _before_flush)
    event.listen(db.session, 'after_flush', _after_flush)
    logger.info('Audit log ativo pra %d tabelas', len(AUDITED_MODELS))
