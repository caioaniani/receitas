"""Gestão de cobranças — boleto híbrido Sicredi via CNAB400 (04/07/2026).

Fluxo: parcela B2B em aberto -> "Gerar cobrança" (snapshot do pagador) ->
seleciona pendentes -> "Gerar remessa" (arquivo .CRM pra subir no Sicredi
Internet / mandar na homologação) -> upload do RETORNO dá baixa (liquidação
quita a parcela junto) e traz o QR Pix do boleto híbrido.
"""
from datetime import datetime, timedelta

from flask import (
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from app.blueprints.cobrancas import cobrancas_bp
from app.extensions import db
from app.models import Cobranca, CobrancaRemessa, VendaB2BParcela
from app.utils import hoje


def _admin_ou_403():
    if not current_user.is_admin():
        abort(403)


@cobrancas_bp.route('/')
@login_required
def lista():
    _admin_ou_403()
    cobrancas = (Cobranca.query
                 .order_by(Cobranca.vencimento.asc(), Cobranca.id.asc())
                 .limit(500).all())
    abertas = [c for c in cobrancas
               if c.status in ('pendente', 'remessa', 'registrada')]
    pagas = [c for c in cobrancas if c.status == 'paga']
    problemas = [c for c in cobrancas
                 if c.status in ('rejeitada', 'baixada')]
    # Parcelas B2B em aberto ainda SEM cobrança — candidatas a boleto.
    parcelas = (VendaB2BParcela.query
                .filter(VendaB2BParcela.pago_em.is_(None))
                .order_by(VendaB2BParcela.vencimento.asc())
                .limit(200).all())
    parcelas_sem = [p for p in parcelas if not p.cobranca]
    remessas = (CobrancaRemessa.query
                .order_by(CobrancaRemessa.numero.desc()).limit(20).all())
    return render_template('cobrancas/lista.html',
                           abertas=abertas, pagas=pagas,
                           problemas=problemas, parcelas_sem=parcelas_sem,
                           remessas=remessas, hoje=hoje())


@cobrancas_bp.route('/gerar-da-parcela/<int:parcela_id>', methods=['POST'])
@login_required
def gerar_da_parcela(parcela_id):
    """Cria a cobrança de UMA parcela B2B (snapshot do pagador da venda)."""
    _admin_ou_403()
    p = VendaB2BParcela.query.get_or_404(parcela_id)
    if p.cobranca:
        flash('Essa parcela já tem cobrança.', 'warning')
        return redirect(url_for('cobrancas.lista'))
    venda = p.venda
    cli = venda.cliente
    emissao = hoje()
    venc = max(p.vencimento, emissao + timedelta(days=7))  # regra Sicredi
    cob = Cobranca(
        parcela_id=p.id,
        pagador_nome=(cli.nome if cli else venda.cliente_nome or ''),
        pagador_cnpj_cpf=(cli.cnpj_cpf if cli else '') or '',
        pagador_endereco=(cli.endereco if cli else '') or '',
        pagador_cep='',
        valor=p.valor, vencimento=venc, emissao=emissao,
        seu_numero=f'V{venda.id}P{p.numero}',
        criado_por_id=current_user.id,
    )
    db.session.add(cob)
    db.session.commit()
    if venc != p.vencimento:
        flash(f'Vencimento ajustado pra {venc.strftime("%d/%m/%Y")} — o '
              'Sicredi exige mínimo de 7 dias após a emissão.', 'warning')
    flash(f'Cobrança criada pra {cob.pagador_nome} '
          f'(R$ {cob.valor}). Complete o CEP antes da remessa.', 'success')
    return redirect(url_for('cobrancas.lista'))


@cobrancas_bp.route('/<int:id>/editar', methods=['POST'])
@login_required
def editar(id):
    """Edita dados do pagador/vencimento enquanto a cobrança está pendente."""
    _admin_ou_403()
    cob = Cobranca.query.get_or_404(id)
    if cob.status != 'pendente':
        flash('Só cobrança pendente pode ser editada (as demais já foram '
              'ao banco).', 'warning')
        return redirect(url_for('cobrancas.lista'))
    for campo in ('pagador_nome', 'pagador_cnpj_cpf', 'pagador_endereco',
                  'pagador_cep'):
        v = (request.form.get(campo) or '').strip()
        if v:
            setattr(cob, campo, v)
    venc = (request.form.get('vencimento') or '').strip()
    if venc:
        try:
            cob.vencimento = datetime.strptime(venc, '%Y-%m-%d').date()
        except ValueError:
            flash('Data de vencimento inválida — mantida a anterior.',
                  'warning')
    db.session.commit()
    flash('Cobrança atualizada.', 'success')
    return redirect(url_for('cobrancas.lista'))


@cobrancas_bp.route('/<int:id>/excluir', methods=['POST'])
@login_required
def excluir(id):
    _admin_ou_403()
    cob = Cobranca.query.get_or_404(id)
    if cob.status != 'pendente':
        flash('Só cobrança pendente pode ser excluída — as demais já foram '
              'ao banco (use baixa pelo retorno).', 'warning')
        return redirect(url_for('cobrancas.lista'))
    db.session.delete(cob)
    db.session.commit()
    flash('Cobrança excluída.', 'info')
    return redirect(url_for('cobrancas.lista'))


@cobrancas_bp.route('/remessa', methods=['POST'])
@login_required
def gerar_remessa_rota():
    _admin_ou_403()
    from app.services.sicredi_cnab import gerar_remessa
    ids = [int(i) for i in request.form.getlist('ids') if str(i).isdigit()]
    cobrancas = Cobranca.query.filter(Cobranca.id.in_(ids)).all() if ids else []
    rem, erros = gerar_remessa(cobrancas, user_id=current_user.id)
    if erros:
        for e in erros[:8]:
            flash(e, 'danger')
        return redirect(url_for('cobrancas.lista'))
    flash(f'Remessa {rem.nome_arquivo} gerada com {rem.n_titulos} título(s) '
          '— baixe e envie no Sicredi Internet (ou pro e-mail da '
          'homologação).', 'success')
    return redirect(url_for('cobrancas.lista'))


@cobrancas_bp.route('/remessa/<int:id>/download')
@login_required
def download_remessa(id):
    _admin_ou_403()
    rem = CobrancaRemessa.query.get_or_404(id)
    return Response(
        rem.conteudo, mimetype='text/plain',
        headers={'Content-Disposition':
                 f'attachment; filename={rem.nome_arquivo}'})


@cobrancas_bp.route('/retorno', methods=['POST'])
@login_required
def upload_retorno():
    _admin_ou_403()
    from app.services.sicredi_cnab import processar_retorno
    arq = request.files.get('arquivo')
    if arq is None or not arq.filename:
        flash('Escolha o arquivo de retorno (.CRT/.RET).', 'warning')
        return redirect(url_for('cobrancas.lista'))
    try:
        texto = arq.read().decode('latin-1')
    except Exception:  # noqa: BLE001
        flash('Não consegui ler o arquivo (codificação?).', 'danger')
        return redirect(url_for('cobrancas.lista'))
    res = processar_retorno(texto, user_id=current_user.id)
    flash(f"Retorno processado: {res['pagas']} paga(s), "
          f"{res['registradas']} registrada(s), {res['qrcode']} QR(s) Pix, "
          f"{res['rejeitadas']} rejeitada(s), {res['baixadas']} baixada(s), "
          f"{res['nao_encontradas']} não encontrada(s).",
          'success' if not res['rejeitadas'] else 'warning')
    for d in res['detalhes'][:10]:
        flash(d, 'info')
    return redirect(url_for('cobrancas.lista'))
