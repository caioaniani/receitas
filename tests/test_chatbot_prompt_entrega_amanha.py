"""Travas no PROMPT contra o match avaro de palavras-chave de entrega.

Bug real (12/06/2026, conv #198 — Mariana): cliente perguntou 'Tem cesta
de cafe da manha? Consegue entregar amanha?' — pergunta de produto com
qualificador temporal. O bot leu 'entregar' e fez handoff sem nem chamar
consultar_produtos. Match avaro: 1 palavra-chave (entregar) sobrescreveu
a intencao inteira (cesta).

Estas travas garantem que o prompt:
1. Tem regra explicita contra match avaro;
2. Cobre o caso 'entrega amanha em PEDIDO NOVO' como NAO-handoff;
3. Mantem o caso 'reagendar pedido EXISTENTE' como handoff (nao
   regredir o que ja funcionava).
"""
import pathlib


def _prompt():
    return pathlib.Path('app/services/chatbot_prompt.py').read_text()


def test_prompt_proibe_match_avaro_explicito():
    p = _prompt()
    assert 'MATCH AVARO' in p, \
        'regra contra match avaro sumiu do prompt'
    # Cita a palavra-chave que foi o gatilho do incidente
    assert 'entregar' in p.lower() and 'amanhã' in p
    assert 'pergunta de PRODUTO' in p or 'pergunta de produto' in p.lower()


def test_prompt_tem_caso_concreto_de_pedido_novo_com_entrega_futura():
    p = _prompt()
    # Caso concreto da Mariana (#198) precisa estar no prompt como
    # exemplo — modelo aprende muito melhor com exemplo que com regra
    assert 'consegue entregar amanhã' in p.lower() \
        or 'tem cesta' in p.lower(), \
        'exemplo concreto do caso da Mariana sumiu do prompt'
    # E o que fazer e explicito: consultar produtos + horario do site
    assert 'consultar_produtos' in p
    assert 'das 7h às 18h' in p or '7h as 18h' in p.lower()


def test_prompt_mantem_handoff_pra_reagendar_pedido_existente():
    """Regressao: o handoff legitimo (reagendar pedido EXISTENTE)
    nao pode ter sido perdido na mudanca."""
    p = _prompt()
    # Em algum lugar do prompt aparece a regra de reagendar pedido
    # existente -> handoff
    assert ('reagendar' in p.lower() or 'alterar data' in p.lower()
            or 'agendar/alterar' in p.lower())
    # E o termo transferir_para_humano ainda existe pra ESSE caso
    assert 'transferir_para_humano' in p


def test_prompt_distingue_pedido_novo_vs_existente_em_entrega():
    """A regra precisa ser EXPLICITA sobre os 2 mundos diferentes
    (novo = catalogo + checkout; existente = humano). Senao o modelo
    confunde de novo."""
    p = _prompt()
    assert ('pedido NOVO' in p or 'pedido novo' in p.lower()), \
        'distincao novo/existente perdida'
    assert ('JÁ EXISTENTE' in p or 'ja existente' in p.lower()
            or 'já existente' in p.lower())
