# Arquitetura — Workshop E-commerce

## Fluxo de dados

```mermaid
sequenceDiagram
    participant Client
    participant OS as orders-service
    participant RMQ as RabbitMQ
    participant PG as PostgreSQL

    Client->>OS: POST /orders
    OS->>PG: INSERT order
    OS->>RMQ: fanout "orders"
    OS-->>Client: 201 Created

    RMQ->>PG: inventory-service atualiza stock
    RMQ->>PG: notification-service insere notificação
```

---

## Infraestrutura (AWS)

```mermaid
graph TD
    Internet --> ALB[ALB]
    ALB -->|Blue/Green| OS[ECS orders-service]
    OS --> RDS[(RDS PostgreSQL)]
    OS --> MQ[RabbitMQ]
    MQ --> IS[ECS inventory-service]
    MQ --> NS[ECS notification-service]
    IS & NS --> RDS

    GH([GitHub]) --> CP[CodePipeline] --> CB[CodeBuild]
    CB --> ECR[(ECR)] -.-> OS
    CB --> CD[CodeDeploy] --> OS
```

---

## Tabelas

```mermaid
erDiagram
    orders ||--o{ reservations : gera
    orders ||--o{ notifications : gera
    stock ||--o{ reservations : reserva

    orders { varchar id PK; varchar product; int quantity; varchar customer; varchar status }
    stock { varchar product PK; int quantity }
    reservations { serial id PK; varchar order_id; varchar product; int quantity; varchar status }
    notifications { varchar id PK; varchar type; varchar order_id; varchar to_email }
```
