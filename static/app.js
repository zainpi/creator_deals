// ============= GLOBAL STATE =============
let currentProducts = [];
let currentSort = 'recent';
let currentFilter = '';

// ============= RENDER =============

function renderProducts(products) {
    const container = document.getElementById('products');
    if (!products || products.length === 0) {
        container.innerHTML = '<p class="loading">No products discovered yet</p>';
        return;
    }

    const tldMap  = { DE: 'de', GB: 'co.uk', UK: 'co.uk', FR: 'fr', IT: 'it', ES: 'es' };
    const flagMap = { DE: '🇩🇪', GB: '🇬🇧', UK: '🇬🇧', FR: '🇫🇷', IT: '🇮🇹', ES: '🇪🇸' };

    let html = '';
    products.forEach(p => {
        const asin      = p.asin || 'N/A';
        const title     = (p.title || 'Unknown').substring(0, 60);
        const hasPrice  = typeof p.current_price === 'number';
        const price     = hasPrice ? `€${p.current_price.toFixed(2)}` : 'N/A';
        const hasSavings = p.savings_percent !== null && p.savings_percent !== undefined && !isNaN(p.savings_percent);
        const savings   = hasSavings ? `${parseInt(p.savings_percent, 10)}%` : 'N/A';
        const hasDrop   = p.keepa_drop_percent !== null && p.keepa_drop_percent !== undefined && !isNaN(p.keepa_drop_percent);
        const drop      = hasDrop ? `${parseFloat(p.keepa_drop_percent).toFixed(0)}%` : 'N/A';
        const hasAvg90  = p.keepa_avg_90 !== null && p.keepa_avg_90 !== undefined && !isNaN(p.keepa_avg_90);
        const avg90     = hasAvg90 ? `€${parseFloat(p.keepa_avg_90).toFixed(2)}` : null;
        const score     = typeof p.ai_score === 'number' ? `${p.ai_score.toFixed(1)}/10` : 'N/A';
        const seller    = p.seller_name || 'Unknown';
        const posted    = p.posted ? '✅' : '❌';
        const image     = p.image ? `<img src="${p.image}" alt="${asin}">` : '';
        const tld       = tldMap[p.marketplace]  || 'de';
        const flag      = flagMap[p.marketplace] || '🌍';
        const href      = `https://www.amazon.${tld}/dp/${asin}`;
        const pageFound = typeof p.page_found === 'number' && !isNaN(p.page_found) ? p.page_found : null;

        let origPriceHtml = '';
        if (hasPrice && hasSavings) {
            const pct = parseInt(p.savings_percent, 10);
            if (pct > 0 && pct < 100) {
                const orig = p.current_price / (1 - pct / 100);
                if (isFinite(orig)) {
                    origPriceHtml = `<span class="old-price" title="Estimated original price before discount">€${orig.toFixed(2)}</span>`;
                }
            }
        }

        html += `
            <div class="product-card">
                <div class="product-image">${image}</div>
                <div class="product-info">
                    <div class="product-asin">
                        <a href="${href}" target="_blank" rel="noopener noreferrer" title="Open product on Amazon">${asin}</a>
                        <span class="market-flag" title="Marketplace">${flag}</span>
                    </div>
                    <div class="product-title" title="Product title">${title}</div>
                    <div class="product-seller" title="Seller offering this listing">Seller: ${seller}</div>
                    <div class="product-category" title="Product category">Category: ${p.category || 'Unknown'}</div>
                    <div class="product-metrics">
                        <span class="metric" title="Current offer price">💶 ${origPriceHtml ? origPriceHtml + ' → ' : ''}${hasPrice ? price : 'N/A'}</span>
                        ${hasSavings ? `<span class="metric" title="Percent off the list/saving basis">📊 ${savings} off</span>` : ''}
                        ${hasAvg90 ? `<span class="metric" title="Keepa 90-day average price">📈 ~${avg90} 90d avg</span>` : ''}
                        <span class="metric" title="How far below 90-day average the current price is">📉 ${drop} drop</span>
                        ${pageFound !== null ? `<span class="metric" title="Search page where this was found">🗂 Page ${pageFound}</span>` : ''}
                        <span class="metric" title="AI relevance score${p.ai_reason ? ` — ${String(p.ai_reason).substring(0, 80)}` : ''}">⭐ ${score}</span>
                        <span class="metric posted" title="Posted to Discord">${posted}</span>
                    </div>
                </div>
            </div>
        `;
    });

    container.innerHTML = html;
}

// ============= SORT + FILTER =============

function applySort(products, sortBy) {
    const sorted = [...products];
    switch (sortBy) {
        case 'savings-high':
            sorted.sort((a, b) => (parseFloat(b.savings_percent) || 0) - (parseFloat(a.savings_percent) || 0));
            break;
        case 'savings-low':
            sorted.sort((a, b) => (parseFloat(a.savings_percent) || 0) - (parseFloat(b.savings_percent) || 0));
            break;
        case 'price-low':
            sorted.sort((a, b) => (parseFloat(a.current_price) || Infinity) - (parseFloat(b.current_price) || Infinity));
            break;
        case 'price-high':
            sorted.sort((a, b) => (parseFloat(b.current_price) || 0) - (parseFloat(a.current_price) || 0));
            break;
        case 'score-high':
            sorted.sort((a, b) => (parseFloat(b.ai_score) || 0) - (parseFloat(a.ai_score) || 0));
            break;
        case 'drop-high':
            sorted.sort((a, b) => (parseFloat(b.keepa_drop_percent) || 0) - (parseFloat(a.keepa_drop_percent) || 0));
            break;
        case 'recent':
        default:
            // Keep original API order
            break;
    }
    return sorted;
}

function applyFilter(products, query) {
    if (!query || !query.trim()) return products;
    const q = query.trim().toLowerCase();
    return products.filter(p =>
        (p.title  || '').toLowerCase().includes(q) ||
        (p.asin   || '').toLowerCase().includes(q) ||
        (p.category || '').toLowerCase().includes(q) ||
        (p.seller_name || '').toLowerCase().includes(q)
    );
}

function sortProducts() {
    const sortSelect = document.getElementById('sort-by');
    currentSort = sortSelect ? sortSelect.value : 'recent';
    updateView();
}

function filterProducts() {
    const filterInput = document.getElementById('filter-input');
    currentFilter = filterInput ? filterInput.value : '';
    updateView();
}

function updateView() {
    const showAll = !!(document.getElementById('show_all_products') && document.getElementById('show_all_products').checked);

    // Apply free-text filter first
    let filtered = applyFilter(currentProducts, currentFilter);

    if (!showAll) {
        // Apply settings-based filters to the dataset when toggle is OFF
        const minSaving   = parseFloat(document.getElementById('min_saving')?.value || '0');
        const maxPrice    = parseFloat(document.getElementById('max_price')?.value || '0');
        const minAIScore  = parseFloat(document.getElementById('min_ai_score')?.value || '0');
        const minKeepa    = parseFloat(document.getElementById('min_keepa_drop')?.value || '0');
        const minRating   = parseFloat(document.getElementById('min_rating')?.value || '0');
        const minReviews  = parseFloat(document.getElementById('min_reviews')?.value || '0');
        const minFeedback = parseFloat(document.getElementById('min_feedback')?.value || '0');

        filtered = filtered.filter(p => {
            // Savings present and above threshold
            if (p.savings_percent === null || p.savings_percent === undefined || isNaN(p.savings_percent)) return false;
            if (Number(p.savings_percent) < minSaving) return false;

            // Price under max when defined (> 0)
            if (maxPrice > 0 && (typeof p.current_price !== 'number' || p.current_price > maxPrice)) return false;

            // AI score threshold
            if (typeof p.ai_score === 'number' && p.ai_score < minAIScore) return false;

            // Keepa drop when available
            if (!isNaN(minKeepa) && minKeepa > 0) {
                const kd = Number(p.keepa_drop_percent);
                if (isNaN(kd) || kd < minKeepa) return false;
            }

            // Rating / review count not persisted; skip until available
            // Seller feedback not persisted; skip until available
            return true;
        });
    }

    const sorted = applySort(filtered, currentSort);
    renderProducts(sorted);
}

// ============= STATS =============

async function refreshStats() {
    try {
        const res  = await fetch('/api/stats');
        const data = await res.json();

        document.getElementById('total_found').textContent    = data.total_found    || 0;
        document.getElementById('keepa_passed').textContent   = data.keepa_passed   || 0;
        document.getElementById('ai_passed').textContent      = data.ai_passed      || 0;
        document.getElementById('discord_posted').textContent = data.discord_posted || 0;
        document.getElementById('scan_count').textContent     = data.scan_count     || 0;
        document.getElementById('api_calls').textContent      = data.api_calls      || 0;

        if (data.last_scan_time) {
            document.getElementById('last_scan_time').textContent =
                new Date(data.last_scan_time).toLocaleTimeString();
        }
    } catch (e) {
        console.error('Stats error:', e);
    }
}

// ============= PRODUCTS =============

async function refreshProducts() {
    try {
        const res      = await fetch('/api/products');
        currentProducts = (await res.json()) || [];
        updateView();
    } catch (e) {
        console.error('Products error:', e);
    }
}

// ============= SCANNER CONTROLS =============

async function startScanner() {
    try {
        const res  = await fetch('/api/start', { method: 'POST' });
        const data = await res.json();
        if (data.success) {
            document.getElementById('status').textContent  = '●';
            document.getElementById('status').className   = 'status-badge running';
            document.getElementById('status-text').textContent = 'Running';
        }
    } catch (e) {
        console.error('Start error:', e);
    }
}

async function stopScanner() {
    try {
        const res  = await fetch('/api/stop', { method: 'POST' });
        const data = await res.json();
        if (data.success) {
            document.getElementById('status').textContent  = '●';
            document.getElementById('status').className   = 'status-badge';
            document.getElementById('status-text').textContent = 'Stopped';
        }
    } catch (e) {
        console.error('Stop error:', e);
    }
}

async function resetStats() {
    if (!confirm('Reset all statistics?')) return;
    try {
        await fetch('/api/reset', { method: 'POST' });
        await refreshStats();
        await refreshProducts();
    } catch (e) {
        console.error('Reset error:', e);
    }
}

async function clearProducts() {
    if (!confirm('Clear all recent discoveries?')) return;
    try {
        const res  = await fetch('/api/clear_products', { method: 'POST' });
        const data = await res.json();
        if (data.success) {
            await refreshProducts();
        } else {
            alert('Failed to clear: ' + (data.error || 'Unknown error'));
        }
    } catch (e) {
        console.error('Clear products error:', e);
        alert('Clear error: ' + e.message);
    }
}

// ============= CONFIG =============

async function saveConfig() {
    try {
        const payload = {
            scanner: {
                pages_to_search: parseInt(document.getElementById('pages_to_search').value) || 1
            },
            amazon: {
                min_saving_percent: parseInt(document.getElementById('min_saving').value) || 0,
                max_price:          parseFloat(document.getElementById('max_price').value) || 0,
                sort_by:            (document.getElementById('sort_by')?.value) || 'Featured',
                keywords:           (document.getElementById('amazon_keywords') && document.getElementById('amazon_keywords').value) || ''
            },
            ai: {
                minimum_score: parseFloat(document.getElementById('min_ai_score').value) || 0
            },
            filters: {
                min_keepa_drop_percent: parseInt(document.getElementById('min_keepa_drop').value) || 0,
                min_rating:             parseFloat(document.getElementById('min_rating').value) || 0,
                min_review_count:       parseInt(document.getElementById('min_reviews').value) || 0,
                apply_to_new:           !!document.getElementById('apply_filters_server')?.checked
            },
            seller: {
                min_feedback_percent: parseInt(document.getElementById('min_feedback').value) || 0,
                allow_amazon_only:    !!document.getElementById('amazon_only').checked
            },
            keepa: {
                enabled: !!document.getElementById('use_keepa').checked
            }
        };

        const res  = await fetch('/api/config', {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify(payload)
        });
        const data = await res.json();
        if (data.success) {
            alert('✅ Config saved successfully');
            // Reload effective config into the UI
            await loadConfig();
        } else alert('❌ Failed to save config: ' + (data.error || 'Unknown error'));
    } catch (e) {
        console.error('Config save error:', e);
        alert('❌ Error saving config: ' + e.message);
    }
}

// Load current config from server and populate the settings form
async function loadConfig() {
    try {
        const res = await fetch('/api/config');
        if (!res.ok) return;
        const cfg = await res.json();

        // scanner
        if (cfg.scanner && cfg.scanner.pages_to_search !== undefined) {
            const el = document.getElementById('pages_to_search');
            if (el) el.value = cfg.scanner.pages_to_search;
        }

        // amazon
        if (cfg.amazon) {
            if (cfg.amazon.min_saving_percent !== undefined && document.getElementById('min_saving'))
                document.getElementById('min_saving').value = cfg.amazon.min_saving_percent;
            if (cfg.amazon.max_price !== undefined && document.getElementById('max_price'))
                document.getElementById('max_price').value = cfg.amazon.max_price;
            if (cfg.amazon.sort_by && document.getElementById('sort_by'))
                document.getElementById('sort_by').value = cfg.amazon.sort_by;
            if (cfg.amazon.keywords !== undefined && document.getElementById('amazon_keywords'))
                document.getElementById('amazon_keywords').value = cfg.amazon.keywords;
        }

        // ai
        if (cfg.ai && cfg.ai.minimum_score !== undefined && document.getElementById('min_ai_score'))
            document.getElementById('min_ai_score').value = cfg.ai.minimum_score;

        // filters
        if (cfg.filters) {
            if (cfg.filters.min_keepa_drop_percent !== undefined && document.getElementById('min_keepa_drop'))
                document.getElementById('min_keepa_drop').value = cfg.filters.min_keepa_drop_percent;
            if (cfg.filters.min_rating !== undefined && document.getElementById('min_rating'))
                document.getElementById('min_rating').value = cfg.filters.min_rating;
            if (cfg.filters.min_review_count !== undefined && document.getElementById('min_reviews'))
                document.getElementById('min_reviews').value = cfg.filters.min_review_count;
            if (cfg.filters.apply_to_new !== undefined && document.getElementById('apply_filters_server'))
                document.getElementById('apply_filters_server').checked = !!cfg.filters.apply_to_new;
        }

        // seller
        if (cfg.seller) {
            if (cfg.seller.min_feedback_percent !== undefined && document.getElementById('min_feedback'))
                document.getElementById('min_feedback').value = cfg.seller.min_feedback_percent;
            if (cfg.seller.allow_amazon_only !== undefined && document.getElementById('amazon_only'))
                document.getElementById('amazon_only').checked = !!cfg.seller.allow_amazon_only;
        }

        // keepa
        if (cfg.keepa && document.getElementById('use_keepa'))
            document.getElementById('use_keepa').checked = !!cfg.keepa.enabled;

    } catch (e) {
        console.error('Load config error:', e);
    }
}

// ============= TEST SCAN =============

async function runTest() {
    const btn = event.target;
    btn.disabled    = true;
    btn.textContent = '⏳ Running...';

    try {
        const pageStart = parseInt(document.getElementById('test_page_start').value);
        const pageEnd   = parseInt(document.getElementById('test_page_end').value);

        if (pageStart > pageEnd) {
            alert('Start page must be ≤ End page');
            return;
        }

        const payload = {
            marketplace: document.getElementById('test_marketplace').value,
            page_start:  pageStart,
            page_end:    pageEnd,
            min_saving:  parseInt(document.getElementById('test_min_saving').value),
            max_price:   parseInt(document.getElementById('test_max_price').value),
            min_drop:    parseInt(document.getElementById('test_min_drop').value),
            min_rating:  parseFloat(document.getElementById('test_min_rating').value),
            min_reviews: parseInt(document.getElementById('test_min_reviews').value),
            use_ai:      document.getElementById('test_ai_enabled').checked,
            min_ai_score: parseFloat(document.getElementById('test_min_ai_score').value),
            use_keepa:   document.getElementById('test_use_keepa').checked,
            keywords:    (document.getElementById('test_keywords') && document.getElementById('test_keywords').value) || ''
        };

        const res  = await fetch('/api/test', {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify(payload)
        });
        const data = await res.json();

        if (data.success) {
            document.getElementById('test-results').style.display = 'block';
            document.getElementById('test_found').textContent  = data.found;
            document.getElementById('test_keepa').textContent  = data.keepa_passed;
            document.getElementById('test_ai').textContent     = data.ai_passed;
            document.getElementById('test_pages').textContent  = data.pages_scanned;
            document.getElementById('test_time').textContent   = data.time + 's';

            const errBox  = document.getElementById('test-errors');
            const errList = document.getElementById('test-error-list');
            if (data.errors && data.errors.length > 0) {
                errBox.style.display = 'block';
                errList.innerHTML = '';
                data.errors.forEach(err => {
                    const li = document.createElement('li');
                    li.textContent = err;
                    errList.appendChild(li);
                });
            } else {
                errBox.style.display = 'none';
            }

            const itemsHtml = data.results.length === 0
                ? '<p class="loading">No items passed all filters</p>'
                : data.results.map(item => `
                    <div class="product-card">
                        <div class="product-info">
                            <div class="product-asin">
                                <a href="${item.url}" target="_blank" rel="noopener noreferrer">${item.asin}</a>
                                <span class="badge" style="background:#667eea;padding:3px 10px;border-radius:4px;font-size:0.75em;margin-left:8px;font-weight:600;">Page ${item.page}</span>
                            </div>
                            <div class="product-title">${item.title}</div>
                            <div class="product-metrics">
                                <span class="metric">💶 €${item.price ? item.price.toFixed(2) : 'N/A'}</span>
                                ${item.keepa_drop !== null && item.keepa_drop !== undefined
                                    ? `<span class="metric">📉 ${item.keepa_drop.toFixed(1)}%</span>`
                                    : `<span class="metric">📉 N/A</span>`}
                                <span class="metric">⭐ ${(item.ai_score || 5.0).toFixed(1)}/10</span>
                            </div>
                        </div>
                    </div>
                `).join('');

            document.getElementById('test-items').innerHTML = itemsHtml;
        } else {
            alert('Test failed: ' + data.error);
        }
    } catch (e) {
        console.error('Test error:', e);
        alert('Test error: ' + e.message);
    } finally {
        btn.disabled    = false;
        btn.textContent = '🔍 Test Scan';
    }
}

// ============= TEST PANEL TOGGLE =============

function toggleTestPanel() {
    const panel = document.getElementById('test-controls');
    const ind   = document.getElementById('test-toggle-indicator');
    if (!panel || !ind) return;
    const isHidden = panel.style.display === 'none' || panel.style.display === '';
    panel.style.display = isHidden ? 'block' : 'none';
    ind.textContent     = isHidden ? '▴' : '▾';
}

// ============= POLLING =============

// Load config first, then start polling and initial refreshes
(async function initDashboard() {
    try {
        await loadConfig();
    } catch (e) {
        console.error('Init loadConfig failed:', e);
    }

    // Start polling after config is applied to avoid flicker
    setInterval(refreshStats,    2000);
    setInterval(refreshProducts, 5000);

    refreshStats();
    refreshProducts();
})();

// ============= GLOBAL EXPORTS (for inline HTML handlers) =============

window.sortProducts    = sortProducts;
window.filterProducts  = filterProducts;
window.renderProducts  = renderProducts;
window.startScanner    = startScanner;
window.stopScanner     = stopScanner;
window.resetStats      = resetStats;
window.clearProducts   = clearProducts;
window.saveConfig      = saveConfig;
window.runTest         = runTest;
window.toggleTestPanel = toggleTestPanel;