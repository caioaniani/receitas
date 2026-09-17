"""Completa a massa comum e distribui o excedente por necessidade líquida.

A primeira explosão calcula a massa realmente necessária, inclusive o
estoque dos intermediários. Uma segunda confere a distribuição proposta.
O lote não é multiplicado pelo número de sabores nem pela quantidade de
elos da ficha. Nenhuma função deste módulo grava ordens ou estoque.
"""

import os
from copy import deepcopy
from datetime import timedelta
from decimal import ROUND_CEILING, Decimal

from app.services.viennoiserie import (
    eh_item_massa,
    eh_massa_compartilhada,
    padrao_massa,
)


def _d(n):
    return Decimal(str(n or 0))


def _caminhos(rid, raiz, dia, receitas, lead, visitados=()):
    from app.services.previsao_producao import _subs_de, producao_permitida_no_dia
    if rid == raiz:
        return [(dia, Decimal(1))]
    if rid in visitados:
        raise ValueError('Há um ciclo nas fichas da viennoiserie. Revise as sub-receitas.')
    retorno = {r.retorno_receita_id for r in receitas.values() if r.retorno_receita_id}
    out = []
    for sid, ratio in _subs_de(rid, receitas):
        if sid in retorno:
            continue  # Almond de retorno não gera uma nova amassada.
        anterior = dia - timedelta(days=lead.get(sid, 0))
        for _ in range(14):
            if producao_permitida_no_dia(receitas[sid], anterior):
                break
            anterior -= timedelta(days=1)
        else:
            raise ValueError('A massa não tem dia de produção permitido antes dos produtos.')
        for data, fator in _caminhos(sid, raiz, anterior, receitas, lead, (*visitados, rid)):
            out.append((data, _d(ratio) * fator))
    return out


def _contexto(base, dias, receitas, lead, raizes):
    from app.models import CronogramaOverride, PlanejamentoProducao
    from app.services.previsao_producao import ant_insumo
    from app.utils import hoje
    freeze_on = os.environ.get('FREEZE_INSUMO', '1') != '0'
    planos = PlanejamentoProducao.query.filter(
        PlanejamentoProducao.origem == 'cronograma',
        PlanejamentoProducao.data >= min(dias) - timedelta(days=14),
        PlanejamentoProducao.data <= max(dias)).all()
    ordens = {(it.receita_id, p.data): it for p in planos for it in p.itens
               if p.enviado_ao_padeiro is not False}
    datas_enviadas = {p.data for p in planos if p.enviado_ao_padeiro is not False}
    manuais = {(o.receita_id, o.data) for o in CronogramaOverride.query.filter(
        CronogramaOverride.data.in_(dias)).all()}
    massas_enviadas = {(rid, dia) for (rid, dia), it in ordens.items()
                       if rid in raizes and (not eh_item_massa(it)
                           or (freeze_on and (dia <= hoje() or int(it.produzido_qtd or 0) > 0)))}
    memo = {}
    for rr in base:
        rid = rr['receita_id']
        if rid in raizes:
            continue
        for c, dia in zip(rr['por_dia'], dias):
            caminhos = [(raiz, d) for raiz in raizes
                        for d, _ in _caminhos(rid, raiz, dia, receitas, lead)]
            if not caminhos:
                continue
            it = ordens.get((rid, dia))
            fechado = freeze_on and (rid, dia) not in manuais and dia in datas_enviadas and (
                dia <= hoje() or (dia - hoje()).days <=
                ant_insumo(rid, dia, receitas, lead, None, memo))
            massa_enviada = any(chave in massas_enviadas for chave in caminhos)
            if fechado:
                alvo = int(it.qtd_alvo or 0) if it and not it.dispensada_em else 0
                confirmado = int(it.produzido_qtd or 0) if it else 0
                c['qtd_solicitada'] = c['qtd']
                c.update(qtd=alvo, alvo_enviado=alvo,
                         produzido_confirmado=confirmado, batelada_congelada=True)
            if fechado or massa_enviada or (rid, dia) in manuais:
                c['viennoiserie_fixa'] = True
        rr['total'] = sum(c['qtd'] for c in rr['por_dia'])
    # O envio do batimento aprova também seu destino. Se só a ordem da
    # massa foi enviada hoje, amanhã não pode voltar ao mix anterior ao
    # reforço, deixando a maior parte do batimento sem produto.
    for chave in sorted(massas_enviadas):
        massa = ordens[chave]
        if not eh_item_massa(massa) or massa.dispensada_em:
            continue
        for destino in massa.batelada_padrao.dados.get('produtos', []):
            from datetime import date
            dia = date.fromisoformat(destino['data'])
            if dia not in dias:
                continue
            rid, i = destino['receita_id'], dias.index(dia)
            if (rid, dia) in manuais:
                continue
            rr = _linha(base, rid)
            if rr is None:
                rr = {'receita_id': rid, 'nome': receitas[rid].nome, 'total': 0,
                      'em_estoque': 0, 'dias_producao': lead.get(rid, 0),
                      'por_dia': [{'data': d.isoformat(), 'qtd': 0, 'fornadas': None}
                                  for d in dias]}
                base.append(rr)
            c = rr['por_dia'][i]
            solicitado = c.get('qtd_solicitada', c['qtd'])
            filho = ordens.get((rid, dia))
            alvo = int(filho.qtd_alvo or 0) if filho else destino['quantidade']
            confirmado = int(filho.produzido_qtd or 0) if filho else 0
            if filho and filho.dispensada_em:
                alvo = confirmado
            adicional = max(0, alvo - confirmado - int(c['qtd']))
            if adicional:
                base[:] = _aplicar_extras(base, {(rid, i): adicional}, dias, receitas)
                rr, c = _linha(base, rid), _linha(base, rid)['por_dia'][i]
            c.update(qtd=alvo, alvo_enviado=alvo, produzido_confirmado=confirmado,
                     batelada_congelada=True, viennoiserie_fixa=True,
                     massa_comprometida=True, qtd_solicitada=solicitado)
            rr['total'] = sum(cd['qtd'] for cd in rr['por_dia'])
    return ordens, manuais


def _linha(linhas, rid):
    return next((rr for rr in linhas if rr['receita_id'] == rid), None)


def _necessidade(linhas, rid, i, peso):
    rr = _linha(linhas, rid)
    if not rr:
        return Decimal(0)
    valores = rr.get('_necessidade_insumo_por_dia', [])
    # Mesma precisão física do ledger de massa (micragrama), eliminando
    # apenas ruído binário da explosão antiga em float, não falta real.
    return (_d(valores[i]) * peso).quantize(Decimal('.000001')) if i < len(valores) else Decimal(0)


def _aplicar_extras(base, propostas, dias, receitas):
    """Produto adiantado cobre os próximos dias, sem apagar decisão humana."""
    novo = deepcopy(base)
    for (rid, i), adicional in propostas.items():
        rr = _linha(novo, rid)
        c = rr['por_dia'][i]
        c['qtd'] += adicional
        c['reforco_viennoiserie'] = c.get('reforco_viennoiserie', 0) + adicional
        saldo = adicional
        antecedencia = receitas[rid].antecedencia_max_dias
        for k, futuro in enumerate(rr['por_dia'][i + 1:], i + 1):
            if futuro.get('viennoiserie_fixa') or futuro.get('batelada_congelada'):
                continue
            refs = (rr.get('ref_pesos') or [])
            referencia = max((int(ref) for ref, peso in (refs[k] if k < len(refs) else [])
                              if peso > 0), default=k)
            if antecedencia is not None and referencia - i > max(0, antecedencia):
                continue
            reduzir = min(saldo, int(futuro['qtd']))
            futuro['qtd'] -= reduzir
            futuro['coberto_viennoiserie'] = futuro.get('coberto_viennoiserie', 0) + reduzir
            saldo -= reduzir
            if saldo == 0:
                break
        rr['total'] = sum(c['qtd'] for c in rr['por_dia'])
    return novo


def planejar_viennoiserie(linhas, dias, receitas, lead, bal, *, explodir):
    from app.models import PlanejamentoItem, PlanejamentoItemBatelada
    from app.services.alocacao_viennoiserie import distribuir_excedente
    from app.services.cronograma_bateladas import _marcar, quantidade_pendente
    from app.services.previsao_producao import producao_permitida_no_dia
    from app.utils import hoje
    historicas = {rid for (rid,) in
                  PlanejamentoItem.query.with_entities(PlanejamentoItem.receita_id)
                  .join(PlanejamentoItemBatelada)
                  .filter(PlanejamentoItemBatelada.dados['tipo'].as_string() == 'massa_viennoiserie')
                  .distinct().all()}
    raizes = {rid: rec for rid, rec in receitas.items()
              if eh_massa_compartilhada(rec) or rid in historicas}
    if not raizes:
        explodir(linhas, dias, receitas, lead, bal)
        return
    base = deepcopy(linhas)
    ordens, manuais = _contexto(base, dias, receitas, lead, raizes)
    # A massa é derivada dos produtos, nunca uma demanda adicional em
    # bolas antigas somada ao novo contador de batimentos.
    base = [rr for rr in base if rr['receita_id'] not in raizes]

    def simular(b):
        out = deepcopy(b)
        explodir(out, dias, receitas, lead, bal)
        return out

    atual = simular(base)
    planejados = {}
    bloqueados = {}
    for raiz, rec in raizes.items():
        try:
            padrao = padrao_massa(rec, resolver_estoque=False)
            if padrao is None:
                raise ValueError('Revise a ficha da massa compartilhada antes de criar novos batimentos.')
        except ValueError as exc:
            padrao = None
            erro = str(exc)
        for j, dia_massa in enumerate(dias):
            it = ordens.get((raiz, dia_massa))
            # Não reinterpretar bolas de uma ordem antiga como batimentos.
            if it and (not eh_item_massa(it) or dia_massa <= hoje()
                       or int(it.produzido_qtd or 0) > 0):
                planejados[raiz, j] = (it, None, None)
                continue
            padrao_dia = it.batelada_padrao.dados if it and eh_item_massa(it) else padrao
            if padrao_dia is None:
                bloqueados[raiz, j] = erro
                continue
            if (raiz, dia_massa) in manuais and it is None:
                bloqueados[raiz, j] = ('Há uma edição manual da massa. Remova essa edição '
                                      'e ajuste as quantidades dos produtos para recalcular o batimento.')
                continue
            peso = _d(padrao_dia['peso_bola_g'])
            lote = _d(padrao_dia['massa_g'])
            if not producao_permitida_no_dia(rec, dia_massa):
                continue
            necessidade = _necessidade(atual, raiz, j, peso)
            if necessidade <= 0:
                continue
            batimentos = int((necessidade / lote).to_integral_value(rounding=ROUND_CEILING))
            capacidade = lote * batimentos
            candidatos, chaves = [], {}
            for rr in base:
                rid = rr['receita_id']
                produto = receitas.get(rid)
                if rid == raiz or not produto or produto.sugerir_pedido_loja is False:
                    continue
                for i, (c, dia) in enumerate(zip(rr['por_dia'], dias)):
                    if c['qtd'] <= 0 or c.get('viennoiserie_fixa'):
                        continue
                    caminhos = _caminhos(rid, raiz, dia, receitas, lead)
                    coef = sum((f for d, f in caminhos if d == dia_massa), Decimal(0))
                    if coef <= 0 or any(d != dia_massa for d, _ in caminhos):
                        continue
                    teto = int(produto.producao_max_dia or 0)
                    # Identificador temporário: mesma receita em duas datas
                    # não pode sobrescrever a distribuição uma da outra.
                    chave = rid * (len(dias) + 1) + i
                    chaves[chave] = (rid, i)
                    candidatos.append({
                        'receita_id': chave, 'nome': produto.nome,
                        'gramas_por_unidade': coef * peso,
                        'necessidade': sum(int(x['qtd']) for x in rr['por_dia'][i:]),
                        'maximo_adicional': max(0, teto - c['qtd']) if teto else None,
                    })
            alocacao = distribuir_excedente(capacidade - necessidade, candidatos)
            propostas = {chaves[k]: v for k, v in alocacao['alocacoes'].items() if v}
            # O rendimento das montagens intermediárias é inteiro. Confira a
            # explosão real, em vez de assumir que o coeficiente linear basta.
            while True:
                proposta_base = _aplicar_extras(base, propostas, dias, receitas)
                teste = simular(proposta_base)
                excesso = _necessidade(teste, raiz, j, peso) - capacidade
                if excesso <= 0:
                    base, atual = proposta_base, teste
                    break
                if not propostas:
                    break
                # Retira primeiro o último reforço (menor prioridade), nunca
                # corta os produtos que já eram necessários antes do lote.
                candidatos_por_chave = {chaves[c['receita_id']]: c for c in candidatos}
                chave = min(propostas, key=lambda k: (
                    candidatos_por_chave[k]['necessidade'],
                    candidatos_por_chave[k]['nome'], k))
                identificador = next(k for k, v in chaves.items() if v == chave)
                candidato = next(c for c in candidatos if c['receita_id'] == identificador)
                cortar = int((excesso / _d(candidato['gramas_por_unidade'])).to_integral_value(
                    rounding=ROUND_CEILING))
                propostas[chave] = max(0, propostas[chave] - max(1, cortar))
                if propostas[chave] == 0:
                    del propostas[chave]
            planejados[raiz, j] = (it, padrao_dia, batimentos)

    # Substitui somente a linha de amassamento. As montagens continuam nas
    # suas unidades e com a manteiga/recheios previstos pela própria ficha.
    for (raiz, j), (it, padrao, batimentos) in planejados.items():
        rr = _linha(atual, raiz)
        if rr is None:
            rr = {'receita_id': raiz, 'nome': receitas[raiz].nome, 'insumo': True,
                  'em_estoque': 0, 'dias_producao': lead.get(raiz, 0), 'total': 0,
                  'por_dia': [{'data': d.isoformat(), 'qtd': 0, 'fornadas': None}
                              for d in dias]}
            atual.append(rr)
        c = rr['por_dia'][j]
        if padrao is None:
            alvo = int(it.qtd_alvo or 0) if not it.dispensada_em else int(it.produzido_qtd or 0)
            c.update(qtd=alvo,
                     produzido_confirmado=int(it.produzido_qtd or 0),
                     alvo_enviado=alvo, batelada_congelada=True)
            if eh_item_massa(it):
                _marcar(c, it.batelada_padrao.dados, c['qtd'])
                c['massa_viennoiserie'] = deepcopy(it.batelada_padrao.dados)
            else:
                c['massa_legada'] = True
            continue
        _marcar(c, padrao, batimentos)
        distribuicao = []
        for destino in atual:
            rid = destino['receita_id']
            if rid == raiz:
                continue
            # Mostra retiradas diretas, sem repetir as gramas dos Danish que
            # já passaram pela linha intermediária "Massa de Danish".
            from app.services.previsao_producao import _subs_de
            ratio = sum(v for sid, v in _subs_de(rid, receitas) if sid == raiz)
            if not ratio:
                continue
            for i, cd in enumerate(destino['por_dia']):
                data_massa = dias[i] - timedelta(days=lead.get(raiz, 0))
                while not producao_permitida_no_dia(receitas[raiz], data_massa):
                    data_massa -= timedelta(days=1)
                if data_massa == dias[j] and quantidade_pendente(cd) > 0:
                    distribuicao.append({'nome': destino['nome'],
                        'receita_id': rid, 'data': dias[i].isoformat(),
                        'quantidade': quantidade_pendente(cd),
                        'massa_g': float(_d(quantidade_pendente(cd)) * _d(ratio) *
                                         _d(padrao['peso_bola_g']))})
        usado = _necessidade(atual, raiz, j, _d(padrao['peso_bola_g']))
        produtos = []
        for destino in base:
            rid = destino['receita_id']
            for i, cd in enumerate(destino['por_dia']):
                if cd['qtd'] > 0 and any(d == dias[j] for d, _ in
                        _caminhos(rid, raiz, dias[i], receitas, lead)):
                    produtos.append({'receita_id': rid, 'nome': destino['nome'],
                                     'data': dias[i].isoformat(), 'quantidade': cd['qtd']})
        c['massa_viennoiserie'] = {
            'distribuicao': distribuicao,
            'produtos': produtos,
            'residuo_g': float(max(Decimal(0), _d(padrao['massa_g']) * batimentos - usado)),
            'massa_g': padrao['massa_g'], 'peso_bola_g': padrao['peso_bola_g'],
        }
        rr['unidade_producao'] = 'batimentos'
    for (raiz, j), erro in bloqueados.items():
        rr = _linha(atual, raiz)
        if rr:
            rr['erro_batelada'] = erro
            rr['por_dia'][j].update(qtd=0, fornadas=None, batelada_bloqueada=erro)
    for rr in atual:
        rr['total'] = sum(c['qtd'] for c in rr['por_dia'])
    linhas[:] = atual
