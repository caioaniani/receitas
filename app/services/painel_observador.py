"""Sala de controle operacional do perfil Observador.

Somente leitura: agrega fatos que ja existem nos modulos de pedidos,
producao, estoque, entregas, integracoes e auditoria. Nao recalcula o motor,
nao chama APIs externas e nao grava estado.
"""
import json
from collections import defaultdict
from datetime import datetime, time, timedelta

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import joinedload, selectinload

from app.extensions import db
from app.models import (
    AuditLog,
    EstoqueLoja,
    EstoqueProducao,
    Loja,
    NotificacaoWhatsapp,
    PedidoLocal,
    PedidoLoja,
    PedidoOnline,
    PlanejamentoItem,
    PlanejamentoProducao,
    Receita,
    TinyPedidoProcessado,
    VendaB2B,
    VendaSeruDiaBreakdown,
    VendaSeruDiaLoja,
)
from app.utils import agora, hoje

_STATUS_LABEL = {
    'pendente': 'Aguardando separação',
    'confirmado': 'Confirmado',
    'separado': 'Separado',
    'em_transporte': 'Em transporte',
    'recebido': 'Recebido',
    'pago': 'Pago',
    'em_preparo': 'Em preparo',
    'a_caminho': 'A caminho',
    'entregue': 'Entregue',
    'agendado': 'Agendado',
}

_LOJA_ABERTOS = ('pendente', 'confirmado', 'separado', 'em_transporte')
_SITE_ATIVOS = ('pago', 'em_preparo', 'a_caminho', 'entregue')
_SITE_ABERTOS = ('pago', 'em_preparo', 'a_caminho')
_B2B_ABERTOS = ('pendente', 'separado', 'em_transporte')


def _inicio_fim(dia):
    return datetime.combine(dia, time.min), datetime.combine(dia, time.max)


def _pedidos_sete_dias():
    """Volume recebido por canal nos ultimos sete dias, sem dinheiro."""
    fim = hoje()
    inicio = fim - timedelta(days=6)
    inicio_dt, _ = _inicio_fim(inicio)
    _, fim_dt = _inicio_fim(fim)
    por_dia = {
        inicio + timedelta(days=i): defaultdict(int)
        for i in range(7)
    }

    for data, in db.session.query(PedidoLoja.data_pedido).filter(
            PedidoLoja.data_pedido >= inicio,
            PedidoLoja.data_pedido <= fim).all():
        if data in por_dia:
            por_dia[data]['loja'] += 1

    for criado_em, in db.session.query(PedidoOnline.criado_em).filter(
            PedidoOnline.criado_em >= inicio_dt,
            PedidoOnline.criado_em <= fim_dt).all():
        if criado_em and criado_em.date() in por_dia:
            por_dia[criado_em.date()]['site'] += 1

    for data, in db.session.query(VendaB2B.data_venda).filter(
            VendaB2B.data_venda >= inicio,
            VendaB2B.data_venda <= fim).all():
        if data in por_dia:
            por_dia[data]['b2b'] += 1

    for criado_em, in db.session.query(PedidoLocal.criado_em).filter(
            PedidoLocal.criado_em >= inicio_dt,
            PedidoLocal.criado_em <= fim_dt).all():
        if criado_em and criado_em.date() in por_dia:
            por_dia[criado_em.date()]['manual'] += 1

    marketplaces = (db.session.query(
        VendaSeruDiaBreakdown.data,
        func.sum(VendaSeruDiaBreakdown.valor))
        .filter(
            VendaSeruDiaBreakdown.data >= inicio,
            VendaSeruDiaBreakdown.data <= fim,
            VendaSeruDiaBreakdown.dimensao == 'marketplace',
            VendaSeruDiaBreakdown.chave.in_(('ifood', '99food', 'rappi')),
        )
        .group_by(VendaSeruDiaBreakdown.data).all())
    for data, quantidade in marketplaces:
        if data in por_dia:
            por_dia[data]['marketplace'] += int(quantidade or 0)

    dias = []
    for data, canais in por_dia.items():
        total = sum(canais.values())
        dias.append({
            'data': data,
            'label': data.strftime('%d/%m'),
            'loja': canais['loja'],
            'site': canais['site'],
            'b2b': canais['b2b'],
            'manual': canais['manual'],
            'marketplace': canais['marketplace'],
            'total': total,
        })
    maior = max((d['total'] for d in dias), default=0)
    for d in dias:
        d['altura_pct'] = round((d['total'] / maior) * 100) if maior else 0
    return {
        'dias': dias,
        'hoje': dias[-1]['total'] if dias else 0,
        'total': sum(d['total'] for d in dias),
    }


def _producao_hoje():
    dia = hoje()
    planos = (PlanejamentoProducao.query.options(
        selectinload(PlanejamentoProducao.itens)
        .selectinload(PlanejamentoItem.receita))
        .filter(PlanejamentoProducao.data == dia,
                PlanejamentoProducao.origem == 'cronograma')
        .order_by(PlanejamentoProducao.id.desc()).all())

    rascunhos = [p for p in planos if p.enviado_ao_padeiro is False]
    enviados = [p for p in planos if p.enviado_ao_padeiro is not False]
    plano = (rascunhos or enviados or [None])[0]

    if plano is None:
        resumo = {
            'estado': 'sem_ordem', 'estado_label': 'Sem ordem enviada',
            'total_itens': 0, 'concluidos': 0, 'pendentes_itens': 0,
            'progresso_pct': 0,
            'pendentes_top': [],
        }
    else:
        itens = [it for it in plano.itens if it.dispensada_em is None]
        dados = []
        for it in itens:
            alvo = int(it.qtd_alvo or 0)
            produzido = int(it.produzido_qtd or 0)
            encerrado = it.falta_encerrada_em is not None
            falta = 0 if encerrado else max(0, alvo - produzido)
            dados.append({
                'nome': it.receita.nome if it.receita else '(receita)',
                'alvo': alvo, 'produzido': produzido, 'falta': falta,
                'concluido': encerrado or produzido >= alvo,
            })
        concluidos = sum(1 for it in dados if it['concluido'])
        total = len(dados)
        resumo = {
            'estado': 'aguardando_envio' if rascunhos else 'enviada',
            'estado_label': ('Aguardando envio ao padeiro'
                             if rascunhos else 'Ordem enviada'),
            'total_itens': total,
            'concluidos': concluidos,
            'pendentes_itens': max(0, total - concluidos),
            # Progresso por linha de receita: peso/gramas nao distorcem o card.
            'progresso_pct': round(concluidos / total * 100) if total else 100,
            'pendentes_top': sorted(
                (it for it in dados if not it['concluido']),
                key=lambda it: it['falta'], reverse=True)[:5],
        }

    falta = (func.coalesce(PlanejamentoItem.qtd_alvo, 0)
             - func.coalesce(PlanejamentoItem.produzido_qtd, 0))
    vencidas = (db.session.query(func.count(PlanejamentoItem.id))
        .join(PlanejamentoProducao,
              PlanejamentoItem.planejamento_id == PlanejamentoProducao.id)
        .filter(
            PlanejamentoProducao.enviado_ao_padeiro.isnot(False),
            PlanejamentoProducao.data < dia,
            PlanejamentoProducao.data >= dia - timedelta(days=30),
            PlanejamentoItem.dispensada_em.is_(None),
            falta > 0,
        ).scalar())
    resumo['vencidas_itens'] = int(vencidas or 0)
    return resumo


def _registro_entrega(canal, referencia, destino, data, status):
    status = (status or 'pendente').lower()
    concluido = status in {'entregue', 'recebido'}
    em_rota = status in {'a_caminho', 'em_transporte'}
    em_preparo = status in {'em_preparo', 'separado'}
    return {
        'canal': canal,
        'referencia': referencia,
        'destino': destino or '(destino)',
        'data': data,
        'status': status,
        'status_label': _STATUS_LABEL.get(status, status.replace('_', ' ')),
        'concluido': concluido,
        'em_rota': em_rota,
        'em_preparo': em_preparo,
        'atrasado': bool(data and data < hoje() and not concluido),
    }


def _entregas():
    dia = hoje()
    registros = []

    lojas = (PedidoLoja.query.options(joinedload(PedidoLoja.loja)).filter(
        PedidoLoja.data_entrega.isnot(None),
        or_(
            PedidoLoja.data_entrega == dia,
            and_(PedidoLoja.data_entrega < dia,
                 PedidoLoja.status.in_(_LOJA_ABERTOS)),
        ),
        PedidoLoja.status != 'cancelado',
    ).all())
    registros.extend(_registro_entrega(
        'Loja', f'LOJA-{p.id}', p.loja.nome if p.loja else 'Loja',
        p.data_entrega, p.status) for p in lojas)

    sites = (PedidoOnline.query.filter(
        PedidoOnline.data_entrega.isnot(None),
        PedidoOnline.status.in_(_SITE_ATIVOS),
        or_(
            PedidoOnline.data_entrega == dia,
            and_(PedidoOnline.data_entrega < dia,
                 PedidoOnline.status.in_(_SITE_ABERTOS)),
        ),
    ).all())
    registros.extend(_registro_entrega(
        'Site', p.codigo or f'SITE-{p.id}', p.nome_cliente,
        p.data_entrega, p.status) for p in sites)

    b2b = (VendaB2B.query.filter(
        VendaB2B.status == 'ativa', VendaB2B.data_entrega.isnot(None),
        or_(
            VendaB2B.data_entrega == dia,
            and_(VendaB2B.data_entrega < dia,
                 VendaB2B.status_entrega.in_(_B2B_ABERTOS)),
        ),
    ).all())
    registros.extend(_registro_entrega(
        'B2B', f'B2B-{p.id}', p.cliente_display,
        p.data_entrega, p.status_entrega) for p in b2b)

    manuais = PedidoLocal.query.filter(PedidoLocal.data_entrega == dia).all()
    registros.extend(_registro_entrega(
        'Manual', p.code or f'MANUAL-{p.id}', p.destinatario,
        p.data_entrega, 'agendado') for p in manuais)

    de_hoje = [r for r in registros if r['data'] == dia]
    fila = [r for r in registros if not r['concluido']]
    fila.sort(key=lambda r: (
        0 if r['atrasado'] else 1,
        r['data'] or dia,
        r['referencia'],
    ))
    return {
        'total': len(de_hoje),
        'concluidas': sum(1 for r in de_hoje if r['concluido']),
        'em_rota': sum(1 for r in de_hoje if r['em_rota']),
        'em_preparo': sum(1 for r in de_hoje if r['em_preparo']),
        'aguardando': sum(1 for r in de_hoje
                          if not (r['concluido'] or r['em_rota']
                                  or r['em_preparo'])),
        'atrasadas': sum(1 for r in registros if r['atrasado']),
        'fila': fila[:8],
    }


def _estoque_baixo():
    itens = []
    industria = (db.session.query(Receita, EstoqueProducao)
        .outerjoin(EstoqueProducao,
                   EstoqueProducao.receita_id == Receita.id)
        .filter(
            Receita.arquivada_em.is_(None),
            Receita.estoque_minimo_industria.isnot(None),
            Receita.estoque_minimo_industria > 0,
        ).all())
    for receita, estoque in industria:
        minimo = int(receita.estoque_minimo_industria or 0)
        quantidade = int(estoque.quantidade or 0) if estoque else 0
        if quantidade < minimo:
            itens.append({
                'local': 'Indústria', 'item': receita.nome,
                'quantidade': quantidade, 'minimo': minimo,
                'zerado': quantidade <= 0,
            })

    lojas = (EstoqueLoja.query.options(
        joinedload(EstoqueLoja.loja), joinedload(EstoqueLoja.receita),
        joinedload(EstoqueLoja.produto),
        joinedload(EstoqueLoja.materia_prima))
        .join(Loja, EstoqueLoja.loja_id == Loja.id)
        .filter(Loja.ativa.is_(True),
                EstoqueLoja.estoque_minimo.isnot(None),
                EstoqueLoja.estoque_minimo > 0).all())
    for estoque in lojas:
        minimo = int(estoque.estoque_minimo or 0)
        quantidade = int(estoque.disponivel)
        if quantidade < minimo:
            itens.append({
                'local': estoque.loja.nome if estoque.loja else 'Loja',
                'item': estoque.nome_item,
                'quantidade': quantidade, 'minimo': minimo,
                'zerado': quantidade <= 0,
            })

    itens.sort(key=lambda x: (
        0 if x['zerado'] else 1,
        x['quantidade'] / x['minimo'] if x['minimo'] else 0,
        x['local'], x['item'],
    ))
    return {
        'total': len(itens),
        'zerados': sum(1 for item in itens if item['zerado']),
        'itens': itens[:10],
    }


def _tempo_relativo(momento):
    if momento is None:
        return 'sem registro'
    segundos = max(0, int((agora() - momento).total_seconds()))
    if segundos < 60:
        return 'agora'
    if segundos < 3600:
        return f'há {segundos // 60} min'
    if segundos < 86400:
        return f'há {segundos // 3600} h'
    return f'há {segundos // 86400} dia(s)'


def _integracoes():
    corte = agora() - timedelta(hours=24)
    seru_em = db.session.query(func.max(
        VendaSeruDiaLoja.atualizado_em)).scalar()
    tiny_em = db.session.query(func.max(
        TinyPedidoProcessado.processado_em)).scalar()
    site_em = db.session.query(func.max(PedidoOnline.criado_em)).scalar()
    whatsapp_falhas = NotificacaoWhatsapp.query.filter(
        NotificacaoWhatsapp.criado_em >= corte,
        or_(NotificacaoWhatsapp.ok.is_(False),
            NotificacaoWhatsapp.zaap_id.is_(None))).count()
    whatsapp_ok_em = db.session.query(func.max(
        NotificacaoWhatsapp.criado_em)).filter(
            NotificacaoWhatsapp.ok.is_(True)).scalar()

    return [
        {
            'nome': 'PDV Seru', 'estado': 'neutro',
            'texto': f'Última atualização {_tempo_relativo(seru_em)}',
        },
        {
            'nome': 'PDV Tiny', 'estado': 'neutro',
            'texto': f'Último pedido processado {_tempo_relativo(tiny_em)}',
        },
        {
            'nome': 'Loja online', 'estado': 'neutro',
            'texto': f'Último pedido recebido {_tempo_relativo(site_em)}',
        },
        {
            'nome': 'WhatsApp',
            'estado': 'atencao' if whatsapp_falhas else 'ok',
            'texto': (f'{whatsapp_falhas} falha(s) nas últimas 24 h'
                      if whatsapp_falhas else
                      f'Sem falhas · último envio {_tempo_relativo(whatsapp_ok_em)}'),
            'falhas': whatsapp_falhas,
        },
    ]


_EVENTOS = {
    'pedido_loja': ('Pedidos', 'pedido da loja'),
    'pedido_online': ('Pedidos', 'pedido do site'),
    'planejamento_producao': ('Produção', 'ordem de produção'),
    'planejamento_item': ('Produção', 'item da produção'),
    'estoque_loja': ('Estoque', 'estoque de loja'),
    'estoque_producao': ('Estoque', 'estoque da indústria'),
    'atribuicao_entrega': ('Entregas', 'entrega'),
    'lote_saida': ('Entregas', 'rota de entrega'),
}

_CAMPOS_EVENTO = {
    'status': 'situação',
    'data_entrega': 'entrega',
    'qtd_alvo': 'planejado',
    'produzido_qtd': 'produzido',
    'quantidade': 'quantidade',
    'enviado_ao_padeiro': 'envio ao padeiro',
    'driver_id': 'motorista',
    'ordem': 'ordem da rota',
}


def _valor_evento(valor):
    if valor is True:
        return 'sim'
    if valor is False:
        return 'não'
    if valor is None:
        return 'vazio'
    return str(valor).replace('_', ' ')


def _json_obj(bruto):
    try:
        return json.loads(bruto) if bruto else {}
    except (TypeError, ValueError):
        return {}


def _eventos_recentes():
    logs = (AuditLog.query.options(joinedload(AuditLog.usuario))
            .filter(AuditLog.tabela.in_(tuple(_EVENTOS)))
            .order_by(AuditLog.criado_em.desc()).limit(12).all())
    eventos = []
    verbos = {'insert': 'criou', 'update': 'atualizou', 'delete': 'excluiu'}
    for log in logs:
        area, objeto = _EVENTOS[log.tabela]
        quem = log.usuario.nome if log.usuario else 'Sistema'
        frase = f'{quem} {verbos.get(log.acao, "alterou")} {objeto} #{log.registro_id}'
        if log.acao == 'update':
            antes, depois = _json_obj(log.antes), _json_obj(log.depois)
            mudancas = []
            for campo, label in _CAMPOS_EVENTO.items():
                if campo in antes or campo in depois:
                    velho, novo = antes.get(campo), depois.get(campo)
                    if velho != novo:
                        mudancas.append(
                            f'{label}: {_valor_evento(velho)} → {_valor_evento(novo)}')
            if mudancas:
                frase += ' · ' + '; '.join(mudancas[:2])
        eventos.append({
            'area': area, 'frase': frase, 'momento': log.criado_em,
        })
    return eventos


def montar_painel():
    pedidos = _pedidos_sete_dias()
    producao = _producao_hoje()
    entregas = _entregas()
    estoque = _estoque_baixo()
    integracoes = _integracoes()

    alertas = []
    if producao['estado'] == 'sem_ordem':
        alertas.append({
            'nivel': 'critico', 'titulo': 'Hoje ainda está sem ordem de produção',
            'texto': 'Nenhuma ordem do cronograma foi enviada ao padeiro.',
        })
    elif producao['estado'] == 'aguardando_envio':
        alertas.append({
            'nivel': 'critico', 'titulo': 'Ordem aguardando envio ao padeiro',
            'texto': 'O planejamento existe, mas ainda não chegou à tela do padeiro.',
        })
    if producao['vencidas_itens']:
        alertas.append({
            'nivel': 'atencao', 'titulo': 'Produção anterior ainda pendente',
            'texto': (f'{producao["vencidas_itens"]} item(ns) de dias '
                      'anteriores ainda estão sem confirmação.'),
        })
    if entregas['atrasadas']:
        alertas.append({
            'nivel': 'critico', 'titulo': 'Entregas vencidas ainda abertas',
            'texto': f'{entregas["atrasadas"]} entrega(s) precisam de acompanhamento.',
        })
    if estoque['total']:
        alertas.append({
            'nivel': 'atencao', 'titulo': 'Estoque abaixo do mínimo',
            'texto': (f'{estoque["total"]} item(ns) abaixo do piso; '
                      f'{estoque["zerados"]} zerado(s).'),
        })
    whatsapp = next(i for i in integracoes if i['nome'] == 'WhatsApp')
    if whatsapp.get('falhas'):
        alertas.append({
            'nivel': 'atencao', 'titulo': 'Falhas recentes no WhatsApp',
            'texto': whatsapp['texto'],
        })

    return {
        'gerado_em': agora(),
        'pedidos': pedidos,
        'producao': producao,
        'entregas': entregas,
        'estoque': estoque,
        'integracoes': integracoes,
        'eventos': _eventos_recentes(),
        'alertas': alertas,
        'alertas_total': len(alertas),
    }
