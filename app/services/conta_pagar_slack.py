"""Captura de NF/boleto postados no Slack → Conta a Pagar.

O bot SO LE os canais de recebimento (nunca posta). Pra cada arquivo
(imagem ou PDF) de uma mensagem:
  1. baixa do Slack (bot token);
  2. sobe pro Dropbox (documento original preservado ANTES de qualquer IA);
  3. extrai dados via IA (Sonnet, fallback Opus);
  4. cria ContaPagar (idempotente por slack_file_id).

Nunca exige SlackVinculo (funcionarios de loja nao tem) e nunca responde.
"""
import json
import logging

from app.extensions import db
from app.models import ContaPagar

logger = logging.getLogger(__name__)

_EXT_POR_MIME = {
    'image/jpeg': 'jpg', 'image/png': 'png', 'image/webp': 'webp',
    'image/heic': 'heic', 'application/pdf': 'pdf',
}


def canal_de_nf(channel_id):
    """True se o canal eh um dos canais de recebimento de NF (SLACK_CANAIS_NF)."""
    from flask import current_app
    ids = (current_app.config.get('SLACK_CANAIS_NF') or '').strip()
    if not ids:
        return False
    return channel_id in {c.strip() for c in ids.split(',') if c.strip()}


def _ja_processado(file_id):
    if not file_id:
        return False
    return (ContaPagar.query
            .filter_by(slack_file_id=file_id).first() is not None)


def _nome_enviante(slack_user_id):
    from app.services import slack as slack_api
    if not slack_user_id:
        return None
    try:
        info = slack_api.info_usuario(slack_user_id) or {}
        return (info.get('real_name') or info.get('name')
                or (info.get('profile') or {}).get('real_name'))
    except Exception:  # noqa: BLE001
        return None


def processar(evento):
    """Processa uma mensagem de canal de NF. Retorna nº de contas criadas."""
    from app.services import conta_pagar_ia, dropbox_storage
    from app.services import slack as slack_api
    from app.utils import agora as _agora

    channel = evento.get('channel')
    files = evento.get('files') or []
    ts = evento.get('ts') or evento.get('event_ts')
    slack_user_id = evento.get('user')
    if not files:
        return 0

    enviado_por = _nome_enviante(slack_user_id)
    criadas = 0

    for f in files:
        file_id = f.get('id')
        mime = (f.get('mimetype') or '').lower()
        # So imagem ou PDF (boleto/NF). Outros tipos ignora.
        if not (mime.startswith('image/') or mime == 'application/pdf'):
            continue
        if _ja_processado(file_id):
            continue

        arq = slack_api.baixar_arquivo(f)
        if not arq:
            logger.warning('conta_pagar_slack: falha ao baixar file %s', file_id)
            continue

        # 1) Documento original ANTES de qualquer IA (nao perde o doc).
        ext = _EXT_POR_MIME.get(mime, 'bin')
        ym = _agora().strftime('%Y-%m')
        path = f'/contas-pagar/{channel}/{ym}/{ts}_{file_id}.{ext}'
        url = storage_path = None
        try:
            up = dropbox_storage.upload_publico(arq['bytes'], path,
                                                mode='add', autorename=True)
            url, storage_path = up['url'], up['storage_path']
        except Exception:  # noqa: BLE001
            logger.exception('conta_pagar_slack: upload Dropbox falhou')

        # 2) Extracao IA (best-effort — se falhar, conta fica pra revisar)
        dados = conta_pagar_ia.extrair_documento(arq['bytes'], arq['mimetype'])
        if dados.get('erro'):
            logger.warning('conta_pagar_slack: IA falhou (%s) file %s',
                           dados['erro'], file_id)

        conta = ContaPagar(
            tipo_documento=dados.get('tipo_documento') or 'desconhecido',
            fornecedor_nome=dados.get('fornecedor'),
            valor_total=dados.get('valor_total'),
            vencimento=_data_iso(dados.get('vencimento')),
            nf_numero=str(dados.get('nf_numero')) if dados.get('nf_numero') else None,
            codigo_barras=dados.get('codigo_barras'),
            linha_digitavel=dados.get('linha_digitavel'),
            info_pagamento=dados.get('info_pagamento'),
            itens_json=json.dumps(dados.get('itens'), ensure_ascii=False)
            if dados.get('itens') else None,
            status='aberto',
            imagem_url=url,
            imagem_storage_path=storage_path,
            origem_canal=channel,
            slack_file_id=file_id,
            slack_ts=ts,
            enviado_por=enviado_por,
            dados_ia_json=json.dumps(dados, ensure_ascii=False)[:8000],
        )
        db.session.add(conta)
        try:
            db.session.commit()
            criadas += 1
        except Exception:  # noqa: BLE001
            db.session.rollback()
            logger.exception('conta_pagar_slack: commit falhou (file %s)', file_id)

    return criadas


def _data_iso(s):
    from datetime import datetime
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:10], '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None
