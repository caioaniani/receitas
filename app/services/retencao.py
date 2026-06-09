"""Retencao de dados — limpeza automatica de PII e backups antigos.

Por que existe (auditoria 2026-06-09): NFLog, conversas do chatbot e os dumps
no Dropbox cresciam PRA SEMPRE — CPF parcial, endereco e conversa de cliente
acumulando sem prazo (risco LGPD + custo). Esta limpeza roda no cron diario
DEPOIS do backup dar OK (garantia: tudo que apagamos do banco esta no dump do
dia, recuperavel por 90 dias).

Prazos (env vars, defaults em config.py):
  RETENCAO_LOGS_DIAS=365       NFLog, VigiaVeredito (audit: 1 ano)
  RETENCAO_CONVERSAS_DIAS=180  ChatbotConversa sem atividade (contexto do bot;
                               a conversa-mestre vive no Chatwoot)
  RETENCAO_EVENTOS_DIAS=7      Slack/Zapi eventos processados (so idempotencia)
  RETENCAO_BACKUPS_DIAS=90     dumps no Dropbox (decisao do dono, 2026-06-09)

Desligar tudo: RETENCAO_AUTO=0. Rota manual (owner): /admin/retencao.
"""
import logging
import re
from datetime import datetime, timedelta

from flask import current_app

from app.utils import agora

logger = logging.getLogger(__name__)

_PASTAS_BACKUP = ('/backups-postgres', '/backups-chatwoot')


def _limite(dias):
    return agora() - timedelta(days=dias)


def executar_limpeza(dry_run=False):
    """Roda 1 ciclo de retencao. Retorna relatorio {alvo: qtd_apagada}.

    `dry_run=True` so CONTA o que seria apagado, sem tocar em nada — usado
    pela rota /admin/retencao pra inspecao antes de executar."""
    from app.extensions import db
    from app.models import (
        ChatbotConversa,
        NFLog,
        SlackEventoProcessado,
        VigiaVeredito,
        ZapiBotEventoProcessado,
    )
    cfg = current_app.config
    rel = {'dry_run': dry_run}

    # (modelo, coluna de data, dias) — escopo FECHADO e documentado por alvo.
    # NUNCA adicionar tabela de negocio (pedido/venda/estoque) aqui: retencao
    # eh so pra log/contexto/idempotencia, que tem copia ou perde valor.
    alvos_db = [
        ('nf_log', NFLog, NFLog.criado_em, cfg['RETENCAO_LOGS_DIAS']),
        ('vigia_veredito', VigiaVeredito, VigiaVeredito.criado_em,
         cfg['RETENCAO_LOGS_DIAS']),
        ('chatbot_conversa', ChatbotConversa, ChatbotConversa.ultima_msg_em,
         cfg['RETENCAO_CONVERSAS_DIAS']),
        ('slack_evento_processado', SlackEventoProcessado,
         SlackEventoProcessado.processado_em, cfg['RETENCAO_EVENTOS_DIAS']),
        ('zapi_bot_evento_processado', ZapiBotEventoProcessado,
         ZapiBotEventoProcessado.processado_em, cfg['RETENCAO_EVENTOS_DIAS']),
    ]

    for nome, modelo, coluna, dias in alvos_db:
        try:
            q = modelo.query.filter(coluna < _limite(dias))
            if dry_run:
                rel[nome] = q.count()
            else:
                rel[nome] = q.delete(synchronize_session=False)
                db.session.commit()
        except Exception:  # noqa: BLE001
            db.session.rollback()
            logger.exception('retencao: falha limpando %s', nome)
            rel[nome] = 'ERRO (ver log)'

    rel['backups_dropbox'] = _limpar_backups_dropbox(
        cfg['RETENCAO_BACKUPS_DIAS'], dry_run=dry_run)
    if not dry_run:
        logger.info('retencao: ciclo OK %s', rel)
    return rel


def _data_do_backup(arq):
    """Resolve a data de um dump: prioriza o timestamp do NOME
    (`padaria_2026-06-09_0400.dump.gz` — hora local BRT de criacao), com
    fallback no `server_modified` do Dropbox (UTC; ignoramos o fuso — erro
    de ate 3h eh irrelevante pra um corte de 90 dias)."""
    m = re.search(r'(\d{4}-\d{2}-\d{2})', arq.get('nome') or '')
    if m:
        try:
            return datetime.strptime(m.group(1), '%Y-%m-%d')
        except ValueError:
            pass
    mod = (arq.get('modificado') or '').rstrip('Z')
    try:
        return datetime.fromisoformat(mod)
    except ValueError:
        return None


def _limpar_backups_dropbox(dias, dry_run=False):
    """Apaga dumps com mais de N dias nas pastas de backup. Retorna a contagem
    (ou lista de nomes no dry_run). Nunca apaga o MAIS RECENTE de cada pasta,
    mesmo que ultrapasse o prazo — um backup velho ainda eh melhor que zero."""
    from app.services import dropbox_storage
    if not dropbox_storage.disponivel():
        return 'dropbox nao configurado'
    limite = _limite(dias).replace(tzinfo=None)
    apagados = []
    for pasta in _PASTAS_BACKUP:
        arquivos = dropbox_storage.listar_pasta(pasta)
        if not arquivos:
            continue
        datados = [(a, _data_do_backup(a)) for a in arquivos]
        datados = [(a, d) for a, d in datados if d is not None]
        if not datados:
            continue
        # Protege o mais recente da pasta
        datados.sort(key=lambda x: x[1], reverse=True)
        for a, d in datados[1:]:
            if d < limite:
                if dry_run or dropbox_storage.deletar(a['path']):
                    apagados.append(a['nome'])
    return apagados if dry_run else len(apagados)
