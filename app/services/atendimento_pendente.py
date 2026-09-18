"""Relógio de espera humana: mensagens automáticas não atendem nem resolvem."""
from datetime import UTC, datetime, timedelta

from app.extensions import db
from app.models import EsperaAtendimento, VigiaVeredito
from app.utils import BRT, agora


def candidatos():
    from app.services import chatwoot
    conversas = chatwoot.listar_conversas_paradas(
        min_minutos=0, status='open', limite=None, estrito=True)
    vistos = {str(c['id']) for c in conversas if c.get('id')}
    for row in EsperaAtendimento.query.filter(
            EsperaAtendimento.estado.in_(('aguardando', 'em_atendimento'))).all():
        if row.conversa_id in vistos:
            continue
        atual = chatwoot.consultar_conversa(row.conversa_id)
        if not atual:
            continue  # indisponibilidade não encerra o incidente
        if atual.get('status') == 'resolved':
            row.estado = 'resolvido'
            row.resolvido_em = agora()
        elif atual.get('status') in ('open', 'pending', 'snoozed'):
            conversas.append({'id': row.conversa_id, 'nome_contato': row.nome,
                              'minutos_paradas': int((agora() - row.inicio_em).total_seconds() / 60)})
    db.session.commit()
    return conversas


def _instante(mensagem, fallback):
    try:
        return datetime.fromtimestamp(float(mensagem['created_at']), UTC).astimezone(BRT).replace(tzinfo=None)
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return fallback


def preparar(conversa, historico, *, min_minutos=10):
    from app.services.chatbot_vigia import TEXTO_CONTENCAO_ESPERA, _e_fechamento, _e_mencao_story

    base = agora()
    conv_id = str(conversa['id'])
    row = db.session.get(EsperaAtendimento, conv_id)
    graves = VigiaVeredito.query.filter(
        VigiaVeredito.conv_id == conv_id, VigiaVeredito.alerta.is_(True),
        VigiaVeredito.gravidade == 'alta',
        db.or_(VigiaVeredito.bot_acao != 'espera_humano', VigiaVeredito.bot_acao.is_(None)))
    if row and row.resolvido_em:
        graves = graves.filter(VigiaVeredito.criado_em > row.resolvido_em)
    grave = graves.order_by(VigiaVeredito.criado_em.desc()).first()
    # Sem metadata (callers legados) assistant é humano, exceto nossa contenção.
    efetivas = [m for m in historico if m.get('role') == 'user' or
                (m.get('role') == 'assistant' and m.get('humano', True)
                 and TEXTO_CONTENCAO_ESPERA[:40] not in (m.get('content') or ''))]
    if grave or (row and row.grave):
        efetivas = [m for m in efetivas if m.get('role') != 'user'
                    or not (_e_fechamento(m.get('content')) or _e_mencao_story(m.get('content')))]
    if not efetivas:
        if row is None and grave:
            row = EsperaAtendimento(conversa_id=conv_id, inicio_em=grave.criado_em,
                                    nome=conversa.get('nome_contato'), mensagem=grave.mensagem_cliente,
                                    grave=True, estado='aguardando')
            db.session.add(row)
            db.session.commit()
        return row if row and row.estado in ('aguardando', 'em_atendimento') else None
    ultima = efetivas[-1]
    if ultima.get('role') != 'user':
        if row is None and grave:
            row = EsperaAtendimento(conversa_id=conv_id, inicio_em=grave.criado_em,
                                    nome=conversa.get('nome_contato'), mensagem=grave.mensagem_cliente,
                                    grave=True, estado='em_atendimento')
            db.session.add(row)
        if row:
            row.grave = bool(row.grave or grave)
            if row.grave and row.estado != 'resolvido':
                if row.estado != 'em_atendimento':
                    row.proximo_aviso_em = base + timedelta(hours=1)
                row.estado = 'em_atendimento'
            else:
                row.estado = 'respondido'
            db.session.commit()
        return row if row and row.estado == 'em_atendimento' else None
    texto = ultima.get('content') or ''
    if _e_mencao_story(texto) or _e_fechamento(texto):
        return row if row and row.grave and row.estado in ('aguardando', 'em_atendimento') else None
    inicio = _instante(ultima, base - timedelta(minutes=conversa.get('minutos_paradas', 0)))
    if row and row.resolvido_em and inicio <= row.resolvido_em:
        return None
    if row is None:
        row = EsperaAtendimento(conversa_id=conv_id, inicio_em=inicio, estado='aguardando')
        db.session.add(row)
    elif row.estado in ('resolvido', 'respondido', 'em_atendimento'):
        if row.estado == 'resolvido':
            row.grave = False
        row.inicio_em = inicio
        row.proximo_aviso_em = None
        row.estado = 'aguardando'
    row.nome = str(conversa.get('nome_contato') or row.nome or '(sem nome)')[:200]
    row.mensagem = texto[:2000]
    row.grave = bool(row.grave or grave)
    db.session.commit()
    if not row.grave and base < row.inicio_em + timedelta(minutes=min_minutos):
        return None
    return row
