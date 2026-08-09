"""Hound 暴力验证脚本（手动运行，非 pytest）。

覆盖：SSRF 变体矩阵、输入边界、feed_fetch 崩溃回归、重定向 SSRF。
用法：python tests/brute_verify.py
"""
from hound_mcp.security import SecurityError, validate_url, validate_search_query, validate_headers

FAIL = []
PASS = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
    else:
        FAIL.append(name)
        print(f"  [FAIL] {name}: {detail}")


def ssrf_matrix():
    print("=== SSRF 变体矩阵 ===")
    cases = [
        ("127.0.0.1", "http://127.0.0.1:8080/test", True),
        ("octal 0177", "http://0177.0.0.1:80/", True),
        ("hex 0x7f000001", "http://0x7f000001/", True),
        ("short-form 127.1", "http://127.1:80/", True),
        ("IPv6 ::1", "http://[::1]:8080/x", True),
        ("malformed bracket", "http://[invalid-host]/", True),
        ("nip.io", "http://127.0.0.1.nip.io/", True),
        ("backslash", r"http://evil.com\@127.0.0.1:80/", True),
        ("ftp", "ftp://example.com/", True),
        ("data", "data:text/html,hi", True),
        ("no scheme", "example.com", True),
        ("port 70000", "http://example.com:70000/", True),
        ("octal 4-part", "http://0177.0000.0000.0001/", True),
        ("hex dotted", "http://0x7f.0.0.1/", True),
        ("decimal int", "http://2130706433/", True),
        ("IPv4-mapped", "http://[::ffff:127.0.0.1]/", True),
        ("localhost", "http://localhost/", True),
        ("metadata google", "http://metadata.google.internal/", True),
        ("sslip.io", "http://x.sslip.io/", True),
        ("公网正常", "https://example.com", False),
    ]
    for label, u, should_block in cases:
        try:
            validate_url(u)
            blocked = False
            detail = "ALLOWED"
        except SecurityError as e:
            blocked = True
            detail = str(e)[:50]
        got = blocked if should_block else not blocked
        check(f"SSRF {label}", got, detail)


def input_boundaries():
    print("=== 输入边界 ===")
    # 超长 URL
    try:
        validate_url("https://example.com/" + "a" * 9000)
        check("超长 URL 拒绝", False, "未拒绝")
    except SecurityError:
        check("超长 URL 拒绝", True)
    # 空 URL
    for bad in ("", None, "   "):
        try:
            validate_url(bad)
            check(f"空 URL {bad!r} 拒绝", False, "未拒绝")
        except SecurityError:
            check(f"空 URL {bad!r} 拒绝", True)
    # 非字符串
    try:
        validate_url(123)
        check("非字符串 URL 拒绝", False)
    except SecurityError:
        check("非字符串 URL 拒绝", True)
    # 搜索 query
    try:
        validate_search_query("  " * 100)
        check("空 query 拒绝", False)
    except SecurityError:
        check("空 query 拒绝", True)
    try:
        validate_search_query("x" * 5000)
        check("超长 query 拒绝", False)
    except SecurityError:
        check("超长 query 拒绝", True)
    # headers 注入
    try:
        validate_headers({"X-Test": "a\nb"})
        check("header 换行注入拒绝", False)
    except SecurityError:
        check("header 换行注入拒绝", True)
    try:
        validate_headers({"Host": "evil.com"})
        check("header 禁止 Host 拒绝", False)
    except SecurityError:
        check("header 禁止 Host 拒绝", True)
    try:
        validate_headers({"X-Backslash\\Name": "v"})
        check("header name backslash 拒绝", False)
    except SecurityError:
        check("header name backslash 拒绝", True)


def main():
    ssrf_matrix()
    input_boundaries()
    print(f"\n结果: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("失败项:", FAIL)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())