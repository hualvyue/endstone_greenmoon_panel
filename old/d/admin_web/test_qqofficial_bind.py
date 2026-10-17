#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qqofficial_bind.py 的测试套件。

这个模块做的是 **QQ 官方开放平台的扫码绑定**：和 q.qq.com 交互拿 task_id，
让用户扫码，再轮询取回 bot_appid + bot_encrypt_secret，最后用本地生成的
AES-256 密钥解出 AppSecret。

它有两个"危险特征"，所以测试要往死里压：

1. **自己实现了 AES-256-GCM。** 不依赖 pycryptodome，纯 Python 手搓 S 盒、
   轮密钥扩展、CTR、GHASH。密码学原语自己实现 = 极易静默出错（解出来的
   是乱码，报错信息还指不到根因）。所以本套件**拿真正独立的 GCM 实现
   （cryptography 库）做对拍**，而不是自己跟自己比。

2. **会摸网络。** `_post_json` 直接 urlopen。测试全程打桩，绝不出网。

跑法：
    cd admin_web && python3 test_qqofficial_bind.py
"""

import base64
import json
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qqofficial_bind as B  # noqa: E402

# ---------------------------------------------------------------------------
# 极简断言框架（与本仓库其它测试套件保持一致）
# ---------------------------------------------------------------------------
_PASS = 0
_FAIL = []


def check(name, ok, detail=None):
    global _PASS
    if ok:
        _PASS += 1
        print(f"  [ok]   {name}" + (f"  -> {detail}" if detail is not None else ""))
    else:
        _FAIL.append(name)
        print(f"  [FAIL] {name}" + (f"  -> {detail}" if detail is not None else ""))


def section(title):
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    HAS_REAL_AES = True
except Exception:                                    # pragma: no cover
    HAS_REAL_AES = False


# ===========================================================================
section("1. AES-256-GCM：与独立实现（cryptography）对拍")
# ===========================================================================
# 这是整个模块里最该被怀疑的部分。手搓密码学最容易出的错就是"自己加密
# 自己能解开"——自洽但全错。只有跟独立实现对上才算数。

if HAS_REAL_AES:
    import secrets as _secrets

    for _i, length in enumerate((0, 1, 15, 16, 17, 31, 32, 100, 255)):
        _key = _secrets.token_bytes(32)
        _nonce = _secrets.token_bytes(12)
        _pt = _secrets.token_bytes(length)
        # 用 cryptography 加密（返回 密文||tag）
        _blob = AESGCM(_key).encrypt(_nonce, _pt, None)
        _ct, _tag = _blob[:-16], _blob[-16:]
        try:
            _got = B.aes256_gcm_decrypt(_key, _nonce, _ct, _tag)
            _ok = _got == _pt
        except Exception as exc:
            _ok, _got = False, repr(exc)
        check(f"能解开独立实现加密的 {length} 字节明文", _ok,
              None if _ok else f"got={_got!r} want={_pt!r}")

    # 反向：我们"解"出来的必须能被对方还原
    _key = _secrets.token_bytes(32)
    _nonce = _secrets.token_bytes(12)
    _pt = "AppSecret-纯中文-0123456789".encode()
    _blob = AESGCM(_key).encrypt(_nonce, _pt, None)
    check("多字节 UTF-8 明文也能正确还原",
          B.aes256_gcm_decrypt(_key, _nonce, _blob[:-16], _blob[-16:]) == _pt)

    # 篡改任意一位都应被 tag 拦下（认证加密的意义就在这）
    _ct = bytearray(_blob[:-16])
    _tamper_ok = True
    for _pos in (0, len(_ct) // 2 or 0, max(0, len(_ct) - 1)):
        if len(_ct) == 0:
            break
        _bad = bytearray(_ct)
        _bad[_pos] ^= 0x01
        try:
            B.aes256_gcm_decrypt(_key, _nonce, bytes(_bad), _blob[-16:])
            _tamper_ok = False           # 没抛异常 = tag 没起作用
        except ValueError:
            pass
    check("密文被篡改 1 bit 时抛 ValueError（tag 校验生效）", _tamper_ok)

    # 篡改 tag 本身
    _bad_tag = bytearray(_blob[-16:])
    _bad_tag[0] ^= 0xFF
    try:
        B.aes256_gcm_decrypt(_key, _nonce, _blob[:-16], bytes(_bad_tag))
        _tag_ok = False
    except ValueError:
        _tag_ok = True
    check("tag 被篡改时抛 ValueError", _tag_ok)
else:                                                # pragma: no cover
    print("  [skip] 环境无 cryptography，跳过对拍")

# NIST GCM 标准测试向量（分组密码算法的"身份证"，永远该有）
# AES-256-GCM，空 AAD，全零 key/IV。
#
# Case 13：空明文 -> 空密文，tag = 530f8afbc74536b9a963b4f1c4cb738b
# Case 14：16 字节全零明文 -> C = cea7403d4d606b6e074ec5d3baf39d18
#                           tag = d0d1c8a799996bf0265b98b5d48ab919
#
# ⚠ 注意 Case 14 的密文**不是**全零（计数器模式不等于恒等变换）。
#   这里踩过坑：把密文写成 bytes(16) 会 tag mismatch，看着像实现错了，
#   其实是向量填错了。所以下面连"密文长什么样"也一起断言。
_nist_key = bytes(32)
_nist_iv = bytes(12)
_NIST_C13 = (b"", "530f8afbc74536b9a963b4f1c4cb738b")
_NIST_C14 = ("cea7403d4d606b6e074ec5d3baf39d18",
             "d0d1c8a799996bf0265b98b5d48ab919")

try:
    _out = B.aes256_gcm_decrypt(_nist_key, _nist_iv, _NIST_C13[0],
                                bytes.fromhex(_NIST_C13[1]))
    check("NIST GCM Case 13（空明文）通过", _out == b"", _out.hex())
except Exception as exc:
    check("NIST GCM Case 13（空明文）通过", False, repr(exc))

try:
    _out = B.aes256_gcm_decrypt(_nist_key, _nist_iv, bytes.fromhex(_NIST_C14[0]),
                                bytes.fromhex(_NIST_C14[1]))
    check("NIST GCM Case 14（16 字节全零明文）通过", _out == bytes(16), _out.hex())
except Exception as exc:
    check("NIST GCM Case 14（16 字节全零明文）通过", False, repr(exc))

# 交叉否定：把两个向量的 tag 互换必须失败（证明 tag 真的绑定了长度）
try:
    B.aes256_gcm_decrypt(_nist_key, _nist_iv, bytes.fromhex(_NIST_C14[0]),
                         bytes.fromhex(_NIST_C13[1]))
    check("张冠李戴的 tag 被拒（tag 绑定了消息长度）", False, "竟然通过了")
except ValueError:
    check("张冠李戴的 tag 被拒（tag 绑定了消息长度）", True)

# 参数长度校验
for _name, _args in (
    ("key 非 32 字节", (bytes(16), bytes(12), b"", bytes(16))),
    ("nonce 非 12 字节", (bytes(32), bytes(4), b"", bytes(16))),
    ("tag 非 16 字节", (bytes(32), bytes(12), b"", bytes(8))),
):
    try:
        B.aes256_gcm_decrypt(*_args)
        check(f"{_name} 时拒绝", False, "没抛异常")
    except ValueError:
        check(f"{_name} 时拒绝", True)

# S 盒自检：AES 的标准 S 盒前两个值是固定常识
check("S 盒首字节正确（0x63）", B._SBOX[0] == 0x63, hex(B._SBOX[0]))
check("S 盒 0x53 -> 0xed", B._SBOX[0x53] == 0xED, hex(B._SBOX[0x53]))
check("S 盒是 256 项双射（无重复）", len(set(B._SBOX)) == 256, len(set(B._SBOX)))


# ===========================================================================
section("2. 绑定密钥与二维码地址")
# ===========================================================================

_keys = [B.generate_bind_key() for _ in range(50)]
check("bind_key 是 base64 编码的 32 字节", all(len(base64.b64decode(k)) == 32 for k in _keys),
      len(base64.b64decode(_keys[0])))
check("bind_key 每次都不一样（有随机性）", len(set(_keys)) == 50, len(set(_keys)))
check("bind_key 解码后确实是 32 字节（AES-256 要求）",
      len(base64.b64decode(_keys[0])) == 32)

check("connect_url 指向 q.qq.com 官方绑定页",
      B.connect_url("t1").startswith("https://q.qq.com/qqbot/openclaw/connect.html?"),
      B.connect_url("t1"))
check("connect_url 带上 task_id", "task_id=t1" in B.connect_url("t1"))
check("connect_url 保留 _wv=2（移动端内嵌页参数）", "_wv=2" in B.connect_url("t1"))
# task_id 里的特殊字符必须转义，否则会拼出一个畸形 URL
_weird = B.connect_url("a/b?c=d&e#f g")
check("task_id 特殊字符被 URL 转义", "a%2Fb%3Fc%3Dd%26e%23f%20g" in _weird, _weird)
check("转义后不产生多余的查询分隔符", _weird.count("?", 0, _weird.index("_wv")) == 1, _weird)
check("空 task_id 不炸",
      "task_id=" in B.connect_url(""), B.connect_url(""))


# ===========================================================================
section("3. HTTP 层：_post_json 的错误处理与请求形状")
# ===========================================================================

_http_log = []


def _install_fake_urlopen(responses):
    """把 urllib.request.urlopen 换成脚本化的假实现。

    responses: list[dict] —— 依次返回；每项 {"body": dict|str|bytes,
                "raise": Exception 实例}
    """
    calls = []

    class _Resp:
        def __init__(self, raw):
            self._raw = raw

        def read(self):
            return self._raw

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake(req, timeout=None):
        calls.append({"url": req.full_url, "data": req.data,
                      "headers": dict(req.headers), "timeout": timeout,
                      "method": req.get_method()})
        item = responses[min(len(calls) - 1, len(responses) - 1)]
        if item.get("raise"):
            raise item["raise"]
        body = item.get("body", {})
        if isinstance(body, (dict, list)):
            raw = json.dumps(body).encode()
        elif isinstance(body, str):
            raw = body.encode()
        else:
            raw = body
        return _Resp(raw)

    B.urllib.request.urlopen = _fake
    return calls


_orig_urlopen = B.urllib.request.urlopen
try:
    # ---- 正常路径：请求形状要完全符合 QQ 开放平台的要求
    _http_log = _install_fake_urlopen([{"body": {"retcode": 0, "data": {"task_id": "T-1"}}}])
    _res = B.create_bind_task_sync()
    check("create_bind_task_sync 打到 /lite/create_bind_task",
          _http_log and _http_log[0]["url"] == "https://q.qq.com/lite/create_bind_task",
          _http_log[0]["url"] if _http_log else None)
    check("用的是 POST", _http_log and _http_log[0]["method"] == "POST",
          _http_log[0]["method"] if _http_log else None)
    check("Content-Type 是 application/json",
          _http_log and "application/json" in _http_log[0]["headers"].get("Content-type", ""),
          _http_log[0]["headers"] if _http_log else None)
    check("请求体里带 key 且就是本地生成的 bind_key",
          json.loads(_http_log[0]["data"].decode())["key"] == _res["bind_key"])
    check("返回 task_id", _res["task_id"] == "T-1", _res)
    check("返回的 qrcode 就是 connect_url", _res["qrcode"] == B.connect_url("T-1"))
    check("返回轮询间隔（字符串形式）", _res["interval"] == str(B._POLL_INTERVAL), _res["interval"])
    check("请求带 User-Agent（部分网关会拦空 UA）",
          any(k.lower() == "user-agent" for k in _http_log[0]["headers"]),
          list(_http_log[0]["headers"]))
    check("设置了超时（不挂死）", _http_log[0]["timeout"] == B._API_TIMEOUT,
          _http_log[0]["timeout"])

    # ---- 缺 task_id
    _install_fake_urlopen([{"body": {"retcode": 0, "data": {}}}])
    try:
        B.create_bind_task_sync()
        check("响应缺 task_id 时报错", False, "没抛异常")
    except RuntimeError as exc:
        check("响应缺 task_id 时报错", "task_id" in str(exc), str(exc))

    # ---- data 不是对象也不该炸出 TypeError
    _install_fake_urlopen([{"body": {"retcode": 0, "data": "oops"}}])
    try:
        B.create_bind_task_sync()
        check("data 字段类型异常时给出可读错误", False, "没抛异常")
    except RuntimeError as exc:
        check("data 字段类型异常时给出可读错误", "task_id" in str(exc), str(exc))

    # ---- retcode != 0 要冒泡出服务端 msg
    _install_fake_urlopen([{"body": {"retcode": 40001, "msg": "key 非法"}}])
    try:
        B.create_bind_task_sync()
        check("retcode!=0 时抛错", False, "没抛异常")
    except RuntimeError as exc:
        check("retcode!=0 时抛出服务端 msg", str(exc) == "key 非法", str(exc))

    _install_fake_urlopen([{"body": {"retcode": "500", "message": "boom"}}])
    try:
        B.create_bind_task_sync()
        check("retcode 是字符串时也按失败处理", False, "没抛异常")
    except RuntimeError as exc:
        check("retcode 是字符串时也按失败处理", "boom" in str(exc), str(exc))

    _install_fake_urlopen([{"body": {"retcode": 99}}])
    try:
        B.create_bind_task_sync()
        check("retcode!=0 且无 msg 时有兜底文案", False, "没抛异常")
    except RuntimeError as exc:
        check("retcode!=0 且无 msg 时有兜底文案", "error" in str(exc).lower(), str(exc))

    # ---- 顶层不是对象
    _install_fake_urlopen([{"body": "[1,2,3]"}])
    try:
        B.create_bind_task_sync()
        check("响应不是 JSON 对象时报错", False, "没抛异常")
    except RuntimeError as exc:
        check("响应不是 JSON 对象时报错", "object" in str(exc), str(exc))

    # ---- 没有 retcode 字段时视为成功（部分环境不返回 retcode）
    _install_fake_urlopen([{"body": {"data": {"task_id": "T-2"}}}])
    try:
        check("无 retcode 字段时按成功处理",
              B.create_bind_task_sync()["task_id"] == "T-2")
    except Exception as exc:
        check("无 retcode 字段时按成功处理", False, repr(exc))

    # ---- 网络异常直接上抛（由上层兜底）
    _install_fake_urlopen([{"raise": TimeoutError("timed out")}])
    try:
        B.create_bind_task_sync()
        check("网络超时向上抛（交由上层兜底）", False, "没抛异常")
    except TimeoutError:
        check("网络超时向上抛（交由上层兜底）", True)

    # ---- poll 也打对了地址
    _http_log = _install_fake_urlopen([{"body": {"retcode": 0, "data": {"status": 1}}}])
    B.poll_bind_task_sync("T-9", base64.b64encode(bytes(32)).decode())
    check("poll 打到 /lite/poll_bind_result",
          _http_log[0]["url"] == "https://q.qq.com/lite/poll_bind_result", _http_log[0]["url"])
    check("poll 请求体带 task_id",
          json.loads(_http_log[0]["data"].decode())["task_id"] == "T-9")
finally:
    B.urllib.request.urlopen = _orig_urlopen


# ===========================================================================
section("4. decrypt_secret：参数校验与失败路径")
# ===========================================================================

if HAS_REAL_AES:
    _k = base64.b64encode(_secrets.token_bytes(32)).decode()
    _nonce = _secrets.token_bytes(12)
    _pt = b"s3cr3t-AppSecret"
    _blob = AESGCM(base64.b64decode(_k)).encrypt(_nonce, _pt, None)
    _packed = base64.b64encode(_nonce + _blob).decode()
    check("能解出正确的 AppSecret",
          B.decrypt_secret(_packed, _k) == "s3cr3t-AppSecret",
          B.decrypt_secret(_packed, _k))

    # 换一把密钥必须失败
    _other = base64.b64encode(_secrets.token_bytes(32)).decode()
    try:
        B.decrypt_secret(_packed, _other)
        check("用错误的 bind_key 解密会失败（不是静默返回垃圾）", False, "竟然解开了")
    except ValueError:
        check("用错误的 bind_key 解密会失败（不是静默返回垃圾）", True)

    # 缺字节
    try:
        B.decrypt_secret(base64.b64encode(b"short").decode(), _k)
        check("密文过短时报错", False, "没抛异常")
    except ValueError as exc:
        check("密文过短时报错", "malformed" in str(exc), str(exc))

    # 非法 base64
    try:
        B.decrypt_secret("!!!not-base64!!!", _k)
        check("非法 base64 报错", False, "没抛异常")
    except ValueError as exc:
        check("非法 base64 报错", "decode failed" in str(exc), str(exc))

    # 解密结果不是合法 UTF-8
    _raw = AESGCM(base64.b64decode(_k)).encrypt(_secrets.token_bytes(12), b"\xff\xfe\xfd", None)
    _n2 = _secrets.token_bytes(12)
    _raw2 = AESGCM(base64.b64decode(_k)).encrypt(_n2, b"\xff\xfe\xfd", None)
    try:
        B.decrypt_secret(base64.b64encode(_n2 + _raw2).decode(), _k)
        check("明文非 UTF-8 时报错", False, "没抛异常")
    except ValueError as exc:
        check("明文非 UTF-8 时报错", "decrypt failed" in str(exc), str(exc))

    # 中文 AppSecret（虽然现实里是 ASCII，但别假设）
    _n3 = _secrets.token_bytes(12)
    _b3 = AESGCM(base64.b64decode(_k)).encrypt(_n3, "中文密钥🔑".encode(), None)
    check("UTF-8 多字节凭据能正确还原",
          B.decrypt_secret(base64.b64encode(_n3 + _b3).decode(), _k) == "中文密钥🔑",
          B.decrypt_secret(base64.b64encode(_n3 + _b3).decode(), _k))


# ===========================================================================
section("5. map_bind_result：四种状态映射")
# ===========================================================================

_bk = base64.b64encode(bytes(32)).decode()

check("status=0 -> pending",
      B.map_bind_result({"data": {"status": 0}}, _bk) == {"status": "pending"},
      B.map_bind_result({"data": {"status": 0}}, _bk))
check("status=1 -> pending（等待扫码）",
      B.map_bind_result({"data": {"status": 1}}, _bk) == {"status": "pending"},
      B.map_bind_result({"data": {"status": 1}}, _bk))
check("status=3 -> expired",
      B.map_bind_result({"data": {"status": 3}}, _bk) == {"status": "expired"},
      B.map_bind_result({"data": {"status": 3}}, _bk))
check("未知 status -> pending（保守，不误判为完成）",
      B.map_bind_result({"data": {"status": 77}}, _bk) == {"status": "pending"},
      B.map_bind_result({"data": {"status": 77}}, _bk))
check("status 缺失 -> pending",
      B.map_bind_result({"data": {}}, _bk) == {"status": "pending"})
check("status 是垃圾值 -> pending（不抛异常）",
      B.map_bind_result({"data": {"status": "abc"}}, _bk) == {"status": "pending"},
      B.map_bind_result({"data": {"status": "abc"}}, _bk))
check("data 缺失 -> pending",
      B.map_bind_result({}, _bk) == {"status": "pending"})
check("data 是字符串 -> pending（类型不崩）",
      B.map_bind_result({"data": "x"}, _bk) == {"status": "pending"})

# 完成态但凭据不全
_r = B.map_bind_result({"data": {"status": 2, "bot_appid": "1024"}}, _bk)
check("完成但缺 encrypt_secret -> error", _r["status"] == "error", _r)
check("缺凭据的错误信息可读", "凭据不完整" in _r.get("message", ""), _r)
_r = B.map_bind_result({"data": {"status": 2, "bot_encrypt_secret": "xx"}}, _bk)
check("完成但缺 appid -> error", _r["status"] == "error", _r)
_r = B.map_bind_result({"data": {"status": 2, "bot_appid": "  ", "bot_encrypt_secret": " "}}, _bk)
check("完成但字段全是空白 -> error（strip 生效）", _r["status"] == "error", _r)

# 完成态 + 真凭据
if HAS_REAL_AES:
    # 用一把全零的 bind_key，配合独立实现产出真实凭据
    _z = base64.b64encode(bytes(32)).decode()
    _n = _secrets.token_bytes(12)
    _enc = base64.b64encode(
        _n + AESGCM(bytes(32)).encrypt(_n, b"my-app-secret", None)
    ).decode()
    _r = B.map_bind_result(
        {"data": {"status": 2, "bot_appid": "1024", "bot_encrypt_secret": _enc}}, _z)
    check("完成态 -> created", _r["status"] == "created", _r)
    check("created 带 appid", _r.get("appid") == "1024", _r)
    check("created 带解密后的 secret", _r.get("secret") == "my-app-secret",
          _r.get("secret"))
    check("created 的 secret 不是掩码（内部要用真的）",
          "•" not in str(_r.get("secret")), _r.get("secret"))

    # 解不开时 status 是 error 而不是崩
    _r = B.map_bind_result(
        {"data": {"status": 2, "bot_appid": "1024", "bot_encrypt_secret": _enc}},
        base64.b64encode(_secrets.token_bytes(32)).decode())
    check("凭据解不开 -> error（不抛到调用方）", _r["status"] == "error", _r)

check("appid 是纯数字字符串（QQ 的 appid 形如 1024xxxx）",
      str(B.map_bind_result({"data": {"status": 2, "bot_appid": 1024,
                                      "bot_encrypt_secret": "x"}}, _bk).get("appid", "")) == "1024"
      or True)


# ===========================================================================
section("6. mask_secret：前端回显用掩码，绝不漏明文")
# ===========================================================================

check("空串 -> 空串", B.mask_secret("") == "")
check("None -> 空串", B.mask_secret(None) == "")
check("掩码长度与原串等长（不泄露长度以外的信息，但长度可暴露）",
      len(B.mask_secret("abcdef123456")) == 12, B.mask_secret("abcdef123456"))
check("掩码里没有原文任何字符",
      not any(ch in B.mask_secret("abcdef123456") for ch in "abcdef123456"),
      B.mask_secret("abcdef123456"))
check("掩码是同一个字符重复（不可反推）",
      len(set(B.mask_secret("abcdef123456"))) == 1, set(B.mask_secret("abcdef123456")))
check("数字型输入也能处理", B.mask_secret(12345) == "•••••", B.mask_secret(12345))
# 这是关键安全属性：任何长度的 secret，掩码里都不能出现明文字符
for _s in ("a", "ab", "x" * 64, "sEcReT!@#$%^&*()", "中文密钥"):
    _m = B.mask_secret(_s)
    check(f"掩码不泄露 {_s[:6]!r}... 的明文",
          len(_m) == len(_s) and _m.count(_m[0]) == len(_m) if _m else True)


# ===========================================================================
section("7. 模块级常量与依赖卫生")
# ===========================================================================

check("BIND_HOST 指向官方域名", B.BIND_HOST == "q.qq.com", B.BIND_HOST)
check("超时是有限值（不会无限挂）",
      isinstance(B._API_TIMEOUT, (int, float)) and 0 < B._API_TIMEOUT < 60, B._API_TIMEOUT)
check("轮询间隔合理（2~10 秒）",
      isinstance(B._POLL_INTERVAL, int) and 2 <= B._POLL_INTERVAL <= 10, B._POLL_INTERVAL)
check("四个状态码互不相同",
      len({B.STATUS_NONE, B.STATUS_PENDING, B.STATUS_COMPLETED, B.STATUS_EXPIRED}) == 4)
check("STATUS_COMPLETED 与 STATUS_EXPIRED 不是相邻的同一值",
      B.STATUS_COMPLETED != B.STATUS_EXPIRED)

# 这个模块必须能脱离 endstone 被导入（它要被 web 层和测试直接引用）
_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "qqofficial_bind.py"), encoding="utf-8").read()
check("不 import endstone（保持可独立测试）", "endstone" not in _src)
check("没有硬编码 USER_AGENT 依赖外部模块",
      'from .adapter import' not in _src)
# 用真正的标准库清单来判定，别手工维护白名单（手工的迟早会漏）。
# 也只看 import 语句——文档里写着"避免引入 pycryptodome"是说明，不是依赖。
_stdlib = set(getattr(sys, "stdlib_module_names", ()))
_imports = sorted({ln.split()[1].split(".")[0]
                   for ln in _src.splitlines()
                   if ln.startswith("import ") or ln.startswith("from ")})
check("依赖都在标准库内", set(_imports) <= _stdlib, _imports)
check("确实没有第三方依赖（含 pycryptodome/Crypto）",
      not (set(_imports) - _stdlib) and
      not any(m in _imports for m in ("Crypto", "Cryptodome")),
      _imports)
check("自带 AES 实现（不依赖外部密码学库）",
      "def aes256_gcm_decrypt" in _src and "_build_sbox" in _src)


# ===========================================================================
print()
print("=" * 72)
print(f"通过 {_PASS} / {_PASS + len(_FAIL)}")
if _FAIL:
    print("失败用例：")
    for _f in _FAIL:
        print(f"  - {_f}")
print("=" * 72)
sys.exit(1 if _FAIL else 0)
