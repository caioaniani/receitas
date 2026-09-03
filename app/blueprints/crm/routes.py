"""CRM card: histórico do cliente por telefone, embutido no Chatwoot.

O atendente abre uma conversa no Chatwoot; um Dashboard App (iframe servido
por `/crm/card`) mostra o que esse telefone já comprou da padaria. O Chatwoot
passa o contato pro iframe via `postMessage`; o iframe extrai o telefone e
chama `/crm/card.json`.

Auth: token compartilhado `CHATWOOT_CARD_TOKEN` embutido na URL do Dashboard
App (`?k=...`) — o iframe repassa em cada chamada do JSON. Sem token válido,
o JSON nega. O endpoint nunca ecoa input arbitrário: só devolve dados de
clientes que já existem no banco.

Match de telefone via `app.utils.telefone_chave` (canônico BR: ignora +55 e
o 9º dígito de celular), porque o WhatsApp manda '5511999998888' mas o
cadastro pode ter '(11) 99999-8888'.
"""
import json
import logging
import secrets
from decimal import Decimal

from flask import current_app, jsonify, render_template, request

from app.blueprints.crm import crm_bp
from app.extensions import csrf
from app.utils import telefone_chave

logger = logging.getLogger(__name__)

# Locks por conv_id pra serializar threads que processam a MESMA conversa.
# Sem isso, mensagens consecutivas do cliente no WhatsApp viram webhooks paralelos
# que disputam o historico — bot esquece o que ja perguntou. Globais (escopo do
# processo gunicorn). Acumulam: aceitavel ate ~milhares de conversas/dia (~32B
# por entry); se virar problema, troca por LRU.
import threading as _threading
import zlib as _zlib
from contextlib import contextmanager as _contextmanager

_BOT_LOCKS: dict = {}
_BOT_LOCKS_GUARD = _threading.Lock()


def _lock_para_conv(conv_id):
    """Devolve um threading.Lock dedicado a essa conv_id. Cria sob demanda."""
    with _BOT_LOCKS_GUARD:
        lock = _BOT_LOCKS.get(conv_id)
        if lock is None:
            lock = _threading.Lock()
            _BOT_LOCKS[conv_id] = lock
        return lock


# Advisory lock class do chatbot no registro do projeto (7723 sync, 7731
# backup, 7738 auditor, 7739 vigia infra, 7745 site, 7748 uso_ia, 7749
# reviews, 7752 reprocesso). Forma de 2 ints: (classe, hash da conversa).
_LOCK_CLASS_CHATBOT = 7753


@_contextmanager
def _lock_conv_cross_worker(conv_id):
    """Serializa o processamento da MESMA conversa ENTRE workers gunicorn.

    O `_BOT_LOCKS` é memória de UM processo e prod roda `--workers 2`
    (Procfile): mensagens consecutivas do cliente podiam cair em workers
    diferentes e fazer read-modify-write concorrente no store — o último
    `salvar_historico` vencia e um turno sumia do contexto (achado da
    revisão 19/07/2026). Advisory lock BLOQUEANTE por conversa (o tempo de
    espera é o do turno do outro worker; as chamadas de IA têm timeout).
    SQLite (dev/teste, 1 processo) = no-op, o thread lock já cobre."""
    from sqlalchemy import text as _text

    from app.extensions import db as _db
    eng = _db.engine
    if eng.dialect.name != 'postgresql':
        yield
        return
    objid = _zlib.crc32(str(conv_id).encode()) & 0x7FFFFFFF
    # AUTOCOMMIT: sem isso o SELECT do lock abre transacao que fica "idle in
    # transaction" o turno inteiro (segura snapshot/vacuum). O advisory lock
    # e de SESSAO — sobrevive fora de transacao.
    conn = eng.connect().execution_options(isolation_level='AUTOCOMMIT')
    try:
        conn.execute(_text('SELECT pg_advisory_lock(:c, :o)'),
                     {'c': _LOCK_CLASS_CHATBOT, 'o': objid})
        try:
            yield
        finally:
            try:
                conn.execute(_text('SELECT pg_advisory_unlock(:c, :o)'),
                             {'c': _LOCK_CLASS_CHATBOT, 'o': objid})
            except Exception:  # noqa: BLE001
                # NUNCA devolver ao pool uma conexao que pode estar com o
                # lock preso — outra request a pegaria e a conversa ficaria
                # travada pra sempre. Invalidate descarta a conexao fisica
                # (o Postgres solta o lock quando ela morre).
                logger.exception('unlock advisory conv=%s falhou — '
                                 'invalidando conexao', conv_id)
                conn.invalidate()
    finally:
        conn.close()


# ── Debounce/coalescing de rajada (02/07/2026) ────────────────────────────
# Cliente que quebra a frase em 3 balões gerava 3 chamadas Opus + 3 respostas.
# Cada webhook agora DEPOSITA a mensagem no buffer da conversa e a thread
# espera o debounce; quem acorda DRENA tudo que acumulou e responde UMA vez
# (as threads das mensagens já drenadas acham o buffer vazio e saem).
_PENDENTES: dict = {}            # conv_id -> [ {'content', 'imagens'} ]
_PENDENTES_GUARD = _threading.Lock()


def _debounce_segundos():
    """Janela de coalescing. Em testes (PYTEST_RUNNING) default 0 pra não
    dormir a suíte; override explícito via CHATBOT_DEBOUNCE_S vale sempre."""
    import os
    bruto = os.environ.get('CHATBOT_DEBOUNCE_S')
    if bruto is None:
        return 0.0 if os.environ.get('PYTEST_RUNNING') else 4.0
    try:
        return max(0.0, float(bruto))
    except (TypeError, ValueError):
        return 4.0


def _depositar_pendente(conv_id, content, imagens):
    with _PENDENTES_GUARD:
        _PENDENTES.setdefault(conv_id, []).append(
            {'content': content, 'imagens': imagens})


def _drenar_pendentes(conv_id):
    """Retira TODAS as mensagens acumuladas da conversa e devolve
    (texto_combinado, imagens). Lista vazia = outra thread já drenou."""
    with _PENDENTES_GUARD:
        msgs = _PENDENTES.pop(conv_id, [])
    if not msgs:
        return None, []
    textos = [m['content'] for m in msgs if (m['content'] or '').strip()]
    imagens = [img for m in msgs for img in (m['imagens'] or [])]
    return '\n'.join(textos), imagens

# Chamado de iframe externo (Chatwoot) — sem token CSRF. Autenticidade vem
# do CHATWOOT_CARD_TOKEN, não do cookie de sessão.
csrf.exempt(crm_bp)


def _token_ok(recebido):
    esperado = (current_app.config.get('CHATWOOT_CARD_TOKEN') or '').strip()
    if not esperado:
        return False
    return secrets.compare_digest(str(recebido or ''), esperado)


@crm_bp.route('/card')
def card():
    """Página HTML enxuta pro iframe do Chatwoot (Dashboard App)."""
    if not (current_app.config.get('CHATWOOT_CARD_TOKEN') or '').strip():
        return ('Integração Chatwoot não configurada '
                '(CHATWOOT_CARD_TOKEN ausente).'), 503
    return render_template('crm/card.html', token=request.args.get('k', ''))


@crm_bp.route('/card.json')
def card_json():
    if not _token_ok(request.args.get('k')):
        return jsonify({'ok': False, 'erro': 'token inválido'}), 403
    chave = telefone_chave(request.args.get('phone', ''))
    if not chave:
        return jsonify({'ok': True, 'encontrado': False, 'telefone': '',
                        'pedidos_locais': [], 'b2b': None})
    logger.info('crm card consultado: chave=%s', chave)
    return jsonify(_buscar_por_telefone(chave))


def _buscar_por_telefone(chave):
    """Agrega histórico do cliente por chave de telefone canônica.

    Como `telefone_chave` é Python-side (não SQL), filtramos em memória.
    Volume de PedidoLocal/ClienteB2B é baixo (pedidos manuais + clientes
    recorrentes); se crescer, adicionar coluna normalizada indexada.
    """
    from app.models import ClienteB2B, PedidoLocal, PedidoSite, VendaB2B

    pedidos = []
    for p in (PedidoLocal.query
              .order_by(PedidoLocal.data_entrega.desc())
              .all()):
        if telefone_chave(p.telefone) != chave:
            continue
        pedidos.append({
            'code': p.code,
            'destinatario': p.destinatario,
            'data_entrega': p.data_entrega.strftime('%d/%m/%Y') if p.data_entrega else '',
            'total': round(p.total, 2),
            'itens': [{'nome': i.nome, 'qtd': i.quantidade,
                       'preco': round(i.preco_unitario or 0, 2)} for i in p.itens],
        })

    b2b = None
    cliente = next((c for c in ClienteB2B.query.all()
                    if telefone_chave(c.telefone) == chave), None)
    if cliente:
        vendas = (VendaB2B.query
                  .filter_by(cliente_id=cliente.id)
                  .filter(VendaB2B.status != 'cancelada')
                  .order_by(VendaB2B.data_venda.desc())
                  .all())
        debito = sum((v.valor_aberto for v in vendas), Decimal('0'))
        b2b = {
            'nome': cliente.nome,
            'contato': cliente.contato,
            'debito_aberto': float(debito),
            'vendas': [{
                'id': v.id,
                'data': (v.data_entrega or v.data_venda).strftime('%d/%m/%Y')
                        if (v.data_entrega or v.data_venda) else '',
                'valor_total': float(v.valor_total or 0),
                'valor_aberto': float(v.valor_aberto),
            } for v in vendas[:20]],
        }

    # Pedidos do site (VNDA) — cache local indexado por telefone_chave,
    # populado por app.services.vnda_card. Lookup direto (rapido).
    site = []
    for p in (PedidoSite.query
              .filter_by(telefone_chave=chave)
              .order_by(PedidoSite.data_pedido.desc())
              .limit(20).all()):
        try:
            itens = json.loads(p.itens_json) if p.itens_json else []
        except (ValueError, TypeError):
            itens = []
        site.append({
            'code': p.code,
            'destinatario': p.destinatario or p.comprador or '',
            'data': (p.data_pedido or p.data_entrega).strftime('%d/%m/%Y')
                    if (p.data_pedido or p.data_entrega) else '',
            'total': float(p.total or 0),
            'status': p.status_vnda or '',
            'itens': itens,
        })

    return {
        'ok': True,
        'encontrado': bool(pedidos or b2b or site),
        'telefone': chave,
        'pedidos_locais': pedidos,
        'b2b': b2b,
        'pedidos_site': site,
    }


# ── Agent Bot (atendimento automatico via Chatwoot) ──


def _bot_secret_ok(recebido):
    esperado = (current_app.config.get('CHATWOOT_BOT_SECRET') or '').strip()
    if not esperado:
        return False
    return secrets.compare_digest(str(recebido or ''), esperado)


def _e_story_mention_instagram(payload, conv):
    """Detecta marcacao em story do Instagram (caso real 16/06/2026): alguem
    marca @opao em uma story, Chatwoot empurra pelo mesmo webhook de bot, e
    o bot tentava 'atender' a story como pergunta de cliente.

    Retorna string com o sinal que casou, ou '' se nao for story mention.
    Heuristica conservadora — combina varios sinais pra evitar falso positivo
    (ex: cliente real mandando foto de produto no IG DM).

    Sinais (qualquer um vale; o primeiro que bate ganha):
    1. `content_attributes.message_type` explicito ('story_mention',
       'instagram_story_mention') — Chatwoot moderno expoe isso direto.
    2. `content_attributes.in_reply_to_external_source_id` com prefixo
       de story (`ig_reel_`, `story_`) — referencia a story original.
    3. Inbox Instagram + content vazio + anexo com URL de CDN do
       Instagram (cdninstagram.com/fbcdn) — fallback observacional.

    Falso positivo aceitavel: handoff silencioso pra equipe decidir. Falso
    negativo (deixou passar) ja era o comportamento antigo — sem regressao.
    """
    content_attrs = payload.get('content_attributes') or {}
    msg_type = (content_attrs.get('message_type') or '').lower()
    if 'story_mention' in msg_type or 'story-mention' in msg_type:
        return f'content_attributes.message_type={msg_type}'

    ref = (content_attrs.get('in_reply_to_external_source_id') or '')
    if isinstance(ref, str) and (ref.startswith('ig_reel_')
                                  or ref.startswith('story_')
                                  or 'story' in ref.lower()[:30]):
        return f'in_reply_to_external_source_id={ref[:40]}'

    inbox = (conv.get('meta') or {}).get('channel') or payload.get('inbox') or {}
    channel = (inbox.get('channel_type') if isinstance(inbox, dict) else '') or ''
    channel = str(channel).lower()
    e_ig = ('instagram' in channel) or ('instagram' in str(payload.get('source') or '').lower())
    if not e_ig:
        return ''
    content = (payload.get('content') or '').strip()
    if content:
        return ''  # IG com texto = DM real, deixa passar
    anexos = payload.get('attachments') or []
    for a in anexos:
        url = str(a.get('data_url') or a.get('file_url') or '').lower()
        if ('cdninstagram' in url) or ('fbcdn' in url) or ('ig_cache' in url):
            return f'inbox_ig + content vazio + anexo CDN ({url[:60]}...)'
    return ''


@crm_bp.route('/bot', methods=['POST'])
def bot_webhook():
    """Webhook do Agent Bot do Chatwoot.

    So processa mensagem NOVA do cliente (incoming) em conversa 'pending'
    (turno do bot). Ignora mensagens do proprio bot/atendente (outgoing) —
    evita loop infinito — e conversas ja 'open' (humano assumiu). Autentica
    pelo segredo na URL (CHATWOOT_BOT_SECRET).
    """
    if not _bot_secret_ok(request.args.get('k')):
        return jsonify({'ok': False, 'erro': 'token inválido'}), 403

    payload = request.get_json(silent=True) or {}
    if payload.get('event') != 'message_created':
        return jsonify({'ok': True, 'ignorado': 'evento'})
    if payload.get('message_type') not in ('incoming', 0):
        return jsonify({'ok': True, 'ignorado': 'nao-incoming'})
    if payload.get('private'):
        return jsonify({'ok': True, 'ignorado': 'nota'})

    conv = payload.get('conversation') or {}

    # Story mention do Instagram NAO eh atendimento — eh marcacao social
    # (alguem marcou a conta @opao numa story). Chatwoot manda pelo mesmo
    # webhook do bot, e o bot tentava "atender" a story como se fosse
    # pergunta de cliente — incidente 16/06/2026. Detecta e faz handoff
    # silencioso: muda pra 'open' (equipe decide se responde) sem chamar
    # o Claude. Heuristica conservadora: combina varios sinais pra evitar
    # falso positivo (cliente mandando so uma foto de produto).
    ig_mention = _e_story_mention_instagram(payload, conv)
    if ig_mention:
        conv_id_log = conv.get('id') or payload.get('conversation_id')
        logger.info('crm/bot: story mention IG conv=%s — handoff silencioso (%s)',
                    conv_id_log, ig_mention)
        if conv_id_log:
            try:
                from app.services import chatwoot
                chatwoot.definir_status(conv_id_log, 'open')
            except Exception:  # noqa: BLE001
                logger.exception('crm/bot: handoff de story mention falhou')
        return jsonify({'ok': True, 'ignorado': 'ig-story-mention',
                        'motivo': ig_mention})

    # Código do PORTAL WI-FI (WIFI-XXXXXX) fura o gate de status: a
    # validação de posse do número tem que funcionar mesmo se o cliente já
    # tiver conversa 'open' com atendente (senão o código cai no colo do
    # humano e o cadastro morre). O processamento fica adiante (depois do
    # dedupe/telefone); aqui só deixamos passar.
    from app.services import wifi_portal as _wifi
    _eh_codigo_wifi = bool(_wifi.RE_CODIGO_WIFI.search(
        (payload.get('content') or '')))

    if (conv.get('status') or '') != 'pending' and not _eh_codigo_wifi:
        # Log com status real recebido — diagnostico de '"Olá" do cliente
        # nao acordou o bot' (incidente 12/06/2026, conv #198): mensagem
        # nova em conversa resolved/open passa por aqui em silencio.
        # Saber o STATUS exato decide se e config do Chatwoot (reabrir
        # como 'pending') ou bug nosso.
        logger.info('crm/bot ignora: status=%s conv=%s',
                    conv.get('status'),
                    conv.get('id') or payload.get('conversation_id'))
        return jsonify({'ok': True, 'ignorado': 'nao-pending',
                        'status': conv.get('status')})
    conv_id = conv.get('id') or payload.get('conversation_id')
    if not conv_id:
        return jsonify({'ok': True, 'ignorado': 'sem-conversa'})
    content = (payload.get('content') or '').strip()

    # Idempotencia: Chatwoot reenvia message_created se o webhook demora
    # (bot precisa de Claude + tools, passa de 5s as vezes). Sem isso,
    # mesma mensagem vira 2 turnos do bot — duplica resposta no canal e
    # gasta token a toa. PK do payload identifica a mensagem unica.
    msg_id = payload.get('id') or payload.get('message_id')
    if msg_id:
        from app.extensions import db as _db
        from app.models import ChatwootEventoProcessado
        msg_id_str = str(msg_id)[:80]
        ja = ChatwootEventoProcessado.query.filter_by(
            message_id=msg_id_str).first()
        if ja:
            logger.info('crm/bot: msg %s ja processada (conv=%s) — ignora',
                         msg_id_str, conv_id)
            return jsonify({'ok': True, 'ignorado': 'duplicado'})
        # Grava ANTES do processamento async — se o webhook for reenviado
        # enquanto o primeiro ainda esta rodando, o segundo cai no
        # IntegrityError (UNIQUE PK) e e ignorado.
        try:
            _db.session.add(ChatwootEventoProcessado(
                message_id=msg_id_str,
                conversation_id=str(conv_id)[:40]))
            _db.session.commit()
        except Exception:  # noqa: BLE001
            _db.session.rollback()
            logger.info('crm/bot: msg %s race no commit — ignora', msg_id_str)
            return jsonify({'ok': True, 'ignorado': 'race-duplicado'})

    # Processa em SEGUNDO PLANO e responde o webhook na hora. O Agent Bot do
    # Chatwoot tem timeout curto: se a gente fizer Claude + ferramentas antes de
    # responder, ele marca a conversa como erro e joga pro humano. Devolvemos
    # 200 imediato; o bot responde via API depois.
    import threading

    app = current_app._get_current_object()

    contato = ((payload.get('sender') or {}).get('name')
               or ((conv.get('meta') or {}).get('sender') or {}).get('name') or '')

    # Telefone do contato (canonico, sem 55 inicial) — vai pro `chatbot.responder`
    # e dali pras tools que autorizam dono de pedido (consultar_pedido,
    # editar_cartinha_pedido). NUNCA pegar telefone que o cliente CHAMA — so do
    # canal. Se canal nao tiver telefone (IG, site), fica vazio e o bot cai no
    # fallback de CPF.
    sender = (payload.get('sender') or {})
    sender_meta = ((conv.get('meta') or {}).get('sender') or {})

    def _tel_ok(bruto):
        # So aceita o que tem CARA de telefone BR (10-13 digitos): o
        # `identifier` de canal IG e um ID scoped de ~17 digitos e o
        # telefone_chave pegaria os ultimos 10 — viraria pseudo-telefone
        # alimentando memoria/busca por engano (revisao 19/07/2026).
        d = ''.join(ch for ch in str(bruto or '') if ch.isdigit())
        return telefone_chave(bruto) if 10 <= len(d) <= 13 else ''

    telefone_contato = (
        _tel_ok(sender.get('phone_number'))
        or _tel_ok(sender.get('identifier'))
        or _tel_ok(sender_meta.get('phone_number'))
        or _tel_ok(sender_meta.get('identifier'))
    )

    # Numeros IGNORADOS (bots externos — caso gov.br 03/07/2026, loop que
    # gerou 6 alertas ALTA sem cliente real): resolve em silencio, sem gastar
    # Claude e sem encher a fila humana. CSV em CHATBOT_NUMEROS_IGNORADOS,
    # comparado pela mesma chave canonica de telefone do resto do sistema.
    ignorados_csv = (current_app.config.get('CHATBOT_NUMEROS_IGNORADOS') or '')
    if telefone_contato and ignorados_csv.strip():
        ignorados = {telefone_chave(t) for t in ignorados_csv.split(',')}
        ignorados.discard('')
        if telefone_contato in ignorados:
            logger.info('crm/bot: numero na lista de ignorados conv=%s — '
                        'resolve silencioso', conv_id)
            try:
                from app.services import chatwoot
                chatwoot.definir_status(conv_id, 'resolved')
            except Exception:  # noqa: BLE001
                logger.exception('crm/bot: resolve de numero ignorado falhou '
                                 'conv=%s', conv_id)
            return jsonify({'ok': True, 'ignorado': 'numero-ignorado'})

    # PORTAL WI-FI: mensagem com código WIFI-XXXXXX é validação de posse do
    # número — resposta determinística (sem Claude) e fim. O telefone do
    # REMETENTE é a prova; o service resolve a conta do site e devolve o
    # link de login. Em conversa 'pending' resolvemos (não é atendimento);
    # em 'open' só respondemos (não roubar a conversa do atendente).
    if _eh_codigo_wifi:
        from app.services import chatwoot as _cw
        telefone_raw = (sender.get('phone_number')
                        or sender_meta.get('phone_number')
                        or telefone_contato or '')
        try:
            res_wifi = _wifi.processar_codigo_whatsapp(content, telefone_raw)
        except Exception:  # noqa: BLE001
            logger.exception('crm/bot: portal wifi falhou conv=%s', conv_id)
            res_wifi = None
        if res_wifi is not None:
            try:
                _cw.enviar_mensagem(conv_id, res_wifi['texto'])
                if (conv.get('status') or '') == 'pending':
                    _cw.definir_status(conv_id, 'resolved')
            except Exception:  # noqa: BLE001
                logger.exception('crm/bot: resposta wifi falhou conv=%s',
                                 conv_id)
            return jsonify({'ok': True, 'wifi_portal': True})
        # Regex casou mas o service não tratou (erro interno): segue o
        # fluxo normal do bot — melhor o bot responder algo do que nada.

    # Lock por conv_id: serializa threads que processam a MESMA conversa
    # (mensagens consecutivas do cliente no WhatsApp = webhooks paralelos).
    _lock_conv = _lock_para_conv(conv_id)

    # Imagens da mensagem ATUAL (vem do webhook, nao do historico).
    anexos = payload.get('attachments') or []
    imagens_atuais = [a.get('data_url') for a in anexos
                      if a.get('file_type') == 'image' and a.get('data_url')]

    # ÁUDIO/anexo nao suportado (02/07/2026): antes, msg so de audio caia num
    # `return` silencioso — conversa PRESA em pending pra sempre (o follow-up
    # nao dispara porque a ultima msg e do cliente). Agora responde
    # deterministicamente pedindo texto, sem gastar Claude.
    if not content and not imagens_atuais and anexos:
        def _responder_sem_suporte():
            with app.app_context():
                from app.services import chatbot, chatwoot
                with _lock_conv, _lock_conv_cross_worker(conv_id):
                    try:
                        texto = ('Ainda não consigo ouvir áudios ou abrir esse '
                                 'tipo de arquivo por aqui. Pode me escrever? '
                                 'Assim te respondo na hora.')
                        chatwoot.enviar_mensagem(conv_id, texto)
                        base = chatbot.carregar_historico(conv_id)
                        chatbot.salvar_historico(
                            conv_id,
                            base + [{'role': 'user',
                                     'content': '[cliente enviou áudio/anexo não suportado]'}],
                            texto, contato_key=telefone_contato)
                    except Exception:
                        logger.exception('crm bot resposta a anexo nao suportado '
                                         'falhou conv=%s', conv_id)
        threading.Thread(target=_responder_sem_suporte, daemon=True).start()
        return jsonify({'ok': True, 'acao': 'anexo-nao-suportado'})

    if not content and not imagens_atuais:
        return jsonify({'ok': True, 'ignorado': 'vazio'})

    # Deposita no buffer de rajada; a thread que acordar primeiro do debounce
    # drena TUDO e responde uma vez só.
    _depositar_pendente(conv_id, content, imagens_atuais)

    def _processar():
        with app.app_context():
            import time as _time

            from app.services import chatbot, chatwoot
            espera = _debounce_segundos()
            if espera > 0:
                _time.sleep(espera)
            with _lock_conv, _lock_conv_cross_worker(conv_id):
                resultado = None
                historico = None
                texto_enviado = False
                try:
                    conteudo, imagens = _drenar_pendentes(conv_id)
                    if conteudo is None:
                        return   # outra thread da rajada ja respondeu tudo
                    msg_atual = {'role': 'user', 'content': conteudo}
                    if imagens:
                        msg_atual['imagens'] = imagens

                    # Contexto vem do NOSSO banco (confiavel). So semeia do
                    # Chatwoot na 1a vez (conv nova ou anterior a este fix). A
                    # msg atual SEMPRE entra como ultima — nunca depende do
                    # Chatwoot pra estar presente.
                    store = chatbot.carregar_historico(conv_id)
                    if store:
                        base = store
                    else:
                        seed = chatwoot.buscar_historico(conv_id) or []
                        # O seed do Chatwoot costuma ja terminar com a msg atual;
                        # tira pra nao duplicar (vamos re-adicionar msg_atual,
                        # que carrega as imagens).
                        if (seed and seed[-1].get('role') == 'user'
                                and (seed[-1].get('content') or '').strip()
                                in (conteudo or '').strip()):
                            seed = seed[:-1]
                        base = seed
                        if not base:
                            # Conversa NOVA no Chatwoot: herda o contexto
                            # recente do MESMO contato (auditor 19/07/2026:
                            # cliente voltava e o bot recomeçava do zero).
                            base = chatbot.contexto_do_contato(
                                telefone_contato, excluir_conv=conv_id)
                            if not base:
                                current_app.logger.info(
                                    'crm/bot: conv %s sem historico previo — conversa nova', conv_id)
                    historico = base + [msg_atual]
                    current_app.logger.info('crm/bot: conv %s historico=%d msgs',
                                            conv_id, len(historico))

                    # Uma segunda mensagem pode entrar enquanto o primeiro
                    # handoff ainda esta mudando `pending` para `open`. Dentro
                    # do lock, o marcador persistido vence o status antigo do
                    # webhook: guarda a fala e mantem o bot em silencio.
                    if chatbot.handoff_recente(conv_id):
                        resultado = {
                            'acao': 'handoff_repetido',
                            'texto': '',
                            'motivo': 'conversa ja transferida recentemente',
                        }
                    else:
                        resultado = chatbot.responder(
                            historico, telefone_contato=telefone_contato,
                            conversa_id=conv_id)
                    # Rede de seguranca: se algum caminho ainda produzir um
                    # handoff depois da checagem acima, mantem silencio. Status
                    # ainda vai pra `open` (idempotente, garante a fila), sem
                    # 2º registro de handoff nem outra fala ao cliente.
                    if (resultado.get('acao') == 'handoff'
                            and chatbot.handoff_recente(conv_id)):
                        logger.info('crm bot handoff REPETIDO suavizado '
                                    'conv=%s (motivo=%s)', conv_id,
                                    resultado.get('motivo'))
                        resultado = dict(
                            resultado, acao='handoff_repetido',
                            texto=chatbot.TEXTO_HANDOFF_REPETIDO)
                    if resultado.get('texto'):
                        chatwoot.enviar_mensagem(conv_id, resultado['texto'])
                        texto_enviado = True
                    # Persiste o turno (msg atual + resposta) pro proximo contexto
                    chatbot.salvar_historico(
                        conv_id, historico, resultado.get('texto') or '',
                        handoff=(resultado['acao'] == 'handoff'),
                        contato_key=telefone_contato)
                    if resultado['acao'] in ('handoff', 'handoff_repetido'):
                        res_status = chatwoot.definir_status(conv_id, 'open')
                        if res_status.get('ok'):
                            logger.info('crm bot handoff conv=%s motivo=%s',
                                        conv_id, resultado.get('motivo'))
                        else:
                            # NUNCA silenciar: se o status nao mudou, a conversa
                            # fica presa no bot e o cliente espera. ERROR -> Sentry.
                            logger.error(
                                'crm bot handoff conv=%s: definir_status FALHOU '
                                '(%s) — conversa pode ter ficado presa no bot',
                                conv_id, res_status.get('erro'))
                    elif resultado['acao'] == 'encerrar':
                        # Cliente fechou com "obrigada/valeu" e o bot ja tinha
                        # resolvido — silencio + resolved no Chatwoot. Quando
                        # cliente mandar nova msg, Chatwoot reabre como pending
                        # e o bot atende normal.
                        chatwoot.definir_status(conv_id, 'resolved')
                        logger.info('crm bot encerrou conv=%s motivo=%s',
                                    conv_id, resultado.get('motivo'))
                except Exception:
                    logger.exception('crm bot processamento falhou conv=%s', conv_id)
                    # Nunca deixa o cliente sem resposta: AVISA o cliente e
                    # joga pro humano. Antes (02/07/2026) so mudava o status —
                    # a conversa ia pra fila humana EM SILENCIO e o cliente
                    # ficava olhando pro nada ate alguem assumir.
                    try:
                        if not texto_enviado:
                            chatwoot.enviar_mensagem(conv_id, chatbot.FALLBACK_TEXTO)
                    except Exception:
                        logger.exception('crm bot fallback msg falhou conv=%s', conv_id)
                    try:
                        chatwoot.definir_status(conv_id, 'open')
                    except Exception:
                        logger.exception('crm bot fallback handoff falhou conv=%s', conv_id)

                # Vigia: assiste a conversa DEPOIS do bot ter respondido (nao
                # atrasa o cliente) e alerta no WhatsApp do dono via Z-API quando
                # detecta problema. Best-effort: falha do vigia nunca afeta o
                # atendimento. Dentro do lock pra serializar com proximos turnos.
                try:
                    from app.services import chatbot_vigia
                    encerrou_por_loop = (
                        resultado is not None
                        and resultado.get('acao') == 'encerrar'
                        and 'loop' in (resultado.get('motivo') or ''))
                    if encerrou_por_loop:
                        # Loop bot-a-bot silenciado: o vigia alertando aqui era
                        # exatamente o ruido (6 ALTAs sem cliente real, 03/07).
                        historico = None
                    if historico and chatbot_vigia.disponivel():
                        # Anexa a resposta do bot ao historico — sem isso, o vigia
                        # ve so a fala do cliente e nunca consegue julgar o que o
                        # bot disse (caso classico: "bot afirmou esgotado mas tem
                        # na loja"). Lista nova pra nao mutar o original.
                        hist_pra_vigia = list(historico)
                        if resultado and (resultado.get('texto') or '').strip():
                            hist_pra_vigia.append({
                                'role': 'assistant',
                                'content': resultado['texto'].strip(),
                            })
                        chatbot_vigia.avaliar(hist_pra_vigia, conv_id=conv_id,
                                              nome_contato=contato,
                                              resultado_bot=resultado)
                except Exception:
                    logger.exception('crm vigia falhou conv=%s', conv_id)

    threading.Thread(target=_processar, daemon=True).start()
    return jsonify({'ok': True})
