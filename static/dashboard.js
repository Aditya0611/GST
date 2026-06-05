// dashboard.js — CA Review Dashboard State Manager & Client Controller

// ── Application State ──
const state = {
    clients: [],
    selectedClientPhone: '',
    selectedMonth: '', // Empty = show all months
    invoices: [],
    activeFilter: 'all',
    searchQuery: '',
    
    // Viewport state for image viewer
    currentInvoice: null,
    zoomLevel: 1.0,
    rotationAngle: 0
};

// ── DOM References ──
const elements = {
    clientSelect: document.getElementById('client-select'),
    syncBtn: document.getElementById('sync-btn'),
    globalSearch: document.getElementById('global-search'),
    
    // KPI metrics
    kpiPendingTrend: document.getElementById('kpi-pending-trend'),
    kpiPendingCount: document.getElementById('kpi-pending-count'),
    kpiPendingSubtext: document.getElementById('kpi-pending-subtext'),
    kpiSalesTotal: document.getElementById('kpi-sales-total'),
    kpiSalesSubtext: document.getElementById('kpi-sales-subtext'),
    kpiItcTotal: document.getElementById('kpi-itc-total'),
    kpiItcSubtext: document.getElementById('kpi-itc-subtext'),
    
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
    
    // Image Viewport
    invoiceDocImg: document.getElementById('invoice-doc-img'),
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
    drawerLogsTimeline: document.getElementById('drawer-logs-timeline'),
    
    // GSTR Modal
    navGstrBtn: document.getElementById('nav-gstr-btn'),
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
    
    // Toast
    toast: document.getElementById('toast')
};

// ── App Init ──
window.addEventListener('DOMContentLoaded', () => {
    setupEventListeners();
    fetchClients();
});

// ── Event Handlers Hookup ──
function setupEventListeners() {
    // Client selection change
    elements.clientSelect.addEventListener('change', (e) => {
        state.selectedClientPhone = e.target.value;
        fetchData();
    });

    // Sync portal mock button click
    elements.syncBtn.addEventListener('click', triggerSync);

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
        toggleItcReasonField(e.target.value === 'false');
    });

    // Input validations for GSTIN during manual edits
    elements.fSupGst.addEventListener('input', (e) => validateGstinField(e.target, elements.fSupGstVal));
    elements.fRecGst.addEventListener('input', (e) => validateGstinField(e.target, elements.fRecGstVal));

    // Form Action buttons
    elements.drawerSaveBtn.addEventListener('click', saveInvoiceProgress);
    elements.drawerApproveBtn.addEventListener('click', verifyAndApproveInvoice);

    // GSTR Modal hooks
    elements.navGstrBtn.addEventListener('click', openGstrModal);
    elements.closeGstrModalBtn.addEventListener('click', closeGstrModal);
    elements.cancelGstrModalBtn.addEventListener('click', closeGstrModal);
    elements.gstrModalOverlay.addEventListener('click', closeGstrModal);
    elements.exportGstrJsonBtn.addEventListener('click', compileGstrDownload);
    elements.modalGstrMonth.addEventListener('change', updateGstrModalPreviews);
    
    // Keyboard shortcuts
    window.addEventListener('keydown', (e) => {
        // Ctrl+K searches
        if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
            e.preventDefault();
            elements.globalSearch.focus();
        }
        // ESC closes drawer/modal
        if (e.key === 'Escape') {
            closeAuditDrawer();
            closeGstrModal();
        }
    });
}

// ── API Functions ──

async function fetchClients() {
    try {
        const response = await fetch('/api/clients');
        const clients = await response.json();
        
        state.clients = clients;
        elements.clientSelect.innerHTML = '';
        
        if (clients.length === 0) {
            elements.clientSelect.innerHTML = '<option value="">No Clients Registered</option>';
            return;
        }

        clients.forEach(c => {
            const opt = document.createElement('option');
            opt.value = c.phone_number;
            opt.textContent = `${c.name} (${c.gstin || 'No GSTIN'})`;
            elements.clientSelect.appendChild(opt);
        });

        // Set default values
        state.selectedClientPhone = clients[0].phone_number;
        elements.clientSelect.value = state.selectedClientPhone;
        
        fetchData();
    } catch (e) {
        console.error("Failed to load clients:", e);
        showToast("Error loading clients profiles.", true);
    }
}

async function fetchData() {
    if (!state.selectedClientPhone) return;

    try {
        renderTableLoadingState();
        
        // Build query params, omitting month if empty
        const monthParam = state.selectedMonth ? `&month=${state.selectedMonth}` : '';
        
        // 1. Fetch Invoices list for client
        const invResponse = await fetch(`/api/invoices?client_phone=${state.selectedClientPhone}${monthParam}`);
        const invoices = await invResponse.json();
        state.invoices = invoices;
        
        // 2. Fetch Metrics for selected period (use current month if none selected)
        const metricsMonth = state.selectedMonth || new Date().toISOString().slice(0, 7);
        const metResponse = await fetch(`/api/metrics?client_phone=${state.selectedClientPhone}&month=${metricsMonth}`);
        const metrics = await metResponse.json();

        // 3. Populate widgets
        updateDashboardMetrics(metrics);
        renderInvoiceTable();
    } catch (e) {
        console.error("Failed to fetch dashboard data:", e);
        showToast("Failed to load dashboard metrics.", true);
    }
}

async function openInvoiceAudit(invoiceId) {
    try {
        const response = await fetch(`/api/invoices/${invoiceId}`);
        const invoice = await response.json();
        
        state.currentInvoice = invoice;
        state.zoomLevel = 1.0;
        state.rotationAngle = 0;
        
        // Update document viewport image path
        // Serving path directly from /storage/ static mount
        elements.invoiceDocImg.src = `/storage/${invoice.file_path}`;
        resetImageTransformations();

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
        
        elements.fItcEligible.value = String(invoice.is_itc_eligible);
        toggleItcReasonField(!invoice.is_itc_eligible);
        elements.fItcReason.value = invoice.itc_ineligibility_reason || '';

        elements.fTaxable.value = invoice.total_taxable_value || 0;
        elements.fGrand.value = invoice.grand_total || 0;
        elements.fCgst.value = invoice.total_cgst || 0;
        elements.fSgst.value = invoice.total_sgst || 0;
        elements.fIgst.value = invoice.total_igst || 0;

        // Render validation errors (warnings checklist)
        renderAuditAlerts(invoice);

        // Render change history timeline log
        renderAuditLogs(invoice.ca_action_logs);

        // Slide drawer open
        elements.auditDrawer.classList.add('active');
        elements.auditDrawerOverlay.classList.add('active');
    } catch (e) {
        console.error("Failed to load invoice details:", e);
        showToast("Error loading invoice audit details.", true);
    }
}

async function saveInvoiceProgress() {
    if (!state.currentInvoice) return;
    
    const fields = getFormFieldsData();
    try {
        const response = await fetch(`/api/invoices/${state.currentInvoice.id}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                ca_user: "CA Admin",
                fields: fields
            })
        });
        const result = await response.json();
        
        if (result.status === "success") {
            showToast("Invoice fields saved successfully.");
            // Reload details inside drawer to reflect override timeline logs
            openInvoiceAudit(state.currentInvoice.id);
            // Refresh main window registers
            fetchData();
        }
    } catch (e) {
        console.error("Failed to save progress:", e);
        showToast("Error saving invoice modifications.", true);
    }
}

async function verifyAndApproveInvoice() {
    if (!state.currentInvoice) return;

    // Save fields progress first
    const fields = getFormFieldsData();
    try {
        // Save
        await fetch(`/api/invoices/${state.currentInvoice.id}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                ca_user: "CA Admin",
                fields: fields
            })
        });

        // Approve
        const approveResponse = await fetch(`/api/invoices/${state.currentInvoice.id}/approve`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ca_user: "CA Admin" })
        });
        const result = await approveResponse.json();

        if (result.status === "success") {
            showToast("Invoice approved & finalized for GSTR compilation!");
            closeAuditDrawer();
            fetchData();
        }
    } catch (e) {
        console.error("Failed to approve invoice:", e);
        showToast("Verification approval failed.", true);
    }
}

// ── GSTR Compiling ──

async function openGstrModal() {
    if (!state.selectedClientPhone) {
        showToast("Select a client first.", true);
        return;
    }
    
    elements.modalGstrMonth.value = state.selectedMonth;
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
        const response = await fetch(`/api/metrics?client_phone=${state.selectedClientPhone}&month=${month}`);
        const m = await response.json();

        elements.prevSales.textContent = formatCurrency(m.sales_taxable);
        elements.prevTax.textContent = formatCurrency(m.sales_gst_liability);
        elements.prevItc.textContent = formatCurrency(m.itc_claimed);
    } catch (e) {
        console.error("Error updating preview aggregates:", e);
    }
}

async function compileGstrDownload() {
    const month = elements.modalGstrMonth.value;
    const type = elements.modalGstrType.value;
    
    try {
        showToast("Compiling returns offline utility file...");
        const response = await fetch(`/api/gstr/export?client_phone=${state.selectedClientPhone}&month=${month}&type=${type}`);
        const data = await response.json();
        
        // Trigger client browser download of JSON file
        const dataStr = "data:text/json;charset=utf-8," + encodeURIComponent(JSON.stringify(data, null, 2));
        const downloadAnchor = document.createElement('a');
        downloadAnchor.setAttribute("href", dataStr);
        downloadAnchor.setAttribute("download", `${type}_${state.selectedClientPhone}_${month.replace('-', '')}.json`);
        document.body.appendChild(downloadAnchor);
        downloadAnchor.click();
        downloadAnchor.remove();
        
        showToast("Utility JSON downloaded successfully!");
        closeGstrModal();
    } catch (e) {
        console.error("GSTR Export failed:", e);
        showToast("Filing compilation error.", true);
    }
}

// ── Sync Portal simulated action ──
function triggerSync() {
    elements.syncBtn.classList.add('loading');
    elements.syncBtn.disabled = true;
    showToast("Synchronizing values with GST portal...");

    setTimeout(() => {
        elements.syncBtn.classList.remove('loading');
        elements.syncBtn.disabled = false;
        showToast("GST portal data synchronized successfully.");
        fetchData(); // Reload
    }, 2000);
}

// ── Local UI Rendering & Helpers ──

function updateDashboardMetrics(metrics) {
    // KPI Numbers
    elements.kpiPendingCount.textContent = metrics.pending_review;
    elements.kpiPendingSubtext.textContent = `${metrics.pending_review} pending CA approval`;
    elements.kpiPendingTrend.textContent = `+${metrics.pending_review} total`;
    
    elements.kpiSalesTotal.textContent = formatCurrency(metrics.sales_taxable);
    elements.kpiSalesSubtext.textContent = `${formatCurrency(metrics.sales_taxable)} this period`;
    
    elements.kpiItcTotal.textContent = formatCurrency(metrics.itc_claimed);
    elements.kpiItcSubtext.textContent = `${formatCurrency(metrics.itc_claimed)} reconciled`;

    // Filing Status bars
    elements.filingStatusTitle.textContent = `FILING STATUS — ${formatYearMonthLabel(state.selectedMonth)}`;
    
    if (metrics.pending_review > 0) {
        elements.statusGstr1Val.textContent = "Action Required";
        elements.statusGstr1Val.className = "status-label text-orange";
        elements.statusGstr3bVal.textContent = "Hold";
        elements.statusGstr3bVal.className = "status-label text-red";
    } else {
        elements.statusGstr1Val.textContent = "Ready to file";
        elements.statusGstr1Val.className = "status-label text-green";
        elements.statusGstr3bVal.textContent = "Ready";
        elements.statusGstr3bVal.className = "status-label text-green";
    }
}

function renderTableLoadingState() {
    elements.invoiceRowsBody.innerHTML = `
        <tr>
            <td colspan="8" class="empty-state">
                <i class="fa-solid fa-spinner fa-spin loading-icon"></i>
                <p>Loading invoice records...</p>
            </td>
        </tr>
    `;
}

function renderInvoiceTable() {
    let filtered = [...state.invoices];

    // 1. Tab Status filters
    if (state.activeFilter === "approved") {
        filtered = filtered.filter(inv => inv.is_approved === 1 || inv.is_approved === true);
    } else if (state.activeFilter === "pending_review") {
        filtered = filtered.filter(inv => !inv.is_approved);
    } else if (state.activeFilter === "flagged") {
        filtered = filtered.filter(inv => !inv.is_calculation_correct);
    }

    // 2. Search query filters
    if (state.searchQuery) {
        filtered = filtered.filter(inv => 
            (inv.invoice_number && inv.invoice_number.toLowerCase().includes(state.searchQuery)) ||
            (inv.supplier_name && inv.supplier_name.toLowerCase().includes(state.searchQuery)) ||
            (inv.supplier_gstin && inv.supplier_gstin.toLowerCase().includes(state.searchQuery)) ||
            (inv.recipient_gstin && inv.recipient_gstin.toLowerCase().includes(state.searchQuery))
        );
    }

    // Update counts
    elements.recordCountTxt.textContent = `${filtered.length} records · ${formatYearMonthLabel(state.selectedMonth)}`;

    if (filtered.length === 0) {
        elements.invoiceRowsBody.innerHTML = `
            <tr>
                <td colspan="8" class="empty-state">
                    <i class="fa-solid fa-folder-open loading-icon" style="color:var(--text-muted);"></i>
                    <p>No invoices matching your selection</p>
                </td>
            </tr>
        `;
        return;
    }

    elements.invoiceRowsBody.innerHTML = '';
    filtered.forEach(inv => {
        const tr = document.createElement('tr');
        tr.style.cursor = 'pointer';
        tr.addEventListener('click', () => openInvoiceAudit(inv.id));

        // Format dates
        const invDate = inv.invoice_date || 'N/A';
        const taxes = (inv.total_cgst || 0.0) + (inv.total_sgst || 0.0) + (inv.total_igst || 0.0);

        // Status Badge
        let statusBadge = '';
        if (inv.is_approved) {
            statusBadge = '<span class="badge badge-approved"><i class="fa-solid fa-circle-check"></i> Approved</span>';
        } else if (!inv.is_calculation_correct) {
            statusBadge = '<span class="badge badge-flagged"><i class="fa-solid fa-triangle-exclamation"></i> Flagged</span>';
        } else {
            statusBadge = '<span class="badge badge-pending"><i class="fa-solid fa-clock"></i> Pending Review</span>';
        }

        tr.innerHTML = `
            <td>
                <div class="tbl-title">INV-${inv.invoice_number || 'N/A'}</div>
                <div class="tbl-subtitle">Date: ${invDate}</div>
            </td>
            <td>
                <div class="tbl-title">${inv.supplier_name || 'Not Detected'}</div>
                <div class="tbl-subtitle"><span class="tbl-gstin">${inv.supplier_gstin || 'None'}</span></div>
            </td>
            <td>
                <div class="tbl-title">${inv.business_category || 'Other'}</div>
                <div class="tbl-subtitle">${inv.supply_type || 'INTRA-STATE'}</div>
            </td>
            <td><div class="tbl-title">₹${formatNumber(inv.total_taxable_value)}</div></td>
            <td>
                <div class="tbl-title">₹${formatNumber(taxes)}</div>
                <div class="tbl-subtitle" style="font-size:10px;">C:${formatNumber(inv.total_cgst)} S:${formatNumber(inv.total_sgst)} I:${formatNumber(inv.total_igst)}</div>
            </td>
            <td><div class="tbl-title" style="color:var(--color-indigo);">₹${formatNumber(inv.grand_total)}</div></td>
            <td>${statusBadge}</td>
            <td><button class="verify-action-btn">Audit <i class="fa-solid fa-angle-right"></i></button></td>
        `;

        elements.invoiceRowsBody.appendChild(tr);
    });
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
    if (!invoice.is_itc_eligible && invoice.itc_ineligibility_reason) {
        alerts.push(`ITC Claim blocked: ${invoice.itc_ineligibility_reason}`);
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
    elements.invoiceDocImg.style.transform = 'scale(1) rotate(0deg)';
}

function applyImageTransformations() {
    elements.invoiceDocImg.style.transform = `scale(${state.zoomLevel}) rotate(${state.rotationAngle}deg)`;
}

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
        is_itc_eligible: elements.fItcEligible.value === 'true',
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
    const [year, month] = yearMonthStr.split('-');
    const dt = new Date(year, parseInt(month) - 1, 1);
    return dt.toLocaleDateString('en-US', { month: 'long', year: 'numeric' });
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
        validationLabelElem.style.color = 'var(--color-green)';
    } else {
        inputElem.className = 'gstin-input invalid';
        validationLabelElem.textContent = '✗ Invalid GSTIN Format';
        validationLabelElem.style.color = 'var(--color-red)';
    }
}

// ── Toast notifier ──
let toastTimeout;
function showToast(message, isError = false) {
    elements.toast.textContent = message;
    elements.toast.className = 'toast';
    
    if (isError) {
        elements.toast.style.backgroundColor = 'var(--color-red)';
        elements.toast.style.boxShadow = '0 4px 15px var(--color-red-glow)';
    } else {
        elements.toast.style.backgroundColor = 'var(--color-indigo)';
        elements.toast.style.boxShadow = '0 4px 15px var(--color-indigo-glow)';
    }
    
    elements.toast.classList.add('active');
    
    clearTimeout(toastTimeout);
    toastTimeout = setTimeout(() => {
        elements.toast.classList.remove('active');
    }, 3500);
}
