"""Média de três dias equivalentes de vendas simples; não movimenta estoque.

Decisão do owner em 23/09/2026: diariamente às 12h, para o dia seguinte,
Ribeiro do Vale e Anésio, no canal do desperdício. Não expandir composição
de recheados/sanduíches nem usar pedidos, estoque ou margem de segurança.
"""
import logging
import unicodedata
from datetime import datetime, time, timedelta
from decimal import ROUND_CEILING, Decimal

from flask import current_app
from sqlalchemy import or_, text

from app.extensions import db
from app.models import (
    FermentacaoEnvio,
    Loja,
    SeruLojaMap,
    VendaSeruDiaBreakdown,
    VendaSeruDiaLoja,
    VendaSeruDiaria,
)
from app.utils import agora, hoje

logger = logging.getLogger(__name__)
LOCK_KEY = 7767
LOJAS = ('Ribeiro do Vale', 'Anésio Pinto Rosa')
DIAS_SEMANA = ('segunda-feira', 'terça-feira', 'quarta-feira', 'quinta-feira',
               'sexta-feira', 'sábado', 'domingo')
# Lista positiva conferida no PDV. SKU sozinho não identifica venda simples.
PRODUTOS = {
    'croissant frances': ('croissant', '272'),
    'croissant tradicional': ('croissant', '272'),
    'croissant esquentado': ('croissant', '355'),
    'pain au chocolat': ('pain', '271'),
}


def _normalizar(nome):
    return ' '.join(''.join(c for c in unicodedata.normalize('NFKD', nome or '')
                           if not unicodedata.combining(c)).lower().split())


def datas_base(data_alvo):
    return [data_alvo - timedelta(weeks=n) for n in (3, 2, 1)]


def calcular(data_alvo=None):
    data_alvo = data_alvo or hoje() + timedelta(days=1)
    datas = datas_base(data_alvo)
    resultado = {'data_alvo': data_alvo.isoformat(),
                 'datas': [d.isoformat() for d in datas], 'lojas': [], 'erros': []}
    lojas = Loja.query.filter_by(ativa=True).all()
    for nome in LOJAS:
        candidatos = [l for l in lojas
                      if _normalizar(l.nome).removeprefix('loja ') == _normalizar(nome)]
        if len(candidatos) != 1:
            resultado['erros'].append(f'{nome}: cadastro da loja não identificado.')
            continue
        loja = candidatos[0]
        mapas = SeruLojaMap.query.filter(
            SeruLojaMap.loja_id == loja.id, SeruLojaMap.ignorar.is_(False),
            SeruLojaMap.confirmado_em.isnot(None)).all()
        nomes = {m.seru_company_name for m in mapas}
        if not nomes:
            resultado['erros'].append(f'{nome}: vínculo com o PDV não confirmado.')
            continue
        observacoes = []
        for dia in datas:
            fechado_em = datetime.combine(dia + timedelta(days=1), time.min)
            totais = VendaSeruDiaLoja.query.filter(
                VendaSeruDiaLoja.data == dia,
                or_(VendaSeruDiaLoja.loja_id == loja.id,
                    VendaSeruDiaLoja.loja_seru.in_(nomes))).all()
            linhas = VendaSeruDiaria.query.filter(
                VendaSeruDiaria.data == dia,
                or_(VendaSeruDiaria.loja_id == loja.id,
                    VendaSeruDiaria.loja_seru.in_(nomes))).all()
            prefixo = f'{nome}, {dia:%d/%m}'
            # Evidência de cada loja/dia; ausência de histórico nunca vira zero.
            if not totais or not linhas or any(
                    r.loja_id != loja.id or not r.atualizado_em
                    or r.atualizado_em < fechado_em for r in [*totais, *linhas]):
                resultado['erros'].append(f'{prefixo}: histórico fechado indisponível.')
                continue
            nomes_historicos = {r.loja_seru for r in totais}
            if nomes_historicos != {r.loja_seru for r in linhas}:
                resultado['erros'].append(f'{prefixo}: totais e itens do histórico divergentes.')
                continue
            sem_itens = VendaSeruDiaBreakdown.query.filter(
                VendaSeruDiaBreakdown.data == dia,
                VendaSeruDiaBreakdown.loja_seru.in_(nomes_historicos),
                VendaSeruDiaBreakdown.dimensao == 'sem_itens',
                VendaSeruDiaBreakdown.valor > 0).first()
            if sem_itens:
                resultado['erros'].append(f'{prefixo}: há vendas sem produtos detalhados.')
            quantidades = {'croissant': Decimal(0), 'pain': Decimal(0)}
            fontes = []
            for linha in linhas:
                produto = PRODUTOS.get(_normalizar(linha.seru_nome))
                if not produto:
                    continue
                grupo, sku = produto
                qtd = Decimal(str(linha.qtd))
                if (str(linha.sku or '').strip() != sku or not qtd.is_finite()
                        or qtd < 0 or qtd != qtd.to_integral_value()):
                    resultado['erros'].append(
                        f'{prefixo}: conferir cadastro/quantidade de {linha.seru_nome}.')
                    continue
                quantidades[grupo] += qtd
                fontes.append({'nome': linha.seru_nome, 'sku': linha.sku,
                               'qtd': str(qtd), 'capturado_em': linha.atualizado_em.isoformat()})
            observacoes.append({'data': dia.isoformat(),
                                **{k: str(v) for k, v in quantidades.items()},
                                'fontes': fontes})
        if len(observacoes) != 3:
            continue
        item = {'nome': nome, 'loja_id': loja.id, 'dias': observacoes}
        for grupo in ('croissant', 'pain'):
            media = sum(Decimal(d[grupo]) for d in observacoes) / Decimal(3)
            item[grupo] = int(media.to_integral_value(rounding=ROUND_CEILING))
            item[f'media_{grupo}'] = str(media)
        resultado['lojas'].append(item)
    resultado['ok'] = not resultado['erros'] and len(resultado['lojas']) == 2
    resultado['texto'] = formatar(resultado, data_alvo)
    return resultado


def formatar(resultado, data_alvo):
    titulo = (f'Tirar para fermentar amanhã {data_alvo:%d/%m} '
              f'({DIAS_SEMANA[data_alvo.weekday()]}):')
    if not resultado['ok']:
        return (titulo + '\n\nNão foi possível calcular a lista com segurança.\n'
                + '\n'.join(resultado['erros'])
                + '\n\nConferir o histórico de vendas no sistema antes de separar.')
    linhas = [titulo]
    for loja in resultado['lojas']:
        linhas.extend(['', f'{loja["nome"]}:',
                       f'{loja["croissant"]} croissants tradicionais',
                       f'{loja["pain"]} pain au chocolat'])
    linhas.extend(['', 'Base: média das vendas simples registradas no PDV em '
                   + ', '.join(d[8:10] + '/' + d[5:7] for d in resultado['datas'])
                   + '; arredondada para cima.'])
    return '\n'.join(linhas)


def enviar_amanha():
    """Cron e botão do owner usam a mesma trava e o mesmo registro por data.

    Reserva persistida ANTES da rede. Resposta incerta não é reenviada
    automaticamente: evita duas instruções de preparo após timeout/restart.
    """
    from app.services import instancia, slack

    canal = (current_app.config.get('SLACK_CANAL_COPILOT') or '').strip()
    if not canal or not (current_app.config.get('SLACK_BOT_TOKEN') or '').strip():
        return {'estado': 'indisponivel', 'mensagem': 'Canal ou bot do Slack não configurado.'}
    if not instancia.pode_falar_com_o_mundo('slack-fermentacao'):
        return {'estado': 'indisponivel', 'mensagem': 'Envio desativado nesta instância.'}
    conn = db.engine.connect()
    pg = db.engine.dialect.name == 'postgresql'
    obtido = False
    try:
        if pg:
            obtido = bool(conn.execute(text('SELECT pg_try_advisory_lock(:k)'),
                                      {'k': LOCK_KEY}).scalar())
            if not obtido:
                return {'estado': 'ocupado', 'mensagem': 'O envio já está em andamento.'}
        alvo = hoje() + timedelta(days=1)
        anterior = db.session.get(FermentacaoEnvio, alvo)
        if anterior:
            return {'estado': anterior.estado,
                    'mensagem': 'Esta data já tem uma tentativa registrada. Confira o estado abaixo.'}
        calculo = calcular(alvo)
        envio = FermentacaoEnvio(data_alvo=alvo, canal=canal, estado='enviando',
                                calculo=calculo, texto=calculo['texto'])
        db.session.add(envio)
        db.session.commit()
        resposta = slack.post_message(canal, envio.texto, retry=False)
        if resposta.get('ok'):
            envio.estado = 'enviado' if calculo['ok'] else 'aviso_enviado'
            envio.slack_ts = resposta.get('ts')
            envio.enviado_em = agora()
        else:
            envio.estado = 'incerto'
            logger.error('fermentacao: Slack não confirmou o envio de %s', alvo)
        db.session.commit()
        return {'estado': envio.estado, 'mensagem': {
            'enviado': 'Lista de amanhã enviada ao Slack.',
            'aviso_enviado': 'Aviso de histórico incompleto enviado ao Slack.',
            'incerto': 'O Slack não confirmou. Confira o canal antes de tentar novamente.',
        }[envio.estado]}
    finally:
        try:
            if pg and obtido:
                conn.execute(text('SELECT pg_advisory_unlock(:k)'), {'k': LOCK_KEY})
        finally:
            conn.close()
