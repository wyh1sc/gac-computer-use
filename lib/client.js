window.__ModuleLoader__.load({
  id: 'gac-pip-view',
  factory(require) {
    const React = require('react');

    const S = {
      root: { display: 'flex', flexDirection: 'column', height: '100%', background: '#15181d' },
      bar: {
        display: 'flex', alignItems: 'center', gap: 8, padding: '6px 10px',
        background: '#1c2027', borderBottom: '1px solid #2a2f38', flex: 'none',
        fontSize: 12, color: '#e6e9ef',
      },
      dot: { width: 7, height: 7, borderRadius: '50%', background: '#6b7280', flex: 'none' },
      dotOn: { background: '#22c55e' },
      title: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
      handoff: {
        display: 'flex', alignItems: 'center', gap: 10, padding: '9px 12px',
        background: '#3b2a12', borderBottom: '1px solid #7c5a1e',
        color: '#fde68a', fontSize: 12, flex: 'none',
      },
      why: { flex: 1, lineHeight: 1.5 },
      doneBtn: {
        background: '#f59e0b', color: '#1a1206', border: 0, borderRadius: 6,
        padding: '5px 13px', fontWeight: 700, cursor: 'pointer', fontSize: 12, flex: 'none',
      },
      stage: {
        flex: 1, minHeight: 0, overflow: 'auto', position: 'relative',
        display: 'flex', alignItems: 'flex-start', justifyContent: 'center', padding: 8,
      },
      img: { width: '100%', height: 'auto', display: 'block', borderRadius: 4 },
      imgClickable: { cursor: 'crosshair' },
      hint: {
        position: 'absolute', inset: 0, display: 'flex', flexDirection: 'column',
        alignItems: 'center', justifyContent: 'center', gap: 6, textAlign: 'center',
        color: '#8b93a1', fontSize: 12, lineHeight: 1.7, padding: 16, background: '#15181d',
      },
      hint2: { color: '#6b7280', fontSize: 11 },
      typebar: {
        display: 'flex', gap: 8, padding: '8px 10px', background: '#1c2027',
        borderTop: '1px solid #2a2f38', flex: 'none',
      },
      input: {
        flex: 1, background: '#0f1216', border: '1px solid #2a2f38', color: '#e6e9ef',
        borderRadius: 6, padding: '6px 9px', font: 'inherit', fontSize: 12, outline: 'none',
      },
      sendBtn: {
        background: '#2563eb', color: '#fff', border: 0, borderRadius: 6,
        padding: '6px 13px', fontWeight: 600, cursor: 'pointer', fontSize: 12, flex: 'none',
      },
      paused: {
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        height: '100%', color: '#6b7280', fontSize: 12, background: '#15181d',
      },
    };

    function postJson(path, body) {
      return fetch(path, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(body || {}),
      }).then(function (r) { return r.json(); });
    }

    function PipView(props) {
      const h = React.createElement;
      const visible = !!(props && props.visible);

      const [nonce, setNonce] = React.useState(0);
      const [info, setInfo] = React.useState(null);
      const [failed, setFailed] = React.useState(false);
      const [sending, setSending] = React.useState(false);
      const [text, setText] = React.useState('');

      React.useEffect(function () {
        if (!visible) return undefined;
        let alive = true;
        function poll() {
          fetch('/gac-pip/status', { cache: 'no-store' })
            .then(function (r) { return r.json(); })
            .then(function (s) { if (alive) { setInfo(s); setFailed(false); } })
            .catch(function () { if (alive) setFailed(true); });
        }
        poll();
        const timer = setInterval(poll, 1200);
        return function () { alive = false; clearInterval(timer); };
      }, [visible]);

      // The MJPEG <img> goes dead when the pip server restarts; re-key it.
      React.useEffect(function () {
        if (!visible || !failed) return undefined;
        const timer = setTimeout(function () { setNonce(function (n) { return n + 1; }); }, 2000);
        return function () { clearTimeout(timer); };
      }, [visible, failed, info]);

      if (!visible) {
        return h('div', { style: S.paused }, 'pip 桌面 · 切到此标签时恢复画面');
      }

      const handoff = (info && info.handoff) || {};
      const takeover = !!handoff.active;
      const seq = handoff.seq || 0;
      const live = !!(info && info.active && info.window);

      function finishHandoff() {
        setSending(true);
        postJson('/gac-pip/handoff/done', { seq: seq })
          .catch(function () { /* the poll will resync */ })
          .then(function () { setSending(false); });
      }

      function onImageClick(event) {
        if (!takeover) return;
        const rect = event.currentTarget.getBoundingClientRect();
        if (!rect.width || !rect.height) return;
        postJson('/gac-pip/input', {
          type: 'click',
          x: (event.clientX - rect.left) / rect.width,
          y: (event.clientY - rect.top) / rect.height,
        }).catch(function () { /* ignore a lost click */ });
      }

      function sendText() {
        const value = text;
        if (!value) return;
        setText('');
        postJson('/gac-pip/input', { type: 'type', text: value }).catch(function () { /* ignore */ });
      }

      const title = live ? info.window
        : failed ? '无法连接本地服务'
        : info && info.active ? 'pip 桌面空闲（未启动应用）'
        : takeover ? '等待你接管'
        : '连接中…';

      return h('div', { style: S.root },
        h('div', { style: S.bar },
          h('span', { style: Object.assign({}, S.dot, (live || takeover) ? S.dotOn : null) }),
          h('span', { style: S.title }, title)
        ),

        takeover ? h('div', { style: S.handoff },
          h('span', { style: S.why }, handoff.reason || '需要你接管操作'),
          h('button', {
            style: S.doneBtn,
            disabled: sending,
            onClick: finishHandoff,
          }, sending ? '提交中…' : '我已完成')
        ) : null,

        h('div', { style: S.stage },
          h('img', {
            key: 'pip-' + nonce,
            src: '/gac-pip/stream?n=' + nonce,
            style: Object.assign({}, S.img, takeover ? S.imgClickable : null),
            alt: 'pip desktop',
            onClick: onImageClick,
            onError: function () { setFailed(true); },
            onLoad: function () { setFailed(false); },
          }),
          live ? null : h('div', { style: S.hint },
            h('div', null, failed ? '本地视口服务未响应'
              : (info && info.active ? 'pip 桌面上还没有可见窗口' : 'pip 平面尚未启用')),
            h('div', { style: S.hint2 }, '先执行 switch_desktop("pip")，再 launch_app("Command Prompt")')
          )
        ),

        takeover ? h('div', { style: S.typebar },
          h('input', {
            style: S.input,
            value: text,
            placeholder: '点画面可直接操作；也可以在这里输入文字，回车发送',
            onChange: function (e) { setText(e.target.value); },
            onKeyDown: function (e) {
              if (e.key === 'Enter') { e.preventDefault(); sendText(); }
            },
          }),
          h('button', { style: S.sendBtn, onClick: sendText }, '发送')
        ) : null
      );
    }

    return {
      inject: ['betterSidebar'],
      apply(ctx) {
        const register = function () {
          return ctx.betterSidebar.registerTab({
            id: 'gac-pip-view',
            title: 'pip 桌面',
            description: '隔离虚拟桌面的实时画面；需要你接管时可点画面操作并确认完成',
            order: 30,
            single: true,
            icon: React.createElement('span', { style: { fontSize: 13, lineHeight: 1 } }, '\uD83D\uDDA5'),
            component: PipView,
          });
        };
        // ctx.effect ties disposal to the plugin fiber; older hosts lack it.
        if (typeof ctx.effect === 'function') {
          ctx.effect(register);
        } else {
          register();
        }
      },
    };
  },
});
