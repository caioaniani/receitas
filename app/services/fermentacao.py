"""Previsão de consumo dos dois folhados em dias equivalentes de vendas.

Decisão do owner em 23/09/2026: diariamente às 12h, para o dia seguinte,
Ribeiro do Vale e Anésio. Desde 26/09, cada loja tem seu canal. Inclui o consumo nas
composições dos lanches, inclusive na chapa; não movimenta estoque.
Em 25/09, Ribeiro passa a usar (maior + quarto maior) / 2 em sete semanas,
separadamente para croissant tradicional e pain au chocolat.
"""
import logging
import re
import unicodedata
from datetime import datetime, time, timedelta
from decimal import ROUND_CEILING, Decimal

from flask import current_app
from sqlalchemy import or_, text

from app.extensions import db
from app.models import (
    FermentacaoEnvio,
    FermentacaoEnvioLoja,
    Loja,
    SeruLojaMap,
    VendaSeruDiaBreakdown,
    VendaSeruDiaLoja,
    VendaSeruDiaria,
)
from app.services.fermentacao_calendario import feriado, selecionar_datas
from app.utils import agora, hoje

logger = logging.getLogger(__name__)
LOCK_KEY = 7767
LOJAS = ('Ribeiro do Vale', 'Anésio Pinto Rosa')
CONFIG_CANAIS = dict(zip(LOJAS, (
    'SLACK_CANAL_FERMENTACAO_RIBEIRO', 'SLACK_CANAL_FERMENTACAO_ANESIO')))
DIAS_SEMANA = ('segunda-feira', 'terça-feira', 'quarta-feira', 'quinta-feira',
               'sexta-feira', 'sábado', 'domingo')


def _normalizar(nome):
    return ' '.join(''.join(c for c in unicodedata.normalize('NFKD', nome or '')
                           if not unicodedata.combining(c)).lower().split())


def datas_base(data_alvo, semanas=3):
    return selecionar_datas(data_alvo, semanas)[0]


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


def destinos():
    return {nome: (current_app.config.get(chave) or '').strip()
            for nome, chave in CONFIG_CANAIS.items()}


def calcular(data_alvo=None, nome_loja=None):
    from app.services.fermentacao_consumo import ConsumoFermentacao

    data_alvo = data_alvo or hoje() + timedelta(days=1)
    if nome_loja is not None and nome_loja not in LOJAS:
        raise ValueError('Loja fora da lista de fermentação.')
    lojas_alvo = (nome_loja,) if nome_loja else LOJAS
    resultado = {'data_alvo': data_alvo.isoformat(),
                 'datas': [], 'lojas': [], 'erros': [],
                 'feriado_alvo': feriado(data_alvo), 'exclusoes_por_loja': []}
    try:
        consumo = ConsumoFermentacao()
    except ValueError as exc:
        resultado.update(ok=False, erros=[str(exc)])
        resultado['texto'] = formatar(resultado, data_alvo)
        return resultado
    lojas = Loja.query.filter_by(ativa=True).all()
    for nome in lojas_alvo:
        metodo = 'maior_quarto_7' if nome == 'Ribeiro do Vale' else 'media_3'
        semanas = 7 if metodo == 'maior_quarto_7' else 3
        datas, excluidas = selecionar_datas(data_alvo, semanas)
        resultado['exclusoes_por_loja'].append({'nome': nome, 'datas': excluidas})
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
                'metodo': metodo, 'semanas': semanas, 'calculos': {},
                'datas_excluidas': excluidas}
        for grupo in ('croissant', 'pain'):
            calculo = resumir_consumo([d[grupo] for d in observacoes], metodo)
            item['calculos'][grupo] = calculo
            item[grupo] = calculo['quantidade']
            if metodo == 'media_3':
                item[f'media_{grupo}'] = calculo['referencia']
        resultado['lojas'].append(item)
    resultado['ok'] = not resultado['erros'] and len(resultado['lojas']) == len(lojas_alvo)
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
    if resultado.get('feriado_alvo'):
        linhas.extend(['', f'Atenção: amanhã é feriado ({resultado["feriado_alvo"]}). '
                       'Referência semanal sem ajuste específico para feriado; conferir a quantidade.'])
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
        if loja.get('datas_excluidas'):
            linhas.append('Feriados fora da conta: ' + '; '.join(
                f'{d["data"][8:10]}/{d["data"][5:7]} ({d["motivo"]})'
                for d in loja['datas_excluidas']) + '.')
    linhas.extend(['',
                   'Inclui lanches, preparações na chapa, Nutella e Nutella com morango. Almond não entra.'])
    return '\n'.join(linhas)


def enviar_amanha(corrigir=False, nome_loja=None, recuperar=False):
    """Cron e botão do owner usam a mesma trava e registros por loja/data.

    Reserva persistida ANTES da rede. Resposta incerta não é reenviada
    automaticamente: evita duas instruções de preparo após timeout/restart.
    """
    from app.services import instancia

    if corrigir and recuperar:
        return {'estado': 'indisponivel', 'mensagem': 'Escolha uma ação por vez.'}
    if nome_loja is not None and nome_loja not in LOJAS:
        return {'estado': 'indisponivel', 'mensagem': 'Loja fora da lista de fermentação.'}
    if not (current_app.config.get('SLACK_BOT_TOKEN') or '').strip():
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
            if recuperar:
                return _recuperar(anterior)
            if corrigir:
                return _corrigir(anterior)
            return {'estado': anterior.estado,
                    'mensagem': 'Esta data já tem uma tentativa registrada. Confira o estado abaixo.'}
        resultados = []
        for nome, canal in destinos().items():
            if nome_loja and nome != nome_loja:
                continue
            envio = db.session.get(FermentacaoEnvioLoja, (alvo, nome))
            if envio:
                if recuperar:
                    resultados.append(_recuperar(envio))
                elif corrigir:
                    resultados.append(_corrigir(envio))
                else:
                    resultados.append({'estado': envio.estado,
                                       'mensagem': f'{nome}: tentativa já registrada.'})
                continue
            if corrigir or recuperar:
                resultados.append({'estado': 'indisponivel',
                                   'mensagem': f'{nome}: não há tentativa registrada para esta ação.'})
                continue
            if not canal:
                resultados.append({'estado': 'indisponivel',
                                   'mensagem': f'{nome}: canal não configurado.'})
                continue
            calculo = calcular(alvo, nome_loja=nome)
            envio = FermentacaoEnvioLoja(
                data_alvo=alvo, loja=nome, canal=canal, estado='enviando',
                calculo=calculo, texto=calculo['texto'])
            db.session.add(envio)
            resultados.append(_publicar(envio))
        estados = {r['estado'] for r in resultados}
        estado = estados.pop() if len(estados) == 1 else 'parcial'
        return {'estado': estado,
                'mensagem': ' '.join(r['mensagem'] for r in resultados)}
    finally:
        try:
            if pg and obtido:
                conn.execute(text('SELECT pg_advisory_unlock(:k)'), {'k': LOCK_KEY})
        finally:
            conn.close()


# Somente códigos curtos entram na auditoria/tela, nunca respostas brutas ou tokens.
ERROS_ENTREGA = {
    'not_in_channel': 'O aplicativo da padaria não está neste canal. Adicione o mesmo aplicativo que envia no Copilot.',
    'channel_not_found': 'O Slack não encontrou o canal ou o aplicativo não tem acesso. Confira o ID e adicione o aplicativo ao canal.',
    'missing_scope': 'Falta uma permissão do aplicativo no Slack. Confira a configuração da integração.',
    'no_permission': 'O aplicativo não tem permissão para publicar neste canal.',
    'is_archived': 'O canal está arquivado.',
    'invalid_auth': 'O Slack recusou a autenticação do aplicativo.',
    'not_authed': 'O Slack não recebeu uma autenticação válida.',
    'token_revoked': 'O acesso do aplicativo foi revogado no Slack.',
    'token_expired': 'O acesso do aplicativo expirou no Slack.',
    'account_inactive': 'O aplicativo ou workspace está inativo.',
    'restricted_action': 'Uma regra do workspace impede publicar neste canal.',
    'rate_limited': 'O Slack limitou as consultas temporariamente.',
    'ratelimited': 'O Slack limitou as consultas temporariamente.',
}


def mensagem_erro_entrega(codigo):
    return ERROS_ENTREGA.get(codigo, 'Não foi possível confirmar a operação com o Slack.')


def _codigo_seguro(codigo):
    return codigo if isinstance(codigo, str) and re.fullmatch(r'[a-z0-9_]{1,80}', codigo) else 'resposta_indisponivel'


def _gravar_entrega(envio, **dados):
    calculo = dict(envio.calculo)
    entrega = dict(calculo.get('_entrega') or {})
    entrega.update(dados)
    calculo['_entrega'] = entrega
    envio.calculo = calculo
    return entrega


def _resultado_confirmado(envio):
    return {'estado': envio.estado,
            'mensagem': ('Lista confirmada no Slack.' if envio.estado == 'enviado'
                         else 'Aviso de histórico incompleto confirmado no Slack.')}


def _publicar(envio):
    """Reserva cada tentativa antes da rede, inclusive a recuperação autorizada."""
    from app.services import slack

    anterior = dict((envio.calculo or {}).get('_entrega') or {})
    historico = list(anterior.pop('historico', []))
    if anterior:
        historico.append(dict(anterior, estado=envio.estado))
    _gravar_entrega(envio, historico=historico,
                   ultima_tentativa_em=agora().isoformat(), codigo_erro=None,
                   rejeitado=False, verificacao=None, url=None)
    envio.estado = 'enviando'
    db.session.commit()
    try:
        resposta = slack.post_message(envio.canal, envio.texto, retry=False)
    except Exception:  # noqa: BLE001 — resposta ambígua nunca autoriza repetir POST.
        logger.exception('fermentacao: falha no envio para %s', getattr(envio, 'loja', 'lojas'))
        resposta = {'ok': False}
    if resposta.get('ok') and resposta.get('ts'):
        envio.estado = 'enviado' if envio.calculo['ok'] else 'aviso_enviado'
        envio.slack_ts = resposta['ts']
        envio.canal = resposta.get('channel') or envio.canal
        envio.enviado_em = agora()
        db.session.commit()
        return _resultado_confirmado(envio)
    codigo = _codigo_seguro(resposta.get('codigo_erro'))
    rejeitado = resposta.get('rejeitado') is True
    envio.estado = 'falhou' if rejeitado else 'incerto'
    _gravar_entrega(envio, codigo_erro=codigo, rejeitado=rejeitado)
    db.session.commit()
    logger.error('fermentacao: entrega %s para %s (%s)', envio.estado,
                 getattr(envio, 'loja', 'lojas'), codigo)
    return {'estado': envio.estado, 'mensagem': mensagem_erro_entrega(codigo)}


def _publicar_recuperacao(envio):
    """Após comprovar ausência/recusa, usa o cálculo atual e guarda o anterior."""
    calculo = calcular(envio.data_alvo, nome_loja=getattr(envio, 'loja', None))
    if not calculo['ok']:
        return {'estado': 'indisponivel',
                'mensagem': 'O cálculo atual está incompleto. Confira a prévia; nenhuma lista antiga foi reenviada.'}
    anterior = dict(envio.calculo)
    negocio_anterior = {k: v for k, v in anterior.items()
                        if k not in ('_entrega', 'historico_recalculos',
                                     'historico_correcoes', 'correcao_pendente')}
    if calculo != negocio_anterior or calculo['texto'] != envio.texto:
        historico = list(anterior.get('historico_recalculos', []))
        historico.append({'texto': envio.texto,
                          'calculo': {k: v for k, v in anterior.items()
                                      if k not in ('historico_recalculos', '_entrega')},
                          'recalculado_em': agora().isoformat()})
        envio.calculo = dict(calculo, historico_recalculos=historico,
                             historico_correcoes=anterior.get('historico_correcoes', []),
                             _entrega=anterior.get('_entrega') or {})
        envio.texto = calculo['texto']
    return _publicar(envio)


def _recuperar(envio):
    """Só repete POST rejeitado explicitamente ou após consulta completa do Slack."""
    from app.services import slack_entrega

    if envio.slack_ts or envio.estado in ('enviado', 'aviso_enviado', 'correcao_incerta'):
        return {'estado': envio.estado, 'mensagem': 'Já existe uma mensagem confirmada. Use a opção de abrir ou corrigir.'}
    entrega = dict(envio.calculo.get('_entrega') or {})
    if envio.estado == 'falhou' and entrega.get('rejeitado') is True:
        return _publicar_recuperacao(envio)
    desde = envio.criado_em
    if entrega.get('ultima_tentativa_em'):
        try:
            desde = datetime.fromisoformat(entrega['ultima_tentativa_em'])
        except (TypeError, ValueError):
            return {'estado': 'indisponivel', 'mensagem': 'Horário da tentativa inválido. É necessária conferência.'}
    if not desde or desde.tzinfo is not None:
        return {'estado': 'indisponivel', 'mensagem': 'Horário da tentativa indisponível. É necessária conferência.'}
    if agora() - desde < timedelta(minutes=5):
        return {'estado': envio.estado, 'mensagem': 'A tentativa é recente. Aguarde cinco minutos antes de verificar.'}
    consulta = slack_entrega.verificar_mensagem(envio.canal, envio.texto, desde=desde)
    estado = consulta.get('estado')
    _gravar_entrega(envio, verificacao=estado, verificado_em=agora().isoformat(),
                   codigo_erro=_codigo_seguro(consulta.get('codigo_erro')) if consulta.get('codigo_erro') else None)
    if estado == 'encontrada' and consulta.get('ts') and consulta.get('canal') == envio.canal:
        envio.slack_ts = consulta['ts']
        envio.estado = 'enviado' if envio.calculo['ok'] else 'aviso_enviado'
        # Momento de confirmação; a publicação original está identificada pelo ts.
        envio.enviado_em = agora()
        _gravar_entrega(envio, url=consulta.get('url'))
        db.session.commit()
        return {'estado': envio.estado, 'mensagem': 'Mensagem localizada no Slack, sem reenviar.'}
    db.session.commit()
    if estado == 'ausente':
        return _publicar_recuperacao(envio)
    if estado == 'ambiguo':
        return {'estado': 'indisponivel', 'mensagem': 'Há mais de uma mensagem correspondente. Confira o canal; nenhum reenvio foi feito.'}
    return {'estado': 'indisponivel', 'mensagem': mensagem_erro_entrega(consulta.get('codigo_erro')) + ' Nenhum reenvio foi feito.'}


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
        calculo = calcular(envio.data_alvo, nome_loja=getattr(envio, 'loja', None))
        texto = calculo['texto']
        if texto == envio.texto:
            return {'estado': envio.estado, 'mensagem': 'A mensagem já está atualizada.'}
        pendente = {'calculo': calculo, 'texto': texto,
                    'solicitado_em': agora().isoformat()}
        salvo['correcao_pendente'] = pendente
        envio.calculo = salvo
        envio.estado = 'correcao_incerta'
        db.session.commit()
    try:
        resposta = slack.update_message(envio.canal, envio.slack_ts,
                                        text=pendente['texto'])
    except Exception:  # noqa: BLE001 — mantém a correção persistida e permite outra loja.
        logger.exception('fermentacao: falha ao corrigir %s', envio.data_alvo)
        resposta = {'ok': False}
    if not resposta.get('ok'):
        return {'estado': 'correcao_incerta',
                'mensagem': 'O Slack não confirmou a correção. A tentativa foi guardada; confira o canal.'}
    historico = list(salvo.get('historico_correcoes', []))
    historico.append({'texto': envio.texto,
                      'calculo': {k: v for k, v in salvo.items()
                                  if k not in ('historico_correcoes', 'correcao_pendente')},
                      'corrigido_em': agora().isoformat()})
    envio.calculo = dict(pendente['calculo'], historico_correcoes=historico,
                         _entrega=salvo.get('_entrega') or {})
    envio.texto = pendente['texto']
    envio.estado = 'enviado' if pendente['calculo']['ok'] else 'aviso_enviado'
    envio.enviado_em = agora()
    db.session.commit()
    return {'estado': envio.estado,
            'mensagem': 'Mensagem corrigida no Slack, sem duplicar a lista.'}
