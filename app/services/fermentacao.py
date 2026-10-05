"""Previsão de consumo dos dois folhados em dias equivalentes de vendas.

Decisão do owner em 23/09/2026: diariamente às 12h, para o dia seguinte,
Ribeiro do Vale e Anésio. Desde 26/09, cada loja tem seu canal. Inclui o consumo nas
composições dos lanches, inclusive na chapa; não movimenta estoque.
Em 25/09, Ribeiro passa a usar (maior + quarto maior) / 2 em sete semanas,
separadamente para croissant tradicional e pain au chocolat.
"""
import hashlib
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
# Resposta de `enviar_amanha` quando outro envio segura a trava; a conferência
# usa o mesmo texto para orientar a trava presa.
MSG_OCUPADO = 'O envio já está em andamento.'


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
                return {'estado': 'ocupado', 'mensagem': MSG_OCUPADO}
        alvo = hoje() + timedelta(days=1)
        anterior = db.session.get(FermentacaoEnvio, alvo)
        if anterior:
            if recuperar:
                return _recuperar(anterior)
            if corrigir:
                return _corrigir(anterior)
            return {'estado': anterior.estado,
                    'mensagem': 'Esta data já tem uma tentativa registrada. Confira o estado abaixo.'}
        # Uma entrada por loja; a conferência (verificar_envio_de_amanha) usa
        # a mensagem de quem ficou sem registro para explicar o motivo.
        lojas = []
        for nome, canal in destinos().items():
            if nome_loja and nome != nome_loja:
                continue
            envio = db.session.get(FermentacaoEnvioLoja, (alvo, nome))
            if envio:
                if recuperar:
                    item = _recuperar(envio)
                elif corrigir:
                    item = _corrigir(envio)
                else:
                    item = {'estado': envio.estado,
                            'mensagem': 'tentativa já registrada.'}
                lojas.append(dict(item, loja=nome))
                continue
            if corrigir or recuperar:
                lojas.append({'loja': nome, 'estado': 'indisponivel',
                              'mensagem': 'não há tentativa registrada para esta ação.'})
                continue
            if not canal:
                lojas.append({'loja': nome, 'estado': 'indisponivel',
                              'mensagem': 'canal não configurado.'})
                continue
            try:
                calculo = calcular(alvo, nome_loja=nome)
            except Exception:  # noqa: BLE001 — erro inesperado numa loja não impede a outra
                logger.exception('fermentacao: cálculo de %s falhou', nome)
                db.session.rollback()
                # Sem registro: a conferência das 12:10/16:00 tenta de novo.
                lojas.append({'loja': nome, 'estado': 'indisponivel',
                              'mensagem': 'erro interno no cálculo; nada foi enviado.'})
                continue
            envio = FermentacaoEnvioLoja(
                data_alvo=alvo, loja=nome, canal=canal, estado='enviando',
                calculo=calculo, texto=calculo['texto'])
            db.session.add(envio)
            lojas.append(dict(_publicar(envio), loja=nome))
        estados = {item['estado'] for item in lojas}
        estado = estados.pop() if len(estados) == 1 else 'parcial'
        return {'estado': estado,
                'mensagem': ' '.join(f'{item["loja"]}: {item["mensagem"]}'
                                     for item in lojas),
                'data_alvo': alvo.isoformat(),
                'lojas': lojas}
    finally:
        _soltar_trava(conn, pg and obtido)


def _soltar_trava(conn, obtido):
    """Solta a trava do envio e fecha a conexão.

    Unlock que falha DESCARTA a conexão (`invalidate`): devolvida ao pool com
    a trava presa, todo envio seguinte responderia "em andamento" até
    reiniciar o serviço. Sem a conexão, o Postgres solta a trava.
    """
    try:
        if obtido:
            conn.execute(text('SELECT pg_advisory_unlock(:k)'), {'k': LOCK_KEY})
    except Exception:  # noqa: BLE001 — o envio já terminou; só a conexão é descartada
        logger.exception('fermentacao: falha ao soltar a trava do envio; conexão descartada')
        conn.invalidate()
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
    calculo = calcular_seguro(envio.data_alvo, nome_loja=getattr(envio, 'loja', None))
    if calculo.get('erro_interno'):
        return {'estado': 'indisponivel',
                'mensagem': ('Erro interno no cálculo atual (detalhe no log do sistema). '
                             'Nenhuma lista foi reenviada.')}
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
        calculo = calcular_seguro(envio.data_alvo, nome_loja=getattr(envio, 'loja', None))
        if calculo.get('erro_interno'):
            # Nunca publicar o texto do erro por cima de uma lista válida.
            return {'estado': envio.estado,
                    'mensagem': ('Erro interno no cálculo: a mensagem do Slack não foi '
                                 'alterada (detalhe no log do sistema).')}
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


# ── Conferência do envio e aviso ao dono ─────────────────────────────────
# Caso real (03/10/2026): o aplicativo nunca foi adicionado a
# #fermentadora-filial. O Slack recusou a lista da Anésio todo dia desde 27/09
# (not_in_channel) e a recusa ficou só no registro e na tela de admin: sete
# listas sem ninguém saber. No mesmo dia a lista de domingo da Ribeiro saiu
# bloqueada por um produto do PDV sem vínculo, que só o dono resolve.
# A conferência lê o estado GRAVADO (não o retorno em memória do envio): cobre
# também o job das 12h que não rodou (deploy), o processo que morreu no meio e
# o WhatsApp fora do ar na primeira rodada.
#
# Um aviso por PROBLEMA por dia: cada loja tem seu claim e o tick leva a
# assinatura do problema (estado, código do Slack, erros do cálculo). A rodada
# das 16:00 não repete o que a das 12:10 já disse, mas avisa problema novo.
_KEY_CLAIM_ALERTA = 'fermentacao_alerta_dono'      # falha da própria conferência
_KEY_CLAIM_LOJA = {nome: f'{_KEY_CLAIM_ALERTA}_' + '_'.join(_normalizar(nome).split())
                   for nome in LOJAS}
_MAX_ERROS_ALERTA = 5
_ESTADOS_NAO_CONFIRMADOS = ('incerto', 'enviando')
# Mesmo prazo que a tela exige antes de verificar uma tentativa (`_recuperar`).
_TENTATIVA_EM_CURSO = timedelta(minutes=5)
# Dica por tipo de erro do cálculo: (padrão, dica, corrigível no sistema). Os
# textos vêm de `calcular()` e de `fermentacao_consumo`. Ordem: do mais
# específico ao genérico — cada erro recebe só a PRIMEIRA dica que casa.
_DICAS_CALCULO = tuple((re.compile(padrao), dica, corrigivel)
                       for padrao, dica, corrigivel in (
    (r'vínculo do PDV ausente, pendente ou ignorado',
     'Vincule o produto em Mapeamentos do PDV (“Ignorar” não destrava a lista).', True),
    (r'mais de um vínculo do PDV',
     'Há mais de um vínculo com esse nome em Mapeamentos do PDV: deixe só o correto.', True),
    (r'menu configurável',
     'O PDV não informa a composição escolhida no menu: vincule o item do PDV a um '
     'produto de composição fixa ou a uma receita.', True),
    (r'fator do PDV',
     'Corrija o fator do vínculo em Mapeamentos do PDV.', True),
    (r'mini/bicolor vinculado|pão francês vinculado|vínculo com matéria-prima incompatível',
     'Corrija o vínculo em Mapeamentos do PDV.', True),
    (r'deve ser inteira',
     'Venda fracionada de item contado em unidades: confira o vínculo em Mapeamentos '
     'do PDV (produto vendido por peso não pode apontar para croissant ou pain); se '
     'o vínculo estiver certo, combine a quantidade com a equipe.', True),
    (r'rendimento',
     'Corrija o rendimento da receita no cadastro.', True),
    (r' / [^:]*: quantidade',
     'Corrija a quantidade do componente na ficha ou na composição citada.', True),
    (r'tipo de componente inválido|sem composição|sem vínculo|sem cadastro'
     r'|Ciclo na composição',
     'Corrija a composição (ficha da receita ou itens do produto) no cadastro.', True),
    (r'vínculo com o PDV não confirmado|cadastro da loja não identificado',
     'Confira o vínculo da loja em Mapeamentos do PDV.', True),
    (r'receita ativa não identificada',
     'Deixe uma única receita ativa de Croissant Tradicional e de Pain au Chocolat.', True),
    (r'histórico fechado indisponível|totais e itens do histórico divergentes',
     'As vendas desse dia não foram capturadas por completo: use “Aquecer histórico” '
     'em PDV → Itens vendidos (se a loja não abriu nesse dia, combine a quantidade '
     'com a equipe).', True),
    (r'há vendas sem produtos detalhados',
     'O PDV registrou venda sem itens nesse dia: não há o que vincular. Combine a '
     'quantidade com a equipe.', False),
    (r'erro interno no cálculo',
     'Erro do sistema (detalhe no log): para amanhã, combine a quantidade com a equipe.',
     False),
    (r'quantidade (não informada ou )?inválida',
     'Confira o número citado no cadastro (receita, composição ou vínculo do PDV).', True),
))


def _numero_dono():
    # Mesma ordem do digest de mensagens seguradas (`zapi.enviar_texto`):
    # ZAPI_NUMERO_DESTINO e, na falta, ZAPI_BOT_DONO_NUMERO. DÍVIDA conhecida:
    # os avisos ao dono não têm fonte única de destino — sobras e anomalias
    # usam só ZAPI_NUMERO_DESTINO; saldo Lalamove e saúde do negócio preferem
    # ZAPI_BOT_DONO_NUMERO; vigia do site e venda sem item usam o número do
    # vigia de infra. Unificar muda o destinatário de outros avisos: decisão do
    # dono, fora deste caso.
    cfg = current_app.config
    return ((cfg.get('ZAPI_NUMERO_DESTINO') or '').strip()
            or (cfg.get('ZAPI_BOT_DONO_NUMERO') or '').strip())


def _rollback_silencioso():
    try:
        db.session.rollback()
    except Exception:  # noqa: BLE001 — já está em caminho de erro; só registra
        logger.exception('fermentacao: rollback falhou')


def calcular_seguro(data_alvo=None, nome_loja=None):
    """`calcular` para a tela, a sonda, a recuperação e a correção.

    Erro inesperado vira cálculo bloqueado com `erro_interno` (e log): nunca
    página 500, e recuperação/correção nunca publicam o texto do erro.
    """
    try:
        return calcular(data_alvo, nome_loja=nome_loja)
    except Exception:  # noqa: BLE001 — a tela mostra o bloqueio; o log guarda o erro
        logger.exception('fermentacao: cálculo de %s falhou',
                         nome_loja or 'todas as lojas')
        _rollback_silencioso()
    data_alvo = data_alvo or hoje() + timedelta(days=1)
    try:
        feriado_alvo = feriado(data_alvo)
    except Exception:  # noqa: BLE001 — o erro pode ter vindo do próprio calendário
        feriado_alvo = None
    resultado = {'data_alvo': data_alvo.isoformat(), 'datas': [], 'lojas': [],
                 'erros': [f'{nome_loja or "Fermentação"}: erro interno no '
                           'cálculo; detalhe no log do sistema.'],
                 'feriado_alvo': feriado_alvo, 'exclusoes_por_loja': [],
                 'ok': False, 'erro_interno': True}
    try:
        resultado['texto'] = formatar(resultado, data_alvo)
    except Exception:  # noqa: BLE001 — o texto do erro nunca derruba a tela
        logger.exception('fermentacao: texto do cálculo com erro falhou')
        resultado['texto'] = ('Não foi possível calcular a lista com segurança: '
                              'erro interno no cálculo.')
    return resultado


def _link_admin():
    base = (current_app.config.get('APP_BASE_URL') or '').strip().rstrip('/')
    return f'{base}/admin/slack/fermentacao'


def _nome_do_canal(canal):
    from app.services import slack_entrega

    nome = slack_entrega.info_canal(canal).get('nome')
    return f'#{nome}' if nome else canal


def _linhas_calculo(calculo):
    """Erros do cálculo bloqueado + a dica de cada tipo de erro.

    Devolve (linhas, corrigivel); cálculo ok → ([], False). `corrigivel` é
    False quando algum erro é de um tipo que o sistema não resolve (venda sem
    itens, erro interno): corrigir o resto ainda não destrava a lista.
    """
    if calculo.get('ok') is not False:
        return [], False
    erros = [str(erro) for erro in calculo.get('erros') or []]
    linhas = [f'• {erro}' for erro in erros[:_MAX_ERROS_ALERTA]]
    if len(erros) > _MAX_ERROS_ALERTA:
        linhas.append(f'• e mais {len(erros) - _MAX_ERROS_ALERTA}')
    dicas, corrigivel = [], True
    for erro in erros:
        dica = next(((texto, pode) for padrao, texto, pode in _DICAS_CALCULO
                     if padrao.search(erro)), None)
        if dica is None:
            continue
        if dica[0] not in dicas:
            dicas.append(dica[0])
        corrigivel = corrigivel and dica[1]
    return linhas + [f'→ {dica}' for dica in dicas], corrigivel


def _bloco_loja(nome, envio, detalhe=None):
    """Uma loja no aviso: o que aconteceu e o gesto que resolve."""
    if envio is None:
        motivo = (detalhe or 'nenhuma tentativa registrada.').rstrip('.')
        if 'canal não configurado' in motivo:
            gesto = (f'Configure {CONFIG_CANAIS[nome]} no Railway e use '
                     '“Enviar mensagens pendentes de amanhã”.')
        elif 'bot do Slack não configurado' in motivo:
            gesto = ('Configure SLACK_BOT_TOKEN no Railway e use “Enviar mensagens '
                     'pendentes de amanhã”.')
        elif motivo == MSG_OCUPADO.rstrip('.'):
            gesto = ('Use “Enviar mensagens pendentes de amanhã”. Se a tela também '
                     'disser “em andamento”, a trava ficou presa: reinicie o serviço '
                     'no Railway e tente de novo.')
        elif 'erro interno no cálculo' in motivo:
            gesto = ('Tente “Enviar mensagens pendentes de amanhã”. Se o erro repetir, '
                     'é falha do sistema (detalhe no log): para amanhã, combine a '
                     'quantidade com a equipe.')
        else:
            gesto = ('Use “Enviar mensagens pendentes de amanhã”; se repetir, a tela '
                     'mostra o motivo.')
        return f'{nome} — lista NÃO enviada: {motivo}.\n→ {gesto}'
    calculo = envio.calculo or {}
    entrega = calculo.get('_entrega') or {}
    linhas_calculo, corrigivel = _linhas_calculo(calculo)
    if envio.estado == 'aviso_enviado':
        linhas = [f'{nome} — aviso publicado no canal, SEM quantidades. O cálculo parou em:',
                  *linhas_calculo]
        if corrigivel:
            linhas.append('→ Depois de corrigir: “Corrigir a mensagem no Slack” e avise '
                          'a equipe (a edição não notifica).')
        else:
            linhas.append('→ Para amanhã, combine a quantidade com a equipe: corrigir o '
                          'cadastro não destrava esta lista.')
        return '\n'.join(linhas)
    if envio.estado == 'correcao_incerta':
        return (f'{nome} — o Slack não confirmou a correção da mensagem.\n'
                '→ Confira o canal e repita “Corrigir a mensagem no Slack” '
                '(atualiza a mesma mensagem).')
    if envio.estado == 'falhou':
        codigo = entrega.get('codigo_erro')
        if codigo in ERROS_ENTREGA:
            motivo = f'{ERROS_ENTREGA[codigo]} ({codigo})'
        else:
            motivo = (f'Código informado pelo Slack: {codigo}.' if codigo
                      else 'O Slack não informou o motivo.')
        linhas = [f'{nome} — lista NÃO publicada: o Slack recusou o envio no canal '
                  f'{_nome_do_canal(envio.canal)}.', motivo]
        if not linhas_calculo:
            gesto = '→ Depois: “Verificar e recuperar envio”.'
        elif corrigivel:
            gesto = ('→ Depois de resolver a recusa e o cálculo: “Verificar e '
                     'recuperar envio”.')
        else:
            gesto = ('→ Resolva a recusa para os próximos dias. Para amanhã, combine a '
                     'quantidade com a equipe: a recuperação não publica lista incompleta.')
    elif envio.estado in _ESTADOS_NAO_CONFIRMADOS:
        linhas = [f'{nome} — o Slack não confirmou o envio no canal '
                  f'{_nome_do_canal(envio.canal)}: a lista pode não ter chegado.']
        if not linhas_calculo:
            gesto = ('→ Espere 5 minutos após a tentativa, confira o canal e use '
                     '“Verificar e recuperar envio” (procura a mensagem antes de '
                     'reenviar; não duplica).')
        elif corrigivel:
            gesto = ('→ Espere 5 minutos após a tentativa e use “Verificar e recuperar '
                     'envio”: localiza a mensagem se ela chegou; se não chegou, só '
                     'publica depois de corrigir o cálculo.')
        else:
            gesto = ('→ Espere 5 minutos após a tentativa e use “Verificar e recuperar '
                     'envio” para saber se a mensagem chegou. Se não chegou, combine a '
                     'quantidade com a equipe: a recuperação não publica lista incompleta.')
    else:
        return f'{nome} — envio em estado “{envio.estado}”: confira a tela.'
    if linhas_calculo:
        linhas += ['O cálculo também está bloqueado (a recuperação só publica a '
                   'lista completa):', *linhas_calculo]
    linhas.append(gesto)
    return '\n'.join(linhas)


def _bloco_atrasada(nome, envio):
    hora = envio.enviado_em.strftime('%H:%M') if envio.enviado_em else 'agora'
    return (f'{nome} — lista publicada com atraso, às {hora}: não houve tentativa '
            'ao meio-dia.\n→ Confirme que a equipe viu a mensagem no canal.')


def _assinatura(envio, detalhe=None):
    """Identidade do problema de uma loja, para não repetir o aviso no dia.

    Não usa o texto do bloco: o nome do canal vem do Slack e muda quando a
    consulta falha. `enviando` e `incerto` são o mesmo problema para o dono.
    """
    if envio is None:
        partes = ['sem_registro', detalhe or '']
    else:
        calculo = envio.calculo or {}
        estado = 'incerto' if envio.estado in _ESTADOS_NAO_CONFIRMADOS else envio.estado
        partes = [estado, str((calculo.get('_entrega') or {}).get('codigo_erro') or '')]
        if calculo.get('ok') is False:
            partes += [str(erro) for erro in calculo.get('erros') or []]
    return hashlib.sha256('\x1f'.join(partes).encode('utf-8')).hexdigest()[:16]


def _tentativa_em_curso(envio):
    """`enviando` há menos de 5 minutos: o POST ainda pode estar em curso (job
    das 12h atrasado, recuperação clicada agora). Antes da última rodada, a
    conferência espera em vez de avisar "não confirmou"."""
    if envio.estado != 'enviando':
        return False
    bruto = ((envio.calculo or {}).get('_entrega') or {}).get('ultima_tentativa_em')
    try:
        inicio = datetime.fromisoformat(bruto) if bruto else envio.criado_em
    except (TypeError, ValueError):
        return False
    if not inicio or inicio.tzinfo is not None:
        return False
    return agora() - inicio < _TENTATIVA_EM_CURSO


def texto_alerta_dono(data_alvo, blocos):
    titulo = (f'⚠️ Fermentação de amanhã — {data_alvo:%d/%m} '
              f'({DIAS_SEMANA[data_alvo.weekday()]})')
    rodape = (f'Conferir: {_link_admin()}\n'
              'Os botões valem até a meia-noite: a tela trata sempre da lista de amanhã.')
    return '\n\n'.join([titulo, *blocos, rodape])


def _avisar(alvo, itens):
    """Um WhatsApp ao dono com os problemas ainda não avisados nesta data.

    `itens`: (chave do claim, assinatura do problema, função que monta o
    bloco). O claim de cada problema é gravado e commitado ANTES do envio
    (`whatsapp.claim_envio`, tick = data-alvo + assinatura) e devolvido se o
    WhatsApp falhar, para a rodada seguinte tentar de novo. O bloco só é
    montado para o problema novo (o nome do canal consulta o Slack).
    """
    from app.services import zapi
    from app.services.whatsapp import claim_envio, devolver_claim

    numero = _numero_dono()
    if not numero:
        logger.warning('fermentacao: lista de %s sem confirmação e nenhum '
                       'número do dono configurado', alvo.isoformat())
        return {'enviado': False, 'motivo': 'sem_numero'}
    tomados, montadores, recusas = [], [], set()
    for chave, assinatura, montar in itens:
        status, anterior = claim_envio(chave, f'{alvo.isoformat()}:{assinatura}')
        if status == 'ok':
            tomados.append((chave, anterior))
            montadores.append(montar)
        else:
            recusas.add(status)
    if not montadores:
        # Sem claim durável não se envia (contrato do claim_envio).
        return {'enviado': False, 'motivo': 'erro' if 'erro' in recusas else 'duplicata'}
    # critico: o teto/hora seguraria o aviso num digest de 60 caracteres. Os
    # claims já limitam a um aviso por problema por dia, e a cópia de
    # homologação é barrada antes (instância canônica no início da conferência).
    try:
        texto = texto_alerta_dono(alvo, [montar() for montar in montadores])
        resposta = zapi.enviar_texto(numero, texto, critico=True)
    except Exception:  # noqa: BLE001 — falha inesperada conta como envio falho
        logger.exception('fermentacao: aviso ao dono falhou antes da resposta')
        resposta = {'ok': False}
    if resposta.get('ok'):
        return {'enviado': True, 'texto': texto}
    for chave, anterior in reversed(tomados):
        devolver_claim(chave, anterior)
    # Sem o texto do erro: pode trazer a URL da Z-API com o token.
    logger.error('fermentacao: aviso ao dono não saiu (desconectado=%s)',
                 bool(resposta.get('desconectado')))
    return {'enviado': False, 'motivo': 'falha_envio'}


def _avisar_falha_da_conferencia(alvo):
    bloco = ('Não foi possível conferir o envio da lista: erro interno no sistema '
             '(detalhe no log).\n→ Abra a tela e confira as lojas; se a lista não '
             'saiu, combine a quantidade com a equipe.')
    try:
        return _avisar(alvo, [(_KEY_CLAIM_ALERTA, 'erro_interno', lambda: bloco)])
    except Exception:  # noqa: BLE001 — último recurso; o log já tem a causa
        logger.exception('fermentacao: aviso da falha da conferência não saiu')
        _rollback_silencioso()
        return {'enviado': False, 'motivo': 'erro'}


def verificar_envio_de_amanha(ultima=False):
    """Conferência da lista de amanhã (cron às 12:10 e às 16:00).

    1. Loja sem nenhuma tentativa registrada (job das 12h perdido no deploy,
       processo que morreu antes da reserva, erro no cálculo): faz o mesmo
       gesto do botão “Enviar mensagens pendentes de amanhã”.
    2. Lê o estado GRAVADO de cada loja e avisa o dono no WhatsApp do que não
       ficou `enviado`, com o gesto que resolve — um aviso por problema por
       dia. Na última rodada, lista que só saiu agora também é avisada.

    `ultima`: na rodada da tarde, envio "em andamento" (trava ocupada ou
    tentativa de menos de 5 minutos) e falha da própria conferência também
    viram aviso; antes, a conferência espera a rodada seguinte. Nunca levanta.
    """
    from app.services import instancia

    # Cópia de homologação não reenvia nem avisa (o aviso é crítico e
    # atravessaria o bloqueio automático da instância).
    if not instancia.pode_falar_com_o_mundo('fermentacao-conferencia'):
        return {'rodou': False, 'motivo': 'instancia'}
    alvo = hoje() + timedelta(days=1)
    try:
        return _conferir(alvo, ultima)
    except Exception:  # noqa: BLE001 — conferência que quebra não pode calar o dia
        logger.exception('fermentacao: conferência da lista de %s falhou',
                         alvo.isoformat())
        _rollback_silencioso()
        saida = {'rodou': True, 'motivo': 'erro_interno'}
        if ultima:
            saida['aviso'] = _avisar_falha_da_conferencia(alvo)
        return saida


def _conferir(alvo, ultima):
    if db.session.get(FermentacaoEnvio, alvo):
        return {'rodou': False, 'motivo': 'envio_unificado'}
    lojas = list(destinos())
    sem_registro = [nome for nome in lojas
                    if db.session.get(FermentacaoEnvioLoja, (alvo, nome)) is None]
    detalhes, em_andamento, publicadas_agora = {}, [], set()
    reenvio = None
    if sem_registro:
        reenvio = enviar_amanha()
        if reenvio.get('estado') == 'ocupado':
            # Outro envio segura a trava (job das 12h atrasado, botão da tela):
            # a loja sem registro é dele. Só a última rodada avisa.
            if ultima:
                detalhes = dict.fromkeys(sem_registro, reenvio.get('mensagem'))
            else:
                em_andamento = list(sem_registro)
        else:
            itens_reenvio = reenvio.get('lojas') or []
            detalhes = ({item['loja']: item['mensagem'] for item in itens_reenvio}
                        or dict.fromkeys(sem_registro, reenvio.get('mensagem')))
            # Só quem estava SEM registro: loja já gravada volta no reenvio como
            # "tentativa já registrada" com o estado antigo (inclusive enviado).
            publicadas_agora = {item['loja'] for item in itens_reenvio
                                if item.get('estado') == 'enviado'
                                and item['loja'] in sem_registro}
    itens, pendentes, atrasadas = [], [], []
    for nome in lojas:
        if nome in em_andamento:
            continue
        envio = db.session.get(FermentacaoEnvioLoja, (alvo, nome))
        if envio is not None and not ultima and _tentativa_em_curso(envio):
            em_andamento.append(nome)
            continue
        detalhe = detalhes.get(nome)
        if envio is None or envio.estado != 'enviado':
            pendentes.append(nome)
            itens.append((_KEY_CLAIM_LOJA[nome], _assinatura(envio, detalhe),
                          lambda nome=nome, envio=envio, detalhe=detalhe:
                          _bloco_loja(nome, envio, detalhe)))
        elif ultima and nome in publicadas_agora:
            atrasadas.append(nome)
            itens.append((_KEY_CLAIM_LOJA[nome], 'atrasada',
                          lambda nome=nome, envio=envio: _bloco_atrasada(nome, envio)))
    saida = {'rodou': True, 'pendentes': pendentes,
             'reenvio': reenvio.get('estado') if reenvio else None}
    if em_andamento:
        saida['em_andamento'] = em_andamento
    if atrasadas:
        saida['atrasadas'] = atrasadas
    if itens:
        saida['aviso'] = _avisar(alvo, itens)
    return saida
