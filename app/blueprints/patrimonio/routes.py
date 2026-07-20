"""Patrimônio — inventário de móveis e equipamentos com etiquetas QR.

Pedido do dono (20/07/2026). Três gestos:
1. Admin cadastra os ativos (um a um ou colando uma lista) e imprime as
   etiquetas QR (`/patrimonio/etiquetas.pdf`) pra colar em cada item.
2. QUALQUER funcionário logado escaneia a etiqueta com a câmera do celular
   → `/patrimonio/<id>/conferir` → confirma "está aqui" (ok ou problema).
3. Admin acompanha na lista: última conferência por ativo e o que ninguém
   achou desde uma data (o relatório do inventário).

Conferir NÃO altera o cadastro (nem local nem situação) — divergência de
local vira aviso na lista; mover/baixar é gesto do admin.
"""
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from flask import (
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from app.blueprints.patrimonio import patrimonio_bp
from app.decorators import admin_required
from app.extensions import db
from app.models import Ativo, AtivoConferencia, Loja
from app.utils import hoje

SITUACOES = ('em_uso', 'manutencao', 'baixado')


def _parse_loja_id(bruto):
    """'' ou 'ind' = indústria (None); senão id de Loja válido ou None."""
    bruto = (bruto or '').strip()
    if bruto in ('', 'ind', 'industria'):
        return None
    try:
        lid = int(bruto)
    except ValueError:
        return None
    return lid if db.session.get(Loja, lid) else None


def _parse_valor(bruto):
    """Dinheiro pt-BR ('1.234,56') → Decimal ou None."""
    bruto = (bruto or '').strip()
    if not bruto:
        return None
    try:
        return Decimal(bruto.replace('.', '').replace(',', '.'))
    except InvalidOperation:
        return None


def _parse_data(bruto):
    try:
        return datetime.strptime((bruto or '').strip(), '%Y-%m-%d').date()
    except ValueError:
        return None


def _ultimas_conferencias(ativo_ids):
    """{ativo_id: AtivoConferencia mais recente} — 2 queries, sem N+1."""
    if not ativo_ids:
        return {}
    from sqlalchemy import func
    pares = dict(db.session.query(
        AtivoConferencia.ativo_id, func.max(AtivoConferencia.momento),
    ).filter(AtivoConferencia.ativo_id.in_(ativo_ids))
     .group_by(AtivoConferencia.ativo_id).all())
    if not pares:
        return {}
    ultimas = {}
    for c in (AtivoConferencia.query
              .filter(AtivoConferencia.ativo_id.in_(list(pares)))
              .order_by(AtivoConferencia.momento).all()):
        ultimas[c.ativo_id] = c      # a última sobrescreve (ordem asc)
    return ultimas


@patrimonio_bp.route('/')
@login_required
@admin_required
def index():
    """Lista do patrimônio + relatório do inventário (não conferidos)."""
    f_loja = (request.args.get('loja') or '').strip()
    f_cat = (request.args.get('categoria') or '').strip()
    f_sit = (request.args.get('situacao') or 'ativos').strip()
    f_busca = (request.args.get('busca') or '').strip()
    desde = _parse_data(request.args.get('desde')) or (hoje() - timedelta(days=30))

    q = Ativo.query
    if f_loja == 'ind':
        q = q.filter(Ativo.loja_id.is_(None))
    elif f_loja:
        try:
            q = q.filter(Ativo.loja_id == int(f_loja))
        except ValueError:
            pass
    if f_cat:
        q = q.filter(Ativo.categoria == f_cat)
    if f_sit == 'ativos':
        q = q.filter(Ativo.situacao != 'baixado')
    elif f_sit in SITUACOES:
        q = q.filter(Ativo.situacao == f_sit)
    if f_busca:
        q = q.filter(Ativo.nome.ilike(f'%{f_busca}%'))
    ativos = q.order_by(Ativo.loja_id.nullsfirst(), Ativo.nome).all() \
        if hasattr(Ativo.loja_id, 'nullsfirst') else q.order_by(Ativo.nome).all()

    ultimas = _ultimas_conferencias([a.id for a in ativos])
    corte = datetime.combine(desde, datetime.min.time())
    linhas = []
    nao_conferidos = 0
    for a in ativos:
        ult = ultimas.get(a.id)
        conferido = ult is not None and ult.momento >= corte
        if not conferido and a.situacao != 'baixado':
            nao_conferidos += 1
        divergente = (ult is not None
                      and (ult.loja_id_visto or None) != (a.loja_id or None))
        linhas.append({'ativo': a, 'ultima': ult, 'conferido': conferido,
                       'local_divergente': divergente})

    categorias = sorted({a for (a,) in db.session.query(Ativo.categoria)
                         .filter(Ativo.categoria.isnot(None),
                                 Ativo.categoria != '').distinct().all()})
    lojas = Loja.query.filter_by(ativa=True).order_by(Loja.nome).all()
    return render_template(
        'patrimonio/lista.html', linhas=linhas, lojas=lojas,
        categorias=categorias, f_loja=f_loja, f_cat=f_cat, f_sit=f_sit,
        f_busca=f_busca, desde=desde, nao_conferidos=nao_conferidos)


@patrimonio_bp.route('/novo', methods=['POST'])
@login_required
@admin_required
def novo():
    """Cria 1 ativo (campos completos) OU vários (textarea, um nome por
    linha — categoria/local do form valem pra todos)."""
    loja_id = _parse_loja_id(request.form.get('loja_id'))
    categoria = (request.form.get('categoria') or '').strip() or None
    local_det = (request.form.get('local_detalhe') or '').strip() or None
    lote = (request.form.get('nomes_lote') or '').strip()
    criados = 0
    if lote:
        for linha in lote.splitlines():
            nome = linha.strip()
            if not nome:
                continue
            db.session.add(Ativo(nome=nome[:200], categoria=categoria,
                                 loja_id=loja_id, local_detalhe=local_det,
                                 criado_por_id=current_user.id))
            criados += 1
    else:
        nome = (request.form.get('nome') or '').strip()
        if not nome:
            flash('Informe o nome do ativo (ou cole a lista).', 'warning')
            return redirect(url_for('patrimonio.index'))
        db.session.add(Ativo(
            nome=nome[:200], categoria=categoria, loja_id=loja_id,
            local_detalhe=local_det,
            numero_serie=(request.form.get('numero_serie') or '').strip() or None,
            valor_aquisicao=_parse_valor(request.form.get('valor_aquisicao')),
            adquirido_em=_parse_data(request.form.get('adquirido_em')),
            observacao=(request.form.get('observacao') or '').strip() or None,
            criado_por_id=current_user.id))
        criados = 1
    db.session.commit()
    flash(f'{criados} ativo(s) cadastrado(s). Imprima as etiquetas novas '
          'no botão "Etiquetas PDF".', 'success')
    return redirect(url_for('patrimonio.index'))


@patrimonio_bp.route('/<int:id>/editar', methods=['POST'])
@login_required
@admin_required
def editar(id):
    a = Ativo.query.get_or_404(id)
    nome = (request.form.get('nome') or '').strip()
    if nome:
        a.nome = nome[:200]
    a.categoria = (request.form.get('categoria') or '').strip() or None
    a.loja_id = _parse_loja_id(request.form.get('loja_id'))
    a.local_detalhe = (request.form.get('local_detalhe') or '').strip() or None
    a.numero_serie = (request.form.get('numero_serie') or '').strip() or None
    a.valor_aquisicao = _parse_valor(request.form.get('valor_aquisicao'))
    a.adquirido_em = _parse_data(request.form.get('adquirido_em'))
    a.observacao = (request.form.get('observacao') or '').strip() or None
    db.session.commit()
    flash(f'Ativo {a.codigo} atualizado.', 'success')
    return redirect(url_for('patrimonio.index'))


@patrimonio_bp.route('/<int:id>/situacao', methods=['POST'])
@login_required
@admin_required
def situacao(id):
    """em_uso ↔ manutencao ↔ baixado. Baixar tira das etiquetas e do
    inventário; nunca apaga (histórico de patrimônio fica)."""
    from app.utils import agora
    a = Ativo.query.get_or_404(id)
    nova = (request.form.get('situacao') or '').strip()
    if nova not in SITUACOES:
        flash('Situação inválida.', 'warning')
        return redirect(url_for('patrimonio.index'))
    a.situacao = nova
    a.baixado_em = agora() if nova == 'baixado' else None
    db.session.commit()
    flash(f'Ativo {a.codigo} agora está "{nova}".', 'success')
    return redirect(url_for('patrimonio.index'))


@patrimonio_bp.route('/etiquetas.pdf')
@login_required
@admin_required
def etiquetas_pdf():
    """PDF de etiquetas QR (3×7 por A4). Filtros: loja/categoria/ids.
    Baixados ficam FORA sempre — etiqueta de ativo morto só confunde."""
    from app.services.patrimonio_pdf import gerar_etiquetas_pdf

    q = Ativo.query.filter(Ativo.situacao != 'baixado')
    f_loja = (request.args.get('loja') or '').strip()
    if f_loja == 'ind':
        q = q.filter(Ativo.loja_id.is_(None))
    elif f_loja:
        try:
            q = q.filter(Ativo.loja_id == int(f_loja))
        except ValueError:
            pass
    f_cat = (request.args.get('categoria') or '').strip()
    if f_cat:
        q = q.filter(Ativo.categoria == f_cat)
    ids = (request.args.get('ids') or '').strip()
    if ids:
        try:
            q = q.filter(Ativo.id.in_([int(x) for x in ids.split(',')]))
        except ValueError:
            pass
    ativos = q.order_by(Ativo.loja_id, Ativo.nome).all()
    base = (current_app.config.get('APP_BASE_URL') or '').rstrip('/')
    pdf = gerar_etiquetas_pdf(ativos, base)
    return (pdf, 200, {
        'Content-Type': 'application/pdf',
        'Content-Disposition': 'inline; filename="etiquetas-patrimonio.pdf"',
    })


@patrimonio_bp.route('/<int:id>/conferir', methods=['GET', 'POST'])
@login_required
def conferir(id):
    """A página do QR — QUALQUER funcionário logado confere (o inventário é
    de todo mundo; cadastro/edição seguem admin)."""
    a = Ativo.query.get_or_404(id)
    if request.method == 'POST':
        estado = (request.form.get('estado') or 'ok').strip()
        if estado not in ('ok', 'problema'):
            estado = 'ok'
        obs = (request.form.get('observacao') or '').strip() or None
        loja_vista = _parse_loja_id(request.form.get('loja_id_visto'))
        db.session.add(AtivoConferencia(
            ativo_id=a.id, usuario_id=current_user.id,
            loja_id_visto=loja_vista, estado=estado,
            observacao=obs[:500] if obs else None))
        db.session.commit()
        flash('Conferência registrada — obrigado!', 'success')
        return redirect(url_for('patrimonio.conferir', id=a.id))
    ultima = (AtivoConferencia.query.filter_by(ativo_id=a.id)
              .order_by(AtivoConferencia.momento.desc()).first())
    lojas = Loja.query.filter_by(ativa=True).order_by(Loja.nome).all()
    return render_template('patrimonio/conferir.html', ativo=a,
                           ultima=ultima, lojas=lojas)
