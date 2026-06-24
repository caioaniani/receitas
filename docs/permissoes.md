# Mapa de permissões

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
