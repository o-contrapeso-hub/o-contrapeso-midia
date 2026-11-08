# Agendador do O Publicador

O workflow `.github/workflows/publicador.yml` roda uma rodada de publicação a cada 5 minutos (`python -m publicador.job`). O cron é `3-58/5 * * * *` (minutos 3, 8, 13 ... 58): o GitHub atrasa ou descarta execuções agendadas nos minutos redondos, e mesmo nos outros o horário é "melhor esforço" (pode atrasar alguns minutos).

## Modo real e simulação

- O padrão é **simulação**: a rodada lê o Notion, aplica a guarda e registra o que faria, sem publicar.
- O modo real só liga se a **variável** do repositório `PUBLICAR_REAL` valer exatamente `1`. Qualquer outro valor (vazio, `true`, ` 1`), ou a variável ausente, continua em simulação.
- Ligar ou desligar: Settings → Secrets and variables → Actions → aba **Variables** → `PUBLICAR_REAL`. Para desligar, apague a variável ou troque o valor.
- A execução manual não tem campos: não dá para ligar o modo real pelo botão. Ela segue a variável.
- Hoje só existe o adaptador falso (as plataformas reais entram a partir da T043). Em modo real, plataforma sem adaptador real é recusada pelo orquestrador.

## Rodar manualmente

Aba **Actions** → workflow **Publicador** → **Run workflow** → escolher a branch → **Run workflow**. O log mostra só o resumo (`modo=simulação`, contagens por desfecho). Não deve conter token, título, corpo, artes nem selo.

## Nomes esperados (somente nomes; nunca coloque valores neste repositório)

Secrets (Settings → Secrets and variables → Actions → Secrets):

- `NOTION_TOKEN`: integração do Notion.
- `PUBLICADOR_ARTES_TOKEN`: token **somente leitura** para ler as artes no GitHub.

Variáveis (aba Variables):

- `PUBLICAR_REAL`
- `PUBLICADOR_DB_ESTAGIOS`, `PUBLICADOR_DB_FORMATOS`, `PUBLICADOR_DB_SERIES`, `PUBLICADOR_DB_TEMAS`, `PUBLICADOR_DB_POSTS`, `PUBLICADOR_DB_EXECUCOES` (ids dos bancos do Notion)
- `PUBLICADOR_ARTES_REPO` (`dono/repo`) e `PUBLICADOR_ARTES_REF` (branch, tag ou commit)

Variável de ambiente obrigatória ausente: o job termina com código 2 e a mensagem fixa "Erro de configuração: ...", sem dizer qual nem ecoar valores. Confira a lista acima.

## Reativar o agendador desativado por inatividade (R2-05)

O GitHub desativa workflows agendados em repositório **público** após 60 dias sem atividade no repositório, e avisa por e-mail por volta do dia 45 (confira o e-mail da conta). Para reativar:

1. Aba **Actions** → clique no workflow **Publicador** → botão **Enable workflow**; ou
2. faça qualquer commit na branch padrão (a atividade reinicia o contador).

Depois confirme com uma execução manual (seção acima) e veja se a próxima rodada agendada aparece. Para evitar o problema, mantenha alguma atividade no repositório (um commit por mês basta) e leia os avisos de 45 dias.

## Validação na prática: PENDENTE

Esta validação **ainda não foi feita**. O código e os testes automatizados (`tests/test_workflow_publicador.py`) conferem a estrutura do YAML e o ponto de entrada, mas o Actions não foi executado. Daniel precisa, no GitHub:

- [ ] configurar os secrets e variáveis acima (sem `PUBLICAR_REAL`);
- [ ] rodar manualmente em simulação e confirmar `modo=simulação` e log sem segredo;
- [ ] confirmar que o cron dispara nos minutos esperados;
- [ ] exercitar a reativação (Enable workflow) quando o aviso de 45 dias ou a desativação ocorrer, e anotar aqui o que viu: ______

## Escrita no GitHub (T044, A-004)

A T044 exige escrita num repositório **público de estágio**, separado, para a cópia temporária das imagens do Instagram. Nomes: secret `PUBLICADOR_ESTAGIO_TOKEN` (escrita só nesse repositório) e variável `PUBLICADOR_ESTAGIO_REPO` (`dono/repo`). O token entra só no `env` do passo que roda o publicador. A ligação no ponto de entrada ainda não existe (T049). Detalhes, roteiro de validação e limpeza: `docs/instagram-estagio.md`.
