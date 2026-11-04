// ケアプランの下書き。打ったマスを1つずつ自動で保存する。保存できなかった内容は画面に残し、先へ進ませない。
(function () {
  var sheet = document.getElementById("plan-sheet");
  var url = sheet.dataset.url;
  var state = document.getElementById("plan-state");
  var timers = new Map();
  var sending = new Map();
  var dirty = new Set();
  var conflict = new Set();
  var saved = new Map();

  function grow(box) {
    box.style.height = "auto";
    box.style.height = box.scrollHeight + "px";
  }

  function show() {
    if (conflict.size) state.textContent = "別の画面で直されたマスがあります。打った文を控えて、開き直してください。";
    else if (dirty.size) state.textContent = "保存できていないマスがあります。つながりを確かめてください。";
    else if (sending.size || timers.size) state.textContent = "保存しています";
    else state.textContent = "保存しました";
  }

  function mark(box, failed) {
    box.parentElement.classList.toggle("saved-failed", failed);
  }

  function send(box) {
    var item = box.dataset.item;
    if (sending.has(item) || conflict.has(item)) return sending.get(item) || Promise.resolve();
    var text = box.value;
    if (text === saved.get(item)) {
      dirty.delete(item);
      show();
      return Promise.resolve();
    }
    var body = new FormData();
    body.append("item", item);
    body.append("text", text);
    body.append("before", saved.get(item));
    var job = fetch(url, { method: "POST", body: body, keepalive: text.length < 30000 }).then(function (r) {
      sending.delete(item);
      if (r.status === 409) {
        conflict.add(item);
        mark(box, true);
      } else if (!r.ok) {
        throw new Error("save");
      } else {
        saved.set(item, text);
        box.parentElement.classList.remove("flagged");
        var flag = box.parentElement.querySelector(".draft-flag");
        if (flag) flag.remove();
        if (box.value === text) {
          dirty.delete(item);
          mark(box, false);
        } else {
          return send(box);  // 送っている間に打った分
        }
      }
      show();
    }).catch(function () {
      sending.delete(item);
      dirty.add(item);
      mark(box, true);
      show();
    });
    sending.set(item, job);
    show();
    return job;
  }

  function flush() {
    timers.forEach(function (t) { clearTimeout(t); });
    timers.clear();
    var boxes = Array.prototype.slice.call(sheet.querySelectorAll(".sheet-cell"));
    return Promise.all(boxes.map(send)).then(function () {
      return Promise.all(Array.from(sending.values()));
    }).then(function () {
      if (dirty.size || conflict.size) throw new Error("unsaved");
    });
  }

  sheet.querySelectorAll(".sheet-cell").forEach(function (box) {
    saved.set(box.dataset.item, box.value.replace(/\r\n/g, "\n"));
    grow(box);
    if (!url) return;
    box.addEventListener("input", function () {
      grow(box);
      box.parentElement.querySelector(".cell-print").textContent = box.value;
      var item = box.dataset.item;
      dirty.add(item);
      clearTimeout(timers.get(item));
      timers.set(item, setTimeout(function () {
        timers.delete(item);
        send(box);
      }, 600));
      show();
    });
  });

  document.querySelectorAll("form.flush-first").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      if (!url) return;
      e.preventDefault();
      flush().then(function () {
        if (form.classList.contains("busy-button")) {
          var button = form.querySelector("button");
          button.disabled = true;
          button.textContent = "作業しています（1〜2分）";
        }
        form.submit();
      }).catch(show);
    });
  });
  document.querySelectorAll("a.flush-link").forEach(function (link) {
    link.addEventListener("click", function (e) {
      if (!url) return;
      e.preventDefault();
      flush().then(function () { location.href = link.href; }).catch(show);
    });
  });
  document.getElementById("print-plan").addEventListener("click", function () {
    (url ? flush() : Promise.resolve()).then(function () { window.print(); }).catch(show);
  });
  window.addEventListener("beforeunload", function (e) {
    if (url && (dirty.size || timers.size || sending.size)) {
      e.preventDefault();
      e.returnValue = "";
    }
  });
  window.addEventListener("pageshow", function (e) {
    if (e.persisted) location.reload();  // 古い文を残したまま打たせない
  });
})();
