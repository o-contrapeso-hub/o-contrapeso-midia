# Esquema do Notion (workspace de testes da T005)

Gerado a partir da seção 3 do plano ("4 · Planejar"). Todos os bancos foram criados dentro da página de teste
"Publicador · Teste (workspace de testes da T005)" (id `4119dfaa-9e4b-81fb-a503-f92cefdc5567`).
Este documento não contém tokens nem credenciais.

O ID do banco (database) está na URL; o ID do data source (`collection://`) é o usado em consultas e na API de
data sources.

## Bancos criados

| Banco | ID do banco | ID do data source | URL |
|---|---|---|---|
| Posts | `e1b19d71705b4fbc8ecd5fedb8092fec` | `916bc6a6-e74a-4694-8f04-51a977c08128` | https://app.notion.com/p/e1b19d71705b4fbc8ecd5fedb8092fec |
| Estágios | `a89a9a3bf7484d5cbf9d73ce526e0e4a` | `9c9d7c58-ec52-417a-9e2f-c6abf255d127` | https://app.notion.com/p/a89a9a3bf7484d5cbf9d73ce526e0e4a |
| Séries | `7bb1e2ea3cf34188a709e5dfe409a55f` | `daebdd14-10fe-4450-8d53-763e3ea19359` | https://app.notion.com/p/7bb1e2ea3cf34188a709e5dfe409a55f |
| Formatos | `42c6a461a7664571b63300098b5b2b02` | `e2070a46-ea23-48a1-a474-e0f92c6ac9fd` | https://app.notion.com/p/42c6a461a7664571b63300098b5b2b02 |
| Temas | `43087b4914d24a09a03f6b1fcc85571e` | `ae6f7553-80fd-4229-afd2-a9cc8c8258e3` | https://app.notion.com/p/43087b4914d24a09a03f6b1fcc85571e |
| Execuções | `e882e4987273430b8fe7002ce2811fa4` | `897e1454-0624-49b0-90b7-39865092e4ee` | https://app.notion.com/p/e882e4987273430b8fe7002ce2811fa4 |
| Conexões | `11662134fc4844fdbe0561328d34bda1` | `74dce82c-ec2c-41ea-8f4e-018105ed1edb` | https://app.notion.com/p/11662134fc4844fdbe0561328d34bda1 |
| Parâmetros | `4ecc370840294d1d93cfeeaec18a018c` | `109f195b-0f21-4b3d-8924-5b440985277a` | https://app.notion.com/p/4ecc370840294d1d93cfeeaec18a018c |

## Propriedades

### Posts
| Propriedade | Tipo | Observação |
|---|---|---|
| Título | title | |
| Estágio | relation -> Estágios | |
| Formato | relation -> Formatos | |
| Plataformas | multi_select | Instagram, TikTok, YouTube, Substack |
| Data e hora | date | com hora e fuso (o valor é gravado com `is_datetime`) |
| Série | relation -> Séries | |
| Ordem na série | number | |
| Tema | relation -> Temas | |
| Criado em | created_time | apoio à fórmula de Ano e Mês |
| Ano | formula | ver fórmulas |
| Mês | formula | ver fórmulas |
| Artes | rich_text | referências de caminho no repositório privado |
| Selo de confirmação | rich_text | hash SHA-256 |
| Confirmado em | date | |

O corpo da página guarda o roteiro/conteúdo e as seções por plataforma (variações, RF-23); não é propriedade.

### Estágios
Nome (title); Papel (select: `pauta`, `pronto`, `autorizado`, `publicado`, `falha`, `cancelado`); Ordem (number).

Linhas iniciais de teste (uma por papel):

| Nome | Papel | Ordem |
|---|---|---|
| pauta | pauta | 1 |
| pronto | pronto | 2 |
| confirmado | autorizado | 3 |
| publicado | publicado | 4 |
| falha na publicação | falha | 5 |
| cancelado | cancelado | 6 |

### Séries
Nome (title); Continuidade (checkbox); Tema (relation -> Temas).

### Formatos
Nome (title); Tipo de mídia (select: imagem, carrossel, vídeo); Plataformas compatíveis (multi_select);
Regras de validação das artes (rich_text); Mapeamento por plataforma (rich_text).

### Temas
Nome (title).

### Execuções
Chave de idempotência (title); Post (relation -> Posts); Plataforma (select); Tentativa (select: 1, 2);
Resultado (select: sucesso, sucesso em modo privado, falha, recusado, simulado, atrasado, não executado, incerto,
sinal de vida); Código do motivo (rich_text); Texto do motivo (rich_text, sanitizado); Início (date);
Fim (date); ID do post na plataforma (rich_text); Link da execução no GitHub (url).

### Conexões
Plataforma (title); Estado (select: ativa, expirando, inválida); Expira em (date); Verificado em (date).

### Parâmetros
Nome (title); Valor (number); Descrição (rich_text).

## Relações

Todas são unidirecionais (sem propriedade espelho no banco de destino):

- Posts.Estágio -> Estágios
- Posts.Formato -> Formatos
- Posts.Série -> Séries
- Posts.Tema -> Temas
- Séries.Tema -> Temas
- Execuções.Post -> Posts

## Fórmulas

Ano e Mês usam a data agendada ou, se ela estiver vazia, a data de criação do post (RF-32):

- Ano: `year(ifs(empty(prop("Data e hora")), prop("Criado em"), prop("Data e hora")))`
- Mês: `month(ifs(empty(prop("Data e hora")), prop("Criado em"), prop("Data e hora")))`

Observação: a API de leitura não expõe o texto das fórmulas (só um `codeUrl`), então a verificação confirmou que as
duas propriedades existem com tipo `formula`, mas não releu a expressão. Verificar no Notion que não há erro de
fórmula (a ser testado ao criar o primeiro post de exemplo).

## Decisões onde o plano era ambíguo

1. **Relações unidirecionais.** O plano não pede propriedades espelho; criei só o lado necessário.
2. **Data e hora** é uma única propriedade date (com fuso por valor), não duas.
3. **Criado em** (created_time) foi adicionado ao Posts apenas porque a fórmula de Ano/Mês precisa dele (RF-32).
   Não consta literalmente na lista do plano.
4. **Mês** é número (1 a 12); **Ano** é número.
5. **Artes** é texto simples (um caminho por linha, com prefixo opcional `plataforma:`), pois o plano diz
   "referências de caminho" sem tipo.
6. **Selo de confirmação** é rich_text (hash hexadecimal); **Confirmado em** é date.
7. **Plataformas**: opções Instagram, TikTok, YouTube, Substack (as quatro citadas no plano), mesmas em Posts,
   Formatos e Execuções.
8. **Formatos**: "regras de validação" e "mapeamento por plataforma" (A-023) são texto livre na v1;
   o mapeamento padrão (Reels, Feed, Stories) ainda não foi populado, não era escopo da T005.
9. **Execuções**: Tentativa como select (1/2); Resultado com os 9 valores do plano; chave de idempotência é o título.
   "Código do motivo" e "Texto do motivo" são campos separados.
10. **Conexões**: Plataforma é o título.
11. **Parâmetros**: o plano só diz que guarda a frequência efetiva (D7/A-012). Modelei como pares
    Nome/Valor numérico/Descrição, sem linhas iniciais. Os limites de segurança ficam no YAML, não aqui.
12. **Estágio de papel `falha`** chama-se "falha na publicação" (nome citado em RF-30/fluxo); `publicado`,
    `pronto`, `pauta`, `cancelado` usam o próprio nome do papel; o autorizado se chama exatamente "confirmado".
13. **Unicidade do estágio autorizador**: o Notion não impõe restrições; a guarda (seção 4.1, item 1) é quem valida
    "exatamente um estágio com papel autorizado chamado confirmado".
14. Nenhum post de exemplo foi criado.

## Divergências entre o plano e o permitido pelo Notion

- A fórmula e o tipo "Papel" funcionaram como no plano. Não houve divergência funcional, apenas: relações
  unidirecionais (decisão 1), propriedade extra "Criado em" (decisão 3) e texto da fórmula não verificável por leitura.
