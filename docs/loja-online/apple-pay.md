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
