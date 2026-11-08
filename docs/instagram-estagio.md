# Instagram: cópia pública temporária das imagens (T044, D1, A-004, A-005)

O Instagram só aceita imagem (feed, story em imagem e itens de carrossel) por **URL pública**: ele baixa o arquivo no momento em que o container é criado. As artes ficam num repositório **privado**, então o publicador faz uma cópia **temporária** num repositório **público de estágio**, separado e exclusivo, passa a URL ao Instagram e apaga a cópia logo depois. Vídeos (Reels e story em vídeo) não passam por aqui: vão por upload direto (T045).

Código: `src/publicador/artes/estagio.py` (`EstagioPublico`, `EstagioGitHub`, `limpar_copias_antigas`) e `AdaptadorInstagram.publicar` (`src/publicador/adaptadores/instagram.py`). Testes: `tests/test_estagio.py` e `tests/test_instagram_imagem.py` (só respostas fabricadas e tokens falsos).

## Validação do redirecionamento: PENDENTE

**Ainda não foi validado.** O código e os testes foram escritos sem acesso à rede; nenhuma chamada real ao GitHub nem ao Instagram foi feita. Os campos e rotas das duas APIs foram escritos de memória.

O que precisa ser confirmado antes da F2:

1. A URL que o GitHub devolve para o arquivo de uma release (`browser_download_url`, `https://github.com/<dono>/<repo>/releases/download/<tag>/<arquivo>.jpg`) **não** serve o arquivo direto: responde com um redirecionamento (302) para um endereço de CDN do GitHub, assinado e com validade curta. O Instagram precisa **seguir esse redirecionamento** ao baixar a imagem.
2. O Instagram baixa a imagem na criação do container. O publicador só apaga a cópia **depois** do `media_publish`; confirmar que nada mais é baixado depois disso (o container já fica `FINISHED`).
3. Os nomes e o formato das respostas da API do GitHub usados no código (`id`, `tag_name`, `name`, `assets`, `browser_download_url`, `state`) e os status esperados (201 na criação, 204 ao apagar, 404 quando já foi apagado, 422 ao apagar tag inexistente).
4. Os campos da Graph API usados para imagem e carrossel: `image_url`, `media_type=STORIES`, `is_carousel_item=true`, `media_type=CAROUSEL`, `children` (ids separados por vírgula) e `caption` só no container publicado.

### Roteiro para o Daniel (conta de teste, imagem sem nada sensível)

Não cole token em chat, arquivo, issue ou commit. Use os tokens só no terminal local ou no Graph API Explorer.

1. Crie o repositório público de estágio (seção abaixo) e uma release de teste **manualmente** pela interface do GitHub, anexando uma imagem JPEG qualquer, sem conteúdo do canal.
2. Copie o link do arquivo da release e rode `curl -sI <link>`. Anote: o status (espera-se `302`) e o host do cabeçalho `Location` (só o host, sem a parte assinada).
3. Rode `curl -sL -o /dev/null -w '%{http_code} %{content_type}\n' <link>` e confirme `200 image/jpeg` (ou `application/octet-stream`; anote qual).
4. Na conta de teste do Instagram, crie um container com essa URL: `POST /<id-da-conta>/media` com `image_url=<link>` (Graph API Explorer ou `curl`, com o token de teste).
   - Se devolver um `id`: consulte `GET /<id>?fields=status_code` até `FINISHED`. O redirecionamento **funciona**.
   - Se devolver erro (por exemplo, código 9004 ou 36001, "media could not be fetched"): o redirecionamento **não funciona**; anote o código e a mensagem (sem o token) e siga a alternativa abaixo.
5. Apague a release de teste e, só então, chame `POST /<id-da-conta>/media_publish` com `creation_id=<id>` (pode ficar visível na conta de teste; apague o post depois). Se publicar, a cópia pode ser apagada já depois do `FINISHED`; se falhar, o publicador deve manter a ordem atual (apagar só depois do `media_publish`), que é o que o código faz.
6. Repita os passos 4 e 5 com um carrossel de 2 imagens (dois containers com `is_carousel_item=true` e um `media_type=CAROUSEL` com `children`).
7. Rode o job uma vez com `PUBLICAR_REAL` **desligado** e confira no log a linha da limpeza (`estagio: N cópia(s) temporária(s) antiga(s) apagada(s)`), sem nomes.
8. Registre aqui o resultado: data, status do passo 2, resultado dos passos 4 a 6: ______

### Se o Instagram não aceitar o redirecionamento

- **Alternativa 1 (menor mudança):** o publicador resolve o redirecionamento ele mesmo: faz um `GET` **sem token** no link do arquivo, sem seguir o redirecionamento, lê o cabeçalho `Location` (URL de CDN assinada, válida por poucos minutos) e passa essa URL ao Instagram. Exige que o transporte devolva o cabeçalho `Location` (hoje devolve só status e corpo) e uma lista fechada de hosts aceitos. A URL assinada nunca vai a log.
- **Alternativa 2 (D1, opção B):** URL de uso único servida pelo host próprio. Mais privada, mas exige o host acessível pelo job e mais um componente.

## Configurar o repositório de estágio (somente nomes; nunca coloque valores neste repositório)

1. Crie um repositório **público**, vazio de conteúdo e exclusivo para isto (por exemplo `<dono>/<algo>-estagio`), com pelo menos um commit (um README): a release cria uma tag e a tag precisa de um commit. Não use o repositório de artes nem o do publicador.
2. Crie um token *fine-grained* com acesso **somente a esse repositório** e permissão **Contents: Read and write** (cobre releases e tags). Nenhuma outra permissão. Defina validade e anote o vencimento (RF-41).
3. No repositório do publicador (Settings → Secrets and variables → Actions):
   - Secret `PUBLICADOR_ESTAGIO_TOKEN`: o token do passo 2.
   - Variável `PUBLICADOR_ESTAGIO_REPO`: `dono/repo` do repositório de estágio.

A ligação no ponto de entrada (`python -m publicador.job`) **ainda não existe**: nenhum adaptador real é registrado até a T049. Quando for ligada, o token deve entrar só no `env` do passo que roda o publicador (A-004) e ser embrulhado em `SegredoStr` na leitura.

## Como a cópia funciona

1. Antes de qualquer cópia: validação local da mídia (JPEG, proporção, até 8 MB, 2 a 10 itens no carrossel), conexão ativa e limite diário. Sem estágio configurado, o resultado é `FALHA_PERMANENTE` com o código `estagio_nao_configurado`, e nada é enviado (também em simulação).
2. Para cada imagem, uma release nova com tag e nome no formato fechado `publicador-tmp-AAAAMMDDTHHMMSSZ-<16 hex aleatórios>` (instante UTC da criação). A release é pública e marcada como *pre-release*, nunca como "latest", nem rascunho (o arquivo de rascunho não é público). O corpo da release é um texto fixo.
3. O arquivo se chama `<16 hex do SHA-256 da chave de idempotência>-<n>.jpg`: nunca título, corpo nem caminho da arte.
4. O token só vai a URLs montadas pelo código (`api.github.com` e `uploads.github.com`); a URL devolvida só é aceita se for exatamente `https://github.com/<dono>/<repo>/releases/download/<tag>/<arquivo>`.
5. Depois do `media_publish` (ou de qualquer falha, dúvida, erro interno ou cancelamento por `KeyboardInterrupt`), cada cópia é apagada: arquivo, release e tag. Já apagada (404) conta como apagada. Falha ao apagar **não muda** o resultado da publicação: só gera o log `copia_nao_apagada`, e a limpeza da próxima execução apaga a sobra.
6. Se o envio do arquivo falhar ou for cancelado depois de a release existir, o próprio estágio apaga a release antes de devolver o erro.

Classificação: falha no estágio antes do `media_publish` é `FALHA_TRANSITORIA` (rede, 5xx, 429) ou `FALHA_PERMANENTE` (401, 403, 404, 422, resposta fora do contrato), com código `estagio_<etapa>_...`. Qualquer dúvida depois de enviar o `media_publish` é `INCERTO`, e repetir o pedido nunca reenvia (nem cria cópia nova).

## Limpeza de cópias antigas (A-005)

`executar(..., estagio=...)` começa, antes de qualquer leitura do Notion, chamando `limpar_copias_antigas`: lista as releases do repositório de estágio (até 10 páginas de 100) e apaga só as que têm tag **e** nome iguais e no formato do publicador, com instante do marcador há **mais de 15 minutos**. Releases de outro formato, recentes ou com marcador no futuro nunca são tocadas. O log registra só as contagens (`N apagada(s); M falha(s)`), nunca nomes, tags ou ids. Falha na limpeza não derruba a rodada.

Isso cobre o job cancelado: o cancelamento pelo GitHub Actions chega como sinal; o `SIGINT` vira `KeyboardInterrupt` e o `finally` apaga as cópias, mas um `SIGTERM` ou a morte do processo não executam o `finally`. Nesses casos a cópia fica pública até a próxima rodada (no máximo cerca de 15 a 20 minutos). Tags sem release (se a exclusão da tag falhar) não expõem arte e não são listadas.

## Pendências

- Validação real do redirecionamento e dos campos das APIs (acima).
- T049: ler `PUBLICADOR_ESTAGIO_REPO` e `PUBLICADOR_ESTAGIO_TOKEN` no ponto de entrada, criar o `EstagioGitHub`, passá-lo ao `AdaptadorInstagram` (`estagio=`) e ao `executar` (`estagio=`), e pôr o secret só no `env` do passo do publicador. Considerar tratar `SIGTERM` como `KeyboardInterrupt` para o `finally` rodar também nesse caso.
- Conferir se o repositório de estágio é diferente do de artes na leitura da configuração.
