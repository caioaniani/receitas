"""Pagina publica de comprovante de entrega — acessada pelo cliente
via link /entrega/<hash> compartilhado pela padaria."""
from flask import abort, render_template

from app.blueprints.comprovante import comprovante_bp
from app.models import AtribuicaoEntrega, EntregaFoto


@comprovante_bp.route('/<proof_hash>')
def comprovante_publico(proof_hash):
    a = AtribuicaoEntrega.query.filter_by(proof_hash=proof_hash).first()
    if not a:
        abort(404)
    # PULADO (dono 09/08/2026): visita comprovada com foto (portaria não
    # recebeu), entrega ainda vai acontecer — página mostra a foto e avisa
    # que o entregador volta. Quando virar entregue, o MESMO link passa a
    # exibir o comprovante de entrega.
    pulado = bool(a.pulado_em) and (a.status or 'pendente') == 'pendente'
    mostrar_fotos = a.status == 'entregue' or pulado
    fotos = (a.fotos.order_by(EntregaFoto.tirada_em).all()
             if mostrar_fotos else [])
    return render_template(
        'driver/comprovante.html',
        encontrado=(a.status == 'entregue'),
        pulado=pulado,
        atribuicao=a,
        fotos=fotos,
        driver_nome=a.driver.nome if a.driver else '',
    )
