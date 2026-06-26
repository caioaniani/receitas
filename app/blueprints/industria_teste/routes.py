"""Tela de ESTUDO da indústria (/telaindustriateste).

NÃO mexe na /padeiro (a TV de produção real). Aqui a gente experimenta o
cronograma de produção POR DIA: em vez de produzir todo o déficit da semana
num dia só, distribui "um pouco de cada dia" acompanhando as entregas
(deslocado pelo lead time da receita). Read-only por enquanto — sem botão que
escreve estoque; o "Produzir" (entrada_producao) entra depois, quando a
lógica de distribuição estiver validada.
"""

from flask import render_template, request
from flask_login import login_required

from app.blueprints.industria_teste import industria_teste_bp
from app.decorators import admin_required


def _horizonte_janela():
    try:
        horizonte = max(1, min(int(request.values.get('horizonte', 7)), 14))
    except (TypeError, ValueError):
        horizonte = 7
    try:
        janela = max(1, min(int(request.values.get('janela', 6)), 26))
    except (TypeError, ValueError):
        janela = 6
    return horizonte, janela


@industria_teste_bp.route('/')
@login_required
@admin_required
def index():
    from app.services.previsao_producao import cronograma_producao

    try:
        horizonte = int(request.args.get('horizonte', 7))
    except ValueError:
        horizonte = 7
    horizonte = max(1, min(horizonte, 14))

    try:
        janela = int(request.args.get('janela', 6))
    except ValueError:
        janela = 6
    janela = max(1, min(janela, 26))

    crono = cronograma_producao(horizonte_dias=horizonte, janela_semanas=janela)
    return render_template('industria_teste/teste.html', crono=crono,
                           horizonte=horizonte, janela=janela)
