# Treinamento e gestão de pessoas

## Escopo

Reorganização visual sobre o layout v2, sem mudar publicação, pontuação,
critérios de conclusão, permissões ou remuneração.

- Meu treinamento: próxima aula em destaque, módulos obrigatórios primeiro,
  andamento das aulas separado da conclusão do módulo.
- Módulo: aulas, avaliações e validação prática explicitadas; detalhes legíveis
  no celular. O player e seu bloqueio de tela cheia permanecem intactos.
- Acompanhar equipe: busca por pessoa/cargo/unidade, filtros de situação,
  progresso individual e acesso direto ao checklist de observação.
- Módulos por cargo: antiga “Progressão de cargo”; nome não sugere promoção
  automática. Busca e links ao progresso respeitam o mesmo escopo autorizado.
- Gerenciar cursos: resumo de publicação, busca por módulo/aula e estado;
  administração eventual recolhida, publicação individual preservada.
- Pessoas: visão macro, cadastros e acessos com navegação local consistente,
  filtros e cartões/tabelas adaptados ao celular.

## Padrões visuais

Reutilizamos as cores, tipografia e tokens `--v2-*`. Os estilos novos ficam
limitados às classes de treinamento; a navegação compartilhada é exclusiva
do layout v2. Busca e estado vazio são explícitos, foco de teclado é visível,
e os detalhes usam `details/summary` nativo. Informações avançadas e reenvios
em lote não competem com a ação cotidiana.

Assets recebem hash de conteúdo no mesmo mecanismo de cache-busting existente.
O novo JavaScript da administração não exige biblioteca externa. Sem JS,
conteúdos e formulários permanecem disponíveis; apenas o filtro local se oculta.

## Proteções

- Owner: visão macro e cargos/salários, mantendo todos os gates existentes.
- Admin: administração do treinamento, sem ganhar acesso ao RH confidencial.
- Líder: somente liderados já autorizados, inclusive vínculos compartilhados
  válidos; busca filtra o escopo existente, nunca consulta outra equipe.
- Funcionário: estudo; nenhuma concessão de acesso a cargos/salários.
- Dakson: cadastro básico conforme autorização já existente, sem remuneração.
- Sem migrations, alterações de cargos, promoções, criação de contas, envio de
  e-mails ou mudança de senha nesta entrega.

Os totais do painel representam a equipe autorizada; busca/situação filtram a
lista, não os totais. “Sem acesso” indica ausência de conta vinculada, não falha
de senha. “Nenhuma atividade nas aulas” não significa que nunca houve login.
No cadastro, a opção “Apenas ativos” envia explicitamente `0` quando desmarcada,
evitando que a omissão do parâmetro aplique novamente o padrão de ativos.

## Verificação

Testes de regressão incluem treinamento, publicação individual, liderança,
acompanhamento, acesso somente treinamento e visão RH. Novos cenários cobrem:

- busca sem distinção de acentos e sem vazamento de outra equipe;
- atalhos condicionados ao papel e bloqueio direto de RH confidencial;
- ausência de link de progresso não autorizado para ex-liderado inativo;
- publicação de aula versus módulo oculto, CSRF e filtro da administração;
- filtro de ativos, restrições do cadastro Dakson e escape de dados na busca.

Validação visual local em 390px e 1440px, com base SQLite isolada e pessoas
fictícias. A prévia não contém arquivos reais de vídeo. Não foi publicada
em produção automaticamente.

Execução: `python -m pytest -q` e `python -m ruff check app tests`.

Validação em 10/09/2026: suíte completa com quatro workers, **5.011 passed,
3 skipped e 3 xpassed**, em 70,34s. Ruff dos arquivos alterados e aplicação,
`node --check` do script novo e `git diff --check` sem erros.
