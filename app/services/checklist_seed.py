"""Checklist operacional da Opão — importação do padrão em papel (03/08/2026).

O dono mandou o PDF "CHECKLISTS OPERACIONAIS POR SETOR": 11 folhas (uma por
setor, fixada no setor) com blocos ABERTURA / DURANTE O EXPEDIENTE /
FECHAMENTO, ~169 pontos no total.

Decisões dele na importação (AskUserQuestion 03/08/2026):
- **Tudo numa tela, agrupado por setor**: o responsável do turno abre
  "Abertura" e vê os 60 pontos de todos os setores com o setor como
  SUBTÍTULO — não há navegação por setor.
- **"Durante o expediente" virou tipo próprio**, ao lado de Abertura e
  Fechamento; "Troca de turno" continua existindo separado, pra quando o
  turno realmente muda de responsável.
- **Nenhum ponto entra exigindo foto** ("os check que EU selecionar que
  precisa de foto") — o dono marca depois em /checklist/config.

Blocos do papel que não têm tipo próprio, e onde entraram:
- "MANHÃ" (Limpeza)                → abertura
- "PADRÃO DO CAFÉ" (Café)          → durante (é conferido ao longo do dia)
- "ORGANIZAÇÃO PEPS" (Câmara Fria) → durante
- "MEIO DO DIA" (Supervisão)       → durante
- "RESPONSÁVEL" (Supervisão)       → OMITIDO: a única linha ("Gerente,
  atendente chefe ou responsável indicado pelo turno") é nota de cabeçalho
  do papel, não ponto de conferência — no sistema quem preencheu já fica
  gravado no registro.

O seed roda UMA vez (guarda em AppConfig) e NUNCA ressuscita: a partir da
importação, o cadastro do dono manda. Apagar/editar item aqui não volta.
"""
import logging

from app.extensions import db

logger = logging.getLogger(__name__)

# Chave do guard — bump no sufixo só se um dia houver um segundo lote.
CFG_SEED = 'checklist_seed_opao_v1'

# (setor, tipo, texto) na ORDEM do PDF. A `ordem` gravada é o índice desta
# lista, então os setores saem na sequência das folhas e os pontos na
# sequência de cada bloco.
ITENS_PADRAO = [
    # ── Café / Barista ────────────────────────────────────────────────
    ('Café / Barista', 'abertura', 'Ligar máquina de espresso e aguardar estabilização.'),
    ('Café / Barista', 'abertura', 'Conferir nível de água e funcionamento da máquina.'),
    ('Café / Barista', 'abertura', 'Conferir moinho e café em grãos disponível.'),
    ('Café / Barista', 'abertura', 'Fazer teste de espresso antes de liberar venda.'),
    ('Café / Barista', 'abertura', 'Conferir filtros, balança e cronômetro para coados.'),
    ('Café / Barista', 'abertura', 'Realizar primeiro coado do dia e avaliar aroma, sabor e extração.'),
    ('Café / Barista', 'abertura', 'Conferir validade dos leites, bebidas e insumos abertos.'),
    ('Café / Barista', 'abertura', 'Garantir que produtos abertos estejam identificados.'),
    ('Café / Barista', 'durante', 'Padrão do café: espresso entre 25 e 35 segundos.'),
    ('Café / Barista', 'durante', 'Padrão do café: espresso sem sabor ralo, queimado ou aguado.'),
    ('Café / Barista', 'durante', 'Padrão do café: crema uniforme e bebida dentro do padrão da casa.'),
    ('Café / Barista', 'durante', 'Padrão do café: coado feito conforme receita padrão da casa.'),
    ('Café / Barista', 'durante', 'Padrão do café: água, moagem e tempo do coado conferidos.'),
    ('Café / Barista', 'durante', 'Limpar porta-filtro após uso.'),
    ('Café / Barista', 'durante', 'Limpar vaporizador após cada bebida.'),
    ('Café / Barista', 'durante', 'Conferir padrão dos espressos ao longo do dia.'),
    ('Café / Barista', 'durante', 'Conferir padrão dos coados periodicamente.'),
    ('Café / Barista', 'durante', 'Manter recipientes de café fechados.'),
    ('Café / Barista', 'durante', 'Conferir validade e identificação dos insumos em uso.'),
    ('Café / Barista', 'fechamento', 'Realizar retrolavagem da máquina.'),
    ('Café / Barista', 'fechamento', 'Limpar grupos, vaporizadores, bandejas e bancada.'),
    ('Café / Barista', 'fechamento', 'Limpar equipamentos dos coados e higienizar balanças.'),
    ('Café / Barista', 'fechamento', 'Organizar estação para abertura do dia seguinte.'),

    # ── Chapa ─────────────────────────────────────────────────────────
    ('Chapa', 'abertura', 'Conferir temperatura da chapa.'),
    ('Chapa', 'abertura', 'Conferir validade dos frios, manteigas, geleias e complementos.'),
    ('Chapa', 'abertura', 'Conferir pães do dia.'),
    ('Chapa', 'abertura', 'Organizar pães fatiados antes do início do atendimento.'),
    ('Chapa', 'abertura', 'Conferir embalagens e insumos da praça.'),
    ('Chapa', 'abertura', 'Garantir que produtos abertos estejam identificados.'),
    ('Chapa', 'durante', 'Seguir ficha técnica dos produtos.'),
    ('Chapa', 'durante', 'Conferir padrão de montagem e apresentação.'),
    ('Chapa', 'durante', 'Higienizar utensílios periodicamente.'),
    ('Chapa', 'durante', 'Manter praça organizada e limpa.'),
    ('Chapa', 'durante', 'Conferir reposição dos insumos.'),
    ('Chapa', 'durante', 'Conferir validade dos produtos em uso.'),
    ('Chapa', 'fechamento', 'Limpeza completa da chapa.'),
    ('Chapa', 'fechamento', 'Limpeza dos utensílios.'),
    ('Chapa', 'fechamento', 'Conferir necessidade de reposição para o dia seguinte.'),
    ('Chapa', 'fechamento', 'Organizar geladeiras e estoque da praça.'),

    # ── Cozinha ───────────────────────────────────────────────────────
    ('Cozinha', 'abertura', 'Conferir temperatura das geladeiras.'),
    ('Cozinha', 'abertura', 'Conferir validade de todos os produtos.'),
    ('Cozinha', 'abertura', 'Conferir identificação dos produtos abertos e produzidos.'),
    ('Cozinha', 'abertura', 'Avaliar salada de frutas: aparência, cor, cheiro e sabor.'),
    ('Cozinha', 'abertura', 'Avaliar iogurtes: aparência, consistência, cheiro, sabor e validade.'),
    ('Cozinha', 'abertura', 'Conferir frutas para sucos: maturação, aparência e cheiro.'),
    ('Cozinha', 'durante', 'Utilizar sistema PEPS: mais antigos primeiro.'),
    ('Cozinha', 'durante', 'Manter produtos refrigerados.'),
    ('Cozinha', 'durante', 'Higienizar bancadas, tábuas e facas após uso.'),
    ('Cozinha', 'durante', 'Conferir bandejas, potes e tampas limpas antes do envase.'),
    ('Cozinha', 'durante', 'Conferir talheres antes de embalar.'),
    ('Cozinha', 'durante', 'Não embalar talheres, potes ou bandejas com resíduos, manchas ou avarias.'),
    ('Cozinha', 'durante', 'Conferir validade e identificação dos produtos em uso.'),
    ('Cozinha', 'fechamento', 'Etiquetar toda produção com fabricação e validade.'),
    ('Cozinha', 'fechamento', 'Conferir validade dos produtos armazenados.'),
    ('Cozinha', 'fechamento', 'Limpar equipamentos e bancadas.'),
    ('Cozinha', 'fechamento', 'Registrar perdas e descartes.'),

    # ── Viagem / Embalagem ────────────────────────────────────────────
    ('Viagem / Embalagem', 'abertura', 'Conferir programação do dia.'),
    ('Viagem / Embalagem', 'abertura', 'Conferir estoque de embalagens.'),
    ('Viagem / Embalagem', 'abertura', 'Conferir etiquetas e identificações.'),
    ('Viagem / Embalagem', 'abertura', 'Organizar estação de embalagem.'),
    ('Viagem / Embalagem', 'durante', 'Conferir qualidade visual dos pães: cor, formato, acabamento e assamento.'),
    ('Viagem / Embalagem', 'durante', 'Separar produtos fora do padrão.'),
    ('Viagem / Embalagem', 'durante', 'Conferir se o produto corresponde à embalagem correta.'),
    ('Viagem / Embalagem', 'durante', 'Conferir etiqueta correta, fabricação e validade.'),
    ('Viagem / Embalagem', 'durante', 'Conferir integridade da embalagem.'),
    ('Viagem / Embalagem', 'durante', 'Conferir quantidade produzida versus embalada.'),
    ('Viagem / Embalagem', 'durante', 'Conferir pedidos especiais, loja e clientes externos.'),
    ('Viagem / Embalagem', 'fechamento', 'Conferir se toda produção foi embalada.'),
    ('Viagem / Embalagem', 'fechamento', 'Organizar câmara e estoque.'),
    ('Viagem / Embalagem', 'fechamento', 'Limpar setor.'),
    ('Viagem / Embalagem', 'fechamento', 'Registrar pendências.'),

    # ── Câmara Fria / Estoque Refrigerado ─────────────────────────────
    ('Câmara Fria', 'abertura', 'Conferir temperatura da câmara fria.'),
    ('Câmara Fria', 'abertura', 'Conferir limpeza das prateleiras.'),
    ('Câmara Fria', 'abertura', 'Conferir validade de frutas, laticínios e insumos de geladeira.'),
    ('Câmara Fria', 'abertura', 'Conferir identificação dos produtos abertos e produzidos.'),
    ('Câmara Fria', 'abertura', 'Retirar produtos vencidos e comunicar responsável.'),
    ('Câmara Fria', 'durante', 'PEPS: produtos mais antigos posicionados à frente.'),
    ('Câmara Fria', 'durante', 'PEPS: produtos novos posicionados ao fundo ou abaixo da fileira.'),
    ('Câmara Fria', 'durante', 'PEPS: usar primeiro os produtos mais antigos.'),
    ('Câmara Fria', 'durante', 'PEPS: identificar produtos próximos ao vencimento.'),
    ('Câmara Fria', 'durante', 'PEPS: comunicar setores sobre produtos que precisam ser usados primeiro.'),
    ('Câmara Fria', 'fechamento', 'Conferir reposições realizadas.'),
    ('Câmara Fria', 'fechamento', 'Conferir organização das prateleiras.'),
    ('Câmara Fria', 'fechamento', 'Manter corredores livres e produtos fora do chão.'),
    ('Câmara Fria', 'fechamento', 'Registrar produtos próximos ao vencimento.'),

    # ── Caixa ─────────────────────────────────────────────────────────
    ('Caixa', 'abertura', 'Conferir fundo de caixa.'),
    ('Caixa', 'abertura', 'Testar sistema PDV.'),
    ('Caixa', 'abertura', 'Testar impressora.'),
    ('Caixa', 'abertura', 'Testar máquinas de cartão.'),
    ('Caixa', 'abertura', 'Organizar balcão e materiais de atendimento.'),
    ('Caixa', 'durante', 'Conferir pedidos antes da cobrança.'),
    ('Caixa', 'durante', 'Conferir pedidos de retirada.'),
    ('Caixa', 'durante', 'Conferir cancelamentos.'),
    ('Caixa', 'durante', 'Oferecer complementos de forma educada.'),
    ('Caixa', 'durante', 'Comunicar falhas de sistema ou divergências.'),
    ('Caixa', 'fechamento', 'Fechar caixa.'),
    ('Caixa', 'fechamento', 'Conferir dinheiro, PIX e cartões.'),
    ('Caixa', 'fechamento', 'Anexar comprovantes necessários.'),
    ('Caixa', 'fechamento', 'Assinatura do responsável pelo turno.'),

    # ── Salão ─────────────────────────────────────────────────────────
    ('Salão', 'abertura', 'Mesas limpas e cadeiras alinhadas.'),
    ('Salão', 'abertura', 'Vitrine abastecida e organizada.'),
    ('Salão', 'abertura', 'Guardanapos, açúcar e adoçante abastecidos.'),
    ('Salão', 'abertura', 'Banheiros revisados.'),
    ('Salão', 'abertura', 'Comunicação visual organizada.'),
    ('Salão', 'durante', 'Limpar mesas após saída dos clientes.'),
    ('Salão', 'durante', 'Repor insumos do salão.'),
    ('Salão', 'durante', 'Conferir vitrine e apresentação dos produtos.'),
    ('Salão', 'durante', 'Conferir banheiros periodicamente.'),
    ('Salão', 'durante', 'Manter salão organizado e acolhedor.'),
    ('Salão', 'fechamento', 'Limpeza geral do salão.'),
    ('Salão', 'fechamento', 'Organização das mesas e cadeiras.'),
    ('Salão', 'fechamento', 'Limpeza da vitrine.'),
    ('Salão', 'fechamento', 'Conferência de objetos esquecidos.'),

    # ── Limpeza ───────────────────────────────────────────────────────
    ('Limpeza', 'abertura', 'Banheiros higienizados.'),
    ('Limpeza', 'abertura', 'Piso limpo.'),
    ('Limpeza', 'abertura', 'Vidros limpos.'),
    ('Limpeza', 'abertura', 'Lixeiras vazias.'),
    ('Limpeza', 'abertura', 'Superfícies de contato limpas.'),
    ('Limpeza', 'durante', 'Conferir banheiros periodicamente.'),
    ('Limpeza', 'durante', 'Repor papel higiênico, sabonete e papel toalha.'),
    ('Limpeza', 'durante', 'Limpar maçanetas, balcões e áreas de contato.'),
    ('Limpeza', 'durante', 'Recolher lixo quando necessário.'),
    ('Limpeza', 'fechamento', 'Lavagem dos pisos.'),
    ('Limpeza', 'fechamento', 'Limpeza dos ralos.'),
    ('Limpeza', 'fechamento', 'Limpeza da área de lixo.'),
    ('Limpeza', 'fechamento', 'Registro da execução.'),

    # ── Área Externa ──────────────────────────────────────────────────
    ('Área Externa', 'abertura', 'Fachada limpa.'),
    ('Área Externa', 'abertura', 'Calçada limpa.'),
    ('Área Externa', 'abertura', 'Mesas externas limpas.'),
    ('Área Externa', 'abertura', 'Lixeira externa limpa.'),
    ('Área Externa', 'abertura', 'Comunicação visual organizada.'),
    ('Área Externa', 'durante', 'Recolher resíduos.'),
    ('Área Externa', 'durante', 'Conferir limpeza da área.'),
    ('Área Externa', 'durante', 'Conferir mesas externas.'),
    ('Área Externa', 'durante', 'Manter entrada apresentável.'),
    ('Área Externa', 'fechamento', 'Limpeza geral da área externa.'),
    ('Área Externa', 'fechamento', 'Recolher ou organizar mobiliário.'),
    ('Área Externa', 'fechamento', 'Conferir iluminação.'),
    ('Área Externa', 'fechamento', 'Fechar acessos.'),

    # ── Escritório e Forno ────────────────────────────────────────────
    ('Escritório e Forno', 'abertura', 'Conferir organização das bancadas.'),
    ('Escritório e Forno', 'abertura', 'Conferir organização das prateleiras.'),
    ('Escritório e Forno', 'abertura', 'Conferir local das chaves.'),
    ('Escritório e Forno', 'abertura', 'Conferir limpeza e funcionamento do forno.'),
    ('Escritório e Forno', 'abertura', 'Conferir assadeiras, papel manteiga e utensílios.'),
    ('Escritório e Forno', 'durante', 'Manter chaves em local definido.'),
    ('Escritório e Forno', 'durante', 'Manter bancadas e prateleiras organizadas.'),
    ('Escritório e Forno', 'durante', 'Não acumular materiais desnecessários.'),
    ('Escritório e Forno', 'durante', 'Seguir padrão de forneamento quando o forno for utilizado.'),
    ('Escritório e Forno', 'durante', 'Conferir tempo, temperatura e padrão visual dos produtos.'),
    ('Escritório e Forno', 'fechamento', 'Guardar todas as chaves.'),
    ('Escritório e Forno', 'fechamento', 'Organizar mesas, bancadas e prateleiras.'),
    ('Escritório e Forno', 'fechamento', 'Limpar forno, assadeiras e utensílios.'),
    ('Escritório e Forno', 'fechamento', 'Conferir equipamentos desligados.'),

    # ── Supervisão da Loja ────────────────────────────────────────────
    # A linha "RESPONSÁVEL" do papel ficou de fora: o sistema já grava quem
    # preencheu cada checklist.
    ('Supervisão da Loja', 'abertura', 'Salão, área externa e banheiros conferidos.'),
    ('Supervisão da Loja', 'abertura', 'Vitrine abastecida.'),
    ('Supervisão da Loja', 'abertura', 'Espresso e coado aprovados.'),
    ('Supervisão da Loja', 'abertura', 'Geladeiras e câmara fria dentro da temperatura.'),
    ('Supervisão da Loja', 'abertura', 'Produtos abertos e produzidos identificados.'),
    ('Supervisão da Loja', 'abertura', 'Validades conferidas.'),
    ('Supervisão da Loja', 'durante', 'Meio do dia: salada de frutas, iogurtes e frutas para sucos aprovados.'),
    ('Supervisão da Loja', 'durante', 'Meio do dia: café, chapa, cozinha e viagem dentro do padrão.'),
    ('Supervisão da Loja', 'durante', 'Meio do dia: câmara fria organizada no PEPS.'),
    ('Supervisão da Loja', 'durante', 'Meio do dia: produtos próximos ao vencimento identificados.'),
    ('Supervisão da Loja', 'durante', 'Meio do dia: limpeza geral dentro do padrão.'),
    ('Supervisão da Loja', 'fechamento', 'Todos os setores limpos e organizados.'),
    ('Supervisão da Loja', 'fechamento', 'Produção etiquetada e embalada.'),
    ('Supervisão da Loja', 'fechamento', 'Perdas registradas.'),
    ('Supervisão da Loja', 'fechamento', 'Escritório, chaves e forno organizados.'),
    ('Supervisão da Loja', 'fechamento', 'Loja preparada para abertura do dia seguinte.'),
]


def importar_padrao(forcar=False):
    """Cria os itens do checklist em papel da Opão. Devolve quantos criou.

    Roda UMA vez (guard em AppConfig): a partir daí o cadastro do dono manda
    — apagar ou editar item aqui NÃO ressuscita no próximo deploy. `forcar`
    ignora só o guard, nunca a deduplicação por (tipo, setor, texto), então
    re-rodar não duplica linha.
    """
    from app.models import AppConfig, ChecklistItemModelo

    if not forcar and AppConfig.get(CFG_SEED):
        return 0
    ja_existem = {
        (i.tipo, i.setor, i.texto) for i in
        db.session.query(ChecklistItemModelo.tipo, ChecklistItemModelo.setor,
                         ChecklistItemModelo.texto).all()}
    criados = 0
    for ordem, (setor, tipo, texto) in enumerate(ITENS_PADRAO):
        if (tipo, setor, texto) in ja_existem:
            continue
        db.session.add(ChecklistItemModelo(
            tipo=tipo, setor=setor, texto=texto, exige_foto=False,
            ordem=ordem, ativo=True, loja_id=None))
        criados += 1
    AppConfig.set(CFG_SEED, '1')
    db.session.commit()
    logger.info('checklist: seed do padrão Opão criou %d item(ns)', criados)
    return criados
