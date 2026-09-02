"""Envio explícito de documentos e auditoria. Sem alteração de dívida ou NF."""
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import EnvioCobranca, FaturaB2B
from app.services import email as email_svc
from app.utils import agora


def email_valido(valor):
    import re
    return bool(valor and len(valor) <= 254 and re.fullmatch(r'[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+', valor))


def _registro(doc, cobrancas, destinatario, documentos, usuario, chave=None):
    fatura = isinstance(doc, FaturaB2B)
    referencia = (f'Fatura {doc.codigo}' if fatura else f'Venda #{doc.id}') if doc else 'Boleto avulso'
    return EnvioCobranca(
        chave=chave or str(uuid4()),
        fatura_id=doc.id if fatura else None,
        venda_id=doc.id if doc and not fatura else None,
        cobranca_ids=[c.id for c in cobrancas],
        referencia=referencia, destinatario=destinatario[:254],
        copias_ocultas=(email_svc.copias_ocultas_cobranca(destinatario)
                       if documentos == 'nf_boleto' else []),
        documentos=documentos, nf_id=getattr(doc, 'tiny_nota_fiscal_id', None),
        status='preparando', usuario_id=usuario.id, usuario_nome=usuario.nome[:100])


def _resultado(e, resultado):
    # Aceitação pela API não comprova entrega, abertura nem pagamento.
    if resultado.get('ok') and resultado.get('id'):
        e.status = 'aceito'
        e.provedor_id = str(resultado['id'])[:150]
        e.erro = None
    elif resultado.get('ok') or resultado.get('incerto'):
        e.status = 'incerto'
        e.erro = 'Não foi possível confirmar o resultado no serviço de e-mail. Confira antes de reenviar.'
    else:
        e.status = 'falha'
        e.erro = str(resultado.get('erro') or 'Envio não confirmado pelo serviço.')[:500]
    e.concluido_em = agora()
    db.session.commit()


def registrar_envio(doc, cobrancas, destinatario, documentos, usuario, resultado, anexos):
    """Caminhos legados também aparecem no histórico da central."""
    e = _registro(doc, cobrancas, destinatario, documentos, usuario)
    e.anexos = anexos
    db.session.add(e)
    _resultado(e, resultado)
    return e


def _repeticao(anterior, r, destinatario):
    from app.services.central_cobrancas import pertence
    if not pertence(anterior, r) or anterior.destinatario != destinatario:
        raise ValueError('Esta solicitação pertence a outro envio. Reabra a tela e confira o destinatário.')
    return anterior, False


def enviar_conjunto(r, destinatario, chave, usuario, banco_confirmado=False):
    """Reserva a intenção antes do envio. Repetir o POST não duplica e-mail.

    Nova tentativa deliberada requer nova chave (nova abertura da tela).
    Preparação interrompida fica visível, nunca é repetida automaticamente.
    """
    from app.services import tiny_nf
    from app.services.sicredi_boleto import (
        codigo_barras_da_cobranca,
        gerar_boleto_pdf,
        linha_digitavel,
    )

    destinatario = (destinatario or '').strip()
    try:
        chave = str(UUID(chave))
    except (TypeError, ValueError, AttributeError):
        raise ValueError('Reabra a tela de envio e tente novamente.')
    anterior = EnvioCobranca.query.filter_by(chave=chave).first()
    if anterior:
        return _repeticao(anterior, r, destinatario)
    if r.bloqueio:
        raise ValueError(r.bloqueio)
    if not email_valido(destinatario):
        raise ValueError('Informe um único e-mail válido para receber os dois documentos.')
    if r.cobranca.status == 'remessa' and not banco_confirmado:
        raise ValueError('Confirme que o boleto foi registrado no banco antes de enviar ao cliente.')

    e = _registro(r.documento, [r.cobranca], destinatario, 'nf_boleto', usuario, chave)
    db.session.add(e)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        anterior = EnvioCobranca.query.filter_by(chave=chave).first()
        if anterior:
            return _repeticao(anterior, r, destinatario)
        raise

    envio_iniciado = False
    try:
        nf_pdf, motivo = tiny_nf.baixar_danfe_pdf_com_motivo(r.documento.tiny_nota_fiscal_id)
        if not nf_pdf or not bytes(nf_pdf).startswith(b'%PDF'):
            raise ValueError(f'Não consegui baixar o DANFE — {motivo or "PDF indisponível"}. Nada foi enviado.')
        c = r.cobranca
        boleto_pdf = bytes(gerar_boleto_pdf(c))
        if not boleto_pdf.startswith(b'%PDF'):
            raise ValueError('PDF do boleto indisponível. Nada foi enviado.')
        ld = linha_digitavel(codigo_barras_da_cobranca(c))
        numero = r.documento.nf_numero or r.documento.tiny_nota_fiscal_id or r.documento.id
        e.anexos = [f'nfe_{numero}.pdf', f'boleto_{c.nosso_numero}.pdf']
        db.session.commit()
        rotulo = (f'fatura {r.documento.codigo} ({r.documento.periodo_display})'
                  if r.tipo == 'fatura' else f'venda #{r.documento.id}')
        envio_iniciado = True
        resultado = email_svc.enviar_nf_e_boleto_b2b(
            r.documento, destinatario, bytes(nf_pdf),
            [{'cob': c, 'pdf': boleto_pdf, 'linha_digitavel': ld}], rotulo=rotulo)
    except Exception as exc:  # Nenhuma queda do provedor deve derrubar a tela.
        # Depois de iniciar o envio, uma exceção pode ocorrer após a aceitação
        # pelo provedor. Não ofereça uma falsa certeza de que nada foi enviado.
        resultado = {'ok': False, 'erro': str(exc), 'incerto': envio_iniciado}
    _resultado(e, resultado)
    return e, True
