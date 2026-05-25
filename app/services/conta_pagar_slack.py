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


def processar(evento, aovivo=True):
    """Processa uma mensagem de canal de NF. Retorna nº de contas criadas.

    `aovivo=True` (captura ao vivo) processa preco/estoque dos itens ja
    mapeados+confirmados. `aovivo=False` (importacao de historico) so cria os
    mapeamentos pendentes — nao mexe no estoque (decisao do usuario).
    """
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
            vencimento=_parse_vencimento(dados),
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
            continue
        # Junta automaticamente com a NF/boleto do mesmo recebimento, se houver.
        try:
            from app.services import conta_pagar as cp_dominio
            cp_dominio.tentar_agrupar(conta)
        except Exception:  # noqa: BLE001
            logger.exception('conta_pagar_slack: agrupamento falhou (file %s)', file_id)

        # Preco + entrada de estoque dos itens ja mapeados+confirmados. Itens
        # novos viram mapeamentos pendentes (nao bloqueiam). aovivo=False so
        # cria os pendentes, sem tocar no estoque.
        try:
            from app.services import conta_pagar_estoque
            conta_pagar_estoque.processar_conta(conta, aovivo=aovivo)
        except Exception:  # noqa: BLE001
            logger.exception('conta_pagar_slack: processar estoque falhou (file %s)',
                             file_id)

    return criadas


def _data_iso(s):
    from datetime import datetime
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:10], '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


def _parse_vencimento(dados):
    """Vencimento dando prioridade ao texto cru do documento em DD/MM/AAAA.

    Documentos brasileiros usam DD/MM/AAAA. A IA as vezes inverte dia/mes ao
    converter pra ISO. Reparseando o texto cru aqui (BR deterministico)
    elimina essa inversao; o ISO da IA fica so como fallback.
    """
    import re
    from datetime import date

    txt = dados.get('vencimento_texto')
    if txt:
        m = re.match(r'\s*(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})\s*$', str(txt))
        if m:
            d, mes, ano = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if ano < 100:
                ano += 2000
            if 1 <= d <= 31 and 1 <= mes <= 12:
                try:
                    return date(ano, mes, d)
                except ValueError:
                    pass
    return _data_iso(dados.get('vencimento'))


def importar_historico(app, dias=30):
    """Varre o historico dos canais de NF (ultimos `dias`) e processa as
    imagens/PDFs que ainda nao viraram conta. Idempotente por slack_file_id.

    Roda em background (a rota dispara num thread). Retorna nº de contas
    criadas (tambem loga).
    """
    import time as _time

    from app.services import slack as slack_api

    with app.app_context():
        ids = (app.config.get('SLACK_CANAIS_NF') or '').strip()
        canais = [c.strip() for c in ids.split(',') if c.strip()]
        if not canais:
            logger.info('importar_historico: SLACK_CANAIS_NF vazio')
            return 0

        oldest = _time.time() - dias * 86400
        total = 0
        for canal in canais:
            cursor = None
            parar = False
            while not parar:
                msgs, cursor = slack_api.historico_canal(canal, oldest=oldest,
                                                         cursor=cursor)
                for m in msgs:
                    # Salvaguarda: msgs vem da mais nova pra mais antiga. Se
                    # cruzar o limite, para (a API as vezes ignora `oldest` na
                    # paginacao por cursor).
                    try:
                        if float(m.get('ts', 0)) < oldest:
                            parar = True
                            break
                    except (TypeError, ValueError):
                        pass
                    if not m.get('files'):
                        continue
                    evento = {
                        'channel': canal,
                        'user': m.get('user'),
                        'ts': m.get('ts'),
                        'files': m.get('files'),
                    }
                    try:
                        total += processar(evento, aovivo=False)
                    except Exception:  # noqa: BLE001
                        logger.exception('importar_historico: msg %s falhou',
                                         m.get('ts'))
                if not cursor:
                    break
                _time.sleep(1)  # respeita rate limit Slack/Anthropic
        logger.info('importar_historico: %d conta(s) criada(s)', total)
        return total
