"""E2E chat test — sends a message to project 2071's ChatGPT session and
streams the run until done. Usage: python chat_test.py "MESSAGE" [--timeout 900]
"""
import json
import sys
import time
import os
import urllib.request
import urllib.error

TOKEN = os.environ["DREAMAGENT_TOKEN"]
BASE = "https://api.dreamagent.cloud"
PROJECT = 2071

def api(method, path, body=None, timeout=60):
    req = urllib.request.Request(
        BASE + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def get_or_create_session():
    sessions = api("GET", f"/projects/{PROJECT}/sessions")
    ours = [s for s in sessions
            if not s.get("archived") and (s.get("label") or "").lower() == "chatgpt"]
    if ours:
        return ours[0]["session_key"]
    s = api("POST", f"/projects/{PROJECT}/sessions",
            {"label": "ChatGPT", "project_id": PROJECT})
    return s["session_key"]

def run_chat(message, timeout_s):
    session_key = get_or_create_session()
    print(f"[session] {session_key}", flush=True)
    payload = {
        "session_key": session_key,
        "messages": [{"role": "user", "content": message}],
        "stream": True, "acp_mode": True, "mode": "dream",
    }
    req = urllib.request.Request(
        BASE + "/chat/stream", method="POST",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    tail = []
    start = time.time()
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if not chunk or chunk == "[DONE]":
                continue
            try:
                d = json.loads(chunk)
            except Exception:
                continue
            # capture meaningful text
            for key in ("content", "text", "delta", "message"):
                v = d.get(key)
                if isinstance(v, str) and v.strip():
                    tail.append(v.strip())
                    if len(tail) > 12:
                        tail.pop(0)
            if d.get("done") or d.get("status") in ("completed", "done", "finished"):
                pass
            elapsed = time.time() - start
            print(f"  [{elapsed:5.0f}s] {chunk[:160]}", flush=True)
            if time.time() - start > timeout_s:
                print("TIMEOUT", flush=True)
                break
    print("── last text ──")
    print(" ".join(tail)[-1500:])

if __name__ == "__main__":
    msg = sys.argv[1]
    tmo = 900
    if "--timeout" in sys.argv:
        tmo = int(sys.argv[sys.argv.index("--timeout") + 1])
    run_chat(msg, tmo)
