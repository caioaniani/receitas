# Fichas de produção (planilha) → etapas do fluxograma

Pedido do dono (02/10/2026): "cadastrar e usar essas infos no gantt/fluxograma
de produção; apagar todos os registros que o gantt usa hoje e trocar por esses
dessa planilha". A planilha é a transcrição das fichas manuscritas da padaria,
em quatro abas: **1. Batimento**, **2. Fermentação** (a "CAIXA"),
**3. Fermentação final** e **4. Forno**. Desde essa data ela é a fonte das
etapas que o fluxograma (`/padeiro/gantt`, "Ver sequência") e a TV do padeiro
mostram; o modelo genérico "padrão da categoria" (etapas pesquisadas,
`ETAPAS_PADRAO`) foi removido.

## O que cada aba vira

| Aba | Etapa gerada | Tipo | Duração | Parâmetros guardados |
| --- | --- | --- | --- | --- |
| Batimento | `Batimento` | amassadeira (máquina) | V1 + V2 (min) | velocidade_1_min, velocidade_2_min, ponto_massa, temp_ambiente_c, temp_liquido_c **ou** temp_liquido_txt (texto como "Água 4,5 / Leite 5,2"), temp_massa_final_c |
| Fermentação | `Fermentação em caixa` | câmara fria ou descanso (passiva) | "Duração (h)" se preenchida (número = horas; aceita 1:30, "2 h"); senão a soma das dobras (tempo até a última dobra), com aviso | local, temp_c, dobras_min, duracao_h, duracao_origem |
| Fermentação (coluna MESA) | `Mesa: do corte à modelagem` | descanso (passiva) | o tempo escrito (h:mm; número puro = horas) | tempo_como_escrito |
| Fermentação final | `Fermentação final` | câmara fria ou descanso (passiva) | "Tempo (min)"; sem ele, o texto ("24h", "43 min") convertido; os dois divergindo = vale o número, com aviso | local, temp_c, tempo_como_escrito, alternativas (a 2ª forma da ficha: CF × ambiente) |
| Forno | `Forno` | forno (máquina) | "Tempo (min)" | forno_c, teto, lastro, unidade_teto_lastro, alvo_interno_c |

Regras:

- **Célula em branco (ou com traço "–") não vira valor.** A chave
  simplesmente não entra no JSON e nada aparece no rótulo. Lacunas que afetam
  a duração geram `avisos` (prévia e log do seed): fermentação em caixa sem
  "Duração (h)", forno sem tempo, fermentação final sem tempo (ou com tempo
  ilegível), duração fora da faixa (0–100000 min, a mesma régua do editor).
  Na Fermentação final, a linha de um método sem tempo (ex.: "CF · –") nunca
  vira a etapa principal: a primeira linha **com tempo** manda.
- **Célula amarela = "leitura incerta"** (legenda do próprio dono): o valor
  entra, mas a chave vai em `parametros['incerto']`, o rótulo termina com
  "leitura incerta na ficha: …" e a prévia/log avisam. A planilha de
  02/10/2026 tem 12 células assim: 11 com valor (Brioche e Massa para folhar
  no batimento, temperaturas do Sourdough Tradicional, fermentação final e
  forno do Pão Francês, fermentação final do Croissant) e 1 **em branco**
  ("Duração (h)" do Pain au Chocolat) — amarela sem valor não vira
  parâmetro, só aviso ("célula amarela em branco na ficha … confira").
  Qualquer das três dobras amarela marca `dobras_min` (uma vez). Na
  Fermentação final, amarela na linha de uma **alternativa** marca
  `alternativas` no `incerto` da etapa (rótulo "leitura incerta na ficha:
  alternativa") e o aviso cita a linha da alternativa. A aba "Notas" que a
  legenda cita não veio no arquivo.
- Uma receita ganha a etapa de uma aba só quando a linha dela naquela aba tem
  dado de verdade. Na Fermentação em caixa, **"Local" sozinho não é dado**: a
  etapa de caixa só nasce com temperatura, "Duração (h)" ou dobras (linha só
  com o local gera aviso "etapa não criada"; linha só com MESA gera só a
  etapa de mesa). Receita só com a aba Forno preenchida fica com uma etapa.
  Linha repetida da mesma receita numa aba: vale a primeira, com aviso — na
  Fermentação final isso vale por **método** (segunda linha "A" do mesmo pão
  cai; "CF" e "A" continuam sendo a principal + alternativa).
- Hora acima de 24 h numa célula h:mm (MESA) chega como `datetime` de 1900:
  o openpyxl reproduz o bug do ano bissexto de 1900 do Excel (serial < 60
  ganha um dia), então a base é 1899-12-31 até 28/02/1900 e 1899-12-30 daí
  em diante — 26:00 = 1560 min, 48:00 = 2880 (teste de round-trip real).
- A receita da planilha casa com **uma** receita não arquivada por nome
  normalizado **exato** (sem acento, sem caixa, espaços colapsados).
  "Sourdough Tradicional" nunca casa "Mini Sourdough Tradicional". Nome sem
  receita ou com duas ativas fica de fora e é listado (prévia, flash,
  marcador). A **mesma** normalização agrupa as abas: "SOURDOUGH TRADICIONAL"
  no Forno é a mesma ficha de "Sourdough Tradicional" no Batimento (com
  aviso de grafia).
- As colunas são achadas pelo cabeçalho (o mais específico ganha: "Teto
  (valor)" antes de "Unidade teto / lastro"), em qualquer ordem; cabeçalho
  obrigatório ausente recusa a planilha com a mensagem da aba/coluna.
- Cada parâmetro guarda `origem` (aba e linha da planilha) para auditoria. O
  JSON passa por um **esquema fechado** (`etapas_receita.validar_parametros`):
  chave desconhecida ou tipo errado é descartada, venha da planilha ou do
  hidden do editor — o rótulo nunca levanta.
- O rótulo legível é único (`etapas_receita.resumo_parametros`): o Gantt, a
  TV, os editores, a prévia e a sonda mostram o mesmo texto, por exemplo
  `V1 14 min · V2 1 min · líquido 5,4 °C · ambiente 20 °C · massa ao fim 23 °C`
  ou `Câmara fria · 3 °C · alternativa: Ambiente · 21 °C · 6 h`. As dobras
  são **intervalos** ("dobras a cada 30, 30 e 25 min"), a mesma leitura da
  duração (soma).

## O que a aplicação faz

`fichas_producao.aplicar` (serviço, sem commit; a rota e o seed fecham a
transação). Recusa (ValueError, nada apagado, sem backup) quando nenhuma
receita da planilha casa com o cadastro; em Postgres serializa dois gestos
concorrentes com `pg_advisory_xact_lock(7768)`.

1. Guarda **todas** as `ReceitaEtapa` atuais em `AppConfig`
   (`fichas_producao_backup_<AAAAMMDD_HHMMSS>`, JSON com receita, ordem, nome,
   duração, tipo, passo a passo e parâmetros). O texto escrito pelo padeiro
   nunca se perde sem volta.
2. Apaga as etapas de **todas** as receitas (como o dono pediu) e insere as
   da planilha nas receitas que casaram.
3. Reescreve o `processo` dos snapshots de batelada
   (`PlanejamentoItemBatelada.dados['processo']`) das ordens que o Gantt ainda
   lê: data a partir de hoje − 3 dias, item com falta, não dispensado, não
   encerrado. Só as etapas mudam; quantidades, ingredientes e prazo congelados
   não. Receita fora da planilha fica com `processo` vazio nessas ordens.
4. Grava o marcador `fichas_producao_import` (quando, quem, origem, arquivo,
   sha256, contagens, não encontradas, ambíguas, chave do backup, snapshots).

## Onde se opera

- **Tela** `/receitas/fichas-producao` (Catálogo → Fichas de produção
  (planilha); admin vê, só o dono aplica): estado atual (última importação,
  etapas por receita, receitas sem etapas), upload do `.xlsx` → **prévia**
  (o que casa, o que não casa, as etapas e parâmetros que nascem, os avisos)
  → **Aplicar** com token assinado (30 min) e confirmação. Nada é gravado na
  prévia.
- **Seed de startup** `_seed_fichas_producao_2026_10` (marker
  `seed_fichas_producao_2026_10` com contagens) aplica uma vez a planilha
  embarcada em `app/seeds_data/fichas_producao_2026_10.xlsx`
  (sha256 `4b99b0bb17ee…`), pelo mesmo serviço. Depois disso a planilha nova
  entra pela tela.
- **Editores** (`/receitas/<id>/etapas` e `/padeiro/fichas/<id>`): mostram os
  parâmetros (⚙) e os preservam ao salvar (`parametros[]`); duração, tipo e
  passo a passo seguem editáveis. Para mudar um parâmetro, importe a planilha.
  O POST antigo `acao=padrao` não grava nada. "Aplicar a toda a categoria"
  copia nome/duração/tipo/passo a passo, **nunca** os parâmetros (são medidos
  por receita): as outras receitas da categoria ficam sem parâmetros até a
  próxima importação; `duplicar` copia tudo, parâmetros inclusive.
- **Sonda** `GET /api/claude/fichas-producao`: marcador da importação, marker do
  seed, receitas com etapas (com parâmetros) e as sem etapas.
  `GET /api/claude/receita` passou a trazer `etapas`.

## Decisões registradas

- Fermentação final com duas formas na ficha (sourdoughs: câmara fria 3 °C
  12 h **ou** ambiente 21 °C 6 h): a **primeira** linha da ficha é a etapa; a
  outra fica em `alternativas` e aparece no rótulo. O Gantt não ramifica.
- "Mesa: do corte à modelagem" nasce passiva (é o tempo de descanso do corte à
  modelagem). Se a padaria quiser contar o trabalho de modelar, troca o tipo
  para "Padeiro" na ficha da receita.
- Croissant Tradicional: a ficha diz "24h" a 28 °C na fermentação final; fica
  registrado literalmente (1440 min). Croissant e Pain au Chocolat não têm na
  planilha laminação, modelagem nem mise en place: no dia da ordem o
  fluxograma mostra só a espera, e o forno no dia seguinte. Acrescentar
  trabalho manual é gesto do dono na ficha.
- Receitas fora da planilha (danishes, cookies, cinnamon, minis, granola…)
  ficam **sem etapas** até alguém cadastrar na ficha ou numa planilha nova. A
  tela lista quais.

Testes: `tests/test_fichas_producao.py` (a planilha real é a fixture),
`tests/test_etapas_producao.py`, `tests/test_padeiro_fichas.py`.
