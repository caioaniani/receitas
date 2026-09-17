"""Bateladas independentes por sabor: 25 kg de farinha; francês, 12 kg.

Nenhuma função grava commit, credita pão pronto ou altera massa-base. O
chamador escolhe quais ordens novas entram no regime; ordens antigas sem
snapshot continuam legadas. Estoque e pesagem usam a MESMA ficha congelada.
"""

import re
import unicodedata
from copy import deepcopy
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation

from sqlalchemy import inspect

from app.extensions import db
from app.utils import SUB_RECEITA_TIPOS, dividir_etapas_preparo


def _texto(value):
    return ''.join(
        c for c in unicodedata.normalize('NFKD', str(value or ''))
        if not unicodedata.combining(c)
    ).lower().strip()


def farinha_padrao_g(rec):
    """Não confunde a família legada Pão/Sourdough com todos os produtos.

    Francês tem prioridade mesmo quando seu cadastro contém "sourdough".
    Auxiliares e retornos nunca viram produção, mesmo com família errada.
    """
    if rec is None:
        return None
    nome = _texto(getattr(rec, 'nome', ''))
    categoria = _texto(getattr(rec, 'categoria', ''))
    if (getattr(rec, 'sub_na_amassadeira', False)
            or re.search(r'\b(retorno|devolucao|brioche|baguete|baguette|levain|'
                         r'granola|iogurte|farinha|torrada|creme|massa|base|'
                         r'avocado|posta|sanduiche|bruschetta)\b', nome)
            or categoria in ('insumos', 'auxiliares', 'insumo', 'auxiliar')):
        return None
    frances = bool(re.search(r'\b(?:pao )?frances\b', nome))
    sourdough = bool(re.search(r'\bsourdough\b', nome)) or (
        getattr(rec, 'familia', None) == 'pao_sourdough'
        and categoria not in ('fornada especial', 'fornadas especiais'))
    if not frances and not sourdough:
        return None
    ingredientes = list(getattr(rec, 'ingredientes', ()) or ())
    tem_farinha = any(
        (i.tipo or 'mp') in ('mp', 'mp_direto')
        and (re.search(r'\bfarinha', _texto(i.ingrediente_nome)) or i.eh_base)
        for i in ingredientes)
    # Sanduíche/montagem usa pão pronto, não é uma nova amassada de farinha.
    # Uma ficha de pão vazia continua elegível e gera erro de cadastro claro
    # no cálculo, em vez de ganhar silenciosamente uma batelada imaginária.
    if not tem_farinha and any(
            (i.tipo or 'mp') in SUB_RECEITA_TIPOS
            and i.sub_receita is not None
            and not i.sub_receita.sub_na_amassadeira for i in ingredientes):
        return None
    estado = inspect(rec, raiseerr=False)
    if estado is not None and estado.persistent and estado.session is not None:
        from app.models.catalogo import Receita
        if estado.session.query(Receita.id).filter(
                Receita.retorno_receita_id == rec.id).first():
            return None
    if frances:
        return 12000
    return 25000


def _numero(valor, campo, *, positivo=False):
    try:
        n = Decimal(str(valor if valor is not None else 0))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f'{campo}: valor inválido na ficha técnica.') from None
    if not n.is_finite() or n < 0 or (positivo and n <= 0):
        raise ValueError(f'{campo}: valor inválido na ficha técnica.')
    return n


def _quantidade(ing, peso):
    pct = _numero(ing.porcentagem, ing.ingrediente_nome)
    return pct * peso / 100 if (ing.tipo or 'mp') in ('mp', 'sub_pct') else pct


def _sub_receita(ing):
    from app.models.catalogo import Receita
    sub = ing.sub_receita
    if sub is None and not ing.sub_receita_id:
        encontradas = Receita.query.filter(
            Receita.nome.ilike((ing.ingrediente_nome or '').strip())).limit(2).all()
        if len(encontradas) == 1:
            sub = encontradas[0]
    if sub is None:
        raise ValueError(f'Vincule a sub-receita "{ing.ingrediente_nome}" antes '
                         'de padronizar a produção.')
    return sub


def _duracao(minutos):
    m = int(minutos or 0)
    if m >= 60:
        h = m / 60
        return (f'{int(h)}h' if h.is_integer() else f'{h:.1f}h'.replace('.', ','))
    return f'{m} min'


def padrao_receita(rec, *, resolver_estoque=True, farinha_g=None):
    """Calcula UMA batelada completa, jamais adapta a farinha ao rendimento.

    A farinha é o total dos ingredientes de farinha, não necessariamente
    peso_base (uma ficha pode misturar farinha branca/integral). A farinha
    dentro do levain não é descontada da farinha direta da amassada.
    Sem peso unitário preserva o rendimento proporcional cadastrado.
    ``resolver_estoque=False`` serve à previsão sem exigir cadastro de MPs.
    """
    farinha = farinha_g if farinha_g is not None else farinha_padrao_g(rec)
    if farinha is None:
        return None
    peso = _numero(rec.peso_base, f'{rec.nome}: peso base', positivo=True)
    itens = list(rec.ingredientes)
    if not itens:
        raise ValueError(f'{rec.nome}: cadastre a farinha e os ingredientes '
                         'antes de padronizar a produção.')
    farinha_base = Decimal(0)
    # O nome específico da farinha manda, inclusive "FarinhaT65" legado.
    # `eh_base` só é fallback quando não há nenhuma farinha identificada,
    # evitando somar água marcada como base por engano junto da farinha.
    farinhas_nomeadas = [
        ing for ing in itens if (ing.tipo or 'mp') in ('mp', 'mp_direto')
        and re.search(r'\bfarinha', _texto(ing.ingrediente_nome))]
    quantidades = []
    for ing in itens:
        tipo = ing.tipo or 'mp'
        if tipo not in ('mp', 'mp_direto', 'mp_un', *SUB_RECEITA_TIPOS):
            raise ValueError(f'{rec.nome}: tipo inválido de ingrediente '
                             f'"{ing.ingrediente_nome}".')
        qtd = _quantidade(ing, peso)
        quantidades.append(qtd)
        if (tipo in ('mp', 'mp_direto') and (
                ing in farinhas_nomeadas if farinhas_nomeadas else ing.eh_base)):
            farinha_base += qtd
    if farinha_base <= 0:
        raise ValueError(f'{rec.nome}: a ficha não tem farinha válida para '
                         'calcular a batelada padronizada.')
    escala = Decimal(farinha) / farinha_base
    mps = {}
    if resolver_estoque:
        from app.models.catalogo import MateriaPrima
        for mp in MateriaPrima.ativas().all():
            mps.setdefault(_texto(mp.nome), []).append(mp)
    ingredientes, mp_totais, sub_totais = [], {}, {}
    massa = Decimal(0)
    tem_montagem = False
    for ing, original in zip(itens, quantidades):
        tipo = ing.tipo or 'mp'
        nome = ing.ingrediente_nome
        qtd = original * escala
        exibir_qtd = qtd
        unidade, pct = 'g', ing.porcentagem if tipo == 'mp' else None
        if tipo in SUB_RECEITA_TIPOS:
            sub = _sub_receita(ing)
            nome = sub.nome
            sub_totais[sub.id] = sub_totais.get(sub.id, Decimal(0)) + qtd
            if sub.sub_na_amassadeira:
                peso_sub = _numero(sub.peso_unitario, f'{sub.nome}: peso unitário',
                                   positivo=True)
                exibir_qtd = qtd * peso_sub
                massa += exibir_qtd
            else:
                unidade = 'un'
                tem_montagem = True
        else:
            if tipo == 'mp_un':
                unidade = 'un'
            else:
                massa += qtd
            if resolver_estoque and qtd > 0:
                encontradas = mps.get(_texto(nome), [])
                if len(encontradas) != 1:
                    raise ValueError(f'{rec.nome}: vincule a matéria-prima '
                                     f'"{nome}" a um cadastro único e ativo.')
                mp = encontradas[0]
                anterior = mp_totais.setdefault(
                    mp.id, {'id': mp.id, 'nome': mp.nome, 'quantidade': Decimal(0),
                            'em_unidades': tipo == 'mp_un'})
                if anterior['em_unidades'] != (tipo == 'mp_un'):
                    raise ValueError(f'{rec.nome}: a matéria-prima "{nome}" '
                                     'mistura gramas e unidades na mesma ficha; '
                                     'padronize sua unidade antes de produzir.')
                anterior['quantidade'] += qtd
        ingredientes.append({'nome': nome, 'qtd': float(exibir_qtd),
                             'unidade': unidade, 'pct': pct})
    peso_un = _numero(rec.peso_unitario, f'{rec.nome}: peso unitário')
    if peso_un > 0 and not tem_montagem:
        rendimento = massa / peso_un
    else:
        rendimento = (_numero(rec.rendimento_qtd, f'{rec.nome}: rendimento',
                               positivo=True) * escala)
    unidades = int(rendimento.to_integral_value(rounding=ROUND_FLOOR))
    if unidades <= 0:
        raise ValueError(f'{rec.nome}: a batelada não rende uma unidade inteira; '
                         'revise o peso unitário e o rendimento da ficha.')
    capacidade = _numero(rec.capacidade_amassadeira_g, 'Capacidade da amassadeira')
    avisos = []
    if capacidade > 0 and massa > capacidade:
        avisos.append(
            f'A massa desta batelada pesa {float(massa / 1000):.2f} kg '
            f'(incluindo levain), acima da capacidade cadastrada de '
            f'{float(capacidade / 1000):.2f} kg. Confirme o equipamento antes '
            'de produzir; a batelada não foi dividida.')
    elif capacidade <= 0:
        avisos.append('Confirme a capacidade da amassadeira antes de produzir '
                      'esta batelada; o cadastro não informa uma capacidade válida.')
    if peso_un <= 0:
        avisos.append('Sem peso unitário: rendimento estimado pela quantidade '
                      'cadastrada na ficha técnica.')
    return {
        'versao': 1, 'receita_id': rec.id, 'nome': rec.nome,
        'dias_producao': int(rec.dias_producao or 0),
        'farinha_g': farinha, 'escala': float(escala), 'unidades': unidades,
        'rendimento_teorico': float(rendimento), 'massa_g': float(massa),
        'capacidade_g': float(capacidade), 'peso_unitario_g': float(peso_un),
        'unidade_producao': 'un', 'ingredientes': ingredientes,
        'mp': [{**v, 'quantidade': float(v['quantidade'])}
               for v in mp_totais.values()],
        'subs': [{'id': sid, 'quantidade': float(qtd)}
                 for sid, qtd in sub_totais.items()],
        'avisos': avisos,
        'etapas': dividir_etapas_preparo(rec.modo_preparo),
        'processo': [{
            'nome': e.nome, 'duracao': _duracao(e.duracao_min),
            'duracao_min': e.duracao_min, 'equipamento': e.equipamento,
            'ativa': e.ativa, 'descricao': e.descricao,
        } for e in rec.etapas],
    }


def normalizar_item(it, *, permitir_novo=True):
    """Ajusta o alvo às bateladas inteiras, sem reescrever a ficha congelada.

    A chamada explícita cabe ao fluxo de criação/envio. Leituras e ordens
    legadas não passam a ter snapshot por simples abertura de uma tela.
    """
    from app.models.producao_batelada import PlanejamentoItemBatelada
    snapshot = getattr(it, 'batelada_padrao', None)
    alvo = _numero(it.qtd_alvo, 'Quantidade-alvo')
    produzido = _numero(it.produzido_qtd, 'Quantidade produzida')
    if snapshot is None:
        if not permitir_novo or produzido > 0 or alvo <= 0:
            return None
        from app.services.viennoiserie import eh_massa_compartilhada
        if eh_massa_compartilhada(it.receita):
            raise ValueError('Planeje a massa para folhar pelo planejamento semanal: '
                             'o sistema distribui o batimento de 25 kg entre os produtos.')
        dados = padrao_receita(it.receita)
        if dados is None:
            return None
        # A ordem pode depender de levain preparado antes. Uma edição da
        # ficha viva não pode apagar essa antecedência já comprometida.
        from app.models import PlanejamentoProducao, Receita
        from app.services.previsao_producao import ant_insumo
        plano = it.planejamento or db.session.get(PlanejamentoProducao, it.planejamento_id)
        if plano is not None:
            receitas = {r.id: r for r in Receita.query.all()}
            lead = {rid: int(r.dias_producao or 0) for rid, r in receitas.items()}
            dados['antecedencia_insumo_dias'] = ant_insumo(
                it.receita_id, plano.data, receitas, lead, None, {})
        snapshot = PlanejamentoItemBatelada(item=it, dados=dados, bateladas=0)
        db.session.add(snapshot)
    tamanho = _numero(snapshot.dados['unidades'], 'Unidades por batelada', positivo=True)
    snapshot.bateladas = int((max(alvo, produzido) / tamanho).to_integral_value(
        rounding=ROUND_CEILING))
    # Encerrar/reduzir uma ordem ao já produzido não abre outra pendência
    # só porque o rendimento real de uma batelada ficou abaixo da estimativa.
    it.qtd_alvo = (int(produzido) if produzido > 0 and alvo <= produzido
                   else snapshot.bateladas * int(tamanho))
    return resumo_item(it)


def resumo_item(it):
    snapshot = getattr(it, 'batelada_padrao', None)
    if snapshot is None:
        return None
    dados = deepcopy(snapshot.dados)
    return {
        **dados, 'bateladas': snapshot.bateladas,
        'farinha_total_g': dados['farinha_g'] * snapshot.bateladas,
        'unidades_total': dados['unidades'] * snapshot.bateladas,
        'massa_total_g': dados['massa_g'] * snapshot.bateladas,
    }


def componentes_item(it, unidades):
    """Débito proporcional às unidades INTEIRAS que a batelada entrega.

    Assim, duas confirmações parciais consomem exatamente uma batelada,
    inclusive quando o rendimento teórico tem fração de pão.
    """
    dados = resumo_item(it)
    if dados is None:
        return None
    fator = (_numero(unidades, 'Quantidade produzida')
             / _numero(dados['unidades'], 'Unidades por batelada', positivo=True))
    return {
        chave: {int(comp['id']): float(Decimal(str(comp['quantidade'])) * fator)
                for comp in dados[chave]}
        for chave in ('mp', 'subs')
    }


def consumir_item(it, unidades, usuario_id, referencia, *, produzido_antes=None):
    """Debita a ficha congelada; não credita produção nem commita."""
    from app.models.catalogo import MateriaPrima, Receita
    from app.models.estoque import ConsumoSubFracao, MovimentacaoEstoque
    from app.services.estoque_congelados import saida_producao
    componentes = componentes_item(it, unidades)
    if componentes is None:
        return None
    # O acumulador legado guarda seis casas. Calcular o DELTA entre totais
    # acumulados, em vez de arredondar cada confirmação, torna a soma das
    # partes exatamente igual ao lote nessa mesma precisão. Não há epsilon
    # que transforme uma fração real em unidade já consumida.
    dados = it.batelada_padrao.dados
    antes = _numero(it.produzido_qtd if produzido_antes is None else produzido_antes,
                    'Quantidade já produzida')
    depois = antes + _numero(unidades, 'Quantidade produzida')
    tamanho = _numero(dados['unidades'], 'Unidades por batelada', positivo=True)
    precisao = Decimal('0.000001')
    deltas = {
        chave: {
            int(comp['id']): (
                (Decimal(str(comp['quantidade'])) * depois / tamanho).quantize(precisao)
                - (Decimal(str(comp['quantidade'])) * antes / tamanho).quantize(precisao)
            ) for comp in dados[chave]
        } for chave in ('mp', 'subs')
    }
    mp_ids, sub_ids = set(componentes['mp']), set(componentes['subs'])
    mps = {mp.id: mp for mp in MateriaPrima.query.filter(
        MateriaPrima.id.in_(mp_ids)).order_by(MateriaPrima.id)
        .with_for_update().populate_existing().all()}
    subs = {s.id: s for s in Receita.query.filter(Receita.id.in_(sub_ids)).all()}
    if set(mps) != mp_ids or set(subs) != sub_ids:
        raise ValueError('Um ingrediente da batelada foi removido do cadastro. '
                         'Nenhum consumo pode ser confirmado sem revisar a ordem.')
    for mp_id, delta in deltas['mp'].items():
        qtd = float(delta)
        if qtd <= 0:
            continue
        mp = mps[mp_id]
        db.session.add(MovimentacaoEstoque(
            materia_prima_id=mp_id, tipo='saida', quantidade=qtd,
            referencia=referencia, usuario_id=usuario_id))
        # O novo regime mantém saldo assinado: zerar uma falta aqui apagaria
        # parte do débito e o estorno da reserva fabricaria matéria-prima.
        mp.estoque_atual = (mp.estoque_atual or 0) - qtd
    out = []
    # A trava da sub serializa inclusive a criação do acumulador ainda ausente.
    for sub_id in sorted(sub_ids):
        consumo = deltas['subs'][sub_id]
        if consumo <= 0:
            continue
        from app.services.estoque_massa import consumir_massa, eh_massa_folhar
        if eh_massa_folhar(subs[sub_id]):
            res = consumir_massa(subs[sub_id], consumo, usuario_id, referencia)
            out.append({'sub_id': sub_id, **res})
            continue
        Receita.query.filter_by(id=sub_id).with_for_update().populate_existing().one()
        frac = ConsumoSubFracao.query.filter_by(
            receita_id=sub_id).with_for_update().populate_existing().first()
        if frac is None:
            frac = ConsumoSubFracao(receita_id=sub_id, fracao_pendente=0.0)
            db.session.add(frac)
        total = consumo + _numero(frac.fracao_pendente, 'Fração acumulada')
        inteiro = int(total.to_integral_value(rounding=ROUND_FLOOR))
        frac.fracao_pendente = max(0.0, float(total - inteiro))
        if inteiro > 0:
            res = saida_producao(
                receita_id=sub_id, quantidade=inteiro, usuario_id=usuario_id,
                referencia=referencia)
            out.append({'sub_id': sub_id, **res})
    return out


def mise_item(it):
    """Pesagem por batelada (N repetições), nunca uma massa-base somada."""
    dados = resumo_item(it)
    if dados is None:
        return None
    return {**dados, 'batelada_padrao': True, 'unidades_batelada': dados['unidades']}
