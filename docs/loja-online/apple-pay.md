# Apple Pay na loja — plano (22/06/2026)

Decisão (22/06): **link agora, nativo depois.**

## Confirmado pelo suporte Pagar.me
- O Pagar.me **processa Apple Pay por padrão** na conta (lado do
  processamento pronto).
- ⚠️ O atendente viu a integração ANTIGA (Olist/VNDA) e mandou "verificar
  com a Olist". **IGNORAR** — saímos do VNDA no cutover; hoje a loja
  chama a API v5 do Pagar.me direto (`pagarme.py`). A plataforma somos nós.

## Caminho "AGORA" — Link de Pagamento (hospedado pelo Pagar.me)
A página de pagamento hospedada do Pagar.me já tem Apple Pay + Pix + cartão.

- **B2B orçamento (zero código, dá pra usar HOJE)**: atendente gera o link
  no painel Pagar.me pro valor do orçamento e manda junto com o PDF.
- **Loja varejo (automatizado, precisa build)**: criar Order v5 com
  `payment_method: 'checkout'` (hosted checkout) que devolve `payment_url`;
  redirecionar o cliente. Mesmo `_post_order` que já usamos pra Pix/cartão.
  Webhook `order.paid` já cai no nosso sistema (casa por `code`).
  **Validar o payload do `checkout` em SANDBOX antes de produção**
  (dinheiro — não subir payload chutado pra prod).

## Caminho "DEPOIS" — Apple Pay NATIVO embutido (doc Pagar.me)
Fluxo (doc oficial): usuário → loja checa elegibilidade do dispositivo →
exibe botão → seleciona Apple Pay → loja inicia sessão com Apple Pay →
device retorna payload CRIPTOGRAFADO → loja repassa pro Pagar.me → Pagar.me
DECRIPTOGRAFA e processa.

**5 etapas (doc Pagar.me)**:
1. Credenciamento na Apple Pay (conta Apple Developer + merchant ID +
   verificação de domínio via `/.well-known/apple-developer-merchantid-domain-association`).
2. Integração Checkout × Apple Pay (`window.ApplePaySession` no front; só
   renderiza em Safari/iOS).
3. Habilitação na Stone (abrir pedido pra Stone liberar a modalidade).
4. Integração Cliente V5 × Apple Pay (criar Order mandando o `paymentData`
   criptografado do token Apple Pay).
5. Testes e homologação.

### Detalhes técnicos do nativo (doc Pagar.me, etapas 2-4)
- **ETAPA 2 (frontend)**: usar `ApplePaySession` (self-service, doc da Apple).
  Ao iniciar a sessão, indicar **só Visa e Mastercard** (bandeiras do
  credenciador Stone). A Apple devolve um JSON; os campos usados depois são
  **`paymentData.data`** e **`ephemeralPublicKey.header.EphemeralPublicKey`**.
- **NÃO decriptografar** o payload da Apple no nosso lado — a doc é explícita:
  "esse trabalho é feito pela API do Pagar.me". A gente só repassa o token.
- **ETAPA 3 (Stone)**: habilitar na página "Gestão de Certificados" do
  Pagar.me (upload do .CER gerado na ETAPA 1).
- **ETAPA 4 (backend v5)**: criar Order no método **cartão** (`credit_card`)
  com as especificidades da Apple (o token criptografado no lugar do
  card_token). Mesmo `_post_order` que já usamos.

### Bloqueios REAIS do nativo (não são código — são você)
- **ETAPA 1**: credenciamento Apple → conta Apple Developer (US$ 99/ano) +
  geração do certificado `.CER`.
- **ETAPA 3**: habilitação na Stone (upload do cert + pedido).
Sem essas duas, o código (etapas 2+4) não funciona. O LINK não tem esses
gates — por isso é o "agora".

Custo extra do nativo: conta Apple Developer (US$ 99/ano). Esforço de código:
~2-3 dias (etapas 2+4) + esperas externas (Apple credenciamento, Stone, homolog.).

**Quando fazer**: só se o Apple Pay (via link) trouxer volume que justifique
a UX embutida.

---

## ▶️ PLANO DE EXECUÇÃO DO NATIVO (doc completo lida em 22/06)

Decisão: o link foi REMOVIDO (não mostra Apple Pay sem credenciamento — só
Cartão/Pix, e ainda re-pedia endereço). O nativo é o único caminho real.
"Com calma" porque tem gates externos que levam dias.

### Caminho crítico = VOCÊ (Caio). Comece por aqui:
**ETAPA 1 — Credenciamento Apple** (doc Pagar.me, exato):
1. Conta **Apple Developer** (US$ 99/ano).
2. **Merchant Identifier** no portal Apple (Certificates, Identifiers &
   Profiles → Identifiers → Merchant IDs).
3. **Certificado de processamento**: PEDIR o `.CSR` pro Pagar.me
   (relacionamento@pagar.me / chat dashboard / 4004-1330) → subir no Apple
   → baixar o `.CER`. ⚠️ Esse passo do `.CSR`-do-Pagar.me é o NÃO-óbvio.
4. **Registro de domínio (web)**: no Merchant ID, Add Domain
   `www.padariaartesanalonline.com.br` → baixar o arquivo de verificação →
   (eu sirvo no `.well-known/`) → Verify.
5. **Certificado de identidade do comerciante** (.cer) — pro handshake de
   merchant validation do Apple Pay JS.

**ETAPA 3 — Habilitação na Stone**: página "Gestão de Certificados" no
dashboard Pagar.me (subir o cert). Só Visa e Mastercard (limite Stone).

### O que EU faço (código), conforme os artefatos chegam:
- **`.well-known/apple-developer-merchantid-domain-association`**: rota Flask
  servindo o arquivo de verificação (me manda o conteúdo do passo 4).
- **ETAPA 2 (frontend)**: botão Apple Pay (só renderiza em Safari/iOS com
  Apple Pay disponível), `ApplePaySession`, captura `paymentData.data` +
  `ephemeralPublicKey.header.EphemeralPublicKey`. NÃO decriptografar (a doc
  é explícita: o Pagar.me decripta).
- **Merchant validation endpoint**: handshake server-side com a Apple usando
  o cert de identidade (passo 5).
- **ETAPA 4 (backend v5)**: criar Order no método cartão com as
  especificidades Apple. ⚠️ Falta o doc exato da ETAPA 4 (link "clique aqui"
  da doc) pro payload preciso — pegar essa página OU partir do
  `criar_pedido_cartao` que já temos.
- **ETAPA 5**: testes + homologação com o Pagar.me.

### Ordem (sem retrabalho):
1. Você: ETAPA 1 (Apple + .CSR Pagar.me + domínio) + ETAPA 3 (Stone).
2. Em paralelo: eu deixo a rota `.well-known` pronta + o scaffold do botão.
3. Você me passa: arquivo de verificação de domínio, Merchant ID, e
   confirma Stone habilitado.
4. Eu fecho ETAPA 2 + 4 + merchant validation, e homologamos.

Custo: US$ 99/ano (Apple). Esforço meu: ~2-3 dias quando os certs existirem.

---

## 🔧 MEU PASSO A PASSO (código) — Claude

Documentado em detalhe pra qualquer sessão futura do Claude continuar
exatamente daqui. NÃO começar a codar antes de o Caio entregar todos os
artefatos do bloco "ARTEFATOS NECESSÁRIOS" abaixo — senão é retrabalho.

### Pré-requisitos (artefatos que o Caio entrega)
1. **Arquivo de verificação de domínio** (`apple-developer-merchantid-domain-association`).
   Conteúdo é um JSON estático que a Apple gera no portal. Eu sirvo em
   `https://www.padariaartesanalonline.com.br/.well-known/apple-developer-merchantid-domain-association`.
2. **Merchant Identifier** completo (string, ex: `merchant.com.opao.web`).
   Vai no `merchantIdentifier` da `ApplePayPaymentRequest`.
3. **`.CER` Apple Pay Payment Processing Certificate** — entregue **DIRETO
   AO PAGAR.ME** (Stone "Gestão de Certificados" no dashboard).
   Eu NÃO uso esse cert no código; é o Pagar.me que decriptografa o token.
4. **`.cer` Merchant Identity Certificate + chave privada** (.key/.p12).
   Esse SIM eu uso no servidor — é o cert client-side pro **merchant
   validation** (handshake com a Apple). Caio guarda no Railway como
   env var (cert + chave em base64 ou path em secret file).
5. **Confirmação que a Stone habilitou** (Caio testa: aparece "Apple Pay"
   no checkout do Pagar.me).
6. **Doc da ETAPA 4** (Pagar.me "Integração Cliente V5 x Apple Pay") —
   o link "clique aqui" da doc. Sem ela, o payload exato do `payment_method`
   vira chute. Caio cola aqui ou no `docs/loja-online/apple-pay.md`.

### Sequência de implementação

**Bloco A — Rota de verificação de domínio (faz primeiro, é inerte)**
- Arquivo: `app/blueprints/loja/routes.py`.
- Rota: `GET /.well-known/apple-developer-merchantid-domain-association`.
- Body: conteúdo literal do arquivo entregue pelo Caio (servir como
  `text/plain`, sem CSP/CSRF, sem auth). Liberar no `_gate_acesso` (usar
  `request.path` em vez de endpoint).
- Onde guardar o conteúdo: env var `APPLE_DOMAIN_ASSOCIATION` (string) ou
  arquivo em `app/static/.well-known/`. Env é mais fácil (sem mexer no
  Dockerfile pra incluir estático novo).
- Teste: `curl -s -o /dev/null -w '%{http_code} %{content_type}'
  https://www.padariaartesanalonline.com.br/.well-known/apple-developer-merchantid-domain-association`
  → `200 text/plain`. **A Apple roda esse teste no "Verify"** — se 404
  ou content-type errado, falha verificação.

**Bloco B — Frontend (ETAPA 2 da doc Pagar.me)**
- Arquivo: `app/templates/loja/pagamento.html` + JS inline (ou
  `app/static/loja/apple_pay.js`).
- Detecção de elegibilidade:
  ```js
  if (window.ApplePaySession && ApplePaySession.canMakePayments()) {
    // mostrar botao
  }
  ```
  Botão renderiza SÓ em Safari/iOS com cartão Apple Pay configurado.
- Criar `PaymentRequest` com:
  - `countryCode: 'BR'`, `currencyCode: 'BRL'`
  - `merchantCapabilities: ['supports3DS']`
  - **`supportedNetworks: ['visa', 'masterCard']`** (Stone — confirmar Visa
    e Mastercard; doc diz isso literal)
  - `total: {label: 'O Pão Padaria Artesanal', amount: '<valor>'}`
  - Campos opcionais: `requiredBillingContactFields`, `requiredShippingContactFields`.
    Como já coletamos endereço no checkout, posso pular billing/shipping —
    confirmar com Caio.
- Eventos do `ApplePaySession`:
  - `onvalidatemerchant` → POST `/loja/apple-pay/validate-merchant` com a
    `validationURL` recebida. Backend faz handshake (Bloco C) e devolve
    a sessão JSON pro `session.completeMerchantValidation(sessionJson)`.
  - `onpaymentauthorized` → POST `/loja/apple-pay/pagar/<codigo>` com o
    `payment.token` (paymentData inteiro). Backend processa (Bloco D) e
    devolve `STATUS_SUCCESS` ou `STATUS_FAILURE` pra
    `session.completePayment(result)`.
- Em sucesso, JS faz `window.location.href = url do pedido_status`.

**Bloco C — Merchant validation endpoint (backend)**
- Arquivo: `app/services/apple_pay.py` (novo).
- Função `validar_merchant(validation_url) -> dict`:
  - POST pra `validation_url` (vem da Apple, valor varia: produção é
    `apple-pay-gateway.apple.com`, sandbox é outro).
  - Headers: `Content-Type: application/json`.
  - Body: `{'merchantIdentifier': MERCHANT_ID,
    'displayName': 'O Pão Padaria Artesanal',
    'initiative': 'web',
    'initiativeContext': 'www.padariaartesanalonline.com.br'}`.
  - **Client cert + chave privada** (Merchant Identity Certificate):
    passar pra `requests.post(..., cert=(cert_path, key_path))`. Em prod,
    montar os arquivos em /tmp a partir de env vars (`APPLE_PAY_CERT_PEM`,
    `APPLE_PAY_KEY_PEM`) no startup.
  - Devolve o JSON exato que a Apple retornar (sem manipular).
- Rota: `POST /loja/apple-pay/validate-merchant`
  (em `app/blueprints/loja/routes.py`). CSRF exempto (chamada de JS, mesmo
  origin). Liberar no `_gate_acesso`.
- Erros possíveis: cert inválido (401), domain mismatch
  (initiativeContext ≠ domínio registrado no Merchant ID). Logar e
  devolver 502 com mensagem.

**Bloco D — Backend de pagamento (ETAPA 4 da doc Pagar.me)**
- Arquivo: `app/services/pagarme.py`.
- Função `criar_pedido_apple_pay(pedido, payment_data)` — espelhar a
  `criar_pedido_cartao` mas trocando o `credit_card.card_token` pelo
  payload Apple. Estrutura provável (a confirmar com a doc da ETAPA 4):
  ```python
  payload['payments'] = [{
      'payment_method': 'credit_card',
      'amount': _centavos(pedido.valor_total),
      'credit_card': {
          'operation_type': 'auth_and_capture',
          'installments': 1,
          'statement_descriptor': 'O PAO PADARIA',
          'wallet': {
              'type': 'apple_pay',
              'apple_pay': {
                  'payment_data': payment_data['paymentData']['data'],
                  'ephemeral_public_key': payment_data['paymentData']
                      ['header']['ephemeralPublicKey'],
                  'public_key_hash': payment_data['paymentData']['header']
                      ['publicKeyHash'],
                  'transaction_id': payment_data['paymentData']['header']
                      ['transactionId'],
                  'version': payment_data['paymentData']['version'],
                  'signature': payment_data['paymentData']['signature'],
              },
          },
          'card': {'billing_address': _billing_address(billing)},
      },
  }]
  ```
  ⚠️ **Os nomes exatos dos campos (snake_case vs camelCase, "wallet" vs
  "apple_pay" no top-level) só ficam certos com a doc da ETAPA 4.**
  Sandbox confirma rapidinho: erro 422 mostra qual campo o Pagar.me não
  reconheceu.
- Função `loja_pagamento.iniciar_apple_pay(pedido, payment_data)` — espelha
  `iniciar_cartao`, mas cria `PagamentoOnline(metodo='apple_pay')`.
- Rota `POST /loja/apple-pay/pagar/<codigo>` (em routes.py).

**Bloco E — Configuração (Railway)**
Env vars novas:
- `APPLE_DOMAIN_ASSOCIATION` — conteúdo do arquivo de verificação (string
  inteira; Apple não exige newline final).
- `APPLE_PAY_MERCHANT_ID` — ex: `merchant.com.opao.web`.
- `APPLE_PAY_CERT_PEM` — Merchant Identity Cert em PEM (multilinha; usar
  Railway "Raw Editor" pra colar).
- `APPLE_PAY_KEY_PEM` — Chave privada do Merchant Identity Cert em PEM.
- `APPLE_PAY_DISPLAY_NAME` — `O Pão Padaria Artesanal` (texto que aparece
  no sheet do Apple Pay).

**Bloco F — Testes**
- Mock do `_post_order` igual aos outros testes de pagamento; afirmar
  shape do payload (campos da `wallet.apple_pay`).
- Mock do `requests.post` pra `validar_merchant`; afirmar que mandou
  `merchantIdentifier`/`initiativeContext` corretos.
- Teste de `_gate_acesso`: `.well-known/...` retorna 200 mesmo com
  `LOJA_VISIVEL=0` (a Apple precisa baixar o arquivo de qualquer host
  público — confirmar isso na doc).

**Bloco G — Homologação (ETAPA 5)**
- Sandbox Pagar.me com cartão de teste Apple Pay (a Apple tem cartões
  de sandbox em https://developer.apple.com/apple-pay/sandbox-testing/).
- Caio testa num iPhone real entrando em produção (porque sandbox da
  Apple só funciona em conta de developer dele).
- Pagar.me homologa formalmente (pode pedir um log de transação OK).

### Ordem dos commits (pra deploy seguro)
1. **`.well-known`** sozinho (rota + env var) — inerte, pode ir agora se
   Caio mandar o arquivo.
2. **Backend validate-merchant + pagar** (Blocos C+D) com feature flag
   `APPLE_PAY_ENABLED=0` por default — código vai pra prod mas inerte.
3. **Frontend** (Bloco B) atrás da mesma flag.
4. Smoke test em prod com a flag manual; depois `APPLE_PAY_ENABLED=1`.

### Riscos / pegadinhas
- **Apex sem www**: o redirect Cloudflare apex→www quebra Apple Pay se a
  Apple iniciar sessão no apex (cliente clica num link sem www). Garantir
  que o domínio registrado no Apple é o `www.padariaartesanalonline.com.br`
  e o JS detecta `location.hostname` antes de iniciar.
- **CSP**: o Apple Pay JS sheet roda em iframe controlado pela Apple, não
  precisa de CSP nossa. Mas o nosso JS chama `ApplePaySession` global —
  ok.
- **CORS**: validate-merchant é same-origin, sem CORS.
- **Replay**: o `payment.token` da Apple é one-time-use; idempotência
  por `pedido.codigo` (já temos).
