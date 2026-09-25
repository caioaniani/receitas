# Mapa de permissões

## Edição de pedidos conforme o acesso da conta (25/09/2026)

A liberação após o corte acompanha a autorização que a conta já possui para
editar pedidos: Admin/dono, perfis com operação de pedidos na web ou edição
no Copilot, e funcionários com autorização individual válida de uma loja.
Os controles de cada canal e de cada loja continuam sendo aplicados. A regra
não concede edição a contas de leitura nem remove a restrição de treinamento;
a delegação válida de uma loja conserva sua exceção limitada ao treinamento.
Ao perder o acesso à edição, a conta também perde a liberação de horário.

Essa pessoa pode editar pedidos pendentes/confirmados após o corte das 12h.
Em **toda edição**, inclusive antes do corte, deve preencher **o que está
mudando** e **por quê** (10 a 1.000 caracteres por campo). Site e Copilot
validam os mesmos campos no servidor. O Copilot deve pedir a justificativa
ao usuário, sem inventá-la. Tentar criar outro pedido para a mesma loja/data
encaminha à edição do existente, evitando alterar as quantidades por merge
sem justificativa.

Pedido e registro são gravados na mesma transação: autor, horário, loja,
data, itens/quantidades/estados/observações anteriores e novos e justificativa.
O detalhe do pedido exibe **Ajustes para aprimorar o motor**, com comparação
antes/depois. É uma base para análise; não treina nem muda automaticamente o
motor. A trilha fica em `AuditLog`, tabela lógica `pedido_ajuste_motor`.

A dispensa vale apenas para edição, não para criar, cancelar ou excluir após
o corte. Também não reabre pedidos com nota, motorista, conferência,
recebimento ou estoque movimentado, nem permite datas passadas. A automação
e usuários sem autorização continuam sujeitos ao corte.

A migração desta publicação ativa `pedido_edicao_por_perfil` em `AppConfig`
e registra a ativação como política do sistema. A tela **Usuários → Permissões
de acesso → Editar pedidos fora do prazo** mostra o acesso efetivo e explica
a regra automática; não exige concessão individual. A configuração anterior
do João é preservada para compatibilidade com a versão anterior. Ambientes
sem a política ativa continuam usando as concessões individuais legadas.

## Consulta de recebimentos de uma loja (17/09/2026)

O perfil fixo `relatorio_loja` consulta **somente** `/pedidos/relatorio`
(HTML, PDF e Excel) e fotos dos pedidos entregues/recebidos da loja vinculada.
Criar em **Usuários → Relatório de uma loja — somente leitura**, escolhendo
uma loja operacional ativa e e-mail. O sistema gera uma senha provisória e
envia o acesso, sem convite de atendimento/Chatwoot. O primeiro acesso exige
trocar a senha; em seguida abre diretamente o relatório.

`Usuario.loja_id` é obrigatório nesse perfil. O servidor recusa parâmetros
de outra loja, inclusive repetidos, e bloqueia se a loja for removida/inativada.
O vínculo não é apenas um filtro de tela. Não há acesso ao estoque, pedidos
editáveis, outras lojas, treinamento, RH, produção, site administrativo ou
ferramentas do Copilot/Slack. A matriz editável não amplia esse perfil.
Catálogo/custos globais não são carregados no HTML, inclusive em senha/erros.

Implementação: `acesso_relatorio_loja.py`, `_gate_conta`, decorator
`relatorio_pedidos_required` e escopo antes da consulta/exportação. Sem novas
colunas ou migração. Não altera o perfil `observador`, que continua multicanal,
nem os acessos existentes dos gestores. Regressões em
`tests/test_acesso_relatorio_loja.py`.

Publicação: validar isolamento HTML/PDF/XLSX/fotos, login/troca de senha,
criação e navegação clássica/v2, testes completos e CI antes de criar a conta.
Se houver falha de isolamento, suspender a conta (remover vínculo de loja)
e corrigir antes de liberar; não trocar para gerente/observador como contorno.
Uma reversão de código deve manter o bloqueio global enquanto existir conta
com esse perfil; código antigo não conhece a restrição.

---

Quem pode fazer o quê, por papel — web (rotas/sidebar) e copilot/Slack.
Fonte da verdade: `app/models/auth.py` (predicados), `app/decorators.py`
(decorators de rota) e `app/services/copilot.py` (`PAPEIS_POR_TOOL`).

Última auditoria/ajuste: 2026-05-28.

## Papéis

`Usuario.papel` (`app/models/auth.py:21`): `funcionario` (default), `gerente`,
`producao`, `padeiro`, `rh`, `admin`. Mais a flag `Usuario.is_owner`
(`auth.py:23`) — o **owner** é um `admin` com `is_owner=True` (dono único).

## Predicados (`app/models/auth.py:36-78`)

| Predicado | Verdadeiro para | Áreas (docstring) |
|-----------|-----------------|-------------------|
| `is_admin()` | `papel=='admin'` **ou** owner | tudo |
| `is_gerente()` | `papel=='gerente'` | — |
| `is_producao()` | `papel=='producao'` | — |
| `is_padeiro()` | `papel=='padeiro'` | tela touchscreen do padeiro |
| `is_rh()` | `papel=='rh'` | — |
| `is_dono()` | `is_owner==True` | owner / áreas pessoais |
| `pode_lojas()` | admin **ou** gerente | Pedidos, Estoque Loja, Relatório |
| `pode_producao()` | admin **ou** produção | Plano de Produção, Congelados, Separação |
| `pode_catalogo()` | admin **ou** produção | Receitas, MP, Produtos, Fornecedores (leitura + estoque de MP) |
| `pode_rh()` | admin **ou** rh | RH |
| `pode_pdv()` | admin | PDV, Seru, VNDA, Mapeamentos |

## Acesso por área (web)

| Área | Quem acessa | Gate |
|------|-------------|------|
| Catálogo — **leitura** (ver receitas/MP/produtos/fornecedores) | admin + produção | `@catalogo_required` (rotas GET) |
| Catálogo — **escrita de definições** (criar/editar/excluir receita, MP, produto, fornecedor; preços/famílias/reaproveitável/upload de imagens em lote) | **admin** | `@admin_required` — *ajustado 2026-05-28* |
| Catálogo — **estoque de MP** (entrada/saída/OCR/alertas) | admin + produção | `@catalogo_required` |
| Salvar ficha de receita (`receitas/routes.py:287`) | admin **ou** dono da ficha atribuída | guard no corpo (`is_admin()` ou `Atribuicao`) |
| Pedidos / Estoque Loja / Relatório | admin + gerente | `@gerente_required` (= `pode_lojas`) |
| Produção (Plano, Congelados, Separação) | admin + produção | `pode_producao` |
| PDV / Seru / VNDA / Mapeamentos | **admin** | `pode_pdv` |
| Entregas | admin **ou** usuário com `loja_id` | `@entrega_access_required` (`decorators.py:91-98`) |
| RH (ponto, férias, cargos) | **owner** | guard temporário `rh/routes.py:30-37` (`is_dono`) |
| RH — salário | **owner** | `@owner_required` (independente do guard) |
| Usuários (gerenciar) | admin | `@admin_required` |
| Áreas pessoais (Vida/Igreja) | owner | `is_dono` |

> **Entregas + `loja_id`**: hoje todos os usuários estão "sem loja"
> (`loja_id` NULL), então `@entrega_access_required` libera apenas admin na
> prática. Ver nota 4.

## Tools do copilot / Slack (`PAPEIS_POR_TOOL`, `copilot.py:674`)

O bot do Slack reusa as mesmas tools, então a tabela vale para os dois.

| Faixa | Tools |
|-------|-------|
| **owner** | `marcar_ponto`, `consultar_funcionario` |
| **admin** | `criar_fornecedor`, `consultar_margem`, `balanco_congelados`, `entrada_lote_loja`, `enviar_digest_whatsapp`, `criar_cliente_b2b`, `criar_venda_b2b` |
| admin + gerente | `criar_pedido`, `editar_pedido`, `receber_mp`, `ajuste_estoque`, `consultar_fornecedores`, `consultar_caixa`, `consultar_vendas_itens`, `consultar_cliente_b2b` |
| admin + gerente + produção | `mudar_status_pedido` |
| admin + gerente + funcionário | `consultar_desperdicio`, `registrar_desperdicio`, `registrar_desperdicio_lote`, `anexar_foto_pedido`, `receber_pedido`, `consultar_pedido`, `consultar_estoque`, `consultar_foco`, `consultar_tarefas`, `criar_tarefa`, `marcar_tarefa_feita` |

Regras do motor (`copilot.py:713-730`):
- `papel_efetivo()`: owner → `'owner'`; admin/owner → `'admin'`; gerente →
  `'gerente'`; demais → `'funcionario'`.
- **owner é superconjunto de admin**: passa em tudo que admin passa, mais as
  tools marcadas exclusivamente `{'owner'}`.
- Tool **não mapeada** = só admin (princípio do menor privilégio).

## Notas — ajustes de 2026-05-28

1. **Copilot RH → owner.** `marcar_ponto` e `consultar_funcionario` passaram de
   `{admin, gerente}` para `{owner}`, espelhando o RH web (owner-only). Exigiu
   um tier `owner` novo no motor (`papel_efetivo`/`pode_usar`). Regressão em
   `tests/test_copilot_permissoes.py`.
2. **Catálogo: escrita de definições → admin.** As rotas de criar/editar/excluir
   (receita, MP, produto, fornecedor) e as telas em lote (preços, famílias,
   reaproveitável, upload de imagens) trocaram `@catalogo_required` por
   `@admin_required`. Produção mantém **leitura** e **estoque de MP**. Antes,
   produção conseguia gravar definições apesar do "read-only" do comentário.
3. **Papel `rh` vestigial (sem mudança de código).** O guard temporário em
   `rh/routes.py:30-37` tranca o RH inteiro no owner; enquanto existir,
   `papel='rh'` não acessa nada (web nem copilot, que colapsa `rh`→
   `funcionario`). É intencional e reversível ("Reverter: remover este guard +
   trocar `is_owner` por `pode_rh()` na sidebar"), então o papel **não** foi
   removido — só documentado aqui.
4. **`loja_id` (loja vinculada).** A coluna `Usuario.loja_id` e o relacionamento
   continuam no banco, mas o seletor "Loja vinculada" saiu da tela de usuários e
   a rota `alterar_loja` foi removida (mudança só de UI). A lógica que lê
   `loja_id` segue dormente: `@entrega_access_required` (`decorators.py:95`),
   `pedidos/routes.py:84-91` e `170-177` (código morto — as rotas são
   `@gerente_required` e admin/gerente são "pode_qualquer_loja") e o dropdown do
   copilot (`copilot/routes.py:191`). Reverter = re-adicionar o seletor + a rota.
