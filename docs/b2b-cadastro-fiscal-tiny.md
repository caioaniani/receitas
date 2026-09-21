# Cadastro fiscal B2B no Tiny

Antes de criar uma NF de venda ou fatura mensal para CNPJ, a integração pesquisa
o contato ativo pelo documento e consulta seu detalhe. Exige correspondência
exata do CNPJ e um único contato, inclusive entre páginas da pesquisa.

A IE e o código vêm desse cadastro. Não se presume isenção nem se inventa
indicador de contribuinte. A nota leva `atualizar_cliente=N`, preservando o
cadastro fiscal do Tiny. Campos de endereço continuam vindo do cadastro B2B.
Se a consulta falhar ou houver duplicidade, nenhuma nova nota é criada.

Refazer consulta a situação atual antes de descartar a referência anterior.
Notas autorizadas são sincronizadas; situação desconhecida ou denegada bloqueia
a recriação. A rejeição confirmada permite montar novamente o payload com o
cadastro atualizado. Não altera automaticamente o certificado do emitente.

O último erro da tentativa fiscal fica visível nos detalhes da venda e da
fatura. Uma consulta que devolva apenas o status genérico de rejeição preserva
o motivo detalhado da tentativa anterior. Dados antigos sem motivo registrado
não têm o detalhe reconstruído automaticamente.

Fontes oficiais:
- https://tiny.com.br/api-docs/api2-contatos-pesquisar
- https://tiny.com.br/api-docs/api2-contatos-obter
- https://tiny.com.br/api-docs/api2-notas-fiscais-incluir

Validação: testes de gateway com respostas simuladas, payload de venda/fatura,
automação e proteções contra recriação; nenhuma emissão real durante os testes.
