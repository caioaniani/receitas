import csv
import io
from datetime import date, timedelta

from flask import render_template, Response, request
from flask_login import login_required

from app.blueprints.relatorios import relatorios_bp
from app.decorators import admin_required
from app.extensions import db
from app.models import MateriaPrima, Receita, ReceitaIngrediente, Loja
from app.services.custos import calcular_custos_receitas
from app.services.previsao_demanda import prever_demanda, prever_semana


@relatorios_bp.route('/custos')
@login_required
@admin_required
def custos():
    resultado = calcular_custos_receitas()
    custos_map = resultado.get('custos', {})
    receitas = Receita.query.order_by(Receita.categoria, Receita.nome).all()

    dados = []
    for r in receitas:
        custo_unit = custos_map.get(r.nome, 0)
        preco_atac = r.preco_venda or 0
        preco_loja = r.preco_loja or 0
        preco_site = r.preco_site or 0

        margem_atac = ((preco_atac - custo_unit) / preco_atac * 100) if preco_atac else 0
        margem_loja = ((preco_loja - custo_unit) / preco_loja * 100) if preco_loja else 0
        margem_site = ((preco_site - custo_unit) / preco_site * 100) if preco_site else 0

        dados.append({
            'nome': r.nome,
            'categoria': r.categoria or 'Outros',
            'custo_unit': custo_unit,
            'preco_atac': preco_atac,
            'preco_loja': preco_loja,
            'preco_site': preco_site,
            'margem_atac': margem_atac,
            'margem_loja': margem_loja,
            'margem_site': margem_site,
        })

    return render_template('relatorios/custos.html', dados=dados)


@relatorios_bp.route('/custos/csv')
@login_required
@admin_required
def custos_csv():
    resultado = calcular_custos_receitas()
    custos_map = resultado.get('custos', {})
    receitas = Receita.query.order_by(Receita.categoria, Receita.nome).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Categoria', 'Receita', 'Custo Unit (R$)', 'Preço Atacado', 'Margem Atac %',
                     'Preço Loja', 'Margem Loja %', 'Preço Site', 'Margem Site %'])

    for r in receitas:
        custo = custos_map.get(r.nome, 0)
        pa = r.preco_venda or 0
        pl = r.preco_loja or 0
        ps = r.preco_site or 0
        ma = ((pa - custo) / pa * 100) if pa else 0
        ml = ((pl - custo) / pl * 100) if pl else 0
        ms = ((ps - custo) / ps * 100) if ps else 0
        writer.writerow([r.categoria or 'Outros', r.nome,
                         f'{custo:.2f}', f'{pa:.2f}', f'{ma:.1f}',
                         f'{pl:.2f}', f'{ml:.1f}', f'{ps:.2f}', f'{ms:.1f}'])

    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=relatorio_custos.csv'}
    )


@relatorios_bp.route('/ingredientes')
@login_required
@admin_required
def ingredientes():
    materias = (
        db.session.query(
            MateriaPrima,
            db.func.count(ReceitaIngrediente.id).label('uso_count')
        )
        .outerjoin(ReceitaIngrediente, MateriaPrima.nome == ReceitaIngrediente.ingrediente_nome)
        .group_by(MateriaPrima.id)
        .order_by(MateriaPrima.custo_por_kg.desc())
        .all()
    )
    return render_template('relatorios/ingredientes.html', materias=materias)


@relatorios_bp.route('/previsao')
@login_required
@admin_required
def previsao():
    """Previsao de demanda diaria por (item × loja) com base em media de
    venda no mesmo dia-da-semana nas ultimas 8 semanas. So vendas PDV/Seru."""
    lojas = Loja.query.filter_by(ativa=True).order_by(Loja.nome).all()
    # Filtra Industria (nao tem PDV)
    lojas = [l for l in lojas if l.nome.lower() != 'industria']
    loja_id = request.args.get('loja', type=int)
    if not loja_id and lojas:
        loja_id = lojas[0].id
    data_param = request.args.get('data')
    if data_param:
        try:
            data_inicio = date.fromisoformat(data_param)
        except ValueError:
            data_inicio = date.today() + timedelta(days=1)
    else:
        data_inicio = date.today() + timedelta(days=1)

    semana = prever_semana(loja_id, data_inicio) if loja_id else {}
    return render_template('relatorios/previsao.html', lojas=lojas,
                           loja_id=loja_id, data_inicio=data_inicio,
                           semana=semana)
