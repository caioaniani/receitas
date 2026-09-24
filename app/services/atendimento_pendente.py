"""Relógio de espera humana: mensagens automáticas não atendem nem resolvem."""
import logging
from datetime import UTC, datetime, timedelta

from app.extensions import db
from app.models import EsperaAtendimento, VigiaVeredito
from app.utils import BRT, agora

logger = logging.getLogger(__name__)


# Versao otimista sem coluna nova: todos os dados do episodio observado.
# As leituras externas so podem alterar esta versao, nunca uma espera mais
# recente gravada pelo webhook/painel enquanto a API respondia.
_CAMPOS_ESPERA = tuple(c.name for c in EsperaAtendimento.__table__.columns)
_SNAPSHOT_CANDIDATA = '_espera_observada'


def _observar_espera(conv_id):
    tabela = EsperaAtendimento.__table__
    registro = db.session.execute(db.select(tabela).where(
        tabela.c.conversa_id == str(conv_id))).mappings().first()
    return dict(registro) if registro is not None else None


def _salvar_se_inalterada(row, observada):
    """CAS atomico; `row` e transitória para nunca sofrer autoflush antecipado."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    tabela = EsperaAtendimento.__table__
    valores = {c: getattr(row, c) for c in _CAMPOS_ESPERA}
    valores['grave'] = bool(valores['grave'])
    valores['estado'] = valores['estado'] or 'aguardando'
    if observada is None:
        inserir = pg_insert if db.engine.dialect.name == 'postgresql' else sqlite_insert
        stmt = inserir(tabela).values(**valores).on_conflict_do_nothing(
            index_elements=['conversa_id'])
    else:
        stmt = tabela.update().where(db.and_(
            *(tabela.c[c] == observada[c] for c in _CAMPOS_ESPERA))).values(**valores)
    mudou = db.session.execute(stmt).rowcount == 1
    db.session.commit()
    if not mudou:
        logger.info('espera humana: resultado antigo descartado conv=%s', row.conversa_id)
        return None
    return (EsperaAtendimento.query.populate_existing()
            .filter_by(conversa_id=row.conversa_id).first())


def candidatos():
    from app.blueprints.crm.routes import _lock_conv_cross_worker, _lock_para_conv
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
    ids = [r.conversa_id for r in EsperaAtendimento.query.filter(db.or_(
            EsperaAtendimento.estado.in_(('aguardando', 'em_atendimento')),
            db.and_(EsperaAtendimento.estado == 'respondido', alerta_do_episodio),
    )).all()]
    for cid in ids:
        # Mesmo lock do webhook, sempre thread -> advisory. Candidatos só
        # roda no monitor (nenhum chamador já segura este lock). A exclusão
        # cobre inclusive mensagens repetidas que deixariam o snapshot igual.
        with _lock_para_conv(cid), _lock_conv_cross_worker(cid):
            observada = _observar_espera(cid)
            if not observada or observada['estado'] not in ('aguardando', 'em_atendimento', 'respondido'):
                continue
            row = EsperaAtendimento(**observada)
            if row.conversa_id in vistos:
                if row.estado == 'respondido':
                    row.estado = 'em_atendimento'
                    _salvar_se_inalterada(row, observada)
                continue
            consulta_iniciada = agora()
            atual = chatwoot.consultar_conversa(row.conversa_id)
            if not atual:
                continue  # indisponibilidade não encerra o incidente
            if atual.get('status') == 'resolved':
                row.estado = 'resolvido'
                row.resolvido_em = agora()
                row.proximo_aviso_em = None
                row = _salvar_se_inalterada(row, observada)
                if row is None:
                    continue
                # Conversa resolvida por humano: episodio "equipe em nota
                # privada" encerrado (sinal alternativo ao evento do webhook).
                from app.services import presenca_humana
                presenca_humana.encerrar(
                    row.conversa_id, 'candidatos:resolved',
                    tolerancia_seg=max(0.000001, (agora() - consulta_iniciada).total_seconds()))
                # Conversa resolvida no Chatwoot NAO fecha o alerta ALTA por si
                # (item 9, regra estrita: so resposta humana depois do alerta ou
                # motivo por escrito — o proprio BOT resolve conversa num
                # "obrigada"). Mas a conversa resolvida SAI de `preparar` (o
                # unico leitor periodico do historico), entao a resposta humana
                # dada no Chatwoot antes do resolve ficaria sem efeito e o ALTA
                # preso na fila ate a janela vencer (revisao 23/09/2026, 2ª
                # rodada). Le o historico UMA vez, so se ha ALTA em aberto.
                _resolver_alertas_por_historico(row.conversa_id)
            elif atual.get('status') in ('open', 'pending', 'snoozed'):
                if _observar_espera(cid) != observada:
                    continue
                if row.estado == 'respondido':
                    row.estado = 'em_atendimento'
                    row = _salvar_se_inalterada(row, observada)
                    if row is None:
                        continue
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
    # O vigia vai buscar o historico APÓS esta captura. Uma mensagem/acao
    # humana durante essa leitura invalida o resultado inteiro de preparar.
    for conversa in conversas:
        conversa[_SNAPSHOT_CANDIDATA] = _observar_espera(conversa['id'])
    return conversas


def _instante(mensagem, fallback):
    try:
        return datetime.fromtimestamp(float(mensagem['created_at']), UTC).astimezone(BRT).replace(tzinfo=None)
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return fallback


def _acompanhar_ate_resolver(row, base, min_minutos=10):
    """Resposta não apaga um alerta; atendimento rápido sem alerta não vira incidente."""
    # `sem_cliente` é terminal como `resolvido`: sem fala do cliente não há
    # episódio a acompanhar (revisão 22/09/2026 — sem isto, um "Sim"/"Ok"
    # ao template caía no ramo de fechamento com `grave` e virava
    # "Caso grave ainda aberto" a cada 15 min).
    if not row or row.estado in ('resolvido', 'sem_cliente'):
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


def _ultima_resposta_humana(efetivas):
    """Instante da ULTIMA fala humana da equipe (as `efetivas` ja excluem o
    bot, quando ha autoria, e a nossa contencao). None = sem instante."""
    instantes = [_instante(m, None) for m in efetivas if m.get('role') == 'assistant']
    return max((t for t in instantes if t), default=None)


def _efetivas_humanas(historico):
    """Falas da equipe (autoria humana; sem metadata, assistant e humano) sem
    a nossa contencao automatica — a mesma regua de `preparar`."""
    from app.services.chatbot_vigia import TEXTO_CONTENCAO_ESPERA
    return [m for m in (historico or [])
            if m.get('role') == 'assistant' and m.get('humano', True)
            and TEXTO_CONTENCAO_ESPERA[:40] not in (m.get('content') or '')]


def _resolver_alertas_respondidos(conv_id, efetivas):
    """Resposta HUMANA na conversa fecha os alertas ALTA criados ANTES dela
    (item 9, caso E3862E49, 22/09/2026): "ALTA so resolvido com resposta
    enviada na conversa depois do alerta". Alerta posterior a resposta
    continua em aberto. Sem commit — quem chama commita."""
    ultima = _ultima_resposta_humana(efetivas)
    if ultima is None:
        return 0
    from app.services import chatbot_vigia
    # Commit proprio SO quando resolveu algo: a resolucao e um fato
    # independente do que `preparar` decide depois (e alguns ramos dele
    # retornam sem commitar).
    return chatbot_vigia.resolver_alertas_da_conversa(
        conv_id, via='resposta_humana', ate=ultima, commit=True)


def _resolver_alertas_por_historico(conv_id):
    """Conversa que ficou `resolved` no Chatwoot: se ha ALTA em aberto, le o
    historico com autoria e resolve os alertas anteriores a ULTIMA resposta
    humana (a mesma regra de `preparar`, que nao roda mais nessa conversa).
    Sem resposta humana ou API fora = nada muda (o alerta segue na fila —
    conservador de proposito). Best-effort: erro aqui nunca derruba o
    ciclo da espera humana. Devolve quantos alertas resolveu."""
    from app.services import chatbot_vigia, chatwoot
    try:
        if not chatbot_vigia.alertas_em_aberto_da_conversa(conv_id):
            return 0
        historico = chatwoot.buscar_historico(conv_id, incluir_autoria=True) or []
        efetivas = _efetivas_humanas(historico)
        return _resolver_alertas_respondidos(conv_id, efetivas)
    except Exception:  # noqa: BLE001
        logger.exception('espera humana: resolver alertas da conversa %s resolvida falhou',
                         conv_id)
        return 0


def preparar(conversa, historico, *, min_minutos=10):
    from app.services.chatbot_vigia import TEXTO_CONTENCAO_ESPERA, _e_fechamento, _e_mencao_story

    base = agora()
    conv_id = str(conversa['id'])
    observada = _observar_espera(conv_id)
    if (_SNAPSHOT_CANDIDATA in conversa
            and conversa[_SNAPSHOT_CANDIDATA] != observada):
        logger.info('espera humana: historico ultrapassado descartado conv=%s', conv_id)
        return None
    # Calcula em objeto nao associado a sessao; consultas auxiliares/commits
    # jamais podem gravar campos antes do UPDATE condicional abaixo.
    row = EsperaAtendimento(**observada) if observada is not None else None
    from app.services.chatbot import cliente_ja_falou
    if not cliente_ja_falou(historico):
        # Conversa iniciada pela EQUIPE ("Chamar cliente") em que o cliente
        # nunca escreveu: não existe espera. Estado terminal `sem_cliente`
        # tira a linha do painel e da cobrança (`alertas_painel`/
        # `candidatos` só olham aguardando/em_atendimento) e fecha o
        # episódio (`resolvido_em`) para o ALTA fantasma de abandono não
        # reabrir a gravidade. Se o cliente responder um dia, o ramo abaixo
        # volta a 'aguardando'. Caso 2429 (22/09/2026).
        if row and row.estado in ('aguardando', 'em_atendimento'):
            row.estado = 'sem_cliente'
            row.resolvido_em = base
            row.proximo_aviso_em = None
            # A gravidade e a "mensagem do cliente" vieram do ALTA fantasma
            # de abandono ("[ABANDONO 17min] "): zeradas, senão a resposta
            # curta ao template ("Sim") reabria a cobrança grave.
            row.grave = False
            row.mensagem = ''
            _salvar_se_inalterada(row, observada)
        return None
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
                    or not (_e_fechamento(m.get('content'), elogio=True)
                            or _e_mencao_story(m.get('content')))]
    # Resposta humana dada pelo Chatwoot (nao pelo painel) tambem fecha os
    # alertas anteriores a ela — este e o unico leitor periodico da conversa.
    _resolver_alertas_respondidos(conv_id, efetivas)
    if not efetivas:
        if row is None and grave:
            row = EsperaAtendimento(conversa_id=conv_id, inicio_em=grave.criado_em,
                                    nome=conversa.get('nome_contato'), mensagem=grave.mensagem_cliente,
                                    grave=True, estado='aguardando')
        return (_salvar_se_inalterada(row, observada)
                if row and row.estado in ('aguardando', 'em_atendimento') else None)
    ultima = efetivas[-1]
    if ultima.get('role') != 'user':
        if row is None and grave:
            row = EsperaAtendimento(conversa_id=conv_id, inicio_em=grave.criado_em,
                                    nome=conversa.get('nome_contato'), mensagem=grave.mensagem_cliente,
                                    grave=True, estado='em_atendimento')
        if row:
            if row.estado == 'resolvido':
                return None
            row.grave = bool(row.grave or grave)
            if _acompanhar_ate_resolver(row, _primeira_resposta(efetivas, row.inicio_em), min_minutos):
                _limitar_proximo_aviso(row, base)
                row.estado = 'em_atendimento'
            else:
                row.estado = 'respondido'
            row = _salvar_se_inalterada(row, observada)
        return row if row and row.estado == 'em_atendimento' else None
    texto = ultima.get('content') or ''
    # `elogio=True`: "Amamosss 🥰" depois da entrega não é cliente esperando
    # (conv 2375, 19/09/2026). Só aqui — o bot não encerra num elogio.
    if _e_mencao_story(texto) or _e_fechamento(texto, elogio=True):
        return (_salvar_se_inalterada(row, observada)
                if _acompanhar_ate_resolver(row, base, min_minutos) else None)
    inicio = _instante(ultima, base - timedelta(minutes=conversa.get('minutos_paradas', 0)))
    # `sem_cliente` foi decidido com um histórico SEM fala do cliente, então
    # qualquer fala dele é nova por construção — o guard abaixo não vale
    # (uma mensagem criada nos segundos entre o GET do histórico e o
    # `agora()` da marcação ficaria presa para sempre atrás de
    # `resolvido_em`; revisão 22/09/2026).
    if row and row.resolvido_em and inicio <= row.resolvido_em and row.estado != 'sem_cliente':
        return None
    if row is None:
        row = EsperaAtendimento(conversa_id=conv_id, inicio_em=inicio, estado='aguardando')
    elif row.estado == 'aguardando' and not _acompanhar_ate_resolver(row, None, min_minutos):
        primeira = _primeira_resposta(efetivas, row.inicio_em)
        if primeira and primeira < inicio and primeira < row.inicio_em + timedelta(minutes=min_minutos):
            # Resposta rápida entre dois ciclos encerrou a espera anterior.
            row.inicio_em = inicio
    elif row.estado == 'em_atendimento':
        # A conversa ainda não foi resolvida: outra mensagem não zera o alerta.
        row.estado = 'aguardando'
    elif row.estado in ('resolvido', 'respondido', 'sem_cliente'):
        if row.estado in ('resolvido', 'sem_cliente'):
            row.grave = False
        if row.estado == 'sem_cliente' and row.resolvido_em and inicio <= row.resolvido_em:
            # Mantém o invariante `inicio_em > resolvido_em` que o painel e
            # `candidatos` usam; os ALTAs fantasmas (anteriores à marcação)
            # já ficaram fora de `graves` acima, calculado com o valor antigo.
            row.resolvido_em = inicio - timedelta(seconds=1)
        row.inicio_em = inicio
        row.proximo_aviso_em = None
        row.estado = 'aguardando'
    row.nome = str(conversa.get('nome_contato') or row.nome or '(sem nome)')[:200]
    row.mensagem = texto[:2000]
    row.grave = bool(row.grave or grave)
    row = _salvar_se_inalterada(row, observada)
    if row is None:
        return None
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
                    and row.proximo_aviso_em is None
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
        elif row.estado not in ('resolvido', 'sem_cliente'):
            # `sem_cliente`: a equipe escreveu numa conversa que ELA iniciou
            # e o cliente nunca respondeu — não é atendimento em curso.
            row.grave = bool(row.grave or any(v.bot_acao != 'espera_humano' for v in vereditos))
            acompanhar = bool(vereditos) or _acompanhar_ate_resolver(row, iniciado_em)
            row.estado = 'em_atendimento' if acompanhar else 'respondido'
            if acompanhar:
                _limitar_proximo_aviso(row, iniciado_em)
            else:
                row.proximo_aviso_em = None
    # Resposta enviada PELO PAINEL fecha os alertas ALTA anteriores ao gesto
    # (item 9, 22/09/2026: "resposta enviada na conversa depois do alerta").
    # "Conversa resolvida" pelo painel SO silencia (marca reconhecido, como
    # antes): resolver sem responder nem escrever o motivo nao fecha o caso
    # — regra estrita da spec; o botao "Resolver" do drawer pede o motivo.
    from app.services import chatbot_vigia
    if acao == 'responder':
        chatbot_vigia.resolver_alertas(
            [v.id for v in vereditos], via='resposta_humana',
            usuario_id=usuario_id, momento=iniciado_em, commit=False)
    for v in vereditos:
        if not v.reconhecido_em:
            v.reconhecido_em = agora()
            v.reconhecido_por_id = usuario_id
    db.session.commit()
