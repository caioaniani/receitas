"""Pagina publica de comprovante de entrega — acessada pelo cliente
via link /entrega/<hash> compartilhado pela padaria."""
from flask import abort, render_template

from app.models import AtribuicaoEntrega, EntregaFoto
from app.blueprints.comprovante import comprovante_bp


@comprovante_bp.route('/<proof_hash>')
def comprovante_publico(proof_hash):
    a = AtribuicaoEntrega.query.filter_by(proof_hash=proof_hash).first()
    if not a:
        abort(404)
    fotos = a.fotos.order_by(EntregaFoto.tirada_em).all() if a.status == 'entregue' else []
    return render_template(
        'driver/comprovante.html',
        encontrado=(a.status == 'entregue'),
        atribuicao=a,
        fotos=fotos,
        driver_nome=a.driver.nome if a.driver else '',
    )
