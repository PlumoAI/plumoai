# PlumoAI Project Structure

## 1) What this project is

PlumoAI is a self-hosted AI employee platform. It is designed to let you create and run autonomous AI workers that behave like business employees, with:

- a defined role
- tools and integrations
- permissions/authorization
- memory
- accountability
- presence (chat, email, messaging, etc.)

The repo is a modular system for connecting AI to business tools such as Gmail, Google Calendar, Slack/Discord, WhatsApp, LinkedIn, Notion, CRMs, databases, and workflow automation nodes.

In simple terms: this project helps you build AI agents that can do real work for a business.

---

## 2) Main idea behind the architecture

The README describes a model based on the "Six Component Principle of an Employee":

1. Role
2. Tools
3. Authorization
4. Memory
5. Accountability
6. Presence

So the platform is not just a chatbot. It tries to create AI workers that can operate inside a business workflow with permissions, memory, and execution capabilities.

---

## 3) Project folders and meaning

```text
plumoai/
├── README.md                          # Main project overview and selling pitch
├── install.sh                         # Linux/macOS installer
├── install.ps1                        # Windows installer
├── docker-compose.yml                 # Main Docker stack
├── docker-compose.local.yml           # Local/dev docker config
├── docker-compose.monitoring.yml      # Monitoring config
├── .env.example                       # Environment variables template
├── LICENSE.md                         # Licensing info
├── SECURITY.md                        # Security policy
├── GOVERNANCE.md                      # Community governance
├── CONTRIBUTING.md                    # Contribution rules
├── CODE_OF_CONDUCT.md                 # Community code of conduct
├── MAINTAINERS.md                     # Maintainer list
├── docs/
│   ├── AI_AGENT_PLUGIN_CREATION_GUIDE.md
│   ├── NODE_CREATION_GUIDE.md
│   ├── architecture.md
│   ├── GMAIL_AGENT_AND_GOOGLE_PROVIDER_WORKED_EXAMPLE.md
│   └── install-linux.md / install-windows.md
├── ai-agents/
│   ├── _shared/
│   ├── ai_writer/
│   ├── aiagent/
│   ├── apify/
│   ├── calcom/
│   ├── calculator_datetime/
│   ├── call/
│   ├── chartmaker/
│   ├── discord/
│   ├── getleads/
│   ├── gmail/
│   ├── google_calendar/
│   ├── google_chat/
│   ├── google_drive/
│   ├── google_meet/
│   ├── google_sheets/
│   ├── imagegen/
│   ├── knowledgebase/
│   ├── linkedin/
│   ├── loop_executor/
│   ├── memory/
│   ├── notion/
│   ├── plumoai/
│   ├── sendgrid/
│   ├── sqlserver/
│   ├── upwork/
│   ├── websearch/
│   ├── whatsapp_business/
│   └── youtube/
├── service-providers/
│   ├── apify/
│   ├── apollo/
│   ├── calcom/
│   ├── calendly/
│   ├── discord/
│   ├── getleads/
│   ├── github/
│   ├── google/
│   ├── linkedin/
│   ├── lusha/
│   ├── microsoft/
│   ├── notion/
│   ├── openrouter/
│   ├── sendgrid/
│   ├── sqlserver/
│   ├── upwork/
│   ├── whatsapp_business/
│   ├── zoom/
│   └── README.md
├── nodes/
│   ├── code/
│   ├── data_transformation/
│   ├── flow/
│   ├── trigger/
├── scripts/
│   ├── backup.sh
│   ├── backup.ps1
│   ├── restore.sh
│   ├── restore.ps1
│   ├── init-mongo-user.sh
│   ├── mongo-secrets-entrypoint.sh
│   └── mysql-root-secrets-entrypoint.sh
├── monitoring/
│   └── prometheus.yml
├── examples/
│   ├── ai-data-analyst-employee.md
│   └── ai-sales-employee.md
├── issue-template/
│   ├── bug_report.yml
│   ├── feature_request.yml
│   └── PULL_REQUEST_TEMPLATE.md
└── .github/ (if present in full repo)
```

---

## 4) Key folder explanations

### `ai-agents/`
This is the core of the project. Each subfolder is a specific AI tool or integration module.

Examples:

- `gmail/` → Gmail operations
- `google_calendar/` → calendar scheduling
- `linkedin/` → LinkedIn automation
- `whatsapp_business/` → WhatsApp messaging
- `notion/` → Notion tasks/docs
- `websearch/` → web search
- `knowledgebase/` → internal knowledge lookup
- `memory/` → persistent memory

These modules act like app capabilities that an AI employee can use.

### `service-providers/`
This holds provider integrations and authentication patterns for external services.
Examples: Google, Microsoft, Notion, Zoom, SendGrid, SQL Server, OpenRouter, etc.

This is where APIs and access credentials are managed.

### `nodes/`
This is the workflow-building layer.

It is similar to a no-code / low-code automation system, with nodes for:

- triggers
- flow control
- code execution
- data transformation

This is useful for deterministic automation, not just AI reasoning.

### `docs/`
Technical documentation for creating and extending AI agents and workflow nodes.

### `scripts/`
Deployment, backup, and restore automation.

### `monitoring/`
Observability and Prometheus monitoring configuration.

### `examples/`
Example employee roles and use cases.

---

## 5) How the project works

The platform is built around a pattern like this:

- you define an AI employee role
- you give the employee tools and permissions
- it accesses external services via AI agents or integrations
- it uses memory to remember context
- it can act autonomously and complete tasks

This is designed to map closely to a real business department or operator, such as:

- sales executive
- operations manager
- data analyst
- marketing assistant
- customer support rep

The README emphasizes that companies hire employees to produce outcomes, not just chat.

---

## 6) What this repo is good for

This repo is useful if you want to:

- run AI employees on your own machine or server
- connect AI with Gmail, CRM, search, messaging, and more
- build AI workflow automations
- self-host an AI business operating system
- create custom AI agents for business functions

---

## 7) How you can earn from this project

The README itself says this project is positioned for business and income generation. The main earning ideas are:

### A) Sell AI employee setup to businesses
You can set up PlumoAI for a client and charge them for:

- initial setup
- custom AI employee configuration
- tool integrations
- automation workflows
- monthly support

Example business offers:

- AI sales outreach employee
- AI support assistant
- AI lead generation employee
- AI appointment booking assistant
- AI research and reporting assistant

### B) Build a service business
Instead of only using the repo yourself, you can offer a service:

- "I build AI employees for businesses"
- "I set up sales automation using PlumoAI"
- "I create hiring/CRM automation workflows"
- "I connect their tools to AI agents"

This is a strong B2B service opportunity.

### C) Use it for your own business
If you run a small business, you can deploy your own AI sales or outreach employee to:

- contact leads
- send messages
- book meetings
- research prospects
- summarize results

This reduces manual labor and can directly increase revenue.

### D) Offer maintenance and subscription support
After setup, you can charge monthly management fees for:

- monitoring the AI employees
- handling integrations
- improving prompts and workflows
- data cleanup
- generating monthly reports

### E) Contribute to the project / build plugins
Adding new integrations and agents can also create value.

If you build a useful new AI tool or provider integration for PlumoAI, you could:

- contribute back to the open source project
- get hired by companies using it
- sell custom agent solutions
- build a startup around one niche workflow

---

## 8) Practical ways to make money with this repo

Here are realistic earning paths:

### Option 1: Freelancer / consultant
Offer setup packages:

- Basic AI worker setup: $300–$1,000+
- Business workflow automation: $1,000–$5,000+
- Monthly management: $200–$1,500+/month

### Option 2: Agency model
Create a small AI automation agency that builds:

- outbound sales agents
- support agents
- scheduling agents
- reporting agents

### Option 3: DIY internal use
Deploy it for your own company and use it to cut operations cost and increase sales output.

### Option 4: Build a niche product
Pick one vertical:

- real estate lead generation
- recruitment outreach
- SaaS sales outreach
- e-commerce support

Then package a specialized AI employee for that niche.

---

## 9) Important note

This repo is open source and self-hosted, but the real money path is often not selling the repo itself — it is selling the outcome it enables.

The value comes from:

- setup
- integration
- workflow design
- business automation
- support and optimization

So the business opportunity is usually in implementation and service, not just installing the software.

---

## 10) Recommended next steps

1. Read `README.md` fully.
2. Review `install.sh` and Docker setup.
3. Look at `ai-agents/` to see integrations.
4. Inspect `examples/` to understand employee roles.
5. Choose one use case: sales outreach, appointment booking, CRM support, or research.
6. Build a demo AI employee and sell a service around it.

---

## 11) Short summary

PlumoAI is a platform for building autonomous AI employees that can interact with real business systems. The project is structured around modular AI agents, external service integrations, workflow nodes, and self-hosted deployment.

The real earning opportunity is to package and sell this as a service: set up AI workers for businesses, integrate tools, automate workflows, and charge for setup and monthly optimization.
