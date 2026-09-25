"""Previsão de consumo dos dois folhados em dias equivalentes de vendas.

Decisão do owner em 23/09/2026: diariamente às 12h, para o dia seguinte,
Ribeiro do Vale e Anésio, no canal do desperdício. Inclui o consumo nas
composições dos lanches, inclusive na chapa; não movimenta estoque.
Em 25/09, Ribeiro passa a usar (maior + quarto maior) / 2 em sete semanas,
separadamente para croissant tradicional e pain au chocolat.
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


def _normalizar(nome):
    return ' '.join(''.join(c for c in unicodedata.normalize('NFKD', nome or '')
                           if not unicodedata.combining(c)).lower().split())


def datas_base(data_alvo, semanas=3):
    return [data_alvo - timedelta(weeks=n) for n in range(semanas, 0, -1)]


def resumir_consumo(valores, metodo):
    """Ordena cada produto separadamente; só arredonda a quantidade final."""
    valores = [Decimal(v) for v in valores]
    semanas = 7 if metodo == 'maior_quarto_7' else 3
    if metodo not in ('maior_quarto_7', 'media_3') or len(valores) != semanas:
        raise ValueError('Método ou quantidade de observações inválidos.')
    if any(not v.is_finite() or v < 0 for v in valores):
        raise ValueError('Consumo inválido.')
    ordenadas = sorted(valores, reverse=True)
    calculo = {'ordenadas': [str(v) for v in ordenadas]}
    if metodo == 'maior_quarto_7':
        referencia = (ordenadas[0] + ordenadas[3]) / Decimal(2)
        calculo.update(maior=str(ordenadas[0]), quarto_maior=str(ordenadas[3]))
    else:
        referencia = sum(ordenadas) / Decimal(3)
    calculo.update(referencia=str(referencia),
                   quantidade=int(referencia.to_integral_value(rounding=ROUND_CEILING)))
    return calculo


def calcular(data_alvo=None):
    from app.services.fermentacao_consumo import ConsumoFermentacao

    data_alvo = data_alvo or hoje() + timedelta(days=1)
    resultado = {'data_alvo': data_alvo.isoformat(),
                 'datas': [], 'lojas': [], 'erros': []}
    try:
        consumo = ConsumoFermentacao()
    except ValueError as exc:
        resultado.update(ok=False, erros=[str(exc)])
        resultado['texto'] = formatar(resultado, data_alvo)
        return resultado
    lojas = Loja.query.filter_by(ativa=True).all()
    for nome in LOJAS:
        metodo = 'maior_quarto_7' if nome == 'Ribeiro do Vale' else 'media_3'
        semanas = 7 if metodo == 'maior_quarto_7' else 3
        datas = datas_base(data_alvo, semanas)
        resultado['datas'] = sorted(set(resultado['datas']) | {d.isoformat() for d in datas})
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
                try:
                    contribuicao = consumo.resolver(linha)
                except ValueError as exc:
                    resultado['erros'].append(f'{prefixo}: {exc}')
                    continue
                for grupo in quantidades:
                    quantidades[grupo] += contribuicao[grupo]
                if contribuicao.get('fonte', {}).get('componentes'):
                    fontes.append(contribuicao['fonte'])
            observacoes.append({'data': dia.isoformat(),
                                **{k: str(v) for k, v in quantidades.items()},
                                'fontes': fontes})
        if len(observacoes) != semanas:
            continue
        item = {'nome': nome, 'loja_id': loja.id, 'dias': observacoes,
                'metodo': metodo, 'semanas': semanas, 'calculos': {}}
        for grupo in ('croissant', 'pain'):
            calculo = resumir_consumo([d[grupo] for d in observacoes], metodo)
            item['calculos'][grupo] = calculo
            item[grupo] = calculo['quantidade']
            if metodo == 'media_3':
                item[f'media_{grupo}'] = calculo['referencia']
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
        base = ('(maior consumo + 4º maior consumo) ÷ 2, nos últimos 7 dias equivalentes'
                if loja['metodo'] == 'maior_quarto_7'
                else 'média do consumo nos últimos 3 dias equivalentes')
        linhas.extend(['', f'{loja["nome"]}:',
                       f'{loja["croissant"]} croissants tradicionais',
                       f'{loja["pain"]} pain au chocolat',
                       f'Base: {base}; arredondado para cima.',
                       'Datas: ' + ', '.join(d['data'][8:10] + '/' + d['data'][5:7]
                                             for d in loja['dias']) + '.'])
    linhas.extend(['',
                   'Inclui lanches, preparações na chapa, Nutella e Nutella com morango. Almond não entra.'])
    return '\n'.join(linhas)


def enviar_amanha(corrigir=False):
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
            if corrigir:
                return _corrigir(anterior)
            return {'estado': anterior.estado,
                    'mensagem': 'Esta data já tem uma tentativa registrada. Confira o estado abaixo.'}
        if corrigir:
            return {'estado': 'indisponivel', 'mensagem': 'Não há mensagem para corrigir.'}
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


def _corrigir(envio):
    """Substitui o mesmo ts sob a trava do envio, preservando a auditoria.

    Se a resposta se perder, o retry explícito repete o mesmo chat.update,
    nunca publica uma segunda instrução. A tentativa fica salva antes da rede.
    """
    from app.services import slack

    if not envio.slack_ts:
        return {'estado': 'indisponivel',
                'mensagem': 'Sem confirmação da mensagem original. Confira o canal.'}
    salvo = dict(envio.calculo)
    pendente = salvo.get('correcao_pendente')
    if not pendente:
        calculo = calcular(envio.data_alvo)
        texto = calculo['texto']
        if texto == envio.texto:
            return {'estado': envio.estado, 'mensagem': 'A mensagem já está atualizada.'}
        pendente = {'calculo': calculo, 'texto': texto,
                    'solicitado_em': agora().isoformat()}
        salvo['correcao_pendente'] = pendente
        envio.calculo = salvo
        envio.estado = 'correcao_incerta'
        db.session.commit()
    resposta = slack.update_message(envio.canal, envio.slack_ts,
                                    text=pendente['texto'])
    if not resposta.get('ok'):
        return {'estado': 'correcao_incerta',
                'mensagem': 'O Slack não confirmou a correção. A tentativa foi guardada; confira o canal.'}
    historico = list(salvo.get('historico_correcoes', []))
    historico.append({'texto': envio.texto,
                      'calculo': {k: v for k, v in salvo.items()
                                  if k not in ('historico_correcoes', 'correcao_pendente')},
                      'corrigido_em': agora().isoformat()})
    envio.calculo = dict(pendente['calculo'], historico_correcoes=historico)
    envio.texto = pendente['texto']
    envio.estado = 'enviado' if pendente['calculo']['ok'] else 'aviso_enviado'
    envio.enviado_em = agora()
    db.session.commit()
    return {'estado': envio.estado,
            'mensagem': 'Mensagem corrigida no Slack, sem duplicar a lista.'}
