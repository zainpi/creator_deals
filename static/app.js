// ============= USER IDENTITY =============

function getUserId() {
    let uid = localStorage.getItem('user_id');
    if (!uid) {
        uid = (typeof crypto !== 'undefined' && crypto.randomUUID)
            ? crypto.randomUUID()
            : 'user-' + Date.now() + '-' + Math.random().toString(36).slice(2);
        localStorage.setItem('user_id', uid);
    }
    return uid;
}

// ============= GLOBAL STATE =============

let currentProducts = [];
let currentSort     = 'recent';
let currentFilter   = '';
let sessionApiCalls = 0;

// ============= RENDER =============

function renderProducts(products, containerId = 'products') {
    const container = document.getElementById(containerId);
    if (!container) return;
    if (!products || products.length === 0) {
        container.innerHTML = '<p class="loading">No products discovered yet — run a search above</p>';
        return;
    }

    const tldMap      = { DE: 'de', GB: 'co.uk', UK: 'co.uk', FR: 'fr', IT: 'it', ES: 'es' };
    const flagMap     = { DE: '🇩🇪', GB: '🇬🇧', UK: '🇬🇧', FR: '🇫🇷', IT: '🇮🇹', ES: '🇪🇸' };
    const currencyMap = { DE: '€', FR: '€', IT: '€', ES: '€', GB: '£', UK: '£' };

    let html = '';
    products.forEach(p => {
        const asin      = p.asin || 'N/A';
        const title     = (p.title || 'Unknown').substring(0, 60);
        const hasPrice  = typeof p.current_price === 'number';
        const hasAvg90  = p.keepa_avg_90 !== null && p.keepa_avg_90 !== undefined && !isNaN(p.keepa_avg_90);
        const hasDrop   = p.keepa_drop_percent !== null && p.keepa_drop_percent !== undefined && !isNaN(p.keepa_drop_percent);
        const hasSavings = p.savings_percent !== null && p.savings_percent !== undefined && !isNaN(p.savings_percent);
        const score     = typeof p.ai_score === 'number' ? `${p.ai_score.toFixed(0)}/100` : 'N/A';
        const seller    = p.seller_name || 'Unknown';
        const posted    = p.posted ? '✅' : '❌';
        const hasRating = p.seller_rating !== null && p.seller_rating !== undefined && !isNaN(p.seller_rating);
        const sellerRating = hasRating ? `${parseFloat(p.seller_rating).toFixed(0)}%` : null;
        const image     = p.image ? `<img src="${p.image}" alt="${asin}">` : '';
        const tld       = tldMap[p.marketplace]  || 'de';
        const flag      = flagMap[p.marketplace] || '🌍';
        const cur       = currencyMap[p.marketplace] || '€';
        const href      = `https://www.amazon.${tld}/dp/${asin}`;
        const pageFound = typeof p.page_found === 'number' && !isNaN(p.page_found) ? p.page_found : null;

        // Was-price: prefer API savings_percent back-calc, else keepa_avg_90
        let wasPrice    = null;
        let discountPct = null;

        if (hasPrice && hasSavings) {
            const pct = parseInt(p.savings_percent, 10);
            if (pct > 0 && pct < 100) {
                const calc = p.current_price / (1 - pct / 100);
                if (isFinite(calc)) { wasPrice = calc; discountPct = pct; }
            }
        }
        if (wasPrice === null && hasAvg90 && hasPrice) {
            wasPrice = parseFloat(p.keepa_avg_90);
        }
        // Discount %: prefer hasDrop (Keepa), else derive from was-price
        if (hasDrop) {
            discountPct = Math.round(parseFloat(p.keepa_drop_percent));
        } else if (wasPrice !== null && hasPrice && wasPrice > p.current_price) {
            discountPct = Math.round((wasPrice - p.current_price) / wasPrice * 100);
        }

        const origPriceHtml = wasPrice !== null ? `<span class="old-price">${cur}${wasPrice.toFixed(2)}</span>` : '';
        const discountBadge = discountPct !== null && discountPct > 0
            ? `<span class="badge" style="background:linear-gradient(135deg,#e53e3e,#c53030);margin-left:0;">-${discountPct}%</span>`
            : '';
        const avg90Label    = hasAvg90 ? `<span class="metric" title="Keepa 90-day average price" style="color:#9aa;font-size:0.85em;">90d avg: ${cur}${parseFloat(p.keepa_avg_90).toFixed(2)}</span>` : '';
        const price         = hasPrice ? `${cur}${p.current_price.toFixed(2)}` : 'N/A';

        html += `
            <div class="product-card">
                <div class="product-image">${image}</div>
                <div class="product-info">
                    <div class="product-asin">
                        <a href="${href}" target="_blank" rel="noopener noreferrer" title="Open on Amazon">${asin}</a>
                        <span class="market-flag" title="Marketplace">${flag}</span>
                    </div>
                    <div class="product-title" title="${(p.title || '').replace(/"/g, '&quot;')}">${title}</div>
                    <div class="product-seller" title="Seller">
                        ${seller}${sellerRating ? ` <span class="seller-rating" title="Seller positive feedback %">⭐ ${sellerRating}</span>` : ''}
                    </div>
                    <div class="product-category" title="Category">Category: ${p.category || 'Unknown'}</div>
                    <div class="product-metrics">
                        <span class="metric" title="Price">
                            ${origPriceHtml ? origPriceHtml + ' → ' : ''}${hasPrice ? price : 'N/A'}
                            ${discountBadge}
                        </span>
                        ${avg90Label}
                        <span class="metric" title="AI score${p.ai_reason ? ' — ' + String(p.ai_reason).substring(0, 80) : ''}">⭐ ${score}</span>
                        ${pageFound !== null ? `<span class="metric" title="Search page">🗂 p${pageFound}</span>` : ''}
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
        case 'savings-high': sorted.sort((a, b) => (parseFloat(b.savings_percent) || 0) - (parseFloat(a.savings_percent) || 0)); break;
        case 'savings-low':  sorted.sort((a, b) => (parseFloat(a.savings_percent) || 0) - (parseFloat(b.savings_percent) || 0)); break;
        case 'price-low':    sorted.sort((a, b) => (parseFloat(a.current_price) || Infinity) - (parseFloat(b.current_price) || Infinity)); break;
        case 'price-high':   sorted.sort((a, b) => (parseFloat(b.current_price) || 0) - (parseFloat(a.current_price) || 0)); break;
        case 'ai-score':     sorted.sort((a, b) => (parseFloat(b.ai_score) || 0) - (parseFloat(a.ai_score) || 0)); break;
        case 'keepa-drop':   sorted.sort((a, b) => (parseFloat(b.keepa_drop_percent) || 0) - (parseFloat(a.keepa_drop_percent) || 0)); break;
        default: break;
    }
    return sorted;
}

function applyFilter(products, query) {
    if (!query || !query.trim()) return products;
    const q = query.trim().toLowerCase();
    return products.filter(p =>
        (p.title        || '').toLowerCase().includes(q) ||
        (p.asin         || '').toLowerCase().includes(q) ||
        (p.category     || '').toLowerCase().includes(q) ||
        (p.seller_name  || '').toLowerCase().includes(q)
    );
}

function applyDisplayFilters(products) {
    if (!document.getElementById('use_filters')?.checked) return products;

    const minSaving   = parseFloat(document.getElementById('f_min_saving')?.value        || '0');
    const minAI       = parseFloat(document.getElementById('f_min_ai_score')?.value       || '0');
    const minSeller   = parseFloat(document.getElementById('f_min_seller_rating')?.value  || '0');
    const minPrice    = parseFloat(document.getElementById('f_min_price')?.value          || '0');
    const maxPrice    = parseFloat(document.getElementById('f_max_price')?.value          || '0');

    return products.filter(p => {
        const effectiveDiscount = p.savings_percent != null ? p.savings_percent : p.keepa_drop_percent;
        if (minSaving > 0 && (effectiveDiscount == null || effectiveDiscount < minSaving)) return false;
        if (minAI > 0 && (typeof p.ai_score !== 'number' || p.ai_score < minAI)) return false;
        if (minSeller > 0 && (p.seller_rating == null || p.seller_rating < minSeller)) return false;
        if (minPrice > 0 && (p.current_price == null || p.current_price < minPrice)) return false;
        if (maxPrice > 0 && (p.current_price == null || p.current_price > maxPrice)) return false;
        return true;
    });
}

function sortProducts() {
    const el = document.getElementById('sort-by');
    currentSort = el ? el.value : 'recent';
    updateView();
}

function filterProducts() {
    const el = document.getElementById('filter-input');
    currentFilter = el ? el.value : '';
    updateView();
}

function updateView() {
    let filtered = applyFilter(currentProducts, currentFilter);
    filtered = applyDisplayFilters(filtered);
    renderProducts(applySort(filtered, currentSort));
}

// ============= STATS =============

async function refreshStats() {
    try {
        const uid = getUserId();
        const res  = await fetch(`/api/stats?user_id=${encodeURIComponent(uid)}`);
        const data = await res.json();
        document.getElementById('total_discovered').textContent = data.total_discovered || 0;
        document.getElementById('total_posted').textContent     = data.total_posted     || 0;
        document.getElementById('api_calls').textContent        = sessionApiCalls;
    } catch (e) {
        console.error('Stats error:', e);
    }
}

// ============= PRODUCTS =============

async function refreshProducts() {
    try {
        const uid = getUserId();
        const res = await fetch(`/api/products?user_id=${encodeURIComponent(uid)}`);
        currentProducts = (await res.json()) || [];
        updateView();
    } catch (e) {
        console.error('Products error:', e);
    }
}

// ============= SEARCH =============

// ============= AUTO-SEARCH TOGGLE =============

let autoSearchTimer    = null;
let autoSearchRunning  = false;
let countdownInterval  = null;
let searchInProgress   = false;

function startCountdown(seconds) {
    if (countdownInterval) clearInterval(countdownInterval);
    const statusEl = document.getElementById('search-status');
    let remaining = seconds;
    const tick = () => {
        if (!autoSearchRunning) { clearInterval(countdownInterval); countdownInterval = null; return; }
        const m = String(Math.floor(remaining / 60)).padStart(2, '0');
        const s = String(remaining % 60).padStart(2, '0');
        statusEl.textContent = `⏳ Next search in ${m}:${s}`;
        if (--remaining < 0) { clearInterval(countdownInterval); countdownInterval = null; }
    };
    tick();
    countdownInterval = setInterval(tick, 1000);
}

async function toggleAutoSearch() {
    const btn      = document.getElementById('search-btn');
    const statusEl = document.getElementById('search-status');
    if (autoSearchRunning) {
        autoSearchRunning = false;
        clearInterval(autoSearchTimer);
        clearInterval(countdownInterval);
        autoSearchTimer   = null;
        countdownInterval = null;
        btn.textContent = '▶ Start';
        btn.classList.remove('btn-stop');
        btn.classList.add('btn-start');
        statusEl.textContent = '⏹ Auto-search stopped.';
    } else {
        autoSearchRunning = true;
        btn.textContent = '⏹ Stop';
        btn.classList.remove('btn-start');
        btn.classList.add('btn-stop');

        const doRun = async () => {
            if (!autoSearchRunning || searchInProgress) return;
            searchInProgress = true;
            await runSearch();
            searchInProgress = false;
            if (autoSearchRunning) startCountdown(300);
        };

        await doRun();
        if (autoSearchRunning) {
            autoSearchTimer = setInterval(doRun, 5 * 60 * 1000);
        }
    }
}

async function runSearch() {
    const statusEl = document.getElementById('search-status');

    const keywords   = (document.getElementById('amazon_keywords')?.value || '').trim();
    const pages      = parseInt(document.getElementById('pages_to_search')?.value || '1');
    const checked    = [...document.querySelectorAll('input[name="marketplace"]:checked')];
    const markets    = checked.map(el => el.value);
    const useFilters = !!document.getElementById('use_filters')?.checked;

    if (markets.length === 0) {
        statusEl.textContent = '⚠️ Select at least one marketplace.';
        return;
    }

    // Persist current settings on every search so they stick for next time.
    await savePreferences(true);

    statusEl.textContent = `🔍 Searching ${markets.join(', ')} — ${pages} page(s)…`;

    try {
        const res  = await fetch('/api/search', {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify({
                user_id:      getUserId(),
                keywords,
                marketplaces: markets,
                pages,
                sort_by:      document.getElementById('sort_by')?.value || 'Featured',
                use_filters:  useFilters,
                use_keepa:    !!document.getElementById('use_keepa')?.checked,
                use_ai:       !!document.getElementById('use_ai')?.checked,
                f_min_saving:        parseFloat(document.getElementById('f_min_saving')?.value       || '0'),
                f_min_ai_score:      parseFloat(document.getElementById('f_min_ai_score')?.value      || '0'),
                f_min_seller_rating: parseFloat(document.getElementById('f_min_seller_rating')?.value || '0'),
                f_min_price:         parseFloat(document.getElementById('f_min_price')?.value         || '0'),
                f_max_price:         parseFloat(document.getElementById('f_max_price')?.value         || '0'),
            }),
        });
        // Read defensively: a timeout/crash returns an HTML error page, not JSON.
        const raw = await res.text();
        let data;
        try {
            data = JSON.parse(raw);
        } catch {
            statusEl.textContent = (res.status === 0 || res.status >= 500 || !res.ok)
                ? `❌ Server error (${res.status || 'timeout'}). The search likely timed out — try fewer pages, or turn off Keepa/AI for broad keywords.`
                : `❌ Unexpected non-JSON response (${res.status}).`;
            return;
        }

        if (!data.success) {
            statusEl.textContent = `❌ Search failed: ${data.error || 'Unknown error'}`;
            return;
        }
        // Search now runs in the background; poll for results as they're saved.
        await pollSearch(statusEl);
    } catch (e) {
        console.error('Search error:', e);
        statusEl.textContent = `❌ Error: ${e.message}`;
    }
}

// Poll the background search: refresh the grid each tick so new deals show up
// as they're processed, and stop when the job reports done/error.
async function pollSearch(statusEl) {
    const uid = getUserId();
    const startCount = currentProducts.length;
    const deadline = Date.now() + 10 * 60 * 1000;  // safety cap: 10 min

    while (Date.now() < deadline) {
        await new Promise(r => setTimeout(r, 2500));
        await refreshProducts();
        await refreshStats();

        let job = {};
        try {
            const res = await fetch(`/api/search_status?user_id=${encodeURIComponent(uid)}`);
            job = await res.json();
        } catch { /* transient — keep polling */ }

        if (job.status === 'done') {
            sessionApiCalls += job.api_calls || 0;
            await refreshStats();
            statusEl.textContent = `✅ Done — ${job.found ?? 0} new deal(s) found.`;
            return;
        }
        if (job.status === 'error') {
            statusEl.textContent = `❌ Search failed: ${job.error || 'Unknown error'}`;
            return;
        }
        const newCount = Math.max(0, currentProducts.length - startCount);
        statusEl.textContent = `⏳ Searching — ${newCount} new deal(s) so far…`;
    }
    statusEl.textContent = '⏱️ Still running in the background — results will keep appearing below.';
}

// ============= CLEAR =============

async function clearProducts() {
    if (!confirm('Clear all your discoveries?')) return;
    try {
        const res  = await fetch('/api/clear_products', {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify({ user_id: getUserId() }),
        });
        const data = await res.json();
        if (data.success) {
            currentProducts = [];
            updateView();
            await refreshStats();
        } else {
            alert('Failed to clear: ' + (data.error || 'Unknown error'));
        }
    } catch (e) {
        alert('Clear error: ' + e.message);
    }
}

// ============= PREFERENCES =============

async function savePreferences(silent = false) {
    const checked = [...document.querySelectorAll('input[name="marketplace"]:checked')];
    const markets = checked.map(el => el.value).join(',');

    const prefs = {
        user_id:      getUserId(),
        keywords:     document.getElementById('amazon_keywords')?.value || '',
        marketplaces: markets,
        pages:        parseInt(document.getElementById('pages_to_search')?.value || '1'),
        sort_by:      document.getElementById('sort_by')?.value || 'Featured',
        use_filters:  !!document.getElementById('use_filters')?.checked,
        use_keepa:    !!document.getElementById('use_keepa')?.checked,
        use_ai:       !!document.getElementById('use_ai')?.checked,
        f_min_saving:        parseFloat(document.getElementById('f_min_saving')?.value        || '0'),
        f_min_ai_score:      parseFloat(document.getElementById('f_min_ai_score')?.value       || '0'),
        f_min_seller_rating: parseFloat(document.getElementById('f_min_seller_rating')?.value  || '0'),
        f_min_price:         parseFloat(document.getElementById('f_min_price')?.value          || '0'),
        f_max_price:         parseFloat(document.getElementById('f_max_price')?.value          || '0'),
    };

    try {
        const res  = await fetch('/api/preferences', {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify(prefs),
        });
        const data = await res.json();
        if (!silent) {
            const status = document.getElementById('search-status');
            status.textContent = data.success ? '💾 Preferences saved.' : '❌ Save failed.';
            setTimeout(() => { if (status.textContent.startsWith('💾')) status.textContent = ''; }, 2000);
        }
    } catch (e) {
        console.error('Save prefs error:', e);
    }
}

async function loadPreferences() {
    try {
        const uid = getUserId();
        const res = await fetch(`/api/preferences?user_id=${encodeURIComponent(uid)}`);
        if (!res.ok) return;
        const p = await res.json();
        if (!p || !Object.keys(p).length) return;

        if (p.keywords !== undefined && document.getElementById('amazon_keywords'))
            document.getElementById('amazon_keywords').value = p.keywords;
        if (p.pages !== undefined && document.getElementById('pages_to_search'))
            document.getElementById('pages_to_search').value = p.pages;
        if (p.sort_by && document.getElementById('sort_by'))
            document.getElementById('sort_by').value = p.sort_by;
        if (p.use_filters !== undefined && document.getElementById('use_filters'))
            document.getElementById('use_filters').checked = !!p.use_filters;
        if (p.use_keepa !== undefined && document.getElementById('use_keepa'))
            document.getElementById('use_keepa').checked = !!p.use_keepa;
        if (p.use_ai !== undefined && document.getElementById('use_ai'))
            document.getElementById('use_ai').checked = !!p.use_ai;
        if (p.f_min_saving        != null && document.getElementById('f_min_saving'))
            document.getElementById('f_min_saving').value = p.f_min_saving;
        if (p.f_min_ai_score      != null && document.getElementById('f_min_ai_score'))
            document.getElementById('f_min_ai_score').value = p.f_min_ai_score;
        if (p.f_min_seller_rating != null && document.getElementById('f_min_seller_rating'))
            document.getElementById('f_min_seller_rating').value = p.f_min_seller_rating;
        if (p.f_min_price         != null && document.getElementById('f_min_price'))
            document.getElementById('f_min_price').value = p.f_min_price;
        if (p.f_max_price         != null && document.getElementById('f_max_price'))
            document.getElementById('f_max_price').value = p.f_max_price;

        if (p.marketplaces) {
            const saved = p.marketplaces.split(',').map(m => m.trim().toUpperCase());
            document.querySelectorAll('input[name="marketplace"]').forEach(el => {
                el.checked = saved.includes(el.value.toUpperCase());
            });
        }
    } catch (e) {
        console.error('Load prefs error:', e);
    }
}

// ============= TEST SCAN =============

async function runTest() {
    const btn = event.target;
    btn.disabled    = true;
    btn.textContent = '⏳ Running…';

    try {
        const pageStart = parseInt(document.getElementById('test_page_start').value);
        const pageEnd   = parseInt(document.getElementById('test_page_end').value);

        if (pageStart > pageEnd) {
            alert('Start page must be ≤ End page');
            return;
        }

        const payload = {
            marketplace:  document.getElementById('test_marketplace').value,
            page_start:   pageStart,
            page_end:     pageEnd,
            min_saving:   parseInt(document.getElementById('test_min_saving').value),
            max_price:    parseInt(document.getElementById('test_max_price').value),
            min_drop:     parseInt(document.getElementById('test_min_drop').value),
            min_rating:   parseFloat(document.getElementById('test_min_rating').value),
            min_reviews:  parseInt(document.getElementById('test_min_reviews').value),
            use_ai:       document.getElementById('test_ai_enabled').checked,
            min_ai_score: parseFloat(document.getElementById('test_min_ai_score').value),
            use_keepa:    document.getElementById('test_use_keepa').checked,
            keywords:     document.getElementById('test_keywords')?.value || '',
        };

        const res  = await fetch('/api/test', {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify(payload),
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
                                <span class="metric">⭐ ${(item.ai_score || 50.0).toFixed(0)}/100</span>
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

// ============= INIT =============

// ============= LIVE FEED TAB =============

let feedProducts   = [];
let feedFilter     = '';
let feedPollTimer  = null;
let scannerEnabled = true;

function switchTab() {} // no-op; tabs removed

async function refreshFeed() {
    try {
        const res = await fetch('/api/feed');
        feedProducts = (await res.json()) || [];
        renderFeed();
    } catch (e) {
        console.error('Feed error:', e);
    }
}

function renderFeed() {
    let list = feedProducts;
    if (feedFilter) {
        const q = feedFilter.toLowerCase();
        list = list.filter(p =>
            (p.title    || '').toLowerCase().includes(q) ||
            (p.asin     || '').toLowerCase().includes(q) ||
            (p.category || '').toLowerCase().includes(q));
    }
    renderProducts(list, 'feed-products');
}

function filterFeed() {
    feedFilter = document.getElementById('feed-filter-input')?.value || '';
    renderFeed();
}

function _ago(iso) {
    if (!iso) return 'never';
    const secs = Math.max(0, (Date.now() - new Date(iso + 'Z').getTime()) / 1000);
    if (secs < 60)   return `${Math.round(secs)}s ago`;
    if (secs < 3600) return `${Math.round(secs / 60)}m ago`;
    return `${Math.round(secs / 3600)}h ago`;
}

async function refreshScannerStatus() {
    try {
        const res = await fetch('/api/scanner_status');
        const s = await res.json();
        scannerEnabled = !!(s.enabled ?? 1);

        document.getElementById('feed_total').textContent   = s.feed_total   ?? 0;
        document.getElementById('feed_scanned').textContent = s.scanned_count ?? 0;
        document.getElementById('feed_kept').textContent    = s.kept_count    ?? 0;
        document.getElementById('feed_posted').textContent  = s.feed_posted   ?? 0;

        const toggle = document.getElementById('scanner-toggle');
        if (toggle) toggle.textContent = scannerEnabled ? '⏸ Pause' : '▶ Resume';

        const heartbeatAge = _ago(s.last_heartbeat);
        const live = s.last_heartbeat &&
            (Date.now() - new Date(s.last_heartbeat + 'Z').getTime()) < 90000;
        let txt;
        if (!scannerEnabled) {
            txt = '⏸ Scanner paused.';
        } else if (live) {
            txt = `🟢 Scanning — ${s.current_target || '…'} · last tick ${heartbeatAge}`;
        } else {
            txt = `⚪ Idle / worker not running (last heartbeat ${heartbeatAge}).`;
        }
        if (s.last_error) txt += ` · ⚠️ ${s.last_error}`;
        document.getElementById('scanner-status').textContent = txt;
    } catch (e) {
        console.error('Scanner status error:', e);
    }
}

async function toggleScanner() {
    const action = scannerEnabled ? 'pause' : 'resume';
    try {
        await fetch('/api/scanner_control', {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify({ action }),
        });
        await refreshScannerStatus();
    } catch (e) {
        console.error('Scanner control error:', e);
    }
}

// ============= CATEGORY ID TEST (RAW JSON) =============

let CATEGORY_TABLE = [];

function _esc(s) {
    return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function _money(v) {
    if (v == null || v === '') return 'N/A';
    const n = parseFloat(v);
    return isNaN(n) ? 'N/A' : n.toFixed(2);
}

function toggleCatPanel() {
    const p = document.getElementById('cat-controls');
    const i = document.getElementById('cat-toggle-indicator');
    const hidden = p.style.display === 'none' || p.style.display === '';
    p.style.display = hidden ? 'block' : 'none';
    i.textContent = hidden ? '▴' : '▾';
}

function toggleBatchPanel() {
    const p = document.getElementById('batch-controls');
    const i = document.getElementById('batch-toggle-indicator');
    const hidden = p.style.display === 'none' || p.style.display === '';
    p.style.display = hidden ? 'block' : 'none';
    i.textContent = hidden ? '▴' : '▾';
}

async function loadCategoryTable() {
    try {
        const res = await fetch('/api/categories');
        CATEGORY_TABLE = (await res.json()) || [];
        const sel = document.getElementById('cat_preset');
        if (sel) {
            CATEGORY_TABLE.forEach(c => {
                const o = document.createElement('option');
                o.value = c.id;
                o.textContent = `#${c.id} · ${c.searchIndex} · ${c.keywords} · node ${c.browseNodeId} (${c.priceNote || '—'})`;
                sel.appendChild(o);
            });
        }
    } catch (e) {
        console.error('loadCategoryTable failed:', e);
    }
    await loadTopCategoryTable();
}

// ============= PICK A CATEGORY (Method 1 / Method 2 quick-fill) =============

let TOP_CATEGORY_TABLE = [];

async function loadTopCategoryTable() {
    try {
        const res = await fetch('/api/topcategories');
        TOP_CATEGORY_TABLE = (await res.json()) || [];
        const sel = document.getElementById('cat_top_category');
        if (!sel) return;
        TOP_CATEGORY_TABLE.forEach(t => {
            const o = document.createElement('option');
            o.value = t.searchIndex;
            o.textContent = `${t.displayName} (${t.searchIndex})${t.parentBrowseNodeId ? '' : ' — no parent node yet'}`;
            sel.appendChild(o);
        });
    } catch (e) {
        console.error('loadTopCategoryTable failed:', e);
    }
}

function applyTopCategory() {
    const idx = document.getElementById('cat_top_category').value;
    const btn = document.getElementById('cat-method2-btn');
    const t = TOP_CATEGORY_TABLE.find(x => x.searchIndex === idx);
    if (!idx || !t) {
        if (btn) btn.disabled = true;
        return;
    }
    document.getElementById('cat_search_index').value = t.searchIndex;
    if (btn) btn.disabled = !t.parentBrowseNodeId;
}

function applyMethod2() {
    const idx = document.getElementById('cat_top_category').value;
    const t = TOP_CATEGORY_TABLE.find(x => x.searchIndex === idx);
    if (!t || !t.parentBrowseNodeId) return;
    // Method 2 per the board: only the parent browse node — no Search Index,
    // no Keywords, no min saving, FBA on.
    document.getElementById('cat_search_index').value = '';
    document.getElementById('cat_keywords').value = '';
    document.getElementById('cat_browse_node').value = t.parentBrowseNodeId;
    document.getElementById('cat_min_saving').value = 0;
    const fba = document.getElementById('cat_use_fba');
    if (fba) fba.checked = true;
}

function loadCatPreset() {
    const id = parseInt(document.getElementById('cat_preset').value);
    const c = CATEGORY_TABLE.find(x => x.id === id);
    if (!c) return;
    document.getElementById('cat_search_index').value = c.searchIndex || '';
    document.getElementById('cat_keywords').value      = c.keywords || '';
    document.getElementById('cat_browse_node').value   = c.browseNodeId || '';
    document.getElementById('cat_min_saving').value    = c.minSavingPercent ?? 50;
}

async function runCatTest() {
    const btn = event.target;
    btn.disabled = true;
    const label = btn.textContent;
    btn.textContent = '⏳ Running…';
    try {
        const payload = {
            marketplace:    document.getElementById('cat_marketplace').value,
            search_index:   document.getElementById('cat_search_index').value,
            keywords:       document.getElementById('cat_keywords').value,
            browse_node_id: document.getElementById('cat_browse_node').value,
            sort_by:        document.getElementById('cat_sort_by').value,
            min_saving:     parseInt(document.getElementById('cat_min_saving').value || '0'),
            min_price:      parseFloat(document.getElementById('cat_min_price').value || '0'),
            max_price:      parseFloat(document.getElementById('cat_max_price').value || '450'),
            item_count:     parseInt(document.getElementById('cat_item_count').value || '10'),
            use_keepa:      !!document.getElementById('cat_use_keepa')?.checked,
            use_fba:        !!document.getElementById('cat_use_fba')?.checked,
        };
        const res = await fetch('/api/raw_search', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        const data = await res.json();

        document.getElementById('cat-results').style.display = 'block';
        const resp = data.response || {};
        const flt = data.filter || {};
        const modeLabel = flt.mode === 'keepa_avg90'
            ? `Keepa 90-day avg ≥ ${flt.min_saving_percent}%`
            : (flt.min_saving_percent > 0
                ? `Amazon minSavingPercent ≥ ${flt.min_saving_percent}% (server-side)`
                : `Amazon · no saving filter (all items)`);
        const fetched = resp.fetched_count ?? flt.fetched;
        document.getElementById('cat-summary').innerHTML = `
            <div class="test-stat"><label>OK</label><span>${data.ok ? '✅' : '❌'}</span></div>
            <div class="test-stat"><label>HTTP Status</label><span>${resp.status ?? '—'}</span></div>
            <div class="test-stat"><label>Items (kept)</label><span>${resp.item_count ?? (data.items || []).length}</span></div>
            <div class="test-stat"><label>Fetched → Filtered</label><span>${fetched ?? '—'} → ${resp.item_count ?? '—'}</span></div>
            <div class="test-stat"><label>Filter</label><span style="font-size:.8em;">${modeLabel}</span></div>
            <div class="test-stat"><label>Pages Scanned</label><span>${resp.pages_scanned ?? '—'}</span></div>
            <div class="test-stat"><label>Total Latency</label><span>${resp.elapsed_ms != null ? resp.elapsed_ms + 'ms' : '—'}</span></div>
            ${flt.mode === 'keepa_avg90'
                ? `<div class="test-stat"><label>Keepa</label><span style="font-size:.8em;">${flt.keepa_queried || 0} ASIN · ${flt.keepa_ean_resolved || 0} via EAN · ${flt.keepa_no_data || 0} no-data${flt.keepa_error ? ' · ⚠️' : ''}</span></div>`
                : ''}
            ${flt.mode === 'keepa_avg90' && flt.keepa_max_saving_sample != null
                ? `<div class="test-stat"><label>Best saving (sample)</label><span>${flt.keepa_max_saving_sample}%</span></div>`
                : ''}
        `;
        if (flt.keepa_error) console.warn('Keepa filter note:', flt.keepa_error);
        if (flt.keepa_debug) console.table(flt.keepa_debug);
        // When Keepa filtered everything out, hint at why using the sampled numbers.
        if (flt.mode === 'keepa_avg90' && (resp.item_count === 0) && flt.keepa_max_saving_sample != null) {
            const el = document.getElementById('cat-items');
            if (el) el.innerHTML =
                `<p class="loading">No item reached ${flt.min_saving_percent}% below its Keepa 90-day average. ` +
                `Best in this batch was <b>${flt.keepa_max_saving_sample}%</b>. ` +
                `Lower Min Saving % or check the console table for per-item avg90 vs price.</p>`;
        }
        document.getElementById('cat-request-json').textContent =
            JSON.stringify(data.request || {}, null, 2);
        document.getElementById('cat-response-json').textContent =
            JSON.stringify(data.response || { error: data.error }, null, 2);

        const items = data.items || [];
        lastCatItems = items;
        const dlBtn = document.getElementById('cat-download-csv');
        if (dlBtn) dlBtn.disabled = items.length === 0;
        document.getElementById('cat-items').innerHTML = items.length === 0
            ? `<p class="loading">${_esc(data.error || 'No items returned.')}</p>`
            : items.map(renderRawCard).join('');
    } catch (e) {
        console.error('runCatTest error:', e);
        alert('Raw search error: ' + e.message);
    } finally {
        btn.disabled = false;
        btn.textContent = label;
    }
}

function renderRawCard(it) {
    const listing = ((it.OffersV2 || {}).Listings || [{}])[0] || {};
    const price = (listing.Price || {}).Amount;
    const was   = (listing.Price || {}).SavingBasisAmount;   // crossed-out "was" price
    const sav   = listing.SavingBasis;                       // computed savings %
    const img   = ((((it.Images || {}).Primary || {}).Medium || {}).URL) || '';
    const title = ((it.ItemInfo || {}).Title || {}).DisplayValue || '';
    const url   = it.DetailPageURL || '';

    // Keepa mode annotates items with KeepaStatus / KeepaAvg90 / KeepaSaving.
    const keepaOn = it.KeepaStatus !== undefined;
    const keepaNoData = it.KeepaStatus === 'no_data' || it.KeepaStatus === 'unavailable' || it.KeepaStatus === 'error';

    // Discount badge: green when there's a real % off, muted "No deal" otherwise.
    const hasDeal = sav != null && !isNaN(sav) && Number(sav) > 0;
    const wasLabel = keepaOn ? '90-day avg' : 'was';
    const savTitle = keepaOn ? `Saving vs Keepa 90-day average` : 'Discount off the was-price';
    let savBadge;
    if (keepaOn && keepaNoData) {
        savBadge = `<span class="metric" title="Keepa has no 90-day data for this ASIN — kept, not validated">📊 No Keepa data</span>`;
    } else if (hasDeal) {
        savBadge = `<span class="metric" style="background:linear-gradient(135deg,#e53e3e,#c53030);color:#fff;" title="${savTitle}">${keepaOn ? '📊' : '🏷️'} -${sav}%</span>`;
    } else {
        savBadge = `<span class="metric" title="No ${keepaOn ? 'Keepa saving' : 'savingBasis / percentage'} for this listing">🏷️ No deal</span>`;
    }

    // Price with strikethrough "was"/avg price when available.
    const priceHtml = (was != null && !isNaN(was))
        ? `<span style="text-decoration:line-through;opacity:.6;" title="${wasLabel}">${_money(was)}</span> → ${_money(price)}`
        : _money(price);

    const asinHtml = url
        ? `<a href="${_esc(url)}" target="_blank" rel="noopener noreferrer sponsored">${_esc(it.ASIN || '')} ↗</a>`
        : _esc(it.ASIN || '');

    // Explicit percentage-discount line, always shown.
    const pctText = (hasDeal)
        ? `${sav}% off${keepaOn ? ' vs 90-day avg' : ''}`
        : (keepaOn && keepaNoData ? 'No Keepa data' : 'No discount');
    const pctColor = hasDeal ? '#e53e3e' : '#888';
    const discountLine = `
        <div class="raw-discount" style="font-weight:700;color:${pctColor};margin-top:2px;" title="${savTitle}">
            ${hasDeal ? '🔻 ' : ''}${_esc(pctText)}
        </div>`;

    // Forwarding (affiliate) link button — DetailPageURL already carries the tag.
    const fwdBtn = url
        ? `<a class="raw-fwd-link" href="${_esc(url)}" target="_blank" rel="noopener noreferrer sponsored"
              style="display:block;margin-top:8px;text-align:center;padding:6px 8px;border-radius:6px;
                     background:linear-gradient(135deg,#ff9900,#e88a00);color:#111;font-weight:600;
                     text-decoration:none;font-size:.85em;">🔗 View on Amazon ↗</a>`
        : '';

    return `
        <div class="product-card">
            ${img ? (url
                ? `<a href="${_esc(url)}" target="_blank" rel="noopener noreferrer sponsored"><img src="${_esc(img)}" alt="" style="width:100%;height:120px;object-fit:contain;background:#fff;border-radius:6px;"></a>`
                : `<img src="${_esc(img)}" alt="" style="width:100%;height:120px;object-fit:contain;background:#fff;border-radius:6px;">`) : ''}
            <div class="product-info">
                <div class="product-asin">${asinHtml}</div>
                <div class="product-title">${_esc(title.slice(0, 90))}</div>
                ${discountLine}
                <div class="product-metrics">
                    <span class="metric">💶 ${priceHtml}</span>
                    ${savBadge}
                    <span class="metric" title="Category">${_esc(it.Category || '—')}</span>
                </div>
                ${fwdBtn}
            </div>
        </div>`;
}

// Holds the items from the most recent raw/category search, for CSV export.
let lastCatItems = [];

function downloadCatCsv() {
    if (!lastCatItems || lastCatItems.length === 0) {
        alert('No results to export — run a search first.');
        return;
    }
    const cols = ['ASIN', 'Title', 'Price', 'WasPrice', 'DiscountPercent', 'Category', 'Link'];
    const esc = (v) => {
        const s = (v == null ? '' : String(v));
        return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
    };
    const rows = lastCatItems.map((it) => {
        const listing = ((it.OffersV2 || {}).Listings || [{}])[0] || {};
        const price = (listing.Price || {}).Amount;
        const was   = (listing.Price || {}).SavingBasisAmount;
        const sav   = listing.SavingBasis;
        const title = ((it.ItemInfo || {}).Title || {}).DisplayValue || '';
        return [
            it.ASIN || '',
            title,
            price != null ? price : '',
            was != null ? was : '',
            (sav != null && !isNaN(sav)) ? sav : '',
            it.Category || '',
            it.DetailPageURL || '',
        ].map(esc).join(',');
    });
    const csv = [cols.join(','), ...rows].join('\n');
    const blob = new Blob(['﻿' + csv], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
    a.href = url;
    a.download = `creators_deals_${stamp}.csv`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
}

// ============= BATCH: ALL CATEGORIES =============

async function runBatchTest() {
    const btn = event.target;
    btn.disabled = true;
    const label = btn.textContent;
    btn.textContent = '⏳ Running…';
    const prog = document.getElementById('batch-progress');
    prog.textContent = 'Contacting Amazon Creators API… (this can take a minute)';

    try {
        const subsetOnly = document.getElementById('batch_only_computers').checked;
        const ids = subsetOnly
            ? CATEGORY_TABLE.slice(0, 10).map(c => c.id)
            : [];

        const overrideRaw = document.getElementById('batch_min_saving').value;
        const payload = {
            marketplace: document.getElementById('batch_marketplace').value,
            sort_by:     document.getElementById('batch_sort_by').value,
            min_price:   parseFloat(document.getElementById('batch_min_price').value || '0'),
            max_price:   parseFloat(document.getElementById('batch_max_price').value || '450'),
            item_count:  parseInt(document.getElementById('batch_item_count').value || '10'),
            category_ids: ids,
            use_category_saving: overrideRaw === '',
        };
        if (overrideRaw !== '') payload.min_saving = parseInt(overrideRaw);

        // Kick off the batch as a background job — it returns immediately.
        const res = await fetch('/api/batch_test', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        const start = await res.json();
        if (start.started === false) {
            prog.textContent = start.message || 'A batch is already running — waiting for it…';
        }

        // Poll for progress + final result.
        const data = await pollBatchStatus(prog);
        renderBatchResults(data);
        prog.textContent = `Done — ${data.total_found} items across ${data.count} categories.`;
    } catch (e) {
        console.error('runBatchTest error:', e);
        prog.textContent = '';
        alert('Batch error: ' + e.message);
    } finally {
        btn.disabled = false;
        btn.textContent = label;
    }
}

// Poll /api/batch_status until the job finishes; resolves with the result payload.
function pollBatchStatus(prog) {
    return new Promise((resolve, reject) => {
        const tick = async () => {
            try {
                const res = await fetch('/api/batch_status');
                const job = await res.json();
                if (job.status === 'running') {
                    const total = job.total || 0;
                    const done = job.completed || 0;
                    prog.textContent = total
                        ? `Scanning categories… ${done}/${total}`
                        : 'Contacting Amazon Creators API…';
                    setTimeout(tick, 1500);
                } else if (job.status === 'done' && job.result) {
                    resolve(job.result);
                } else if (job.status === 'error') {
                    reject(new Error(job.error || 'batch failed'));
                } else {
                    // No job yet / unexpected state — keep waiting briefly.
                    setTimeout(tick, 1500);
                }
            } catch (e) {
                reject(e);
            }
        };
        tick();
    });
}

function renderBatchResults(data) {
    document.getElementById('batch-results').style.display = 'block';
    document.getElementById('batch-summary').innerHTML = `
        <div class="test-stat"><label>Categories</label><span>${data.count}</span></div>
        <div class="test-stat"><label>Total Items</label><span>${data.total_found}</span></div>
        <div class="test-stat"><label>API Calls</label><span>${data.api_calls}</span></div>
        <div class="test-stat"><label>Time</label><span>${data.elapsed_s}s</span></div>
    `;

    const tbody = document.getElementById('batch-tbody');
    tbody.innerHTML = (data.rows || []).map(r => {
        const cls = r.error ? 'err-row' : (r.found > 0 ? 'ok-row' : 'zero-row');
        const detailJson = JSON.stringify({
            request_payload: r.request_payload,
            items: r.items,
            error: r.error,
        }, null, 2);
        return `
            <tr class="${cls}">
                <td>${r.id}</td>
                <td>${_esc(r.searchIndex)}</td>
                <td>${_esc(r.keywords)}</td>
                <td>${_esc(r.browseNodeId)}</td>
                <td>${r.minSavingUsed}</td>
                <td>${r.found}</td>
                <td>${r.status ?? (r.error ? 'ERR' : '—')}</td>
                <td>${r.elapsed_ms ?? '—'}</td>
                <td><span class="row-json" onclick="toggleBatchJson(${r.id})">view</span></td>
            </tr>
            <tr class="batch-json-detail" id="batch-json-${r.id}" style="display:none;">
                <td colspan="9"><pre class="json-box">${_esc(detailJson)}</pre></td>
            </tr>`;
    }).join('');
}

function toggleBatchJson(id) {
    const row = document.getElementById('batch-json-' + id);
    if (row) row.style.display = row.style.display === 'none' ? 'table-row' : 'none';
}

// ============= METHOD 1 vs METHOD 2 (LIVE A/B ENGINE) =============

let methodEngineEnabled = false;

function toggleMethodPanel() {
    const p = document.getElementById('method-controls');
    const i = document.getElementById('method-toggle-indicator');
    const hidden = p.style.display === 'none' || p.style.display === '';
    p.style.display = hidden ? 'block' : 'none';
    i.textContent = hidden ? '▴' : '▾';
    if (hidden) {
        loadMethodCategories();
        refreshMethodStatus();
    }
}

async function loadMethodCategories() {
    const tbody = document.getElementById('method-categories-tbody');
    try {
        const res = await fetch('/api/method_test/categories');
        const cats = (await res.json()) || [];
        if (!cats.length) {
            tbody.innerHTML = '<tr><td colspan="3">No categories seeded yet — start the worker process once to seed them.</td></tr>';
            return;
        }
        tbody.innerHTML = cats.map(c => {
            const m1 = c.method1, m2 = c.method2;
            const cell = (m, method) => m.available
                ? `<label style="display:flex;align-items:center;gap:6px;cursor:pointer;">
                       <input type="checkbox" ${m.enabled ? 'checked' : ''}
                              onchange="toggleMethodCategory('${_esc(c.searchIndex)}', ${method}, this.checked)">
                       ${m.nodeCount} node${m.nodeCount === 1 ? '' : 's'}
                   </label>`
                : `<span style="color:#666;">no node data yet</span>`;
            return `
                <tr>
                    <td>${_esc(c.displayName)} <span style="color:#9aa;">(${_esc(c.searchIndex)})</span></td>
                    <td>${cell(m1, 1)}</td>
                    <td>${cell(m2, 2)}</td>
                </tr>`;
        }).join('');
    } catch (e) {
        console.error('loadMethodCategories failed:', e);
        tbody.innerHTML = '<tr><td colspan="3">Failed to load categories.</td></tr>';
    }
}

async function toggleMethodCategory(searchIndex, method, enabled) {
    try {
        await fetch('/api/method_test/toggle', {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify({ category: searchIndex, method, enabled }),
        });
        await refreshMethodStatus();
    } catch (e) {
        console.error('toggleMethodCategory failed:', e);
    }
}

async function toggleMethodEngine() {
    const action = methodEngineEnabled ? 'pause' : 'start';
    try {
        await fetch('/api/method_test/control', {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify({ action }),
        });
        await refreshMethodStatus();
    } catch (e) {
        console.error('toggleMethodEngine failed:', e);
    }
}

async function refreshMethodStatus() {
    try {
        const res = await fetch('/api/method_test/status');
        const s = await res.json();
        const engine = s.engine || {};
        methodEngineEnabled = !!engine.enabled;

        const toggle = document.getElementById('method-engine-toggle');
        if (toggle) toggle.textContent = methodEngineEnabled ? '⏸ Pause' : '▶ Start';

        const heartbeatAge = _ago(engine.last_heartbeat);
        const live = engine.last_heartbeat &&
            (Date.now() - new Date(engine.last_heartbeat + 'Z').getTime()) < 90000;
        let txt;
        if (!methodEngineEnabled) {
            txt = '⏸ Engine paused.';
        } else if (live) {
            txt = `🟢 Running — ${engine.current_target || '…'} · last tick ${heartbeatAge}`;
        } else {
            txt = `⚪ Idle / worker not running (last heartbeat ${heartbeatAge}).`;
        }
        if (engine.last_error) txt += ` · ⚠️ ${engine.last_error}`;
        const statusEl = document.getElementById('method-engine-status');
        if (statusEl) statusEl.textContent = txt;

        const b = s.budget || {};
        document.getElementById('method_calls_today').textContent  = b.calls_today ?? 0;
        document.getElementById('method_daily_budget').textContent = b.daily_budget_requests ?? 0;
        document.getElementById('method_pct_used').textContent     = `${b.pct_used ?? 0}%`;
        document.getElementById('method_theoretical').textContent  = b.theoretical_share_per_active_target ?? 0;

        renderMethodTargets(s.targets || []);
    } catch (e) {
        console.error('refreshMethodStatus failed:', e);
    }
}

function renderMethodTargets(targets) {
    const tbody = document.getElementById('method-targets-tbody');
    if (!targets.length) {
        tbody.innerHTML = '<tr><td colspan="12">No data yet — enable a category above and start the engine.</td></tr>';
        return;
    }
    tbody.innerHTML = targets.map(t => `
        <tr class="${t.enabled ? 'ok-row' : ''}">
            <td>${_esc(t.category)}</td>
            <td>Method ${t.method}</td>
            <td>${t.enabled_node_count}/${t.node_count}</td>
            <td>€${t.avg_price_floor}</td>
            <td>${t.creators_api_calls}</td>
            <td>${t.keepa_calls}</td>
            <td>${t.asins_scanned}</td>
            <td>${t.cache_skipped}</td>
            <td>${t.keepa_rejected}</td>
            <td>${t.ai_rejected}</td>
            <td>${t.posted}</td>
            <td>${t.success_rate}%</td>
        </tr>`).join('');
}

// ============= INIT =============

(async function initDashboard() {
    try {
        await loadPreferences();
    } catch (e) {
        console.error('Init loadPreferences failed:', e);
    }
    await refreshProducts();
    await refreshStats();
    loadCategoryTable();   // populate the category-ID test presets
    // Live feed disabled
    // refreshFeed();
    // refreshScannerStatus();
    // feedPollTimer = setInterval(() => { refreshFeed(); refreshScannerStatus(); }, 5000);
    // Refresh products every 30s in case another tab ran a search
    setInterval(refreshProducts, 30000);
    setInterval(refreshStats,    15000);
    // Method A/B panel polls only while its own section is expanded
    setInterval(() => {
        const p = document.getElementById('method-controls');
        if (p && p.style.display === 'block') refreshMethodStatus();
    }, 10000);
})();

// ============= EXPORTS =============

window.toggleAutoSearch = toggleAutoSearch;
window.runSearch       = runSearch;
window.savePreferences = savePreferences;
window.clearProducts   = clearProducts;
window.sortProducts    = sortProducts;
window.filterProducts  = filterProducts;
window.runTest         = runTest;
window.toggleTestPanel = toggleTestPanel;
window.switchTab       = switchTab;
window.filterFeed      = filterFeed;
window.toggleScanner   = toggleScanner;
window.toggleCatPanel  = toggleCatPanel;
window.toggleBatchPanel = toggleBatchPanel;
window.loadCatPreset   = loadCatPreset;
window.runCatTest      = runCatTest;
window.runBatchTest    = runBatchTest;
window.toggleMethodPanel    = toggleMethodPanel;
window.toggleMethodCategory = toggleMethodCategory;
window.toggleMethodEngine   = toggleMethodEngine;
window.applyTopCategory     = applyTopCategory;
window.applyMethod2         = applyMethod2;
window.toggleBatchJson = toggleBatchJson;
