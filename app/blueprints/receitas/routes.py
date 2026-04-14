import json

from flask import render_template, redirect, url_for, flash, request

from app.blueprints.receitas import receitas_bp
from app.extensions import db
from app.models import MateriaPrima, Receita, ReceitaIngrediente


@receitas_bp.route('/<int:id>')
def ficha(id):
    receita = Receita.query.get_or_404(id)
    mp_dict = {mp.nome: mp for mp in MateriaPrima.query.all()}

    # Custos e pesos das receitas (para sub-receitas)
    receita_custos, receita_pesos = _calcular_custos_receitas_simples()

    return render_template('receitas/ficha.html', receita=receita, mp_dict=mp_dict,
                           receita_custos_json=json.dumps(receita_custos, ensure_ascii=False),
                           receita_pesos_json=json.dumps(receita_pesos, ensure_ascii=False))


def _calcular_custos_receitas_simples():
    """Calcula custo unitário e peso unitário de cada receita (para uso como sub-receita)."""
    receitas = Receita.query.all()
    mp_dict = {mp.nome: mp.custo_por_kg for mp in MateriaPrima.query.all()}

    custos = {}
    pesos = {}

    # Múltiplas passadas para resolver dependências entre receitas
    remaining = list(receitas)
    for _ in range(5):
        still_remaining = []
        for r in remaining:
            can_calc = True
            custo_total = 0
            sum_pct = 0

            for ing in r.ingredientes:
                tipo = ing.tipo or 'mp'
                if tipo == 'receita':
                    if ing.ingrediente_nome not in custos:
                        can_calc = False
                        break
                    custo_total += custos[ing.ingrediente_nome] * ing.porcentagem
                else:
                    sum_pct += ing.porcentagem
                    qtd_g = r.peso_base * ing.porcentagem / 100
                    custo_kg = mp_dict.get(ing.ingrediente_nome, 0)
                    custo_total += qtd_g / 1000 * custo_kg

            if not can_calc:
                still_remaining.append(r)
                continue

            total_qtd = r.peso_base * sum_pct / 100
            perda = r.perda_percentual or 0
            peso_pos_perda = total_qtd * (1 - perda / 100)

            if r.peso_unitario and r.peso_unitario > 0 and peso_pos_perda > 0:
                rendimento = int(peso_pos_perda / r.peso_unitario)
            else:
                rendimento = int(r.rendimento_qtd)

            embalagem = r.custo_embalagem or 0
            custo_un = (custo_total / rendimento + embalagem) if rendimento > 0 else 0
            custos[r.nome] = custo_un
            pesos[r.nome] = r.peso_unitario or 0

        remaining = still_remaining
        if not remaining:
            break

    return custos, pesos


@receitas_bp.route('/<int:id>/salvar', methods=['POST'])
def salvar(id):
    receita = Receita.query.get_or_404(id)

    receita.nome = request.form.get('nome', receita.nome).strip()
    receita.categoria = request.form.get('categoria', '').strip() or None
    preco = request.form.get('preco_venda', '').replace(',', '.').strip()
    receita.preco_venda = float(preco) if preco else None
    preco_loja = request.form.get('preco_loja', '').replace(',', '.').strip()
    receita.preco_loja = float(preco_loja) if preco_loja else None
    preco_site = request.form.get('preco_site', '').replace(',', '.').strip()
    receita.preco_site = float(preco_site) if preco_site else None
    receita.rendimento_qtd = float(request.form.get('rendimento_qtd', '1').replace(',', '.'))
    receita.rendimento_unidade = request.form.get('rendimento_unidade', 'unidades').strip()
    receita.peso_base = float(request.form.get('peso_base', '1000').replace(',', '.'))
    peso_un = request.form.get('peso_unitario', '').replace(',', '.').strip()
    receita.peso_unitario = float(peso_un) if peso_un else None
    perda = request.form.get('perda_percentual', '').replace(',', '.').strip()
    receita.perda_percentual = float(perda) if perda else 0
    emb = request.form.get('custo_embalagem', '').replace(',', '.').strip()
    receita.custo_embalagem = float(emb) if emb else 0

    # Atualiza ingredientes
    ReceitaIngrediente.query.filter_by(receita_id=receita.id).delete()

    tipos = request.form.getlist('ingrediente_tipo[]')
    nomes = request.form.getlist('ingrediente_nome[]')
    porcentagens = request.form.getlist('porcentagem[]')
    bases = request.form.getlist('eh_base[]')
    notas = request.form.getlist('nota[]')

    for i in range(len(nomes)):
        nome = nomes[i].strip()
        pct_str = porcentagens[i].replace(',', '.').strip()
        if not nome or not pct_str:
            continue
        tipo = tipos[i] if i < len(tipos) else 'mp'
        ing = ReceitaIngrediente(
            receita_id=receita.id,
            tipo=tipo,
            ingrediente_nome=nome,
            porcentagem=float(pct_str),
            eh_base=(bases[i] == '1') if i < len(bases) else False,
            nota=notas[i].strip() if i < len(notas) else None,
        )
        db.session.add(ing)

    db.session.commit()
    flash('Ficha salva com sucesso!', 'success')
    return redirect(url_for('receitas.ficha', id=receita.id))


@receitas_bp.route('/nova', methods=['POST'])
def nova():
    receita = Receita(
        nome='Novo Produto',
        categoria='',
        rendimento_qtd=1,
        rendimento_unidade='unidades',
        peso_base=1000,
    )
    db.session.add(receita)
    db.session.commit()
    flash('Novo produto criado!', 'success')
    return redirect(url_for('receitas.ficha', id=receita.id))


@receitas_bp.route('/<int:id>/duplicar', methods=['POST'])
def duplicar(id):
    original = Receita.query.get_or_404(id)
    copia = Receita(
        nome=f'Cópia de {original.nome}',
        categoria=original.categoria,
        preco_venda=original.preco_venda,
        preco_loja=original.preco_loja,
        preco_site=original.preco_site,
        rendimento_qtd=original.rendimento_qtd,
        rendimento_unidade=original.rendimento_unidade,
        peso_base=original.peso_base,
        peso_unitario=original.peso_unitario,
        perda_percentual=original.perda_percentual,
        custo_embalagem=original.custo_embalagem,
    )
    db.session.add(copia)
    db.session.flush()

    for ing in original.ingredientes:
        novo_ing = ReceitaIngrediente(
            receita_id=copia.id,
            tipo=ing.tipo or 'mp',
            ingrediente_nome=ing.ingrediente_nome,
            porcentagem=ing.porcentagem,
            eh_base=ing.eh_base,
            nota=ing.nota,
        )
        db.session.add(novo_ing)

    db.session.commit()
    flash(f'Receita duplicada: "{copia.nome}"', 'success')
    return redirect(url_for('receitas.ficha', id=copia.id))


@receitas_bp.route('/<int:id>/excluir', methods=['POST'])
def excluir(id):
    receita = Receita.query.get_or_404(id)
    nome = receita.nome
    db.session.delete(receita)
    db.session.commit()
    flash(f'"{nome}" excluído com sucesso!', 'success')
    return redirect(url_for('materias_primas.banco'))
