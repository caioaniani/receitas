"""Chave da interface v2 (redesenho promovido do preview em 18/08/2026).

Promocao SELETIVA do branch codex/ui-simplification-preview: veio SO o
visual/usabilidade; a infraestrutura de homologacao (preview_copy.py,
preview_seed.py, PREVIEW_MODE, copia sanitizada de banco, reset de senha
do admin) NAO foi promovida de proposito — nada aqui toca banco.

A v2 e o PADRAO do sistema interno desde 18/08/2026 (decisao do dono no
PR #14) — sem variavel de ambiente. Camadas de saida:
- `config.UI_V2_ENABLED` e uma constante (True) SEM leitura de env:
  rollback geral = trocar pra False em config.py (1 commit);
- cookie `ui_classic` devolve UM usuario a interface anterior (link
  "Interface anterior" na sidebar nova; "Nova interface" na antiga
  re-liga) — saida individual de emergencia;
- `?v2=1` forca a tela nova NUMA request mesmo pra quem optou pelo
  classico (util pra suporte/comparacao).
O site publico (/loja, carrinho, checkout, pagamento) nao passa por
aqui — tem layout proprio, fora do base.html.
"""
from flask import current_app, request

UI_CLASSIC_COOKIE = 'ui_classic'


def ui_v2_ativo():
    """True quando ESTA request deve renderizar a interface v2."""
    try:
        if request.args.get('v2') == '1':
            return True
        if not current_app.config.get('UI_V2_ENABLED'):
            return False
        return request.cookies.get(UI_CLASSIC_COOKIE) != '1'
    except RuntimeError:                    # fora de request/app context
        return False
