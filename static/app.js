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

function renderProducts(products) {
    const container = document.getElementById('products');
    if (!products || products.length === 0) {
        container.innerHTML = '<p class="loading">No products discovered yet — run a search above</p>';
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
        const score      = typeof p.ai_score === 'number' ? `${p.ai_score.toFixed(1)}/10` : 'N/A';
        const seller     = p.seller_name || 'Unknown';
        const posted     = p.posted ? '✅' : '❌';
        const hasRating  = p.seller_rating !== null && p.seller_rating !== undefined && !isNaN(p.seller_rating);
        const sellerRating = hasRating ? `${parseFloat(p.seller_rating).toFixed(0)}%` : null;
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
                    origPriceHtml = `<span class="old-price" title="Estimated original price">€${orig.toFixed(2)}</span>`;
                }
            }
        }

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
                        <span class="metric" title="Current price">💶 ${origPriceHtml ? origPriceHtml + ' → ' : ''}${hasPrice ? price : 'N/A'}</span>
                        ${hasAvg90 ? `<span class="metric metric-avg" title="Keepa 90-day average price">📈 ${avg90} avg</span>` : ''}
                        ${hasSavings ? `<span class="metric" title="Savings">📊 ${savings} off</span>` : ''}
                        ${hasDrop ? `<span class="metric" title="Drop from 90d avg">📉 ${drop} drop</span>` : ''}
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
        if (minSaving > 0 && (p.savings_percent == null || p.savings_percent < minSaving)) return false;
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

async function runSearch() {
    const btn = document.getElementById('search-btn');
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

    btn.disabled    = true;
    btn.textContent = '⏳ Searching…';
    statusEl.textContent = `Searching ${markets.join(', ')} — ${pages} page(s)…`;

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
                f_min_saving:        parseFloat(document.getElementById('f_min_saving')?.value       || '0'),
                f_min_ai_score:      parseFloat(document.getElementById('f_min_ai_score')?.value      || '0'),
                f_min_seller_rating: parseFloat(document.getElementById('f_min_seller_rating')?.value || '0'),
                f_min_price:         parseFloat(document.getElementById('f_min_price')?.value         || '0'),
                f_max_price:         parseFloat(document.getElementById('f_max_price')?.value         || '0'),
            }),
        });
        const data = await res.json();

        if (data.success) {
            sessionApiCalls += data.api_calls || 0;
            statusEl.textContent = `✅ Done — ${data.found} new deal(s) found.`;
            await refreshProducts();
            await refreshStats();
        } else {
            statusEl.textContent = `❌ Search failed: ${data.error || 'Unknown error'}`;
        }
    } catch (e) {
        console.error('Search error:', e);
        statusEl.textContent = `❌ Error: ${e.message}`;
    } finally {
        btn.disabled    = false;
        btn.textContent = '🔍 Search';
    }
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

async function savePreferences() {
    const checked = [...document.querySelectorAll('input[name="marketplace"]:checked')];
    const markets = checked.map(el => el.value).join(',');

    const prefs = {
        user_id:      getUserId(),
        keywords:     document.getElementById('amazon_keywords')?.value || '',
        marketplaces: markets,
        pages:        parseInt(document.getElementById('pages_to_search')?.value || '1'),
        sort_by:      document.getElementById('sort_by')?.value || 'Featured',
        use_filters:  !!document.getElementById('use_filters')?.checked,
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
        const status = document.getElementById('search-status');
        status.textContent = data.success ? '💾 Preferences saved.' : '❌ Save failed.';
        setTimeout(() => { if (status.textContent.startsWith('💾')) status.textContent = ''; }, 2000);
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

// ============= INIT =============

(async function initDashboard() {
    try {
        await loadPreferences();
    } catch (e) {
        console.error('Init loadPreferences failed:', e);
    }
    await refreshProducts();
    await refreshStats();
    // Refresh products every 30s in case another tab ran a search
    setInterval(refreshProducts, 30000);
    setInterval(refreshStats,    15000);
})();

// ============= EXPORTS =============

window.runSearch       = runSearch;
window.savePreferences = savePreferences;
window.clearProducts   = clearProducts;
window.sortProducts    = sortProducts;
window.filterProducts  = filterProducts;
window.runTest         = runTest;
window.toggleTestPanel = toggleTestPanel;
