#!/usr/bin/env python3
"""FlaskBase CLI — gestione DB e server.

Comandi supportati (stabili):
- serve
- reset-db
- init-db-complete (reset + seed utenti)
"""
from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import logging
import os
import signal
import socket
import shutil
import subprocess
import sys
import time
import json as _json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.traceback import install as rich_traceback_install

rich_traceback_install(show_locals=False)
_CONSOLE = Console()
os.environ.setdefault("CLI_RICH_LOGS", "1")


def _echo(msg: str) -> None:
    _CONSOLE.print(msg)


def _log_info(msg: str) -> None:
    _CONSOLE.print(f"[cyan]INFO[/cyan] {msg}")


def _log_ok(msg: str) -> None:
    _CONSOLE.print(f"[green]PASS[/green] {msg}")


def _log_warn(msg: str) -> None:
    _CONSOLE.print(f"[yellow]WARN[/yellow] {msg}")


def _log_err(msg: str) -> None:
    _CONSOLE.print(f"[red]FAIL[/red] {msg}")


def _now_utc() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _print_cli_help(parser: argparse.ArgumentParser) -> None:
    usage = parser.format_usage().strip()
    _CONSOLE.print(
        Panel.fit(
            Text(usage, style="bold cyan"),
            title="[bold]FlaskBase CLI[/bold]",
            border_style="blue",
        )
    )

    if parser.description:
        _CONSOLE.print(f"[white]{parser.description}[/white]")

    sub_actions = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    if sub_actions:
        sub = sub_actions[0]
        helps = {a.dest: (a.help or "").strip() for a in getattr(sub, "_choices_actions", [])}
        table = Table(show_header=True, header_style="bold magenta", box=None, pad_edge=False)
        table.add_column("Comando", style="bold cyan", no_wrap=True)
        table.add_column("Descrizione", style="white")
        for name in sorted(sub.choices.keys()):
            sp = sub.choices[name]
            desc = (sp.description or helps.get(name, "")).strip()
            table.add_row(name, desc)
        _CONSOLE.print()
        _CONSOLE.print("[bold]Comandi disponibili[/bold]")
        _CONSOLE.print(table)

    _CONSOLE.print("\n[dim]Usa[/dim] [cyan]python cli.py <comando> --help[/cyan] [dim]per i dettagli.[/dim]")


def _get_app():
    from app import create_app
    return create_app()


def _ensure_postgres_db() -> None:
    """Per PostgreSQL: crea il database se non esiste ancora.
    Deve girare PRIMA di create_app() / db.create_all().
    No-op per SQLite.
    """
    import urllib.parse

    db_url = os.getenv("DATABASE_URL", "").strip()
    if not db_url or db_url.startswith("sqlite"):
        return

    parsed = urllib.parse.urlparse(db_url)
    if not any(s in parsed.scheme.lower() for s in ("postgres", "psycopg")):
        return

    db_name = (parsed.path or "").lstrip("/").strip()
    if not db_name:
        _log_warn("_ensure_postgres_db: impossibile ricavare il nome DB dall'URL.")
        return
    if not all(c.isalnum() or c in ("_", "-") for c in db_name):
        _log_err(f"_ensure_postgres_db: nome DB non sicuro: {db_name!r}")
        return

    host     = parsed.hostname or "127.0.0.1"
    port     = parsed.port or 5432
    user     = urllib.parse.unquote(parsed.username or "")
    password = urllib.parse.unquote(parsed.password or "")

    # --- prova psycopg v3 poi psycopg2 ---
    _psycopg = None
    _v3 = False
    try:
        import psycopg as _psycopg3  # type: ignore
        _psycopg = _psycopg3
        _v3 = True
    except ImportError:
        pass
    if _psycopg is None:
        try:
            import psycopg2 as _psycopg2  # type: ignore
            _psycopg = _psycopg2
        except ImportError:
            pass
    if _psycopg is None:
        _log_warn("_ensure_postgres_db: psycopg/psycopg2 non trovato — skip.")
        return

    # Connessione al DB di manutenzione 'postgres'
    conn_kwargs = dict(host=host, port=port, user=user, password=password, dbname="postgres")
    conn = None
    try:
        if _v3:
            # psycopg v3: autocommit è attributo, non parametro di connect()
            conn = _psycopg.connect(**conn_kwargs)
            conn.autocommit = True
        else:
            conn = _psycopg.connect(**conn_kwargs)
            conn.autocommit = True
    except Exception as exc:
        _log_warn(f"_ensure_postgres_db: connessione a 'postgres' fallita: {exc}")
        return

    try:
        with contextlib.closing(conn):
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
                exists = cur.fetchone() is not None

            if not exists:
                _log_info(f"Database '{db_name}' non esiste — creazione in corso...")
                with conn.cursor() as cur:
                    cur.execute(f'CREATE DATABASE "{db_name}"')
                _log_ok(f"Database '{db_name}' creato con successo.")
            else:
                _log_info(f"Database '{db_name}' già esistente.")
    except Exception as exc:
        _log_err(f"_ensure_postgres_db: impossibile creare il DB: {exc}")


def _cli_version() -> str:
    cfg_path = Path(os.getenv("CONFIG_PATH", "app_config.json"))
    if cfg_path.exists():
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        settings = data.get("SETTINGS") if isinstance(data, dict) else None
        if isinstance(settings, dict):
            return str(settings.get("APP_VERSION", "1.0.0"))
        return str(data.get("APP_VERSION", "1.0.0"))
    return "1.0.0"


def cmd_init_db(admin_email: Optional[str], admin_name: Optional[str]) -> int:
    from app.extensions import db
    from app.models import User

    _ensure_postgres_db()
    app = _get_app()
    with app.app_context():
        try:
            os.makedirs(app.instance_path, exist_ok=True)
            db.create_all()
            _echo("[green]PASS Tabelle create.[/green]")
            if admin_email is None:
                admin_email = input("Email admin (lascia vuoto per saltare): ").strip().lower() or None
            if not admin_email:
                _echo("[yellow]Admin non creato.[/yellow]")
                return 0
            admin_name = admin_name or (input("Nome admin: ").strip() or "Admin")
            pwd1 = getpass.getpass("Password: ")
            pwd2 = getpass.getpass("Conferma: ")
            if pwd1 != pwd2:
                _echo("[red]FAIL Le password non coincidono.[/red]")
                return 2
            if len(pwd1) < 8:
                _echo("[red]FAIL Password troppo corta (min 8 caratteri).[/red]")
                return 2
            if User.query.filter_by(email=admin_email).first():
                _echo(f"[red]FAIL Utente già esistente:[/red] {admin_email}")
                return 1
            u = User(
                email=admin_email,
                name=admin_name,
                role="admin",
                is_active=True,
                email_verified=True,
                account_status="active",
                signup_source="admin_cli",
            )
            u.set_password(pwd1)
            db.session.add(u)
            db.session.commit()
            _echo(f"[green]PASS Admin creato:[/green] {admin_email}")
            return 0
        finally:
            with contextlib.suppress(Exception):
                db.session.remove()
            with contextlib.suppress(Exception):
                db.engine.dispose()


def cmd_init_db_complete(force: bool = False) -> int:
    """Reset DB + seed utenti di default (admin/user)."""
    rc = cmd_reset_db(force=force)
    if rc != 0:
        return rc
    return cmd_init_users()


def cmd_reset_db(force: bool = False) -> int:
    from app.extensions import db
    from app.models import LogEvent, User

    if not force:
        ans = input("Confermi RESET completo DB (DROP + CREATE)? [y/N]: ").strip().lower()
        if ans not in ("y", "yes"):
            _echo("[yellow]Operazione annullata.[/yellow]")
            return 1

    _ensure_postgres_db()
    app = _get_app()
    with app.app_context():
        try:
            _echo(f"[dim]DB in uso: {_mask_db_uri(str(app.config.get('SQLALCHEMY_DATABASE_URI','')))}[/dim]")
            db.drop_all()
            db.create_all()
            db.session.commit()
            _echo("[green]PASS Database resettato (tabelle ricreate).[/green]")
            _echo(f"  users={User.query.count()}  log_events={LogEvent.query.count()}")
            return 0
        finally:
            with contextlib.suppress(Exception):
                db.session.remove()
            with contextlib.suppress(Exception):
                db.engine.dispose()


def _sqlite_files_from_uri(uri: str) -> list[Path]:
    raw = str(uri or "").strip()
    if not raw.startswith("sqlite:///"):
        return []
    db_path = raw.replace("sqlite:///", "", 1).strip()
    if not db_path or db_path == ":memory:":
        return []
    p = Path(db_path)
    return [p, Path(str(p) + "-wal"), Path(str(p) + "-shm")]


def _mask_db_uri(uri: str) -> str:
    raw = (uri or "").strip()
    if not raw:
        return ""
    if raw.startswith("sqlite"):
        return raw
    try:
        import urllib.parse

        parsed = urllib.parse.urlsplit(raw)
        netloc = parsed.netloc
        if "@" in netloc and ":" in netloc.split("@", 1)[0]:
            creds, rest = netloc.split("@", 1)
            user = creds.split(":", 1)[0]
            netloc = f"{user}:***@{rest}"
        return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))
    except Exception:
        return raw


def cmd_reset(force: bool = False) -> int:
    """Hard reset locale: cancella DB/file runtime, documenti RAG, ricrea schema e utenti demo."""
    if not force:
        ans = input("Confermi RESET completo (DB + RAG storage + utenti demo)? [y/N]: ").strip().lower()
        if ans not in ("y", "yes"):
            _echo("[yellow]Operazione annullata.[/yellow]")
            return 1

    from app.extensions import db
    try:
        from app.addons.rag.service import rag_root  # type: ignore
    except Exception:
        rag_root = None  # type: ignore
    from app.services.pages_service import read_pages, write_pages

    app = _get_app()
    removed_files = 0
    removed_dirs = 0

    with app.app_context():
        os.makedirs(app.instance_path, exist_ok=True)

        # Close active connections before deleting sqlite files.
        try:
            db.session.remove()
        except Exception:
            pass
        try:
            db.engine.dispose()
        except Exception:
            pass

        db_uri = str(app.config.get("SQLALCHEMY_DATABASE_URI", "")).strip()
        is_sqlite = db_uri.startswith("sqlite:///")

        if not is_sqlite:
            # Non-sqlite backends: reset via schema drop/create.
            try:
                db.drop_all()
                db.session.commit()
            except Exception as ex:
                _log_warn(f"Drop schema non riuscito: {ex}")
                db.session.rollback()

        # Remove SQLite files linked to current config (if any).
        for path in _sqlite_files_from_uri(db_uri):
            try:
                if path.exists() and path.is_file():
                    path.unlink()
                    removed_files += 1
            except Exception as ex:
                _log_warn(f"Impossibile rimuovere DB file {path}: {ex}")

        # Remove extra local sqlite artifacts under instance/ to guarantee clean reset.
        instance_dir = Path(app.instance_path)
        for pattern in ("*.db", "*.sqlite", "*.sqlite3", "*.db-wal", "*.db-shm", "*.sqlite-wal", "*.sqlite-shm"):
            for path in instance_dir.glob(pattern):
                try:
                    if path.exists() and path.is_file():
                        path.unlink()
                        removed_files += 1
                except Exception as ex:
                    _log_warn(f"Impossibile rimuovere file runtime {path}: {ex}")

        # Remove common runtime artifacts/log files.
        for path in (
            instance_dir / "app.log",
            instance_dir / "app.log.1",
            instance_dir / "flask-limiter.db",
            instance_dir / "flask-limiter.db-wal",
            instance_dir / "flask-limiter.db-shm",
        ):
            try:
                if path.exists() and path.is_file():
                    path.unlink()
                    removed_files += 1
            except Exception as ex:
                _log_warn(f"Impossibile rimuovere artifact {path}: {ex}")

        # Remove local RAG persisted data (documents + Qdrant index).
        try:
            rr = rag_root()
            if rr.exists() and rr.is_dir():
                shutil.rmtree(rr)
                removed_dirs += 1
        except Exception as ex:
            _log_warn(f"Impossibile rimuovere RAG storage: {ex}")

        # Recreate minimal runtime folders.
        os.makedirs(app.instance_path, exist_ok=True)
        try:
            rag_root().mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        # Recreate schema.
        db.create_all()
        db.session.commit()

        # Ensure maintenance mode is disabled after a full reset.
        try:
            pages = read_pages()
            services = pages.get("services", {}) or pages.get("features", {}) or {}
            slot = services.get("maintenance_mode")
            if not isinstance(slot, dict):
                slot = {
                    "enabled": False,
                    "label": "Modalità Manutenzione",
                    "icon": "cone-striped",
                    "description": "Blocca l'accesso non-admin mostrando la pagina di manutenzione",
                }
            slot["enabled"] = False
            services["maintenance_mode"] = slot
            pages["services"] = services
            pages["features"] = {k: dict(v or {}) for k, v in services.items()}
            write_pages(pages)
            _log_info("Maintenance disattivata dal reset.")
        except Exception as ex:
            _log_warn(f"Impossibile aggiornare maintenance mode nel runtime store: {ex}")

    _echo(f"[green]PASS Reset storage completato.[/green] files={removed_files} dirs={removed_dirs}")
    return cmd_init_users()


def cmd_maintenance(action: str = "status") -> int:
    """Gestione maintenance mode sul runtime store: status|on|off|toggle."""
    from app.services.pages_service import read_pages, write_pages

    op = str(action or "status").strip().lower()
    valid = {"status", "on", "off", "toggle"}
    if op not in valid:
        _log_err(f"Azione non valida: {action}. Usa: status, on, off, toggle")
        return 2

    app = _get_app()
    with app.app_context():
        data = read_pages()
        services = data.get("services", {}) or data.get("features", {}) or {}
        slot = services.get("maintenance_mode")
        if not isinstance(slot, dict):
            slot = {
                "enabled": False,
                "label": "Modalità Manutenzione",
                "icon": "cone-striped",
                "description": "Blocca l'accesso non-admin mostrando la pagina di manutenzione",
            }

        current = bool(slot.get("enabled", False))
        if op == "status":
            _echo(f"[bold]Maintenance:[/bold] {'[green]ON[/green]' if current else '[red]OFF[/red]'}")
            return 0
        if op == "toggle":
            target = not current
        else:
            target = op == "on"

        slot["enabled"] = bool(target)
        services["maintenance_mode"] = slot
        data["services"] = services
        data["features"] = {k: dict(v or {}) for k, v in services.items()}
        write_pages(data)
        _log_ok(f"Maintenance {'abilitata' if target else 'disabilitata'} ({'ON' if target else 'OFF'})")
        return 0


def cmd_init_users() -> int:
    """Crea tabelle e popola il DB dal seed JSON."""
    from app.extensions import db

    _ensure_postgres_db()
    app = _get_app()
    with app.app_context():
        try:
            _echo(f"[dim]DB in uso: {_mask_db_uri(str(app.config.get('SQLALCHEMY_DATABASE_URI','')))}[/dim]")
            os.makedirs(app.instance_path, exist_ok=True)
            db.create_all()

            try:
                from app.services.seed_service import seed_runtime_settings, seed_users

                seed_runtime_settings()
                _echo("[green]  PASS Runtime platform seed applicato (settings + addons + queue + broadcasts)[/green]")
            except Exception as e:
                _log_warn(f"Runtime settings seed skipped: {e}")

            _echo("[cyan]Seeding utenti demo dal file seed/seed.json...[/cyan]")
            try:
                results = seed_users()
            except Exception as e:
                _log_err(f"Seed utenti fallito: {e}")
                return 2

            for row in results:
                if row.get("created"):
                    _echo(f"[green]  PASS Creato:[/green] {row['email']} ({row['role']})")
                    if row.get("api_token"):
                        _echo(f"[yellow]    API key:[/yellow] {row['api_token']}")
                else:
                    _echo(f"[green]  PASS Aggiornato:[/green] {row['email']} ({row['role']})")

            return 0
        finally:
            with contextlib.suppress(Exception):
                db.session.remove()
            with contextlib.suppress(Exception):
                db.engine.dispose()


def cmd_create_user(role: str = "user") -> int:
    from app.extensions import db
    from app.models import ApiToken, User

    app = _get_app()
    with app.app_context():
        email = input("Email: ").strip().lower()
        name = input("Nome: ").strip() or email.split("@")[0]
        pwd1 = getpass.getpass("Password (min 8 char): ")
        if len(pwd1) < 8:
            _echo("[red]FAIL Password troppo corta.[/red]")
            return 2
        pwd2 = getpass.getpass("Conferma: ")
        if pwd1 != pwd2:
            _echo("[red]FAIL Le password non coincidono.[/red]")
            return 2
        if User.query.filter_by(email=email).first():
            _echo(f"[red]FAIL Utente già esistente:[/red] {email}")
            return 1
        u = User(
            email=email,
            name=name,
            role=role,
            is_active=True,
            email_verified=True,
            account_status="active",
            signup_source="admin_cli",
        )
        u.set_password(pwd1)
        db.session.add(u)
        db.session.commit()
        tok, raw = ApiToken.create(user_id=u.id, name=f"cli-{role}", expires_at=None)
        db.session.add(tok)
        db.session.commit()
        _echo(f"[green]PASS Utente creato:[/green] {email} (ruolo: {role})")
        _echo(f"[bold yellow]API key (salvala ora): {raw}[/bold yellow]")
        return 0


def cmd_create_api_token(email: str, name: str, days: int) -> int:
    from app.extensions import db
    from app.models import ApiToken, User

    app = _get_app()
    with app.app_context():
        usr = User.query.filter_by(email=(email or "").strip().lower()).first()
        if not usr:
            _echo(f"[red]FAIL Utente non trovato:[/red] {email}")
            return 1
        expires_at = None
        if days > 0:
            expires_at = _now_utc() + timedelta(days=days)
        tok, raw = ApiToken.create(user_id=usr.id, name=name or "cli-token", expires_at=expires_at)
        db.session.add(tok)
        db.session.commit()
        _echo(f"[green]PASS Token API creato[/green] per {usr.email}")
        _echo(f"  name={tok.name}  prefix={tok.token_prefix}  expires_at={tok.expires_at or 'never'}")
        _echo(f"[bold yellow]{raw}[/bold yellow]")
        _echo("[yellow]Salva ora il token: non sarà più visibile in chiaro.[/yellow]")
        return 0


def cmd_fill_logger(events: int = 500) -> int:
    from app.extensions import db
    from app.models import LogEvent
    from app.services.audit import audit

    if events < 1:
        _echo("[red]FAIL events deve essere >= 1[/red]")
        return 2

    app = _get_app()
    app_log = logging.getLogger("fill.logger")
    levels = ("DEBUG", "INFO", "WARNING", "ERROR")

    with app.app_context():
        try:
            before = LogEvent.query.count()
            _echo(f"[cyan]Generazione eventi logger...[/cyan] target={events}")
            for i in range(events):
                lv = levels[i % len(levels)]
                event_type = f"fill.{lv.lower()}"
                msg = f"Fill logger event #{i + 1}/{events}"
                ctx = {
                    "seq": i + 1,
                    "batch_total": events,
                    "source": "cli.fill-logger",
                    "level_index": i % len(levels),
                }
                audit(event_type, msg, level=lv, context=ctx)
                app_log.log(getattr(logging, lv, logging.INFO), f"{event_type} | {msg}", extra={"context": ctx})

            after = LogEvent.query.count()
            _echo(f"[green]PASS Logger riempito[/green] eventi_db_aggiunti={max(0, after - before)}")
            return 0
        finally:
            with contextlib.suppress(Exception):
                db.session.remove()
            with contextlib.suppress(Exception):
                db.engine.dispose()


def cmd_chat_check(message: str, role: str = "user") -> int:
    try:
        from app.addons.chat.service import ChatServiceError, generate_chat_reply  # type: ignore
    except Exception:
        _echo("[yellow]chat-check disabilitato: modulo chat non presente[/yellow]")
        return 2

    msg = (message or "").strip()
    if not msg:
        _echo("[red]FAIL Messaggio vuoto[/red]")
        return 2

    app = _get_app()
    with app.app_context():
        _echo("[cyan]Chat check[/cyan] provider=%s model=%s" % (app.config.get("CHAT_PROVIDER"), app.config.get("CHAT_MODEL")))
        try:
            out = generate_chat_reply(msg, role_hint=role)
        except ChatServiceError as ex:
            _echo(f"[red]FAIL Chat provider error:[/red] {ex}")
            return 3
        except Exception as ex:
            _echo(f"[red]FAIL Errore chat check:[/red] {ex}")
            return 3

        _echo("[green]PASS Risposta ricevuta[/green]")
        _echo(f"  elapsed_ms={out.elapsed_ms}  total_tokens={out.total_tokens}  tok_per_sec={out.tokens_per_sec}")
        if out.thinking:
            _echo("[yellow]-- thinking --[/yellow]")
            _echo(out.thinking[:600] + ("..." if len(out.thinking) > 600 else ""))
        _echo("[bold]-- reply --[/bold]")
        _echo(out.reply)
        return 0


def cmd_addons_list(json_output: bool = False, verbose: bool = False) -> int:
    app = _get_app()
    with app.app_context():
        addons_state = app.extensions.get("addons", {}) if hasattr(app, "extensions") else {}
        registry = app.extensions.get("addon_registry", {}) if hasattr(app, "extensions") else {}
        discovered = list(addons_state.get("discovered", []) or [])
        enabled_runtime = set(addons_state.get("enabled", []) or [])
        loaded = dict(addons_state.get("loaded", {}) or {})
        failed = dict(addons_state.get("failed", {}) or {})

        cfg = app.config.get("ADDONS", {}) or {}
        cfg_enabled = cfg.get("ENABLED", [])
        cfg_disabled = cfg.get("DISABLED", [])
        cfg_enabled = [str(x).strip() for x in cfg_enabled] if isinstance(cfg_enabled, list) else []
        cfg_disabled = [str(x).strip() for x in cfg_disabled] if isinstance(cfg_disabled, list) else []

        names = sorted(set(discovered) | set(cfg_enabled) | set(cfg_disabled))
        rows = []
        healthy = 0
        unhealthy = 0

        page_map = app.extensions.get("page_endpoint_map", {}) if hasattr(app, "extensions") else {}
        for name in names:
            meta = loaded.get(name) or registry.get(name) or {}
            mode = str(meta.get("mode", "-"))
            in_discovered = name in discovered
            in_enabled = name in enabled_runtime
            is_loaded = name in loaded
            err = str(failed.get(name, "") or meta.get("error", "")).strip()

            if is_loaded:
                status = "loaded"
            elif err:
                status = "failed"
            elif not in_discovered:
                status = "missing"
            elif not in_enabled:
                status = "disabled"
            else:
                status = "unknown"

            endpoint_keys = list(meta.get("page_endpoints", []) or [])
            missing_endpoints = [ep for ep in endpoint_keys if ep not in page_map]

            if status == "loaded" and not err and not missing_endpoints:
                health = "healthy"
                healthy += 1
            else:
                health = "unhealthy" if status in ("failed", "missing", "unknown") or missing_endpoints else "degraded"
                unhealthy += 1

            rows.append(
                {
                    "addon": name,
                    "status": status,
                    "health": health,
                    "mode": mode,
                    "version": str(meta.get("version", "-")),
                    "routes": len(meta.get("routes", []) or []),
                    "api_routes": len(meta.get("api_routes", []) or []),
                    "jobs": len(meta.get("jobs", []) or []),
                    "missing_endpoints": missing_endpoints,
                    "error": err,
                    "meta": meta,
                }
            )

        summary = {
            "app_version": str(app.config.get("APP_VERSION", "1.0.0")),
            "discovered": discovered,
            "enabled_runtime": sorted(enabled_runtime),
            "failed": failed,
            "counts": {
                "total": len(rows),
                "healthy": healthy,
                "unhealthy_or_degraded": unhealthy,
            },
            "addons": rows,
        }

        if json_output:
            _echo(json.dumps(summary, indent=2, ensure_ascii=False))
            return 0 if not failed else 2

        table = Table(title=f"Add-ons Registry (app v{summary['app_version']})")
        table.add_column("Addon")
        table.add_column("Status")
        table.add_column("Health")
        table.add_column("Mode")
        table.add_column("Version")
        table.add_column("Routes", justify="right")
        table.add_column("API", justify="right")
        table.add_column("Jobs", justify="right")
        for row in rows:
            status_color = "green" if row["status"] == "loaded" else ("yellow" if row["status"] == "disabled" else "red")
            health_color = "green" if row["health"] == "healthy" else ("yellow" if row["health"] == "degraded" else "red")
            table.add_row(
                row["addon"],
                f"[{status_color}]{row['status']}[/{status_color}]",
                f"[{health_color}]{row['health']}[/{health_color}]",
                row["mode"],
                row["version"],
                str(row["routes"]),
                str(row["api_routes"]),
                str(row["jobs"]),
            )
        _CONSOLE.print(table)
        _echo(f"Discovered={len(discovered)} EnabledRuntime={len(enabled_runtime)} Failed={len(failed)}")
        if verbose:
            for row in rows:
                if row["missing_endpoints"]:
                    _echo(f"[yellow]{row['addon']} missing endpoint mappings: {', '.join(row['missing_endpoints'])}[/yellow]")
                if row["error"]:
                    _echo(f"[red]{row['addon']} error: {row['error']}[/red]")
        return 0 if not failed else 2


def cmd_serve(host: str, port: int, debug: bool = False) -> int:
    if debug:
        return cmd_simple_debug(host=host, port=port, debug=True)
    if shutil.which("gunicorn"):
        workers = int(os.getenv("GUNICORN_WORKERS", os.getenv("WEB_WORKERS", "2")) or 2)
        threads = int(os.getenv("GUNICORN_THREADS", "4") or 4)
        reload_enabled = str(os.getenv("GUNICORN_RELOAD", "false")).strip().lower() in {"1", "true", "yes", "on"}
        return cmd_debug(host=host, port=port, web_workers=workers, threads=threads, reload_enabled=reload_enabled)
    _log_warn("gunicorn non trovato: fallback al Flask dev server, non adatto a carico multiutente.")
    return cmd_simple_debug(host=host, port=port, debug=False)


def _build_gunicorn_cmd(host: str, port: int, web_workers: int, threads: int, reload_enabled: bool) -> list[str]:
    cmd = [
        "gunicorn",
        "-w",
        str(max(1, int(web_workers))),
        "-k",
        "gthread",
        "--threads",
        str(max(1, int(threads))),
        "-b",
        f"{host}:{int(port)}",
        "wsgi:app",
    ]
    if reload_enabled:
        cmd.append("--reload")
    timeout = str(os.getenv("GUNICORN_TIMEOUT", "120")).strip()
    if timeout.isdigit():
        cmd.extend(["--timeout", timeout])
    return cmd


def _is_bind_available(host: str, port: int) -> bool:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        # In ambienti sandboxed il probe socket può essere vietato.
        # Non blocchiamo l'avvio: la validazione reale resta a Gunicorn.
        return True
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, int(port)))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _terminate_proc(name: str, proc: subprocess.Popen, timeout_sec: float = 5.0) -> None:
    if proc.poll() is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
    except Exception:
        pgid = None
    try:
        if pgid is not None:
            os.killpg(pgid, signal.SIGTERM)
        else:
            proc.terminate()
    except Exception:
        pass

    deadline = time.time() + max(0.5, float(timeout_sec))
    while time.time() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(0.1)

    try:
        if pgid is not None:
            os.killpg(pgid, signal.SIGKILL)
        else:
            proc.kill()
    except Exception:
        pass


def cmd_simple_debug(host: str, port: int, debug: bool = True) -> int:
    from app.extensions import db
    from app.services.job_service import init_job_runtime

    os.environ["JOB_RUNTIME_MODE"] = "web"
    os.environ["JOB_PROCESS_ROLE"] = "web"
    os.environ["WEBAPP_RUNTIME_KIND"] = "simple_debug"
    os.environ["WEBAPP_RESTART_COMMAND"] = _json.dumps(
        [sys.executable, str(Path(__file__).resolve()), "serve", "--host", str(host), "--port", str(int(port))]
        + (["--debug"] if debug else [])
    )
    app = _get_app()
    with app.app_context():
        init_job_runtime(app)
    _log_info(f"Simple debug (Flask dev server) host={host} port={port} debug={debug}")
    if host == "0.0.0.0":
        _log_warn("Rete LAN: usa http://<IP-LOCALE-PC>:%d" % port)
    try:
        app.run(host=host, port=port, debug=debug, threaded=True)
        return 0
    except OSError as ex:
        _log_err(f"Impossibile avviare il server: {ex}")
        return 2
    except Exception as ex:
        _log_err(f"Errore avvio server: {ex}")
        return 3
    finally:
        with contextlib.suppress(Exception):
            db.session.remove()
        with contextlib.suppress(Exception):
            db.engine.dispose()


def cmd_debug(host: str, port: int, web_workers: int = 2, threads: int = 2, reload_enabled: bool = True) -> int:
    if not shutil.which("gunicorn"):
        _log_err("gunicorn non trovato. Installa dipendenze: pip install -r requirements.txt")
        return 2
    if not _is_bind_available(host, port):
        _log_err(f"Porta già in uso su {host}:{port}. Ferma il processo esistente o cambia --port.")
        return 2
    env = os.environ.copy()
    env["JOB_RUNTIME_MODE"] = "web"
    env["JOB_PROCESS_ROLE"] = "web"
    env["WEBAPP_RUNTIME_KIND"] = "gunicorn"
    cmd = _build_gunicorn_cmd(host=host, port=port, web_workers=web_workers, threads=threads, reload_enabled=reload_enabled)
    _log_info(f"Debug (Gunicorn web only): {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd, env=env, check=False)
        return int(proc.returncode)
    except KeyboardInterrupt:
        _log_warn("Interrotto da tastiera.")
        return 130
    except Exception as ex:
        _log_err(f"Errore avvio Gunicorn: {ex}")
        return 3


def cmd_serve_stack(
    host: str,
    port: int,
    web_workers: int = 4,
    threads: int = 2,
    job_workers: int = 1,
    worker_poll: float = 1.0,
    reload_enabled: bool = False,
) -> int:
    if not shutil.which("gunicorn"):
        _log_err("gunicorn non trovato. Installa dipendenze: pip install -r requirements.txt")
        return 2
    if int(job_workers) < 1:
        _log_err("job_workers deve essere >= 1")
        return 2
    if not _is_bind_available(host, port):
        _log_err(f"Porta già in uso su {host}:{port}. Ferma il processo esistente o cambia --port.")
        return 2

    env_web = os.environ.copy()
    env_web["JOB_RUNTIME_MODE"] = "web"
    env_web["JOB_PROCESS_ROLE"] = "web"
    env_worker = os.environ.copy()
    env_worker["JOB_RUNTIME_MODE"] = "worker"
    env_worker["JOB_PROCESS_ROLE"] = "worker"

    web_cmd = _build_gunicorn_cmd(host=host, port=port, web_workers=web_workers, threads=threads, reload_enabled=reload_enabled)
    worker_cmd = [sys.executable, str(Path(__file__).resolve()), "worker", "--poll", str(max(0.2, float(worker_poll)))]

    _log_info(f"Serve stack: web + {job_workers} worker")
    _log_info(f"web: {' '.join(web_cmd)}")
    _log_info(f"worker: {' '.join(worker_cmd)}")

    procs: list[tuple[str, subprocess.Popen]] = []
    try:
        web_proc = subprocess.Popen(web_cmd, env=env_web, start_new_session=True)
        procs.append(("web", web_proc))

        # Fail fast if Gunicorn exits immediately (e.g. bind conflict, config error).
        time.sleep(0.6)
        web_rc = web_proc.poll()
        if web_rc is not None:
            _log_err(f"Web process terminato subito con codice {web_rc}. Stack non avviato.")
            return int(web_rc)

        for i in range(int(job_workers)):
            wp = subprocess.Popen(worker_cmd, env=env_worker, start_new_session=True)
            procs.append((f"worker-{i+1}", wp))

        while True:
            for name, proc in procs:
                rc = proc.poll()
                if rc is not None:
                    _log_warn(f"{name} terminato con codice {rc}. Arresto stack...")
                    for _, p in procs:
                        _terminate_proc(name=name, proc=p, timeout_sec=3.0)
                    return int(rc)
            time.sleep(0.5)
    except KeyboardInterrupt:
        _log_warn("Arresto stack richiesto da tastiera.")
        for name, proc in procs:
            _terminate_proc(name=name, proc=proc, timeout_sec=5.0)
        return 130
    except Exception as ex:
        _log_err(f"Errore avvio stack: {ex}")
        for name, proc in procs:
            _terminate_proc(name=name, proc=proc, timeout_sec=1.0)
        return 3


def cmd_worker(poll_sec: float = 1.0) -> int:
    """Run dedicated async worker process for Job & Queue runtime."""
    from app.services.job_service import init_job_runtime

    os.environ["JOB_RUNTIME_MODE"] = "worker"
    os.environ["JOB_PROCESS_ROLE"] = "worker"
    app = _get_app()
    with app.app_context():
        init_job_runtime(app)
        mode = str(app.config.get("JOB_RUNTIME_MODE", "hybrid"))
        role = str(app.config.get("JOB_PROCESS_ROLE", "web"))
        backend = str(app.config.get("JOB_QUEUE_BACKEND", "db"))
        redis_url = str(app.config.get("JOB_QUEUE_REDIS_URL", ""))
        redis_hint = f" redis={redis_url}" if backend == "redis" and redis_url else ""
        _log_info(
            f"Worker avviato | mode={mode} role={role} backend={backend}{redis_hint} poll={app.config.get('JOB_RUNTIME_POLL_SEC', 0.2)}s"
        )
        _log_info("Premi Ctrl+C per fermare il worker.")
        try:
            while True:
                time.sleep(max(0.2, float(poll_sec)))
        except KeyboardInterrupt:
            _log_warn("Worker interrotto da tastiera.")
            return 0


def cmd_settings_export(out_path: str) -> int:
    """Export DB-backed runtime settings to a JSON file."""
    out = Path(out_path)
    if out.is_dir():
        out = out / "settings_export.json"
    app = _get_app()
    with app.app_context():
        from app.extensions import db
        from app.services.app_settings_service import build_settings_export_payload, get_app_settings_raw

        row = get_app_settings_raw()
        payload = build_settings_export_payload(row)
        row.last_exported_at = _now_utc()
        db.session.add(row)
        db.session.commit()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        _log_ok(f"Settings exported to: {out}")
        return 0


def cmd_settings_import(in_path: str) -> int:
    """Import runtime settings JSON into DB and apply it immediately."""
    p = Path(in_path)
    if not p.exists() or not p.is_file():
        _log_err(f"File not found: {in_path}")
        return 2
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as ex:
        _log_err(f"Invalid JSON: {ex}")
        return 3

    app = _get_app()
    with app.app_context():
        from app.extensions import db
        from app.services.app_settings_service import get_app_settings_raw, import_settings_payload, update_settings

        try:
            cfg, theme, visual = import_settings_payload(data if isinstance(data, dict) else {})
        except Exception as ex:
            _log_err(f"Invalid settings payload: {ex}")
            return 4

        update_settings(config=cfg, theme=theme, visual=visual)
        row = get_app_settings_raw()
        row.last_imported_at = _now_utc()
        db.session.add(row)
        db.session.commit()
        _log_ok("Settings imported and applied.")
        return 0


def cmd_serve_api(host: str, port: int) -> int:
    """Compatibility command kept for older instructions."""
    _log_warn("A separate FastAPI server is no longer required.")
    _log_info("FastAPI is exposed by the main WebApp server under /api.")
    _log_info("Use: python cli.py serve")
    _log_info("Then open the API at: http://127.0.0.1:5000/api")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python cli.py",
        description="FlaskBase CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"FlaskBase CLI {_cli_version()}")
    sub = parser.add_subparsers(dest="cmd", required=False, metavar="command")

    p_reset = sub.add_parser("reset-db", help="Drop + create di tutte le tabelle")
    p_reset.add_argument("--force", action="store_true", help="Non chiedere conferma")
    p_reset.set_defaults(func=lambda a: cmd_reset_db(force=bool(a.force)))

    p_init_complete = sub.add_parser("init-db-complete", help="Reset DB + seed utenti demo (admin/user)")
    p_init_complete.add_argument("--force", action="store_true", help="Non chiedere conferma")
    p_init_complete.set_defaults(func=lambda a: cmd_init_db_complete(force=bool(a.force)))

    p_serve = sub.add_parser("serve", help="Avvia Flask dev server (web only)")
    p_serve.add_argument("--host", default=os.getenv("HOST", "0.0.0.0"), help="Host bind (default: 0.0.0.0)")
    p_serve.add_argument("--port", type=int, default=int(os.getenv("PORT", "5000")), help="Porta bind (default: 5000)")
    p_serve.add_argument("--debug", action="store_true", help="Abilita debug Flask")
    p_serve.set_defaults(func=lambda a: cmd_serve(host=a.host, port=int(a.port), debug=bool(a.debug)))

    p_api = sub.add_parser("serve-api", help="Compatibility alias: FastAPI is served from the main WebApp server")
    p_api.add_argument("--host", default=os.getenv("API_HOST", "0.0.0.0"), help="Host bind (default: 0.0.0.0)")
    p_api.add_argument("--port", type=int, default=int(os.getenv("API_PORT", "8000")), help="Porta bind (default: 8000)")
    p_api.set_defaults(func=lambda a: cmd_serve_api(host=a.host, port=int(a.port)))

    p_fill_logger = sub.add_parser("fill-logger", help="Genera eventi di log di esempio nel DB")
    p_fill_logger.add_argument("--events", type=int, default=500, help="Numero eventi da generare")
    p_fill_logger.set_defaults(func=lambda a: cmd_fill_logger(events=int(a.events)))

    p_exp = sub.add_parser("settings-export", help="Esporta le impostazioni (DB) in un file JSON")
    p_exp.add_argument("--out", default="settings_export.json", help="Percorso output (default: settings_export.json)")
    p_exp.set_defaults(func=lambda a: cmd_settings_export(out_path=str(a.out)))

    p_imp = sub.add_parser("settings-import", help="Importa un file JSON di impostazioni nel DB")
    p_imp.add_argument("--in", dest="in_path", required=True, help="Percorso JSON da importare")
    p_imp.set_defaults(func=lambda a: cmd_settings_import(in_path=str(a.in_path)))

    if len(sys.argv) == 1:
        _print_cli_help(parser)
        return 0

    args = parser.parse_args()
    if not hasattr(args, "func"):
        _print_cli_help(parser)
        return 0
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())


def cmd_addons_install(zip_path: str, overwrite: bool = False) -> int:
    from app.services.addon_installer import install_addon_zip

    p = Path(zip_path)
    if not p.exists() or not p.is_file():
        _echo(f"[red]FAIL File non trovato:[/red] {zip_path}")
        return 2
    data = p.read_bytes()
    try:
        res = install_addon_zip(data, overwrite=overwrite)
    except FileExistsError as ex:
        _echo(f"[yellow]SKIP[/yellow] {ex}")
        return 1
    except Exception as ex:
        _echo(f"[red]FAIL Installazione add-on:[/red] {ex}")
        return 3
    _echo("[green]PASS Add-on installato[/green]")
    _echo(f"  id={res.get('installed')} path={res.get('path')}")
    return 0
