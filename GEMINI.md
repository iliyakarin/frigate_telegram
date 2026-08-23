# Rules & Development Guidelines for Frigate-Telegram

This document defines the strict engineering, testing, and deployment rules for the `frigate-telegram` project. All contributors and AI coding assistants must strictly adhere to these practices.

---

## 1. Core Engineering Principles

- **TDD (Test-Driven Development)**:
  - Follow the **Red-Green-Refactor** cycle.
  - **No production code without a failing test first**. Always write or update tests to reproduce a bug or specify a new feature before modifying implementation files (`main.py`, `grouping.py`, etc.).
  - Verify tests fail for the expected reason, write minimal code to pass, and verify all tests pass.
- **KISS (Keep It Simple, Stupid)**:
  - Strive for simplicity in implementation and configuration.
  - Avoid premature abstractions, speculative features, or over-engineering. YAGNI (You Aren't Gonna Need It).
- **SOLID**:
  - **Single Responsibility Principle**: Each module, class, and function must have one clear, well-defined responsibility.
  - **Open/Closed & Liskov Substitution**: Design components for extensibility without breaking existing contracts.
  - **Interface Segregation & Dependency Inversion**: Keep function signatures and dependencies modular, testable, and loosely coupled.
- **DRY (Don't Repeat Yourself)**:
  - Centralize common logic (e.g. Telegram timeout settings, caption formatting, API wrappers, config parsing).
  - Reuse test fixtures and utilities across test suites.

---

## 2. Testing Standards

- **Mandatory Coverage**: Every new feature, bug fix, or behavioral change must be accompanied by comprehensive unit tests.
- **Test Suites**:
  - `test_main.py` — Core application, API polling, notifications, command handlers.
  - `test_grouping.py` — Event grouping and debounce window logic.
  - `test_security.py` — Authorization and access control.
  - `test_state.py` — Persistent notification state management.
  - `test_utils.py` — Helper functions and formatting.
- **Verification Command**:
  Always verify the entire test suite passes before committing:
  ```bash
  python3 -m pytest
  # or
  python3 test_main.py
  ```

---

## 3. Strict Deployment & CI/CD Process

> [!CAUTION]
> **NEVER make direct, manual, or incremental code edits inside running Docker containers** on production servers (no `docker cp`, no editing `/app/main.py` in containers). All updates must go through the automated CI/CD pipeline.

### The Deployment Workflow:

1. **Implement and Test Locally**:
   - Write tests first (TDD).
   - Implement the change.
   - Run `python3 -m pytest` and ensure 100% of tests pass.

2. **Commit and Push to GitHub**:
   - Push commits directly to the `main` branch (or via PR):
     ```bash
     git add .
     git commit -m "feat/fix: descriptive message"
     git push origin main
     ```

3. **Verify GitHub Actions Build**:
   - Automated CI/CD (`.github/workflows/docker-publish.yml`) will:
     - Run the automated test suite.
     - Bump the semantic version tag (e.g. `v1.0.52`).
     - Build multi-arch Docker images and push them to Docker Hub (`iliyakarin/frigate-telegram`) and GitHub Container Registry (`ghcr.io/ikarin/frigate-telegram`).
   - **Monitor the build status and wait for completion** before deploying:
     ```bash
     gh run list --limit 1
     gh run watch <run_id>
     ```

4. **Deploy on Production Server (CCTV VM)**:
   - Once the build succeeds, connect to the server or update `docker-compose.yml`:
     ```bash
     cd /home/ikarin/frigate-compose
     # Update image tag in docker-compose.yml if pinned to a specific version
     docker compose pull frigate-telegram
     docker compose up -d --force-recreate frigate-telegram
     ```
   - Verify logs and container health:
     ```bash
     docker logs --tail 50 -f frigate-telegram
     ```
