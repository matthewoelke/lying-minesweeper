(function () {
  "use strict";

  const DIFFICULTIES = {
    beginner: { cols: 9, rows: 9, mines: 10 },
    intermediate: { cols: 16, rows: 16, mines: 40 },
    expert: { cols: 30, rows: 16, mines: 99 },
  };

  const LIE_RATE_MAX_BY_DIFFICULTY = {
    beginner: 0.075,    // 1.5x — small board, want ~1-2 liars on average
    intermediate: 0.05,
    expert: 0.05,
  };
  function lieRateMaxFor(difficulty) {
    return LIE_RATE_MAX_BY_DIFFICULTY[difficulty] ?? 0.05;
  }
  const LONG_PRESS_MS = 350;
  const MOVE_TOLERANCE_PX = 8;

  // ---- Deterministic RNG (mulberry32 + FNV-1a 32-bit hash) ----
  // Both functions are 32-bit operations only and have identical implementations
  // in server/board.py so the server can reconstruct any seeded board byte-for-byte.
  function fnv1a32(str) {
    let h = 0x811c9dc5 >>> 0;
    for (let i = 0; i < str.length; i++) {
      h ^= str.charCodeAt(i);
      h = Math.imul(h, 0x01000193) >>> 0;
    }
    return h >>> 0;
  }

  function mulberry32(seed) {
    let a = seed >>> 0;
    return function () {
      a = (a + 0x6d2b79f5) >>> 0;
      let t = a;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  function rngFromSeedAndFirstClick(seed, r, c) {
    return mulberry32(fnv1a32(seed + ":" + r + ":" + c));
  }

  // ---- Seed (8-char base32, RFC 4648 alphabet) ----
  const SEED_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  const SEED_LENGTH = 8;
  const SEED_REGEX = /^[A-Z2-7]{8}$/;

  function generateRandomSeed() {
    const bytes = new Uint8Array(5); // 40 bits → 8 base32 chars
    crypto.getRandomValues(bytes);
    let bits = "";
    for (let i = 0; i < bytes.length; i++) {
      bits += bytes[i].toString(2).padStart(8, "0");
    }
    let out = "";
    for (let i = 0; i < SEED_LENGTH; i++) {
      out += SEED_ALPHABET[parseInt(bits.substr(i * 5, 5), 2)];
    }
    return out;
  }

  function isValidSeed(s) {
    return typeof s === "string" && SEED_REGEX.test(s);
  }

  function readSharedGameFromUrl() {
    const params = new URLSearchParams(window.location.search);
    const seed = params.get("seed");
    const fc = params.get("fc");
    const d = params.get("d");
    if (!seed || !fc) return null;
    if (!isValidSeed(seed)) return null;
    if (d && !DIFFICULTIES[d]) return null;
    const m = /^(\d+),(\d+)$/.exec(fc);
    if (!m) return null;
    const r = parseInt(m[1], 10);
    const c = parseInt(m[2], 10);
    const difficulty = d || "intermediate";
    const cfg = DIFFICULTIES[difficulty];
    if (r < 0 || r >= cfg.rows || c < 0 || c >= cfg.cols) return null;
    return { seed, r, c, difficulty };
  }

  function buildShareUrl(state) {
    const base =
      window.location.origin + window.location.pathname;
    const params = new URLSearchParams();
    const diffKey = Object.keys(DIFFICULTIES).find(
      (k) => DIFFICULTIES[k] === state.cfg
    );
    if (diffKey) params.set("d", diffKey);
    params.set("seed", state.seed);
    if (state.firstClick) {
      params.set("fc", state.firstClick.r + "," + state.firstClick.c);
    }
    return base + "?" + params.toString();
  }

  // ---- DOM refs ----
  const boardEl = document.getElementById("board");
  const difficultyEl = document.getElementById("difficulty");
  const minesRemainingEl = document.getElementById("mines-remaining");
  const timerEl = document.getElementById("timer");
  const newGameBtn = document.getElementById("new-game");
  const restartBtn = document.getElementById("restart-game");
  const shareBtn = document.getElementById("share-game");
  const shareStatusEl = document.getElementById("share-status");
  const modalBackdrop = document.getElementById("modal-backdrop");
  const modalTitle = document.getElementById("modal-title");
  const modalBody = document.getElementById("modal-body");
  const modalTrick = document.getElementById("modal-trick");
  const modalNewGameBtn = document.getElementById("modal-new-game");
  const modalCloseBtn = document.getElementById("modal-close");
  const modalShareBtn = document.getElementById("modal-share");
  const modalReplayBtn = document.getElementById("modal-replay");

  // ---- Game state ----
  let state = null;
  let timerInterval = null;
  let pendingSharedGame = null; // {seed, r, c, difficulty} parsed from URL on load
  let pendingPractice = false; // set when entering via Restart/Replay; clears scoring

  function createState(difficulty, seed) {
    const cfg = DIFFICULTIES[difficulty];
    const cells = [];
    for (let r = 0; r < cfg.rows; r++) {
      const row = [];
      for (let c = 0; c < cfg.cols; c++) {
        row.push({
          r,
          c,
          isMine: false,
          trueValue: 0,
          displayedValue: 0,
          isLiar: false,
          revealed: false,
          flagged: false,
        });
      }
      cells.push(row);
    }
    return {
      cfg,
      difficulty,
      cells,
      seed: seed || generateRandomSeed(),
      firstClick: null,
      rng: null,
      actionLog: [],
      generated: false,
      gameOver: false,
      won: false,
      flagsPlaced: 0,
      revealedCount: 0,
      startedAt: null,
      finishedAt: null,
      lieRate: 0,
      liarCount: 0,
      trickedByLiar: false,
      practice: false,
    };
  }

  function forEachNeighbor(r, c, fn) {
    const rows = state.cfg.rows;
    const cols = state.cfg.cols;
    for (let dr = -1; dr <= 1; dr++) {
      for (let dc = -1; dc <= 1; dc++) {
        if (dr === 0 && dc === 0) continue;
        const nr = r + dr;
        const nc = c + dc;
        if (nr >= 0 && nr < rows && nc >= 0 && nc < cols) {
          fn(state.cells[nr][nc]);
        }
      }
    }
  }

  function placeMinesAndComputeValues(firstR, firstC) {
    const { rows, cols, mines } = state.cfg;
    const banned = new Set();
    banned.add(firstR * cols + firstC);
    for (let dr = -1; dr <= 1; dr++) {
      for (let dc = -1; dc <= 1; dc++) {
        const nr = firstR + dr;
        const nc = firstC + dc;
        if (nr >= 0 && nr < rows && nc >= 0 && nc < cols) {
          banned.add(nr * cols + nc);
        }
      }
    }

    const eligible = [];
    for (let r = 0; r < rows; r++) {
      for (let c = 0; c < cols; c++) {
        if (!banned.has(r * cols + c)) eligible.push([r, c]);
      }
    }

    // Fisher-Yates partial shuffle to pick `mines` indices
    const minesToPlace = Math.min(mines, eligible.length);
    for (let i = 0; i < minesToPlace; i++) {
      const j = i + Math.floor(state.rng() * (eligible.length - i));
      const tmp = eligible[i];
      eligible[i] = eligible[j];
      eligible[j] = tmp;
      const [mr, mc] = eligible[i];
      state.cells[mr][mc].isMine = true;
    }

    // Compute trueValue and initial displayedValue for non-mine cells
    for (let r = 0; r < rows; r++) {
      for (let c = 0; c < cols; c++) {
        const cell = state.cells[r][c];
        if (cell.isMine) continue;
        let count = 0;
        forEachNeighbor(r, c, (n) => {
          if (n.isMine) count++;
        });
        cell.trueValue = count;
        cell.displayedValue = count;
      }
    }
  }

  function cascadeReveal(startR, startC) {
    const stack = [[startR, startC]];
    while (stack.length) {
      const [r, c] = stack.pop();
      const cell = state.cells[r][c];
      if (cell.revealed || cell.flagged || cell.isMine) continue;
      cell.revealed = true;
      state.revealedCount++;
      // Cascade uses trueValue (NOT displayedValue) by design.
      if (cell.trueValue === 0) {
        forEachNeighbor(r, c, (n) => {
          if (!n.revealed && !n.flagged && !n.isMine) {
            stack.push([n.r, n.c]);
          }
        });
      }
    }
  }

  function computeRevealedRegionFromFirstClick(firstR, firstC) {
    // Determine which cells WILL be revealed by the first-click cascade.
    // Returns a Set of "r*cols+c" keys.
    const cols = state.cfg.cols;
    const region = new Set();
    const stack = [[firstR, firstC]];
    while (stack.length) {
      const [r, c] = stack.pop();
      const key = r * cols + c;
      if (region.has(key)) continue;
      const cell = state.cells[r][c];
      if (cell.isMine) continue;
      region.add(key);
      if (cell.trueValue === 0) {
        forEachNeighbor(r, c, (n) => {
          if (!region.has(n.r * cols + n.c) && !n.isMine) {
            stack.push([n.r, n.c]);
          }
        });
      }
    }
    return region;
  }

  function assignLiars(firstR, firstC) {
    const { rows, cols } = state.cfg;
    state.lieRate = state.rng() * lieRateMaxFor(state.difficulty);

    const revealedRegion = computeRevealedRegionFromFirstClick(firstR, firstC);

    // Frontier neighbors of the revealed region also excluded (the player's
    // initial trustworthy foothold). Constraint #3.
    const safeZone = new Set(revealedRegion);
    revealedRegion.forEach((key) => {
      const r = Math.floor(key / cols);
      const c = key % cols;
      forEachNeighbor(r, c, (n) => {
        safeZone.add(n.r * cols + n.c);
      });
    });

    // Build initial candidate list: non-mine, trueValue 1-8, not in safe zone.
    const candidates = [];
    for (let r = 0; r < rows; r++) {
      for (let c = 0; c < cols; c++) {
        const cell = state.cells[r][c];
        if (cell.isMine) continue;
        if (cell.trueValue < 1 || cell.trueValue > 8) continue;
        if (safeZone.has(r * cols + c)) continue;
        candidates.push(cell);
      }
    }

    if (candidates.length === 0) {
      state.liarCount = 0;
      return;
    }

    // Target liar count
    const target = Math.round(state.lieRate * candidates.length);
    if (target === 0) {
      state.liarCount = 0;
      return;
    }

    // Shuffle candidates for unbiased pick order
    for (let i = candidates.length - 1; i > 0; i--) {
      const j = Math.floor(state.rng() * (i + 1));
      const tmp = candidates[i];
      candidates[i] = candidates[j];
      candidates[j] = tmp;
    }

    const chosen = [];
    const chosenKeys = new Set();

    for (const cand of candidates) {
      if (chosen.length >= target) break;

      // Constraint #1: no two liars 8-adjacent.
      let adjacentToLiar = false;
      forEachNeighbor(cand.r, cand.c, (n) => {
        if (chosenKeys.has(n.r * cols + n.c)) adjacentToLiar = true;
      });
      if (adjacentToLiar) continue;

      // Constraint #2: over-constraint guarantee.
      // Every unrevealed cell adjacent to `cand` must also be adjacent to at
      // least one OTHER numbered (non-mine, trueValue>=1) cell that is not a
      // current liar candidate-chosen and is not `cand` itself.
      let overConstrained = true;
      forEachNeighbor(cand.r, cand.c, (n) => {
        if (!overConstrained) return;
        if (n.isMine) return; // mines are not "unrevealed cells we deduce about" in the helpful sense; skip
        // We only care about cells that will be UNREVEALED at start (i.e. not in revealedRegion).
        const nKey = n.r * cols + n.c;
        if (revealedRegion.has(nKey)) return;
        // Find another truthful numbered neighbor of n that isn't cand and isn't already chosen as liar.
        let otherWitness = false;
        forEachNeighbor(n.r, n.c, (nn) => {
          if (otherWitness) return;
          if (nn === cand) return;
          if (nn.isMine) return;
          if (nn.trueValue < 1) return; // must be a numbered tile to act as constraint
          if (chosenKeys.has(nn.r * cols + nn.c)) return;
          otherWitness = true;
        });
        if (!otherWitness) overConstrained = false;
      });
      if (!overConstrained) continue;

      // Accept candidate as liar.
      cand.isLiar = true;
      const dir = state.rng() < 0.5 ? -1 : 1;
      cand.displayedValue = cand.trueValue + dir;
      chosen.push(cand);
      chosenKeys.add(cand.r * cols + cand.c);
    }

    state.liarCount = chosen.length;
  }

  // ---- Rendering ----
  function buildBoardDom() {
    boardEl.textContent = "";
    boardEl.style.gridTemplateColumns = `repeat(${state.cfg.cols}, var(--tile-size))`;
    boardEl.style.gridTemplateRows = `repeat(${state.cfg.rows}, var(--tile-size))`;

    for (let r = 0; r < state.cfg.rows; r++) {
      for (let c = 0; c < state.cfg.cols; c++) {
        const tile = document.createElement("div");
        tile.className = "tile";
        tile.setAttribute("role", "gridcell");
        tile.dataset.r = String(r);
        tile.dataset.c = String(c);
        boardEl.appendChild(tile);
      }
    }
  }

  function tileEl(r, c) {
    const idx = r * state.cfg.cols + c;
    return boardEl.children[idx];
  }

  function renderCell(r, c) {
    const cell = state.cells[r][c];
    const el = tileEl(r, c);
    el.className = "tile";
    el.textContent = "";

    if (state.gameOver) el.classList.add("disabled");

    if (cell.flagged && !cell.revealed) {
      el.classList.add("flagged");
      return;
    }

    if (!cell.revealed) {
      // Reveal liars post-game by default
      if (state.gameOver && cell.isLiar) {
        el.classList.add("liar-revealed");
      }
      return;
    }

    el.classList.add("revealed");

    if (cell.isMine) {
      el.classList.add("mine");
      return;
    }

    if (cell.displayedValue > 0) {
      el.classList.add(`num-${cell.displayedValue}`);
      el.textContent = String(cell.displayedValue);
    }

    if (state.gameOver && cell.isLiar) {
      el.classList.add("liar-revealed");
    }
  }

  function renderAll() {
    for (let r = 0; r < state.cfg.rows; r++) {
      for (let c = 0; c < state.cfg.cols; c++) {
        renderCell(r, c);
      }
    }
    minesRemainingEl.textContent = String(Math.max(0, state.cfg.mines - state.flagsPlaced));
  }

  function updateTimer() {
    if (!state.startedAt) {
      timerEl.textContent = "0";
      return;
    }
    const end = state.finishedAt || Date.now();
    const sec = Math.floor((end - state.startedAt) / 1000);
    timerEl.textContent = String(sec);
  }

  function startTimer() {
    stopTimer();
    state.startedAt = Date.now();
    timerInterval = setInterval(updateTimer, 250);
  }

  function stopTimer() {
    if (timerInterval) {
      clearInterval(timerInterval);
      timerInterval = null;
    }
    updateTimer();
  }

  // ---- Game actions ----
  function logAction(type, r, c, extra) {
    if (!state || !state.actionLog) return;
    // Use wall-clock relative to game start so the server's replay validator
    // doesn't depend on the client's high-res clock.
    const t = state.startedAt ? Date.now() - state.startedAt : 0;
    const entry = { t, type, r, c };
    if (extra) Object.assign(entry, extra);
    state.actionLog.push(entry);
  }

  function handleReveal(r, c) {
    if (state.gameOver) return;
    const cell = state.cells[r][c];
    if (cell.flagged) return;

    if (!state.generated) {
      state.firstClick = { r, c };
      state.rng = rngFromSeedAndFirstClick(state.seed, r, c);
      placeMinesAndComputeValues(r, c);
      assignLiars(r, c);
      state.generated = true;
      startTimer();
      if (shareBtn) shareBtn.disabled = false;
      if (restartBtn) restartBtn.disabled = false;
      registerGameWithServer();
    }

    if (cell.revealed) return;

    logAction("reveal", r, c);

    if (cell.isMine) {
      cell.revealed = true;
      endGame(false, r, c);
      return;
    }

    cascadeReveal(r, c);

    // Win check: all non-mine cells revealed
    const totalNonMine = state.cfg.rows * state.cfg.cols - state.cfg.mines;
    if (state.revealedCount >= totalNonMine) {
      endGame(true);
      return;
    }

    renderAll();
  }

  function handleFlag(r, c) {
    if (state.gameOver) return;
    if (!state.generated) return; // can't flag before first reveal
    const cell = state.cells[r][c];
    if (cell.revealed) return;
    cell.flagged = !cell.flagged;
    state.flagsPlaced += cell.flagged ? 1 : -1;
    logAction(cell.flagged ? "flag" : "unflag", r, c);
    renderAll();
  }

  function endGame(won, hitR, hitC) {
    state.gameOver = true;
    state.won = won;
    state.finishedAt = Date.now();
    stopTimer();

    // Detect "tricked":
    //   The player clicked a bomb that is in the 8-neighborhood of an
    //   already-revealed lying tile. Only revealed liars can have misled
    //   them — a face-down liar shows no number, so it couldn't have caused
    //   the mistake. Every liar has a bomb nearby by construction, so what
    //   matters is whether the bomb they actually clicked was one of those
    //   "next to an exposed lie" bombs.
    if (!won && hitR !== undefined) {
      state.trickedByLiar = false;
      for (let dr = -1; dr <= 1; dr++) {
        for (let dc = -1; dc <= 1; dc++) {
          if (dr === 0 && dc === 0) continue;
          const nr = hitR + dr, nc = hitC + dc;
          if (nr < 0 || nr >= state.cfg.rows || nc < 0 || nc >= state.cfg.cols) continue;
          const n = state.cells[nr][nc];
          if (n.isLiar && n.revealed) { state.trickedByLiar = true; break; }
        }
        if (state.trickedByLiar) break;
      }
    } else {
      state.trickedByLiar = false;
    }

    // Reveal all mines on loss
    if (!won) {
      for (let r = 0; r < state.cfg.rows; r++) {
        for (let c = 0; c < state.cfg.cols; c++) {
          const cell = state.cells[r][c];
          if (cell.isMine) cell.revealed = true;
        }
      }
    }

    renderAll();

    if (!won && hitR !== undefined) {
      const el = tileEl(hitR, hitC);
      if (el) el.classList.add("hit");
    }

    showModal(won);
    submitGameToServer();
  }

  function showModal(won) {
    modalTitle.textContent = won ? "You won! 🎉" : "Boom 💥";
    const seconds = Math.max(0, Math.floor(((state.finishedAt || Date.now()) - (state.startedAt || Date.now())) / 1000));
    const noun = `lying tile${state.liarCount === 1 ? "" : "s"}`;

    modalBody.textContent = "";
    modalBody.append(
      `${won ? "Cleared in" : "Lost after"} ${seconds}s · ${state.liarCount} `
    );
    const liarLabel = document.createElement("span");
    liarLabel.className = "liar-label";
    liarLabel.textContent = noun;
    modalBody.append(liarLabel);
    modalBody.append(".");

    if (modalTrick) {
      if (!won && state.trickedByLiar) {
        modalTrick.classList.remove("hidden");
      } else {
        modalTrick.classList.add("hidden");
      }
    }

    // Reset to default anchored position whenever a new endgame is shown
    modalDragged = false;
    anchorModalToDefault();
    modalBackdrop.classList.remove("hidden");
    modalBackdrop.setAttribute("aria-hidden", "false");
  }

  // Pin the modal to the top-right corner. When the leaderboard panel is open,
  // shift left so the card doesn't overlap it. Skipped if the user manually
  // dragged the modal — we don't want to clobber their position.
  let modalDragged = false;
  function anchorModalToDefault() {
    if (modalDragged) return;
    const lb = document.getElementById("leaderboard-panel");
    const lbOpen = lb && !lb.classList.contains("hidden");
    const lbW = lbOpen ? Math.round(lb.getBoundingClientRect().width) : 0;
    modalBackdrop.style.left = "";
    modalBackdrop.style.top = "";
    modalBackdrop.style.bottom = "";
    // 1rem base gap + leaderboard width when open.
    modalBackdrop.style.right = lbW > 0 ? `calc(1rem + ${lbW}px)` : "";
  }

  function hideModal() {
    modalBackdrop.classList.add("hidden");
    modalBackdrop.setAttribute("aria-hidden", "true");
  }

  function newGame() {
    hideModal();
    let seed = undefined;
    let autoFirstClick = null;
    if (pendingSharedGame) {
      // Consume the URL-shared game once; subsequent New Game clicks are fresh.
      const shared = pendingSharedGame;
      pendingSharedGame = null;
      if (difficultyEl.value !== shared.difficulty) {
        difficultyEl.value = shared.difficulty;
      }
      seed = shared.seed;
      autoFirstClick = { r: shared.r, c: shared.c };
      // Clear the URL params so a refresh doesn't replay the seed (player can re-share if they want).
      try {
        const cleanUrl = window.location.origin + window.location.pathname;
        window.history.replaceState({}, "", cleanUrl);
      } catch (_) { /* ignore */ }
    }
    state = createState(difficultyEl.value, seed);
    if (pendingPractice) {
      state.practice = true;
      pendingPractice = false;
    }
    stopTimer();
    timerEl.textContent = "0";
    if (shareBtn) shareBtn.disabled = true;
    if (restartBtn) restartBtn.disabled = true;
    if (shareStatusEl) shareStatusEl.textContent = "";
    updatePracticeBadge();
    buildBoardDom();
    renderAll();
    rescaleBoard();
    if (autoFirstClick) {
      handleReveal(autoFirstClick.r, autoFirstClick.c);
    }
  }

  function updatePracticeBadge() {
    const badge = document.getElementById("practice-badge");
    if (!badge) return;
    if (state && state.practice) badge.classList.remove("hidden");
    else badge.classList.add("hidden");
  }

  function buildShareMessages(url) {
    const plain = `Can't fool me!  Top my score at Lying Minesweeper:\n\n${url}`;
    const safeUrl = url
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
    const html =
      `<p>Can't fool me!&nbsp; Top my score at <b>Lying Minesweeper</b>:</p>` +
      `<p><a href="${safeUrl}">${safeUrl}</a></p>`;
    return { plain, html };
  }

  function onShareClick() {
    if (!state || !state.generated) return;
    const url = buildShareUrl(state);
    const { plain, html } = buildShareMessages(url);
    const fallback = () => {
      // Last-resort: select plain text via a temporary input
      try {
        const tmp = document.createElement("textarea");
        tmp.value = plain;
        document.body.appendChild(tmp);
        tmp.select();
        document.execCommand("copy");
        document.body.removeChild(tmp);
        if (shareStatusEl) shareStatusEl.textContent = "Message copied!";
      } catch (_) {
        if (shareStatusEl) shareStatusEl.textContent = "Copy failed";
      }
    };
    // Prefer rich HTML+text clipboard when available so bold renders in Outlook/Teams/Slack.
    if (
      navigator.clipboard &&
      window.ClipboardItem &&
      navigator.clipboard.write
    ) {
      try {
        const item = new ClipboardItem({
          "text/html": new Blob([html], { type: "text/html" }),
          "text/plain": new Blob([plain], { type: "text/plain" }),
        });
        navigator.clipboard.write([item]).then(
          () => {
            if (shareStatusEl) shareStatusEl.textContent = "Message copied!";
          },
          () => {
            // Fall back to plain text if rich copy is blocked.
            if (navigator.clipboard.writeText) {
              navigator.clipboard.writeText(plain).then(
                () => {
                  if (shareStatusEl) shareStatusEl.textContent = "Message copied!";
                },
                fallback
              );
            } else {
              fallback();
            }
          }
        );
      } catch (_) {
        fallback();
      }
    } else if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(plain).then(
        () => {
          if (shareStatusEl) shareStatusEl.textContent = "Message copied!";
        },
        fallback
      );
    } else {
      fallback();
    }
    if (shareStatusEl) {
      setTimeout(() => { shareStatusEl.textContent = ""; }, 2500);
    }
  }

  // ---- Username (localStorage-backed) ----
  const USERNAME_REGEX = /^[A-Za-z0-9]{1,20}$/;
  const USERNAME_KEY = "lying-minesweeper.username";
  const USERNAME_ADJ = [
    "Bold","Brave","Calm","Clever","Cosmic","Crafty","Daring","Eager","Epic","Fancy",
    "Fierce","Glad","Grand","Happy","Jolly","Keen","Lucky","Merry","Mighty","Nimble",
    "Plucky","Quick","Royal","Sharp","Silent","Sly","Snappy","Sunny","Swift","Witty",
  ];
  const USERNAME_NOUN = [
    "Fox","Hawk","Wolf","Bear","Tiger","Lion","Otter","Falcon","Lynx","Raven",
    "Panda","Shark","Eagle","Hare","Bison","Moose","Crow","Stoat","Heron","Pika",
    "Yak","Boar","Crab","Newt","Owl","Seal","Stag","Wren","Toad","Skunk",
  ];
  function randomUsername() {
    const a = USERNAME_ADJ[Math.floor(Math.random() * USERNAME_ADJ.length)];
    const n = USERNAME_NOUN[Math.floor(Math.random() * USERNAME_NOUN.length)];
    const d = Math.floor(Math.random() * 90) + 10; // 10-99
    return a + n + d;
  }
  function readUsername() {
    try {
      const v = localStorage.getItem(USERNAME_KEY);
      return v && USERNAME_REGEX.test(v) ? v : null;
    } catch (_) { return null; }
  }
  function writeUsername(v) {
    try { localStorage.setItem(USERNAME_KEY, v); } catch (_) { /* ignore */ }
  }

  function refreshUsernameDisplay() {
    const el = document.getElementById("username-display");
    if (!el) return;
    el.textContent = readUsername() || "(set name)";
  }

  function openUsernameModal(force) {
    const backdrop = document.getElementById("username-backdrop");
    const input = document.getElementById("username-input");
    const errEl = document.getElementById("username-error");
    if (!backdrop || !input) return;
    const current = readUsername();
    input.value = current || randomUsername();
    errEl.textContent = "";
    backdrop.classList.remove("hidden");
    backdrop.setAttribute("aria-hidden", "false");
    backdrop.dataset.required = force ? "1" : "0";
    setTimeout(() => { try { input.focus(); input.select(); } catch (_) {} }, 0);
  }

  function closeUsernameModal() {
    const backdrop = document.getElementById("username-backdrop");
    if (!backdrop) return;
    if (backdrop.dataset.required === "1" && !readUsername()) return; // can't dismiss
    backdrop.classList.add("hidden");
    backdrop.setAttribute("aria-hidden", "true");
  }

  function saveUsernameFromInput() {
    const input = document.getElementById("username-input");
    const errEl = document.getElementById("username-error");
    if (!input) return;
    const v = (input.value || "").trim();
    if (!USERNAME_REGEX.test(v)) {
      errEl.textContent = "Letters and digits only, max 20.";
      return;
    }
    writeUsername(v);
    refreshUsernameDisplay();
    closeUsernameModal();
  }

  function wireUsernameModal() {
    const rerollBtn = document.getElementById("username-reroll");
    const saveBtn = document.getElementById("username-save");
    const input = document.getElementById("username-input");
    const displayBtn = document.getElementById("username-display");
    if (rerollBtn) rerollBtn.addEventListener("click", () => {
      input.value = randomUsername();
      document.getElementById("username-error").textContent = "";
      try { input.focus(); input.select(); } catch (_) {}
    });
    if (saveBtn) saveBtn.addEventListener("click", saveUsernameFromInput);
    if (input) input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); saveUsernameFromInput(); }
    });
    if (displayBtn) displayBtn.addEventListener("click", () => openUsernameModal(false));
    refreshUsernameDisplay();
    if (!readUsername()) openUsernameModal(true);
  }

  // ---- Leaderboard panel ----
  let activeLbKind = "week";

  function fmtTime(ms) {
    const s = Math.floor(ms / 1000);
    const m = Math.floor(s / 60);
    const r = s % 60;
    return m > 0 ? `${m}m${r.toString().padStart(2, "0")}s` : `${s}s`;
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function renderLeaderboard(data) {
    const body = document.getElementById("leaderboard-body");
    if (!body) return;
    const entries = data && data.entries ? data.entries : [];
    if (!entries.length) {
      body.innerHTML = '<p class="lb-empty">No entries yet. Be the first!</p>';
      return;
    }
    let html = '<table class="lb-table"><thead><tr>';
    if (data.kind === "seed") {
      html += "<th>#</th><th>Player</th><th>Time</th><th>Liars</th>";
    } else if (data.kind === "week") {
      html += "<th>#</th><th>Player</th><th>Time</th><th>Board</th>";
    } else if (data.kind === "tricked") {
      html += "<th>#</th><th>Player</th><th>Tricked</th>";
    } else if (data.kind === "cheaters") {
      html += "<th>Player</th>";
    }
    html += "</tr></thead><tbody>";
    entries.forEach((e, i) => {
      html += "<tr>";
      if (data.kind === "seed") {
        const trick = e.tricked ? " ⚠️" : "";
        html += `<td>${i+1}</td><td>${escapeHtml(e.username)}${trick}</td>` +
                `<td class="num">${fmtTime(e.duration_ms)}</td><td class="num">${e.liar_count}</td>`;
      } else if (data.kind === "week") {
        html += `<td>${i+1}</td><td>${escapeHtml(e.username)}</td>` +
                `<td class="num">${fmtTime(e.duration_ms)}</td>` +
                `<td><button class="lb-link" data-seed="${escapeHtml(e.seed)}" data-diff="${escapeHtml(data.difficulty)}">${escapeHtml(e.seed)}</button></td>`;
      } else if (data.kind === "tricked") {
        html += `<td>${i+1}</td><td>${escapeHtml(e.username)}</td><td class="num">${e.trick_count}</td>`;
      } else if (data.kind === "cheaters") {
        html += `<td>${escapeHtml(e.username)}</td>`;
      }
      html += "</tr>";
    });
    html += "</tbody></table>";
    body.innerHTML = html;

    body.querySelectorAll(".lb-link[data-seed]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const seed = btn.getAttribute("data-seed");
        const d = btn.getAttribute("data-diff");
        loadSharedGame(d, seed, null);
      });
    });
  }

  async function refreshLeaderboard(kind) {
    activeLbKind = kind || activeLbKind;
    const diffKey = Object.keys(DIFFICULTIES).find(
      (k) => DIFFICULTIES[k] === state.cfg
    ) || "intermediate";
    const params = new URLSearchParams({ kind: activeLbKind, difficulty: diffKey });
    if (activeLbKind === "seed" && state && state.seed) {
      params.set("seed", state.seed);
    }
    const body = document.getElementById("leaderboard-body");
    if (body) body.innerHTML = '<p class="lb-empty">Loading…</p>';
    try {
      const data = await apiGet("/leaderboards?" + params.toString());
      renderLeaderboard(data);
    } catch (e) {
      if (body) body.innerHTML = '<p class="lb-empty">Server unreachable.</p>';
    }
  }

  function toggleLeaderboard(forceState) {
    const panel = document.getElementById("leaderboard-panel");
    if (!panel) return;
    const show = forceState !== undefined ? forceState : panel.classList.contains("hidden");
    panel.classList.toggle("hidden", !show);
    panel.setAttribute("aria-hidden", show ? "false" : "true");
    rescaleBoard();
    // Reflow the endgame modal so it doesn't overlap (or leave a hole next to)
    // the leaderboard panel.
    if (!modalBackdrop.classList.contains("hidden")) {
      anchorModalToDefault();
    }
    if (show) refreshLeaderboard();
  }

  function loadSharedGame(difficulty, seed, firstClick) {
    // firstClick null → center click for daily-style flow; player picks first click otherwise.
    if (firstClick === null) {
      // Use a sensible default: center of board
      const cfg = DIFFICULTIES[difficulty];
      firstClick = { r: Math.floor(cfg.rows / 2), c: Math.floor(cfg.cols / 2) };
    }
    pendingSharedGame = { difficulty, seed, r: firstClick.r, c: firstClick.c };
    newGame();
    toggleLeaderboard(false);
  }

  // Track which dailies the user has already played today. If they replay,
  // mark the game as practice (non-scoring) and show the pill so they know.
  const DAILY_PLAYED_KEY = "lying-minesweeper.daily-played";
  function readDailyPlayed() {
    try {
      const raw = localStorage.getItem(DAILY_PLAYED_KEY);
      if (!raw) return {};
      const obj = JSON.parse(raw);
      return obj && typeof obj === "object" ? obj : {};
    } catch (_) { return {}; }
  }
  function markDailyPlayed(date, difficulty, seed) {
    try {
      const all = readDailyPlayed();
      // Reset whenever the date changes so stale entries don't accumulate.
      const fresh = (all.date === date) ? all : { date };
      fresh[difficulty] = seed;
      localStorage.setItem(DAILY_PLAYED_KEY, JSON.stringify(fresh));
    } catch (_) { /* ignore */ }
  }
  function hasPlayedDaily(date, difficulty, seed) {
    const all = readDailyPlayed();
    return all.date === date && all[difficulty] === seed;
  }

  async function playGameOfDay() {
    try {
      // Use the difficulty currently selected in the dropdown
      const diff = difficultyEl ? difficultyEl.value : "intermediate";
      const data = await apiGet("/game-of-day?difficulty=" + encodeURIComponent(diff));
      if (hasPlayedDaily(data.date_utc, data.difficulty, data.seed)) {
        pendingPractice = true;
      } else {
        markDailyPlayed(data.date_utc, data.difficulty, data.seed);
      }
      loadSharedGame(data.difficulty, data.seed, data.first_click);
    } catch (_) {
      if (shareStatusEl) shareStatusEl.textContent = "Server unreachable.";
    }
  }

  function wireLeaderboardUi() {
    const toggleBtn = document.getElementById("toggle-leaderboard");
    const closeBtn = document.getElementById("leaderboard-close");
    const dailyBtn = document.getElementById("game-of-day");
    if (toggleBtn) toggleBtn.addEventListener("click", () => toggleLeaderboard());
    if (closeBtn) closeBtn.addEventListener("click", () => toggleLeaderboard(false));
    if (dailyBtn) dailyBtn.addEventListener("click", playGameOfDay);
    document.querySelectorAll(".lb-tab").forEach((tab) => {
      tab.addEventListener("click", () => {
        document.querySelectorAll(".lb-tab").forEach((t) => t.classList.remove("active"));
        tab.classList.add("active");
        refreshLeaderboard(tab.getAttribute("data-kind"));
      });
    });
  }

  // ---- Server API client ----
  // Same-origin: the API is served from the same host/port as the UI.
  const API_BASE = "/api";
  let currentGameId = null;

  async function apiPost(path, body) {
    const res = await fetch(API_BASE + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error("HTTP " + res.status);
    return res.json();
  }

  async function apiGet(path) {
    const res = await fetch(API_BASE + path);
    if (!res.ok) throw new Error("HTTP " + res.status);
    return res.json();
  }

  async function registerGameWithServer() {
    if (!state || !state.firstClick) return;
    if (state.practice) { currentGameId = null; return; } // practice replays are non-scoring
    try {
      const diffKey = Object.keys(DIFFICULTIES).find(
        (k) => DIFFICULTIES[k] === state.cfg
      );
      const data = await apiPost("/games/new", {
        seed: state.seed,
        difficulty: diffKey,
        first_click: { r: state.firstClick.r, c: state.firstClick.c },
      });
      currentGameId = data.game_id || null;
    } catch (_) {
      currentGameId = null; // server unreachable: don't break gameplay
    }
  }

  async function submitGameToServer() {
    if (!currentGameId) return;
    const username = readUsername();
    if (!username) return;
    try {
      await apiPost(`/games/${currentGameId}/submit`, {
        username,
        outcome: state.won ? "won" : "lost",
        action_log: state.actionLog,
      });
    } catch (_) { /* silent: gameplay unaffected */ }
    currentGameId = null;
  }

  function replayCurrentGame() {
    if (!state || !state.seed) return;
    const diffKey = Object.keys(DIFFICULTIES).find(
      (k) => DIFFICULTIES[k] === state.cfg
    ) || "intermediate";
    const fc = state.firstClick || null;
    hideModal();
    pendingPractice = true;
    loadSharedGame(diffKey, state.seed, fc);
  }

  function restartCurrentGame() {
    // Same as modal Replay: re-enter the current board as a practice game.
    if (!state || !state.seed) return;
    replayCurrentGame();
  }

  // ---- Board auto-scaling ----
  // Recompute --tile-size so the entire board (plus header + any open
  // leaderboard panel) fits the viewport without scrolling.
  function rescaleBoard() {
    if (!state || !state.cfg) return;
    const rows = state.cfg.rows;
    const cols = state.cfg.cols;
    const header = document.getElementById("status-bar");
    const headerH = header ? header.getBoundingClientRect().height : 56;
    const lb = document.getElementById("leaderboard-panel");
    const lbOpen = lb && !lb.classList.contains("hidden");
    const lbW = lbOpen ? lb.getBoundingClientRect().width : 0;
    const pad = 24; // padding around board
    const gap = 2;
    const availW = Math.max(120, window.innerWidth - lbW - pad);
    const availH = Math.max(120, window.innerHeight - headerH - pad);
    // Tile size such that cols*(size+gap)+gap <= availW
    const sizeFromW = Math.floor((availW - gap * (cols + 1)) / cols);
    const sizeFromH = Math.floor((availH - gap * (rows + 1)) / rows);
    let size = Math.min(sizeFromW, sizeFromH);
    size = Math.max(14, Math.min(48, size));
    document.documentElement.style.setProperty("--tile-size", size + "px");
  }
  let dragInfo = null;

  function onDragStart(e) {
    if (!(e.target instanceof HTMLElement)) return;
    const handle = document.getElementById("modal-drag-handle");
    if (!handle || !handle.contains(e.target)) return;
    // Don't hijack pointer events on interactive children (close button, etc.)
    if (e.target.closest("button, a, input, select, textarea")) return;
    e.preventDefault();
    const rect = modalBackdrop.getBoundingClientRect();
    // Switch to left/top positioning anchored to current visual position
    modalBackdrop.style.right = "auto";
    modalBackdrop.style.bottom = "auto";
    modalBackdrop.style.left = rect.left + "px";
    modalBackdrop.style.top = rect.top + "px";
    dragInfo = {
      pointerId: e.pointerId,
      offsetX: e.clientX - rect.left,
      offsetY: e.clientY - rect.top,
    };
    handle.setPointerCapture(e.pointerId);
  }

  function onDragMove(e) {
    if (!dragInfo || e.pointerId !== dragInfo.pointerId) return;
    e.preventDefault();
    modalDragged = true;
    const margin = 4;
    const rect = modalBackdrop.getBoundingClientRect();
    let newLeft = e.clientX - dragInfo.offsetX;
    let newTop = e.clientY - dragInfo.offsetY;
    // Keep at least part of the card on screen
    const minLeft = -rect.width + 80;
    const maxLeft = window.innerWidth - 80;
    const minTop = margin;
    const maxTop = window.innerHeight - 40;
    newLeft = Math.max(minLeft, Math.min(maxLeft, newLeft));
    newTop = Math.max(minTop, Math.min(maxTop, newTop));
    modalBackdrop.style.left = newLeft + "px";
    modalBackdrop.style.top = newTop + "px";
  }

  function onDragEnd(e) {
    if (!dragInfo || e.pointerId !== dragInfo.pointerId) return;
    const handle = document.getElementById("modal-drag-handle");
    if (handle) {
      try { handle.releasePointerCapture(e.pointerId); } catch (_) { /* ignore */ }
    }
    dragInfo = null;
  }


  let pressInfo = null;

  function onPointerDown(e) {
    const target = e.target;
    if (!(target instanceof HTMLElement)) return;
    if (!target.classList.contains("tile")) return;
    if (state.gameOver) return;

    const r = parseInt(target.dataset.r, 10);
    const c = parseInt(target.dataset.c, 10);
    if (Number.isNaN(r) || Number.isNaN(c)) return;

    // Right-click: flag immediately, no need for long-press timer.
    if (e.button === 2) {
      e.preventDefault();
      handleFlag(r, c);
      pressInfo = null;
      return;
    }

    if (e.button !== undefined && e.button !== 0) return;

    pressInfo = {
      r,
      c,
      startX: e.clientX,
      startY: e.clientY,
      startedAt: Date.now(),
      longPressFired: false,
      pointerId: e.pointerId,
      timer: setTimeout(() => {
        if (!pressInfo) return;
        pressInfo.longPressFired = true;
        handleFlag(pressInfo.r, pressInfo.c);
      }, LONG_PRESS_MS),
    };
  }

  function onPointerMove(e) {
    if (!pressInfo) return;
    if (e.pointerId !== pressInfo.pointerId) return;
    const dx = e.clientX - pressInfo.startX;
    const dy = e.clientY - pressInfo.startY;
    if (Math.hypot(dx, dy) > MOVE_TOLERANCE_PX) {
      clearTimeout(pressInfo.timer);
      pressInfo = null;
    }
  }

  function onPointerUp(e) {
    if (!pressInfo) return;
    if (e.pointerId !== pressInfo.pointerId) return;
    clearTimeout(pressInfo.timer);
    const info = pressInfo;
    pressInfo = null;
    if (info.longPressFired) return;
    handleReveal(info.r, info.c);
  }

  function onPointerCancel() {
    if (pressInfo) {
      clearTimeout(pressInfo.timer);
      pressInfo = null;
    }
  }

  function onContextMenu(e) {
    // Suppress native context menu on the board so right-click can flag.
    if (e.target instanceof HTMLElement && e.target.closest(".board")) {
      e.preventDefault();
    }
  }

  // ---- Wire up ----
  function init() {
    boardEl.addEventListener("pointerdown", onPointerDown);
    boardEl.addEventListener("pointermove", onPointerMove);
    boardEl.addEventListener("pointerup", onPointerUp);
    boardEl.addEventListener("pointercancel", onPointerCancel);
    boardEl.addEventListener("pointerleave", onPointerCancel);
    document.addEventListener("contextmenu", onContextMenu);

    newGameBtn.addEventListener("click", newGame);
    if (restartBtn) restartBtn.addEventListener("click", restartCurrentGame);
    modalNewGameBtn.addEventListener("click", newGame);
    if (modalCloseBtn) modalCloseBtn.addEventListener("click", hideModal);
    if (modalShareBtn) modalShareBtn.addEventListener("click", onShareClick);
    if (modalReplayBtn) modalReplayBtn.addEventListener("click", replayCurrentGame);
    difficultyEl.addEventListener("change", newGame);
    if (shareBtn) shareBtn.addEventListener("click", onShareClick);

    // Auto-scale board on window/leaderboard size changes
    window.addEventListener("resize", () => {
      rescaleBoard();
      if (!modalBackdrop.classList.contains("hidden")) anchorModalToDefault();
    });

    // Parse ?d=&seed=&fc= from URL once on init; consumed by first newGame() call.
    pendingSharedGame = readSharedGameFromUrl();

    wireUsernameModal();
    wireLeaderboardUi();

    const dragHandle = document.getElementById("modal-drag-handle");
    if (dragHandle) {
      dragHandle.addEventListener("pointerdown", onDragStart);
      dragHandle.addEventListener("pointermove", onDragMove);
      dragHandle.addEventListener("pointerup", onDragEnd);
      dragHandle.addEventListener("pointercancel", onDragEnd);
    }

    newGame();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
