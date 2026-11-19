// -----------------------------------------------------------------------------
// RADAR: o catálogo não contém recomendações fixas.
// Todos os cards são montados com os resultados reais recebidos das APIs via Flask.
// -----------------------------------------------------------------------------

const ALL_CATEGORIES = ['Livros', 'Músicas', 'Séries', 'Filmes', 'Jogos', 'Mangás', 'HQs'];

const grid = document.getElementById('recommendationGrid');
const searchInput = document.getElementById('searchInput');
const resultsLabel = document.getElementById('resultsLabel');
const emptyState = document.getElementById('emptyState');
const favoriteCount = document.getElementById('favoriteCount');
const showFavoritesButton = document.getElementById('showFavorites');
const dialog = document.getElementById('detailsDialog');
const dialogContent = document.getElementById('dialogContent');

let activeCategory = 'Todos';
let favoritesOnly = false;
let activeFavoriteScope = 'guest';
let favorites = loadFavorites();
let favoriteItems = loadFavoriteItems();
// Descarta IDs antigos do protótipo que não possuem mais um objeto de API associado.
favorites = new Set([...favorites].filter((id) => favoriteItems.has(id)));
let currentItems = [];
let currentReviewChart = null;
let searchSequence = 0;
let lastSearchErrors = [];
let backendProblem = '';
let categoryPages = Object.fromEntries(ALL_CATEGORIES.map((category) => [category, 1]));
let categoryHasMore = Object.fromEntries(ALL_CATEGORIES.map((category) => [category, false]));
let isLoadingMore = false;
let isRetryingInitial = false;
let failedInitialCategories = new Set();
let lastSearchTerm = '';
let paginationNotice = '';
let currentUser = null;
let authMode = 'login';
const personalizedGrid = document.getElementById('personalizedGrid');

// -----------------------------------------------------------------------------
// FAVORITOS
// -----------------------------------------------------------------------------
function favoriteStorageKey(kind) {
  const prefix = activeFavoriteScope === 'guest' ? 'radar' : `radar:user:${activeFavoriteScope}`;
  return `${prefix}-${kind}`;
}

function loadFavorites() {
  try {
    const storedIds = JSON.parse(localStorage.getItem(favoriteStorageKey('favorites')) || '[]');
    return new Set(Array.isArray(storedIds) ? storedIds.map(String) : []);
  } catch (error) {
    console.warn('Não foi possível carregar os IDs favoritos.', error);
    return new Set();
  }
}

function loadFavoriteItems() {
  try {
    const storedItems = JSON.parse(localStorage.getItem(favoriteStorageKey('favorite-items')) || '[]');
    const validItems = Array.isArray(storedItems)
      ? storedItems.filter((item) => item && typeof item.id === 'string' && item.title)
      : [];
    return new Map(validItems.map((item) => [item.id, item]));
  } catch (error) {
    console.warn('Não foi possível carregar os itens favoritos.', error);
    return new Map();
  }
}

function switchFavoriteScope(userId = null) {
  const nextScope = userId ? String(userId) : 'guest';
  if (nextScope === activeFavoriteScope) return;
  activeFavoriteScope = nextScope;
  favorites = loadFavorites();
  favoriteItems = loadFavoriteItems();
  favorites = new Set([...favorites].filter((id) => favoriteItems.has(id)));
  renderCards();
}

function saveFavorites() {
  try {
    localStorage.setItem(favoriteStorageKey('favorites'), JSON.stringify([...favorites]));
    localStorage.setItem(favoriteStorageKey('favorite-items'), JSON.stringify([...favoriteItems.values()]));
  } catch (error) {
    console.warn('O navegador não permitiu salvar os favoritos.', error);
  }
}

async function toggleFavorite(id) {
  const key = String(id);
  const selectedItem = currentItems.find((item) => String(item.id) === key);

  if (favorites.has(key)) {
    favorites.delete(key);
    favoriteItems.delete(key);
  } else if (selectedItem) {
    favorites.add(key);
    favoriteItems.set(key, selectedItem);
  }

  saveFavorites();
  if (selectedItem && currentUser) {
    await recordInteraction(selectedItem, favorites.has(key) ? 'favorite' : 'unfavorite');
  }
  renderCards();
  if (currentUser) await loadPersonalizedRecommendations();
}

// -----------------------------------------------------------------------------
// FUNÇÕES AUXILIARES
// -----------------------------------------------------------------------------
function escapeHTML(value) {
  return String(value ?? '').replace(/[&<>"']/g, (character) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#039;'
  })[character]);
}

function safeURL(value) {
  if (!value) return null;
  try {
    const url = new URL(value);
    return url.protocol === 'https:' ? url.href : null;
  } catch {
    return null;
  }
}

function itemSource(item) {
  return String(item.source || '');
}

function itemExternalId(item) {
  return String(item.externalId ?? item.id);
}

function getVisibleItems() {
  const base = favoritesOnly ? [...favoriteItems.values()] : currentItems;
  const categoryFiltered = base.filter((item) => (
    activeCategory === 'Todos' || item.category === activeCategory
  ));

  const term = searchInput.value.trim().toLocaleLowerCase('pt-BR');
  if (!term || favoritesOnly) return categoryFiltered;

  return categoryFiltered.filter((item) => {
    const searchable = `${item.title} ${item.creator} ${item.category} ${item.vibe} ${item.description}`
      .toLocaleLowerCase('pt-BR');
    return searchable.includes(term);
  });
}

// -----------------------------------------------------------------------------
// CARDS: capas/imagens vêm do campo image enviado pela API.
// Sem imagem fornecida pelo provedor, usamos apenas um fundo neutro, não uma capa falsa.
// -----------------------------------------------------------------------------
function renderCards() {
  const items = getVisibleItems();

  grid.innerHTML = items.map((item, index) => {
    const isFavorite = favorites.has(String(item.id));
    const image = safeURL(item.image);
    const fallback = `
      <div class="no-cover" aria-label="Imagem não disponível">
        <span>${escapeHTML(item.category)}</span>
        <strong>${escapeHTML(item.title.slice(0, 1).toLocaleUpperCase('pt-BR'))}</strong>
        <small>Imagem não disponível na fonte</small>
      </div>
    `;

    return `
      <article class="recommendation-card">
        <div class="card-art ${image ? 'has-cover' : 'without-cover'}">
          ${image
            ? `<img class="card-cover" src="${escapeHTML(image)}" alt="Imagem de ${escapeHTML(item.title)}" loading="lazy" referrerpolicy="no-referrer" onerror="this.hidden=true; this.nextElementSibling.hidden=false; this.parentElement.classList.remove('has-cover'); this.parentElement.classList.add('without-cover');"><div class="no-cover" hidden><span>${escapeHTML(item.category)}</span><strong>${escapeHTML(item.title.slice(0, 1).toLocaleUpperCase('pt-BR'))}</strong><small>Imagem não disponível na fonte</small></div>`
            : fallback
          }
          <div class="art-topline">
            <span>${escapeHTML(item.category)}</span>
            <span class="art-index">${String(index + 1).padStart(2, '0')}</span>
          </div>
          <button class="favorite-button ${isFavorite ? 'is-favorite' : ''}"
            type="button" data-favorite="${escapeHTML(item.id)}"
            aria-label="${isFavorite ? 'Remover dos' : 'Adicionar aos'} favoritos"
            aria-pressed="${isFavorite}">${isFavorite ? '♥' : '♡'}</button>
        </div>
        <div class="card-body">
          <div class="card-category">
            <span>${escapeHTML(item.category)}</span>
            <span class="vibe">${escapeHTML(item.vibe || '')}</span>
          </div>
          <h3 class="card-title" title="${escapeHTML(item.title)}">${escapeHTML(item.title)}</h3>
          <p class="card-creator">${escapeHTML(item.creator)}</p>
          <div class="card-bottom">
            <span class="card-year">${escapeHTML(item.year || '—')}</span>
            <button class="details-button" type="button" data-details="${escapeHTML(item.id)}">Ver detalhes ↗</button>
          </div>
        </div>
      </article>`;
  }).join('');

  const context = favoritesOnly
    ? 'na sua lista'
    : activeCategory === 'Todos'
      ? 'nas bibliotecas das APIs'
      : `em ${activeCategory.toLocaleLowerCase('pt-BR')}`;

  resultsLabel.textContent = `${items.length} ${items.length === 1 ? 'resultado' : 'resultados'} ${context}`;
  if (lastSearchErrors.length) {
    const failedNames = lastSearchErrors.map((entry) => entry.category);
    resultsLabel.textContent += ` · fontes indisponíveis: ${failedNames.join(', ')}`;
  }
  const apiStatus = document.getElementById('apiStatus');
  if (apiStatus) {
    if (backendProblem) {
      apiStatus.hidden = false;
      apiStatus.textContent = backendProblem;
    } else if (lastSearchErrors.length) {
      apiStatus.hidden = false;
      apiStatus.textContent = lastSearchErrors
        .map((entry) => `${entry.category}: ${entry.message}`)
        .join(' | ');
    } else {
      apiStatus.hidden = true;
      apiStatus.textContent = '';
    }
  }

  const loadMoreButton = document.getElementById('loadMoreButton');
  if (loadMoreButton) {
    // A disponibilidade é baseada em resultados já carregados, não somente
    // no indicador has_more do provedor (que pode vir ausente/inconsistente).
    const categoriesWithResults = activeCategory === 'Todos'
      ? ALL_CATEGORIES.filter((category) => currentItems.some((item) => item.category === category))
      : [activeCategory].filter((category) => currentItems.some((item) => item.category === category));
    const canLoadMore = !favoritesOnly && categoriesWithResults.some((category) => categoryHasMore[category]);
    loadMoreButton.hidden = !canLoadMore;
    loadMoreButton.disabled = isLoadingMore || isRetryingInitial;
    loadMoreButton.textContent = isLoadingMore ? 'Carregando...' : 'Carregar mais resultados';
  }

  const paginationStatus = document.getElementById('paginationStatus');
  if (paginationStatus) {
    paginationStatus.hidden = !paginationNotice;
    paginationStatus.textContent = paginationNotice;
  }

  const retryButton = document.getElementById('retryApisButton');
  if (retryButton) {
    retryButton.hidden = failedInitialCategories.size === 0;
    retryButton.disabled = isRetryingInitial || isLoadingMore;
    retryButton.textContent = isRetryingInitial
      ? 'Tentando novamente...'
      : `Tentar novamente (${failedInitialCategories.size} fonte(s))`;
  }

  emptyState.hidden = items.length !== 0;
  grid.hidden = items.length === 0;
  favoriteCount.textContent = favorites.size;
  showFavoritesButton.classList.toggle('active', favoritesOnly);
  showFavoritesButton.setAttribute('aria-pressed', String(favoritesOnly));

  if (items.length === 0 && backendProblem) {
    const heading = emptyState.querySelector('h3');
    const paragraph = emptyState.querySelector('p');
    heading.textContent = 'O servidor Python não foi encontrado.';
    paragraph.textContent = backendProblem;
  } else if (items.length === 0 && lastSearchErrors.length) {
    const heading = emptyState.querySelector('h3');
    const paragraph = emptyState.querySelector('p');
    heading.textContent = 'As bibliotecas não puderam ser carregadas.';
    paragraph.textContent = lastSearchErrors
      .map((entry) => `${entry.category}: ${entry.message}`)
      .join(' | ');
  } else if (items.length === 0) {
    const heading = emptyState.querySelector('h3');
    const paragraph = emptyState.querySelector('p');
    heading.textContent = favoritesOnly ? 'Sua lista ainda está vazia.' : 'Nenhum resultado encontrado.';
    paragraph.textContent = favoritesOnly
      ? 'Adicione conteúdos aos favoritos para encontrá-los aqui.'
      : 'Tente outro termo ou escolha uma categoria diferente.';
  }
}

function updateCategoryButtons(category) {
  document.querySelectorAll('.category-chip').forEach((button) => {
    const selected = button.dataset.category === category;
    button.classList.toggle('selected', selected);
    button.setAttribute('aria-pressed', String(selected));
  });
}

// -----------------------------------------------------------------------------
// CATÁLOGOS DAS APIs
// q vazio significa: carregar seleção/popularidade diretamente do catálogo externo.
// q preenchido significa: pesquisar o termo informado.
// -----------------------------------------------------------------------------
async function readJSONResponse(response) {
  const contentType = response.headers.get('content-type') || '';
  if (!contentType.toLowerCase().includes('application/json')) {
    throw new Error('O servidor não retornou JSON. É provável que você tenha aberto o site pelo Live Server. Execute py app.py e abra http://127.0.0.1:5000/.');
  }
  return response.json();
}

async function checkFlaskBackend() {
  if (window.location.protocol === 'file:') {
    throw new Error('Não abra o index.html diretamente. Inicie o Flask com py app.py e acesse http://127.0.0.1:5000/.');
  }

  let response;
  try {
    response = await fetch('/api/health', { cache: 'no-store' });
  } catch (error) {
    throw new Error('Não foi possível conectar ao Flask. Abra o terminal na pasta do projeto, execute py app.py e acesse http://127.0.0.1:5000/.');
  }

  const health = await readJSONResponse(response);
  if (!response.ok || health.status !== 'ok') {
    throw new Error('O endpoint /api/health não confirmou que o Flask está funcionando. Atualize o app.py e reinicie o servidor.');
  }
  return health;
}

async function searchOneCategory(category, term = '', page = 1) {
  const params = new URLSearchParams({ category, q: term, page: String(page) });
  let response;
  try {
    response = await fetch(`/api/search?${params.toString()}`);
  } catch (error) {
    throw new Error('Falha de conexão com o servidor local. Confira se o Flask continua ligado.');
  }

  const data = await readJSONResponse(response);
  if (!response.ok) {
    const status = data.configuration_required ? 'chave necessária' : `HTTP ${response.status}`;
    throw new Error(`${data.error || `Falha ao consultar ${category}.`} (${status})`);
  }
  return {
    results: data.results || [],
    hasMore: Boolean(data.has_more),
    page: Number(data.page || page),
  };
}

async function searchRadar(term = '') {
  const requestNumber = ++searchSequence;
  const categories = activeCategory === 'Todos' ? ALL_CATEGORIES : [activeCategory];

  favoritesOnly = false;
  lastSearchTerm = term;
  lastSearchErrors = [];
  failedInitialCategories = new Set();
  isRetryingInitial = false;
  backendProblem = '';
  isLoadingMore = false;
  categoryPages = Object.fromEntries(ALL_CATEGORIES.map((category) => [category, 1]));
  categoryHasMore = Object.fromEntries(ALL_CATEGORIES.map((category) => [category, false]));
  paginationNotice = '';
  resultsLabel.textContent = term
    ? 'Pesquisando nas bibliotecas culturais...'
    : 'Carregando catálogos das APIs...';
  grid.hidden = false;
  emptyState.hidden = true;
  grid.innerHTML = '<p class="loading-message">Consultando as bibliotecas externas e carregando capas reais...</p>';
  const loadMoreButton = document.getElementById('loadMoreButton');
  if (loadMoreButton) { loadMoreButton.hidden = true; loadMoreButton.disabled = true; }

  try {
    await checkFlaskBackend();
  } catch (error) {
    if (requestNumber !== searchSequence) return;
    backendProblem = error.message;
    currentItems = [];
    renderCards();
    return;
  }

  // A primeira página é consultada em sequência para reduzir picos de requisições
  // simultâneas, principalmente no Google Books.
  const results = [];
  for (const category of categories) {
    if (requestNumber !== searchSequence) return;
    try {
      const response = await searchOneCategory(category, term, 1);
      results.push(...response.results);
      // Se a API devolveu itens, habilitamos a tentativa da página seguinte
      // mesmo quando não informou corretamente has_more.
      categoryHasMore[category] = response.results.length > 0;
      categoryPages[category] = 1;
    } catch (error) {
      const message = error?.message || 'Erro desconhecido ao consultar a API.';
      lastSearchErrors.push({ category, message });
      failedInitialCategories.add(category);
      console.warn(`Não foi possível carregar ${category}: ${message}`);
    }
  }

  if (requestNumber !== searchSequence) return;
  currentItems = results;
  renderCards();
  if (currentUser) await loadPersonalizedRecommendations();
}

async function loadMoreResults() {
  if (isLoadingMore || isRetryingInitial || favoritesOnly) return;

  const categories = activeCategory === 'Todos'
    ? ALL_CATEGORIES.filter((category) => categoryHasMore[category])
    : (categoryHasMore[activeCategory] ? [activeCategory] : []);

  if (!categories.length) return;

  isLoadingMore = true;
  renderCards();
  const requestNumber = searchSequence;
  const added = [];

  try {
    for (const category of categories) {
      if (requestNumber !== searchSequence) return;

      const nextPage = (categoryPages[category] || 1) + 1;
      try {
        const response = await searchOneCategory(category, lastSearchTerm, nextPage);
        if (requestNumber !== searchSequence) return;

        added.push(...response.results);
        categoryPages[category] = nextPage;
        // Continua enquanto a API devolver itens. Quando uma página vier vazia,
        // a biblioteca é considerada esgotada e o botão deixa de aparecer.
        categoryHasMore[category] = response.results.length > 0;
        lastSearchErrors = lastSearchErrors.filter((entry) => entry.category !== category);
      } catch (error) {
        const message = error?.message || 'Falha ao carregar mais resultados.';
        lastSearchErrors = lastSearchErrors.filter((entry) => entry.category !== category);
        lastSearchErrors.push({ category, message });
        console.warn(`Não foi possível carregar a página ${nextPage} de ${category}:`, error);
        // Mantém a mesma página para que o botão permita tentar novamente.
      }
    }

    if (requestNumber !== searchSequence) return;

    const existingIds = new Set(currentItems.map((item) => String(item.id)));
    let newlyAdded = 0;
    for (const item of added) {
      const key = String(item.id);
      if (!existingIds.has(key)) {
        currentItems.push(item);
        existingIds.add(key);
        newlyAdded += 1;
      }
    }
    paginationNotice = newlyAdded > 0
      ? `${newlyAdded} novo(s) resultado(s) carregado(s).`
      : 'As APIs não retornaram mais conteúdos para esta biblioteca.';
  } finally {
    if (requestNumber === searchSequence) {
      isLoadingMore = false;
      renderCards();
      if (currentUser) loadPersonalizedRecommendations();
    }
  }
}

async function retryFailedInitialCategories() {
  if (isRetryingInitial || isLoadingMore || failedInitialCategories.size === 0) return;

  const requestNumber = ++searchSequence;
  const categories = [...failedInitialCategories];
  isRetryingInitial = true;
  renderCards();

  try {
    for (const category of categories) {
      if (requestNumber !== searchSequence) return;
      try {
        const response = await searchOneCategory(category, lastSearchTerm, 1);
        if (requestNumber !== searchSequence) return;

        currentItems = currentItems.filter((item) => item.category !== category);
        currentItems.push(...response.results);
        categoryPages[category] = 1;
        categoryHasMore[category] = response.results.length > 0;
        failedInitialCategories.delete(category);
        lastSearchErrors = lastSearchErrors.filter((entry) => entry.category !== category);
      } catch (error) {
        const message = error?.message || 'Erro desconhecido ao consultar a API.';
        lastSearchErrors = lastSearchErrors.filter((entry) => entry.category !== category);
        lastSearchErrors.push({ category, message });
        console.warn(`Nova tentativa falhou para ${category}:`, error);
      }
    }
  } finally {
    if (requestNumber === searchSequence) {
      isRetryingInitial = false;
      renderCards();
    }
  }
}


async function setCategory(category) {
  activeCategory = category;
  favoritesOnly = false;
  updateCategoryButtons(category);
  await searchRadar(searchInput.value.trim());
}

// -----------------------------------------------------------------------------
// CONTA, PREFERÊNCIAS E RECOMENDAÇÕES PERSONALIZADAS
// -----------------------------------------------------------------------------
function updateAccountButton() {
  const button = document.getElementById('accountButton');
  const section = document.getElementById('personalizedSection');
  if (!button || !section) return;
  button.textContent = currentUser ? `Olá, ${currentUser.username}` : 'Entrar / criar conta';
  section.hidden = !currentUser;
}

async function recordInteraction(item, interactionType) {
  if (!currentUser || !item) return;
  try {
    await fetch('/api/interactions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        source: itemSource(item),
        external_id: itemExternalId(item),
        category: item.category,
        title: item.title,
        interaction_type: interactionType,
        tags: item.vibe || ''
      })
    });
  } catch (error) {
    console.warn('Não foi possível registrar esta interação.', error);
  }
}

function renderPersonalizedCards(items) {
  if (!personalizedGrid) return;
  personalizedGrid.innerHTML = items.map((item, index) => {
    const image = safeURL(item.image);
    const isFavorite = favorites.has(String(item.id));
    const cover = image
      ? `<img class="card-cover" src="${escapeHTML(image)}" alt="Imagem de ${escapeHTML(item.title)}" loading="lazy" referrerpolicy="no-referrer">`
      : `<div class="no-cover"><span>${escapeHTML(item.category)}</span><strong>${escapeHTML(item.title.slice(0, 1))}</strong><small>Imagem não disponível</small></div>`;
    return `
      <article class="recommendation-card personalized-card">
        <div class="card-art ${image ? 'has-cover' : 'without-cover'}">
          ${cover}
          <div class="art-topline"><span>${escapeHTML(item.category)}</span><span class="art-index">${String(index + 1).padStart(2, '0')}</span></div>
          <button class="favorite-button ${isFavorite ? 'is-favorite' : ''}" type="button" data-favorite="${escapeHTML(item.id)}" aria-label="${isFavorite ? 'Remover dos' : 'Adicionar aos'} favoritos" aria-pressed="${isFavorite}">${isFavorite ? '♥' : '♡'}</button>
        </div>
        <div class="card-body">
          <div class="card-category"><span>${escapeHTML(item.category)}</span><span class="vibe">${escapeHTML(item.vibe || '')}</span></div>
          <h3 class="card-title" title="${escapeHTML(item.title)}">${escapeHTML(item.title)}</h3>
          <p class="card-creator">${escapeHTML(item.creator || '')}</p>
          <p class="personalized-reason">${escapeHTML(item.reason || 'Sugestão para explorar')}</p>
          <div class="card-bottom"><span class="card-year">Pontuação ${Math.round(Number(item.score || 0) * 100)}/100</span><button class="details-button" type="button" data-details="${escapeHTML(item.id)}">Ver detalhes ↗</button></div>
        </div>
      </article>`;
  }).join('');
}

async function loadPersonalizedRecommendations() {
  const section = document.getElementById('personalizedSection');
  const status = document.getElementById('personalizedStatus');
  const message = document.getElementById('personalizedMessage');
  if (!section || !currentUser) {
    if (section) section.hidden = true;
    return;
  }
  section.hidden = false;
  if (!currentItems.length) {
    message.textContent = 'Carregue alguns conteúdos para receber sugestões.';
    personalizedGrid.innerHTML = '';
    return;
  }
  status.textContent = 'Analisando seu perfil...';
  message.textContent = 'Comparando seus interesses com os conteúdos encontrados nas bibliotecas.';
  try {
    const response = await fetch('/api/recommendations', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        items: currentItems.slice(0, 250).map((item) => ({
          id: item.id,
          source: itemSource(item),
          externalId: itemExternalId(item),
          category: item.category,
          title: item.title,
          creator: item.creator,
          image: item.image,
          year: item.year,
          vibe: item.vibe,
          externalRating: item.externalRating
        }))
      })
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Não foi possível gerar recomendações.');
    renderPersonalizedCards(data.items || []);
    status.textContent = data.neural_ready ? 'Rede neural ativa' : 'Aprendendo seus gostos';
    message.textContent = data.message || 'Sugestões baseadas no seu perfil.';
    if (!data.neural_ready) {
      message.textContent += ` · ${data.training_interactions || 0} interações registradas; o modelo colaborativo precisa de mais histórico e de pelo menos dois usuários.`;
    }
  } catch (error) {
    status.textContent = 'Perfil de interesses';
    message.textContent = error.message;
    personalizedGrid.innerHTML = '';
  }
}

async function fetchPreferences() {
  const response = await fetch('/api/preferences');
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Não foi possível carregar preferências.');
  document.querySelectorAll('[name="preferredCategory"]').forEach((checkbox) => {
    checkbox.checked = (data.categories || []).includes(checkbox.value);
  });
  document.getElementById('preferredGenres').value = (data.genres || []).join(', ');
}

function setAuthMode(mode) {
  authMode = mode;
  document.querySelectorAll('[data-auth-mode]').forEach((button) => {
    const active = button.dataset.authMode === mode;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', String(active));
  });
  document.getElementById('authTitle').textContent = mode === 'register'
    ? 'Crie sua conta no Radar.'
    : 'Entre para personalizar seu Radar.';
  document.getElementById('authSubmitButton').textContent = mode === 'register' ? 'Criar conta' : 'Entrar';
  document.getElementById('authPassword').autocomplete = mode === 'register' ? 'new-password' : 'current-password';
  document.getElementById('authMessage').textContent = '';
}

async function refreshAuthState() {
  try {
    const response = await fetch('/api/auth/me', { cache: 'no-store' });
    const data = await response.json();
    currentUser = response.ok && data.authenticated ? data.user : null;
  } catch (error) {
    currentUser = null;
    console.warn('Não foi possível verificar a sessão.', error);
  }
  switchFavoriteScope(currentUser ? currentUser.id : null);
  updateAccountButton();
  if (currentUser) {
    try { await fetchPreferences(); } catch (error) { console.warn(error); }
    if (currentItems.length) await loadPersonalizedRecommendations();
  }
}

// -----------------------------------------------------------------------------
// AVALIAÇÕES E GRÁFICO
// -----------------------------------------------------------------------------
function reviewParams(item) {
  return new URLSearchParams({
    source: itemSource(item),
    external_id: itemExternalId(item)
  });
}

function formatReviewDate(value) {
  if (!value) return '';
  const parsed = new Date(value.replace(' ', 'T'));
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString('pt-BR');
}

function renderReviewChart(summary) {
  const canvas = document.getElementById('reviewsChart');
  if (!canvas || typeof Chart === 'undefined') return;

  if (currentReviewChart) {
    currentReviewChart.destroy();
    currentReviewChart = null;
  }

  const values = [1, 2, 3, 4, 5].map((star) => Number(summary.distribution?.[String(star)] || 0));
  currentReviewChart = new Chart(canvas, {
    type: 'bar',
    data: {
      labels: ['1 estrela', '2 estrelas', '3 estrelas', '4 estrelas', '5 estrelas'],
      datasets: [{
        label: 'Quantidade de avaliações',
        data: values,
        backgroundColor: ['#e18b78', '#dba36d', '#d6c46a', '#9eb66a', '#6d985a'],
        borderRadius: 7,
        borderSkipped: false
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: (context) => `${context.parsed.y} avaliação(ões)` } }
      },
      scales: {
        y: { beginAtZero: true, ticks: { precision: 0 }, grid: { color: '#eceee7' } },
        x: { grid: { display: false } }
      }
    }
  });
}

async function loadReviews(item) {
  const params = reviewParams(item);
  const list = document.getElementById('reviewList');
  const summaryLabel = document.getElementById('reviewSummary');
  if (!list || !summaryLabel) return;

  list.innerHTML = '<p class="review-muted">Carregando avaliações...</p>';
  try {
    const [reviewsResponse, summaryResponse] = await Promise.all([
      fetch(`/api/reviews?${params.toString()}`),
      fetch(`/api/reviews/summary?${params.toString()}`)
    ]);
    if (!reviewsResponse.ok || !summaryResponse.ok) {
      throw new Error('Não foi possível carregar as avaliações.');
    }

    const reviews = await reviewsResponse.json();
    const summary = await summaryResponse.json();
    summaryLabel.textContent = summary.total
      ? `${Number(summary.average).toFixed(1)} / 5 · ${summary.total} avaliação(ões)`
      : 'Ainda sem avaliações';

    if (!reviews.length) {
      list.innerHTML = '<p class="review-muted">Este conteúdo ainda não recebeu avaliações. Seja o primeiro!</p>';
    } else {
      list.innerHTML = reviews.map((review) => `
        <article class="review-card">
          <div class="review-card-heading">
            <strong>${escapeHTML(review.username)}</strong>
            <span>${'★'.repeat(Number(review.rating))}${'☆'.repeat(5 - Number(review.rating))}</span>
          </div>
          <p>${escapeHTML(review.comment)}</p>
          <small>${escapeHTML(formatReviewDate(review.created_at))}</small>
        </article>
      `).join('');
    }
    renderReviewChart(summary);
  } catch (error) {
    console.error(error);
    list.innerHTML = '<p class="review-muted">Não foi possível carregar as avaliações.</p>';
    summaryLabel.textContent = 'Indisponível';
  }
}

async function submitReview(event, item) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector('button[type="submit"]');
  const message = document.getElementById('reviewMessage');
  const formData = new FormData(form);

  const payload = {
    source: itemSource(item),
    external_id: itemExternalId(item),
    category: item.category,
    title: item.title,
    username: String(formData.get('username') || '').trim(),
    rating: Number(formData.get('rating')),
    comment: String(formData.get('comment') || '').trim(),
    tags: item.vibe || ''
  };

  button.disabled = true;
  message.textContent = 'Publicando avaliação...';
  try {
    const response = await fetch('/api/reviews', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Não foi possível publicar a avaliação.');

    form.reset();
    if (currentUser) document.getElementById('reviewUsername').value = currentUser.username;
    message.textContent = 'Avaliação publicada!';
    await loadReviews(item);
    if (currentUser) await loadPersonalizedRecommendations();
  } catch (error) {
    message.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

// -----------------------------------------------------------------------------
// DETALHES DO ITEM
// -----------------------------------------------------------------------------
function openDetails(id) {
  const item = currentItems.find((entry) => String(entry.id) === String(id))
    || favoriteItems.get(String(id));
  if (!item) return;

  const image = safeURL(item.image);
  const externalURL = safeURL(item.url);
  dialogContent.innerHTML = `
    <div class="dialog-art ${image ? 'has-cover' : ''}">
      ${image ? `<img class="dialog-cover" src="${escapeHTML(image)}" alt="Imagem de ${escapeHTML(item.title)}">` : ''}
      <h2 id="dialogTitle">${escapeHTML(item.title)}</h2>
    </div>
    <div class="dialog-info">
      <div class="dialog-meta">${escapeHTML(item.category)} · ${escapeHTML(item.vibe || '')} · ${escapeHTML(item.year || '—')}</div>
      <p class="dialog-creator">${escapeHTML(item.creator)}</p>
      <p class="dialog-description">${escapeHTML(item.description)}</p>
      ${item.externalRating !== null && item.externalRating !== undefined ? `<p class="external-rating">Nota exibida pela fonte: ${escapeHTML(item.externalRating)}</p>` : ''}
      ${externalURL ? `<a class="source-link" href="${escapeHTML(externalURL)}" target="_blank" rel="noopener noreferrer">Ver na fonte ↗</a>` : ''}
    </div>

    <section class="reviews-panel">
      <div class="reviews-heading">
        <div>
          <div class="eyebrow section-eyebrow">OPINIÃO DA COMUNIDADE</div>
          <h3>Avaliações dos usuários</h3>
        </div>
        <span id="reviewSummary">Carregando...</span>
      </div>
      <div class="review-chart-wrap">
        <canvas id="reviewsChart" aria-label="Distribuição das avaliações de 1 a 5 estrelas" role="img"></canvas>
      </div>
      <p class="chart-caption">Quantidade de avaliações feitas pelos usuários em cada nota.</p>
      <div id="reviewList" class="review-list"><p class="review-muted">Carregando avaliações...</p></div>

      <form id="reviewForm" class="review-form">
        <h3>Deixe sua avaliação</h3>
        <label for="reviewUsername">Seu nome</label>
        <input id="reviewUsername" name="username" type="text" minlength="2" maxlength="40" required placeholder="Como podemos te chamar?">
        <label for="reviewRating">Sua nota</label>
        <select id="reviewRating" name="rating" required>
          <option value="5">5 — Amei</option>
          <option value="4">4 — Muito bom</option>
          <option value="3">3 — Bom</option>
          <option value="2">2 — Regular</option>
          <option value="1">1 — Não gostei</option>
        </select>
        <label for="reviewComment">Seu comentário</label>
        <textarea id="reviewComment" name="comment" minlength="5" maxlength="1200" required placeholder="Conte o que achou deste conteúdo..."></textarea>
        <button type="submit">Publicar avaliação</button>
        <p id="reviewMessage" class="review-message" aria-live="polite"></p>
        <small class="review-disclaimer">O nome é informado pelo próprio usuário; ainda não há autenticação de contas.</small>
      </form>
    </section>
  `;

  dialog.showModal();
  const usernameInput = document.getElementById('reviewUsername');
  const disclaimer = document.querySelector('.review-disclaimer');
  if (currentUser) {
    usernameInput.value = currentUser.username;
    usernameInput.readOnly = true;
    if (disclaimer) disclaimer.textContent = 'Sua avaliação ficará associada à sua conta e poderá ajudar a personalizar recomendações.';
  } else if (disclaimer) {
    disclaimer.textContent = 'Entre na sua conta para que esta avaliação contribua para suas recomendações personalizadas.';
  }
  document.getElementById('reviewForm').addEventListener('submit', (event) => submitReview(event, item));
  if (currentUser) recordInteraction(item, 'view');
  loadReviews(item);
}

function surpriseMe() {
  const options = getVisibleItems();
  if (!options.length) return;
  const chosen = options[Math.floor(Math.random() * options.length)];
  openDetails(chosen.id);
}

// -----------------------------------------------------------------------------
// EVENTOS
// -----------------------------------------------------------------------------
document.querySelectorAll('.category-chip').forEach((button) => {
  button.addEventListener('click', () => setCategory(button.dataset.category));
});

grid.addEventListener('click', (event) => {
  const favoriteButton = event.target.closest('[data-favorite]');
  const detailsButton = event.target.closest('[data-details]');
  if (favoriteButton) toggleFavorite(favoriteButton.dataset.favorite);
  if (detailsButton) openDetails(detailsButton.dataset.details);
});

document.getElementById('searchForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  await searchRadar(searchInput.value.trim());
  document.getElementById('explorar').scrollIntoView({ behavior: 'smooth' });
});

showFavoritesButton.addEventListener('click', () => {
  favoritesOnly = !favoritesOnly;
  if (favoritesOnly) activeCategory = 'Todos';
  updateCategoryButtons(activeCategory);
  renderCards();
  document.getElementById('explorar').scrollIntoView({ behavior: 'smooth' });
});

document.getElementById('loadMoreButton')?.addEventListener('click', loadMoreResults);
document.getElementById('retryApisButton')?.addEventListener('click', retryFailedInitialCategories);

document.getElementById('surpriseButton').addEventListener('click', surpriseMe);
document.getElementById('bottomSurprise').addEventListener('click', surpriseMe);

document.getElementById('clearFilters').addEventListener('click', async () => {
  searchInput.value = '';
  favoritesOnly = false;
  activeCategory = 'Todos';
  updateCategoryButtons('Todos');
  await searchRadar('');
});

document.getElementById('resetSearch').addEventListener('click', async () => {
  searchInput.value = '';
  favoritesOnly = false;
  activeCategory = 'Todos';
  updateCategoryButtons('Todos');
  await searchRadar('');
});

document.getElementById('closeDialog').addEventListener('click', () => dialog.close());
dialog.addEventListener('click', (event) => {
  if (event.target === dialog) dialog.close();
});
dialog.addEventListener('close', () => {
  if (currentReviewChart) {
    currentReviewChart.destroy();
    currentReviewChart = null;
  }
});

// -----------------------------------------------------------------------------
// EVENTOS DE CONTA E PREFERÊNCIAS
// -----------------------------------------------------------------------------
const authDialog = document.getElementById('authDialog');
const authFormPanel = document.getElementById('authFormPanel');
const accountPanel = document.getElementById('accountPanel');

document.getElementById('accountButton').addEventListener('click', async () => {
  authDialog.showModal();
  authFormPanel.hidden = Boolean(currentUser);
  accountPanel.hidden = !currentUser;
  if (currentUser) {
    document.getElementById('accountWelcome').textContent = `Você está conectado como ${currentUser.username}.`;
    try { await fetchPreferences(); } catch (error) { document.getElementById('preferencesMessage').textContent = error.message; }
  } else {
    setAuthMode('login');
  }
});

document.querySelectorAll('[data-auth-mode]').forEach((button) => {
  button.addEventListener('click', () => setAuthMode(button.dataset.authMode));
});

document.getElementById('authForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = document.getElementById('authSubmitButton');
  const message = document.getElementById('authMessage');
  const formData = new FormData(form);
  const endpoint = authMode === 'register' ? '/api/auth/register' : '/api/auth/login';
  submit.disabled = true;
  message.textContent = authMode === 'register' ? 'Criando sua conta...' : 'Entrando...';
  try {
    const response = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        username: String(formData.get('username') || '').trim(),
        password: String(formData.get('password') || '')
      })
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Não foi possível autenticar.');
    currentUser = data.user;
    switchFavoriteScope(currentUser.id);
    authFormPanel.hidden = true;
    accountPanel.hidden = false;
    document.getElementById('accountWelcome').textContent = `Bem-vindo, ${currentUser.username}! Escolha suas preferências.`;
    document.getElementById('authTitle').textContent = 'Suas preferências no Radar.';
    updateAccountButton();
    await fetchPreferences();
    await loadPersonalizedRecommendations();
  } catch (error) {
    message.textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});

document.getElementById('preferencesForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const message = document.getElementById('preferencesMessage');
  const categories = [...document.querySelectorAll('[name="preferredCategory"]:checked')].map((input) => input.value);
  const genres = document.getElementById('preferredGenres').value.split(',').map((value) => value.trim()).filter(Boolean);
  try {
    const response = await fetch('/api/preferences', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ categories, genres })
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Não foi possível salvar preferências.');
    message.textContent = 'Preferências salvas! Vamos atualizar suas sugestões.';
    await loadPersonalizedRecommendations();
  } catch (error) {
    message.textContent = error.message;
  }
});

document.getElementById('logoutButton').addEventListener('click', async () => {
  try {
    await fetch('/api/auth/logout', { method: 'POST' });
  } finally {
    currentUser = null;
    switchFavoriteScope(null);
    document.getElementById('personalizedSection').hidden = true;
    document.getElementById('personalizedGrid').innerHTML = '';
    authDialog.close();
    updateAccountButton();
  }
});

document.getElementById('closeAuthDialog').addEventListener('click', () => authDialog.close());
authDialog.addEventListener('click', (event) => {
  if (event.target === authDialog) authDialog.close();
});

personalizedGrid.addEventListener('click', (event) => {
  const favoriteButton = event.target.closest('[data-favorite]');
  const detailsButton = event.target.closest('[data-details]');
  if (favoriteButton) toggleFavorite(favoriteButton.dataset.favorite);
  if (detailsButton) openDetails(detailsButton.dataset.details);
});

// Carrega o catálogo real das sete APIs assim que a página é aberta.
updateCategoryButtons('Todos');
refreshAuthState().finally(() => searchRadar(''));
