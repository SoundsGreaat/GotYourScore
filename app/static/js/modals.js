/* Native app dialogs own focus, modality, and cancel requests. Keep them in
   the top layer until the exit fade finishes, then emit the native close. */
(() => {
    const selector = 'dialog.gys-modal';
    const closing = new WeakMap();
    const reopening = new WeakSet();
    const openDialogs = new Set();

    function active() {
        let top = null;
        for (const dialog of openDialogs) {
            if (dialog.isConnected && dialog.open) top = dialog;
            else openDialogs.delete(dialog);
        }
        return top;
    }

    function onNativeClose(event) {
        const dialog = event.currentTarget;
        if (!dialog.open) openDialogs.delete(dialog);
    }

    function onCancel(event) {
        // Workflow guards are registered when their partial is initialized,
        // before the first open. A veto must also stop explicit Close/Cancel.
        if (event.defaultPrevented) return;
        event.preventDefault();
        close(event.currentTarget);
    }

    function open(dialog, onOpen) {
        if (!dialog?.matches?.(selector) || !dialog.isConnected || dialog.open || reopening.has(dialog)
            || typeof dialog.showModal !== 'function') return false;
        const pendingClose = closing.get(dialog);
        if (pendingClose) {
            // Native close restores focus before the close event. Save an
            // activation in that gap, then let the old close handlers finish
            // before showing the dialog again (if it is still mounted).
            reopening.add(dialog);
            pendingClose.then(() => {
                reopening.delete(dialog);
                open(dialog, onOpen);
            });
            return true;
        }
        active(); // Discard dialogs detached by a partial swap.
        dialog.returnValue = '';
        dialog.addEventListener('cancel', onCancel);
        dialog.addEventListener('close', onNativeClose);
        dialog.showModal();
        openDialogs.add(dialog);
        onOpen?.();
        return true;
    }

    function close(dialog, returnValue = '') {
        if (closing.has(dialog)) return closing.get(dialog);
        if (!dialog?.matches?.(selector) || !dialog.open) return Promise.resolve();

        let resolveClose;
        const closed = new Promise((resolve) => { resolveClose = resolve; });
        closing.set(dialog, closed);
        let timer;

        function settle() {
            if (closing.get(dialog) !== closed) return;
            dialog.removeEventListener('transitionend', onTransitionEnd);
            dialog.removeEventListener('close', onClose);
            dialog.removeAttribute('data-closing');
            clearTimeout(timer);
            closing.delete(dialog);
            openDialogs.delete(dialog);
            resolveClose();
        }

        function finish() {
            dialog.removeEventListener('transitionend', onTransitionEnd);
            clearTimeout(timer);
            // A partial swap can detach the dialog or remove its open
            // state; in that case no native close event may be delivered.
            const detached = !dialog.isConnected;
            if (!dialog.open || detached) {
                if (dialog.open) dialog.close(returnValue);
                settle();
                return;
            }
            dialog.close(returnValue);
        }

        function onTransitionEnd(event) {
            // The dialog and its backdrop use the same duration. Ignore
            // descendant transitions (e.g. buttons and notifications).
            if (event.target === dialog && !event.pseudoElement && event.propertyName === 'opacity') finish();
        }

        function onClose() {
            if (!dialog.open) settle();
        }
        dialog.addEventListener('close', onClose);

        const style = getComputedStyle(dialog);
        // The first transition is the surface's opacity fade.
        const duration = style.transitionDuration.split(',')[0].trim();
        const milliseconds = parseFloat(duration) * (duration.endsWith('ms') ? 1 : 1000);
        if (dialog.isConnected && milliseconds > 0) {
            dialog.addEventListener('transitionend', onTransitionEnd);
            dialog.setAttribute('data-closing', '');
            // Safari/Firefox can skip transitionend when a fade is interrupted.
            // Never leave a dialog modal indefinitely waiting for that event.
            timer = setTimeout(finish, Math.min(milliseconds + 50, 500));
        } else {
            finish();
        }
        return closed;
    }

    // While exiting, a focused control can still receive Enter/Space. Do
    // not allow another action or a second submission before native close.
    for (const type of ['click', 'submit']) {
        document.addEventListener(type, (event) => {
            if (!event.target.closest?.(`${selector}[data-closing]`)) return;
            event.preventDefault();
            event.stopImmediatePropagation();
        }, true);
    }

    document.addEventListener('click', (event) => {
        if (event.defaultPrevented) return;
        const button = event.target.closest?.('[data-gys-modal-close]');
        const dialog = button?.closest(`${selector}[open]`);
        if (!dialog || button.disabled) return;
        if (typeof dialog.requestClose === 'function') {
            dialog.requestClose();
        } else {
            dialog.dispatchEvent(new Event('cancel', { cancelable: true }));
        }
    });

    async function confirm({ message }) {
        const dialog = document.getElementById('gys-confirm-dialog');
        if (!dialog || dialog.open || closing.has(dialog)) return false;
        dialog.querySelector('#gys-confirm-message').textContent = message;
        if (!open(dialog)) return false;
        const confirmButton = dialog.querySelector('[data-gys-confirm]');
        const confirmDelete = () => close(dialog, 'delete');
        confirmButton.addEventListener('click', confirmDelete);
        await new Promise((resolve) => dialog.addEventListener('close', resolve, { once: true }));
        confirmButton.removeEventListener('click', confirmDelete);
        return dialog.returnValue === 'delete';
    }

    window.gysModal = { open, close, confirm, active };
})();
