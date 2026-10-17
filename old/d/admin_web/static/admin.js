<script>
        let currentPlayers = [];
        async function loadDiag() {
            const box = el('diag-box'), st = el('diag-status');
            try {
                const r = await apiGet('/api/diag');
                if (!r || r.error) { if (st) st.textContent = '读取失败: ' + (r ? r.error : '无响应'); return; }
                const d = r.diag || {};
                const lines = [];
                // ★★★ 存储故障优先显示在最上方：用户反馈"日志写入错误"时，
                // 这是唯一能说明"为什么"的地方（其余统计项帮不上忙）。
                if (d.log_error) lines.push('★★★ 日志未能正常启用：' + d.log_error);
                if (d.data_dir_error) lines.push('★★★ 数据目录解析失败：' + d.data_dir_error);
                if (d.packaging_error) lines.push('★★★ ' + d.packaging_error);
                if (lines.length) lines.push('');
                lines.push('数据目录(data_dir)   : ' + (d.data_dir || '(未设置)'));
                lines.push('日志文件(log_path)   : ' + (d.log_path || '(未启用)'));
                lines.push('');
                lines.push(
                    'tick间隔(tick_gap_ms) : ' + (d.tick_gap_ms != null ? d.tick_gap_ms : '?') + '  ms（正常约 50ms）',
                    'tick偏大次数(slow)   : ' + (d.tick_slow_count != null ? d.tick_slow_count : '?'),
                    '排队/执行中任务      : ' + (d.current ? String(d.current) : '(无)') + '   |  排队计数=' + (d.pending != null ? d.pending : '?'),
                    '备份正在执行         : ' + (d.backup_running ? '是' : '否'),
                    'UP: ' + (d.cloud_cfg ? ('cloud_enabled=' + d.cloud_cfg.enabled + ' check_join=' + d.cloud_cfg.check_on_join + ' token=' + (d.cloud_cfg.token_set ? '已设置' : '未设置')) : ''),
                    '诊断开关: ' + (d.enabled ? '开启' : '关闭') + '   |  详细日志: ' + (d.detailed ? '开启' : '关闭')
                );
                let ev = d.events || [];
                lines.push('');
                lines.push('--- 最近诊断事件 (' + ev.length + ') ---');
                ev = ev.slice(-15);
                ev.forEach(function (e) {
                    const ex = e.extra || {};
                    let detail = '';
                    if (e.kind === 'timeout') detail = '任务=' + ex.name + ' 当前=' + ex.current + ' 排队=' + ex.pending + ' tick=' + ex.tick_gap_ms + 'ms';
                    else if (e.kind === 'tick_slow') detail = 'gap=' + ex.gap_ms + 'ms 当前=' + ex.current;
                    else if (e.kind === 'busy') detail = '任务=' + ex.name;
                    else detail = JSON.stringify(ex);
                    lines.push('[' + e.ts + '] ' + e.kind + '  ' + detail);
                });
                if (box) box.textContent = lines.join(String.fromCharCode(10));
                if (st) st.textContent = '';
                const de = el('diag-enabled');
                if (de) de.checked = !!(d.enabled);
                const dd = el('diag-detailed');
                if (dd) dd.checked = !!(d.detailed);
            } catch (e) {
                if (st) st.textContent = '读取失败: ' + e;
            }
        }
        async function setDiagToggle(key, val) {
            const st = el('diag-status');
            try {
                const r = await apiGet('/api/diag/toggle?key=' + key + '&v=' + (val ? 1 : 0));
                if (!r || !r.ok) { if (st) st.textContent = '保存失败: ' + (r ? r.error : '无响应'); return; }
                if (st) st.textContent = (key === 'enabled' ? '诊断功能' : '详细日志') + '已' + (val ? '开启' : '关闭');
                loadDiag();
            } catch (e) { if (st) st.textContent = '保存失败: ' + e; }
        }
        async function dumpstacks() {
            const st = el('diag-status');
            try {
                const r = await apiGet('/api/diag/stacks');
                if (st) st.textContent = r && r.ok ? r.message : ('失败: ' + (r ? r.error : '无响应'));
            } catch (e) { if (st) st.textContent = '失败: ' + e; }
        }
        console.log('[GM] 前端版本 v2026.08.26-B 已加载');

        function el(id) { return document.getElementById(id); }

        function showStatus(msg, ok) {
            let t = document.getElementById('toast');
            if (!t) {
                t = document.createElement('div');
                t.id = 'toast';
                document.body.appendChild(t);
            }
            t.textContent = (ok ? '✓ ' : '✗ ') + msg;
            t.className = ok ? 'is-ok' : 'is-bad';
            void t.offsetWidth;                       /* 强制重排，保证每次都播放过渡 */
            t.classList.add('show');
            clearTimeout(t.__hide);
            t.__hide = setTimeout(function () { t.classList.remove('show'); }, 3500);
            const s = el('map-status');
            if (s && s.offsetParent !== null) { s.textContent = (ok ? '成功: ' : '失败: ') + msg; s.className = ok ? 'status text-ok' : 'status text-bad'; }
        }

        /* ---------- 主题：默认跟随系统，可手动切换并记忆 ---------- */
        function gmCurrentTheme() {
            var attr = document.documentElement.getAttribute('data-theme');
            if (attr === 'light' || attr === 'dark') return attr;
            return (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches) ? 'light' : 'dark';
        }
        function gmSyncThemeBtn() {
            var list = document.querySelectorAll('.theme-toggle');
            var txt = gmCurrentTheme() === 'light' ? '☀️ 浅色' : '🌙 深色';
            for (var i = 0; i < list.length; i++) list[i].textContent = txt;
        }
        function gmToggleTheme() {
            var next = gmCurrentTheme() === 'light' ? 'dark' : 'light';
            document.documentElement.setAttribute('data-theme', next);
            try { localStorage.setItem('gm-theme', next); } catch (e) {}
            gmSyncThemeBtn();
        }

        // ---------- 问号帮助：把长解释折进浮层，不占版面 ----------
        // 点击 ? 弹出说明；再点、点空白、按 Esc 或滚动都收起。
        var gmHelpPop = null;      // 当前打开的浮层节点
        var gmHelpBtn = null;      // 触发它的按钮

        function gmCloseHelp() {
            if (gmHelpPop && gmHelpPop.parentNode) gmHelpPop.parentNode.removeChild(gmHelpPop);
            if (gmHelpBtn) {
                gmHelpBtn.setAttribute('aria-expanded', 'false');
                gmHelpBtn = null;
            }
            gmHelpPop = null;
        }

        function gmOpenHelp(btn) {
            var text = btn.getAttribute('data-help') || '';
            if (!text) return;
            gmCloseHelp();

            var pop = document.createElement('div');
            pop.className = 'help-pop';
            pop.innerHTML = text;
            // 先挂到 body 量尺寸，再定位，避免闪一下
            pop.style.visibility = 'hidden';
            pop.style.top = '0px';
            pop.style.left = '0px';
            document.body.appendChild(pop);

            var r = btn.getBoundingClientRect();
            var pw = pop.offsetWidth, ph = pop.offsetHeight;
            var vw = window.innerWidth, vh = window.innerHeight;
            var pad = 8;

            var left = r.left;
            if (left + pw > vw - pad) left = vw - pad - pw;
            if (left < pad) left = pad;

            var top = r.bottom + 6;
            // 下方放不下就翻到上方
            if (top + ph > vh - pad) {
                top = r.top - ph - 6;
                if (top < pad) top = Math.max(pad, vh - pad - ph);
            }

            pop.style.left = Math.round(left) + 'px';
            pop.style.top = Math.round(top) + 'px';
            pop.style.visibility = 'visible';

            gmHelpPop = pop;
            gmHelpBtn = btn;
            btn.setAttribute('aria-expanded', 'true');
        }

        // 事件委托：任何带 .help-q 的元素都能用，包括 JS 动态生成的
        document.addEventListener('click', function (ev) {
            var t = ev.target;
            if (!t || !t.closest) return;
            var btn = t.closest('.help-q');
            if (btn) {
                ev.preventDefault();
                ev.stopPropagation();
                if (gmHelpBtn === btn) gmCloseHelp();
                else gmOpenHelp(btn);
                return;
            }
            // 点浮层内部不关
            if (gmHelpPop && gmHelpPop.contains(t)) return;
            gmCloseHelp();
        });
        document.addEventListener('keydown', function (ev) {
            if (ev.key === 'Escape') gmCloseHelp();
            // 键盘可达：回车 / 空格也能触发
            if ((ev.key === 'Enter' || ev.key === ' ') && ev.target && ev.target.classList
                && ev.target.classList.contains('help-q')) {
                ev.preventDefault();
                gmOpenHelp(ev.target);
            }
        });
        window.addEventListener('resize', gmCloseHelp);
        // 滚动时浮层会跟丢位置，直接收起更干净
        window.addEventListener('scroll', function () {
            if (gmHelpPop) gmCloseHelp();
        }, true);

        /* 统一维护 .ios 开关文案，避免「状态变了文字没变」 */
        document.addEventListener('change', function (ev) {
            var t = ev.target;
            if (!t || t.type !== 'checkbox' || !t.closest) return;
            var lab = t.closest('.ios');
            if (!lab) return;
            var txt = lab.querySelector('.ios-text');
            if (txt) txt.textContent = t.checked ? (lab.getAttribute('data-on') || '启用') : (lab.getAttribute('data-off') || '禁用');
        });

        // ---------- 抽屉侧边栏 ----------
        // 默认收起，点标题栏 ☰ 展开；点遮罩、✕ 或任一菜单项后收起。
        function gmOpenSidebar() {
            var sb = el('sidebar'), sc = el('side-scrim');
            if (sb) sb.classList.add('open');
            if (sc) sc.classList.add('open');
            document.body.classList.add('side-open');
        }

        function gmCloseSidebar() {
            var sb = el('sidebar'), sc = el('side-scrim');
            if (sb) sb.classList.remove('open');
            if (sc) sc.classList.remove('open');
            document.body.classList.remove('side-open');
        }

        function gmToggleSidebar() {
            var sb = el('sidebar');
            if (sb && sb.classList.contains('open')) gmCloseSidebar();
            else gmOpenSidebar();
        }

        function gmSidebarOpen() {
            var sb = el('sidebar');
            return !!(sb && sb.classList.contains('open'));
        }

        // ---------- 轮询调度：只让「当前可见板块」的定时器运行 ----------
        // 四个定时器原本无条件常驻，人不在那个板块也会一直发请求。
        // 这里改为按板块挂载/卸载，切走即停，切回立即拉一次。
        var gmTimers = {};          // 板块 id -> [timerId, ...]
        var gmPolls = {};           // 板块 id -> [fn, fn, ...]
        var gmActiveSection = 'sec-players';

        function gmRegisterPoll(sectionId, fn) {
            (gmPolls[sectionId] = gmPolls[sectionId] || []).push(fn);
        }

        function gmStartPolls(sectionId) {
            gmStopPolls(sectionId);
            var fns = gmPolls[sectionId];
            if (!fns) return;
            // ★ 轮询函数多数是 async，try/catch 只能捕获**同步**抛出。
            // 被 reject 的 Promise 会变成 unhandledrejection —— 既没有
            // 用户提示、也很难在控制台定位（尤其 /api 返回 500 时）。
            // 这里统一接住并记一条 error 日志。
            function _safe(fn) {
                try {
                    var r = fn();
                    if (r && typeof r.then === 'function') {
                        r.catch(function (e) { console.error('[轮询] ' + (fn.name || 'anonymous'), e); });
                    }
                } catch (e) {
                    console.error('[轮询] ' + (fn.name || 'anonymous'), e);
                }
            }
            gmTimers[sectionId] = fns.map(function (fn) {
                _safe(fn);
                return setInterval(function () {
                    // 页面不可见（切到别的标签页）时跳过，省电省请求
                    if (document.hidden) return;
                    _safe(fn);
                }, fn.gmInterval || 5000);
            });
        }

        function gmStopPolls(sectionId) {
            (gmTimers[sectionId] || []).forEach(clearInterval);
            delete gmTimers[sectionId];
        }

        function showSection(id, link) {
            document.querySelectorAll('.section').forEach(function (s) { s.style.display = 'none'; });
            const sec = document.getElementById(id);
            if (sec) sec.style.display = 'block';
            document.querySelectorAll('.sidebar a').forEach(function (a) { a.classList.remove('active'); });
            if (link) {
                link.classList.add('active');
                try { window.scrollTo(0, 0); } catch (e) {}
            }
            // 触屏上选完板块就把抽屉收起来，别挡着内容
            gmCloseSidebar();
            gmStopPolls(gmActiveSection);
            gmActiveSection = id;
            gmStartPolls(id);
            try {
                if (id === 'sec-account') loadAccount();
                else if (id === 'sec-cross') loadCross();
                else if (id === 'sec-diag') loadDiag();
                else if (id === 'sec-players') refreshPlayers();
                else if (id === 'sec-gamerules') loadGamerules();
                else if (id === 'sec-bans') loadBans();
                else if (id === 'sec-whitelist') loadWhitelist();
                else if (id === 'sec-properties') loadProperties();
                else if (id === 'sec-worlds') loadWorlds();
                else if (id === 'sec-scoreboard') loadScoreboards();
                else if (id === 'sec-bots') { loadBots(); qrFillCards(); }
                else if (id === 'sec-regex') loadRegex();
                // ★ 模组页**不做自动扫描**：那两个栏位要遍历磁盘上的
                //   resource_packs / behavior_packs 与两个 world_*_packs.json，
                //   每次切进来都扫一遍纯属浪费。改成只渲染上次扫过的结果，
                //   用户点「🔄 刷新」才真正扫一次。
                else if (id === 'sec-mods') renderMods();
                else if (id === 'sec-gmods') loadGmods();
                else if (id === 'sec-files') loadFiles();
                else if (id === 'sec-gametools') loadGameTools();
                else if (id === 'sec-console') pollConsole();
                // 云黑板块：进板块就把统计与明细拉一次（都是只读查询，
                // 新版接口下是两次很轻的 GET）。旧版服务会返回友好提示，
                // 不弹错误。
                else if (id === 'sec-cloud') { loadCloudStats(); loadCloudBans(); }
            } catch (e) { /* 忽略板块加载错误 */ }
        }

        function switchLogTab(which) {
            const chatTab = el('tab-chat'), conTab = el('tab-console');
            const chatPane = el('chat-pane'), conPane = el('console-pane');
            if (!chatTab || !conTab || !chatPane || !conPane) return;

            // ★ 显示/隐藏必须**同时**管住 style.display 和 .hidden 类。
            //
            //   历史上 console-pane 上同时挂了 .hidden 类（CSS 里是
            //   `.hidden { display: none }`）和 style="display:none"。
            //   而这里原本只改内联样式：把 style.display 清成 '' 之后，
            //   元素的 display 回退到 CSS 层，.hidden 的 display:none 立刻
            //   接管 —— 于是「点一下控制台标签，内容框就消失」。
            //   聊天栏没有 .hidden 类，所以一直是好的。
            //
            //   两套机制并存迟早会打架。这里统一走 syncPane()，两边一起设，
            //   并把 .hidden 当作唯一真相来源之外的冗余标记清掉。
            function syncPane(pane, visible) {
                if (visible) {
                    pane.classList.remove('hidden');
                    pane.style.display = '';
                } else {
                    pane.classList.add('hidden');
                    // 显式设 none 而不是留空：留空会回退到 CSS，
                    // 万一 .hidden 被别的样式覆盖就藏不住了。
                    pane.style.display = 'none';
                }
            }

            if (which === 'chat') {
                chatTab.classList.add('active'); conTab.classList.remove('active');
                syncPane(chatPane, true); syncPane(conPane, false);
                // 切过去立刻拉一次，否则要等下一个 2s 轮询才出内容
                pollMessages('message-panel', 'msg-autoscroll');
            } else {
                conTab.classList.add('active'); chatTab.classList.remove('active');
                syncPane(chatPane, false); syncPane(conPane, true);
                pollConsole('console-panel', 'console-autoscroll');
            }
        }

        // —— 服务器状态仪表盘（液面玻璃圆环） ——
        var GAUGE_C = 2 * Math.PI * 50;
        function setGauge(ringId, txtId, pct, kind, unit) {
            pct = Number(pct); if (!(pct >= 0)) pct = 0; if (pct > 100) pct = 100;
            var ring = el(ringId), txt = el(txtId);
            if (!ring) return;
            ring.style.strokeDasharray = GAUGE_C.toFixed(2);
            ring.style.strokeDashoffset = (GAUGE_C * (1 - pct / 100)).toFixed(2);
            var color = pct >= 85 ? '#ff6b81'
                      : (pct >= 60 ? '#ffd166'
                      : (kind === 'mem' ? '#4ea8ff' : (kind === 'disk' ? '#b48cff' : '#4ef09a')));
            ring.style.stroke = color;
            if (txt) txt.textContent = (pct % 1 === 0 ? pct.toFixed(0) : pct.toFixed(1)) + (unit || '%');
        }
        function fmtUptimeHMS(secs) {
            secs = Math.max(0, Math.floor(Number(secs) || 0));
            var d = Math.floor(secs / 86400), h = Math.floor((secs % 86400) / 3600), m = Math.floor((secs % 3600) / 60);
            var s = secs % 60, parts = [];
            if (d) parts.push(d + '天');
            if (h) parts.push(h + '时');
            if (m) parts.push(m + '分');
            if (s !== 0 || parts.length === 0) parts.push(s + '秒');
            return parts.join('');
        }
        function updateGauges() {
            apiGet('/api/system/stats').then(function (r) {
                if (!r || !r.stats) return;
                var s = r.stats;
                setGauge('gauge-cpu', 'gauge-cpu-txt', s.cpu, 'cpu', '%');
                setGauge('gauge-mem', 'gauge-mem-txt', s.mem, 'mem', '%');
                setGauge('gauge-disk', 'gauge-disk-txt', s.disk, 'disk', '%');
                var line = el('metric-uptime');
                if (line) {
                    line.textContent = '运行时长：' + fmtUptimeHMS(s.uptime)
                        + '　·　内存 ' + (Number(s.mem_used_mb) || 0) + ' / ' + (Number(s.mem_total_mb) || 0) + ' MB'
                        + '　·　磁盘 ' + (Number(s.disk_used_gb) || 0) + ' / ' + (Number(s.disk_total_gb) || 0) + ' GB';
                }
                renderEnv(s.env);
            }).catch(function () {});
        }

        // 运行环境：给用户一个「该下哪个包」的明确标识
        function renderEnv(env) {
            var box = el('pane-env');
            if (!env || !env.os_name) {
                if (box) box.classList.add('hidden');
                return;
            }
            if (box) box.classList.remove('hidden');
            function put(id, v) { var e = el(id); if (e) e.textContent = v || '未知'; }
            put('env-os', env.os_detail || env.os_name);
            put('env-arch',
                (env.arch || '未知') + (env.bits ? '（' + env.bits + ' 位）' : ''));
            put('env-py', env.python || '未知');
            put('env-bits', env.bits ? env.bits + ' 位' : '未知');

            var tag = el('env-pip-target');
            if (tag) tag.textContent = env.pip_target || '—';

            var hint = el('env-hint');
            if (hint) {
                var parts = [];
                if (env.lib_bundle) {
                    parts.push('面板离线库包请选 <b>' + esc(env.lib_bundle) + '</b>');
                }
                if (env.libext) {
                    parts.push('本机动态库后缀为 <b>' + esc(env.libext) + '</b>');
                }
                hint.innerHTML = parts.length
                    ? parts.join('　·　')
                    : '按上面的标识下载即可。';
            }
        }

        // 复制下载标识。navigator.clipboard 在 http:// （非 https）下不可用，
        // 所以留一个 execCommand 的兜底 —— 面板常常是内网 http 访问的。
        function copyEnvTag(id) {
            var node = el(id);
            var st = el('env-copy-status');
            var text = node ? (node.textContent || '').trim() : '';
            if (!text || text === '—') {
                if (st) st.textContent = '暂无可复制的标识';
                return;
            }
            function done(ok) {
                if (!st) return;
                st.textContent = ok ? '✓ 已复制' : '✗ 复制失败，请手动选中';
                st.className = 'status ' + (ok ? 'text-ok' : 'text-bad');
                setTimeout(function () { if (st) st.textContent = ''; }, 2000);
            }
            function fallback() {
                try {
                    var ta = document.createElement('textarea');
                    ta.value = text;
                    ta.setAttribute('readonly', '');
                    ta.style.position = 'fixed';
                    ta.style.left = '-9999px';
                    document.body.appendChild(ta);
                    ta.select();
                    var ok = document.execCommand('copy');
                    document.body.removeChild(ta);
                    done(ok);
                } catch (e) { done(false); }
            }
            try {
                if (navigator.clipboard && navigator.clipboard.writeText) {
                    navigator.clipboard.writeText(text).then(function () { done(true); }, fallback);
                } else {
                    fallback();
                }
            } catch (e) { fallback(); }
        }

        // 兼容反向代理子路径（如 https://host/server/admin）：脚本内所有请求统一走 apiPath 计算前缀
        var API_BASE = (function () {
            var p = location.pathname;
            if (p.charAt(p.length - 1) !== '/') p = p.slice(0, p.lastIndexOf('/') + 1);
            return p;
        })();
        function apiPath(u) { return API_BASE + u.replace(/^\//, ''); }

        // 统一解析响应：非 2xx 时也不再静默失败，而是把状态码和错误带回去
        async function apiParse(res) {
            let data = null;
            try { data = await res.json(); } catch (e) { data = null; }
            if (data && typeof data === 'object') {
                if (!res.ok && !data.error) data.error = 'HTTP ' + res.status;
                data.__status = res.status;
                return data;
            }
            return { ok: false, __status: res.status,
                     error: res.ok ? '响应格式错误' : ('HTTP ' + res.status) };
        }

        async function apiGet(url) {
            const res = await fetch(apiPath(url));
            return await apiParse(res);
        }

        async function apiPost(url, body) {
            const res = await fetch(apiPath(url), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body)
            });
            return await apiParse(res);
        }

        // ---------- 登录身份与权限 ----------
        var gmAuth = { role: 'admin', scopes: ['*'], all_scopes: [], loaded: false };

        // 判断当前账户是否拥有某个 scope（data-scope 支持逗号分隔的多值）
        function gmHasScope(spec) {
            if (!spec) return true;                       // 未标注 = 公共入口
            if (gmAuth.role === 'admin') return true;
            const mine = gmAuth.scopes || [];
            if (mine.indexOf('*') >= 0 || mine.indexOf('__any__') >= 0) return true;
            const need = String(spec).split(',').map(function (s) { return s.trim(); }).filter(Boolean);
            if (!need.length) return true;
            for (var i = 0; i < need.length; i++) {
                if (mine.indexOf(need[i]) >= 0) return true;
            }
            return false;
        }

        async function gmLoadAuth() {
            try {
                const r = await apiGet('/api/auth/me');
                if (r && r.ok) {
                    gmAuth.role = r.role || 'admin';
                    gmAuth.scopes = Array.isArray(r.scopes) ? r.scopes : [];
                    gmAuth.all_scopes = Array.isArray(r.all_scopes) ? r.all_scopes : [];
                    gmAuth.loaded = true;
                }
            } catch (e) { console.error('gmLoadAuth', e); }
            gmApplyScopes();
        }

        // 按权限隐藏侧边栏入口；顺带隐藏无权限的板块本体
        function gmApplyScopes() {
            if (gmAuth.role === 'admin') return;          // 管理员不做任何隐藏
            const links = document.querySelectorAll('.sidebar a[data-scope]');
            for (var i = 0; i < links.length; i++) {
                var a = links[i];
                var ok = gmHasScope(a.getAttribute('data-scope'));
                a.style.display = ok ? '' : 'none';
            }
        }

        // 选择首个有权限的板块作为落地页；无任何权限则给出明确提示
        function gmSelectInitialSection() {
            const links = document.querySelectorAll('.sidebar a');
            var firstVisible = null;
            for (var i = 0; i < links.length; i++) {
                if (links[i].style.display !== 'none') { firstVisible = links[i]; break; }
            }
            if (firstVisible) {
                var m = /showSection\('([^']+)'/.exec(firstVisible.getAttribute('onclick') || '');
                showSection(m ? m[1] : 'sec-players', firstVisible);
                return true;
            }
            // 没有任何可见入口：给出可见提示，而不是留一个空白页
            var main = document.querySelector('.main');
            if (main) {
                document.querySelectorAll('.section').forEach(function (s) { s.style.display = 'none'; });
                var tip = document.createElement('div');
                tip.className = 'section';
                tip.innerHTML = '<h2>无可用板块</h2><p class="hint">当前账户未分配任何可访问板块的权限，请联系管理员在「账户与安全 → 临时账户」中重新分配。</p>';
                main.appendChild(tip);
            }
            return false;
        }

        var filesCwd = '';
        var filesEditPath = null;

        function fsJoin(a, b) { a = String(a == null ? '' : a); b = String(b == null ? '' : b); if (!a) return b; return a.replace(/\/+$/, '') + '/' + b; }
        function fsParent(p) { p = String(p == null ? '' : p); var i = p.lastIndexOf('/'); return i <= 0 ? '' : p.slice(0, i); }
        function fsEscJs(s) { return String(s == null ? '' : s).replace(/\\/g, '\\\\').replace(/'/g, "\\'"); }
        function fsSize(v) { v = Number(v) || 0; var u = ['B','KB','MB','GB','TB']; var i = 0; while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; } return (i ? v.toFixed(1) : v) + ' ' + u[i]; }
        function fsTime(t) { if (!t) return '—'; var d = new Date(t * 1000); var p = function (x) { return (x < 10 ? '0' : '') + x; }; return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes()); }
        function fsMsg(m) { try { el('files-status').textContent = m; } catch (e) {} }

        async function loadFiles() {
            try {
                const r = await apiGet('/api/files/list?path=' + encodeURIComponent(filesCwd));
                if (!r || !r.ok) { fsMsg((r && r.error) || '加载失败'); return; }
                filesCwd = r.cwd || '';
                renderFiles(r.entries || []);
                fsMsg('路径：' + (filesCwd === '' ? '根目录' : filesCwd));
            } catch (e) { fsMsg('加载失败'); }
        }

        function filesJump(p) { filesCwd = p || ''; filesSelected.clear(); loadFiles(); }
        function filesGoUp() { filesCwd = fsParent(filesCwd); filesSelected.clear(); loadFiles(); }

        // ---------- 选择与工具栏状态 ----------
        var filesEntries = [];      // 当前目录的条目缓存
        var filesSelected = new Set();  // 已勾选的名称

        function filesGetSelected() {
            return filesEntries.filter(function (it) { return filesSelected.has(it.name); });
        }

        function filesSelectAll(checked) {
            filesSelected.clear();
            if (checked) filesEntries.forEach(function (it) { filesSelected.add(it.name); });
            syncFileCheckboxes();
            filesRenderToolbar();
        }

        function filesToggle(name, checked) {
            if (checked) filesSelected.add(name); else filesSelected.delete(name);
            syncFileCheckboxes();
            filesRenderToolbar();
        }

        // 把选中状态同步到 DOM（复选框、行高亮、全选框）
        function syncFileCheckboxes() {
            const rows = document.querySelectorAll('#files-list tr[data-fs-name]');
            for (var i = 0; i < rows.length; i++) {
                var tr = rows[i];
                var name = tr.getAttribute('data-fs-name');
                var on = filesSelected.has(name);
                var cb = tr.querySelector('input[type="checkbox"]');
                if (cb) cb.checked = on;
                tr.classList.toggle('fs-selected', on);
            }
            const all = el('files-check-all');
            if (all) {
                all.checked = filesEntries.length > 0 && filesSelected.size === filesEntries.length;
                all.indeterminate = filesSelected.size > 0 && filesSelected.size < filesEntries.length;
            }
        }

        function filesRenderToolbar() {
            const n = filesSelected.size;
            const cnt = el('files-sel-count');
            if (cnt) {
                cnt.textContent = n ? ('已选择 ' + n + ' 项') : '未选择';
                cnt.className = 'fs-sel-count' + (n ? ' on' : '');
            }
            const btns = document.querySelectorAll('[data-fs-act]');
            const sel = filesGetSelected();
            for (var i = 0; i < btns.length; i++) {
                var act = btns[i].getAttribute('data-fs-act');
                var ok = n > 0;
                // 查看仅对单个文件可用；重命名同样只允许单选
                if (act === 'view') ok = sel.length === 1 && !sel[0].is_dir;
                if (act === 'rename') ok = sel.length === 1;
                btns[i].disabled = !ok;
            }
        }

        // 顶部工具栏按钮统一入口
        function filesApplyAction(act) {
            const sel = filesGetSelected();
            if (!sel.length) { fsMsg('请先勾选要操作的文件'); return; }
            if (act === 'view') { filesOpen(sel[0].name, false); return; }
            if (act === 'download') { sel.forEach(function (it) { if (!it.is_dir) filesDown(it.name); }); return; }
            if (act === 'rename') { filesRename(sel[0].name); return; }
            if (act === 'copy') { filesCopyMany(sel); return; }
            if (act === 'move') { filesMoveMany(sel); return; }
            if (act === 'delete') { filesDeleteMany(sel); return; }
        }

        function renderFiles(list) {
            filesEntries = list || [];
            // 目录切换后清理已不在当前目录的选中项
            const names = {};
            filesEntries.forEach(function (it) { names[it.name] = true; });
            filesSelected.forEach(function (n) { if (!names[n]) filesSelected.delete(n); });

            let bc = el('files-breadcrumb');
            let crumbs = '<a href="javascript:void(0)" onclick="filesJump(\'\')">根目录</a>';
            let acc = '';
            (filesCwd ? filesCwd.split('/') : []).forEach(function (seg) {
                acc = acc ? acc + '/' + seg : seg;
                crumbs += ' / <a href="javascript:void(0)" onclick="filesJump(' + jsArg(acc) + ')">' + esc(seg) + '</a>';
            });
            bc.innerHTML = crumbs;
            const tb = el('files-list');
            tb.innerHTML = '';
            list.forEach(function (it) {
                const tr = document.createElement('tr');
                tr.setAttribute('data-fs-name', it.name);

                // 首列：复选框
                const c0 = document.createElement('td');
                c0.className = 'fs-check';
                const cb = document.createElement('input');
                cb.type = 'checkbox';
                cb.checked = filesSelected.has(it.name);
                cb.setAttribute('aria-label', '选择 ' + it.name);
                cb.onclick = function (ev) { ev.stopPropagation(); filesToggle(it.name, cb.checked); };
                c0.appendChild(cb);

                const c1 = document.createElement('td');
                c1.innerHTML = (it.is_dir ? '📁 ' : '📄 ') + esc(it.name);
                c1.classList.add('pointer');
                c1.title = it.is_dir ? '双击进入目录' : '双击编辑';
                c1.ondblclick = function () { filesOpen(it.name, it.is_dir); };

                const c2 = document.createElement('td'); c2.textContent = it.is_dir ? '目录' : '文件';
                const c3 = document.createElement('td'); c3.textContent = it.is_dir ? '—' : fsSize(it.size);
                const c4 = document.createElement('td'); c4.textContent = fsTime(it.mtime);
                const c5 = document.createElement('td');
                // 行内仅保留最常用的两个快捷操作，其余统一走顶部工具栏
                const quick = document.createElement('button');
                quick.type = 'button';
                quick.className = 'btn-xs';
                quick.textContent = it.is_dir ? '进入' : '打开';
                quick.onclick = function () { filesOpen(it.name, it.is_dir); };
                c5.appendChild(quick);

                tr.appendChild(c0); tr.appendChild(c1); tr.appendChild(c2);
                tr.appendChild(c3); tr.appendChild(c4); tr.appendChild(c5);
                // 点行任意处也可切换勾选（复选框与按钮已 stopPropagation）
                tr.onclick = function (ev) {
                    if (ev.target && (ev.target.tagName === 'BUTTON' || ev.target.tagName === 'INPUT')) return;
                    filesToggle(it.name, !filesSelected.has(it.name));
                };
                tb.appendChild(tr);
            });
            if (!list.length) tb.innerHTML = '<tr><td colspan="6" class="empty">（空目录）</td></tr>';
            syncFileCheckboxes();
            filesRenderToolbar();
        }

        function filesOpen(name, isDir) {
            if (isDir) { filesCwd = fsJoin(filesCwd, name); loadFiles(); return; }
            const p = fsJoin(filesCwd, name);
            apiGet('/api/files/read?path=' + encodeURIComponent(p)).then(function (r) {
                if (!r || !r.ok) { alert((r && r.error) || '读取失败'); return; }
                el('files-editor-name').textContent = r.name;
                el('files-editor-path').textContent = '路径：' + r.path;
                el('files-editor-area').value = r.content;
                filesEditPath = r.path;
                el('files-editor').style.display = 'block';
            });
        }
        function filesCloseEditor() { filesEditPath = null; el('files-editor').style.display = 'none'; }
        async function filesSaveEditor() {
            if (!filesEditPath) return;
            const r = await apiPost('/api/files/write', { path: filesEditPath, content: el('files-editor-area').value });
            fsMsg((r && r.message) || (r && r.error) || (r && r.ok ? '已保存' : '保存失败'));
        }
        function filesDown(name) {
            window.location.href = apiPath('/api/files/download?path=' + encodeURIComponent(fsJoin(filesCwd, name)));
        }
        async function filesNewFolder() {
            const n = prompt('输入新文件夹名称：');
            if (!n) return;
            const r = await apiPost('/api/files/mkdir', { path: fsJoin(filesCwd, n.trim()) });
            fsMsg((r && r.message) || (r && r.error) || '操作失败');
            if (r && r.ok) loadFiles();
        }
        async function filesRename(name) {
            const n = prompt('输入新名称：', name);
            if (!n || n === name) return;
            const r = await apiPost('/api/files/rename', { path: fsJoin(filesCwd, name), newname: n.trim() });
            fsMsg((r && r.message) || (r && r.error) || '操作失败');
            if (r && r.ok) loadFiles();
        }
        async function filesDelete(name) {
            if (!confirm('确认删除「' + name + '」？目录将递归删除，无法撤销！')) return;
            const r = await apiPost('/api/files/delete', { path: fsJoin(filesCwd, name) });
            fsMsg((r && r.message) || (r && r.error) || '操作失败');
            if (r && r.ok) loadFiles();
        }

        // ---------- 批量操作 ----------
        async function filesDeleteMany(items) {
            const names = items.map(function (it) { return it.name; });
            const hasDir = items.some(function (it) { return it.is_dir; });
            const tip = '确认删除以下 ' + names.length + ' 项？' +
                (hasDir ? '\n（含目录，将递归删除）' : '') + '\n\n' + names.join('\n') + '\n\n此操作无法撤销！';
            if (!confirm(tip)) return;
            let ok = 0, fail = 0;
            for (var i = 0; i < names.length; i++) {
                try {
                    const r = await apiPost('/api/files/delete', { path: fsJoin(filesCwd, names[i]) });
                    if (r && r.ok) ok++; else fail++;
                } catch (e) { fail++; }
            }
            fsMsg('删除完成：成功 ' + ok + ' 项' + (fail ? ('，失败 ' + fail + ' 项') : ''));
            filesSelected.clear();
            loadFiles();
        }

        async function filesCopyMany(items) {
            const names = items.map(function (it) { return it.name; });
            if (names.length === 1) { filesCopy(names[0]); return; }
            const d = prompt('把这 ' + names.length + ' 项复制到哪个目录？（相对服务器根目录，例如 backup/copy）');
            if (!d) return;
            let ok = 0, fail = 0;
            for (var i = 0; i < names.length; i++) {
                try {
                    const r = await apiPost('/api/files/copy',
                        { src: fsJoin(filesCwd, names[i]), dst: fsJoin(d.trim(), names[i]) });
                    if (r && r.ok) ok++; else fail++;
                } catch (e) { fail++; }
            }
            fsMsg('复制完成：成功 ' + ok + ' 项' + (fail ? ('，失败 ' + fail + ' 项') : ''));
            filesSelected.clear();
            loadFiles();
        }

        async function filesMoveMany(items) {
            const names = items.map(function (it) { return it.name; });
            if (names.length === 1) { filesMove(names[0]); return; }
            const d = prompt('把这 ' + names.length + ' 项移动到哪个目录？（相对服务器根目录，例如 backup/new）');
            if (!d) return;
            let ok = 0, fail = 0;
            for (var i = 0; i < names.length; i++) {
                try {
                    const r = await apiPost('/api/files/move',
                        { src: fsJoin(filesCwd, names[i]), dst: fsJoin(d.trim(), names[i]) });
                    if (r && r.ok) ok++; else fail++;
                } catch (e) { fail++; }
            }
            fsMsg('移动完成：成功 ' + ok + ' 项' + (fail ? ('，失败 ' + fail + ' 项') : ''));
            filesSelected.clear();
            loadFiles();
        }

        async function filesCopy(name) {
            const d = prompt('输入目标完整路径（相对服务器根目录，例如 backup/copy）：');
            if (!d) return;
            const r = await apiPost('/api/files/copy', { src: fsJoin(filesCwd, name), dst: d.trim() });
            fsMsg((r && r.message) || (r && r.error) || '操作失败');
            if (r && r.ok) loadFiles();
        }
        async function filesMove(name) {
            const d = prompt('输入目标完整路径（相对服务器根目录，例如 backup/new）：');
            if (!d) return;
            const r = await apiPost('/api/files/move', { src: fsJoin(filesCwd, name), dst: d.trim() });
            fsMsg((r && r.message) || (r && r.error) || '操作失败');
            if (r && r.ok) loadFiles();
        }
        async function filesUpload() {
            const inp = el('files-upload');
            if (!inp.files || !inp.files.length) return;
            const fd = new FormData();
            fd.append('dir', filesCwd);
            for (let i = 0; i < inp.files.length; i++) fd.append('file', inp.files[i]);
            fsMsg('上传中…');
            try {
                const res = await fetch(apiPath('/api/files/upload'), { method: 'POST', body: fd });
                const r = await res.json();
                fsMsg((r && r.message) || (r && r.error) || '上传失败');
                if (r && r.ok) { inp.value = ''; loadFiles(); }
            } catch (e) { fsMsg('上传失败'); }
        }

        function updateAllSelects() {
            const ids = ['msg-player','give-player','health-player','tag-player','score-player','detail-player-select','kick-player','op-player','tp-player','map-player','perm-player','ban-online'];
            ids.forEach(id => {
                const sel = el(id);
                if (!sel) return;
                const prev = sel.value;
                sel.innerHTML = '<option value="">-- 玩家 --</option>';
                currentPlayers.forEach(p => {
                    const o = document.createElement('option');
                    o.value = p.name;
                    o.textContent = p.name;
                    sel.appendChild(o);
                });
                if (prev && currentPlayers.some(p => p.name === prev)) sel.value = prev;
            });
        }

        async function refreshPlayers() {
            try {
                const data = await apiGet('/api/players');
                currentPlayers = data.players || [];
                const tbody = el('player-list');
                if (!tbody) return;   // 板块结构未就绪时不抛错
                tbody.innerHTML = '';
                currentPlayers.forEach(p => {
                    const tr = document.createElement('tr');
                    // ⚠ 玩家名是可控输入（玩家可自由取名，含 < > & 等字符）。
                    // 这里曾经是 innerHTML 直接拼 String(p.name)：
                    // 一个叫 <img src=x onerror=...> 的玩家上线即可在管理员
                    // 面板里执行脚本。必须走 esc()。同文件其它地方（如玩家
                    // 详情）本来就用了 esc，唯独这里漏了。
                    const c1 = document.createElement('td'); c1.innerHTML = '<span class="online">●</span> ' + esc(p.name);
                    const c2 = document.createElement('td'); c2.textContent = Math.round(p.x) + ', ' + Math.round(p.y) + ', ' + Math.round(p.z);
                    const c3 = document.createElement('td'); c3.textContent = p.dimension;
                    const c4 = document.createElement('td'); c4.textContent = p.health + '/' + (p.max_health != null ? p.max_health : '?');
                    const c5 = document.createElement('td'); c5.textContent = p.is_op ? 'OP' : '';
                    const c6 = document.createElement('td');
                    const btn = document.createElement('button');
                    btn.className = 'kill';
                    btn.textContent = '击杀';
                    btn.onclick = function () { killPlayer(p.name); };
                    c6.appendChild(btn);
                    tr.appendChild(c1); tr.appendChild(c2); tr.appendChild(c3);
                    tr.appendChild(c4); tr.appendChild(c5); tr.appendChild(c6);
                    tbody.appendChild(tr);
                });
                updateAllSelects();
                const dsel = el('detail-player-select');
                if (dsel && dsel.value) loadPlayerDetail();
            } catch (e) { console.error('refreshPlayers', e); }
        }

        async function loadScoreboards() {
            try {
                const data = await apiGet('/api/scoreboards');
                const sel = el('score-objective');
                if (!sel) return;
                const prev = sel.value;
                const names = data.objectives || [];
                sel.innerHTML = '<option value="">-- 目标 --</option>';
                names.forEach(n => {
                    const o = document.createElement('option');
                    o.value = n; o.textContent = n; sel.appendChild(o);
                });
                if (prev && names.includes(prev)) sel.value = prev;
            } catch (e) { console.error('loadScoreboards', e); }
        }

        async function loadPlayerDetail() {
            const sel = el('detail-player-select');
            const area = el('detail-area');
            if (!area) return;
            const name = sel ? sel.value : '';
            if (!name) { area.innerHTML = ''; return; }
            try {
                const res = await fetch(apiPath('/api/player/' + encodeURIComponent(name)));
                if (res.status === 404) { area.innerHTML = '<p>玩家不在线</p>'; return; }
                const p = await res.json();
                // tags / scores 的键与值都来自游戏内数据（玩家可写记分板），
                // 必须逐个 esc，否则记分板名可以塞 <img onerror=...>。
                const tags = (p.tags || []).map(t => '<span class="tag-badge">' + esc(t) + '</span>').join(' ') || '（无）';
                const scores = Object.entries(p.scores || {}).map(function (e) { return '<span class="score-item"><b>' + esc(e[0]) + '</b>: ' + esc(e[1]) + '</span>'; }).join('') || '（无）';
                area.innerHTML = '<div class="player-detail">' +
                    '<p><b>玩家:</b> ' + esc(p.name) + '</p>' +
                    '<p><b>XUID:</b> <span class="uuid-text">' + esc(p.xuid || '（无）') + '</span></p>' +
                    '<p><b>生命值:</b> ' + esc(p.health) + '/' + esc(p.max_health) + '</p>' +
                    '<p><b>OP:</b> ' + (p.is_op ? '是' : '否') + '</p>' +
                    '<p><b>权限级别:</b> ' + (p.permission_level === 2 ? 'CONSOLE' : (p.permission_level === 1 ? 'OP' : 'DEFAULT')) + '</p>' +
                    '<p><b>标签:</b> ' + tags + '</p>' +
                    '<p><b>记分板:</b> ' + scores + '</p>' +
                    '</div>';
            } catch (e) { console.error('loadPlayerDetail', e); }
        }

        async function killPlayer(name) {
            const r = await apiPost('/api/kill', { player_name: name });
            showStatus(r.message || r.error, !!r.success);
            refreshPlayers();
        }

        async function sendMessage() {
            const r = await apiPost('/api/message', { player_name: el('msg-player').value, message: el('msg-text').value });
            showStatus(r.message || r.error, !!r.success);
        }

        async function giveItem() {
            const r = await apiPost('/api/give', { player_name: el('give-player').value, item_id: el('give-item').value, amount: parseInt(el('give-amount').value, 10) || 1 });
            showStatus(r.message || r.error, !!r.success);
        }

        async function adjustHealth() {
            const r = await apiPost('/api/health', { player_name: el('health-player').value, operation: el('health-op').value, value: parseInt(el('health-value').value, 10) });
            showStatus(r.message || r.error, !!r.success);
        }

        async function doTag(action) {
            const r = await apiPost('/api/tag', { player_name: el('tag-player').value, tag: el('tag-name').value, action: action });
            showStatus(r.message || r.error, !!r.success);
            loadPlayerDetail();
        }
        function addTag() { doTag('add'); }
        function removeTag() { doTag('remove'); }

        async function setScore() {
            const r = await apiPost('/api/score', { player_name: el('score-player').value, objective: el('score-objective').value, score: parseInt(el('score-value').value, 10), operation: el('score-op').value });
            showStatus(r.message || r.error, !!r.success);
        }

        async function kickPlayer() {
            const r = await apiPost('/api/kick', { player_name: el('kick-player').value, reason: el('kick-reason').value || '被管理员踢出' });
            showStatus(r.message || r.error, !!r.success);
            refreshPlayers();
        }

        async function setOp() {
            const r = await apiPost('/api/op', { player_name: el('op-player').value, value: el('op-value').value === 'true' });
            showStatus(r.message || r.error, !!r.success);
            refreshPlayers();
        }

        async function teleportPlayer() {
            const r = await apiPost('/api/teleport', { player_name: el('tp-player').value, x: parseFloat(el('tp-x').value), y: parseFloat(el('tp-y').value), z: parseFloat(el('tp-z').value), dimension: el('tp-dimension').value });
            showStatus(r.message || r.error, !!r.success);
        }

        // 在前端把图片转成 128x128、24 位、无压缩、自下而上的 BMP（BI_RGB）
        function buildBmp24(imageData, width, height) {
            const rowSize = (width * 3 + 3) & ~3;      // 128*3=384，无需填充
            const pixelDataSize = rowSize * height;
            const fileSize = 54 + pixelDataSize;
            const buf = new ArrayBuffer(fileSize);
            const dv = new DataView(buf);
            dv.setUint8(0, 0x42);          // 'B'
            dv.setUint8(1, 0x4D);          // 'M'
            dv.setUint32(2, fileSize, true);
            dv.setUint32(10, 54, true);    // 数据偏移
            dv.setUint32(14, 40, true);    // BITMAPINFOHEADER 大小
            dv.setInt32(18, width, true);
            dv.setInt32(22, height, true); // 正高度 = 自下而上
            dv.setUint16(26, 1, true);     // planes
            dv.setUint16(28, 24, true);    // 24bpp
            dv.setUint32(30, 0, true);     // BI_RGB 无压缩
            dv.setUint32(34, pixelDataSize, true);
            dv.setInt32(38, 2835, true);   // 水平分辨率（可选）
            dv.setInt32(42, 2835, true);   // 垂直分辨率（可选）
            const data = imageData.data;
            let offset = 54;
            // BMP 自下而上：文件第一行 = 图像最底部一行
            for (let y = height - 1; y >= 0; y--) {
                for (let x = 0; x < width; x++) {
                    const idx = (y * width + x) * 4;
                    dv.setUint8(offset++, data[idx + 2]);     // R
                    dv.setUint8(offset++, data[idx + 1]);     // G
                    dv.setUint8(offset++, data[idx]);         // B
                }
            }
            return buf;
        }

        async function fileToBmpBlob(file) {
            const img = await new Promise(function (resolve, reject) {
                const url = URL.createObjectURL(file);
                const im = new Image();
                im.onload = function () { URL.revokeObjectURL(url); resolve(im); };
                im.onerror = function (e) { URL.revokeObjectURL(url); reject(e); };
                im.src = url;
            });
            const SIZE = 128;
            const canvas = document.createElement('canvas');
            canvas.width = SIZE; canvas.height = SIZE;
            const ctx = canvas.getContext('2d');
            ctx.fillStyle = '#000';
            ctx.fillRect(0, 0, SIZE, SIZE);
            // cover：居中裁剪成正方形再缩放到 128x128
            const scale = Math.max(SIZE / img.width, SIZE / img.height);
            const dw = img.width * scale;
            const dh = img.height * scale;
            ctx.drawImage(img, (SIZE - dw) / 2, (SIZE - dh) / 2, dw, dh);
            const imageData = ctx.getImageData(0, 0, SIZE, SIZE);
            return new Blob([buildBmp24(imageData, SIZE, SIZE)], { type: 'image/bmp' });
        }

        async function sendMap() {
            const file = el('map-image').files[0];
            if (!el('map-player').value || !file) { showStatus('请选择玩家并选择图片', false); return; }
            showStatus('正在转码为 128x128 BMP…', true);
            try {
                const bmpBlob = await fileToBmpBlob(file);
                const fd = new FormData();
                fd.append('player', el('map-player').value);
                fd.append('image', bmpBlob, 'map.bmp');
                const res = await fetch(apiPath('/api/map'), { method: 'POST', body: fd });
                const text = await res.text();
                showStatus(text, res.ok);
            } catch (e) { showStatus('转码或发送失败: ' + e, false); }
        }

        async function loadGamerules() {
            try {
                const data = await apiGet('/api/gamerules');
                renderGamerules(data.gamerules || []);
            } catch (e) { console.error('loadGamerules', e); }
        }

        function renderGamerules(rules) {
            const area = el('gamerule-area');
            if (!area) return;
            const cats = {};
            rules.forEach(function (r) {
                const c = r.category || '其他';
                (cats[c] = cats[c] || []).push(r);
            });
            area.innerHTML = '';
            Object.keys(cats).forEach(function (cat) {
                const block = document.createElement('div');
                block.className = 'card';
                block.innerHTML = '<h3 class="mt-xs">' + esc(cat) + '</h3>';
                cats[cat].forEach(function (r) {
                    const row = document.createElement('div');
                    row.className = 'list-item';
                    row.title = r.desc || '';
                    const lab = document.createElement('label');
                    lab.className = 'spacer';
                    lab.innerHTML = '<b class="text-strong">' + esc(r.label) + '</b><br><small class="hint-sm">' + esc(r.name) + '</small>';
                    if (r.type === 'bool') {
                        const wrap = document.createElement('label');
                        wrap.className = 'ios';
                        const sw = document.createElement('input');
                        sw.type = 'checkbox';
                        sw.checked = !!r.value;
                        sw.onchange = function () { setGamerule(r.name, sw.checked, sw); };
                        wrap.appendChild(sw);
                        wrap.appendChild(document.createElement('i'));
                        row.appendChild(lab);
                        row.appendChild(wrap);
                    } else {
                        const inp = document.createElement('input');
                        inp.type = 'number';
                        inp.value = r.value;
                        inp.className = 'w-input-sm';
                        if (r.min !== undefined) inp.min = r.min;
                        inp.onchange = function () { setGamerule(r.name, parseInt(inp.value, 10), inp); };
                        row.appendChild(lab);
                        row.appendChild(inp);
                    }
                    block.appendChild(row);
                });
                area.appendChild(block);
            });
        }

        async function setGamerule(name, value, ctrl) {
            const st = el('gamerule-status');
            if (typeof value === 'number' && isNaN(value)) {
                if (st) { st.textContent = '✗ 请输入有效数字'; st.className = 'status text-bad'; }
                return;
            }
            if (st) { st.textContent = '… 正在设置 ' + name; st.className = 'status'; }
            try {
                const r = await apiPost('/api/gamerule', { name: name, value: value });
                if (r.success) {
                    if (st) { st.textContent = '✓ ' + r.message; st.className = 'status text-ok'; }
                } else {
                    if (st) { st.textContent = '✗ ' + (r.error || r.message); st.className = 'status text-bad'; }
                    if (ctrl && ctrl.type === 'checkbox') { ctrl.checked = !value; }
                    else { loadGamerules(); }
                }
            } catch (e) {
                if (st) { st.textContent = '✗ 请求失败: ' + e; st.className = 'status text-bad'; }
                loadGamerules();
            }
        }

        const SLOT_LABELS = { 'SIDE_BAR': '侧边栏', 'PLAYER_LIST': '玩家列表', 'BELOW_NAME': '名字下方' };
        const RENDER_LABELS = { 'INTEGER': '整数', 'HEARTS': '红心' };
        const ORDER_LABELS = { 'ASCENDING': '升序', 'DESCENDING': '降序' };
        const SLOT_KEYS = { 'SIDE_BAR': 'sidebar', 'PLAYER_LIST': 'list', 'BELOW_NAME': 'belowname' };

        function esc(s) {
            return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        }

        function escAttr(s) {
            return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        }

        // 一段文本 → 问号浮层 data-help 属性值。
        //
        // ★★★ 为什么必须转两层，且换行要用占位符保护
        // -------------------------------------------
        // gmOpenHelp 的取值链是：
        //     var text = btn.getAttribute('data-help');   // ① 浏览器把属性值**解码一次**
        //     pop.innerHTML = text;                       // ② 再当 HTML 解析注入
        // 内容被解析两次，所以只转一层是不够的：
        //     esc 把 "<script>" 变成 "&lt;script&gt;"
        //     ① 解码后还原成 "<script>"  ← 真标签又回来了
        //     ② innerHTML 就执行了        ← XSS
        // 因此第一层 esc 之外，**还必须有一层针对属性的转义**，
        // 把 & 再转成 &amp; ，让第 ① 步只能解出 "&lt;script&gt;" 这个字面量。
        //
        // 但第二层转义（escAttr 会连 < > 一起转）又有个副作用：
        // 它会吃掉我们自己插入的 <br>，浮层里就会显示字面量 "<br>" 而不是换行。
        //
        // 解法：换行先用一个**不会被转义**的占位符表示，
        //       走完两层转义后再把占位符还原成 <br>。
        //       占位符用 \u0000 包裹，正常文本里不可能出现，也就无法被伪造。
        //
        // 最终顺序：归一换行 → 换占位符 → esc → escAttr → 占位符还原 <br>
        var GM_BR_TOKEN = '\u0000GM_BR\u0000';

        function helpAttr(text) {
            var s = String(text == null ? '' : text);
            s = s.replace(/\r\n?/g, '\n');            // 归一闪行，避免 Windows 换行多出空行
            s = s.replace(/\n/g, GM_BR_TOKEN);        // 换行 → 占位符（避开后续转义）
            s = esc(s);                               // 第一层：内容 → HTML 文本
            s = escAttr(s);                           // 第二层：属性（连 & < > " 一起转）
            return s.replace(new RegExp(GM_BR_TOKEN, 'g'), '<br>');   // 占位符 → 真换行
        }

        function qqSafe(s) { return String(s == null ? '' : s).replace(/[^0-9a-zA-Z_-]/g, '_'); }

        // 把一个值安全地嵌进内联 onclick 的**字符串字面量**里（含外侧引号）。
        //
        // ★★★ 为什么不能只做 HTML 转义
        // ---------------------------------
        // 常见错误写法是 onclick="f('" + escAttr(name) + "')"。
        // 这里有两层解析：先 HTML 属性解码，再当 JS 解析。escAttr 只挡了
        // 第一层。玩家名/机器人名里只要出现反斜杠就会出事：
        //
        //     name = a\'  +  )" onmouseover="alert(1)
        //     → 输出 onclick="f('a\')" onmouseover="alert(1)')"
        //          ↑ 反斜杠把引号转义掉了，JS 串提前闭合，
        //            后面的内容变成新的 HTML 属性 → 存储型 XSS
        //
        // 也不能用「把 ' 替换成 \'」这种土办法：反斜杠本身没转义，
        // 攻击者加一个 \ 就能反过来控制转义方向。
        //
        // 正确做法：用 JSON.stringify 生成一个**合法的 JS 字符串字面量**
        // （它会把 \ " 换行等全部正确转义），再把 < > & 转成 HTML 实体
        // 防止提前闭合 HTML 属性。返回的串已含两侧双引号，直接拼即可。
        function jsArg(v) {
            var lit = JSON.stringify(String(v == null ? '' : v));
            // JSON.stringify 产出的引号是 " ，而 HTML 属性用双引号包裹的话
            // 必须转成 &quot; （属性解码后会还原成 " ，JS 仍能正确解析）。
            return lit.replace(/&/g, '&amp;')
                      .replace(/</g, '&lt;')
                      .replace(/>/g, '&gt;')
                      .replace(/"/g, '&quot;');
        }

        function fmtExpiry(v) {
            if (!v) return '永久';
            return String(v).replace('T', ' ').slice(0, 16);
        }

        async function loadBans() {
            try {
                const data = await apiGet('/api/bans');
                renderBanTables(data.player_bans || [], data.ip_bans || []);
            } catch (e) { console.error('loadBans', e); }
        }

        function renderBanTables(playerBans, ipBans) {
            const p = el('player-ban-list');
            if (p) {
                p.innerHTML = '';
                if (!playerBans.length) { p.innerHTML = '<tr><td colspan="6" class="empty">暂无封禁</td></tr>'; }
                playerBans.forEach(function (b) {
                    const tr = document.createElement('tr');
                    tr.innerHTML = '<td>' + esc(b.name) + '</td><td class="uuid-text">' + esc(b.xuid || '') + '</td><td>' + esc(b.reason) + '</td><td>' + esc(b.source || '') + '</td><td>' + fmtExpiry(b.expiration) + '</td>';
                    const td = document.createElement('td');
                    const btn = document.createElement('button');
                    btn.className = 'kill';
                    btn.textContent = '解封';
                    btn.onclick = function () { unban('player', b.name, b.xuid); };
                    td.appendChild(btn);
                    tr.appendChild(td);
                    p.appendChild(tr);
                });
            }
            const ip = el('ip-ban-list');
            if (ip) {
                ip.innerHTML = '';
                if (!ipBans.length) { ip.innerHTML = '<tr><td colspan="5" class="empty">暂无封禁</td></tr>'; }
                ipBans.forEach(function (b) {
                    const tr = document.createElement('tr');
                    tr.innerHTML = '<td>' + esc(b.address) + '</td><td>' + esc(b.reason) + '</td><td>' + esc(b.source || '') + '</td><td>' + fmtExpiry(b.expiration) + '</td>';
                    const td = document.createElement('td');
                    const btn = document.createElement('button');
                    btn.className = 'kill';
                    btn.textContent = '解封';
                    btn.onclick = function () { unban('ip', b.address); };
                    td.appendChild(btn);
                    tr.appendChild(td);
                    ip.appendChild(tr);
                });
            }
        }

        function banDuration() { return parseInt(el('ban-duration').value, 10) || 0; }
        function banSyncCloud() { const s = el('ban-sync-cloud'); return !!(s && s.checked); }

        async function banPlayer() {
            const name = el('ban-name').value.trim();
            if (!name) { showStatus('请输入玩家名', false); return; }
            const r = await apiPost('/api/ban', { type: 'player', target: name, by: 'name', reason: el('ban-reason').value, expires: banDuration(), sync_cloud: banSyncCloud() });
            showStatus(r.message || r.error, !!r.success);
            if (r.success) { el('ban-name').value = ''; el('ban-reason').value = ''; loadBans(); refreshPlayers(); }
        }

        async function banOnlinePlayer() {
            const name = el('ban-online').value;
            if (!name) { showStatus('请选择在线玩家', false); return; }
            const r = await apiPost('/api/ban', { type: 'player', target: name, by: 'name', reason: el('ban-reason').value, expires: banDuration(), sync_cloud: banSyncCloud() });
            showStatus(r.message || r.error, !!r.success);
            if (r.success) { el('ban-reason').value = ''; loadBans(); }
        }

        async function banPlayerByXuid() {
            const xuid = el('ban-xuid').value.trim();
            if (!xuid) { showStatus('请输入 XUID', false); return; }
            const r = await apiPost('/api/ban', { type: 'player', target: xuid, by: 'xuid', reason: el('ban-reason').value, expires: banDuration(), sync_cloud: banSyncCloud() });
            showStatus(r.message || r.error, !!r.success);
            if (r.success) { el('ban-xuid').value = ''; el('ban-reason').value = ''; loadBans(); }
        }

        async function banIp() {
            const addr = el('ban-ip').value.trim();
            if (!addr) { showStatus('请输入 IP', false); return; }
            const r = await apiPost('/api/ban', { type: 'ip', target: addr, reason: el('ipban-reason').value, expires: parseInt(el('ipban-duration').value, 10) || 0, sync_cloud: banSyncCloud() });
            showStatus(r.message || r.error, !!r.success);
            if (r.success) { el('ban-ip').value = ''; el('ipban-reason').value = ''; loadBans(); }
        }

        async function unban(type, target, xuid) {
            const r = await apiPost('/api/unban', { type: type, target: target, xuid: xuid || undefined });
            showStatus(r.message || r.error, !!r.success);
            if (r.success) loadBans();
        }

        // 日志面板可能同时出现在多个板块（「服务器信息」的聊天/控制台面板、
        // 「操作控制台」的 console-panel）。游标与首载标记必须按面板隔离，
        // 否则先轮询的面板把 lastId 推到最新后，后轮询的面板查 after=lastId
        // 只会拿到空数组，而首载标记已被置 false，于是既无日志也无空状态提示
        // —— 表现为整个面板空白。
        const _panelCursor = Object.create(null);   // panelId -> lastId
        const _panelFirst = Object.create(null);    // panelId -> 是否尚未渲染过

        function _cursorOf(panelId, key, def) {
            const k = key + ':' + panelId;
            if (!(k in _panelCursor)) _panelCursor[k] = def;
            return _panelCursor[k];
        }
        function _setCursor(panelId, key, v) { _panelCursor[key + ':' + panelId] = v; }

        //: 返回 true 表示「该面板还没渲染过任何内容，调用方可整体替换 innerHTML」。
        //: 第一次读取即消费掉该标记，后续调用一律返回 false。
        function _firstLoad(panelId, key) {
            const k = key + ':' + panelId;
            if (_panelFirst[k] === false) return false;
            _panelFirst[k] = false;
            return true;
        }

        function appendMessages(msgs, panelId, scrollId) {
            const pid = panelId || 'message-panel';
            const panel = el(pid);
            if (!panel) return;
            if (_firstLoad(pid, 'msg')) { panel.innerHTML = ''; }
            else { const empty = panel.querySelector('.msg-empty'); if (empty) empty.remove(); }
            msgs.forEach(function (m) {
                const div = document.createElement('div');
                div.className = 'msg msg-' + m.type;
                let body;
                if (m.type === 'chat') {
                    body = '<span class="msg-name">' + esc(m.player) + '</span>: ' + esc(m.message);
                } else if (m.type === 'broadcast') {
                    body = '<span class="tag-label">广播</span>' + esc(m.message);
                } else {
                    body = '<span class="msg-name">' + esc(m.player) + '</span> ' + esc(m.message);
                }
                div.innerHTML = '<span class="msg-time">[' + esc(m.time) + ']</span>' + body;
                panel.appendChild(div);
            });
            const as = el(scrollId || 'msg-autoscroll');
            if (as && as.checked) { panel.scrollTop = panel.scrollHeight; }
        }

        async function pollMessages(panelId, scrollId) {
            try {
                const pid = panelId || 'message-panel';
                const data = await apiGet('/api/messages?after=' + _cursorOf(pid, 'msg', 0));
                if (data && data.__status === 403) {
                    const p = el(pid);
                    if (p) p.innerHTML = '<div class="msg msg-empty empty">无权限查看聊天日志</div>';
                    return;
                }
                const msgs = data.messages || [];
                if (msgs.length) {
                    appendMessages(msgs, pid, scrollId);
                    _setCursor(pid, 'msg', data.last_id || _cursorOf(pid, 'msg', 0));
                } else if (_firstLoad(pid, 'msg')) {
                    // 空状态只在「本面板」还没渲染过任何内容时给出
                    const panel = el(pid);
                    if (panel) panel.innerHTML = '<div class="msg msg-empty empty">暂无消息</div>';
                }
            } catch (e) { console.error('pollMessages', e); }
        }

        function clearMessagesUI(panelId) {
            const pid = panelId || 'message-panel';
            const panel = el(pid);
            if (!panel) return;
            panel.innerHTML = '';
            // 与 clearConsoleUI 同理：清空后要重新拉全量，否则只显示后续新增，
            // 用户会以为「清空」把历史也删了
            _setCursor(pid, 'msg', 0);
            _panelFirst['msg:' + pid] = true;
            pollMessages(pid, _scrollIdFor(pid, 'msg'));
        }

        // 面板 id → 对应的「自动滚动」复选框 id。
        // 容器 id 是 `xxx-panel`，复选框 id 是 `xxx-autoscroll`，
        // 唯一例外是 message→msg。
        function _scrollIdFor(panelId, kind) {
            if (panelId === 'message-panel') return 'msg-autoscroll';
            return String(panelId).replace(/-panel$/, '') + '-autoscroll';
        }

        // ==================== 聊天与日志 ====================
        // 原来这里有一整个独立的「日志中心」板块（lg-* 一套 DOM + 
        // switchLogCenterTab / refreshLogsCenter），内容和服务器信息里的
        // 「聊天与日志」面板完全重复。现已合并：只保留 sec-players 里的
        // 那一套面板，下面这个函数为「🔄 刷新」按钮服务。
        function refreshLogsCenter() {
            const chatPane = el('chat-pane');
            // 控制台面板可见时刷新控制台，否则刷新聊天
            const onConsole = !!(chatPane && chatPane.style.display === 'none');
            if (onConsole) pollConsole('console-panel', 'console-autoscroll');
            else pollMessages('message-panel', 'msg-autoscroll');
            const st = el('logs-status');
            if (st) { st.textContent = '已刷新'; st.className = 'status text-ok'; }
        }

        function loadPermInfo() {
            // perm-player 在「🔐 权限管理」板块里，而该板块默认 .hidden。
            // 若用户在 DOMContentLoaded 之前点到了侧边栏（页面卡顿时可能），
            // 或某次渲染异常导致该 select 缺失，直接 .value 会抛
            // "Cannot read properties of null" 并中断整个板块渲染。
            const sel = el('perm-player');
            const name = sel ? sel.value : '';
            const area = el('perm-info');
            if (!area) return;
            if (!name) { area.innerHTML = ''; return; }
            const p = currentPlayers.find(function (x) { return x.name === name; });
            if (!p) { area.innerHTML = '<p>玩家不在线</p>'; return; }
            const lvl = p.permission_level === 2 ? 'CONSOLE' : (p.permission_level === 1 ? 'OP' : 'DEFAULT');
            const perms = (p.permissions || []).map(function (x) { return '<span class="tag-badge">' + esc(x.permission) + ' = ' + (x.value ? 'true' : 'false') + '</span>'; }).join(' ') || '（无）';
            area.innerHTML = '<p><b>权限级别:</b> ' + lvl + '</p><p><b>有效权限:</b> ' + perms + '</p>';
        }

        async function setPermission() {
            const selP = el('perm-player');
            const name = selP ? selP.value : '';
            const nodeEl = el('perm-node');
            if (!selP || !nodeEl) { showStatus('权限面板未就绪，请重新打开本板块', false); return; }
            const node = nodeEl.value.trim();
            if (!name || !node) { showStatus('请选择玩家并填写权限节点', false); return; }
            const value = el('perm-value').value === 'true';
            const r = await apiPost('/api/permission', { player_name: name, permission: node, action: 'set', value: value });
            showStatus(r.message || r.error, !!r.success);
            if (r.success) { nodeEl.value = ''; await refreshPlayers(); loadPermInfo(); }
        }

        async function unsetPermission() {
            const selP = el('perm-player');
            const name = selP ? selP.value : '';
            const nodeEl = el('perm-node');
            if (!selP || !nodeEl) { showStatus('权限面板未就绪，请重新打开本板块', false); return; }
            const node = nodeEl.value.trim();
            if (!name || !node) { showStatus('请选择玩家并填写权限节点', false); return; }
            const r = await apiPost('/api/permission', { player_name: name, permission: node, action: 'unset' });
            showStatus(r.message || r.error, !!r.success);
            if (r.success) { nodeEl.value = ''; await refreshPlayers(); loadPermInfo(); }
        }

        async function loadScoreboardsFull() {
            const data = await apiGet('/api/scoreboards');
            renderObjectives(data.full_objectives || []);
            const sel = el('score-objective');
            if (sel) {
                const prev = sel.value;
                const names = data.objectives || [];
                sel.innerHTML = '<option value="">-- 目标 --</option>';
                names.forEach(function (n) {
                    const o = document.createElement('option');
                    o.value = n; o.textContent = n; sel.appendChild(o);
                });
                if (prev && names.includes(prev)) sel.value = prev;
            }
        }

        function renderObjectives(objectives) {
            const tb = el('objective-list');
            if (!tb) return;
            tb.innerHTML = '';
            if (!objectives.length) { tb.innerHTML = '<tr><td colspan="6" class="empty">暂无记分板目标</td></tr>'; return; }
            objectives.forEach(function (o) {
                const tr = document.createElement('tr');
                const c1 = document.createElement('td'); c1.textContent = o.name;
                const c2 = document.createElement('td'); c2.textContent = o.display_name || '';
                const c3 = document.createElement('td'); c3.textContent = (o.display_slot ? SLOT_LABELS[o.display_slot] : '') || '（无）';
                const c4 = document.createElement('td'); c4.textContent = RENDER_LABELS[o.render_type] || o.render_type;
                const c5 = document.createElement('td'); c5.textContent = ORDER_LABELS[o.sort_order] || '（无）';
                const c6 = document.createElement('td');
                const slotSel = document.createElement('select');
                slotSel.innerHTML = '<option value="">不显示</option><option value="sidebar">侧边栏</option><option value="list">玩家列表</option><option value="belowname">名字下方</option>';
                slotSel.value = SLOT_KEYS[o.display_slot] || '';
                const orderSel = document.createElement('select');
                orderSel.innerHTML = '<option value="ascending">升序</option><option value="descending">降序</option>';
                orderSel.value = (o.sort_order || 'ASCENDING').toLowerCase();
                const applyBtn = document.createElement('button');
                applyBtn.textContent = '应用显示';
                applyBtn.onclick = function () { setObjectiveDisplay(o.name, slotSel.value, orderSel.value); };
                const delBtn = document.createElement('button');
                delBtn.textContent = '删除';
                delBtn.className = 'kill';
                delBtn.onclick = function () { removeObjective(o.name); };
                c6.appendChild(slotSel); c6.appendChild(orderSel); c6.appendChild(applyBtn); c6.appendChild(delBtn);
                [c1, c2, c3, c4, c5, c6].forEach(function (c) { tr.appendChild(c); });
                tb.appendChild(tr);
            });
        }

        async function addObjective() {
            const name = el('obj-name').value.trim();
            if (!name) { showStatus('请输入目标名称', false); return; }
            const r = await apiPost('/api/objective', { action: 'add', name: name, display_name: el('obj-display').value.trim() || name, render_type: el('obj-render').value });
            showStatus(r.message || r.error, !!r.success);
            if (r.success) { el('obj-name').value = ''; el('obj-display').value = ''; loadScoreboardsFull(); }
        }

        async function removeObjective(name) {
            const r = await apiPost('/api/objective', { action: 'remove', name: name });
            showStatus(r.message || r.error, !!r.success);
            if (r.success) loadScoreboardsFull();
        }

        async function setObjectiveDisplay(name, slot, order) {
            const r = await apiPost('/api/objective', { action: 'display', name: name, slot: slot || null, order: order });
            showStatus(r.message || r.error, !!r.success);
            if (r.success) loadScoreboardsFull();
        }

        // ==================== 白名单审核 ====================
        const WL_LABEL = { pending: '待审核', approved: '已通过', rejected: '已拒绝' };
        const WL_CLASS = { pending: 'wl-pending', approved: 'wl-approved', rejected: 'wl-rejected' };

        async function loadWhitelist() {
            try {
                const data = await apiGet('/api/whitelist/applications');
                renderWhitelist(data.applications || []);
            } catch (e) { console.error('loadWhitelist', e); }
        }

        function renderWhitelist(apps) {
            const tb = el('whitelist-list');
            if (!tb) return;
            tb.innerHTML = '';
            if (!apps.length) { tb.innerHTML = '<tr><td colspan="6" class="empty">暂无申请</td></tr>'; return; }
            apps.forEach(function (a) {
                const tr = document.createElement('tr');
                const c1 = document.createElement('td'); c1.textContent = a.name;
                const c2 = document.createElement('td'); c2.className = 'uuid-text'; c2.textContent = a.xuid || '';
                const c3 = document.createElement('td');
                const badge = document.createElement('span');
                badge.className = 'wl-status ' + (WL_CLASS[a.status] || 'wl-pending');
                badge.textContent = WL_LABEL[a.status] || a.status;
                c3.appendChild(badge);
                const c4 = document.createElement('td'); c4.textContent = a.note || '';
                const c5 = document.createElement('td'); c5.textContent = fmtExpiry(a.applied_at);
                const c6 = document.createElement('td');
                if (a.status === 'pending') {
                    const okBtn = document.createElement('button'); okBtn.className = 'ok'; okBtn.textContent = '通过'; okBtn.onclick = function () { reviewWhitelist(a.name, 'approve'); };
                    const noBtn = document.createElement('button'); noBtn.className = 'kill'; noBtn.textContent = '拒绝'; noBtn.onclick = function () { reviewWhitelist(a.name, 'reject'); };
                    c6.appendChild(okBtn); c6.appendChild(noBtn);
                } else {
                    c6.textContent = '—';
                }
                [c1, c2, c3, c4, c5, c6].forEach(function (c) { tr.appendChild(c); });
                tb.appendChild(tr);
            });
        }

        async function reviewWhitelist(name, action) {
            let note = '';
            if (action === 'reject') {
                note = prompt('拒绝原因（可选）：', '');
                if (note === null) return;
            }
            try {
                const r = await apiPost('/api/whitelist/review', { name: name, action: action, note: note });
                showStatus(r.message || r.error, !!r.success);
                if (r.success) loadWhitelist();
            } catch (e) { showStatus('请求失败: ' + e, false); }
        }

        // ==================== server.properties ====================
        // 已知枚举项（下拉选择）；其余按值类型自动生成开关或文本框快捷编辑
        const PROPS_ENUM = {
            'gamemode': ['survival', 'creative', 'adventure', 'spectator'],
            'difficulty': ['peaceful', 'easy', 'normal', 'hard'],
            'default-player-permission-level': ['visitor', 'member', 'operator'],
            'level-type': ['FLAT', 'LEGACY', 'DEFAULT'],
            'chat-restriction': ['None', 'Dropped', 'Disabled'],
            'server-authoritative-movement': ['client-auth', 'server-auth', 'server-auth-with-rewind']
        };
        let propsKeys = {};

        function isBoolValue(v) {
            const s = String(v).trim().toLowerCase();
            return ['true', 'false', 'yes', 'no', 'on', 'off'].indexOf(s) >= 0;
        }

        async function loadProperties() {
            try {
                const data = await apiGet('/api/properties');
                const pathEl = el('props-path');
                if (pathEl) pathEl.textContent = data.found ? ('文件路径：' + data.path) : '未找到 server.properties（请确认插件数据目录层级）';
                const ta = el('props-content');
                if (ta) ta.value = data.raw || '';
                propsKeys = data.keys || {};
                renderProperties();
            } catch (e) { console.error('loadProperties', e); }
        }

        function renderProperties() {
            const box = el('props-quick');
            if (!box) return;
            const kw = (el('props-filter') ? el('props-filter').value : '').trim().toLowerCase();
            const keys = Object.keys(propsKeys).sort();
            box.innerHTML = '';
            if (!keys.length) {
                box.innerHTML = '<div class="empty grid-full">未能解析到配置项（server.properties 可能为空）</div>';
                return;
            }
            let visible = 0;
            keys.forEach(function (key) {
                const val0 = String(propsKeys[key] == null ? '' : propsKeys[key]);
                if (kw && key.toLowerCase().indexOf(kw) < 0 && val0.toLowerCase().indexOf(kw) < 0) return;
                visible++;
                const card = document.createElement('div');
                card.className = 'world-card';
                card.style.margin = '0';
                const title = document.createElement('div');
                title.className = 'text-accent break mb-sm';
                title.textContent = key;
                card.appendChild(title);

                if (isBoolValue(val0)) {
                    const isOn = ['true', 'yes', 'on'].indexOf(val0.toLowerCase()) >= 0;
                    const label = document.createElement('label');
                    label.className = 'row pointer';
                    const cb = document.createElement('input');
                    cb.type = 'checkbox';
                    cb.checked = isOn;
                    cb.className = 'sw-check';
                    cb.onchange = function () { setProperty(key, cb.checked ? 'true' : 'false'); };
                    const span = document.createElement('span');
                    span.textContent = isOn ? 'true' : 'false';
                    span.style.fontFamily = 'monospace';
                    label.appendChild(cb);
                    label.appendChild(span);
                    card.appendChild(label);
                } else if (PROPS_ENUM[key]) {
                    const sel = document.createElement('select');
                    sel.className = 'block-input';
                    const opts = PROPS_ENUM[key].slice();
                    if (val0 && opts.indexOf(val0) < 0) opts.push(val0);
                    opts.forEach(function (o) {
                        const opt = document.createElement('option');
                        opt.value = o; opt.textContent = o;
                        if (o === val0) opt.selected = true;
                        sel.appendChild(opt);
                    });
                    sel.onchange = function () { setProperty(key, sel.value); };
                    card.appendChild(sel);
                } else {
                    const row = document.createElement('div');
                    row.className = 'row';
                    const inp = document.createElement('input');
                    inp.type = 'text';
                    inp.value = val0;
                    inp.className = 'flex-input';
                    const btn = document.createElement('button');
                    btn.className = 'ok';
                    btn.textContent = '保存';
                    btn.onclick = function () { setProperty(key, inp.value, btn); };
                    row.appendChild(inp);
                    row.appendChild(btn);
                    card.appendChild(row);
                }
                box.appendChild(card);
            });
            if (!visible) box.innerHTML = '<div class="empty grid-full">没有匹配「' + esc(kw) + '」的配置项</div>';
        }

        async function setProperty(key, value, btn) {
            try {
                if (btn) { btn.disabled = true; btn.textContent = '保存中…'; }
                const r = await apiPost('/api/properties/set', { key: key, value: value });
                showStatus(r.message || r.error, !!r.success);
                if (r.success) {
                    propsKeys[key] = String(value);
                    const ta = el('props-content');
                    if (ta) {
                        const lines = ta.value.split('\n');
                        let found = false;
                        for (let i = 0; i < lines.length; i++) {
                            const s = lines[i].trim();
                            if (s && s.charAt(0) !== '#' && s.indexOf('=') >= 0) {
                                const k = s.split('=')[0].trim();
                                if (k.toLowerCase() === key.toLowerCase()) { lines[i] = k + '=' + value; found = true; break; }
                            }
                        }
                        if (!found) lines.push(key + '=' + value);
                        ta.value = lines.join('\n');
                    }
                    if (key.toLowerCase() === 'level-name') loadWorlds();
                    renderProperties();
                }
            } catch (e) {
                showStatus('请求失败: ' + e, false);
            } finally {
                if (btn) { btn.disabled = false; btn.textContent = '保存'; }
            }
        }

        async function saveProperties() {
            const st = el('props-status');
            if (st) { st.textContent = '保存中…'; st.className = 'status'; }
            try {
                const content = el('props-content').value;
                const r = await apiPost('/api/properties/save', { content: content });
                showStatus(r.message || r.error, !!r.success);
                if (st) { st.textContent = (r.success ? '✓ ' : '✗ ') + (r.message || r.error); st.className = r.success ? 'status text-ok' : 'status text-bad'; }
                if (r.success) loadWorlds();
            } catch (e) {
                showStatus('请求失败: ' + e, false);
                if (st) { st.textContent = '✗ 请求失败: ' + e; st.className = 'status text-bad'; }
            }
        }

        // ==================== 多存档切换 ====================
        async function loadWorlds() {
            try {
                const data = await apiGet('/api/worlds');
                const cur = el('world-current');
                if (cur) cur.textContent = data.current || '（未设置）';
                renderWorlds(data.worlds || [], data.current);
            } catch (e) { console.error('loadWorlds', e); }
        }

        function renderWorlds(worlds, current) {
            const box = el('world-list');
            if (!box) return;
            box.innerHTML = '';
            if (!worlds.length) { box.innerHTML = '<p class="empty">未发现存档目录（请在服务器根目录 worlds/ 下检测到含 level.dat 的世界）</p>'; return; }
            worlds.forEach(function (w) {
                const card = document.createElement('div');
                const isCur = w.name === current;
                card.className = 'world-card' + (isCur ? ' current' : '');
                card.innerHTML = '<div><b>' + esc(w.name) + '</b> ' +
                    (isCur ? '<span class="world-badge">当前存档</span>' : '') +
                    (w.valid ? '' : ' <span class="world-badge">（无 level.dat）</span>') + '</div>' +
                    (isCur ? '' : '<button class="ok mt-xs">切换到此存档</button>');
                box.appendChild(card);
                const btn = card.querySelector('button');
                if (btn) btn.onclick = function () { switchWorld(w.name); };
            });
        }

        async function switchWorld(name) {
            if (!confirm('确定把服务器存档切换到「' + name + '」吗？\n修改后需要重启服务器才会生效。')) return;
            try {
                const r = await apiPost('/api/world/switch', { world: name });
                showStatus(r.message || r.error, !!r.success);
                if (r.success) {
                    loadWorlds();
                    if (confirm(r.message + '\n\n是否立即重启服务器？（若未配置自动拉起，请在控制台手动重启）')) {
                        restartServer();
                    }
                }
            } catch (e) { showStatus('请求失败: ' + e, false); }
        }

        async function restartServer() {
            try {
                const r = await apiPost('/api/restart', {});
                showStatus(r.message || r.error, !!r.success);
            } catch (e) { showStatus('请求失败: ' + e, false); }
        }

        // ==================== 控制台日志 ====================
        // 游标与首载标记走 _panelCursor / _panelFirst（见 pollMessages 处说明），
        // 保证「服务器信息」与「操作控制台」两个控制台面板互不干扰。
        function appendConsoleLogs(logs, panelId, scrollId) {
            const pid = panelId || 'console-panel';
            const panel = el(pid);
            if (!panel) return;
            if (_firstLoad(pid, 'con')) { panel.innerHTML = ''; }
            else { const empty = panel.querySelector('.clog-empty'); if (empty) empty.remove(); }
            logs.forEach(function (l) {
                const div = document.createElement('div');
                div.className = 'clog clog-' + (l.level || 'INFO');
                div.innerHTML = '<span class="clog-time">[' + esc(l.time) + ' ' + esc(l.level) + ']</span>' + esc(l.message);
                panel.appendChild(div);
            });
            const as = el(scrollId || 'console-autoscroll');
            if (as && as.checked) { panel.scrollTop = panel.scrollHeight; }
        }

        async function pollConsole(panelId, scrollId) {
            try {
                const pid = panelId || 'console-panel';
                const data = await apiGet('/api/console?after=' + _cursorOf(pid, 'con', 0));
                if (data && data.__status === 403) {
                    const p = el(pid);
                    if (p) p.innerHTML = '<div class="clog clog-empty empty">无权限查看控制台日志</div>';
                    return;
                }
                const logs = data.logs || [];
                if (logs.length) {
                    appendConsoleLogs(logs, pid, scrollId);
                    _setCursor(pid, 'con', data.last_id || _cursorOf(pid, 'con', 0));
                } else if (_firstLoad(pid, 'con')) {
                    const panel = el(pid);
                    if (panel) panel.innerHTML = '<div class="clog clog-empty empty">暂无控制台日志</div>';
                }
            } catch (e) { console.error('pollConsole', e); }
        }

        function clearConsoleUI(panelId) {
            const pid = panelId || 'console-panel';
            const panel = el(pid);
            if (!panel) return;
            panel.innerHTML = '';
            // 清空显示后要重新拉全量：把游标归零并让该面板回到「可整块替换」状态
            _setCursor(pid, 'con', 0);
            _panelFirst['con:' + pid] = true;
            pollConsole(pid, _scrollIdFor(pid, 'con'));
        }

        function sendConsoleCommand() {
            const input = el('console-cmd');
            const st = el('console-cmd-status');
            const cmd = (input ? input.value : '').trim();
            if (!cmd) { if (st) { st.textContent = '请输入指令'; st.className = 'status text-bad'; } return; }
            if (st) { st.textContent = '发送中…'; st.className = 'status'; }
            apiPost('/api/console/command', { command: cmd }).then(function (r) {
                if (r && r.ok) {
                    if (st) { st.textContent = '✅ 已执行: ' + cmd; st.className = 'status text-ok'; }
                    if (input) input.value = '';
                } else {
                    if (st) { st.textContent = '发送失败: ' + (r ? r.error : '无响应'); st.className = 'status text-bad'; }
                }
            }).catch(function (e) {
                if (st) { st.textContent = '发送失败: ' + e; st.className = 'status text-bad'; }
            });
        }

        function initConsoleCmd() {
            const input = el('console-cmd');
            if (input) input.addEventListener('keydown', function (ev) {
                if (ev.key === 'Enter') { ev.preventDefault(); sendConsoleCommand(); }
            });
        }
        initConsoleCmd();

        // ==================== 账户与安全 ====================
        // 板块元数据：中文名 + 一句话解释 + 分组。键名与后端 _ALL_SCOPES 对齐。
        var GM_SCOPE_META = {
            players:    { n: '服务器信息', d: '玩家与运行状态', g: '常用' },
            console:    { n: '操作控制台', d: '执行指令、给物、踢人', g: '常用' },
            // logs 已并入「服务器信息」板块（见 admin.html），这里仅作为
            // 日志类 API 的授权 scope 保留，名称标注清楚以免误解。
            logs:       { n: '日志查看', d: '聊天与控制台日志（随服务器信息展示）', g: '常用' },
            bans:       { n: '封禁管理', d: '封禁 / 解封玩家', g: '玩家管理' },
            whitelist:  { n: '白名单审核', d: '审批进服申请', g: '玩家管理' },
            detail:     { n: '玩家详情', d: '单个玩家完整数据', g: '玩家管理' },
            perm:       { n: '权限管理', d: 'OP 与权限组', g: '玩家管理' },
            gamerules:  { n: '游戏规则', d: '开关 gamerule', g: '服务器' },
            worlds:     { n: '多存档切换', d: '切换 / 管理存档', g: '服务器' },
            backup:     { n: '自动备份', d: '存档定时备份', g: '服务器' },
            files:      { n: '文件管理', d: '服务器文件读写', g: '服务器' },
            diag:       { n: '诊断', d: '主线程卡顿排查', g: '服务器' },
            qqbot:      { n: '机器人管理', d: 'QQ 机器人配置', g: '扩展' },
            cross:      { n: '跨服互联', d: '多服数据同步', g: '扩展' },
            gametools:  { n: '签到与绑定', d: 'MC ↔ QQ 绑定、签到', g: '扩展' },
            cloud:      { n: '云黑名单', d: '云黑联合封禁', g: '扩展' },
            mods:       { n: '模组管理', d: '资源 / 行为包与子插件', g: '扩展' },
        };
        var GM_SCOPE_GROUPS = ['常用', '玩家管理', '服务器', '扩展'];
        // 常用组合：一键勾选，省得一个个点
        var GM_SCOPE_PRESETS = {
            '只读监控':   ['players', 'logs', 'diag'],
            '日常运维':   ['players', 'logs', 'console', 'bans', 'whitelist', 'diag'],
            '全部权限':   null,   // null = 全选
        };

        function _scopeMeta(s) {
            return GM_SCOPE_META[s] || { n: s, d: '', g: '扩展' };
        }

        function renderScopeChips() {
            const box = el('tmp-scopes');
            if (!box) return;
            const all = (window._ALL_SCOPES || []).slice();
            if (!all.length) { box.innerHTML = '<p class="empty">暂无可用板块</p>'; return; }

            // 按分组归拢；未在元数据里的归到「扩展」
            const buckets = {};
            GM_SCOPE_GROUPS.forEach(function (g) { buckets[g] = []; });
            all.forEach(function (s) {
                const meta = _scopeMeta(s);
                (buckets[meta.g] = buckets[meta.g] || []).push(s);
            });

            let html = '';
            GM_SCOPE_GROUPS.forEach(function (g) {
                const items = buckets[g] || [];
                if (!items.length) return;
                html += '<div class="scope-group">'
                    + '<div class="scope-group-title">' + esc(g) + '</div>'
                    + '<div class="scope-grid">'
                    + items.map(function (s) {
                        const m = _scopeMeta(s);
                        return '<label class="scope-card" data-card="' + escAttr(s) + '">'
                            + '<input type="checkbox" data-scp="' + escAttr(s) + '">'
                            + '<span class="scope-card-txt">'
                            + '<span class="scope-card-name">' + esc(m.n) + '</span>'
                            + (m.d ? '<span class="scope-card-desc">' + esc(m.d) + '</span>' : '')
                            + '</span></label>';
                    }).join('')
                    + '</div></div>';
            });

            // 快捷按钮：一键套用常用组合
            html += '<div class="scope-actions">'
                + Object.keys(GM_SCOPE_PRESETS).map(function (k) {
                    return '<button type="button" onclick="applyScopePreset('
                        + JSON.stringify(k).replace(/"/g, '&quot;') + ')">' + esc(k) + '</button>';
                }).join('')
                + '<button type="button" onclick="clearScopes()">清空</button>'
                + '<span class="scope-count" id="tmp-scope-count"></span>'
                + '</div>';

            box.innerHTML = html;
            _syncScopeCards();

            // 勾选状态变化时同步卡片高亮与计数。
            //
            // ⚠ 本函数每次 loadAccount() 成功都会被调用，而 #tmp-scopes
            // 这个容器节点**从头到尾不会被替换**（只换 innerHTML）。
            // 如果无条件 addEventListener，每进一次「账户与安全」就多挂
            // 一个监听器 —— 点一次勾选会触发 N 次 _syncScopeCards，
            // 切板块几次后明显变卡。用一个标记保证只绑一次。
            if (!box._gmScopeBound) {
                box._gmScopeBound = true;
                box.addEventListener('change', function (ev) {
                    if (ev.target && ev.target.hasAttribute && ev.target.hasAttribute('data-scp')) {
                        _syncScopeCards();
                    }
                });
            }
        }

        function _syncScopeCards() {
            const box = el('tmp-scopes');
            if (!box) return;
            const cards = box.querySelectorAll('.scope-card');
            let n = 0;
            cards.forEach(function (card) {
                const inp = card.querySelector('input[data-scp]');
                const on = !!(inp && inp.checked);
                card.classList.toggle('on', on);
                if (on) n++;
            });
            const cnt = el('tmp-scope-count');
            if (cnt) cnt.textContent = n ? ('已选 ' + n + ' 个板块') : '未选择任何板块';
        }

        function _setScopes(list) {
            const box = el('tmp-scopes');
            if (!box) return;
            const want = list ? new Set(list) : null;
            box.querySelectorAll('input[data-scp]').forEach(function (inp) {
                const s = inp.getAttribute('data-scp');
                inp.checked = want ? want.has(s) : true;
            });
            _syncScopeCards();
        }

        function applyScopePreset(name) {
            const def = GM_SCOPE_PRESETS[name];
            _setScopes(def === undefined ? [] : def);
        }

        function clearScopes() { _setScopes([]); }

        function _checkedScopes() {
            const box = el('tmp-scopes');
            const out = [];
            if (!box) return out;
            const checks = box.querySelectorAll('input[data-scp]');
            checks.forEach(function (c) { if (c.checked) out.push(c.getAttribute('data-scp')); });
            return out;
        }

        async function loadAccount() {
            try {
                const r = await apiGet('/api/account/info');
                if (r && r.ok) {
                    window._ALL_SCOPES = r.all_scopes || [];
                    if (el('acc-user')) el('acc-user').value = r.admin_user || '';
                const st = el('acc-save-status');
                    if (st) { st.textContent = '当前账号：' + (r.admin_user || '未设置（当前免登录）'); st.className = 'status'; }
                    const tm = el('acc-test-mode');
                    // 用 === true 而不是 !== false：后端字段缺失时开关要落到
                    // 「关」。写成 !== false 的话缺字段就渲染成「开」，
                    // 与后端 fail-closed 的默认相反，用户会以为动态口令还开着。
                    if (tm) tm.checked = r.test_mode === true;
                    renderScopeChips();
                }
            } catch (e) {
                const st = el('acc-save-status');
                if (st) { st.textContent = '读取失败：' + e; st.className = 'status text-bad'; }
            }
            try {
                const r = await apiGet('/api/account/temps');
                if (r && r.ok) renderTempAccounts(r.accounts || []);
            } catch (e) { /* ignore */ }
            try {
                const r = await apiGet('/api/security/info');
                if (r && r.ok) {
                    const en = el('secwl-enabled'), ips = el('secwl-ips');
                    if (en) en.checked = !!r.enabled;
                    if (ips) ips.value = (r.ips || []).join('\n');
                }
            } catch (e) { /* ignore */ }
        }

        async function saveTestMode(checked) {
            const st = el('tm-status');
            if (st) { st.textContent = '保存中…'; st.className = 'status'; }
            try {
                const r = await apiPost('/api/account/test_mode', { test_mode: !!checked });
                if (r && r.ok) {
                    if (st) { st.textContent = '✅ 测试模式已' + (r.test_mode ? '开启' : '关闭'); st.className = 'status text-ok'; }
                } else {
                    if (st) { st.textContent = r.error || '保存失败'; st.className = 'status text-bad'; }
                }
            } catch (e) {
                if (st) { st.textContent = '保存失败：' + e; st.className = 'status text-bad'; }
            }
        }

        async function saveAccountPassword() {
            const st = el('acc-save-status');
            const user = el('acc-user') ? el('acc-user').value.trim() : '';
            const pass = el('acc-pass') ? el('acc-pass').value : '';
            if (st) { st.textContent = '保存中…'; st.className = 'status'; }
            try {
                const r = await apiPost('/api/account/password', { admin_user: user, admin_pass: pass });
                if (r && r.ok) {
                    if (st) { st.textContent = '✅ ' + (r.message || '账号/密码已更新'); st.className = 'status text-ok'; }
                    if (el('acc-pass')) el('acc-pass').value = '';
                } else {
                    if (st) { st.textContent = r.error || '保存失败'; st.className = 'status text-bad'; }
                }
            } catch (e) {
                if (st) { st.textContent = '保存失败：' + e; st.className = 'status text-bad'; }
            }
        }

        async function saveSecurityWl() {
            const st = el('secwl-status');
            if (st) { st.textContent = '保存中…'; st.className = 'status'; }
            try {
                const r = await apiPost('/api/security/save', {
                    enabled: !!(el('secwl-enabled') && el('secwl-enabled').checked),
                    ips: el('secwl-ips') ? el('secwl-ips').value : ''
                });
                if (r && r.ok) {
                    if (st) { st.textContent = '✅ ' + (r.message || '已保存'); st.className = 'status text-ok'; }
                } else {
                    if (st) { st.textContent = r.error || '保存失败'; st.className = 'status text-bad'; }
                }
            } catch (e) {
                if (st) { st.textContent = '保存失败：' + e; st.className = 'status text-bad'; }
            }
        }

        async function createTempAccount() {
            const st = el('tmp-create-status');
            const days = parseInt(el('tmp-days') ? el('tmp-days').value : 0, 10) || 0;
            const hours = parseInt(el('tmp-hours') ? el('tmp-hours').value : 0, 10) || 0;
            const expires = (days * 86400 + hours * 3600);
            const scopes = _checkedScopes();
            if (!scopes.length) { if (st) { st.textContent = '请至少勾选一个可访问板块'; st.className = 'status text-bad'; } return; }
            if (st) { st.textContent = '生成中…'; st.className = 'status'; }
            try {
                const r = await apiPost('/api/account/temp/create', { expires: expires, scopes: scopes });
                if (r && r.ok) {
                    const box = el('tmp-result');
                    if (box) {
                        const expTxt = (r.expires ? new Date(r.expires * 1000).toLocaleString() : '永久');
                        box.style.display = 'block';
                        box.innerHTML = '<b>临时账户已创建（仅此一次显示）：</b><br>' +
                            '账号：<code>' + esc(r.username) + '</code>　' +
                            '密码：<code>' + esc(r.password) + '</code><br>' +
                            '到期：' + esc(expTxt) + '　可访问板块：' + (r.scopes || []).map(esc).join('、');
                        box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
                    }
                    if (st) st.textContent = '';
                    loadAccount();
                } else {
                    if (st) { st.textContent = r.error || '创建失败'; st.className = 'status text-bad'; }
                }
            } catch (e) {
                if (st) { st.textContent = '创建失败：' + e; st.className = 'status text-bad'; }
            }
        }

        function renderTempAccounts(accounts) {
            const tbody = el('tmp-list');
            if (!tbody) return;
            if (!accounts.length) { tbody.innerHTML = '<tr><td colspan="5" class="empty">暂无临时账户</td></tr>'; return; }
            tbody.innerHTML = accounts.map(function (a) {
                const expTxt = a.expires ? new Date(a.expires * 1000).toLocaleString() : '永久';
                const scopesTxt = (a.scopes || []).map(esc).join('、') || '无';
                const badge = a.expired
                    ? '<span class="text-bad">已过期</span>'
                    : '<span class="text-ok">有效</span>';
                const btn = a.expired ? '<span class="empty">—</span>' : '<button onclick="revokeTemp(' + jsArg(a.id) + ')">撤销</button>';
                return '<tr><td>' + esc(a.username) + '</td><td>' + scopesTxt + '</td><td>' + esc(expTxt) + '</td><td>' + badge + '</td><td>' + btn + '</td></tr>';
            }).join('');
        }

        async function revokeTemp(id) {
            const st = el('tmp-create-status');
            if (!confirm('确认撤销该临时账户？撤销后其账号立即失效。')) return;
            try {
                const r = await apiPost('/api/account/temp/revoke', { id: id });
                if (r && r.ok) {
                    if (st) { st.textContent = '✅ 已撤销'; st.className = 'status text-ok'; }
                    loadAccount();
                } else {
                    if (st) { st.textContent = r.error || '撤销失败'; st.className = 'status text-bad'; }
                }
            } catch (e) {
                if (st) { st.textContent = '撤销失败：' + e; st.className = 'status text-bad'; }
            }
        }


        // ==================== 跨服互联 ====================

        // 本服地址（self_url）从浏览器当前网址推断。
        //
        // 为什么可以自动取
        // ----------------
        // 这个值必须是「主服能回连到本副服的地址」。主服拿到它之后会主动
        // 回连：转发跨服聊天、转发系统通知、推送白名单、推送封禁（见
        // cross.py 的 push_chat_now / _relay_sys 与 web.py 的两个 broadcast）。
        //
        // 而用户正是通过某个网址打开这个面板的 —— 主服要回连的地址，
        // 就是用户此刻用的这个地址。所以 location.origin 是最可信的来源，
        // 也让用户完全不必理解这个字段（原先要手填，且 label 还写反了）。
        //
        // 返回 '' 表示推断失败（file:// 打开、沙箱环境等）。
        // ★ 调用方在返回 '' 时**绝对不能**把空串提交上去，见 saveCross 的说明。
        function crossSelfUrl() {
            try {
                var o = window.location && window.location.origin;
                // file:// 下 origin 是**字符串** "null"，必须挡掉 ——
                // 否则会把字面量 "null" 上报给主服，peers 里就存进一个假地址，
                // 主服还会拿它去回连（必然失败）。
                if (!o || o === 'null' || o.indexOf('http') !== 0) return '';
                return o.replace(/\/+$/, '');
            } catch (e) {
                return '';
            }
        }

        // 按当前角色显隐跨服字段：选主服就不出现副服的设置，反之亦然。
        //
        // 用 .hidden（admin.css 里 display:none）而不是内联 style：
        // 与全站写法一致，也避开 style.display='' 恢复默认值时的浏览器差异。
        //
        // ★ 只切 display，绝不动 input.value。
        //   切到 master 再切回 slave，之前填的主服地址、密码必须还在 ——
        //   否则用户只是换个角色试一下，配置就丢了。
        function syncCrossVisibility() {
            var roleEl = el('cross-role');
            if (!roleEl) return;                    // 区块还没渲染
            var role = roleEl.value || 'none';

            var isSlave = (role === 'slave');
            var active = (role === 'master' || isSlave);   // 只要参与了互服

            var rules = [
                // 角色下拉永远可见，否则用户没有入口选回去，等于把自己锁死
                ['cross-row-role', true],
                // 显示名先填好，选角色时立刻可用
                // （主服做 peers 判重要用它，见 web.py 的 register 分支）
                ['cross-row-name', true],
                // 密码主副都需要：各自校验对方的入站请求
                ['cross-row-pass', active],
                // 主服地址只有副服要主动连，主服是 Hub 被动接收
                ['cross-row-master', isSlave],
                // 同步间隔只有副服的轮询循环用
                ['cross-row-sync', isSlave],
                // 按钮行永远可见，否则改完角色没法保存
                ['cross-row-actions', true]
            ];
            for (var i = 0; i < rules.length; i++) {
                var node = el(rules[i][0]);
                if (!node) continue;
                if (rules[i][1]) node.classList.remove('hidden');
                else node.classList.add('hidden');
            }

            // 「测试连接」只对副服有意义：后端 _handle_cross_test 在主服
            // 直接返回「当前为主服，无需测试连接」，留着纯属误导。
            var testBtn = el('cross-test-btn');
            if (testBtn) {
                if (isSlave) testBtn.classList.remove('hidden');
                else testBtn.classList.add('hidden');
            }

            // 自动上报地址的前提：用户是从「主服能访问到的地址」打开的本页面。
            // 从 localhost 打开就会上报 localhost，主服回连会连到自己或失败 ——
            // 这是方案固有边界，代码消不掉，只能显式提醒。
            var warn = el('cross-self-warn');
            if (warn) {
                if (isSlave) warn.classList.remove('hidden');
                else warn.classList.add('hidden');
            }
        }

        async function loadCross() {
            try {
                const r = await apiGet('/api/cross/config');
                if (!r || r.error) throw new Error(r.error || '读取失败');
                const c = r.config || {};
                el('cross-enabled').checked = !!c.enabled;
                el('cross-role').value = c.role || 'none';
                el('cross-server-name').value = c.server_name || '本服';
                el('cross-password').value = c.password || '';
                el('cross-master-url').value = c.master_url || '';
                // 本服地址不再回填后端旧值 —— 它现在是「自动推断」的，
                // 显示旧值只会误导。直接把当前浏览器地址写进隐藏字段。
                el('cross-self-url').value = crossSelfUrl();
                el('cross-sync').value = c.sync_seconds || 8;
                const it = c.interop || {};
                el('cross-it-chat').checked = it.chat !== false;
                el('cross-it-player').checked = it.player_data !== false;
                el('cross-it-wl').checked = it.whitelist !== false;
                el('cross-it-ban').checked = it.ban !== false;
                // 实验板：默认关闭。后端缺该键（老 cross.json）时也按关闭处理。
                el('cross-experimental').checked = c.experimental === true;
                syncCrossExpHint();
            } catch (e) {
                const s = el('cross-save-status');
                if (s) { s.textContent = '加载失败：' + e; s.className = 'status text-bad'; }
            }
            try {
                const r = await apiGet('/api/cross/status');
                if (!r || r.error) throw new Error(r.error || '读取失败');
                renderCrossStatus(r);
                const badge = el('cross-status-badge');
                if (badge) badge.textContent = r.enabled ? ('● ' + (r.role === 'master' ? '主服' : '从服')) : '○ 未启用';
            } catch (e) {
                const box = el('cross-status-box');
                if (box) box.innerHTML = '<p class="text-bad">读取状态失败：' + esc(String(e)) + '</p>';
            }
            // 角色是从后端读回来的，读完必须同步一次显隐 ——
            // 否则刷新页面后会看到与当前角色不符的字段。
            syncCrossVisibility();
        }

        // 实验板提示文案随开关状态切换：关闭时只显示说明，打开时才显示风险警告。
        // 默认关着却一直挂个红字警告，会让人以为服务器已经出问题了。
        function syncCrossExpHint() {
            const cb = el('cross-experimental');
            if (!cb) return;
            const warn = el('cross-exp-warn');
            if (warn) warn.style.display = cb.checked ? '' : 'none';
        }

        function renderCrossStatus(d) {
            const box = el('cross-status-box');
            if (!box) return;

            const role = d.role || 'none';

            // 未参与互服：卡片会全是「未参与 / 0 / —」，没有任何信息量，
            // 看着像坏了。直接一句话说明当前状态。
            if (role === 'none') {
                box.innerHTML = '<p class="hint">未参与跨服互联。'
                    + '在上方选择「主服务器」或「副服务器」并保存后，这里会显示运行状态。</p>';
                return;
            }

            const isMaster = (role === 'master');
            const peers = d.peers || {};
            const peerKeys = Object.keys(peers);

            // 主服：peers 是**本机登记的从服**（主服自己维护的注册表）→「已注册从服」
            // 副服：peers 是主服回包里带下来的**其他**副服（不含自己）→
            //       沿用「已注册从服」会让副服主以为自己在管理这些机器，必须区分措辞
            const peersTitle = isMaster ? '已注册从服：' : '主服上报的其他副服：';
            let peersHtml;
            if (peerKeys.length) {
                peersHtml = '<ul class="peers-list">';
                peerKeys.forEach(function (k) {
                    peersHtml += '<li>' + esc(k) + ' → ' + esc(peers[k]) + '</li>';
                });
                peersHtml += '</ul>';
            } else {
                peersHtml = isMaster ? '（暂无副服连入）' : '（主服未上报其他副服）';
            }
            const card = function (label, val, color) {
                var tone = 'is-ok';
                if (color === '#e94560') tone = 'is-bad';
                else if (color === '#888') tone = 'is-idle';
                // ★ val 必须转义：d.server_name 是用户自己填的、
                //   d.last_error 来自异常字符串（可能含 < >），原样拼接是同一条注入路径。
                return '<div class="stat ' + tone + '"><div class="k">' + esc(label)
                    + '</div><div class="v">' + esc(val) + '</div></div>';
            };
            let cards = [
                card('本服角色', isMaster ? '主服务器' : '副服务器', '#4ecca3'),
                card('本服名称', d.server_name || '未设置')
            ];

            // 「已互通玩家数」只有主服有意义：这个数只在主服的转发路径里累加，
            // 副服恒为 0 —— 给副服显示会让人误以为互通没生效。
            // 副服改成显示「正在连哪个主服」，那才是副服真正要盯的信息。
            if (isMaster) {
                cards.push(card('已互通玩家数', String(d.players || 0)));
            } else {
                cards.push(card('主服地址', d.master_url || '未设置',
                    d.master_url ? '#4ecca3' : '#e94560'));
            }

            cards.push(card('最近心跳', d.last_ping ? (new Date(d.last_ping * 1000).toLocaleTimeString()) : '—', d.last_ping ? '#4ecca3' : '#e94560'));
            if (d.last_error) cards.push(card('最近错误', d.last_error, '#e94560'));
            box.innerHTML = '<div class="row">' + cards.join('') + '</div>' +
                '<div class="hint mt-sm">' + esc(peersTitle) + '</div><div class="hint">' + peersHtml + '</div>';
        }

        async function saveCross() {
            const st = el('cross-save-status');
            if (st) { st.textContent = '保存中…'; st.className = 'status'; }
            try {
                const payload = {
                    enabled: el('cross-enabled').checked,
                    role: el('cross-role').value,
                    server_name: el('cross-server-name').value,
                    password: el('cross-password').value,
                    master_url: el('cross-master-url').value,
                    sync_seconds: parseFloat(el('cross-sync').value) || 8,
                    interop: {
                        chat: el('cross-it-chat').checked,
                        player_data: el('cross-it-player').checked,
                        whitelist: el('cross-it-wl').checked,
                        ban: el('cross-it-ban').checked
                    },
                    experimental: el('cross-experimental').checked
                };
                // 本服地址（self_url）：用自动推断的浏览器地址 —— 主服靠它回连本副服。
                //
                // ★ 推断失败时（返回 ''）**绝不能提交空串**：
                //   后端见到空串会把该字段清空，主服随即把这个副服从 peers 里删掉，
                //   于是跨服聊天 / 系统通知 / 白名单 / 封禁推送全部**静默失效**
                //   （不报错、只是不工作，最难排查）。
                //   所以这里选择「整个键都不传」，让后端用 .get(key, 旧值) 保留原值。
                var selfUrl = crossSelfUrl();
                if (selfUrl) payload.self_url = selfUrl;
                const r = await apiPost('/api/cross/config/save', payload);
                if (r && r.ok) {
                    if (st) { st.textContent = '已保存并重新加载跨服模块'; st.className = 'status text-ok'; }
                    loadCross();
                } else {
                    if (st) { st.textContent = r.error || '保存失败'; st.className = 'status text-bad'; }
                }
            } catch (e) {
                if (st) { st.textContent = '保存失败：' + e; st.className = 'status text-bad'; }
            }
        }

        async function testCross() {
            const st = el('cross-save-status');
            if (st) { st.textContent = '测试中…'; st.className = 'status'; }
            try {
                const r = await apiPost('/api/cross/test', {});
                if (r && r.ok) {
                    if (st) { st.textContent = r.message || '连接成功'; st.className = 'status text-ok'; }
                } else {
                    if (st) { st.textContent = r.error || '连接失败'; st.className = 'status text-bad'; }
                }
            } catch (e) {
                if (st) { st.textContent = '测试失败：' + e; st.className = 'status text-bad'; }
            }
        }

        // ==================== QQ机器人 ====================
        //
        // 多机器人隔离：当前编辑的卡片 id 存在模块级变量里，所有群管理请求
        // 都带上它。不这样做的话后端只能按「主卡」处理，A 卡申请的验证码
        // 会出现在 B 卡的编辑页里。
        //
        // __botEditSeq 是请求代次：打开 A 卡 → 请求尚未返回 → 关掉再打开
        // B 卡 → A 的响应到达时会把 B 的群列表覆盖成 A 的。每次切换卡片都
        // 自增序号，回调里比对序号，不是自己那一代就直接丢弃。
        var __botEditId = '';
        var __botEditSeq = 0;

        function qqCardIdQuery() {
            return __botEditId ? ('?id=' + encodeURIComponent(__botEditId)) : '';
        }



        async function qqFetchGroupList() {
            var mySeq = __botEditSeq;
            try {
                const r = await apiGet('/api/qqbot/status' + qqCardIdQuery());
                if (mySeq !== __botEditSeq) return;   // 已切到别的卡片，丢弃过期响应
                if (!r.ok) throw new Error(r.error || '失败');
                renderQQGroups(r.groups || [], r.group_openid || '', r.pending_codes || []);
            } catch (e) {
                if (mySeq !== __botEditSeq) return;
                const box = el('qq-group-list');
                if (box) box.innerHTML = '<p class="text-bad">读取群列表失败：' + esc(String(e)) + '</p>';
            }
        }

        function renderQQGroups(groups, primary, pendingCodes) {
            const box = el('qq-group-list');
            if (!box) return;
            let html = '';
            let has = false;
            if (primary) {
                has = true;
                html += '<div class="row list-divider">' +
                    '<span class="badge badge-ok">主群</span>' +
                    '<input type="text" value="' + escAttr(primary) + '" class="block-input" placeholder="主群显示名" data-gopenid="' + escAttr(primary) + '">' +
                    '<button onclick="qqSaveGroupName(this)" class="ok">保存</button>' +
                    '</div>';
            }
            (groups || []).forEach(function (g) {
                has = true;
                html += '<div class="row list-divider">' +
                    '<span class="badge badge-ok">已绑定</span>' +
                    '<input type="text" value="' + escAttr(g.name || g.openid) + '" class="block-input" placeholder="群显示名" data-gopenid="' + escAttr(g.openid || '') + '">' +
                    '<button onclick="qqSaveGroupName(this)" class="ok">保存</button>' +
                    '<span class="hint-sm break">' + esc(g.openid || '') + '</span>' +
                    '</div>';
            });
            (pendingCodes || []).forEach(function (p) {
                has = true;
                html += '<div class="row list-divider">' +
                    '<span class="badge badge-warn">待绑定</span>' +
                    '<b class="code-lg">' + esc(p.code) + '</b>' +
                    '<span class="hint-sm">把机器人拉进目标群，然后在<b>那个群</b>里发送该验证码</span>' +
                    '<span class="hint-sm break">同时只保留一个有效码；申请新码会作废旧码，成功加入一个群后此码自动注销。</span>' +
                    '<span class="hint-sm break">若在已绑定的群里发送，机器人会提示「已绑定」而不会消耗此码。</span>' +
                    '</div>';
            });
            if (!has) {
                html = '<p class="empty">尚未绑定任何群。点击上方「加入新群」获取 4 位验证码，再把机器人拉入目标群并发送验证码即可完成绑定。</p>';
            }
            html += '<p class="hint-sm mt-sm">提示：修改名字后点对应「保存」按钮；也可在 QQ 群内发送「设置群名 名字」快速重命名。</p>';
            box.innerHTML = html;
        }

        async function qqSaveGroupName(btn) {
            const row = btn.closest('div');
            if (!row) return;
            const input = row.querySelector('input[data-gopenid]');
            if (!input) return;
            const openid = input.getAttribute('data-gopenid') || '';
            const name = input.value.trim() || openid;
            const old = btn.textContent;
            btn.textContent = '保存中…';
            try {
                const r = await apiPost('/api/qqbot/group/rename',
                    { id: __botEditId, openid: openid, name: name });
                if (r && r.ok) {
                    btn.textContent = '已保存';
                    setTimeout(function(){ btn.textContent = old; }, 1500);
                    qqFetchGroupList();
                } else {
                    btn.textContent = '失败';
                    setTimeout(function(){ btn.textContent = old; }, 1500);
                }
            } catch (e) {
                btn.textContent = '失败';
                setTimeout(function(){ btn.textContent = old; }, 1500);
            }
        }







        let __toolsCfg = null;
        function fmtBytes(n) {
            n = Number(n) || 0;
            const u = ['B', 'KB', 'MB', 'GB'];
            let i = 0;
            while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
            return (i === 0 ? n.toFixed(0) : n.toFixed(2)) + ' ' + u[i];
        }
        function setToolsStatus(id, msg, ok) {
            const s = document.getElementById(id);
            if (s) { s.textContent = (ok ? '成功: ' : '') + msg; s.className = ok ? 'status text-ok' : 'status text-bad'; }
        }
        function renderBackupState(cfg) {
            // cfg / cfg.backup 都可能缺失：loadToolsConfig 只在 r.config 存在时
            // 才调用本函数，但后端完全可能返回一个没有 backup 键的 config
            // （旧版本配置文件、字段被裁剪等）。L2308 直接读 cfg.backup.enabled
            // 会抛 TypeError 并冒泡到 loadToolsConfig 的 catch，导致
            // renderBackupList 也不再执行 —— 整个「自动备份」板块空白。
            cfg = cfg || {};
            const st = cfg.backup_state || {};
            const bk = cfg.backup || {};
            const box = el('backup-state-box');
            if (!box) return;
            const badge = el('backup-state-badge');
            const lines = [];
            if (st.running) {
                lines.push('<span class="text-warn">⏳ 备份正在进行中…</span>');
                if (badge) { badge.textContent = '备份中'; badge.className = 'status text-warn'; }
            } else {
                if (badge) { badge.textContent = bk.enabled ? '自动备份已开启' : '自动备份已关闭'; badge.style.color = bk.enabled ? '#4ef09a' : '#888'; }
            }
            if (st.last_result && st.last_result[0] === true) {
                lines.push('上次备份：<span class="text-ok">' + esc(String(st.last_result[1])) + '</span>');
            } else if (st.last_result && st.last_result[0] === false) {
                lines.push('上次备份：<span class="text-bad">' + esc(String(st.last_result[1])) + '</span>');
            } else {
                lines.push('上次备份：<span class="empty">尚未执行</span>');
            }
            const next = Number(st.next_run) || 0;
            if (next > 0 && bk.enabled) {
                const d = new Date(next * 1000);
                const pad = function (x) { return (x < 10 ? '0' : '') + x; };
                lines.push('下次自动备份：' + d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes()));
            }
            box.innerHTML = lines.join('<br>');
        }
        function renderBackupList(cfg) {
            const box = el('backup-list-box');
            if (!box) return;
            // 备份文件名可能来自用户上传的存档命名，属可控输入；
            // 既进 onclick（JS 串）又进 innerHTML，两处都要处理。
            const list = (cfg && cfg.backups) || [];
            if (!list.length) { box.innerHTML = '<p class="empty">暂无备份文件</p>'; return; }
            const rows = list.map(function (b) {
                return '<div class="list-item">'
                    + '<span class="break">📄 ' + esc(b.name) + '<br><small class="text-accent">' + esc(fmtBytes(b.size)) + '　' + esc(b.mtime) + '</small></span>'
                    + '<span class="row">'
                    + '<button class="ok" onclick="restoreBackup(' + jsArg(b.name) + ')">恢复</button>'
                    + '<button onclick="deleteBackup(' + jsArg(b.name) + ')">删除</button></span></div>';
            });
            box.innerHTML = rows.join('');
        }
        async function loadToolsConfig() {
            try {
                const r = await apiGet('/api/tools/config');
                if (!r || !r.config) return;
                __toolsCfg = r.config;
                const b = __toolsCfg.backup || {}, c = __toolsCfg.cloud_blacklist || {};
                const B = function (v, def) { return v === undefined ? def : !!v; };
                if (el('backup-enabled')) {
                    el('backup-enabled').checked = B(b.enabled, false);
                    el('backup-interval').value = Number(b.interval_hours) || 24;
                    el('backup-keep').value = Number(b.max_keep) || 10;
                    el('backup-sources').value = (typeof b.source_paths === 'string') ? (b.source_paths || '') : ((b.source_paths || []).join(String.fromCharCode(10)));
                }
                if (el('cloud-enabled')) {
                    el('cloud-enabled').checked = B(c.enabled, false);
                    el('cloud-check-login').checked = B(c.check_on_login, true);
                    el('cloud-sync-default').checked = B(c.sync_default, false);
                    el('cloud-base').value = c.api_base || '';
                    el('cloud-token').value = (c.token === '__SET__') ? '' : (c.token || '');
                    el('cloud-server-name').value = c.server_name || '';
                    el('cloud-kick-message').value = c.kick_message || '';
                    if (el('cloud-api-style')) {
                        // 老配置没有 api_style 字段 → 落到 auto，与后端默认一致
                        const st = String(c.api_style || 'auto').toLowerCase();
                        el('cloud-api-style').value = (st === 'rest' || st === 'legacy') ? st : 'auto';
                    }
                    if (el('ban-sync-cloud')) el('ban-sync-cloud').checked = B(c.sync_default, false);
                }
                renderBackupState(__toolsCfg);
                renderBackupList(__toolsCfg);
            } catch (e) { console.error('loadToolsConfig', e); }
        }
        async function saveToolsConfig() {
            const payload = {
                backup: {
                    enabled: el('backup-enabled').checked,
                    interval_hours: (function(){var v=parseFloat(el('backup-interval').value); return (isFinite(v)&&v>0)?v:24;})(),
                    max_keep: (function(){var v=parseInt(el('backup-keep').value,10); return (isFinite(v)&&v>=1)?v:10;})(),
                    source_paths: el('backup-sources').value
                },
                cloud_blacklist: {
                    enabled: el('cloud-enabled').checked,
                    check_on_login: el('cloud-check-login').checked,
                    sync_default: el('cloud-sync-default').checked,
                    api_base: el('cloud-base').value.trim(),
                    token: el('cloud-token').value.trim(),
                    server_name: el('cloud-server-name').value.trim(),
                    kick_message: el('cloud-kick-message').value.trim(),
                    api_style: el('cloud-api-style') ? el('cloud-api-style').value : 'auto'
                }
            };
            try {
                const r = await apiPost('/api/tools/config/save', payload);
                setToolsStatus('backup-save-status', r.message || '', !!r.ok);
                setToolsStatus('cloud-save-status', r.ok ? '已保存' : (r.error || '保存失败'), !!r.ok);
                loadToolsConfig();
                // 保存后端点缓存被清（地址/协议可能变了），顺手重测一次，
                // 让状态卡立刻反映新配置，而不是等用户想到再点测试。
                if (r.ok) testCloud();
            } catch (e) { setToolsStatus('cloud-save-status', '保存失败: ' + e, false); }
        }
        async function triggerBackup() {
            try {
                const r = await apiPost('/api/backup/trigger', {});
                setToolsStatus('backup-save-status', r.message || r.error || '', !!r.ok);
                const en = el('backup-enabled') ? el('backup-enabled').checked : false;
                renderBackupState({ backup: { enabled: en }, backup_state: r.ok ? { running: true } : {} });
                setTimeout(loadToolsConfig, 1600);
            } catch (e) { setToolsStatus('backup-save-status', '启动失败: ' + e, false); }
        }
        async function deleteBackup(name) {
            if (!confirm('确认删除备份 ' + name + ' ？')) return;
            try {
                const r = await apiPost('/api/backup/delete', { name: String(name) });
                setToolsStatus('backup-save-status', r.message || r.error || '', !!r.ok);
                loadToolsConfig();
            } catch (e) { setToolsStatus('backup-save-status', '删除失败: ' + e, false); }
        }
        async function restoreBackup(name) {
            if (!confirm('确认把备份「' + name + '」复制到服务器根目录？不会在运行时换档。')) return;
            try {
                const r = await apiPost('/api/backup/restore', { name: String(name) });
                setToolsStatus('backup-save-status', r.ok ? '已复制，请按弹窗说明手动切换' : (r.error || r.message || '失败'), !!r.ok);
                if (r.ok && r.message) alert(r.message);
            } catch (e) { setToolsStatus('backup-save-status', '' + e, false); }
        }
        async function testCloud() {
            const st = el('cloud-save-status');
            const card = el('cloud-status-card');
            if (st) { st.textContent = '检测中…'; st.className = 'status text-warn'; }
            try {
                const r = await apiPost('/api/cloud/test', {});
                setToolsStatus('cloud-save-status', (r.message || '测试完成') + (r.ok ? '（连接正常）' : '（连接失败）'), !!r.ok);
                if (card) {
                    const role = (r.data && r.data._role) || '';
                    const roleText = role === 'admin' ? '管理员（可封禁/解封）'
                                   : role === 'readonly' ? '只读 Key（仅可查黑）'
                                   : '未识别（旧版接口无此能力）';
                    const roleTone = role === 'admin' ? 'text-ok' : (role === 'readonly' ? 'text-warn' : 'text-mute');
                    card.innerHTML = ''
                        + '<div class="row"><b class="spacer">接入状态</b>'
                        + '<span class="badge ' + (r.ok ? 'badge-ok' : 'badge-bad') + '">● ' + (r.ok ? '连接正常' : '连接失败') + '</span></div>'
                        + '<div class="hint-sm mt-xs">接口协议：<b>' + esc(r.api_style || '未知') + '</b></div>'
                        + '<div class="hint-sm">身份：<span class="' + roleTone + '">' + esc(roleText) + '</span></div>'
                        + '<div class="hint-sm break">' + esc(r.message || '') + '</div>';
                }
                // 连上了就顺手拉一次总览与明细，省得用户再点两个刷新
                if (r.ok) { loadCloudStats(); loadCloudBans(); }
            } catch (e) { setToolsStatus('cloud-save-status', '测试失败: ' + e, false); }
        }

        // ---------------- 云黑：总览 / 明细 / 手动查黑 ----------------
        //
        // 这三块都依赖新版 REST 接口。旧版服务上会返回
        // ok:false + 「不支持列表查询（旧版接口）」，这里把它当成一句
        // 普通说明展示，**不弹错误** —— 旧版服务本来就没有这些能力，
        // 报错只会让用户以为配置坏了。
        async function loadCloudStats() {
            const box = el('cloud-stats-box');
            const st = el('cloud-stats-status');
            if (st) { st.textContent = '加载中…'; st.className = 'status'; }
            try {
                const r = await apiGet('/api/cloud/stats');
                if (!r || !r.ok) {
                    if (box) box.innerHTML = '<p class="hint">' + esc((r && r.message) || '加载失败') + '</p>';
                    if (st) { st.textContent = '不可用'; st.className = 'status text-mute'; }
                    return;
                }
                const s = r.stats || {};
                const cell = function (n, label, tone) {
                    return '<div class="stat-cell"><b class="' + (tone || '') + '">' + esc(String(n == null ? '-' : n)) + '</b>'
                         + '<span class="hint-sm">' + esc(label) + '</span></div>';
                };
                if (box) {
                    box.innerHTML = '<div class="row">'
                        + cell(s.active_bans, '当前有效云黑', 'text-warn')
                        + cell(s.bedrock, '基岩版分组')
                        + cell(s.java, 'Java 版分组')
                        + '</div>';
                }
                if (st) { st.textContent = '✓'; st.className = 'status text-ok'; }
            } catch (e) {
                if (box) box.innerHTML = '<p class="hint">加载失败: ' + esc(String(e)) + '</p>';
                if (st) { st.textContent = '失败'; st.className = 'status text-bad'; }
            }
        }

        function fmtExpire(ts) {
            const n = Number(ts);
            // 0 / null / 非法 一律视为永久（与文档「expires=0 表示永久」一致）
            if (!isFinite(n) || n <= 0) return '永久';
            const d = new Date(n * 1000);
            return d.toLocaleString();
        }

        async function loadCloudBans() {
            const tb = el('cloud-ban-list');
            const st = el('cloud-bans-status');
            const hint = el('cloud-bans-hint');
            if (st) { st.textContent = '加载中…'; st.className = 'status'; }
            try {
                // 本插件面向 BDS，只看基岩版分组
                const r = await apiGet('/api/cloud/list?edition=bedrock');
                if (!r || !r.ok) {
                    if (tb) tb.innerHTML = '';
                    const m = (r && r.message) || '加载失败';
                    if (hint) hint.textContent = m;
                    if (st) { st.textContent = '不可用'; st.className = 'status text-mute'; }
                    return;
                }
                const bans = r.bans || [];
                if (hint) hint.textContent = '';
                if (!bans.length) {
                    if (tb) tb.innerHTML = '<tr><td colspan="5" class="hint">云黑列表为空</td></tr>';
                } else if (tb) {
                    tb.innerHTML = bans.map(function (b) {
                        return '<tr>'
                            + '<td>' + esc(String(b.player_name || '-')) + '</td>'
                            + '<td class="break">' + esc(String(b.xuid || '-')) + '</td>'
                            + '<td>' + esc(String(b.reason || '-')) + '</td>'
                            + '<td>' + esc(String(b.server_name || '-')) + '</td>'
                            + '<td>' + esc(fmtExpire(b.expires)) + '</td>'
                            + '</tr>';
                    }).join('');
                }
                if (st) { st.textContent = '✓ ' + bans.length + ' 条'; st.className = 'status text-ok'; }
            } catch (e) {
                if (tb) tb.innerHTML = '';
                if (hint) hint.textContent = '加载失败: ' + e;
                if (st) { st.textContent = '失败'; st.className = 'status text-bad'; }
            }
        }

        async function cloudLookup() {
            const box = el('cloud-lookup-result');
            const xuid = el('cloud-lookup-xuid') ? el('cloud-lookup-xuid').value.trim() : '';
            const ip = el('cloud-lookup-ip') ? el('cloud-lookup-ip').value.trim() : '';
            if (!xuid && !ip) {
                if (box) { box.innerHTML = '<span class="text-warn">请至少填写 XUID 或 IP</span>'; }
                return;
            }
            if (box) box.innerHTML = '<span class="text-warn">查询中…</span>';
            try {
                const r = await apiPost('/api/cloud/lookup', { xuid: xuid, ip_address: ip });
                if (!box) return;
                if (!r || !r.ok) {
                    box.innerHTML = '<span class="text-bad">✗ ' + esc((r && r.message) || '查询失败') + '</span>';
                    return;
                }
                if (r.ban) {
                    box.innerHTML = '<span class="text-bad">● 命中云黑</span>　'
                        + '原因：<b>' + esc(String(r.ban.reason || '-')) + '</b>'
                        + '　来源：' + esc(String(r.ban.server_name || '-'))
                        + '　分组：' + esc(String(r.ban.edition || '-'));
                } else {
                    box.innerHTML = '<span class="text-ok">○ 未命中云黑</span>';
                }
            } catch (e) {
                if (box) box.innerHTML = '<span class="text-bad">✗ ' + esc(String(e)) + '</span>';
            }
        }

        // ==================== 签到与绑定 ====================
        async function loadGameTools() {
            const st = el('gametools-status');
            try {
                const r = await apiGet('/api/gametools');
                if (!r || !r.ok) throw new Error((r && r.error) || '加载失败');
                const B = function (v, def) { return (v === undefined || v === null) ? def : !!v; };
                if (el('bind-enabled')) {
                    el('bind-enabled').checked = B(r.binding.enabled, true);
                    el('bind-require').checked = B(r.binding.require_on_join, true);
                    el('bind-digits').value = r.binding.code_digits || 5;
                    el('bind-ttl').value = r.binding.code_ttl_seconds || 300;
                    el('signin-enabled').checked = B(r.signin.enabled, true);
                    el('signin-cmd').value = r.signin.command || '签到';
                    el('signin-item').value = r.signin.item_id || 'minecraft:diamond';
                    el('signin-amount').value = r.signin.amount || 1;
                }
                const dlist = r.delayed || [];
                if (el('delayed-count')) el('delayed-count').textContent = String(dlist.length);
                const db = el('delayed-list-box');
                if (db) {
                    if (!dlist.length) { db.innerHTML = '<p class="empty">暂无待发放奖励</p>'; }
                    else {
                        db.innerHTML = dlist.map(function (t) {
                            return '<div class="list-item">'
                                + esc(t.player) + ' +' + esc(String(t.amount)) + ' ' + esc(t.item)
                                + ' <span class="empty">(' + esc(t.source || 'delay') + ') ' + esc(t.created || '') + '</span></div>';
                        }).join('');
                    }
                }
                const blist = r.bindings || [];
                const bb = el('bindings-list-box');
                if (bb) {
                    if (!blist.length) { bb.innerHTML = '<p class="empty">暂无绑定</p>'; }
                    else {
                        bb.innerHTML = blist.map(function (b) {
                            return '<div class="list-item">'
                                + esc(b.mc) + ' ↔ QQ ' + esc(b.qq) + ' <span class="empty">' + esc(b.bound_at || '') + '</span></div>';
                        }).join('');
                    }
                }
                if (st) { st.textContent = '✓ ' + dlist.length + ' 个待发奖励 / ' + blist.length + ' 个绑定'; st.className = 'status text-ok'; }
            } catch (e) {
                if (st) { st.textContent = '✗ ' + e; st.className = 'status text-bad'; }
            }
        }

        async function saveGameTools() {
            const payload = {
                binding: {
                    enabled: el('bind-enabled').checked,
                    require_on_join: el('bind-require').checked,
                    code_digits: (function(){var v=parseInt(el('bind-digits').value,10);return (isFinite(v)&&v>=4&&v<=8)?v:5;})(),
                    code_ttl_seconds: (function(){var v=parseInt(el('bind-ttl').value,10);return (isFinite(v)&&v>=60)?v:300;})()
                },
                signin: {
                    enabled: el('signin-enabled').checked,
                    command: el('signin-cmd').value.trim() || '签到',
                    item_id: el('signin-item').value.trim() || 'minecraft:diamond',
                    amount: (function(){var v=parseInt(el('signin-amount').value,10);return (isFinite(v)&&v>=1)?v:1;})()
                }
            };
            try {
                const r = await apiPost('/api/gametools/save', payload);
                const s = el('gametools-save-status');
                if (s) { s.textContent = r.message || (r.ok ? '已保存' : '保存失败'); s.className = r.ok ? 'status text-ok' : 'status text-bad'; }
                loadGameTools();
            } catch (e) {
                const s = el('gametools-save-status');
                if (s) { s.textContent = '保存失败: ' + e; s.className = 'status text-bad'; }
            }
        }

        // ==================== 子插件（.gmmod / .gmlib） ====================
        var gmodCurrent = null;   // 当前在文件查看器里打开的子插件 uuid

        function gmodStatus(id, msg, ok) {
            const s = el(id);
            if (!s) return;
            s.textContent = msg || '';
            s.className = 'status ' + (ok === true ? 'text-ok' : (ok === false ? 'text-bad' : 'text-warn'));
        }

        function gmodTypeLabel(t) {
            if (t === 'index') return '<span class="badge">🌐 官网首页</span>';
            if (t === 'admin') return '<span class="badge">🗄 管理后台</span>';
            if (t === 'py') return '<span class="badge">🐍 可执行代码</span>';
            return '<span class="badge">🧩 代码型</span>';
        }

        async function loadGmods() {
            gmodStatus('gmods-status', '加载中…', null);
            try {
                const r = await apiGet('/api/gmods/list');
                if (!r || !r.ok) throw new Error((r && r.error) || '加载失败');
                const mods = r.mods || [];
                const libs = r.libs || [];
                if (el('gmod-count')) el('gmod-count').textContent = String(mods.length);
                if (el('gmod-lib-count')) el('gmod-lib-count').textContent = String(libs.length);

                // 接管状态概览：只看页面型子插件
                const st = el('gmod-page-state');
                if (st) {
                    const on = mods.filter(function (m) { return m.enabled && (m.type === 'index' || m.type === 'admin'); });
                    if (!on.length) {
                        st.innerHTML = '当前 <b>没有</b> 子插件接管页面，官网与后台使用面板默认页面。';
                    } else {
                        st.innerHTML = '当前接管：' + on.map(function (m) {
                            return (m.type === 'admin' ? '/admin' : '官网首页') + ' ← <b>' + esc(m.name || m.uuid) + '</b>';
                        }).join('　|　');
                    }
                }

                const box = el('gmod-box');
                if (box) {
                    if (!mods.length) {
                        box.innerHTML = '<p class="empty">尚未安装任何子插件</p>';
                    } else {
                        box.innerHTML = mods.map(function (m) {
                            const q = JSON.stringify(String(m.uuid || '')).replace(/"/g, '&quot;');
                            const dep = (m.libs && m.libs.length) ? esc(m.libs.join('、')) : '无';
                            const miss = (m.missing_libs && m.missing_libs.length)
                                ? '<span class="text-bad">　缺少依赖：' + esc(m.missing_libs.join('、')) + '</span>' : '';
                            const state = m.enabled
                                ? '<span class="text-ok">● 已启用</span>'
                                : '<span class="text-accent">○ 未启用</span>';
                            const toggle = m.enabled
                                ? '<button onclick="disableGmod(' + q + ')">⏹ 停用</button>'
                                : '<button class="ok" onclick="enableGmod(' + q + ')">▶ 启用</button>';
                            let preview = '';
                            if (m.type === 'index') preview = '<a class="btn-link" href="/" target="_blank" rel="noopener">🌐 打开首页</a>';
                            if (m.type === 'admin') preview = '<a class="btn-link" href="/admin" target="_blank" rel="noopener">🗄 打开后台</a>';
                            // py 型：补一条「入口 / 权限」信息，方便确认它到底跑没跑、能读什么
                            let extra = '';
                            if (m.type === 'py') {
                                const run = m.running
                                    ? '<span class="text-ok">● 运行中</span>'
                                    : '<span class="text-mute">○ 未运行</span>';
                                const perms = (m.permissions && m.permissions.length)
                                    ? esc(m.permissions.join('、'))
                                    : '未声明（仅游戏侧能力）';
                                extra = '<div class="hint-sm break">入口: ' + esc(m.entry || 'main/main.py')
                                    + '　' + run + '　权限: ' + perms + '</div>';
                            }
                            return '<div class="list-item">'
                                + '<span class="break"><b>' + esc(m.name || m.uuid) + '</b> '
                                + '<span class="text-accent">v' + esc(m.version || '?') + '</span> '
                                + gmodTypeLabel(m.type) + ' ' + state
                                + '<span class="help-q help-q-label" data-help="'
                                + helpAttr(m.description || '该子插件未提供描述（manifest.json 中的 description 字段为空）')
                                + '" role="button" tabindex="0" aria-label="查看子插件描述">描述</span>'
                                + '<div class="hint-sm break">uuid: ' + esc(m.uuid) + '　依赖: ' + dep + miss + '</div>'
                                + extra
                                + '</span>'
                                + toggle
                                + (m.has_settings
                                    ? '<button onclick="gmodOpenSettings(' + q + ')" title="该子插件的独立设置项">⚙ 设置</button>'
                                    : '')
                                + '<button onclick="viewGmod(' + q + ')" title="查看包内文件">📄</button>'
                                + '<button onclick="uninstallGmod(' + q + ')" title="删除子插件文件">🗑</button>'
                                + preview
                                + '</div>';
                        }).join('');
                    }
                }

                const lb = el('gmod-lib-box');
                if (lb) {
                    lb.innerHTML = libs.length
                        ? libs.map(function (n) { return '<div class="list-item break">📦 ' + esc(n) + '</div>'; }).join('')
                        : '<p class="empty">暂无依赖包</p>';
                }
                if (el('gmod-paths')) {
                    el('gmod-paths').textContent = '子插件目录：' + (r.mod_dir || '') + '　依赖目录：' + (r.libs_dir || '');
                }
                gmodStatus('gmods-status', '共 ' + mods.length + ' 个子插件、' + libs.length + ' 个依赖', true);
            } catch (e) {
                gmodStatus('gmods-status', '加载失败: ' + e, false);
            }
        }

        async function uploadGmod() {
            const fileInput = el('gmod-file');
            const f = fileInput && fileInput.files ? fileInput.files[0] : null;
            if (!f) { gmodStatus('gmod-upload-status', '请先选择 .gmmod / .gmlib 文件', false); return; }
            if (!/\.(gmmod|gmlib)$/i.test(f.name)) { gmodStatus('gmod-upload-status', '只支持 .gmmod / .gmlib', false); return; }
            const fd = new FormData();
            fd.append('file', f);
            gmodStatus('gmod-upload-status', '上传并导入中…', null);
            try {
                const res = await fetch(apiPath('/api/gmods/upload'), { method: 'POST', body: fd });
                const r = await res.json();
                gmodStatus('gmod-upload-status', (r.ok ? '✓ ' : '✗ ') + (r.message || r.error || '导入失败'), !!r.ok);
                if (r.ok) { fileInput.value = ''; loadGmods(); }
            } catch (e) {
                gmodStatus('gmod-upload-status', '上传失败: ' + e, false);
            }
        }

        async function enableGmod(uuid) {
            try {
                const r = await apiPost('/api/gmods/enable', { uuid: String(uuid) });
                gmodStatus('gmods-status', (r.ok ? '✓ ' : '✗ ') + (r.message || r.error || ''), !!r.ok);
                loadGmods();
            } catch (e) {
                gmodStatus('gmods-status', '启用失败: ' + e, false);
            }
        }

        async function disableGmod(uuid) {
            try {
                const r = await apiPost('/api/gmods/disable', { uuid: String(uuid) });
                gmodStatus('gmods-status', (r.ok ? '✓ ' : '✗ ') + (r.message || r.error || ''), !!r.ok);
                loadGmods();
            } catch (e) {
                gmodStatus('gmods-status', '停用失败: ' + e, false);
            }
        }

        async function uninstallGmod(uuid) {
            if (!confirm('确认删除子插件 ' + uuid + ' ？\n目录与原始包都会被移除，此操作不可撤销。')) return;
            try {
                const r = await apiPost('/api/gmods/uninstall', { uuid: String(uuid) });
                gmodStatus('gmods-status', (r.ok ? '✓ ' : '✗ ') + (r.message || r.error || ''), !!r.ok);
                if (gmodCurrent === String(uuid)) closeGmodViewer();
                loadGmods();
            } catch (e) {
                gmodStatus('gmods-status', '删除失败: ' + e, false);
            }
        }

        // ---------- 子插件独立设置页 ----------
        //
        // 字段由子插件包内的 settings.json **声明**，这里只负责按声明渲染控件。
        // 刻意不支持子插件自带 HTML —— 那等于把任意 JS 注入到管理员会话里，
        // 而声明式只允许出现面板认识的这几种控件，注入面为零。
        //
        // 控件 id 约定：gmodset-<uuid>-<key>。uuid 里可能有连字符，但 key
        // 已被主插件校验为纯标识符（字母/数字/下划线），所以从右往左切是安全的。

        function gmodSetCtlId(uuid, key) {
            return 'gmodset-' + String(uuid) + '-' + String(key);
        }

        // 密码字段的面罩串，必须与主插件 mod_loader.SETTINGS_MASK 一致。
        // 前端看到它就原样提交回去，主插件会识别并跳过（保留磁盘原值）。
        var GMOD_SET_MASK = '******';

        function gmodFieldHtml(uuid, f) {
            const id = gmodSetCtlId(uuid, f.key);
            const label = esc(f.label || f.key);
            let inner = '';
            if (f.type === 'bool') {
                // bfBool 用的 ios 开关带上文案，跟机器人编辑框保持一致
                return '<div class="mb-sm"><label class="ios" data-on="启用" data-off="关闭">'
                    + '<input type="checkbox" id="' + escAttr(id) + '"' + (f.default === true ? ' checked' : '') + '><i></i>'
                    + ' <span class="ios-text">' + label + '</span></label>'
                    + (f.hint ? '<div class="hint-sm">' + esc(f.hint) + '</div>' : '')
                    + '</div>';
            }
            if (f.type === 'select') {
                let o = '';
                (f.options || []).forEach(function (x) {
                    o += '<option value="' + escAttr(String(x.value)) + '">' + esc(String(x.label)) + '</option>';
                });
                inner = '<select id="' + escAttr(id) + '" class="block-input">' + o + '</select>';
            } else if (f.type === 'textarea') {
                inner = '<textarea id="' + escAttr(id) + '" rows="5" class="block-input min-h-80"'
                    + (f.placeholder ? ' placeholder="' + escAttr(f.placeholder) + '"' : '')
                    + '></textarea>';
            } else {
                // text / password / number
                const itype = (f.type === 'password') ? 'password' : (f.type === 'number' ? 'number' : 'text');
                let extra = '';
                if (f.type === 'number') {
                    if (f.min != null) extra += ' min="' + escAttr(String(f.min)) + '"';
                    if (f.max != null) extra += ' max="' + escAttr(String(f.max)) + '"';
                    extra += ' step="any"';
                }
                inner = '<input type="' + itype + '" id="' + escAttr(id) + '" class="block-input"' + extra
                    + (f.placeholder ? ' placeholder="' + escAttr(f.placeholder) + '"' : '')
                    + '>';
            }
            return '<div class="mb-sm">'
                + '<div class="field-label">' + label + '</div>'
                + inner
                + (f.hint ? '<div class="hint-sm">' + esc(f.hint) + '</div>' : '')
                + '</div>';
        }

        async function gmodOpenSettings(uuid) {
            const uid = String(uuid);
            let r;
            try {
                r = await apiGet('/api/gmods/settings?uuid=' + encodeURIComponent(uid));
            } catch (e) {
                gmodStatus('gmods-status', '读取设置失败: ' + e, false);
                return;
            }
            if (!r || !r.ok) {
                gmodStatus('gmods-status', '读取设置失败: ' + ((r && r.error) || '未知错误'), false);
                return;
            }
            const fields = r.fields || [];
            if (!fields.length) {
                botEditOpenHtml('⚙ ' + esc(r.name || uid),
                    '<p class="hint">该子插件未提供设置项（包内缺少 settings.json）。</p>'
                    + '<div class="row mt"><button onclick="botEditClose()" class="btn-ghost">关闭</button></div>');
                return;
            }
            const main = [], adv = [];
            fields.forEach(function (f) {
                if (!f || !f.type) return;
                if (f.type === 'group') {
                    const t = '<div class="modal-section-title">' + esc(f.label || '') + '</div>';
                    (f.advanced ? adv : main).push(t);
                    return;
                }
                (f.advanced ? adv : main).push(gmodFieldHtml(uid, f));
            });

            let body = main.join('');
            if (adv.length) {
                // 高级项默认收起：设置页一打开就是几十行，谁都不想看
                body += '<details class="gmod-adv"><summary>高级选项</summary>' + adv.join('') + '</details>';
            }
            body += '<div class="row mt">'
                + '<button class="ok" onclick="gmodSettingsSave(' + JSON.stringify(uid) + ')">💾 保存</button>'
                + '<button class="text-bad" onclick="botEditClose()">取消</button>'
                + '<span id="gmodset-status" class="hint"></span></div>';

            botEditOpenHtml('⚙ ' + esc(r.name || uid) + ' 设置', body);

            // 渲染完再填值 —— 密码字段的 placeholder 要等主插件给了面罩才知道
            const vals = r.values || {};
            fields.forEach(function (f) {
                if (!f || !f.key) return;
                const node = document.getElementById(gmodSetCtlId(uid, f.key));
                if (!node) return;
                const v = (f.key in vals) ? vals[f.key] : '';
                if (f.type === 'bool') {
                    node.checked = !!v;
                } else if (f.type === 'password') {
                    // 面罩值不填进 value（否则提交时看着像真值），改用 placeholder 提示
                    if (String(v) === GMOD_SET_MASK) node.placeholder = '已设置（留空不修改）';
                    else node.value = (v == null ? '' : String(v));
                } else {
                    node.value = (v == null ? '' : String(v));
                }
            });
        }

        async function gmodSettingsSave(uuid) {
            const uid = String(uuid);
            const st = document.getElementById('gmodset-status');
            function setSt(msg, ok) {
                if (!st) return;
                st.textContent = msg || '';
                st.className = 'hint ' + (ok === true ? 'text-ok' : (ok === false ? 'text-bad' : ''));
            }
            // 从 DOM 反查当前有哪些字段，避免依赖外层的 fields 变量
            const nodes = document.querySelectorAll('[id^="gmodset-' + uid + '-"]');
            const values = {};
            const seenPwd = {};
            for (let i = 0; i < nodes.length; i++) {
                const node = nodes[i];
                const key = node.id.slice(('gmodset-' + uid + '-').length);
                if (node.type === 'checkbox') {
                    values[key] = !!node.checked;
                } else if (node.type === 'password') {
                    seenPwd[key] = true;
                    const raw = String(node.value || '');
                    // 空 = 管理员没动它（可能是"已设置"状态）→ 不提交，保留原值
                    if (raw === '') continue;
                    values[key] = raw;
                } else {
                    values[key] = node.value;
                }
            }
            setSt('保存中…', null);
            try {
                const r = await apiPost('/api/gmods/config/save', { uuid: uid, values: values });
                if (r && r.ok) {
                    setSt('✓ ' + (r.message || '已保存'), true);
                    gmodStatus('gmods-status', '✓ 已保存子插件设置', true);
                } else {
                    setSt('✗ ' + ((r && r.error) || '保存失败'), false);
                }
            } catch (e) {
                setSt('✗ 保存失败: ' + e, false);
            }
        }

        // ---------- 包内文件查看 / 在线编辑 ----------
        async function viewGmod(uuid, file) {
            gmodCurrent = String(uuid);
            const wrap = el('gmod-viewer-wrap');
            if (wrap) wrap.classList.remove('hidden');
            try {
                const url = '/api/gmods/files?uuid=' + encodeURIComponent(gmodCurrent)
                    + (file ? ('&file=' + encodeURIComponent(file)) : '');
                const r = await apiGet(url);
                if (!r || !r.ok) throw new Error((r && r.error) || '读取失败');
                if (el('gmod-viewer-name')) el('gmod-viewer-name').textContent = r.root || '';
                const list = el('gmod-file-list');
                if (list) {
                    list.innerHTML = (r.files || []).map(function (f) {
                        // 包内文件名/子插件 id 都来自压缩包，属不可信输入：
                        // 必须用 jsArg 生成合法 JS 字面量，不能简单拼单引号。
                        const act = (f === r.file) ? ' text-ok' : '';
                        return '<div class="list-item break' + act + '">📄 <a class="btn-link" onclick="viewGmod('
                            + jsArg(gmodCurrent) + ', ' + jsArg(f) + ')">' + esc(f) + '</a></div>';
                    }).join('') || '<p class="empty">包内没有文件</p>';
                }
                if (el('gmod-file-head')) {
                    el('gmod-file-head').textContent = r.file + (r.editable ? '' : '　（非文本文件，仅可查看文件树）');
                }
                const body = el('gmod-file-body');
                if (body) {
                    body.value = r.editable ? r.content : '';
                    body.disabled = !r.editable;
                }
                gmodStatus('gmod-file-status', '', null);
            } catch (e) {
                gmodStatus('gmod-file-status', '读取失败: ' + e, false);
            }
        }

        async function saveGmodFile() {
            if (!gmodCurrent) return;
            const body = el('gmod-file-body');
            const head = el('gmod-file-head');
            if (!body || !head) return;
            const file = String(head.textContent || '').split('　')[0].trim();
            if (!file) { gmodStatus('gmod-file-status', '未选择文件', false); return; }
            try {
                const r = await apiPost('/api/gmods/file/save', { uuid: gmodCurrent, file: file, content: body.value });
                gmodStatus('gmod-file-status', (r.ok ? '✓ ' : '✗ ') + (r.message || r.error || ''), !!r.ok);
            } catch (e) {
                gmodStatus('gmod-file-status', '保存失败: ' + e, false);
            }
        }

        function closeGmodViewer() {
            gmodCurrent = null;
            const wrap = el('gmod-viewer-wrap');
            if (wrap) wrap.classList.add('hidden');
        }

        // ==================== 模组管理器 ====================
        //
        // ★ 为什么改成手动刷新
        // --------------------
        // 「待安装」栏要读上传库 + 实时遍历 mods/inbox/，
        // 「存档启用状态」栏要遍历 resource_packs/、behavior_packs/，
        // 再逐个读 manifest.json，还要读两个 world_*_packs.json。
        // 在装了几百个包的服务器上这是一次不小的磁盘遍历 ——
        // 每次切进这个板块都扫一遍毫无意义（内容几乎不变），
        // 所以改成**只在点刷新时扫**，切板块时只重绘上次的结果。
        //
        // 分工：
        //   scanMods()   —— 真正发请求（唯一的网络入口）
        //   renderMods() —— 只读缓存重绘，不发请求
        //   loadMods()   —— 兼容旧调用点，等价于 scanMods()

        //: 上次扫描的结果快照。null = 从未扫过。
        let modCache = null;

        async function scanMods(opts) {
            const st = el('mods-status');
            const wantWorld = (opts && opts.world) || currentModWorld || '';
            if (st) { st.textContent = '扫描中…'; st.className = 'status text-warn'; }
            try {
                const qs = wantWorld ? ('?world=' + encodeURIComponent(wantWorld)) : '';
                const r = await apiGet('/api/mods' + qs);
                if (!r || !r.ok) throw new Error((r && r.error) || '加载失败');
                modCache = r;
                modCache._at = new Date();
                renderMods();
                if (st) { st.className = 'status text-ok'; }
            } catch (e) {
                if (st) { st.textContent = '✗ ' + e; st.className = 'status text-bad'; }
            }
        }

        //: 兼容旧调用点（上传完 / 开关完 / 删除完都要重新扫一次）
        async function loadMods(opts) {
            return scanMods(opts);
        }

        //: 正在安装中的文件名。列表每次轮询都会**整块重建 innerHTML**，
        //: 重建出来的按钮是全新的 DOM，disabled 自然丢了 —— 于是大包解压的
        //: 十几秒里，用户完全可以在同一行上再点一次「🛠 安装」。两次安装会
        //: 并发写同一个目标目录，而且和「装完即删源文件」互相打架。
        //: 所以「谁在装」要记在函数外的变量上，由 renderMods 重建时补回禁用态。
        var installingMods = {};

        //: 只重绘，不请求。从未扫过时给出明确提示而不是空白。
        function renderMods() {
            const st = el('mods-status');

            if (modCache === null) {
                if (el('mod-uploaded-count')) el('mod-uploaded-count').textContent = '0';
                const lb = el('mod-library-box');
                if (lb) lb.innerHTML = '<p class="empty">尚未扫描。点上方「🔄 刷新」开始扫描。</p>';
                const box = el('mod-pack-groups');
                if (box) box.innerHTML = '<p class="empty">尚未扫描。点上方「🔄 刷新」开始扫描。</p>';
                const metaEl = el('mod-world-meta');
                if (metaEl) metaEl.innerHTML = '<span class="hint">点「🔄 刷新」读取该存档的模组启用状态</span>';
                if (st) { st.textContent = '未扫描'; st.className = 'status'; }
                return;
            }
            const r = modCache;
            {
                const worlds = r.worlds || [];
                const meta = r.meta || {};
                currentModWorld = meta.world || currentModWorld || '';

                if (el('mod-uploaded-count')) el('mod-uploaded-count').textContent = String((r.uploaded || []).length);

                // 手动投放目录提示：大包走不了 HTTP 上传，这是唯一的兜底入口。
                const inboxEl = el('mod-inbox-hint');
                if (inboxEl) {
                    if (r.inbox_dir) {
                        inboxEl.innerHTML = '💡 大文件上传不了？用文件管理器把 .mcaddon / .mcpack '
                            + '拷进下面这个目录，刷新即可见：<br><code class="break">'
                            + esc(r.inbox_dir) + '</code><br>'
                            + '<span class="text-mute">安装成功的包会自动从该目录移走；安装失败会原样保留，方便你修正后重试。</span>';
                    } else {
                        inboxEl.innerHTML = '';
                    }
                }

                // 上传库
                const ups = r.uploaded || [];
                const lb = el('mod-library-box');
                if (lb) {
                    if (!ups.length) { lb.innerHTML = '<p class="empty">尚未上传任何模组</p>'; }
                    else {
                        const opts2 = targetSelectHtml(worlds);
                        lb.innerHTML = ups.map(function (u) {
                            const raw = String(u.file);
                            // data-file 里要放「裸文件名」，不能放 JSON 串：JSON 串会
                            // 带上首尾双引号，后端 os.path.basename 之后名字就变成
                            // 带引号的 `"xxx.mcpack"`，永远找不到文件。
                            const attr = escAttr(raw);
                            const jsArg = escAttr(JSON.stringify(raw));
                            // 磁盘名是 ASCII 化的，界面显示登记时的原始名（可含中文）
                            const label = u.name || u.file;
                            const sub = (label !== u.file) ? ('<br><small class="text-mute">磁盘名 ' + esc(u.file) + '</small>') : '';
                            // inbox 条目要标出来：它不受「上传」概念约束，用户
                            // 手动放进去的东西和他自己传的要能一眼分开。
                            const tag = (u.source === 'inbox')
                                ? '<span class="tag-ok" title="来自手动投放目录（mods/inbox）">📁 手动投放</span> '
                                : '';
                            // 正在装的这一行：重建后也要保持禁用 + 「⏳ 安装中」，
                            // 否则轮询每 800ms 就把按钮"解锁"一次。
                            const busy = !!installingMods[raw];
                            const btnHtml = !u.exists
                                ? '<span class="text-bad">文件缺失</span>'
                                : (busy
                                    ? '<button class="ok" disabled>⏳ 安装中</button>'
                                    : '<button class="ok" onclick="installMod(this)">🛠 安装</button>');
                            return '<div class="mod-row card card-sm" data-file="' + attr + '">'
                                + '<div class="list-item break">'
                                + '<span>' + tag + '📦 ' + esc(label) + '<br><small class="text-accent">' + fmtBytes(u.size) + '　' + esc(u.uploaded || '') + '</small>' + sub + '</span>'
                                + '<button onclick="deleteMod(' + jsArg + ')" title="从待安装列表删除">🗑</button></div>'
                                + '<div class="row mt-xs">'
                                + '<select class="mod-target block-input">' + opts2 + '</select>'
                                + btnHtml
                                + '</div></div>';
                        }).join('');
                    }
                }

                // 存档选择器
                const sel = el('mod-world-select');
                if (sel) {
                    const prev = currentModWorld;
                    sel.innerHTML = worlds.length
                        ? worlds.map(function (w) {
                            const name = w.name || '';
                            return '<option value="' + escAttr(name) + '">' + esc(name)
                                 + (w.valid ? '' : '（缺少 level.dat）') + '</option>';
                          }).join('')
                        : '<option value="">（未找到存档）</option>';
                    if (prev && worlds.some(function (w) { return w.name === prev; })) sel.value = prev;
                }

                // 存档元信息：两个 JSON 的实际路径
                const metaEl = el('mod-world-meta');
                // 教育模式（化学）模组：磁盘上还在，只是不在界面里显示。
                // 必须明确告诉用户「藏了几个」，否则他会以为包丢了。
                const eduN = Number(meta.education_hidden || 0);
                const eduLine = eduN > 0
                    ? ('<br><span class="text-mute" title="' + escAttr((meta.education_names || []).join('、'))
                       + '">🧪 已自动隐藏 ' + eduN + ' 个教育版（化学）模组，文件仍在磁盘上未被删除</span>')
                    : '';
                if (metaEl) {
                    if (!meta.world) {
                        metaEl.innerHTML = '<span class="text-warn">未定位到存档，无法读取启用状态</span>' + eduLine;
                    } else {
                        const src = meta.world_source === 'given' ? '指定存档' : 'server.properties 的 level-name';
                        metaEl.innerHTML = '存档 <b>' + esc(meta.world) + '</b>（来源：' + src + '）　'
                            + '资源包 <b>' + (meta.enabled_resource || []).length + '</b> 个已启用 · '
                            + '行为包 <b>' + (meta.enabled_behavior || []).length + '</b> 个已启用'
                            + eduLine;
                    }
                }

                renderPackGroups(r.resource_packs || [], r.behavior_packs || [], r.unknown_packs || [], meta);

                const res = (r.resource_packs || []).length;
                const beh = (r.behavior_packs || []).length;
                if (st) {
                    st.textContent = '✓ ' + ups.length + ' 个待安装 / 资源包 ' + res + ' · 行为包 ' + beh
                        + (eduN > 0 ? (' · 已隐藏教育版 ' + eduN) : '')
                        + (r._at ? ('　上次扫描 ' + fmtClock(r._at)) : '');
                    st.className = 'status text-ok';
                }
            }
        }

        //: 「上次扫描 14:32:05」——只显示时间，同一天内够用了。
        function fmtClock(d) {
            try {
                const p = function (n) { return (n < 10 ? '0' : '') + n; };
                return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
            } catch (e) {
                return '';
            }
        }

        //: 当前查看的存档（切换存档时不丢失选择）
        let currentModWorld = '';

        //: 渲染「资源包 / 行为包」两个分组。enabled 为 null 表示该包在全局目录，
        //: 本身没有启用状态，只做展示。
        function renderPackGroups(resource, behavior, unknown, meta) {
            const box = el('mod-pack-groups');
            if (!box) return;
            const groups = [
                { key: 'resource', title: '📦 资源包', items: resource },
                { key: 'behavior', title: '⚙️ 行为包', items: behavior },
            ];
            if (unknown.length) groups.push({ key: 'unknown', title: '❓ 类型未知', items: unknown });

            if (!resource.length && !behavior.length && !unknown.length) {
                box.innerHTML = '<p class="empty">该存档目录下没有已安装的资源包或行为包</p>';
                return;
            }

            box.innerHTML = groups.map(function (g) {
                if (!g.items.length) {
                    return '<div class="pack-group"><h4 class="pack-group-title">' + g.title
                         + ' <span class="hint-sm">0</span></h4>'
                         + '<p class="empty hint-sm">无</p></div>';
                }
                const on = g.items.filter(function (p) { return p.enabled === true; }).length;
                const body = g.items.map(function (p) { return packRowHtml(p, g.key); }).join('');
                return '<div class="pack-group"><h4 class="pack-group-title">' + g.title
                     + ' <span class="hint-sm">' + g.items.length + ' 个，已启用 ' + on + '</span></h4>'
                     + body + '</div>';
            }).join('');
        }

        function packRowHtml(p, kind) {
            // 模组包 uuid 来自第三方 manifest.json，属不可信输入。
            // 它会被塞进 onchange 的 JS 串里，所以用 jsArg 而不是 escAttr
            // （escAttr 只防 HTML，防不住反斜杠 + 单引号的 JS 串逃逸）。
            const q = jsArg(p.uuid || '');
            const name = esc(String(p.name || p.uuid || ''));
            const ver = esc(String(p.version || '?'));
            const parent = esc(String(p.parent || ''));
            const scope = p.scope === 'world' ? '存档内' : '全局目录';
            const types = (p.mod_types || []).length
                ? '<span class="hint-sm">模块：' + esc(p.mod_types.join('/')) + '</span>' : '';

            let ctrl;
            if (p.enabled === true || p.enabled === false) {
                // 存档内的包：可切换
                const checked = p.enabled ? ' checked' : '';
                ctrl = '<label class="ios" title="启用 / 停用（需重启服务器生效）">'
                     + '<input type="checkbox"' + checked
                     + ' onchange="toggleModPack(this,' + q + ',' + jsArg(kind) + ')">'
                     + '<i></i></label>';
            } else {
                // 全局目录的包：没有独立的启用状态
                ctrl = '<span class="hint-sm text-mute" title="全局目录中的包由各存档自行引用">—</span>';
            }

            const state = p.enabled === true ? '<span class="tag-ok">已启用</span>'
                        : (p.enabled === false ? '<span class="tag-off">未启用</span>' : '');

            return '<div class="list-item pack-item' + (p.enabled === true ? ' is-on' : '') + '" data-uuid="' + q + '">'
                 + '<div class="break grow">'
                 + '<span>' + name + ' ' + state + ' <span class="text-accent">v' + ver + '</span></span>'
                 + '<div class="hint-sm break">' + parent + ' · ' + scope + '</div>'
                 + '<div class="hint-sm break">' + q + ' ' + types + '</div>'
                 + '</div>'
                 + '<div class="pack-ctrl">' + ctrl + '</div>'
                 + '</div>';
        }

        async function toggleModPack(input, uuid, kind) {
            const enabled = !!(input && input.checked);
            const st = el('mods-status');
            if (input) input.disabled = true;
            if (st) { st.textContent = (enabled ? '启用中…' : '停用中…'); st.className = 'status text-warn'; }
            try {
                const r = await apiPost('/api/mod/toggle', {
                    uuid: uuid, type: kind, world: currentModWorld, enabled: enabled,
                });
                if (r && r.ok) {
                    if (st) { st.textContent = '✓ ' + (r.message || '已更新'); st.className = 'status text-ok'; }
                    // 重新拉取，让两组的分组计数同步刷新
                    await loadMods();
                } else {
                    if (st) { st.textContent = '✗ ' + ((r && (r.error || r.message)) || '操作失败'); st.className = 'status text-bad'; }
                    if (input) input.checked = !enabled;   // 回滚开关
                }
            } catch (e) {
                if (st) { st.textContent = '✗ ' + e; st.className = 'status text-bad'; }
                if (input) input.checked = !enabled;
            } finally {
                if (input) input.disabled = false;
            }
        }

        function targetSelectHtml(worlds) {
            const g = '<option value="global">全局（服务器根目录）</option>';
            const ws = (worlds || []).map(function (w) {
                return '<option value="' + escAttr(w.name) + '">存档：' + esc(w.name) + '</option>';
            }).join('');
            return g + ws;
        }

        async function uploadMod() {
            const fileInput = el('mod-file');
            const st = el('mod-upload-status');
            if (!fileInput || !fileInput.files || !fileInput.files[0]) { if (st) { st.textContent = '请先选择 .mcaddon / .mcpack 文件'; st.className = 'status text-warn'; } return; }
            if (!/\.(mcaddon|mcpack)$/i.test(fileInput.files[0].name)) {
                if (st) { st.textContent = '只支持 .mcaddon / .mcpack'; st.className = 'status text-bad'; }
                return;
            }
            const fd = new FormData();
            fd.append('file', fileInput.files[0]);
            if (st) { st.textContent = '上传中…'; st.className = 'status text-warn'; }
            try {
                const res = await fetch(apiPath('/api/mod/upload'), { method: 'POST', body: fd });
                const r = await res.json();
                if (st) { st.textContent = r.ok ? ('✓ ' + (r.message || '已上传')) : ('✗ ' + (r.error || r.message || '上传失败')); st.className = r.ok ? 'status text-ok' : 'status text-bad'; }
                if (r.ok) { fileInput.value = ''; loadMods(); }
            } catch (e) {
                if (st) { st.textContent = '上传失败: ' + e; st.className = 'status text-bad'; }
            }
        }

        //: 安装是**后台异步**的：接口立刻返回「已提交」，真正解压落盘要几秒。
        //: 所以这里不能用固定的 setTimeout(…, 2500)：
        //:   · 大包（几十 MB）解压慢，2.5 秒大概率还没装完，界面刷新出来
        //:     仍是旧数据，「待安装」里那个包还在 —— 用户以为没装上，又点一次；
        //:   · 小包则白等 2.5 秒。
        //: 改成轮询：反复扫，直到该文件从「待安装」列表消失（=安装成功并清理了
        //: 源文件），或者出现明确失败。
        //:
        //: ★ 为什么不能只靠「文件消失」判断成功：装失败时源文件是**故意保留**的，
        //:   那时文件一直在列表里，光等消失会一路等到超时。所以轮询同时看两件事：
        //:   文件没了 → 成功；超时了但文件还在 → 提示用户看服务端日志。
        async function waitModInstall(file, btn) {
            const st = el('mod-upload-status');
            for (let i = 0; i < 20; i++) {
                await new Promise(function (r) { setTimeout(r, 800); });
                //: 每轮都刷新状态栏：大包（几十 MB）解压要十几秒，期间文案如果
                //: 一直停在「🛠 正在后台安装」，用户分不清是在跑还是卡死了
                //: —— 于是又点一次安装。这里显示「已等待 Ns」让他看得见进度。
                if (st) {
                    st.textContent = '⏳ 安装中…（已等待 ' + Math.round((i + 1) * 0.8) + 's）';
                    st.className = 'status text-warn';
                }
                await loadMods();
                const still = ((modCache && modCache.uploaded) || []).some(function (u) {
                    return String(u.file) === String(file);
                });
                if (!still) return true;          // 源文件已被清理 → 装完了
            }
            return false;
        }

        async function installMod(btn) {
            const row = btn && btn.closest ? btn.closest('.mod-row') : null;
            if (!row) return;
            const file = row.getAttribute('data-file');
            // 双保险：即使 DOM 上的 disabled 因某次重绘丢了，这里也拦住第二次提交。
            if (installingMods[file]) return;
            const sel = row.querySelector('.mod-target');
            const val = sel ? sel.value : 'global';
            const payload = { file: file, target: ('global' === val ? 'global' : 'world'), world: ('global' === val ? '' : val) };
            const st = el('mod-upload-status');

            // 安装期间禁用按钮：同一个包并发点两次会让两次安装互相覆盖目标
            // 目录，也和「装完即删源文件」冲突（后一次可能撞上文件已被删）。
            // ★ 光设 btn.disabled 不够：轮询里的 loadMods() 会把整个列表
            //   innerHTML 重建，按钮被换成新的。所以还要登记到 installingMods，
            //   由 renderMods 在每次重建时把禁用态补回来。
            const btnText = btn ? btn.textContent : '';
            installingMods[file] = true;
            if (btn) { btn.disabled = true; btn.textContent = '⏳ 安装中'; }
            if (st) { st.textContent = '安装中…'; st.className = 'status text-warn'; }
            try {
                const r = await apiPost('/api/mod/install', payload);
                if (!r.ok) {
                    if (st) { st.textContent = '✗ ' + (r.error || r.message || '安装失败'); st.className = 'status text-bad'; }
                    return;
                }
                if (st) { st.textContent = '🛠 ' + (r.message || '开始安装'); st.className = 'status text-warn'; }
                const done = await waitModInstall(file, btn);
                if (st) {
                    st.textContent = done
                        ? '✓ 已安装，安装包已从待安装列表移除'
                        : '⚠ 安装仍在进行或已失败，请查看服务端日志（安装包已保留以便重试）';
                    st.className = done ? 'status text-ok' : 'status text-warn';
                }
            } catch (e) {
                if (st) { st.textContent = '安装失败: ' + e; st.className = 'status text-bad'; }
            } finally {
                delete installingMods[file];
                // 按钮所在的 DOM 会随列表重绘被替换掉，此时 btn 已脱离文档，
                // 改它没有意义也不会报错 —— 直接判断 isConnected。
                if (btn && btn.isConnected) { btn.disabled = false; btn.textContent = btnText; }
                // 收尾必重绘一次：否则「安装中」的禁用态会一直挂在列表上
                // （installingMods 已清空，但 DOM 还是旧的那份）。
                if (modCache) renderMods();
            }
        }

        async function deleteMod(file) {
            // inbox 里的文件是用户自己拷进去的，删掉不影响他本地的原件，
            // 但还是要确认一次：这个目录也是「待处理」队列。
            if (!confirm('确认从服务器删除该文件？\n（仅删除服务器上的副本，你本地的原件不受影响）')) return;
            try {
                const r = await apiPost('/api/mod/delete', { file: String(file) });
                const st = el('mod-upload-status');
                if (st) { st.textContent = r.ok ? '✓ 已删除' : ('✗ ' + (r.error || '删除失败')); st.className = r.ok ? 'status text-ok' : 'status text-bad'; }
                loadMods();
            } catch (e) {
                const st = el('mod-upload-status');
                if (st) { st.textContent = '删除失败: ' + e; st.className = 'status text-bad'; }
            }
        }

        // ================= 机器人管理（多卡片） =================
        function botsStatus(m) { const st = el('bots-op-status'); if (st) st.textContent = m || ''; }
        function botsBadge(txt) { const b = el('bots-status-badge'); if (b) b.textContent = txt || ''; }
        function botsEsc(s) { return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }

        function botsBadgeCard(card) {
            if (!card.enabled) return '已禁用';
            if (!card.configured) return '未配置';
            return card.connected ? '已连接' : (card.running ? '连接中' : '未运行');
        }
        function botsBadgeTone(card) {
            if (!card.enabled) return '';
            if (!card.configured) return 'badge-warn';
            return card.connected ? 'badge-ok' : 'badge-warn';
        }

        async function loadBots() {
            try {
                const r = await apiGet('/api/bots/list');
                if (!r || !r.ok) { botsBadge('加载失败'); return; }
                r.primary = r.primary || null;
                renderBots(r.adapters || [], r.primary);
            } catch (e) { botsBadge('加载失败: ' + e); }
        }

        function renderBots(adapters, primaryId) {
            const box = el('bots-list');
            if (!box) return;
            if (!adapters.length) {
                box.innerHTML = '<p class="empty">暂无机器人，点击「＋ 新建机器人」添加。</p>';
                return;
            }
            const typeLabel = { qqofficial: 'QQ 官方', websocket: 'OneBot v11' };
            box.innerHTML = adapters.map(function (c) {
                const isPrimary = c.id === primaryId;
                const sync = c.sync || {};
                const badge = botsBadgeCard(c);
                const extra =
                    c.type === 'qqofficial'
                        ? 'AppID: ' + botsEsc(c.app_id || '未填')
                        : (Number(c.ws_type) === 0 ? '正向 ' + botsEsc(c.target || '未填') : '反向 :' + (c.listen_port || ''));
                return '' +
                    '<div class="card">' +
                    '  <div class="row">' +
                    '    <b class="break spacer">' + botsEsc(c.name) + (isPrimary ? ' <span class="text-warn">★ 主</span>' : '') + '</b>' +
                    '    <span class="badge">' + (typeLabel[c.type] || c.type) + '</span>' +
                    '    <span class="badge ' + botsBadgeTone(c) + '">● ' + badge + '</span>' +
                    '  </div>' +
                    '  <div class="hint-sm break">' + extra + '</div>' +
                    '  <div class="hint-sm">互通: ' + (sync.qq_to_mc === false ? '群→服✘' : '群→服✓') + ' ' + (sync.mc_to_qq === false ? '服→群✘' : '服→群✓') + '</div>' +
                    '  <div class="row mt">' +
                    '    <label class="ios" data-on="启用" data-off="禁用"><input type="checkbox"' + (c.enabled ? ' checked' : '') + ' onchange="botsToggle(' + jsArg(c.id) + ', this.checked)"><i></i> <span class="ios-text">' + (c.enabled ? '启用' : '禁用') + '</span></label>' +
                    '    <div class="spacer"></div>' +
                    '    <button onclick="botsEdit(' + jsArg(c.id) + ')">✏️ 编辑</button>' +
                    '    <button onclick="botsDelete(' + jsArg(c.id) + ', ' + jsArg(c.name || '') + ')" class="text-bad">🗑 删除</button>' +
                    '  </div>' +
                    '</div>';
            }).join('');
        }

        function botsOpenCreate() {
            const atype = (el('bots-new-type') || {}).value || 'qqofficial';
            botsCreateBox(atype);
        }

        function botsCreateBox(atype) {
            const box = el('bots-create-box');
            if (!box) return;
            box.style.display = 'block';
            if (atype === 'qqofficial') {
                box.innerHTML = '' +
                    '<b class="text-strong">新建 QQ 官方机器人</b>' +
                    '<div class="modal-grid-2 mt-sm">' +
                    '  <div><label class="field-label">名称</label><input id="bots-c-name" placeholder="如 官方群服机器人" class="block-input"></div>' +
                    '  <div><label class="field-label">AppID</label><input id="bots-c-appid" placeholder="机器人 AppID" class="block-input"></div>' +
                    '  <div><label class="field-label">AppSecret</label><input id="bots-c-secret" type="password" placeholder="机器人 AppSecret" class="block-input"></div>' +
                    '  <div><label class="field-label">环境</label><select id="bots-c-env" class="block-input"><option value="formal">正式</option><option value="sandbox">沙箱</option></select></div>' +
                    '</div>' +
                    '<div class="row mt">' +
                    '  <button class="ok" onclick="botsCreateSubmit()">✓ 创建</button>' +
                    '  <button onclick="botsCreateClose()">取消</button>' +
                    '  <span class="hint-sm" id="bots-c-status"></span>' +
                    '</div>';
            } else {
                box.innerHTML = '' +
                    '<b class="text-strong">新建 WebSocket (OneBot v11) 机器人</b>' +
                    '<div class="modal-grid-2 mt-sm">' +
                    '  <div><label class="field-label">名称</label><input id="bots-c-name" placeholder="如 个人号机器人" class="block-input"></div>' +
                    '  <div><label class="field-label">连接方式</label><select id="bots-c-wstype" class="block-input"><option value="0">正向（面板连接 Go-CQHTTP）</option><option value="1">反向（面板监听）</option></select></div>' +
                    '  <div><label class="field-label">正向地址 ws://host:port</label><input id="bots-c-target" placeholder="ws://127.0.0.1:6700" class="block-input"></div>' +
                    '  <div><label class="field-label">反向监听端口</label><input id="bots-c-port" placeholder="3002" type="number" class="block-input"></div>' +
                    '  <div><label class="field-label">AccessToken（可选）</label><input id="bots-c-token" type="password" placeholder="留空不校验" class="block-input"></div>' +
                    '  <div><label class="field-label">机器人QQ号</label><input id="bots-c-qq" type="number" placeholder="机器人 QQ" class="block-input"></div>' +
                    '</div>' +
                    '<div class="hint-sm mt-sm">创建后请在卡片「编辑」中填写群绑定(main_group)与管理员QQ，启用后即可互通。</div>' +
                    '<div class="row mt">' +
                    '  <button class="ok" onclick="botsCreateSubmit()">✓ 创建</button>' +
                    '  <button onclick="botsCreateClose()">取消</button>' +
                    '  <span class="hint-sm" id="bots-c-status"></span>' +
                    '</div>';
            }
        }
        function botsCreateClose() {
            const box = el('bots-create-box');
            if (box) box.style.display = 'none';
        }

        async function botsCreateSubmit() {
            const atype = (el('bots-new-type') || {}).value || 'qqofficial';
            const getName = function (id) { const e = document.getElementById(id); return e ? e.value.trim() : ''; };
            const patch = { type: atype, name: getName('bots-c-name') || undefined };
            const st = el('bots-c-status');
            if (atype === 'qqofficial') {
                Object.assign(patch, { app_id: getName('bots-c-appid'), app_secret: getName('bots-c-secret'), env: getName('bots-c-env') || 'formal' });
            } else {
                Object.assign(patch, {
                    ws_type: Number(getName('bots-c-wstype') || 0),
                    target: getName('bots-c-target'),
                    listen_port: Number(getName('bots-c-port') || 3002),
                    access_token: getName('bots-c-token'),
                    bot_qq: Number(getName('bots-c-qq') || 0)
                });
            }
            try {
                const r = await apiPost('/api/bots/create', patch);
                if (r && r.ok) {
                    if (st) st.textContent = '✓ 已创建';
                    botsCreateClose();
                    botsStatus('已创建机器人');
                    loadBots();
                } else {
                    if (st) st.textContent = '✗ ' + ((r && r.error) || '创建失败');
                }
            } catch (e) { if (st) st.textContent = '✗ ' + e; }
        }

        async function botsToggle(id, enabled) {
            try {
                const r = await apiPost('/api/bots/toggle', { id: id, enabled: enabled });
                botsStatus(r && r.ok ? (enabled ? '已启用' : '已禁用') : ((r && r.error) || '操作失败'));
                loadBots();
            } catch (e) { botsStatus('操作失败: ' + e); }
        }

        async function botsDelete(id, name) {
            if (!confirm('确认删除机器人「' + name + '」？此操作不可撤销。')) return;
            try {
                const r = await apiPost('/api/bots/delete', { id: id });
                botsStatus(r && r.ok ? '已删除' : ((r && r.error) || '删除失败'));
                loadBots();
            } catch (e) { botsStatus('删除失败: ' + e); }
        }

        // ============ 机器人编辑弹窗（单「编辑」按钮完整配置） ============
        var BOT_SYNC_FIELDS = [
            {k:'qq_to_mc', t:'bool', l:'群→服 互通'},
            {k:'mc_to_qq', t:'bool', l:'服→群 互通'},
            {k:'join_to_qq', t:'bool', l:'进服播报到群'},
            {k:'leave_to_qq', t:'bool', l:'退服播报到群'},
            {k:'notify_start', t:'bool', l:'服务器上线播报'},
            {k:'notify_stop', t:'bool', l:'服务器下线播报'},
            {k:'server_start_msg', t:'text', l:'上线提示语'},
            {k:'server_stop_msg', t:'text', l:'下线提示语'},
            // qq_to_mc_format 的占位符语义固定为「昵称 / 群名 / 内容」，
            // 与后端 qqrelay.format_relay 保持一致。旧文案写的是
            // 「%s=群名/名字」，两种可能混在一起说，导致用户配出来的
            // 格式显示成一串 openid（第一个位置填的是群名，取不到群名
            // 时又回退成了 openid）。
            {k:'qq_to_mc_format', t:'text', l:'群→服 格式（%s=昵称, %s=群名, %s=内容）'},
            {k:'mc_to_qq_format', t:'text', l:'服→群 格式（%s=名字, %s=消息）'},
            {k:'join_format', t:'text', l:'进服播报格式（%s=名字）'},
            {k:'leave_format', t:'text', l:'退服播报格式（%s=名字）'},
            {k:'banned_words', t:'wordlist', l:'违禁词（每行一个，命中即不转发到游戏）'},
            {k:'custom_keywords', t:'textarea', l:'特殊提示词（每行：关键词 = 回复；支持 {count} {players} {online} {tps} {world} {cpu} {mem} {disk} {mspt} {uptime} {wl}）'}
        ];

        function bfRow(label, inner) {
            return '<div class="mb-sm">' +
                '<div class="field-label">' + label + '</div>' +
                inner + '</div>';
        }
        function bfInput(label, id, val, ph, type) {
            type = type || 'text';
            val = val == null ? '' : String(val);
            return bfRow(label, '<input type="' + type + '" id="' + id + '" step="any" value="' + escAttr(val) + '"' + (ph ? ' placeholder="' + escAttr(ph) + '"' : '') + ' class="block-input">');
        }
        function bfSelect(label, id, val, opts) {
            var ohtml = '';
            opts.forEach(function (o) {
                ohtml += '<option value="' + escAttr(String(o[0])) + '"' + (String(o[0]) === String(val) ? ' selected' : '') + '>' + esc(String(o[1])) + '</option>';
            });
            return bfRow(label, '<select id="' + id + '" class="block-input">' + ohtml + '</select>');
        }
        function bfBool(label, id, checked) {
            return bfRow(label, '<label class="ios" data-on="启用" data-off="关闭"><input type="checkbox" id="' + id + '"' + (checked ? ' checked' : '') + '><i></i> <span class="ios-text">' + (checked ? '启用' : '关闭') + '</span></label>');
        }
        function bfTextarea(label, id, val, ph) {
            return bfRow(label, '<textarea id="' + id + '" rows="4" class="block-input min-h-80"' + (ph ? ' placeholder="' + escAttr(ph) + '"' : '') + '>' + escAttr(val == null ? '' : val) + '</textarea>');
        }
        function bfSyncHtml(card, pf) {
            var sync = card.sync || {};
            var html = '';
            BOT_SYNC_FIELDS.forEach(function (f) {
                var id = pf + '-' + f.k;
                var raw = f.k in sync ? sync[f.k] : undefined;
                if (f.t === 'bool') {
                    html += bfBool(f.l, id, !!raw);
                } else if (f.t === 'wordlist') {
                    var arr = Array.isArray(raw) ? raw : [];
                    html += bfTextarea(f.l, id, arr.join(String.fromCharCode(10)), '每行一个违禁词');
                } else if (f.t === 'textarea') {
                    var lines = [];
                    if (raw && typeof raw === 'object' && !Array.isArray(raw)) {
                        Object.keys(raw).forEach(function (k) {
                            var v = raw[k];
                            if (k && String(v)) lines.push(k + ' = ' + String(v));
                        });
                    }
                    html += bfTextarea(f.l, id, lines.join(String.fromCharCode(10)), '关键词 = 回复');
                } else {
                    var v = Array.isArray(raw) ? raw.join(', ') : (raw == null ? '' : String(raw));
                    html += bfInput(f.l, id, v);
                }
            });
            return html;
        }
        function bfSyncGather(pf) {
            var out = {};
            BOT_SYNC_FIELDS.forEach(function (f) {
                var e = document.getElementById(pf + '-' + f.k);
                if (!e) return;
                if (f.t === 'bool') out[f.k] = e.checked;
                else if (f.t === 'wordlist') {
                    var arr = [];
                    String(e.value || '').split(String.fromCharCode(10)).forEach(function (l) { l = l.trim(); if (l) arr.push(l); });
                    out[f.k] = arr;
                } else if (f.t === 'textarea') {
                    var obj = {};
                    String(e.value || '').split(String.fromCharCode(10)).forEach(function (l) {
                        l = l.trim(); if (!l) return;
                        var idx = l.indexOf('=');
                        var k = idx >= 0 ? l.slice(0, idx).trim() : l.trim();
                        var v = idx >= 0 ? l.slice(idx + 1).trim() : '';
                        if (k && v) obj[k] = v;
                    });
                    out[f.k] = obj;
                } else out[f.k] = e.value;
            });
            return out;
        }

        function botEditClose() {
            var m = document.getElementById('bot-edit-mask');
            if (m) m.remove();
            // 关闭编辑框即视为离开该卡片：清掉 id 并让在途请求作废，
            // 避免手动触发的群列表刷新又落回主卡。
            __botEditId = '';
            __botEditSeq++;
        }
        function bfMasked(val) {
            return typeof val === 'string' && val.length > 0 && /^\*+$/.test(val);
        }
        function botEditOpenHtml(title, bodyHtml) {
            var mask = document.createElement('div');
            mask.id = 'bot-edit-mask';
            mask.className = 'modal-mask';
            mask.innerHTML = '<div class="modal">' +
                '<div class="row mb">' +
                '<b class="modal-title">' + title + '</b>' +
                '<button onclick="botEditClose()" class="btn-ghost">✕ 关闭</button></div>' +
                bodyHtml + '</div>';
            mask.addEventListener('click', function (e) { if (e.target === mask) botEditClose(); });
            document.body.appendChild(mask);
        }
        function openBotEditModal(card) {
            __botEditId = String(card.id || '');
            __botEditSeq++;                 // 新的一代，作废上一张卡的在途请求
            var pf = 'bf' + (card.type === 'qqofficial' ? 'o' : 'w') + (card.id || 'x');
            var sync = card.sync || {};
            var typeLabel = card.type === 'qqofficial' ? 'QQ 官方' : 'OneBot v11';
            var brand = escAttr(card.name || '机器人') + '（' + typeLabel + '）';

            var conn = '';
            if (card.type === 'qqofficial') {
                var secretMasked = bfMasked(card.app_secret);
                conn = bfInput('AppID', pf + '-app_id', card.app_id) +
                    bfInput('AppSecret', pf + '-app_secret', secretMasked ? '' : (card.app_secret || ''), secretMasked ? '已设置（留空不修改）' : '', 'password') +
                    bfSelect('环境', pf + '-env', card.env || 'formal', [['formal', '正式环境'], ['sandbox', '沙箱环境（无需 IP 白名单）']]) +
                    bfInput('主群 OpenID', pf + '-group_openid', card.group_openid, '官方机器人的主群 OpenID') +
                    bfInput('管理员 OpenID（逗号分隔）', pf + '-group_admins', card.group_admins);
            } else {
                var tokMasked = bfMasked(card.access_token);
                conn = bfSelect('连接方式', pf + '-ws_type', card.ws_type || 0, [['0', '正向（面板连接 Go-CQHTTP）'], ['1', '反向（面板监听）']]) +
                    bfInput('正向地址 ws://host:port', pf + '-target', card.target, 'ws://127.0.0.1:6700') +
                    bfInput('反向监听地址', pf + '-listen_host', card.listen_host || '0.0.0.0') +
                    bfInput('反向监听端口', pf + '-listen_port', card.listen_port || 3002, '', 'number') +
                    bfInput('AccessToken', pf + '-access_token', tokMasked ? '' : (card.access_token || ''), tokMasked ? '已设置（留空不修改）' : '留空不校验', 'password') +
                    bfInput('机器人QQ号', pf + '-bot_qq', card.bot_qq || 0, '', 'number') +
                    bfInput('群绑定 main_group（逗号分隔群号）', pf + '-main_group', card.main_group, '如 10001,10002') +
                    bfInput('管理员QQ（逗号分隔）', pf + '-admin_qq', card.admin_qq, '用于 /指令');
            }

            var groupsBlock = card.type === 'qqofficial'
                ? '<div class="divider-top">' +
                    '<div class="row mb-sm">' +
                    '<b class="status">👥 群管理（官方绑定码）</b>' +
                    '<button onclick="qqNewGroupModal()">＋ 生成绑定码</button>' +
                    '<span id="qq-newgroup-status" class="hint-sm"></span></div>' +
                    '<div id="qq-group-list"><p class="hint">正在读取群列表…</p></div></div>'
                : '';

            var bodyHtml =
                bfBool('启用网关', pf + '-enabled', !!card.enabled) +
                '<div class="modal-section-title">⚙ 连接设置</div>' +
                bfInput('名称', pf + '-name', card.name) +
                conn +
                '<div class="modal-section-title">🔁 群服互通设置</div>' +
                '<div class="modal-grid-2">' + bfSyncHtml(card, pf) + '</div>' +
                groupsBlock +
                '<div class="row mt">' +
                '<button class="ok" onclick="botsEditSave(' + jsArg(card.id) + ', ' + jsArg(card.type) + ', ' + jsArg(pf) + ')">💾 保存</button>' +
                '<button class="text-bad" onclick="botEditClose()">取消</button>' +
                '<span id="' + pf + '-status" class="hint"></span></div>';

            botEditOpenHtml('编辑机器人：' + brand, bodyHtml);
            if (card.type === 'qqofficial') qqFetchGroupList();
        }

        async function botsEdit(id) {
            try {
                const r = await apiGet('/api/bots/list');
                if (!r || !r.ok) { botsStatus('读取失败'); return; }
                const card = (r.adapters || []).filter(function (c) { return c.id === id; })[0];
                if (!card) { botsStatus('未找到该机器人'); return; }
                openBotEditModal(card);
            } catch (e) { botsStatus('读取失败: ' + e); }
        }

        async function botsEditSave(id, type, pf) {
            const st = document.getElementById(pf + '-status');
            const getV = function (i) { const e = document.getElementById(i); return e ? e.value.trim() : ''; };
            const getB = function (i) { const e = document.getElementById(i); return e ? e.checked : false; };
            const patch = {
                name: getV(pf + '-name') || undefined,
                enabled: getB(pf + '-enabled'),
                sync: bfSyncGather(pf)
            };
            if (type === 'qqofficial') {
                Object.assign(patch, {
                    app_id: getV(pf + '-app_id'),
                    env: getV(pf + '-env') || 'formal',
                    group_openid: getV(pf + '-group_openid'),
                    group_admins: getV(pf + '-group_admins')
                });
                const secret = getV(pf + '-app_secret');
                if (secret) patch.app_secret = secret;
            } else {
                Object.assign(patch, {
                    ws_type: Number(getV(pf + '-ws_type') || 0),
                    target: getV(pf + '-target'),
                    listen_host: getV(pf + '-listen_host') || '0.0.0.0',
                    listen_port: Number(getV(pf + '-listen_port') || 3002),
                    bot_qq: Number(getV(pf + '-bot_qq') || 0),
                    main_group: getV(pf + '-main_group'),
                    admin_qq: getV(pf + '-admin_qq')
                });
                // access_token 仅在用户填写新值时覆盖，否则保留原值（避免写回掩码）
                const tok = getV(pf + '-access_token');
                if (tok) patch.access_token = tok;
            }
            if (st) { st.textContent = '保存中…'; st.className = 'status'; }
            try {
                const r = await apiPost('/api/bots/update', { id: id, patch: patch });
                if (r && r.ok) {
                    botsStatus('已保存');
                    botEditClose();
                    loadBots();
                } else {
                    if (st) { st.textContent = '✗ ' + ((r && r.error) || '保存失败'); st.className = 'status text-bad'; }
                }
            } catch (e) {
                if (st) { st.textContent = '✗ ' + e; st.className = 'status text-bad'; }
            }
        }

        async function qqNewGroupModal() {
            const st = document.getElementById('qq-newgroup-status');
            const mySeq = __botEditSeq;
            if (st) { st.textContent = '正在生成验证码…'; st.className = 'status text-warn'; }
            try {
                const r = await apiGet('/api/qqbot/group/new' + qqCardIdQuery());
                if (mySeq !== __botEditSeq) return;   // 已切卡，别把码贴到别人的弹窗上
                if (!r.ok) throw new Error(r.error || '失败');
                if (st) { st.textContent = '已生成验证码：' + r.code + '（60 分钟内有效）'; st.className = 'status text-ok'; }
                qqFetchGroupList();
            } catch (e) {
                if (st) { st.textContent = '生成失败：' + e; st.className = 'status text-bad'; }
            }
        }

        function init() {
            gmSyncThemeBtn();
            gmCloseSidebar();               // 默认收起
            // 先拉取身份与权限，再决定显示哪些板块；避免无权限账户落到空白页
            gmLoadAuth().then(function () {
                if (!gmSelectInitialSection()) return;
                refreshPlayers().then(loadScoreboards).catch(function (e) { console.error(e); });
                loadGamerules();
                loadBans();
                loadWhitelist();
                loadProperties();
                loadWorlds();
                loadScoreboardsFull();
                loadToolsConfig();
                loadGameTools();
                // 模组页首次进来也**不自动扫**：等用户点刷新。
                // 这里只把两栏置成「未扫描」提示，避免页面空白让人以为坏了。
                renderMods();
            });
            // 轮询改为按可见板块启停：只在对应板块显示时才拉数据。
            // 「在线玩家 / 系统状态 / 玩家详情 / 聊天日志」现已合并进 sec-players，
            // 所以这些轮询统一挂在 sec-players 下，一起启停。
            var _p5 = refreshPlayers;  _p5.gmInterval = 5000; gmRegisterPoll('sec-players', _p5);
            var _g3 = updateGauges;    _g3.gmInterval = 3000; gmRegisterPoll('sec-players', _g3);
            var _m2 = pollMessages;    _m2.gmInterval = 2000; gmRegisterPoll('sec-players', _m2);
            var _c2 = pollConsole;     _c2.gmInterval = 2000; gmRegisterPoll('sec-players', _c2);
            var _c2b = pollConsole;    _c2b.gmInterval = 2000; gmRegisterPoll('sec-console', _c2b);
            // 注：原来这里还给已删除的日志中心板块（id 与「日志」同义）注册了匿名轮询，
            // 现随板块合并一并移除（否则会对不存在的板块每 2s 空跑一次请求）。

            // 触屏滑动：从屏幕左缘往右划展开，往左划收起
            var _tx = 0, _ty = 0, _tActive = false;
            document.addEventListener('touchstart', function (ev) {
                if (!ev.touches || ev.touches.length !== 1) { _tActive = false; return; }
                _tx = ev.touches[0].clientX;
                _ty = ev.touches[0].clientY;
                _tActive = _tx < 28 || gmSidebarOpen();
            }, { passive: true });
            document.addEventListener('touchend', function (ev) {
                if (!_tActive || !ev.changedTouches || !ev.changedTouches.length) return;
                _tActive = false;
                var dx = ev.changedTouches[0].clientX - _tx;
                var dy = ev.changedTouches[0].clientY - _ty;
                if (Math.abs(dx) < 50 || Math.abs(dy) > Math.abs(dx)) return;   // 竖向滚动不处理
                if (dx > 0) gmOpenSidebar(); else gmCloseSidebar();
            }, { passive: true });

            // Esc 收起抽屉
            document.addEventListener('keydown', function (ev) {
                if (ev.key === 'Escape' && gmSidebarOpen()) gmCloseSidebar();
            });

            // 跨服：角色切换即时生效，不必等保存或刷新
            var _crossRoleEl = el('cross-role');
            if (_crossRoleEl) _crossRoleEl.addEventListener('change', syncCrossVisibility);

            // 实验板：勾选后立刻显示风险提示，不用等保存
            var _crossExpEl = el('cross-experimental');
            if (_crossExpEl) _crossExpEl.addEventListener('change', syncCrossExpHint);

            gmStartPolls(gmActiveSection);
        }

        // ================================================================
        // QQ 官方扫码登录
        //
        // 流程：获取二维码 → 用内嵌的 QRCode 库把 connect.html 打印成图
        //       → 每 2 秒轮询一次 → created 时刷新卡片列表。
        //
        // ⚠ 后端**不会**返回 AppSecret 明文，只给长度和掩码。所以这里的
        //   任何提示都只显示掩码 —— 别想着"顺手把它显示出来方便用户复制"，
        //   那等于把凭据刻在浏览器历史和截图里。
        // ================================================================

        var qrTimer = null;       // 轮询定时器
        var qrTaskId = '';        // 当前任务 id
        var qrDeadline = 0;       // 本任务的超时时刻（ms）

        function qrOpen() {
            var box = el('qr-box');
            if (!box) return;
            box.classList.remove('hidden');
            box.style.display = 'block';
            qrFillCards();
        }

        function qrClose() {
            var box = el('qr-box');
            if (box) { box.classList.add('hidden'); box.style.display = 'none'; }
            qrStopPoll();
            var imgBox = el('qr-img-box');
            if (imgBox) imgBox.innerHTML = '';
            qrSetStatus('');
        }

        function qrStopPoll() {
            if (qrTimer) { clearInterval(qrTimer); qrTimer = null; }
            qrTaskId = '';
        }

        function qrSetStatus(text, tone) {
            var st = el('qr-status');
            if (!st) return;
            st.textContent = text || '';
            st.className = 'status' + (tone ? ' ' + tone : '');
        }

        async function qrFillCards() {
            var sel = el('qr-card');
            if (!sel) return;
            try {
                var r = await apiGet('/api/bots/list');
                var list = (r && r.ok && r.adapters) ? r.adapters : [];
                var official = list.filter(function (c) { return c.type === 'qqofficial'; });
                // 没有官方卡片时不能直接空着：用户第一次用通常还没建过卡，
                // 这时给一个「自动创建」选项，扫码后会由后端落到第一张卡上。
                var opts = official.map(function (c) {
                    return '<option value="' + escAttr(c.id) + '">' + botsEsc(c.name) +
                           (c.app_id ? '（已有 AppID）' : '') + '</option>';
                });
                if (!opts.length) {
                    opts = ['<option value="">（未找到官方机器人卡片，请先新建）</option>'];
                }
                sel.innerHTML = opts.join('');
            } catch (e) {
                sel.innerHTML = '<option value="">加载失败</option>';
            }
        }

        function qrRender(code) {
            var box = el('qr-img-box');
            if (!box) return;
            if (typeof qrcode !== 'function') {
                // 兜底：qrcode 库没加载出来时至少让用户能手动复制链接
                box.innerHTML = '<p class="hint text-warn">二维码组件未加载，请手动复制链接：<br>' +
                                '<span class="break">' + botsEsc(code) + '</span></p>';
                return;
            }
            try {
                // typeNumber=0 让库自己选版本；纠错级别 M 在「屏幕扫」场景下足够
                var qr = qrcode(0, 'M');
                qr.addData(code);
                qr.make();
                // marginscale: 8 像素/模块，留 4 模块白边（扫码器需要静默区）
                box.innerHTML = '<img alt="QQ 扫码绑定" style="width:220px;height:220px" src="' +
                                qr.createDataURL(8, 4) + '">';
            } catch (e) {
                box.innerHTML = '<p class="hint text-bad">二维码生成失败：' + botsEsc(e) + '</p>';
            }
        }

        async function qrCreate() {
            var btn = el('qr-create-btn');
            var sel = el('qr-card');
            var cardId = sel ? sel.value : '';
            if (btn) { btn.disabled = true; btn.textContent = '获取中…'; }
            qrStopPoll();
            qrSetStatus('正在向 QQ 申请二维码…', 'text-warn');
            try {
                var r = await apiGet('/api/qqbot/qr/create');
                if (!r || !r.ok) {
                    qrSetStatus('获取失败：' + ((r && r.error) || '未知错误'), 'text-bad');
                    return;
                }
                qrTaskId = r.task_id;
                qrRender(r.qrcode || '');
                var interval = Math.max(1, parseInt(r.interval, 10) || 2) * 1000;
                // 后端任务 TTL 600s，前端提前 20s 收手，避免最后几轮必然 404
                qrDeadline = Date.now() + 580 * 1000;
                qrSetStatus('请用手机 QQ 扫码并确认授权…', 'text-warn');
                qrTimer = setInterval(qrPoll, interval);
            } catch (e) {
                qrSetStatus('获取失败：' + e, 'text-bad');
            } finally {
                if (btn) { btn.disabled = false; btn.textContent = '重新获取'; }
            }
        }

        async function qrPoll() {
            if (!qrTaskId) { qrStopPoll(); return; }
            if (Date.now() > qrDeadline) {
                qrStopPoll();
                qrSetStatus('二维码已过期，请重新获取', 'text-bad');
                return;
            }
            var sel = el('qr-card');
            var cardId = sel ? sel.value : '';
            try {
                var r = await apiGet('/api/qqbot/qr/poll?task_id=' +
                                     encodeURIComponent(qrTaskId) +
                                     '&card_id=' + encodeURIComponent(cardId || ''));
                if (!r) return;
                if (r.status === 'pending') { qrSetStatus('等待扫码中…', 'text-warn'); return; }
                if (r.status === 'expired') {
                    qrStopPoll();
                    qrSetStatus('二维码已过期，请重新获取', 'text-bad');
                    return;
                }
                if (r.status === 'error') {
                    qrStopPoll();
                    qrSetStatus('绑定失败：' + (r.error || '未知错误'), 'text-bad');
                    return;
                }
                if (r.status === 'created') {
                    qrStopPoll();
                    var imgBox = el('qr-img-box');
                    if (imgBox) imgBox.innerHTML = '<p class="hint text-ok">✅ 授权成功</p>';
                    // 只显示掩码与长度 —— 明文永远不出现在页面上
                    qrSetStatus('✅ 绑定成功！AppID ' + (r.appid || '') +
                                '，AppSecret ' + (r.secret_mask || '') +
                                '（' + (r.secret_len || 0) + ' 位）已写入卡片。' +
                                (r.message ? ' ' + r.message : ''), 'text-ok');
                    try { await loadBots(); } catch (e) {}
                }
            } catch (e) {
                // 单次网络抖动不算失败，等下一轮；连续失败由超时兜底
                console.error('[扫码] 轮询异常', e);
            }
        }

        // ================================================================
        // 正则触发器
        // ================================================================

        var regexRules = [];      // 本地规则副本（保存时整表提交）
        var regexMeta = {};       // eventTypes / actionTypes 等后端给出的可选值

        async function loadRegex() {
            var box = el('regex-list');
            try {
                var r = await apiGet('/api/qqbot/regex/rules');
                if (!r || !r.ok) {
                    if (box) box.innerHTML = '<p class="hint text-bad">加载失败：' +
                                             botsEsc((r && r.error) || '未知错误') + '</p>';
                    regexBadge('加载失败');
                    return;
                }
                regexRules = r.rules || [];
                regexMeta = r;
                regexBadge('共 ' + regexRules.length + ' 条规则');
                regexRenderConfig(r.config || {});
                regexRenderList();
            } catch (e) {
                if (box) box.innerHTML = '<p class="hint text-bad">加载失败：' + botsEsc(e) + '</p>';
                regexBadge('加载失败');
            }
        }

        function regexBadge(text, tone) {
            var b = el('regex-status-badge');
            if (!b) return;
            b.textContent = text || '';
            b.className = 'status' + (tone ? ' ' + tone : '');
        }

        function regexRenderConfig(cfg) {
            if (el('regex-enable')) el('regex-enable').checked = cfg.enable !== false;
            if (el('regex-only-main')) el('regex-only-main').checked = !!cfg.only_on_main;
            if (el('regex-debug')) el('regex-debug').checked = !!cfg.admin_debug;
            if (el('regex-timeout')) el('regex-timeout').value = (cfg.command_timeout != null ? cfg.command_timeout : 5);
        }

        function regexRenderList() {
            var box = el('regex-list');
            if (!box) return;
            if (!regexRules.length) {
                box.innerHTML = '<p class="empty">暂无规则，点击「＋ 新增规则」添加。</p>';
                return;
            }
            var events = regexMeta.eventTypes || [];
            box.innerHTML = regexRules.map(function (rule, idx) {
                var trigger = rule.triggerType || 'message';
                var trigLabel = trigger === 'message'
                    ? '消息 · <code>' + botsEsc(rule.pattern || '') + '</code>'
                    : '事件 · ' + botsEsc(rule.eventType || '') +
                      (rule.pattern ? ' · <code>' + botsEsc(rule.pattern) + '</code>' : '');
                var acts = (rule.actions || []).map(function (a) {
                    return '<span class="badge">' + botsEsc(a && a.type) + '</span>';
                }).join(' ');
                var conds = (rule.conditions || []).length
                    ? '<div class="hint-sm">条件: ' + (rule.conditions || []).map(function (c) {
                        return botsEsc(c && c.field) + ' ' + botsEsc(c && c.operator) + ' ' + botsEsc(c && c.value);
                      }).join('，') + '</div>'
                    : '';
                return '' +
                    '<div class="card mb">' +
                    '  <div class="row">' +
                    '    <b class="break spacer">' + botsEsc(rule.name || rule.id) + '</b>' +
                    (rule.block ? '<span class="badge text-warn">阻断</span>' : '<span class="badge">继续</span>') +
                    '    <span class="badge ' + (rule.enabled ? 'text-ok' : '') + '">' + (rule.enabled ? '启用' : '禁用') + '</span>' +
                    '  </div>' +
                    '  <div class="hint-sm break">' + trigLabel + '</div>' +
                    conds +
                    '  <div class="row mt-sm">' + acts + '</div>' +
                    '  <div class="row mt">' +
                    '    <button onclick="regexToggle(' + idx + ')">' + (rule.enabled ? '⏸ 禁用' : '▶ 启用') + '</button>' +
                    '    <button onclick="regexEdit(' + idx + ')">✏️ 编辑</button>' +
                    '    <button class="text-bad" onclick="regexDelete(' + idx + ')">🗑 删除</button>' +
                    '  </div>' +
                    '  <div id="regex-edit-' + idx + '" class="hidden mt"></div>' +
                    '</div>';
            }).join('');
        }

        function regexEdit(idx) {
            var box = el('regex-edit-' + idx);
            if (!box) return;
            var rule = regexRules[idx];
            if (!rule) return;
            if (box.style.display === 'block') {
                box.style.display = 'none';
                box.classList.add('hidden');
                return;
            }
            var events = regexMeta.eventTypes || [];
            var actions = (rule.actions || []).length ? rule.actions : [{ type: 'replyText', params: '' }];
            var conditions = (rule.conditions || []).length ? rule.conditions : [{ field: 'userId', operator: '==', value: '' }];
            box.classList.remove('hidden');
            box.style.display = 'block';
            box.innerHTML = '' +
                '<div class="modal-grid-2">' +
                '  <div><label class="field-label">规则名</label><input id="rx-name-' + idx + '" class="block-input" value="' + escAttr(rule.name || '') + '"></div>' +
                '  <div><label class="field-label">触发类型</label><select id="rx-trigger-' + idx + '" class="block-input">' +
                '    <option value="message"' + ((rule.triggerType || 'message') === 'message' ? ' selected' : '') + '>群消息</option>' +
                '    <option value="event"' + (rule.triggerType === 'event' ? ' selected' : '') + '>事件</option>' +
                '  </select></div>' +
                '  <div><label class="field-label">事件类型（触发类型=事件时生效）</label><select id="rx-event-' + idx + '" class="block-input">' +
                '    <option value="">（不指定）</option>' +
                events.map(function (e) {
                    return '<option value="' + escAttr(e) + '"' + (rule.eventType === e ? ' selected' : '') + '>' + botsEsc(e) + '</option>';
                }).join('') +
                '  </select></div>' +
                '  <div><label class="field-label">flags（i / m / s）</label><input id="rx-flags-' + idx + '" class="block-input" value="' + escAttr(rule.flags || '') + '"></div>' +
                '</div>' +
                '<div class="mt-sm"><label class="field-label">正则表达式（消息规则必填；事件规则留空=匹配该事件全部）</label>' +
                '<input id="rx-pattern-' + idx + '" class="block-input" value="' + escAttr(rule.pattern || '') + '"></div>' +
                '<div class="mt-sm"><label class="field-label">条件（每行一条：字段 操作符 值，支持 userId / groupId / nickname，操作符 == != includes matches）</label>' +
                '<textarea id="rx-cond-' + idx + '" class="block-input" rows="2">' +
                conditions.map(function (c) {
                    return botsEsc((c && c.field || '') + ' ' + (c && c.operator || '==') + ' ' + (c && c.value || ''));
                }).join('\n') +
                '</textarea></div>' +
                '<div class="mt-sm"><label class="field-label">动作（每行一条：类型 = 参数，变量 $1 $userId $groupId $at $nickname $result）</label>' +
                '<textarea id="rx-act-' + idx + '" class="block-input" rows="3">' +
                actions.map(function (a) {
                    return botsEsc((a && a.type || '') + ' = ' + (a && a.params || ''));
                }).join('\n') +
                '</textarea></div>' +
                '<label class="ios mt"><input type="checkbox" id="rx-block-' + idx + '"' + (rule.block ? ' checked' : '') + '><i></i> <span class="ios-text">命中后阻断后续处理</span></label>' +
                '<div class="row mt"><button class="ok" onclick="regexApply(' + idx + ')">保存此条</button>' +
                '<button onclick="regexEdit(' + idx + ')">取消</button></div>';
        }

        function regexParseLines(text, sep) {
            var out = [];
            String(text || '').split('\n').forEach(function (line) {
                var s = line.trim();
                if (!s) return;
                var at = s.indexOf(sep);
                if (at < 0) return;
                out.push([s.slice(0, at).trim(), s.slice(at + sep.length).trim()]);
            });
            return out;
        }

        function regexApply(idx) {
            var rule = regexRules[idx];
            if (!rule) return;
            rule.name = (el('rx-name-' + idx) || {}).value || rule.id;
            rule.triggerType = (el('rx-trigger-' + idx) || {}).value || 'message';
            rule.eventType = (el('rx-event-' + idx) || {}).value || '';
            rule.flags = (el('rx-flags-' + idx) || {}).value || '';
            rule.pattern = (el('rx-pattern-' + idx) || {}).value || '';
            rule.block = !!(el('rx-block-' + idx) || {}).checked;

            rule.conditions = regexParseLines((el('rx-cond-' + idx) || {}).value, ' ').map(function (pair) {
                // 「字段 操作符 值」——操作符在中间，值可能含空格，所以只切两刀
                var parts = pair[0].split(/\s+/);
                var field = parts[0] || 'userId';
                var operator = parts[1] || '==';
                var rest = pair[0].slice(field.length).trim();
                var value = rest.slice(operator.length).trim();
                return { field: field, operator: operator, value: value };
            }).filter(function (c) { return c.value || c.operator === '==' ; });

            rule.actions = regexParseLines((el('rx-act-' + idx) || {}).value, '=').map(function (pair) {
                return { type: pair[0], params: pair[1] };
            }).filter(function (a) { return a.type; });

            regexSave();
        }

        function regexAddRule() {
            var n = 1;
            var used = {};
            regexRules.forEach(function (r) { used[r.id] = true; });
            while (used['rule_custom_' + n]) n++;
            regexRules.push({
                id: 'rule_custom_' + n,
                name: '新规则 ' + n,
                enabled: true,
                triggerType: 'message',
                pattern: '',
                flags: '',
                eventType: '',
                conditions: [],
                actions: [{ type: 'replyText', params: '' }],
                block: true
            });
            regexRenderList();
            regexEdit(regexRules.length - 1);
        }

        function regexToggle(idx) {
            var rule = regexRules[idx];
            if (!rule) return;
            rule.enabled = !rule.enabled;
            regexSave();
        }

        function regexDelete(idx) {
            var rule = regexRules[idx];
            if (!rule) return;
            if (!confirm('确定删除规则「' + (rule.name || rule.id) + '」？')) return;
            regexRules.splice(idx, 1);
            regexSave();
        }

        async function regexSave() {
            regexBadge('保存中…', 'text-warn');
            var payload = {
                rules: regexRules,
                config: {
                    enable: !!(el('regex-enable') || {}).checked,
                    only_on_main: !!(el('regex-only-main') || {}).checked,
                    admin_debug: !!(el('regex-debug') || {}).checked,
                    command_timeout: parseFloat((el('regex-timeout') || {}).value) || 5
                }
            };
            try {
                var r = await apiPost('/api/qqbot/regex/save', payload);
                if (!r || !r.ok) {
                    regexBadge('保存失败：' + ((r && r.error) || '未知错误'), 'text-bad');
                    return;
                }
                regexBadge(r.message || '已保存', 'text-ok');
                await loadRegex();
            } catch (e) {
                regexBadge('保存失败：' + e, 'text-bad');
            }
        }

        function saveRegexConfig() { regexSave(); }

        async function regexTest() {
            var out = el('regex-test-result');
            var payload = {
                pattern: (el('regex-test-pattern') || {}).value || '',
                flags: (el('regex-test-flags') || {}).value || '',
                text: (el('regex-test-text') || {}).value || ''
            };
            if (out) out.textContent = '测试中…';
            try {
                var r = await apiPost('/api/qqbot/regex/test', payload);
                if (!r) { if (out) out.textContent = '无响应'; return; }
                if (!r.ok) { if (out) out.textContent = '❌ ' + (r.error || '测试失败'); return; }
                if (!r.matched) { if (out) out.textContent = '未命中'; return; }
                if (out) {
                    out.textContent = '✅ 命中\n完整匹配: ' + (r.groups[0] || '') +
                        (r.groups.length > 1
                            ? '\n捕获组:\n' + r.groups.slice(1).map(function (g, i) {
                                return '  $' + (i + 1) + ' = ' + g;
                              }).join('\n')
                            : '');
                }
            } catch (e) {
                if (out) out.textContent = '❌ ' + e;
            }
        }

        window.addEventListener('DOMContentLoaded', init);
    </script>