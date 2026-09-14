// dashboard.js — CA Review Dashboard State Manager & Client Controller

// ── API auth helper (Phase 1: CA session + optional API key) ─────────────────
function getDashboardApiKey() {
    return localStorage.getItem('DASHBOARD_API_KEY') || '';
}

function getCaSessionToken() {
    return localStorage.getItem('CA_SESSION_TOKEN') || '';
}

function getCaProfile() {
    try {
        return JSON.parse(localStorage.getItem('CA_PROFILE') || 'null');
    } catch {
        return null;
    }
}

function apiHeaders(extra = {}) {
    const headers = { ...extra };
    const session = getCaSessionToken();
    if (session) {
        // Firm CA session wins — never also send admin key (avoids "see all clients" confusion)
        headers['X-CA-Session'] = session;
        return headers;
    }
    const key = getDashboardApiKey();
    if (key) headers['X-API-Key'] = key;
    return headers;
}

async function caLogin(inviteCode, password) {
    const res = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ invite_code: inviteCode, password }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
        throw new Error(formatApiDetail(data, 'Login failed'));
    }
    localStorage.setItem('CA_SESSION_TOKEN', data.session_token);
    localStorage.setItem('CA_PROFILE', JSON.stringify(data.ca || {}));
    if (data.firm) {
        localStorage.setItem('CA_FIRM', JSON.stringify(data.firm));
    } else {
        localStorage.removeItem('CA_FIRM');
    }
    // Leave admin mode — firm login must not keep seeing all tenants via API key
    localStorage.removeItem('DASHBOARD_API_KEY');
    resetClientWorkspace();
    updateFirmChip();
    return data;
}

async function caLogout() {
    const token = getCaSessionToken();
    let invalidated = false;
    try {
        const res = await fetch('/api/auth/logout', {
            method: 'POST',
            headers: token ? { 'X-CA-Session': token } : {},
        });
        const data = await res.json().catch(() => ({}));
        invalidated = !!data.invalidated;
        // Always clear client credentials listed by server (or our defaults)
        const keys = Array.isArray(data.clear_local)
            ? data.clear_local
            : ['CA_SESSION_TOKEN', 'CA_PROFILE', 'CA_FIRM', 'DASHBOARD_API_KEY'];
        keys.forEach((k) => {
            try { localStorage.removeItem(k); } catch (_) { /* ignore */ }
        });
    } catch (_) {
        // Network failure — still wipe local creds so UI cannot keep admin/CA view
        localStorage.removeItem('CA_SESSION_TOKEN');
        localStorage.removeItem('CA_PROFILE');
        localStorage.removeItem('CA_FIRM');
        localStorage.removeItem('DASHBOARD_API_KEY');
    }
    try {
        localStorage.removeItem('DASHBOARD_CONTEXT');
    } catch (_) { /* ignore */ }
    resetClientWorkspace();
    updateFirmChip();
    return { invalidated };
}

/** Sidebar Sign out — must clear session (old link to "/" did not). */
async function handleSignOutClick(e) {
    if (e) e.preventDefault();
    const result = await caLogout();
    showToast(
        result && result.invalidated
            ? 'Signed out — session revoked on server. Log in with another firm invite to switch.'
            : 'Signed out locally. Use Settings → 1 to log in as another firm.'
    );
    if (elements.clientSelect) {
        elements.clientSelect.innerHTML = '<option value="">Signed out — use Settings → 1 to log in…</option>';
    }
}

/** Clear selected client / cached lists so a new firm login cannot show old dropdown data. */
function resetClientWorkspace() {
    try {
        state.selectedClientPhone = '';
        state.clients = [];
        state.invoices = [];
        state.filteredInvoiceIds = [];
        state.currentInvoice = null;
        if (elements.clientSelect) {
            elements.clientSelect.innerHTML = '<option value="">Select a client…</option>';
        }
        if (elements.activeClientName) elements.activeClientName.textContent = '—';
        if (elements.activeClientGstin) elements.activeClientGstin.textContent = '';
    } catch (_) { /* state/elements may not exist yet */ }
}

function getCaFirm() {
    try {
        return JSON.parse(localStorage.getItem('CA_FIRM') || 'null');
    } catch {
        return null;
    }
}

function updateFirmChip() {
    const el = document.getElementById('firm-chip');
    if (!el) return;
    const firm = getCaFirm();
    const profile = getCaProfile();
    const name = (firm && firm.name) || (profile && profile.firm_name) || '';
    if (name && getCaSessionToken()) {
        el.textContent = name;
        el.style.display = '';
        el.title = 'Firm workspace — you only see clients linked to your CA in this firm';
    } else if (getDashboardApiKey() && !getCaSessionToken()) {
        el.textContent = 'Platform admin';
        el.style.display = '';
        el.title = 'DASHBOARD_API_KEY sees all firms (ops only — do not share with customers)';
    } else {
        el.textContent = '';
        el.style.display = 'none';
    }
}

async function refreshAuthMe() {
    try {
        const res = await apiFetch('/api/auth/me');
        if (!res.ok) return;
        const me = await res.json();
        state.isAdminKey = !!me.is_admin_key;
        if (me.firm_id != null || me.firm_name) {
            localStorage.setItem(
                'CA_FIRM',
                JSON.stringify({ id: me.firm_id, name: me.firm_name })
            );
        }
        updateFirmChip();
        updatePilotAdminControls();
    } catch (_) { /* ignore */ }
}

/**
 * Force CA login (or admin API key) when the dashboard opens with no valid session.
 * Returns true if credentials are available afterward.
 */
async function promptCaLoginOrAdminKey({ reason } = {}) {
    const invite = window.prompt(
        (reason ? reason + '\n\n' : '')
            + 'CA login required.\n\n'
            + 'Enter your 6-digit firm invite code.\n'
            + '(Demo firm: 123456)\n\n'
            + 'Leave blank only for platform admin API key:',
        ''
    );
    if (invite === null) return false;
    if (invite.trim()) {
        const password = window.prompt(
            'CA password\n(from CA_BOOTSTRAP_PASSWORD / your firm password):',
            ''
        );
        if (password === null || !String(password).trim()) {
            showToast('Password required for CA login.', true);
            return false;
        }
        try {
            const data = await caLogin(invite.trim(), password);
            const firmLabel = data.firm?.name || data.ca?.firm_name || '';
            showToast(
                firmLabel
                    ? `Logged in as ${data.ca?.name || 'CA'} · ${firmLabel}`
                    : `Logged in as ${data.ca?.name || 'CA'}`
            );
            return true;
        } catch (e) {
            showToast(e.message || 'CA login failed', true);
            return false;
        }
    }
    const key = window.prompt(
        'Platform admin API key (DASHBOARD_API_KEY).\n'
            + 'Do not use this for a normal CA firm login.\n'
            + 'Leave blank to cancel:',
        ''
    );
    if (key === null || !String(key).trim()) {
        showToast('Sign in required to use the dashboard.', true);
        return false;
    }
    localStorage.removeItem('CA_SESSION_TOKEN');
    localStorage.removeItem('CA_PROFILE');
    localStorage.removeItem('CA_FIRM');
    localStorage.setItem('DASHBOARD_API_KEY', String(key).trim());
    updateFirmChip();
    showToast('Admin API key saved');
    return true;
}

async function ensureDashboardAuth() {
    // Validate existing CA session
    if (getCaSessionToken()) {
        try {
            const res = await fetch('/api/auth/me', { headers: apiHeaders() });
            if (res.ok) {
                const me = await res.json();
                if (me.firm_id != null || me.firm_name) {
                    localStorage.setItem(
                        'CA_FIRM',
                        JSON.stringify({ id: me.firm_id, name: me.firm_name })
                    );
                }
                updateFirmChip();
                return true;
            }
            // Stale / revoked session
            localStorage.removeItem('CA_SESSION_TOKEN');
            localStorage.removeItem('CA_PROFILE');
            localStorage.removeItem('CA_FIRM');
            updateFirmChip();
        } catch (_) {
            /* network — fall through to prompt */
        }
    }
    // Admin key path: confirm /api/clients accepts it
    if (getDashboardApiKey() && !getCaSessionToken()) {
        try {
            const res = await fetch('/api/clients', { headers: apiHeaders() });
            if (res.ok) {
                updateFirmChip();
                return true;
            }
            localStorage.removeItem('DASHBOARD_API_KEY');
            updateFirmChip();
        } catch (_) { /* fall through */ }
    }
    return promptCaLoginOrAdminKey({
        reason: 'Welcome to the CA workspace — sign in to continue.',
    });
}

async function apiFetch(url, options = {}) {
    const opts = { ...options };
    opts.headers = apiHeaders(opts.headers || {});
    let response = await fetch(url, opts);
    if (response.status === 401) {
        // Prefer CA login; fall back to shared API key for admins/scripts
        const invite = window.prompt(
            'Log in as CA (firm workspace).\n\n'
            + 'Enter THIS firm\'s 6-digit invite\n'
            + '(Demo=123456 · Firm B=574646 · Firm A=574509).\n\n'
            + 'Leave blank only for platform admin API key:'
        ) || '';
        if (invite.trim()) {
            const password = window.prompt(
                'CA password\n(default from .env CA_BOOTSTRAP_PASSWORD,\noften: Taxova@ChangeMe):'
            ) || '';
            if (!password.trim()) {
                if (typeof showToast === 'function') {
                    showToast('Password required for CA login.', true);
                }
                return response;
            }
            try {
                await caLogin(invite.trim(), password);
                opts.headers = apiHeaders(options.headers || {});
                response = await fetch(url, opts);
            } catch (e) {
                if (typeof showToast === 'function') showToast(e.message || 'CA login failed', true);
            }
        } else {
            const key = window.prompt('Enter Dashboard API Key (DASHBOARD_API_KEY from .env):') || '';
            if (key) {
                localStorage.setItem('DASHBOARD_API_KEY', key.trim());
                opts.headers = apiHeaders(options.headers || {});
                response = await fetch(url, opts);
            } else if (typeof showToast === 'function') {
                showToast('Login required: CA invite+password or DASHBOARD_API_KEY.', true);
            }
        }
    }
    return response;
}

function formatApiDetail(payload, fallback) {
    if (!payload) return fallback;
    const detail = payload.detail;
    if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
        if (typeof detail.user_message === 'string' && detail.user_message.trim()) {
            return detail.user_message.trim();
        }
        if (typeof detail.message === 'string' && detail.message.trim()) {
            return detail.message.trim();
        }
    }
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail)) {
        return detail.map(d => d.msg || JSON.stringify(d)).join('; ') || fallback;
    }
    if (typeof payload.user_message === 'string') return payload.user_message;
    if (typeof payload.message === 'string') return payload.message;
    return fallback;
}

/** Plain-language GSTN portal check errors (never dump Sandbox JSON to the CA). */
function formatPortalCheckError(err) {
    const raw = String(
        (err && (err.userMessage || err.message)) || err || ''
    );
    const code = (err && err.errorCode) || '';
    const low = raw.toLowerCase();

    if (
        code === 'sandbox_subscription_expired' ||
        low.includes('subscription has expired') ||
        (low.includes('401') && low.includes('subscription'))
    ) {
        return {
            title: 'Portal check offline (not a Taxova outage)',
            body: 'The GST portal API plan needs renewal. Your invoice data is fine — you can still review, edit, and approve. Use Import GSTR-2B for file-based matching until lookup is back.',
            errorCode: 'sandbox_subscription_expired',
            portalOutage: true,
        };
    }
    if (code === 'sandbox_not_configured' || low.includes('not configured')) {
        return {
            title: 'Portal check offline (optional)',
            body: 'Live GSTIN lookup is not configured. Your invoice data is fine — you can still review, edit, and approve.',
            errorCode: 'sandbox_not_configured',
            portalOutage: true,
        };
    }
    if (
        code === 'sandbox_unreachable' ||
        low.includes('timed out') ||
        low.includes('timeout') ||
        low.includes('failed to fetch') ||
        low.includes('networkerror')
    ) {
        return {
            title: 'Portal check offline (not a Taxova outage)',
            body: 'Try Recheck in a minute. Your invoice data is fine — you can still review and approve. Prefer Import GSTR-2B if you need matching today.',
            errorCode: 'sandbox_unreachable',
            portalOutage: true,
        };
    }
    if (
        code === 'sandbox_auth_failed' ||
        low.includes('authenticate failed') ||
        low.includes('sandbox authenticate')
    ) {
        return {
            title: 'Portal check offline (not a Taxova outage)',
            body: 'The portal API could not authenticate. Invoice data is fine — you can still review, edit, and approve.',
            errorCode: code || 'sandbox_auth_failed',
            portalOutage: true,
        };
    }
    // Prefer API user_message when present; never show raw JSON blobs
    const looksTechnical =
        low.includes('transaction_id') ||
        low.includes("{'code'") ||
        low.includes('{"code"') ||
        low.includes('sandbox /');
    return {
        title: 'Portal check offline (not a Taxova outage)',
        body: looksTechnical
            ? 'Portal check is offline right now. Your invoice data is fine — you can still review, edit, and approve.'
            : (raw || 'Portal check is offline right now. Your invoice data is fine — you can still review and approve.'),
        errorCode: code || 'sandbox_error',
        portalOutage: true,
    };
}

function isPortalOutageAssessment(assessment) {
    if (!assessment) return false;
    if (assessment.portal_outage || assessment.portalOutage) return true;
    const code = assessment.error_code || assessment.errorCode || '';
    if (String(code).startsWith('sandbox_')) return true;
    const blob = [
        assessment.reason,
        ...(assessment.issues || []),
        assessment.user_message,
    ].filter(Boolean).join(' ');
    if (!blob) return false;
    const low = blob.toLowerCase();
    return (
        low.includes('subscription has expired') ||
        low.includes('sandbox authenticate') ||
        low.includes('transaction_id') ||
        low.includes('portal api') ||
        low.includes('gstn lookup')
    );
}

async function readJsonOrThrow(response, fallbackMsg) {
    let payload = null;
    try {
        payload = await response.json();
    } catch (_) {
        /* non-JSON body */
    }
    if (!response.ok) {
        const err = new Error(formatApiDetail(payload, fallbackMsg || `Request failed (${response.status})`));
        const detail = payload && payload.detail;
        if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
            err.errorCode = detail.error_code || '';
            err.userMessage = detail.user_message || err.message;
            err.technical = detail.technical || '';
        }
        throw err;
    }
    return payload;
}

/** Pending = not approved and not rejected/skipped (matches KPI / Pending tab). */
function isPendingReview(inv) {
    const rs = inv.review_status || (inv.is_approved ? 'approved' : 'needs_review');
    if (rs === 'rejected' || rs === 'approved' || rs === 'skipped') return false;
    return !(inv.is_approved === 1 || inv.is_approved === true);
}

/** CA exception queue: not approved, or math/GSTIN flags, or 2B problems. */
function isExceptionInvoice(inv) {
    const rs = inv.review_status || (inv.is_approved ? 'approved' : 'needs_review');
    if (rs === 'rejected' || rs === 'skipped') return false;
    if (isPendingReview(inv)) return true;
    if (!inv.is_calculation_correct) return true;
    const m2b = (inv.gstr2b_match_status || 'none').toLowerCase();
    if (m2b === 'unmatched' || m2b === 'mismatch') return true;
    return false;
}

/** Higher = review first. Deterministic flags only — not model confidence. */
function scoreExceptionRisk(inv) {
    let score = 0;
    if (!inv.is_calculation_correct) score += 40;
    const m2b = (inv.gstr2b_match_status || 'none').toLowerCase();
    if (m2b === 'mismatch') score += 35;
    else if (m2b === 'unmatched') score += 25;
    const rs = inv.review_status || (inv.is_approved ? 'approved' : 'needs_review');
    if (rs === 'needs_review') score += 20;
    if (inv.supplier_gstin && !validateGstinString(inv.supplier_gstin)) score += 20;
    if (inv.itc_partial) score += 15;
    else if (inv.is_itc_eligible === false || inv.is_itc_eligible === 0) score += 10;
    if (rs === 'awaiting_client' || rs === 'client_confirmed') score += 5;
    const ps = inv.portal_supplier;
    if (ps && typeof ps.risk_boost === 'number' && !isPortalOutageAssessment(ps)) score += ps.risk_boost;
    const pr = inv.portal_recipient;
    if (pr && typeof pr.risk_boost === 'number' && !isPortalOutageAssessment(pr)) score += pr.risk_boost;
    return score;
}

/** Clean enough for one-click approve toward the pilot volume bar. */
function isReadyToApprove(inv) {
    if (!inv || inv.is_approved === 1 || inv.is_approved === true) return false;
    const rs = inv.review_status || 'needs_review';
    if (rs === 'rejected' || rs === 'skipped' || rs === 'approved') return false;
    if (inv.is_calculation_correct === false || inv.is_calculation_correct === 0) return false;
    const m2b = (inv.gstr2b_match_status || 'none').toLowerCase();
    if (m2b === 'mismatch' || m2b === 'unmatched') return false;
    if (inv.supplier_gstin && !validateGstinString(inv.supplier_gstin)) return false;
    return true;
}

function truncateExceptionReason(text, maxLen) {
    const s = String(text || '').replace(/\s+/g, ' ').trim();
    const max = maxLen || 72;
    if (s.length <= max) return s;
    return s.slice(0, max - 1) + '…';
}

/** Bill has ITC-specific attention (Sec 17(5), partial, or 2B). */
function hasItcIssue(inv) {
    const rs = inv.review_status || (inv.is_approved ? 'approved' : 'needs_review');
    if (rs === 'rejected' || rs === 'skipped') return false;
    if (inv.itc_partial) return true;
    if (inv.is_itc_eligible === false || inv.is_itc_eligible === 0) return true;
    const blocked = parseFloat(inv.itc_blocked_gst) || 0;
    if (blocked > 0) return true;
    const m2b = (inv.gstr2b_match_status || 'none').toLowerCase();
    return m2b === 'unmatched' || m2b === 'mismatch';
}

function scoreItcIssue(inv) {
    let score = 0;
    const m2b = (inv.gstr2b_match_status || 'none').toLowerCase();
    if (m2b === 'mismatch') score += 40;
    else if (m2b === 'unmatched') score += 30;
    if (inv.itc_partial) score += 25;
    else if (inv.is_itc_eligible === false || inv.is_itc_eligible === 0) score += 20;
    const blocked = parseFloat(inv.itc_blocked_gst) || 0;
    score += Math.min(20, blocked / 500);
    return score;
}

function getItcIssueSummary(inv) {
    const m2b = (inv.gstr2b_match_status || 'none').toLowerCase();
    if (m2b === 'mismatch') {
        return {
            badge: '2B mismatch',
            badgeClass: 'is-2b',
            reason: inv.gstr2b_mismatch_reason || 'Amount or date differs from GSTR-2B',
            amount: parseFloat(inv.itc_eligible_cgst || 0) + parseFloat(inv.itc_eligible_sgst || 0) + parseFloat(inv.itc_eligible_igst || 0),
            amountClass: 'is-warn',
        };
    }
    if (m2b === 'unmatched') {
        return {
            badge: 'Not in 2B',
            badgeClass: 'is-2b',
            reason: 'Invoice missing from imported GSTR-2B — Sec 16(2)(aa)',
            amount: parseFloat(inv.itc_eligible_cgst || 0) + parseFloat(inv.itc_eligible_sgst || 0) + parseFloat(inv.itc_eligible_igst || 0),
            amountClass: 'is-warn',
        };
    }
    if (inv.itc_partial) {
        const blocked = parseFloat(inv.itc_blocked_gst) || 0;
        const elig = parseFloat(inv.itc_eligible_cgst || 0) + parseFloat(inv.itc_eligible_sgst || 0) + parseFloat(inv.itc_eligible_igst || 0);
        return {
            badge: 'Partial ITC',
            badgeClass: 'is-partial',
            reason: inv.itc_ineligibility_reason || 'Mixed bill — some lines blocked under Sec 17(5)',
            amount: blocked > 0 ? blocked : elig,
            amountClass: blocked > 0 ? 'is-blocked' : 'is-warn',
        };
    }
    const blocked = parseFloat(inv.itc_blocked_gst) || 0;
    return {
        badge: 'Blocked',
        badgeClass: 'is-blocked',
        reason: inv.itc_ineligibility_reason || 'Ineligible under Sec 17(5)',
        amount: blocked || (parseFloat(inv.total_cgst || 0) + parseFloat(inv.total_sgst || 0) + parseFloat(inv.total_igst || 0)),
        amountClass: 'is-blocked',
    };
}

/** Why this bill is in the CA queue (rules / 2B / HITL — not AI confidence). */
function getExceptionReasons(inv) {
    const reasons = [];
    if (!inv.is_calculation_correct) reasons.push('Tax math — check amounts');
    if (inv.portal_supplier && inv.portal_supplier.reason && !isPortalOutageAssessment(inv.portal_supplier)) {
        reasons.push(truncateExceptionReason(inv.portal_supplier.reason, 48));
    }
    if (inv.portal_recipient && inv.portal_recipient.reason && !isPortalOutageAssessment(inv.portal_recipient)) {
        reasons.push(truncateExceptionReason(inv.portal_recipient.reason, 48));
    }
    const m2b = (inv.gstr2b_match_status || 'none').toLowerCase();
    if (m2b === 'mismatch') {
        const r = (inv.gstr2b_mismatch_reason || '').trim();
        reasons.push(r ? truncateExceptionReason(r, 48) : 'GSTR-2B mismatch');
    } else if (m2b === 'mismatch_accepted') {
        reasons.push('2B mismatch noted');
    } else if (m2b === 'unmatched') {
        reasons.push('Not in imported 2B');
    }
    if (inv.supplier_gstin && !validateGstinString(inv.supplier_gstin)) {
        reasons.push('Invalid supplier GSTIN');
    }
    if (inv.itc_partial) {
        reasons.push(truncateExceptionReason(
            inv.itc_ineligibility_reason || 'Partial ITC (Sec 17(5))',
            48
        ));
    } else if ((inv.is_itc_eligible === false || inv.is_itc_eligible === 0) && inv.itc_ineligibility_reason) {
        reasons.push(truncateExceptionReason(`ITC blocked: ${inv.itc_ineligibility_reason}`, 48));
    }
    const hitl = (inv.hitl_reason || '').trim();
    if (hitl) reasons.push(truncateExceptionReason(hitl.split(';')[0].trim(), 48));
    const rs = inv.review_status || '';
    if (rs === 'awaiting_client') reasons.push('Awaiting client');
    if (rs === 'client_confirmed') reasons.push('Client OK — CA approve');
    if (!reasons.length) reasons.push('Awaiting CA review');
    // Dedupe while preserving order
    return [...new Set(reasons)];
}

function getExceptionReason(inv) {
    return getExceptionReasons(inv)[0] || 'Awaiting CA review';
}

// ── Application State ──
const state = {
    clients: [],
    selectedClientPhone: '',
    activeModule: 'gst',
    itrReturnId: null,
    itrEstimate: null,
    itrExtracted: null,
    itrLastDocuments: [],
    itrLatestForm16Path: null,
    itrAisSummary: null,
    itrAisReconcile: null,
    selectedMonth: '', // Set on load to latest invoice month / current month (not "all")
    monthCalViewYear: new Date().getFullYear(),
    categoryFilter: '', // Empty = all categories
    pilotDays: 30,
    isAdminKey: false,
    invoices: [],
    activeFilter: 'exceptions',
    searchQuery: '',
    selectedInvoiceIds: new Set(),
    /** Current table order (for drawer next/prev) */
    filteredInvoiceIds: [],
    
    // Viewport state for image viewer
    currentInvoice: null,
    zoomLevel: 1.0,
    rotationAngle: 0,
    previewIsPdf: false,
    /** Latest GSTN portal checks for open audit drawer */
    supplierPortalCheck: null,
    recipientPortalCheck: null,
    supplierPortalCheckSeq: 0,
    recipientPortalCheckSeq: 0,
    lookupFy: ''
};

// Expose for inline month-picker wiring in dashboard.html
window.state = state;

// ── DOM References ──
const elements = {
    clientSelect: document.getElementById('client-select'),
    editClientGstinBtn: document.getElementById('edit-client-gstin-btn'),
    clientGstinLabel: document.getElementById('client-gstin-label'),
    recomputeClientItcBtn: document.getElementById('recompute-client-itc-btn'),
    importGstr2bBtn: document.getElementById('import-gstr2b-btn'),
    fetchGstr2bBtn: document.getElementById('fetch-gstr2b-btn'),
    gstSessionChip: document.getElementById('gst-session-chip'),
    gstr2bFileInput: document.getElementById('gstr2b-file-input'),
    gstr2bSummaryChip: document.getElementById('gstr2b-summary-chip'),
    toggleAdvancedTools: document.getElementById('toggle-advanced-tools'),
    advancedToolsPanel: document.getElementById('advanced-tools-panel'),
    advancedToolsChevron: document.getElementById('advanced-tools-chevron'),
    workflowGuide: document.getElementById('workflow-guide'),
    workflowHint: document.getElementById('workflow-hint'),
    wfStepReview: document.getElementById('wf-step-review'),
    wfStepImport: document.getElementById('wf-step-import'),
    wfStepClose: document.getElementById('wf-step-close'),
    gstinLookupInput: document.getElementById('gstin-lookup-input'),
    gstinLookupFy: document.getElementById('gstin-lookup-fy'),
    gstinLookupBtn: document.getElementById('gstin-lookup-btn'),
    gstinLookupClientBtn: document.getElementById('gstin-lookup-client-btn'),
    gstinLookupResult: document.getElementById('gstin-lookup-result'),
    supplierPortalCheck: document.getElementById('supplier-portal-check'),
    supplierPortalCheckBody: document.getElementById('supplier-portal-check-body'),
    supplierPortalRecheckBtn: document.getElementById('supplier-portal-recheck-btn'),
    recipientPortalCheck: document.getElementById('recipient-portal-check'),
    recipientPortalCheckBody: document.getElementById('recipient-portal-check-body'),
    recipientPortalRecheckBtn: document.getElementById('recipient-portal-recheck-btn'),
    syncBtn: document.getElementById('sync-btn'),
    globalSearch: document.getElementById('global-search'),

    // GST pilot panel
    pilotPanel: document.getElementById('pilot-panel'),
    pilotStatusPill: document.getElementById('pilot-status-pill'),
    pilotStatusHint: document.getElementById('pilot-status-hint'),
    pilotDaysSelect: document.getElementById('pilot-days-select'),
    pilotRefreshBtn: document.getElementById('pilot-refresh-btn'),
    pilotReviewNextBtn: document.getElementById('pilot-review-next-btn'),
    pilotProgressLabel: document.getElementById('pilot-progress-label'),
    pilotProgressFill: document.getElementById('pilot-progress-fill'),
    pilotApproved: document.getElementById('pilot-approved'),
    pilotApprovedSub: document.getElementById('pilot-approved-sub'),
    pilotEditRateCard: document.getElementById('pilot-edit-rate-card'),
    pilotEditRate: document.getElementById('pilot-edit-rate'),
    pilotEditRateSub: document.getElementById('pilot-edit-rate-sub'),
    pilotRejectSkip: document.getElementById('pilot-reject-skip'),
    pilotRejectSkipSub: document.getElementById('pilot-reject-skip-sub'),
    pilotPending: document.getElementById('pilot-pending'),
    pilotPendingSub: document.getElementById('pilot-pending-sub'),
    pilotPendingCard: document.getElementById('pilot-pending-card'),
    pilotFieldBody: document.getElementById('pilot-field-body'),
    pilotPreflightCount: document.getElementById('pilot-preflight-count'),
    pilotResetStatsBtn: document.getElementById('pilot-reset-stats-btn'),
    
    // KPI metrics
    kpiPendingTrend: document.getElementById('kpi-pending-trend'),
    kpiPendingCount: document.getElementById('kpi-pending-count'),
    kpiPendingSubtext: document.getElementById('kpi-pending-subtext'),
    kpiSalesTotal: document.getElementById('kpi-sales-total'),
    kpiSalesSubtext: document.getElementById('kpi-sales-subtext'),
    kpiItcTotal: document.getElementById('kpi-itc-total'),
    kpiItcSubtext: document.getElementById('kpi-itc-subtext'),
    kpiFlaggedCount: document.getElementById('kpi-flagged-count'),

    // Net GST Payable row
    netSalesGst: document.getElementById('net-sales-gst'),
    netItcAmount: document.getElementById('net-itc-amount'),
    netGstPayable: document.getElementById('net-gst-payable'),
    netItcBlocked: document.getElementById('net-itc-blocked'),
    netItc2bMatched: document.getElementById('net-itc-2b-matched'),
    netItcCgst: document.getElementById('net-itc-cgst'),
    netItcSgst: document.getElementById('net-itc-sgst'),
    netItcIgst: document.getElementById('net-itc-igst'),
    itcUtilisationBar: document.getElementById('itc-utilisation-bar'),
    itcUtilisationPct: document.getElementById('itc-utilisation-pct'),

    // Sparkline
    sparklineTotalLabel: document.getElementById('sparkline-total-label'),
    sparklineTrendLabel: document.getElementById('sparkline-trend-label'),
    sparklineCanvas: document.getElementById('itc-sparkline-chart'),
    itcSectionPanel: document.getElementById('itc-section-panel'),
    itcIssuesPanel: document.getElementById('itc-issues-panel'),
    itcIssuesList: document.getElementById('itc-issues-list'),
    itcIssuesSubtitle: document.getElementById('itc-issues-subtitle'),
    itcIssuesViewAll: document.getElementById('itc-issues-view-all'),
    kpiItcCard: document.getElementById('kpi-itc-card'),
    
    // Filing list
    filingStatusTitle: document.getElementById('filing-status-title'),
    statusGstr1Val: document.getElementById('status-gstr1-val'),
    statusGstr3bVal: document.getElementById('status-gstr3b-val'),
    
    // Register
    recordCountTxt: document.getElementById('record-count-txt'),
    invoiceRowsBody: document.getElementById('invoice-rows-body'),
    tabBtns: document.querySelectorAll('.tab-btn'),
    
    // Drawer
    auditDrawer: document.getElementById('audit-drawer'),
    auditDrawerOverlay: document.getElementById('audit-drawer-overlay'),
    drawerInvoiceNum: document.getElementById('drawer-invoice-num'),
    drawerInvoiceId: document.getElementById('drawer-invoice-id'),
    closeDrawerBtn: document.getElementById('close-drawer-btn'),
    drawerCancelBtn: document.getElementById('drawer-cancel-btn'),
    drawerSaveBtn: document.getElementById('drawer-save-btn'),
    drawerApproveBtn: document.getElementById('drawer-approve-btn'),
    drawerPrimaryIssue: document.getElementById('drawer-primary-issue'),
    drawerIssueTitle: document.getElementById('drawer-issue-title'),
    drawerIssueMore: document.getElementById('drawer-issue-more'),
    drawerAlertsCount: document.getElementById('drawer-alerts-count'),
    drawerFoldItc: document.getElementById('drawer-fold-itc'),
    drawerFoldAlerts: document.getElementById('drawer-fold-alerts'),
    drawerFoldPortal: document.getElementById('drawer-fold-portal'),
    drawerFoldParties: document.getElementById('drawer-fold-parties'),
    drawerRejectBtn: document.getElementById('drawer-reject-btn'),
    recomputeItcBtn: document.getElementById('recompute-itc-btn'),
    drawerPrevBtn: document.getElementById('drawer-prev-btn'),
    drawerNextBtn: document.getElementById('drawer-next-btn'),
    drawerNavPos: document.getElementById('drawer-nav-pos'),
    selectAllInvoices: document.getElementById('select-all-invoices'),
    bulkBar: document.getElementById('bulk-bar'),
    bulkSelectedCount: document.getElementById('bulk-selected-count'),
    bulkApproveBtn: document.getElementById('bulk-approve-btn'),
    bulkClearBtn: document.getElementById('bulk-clear-btn'),
    queueViewCount: document.getElementById('queue-view-count'),
    
    // Image Viewport
    invoiceDocImg: document.getElementById('invoice-doc-img'),
    invoiceDocPdf: document.getElementById('invoice-doc-pdf'),
    invoiceDocFallback: document.getElementById('invoice-doc-fallback'),
    invoiceDocDownload: document.getElementById('invoice-doc-download'),
    categoryFilter: document.getElementById('category-filter'),
    monthPicker: null, // replaced by custom calendar popover
    monthPickerClear: document.getElementById('month-picker-clear'),
    monthPickerWrap: document.getElementById('month-picker-wrap'),
    monthPickerDisplay: document.getElementById('month-picker-display'),
    monthCalPopover: document.getElementById('month-cal-popover'),
    monthCalYear: document.getElementById('month-cal-year'),
    monthCalGrid: document.getElementById('month-cal-grid'),
    monthCalPrev: document.getElementById('month-cal-prev'),
    monthCalNext: document.getElementById('month-cal-next'),
    monthCalAll: document.getElementById('month-cal-all'),
    zoomInBtn: document.getElementById('zoom-in-btn'),
    zoomOutBtn: document.getElementById('zoom-out-btn'),
    rotateBtn: document.getElementById('rotate-btn'),
    
    // Form Inputs
    fInvNum: document.getElementById('f-inv-num'),
    fInvDate: document.getElementById('f-inv-date'),
    fSupName: document.getElementById('f-sup-name'),
    fSupGst: document.getElementById('f-sup-gst'),
    fSupGstVal: document.getElementById('f-sup-gst-val'),
    fRecName: document.getElementById('f-rec-name'),
    fRecGst: document.getElementById('f-rec-gst'),
    fRecGstVal: document.getElementById('f-rec-gst-val'),
    fCategory: document.getElementById('f-category'),
    fSupplyType: document.getElementById('f-supply-type'),
    fItcEligible: document.getElementById('f-itc-eligible'),
    fPos: document.getElementById('f-pos'),
    fItcReason: document.getElementById('f-itc-reason'),
    itcReasonGroup: document.getElementById('itc-reason-group'),
    fTaxable: document.getElementById('f-taxable'),
    fGrand: document.getElementById('f-grand'),
    fCgst: document.getElementById('f-cgst'),
    fSgst: document.getElementById('f-sgst'),
    fIgst: document.getElementById('f-igst'),
    
    // Drawer Alerts & Logs
    auditAlertsCard: document.getElementById('audit-alerts-card'),
    auditAlertsList: document.getElementById('audit-alerts-list'),
    lineItcCard: document.getElementById('line-itc-card'),
    lineItcBody: document.getElementById('line-itc-body'),
    lineItcSummary: document.getElementById('line-itc-summary'),
    drawerLogsTimeline: document.getElementById('drawer-logs-timeline'),
    
    // GSTR Modal
    navGstrBtn: document.getElementById('nav-gstr-btn'),
    navQueueBtn: document.getElementById('nav-queue-btn'),
    navClientsBtn: document.getElementById('nav-clients-btn'),
    sidebarClientName: document.getElementById('sidebar-client-name'),
    sidebarClientMeta: document.getElementById('sidebar-client-meta'),
    navSettingsBtn: document.getElementById('nav-settings-btn'),
    navSupportBtn: document.getElementById('nav-support-btn'),
    navNewAuditBtn: document.getElementById('nav-new-audit-btn'),
    navDashboardBtn: document.getElementById('nav-dashboard-btn'),
    navSimulatorBtn: document.getElementById('nav-simulator-btn'),
    navSignoutBtn: document.getElementById('nav-signout-btn'),
    gstrModalOverlay: document.getElementById('gstr-modal-overlay'),
    gstrModal: document.getElementById('gstr-modal'),
    closeGstrModalBtn: document.getElementById('close-gstr-modal-btn'),
    cancelGstrModalBtn: document.getElementById('cancel-gstr-modal-btn'),
    exportGstrJsonBtn: document.getElementById('export-gstr-json-btn'),
    modalGstrMonth: document.getElementById('modal-gstr-month'),
    modalGstrType: document.getElementById('modal-gstr-type'),
    prevSales: document.getElementById('prev-sales'),
    prevTax: document.getElementById('prev-tax'),
    prevItc: document.getElementById('prev-itc'),
    prevItcBlocked: document.getElementById('prev-itc-blocked'),
    prevNetGst: document.getElementById('prev-net-gst'),

    // CA form modal (GSTIN / Import 2B / queue notes)
    caFormModalOverlay: document.getElementById('ca-form-modal-overlay'),
    caFormModal: document.getElementById('ca-form-modal'),
    caFormModalTitle: document.getElementById('ca-form-modal-title-text'),
    caFormModalIcon: document.getElementById('ca-form-modal-icon'),
    caFormModalBody: document.getElementById('ca-form-modal-body'),
    caFormModalClose: document.getElementById('ca-form-modal-close'),
    caFormModalCancel: document.getElementById('ca-form-modal-cancel'),
    caFormModalSubmit: document.getElementById('ca-form-modal-submit'),

    // Month-close strip
    monthCloseStrip: document.getElementById('month-close-strip'),
    monthClosePeriod: document.getElementById('month-close-period'),
    monthCloseReadyBadge: document.getElementById('month-close-ready-badge'),
    monthCloseBills: document.getElementById('month-close-bills'),
    monthCloseNeedCa: document.getElementById('month-close-need-ca'),
    monthClose2bImported: document.getElementById('month-close-2b-imported'),
    monthClose2bMatched: document.getElementById('month-close-2b-matched'),
    monthClose2bMissing: document.getElementById('month-close-2b-missing'),
    monthClose2bMismatch: document.getElementById('month-close-2b-mismatch'),
    monthCloseBlockers: document.getElementById('month-close-blockers'),
    monthCloseReviewBtn: document.getElementById('month-close-review-btn'),
    monthCloseImportBtn: document.getElementById('month-close-import-btn'),
    monthCloseNudgeBtn: document.getElementById('month-close-nudge-btn'),
    monthCloseExportBtn: document.getElementById('month-close-export-btn'),
    stepBillsDot: document.getElementById('step-bills-dot'),
    step2bSub: document.getElementById('step-2b-sub'),
    stepBillsSub: document.getElementById('step-bills-sub'),
    stepQueueSub: document.getElementById('step-queue-sub'),
    stepExportSub: document.getElementById('step-export-sub'),

    // GST Filing panel (Phase 1)
    gstFilingPanel: document.getElementById('gst-filing-panel'),
    gstFilingReadyBadge: document.getElementById('gst-filing-ready-badge'),
    gstFilingPeriod: document.getElementById('gst-filing-period'),
    gstFilingStatBills: document.getElementById('gst-filing-stat-bills'),
    gstFilingStatNeedCa: document.getElementById('gst-filing-stat-need-ca'),
    gstFilingStat2b: document.getElementById('gst-filing-stat-2b'),
    gstFilingStatGaps: document.getElementById('gst-filing-stat-gaps'),
    gstFilingBlockers: document.getElementById('gst-filing-blockers'),
    gstFilingReviewBtn: document.getElementById('gst-filing-review-btn'),
    gstFilingExportGstr1Btn: document.getElementById('gst-filing-export-gstr1-btn'),
    gstFilingExportGstr3bBtn: document.getElementById('gst-filing-export-gstr3b-btn'),
    gstFilingGstr1Exported: document.getElementById('gst-filing-gstr1-exported'),
    gstFilingGstr3bExported: document.getElementById('gst-filing-gstr3b-exported'),
    gstFilingMarked: document.getElementById('gst-filing-marked'),
    gstFilingArn: document.getElementById('gst-filing-arn'),
    
    // Toast
    toast: document.getElementById('toast')
};

// Chart.js instance (stored to destroy on re-render)
let itcSparklineChart = null;
/** Active CA form modal submit handler */
let caFormSubmitHandler = null;

// ── App Init ──
window.addEventListener('DOMContentLoaded', () => {
    (async () => {
        try {
            // Never show "All months" while clients load — seed a real period immediately
            if (!state.selectedMonth || state.selectedMonth === 'all') {
                const saved = (loadDashContext().month || '').trim();
                state.selectedMonth = /^\d{4}-\d{2}$/.test(saved) ? saved : currentYearMonth();
            }
            setupEventListeners();
            syncMonthPickerDisplay();
            setupItrModule();
            updateFirmChip();
        } catch (err) {
            console.error('Dashboard init error:', err);
        }

        const ok = await ensureDashboardAuth();
        if (!ok) {
            if (elements.clientSelect) {
                elements.clientSelect.innerHTML =
                    '<option value="">Not signed in — open Settings → 1 (CA login)…</option>';
            }
            showToast('Sign in required: Settings → 1 = CA invite + password.', true);
            return;
        }
        try {
            await refreshAuthMe();
        } catch (_) { /* ignore */ }
        await fetchClients();
    })().catch((err) => {
        console.error('Dashboard auth/init failed:', err);
        showToast(err.message || 'Could not start dashboard', true);
    });
});

// ── Event Handlers Hookup ──
function setupEventListeners() {
    // Client selection change — keep a real month for month-close clarity
    elements.clientSelect.addEventListener('change', async (e) => {
        state.selectedClientPhone = e.target.value;
        invalidateClientInvoiceCache();
        saveDashContext({ phone: state.selectedClientPhone });
        updateClientGstinLabel();
        refreshGstSessionStatus();
        resetItrPanelForClient();
        tryLoadItrReturnForFy(true).catch(() => {});
        try {
            const month = await resolveMonthForClient(state.selectedClientPhone, { preferSaved: true });
            if (state.selectedMonth === month) {
                fetchData();
            } else {
                setSelectedMonth(month);
            }
        } catch (err) {
            console.error(err);
            if (!state.selectedMonth) state.selectedMonth = currentYearMonth();
            syncMonthPickerDisplay();
            fetchData();
        }
    });

    if (elements.editClientGstinBtn) {
        elements.editClientGstinBtn.addEventListener('click', editClientGstin);
    }
    if (elements.recomputeClientItcBtn) {
        elements.recomputeClientItcBtn.addEventListener('click', recomputeClientItc);
    }
    if (elements.importGstr2bBtn && elements.gstr2bFileInput) {
        elements.importGstr2bBtn.addEventListener('click', openImportGstr2bModal);
        elements.gstr2bFileInput.addEventListener('change', () => {
            const nameEl = document.getElementById('ca-import-file-name');
            const f = elements.gstr2bFileInput.files && elements.gstr2bFileInput.files[0];
            if (nameEl) nameEl.textContent = f ? f.name : 'No file selected';
        });
    }
    if (elements.caFormModalClose) elements.caFormModalClose.addEventListener('click', closeCaFormModal);
    if (elements.caFormModalCancel) elements.caFormModalCancel.addEventListener('click', closeCaFormModal);
    if (elements.caFormModalOverlay) elements.caFormModalOverlay.addEventListener('click', closeCaFormModal);
    if (elements.caFormModalSubmit) {
        elements.caFormModalSubmit.addEventListener('click', async () => {
            if (!caFormSubmitHandler) return;
            try {
                elements.caFormModalSubmit.disabled = true;
                await caFormSubmitHandler();
            } catch (e) {
                console.error(e);
                if (e.message !== 'Cancelled') {
                    showToast(e.message || 'Action failed', true);
                }
            } finally {
                elements.caFormModalSubmit.disabled = false;
            }
        });
    }
    if (elements.invoiceRowsBody) {
        elements.invoiceRowsBody.addEventListener('click', onInvoiceRowActionClick);
        elements.invoiceRowsBody.addEventListener('click', onEmptyStateActionClick);
    }
    document.addEventListener('click', (e) => {
        if (!e.target.closest('.row-more')) {
            document.querySelectorAll('.row-more.open').forEach((el) => el.classList.remove('open'));
        }
    });
    if (elements.monthCloseReviewBtn) {
        elements.monthCloseReviewBtn.addEventListener('click', () => {
            const tab = document.querySelector('.tab-btn[data-filter="exceptions"]');
            if (tab) tab.click();
            else {
                state.activeFilter = 'exceptions';
                elements.tabBtns.forEach((b) => b.classList.toggle('active', b.dataset.filter === 'exceptions'));
                renderInvoiceTable();
            }
            showToast('Showing Needs review queue');
        });
    }
    if (elements.monthCloseImportBtn) {
        elements.monthCloseImportBtn.addEventListener('click', openImportGstr2bModal);
    }
    if (elements.monthCloseNudgeBtn) {
        elements.monthCloseNudgeBtn.addEventListener('click', () => openClientNudgeModal());
    }
    if (elements.monthCloseExportBtn) {
        elements.monthCloseExportBtn.addEventListener('click', () => openGstrModalForMonthClose());
    }
    if (elements.fetchGstr2bBtn) {
        elements.fetchGstr2bBtn.addEventListener('click', fetchGstr2bFromPortal);
    }
    if (elements.gstinLookupBtn) {
        elements.gstinLookupBtn.addEventListener('click', () => lookupGstinPortal());
    }
    if (elements.gstinLookupClientBtn) {
        elements.gstinLookupClientBtn.addEventListener('click', () => {
            const client = getSelectedClient();
            const gstin = (client && client.gstin) ? String(client.gstin).trim() : '';
            if (!gstin) {
                showToast('Set a client GSTIN first (or type one in the lookup box).', true);
                return;
            }
            if (elements.gstinLookupInput) elements.gstinLookupInput.value = gstin;
            lookupGstinPortal(gstin);
        });
    }
    if (elements.gstinLookupInput) {
        elements.gstinLookupInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                lookupGstinPortal();
            }
        });
    }

    // Month calendar picker
    setupMonthCalendar();
    if (elements.monthPickerClear) {
        elements.monthPickerClear.addEventListener('click', (e) => {
            e.preventDefault();
            e.stopPropagation();
            setSelectedMonth('');
        });
    }

    // Category filter
    if (elements.categoryFilter) {
        elements.categoryFilter.addEventListener('change', (e) => {
            state.categoryFilter = e.target.value || '';
            renderInvoiceTable();
        });
    }

    // Reload dashboard data (no GST portal API yet)
    elements.syncBtn.addEventListener('click', triggerSync);

    if (elements.pilotDaysSelect) {
        elements.pilotDaysSelect.addEventListener('change', () => {
            const days = parseInt(elements.pilotDaysSelect.value, 10);
            state.pilotDays = [7, 30, 90].includes(days) ? days : 30;
            fetchPilotStats().catch((err) => console.error(err));
        });
    }
    if (elements.pilotRefreshBtn) {
        elements.pilotRefreshBtn.addEventListener('click', () => {
            fetchPilotStats().catch((err) => {
                console.error(err);
                showToast(err.message || 'Pilot stats failed', true);
            });
        });
    }
    if (elements.pilotReviewNextBtn) {
        elements.pilotReviewNextBtn.addEventListener('click', () => {
            reviewNextPilotPending().catch((err) => {
                console.error(err);
                showToast(err.message || 'Could not open next bill', true);
            });
        });
    }
    if (elements.pilotPendingCard) {
        const goPending = () => {
            reviewNextPilotPending().catch((err) => {
                console.error(err);
                showToast(err.message || 'Could not open next bill', true);
            });
        };
        elements.pilotPendingCard.addEventListener('click', goPending);
        elements.pilotPendingCard.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                goPending();
            }
        });
    }
    setupPilotPreflightChecklist();
    if (elements.pilotResetStatsBtn) {
        elements.pilotResetStatsBtn.addEventListener('click', () => {
            resetPilotSmokeStats().catch((err) => {
                console.error(err);
                showToast(err.message || 'Reset failed', true);
            });
        });
    }

    // Global Search filter
    elements.globalSearch.addEventListener('input', (e) => {
        state.searchQuery = e.target.value.trim().toLowerCase();
        renderInvoiceTable();
    });

    // Tab switcher
    elements.tabBtns.forEach(btn => {
        btn.addEventListener('click', () => {
            elements.tabBtns.forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            state.activeFilter = btn.dataset.filter;
            renderInvoiceTable();
        });
    });

    // ITC trend chart (collapsed — canvas must be visible to size)
    const toggleItcTrends = document.getElementById('toggle-itc-trends');
    const itcTrendsPanel = document.getElementById('itc-trends-panel');
    const itcTrendsChevron = document.getElementById('itc-trends-chevron');
    if (toggleItcTrends && itcTrendsPanel) {
        toggleItcTrends.addEventListener('click', () => {
            const open = itcTrendsPanel.style.display !== 'none';
            itcTrendsPanel.style.display = open ? 'none' : 'block';
            if (itcTrendsChevron) itcTrendsChevron.textContent = open ? 'expand_more' : 'expand_less';
            if (!open) renderITCSparkline();
        });
    }

    if (elements.kpiItcCard) {
        elements.kpiItcCard.addEventListener('click', () => {
            if (elements.itcSectionPanel) {
                elements.itcSectionPanel.scrollIntoView({ behavior: 'smooth', block: 'start' });
            }
        });
    }
    if (elements.itcIssuesViewAll) {
        elements.itcIssuesViewAll.addEventListener('click', () => {
            const tab = document.querySelector('.tab-btn[data-filter="itc"]');
            if (tab) tab.click();
            const table = document.querySelector('.queue-table');
            if (table) table.scrollIntoView({ behavior: 'smooth', block: 'start' });
        });
    }

    // Advanced tools (OTP / GSTIN FY / recompute) — collapsed by default
    setupAdvancedToolsToggle();

    if (elements.wfStepReview) {
        elements.wfStepReview.addEventListener('click', () => {
            const tab = document.querySelector('.tab-btn[data-filter="exceptions"]');
            if (tab) tab.click();
            const table = document.querySelector('.queue-table');
            if (table) table.scrollIntoView({ behavior: 'smooth', block: 'start' });
        });
    }
    if (elements.wfStepImport) {
        elements.wfStepImport.addEventListener('click', () => openImportGstr2bModal());
    }
    if (elements.wfStepClose) {
        elements.wfStepClose.addEventListener('click', () => {
            if (elements.monthCloseStrip) {
                elements.monthCloseStrip.scrollIntoView({ behavior: 'smooth', block: 'center' });
            }
        });
    }

    // Drawer close hooks
    elements.closeDrawerBtn.addEventListener('click', closeAuditDrawer);
    elements.drawerCancelBtn.addEventListener('click', closeAuditDrawer);
    elements.auditDrawerOverlay.addEventListener('click', closeAuditDrawer);

    // Viewport transform actions
    elements.zoomInBtn.addEventListener('click', () => adjustZoom(0.15));
    elements.zoomOutBtn.addEventListener('click', () => adjustZoom(-0.15));
    elements.rotateBtn.addEventListener('click', rotateImage);

    // Dynamic ITC status toggles
    elements.fItcEligible.addEventListener('change', (e) => {
        toggleItcReasonField(e.target.value !== 'true');
    });

    // Input validations for GSTIN during manual edits
    elements.fSupGst.addEventListener('input', (e) => validateGstinField(e.target, elements.fSupGstVal));
    elements.fRecGst.addEventListener('input', (e) => validateGstinField(e.target, elements.fRecGstVal));
    if (elements.supplierPortalRecheckBtn) {
        elements.supplierPortalRecheckBtn.addEventListener('click', () => {
            const gstin = (elements.fSupGst && elements.fSupGst.value || '').trim().toUpperCase();
            const name = (elements.fSupName && elements.fSupName.value || '').trim();
            verifyPartyOnPortal('supplier', gstin, name, true);
        });
    }
    if (elements.recipientPortalRecheckBtn) {
        elements.recipientPortalRecheckBtn.addEventListener('click', () => {
            const gstin = (elements.fRecGst && elements.fRecGst.value || '').trim().toUpperCase();
            const name = (elements.fRecName && elements.fRecName.value || '').trim();
            verifyPartyOnPortal('recipient', gstin, name, true);
        });
    }
    if (elements.gstinLookupFy) {
        populateGstinLookupFyOptions();
        elements.gstinLookupFy.addEventListener('change', () => {
            state.lookupFy = elements.gstinLookupFy.value;
            const g = (elements.gstinLookupInput && elements.gstinLookupInput.value || '').trim();
            if (g.length === 15) lookupGstinPortal(g);
        });
    }

    // Form Action buttons
    elements.drawerSaveBtn.addEventListener('click', saveInvoiceProgress);
    elements.drawerApproveBtn.addEventListener('click', verifyAndApproveInvoice);
    if (elements.drawerRejectBtn) {
        elements.drawerRejectBtn.addEventListener('click', rejectInvoiceHitl);
    }
    if (elements.recomputeItcBtn) {
        elements.recomputeItcBtn.addEventListener('click', recomputeInvoiceItc);
    }

    // GSTR Modal hooks — nav jumps to Filing panel; modal still used for download
    if (elements.navGstrBtn) {
        elements.navGstrBtn.addEventListener('click', (e) => {
            e.preventDefault();
            setSidebarNavActive('gstr');
            jumpToGstFiling();
        });
    }
    elements.closeGstrModalBtn.addEventListener('click', closeGstrModal);
    elements.cancelGstrModalBtn.addEventListener('click', closeGstrModal);
    elements.gstrModalOverlay.addEventListener('click', closeGstrModal);
    elements.exportGstrJsonBtn.addEventListener('click', compileGstrDownload);
    elements.modalGstrMonth.addEventListener('change', updateGstrModalPreviews);

    if (elements.gstFilingExportGstr1Btn) {
        elements.gstFilingExportGstr1Btn.addEventListener('click', () => openGstFilingExport('GSTR1'));
    }
    if (elements.gstFilingExportGstr3bBtn) {
        elements.gstFilingExportGstr3bBtn.addEventListener('click', () => openGstFilingExport('GSTR3B'));
    }
    if (elements.gstFilingReviewBtn) {
        elements.gstFilingReviewBtn.addEventListener('click', () => {
            jumpToAuditQueue();
            showToast('Clear Needs review, then return to GST Filing');
        });
    }
    if (elements.gstFilingMarked) {
        elements.gstFilingMarked.addEventListener('change', () => {
            const st = loadGstFilingStatus();
            st.marked_prepared = !!elements.gstFilingMarked.checked;
            if (st.marked_prepared && !st.marked_filed_at) {
                st.marked_filed_at = new Date().toISOString();
            }
            if (!st.marked_prepared) st.marked_filed_at = null;
            saveGstFilingStatus(st);
            renderGstFilingPanel(state._lastMetrics || {});
        });
    }
    if (elements.gstFilingArn) {
        let arnTimer = null;
        elements.gstFilingArn.addEventListener('input', () => {
            clearTimeout(arnTimer);
            arnTimer = setTimeout(() => {
                const st = loadGstFilingStatus();
                st.arn_note = (elements.gstFilingArn.value || '').trim();
                saveGstFilingStatus(st);
            }, 300);
        });
    }

    setupSidebarNavigation();

    const monthScopeBanner = document.getElementById('month-scope-banner');
    if (monthScopeBanner) {
        monthScopeBanner.addEventListener('click', (e) => {
            const btn = e.target.closest('[data-scope-action]');
            if (!btn) return;
            const action = btn.getAttribute('data-scope-action');
            if (action === 'all') {
                setSelectedMonth('');
                return;
            }
            if (action === 'month') {
                const m = btn.getAttribute('data-month') || '';
                if (/^\d{4}-\d{2}$/.test(m)) setSelectedMonth(m);
            }
        });
    }
    
    // Keyboard shortcuts
    window.addEventListener('keydown', (e) => {
        const tag = (e.target && e.target.tagName) ? e.target.tagName.toLowerCase() : '';
        const typing = tag === 'input' || tag === 'textarea' || tag === 'select' || e.target?.isContentEditable;

        // Ctrl+K searches
        if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
            e.preventDefault();
            elements.globalSearch.focus();
            return;
        }
        // ESC closes drawer/modal
        if (e.key === 'Escape') {
            closeAuditDrawer();
            closeGstrModal();
            closeCaFormModal();
            return;
        }
        if (typing) return;

        const drawerOpen = elements.auditDrawer && elements.auditDrawer.classList.contains('active');
        if (drawerOpen && state.currentInvoice) {
            if (e.key === 'a' || e.key === 'A') {
                e.preventDefault();
                if (elements.drawerApproveBtn) elements.drawerApproveBtn.click();
                return;
            }
            if (e.key === 'r' || e.key === 'R') {
                e.preventDefault();
                if (elements.drawerRejectBtn) elements.drawerRejectBtn.click();
                return;
            }
            if (e.key === 'j' || e.key === 'ArrowDown') {
                e.preventDefault();
                navigateAuditDrawer(1);
                return;
            }
            if (e.key === 'k' || e.key === 'ArrowUp') {
                e.preventDefault();
                navigateAuditDrawer(-1);
                return;
            }
        }
    });

    if (elements.bulkApproveBtn) {
        elements.bulkApproveBtn.addEventListener('click', bulkApproveSelected);
    }
    if (elements.bulkClearBtn) {
        elements.bulkClearBtn.addEventListener('click', () => {
            state.selectedInvoiceIds.clear();
            updateBulkBar();
            renderInvoiceTable();
        });
    }
    if (elements.selectAllInvoices) {
        elements.selectAllInvoices.addEventListener('change', (e) => {
            const checked = !!e.target.checked;
            const ids = state.filteredInvoiceIds || [];
            ids.forEach((id) => {
                if (checked) state.selectedInvoiceIds.add(Number(id));
                else state.selectedInvoiceIds.delete(Number(id));
            });
            renderInvoiceTable();
            updateBulkBar();
        });
    }
    if (elements.drawerPrevBtn) {
        elements.drawerPrevBtn.addEventListener('click', () => navigateAuditDrawer(-1));
    }
    if (elements.drawerNextBtn) {
        elements.drawerNextBtn.addEventListener('click', () => navigateAuditDrawer(1));
    }
}

// ── API Functions ──

const MONTH_SHORT = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const DASH_CTX_KEY = 'DASHBOARD_CONTEXT';
const DASH_CLIENT_MONTHS_KEY = 'DASHBOARD_CLIENT_MONTHS';

function currentYearMonth() {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
}

function loadDashContext() {
    try {
        return JSON.parse(localStorage.getItem(DASH_CTX_KEY) || '{}') || {};
    } catch (_) {
        return {};
    }
}

function saveDashContext(partial) {
    try {
        const next = { ...loadDashContext(), ...partial };
        localStorage.setItem(DASH_CTX_KEY, JSON.stringify(next));
    } catch (_) { /* ignore */ }
}

function loadClientMonthMap() {
    try {
        return JSON.parse(localStorage.getItem(DASH_CLIENT_MONTHS_KEY) || '{}') || {};
    } catch (_) {
        return {};
    }
}

function saveClientMonth(phone, month) {
    if (!phone || !/^\d{4}-\d{2}$/.test(month || '')) return;
    try {
        const map = loadClientMonthMap();
        map[phone] = month;
        localStorage.setItem(DASH_CLIENT_MONTHS_KEY, JSON.stringify(map));
        saveDashContext({ phone, month });
    } catch (_) { /* ignore */ }
}

function getSavedMonthForClient(phone) {
    if (!phone) return '';
    const map = loadClientMonthMap();
    if (/^\d{4}-\d{2}$/.test(map[phone] || '')) return map[phone];
    const ctx = loadDashContext();
    if (ctx.phone === phone && /^\d{4}-\d{2}$/.test(ctx.month || '')) return ctx.month;
    return '';
}

function pickDefaultClient(clients) {
    if (!clients || !clients.length) return null;
    const savedPhone = (loadDashContext().phone || '').trim();
    if (savedPhone) {
        const hit = clients.find((c) => c.phone_number === savedPhone);
        if (hit) return hit;
    }
    return clients.find((c) => c.phone_number === '919999999999') || clients[0];
}

async function fetchAllInvoicesForClient(phone) {
    if (!phone) return [];
    if (
        state._allInvoicesCache
        && state._allInvoicesCachePhone === phone
        && Array.isArray(state._allInvoicesCache)
    ) {
        return state._allInvoicesCache;
    }
    const response = await apiFetch(`/api/invoices?client_phone=${encodeURIComponent(phone)}`);
    const invoices = await readJsonOrThrow(response, 'Failed to load invoices for month scope');
    state._allInvoicesCachePhone = phone;
    state._allInvoicesCache = invoices || [];
    return state._allInvoicesCache;
}

function invalidateClientInvoiceCache() {
    state._allInvoicesCache = null;
    state._allInvoicesCachePhone = '';
}

/** Prefer month with most Needs-review bills, then most bills, then latest month. */
async function discoverBestInvoiceMonth(phone) {
    if (!phone) return '';
    try {
        const invoices = await fetchAllInvoicesForClient(phone);
        const byMonth = {};
        for (const inv of invoices) {
            const m = String(inv.invoice_date || '').slice(0, 7);
            if (!/^\d{4}-\d{2}$/.test(m)) continue;
            if (!byMonth[m]) byMonth[m] = { total: 0, needs: 0 };
            byMonth[m].total += 1;
            if (isExceptionInvoice(inv) || isPendingReview(inv)) byMonth[m].needs += 1;
        }
        const months = Object.keys(byMonth);
        if (!months.length) return '';
        months.sort((a, b) => {
            const na = byMonth[a].needs - byMonth[b].needs;
            if (na !== 0) return na > 0 ? -1 : 1;
            const ta = byMonth[a].total - byMonth[b].total;
            if (ta !== 0) return ta > 0 ? -1 : 1;
            return a < b ? 1 : -1; // latest YYYY-MM
        });
        return months[0];
    } catch (e) {
        console.warn('discoverBestInvoiceMonth failed:', e);
        return '';
    }
}

/** Always land on a real YYYY-MM for month-close clarity. */
async function resolveMonthForClient(phone, { preferSaved = true } = {}) {
    invalidateClientInvoiceCache();
    let all = [];
    try {
        all = await fetchAllInvoicesForClient(phone);
    } catch (_) {
        all = [];
    }

    if (preferSaved) {
        const saved = getSavedMonthForClient(phone);
        if (saved) {
            const inSaved = all.filter((inv) => String(inv.invoice_date || '').startsWith(saved));
            if (inSaved.length) {
                const needsHere = inSaved.filter((inv) => isExceptionInvoice(inv) || isPendingReview(inv)).length;
                const needsElsewhere = all.filter((inv) => (
                    !String(inv.invoice_date || '').startsWith(saved)
                    && (isExceptionInvoice(inv) || isPendingReview(inv))
                )).length;
                // Don't stick on a closed/approved month when other months still need review
                // (common with test data: storage folder ≠ invoice_date month)
                if (!(needsHere === 0 && needsElsewhere > 0)) return saved;
            }
        }
    }

    // If any month still has CA work, open that first (test bills often have older invoice dates)
    const best = await discoverBestInvoiceMonth(phone);
    return best || currentYearMonth();
}

function updateMonthScopeBanner() {
    const el = document.getElementById('month-scope-banner');
    if (!el) return;
    const scopeAll = !state.selectedMonth || state.selectedMonth === 'all';
    const all = (state._allInvoicesCachePhone === state.selectedClientPhone)
        ? (state._allInvoicesCache || [])
        : [];
    if (scopeAll || !all.length) {
        el.style.display = 'none';
        el.innerHTML = '';
        return;
    }
    const other = all.filter((inv) => !String(inv.invoice_date || '').startsWith(state.selectedMonth));
    if (!other.length) {
        el.style.display = 'none';
        el.innerHTML = '';
        return;
    }
    const otherNeeds = other.filter((inv) => isExceptionInvoice(inv) || isPendingReview(inv)).length;
    const byMonth = {};
    other.forEach((inv) => {
        const m = String(inv.invoice_date || '').slice(0, 7);
        if (!/^\d{4}-\d{2}$/.test(m)) return;
        byMonth[m] = (byMonth[m] || 0) + 1;
    });
    const topOther = Object.keys(byMonth).sort((a, b) => byMonth[b] - byMonth[a])[0];
    el.style.display = 'flex';
    el.innerHTML = `
        <span class="material-symbols-outlined text-[18px]" style="color:#F0B35A;">info</span>
        <span class="msb-text">
            Showing <strong>${formatYearMonthLabel(state.selectedMonth)}</strong> only —
            <strong>${other.length}</strong> more bill(s) in other months
            ${otherNeeds ? `(${otherNeeds} need review)` : ''}.
        </span>
        <button type="button" class="msb-btn" data-scope-action="all">Show all months</button>
        ${topOther ? `<button type="button" class="msb-btn is-primary" data-scope-action="month" data-month="${topOther}">Open ${formatYearMonthLabel(topOther)}</button>` : ''}
    `;
}

async function refreshMonthScopeBanner() {
    if (!state.selectedClientPhone) {
        updateMonthScopeBanner();
        return;
    }
    try {
        if (state._allInvoicesCachePhone !== state.selectedClientPhone) {
            invalidateClientInvoiceCache();
        }
        await fetchAllInvoicesForClient(state.selectedClientPhone);
    } catch (_) { /* ignore */ }
    updateMonthScopeBanner();
}

async function applyClientAndMonthDefaults(clients) {
    const preferred = pickDefaultClient(clients);
    if (!preferred) return;

    state.selectedClientPhone = preferred.phone_number;
    if (elements.clientSelect) elements.clientSelect.value = state.selectedClientPhone;
    saveDashContext({ phone: state.selectedClientPhone });
    updateClientGstinLabel();

    const month = await resolveMonthForClient(state.selectedClientPhone, { preferSaved: true });
    state.selectedMonth = month;
    saveClientMonth(state.selectedClientPhone, month);
    if (state.selectedMonth && /^\d{4}-\d{2}$/.test(state.selectedMonth)) {
        state.monthCalViewYear = parseInt(state.selectedMonth.slice(0, 4), 10);
    }
    syncMonthPickerDisplay();
    if (typeof renderMonthCalendarGrid === 'function') {
        try { renderMonthCalendarGrid(); } catch (_) { /* calendar may not be ready */ }
    }
}

async function fetchClients() {
    try {
        if (elements.clientSelect) {
            elements.clientSelect.innerHTML = '<option value="">Loading clients…</option>';
            elements.clientSelect.disabled = true;
        }
        const response = await apiFetch('/api/clients');
        if (response.status === 401) {
            throw new Error('Dashboard API key required (401). Open Settings and paste DASHBOARD_API_KEY.');
        }
        const clients = await readJsonOrThrow(response, 'Failed to load clients');
        
        state.clients = clients;
        elements.clientSelect.innerHTML = '';
        elements.clientSelect.disabled = false;
        
        if (clients.length === 0) {
            elements.clientSelect.innerHTML = '<option value="">No clients yet</option>';
            state.selectedClientPhone = '';
            state.selectedMonth = currentYearMonth();
            syncMonthPickerDisplay();
            updateClientGstinLabel();
            await fetchPilotStats();
            return;
        }

        clients.forEach(c => {
            const opt = document.createElement('option');
            opt.value = c.phone_number;
            opt.textContent = `${c.name} (${c.gstin || 'No GSTIN'})`;
            elements.clientSelect.appendChild(opt);
        });

        await applyClientAndMonthDefaults(clients);
        if (typeof resetItrPanelForClient === 'function') resetItrPanelForClient();
        await fetchData();
        await fetchPilotStats();
    } catch (e) {
        console.error("Failed to load clients:", e);
        if (elements.clientSelect) {
            elements.clientSelect.disabled = false;
            elements.clientSelect.innerHTML = '';
            const opt = document.createElement('option');
            opt.value = '';
            opt.textContent = 'Failed to load — click Settings for API key';
            elements.clientSelect.appendChild(opt);
        }
        if (!state.selectedMonth) {
            state.selectedMonth = currentYearMonth();
            syncMonthPickerDisplay();
        }
        updateClientGstinLabel();
        showToast(e.message || 'Error loading clients. Open Settings → paste DASHBOARD_API_KEY.', true);
    }
}

function getSelectedClient() {
    return (state.clients || []).find(c => c.phone_number === state.selectedClientPhone) || null;
}

function updateClientGstinLabel() {
    const client = getSelectedClient();
    const gstin = (client && client.gstin) ? String(client.gstin).trim() : '';
    if (elements.clientGstinLabel) {
        elements.clientGstinLabel.textContent = gstin || 'No GSTIN set';
        elements.clientGstinLabel.title = gstin || 'Click to set a valid GSTIN';
    }
    if (elements.editClientGstinBtn) {
        elements.editClientGstinBtn.classList.toggle('border-tertiary/50', !gstin);
        elements.editClientGstinBtn.classList.toggle('text-tertiary', !gstin);
    }
    updateSidebarClientCard(client);
}

function updateSidebarClientCard(client) {
    const card = elements.navClientsBtn;
    const nameEl = elements.sidebarClientName;
    const metaEl = elements.sidebarClientMeta;
    if (!card || !nameEl || !metaEl) return;

    if (!client) {
        card.classList.add('is-empty');
        nameEl.textContent = 'Select a client';
        metaEl.textContent = 'Tap to choose who you’re reviewing';
        return;
    }

    const gstin = (client.gstin || '').trim();
    card.classList.toggle('is-empty', !gstin);
    nameEl.textContent = client.name || 'Unnamed client';
    metaEl.textContent = gstin || 'No GSTIN set — tap to switch';
}

function setSelectedMonth(ym) {
    state.selectedMonth = ym || '';
    if (state.selectedClientPhone && /^\d{4}-\d{2}$/.test(state.selectedMonth)) {
        saveClientMonth(state.selectedClientPhone, state.selectedMonth);
    } else if (!state.selectedMonth) {
        // Clearing to "all months" — keep last per-client month in map, only clear active ctx month
        saveDashContext({ phone: state.selectedClientPhone || '', month: '' });
    }
    closeMonthCalendar();
    syncMonthPickerDisplay();
    fetchData();
}

function setupMonthCalendar() {
    if (!elements.monthPickerWrap || !elements.monthCalPopover) return;

    // Seed view year from selection or today
    if (state.selectedMonth && /^\d{4}-\d{2}$/.test(state.selectedMonth)) {
        state.monthCalViewYear = parseInt(state.selectedMonth.slice(0, 4), 10);
    } else {
        state.monthCalViewYear = new Date().getFullYear();
    }
    syncMonthPickerDisplay();
    renderMonthCalendarGrid();

    elements.monthPickerWrap.addEventListener('click', (e) => {
        if (e.target.closest('#month-picker-clear')) return;
        if (e.target.closest('#month-cal-popover')) return;
        toggleMonthCalendar();
    });

    if (elements.monthCalPrev) {
        elements.monthCalPrev.addEventListener('click', (e) => {
            e.stopPropagation();
            state.monthCalViewYear -= 1;
            renderMonthCalendarGrid();
        });
    }
    if (elements.monthCalNext) {
        elements.monthCalNext.addEventListener('click', (e) => {
            e.stopPropagation();
            state.monthCalViewYear += 1;
            renderMonthCalendarGrid();
        });
    }
    if (elements.monthCalAll) {
        elements.monthCalAll.addEventListener('click', (e) => {
            e.stopPropagation();
            setSelectedMonth('');
        });
    }

    document.addEventListener('click', (e) => {
        if (!elements.monthPickerWrap) return;
        if (!elements.monthPickerWrap.contains(e.target)) closeMonthCalendar();
    });
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') closeMonthCalendar();
    });
}

function toggleMonthCalendar() {
    if (!elements.monthCalPopover) return;
    const open = elements.monthCalPopover.classList.contains('open');
    if (open) closeMonthCalendar();
    else openMonthCalendar();
}

function openMonthCalendar() {
    if (!elements.monthCalPopover) return;
    if (state.selectedMonth && /^\d{4}-\d{2}$/.test(state.selectedMonth)) {
        state.monthCalViewYear = parseInt(state.selectedMonth.slice(0, 4), 10);
    }
    renderMonthCalendarGrid();
    elements.monthCalPopover.classList.add('open');
}

function closeMonthCalendar() {
    if (elements.monthCalPopover) elements.monthCalPopover.classList.remove('open');
}

function renderMonthCalendarGrid() {
    if (!elements.monthCalGrid || !elements.monthCalYear) return;
    const year = state.monthCalViewYear;
    elements.monthCalYear.textContent = String(year);
    const now = new Date();
    const curYm = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`;
    const selected = state.selectedMonth || '';

    elements.monthCalGrid.innerHTML = '';
    for (let m = 1; m <= 12; m++) {
        const ym = `${year}-${String(m).padStart(2, '0')}`;
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.textContent = MONTH_SHORT[m - 1];
        if (ym === selected) btn.classList.add('is-selected');
        if (ym === curYm) btn.classList.add('is-current');
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            setSelectedMonth(ym);
        });
        elements.monthCalGrid.appendChild(btn);
    }
}

function syncMonthPickerDisplay() {
    const value = state.selectedMonth || '';
    if (elements.monthPickerDisplay) {
        elements.monthPickerDisplay.textContent = value
            ? formatYearMonthShort(value)
            : 'All months';
    }
    if (elements.monthPickerWrap) {
        elements.monthPickerWrap.classList.toggle('is-empty', !value);
    }
    if (elements.monthPickerClear) {
        elements.monthPickerClear.style.visibility = value ? 'visible' : 'hidden';
        elements.monthPickerClear.style.pointerEvents = value ? 'auto' : 'none';
    }
    // Keep grid highlight in sync if open
    if (elements.monthCalPopover && elements.monthCalPopover.classList.contains('open')) {
        renderMonthCalendarGrid();
    }
}

function escapeHtml(text) {
    return String(text ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

function renderGstinLookupResult(payload, isError) {
    const el = elements.gstinLookupResult;
    if (!el) return;
    el.classList.add('is-visible');
    el.classList.toggle('is-error', !!isError);
    if (isError) {
        el.innerHTML = `<div class="name">Lookup failed</div><div class="meta">${escapeHtml(payload)}</div>`;
        return;
    }
    if (!payload.found) {
        el.classList.add('is-error');
        el.innerHTML = `<div class="name">No GSTN record</div><div class="meta">${escapeHtml(payload.message || 'No records found')} · ${escapeHtml(payload.gstin || '')}</div>`;
        return;
    }
    const lines = [
        payload.trade_name && payload.trade_name !== payload.legal_name
            ? `Trade: ${payload.trade_name}`
            : null,
        [payload.status, payload.taxpayer_type].filter(Boolean).join(' · '),
        payload.state_jurisdiction ? `Jurisdiction: ${payload.state_jurisdiction}` : null,
        payload.address || null,
    ].filter(Boolean);

    let historyHtml = '';
    const hist = payload.filing_history;
    if (hist) {
        const fy = hist.financial_year || '';
        const filings = Array.isArray(hist.filings) ? hist.filings : [];
        if (filings.length > 0) {
            const rows = filings.slice(0, 24).map((f) => `
                <tr>
                    <td>${escapeHtml(f.return_type)}</td>
                    <td>${escapeHtml(f.period)}</td>
                    <td>${escapeHtml(f.status)}</td>
                    <td>${escapeHtml(f.filed_on)}</td>
                    <td style="font-family:ui-monospace,monospace;font-size:10px;color:#8B9BB0;">${escapeHtml(f.arn || '—')}</td>
                </tr>
            `).join('');
            historyHtml = `
                <div class="meta" style="margin-top:10px;font-weight:700;color:#F0B35A;">Filing history · ${escapeHtml(fy)} · ${filings.length} return(s)</div>
                <table class="gstin-filing-table">
                    <thead><tr><th>Return</th><th>Period</th><th>Status</th><th>Filed on</th><th>ARN</th></tr></thead>
                    <tbody>${rows}</tbody>
                </table>
                ${filings.length > 24 ? `<div class="gstin-filing-note">Showing latest 24 of ${filings.length}.</div>` : ''}
                <div class="gstin-filing-note">Public filing track only (filed / date / ARN). For invoice-level GSTR-2B use <strong>Pull 2B (OTP)</strong>.</div>
            `;
        } else {
            historyHtml = `
                <div class="meta" style="margin-top:10px;">No filings listed for ${escapeHtml(fy)}${hist.message ? ' — ' + escapeHtml(hist.message) : ''}.</div>
                <div class="gstin-filing-note">Try another FY, or use OTP taxpayer APIs for full return / 2B data.</div>
            `;
        }
    }

    el.innerHTML = `
        <div class="name">${escapeHtml(payload.legal_name || 'Registered taxpayer')}</div>
        <div class="meta">${escapeHtml(payload.gstin)}${lines.length ? ' · ' + escapeHtml(lines.join(' · ')) : ''}</div>
        ${historyHtml}
    `;
}

function indianFyOptions(count) {
    const n = count || 3;
    const now = new Date();
    let start = now.getMonth() >= 3 ? now.getFullYear() : now.getFullYear() - 1;
    // Default selection = previous FY (richer history)
    const options = [];
    for (let i = 0; i < n; i++) {
        const s = start - i;
        const label = `FY ${s}-${String(s + 1).slice(-2)}`;
        options.push(label);
    }
    return options;
}

function populateGstinLookupFyOptions() {
    if (!elements.gstinLookupFy) return;
    const opts = indianFyOptions(3);
    elements.gstinLookupFy.innerHTML = opts.map((fy, i) =>
        `<option value="${fy}" ${i === 1 ? 'selected' : ''}>${fy}</option>`
    ).join('');
    // index 1 = previous FY when current is index 0
    state.lookupFy = elements.gstinLookupFy.value;
}

async function lookupGstinPortal(forcedGstin) {
    const raw = (forcedGstin != null
        ? String(forcedGstin)
        : (elements.gstinLookupInput ? elements.gstinLookupInput.value : '')
    ).trim().toUpperCase();
    if (elements.gstinLookupInput && forcedGstin != null) {
        elements.gstinLookupInput.value = raw;
    }
    if (!raw || raw.length !== 15) {
        showToast('Enter a valid 15-character GSTIN.', true);
        return;
    }
    if (!validateGstinString(raw)) {
        showToast('GSTIN format looks invalid.', true);
        return;
    }
    const fy = (elements.gstinLookupFy && elements.gstinLookupFy.value)
        || state.lookupFy
        || indianFyOptions(3)[1];
    if (elements.gstinLookupBtn) {
        elements.gstinLookupBtn.disabled = true;
    }
    try {
        const qs = new URLSearchParams({
            gstin: raw,
            include_history: 'true',
            financial_year: fy,
        });
        const response = await apiFetch(`/api/gstin/lookup?${qs.toString()}`);
        const data = await readJsonOrThrow(response, 'GSTIN lookup failed');
        renderGstinLookupResult(data, false);
        const n = data.filing_history && data.filing_history.count ? data.filing_history.count : 0;
        if (data.found) {
            showToast(n ? `Found ${data.legal_name || data.gstin} · ${n} filing(s)` : `Found: ${data.legal_name || data.gstin}`);
        } else {
            showToast(data.message || 'No GSTN record for this GSTIN', true);
        }
    } catch (err) {
        renderGstinLookupResult(err.message || String(err), true);
        showToast(err.message || 'GSTIN lookup failed', true);
    } finally {
        if (elements.gstinLookupBtn) {
            elements.gstinLookupBtn.disabled = false;
        }
    }
}

function escapeHtmlAttr(text) {
    return String(text ?? '')
        .replace(/&/g, '&amp;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;')
        .replace(/</g, '&lt;');
}

function openCaFormModal({ title, icon, submitLabel, bodyHtml, onSubmit, onOpen }) {
    if (!elements.caFormModal || !elements.caFormModalBody) {
        throw new Error('CA form modal not found in page');
    }
    if (elements.caFormModalTitle) elements.caFormModalTitle.textContent = title || 'Action';
    if (elements.caFormModalIcon) elements.caFormModalIcon.textContent = icon || 'edit';
    if (elements.caFormModalSubmit) elements.caFormModalSubmit.textContent = submitLabel || 'Save';
    elements.caFormModalBody.innerHTML = bodyHtml || '';
    caFormSubmitHandler = onSubmit || null;
    elements.caFormModalOverlay.classList.add('active');
    elements.caFormModal.classList.add('active');
    if (typeof onOpen === 'function') onOpen();
    const firstInput = elements.caFormModalBody.querySelector('input, textarea, select');
    if (firstInput) setTimeout(() => firstInput.focus(), 50);
}

function closeCaFormModal() {
    caFormSubmitHandler = null;
    if (elements.caFormModalOverlay) elements.caFormModalOverlay.classList.remove('active');
    if (elements.caFormModal) elements.caFormModal.classList.remove('active');
    if (elements.gstr2bFileInput) elements.gstr2bFileInput.value = '';
}

function findInvoiceById(invoiceId) {
    return state.invoices.find((inv) => Number(inv.id) === Number(invoiceId));
}

function canQuickAct(inv) {
    if (!inv) return false;
    const rs = inv.review_status || (inv.is_approved ? 'approved' : 'needs_review');
    if (rs === 'approved' || rs === 'rejected' || rs === 'skipped') return false;
    if (inv.is_approved === 1 || inv.is_approved === true) return false;
    return true;
}

async function onEmptyStateActionClick(e) {
    const btn = e.target.closest('[data-empty-action]');
    if (!btn) return;
    e.preventDefault();
    e.stopPropagation();
    const action = btn.getAttribute('data-empty-action');
    if (action === 'import') {
        openImportGstr2bModal();
        return;
    }
    if (action === 'close') {
        if (elements.monthCloseStrip) {
            elements.monthCloseStrip.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
        return;
    }
    if (action === 'month') {
        if (typeof openMonthCalendar === 'function') openMonthCalendar();
        else if (elements.monthPickerWrap) elements.monthPickerWrap.click();
        return;
    }
    if (action === 'client') {
        if (elements.clientSelect) {
            elements.clientSelect.focus();
            elements.clientSelect.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
        return;
    }
    if (action === 'exceptions') {
        const tab = document.querySelector('.tab-btn[data-filter="exceptions"]');
        if (tab) tab.click();
        return;
    }
    if (action === 'all-bills') {
        const tab = document.querySelector('.tab-btn[data-filter="all"]');
        if (tab) tab.click();
        return;
    }
    if (action === 'simulator') {
        window.location.href = '/simulator';
    }
}

async function onInvoiceRowActionClick(e) {
    const moreToggle = e.target.closest('[data-row-more-toggle]');
    if (moreToggle) {
        e.preventDefault();
        e.stopPropagation();
        const wrap = moreToggle.closest('.row-more');
        const wasOpen = wrap && wrap.classList.contains('open');
        document.querySelectorAll('.row-more.open').forEach((el) => el.classList.remove('open'));
        if (wrap && !wasOpen) wrap.classList.add('open');
        return;
    }

    const btn = e.target.closest('[data-row-action]');
    if (!btn) return;
    e.preventDefault();
    e.stopPropagation();
    document.querySelectorAll('.row-more.open').forEach((el) => el.classList.remove('open'));
    const action = btn.getAttribute('data-row-action');
    const invoiceId = Number(btn.getAttribute('data-invoice-id'));
    if (!invoiceId || !action) return;
    const inv = findInvoiceById(invoiceId);
    if (!inv) {
        showToast('Invoice not found in current view.', true);
        return;
    }
    try {
        btn.disabled = true;
        if (action === 'audit') {
            await openInvoiceAudit(invoiceId);
            return;
        }
        if (action === 'approve') {
            await quickApproveInvoice(invoiceId);
            return;
        }
        if (action === 'fix-gstin') {
            openFixSupplierGstinModal(inv);
            return;
        }
        if (action === 'mismatch') {
            openMarkMismatchModal(inv);
            return;
        }
        if (action === 'skip') {
            openSkipInvoiceModal(inv);
            return;
        }
    } catch (err) {
        console.error(err);
        showToast(err.message || 'Action failed', true);
    } finally {
        btn.disabled = false;
    }
}

async function quickApproveInvoice(invoiceId) {
    const response = await apiFetch(`/api/invoices/${invoiceId}/approve`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ca_user: 'CA Admin' }),
    });
    await readJsonOrThrow(response, 'Approval failed');
    showToast(`Invoice #${invoiceId} approved`);
    if (state.currentInvoice && Number(state.currentInvoice.id) === Number(invoiceId)) {
        closeAuditDrawer();
    }
    await refreshQueueAndPilot();
}

function openFixSupplierGstinModal(inv) {
    openCaFormModal({
        title: 'Fix supplier GSTIN',
        icon: 'edit',
        submitLabel: 'Save GSTIN',
        bodyHtml: `
            <p class="text-[13px] text-on-surface-variant">INV-${escapeHtml(inv.invoice_number || inv.id)} · ${escapeHtml(inv.supplier_name || 'Supplier')}</p>
            <div class="ca-form-field">
                <label for="ca-supplier-gstin">Supplier GSTIN</label>
                <input id="ca-supplier-gstin" type="text" maxlength="15" spellcheck="false" autocomplete="off"
                    value="${escapeHtmlAttr(inv.supplier_gstin || '')}" placeholder="15-character GSTIN" />
            </div>
        `,
        onSubmit: async () => {
            const input = document.getElementById('ca-supplier-gstin');
            const gstin = (input && input.value ? input.value : '').trim().toUpperCase();
            if (!gstin || gstin.length !== 15) {
                throw new Error('Enter a 15-character GSTIN.');
            }
            if (!validateGstinString(gstin)) {
                throw new Error('GSTIN format looks invalid.');
            }
            const response = await apiFetch(`/api/invoices/${inv.id}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    ca_user: 'CA Admin',
                    fields: { supplier_gstin: gstin },
                }),
            });
            await readJsonOrThrow(response, 'Failed to update GSTIN');
            closeCaFormModal();
            showToast(`Supplier GSTIN updated to ${gstin}`);
            fetchData();
        },
    });
}

function openMarkMismatchModal(inv) {
    openCaFormModal({
        title: 'Mark 2B mismatch',
        icon: 'difference',
        submitLabel: 'Note mismatch',
        bodyHtml: `
            <p class="text-[13px] text-on-surface-variant">
                Confirms you reviewed the GSTR-2B difference for INV-${escapeHtml(inv.invoice_number || inv.id)}.
                It stops driving exception risk; approve or skip separately if needed.
            </p>
            <div class="ca-form-field">
                <label for="ca-mismatch-note">Note (optional)</label>
                <textarea id="ca-mismatch-note" rows="3" placeholder="e.g. Amount difference accepted — supplier credit note pending"></textarea>
            </div>
        `,
        onSubmit: async () => {
            const noteEl = document.getElementById('ca-mismatch-note');
            const note = (noteEl && noteEl.value ? noteEl.value : '').trim() || 'CA noted GSTR-2B mismatch';
            const response = await apiFetch(`/api/invoices/${inv.id}/acknowledge-mismatch`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ ca_user: 'CA Admin', note }),
            });
            await readJsonOrThrow(response, 'Failed to note mismatch');
            closeCaFormModal();
            showToast('2B mismatch noted');
            fetchData();
        },
    });
}

function openSkipInvoiceModal(inv) {
    openCaFormModal({
        title: 'Skip for now',
        icon: 'schedule',
        submitLabel: 'Skip invoice',
        bodyHtml: `
            <p class="text-[13px] text-on-surface-variant">
                Removes INV-${escapeHtml(inv.invoice_number || inv.id)} from the review queue without approving or rejecting.
            </p>
            <div class="ca-form-field">
                <label for="ca-skip-reason">Reason</label>
                <input id="ca-skip-reason" type="text" value="Skipped by CA — revisit later" />
            </div>
        `,
        onSubmit: async () => {
            const reasonEl = document.getElementById('ca-skip-reason');
            const reason = (reasonEl && reasonEl.value ? reasonEl.value : '').trim() || 'Skipped by CA';
            const response = await apiFetch(`/api/invoices/${inv.id}/skip`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ ca_user: 'CA Admin', reason }),
            });
            await readJsonOrThrow(response, 'Skip failed');
            closeCaFormModal();
            showToast('Invoice skipped from queue');
            if (state.currentInvoice && Number(state.currentInvoice.id) === Number(inv.id)) {
                closeAuditDrawer();
            }
            await refreshQueueAndPilot();
        },
    });
}

async function editClientGstin() {
    if (!state.selectedClientPhone) {
        showToast('Select a client first.', true);
        return;
    }
    const client = getSelectedClient();
    const current = (client && client.gstin) || '';
    openCaFormModal({
        title: 'Client GSTIN',
        icon: 'badge',
        submitLabel: 'Save GSTIN',
        bodyHtml: `
            <p class="text-[13px] text-on-surface-variant">Used to classify purchases (recipient) vs sales (supplier) for ITC. Leave blank to clear.</p>
            <div class="ca-form-field">
                <label for="ca-client-gstin">GSTIN</label>
                <input id="ca-client-gstin" type="text" maxlength="15" spellcheck="false" autocomplete="off"
                    value="${escapeHtmlAttr(current)}" placeholder="15-character GSTIN" />
                <div class="ca-form-hint">Example: 27AAAAA0000A1Z5</div>
            </div>
        `,
        onSubmit: async () => {
            const input = document.getElementById('ca-client-gstin');
            const gstin = (input && input.value ? input.value : '').trim().toUpperCase();
            if (gstin && gstin.length !== 15) {
                throw new Error('GSTIN must be 15 characters (or leave blank to clear).');
            }
            if (gstin && !validateGstinString(gstin)) {
                throw new Error('GSTIN format looks invalid.');
            }
            const response = await apiFetch(`/api/clients/${encodeURIComponent(state.selectedClientPhone)}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ gstin })
            });
            const result = await readJsonOrThrow(response, 'Failed to update GSTIN');
            const updated = result.client;
            const idx = state.clients.findIndex(c => c.phone_number === state.selectedClientPhone);
            if (idx >= 0 && updated) state.clients[idx] = updated;
            const opt = elements.clientSelect.selectedOptions[0];
            if (opt && updated) {
                opt.textContent = `${updated.name} (${updated.gstin || 'No GSTIN'})`;
            }
            updateClientGstinLabel();
            closeCaFormModal();
            showToast(gstin ? `Client GSTIN set to ${gstin}` : 'Client GSTIN cleared');
            fetchData();
        }
    });
}

function openImportGstr2bModal() {
    if (!state.selectedClientPhone) {
        showToast('Select a client first.', true);
        return;
    }
    if (elements.gstr2bFileInput) elements.gstr2bFileInput.value = '';
    const periodGuess = (state.selectedMonth && state.selectedMonth !== 'all')
        ? state.selectedMonth
        : '';
    openCaFormModal({
        title: 'Import GSTR-2B',
        icon: 'compare_arrows',
        submitLabel: 'Import & reconcile',
        bodyHtml: `
            <p class="text-[13px] text-on-surface-variant">Upload a GSTR-2B JSON/CSV and reconcile against purchase invoices.</p>
            <div class="ca-form-field">
                <label for="ca-import-period">Return period</label>
                <input id="ca-import-period" type="month" value="${escapeHtmlAttr(periodGuess)}" />
                <div class="ca-form-hint">Defaults to the dashboard month filter. Clear to infer from the file.</div>
            </div>
            <div class="ca-form-field">
                <label>GSTR-2B file</label>
                <button type="button" id="ca-import-pick-file"
                    class="w-full px-3 py-2 rounded-md text-[13px] font-bold border border-outline-variant text-secondary hover:border-primary/40 transition-colors text-left">
                    Choose JSON or CSV…
                </button>
                <div class="ca-form-hint" id="ca-import-file-name">No file selected</div>
            </div>
        `,
        onOpen: () => {
            const pick = document.getElementById('ca-import-pick-file');
            if (pick && elements.gstr2bFileInput) {
                pick.addEventListener('click', () => elements.gstr2bFileInput.click());
            }
        },
        onSubmit: async () => {
            const file = elements.gstr2bFileInput && elements.gstr2bFileInput.files
                && elements.gstr2bFileInput.files[0];
            if (!file) throw new Error('Choose a GSTR-2B file first.');
            const periodEl = document.getElementById('ca-import-period');
            const period = (periodEl && periodEl.value ? periodEl.value : '').trim();
            await runGstr2bImport(file, period);
            closeCaFormModal();
        }
    });
}

async function runGstr2bImport(file, period) {
    const form = new FormData();
    form.append('file', file);
    form.append('ca_user', 'CA Dashboard');

    let url = `/api/gstr2b/import?client_phone=${encodeURIComponent(state.selectedClientPhone)}&reconcile=true`;
    if (period) {
        url += `&return_period=${encodeURIComponent(period)}`;
    }

    showToast('Importing GSTR-2B…');
    const response = await apiFetch(url, { method: 'POST', body: form });
    const result = await readJsonOrThrow(response, 'GSTR-2B import failed');
    const recon = result.reconcile || {};
    const counts = recon.counts || {};
    showToast(
        `2B imported ${result.imported} · matched ${counts.matched || 0} · ` +
        `unmatched ${counts.unmatched || 0} · mismatch ${counts.mismatch || 0}` +
        (recon.orphan_2b_count ? ` · orphans ${recon.orphan_2b_count}` : '')
    );
    const usedPeriod = (result.return_period || period || state.selectedMonth || '').trim();
    if (usedPeriod && /^\d{4}-\d{2}$/.test(usedPeriod) && state.selectedMonth !== usedPeriod) {
        state.selectedMonth = usedPeriod;
        syncMonthPickerDisplay();
    }
    await fetchData();
    const gaps = (counts.unmatched || 0) + (counts.mismatch || 0);
    if (gaps > 0 && usedPeriod && /^\d{4}-\d{2}$/.test(usedPeriod)) {
        openClientNudgeModal({ returnPeriod: usedPeriod, afterImport: true });
    }
}

async function importGstr2bFile(ev) {
    // Legacy file-input change path (kept unused; import goes through modal)
    const file = ev.target.files && ev.target.files[0];
    ev.target.value = '';
    if (!file) return;
    const periodGuess = (state.selectedMonth && state.selectedMonth !== 'all')
        ? state.selectedMonth
        : '';
    await runGstr2bImport(file, periodGuess);
}

async function recomputeClientItc() {
    if (!state.selectedClientPhone) {
        showToast('Select a client first.', true);
        return;
    }
    const client = getSelectedClient();
    if (!(client && client.gstin)) {
        showToast('Set a valid client GSTIN first (badge next to client).', true);
        return;
    }
    try {
        if (elements.recomputeClientItcBtn) elements.recomputeClientItcBtn.disabled = true;
        showToast('Recomputing ITC for all invoices…');
        const response = await apiFetch(
            `/api/clients/${encodeURIComponent(state.selectedClientPhone)}/recompute-itc`,
            {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ ca_user: 'CA Admin' })
            }
        );
        const result = await readJsonOrThrow(response, 'Bulk ITC recompute failed');
        showToast(`ITC recomputed on ${result.updated || 0} invoices`);
        fetchData();
    } catch (e) {
        console.error(e);
        showToast(e.message || 'Bulk ITC recompute failed', true);
    } finally {
        if (elements.recomputeClientItcBtn) elements.recomputeClientItcBtn.disabled = false;
    }
}

function updateGstSessionChip(status) {
    const chip = elements.gstSessionChip;
    if (!chip) return;
    const session = status && status.session;
    if (session && session.active) {
        chip.style.display = 'inline';
        chip.style.color = '#36D6AE';
        chip.textContent = `Portal: connected (${session.username || 'session'})`;
    } else if (status && status.gst_portal_username) {
        chip.style.display = 'inline';
        chip.style.color = '#F0B35A';
        chip.textContent = 'Portal: OTP needed';
    } else {
        chip.style.display = 'none';
        chip.textContent = '';
    }
}

async function refreshGstSessionStatus() {
    if (!state.selectedClientPhone) {
        updateGstSessionChip(null);
        return null;
    }
    try {
        const response = await apiFetch(
            `/api/clients/${encodeURIComponent(state.selectedClientPhone)}/gst-session`
        );
        const status = await readJsonOrThrow(response, 'Failed to load GST session');
        state.gstSessionStatus = status;
        updateGstSessionChip(status);
        return status;
    } catch (e) {
        console.warn(e);
        updateGstSessionChip(null);
        return null;
    }
}

function formatGstPortalError(message) {
    const msg = String(message || '');
    if (/AUTH4037/i.test(msg) || /API access is not available/i.test(msg)) {
        return (
            'AUTH4037: On gst.gov.in for this username — View Profile → Manage API Access → ' +
            'Enable Yes → Duration 30 days → Confirm. Then retry Pull 2B (OTP).'
        );
    }
    return msg;
}

async function ensureGstPortalSession() {
    let status = await refreshGstSessionStatus();
    if (status && status.session && status.session.active) {
        return status;
    }
    if (!status) {
        throw new Error(
            'Could not reach GST session API. Restart the server (uvicorn) so OTP routes are loaded.'
        );
    }
    if (!status.sandbox_configured) {
        throw new Error('Sandbox API keys not configured in .env');
    }
    const client = getSelectedClient();
    if (!(client && client.gstin)) {
        throw new Error('Set client GSTIN first (Client GSTIN button).');
    }

    const defaultUser = (status && status.gst_portal_username) || '';
    const username = window.prompt(
        'GST portal username (same as gst.gov.in login).\nAPI Access: View Profile → Manage API Access → Yes → 30 days.',
        defaultUser
    );
    if (username === null) throw new Error('Cancelled');
    if (!username.trim()) throw new Error('Username is required');

    showToast('Sending OTP to GST-registered mobile/email…');
    const otpRes = await apiFetch(
        `/api/clients/${encodeURIComponent(state.selectedClientPhone)}/gst-session/otp`,
        {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username: username.trim() }),
        }
    );
    await readJsonOrThrow(otpRes, 'OTP request failed');

    const otp = window.prompt('Enter the OTP received on GST-registered mobile/email:');
    if (otp === null) throw new Error('Cancelled');
    if (!String(otp).trim()) throw new Error('OTP is required');

    showToast('Verifying OTP…');
    const verifyRes = await apiFetch(
        `/api/clients/${encodeURIComponent(state.selectedClientPhone)}/gst-session/verify`,
        {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username: username.trim(), otp: String(otp).trim() }),
        }
    );
    await readJsonOrThrow(verifyRes, 'OTP verification failed');
    showToast('GST portal connected (~6 hours)');
    return refreshGstSessionStatus();
}

async function fetchGstr2bFromPortal() {
    if (!state.selectedClientPhone) {
        showToast('Select a client first.', true);
        return;
    }
    const periodGuess = (state.selectedMonth && state.selectedMonth !== 'all')
        ? state.selectedMonth
        : '';
    const period = window.prompt(
        'Pull live GSTR-2B for return period (YYYY-MM):',
        periodGuess || new Date().toISOString().slice(0, 7)
    );
    if (period === null) return;
    const rp = period.trim();
    if (!/^\d{4}-\d{2}$/.test(rp)) {
        showToast('Period must be YYYY-MM (e.g. 2026-07)', true);
        return;
    }

    if (elements.fetchGstr2bBtn) elements.fetchGstr2bBtn.disabled = true;
    try {
        await ensureGstPortalSession();
        showToast(`Pulling GSTR-2B for ${rp}…`);
        const response = await apiFetch(
            `/api/clients/${encodeURIComponent(state.selectedClientPhone)}/gstr2b/fetch?return_period=${encodeURIComponent(rp)}&reconcile=true`,
            { method: 'POST' }
        );
        const result = await readJsonOrThrow(response, 'Live GSTR-2B pull failed');
        const recon = result.reconcile || {};
        const counts = recon.counts || {};
        showToast(
            `Live 2B ${result.imported} rows · matched ${counts.matched || 0} · ` +
            `unmatched ${counts.unmatched || 0} · mismatch ${counts.mismatch || 0}`
        );
        if (state.selectedMonth !== rp) {
            state.selectedMonth = rp;
            syncMonthPickerDisplay();
        }
        await fetchData();
        const gaps = (counts.unmatched || 0) + (counts.mismatch || 0);
        if (gaps > 0) {
            openClientNudgeModal({ returnPeriod: rp, afterImport: true });
        }
    } catch (e) {
        console.error(e);
        if (e.message && e.message !== 'Cancelled') {
            showToast(formatGstPortalError(e.message) || 'Live GSTR-2B pull failed', true);
        }
        refreshGstSessionStatus();
    } finally {
        if (elements.fetchGstr2bBtn) elements.fetchGstr2bBtn.disabled = false;
    }
}

async function fetchData() {
    if (!state.selectedClientPhone) return;

    try {
        renderTableLoadingState();
        
        // Same period for invoices + metrics (empty = all months)
        const monthParam = state.selectedMonth ? `&month=${state.selectedMonth}` : '';
        const metricsMonth = state.selectedMonth || 'all';
        
        // 1. Fetch Invoices list for client
        const invResponse = await apiFetch(`/api/invoices?client_phone=${state.selectedClientPhone}${monthParam}`);
        const invoices = await readJsonOrThrow(invResponse, 'Failed to load invoices');
        state.invoices = invoices;
        syncMonthPickerDisplay();
        
        // 2. Fetch Metrics for the same period
        const metResponse = await apiFetch(
            `/api/metrics?client_phone=${state.selectedClientPhone}&month=${encodeURIComponent(metricsMonth)}`
        );
        const metrics = await readJsonOrThrow(metResponse, 'Failed to load metrics');
        state.metrics = metrics;

        // 3. Populate widgets
        updateDashboardMetrics(metrics);
        renderItcIssuesList();
        renderInvoiceTable();
        refreshMonthScopeBanner();

        // 4. Render real ITC sparkline chart
        renderITCSparkline();

        // 5. Warm GSTN portal cache for queue risk (async; re-renders when done)
        warmPortalCacheForInvoices();
        refreshGstSessionStatus();
    } catch (e) {
        console.error("Failed to fetch dashboard data:", e);
        showToast(e.message || "Failed to load dashboard metrics.", true);
    }
}

function portalAssessmentMissing(assessment) {
    return !assessment || assessment.found == null;
}

async function warmPortalCacheForInvoices() {
    if (!Array.isArray(state.invoices) || !state.invoices.length) return;
    const parties = [];
    const seen = new Set();
    for (const inv of state.invoices) {
        const pairs = [
            [inv.supplier_gstin, inv.supplier_name, 'supplier', inv.portal_supplier],
            [inv.recipient_gstin, inv.recipient_name, 'recipient', inv.portal_recipient],
        ];
        for (const [gstin, name, role, existing] of pairs) {
            const g = String(gstin || '').trim().toUpperCase();
            if (g.length !== 15 || seen.has(g)) continue;
            if (!portalAssessmentMissing(existing)) continue;
            seen.add(g);
            parties.push({ gstin: g, name: name || '', role });
            if (parties.length >= 10) break;
        }
        if (parties.length >= 10) break;
    }
    if (!parties.length) return;
    try {
        const response = await apiFetch('/api/gstin/cache/warm', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ parties }),
        });
        const data = await readJsonOrThrow(response, 'Portal cache warm failed');
        const map = data.assessments || {};
        let changed = false;
        for (const inv of state.invoices) {
            const sg = String(inv.supplier_gstin || '').trim().toUpperCase();
            const rg = String(inv.recipient_gstin || '').trim().toUpperCase();
            if (sg && map[sg]) {
                inv.portal_supplier = map[sg];
                changed = true;
            }
            if (rg && map[rg]) {
                inv.portal_recipient = map[rg];
                changed = true;
            }
        }
        if (changed) renderInvoiceTable();
    } catch (e) {
        console.warn('Portal cache warm skipped:', e.message || e);
    }
}

async function openInvoiceAudit(invoiceId) {
    try {
        const response = await apiFetch(`/api/invoices/${invoiceId}`);
        const invoice = await readJsonOrThrow(response, 'Failed to load invoice');
        
        state.currentInvoice = invoice;
        state.zoomLevel = 1.0;
        state.rotationAngle = 0;
        
        setDocumentPreview(invoice.file_path);

        // Populate header
        elements.drawerInvoiceNum.textContent = `Audit Invoice #${invoice.invoice_number || 'None'}`;
        elements.drawerInvoiceId.textContent = `Invoice Record DB-ID: ${invoice.id}`;

        // Populate form details
        elements.fInvNum.value = invoice.invoice_number || '';
        elements.fInvDate.value = invoice.invoice_date || '';
        
        elements.fSupName.value = invoice.supplier_name || '';
        elements.fSupGst.value = invoice.supplier_gstin || '';
        validateGstinField(elements.fSupGst, elements.fSupGstVal);

        elements.fRecName.value = invoice.recipient_name || '';
        elements.fRecGst.value = invoice.recipient_gstin || '';
        validateGstinField(elements.fRecGst, elements.fRecGstVal);

        elements.fCategory.value = invoice.business_category || 'Other';
        elements.fSupplyType.value = invoice.supply_type || 'INTRA-STATE';
        elements.fPos.value = invoice.place_of_supply || '';
        
        if (invoice.itc_partial) {
            elements.fItcEligible.value = 'partial';
            toggleItcReasonField(true);
        } else {
            elements.fItcEligible.value = String(!!invoice.is_itc_eligible);
            toggleItcReasonField(!invoice.is_itc_eligible);
        }
        elements.fItcReason.value = invoice.itc_ineligibility_reason || '';

        elements.fTaxable.value = invoice.total_taxable_value || 0;
        elements.fGrand.value = invoice.grand_total || 0;
        elements.fCgst.value = invoice.total_cgst || 0;
        elements.fSgst.value = invoice.total_sgst || 0;
        elements.fIgst.value = invoice.total_igst || 0;

        // Render validation errors (warnings checklist)
        state.supplierPortalCheck = null;
        state.recipientPortalCheck = null;
        renderPrimaryIssue(invoice);
        renderAuditAlerts(invoice);
        renderLineItcBreakdown(invoice);

        // Render change history timeline log
        renderAuditLogs(invoice.ca_action_logs);
        resetDrawerFolds(invoice);

        // Slide drawer open
        elements.auditDrawer.classList.add('active');
        elements.auditDrawerOverlay.classList.add('active');
        updateDrawerNavPos();

        // Async GSTN portal checks (public search; uses 24h cache)
        verifyPartyOnPortal('supplier', invoice.supplier_gstin, invoice.supplier_name, false);
        verifyPartyOnPortal('recipient', invoice.recipient_gstin, invoice.recipient_name, false);
    } catch (e) {
        console.error("Failed to load invoice details:", e);
        showToast(e.message || "Error loading invoice audit details.", true);
    }
}

async function saveInvoiceProgress() {
    if (!state.currentInvoice) return;
    
    const fields = getFormFieldsData();
    try {
        const response = await apiFetch(`/api/invoices/${state.currentInvoice.id}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                ca_user: "CA Admin",
                fields: fields
            })
        });
        const result = await readJsonOrThrow(response, 'Failed to save invoice');
        
        if (result.status === "success") {
            showToast("Invoice fields saved successfully.");
            // Reload details inside drawer to reflect override timeline logs
            openInvoiceAudit(state.currentInvoice.id);
            // Refresh main window registers
            fetchData();
        } else {
            showToast(formatApiDetail(result, 'Save failed'), true);
        }
    } catch (e) {
        console.error("Failed to save progress:", e);
        showToast(e.message || "Error saving invoice modifications.", true);
    }
}

async function verifyAndApproveInvoice() {
    if (!state.currentInvoice) return;

    // Save fields progress first
    const fields = getFormFieldsData();
    try {
        // Save
        const saveResponse = await apiFetch(`/api/invoices/${state.currentInvoice.id}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                ca_user: "CA Admin",
                fields: fields
            })
        });
        await readJsonOrThrow(saveResponse, 'Failed to save before approve');

        // Approve
        const approveResponse = await apiFetch(`/api/invoices/${state.currentInvoice.id}/approve`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ca_user: "CA Admin" })
        });
        const result = await readJsonOrThrow(approveResponse, 'Approval failed');

        if (result.status === "success") {
            showToast("Invoice approved & finalized for GSTR compilation!");
            const ids = state.filteredInvoiceIds || [];
            const idx = ids.indexOf(Number(state.currentInvoice.id));
            const nextId = idx >= 0 && idx + 1 < ids.length ? ids[idx + 1] : null;
            await refreshQueueAndPilot();
            if (nextId && (state.filteredInvoiceIds || []).includes(nextId)) {
                await openInvoiceAudit(nextId);
            } else {
                closeAuditDrawer();
            }
        } else {
            showToast(formatApiDetail(result, 'Approval failed'), true);
        }
    } catch (e) {
        console.error("Failed to approve invoice:", e);
        showToast(e.message || "Verification approval failed.", true);
    }
}

async function rejectInvoiceHitl() {
    if (!state.currentInvoice) return;
    openCaFormModal({
        title: 'Reject invoice',
        icon: 'cancel',
        submitLabel: 'Reject',
        bodyHtml: `
            <p class="text-[13px] text-on-surface-variant">Reason is stored in the audit trail.</p>
            <div class="ca-form-field">
                <label for="ca-reject-reason">Reason</label>
                <input id="ca-reject-reason" type="text" value="Needs clearer invoice / incorrect extraction" />
            </div>
        `,
        onSubmit: async () => {
            const reasonEl = document.getElementById('ca-reject-reason');
            const reason = (reasonEl && reasonEl.value ? reasonEl.value : '').trim();
            if (!reason) throw new Error('Reject reason is required.');
            const response = await apiFetch(`/api/invoices/${state.currentInvoice.id}/reject`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ ca_user: 'CA Admin', reason }),
            });
            await readJsonOrThrow(response, 'Reject failed');
            closeCaFormModal();
            showToast('Invoice rejected — removed from filing queue.');
            closeAuditDrawer();
            await refreshQueueAndPilot();
        },
    });
}

async function recomputeInvoiceItc() {
    if (!state.currentInvoice) return;
    try {
        if (elements.recomputeItcBtn) {
            elements.recomputeItcBtn.disabled = true;
        }
        showToast('Recomputing ITC under Sec 17(5)…');
        const response = await apiFetch(`/api/invoices/${state.currentInvoice.id}/recompute-itc`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ca_user: 'CA Admin' })
        });
        const result = await readJsonOrThrow(response, 'ITC recompute failed');
        const eligible = result.is_itc_eligible;
        showToast(eligible
            ? 'ITC marked eligible (rules / review).'
            : `ITC blocked: ${result.itc_ineligibility_reason || 'Sec 17(5)'}`);
        await openInvoiceAudit(state.currentInvoice.id);
        fetchData();
    } catch (e) {
        console.error('ITC recompute failed:', e);
        showToast(e.message || 'ITC recompute failed.', true);
    } finally {
        if (elements.recomputeItcBtn) {
            elements.recomputeItcBtn.disabled = false;
        }
    }
}

// ── GSTR Compiling ──

async function openGstrModal() {
    if (!state.selectedClientPhone) {
        showToast("Select a client first.", true);
        return;
    }

    // Filing export needs a concrete YYYY-MM (fallback to current month)
    elements.modalGstrMonth.value =
        state.selectedMonth || new Date().toISOString().slice(0, 7);
    await updateGstrModalPreviews();

    elements.gstrModal.classList.add('active');
    elements.gstrModalOverlay.classList.add('active');
}

function closeGstrModal() {
    elements.gstrModal.classList.remove('active');
    elements.gstrModalOverlay.classList.remove('active');
}

async function updateGstrModalPreviews() {
    const month = elements.modalGstrMonth.value;
    try {
        const response = await apiFetch(`/api/metrics?client_phone=${state.selectedClientPhone}&month=${month}`);
        const m = await readJsonOrThrow(response, 'Failed to load GSTR preview');

        elements.prevSales.textContent = formatCurrency(m.sales_taxable);
        elements.prevTax.textContent = formatCurrency(m.sales_gst_liability);
        elements.prevItc.textContent = formatCurrency(m.itc_claimed);

        // New: blocked ITC and net payable
        if (elements.prevItcBlocked) elements.prevItcBlocked.textContent = formatCurrency(m.itc_blocked || 0);
        if (elements.prevNetGst)     elements.prevNetGst.textContent = formatCurrency(m.net_gst_payable ?? 0);
    } catch (e) {
        console.error("Error updating preview aggregates:", e);
        showToast(e.message || "Failed to load GSTR preview.", true);
    }
}

async function compileGstrDownload() {
    const month = elements.modalGstrMonth.value;
    const type = elements.modalGstrType.value;
    
    try {
        showToast("Compiling returns offline utility file...");
        const response = await apiFetch(`/api/gstr/export?client_phone=${state.selectedClientPhone}&month=${month}&type=${type}`);
        const data = await readJsonOrThrow(response, 'GSTR export failed');
        
        // Trigger client browser download of JSON file
        const dataStr = "data:text/json;charset=utf-8," + encodeURIComponent(JSON.stringify(data, null, 2));
        const downloadAnchor = document.createElement('a');
        downloadAnchor.setAttribute("href", dataStr);
        downloadAnchor.setAttribute("download", `${type}_${state.selectedClientPhone}_${month.replace('-', '')}.json`);
        document.body.appendChild(downloadAnchor);
        downloadAnchor.click();
        downloadAnchor.remove();

        // Track export on Filing panel (same client/month)
        if (state.selectedClientPhone && /^\d{4}-\d{2}$/.test(month)) {
            const prevMonth = state.selectedMonth;
            // Persist against the exported month key
            const keyPhone = state.selectedClientPhone;
            const keyMonth = month;
            const st = loadGstFilingStatusFor(keyPhone, keyMonth);
            if (type === 'GSTR1') st.gstr1_exported = true;
            if (type === 'GSTR3B') st.gstr3b_exported = true;
            st.exported_at = new Date().toISOString();
            saveGstFilingStatusFor(keyPhone, keyMonth, st);
            if (prevMonth === keyMonth || state.selectedMonth === keyMonth) {
                renderGstFilingPanel(state._lastMetrics || {});
            }
        }
        
        showToast("Utility JSON downloaded — continue on gst.gov.in");
        closeGstrModal();
    } catch (e) {
        console.error("GSTR Export failed:", e);
        showToast(e.message || "Filing compilation error.", true);
    }
}

// ── Reload data (GST portal sync not wired yet) ──
function triggerSync() {
    elements.syncBtn.classList.add('loading');
    elements.syncBtn.disabled = true;
    showToast("Reloading dashboard from database…");

    Promise.all([fetchData(), fetchPilotStats()]).finally(() => {
        elements.syncBtn.classList.remove('loading');
        elements.syncBtn.disabled = false;
        showToast("Dashboard data refreshed.");
    });
}

function formatPilotPct(rate) {
    if (rate == null || Number.isNaN(Number(rate))) return '—';
    return `${Math.round(Number(rate) * 1000) / 10}%`;
}

function renderPilotStats(payload) {
    if (!elements.pilotPanel || !payload) return;
    const pilot = payload.pilot || {};
    const approved = payload.approved_invoices || 0;
    const minA = pilot.min_approvals || 25;
    const status = pilot.status || 'collecting';

    if (elements.pilotStatusPill) {
        elements.pilotStatusPill.className = `pilot-status-pill ${status}`;
        elements.pilotStatusPill.textContent = pilot.status_label || status;
    }
    if (elements.pilotStatusHint) {
        elements.pilotStatusHint.textContent = pilot.status_hint || '';
    }
    if (elements.pilotProgressLabel) {
        elements.pilotProgressLabel.textContent = `${approved} / ${minA}`;
    }
    if (elements.pilotProgressFill) {
        elements.pilotProgressFill.style.width = `${Math.min(100, Number(pilot.progress_pct) || 0)}%`;
    }
    if (elements.pilotApproved) elements.pilotApproved.textContent = String(approved);
    if (elements.pilotApprovedSub) {
        const left = pilot.approvals_remaining;
        elements.pilotApprovedSub.textContent =
            left > 0 ? `${left} more to volume bar` : 'volume bar met';
    }

    const showRate = !!pilot.show_edit_rate;
    if (elements.pilotEditRateCard) {
        elements.pilotEditRateCard.classList.toggle('pilot-muted', !showRate);
    }
    if (elements.pilotEditRate) {
        elements.pilotEditRate.textContent = showRate
            ? formatPilotPct(payload.edit_rate)
            : 'locked';
    }
    if (elements.pilotEditRateSub) {
        elements.pilotEditRateSub.textContent = showRate
            ? `hold if ≥ ${formatPilotPct(pilot.edit_rate_hold_threshold)}`
            : `% you changed on extract · after ${minA}`;
    }

    const rejected = pilot.rejected_invoices || 0;
    const skipped = pilot.skipped_invoices || 0;
    if (elements.pilotRejectSkip) {
        elements.pilotRejectSkip.textContent = `${rejected} / ${skipped}`;
    }
    if (elements.pilotRejectSkipSub) {
        elements.pilotRejectSkipSub.textContent = 'rejected / skipped';
    }
    if (elements.pilotPending) {
        elements.pilotPending.textContent = String(pilot.pending_review ?? '—');
    }
    if (elements.pilotPendingSub) {
        const pending = Number(pilot.pending_review) || 0;
        elements.pilotPendingSub.textContent = pending
            ? 'click → review next'
            : 'queue clear';
    }

    if (elements.pilotFieldBody) {
        const rows = pilot.filing_critical || [];
        if (!rows.length) {
            elements.pilotFieldBody.innerHTML =
                '<tr><td colspan="4" class="text-on-surface-variant">No filing-critical edits in this window.</td></tr>';
        } else {
            elements.pilotFieldBody.innerHTML = rows.map((row) => {
                const rateLabel = showRate ? formatPilotPct(row.rate) : '—';
                const signal = !showRate
                    ? '<span class="text-on-surface-variant">wait for volume</span>'
                    : (row.hold
                        ? '<span style="color:#FF8A8A;font-weight:700;">HOLD</span>'
                        : '<span style="color:#36D6AE;font-weight:700;">OK</span>');
                return `<tr>
                    <td class="font-semibold text-on-surface">${row.field_name}</td>
                    <td>${row.invoices}</td>
                    <td>${rateLabel}</td>
                    <td>${signal}</td>
                </tr>`;
            }).join('');
        }
    }
}

async function fetchPilotStats() {
    if (!elements.pilotPanel) return null;
    const days = state.pilotDays || 30;
    try {
        const response = await apiFetch(`/api/pilot/stats?days=${days}`);
        if (response.status === 401 || response.status === 403) {
            if (elements.pilotStatusHint) {
                elements.pilotStatusHint.textContent =
                    'Sign in as CA (or admin API key) to load pilot KPIs.';
            }
            return null;
        }
        const payload = await readJsonOrThrow(response, 'Failed to load pilot stats');
        renderPilotStats(payload);
        return payload;
    } catch (err) {
        console.error('Pilot stats failed:', err);
        if (elements.pilotStatusHint) {
            elements.pilotStatusHint.textContent = err.message || 'Could not load pilot stats';
        }
        throw err;
    }
}

async function refreshQueueAndPilot() {
    await Promise.all([
        fetchData(),
        fetchPilotStats().catch((err) => console.error(err)),
    ]);
}

const PILOT_PREFLIGHT_KEY = 'PILOT_PREFLIGHT_V1';

function loadPilotPreflight() {
    try {
        return JSON.parse(localStorage.getItem(PILOT_PREFLIGHT_KEY) || '{}') || {};
    } catch (_) {
        return {};
    }
}

function savePilotPreflight(map) {
    try {
        localStorage.setItem(PILOT_PREFLIGHT_KEY, JSON.stringify(map || {}));
    } catch (_) { /* ignore */ }
}

function updatePilotPreflightCount() {
    const boxes = document.querySelectorAll('[data-pilot-check]');
    let done = 0;
    boxes.forEach((el) => { if (el.checked) done += 1; });
    if (elements.pilotPreflightCount) {
        elements.pilotPreflightCount.textContent = `${done}/${boxes.length || 6}`;
    }
}

function setupPilotPreflightChecklist() {
    const saved = loadPilotPreflight();
    document.querySelectorAll('[data-pilot-check]').forEach((el) => {
        const key = el.getAttribute('data-pilot-check');
        el.checked = !!saved[key];
        el.addEventListener('change', () => {
            const map = loadPilotPreflight();
            map[key] = !!el.checked;
            savePilotPreflight(map);
            updatePilotPreflightCount();
        });
    });
    updatePilotPreflightCount();
}

function markPilotPreflight(key, value = true) {
    const el = document.querySelector(`[data-pilot-check="${key}"]`);
    if (el) {
        el.checked = !!value;
        el.dispatchEvent(new Event('change'));
    } else {
        const map = loadPilotPreflight();
        map[key] = !!value;
        savePilotPreflight(map);
        updatePilotPreflightCount();
    }
}

function updatePilotAdminControls() {
    if (!elements.pilotResetStatsBtn) return;
    const admin = !!state.isAdminKey || !!(localStorage.getItem('DASHBOARD_API_KEY') || '').trim();
    elements.pilotResetStatsBtn.style.display = admin ? 'inline-flex' : 'none';
}

async function resetPilotSmokeStats() {
    const ok = window.confirm(
        'Reset smoke edit-rate stats?\n\n'
        + 'Deletes extraction_field_edits + invoice_extraction_outcomes.\n'
        + 'Does NOT delete invoices.\n\n'
        + 'Only do this on pilot day zero before real firm traffic.'
    );
    if (!ok) return;
    const response = await apiFetch('/api/admin/extraction-edit-stats/reset', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ confirm: 'RESET_EDIT_STATS' }),
    });
    const data = await readJsonOrThrow(response, 'Reset failed (admin API key required)');
    markPilotPreflight('reset', true);
    showToast(
        `Cleared ${data.deleted_outcomes || 0} outcomes · ${data.deleted_edit_events || 0} edit events`
    );
    await fetchPilotStats().catch(() => null);
}

function invoiceYearMonth(inv) {
    const raw = String((inv && inv.invoice_date) || '').trim();
    const ym = raw.slice(0, 7);
    return /^\d{4}-\d{2}$/.test(ym) ? ym : '';
}

/** Firm-wide pending → switch client/month → open easiest bill first. */
async function reviewNextPilotPending() {
    if (elements.pilotReviewNextBtn) elements.pilotReviewNextBtn.disabled = true;
    try {
        showToast('Finding next pending bill…');
        const response = await apiFetch('/api/invoices?status=pending_review');
        const invoices = await readJsonOrThrow(response, 'Failed to load pending invoices');
        const list = Array.isArray(invoices) ? invoices : [];
        if (!list.length) {
            showToast('No pending bills — send WhatsApp invoices or wait for client uploads.', true);
            await fetchPilotStats().catch(() => null);
            return;
        }
        const sorted = [...list].sort((a, b) => {
            const ra = isReadyToApprove(a) ? 1 : 0;
            const rb = isReadyToApprove(b) ? 1 : 0;
            if (ra !== rb) return rb - ra;
            const d = scoreExceptionRisk(a) - scoreExceptionRisk(b); // easier first among hard
            return d !== 0 ? d : (Number(b.id) || 0) - (Number(a.id) || 0);
        });
        const inv = sorted[0];
        const phone = (inv.client_phone || '').trim();
        if (!phone) {
            showToast('Pending invoice missing client phone', true);
            return;
        }

        // Ensure GST module + client context
        if (typeof setModule === 'function') setModule('gst');
        state.selectedClientPhone = phone;
        if (elements.clientSelect) elements.clientSelect.value = phone;
        invalidateClientInvoiceCache();
        updateClientGstinLabel();
        refreshGstSessionStatus();

        let ym = invoiceYearMonth(inv);
        if (!ym) {
            ym = await resolveMonthForClient(phone, { preferSaved: true });
        }
        state.selectedMonth = ym || '';
        if (phone && /^\d{4}-\d{2}$/.test(state.selectedMonth)) {
            saveClientMonth(phone, state.selectedMonth);
        }
        saveDashContext({ phone, month: state.selectedMonth || '' });
        if (typeof syncMonthPickerDisplay === 'function') syncMonthPickerDisplay();
        if (typeof renderMonthCalendarGrid === 'function') {
            try { renderMonthCalendarGrid(); } catch (_) { /* ignore */ }
        }

        await fetchData();
        jumpToAuditQueue();
        await openInvoiceAudit(inv.id);
        const ready = isReadyToApprove(inv);
        showToast(ready
            ? `Opened ready bill #${inv.id} — approve to count toward 25`
            : `Opened bill #${inv.id} — review flags then approve`);
        await fetchPilotStats().catch(() => null);
    } finally {
        if (elements.pilotReviewNextBtn) elements.pilotReviewNextBtn.disabled = false;
    }
}

// ── Local UI Rendering & Helpers ──

function setupAdvancedToolsToggle() {
    const btn = elements.toggleAdvancedTools;
    const panel = elements.advancedToolsPanel;
    if (!btn || !panel) return;

    const setOpen = (open) => {
        panel.classList.toggle('is-open', open);
        if (open) panel.removeAttribute('hidden');
        else panel.setAttribute('hidden', '');
        btn.setAttribute('aria-expanded', open ? 'true' : 'false');
        if (elements.advancedToolsChevron) {
            elements.advancedToolsChevron.textContent = open ? 'expand_less' : 'expand_more';
        }
        try {
            localStorage.setItem('DASHBOARD_ADVANCED_OPEN', open ? '1' : '0');
        } catch (_) { /* ignore */ }
    };

    let preferOpen = false;
    try {
        preferOpen = localStorage.getItem('DASHBOARD_ADVANCED_OPEN') === '1';
    } catch (_) { /* ignore */ }
    setOpen(preferOpen);

    btn.addEventListener('click', () => {
        const open = btn.getAttribute('aria-expanded') === 'true';
        setOpen(!open);
    });
}

/** Highlight Review → Import 2B → Close month based on current blockers. */
function updateWorkflowGuide({ needCa, imported, ready, scopeAll, bills }) {
    const steps = {
        review: elements.wfStepReview,
        import: elements.wfStepImport,
        close: elements.wfStepClose,
    };
    Object.values(steps).forEach((el) => {
        if (!el) return;
        el.classList.remove('is-active', 'is-done');
    });

    const reviewDone = !scopeAll && needCa === 0 && bills > 0;
    const importDone = !scopeAll && !!imported;
    let active = 'review';
    let hint = 'Start with the queue';

    if (scopeAll) {
        active = 'close';
        hint = 'Pick a month, then follow the path';
    } else if (bills === 0) {
        active = 'review';
        hint = 'Waiting for WhatsApp bills';
    } else if (needCa > 0) {
        active = 'review';
        hint = `${needCa} still need a CA check`;
    } else if (!imported) {
        active = 'import';
        hint = 'Import GSTR-2B for this month';
    } else if (!ready) {
        active = 'close';
        hint = 'Finish month-close blockers';
    } else {
        active = null;
        hint = 'Month ready — export draft when you are';
    }

    if (reviewDone && steps.review) steps.review.classList.add('is-done');
    if (importDone && steps.import) steps.import.classList.add('is-done');
    if (ready && !scopeAll && steps.close) steps.close.classList.add('is-done');

    if (active && steps[active]) steps[active].classList.add('is-active');
    if (elements.workflowHint) elements.workflowHint.textContent = hint;
}

function updateMonthCloseStrip(metrics) {
    if (!elements.monthCloseStrip) return;
    const m = metrics || {};
    const periodLabel = formatYearMonthLabel(state.selectedMonth);
    const scopeAll = !state.selectedMonth || state.selectedMonth === 'all';

    // Prefer API needs_ca; fall back to local exception filter
    let needCa = m.needs_ca_count;
    if (typeof needCa !== 'number') {
        needCa = (state.invoices || []).filter(isExceptionInvoice).length;
    }
    const bills = m.invoice_count ?? (state.invoices || []).length;
    const matched = m.gstr2b_matched_count || 0;
    const missing = m.gstr2b_unmatched_count || 0;
    const mismatch = m.gstr2b_mismatch_count || 0;
    const imported = !!m.gstr2b_imported;
    const entryCount = m.gstr2b_entry_count || 0;

    if (elements.monthClosePeriod) {
        elements.monthClosePeriod.textContent = scopeAll
            ? 'Select a month to close'
            : periodLabel;
    }
    if (elements.monthCloseBills) elements.monthCloseBills.textContent = String(bills);
    if (elements.monthCloseNeedCa) {
        elements.monthCloseNeedCa.textContent = String(needCa);
        elements.monthCloseNeedCa.className = 'val' + (needCa > 0 ? ' is-warn' : ' is-ok');
    }
    if (elements.monthClose2bImported) {
        if (scopeAll) {
            elements.monthClose2bImported.textContent = '—';
            elements.monthClose2bImported.className = 'val';
        } else if (imported) {
            elements.monthClose2bImported.textContent = entryCount > 0 ? `Yes (${entryCount})` : 'Yes';
            elements.monthClose2bImported.className = 'val is-ok';
        } else {
            elements.monthClose2bImported.textContent = 'No';
            elements.monthClose2bImported.className = 'val is-warn';
        }
    }
    if (elements.monthClose2bMatched) elements.monthClose2bMatched.textContent = String(matched);
    if (elements.monthClose2bMissing) {
        elements.monthClose2bMissing.textContent = String(missing);
        elements.monthClose2bMissing.className = 'val' + (missing > 0 ? ' is-warn' : '');
    }
    if (elements.monthClose2bMismatch) {
        elements.monthClose2bMismatch.textContent = String(mismatch);
        elements.monthClose2bMismatch.className = 'val' + (mismatch > 0 ? ' is-bad' : '');
    }

    let blockers = Array.isArray(m.month_close_blockers) ? [...m.month_close_blockers] : null;
    if (!blockers) {
        blockers = [];
        if (!m.has_gstin) blockers.push('Set a valid client GSTIN');
        if (scopeAll) blockers.push('Pick a single month to close');
        else if (bills === 0) blockers.push('No bills in this month');
        if (needCa > 0) blockers.push(`${needCa} still need CA`);
        if (!scopeAll && !imported) blockers.push('Import GSTR-2B for this month');
        else if (!scopeAll && imported) {
            if (missing > 0) blockers.push(`${missing} missing in 2B`);
            if (mismatch > 0) blockers.push(`${mismatch} 2B mismatch`);
        }
    }

    const ready = blockers.length === 0;
    if (elements.monthCloseReadyBadge) {
        elements.monthCloseReadyBadge.classList.remove('is-ready', 'is-blocked', 'is-pending');
        if (scopeAll) {
            elements.monthCloseReadyBadge.classList.add('is-pending');
            elements.monthCloseReadyBadge.textContent = 'Pick month';
        } else if (ready) {
            elements.monthCloseReadyBadge.classList.add('is-ready');
            elements.monthCloseReadyBadge.textContent = 'Ready';
        } else {
            elements.monthCloseReadyBadge.classList.add('is-blocked');
            elements.monthCloseReadyBadge.textContent = 'Not ready';
        }
    }
    if (elements.monthCloseBlockers) {
        elements.monthCloseBlockers.textContent = ready
            ? (scopeAll ? '' : 'All clear for this month — CA exceptions closed and 2B checked.')
            : blockers.join(' · ');
    }

    // Guided checklist: bills → 2B → queue → export
    const stepBills = document.querySelector('.month-close-step[data-step="bills"]');
    const step2b = document.querySelector('.month-close-step[data-step="gstr2b"]');
    const stepQueue = document.querySelector('.month-close-step[data-step="queue"]');
    const stepExport = document.querySelector('.month-close-step[data-step="export"]');
    const setStep = (el, mode) => {
        if (!el) return;
        el.classList.remove('is-done', 'is-active');
        if (mode) el.classList.add(mode);
    };
    const billsDone = !scopeAll && bills > 0;
    const twoBDone = !scopeAll && imported;
    const queueDone = !scopeAll && needCa === 0 && billsDone;
    const exportDone = ready;

    setStep(stepBills, scopeAll ? null : (billsDone ? 'is-done' : 'is-active'));
    setStep(step2b, scopeAll ? null : (twoBDone ? 'is-done' : (billsDone ? 'is-active' : null)));
    setStep(stepQueue, scopeAll ? null : (queueDone ? 'is-done' : (twoBDone ? 'is-active' : null)));
    setStep(stepExport, scopeAll ? null : (exportDone ? 'is-done' : (queueDone ? 'is-active' : null)));

    if (elements.stepBillsSub) {
        elements.stepBillsSub.textContent = scopeAll
            ? 'Pick a month first'
            : (billsDone ? `${bills} bill(s) received` : 'Waiting for WhatsApp uploads');
    }
    if (elements.step2bSub) {
        elements.step2bSub.textContent = scopeAll
            ? '—'
            : (twoBDone
                ? `${matched} matched · ${missing} missing · ${mismatch} mismatch`
                : 'Import GSTR-2B JSON/CSV');
    }
    if (elements.stepQueueSub) {
        elements.stepQueueSub.textContent = scopeAll
            ? '—'
            : (queueDone ? 'No CA exceptions left' : `${needCa} still need CA`);
    }
    if (elements.stepExportSub) {
        elements.stepExportSub.textContent = ready
            ? 'Ready to export GSTR draft'
            : 'Finish blockers first (or export anyway)';
    }

    if (elements.monthCloseReviewBtn) {
        elements.monthCloseReviewBtn.disabled = scopeAll;
    }
    if (elements.monthCloseImportBtn) {
        elements.monthCloseImportBtn.disabled = scopeAll || !state.selectedClientPhone;
    }
    if (elements.monthCloseNudgeBtn) {
        elements.monthCloseNudgeBtn.disabled = scopeAll || !state.selectedClientPhone;
    }
    if (elements.monthCloseExportBtn) {
        elements.monthCloseExportBtn.disabled = !state.selectedClientPhone;
    }

    updateWorkflowGuide({ needCa, imported, ready, scopeAll, bills });
    renderGstFilingPanel({
        ...m,
        needs_ca_count: needCa,
        invoice_count: bills,
        gstr2b_matched_count: matched,
        gstr2b_unmatched_count: missing,
        gstr2b_mismatch_count: mismatch,
        gstr2b_imported: imported,
        month_close_blockers: blockers,
        month_close_ready: ready,
    });
}

function gstFilingStatusKey(phone, month) {
    return `GST_FILING_STATUS:${phone || ''}:${month || ''}`;
}

function loadGstFilingStatusFor(phone, month) {
    try {
        return JSON.parse(localStorage.getItem(gstFilingStatusKey(phone, month)) || '{}') || {};
    } catch (_) {
        return {};
    }
}

function saveGstFilingStatusFor(phone, month, st) {
    try {
        localStorage.setItem(gstFilingStatusKey(phone, month), JSON.stringify(st || {}));
    } catch (_) { /* ignore */ }
}

function loadGstFilingStatus() {
    const month = (state.selectedMonth && state.selectedMonth !== 'all') ? state.selectedMonth : '';
    return loadGstFilingStatusFor(state.selectedClientPhone || '', month);
}

function saveGstFilingStatus(st) {
    const month = (state.selectedMonth && state.selectedMonth !== 'all') ? state.selectedMonth : '';
    saveGstFilingStatusFor(state.selectedClientPhone || '', month, st);
}

function jumpToGstFiling() {
    setSidebarNavActive('gstr');
    if (typeof setModule === 'function') setModule('gst');
    const panel = elements.gstFilingPanel || document.getElementById('month-close-strip');
    if (panel) {
        panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
        panel.classList.add('ring-pulse');
        setTimeout(() => panel.classList.remove('ring-pulse'), 1200);
    }
}

function openGstFilingExport(type) {
    if (!state.selectedClientPhone) {
        showToast('Select a client first.', true);
        return;
    }
    if (state.selectedMonth && state.selectedMonth !== 'all' && elements.modalGstrMonth) {
        elements.modalGstrMonth.value = state.selectedMonth;
    }
    if (elements.modalGstrType) {
        elements.modalGstrType.value = type === 'GSTR3B' ? 'GSTR3B' : 'GSTR1';
    }
    openGstrModal();
}

function renderGstFilingPanel(metrics) {
    if (!elements.gstFilingPanel) return;
    const m = metrics || {};
    state._lastMetrics = m;
    const scopeAll = !state.selectedMonth || state.selectedMonth === 'all';
    const periodLabel = formatYearMonthLabel(state.selectedMonth);
    const needCa = typeof m.needs_ca_count === 'number'
        ? m.needs_ca_count
        : (state.invoices || []).filter(isExceptionInvoice).length;
    const bills = m.invoice_count ?? (state.invoices || []).length;
    const missing = m.gstr2b_unmatched_count || 0;
    const mismatch = m.gstr2b_mismatch_count || 0;
    const imported = !!m.gstr2b_imported;
    const blockers = Array.isArray(m.month_close_blockers) ? m.month_close_blockers : [];
    const ready = !!m.month_close_ready || (blockers.length === 0 && !scopeAll && !!state.selectedClientPhone);

    if (elements.gstFilingPeriod) {
        elements.gstFilingPeriod.textContent = !state.selectedClientPhone
            ? 'Select a client'
            : (scopeAll ? 'Pick a single month to file' : `Period: ${periodLabel}`);
    }

    const st = loadGstFilingStatus();
    const marked = !!st.marked_prepared;
    if (elements.gstFilingReadyBadge) {
        elements.gstFilingReadyBadge.classList.remove('is-ready', 'is-blocked', 'is-pending', 'is-filed');
        if (!state.selectedClientPhone || scopeAll) {
            elements.gstFilingReadyBadge.classList.add('is-pending');
            elements.gstFilingReadyBadge.textContent = scopeAll ? 'Pick month' : 'Pick client';
        } else if (marked) {
            elements.gstFilingReadyBadge.classList.add('is-filed');
            elements.gstFilingReadyBadge.textContent = 'Marked on portal';
        } else if (ready) {
            elements.gstFilingReadyBadge.classList.add('is-ready');
            elements.gstFilingReadyBadge.textContent = 'Ready to export';
        } else {
            elements.gstFilingReadyBadge.classList.add('is-blocked');
            elements.gstFilingReadyBadge.textContent = 'Not ready';
        }
    }

    if (elements.gstFilingStatBills) elements.gstFilingStatBills.textContent = String(bills);
    if (elements.gstFilingStatNeedCa) {
        elements.gstFilingStatNeedCa.textContent = String(needCa);
        elements.gstFilingStatNeedCa.style.color = needCa > 0 ? '#A85C10' : '#33604A';
    }
    if (elements.gstFilingStat2b) {
        elements.gstFilingStat2b.textContent = scopeAll ? '—' : (imported ? 'Yes' : 'No');
    }
    if (elements.gstFilingStatGaps) {
        const gaps = missing + mismatch;
        elements.gstFilingStatGaps.textContent = scopeAll ? '—' : String(gaps);
        elements.gstFilingStatGaps.style.color = gaps > 0 ? '#A85C10' : '#33604A';
    }
    if (elements.gstFilingBlockers) {
        if (!state.selectedClientPhone) {
            elements.gstFilingBlockers.textContent = 'Select a client to prepare filing.';
            elements.gstFilingBlockers.classList.remove('is-ok');
        } else if (scopeAll) {
            elements.gstFilingBlockers.textContent = 'Pick a single return month in the header.';
            elements.gstFilingBlockers.classList.remove('is-ok');
        } else if (ready) {
            elements.gstFilingBlockers.textContent = 'All clear — export drafts, then file on gst.gov.in.';
            elements.gstFilingBlockers.classList.add('is-ok');
        } else {
            elements.gstFilingBlockers.textContent = blockers.join(' · ') || 'Finish month-close blockers first.';
            elements.gstFilingBlockers.classList.remove('is-ok');
        }
    }

    const canExport = !!state.selectedClientPhone;
    if (elements.gstFilingExportGstr1Btn) elements.gstFilingExportGstr1Btn.disabled = !canExport;
    if (elements.gstFilingExportGstr3bBtn) elements.gstFilingExportGstr3bBtn.disabled = !canExport;
    if (elements.gstFilingReviewBtn) elements.gstFilingReviewBtn.disabled = !state.selectedClientPhone || scopeAll;

    if (elements.gstFilingGstr1Exported) {
        elements.gstFilingGstr1Exported.checked = !!st.gstr1_exported;
    }
    if (elements.gstFilingGstr3bExported) {
        elements.gstFilingGstr3bExported.checked = !!st.gstr3b_exported;
    }
    if (elements.gstFilingMarked) {
        elements.gstFilingMarked.checked = marked;
        elements.gstFilingMarked.disabled = scopeAll || !state.selectedClientPhone;
    }
    if (elements.gstFilingArn) {
        if (document.activeElement !== elements.gstFilingArn) {
            elements.gstFilingArn.value = st.arn_note || '';
        }
        elements.gstFilingArn.disabled = scopeAll || !state.selectedClientPhone;
    }
}

function openGstrModalForMonthClose() {
    if (!state.selectedClientPhone) {
        showToast('Select a client first.', true);
        return;
    }
    if (state.selectedMonth && state.selectedMonth !== 'all' && elements.modalGstrMonth) {
        elements.modalGstrMonth.value = state.selectedMonth;
    }
    openGstrModal();
}

async function openClientNudgeModal(opts = {}) {
    if (!state.selectedClientPhone) {
        showToast('Select a client first.', true);
        return;
    }
    const period = (opts.returnPeriod
        || (state.selectedMonth && state.selectedMonth !== 'all' ? state.selectedMonth : '')
        || '').trim();
    if (!/^\d{4}-\d{2}$/.test(period)) {
        showToast('Pick a month (YYYY-MM) before nudging the client.', true);
        return;
    }

    showToast('Preparing WhatsApp nudge…');
    let preview;
    try {
        const response = await apiFetch(
            `/api/clients/${encodeURIComponent(state.selectedClientPhone)}/nudge/preview?return_period=${encodeURIComponent(period)}`
        );
        preview = await readJsonOrThrow(response, 'Failed to build nudge');
    } catch (e) {
        showToast(e.message || 'Failed to build nudge', true);
        return;
    }

    const counts = preview.counts || {};
    const hint = opts.afterImport
        ? 'Suggested after GSTR-2B import — review the message, then send.'
        : 'Sends on WhatsApp (also appears in WA Simulator if API send fails).';

    openCaFormModal({
        title: 'Nudge client on WhatsApp',
        icon: 'sms',
        submitLabel: 'Send nudge',
        bodyHtml: `
            <p class="text-[13px] text-on-surface-variant">${escapeHtml(hint)}</p>
            <div class="text-[11px] text-on-surface-variant">
                ${escapeHtml(preview.month_label || period)} ·
                unmatched ${counts.unmatched || 0} ·
                mismatch ${counts.mismatch || 0} ·
                pending ${counts.pending || 0}
            </div>
            <div class="ca-form-field">
                <label for="ca-nudge-message">Message</label>
                <textarea id="ca-nudge-message" rows="10">${escapeHtml(preview.message || '')}</textarea>
            </div>
        `,
        onSubmit: async () => {
            const ta = document.getElementById('ca-nudge-message');
            const message = (ta && ta.value ? ta.value : '').trim();
            if (!message) throw new Error('Message cannot be empty');
            const response = await apiFetch(
                `/api/clients/${encodeURIComponent(state.selectedClientPhone)}/nudge`,
                {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        return_period: period,
                        message,
                        send: true,
                        ca_user: 'CA Admin',
                    }),
                }
            );
            await readJsonOrThrow(response, 'Failed to send nudge');
            closeCaFormModal();
            showToast(`Nudge sent to client for ${preview.month_label || period}`);
        },
    });
}

function setSidebarNavActive(navKey) {
    document.querySelectorAll('.nav-link[data-nav]').forEach((el) => {
        el.classList.toggle('active', el.getAttribute('data-nav') === navKey);
    });
    if (elements.navClientsBtn) {
        elements.navClientsBtn.classList.toggle('is-active', navKey === 'clients');
    }
    // Swap main content focus for Dashboard / Audit Queue / GSTR Filing
    if (navKey === 'dashboard' || navKey === 'queue' || navKey === 'gstr') {
        document.body.setAttribute('data-view', navKey);
    }
}

function jumpToAuditQueue() {
    setSidebarNavActive('queue');
    const tab = document.querySelector('.tab-btn[data-filter="exceptions"]');
    if (tab) tab.click();
    const table = document.querySelector('.queue-table') || elements.invoiceRowsBody;
    if (table) table.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

async function startNewAudit() {
    jumpToAuditQueue();
    const ids = state.filteredInvoiceIds && state.filteredInvoiceIds.length
        ? state.filteredInvoiceIds
        : getFilteredInvoices().map((inv) => Number(inv.id));
    if (ids.length) {
        await openInvoiceAudit(ids[0]);
        showToast('Opened next bill in Needs review');
        return;
    }
    showToast('Queue is empty — import 2B or wait for WhatsApp bills.', true);
}

function openDashboardSettings() {
    setSidebarNavActive('settings');
    const profile = getCaProfile();
    const choice = window.prompt(
        'Security settings:\n' +
        '1 = CA login (invite + password)\n' +
        '2 = Set shared API key (admin)\n' +
        '3 = Log out CA\n' +
        '4 = Clear API key\n\n' +
        (profile && profile.name ? `CA: ${profile.name}` : 'Not logged in as CA'),
        '1'
    );
    if (choice === null) return;
    const c = String(choice).trim();
    if (c === '1') {
        const invite = window.prompt(
            'CA invite code (6 digits) for THIS firm.\n\n'
            + 'Demo firm: 123456\n'
            + 'Test Firm B: 574646\n'
            + 'Test Firm A: 574509\n\n'
            + 'Do not reuse 123456 if you want another firm.',
            ''
        ) || '';
        if (!invite.trim()) {
            showToast('Enter a firm invite code to log in as that CA.', true);
            return;
        }
        const password = window.prompt('CA password (often Taxova@ChangeMe):', '') || '';
        if (!password.trim()) {
            showToast('Password required.', true);
            return;
        }
        caLogin(invite.trim(), password)
            .then((data) => {
                const firmLabel = data.firm?.name || data.ca?.firm_name || '';
                showToast(
                    firmLabel
                        ? `Logged in as ${data.ca?.name || 'CA'} · ${firmLabel}`
                        : `Logged in as ${data.ca?.name || 'CA'}`
                );
                return fetchClients();
            })
            .catch((e) => showToast(e.message || 'Login failed', true));
        return;
    }
    if (c === '2') {
        const current = getDashboardApiKey();
        const key = window.prompt(
            'Platform admin API key only — sees ALL firms.\n'
            + 'Do NOT use this to test firm isolation.\n'
            + 'Leave blank to clear.\nCurrent: '
            + (current ? `${current.slice(0, 4)}…` : '(not set)'),
            current || ''
        );
        if (key === null) return;
        if (!String(key).trim()) {
            localStorage.removeItem('DASHBOARD_API_KEY');
            updateFirmChip();
            showToast('API key cleared');
            return;
        }
        // Admin mode: drop CA session so key is actually used
        localStorage.removeItem('CA_SESSION_TOKEN');
        localStorage.removeItem('CA_PROFILE');
        localStorage.removeItem('CA_FIRM');
        localStorage.setItem('DASHBOARD_API_KEY', String(key).trim());
        updateFirmChip();
        showToast('Admin API key saved — all firms visible');
        fetchClients();
        return;
    }
    if (c === '3') {
        caLogout().then(() => {
            showToast('Logged out. Use Settings → 1 to log in as another firm.');
            if (elements.clientSelect) {
                elements.clientSelect.innerHTML = '<option value="">Log in to load clients…</option>';
            }
        });
        return;
    }
    if (c === '4') {
        localStorage.removeItem('DASHBOARD_API_KEY');
        updateFirmChip();
        showToast('API key cleared');
    }
}

function showSupportHelp() {
    setSidebarNavActive('support');
    showToast('Shortcuts: Ctrl+K search · A approve · R reject · J/K next bill');
    if (elements.workflowGuide) {
        elements.workflowGuide.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
}

function setupSidebarNavigation() {
    if (elements.navQueueBtn) {
        elements.navQueueBtn.addEventListener('click', () => jumpToAuditQueue());
    }
    if (elements.navClientsBtn) {
        elements.navClientsBtn.addEventListener('click', () => {
            setSidebarNavActive('clients');
            if (elements.clientSelect) {
                elements.clientSelect.focus();
                try { elements.clientSelect.showPicker(); } catch (_) { /* unsupported */ }
                elements.clientSelect.scrollIntoView({ behavior: 'smooth', block: 'center' });
            }
            showToast('Choose a client from the top bar');
        });
    }
    if (elements.navSettingsBtn) {
        elements.navSettingsBtn.addEventListener('click', openDashboardSettings);
    }
    if (elements.navSupportBtn) {
        elements.navSupportBtn.addEventListener('click', showSupportHelp);
    }
    if (elements.navNewAuditBtn) {
        elements.navNewAuditBtn.addEventListener('click', () => {
            startNewAudit().catch((err) => {
                console.error(err);
                showToast(err.message || 'Could not open audit', true);
            });
        });
    }
    if (elements.navDashboardBtn) {
        elements.navDashboardBtn.addEventListener('click', (e) => {
            e.preventDefault();
            setSidebarNavActive('dashboard');
            window.scrollTo({ top: 0, behavior: 'smooth' });
        });
    }
    if (elements.navSimulatorBtn) {
        elements.navSimulatorBtn.addEventListener('click', () => setSidebarNavActive('simulator'));
    }
    if (elements.navSignoutBtn) {
        elements.navSignoutBtn.addEventListener('click', (e) => {
            handleSignOutClick(e).catch((err) => {
                console.error(err);
                showToast(err.message || 'Sign out failed', true);
            });
        });
    }

    const collapseBtn = document.getElementById('sidebar-collapse-btn');
    const collapseIcon = document.getElementById('sidebar-collapse-icon');
    const openBtn = document.getElementById('sidebar-open-btn');

    function setSidebarCollapsed(collapsed) {
        document.body.classList.toggle('sidebar-collapsed', collapsed);
        localStorage.setItem('TAXOVA_SIDEBAR_COLLAPSED', collapsed ? '1' : '0');
        if (collapseIcon) {
            collapseIcon.textContent = collapsed ? 'left_panel_open' : 'left_panel_close';
        }
        if (collapseBtn) {
            collapseBtn.title = collapsed ? 'Open sidebar' : 'Hide sidebar';
            collapseBtn.setAttribute('aria-label', collapsed ? 'Open sidebar' : 'Hide sidebar');
        }
        if (openBtn) {
            openBtn.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
        }
    }

    if (collapseBtn) {
        const stored = localStorage.getItem('TAXOVA_SIDEBAR_COLLAPSED') === '1';
        setSidebarCollapsed(stored);
        collapseBtn.addEventListener('click', () => {
            setSidebarCollapsed(!document.body.classList.contains('sidebar-collapsed'));
        });
    }
    if (openBtn) {
        openBtn.addEventListener('click', () => setSidebarCollapsed(false));
    }
}

function prefersReducedMotion() {
    try {
        return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    } catch (_) {
        return false;
    }
}

function animateMetricText(el, nextText) {
    if (!el) return;
    const text = String(nextText);
    if (el.textContent === text) return;
    el.textContent = text;
    if (prefersReducedMotion()) return;
    el.classList.remove('is-ticking');
    void el.offsetWidth;
    el.classList.add('is-ticking');
}

function updateDashboardMetrics(metrics) {
    const periodLabel = formatYearMonthLabel(state.selectedMonth);
    const hasGstin = !!metrics.has_gstin;

    // KPI Numbers — same scope as the invoice table
    animateMetricText(elements.kpiPendingCount, metrics.pending_review ?? 0);
        elements.kpiPendingSubtext.textContent = `${metrics.pending_review ?? 0} in your review queue for ${periodLabel}`;
    elements.kpiPendingTrend.textContent = `${metrics.invoice_count ?? 0} invoices in view`;

    if (elements.kpiFlaggedCount) {
        animateMetricText(elements.kpiFlaggedCount, metrics.flagged_count ?? 0);
    }

    updateMonthCloseStrip(metrics);

    // Taxable: prefer sales when GSTIN exists; otherwise show all-invoice taxable honestly
    const displayTaxable = hasGstin
        ? (metrics.sales_taxable || 0)
        : (metrics.total_taxable || 0);
    const displayItc = metrics.itc_claimed || 0;
    const displayCgst = metrics.itc_cgst || 0;
    const displaySgst = metrics.itc_sgst || 0;
    const displayIgst = metrics.itc_igst || 0;
    const displayBlocked = metrics.itc_blocked || 0;
    const displayProvisional = metrics.itc_provisional || 0;
    const displayCredit = metrics.itc_credit_balance || 0;
    const displaySalesGst = hasGstin ? (metrics.sales_gst_liability || 0) : 0;
    const displayNetGst = hasGstin ? (metrics.net_gst_payable ?? 0) : 0;

    animateMetricText(elements.kpiSalesTotal, formatCurrency(displayTaxable));
    const salesLabel = document.getElementById('kpi-sales-label');
    const salesChip = document.getElementById('kpi-sales-chip');
    if (hasGstin) {
        if (salesLabel) salesLabel.textContent = 'Sales Taxable';
        elements.kpiSalesSubtext.textContent = `${formatCurrency(displayTaxable)} approved outward for ${periodLabel}`;
        if (salesChip) salesChip.textContent = 'Approved with supplier GSTIN match';
    } else {
        if (salesLabel) salesLabel.textContent = 'Total Taxable';
        elements.kpiSalesSubtext.textContent = `${formatCurrency(displayTaxable)} all invoices — set a valid GSTIN for sales/ITC`;
        if (salesChip) salesChip.textContent = 'No valid GSTIN — sales/ITC not classified';
    }

    animateMetricText(elements.kpiItcTotal, formatCurrency(displayItc));
    const itcChip = document.getElementById('kpi-itc-chip');
    const itcProv = document.getElementById('kpi-itc-provisional');
    if (!hasGstin) {
        elements.kpiItcSubtext.textContent = 'Set a valid client GSTIN to classify purchase ITC';
        if (itcChip) itcChip.textContent = 'Unclassified without GSTIN';
    } else {
        elements.kpiItcSubtext.textContent = displayItc > 0
            ? `${formatCurrency(displayItc)} eligible on approved bills`
            : 'Approve purchases where client is recipient (mixed bills split by line)';
        if (itcChip) itcChip.textContent = displayBlocked > 0
            ? `Blocked ${formatCurrency(displayBlocked)}`
            : 'Approved with buyer match';
    }
    if (itcProv) {
        if (hasGstin && displayProvisional > 0) {
            itcProv.style.display = 'inline-block';
            itcProv.textContent = `Provisional ${formatCurrency(displayProvisional)}`;
        } else {
            itcProv.style.display = 'none';
        }
    }

    if (elements.gstr2bSummaryChip) {
        const m2 = metrics.itc_2b_matched || 0;
        const u2 = metrics.itc_2b_unmatched || 0;
        const x2 = metrics.itc_2b_mismatch || 0;
        const nM = metrics.gstr2b_matched_count || 0;
        const nU = metrics.gstr2b_unmatched_count || 0;
        const nX = metrics.gstr2b_mismatch_count || 0;
        if (nM + nU + nX > 0) {
            elements.gstr2bSummaryChip.style.display = 'inline';
            elements.gstr2bSummaryChip.textContent =
                `2B: ${nM} matched (${formatCurrency(m2)}) · ${nU} missing · ${nX} mismatch`;
        } else {
            elements.gstr2bSummaryChip.style.display = 'none';
            elements.gstr2bSummaryChip.textContent = '';
        }
    }

    if (elements.netSalesGst)    elements.netSalesGst.textContent  = formatCurrency(displaySalesGst);
    if (elements.netItcAmount)   elements.netItcAmount.textContent = formatCurrency(displayItc);
    if (elements.netGstPayable)  elements.netGstPayable.textContent = formatCurrency(
        displayCredit > 0 ? displayCredit : displayNetGst
    );
    if (elements.netItcBlocked)  elements.netItcBlocked.textContent = formatCurrency(displayBlocked);
    if (elements.netItc2bMatched) {
        elements.netItc2bMatched.textContent = formatCurrency(metrics.itc_2b_matched || 0);
    }
    if (elements.netItcCgst)     elements.netItcCgst.textContent   = formatCurrency(displayCgst);
    if (elements.netItcSgst)     elements.netItcSgst.textContent   = formatCurrency(displaySgst);
    if (elements.netItcIgst)     elements.netItcIgst.textContent   = formatCurrency(displayIgst);

    const netTitle = document.getElementById('net-gst-title');
    const netSubtitle = document.getElementById('net-gst-subtitle');
    const netResultLabel = document.getElementById('net-gst-result-label');
    if (hasGstin && displayCredit > 0) {
        if (netTitle) netTitle.textContent = 'ITC Credit Balance this period';
        if (netSubtitle) netSubtitle.textContent = 'Eligible ITC exceeds approved sales GST';
        if (netResultLabel) netResultLabel.textContent = 'ITC Credit';
        if (elements.netGstPayable) elements.netGstPayable.className = 'text-[20px] font-extrabold text-secondary kpi-value';
    } else {
        if (netTitle) netTitle.textContent = 'Net GST Payable this period';
        if (netSubtitle) netSubtitle.textContent = 'Approved sales GST − approved eligible ITC';
        if (netResultLabel) netResultLabel.textContent = 'Net Payable';
        if (elements.netGstPayable) elements.netGstPayable.className = 'text-[20px] font-extrabold text-primary kpi-value';
    }

    if (elements.itcUtilisationBar && elements.itcUtilisationPct) {
        const utilisationPct = displaySalesGst > 0 ? Math.min(100, (displayItc / displaySalesGst) * 100) : 0;
        elements.itcUtilisationBar.style.width = `${utilisationPct.toFixed(1)}%`;
        elements.itcUtilisationPct.textContent = `${utilisationPct.toFixed(1)}%`;
    }

    if (elements.itcSectionPanel) {
        elements.itcSectionPanel.style.display = state.selectedClientPhone ? 'block' : 'none';
    }

    elements.filingStatusTitle.textContent = `REVIEW READINESS — ${periodLabel}`;

    const pending = metrics.pending_review ?? 0;
    if (!hasGstin) {
        elements.statusGstr1Val.textContent = 'GSTIN required';
        elements.statusGstr1Val.className = 'text-[11px] font-semibold text-tertiary';
        elements.statusGstr3bVal.textContent = 'On hold';
        elements.statusGstr3bVal.className = 'text-[11px] font-semibold text-tertiary';
    } else if (pending > 0) {
        elements.statusGstr1Val.textContent = 'Action required';
        elements.statusGstr1Val.className = 'text-[11px] font-semibold text-tertiary';
        elements.statusGstr3bVal.textContent = 'Hold';
        elements.statusGstr3bVal.className = 'text-[11px] font-semibold text-error';
    } else {
        elements.statusGstr1Val.textContent = 'Ready to export';
        elements.statusGstr1Val.className = 'text-[11px] font-semibold text-secondary';
        elements.statusGstr3bVal.textContent = 'Ready';
        elements.statusGstr3bVal.className = 'text-[11px] font-semibold text-secondary';
    }
}

// ── ITC Sparkline Chart (real Chart.js) ──────────────────────────────────────
async function renderITCSparkline() {
    if (!state.selectedClientPhone || !elements.sparklineCanvas) return;

    try {
        const response = await apiFetch(`/api/itc/summary?client_phone=${state.selectedClientPhone}&months=12`);
        const trendData = await response.json(); // [{month, itc_eligible, itc_blocked, sales_gst}, ...]

        const labels      = trendData.map(d => {
            const [y, m] = d.month.split('-');
            return new Date(parseInt(y), parseInt(m) - 1, 1).toLocaleDateString('en-US', { month: 'short', year: '2-digit' });
        });
        const itcEligible = trendData.map(d => d.itc_eligible);
        const itcBlocked  = trendData.map(d => d.itc_blocked);
        const salesGst    = trendData.map(d => d.sales_gst);

        // Calculate total eligible ITC and YoY-style trend (last 6 vs first 6)
        const totalEligible = itcEligible.reduce((a, b) => a + b, 0);
        const firstHalf  = itcEligible.slice(0, 6).reduce((a, b) => a + b, 0);
        const secondHalf = itcEligible.slice(6).reduce((a, b) => a + b, 0);
        let trendPct = 0;
        if (firstHalf > 0) trendPct = ((secondHalf - firstHalf) / firstHalf) * 100;

        // Update summary label
        if (elements.sparklineTotalLabel) elements.sparklineTotalLabel.textContent = formatCurrency(totalEligible);
        if (elements.sparklineTrendLabel) {
            const arrow = trendPct >= 0 ? 'trending_up' : 'trending_down';
            const color = trendPct >= 0 ? '#4edea3' : '#FFA8A0';
            elements.sparklineTrendLabel.innerHTML = `<span class="material-symbols-outlined text-[14px]">${arrow}</span> ${Math.abs(trendPct).toFixed(1)}% vs prev 6mo`;
            elements.sparklineTrendLabel.style.color = color;
        }

        // Destroy old chart before creating new one
        if (itcSparklineChart) {
            itcSparklineChart.destroy();
            itcSparklineChart = null;
        }

        const ctx = elements.sparklineCanvas.getContext('2d');
        itcSparklineChart = new Chart(ctx, {
            type: 'line',
            data: {
                labels,
                datasets: [
                    {
                        label: 'ITC Eligible',
                        data: itcEligible,
                        borderColor: '#36D6AE',
                        backgroundColor: 'rgba(54,214,174,0.12)',
                        borderWidth: 2.5,
                        fill: true,
                        tension: 0.4,
                        pointRadius: 3,
                        pointBackgroundColor: '#36D6AE',
                    },
                    {
                        label: 'Blocked ITC',
                        data: itcBlocked,
                        borderColor: '#ED6A6A',
                        backgroundColor: 'rgba(240,120,120,0.08)',
                        borderWidth: 1.5,
                        fill: true,
                        tension: 0.4,
                        pointRadius: 2,
                        pointBackgroundColor: '#ED6A6A',
                        borderDash: [4, 3],
                    },
                    {
                        label: 'Sales GST',
                        data: salesGst,
                        borderColor: '#7BE8C8',
                        backgroundColor: 'transparent',
                        borderWidth: 1.5,
                        fill: false,
                        tension: 0.4,
                        pointRadius: 2,
                        pointBackgroundColor: '#7BE8C8',
                    }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: { duration: 600 },
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        backgroundColor: '#141A22',
                        borderColor: '#494454',
                        borderWidth: 1,
                        titleColor: '#cbc3d7',
                        bodyColor: '#e1e1ef',
                        callbacks: {
                            label: ctx => ` ${ctx.dataset.label}: ${formatCurrency(ctx.parsed.y)}`
                        }
                    }
                },
                scales: {
                    x: {
                        grid: { color: 'rgba(73,68,84,0.4)', drawBorder: false },
                        ticks: { color: '#cbc3d7', font: { size: 10 }, maxRotation: 0 }
                    },
                    y: {
                        grid: { color: 'rgba(73,68,84,0.4)', drawBorder: false },
                        ticks: { color: '#cbc3d7', font: { size: 10 }, callback: v => '₹' + (v >= 1000 ? (v/1000).toFixed(0)+'K' : v) }
                    }
                }
            }
        });
    } catch (e) {
        console.error('ITC sparkline failed:', e);
    }
}

function renderTableLoadingState() {
    elements.invoiceRowsBody.innerHTML = `
        <tr>
            <td colspan="9" class="empty-state loading-pulse">
                <span class="material-symbols-outlined loading-icon" style="animation: spin 1s linear infinite; display:inline-block;">sync</span>
                <p>Loading invoice records…</p>
            </td>
        </tr>
    `;
}

function getFilteredInvoices() {
    let filtered = [...state.invoices];

    if (state.activeFilter === "approved") {
        filtered = filtered.filter(inv => inv.is_approved === 1 || inv.is_approved === true);
    } else if (state.activeFilter === "pending_review") {
        filtered = filtered.filter(isPendingReview);
    } else if (state.activeFilter === "flagged") {
        filtered = filtered.filter(inv => !inv.is_calculation_correct);
    } else if (state.activeFilter === "hitl" || state.activeFilter === "exceptions") {
        filtered = filtered.filter(isExceptionInvoice);
    } else if (state.activeFilter === "itc") {
        filtered = filtered.filter(hasItcIssue);
    }

    if (state.categoryFilter) {
        filtered = filtered.filter(inv =>
            (inv.business_category || 'Other') === state.categoryFilter
        );
    }

    if (state.searchQuery) {
        filtered = filtered.filter(inv =>
            (inv.invoice_number && inv.invoice_number.toLowerCase().includes(state.searchQuery)) ||
            (inv.supplier_name && inv.supplier_name.toLowerCase().includes(state.searchQuery)) ||
            (inv.supplier_gstin && inv.supplier_gstin.toLowerCase().includes(state.searchQuery)) ||
            (inv.recipient_gstin && inv.recipient_gstin.toLowerCase().includes(state.searchQuery))
        );
    }

    const isExceptionTab = state.activeFilter === 'exceptions' || state.activeFilter === 'hitl';
    const isItcTab = state.activeFilter === 'itc';
    if (isExceptionTab || isItcTab) {
        filtered.sort((a, b) => {
            if (isExceptionTab) {
                const ra = isReadyToApprove(a) ? 1 : 0;
                const rb = isReadyToApprove(b) ? 1 : 0;
                if (ra !== rb) return rb - ra; // ready-to-approve first (pilot velocity)
            }
            const scoreFn = isItcTab ? scoreItcIssue : scoreExceptionRisk;
            const d = scoreFn(b) - scoreFn(a);
            return d !== 0 ? d : (b.id || 0) - (a.id || 0);
        });
    }
    return filtered;
}

function renderItcIssuesList() {
    if (!elements.itcIssuesList) return;

    const issues = (state.invoices || []).filter(hasItcIssue);
    issues.sort((a, b) => scoreItcIssue(b) - scoreItcIssue(a));

    const periodLabel = formatYearMonthLabel(state.selectedMonth);
    if (elements.itcIssuesSubtitle) {
        elements.itcIssuesSubtitle.textContent = issues.length
            ? `${issues.length} bill(s) with blocked, partial, or 2B flags · ${periodLabel}`
            : `No ITC flags in ${periodLabel}`;
    }
    if (elements.itcIssuesViewAll) {
        elements.itcIssuesViewAll.style.display = issues.length > 5 ? 'inline' : 'none';
    }

    if (!issues.length) {
        elements.itcIssuesList.innerHTML = '<div class="itc-issues-empty">All clear — no blocked, partial, or 2B ITC issues in this view.</div>';
        return;
    }

    elements.itcIssuesList.innerHTML = '';
    issues.slice(0, 5).forEach((inv) => {
        const summary = getItcIssueSummary(inv);
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'itc-issue-row';
        btn.innerHTML = `
            <div class="itc-issue-main">
                <div class="itc-issue-supplier">
                    <span class="itc-issue-badge ${summary.badgeClass}">${summary.badge}</span>
                    ${(inv.supplier_name || 'Unknown supplier').replace(/</g, '&lt;')}
                </div>
                <div class="itc-issue-reason">${summary.reason.replace(/</g, '&lt;')}</div>
            </div>
            <div class="itc-issue-amt ${summary.amountClass}">${formatCurrency(summary.amount)}</div>
        `;
        btn.addEventListener('click', () => openInvoiceAudit(inv.id));
        elements.itcIssuesList.appendChild(btn);
    });
}

function updateBulkBar() {
    const n = state.selectedInvoiceIds.size;
    if (elements.bulkBar) {
        elements.bulkBar.style.display = n > 0 ? 'inline-flex' : 'none';
    }
    if (elements.bulkSelectedCount) {
        elements.bulkSelectedCount.textContent = `${n} selected`;
    }
    if (elements.selectAllInvoices) {
        const ids = state.filteredInvoiceIds || [];
        const allSelected = ids.length > 0 && ids.every((id) => state.selectedInvoiceIds.has(Number(id)));
        elements.selectAllInvoices.checked = allSelected;
        elements.selectAllInvoices.indeterminate = n > 0 && !allSelected;
    }
}

function updateDrawerNavPos() {
    if (!elements.drawerNavPos) return;
    const ids = state.filteredInvoiceIds || [];
    const cur = state.currentInvoice ? Number(state.currentInvoice.id) : null;
    const idx = cur == null ? -1 : ids.indexOf(cur);
    if (idx < 0 || !ids.length) {
        elements.drawerNavPos.textContent = '—';
        if (elements.drawerPrevBtn) elements.drawerPrevBtn.disabled = true;
        if (elements.drawerNextBtn) elements.drawerNextBtn.disabled = true;
        return;
    }
    elements.drawerNavPos.textContent = `${idx + 1} / ${ids.length}`;
    if (elements.drawerPrevBtn) elements.drawerPrevBtn.disabled = idx <= 0;
    if (elements.drawerNextBtn) elements.drawerNextBtn.disabled = idx >= ids.length - 1;
}

async function navigateAuditDrawer(delta) {
    const ids = state.filteredInvoiceIds || [];
    if (!ids.length || !state.currentInvoice) return;
    const idx = ids.indexOf(Number(state.currentInvoice.id));
    if (idx < 0) return;
    const next = idx + delta;
    if (next < 0 || next >= ids.length) return;
    await openInvoiceAudit(ids[next]);
}

async function bulkApproveSelected() {
    const ids = [...state.selectedInvoiceIds];
    if (!ids.length) {
        showToast('Select at least one invoice.', true);
        return;
    }
    if (elements.bulkApproveBtn) elements.bulkApproveBtn.disabled = true;
    let ok = 0;
    let fail = 0;
    try {
        showToast(`Approving ${ids.length} invoice(s)…`);
        for (const id of ids) {
            try {
                const response = await apiFetch(`/api/invoices/${id}/approve`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ ca_user: 'CA Admin' }),
                });
                await readJsonOrThrow(response, `Approve #${id} failed`);
                ok += 1;
                state.selectedInvoiceIds.delete(Number(id));
            } catch (err) {
                console.error(err);
                fail += 1;
            }
        }
        showToast(
            fail
                ? `Approved ${ok} · failed ${fail}`
                : `Approved ${ok} invoice(s)`
        );
        await refreshQueueAndPilot();
        updateBulkBar();
    } finally {
        if (elements.bulkApproveBtn) elements.bulkApproveBtn.disabled = false;
    }
}

function renderInvoiceTable() {
    const filtered = getFilteredInvoices();
    state.filteredInvoiceIds = filtered.map((inv) => Number(inv.id));
    // Drop selections that are no longer in view
    const visible = new Set(state.filteredInvoiceIds);
    [...state.selectedInvoiceIds].forEach((id) => {
        if (!visible.has(Number(id))) state.selectedInvoiceIds.delete(Number(id));
    });

    const isExceptionTab = state.activeFilter === 'exceptions' || state.activeFilter === 'hitl';
    const isItcTab = state.activeFilter === 'itc';
    const pendingCount = state.invoices.filter(isPendingReview).length;
    const exceptionCount = state.invoices.filter(isExceptionInvoice).length;
    const itcIssueCount = state.invoices.filter(hasItcIssue).length;
    const periodLabel = formatYearMonthLabel(state.selectedMonth);
    const scopeAll = !state.selectedMonth || state.selectedMonth === 'all';
    const periodPlain = scopeAll ? 'all months' : periodLabel;

    if (isItcTab) {
        elements.recordCountTxt.textContent = itcIssueCount > 0
            ? `${itcIssueCount} bills need ITC attention for ${periodPlain}. Showing ${filtered.length}.`
            : `No ITC flags for ${periodPlain}. Showing ${filtered.length} bills.`;
        elements.recordCountTxt.style.color = itcIssueCount > 0 ? '#A85C10' : '#33604A';
    } else if (isExceptionTab) {
        const ready = state.invoices.filter((inv) => isExceptionInvoice(inv) && isReadyToApprove(inv)).length;
        if (exceptionCount === 0) {
            elements.recordCountTxt.textContent = `Nothing is waiting on your review for ${periodPlain}.`;
        } else if (ready > 0) {
            elements.recordCountTxt.textContent =
                `${exceptionCount} bills need review for ${periodPlain}. ${ready} ${ready === 1 ? 'is' : 'are'} ready to approve first.`;
        } else {
            elements.recordCountTxt.textContent =
                `${exceptionCount} bills need review for ${periodPlain}. Highest risk first.`;
        }
        elements.recordCountTxt.style.color = exceptionCount > 0 ? '#A85C10' : '#33604A';
    } else {
        elements.recordCountTxt.textContent =
            `${pendingCount} pending in this view for ${periodPlain}. Showing ${filtered.length}.`;
        elements.recordCountTxt.style.color = exceptionCount > 0 ? '#A85C10' : '#33604A';
    }
    if (elements.queueViewCount) {
        elements.queueViewCount.textContent = `${filtered.length} in this view`;
    }

    if (filtered.length === 0) {
        const scopeAll = !state.selectedMonth || state.selectedMonth === 'all';
        const noClient = !state.selectedClientPhone;
        let emptyClass = 'empty-state';
        let icon = 'inbox';
        let emptyTitle = 'No invoices in this view';
        let emptySub = 'Try another month, client, or send a sample via the WA Simulator.';
        let actions = `
            <button type="button" class="empty-btn" data-empty-action="simulator">
                <span class="material-symbols-outlined text-[16px]">science</span> WA Simulator
            </button>`;

        if (noClient) {
            emptyClass += ' is-muted';
            icon = 'group';
            emptyTitle = 'Pick a client to begin';
            emptySub = 'Select a client in the top bar — then review bills, import 2B, and close the month.';
            actions = `
                <button type="button" class="empty-btn is-primary" data-empty-action="client">
                    <span class="material-symbols-outlined text-[16px]">person_search</span> Choose client
                </button>`;
        } else if (isExceptionTab && exceptionCount === 0) {
            emptyClass += ' is-clear';
            icon = 'task_alt';
            emptyTitle = 'All clear';
            const totalBills = state.invoices.length;
            emptySub = scopeAll
                ? 'No bills need review. Pick a month to import 2B and close.'
                : (totalBills > 0
                    ? `${totalBills} bill(s) in this month — none need CA review. Next: Import 2B → Close month.`
                    : 'Queue is empty — next: Import 2B → Close month.');
            actions = scopeAll
                ? `<button type="button" class="empty-btn is-primary" data-empty-action="month">
                     <span class="material-symbols-outlined text-[16px]">calendar_month</span> Pick month
                   </button>`
                : `<button type="button" class="empty-btn is-primary" data-empty-action="import">
                     <span class="material-symbols-outlined text-[16px]">compare_arrows</span> Import 2B
                   </button>
                   <button type="button" class="empty-btn" data-empty-action="all-bills">
                     <span class="material-symbols-outlined text-[16px]">receipt_long</span> All bills
                   </button>
                   <button type="button" class="empty-btn" data-empty-action="close">
                     <span class="material-symbols-outlined text-[16px]">event_available</span> Month close
                   </button>`;
        } else if (isItcTab) {
            emptyClass += ' is-clear';
            icon = 'verified_user';
            emptyTitle = 'No ITC issues';
            emptySub = 'No blocked, partial, or 2B mismatches in this period.';
            actions = `
                <button type="button" class="empty-btn" data-empty-action="exceptions">
                    <span class="material-symbols-outlined text-[16px]">fact_check</span> Needs review
                </button>
                <button type="button" class="empty-btn" data-empty-action="all-bills">
                    <span class="material-symbols-outlined text-[16px]">receipt_long</span> All bills
                </button>`;
        } else if (!isExceptionTab) {
            emptyClass += ' is-muted';
            icon = 'filter_alt';
            emptyTitle = 'Nothing in this filter';
            emptySub = 'Switch to Needs review, or widen month / category.';
            actions = `
                <button type="button" class="empty-btn is-primary" data-empty-action="exceptions">
                    <span class="material-symbols-outlined text-[16px]">fact_check</span> Needs review
                </button>`;
        }

        elements.invoiceRowsBody.innerHTML = `
            <tr>
                <td colspan="9" class="${emptyClass}">
                    <div class="empty-illu">
                        <span class="material-symbols-outlined" style="font-size:36px;font-variation-settings:'FILL' 1;">${icon}</span>
                    </div>
                    <p class="empty-title">${emptyTitle}</p>
                    <p class="empty-sub">${emptySub}</p>
                    <div class="empty-actions">${actions}</div>
                </td>
            </tr>
        `;
        updateBulkBar();
        updateDrawerNavPos();
        return;
    }

    elements.invoiceRowsBody.innerHTML = '';
    filtered.forEach((inv, rowIdx) => {
        const tr = document.createElement('tr');
        tr.classList.add('row-enter');
        tr.style.animationDelay = `${Math.min(rowIdx, 12) * 0.03}s`;
        tr.style.cursor = 'pointer';
        tr.addEventListener('click', (ev) => {
            if (ev.target.closest('.row-actions') || ev.target.closest('.queue-check')) return;
            openInvoiceAudit(inv.id);
        });

        const invDate = inv.invoice_date || 'N/A';

        let statusBadge = '';
        const rs = inv.review_status || (inv.is_approved ? 'approved' : 'needs_review');
        if (rs === 'approved' || inv.is_approved) {
            statusBadge = `<span class="badge badge-approved"><span class="material-symbols-outlined" style="font-size:13px;font-variation-settings:'FILL' 1;">task_alt</span> Approved</span>`;
        } else if (rs === 'rejected') {
            statusBadge = `<span class="badge badge-rejected"><span class="material-symbols-outlined" style="font-size:13px;">cancel</span> Rejected</span>`;
        } else if (rs === 'skipped') {
            statusBadge = `<span class="badge badge-pending"><span class="material-symbols-outlined" style="font-size:13px;">schedule</span> Skipped</span>`;
        } else if (rs === 'client_confirmed') {
            statusBadge = `<span class="badge badge-confirmed"><span class="material-symbols-outlined" style="font-size:13px;">how_to_reg</span> Client OK</span>`;
        } else if (rs === 'awaiting_client') {
            statusBadge = `<span class="badge badge-hitl"><span class="material-symbols-outlined" style="font-size:13px;">sms</span> Await Client</span>`;
        } else if (!inv.is_calculation_correct) {
            statusBadge = `<span class="badge badge-flagged"><span class="material-symbols-outlined" style="font-size:13px;">warning</span> Math</span>`;
        } else {
            statusBadge = `<span class="badge badge-pending"><span class="material-symbols-outlined" style="font-size:13px;">person_search</span> Review</span>`;
        }

        const rowReasons = isItcTab
            ? [getItcIssueSummary(inv).reason]
            : (isExceptionTab ? getExceptionReasons(inv) : []);
        const reasonTitle = rowReasons.join(' · ').replace(/"/g, '&quot;');
        const reasonHtml = rowReasons.length
            ? `<div class="exception-reasons" title="${reasonTitle}">${rowReasons.slice(0, 2).map((r) =>
                `<span class="exception-chip">${r.replace(/</g, '&lt;')}</span>`
              ).join('')}${rowReasons.length > 2
                ? `<span class="exception-chip is-more">+${rowReasons.length - 2}</span>`
                : ''}</div>`
            : '';

        const showQuick = canQuickAct(inv);
        const checked = state.selectedInvoiceIds.has(Number(inv.id)) ? 'checked' : '';
        const actionsHtml = showQuick
            ? `
            <div class="row-actions">
                <button type="button" class="row-icon-btn is-approve" data-row-action="approve" data-invoice-id="${inv.id}" title="Approve">
                    <span class="material-symbols-outlined">check</span>
                </button>
                <button type="button" class="audit-btn" data-row-action="audit" data-invoice-id="${inv.id}" title="Open audit drawer">Audit</button>
                <div class="row-more">
                    <button type="button" class="row-icon-btn" data-row-more-toggle aria-label="More actions" title="More">
                        <span class="material-symbols-outlined">more_horiz</span>
                    </button>
                    <div class="row-more-menu">
                        <button type="button" data-row-action="fix-gstin" data-invoice-id="${inv.id}">
                            <span class="material-symbols-outlined">edit</span> Fix GSTIN
                        </button>
                        <button type="button" data-row-action="mismatch" data-invoice-id="${inv.id}">
                            <span class="material-symbols-outlined">difference</span> Note mismatch
                        </button>
                        <button type="button" class="is-danger" data-row-action="skip" data-invoice-id="${inv.id}">
                            <span class="material-symbols-outlined">schedule</span> Skip
                        </button>
                    </div>
                </div>
            </div>`
            : `
            <div class="row-actions">
                <button type="button" class="audit-btn" data-row-action="audit" data-invoice-id="${inv.id}">Audit</button>
            </div>`;

        tr.innerHTML = `
            <td class="py-2 px-3" onclick="event.stopPropagation()">
                <input type="checkbox" class="queue-check row-select" data-invoice-id="${inv.id}" ${checked} />
            </td>
            <td class="py-2 px-3">
                <span style="font-size:13px;font-weight:700;color:#17233B;font-family:'IBM Plex Mono',monospace;">INV-${inv.invoice_number || 'N/A'}</span>
            </td>
            <td class="py-2 px-3">
                <div style="font-size:13px;font-weight:700;color:#17233B;">${inv.supplier_name || 'Not Detected'}</div>
                <div style="font-size:11px;color:#6B6459;font-family:'IBM Plex Mono',monospace;">${inv.supplier_gstin || 'No GSTIN'}</div>
                ${reasonHtml}
            </td>
            <td class="py-2 px-3" style="font-size:13px;color:#6B6459;white-space:nowrap;">${invDate}</td>
            <td class="py-2 px-3">
                <span style="display:inline-block;padding:3px 8px;border-radius:6px;font-size:11px;font-weight:600;background:#FFFFFF;border:1px solid #DAD3C3;color:#17233B;">${inv.business_category || 'Other'}</span>
                ${inv.itc_partial ? '<span style="display:inline-block;margin-left:4px;padding:3px 6px;border-radius:6px;font-size:10px;font-weight:700;background:rgba(168,92,16,0.12);border:1px solid rgba(168,92,16,0.35);color:#A85C10;">Partial</span>' : ''}
                ${gstr2bBadgeHtml(inv.gstr2b_match_status)}
            </td>
            <td class="py-2 px-3" style="text-align:right;font-size:13px;font-weight:700;color:#17233B;font-variant-numeric:tabular-nums;">₹${formatNumber(inv.total_taxable_value)}</td>
            <td class="py-2 px-3" style="text-align:right;font-size:13px;font-weight:700;color:#33604A;font-variant-numeric:tabular-nums;">₹${formatNumber(inv.grand_total)}</td>
            <td class="py-2 px-3" style="text-align:center;">${statusBadge}</td>
            <td class="py-2 px-3 actions-cell" style="text-align:right;">
                ${actionsHtml}
            </td>
        `;

        const check = tr.querySelector('.row-select');
        if (check) {
            check.addEventListener('change', (e) => {
                const id = Number(e.target.getAttribute('data-invoice-id'));
                if (e.target.checked) state.selectedInvoiceIds.add(id);
                else state.selectedInvoiceIds.delete(id);
                updateBulkBar();
            });
        }

        elements.invoiceRowsBody.appendChild(tr);
    });
    updateBulkBar();
    updateDrawerNavPos();
}

function gstr2bBadgeHtml(status) {
    const s = (status || 'none').toLowerCase();
    if (s === 'matched') {
        return '<span style="display:inline-block;margin-left:6px;padding:4px 8px;border-radius:6px;font-size:10px;font-weight:700;background:rgba(51,96,74,0.12);border:1px solid rgba(51,96,74,0.35);color:#33604A;">2B OK</span>';
    }
    if (s === 'unmatched') {
        return '<span style="display:inline-block;margin-left:6px;padding:4px 8px;border-radius:6px;font-size:10px;font-weight:700;background:rgba(140,47,47,0.12);border:1px solid rgba(140,47,47,0.35);color:#8C2F2F;">Not in 2B</span>';
    }
    if (s === 'mismatch') {
        return '<span style="display:inline-block;margin-left:6px;padding:4px 8px;border-radius:6px;font-size:10px;font-weight:700;background:rgba(168,92,16,0.12);border:1px solid rgba(168,92,16,0.35);color:#A85C10;">2B Mismatch</span>';
    }
    if (s === 'mismatch_accepted') {
        return '<span style="display:inline-block;margin-left:6px;padding:4px 8px;border-radius:6px;font-size:10px;font-weight:700;background:rgba(107,100,89,0.12);border:1px solid rgba(107,100,89,0.35);color:#6B6459;">2B Noted</span>';
    }
    return '';
}

function normalizePartyName(name) {
    return String(name || '')
        .toUpperCase()
        .replace(/[^A-Z0-9\s]/g, ' ')
        .replace(/\b(PRIVATE|LIMITED|PVT|LTD|LLP|OPC|AND|THE|CO|COMPANY|INDIA)\b/g, ' ')
        .replace(/\s+/g, ' ')
        .trim();
}

/** true / false / null (insufficient data) */
function partyNamesLikelyMatch(extracted, portalLegal, portalTrade) {
    const a = normalizePartyName(extracted);
    if (!a || a.length < 3) return null;
    const candidates = [portalLegal, portalTrade]
        .map(normalizePartyName)
        .filter((n) => n && n.length >= 3);
    if (!candidates.length) return null;
    for (const b of candidates) {
        if (a === b || a.includes(b) || b.includes(a)) return true;
        const ta = new Set(a.split(' ').filter((t) => t.length > 2));
        const tb = new Set(b.split(' ').filter((t) => t.length > 2));
        if (!ta.size || !tb.size) continue;
        let hit = 0;
        ta.forEach((t) => { if (tb.has(t)) hit += 1; });
        const ratio = hit / Math.min(ta.size, tb.size);
        if (ratio >= 0.6) return true;
    }
    return false;
}

function setPartyPortalCheckUi(role, kind, html) {
    const card = role === 'recipient' ? elements.recipientPortalCheck : elements.supplierPortalCheck;
    const body = role === 'recipient' ? elements.recipientPortalCheckBody : elements.supplierPortalCheckBody;
    if (!card || !body) return;
    card.classList.remove('is-ok', 'is-warn', 'is-bad');
    if (kind) card.classList.add(kind);
    body.innerHTML = html;
}

async function verifyPartyOnPortal(role, gstin, extractedName, forceRefresh) {
    const isRecipient = role === 'recipient';
    const seqKey = isRecipient ? 'recipientPortalCheckSeq' : 'supplierPortalCheckSeq';
    const stateKey = isRecipient ? 'recipientPortalCheck' : 'supplierPortalCheck';
    const label = isRecipient ? 'Recipient' : 'Supplier';
    const seq = ++state[seqKey];
    const g = String(gstin || '').trim().toUpperCase();
    state[stateKey] = null;

    if (!g) {
        setPartyPortalCheckUi(role, 'is-warn', `No ${label.toLowerCase()} GSTIN on this bill — cannot verify on GSTN.`);
        if (state.currentInvoice) renderAuditAlerts(state.currentInvoice);
        return;
    }
    if (!validateGstinString(g)) {
        setPartyPortalCheckUi(role, 'is-bad', `GSTIN format invalid: <span class="gstin-mono">${escapeHtml(g)}</span>`);
        state[stateKey] = { ok: false, reason: 'invalid_format', gstin: g, issues: [`${label} GSTIN format invalid`] };
        if (state.currentInvoice) renderAuditAlerts(state.currentInvoice);
        return;
    }

    setPartyPortalCheckUi(role, '', `Checking <span class="gstin-mono">${escapeHtml(g)}</span> on GSTN…`);

    try {
        const qs = new URLSearchParams({
            gstin: g,
            include_history: 'false',
            role,
            compare_name: extractedName || '',
        });
        if (forceRefresh) qs.set('refresh', 'true');
        const response = await apiFetch(`/api/gstin/lookup?${qs.toString()}`);
        const data = await readJsonOrThrow(response, `${label} GSTIN portal check failed`);
        if (seq !== state[seqKey]) return;

        const assessment = data.assessment || null;
        state[stateKey] = assessment;

        if (!data.found) {
            if (data.portal_outage || (data.assessment && data.assessment.portal_outage)) {
                const friendly = formatPortalCheckError({
                    errorCode: (data.assessment && data.assessment.error_code) || '',
                    userMessage: (data.assessment && data.assessment.user_message) || data.message || '',
                    message: data.message || '',
                });
                state[stateKey] = data.assessment || {
                    ok: null,
                    portal_outage: true,
                    error_code: friendly.errorCode,
                    user_message: friendly.body,
                    issues: [],
                    reason: null,
                    risk_boost: 0,
                };
                setPartyPortalCheckUi(
                    role,
                    'is-warn',
                    `<div><strong>${escapeHtml(friendly.title)}</strong></div><div class="spc-meta">${escapeHtml(friendly.body)}</div>`
                );
                if (state.currentInvoice) renderAuditAlerts(state.currentInvoice);
                return;
            }
            setPartyPortalCheckUi(
                role,
                'is-bad',
                `<div><strong>This GSTIN was not found on GSTN</strong></div><div class="spc-meta">${escapeHtml(g)}</div>`
            );
            if (state.currentInvoice) renderAuditAlerts(state.currentInvoice);
            return;
        }

        const issues = (assessment && assessment.issues) || [];
        const status = data.status || (assessment && assessment.status) || '';
        const nameMatch = assessment ? assessment.name_match : null;
        const kind = issues.length
            ? (String(status).toLowerCase() && !['active', 'provisional'].includes(String(status).toLowerCase()) ? 'is-bad' : 'is-warn')
            : 'is-ok';
        const matchLabel =
            nameMatch === true ? 'Name matches bill' :
            nameMatch === false ? 'Name does not match bill' :
            'Name not compared';
        const cacheNote = data.from_cache ? ' · cached' : '';
        setPartyPortalCheckUi(
            role,
            kind,
            `<div><strong>${escapeHtml(data.legal_name || 'Registered taxpayer')}</strong> · ${escapeHtml(status || 'Status n/a')}</div>
             <div class="spc-meta">${escapeHtml(g)} · ${escapeHtml(matchLabel)}${data.taxpayer_type ? ' · ' + escapeHtml(data.taxpayer_type) : ''}${escapeHtml(cacheNote)}</div>
             ${issues.length ? `<div class="spc-meta" style="color:#F0B35A;margin-top:6px;">${issues.map(escapeHtml).join(' · ')}</div>` : ''}`
        );
        if (state.currentInvoice) renderAuditAlerts(state.currentInvoice);
    } catch (err) {
        if (seq !== state[seqKey]) return;
        const friendly = formatPortalCheckError(err);
        state[stateKey] = {
            ok: null,
            reason: null,
            gstin: g,
            issues: [],
            portal_outage: true,
            error_code: friendly.errorCode,
            user_message: friendly.body,
            risk_boost: 0,
        };
        setPartyPortalCheckUi(
            role,
            'is-warn',
            `<div><strong>${escapeHtml(friendly.title)}</strong></div><div class="spc-meta">${escapeHtml(friendly.body)}</div>`
        );
        if (state.currentInvoice) renderAuditAlerts(state.currentInvoice);
    }
}

function renderPrimaryIssue(invoice) {
    const card = elements.drawerPrimaryIssue;
    const titleEl = elements.drawerIssueTitle;
    const moreEl = elements.drawerIssueMore;
    if (!card || !titleEl) return;

    const rs = invoice.review_status || (invoice.is_approved ? 'approved' : 'needs_review');
    const reasons = getExceptionReasons(invoice);
    const m2b = (invoice.gstr2b_match_status || 'none').toLowerCase();

    let tone = 'warn';
    let icon = 'priority_high';
    let title = reasons[0] || 'Review and approve if correct';
    let extras = reasons.slice(1);

    if (rs === 'approved' || invoice.is_approved) {
        tone = 'ok';
        icon = 'task_alt';
        title = 'Approved — open only if you need to re-check';
        extras = [];
    } else if (rs === 'rejected') {
        tone = 'bad';
        icon = 'cancel';
        title = 'Rejected';
    } else if (!invoice.is_calculation_correct || m2b === 'mismatch') {
        tone = 'bad';
        icon = 'warning';
    } else if (m2b === 'unmatched' || (invoice.supplier_gstin && !validateGstinString(invoice.supplier_gstin))) {
        tone = 'bad';
        icon = 'warning';
    }

    card.classList.remove('is-warn', 'is-bad', 'is-ok');
    card.classList.add(`is-${tone}`);
    const iconEl = card.querySelector('.drawer-issue-icon');
    if (iconEl) iconEl.textContent = icon;
    titleEl.textContent = title;

    if (moreEl) {
        if (extras.length) {
            moreEl.style.display = 'flex';
            moreEl.innerHTML = extras.slice(0, 4).map((r) =>
                `<span class="drawer-issue-chip">${String(r).replace(/</g, '&lt;')}</span>`
            ).join('');
        } else {
            moreEl.style.display = 'none';
            moreEl.innerHTML = '';
        }
    }
}

function resetDrawerFolds(invoice) {
    // Keep folds closed by default; auto-open ITC when partial/blocked lines matter
    const openItc = !!(invoice && (invoice.itc_partial
        || invoice.is_itc_eligible === false
        || invoice.is_itc_eligible === 0));
    if (elements.drawerFoldItc) {
        elements.drawerFoldItc.open = openItc;
        elements.drawerFoldItc.classList.toggle('has-attention', openItc);
    }
    if (elements.drawerFoldAlerts) elements.drawerFoldAlerts.open = false;
    if (elements.drawerFoldPortal) elements.drawerFoldPortal.open = false;
    if (elements.drawerFoldParties) {
        const needParties = !!(invoice && (
            (invoice.recipient_gstin && !validateGstinString(invoice.recipient_gstin))
            || invoice.itc_partial
            || invoice.is_itc_eligible === false
            || invoice.is_itc_eligible === 0
        ));
        elements.drawerFoldParties.open = needParties;
        elements.drawerFoldParties.classList.toggle('has-attention', needParties);
    }
}

function renderAuditAlerts(invoice) {
    elements.auditAlertsList.innerHTML = '';
    let alerts = [];

    // Math calculation alerts
    if (!invoice.is_calculation_correct && invoice.calculation_errors) {
        invoice.calculation_errors.forEach(err => alerts.push(err));
    }

    // Invalid GSTIN alerts
    if (invoice.supplier_gstin && !validateGstinString(invoice.supplier_gstin)) {
        alerts.push(`Supplier GSTIN "${invoice.supplier_gstin}" has an invalid formatting syntax.`);
    }
    if (invoice.recipient_gstin && !validateGstinString(invoice.recipient_gstin)) {
        alerts.push(`Recipient GSTIN "${invoice.recipient_gstin}" has an invalid formatting syntax.`);
    }

    // ITC Eligibility alerts
    if (invoice.itc_partial) {
        alerts.push(invoice.itc_ineligibility_reason || 'Partial ITC: some lines eligible, some blocked under Sec 17(5).');
    } else if (!invoice.is_itc_eligible && invoice.itc_ineligibility_reason) {
        alerts.push(`ITC Claim blocked: ${invoice.itc_ineligibility_reason}`);
    }

    const m2b = (invoice.gstr2b_match_status || 'none').toLowerCase();
    if (m2b === 'unmatched') {
        alerts.push('GSTR-2B: invoice not found in imported 2B (Sec 16(2)(aa) — do not claim until it appears).');
    } else if (m2b === 'mismatch') {
        alerts.push(`GSTR-2B mismatch: ${invoice.gstr2b_mismatch_reason || 'amount/date differs from 2B'}`);
    }

    for (const portal of [state.supplierPortalCheck, state.recipientPortalCheck]) {
        if (!portal || isPortalOutageAssessment(portal)) continue;
        if (Array.isArray(portal.issues) && portal.issues.length) {
            portal.issues.forEach((msg) => alerts.push(`GSTN portal: ${msg}`));
        } else if (portal.reason === 'invalid_format') {
            alerts.push('GSTN portal: GSTIN format invalid.');
        }
    }

    if (elements.drawerAlertsCount) {
        elements.drawerAlertsCount.textContent = alerts.length ? `${alerts.length}` : '';
    }
    if (elements.drawerFoldAlerts) {
        elements.drawerFoldAlerts.classList.toggle('has-attention', alerts.length > 0);
    }
    if (elements.drawerFoldPortal) {
        const portalIssues = [state.supplierPortalCheck, state.recipientPortalCheck].some((p) =>
            p && !isPortalOutageAssessment(p) && ((Array.isArray(p.issues) && p.issues.length) || p.reason === 'invalid_format')
        );
        elements.drawerFoldPortal.classList.toggle('has-attention', portalIssues);
        if (portalIssues) elements.drawerFoldPortal.open = true;
    }

    if (alerts.length > 0) {
        alerts.forEach(al => {
            const li = document.createElement('li');
            li.textContent = al;
            elements.auditAlertsList.appendChild(li);
        });
        elements.auditAlertsCard.style.display = 'block';
    } else {
        elements.auditAlertsCard.style.display = 'none';
    }

    renderPrimaryIssue(invoice);
}

function renderLineItcBreakdown(invoice) {
    if (!elements.lineItcCard || !elements.lineItcBody) return;
    const lines = invoice.line_items || [];
    if (!lines.length) {
        elements.lineItcCard.style.display = 'none';
        if (elements.lineItcSummary) elements.lineItcSummary.textContent = '';
        if (elements.drawerFoldItc) elements.drawerFoldItc.style.display = 'none';
        return;
    }
    if (elements.drawerFoldItc) elements.drawerFoldItc.style.display = '';

    const eligGst = (parseFloat(invoice.itc_eligible_cgst) || 0)
        + (parseFloat(invoice.itc_eligible_sgst) || 0)
        + (parseFloat(invoice.itc_eligible_igst) || 0);
    const blockedGst = parseFloat(invoice.itc_blocked_gst) || 0;

    elements.lineItcBody.innerHTML = '';
    lines.forEach(line => {
        const gst = (parseFloat(line.cgst) || 0) + (parseFloat(line.sgst) || 0) + (parseFloat(line.igst) || 0);
        const eligible = line.is_itc_eligible === true || line.is_itc_eligible === 1;
        const claimGst = eligible ? gst : 0;
        const reason = line.itc_ineligibility_reason
            || (line.itc_rule_code ? `Rule ${line.itc_rule_code}` : '');
        const tr = document.createElement('tr');
        tr.style.borderTop = '1px solid rgba(255,255,255,0.04)';
        tr.innerHTML = `
            <td class="py-2 pr-2 text-on-surface">${(line.description || '—').slice(0, 42)}</td>
            <td class="py-2 pr-2 text-on-surface-variant">${line.inferred_category || '—'}</td>
            <td class="py-2 pr-2 text-right font-semibold" style="font-variant-numeric:tabular-nums;">${formatCurrency(gst)}</td>
            <td class="py-2 pr-2 text-right font-semibold ${eligible ? 'text-secondary' : 'text-on-surface-variant'}" style="font-variant-numeric:tabular-nums;">${eligible ? formatCurrency(claimGst) : '—'}</td>
            <td class="py-2 pr-2">
                <span class="text-[11px] font-bold ${eligible ? 'text-secondary' : 'text-error'}">
                    ${eligible ? 'Eligible' : 'Blocked'}
                </span>
            </td>
            <td class="py-2 line-itc-reason">${eligible ? '—' : (reason || 'Sec 17(5)').replace(/</g, '&lt;')}</td>
        `;
        elements.lineItcBody.appendChild(tr);
    });

    if (elements.lineItcSummary) {
        const partial = invoice.itc_partial ? ' · Partial' : '';
        elements.lineItcSummary.textContent =
            `Claimable ${formatCurrency(eligGst)} · Blocked ${formatCurrency(blockedGst)}${partial}`;
    }
    elements.lineItcCard.style.display = 'block';
}

function renderAuditLogs(logs) {
    elements.drawerLogsTimeline.innerHTML = '';
    
    if (!logs || logs.length === 0) {
        elements.drawerLogsTimeline.innerHTML = '<span class="timeline-empty">No manual CA modifications registered.</span>';
        return;
    }

    logs.forEach(l => {
        const item = document.createElement('div');
        item.className = 'timeline-item';
        
        let desc = '';
        if (l.action === "APPROVE") {
            desc = `Approved and verified invoice values.`;
        } else if (l.action === "EDIT_FIELD") {
            desc = `Changed field <strong>${l.field_name}</strong> from <code>"${l.old_value || 'None'}"</code> to <code>"${l.new_value}"</code>`;
        }

        const dateStr = formatTimestamp(l.created_at);

        item.innerHTML = `
            <span class="timeline-user">${l.ca_user}</span>
            <span class="timeline-time">${dateStr}</span>
            <div class="timeline-change">${desc}</div>
        `;
        elements.drawerLogsTimeline.appendChild(item);
    });
}

function closeAuditDrawer() {
    elements.auditDrawer.classList.remove('active');
    elements.auditDrawerOverlay.classList.remove('active');
    state.currentInvoice = null;
}

// ── Document preview (image / PDF) ──

async function setDocumentPreview(filePath) {
    const isPdf = /\.pdf$/i.test(filePath || '');
    state.previewIsPdf = isPdf;

    const img = elements.invoiceDocImg;
    const pdf = elements.invoiceDocPdf;
    const fallback = elements.invoiceDocFallback;
    const download = elements.invoiceDocDownload;

    // Revoke previous blob URL
    if (state._previewObjectUrl) {
        try { URL.revokeObjectURL(state._previewObjectUrl); } catch (_) {}
        state._previewObjectUrl = null;
    }

    let url = '';
    if (filePath) {
        try {
            const res = await apiFetch(`/api/files/${filePath}`);
            if (!res.ok) throw new Error('Unable to load document');
            const blob = await res.blob();
            url = URL.createObjectURL(blob);
            state._previewObjectUrl = url;
        } catch (e) {
            if (typeof showToast === 'function') {
                showToast(e.message || 'Document preview failed', true);
            }
        }
    }

    if (img) {
        img.style.display = isPdf || !url ? 'none' : 'block';
        if (!isPdf && url) img.src = url;
        else img.removeAttribute('src');
    }
    if (pdf) {
        pdf.style.display = isPdf && url ? 'block' : 'none';
        pdf.src = isPdf && url ? url : 'about:blank';
    }
    if (fallback) {
        fallback.style.display = url ? 'flex' : 'none';
    }
    if (download) {
        download.href = url || '#';
        download.textContent = isPdf ? 'Open PDF in new tab' : 'Open original document';
    }

    resetImageTransformations();
}

function getActivePreviewEl() {
    if (state.previewIsPdf && elements.invoiceDocPdf) return elements.invoiceDocPdf;
    return elements.invoiceDocImg;
}

// ── Viewport Image Actions ──

function adjustZoom(amount) {
    state.zoomLevel = Math.max(0.5, Math.min(3.0, state.zoomLevel + amount));
    applyImageTransformations();
}

function rotateImage() {
    state.rotationAngle = (state.rotationAngle + 90) % 360;
    applyImageTransformations();
}

function resetImageTransformations() {
    state.zoomLevel = 1.0;
    state.rotationAngle = 0;
    applyImageTransformations();
}

function applyImageTransformations() {
    const el = getActivePreviewEl();
    if (!el) return;
    el.style.transform = `scale(${state.zoomLevel}) rotate(${state.rotationAngle}deg)`;
}

// Expose for month-picker / table onclick / external callers
window.fetchData = fetchData;
window.openInvoiceAudit = openInvoiceAudit;

// ── Helper Form Mappers ──

function getFormFieldsData() {
    return {
        invoice_number: elements.fInvNum.value.trim(),
        invoice_date: elements.fInvDate.value,
        supplier_name: elements.fSupName.value.trim(),
        supplier_gstin: elements.fSupGst.value.trim().toUpperCase(),
        recipient_name: elements.fRecName.value.trim(),
        recipient_gstin: elements.fRecGst.value.trim().toUpperCase(),
        business_category: elements.fCategory.value,
        supply_type: elements.fSupplyType.value,
        place_of_supply: elements.fPos.value.trim(),
        is_itc_eligible: elements.fItcEligible.value !== 'false',
        itc_ineligibility_reason: elements.fItcEligible.value === 'true' ? null : elements.fItcReason.value.trim(),
        total_taxable_value: parseFloat(elements.fTaxable.value) || 0.0,
        grand_total: parseFloat(elements.fGrand.value) || 0.0,
        total_cgst: parseFloat(elements.fCgst.value) || 0.0,
        total_sgst: parseFloat(elements.fSgst.value) || 0.0,
        total_igst: parseFloat(elements.fIgst.value) || 0.0
    };
}

function toggleItcReasonField(show) {
    if (show) {
        elements.itcReasonGroup.style.display = 'flex';
    } else {
        elements.itcReasonGroup.style.display = 'none';
        elements.fItcReason.value = '';
    }
}

// ── Formatting Utilities ──

function formatCurrency(amount) {
    if (amount === undefined || amount === null) return "₹0.00";
    // Formats into Indian numbering system (Lakh/Crore)
    const formatter = new Intl.NumberFormat('en-IN', {
        style: 'currency',
        currency: 'INR',
        maximumFractionDigits: 2
    });
    return formatter.format(amount);
}

function formatNumber(num) {
    if (num === undefined || num === null) return "0.00";
    return num.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function formatYearMonthLabel(yearMonthStr) {
    // Converts "2026-05" -> "May 2026"
    if (!yearMonthStr || !yearMonthStr.includes('-')) return 'All Months';
    const parts = yearMonthStr.split('-');
    if (parts.length < 2) return yearMonthStr;
    const year = parseInt(parts[0]);
    const month = parseInt(parts[1]);
    if (isNaN(year) || isNaN(month) || month < 1 || month > 12) return yearMonthStr;
    const dt = new Date(year, month - 1, 1);
    return dt.toLocaleDateString('en-US', { month: 'long', year: 'numeric' });
}

/** Compact label for header month picker — avoids truncation ("Sep 2025"). */
function formatYearMonthShort(yearMonthStr) {
    if (!yearMonthStr || !yearMonthStr.includes('-')) return 'All months';
    const parts = yearMonthStr.split('-');
    if (parts.length < 2) return yearMonthStr;
    const year = parseInt(parts[0], 10);
    const month = parseInt(parts[1], 10);
    if (isNaN(year) || isNaN(month) || month < 1 || month > 12) return yearMonthStr;
    const dt = new Date(year, month - 1, 1);
    return dt.toLocaleDateString('en-US', { month: 'short', year: 'numeric' });
}

function formatTimestamp(timestampStr) {
    // Converts "2026-06-05 10:04:39" -> "05 Jun 10:04"
    try {
        const dt = new Date(timestampStr.replace(' ', 'T'));
        if (isNaN(dt.getTime())) return timestampStr;
        return dt.toLocaleDateString('en-US', { day: '2-digit', month: 'short' }) + ' ' + dt.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false });
    } catch (e) {
        return timestampStr;
    }
}

// ── Validation checks ──

function validateGstinString(gstin) {
    const regex = /^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$/;
    return regex.test(gstin.trim().toUpperCase());
}

function validateGstinField(inputElem, validationLabelElem) {
    const val = inputElem.value.trim();
    if (!val) {
        inputElem.className = 'gstin-input';
        validationLabelElem.textContent = '';
        return;
    }
    
    if (validateGstinString(val)) {
        inputElem.className = 'gstin-input valid';
        validationLabelElem.textContent = '✓ GSTIN Format Valid';
        validationLabelElem.style.color = '#33604A';
    } else {
        inputElem.className = 'gstin-input invalid';
        validationLabelElem.textContent = '✗ Invalid GSTIN Format';
        validationLabelElem.style.color = '#8C2F2F';
    }
}

// ── Toast notifier ──
let toastTimeout;
function showToast(message, isError = false) {
    elements.toast.textContent = message;
    elements.toast.className = 'toast';
    
    if (isError) {
        elements.toast.style.background = '#ba1a1a';
        elements.toast.style.boxShadow = '0 8px 24px rgba(186,26,26,0.4)';
    } else {
        elements.toast.style.background = '#4f46e5';
        elements.toast.style.boxShadow = '0 8px 24px rgba(79,70,229,0.4)';
    }
    
    elements.toast.classList.add('active');
    
    clearTimeout(toastTimeout);
    toastTimeout = setTimeout(() => {
        elements.toast.classList.remove('active');
    }, 3500);
}


// ── AI Chatbot Controller ──

document.addEventListener('DOMContentLoaded', () => {
    const chatTriggerBtn = document.getElementById('chatbot-trigger-btn');
    const chatPanel = document.getElementById('chatbot-panel');
    const closeChatBtn = document.getElementById('close-chatbot-btn');
    const chatForm = document.getElementById('chatbot-form');
    const chatInput = document.getElementById('chatbot-input');
    const chatMessages = document.getElementById('chatbot-messages');
    
    if (!chatTriggerBtn || !chatPanel || !closeChatBtn || !chatForm || !chatInput || !chatMessages) {
        console.error("Chatbot DOM elements not found.");
        return;
    }

    // Toggle panel visibility
    chatTriggerBtn.addEventListener('click', () => {
        const isClosed = chatPanel.classList.contains('opacity-0');
        
        if (isClosed) {
            chatPanel.classList.remove('opacity-0', 'pointer-events-none', 'translate-y-4');
            chatPanel.classList.add('opacity-100', 'pointer-events-all', 'translate-y-0');
            chatMessages.scrollTop = chatMessages.scrollHeight;
            chatInput.focus();
        } else {
            chatPanel.classList.add('opacity-0', 'pointer-events-none', 'translate-y-4');
            chatPanel.classList.remove('opacity-100', 'pointer-events-all', 'translate-y-0');
        }
    });

    closeChatBtn.addEventListener('click', () => {
        chatPanel.classList.add('opacity-0', 'pointer-events-none', 'translate-y-4');
        chatPanel.classList.remove('opacity-100', 'pointer-events-all', 'translate-y-0');
    });

    // Handle chips clicks using event delegation
    chatMessages.addEventListener('click', (e) => {
        if (e.target && e.target.classList.contains('chat-chip')) {
            const query = e.target.textContent;
            chatInput.value = query;
            chatForm.dispatchEvent(new Event('submit'));
        }
    });

    // Send chat message
    chatForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const text = chatInput.value.trim();
        if (!text) return;

        // Clear input
        chatInput.value = '';

        // Render user message bubble
        appendMessage('user', text);

        // Render typing indicator
        const typingId = appendTypingIndicator();
        chatMessages.scrollTop = chatMessages.scrollHeight;

        try {
            const response = await apiFetch('/api/chat', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    message: text,
                    client_phone: state.selectedClientPhone || null
                })
            });
            const data = await response.json();
            
            // Remove typing indicator
            removeTypingIndicator(typingId);

            if (response.ok && data.response) {
                appendMessage('bot', data.response, data.steps || []);
            } else {
                appendMessage('bot', `Error: ${data.detail || 'Failed to generate response.'}`);
            }
        } catch (err) {
            console.error("Chatbot API failed:", err);
            removeTypingIndicator(typingId);
            appendMessage('bot', "Network error. Please check if server is running.");
        }
        
        chatMessages.scrollTop = chatMessages.scrollHeight;
    });

    function appendMessage(sender, messageText, steps) {
        const bubble = document.createElement('div');
        
        if (sender === 'user') {
            bubble.className = 'flex items-start gap-2.5 max-w-[85%] ml-auto justify-end';
            bubble.innerHTML = `
                <div class="rounded-[12px] px-4 py-2.5 bg-primary text-[13px] leading-relaxed text-on-primary font-semibold">
                    ${escapeHTML(messageText)}
                </div>
            `;
        } else {
            bubble.className = 'flex items-start gap-2.5 max-w-[90%]';
            
            // Format simple markdown (bold text, bullet points)
            const formatted = formatMarkdown(messageText);
            let stepsHtml = '';
            if (steps && steps.length) {
                const items = steps.map(s => {
                    const tool = escapeHTML(s.tool || 'tool');
                    return `<span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-md bg-primary/10 border border-primary/20 text-primary text-[10px] font-semibold">${tool}</span>`;
                }).join(' ');
                stepsHtml = `<div class="mt-2 flex flex-wrap gap-1.5"><span class="text-[10px] text-on-surface-variant font-semibold uppercase tracking-wider mr-1">Tools</span>${items}</div>`;
            }
            
            bubble.innerHTML = `
                <div class="w-7 h-7 rounded-[8px] bg-primary/15 text-primary flex items-center justify-center shrink-0 border border-primary/20">
                    <span class="material-symbols-outlined text-[14px]">psychology</span>
                </div>
                <div class="rounded-[12px] px-4 py-2.5 bg-surface-container-high text-[13px] leading-relaxed text-on-surface">
                    ${formatted}
                    ${stepsHtml}
                </div>
            `;
        }
        
        chatMessages.appendChild(bubble);
    }

    function appendTypingIndicator() {
        const id = 'typing_' + Date.now();
        const indicator = document.createElement('div');
        indicator.id = id;
        indicator.className = 'flex items-start gap-2.5 max-w-[85%]';
        indicator.innerHTML = `
            <div class="w-7 h-7 rounded-full bg-primary/20 flex items-center justify-center text-[12px] shrink-0 text-center">🤖</div>
            <div class="rounded-2xl px-4 py-2.5 bg-surface-container-high text-[13px] text-on-surface-variant shadow-md flex items-center gap-1">
                <span class="w-1.5 h-1.5 rounded-full bg-on-surface-variant/60 animate-bounce" style="animation-delay: 0ms"></span>
                <span class="w-1.5 h-1.5 rounded-full bg-on-surface-variant/60 animate-bounce" style="animation-delay: 150ms"></span>
                <span class="w-1.5 h-1.5 rounded-full bg-on-surface-variant/60 animate-bounce" style="animation-delay: 300ms"></span>
            </div>
        `;
        chatMessages.appendChild(indicator);
        return id;
    }

    function removeTypingIndicator(id) {
        const indicator = document.getElementById(id);
        if (indicator) {
            indicator.remove();
        }
    }

    function escapeHTML(text) {
        return text
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    }

    function formatMarkdown(text) {
        // Escape HTML tags to prevent XSS
        let clean = escapeHTML(text);
        
        // Bold formatting **text** -> <strong>text</strong>
        clean = clean.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
        clean = clean.replace(/\*(.*?)\*/g, '<strong>$1</strong>');
        
        // Inline code formatting `code` -> <code>code</code>
        clean = clean.replace(/`(.*?)`/g, '<code class="bg-[#0E131A] px-1 py-0.5 rounded text-[11px] font-mono border border-outline-variant">$1</code>');
        
        // Bullet lists
        const lines = clean.split('\n');
        let inList = false;
        let formattedLines = [];
        
        lines.forEach(line => {
            const trimmed = line.trim();
            if (trimmed.startsWith('•') || trimmed.startsWith('-') || trimmed.startsWith('*')) {
                const content = trimmed.substring(1).trim();
                if (!inList) {
                    formattedLines.push('<ul class="list-disc pl-5 space-y-1 mt-1 mb-1">');
                    inList = true;
                }
                formattedLines.push(`<li>${content}</li>`);
            } else {
                if (inList) {
                    formattedLines.push('</ul>');
                    inList = false;
                }
                formattedLines.push(line);
            }
        });
        if (inList) {
            formattedLines.push('</ul>');
        }
        
        return formattedLines.join('<br>');
    }
});


// --- Income Tax Phase 1 module ---
const ITR_REVIEW_FIELDS = [
    { key: 'gross_salary', label: 'Gross salary', inputId: 'itr-gross', money: true },
    { key: 'exemptions', label: 'Exemptions (u/s 10)', inputId: 'itr-exemptions', money: true },
    { key: 'other_income', label: 'Other income', inputId: 'itr-other', money: true },
    { key: 'deductions_80c', label: '80C deductions', inputId: 'itr-80c', money: true },
    { key: 'tds', label: 'TDS', inputId: 'itr-tds', money: true },
    { key: 'pan', label: 'PAN', inputId: 'itr-pan-input', money: false },
];

function itrNumInput(id) {
    return Number((document.getElementById(id) || {}).value || 0);
}

function itrAmountsClose(a, b, tol = 1) {
    return Math.abs(Number(a || 0) - Number(b || 0)) <= tol;
}

function parseItrExtracted(row) {
    if (!row) return null;
    if (row.extracted && typeof row.extracted === 'object') return row.extracted;
    if (row.extracted_json) {
        try { return JSON.parse(row.extracted_json); } catch (_) { return null; }
    }
    return null;
}

function getItrSavedFieldValues() {
    const pan = ((document.getElementById('itr-pan-input') || {}).value || '').trim().toUpperCase();
    return {
        gross_salary: itrNumInput('itr-gross'),
        exemptions: itrNumInput('itr-exemptions'),
        other_income: itrNumInput('itr-other'),
        deductions_80c: itrNumInput('itr-80c'),
        tds: itrNumInput('itr-tds'),
        pan,
    };
}

function formatItrReviewValue(field, value) {
    if (value === null || value === undefined || value === '') return '—';
    if (field.money) return inr(value);
    return String(value);
}

function compareItrField(field, extracted, saved) {
    const exVal = extracted ? extracted[field.key] : null;
    const hasExtract = exVal !== null && exVal !== undefined && exVal !== '';
    if (!hasExtract) return { status: 'missing', label: 'Not in extract' };
    if (field.money) {
        if (itrAmountsClose(exVal, saved[field.key])) return { status: 'match', label: 'Match' };
        return { status: 'diff', label: 'Mismatch' };
    }
    const a = String(exVal || '').trim().toUpperCase();
    const b = String(saved[field.key] || '').trim().toUpperCase();
    if (!a && !b) return { status: 'missing', label: 'Not in extract' };
    if (a === b) return { status: 'match', label: 'Match' };
    return { status: 'diff', label: 'Mismatch' };
}

function clearItrFieldHighlights() {
    document.querySelectorAll('.itr-field[data-itr-field]').forEach((el) => {
        el.classList.remove('is-extract-diff');
    });
}

function updateItrFieldHighlights(extracted) {
    clearItrFieldHighlights();
    if (!extracted) return;
    const saved = getItrSavedFieldValues();
    ITR_REVIEW_FIELDS.forEach((field) => {
        const cmp = compareItrField(field, extracted, saved);
        if (cmp.status !== 'diff') return;
        const wrap = document.querySelector(`.itr-field[data-itr-field="${field.key}"]`);
        if (wrap) wrap.classList.add('is-extract-diff');
    });
}

function renderForm16Review(row) {
    const panel = document.getElementById('itr-form16-review');
    const body = document.getElementById('itr-review-body');
    const meta = document.getElementById('itr-review-meta');
    const summaryBadge = document.getElementById('itr-review-summary-badge');
    const viewLink = document.getElementById('itr-view-form16-link');

    const extracted = parseItrExtracted(row);
    state.itrExtracted = extracted;

    const docs = row && row.documents ? row.documents : [];
    const form16Doc = docs.find((d) => (d.doc_type || '').toLowerCase() === 'form16') || docs[0];
    state.itrLatestForm16Path = form16Doc && form16Doc.file_path ? form16Doc.file_path : null;

    if (!panel || !body) return;

    if (!extracted) {
        panel.style.display = 'none';
        clearItrFieldHighlights();
        if (viewLink) viewLink.style.display = 'none';
        updateItrOnboardingUI();
        return;
    }

    panel.style.display = '';
    panel.classList.toggle('is-verified', (row.status || '') === 'approved');

    if (meta) {
        const bits = [];
        if (extracted.employee_name) bits.push(`Employee: <strong>${extracted.employee_name}</strong>`);
        if (extracted.employer_name) bits.push(`Employer: ${extracted.employer_name}`);
        if (extracted.financial_year) bits.push(`FY on Form 16: ${extracted.financial_year}`);
        if (form16Doc && form16Doc.original_filename) bits.push(`File: ${form16Doc.original_filename}`);
        meta.innerHTML = bits.length ? bits.join(' · ') : 'Form 16 uploaded — verify amounts below.';
    }

    if (viewLink) {
        viewLink.style.display = state.itrLatestForm16Path ? '' : 'none';
    }

    const saved = getItrSavedFieldValues();
    let diffCount = 0;
    body.innerHTML = '';

    ITR_REVIEW_FIELDS.forEach((field) => {
        const cmp = compareItrField(field, extracted, saved);
        if (cmp.status === 'diff') diffCount += 1;

        const tr = document.createElement('tr');
        const exDisplay = formatItrReviewValue(field, extracted[field.key]);
        const savedDisplay = field.money ? inr(saved[field.key]) : (saved[field.key] || '—');

        const useBtn = cmp.status === 'diff'
            ? `<button type="button" class="itr-review-btn is-secondary" style="height:28px;padding:0 8px;font-size:11px;" data-itr-use="${field.key}">Use extracted</button>`
            : '';

        tr.innerHTML = `
            <td>${field.label}</td>
            <td class="num">${exDisplay}</td>
            <td class="num">${savedDisplay}</td>
            <td><span class="itr-review-status is-${cmp.status}">${cmp.label}</span></td>
            <td class="text-right">${useBtn}</td>
        `;
        body.appendChild(tr);
    });

    if (summaryBadge) {
        if (diffCount === 0) {
            summaryBadge.className = 'itr-review-status is-match';
            summaryBadge.textContent = 'All fields match';
        } else {
            summaryBadge.className = 'itr-review-status is-diff';
            summaryBadge.textContent = `${diffCount} mismatch${diffCount === 1 ? '' : 'es'} — review`;
        }
    }

    body.querySelectorAll('[data-itr-use]').forEach((btn) => {
        btn.addEventListener('click', () => {
            applyForm16ExtractedField(btn.getAttribute('data-itr-use'));
        });
    });

    updateItrFieldHighlights(extracted);
    updateItrOnboardingUI();
}

function itrReviewContext(status) {
    return {
        extracted: state.itrExtracted,
        documents: state.itrLatestForm16Path ? [{ file_path: state.itrLatestForm16Path }] : [],
        status: status || ((document.getElementById('itr-status-badge') || {}).textContent || ''),
    };
}

function applyForm16ExtractedField(key) {
    const ex = state.itrExtracted;
    if (!ex || !key) return;
    const field = ITR_REVIEW_FIELDS.find((f) => f.key === key);
    if (!field) return;
    const el = document.getElementById(field.inputId);
    if (!el) return;
    const val = ex[key];
    if (val === null || val === undefined || val === '') return;
    el.value = field.money ? Number(val) : String(val).toUpperCase();
    renderForm16Review(itrReviewContext());
    showToast(`Applied extracted ${field.label.toLowerCase()}`);
}

function applyForm16ExtractedAll() {
    const ex = state.itrExtracted;
    if (!ex) {
        showToast('No Form 16 extraction to apply', true);
        return;
    }
    ITR_REVIEW_FIELDS.forEach((field) => {
        const val = ex[field.key];
        if (val === null || val === undefined || val === '') return;
        const el = document.getElementById(field.inputId);
        if (!el) return;
        el.value = field.money ? Number(val) : String(val).toUpperCase();
    });
    const fy = ex.financial_year;
    const fyEl = document.getElementById('itr-fy-select');
    if (fy && fyEl) {
        if (![...fyEl.options].some((o) => o.value === fy)) {
            const opt = document.createElement('option');
            opt.value = fy;
            opt.textContent = fy;
            fyEl.appendChild(opt);
        }
        fyEl.value = fy;
    }
    renderForm16Review(itrReviewContext());
    showToast('Applied all extracted values — click Save & estimate to confirm');
}

async function openForm16Document() {
    const path = state.itrLatestForm16Path;
    if (!path) {
        showToast('No Form 16 file on this return', true);
        return;
    }
    try {
        const res = await apiFetch(`/api/files/${path}`);
        if (!res.ok) throw new Error('Unable to load Form 16');
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const w = window.open(url, '_blank');
        if (!w) showToast('Allow pop-ups to view Form 16', true);
        setTimeout(() => { try { URL.revokeObjectURL(url); } catch (_) {} }, 60000);
    } catch (e) {
        showToast(e.message || 'Could not open Form 16', true);
    }
}

function inr(n) {
    const v = Number(n || 0);
    return "\u20B9" + v.toLocaleString("en-IN", { maximumFractionDigits: 2 });
}

function updateItrOnboardingUI() {
    const onboarding = document.getElementById('itr-onboarding-panel');
    const banner = document.getElementById('itr-return-banner');
    const bannerText = document.getElementById('itr-return-banner-text');
    const stepOpen = document.getElementById('itr-step-open');
    const stepUpload = document.getElementById('itr-step-upload');
    const stepReview = document.getElementById('itr-step-review');
    const hasReturn = !!state.itrReturnId;
    const hasExtract = !!state.itrExtracted;
    const hasEstimate = !!state.itrEstimate;

    if (onboarding) onboarding.style.display = hasReturn && hasExtract ? 'none' : '';
    if (banner) banner.style.display = hasReturn ? 'flex' : 'none';
    if (bannerText && hasReturn) {
        const fy = (document.getElementById('itr-fy-select') || {}).value || '';
        bannerText.textContent = hasExtract
            ? `FY ${fy} return open — review extraction below, then Save & estimate.`
            : `FY ${fy} return open — upload Form 16 or enter amounts manually.`;
    }

    const setStep = (el, mode) => {
        if (!el) return;
        el.classList.remove('is-active', 'is-done');
        if (mode) el.classList.add(mode);
    };
    if (!hasReturn) {
        setStep(stepOpen, 'is-active');
        setStep(stepUpload, '');
        setStep(stepReview, '');
    } else if (!hasExtract) {
        setStep(stepOpen, 'is-done');
        setStep(stepUpload, 'is-active');
        setStep(stepReview, '');
    } else if (!hasEstimate) {
        setStep(stepOpen, 'is-done');
        setStep(stepUpload, 'is-done');
        setStep(stepReview, 'is-active');
    } else {
        setStep(stepOpen, 'is-done');
        setStep(stepUpload, 'is-done');
        setStep(stepReview, 'is-done');
    }
}

async function tryLoadItrReturnForFy(silent = true) {
    if (!state.selectedClientPhone || state.activeModule !== 'itr') {
        updateItrOnboardingUI();
        return false;
    }
    const fy = (document.getElementById('itr-fy-select') || {}).value || '2025-26';
    try {
        const response = await apiFetch(
            `/api/itr/returns?client_phone=${encodeURIComponent(state.selectedClientPhone)}`
        );
        const list = await readJsonOrThrow(response, 'Could not list ITR returns');
        const match = (list || []).find((r) => r.financial_year === fy);
        if (!match) {
            updateItrOnboardingUI();
            return false;
        }
        const detail = await apiFetch(`/api/itr/returns/${match.id}`);
        const full = await readJsonOrThrow(detail, 'Could not load ITR return');
        fillItrFormFromReturn(full);
        if (!silent) showToast(`Loaded FY ${fy} return`);
        return true;
    } catch (e) {
        if (!silent) showToast(e.message || 'Could not load ITR return', true);
        updateItrOnboardingUI();
        return false;
    }
}

function setModule(module) {
    state.activeModule = module === "itr" ? "itr" : "gst";
    document.body.classList.toggle("module-itr", state.activeModule === "itr");
    const gstBtn = document.getElementById("module-btn-gst");
    const itrBtn = document.getElementById("module-btn-itr");
    if (gstBtn) {
        gstBtn.classList.toggle("is-active", state.activeModule === "gst");
        gstBtn.setAttribute("aria-selected", state.activeModule === "gst" ? "true" : "false");
    }
    if (itrBtn) {
        itrBtn.classList.toggle("is-active", state.activeModule === "itr");
        itrBtn.setAttribute("aria-selected", state.activeModule === "itr" ? "true" : "false");
    }
    try { localStorage.setItem("DASHBOARD_MODULE", state.activeModule); } catch (_) {}
    if (state.activeModule === "itr") {
        const c = getSelectedClient();
        const panEl = document.getElementById("itr-pan-input");
        if (panEl && c && c.pan && !panEl.value) panEl.value = c.pan;
        tryLoadItrReturnForFy(true).catch(() => updateItrOnboardingUI());
    }
}

function resetItrPanelForClient() {
    state.itrReturnId = null;
    state.itrEstimate = null;
    state.itrExtracted = null;
    state.itrLastDocuments = [];
    state.itrLatestForm16Path = null;
    state.itrAisSummary = null;
    state.itrAisReconcile = null;
    const badge = document.getElementById("itr-status-badge");
    if (badge) { badge.style.display = "none"; badge.textContent = ""; }
    ["itr-gross","itr-exemptions","itr-other","itr-80c","itr-tds","itr-advance"].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.value = "0";
    });
    const notes = document.getElementById("itr-notes");
    if (notes) notes.value = "";
    const panEl = document.getElementById("itr-pan-input");
    const c = getSelectedClient();
    if (panEl) panEl.value = (c && c.pan) || "";
    renderItrEstimate(null);
    renderItrDocs([]);
    renderForm16Review(null);
    renderAisReconcile(null);
    setItrActionsEnabled(false);
    updateItrOnboardingUI();
    const hint = document.getElementById("itr-upload-hint");
    if (hint) hint.textContent = state.selectedClientPhone
        ? "Step 1: Click Open / create return above."
        : "Select a client in the sidebar first.";
}

function setItrActionsEnabled(on) {
    ["itr-upload-btn","itr-ais-upload-btn","itr-save-btn","itr-approve-btn","itr-export-btn"].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.disabled = !on;
    });
    const pwRow = document.getElementById("itr-ais-password-row");
    if (pwRow) pwRow.style.display = on ? "" : "none";
}

function renderItrEstimate(est) {
    state.itrEstimate = est || null;
    const empty = document.getElementById("itr-estimate-empty");
    const body = document.getElementById("itr-estimate-body");
    const pref = document.getElementById("itr-preferred");
    if (!est) {
        if (empty) empty.style.display = "";
        if (body) body.style.display = "none";
        if (pref) pref.textContent = "\u2014";
        return;
    }
    if (empty) empty.style.display = "none";
    if (body) body.style.display = "";
    const map = {
        "itr-e-taxable-old": est.taxable_old,
        "itr-e-taxable-new": est.taxable_new,
        "itr-e-net-old": est.net_tax_old,
        "itr-e-net-new": est.net_tax_new,
        "itr-e-pay-old": est.payable_or_refund_old,
        "itr-e-pay-new": est.payable_or_refund_new,
    };
    Object.entries(map).forEach(([id, val]) => {
        const el = document.getElementById(id);
        if (el) el.textContent = inr(val);
    });
    if (pref) pref.textContent = "Preferred: " + String(est.regime_preferred || "\u2014").toUpperCase();
    const disc = document.getElementById("itr-disclaimer");
    if (disc) disc.textContent = est.disclaimer || "";
}

function renderItrDocs(docs) {
    const el = document.getElementById("itr-docs-list");
    if (!el) return;
    const list = docs && docs.length ? docs : (state.itrLastDocuments || []);
    if (!list.length) {
        el.textContent = "No Form 16 uploaded yet.";
        return;
    }
    state.itrLastDocuments = list;
    const latest = list.find((d) => (d.doc_type || '').toLowerCase() === 'form16') || list[0];
    if (latest && latest.file_path) state.itrLatestForm16Path = latest.file_path;
    el.innerHTML = '<div class="font-semibold mb-1">Documents</div>' + list.map((d) => {
        const name = d.original_filename || d.file_path || "file";
        const dtype = (d.doc_type || "file").toLowerCase();
        return "<div>" + dtype + ": " + name + "</div>";
    }).join("");
}

function fillItrFormFromReturn(row) {
    if (!row) return;
    state.itrReturnId = row.id;
    const set = (id, v) => { const el = document.getElementById(id); if (el) el.value = v ?? 0; };
    set("itr-gross", row.gross_salary || 0);
    set("itr-exemptions", row.exemptions || 0);
    set("itr-other", row.other_income || 0);
    set("itr-80c", row.deductions_80c || 0);
    set("itr-tds", row.tds || 0);
    set("itr-advance", row.advance_tax || 0);
    const notes = document.getElementById("itr-notes");
    if (notes) notes.value = row.ca_notes || "";
    const panEl = document.getElementById("itr-pan-input");
    if (panEl && row.pan) panEl.value = row.pan;
    const fyEl = document.getElementById("itr-fy-select");
    if (fyEl && row.financial_year) {
        if (![...fyEl.options].some(o => o.value === row.financial_year)) {
            const opt = document.createElement("option");
            opt.value = row.financial_year;
            opt.textContent = row.financial_year;
            fyEl.appendChild(opt);
        }
        fyEl.value = row.financial_year;
    }
    const badge = document.getElementById("itr-status-badge");
    if (badge) {
        badge.style.display = "";
        badge.textContent = row.status || "draft";
    }
    let est = row.estimate || null;
    if (!est && row.estimate_json) {
        try { est = JSON.parse(row.estimate_json); } catch (_) { est = null; }
    }
    renderItrEstimate(est);
    renderItrDocs(row.documents || state.itrLastDocuments || []);
    renderForm16Review(row);
    const aisBlob = row.ais || null;
    const aisSummary = aisBlob && aisBlob.summary ? aisBlob.summary : aisBlob;
    state.itrAisSummary = aisSummary || null;
    if (aisSummary) {
        // Live reconcile against current form values
        renderAisReconcileFromClient(aisSummary);
    } else {
        renderAisReconcile(null);
    }
    setItrActionsEnabled(true);
    updateItrOnboardingUI();
    const hint = document.getElementById("itr-upload-hint");
    if (hint) hint.textContent = "Form 16 PDF/image · AIS JSON from portal (or scratch/dummy_ais.json).";
}

function formatAisMoney(v) {
    if (v == null || v === "") return "—";
    const n = Number(v);
    if (Number.isNaN(n)) return String(v);
    return "₹" + n.toLocaleString("en-IN", { maximumFractionDigits: 0 });
}

function renderAisReconcile(payload) {
    const panel = document.getElementById("itr-ais-panel");
    const body = document.getElementById("itr-ais-mismatch-body");
    const badge = document.getElementById("itr-ais-status-badge");
    const summaryEl = document.getElementById("itr-ais-summary");
    if (!panel) return;
    if (!payload) {
        panel.style.display = "none";
        state.itrAisReconcile = null;
        return;
    }
    panel.style.display = "";
    state.itrAisReconcile = payload;
    const recon = payload.reconcile || payload;
    const ais = payload.ais || recon.ais || state.itrAisSummary || {};
    const status = recon.status || "review";
    if (badge) {
        badge.textContent =
            status === "match" ? "Match" :
            status === "mismatch" ? "Mismatch" : "Review";
        badge.className = "itr-review-status is-" + (status === "match" ? "match" : "diff");
    }
    if (summaryEl) {
        const bits = [];
        if (ais.pan) bits.push("PAN " + ais.pan);
        if (ais.financial_year) bits.push("FY " + ais.financial_year);
        bits.push("Salary " + formatAisMoney(ais.salary));
        bits.push("TDS " + formatAisMoney(ais.tds_on_salary));
        if (Number(ais.interest_income) > 0) bits.push("Interest " + formatAisMoney(ais.interest_income));
        if (ais.encrypted) bits.push("decrypted portal file");
        summaryEl.textContent = bits.join(" · ");
    }
    const rows = recon.mismatches || [];
    if (!body) return;
    if (!rows.length) {
        body.innerHTML = '<tr><td colspan="5" class="text-on-surface-variant">No mismatches — AIS aligns with saved Form 16 fields.</td></tr>';
        return;
    }
    body.innerHTML = rows.map((m) => {
        const sev = (m.severity || "info").toLowerCase();
        const color = sev === "error" ? "#FF8A8A" : (sev === "warn" ? "#F0B35A" : "#9aa7b5");
        const aisVal = typeof m.ais === "number" ? formatAisMoney(m.ais) : (m.ais ?? "—");
        const retVal = typeof m.return_value === "number" ? formatAisMoney(m.return_value) : (m.return_value ?? "—");
        return `<tr>
            <td class="font-semibold">${m.field || "—"}</td>
            <td class="text-right">${aisVal}</td>
            <td class="text-right">${retVal}</td>
            <td style="color:${color};font-weight:700;text-transform:uppercase;font-size:11px;">${sev}</td>
            <td class="text-on-surface-variant">${m.note || ""}</td>
        </tr>`;
    }).join("");
}

function renderAisReconcileFromClient(aisSummary) {
    // Lightweight client-side preview; server reconcile runs on upload
    if (!aisSummary) {
        renderAisReconcile(null);
        return;
    }
    const num = (id) => Number((document.getElementById(id) || {}).value || 0);
    const pan = ((document.getElementById("itr-pan-input") || {}).value || "").trim().toUpperCase();
    const fy = (document.getElementById("itr-fy-select") || {}).value || "";
    const itrRow = {
        pan,
        financial_year: fy,
        gross_salary: num("itr-gross"),
        tds: num("itr-tds"),
        other_income: num("itr-other"),
    };
    // Call server if we have a return id for authoritative reconcile
    if (state.itrReturnId) {
        apiFetch("/api/itr/returns/" + state.itrReturnId + "/ais/reconcile")
            .then((r) => readJsonOrThrow(r, "AIS reconcile failed"))
            .then((data) => renderAisReconcile(data))
            .catch(() => {
                renderAisReconcile({
                    ais: aisSummary,
                    reconcile: { status: "review", mismatches: [], ais: aisSummary },
                });
            });
        return;
    }
    renderAisReconcile({
        ais: aisSummary,
        reconcile: { status: "review", mismatches: [], ais: aisSummary },
    });
}

async function uploadAis() {
    if (!state.itrReturnId) {
        showToast("Open a return first", true);
        return;
    }
    const input = document.getElementById("itr-ais-input");
    if (!input || !input.files || !input.files[0]) {
        if (input) input.click();
        return;
    }
    const form = new FormData();
    form.append("file", input.files[0]);
    const dob = ((document.getElementById("itr-ais-dob") || {}).value || "").trim();
    const password = ((document.getElementById("itr-ais-password") || {}).value || "").trim();
    if (dob) form.append("dob", dob);
    if (password) form.append("password", password);
    showToast("Uploading AIS…");
    const response = await apiFetch("/api/itr/returns/" + state.itrReturnId + "/ais", {
        method: "POST",
        body: form,
    });
    const data = await readJsonOrThrow(response, "AIS upload failed");
    input.value = "";
    await reloadItrReturnFull();
    renderAisReconcile(data);
    const st = (data.reconcile || {}).status;
    if (st === "match") showToast("AIS matches Form 16 fields");
    else if (st === "mismatch") showToast("AIS mismatches found — review the table", true);
    else showToast("AIS uploaded — review comparison");
}


async function openOrCreateItrReturn() {
    if (!state.selectedClientPhone) {
        showToast("Select a client first", true);
        return;
    }
    const openBtn = document.getElementById("itr-open-btn");
    if (openBtn) {
        openBtn.disabled = true;
        openBtn.textContent = "Opening…";
    }
    try {
        const fy = (document.getElementById("itr-fy-select") || {}).value || "2025-26";
        const pan = ((document.getElementById("itr-pan-input") || {}).value || "").trim().toUpperCase();
        const response = await apiFetch("/api/itr/returns", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                client_phone: state.selectedClientPhone,
                financial_year: fy,
                pan: pan || null,
            }),
        });
        const row = await readJsonOrThrow(response, "Could not open ITR return");
        const detail = await apiFetch("/api/itr/returns/" + row.id);
        const full = await readJsonOrThrow(detail, "Could not load ITR return");
        fillItrFormFromReturn(full);
        showToast("Return ready for FY " + full.financial_year + " — upload Form 16 or edit fields");
    } finally {
        if (openBtn) {
            openBtn.disabled = false;
            openBtn.textContent = "Open / create return";
        }
    }
}

async function reloadItrReturnFull() {
    if (!state.itrReturnId) return null;
    const detail = await apiFetch("/api/itr/returns/" + state.itrReturnId);
    const full = await readJsonOrThrow(detail, "Reload failed");
    fillItrFormFromReturn(full);
    return full;
}

async function saveItrEstimate() {
    if (!state.itrReturnId) return;
    const num = (id) => Number((document.getElementById(id) || {}).value || 0);
    const pan = ((document.getElementById("itr-pan-input") || {}).value || "").trim().toUpperCase();
    const body = {
        gross_salary: num("itr-gross"),
        exemptions: num("itr-exemptions"),
        other_income: num("itr-other"),
        deductions_80c: num("itr-80c"),
        tds: num("itr-tds"),
        advance_tax: num("itr-advance"),
        ca_notes: ((document.getElementById("itr-notes") || {}).value || "").trim(),
        pan: pan || null,
        financial_year: (document.getElementById("itr-fy-select") || {}).value,
    };
    const response = await apiFetch("/api/itr/returns/" + state.itrReturnId, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
    });
    await readJsonOrThrow(response, "Save failed");
    await reloadItrReturnFull();
    showToast("Estimate updated");
}

async function uploadForm16() {
    if (!state.itrReturnId) {
        showToast("Open a return first", true);
        return;
    }
    const input = document.getElementById("itr-form16-input");
    if (!input || !input.files || !input.files[0]) {
        if (input) input.click();
        return;
    }
    const form = new FormData();
    form.append("file", input.files[0]);
    showToast("Uploading Form 16\u2026");
    const response = await apiFetch("/api/itr/returns/" + state.itrReturnId + "/form16?extract=true", {
        method: "POST",
        body: form,
    });
    const data = await readJsonOrThrow(response, "Form 16 upload failed");
    input.value = "";
    const detail = await apiFetch("/api/itr/returns/" + state.itrReturnId);
    const full = await readJsonOrThrow(detail, "Reload failed");
    fillItrFormFromReturn(full);
    if (data.extract_error) {
        showToast(data.hint || data.extract_error, true);
    } else if (parseItrExtracted(full)) {
        showToast("Form 16 extracted — review fields before saving");
    } else {
        showToast("Form 16 uploaded");
    }
}

async function approveItrReturn() {
    if (!state.itrReturnId) return;
    const notes = ((document.getElementById("itr-notes") || {}).value || "").trim();
    const response = await apiFetch("/api/itr/returns/" + state.itrReturnId + "/approve", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ca_notes: notes }),
    });
    const data = await readJsonOrThrow(response, "Approve failed");
    await reloadItrReturnFull();
    showToast("ITR prep approved");
}

async function exportItrDraft() {
    if (!state.itrReturnId) return;
    const headers = apiHeaders();
    const response = await fetch("/api/itr/returns/" + state.itrReturnId + "/export", { headers });
    if (!response.ok) {
        const err = await response.json().catch(() => ({}));
        throw new Error(formatApiDetail(err, "Export failed"));
    }
    const html = await response.text();
    const w = window.open("", "_blank");
    if (!w) {
        showToast("Allow pop-ups to view the export draft", true);
        return;
    }
    w.document.write(html);
    w.document.close();
    showToast("Draft opened \u2014 use Print / Save PDF");
}

function setupItrModule() {
    const gstBtn = document.getElementById("module-btn-gst");
    const itrBtn = document.getElementById("module-btn-itr");
    if (gstBtn) gstBtn.addEventListener("click", () => setModule("gst"));
    if (itrBtn) itrBtn.addEventListener("click", () => setModule("itr"));
    const openBtn = document.getElementById("itr-open-btn");
    if (openBtn) openBtn.addEventListener("click", () => {
        openOrCreateItrReturn().catch(e => showToast(e.message || "Failed", true));
    });
    const saveBtn = document.getElementById("itr-save-btn");
    if (saveBtn) saveBtn.addEventListener("click", () => {
        saveItrEstimate().catch(e => showToast(e.message || "Save failed", true));
    });
    const uploadBtn = document.getElementById("itr-upload-btn");
    if (uploadBtn) uploadBtn.addEventListener("click", () => {
        const input = document.getElementById("itr-form16-input");
        if (input) input.click();
    });
    const fileInput = document.getElementById("itr-form16-input");
    if (fileInput) fileInput.addEventListener("change", () => {
        uploadForm16().catch(e => showToast(e.message || "Upload failed", true));
    });
    const aisBtn = document.getElementById("itr-ais-upload-btn");
    if (aisBtn) aisBtn.addEventListener("click", () => {
        const input = document.getElementById("itr-ais-input");
        if (input) input.click();
    });
    const aisInput = document.getElementById("itr-ais-input");
    if (aisInput) aisInput.addEventListener("change", () => {
        uploadAis().catch(e => showToast(e.message || "AIS upload failed", true));
    });
    const approveBtn = document.getElementById("itr-approve-btn");
    if (approveBtn) approveBtn.addEventListener("click", () => {
        approveItrReturn().catch(e => showToast(e.message || "Approve failed", true));
    });
    const exportBtn = document.getElementById("itr-export-btn");
    if (exportBtn) exportBtn.addEventListener("click", () => {
        exportItrDraft().catch(e => showToast(e.message || "Export failed", true));
    });
    const applyExtractedBtn = document.getElementById("itr-apply-extracted-btn");
    if (applyExtractedBtn) applyExtractedBtn.addEventListener("click", applyForm16ExtractedAll);
    const reviewSaveBtn = document.getElementById("itr-review-save-btn");
    if (reviewSaveBtn) reviewSaveBtn.addEventListener("click", () => {
        saveItrEstimate().catch(e => showToast(e.message || "Save failed", true));
    });
    const viewForm16Link = document.getElementById("itr-view-form16-link");
    if (viewForm16Link) {
        viewForm16Link.addEventListener("click", (e) => {
            e.preventDefault();
            openForm16Document().catch(err => showToast(err.message || "Open failed", true));
        });
    }
    ITR_REVIEW_FIELDS.forEach((field) => {
        const el = document.getElementById(field.inputId);
        if (!el) return;
        el.addEventListener("input", () => {
            if (!state.itrExtracted) return;
            renderForm16Review(itrReviewContext());
        });
    });
    const fySelect = document.getElementById("itr-fy-select");
    if (fySelect) {
        fySelect.addEventListener("change", () => {
            resetItrPanelForClient();
            const c = getSelectedClient();
            const panEl = document.getElementById("itr-pan-input");
            if (panEl && c && c.pan) panEl.value = c.pan;
            tryLoadItrReturnForFy(true).catch(() => updateItrOnboardingUI());
        });
    }
    let saved = "gst";
    try { saved = localStorage.getItem("DASHBOARD_MODULE") || "gst"; } catch (_) {}
    setModule(saved);
    resetItrPanelForClient();
    updateItrOnboardingUI();
}
