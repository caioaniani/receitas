"""Impostos sobre VENDA (PIS/COFINS/ICMS) — fonte única da margem LÍQUIDA.

Pedido do dono (13/07/2026, da planilha dele): PIS 1,65% + COFINS 7,60% +
ICMS 4,00% = 13,25% sobre o preço de venda, descontados nas margens exibidas
(ficha da receita, /rentabilidade, relatório de custos, dashboard, copilot).

- Alíquotas ficam em AppConfig (`imposto_pis_pct` etc.) e são editáveis na
  tela /rentabilidade — os defaults abaixo são os números do dono.
- É cálculo de EXIBIÇÃO/decisão: nada aqui altera preço, pedido ou transação.
- Os helpers de margem recebem `carga` explícita: quem itera centenas de
  receitas busca a carga UMA vez (`carga_venda()`) e passa adiante — sem
  query escondida em loop.
"""
from app.models import AppConfig

ALIQUOTAS_PADRAO = {'pis': 1.65, 'cofins': 7.6, 'icms': 4.0}
_CHAVE = 'imposto_%s_pct'
_TETO_PCT = 95.0   # sanidade: carga acima disso é erro de digitação


def aliquotas():
    """{'pis': 1.65, 'cofins': 7.6, 'icms': 4.0, 'total': 13.25} (em %),
    lendo AppConfig com fallback pros padrões do dono. Valor inválido no
    banco cai no padrão (nunca quebra tela de relatório)."""
    out = {}
    for nome, padrao in ALIQUOTAS_PADRAO.items():
        try:
            v = float(AppConfig.get(_CHAVE % nome, padrao))
        except (TypeError, ValueError):
            v = padrao
        out[nome] = v if 0 <= v <= _TETO_PCT else padrao
    out['total'] = round(out['pis'] + out['cofins'] + out['icms'], 4)
    return out


def carga_venda():
    """Fração do preço de venda que vira imposto (ex: 0.1325)."""
    return min(aliquotas()['total'], _TETO_PCT) / 100.0


def salvar_aliquotas(pis, cofins, icms):
    """Valida (0..95% cada) e persiste em AppConfig. Commita.
    Levanta ValueError com o nome do campo inválido."""
    from app.extensions import db
    valores = {'pis': pis, 'cofins': cofins, 'icms': icms}
    limpos = {}
    for nome, bruto in valores.items():
        try:
            v = float(str(bruto).replace(',', '.'))
        except (TypeError, ValueError):
            raise ValueError(nome)
        if not (0 <= v <= _TETO_PCT):
            raise ValueError(nome)
        limpos[nome] = v
    for nome, v in limpos.items():
        AppConfig.set(_CHAVE % nome, v)
    db.session.commit()
    return aliquotas()


def lucro_liquido(preco, custo, carga):
    """R$ que sobra por unidade após impostos sobre a venda e o custo.
    None se não há preço."""
    if not preco or preco <= 0:
        return None
    return preco * (1 - carga) - (custo or 0)


def margem_liquida(preco, custo, carga):
    """% do preço que sobra após impostos e custo. None se não há preço."""
    lucro = lucro_liquido(preco, custo, carga)
    if lucro is None:
        return None
    return lucro / preco * 100
