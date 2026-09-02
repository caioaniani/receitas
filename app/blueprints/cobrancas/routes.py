"""Gestão de cobranças — boleto híbrido Sicredi via CNAB400 (04/07/2026).

Fluxo: parcela B2B em aberto -> "Gerar cobrança" (snapshot do pagador) ->
seleciona pendentes -> "Gerar remessa" (arquivo .CRM pra subir no Sicredi
Internet / mandar na homologação) -> upload do RETORNO dá baixa (liquidação
quita a parcela junto) e traz o QR Pix do boleto híbrido.
"""
from datetime import date, datetime
from uuid import uuid4

from flask import (
    Response,
    abort,
    current_app,
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


@cobrancas_bp.context_processor
def pendencias_automacao():
    if not current_user.is_authenticated or not current_user.is_admin():
        return {}
    from app.models import AutomacaoCobranca
    from app.services.cobrancas_automacao import remessas_pendentes
    return {'remessas_a_conferir': len(remessas_pendentes()),
            'automacoes_com_erro': AutomacaoCobranca.query.filter_by(estado='erro').count()}


@cobrancas_bp.route('/automacao')
@login_required
def automacao():
    _admin_ou_403()
    from app.models import AppConfig, AutomacaoCobranca, AvisoRemessa
    from app.services.cobrancas_automacao import ESTADOS, RESPONSAVEIS, remessas_pendentes
    from app.utils import agora
    try:
        ultimo = datetime.fromisoformat(AppConfig.get('cobrancas_automacao_ultimo_ciclo', ''))
    except (ValueError, TypeError):
        ultimo = None
    atrasado = ultimo is None or (agora() - ultimo).total_seconds() > 300
    fila = AutomacaoCobranca.query.order_by(AutomacaoCobranca.id.desc()).paginate(per_page=30, error_out=False)
    avisos = AvisoRemessa.query.order_by(AvisoRemessa.id.desc()).limit(40).all()
    return render_template('cobrancas/automacao.html', fila=fila, remessas=remessas_pendentes(),
                           avisos=avisos, estados=ESTADOS, responsaveis=RESPONSAVEIS,
                           ultimo_ciclo=ultimo, ciclo_atrasado=atrasado)


@cobrancas_bp.route('/automacao/remessa/<int:id>/confirmar', methods=['POST'])
@login_required
def confirmar_registro_remessa(id):
    _admin_ou_403()
    from app.services.cobrancas_automacao import confirmar_registro
    if request.form.get('confirmado') != '1':
        flash('Confira primeiro no Sicredi se todos os boletos desta remessa foram registrados.', 'warning')
    else:
        try:
            confirmar_registro(CobrancaRemessa.query.get_or_404(id), current_user.id)
            flash('Conferência registrada. A fila automática poderá enviar NF + boleto no próximo ciclo.', 'success')
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'warning')
    return redirect(url_for('cobrancas.automacao'))


@cobrancas_bp.route('/automacao/<int:id>/retomar', methods=['POST'])
@login_required
def retomar_automacao(id):
    _admin_ou_403()
    from app.models import AutomacaoCobranca
    from app.services.cobrancas_automacao import _mudar
    j = AutomacaoCobranca.query.get_or_404(id)
    if j.estado != 'erro':
        abort(409)
    if not current_user.pode_emitir_nf_b2b():
        abort(403)
    _mudar(j, 'pendente')
    flash('Conferência solicitada. Documentos já gerados serão reutilizados; e-mails incertos não serão repetidos.', 'success')
    return redirect(url_for('cobrancas.automacao'))


@cobrancas_bp.route('/painel')
@login_required
def visao_geral():
    _admin_ou_403()
    from app.services.central_cobrancas import painel, resumo_dashboard
    return render_template('cobrancas/dashboard.html', resumo=resumo_dashboard(painel()))


@cobrancas_bp.route('/')
@login_required
def lista():
    _admin_ou_403()
    from app.services.central_cobrancas import ETAPAS, filtrar_etapa, painel
    linhas = painel()
    busca = (request.args.get('q') or '').strip()[:120]
    situacao = request.args.get('situacao', 'abertas')
    envio = request.args.get('envio', '')
    etapa = request.args.get('etapa', '')
    if etapa not in ETAPAS:
        etapa = ''
    if situacao not in ('abertas', 'vencidas', 'pagas', 'canceladas', 'sem_cobranca', 'todas'):
        situacao = 'abertas'
    if envio not in ('', 'sem_historico', 'aceito', 'problema'):
        envio = ''
    de, ate = request.args.get('de', ''), request.args.get('ate', '')
    try:
        inicio, fim = date.fromisoformat(de) if de else None, date.fromisoformat(ate) if ate else None
        if inicio and fim and inicio > fim:
            raise ValueError
    except ValueError:
        flash('Confira o período de vencimento informado.', 'warning')
        inicio = fim = None
        de = ate = ''
    linhas = [r for r in linhas if (not busca or busca.casefold() in f'{r.cliente} {r.referencia}'.casefold())
              and (not inicio or r.vencimento >= inicio) and (not fim or r.vencimento <= fim)]
    if envio == 'sem_historico':
        linhas = [r for r in linhas if not r.envio]
    elif envio == 'aceito':
        linhas = [r for r in linhas if r.envio_confirmado]
    elif envio == 'problema':
        linhas = [r for r in linhas if r.envio and r.envio.status != 'aceito']
    linhas = filtrar_etapa(linhas, etapa)
    abertas = [r for r in linhas if r.saldo and not r.cancelada]
    vencidas = [r for r in abertas if r.vencimento < hoje()]
    resumo = {
        'aberto': sum((r.saldo for r in abertas), 0),
        'vencido': sum((r.saldo for r in vencidas), 0),
        'sem_historico': sum(r.envio is None for r in abertas),
    }
    grupos = {
        'abertas': abertas, 'vencidas': vencidas,
        'pagas': [r for r in linhas if not r.saldo and not r.cancelada and not r.sem_cobranca],
        'sem_cobranca': [r for r in linhas if r.sem_cobranca],
        'canceladas': [r for r in linhas if r.cancelada], 'todas': linhas,
    }
    contagens = {k: len(v) for k, v in grupos.items()}
    linhas = grupos[situacao]
    total = len(linhas)
    paginas = max(1, (total + 29) // 30)
    pagina = max(1, min(paginas, request.args.get('pagina', 1, type=int)))

    def filtro_url(**kwargs):
        params = dict(q=busca, situacao=situacao, envio=envio, etapa=etapa, de=de, ate=ate)
        params.update(kwargs)
        return url_for('cobrancas.lista', **{k: v for k, v in params.items() if v})

    return render_template('cobrancas/central.html', linhas=linhas[(pagina - 1) * 30:pagina * 30],
                           resumo=resumo, contagens=contagens, total=total, pagina=pagina,
                           paginas=paginas, busca=busca, situacao=situacao, envio=envio,
                           de=de, ate=ate, filtro_url=filtro_url, etapa=etapa, etapas=ETAPAS)


@cobrancas_bp.route('/<any(fatura,parcela,boleto):tipo>/<int:ref>/documentos', methods=['GET', 'POST'])
@login_required
def documentos(tipo, ref):
    _admin_ou_403()
    from app.services.central_cobrancas import ENVIOS, atribuir_envios, carregar, historico
    from app.services.cobrancas_envio import enviar_conjunto
    from app.services.email import COPIAS_OCULTAS_COBRANCA
    r = carregar(tipo, ref)
    # Uma parcela absorvida por fatura sempre volta à cobrança consolidada.
    if (r.tipo, r.id) != (tipo, ref):
        if request.method == 'POST':
            flash('Esta parcela pertence a uma fatura. Confira o fechamento antes de enviar.', 'warning')
        return redirect(url_for('cobrancas.documentos', tipo=r.tipo, ref=r.id))
    if request.method == 'POST':
        try:
            e, novo = enviar_conjunto(r, request.form.get('email'), request.form.get('chave'),
                                     current_user, request.form.get('banco_confirmado') == '1')
        except ValueError as exc:
            flash(str(exc), 'warning')
        else:
            if not novo:
                flash('Esta solicitação já foi processada. Nenhum novo e-mail foi disparado.', 'info')
            elif e.status == 'aceito':
                flash('NF + boleto aceitos pelo serviço de e-mail. O histórico abaixo registra o envio.', 'success')
            else:
                flash(e.erro or 'Envio não confirmado. Consulte o histórico antes de tentar novamente.', 'warning')
        return redirect(url_for('cobrancas.documentos', tipo=tipo, ref=ref))
    envios = historico(r)
    atribuir_envios(r, envios)
    return render_template('cobrancas/documentos.html', r=r, historico=envios,
                           envio_labels=ENVIOS, chave=str(uuid4()),
                           copias_ocultas=COPIAS_OCULTAS_COBRANCA)


@cobrancas_bp.route('/<any(fatura,parcela,boleto):tipo>/<int:ref>/baixar')
@login_required
def baixar_documentos(tipo, ref):
    _admin_ou_403()
    from app.services.central_cobrancas import carregar
    from app.services.cobrancas_download import baixar_pacote

    r = carregar(tipo, ref)
    if (r.tipo, r.id) != (tipo, ref):
        flash('Esta cobrança pertence a uma fatura ou parcela. Confira a origem antes de baixar.', 'info')
        return redirect(url_for('cobrancas.documentos', tipo=r.tipo, ref=r.id))
    try:
        conteudo, nome, mime = baixar_pacote(r, request.args.get('formato', 'pdf'),
                                           request.args.get('banco_confirmado') == '1')
    except ValueError as exc:
        flash(str(exc), 'warning')
    except Exception:
        current_app.logger.exception('Falha ao baixar pacote de cobrança %s/%s', tipo, ref)
        flash('Não foi possível preparar os três documentos. Tente novamente em instantes. Nada foi emitido ou enviado.', 'warning')
    else:
        return Response(conteudo, mimetype=mime, headers={
            'Content-Disposition': f'attachment; filename="{nome}"',
            'Cache-Control': 'private, no-store',
            'X-Content-Type-Options': 'nosniff',
        })
    return redirect(url_for('cobrancas.documentos', tipo=r.tipo, ref=r.id))


@cobrancas_bp.route('/banco')
@login_required
def banco():
    _admin_ou_403()
    cobrancas = (Cobranca.query
                 .order_by(Cobranca.vencimento.asc(), Cobranca.id.asc())
                 .limit(500).all())
    cobrancas = [c for c in cobrancas if not (c.parcela and c.parcela.venda.sem_cobranca)]
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
    # Parcela de FATURA mensal fica fora: o boleto dela é o da fatura
    # (Cobranca.fatura_id) — listar aqui geraria boleto em dobro pro
    # cliente (achado da revisão 07/07/2026).
    parcelas_sem = [p for p in parcelas if not p.cobranca and not p.fatura_id
                    and not p.venda.fatura_id and p.venda.status != 'cancelada' and p.saldo > 0]
    remessas = (CobrancaRemessa.query
                .order_by(CobrancaRemessa.numero.desc()).limit(20).all())
    return render_template('cobrancas/lista.html',
                           abertas=abertas, pagas=pagas,
                           problemas=problemas, parcelas_sem=parcelas_sem,
                           remessas=remessas, hoje=hoje())


def _snapshot_pagador(cli):
    from app.services.cobrancas_preparo import snapshot_pagador
    return snapshot_pagador(cli)


@cobrancas_bp.route('/gerar-da-parcela/<int:parcela_id>', methods=['POST'])
@login_required
def gerar_da_parcela(parcela_id):
    """Mesmo preparo e trava usados pela automação; sem emitir NF ou enviar."""
    _admin_ou_403()
    from app.services.cobrancas_preparo import da_parcela
    from app.services.cobrancas_trava import chave_documento, trava
    p = VendaB2BParcela.query.get_or_404(parcela_id)
    try:
        with trava(chave_documento(p.venda)):
            venc_anterior = p.vencimento
            cob, novo = da_parcela(p, current_user.id)
            db.session.commit()
        if novo:
            flash(f'Cobrança criada para {cob.pagador_nome} (R$ {cob.valor}). Gere a remessa na área Banco.', 'success')
            if cob.vencimento != venc_anterior:
                flash(f'Vencimento ajustado para {cob.vencimento.strftime("%d/%m/%Y")} — mínimo de 7 dias para o Sicredi.', 'warning')
        else:
            flash('Essa parcela já tem cobrança.', 'warning')
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'warning')
    return redirect(url_for('cobrancas.lista'))


@cobrancas_bp.route('/gerar-da-fatura/<int:fatura_id>', methods=['POST'])
@login_required
def gerar_da_fatura(fatura_id):
    """Um título do total mensal, nunca um título para cada entrega."""
    _admin_ou_403()
    from app.models import FaturaB2B
    from app.services.cobrancas_preparo import da_fatura
    from app.services.cobrancas_trava import chave_documento, trava
    fat = FaturaB2B.query.get_or_404(fatura_id)
    try:
        with trava(chave_documento(fat)):
            cob, novo = da_fatura(fat, current_user.id)
            db.session.commit()
        flash(f'Cobrança da fatura {fat.codigo} criada (R$ {cob.valor}). Gere a remessa na área Banco.'
              if novo else 'A fatura já tem cobrança.', 'success' if novo else 'warning')
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'warning')
    return redirect(url_for('b2b.fatura_detalhe', fid=fatura_id))


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


@cobrancas_bp.route('/<int:id>/voltar-pendente', methods=['POST'])
@login_required
def voltar_pendente(id):
    """Cobrança que foi numa remessa mas o banco DEVOLVEU (homologação
    reprovada, rejeição) volta pra pendente: corrige os dados e entra numa
    NOVA remessa (novo sequencial). O nosso número é mantido — o título
    nunca chegou a ser registrado."""
    _admin_ou_403()
    cob = Cobranca.query.get_or_404(id)
    if cob.status not in ('remessa', 'rejeitada'):
        flash('Só cobrança em remessa (ainda não registrada) ou rejeitada '
              'pode voltar pra pendente.', 'warning')
        return redirect(url_for('cobrancas.lista'))
    cob.status = 'pendente'
    cob.remessa_id = None
    cob.motivo_retorno = None
    db.session.commit()
    flash(f'{cob.pagador_nome} voltou pra pendente — corrija os dados e '
          'gere uma NOVA remessa.', 'success')
    return redirect(url_for('cobrancas.lista'))


@cobrancas_bp.route('/<int:id>/definir-pix', methods=['POST'])
@login_required
def definir_pix(id):
    """Define MANUALMENTE o Pix copia-e-cola de uma cobrança (dono).

    Em PRODUÇÃO o Pix do boleto híbrido chega no arquivo de RETORNO
    (registro tipo 8) e nunca precisa disso. Este caminho existe pra
    HOMOLOGAÇÃO: o Sicredi manda um copia-e-cola de exemplo (mock) por
    e-mail pra validar a montagem/medidas do QR no PDF (07/07/2026)."""
    if not current_user.is_owner:
        abort(403)
    cob = Cobranca.query.get_or_404(id)
    pix = (request.form.get('pix') or '').strip()
    cob.pix_copia_cola = pix or None
    db.session.commit()
    flash(f'Pix copia-e-cola {"definido" if pix else "removido"} na '
          f'cobrança de {cob.pagador_nome} — o QR aparece no PDF do '
          'boleto.', 'success')
    return redirect(url_for('cobrancas.lista'))


@cobrancas_bp.route('/<int:id>/boleto.pdf')
@login_required
def boleto_pdf(id):
    """Boleto (ficha de compensação + recibo do pagador) pra imprimir/enviar.
    Disponível depois que a remessa atribui o nosso número."""
    _admin_ou_403()
    cob = Cobranca.query.get_or_404(id)
    if not cob.nosso_numero:
        flash('Essa cobrança ainda não tem nosso número — gere a remessa '
              'primeiro.', 'warning')
        return redirect(url_for('cobrancas.lista'))
    from app.services.sicredi_boleto import (
        gerar_boleto_pdf,
        nome_arquivo_boleto,
    )
    pdf = gerar_boleto_pdf(cob)
    return Response(
        bytes(pdf), mimetype='application/pdf',
        headers={'Content-Disposition':
                 f'inline; filename={nome_arquivo_boleto(cob)}'})


@cobrancas_bp.route('/<int:id>/enviar-email', methods=['POST'])
@login_required
def enviar_email(id):
    """Formulários antigos não podem mais disparar somente o boleto."""
    _admin_ou_403()
    from app.services.central_cobrancas import carregar
    r = carregar('boleto', id)
    flash('O envio agora é sempre NF + boleto. Confira os documentos antes de confirmar.', 'info')
    return redirect(url_for('cobrancas.documentos', tipo=r.tipo, ref=r.id), code=303)


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
