# App Review — Instagram (Meta for Developers)

Guia de submissão. Os textos aqui são pra serem **colados** nos
formulários da Meta. Cada permissão tem um formulário próprio (mesmo
caminho, justificativa diferente).

---

## Antes de começar — checklist

- [ ] App em modo **Live** (Meta Dev → Settings → Basic → toggle no topo)
- [ ] **Business Verification** já feita (confirmar em Business Manager
      → Configurações → Informações da Empresa)
- [ ] Conta Instagram da padaria conectada como **Profissional**
      (Business) e linkada à página do Facebook da padaria
- [ ] **Política de Privacidade** publicada em
      `https://www.padariaartesanalonline.com.br/privacidade`
      (URL preenchida em Settings → Basic → Privacy Policy URL)
- [ ] **Data Deletion Instructions** publicada em
      `https://www.padariaartesanalonline.com.br/exclusao-dados`
      (URL preenchida em Settings → Basic → User Data Deletion → "Data
      Deletion Instructions URL")
- [ ] **Ícone do app** 1024×1024 PNG (logo da padaria)
- [ ] **Categoria do app**: "Business and Pages"
- [ ] Webhook do Instagram configurado e respondendo `200`
      (Chatwoot já cuida disso — só verificar em Webhooks → Instagram
      que está com status verde)
- [ ] **Screencast** gravado (ver roteiro em `screencast-roteiro.md`)
- [ ] **Conta de teste** (você mesmo): perfil pessoal seu deve ser
      Tester do app — pra demonstrar a DM no vídeo

---

## Permissões a solicitar (4)

Vá em **Meta for Developers → App → App Review → Permissions and
Features**. Para cada uma das 4 permissões abaixo, clique
**"Request Advanced Access"** e preencha o formulário com o texto
indicado.

> Atenção: os nomes das permissões mudam de tempos em tempos. Se algum
> nome aqui não bater com o que aparece no painel, procure pela
> descrição equivalente. A descrição não muda.

### 1. `instagram_basic`

**Como o app usa a permissão (How your app uses this permission):**

> Usamos `instagram_basic` para ler o perfil da conta Instagram
> profissional da empresa (nome de exibição e foto), de modo que o
> atendente que recebe a DM saiba qual conta da empresa o cliente está
> contatando (no caso de empresas com mais de um perfil), e para
> identificar o remetente das mensagens recebidas, exibindo nome e foto
> de perfil ao lado da conversa na ferramenta interna de atendimento. Não
> usamos esta permissão para publicar conteúdo, listar mídia ou para
> qualquer finalidade fora do atendimento humano ao cliente.

---

### 2. `instagram_manage_messages`

**Como o app usa a permissão:**

> Esta é a permissão central da nossa integração. A empresa atende
> clientes que entram em contato por DM no Instagram. Hoje esse
> atendimento é feito por uma ferramenta paga de terceiros que pretendemos
> substituir por uma instância própria do **Chatwoot (open-source)** que
> hospedamos. A permissão `instagram_manage_messages` permite que o
> nosso sistema **receba as DMs enviadas pelos clientes** (via webhook)
> e que a equipe de atendimento **responda** essas mensagens a partir da
> mesma ferramenta interna onde já atende WhatsApp e chat do site,
> evitando que o atendente precise abrir o aplicativo do Instagram em
> celulares separados. Cada DM é exibida em uma fila compartilhada de 12
> atendentes; o primeiro disponível assume e responde. As mensagens
> respondidas voltam ao cliente como uma resposta normal de DM,
> identificada como vinda da conta da empresa. Não enviamos mensagens
> proativas (campanhas frias) por esta integração — respondemos apenas a
> DMs iniciadas pelo cliente, dentro da janela de 24 horas permitida
> pela Meta. Em casos de dúvida que excedam o escopo do atendimento
> automatizado inicial, há um botão claro para transferir a conversa a
> um humano.

---

### 3. `pages_messaging`

**Como o app usa a permissão:**

> Permissão necessária porque a conta Instagram da empresa está vinculada
> a uma Página do Facebook, e a Meta exige `pages_messaging` em conjunto
> com `instagram_manage_messages` para processar mensagens dessa Página.
> O uso é o mesmo descrito em `instagram_manage_messages`: receber e
> responder mensagens iniciadas pelo cliente, dentro da janela permitida,
> a partir da ferramenta interna Chatwoot. Não publicamos conteúdo na
> Página por esta integração.

---

### 4. `pages_manage_metadata`

**Como o app usa a permissão:**

> Usada exclusivamente para inscrever o webhook do nosso sistema nos
> eventos de mensagem da Página (chamada da Graph API
> `/<PAGE_ID>/subscribed_apps`). Sem essa inscrição, o nosso sistema
> não recebe notificações de novas DMs. Não usamos esta permissão para
> alterar configurações públicas da Página, mudar nome, foto ou qualquer
> outro metadado visível ao público.

---

## Campos comuns do formulário

**Plataforma:** Web (Chatwoot self-hosted)

**Usuário de teste para revisão da Meta:**
Forneça à Meta as credenciais de uma conta de atendente comum no
Chatwoot, com permissão limitada:
- URL: `https://atendimento.opaopadariaartesanal.com.br`
- Usuário: [criar `meta-review@opao.online`]
- Senha: [senha temporária forte; rotacionar depois da aprovação]

(Crie esse usuário **antes** de submeter. Se a Meta não conseguir
entrar, recusa.)

**Informações adicionais ("Anything else?"):**

> Operamos a padaria há 5 anos com canal ativo no Instagram. Já temos
> WhatsApp Business Cloud em produção no mesmo Chatwoot. A Página do
> Facebook tem [número] seguidores ativos. O ícone do app, política de
> privacidade e instruções de exclusão de dados estão preenchidos. O
> screencast em anexo mostra o fluxo completo: cliente envia DM,
> mensagem chega no Chatwoot, atendente responde, cliente recebe.
> Permanecemos à disposição para esclarecimentos pelo e-mail
> caio@opao.online.

---

## Depois de submeter

- A Meta costuma responder em **3 a 7 dias úteis**
- Em ~50% dos casos pedem ajustes na primeira rodada (vídeo mais claro,
  texto mais específico, conta de teste com mais permissão)
- Não tem como acelerar — só responder rápido a cada pedido
- Mantenha o app em **Live** durante todo o processo de revisão

## Se for recusado

A recusa vem com um motivo específico. Padrão: ler com calma, ajustar
**só** o que foi pedido, e resubmeter. Não vale resubmeter idêntico.
