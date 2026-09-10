# Diário de produção por lote

A tela do padeiro permite coletar os dados reais ao longo dos dias, antes de
revisar tempos das fichas ou a programação. Em **Ver sequência**, use
**Registrar lote** no produto ou **Registrar lote da base** no preparo conjunto.
**Diário de produção** mantém todos os lotes abertos visíveis, mesmo os antigos.

## Uso diário

1. Escolha a ordem e o produto/base. Informe a data real do lote e, se útil,
   uma identificação, como “Batida 1”. A data da ordem é guardada separadamente.
2. Inicie cada etapa quando ela acontecer e conclua ao terminar. O nome pode
   ser escolhido entre sugestões da ficha ou digitado. Para sourdough, as duas
   velocidades podem ser medidas separadamente. Nenhuma etapa nasce concluída.
3. Preencha pesos, temperaturas e observações quando tiver as medições.
   Farinha, água adicionada e massa final são campos distintos em kg. Temperaturas
   não medidas ficam vazias; o sistema não presume temperatura da farinha.
4. Se o registro foi feito depois, informe início e fim reais, inclusive quando
   a fermentação atravessar a meia-noite. Corrija os dados mantendo a auditoria.
5. Finalize o registro do lote quando terminar a coleta. Lotes com etapas ainda
   abertas não podem ser finalizados. É possível reabrir um registro.
6. No histórico, filtre o período e consulte ou exporte CSV. Cada linha do CSV
   representa uma etapa e repete a identificação e as medições do lote; não some
   pesos repetidos para calcular consumo. Lotes sem etapas também são exportados.

O registro é parcial por natureza: pode começar apenas com identificação e ser
completado durante os dias. As sugestões são nomes da ficha, não tempos validados.
Nada da planilha de sourdough foi importado como medição real ou padrão aprovado.

## Limites da primeira versão

O diário não recalcula o Gantt, não emite alarmes de dobra e não modifica fichas
nem produz estoque. A quantidade pronta continua sendo registrada no botão
**Registrar produção**, pelo fluxo existente. Essa separação evita tratar começo
de batimento ou final de descanso como pão pronto.

Após reunir lotes comparáveis, revise tempos por etapa e tamanho de batida.
Fermentação depende também das condições registradas; médias de lotes distintos
não são automaticamente convertidas em padrão. Etapas podem se sobrepor, por
isso somar suas durações não representa o tempo total de produção.

## Persistência e permissões

O acesso segue `web_padeiro`, incluindo administradores e os papéis já liberados
na tela. Todas as gravações usam POST com CSRF. Alterações recebem autor e horário;
versão do lote evita sobrescrita concorrente. A criação tem chave única para
repetição do envio não duplicar lote. Finalizar uma etapa é idempotente.

Três tabelas novas (`producao_diario_lote`, `producao_diario_etapa` e
`producao_diario_alteracao`) são criadas pelo `db.create_all()` existente, sob lock
no PostgreSQL. Não há alteração de colunas antigas. Origem, nome e datas são
snapshots: replanejar ou apagar a ordem não apaga as observações já registradas.

Validação: `tests/test_producao_diario.py`, `tests/test_padeiro_diario_routes.py`
e os testes existentes da sequência do padeiro.
