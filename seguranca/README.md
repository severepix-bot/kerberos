# Segurança e regras do projeto

Este diretório reúne um resumo dos principais arquivos, políticas e regras de segurança do projeto.

## Arquivos relevantes

### 1) README.md
Arquivo principal de orientação do projeto.

Principais regras:
- princípio de menor privilégio
- aprovação antes de ações sensíveis
- instalação/download/acesso externo exigem confirmação
- processos do projeto exigem aprovação para iniciar, enviar entrada ou encerrar
- ações que alterem dados via navegador ou desktop exigem confirmação
- uso de MCP e hubs externos exige controle e consentimento
- não é um sandbox; executar comando sem entender o efeito é proibido
- dados sensíveis não devem ser enviados a serviços externos sem escopo explícito

### 2) .github/workflows/ci.yml
Arquivo de integração contínua.

Regras e política:
- `permissions: contents: read`
- execução de lint, type-check e testes em push/PR
- validação automática com:
  - Ruff
  - mypy
  - pytest com cobertura

### 3) config/hubs.example.json
Exemplo de configuração de hubs externos.

Regras:
- hubs externos devem ser confiáveis
- credenciais devem ficar em ambiente e não em JSON
- uso de HTTPS obrigatório para endpoints autenticados
- não expor chaves e segredos no repositório
- failover de contas deve ser controlado e manual

### 4) config/mcp.example.json
Exemplo de configuração de servidores MCP.

Regras:
- usar apenas servidores confiáveis
- não habilitar plugins desconhecidos
- cada chamada de servidor MCP exige confirmação

### 5) deploy/cerebro.env.example
Modelo de variáveis de ambiente do serviço.

Regras:
- usar segredo real e único em produção
- proteger arquivo com permissões adequadas
- não usar tokens de exemplo em ambientes expostos

### 6) deploy/cerebro.service.example
Exemplo de unit/service do sistema.

Regras:
- execução com usuário de serviço
- isolamento de privilégios
- evitar exposição de portas sensíveis

### 7) deploy/Caddyfile.example
Exemplo de proxy/HTTPS para a aplicação.

Regras:
- restringir SSH e portas públicas
- usar HTTPS via Caddy
- não expor Ollama, Cerebro ou MCP para internet sem necessidade
- manter acesso apenas ao que for estritamente necessário

### 8) .gitignore
Arquivo de proteção de artefatos locais.

Função:
- impedir versionamento acidental de cachês, ambiente local e arquivos temporários

## Regras de acesso sudo e `NOPASSWD:ALL`

Não há qualquer regra do projeto autorizando `NOPASSWD:ALL` em `sudoers`.

Política efetiva do repositório:
- usar o mínimo privilégio necessário
- evitar elevação irrestrita de privilégios
- manter SSH restrito e acesso administrativo controlado
- operar o serviço com usuário dedicado `cerebro`
- separar usuário de serviço de conta de administração pessoal
- manter secrets fora do repositório e protegidos por permissões adequadas

Recomendação de segurança:
- `NOPASSWD:ALL` deve ser considerado proibido em ambientes sensíveis
- preferir sudo com senha ou execução por usuário específico e escopo limitado
- se houver `sudoers`, o ideal é permitir apenas comandos explícitos e necessários

## Políticas de segurança efetivas do projeto

### 1) Menor privilégio
A execução deve usar o mínimo de permissões necessário.

### 2) Confirmação explícita para ações sensíveis
Ações como instalação, download, deploy, acesso externo, alteração de dados e interação com ferramentas externas precisam de aprovação.

### 3) Isolamento de redes e serviços
Não expor internamente endpoints como Ollama, Cerebro ou MCP publicamente sem necessidade.

### 4) Proteção de segredos
Chaves, tokens e secrets não devem ficar em arquivos versionados. Devem ser configurados em ambiente seguro.

### 5) Validação contínua
CI executa lint, análise de tipos e testes para reduzir regressões e falhas de segurança.

### 6) Segurança defensiva
O projeto reconhece limitações e reforça que não há garantia absoluta de segurança, mas exige boa prática, confirmação e controle de escopo.

### 7) Uso consciente de integrações externas
Hubs e servidores MCP só devem ser habilitados quando forem confiáveis e quando seu uso estiver claramente consentido.

## Resumo executivo

Este projeto não possui um `SECURITY.md` tradicional na raiz, mas a política de segurança está distribuída em documentos e arquivos de configuração, com foco em:
- menor privilégio
- confirmação explícita
- proteção de segredos
- isolamento e restrição de rede
- validação por CI
- cuidado com integrações externas e dados sensíveis

## Observação

Este resumo foi criado para documentação local e reforça as regras declaradas no projeto. Em ambientes de produção, também é recomendável criar um `SECURITY.md` formal com política de disclosure, resposta a vulnerabilidades e processo de reporte.
