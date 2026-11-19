# Radar Cultural — personalização e recomendações

O Radar é um catálogo dinâmico de livros, músicas, séries, filmes, jogos, mangás e HQs. Os conteúdos vêm das APIs externas conectadas ao Flask; o projeto não mantém uma lista fictícia de títulos.

## Novidade: contas e recomendações personalizadas

O projeto agora inclui cadastro/login, preferências e histórico por usuário.

1. Crie uma conta no botão **Entrar / criar conta**.
2. Selecione categorias e informe gêneros favoritos separados por vírgula.
3. Abra os detalhes de conteúdos, salve favoritos e publique avaliações enquanto estiver conectado.
4. A seção **Descobertas para você** usa esses sinais para ordenar os conteúdos que já foram carregados no catálogo.

### Como funciona a inteligência

- **Perfil de conteúdo (desde o primeiro uso):** soma preferências explícitas de categoria/gênero com sinais de avaliações, favoritos e visualizações. Avaliações com notas altas reforçam tags; notas baixas reduzem sua afinidade.
- **Rede neural colaborativa (NumPy):** é uma pequena rede neural com embeddings de usuário e conteúdo, uma camada oculta ReLU e saída sigmoide. Ela é treinada com interações de contas registradas.
- **Fase de aprendizado:** a rede neural colaborativa só é ativada com pelo menos 20 interações, 2 usuários diferentes e 6 conteúdos distintos. Antes disso, o Radar usa o perfil de conteúdo e informa que o modelo ainda está aprendendo.
- **Itens novos:** conteúdos que ainda não aparecem no histórico de treinamento são pontuados pelo perfil de conteúdo; a rede neural não inventa previsões para IDs desconhecidos.
- **Re-treinamento/cache:** o modelo treinado fica em memória e é refeito quando o conjunto de interações muda. O servidor reinicia a sessão do modelo, mas não perde os dados do SQLite.

Isto é um modelo inicial de aprendizado, não um recomendador de produção nem um modelo previamente treinado. Com poucos usuários, as recomendações podem ser simples. Quanto mais pessoas avaliarem/interagirem com itens em comum, mais informação colaborativa haverá.

### Privacidade e contas

- Senhas são armazenadas como hash usando Werkzeug; a senha original não é gravada.
- A sessão é mantida por cookie HttpOnly e SameSite=Lax.
- Avaliações publicadas sem login continuam aparecendo publicamente, mas não alimentam o perfil individual.
- Visualizações, favoritos, avaliações e preferências de contas registradas são guardados no SQLite e usados para personalização.
- Para produção na internet ainda será necessário HTTPS, proteção CSRF, limitação de tentativas de login, moderação e política de privacidade. Esta implementação é para desenvolvimento local.
- Um arquivo `.flask_secret` é criado automaticamente para manter a sessão entre reinicializações. Ele é ignorado pelo Git. Você também pode definir `FLASK_SECRET_KEY` no `.env`.

## Fontes por categoria

| Categoria | API | Credencial |
|---|---|---|
| Livros | Google Books | Pública; chave opcional |
| Filmes e séries | TMDB | API Read Access Token |
| Jogos | RAWG | API key |
| Mangás | AniList | GraphQL público |
| Músicas | Last.fm | API key |
| HQs | Comic Vine | API key |

As políticas, cotas e disponibilidade pertencem a cada provedor e podem mudar. Consulte os termos oficiais antes de publicar ou usar comercialmente.

## Requisitos

- Python 3.10 ou superior
- Internet para consultar as APIs e carregar Chart.js
- Credenciais para TMDB, RAWG, Last.fm e Comic Vine
- NumPy (instalado automaticamente por `requirements.txt`)

## Executar no Windows

1. Extraia a pasta e abra-a no VS Code.
2. Copie `.env.example` para `.env` e preencha suas chaves.
3. Dê dois cliques em `run.bat`.
4. Abra http://127.0.0.1:5000/.

O `run.bat` cria `.venv`, instala `requirements.txt` e inicia o Flask. Não use Live Server nem abra o HTML diretamente: as rotas `/api/...` são servidas pelo Flask.

Execução manual no PowerShell, dentro da pasta do projeto:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env  # só se .env ainda não existir
.\.venv\Scripts\python.exe app.py
```

### Credenciais

```dotenv
TMDB_READ_ACCESS_TOKEN=seu_token
RAWG_API_KEY=sua_chave
LASTFM_API_KEY=sua_chave
COMICVINE_API_KEY=sua_chave
GOOGLE_BOOKS_API_KEY=
FLASK_SECRET_KEY=
```

Nunca coloque credenciais em `script.js` ou `index.html`. Não envie `.env` nem `.flask_secret` para repositórios públicos.

## Endpoints locais

| Método | Rota | Função |
|---|---|---|
| GET | `/api/health` | Saúde do Flask e estado das credenciais |
| GET | `/api/search?category=Livros&q=Fundação&page=1` | Consultar/paginar biblioteca externa |
| POST | `/api/auth/register` | Criar usuário e iniciar sessão |
| POST | `/api/auth/login` | Entrar |
| POST | `/api/auth/logout` | Sair |
| GET | `/api/auth/me` | Verificar sessão atual |
| GET/PUT | `/api/preferences` | Consultar/salvar categorias e gêneros favoritos |
| POST | `/api/interactions` | Registrar view/favorite/unfavorite (com login) |
| POST | `/api/recommendations` | Ordenar candidatos de acordo com o perfil (com login) |
| POST | `/api/reviews` | Publicar avaliação; autenticado associa a review ao usuário |
| GET | `/api/reviews?source=google-books&external_id=ID` | Listar avaliações de um conteúdo |
| GET | `/api/reviews/summary?source=google-books&external_id=ID` | Média e distribuição das notas |

## Banco de dados

O SQLite cria estas tabelas automaticamente:

- `users`: usuários e hash de senha.
- `user_preferences`: categorias e gêneros que o usuário selecionou.
- `interactions`: visualizações, favoritos, remoções de favorito e notas normalizadas.
- `reviews`: comentários e notas da comunidade, com `user_id` opcional.

Na inicialização, o app adiciona `user_id` à tabela de reviews antiga quando necessário e mantém avaliações antigas como anônimas. Não apague `radar.db` se quiser preservar avaliações e perfis.

## Paginação e limites

O Radar carrega resultados em páginas e o botão **Carregar mais resultados** acrescenta itens aos existentes. Cada API tem suas próprias cotas e limites. O Google Books aceita até 40 volumes por requisição e usa `startIndex`; nenhuma API permite resultados ilimitados numa única chamada.

## Gráfico de avaliações

A janela de detalhes mostra a média e um gráfico de barras com a quantidade de avaliações de 1 a 5 estrelas. O gráfico usa Chart.js e os dados vêm do SQLite.

## Créditos e uso dos provedores

- TMDB: requer atribuição, aviso legal e uso correto do logo.
- RAWG, Last.fm e Comic Vine têm exigências de atribuição, limites e regras de uso próprias.
- AniList e Google Books também aplicam cotas e termos.

Consulte a documentação/termos atuais de cada provedor antes de publicar. O projeto local não é automaticamente autorizado para uso comercial.

## Estrutura

```text
radar-cultural/
├── index.html
├── style.css
├── script.js
├── app.py
├── requirements.txt
├── .env.example
├── .gitignore
├── run.bat
├── README.md
├── .flask_secret  # gerado localmente na primeira execução
└── radar.db       # criado automaticamente
```
