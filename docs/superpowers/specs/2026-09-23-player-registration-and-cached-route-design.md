# Registro de jogador + rota cacheada por uuid — design

Branch alvo: `feat/mesh-routing` (app + coletor) + `gh-pages` (site)
Data: 2026-09-23

## Problema

Hoje o site (`.gh-pages-worktree/index.html`) descobre seu ping real de
duas formas efêmeras: ponte local (`measureViaLocalApp`, exige o app
aberto e respondendo `127.0.0.1:5757` no exato momento do clique) ou STUN
(`measureProxyRttViaStun`, exige o proxy escolhido rodar STUN responder).
Sem nenhum dos dois, cai pra estimativa geográfica.

O app (`player-ping-app/`) já mede ping real UDP contra todos os alvos
conhecidos e já envia isso pro coletor via `POST /player-route`, que já
calcula a rota fim-a-fim (você → QWFWD(s) → destino, ex. um servidor KTX)
somando seu ping de entrada com o grafo de pings entre proxies já medido
pelo coletor (`dijkstra_with_extra_edges`). Esse cálculo é correto e já
existe — o problema é que é **stateless**: o coletor não guarda nada, só
responde na hora do POST. O site não tem como reaproveitar esse resultado
depois, porque não fala com o mesmo POST nem sabe qual uuid usar.

## Escopo desta mudança

1. Coletor guarda os últimos samples de cada uuid por um TTL curto, e
   expõe um `GET` pra recalcular a rota a partir deles sem exigir um novo
   POST na hora.
2. Site permite a pessoa se registrar (nick, país, cidade) uma vez;
   registro fica vinculado ao mesmo uuid que o app já gera e persiste
   localmente. Um código curto gerado no registro é colado uma vez no
   app pra linkar os dois processos (não dá pra sincronizar automático:
   app nativo e navegador são sandboxes separados, sem canal em comum
   além do que o usuário copia manualmente).
3. Cálculo fim-a-fim (seu ping de entrada + ping QWFWD→destino já medido
   pelo coletor, multi-hop se for o caso) **não muda** — já é
   `dijkstra_with_extra_edges`, só passa a ser consultável via cache em
   vez de exigir POST síncrono no clique.

**Fora de escopo:**
- Ranking público / leaderboard por nick — registro serve só pra linkar
  app↔site nesta v1, nick/país/cidade não aparecem em UI pública ainda.
- Trocar uuid por nick como chave técnica — uuid continua a chave; nick é
  metadado associado a ele.
- Persistência em banco de dados — arquivo JSON simples no coletor,
  mesmo padrão de simplicidade do resto do projeto (sem SQLite/Postgres
  novo pra este volume de dados).
- Autenticação/senha — registro é só um nick de exibição, sem conta.

## Arquitetura

```
[App, já existente]
  1. Mede ping local (QwPing), POST /player-route?to=X {uuid, samples}
     -> resposta síncrona de rota (já existe, sem mudança)
     -> NOVO: coletor também salva {uuid: (timestamp, samples)} no
        cache em memória

[Site, novo fluxo de registro]
  2. Primeira visita: formulário nick/país/cidade
     -> POST /player-register {nick, country, city} (coletor gera um
        novo uuid + código curto de 6 dígitos)
     -> resposta: {uuid, link_code}
     -> site salva uuid em localStorage, mostra link_code na tela
        (este uuid é a chave estável e definitiva - o site nunca troca
        de uuid depois do registro)
  3. Pessoa abre o app, cola o link_code num campo novo da janela
     -> app faz POST /player-link {link_code}
     -> coletor devolve o uuid gerado no passo 2 (o do REGISTRO, não um
        uuid do app)
     -> app IMPORTA esse uuid, substituindo o seu próprio em
        ClientIdentity (sobrescreve client-id.txt) - dali em diante o
        app é quem passa a usar o uuid nascido no site
  4. Dali em diante: app manda POST /player-route com esse uuid
     importado (sem mudança de formato), populando o cache do passo 1
     sob o MESMO uuid que o site já tem no localStorage desde o passo 2.
     Não existe troca de chave depois do link, nem janela de uuid órfão.

[Site, consulta de rota]
  5. Ao clicar num destino: tenta, nesta ordem
     a. GET /player-route-cached?uuid=<localStorage>&to=<addr>
        -> se o coletor tem samples recentes (TTL não expirado) pra esse
           uuid, recalcula e devolve a mesma forma de resposta do POST
     b. measureViaLocalApp (como hoje, ponte 127.0.0.1, útil quando o
        app está aberto agora e o cache ainda não chegou nesse alvo)
     c. measureProxyRttViaStun (como hoje)
     d. estimatePlayerLegMs (como hoje)
```

## Componentes

### Coletor: cache de samples por uuid (`collector.py`)

- `_player_samples: dict[str, tuple[float, list[dict]]]` — uuid →
  (timestamp, samples brutos), protegido por `_player_samples_lock`
  próprio (não reusa `graph.lock`, dados diferentes).
- `PLAYER_SAMPLES_TTL_SECONDS = 120` — ping muda rápido; dado velho
  demais é pior que cair pro fallback do site.
- `POST /player-route` (handler existente): depois de validar e calcular
  a rota como já faz, adiciona uma linha salvando
  `_player_samples[uuid] = (time.time(), samples)`. Sem mudança no
  formato de request/response existente.
- Novo `GET /player-route-cached?uuid=...&to=ip:port`:
  - `uuid` inválido (`_valid_uuid` já existe) → 400.
  - `to` malformado (`parse_addr_param` já existe) → 400.
  - uuid sem entrada no cache, ou entrada expirada (agora - timestamp >
    TTL) → 404 `{"error": "no cached samples"}` — site trata igual a
    "ponte indisponível", cai pro próximo fallback sem popup.
  - Caso contrário: reusa a mesma lógica de validação de samples e
    `dijkstra_with_extra_edges` que o POST já usa (extrai isso pra uma
    função compartilhada `_route_from_samples(uuid, samples, to_addr)`
    pra não duplicar as ~30 linhas de validação/cálculo) — mesma
    resposta JSON do POST.
  - Rate limit: reusa `_rate_limited` (por IP, já existe); não precisa
    de `_uuid_rate_limited` aqui porque não há POST/gravação, é leitura.

### Coletor: registro de jogador (`collector.py` + novo `players.json`)

- `_players: dict[str, dict]` carregado de `collector/players.json` no
  boot (arquivo simples, cria vazio `{}` se não existir), salvo em disco
  a cada escrita — mesmo padrão do resto do coletor (sem serialização
  incremental sofisticada, é um dict pequeno).
- Registro: `{uuid: {"nick": str, "country": str, "city": str,
  "registered_at": float}}`.
- `POST /player-register` `{nick, country, city}`:
  - Valida `nick` (1-24 chars, sem controle/whitespace só), `country`/
    `city` (1-64 chars cada, texto livre — não é validação geográfica
    rígida, é só o que a pessoa digitou).
  - Gera um novo `uuid` (mesmo formato do app, `uuid.uuid4()`) e um
    `link_code` de 6 dígitos numéricos, não colidente com códigos ainda
    não resgatados (`_pending_links: dict[str, str]` código → uuid, TTL
    de 15 minutos — código não resgatado expira, evita acumulação).
  - Salva o registro em `_players[uuid]`, salva `_pending_links[code] =
    uuid`, responde `{"uuid": uuid, "link_code": code}`.
- `POST /player-link` `{"link_code": str}` (chamado pelo APP, não pelo
  site):
  - Código inválido/expirado → 404.
  - Válido: apenas resolve `link_code -> uuid` via `_pending_links`
    (sem criar nem trocar nenhum registro — o registro já existe desde
    o passo 2, sob o uuid do site); remove o código de `_pending_links`
    (uso único).
  - Responde `{"uuid": ..., "nick": ..., "country": ..., "city": ...}`
    — esse é o mesmo uuid que o site já tem em `localStorage`. O app usa
    esse valor pra **sobrescrever** o seu próprio uuid local
    (`ClientIdentity`), não o contrário. Não há troca de chave no
    coletor, não há uuid órfão: o site sempre teve a chave certa desde
    o registro.

### App: campo de link (`MainWindow.axaml` + `.axaml.cs`, `BackendClient.cs`)

- Novo `TextBox` + botão "Vincular" na janela (perto do UUID local já
  exibido) — usuário cola o código de 6 dígitos do site.
- `BackendClient.LinkAsync(baseUrl, linkCode)` → `POST /player-link`.
  Sucesso: `ClientIdentity` sobrescreve `client-id.txt` com o uuid
  retornado, UI mostra nick confirmado; falha (código inválido/
  expirado): mensagem de erro simples, sem crash, uuid local mantido
  como estava.

### Site: registro + consulta cacheada (`index.html`)

- Formulário simples (nick/país/cidade) na primeira visita (sem
  `localStorage.uuid` ainda) — ou acessível por um link/botão
  "registrar" permanente, pra quem pulou a primeira vez.
- `POST {COLLECTOR_BASE}/player-register`, salva `uuid` retornado em
  `localStorage`, mostra `link_code` numa tela clara ("cole este código
  no app: 123456").
- `chooseNearbyDestination`: nova primeira tentativa
  `fetchCachedRoute(candidate.addr)` → `GET
  {COLLECTOR_BASE}/player-route-cached?uuid=<localStorage>&to=<addr>`,
  antes de `measureViaLocalApp`. 200 → usa o RTT/rota, marca
  `playerLegSource = "cached"`. 404/erro → segue pra
  `measureViaLocalApp` → STUN → estimativa, como hoje.

## Testes

- Coletor: `tests/test_player_route_cache.py` — POST popula cache, GET
  cacheado retorna a mesma rota, GET após TTL expirar retorna 404, uuid
  desconhecido retorna 404. `tests/test_player_register.py` — registro
  gera uuid+code válidos, link troca a chave corretamente, código usado
  duas vezes falha na segunda, código expirado falha.
- App/site: verificação manual (mesmo padrão do resto do projeto, sem
  framework de teste em C#/JS hoje) — registrar no site, colar código no
  app, rodar um scan, confirmar que `/player-route-cached` responde com
  os dados do último scan mesmo depois de fechar o app.

## Riscos aceitos

- `link_code` de 6 dígitos numéricos (1M combinações) com TTL de 15min e
  uso único — força bruta é impraticável no tempo de expiração, mas o
  endpoint não tem rate limit dedicado a tentativas de código incorreto
  além do `_rate_limited` por IP já existente; aceitável pro risco (o
  pior caso é vincular a UUID errado de outro jogador que também estava
  registrando naquele minuto, não um dado sensível).
- App importando (sobrescrevendo) seu próprio `client-id.txt` no link
  significa que qualquer rota/histórico pessoal anterior sob o uuid
  antigo do app fica "órfão" localmente — aceitável, o app não guarda
  histórico hoje (cada scan é stateless na UI), só o uuid em si.
- Arquivo `players.json` sem lock de arquivo entre escritas concorrentes
  — aceitável no volume esperado (registro é ação humana rara, não
  request de alta frequência como `/player-route`), mas escrita usa
  "ler tudo, escrever tudo" sem journaling; perda de uma escrita
  concorrente rara é aceitável pra v1 (metadado de exibição, não dado
  crítico).
