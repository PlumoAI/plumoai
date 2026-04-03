# PlumoAI

Still in **private beta** -- email **krishna@plumoai.com** for access. Official single-image Docker packaging is rolling out; this repository includes a **full Compose installer** (`install.ps1` / `install.sh`).

![PlumoAI logo](https://github.com/user-attachments/assets/43d692bd-912c-44eb-b13b-0a2ed0a1ce6e)

## Autonomous AI employee platform (AI Employees OS)

![GitHub stars](https://img.shields.io/github/stars/PlumoAI/plumoai?style=social)
![License](https://img.shields.io/badge/license-PlumoAI-blue)
![Docker](https://img.shields.io/badge/docker-supported-blue)
![Self Hosted](https://img.shields.io/badge/self--hosted-free-green)
![AI Employees](https://img.shields.io/badge/AI%20Employees-autonomous-purple)
![Security](https://img.shields.io/badge/security-report%20issues%20responsibly-green)

---

## Hire AI employees in minutes

PlumoAI is the platform for **autonomous AI employees**: own work, deliver outcomes, run on your infrastructure, connect tools with App AI agents, and use built-in project management.

---

## Completely free self-hosted

Individuals, startups, teams, and enterprises -- deploy on your own infrastructure with no artificial caps on AI employees for self-hosted use.

---

## Current sponsors

![Sponsor](https://github.com/user-attachments/assets/8f3c549d-9eca-4a4d-a0ef-f38153929464)

### Approaching to onboard

![Partner](https://github.com/user-attachments/assets/e9c4963d-a6e5-49f1-ad9d-e2b3b3caf33e)

![Partner](https://github.com/user-attachments/assets/05f40114-b2bc-413e-8f67-62aba024802e)

![Partner](https://github.com/user-attachments/assets/234ec1a7-a750-475d-bdc7-56518b9cbc10)

![Partner](https://github.com/user-attachments/assets/c9a6b8f1-b49a-4bad-b0e4-363692151124)

---

## Powered by OpenClaw

Advanced reasoning, planning, and multi-step autonomous workflows across your systems.

---

## The six components of an employee

**Role**, **tools** (App AI agents), **authorization**, **memory**, **accountability**, and **presence** (chat, email, voice, and more).

---

## App AI agent ecosystem

MCP-backed integrations: email, analytics, messaging, databases, CRM, and more.

---

## Built-in project management

Projects, tasks, assignments to AI employees, monitoring, and human collaboration.

---

## Installation (from Git -- recommended)

Start from the **installer** after cloning -- **not** from quickstart. Quickstart zip/curl is optional; see [INSTALL.md](INSTALL.md).

### Prerequisites

- Docker Engine and **Docker Compose v2** (Docker Desktop on Windows).

### 1. Clone

```bash
git clone https://github.com/PlumoAI/plumoai.git
cd plumoai
```

Run the next steps from the folder that contains `docker-compose.yml`, `install.ps1`, and `install.sh` (if your monorepo keeps the stack in a subfolder, `cd` there).

**Dedicated self-hosted repo (same installer):** [github.com/PlumoAI/PlumoAi-Self-Hosted](https://github.com/PlumoAI/PlumoAi-Self-Hosted)

### 2. Optional: `.env`

Create `.env` for domain mode, localhost port, optional AWS variables. Examples in [INSTALL.md](INSTALL.md). If you skip this, the installer prompts when run interactively.

### 3. Run the installer

**Windows (PowerShell):**

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

**Linux / macOS / WSL / Git Bash:**

```bash
chmod +x install.sh
./install.sh
```

### 4. Fresh MySQL reset (optional)

```powershell
.\install.ps1 -Fresh
```

```bash
./install.sh --fresh
```

### 5. Open the app

| Mode | URL |
|------|-----|
| Localhost | `http://localhost:<PORT>` (`LOCALHOST_PORT` in `.env`) |
| Domain | `https://<your-domain>` (Let's Encrypt via Traefik) |

### Alternative: packaged quickstart

[INSTALL.md](INSTALL.md) documents `quickstart.ps1` / `quickstart.sh` for versioned zip downloads.

### Single-image preview (when published)

```bash
docker pull plumoai/platform
docker run -p 3000:3000 plumoai/platform
```

The **full** product stack runs via the **Git installer** and Compose.

---

## Deployment options

- **Self-hosted:** free, your infrastructure -- use the Git installer above.
- **PlumoAI Cloud:** managed, usage credits from **$20** -- [plumoai.com/get-started](https://plumoai.com/get-started)

---

## Sponsorship program

Silver and Gold tiers -- [plumoai.com/get-started](https://plumoai.com/get-started)

---

## Product roadmap

1. Core platform, OpenClaw, PM workspace, self-hosted Docker
2. App AI agent ecosystem and MCP integrations
3. AI employee templates (sales, research, ops, marketing, PM)
4. App marketplace

---

## Community

Developers, founders, automation engineers, and SaaS teams building autonomous AI employees.

---

## License

**PlumoAI Community License** -- run official deployments in your organization; no reselling, unauthorized redistribution of images, or competing SaaS without permission.

---

## Vision

Teams of **autonomous AI employees** alongside humans -- PlumoAI is the infrastructure where they operate.
