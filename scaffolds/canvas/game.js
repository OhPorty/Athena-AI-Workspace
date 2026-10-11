// Game shell: a fixed-timestep loop, keyboard and pointer input, and title/playing/paused/over states.
// There are no rules in here. Put your game in reset(), update(dt) and draw().

const canvas = document.getElementById("game");
const ctx = canvas.getContext("2d");
const statusEl = document.getElementById("status");
const startBtn = document.getElementById("start");
const pauseBtn = document.getElementById("pause");

const W = canvas.width;   // logical size; CSS scales the canvas to fit the page
const H = canvas.height;
const STEP = 1 / 60;      // fixed timestep keeps the game identical on every screen

// Colors come from the tokens at the top of style.css, so one edit restyles page and canvas.
const css = getComputedStyle(document.documentElement);
const color = (name) => css.getPropertyValue(name).trim();

let state = "title";      // "title" | "playing" | "paused" | "gameover"
let score = 0;
let best = Number(localStorage.getItem("game-best") || 0);

const keys = new Set();                   // lowercase key names currently held, e.g. "arrowleft", "a", " "
const pointer = { x: 0, y: 0, down: false };   // in logical canvas coordinates

function reset() {
  score = 0;
  // Set up a fresh round here.
}

function update(dt) {
  // Advance the game by dt seconds (always 1/60). Call endGame() when the round is lost.
}

function draw() {
  ctx.fillStyle = color("--panel");
  ctx.fillRect(0, 0, W, H);
  // Draw the game here.

  if (state !== "playing") {
    ctx.fillStyle = color("--ink");
    ctx.textAlign = "center";
    ctx.font = "700 30px system-ui, sans-serif";
    ctx.fillText(state === "paused" ? "Paused" : state === "gameover" ? "Game over" : document.title, W / 2, H / 2);
    ctx.font = "500 16px system-ui, sans-serif";
    ctx.fillText(state === "paused" ? "Press P to continue" : "Press Space or Start", W / 2, H / 2 + 28);
  }
}

function setStatus() {
  if (state === "title") statusEl.textContent = best ? `Best score ${best}. Press Start to play` : "Press Start to play";
  else if (state === "paused") statusEl.textContent = `Paused - score ${score}`;
  else if (state === "gameover") statusEl.textContent = `Game over - score ${score}, best ${best}. Press Start to play again`;
  else statusEl.textContent = `Score ${score}`;
  window.__game = { state, score, best };   // read by automated checks; add whatever else is worth asserting
}

function startGame() { reset(); state = "playing"; startBtn.textContent = "Restart"; setStatus(); }
function endGame() {
  state = "gameover";
  if (score > best) { best = score; localStorage.setItem("game-best", String(best)); }
  startBtn.textContent = "Play again";
  setStatus();
}
function togglePause() {
  if (state === "playing") state = "paused";
  else if (state === "paused") state = "playing";
  setStatus();
}

let last = performance.now();
let acc = 0;
function frame(now) {
  acc += Math.min(0.25, (now - last) / 1000);
  last = now;
  while (acc >= STEP) { if (state === "playing") update(STEP); acc -= STEP; }
  draw();
  requestAnimationFrame(frame);
}

window.addEventListener("keydown", (e) => {
  const k = e.key.toLowerCase();
  if (k === " " || k === "enter") { if (state !== "playing" && state !== "paused") startGame(); e.preventDefault(); return; }
  if (k === "p") { togglePause(); return; }
  keys.add(k);
  if (k.startsWith("arrow")) e.preventDefault();
});
window.addEventListener("keyup", (e) => keys.delete(e.key.toLowerCase()));
window.addEventListener("blur", () => { keys.clear(); if (state === "playing") togglePause(); });

function toLogical(e) {
  const rect = canvas.getBoundingClientRect();
  pointer.x = ((e.clientX - rect.left) / rect.width) * W;
  pointer.y = ((e.clientY - rect.top) / rect.height) * H;
}
canvas.addEventListener("pointerdown", (e) => { pointer.down = true; toLogical(e); canvas.setPointerCapture(e.pointerId); });
canvas.addEventListener("pointermove", toLogical);
canvas.addEventListener("pointerup", () => { pointer.down = false; });
canvas.addEventListener("pointercancel", () => { pointer.down = false; });

startBtn.addEventListener("click", startGame);
pauseBtn.addEventListener("click", togglePause);

setStatus();
requestAnimationFrame(frame);
