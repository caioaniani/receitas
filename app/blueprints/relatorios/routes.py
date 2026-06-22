import csv
import io
from datetime import date, timedelta

from flask import Response, render_template, request
from flask_login import login_required

from app.blueprints.relatorios import relatorios_bp
from app.decorators import admin_required
from app.extensions import db
from app.models import Loja, MateriaPrima, Receita, ReceitaIngrediente
from app.services.custos import calcular_custos_receitas
from app.services.previsao_demanda import prever_semana
from app.utils import hoje as hoje_brt


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


@relatorios_bp.route('/dashboards')
@login_required
@admin_required
def dashboards():
    """Painel executivo com gráficos interativos: vendas, margem, desperdício,
    estoque, pedidos. Dados agregados em queries leves; tudo renderizado via
    Chart.js no client (zero dependência de Metabase/Grafana)."""
    return render_template('relatorios/dashboards.html')


@relatorios_bp.route('/dashboards/api/vendas-por-dia')
@login_required
@admin_required
def api_vendas_por_dia():
    """Quantidade de pedidos por dia nos últimos 30 dias.
    Soma Seru (PDV) + loja própria (site, PedidoOnline) + pedidos manuais."""

    from sqlalchemy import func

    from app.models import PedidoOnline, SeruPedidoProcessado, VendaManualLoja
    fim = hoje_brt()
    ini = fim - timedelta(days=30)

    # Seru: 1 venda por seru_pedido_id, na data processado_em
    serus = (db.session.query(
            func.date(SeruPedidoProcessado.processado_em).label('dia'),
            func.count(SeruPedidoProcessado.seru_pedido_id).label('n'))
        .filter(SeruPedidoProcessado.processado_em >= ini,
                SeruPedidoProcessado.cancelado_em.is_(None))
        .group_by('dia').all())

    # Vendas manuais
    manuais = (db.session.query(
            VendaManualLoja.data_venda.label('dia'),
            func.count(VendaManualLoja.id).label('n'))
        .filter(VendaManualLoja.data_venda >= ini)
        .group_by(VendaManualLoja.data_venda).all())

    # Loja propria (PedidoOnline): pedidos pagos, por data de pagamento.
    onlines = (db.session.query(
            func.date(PedidoOnline.pago_em).label('dia'),
            func.count(PedidoOnline.id).label('n'))
        .filter(PedidoOnline.pago_em >= ini,
                PedidoOnline.status != 'cancelado')
        .group_by('dia').all())

    por_dia = {}
    for s in serus:
        d = str(s.dia) if not hasattr(s.dia, 'isoformat') else s.dia.isoformat()
        por_dia[d] = por_dia.get(d, 0) + s.n
    for m in manuais:
        d = m.dia.isoformat()
        por_dia[d] = por_dia.get(d, 0) + m.n
    for o in onlines:
        d = str(o.dia) if not hasattr(o.dia, 'isoformat') else o.dia.isoformat()
        por_dia[d] = por_dia.get(d, 0) + o.n

    # Preenche dias zerados
    labels = []
    valores = []
    d = ini
    while d <= fim:
        labels.append(d.strftime('%d/%m'))
        valores.append(por_dia.get(d.isoformat(), 0))
        d += timedelta(days=1)

    return {'labels': labels, 'valores': valores}


@relatorios_bp.route('/dashboards/api/margem-categoria')
@login_required
@admin_required
def api_margem_categoria():
    """Margem média por categoria de receita (preço atacado vs custo)."""
    from collections import defaultdict
    resultado = calcular_custos_receitas()
    custos_map = resultado.get('custos', {})
    receitas = Receita.query.all()
    cats = defaultdict(list)
    for r in receitas:
        if not r.preco_venda or r.preco_venda <= 0:
            continue
        custo = custos_map.get(r.nome, 0)
        margem = (r.preco_venda - custo) / r.preco_venda * 100
        cat = r.categoria or 'Outros'
        cats[cat].append(margem)
    labels = sorted(cats.keys())
    valores = [round(sum(cats[c]) / len(cats[c]), 1) for c in labels]
    return {'labels': labels, 'valores': valores}


@relatorios_bp.route('/dashboards/api/desperdicio')
@login_required
@admin_required
def api_desperdicio():
    """Desperdício dos últimos 30 dias agrupado por motivo."""
    from sqlalchemy import func

    from app.models import Desperdicio
    fim = hoje_brt()
    ini = fim - timedelta(days=30)
    rows = (db.session.query(
            Desperdicio.motivo,
            func.sum(Desperdicio.quantidade).label('qtd'))
        .filter(Desperdicio.data >= ini)
        .group_by(Desperdicio.motivo).all())
    labels = [r.motivo or 'sem motivo' for r in rows]
    valores = [int(r.qtd or 0) for r in rows]
    return {'labels': labels, 'valores': valores}


@relatorios_bp.route('/dashboards/api/top-receitas')
@login_required
@admin_required
def api_top_receitas():
    """Top 10 receitas mais vendidas (vendas manuais) nos últimos 30 dias."""
    from sqlalchemy import func

    from app.models import Receita, VendaManualLoja
    fim = hoje_brt()
    ini = fim - timedelta(days=30)

    # Vendas manuais (receita_id direto na tabela)
    rows = (db.session.query(
            Receita.nome,
            func.sum(VendaManualLoja.quantidade).label('qtd'))
        .join(Receita, VendaManualLoja.receita_id == Receita.id)
        .filter(VendaManualLoja.data_venda >= ini)
        .group_by(Receita.nome)
        .order_by(func.sum(VendaManualLoja.quantidade).desc())
        .limit(10).all())

    return {
        'labels': [r[0] for r in rows],
        'valores': [int(r[1] or 0) for r in rows],
    }


@relatorios_bp.route('/dashboards/api/estoque-baixo')
@login_required
@admin_required
def api_estoque_baixo():
    """Itens com estoque abaixo do mínimo nas lojas."""
    from sqlalchemy import func

    from app.models import EstoqueLoja, Loja
    # Conta por loja itens com estoque <= 5 (ad hoc threshold; pode virar setting)
    rows = (db.session.query(
            Loja.nome,
            func.count(EstoqueLoja.id).label('n'))
        .join(EstoqueLoja, EstoqueLoja.loja_id == Loja.id)
        .filter(EstoqueLoja.quantidade <= 5,
                EstoqueLoja.quantidade >= 0)
        .group_by(Loja.nome).all())
    labels = [r.nome for r in rows]
    valores = [r.n for r in rows]
    return {'labels': labels, 'valores': valores}


@relatorios_bp.route('/previsao')
@login_required
@admin_required
def previsao():
    """Previsao de demanda diaria por (item × loja) com base em media de
    venda no mesmo dia-da-semana nas ultimas 8 semanas. So vendas PDV/Seru."""
    lojas = (Loja.query
             .filter(Loja.ativa.is_(True), Loja.nome != 'Industria')
             .order_by(Loja.nome).all())
    loja_id = request.args.get('loja', type=int)
    if not loja_id and lojas:
        loja_id = lojas[0].id
    data_param = request.args.get('data')
    if data_param:
        try:
            data_inicio = date.fromisoformat(data_param)
        except ValueError:
            data_inicio = hoje_brt() + timedelta(days=1)
    else:
        data_inicio = hoje_brt() + timedelta(days=1)

    semana = prever_semana(loja_id, data_inicio) if loja_id else {}
    return render_template('relatorios/previsao.html', lojas=lojas,
                           loja_id=loja_id, data_inicio=data_inicio,
                           semana=semana)
