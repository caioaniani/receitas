"""Formulário PÚBLICO de pré-cadastro de funcionário (aberto por QR Code).

Sem login — o funcionário só informa nome/sobrenome/e-mail/telefone. O gate
global (`_gate_conta`) não age em anônimo. Rate-limit contra abuso.
"""
from flask import render_template, request

from app.blueprints.precadastro import precadastro_bp
from app.extensions import limiter
from app.services import precadastro as svc


@precadastro_bp.route('/cadastro-funcionario', methods=['GET'])
def form():
    return render_template('precadastro/form.html', enviado=False, dados={})


@precadastro_bp.route('/cadastro-funcionario', methods=['POST'])
@limiter.limit('6 per minute')
def enviar():
    dados, erro = svc.validar(
        request.form.get('nome'), request.form.get('sobrenome'),
        request.form.get('email'), request.form.get('telefone'))
    if erro:
        return render_template('precadastro/form.html', enviado=False,
                               erro=erro, dados=request.form.to_dict()), 400
    svc.criar(dados)
    return render_template('precadastro/form.html', enviado=True, dados={})
