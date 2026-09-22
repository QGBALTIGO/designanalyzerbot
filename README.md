# Design Analyzer Bot 🎨

DesignAnalyzerBot é um bot comercial para Telegram que transforma URLs públicas em análises de design, auditorias técnicas, clones offline, arquivos de preservação e projetos editáveis para desenvolvimento.

O núcleo usa `designsys`, Playwright/Chromium, Lighthouse, axe-core, Wappalyzer-compatible fingerprints, WARC e ferramentas próprias de clonagem/reconstrução.

## Recursos

### 🔍 Analisar site

- design system completo via `designsys`;
- PDF;
- tokens W3C;
- CSS variables;
- Tailwind config;
- componentes;
- style guide;
- fila, cota e progresso ao vivo.

### 🧬 Clone seguro de página

- DOM renderizado com Chromium;
- lazy-loading;
- imagens, fontes, ícones e CSS salvos localmente;
- reescrita de URLs;
- screenshot original e offline;
- similaridade visual;
- manifest JSON;
- scripts, iframes, formulários, login e checkout desativados.

### 🕷 Clone multipágina

- descoberta de páginas internas do mesmo domínio;
- Pro: até 3 páginas por padrão;
- Agency: até 12 páginas por padrão;
- links internos reescritos para navegação offline;
- sitemap.xml;
- Markdown por página;
- site-map.json;
- ZIP navegável.

### 📄 HTML único

Gera um único arquivo HTML com CSS, imagens e fontes locais incorporados como data URLs, sem depender da rede para abrir.

### 🧪 Auditoria completa

Entrega um pacote contendo:

- Lighthouse;
- Performance Web API;
- SEO;
- axe-core / acessibilidade;
- headers de segurança;
- links quebrados;
- tecnologias;
- screenshot;
- PDF;
- conteúdo legível em Markdown;
- WARC;
- relatório JSON;
- relatório HTML navegável.

### 🧠 Tecnologias

Detecção baseada nos fingerprints do WebAnalyze/Wappalyzer, com DOM renderizado, headers, meta tags, cookies e scripts.

### 🖼 Galeria de assets

- imagens e ícones encontrados;
- contact sheet;
- gallery.html navegável;
- arquivos originais;
- ZIP completo.

### 🔄 Histórico e comparação de versões

Cada snapshot premium pode ser persistido no SQLite.

A comparação mostra:

- diferença visual;
- imagem de diff;
- assets adicionados/removidos;
- tecnologias adicionadas/removidas;
- hash do conteúdo;
- comparison.json.

### 🧱 Reconstrução editável

`/reconstruir` gera em um único ZIP:

- HTML/CSS offline;
- React + Vite;
- Next.js App Router;
- scaffold Tailwind;
- tokens de cores e fontes;
- preview.

### ✨ Modernizar / Inspire-se

- usa conteúdo e sinais visuais da referência;
- gera uma composição responsiva nova e editável;
- funciona sem API externa através de fallback determinístico;
- opcionalmente usa OpenAI quando `OPENAI_API_KEY` estiver configurada;
- nunca preserva scripts, login ou checkout da referência.

## Comandos

### Base

- `/start`
- `/analisar <url>`
- `/clonar <url>`
- `/assets <url>`
- `/status [id]`
- `/historico`
- `/plano`
- `/cancelar <id>`
- `/id`

### Premium

- `/premium`
- `/auditar <url>`
- `/clonarsite <url>`
- `/htmlunico <url>`
- `/galeria <url>`
- `/tecnologias <url>`
- `/comparar <url>`
- `/versoes [url]`
- `/reconstruir <url>`
- `/modernizar <url>`
- `/inspirar <url>`

### Admin

- `/setplan USER_ID free|pro|agency`

Administradores têm acesso funcional total para testes.

## Planos

| Recurso | Free | Pro | Agency |
|---|---:|---:|---:|
| Análises / mês | 1 | 10 | 100 |
| Páginas internas no designsys | 2 | 8 | 20 |
| Clone simples | — | ✅ | ✅ |
| Extração de assets | — | ✅ | ✅ |
| Auditoria completa | — | ✅ | ✅ |
| HTML único | — | ✅ | ✅ |
| Tecnologias profissionais | — | ✅ | ✅ |
| Galeria de assets | — | ✅ | ✅ |
| Histórico e comparação | — | ✅ | ✅ |
| Clone multipágina | — | 3 páginas | 12 páginas |
| React / Next / Tailwind | — | — | ✅ |
| Modernizar / Inspire-se | — | — | ✅ |
| Créditos premium / mês | 0 | 50 | 500 |

Os valores são defaults e podem ser alterados por variáveis de ambiente.

### Custos em créditos

- extração de assets simples (`/assets`): 1;
- clone simples (`/clonar`): 2;
- tecnologias: 1;
- HTML único: 2;
- galeria: 2;
- comparação: 2;
- auditoria: 4;
- clone multipágina: 1 crédito por página disponível no plano, com mínimo de 2;
- reconstrução editável: 6;
- modernizar: 8;
- inspire-se: 8.

Operações que falham tecnicamente são marcadas como falha e os créditos deixam de contar no consumo mensal.

## Segurança

A entrada de URLs é tratada como não confiável.

- somente HTTP/HTTPS;
- URLs com credenciais são rejeitadas;
- bloqueio de localhost e sufixos internos;
- DNS é resolvido e IPv4/IPv6 privados, loopback, link-local, multicast, reservados e não especificados são rejeitados;
- redirects de downloads são validados salto a salto;
- subrequisições do Chromium também passam por bloqueio de rede privada;
- o Chromium usado pelo Lighthouse é roteado por um proxy local que valida e fixa cada destino a IPs públicos antes da conexão;
- service workers são bloqueados nos fluxos de captura;
- concorrência e timeouts configuráveis;
- previews muito altos são reduzidos antes do envio ao Telegram;
- fallback para documento se o Telegram rejeitar uma imagem;
- token e segredos somente por environment variables.

Para escala pública elevada, a evolução recomendada continua sendo isolar cada job em container/sandbox com egress de rede controlado.

## Persistência

O Railway usa volume persistente em `/app/data`.

Por padrão:

- SQLite: `/app/data/designanalyzer.db`;
- jobs: `/app/data/jobs`;
- premium: `/app/data/premium`.

Snapshots, comparações e consumo de créditos sobrevivem a redeploys.

## IA opcional

`/modernizar` e `/inspirar` não dependem de IA para funcionar.

Quando configurado:

```env
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-5.6-luna
```

o bot tenta uma reconstrução assistida pelo modelo e usa o gerador local como fallback em caso de erro.

A chave nunca deve ser adicionada ao GitHub.

## Desenvolvimento

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
PYTHONPATH=. pytest -q
ANALYZER_MOCK=1 PYTHONPATH=. python scripts/smoke.py
```

## Testes de produção

A CI executa:

1. compilação Python;
2. testes unitários;
3. smoke do analisador;
4. opcionalmente clone real com Chromium na main;
5. smoke específico do BaltigoFlix quando solicitado;
6. build do Docker premium quando o commit da branch contém `[premium-smoke]`;
7. dentro desse Docker: Lighthouse, auditoria real, HTML único, exports HTML/React/Next/Tailwind e Modernizar sem IA.

## Deploy

`Dockerfile` e `railway.toml` estão prontos para Railway.

O Docker de produção inclui:

- Python 3.12;
- Playwright Chromium;
- designsys;
- Lighthouse 13.5;
- Node 22;
- fingerprints do WebAnalyze.

Configuração completa em `.env.example`.

## Licenças

O código do projeto e os componentes de terceiros devem respeitar seus respectivos termos. Os avisos dos componentes incorporados estão em `THIRD_PARTY.md`.

SingleFile e Browsertrix Crawler foram avaliados durante a pesquisa, mas não são incorporados ao runtime desta implementação.
