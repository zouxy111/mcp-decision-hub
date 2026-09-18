/* ============================================================================
   mcp-decision-hub — Console 交互层
   纯原生，无框架、无外部 CDN。所有增强均为「渐进式」：
   JS 未加载时表单仍可正常提交，只是失去确认弹层与即时反馈。
   ---------------------------------------------------------------------------
   1. data-confirm   → 危险操作确认弹层（替代原生 confirm / htmx hx-confirm）
   2. data-copy      → 一次性凭证复制 + toast
   3. 表单提交       → 防重复提交 + 提交态反馈
   4. data-autorefresh → 生成中的页面保守轮询（有交互即永久停止）
   ========================================================================= */
(() => {
  'use strict';

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  /* ------------------------------------------------------------- toast */
  let toastEl = null;
  let toastTimer = 0;

  function toast(message) {
    if (!toastEl) {
      toastEl = document.createElement('div');
      toastEl.className = 'toast';
      toastEl.setAttribute('role', 'status');
      toastEl.setAttribute('aria-live', 'polite');
      document.body.appendChild(toastEl);
    }
    toastEl.textContent = message;
    // 强制回流以便连续调用时动画能重放
    void toastEl.offsetWidth;
    toastEl.dataset.show = '1';
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toastEl.dataset.show = '0'; }, 2200);
  }

  /* ------------------------------------------------------ confirm dialog */
  const WARN_ICON =
    '<svg class="confirm-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/>' +
    '</svg>';

  let dialog = null;
  let pending = null;   // 被拦截的提交动作

  function buildDialog() {
    dialog = document.createElement('dialog');
    dialog.className = 'confirm';
    dialog.innerHTML =
      '<div class="confirm-body">' +
        WARN_ICON +
        '<div><div class="confirm-title"></div><div class="confirm-text"></div></div>' +
      '</div>' +
      '<div class="confirm-actions">' +
        '<button type="button" class="btn btn--ghost" data-act="cancel">取消</button>' +
        '<button type="button" class="btn btn--primary" data-act="ok">确认</button>' +
      '</div>';
    document.body.appendChild(dialog);

    dialog.addEventListener('click', (e) => {
      const act = e.target.closest('[data-act]');
      if (!act) {
        // 点击 backdrop 关闭
        if (e.target === dialog) close(false);
        return;
      }
      close(act.dataset.act === 'ok');
    });
    // Esc 关闭
    dialog.addEventListener('cancel', (e) => { e.preventDefault(); close(false); });
  }

  function openConfirm(message, { danger = false, title = '请确认操作' } = {}) {
    if (!dialog) buildDialog();
    $('.confirm-title', dialog).textContent = title;
    $('.confirm-text', dialog).textContent = message;
    $('[data-act="ok"]', dialog).className =
      'btn ' + (danger ? 'btn--danger' : 'btn--primary');
    $('.confirm-icon', dialog).classList.toggle('confirm-icon--danger', danger);
    dialog.showModal();
    $('[data-act="ok"]', dialog).focus();
  }

  function close(confirmed) {
    const action = pending;
    pending = null;
    if (dialog && dialog.open) dialog.close();
    if (confirmed && typeof action === 'function') action();
  }

  /* 拦截带 data-confirm 的表单提交与链接点击 */
  document.addEventListener('submit', (e) => {
    const form = e.target;
    if (!(form instanceof HTMLFormElement)) return;

    const msg = form.dataset.confirm;
    if (!msg || form.dataset.confirmed === '1') return;

    e.preventDefault();
    pending = () => { form.dataset.confirmed = '1'; form.submit(); };
    openConfirm(msg, {
      danger: form.dataset.confirmDanger === '1',
      title: form.dataset.confirmTitle || '请确认操作',
    });
  }, true);

  document.addEventListener('click', (e) => {
    const link = e.target.closest('a[data-confirm]');
    if (!link) return;
    e.preventDefault();
    const href = link.href;
    pending = () => { window.location.href = href; };
    openConfirm(link.dataset.confirm, { danger: true, title: '请确认操作' });
  });

  /* --------------------------------------------------------------- copy */
  document.addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-copy]');
    if (!btn) return;
    e.preventDefault();
    const value = btn.dataset.copy;
    const label = btn.dataset.copyLabel || '已复制到剪贴板';

    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(value);
      } else {
        // http 环境下的回退路径（非 secure context 无 clipboard API）
        const ta = document.createElement('textarea');
        ta.value = value;
        ta.setAttribute('readonly', '');
        ta.style.cssText = 'position:fixed;left:-9999px;opacity:0';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        ta.remove();
      }
      toast(label);
    } catch {
      toast('复制失败，请手动选择文本复制');
    }
  });

  /* --------------------------------------------- 防重复提交 + 提交态反馈 */
  document.addEventListener('submit', (e) => {
    const form = e.target;
    if (!(form instanceof HTMLFormElement)) return;
    // 被确认弹层拦截的那次提交不处理；用户确认后的真实提交才禁用按钮
    if (form.dataset.confirm && form.dataset.confirmed !== '1') return;

    const btn = form.querySelector('button[type="submit"]');
    if (!btn) return;

    form.dataset.submitting = '1';
    btn.disabled = true;
    btn.setAttribute('aria-busy', 'true');
    const original = btn.textContent;
    if (btn.dataset.busyLabel !== undefined) {
      btn.textContent = btn.dataset.busyLabel || '处理中';
    }
    // 提交被浏览器取消（校验失败等）时恢复可点，避免按钮卡死
    window.setTimeout(() => {
      if (document.contains(form)) {
        btn.disabled = false;
        btn.removeAttribute('aria-busy');
        btn.textContent = original;
        form.dataset.submitting = '';
      }
    }, 12000);
  });

  /* ------------------------------------------------------- 保守自动刷新 */
  const refreshRoot = $('[data-autorefresh]');
  if (refreshRoot) {
    const interval = Math.max(3000, Number(refreshRoot.dataset.autorefreshInterval) || 8000);
    const maxTicks = Math.max(1, Number(refreshRoot.dataset.autorefreshMax) || 30);
    let ticks = 0;
    let stopped = false;

    // 任何一次用户交互都视为「正在填表」，永久停止轮询，避免打断输入
    const stop = () => { stopped = true; };
    ['input', 'change', 'keydown'].forEach((ev) =>
      document.addEventListener(ev, stop, { once: true, capture: true }));

    const timer = window.setInterval(() => {
      if (stopped || ++ticks > maxTicks) {
        window.clearInterval(timer);
        return;
      }
      if (document.hidden) return;
      if (refreshRoot.dataset.autorefreshDirty === '1') {
        window.clearInterval(timer);
        return;
      }
      window.location.reload();
    }, interval);
  }
})();
