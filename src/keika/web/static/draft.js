// 下書き画面。その場で保存し、画面を描き直す。JS が無くても普通のフォームとして動く。
(function () {
  var pending = Promise.resolve();

  function main() { return document.querySelector("main"); }

  function remember() {
    var el = document.activeElement;
    if (!el || !el.name || !main().contains(el)) return null;
    var same = main().querySelectorAll('[name="' + el.name + '"]');
    return { id: el.id, name: el.name, at: Array.prototype.indexOf.call(same, el), value: el.value };
  }

  function restore(was) {
    if (!was) return;
    var el = was.id ? document.getElementById(was.id) : main().querySelectorAll('[name="' + was.name + '"]')[was.at];
    if (!el) return;
    if (el.tagName !== "BUTTON") el.value = was.value;
    el.focus({ preventScroll: true });
  }

  function say(words) {
    var line = document.querySelector(".draft-say");
    if (line) line.textContent = words;
  }

  function show(html, thenNext) {
    var fresh = new DOMParser().parseFromString(html, "text/html").querySelector("main");
    if (!fresh) return false;
    var was = remember(), y = window.scrollY;
    main().innerHTML = fresh.innerHTML;
    window.scrollTo(0, y);
    restore(was);
    if (thenNext) {
      var next = main().querySelector(".sentence.to-check");
      if (next) { next.open = true; next.scrollIntoView({ block: "center" }); }
    }
    return true;
  }

  function send(form, submitter) {
    var url = (submitter && submitter.getAttribute("formaction")) || form.getAttribute("action");  // action という名の欄があると form.action は欄を指す
    var body = new URLSearchParams(new FormData(form, submitter));
    var thenNext = !!(submitter && submitter.hasAttribute("data-then-next"));
    pending = pending.then(function () {
      return fetch(url, { method: "POST", body: body }).then(function (r) {
        return r.text().then(function (html) {
          if (!r.ok || !show(html, thenNext)) say("保存できませんでした。もう一度押してください。");
        });
      }).catch(function () { say("保存できませんでした。もう一度押してください。"); });
    });
  }

  function go(target) {
    if (!target) return;
    if (target.tagName === "DETAILS") target.open = true;
    target.scrollIntoView({ block: "center" });
    if (target.tagName === "INPUT" || target.tagName === "TEXTAREA") target.focus({ preventScroll: true });
  }

  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!form.hasAttribute("data-inplace")) return;
    e.preventDefault();
    send(form, e.submitter);
  });

  document.addEventListener("change", function (e) {
    var el = e.target;
    if (el.hasAttribute && el.hasAttribute("data-autosave") && el.form) send(el.form, null);
  });

  document.addEventListener("toggle", function (e) {  // 開いた中身が画面の下に隠れないように
    if (e.target.open && e.target.classList.contains("sentence")) e.target.scrollIntoView({ block: "nearest" });
  }, true);

  document.addEventListener("focusin", function (e) {
    var el = e.target;
    if (el.classList && (el.classList.contains("is-blank") || el.classList.contains("is-kept")) && el.select && el.value === "〇〇") el.select();
  });

  document.addEventListener("click", function (e) {
    if (!e.target.closest) return;
    var next = e.target.closest(".draft-next");
    if (next) {
      e.preventDefault();
      go(document.getElementById(next.getAttribute("href").slice(1)));
      return;
    }
    var word = e.target.closest(".draft-word");
    if (word) {  // 書いたばかりの欄の保存を待ってから Word を作る
      e.preventDefault();
      pending.then(function () {
        var left = Number(word.dataset.left || 0);
        window.location.href = word.getAttribute("href");
        say(left ? "残り " + left + " 件のまま保存しました。〇〇と【確かめる】の印が入っています。" : "保存しました。");
      });
      return;
    }
    var copy = e.target.closest("#copy-draft");
    if (copy) {
      navigator.clipboard.writeText(copy.dataset.text);
      copy.textContent = "コピーしました";
    }
  });

  if (location.hash.length > 1 && location.hash !== "#kakunin") go(document.getElementById(decodeURIComponent(location.hash.slice(1))));
})();
