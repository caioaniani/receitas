"""Pilar B da meta 90% de contenção — FAQ honesto + horário do chat.

Trava as 12 regras de FAQ confirmadas pelo dono em 14/06/2026:
- Bot consulta receitas (`consultar_ingredientes`) pra responder
  ingrediente/glúten/lactose/ovo sem chutar.
- Alergia CONFIRMADA = handoff sempre (não chama tool pra tranquilizar).
- Cesta com troca, encomenda de evento, modificação de pedido feito = humano.
- Entrega: 8h-18h, corte 17h pra D+1, área 15 km.
- Retirada: SÓ Anésio Pinto Rosa, 78. Fora dos 15 km = oferece retirada
  PRIMEIRO, só transfere se cliente recusar.
- Pagamento: cartão crédito 1x, débito, Pix. Nada na entrega.
- Atendimento chat: 06h-20h. Fora disso, bot avisa e nao chama API.
"""
from unittest.mock import patch

# -------- Tool nova: consultar_ingredientes -------------------------------

def _criar_receita(app, nome, ingredientes):
    from app.extensions import db
    from app.models import Receita, ReceitaIngrediente
    rec = Receita(nome=nome, categoria='Paes',
                   rendimento_qtd=1, rendimento_unidade='un',
                   peso_base=100.0)
    db.session.add(rec)
    db.session.flush()
    for nome_ing, pct in ingredientes:
        db.session.add(ReceitaIngrediente(
            receita_id=rec.id, ingrediente_nome=nome_ing, porcentagem=pct))
    db.session.commit()
    return rec


def test_consultar_ingredientes_match_exato(app):
    from app.services.bot_tools import consultar_ingredientes
    with app.app_context():
        _criar_receita(app, 'Sourdough Tradicional', [
            ('Farinha de trigo', 60.0),
            ('Água', 35.0),
            ('Sal', 2.0),
            ('Fermento natural', 3.0),
        ])
        out = consultar_ingredientes('Sourdough Tradicional')
        assert out.get('receita') == 'Sourdough Tradicional'
        nomes = [i['nome'] for i in out['ingredientes']]
        # ordenado por % desc
        assert nomes[0] == 'Farinha de trigo'
        assert nomes[1] == 'Água'
        # NÃO contém 'leite' nem 'ovo' nesta receita
        assert not any('leite' in n.lower() for n in nomes)
        assert not any('ovo' in n.lower() for n in nomes)


def test_consultar_ingredientes_fuzzy_acento(app):
    from app.services.bot_tools import consultar_ingredientes
    with app.app_context():
        _criar_receita(app, 'Pão Francês', [('Farinha', 60.0), ('Água', 35.0)])
        out = consultar_ingredientes('pao frances')   # sem acento
        assert out.get('receita') == 'Pão Francês'


def test_consultar_ingredientes_filtra_irrelevante(app):
    """Ingredientes < 0.5% (especiarias residuais) não vão pra resposta —
    poluição. O cliente quer ouvir trigo/leite/ovo, não 0.03% de canela."""
    from app.services.bot_tools import consultar_ingredientes
    with app.app_context():
        _criar_receita(app, 'Croissant X', [
            ('Farinha', 50.0),
            ('Manteiga', 30.0),
            ('Especiaria Y', 0.2),  # filtrado
        ])
        out = consultar_ingredientes('Croissant X')
        nomes = [i['nome'] for i in out['ingredientes']]
        assert 'Especiaria Y' not in nomes


def test_consultar_ingredientes_nao_encontrado_sugere(app):
    from app.services.bot_tools import consultar_ingredientes
    with app.app_context():
        _criar_receita(app, 'Sourdough Tradicional', [('Farinha', 60.0)])
        _criar_receita(app, 'Sourdough Integral', [('Farinha integral', 60.0)])
        out = consultar_ingredientes('sourdough')
        # match parcial vai pegar uma das duas — ok
        # mas se eu busco algo IMPOSSIVEL:
        out2 = consultar_ingredientes('cerveja')
        assert out2.get('erro') == 'nao_encontrado'


def test_consultar_ingredientes_arquivada_invisivel(app):
    """Receita arquivada não entra na busca — não vamos responder sobre
    produto que não está mais sendo feito."""
    from app.extensions import db
    from app.services.bot_tools import consultar_ingredientes
    from app.utils import agora
    with app.app_context():
        # Outra receita ativa garante que NÃO caímos em 'sem receitas
        # cadastradas' — queremos validar que a ARQUIVADA está invisível.
        _criar_receita(app, 'Receita Ativa', [('Farinha', 60.0)])
        rec = _criar_receita(app, 'Sazonal Antigo', [('Farinha', 100.0)])
        rec.arquivada_em = agora()
        db.session.commit()
        out = consultar_ingredientes('Sazonal Antigo')
        assert out.get('erro') == 'nao_encontrado'


def test_tool_consultar_ingredientes_registrada(app):
    """Sem isso, o Sonnet vê a tool no prompt mas a chamada quebra com
    'ferramenta desconhecida'."""
    from app.services.chatbot import TOOLS, _executar_tool
    nomes = [t['name'] for t in TOOLS]
    assert 'consultar_ingredientes' in nomes

    with app.app_context():
        _criar_receita(app, 'Pão Teste', [('Farinha', 80.0)])
        out = _executar_tool('consultar_ingredientes', {'nome_produto': 'Pão Teste'})
        assert out.get('receita') == 'Pão Teste'


# -------- Aviso de fora-horário no handoff (06-20 BRT) --------------------
# Decisão do dono 14/06/2026: o bot NÃO bloqueia mais fora do horário —
# continua respondendo normal (catálogo, link). Só quando precisa fazer
# HANDOFF é que o aviso é injetado no texto (cliente saber que ninguém
# vai pegar agora). Verifica direto a função `_texto_handoff_com_horario`
# pra evitar mock do LLM.

def test_handoff_fora_horario_prepend_aviso():
    from app.services import chatbot
    with patch('app.services.chatbot._fora_horario_chat', return_value=True):
        out = chatbot._texto_handoff_com_horario(
            'Vou te passar para a Elô continuar o atendimento. 💛')
    assert '06:00' in out
    assert '20:00' in out
    # Texto original preservado depois do aviso
    assert 'Vou te passar para a Elô' in out


def test_handoff_dentro_horario_nao_muda_texto():
    from app.services import chatbot
    with patch('app.services.chatbot._fora_horario_chat', return_value=False):
        original = 'Vou te passar para a Elô continuar o atendimento. 💛'
        out = chatbot._texto_handoff_com_horario(original)
    assert out == original


def test_handoff_fora_horario_idempotente():
    """Se o LLM já escreveu o aviso (mensagem já contém '06:00'), NÃO
    duplica — senão o cliente recebe a mesma frase 2x."""
    from app.services import chatbot
    msg_llm_ja_avisou = (
        'Estamos fora do horário (06:00 às 20:00). Já anotei aqui '
        'e respondemos pela manhã, tá? 🙂')
    with patch('app.services.chatbot._fora_horario_chat', return_value=True):
        out = chatbot._texto_handoff_com_horario(msg_llm_ja_avisou)
    # NÃO duplicou: a frase aparece exatamente 1 vez (1 '06:00' só)
    assert out.count('06:00') == 1


def test_handoff_via_fallback_tambem_avisa_fora_horario(app):
    """Fallback de erro (ex: sem API key, lib ausente) também é um handoff
    e o cliente precisa do aviso. Sem isso, erro técnico fora de horário
    devolveria 'já te passo para um atendente' sem avisar a janela."""
    from app.services import chatbot
    with app.app_context():
        with patch('app.services.chatbot._fora_horario_chat', return_value=True):
            # Sem ANTHROPIC_API_KEY → cai no fallback de chave
            out = chatbot.responder([{'role': 'user', 'content': 'oi'}])
        assert out['acao'] == 'handoff'
        assert '06:00' in out['texto']


def test_bot_continua_respondendo_fora_horario(app):
    """A regra antiga (bloquear bot fora de horário) FOI REMOVIDA — o bot
    continua processando. Sem ANTHROPIC_API_KEY, vai cair em handoff de
    fallback (com aviso), mas o caminho NÃO é mais o early-return de
    fora-horário. Confirmação: motivo NÃO é 'fora_horario_chat'."""
    from app.services import chatbot
    with app.app_context():
        with patch('app.services.chatbot._fora_horario_chat', return_value=True):
            out = chatbot.responder([{'role': 'user', 'content': 'oi'}])
        # Não existe mais o caminho 'tools_usadas=[fora_horario_chat]'
        assert out.get('tools_usadas') != ['fora_horario_chat']
        assert out.get('motivo') == 'sem ANTHROPIC_API_KEY'


def test_fora_horario_real_calcula_pela_hora_local(app):
    """Trava a logica real `_fora_horario_chat` sem mock — sob diferentes
    horas BRT (via mock no `agora`), o detector decide certo."""
    from datetime import datetime

    from app.services import chatbot
    with app.app_context():
        # Caso 1: 23h45 → fora
        with patch('app.services.chatbot.agora' if False else 'app.utils.agora',
                   return_value=datetime(2026, 6, 14, 23, 45)):
            assert chatbot._fora_horario_chat() is True
        # Caso 2: 05h59 → fora (limite inferior)
        with patch('app.utils.agora',
                   return_value=datetime(2026, 6, 14, 5, 59)):
            assert chatbot._fora_horario_chat() is True
        # Caso 3: 06h00 → dentro
        with patch('app.utils.agora',
                   return_value=datetime(2026, 6, 14, 6, 0)):
            assert chatbot._fora_horario_chat() is False
        # Caso 4: 19h59 → dentro (limite superior exclusivo)
        with patch('app.utils.agora',
                   return_value=datetime(2026, 6, 14, 19, 59)):
            assert chatbot._fora_horario_chat() is False
        # Caso 5: 20h00 → fora
        with patch('app.utils.agora',
                   return_value=datetime(2026, 6, 14, 20, 0)):
            assert chatbot._fora_horario_chat() is True


# -------- Prompt FAQ — travas de regra ------------------------------------

def test_prompt_tem_secao_pagamento_correta():
    from app.services.chatbot_prompt import PROMPT
    assert 'cartão de crédito' in PROMPT.lower()
    assert 'débito' in PROMPT.lower()
    assert 'pix' in PROMPT.lower()
    assert 'não aceitamos pagamento na entrega' in PROMPT.lower()
    # 1x sem parcelamento
    assert '1x' in PROMPT or 'sem parcelamento' in PROMPT.lower()


def test_prompt_tem_regra_alergia_handoff():
    from app.services.chatbot_prompt import PROMPT
    # Alergia confirmada = handoff direto, NÃO chama consultar_ingredientes
    assert 'ALERGIA' in PROMPT.upper()
    assert 'transferir_para_humano' in PROMPT
    # Linguagem específica
    assert 'celíaco' in PROMPT.lower() or 'intolerante' in PROMPT.lower()
    assert 'contaminação cruzada' in PROMPT.lower()


def test_prompt_tem_regra_ingredientes_tool():
    from app.services.chatbot_prompt import PROMPT
    assert 'consultar_ingredientes' in PROMPT
    # Honestidade quando nao acha
    assert 'nao_encontrado' in PROMPT.lower() or 'não tenho a ficha' in PROMPT.lower()


def test_prompt_entrega_horario_corrigido():
    from app.services.chatbot_prompt import PROMPT
    # Site entrega 8h-18h (NÃO 7h-18h)
    assert '8h às 18h' in PROMPT
    assert '7h às 18h' not in PROMPT  # regressão
    # Corte 17h pra D+1
    assert '17h' in PROMPT


def test_prompt_retirada_so_anesio_pinto_rosa():
    from app.services.chatbot_prompt import PROMPT
    # Quando bot oferece retirada, é só essa loja
    assert 'Anésio Pinto Rosa, 78' in PROMPT
    # Confirmação de exclusividade
    assert ('SOMENTE' in PROMPT and 'Anésio Pinto Rosa' in PROMPT) or \
           ('única loja' in PROMPT.lower() and 'Anésio' in PROMPT)


def test_prompt_fora_area_oferece_retirada_antes_de_transferir():
    from app.services.chatbot_prompt import PROMPT
    # Regra do Q7: fora dos 15 km, oferece retirada PRIMEIRO
    fora_area_idx = PROMPT.find('fora_area')
    assert fora_area_idx > 0
    bloco = PROMPT[fora_area_idx:fora_area_idx + 800]
    assert 'retirada' in bloco.lower() or 'retirar' in bloco.lower()
    assert 'Anésio Pinto Rosa, 78' in bloco
    # Só transfere se cliente recusar
    assert 'recusar' in bloco.lower() or 'só use transferir' in bloco.lower()


def test_prompt_pedido_feito_modificacoes_humano():
    from app.services.chatbot_prompt import PROMPT
    # Q12: cancelar + remarcar + trocar item de pedido já feito = humano
    assert 'CANCELAR' in PROMPT.upper() or 'cancelar pedido' in PROMPT.lower()
    assert 'remarcar' in PROMPT.lower() or 'REMARCAR' in PROMPT


def test_prompt_encomenda_evento_humano():
    from app.services.chatbot_prompt import PROMPT
    # Q8: encomenda mesmo pequena = humano
    assert ('evento' in PROMPT.lower() or 'encomenda' in PROMPT.lower())
    # Não tem auto-resolução de evento
    bloco_idx = PROMPT.lower().find('encomenda')
    if bloco_idx < 0:
        bloco_idx = PROMPT.lower().find('evento')
    bloco = PROMPT[bloco_idx:bloco_idx + 400]
    assert 'transferir_para_humano' in bloco
