// 訪問前の1枚: 書いた答えを自動で保存し、コピーと印刷にも入れる。
(function () {
  var data = document.getElementById("sheet-data");
  var url = data.dataset.url;
  var state = document.getElementById("note-state");
  var timers = new Map();  // 入力の後、送るのを待っている項目
  var sending = new Map(); // 送っている途中の項目
  var dirty = new Set();   // まだ保存できていない項目（失敗した物も残る）

  function rows() { return document.querySelectorAll(".ask, .ask-row"); }

  function valueOf(row) {
    var checked = row.querySelector("input[type=radio]:checked");
    if (checked) return checked.value;
    var field = row.querySelector(".note, .note-line");
    return field ? field.value : "";
  }

  function post(row) {
    var body = new URLSearchParams({ section: row.dataset.section, label: row.dataset.label, answer: valueOf(row) });
    return fetch(url, { method: "POST", body: body, keepalive: true }).then(function (r) {
      if (!r.ok) throw new Error(r.status);
    });
  }

  function report() {
    if (dirty.size && !sending.size && !timers.size) {
      state.textContent = "保存できていない入力があります。送る前に通信を確かめてください";
      state.classList.add("note-failed");
    } else if (!dirty.size) {
      state.textContent = "保存しました";
      state.classList.remove("note-failed");
    }
  }

  // 同じ項目は1通ずつ送る。送っている間に書き足された分は、終わってから送る
  function send(row) {
    clearTimeout(timers.get(row));
    timers.delete(row);
    if (!url) return Promise.resolve();
    if (sending.has(row)) return sending.get(row);
    state.textContent = "保存しています";
    dirty.delete(row);
    var job = post(row).then(function () {
      sending.delete(row);
      if (dirty.has(row) && !timers.has(row)) return send(row);
      report();
    }, function () {
      sending.delete(row);
      dirty.add(row);
      report();
    });
    sending.set(row, job);
    return job;
  }

  function changed(row, wait) {
    if (!url) return;
    dirty.add(row);
    clearTimeout(timers.get(row));
    timers.set(row, setTimeout(function () { send(row); }, wait));
  }

  function flush() {
    var jobs = Array.from(dirty).map(send).concat(Array.from(sending.values()));
    return Promise.all(jobs).then(function () {
      if (sending.size) return flush();
      if (dirty.size) throw new Error("not saved");
    });
  }

  function grow(field) {
    field.style.height = "auto";
    field.style.height = field.scrollHeight + 2 + "px";
  }

  function mirror(row) {
    var copy = row.querySelector(".note-print");
    if (copy) copy.textContent = valueOf(row);
  }

  rows().forEach(function (row) {
    row.querySelectorAll(".note, .note-line").forEach(function (field) {
      if (field.classList.contains("note") && field.value) grow(field);
      field.addEventListener("input", function () {
        if (field.classList.contains("note")) grow(field);
        mirror(row);
        changed(row, 800);
      });
      field.addEventListener("blur", function () { if (timers.has(row)) send(row); });
    });
    row.querySelectorAll("input[type=radio]").forEach(function (radio) {
      if (radio.checked) row.dataset.picked = radio.value;
      radio.addEventListener("click", function () {
        if (row.dataset.picked === radio.value) { // もう一度押すと外せる
          radio.checked = false;
          row.dataset.picked = "";
        } else {
          row.dataset.picked = radio.value;
        }
        changed(row, 0);
      });
    });
  });

  document.querySelectorAll("form.flush-first").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var button = form.querySelector("button");
      if (button.disabled) return;
      var label = button.textContent;
      button.disabled = true;
      flush().then(function () {
        if (form.classList.contains("busy")) button.textContent = "作っています（1分ほど）";
        form.submit();
      }).catch(function () { // 保存できない時は進まない。知らせは state に出ている
        button.disabled = false;
        button.textContent = label;
      });
    });
  });

  // ページを離れる時は、順番を待たずに送る。届かないと消えるので
  window.addEventListener("pagehide", function () {
    dirty.forEach(function (row) { post(row).catch(function () {}); });
  });
  window.addEventListener("beforeunload", function (e) {
    if (Array.from(dirty).some(function (r) { return !timers.has(r) && !sending.has(r); })) e.preventDefault();
  });
  window.addEventListener("pageshow", function (e) { if (e.persisted) location.reload(); });

  function sheetText() {
    var out = [data.dataset.head];
    var section = null;
    rows().forEach(function (row) {
      if (row.dataset.section !== section) {
        section = row.dataset.section;
        out.push("", "【" + section + "】");
      }
      out.push("□ " + row.dataset.label + (row.dataset.past ? "（前回: " + row.dataset.past + "）" : ""));
      out.push("→ " + valueOf(row));
    });
    return out.join("\n") + "\n";
  }
  window.sheetText = sheetText;

  document.getElementById("copy-sheet").addEventListener("click", function () {
    navigator.clipboard.writeText(sheetText());
    this.textContent = "コピーしました";
  });
})();
