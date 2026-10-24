<script>
        function el(id) { return document.getElementById(id); }

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
                    'UP: ' + (d.cloud_cfg ? ('cloud_enabled=' + d.cloud_cfg.enabled + ' check_join=' + d.cloud_cfg.check_on_join + ' token=' + (d.cloud_cfg.token_set ? '已设置' : '未设置')) : '')
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
        // 兼容反向代理子路径（如 https://host/server/whitelist）：动态计算 API 前缀
        var API_BASE = (function () {
            var p = location.pathname;
            if (p.charAt(p.length - 1) !== '/') p = p.slice(0, p.lastIndexOf('/') + 1);
            return p;
        })();
        function apiPath(u) { return API_BASE + u.replace(/^\//, ''); }
        async function submitApply() {
            const name = el('apply-name').value.trim();
            const st = el('status');
            if (!name) { st.className = 'err'; st.textContent = '请填写游戏名'; return; }
            try {
                const res = await fetch(apiPath('/api/whitelist/apply'), {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ name: name, xuid: el('apply-xuid').value.trim(), note: el('apply-note').value.trim() })
                });
                const r = await res.json();
                st.className = r.success ? 'ok' : 'err';
                st.textContent = r.message || r.error;
            } catch (e) { st.className = 'err'; st.textContent = '请求失败: ' + e; }
        }
        async function checkStatus() {
            const name = el('check-name').value.trim();
            const box = el('check-result');
            if (!name) { box.textContent = '请输入游戏名'; return; }
            try {
                const res = await fetch(apiPath('/api/whitelist/status?name=' + encodeURIComponent(name)));
                const r = await res.json();
                const a = r.application;
                const S = { pending: '⏳ 待审核', approved: '✅ 已通过', rejected: '❌ 已拒绝' };
                box.textContent = a ? (S[a.status] || a.status) + (a.note ? '（' + a.note + '）' : '') : '未找到申请记录';
            } catch (e) { box.textContent = '查询失败: ' + e; }
        }
    </script>