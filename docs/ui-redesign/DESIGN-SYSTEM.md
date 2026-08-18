# Design system da prévia administrativa

Este sistema visual é o PADRÃO do sistema interno (cookie `ui_classic` devolve a interface anterior; `?v2=1` força a v2 mesmo pra quem optou fora). Ele cobre as
telas internas que usam `base.html`; a loja pública usa outro layout e não é
afetada.

## Princípios

1. **Atenção antes de navegação:** pendências e riscos aparecem antes dos
   atalhos e relatórios.
2. **Uma ação principal por contexto:** ações secundárias usam botão neutro,
   menu ou link.
3. **Densidade controlada:** tabelas continuam compactas, mas campos e ações
   mantêm área de toque suficiente.
4. **Cor comunica estado:** verde é ação principal/sucesso; amarelo é atenção;
   vermelho é erro ou ação destrutiva; azul é informação.
5. **A mesma gramática em todas as áreas:** página, cartão, formulário, tabela,
   alerta e navegação usam os mesmos componentes.
6. **Leitura antes da edição:** cadastros extensos mostram primeiro nome,
   estado e dados essenciais. Campos aparecem somente quando a pessoa escolhe
   editar um item.

## Tokens

| Grupo | Tokens principais | Uso |
| --- | --- | --- |
| Superfícies | `--v2-canvas`, `--v2-surface`, `--v2-surface-muted` | Fundo, cartões e áreas secundárias |
| Texto | `--v2-ink`, `--v2-muted` | Conteúdo principal e apoio |
| Ação | `--v2-accent`, `--v2-accent-soft` | Botão principal, foco e item ativo |
| Estados | `--v2-success`, `--v2-warning`, `--v2-danger`, `--v2-info` | Feedback semântico |
| Estrutura | `--v2-line`, `--v2-radius`, `--v2-shadow` | Bordas, cantos e elevação |
| Espaçamento | `--v2-space-1` a `--v2-space-6` | Escala de 4 a 32 px |

## Componentes

### Cabeçalho de página

- Use `.page-header`, `.page-title`, `.page-subtitle` e `.page-actions`.
- O título identifica a decisão ou o objeto da tela.
- A ação primária fica à direita no desktop e abaixo do título no celular.

### Botões

| Variante | Quando usar |
| --- | --- |
| `.btn-primary` | A ação que conclui ou avança o fluxo |
| `.btn-outline-secondary` | Ação de apoio, filtro ou retorno |
| `.btn-outline-danger` | Ação destrutiva que ainda pede confirmação |
| `.btn-link` | Drill-down ou ação de baixa ênfase |

Estados obrigatórios: padrão, hover, foco visível, desabilitado e carregando
quando houver operação assíncrona.

### Formulários

- Rótulo sempre visível com `.form-label`.
- Ajuda e formato esperado em `.form-text`.
- Campos desabilitados não substituem texto explicativo.
- Erros devem ficar junto ao campo e também no resumo do formulário quando a
  página for longa.

### Tabelas

- Toda tabela larga fica em `.table-responsive`.
- Cabeçalho descreve o dado; ações ficam na última coluna.
- Números comparáveis são alinhados à direita.
- No celular, preserve leitura por rolagem horizontal em vez de esmagar
  colunas.
- Grades de planejamento devem começar focadas no período mais relevante e
  oferecer a visão completa como alternativa explícita.

### Listas editáveis

- Use o padrão `mp-v2-item` para cadastros extensos: resumo em `<summary>` e
  formulário dentro de `<details>`.
- Busca e paginação acontecem antes da lista; a página deve limitar a quantidade
  de controles carregados de uma vez.
- O rodapé de salvamento explica quando a alteração passa a valer.
- Não esconda operações destrutivas no resumo: elas aparecem somente dentro do
  item aberto, com confirmação.

### Cartões e alertas

- Cartões agrupam um assunto; não devem virar botões gigantes sem descrição.
- Alertas usam cor semântica suave e texto que diga o que aconteceu e o que o
  usuário pode fazer.
- Estado vazio deve explicar se está tudo certo ou qual é o próximo passo.

### Entrada de área

`area_preview.html` é a porta de entrada comum para Lojas, Produção, Catálogo,
Vendas, Financeiro, Pessoas, Relatórios e Administração. Os links vêm de
`_area_nav.html`, a mesma fonte usada pela navegação.

## Acessibilidade

- Todo controle interativo recebe foco visível.
- Controles novos têm alvo de pelo menos 40 px no desktop e 44 px em telas de
  toque.
- Todo campo tem `<label>` associado; placeholder é apenas exemplo, nunca o
  único rótulo.
- Links que abrem outra aba usam `rel="noopener"` e ícone de saída.
- A hierarquia começa em `h1` e avança sem saltos no layout novo.
- Animações são praticamente removidas quando o sistema pede movimento
  reduzido.
- Contraste e significado não dependem apenas de cor; texto e ícone acompanham
  estados relevantes.

## Migração

Telas legadas herdam os componentes globais automaticamente no preview. Ao
editar uma tela, prefira remover estilos inline e adotar as classes acima. A
produção mantém o CSS original até que o redesenho seja aprovado e incorporado
explicitamente.
