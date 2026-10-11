#!/usr/bin/env node
// Braznarkel Ranch Fall Fest: a Jackbox-style party game server.
// The big screen opens /host, players open / on their phones (same Wi-Fi).
// No dependencies: plain Node http, Server-Sent Events for live updates.
"use strict";

const http = require("http");
const fs = require("fs");
const path = require("path");
const os = require("os");
const crypto = require("crypto");

const PORT = +process.env.PORT || 3000;
const HOST_KEY = process.env.HOST_KEY || crypto.randomBytes(3).toString("hex");
const ROOT = __dirname;
const PUBLIC = path.join(ROOT, "public");
const MAX_PLAYERS = 24;

// ---------- helpers ----------
const shuffle = a => { a = a.slice(); for (let i = a.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [a[i], a[j]] = [a[j], a[i]]; } return a; };
const pick = (list, n) => shuffle(list).slice(0, Math.min(n, list.length));
const id = () => crypto.randomBytes(6).toString("hex");
const norm = s => String(s).toLowerCase().replace(/^(the|a|an)\s+/, "").replace(/[^a-z0-9]/g, "");
function lev(a, b) {
  const d = Array.from({ length: a.length + 1 }, (_, i) => [i]);
  for (let j = 1; j <= b.length; j++) d[0][j] = j;
  for (let i = 1; i <= a.length; i++) for (let j = 1; j <= b.length; j++)
    d[i][j] = Math.min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
  return d[a.length][b.length];
}
// "yes" for a match (small typos allowed on longer answers), "close" for a near miss.
function fuzzy(guess, answers) {
  const g = norm(guess); if (!g) return "no";
  let best = Infinity, len = 0;
  for (const a of answers) { const n = norm(a); const d = lev(g, n); if (d < best) { best = d; len = n.length; } }
  const ok = len >= 8 ? 2 : len >= 5 ? 1 : 0;
  return best <= ok ? "yes" : best <= ok + 2 ? "close" : "no";
}

// A small family-friendly filter for text that ends up on the big screen.
const BLOCKED = ["fuck", "shit", "bitch", "cunt", "dick", "pussy", "cock", "nigg", "fag", "slut", "whore", "retard", "rape", "porn", "penis", "vagina", "asshole", "bastard"];
function clean(s, max) {
  let t = String(s || "").replace(/[\u0000-\u001f\u007f]/g, " ").replace(/\s+/g, " ").trim().slice(0, max);
  for (const w of BLOCKED) t = t.replace(new RegExp(w, "gi"), m => m[0] + "*".repeat(m.length - 1));
  return t;
}

const SOURCES = ["trivia", "tf", "fib", "guess", "order", "emoji", "herd", "likely", "draw", "quip"];
let content = loadContent();
function loadContent() {
  try {
    const c = JSON.parse(fs.readFileSync(path.join(ROOT, "content.json"), "utf8"));
    for (const k of SOURCES) if (!Array.isArray(c[k])) c[k] = [];
    c.brand = { ranch: "Braznarkel Ranch", event: "Fall Fest", year: "2026", ...(c.brand || {}) };
    return c;
  } catch (e) {
    console.error("Couldn't read content.json:", e.message);
    return content || Object.fromEntries(SOURCES.map(k => [k, []]).concat([["brand", { ranch: "Braznarkel Ranch", event: "Fall Fest", year: "2026" }]]));
  }
}

// ---------- state ----------
const COLORS = ["#ff7a1a", "#ffc93c", "#e8445a", "#7bd389", "#5bc0eb", "#c38bff", "#ff9fb2", "#f4f1de", "#2ec4b6", "#ffa62b", "#a1c181", "#fe5f55"];
const players = new Map(); // id -> {id,name,emoji,color,score,online}
let G = null;              // the running game, or null in the lobby
let colorIdx = 0;

const online = () => [...players.values()].filter(p => p.online > 0);
const award = (pid, pts) => {
  const p = players.get(pid); if (!p || !pts) return;
  p.score += pts; G.earned[pid] = (G.earned[pid] || 0) + pts;
  if (MIX) MIX.earned[pid] = (MIX.earned[pid] || 0) + pts;
};

// ---------- games ----------
const GAMES = {
  trivia: {
    title: "Pumpkin Patch Pop Quiz",
    blurb: "Multiple choice on your phone. Faster right answers score more.",
    min: 1, rounds: 8, maxRounds: 30, unit: "questions", source: "trivia", mix: 4,
    setup(G) {
      G.items = pick(content.trivia, G.rounds).map(t => {
        const choices = shuffle([t.a, ...(t.wrong || []).slice(0, 3)]);
        return { q: t.q, cat: t.cat || "", choices, correct: choices.indexOf(t.a) };
      });
      G.rounds = G.items.length; G.i = -1;
    },
    begin(G) { this.nextQ(G); },
    nextQ(G) {
      G.i++;
      if (G.i >= G.items.length) return finish(G);
      G.answers = {}; G.qStart = Date.now(); G.qSecs = 20;
      setPhase("question", G.qSecs);
    },
    next(G) {
      if (G.phase === "question") {
        const item = G.items[G.i];
        G.results = {};
        for (const [pid, a] of Object.entries(G.answers)) {
          let pts = 0;
          if (a.c === item.correct) pts = 500 + Math.round(500 * Math.max(0, 1 - (a.t - G.qStart) / (G.qSecs * 1000)));
          award(pid, pts); G.results[pid] = pts;
        }
        setPhase("reveal", 9);
      } else if (G.phase === "reveal") this.nextQ(G);
    },
    act(G, pid, m) {
      if (G.phase !== "question" || G.answers[pid]) return;
      const c = +m.choice;
      if (!(c >= 0 && c < G.items[G.i].choices.length)) return;
      G.answers[pid] = { c, t: Date.now() };
      if (online().every(p => G.answers[p.id])) advance();
    },
    view(G, viewer) {
      const it = G.items[G.i]; if (!it) return {};
      const v = { i: G.i, total: G.items.length, q: it.q, cat: it.cat, choices: it.choices, answered: Object.keys(G.answers) };
      if (viewer !== "host") v.mine = G.answers[viewer] ? G.answers[viewer].c : null;
      if (G.phase === "reveal") {
        v.correct = it.correct; v.results = G.results;
        v.counts = it.choices.map((_, k) => Object.values(G.answers).filter(a => a.c === k).length);
      }
      return v;
    },
  },

  fib: {
    title: "Fib-o-Lantern",
    blurb: "Write a fake answer that sounds true, then find the real one. Fool your friends for points.",
    min: 2, rounds: 4, maxRounds: 10, unit: "questions", source: "fib", mix: 2,
    setup(G) { G.items = pick(content.fib, G.rounds); G.rounds = G.items.length; G.i = -1; },
    begin(G) { this.nextQ(G); },
    nextQ(G) {
      G.i++;
      if (G.i >= G.items.length) return finish(G);
      G.lies = {}; G.picks = {}; G.options = null;
      setPhase("write", 60);
    },
    isTruth(item, text) { const n = norm(text); return [item.a, ...(item.alts || [])].some(x => norm(x) === n); },
    next(G) {
      const item = G.items[G.i];
      if (G.phase === "write") {
        const groups = new Map();
        for (const [pid, text] of Object.entries(G.lies)) {
          const k = norm(text);
          if (!groups.has(k)) groups.set(k, { text, authors: [] });
          groups.get(k).authors.push(pid);
        }
        let opts = [...groups.values()];
        for (const d of shuffle(item.decoys || [])) {
          if (opts.length >= 3) break;
          if (!groups.has(norm(d))) opts.push({ text: d, authors: [], house: true });
        }
        opts.push({ text: item.a, authors: [], truth: true });
        G.options = shuffle(opts);
        setPhase("pick", 30);
      } else if (G.phase === "pick") {
        G.results = {};
        const add = (pid, n) => { award(pid, n); G.results[pid] = (G.results[pid] || 0) + n; };
        for (const [pid, k] of Object.entries(G.picks)) {
          const o = G.options[k];
          if (o.truth) add(pid, 1000);
          else for (const a of o.authors) if (a !== pid) add(a, 500);
        }
        setPhase("reveal", 14);
      } else if (G.phase === "reveal") this.nextQ(G);
    },
    act(G, pid, m) {
      const item = G.items[G.i];
      if (G.phase === "write") {
        const text = clean(m.text, 40);
        if (!text) return { error: "Type a fake answer first." };
        if (this.isTruth(item, text)) return { error: "That's the real answer! Write a fib instead." };
        G.lies[pid] = text;
        if (online().every(p => G.lies[p.id])) advance();
      } else if (G.phase === "pick") {
        const k = +m.choice, o = G.options[k];
        if (!o || G.picks[pid] != null || o.authors.includes(pid)) return;
        G.picks[pid] = k;
        if (online().every(p => G.picks[p.id] != null)) advance();
      }
    },
    view(G, viewer) {
      const item = G.items[G.i]; if (!item) return {};
      const v = { i: G.i, total: G.items.length, q: item.q };
      if (G.phase === "write") {
        v.written = Object.keys(G.lies);
        if (viewer !== "host") v.mine = G.lies[viewer] || null;
      }
      if (G.phase === "pick") {
        v.options = G.options.map(o => o.text);
        v.picked = Object.keys(G.picks);
        if (viewer !== "host") {
          v.own = G.options.map(o => o.authors.includes(viewer));
          v.mine = G.picks[viewer] ?? null;
        }
      }
      if (G.phase === "reveal") {
        v.options = G.options.map((o, k) => ({ text: o.text, truth: !!o.truth, house: !!o.house, authors: o.authors,
          pickers: Object.keys(G.picks).filter(p => G.picks[p] === k) }));
        v.results = G.results;
      }
      return v;
    },
  },

  quip: {
    title: "Hayride Head-to-Head",
    blurb: "Two players answer the same funny prompt. Everyone else votes for the best one.",
    min: 3, rounds: 1, maxRounds: 3, unit: "rounds", source: "quip", mix: 1,
    setup(G) { G.round = 0; G.used = new Set(); },
    begin(G) { this.beginRound(G); },
    beginRound(G) {
      const ps = shuffle(online().map(p => p.id));
      const fresh = content.quip.filter(q => !G.used.has(q));
      const prompts = pick(fresh.length >= ps.length ? fresh : content.quip, ps.length);
      prompts.forEach(p => G.used.add(p));
      const n = ps.length;
      G.matchups = (n === 2 ? [0] : prompts.map((_, i) => i)).map(i => ({
        prompt: prompts[i], a: ps[i], b: ps[(i + 1) % n], ansA: null, ansB: null, votes: {},
      }));
      G.mi = -1;
      setPhase("write", 90);
    },
    tasks(G, pid) {
      const out = [];
      G.matchups.forEach((m, k) => {
        if (m.a === pid) out.push({ m: k, prompt: m.prompt, done: m.ansA != null });
        if (m.b === pid) out.push({ m: k, prompt: m.prompt, done: m.ansB != null });
      });
      return out;
    },
    nextVote(G) {
      G.mi++;
      while (G.mi < G.matchups.length && G.matchups[G.mi].ansA == null && G.matchups[G.mi].ansB == null) G.mi++;
      if (G.mi >= G.matchups.length) {
        G.round++;
        return G.round < G.rounds ? this.beginRound(G) : finish(G);
      }
      const m = G.matchups[G.mi];
      if (m.ansA == null || m.ansB == null) { // a no-show forfeits
        m.forfeit = true; const w = m.ansA != null ? m.a : m.b; m.pts = { [w]: 500 }; award(w, 500);
        return setPhase("result", 6);
      }
      setPhase("vote", 20);
    },
    voters(G) { const m = G.matchups[G.mi]; return online().filter(p => p.id !== m.a && p.id !== m.b); },
    next(G) {
      if (G.phase === "write") return this.nextVote(G);
      if (G.phase === "vote") {
        const m = G.matchups[G.mi];
        const va = Object.values(m.votes).filter(s => s === "a").length, vb = Object.values(m.votes).filter(s => s === "b").length;
        const tot = va + vb;
        m.pts = { [m.a]: tot ? Math.round(1000 * va / tot) : 0, [m.b]: tot ? Math.round(1000 * vb / tot) : 0 };
        if (tot >= 2 && (va === 0 || vb === 0)) { m.sweep = va ? "a" : "b"; m.pts[va ? m.a : m.b] += 250; }
        award(m.a, m.pts[m.a]); award(m.b, m.pts[m.b]);
        return setPhase("result", 8);
      }
      if (G.phase === "result") return this.nextVote(G);
    },
    act(G, pid, m) {
      if (G.phase === "write") {
        const mu = G.matchups[+m.m]; if (!mu) return;
        const text = clean(m.text, 60); if (!text) return { error: "Type an answer first." };
        if (mu.a === pid && mu.ansA == null) mu.ansA = text;
        else if (mu.b === pid && mu.ansB == null) mu.ansB = text;
        else return;
        const waiting = G.matchups.some(x => (x.ansA == null && players.get(x.a)?.online) || (x.ansB == null && players.get(x.b)?.online));
        if (!waiting) advance();
      } else if (G.phase === "vote") {
        const mu = G.matchups[G.mi];
        if (pid === mu.a || pid === mu.b || mu.votes[pid] || !["a", "b"].includes(m.side)) return;
        mu.votes[pid] = m.side;
        if (this.voters(G).every(p => mu.votes[p.id])) advance();
      }
    },
    view(G, viewer) {
      const v = { round: G.round, rounds: G.rounds };
      if (G.phase === "write") {
        const all = G.matchups.flatMap(m => [m.ansA == null ? null : m.a, m.ansB == null ? null : m.b]).filter(Boolean);
        v.done = [...new Set(G.matchups.flatMap(m => [m.a, m.b]))].filter(p => !this.tasks(G, p).some(t => !t.done));
        v.answeredCount = all.length; v.needed = G.matchups.length * 2;
        if (viewer !== "host") v.tasks = this.tasks(G, viewer);
      }
      const m = G.matchups[G.mi];
      if (m && (G.phase === "vote" || G.phase === "result")) {
        v.prompt = m.prompt; v.ansA = m.ansA; v.ansB = m.ansB; v.k = G.mi + 1; v.of = G.matchups.length;
        v.voted = Object.keys(m.votes);
        if (viewer !== "host") { v.isAuthor = viewer === m.a || viewer === m.b; v.mine = m.votes[viewer] || null; }
        if (G.phase === "result") {
          v.a = m.a; v.b = m.b; v.pts = m.pts; v.sweep = m.sweep || null; v.forfeit = !!m.forfeit;
          v.votesA = Object.keys(m.votes).filter(p => m.votes[p] === "a");
          v.votesB = Object.keys(m.votes).filter(p => m.votes[p] === "b");
        }
      }
      return v;
    },
  },

  guess: {
    title: "Guess the Gourd",
    blurb: "Every answer is a number. Closest guess wins. Nail it exactly for a bonus.",
    min: 1, rounds: 6, maxRounds: 15, unit: "questions", source: "guess", mix: 3,
    setup(G) { G.items = pick(content.guess, G.rounds); G.rounds = G.items.length; G.i = -1; },
    begin(G) { this.nextQ(G); },
    nextQ(G) {
      G.i++;
      if (G.i >= G.items.length) return finish(G);
      G.guesses = {};
      setPhase("guess", 30);
    },
    next(G) {
      if (G.phase === "guess") {
        const a = +G.items[G.i].a;
        const rows = Object.entries(G.guesses).map(([pid, g]) => ({ id: pid, g, diff: Math.abs(g - a) })).sort((x, y) => x.diff - y.diff);
        const dists = [...new Set(rows.map(r => r.diff))];
        const prize = [1000, 600, 300];
        rows.forEach(r => {
          r.pts = r.diff === 0 ? 1500 : (prize[dists.indexOf(r.diff) - (dists[0] === 0 ? 1 : 0)] || 0);
          award(r.id, r.pts);
        });
        G.ranked = rows;
        setPhase("reveal", 10);
      } else if (G.phase === "reveal") this.nextQ(G);
    },
    act(G, pid, m) {
      if (G.phase !== "guess" || G.guesses[pid] != null) return;
      const g = Number(String(m.value).replace(/[, ]/g, ""));
      if (!Number.isFinite(g)) return { error: "Enter a number." };
      G.guesses[pid] = g;
      if (online().every(p => G.guesses[p.id] != null)) advance();
    },
    view(G, viewer) {
      const it = G.items[G.i]; if (!it) return {};
      const v = { i: G.i, total: G.items.length, q: it.q, unit: it.unit || "", guessed: Object.keys(G.guesses) };
      if (viewer !== "host") v.mine = G.guesses[viewer] ?? null;
      if (G.phase === "reveal") { v.answer = it.a; v.ranked = G.ranked; }
      return v;
    },
  },

  tf: {
    title: "Fact or Fiction",
    blurb: "Lightning round. Is it true or made up? Answer fast and build a streak.",
    min: 1, rounds: 8, maxRounds: 20, unit: "statements", source: "tf", mix: 5,
    setup(G) { G.items = pick(content.tf, G.rounds); G.rounds = G.items.length; G.i = -1; G.streak = {}; },
    begin(G) { this.nextQ(G); },
    nextQ(G) {
      G.i++;
      if (G.i >= G.items.length) return finish(G);
      G.answers = {}; G.qStart = Date.now(); G.qSecs = 10;
      setPhase("question", G.qSecs);
    },
    next(G) {
      if (G.phase === "question") {
        const truth = !!G.items[G.i].t;
        G.results = {};
        for (const p of players.values()) {
          const a = G.answers[p.id];
          if (a && a.v === truth) {
            G.streak[p.id] = (G.streak[p.id] || 0) + 1;
            const pts = 300 + Math.round(200 * Math.max(0, 1 - (a.t - G.qStart) / (G.qSecs * 1000))) + 100 * (G.streak[p.id] - 1);
            award(p.id, pts); G.results[p.id] = pts;
          } else G.streak[p.id] = 0;
        }
        setPhase("reveal", 7);
      } else if (G.phase === "reveal") this.nextQ(G);
    },
    act(G, pid, m) {
      if (G.phase !== "question" || G.answers[pid] || typeof m.choice !== "boolean") return;
      G.answers[pid] = { v: m.choice, t: Date.now() };
      if (online().every(p => G.answers[p.id])) advance();
    },
    view(G, viewer) {
      const it = G.items[G.i]; if (!it) return {};
      const v = { i: G.i, total: G.items.length, s: it.s, answered: Object.keys(G.answers) };
      if (viewer !== "host") { v.mine = G.answers[viewer] ? G.answers[viewer].v : null; v.streak = G.streak[viewer] || 0; }
      if (G.phase === "reveal") {
        v.truth = !!it.t; v.why = it.why || ""; v.results = G.results;
        v.counts = { true: Object.values(G.answers).filter(a => a.v).length, false: Object.values(G.answers).filter(a => !a.v).length };
        v.streaks = G.streak;
      }
      return v;
    },
  },

  order: {
    title: "Line 'Em Up",
    blurb: "Tap the four answers into the right order. Points for every one in the right spot.",
    min: 1, rounds: 5, maxRounds: 10, unit: "puzzles", source: "order", mix: 3,
    setup(G) { G.items = pick(content.order, G.rounds); G.rounds = G.items.length; G.i = -1; },
    begin(G) { this.nextQ(G); },
    nextQ(G) {
      G.i++;
      if (G.i >= G.items.length) return finish(G);
      const it = G.items[G.i];
      do { G.shown = shuffle(it.items); } while (G.shown.every((x, k) => x === it.items[k]));
      G.answers = {};
      setPhase("question", 35);
    },
    next(G) {
      if (G.phase === "question") {
        const it = G.items[G.i];
        G.results = {}; G.right = {};
        for (const [pid, ans] of Object.entries(G.answers)) {
          const right = ans.filter((s, k) => G.shown[s] === it.items[k]).length;
          const pts = right * 250 + (right === it.items.length ? 250 : 0);
          award(pid, pts); G.results[pid] = pts; G.right[pid] = right;
        }
        setPhase("reveal", 9);
      } else if (G.phase === "reveal") this.nextQ(G);
    },
    act(G, pid, m) {
      if (G.phase !== "question" || G.answers[pid] || !Array.isArray(m.order)) return;
      const n = G.shown.length, o = m.order.map(Number);
      if (o.length !== n || new Set(o).size !== n || o.some(x => !(x >= 0 && x < n))) return { error: "Tap every answer once." };
      G.answers[pid] = o;
      if (online().every(p => G.answers[p.id])) advance();
    },
    view(G, viewer) {
      const it = G.items[G.i]; if (!it) return {};
      const v = { i: G.i, total: G.items.length, q: it.q, shown: G.shown, answered: Object.keys(G.answers) };
      if (viewer !== "host") v.mine = G.answers[viewer] || null;
      if (G.phase === "reveal") { v.correct = it.items; v.results = G.results; v.right = G.right; }
      return v;
    },
  },

  emoji: {
    title: "Emoji Roundup",
    blurb: "Name the movie, song or thing from the emoji. Type it in. Spelling close counts.",
    min: 1, rounds: 6, maxRounds: 20, unit: "puzzles", source: "emoji", mix: 4,
    setup(G) { G.items = pick(content.emoji, G.rounds); G.rounds = G.items.length; G.i = -1; },
    begin(G) { this.nextQ(G); },
    nextQ(G) {
      G.i++;
      if (G.i >= G.items.length) return finish(G);
      G.solved = {}; G.tries = {}; G.qStart = Date.now(); G.qSecs = 30;
      setPhase("question", G.qSecs);
    },
    next(G) {
      if (G.phase === "question") { G.results = {}; for (const [pid, t] of Object.entries(G.solved)) { const pts = 500 + Math.round(500 * Math.max(0, 1 - (t - G.qStart) / (G.qSecs * 1000))); award(pid, pts); G.results[pid] = pts; } setPhase("reveal", 7); }
      else if (G.phase === "reveal") this.nextQ(G);
    },
    act(G, pid, m) {
      if (G.phase !== "question" || G.solved[pid]) return;
      const it = G.items[G.i];
      const r = fuzzy(m.text, [it.a, ...(it.alts || [])]);
      G.tries[pid] = (G.tries[pid] || 0) + 1;
      if (r === "yes") { G.solved[pid] = Date.now(); if (online().every(p => G.solved[p.id])) advance(); return; }
      return { error: r === "close" ? "So close! Check your spelling." : "Nope. Try again!" };
    },
    view(G, viewer) {
      const it = G.items[G.i]; if (!it) return {};
      const v = { i: G.i, total: G.items.length, e: it.e, cat: it.cat || "", solvedIds: Object.keys(G.solved).sort((a, b) => G.solved[a] - G.solved[b]) };
      if (viewer !== "host") { v.solved = !!G.solved[viewer]; v.tries = G.tries[viewer] || 0; }
      if (G.phase === "reveal") { v.answer = it.a; v.results = G.results; }
      return v;
    },
  },

  herd: {
    title: "Herd Mentality",
    blurb: "No right answers. Pick what you think most people will pick. Run with the herd to score.",
    min: 2, rounds: 6, maxRounds: 12, unit: "questions", source: "herd", mix: 3,
    setup(G) { G.items = pick(content.herd, G.rounds); G.rounds = G.items.length; G.i = -1; },
    begin(G) { this.nextQ(G); },
    nextQ(G) {
      G.i++;
      if (G.i >= G.items.length) return finish(G);
      G.answers = {};
      setPhase("question", 15);
    },
    next(G) {
      if (G.phase === "question") {
        const n = G.items[G.i].options.length;
        G.counts = Array.from({ length: n }, (_, k) => Object.values(G.answers).filter(c => c === k).length);
        const top = Math.max(0, ...G.counts);
        G.herd = top ? G.counts.map((c, k) => c === top ? k : -1).filter(k => k >= 0) : [];
        G.results = {};
        for (const [pid, c] of Object.entries(G.answers)) if (G.herd.includes(c)) { award(pid, 500); G.results[pid] = 500; }
        G.lone = Object.keys(G.answers).length >= 3 ? Object.keys(G.answers).filter(pid => G.counts[G.answers[pid]] === 1) : [];
        setPhase("reveal", 8);
      } else if (G.phase === "reveal") this.nextQ(G);
    },
    act(G, pid, m) {
      const c = +m.choice;
      if (G.phase !== "question" || G.answers[pid] != null || !(c >= 0 && c < G.items[G.i].options.length)) return;
      G.answers[pid] = c;
      if (online().every(p => G.answers[p.id] != null)) advance();
    },
    view(G, viewer) {
      const it = G.items[G.i]; if (!it) return {};
      const v = { i: G.i, total: G.items.length, q: it.q, options: it.options, answered: Object.keys(G.answers) };
      if (viewer !== "host") v.mine = G.answers[viewer] ?? null;
      if (G.phase === "reveal") { v.counts = G.counts; v.herd = G.herd; v.results = G.results; v.lone = G.lone; v.picks = G.answers; }
      return v;
    },
  },

  likely: {
    title: "Most Likely To",
    blurb: "Who in this crowd is most likely to…? Vote for a player. Pick who the room picks to score.",
    min: 3, rounds: 5, maxRounds: 15, unit: "questions", source: "likely", mix: 3,
    setup(G) { G.items = pick(content.likely, G.rounds); G.rounds = G.items.length; G.i = -1; },
    begin(G) { this.nextQ(G); },
    nextQ(G) {
      G.i++;
      if (G.i >= G.items.length) return finish(G);
      G.votes = {};
      setPhase("vote", 20);
    },
    next(G) {
      if (G.phase === "vote") {
        const tally = {};
        for (const t of Object.values(G.votes)) tally[t] = (tally[t] || 0) + 1;
        const top = Math.max(0, ...Object.values(tally));
        G.tally = tally; G.crowned = top ? Object.keys(tally).filter(k => tally[k] === top) : [];
        G.results = {};
        const add = (pid, n) => { award(pid, n); G.results[pid] = (G.results[pid] || 0) + n; };
        for (const pid of G.crowned) add(pid, 200);
        for (const [voter, t] of Object.entries(G.votes)) if (G.crowned.includes(t)) add(voter, 300);
        setPhase("reveal", 8);
      } else if (G.phase === "reveal") this.nextQ(G);
    },
    act(G, pid, m) {
      if (G.phase !== "vote" || G.votes[pid] || m.target === pid || !players.has(m.target)) return;
      G.votes[pid] = m.target;
      if (online().every(p => G.votes[p.id])) advance();
    },
    view(G, viewer) {
      const v = { i: G.i, total: G.items.length, q: G.items[G.i], voted: Object.keys(G.votes) };
      if (viewer !== "host") v.mine = G.votes[viewer] || null;
      if (G.phase === "reveal") { v.tally = G.tally; v.crowned = G.crowned; v.results = G.results; }
      return v;
    },
  },

  draw: {
    title: "Scarecrow Sketch",
    blurb: "One player draws a secret word on their phone. Everyone else races to guess it.",
    min: 2, rounds: 3, maxRounds: 12, unit: "drawings", source: "draw", mix: 2,
    setup(G) {
      const ps = shuffle(online().map(p => p.id));
      G.drawers = Array.from({ length: G.rounds }, (_, k) => ps[k % ps.length]);
      G.words = pick(content.draw, G.rounds); G.rounds = Math.min(G.rounds, G.words.length); G.i = -1;
    },
    begin(G) { this.nextQ(G); },
    nextQ(G) {
      G.i++;
      while (G.i < G.rounds && !players.get(G.drawers[G.i])?.online) G.i++;
      if (G.i >= G.rounds) return finish(G);
      G.strokes = []; G.solved = {}; G.feed = []; G.qStart = Date.now(); G.qSecs = 75;
      setPhase("draw", G.qSecs);
    },
    guessers(G) { return online().filter(p => p.id !== G.drawers[G.i]); },
    next(G) {
      if (G.phase === "draw") {
        G.results = {};
        const add = (pid, n) => { award(pid, n); G.results[pid] = (G.results[pid] || 0) + n; };
        for (const [pid, t] of Object.entries(G.solved)) add(pid, 300 + Math.round(500 * Math.max(0, 1 - (t - G.qStart) / (G.qSecs * 1000))));
        const n = Object.keys(G.solved).length;
        if (n) add(G.drawers[G.i], 200 * n);
        setPhase("reveal", 8);
      } else if (G.phase === "reveal") this.nextQ(G);
    },
    act(G, pid, m) {
      if (G.phase !== "draw") return;
      const word = G.words[G.i];
      if (pid === G.drawers[G.i]) {
        if (!Array.isArray(m.strokes)) return;
        const COLORS = ["#1d140e", "#e2702c", "#e7b54a", "#b8322f", "#4f9a95", "#6b8f3a", "#7a4a26", "#ffffff"];
        let budget = 8000;
        G.strokes = m.strokes.slice(0, 400).map(s => {
          const p = Array.isArray(s.p) ? s.p.slice(0, Math.max(0, budget)).map(n => Math.max(0, Math.min(1000, Math.round(+n) || 0))) : [];
          budget -= p.length;
          return { c: COLORS.includes(s.c) ? s.c : COLORS[0], w: Math.max(2, Math.min(40, +s.w || 6)), p: p.length % 2 ? p.slice(0, -1) : p };
        });
        return;
      }
      if (G.solved[pid]) return;
      const text = clean(m.text, 30);
      if (!text) return;
      const r = fuzzy(text, [word.w, ...(word.alts || [])]);
      if (r === "yes") {
        G.solved[pid] = Date.now(); G.feed.push({ id: pid, ok: true });
        if (this.guessers(G).every(p => G.solved[p.id])) advance();
      } else G.feed.push({ id: pid, text });
      G.feed = G.feed.slice(-12);
      if (r === "close") return { error: "So close!" };
      if (r !== "yes") return { error: "Nope, keep guessing." };
    },
    view(G, viewer) {
      const word = G.words[G.i]; if (!word) return {};
      const drawer = G.drawers[G.i];
      const v = { i: G.i, total: G.rounds, drawer, hint: word.w.replace(/[a-z]/gi, "_"), feed: G.feed, solvedIds: Object.keys(G.solved) };
      if (viewer === "host") v.strokes = G.strokes;
      else { v.isDrawer = viewer === drawer; v.solved = !!G.solved[viewer]; if (v.isDrawer) v.word = word.w; }
      if (G.phase === "reveal") { v.word = word.w; v.results = G.results; v.strokes = G.strokes; }
      return v;
    },
  },
};

// ---------- game flow ----------
let timer = null;
function setPhase(phase, secs) {
  clearTimeout(timer); timer = null;
  G.phase = phase; G.step = (G.step || 0) + 1;
  G.deadline = secs ? Date.now() + secs * 1000 : null;
  if (secs) { const step = G.step; timer = setTimeout(() => { if (G && G.step === step) advance(); }, secs * 1000); }
  broadcast();
}
function advance() {
  if (!G) return;
  const mod = GAMES[G.type];
  if (G.phase === "intro") mod.begin(G);
  else if (G.phase === "final") { if (MIX && MIX.i < MIX.queue.length - 1) nextInMix(); }
  else mod.next(G);
  broadcast();
}
function finish(G) {
  // In a mix, the scoreboard shows briefly, then the next game starts on its own.
  if (MIX) {
    MIX.queue = MIX.queue.filter((q, k) => k <= MIX.i || online().length >= GAMES[q.type].min);
    G.nextUp = MIX.i < MIX.queue.length - 1 ? GAMES[MIX.queue[MIX.i + 1].type].title : null;
    return setPhase("final", G.nextUp ? 12 : 0);
  }
  setPhase("final", 0);
}

// ---------- Mix It Up: a random run of mini-games ----------
let MIX = null;
function startMix(count) {
  content = loadContent();
  const n = online().length;
  const ok = Object.keys(GAMES).filter(k => n >= GAMES[k].min && content[GAMES[k].source].length);
  if (!ok.length) return "Get at least one player connected first.";
  const want = Math.max(2, Math.min(12, +count || 6));
  let queue = [];
  while (queue.length < want) {
    const bag = shuffle(ok).filter(k => k !== queue[queue.length - 1]);
    queue.push(...bag);
  }
  queue = queue.slice(0, want).map(type => ({ type, rounds: GAMES[type].mix || 2 }));
  MIX = { queue, i: 0, earned: {} };
  return startGame(queue[0].type, queue[0].rounds, true);
}
function nextInMix() {
  MIX.i++;
  const q = MIX.queue[MIX.i];
  const err = startGame(q.type, q.rounds, true);
  if (err) {
    MIX.queue.splice(MIX.i, 1); MIX.i--;
    if (MIX.i < MIX.queue.length - 1) return nextInMix();
    G.nextUp = null; setPhase("final", 0);
  }
}

function startGame(type, rounds, fromMix) {
  const mod = GAMES[type];
  if (!mod) return "Unknown game.";
  if (!fromMix) { MIX = null; content = loadContent(); }
  if (!content[mod.source].length) return `content.json has no "${mod.source}" questions.`;
  if (online().length < mod.min) return `${mod.title} needs at least ${mod.min} player${mod.min > 1 ? "s" : ""} connected.`;
  G = { type, rounds: Math.max(1, Math.min(mod.maxRounds, +rounds || mod.rounds)), earned: {} };
  mod.setup(G);
  setPhase("intro", fromMix ? 6 : 8);
  return null;
}

// ---------- views ----------
function publicPlayers() {
  return [...players.values()].map(p => ({ id: p.id, name: p.name, emoji: p.emoji, color: p.color, score: p.score, online: p.online > 0 }));
}
function gameView(viewer) {
  if (!G) return null;
  const mod = GAMES[G.type];
  return {
    type: G.type, title: mod.title, blurb: mod.blurb, phase: G.phase, deadline: G.deadline, step: G.step,
    earned: G.earned, nextUp: G.nextUp || null,
    mix: MIX ? { i: MIX.i, n: MIX.queue.length, earned: MIX.earned, last: MIX.i === MIX.queue.length - 1 } : null,
    ...(G.phase === "intro" || G.phase === "final" ? {} : mod.view(G, viewer)),
  };
}
function view(c) {
  const base = { now: Date.now(), brand: content.brand, players: publicPlayers(), game: gameView(c.role === "host" ? "host" : c.pid) };
  if (c.role === "host") {
    return { ...base, role: "host", joinUrl: joinUrl(),
      catalog: Object.entries(GAMES).map(([k, m]) => ({ key: k, title: m.title, blurb: m.blurb, min: m.min, rounds: m.rounds, maxRounds: m.maxRounds, unit: m.unit, available: content[m.source].length, mix: m.mix || 2 })) };
  }
  const me = players.get(c.pid);
  return { ...base, role: "player", me: me ? { id: me.id, name: me.name, emoji: me.emoji, color: me.color, score: me.score } : null };
}

// ---------- live connections (Server-Sent Events) ----------
const clients = new Set();
let queued = false;
function broadcast() {
  if (queued) return; queued = true;
  setImmediate(() => {
    queued = false;
    for (const c of clients) { try { c.res.write(`data: ${JSON.stringify(view(c))}\n\n`); } catch { /* closed */ } }
  });
}
setInterval(() => { for (const c of clients) { try { c.res.write(": ping\n\n"); } catch { /* closed */ } } }, 15000);

// ---------- http ----------
const isLocal = req => ["127.0.0.1", "::1", "::ffff:127.0.0.1"].includes(req.socket.remoteAddress);
const isHost = (req, key) => key === HOST_KEY || isLocal(req);

function lanIPs() {
  const out = [];
  for (const list of Object.values(os.networkInterfaces())) for (const a of list || []) if (a.family === "IPv4" && !a.internal) out.push(a.address);
  return out.sort((a, b) => (/^(192\.168|10\.)/.test(b) ? 1 : 0) - (/^(192\.168|10\.)/.test(a) ? 1 : 0));
}
function joinUrl() {
  if (process.env.PUBLIC_URL) return process.env.PUBLIC_URL.replace(/\/$/, "");
  const ip = lanIPs()[0] || "localhost";
  return `http://${ip}${PORT === 80 ? "" : ":" + PORT}`;
}

const TYPES = { ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon" };
function serveFile(res, file) {
  const full = path.join(PUBLIC, file);
  if (!full.startsWith(PUBLIC + path.sep)) { res.writeHead(404); return res.end(); }
  fs.readFile(full, (err, buf) => {
    if (err) { res.writeHead(404, { "Content-Type": "text/plain" }); return res.end("Not found"); }
    res.writeHead(200, { "Content-Type": TYPES[path.extname(full)] || "application/octet-stream", "Cache-Control": "no-cache" });
    res.end(buf);
  });
}
function readJSON(req) {
  return new Promise(resolve => {
    let body = "";
    req.on("data", d => { body += d; if (body.length > 300000) req.destroy(); });
    req.on("end", () => { try { resolve(JSON.parse(body || "{}")); } catch { resolve({}); } });
  });
}
const json = (res, code, obj) => { res.writeHead(code, { "Content-Type": "application/json" }); res.end(JSON.stringify(obj)); };

const EMOJI = ["🎃", "🍂", "🍁", "🌽", "🦃", "🍎", "🥧", "🦉", "🍄", "🐿️", "👻", "🦇", "🕷️", "🌻", "🧣", "☕", "🍩", "🍏", "🐈‍⬛", "🌰"];

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, "http://x");
  const p = url.pathname;

  if (req.method === "GET") {
    if (p === "/" || p === "/play") return serveFile(res, "play.html");
    if (p === "/host") return serveFile(res, "host.html");
    if (p === "/events") {
      const role = url.searchParams.get("role") === "host" ? "host" : "player";
      if (role === "host" && !isHost(req, url.searchParams.get("key"))) return json(res, 403, { error: "Wrong host key." });
      res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache", Connection: "keep-alive", "X-Accel-Buffering": "no" });
      res.write("retry: 1500\n\n");
      const c = { res, role, pid: url.searchParams.get("id") || null };
      clients.add(c);
      const pl = c.pid && players.get(c.pid);
      if (pl) pl.online++;
      res.write(`data: ${JSON.stringify(view(c))}\n\n`);
      broadcast();
      req.on("close", () => {
        clients.delete(c);
        const pl2 = c.pid && players.get(c.pid);
        if (pl2) pl2.online = Math.max(0, pl2.online - 1);
        broadcast();
      });
      return;
    }
    if (p.startsWith("/static/")) return serveFile(res, p.slice(8));
    res.writeHead(404, { "Content-Type": "text/plain" }); return res.end("Not found");
  }

  if (req.method !== "POST") { res.writeHead(405); return res.end(); }
  const body = await readJSON(req);

  if (p === "/api/join") {
    const existing = body.id && players.get(body.id);
    let name = clean(body.name, 14);
    if (!name) return json(res, 400, { error: "Enter a name." });
    const taken = n => [...players.values()].some(x => x.name.toLowerCase() === n.toLowerCase() && x !== existing);
    if (taken(name)) { let k = 2; while (taken(`${name.slice(0, 12)}${k}`)) k++; name = `${name.slice(0, 12)}${k}`; }
    const emoji = EMOJI.includes(body.emoji) ? body.emoji : EMOJI[Math.floor(Math.random() * EMOJI.length)];
    if (existing) { existing.name = name; existing.emoji = emoji; broadcast(); return json(res, 200, { id: existing.id }); }
    if (players.size >= MAX_PLAYERS) return json(res, 400, { error: "The game is full." });
    const pl = { id: id(), name, emoji, color: COLORS[colorIdx++ % COLORS.length], score: 0, online: 0 };
    players.set(pl.id, pl);
    broadcast();
    return json(res, 200, { id: pl.id });
  }

  if (p === "/api/act") {
    if (!players.has(body.id) || !G) return json(res, 200, { ok: false });
    const r = GAMES[G.type].act(G, body.id, body) || {};
    broadcast();
    return json(res, 200, { ok: !r.error, ...r });
  }

  if (p === "/api/host") {
    if (!isHost(req, body.key)) return json(res, 403, { error: "Wrong host key." });
    let error = null;
    switch (body.cmd) {
      case "start": error = startGame(body.type, body.rounds); break;
      case "next": advance(); break;
      case "mix": error = startMix(body.count); break;
      case "lobby": clearTimeout(timer); G = null; MIX = null; content = loadContent(); break;
      case "kick": players.delete(body.pid); break;
      case "resetScores": for (const pl of players.values()) pl.score = 0; break;
      case "clearPlayers": if (!G) { players.clear(); colorIdx = 0; } break;
      default: error = "Unknown command.";
    }
    broadcast();
    return json(res, error ? 400 : 200, error ? { error } : { ok: true });
  }

  res.writeHead(404); res.end();
});

server.on("error", e => {
  if (e.code === "EADDRINUSE") {
    console.error(`\n  Port ${PORT} is already in use. The game is probably already running in another window.`);
    console.error(`  Close that window first, or start on another port, for example: PORT=${PORT + 1} node server.js\n`);
    process.exit(1);
  }
  throw e;
});
server.listen(PORT, "0.0.0.0", () => {
  const ips = lanIPs();
  const b = content.brand;
  console.log(`\n  🤠  ${b.ranch} ${b.year} ${b.event} is running\n`);
  console.log(`  Big screen (open on this computer):  http://localhost:${PORT}/host`);
  for (const ip of ips) console.log(`  Big screen from another device:      http://${ip}:${PORT}/host?key=${HOST_KEY}`);
  console.log(`\n  Players join at:                     ${joinUrl()}`);
  if (!ips.length) console.log("  (No Wi-Fi network found. Connect this computer to the same Wi-Fi as the players.)");
  console.log("\n  Press Ctrl+C to stop.\n");
});
