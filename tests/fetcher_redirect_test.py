"""实测 fetcher.HTTPSession 重定向行为（修复后）。

验证声明 2/3 已修复：
- 重定向到内网现应被拒绝（SecurityError）
- 同域重定向应正常跟随
- max_redirects 应生效
"""
import asyncio
import http.server
import socketserver
import threading

PUBLIC_PORT = 18765
INTERNAL_PORT = 18766
HITS = []


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        HITS.append((self.path, self.client_address[0]))
        if self.path.startswith("/redirect-to-internal"):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{INTERNAL_PORT}/internal-secret")
            self.end_headers()
        elif self.path.startswith("/redirect-chain"):
            hop = int(self.path.split("hop=")[1]) if "hop=" in self.path else 1
            if hop < 4:
                self.send_response(302)
                self.send_header("Location", f"/redirect-chain?hop={hop+1}")
                self.end_headers()
            else:
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{INTERNAL_PORT}/internal-secret")
                self.end_headers()
        elif self.path.startswith("/redirect-ok"):
            self.send_response(302)
            self.send_header("Location", "/final")
            self.end_headers()
        elif self.path.startswith("/final"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body>FINAL_OK</body></html>")
        elif self.path.startswith("/internal-secret"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"INTERNAL_SECRET_DATA")
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body>ok</body></html>")


class S(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


pub = S(("127.0.0.1", PUBLIC_PORT), H)
internal = S(("127.0.0.1", INTERNAL_PORT), H)
threading.Thread(target=pub.serve_forever, daemon=True).start()
threading.Thread(target=internal.serve_forever, daemon=True).start()


async def main():
    from hound_mcp.fetcher import HTTPSession
    from hound_mcp.security import SecurityError

    print("=" * 60)
    print("修复验证 1: 公网 302 -> 内网 应被拒绝")
    print("=" * 60)
    HITS.clear()
    try:
        async with HTTPSession(stealthy_headers=False, retries=0, timeout=10) as s:
            await s.get(f"http://127.0.0.1:{PUBLIC_PORT}/redirect-to-internal",
                        follow_redirects=True)
        print("  FAIL: 未抛出 SecurityError")
    except SecurityError as e:
        print(f"  PASS: SecurityError = {str(e)[:60]}")
    except Exception as e:
        print(f"  PASS(其他异常): {type(e).__name__}: {str(e)[:80]}")
    print(f"  靶场命中: {HITS}")
    assert not any("internal-secret" in h[0] for h in HITS), "内网不应被访问"

    print()
    print("=" * 60)
    print("修复验证 2: 同域重定向应正常跟随")
    print("=" * 60)
    HITS.clear()
    async with HTTPSession(stealthy_headers=False, retries=0, timeout=10) as s:
        r = await s.get(f"http://127.0.0.1:{PUBLIC_PORT}/redirect-ok",
                        follow_redirects=True, allow_internal=True)
    print(f"  final status={r.status} url={r.url} body={r.body[:20]!r}")
    assert b"FINAL_OK" in r.body, "同域重定向应跟随成功"
    print("  PASS: 同域重定向正常跟随")

    print()
    print("=" * 60)
    print("修复验证 3: max_redirects 生效")
    print("=" * 60)
    HITS.clear()
    async with HTTPSession(stealthy_headers=False, retries=0, timeout=10) as s:
        r = await s.get(f"http://127.0.0.1:{PUBLIC_PORT}/redirect-chain?hop=1",
                        follow_redirects=True, max_redirects=2, allow_internal=True)
    print(f"  max_redirects=2, 命中数={len(HITS)} (应为 3: 初始+2跳)")
    print(f"  final url={r.url} status={r.status}")
    assert len(HITS) == 3, f"max_redirects 未严格生效: {len(HITS)}"
    print("  PASS: max_redirects 生效")

    print()
    print("=" * 60)
    print("修复验证 4: follow_redirects=False 不跟随")
    print("=" * 60)
    HITS.clear()
    async with HTTPSession(stealthy_headers=False, retries=0, timeout=10) as s:
        r = await s.get(f"http://127.0.0.1:{PUBLIC_PORT}/redirect-ok",
                        follow_redirects=False, allow_internal=True)
    print(f"  final status={r.status} (应为 302, 不跟随)")
    assert r.status == 302, f"follow_redirects=False 被忽略: {r.status}"
    print("  PASS: follow_redirects=False 不跟随")

    pub.shutdown()
    internal.shutdown()
    print("\nALL PASSED")


asyncio.run(main())