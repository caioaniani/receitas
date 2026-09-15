# Envio recuperável do checklist de loja

## Problema observado

Fotos originais eram enviadas juntas antes da compressão no servidor. Ao exceder
25 MB, o formulário HTML recebia um redirecionamento e perdia as respostas.
O botão desabilitado no envio também podia continuar travado ao voltar à página
no Safari. A confirmação dependia de um aviso temporário na tela inicial.
Esses caminhos foram reproduzidos; não significam que toda reclamação anterior
tenha tido a mesma causa. Registros já existentes não foram alterados.

## Comportamento novo

- Validação aponta o primeiro ponto pendente, sem sair da tela.
- Fotos grandes são reduzidas sequencialmente no navegador; os arquivos nos
  campos permanecem intactos. Há verificação do tamanho total antes do POST.
- Envio mostra preparação, transferência e espera pela confirmação do servidor.
- Falha mantém a página, as marcações, observações e arquivos selecionados.
- Rascunho por usuário/loja/tipo/dia fica no sessionStorage desta aba por 12 horas.
  Não contém imagens, senha ou CSRF. Ao recarregar é preciso reanexar as fotos.
- Uma resposta JSON de sucesso leva à confirmação persistente com comprovante,
  autor, horário e respostas. HTML de login nunca é interpretado como sucesso.
- Se a resposta da rede não chegar, consulta o recibo antes de reenviar. Não há
  reenvio automático do POST. Voltar pelo histórico restaura o botão.

## Contrato e segurança

O formulário usa simultaneamente `X-Checklist-Request: 1` e
`Accept: application/json`, mantendo CSRF obrigatório. O POST sem esse opt-in
continua compatível com o fluxo HTML anterior.

`ChecklistEnvio` é uma tabela nova. Seu token aleatório é uma chave de tentativa,
não uma credencial: o servidor verifica também usuário, loja e tipo. O recibo e
as respostas são gravados na mesma transação. A trava existente da loja
serializa envio/edição; o mesmo token retorna o recibo mesmo depois dos 30 segundos
da proteção anterior contra clique duplo. Outros tokens dentro dessa janela
podem apontar ao mesmo registro.

`GET /checklist/envio-status` consulta apenas o token e escopo do autor autenticado.
`GET /checklist/comprovante/<id>` é exclusivo do autor ou administrador. Ambos
mantêm a permissão de checklist, retornam `no-store, private` e não concedem
acesso a RH para contas limitadas ao treinamento.

## Publicação e reversão

Não há variável nova, alteração de coluna, exclusão de histórico ou integração
nova. A tabela nasce pelo `create_all` serializado já existente no startup.
Os dois JavaScripts recebem hash de versão para evitar cache do formulário antigo.

Publicar código/template/backend juntos. Em caso de reversão, reverter o commit
completo; a tabela adicional pode permanecer, sem apagar comprovantes ou respostas.
Uma aba que já carregou o cliente novo deve ser recarregada após a reversão.
As fotos continuam obrigatórias: erro de armazenamento não vira sucesso falso.

## Verificação

Testes cobrem validação, respostas de erro/CSRF/413, falha e rollback, replay após
30 segundos, escopo entre usuários/lojas, permissão de conta de treinamento com
checklist, HTML legado, armazenamento bloqueado, compressão e recuperação de rede.
O teste visual é feito com banco isolado, fotos sintéticas e Dropbox simulado;
nenhum checklist de teste deve ser lançado em produção.
