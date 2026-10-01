"use strict";

const tg = window.Telegram && window.Telegram.WebApp;
const PAGE = 30;
const state = { status: "published", view: "grid", posts: [], offset: 0, channel: null, editing: null, files: [] };
const $ = (id) => document.getElementById(id);

// ---------- утилиты ----------
function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) if (c != null) el.append(c instanceof Node ? c : document.createTextNode(c));
  return el;
}
function icon(path) {
  const span = document.createElement("span");
  span.innerHTML = `<svg viewBox="0 0 24 24">${path}</svg>`; // только константы из кода
  return span.firstChild;
}
const ICONS = {
  send: '<path d="M22 3 11 14M22 3l-7 19-4-8-8-4z"/>',
  edit: '<path d="M4 20h4L19 9l-4-4L4 16z"/>',
  trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
  open: '<path d="M14 4h6v6M20 4l-9 9M18 14v6H4V6h6"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  album: '<rect x="7" y="7" width="14" height="14" rx="2"/><path d="M3 17V5a2 2 0 0 1 2-2h12"/>',
  video: '<path d="M8 5v14l11-7z"/>',
};
function toast(text) {
  const t = $("toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (t.hidden = true), 2600);
}
function haptic(type = "success") {
  if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred(type);
}
const fmtDate = (iso) =>
  new Date(iso).toLocaleString("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
function relative(iso) {
  const diff = (new Date(iso) - Date.now()) / 1000;
  const rtf = new Intl.RelativeTimeFormat("ru", { numeric: "auto" });
  const abs = Math.abs(diff);
  if (abs < 60) return rtf.format(Math.round(diff), "second");
  if (abs < 3600) return rtf.format(Math.round(diff / 60), "minute");
  if (abs < 86400) return rtf.format(Math.round(diff / 3600), "hour");
  if (abs < 86400 * 7) return rtf.format(Math.round(diff / 86400), "day");
  return fmtDate(iso);
}
function toLocalInput(date) {
  const d = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
  return d.toISOString().slice(0, 16);
}

// ---------- API ----------
function authHeaders() {
  if (tg && tg.initData) return { "X-Telegram-Init-Data": tg.initData };
  const token = localStorage.getItem("adminToken");
  return token ? { "X-Admin-Token": token } : {};
}
async function api(path, opts = {}) {
  const res = await fetch(path, { ...opts, headers: { ...authHeaders(), ...(opts.headers || {}) } });
  if (res.status === 401 || res.status === 403) {
    const body = await res.json().catch(() => ({}));
    showLogin(body.detail);
    throw new Error(body.detail || "Нет доступа");
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    const detail = Array.isArray(body.detail) ? body.detail.map((d) => d.msg).join("; ") : body.detail;
    throw new Error(detail || `Ошибка ${res.status}`);
  }
  return res.status === 204 ? null : res.json();
}

// ---------- шапка профиля ----------
function avatarEl(size = "") {
  const ch = state.channel;
  const inner = ch && ch.avatar ? h("img", { src: ch.avatar, alt: "" }) : h("div", {}, ((ch && ch.title) || "?")[0]);
  return h("div", { class: `avatar ${size}` }, inner);
}
async function loadChannel() {
  state.channel = await api("/api/channel");
  const ch = state.channel;
  $("topName").textContent = ch.username ? ch.username : ch.title;
  $("title").textContent = ch.title;
  $("avatar").replaceWith(Object.assign(avatarEl("avatar--lg"), { id: "avatar" }));
  $("statPublished").textContent = ch.stats.published;
  $("statScheduled").textContent = ch.stats.scheduled;
  $("statDraft").textContent = ch.stats.draft;
  $("slotsInfo").textContent = ch.slots.length
    ? `Автопубликация: ${ch.slots.join(", ")} (${ch.timezone})`
    : "Слоты расписания не заданы";
  $("nextSlot").textContent = `— ${fmtDate(ch.next_slot)}`;
  const failedTab = document.querySelector('[data-status="failed"]');
  failedTab.style.color = ch.stats.failed ? "var(--danger)" : "";
}

async function loadStories() {
  const upcoming = await api("/api/posts?status=scheduled&limit=15");
  const box = $("stories");
  box.replaceChildren(
    h("button", { class: "story story--add", onclick: () => openEditor() },
      h("div", { class: "avatar" }, h("div", {}, "+")), "Новый"),
    ...upcoming.map((p) => {
      const first = p.media[0];
      const inner = first && first.type === "photo" ? h("img", { src: first.url, alt: "" }) : h("div", {}, first ? "▶" : "Aa");
      return h("button", { class: "story", onclick: () => jumpTo("scheduled", p.id) },
        h("div", { class: "avatar" }, inner),
        new Date(p.scheduled_at).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" }));
    }),
  );
}

// ---------- посты ----------
async function loadPosts(reset = true) {
  if (reset) { state.offset = 0; state.posts = []; }
  const page = await api(`/api/posts?status=${state.status}&limit=${PAGE}&offset=${state.offset}`);
  state.posts.push(...page);
  state.offset += page.length;
  $("more").hidden = page.length < PAGE;
  render();
}

function mediaEl(m, attrs = {}) {
  return m.type === "video"
    ? h("video", { src: m.url, muted: true, playsinline: true, preload: "metadata", ...attrs })
    : h("img", { src: m.url, loading: "lazy", alt: "", ...attrs });
}

function postTime(p) {
  if (p.status === "published") return relative(p.published_at);
  if (p.status === "scheduled") return `${relative(p.scheduled_at)} · ${fmtDate(p.scheduled_at)}`;
  return fmtDate(p.created_at);
}

function tile(p) {
  const first = p.media[0];
  return h("button", { class: "tile", onclick: () => jumpTo(state.status, p.id) },
    first ? mediaEl(first) : h("div", { class: "tile__text" }, p.caption),
    p.media.length > 1 ? h("span", { class: "tile__badge" }, icon(ICONS.album))
      : first && first.type === "video" ? h("span", { class: "tile__badge" }, icon(ICONS.video)) : null,
    p.status === "scheduled" ? h("span", { class: "tile__time" }, fmtDate(p.scheduled_at)) : null,
  );
}

function carousel(media) {
  const track = h("div", { class: "carousel__track" }, media.map((m) => mediaEl(m, m.type === "video" ? { controls: true } : {})));
  if (media.length < 2) return h("div", { class: "carousel" }, track);
  const count = h("span", { class: "carousel__count" }, `1/${media.length}`);
  const dots = h("div", { class: "dots" }, media.map((_, i) => h("i", { class: i === 0 ? "on" : "" })));
  track.addEventListener("scroll", () => {
    const i = Math.round(track.scrollLeft / track.clientWidth);
    count.textContent = `${i + 1}/${media.length}`;
    [...dots.children].forEach((d, j) => d.classList.toggle("on", i === j));
  }, { passive: true });
  return [h("div", { class: "carousel" }, track, count), dots];
}

const STATUS_LABEL = { scheduled: "В очереди", draft: "Черновик", failed: "Ошибка", publishing: "Публикуется…" };

function card(p) {
  const ch = state.channel || {};
  const editable = ["draft", "scheduled", "failed"].includes(p.status);
  const caption = h("div", { class: "card__caption" }, p.caption);
  const long = p.media.length && p.caption.length > 120;
  if (long) caption.classList.add("clamp");
  const moreBtn = long ? h("button", { class: "card__more", onclick: (e) => { caption.classList.remove("clamp"); e.target.remove(); } }, "ещё") : null;

  return h("article", { class: "card", id: `post-${p.id}` },
    h("header", { class: "card__head" },
      avatarEl("avatar--sm"),
      h("div", { class: "card__who" }, h("b", {}, ch.username || ch.title || "канал"), h("span", { class: "muted" }, postTime(p))),
      STATUS_LABEL[p.status] ? h("span", { class: `badge badge--${p.status}` }, STATUS_LABEL[p.status]) : null,
    ),
    p.media.length ? carousel(p.media) : h("div", { class: "card__textonly" }, p.caption),
    h("div", { class: "card__actions" },
      p.link ? h("a", { href: p.link, target: "_blank", title: "Открыть в Telegram", onclick: (e) => openLink(e, p.link) }, icon(ICONS.open)) : null,
      editable ? h("button", { title: "Опубликовать сейчас", onclick: () => publishNow(p) }, icon(ICONS.send)) : null,
      editable ? h("button", { title: "Редактировать", onclick: () => openEditor(p) }, icon(ICONS.edit)) : null,
      h("span", { class: "spacer" }),
      h("button", { title: "Удалить", onclick: () => removePost(p) }, icon(ICONS.trash)),
    ),
    p.media.length && p.caption ? [caption, moreBtn] : null,
    p.error ? h("div", { class: "card__error" }, `⚠️ ${p.error}`) : null,
    p.status === "published" ? h("div", { class: "card__date muted" }, fmtDate(p.published_at)) : null,
  );
}

function render() {
  const empty = state.posts.length === 0;
  $("empty").hidden = !empty;
  $("grid").hidden = state.view !== "grid";
  $("feed").hidden = state.view !== "feed";
  if (state.view === "grid") $("grid").replaceChildren(...state.posts.map(tile));
  else $("feed").replaceChildren(...state.posts.map(card));
}

async function refresh() {
  await Promise.all([loadChannel(), loadStories(), loadPosts()]);
}

function setTab(status) {
  state.status = status;
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.status === status));
}
function setView(view) {
  state.view = view;
  document.querySelectorAll(".view-toggle button").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  render();
}
async function jumpTo(status, id) {
  if (state.status !== status) { setTab(status); await loadPosts(); }
  setView("feed");
  const el = $(`post-${id}`);
  if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
}
function openLink(e, url) {
  if (tg && tg.openTelegramLink && url.startsWith("https://t.me/")) { e.preventDefault(); tg.openTelegramLink(url); }
}

// ---------- действия ----------
async function confirmDialog(text) {
  if (tg && tg.showConfirm && tg.initData) return new Promise((resolve) => tg.showConfirm(text, resolve));
  return confirm(text);
}
async function publishNow(p) {
  if (!(await confirmDialog("Опубликовать пост в канал сейчас?"))) return;
  try {
    const res = await api(`/api/posts/${p.id}/publish`, { method: "POST" });
    if (res.status === "published") { haptic(); toast("Опубликовано 🚀"); }
    else { haptic("error"); toast(res.error || "Ошибка публикации"); }
    await refresh();
  } catch (e) { toast(e.message); }
}
async function removePost(p) {
  let fromChannel = false;
  if (p.status === "published") {
    if (!(await confirmDialog("Удалить пост из ленты И из канала?"))) return;
    fromChannel = true;
  } else if (!(await confirmDialog("Удалить пост?"))) return;
  try {
    await api(`/api/posts/${p.id}?from_channel=${fromChannel}`, { method: "DELETE" });
    toast("Удалено");
    await refresh();
  } catch (e) { toast(e.message); }
}

// ---------- редактор ----------
function updateCounter() {
  const hasMedia = state.editing ? state.editing.media.length > 0 : state.files.length > 0;
  const limit = hasMedia ? 1024 : 4096;
  $("limit").textContent = limit;
  $("counter").textContent = $("caption").value.length;
  $("counter").parentElement.style.color = $("caption").value.length > limit ? "var(--danger)" : "";
}
function renderPreviews() {
  $("previews").replaceChildren(...state.files.map((f, i) => {
    const url = URL.createObjectURL(f);
    const media = f.type.startsWith("video/") ? h("video", { src: url, muted: true }) : h("img", { src: url, alt: "" });
    return h("div", { class: "pv" }, media,
      h("button", { type: "button", onclick: () => { state.files.splice(i, 1); renderPreviews(); } }, "×"));
  }));
  updateCounter();
}
function openEditor(post = null) {
  state.editing = post;
  state.files = [];
  $("editorTitle").textContent = post ? `Пост #${post.id}` : "Новый пост";
  $("mediaPicker").hidden = !!post;
  $("caption").value = post ? post.caption : "";
  $("editorError").textContent = "";
  const mode = post ? (post.status === "draft" ? "draft" : "at") : "queue";
  document.querySelector(`input[name=mode][value=${mode}]`).checked = true;
  const when = post && post.scheduled_at ? new Date(post.scheduled_at) : new Date(Date.now() + 3600e3);
  $("whenInput").value = toLocalInput(when);
  $("whenInput").hidden = mode !== "at";
  renderPreviews();
  $("editor").showModal();
}

async function submitEditor(e) {
  e.preventDefault();
  const mode = document.querySelector("input[name=mode]:checked").value;
  const caption = $("caption").value;
  const btn = $("editorSubmit");
  btn.disabled = true;
  $("editorError").textContent = "";
  try {
    let res;
    const whenIso = mode === "at" ? new Date($("whenInput").value).toISOString() : null;
    if (mode === "at" && new Date(whenIso) < Date.now()) throw new Error("Время уже прошло");
    if (state.editing) {
      const patch = { caption };
      if (mode === "at") patch.scheduled_at = whenIso;
      if (mode === "draft") patch.status = "draft";
      if (mode === "queue") patch.status = "scheduled";
      res = await api(`/api/posts/${state.editing.id}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch),
      });
      if (mode === "now") res = await api(`/api/posts/${state.editing.id}/publish`, { method: "POST" });
    } else {
      const form = new FormData();
      form.append("caption", caption);
      form.append("mode", mode);
      if (whenIso) form.append("scheduled_at", whenIso);
      state.files.forEach((f) => form.append("files", f));
      res = await api("/api/posts", { method: "POST", body: form });
    }
    if (res.status === "failed") throw new Error(res.error || "Ошибка публикации");
    $("editor").close();
    haptic();
    toast({ published: "Опубликовано 🚀", scheduled: `Запланировано на ${fmtDate(res.scheduled_at || Date.now())}`, draft: "Черновик сохранён" }[res.status] || "Сохранено");
    if (res.status !== state.status) setTab(res.status);
    await refresh();
  } catch (err) {
    haptic("error");
    $("editorError").textContent = err.message;
  } finally {
    btn.disabled = false;
  }
}

// ---------- вход ----------
function showLogin(message) {
  $("app").hidden = true;
  $("login").hidden = false;
  $("loginError").textContent = message || "";
  if (tg && tg.initData) $("loginForm").hidden = true; // внутри Telegram пароль не поможет
}

// ---------- старт ----------
function init() {
  if (tg && tg.initData) {
    document.documentElement.classList.add("tg");
    tg.ready();
    tg.expand();
  }
  $("tabs").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (b) { setTab(b.dataset.status); loadPosts().catch((err) => toast(err.message)); }
  });
  document.querySelector(".view-toggle").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (b) setView(b.dataset.view);
  });
  $("more").addEventListener("click", () => loadPosts(false).catch((err) => toast(err.message)));
  $("newBtn").addEventListener("click", () => openEditor());
  $("editorForm").addEventListener("submit", submitEditor);
  document.querySelector("[data-close]").addEventListener("click", () => $("editor").close());
  $("caption").addEventListener("input", updateCounter);
  $("fileInput").addEventListener("change", (e) => {
    state.files.push(...e.target.files);
    if (state.files.length > 10) { state.files.length = 10; toast("Максимум 10 файлов"); }
    e.target.value = "";
    renderPreviews();
  });
  $("whenSet").addEventListener("change", () => {
    $("whenInput").hidden = document.querySelector("input[name=mode]:checked").value !== "at";
  });
  $("loginForm").addEventListener("submit", (e) => {
    e.preventDefault();
    localStorage.setItem("adminToken", $("tokenInput").value);
    start();
  });
  start();
}
async function start() {
  try {
    await refresh();
    $("login").hidden = true;
    $("app").hidden = false;
  } catch (e) {
    if ($("login").hidden) toast(e.message);
  }
}
init();
// счётчики и «истории» обновляем фоном; саму ленту не трогаем, чтобы не сбивать прокрутку
setInterval(() => { if (!$("app").hidden) Promise.all([loadChannel(), loadStories()]).catch(() => {}); }, 60000);
