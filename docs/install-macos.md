# Install PlumoAI on macOS

PlumoAI runs on macOS as a Docker Compose stack, the same containers used on Linux and Windows.
There is no separate macOS build and no feature difference between platforms.

Three ways to install:

- **[Double-click installer](#double-click-installer)** - download one file, double-click it. Closest to the Windows `PlumoAISetup.exe` experience.
- **[Quick install](#quick-install)** - one script that checks prerequisites, installs Docker Desktop if needed, and starts the stack.
- **[Manual install](#manual-install)** - the same steps run by hand.

---

## Requirements

| | |
|---|---|
| macOS | 12 (Monterey) or newer |
| Chip | Apple Silicon or Intel |
| Docker | Docker Desktop with Compose v2 |
| Git | Included with the Xcode Command Line Tools |
| Disk | About 6 GB for images and volumes |

For **domain mode** you also need an `A`/`AAAA` record pointing at the host and inbound ports **80** and **443** open.

---

## Double-click installer

For users who would rather not touch the command line, `PlumoAI-Installer.command` is a double-clickable bootstrapper - the macOS counterpart to `PlumoAISetup.exe` on Windows.

1. Download `PlumoAI-Installer.command` (or the `.zip` containing it, then unzip).
2. **Right-click it and choose Open** the first time, then confirm. macOS shows a warning for any downloaded, unsigned file; right-click-Open is how you approve it. A plain double-click works on every launch after that.
3. A Terminal window opens and the install runs. When it finishes, your browser opens at the running instance.

Behind the scenes it downloads PlumoAI into `~/PlumoAI` (or uses a checkout it is sitting inside), then runs `install-macos.sh`. If that script is not present in the version it fetches, it falls back to running `install.sh` directly.

> The one-time warning is expected. Removing it entirely requires an Apple Developer ID to sign and notarize the file, the same way the Windows installer would need a code-signing certificate to avoid SmartScreen.

## Quick install

```bash
git clone https://github.com/PlumoAI/plumoai.git
cd plumoai
chmod +x install-macos.sh
./install-macos.sh
```

The script will:

1. Verify the macOS version and that Git is available.
2. Check for Docker Desktop, and offer to install it through Homebrew if it is missing.
3. Start Docker Desktop and wait for the daemon to come up.
4. Confirm Docker Compose v2 is present.
5. Hand off to `install.sh`, which creates `.env`, generates secrets, and starts the containers.
6. Open your browser at the running instance.

### Check without changing anything

To see what the installer would do on this machine without installing or starting anything:

```bash
./install-macos.sh --check
```

Example output:

```
  PlumoAi
  Self-Hosted - macOS installer

  check mode - nothing will be installed or changed

Checking prerequisites
  ok    macOS 15.2 (arm64)
  ok    git 2.50.1
  ok    Homebrew 4.4.0
  ok    Docker Desktop found
  ok    Docker daemon is running
  ok    Docker Compose v2 (2.31.0)

Repository
  ok    Using existing checkout: /Users/you/plumoai

  This Mac is ready. Run ./install-macos.sh to install.
```

### Options

| Flag | Effect |
|---|---|
| `--check` | Report prerequisites only. Installs nothing, starts nothing. |
| `--fresh` | Passed through to `install.sh`: backs up, then resets the MySQL volume. |
| `--no-backup` | Passed through to `install.sh`: skip the backup that `--fresh` takes first. |
| `--no-open` | Do not open the browser when the install finishes. |
| `--help` | Show usage. |

---

## Manual install

If you would rather not use the wrapper, or Docker Desktop is already set up:

### 1. Install Docker Desktop

```bash
brew install --cask docker
open -a Docker
```

Or download it from [docker.com](https://www.docker.com/products/docker-desktop/). Wait for the whale icon in the menu bar to stop animating, then confirm:

```bash
docker compose version
```

### 2. Clone

```bash
git clone https://github.com/PlumoAI/plumoai.git
cd plumoai
```

### 3. Optional: configure `.env`

You can skip this. Step 4 creates `.env` from `.env.example` and prompts for the values it needs when run interactively. For production or non-interactive installs, write it in advance.

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

### 4. Install and start

```bash
chmod +x install.sh
./install.sh
```

First run pulls images and initialises the databases, which usually takes 5 to 10 minutes.

When it finishes, PlumoAI is at `http://localhost:7861` in localhost mode, or `https://your-domain` in domain mode.

---

## Managing the stack

```bash
docker compose ps                       # container status
docker compose logs --tail 200 -f       # follow logs
docker compose stop                     # stop, keep data
docker compose start                    # start again
docker compose down                     # stop and remove containers, keep volumes
```

Reset the database and start over, taking a backup first:

```bash
./install.sh --fresh
```

---

## Troubleshooting

**`Cannot connect to the Docker daemon`**
Docker Desktop is installed but not running. Start it with `open -a Docker` and wait for the menu bar icon to settle, then re-run.

**`Docker Compose not found`**
Docker Desktop bundles Compose v2. If `docker compose version` fails, update Docker Desktop to a current release.

**Port 7861 already in use**
Something else holds the port. Find it with `lsof -i :7861`, then either stop that process or set a different `LOCALHOST_PORT` in `.env`.

**Docker Desktop asks for a password on first launch**
Expected. It installs a privileged helper the first time it runs. Complete that prompt before re-running the installer.

**Apple Silicon image warnings**
Some images may pull an `amd64` build and run under Rosetta emulation. It works, but it is slower. Nothing needs changing.

**Installer says macOS is not supported**
The script requires macOS 12 or newer, because that is Docker Desktop's own minimum. Check with `sw_vers -productVersion`.

---

## Related

- [Linux install](install-linux.md)
- [Windows install](install-windows.md)
- [Architecture](architecture.md)
