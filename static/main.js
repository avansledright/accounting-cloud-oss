document.addEventListener('DOMContentLoaded', () => {
    // Sidebar toggle (mobile)
    const sidebarToggle = document.querySelector('.sidebar-toggle');
    const sidebar = document.querySelector('.sidebar');
    const sidebarOverlay = document.querySelector('.sidebar-overlay');

    function openSidebar() {
        sidebar?.classList.add('open');
        sidebarOverlay?.classList.add('open');
    }
    function closeSidebar() {
        sidebar?.classList.remove('open');
        sidebarOverlay?.classList.remove('open');
    }

    if (sidebarToggle) {
        sidebarToggle.addEventListener('click', () => {
            sidebar?.classList.contains('open') ? closeSidebar() : openSidebar();
        });
    }
    sidebarOverlay?.addEventListener('click', closeSidebar);

    // Mobile: any table that isn't converted to a card layout by CSS gets a
    // horizontal-scroll wrapper so it can't push the page wider than the phone.
    const CARD_TABLE_CLASSES = ['transaction-table', 'budget-table', 'account-table',
        'asset-table', 'amortization-table', 'outlook-table', 'sim-table', 'data-table'];
    document.querySelectorAll('.container table').forEach(table => {
        if (CARD_TABLE_CLASSES.some(c => table.classList.contains(c))) return;
        if (table.closest('.table-scroll') || table.closest('table')) return;
        const wrap = document.createElement('div');
        wrap.className = 'table-scroll';
        table.parentNode.insertBefore(wrap, table);
        wrap.appendChild(table);
    });

    // Transfer section toggle
    const transferToggle = document.getElementById('transfer-toggle');
    const transferBody = document.getElementById('transfer-body');
    if (transferToggle && transferBody) {
        transferToggle.addEventListener('click', () => {
            const isOpen = transferBody.style.display !== 'none';
            transferBody.style.display = isOpen ? 'none' : 'block';
            transferToggle.classList.toggle('open', !isOpen);
        });
    }

    // Filter section toggle
    const filterToggle = document.getElementById('filter-toggle');
    const filterBody = document.getElementById('filter-body');
    if (filterToggle && filterBody) {
        filterToggle.addEventListener('click', () => {
            const isOpen = filterBody.style.display !== 'none';
            filterBody.style.display = isOpen ? 'none' : 'block';
            filterToggle.classList.toggle('open', !isOpen);
        });
    }

    // Get CSRF token from meta tag
    const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');

    // Helper function to get headers with CSRF token
    function getHeaders(contentType = 'application/json') {
        const headers = {
            'Content-Type': contentType,
            'Accept': 'application/json'
        };
        if (csrfToken) {
            headers['X-CSRFToken'] = csrfToken;
        }
        return headers;
    }

    // Inline holding-edit forms (portfolio: quantity / cost basis)
    document.querySelectorAll('.holding-update-form').forEach(form => {
        const input = form.querySelector('.inline-edit-input');
        const original = input.value;
        input.addEventListener('input', () => {
            if (input.value !== original) {
                form.classList.add('dirty');
                form.classList.remove('saved');
            } else {
                form.classList.remove('dirty');
            }
        });
        form.addEventListener('submit', async (e) => {
            e.preventDefault();
            if (!form.classList.contains('dirty')) return;
            const holdingId = form.dataset.holdingId;
            const field = form.dataset.field;
            const value = input.value;
            form.classList.remove('dirty');
            form.classList.add('saving');
            try {
                const body = new URLSearchParams();
                body.set(field, value);
                const response = await fetch(`portfolio/update/${holdingId}`, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/x-www-form-urlencoded',
                        'Accept': 'application/json',
                        ...(csrfToken ? {'X-CSRFToken': csrfToken} : {})
                    },
                    body: body.toString()
                });
                form.classList.remove('saving');
                if (response.ok) {
                    form.classList.add('saved');
                    setTimeout(() => {
                        form.classList.remove('saved');
                        location.reload();
                    }, 600);
                } else {
                    form.classList.add('dirty');
                    console.error('Failed to update holding');
                }
            } catch (err) {
                form.classList.remove('saving');
                form.classList.add('dirty');
                console.error('Error:', err);
            }
        });
    });

    // Floating add-transaction widget: default date to today, wire collapse toggle
    const floatingAdd = document.getElementById('floatingAddTransaction');
    if (floatingAdd) {
        const dateInput = floatingAdd.querySelector('input[name="date"]');
        if (dateInput && !dateInput.value) {
            const now = new Date();
            const localISO = new Date(now.getTime() - now.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
            dateInput.value = localISO;
        }
        const toggle = document.getElementById('floatingAddToggle');
        toggle?.addEventListener('click', () => {
            floatingAdd.classList.toggle('collapsed');
        });
    }

    const transactionForms = document.querySelectorAll('.transaction-form');
    const monthRows = document.querySelectorAll('.month-row');
    const transactionTables = document.querySelectorAll('.transaction-table');

    monthRows.forEach(row => {
        row.addEventListener('click', () => {
            const monthId = row.dataset.monthId;
            const transactionRow = document.getElementById(`transactions-${monthId}`);
            if (transactionRow) {
                if (transactionRow.style.display === 'none') {
                    transactionRow.style.display = 'table-row';
                } else {
                    transactionRow.style.display = 'none';
                }
            }
        });
    });

    // Auto-expand months with negative min_balance
    monthRows.forEach(row => {
        const minBalance = parseFloat(row.dataset.minBalance);
        if (!isNaN(minBalance) && minBalance < 0) {
            const monthId = row.dataset.monthId;
            const transactionRow = document.getElementById(`transactions-${monthId}`);
            if (transactionRow && transactionRow.style.display === 'none') {
                transactionRow.style.display = 'table-row';
            }
        }
    });

    // Forecast card click-to-scroll
    document.querySelectorAll('.forecast-card').forEach(card => {
        card.addEventListener('click', () => {
            const targetMonth = card.dataset.targetMonth;
            const monthRow = document.querySelector(`.month-row[data-month-id="${targetMonth}"]`);
            if (monthRow) {
                monthRow.scrollIntoView({ behavior: 'smooth', block: 'center' });
                // Expand the month's transactions
                const transactionRow = document.getElementById(`transactions-${targetMonth}`);
                if (transactionRow && transactionRow.style.display === 'none') {
                    transactionRow.style.display = 'table-row';
                }
            }
        });
    });

    // Cash flow warning dismiss
    const warningDismiss = document.getElementById('cash-flow-warning-dismiss');
    const warningBanner = document.getElementById('cash-flow-warning');
    if (warningDismiss && warningBanner) {
        warningDismiss.addEventListener('click', (e) => {
            e.stopPropagation();
            warningBanner.style.display = 'none';
        });
    }

    // Cash flow warning toggle body
    const warningToggle = document.getElementById('cash-flow-warning-toggle');
    const warningBody = document.getElementById('cash-flow-warning-body');
    if (warningToggle && warningBody) {
        warningToggle.addEventListener('click', () => {
            warningBody.style.display = warningBody.style.display === 'none' ? 'block' : 'none';
        });
    }

    transactionTables.forEach(table => {
        table.addEventListener('click', async (e) => {
            // Handle delete recurring template button (must run before generic delete-btn)
            if (e.target.classList.contains('delete-recurring-btn')) {
                const btn = e.target;
                const recurringId = btn.dataset.recurringId;
                const isTransfer = btn.dataset.isTransfer === '1';
                const description = btn.dataset.description || 'this recurring template';
                if (!recurringId) return;
                if (!confirm(`Delete recurring template "${description}"?\n\nThis removes the template and all future projections. Past actuals are unaffected.`)) {
                    return;
                }
                const url = isTransfer
                    ? `transfers/recurring/delete/${recurringId}`
                    : `budget/delete/${recurringId}`;
                const originalText = btn.textContent;
                btn.textContent = '...';
                btn.disabled = true;
                try {
                    const response = await fetch(url, {
                        method: 'POST',
                        headers: getHeaders()
                    });
                    if (response.ok || response.redirected) {
                        location.reload();
                    } else {
                        console.error('Failed to delete recurring template');
                        btn.textContent = originalText;
                        btn.disabled = false;
                    }
                } catch (error) {
                    console.error('Error:', error);
                    btn.textContent = originalText;
                    btn.disabled = false;
                }
                return;
            }

            // Handle delete button
            if (e.target.classList.contains('delete-btn')) {
                const transactionId = e.target.dataset.id;
                if (!transactionId) return;
                if (confirm('Are you sure you want to delete this transaction?')) {
                    const btn = e.target;
                    const originalText = btn.textContent;
                    btn.textContent = '...';
                    btn.disabled = true;

                    try {
                        const response = await fetch(`transactions/${transactionId}`, {
                            method: 'DELETE',
                            headers: getHeaders()
                        });

                        if (response.ok) {
                            const row = btn.closest('tr');
                            row.style.transition = 'opacity 0.3s, background-color 0.3s';
                            row.style.backgroundColor = '#f8d7da';
                            row.style.opacity = '0';
                            setTimeout(() => {
                                row.remove();
                                // Refresh to update running balances
                                setTimeout(() => location.reload(), 500);
                            }, 300);
                        } else {
                            console.error('Failed to delete transaction');
                            btn.textContent = originalText;
                            btn.disabled = false;
                        }
                    } catch (error) {
                        console.error('Error:', error);
                        btn.textContent = originalText;
                        btn.disabled = false;
                    }
                }
            }

            // Handle skip button
            if (e.target.classList.contains('skip-btn')) {
                const btn = e.target;
                const description = btn.dataset.description;
                const date = btn.dataset.date;

                if (confirm(`Skip "${description}" for ${date}?`)) {
                    btn.textContent = '...';
                    btn.disabled = true;

                    try {
                        const response = await fetch('transactions/skip', {
                            method: 'POST',
                            headers: getHeaders(),
                            body: JSON.stringify({ description, date })
                        });

                        if (response.ok) {
                            const row = btn.closest('tr');
                            row.style.transition = 'opacity 0.3s, background-color 0.3s';
                            row.style.backgroundColor = '#fff3cd';
                            row.style.opacity = '0';
                            setTimeout(() => {
                                row.remove();
                                setTimeout(() => location.reload(), 500);
                            }, 300);
                        } else {
                            console.error('Failed to skip transaction');
                            btn.textContent = 'Skip';
                            btn.disabled = false;
                        }
                    } catch (error) {
                        console.error('Error:', error);
                        btn.textContent = 'Skip';
                        btn.disabled = false;
                    }
                }
            }
        });
    });

    transactionForms.forEach(form => {
        form.addEventListener('submit', async (e) => {
            e.preventDefault();
            const formData = new FormData(form);
            const data = Object.fromEntries(formData.entries());
            const submitBtn = form.querySelector('button[type="submit"]');
            const originalText = submitBtn.textContent;

            submitBtn.textContent = 'Adding...';
            submitBtn.disabled = true;

            try {
                const response = await fetch('transactions', {
                    method: 'POST',
                    headers: getHeaders(),
                    body: JSON.stringify(data)
                });

                if (response.ok) {
                    // Clear form
                    form.reset();
                    submitBtn.textContent = 'Added!';

                    // Refresh after delay to get accurate running balances
                    setTimeout(() => {
                        location.reload();
                    }, 1000);
                } else {
                    console.error('Failed to add transaction');
                    submitBtn.textContent = 'Error!';
                    setTimeout(() => {
                        submitBtn.textContent = originalText;
                        submitBtn.disabled = false;
                    }, 2000);
                }
            } catch (error) {
                console.error('Error:', error);
                submitBtn.textContent = 'Error!';
                setTimeout(() => {
                    submitBtn.textContent = originalText;
                    submitBtn.disabled = false;
                }, 2000);
            }
        });
    });

    // Handle delete buttons in filtered results (asset-table used for search results)
    document.querySelectorAll('.asset-table .delete-btn').forEach(btn => {
        btn.addEventListener('click', async () => {
            const transactionId = btn.dataset.id;
            if (!transactionId) return;
            if (confirm('Are you sure you want to delete this transaction?')) {
                const originalText = btn.textContent;
                btn.textContent = '...';
                btn.disabled = true;
                try {
                    const response = await fetch(`transactions/${transactionId}`, {
                        method: 'DELETE',
                        headers: getHeaders()
                    });
                    if (response.ok) {
                        const row = btn.closest('tr');
                        row.style.transition = 'opacity 0.3s';
                        row.style.opacity = '0';
                        setTimeout(() => location.reload(), 300);
                    } else {
                        btn.textContent = originalText;
                        btn.disabled = false;
                    }
                } catch (error) {
                    console.error('Error:', error);
                    btn.textContent = originalText;
                    btn.disabled = false;
                }
            }
        });
    });

    // Handle editable projected amounts
    const editableAmounts = document.querySelectorAll('.editable-amount');
    editableAmounts.forEach(span => {
        span.addEventListener('click', (e) => {
            e.stopPropagation();
            const currentAmount = parseFloat(span.textContent.replace('$', ''));
            const baseAmount = parseFloat(span.dataset.baseAmount);
            const recurringId = span.dataset.recurringId;
            const date = span.dataset.date;

            // Create inline form
            const form = document.createElement('form');
            form.className = 'inline-amount-form';
            form.innerHTML = `
                <input type="number" step="0.01" value="${currentAmount}" class="amount-input" />
                <button type="submit" class="action-btn update-btn">Save</button>
                <button type="button" class="action-btn reset-btn" title="Reset to base: $${baseAmount.toFixed(2)}">Reset</button>
            `;

            span.style.display = 'none';
            span.parentNode.insertBefore(form, span.nextSibling);

            const input = form.querySelector('input');
            input.focus();
            input.select();

            form.addEventListener('submit', async (e) => {
                e.preventDefault();
                const newAmount = parseFloat(input.value);

                try {
                    const response = await fetch('projected/update-amount', {
                        method: 'POST',
                        headers: getHeaders(),
                        body: JSON.stringify({
                            recurring_id: recurringId,
                            date: date,
                            amount: newAmount
                        })
                    });

                    if (response.ok) {
                        location.reload();
                    } else {
                        alert('Failed to update amount');
                        form.remove();
                        span.style.display = '';
                    }
                } catch (error) {
                    console.error('Error:', error);
                    alert('Error updating amount');
                    form.remove();
                    span.style.display = '';
                }
            });

            // Reset button
            form.querySelector('.reset-btn').addEventListener('click', async () => {
                try {
                    const response = await fetch('projected/update-amount', {
                        method: 'POST',
                        headers: getHeaders(),
                        body: JSON.stringify({
                            recurring_id: recurringId,
                            date: date,
                            amount: baseAmount
                        })
                    });

                    if (response.ok) {
                        location.reload();
                    }
                } catch (error) {
                    console.error('Error:', error);
                }
            });

            // Cancel on blur (after a short delay to allow button clicks)
            input.addEventListener('blur', (e) => {
                setTimeout(() => {
                    if (form.parentNode) {
                        form.remove();
                        span.style.display = '';
                    }
                }, 200);
            });
        });
    });

});
