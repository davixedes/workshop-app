# Workshop: Pipeline CI/CD com CodeCommit + CodeBuild + CodeDeploy + ECS (Fargate)

## O que vamos construir

```
[CodeCommit] → [CodePipeline] → [CodeBuild] → [CodeDeploy] → [ECS Fargate]
   (fonte)                       (build +        (Blue/Green    (app rodando)
                                  push ECR)       no cluster)
```

O app é um servidor Node.js simples. Ao fazer push no CodeCommit, a pipeline:
1. Detecta a mudança automaticamente
2. Builda a imagem Docker e sobe no ECR
3. Faz deploy Blue/Green no ECS via CodeDeploy

---

## Pré-requisitos

- Conta AWS com permissão de Admin (ou permissões para IAM, ECS, ECR, CodeBuild, CodeDeploy, CodePipeline, ALB)
- Uma VPC com pelo menos 2 subnets públicas e 2 privadas (a default VPC funciona)
- Git instalado localmente
- Credencial Git para CodeCommit configurada (HTTPS via AWS CLI ou SSH)

---

## Estrutura dos arquivos do repositório

```
ecs-pipeline-app/
├── app/
│   ├── app.js          # servidor Express
│   └── package.json
├── Dockerfile
├── buildspec.yml       # instruções para o CodeBuild
├── taskdef.json        # template da Task Definition do ECS
└── appspec.yaml        # instruções para o CodeDeploy
```

---

## PARTE 1 — Infraestrutura base

### 1.1 Criar repositório ECR

1. Acesse **Amazon ECR** → **Repositories** → **Create repository**
2. Visibility: **Private**
3. Repository name: `workshop-app`
4. Clique em **Create repository**
5. Anote a URI do repositório: `ACCOUNT_ID.dkr.ecr.REGION.amazonaws.com/workshop-app`

---

### 1.2 Criar repositório CodeCommit

1. Acesse **CodeCommit** → **Repositories** → **Create repository**
2. Repository name: `workshop-app`
3. Clique em **Create**
4. Siga as instruções para configurar credenciais HTTPS (IAM → Security credentials → HTTPS Git credentials for CodeCommit)
5. Clone o repositório localmente:
   ```bash
   git clone https://git-codecommit.REGION.amazonaws.com/v1/repos/workshop-app
   ```
6. Copie todos os arquivos do `ecs-pipeline-app/` para dentro do repositório clonado
7. Edite o `taskdef.json` e substitua `ACCOUNT_ID` pelo ID da sua conta AWS e a região correta
8. Faça o primeiro push:
   ```bash
   git add .
   git commit -m "initial commit"
   git push
   ```

---

### 1.3 Criar CloudWatch Log Group

1. Acesse **CloudWatch** → **Log groups** → **Create log group**
2. Log group name: `/ecs/workshop-app`
3. Retention: **1 week** (economiza custo no workshop)
4. Clique em **Create**

---

## PARTE 2 — ECS (Cluster + ALB + Service)

### 2.1 Criar ECS Cluster

1. Acesse **Amazon ECS** → **Clusters** → **Create Cluster**
2. Cluster name: `workshop-cluster`
3. Infrastructure: marque **AWS Fargate (serverless)**
4. Clique em **Create**

---

### 2.2 Criar Application Load Balancer

> O ALB precisa de dois listeners (produção e teste) para o Blue/Green funcionar.

1. Acesse **EC2** → **Load Balancers** → **Create load balancer**
2. Selecione **Application Load Balancer**
3. Name: `workshop-alb`
4. Scheme: **Internet-facing**
5. IP address type: **IPv4**
6. VPC: selecione sua VPC
7. Mappings: selecione pelo menos 2 subnets **públicas**
8. Security groups: crie um novo SG ou selecione existente com:
   - Inbound: TCP 80 de 0.0.0.0/0
   - Inbound: TCP 8080 de 0.0.0.0/0

**Criar Target Group Blue:**
9. Em "Listeners and routing" → clique em **Create target group**
   - Target type: **IP addresses**
   - Name: `workshop-tg-blue`
   - Protocol: HTTP, Port: 3000
   - VPC: sua VPC
   - Health check path: `/health`
   - Clique em **Create target group**

10. De volta no ALB, Listener 1: Port **80** → selecione `workshop-tg-blue`

**Criar Target Group Green:**
11. Clique em **Add listener**
    - Port: **8080**
    - Crie um segundo target group:
      - Name: `workshop-tg-green`
      - Target type: IP addresses, Port: 3000
      - Health check: `/health`
    - Listener 8080 → selecione `workshop-tg-green`

12. Clique em **Create load balancer**

---

### 2.3 Criar Task Definition

1. Acesse **ECS** → **Task definitions** → **Create new task definition** → **Create new task definition with JSON**
2. Cole o conteúdo do arquivo `taskdef.json` do repositório
   > Lembre-se: `ACCOUNT_ID` já deve estar substituído pelo ID real
3. Clique em **Create**

---

### 2.4 Criar ECS Service

> O Service precisa usar **CodeDeploy** como deployment controller para o Blue/Green funcionar.

1. Acesse **ECS** → **Clusters** → `workshop-cluster` → **Create** (em Services)
2. **Compute options**: Launch type → **FARGATE**
3. **Task definition**: `workshop-app` (a que acabou de criar)
4. **Service name**: `workshop-service`
5. **Desired tasks**: `2`

6. Em **Deployment options**:
   - Deployment type: **Blue/green deployment (powered by AWS CodeDeploy)**
   - Service role for CodeDeploy: se não tiver, AWS vai criar automaticamente (`AWSCodeDeployRoleForECS`)

7. Em **Networking**:
   - VPC: sua VPC
   - Subnets: selecione as subnets **privadas** (ou públicas se não tiver NAT)
   - Security group: crie ou selecione SG com inbound TCP 3000 vindo do SG do ALB

8. Em **Load balancing**:
   - Load balancer type: **Application Load Balancer**
   - Load balancer: `workshop-alb`
   - Container: `workshop-app 3000`
   - Production listener: **80**
   - Test listener: **8080**
   - Target group 1 (Blue): `workshop-tg-blue`
   - Target group 2 (Green): `workshop-tg-green`

9. Clique em **Create**

> O service vai falhar no primeiro deploy porque a imagem `<IMAGE1_NAME>` não é válida — isso é esperado. A pipeline vai corrigir isso.

---

## PARTE 3 — CodeBuild

### 3.1 Criar projeto CodeBuild

1. Acesse **CodeBuild** → **Build projects** → **Create build project**
2. Project name: `workshop-app-build`

**Source:**
3. Source provider: **AWS CodeCommit**
4. Repository: `workshop-app`
5. Branch: `main`

**Environment:**
6. Environment image: **Managed image**
7. Operating system: **Amazon Linux**
8. Runtime: **Standard**
9. Image: **aws/codebuild/amazonlinux-x86_64-standard:5.0** (ou a mais recente)
10. Image version: **Always use the latest**
11. Service role: **New service role** (anote o nome criado)
12. Marque **Enable this flag if you want to build Docker images** (Privileged)

**Buildspec:**
13. Use a buildspec file (já está no repositório como `buildspec.yml`)

**Artifacts:**
14. Type: **No artifacts** (o CodePipeline gerencia os artefatos)

**Environment variables** (clique em "Additional configuration"):
15. Adicione as seguintes variáveis:
    | Name | Value |
    |------|-------|
    | `AWS_ACCOUNT_ID` | `123456789012` (seu account ID) |
    | `AWS_DEFAULT_REGION` | `us-east-1` (sua região) |
    | `ECR_REPO_NAME` | `workshop-app` |
    | `CONTAINER_NAME` | `workshop-app` |

16. Clique em **Create build project**

---

### 3.2 Permissão do CodeBuild para o ECR

O role do CodeBuild precisa de permissão para fazer push no ECR.

1. Acesse **IAM** → **Roles** → busque pelo role criado (ex: `codebuild-workshop-app-build-service-role`)
2. Clique em **Add permissions** → **Attach policies**
3. Busque e adicione: **AmazonEC2ContainerRegistryPowerUser**
4. Clique em **Add permissions**

---

## PARTE 4 — CodeDeploy

> O CodeDeploy para ECS foi criado automaticamente quando criamos o ECS Service com Blue/Green. Vamos apenas verificar.

1. Acesse **CodeDeploy** → **Applications**
2. Verifique se existe uma application com nome similar a `AppECS-workshop-cluster-workshop-service`
3. Clique nela → veja o **Deployment group** criado
4. Anote o nome exato da **Application** e do **Deployment group** (você vai precisar na pipeline)

---

## PARTE 5 — CodePipeline

### 5.1 Criar a pipeline

1. Acesse **CodePipeline** → **Pipelines** → **Create pipeline**
2. Pipeline name: `workshop-pipeline`
3. Pipeline type: **V2**
4. Service role: **New service role**
5. Clique em **Next**

**Stage: Source**
6. Source provider: **AWS CodeCommit**
7. Repository: `workshop-app`
8. Branch: `main`
9. Detection option: **Amazon CloudWatch Events** (trigger automático no push)
10. Clique em **Next**

**Stage: Build**
11. Build provider: **AWS CodeBuild**
12. Region: sua região
13. Project name: `workshop-app-build`
14. Clique em **Next**

**Stage: Deploy**
15. Deploy provider: **Amazon ECS (Blue/Green)**
16. Application name: (selecione o que apareceu no CodeDeploy, ex: `AppECS-workshop-cluster-workshop-service`)
17. Deployment group: (selecione o deployment group correspondente)
18. Task definition:
    - Artifact: **BuildArtifact**
    - File name: `taskdef.json`
19. AWS CodeDeploy AppSpec file:
    - Artifact: **BuildArtifact**
    - File name: `appspec.yaml`
20. Dynamically update task definition image:
    - Input artifact with image details: **BuildArtifact**
    - Placeholder text in the task definition: `IMAGE1_NAME`
21. Clique em **Next** → **Create pipeline**

---

## PARTE 6 — Testar

### 6.1 Primeiro deploy automático

Ao criar a pipeline, ela já vai rodar automaticamente com o código atual do CodeCommit.

Acompanhe em **CodePipeline** → `workshop-pipeline`:
- **Source**: ✅ puxou o código
- **Build**: ✅ buildou a imagem e enviou ao ECR
- **Deploy**: ✅ CodeDeploy iniciou o Blue/Green

Ao finalizar, acesse o DNS do ALB no browser:
```
http://workshop-alb-XXXXXXX.REGION.elb.amazonaws.com
```

Resposta esperada:
```json
{ "message": "Hello from ECS!", "version": "1.0.0", "host": "ip-10-0-x-x" }
```

---

### 6.2 Testar um novo deploy (mudança de código)

1. Edite `app/app.js` e mude a mensagem ou version:
   ```js
   version: process.env.APP_VERSION || '2.0.0',
   ```
2. Faça push:
   ```bash
   git add app/app.js
   git commit -m "bump version to 2.0.0"
   git push
   ```
3. A pipeline vai disparar automaticamente
4. Observe no CodeDeploy o deploy Blue/Green:
   - Tasks novas sobem no Target Group Green
   - Tráfego vira para o Green
   - Tasks antigas (Blue) são encerradas após 5 minutos
5. Acesse o ALB novamente e veja a versão atualizada

---

## Resumo dos arquivos e o que cada um faz

| Arquivo | Usado por | Função |
|---------|-----------|--------|
| `buildspec.yml` | CodeBuild | Define os comandos de build: login ECR, docker build, docker push, gera `imageDetail.json` |
| `taskdef.json` | CodePipeline/CodeDeploy | Template da Task Definition. `<IMAGE1_NAME>` é substituído automaticamente pela URI da imagem nova |
| `appspec.yaml` | CodeDeploy | Diz ao CodeDeploy qual container/porta usar. `<TASK_DEFINITION>` é substituído pela nova Task Definition |
| `imageDetail.json` | CodePipeline | Gerado pelo CodeBuild. Contém a URI da imagem buildada. CodePipeline usa para preencher `<IMAGE1_NAME>` no taskdef |

---

## Troubleshooting comum

**Build falha com "denied: Your authorization token has expired"**
→ O role do CodeBuild não tem permissão no ECR. Adicione `AmazonEC2ContainerRegistryPowerUser` ao role.

**Deploy falha com "The ECS service cannot be updated"**
→ Verifique se o ECS Service foi criado com Deployment controller = CODE_DEPLOY.

**Task não sobe (health check failing)**
→ Verifique se o Security Group das tasks permite TCP 3000 vindo do SG do ALB.

**Pipeline não dispara no push**
→ Verifique se o CloudWatch Events Rule foi criado (CodePipeline cria automaticamente; pode ter faltado permissão).
