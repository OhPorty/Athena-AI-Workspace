// A small, complete canvas game to build from.
// Structure: theme -> constants -> state -> input -> update(dt) -> draw() -> loop.
// Everything is drawn with canvas shapes, so there are no asset files to manage.

// The look of the game: colors, what you collect, what you avoid, and the title.
// THEME:START
const THEME = {};
// THEME:END

const canvas = document.getElementById("game");
const ctx = canvas.getContext("2d");
const statusEl = document.getElementById("status");
const startBtn = document.getElementById("start");
const pauseBtn = document.getElementById("pause");

const W = canvas.width;   // logical size; CSS scales the canvas to fit the page
const H = canvas.height;
const STEP = 1 / 60;      // fixed timestep keeps gameplay identical on every screen
const START_LIVES = 3;

const player = { x: W / 2, y: H - 34, w: 72, h: 14, speed: 380, vx: 0 };
let state = "title";      // "title" | "playing" | "paused" | "gameover"
let score = 0;
let lives = START_LIVES;
let best = Number(localStorage.getItem("game-best") || 0);
let items = [];           // falling good things (collect) and bad things (avoid)
let sparks = [];          // particles when something good is caught
let spawnTimer = 0;
let elapsed = 0;
let seed = 7;

// Small seeded generator: the same run plays the same way, which makes bugs reproducible.
function rand() {
  seed = (seed * 16807) % 2147483647;
  return (seed - 1) / 2147483646;
}

const keys = new Set();
let dragX = null;

function setStatus() {
  if (state === "title") statusEl.textContent = best ? `Best score ${best}. Press Start to play` : "Press Start to play";
  else if (state === "paused") statusEl.textContent = `Paused - score ${score}`;
  else if (state === "gameover") statusEl.textContent = `Game over - score ${score}, best ${best}. Press Start to play again`;
  else statusEl.textContent = `Score ${score} - lives ${lives}`;
  window.__game = { state, score, lives, best };   // read by automated checks
}

function startGame() {
  score = 0; lives = START_LIVES; items = []; sparks = []; spawnTimer = 0; elapsed = 0; seed = 7;
  player.x = W / 2; player.vx = 0;
  state = "playing";
  startBtn.textContent = "Restart";
  setStatus();
}

function togglePause() {
  if (state === "playing") state = "paused";
  else if (state === "paused") state = "playing";
  setStatus();
}

function endGame() {
  state = "gameover";
  if (score > best) { best = score; localStorage.setItem("game-best", String(best)); }
  startBtn.textContent = "Play again";
  setStatus();
}

function spawn() {
  const bad = rand() < Math.min(0.45, 0.2 + elapsed * 0.004);
  items.push({
    good: !bad,
    x: 24 + rand() * (W - 48),
    y: -20,
    r: bad ? 14 : 11,
    vy: 90 + rand() * 70 + elapsed * 3,
    spin: rand() * 6,
  });
}

function update(dt) {
  if (state !== "playing") return;
  elapsed += dt;

  const left = keys.has("arrowleft") || keys.has("a");
  const right = keys.has("arrowright") || keys.has("d");
  player.vx = (right - left) * player.speed;
  if (dragX !== null) player.x += (dragX - player.x) * Math.min(1, dt * 14);
  player.x = Math.max(player.w / 2, Math.min(W - player.w / 2, player.x + player.vx * dt));

  spawnTimer -= dt;
  if (spawnTimer <= 0) { spawn(); spawnTimer = Math.max(0.35, 0.9 - elapsed * 0.01); }

  for (const it of items) { it.y += it.vy * dt; it.spin += dt * 3; }
  for (const it of items) {
    const hit = it.y + it.r >= player.y && it.y - it.r <= player.y + player.h && Math.abs(it.x - player.x) <= player.w / 2 + it.r * 0.6;
    if (!hit) continue;
    it.dead = true;
    if (it.good) {
      score += 1;
      for (let i = 0; i < 8; i++) sparks.push({ x: it.x, y: it.y, vx: (rand() - 0.5) * 220, vy: -rand() * 180, life: 0.5 });
    } else {
      lives -= 1;
      if (lives <= 0) endGame();
    }
    if (state === "playing") setStatus();
  }
  // Anything that falls past the bottom is simply gone; missing a good one costs nothing.
  items = items.filter((it) => !it.dead && it.y < H + 30);

  for (const s of sparks) { s.x += s.vx * dt; s.y += s.vy * dt; s.vy += 500 * dt; s.life -= dt; }
  sparks = sparks.filter((s) => s.life > 0);
}

// Shapes a theme can use for what you collect or avoid: star, coin, gem, drop, spike, rock.
function polygon(x, y, points, rot, radius, inner) {
  ctx.beginPath();
  for (let i = 0; i < points * 2; i++) {
    const a = rot + (i * Math.PI) / points - Math.PI / 2;
    const rad = i % 2 ? radius * inner : radius;
    ctx.lineTo(x + Math.cos(a) * rad, y + Math.sin(a) * rad);
  }
  ctx.closePath();
  ctx.fill();
}

function drawShape(shape, x, y, r, rot, color, accent) {
  ctx.fillStyle = color;
  if (shape === "star") polygon(x, y, 5, rot * 0.3, r, 0.45);
  else if (shape === "spike") polygon(x, y, 8, rot * 0.5, r, 0.5);
  else if (shape === "gem") {
    ctx.beginPath(); ctx.moveTo(x, y - r); ctx.lineTo(x + r * 0.85, y); ctx.lineTo(x, y + r); ctx.lineTo(x - r * 0.85, y); ctx.closePath(); ctx.fill();
    ctx.strokeStyle = accent; ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(x - r * 0.85, y); ctx.lineTo(x + r * 0.85, y); ctx.stroke();
  } else if (shape === "drop") {
    ctx.beginPath(); ctx.moveTo(x, y - r * 1.3); ctx.quadraticCurveTo(x + r * 1.15, y + r * 0.3, x, y + r); ctx.quadraticCurveTo(x - r * 1.15, y + r * 0.3, x, y - r * 1.3); ctx.fill();
  } else if (shape === "coin") {
    ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.fill();
    ctx.strokeStyle = accent; ctx.lineWidth = 2; ctx.beginPath(); ctx.arc(x, y, r * 0.6, 0, Math.PI * 2); ctx.stroke();
  } else {
    ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = accent; ctx.beginPath(); ctx.arc(x - r * 0.28, y - r * 0.22, r * 0.35, 0, Math.PI * 2); ctx.fill();
  }
}

function draw() {
  const sky = ctx.createLinearGradient(0, 0, 0, H);
  sky.addColorStop(0, THEME.sky[0]);
  sky.addColorStop(1, THEME.sky[1]);
  ctx.fillStyle = sky;
  ctx.fillRect(0, 0, W, H);

  ctx.fillStyle = THEME.specks;
  for (let i = 0; i < 40; i++) ctx.fillRect((i * 97) % W, (i * 53) % H, 2, 2);   // fixed background stars

  for (const it of items) {
    if (it.good) drawShape(THEME.collectible, it.x, it.y, it.r, it.spin, THEME.collectColor, THEME.collectAccent);
    else drawShape(THEME.hazard, it.x, it.y, it.r, it.spin, THEME.hazardColor, THEME.hazardAccent);
  }
  ctx.fillStyle = THEME.collectColor;
  for (const s of sparks) ctx.fillRect(s.x, s.y, 3, 3);

  ctx.fillStyle = THEME.player;
  ctx.beginPath();
  ctx.roundRect(player.x - player.w / 2, player.y, player.w, player.h, 7);
  ctx.fill();

  ctx.fillStyle = THEME.ink;
  ctx.font = "600 18px system-ui, sans-serif";
  ctx.textAlign = "left";
  ctx.fillText(`Score ${score}`, 14, 26);
  ctx.textAlign = "right";
  ctx.fillText("♥".repeat(Math.max(0, lives)), W - 14, 26);

  if (state !== "playing") {
    ctx.fillStyle = THEME.sky[1] + "cc";
    ctx.fillRect(0, 0, W, H);
    ctx.fillStyle = THEME.ink;
    ctx.textAlign = "center";
    ctx.font = "700 34px system-ui, sans-serif";
    ctx.fillText(state === "title" ? THEME.title : state === "paused" ? "Paused" : "Game over", W / 2, H / 2 - 6);
    ctx.font = "500 16px system-ui, sans-serif";
    ctx.fillText(state === "paused" ? "Press P to continue" : "Press Space or Start", W / 2, H / 2 + 26);
  }
}

let last = performance.now();
let acc = 0;
function frame(now) {
  acc += Math.min(0.25, (now - last) / 1000);
  last = now;
  while (acc >= STEP) { update(STEP); acc -= STEP; }
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

function pointerToLogicalX(e) {
  const rect = canvas.getBoundingClientRect();
  return ((e.clientX - rect.left) / rect.width) * W;
}
canvas.addEventListener("pointerdown", (e) => { dragX = pointerToLogicalX(e); canvas.setPointerCapture(e.pointerId); });
canvas.addEventListener("pointermove", (e) => { if (dragX !== null) dragX = pointerToLogicalX(e); });
canvas.addEventListener("pointerup", () => { dragX = null; });
canvas.addEventListener("pointercancel", () => { dragX = null; });

startBtn.addEventListener("click", startGame);
pauseBtn.addEventListener("click", togglePause);

setStatus();
requestAnimationFrame(frame);
