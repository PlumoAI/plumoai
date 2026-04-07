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

#### Step 2: Create `.env`

Creating `.env` is **optional**. If it doesn’t exist, the installer will create it from `.env.example` and (when run interactively) prompt you for required values.
For production or non-interactive installs, it’s recommended to create/edit `.env` yourself.

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

#### Step 3: Install / Start

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

#### Step 4: Open

- Domain mode: `https://<your-domain>`
- Localhost mode: `http://localhost:7861`

#### Optional: Fresh install (resets MySQL volume)

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

- Docker Engine + **Docker Compose v2 plugin**
- Git
- **Production (domain mode)**: domain `A/AAAA` record → your server IP, and open inbound **80/443**

#### Step 1: Clone

```bash
git clone https://github.com/PlumoAI/plumoai.git
cd plumoai
```

#### Step 2: Create `.env`

Creating `.env` is **optional**. If it doesn’t exist, the installer will create it from `.env.example` and (when run interactively) prompt you for required values.
For production or non-interactive installs, it’s recommended to create/edit `.env` yourself.

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

#### Step 3: Install / Start

```bash
chmod +x install.sh
./install.sh
```

#### Step 4: Open

- Domain mode: `https://<your-domain>`
- Localhost mode: `http://localhost:7861`

#### Optional: Fresh install (resets MySQL volume)

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

#### Step 2: Create `.env`

Creating `.env` is **optional**. If it doesn’t exist, the installer will create it from `.env.example` and (when run interactively) prompt you for required values.
For production or non-interactive installs, it’s recommended to create/edit `.env` yourself.

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

#### Step 3: Install / Start

```bash
chmod +x install.sh
./install.sh
```

#### Step 4: Open

- Domain mode: `https://<your-domain>`
- Localhost mode: `http://localhost:7861`

#### Optional: Fresh install (resets MySQL volume)

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

1) Set `PLUMOAI_VERSION` in `.env` (or keep the default).  
2) Re-run the installer for your OS (it will pull the new images and apply changes).

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
