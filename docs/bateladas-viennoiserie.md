# Viennoiserie: batimento compartilhado de 25 kg de farinha

Decisão do proprietário, 17/09/2026: croissants, danishes, pain au chocolat
e demais derivados dividem batimentos completos de 25.000 g de farinha da
Massa para folhar. **Não são 25 kg por produto.** O excedente aumenta primeiro
os produtos de maior necessidade líquida, seguindo para os menores.

## Planejamento

- A explosão das fichas desconta estoques e produção pendente, incluindo
  intermediários como Massa de Danish, sem contar a mesma massa duas vezes.
- A distribuição usa peças inteiras e prioridades decrescentes. Tetos,
  overrides e ordens fechadas são respeitados; eventual massa não distribuída
  fica explícita. Massa não distribuída não vira produto fictício.
- Antecipação cobre necessidades futuras do mesmo produto, dentro do limite
  cadastrado. Não manda produzir novamente o que já foi antecipado.
- A massa continua com a antecedência da ficha (24 horas em produção).
  Dias sem produção rolam para o dia permitido anterior, como no MRP existente.
- O snapshot da ordem guarda o mix destinado àquele batimento. Enviar somente
  a ordem da massa não apaga o reforço dos produtos no próximo cálculo.
- Edições manuais antigas na linha de massa precisam ser removidas; a interface
  orienta ajustar produtos e recalcular, em vez de interpretar bolas antigas
  como batimentos novos. Ordens enviadas legadas não são convertidas.

## Unidades e confirmação

Ordens novas da massa contam **batimentos**. O snapshot guarda ingredientes,
25.000 g de farinha, massa final, unidade histórica e destinos. Croissants,
danishes e pains continuam sendo registrados em unidades de produto.

Na ficha verificada na implantação: 25.000 g de farinha resultam em 44.900 g
de massa. A unidade histórica de 3.580 g é preservada: isso equivale a
12 bolas completas e 1.940 g, não a 12 ou 13 bolas arredondadas.

`SaldoResidualMassa` e `MovEstoqueMassa` são tabelas novas, criadas pelo startup
existente. Não alteram colunas antigas. O inteiro legado é uma projeção do
saldo exato e não deve ser somado novamente ao movimento em gramas. O peso
da unidade histórica fica congelado; editar ou renomear a ficha não revaloriza
saldo anterior. Contagens substituem também a fração e geram auditoria.

Somente confirmação credita massa física. Reserva de ingredientes é liberada
e convertida em consumo na mesma transação. Montagens debitam massa exata;
parciais de uma ordem usam deltas acumulados em gramas. O formulário guarda
a quantidade já produzida para recusar repetição de uma tela desatualizada.

## Segurança da publicação

- Não recalcular nem reenviar ordens existentes durante a publicação.
- Não editar receitas, estoque físico ou pedidos reais como parte do deploy.
- Validar cálculo compartilhado, estoque, reservas, perdas/estornos, contagens,
  prazo, freeze, overrides, telas e regressões do motor.
- A reversão não pode simplesmente remover o leitor de snapshots/gramas com
  ordens abertas ou estoque residual. Preservar tabelas e leitores e corrigir
  a lógica; avaliar ordens explicitamente antes de voltar a código antigo.
