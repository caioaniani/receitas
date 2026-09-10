"""Medições do preparo por lote: leitura histórica e escrita auditada."""
import csv
import io
from datetime import date, timedelta
from uuid import uuid4

from flask import Response, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy.orm import selectinload

from app.blueprints.padeiro import padeiro_bp
from app.decorators import padeiro_required
from app.extensions import db
from app.models import MassaBase, PlanejamentoItem, ProducaoDiarioLote
from app.services import producao_diario as diario
from app.utils import agora, hoje

CAMPOS_MEDIDAS = [
    ('farinha_kg', 'Farinha utilizada', 'kg'),
    ('agua_kg', 'Água adicionada', 'kg'),
    ('massa_kg', 'Massa final do lote', 'kg'),
    ('temperatura_agua', 'Temperatura da água', '°C'),
    ('temperatura_ambiente', 'Temperatura do ambiente', '°C'),
    ('temperatura_farinha', 'Temperatura da farinha', '°C'),
    ('temperatura_massa', 'Temperatura da massa após bater', '°C'),
]


def _data(valor, padrao=None):
    if not valor and padrao is not None:
        return padrao
    try:
        return date.fromisoformat(valor or '')
    except ValueError:
        abort(400, 'Informe uma data válida.')


def _lote(id):
    return db.get_or_404(ProducaoDiarioLote, id)


def _sugestoes(lote):
    # Sugestões não criam registros e não transformam a ficha em observação.
    receitas = []
    if lote.tipo_origem == 'item':
        item = db.session.get(PlanejamentoItem, lote.origem_id)
        if item and item.planejamento_id == lote.planejamento_id and item.receita:
            receitas = [item.receita]
    else:
        base = db.session.get(MassaBase, lote.origem_id)
        if base:
            receitas = [item.receita for item in base.itens if item.receita]
    nomes = list(dict.fromkeys(e.nome for rec in receitas for e in rec.etapas))
    # Esses nomes permitem separar as duas medições presentes no caderno.
    if any('sourdough' in rec.nome.lower() for rec in receitas) or 'sourdough' in lote.nome.lower():
        nomes += ['Batimento — velocidade 1', 'Batimento — velocidade 2']
    return list(dict.fromkeys(nomes))


def _detalhe(lote, status=200):
    return render_template(
        'padeiro/diario_lote.html', lote=lote,
        agora_local=agora().strftime('%Y-%m-%dT%H:%M'),
        sugestoes=_sugestoes(lote), campos_medidas=CAMPOS_MEDIDAS,
        rascunho=request.form if request.method == 'POST' and status != 200 else {},
        erro_form=request.endpoint if status != 200 else None,
    ), status


def _erro_lote(id, exc):
    db.session.rollback()
    flash(str(exc), 'danger')
    return _detalhe(_lote(id), 409 if isinstance(exc, diario.DiarioConflito) else 400)


def _indice(dia, status=200):
    return render_template(
        'padeiro/diario_index.html', dia=dia,
        origens=diario.origens_do_dia(dia),
        abertos=ProducaoDiarioLote.query.filter_by(status='aberto').order_by(
            ProducaoDiarioLote.data_producao, ProducaoDiarioLote.id).all(),
        lotes=ProducaoDiarioLote.query.filter_by(data_producao=dia, status='concluido')
            .order_by(ProducaoDiarioLote.id.desc()).all(),
        chave=request.form.get('chave') or str(uuid4()),
        pre_origem=request.form.get('origem') or request.args.get('origem', ''),
        data_registro=hoje(), rascunho=request.form,
    ), status


@padeiro_bp.route('/diario')
@login_required
@padeiro_required
def diario_index():
    return _indice(_data(request.args.get('data'), hoje()))


@padeiro_bp.route('/diario/lotes', methods=['POST'])
@login_required
@padeiro_required
def diario_criar():
    dia = _data(request.form.get('data_ordem'))
    try:
        tipo, origem = request.form.get('origem', '').split(':', 1)
        lote = diario.criar_lote(
            tipo, int(origem), dia, _data(request.form.get('data_producao')),
            request.form.get('identificacao', ''), request.form.get('chave', ''),
            current_user.id)
    except (ValueError, diario.DiarioError) as exc:
        db.session.rollback()
        flash(str(exc) if isinstance(exc, diario.DiarioError) else 'Escolha o produto ou a massa-base.', 'danger')
        return _indice(dia, 400)
    flash('Lote aberto. Registre os dados conforme o preparo acontecer.', 'success')
    return redirect(url_for('padeiro.diario_lote', id=lote.id))


@padeiro_bp.route('/diario/lotes/<int:id>')
@login_required
@padeiro_required
def diario_lote(id):
    return _detalhe(_lote(id))


@padeiro_bp.route('/diario/lotes/<int:id>/medidas', methods=['POST'])
@login_required
@padeiro_required
def diario_salvar(id):
    _lote(id)
    try:
        diario.salvar_lote(id, request.form, current_user.id, request.form.get('versao'))
    except diario.DiarioError as exc:
        return _erro_lote(id, exc)
    flash('Medições salvas.', 'success')
    return redirect(url_for('padeiro.diario_lote', id=id))


@padeiro_bp.route('/diario/lotes/<int:id>/etapas', methods=['POST'])
@login_required
@padeiro_required
def diario_etapa_adicionar(id):
    _lote(id)
    modo = request.form.get('modo')
    if modo not in ('agora', 'horarios'):
        abort(400)
    try:
        diario.adicionar_etapa(
            id, request.form.get('nome', ''),
            agora() if modo == 'agora' else request.form.get('inicio_em'),
            None if modo == 'agora' else request.form.get('fim_em'),
            request.form.get('observacao', ''), current_user.id, request.form.get('versao'))
    except diario.DiarioError as exc:
        return _erro_lote(id, exc)
    flash('Etapa registrada.', 'success')
    return redirect(url_for('padeiro.diario_lote', id=id))


@padeiro_bp.route('/diario/lotes/<int:id>/etapas/<int:etapa_id>/concluir', methods=['POST'])
@login_required
@padeiro_required
def diario_etapa_concluir(id, etapa_id):
    _lote(id)
    try:
        diario.concluir_etapa(id, etapa_id, current_user.id)
    except diario.DiarioError as exc:
        return _erro_lote(id, exc)
    flash('Término da etapa registrado.', 'success')
    return redirect(url_for('padeiro.diario_lote', id=id))


@padeiro_bp.route('/diario/lotes/<int:id>/etapas/<int:etapa_id>/corrigir', methods=['POST'])
@login_required
@padeiro_required
def diario_etapa_corrigir(id, etapa_id):
    _lote(id)
    try:
        diario.corrigir_etapa(id, etapa_id, request.form.get('inicio_em'),
                             request.form.get('fim_em'), request.form.get('observacao', ''),
                             current_user.id, request.form.get('versao'),
                             nome=request.form.get('nome'))
    except diario.DiarioError as exc:
        return _erro_lote(id, exc)
    flash('Horários corrigidos. A alteração ficou registrada no histórico.', 'success')
    return redirect(url_for('padeiro.diario_lote', id=id))


@padeiro_bp.route('/diario/lotes/<int:id>/status', methods=['POST'])
@login_required
@padeiro_required
def diario_status(id):
    _lote(id)
    try:
        diario.definir_status(id, request.form.get('status'), current_user.id,
                             request.form.get('versao'))
    except diario.DiarioError as exc:
        return _erro_lote(id, exc)
    flash('Situação do registro atualizada.', 'success')
    return redirect(url_for('padeiro.diario_lote', id=id))


def _historico():
    fim = _data(request.args.get('fim'), hoje())
    inicio = _data(request.args.get('inicio'), fim - timedelta(days=6))
    if inicio > fim or (fim - inicio).days > 366:
        abort(400, 'Escolha um período de até um ano, com início antes do fim.')
    lotes = (ProducaoDiarioLote.query.filter(
        ProducaoDiarioLote.data_producao >= inicio, ProducaoDiarioLote.data_producao <= fim)
        .options(selectinload(ProducaoDiarioLote.etapas))
        .order_by(ProducaoDiarioLote.data_producao.desc(), ProducaoDiarioLote.id.desc()).all())
    return inicio, fim, lotes


@padeiro_bp.route('/diario/historico')
@login_required
@padeiro_required
def diario_historico():
    inicio, fim, lotes = _historico()
    return render_template('padeiro/diario_historico.html', inicio=inicio, fim=fim, lotes=lotes)


@padeiro_bp.route('/diario/exportar.csv')
@login_required
@padeiro_required
def diario_exportar():
    inicio, fim, lotes = _historico()
    out = io.StringIO()
    writer = csv.writer(out, delimiter=';')

    def texto_seguro(valor):
        texto = str(valor or '')
        return "'" + texto if texto.lstrip().startswith(('=', '+', '-', '@')) else texto

    writer.writerow(['Lote', 'Data do lote', 'Data da ordem', 'Produto ou base', 'Identificação',
                     'Situação', *[f'{label} ({unit})' for _, label, unit in CAMPOS_MEDIDAS],
                     'Observações do lote', 'Etapa', 'Início (Brasília)', 'Fim (Brasília)',
                     'Duração (min)', 'Observações da etapa'])
    for lote in lotes:
        for etapa in lote.etapas or [None]:
            writer.writerow([
                lote.id, lote.data_producao, lote.data_ordem, texto_seguro(lote.nome),
                texto_seguro(lote.identificacao), lote.status,
                *[str((lote.medidas or {}).get(key, '')).replace('.', ',') for key, _, _ in CAMPOS_MEDIDAS],
                texto_seguro(lote.observacao), texto_seguro(etapa.nome) if etapa else '',
                etapa.inicio_em.isoformat(' ', timespec='seconds') if etapa else '',
                etapa.fim_em.isoformat(' ', timespec='seconds') if etapa and etapa.fim_em else '',
                str(round(etapa.duracao_min, 2)).replace('.', ',') if etapa and etapa.duracao_min is not None else '',
                texto_seguro(etapa.observacao) if etapa else '',
            ])
    return Response('\ufeff' + out.getvalue(), mimetype='text/csv', headers={
        'Content-Disposition': f'attachment; filename="producao-{inicio}-{fim}.csv"',
        'Cache-Control': 'no-store',
    })
