# Workshop: Pipeline CI/CD com GitHub + CodeBuild + ECS (Fargate)

## O que vamos construir

```
[GitHub] → [CodePipeline] → [CodeBuild] → [ECS Fargate]
 (fonte)                     (build +       (Blue/Green
                              push ECR)      nativo)
```

O app é um e-commerce com 2 microserviços em Python (FastAPI) que se comunicam via **AWS SQS** e persistem dados no PostgreSQL (RDS).

Ao fazer push no GitHub dentro da pasta `orders-service/`, a pipeline:
1. Detecta a mudança automaticamente (filtro de path no CodePipeline V2)
2. Builda a imagem Docker do `orders-service` e sobe no ECR
3. Faz deploy Blue/Green no ECS (nativo — sem CodeDeploy)

**Arquitetura dos serviços:**
```
[HTTP] → orders-service:3001 ──publica──→ [AWS SQS: workshop-orders]
              ↓                                         ↓
        [PostgreSQL RDS]               inventory-service:3002 (consumer)
                                                 ↓
                                         [PostgreSQL RDS]
```

**Filosofia do workshop:**
- `orders-service` — guiado: seguiremos cada passo juntos e montaremos a pipeline completa
- `inventory-service` — **desafio**: ao final, você vai montar a pipeline deste serviço sozinho

---

## Pré-requisitos

- Conta AWS com permissão de Admin (ou permissões para IAM, ECS, ECR, CodeBuild, CodePipeline, CodeConnections, RDS, SQS, CloudFormation, ALB)
- Uma VPC com pelo menos 2 subnets públicas (a default VPC funciona)
- Git e Docker instalados localmente
- Conta GitHub com um repositório criado para o workshop

---

## Estrutura dos arquivos do repositório

```
workshop-app/
├── orders-service/
│   ├── main.py            # API REST: POST/GET /orders, publica no SQS
│   ├── requirements.txt
│   └── Dockerfile
├── inventory-service/
│   ├── main.py            # Consumer SQS: reserva estoque no PostgreSQL
│   ├── requirements.txt
│   └── Dockerfile
├── docker-compose.yml          # ambiente local completo (Postgres + LocalStack + 2 serviços)
├── cloudformation-db.yml       # RDS PostgreSQL via CloudFormation
├── cloudformation-infra.yml    # ECR + SQS + IAM Roles + ALB + ECS Cluster via CloudFormation
├── buildspec.yml               # instruções para o CodeBuild (orders-service)
└── taskdef.json                # template da Task Definition do ECS (orders-service)
```

---

## PARTE 1 — Infraestrutura base

### 1.1 Preparar repositório GitHub e criar conexão AWS

**No GitHub:**
1. Crie um repositório público ou privado chamado `workshop-app`
2. Clone localmente e copie todos os arquivos do workshop:
   ```bash
   git clone https://github.com/SEU_USUARIO/workshop-app.git
   cd workshop-app
   # copie as pastas: orders-service/, inventory-service/
   # copie os arquivos: docker-compose.yml, cloudformation-*.yml,
   #                    buildspec.yml, taskdef.json
   ```
3. Faça o primeiro push:
   ```bash
   git add .
   git commit -m "initial commit"
   git push
   ```

**Na AWS — criar conexão GitHub (AWS CodeConnections):**
4. Acesse **Developer Tools** → **Settings** → **Connections** → **Create connection**
5. Provider: **GitHub**
6. Connection name: `github-workshop`
7. Clique em **Connect to GitHub**
8. Na janela que abre, autorize o AWS Connector for GitHub e instale-o no seu usuário/organização
9. Clique em **Connect**
10. Aguarde o status ficar **Available** (verde)
11. **Copie o ARN da conexão** — você vai precisar ao criar a pipeline

---

### 1.2 Criar infraestrutura base via CloudFormation

O arquivo `cloudformation-infra.yml` provisiona **tudo de uma vez**:
- 2 repositórios ECR (`workshop-orders`, `workshop-inventory`)
- Fila SQS (`workshop-orders`)
- IAM Execution Role (ECS puxa imagem do ECR, escreve logs)
- IAM Task Role (app acessa SQS)
- Security Groups do ALB e das tasks ECS
- Application Load Balancer (internet-facing)
- Target Groups Blue e Green (porta 3001, health check em `/health`)
- Listeners: produção na porta 80, teste na porta 8080
- ECS Cluster (`workshop-cluster`, Fargate)

**Deploy via Console:**
1. Acesse **CloudFormation** → **Stacks** → **Create stack** → **With new resources**
2. Template source: **Upload a template file** → selecione `cloudformation-infra.yml`
3. Stack name: `workshop-infra`
4. Preencha os parâmetros:
   | Parâmetro | Valor |
   |-----------|-------|
   | `VpcId` | ID da sua VPC (ex: `vpc-xxxxxxxx`) |
   | `PublicSubnetIds` | 2 subnets **públicas** para o ALB |
   | `PrivateSubnetIds` | Subnets para as tasks ECS |
5. Clique em **Next** → **Next** → marque **"I acknowledge that AWS CloudFormation might create IAM resources with custom names"** → **Create stack**
6. Aguarde **CREATE_COMPLETE** (~2 minutos)

**Após o deploy, anote os Outputs:**
| Output | Onde usar |
|--------|-----------|
| `ALBDNSName` | URL do app para testes finais |
| `SQSQueueUrl` | `taskdef.json` → env var `SQS_QUEUE_URL` |
| `ECSExecutionRoleArn` | `taskdef.json` → campo `executionRoleArn` |
| `ECSTaskRoleArn` | `taskdef.json` → campo `taskRoleArn` |
| `ECSTaskSecurityGroupId` | ECS Service → Networking |
| `ECSClusterName` | ECS Service → Cluster |
| `ECSInfraRoleArn` | ECS Service → `advancedConfiguration.roleArn` |
| `ListenerRuleProdArn` | ECS Service → `advancedConfiguration.productionListenerRule` |
| `ListenerRuleTestArn` | ECS Service → `advancedConfiguration.testListenerRule` |
| `ECROrdersURI` | referência: `ACCOUNT_ID.dkr.ecr.REGION.amazonaws.com/workshop-orders` |

**Deploy via CLI (alternativa):**
```bash
aws cloudformation create-stack \
  --stack-name workshop-infra \
  --template-body file://cloudformation-infra.yml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameters \
    ParameterKey=VpcId,ParameterValue=vpc-XXXXXXXX \
    ParameterKey=PublicSubnetIds,ParameterValue="subnet-AAAA,subnet-BBBB" \
    ParameterKey=PrivateSubnetIds,ParameterValue="subnet-CCCC,subnet-DDDD"
```

---

### 1.3 Criar banco de dados RDS (CloudFormation)

O arquivo `cloudformation-db.yml` provisiona um RDS PostgreSQL `db.t3.micro` (free-tier eligible), um DB Subnet Group e um Security Group.

**Deploy via Console:**
1. Acesse **CloudFormation** → **Stacks** → **Create stack** → **With new resources**
2. Template source: **Upload a template file** → selecione `cloudformation-db.yml`
3. Stack name: `workshop-db`
4. Preencha os parâmetros:
   | Parâmetro | Valor |
   |-----------|-------|
   | `VpcId` | ID da sua VPC |
   | `SubnetIds` | Selecione 2 subnets em AZs diferentes |
   | `VpcCidr` | CIDR da sua VPC (default VPC: `172.31.0.0/16`) |
   | `DBPassword` | Senha forte de sua escolha (mín. 8 caracteres) |
5. Clique em **Next** → **Next** → **Create stack**
6. Aguarde o status ficar **CREATE_COMPLETE** (~5 minutos)

**Após o deploy, anote os Outputs:**
- `DBEndpoint` — hostname do RDS
- `DatabaseURL` — connection string completa (substitua `PASSWORD` pela senha escolhida)

**Deploy via CLI (alternativa):**
```bash
aws cloudformation create-stack \
  --stack-name workshop-db \
  --template-body file://cloudformation-db.yml \
  --parameters \
    ParameterKey=VpcId,ParameterValue=vpc-XXXXXXXX \
    ParameterKey=SubnetIds,ParameterValue="subnet-AAAA,subnet-BBBB" \
    ParameterKey=VpcCidr,ParameterValue=172.31.0.0/16 \
    ParameterKey=DBPassword,ParameterValue=SuaSenhaAqui
```

---

### 1.4 Criar CloudWatch Log Group

1. Acesse **CloudWatch** → **Log groups** → **Create log group**
2. Log group name: `/ecs/workshop-app`
3. Retention: **1 week** (economiza custo no workshop)
4. Clique em **Create**

---

### 1.5 Preencher o taskdef.json

Abra o arquivo `taskdef.json` e substitua os placeholders com os valores obtidos nos passos anteriores:

| Placeholder | Substituir por | Onde encontrar |
|-------------|----------------|----------------|
| `EXECUTION_ROLE_ARN` | ARN do Execution Role | Output `ECSExecutionRoleArn` do `workshop-infra` |
| `TASK_ROLE_ARN` | ARN do Task Role | Output `ECSTaskRoleArn` do `workshop-infra` |
| `DB_PASSWORD` | senha escolhida no passo 1.3 | você mesmo definiu |
| `RDS_ENDPOINT` | hostname do RDS | Output `DBEndpoint` do `workshop-db` |
| `SQS_QUEUE_URL` | URL da fila SQS | Output `SQSQueueUrl` do `workshop-infra` |

Após editar, faça push:
```bash
git add taskdef.json
git commit -m "fill taskdef placeholders"
git push
```

---

## PARTE 2 — ECS (Task Definition + Service)

### 2.1 Criar Task Definition

1. Acesse **ECS** → **Task definitions** → **Create new task definition** → **Create new task definition with JSON**
2. Cole o conteúdo do arquivo `taskdef.json` do repositório (com os placeholders já preenchidos)
3. Clique em **Create**

> A task definition define: CPU/memória (256/512), roles IAM, variáveis de ambiente e configuração de log para o CloudWatch.

---

### 2.2 Criar ECS Service (Blue/Green nativo)

> O ECS suporta Blue/Green nativamente desde 2024 — sem CodeDeploy. O serviço gerencia o traffic shifting diretamente via ALB.

**A. Coletar os ARNs dos Outputs do CloudFormation:**
```bash
aws cloudformation describe-stacks --stack-name workshop-infra \
  --query "Stacks[0].Outputs" --output table
```

**B. Criar o arquivo `service.json`** com os valores dos Outputs (substitua os placeholders):
```json
{
  "cluster": "workshop-cluster",
  "serviceName": "workshop-orders-service",
  "taskDefinition": "workshop-orders",
  "desiredCount": 2,
  "launchType": "FARGATE",
  "networkConfiguration": {
    "awsvpcConfiguration": {
      "subnets": ["subnet-AAAA", "subnet-BBBB"],
      "securityGroups": ["ECSTaskSecurityGroupId"],
      "assignPublicIp": "ENABLED"
    }
  },
  "loadBalancers": [
    {
      "targetGroupArn": "TG_BLUE_ARN",
      "containerName": "orders-service",
      "containerPort": 3001,
      "advancedConfiguration": {
        "alternateTargetGroupArn": "TG_GREEN_ARN",
        "productionListenerRule": "ListenerRuleProdArn",
        "testListenerRule": "ListenerRuleTestArn",
        "roleArn": "ECSInfraRoleArn"
      }
    }
  ],
  "deploymentConfiguration": {
    "strategy": "BLUE_GREEN",
    "bakeTimeInMinutes": 5
  }
}
```

> Substitua cada valor pelo Output correspondente do stack `workshop-infra`. Para os Target Group ARNs:
> ```bash
> aws elbv2 describe-target-groups --names workshop-tg-blue workshop-tg-green \
>   --query "TargetGroups[*].[TargetGroupName,TargetGroupArn]" --output table
> ```

**C. Criar o serviço:**
```bash
aws ecs create-service --cli-input-json file://service.json
```

> O service vai falhar no primeiro deploy porque a imagem `<IMAGE1_NAME>` não é válida — isso é esperado. A pipeline vai corrigir isso na primeira execução.

---

## PARTE 3 — CodeBuild

### 3.1 Criar projeto CodeBuild

1. Acesse **CodeBuild** → **Build projects** → **Create build project**
2. Project name: `workshop-orders-build`

**Source:**
3. Source provider: **GitHub (via GitHub App)**
4. Connection: selecione `github-workshop`
5. Repository: `SEU_USUARIO/workshop-app`
6. Branch: `main`

**Environment:**
7. Environment image: **Managed image**
8. Operating system: **Amazon Linux**
9. Runtime: **Standard**
10. Image: **aws/codebuild/amazonlinux-x86_64-standard:5.0** (ou a mais recente)
11. Image version: **Always use the latest**
12. Service role: **New service role** (anote o nome criado)
13. Marque **Enable this flag if you want to build Docker images** (Privileged)

**Buildspec:**
14. Use a buildspec file (já está no repositório como `buildspec.yml`)

**Artifacts:**
15. Type: **No artifacts** (o CodePipeline gerencia os artefatos)

**Environment variables** (clique em "Additional configuration"):
16. Adicione as seguintes variáveis:
    | Name | Value | Descrição |
    |------|-------|-----------|
    | `AWS_ACCOUNT_ID` | `123456789012` | seu AWS Account ID |
    | `AWS_DEFAULT_REGION` | `us-east-1` | sua região |
    | `ECR_REPO_NAME` | `workshop-orders` | nome do repositório ECR |

17. Clique em **Create build project**

---

### 3.2 Permissão do CodeBuild para o ECR

O role do CodeBuild precisa de permissão para fazer push no ECR.

1. Acesse **IAM** → **Roles** → busque pelo role criado (ex: `codebuild-workshop-orders-build-service-role`)
2. Clique em **Add permissions** → **Attach policies**
3. Busque e adicione: **AmazonEC2ContainerRegistryPowerUser**
4. Clique em **Add permissions**

---

## PARTE 4 — CodePipeline

### 4.1 Criar a pipeline

1. Acesse **CodePipeline** → **Pipelines** → **Create pipeline**
2. Pipeline name: `workshop-orders-pipeline`
3. Pipeline type: **V2** ← importante para usar filtros de path
4. Service role: **New service role**
5. Clique em **Next**

**Stage: Source**
6. Source provider: **GitHub (via GitHub App)**
7. Connection: selecione `github-workshop`
8. Repository: `SEU_USUARIO/workshop-app`
9. Branch: `main`
10. Output artifact format: **CodePipeline default**
11. Clique em **Next**

**Stage: Build**
12. Build provider: **AWS CodeBuild**
13. Region: sua região
14. Project name: `workshop-orders-build`
15. Clique em **Next**

**Stage: Deploy**
16. Deploy provider: **Amazon ECS**
17. Cluster name: `workshop-cluster`
18. Service name: `workshop-orders-service`
19. Image definitions file: `imagedefinitions.json`
20. Clique em **Next** → **Create pipeline**

---

### 4.2 Configurar filtro de path (CodePipeline V2)

Por padrão, qualquer push no repositório dispara a pipeline. Para que ela dispare **apenas quando arquivos dentro de `orders-service/` forem modificados**, configure um filtro de path:

1. Na pipeline criada, clique em **Edit**
2. No stage **Source**, clique em **Edit stage** → **Edit** no action de Source
3. Role até a seção **Trigger** (ou **Filter**):
   - Em **Event type**: selecione **Push**
   - Em **File paths** → **Include**: adicione o padrão `orders-service/**`
4. Clique em **Done** → **Save**

> **Por que isso importa?** Com 2 serviços no mesmo repositório, você não quer que um push na pasta `inventory-service/` dispare a pipeline do `orders-service`. O filtro de path resolve isso elegantemente — cada serviço tem sua própria pipeline com seu próprio filtro.

---

## PARTE 5 — Testar

### 5.1 Primeiro deploy automático

Ao criar a pipeline, ela já vai rodar automaticamente com o código atual do GitHub.

Acompanhe em **CodePipeline** → `workshop-orders-pipeline`:
- **Source**: ✅ puxou o código
- **Build**: ✅ buildou a imagem Python e enviou ao ECR
- **Deploy**: ✅ ECS iniciou o Blue/Green nativo

Ao finalizar, acesse o DNS do ALB (Output `ALBDNSName` do `workshop-infra`):
```
http://workshop-alb-XXXXXXX.REGION.elb.amazonaws.com
```

Resposta esperada em `GET /`:
```json
{
  "service": "orders-service",
  "version": "1.0.0",
  "host": "ip-10-0-x-x",
  "endpoints": ["POST /orders", "GET /orders", "GET /orders/{id}", "GET /health"]
}
```

---

### 5.2 Testar o fluxo completo via API

**Criar um pedido** (persiste no PostgreSQL e publica na fila SQS):
```bash
curl -X POST http://workshop-alb-1713889946.us-east-1.elb.amazonaws.com/orders \
  -H "Content-Type: application/json" \
  -d '{"product":"notebook","quantity":2,"customer":"joao@email.com"}'
```

Resposta esperada:
```json
{
  "id": 1,
  "product": "notebook",
  "quantity": 2,
  "customer": "joao@email.com",
  "status": "pending",
  "created_at": "2024-01-15T10:30:00"
}
```

**Ver pedidos criados:**
```bash
curl http://SEU_ALB/orders
```

**Ver um pedido específico:**
```bash
curl http://SEU_ALB/orders/1
```

**Documentação interativa (Swagger UI):**
```
http://SEU_ALB/docs
```

> O `inventory-service` consome a fila SQS automaticamente. Para vê-lo em ação, ele precisa ter seu próprio ECS Service rodando — o que é exatamente o **desafio** da próxima parte!

---

### 5.3 Sentir o Blue/Green na prática

**Passo 1 — Abra um terminal e inicie o polling do ALB:**
```bash
while true; do
  RESP=$(curl -s http://SEU_ALB/)
  VERSION=$(echo $RESP | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['version'])")
  HOST=$(echo $RESP | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['host'])")
  echo "$(date '+%H:%M:%S')  version=$VERSION  host=$HOST"
  sleep 2
done
```

Você verá linhas como:
```
14:32:01  version=1.0.0  host=ip-10-0-1-42
14:32:03  version=1.0.0  host=ip-10-0-2-17
14:32:05  version=1.0.0  host=ip-10-0-1-42
```

**Passo 2 — Em outro terminal, faça a mudança e o push:**

Edite `orders-service/main.py` e mude o valor default da versão:
```python
# linha 76 — mude de "1.0.0" para "2.0.0"
"version": os.environ.get("APP_VERSION", "2.0.0"),
```

```bash
git add orders-service/main.py
git commit -m "bump version to 2.0.0"
git push
```

**Passo 3 — Observe o que acontece no terminal de polling:**

```
14:35:10  version=1.0.0  host=ip-10-0-1-42   ← blue ainda ativo
14:35:12  version=1.0.0  host=ip-10-0-2-17
  ... (pipeline rodando: ~2min build + deploy)
14:37:44  version=2.0.0  host=ip-10-0-3-88   ← traffic shift: tudo vai pro green
14:37:46  version=2.0.0  host=ip-10-0-4-11
14:37:48  version=2.0.0  host=ip-10-0-3-88
  ... (bake time: 5 minutos com 100% no green)
14:42:51  version=2.0.0  host=ip-10-0-3-88   ← tasks blue encerradas
```

O que está acontecendo por baixo:
- **Build (~2min)**: CodeBuild builda a imagem `2.0.0` e sobe no ECR
- **Green tasks sobem**: ECS cria novas tasks com a imagem nova no Target Group Green
- **Traffic shift**: no momento que as tasks green passam no health check, o ALB vira 100% para o Green (corte instantâneo — você vê no polling)
- **Bake time (5min)**: ambas as versões estão rodando, mas só o green recebe tráfego
- **Cleanup**: tasks blue são encerradas automaticamente

> Para fazer rollback manual durante o bake time:
> ```bash
> # Acesse ECS → Clusters → workshop-cluster → Services → workshop-orders-service
> # Clique em "Deployments" → botão "Rollback"
> ```

---

## PARTE 6 — DESAFIO: Pipeline do inventory-service

> Esta parte não tem passo a passo guiado. O objetivo é você aplicar o que aprendeu para montar a pipeline completa do `inventory-service`.

### O que o inventory-service faz

O `inventory-service` é um **consumer SQS**. Ele:
1. Sobe com um background thread que fica em long polling na fila `workshop-orders`
2. Para cada mensagem recebida (um pedido), reserva o estoque no PostgreSQL
3. Deleta a mensagem da fila após processar com sucesso

Ele **não expõe endpoints de negócio** — apenas `/health` e `/` para diagnóstico. O trabalho real acontece na thread de consumo.

### Sua missão

Monte a pipeline completa para o `inventory-service` seguindo o mesmo padrão do `orders-service`. Você vai precisar:

**Infraestrutura:**
- [ ] Criar uma nova Task Definition (`workshop-inventory`) com a imagem do ECR `workshop-inventory`
  - Mesmos roles IAM (Execution Role e Task Role já existem no `workshop-infra`)
  - Mesmas variáveis de ambiente: `DATABASE_URL`, `SQS_QUEUE_URL`, `AWS_DEFAULT_REGION`
  - Porta do container: `3002`
- [ ] Criar um novo ECS Service para o `inventory-service`
  - Você precisará de um novo par de Target Groups ou usar outro ALB listener
  - Alternativamente: o inventory-service não precisa de ALB (não recebe tráfego HTTP externo) — pode ser criado sem load balancer

**Pipeline:**
- [ ] Criar um projeto CodeBuild (`workshop-inventory-build`) com a variável `ECR_REPO_NAME=workshop-inventory`
  - Dica: o `buildspec.yml` usa `ECR_REPO_NAME` — você precisa de um buildspec que aponte para `inventory-service/Dockerfile`
- [ ] Criar uma pipeline CodePipeline V2 (`workshop-inventory-pipeline`)
- [ ] Configurar filtro de path: `inventory-service/**`

### Dicas

> **Dica 1 — buildspec:** O `buildspec.yml` atual builda o `orders-service`. Você vai precisar de um `buildspec-inventory.yml` (ou uma variável de ambiente que parametrize o nome da pasta). Olhe como o comando `docker build` referencia o Dockerfile.

> **Dica 2 — Task Definition sem ALB:** Se optar por não colocar o inventory-service atrás de um ALB, crie o ECS Service sem load balancer. O service vai rodar normalmente — ele só não vai receber tráfego HTTP externo, mas o consumer SQS interno funciona independentemente.

> **Dica 3 — Blue/Green vs Rolling:** Para usar Blue/Green nativo, o ECS Service precisa de Target Groups, Listeners e Listener Rules (igual ao orders-service). Se optar por não usar ALB no inventory-service, use **Rolling update** como deployment strategy (mais simples). A desvantagem é não ter zero-downtime garantido.

> **Dica 4 — imagedefinitions.json:** O buildspec do inventory-service precisa gerar o `imagedefinitions.json` com `"name":"inventory-service"` (não `orders-service`). Verifique o `printf` no `post_build` do buildspec.

### Verificando o sucesso

Após o deploy, você pode verificar que o inventory-service está consumindo a fila:

1. Crie um pedido no orders-service (como no passo 5.2)
2. Acesse **SQS** → `workshop-orders` → **Send and receive messages** → **Poll for messages**
   - Se o inventory-service estiver rodando, as mensagens devem ser consumidas rapidamente
3. Verifique os logs no CloudWatch: `/ecs/workshop-app` → stream `inventory/*`
   - Você deve ver linhas como `Processing order: {"id": 1, "product": "notebook", ...}`

---

## Resumo dos arquivos e o que cada um faz

| Arquivo | Usado por | Função |
|---------|-----------|--------|
| `orders-service/main.py` | CodeBuild/ECS | API REST: recebe pedidos, persiste no PostgreSQL, publica no SQS |
| `inventory-service/main.py` | ECS | Consumer SQS: faz long polling, reserva estoque no PostgreSQL |
| `cloudformation-db.yml` | CloudFormation | Provisiona RDS PostgreSQL + Security Group |
| `cloudformation-infra.yml` | CloudFormation | ECR + SQS + IAM Roles + ALB + Target Groups + ECS Cluster |
| `buildspec.yml` | CodeBuild | Login ECR, docker build do `orders-service`, docker push, gera `imagedefinitions.json` |
| `taskdef.json` | CodePipeline/ECS | Template da Task Definition. `<IMAGE1_NAME>` é substituído automaticamente pela URI da imagem nova |
| `docker-compose.yml` | Local | Sobe Postgres + LocalStack (SQS) + 2 serviços para desenvolvimento local |

---

## Desenvolvimento local

Para testar localmente antes de subir para a AWS, use o Docker Compose:

```bash
docker compose up --build
```

O ambiente local usa **LocalStack** para emular o SQS (não precisa de credenciais AWS reais).

Serviços disponíveis após o `up`:
- `orders-service`: http://localhost:3001
- `inventory-service`: http://localhost:3002
- PostgreSQL: localhost:5432
- LocalStack (SQS): localhost:4566

Testando localmente:
```bash
# Criar pedido
curl -X POST http://localhost:3001/orders \
  -H "Content-Type: application/json" \
  -d '{"product":"teclado","quantity":1,"customer":"maria@email.com"}'

# Ver pedidos
curl http://localhost:3001/orders

# Swagger UI
open http://localhost:3001/docs
```

---

## Troubleshooting comum

**Build falha com "denied: Your authorization token has expired"**
→ O role do CodeBuild não tem permissão no ECR. Adicione `AmazonEC2ContainerRegistryPowerUser` ao role.

**Deploy falha com "The ECS service cannot be updated"**
→ Verifique se o ECS Service foi criado com `deploymentConfiguration.strategy = BLUE_GREEN` e se os ARNs do `advancedConfiguration` estão corretos.

**Deploy falha com "unable to update the service because the blue/green deployment is not configured"**
→ O service foi criado sem Blue/Green (Rolling update). Delete e recrie usando o `service.json` do passo 2.2.

**Task não sobe (health check failing)**
→ Verifique se o Security Group das tasks (`ECSTaskSecurityGroupId`) permite TCP 3001 vindo do SG do ALB. O health check usa `GET /health`.

**Task para com "Startup failed" — erro de banco**
→ O `DATABASE_URL` no `taskdef.json` está incorreto ou o Security Group do RDS não permite conexão das tasks. Confirme que o CIDR da VPC está liberado na porta 5432 no SG criado pelo `cloudformation-db.yml`.

**CloudFormation falha com "InvalidSubnet"**
→ As subnets fornecidas precisam estar em AZs diferentes. Selecione subnets de pelo menos 2 AZs distintas.

**CloudFormation falha com "InsufficientCapabilitiesException"**
→ Marque a caixa **"I acknowledge that AWS CloudFormation might create IAM resources"** (ou use `--capabilities CAPABILITY_NAMED_IAM` na CLI).

**Pipeline não dispara no push**
→ Verifique se a conexão GitHub está com status **Available** (CodeConnections → Connections). Se estiver **Pending**, finalize a autorização OAuth no GitHub.

**Pipeline dispara mas não deveria (push em outro serviço)**
→ Verifique o filtro de path configurado na Source action. O padrão deve ser `orders-service/**` para a pipeline do orders-service.

**inventory-service não consome mensagens**
→ Verifique se a variável `SQS_QUEUE_URL` está correta no taskdef do inventory-service. Veja os logs no CloudWatch — se aparecer "SQS_QUEUE_URL not set", a variável está faltando na Task Definition.
