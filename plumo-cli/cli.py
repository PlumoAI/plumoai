#!/usr/bin/env python3
"""
plumo-cli: Headless CLI orchestrator for PlumoAI deployment.

Replaces interactive install.sh/install.ps1 with declarative configuration.

Usage:
    plumo-cli init          # Generate .env and secrets
    plumo-cli start         # Start all services
    plumo-cli stop          # Stop all services
    plumo-cli status        # Show service health
    plumo-cli logs          # Tail logs
    plumo-cli migrate       # Run database migrations
    plumo-cli backup        # Backup PostgreSQL
    plumo-cli restore       # Restore from backup
    plumo-cli doctor        # Check system requirements
"""

from __future__ import annotations

import os
import sys
import json
import secrets
import subprocess
import argparse
from pathlib import Path
from typing import Optional


def run_cmd(cmd: list[str], check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    """Run a shell command and return the result."""
    return subprocess.run(
        cmd,
        check=check,
        capture_output=capture,
        text=True
    )


def find_compose_cmd() -> str:
    """Find the docker compose command."""
    # Try docker compose v2 first
    result = run_cmd(["docker", "compose", "version"], check=False, capture=True)
    if result.returncode == 0:
        return "docker compose"

    # Fall back to docker-compose
    result = run_cmd(["docker-compose", "version"], check=False, capture=True)
    if result.returncode == 0:
        return "docker-compose"

    print("Error: Docker Compose not found. Please install Docker Compose v2.", file=sys.stderr)
    sys.exit(1)


def cmd_init(args):
    """Generate .env and secrets for PlumoAI."""
    print("🔧 Initializing PlumoAI configuration...")

    # Create secrets directory
    secrets_dir = Path("secrets")
    secrets_dir.mkdir(exist_ok=True)

    # Generate PostgreSQL secrets
    secrets_file = secrets_dir / "postgres_db.txt"
    if not secrets_file.exists():
        secrets_file.write_text("plumoai")
        print(f"  Created {secrets_file}")

    secrets_file = secrets_dir / "postgres_user.txt"
    if not secrets_file.exists():
        secrets_file.write_text("plumoai_user")
        print(f"  Created {secrets_file}")

    secrets_file = secrets_dir / "postgres_password.txt"
    if not secrets_file.exists():
        secrets_file.write_text(secrets.token_urlsafe(32))
        print(f"  Created {secrets_file}")

    # Create .env from template if it doesn't exist
    env_file = Path(".env")
    env_example = Path(".env.example")
    if not env_file.exists() and env_example.exists():
        env_file.write_text(env_example.read_text())
        print(f"  Created {env_file} from {env_example}")

    print("✅ Initialization complete!")
    print("\nNext steps:")
    print("  1. Edit .env to configure your deployment")
    print("  2. Run: plumo-cli start")


def cmd_start(args):
    """Start PlumoAI services."""
    print("🚀 Starting PlumoAI services...")

    compose_cmd = find_compose_cmd()
    env_file = args.env_file or ".env"

    if not Path(env_file).exists():
        print(f"Error: {env_file} not found. Run 'plumo-cli init' first.", file=sys.stderr)
        sys.exit(1)

    # Determine compose files
    compose_files = ["-f", "docker-compose.yml"]

    if args.profile == "dev":
        compose_files.extend(["-f", "docker-compose.dev.yml"])
    elif args.profile == "prod":
        compose_files.extend(["-f", "docker-compose.prod.yml"])
    elif args.profile == "local":
        compose_files.extend(["-f", "docker-compose.local.yml"])

    # Build command
    cmd = compose_cmd.split() + ["--env-file", env_file] + compose_files + ["up", "-d"]

    if args.force_recreate:
        cmd.append("--force-recreate")

    print(f"  Running: {' '.join(cmd)}")
    result = run_cmd(cmd, check=False)

    if result.returncode != 0:
        print("Error: Failed to start services.", file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        sys.exit(1)

    print("✅ Services started!")
    print("\nCheck status with: plumo-cli status")


def cmd_stop(args):
    """Stop PlumoAI services."""
    print("⏹️  Stopping PlumoAI services...")

    compose_cmd = find_compose_cmd()
    env_file = args.env_file or ".env"

    compose_files = ["-f", "docker-compose.yml"]
    if args.profile == "local":
        compose_files.extend(["-f", "docker-compose.local.yml"])

    cmd = compose_cmd.split() + ["--env-file", env_file] + compose_files + ["down"]

    if args.remove_volumes:
        cmd.append("-v")

    result = run_cmd(cmd, check=False)

    if result.returncode != 0:
        print("Error: Failed to stop services.", file=sys.stderr)
        sys.exit(1)

    print("✅ Services stopped!")


def cmd_status(args):
    """Show service status and health."""
    print("📊 PlumoAI Service Status\n")

    compose_cmd = find_compose_cmd()
    env_file = args.env_file or ".env"

    compose_files = ["-f", "docker-compose.yml"]
    if args.profile == "local":
        compose_files.extend(["-f", "docker-compose.local.yml"])

    cmd = compose_cmd.split() + ["--env-file", env_file] + compose_files + ["ps"]

    result = run_cmd(cmd, check=False, capture=True)
    print(result.stdout)


def cmd_logs(args):
    """View service logs."""
    compose_cmd = find_compose_cmd()
    env_file = args.env_file or ".env"

    compose_files = ["-f", "docker-compose.yml"]
    if args.profile == "local":
        compose_files.extend(["-f", "docker-compose.local.yml"])

    cmd = compose_cmd.split() + ["--env-file", env_file] + compose_files + ["logs"]

    if args.follow:
        cmd.append("-f")

    if args.service:
        cmd.append(args.service)

    if args.tail:
        cmd.extend(["--tail", str(args.tail)])

    # Run interactively so user can see logs
    subprocess.run(cmd)


def cmd_doctor(args):
    """Check system requirements and configuration."""
    print("🩺 PlumoAI System Check\n")

    issues = []

    # Check Docker
    result = run_cmd(["docker", "--version"], check=False, capture=True)
    if result.returncode == 0:
        print(f"✅ Docker: {result.stdout.strip()}")
    else:
        print("❌ Docker: Not found")
        issues.append("Docker is not installed")

    # Check Docker Compose
    result = run_cmd(["docker", "compose", "version"], check=False, capture=True)
    if result.returncode == 0:
        print(f"✅ Docker Compose: {result.stdout.strip()}")
    else:
        result = run_cmd(["docker-compose", "version"], check=False, capture=True)
        if result.returncode == 0:
            print(f"✅ Docker Compose: {result.stdout.strip()}")
        else:
            print("❌ Docker Compose: Not found")
            issues.append("Docker Compose is not installed")

    # Check .env file
    if Path(".env").exists():
        print("✅ .env file: Found")
    else:
        print("❌ .env file: Not found")
        issues.append("Run 'plumo-cli init' to create .env")

    # Check secrets
    secrets_dir = Path("secrets")
    required_secrets = ["postgres_db.txt", "postgres_user.txt", "postgres_password.txt"]
    for secret in required_secrets:
        if (secrets_dir / secret).exists():
            print(f"✅ Secret {secret}: Found")
        else:
            print(f"❌ Secret {secret}: Not found")
            issues.append(f"Missing secret: {secret}")

    # Check Docker daemon
    result = run_cmd(["docker", "info"], check=False, capture=True)
    if result.returncode == 0:
        print("✅ Docker daemon: Running")
    else:
        print("❌ Docker daemon: Not running")
        issues.append("Start Docker daemon")

    # Summary
    print()
    if issues:
        print(f"Found {len(issues)} issue(s):")
        for issue in issues:
            print(f"  • {issue}")
    else:
        print("✅ All checks passed! Ready to start PlumoAI.")


def main():
    parser = argparse.ArgumentParser(
        description="PlumoAI CLI - Headless orchestration for PlumoAI deployment",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  plumo-cli init                    # Generate .env and secrets
  plumo-cli start                   # Start services (default profile)
  plumo-cli start --profile local   # Start in localhost mode
  plumo-cli start --profile dev     # Start in development mode
  plumo-cli status                  # Check service health
  plumo-cli logs ai                 # View AI service logs
  plumo-cli doctor                  # Check system requirements
        """
    )

    parser.add_argument("--env-file", help="Path to .env file (default: .env)")

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # init
    subparsers.add_parser("init", help="Generate .env and secrets")

    # start
    start_parser = subparsers.add_parser("start", help="Start PlumoAI services")
    start_parser.add_argument("--profile", choices=["default", "local", "dev", "prod"],
                              default="default", help="Deployment profile")
    start_parser.add_argument("--force-recreate", action="store_true",
                              help="Force recreate containers")

    # stop
    stop_parser = subparsers.add_parser("stop", help="Stop PlumoAI services")
    stop_parser.add_argument("--profile", choices=["default", "local"],
                             default="default", help="Deployment profile")
    stop_parser.add_argument("--remove-volumes", action="store_true",
                             help="Remove data volumes")

    # status
    status_parser = subparsers.add_parser("status", help="Show service status")
    status_parser.add_argument("--profile", choices=["default", "local"],
                               default="default", help="Deployment profile")

    # logs
    logs_parser = subparsers.add_parser("logs", help="View service logs")
    logs_parser.add_argument("service", nargs="?", help="Service name (e.g., ai, auth)")
    logs_parser.add_argument("-f", "--follow", action="store_true", help="Follow logs")
    logs_parser.add_argument("-n", "--tail", type=int, default=100, help="Number of lines to show")
    logs_parser.add_argument("--profile", choices=["default", "local"],
                             default="default", help="Deployment profile")

    # doctor
    subparsers.add_parser("doctor", help="Check system requirements")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Map commands to functions
    commands = {
        "init": cmd_init,
        "start": cmd_start,
        "stop": cmd_stop,
        "status": cmd_status,
        "logs": cmd_logs,
        "doctor": cmd_doctor,
    }

    commands[args.command](args)


if __name__ == "__main__":
    main()
