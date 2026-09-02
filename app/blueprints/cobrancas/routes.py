"""Gestão de cobranças — boleto híbrido Sicredi via CNAB400 (04/07/2026).

Fluxo: parcela B2B em aberto -> "Gerar cobrança" (snapshot do pagador) ->
seleciona pendentes -> "Gerar remessa" (arquivo .CRM pra subir no Sicredi
Internet / mandar na homologação) -> upload do RETORNO dá baixa (liquidação
quita a parcela junto) e traz o QR Pix do boleto híbrido.
"""
from datetime import date, datetime, timedelta
from uuid import uuid4

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
    from app.services.central_cobrancas import painel
    linhas = painel()
    busca = (request.args.get('q') or '').strip()[:120]
    situacao = request.args.get('situacao', 'abertas')
    envio = request.args.get('envio', '')
    if situacao not in ('abertas', 'vencidas', 'pagas', 'canceladas', 'todas'):
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
    abertas = [r for r in linhas if r.saldo and not r.cancelada]
    vencidas = [r for r in abertas if r.vencimento < hoje()]
    resumo = {
        'aberto': sum((r.saldo for r in abertas), 0),
        'vencido': sum((r.saldo for r in vencidas), 0),
        'sem_historico': sum(r.envio is None for r in abertas),
    }
    grupos = {
        'abertas': abertas, 'vencidas': vencidas,
        'pagas': [r for r in linhas if not r.saldo and not r.cancelada],
        'canceladas': [r for r in linhas if r.cancelada], 'todas': linhas,
    }
    contagens = {k: len(v) for k, v in grupos.items()}
    linhas = grupos[situacao]
    if envio == 'sem_historico':
        linhas = [r for r in linhas if not r.envio]
    elif envio == 'aceito':
        linhas = [r for r in linhas if r.envio and r.envio.status == 'aceito']
    elif envio == 'problema':
        linhas = [r for r in linhas if r.envio and r.envio.status != 'aceito']
    total = len(linhas)
    paginas = max(1, (total + 29) // 30)
    pagina = max(1, min(paginas, request.args.get('pagina', 1, type=int)))

    def filtro_url(**kwargs):
        params = dict(q=busca, situacao=situacao, envio=envio, de=de, ate=ate)
        params.update(kwargs)
        return url_for('cobrancas.lista', **{k: v for k, v in params.items() if v})

    return render_template('cobrancas/central.html', linhas=linhas[(pagina - 1) * 30:pagina * 30],
                           resumo=resumo, contagens=contagens, total=total, pagina=pagina,
                           paginas=paginas, busca=busca, situacao=situacao, envio=envio,
                           de=de, ate=ate, filtro_url=filtro_url)


@cobrancas_bp.route('/<any(fatura,parcela,boleto):tipo>/<int:ref>/documentos', methods=['GET', 'POST'])
@login_required
def documentos(tipo, ref):
    _admin_ou_403()
    from app.services.central_cobrancas import ENVIOS, carregar, historico
    from app.services.cobrancas_envio import enviar_conjunto
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
    r.envio = envios[0] if envios else None
    return render_template('cobrancas/documentos.html', r=r, historico=envios,
                           envio_labels=ENVIOS, chave=str(uuid4()))


@cobrancas_bp.route('/banco')
@login_required
def banco():
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
    # Parcela de FATURA mensal fica fora: o boleto dela é o da fatura
    # (Cobranca.fatura_id) — listar aqui geraria boleto em dobro pro
    # cliente (achado da revisão 07/07/2026).
    parcelas_sem = [p for p in parcelas if not p.cobranca and not p.fatura_id]
    remessas = (CobrancaRemessa.query
                .order_by(CobrancaRemessa.numero.desc()).limit(20).all())
    return render_template('cobrancas/lista.html',
                           abertas=abertas, pagas=pagas,
                           problemas=problemas, parcelas_sem=parcelas_sem,
                           remessas=remessas, hoje=hoje())


def _snapshot_pagador(cli):
    """(endereco, cep) do CADASTRO do ClienteB2B — fonte ÚNICA das duas
    rotas de geração (07/08/2026, pergunta do dono "por que não puxa o CEP
    direto do cadastro?"): a rota de parcela avulsa gravava `pagador_cep=''`
    fixo e toda cobrança exigia digitação manual na tela, mesmo com o
    cadastro completo. Montagem idêntica à que a fatura mensal já usava
    (homologada): campo livre com fallback pros estruturados; CEP só
    dígitos."""
    if cli is None:
        return '', ''
    endereco = (cli.endereco or '').strip()
    if not endereco and cli.endereco_logradouro:
        endereco = ' '.join(x for x in (
            cli.endereco_logradouro,
            (f'{cli.endereco_numero}' if cli.endereco_numero else ''),
            (f'- {cli.endereco_bairro}' if cli.endereco_bairro else ''))
            if x)
    cep = ''.join(ch for ch in (cli.endereco_cep or '') if ch.isdigit())
    return endereco, cep


@cobrancas_bp.route('/gerar-da-parcela/<int:parcela_id>', methods=['POST'])
@login_required
def gerar_da_parcela(parcela_id):
    """Cria a cobrança de UMA parcela B2B (snapshot do pagador da venda)."""
    _admin_ou_403()
    p = VendaB2BParcela.query.get_or_404(parcela_id)
    if p.cobranca:
        flash('Essa parcela já tem cobrança.', 'warning')
        return redirect(url_for('cobrancas.lista'))
    if p.fatura_id:
        flash('Parcela de fatura mensal — o boleto sai pela FATURA '
              '(B2B → Faturas mensais), não por parcela; gerar aqui '
              'cobraria o cliente em dobro.', 'warning')
        return redirect(url_for('cobrancas.lista'))
    # Venda cancelada não vira boleto (achado da revisão 20/07/2026:
    # cancelar_venda mantém as parcelas como registro e elas continuavam
    # "candidatas" — cobrar venda morta no Sicredi).
    if p.venda and p.venda.status == 'cancelada':
        flash(f'A venda #{p.venda.id} está CANCELADA — parcela não vira '
              'boleto.', 'danger')
        return redirect(url_for('cobrancas.lista'))
    venda = p.venda
    cli = venda.cliente
    emissao = hoje()
    venc = max(p.vencimento, emissao + timedelta(days=7))  # regra Sicredi
    endereco, cep = _snapshot_pagador(cli)
    cob = Cobranca(
        parcela_id=p.id,
        pagador_nome=(cli.nome if cli else venda.cliente_nome or ''),
        pagador_cnpj_cpf=(cli.cnpj_cpf if cli else '') or '',
        pagador_endereco=endereco,
        pagador_cep=cep,
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
          f'(R$ {cob.valor}). Complete endereço e CEP antes da remessa — '
          'o banco rejeita sem eles.', 'success')
    return redirect(url_for('cobrancas.lista'))


@cobrancas_bp.route('/gerar-da-fatura/<int:fatura_id>', methods=['POST'])
@login_required
def gerar_da_fatura(fatura_id):
    """Cria a cobrança (UM boleto) de uma fatura mensal B2B — o total do
    fechamento. A liquidação quita a fatura e as parcelas juntas."""
    _admin_ou_403()
    from app.models import FaturaB2B
    fat = FaturaB2B.query.get_or_404(fatura_id)
    if fat.status != 'fechada':
        flash(f'Fatura {fat.codigo} está "{fat.status}" — só fatura fechada '
              'gera boleto.', 'warning')
        return redirect(url_for('b2b.fatura_detalhe', fid=fatura_id))
    if fat.cobrancas:
        flash(f'A fatura {fat.codigo} já tem cobrança.', 'warning')
        return redirect(url_for('b2b.fatura_detalhe', fid=fatura_id))
    cli = fat.cliente
    endereco, cep = _snapshot_pagador(cli)
    emissao = hoje()
    venc = max(fat.vencimento, emissao + timedelta(days=7))  # regra Sicredi
    cob = Cobranca(
        fatura_id=fat.id,
        pagador_nome=cli.nome,
        pagador_cnpj_cpf=cli.cnpj_cpf or '',
        pagador_endereco=endereco,
        pagador_cep=cep,
        valor=fat.valor_total, vencimento=venc, emissao=emissao,
        seu_numero=fat.codigo[:10],
        criado_por_id=current_user.id,
    )
    db.session.add(cob)
    if venc != fat.vencimento:
        # Realinha fatura + parcelas do fechamento com o boleto — senão o
        # contas a receber acusa "atrasado" antes de o boleto vencer.
        fat.vencimento = venc
        for p in fat.parcelas:
            p.vencimento = venc
        flash(f'Vencimento ajustado pra {venc.strftime("%d/%m/%Y")} (fatura '
              'e parcelas juntas) — o Sicredi exige mínimo de 7 dias após '
              'a emissão.', 'warning')
    db.session.commit()
    flash(f'Cobrança da fatura {fat.codigo} criada (R$ {cob.valor}). '
          'Marque-a e gere a remessa em Cobranças.', 'success')
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
    """Manda o boleto (PDF anexado + linha digitável + Pix se houver) pro
    e-mail do cliente B2B da parcela. Aceita e-mail avulso no form pra
    cobrança sem cliente cadastrado."""
    _admin_ou_403()
    cob = Cobranca.query.get_or_404(id)
    if not cob.nosso_numero:
        flash('Essa cobrança ainda não tem nosso número — gere a remessa '
              'primeiro.', 'warning')
        return redirect(url_for('cobrancas.lista'))
    cliente = (cob.fatura.cliente if cob.fatura
               else cob.parcela.venda.cliente if cob.parcela else None)
    destinatario = ((request.form.get('email') or '').strip()
                    or (cliente.email if cliente else '') or '')
    if not destinatario:
        flash('Cliente sem e-mail cadastrado — complete o cadastro em '
              'B2B → Clientes ou informe um e-mail.', 'warning')
        return redirect(url_for('cobrancas.lista'))
    from app.services import email as email_svc
    from app.services.sicredi_boleto import (
        codigo_barras_da_cobranca,
        gerar_boleto_pdf,
        linha_digitavel,
    )
    pdf = bytes(gerar_boleto_pdf(cob))
    ld = linha_digitavel(codigo_barras_da_cobranca(cob))
    res = email_svc.enviar_boleto_b2b(cob, destinatario, pdf,
                                      linha_digitavel=ld)
    from app.services.cobrancas_envio import registrar_envio
    registrar_envio(cob.fatura if cob.fatura else cob.parcela.venda if cob.parcela else None,
                   [cob], destinatario, 'boleto', current_user, res,
                   [f'boleto_{cob.nosso_numero}.pdf'])
    if res.get('ok'):
        flash(f'Boleto enviado pra {destinatario}.', 'success')
    else:
        flash(f'Falha ao enviar o e-mail: {res.get("erro")}', 'danger')
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
