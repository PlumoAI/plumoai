# PlumoAI Self-Hosted — Installation Guide

## Prerequisites

- Docker and Docker Compose installed
- For **domain mode**: a domain name pointing to your server
- For **localhost mode**: no domain required

---

## Step 1: Create `.env` (before running quickstart)

Create a `.env` file **in the directory where you will run the quickstart command**. The script will copy it into the install folder.

### Option A: Domain mode (HTTPS with Let's Encrypt)

For production use with your own domain.

```bash
cd ~
cat > .env << 'EOF'
RUN_MODE=domain
DOMAIN_NAME=self.plumoai.com
SSL_EMAIL=admin@your-domain.com

# AWS (optional)
AWS_S3_BUCKET=<YOUR_AWS_S3_BUCKET>
AWS_REGION=<YOUR_AWS_REGION>
AWS_ACCESS_KEY_ID=<YOUR_AWS_ACCESS_KEY_ID>
AWS_SECRET_ACCESS_KEY=<YOUR_AWS_SECRET_ACCESS_KEY>
EOF
```

**Replace:**
- `self.plumoai.com` → your domain (must point to your server IP)
- `admin@your-domain.com` → your email for Let's Encrypt certificates

---

### Option B: Localhost mode (HTTP for local development)

For local use without a domain or SSL.

```bash
cd ~
cat > .env << 'EOF'
RUN_MODE=localhost
LOCALHOST_PORT=80

# AWS (optional)
AWS_S3_BUCKET=<YOUR_AWS_S3_BUCKET>
AWS_REGION=<YOUR_AWS_REGION>
AWS_ACCESS_KEY_ID=<YOUR_AWS_ACCESS_KEY_ID>
AWS_SECRET_ACCESS_KEY=<YOUR_AWS_SECRET_ACCESS_KEY>
EOF
```

**Replace:**
- `80` → any port you want (e.g. `8080`, `7543`)

You can also use `nano .env` or any editor to create the file.

---

## Step 2: Run quickstart

From the **same directory** where `.env` was created:

### Linux / macOS / WSL

```bash
curl -sSL https://plumoai.com/downloads/self-hosted/linux/quickstart.sh | bash
```

(Uses latest version; script downloads the versioned zip.)

### Windows (PowerShell)

1. Create `.env` in your working directory (see Step 1).
2. Download `quickstart.ps1`:
   - From the zip: extract and use `quickstart.ps1` from `plumoai-self-hosted/`
   - Or: `https://plumoai.com/downloads/self-hosted/windows/quickstart.ps1`
3. Run from the directory containing `.env`:

```powershell
powershell -ExecutionPolicy Bypass -File quickstart.ps1
```

**Requires:** Docker Desktop. Uses native `install.ps1`; falls back to WSL/Git Bash if needed.

---

## Step 3: Access the app

| Mode    | URL                          |
|---------|------------------------------|
| Domain  | `https://your-domain.com`    |
| Localhost | `http://localhost:PORT` (e.g. `http://localhost:80` or `http://localhost:7543`) |

**Localhost API:** Use the same base URL for API calls, e.g. `http://localhost:7543/api/auth/company/signup`. Traefik routes `/api/auth/*` and `/api/company/*` to the auth and company services. Always start with both compose files: `-f docker-compose.yml -f docker-compose.local.yml`.

---

## Quick reference

| Variable        | Domain mode | Localhost mode |
|-----------------|-------------|----------------|
| `RUN_MODE`      | `domain`    | `localhost`    |
| `DOMAIN_NAME`   | Required    | Ignored        |
| `SSL_EMAIL`     | Required    | Ignored        |
| `LOCALHOST_PORT`| Ignored     | Optional (default: 80) |

---

## Troubleshooting

- **Error: DOMAIN_NAME and SSL_EMAIL must be set** — Ensure `.env` exists in the directory where you ran the command before running quickstart.
- **Interactive prompts** — If you skip creating `.env`, the script will prompt for values when run interactively (not via `curl | bash`).
