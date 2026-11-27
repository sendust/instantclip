"use strict";
/* 4. 호스트 미디어 브라우저: capture panel (recorded / recording clips) + subclip panel */
class FileBrowser {
  constructor(el) {
    this.el = el;
    this.root = el.dataset.root;
    this.path = localStorage.getItem(`ic.fb.${this.root}`) || "";
    this.data = { folders: [], files: [] };
    this.sel = new Set();     // selected paths
    this.cursor = null;       // the keyboard row (outline); kept apart from the selection, by path
    this.anchor = null;       // where a Shift range starts
    this.list = $(".fb-list", el);
    $$(".fb-tools [data-op]", el).forEach((b) => { b.onclick = () => this.op(b.dataset.op); });
    // sort: click a column header (이름 / 길이 / 용량 / 날짜); the same header again reverses it.
    // Remembered per panel
    this.sortKey = localStorage.getItem(`ic.sort.${this.root}`) || "mtime";
    if (!["name", "duration", "size", "mtime"].includes(this.sortKey)) this.sortKey = "mtime";
    this.sortDesc = localStorage.getItem(`ic.sortDesc.${this.root}`) !== "0";
    $$(".fb-cols [data-sort]", el).forEach((h) => {
      h.addEventListener("mousedown", (e) => e.preventDefault());   // keep focus in the list
      h.onclick = () => {
        const k = h.dataset.sort;
        if (k === this.sortKey) this.sortDesc = !this.sortDesc;
        else { this.sortKey = k; this.sortDesc = k !== "name"; }   // name A→Z first, the others largest/newest first
        this.saveSort();
        this.render();
      };
    });
    // "↑" also takes drops: move the dragged items to the parent folder
    this.dropTarget($('[data-op="up"]', el), () => this.parent());
    this.moveType = `application/x-ic-move-${this.root}`;
    this.list.addEventListener("keydown", (e) => this.onKey(e));
    this.list.tabIndex = 0;
  }

  async refresh() {
    try {
      this.data = await api("GET", `/api/files?root=${this.root}&path=${encodeURIComponent(this.path)}`);
    } catch (e) {
      if (this.path) { this.cd(""); return; }   // folder vanished: back to the root
      this.list.innerHTML = `<div class="fb-empty error">${esc(e.message)}</div>`;
      return;
    }
    const names = new Set([...this.data.folders, ...this.data.files].map((x) => x.path));
    this.sel = new Set([...this.sel].filter((p) => names.has(p)));
    if (this.cursor && !names.has(this.cursor)) this.cursor = null;
    this.render();
  }

  saveSort() {
    localStorage.setItem(`ic.sort.${this.root}`, this.sortKey);
    localStorage.setItem(`ic.sortDesc.${this.root}`, this.sortDesc ? "1" : "0");
    $$(".fb-cols [data-sort]", this.el).forEach((h) => {
      const on = h.dataset.sort === this.sortKey;
      h.classList.toggle("on", on);
      h.dataset.dir = on ? (this.sortDesc ? "▼" : "▲") : "";
      h.title = on ? (this.sortDesc ? "내림차순 (클릭: 오름차순)" : "오름차순 (클릭: 내림차순)") : "클릭: 이 기준으로 정렬";
    });
  }

  sorted(items) {
    const k = this.sortKey;
    const byName = (a, b) => a.name.localeCompare(b.name, "ko", { numeric: true, sensitivity: "base" });
    // folders have no length / size: they sort by name then; unknown durations go last either way
    const val = (x) => (k === "mtime" ? x.mtime : x[k]);
    const cmp = k === "name" ? byName : (a, b) => {
      const x = val(a), y = val(b);
      if (x == null || y == null) return x == null && y == null ? byName(a, b) : x == null ? 1 : -1;
      return (x - y || byName(a, b)) * (this.sortDesc ? -1 : 1);
    };
    if (k !== "name") return [...items].sort(cmp);
    return [...items].sort((a, b) => (this.sortDesc ? -cmp(a, b) : cmp(a, b)));
  }

  parent() {
    const parts = this.path.split("/").filter(Boolean);
    parts.pop();
    return parts.join("/");
  }

  /* el accepts a drag of items from this panel; dest() gives the folder to move into */
  dropTarget(el, dest) {
    el.addEventListener("dragover", (e) => {
      if (!e.dataTransfer.types.includes(this.moveType)) return;
      if (el.dataset.op === "up" && !this.path) return;   // already at the root
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      el.classList.add("droptarget");
    });
    el.addEventListener("dragleave", () => el.classList.remove("droptarget"));
    el.addEventListener("drop", (e) => {
      el.classList.remove("droptarget");
      const d = e.dataTransfer.getData(this.moveType);
      if (!d) return;
      e.preventDefault();
      e.stopPropagation();
      this.move(JSON.parse(d), dest());
    });
  }

  async move(paths, dest) {
    paths = paths.filter((p) => p !== dest);
    if (!paths.length) return;
    try {
      const r = await api("POST", "/api/files/move", { root: this.root, paths, dest });
      toast(`${r.moved}개 항목을 /${dest} 로 이동`);
      this.sel.clear();
    } catch (e) { toast(e.message, true); }
    this.refresh();
  }

  cd(path, cursor = null) {
    this.path = path;
    localStorage.setItem(`ic.fb.${this.root}`, path);
    this.sel.clear();
    this.cursor = this.anchor = cursor;   // Backspace: the cursor lands on the folder we came from
    this.refresh();
  }

  render() {
    $(".crumb", this.el).textContent = "/" + (this.data.path || "");
    const fl = this.data.files, rec = fl.filter((f) => f.recording).length;
    $(".fb-sum", this.el).textContent = `폴더 ${this.data.folders.length} · 파일 ${fl.length} (${bytes(fl.reduce((a, f) => a + f.size, 0))})`
      + (rec ? ` · 녹화중 ${rec}` : "") + (this.data.free != null ? ` · 여유 ${bytes(this.data.free)}` : "");
    const rows = [];
    for (const f of this.sorted(this.data.folders)) {
      rows.push(`<div class="fb-row folder" draggable="true" data-path="${esc(f.path)}" data-dir="1">
        <span class="nm">${esc(f.name)}</span><span class="d"></span><span class="s"></span><span class="t">${dateStr(f.mtime)}</span></div>`);
    }
    for (const f of this.sorted(this.data.files)) {
      let tags = "";
      if (f.recording) tags += '<span class="tag rec">REC</span>';
      else if (this.root === "capture" && f.proxy_building) tags += '<span class="tag building">PROXY…</span>';
      else if (this.root === "capture" && f.ts) tags += '<span class="tag live" title="프록시 없음 — 브라우저에서 원본 TS 를 직접 디코딩합니다">TS</span>';
      else if (this.root === "capture" && !f.has_proxy) tags += '<span class="tag noproxy" title="프록시 없음 — 플레이어로 보내면 생성합니다">NO PROXY</span>';
      const busy = f.busy ? ` data-busy="1" title="${esc(f.name)}\n${esc(f.busy)}"` : ` title="${esc(f.name)}"`;
      rows.push(`<div class="fb-row file${f.busy ? " busy" : ""}" draggable="true" data-path="${esc(f.path)}"${busy}>
        <span class="nm">${tags}${esc(f.name)}</span><span class="d">${hms(f.duration)}</span>
        <span class="s">${bytes(f.size)}</span><span class="t">${dateStr(f.mtime)}</span></div>`);
    }
    this.list.innerHTML = rows.join("") || '<div class="fb-empty">비어 있음</div>';
    $$(".fb-row", this.list).forEach((r) => {
      const p = r.dataset.path;
      r.onclick = (e) => this.select(p, e);
      r.ondblclick = () => (r.dataset.dir ? this.cd(p) : Player.load(this.root, p));
      r.ondragstart = (e) => {
        // dragging an unselected row drags just that row; a selected row drags the whole selection
        if (!this.sel.has(p)) {
          this.sel = new Set([p]);
          this.cursor = this.anchor = p;
          this.mark();
        }
        e.dataTransfer.setData(this.moveType, JSON.stringify([...this.sel]));
        if (!r.dataset.dir) {
          e.dataTransfer.setData("application/x-instantclip", JSON.stringify({ root: this.root, path: p }));
          // dropped on mpv / VLC (or their "open network stream"): streams the file over HTTP with seeking
          e.dataTransfer.setData("text/uri-list", this.url(p));
          e.dataTransfer.setData("text/plain", this.url(p));
        }
        e.dataTransfer.effectAllowed = "copyMove";
      };
      if (r.dataset.dir) this.dropTarget(r, () => p);
    });
    this.mark();
  }

  /* selection / cursor classes in place: rebuilding rows on a click would cancel a drag or double click */
  mark() {
    for (const r of this.rows()) {
      r.classList.toggle("sel", this.sel.has(r.dataset.path));
      r.classList.toggle("cur", r.dataset.path === this.cursor);
    }
  }

  rows() { return $$(".fb-row", this.list); }

  /* absolute URL of a file, every path segment encoded (Korean names, spaces); Range and HEAD work */
  url(p) {
    return `${location.origin}/api/files/download?root=${this.root}&inline=1&path=${encodeURIComponent(p)}`;
  }

  /* keys while the list has focus: the cursor moves, the selection follows only with Shift / Space */
  onKey(e) {
    if (e.target !== this.list || e.altKey || e.metaKey) return;
    const all = this.rows().map((r) => r.dataset.path);
    let i = all.indexOf(this.cursor);
    const page = Math.max(1, Math.floor(this.list.clientHeight / (this.rows()[0]?.offsetHeight || 22)) - 1);
    const to = { ArrowDown: i + 1, ArrowUp: i < 0 ? 0 : i - 1, PageDown: i + page, PageUp: Math.max(0, i - page), Home: 0, End: all.length - 1 }[e.code];
    const ctrl = e.ctrlKey;
    if (to !== undefined && !ctrl) {
      if (!all.length) return;
      e.preventDefault();
      i = Math.max(0, Math.min(all.length - 1, to));
      this.cursor = all[i];
      if (e.shiftKey) this.range(this.anchor ?? this.cursor, this.cursor, false);
      else this.anchor = this.cursor;
      this.mark();
      this.rows()[i].scrollIntoView({ block: "nearest" });
      return;
    }
    const cur = this.cursor;
    const k = (ctrl ? "C-" : "") + e.code;
    const act = {
      Space: () => {
        if (!cur) return;
        this.sel.has(cur) ? this.sel.delete(cur) : this.sel.add(cur);
        this.anchor = cur;
        const n = all[all.indexOf(cur) + 1];
        if (n) this.cursor = n;
        this.mark();
        this.rows()[all.indexOf(this.cursor)]?.scrollIntoView({ block: "nearest" });
      },
      Enter: () => {
        if (!cur) return this.op("load");
        return this.isFile(cur) ? Player.load(this.root, cur) : this.cd(cur);
      },
      NumpadEnter: () => act.Enter(),
      Backspace: () => this.path && this.cd(this.parent(), this.path),
      Delete: () => this.op("delete"),
      F2: () => this.op("rename"),
      "C-KeyA": () => { this.sel = new Set(this.rows().filter((r) => !r.dataset.busy).map((r) => r.dataset.path)); this.mark(); },
      Escape: () => { this.sel.clear(); this.mark(); },
    }[k];
    if (!act) return;
    e.preventDefault();
    e.stopPropagation();
    act();
  }

  /* select a..b (rows in list order), skipping busy rows; add keeps the current selection */
  range(a, b, add) {
    const rows = this.rows(), all = rows.map((r) => r.dataset.path);
    let [x, y] = [all.indexOf(a), all.indexOf(b)].sort((m, n) => m - n);
    if (x < 0) x = y;
    if (!add) this.sel.clear();
    rows.slice(x, y + 1).filter((r) => !r.dataset.busy).forEach((r) => this.sel.add(r.dataset.path));
  }

  /* recording rows: duration follows the recorder clock between list refreshes */
  tick(recorders) {
    if (this.root !== "capture") return;
    for (const c of recorders) {
      if (c.mode !== "recording" || !c.clip) continue;
      const row = this.list.querySelector(`.fb-row[data-path="${CSS.escape(c.clip)}"] .d`);
      if (row) row.textContent = hms(c.elapsed);
    }
  }

  select(p, e) {
    const all = this.rows().map((r) => r.dataset.path);
    if (e.shiftKey && this.anchor && all.includes(this.anchor)) {
      this.range(this.anchor, p, e.ctrlKey);
    } else if (e.ctrlKey || e.metaKey) {
      this.sel.has(p) ? this.sel.delete(p) : this.sel.add(p);
      this.anchor = p;
    } else {
      this.sel = new Set([p]);
      this.anchor = p;
    }
    this.cursor = p;
    this.mark();
  }

  /* the selection, or the cursor row when nothing is selected */
  selected() { return this.sel.size ? [...this.sel] : this.cursor ? [this.cursor] : []; }
  isFile(p) { return this.data.files.some((f) => f.path === p); }

  async op(op) {
    try { await this.op1(op); }
    finally { if (op !== "load") this.list.focus({ preventScroll: true }); }   // prompt/confirm took focus
  }

  async op1(op) {
    const sel = this.selected();
    try {
      switch (op) {
        case "up": return this.cd(this.parent());
        case "refresh": return this.refresh();
        case "mkdir": {
          const name = prompt("새 폴더 이름");
          if (!name) return;
          await api("POST", "/api/files/mkdir", { root: this.root, path: this.path, name });
          return this.refresh();
        }
        case "rename": {
          if (sel.length !== 1) return toast("이름을 바꿀 항목 하나를 선택하세요", true);
          const old = sel[0].split("/").pop();
          const name = prompt("새 이름", old);
          if (!name || name === old) return;
          await api("POST", "/api/files/rename", { root: this.root, path: sel[0], name });
          return this.refresh();
        }
        case "delete": {
          if (!sel.length) return toast("삭제할 항목을 선택하세요", true);
          const dirs = sel.filter((p) => !this.isFile(p)).length;
          const extra = this.root === "capture" ? "\n(프록시·썸네일도 함께 삭제됩니다)" : "";
          if (!confirm(`${sel.length}개 항목을 삭제할까요?${dirs ? `\n폴더 ${dirs}개는 안의 내용까지 모두 삭제됩니다.` : ""}${extra}`)) return;
          const r = await api("POST", "/api/files/delete", { root: this.root, paths: sel });
          const bad = (r.results || []).filter((x) => !x.ok);
          if (bad.length) toast(`삭제 실패 ${bad.length}건: ${bad.slice(0, 3).map((x) => `${x.path.split("/").pop()} — ${x.error}`).join(" / ")}`, true);
          this.sel.clear();
          return this.refresh();
        }
        case "load": {
          const f = sel.find((p) => this.isFile(p));
          if (!f) return toast("클립을 선택하세요", true);
          return Player.load(this.root, f);
        }
        case "url": {
          const f = sel.find((p) => this.isFile(p));
          if (!f) return toast("파일을 선택하세요", true);
          await copyText(this.url(f));
          return toast("URL 복사됨 — mpv / VLC 의 '네트워크 스트림 열기'에 붙여 넣으세요");
        }
        case "download": {
          const files = sel.filter((p) => this.isFile(p));
          if (!files.length) return toast("다운로드할 subclip 을 선택하세요", true);
          files.forEach((p, i) => setTimeout(() => {
            const a = document.createElement("a");
            a.href = `/api/files/download?root=${this.root}&path=${encodeURIComponent(p)}`;
            a.download = p.split("/").pop();
            document.body.append(a);
            a.click();
            a.remove();
          }, i * 400));
          return;
        }
      }
    } catch (e) { toast(e.message, true); }
  }
}

const Browser = {
  panels: {},
  rev: -1,
  init() {
    $$(".fb").forEach((el) => { this.panels[el.dataset.root] = new FileBrowser(el); });
    Object.values(this.panels).forEach((p) => p.saveSort());
    Object.values(this.panels).forEach((p) => p.refresh());
  },
  folder(root) { return this.panels[root].path; },
  onStatus(st) {
    if (st.rev !== this.rev) {
      if (this.rev !== -1) Object.values(this.panels).forEach((p) => p.refresh());
      this.rev = st.rev;
    }
    // sizes of growing files: re-list the capture panel every 10 s while anything records
    const now = Date.now();
    if (st.recorders.some((c) => c.mode === "recording") && now - (this.lastRecRefresh || 0) > 10000) {
      this.lastRecRefresh = now;
      this.panels.capture.refresh();
    }
    this.panels.capture.tick(st.recorders);
  },
};
