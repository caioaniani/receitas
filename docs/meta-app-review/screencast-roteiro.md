# Roteiro do screencast — App Review Instagram

A Meta exige um vídeo mostrando **o uso real** das permissões. Não pode
ser apresentação de slides nem vídeo cortado/editado. Tem que ser
**screencast contínuo**, com áudio explicando o que está acontecendo.

## Requisitos técnicos

- **Duração:** 2 a 4 minutos (a Meta avisa "no máximo 10 min", mas o
  ideal é menor pra revisor não pular)
- **Resolução:** 1080p mínimo
- **Áudio:** narração em **inglês** (Meta revisa fora do Brasil; PT-BR
  funciona às vezes, mas inglês passa mais rápido) — texto sugerido
  abaixo
- **Conteúdo:** captura de tela contínua, sem cortes invisíveis. Pode
  acelerar trechos lentos (envio de DM esperando chegar), desde que
  fique evidente que é o mesmo vídeo
- **Não mostrar:** tokens, senhas, dados de outros clientes

## O que você precisa preparar antes

- **Conta cliente:** seu Instagram pessoal (que já está como Tester do
  app)
- **Conta empresa:** Instagram da padaria, conectada ao Chatwoot
- **Chatwoot aberto:** logado como atendente comum
- **Software de gravação:** OBS (grátis), Loom, ou QuickTime no Mac

## Sequência (passo a passo)

### Cena 1 — Apresentação (15s)

Tela do Chatwoot aberta, na caixa Instagram.

**Narração (inglês sugerido):**
> "Hi. This is Caio from O Pão, a bakery in São Paulo, Brazil. We use
> our own self-hosted Chatwoot to handle customer messages from
> WhatsApp, Instagram, Facebook and our website. I'm going to show how
> the Instagram integration works in real time — receiving and replying
> to a customer DM."

### Cena 2 — Cliente envia DM (30s)

Cole o lado do **celular** (espelhado na tela, ou janela do Instagram
Web) ao lado do Chatwoot.

No celular/Instagram web (lado cliente):
- Abrir DM da página da padaria
- Digitar: *"Hi, do you deliver croissants today?"*
- Enviar

**Narração:**
> "From a customer account that has Tester access to our app, I'm
> sending a direct message to the bakery's Instagram profile."

### Cena 3 — Mensagem chega no Chatwoot (30s)

Foque na tela do Chatwoot. A nova conversa Instagram aparece.

**Narração:**
> "Within a few seconds, the message arrives in Chatwoot through our
> webhook. The agent assigned to the Instagram inbox sees the customer's
> name, profile picture, and message content. Twelve agents share this
> inbox — the first one available picks up the conversation."

### Cena 4 — Atendente responde (30s)

No Chatwoot, abra a conversa. Digite a resposta:
*"Hi! Yes, we deliver croissants today. You can place your order at
www.padariaartesanalonline.com.br"*

Envie.

**Narração:**
> "The agent types and sends a reply right from Chatwoot — without
> opening the Instagram app on a phone. This is the workflow that
> requires `instagram_manage_messages`."

### Cena 5 — Resposta chega no cliente (20s)

Volte pro celular/Instagram web do cliente. Mostre a mensagem chegando
em segundos.

**Narração:**
> "The customer receives the reply as a normal DM from the bakery's
> profile, inside Instagram. The whole exchange takes a few seconds."

### Cena 6 — Transferência pra humano / encerramento (20s)

No Chatwoot, mostre o botão "Resolve" / "Snooze" / atribuir a outro
agente.

**Narração:**
> "The agent can resolve the conversation, snooze it, or transfer it to
> a teammate. We never use this integration to send proactive marketing
> messages — only to reply to messages that customers start. Thanks for
> reviewing."

## Verificação final antes de enviar

- [ ] Vídeo tem áudio narrado o tempo todo
- [ ] Mostrei o **app real** (Chatwoot URL visível na barra do navegador)
- [ ] Mostrei a DM chegando E sendo respondida (os dois lados)
- [ ] Não tem token, senha ou dado de cliente real na tela
- [ ] Menos de 4 minutos
- [ ] MP4 ou MOV, menos de 100 MB
