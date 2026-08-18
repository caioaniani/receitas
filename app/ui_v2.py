"""Chave da interface v2 (redesenho promovido do preview em 18/08/2026).

Promocao SELETIVA do branch codex/ui-simplification-preview: veio SO o
visual/usabilidade; a infraestrutura de homologacao (preview_copy.py,
preview_seed.py, PREVIEW_MODE, copia sanitizada de banco, reset de senha
do admin) NAO foi promovida de proposito — nada aqui toca banco.

Tres camadas, da mais larga pra mais fina:
- env `UI_V2_ENABLED=1` liga o visual novo pra todo mundo (kill-switch:
  remover/zerar a env volta TUDO ao visual anterior, sem deploy de
  codigo);
- cookie `ui_classic` devolve UM usuario a interface anterior (link
  "Interface anterior" na sidebar nova; "Nova interface" na antiga
  re-liga) — rollback individual sem mexer na env;
- `?v2=1` forca a tela nova NUMA request (validacao em producao antes de
  ligar a env pra equipe).
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
