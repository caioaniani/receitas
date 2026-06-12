# Comandas por setor (chapa, café, cozinha, viagem)

Quando uma venda do caixa (`/pdv/caixa`) é **paga**, os itens são agrupados
pelo setor de produção de cada um e cada setor com impressora cadastrada
recebe sua comanda na hora — pão na chapa sai na térmica da chapa, café
com leite sai na do café. O setor especial **`caixa`** recebe um cupom de
conferência da venda inteira (não fiscal — a NFC-e continua pela
Seru/Clover até o módulo fiscal próprio existir).

## Configuração (admin)

Tudo em **Caixa → ⚙ Configurar** (`/pdv/caixa/config`):

1. **Impressoras**: cadastre cada térmica com loja, setor, IP e porta
   (padrão 9100). Largura 80mm = 48 colunas, 58mm = 32. Use o botão de
   teste pra confirmar que sai papel.
2. **Setor de cada item**: atribua o setor de receitas/produtos em lote.
   Item sem setor não gera comanda (ex: pão pra viagem direto do balcão).

## Impressoras Jetway

O sistema fala **ESC/POS via TCP raw (porta 9100)** — o padrão das
térmicas de rede, incluindo as Jetway com interface Ethernet/Wi-Fi.

- Modelo **só USB**: ligue no computador da loja e compartilhe como
  impressora RAW de rede (print server), apontando o cadastro pro IP
  desse computador.
- O texto é normalizado sem acentos de propósito — evita lixo por
  diferença de codepage entre modelos.

## Comportamento e falhas

- A impressão roda em background logo após o pagamento aprovar (não
  atrasa o caixa). Cada tentativa fica registrada em `venda_impressao`.
- Se uma térmica falhar (papel, rede), a venda do dia mostra o botão de
  impressora em vermelho — um toque **reimprime** (dá pra reimprimir
  setores específicos via API: `POST /pdv/caixa/api/vendas/<id>/imprimir`
  com `{"setores": ["chapa"]}`).
- A comanda sai com o número da venda e hora em letra grande, itens em
  altura dupla e a observação da venda.

## Importante: onde isso roda

Impressora de rede da loja só é alcançável por um servidor **dentro da
loja**. No Railway (nuvem) as comandas vão registrar erro de conexão — o
recurso foi desenhado para o **servidor local por loja** (próxima fase:
sync loja↔nuvem; depois, emissão fiscal própria para substituir a Seru).
Pra testar o fluxo num computador da loja: clonar o repositório, rodar
`python run.py` e cadastrar as impressoras com os IPs reais.
