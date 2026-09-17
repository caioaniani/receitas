"""Escopo obrigatório do perfil de consulta de uma loja (não editável por matriz)."""
from flask import abort


def loja_operacional(loja_id):
    from app.models import Loja

    if not loja_id:
        return None
    return Loja.query.filter(
        Loja.id == loja_id, Loja.ativa.is_(True), Loja.nome != 'Industria',
    ).first()


def loja_permitida(usuario):
    """Falha fechada: nunca interpretar vínculo ausente/inativo como todas."""
    loja = loja_operacional(usuario.loja_id)
    if not usuario.is_relatorio_loja() or not loja:
        abort(403)
    return loja
