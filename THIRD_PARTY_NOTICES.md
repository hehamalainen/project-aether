# Third-Party Notices and Provenance Review

Project Aether is released under the [MIT License](LICENSE).

## Source Provenance Summary

- All tracked source code in `packages/`, `apps/`, `aether-rs/`, `scripts/`, and `tests/` is original first-party research prototype code authored for Project Aether.
- No third-party source files, fonts, or precompiled binaries are vendored in the repository tree (aside from the Next.js scaffold `apps/aether_ui/app/favicon.ico` and SVG icons under `apps/aether_ui/public/`).
- External dependencies are installed at build/runtime via `pip` (`pyproject.toml`), `cargo` (`aether-rs/Cargo.toml`), and `npm` (`apps/aether_ui/package.json`).

---

## Direct Dependency License Inventory

### 1. Python Reference Stack (`pyproject.toml`)

| Package | Declared License |
|---|---|
| `fastapi` | MIT |
| `uvicorn` | BSD-3-Clause |
| `sqlmodel` | MIT |
| `sqlalchemy` | MIT |
| `typer` | MIT |
| `rich` | MIT |
| `httpx` | BSD-3-Clause |
| `pydantic` | MIT |
| `pydantic-settings` | MIT |
| `python-dotenv` | BSD-3-Clause |
| `orjson` | Apache-2.0 OR MIT |
| `aiofiles` | Apache-2.0 |
| `libcst` | MIT (includes PSF-2.0 grammar derivatives) |
| `python-dateutil` | Apache-2.0 OR BSD-3-Clause |
| `structlog` | Apache-2.0 OR MIT |
| `pyyaml` | MIT |
| `websockets` | BSD-3-Clause |
| `pytest` (dev) | MIT |
| `pytest-asyncio` (dev) | Apache-2.0 |
| `ruff` (dev) | MIT |
| `mypy` (dev) | MIT |

### 2. Rust Control Plane Workspace (`aether-rs/Cargo.toml`)

| Crate | Declared License |
|---|---|
| `tokio` | MIT |
| `axum` | MIT |
| `serde` / `serde_json` | MIT OR Apache-2.0 |
| `sqlx` | MIT OR Apache-2.0 |
| `clap` | MIT OR Apache-2.0 |
| `uuid` | Apache-2.0 OR MIT |
| `chrono` | MIT OR Apache-2.0 |
| `tracing` / `tracing-subscriber` | MIT |
| `anyhow` / `thiserror` | MIT OR Apache-2.0 |
| `sha2` | MIT OR Apache-2.0 |
| `wasmi` | MIT OR Apache-2.0 |
| `tree-sitter` / `tree-sitter-python` | MIT |
| `diffy` | MIT OR Apache-2.0 |
| `walkdir` | Unlicense OR MIT |
| `regex` | MIT OR Apache-2.0 |
| `once_cell` | MIT OR Apache-2.0 |
| `reqwest` | MIT OR Apache-2.0 |
| `proptest` (dev) | MIT OR Apache-2.0 |
| `rand` (dev) | MIT OR Apache-2.0 |

### 3. Next.js UI Dashboard (`apps/aether_ui/package.json`)

| Package | Declared License |
|---|---|
| `next` | MIT |
| `react` / `react-dom` | MIT |
| `@radix-ui/react-slot` / `@radix-ui/react-tabs` | MIT |
| `class-variance-authority` | Apache-2.0 |
| `clsx` | MIT |
| `lucide-react` | ISC |
| `tailwind-merge` | MIT |
| `tailwindcss` / `@tailwindcss/postcss` | MIT |
| `typescript` | Apache-2.0 |
| `eslint` / `eslint-config-next` | MIT |
