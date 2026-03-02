from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _load_env_fallback(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


class StressTestFailure(RuntimeError):
    pass


@dataclass
class CommandResult:
    code: int
    stdout: str
    stderr: str


class HttpSession:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.cookie_jar = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.cookie_jar))

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 15.0,
        allow_error: bool = False,
    ) -> tuple[int, str]:
        payload = None
        req_headers = {"User-Agent": "webapp-stress-test/1.0"}
        if headers:
            req_headers.update(headers)
        if data is not None:
            payload = urlencode({k: v for k, v in data.items() if v is not None}).encode("utf-8")
            req_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        request = Request(self.base_url + path, data=payload, headers=req_headers, method=method.upper())
        try:
            with self.opener.open(request, timeout=timeout) as response:
                return int(getattr(response, "status", 200)), response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if allow_error:
                return int(exc.code), body
            raise StressTestFailure(f"HTTP {exc.code} on {path}: {body[:400]}") from exc
        except URLError as exc:
            raise StressTestFailure(f"Network error on {path}: {exc}") from exc

    def extract_csrf(self, html: str) -> str:
        patterns = (
            r'name="csrf_token"\s+type="hidden"\s+value="([^"]+)"',
            r'name="csrf_token"\s+value="([^"]+)"',
            r'value="([^"]+)"\s+name="csrf_token"',
        )
        for pattern in patterns:
            match = re.search(pattern, html)
            if match:
                return match.group(1)
        raise StressTestFailure("CSRF token not found in HTML response.")

    def login(self, email: str, password: str) -> None:
        status, html = self.request("/auth/login")
        if status != 200:
            raise StressTestFailure(f"Login page not reachable for {email}.")
        csrf = self.extract_csrf(html)
        status, body = self.request(
            "/auth/login",
            method="POST",
            data={"csrf_token": csrf, "email": email, "password": password, "remember": "y"},
        )
        if status != 200 or "Dashboard" not in body:
            raise StressTestFailure(f"Login failed for {email}. Status={status}")


class StressRunner:
    def __init__(self) -> None:
        self.python = sys.executable
        self.cli = PROJECT_ROOT / "cli.py"
        self.base_url = os.getenv("STRESS_BASE_URL", "http://127.0.0.1:5055")
        self.host = os.getenv("STRESS_HOST", "127.0.0.1")
        self.port = int(os.getenv("STRESS_PORT", "5055"))
        self.log_events = int(os.getenv("STRESS_LOG_EVENTS", "120"))
        self.http_workers = int(os.getenv("STRESS_HTTP_WORKERS", "6"))
        self.http_loops = int(os.getenv("STRESS_HTTP_LOOPS", "12"))
        self.server_proc: subprocess.Popen[str] | None = None
        self.server_output: list[str] = []
        self._server_lock = threading.Lock()

    def _dispose_app(self, app: Any) -> None:
        try:
            from app.extensions import db
        except Exception:
            return
        with contextlib.suppress(Exception):
            with app.app_context():
                db.session.remove()
                db.engine.dispose()

    def run(self) -> int:
        self._preflight()
        self._run_cli("init-db-complete", "--force")
        self._prepare_runtime_for_stress()
        self._start_server()
        try:
            self._wait_for_server()
            self._http_auth_checks()
            self._settings_roundtrip()
            self._logger_fill_and_db_checks()
            self._http_stress()
            self._browser_checks()
        finally:
            self._stop_server()
        return 0

    def _prepare_runtime_for_stress(self) -> None:
        from app import create_app
        from app.services.app_settings_service import get_effective_settings, update_settings

        app = create_app({"TESTING": True})
        try:
            with app.app_context():
                config, theme, visual = get_effective_settings()
                settings = dict(config.get("SETTINGS") or {})
                security = dict(settings.get("SECURITY") or {})
                security["LOGIN_RATE_LIMIT"] = "500 per minute"
                settings["SECURITY"] = security
                config["SETTINGS"] = settings
                update_settings(config=config, theme=theme, visual=visual)
        finally:
            self._dispose_app(app)

    def _preflight(self) -> None:
        if not self.cli.exists():
            raise StressTestFailure("cli.py not found.")
        if not (os.getenv("TEST_DATABASE_URL") or os.getenv("DATABASE_URL")):
            try:
                from dotenv import load_dotenv
            except ModuleNotFoundError as exc:
                _load_env_fallback(PROJECT_ROOT / ".env")
            else:
                load_dotenv(PROJECT_ROOT / ".env")
        if not (os.getenv("TEST_DATABASE_URL") or os.getenv("DATABASE_URL")):
            raise StressTestFailure("TEST_DATABASE_URL or DATABASE_URL is required.")
        try:
            import flask  # noqa: F401
            import playwright.sync_api  # noqa: F401
        except ModuleNotFoundError as exc:
            raise StressTestFailure(
                f"Missing dependency '{exc.name}'. Install requirements and Playwright browsers before running tests/test_all.py."
            ) from exc
        self._check_database_reachable()

    def _check_database_reachable(self) -> None:
        database_url = os.getenv("TEST_DATABASE_URL") or os.getenv("DATABASE_URL")
        if not database_url:
            raise StressTestFailure("TEST_DATABASE_URL or DATABASE_URL is required.")
        engine = create_engine(database_url, pool_pre_ping=True, future=True)
        try:
            with engine.connect() as conn:
                conn.execute(text("select 1"))
        except SQLAlchemyError as exc:
            raise StressTestFailure(
                f"Database not reachable for stress test: {database_url}. Original error: {exc}"
            ) from exc
        finally:
            engine.dispose()

    def _run_cli(self, *args: str, timeout: float = 180.0) -> CommandResult:
        proc = subprocess.run(
            [self.python, str(self.cli), *args],
            cwd=str(PROJECT_ROOT),
            text=True,
            capture_output=True,
            timeout=timeout,
            env=dict(os.environ),
        )
        result = CommandResult(proc.returncode, proc.stdout, proc.stderr)
        if result.code != 0:
            raise StressTestFailure(
                f"CLI command failed: {' '.join(args)}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
            )
        return result

    def _start_server(self) -> None:
        proc = subprocess.Popen(
            [self.python, str(self.cli), "serve", "--host", self.host, "--port", str(self.port)],
            cwd=str(PROJECT_ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=dict(os.environ),
        )
        self.server_proc = proc
        assert proc.stdout is not None

        def _reader() -> None:
            for line in proc.stdout:
                with self._server_lock:
                    self.server_output.append(line.rstrip())
                    if len(self.server_output) > 400:
                        self.server_output.pop(0)

        threading.Thread(target=_reader, daemon=True).start()

    def _stop_server(self) -> None:
        if self.server_proc is None:
            return
        proc = self.server_proc
        try:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGINT)
                    proc.wait(timeout=8)
                except Exception:
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                        proc.wait(timeout=4)
                    except Exception:
                        pass
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except Exception:
                        pass
            if proc.stdout is not None:
                proc.stdout.close()
        finally:
            try:
                self.server_proc = None
            except Exception:
                pass

    def _wait_for_server(self, timeout: float = 45.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                session = HttpSession(self.base_url)
                status, body = session.request("/auth/login", allow_error=True)
                if status == 200 and "<form" in body:
                    return
            except Exception:
                pass
            if self.server_proc and self.server_proc.poll() is not None:
                output = "\n".join(self.server_output[-80:])
                raise StressTestFailure(f"Server exited early.\n{output}")
            time.sleep(0.5)
        output = "\n".join(self.server_output[-80:])
        raise StressTestFailure(f"Server did not become ready.\n{output}")

    def _new_session(self) -> HttpSession:
        return HttpSession(self.base_url)

    def _http_auth_checks(self) -> None:
        admin = self._new_session()
        admin.login("admin@test.com", "admin")
        user = self._new_session()
        user.login("user@test.com", "user")

        checks = [
            (admin, "/dashboard", 200, "Dashboard"),
            (admin, "/admin/settings", 200, 'data-page="admin-settings"'),
            (admin, "/hello", 200, 'data-addon-page="hello-world-user"'),
            (admin, "/admin/addons/hello_world", 200, 'data-addon-page="hello-world-admin"'),
            (user, "/dashboard", 200, "Dashboard"),
            (user, "/hello", 200, 'data-addon-page="hello-world-user"'),
        ]
        for session, path, expected_status, expected_text in checks:
            status, body = session.request(path, allow_error=True)
            if status != expected_status or expected_text not in body:
                hint = body[:220].replace("\n", " ")
                raise StressTestFailure(f"Unexpected response for {path}: status={status} body={hint}")

        status, body = user.request("/admin/addons/hello_world", allow_error=True)
        if status != 403:
            raise StressTestFailure(f"User should receive 403 on admin addon page, got {status}.")

    def _settings_roundtrip(self) -> None:
        admin = self._new_session()
        admin.login("admin@test.com", "admin")
        status, html = admin.request("/admin/settings")
        if status != 200:
            raise StressTestFailure("Admin settings page not reachable.")
        csrf = admin.extract_csrf(html)
        payload = {
            "csrf_token": csrf,
            "st_app_name": "WebApp",
            "st_app_version": "1.0.0",
            "st_base_url": "http://127.0.0.1:5000",
            "ad_hello_world_enabled": "1",
            "ad_widgets_ui_enabled": "1",
            "addon_cfg__hello_world__welcome_copy": "Hello from stress test",
        }
        status, body = admin.request("/admin/settings", method="POST", data=payload)
        success_markers = (
            "Settings saved.",
            'data-page="admin-settings"',
            "Hello from stress test",
        )
        if status != 200 or not all(marker in body for marker in success_markers[:2]):
            raise StressTestFailure("Settings roundtrip failed.")

    def _logger_fill_and_db_checks(self) -> None:
        self._run_cli("fill-logger", "--events", str(self.log_events))
        from app import create_app
        from app.models import AppSettings, LogEvent, User

        app = create_app({"TESTING": True})
        try:
            with app.app_context():
                from app.extensions import db

                if User.query.count() < 2:
                    raise StressTestFailure("Seed users missing from database.")
                row = db.session.get(AppSettings, 1)
                if row is None:
                    raise StressTestFailure("AppSettings row missing.")
                if row.app_name != "WebApp":
                    raise StressTestFailure("Unexpected app settings payload in DB.")
                if LogEvent.query.count() < self.log_events:
                    raise StressTestFailure("Logger filler did not populate enough DB events.")
        finally:
            self._dispose_app(app)
        log_file = PROJECT_ROOT / "instance" / "app.log"
        if not log_file.exists() or log_file.stat().st_size <= 0:
            raise StressTestFailure("Application log file was not written.")

    def _http_stress(self) -> None:
        def worker(role: str, idx: int) -> int:
            session = self._new_session()
            if role == "admin":
                session.login("admin@test.com", "admin")
                routes = ["/dashboard", "/messages", "/admin/settings", "/admin/logs", "/hello", "/admin/addons/widgets_ui"]
            else:
                session.login("user@test.com", "user")
                routes = ["/dashboard", "/messages", "/hello", "/widgets"]
            completed = 0
            for loop in range(self.http_loops):
                path = routes[loop % len(routes)]
                status, _ = session.request(path, allow_error=True)
                if role == "user" and path.startswith("/admin/"):
                    if status != 403:
                        raise StressTestFailure(f"Worker {idx}: expected 403 for user on {path}, got {status}")
                else:
                    if status != 200:
                        raise StressTestFailure(f"Worker {idx}: expected 200 on {path}, got {status}")
                completed += 1
            return completed

        total = 0
        with ThreadPoolExecutor(max_workers=self.http_workers) as executor:
            futures = []
            for idx in range(self.http_workers):
                role = "admin" if idx == 0 else "user"
                futures.append(executor.submit(worker, role, idx))
            for future in as_completed(futures):
                total += future.result()
        if total < self.http_workers * self.http_loops:
            raise StressTestFailure("HTTP stress did not complete all scheduled requests.")

    def _browser_checks(self) -> None:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                self._browser_admin_checks(browser)
                self._browser_user_checks(browser)
                self._browser_mobile_checks(browser)
            finally:
                browser.close()

    def _browser_login(self, page: Any, email: str, password: str) -> None:
        page.goto(self.base_url + "/auth/login?force=1", wait_until="domcontentloaded")
        page.locator('input[name="email"]').wait_for(state="visible")
        page.fill('input[name="email"]', email)
        page.fill('input[name="password"]', password)
        submit = page.locator('form[action$="/auth/login"] button[type="submit"]')
        with page.expect_navigation(wait_until="domcontentloaded"):
            submit.click()
        page.wait_for_load_state("networkidle")
        if "dashboard" not in page.url:
            body = page.content()
            flash_match = re.search(r"(Bentornato|Email o password non validi|Dati non validi|Conferma la tua email)", body)
            hint = flash_match.group(1) if flash_match else body[:220].replace("\n", " ")
            raise StressTestFailure(f"Browser login failed for {email}: {page.url} | {hint}")

    def _browser_admin_checks(self, browser: Any) -> None:
        page = browser.new_page(viewport={"width": 1440, "height": 1080})
        try:
            self._browser_login(page, "admin@test.com", "admin")
            if not page.locator("#sidebar").is_visible():
                raise StressTestFailure("Desktop sidebar is not visible for admin.")
            page.goto(self.base_url + "/hello", wait_until="networkidle")
            if page.locator('[data-addon-page="hello-world-user"]').count() == 0:
                raise StressTestFailure("Admin cannot see addon user view in browser.")
            page.goto(self.base_url + "/admin/addons/hello_world", wait_until="networkidle")
            if page.locator('[data-addon-page="hello-world-admin"]').count() == 0:
                raise StressTestFailure("Admin cannot see addon admin view in browser.")
            page.goto(self.base_url + "/admin/logs", wait_until="networkidle")
            page.locator('form[action="/admin/logs/fill"] input[name="events"]').fill("40")
            page.locator('form[action="/admin/logs/fill"] button[type="submit"]').click()
            if page.locator("#confirmActionModal.show").count():
                page.locator("#confirmActionOkBtn").click()
            page.wait_for_load_state("networkidle")
            if "Generati 40 eventi di log" not in page.content():
                raise StressTestFailure("Admin log filler form failed in browser.")
        finally:
            page.close()

    def _browser_user_checks(self, browser: Any) -> None:
        page = browser.new_page(viewport={"width": 1366, "height": 900})
        try:
            self._browser_login(page, "user@test.com", "user")
            page.goto(self.base_url + "/hello", wait_until="networkidle")
            if page.locator('[data-addon-page="hello-world-user"]').count() == 0:
                raise StressTestFailure("User addon view missing in browser.")
            response = page.goto(self.base_url + "/admin/addons/hello_world", wait_until="networkidle")
            if response is None or response.status != 403:
                raise StressTestFailure("User should receive 403 for admin addon browser page.")
        finally:
            page.close()

    def _browser_mobile_checks(self, browser: Any) -> None:
        page = browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=True)
        try:
            self._browser_login(page, "admin@test.com", "admin")
            page.goto(self.base_url + "/dashboard", wait_until="networkidle")
            if not page.locator("#mobileTopnav").is_visible():
                raise StressTestFailure("Mobile topnav is not visible.")
            if page.locator("#sidebar").is_visible():
                raise StressTestFailure("Desktop sidebar should not be visible on mobile.")
        finally:
            page.close()


def run_full_stress_test() -> None:
    runner = StressRunner()
    code = runner.run()
    if code != 0:
        raise StressTestFailure(f"Stress test failed with exit code {code}")


def test_full_stack_stress() -> None:
    run_full_stress_test()


if __name__ == "__main__":
    try:
        run_full_stress_test()
    except StressTestFailure as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
    raise SystemExit(0)
