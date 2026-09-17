"""Agenda bateladas inteiras por sabor, sem gravar ordens ou estoque.

A sobra calculada pertence ao MESMO sabor e só cobre necessidades posteriores.
Ordens já fechadas são lidas como estão: não se inventa estoque de uma batelada
que o padeiro não recebeu. A ficha congelada prevalece sobre edições futuras.
"""

import os
from collections import defaultdict
from math import ceil


def _marcar(celula, padrao, qtd):
    celula['qtd'] = int(qtd)
    celula['qtd_pendente'] = quantidade_pendente(celula)
    celula['batelada_padrao'] = {
        k: padrao[k] for k in
        ('farinha_g', 'unidades', 'rendimento_teorico', 'escala')}
    if 'subs' in padrao:
        celula['batelada_padrao']['subs'] = padrao['subs']
    for chave in ('tipo', 'unidade_producao', 'massa_g', 'peso_bola_g'):
        if chave in padrao:
            celula['batelada_padrao'][chave] = padrao[chave]
    celula['bateladas'] = ceil(int(qtd) / padrao['unidades']) if qtd else 0
    celula['fornadas'] = celula['bateladas'] or None
    celula['farinha_total_g'] = celula['bateladas'] * padrao['farinha_g']


def _contexto_ordens(dias, receitas, lead):
    from app.models import CronogramaOverride, PlanejamentoProducao
    from app.services.bateladas_paes import farinha_padrao_g
    from app.services.previsao_producao import ant_insumo
    from app.utils import hoje

    overrides = {(o.receita_id, o.data): int(o.qtd)
                 for o in CronogramaOverride.query.filter(
                     CronogramaOverride.data.in_(dias)).all()}
    planos = PlanejamentoProducao.query.filter(
        PlanejamentoProducao.data.in_(dias),
        PlanejamentoProducao.origem == 'cronograma').all()
    fixos, snapshots = {}, {}
    memo = {}
    hoje_d = hoje()
    alvos = {rid for rid, rec in receitas.items() if farinha_padrao_g(rec)}
    for plano in planos:
        for it in plano.itens:
            snapshot = getattr(it, 'batelada_padrao', None)
            if (snapshot and it.receita_id in receitas
                    and snapshot.dados.get('tipo') != 'massa_viennoiserie'):
                snapshots[it.receita_id, plano.data] = {
                    **snapshot.dados,
                    'produzido_confirmado': int(it.produzido_qtd or 0)}
                alvos.add(it.receita_id)
    for plano in planos:
        itens = {it.receita_id: it for it in plano.itens}
        for rid in alvos:
            it = itens.get(rid)
            snapshot = getattr(it, 'batelada_padrao', None) if it else None
            ant = (ant_insumo(rid, plano.data, receitas, lead, None, memo)
                   if os.environ.get('FREEZE_INSUMO', '1') != '0' else 0)
            if os.environ.get('FREEZE_INSUMO', '1') != '0' and snapshot:
                ant = max(ant, int(snapshot.dados.get('antecedencia_insumo_dias', 0)))
            fechado = (plano.enviado_ao_padeiro is not False and
                       (plano.data <= hoje_d or
                        (ant > 0 and (plano.data - hoje_d).days <= ant)))
            if fechado:
                fixos[rid, plano.data] = {
                    'alvo': int(it.qtd_alvo or 0) if it and not it.dispensada_em else 0,
                    'produzido': int(it.produzido_qtd or 0) if it else 0,
                }
    return overrides, fixos, snapshots


def normalizar_cronograma(receitas_out, dias, receitas, lead, piso=0,
                          pesos=None, *, contexto=None):
    """Completa o piso familiar em bateladas, não um lote para cada parcela.

    ``contexto`` torna a conta isolável em testes: (overrides, fixos, snapshots).
    Um override positivo pede produção explicitamente; zero bloqueia aquele
    sabor naquele dia, inclusive para o piso. O teto continua obrigatório nas
    sugestões automáticas; edição humana mantém a exceção já existente.
    """
    from app.services.bateladas_paes import farinha_padrao_g, padrao_receita
    from app.services.previsao_producao import producao_permitida_no_dia

    overrides, fixos, snapshots = (
        contexto if contexto is not None else _contexto_ordens(dias, receitas, lead))
    pesos = pesos or {}
    linhas = {rr['receita_id']: rr for rr in receitas_out}
    padroes = {}
    erros = {}
    rids_snapshot = {rid for rid, _dia in snapshots}
    for rid, rec in receitas.items():
        if rid not in rids_snapshot and not farinha_padrao_g(rec):
            continue
        rr = linhas.get(rid)
        if rr is None:
            continue
        try:
            padroes[rid] = padrao_receita(rec, resolver_estoque=False)
        except ValueError as exc:
            # A ficha viva só vale para novas ordens. Uma célula que já tem
            # snapshot continua usando-o, mesmo fora da janela de freeze.
            padroes[rid] = None
            erros[rid] = str(exc)
        rr.pop('piso_sourdough_aplicado', None)

    sobra = defaultdict(int)
    produzido = defaultdict(int)
    for i, dia in enumerate(dias):
        elegiveis = []
        for rid in padroes:
            rr, rec = linhas[rid], receitas[rid]
            c = rr['por_dia'][i]
            padrao = snapshots.get((rid, dia), padroes[rid])
            fixo = fixos.get((rid, dia))
            alvo_fixo = fixo.get('alvo', 0) if isinstance(fixo, dict) else (fixo or 0)
            confirmado = (fixo.get('produzido', 0) if isinstance(fixo, dict)
                          else (padrao or {}).get('produzido_confirmado', 0))
            c['produzido_confirmado'] = int(confirmado)
            manual = (rid, dia) in overrides
            c['batelada_manual'] = manual
            # Compatível com chamadas que ainda trouxeram o piso fracionado.
            necessidade = max(0, int(c['qtd'] or 0) -
                             (0 if manual else int(c.pop('piso_sourdough', 0))))
            c['necessidade_batelada'] = necessidade
            if (rid, dia) in fixos:
                c['qtd'] = max(confirmado, overrides.get((rid, dia), alvo_fixo))
                c['batelada_congelada'] = True
                c['alvo_enviado'] = alvo_fixo
                c['batelada_override_pendente'] = manual
                if (rid, dia) in snapshots:
                    lote = int(padrao['unidades'])
                    qtd = (ceil(c['qtd'] / lote) * lote
                           if manual and c['qtd'] > confirmado else c['qtd'])
                    _marcar(c, padrao, qtd)
                # Nunca credita a sobra de uma proposta que o envio automático
                # não executará. Mas uma ordem real menor que a necessidade
                # consome a sobra anterior, que não pode reaparecer amanhã.
                falta_real = max(0, necessidade - max(0, alvo_fixo - confirmado))
                sobra[rid] = max(0, sobra[rid] - falta_real)
                c['qtd_pendente'] = quantidade_pendente(c)
                produzido[rid] += c['qtd']
                continue
            if padrao is None:
                if rid in erros:
                    rr['erro_batelada'] = erros[rid]
                    c.update(qtd=0, fornadas=None, batelada_bloqueada=erros[rid])
                # Receita reclassificada não muda snapshots anteriores nem
                # ganha uma nova regra de farinha nos dias sem snapshot.
                continue
            lote = int(padrao['unidades'])
            if not producao_permitida_no_dia(rec, dia):
                _marcar(c, padrao, 0)
                continue
            if not manual:
                coberto = min(sobra[rid], necessidade)
                sobra[rid] -= coberto
                necessidade -= coberto
                c['coberto_sobra_batelada'] = coberto
            alvo = max(necessidade, confirmado) if manual else necessidade + confirmado
            qtd = (confirmado if confirmado > 0 and alvo <= confirmado else
                   ceil(alvo / lote) * lote if alvo else 0)
            teto = int(getattr(rec, 'producao_max_dia', 0) or 0)
            if teto > 0 and not manual and qtd > teto:
                qtd = (teto // lote) * lote
                c['batelada_bloqueada'] = (
                    f'O teto diário de {teto} unidades não comporta '
                    'todas as bateladas necessárias.')
                c['teto_aplicado'] = True
                rr['limitado_teto'] = True
            _marcar(c, padrao, qtd)
            # Override é um alvo de produção, não uma nova demanda de loja.
            # Só sua sobra de arredondamento pode cobrir o futuro.
            atendido = max(necessidade, confirmado) if manual else necessidade + confirmado
            sobra[rid] += max(0, qtd - atendido)
            produzido[rid] += qtd
            if (not manual and getattr(rec, 'sugerir_pedido_loja', True) is not False):
                elegiveis.append((rid, padrao, teto))

        atual = sum(linhas[rid]['por_dia'][i]['qtd'] for rid in padroes)
        while atual < piso and elegiveis:
            candidatos = [(rid, p, teto) for rid, p, teto in elegiveis
                          if teto <= 0 or
                          linhas[rid]['por_dia'][i]['qtd'] + p['unidades'] <= teto]
            if not candidatos:
                break
            # Menor cobertura ponderada histórica: alterna sabores sem
            # arredondar cinco parcelas pequenas para cinco lotes enormes.
            rid, padrao, _teto = min(
                candidatos,
                key=lambda v: (produzido[v[0]] /
                               max(float(pesos.get((v[0], dia), 1) or 1), 1), v[0]))
            rr = linhas[rid]
            c = rr['por_dia'][i]
            add = padrao['unidades']
            _marcar(c, padrao, c['qtd'] + add)
            c['piso_sourdough'] = int(c.get('piso_sourdough', 0)) + add
            rr['piso_sourdough_aplicado'] = True
            sobra[rid] += add
            produzido[rid] += add
            atual += add
        for rid in padroes:
            linhas[rid]['por_dia'][i]['estoque_virtual_batelada'] = sobra[rid]
    for rr in receitas_out:
        rr['total'] = sum(int(c['qtd'] or 0) for c in rr['por_dia'])


def quantidade_pendente(celula):
    """Confirmado já está no estoque físico; não entra uma segunda vez."""
    alvo = (celula.get('alvo_enviado', celula['qtd'])
            if celula.get('batelada_congelada') else celula['qtd'])
    return max(0, int(alvo or 0) -
               int(celula.get('produzido_confirmado', 0) or 0))


def unidades_equivalentes(celula):
    """Unidades teóricas para BOM: 83 pães podem carregar 83,75 de receita."""
    padrao = celula.get('batelada_padrao')
    qtd = quantidade_pendente(celula)
    if not padrao:
        return qtd
    return qtd / padrao['unidades'] * padrao['rendimento_teorico']


def consumo_insumo(celula, sub_id, ratio):
    """Snapshot também congela o BOM, mesmo se a ficha for editada depois."""
    padrao = celula.get('batelada_padrao')
    if padrao and 'subs' in padrao:
        por_batelada = sum(float(s['quantidade']) for s in padrao['subs']
                          if s['id'] == sub_id)
        return quantidade_pendente(celula) / padrao['unidades'] * por_batelada
    return unidades_equivalentes(celula) * ratio


def adicionar_demanda_insumo(linha, extras, rec):
    """Um pão também usado em outra ficha mantém lotes inteiros no MRP.

    A sobra já reservada a demandas futuras não é contada duas vezes. Mantém
    o piso previamente agendado; pode ampliar somente dias ainda abertos.
    """
    carry = 0
    novo = []
    for c, adicional in zip(linha['por_dia'], extras):
        padrao = c.get('batelada_padrao')
        base = int(c['qtd'] or 0)
        necessidade = int(c.get('necessidade_batelada', base)) + adicional
        bloqueada = (c.get('batelada_congelada') or
                     (c.get('batelada_manual') and base == 0))
        if not padrao or bloqueada:
            novo.append(base if bloqueada else base + adicional)
            if bloqueada and adicional:
                c['batelada_bloqueada'] = (
                    'A demanda adicional de insumos não cabe na ordem fechada '
                    'ou no bloqueio manual deste dia.')
            carry = 0
            continue
        lote = padrao['unidades']
        confirmado = int(c.get('produzido_confirmado', 0) or 0)
        alvo = max(base, ceil((confirmado + max(0, necessidade - carry)) / lote) * lote)
        teto = int(getattr(rec, 'producao_max_dia', 0) or 0)
        if teto > 0 and alvo > teto and not c.get('batelada_manual'):
            alvo = max(base, (teto // lote) * lote)
            c['batelada_bloqueada'] = 'O teto diário impede completar a demanda de insumos.'
        carry = max(0, carry + alvo - confirmado - necessidade)
        _marcar(c, padrao, alvo)
        novo.append(alvo)
    return novo
