## 🚀 PlumoAI - We are in Beta. If you find any issues during setup then Join our Discord Live Support call on https://discord.gg/WarY2yWZkg 
<img width="1024" height="388" alt="PlumoAIlogo" src="https://github.com/user-attachments/assets/43d692bd-912c-44eb-b13b-0a2ed0a1ce6e" />

## 🌍 World's First 100% FREE Autonomous AI Employee Platform OR you can say AI Employees OS (Operating System)

![GitHub stars](https://img.shields.io/github/stars/PlumoAI/plumoai?style=social)
![License](https://img.shields.io/badge/license-PlumoAI-blue)
![Docker](https://img.shields.io/badge/docker-supported-blue)
![Self Hosted](https://img.shields.io/badge/self--hosted-free-green)
![AI Employees](https://img.shields.io/badge/AI%20Employees-autonomous-purple)
![Security](https://img.shields.io/badge/security-report%20issues%20responsibly-green)

---

## 🤖 Hire AI Employees In Minutes. Works like human

PlumoAI is the **world's first platform designed to run Autonomous AI Employees**.

Companies do not hire people for their biology.
They hire **employees to own work and deliver outcomes**.

PlumoAI introduces a new system where **AI Employees operate as real organizational units of execution**.

With PlumoAI you can:

✨ Create unlimited AI Employees
✨ Run them on your own infrastructure
✨ Assign real responsibilities
✨ Connect tools using AI Agents
✨ Manage work using a built in project management system

---

## 🆓 Completely Free Self Hosted

PlumoAI can be deployed on your own infrastructure and used **completely free**.

✔ Individuals
✔ Startups
✔ Growing teams
✔ Enterprise companies

Everyone can run **unlimited AI Employees**.

No feature restrictions.

---

## 🤝 Current Sponsors
<img width="284" height="75" alt="image" src="https://github.com/user-attachments/assets/8f3c549d-9eca-4a4d-a0ef-f38153929464" />


### Approaching to onboard below 

<img width="284" height="178" alt="image" src="https://github.com/user-attachments/assets/e9c4963d-a6e5-49f1-ad9d-e2b3b3caf33e" />

<img width="187" height="48" alt="image" src="https://github.com/user-attachments/assets/05f40114-b2bc-413e-8f67-62aba024802e" />

<img width="225" height="225" alt="image" src="https://github.com/user-attachments/assets/234ec1a7-a750-475d-bdc7-56518b9cbc10" />

<img width="318" height="159" alt="image" src="https://github.com/user-attachments/assets/c9a6b8f1-b49a-4bad-b0e4-363692151124" />


and many more

---

## 🧠 Powered By OpenClaw

PlumoAI integrates **OpenClaw** (We have added this as an AI Agent) to provide advanced reasoning and autonomous workflow execution.

AI Employees can:

🧠 Plan tasks
⚙️ Generate workflows
🔄 Execute multi step operations
📊 Interact with business systems

This enables true **autonomous execution** across company operations.

---

# 🏢 The Six Component Principle of an Employee

Every real employee is defined by **six core components**.

PlumoAI implements this architecture for AI Employees.

---

## 🎯 Role

Clear responsibility and scope of work.

Examples:

Sales Executive
Operations Manager
Data Analyst
Project Manager
Software Developer

The role defines the **outcomes the employee must deliver**.

---

## 🧰 Tools

Software and systems the employee can operate.

Examples:

CRM systems
Analytics platforms
Communication tools
Project management systems
Databases

Tools are connected using **AI Agents**.

---

## 🔐 Authorization

Permissions that define what actions an employee can perform.

Examples:

Read company reports
Write CRM records
Update databases
Access internal APIs

Authorization ensures **controlled and secure operations**.

---

## 🧠 Memory

Persistent knowledge across time.

Examples:

Past conversations
Customer interactions
Business decisions
Operational context

Memory enables **continuous learning and improved outcomes**.

---

## 📊 Accountability

Ownership of performance and results.

Examples:

Response time
Task completion
Accuracy of work
Operational outcomes

AI Employees are accountable for delivering results.

---

## 👤 Presence

How the employee interacts with the organization.

Examples:

Chat
Email
Audio communication
Video interaction
Future holographic or robotic interfaces

Presence determines how employees **participate in daily operations**.

---

# 🧩 AI Agent Ecosystem

PlumoAI supports an extensible ecosystem of **AI Agents**.

AI Agents connect external systems to the platform using MCP servers or any specific agent.

Examples:

📧 Email platforms
📊 Analytics tools
💬 Messaging systems
🗄 Databases
📈 CRM platforms
or some of like
Scheduler
Workflow Builder
Memory
ChartMaker
etc.

A single AI Employee (Just like human) can use **multiple AI Agents simultaneously**.

### Build or update an AI Agent tool

AI Agent tools live in `ai-agents/` in this repo and are mounted into the `ai-service` container at runtime.

- Guide: [AI Agent Tool Creation Guide](docs/AI_AGENT_PLUGIN_CREATION_GUIDE.md)

---

# 🗂 Built In Project Management Workspace

PlumoAI includes a full **project management system** where humans and AI Employees collaborate.

You can:

✔ Create projects and tasks
✔ Assign work to AI Employees
✔ Monitor execution
✔ Manage teams and operations

This makes PlumoAI not just an AI platform but a **complete operational workspace**.

---

## Production Installation (Docker Compose)

This repo runs PlumoAI as a **multi-container stack** (UI + APIs + AI service + MySQL + MongoDB + Milvus) behind Traefik.
For production, use **domain mode** (HTTPS with Let's Encrypt). For local evaluation/dev, use **localhost mode**.

### System requirements

- **Minimum**
  - **CPU**: 2 vCPU
  - **RAM**: **16 GB** (required; the full stack needs enough headroom for MySQL, MongoDB, and services)
  - **Disk**: 30 GB free (SSD recommended)
- **Recommended (production)**
  - **CPU**: 4+ vCPU
  - **RAM**: 32+ GB
  - **Disk**: 100+ GB SSD (depends on file uploads + database size)
- **Network (production / domain mode)**
  - Public IP + domain DNS `A/AAAA` → server IP
  - Inbound ports **80** and **443** open (Let’s Encrypt + routing)

**This is a single-node reference deployment.** `docker-compose.yml` runs one MySQL container and one MongoDB container with `restart: unless-stopped` — that recovers a crashed container on the same host, but it is not high availability: a host failure takes the whole stack down, and there's no replication.

If you need HA past a single host, point the stack at externally managed databases instead of running `mysql`/`mongodb` yourself — the services already read their DB host from env vars, so this needs no compose changes:

```ini
DB_HOST=your-managed-mysql-host       # e.g. an RDS/Cloud SQL endpoint
MONGODB_HOST=your-managed-mongo-host  # e.g. an Atlas cluster
```

Then remove the `mysql`/`mongodb` services (and their `depends_on` entries) from your own `docker-compose.override.yml` if you don't want the local instances running alongside the managed ones.

### Windows (Production / Localhost)

#### Prerequisites

- Windows 10/11
- Docker Desktop with **Docker Compose v2**
- Git
- **Production (domain mode)**: domain `A/AAAA` record → your server IP, and open inbound **80/443**

#### Step 1: Clone

```powershell
git clone https://github.com/PlumoAI/plumoai.git
cd plumoai
```

#### Optional Step 2: Configure `.env` (recommended for production)

You can skip this step completely. **Step 3 will auto-create `.env`** from `.env.example` and (when run interactively) prompt you for required values.
For production or non-interactive installs, it’s recommended to create/edit `.env` yourself in advance.

**Domain mode (recommended for production):**

```ini
RUN_MODE=domain
DOMAIN_NAME=self.example.com
SSL_EMAIL=admin@example.com
```

**Localhost mode (HTTP):**

```ini
RUN_MODE=localhost
LOCALHOST_PORT=7861
```

#### Step 3: Install / Start (auto-creates `.env`, secrets, and starts containers)

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

#### Step 4: Open

- Domain mode: `https://<your-domain>`
- Localhost mode: `http://localhost:7861`

#### Optional: Fresh install (backs up, then resets MySQL volume)

```powershell
.\install.ps1 -Fresh
```

#### Verify / Logs

```powershell
docker compose ps
docker compose logs --tail 200 -f traefik
```

---

### Linux (Production / Localhost)

#### Prerequisites

- Docker Engine + **Docker Compose v2**
- Git
- **Production (domain mode)**: domain `A/AAAA` record → your server IP, and open inbound **80/443**

#### Step 1: Clone

```bash
git clone https://github.com/PlumoAI/plumoai.git
cd plumoai
```

#### Optional Step 2: Configure `.env` (recommended for production)

You can skip this step completely. **Step 3 will auto-create `.env`** from `.env.example` and (when run interactively) prompt you for required values.
For production or non-interactive installs, it’s recommended to create/edit `.env` yourself in advance.

**Domain mode (recommended for production):**

```ini
RUN_MODE=domain
DOMAIN_NAME=self.example.com
SSL_EMAIL=admin@example.com
```

**Localhost mode (HTTP):**

```ini
RUN_MODE=localhost
LOCALHOST_PORT=7861
```

#### Step 3: Install / Start (auto-creates `.env`, secrets, and starts containers)

```bash
chmod +x install.sh
./install.sh
```

#### Step 4: Open

- Domain mode: `https://<your-domain>`
- Localhost mode: `http://localhost:7861`

#### Optional: Fresh install (backs up, then resets MySQL volume)

```bash
./install.sh --fresh
```

#### Verify / Logs

```bash
docker compose ps
docker compose logs --tail 200 -f traefik
```

---

### macOS (Production / Localhost)

#### Prerequisites

- Docker Desktop with **Docker Compose v2**
- Git
- **Production (domain mode)**: domain `A/AAAA` record → your server IP, and open inbound **80/443**

#### Step 1: Clone

```bash
git clone https://github.com/PlumoAI/plumoai.git
cd plumoai
```

#### Optional Step 2: Configure `.env` (recommended for production)

You can skip this step completely. **Step 3 will auto-create `.env`** from `.env.example` and (when run interactively) prompt you for required values.
For production or non-interactive installs, it’s recommended to create/edit `.env` yourself in advance.

**Domain mode (recommended for production):**

```ini
RUN_MODE=domain
DOMAIN_NAME=self.example.com
SSL_EMAIL=admin@example.com
```

**Localhost mode (HTTP):**

```ini
RUN_MODE=localhost
LOCALHOST_PORT=7861
```

#### Step 3: Install / Start (auto-creates `.env`, secrets, and starts containers)

```bash
chmod +x install.sh
./install.sh
```

#### Step 4: Open

- Domain mode: `https://<your-domain>`
- Localhost mode: `http://localhost:7861`

#### Optional: Fresh install (backs up, then resets MySQL volume)

```bash
./install.sh --fresh
```

#### Verify / Logs

```bash
docker compose ps
docker compose logs --tail 200 -f traefik
```

### Common troubleshooting (all platforms)

- **DNS not pointing to the server** (domain mode): `DOMAIN_NAME` must resolve to your server’s public IP.
- **Ports blocked** (domain mode): open inbound **80/443** (cloud security group + OS firewall). Let’s Encrypt will fail if port 80 is blocked.
- **Non-interactive installs**: ensure `.env` is fully set (especially `DOMAIN_NAME` and `SSL_EMAIL` for domain mode).

### Optional: Update / change version (all platforms)

1) **Back up first** — `./scripts/backup.sh` (or `.\scripts\backup.ps1`). An upgrade that goes wrong is only recoverable if you took a backup beforehand.
2) Set `PLUMOAI_VERSION` (and/or `PLUMOAI_API_VERSION`) in `.env` (or keep the default).
3) Re-run the installer for your OS — it pulls the new images and restarts the containers.

Database migrations are **not** a separate step: `auth` and `plumoai-api` run their own pending migrations as part of their container's startup, before the healthcheck reports healthy. When you update the version and restart, the new container image starts, applies whatever migrations ship with that version against the existing `mysql`/`mongodb` data, and only then starts serving traffic. If a migration fails, the container will not become healthy — check `docker compose logs auth` / `docker compose logs plumoai-api`, and restore the pre-upgrade backup if you need to roll back:

```bash
./scripts/restore.sh backups/<pre-upgrade-timestamp>
```

**Pinning an exact image** instead of trusting a tag at pull time: create a `docker-compose.override.yml` (compose merges it automatically) with the digest from the `images:` output the installer prints after every run:

```yaml
services:
  auth:
    image: plumoai/authservice@sha256:<digest-from-installer-output>
```

### Backups

MySQL, MongoDB, and the Traefik TLS certificate store are not backed up automatically outside of `--fresh`/`-Fresh` (which backs up before it wipes the MySQL volume). Take a backup yourself before upgrades or any manual maintenance:

```bash
# Linux / macOS
./scripts/backup.sh                 # writes to backups/<timestamp>/
./scripts/restore.sh backups/<timestamp>
```

```powershell
# Windows
.\scripts\backup.ps1                # writes to backups/<timestamp>/
.\scripts\restore.ps1 -InDir backups/<timestamp>
```

Copy the `backups/` folder off the host regularly — a local backup on the same disk doesn't protect against disk failure.

### Optional: Monitoring

An opt-in overlay (`docker-compose.monitoring.yml`) adds Prometheus, Grafana, cAdvisor, and node-exporter — container and host metrics, not started by default:

```bash
# Set a Grafana admin password first
echo "GRAFANA_ADMIN_PASSWORD=$(openssl rand -base64 24)" >> .env

docker compose --env-file .env -f docker-compose.yml -f docker-compose.monitoring.yml up -d
```

Grafana is bound to `127.0.0.1:3001` by default — use an SSH tunnel, or put it behind Traefik with an auth middleware if you need remote access.

---

# ☁️ Deployment Options

PlumoAI can run in two ways.

### Self Hosted

Run on your own infrastructure using Docker.

✔ Completely free
✔ Unlimited AI Employees
✔ Unlimited Project Management including web documentation
✔ Full data control

---

### PlumoAI Cloud

If you dont want to FREE setup on your cloud or local machine then you can use our managed infrastructure with usage based credits.

Starting from **$20 credits**.

More info:

https://plumoai.com/get-started

---

# 🤝 Sponsorship Program

Organizations can support the development of PlumoAI.

### 🥈 Silver Sponsor

Priority issue response
Email support
Roadmap updates
Sponsor recognition

### 🥇 Gold Sponsor

Priority feature requests
Private support channel
Direct support calls
AI Employee deployment consultation

More info:

https://plumoai.com/get-started

---

# 🗺 Product Roadmap (Work in Progress)

### Phase 1 — Core Platform

AI Employee architecture
OpenClaw integration
Project management workspace
Self hosted Docker deployment

---

### Phase 2 — AI Agent Ecosystem

Developer framework for AI Agents
MCP based integrations
Integration submission system

---

### Phase 3 — AI Employee Templates

Pre built employees for:

Sales - like AI Sales Managers, AI Sales Executive etc
Research - AI Research Manager
Operations - AI Operationo Manager
Marketing - AI Marketing Manager
Product management - AI Product Manager


---

# 🌎 Community

Join the growing ecosystem around **Autonomous AI Employees**.

Developers
Founders
Automation engineers
SaaS companies

Together we are building the **future of work**.

---

# 📜 License

PlumoAI is distributed under the **PlumoAI Community License**.

Users may run PlumoAI using official Docker images inside their organizations.

Users may not:

❌ Resell the platform
❌ Redistribute Docker images
❌ Operate PlumoAI as a competing SaaS service

---

# 🚀 Vision

The future of organizations will include teams of **Autonomous AI Employees** working alongside humans.

PlumoAI provides the infrastructure where these AI Employees operate.
