"""Start the loopback-only trial workbench or check its replay catalog."""

import argparse
from http import HTTPStatus
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import logging
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from .scenarios import ROOT
from .workbench import Workbench


WEB = Path(__file__).with_name("web")
ASSETS = {"/": ("index.html", "text/html"), "/style.css": ("style.css", "text/css"),
          "/calendar.js": ("calendar.js", "text/javascript"), "/app.js": ("app.js", "text/javascript")}
TASK = re.compile(r"/api/tasks/([\da-f]{32})(?:/([a-z]+))?")


def _checked_task(app, owner, task, failures):
    if task["state"] != "COMPLETED":
        failures.append({"case": task["scenario"]["id"], "phase": task["phase"], "reason": task["state"]})
        return False
    return True


def check(use_archive):
    with TemporaryDirectory(prefix="threadplan-replay-") as directory:
        app = Workbench(Path(directory), use_archive=use_archive)
        owner = app.principal(app.create_session())
        failures = []
        passed = 0
        branches = 0
        outcomes = {}
        for case in app.cases.values():
            try:
                task = app.create_task(owner, case.id)
                if not _checked_task(app, owner, task, failures):
                    continue
                for choice in task["choices"]:
                    branch = app.create_task(owner, case.id)
                    branch = app.clarify(owner, branch["id"], choice)
                    branches += int(_checked_task(app, owner, branch, failures))
                for item in task["items"]:
                    if "operation" in item:
                        command = app.prepare(owner, task["id"], item["activity_ref"])
                        result = app.command(owner, task["id"], command["id"], "confirm")
                        expected = {"lost_ack": "OUTCOME_UNKNOWN", "readback": "VERIFY_PENDING"}.get(case.fault, "SUCCEEDED")
                        if result["state"] != expected:
                            failures.append({"case": case.id, "reason": "unexpected_execution_state",
                                             "observed": result["state"], "expected": expected})
                        outcomes[result["state"]] = outcomes.get(result["state"], 0) + 1
                passed += 1
            except (ValueError, RuntimeError) as error:
                failures.append({"case": case.id, "reason": str(error)})
        print(json.dumps({"catalog": app.catalog_info, "cases": len(app.cases), "completed": passed,
                          "clarified_branches": branches, "execution_states": outcomes,
                          "external_calls": 0, "failures": failures}, ensure_ascii=False, indent=2))
        return int(bool(failures))


class Handler(BaseHTTPRequestHandler):
    timeout = 10

    def log_message(self, format, *args):
        pass

    @property
    def app(self):
        return self.server.workbench

    @property
    def host(self):
        return f"127.0.0.1:{self.server.server_port}"

    def reply(self, status, mime, body, *, cookie=None):
        self.send_response(status)
        self.send_header("Content-Type", mime + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if cookie:
            self.send_header("Set-Cookie", f"threadplan_session={cookie}; Path=/; HttpOnly; SameSite=Strict")
        self.end_headers()
        self.wfile.write(body)

    def json(self, status, data):
        self.reply(status, "application/json", json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8"))

    def owner(self):
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
        except CookieError:
            return None
        token = cookies.get("threadplan_session")
        return self.app.principal(token.value) if token else None

    def do_GET(self):
        self.handle_request(False)

    def do_POST(self):
        self.handle_request(True)

    def handle_request(self, write):
        if self.headers.get("Host") != self.host or (write and self.headers.get("Origin") != f"http://{self.host}"):
            self.json(HTTPStatus.FORBIDDEN, {"error": "wrong_origin"})
            return
        path = urlsplit(self.path).path
        owner = self.owner()
        if not write and path in ASSETS:
            name, mime = ASSETS[path]
            token = self.app.create_session() if path == "/" and not owner else None
            self.reply(HTTPStatus.OK, mime, (WEB / name).read_bytes(), cookie=token)
            return
        if not owner:
            self.json(HTTPStatus.UNAUTHORIZED, {"error": "session_required"})
            return
        try:
            data = self.input() if write else None
            result = self.route(path, owner, write, data)
            self.json(HTTPStatus.OK, result)
        except LookupError:
            self.json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
        except ValueError as error:
            self.json(HTTPStatus.CONFLICT, {"error": str(error) if str(error).isascii() else "request_rejected"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            logging.exception("Local workbench request failed")
            self.json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "local_request_failed"})

    def input(self):
        if self.headers.get_content_type() != "application/json":
            raise ValueError("json_required")
        size = int(self.headers.get("Content-Length", 0))
        if not 1 <= size <= 16384:
            raise ValueError("invalid_body_size")
        value = json.loads(self.rfile.read(size))
        if not isinstance(value, dict):
            raise ValueError("invalid_body")
        return value

    def route(self, path, owner, write, data):
        if path == "/api/scenarios" and not write:
            return self.app.catalog()
        if path == "/api/tasks":
            return self.app.create_task(owner, self.field(data, "scenario")) if write else self.app.list_tasks(owner)
        if path == "/api/sandbox" and write:
            return self.app.start_sandbox(owner, data)
        match = TASK.fullmatch(path)
        if match:
            identifier, action = match.groups()
            if action is None and not write:
                return self.app.task_view(owner, identifier)
            if action == "followup" and write:
                return self.app.followup(owner, identifier, data)
            if write and action in {"prepare", "clarify", "restart", "confirm", "recover"}:
                if action == "prepare":
                    return self.app.prepare(owner, identifier, self.field(data, "activity"))
                if action == "clarify":
                    return self.app.clarify(owner, identifier, self.field(data, "choice"))
                if action == "restart":
                    return self.app.restart(owner, identifier)
                return self.app.command(owner, identifier, self.field(data, "command"), action)
        raise LookupError("route_not_found")

    @staticmethod
    def field(data, key):
        value = data.get(key)
        if not isinstance(value, str) or not 0 < len(value) <= 160:
            raise ValueError("invalid_" + key)
        return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--state-dir", type=Path, default=Path("data/demo-local"))
    parser.add_argument("--builtin-only", action="store_true")
    parser.add_argument("--live-sandbox", action="store_true")
    parser.add_argument("--open", action="store_true", help="open the loopback URL in the default browser")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        return check(not args.builtin_only)
    key = None
    if args.live_sandbox:
        from threadplan_host.model_config import openrouter_key
        key = openrouter_key(ROOT)
        if not key:
            parser.error("OPENROUTER_API_KEY is missing for the fixed free sandbox")
    with HTTPServer(("127.0.0.1", args.port), Handler) as server:
        server.workbench = Workbench(args.state_dir, use_archive=not args.builtin_only, live_key=key)
        url = f"http://127.0.0.1:{server.server_port}/"
        print(f"ThreadPlan · {url}", flush=True)
        if args.open:
            import webbrowser
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.workbench.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
