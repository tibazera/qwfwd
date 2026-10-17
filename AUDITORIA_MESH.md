# Auditoria do qwfwd mesh — 2026-09-30

## Veredito

**Ainda não liberar como pronto para uso geral nem como superior ao upstream.** Há integração funcional de mesh no cliente modificado, builds bem-sucedidos e testes locais úteis. Restam falhas de identidade dos nós, qualidade/frescor das medições, cobertura do grafo e validação operacional que impedem garantir a melhor rota disponível.

O objetivo verificável deve ser: escolher a melhor rota **entre as rotas descobertas, medidas recentemente e executáveis**, com critério explícito de latência, perda e jitter. Nenhum algoritmo garante a melhor rota de toda a Internet com descoberta parcial e medições que mudam. A soma dos RTTs das arestas é uma estimativa; o caminho completo precisa ser medido.

## Escopo e evidências

- Repositório principal: branch `feat/mesh-routing`, base local `d12edfc`.
- Cliente unezQuake: `_mesh-drafts/ezquake-fork-native`, `f28b36e0`, branch `unez-branch`.
- Cliente ezQuake: `_mesh-drafts/ezquake-fork`, `24891cc`.
- Revisão do fluxo de descoberta, medição, grafo, seleção, encaminhamento, coletor/API, app, persistência e builds/CI. Não equivale a auditoria formal de segurança de cada linha do código C legado.
- As referências abaixo usam caminhos relativos a esta pasta. Checkouts dentro de `_mesh-drafts` são repositórios separados; suas alterações não são distribuídas automaticamente pelo repositório raiz.
- Há alterações locais anteriores desta sessão em `src/peer.c`, `src/qwfwd.h`, coletor, documentação e testes. Não houve deploy nem commit dessas alterações. Esta rodada acrescenta o relatório, sem iniciar a sequência de fixes.

## O fluxo real já implementado

1. O cliente mede servidores/proxies conhecidos no Server Browser.
2. Consulta proxies com `meshprobe` e fallback `pingstatus`.
3. Constrói um grafo e calcula caminhos; `connectbr` usa `SB_PingTree_GetRoutes`.
4. A rota vira `cl_proxyaddr`; o cliente transmite a cadeia em `prx` ao conectar.
5. Os qwfwd encaminham o tráfego pela cadeia.

Portanto, a integração mesh do cliente **existe**. A afirmação anterior de que ela faltava estava errada. O coletor HTTP/app é um fluxo separado, não um requisito desse caminho nativo. Há ainda um segundo caminho de seleção/conexão no Server Browser que usa o Dijkstra antigo, e isso importa para os bugs abaixo.

## Bloqueadores e correções prioritárias

### P1 — Identidade por IP mistura portas e funções no cliente

**Evidência:** `EX_browser_pathfind.c:146`, `:162`, `:266`, `:963` no cliente nativo: busca e criação usam IP; `proxport` guarda uma única porta. A porta reportada não integra a chave do nó.

**Consequência:** dois proxies no mesmo IP ou proxy e jogo no mesmo host podem se fundir. O custo de um serviço pode ser aplicado a outro; desaparecem distinções necessárias para montar uma cadeia executável.

**Correção mínima:** identidade por IP+porta, com papel de proxy explícito. Qualquer transferência local proxy→jogo deve ser uma aresta explícita e validada, não igualdade de IP.

**Aceite:** dois proxies e um jogo no mesmo IP permanecem distintos; jogo não é utilizado como relay; a cadeia conecta à porta realmente medida.

### P1 — Overflow no Dijkstra ainda utilizado pelo Server Browser

**Evidência:** cliente nativo `EX_browser_pathfind.c:759`: soma armazenada em `dist_t` (`short`). O parser aceita custo de aresta até `SHRT_MAX` (`:344–347`). `GetRoutes` usa inteiros com proteção, mas não substitui todos os chamadores antigos.

**Consequência:** somas acima de 32767 não são representáveis; podem virar custos negativos nas plataformas usadas e corromper a escolha. A reconstrução antiga precisa também de limite/checagem de ciclos.

**Correção:** usar soma segura e sentinela consistente em todos os caminhos; compartilhar a seleção validada quando possível. Não corrigir somente `connectbr`.

**Aceite:** grafo de custos altos não gera custo negativo, rota inválida ou loop; conectar pelo browser e por `connectbr` produz decisões compatíveis.

### P1 — Cliente e coletor aplicam políticas diferentes de qualidade

**Evidência:** parser do cliente `EX_browser_pathfind.c:344` usa `ping + jitter/2 + loss*2`; não rejeita perda elevada. O coletor agora rejeita perda conhecida >25%, além de penalizar relays. `meshprobe` omite perda de 100% no servidor, mas ainda publica perdas altas como 88%.

**Consequência:** o cliente pode escolher um caminho que o coletor considera impraticável. A pontuação combina grandezas distintas e não deve aparecer como RTT real.

**Correção:** documentar e alinhar limites de admissibilidade e pontuação; preservar RTT bruto separado do custo de decisão. Não tratar métrica desconhecida como evidência de ausência de perda.

**Aceite:** mesma topologia e métricas geram escolhas compatíveis; perda alta não vence apenas por ter RTT baixo.

### P1 — Frescor do coletor não avança e publicação depende da ordem de chegada

**Evidência:** `collector/collector.py:161` substitui aresta existente sem comparar idade/origem. `probe_one:311` mistura `pingstatus` direto e relatos de peers `meshstatus`; coleta é concorrente. Dijkstra (`:466`, `:612`) verifica `age_seconds` congelado, sem somar o tempo desde a coleta. `recollect_loop` conserva o snapshot após falha.

**Consequência:** cache antigo pode substituir observação mais nova; a ordem de conclusão das threads afeta a rota. Se a coleta falha repetidamente, arestas continuam passando pelo filtro de idade.

**Correção:** registrar instante de observação, comparar frescor e origem antes de substituir, expirar na consulta. Preferir medição rica direta quando disponível; fallback sem qualidade precisa ter semântica explícita.

**Aceite:** inverter a ordem dos relatos não altera a escolha da observação mais recente; avançar o relógio invalida arestas sem nova coleta.

### P1 — `meshprobe` entrega somente os primeiros 100 destinos

**Evidência:** `src/query.c:833`, `src/qwfwd.h:475`. Resposta única, sem paginação ou sinalização de continuidade para esse comando. A paginação de `meshstatus` não resolve a consulta `meshprobe` feita pelo cliente.

**Consequência:** a visão do cliente pode omitir o destino ou uma aresta essencial, conforme a ordem da lista. Descobrir os melhores caminhos exige antes descobrir as arestas relevantes.

**Correção:** consulta direcionada ao destino ou paginação compatível/versionada. Preservar limites de datagrama e ant amplificação.

**Aceite:** em uma lista com mais de 100 destinos, o último continua consultável e participa da seleção.

### P1 no fluxo app/site — Medições não incluem entradas da mesh

**Evidência:** `/player-targets` em `collector.py:1149` exclui proxies e deduplica por IP. `MainWindow.axaml.cs:54–87` mede essa lista; `:140` envia as mesmas amostras à rota.

**Consequência:** faltam arestas cliente→proxy. O app não consegue, em geral, comparar os pontos de entrada da mesh, embora consiga medir jogos. Isso não prova ausência de integração no cliente nativo.

**Correção:** distinguir destinos de jogo e candidatos de entrada; medir proxies para o grafo e o destino para comparação direta.

**Aceite:** cenário em que a melhor entrada não está no host do jogo produz uma rota multi-hop pelo app/API.

## Outras falhas concretas e melhorias

| Prioridade | Evidência | Problema e ação |
|---|---|---|
| P1 antes de distribuir app | `player-ping-app/LocalPingServer.cs` | CORS aberto, aceita qualquer IP/porta e inicia requisições sem limite. Uma página com acesso ao bridge pode disparar sondagens UDP inclusive para rede privada. Restringir origens/destinos esperados e concorrência; testar chamadas recusadas. A capacidade do navegador depende das suas políticas de acesso local. |
| P2 | `player-ping-app/QwPing.cs` | Considera a conclusão de `ReceiveAsync` como resposta válida, sem aguardar seu resultado nem verificar remetente/conteúdo. Uma falha da task pode ser contabilizada como RTT. Usar relógio monotônico, aguardar resultado e validar resposta QW do alvo. |
| P2 | `MainWindow.axaml.cs:18` | URL de backend é um túnel temporário compilado no app. Definir endereço estável/configurável antes de distribuição. |
| P2 | `src/main.c:142–152`, `src/peer.c:518` | Config é executada antes de registrar novas cvars. A forma simples `port_ping_probe_enable 0` pode ser comando desconhecido na primeira carga; `set` e argumentos `+` têm comportamento diferente. Registrar antes de carregar config e testar ambas as formas suportadas. |
| P2 | `src/peer.c` | Probe de quatro portas usa uma amostra por socket e início comum; não mede jitter/perda nem garante melhor porta estável. Aguarda até o prazo e acrescenta atraso à conexão. Tratar como otimização experimental, verificar ACKs simultâneos, consumo de descritores e conexões concorrentes. |
| P2 | `src/peer.c`, `src/query.c` | Porta escolhida para encaminhar não é a porta das medições periódicas do grafo. Validar o caminho real depois da seleção; não prometer que a pontuação prevê exatamente o tráfego final. |
| P2 | `collector.py:104`, cliente `MAX_NONLEAVES` | Coletor limita a 4 arestas; cliente limita reconstrução a 40. Não existe suporte contratual a quantidade ilimitada de hops. Definir limite prático comum, contar o trecho do jogador e testar fronteiras. Não basta remover o limite. |
| P2 | `collector.py:745` | Salva `players.json` diretamente, truncando antes de escrever; JSON inválido é carregado como vazio. Queda pode perder registros. Escrever arquivo temporário e substituir atomicamente, preservando diagnóstico/recuperação. |
| P1 para liberação reproduzível | `.github/workflows/*.yml` | Jobs condicionados a `QW-Group/qwfwd`: ficam desativados no fork. Sem gate integrado dos testes mesh/coletor. Habilitar build/testes no fork, mantendo deploy separado e deliberado. |
| P2 | `CMakeLists.txt` | Padrão C não fixado; compilação MinGW com GCC recente exigiu `-std=gnu99` por conflito de `false/true` com C23. Fixar padrão suportado. `ban.c` também aparece duas vezes na lista de fontes. |
| P2 | `_mesh-drafts/`, `.gitignore` | Clientes essenciais vivem em checkouts locais não rastreados pela raiz. Definir repositórios/commits oficiais e processo de build; excluir artefatos gerados sem apagar trabalho local. |
| P2 | `MESH_UPDATE.md`, testes | Documentação ainda contém afirmações antigas sobre integração do cliente; testes têm caminhos Windows específicos. Atualizar fluxo real e tornar execução reprodutível. |

## Pontos que precisam de prova adicional

- Limite total de `prx`/userinfo: uma cadeia aceita pelo grafo deve caber integralmente no protocolo; erro de serialização deve ser visível, nunca conexão silenciosa por outra rota.
- Capacidade de sockets/select durante probes simultâneos, especialmente descritores Linux acima de `FD_SETSIZE`. Aumento do limite Windows não resolve o caso Linux.
- Identificação de destinos no cliente não conhecidos inicialmente como proxy e comportamento quando apenas parte dos proxies responde.
- Proteção de cadastro/link/cache contra crescimento de memória e abuso; UUID não deve ser tratado implicitamente como autenticação forte.
- Segurança dos parsers C legados, sanitizers e tráfego malformado sustentado. Compilar não elimina riscos de memória.
- Assimetria, perda e jitter do caminho completo: soma de RTTs médios e jitter ponderado por aresta não substitui a medição fim a fim.

Estes itens são lacunas de validação, não falhas de exploração já demonstradas.

## Verificações já feitas nesta sessão

| Verificação | Resultado e limite |
|---|---|
| qwfwd Linux/WSL | Build passou; warnings existentes em query/svc. |
| qwfwd Windows/MinGW | Build passou com C99 explícito; build padrão com toolchain recente precisa ajuste. |
| App .NET | Build passou, sem warnings reportados. |
| Cliente unezQuake Linux | Build completo passou; warnings não equivalem a validação em jogo. |
| Protocolo mesh | 19/19 verificações passaram. |
| Mesh e2e local | 6/6 passaram. |
| Player route | 20/20 passaram. |
| Cache de rota | 11/11 passaram. |
| Cadastro/link | 12/12 passaram. |
| Probe de porta | Dois cenários passaram: usa socket vencedor para handshake/tráfego e mantém socket original quando não há ACK. |

Esses resultados são das execuções desta sessão, não de CI nem de um ensaio Brasil→Rússia. O teste local de porta usa servidor simulado; prova conservação do socket, não ganho de latência na Internet. Testes atuais também podem validar um contrato inadequado: `/player-targets` excluir proxies passa no teste, mas deixa o fluxo app sem entradas da mesh.

## Comparação honesta com upstream

O fork acrescenta métricas de qualidade, troca de informações mesh, coletor/app e integração específica nos clientes modificados. São capacidades adicionais relevantes. O upstream já fornece encaminhamento/chaining; a escolha da rota depende também do cliente usado. Não atribuir ao binário upstream sozinho toda a limitação do fluxo original.

Ainda não há evidência controlada de que o conjunto novo entregue menor RTT/jitter/perda, maior disponibilidade ou melhor estabilidade. Para afirmar superioridade, comparar o mesmo destino, os mesmos proxies, o mesmo cliente/ambiente e janelas próximas: direto, caminho convencional e mesh. Medir também o custo de descoberta e tempo para entrar no jogo.

## Ordem proposta para começar a corrigir

1. **Grafo do cliente:** IP+porta, papel dos nós, soma segura, ciclos e cadeia serializada. Cobrir browser e `connectbr` juntos.
2. **Contrato de medição:** frescor, precedência de observações, perda admissível e separação custo/RTT. Alinhar cliente e coletor.
3. **Descoberta suficiente:** resolver corte de 100 destinos e definir hops suportados. Testar topologias em que o melhor caminho tem mais de dois proxies.
4. **Encaminhamento:** config, probes, preservação do socket e capacidade sob concorrência. Confirmar destino efetivo e caminho de retorno.
5. **Fluxo app/site:** entradas da mesh, validação de ping, bridge, backend estável e persistência. Pode seguir separado do piloto nativo.
6. **Entrega:** CI no fork, builds reproduzíveis e versões exatas de cliente/proxy/documentação.
7. **Piloto controlado:** somente após os bloqueadores dos componentes usados no piloto estarem resolvidos.

## Critérios para liberar o teste final

- Topologias determinísticas: direto vence; 1/2/3+ proxies vencem; mesma IP com portas distintas; destino além do centésimo registro.
- Falhas determinísticas: proxy indisponível, aresta expirada, perda alta, resposta malformada, custo elevado, ciclo e cadeia acima do limite.
- Cliente real realiza handshake e troca payload através de todos os proxies esperados; logs/captura confirmam portas e destino.
- Mudança de qualidade não provoca flapping; comportamento de recálculo/reconexão é explícito. Não presumir migração transparente de uma sessão em andamento.
- Piloto compara direto/convencional/mesh com RTT p50/p95, jitter definido, perda, sucesso e tempo de conexão; registrar rota e idade das medições.
- Declarar sucesso pela medição do tráfego completo. Se direto for melhor, escolher direto também é um resultado correto.

**Primeiro pacote recomendado:** identidade IP+porta e aritmética/reconstrução segura do cliente. É a base para confiar em qualquer ranking posterior.
