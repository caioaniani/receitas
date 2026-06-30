"""Bot deve REMONTAR o carrinho (gerar_link_carrinho) quando o cliente diz que
sumiu — em vez de ficar perdido. Auditoria 30/06 (conv Angélica, venda quase
perdida). O carrinho da loja vive em localStorage; reenviar o link ?add= o
remonta no contexto atual do cliente."""
from app.services.chatbot_prompt import PROMPT


def test_prompt_ensina_remontar_carrinho_sumido():
    assert 'CARRINHO / PEDIDO SUMIU' in PROMPT
    trecho = (PROMPT.split('CARRINHO / PEDIDO SUMIU', 1)[1]
              .split('LINKS — SEMPRE', 1)[0])
    # a saida e remontar o carrinho com a tool, na hora
    assert 'gerar_link_carrinho' in trecho
    assert 'remonta o carrinho' in trecho
    # e tratado como venda em risco / prioridade
    assert 'venda em risco' in trecho.lower()
    # handoff so se o link NOVO tambem falhar (nao como primeira reacao)
    assert 'também falhar' in trecho.lower() or 'tambem falhar' in trecho.lower()
