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
Se essa chave também for aceita parcialmente e o servidor pedir senha novamente,
a coleta atende essa continuação uma única vez. O fluxo termina após no máximo
quatro etapas principais (chave, senha, chave, senha), sem reiniciar o ciclo.
Um desafio interativo admite apenas uma resposta de senha, sem terminal ou OTP.
A sessão solicita o serviço SSH de autenticação uma única vez, usando o
`ServiceRequestingTransport` do Paramiko, para não reiniciar fatores já aceitos
em servidores que tratam um novo `SERVICE_REQUEST` como reinício. A espera pela
aceitação respeita o prazo total, o timeout de autenticação e o fechamento da
conexão. Cada fator usa um handler novo com o evento de resposta armado antes
do envio, inclusive para respostas imediatas. Não há mudança nos algoritmos ou
na exigência da chave conhecida do servidor.
A abertura do canal, a ativação do subsistema e a negociação SFTP têm
diagnósticos separados, preservando a conferência da identidade do servidor.

Falhas de autenticação também distinguem a etapa da chave, senha, desafio
interativo e confirmação final, usando apenas códigos locais e mensagens
fixas. A recusa de uma etapa não é apresentada como prova de senha incorreta.
Enquanto uma coleta está pendente ou em andamento, o painel atualiza o resultado
a cada 15 segundos. A atualização para ao editar qualquer campo e não é ativada
no formulário que recebe as credenciais, preservando o preenchimento do owner.

Os arquivos recebidos são preservados cifrados e podem ser baixados pelo owner.
O painel **Recebimentos Fiserv**, em `/financeiro/fiserv/resumo`, interpreta vendas,
pagamentos, recebíveis, Pix e vouchers em quadros separados. A leitura é local e
independente do SFTP: arquivos já guardados continuam sendo interpretados mesmo
quando um download falha. O agendador lê lotes de até 20 arquivos; o owner também
pode iniciar a leitura dos pendentes no painel. O GET apenas consulta resultados.
Não há lançamentos automáticos no caixa, baixas de pedidos ou duplicação das
vendas SERU. Pagamento informado pela adquirente não substitui a conciliação com
o extrato da conta bancária.

O envelope JSON e as grafias dos cinco tipos foram conferidos em arquivos reais
da versão 7.6.0, além dos manuais 7.5 e 7.7. Campos monetários decimais são lidos
com Decimal; o espelho `*Field`, quando presente, tem 15 dígitos em centavos e
deve coincidir com o valor em reais. Ajustes negativos podem trazer o espelho
sem sinal: nesse caso ele deve coincidir com a magnitude e o sinal decimal é
preservado. Não se arredonda divergência silenciosamente. Arquivos sem movimento
podem repetir a sequência entre controles de tipos diferentes; a exceção não
se aplica a arquivos com fatos financeiros nem aos controles inicial e final.
Campos ausentes permanecem ausentes. Status ou formatos não confirmados ficam
informativos ou geram uma pendência de interpretação, sem fabricar valores.

Cada arquivo tem uma interpretação por versão do parser, com observações
imutáveis. Reenvios byte a byte são filtrados na coleta; documentos financeiros
semanticamente repetidos reutilizam as observações. Correções são ordenadas pela
data da fonte, não pela ordem de download. Empates divergentes ficam fora dos
totais e aparecem para conferência. Resumos e detalhes da mesma composição nunca
são somados juntos. Nos pagamentos, ordens não conferidas ficam informativas;
somente componentes identificados, válidos e com valor liquidado informado
entram nos totais, mantendo um aviso quando a composição é parcial.
Recebíveis são posições por unidade, não novas entradas a
cada atualização; coleções explicitamente vazias substituem as anteriores e
coleções ausentes as preservam. O painel usa o termo "recebíveis conhecidos",
pois os arquivos parciais não comprovam a posição completa da conta.

Pix POS/TEF e voucher representam capturas; Pix PSP representa movimentos com
direção própria. Transações recusadas não entram nas capturas aprovadas.
Vendas, pagamentos, agenda e recebíveis não têm um total geral somado.
Os filtros usam a data do fato ou a data do pagamento/recebível. Pagamentos usam
`paymentDate`, incluindo antecipações, e preservam `valueDate` como vencimento
original no detalhamento. Cabeçalhos e trailers não contam como movimentos ou
registros sem identificação. Os filtros mostram até
200 linhas, com acesso ao original correspondente. Falhas de recebimento,
interpretação e conflitos permanecem visíveis, tornando explícita a cobertura
parcial. Novas versões do parser podem interpretar novamente os originais sem
baixá-los outra vez. Testes e amostras financeiras permanecem privados.

A visão inicial explica os valores em linguagem comum e mantém a composição e
os lançamentos em seções recolhidas. O subtotal pago usa somente `liquidado`
das linhas P vigentes e elegíveis de pagamentos, antecipações e ajustes. Não
soma novamente liquidações de R. A previsão considera apenas a agenda vigente
com status `previsto`; datas passadas são sinalizadas como posições sem
confirmação, sem presumir inadimplência. As próximas cinco datas e os totais
usam todas as linhas filtradas, antes do limite de exibição de 200 registros.
A proporção de taxas só aparece quando bruto menos taxas confere exatamente
com o líquido das vendas e a proporção está entre zero e cem por cento.
Custos de antecipação correspondem aos pagamentos antecipados identificados.
Dados ausentes continuam como "Não informado". Os atalhos de período
preservam os filtros de tipo e documento; pendências permanecem acessíveis.

Com o automático pausado, **Arquivos disponíveis na Fiserv** lista nomes e tamanhos
diretamente do acesso salvo, em páginas de 20 arquivos ordenados por nome. Isso
permite comparar com o mesmo arquivo do WinSCP, sem presumir que o primeiro da
lista corresponde ao download que o owner realizou. **Receber este arquivo**
baixa somente a seleção e a preserva cifrada pelo mesmo callback do agendador,
inclusive antes do CLOSE. Consultar ou receber um arquivo não ativa, agenda ou
altera o resultado do ciclo automático; falhas não agendam novas tentativas.
As duas ações exigem owner, CSRF, instância de coleta autorizada, limitador e a
mesma trava do agendador. A seleção é assinada por dez minutos e vinculada ao
owner, à sessão, à versão do acesso e aos metadados conferidos antes do OPEN.
A lista não é guardada em cookie e não inclui credenciais. O POST tem prazo
de rede de 45 segundos, inferior ao timeout HTTP; permanecem os limites de
arquivo e de listagem. Mensagens mostram somente categorias locais de erro.

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
Arquivo cifrado e metadados são gravados na mesma transação por arquivo, logo
após a leitura e a validação completas, antes do CLOSE normal do handle. Uma
falha no arquivo seguinte não descarta os anteriores. Falhas de persistência
têm classificação local própria, pausam a coleta e não viram erros de rede.

Ausência de um arquivo (`IO_AUSENTE` em LSTAT, REALPATH, OPEN, FSTAT ou READ do
arquivo) não bloqueia os demais downloads do lote automático. A tentativa é
registrada em `PendenciaFiserv`, com nome, metadados, etapa, código fixo e horário;
nenhum conteúdo parcial é salvo ou marcado como recebido. Falhas também contam
para os limites de quantidade e bytes do lote. A mesma revisão fica adiada por
uma hora, permitindo que os lotes seguintes avancem. Mudança de tamanho, data ou
versão do acesso permite uma nova tentativa sem aguardar esse intervalo.
Revisões nunca tentadas vêm antes das repetições; pendências vencidas são
retomadas da tentativa mais antiga à mais recente, evitando que os primeiros
nomes monopolizem a coleta quando o histórico leva mais de uma hora para drenar.
Uma pendência não desaparece só porque o arquivo deixou a listagem remota: fica
visível ao owner até um recebimento bem-sucedido, inclusive manual, resolver o
mesmo nome na mesma versão do acesso. Com pendências, o painel informa coleta
parcial; não apresenta o conjunto como totalmente recebido. Os demais erros de
conexão, permissão, segurança, limite ou armazenamento continuam interrompendo
o lote. Falha ao guardar a própria pendência também interrompe a coleta.

Arquivo regular e metadados válidos, conferidos por LSTAT e realpath, são
obrigatórios antes do OPEN. A transferência segue a sequência do download
do WinSCP: OPEN, FSTAT opcional e READ pelo handle, sem intercalar consultas
LSTAT/REALPATH pelo nome enquanto o arquivo está aberto. A versão anterior
intercalava essas consultas; essa diferença não está prevista no guia da
Fiserv. A compatibilidade do gateway com a sequência anterior não foi
confirmada. Não se ignoram erros de permissão ou rede. Ausência em OPEN/READ
interrompe esse arquivo e, no lote automático, registra uma pendência antes de
continuar para o próximo; não transforma uma leitura incompleta em sucesso.
Quando FSTAT retorna status SFTP 4 (falha genérica) ou 8 (operação não
suportada), usa os metadados conferidos antes do OPEN. FSTAT válido e divergente
é recusado e é repetido no mesmo handle após a leitura quando suportado.
O recebimento exige exatamente o tamanho anunciado e
todos os limites. Lê somente os bytes esperados, sem uma requisição adicional
após o tamanho informado: caixas de entrega podem encerrar o handle no último
byte. Retorno vazio precoce, excesso de bytes ou qualquer erro durante os bytes
esperados interrompem o arquivo. Crescimento observável no FSTAT final
também é recusado. Sem FSTAT, o tamanho anunciado e a estabilidade do conteúdo
dependem do servidor confiável: não se detecta toda troca ou crescimento
posterior aos metadados anteriores ao OPEN.
Não há consultas de caminho após CLOSE. Uma falha no encerramento não substitui
uma falha anterior de leitura, validação ou armazenamento.
Somente o código numérico do status é preservado, sem texto remoto. Como no
fluxo anterior, verificações de metadados detectam trocas observáveis, mas não
autenticam o conteúdo de um servidor comprometido nem toda troca transitória.

Watchdog encerra o transporte quando o prazo da coleta vence, inclusive durante
a abertura SFTP. Pode interromper o transporte durante um commit: a persistência
antes do CLOSE normal não garante precedência sobre todo encerramento abrupto.
Resolução DNS e leitura local de arquivos pela API legada não
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
