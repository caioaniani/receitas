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
    alerta_do_episodio = db.exists().where(db.and_(
        VigiaVeredito.conv_id == EsperaAtendimento.conversa_id,
        VigiaVeredito.alerta.is_(True), VigiaVeredito.gravidade == 'alta',
        VigiaVeredito.criado_em >= EsperaAtendimento.inicio_em,
        db.or_(EsperaAtendimento.resolvido_em.is_(None),
               VigiaVeredito.criado_em > EsperaAtendimento.resolvido_em),
    ))
    for row in EsperaAtendimento.query.filter(db.or_(
            EsperaAtendimento.estado.in_(('aguardando', 'em_atendimento')),
            db.and_(EsperaAtendimento.estado == 'respondido', alerta_do_episodio),
    )).all():
        if row.conversa_id in vistos:
            if row.estado == 'respondido':
                row.estado = 'em_atendimento'
            continue
        atual = chatwoot.consultar_conversa(row.conversa_id)
        if not atual:
            continue  # indisponibilidade não encerra o incidente
        if atual.get('status') == 'resolved':
            row.estado = 'resolvido'
            row.resolvido_em = agora()
            row.proximo_aviso_em = None
        elif atual.get('status') in ('open', 'pending', 'snoozed'):
            if row.estado == 'respondido':
                row.estado = 'em_atendimento'
            # `status` e `telefone` acompanham a candidata (caso Jessica
            # 19/09/2026): sem o status, o alerta dizia "esperando ATENDENTE
            # em conversa open" numa conversa PENDING que o bot atendia; sem
            # o telefone, a chave de contato saia vazia ("c:") e o dedupe de
            # contencao por contato ficava cego neste caminho.
            sender = ((atual.get('meta') or {}).get('sender') or {})
            conversas.append({'id': row.conversa_id, 'nome_contato': row.nome,
                              'minutos_paradas': int((agora() - row.inicio_em).total_seconds() / 60),
                              'status': atual.get('status'),
                              'telefone': (sender.get('phone_number')
                                           or sender.get('identifier') or '')})
    db.session.commit()
    return conversas


def _instante(mensagem, fallback):
    try:
        return datetime.fromtimestamp(float(mensagem['created_at']), UTC).astimezone(BRT).replace(tzinfo=None)
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return fallback


def _acompanhar_ate_resolver(row, base, min_minutos=10):
    """Resposta não apaga um alerta; atendimento rápido sem alerta não vira incidente."""
    if not row or row.estado == 'resolvido':
        return False
    if row.grave or row.estado == 'em_atendimento' or row.proximo_aviso_em:
        return True
    aviso = VigiaVeredito.query.filter(
        VigiaVeredito.conv_id == row.conversa_id, VigiaVeredito.alerta.is_(True),
        VigiaVeredito.gravidade == 'alta', VigiaVeredito.criado_em >= row.inicio_em)
    if row.resolvido_em:
        aviso = aviso.filter(VigiaVeredito.criado_em > row.resolvido_em)
    return bool(aviso.first() or (base and row.estado == 'aguardando'
                and base >= row.inicio_em + timedelta(minutes=min_minutos)))


def _limitar_proximo_aviso(row, base):
    # Preserva cobranças vencidas e encurta os antigos intervalos de uma hora.
    if row.proximo_aviso_em and row.proximo_aviso_em > base + timedelta(minutes=15):
        row.proximo_aviso_em = base + timedelta(minutes=15)


def _primeira_resposta(historico, inicio):
    instantes = [_instante(m, None) for m in historico if m.get('role') == 'assistant']
    return min((t for t in instantes if t and t >= inicio), default=None)


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
    if grave or _acompanhar_ate_resolver(row, base, min_minutos):
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
            if row.estado == 'resolvido':
                return None
            row.grave = bool(row.grave or grave)
            if _acompanhar_ate_resolver(row, _primeira_resposta(efetivas, row.inicio_em), min_minutos):
                _limitar_proximo_aviso(row, base)
                row.estado = 'em_atendimento'
            else:
                row.estado = 'respondido'
            db.session.commit()
        return row if row and row.estado == 'em_atendimento' else None
    texto = ultima.get('content') or ''
    if _e_mencao_story(texto) or _e_fechamento(texto):
        return row if _acompanhar_ate_resolver(row, base, min_minutos) else None
    inicio = _instante(ultima, base - timedelta(minutes=conversa.get('minutos_paradas', 0)))
    if row and row.resolvido_em and inicio <= row.resolvido_em:
        return None
    if row is None:
        row = EsperaAtendimento(conversa_id=conv_id, inicio_em=inicio, estado='aguardando')
        db.session.add(row)
    elif row.estado == 'aguardando' and not _acompanhar_ate_resolver(row, None, min_minutos):
        primeira = _primeira_resposta(efetivas, row.inicio_em)
        if primeira and primeira < inicio and primeira < row.inicio_em + timedelta(minutes=min_minutos):
            # Resposta rápida entre dois ciclos encerrou a espera anterior.
            row.inicio_em = inicio
    elif row.estado == 'em_atendimento':
        # A conversa ainda não foi resolvida: outra mensagem não zera o alerta.
        row.estado = 'aguardando'
    elif row.estado in ('resolvido', 'respondido'):
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


def registrar_alerta(veredito):
    """Incidente grave visível imediatamente, antes do cron e do reconhecimento."""
    from sqlalchemy import case
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    cid = str(veredito.conv_id or '')
    if (not veredito.alerta or veredito.gravidade != 'alta' or
            not cid.isascii() or not cid.isdigit() or int(cid) <= 0):
        return
    tabela = EsperaAtendimento.__table__
    inserir = pg_insert if db.engine.dialect.name == 'postgresql' else sqlite_insert
    stmt = inserir(tabela).values(
        conversa_id=cid, inicio_em=veredito.criado_em, nome=veredito.cliente,
        mensagem=veredito.mensagem_cliente, grave=True, estado='aguardando',
    )
    db.session.execute(stmt.on_conflict_do_update(
        index_elements=['conversa_id'],
        set_={'grave': True, 'estado': 'aguardando', 'nome': stmt.excluded.nome,
              'mensagem': stmt.excluded.mensagem,
              'inicio_em': case((tabela.c.estado.in_(('aguardando', 'em_atendimento')),
                                 tabela.c.inicio_em), else_=stmt.excluded.inicio_em),
              'proximo_aviso_em': None},
        where=db.and_(
            db.or_(tabela.c.resolvido_em.is_(None), tabela.c.resolvido_em < veredito.criado_em),
            tabela.c.inicio_em <= veredito.criado_em),
    ))
    db.session.commit()


def alertas_painel():
    """Fila de atendimento, não de recibos de leitura. Sem HTTP a cada poll."""
    from app.services.chatbot_vigia import _janela_horas

    base = agora()
    recentes = VigiaVeredito.query.filter(
        VigiaVeredito.alerta.is_(True), VigiaVeredito.gravidade == 'alta',
        VigiaVeredito.criado_em >= base - timedelta(hours=_janela_horas()),
    ).order_by(VigiaVeredito.criado_em.desc(), VigiaVeredito.id.desc()).all()
    ultimos = {}
    for v in recentes:
        cid = str(v.conv_id or '')
        if cid.isascii() and cid.isdigit() and int(cid) > 0:
            ultimos.setdefault(cid, v)
    esperas = {r.conversa_id: r for r in EsperaAtendimento.query.filter(db.or_(
        EsperaAtendimento.estado.in_(('aguardando', 'em_atendimento')),
        EsperaAtendimento.conversa_id.in_(list(ultimos)),
    )).all()}
    out = []
    for cid in set(esperas) | set(ultimos):
        if not (cid.isascii() and cid.isdigit() and int(cid) > 0):
            continue
        row, veredito = esperas.get(cid), ultimos.get(cid)
        if row:
            # Um caso encerrado não volta por um aviso antigo ou por um clique.
            if row.estado not in ('aguardando', 'em_atendimento'):
                continue
            if row.resolvido_em and row.inicio_em <= row.resolvido_em:
                continue
            if (not row.grave and row.estado == 'aguardando'
                    and base < row.inicio_em + timedelta(minutes=10)):
                continue
            inicio, grave, estado = row.inicio_em, row.grave, row.estado
            nome, mensagem = row.nome, row.mensagem
            chave = f'{cid}:{inicio.isoformat()}:{int(grave)}'
        else:
            # Novo incidente grave aparece antes do próximo ciclo do monitor.
            # As esperas já acompanhadas acima não dependem de reconhecimento.
            if not veredito:
                continue
            inicio = veredito.criado_em
            grave = veredito.bot_acao != 'espera_humano'
            estado, nome, mensagem = 'aguardando', veredito.cliente, veredito.mensagem_cliente
            chave = f'{cid}:alerta:{veredito.id}'
        motivo = ('Já houve resposta, mas o atendimento ainda precisa ser resolvido.' if estado == 'em_atendimento'
                  else 'Cliente aguardando uma resposta da equipe.')
        if grave and veredito and veredito.bot_acao != 'espera_humano':
            motivo = veredito.motivo_vigia or veredito.bot_motivo or motivo
        out.append(dict(chave=chave, conv_id=cid, cliente=nome or 'Cliente',
                        motivo=motivo[:500], mensagem=(mensagem or '')[:300],
                        grave=bool(grave), estado=estado,
                        ha_minutos=max(0, int((base - inicio).total_seconds() // 60))))
    return sorted(out, key=lambda a: (not a['grave'], -a['ha_minutos'], a['conv_id']))


def confirmar_acao_painel(cid, acao, *, iniciado_em, usuario_id):
    """Chamado só depois de envio/resolução confirmado pelo Chatwoot."""
    if acao not in ('responder', 'resolved'):
        return
    cid = str(cid)
    row = EsperaAtendimento.query.filter_by(conversa_id=cid).with_for_update().first()
    recentes = VigiaVeredito.query.filter(
        VigiaVeredito.conv_id == cid, VigiaVeredito.alerta.is_(True),
        VigiaVeredito.gravidade == 'alta', VigiaVeredito.criado_em <= iniciado_em,
    )
    if row and row.resolvido_em:
        recentes = recentes.filter(VigiaVeredito.criado_em > row.resolvido_em)
    vereditos = recentes.order_by(VigiaVeredito.criado_em.desc()).all()
    if row is None and vereditos:
        v = vereditos[0]
        row = EsperaAtendimento(conversa_id=cid, inicio_em=v.criado_em,
                                nome=v.cliente, mensagem=v.mensagem_cliente,
                                grave=any(v.bot_acao != 'espera_humano' for v in vereditos))
        db.session.add(row)
    if row is None and acao == 'resolved':
        # Guarda a resolução mesmo antes de a primeira avaliação de IA terminar.
        row = EsperaAtendimento(conversa_id=cid, inicio_em=iniciado_em,
                                estado='resolvido', grave=False)
        db.session.add(row)
    novo_incidente = VigiaVeredito.query.filter(
        VigiaVeredito.conv_id == cid, VigiaVeredito.alerta.is_(True),
        VigiaVeredito.gravidade == 'alta', VigiaVeredito.criado_em > iniciado_em,
        db.or_(VigiaVeredito.bot_acao != 'espera_humano', VigiaVeredito.bot_acao.is_(None)),
    ).first()
    if row and row.inicio_em <= iniciado_em and not novo_incidente:
        if acao == 'resolved':
            row.estado = 'resolvido'
            row.resolvido_em = iniciado_em
            row.proximo_aviso_em = None
        elif row.estado != 'resolvido':
            row.grave = bool(row.grave or any(v.bot_acao != 'espera_humano' for v in vereditos))
            acompanhar = bool(vereditos) or _acompanhar_ate_resolver(row, iniciado_em)
            row.estado = 'em_atendimento' if acompanhar else 'respondido'
            if acompanhar:
                _limitar_proximo_aviso(row, iniciado_em)
            else:
                row.proximo_aviso_em = None
    for v in vereditos:
        if not v.reconhecido_em:
            v.reconhecido_em = agora()
            v.reconhecido_por_id = usuario_id
    db.session.commit()
