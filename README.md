# SA Retail Inventory Pipeline

A Kubernetes based inventory sync pipeline simulating real time stock 
management across South African retail branches. Built to address the 
real problem of disconnected inventory systems that leave township stores 
empty of essential goods like maize meal, cooking oil and bread.

## The Problem
When a Shoprite in Soweto runs out of maize meal the system doesn't 
alert anyone automatically. A manager notices 2 days later. By then 
200 families either went without or paid inflated prices at a spaza shop.
This project is the infrastructure layer that prevents that.

## Architecture
FastAPI → PostgreSQL (stock data) → Redis (cache)
     ↑
Kubernetes (orchestration + autoscaling + rollback)
     ↑
GitHub Actions (CI/CD: test → build → deploy)
     ↑
Prometheus + Grafana (observability)

## Environments
Dev:     Docker Compose locally — port 8013
Prod:    Kubernetes namespace sa-retail — port 30080

## Stack
- Python / FastAPI     — inventory REST API
- PostgreSQL           — persistent stock level storage
- Redis                — inventory caching layer
- Docker               — containerisation
- Kubernetes k3s       — orchestration autoscaling rollback
- Ansible              — server automation
- GitHub Actions       — CI/CD pipeline
- Prometheus + Grafana — live observability dashboards

## Run locally
cp .env.example .env
docker compose up --build
curl http://localhost:8013/health

## Deploy to Kubernetes
kubectl apply -f k8s/
kubectl get pods -n sa-retail

## Key Endpoints
GET  /health              health check
GET  /inventory           all branches stock levels
GET  /inventory/{branch}  specific branch stock
PUT  /inventory/update    update stock level
PUT  /inventory/decrease  decrease stock triggers alert if low
GET  /low-stock           all products below threshold
GET  /alerts              reorder alert history
