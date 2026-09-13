# 跨服同步子系统（CrossManager 实现）。
import json
import threading
import time
import urllib.parse
import urllib.request

from .core import DEFAULT_CROSS_CONFIG, _json_default


class CrossManager(object):

    def __init__(self, plugin):
        self.plugin = plugin
        self.logger = plugin.logger
        self.path = plugin.data_dir / "cross.json"
        self.cfg = self._load()
        self.lock = threading.Lock()
        self.db_lock = threading.Lock()
        self.db_path = plugin.data_dir / "cross_db.json"
        self.db = {"players": {}, "whitelist": {}, "bans": {}, "peers": {}}
        self._stop = threading.Event()
        self._thread = None
        self.started = False
        self.last_ping = 0.0
        self.last_error = ""
        self._load_db()

    def _load(self):
        default = json.loads(json.dumps(DEFAULT_CROSS_CONFIG))
        try:
            if self.path.exists():
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    default = {**default, **loaded}
                    merged = dict(default["interop"])
                    merged.update((loaded.get("interop") or {}))
                    default["interop"] = merged
        except Exception:
            pass
        return default

    def save(self):
        try:
            self.plugin.data_dir.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except Exception as e:
            self.logger.error(f"[跨服] 保存配置失败: {e}")

    def _load_db(self):
        try:
            if self.db_path.exists():
                loaded = json.loads(self.db_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    for k in ("players", "whitelist", "bans", "peers"):
                        if isinstance(loaded.get(k), dict):
                            self.db[k] = loaded[k]
        except Exception:
            pass

    def _save_db(self):
        try:
            self.plugin.data_dir.mkdir(parents=True, exist_ok=True)
            self.db_path.write_text(json.dumps(self.db, ensure_ascii=False, indent=2, default=_json_default) + "\n", encoding="utf-8")
        except Exception as e:
            self.logger.error(f"[跨服] 保存跨服数据失败: {e}")

    def role(self):
        if not self.cfg.get("enabled"):
            return "none"
        return str(self.cfg.get("role", "none") or "none")

    def is_master(self):
        return self.role() == "master"

    def is_slave(self):
        return self.role() == "slave"

    def interop(self, key):
        try:
            return bool((self.cfg.get("interop") or {}).get(key, True))
        except Exception:
            return True

    def enabled(self):
        return bool(self.cfg.get("enabled")) and self.role() in ("master", "slave")

    def start(self):
        if self.started or not self.enabled():
            return
        self._stop.clear()
        self.started = True
        self.last_ping = time.time()
        if self.is_slave():
            self._thread = threading.Thread(target=self._slave_loop, daemon=True, name="cross-slave")
            self._thread.start()
            self.logger.info(f"[跨服] 从服模式启动，主服地址: {self.cfg.get('master_url') or '(空)'}")
        else:
            self.logger.info(f"[跨服] 主服(Hub)模式启动，服务器名: {self.cfg.get('server_name') or '本服'}")

    def stop(self):
        self._stop.set()
        self.started = False
        self._thread = None

    @staticmethod
    def _header_encode(v):
        try:
            return urllib.parse.quote(str(v or ""), safe="")
        except Exception:
            return urllib.parse.quote("", safe="")

    def _http_json(self, method, url, payload, timeout=6.0):
        url = self._normalize_url(url)
        if not url:
            raise ValueError("empty url")
        body = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Content-Type", "application/json; charset=utf-8")
        req.add_header("X-Cross-Pass", self._header_encode(str(self.cfg.get("password", "") or "") or "-"))
        req.add_header("X-Cross-Role", self._header_encode(self.role()))
        req.add_header("X-Cross-Name", self._header_encode(str(self.cfg.get("server_name", "") or "本服")))
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8") or "{}"
            return json.loads(raw)

    @staticmethod
    def _normalize_url(url):
        s = str(url or "").strip()
        if not s:
            return ""
        if "://" not in s:
            s = "http://" + s
        return s.rstrip("/")

    def _hub(self, base_url, packet):
        try:
            return self._http_json("POST", base_url.rstrip("/") + "/api/cross/hub", packet)
        except Exception as e:
            msg = str(e)
            if len(msg) > 160:
                msg = msg[:160] + "..."
            self.last_error = msg
            return None

    def master_base(self):
        return self._normalize_url(self.cfg.get("master_url", ""))

    def _slave_loop(self):
        base = self.master_base()
        if not base:
            self.last_error = "未配置主服地址 master_url"
            return
        if not str(self.cfg.get("password", "") or "").strip():
            self.last_error = "未设置连接密码"
            while not self._stop.is_set():
                self._stop.wait(20)
            return
        while not self._stop.is_set():
            try:
                wl_names, ban_names = self._local_state()
                reg = self._hub(base, {
                    "action": "register",
                    "server_name": str(self.cfg.get("server_name", "") or "本服"),
                    "self_url": str(self.cfg.get("self_url", "") or "").strip(),
                    "wl_names": wl_names,
                    "ban_names": ban_names,
                })
                if reg is not None:
                    self.last_ping = time.time()
                    self.last_error = ""
                    self._apply_remote_state(reg)
                else:
                    self.last_ping = 0
            except Exception as e:
                self.last_ping = 0
                self.last_error = str(e)[:200]
            self._stop.wait(max(3.0, float(self.cfg.get("sync_seconds", 8) or 8)))

    def _local_state(self):
        wl = []
        try:
            wl = list(getattr(self.plugin.server.whitelist, "names", None) or [])
        except Exception:
            wl = []
        bans = []
        try:
            bl = getattr(self.plugin.server, "bans", None)
            if bl is not None:
                for b in bl.get_entries():
                    try:
                        bans.append(str(b.name))
                    except Exception:
                        pass
        except Exception:
            bans = []
        return wl, bans

    def _apply_remote_state(self, reg):
        try:
            wl = reg.get("whitelist")
            if isinstance(wl, dict):
                with self.db_lock:
                    self.db["whitelist"] = dict(wl)
                    self._save_db()
                got = []
                try:
                    _wl_obj = getattr(self.plugin.server, "whitelist", None)
                    if _wl_obj is not None:
                        got = list(getattr(_wl_obj, "names", None) or [])
                except Exception:
                    got = []
                try:
                    self.plugin._apply_cross_whitelist(list(wl.keys()))
                except Exception as e:
                    self.logger.warning(f"[跨服] 应用白名单失败: {e}")
            bans = reg.get("bans")
            if isinstance(bans, dict):
                with self.db_lock:
                    self.db["bans"] = dict(bans)
                    self._save_db()
                try:
                    self.plugin._apply_cross_bans(bans)
                except Exception as e:
                    self.logger.warning(f"[跨服] 应用封禁失败: {e}")
            peers = reg.get("peers")
            if isinstance(peers, dict):
                with self.db_lock:
                    self.db["peers"] = dict(peers)
        except Exception as e:
            self.logger.warning(f"[跨服] 处理远程状态失败: {e}")

    def send_chat(self, server_name, player, text, origin="server"):
        if not self.enabled() or not self.interop("chat"):
            return
        try:
            threading.Thread(target=self.push_chat_now, args=(server_name, player, text, origin), daemon=True).start()
        except Exception:
            try:
                self.push_chat_now(server_name, player, text, origin)
            except Exception:
                pass

    def send_qq_relay(self, group_name, text):
        if not self.enabled() or not self.interop("chat"):
            return
        try:
            sname = str(self.cfg.get("server_name", "") or "本服")
            if self.is_slave():
                self._hub(self.master_base(), {"action": "chat", "server": sname, "player": group_name, "text": text, "origin": "qq"})
            elif self.is_master():
                self._master_on_chat(sname, group_name, text, "qq")
        except Exception as e:
            self.logger.warning(f"[跨服] 推送群聊到服务器失败: {e}")

    def broadcast_sys(self, text):
        if not self.enabled() or not self.interop("chat"):
            return
        try:
            self.plugin._cross_broadcast(text)
        except Exception:
            pass
        try:
            if self.is_slave():
                self._hub(self.master_base(), {"action": "sys_notice", "text": text})
            elif self.is_master():
                self._relay_sys(text)
        except Exception as e:
            self.logger.warning(f"[跨服] 广播系统通知失败: {e}")

    def _master_on_sys(self, text):
        try:
            self.plugin._cross_broadcast(text)
        except Exception:
            pass
        self._relay_sys(text)

    def _relay_sys(self, text):
        try:
            peers = {}
            with self.db_lock:
                peers = dict(self.db.get("peers", {}))
            for pname, purl in peers.items():
                if purl:
                    try:
                        self._hub(purl, {"action": "sys_relay", "text": text})
                    except Exception:
                        pass
        except Exception as e:
            self.logger.warning(f"[跨服] 转发系统通知失败: {e}")

    def push_chat_now(self, server_name, player, text, origin="server"):
        try:
            if self.is_slave():
                self._hub(self.master_base(), {"action": "chat", "server": server_name, "player": player, "text": text, "origin": origin})
            elif self.is_master():
                self._master_on_chat(server_name, player, text, origin)
        except Exception as e:
            self.logger.warning(f"[跨服] 推送聊天失败: {e}")

    def _master_on_chat(self, server_name, player, text, origin="server"):
        try:
            pl = self.plugin
            if self.interop("chat"):
                try:
                    if origin != "qq" or server_name != str(self.cfg.get("server_name", "") or "本服"):
                        pl._cross_broadcast(f"§b[{server_name}] §f{player}: §7{text}")
                except Exception:
                    pass
            if origin == "server":
                bot = getattr(pl, "qq_bot", None)
                if bot is not None:
                    try:
                        bot.send_qq_text(f"[跨服·{server_name}] {player}: {text}")
                    except Exception:
                        pass
                hub = getattr(pl, "qq_hub", None)
                if hub is not None:
                    try:
                        hub.websocket_relay("chat", player=f"[跨服·{server_name}] {player}", message=text)
                    except Exception:
                        pass
            peers = {}
            with self.db_lock:
                peers = dict(self.db.get("peers", {}))
            for pname, purl in peers.items():
                if purl and pname != server_name:
                    self._hub(purl, {"action": "relay", "server": server_name, "player": player, "text": text, "origin": origin})
        except Exception as e:
            self.logger.warning(f"[跨服] 主服转发聊天失败: {e}")

    def put_player_data(self, uuid, name, data):
        if not self.enabled() or not self.interop("player_data"):
            return
        try:
            if self.is_slave():
                self._hub(self.master_base(), {"action": "player_put", "uuid": uuid, "name": name, "data": data})
            elif self.is_master():
                with self.db_lock:
                    self.db.setdefault("players", {})[str(uuid)] = {"name": name, "data": data, "ts": time.time()}
                    self._save_db()
        except Exception as e:
            self.logger.warning(f"[跨服] 上传玩家数据失败: {e}")

    def get_player_data(self, uuid):
        if not self.enabled() or not self.interop("player_data"):
            return None
        try:
            if self.is_slave():
                r = self._hub(self.master_base(), {"action": "player_get", "uuid": str(uuid)})
                if r and r.get("ok"):
                    data = (r.get("data") or {}).get("data")
                    return data
                return None
            elif self.is_master():
                with self.db_lock:
                    rec = (self.db.get("players") or {}).get(str(uuid))
                    return (rec or {}).get("data")
        except Exception:
            return None

    def push_whitelist(self, names):

        if not self.enabled() or not self.interop("whitelist"):
            return
        mapping = {}
        for n in names:
            mapping[str(n).strip()] = True
        try:
            if self.is_slave():
                self._hub(self.master_base(), {"action": "wl_set", "names": mapping, "server_name": self.cfg.get("server_name")})
            elif self.is_master():
                with self.db_lock:
                    self.db.setdefault("whitelist", {}).update(mapping)
                    self._save_db()
                    wl = dict(self.db["whitelist"])
                    peers = dict(self.db.get("peers", {}))
                self.plugin._cross_broadcast_wl_to_peers(self, wl, peers)
        except Exception as e:
            self.logger.warning(f"[跨服] 推送白名单失败: {e}")

    def push_bans(self, entries):

        if not self.enabled() or not self.interop("ban"):
            return
        try:
            if self.is_slave():
                self._hub(self.master_base(), {"action": "ban_set", "entries": dict(entries), "server_name": self.cfg.get("server_name")})
            elif self.is_master():
                with self.db_lock:
                    self.db.setdefault("bans", {}).update(dict(entries))
                    self._save_db()
                    bans = dict(self.db["bans"])
                    peers = dict(self.db.get("peers", {}))
                self.plugin._cross_broadcast_ban_to_peers(self, bans, peers)
        except Exception as e:
            self.logger.warning(f"[跨服] 推送封禁失败: {e}")
