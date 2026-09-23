"""Corrida Lalamove encerrada SEM entrega (CANCELED / EXPIRED / REJECTED).

Até 23/09/2026 o webhook (`app/blueprints/lalamove/routes.py`) só gravava o
status: ninguém era avisado, o pedido ficava "a caminho" e o cliente
esperava um motoboy que não ia chegar. Agora, no primeiro evento de
encerramento sem entrega de cada corrida:

1. `VigiaVeredito` ALTA com código do pedido e motivo — o banner + som do
   /entregas/painel (mesma fonte dos alertas do vigia) e o histórico.
2. WhatsApp ao dono pelo caminho dos alertas CRÍTICOS (`loja_alerta`:
   `critico=True`, isento do teto/hora — pedido pago sem motoboy).
3. Conversa do cliente no Chatwoot (aberta/pendente do contato, ou a mais
   recente do nosso histórico em `JANELA_CONVERSA_DIAS`) vai para `open`
   — fila da EQUIPE, sem mensagem automática ao cliente.

NÃO marca entregue, NÃO muda o status do pedido (regra do dono). Rede fica
numa thread (o webhook responde na hora); dedupe por (order_id, status) no
próprio `VigiaVeredito` (`bot_motivo` = marcador), que cruza workers.
Cancelamento feito PELO PAINEL já grava CANCELED antes do webhook
(`api_lalamove_cancelar`), então `anterior == status` cala o alerta.
"""
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from flask import current_app

logger = logging.getLogger(__name__)

STATUS_SEM_ENTREGA = ('CANCELED', 'EXPIRED', 'REJECTED')
JANELA_CONVERSA_DIAS = 7
MOTIVO_DESCONHECIDO = 'motivo não informado pela Lalamove'
# `remarks` ficou FORA (revisão 23/09/2026): é o campo que NÓS preenchemos
# na criação da corrida ("Pedido X — O Pão… PRESENTE: …") e voltava como
# "motivo" do cancelamento.
_CHAVES_MOTIVO = ('cancelReason', 'cancellationReason', 'cancelledReason',
                  'reason', 'rejectReason', 'message')
# Anti-flood do WhatsApp CRÍTICO ao dono (revisão 23/09/2026, mesma
# preocupação do dono em 18/07: "cuidado pra não bloquear a conta"): até N
# alertas por hora saem isentos do teto global; do N+1 em diante o alerta
# segue pelo caminho normal do Z-API (teto/hora) — o painel e o
# VigiaVeredito já têm tudo. Env `LALAMOVE_ALERTA_MAX_CRITICO_HORA`.
MAX_CRITICO_HORA = 5
_CHAVE_CONTADOR = 'lalamove_alerta_critico_hora'

_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix='lalamove-alerta')


def _max_critico_hora():
    import os
    try:
        v = int(os.environ.get('LALAMOVE_ALERTA_MAX_CRITICO_HORA', '') or MAX_CRITICO_HORA)
    except ValueError:
        logger.warning('LALAMOVE_ALERTA_MAX_CRITICO_HORA ilegível — usando %s',
                       MAX_CRITICO_HORA)
        return MAX_CRITICO_HORA
    return max(0, v)


def _critico_permitido():
    """Conta os alertas da hora corrente em AppConfig (cruza workers) e diz
    se ESTE ainda pode sair como crítico. Fail-open: erro na contagem nunca
    cala um alerta crítico."""
    from app.extensions import db
    from app.models import AppConfig
    from app.utils import agora
    hora = agora().strftime('%Y%m%d%H')
    try:
        raw = AppConfig.get(_CHAVE_CONTADOR) or ''
        h, _, n = raw.partition(':')
        n = int(n) if (h == hora and n.isdigit()) else 0
        AppConfig.set(_CHAVE_CONTADOR, f'{hora}:{n + 1}')
        db.session.commit()
        return n < _max_critico_hora()
    except Exception:  # noqa: BLE001
        logger.exception('lalamove_alerta: contador de críticos falhou (fail-open)')
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return True


def marcador(order_id, status):
    """Chave de dedupe gravada em `VigiaVeredito.bot_motivo`."""
    return f'lalamove:{order_id}:{status}'


def motivo_do_payload(dados):
    """Motivo legível a partir do payload cru do webhook (parse liberal —
    a doc v3 não fixa o campo). `MOTIVO_DESCONHECIDO` quando não há."""
    data = (dados or {}).get('data') or {}
    ordem = data.get('order') or {}
    for fonte in (ordem, data, dados or {}):
        if not isinstance(fonte, dict):
            continue
        for k in _CHAVES_MOTIVO:
            v = fonte.get(k)
            if isinstance(v, dict):
                v = v.get('message') or v.get('description') or v.get('code')
            if v and str(v).strip():
                return str(v).strip()[:300]
    return MOTIVO_DESCONHECIDO


def _conversa_local_do_telefone(telefone):
    """conv_id da conversa mais recente do MESMO contato no nosso histórico
    (`ChatbotConversa.contato_key`, sem HTTP), dentro da janela; None se não há."""
    from app.models import ChatbotConversa
    from app.utils import agora, classificar_telefone, telefone_chave
    # Só telefone BR: a chave canônica descarta o DDI, e '+1 475-292-9850'
    # colidiria com (47) 9 5292-9850 — a conversa de OUTRO cliente iria
    # para `open` (revisão 23/09/2026).
    if classificar_telefone(telefone)['tipo'] not in ('br_celular', 'br_fixo'):
        return None
    key = telefone_chave(telefone)
    if not key:
        return None
    corte = agora() - timedelta(days=JANELA_CONVERSA_DIAS)
    try:
        row = (ChatbotConversa.query
               .filter(ChatbotConversa.contato_key == key,
                       ChatbotConversa.ultima_msg_em >= corte)
               .order_by(ChatbotConversa.ultima_msg_em.desc())
               .first())
    except Exception:  # noqa: BLE001
        logger.exception('lalamove_alerta: busca de conversa local falhou')
        return None
    cid = str(row.conv_id) if row else ''
    return cid if cid.isdigit() else None


def _texto_whatsapp(e, status, motivo, pedido, link_conversa):
    from app.services.lalamove import rotulo_status
    linhas = [f'🛵 CORRIDA LALAMOVE: {rotulo_status(status).upper()} — pedido {e.pedido_code}']
    quem = (e.destinatario or (pedido.nome_destinatario if pedido else None)
            or (pedido.nome_cliente if pedido else None) or '').strip()
    if quem:
        linhas.append(f'Quem recebe: {quem}')
    if pedido is not None and pedido.data_entrega:
        janela = f' {pedido.janela_entrega}' if pedido.janela_entrega else ''
        linhas.append(f'Entrega: {pedido.data_entrega.strftime("%d/%m")}{janela}')
    if (e.endereco_destino or '').strip():
        linhas.append(f'Endereço: {e.endereco_destino.strip()}')
    if (e.motorista_nome or e.motorista_telefone or '').strip():
        mot = (e.motorista_nome or 'motorista').strip()
        if (e.motorista_telefone or '').strip():
            mot += f' ({e.motorista_telefone.strip()})'
        linhas.append(f'Motorista: {mot}')
    linhas.append(f'Motivo: {motivo}')
    linhas.append('O pedido NÃO foi entregue nem mudou de status — chame outro '
                  'entregador no painel ou avise o cliente.')
    if link_conversa:
        linhas.append(f'Conversa do cliente: {link_conversa}')
    return '\n'.join(linhas)


def tratar_encerramento(e, status, anterior, dados):
    """Chamado pelo webhook DEPOIS de persistir `e.status`. Best-effort:
    nunca levanta. Devolve dict com o que fez (pra teste/diagnóstico)."""
    try:
        return _tratar(e, status, anterior, dados)
    except Exception:  # noqa: BLE001
        logger.exception('lalamove_alerta: falhou pra ordem %s (%s)',
                         getattr(e, 'order_id', '?'), status)
        try:
            from app.extensions import db
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {'ok': False, 'erro': 'excecao'}


def _tratar(e, status, anterior, dados):
    from app.extensions import db
    from app.models import PedidoOnline, VigiaVeredito
    from app.services import chatbot_vigia
    from app.services.lalamove import rotulo_status
    from app.utils import agora

    status = (status or '').upper()
    if status not in STATUS_SEM_ENTREGA:
        return {'ok': True, 'ignorado': 'status'}
    if (anterior or '').upper() == status:
        # Cancelamento pelo painel (a rota já gravou CANCELED) ou reentrega
        # do mesmo evento: a equipe já sabe.
        return {'ok': True, 'ignorado': 'ja_estava'}
    marca = marcador(e.order_id, status)
    if VigiaVeredito.query.filter_by(bot_motivo=marca).first() is not None:
        return {'ok': True, 'ignorado': 'duplicado'}

    motivo = motivo_do_payload(dados)
    pedido = (PedidoOnline.query.filter_by(codigo=e.pedido_code).first()
              if e.pedido_code else None)
    # Evento fora de ordem (CANCELED chegando DEPOIS do COMPLETED) ou pedido
    # já entregue por outro caminho: alertar "não entregue" mentiria.
    if (anterior or '').upper() == 'COMPLETED' or (
            pedido is not None and pedido.status == 'entregue'):
        return {'ok': True, 'ignorado': 'ja_entregue'}
    if pedido is not None and pedido.status == 'cancelado':
        # Pedido cancelado/reembolsado: a corrida cai por consequência —
        # "chame outro entregador" seria errado (revisão 23/09/2026).
        return {'ok': True, 'ignorado': 'pedido_cancelado'}
    telefone_cliente = (pedido.telefone_cliente if pedido else None) or ''
    conv_id = _conversa_local_do_telefone(telefone_cliente)
    rotulo = rotulo_status(status)
    quem = (e.destinatario or (pedido.nome_cliente if pedido else None) or '').strip()
    resumo = f'[LALAMOVE {status}] pedido {e.pedido_code} — {rotulo}: {motivo}'
    row = VigiaVeredito(
        criado_em=agora(),
        conv_id=conv_id,
        cliente=(quem or f'pedido {e.pedido_code}')[:200],
        mensagem_cliente=resumo[:2000],
        bot_acao='lalamove',
        bot_motivo=marca,
        alerta=True,
        gravidade='alta',
        motivo_vigia=(f'Corrida Lalamove {rotulo.lower()} — pedido {e.pedido_code}: '
                      f'{motivo}. Chame outro entregador ou avise o cliente; '
                      'o pedido segue sem entrega.')[:1000],
        enviado_whatsapp=False,
    )
    db.session.add(row)
    db.session.commit()
    row_id = row.id
    link = chatbot_vigia.link_chatwoot(conv_id) if conv_id else ''
    texto = _texto_whatsapp(e, status, motivo, pedido, link)
    app = current_app._get_current_object()
    _POOL.submit(_executar_rede, app, row_id, texto, conv_id, telefone_cliente,
                 e.pedido_code, f'CORRIDA LALAMOVE {rotulo.upper()}', motivo)
    logger.warning('lalamove_alerta: %s (veredito %s, conversa %s)',
                   resumo, row_id, conv_id or '-')
    return {'ok': True, 'veredito_id': row_id, 'conv_id': conv_id,
            'motivo': motivo, 'texto': texto}


def _executar_rede(app, veredito_id, texto, conv_id, telefone_cliente,
                   pedido_code=None, rotulo=None, motivo=''):
    """Thread: WhatsApp ao dono (crítico) + conversa do cliente → open +
    vínculo conversa↔pedido e nota privada nas conversas abertas do pedido
    (item 8). Nunca escreve pro cliente."""
    with app.app_context():
        from app.extensions import db
        from app.models import VigiaVeredito
        from app.services import chatwoot, loja_alerta, zapi
        enviado = False
        try:
            numero = loja_alerta._numero_destino()
            if numero:
                critico = _critico_permitido()
                if not critico:
                    texto += ('\n(alerta além do limite de críticos desta hora — '
                              'os demais seguem no painel de entregas)')
                res = zapi.enviar_texto(numero, texto, critico=critico)
                enviado = bool(isinstance(res, dict) and res.get('ok'))
            else:
                logger.info('lalamove_alerta: sem numero de destino do dono')
        except Exception:  # noqa: BLE001
            logger.exception('lalamove_alerta: WhatsApp ao dono falhou')
        conv = conv_id
        try:
            if not conv:
                conv = _conversa_no_chatwoot(chatwoot, telefone_cliente)
            if conv:
                r = chatwoot.definir_status(conv, 'open', critico=False)
                if not (isinstance(r, dict) and r.get('ok')):
                    logger.warning('lalamove_alerta: conversa %s nao foi para open (%s)',
                                   conv, (r or {}).get('erro'))
        except Exception:  # noqa: BLE001
            logger.exception('lalamove_alerta: abrir conversa %s falhou', conv)
        try:
            row = db.session.get(VigiaVeredito, veredito_id)
            if row is not None:
                row.enviado_whatsapp = enviado
                if conv and not row.conv_id:
                    row.conv_id = str(conv)
                db.session.commit()
        except Exception:  # noqa: BLE001
            logger.exception('lalamove_alerta: atualizar veredito %s falhou', veredito_id)
            db.session.rollback()


def _conversa_no_chatwoot(chatwoot, telefone_cliente):
    """Conversa open/pending do contato na inbox do WhatsApp (1-2 GETs),
    só quando o telefone é celular BR; None se não há ou sem config."""
    from app.utils import telefone_e164_whatsapp
    fone = telefone_e164_whatsapp(telefone_cliente)
    inbox = chatwoot._whatsapp_inbox_id()
    if not (fone and inbox and chatwoot.disponivel()):
        return None
    contato = chatwoot._buscar_contato(fone)
    if not contato or not contato.get('id'):
        return None
    conv = chatwoot._conversa_aberta_do_contato(contato['id'], inbox)
    return str(conv) if conv else None
