# Recebimento automático Fiserv EDI

## Operação do owner

A área **Financeiro → Recebimentos Fiserv**, em `/financeiro/fiserv`, recebe os
arquivos da adquirente. Todos os endpoints, incluindo configuração, sondagem
e download, são exclusivos do owner. A coleta não envia mensagens, não remove
arquivos na Fiserv e não altera pagamentos ou saldos.

1. Escolher um dos dois servidores informados pela Fiserv e consultar a
   identificação apresentada. Essa sondagem faz somente handshake SSH, sem
   enviar usuário, senha ou chave privada.
2. Conferir a impressão SHA256 com a Fiserv por um canal conhecido. A página
   não afirma que observar a chave autentica o servidor. A confirmação vale
   dez minutos, vinculada ao owner, à sessão e ao host consultado.
3. Preencher usuário, senha de acesso se fornecida, arquivo da chave e senha
   própria da chave se houver. Esses valores nunca são repopulados na tela.
4. Salvar e iniciar. O job atende a solicitação no próximo minuto; após sucesso,
   coleta a cada hora. A página mostra tentativas, sucesso, arquivos e falhas.

Uma falha de autenticação, configuração, identidade do servidor ou permissão
de leitura pausa a coleta até intervenção do owner. Pasta ausente e recusa de
abertura do canal SFTP também têm mensagens próprias, sem expor a resposta do
servidor. Indisponibilidade de conexão permite nova
tentativa em uma hora. Pausar e solicitar novamente são ações explícitas.

Falhas transitórias informam a etapa e uma categoria fixa entre colchetes,
por exemplo `PASTA_LSTAT/IO_SEM_CODIGO`. Isso distingue conexão, abertura SFTP,
consulta da pasta, listagem e leitura sem guardar texto da exceção remota,
nome de usuário, caminhos de chaves ou conteúdo dos arquivos. As categorias
não inferem senha incorreta a partir de uma falha genérica de protocolo.

A autenticação da chave guardada no painel só é considerada concluída quando
o transporte confirma isso. São suportadas as sequências chave/senha e
senha/chave: a chave pode ser repetida após a senha somente quando o servidor
solicitar essa continuação. A senha não é repetida automaticamente após recusa.
Um desafio interativo admite apenas uma resposta de senha, sem terminal ou OTP.
A abertura do canal, a ativação do subsistema e a negociação SFTP têm
diagnósticos separados, preservando a conferência da identidade do servidor.

Os arquivos recebidos são preservados cifrados e podem ser baixados pelo owner.
Este estágio recebe o EDI original: **não normaliza ou soma os valores no caixa**.
Os cinco leiautes (vendas, pagamentos, recebíveis, PIX e voucher) ainda precisam
ser homologados com amostra real, pois os PDFs não definem o envelope externo
completamente. Não confundir posições de recebíveis com novas entradas de caixa,
nem somar venda, parcela, resumo e liquidação da mesma operação.

## Segredos e criptografia

O formulário usa HTTPS/cookie de sessão, CSRF e limite total de 96 KiB antes do
processamento do multipart. Chave enviada: até 16 KiB. Chaves aceitas e limites
criptográficos ficam em `fiserv_segredos.py`; formatos não homologados são
recusados com orientação específica.

Credenciais e chave privada ficam em `IntegracaoFiserv.acesso_cifrado`, usando
Fernet com HKDF-SHA256 derivado da SECRET_KEY permanente do servidor e contexto
`fiserv-segredos-v1`. EDI bruto usa contexto separado `fiserv-edi-bruto-v1`.
Não há cópia da chave de cifragem no banco. Configuração com SECRET_KEY efêmera
não pode ser salva. A rotação de SECRET_KEY exige recifrar o acesso e os arquivos
com a chave anterior antes de descartá-la; restaurar backups exige a chave
correspondente. A integração não muda SECRET_KEY nem gerencia sua rotação.

Não guardar acesso em AppConfig, Git, logs, links públicos ou testes. Nenhuma
rota retorna credenciais ou ciphertext. Modelos privados ficam fora da auditoria
genérica; EventoFiserv registra somente ações, ator, data e contagens.
Requisições da área e jobs marcados são excluídos da telemetria Sentry, incluindo
transações. Frames que manuseiam segredos não expõem variáveis locais.

## Coleta e idempotência

Hosts permitidos: `prod-gw-lac.firstdataclients.com` e
`prod2-gw-lac.firstdataclients.com`; porta 6522; pasta fixa absoluta `/available`.
O caminho foi confirmado no acesso real pelo WinSCP: é minúsculo e independe
do diretório inicial da conta. Não tentar `Available` nem outros diretórios.
A chave pública aprovada é fixada na configuração. Conexões seguintes usam
RejectPolicy; mudança da chave não é aceita automaticamente. Credenciais são
carregadas em memória, sem arquivo temporário.

O coletor lista no máximo 10000 entradas, não percorre subdiretórios e rejeita
links e travessia de diretórios. Cada lote baixa até 20 arquivos e 64 MiB no total,
com máximo 16 MiB por arquivo. Arquivos restantes são solicitados no próximo
ciclo, sem contar históricos já conferidos para o limite de download.
FiservArquivoRemoto mantém metadados por versão da configuração; registros sem
mudança são revalidados pelo conteúdo após 24 horas. O SHA256 tem unicidade no
banco: nomes diferentes ou reenvio do mesmo conteúdo não duplicam o arquivo.
Mudanças de conteúdo preservam a revisão anterior; não são lançamentos novos.
Arquivo cifrado e metadados são gravados na mesma transação por arquivo.

Watchdog encerra o transporte quando o prazo da coleta vence, inclusive durante
a abertura SFTP. Resolução DNS e leitura local de arquivos pela API legada não
são canceláveis pelo watchdog; limites do sistema operacional também se aplicam.
O formulário de produção usa somente credenciais em memória.

## Agendamento e implantação

O job `fiserv-coleta` usa o scheduler existente. Exige configuração salva,
solicitação/ativação do owner e `RAILWAY_GIT_BRANCH` exatamente igual ao branch
canônico em `app/services/instancia.py`. Branches diferentes são bloqueados,
mesmo se copiarem os dados e variáveis da produção. Fora do Railway, somente
`FISERV_INSTANCIA_LOCAL_AUTORIZADA=1` libera uma instalação explícita.
`FISERV_AUTO_COLETA=0` ou `SERU_AUTO_SYNC=0` desabilitam o job.

Postgres usa advisory lock na mesma conexão durante todo o ciclo; SQLite usa
lock de arquivo entre processos, inclusive Windows. O mesmo lock protege edição,
pausa e nova solicitação. Tabelas são novas e criadas pelo startup serializado;
nenhuma coluna foi adicionada às tabelas existentes.

A biblioteca usa Paramiko 5.0.0. A API legada de arquivos locais continua disponível
com FISERV_SFTP_HOST, FISERV_SFTP_USERNAME, FISERV_SFTP_PRIVATE_KEY_PATH,
FISERV_SFTP_KNOWN_HOSTS_PATH e FISERV_SFTP_PASSWORD opcional; senha da chave usa
FISERV_SFTP_KEY_PASSPHRASE ou FISERV_SFTP_KEY_PASSPHRASE_PATH, mutuamente exclusivas.
Essas variáveis não ativam a área automaticamente: o job usa o cadastro do owner.

## Extrato bancário Sicredi

O SFTP da Fiserv contém dados da adquirente e não é acesso à conta Sicredi.
O extrato bancário CNAB240 do outro manual depende de acesso próprio, incluindo
contratação/transmissão Nexxera quando aplicável. A integração CNAB400 existente
para boletos permanece separada.
