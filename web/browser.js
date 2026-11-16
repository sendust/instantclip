"use strict";
/* 4. 호스트 미디어 브라우저: capture panel (recorded / recording clips) + subclip panel */
class FileBrowser {
  constructor(el) {
    this.el = el;
    this.root = el.dataset.root;
    this.path = localStorage.getItem(`ic.fb.${this.root}`) || "";
    this.data = { folders: [], files: [] };
    this.sel = new Set();
    this.anchor = null;
    this.list = $(".fb-list", el);
    $$(".fb-tools [data-op]", el).forEach((b) => { b.onclick = () => this.op(b.dataset.op); });
    // sort: name | mtime (생성일시), ascending or descending; remembered per panel
    this.sortKey = localStorage.getItem(`ic.sort.${this.root}`) || "mtime";
    this.sortDesc = localStorage.getItem(`ic.sortDesc.${this.root}`) !== "0";
    this.sortSel = $(".fb-sort", el);
    this.sortSel.value = this.sortKey;
    this.sortSel.onchange = () => { this.sortKey = this.sortSel.value; this.saveSort(); this.render(); };
    // "↑" also takes drops: move the dragged items to the parent folder
    this.dropTarget($('[data-op="up"]', el), () => this.parent());
    this.moveType = `application/x-ic-move-${this.root}`;
    this.list.addEventListener("keydown", (e) => {
      if (e.code === "Delete") this.op("delete");
      else if (e.code === "F2") this.op("rename");
      else if (e.code === "Enter") this.op("load");
    });
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
    this.render();
  }

  saveSort() {
    localStorage.setItem(`ic.sort.${this.root}`, this.sortKey);
    localStorage.setItem(`ic.sortDesc.${this.root}`, this.sortDesc ? "1" : "0");
    $(".fb-dir", this.el).textContent = this.sortDesc ? "↓" : "↑";
    $(".fb-dir", this.el).title = this.sortDesc ? "내림차순 (클릭: 오름차순)" : "오름차순 (클릭: 내림차순)";
  }

  sorted(items) {
    const k = this.sortKey;
    const cmp = k === "name"
      ? (a, b) => a.name.localeCompare(b.name, "ko", { numeric: true, sensitivity: "base" })
      : (a, b) => a.mtime - b.mtime;
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

  cd(path) {
    this.path = path;
    localStorage.setItem(`ic.fb.${this.root}`, path);
    this.sel.clear();
    this.refresh();
  }

  render() {
    $(".crumb", this.el).textContent = "/" + (this.data.path || "");
    const rows = [];
    for (const f of this.sorted(this.data.folders)) {
      rows.push(`<div class="fb-row folder" draggable="true" data-path="${esc(f.path)}" data-dir="1">
        <span class="nm">${esc(f.name)}</span><span class="d"></span><span class="s"></span><span class="t">${dateStr(f.mtime)}</span></div>`);
    }
    for (const f of this.sorted(this.data.files)) {
      let tags = "";
      if (f.recording) tags += '<span class="tag rec">REC</span>';
      else if (this.root === "capture" && f.proxy_building) tags += '<span class="tag building">PROXY…</span>';
      else if (this.root === "capture" && f.live) tags += '<span class="tag live" title="프록시 없음 — 재생하는 구간만 실시간 변환합니다">LIVE</span>';
      else if (this.root === "capture" && !f.has_proxy) tags += '<span class="tag noproxy" title="프록시 없음 — 플레이어로 보내면 생성합니다">NO PROXY</span>';
      rows.push(`<div class="fb-row file" draggable="true" data-path="${esc(f.path)}" title="${esc(f.name)}">
        <span class="nm">${tags}${esc(f.name)}</span><span class="d">${hms(f.duration)}</span>
        <span class="s">${bytes(f.size)}</span><span class="t">${dateStr(f.mtime)}</span></div>`);
    }
    this.list.innerHTML = rows.join("") || '<div class="fb-empty">비어 있음</div>';
    $$(".fb-row", this.list).forEach((r) => {
      const p = r.dataset.path;
      r.classList.toggle("sel", this.sel.has(p));
      r.onclick = (e) => this.select(p, e);
      r.ondblclick = () => (r.dataset.dir ? this.cd(p) : Player.load(this.root, p));
      r.ondragstart = (e) => {
        // dragging an unselected row drags just that row; a selected row drags the whole selection
        if (!this.sel.has(p)) {
          this.sel = new Set([p]);
          this.anchor = p;
          $$(".fb-row", this.list).forEach((x) => x.classList.toggle("sel", x.dataset.path === p));
        }
        e.dataTransfer.setData(this.moveType, JSON.stringify([...this.sel]));
        if (!r.dataset.dir) e.dataTransfer.setData("application/x-instantclip", JSON.stringify({ root: this.root, path: p }));
        e.dataTransfer.effectAllowed = "copyMove";
      };
      if (r.dataset.dir) this.dropTarget(r, () => p);
    });
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
    const all = $$(".fb-row", this.list).map((r) => r.dataset.path);
    if (e.shiftKey && this.anchor && all.includes(this.anchor)) {
      const [a, b] = [all.indexOf(this.anchor), all.indexOf(p)].sort((x, y) => x - y);
      if (!e.ctrlKey) this.sel.clear();
      all.slice(a, b + 1).forEach((x) => this.sel.add(x));
    } else if (e.ctrlKey || e.metaKey) {
      this.sel.has(p) ? this.sel.delete(p) : this.sel.add(p);
      this.anchor = p;
    } else {
      this.sel = new Set([p]);
      this.anchor = p;
    }
    this.render();
  }

  selected() { return [...this.sel]; }
  isFile(p) { return this.data.files.some((f) => f.path === p); }

  async op(op) {
    const sel = this.selected();
    try {
      switch (op) {
        case "up": return this.cd(this.parent());
        case "sortdir":
          this.sortDesc = !this.sortDesc;
          this.saveSort();
          return this.render();
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
          await api("POST", "/api/files/delete", { root: this.root, paths: sel });
          this.sel.clear();
          return this.refresh();
        }
        case "load": {
          const f = sel.find((p) => this.isFile(p));
          if (!f) return toast("클립을 선택하세요", true);
          return Player.load(this.root, f);
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
