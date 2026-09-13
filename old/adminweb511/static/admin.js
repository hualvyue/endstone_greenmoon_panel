<script>
        let currentPlayers = [];
        async function loadDiag() {
            const box = el('diag-box'), st = el('diag-status');
            try {
                const r = await apiGet('/api/diag');
                if (!r || r.error) { if (st) st.textContent = '读取失败: ' + (r ? r.error : '无响应'); return; }
                const d = r.diag || {};
                const lines = [
                    'tick间隔(tick_gap_ms) : ' + (d.tick_gap_ms != null ? d.tick_gap_ms : '?') + '  ms（正常约 50ms）',
                    'tick偏大次数(slow)   : ' + (d.tick_slow_count != null ? d.tick_slow_count : '?'),
                    '排队/执行中任务      : ' + (d.current ? String(d.current) : '(无)') + '   |  排队计数=' + (d.pending != null ? d.pending : '?'),
                    '备份正在执行         : ' + (d.backup_running ? '是' : '否'),
                    'UP: ' + (d.cloud_cfg ? ('cloud_enabled=' + d.cloud_cfg.enabled + ' check_join=' + d.cloud_cfg.check_on_join + ' token=' + (d.cloud_cfg.token_set ? '已设置' : '未设置')) : ''),
                    '诊断开关: ' + (d.enabled ? '开启' : '关闭') + '   |  详细日志: ' + (d.detailed ? '开启' : '关闭')
                ];
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
        console.log('[GreenMoon] 前端版本 v2026.08.26-B 已加载');

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
            var b = el('theme-toggle');
            if (b) b.textContent = gmCurrentTheme() === 'light' ? '☀️ 浅色' : '🌙 深色';
        }
        function gmToggleTheme() {
            var next = gmCurrentTheme() === 'light' ? 'dark' : 'light';
            document.documentElement.setAttribute('data-theme', next);
            try { localStorage.setItem('gm-theme', next); } catch (e) {}
            gmSyncThemeBtn();
        }

        /* 统一维护 .ios 开关文案，避免「状态变了文字没变」 */
        document.addEventListener('change', function (ev) {
            var t = ev.target;
            if (!t || t.type !== 'checkbox' || !t.closest) return;
            var lab = t.closest('.ios');
            if (!lab) return;
            var txt = lab.querySelector('.ios-text');
            if (txt) txt.textContent = t.checked ? (lab.getAttribute('data-on') || '启用') : (lab.getAttribute('data-off') || '禁用');
        });

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
            gmTimers[sectionId] = fns.map(function (fn) {
                try { fn(); } catch (e) {}
                return setInterval(function () {
                    // 页面不可见（切到别的标签页）时跳过，省电省请求
                    if (document.hidden) return;
                    try { fn(); } catch (e) {}
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
                else if (id === 'sec-bots') loadBots();
                else if (id === 'sec-mods') loadMods();
                else if (id === 'sec-gmods') loadGmods();
                else if (id === 'sec-files') loadFiles();
                else if (id === 'sec-gametools') loadGameTools();
                else if (id === 'sec-console') pollConsole();
            } catch (e) { /* 忽略板块加载错误 */ }
        }

        function switchLogTab(which) {
            const chatTab = el('tab-chat'), conTab = el('tab-console');
            const chatPane = el('chat-pane'), conPane = el('console-pane');
            if (which === 'chat') {
                chatTab.classList.add('active'); conTab.classList.remove('active');
                chatPane.style.display = ''; conPane.style.display = 'none';
            } else {
                conTab.classList.add('active'); chatTab.classList.remove('active');
                conPane.style.display = ''; chatPane.style.display = 'none';
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
            }).catch(function () {});
        }

        // 兼容反向代理子路径（如 https://host/server/admin）：脚本内所有请求统一走 apiPath 计算前缀
        var API_BASE = (function () {
            var p = location.pathname;
            if (p.charAt(p.length - 1) !== '/') p = p.slice(0, p.lastIndexOf('/') + 1);
            return p;
        })();
        function apiPath(u) { return API_BASE + u.replace(/^\//, ''); }

        async function apiGet(url) {
            const res = await fetch(apiPath(url));
            return await res.json();
        }

        async function apiPost(url, body) {
            const res = await fetch(apiPath(url), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body)
            });
            return await res.json();
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

        function filesJump(p) { filesCwd = p || ''; loadFiles(); }
        function filesGoUp() { filesCwd = fsParent(filesCwd); loadFiles(); }

        function fsSep() { return document.createTextNode(' · '); }
        function fsLink(text, name, isDir, act) {
            const a = document.createElement('a');
            a.href = 'javascript:void(0)';
            a.textContent = text;
            a.onclick = function () {
                if (act === 'download') filesDown(name);
                else if (act === 'rename') filesRename(name);
                else if (act === 'copy') filesCopy(name);
                else if (act === 'move') filesMove(name);
                else if (act === 'delete') filesDelete(name);
                else filesOpen(name, isDir);
            };
            if (act === 'delete') a.className = 'status text-bad';
            return a;
        }

        function renderFiles(list) {
            let bc = el('files-breadcrumb');
            let crumbs = '<a href="javascript:void(0)" onclick="filesJump(\'\')">根目录</a>';
            let acc = '';
            (filesCwd ? filesCwd.split('/') : []).forEach(function (seg) {
                acc = acc ? acc + '/' + seg : seg;
                crumbs += ' / <a href="javascript:void(0)" onclick="filesJump(\'' + fsEscJs(acc) + '\')">' + esc(seg) + '</a>';
            });
            bc.innerHTML = crumbs;
            const tb = el('files-list');
            tb.innerHTML = '';
            list.forEach(function (it) {
                const tr = document.createElement('tr');
                const c1 = document.createElement('td');
                c1.innerHTML = (it.is_dir ? '📁 ' : '📄 ') + esc(it.name);
                if (it.is_dir) c1.classList.add('pointer');
                if (it.is_dir) c1.onclick = function () { filesOpen(it.name, true); };
                const c2 = document.createElement('td'); c2.textContent = it.is_dir ? '目录' : '文件';
                const c3 = document.createElement('td'); c3.textContent = it.is_dir ? '—' : fsSize(it.size);
                const c4 = document.createElement('td'); c4.textContent = fsTime(it.mtime);
                const c5 = document.createElement('td');
                c5.appendChild(fsLink(it.is_dir ? '进入' : '查看', it.name, it.is_dir));
                if (!it.is_dir) { c5.appendChild(fsSep()); c5.appendChild(fsLink('下载', it.name, false, 'download')); }
                c5.appendChild(fsSep()); c5.appendChild(fsLink('重命名', it.name, false, 'rename'));
                c5.appendChild(fsSep()); c5.appendChild(fsLink('复制', it.name, false, 'copy'));
                c5.appendChild(fsSep()); c5.appendChild(fsLink('移动', it.name, false, 'move'));
                c5.appendChild(fsSep()); c5.appendChild(fsLink('删除', it.name, false, 'delete'));
                tr.appendChild(c1); tr.appendChild(c2); tr.appendChild(c3); tr.appendChild(c4); tr.appendChild(c5);
                tb.appendChild(tr);
            });
            if (!list.length) tb.innerHTML = '<tr><td colspan="5" class="empty">（空目录）</td></tr>';
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
                tbody.innerHTML = '';
                currentPlayers.forEach(p => {
                    const tr = document.createElement('tr');
                    const c1 = document.createElement('td'); c1.innerHTML = '<span class="online">●</span> ' + String(p.name);
                    const c2 = document.createElement('td'); c2.textContent = Math.round(p.x) + ', ' + Math.round(p.y) + ', ' + Math.round(p.z);
                    const c3 = document.createElement('td'); c3.textContent = p.dimension;
                    const c4 = document.createElement('td'); c4.textContent = p.health + '/' + p.max_health;
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
                if (el('detail-player-select').value) loadPlayerDetail();
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
            const name = el('detail-player-select').value;
            const area = el('detail-area');
            if (!name) { area.innerHTML = ''; return; }
            try {
                const res = await fetch(apiPath('/api/player/' + encodeURIComponent(name)));
                if (res.status === 404) { area.innerHTML = '<p>玩家不在线</p>'; return; }
                const p = await res.json();
                const tags = (p.tags || []).map(t => '<span class="tag-badge">' + t + '</span>').join(' ') || '（无）';
                const scores = Object.entries(p.scores || {}).map(function (e) { return '<span class="score-item"><b>' + e[0] + '</b>: ' + e[1] + '</span>'; }).join('') || '（无）';
                area.innerHTML = '<div class="player-detail">' +
                    '<p><b>玩家:</b> ' + esc(p.name) + '</p>' +
                    '<p><b>XUID:</b> <span class="uuid-text">' + esc(p.xuid || '（无）') + '</span></p>' +
                    '<p><b>生命值:</b> ' + p.health + '/' + p.max_health + '</p>' +
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

        function qqSafe(s) { return String(s == null ? '' : s).replace(/[^0-9a-zA-Z_-]/g, '_'); }

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

        let lastMsgId = 0;
        let msgFirstLoad = true;

        function appendMessages(msgs) {
            const panel = el('message-panel');
            if (!panel) return;
            if (msgFirstLoad) { panel.innerHTML = ''; msgFirstLoad = false; }
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
            const as = el('msg-autoscroll');
            if (as && as.checked) { panel.scrollTop = panel.scrollHeight; }
        }

        async function pollMessages() {
            try {
                const data = await apiGet('/api/messages?after=' + lastMsgId);
                const msgs = data.messages || [];
                if (msgs.length) {
                    appendMessages(msgs);
                    lastMsgId = data.last_id || lastMsgId;
                } else if (msgFirstLoad) {
                    const panel = el('message-panel');
                    if (panel) panel.innerHTML = '<div class="msg msg-empty empty">暂无消息</div>';
                    msgFirstLoad = false;
                }
            } catch (e) { console.error('pollMessages', e); }
        }

        function clearMessagesUI() {
            const panel = el('message-panel');
            if (panel) panel.innerHTML = '';
        }

        function loadPermInfo() {
            const name = el('perm-player').value;
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
            const name = el('perm-player').value;
            const node = el('perm-node').value.trim();
            if (!name || !node) { showStatus('请选择玩家并填写权限节点', false); return; }
            const value = el('perm-value').value === 'true';
            const r = await apiPost('/api/permission', { player_name: name, permission: node, action: 'set', value: value });
            showStatus(r.message || r.error, !!r.success);
            if (r.success) { el('perm-node').value = ''; await refreshPlayers(); loadPermInfo(); }
        }

        async function unsetPermission() {
            const name = el('perm-player').value;
            const node = el('perm-node').value.trim();
            if (!name || !node) { showStatus('请选择玩家并填写权限节点', false); return; }
            const r = await apiPost('/api/permission', { player_name: name, permission: node, action: 'unset' });
            showStatus(r.message || r.error, !!r.success);
            if (r.success) { el('perm-node').value = ''; await refreshPlayers(); loadPermInfo(); }
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
        let lastConsoleId = 0;
        let consoleFirstLoad = true;

        function appendConsoleLogs(logs) {
            const panel = el('console-panel');
            if (!panel) return;
            if (consoleFirstLoad) { panel.innerHTML = ''; consoleFirstLoad = false; }
            else { const empty = panel.querySelector('.clog-empty'); if (empty) empty.remove(); }
            logs.forEach(function (l) {
                const div = document.createElement('div');
                div.className = 'clog clog-' + (l.level || 'INFO');
                div.innerHTML = '<span class="clog-time">[' + esc(l.time) + ' ' + esc(l.level) + ']</span>' + esc(l.message);
                panel.appendChild(div);
            });
            const as = el('console-autoscroll');
            if (as && as.checked) { panel.scrollTop = panel.scrollHeight; }
        }

        async function pollConsole() {
            try {
                const data = await apiGet('/api/console?after=' + lastConsoleId);
                const logs = data.logs || [];
                if (logs.length) {
                    appendConsoleLogs(logs);
                    lastConsoleId = data.last_id || lastConsoleId;
                } else if (consoleFirstLoad) {
                    const panel = el('console-panel');
                    if (panel) panel.innerHTML = '<div class="clog clog-empty empty">暂无控制台日志</div>';
                    consoleFirstLoad = false;
                }
            } catch (e) { console.error('pollConsole', e); }
        }

        function clearConsoleUI() {
            const panel = el('console-panel');
            if (panel) panel.innerHTML = '';
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
        function renderScopeChips() {
            const box = el('tmp-scopes');
            if (!box) return;
            const all = window._ALL_SCOPES || [];
            box.innerHTML = '可访问板块：' + all.map(function (s) {
                return '<label class="scope-chip"><input type="checkbox" data-scp="' + s + '"> ' + esc(s) + '</label>';
            }).join('') + '（<span class="empty">勾选后临时账户可访问对应板块，不勾选即无权限</span>）';
        }

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
                    if (tm) tm.checked = r.test_mode !== false;
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
                const btn = a.expired ? '<span class="empty">—</span>' : '<button onclick="revokeTemp(\'' + escAttr(String(a.id)) + '\')">撤销</button>';
                return '<tr><td>' + esc(a.username) + '</td><td>' + scopesTxt + '</td><td>' + expTxt + '</td><td>' + badge + '</td><td>' + btn + '</td></tr>';
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
                el('cross-self-url').value = c.self_url || '';
                el('cross-sync').value = c.sync_seconds || 8;
                const it = c.interop || {};
                el('cross-it-chat').checked = it.chat !== false;
                el('cross-it-player').checked = it.player_data !== false;
                el('cross-it-wl').checked = it.whitelist !== false;
                el('cross-it-ban').checked = it.ban !== false;
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
        }

        function renderCrossStatus(d) {
            const box = el('cross-status-box');
            if (!box) return;
            const peers = d.peers || {};
            let peersHtml = '（无已注册从服）';
            const keys = Object.keys(peers);
            if (keys.length) {
                peersHtml = '<ul class="peers-list">';
                keys.forEach(function (k) {
                    peersHtml += '<li>' + esc(k) + ' → ' + esc(peers[k]) + '</li>';
                });
                peersHtml += '</ul>';
            }
            const card = function (label, val, color) {
                var tone = 'is-ok';
                if (color === '#e94560') tone = 'is-bad';
                else if (color === '#888') tone = 'is-idle';
                return '<div class="stat ' + tone + '"><div class="k">' + esc(label) + '</div><div class="v">' + val + '</div></div>';
            };
            let cards = [
                card('本服角色', d.role === 'master' ? '主服务器' : (d.role === 'slave' ? '副服务器' : '未参与'), d.role === 'none' ? '#888' : '#4ecca3'),
                card('本服名称', d.server_name || '未设置'),
                card('已互通玩家数', String(d.players || 0)),
                card('最近心跳', d.last_ping ? (new Date(d.last_ping * 1000).toLocaleTimeString()) : '—', d.last_ping ? '#4ecca3' : '#e94560')
            ];
            if (d.last_error) cards.push(card('最近错误', d.last_error, '#e94560'));
            box.innerHTML = '<div class="row">' + cards.join('') + '</div>' +
                '<div class="hint mt-sm">已注册从服：</div><div class="hint">' + peersHtml + '</div>';
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
                    self_url: el('cross-self-url').value,
                    sync_seconds: parseFloat(el('cross-sync').value) || 8,
                    interop: {
                        chat: el('cross-it-chat').checked,
                        player_data: el('cross-it-player').checked,
                        whitelist: el('cross-it-wl').checked,
                        ban: el('cross-it-ban').checked
                    }
                };
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





        async function qqFetchGroupList() {
            try {
                const r = await apiGet('/api/qqbot/status');
                if (!r.ok) throw new Error(r.error || '失败');
                renderQQGroups(r.groups || [], r.group_openid || '', r.pending_codes || []);
            } catch (e) {
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
                    '<span class="hint-sm">在要加入的 QQ 群内发送该验证码即可绑定</span>' +
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
                const r = await apiPost('/api/qqbot/group/rename', { openid: openid, name: name });
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
            const st = cfg.backup_state || {};
            const box = el('backup-state-box');
            if (!box) return;
            const badge = el('backup-state-badge');
            const lines = [];
            if (st.running) {
                lines.push('<span class="text-warn">⏳ 备份正在进行中…</span>');
                if (badge) { badge.textContent = '备份中'; badge.className = 'status text-warn'; }
            } else {
                if (badge) { badge.textContent = cfg.backup.enabled ? '自动备份已开启' : '自动备份已关闭'; badge.style.color = cfg.backup.enabled ? '#4ef09a' : '#888'; }
            }
            if (st.last_result && st.last_result[0] === true) {
                lines.push('上次备份：<span class="text-ok">' + String(st.last_result[1]) + '</span>');
            } else if (st.last_result && st.last_result[0] === false) {
                lines.push('上次备份：<span class="text-bad">' + String(st.last_result[1]) + '</span>');
            } else {
                lines.push('上次备份：<span class="empty">尚未执行</span>');
            }
            const next = Number(st.next_run) || 0;
            if (next > 0 && cfg.backup.enabled) {
                const d = new Date(next * 1000);
                const pad = function (x) { return (x < 10 ? '0' : '') + x; };
                lines.push('下次自动备份：' + d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes()));
            }
            box.innerHTML = lines.join('<br>');
        }
        function renderBackupList(cfg) {
            const box = el('backup-list-box');
            if (!box) return;
            const list = cfg.backups || [];
            if (!list.length) { box.innerHTML = '<p class="empty">暂无备份文件</p>'; return; }
            const rows = list.map(function (b) {
                const q = JSON.stringify(String(b.name)).replace(/"/g, "&quot;");
                return '<div class="list-item">'
                    + '<span class="break">📄 ' + String(b.name) + '<br><small class="text-accent">' + fmtBytes(b.size) + '　' + String(b.mtime) + '</small></span>'
                    + '<span class="row">'
                    + '<button class="ok" onclick="restoreBackup(' + q + ')">恢复</button>'
                    + '<button onclick="deleteBackup(' + q + ')">删除</button></span></div>';
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
                    kick_message: el('cloud-kick-message').value.trim()
                }
            };
            try {
                const r = await apiPost('/api/tools/config/save', payload);
                setToolsStatus('backup-save-status', r.message || '', !!r.ok);
                setToolsStatus('cloud-save-status', r.ok ? '已保存' : (r.error || '保存失败'), !!r.ok);
                loadToolsConfig();
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
            try {
                const r = await apiPost('/api/cloud/test', {});
                setToolsStatus('cloud-save-status', (r.message || '测试完成') + (r.ok ? '（连接正常）' : '（连接失败）'), !!r.ok);
            } catch (e) { setToolsStatus('cloud-save-status', '测试失败: ' + e, false); }
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

                // 接管状态概览
                const st = el('gmod-page-state');
                if (st) {
                    const on = mods.filter(function (m) { return m.enabled; });
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
                            return '<div class="list-item">'
                                + '<span class="break"><b>' + esc(m.name || m.uuid) + '</b> '
                                + '<span class="text-accent">v' + esc(m.version || '?') + '</span> '
                                + gmodTypeLabel(m.type) + ' ' + state
                                + '<div class="hint-sm break">' + esc(m.description || '（无描述）') + '</div>'
                                + '<div class="hint-sm break">uuid: ' + esc(m.uuid) + '　依赖: ' + dep + miss + '</div>'
                                + '</span>'
                                + toggle
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
                        const q = JSON.stringify(String(f)).replace(/"/g, '&quot;');
                        const act = (f === r.file) ? ' text-ok' : '';
                        return '<div class="list-item break' + act + '">📄 <a class="btn-link" onclick="viewGmod('
                            + JSON.stringify(gmodCurrent) + ', ' + q + ')">' + esc(f) + '</a></div>';
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
        async function loadMods() {
            const st = el('mods-status');
            try {
                const r = await apiGet('/api/mods');
                if (!r || !r.ok) throw new Error((r && r.error) || '加载失败');
                const worlds = r.worlds || [];
                if (el('mod-uploaded-count')) el('mod-uploaded-count').textContent = String((r.uploaded || []).length);
                if (el('mod-installed-count')) el('mod-installed-count').textContent = String((r.installed || []).length);
                const ups = r.uploaded || [];
                const lb = el('mod-library-box');
                if (lb) {
                    if (!ups.length) { lb.innerHTML = '<p class="empty">尚未上传任何模组</p>'; }
                    else {
                        const opts = targetSelectHtml(worlds);
                        lb.innerHTML = ups.map(function (u) {
                            const q = JSON.stringify(String(u.file)).replace(/"/g, '&quot;');
                            return '<div class="mod-row" data-file="' + q + '" class="card card-sm">'
                                + '<div class="list-item break">'
                                + '<span>📦 ' + esc(u.file) + '<br><small class="text-accent">' + fmtBytes(u.size) + '　' + esc(u.uploaded || '') + '</small></span>'
                                + '<button onclick="deleteMod(' + q + ')" title="从上传库删除">🗑</button></div>'
                                + '<div class="row mt-xs">'
                                + '<select class="mod-target block-input">' + opts + '</select>'
                                + (u.exists ? '<button class="ok" onclick="installMod(this)">🛠 安装</button>' : '<span class="text-bad">文件缺失</span>')
                                + '</div></div>';
                        }).join('');
                    }
                }
                const inst = r.installed || [];
                const ib = el('mod-installed-box');
                if (ib) {
                    if (!inst.length) { ib.innerHTML = '<p class="empty">当前未安装任何资源/行为包</p>'; }
                    else {
                        ib.innerHTML = inst.map(function (p) {
                            return '<div class="list-item">'
                                + esc(p.name) + ' <span class="text-accent">v' + esc(p.version) + '</span>'
                                + '<div class="hint-sm break">' + esc(p.parent) + ' · ' + esc(p.uuid) + '</div></div>';
                        }).join('');
                    }
                }
                if (st) { st.textContent = '✓ ' + ups.length + ' 个已上传 / ' + inst.length + ' 个已安装'; st.className = 'status text-ok'; }
            } catch (e) {
                if (st) { st.textContent = '✗ ' + e; st.className = 'status text-bad'; }
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

        async function installMod(btn) {
            const row = btn && btn.closest ? btn.closest('.mod-row') : null;
            if (!row) return;
            const file = row.getAttribute('data-file');
            const sel = row.querySelector('.mod-target');
            const val = sel ? sel.value : 'global';
            const payload = { file: file, target: ('global' === val ? 'global' : 'world'), world: ('global' === val ? '' : val) };
            const st = el('mod-upload-status');
            if (st) { st.textContent = '安装中…'; st.className = 'status text-warn'; }
            try {
                const r = await apiPost('/api/mod/install', payload);
                if (st) { st.textContent = r.ok ? ('🛠 ' + (r.message || '开始安装')) : ('✗ ' + (r.error || r.message)); st.className = r.ok ? 'status text-ok' : 'status text-bad'; }
                setTimeout(loadMods, 2500);
            } catch (e) {
                if (st) { st.textContent = '安装失败: ' + e; st.className = 'status text-bad'; }
            }
        }

        async function deleteMod(file) {
            if (!confirm('确认从服务器删除该上传文件？')) return;
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
                    '    <label class="ios" data-on="启用" data-off="禁用"><input type="checkbox"' + (c.enabled ? ' checked' : '') + ' onchange="botsToggle(\'' + c.id + '\', this.checked)"><i></i> <span class="ios-text">' + (c.enabled ? '启用' : '禁用') + '</span></label>' +
                    '    <div class="spacer"></div>' +
                    '    <button onclick="botsEdit(\'' + c.id + '\')">✏️ 编辑</button>' +
                    '    <button onclick="botsDelete(\'' + c.id + '\', \'' + botsEsc(c.name || '').replace(/'/g, "\\'") + '\')" class="text-bad">🗑 删除</button>' +
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
            {k:'qq_to_mc_format', t:'text', l:'群→服 格式（%s=群名/名字, %s=内容）'},
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
                '<button class="ok" onclick="botsEditSave(\'' + card.id + '\', \'' + card.type + '\', \'' + pf + '\')">💾 保存</button>' +
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
            if (st) { st.textContent = '正在生成验证码…'; st.className = 'status text-warn'; }
            try {
                const r = await apiGet('/api/qqbot/group/new');
                if (!r.ok) throw new Error(r.error || '失败');
                if (st) { st.textContent = '已生成验证码：' + r.code + '（60 分钟内有效）'; st.className = 'status text-ok'; }
                qqFetchGroupList();
            } catch (e) {
                if (st) { st.textContent = '生成失败：' + e; st.className = 'status text-bad'; }
            }
        }

        function init() {
            gmSyncThemeBtn();
            showSection('sec-players', document.querySelector('.sidebar a'));
            refreshPlayers().then(loadScoreboards).catch(function (e) { console.error(e); });
            loadGamerules();
            loadBans();
            loadWhitelist();
            loadProperties();
            loadWorlds();
            loadScoreboardsFull();
            loadToolsConfig();
            loadGameTools();
            loadMods();
            // 轮询改为按可见板块启停：只在对应板块显示时才拉数据
            var _p5 = refreshPlayers;  _p5.gmInterval = 5000; gmRegisterPoll('sec-players', _p5);
            var _g3 = updateGauges;    _g3.gmInterval = 3000; gmRegisterPoll('sec-players', _g3);
            var _m2 = pollMessages;    _m2.gmInterval = 2000; gmRegisterPoll('sec-logs', _m2);
            var _c2 = pollConsole;     _c2.gmInterval = 2000; gmRegisterPoll('sec-logs', _c2);
            var _c2b = pollConsole;    _c2b.gmInterval = 2000; gmRegisterPoll('sec-console', _c2b);
            gmStartPolls(gmActiveSection);
        }
        window.addEventListener('DOMContentLoaded', init);
    </script>