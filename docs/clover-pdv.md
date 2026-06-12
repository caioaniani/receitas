# Caixa (PDV próprio) + Clover Mini

O caixa fica em **`/pdv/caixa`** (menu "Caixa"). Funciona já hoje, sem
nenhuma configuração: dinheiro, PIX e cartão com **captura manual** (o
operador digita o valor na Clover Mini e o caixa só registra a venda).

Com a integração ativada, ao tocar em **Débito/Crédito** o sistema manda a
cobrança direto pra maquininha — o valor aparece na tela da Clover, o
cliente paga, e o caixa recebe o resultado (aprovado/negado) sozinho.

## Como funciona

```
Navegador (loja) ──► Flask (Railway) ──► Nuvem Clover ──► Clover Mini
      ▲                    │
      └──── polling 2s ────┘   (venda + status do pagamento no Postgres)
```

- Modo **cloud** (recomendado): o servidor chama a API REST Pay Display da
  Clover (`POST /connect/v1/payments`), que repassa pra maquininha rodando
  o app **Cloud Pay Display**. Funciona com o sistema hospedado fora da
  loja, que é o nosso caso (Railway).
- Modo **local**: o servidor fala direto com a maquininha na rede local
  (app REST Pay Display, porta 12346). Só serve se o sistema rodar num
  computador dentro da loja.
- Modo **simulado**: aprova qualquer cobrança em ~4s. Use pra treinar a
  equipe e testar o fluxo de ponta a ponta sem mexer na maquininha.

A chamada de pagamento roda numa thread de background no servidor (ela
bloqueia até o cliente concluir na maquininha) e o navegador faz polling
do status da venda a cada 2 segundos.

## Variáveis de ambiente (Railway)

| Variável | Exemplo | O que é |
|---|---|---|
| `CLOVER_MODE` | `cloud` | `cloud`, `local`, `simulado` ou vazio (desativado) |
| `CLOVER_API_BASE` | `https://api.clover.com` | Cloud: produção `https://api.clover.com`, sandbox `https://sandbox.dev.clover.com`. Local: `https://IP-da-mini:12346` |
| `CLOVER_ACCESS_TOKEN` | `a1b2c3...` | Token OAuth do app criado no painel de desenvolvedor |
| `CLOVER_DEVICE_SERIAL` | `C045UQ12345678` | Número de série da Mini (Configurações → Sobre) |
| `CLOVER_POS_ID` | `OpaoPDV` | Remote Application ID (RAID) gerado no painel |
| `CLOVER_TLS_VERIFY` | `1` | `0` desliga verificação TLS (só no modo local) |

Para testar agora: defina só `CLOVER_MODE=simulado` e abra `/pdv/caixa`.

## Passo a passo do credenciamento

1. **Conta de desenvolvedor Clover** — crie em
   [docs.clover.com](https://docs.clover.com/dev/docs/home) (sandbox) e
   registre um app de semi-integração. Nele você gera o **RAID**
   (`CLOVER_POS_ID`) e obtém o token OAuth (`CLOVER_ACCESS_TOKEN`).
2. **Instale o app de Pay Display na Mini** — pelo Merchant Dashboard,
   instale **Cloud Pay Display** (modo cloud) ou **REST Pay Display**
   (modo local) e deixe-o aberto na maquininha.
3. **Anote o serial da Mini** em Configurações → Sobre.
4. Preencha as variáveis no Railway e confira o badge "Clover: conectada"
   no topo do caixa (ele usa `GET /connect/v1/device/ping`).

### ⚠️ Importante — Clover no Brasil

A operação da Clover no Brasil (Fiserv, lançada em dez/2024) processa os
pagamentos via **SiTef** e o credenciamento de integração externa é feito
direto com a Fiserv Brasil — o modelo pode diferir do fluxo americano
descrito acima (lá fora o REST Pay Display é o caminho padrão; aqui a
Fiserv classifica como "integração externa" e tem guia próprio,
"Clover-SiTef Sales App").

Antes de ir pra produção:

- Fale com o time de Developer Relations da Clover/Fiserv (**dvrel@clover.com**,
  respondem em ~5 dias úteis) ou com o gerente da conta Fiserv/adquirente
  (Sicredi, Bin etc.) pedindo o credenciamento de **integração externa de
  PDV com Clover Mini** e as credenciais/endpoint corretos para o Brasil.
- Páginas úteis: [Developer guides — LATAM Brazil](https://docs.clover.com/dev/docs/developer-guides-for-latam-brazil),
  [Clover Brasil — desenvolvedores](https://br.clover.com/desenvolvedores/),
  [REST Pay Display API](https://docs.clover.com/dev/docs/rest-pay-intro).

O serviço (`app/services/clover.py`) já isola toda a comunicação com a
maquininha: se o endpoint/headers do Brasil divergirem, o ajuste fica
restrito a esse arquivo (a resposta crua de cada pagamento fica salva em
`venda_pagamento.clover_resposta` pra facilitar a depuração).

## O que fica gravado

- `venda` — code (`V20260612-001`), loja, operador, subtotal/desconto/total,
  status (`aberta` → `paga`/`cancelada`).
- `venda_item` — snapshot de descrição e preço de cada item (preço de
  catálogo é sempre resolvido no servidor: override por loja →
  `preco_loja` → `preco_venda`).
- `venda_pagamento` — método, valor, troco, status, via de captura
  (`manual`/`cloud`/`local`/`simulado`), IDs Clover (`externalPaymentId`
  nosso + `paymentId` deles, pra conciliação) e a resposta crua da API.

Pagamento dividido é suportado (ex.: metade dinheiro, metade cartão) — a
venda fecha quando a soma dos pagamentos aprovados cobre o total.

## Limitações conhecidas (v1)

- Estorno/cancelamento de pagamento **aprovado** se faz na própria
  maquininha (e a venda, um admin cancela depois).
- O caixa não baixa o estoque da loja automaticamente (próximo passo
  natural: movimentar `estoque_loja` ao fechar a venda).
- Sem emissão de NFC-e — a nota continua saindo pela própria Clover/app
  fiscal atual.
