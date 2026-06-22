# Transferência do domínio `padariaartesanalonline.com.br` (Wix/TOWEB → Registro.br + Cloudflare)

> Playbook executável. Objetivo: tirar o domínio do controle do Wix
> (que trava nameservers e bloqueia o apex), levar pro controle direto
> do titular no Registro.br, e apontar NS pro Cloudflare — resolvendo
> o apex de forma definitiva e canônica.
>
> Criado em 22/06/2026. Atualizar conforme cada etapa for executada.

## Visão geral

| Aspecto | Valor |
|---|---|
| Titular | caio antinhani (CPF do dono) |
| Registrador atual | TOWEB-BRASIL (parceiro Wix) |
| Registro feito em | 10/04/2020 |
| Expira | 10/04/2027 |
| Nameservers hoje | ns8/ns9.wixdns.net (travados) |
| Nameservers alvo | anderson.ns.cloudflare.com / uma.ns.cloudflare.com |
| Prazo total | 1-3 dias úteis (Registro.br processa rápido; propagação ~horas) |
| Custo | R$ 0 — transferência de provedor Registro.br é gratuita |
| Risco pro `www` | quase zero se a ordem for respeitada |

## Diferencial do `.com.br`

Em `.com.br` o **titular é soberano**: você loga no Registro.br com o seu
CPF e troca o provedor de serviços sem precisar de aprovação do TOWEB/Wix.
Isso é diferente de domínios `.com` (gTLD), onde precisa de "EPP code"
liberado pelo registrador antigo. Aqui o caminho é mais direto.

## Pré-condições (✅ todas atendidas)

- [x] Domínio criado há mais de 60 dias (10/04/2020).
- [x] Titularidade não foi alterada nos últimos 60 dias.
- [x] Você sabe o **CPF do titular** (o que aparece no WHOIS como
  "caio antinhani").
- [x] Você tem acesso ao **e-mail do titular** (pode receber confirmações).
- [x] Zona no Cloudflare já criada (apenas pendente).

## ⚠️ Regra de ouro

**Trocar nameservers é A ÚLTIMA coisa.** Antes disso, a zona Cloudflare
tem que estar 100% pronta. O `www` em produção fica respondendo pelo Wix
DNS até o NS propagar pro Cloudflare — então qualquer falta de registro
no Cloudflare aparece como queda do `www` na hora da virada.

## Etapa 1 — Cloudflare: zona pronta

**O que tem que existir antes de trocar NS** (conferir em DNS Records):

| Type | Name | Content | Proxy |
|---|---|---|---|
| CNAME | `www` | `s8kr0sma.up.railway.app` | DNS only (cinza) |
| A | `@` (apex) | `192.0.2.1` (dummy) | Proxied (laranja) |
| TXT | `_railway-verify.www` | `railway-verify=092601af...` (valor do Railway) | n/a |
| TXT | `@` | `v=spf1 include:_spf.google.com ~all` | n/a (opcional, sem efeito sem MX) |

**Em Rules → Redirect Rules**:
- Apex `padariaartesanalonline.com.br` → `https://www.padariaartesanalonline.com.br`
  (301, preserve query string).

Status: 🟡 a ajustar (deletar o A do VNDA antigo, criar o A dummy + Redirect Rule, adicionar o TXT railway-verify).

## Etapa 2 — Registro.br: criar conta e reivindicar o domínio

1. Acessar https://registro.br.
2. **Criar conta** com o **MESMO CPF** que aparece no WHOIS (caio antinhani).
   - Se já existir conta com esse CPF, fazer login.
   - Senão, cadastrar (precisa do CPF, e-mail e dados do titular).
3. Após login, ir em **"Domínios"**. O domínio
   `padariaartesanalonline.com.br` deve aparecer automaticamente vinculado
   ao CPF — independente de hoje estar gerenciado pelo TOWEB.
4. Se NÃO aparecer: significa que o CPF informado no Wix/WHOIS é
   diferente do que você está usando pra logar. Conferir com calma; pode
   precisar de "Esqueci minha senha" pelo CPF do titular.

## Etapa 3 — Trocar o provedor para "Registro.br" (autogestão)

1. Na lista de domínios, clicar em `padariaartesanalonline.com.br`.
2. Procurar a seção **"Provedor de Serviços"**.
3. Clicar em **"Selecionar Provedor"** (ou "Alterar provedor").
4. Escolher **"Registro.br"** (autogestão — você gerencia sozinho).
5. Ler os termos, confirmar.

⏱️ **Efeito imediato**: o domínio sai do controle do TOWEB e passa pra
sua autogestão no Registro.br. **Nada de DNS muda nesse passo** — os NS
continuam ns8/ns9.wixdns.net e o `www` continua respondendo igual. Esse
é o ponto seguro do processo: você ganha o controle administrativo sem
mexer no DNS ainda.

⚠️ O Wix vai descobrir nas próximas horas que perdeu o gerenciamento.
A conta Wix continua funcionando, mas a seção "Domínios" do Wix pode
mostrar o domínio como "desconectado" ou pedir pra reconectar. Ignorar.

## Etapa 4 — Apontar os nameservers pro Cloudflare

Já no Registro.br, com o domínio sob sua gestão:

1. Na página do domínio, procurar **"Servidores DNS"** (ou "Nameservers"
   ou "DNS").
2. Substituir os 2 atuais (`ns8.wixdns.net` / `ns9.wixdns.net`) pelos do
   Cloudflare:
   - `anderson.ns.cloudflare.com`
   - `uma.ns.cloudflare.com`
3. Salvar.

⏱️ **Propagação**: ~30 min a algumas horas pra `.com.br`. Resolvedores
do mundo todo vão progressivamente perguntar pro Cloudflare em vez do
Wix. O `www` (CNAME → Railway) continua respondendo igual durante a
transição, sem queda.

4. No painel Cloudflare, clicar **"I updated my nameservers" → Check
   nameservers now**. Quando ele detectar, marca o site como **"Active"**.

## Etapa 5 — Validar

Após "Active" no Cloudflare:

1. Testar `https://www.padariaartesanalonline.com.br/` → loja carrega ✅
2. Testar `https://padariaartesanalonline.com.br/` (sem www) → faz 301
   pra `www`, com cadeado válido ✅
3. Testar `http://padariaartesanalonline.com.br/` → redireciona pra https + www ✅
4. Conferir certificado SSL do apex no navegador (deve ser do Cloudflare
   ou Let's Encrypt, emitido automaticamente).

## Etapa 6 — Limpeza (depois de tudo OK por 48h)

1. **NÃO apagar a zona no Wix por 48h** — fallback caso queira reverter
   NS rapidamente.
2. Após 48h estável, pode remover a zona no Wix (Domínios → Gerenciar →
   Remover) — mantém só o **registro do domínio** (que foi pro
   Registro.br) e a **zona** no Cloudflare.
3. No final, a conta Wix do dono não tem mais NADA desse domínio.

## Rollback (se algo der errado)

Plano de reversão em qualquer etapa:

- **Antes da Etapa 4**: nada mudou no DNS — basta cancelar o processo, o
  Wix continua sendo o DNS. Voltar o provedor pro TOWEB no Registro.br
  (mesma tela da Etapa 3, escolher TOWEB).
- **Depois da Etapa 4, dentro de ~1h**: voltar os NS pro
  `ns8.wixdns.net` / `ns9.wixdns.net` no Registro.br. Em ~30min volta a
  responder pelo Wix.
- **Depois de 24h**: a zona Cloudflare já está respondendo pro mundo.
  Reverter exige voltar NS pro Wix e esperar propagação — mas o `www`
  no Wix nunca foi tocado nesse processo (CNAME continua → Railway),
  então a "loja" não cai em nenhum cenário.

## Sinais de problema

- **Wix bloqueando a transferência**: pode pedir "código EPP" — `.com.br`
  não usa EPP, mas se o Wix exigir, peça pelo painel deles ("Transferir
  domínio para fora"); eles enviam o código pro e-mail do titular.
- **Registro.br não reconhece o CPF**: o WHOIS mostra "caio antinhani" —
  conferir se o CPF cadastrado no Wix é o do titular do contrato (vai
  estar no painel TOWEB ou no histórico de pagamentos Wix).
- **Cloudflare não ativa**: confirmar que os 2 NS estão exatamente
  iguais (sem espaço, sem ponto extra) ao que o Cloudflare pede.
  Cloudflare re-checa a cada minuto; se demorar > 2h, abrir suporte.

## Apêndice — Por que NÃO é gambiarra do A record direto

Tentar resolver o apex apontando `A @ → 69.46.46.90` (IP atual do Railway)
parece tentador, mas:
- Railway documenta oficialmente que **não dá IP estático**; pode mudar.
- Sem SSL pro apex pelo Railway → cadeado quebrado em
  `https://padariaartesanalonline.com.br`.
- Resultado: cliente que digita sem www vê "site inseguro" — pior que
  hoje. Por isso o caminho canônico (Cloudflare) é o único definitivo.
