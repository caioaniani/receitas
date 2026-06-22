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
