# Download da cobrança para WhatsApp

Na central de cobranças, abra os documentos da fatura/parcela. A seção
**Cobrar pelo WhatsApp** oferece:

- **Baixar cobrança completa (PDF)**: DANFE, boleto existente e detalhamento
  dos pedidos em um único arquivo, nessa ordem, com marcadores de navegação.
- **PDFs separados (ZIP)**: os mesmos três documentos separados. O DANFE
  original é preservado byte a byte; use essa opção caso haja assinatura no PDF.

O usuário anexa o arquivo manualmente no WhatsApp. Download não significa
envio e não cria registro de e-mail, não emite nota, não cria boleto/remessa,
não altera saldo, recebimento ou estoque. Não precisa de e-mail cadastrado.
Os envios por e-mail continuam com NF + boleto, sem mudar esse fluxo.

Na lista, **Baixar PDF / cobrança completa** fica ao lado da ação de envio.
O atalho mantém a confirmação explícita de registro para boletos em remessa;
sem JavaScript, o servidor exige a conferência na tela de documentos.

Os arquivos PDF/ZIP usam `Empresa - Entrega DD-MM-AAAA - Pedido N - NF N.pdf`.
Usa-se a empresa cobrada (cliente/origem da linha), não a padaria como emissora.
A data é a entrega cadastrada, nunca o vencimento: ausência vira `Entrega sem data`.
Faturas incluem as datas e pedidos; mais de três datas viram intervalo e listas
de pedidos muito extensas usam a referência da fatura e a quantidade de pedidos.
Vendas parceladas incluem o número da parcela para não confundir boletos distintos.
Caracteres incompatíveis com nomes de arquivo são removidos e os nomes têm até
240 caracteres. O número da NF preserva zeros à esquerda; se ausente, é sinalizado.

## Conferências

- Apenas administradores, com as mesmas proteções de conta/host da central.
- Mesmos bloqueios do envio conjunto: canceladas, pagas, divulgações sem
  cobrança, NF não autorizada, boleto pendente/rejeitado e divergência do saldo.
- Boletos em `remessa` exigem confirmação explícita do registro no banco.
- Parcela absorvida por fatura redireciona à fatura; não baixa cobrança duplicada.
- Todos os pedidos vinculados à fatura aparecem, separados por venda/data.
  O total da parcela é identificado separadamente do total do pedido.
- Pedidos sem itens, totais divergentes ou de outro cliente bloqueiam o pacote.
- Documento ausente, corrompido, criptografado, acima de 20 MB ou 150 páginas
  bloqueia o conjunto inteiro. Nada é entregue parcialmente.
- PDF assinado não é mesclado: o ZIP preserva os originais.
- Resposta privada (`Cache-Control: private, no-store`), nomes higienizados,
  arquivos montados em memória, sem links públicos ou novas tabelas.

## Validação e publicação

- Testes em `tests/test_cobrancas_download.py`: PDF/ZIP completos, ordem das
  páginas, linha digitável, frete/desconto, múltiplas vendas, nomes extensos,
  paginação, permissões, falhas e ausência de emissão/envio/escritas financeiras.
- Testes em `tests/test_cobrancas_atalho_nome.py`: atalho na lista, confirmação
  bancária no clique, nomes seguros, entrega ausente, múltiplos pedidos e parcelas.
- Regressão: central, dispensa, envio unificado e e-mail B2B; suíte completa e Ruff.
- Prévia local com dados fictícios e rede bloqueada; revisão visual desktop/mobile
  e renderização do PDF simples, boleto mesclado e pedido multipágina.
- Dependência nova: `pypdf==6.16.2`; nenhuma migration ou variável de ambiente.
- Publicar somente após CI verde. Conferir Railway, saúde do ERP, checkout público
  e presença do botão. Teste de download real não deve acionar envio/emissão.
- Reversão: reverter somente o commit desta funcionalidade e republicar.
  Nenhum dado ou alteração de banco precisa ser revertido. Reverter caso a
  aplicação deixe de iniciar ou o download comprometa os fluxos existentes.
