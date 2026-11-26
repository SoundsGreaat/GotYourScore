(() => {
    const dialog = document.getElementById('bug-report-dialog');
    const trigger = document.getElementById('report-bug-button');
    if (!trigger || typeof dialog?.showModal !== 'function') return;

    const draft = document.getElementById('bug-report-draft');
    const preview = document.getElementById('bug-report-preview');
    const success = document.getElementById('bug-report-success');
    const back = document.getElementById('bug-report-back');
    const issueTitle = document.getElementById('bug-report-issue-title');
    const issueBody = document.getElementById('bug-report-issue-body');
    const issueLink = document.getElementById('bug-report-issue-link');
    const fields = Array.from(draft.querySelectorAll('input, textarea'));
    const validation = document.getElementById('bug-report-validation');
    const validationReason = document.getElementById('bug-report-validation-reason');
    const validationList = validation.querySelector('ul');
    const draftSubmit = draft.querySelector('[type="submit"]');
    const publish = preview.querySelector('[type="submit"]');
    let step = draft;
    let pending = false;
    let token = '';
    let successSeen = false;

    const fieldHints = {
        title: 'Name the affected feature and symptom. Example: “Archived filter shows active items.”',
        steps: 'Include the starting screen and the actions in order. Example: “Open Demo → select Archived.”',
        actual: 'Say what you actually saw. Example: “The list still showed active items.”',
        expected: 'Say what the same steps should produce. Example: “Only archived items should be listed.”',
        environment: 'Check the browser and device description.',
        context: 'Check the additional context for anything irrelevant or confidential.',
    };
    const uncertainCopy = 'Publication could not be confirmed. Check the GitHub repository for your issue before trying again.';
    const transportErrorCopy = 'The connection was interrupted before publication could be confirmed. Check the GitHub repository or retry the same confirmed preview. This preview cannot be published twice.';

    function setAvailability(button, disabled) {
        if (disabled) button.setAttribute('aria-disabled', 'true');
        else button.removeAttribute('aria-disabled');
    }

    function setPending(value) {
        pending = value;
        const drafting = step === draft;
        step.querySelector('[role="status"]').textContent = !value ? '' : drafting
            ? 'Checking your report for confidential information…'
            : 'Publishing to GitHub…';
        setAvailability(draftSubmit, value);
        setAvailability(back, value);
        setAvailability(publish, value || !token);
        for (const field of fields) field.readOnly = value;
    }

    function show(next) {
        step = next;
        for (const panel of [draft, preview, success]) panel.hidden = panel !== next;
        if (dialog.open) {
            if (step === success) successSeen = true;
            dialog.scrollTop = 0;
            step.querySelector('h2').focus({ preventScroll: true });
        }
    }

    function setRetryPublication(retry) {
        publish.textContent = retry ? 'Retry publication' : 'Publish to GitHub';
        publish.setAttribute('aria-describedby', retry
            ? 'bug-report-publication-error'
            : 'bug-report-publish-warning');
    }

    function blockPublication() {
        token = '';
        setAvailability(publish, true);
        setRetryPublication(false);
    }

    function clearPreview() {
        blockPublication();
        issueTitle.textContent = '';
        issueBody.textContent = '';
        preview.querySelector('[role="alert"]').textContent = '';
    }

    function fieldError(field, text) {
        const error = document.getElementById(`${field.id}-error`);
        const ids = field.getAttribute('aria-describedby').split(/\s+/).filter((id) => id !== error.id);
        if (text) field.setAttribute('aria-invalid', 'true');
        else field.removeAttribute('aria-invalid');
        error.textContent = text;
        error.hidden = !text;
        if (text) ids.push(error.id);
        field.setAttribute('aria-describedby', ids.join(' '));
    }

    function showFieldErrors(names, reason, messageFor) {
        const invalid = fields.filter((field) => names.includes(field.name));
        validationReason.textContent = reason;
        validationList.replaceChildren();
        for (const field of invalid) {
            const text = messageFor(field);
            fieldError(field, text);
            const item = document.createElement('li');
            const link = document.createElement('a');
            link.className = 'link';
            link.href = `#${field.id}`;
            link.textContent = field.labels[0].firstChild.textContent.trim();
            link.addEventListener('click', (event) => {
                event.preventDefault(); // The dashboard uses the URL fragment for navigation.
                field.focus();
            });
            item.append(link);
            validationList.append(item);
        }
        validation.hidden = false;
        if (dialog.open) validation.focus();
    }

    async function post(form, values) {
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 70_000);
        try {
            const response = await fetch(form.getAttribute('action'), {
                method: 'POST',
                credentials: 'same-origin',
                redirect: 'error',
                headers: { 'Content-Type': 'application/json', 'X-Bug-Report': '1' },
                body: JSON.stringify(values),
                signal: controller.signal,
            });
            const data = await response.json().catch((error) => {
                if (!(error instanceof SyntaxError)) throw error;
                return null;
            });
            return { ok: response.ok, status: response.status, retryAfter: response.headers.get('Retry-After'), data };
        } finally {
            clearTimeout(timeout);
        }
    }

    function fallback(status, operation) {
        if (status === 401) return 'Your session has expired. Sign in again before continuing.';
        return `${operation}: ${status >= 500 ? 'Server error' : 'Request error'}.`;
    }

    function rateLimitCopy(retryAfter, publishing) {
        // The endpoints use delta-seconds, not an HTTP date. Never display the raw header.
        const seconds = /^[1-9]\d{0,4}$/.test(retryAfter) ? Number(retryAfter) : 0;
        const minutes = Math.ceil(seconds / 60);
        let wait = 'Wait a little while';
        if (seconds > 0 && seconds < 60) {
            wait = `Wait ${seconds} ${seconds === 1 ? 'second' : 'seconds'}`;
        } else if (seconds >= 60 && seconds <= 86400) {
            wait = `Wait about ${minutes} ${minutes === 1 ? 'minute' : 'minutes'}`;
        }
        return publishing
            ? `Too many publication requests. ${wait} before trying again. If this preview expires, go back to create a new one.`
            : `Too many preview requests. ${wait} before trying again.`;
    }

    function previewError({ status, data, retryAfter }) {
        if (status === 429 || data?.code === 'rate_limited') return rateLimitCopy(retryAfter, false);
        const names = Array.isArray(data?.fields) ? data.fields : [];
        switch (data?.code) {
            case 'invalid_report':
                showFieldErrors(names, 'Check the required fields and length limits, then try again.',
                    (field) => `Enter a valid value of no more than ${field.maxLength} characters.`);
                return '';
            case 'needs_details':
                showFieldErrors(names,
                    'There isn’t enough detail to reproduce the bug. Replace placeholders like “Test title” with specific, non-confidential details, then try again. Nothing was published.',
                    (field) => fieldHints[field.name]);
                return '';
            case 'privacy_unavailable':
                return 'The AI privacy check is unavailable. Nothing was published. Try previewing later.';
            case 'preview_unavailable':
                return 'The preview is temporarily unavailable. Try again later.';
            case 'reporting_disabled':
                return 'Bug reporting is currently unavailable.';
            default:
                return fallback(status, 'Unable to preview report');
        }
    }

    function publishError({ status, data, retryAfter }) {
        if (status === 429 || data?.code === 'rate_limited') return rateLimitCopy(retryAfter, true);
        switch (data?.code) {
            case 'expired_preview':
                return 'This preview has expired. Go back to create a new preview.';
            case 'github_unavailable':
                return 'GitHub is unavailable. Check the GitHub repository for your issue before trying again.';
            case 'publication_uncertain':
                return uncertainCopy;
            case 'reporting_disabled':
                return 'Bug reporting is currently unavailable.';
            default:
                if (status >= 500) return `Unable to publish report: Server error. ${uncertainCopy}`;
                return fallback(status, 'Unable to publish report');
        }
    }

    function githubIssueUrl(data) {
        if (!Number.isSafeInteger(data?.number) || data.number < 1 || typeof data.url !== 'string') return '';
        let url;
        try {
            url = new URL(data.url);
        } catch {
            return '';
        }
        const issuePath = /^\/[A-Za-z0-9][A-Za-z0-9-]{0,38}\/[A-Za-z0-9_.-]{1,100}\/issues\/[1-9]\d*$/;
        if (url.origin !== 'https://github.com' || url.username || url.password || url.search || url.hash
            || !issuePath.test(url.pathname) || !url.pathname.endsWith(`/issues/${data.number}`)) return '';
        return url.href;
    }

    for (const field of fields) {
        field.addEventListener('input', () => {
            fieldError(field, '');
            const link = validationList.querySelector(`a[href="#${field.id}"]`);
            if (!link) return;
            link.parentElement.remove();
            if (!validationList.hasChildNodes()) validation.hidden = true;
        });
    }

    trigger.addEventListener('click', () => {
        if (step === success && successSeen) {
            issueLink.removeAttribute('href');
            issueLink.textContent = '';
            successSeen = false;
            show(draft);
        }
        dialog.showModal();
        if (step !== draft) show(step);
    });
    trigger.hidden = false;

    draft.addEventListener('submit', async (event) => {
        event.preventDefault();
        if (pending) return;
        const alert = draft.querySelector('[role="alert"]');
        alert.textContent = '';
        validation.hidden = true;
        validationList.replaceChildren();
        const values = {};
        const blanks = [];
        for (const field of fields) {
            fieldError(field, '');
            const value = field.value.trim();
            if (field.required && !value) blanks.push(field.name);
            if (value) values[field.name] = value;
        }
        if (blanks.length) {
            showFieldErrors(blanks, 'Fill in the required fields below.',
                () => 'This field is required. Enter more than spaces.');
            return;
        }

        clearPreview();
        setPending(true);
        let result;
        try {
            result = await post(draft, values);
        } catch {
            alert.textContent = 'Unable to preview report: Request error. Check your connection and try again.';
        } finally {
            setPending(false);
        }
        if (!result) return;
        if (!result.ok) {
            alert.textContent = previewError(result);
            return;
        }
        const { data } = result;
        if (![data?.title, data?.body, data?.token].every((value) => typeof value === 'string' && value.trim())) {
            alert.textContent = 'Unable to preview report: Server error.';
            return;
        }
        token = data.token;
        issueTitle.textContent = data.title;
        issueBody.textContent = data.body;
        setAvailability(publish, false);
        show(preview);
    });

    back.addEventListener('click', () => {
        if (pending) return;
        clearPreview();
        show(draft);
    });

    preview.addEventListener('submit', async (event) => {
        event.preventDefault();
        if (pending || !token) return;
        const alert = preview.querySelector('[role="alert"]');
        alert.textContent = '';
        setPending(true);
        let result;
        try {
            result = await post(preview, { token });
        } catch {
            // Only retry explicitly, using the same single-publication token.
            setRetryPublication(true);
            alert.textContent = transportErrorCopy;
        } finally {
            setPending(false);
        }
        if (!result) return;
        if (!result.ok) {
            const code = result.data?.code;
            // Authentication and rate limiting do not attempt to publish.
            if (code !== 'rate_limited' && result.status !== 429 && result.status !== 401) blockPublication();
            alert.textContent = publishError(result);
            return;
        }
        const url = githubIssueUrl(result.data);
        if (!url) {
            blockPublication();
            alert.textContent = uncertainCopy;
            return;
        }

        issueLink.href = url;
        issueLink.textContent = `View issue #${result.data.number} on GitHub (opens in a new tab)`;
        draft.reset();
        clearPreview();
        show(success);
    });
})();
